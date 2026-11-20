"""共享配置、权限、OAuth 和模型路由。"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

from gateway_runtime.http import HttpError
from gateway_runtime.store import D1, StateStore
from src.config.provider_config import RuntimeProviderSpec
from src.external import LLMProvider

_HOOK_CACHE: dict[str, Any] = {}


@dataclass
class RouteConfig:
    request_model: str
    target_format: str
    client_ip: str
    providers: list[LLMProvider]
    key: dict[str, Any] | None = None
    user: dict[str, Any] | None = None
    key_id: str | None = None
    request_id: str = ""
    snapshot: dict[str, Any] = field(default_factory=dict)
    mapping: dict[str, Any] | None = None
    targets_by_provider: dict[str, str] = field(default_factory=dict)
    force_privacy: bool = False
    auth_entries: dict[str, list[dict[str, Any]]] = field(default_factory=dict)


class GatewayServices:
    """管理面与数据面共用的 Workers 服务。"""

    def __init__(self, env: Any, root: Any, ctx: Any):
        self.env = env
        self.ctx = ctx
        self.store = StateStore(D1(env.DB))
        self.root = root

    async def initialize(self) -> None:
        await self.store.initialize()

    async def snapshot(self) -> dict[str, Any]:
        return {
            kind: await self.store.list(kind)
            for kind in ("providers", "auth_groups", "users", "api_keys", "mappings", "oauth", "settings")
        }

    def setting(self, snapshot: dict[str, Any], key: str, default: Any = None) -> Any:
        for item in snapshot["settings"]:
            if item.get("id") == key:
                return item.get("value", default)
        return default

    async def authenticate(self, request: Any) -> dict[str, Any]:
        settings = (await self.store.get("settings", "admin")) or {}
        enabled = bool(settings.get("username") and settings.get("password_hash"))
        cookie = _cookies(request.headers.get("Cookie") or "")
        session = await self.store.get("sessions", cookie.get("session_token", ""))
        authenticated = bool(session and session.get("expires", 0) > time.time()) or not enabled
        if not authenticated:
            raise HttpError("Authentication required", 401, "unauthorized")
        return {"enabled": enabled, "username": session.get("username", "") if session else ""}

    async def login(self, body: dict[str, Any]) -> tuple[str, str]:
        admin = (await self.store.get("settings", "admin")) or {}
        username = str(body.get("username") or "")
        password = str(body.get("password") or "")
        if not admin.get("username") or not hmac.compare_digest(username, admin.get("username", "")):
            raise HttpError("Invalid username or password", 401, "invalid_credentials")
        expected = admin.get("password_hash", "")
        actual = await _password_hash(password, admin.get("salt", ""))
        if not expected or not hmac.compare_digest(actual, expected):
            raise HttpError("Invalid username or password", 401, "invalid_credentials")
        token = secrets.token_urlsafe(32)
        await self.store.put("sessions", token, {"username": username, "expires": time.time() + 86400})
        return token, username

    async def resolve_route(self, model: str, request: Any, body: dict[str, Any], target: str) -> RouteConfig:
        snapshot = await self.snapshot()
        client_ip = request.headers.get("CF-Connecting-IP") or ""
        users = [user for user in snapshot["users"] if user.get("ip_address") == client_ip]
        user = users[0] if users else None
        if self.setting(snapshot, "whitelist", False) and not (user and user.get("whitelist_access_enabled", True)):
            raise HttpError("Client IP is not allowed", 403, "whitelist_denied")
        key = await self._api_key(request, snapshot)
        permissions = _permissions(user, key)
        if permissions is not None and "*" not in permissions and model not in permissions:
            raise HttpError("Model is not permitted", 403, "model_not_permitted")
        if not self.setting(snapshot, "oauth_enabled", True) and any(
            account.get("model") == model for account in snapshot["oauth"]
        ):
            raise HttpError("OAuth routing is disabled", 403, "oauth_disabled")
        mappings = [
            mapping for mapping in snapshot["mappings"] if mapping["id"] == model and mapping.get("enabled", True)
        ]
        models: list[str] = []
        mapping = mappings[0] if mappings and self.setting(snapshot, "mapping_enabled", True) else None
        if mapping:
            targets = mapping.get("targets") or []
            if len(targets) > 1:
                selected = await self.stub(f"mapping:{model}").select_mapping(
                    targets, {"strategy": mapping.get("strategy")}
                )
                model_target = (await _maybe_await(selected)).get("target")
                targets = ([model_target] if model_target else []) + [
                    target for target in targets if target != model_target
                ]
            models = [target["model_id"] for target in targets if target and target.get("enabled", True)]
        else:
            models = [model]
        providers: list[LLMProvider] = []
        targets_by_provider: dict[str, str] = {}
        for candidate in models:
            for provider in await self._providers(candidate, snapshot):
                if provider.supports_target_format(target) and provider.name not in {item.name for item in providers}:
                    providers.append(provider)
                    targets_by_provider[provider.name] = candidate
        if not providers:
            raise HttpError(f"Model not found: {model}", 404, "model_not_found")
        return RouteConfig(
            request_model=model,
            target_format=target,
            client_ip=client_ip,
            providers=providers,
            key=key,
            user=user,
            key_id=str(key["id"]) if key else None,
            mapping=mapping,
            targets_by_provider=targets_by_provider,
            force_privacy=bool(
                mapping and mapping.get("force_safe_desensitization", mapping.get("safe_desensitization_enabled"))
            ),
            snapshot=snapshot,
        )

    async def authorize(self, config: RouteConfig, request_id: str) -> None:
        if not config.key:
            return
        key = config.key
        enabled = self.setting(config.snapshot, "api_keys_enabled", True)
        if not enabled or not key.get("enabled", True):
            raise HttpError("Invalid API key", 401, "invalid_api_key")
        limits = key.get("limits") or {}
        result = await self.stub(f"key:{key['id']}").acquire_key(
            request_id,
            {
                "limit": int(limits.get("concurrency") or 0),
                "minute_limit": int(limits.get("per_minute") or 0),
                "daily_limit": int(limits.get("per_day") or 0),
                "token_limit": int(key.get("token_limit") or 0),
            },
        )
        if not result.get("ok"):
            raise HttpError(f"API key quota exceeded: {result.get('reason')}", 429, "quota_exceeded")

    async def finish(self, config: RouteConfig, request_id: str, entry: dict[str, Any]) -> None:
        entry.pop("snapshot", None)
        entry.pop("_streaming", None)
        if config.key:
            await self.stub(f"key:{config.key['id']}").finish_key(request_id, entry.get("total_tokens", 0))
        try:
            await self.store.record(entry)
        except Exception:
            return

    async def acquire_auth(self, provider: LLMProvider) -> dict[str, Any] | None:
        groups = await self.store.list("auth_groups")
        group = next((group for group in groups if group.get("name") == provider.auth_group), None)
        if not group:
            return None
        result = await self.stub(f"auth:{group['name']}").acquire_auth(
            group.get("entries") or [], {"strategy": group.get("strategy", "least_inflight")}
        )
        return result.get("entry")

    async def finish_auth(
        self, provider: LLMProvider, entry: dict[str, Any] | None, status: int, retry_after: Any = None
    ) -> None:
        if not entry or not provider.auth_group:
            return
        seconds = int(retry_after) if str(retry_after or "").isdigit() else 60
        await self.stub(f"auth:{provider.auth_group}").finish_auth(
            entry["id"], {"status": status, "retry_after": seconds}
        )

    async def fail_target(self, config: RouteConfig, provider: LLMProvider, status: int) -> None:
        if config.mapping:
            model_id = config.targets_by_provider.get(provider.name, provider.name)
            await self.stub(f"mapping:{config.request_model}").fail_mapping(model_id, {"status": status})

    async def _providers(self, model: str, snapshot: dict[str, Any]) -> list[LLMProvider]:
        candidates: list[LLMProvider] = []
        for item in snapshot["providers"]:
            if not item.get("enabled", True):
                continue
            prefix = f"{item['name']}/"
            if model.startswith(prefix) and model[len(prefix) :] in item.get("model_list", []):
                candidates.append(self._runtime(item))
        for account in snapshot["oauth"]:
            if account.get("model") != model or not account.get("enabled", True):
                continue
            try:
                credentials = await self.oauth_credentials(account)
            except HttpError:
                continue
            headers = {
                "chatgpt-account-id": credentials.get("account_id") or account.get("account_id") or "",
                "OpenAI-Beta": "responses=experimental",
                "originator": "codex_cli_rs",
                "User-Agent": "codex_cli_rs/0.76.0",
                **(account.get("headers") or {}),
            }
            if account.get("source_format") == "claude_chat" or "anthropic" in account.get("api", ""):
                headers = {
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "oauth-2025-04-20",
                    "User-Agent": "claude-cli/2.0.0",
                    **(account.get("headers") or {}),
                }
            headers = {key: value for key, value in headers.items() if value}
            candidates.append(
                LLMProvider(
                    name=account["id"],
                    api=account["api"],
                    source_format=account.get("source_format", "openai_responses"),
                    model_list=(model,),
                    api_key=credentials.get("access_token"),
                    max_retries=account.get("max_retries", 3),
                    extra_headers=tuple(headers.items()),
                )
            )
        return candidates

    async def oauth_credentials(self, account: dict[str, Any]) -> dict[str, Any]:
        """以账号 DO 为权威保存 token；必要时执行有界刷新。"""
        stub = self.stub(f"oauth:{account['id']}")
        credentials = await _maybe_await(stub.oauth_snapshot())
        if credentials is None:
            credentials = await _maybe_await(stub.save_oauth(account))
        if account.get("refresh_token") and credentials.get("expires_at", 0) <= time.time() + 60:
            lease = await stub.oauth_refresh(account["id"])
            if lease.get("ok"):
                credentials = await self._refresh_oauth(account, credentials, stub)
            else:
                credentials = await _maybe_await(stub.oauth_snapshot())
        return credentials

    async def _refresh_oauth(self, account: dict[str, Any], credentials: dict[str, Any], stub: Any) -> dict[str, Any]:
        from workers import fetch

        response = await fetch(
            account["token_url"],
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            body=_form(
                {
                    "grant_type": "refresh_token",
                    "refresh_token": credentials["refresh_token"],
                    "client_id": account["client_id"],
                    **({"client_secret": account["client_secret"]} if account.get("client_secret") else {}),
                }
            ),
        )
        if response.status != 200:
            raise HttpError("OAuth token refresh failed", 502, "oauth_refresh_failed")
        payload = await response.json()
        if not payload.get("access_token"):
            raise HttpError("OAuth refresh returned no access token", 502, "oauth_refresh_failed")
        return await stub.save_oauth(
            {
                "access_token": payload["access_token"],
                "refresh_token": payload.get("refresh_token", credentials["refresh_token"]),
                "expires_at": time.time() + int(payload.get("expires_in", 3600)),
                "refresh_until": 0,
            }
        )

    def _runtime(self, payload: dict[str, Any]) -> LLMProvider:
        from src.config.provider_config import SUPPORTED_PROVIDER_FIELDS

        spec = RuntimeProviderSpec.from_mapping(
            {key: value for key, value in payload.items() if key in SUPPORTED_PROVIDER_FIELDS}
        )
        hook = None
        if spec.hook:
            hook = _load_hook(self.root / "hooks" / spec.hook)
        return LLMProvider(
            name=spec.name,
            api=_upstream_api_endpoint(spec.api, spec.source_format),
            transport=spec.transport,
            source_format=spec.source_format,
            target_formats=spec.target_formats,
            api_key=spec.api_key,
            auth_group=spec.auth_group,
            model_list=spec.model_list,
            proxy_mode=spec.proxy_mode,
            proxy=spec.proxy,
            timeout_seconds=spec.timeout_seconds,
            max_retries=spec.max_retries,
            verify_ssl=spec.verify_ssl,
            force_upstream_stream=spec.force_upstream_stream,
            safe_desensitization_enabled=spec.safe_desensitization_enabled,
            hook=hook,
        )

    async def _api_key(self, request: Any, snapshot: dict[str, Any]) -> dict[str, Any] | None:
        authorization = request.headers.get("Authorization") or ""
        supplied = (
            authorization[7:].strip()
            if authorization.lower().startswith("bearer ")
            else request.headers.get("X-API-Key")
        )
        enabled = self.setting(snapshot, "api_keys_enabled", False)
        if not enabled:
            return None
        if not supplied:
            raise HttpError("API key required", 401, "invalid_api_key")
        digest = hashlib.sha256(supplied.encode()).hexdigest()
        key = next((item for item in snapshot["api_keys"] if item.get("key_hash") == digest), None)
        if not key or not key.get("enabled", True):
            raise HttpError("Invalid API key", 401, "invalid_api_key")
        return key

    async def record_llm(self, env: Any, entry: dict[str, Any], request: Any, output: Any) -> None:
        """按设置将脱敏后的请求摘要和输出写入 R2，记录失败不影响响应。"""
        snapshot = entry.pop("snapshot", None) or {}
        settings = self.setting(snapshot, "llm_recording_enabled", False)
        if not settings:
            return
        try:
            record = {
                "request_id": entry["request_id"],
                "created_at": entry["created_at"],
                "request_model": entry["request_model"],
                "provider": entry.get("provider"),
                "input": _bounded(request),
                "output": _bounded(output),
                "usage": {key: entry.get(key, 0) for key in ("prompt_tokens", "completion_tokens", "total_tokens")},
                "outcome": entry.get("outcome"),
            }
            await _maybe_await(
                env.RECORDS.put(f"llm/{entry['request_id']}.json", json.dumps(record, ensure_ascii=False))
            )
        except Exception as exc:
            import logging

            logging.getLogger("workers").error("Failed to store LLM record: %s", exc)
            try:
                await self.store.db.execute(
                    "INSERT OR REPLACE INTO gateway_record_failures(request_id, created_at, error) VALUES(?,?,?)",
                    (entry["request_id"], time.time(), str(exc)[:1000]),
                )
            except Exception:
                pass
            return

    def stub(self, name: str) -> Any:
        return self.env.GATEWAY_STATE.getByName(name)


async def _maybe_await(value: Any) -> Any:
    """兼容 Workers SDK 已转换的 RPC 值与待等待对象。"""
    if hasattr(value, "__await__"):
        return await value
    return value


def _permissions(user: dict[str, Any] | None, key: dict[str, Any] | None) -> list[str] | None:
    if user and key:
        left, right = set(user.get("model_permissions") or ["*"]), set(key.get("model_permissions") or ["*"])
        return ["*"] if "*" in left and "*" in right else sorted(left & right)
    if user:
        return user.get("model_permissions")
    if key:
        return key.get("model_permissions")
    return None


def _upstream_api_endpoint(api: str, source_format: str) -> str:
    """根地址或 `/v1` 基址按上游协议补全标准端点。"""
    from urllib.parse import urlsplit, urlunsplit

    parsed = urlsplit(api.strip())
    normalized_path = parsed.path.rstrip("/")
    if normalized_path not in {"", "/v1"}:
        return api
    suffix = {
        "openai_chat": "/chat/completions",
        "openai_responses": "/responses",
        "claude_chat": "/messages",
    }.get(source_format, "/chat/completions")
    return urlunsplit(
        (parsed.scheme, parsed.netloc, f"{normalized_path or '/v1'}{suffix}", parsed.query, parsed.fragment)
    )


def _cookies(header: str) -> dict[str, str]:
    return dict(part.strip().split("=", 1) for part in header.split(";") if "=" in part)


def _bounded(value: Any, limit: int = 64_000) -> Any:
    """限制记录载荷，避免大提示词或媒体对象耗尽内存。"""
    text = json.dumps(value, ensure_ascii=False)
    return text if len(text) <= limit else {"truncated": True, "prefix": text[:limit]}


def _form(payload: dict[str, Any]) -> str:
    from urllib.parse import urlencode

    return urlencode(payload)


async def _password_hash(password: str, salt: str) -> str:
    import js
    from pyodide.ffi import to_js

    key = await js.crypto.subtle.importKey("raw", to_js(password.encode()), "PBKDF2", False, to_js(["deriveBits"]))
    bits = await js.crypto.subtle.deriveBits(
        {"name": "PBKDF2", "salt": to_js(salt.encode()), "iterations": 100_000, "hash": "SHA-256"},
        key,
        256,
    )
    return bytes(bits.to_bytes()).hex()


async def password_record(username: str, password: str) -> dict[str, str]:
    salt = secrets.token_hex(16)
    return {
        "id": "admin",
        "username": username,
        "password_hash": await _password_hash(password, salt),
        "salt": salt,
    }


class _RuntimeContext:
    def __init__(self, root: Any):
        self.root_path = root
        import logging

        self.logger = logging.getLogger("workers")


def _load_hook(path: Any) -> Any:
    """按部署期 Hook 文件加载，不在运行时写源代码。"""
    root = path.parents[1] if path.parent.name == "hooks" else path.parent
    hooks_root = (root / "hooks").resolve()
    resolved = path.resolve()
    if not resolved.is_relative_to(hooks_root):
        raise HttpError("Hook path must stay under hooks directory", 400, "invalid_hook")
    cache_key = str(resolved)
    cached = _HOOK_CACHE.get(cache_key)
    if cached is not None:
        return cached
    namespace: dict[str, Any] = {}
    source = resolved.read_text(encoding="utf-8")
    exec(compile(source, cache_key, "exec"), namespace)
    hook_class = namespace.get("Hook")
    hook = hook_class() if hook_class else None
    _HOOK_CACHE[cache_key] = hook
    return hook
