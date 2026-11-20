"""同源 Flask 管理 API 与 Vue 页面。

Flask 只承担路由和序列化；业务数据使用 Workers 异步存储。
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any

from flask import Flask, Response, jsonify, request
from gateway_runtime.http import HttpError
from gateway_runtime.services import (
    GatewayServices,
    _maybe_await,
    _permissions,
    _upstream_api_endpoint,
    password_record,
)
from pyodide.ffi import run_sync


class Management:
    """提供 Provider、认证、权限、映射、OAuth 与统计管理接口。"""

    def __init__(self, env: Any, root: Path, ctx: Any):
        self.services = GatewayServices(env, root, ctx)
        self.app = Flask(__name__, static_folder=str(root / "static"), static_url_path="/static")
        self.app.config["JSON_AS_ASCII"] = False
        self._routes()

    def handle(self, req: Any, env: Any) -> Any:
        from workers import wsgi

        return wsgi.fetch(self.app, req, env)

    def _routes(self) -> None:
        self.app.get("/api/health", endpoint="health")(lambda: {"ok": True, "runtime": "python-workers"})
        self.app.get("/api/ui/bootstrap")(self.bootstrap)
        self.app.post("/api/login")(self.login)
        self.app.post("/api/logout")(self.logout)
        self.app.get("/login", endpoint="login_page")(self.page)
        for path in ("/", "/providers", "/users", "/api-keys", "/model-mappings", "/oauth", "/statistics", "/settings"):
            self.app.get(path, endpoint=f"page_{path.strip('/') or 'home'}")(self.page)
        self.app.get("/v1/models", endpoint="models")(lambda: run_sync(self.models()))
        for kind, path in (
            ("providers", "providers"),
            ("auth_groups", "auth-groups"),
            ("users", "users"),
            ("api_keys", "api-keys"),
            ("mappings", "model-mappings"),
            ("oauth", "oauth"),
        ):
            self.register_collection(kind, path)
        self.route("GET", "/api/settings/system", self.settings)
        self.route("PUT", "/api/settings/system", self.save_settings)
        self.route("PUT", "/api/settings/system/<path:section>", self.save_settings)
        self.route("GET", "/api/statistics", self.statistics)
        self.route("GET", "/api/statistics/user-usage-summary", self.statistics)
        self.route("GET", "/api/request-logs", self.logs)
        self.route("GET", "/api/statistics/overview", self.statistics_overview)
        self.route("GET", "/api/statistics/filter-options", self.statistics_filters)
        self.route("GET", "/api/statistics/api-key-usage-summary", self.api_key_usage)
        self.route("GET", "/api/statistics/daily-stats/export", self.export_statistics)
        self.route("POST", "/api/statistics/daily-stats/import", self.import_statistics)
        self.route("GET", "/api/statistics/export", self.export_statistics_excel)
        self.route("PUT", "/api/providers/chat-whitelist", self.save_chat_whitelist)
        self.route("POST", "/api/auth-groups/import-entries", self.import_auth_entries)
        self.route("GET", "/api/auth-groups/<string:identifier>/runtime", self.auth_group_runtime)
        self.route(
            "POST",
            "/api/auth-groups/<string:identifier>/entries/<string:entry_id>/<string:entry_action>",
            self.auth_entry_action,
        )
        self.route("POST", "/api/model-mappings/<string:identifier>/targets/toggle", self.toggle_mapping_target)
        self.route("POST", "/api/oauth/codex/session", self.oauth_session)
        self.route("GET", "/api/oauth/codex/auth-files", self.oauth_files)
        self.route("POST", "/api/oauth/codex/auth-files/import", self.oauth_import)
        self.route("POST", "/api/oauth/codex/auth-files/export", self.oauth_export)
        self.route("GET", "/api/oauth/codex/auth-files/<string:account_id>/quota", self.oauth_quota)
        self.route("GET", "/api/oauth/codex/auth-files/<string:account_id>/reset-cards", self.oauth_reset_cards)
        self.route("POST", "/api/oauth/codex/auth-files/<string:account_id>/enable", self.oauth_file_toggle)
        self.route("POST", "/api/oauth/codex/auth-files/<string:account_id>/disable", self.oauth_file_toggle)
        self.route("DELETE", "/api/oauth/codex/auth-files/<string:account_id>", self.oauth_file_delete)
        self.route("GET", "/api/oauth/codex/<string:kind>", self.oauth_model_catalog)
        self.route("POST", "/api/oauth/codex/<string:kind>", self.oauth_model_catalog)
        self.route("DELETE", "/api/oauth/codex/<string:kind>/<string:model_id>", self.oauth_model_catalog)
        self.route("PUT", "/api/oauth/codex/image-models/default", self.oauth_default_image_model)
        self.route("POST", "/api/oauth/codex/models/restore-builtins", self.oauth_restore_models)
        self.route("POST", "/api/oauth/codex/image-models/restore-builtins", self.oauth_restore_models)
        self.route("GET", "/api/providers/fetch-models", self.fetch_models)
        self.route("POST", "/api/providers/test-models", self.test_models)
        self.route("POST", "/api/oauth/codex/authorize", self.oauth_authorize)
        self.route("POST", "/api/oauth/codex/callback", self.oauth_callback)
        self.route("GET", "/api/oauth/codex/models", self.oauth_model_catalog)
        self.route("GET", "/api/oauth/codex/image-models", self.oauth_model_catalog)
        self.route("GET", "/api/statistics/export", self.export_statistics)
        self.route("GET", "/api/llm-records/<path:identifier>", self.llm_record)
        self.route("POST", "/api/oauth/<account_id>/refresh", self.refresh_oauth)
        self.route("POST", "/api/llm-recording", self.save_recording_setting)
        self.route("GET", "/api/llm-recording/failures", self.record_failures)
        self.app.errorhandler(HttpError)(self.error)
        self.app.errorhandler(ValueError)(self.error)
        self.app.after_request(self.security_headers)

    def route(self, method: str, path: str, callback: Any, kind: str | None = None) -> None:
        suffix = f"_{kind}" if kind else ""
        self.app.add_url_rule(
            path,
            f"{method}_{path}{suffix}_{callback.__name__}",
            lambda **kwargs: self.secure(run_sync_callback(callback, kind, kwargs)),
            methods=[method],
        )

    def register_collection(self, kind: str, path: str) -> None:
        self.route("GET", f"/api/{path}", self.list_kind, kind)
        self.route("POST", f"/api/{path}", self.create_kind, kind)
        self.route("GET", f"/api/{path}/<identifier>", self.get_kind, kind)
        self.route("PUT", f"/api/{path}/<identifier>", self.update_kind, kind)
        self.route("DELETE", f"/api/{path}/<identifier>", self.delete_kind, kind)
        self.route("POST", f"/api/{path}/<identifier>/<action>", self.action_kind, kind)
        self.route("POST", f"/api/{path}/batch", self.batch_kind, kind)
        self.route("POST", f"/api/{path}/import", self.import_kind, kind)
        self.route("POST", f"/api/{path}/export", self.export_kind, kind)
        self.route("PUT", f"/api/{path}/order", self.order_kind, kind)

    def secure(self, callback: Any, *args: Any) -> Any:
        try:
            run_sync(self.services.authenticate(request._get_current_object()))
            return callback
        except HttpError as exc:
            return self.error(exc)
        except Exception as exc:
            return jsonify({"error": str(exc), "code": "management_error"}), 500

    async def list_kind(self, kind: str) -> Any:
        items = await self.services.store.list(kind)
        if kind == "api_keys":
            models = await self.available_models()
            items = [_api_key_view(item, models) for item in items]
            return jsonify(
                {
                    "api_keys": items,
                    "total": len(items),
                    "page": 1,
                    "page_size": len(items) or 50,
                    "total_pages": 1,
                    "available_models": await self.available_models(),
                }
            )
        if kind == "auth_groups":
            items = [_auth_group_view(item) for item in items]
            return jsonify(items)
        if kind == "users":
            models = await self.available_models()
            items = [_user_view(item, models) for item in items]
            return jsonify({"users": items, "total": len(items), "available_models": models})
        if kind == "mappings":
            return jsonify(
                {"enabled": True, "model_mappings": items, "available_targets": await self.available_models()}
            )
        if kind == "oauth":
            items = [_oauth_file_view(item) for item in items]
        return jsonify(items)

    async def get_kind(self, kind: str, identifier: str) -> Any:
        item = await self.services.store.get(kind, identifier)
        if not item:
            return jsonify({"error": "Not found"}), 404
        if kind == "auth_groups":
            item = _auth_group_view(item)
        elif kind == "users":
            item = _user_view(item, await self.available_models())
        elif kind == "api_keys":
            item = _api_key_view(item, await self.available_models())
        elif kind == "oauth":
            item = {
                key: value
                for key, value in item.items()
                if key not in {"access_token", "refresh_token", "client_secret"}
            }
        return jsonify(item)

    async def create_kind(self, kind: str) -> Any:
        body = _body()
        item = await self.save_item(kind, body)
        if kind == "api_keys":
            return jsonify({"api_key": _api_key_view(item, await self.available_models())}), 201
        if kind == "auth_groups":
            item = _auth_group_view(item)
        if kind == "users":
            item = _user_view(item, await self.available_models())
        return jsonify(item), 201

    async def update_kind(self, kind: str, identifier: str) -> Any:
        previous = await self.services.store.get(kind, identifier)
        if not previous:
            return jsonify({"error": "Not found"}), 404
        body = _body()
        if kind == "api_keys":
            merged = {**previous, **body}
            body = _api_key_input(merged)
            body["api_key"] = previous.get("api_key", "")
            body["key_hash"] = previous.get("key_hash", "")
            body["created_at"] = previous.get("created_at")
        if kind == "providers":
            from src.config.provider_config import SUPPORTED_PROVIDER_FIELDS

            previous = {key: value for key, value in previous.items() if key in SUPPORTED_PROVIDER_FIELDS}
        item = await self.save_item(kind, {**previous, **body}, identifier)
        if kind == "auth_groups":
            item = _auth_group_view(item)
        elif kind == "users":
            item = _user_view(item, await self.available_models())
        elif kind == "api_keys":
            item = _api_key_view(item, await self.available_models())
        return jsonify(item)

    async def delete_kind(self, kind: str, identifier: str) -> Any:
        await self.services.store.delete(kind, identifier)
        return jsonify({"message": "Deleted successfully"})

    async def action_kind(self, kind: str, identifier: str, action: str) -> Any:
        if action in {"enable", "disable", "toggle"}:
            item = await self.services.store.get(kind, identifier)
            if not item:
                return jsonify({"error": "Not found"}), 404
            item["enabled"] = action == "enable" or (action == "toggle" and not item.get("enabled", True))
            if kind == "users":
                item["whitelist_access_enabled"] = action == "enable" or (
                    action == "toggle" and not item.get("whitelist_access_enabled", True)
                )
            await self.services.store.put(kind, identifier, item)
            return jsonify(_user_view(item, await self.available_models()) if kind == "users" else item)
        if action == "copy":
            item = await self.services.store.get(kind, identifier)
            if not item:
                return jsonify({"error": "Not found"}), 404
            copied = dict(item)
            if kind == "providers":
                from src.config.provider_config import SUPPORTED_PROVIDER_FIELDS, ProviderConfigSchema

                base = str(item.get("name") or identifier)
                suffix = 1
                names = {provider.get("name") for provider in await self.services.store.list(kind)}
                name = f"{base}_copy"
                while name in names:
                    suffix += 1
                    name = f"{base}_copy{suffix}"
                copied = {key: value for key, value in item.items() if key in SUPPORTED_PROVIDER_FIELDS}
                copied["name"] = name
                copied = dict(ProviderConfigSchema.from_mapping(copied).__dict__)
                identifier = name
            else:
                copied["id"] = _new_id(kind, item)
                identifier = copied["id"]
            await self.services.store.put(kind, identifier, copied)
            return jsonify(copied), 201
        if action in {"refresh", "sync"} and kind == "oauth":
            account = await self.services.store.get("oauth", identifier)
            if not account:
                return jsonify({"error": "Not found"}), 404
            credentials = await self.services.oauth_credentials({**account, "expires_at": 0})
            return jsonify({"message": "OAuth account refreshed", "expires_at": credentials.get("expires_at")})
        return jsonify({"error": f"Unsupported action: {action}"}), 400

    async def batch_kind(self, kind: str) -> Any:
        body = _body()
        action = body.get("action")
        identifiers = body.get("ids") or body.get("names") or body.get("mapping_ids") or body.get("user_ids") or []
        for identifier in identifiers:
            if action == "delete":
                await self.services.store.delete(kind, str(identifier))
            elif action in {"enable", "disable"}:
                item = await self.services.store.get(kind, str(identifier))
                if item:
                    item["enabled"] = action == "enable"
                    await self.services.store.put(kind, str(identifier), item)
            elif action == "set_model_permissions" and kind == "users":
                item = await self.services.store.get(kind, str(identifier))
                if item:
                    item["model_permissions"] = body.get("model_permissions") or ["*"]
                    await self.services.store.put(kind, str(identifier), item)
        return jsonify({"count": len(identifiers)})

    async def import_kind(self, kind: str) -> Any:
        body = _body()
        if kind == "providers":
            groups = body.get("auth_groups") or []
            group_names = []
            for group in groups:
                saved = await self.save_item("auth_groups", group)
                group_names.append(saved["name"])
            items = body.get("providers") or []
            for item in items:
                await self.save_item("providers", item)
            return jsonify(
                {
                    "count": len(items),
                    "auth_groups_count": len(group_names),
                    "auth_entry_runtime_state_count": 0,
                    "auth_entry_usage_buckets_count": 0,
                }
            ), 201
        if kind == "auth_groups":
            items = body.get("auth_groups") or body.get("items") or []
            names = []
            for item in items:
                saved = await self.save_item("auth_groups", item)
                names.append(saved["name"])
            return jsonify({"count": len(names), "names": names}), 201
        if kind == "users":
            items = body.get("users") or []
            created = updated = 0
            existing = await self.services.store.list("users")
            for item in items:
                match = next((row for row in existing if row.get("ip_address") == item.get("ip_address")), None)
                if match:
                    await self.save_item("users", {**match, **item}, str(match["id"]))
                    updated += 1
                else:
                    await self.save_item("users", item)
                    created += 1
            return jsonify({"count": created + updated, "created_count": created, "updated_count": updated}), 201
        items = body.get(kind) or body.get("items") or ([body] if body.get("id") or body.get("name") else [])
        for item in items:
            await self.save_item(kind, item)
        return jsonify({"count": len(items)}), 201

    async def export_kind(self, kind: str) -> Any:
        items = await self.services.store.list(kind)
        identifiers = set(_body().get("ids") or _body().get("names") or [])
        selected = [
            item
            for item in items
            if not identifiers or item.get("id") in identifiers or item.get("name") in identifiers
        ]
        if kind == "providers":
            group_names = {item.get("auth_group") for item in selected if item.get("auth_group")}
            groups = [
                group for group in await self.services.store.list("auth_groups") if group.get("name") in group_names
            ]
            return jsonify({"version": 1, "providers": selected, "auth_groups": groups})
        return jsonify({kind: selected})

    async def order_kind(self, kind: str) -> Any:
        items = await self.services.store.list(kind)
        identifiers = _body().get("ids") or _body().get("names") or []
        order = {identifier: index for index, identifier in enumerate(identifiers)}
        for item in items:
            item["sort_order"] = order.get(item.get("id") or item.get("name"), len(order))
            await self.services.store.put(kind, item.get("id") or item.get("name"), item)
        return jsonify({"count": len(items)})

    async def save_item(self, kind: str, body: dict[str, Any], identifier: str | None = None) -> dict[str, Any]:
        item = dict(body)
        if kind == "providers":
            from src.config.provider_config import ProviderConfigSchema

            item = dict(ProviderConfigSchema.from_mapping(item).__dict__)
        elif kind == "api_keys":
            import hashlib

            item = _api_key_input(item)
            existing = await self.services.store.list("api_keys") if identifier is None else []
            if identifier is None:
                numeric_ids = [int(value["id"]) for value in existing if str(value.get("id", "")).isdigit()]
                item["id"] = item.get("id") or (max(numeric_ids, default=0) + 1)
            item.setdefault("name", f"key-{item['id']}")
            if "api_key" in item:
                item["key_hash"] = hashlib.sha256(item["api_key"].encode()).hexdigest()
            else:
                raw = f"sk-{secrets.token_urlsafe(30)}"
                item["api_key"] = raw
                item["key_hash"] = hashlib.sha256(raw.encode()).hexdigest()
            item.setdefault("enabled", True)
            item["created_at"] = item.get("created_at") or time.time()
        elif kind == "users":
            existing = await self.services.store.list("users") if identifier is None else []
            if identifier is None:
                numeric_ids = [int(value["id"]) for value in existing if str(value.get("id", "")).isdigit()]
                item["id"] = item.get("id") or (max(numeric_ids, default=0) + 1)
            item.setdefault("username", str(item.get("ip_address") or f"user-{item['id']}"))
            item.setdefault("ip_address", "")
            item.setdefault("whitelist_access_enabled", True)
            item.setdefault("model_permissions", ["*"])
            item["created_at"] = item.get("created_at") or time.time()
        elif kind == "auth_groups":
            from src.config.provider_config import AuthGroupSchema

            item = AuthGroupSchema.from_mapping(item).to_mapping()
        elif kind == "oauth":
            item.setdefault("id", secrets.token_hex(8))
            item.setdefault("enabled", True)
            item.setdefault("source_format", "openai_responses")
        else:
            item.setdefault("id", str(item.get("name") or secrets.token_hex(8)))
            item.setdefault("enabled", True)
        identifier = identifier or str(item.get("name") if kind in {"providers", "auth_groups"} else item["id"])
        item["updated_at"] = time.time()
        return await self.services.store.put(kind, identifier, item)

    def bootstrap(self) -> Any:
        async def load() -> Any:
            auth = await self.services.authenticate(request._get_current_object())
            settings = await self.settings_view()
            return jsonify(
                {
                    "version": "workers-1.0.0",
                    "auth_enabled": settings["auth_enabled"],
                    "authenticated": True,
                    "username": auth.get("username", ""),
                    "features": {
                        "oauth": settings["oauth"]["enabled"],
                        "api_keys": settings["api_keys"]["enabled"],
                        "model_mapping": settings["model_mapping"]["enabled"],
                        "whitelist": self.services.setting(await self.services.snapshot(), "whitelist", False),
                    },
                    "capabilities": {"agents": False},
                }
            )

        try:
            return run_sync(load())
        except HttpError:
            return jsonify(
                {
                    "version": "workers-1.0.0",
                    "auth_enabled": True,
                    "authenticated": False,
                    "features": {},
                    "capabilities": {"agents": False},
                }
            )

    def login(self) -> Any:
        try:
            token, username = run_sync(self.services.login(_body()))
            response = jsonify({"message": "Login successful", "username": username})
            response.set_cookie("session_token", token, httponly=True, secure=True, samesite="Lax", max_age=86400)
            return response
        except HttpError as exc:
            return self.error(exc)

    def logout(self) -> Any:
        cookie = request.cookies.get("session_token", "")
        run_sync(self.services.store.delete("sessions", cookie))
        response = jsonify({"message": "Logout successful"})
        response.delete_cookie("session_token")
        return response

    def page(self) -> Any:
        if request.path == "/login":
            template = Path(self.app.static_folder) / "static" / "app" / "index.html"
            return (
                Response(template.read_text(encoding="utf-8"))
                if template.is_file()
                else Response("Assets unavailable", status=503)
            )
        try:
            run_sync(self.services.authenticate(request._get_current_object()))
        except HttpError:
            return Response(
                "<!doctype html><html><body><script>location.href='/login'</script></body></html>", status=401
            )
        template = Path(self.app.static_folder) / "static" / "app" / "index.html"
        if not template.is_file():
            return Response("Frontend assets are unavailable", status=503)
        response = Response(template.read_text(encoding="utf-8"))
        response.headers["Cache-Control"] = "no-store"
        return response

    async def available_models(self) -> list[str]:
        providers = await self.services.store.list("providers")
        oauth = await self.services.store.list("oauth")
        mappings = await self.services.store.list("mappings")
        return sorted(
            {
                *(f"{p['name']}/{model}" for p in providers for model in p.get("model_list", [])),
                *(o["model"] for o in oauth),
                *(m["id"] for m in mappings),
            }
        )

    async def models(self) -> Any:
        snapshot = await self.services.snapshot()
        key = await self.services._api_key(request._get_current_object(), snapshot)
        client_ip = request.headers.get("CF-Connecting-IP") or ""
        user = next((item for item in snapshot["users"] if item.get("ip_address") == client_ip), None)
        if self.services.setting(snapshot, "whitelist", False) and not (
            user and user.get("whitelist_access_enabled", True)
        ):
            raise HttpError("Client IP is not allowed", 403, "whitelist_denied")
        allowed = _permissions(user, key)
        items = []
        for provider in snapshot["providers"]:
            if not provider.get("enabled", True):
                continue
            hidden = set(provider.get("hidden_model_list") or [])
            for model in provider.get("model_list") or []:
                model_id = f"{provider['name']}/{model}"
                if model in hidden or (allowed is not None and "*" not in allowed and model_id not in allowed):
                    continue
                items.append(
                    {
                        "id": model_id,
                        "object": "model",
                        "owned_by": provider["name"],
                        "provider_name": provider["name"],
                        "source_format": provider.get("source_format"),
                        "target_formats": list(provider.get("target_formats") or []),
                        "capabilities": [],
                    }
                )
        if self.services.setting(snapshot, "oauth_enabled", True):
            for account in snapshot["oauth"]:
                if account.get("enabled", True) and (
                    allowed is None or "*" in allowed or account.get("model") in allowed
                ):
                    items.append(
                        {
                            "id": account["model"],
                            "object": "model",
                            "owned_by": "claude"
                            if account.get("source_format") == "claude_chat" or "anthropic" in account.get("api", "")
                            else "codex",
                            "target_formats": ["openai_chat", "openai_responses", "claude_chat"],
                            "capabilities": [],
                        }
                    )
        for mapping in snapshot["mappings"]:
            if mapping.get("enabled", True) and (allowed is None or "*" in allowed or mapping["id"] in allowed):
                items.append(
                    {
                        "id": mapping["id"],
                        "object": "model",
                        "owned_by": "llm-proxy",
                        "target_formats": ["openai_chat", "openai_responses", "claude_chat"],
                        "capabilities": [],
                    }
                )
        return jsonify({"object": "list", "data": items})

    async def settings_view(self) -> dict[str, Any]:
        raw = {item["id"]: item for item in await self.services.store.list("settings")}
        admin = raw.get("admin") or {}
        return {
            "server": _settings_group(raw, "server", {"host": "0.0.0.0", "port": 8080}),
            "admin": {"username": admin.get("username", ""), "password": ""},
            "client_ip": _settings_group(
                raw, "client_ip", {"real_ip_enabled": False, "real_ip_header": "CF-Connecting-IP"}
            ),
            "logging": _settings_group(
                raw, "logging", {"path": "logs", "level": "INFO", "llm_request_debug_enabled": False}
            ),
            "llm_recording": _settings_group(raw, "llm_recording", {"enabled": False, "directory": ""}),
            "oauth": _settings_group(
                raw,
                "oauth",
                {
                    "enabled": True,
                    "proxy_mode": "direct",
                    "proxy": "",
                    "verify_ssl": True,
                    "safe_desensitization_enabled": False,
                },
            ),
            "api_keys": _settings_group(raw, "api_keys", {"enabled": False}),
            "model_mapping": _settings_group(raw, "model_mapping", {"enabled": True}),
            "auth_enabled": bool(admin.get("username") and admin.get("password_hash")),
        }

    async def settings(self) -> Any:
        return jsonify(await self.settings_view())

    async def save_settings(self, section: str | None = None) -> Any:
        body = _body()
        group = (section or "basic").replace("-", "_")
        auth_changed = False
        if group == "basic":
            previous = await self.services.store.get("settings", "admin") or {}
            username = str(
                body.get("admin", {}).get("username") or body.get("username") or previous.get("username", "")
            )
            password = str(body.get("admin", {}).get("password") or body.get("password") or "")
            if not username:
                raise HttpError("Admin username is required", 400, "invalid_settings")
            auth_changed = bool(password) or username != previous.get("username")
            admin = await password_record(username, password) if password else previous
            if not admin.get("password_hash"):
                raise HttpError("Admin password is required", 400, "invalid_settings")
            await self.services.store.put("settings", "admin", admin)
            for key in ("server",):
                if isinstance(body.get(key), dict):
                    await self.services.store.put("settings", key, {"id": key, "value": body[key]})
            if isinstance(body.get("client_ip"), dict):
                await self.services.store.put("settings", "client_ip", {"id": "client_ip", "value": body["client_ip"]})
        elif group in {"client_ip", "debug", "llm_recording", "oauth", "api_keys", "model_mapping"}:
            key = "llm_recording" if group == "llm_recording" else group
            payload = body.get(key) if isinstance(body.get(key), dict) else body
            await self.services.store.put("settings", key, {"id": key, "value": payload})
            flag = {
                "api_keys": "api_keys_enabled",
                "model_mapping": "mapping_enabled",
                "llm_recording": "llm_recording_enabled",
                "oauth": "oauth_enabled",
            }.get(group)
            if flag and "enabled" in payload:
                await self.services.store.put("settings", flag, {"id": flag, "value": bool(payload["enabled"])})
            if group == "debug":
                await self.services.store.put("settings", "logging", {"id": "logging", "value": payload})
        else:
            raise HttpError(f"Unsupported settings group: {group}", 400, "invalid_settings")
        return jsonify(
            {
                "settings": await self.settings_view(),
                "auth_config_changed": auth_changed,
                "server_restart_required": False,
                "server_listener_changed": False,
                "server_access_url": None,
            }
        )

    async def save_chat_whitelist(self) -> Any:
        enabled = bool(_body().get("enabled"))
        await self.services.store.put("settings", "whitelist", {"id": "whitelist", "value": enabled})
        return jsonify({"enabled": enabled})

    async def statistics(self) -> Any:
        stats = await self.services.store.db.all("SELECT * FROM gateway_daily_stats ORDER BY day DESC LIMIT 1000")
        return jsonify(
            [
                {
                    "request_model": row["request_model"],
                    "response_model": row["response_model"],
                    "ip_address": row["client_ip"],
                    "request_count": row["requests"],
                    "prompt_tokens": row["prompt_tokens"],
                    "completion_tokens": row["completion_tokens"],
                    "total_tokens": row["total_tokens"],
                    "last_request_date": row["day"],
                }
                for row in stats
            ]
        )

    async def api_key_usage(self) -> Any:
        rows = await self.services.store.db.all(
            """SELECT api_key_id, COUNT(*) AS request_count, COALESCE(SUM(prompt_tokens),0) AS prompt_tokens,
               COALESCE(SUM(completion_tokens),0) AS completion_tokens,
               COALESCE(SUM(total_tokens),0) AS total_tokens
               FROM gateway_logs WHERE api_key_id IS NOT NULL GROUP BY api_key_id"""
        )
        return jsonify(rows)

    async def statistics_overview(self) -> Any:
        stats = await self.services.store.db.all("SELECT * FROM gateway_daily_stats ORDER BY day")
        logs = await self.services.store.db.all("SELECT * FROM gateway_logs ORDER BY created_at")
        totals = {
            "prompt_tokens": sum(row["prompt_tokens"] for row in stats),
            "completion_tokens": sum(row["completion_tokens"] for row in stats),
            "total_tokens": sum(row["total_tokens"] for row in stats),
            "request_count": sum(row["requests"] for row in stats),
            "success_count": sum(1 for row in logs if row.get("outcome") == "success"),
            "error_count": sum(1 for row in logs if row.get("outcome") not in {"success", None}),
        }
        series: dict[str, dict[str, Any]] = {}
        models: dict[str, dict[str, Any]] = {}
        ips: dict[str, dict[str, Any]] = {}
        for row in stats:
            day = series.setdefault(
                row["day"],
                {"date": row["day"], "request_count": 0, "total_tokens": 0, "prompt_tokens": 0, "completion_tokens": 0},
            )
            day["request_count"] += row["requests"]
            day["prompt_tokens"] += row["prompt_tokens"]
            day["completion_tokens"] += row["completion_tokens"]
            day["total_tokens"] += row["total_tokens"]
            model = models.setdefault(
                row["request_model"] or "-",
                {
                    "request_model": row["request_model"],
                    "request_count": 0,
                    "total_tokens": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                },
            )
            model["request_count"] += row["requests"]
            model["prompt_tokens"] += row["prompt_tokens"]
            model["completion_tokens"] += row["completion_tokens"]
            model["total_tokens"] += row["total_tokens"]
            ip = ips.setdefault(
                row["client_ip"] or "-",
                {"ip_address": row["client_ip"], "request_count": 0, "total_tokens": 0},
            )
            ip["request_count"] += row["requests"]
            ip["total_tokens"] += row["total_tokens"]
        return jsonify(
            {
                "totals": totals,
                "series": list(series.values()),
                "models": list(models.values()),
                "model_flows": list(models.values()),
                "response_models": list(models.values()),
                "ips": list(ips.values()),
            }
        )

    async def statistics_filters(self) -> Any:
        logs = await self.services.store.db.all(
            "SELECT DISTINCT client_ip, request_model, response_model FROM gateway_logs"
        )
        return jsonify(
            {
                "users": [],
                "models": sorted({row["request_model"] for row in logs if row.get("request_model")}),
                "response_models": sorted({row["response_model"] for row in logs if row.get("response_model")}),
                "ip_addresses": sorted({row["client_ip"] for row in logs if row.get("client_ip")}),
            }
        )

    async def import_statistics(self) -> Any:
        body = _body()
        logs = body.get("request_logs") or []
        daily = body.get("daily_request_stats") or []
        inserted = skipped = 0
        for row in logs:
            existing = await self.services.store.db.one(
                "SELECT request_id FROM gateway_logs WHERE request_id = ?", (str(row.get("request_id") or ""),)
            )
            if existing:
                skipped += 1
                continue
            await self.services.store.record(
                {
                    **row,
                    "request_id": str(row.get("request_id") or secrets.token_hex(16)),
                    "created_at": float(row.get("created_at") or time.time()),
                    "client_ip": row.get("client_ip") or row.get("ip_address"),
                    "outcome": row.get("outcome") or "success",
                }
            )
            inserted += 1
        merged = 0
        for row in daily:
            await self.services.store.db.execute(
                """INSERT INTO gateway_daily_stats(day,client_ip,request_model,response_model,requests,
                   prompt_tokens,completion_tokens,total_tokens) VALUES(?,?,?,?,?,?,?,?)
                   ON CONFLICT(day,client_ip,request_model,response_model) DO UPDATE SET
                   requests=requests+excluded.requests,prompt_tokens=prompt_tokens+excluded.prompt_tokens,
                   completion_tokens=completion_tokens+excluded.completion_tokens,
                   total_tokens=total_tokens+excluded.total_tokens""",
                (
                    row.get("day") or row.get("stat_date"),
                    row.get("client_ip") or row.get("ip_address"),
                    row.get("request_model"),
                    row.get("response_model"),
                    int(row.get("requests") or 0),
                    int(row.get("prompt_tokens") or 0),
                    int(row.get("completion_tokens") or 0),
                    int(row.get("total_tokens") or 0),
                ),
            )
            merged += 1
        return jsonify(
            {
                "request_logs_inserted_count": inserted,
                "request_logs_skipped_count": skipped,
                "daily_request_stats_inserted_count": merged,
                "daily_request_stats_merged_count": merged,
            }
        )

    async def export_statistics_excel(self) -> Any:
        import io
        import zipfile

        rows = await self.services.store.db.all("SELECT * FROM gateway_logs ORDER BY created_at DESC LIMIT 5000")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", _XLSX_CONTENT_TYPES)
            archive.writestr("_rels/.rels", _XLSX_RELS)
            archive.writestr("xl/workbook.xml", _XLSX_WORKBOOK)
            archive.writestr("xl/_rels/workbook.xml.rels", _XLSX_WORKBOOK_RELS)
            archive.writestr("xl/worksheets/sheet1.xml", _xml_sheet(rows))
        return Response(
            buffer.getvalue(),
            headers={
                "Content-Type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "Content-Disposition": 'attachment; filename="statistics.xlsx"',
            },
        )

    async def logs(self) -> Any:
        rows = await self.services.store.db.all("SELECT * FROM gateway_logs ORDER BY created_at DESC LIMIT 500")
        items = [
            {
                **row,
                "ip_address": row["client_ip"],
                "http_status": row["status"],
                "request_count": 1,
                "start_time": _iso_time(row["created_at"]),
                "outcome": row.get("outcome"),
            }
            for row in rows
        ]
        return jsonify({"logs": items, "request_logs": items, "total": len(items)})

    async def fetch_models(self) -> Any:
        provider = _provider_from_request()
        provider = await self._with_auth_headers(provider)
        result = await _fetch_upstream(provider, self.services.env)
        return jsonify(result)

    async def test_models(self) -> Any:
        body = _body()
        provider = await self._with_auth_headers(_provider_from_request(body))
        models = body.get("models") or provider.get("model_list") or []
        results = []
        for model in models:
            result = await _test_upstream_model(provider, model, self.services.env)
            results.append(result)
        return jsonify({"results": results})

    async def _with_auth_headers(self, provider: dict[str, Any]) -> dict[str, Any]:
        group_name = provider.get("auth_group")
        entry_id = provider.get("auth_entry_id")
        if not group_name or not entry_id:
            return provider
        group = await self.services.store.get("auth_groups", group_name)
        entry = next((item for item in (group or {}).get("entries") or [] if item.get("id") == entry_id), None)
        return {**provider, "auth_headers": (entry or {}).get("headers") or {}}

    async def oauth_authorize(self) -> Any:
        body = _body()
        state = secrets.token_urlsafe(24)
        verifier = secrets.token_urlsafe(64)
        import base64
        import hashlib

        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        from urllib.parse import urlencode

        params = {
            "client_id": body.get("client_id", ""),
            "redirect_uri": body.get("redirect_uri", ""),
            "response_type": "code",
            "scope": body.get("scope", "openid"),
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
        }
        await self.services.store.put(
            "oauth_states", state, {"expires": time.time() + 600, "body": body, "verifier": verifier}
        )
        return jsonify(
            {
                "state": state,
                "authorization_url": f"{body['authorization_url']}?{urlencode(params)}",
                "code_verifier": verifier,
            }
        )

    async def oauth_callback(self) -> Any:
        body = _body()
        callback_url = str(body.get("callback_url") or "")
        if callback_url:
            from urllib.parse import parse_qs, urlsplit

            query = parse_qs(urlsplit(callback_url).query)
            body = {
                "state": (query.get("state") or [""])[0],
                "code": (query.get("code") or [""])[0],
            }
        state = body.get("state", "")
        saved = await self.services.store.get("oauth_states", state)
        if not saved or saved["expires"] < time.time():
            raise HttpError("OAuth state expired", 400, "oauth_state_expired")
        oauth = saved["body"]
        from urllib.parse import urlencode

        from workers import fetch

        token_body = {
            "grant_type": "authorization_code",
            "code": body.get("code", ""),
            "redirect_uri": oauth.get("redirect_uri", ""),
            "client_id": oauth.get("client_id", ""),
            "code_verifier": saved["verifier"],
        }
        if oauth.get("client_secret"):
            token_body["client_secret"] = oauth["client_secret"]
        response = await fetch(
            oauth["token_url"],
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            body=urlencode(token_body),
        )
        tokens = await response.json()
        if response.status != 200 or not tokens.get("access_token"):
            raise HttpError("OAuth token exchange failed", 502, "oauth_token_exchange_failed")
        claims = _jwt_claims(tokens.get("id_token"))
        account = {
            **oauth,
            **tokens,
            "id": claims.get("sub") or secrets.token_hex(8),
            "email": claims.get("email", ""),
            "plan_type": (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type", ""),
            "expires_at": time.time() + int(tokens.get("expires_in", 3600)),
            "enabled": True,
        }
        await self.services.stub(f"oauth:{account['id']}").save_oauth(account)
        await self.services.store.put("oauth", account["id"], account)
        await self.services.store.delete("oauth_states", state)
        return jsonify({"auth_file": _oauth_file_view(account)})

    async def oauth_models(self) -> Any:
        return jsonify({"models": await self.services.store.list("oauth")})

    async def export_statistics(self) -> Any:
        logs = await self.services.store.db.all("SELECT * FROM gateway_logs ORDER BY created_at DESC LIMIT 5000")
        daily = await self.services.store.db.all("SELECT * FROM gateway_daily_stats ORDER BY day DESC LIMIT 1000")
        return jsonify({"request_logs": logs, "daily_request_stats": daily})

    async def llm_record(self, identifier: str) -> Any:
        item = await self.services.env.RECORDS.get(f"llm/{identifier}.json")
        if item is None:
            return jsonify({"error": "Record not found"}), 404
        return Response(await item.text(), content_type="application/json")

    async def refresh_oauth(self, account_id: str) -> Any:
        account = await self.services.store.get("oauth", account_id)
        if not account:
            return jsonify({"error": "OAuth account not found"}), 404
        credentials = await self.services.oauth_credentials({**account, "expires_at": 0})
        return jsonify({"id": account_id, "expires_at": credentials.get("expires_at")})

    async def save_recording_setting(self) -> Any:
        body = _body()
        item = {"id": "llm_recording_enabled", "value": bool(body.get("enabled"))}
        await self.services.store.put("settings", item["id"], item)
        return jsonify(item)

    async def record_failures(self) -> Any:
        rows = await self.services.store.db.all(
            "SELECT * FROM gateway_record_failures ORDER BY created_at DESC LIMIT 100"
        )
        return jsonify({"failures": rows})

    async def auth_group_runtime(self, identifier: str) -> Any:
        group = await self.services.store.get("auth_groups", identifier)
        if not group:
            return jsonify({"error": "Auth group not found"}), 404
        entries = []
        for entry in group.get("entries") or []:
            entries.append(
                {
                    **{key: value for key, value in entry.items() if key != "headers"},
                    "status": "available" if entry.get("enabled", True) else "disabled",
                    "disabled": not entry.get("enabled", True),
                    "disabled_reason": None,
                    "inflight": 0,
                    "cooldown_until": None,
                    "minute_request_count": 0,
                    "day_request_count": 0,
                    "minute_total_tokens": 0,
                    "day_total_tokens": 0,
                    "last_status_code": None,
                    "last_error_type": None,
                    "last_error_message": None,
                    "updated_at": None,
                }
            )
        return jsonify(
            {
                "name": group["name"],
                "summary": _auth_group_view(group)["summary"],
                "entries": entries,
            }
        )

    async def auth_entry_action(self, identifier: str, entry_id: str, entry_action: str) -> Any:
        group = await self.services.store.get("auth_groups", identifier)
        if not group:
            return jsonify({"error": "Auth group not found"}), 404
        entries = group.get("entries") or []
        entry = next((item for item in entries if item.get("id") == entry_id), None)
        if not entry:
            return jsonify({"error": "Auth entry not found"}), 404
        if entry_action in {"enable", "disable"}:
            entry["enabled"] = entry_action == "enable"
        elif entry_action not in {"clear-cooldown", "reset-minute-usage", "reset", "restore"}:
            raise HttpError(f"Unsupported auth entry action: {entry_action}", 400, "invalid_action")
        await self.services.store.put("auth_groups", identifier, group)
        return jsonify({"message": "Auth entry updated"})

    async def import_auth_entries(self) -> Any:
        try:
            import yaml

            payload = yaml.safe_load(_body().get("yaml") or "[]")
        except Exception as exc:
            raise HttpError(f"Invalid YAML: {exc}", 400, "invalid_yaml") from exc
        if isinstance(payload, dict):
            payload = payload.get("entries") or []
        return jsonify({"entries": payload})

    async def toggle_mapping_target(self, identifier: str) -> Any:
        mapping = await self.services.store.get("mappings", identifier)
        if not mapping:
            return jsonify({"error": "Model mapping not found"}), 404
        body = _body()
        model_id = str(body.get("model_id") or "")
        target = next((item for item in mapping.get("targets") or [] if item.get("model_id") == model_id), None)
        if not target:
            return jsonify({"error": "Target not found"}), 404
        target["enabled"] = bool(body.get("enabled"))
        await self.services.store.put("mappings", identifier, mapping)
        return jsonify(mapping)

    async def oauth_session(self) -> Any:
        from urllib.parse import urlencode

        constants = _codex_constants()
        verifier = secrets.token_urlsafe(64)
        state = secrets.token_urlsafe(24)
        import base64
        import hashlib

        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        body = {
            "authorization_url": constants["authorize_url"],
            "token_url": constants["token_url"],
            "client_id": constants["client_id"],
            "redirect_uri": constants["redirect_uri"],
            "scope": constants["scope"],
            "api": "https://chatgpt.com/backend-api/codex/responses",
            "source_format": "openai_responses",
            "enabled": True,
        }
        await self.services.store.put(
            "oauth_states", state, {"expires": time.time() + 600, "body": body, "verifier": verifier}
        )
        params = {
            "client_id": body["client_id"],
            "redirect_uri": body["redirect_uri"],
            "response_type": "code",
            "scope": body["scope"],
            "state": state,
            "code_challenge": challenge,
            "code_challenge_method": "S256",
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
        }
        return jsonify(
            {
                "authorization_url": f"{body['authorization_url']}?{urlencode(params)}",
                "state": state,
                "expires_at": _iso_time(time.time() + 600),
            }
        )

    async def oauth_files(self) -> Any:
        accounts = await self.services.store.list("oauth")
        return jsonify(
            {
                "files": [_oauth_file_view(account) for account in accounts],
                "total": len(accounts),
                "auth_dir": "D1",
            }
        )

    async def oauth_import(self) -> Any:
        form = await request._get_current_object().form_data()
        files = [value for value in form.values() if hasattr(value, "filename")]
        if not files:
            raise HttpError("No OAuth files were uploaded", 400, "empty_oauth_import")
        imported, failed = [], []
        for upload in files:
            try:
                raw = await upload.bytes()
                payloads = _oauth_payloads(upload.filename, raw)
                for name, payload in payloads:
                    if not payload.get("access_token"):
                        raise ValueError("missing access_token")
                    claims = _jwt_claims(payload.get("id_token"))
                    account_id = claims.get("sub") or payload.get("account_id") or name
                    account = {
                        **payload,
                        "id": account_id,
                        "name": name,
                        "email": claims.get("email", ""),
                        "plan_type": (claims.get("https://api.openai.com/auth") or {}).get("chatgpt_plan_type", ""),
                        "expires_at": time.time() + int(payload.get("expires_in", 3600)),
                        "enabled": True,
                    }
                    await self.services.store.put("oauth", account_id, account)
                    await _maybe_await(self.services.stub(f"oauth:{account_id}").save_oauth(account))
                    imported.append(name)
            except Exception as exc:
                failed.append({"name": getattr(upload, "filename", "unknown"), "reason": str(exc)})
        return jsonify(
            {
                "imported": len(imported),
                "failed": len(failed),
                "total": len(imported) + len(failed),
                "imported_files": imported,
                "failed_files": failed,
            }
        )

    async def oauth_export(self) -> Any:
        import io
        import zipfile

        body = _body()
        names = set(body.get("names") or [])
        accounts = await self.services.store.list("oauth")
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for account in accounts:
                if names and account.get("id") not in names and account.get("name") not in names:
                    continue
                payload = {
                    key: value
                    for key, value in account.items()
                    if key not in {"id", "name", "email", "plan_type", "quota", "quota_refreshed_at", "updated_at"}
                }
                archive.writestr(f"{account.get('id') or 'oauth'}.json", json.dumps(payload, ensure_ascii=False))
        return Response(
            buffer.getvalue(),
            status=200,
            headers={
                "Content-Type": "application/zip",
                "Content-Disposition": 'attachment; filename="codex-auth-files.zip"',
            },
        )

    async def oauth_model_catalog(self, kind: str | None = None, model_id: str | None = None) -> Any:
        kind = kind or ("image-models" if "/image-models" in request.path else "models")
        key = "oauth_image_models" if kind == "image-models" else "oauth_models"
        stored = await self.services.store.get("settings", key) or {}
        models = list(stored.get("value") or [])
        if request.method == "POST":
            candidate = str(_body().get("model_id") or "").strip()
            if candidate and candidate not in models:
                models.append(candidate)
                await self.services.store.put("settings", key, {"id": key, "value": models})
        elif request.method == "DELETE" and model_id:
            models = [model for model in models if model != model_id]
            await self.services.store.put("settings", key, {"id": key, "value": models})
        return jsonify(
            {
                "models": [{"id": model, "built_in": False} for model in models],
                "default_model": stored.get("default") if isinstance(stored, dict) else None,
            }
        )

    async def oauth_default_image_model(self) -> Any:
        model_id = str(_body().get("model_id") or "")
        current = await self.services.store.get("settings", "oauth_image_models") or {"value": []}
        await self.services.store.put(
            "settings",
            "oauth_image_models",
            {
                "id": "oauth_image_models",
                "value": current.get("value") or [],
                "default": model_id,
            },
        )
        return jsonify(
            {
                "models": [{"id": model, "built_in": False} for model in current.get("value") or []],
                "default_model": model_id,
            }
        )

    async def oauth_quota(self, account_id: str) -> Any:
        account = await self.services.store.get("oauth", account_id)
        if not account:
            return jsonify({"error": "OAuth account not found"}), 404
        quota = account.get("quota") or {"windows": [], "refreshed_at": _iso_time(time.time())}
        return jsonify(quota)

    async def oauth_reset_cards(self, account_id: str) -> Any:
        return jsonify({"reset_cards": [], "refreshed_at": _iso_time(time.time())})

    async def oauth_file_toggle(self, account_id: str) -> Any:
        account = await self.services.store.get("oauth", account_id)
        if not account:
            return jsonify({"error": "OAuth account not found"}), 404
        account["enabled"] = request.path.endswith("/enable")
        await self.services.store.put("oauth", account_id, account)
        return jsonify({"auth_file": _oauth_file_view(account)})

    async def oauth_file_delete(self, account_id: str) -> Any:
        await self.services.store.delete("oauth", account_id)
        return jsonify({"message": "OAuth account deleted"})

    async def oauth_restore_models(self) -> Any:
        from src.config.openai_model_catalog import (
            DEFAULT_CODEX_IMAGE_MODEL_IDS,
            DEFAULT_CODEX_MODEL_IDS,
        )

        image = "/image-models/" in request.path
        models = list(DEFAULT_CODEX_IMAGE_MODEL_IDS if image else DEFAULT_CODEX_MODEL_IDS)
        key = "oauth_image_models" if image else "oauth_models"
        await self.services.store.put("settings", key, {"id": key, "value": models})
        return jsonify(
            {
                "models": [{"id": model, "built_in": True} for model in models],
                "default_model": models[0] if image and models else None,
            }
        )

    @staticmethod
    def error(exc: Exception) -> Any:
        status = exc.status if isinstance(exc, HttpError) else 400
        code = exc.code if isinstance(exc, HttpError) else "bad_request"
        return jsonify({"error": str(exc), "code": code}), status

    @staticmethod
    def security_headers(response: Any) -> Any:
        if request.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        if request.path.startswith("/v1/"):
            response.headers["Access-Control-Allow-Origin"] = "*"
        if request.path.startswith("/static/app/assets/"):
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return response


async def request_json(req: Any) -> dict[str, Any]:
    return await req.json() if req.method in {"POST", "PUT", "PATCH"} else {}


def run_sync_callback(callback: Any, kind: str | None, kwargs: dict[str, Any]) -> Any:
    args = ([kind] if kind else []) + list(kwargs.values())
    result = callback(*args)
    return run_sync(result) if hasattr(result, "__await__") else result


def _body() -> dict[str, Any]:
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise HttpError("Request body must be a JSON object", 400, "invalid_json")
    return body


_XLSX_CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/xl/workbook.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
    '<Override PartName="/xl/worksheets/sheet1.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
    "</Types>"
)
_XLSX_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="xl/workbook.xml"/></Relationships>'
)
_XLSX_WORKBOOK = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheets>'
    '<sheet name="Statistics" sheetId="1" r:id="rId1" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>'
    "</sheets></workbook>"
)
_XLSX_WORKBOOK_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" '
    'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
    'Target="worksheets/sheet1.xml"/></Relationships>'
)


def _xml_sheet(rows: list[dict[str, Any]]) -> str:
    from xml.sax.saxutils import escape

    def column(index: int) -> str:
        if index < 26:
            return chr(65 + index)
        return chr(64 + index // 26) + chr(65 + index % 26)

    headers = sorted({key for row in rows for key in row})
    xml_rows = []
    for row_index, row in enumerate([{key: key for key in headers}, *rows], start=1):
        cells = []
        for col_index, key in enumerate(headers):
            value = row.get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                cells.append(f'<c r="{column(col_index)}{row_index}"><v>{value}</v></c>')
            else:
                text = escape("" if value is None else str(value))
                cells.append(f'<c r="{column(col_index)}{row_index}" t="inlineStr"><is><t>{text}</t></is></c>')
        xml_rows.append(f'<row r="{row_index}">{"".join(cells)}</row>')
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>'
        + "".join(xml_rows)
        + "</sheetData></worksheet>"
    )


def _api_key_view(item: dict[str, Any], models: list[str]) -> dict[str, Any]:
    """将内部 Key 记录转换为前端公开字段。"""
    key = str(item.get("api_key") or "")
    permissions = item.get("model_permissions") or ["*"]
    return {
        **item,
        "key_prefix": key[:7],
        "key_suffix": key[-4:],
        "model_permissions": permissions,
        "allowed_models_count": len(models) if permissions == ["*"] else len(permissions),
        "available_models_count": len(models),
        "token_limit_k": int(item["token_limit"]) // 1000 if item.get("token_limit") else None,
        "token_limit_remaining": None,
        "max_concurrency": (item.get("limits") or {}).get("concurrency"),
        "request_quota_per_minute": (item.get("limits") or {}).get("per_minute"),
        "request_quota_per_day": (item.get("limits") or {}).get("per_day"),
        "created_at": _iso_time(item.get("created_at")),
        "last_used_at": item.get("last_used_at"),
    }


def _user_view(item: dict[str, Any], models: list[str]) -> dict[str, Any]:
    permissions = item.get("model_permissions") or ["*"]
    return {
        **item,
        "allowed_models_count": len(models) if permissions == ["*"] else len(permissions),
        "available_models_count": len(models),
        "created_at": _iso_time(item.get("created_at")),
        "updated_at": _iso_time(item.get("updated_at")),
    }


def _auth_group_view(item: dict[str, Any]) -> dict[str, Any]:
    entries = item.get("entries") or []
    enabled = [entry for entry in entries if entry.get("enabled", True)]
    return {
        **item,
        "cooldown_seconds_on_429": item.get("cooldown_seconds_on_429"),
        "summary": {
            "entry_count": len(entries),
            "available_count": len(enabled),
            "cooldown_count": 0,
            "disabled_count": len(entries) - len(enabled),
        },
    }


def _oauth_payloads(filename: str, raw: bytes) -> list[tuple[str, dict[str, Any]]]:
    """解析单个 JSON 或 ZIP OAuth 上传。"""
    import io
    import zipfile

    if filename.lower().endswith(".zip"):
        results = []
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            for member in archive.namelist():
                if member.endswith(".json"):
                    payload = json.loads(archive.read(member))
                    if isinstance(payload, dict):
                        results.append((member.rsplit("/", 1)[-1], payload))
        return results
    payload = json.loads(raw)
    if not isinstance(payload, dict):
        raise ValueError("OAuth file must contain a JSON object")
    return [(filename.rsplit("/", 1)[-1].removesuffix(".json"), payload)]


def _iso_time(value: Any) -> str | None:
    if not isinstance(value, (int, float)):
        return None
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(value))


def _codex_constants() -> dict[str, str]:
    return {
        "authorize_url": "https://auth.openai.com/oauth/authorize",
        "token_url": "https://auth.openai.com/oauth/token",
        "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
        "redirect_uri": "http://localhost:1455/auth/callback",
        "scope": "openid email profile offline_access",
    }


def _jwt_claims(token: Any) -> dict[str, Any]:
    if not isinstance(token, str) or "." not in token:
        return {}
    import base64

    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        claims = json.loads(base64.urlsafe_b64decode(payload))
        return claims if isinstance(claims, dict) else {}
    except Exception:
        return {}


def _oauth_file_view(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": account.get("id", ""),
        "path": account.get("id", ""),
        "email": account.get("email", ""),
        "plan_type": account.get("plan_type", ""),
        "enabled": bool(account.get("enabled", True)),
        "disabled": not account.get("enabled", True),
        "availability_status": "available" if account.get("enabled", True) else "disabled",
        "availability_status_message": "",
        "availability_retry_at": None,
        "usage_status_message": "",
        "expired": _iso_time(account.get("expires_at")),
        "quota": account.get("quota"),
        "quota_error": "",
        "quota_refreshed_at": _iso_time(account.get("quota_refreshed_at")),
        "reset_cards": [],
        "reset_cards_refreshed_at": "",
        "current_usage": None,
        "previous_usage": None,
    }


def _settings_group(flat: dict[str, Any], key: str, default: dict[str, Any]) -> dict[str, Any]:
    value = flat.get(key, {}).get("value") if isinstance(flat.get(key), dict) else None
    return {**default, **(value if isinstance(value, dict) else {})}


def _api_key_input(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "enabled": bool(item.get("enabled", True)),
        "model_permissions": item.get("model_permissions") or ["*"],
        "token_limit": int(item["token_limit_k"] or 0) * 1000 if item.get("token_limit_k") else 0,
        "limits": {
            "concurrency": int(item.get("max_concurrency") or 0),
            "per_minute": int(item.get("request_quota_per_minute") or 0),
            "per_day": int(item.get("request_quota_per_day") or 0),
        },
        **({"api_key": item["api_key"]} if item.get("api_key") else {}),
    }


def _new_id(kind: str, item: dict[str, Any]) -> str:
    return f"{item.get('id') or item.get('name')}-copy-{secrets.token_hex(3)}"


def _provider_from_request(body: dict[str, Any] | None = None) -> dict[str, Any]:
    body = body or dict(request.args)
    if isinstance(body.get("provider"), dict):
        return body["provider"]
    return body


async def _test_upstream_model(provider: dict[str, Any], model: str, env: Any) -> dict[str, Any]:
    """使用实际 HTTP 请求测试模型，而不是只检查配置。"""
    started = time.time()
    source = provider.get("source_format", "openai_chat")
    if source == "openai_responses":
        body = {"model": model, "input": "Hello", "max_output_tokens": 8, "stream": False}
    elif source == "claude_chat":
        body = {"model": model, "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 8, "stream": False}
    else:
        body = {"model": model, "messages": [{"role": "user", "content": "Hello"}], "max_tokens": 8, "stream": False}
    headers = {"Content-Type": "application/json"}
    auth_entry = _find_auth_entry(provider)
    if auth_entry:
        headers.update(auth_entry.get("headers") or {})
    elif provider.get("api_key"):
        headers["Authorization"] = f"Bearer {provider['api_key']}"
    try:
        from gateway_runtime.http import open_upstream

        endpoint = _upstream_api_endpoint(str(provider.get("api") or ""), source)
        response = await open_upstream(endpoint, headers, body, env)
        latency = int((time.time() - started) * 1000)
        if response.status >= 400:
            detail = (await response.text()).strip()[:300]
            return {
                "requested_model": model,
                "available": False,
                "first_token_latency_ms": None,
                "tps": None,
                "response_model": None,
                "error": f"HTTP {response.status}{f': {detail}' if detail else ''}",
            }
        payload = await response.json()
        response_model = payload.get("model") if isinstance(payload, dict) else None
        return {
            "requested_model": model,
            "available": True,
            "first_token_latency_ms": latency,
            "tps": None,
            "response_model": response_model,
            "error": None,
        }
    except Exception as exc:
        return {
            "requested_model": model,
            "available": False,
            "first_token_latency_ms": None,
            "tps": None,
            "response_model": None,
            "error": str(exc),
        }


def _find_auth_entry(provider: dict[str, Any]) -> dict[str, Any] | None:
    group = provider.get("auth_group")
    entry_id = provider.get("auth_entry_id")
    if not group or not entry_id:
        return None
    return {"id": entry_id, "headers": provider.get("auth_headers") or {}}


async def _fetch_upstream(provider: dict[str, Any], env: Any) -> dict[str, Any]:
    from gateway_runtime.http import get_upstream

    api = str(provider.get("api") or "").strip()
    candidates = _model_endpoint_candidates(api)
    headers = {"Accept": "application/json"}
    entry = _find_auth_entry(provider)
    if entry:
        headers.update(entry.get("headers") or {})
    elif provider.get("api_key"):
        headers["Authorization"] = f"Bearer {provider['api_key']}"
    errors: list[str] = []
    for url in candidates:
        try:
            response = await get_upstream(url, headers, env)
            if response.status >= 400:
                detail = (await response.text()).strip()[:200]
                errors.append(f"{url} returned HTTP {response.status}{f': {detail}' if detail else ''}")
                continue
            raw = await response.text()
            try:
                payload = json.loads(raw)
            except (TypeError, json.JSONDecodeError):
                errors.append(f"{url} returned non-JSON content")
                continue
            models = _extract_model_ids(payload)
            if models:
                return {"fetched_models": models, "fetched_count": len(models)}
            errors.append(f"{url} returned no models")
        except Exception as exc:
            errors.append(f"{url} request failed: {exc}")
    raise HttpError(
        "Failed to fetch models from provider. " + "; ".join(errors or ["Provider API is invalid"]),
        502,
        "model_discovery_failed",
    )


def _model_endpoint_candidates(api: str) -> list[str]:
    """按现有后端契约构造模型发现候选 URL。"""
    from urllib.parse import urlparse

    cleaned = api.strip().rstrip("/")
    parsed = urlparse(cleaned)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc:
        raise HttpError("Provider api must use http:// or https://", 400, "invalid_provider_api")
    known_suffixes = (
        "/v1/chat/completions",
        "/chat/completions",
        "/v1/completions",
        "/completions",
        "/v1/responses",
        "/responses",
        "/v1/messages",
        "/messages",
        "/v1/models",
        "/models",
        "/v1",
    )
    lower_path = parsed.path.rstrip("/").lower()
    base_path = parsed.path.rstrip("/")
    for suffix in known_suffixes:
        if lower_path.endswith(suffix):
            base_path = parsed.path.rstrip("/")[: -len(suffix)].rstrip("/")
            break
    root = f"{parsed.scheme.lower()}://{parsed.netloc}"
    return [f"{root}{base_path}/v1/models", f"{root}{base_path}/models"]


def _extract_model_ids(payload: Any) -> list[str]:
    """兼容 OpenAI、Anthropic 和常见网关的模型列表结构。"""
    items: Any
    if isinstance(payload, dict):
        items = payload.get("data")
        if items is None:
            items = payload.get("models")
        if items is None:
            items = payload.get("model_list")
    elif isinstance(payload, list):
        items = payload
    else:
        return []
    if not isinstance(items, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = item.get("id") or item.get("name") if isinstance(item, dict) else item
        model = str(value or "").strip()
        if model and model not in seen:
            seen.add(model)
            result.append(model)
    return result
