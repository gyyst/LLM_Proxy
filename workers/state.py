"""Durable Object 中的强一致共享网关状态。

对象按 API Key、Auth Group、模型映射和 OAuth 账号分片，不使用全局单例。
"""

from __future__ import annotations

import json
import time
from typing import Any

from workers import DurableObject


class GatewayState(DurableObject):
    """使用每个 DO 私有的 SQLite 事务协调共享运行状态。"""

    def __init__(self, ctx: Any, env: Any):
        super().__init__(ctx, env)
        self.sql = ctx.storage.sql
        self.sql.exec("CREATE TABLE IF NOT EXISTS gateway_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)")

    async def acquire_key(self, request_id: str, options: dict[str, Any] | None = None) -> dict[str, Any]:
        """原子预占并发与窗口配额，重试同一请求不会重复占用。"""
        options = options or {}
        limit = int(options.get("limit") or 0)
        minute_limit = int(options.get("minute_limit") or 0)
        daily_limit = int(options.get("daily_limit") or 0)
        token_limit = int(options.get("token_limit") or 0)
        now = options.get("now")
        current = now or time.time()
        minute = int(current // 60)
        day = int(current // 86400)
        cursor = self.sql.exec("SELECT value FROM gateway_state WHERE key = 'api_key'")
        rows = cursor.toArray()
        state = json.loads(rows[0].value) if rows else {"concurrent": 0, "tokens": 0, "requests": {}}
        state.setdefault("concurrent", 0)
        state.setdefault("tokens", 0)
        state.setdefault("requests", {})
        state["requests"] = {key: value for key, value in state["requests"].items() if value > current - 86400}
        active = {key: value for key, value in state["requests"].items() if value > current - 1300}
        state["concurrent"] = len(active)
        if request_id in state["requests"]:
            return {"ok": True, "duplicate": True}
        if limit and state["concurrent"] >= limit:
            return {"ok": False, "reason": "concurrency_limit"}
        if minute_limit and sum(ts >= minute * 60 for ts in state["requests"].values()) >= minute_limit:
            return {"ok": False, "reason": "minute_limit"}
        if daily_limit and sum(ts >= day * 86400 for ts in state["requests"].values()) >= daily_limit:
            return {"ok": False, "reason": "daily_limit"}
        if token_limit and state["tokens"] >= token_limit:
            return {"ok": False, "reason": "token_limit"}
        state["requests"][request_id] = current
        state["concurrent"] += 1
        self.sql.exec(
            "INSERT INTO gateway_state(key,value) VALUES('api_key',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            json.dumps(state),
        )
        return {"ok": True, "duplicate": False}

    async def finish_key(self, request_id: str, tokens: int = 0) -> dict[str, Any]:
        """幂等释放名额并结算 token。"""
        rows = self.sql.exec("SELECT value FROM gateway_state WHERE key = 'api_key'").toArray()
        if not rows:
            return {"ok": True, "duplicate": False}
        state = json.loads(rows[0].value)
        state.setdefault("requests", {})
        if request_id not in state["requests"]:
            return {"ok": True, "duplicate": True}
        del state["requests"][request_id]
        state["concurrent"] = max(0, state.get("concurrent", 0) - 1)
        state["tokens"] = state.get("tokens", 0) + max(0, int(tokens))
        self.sql.exec("UPDATE gateway_state SET value = ? WHERE key = 'api_key'", json.dumps(state))
        return {"ok": True, "duplicate": False, "tokens": state["tokens"]}

    async def key_snapshot(self) -> dict[str, Any]:
        rows = self.sql.exec("SELECT value FROM gateway_state WHERE key = 'api_key'").toArray()
        return json.loads(rows[0].value) if rows else {"concurrent": 0, "tokens": 0, "requests": {}}

    async def save_oauth(self, account: dict[str, Any]) -> dict[str, Any]:
        """保存账号状态；刷新调用必须携带版本，避免并发覆盖 token。"""
        previous = self.sql.exec("SELECT value FROM gateway_state WHERE key = 'oauth'").toArray()
        current = json.loads(previous[0].value) if previous else {}
        expected = account.pop("expected_version", None)
        if expected is not None and current.get("version", 0) != expected:
            raise ValueError("OAuth account changed concurrently")
        current.update(account)
        current["version"] = current.get("version", 0) + 1
        current["updated_at"] = time.time()
        self.sql.exec(
            "INSERT INTO gateway_state(key,value) VALUES('oauth',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            json.dumps(current),
        )
        return current

    async def oauth_snapshot(self) -> dict[str, Any] | None:
        rows = self.sql.exec("SELECT value FROM gateway_state WHERE key = 'oauth'").toArray()
        return json.loads(rows[0].value) if rows else None

    async def oauth_refresh(self, account_id: str, now: float | None = None) -> dict[str, Any]:
        """以租约确保同账号只有一个有效刷新任务。"""
        state = await self.oauth_snapshot() or {"id": account_id, "tokens": {}, "enabled": True}
        current = now or time.time()
        if state.get("refresh_until", 0) > current:
            return {"ok": False, "retry_after": state["refresh_until"] - current}
        state["refresh_until"] = current + 45
        self.sql.exec(
            "INSERT INTO gateway_state(key,value) VALUES('oauth',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            json.dumps(state),
        )
        return {"ok": True, "account": state}

    async def select_mapping(
        self, targets: list[dict[str, Any]], options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """保存跨请求粘滞目标并排除仍在冷却的目标。"""
        strategy = (options or {}).get("strategy", "highest_priority")
        state = await self._state("mapping", {"current": None, "cooldowns": {}})
        now = time.time()
        candidates = [target for target in targets if target.get("enabled", True)]
        candidates.sort(key=lambda target: int(target.get("priority", 0)), reverse=True)
        normal = [target for target in candidates if state["cooldowns"].get(target["model_id"], 0) <= now]
        if strategy == "sticky_failover" and state.get("current") in {t["model_id"] for t in normal}:
            selected = next(t for t in normal if t["model_id"] == state["current"])
        else:
            selected = (normal or candidates)[0] if (normal or candidates) else None
        if selected:
            state["current"] = selected["model_id"]
        await self._save("mapping", state)
        return {"target": selected, "fallback": not normal}

    async def fail_mapping(self, model_id: str, options: dict[str, Any] | None = None) -> None:
        options = options or {}
        status = int(options.get("status") or 0)
        retry_after = int(options.get("retry_after") or 60)
        state = await self._state("mapping", {"current": None, "cooldowns": {}})
        if status in {301, 302, 307, 308, 404, 405, 410}:
            state["cooldowns"][model_id] = 32503680000
        elif status == 429 or status in {401, 403, 408, 425} or status >= 500 or status == 0:
            state["cooldowns"][model_id] = time.time() + max(1, retry_after)
        if state.get("current") == model_id:
            state["current"] = None
        await self._save("mapping", state)

    async def acquire_auth(
        self, entries: list[dict[str, Any]], options: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """原子选择认证项并登记 inflight 租约。"""
        strategy = (options or {}).get("strategy", "least_inflight")
        state = await self._state("auth", {"inflight": {}, "cursor": 0, "current": None, "cooldowns": {}})
        now = time.time()
        state["inflight"] = {key: value for key, value in state["inflight"].items() if value["expires"] > now}
        available = [entry for entry in entries if entry.get("enabled", True)]
        normal = [entry for entry in available if state["cooldowns"].get(entry["id"], 0) <= now]
        candidates = normal or available
        if not candidates:
            return {"entry": None}
        eligible = [
            entry
            for entry in candidates
            if len(state["inflight"].get(entry["id"], [])) < entry.get("max_concurrency", 0)
            or not entry.get("max_concurrency", 0)
        ]
        if not eligible:
            return {"entry": None, "reason": "concurrency_limit"}
        if strategy == "sticky_failover" and state["current"] in {entry["id"] for entry in eligible}:
            selected = next(entry for entry in eligible if entry["id"] == state["current"])
        else:
            eligible.sort(key=lambda entry: (len(state["inflight"].get(entry["id"], [])), entries.index(entry)))
            selected = eligible[0]
        state["current"] = selected["id"]
        state["inflight"].setdefault(selected["id"], []).append({"expires": now + 1300})
        await self._save("auth", state)
        return {"entry": selected}

    async def finish_auth(self, entry_id: str, options: dict[str, Any] | None = None) -> None:
        options = options or {}
        status = int(options.get("status") or 0)
        retry_after = int(options.get("retry_after") or 60)
        state = await self._state("auth", {"inflight": {}, "cursor": 0, "current": None, "cooldowns": {}})
        inflight = state["inflight"].get(entry_id, [])
        if inflight:
            inflight.pop()
        if status == 429 or status in {401, 403, 408, 425} or status >= 500 or status == 0:
            state["cooldowns"][entry_id] = time.time() + max(1, retry_after)
        await self._save("auth", state)

    async def _state(self, key: str, default: dict[str, Any]) -> dict[str, Any]:
        rows = self.sql.exec("SELECT value FROM gateway_state WHERE key = ?", key).toArray()
        return json.loads(rows[0].value) if rows else default

    async def _save(self, key: str, state: dict[str, Any]) -> None:
        self.sql.exec(
            "INSERT INTO gateway_state(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            key,
            json.dumps(state),
        )
