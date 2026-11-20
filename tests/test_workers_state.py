"""共享状态的核心单元测试，不依赖 Cloudflare 凭据。"""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path

sys.modules["workers"] = types.SimpleNamespace(DurableObject=type("DurableObject", (), {}))

_spec = importlib.util.spec_from_file_location("workers.state", Path(__file__).parents[1] / "workers" / "state.py")
_module = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_module)
GatewayState = _module.GatewayState


class _Rows:
    def __init__(self, rows):
        self.rows = rows

    def toArray(self):
        return self.rows


class _Sql:
    def __init__(self):
        self.values = {}

    def exec(self, sql, *params):
        if sql.startswith("CREATE TABLE"):
            return _Rows([])
        if sql.startswith("SELECT"):
            if not params:
                key = "oauth" if "'oauth'" in sql else "api_key"
            else:
                key = params[0]
            value = self.values.get(key)
            return _Rows([type("Row", (), {"value": value})()] if value else [])
        if sql.startswith("UPDATE"):
            self.values["api_key"] = params[0]
            return _Rows([])
        if "VALUES('api_key',?)" in sql or "VALUES('oauth',?)" in sql:
            key = "api_key" if "VALUES('api_key',?)" in sql else "oauth"
            self.values[key] = params[0]
            return _Rows([])
        key, value = params[-2:]
        self.values[key] = value
        return _Rows([])


class StateTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = object.__new__(GatewayState)
        self.state.sql = _Sql()

    async def test_key_acquire_and_finish_are_idempotent(self):
        first = await self.state.acquire_key("r1", {"limit": 1, "minute_limit": 2, "now": 100})
        self.assertTrue(first["ok"])
        duplicate = await self.state.acquire_key("r1", {"limit": 1, "now": 100})
        self.assertTrue(duplicate["duplicate"])
        rejected = await self.state.acquire_key("r2", {"limit": 1, "now": 100})
        self.assertEqual(rejected["reason"], "concurrency_limit")
        self.assertFalse((await self.state.finish_key("r1", 7))["duplicate"])
        self.assertTrue((await self.state.finish_key("r1", 7))["duplicate"])
        self.assertEqual((await self.state.key_snapshot())["tokens"], 7)

    async def test_auth_group_unlimited_entries_are_eligible(self):
        result = await self.state.acquire_auth([{"id": "entry-1", "headers": {"authorization": "Bearer test"}}])
        self.assertEqual(result["entry"]["id"], "entry-1")
        await self.state.finish_auth("entry-1", {"status": 200})

    async def test_mapping_keeps_all_targets_for_failover(self):
        targets = [{"model_id": "a", "priority": 10}, {"model_id": "b", "priority": 1}]
        selected = await self.state.select_mapping(targets)
        self.assertEqual(selected["target"]["model_id"], "a")
        await self.state.fail_mapping("a", {"status": 500, "retry_after": 60})
        selected = await self.state.select_mapping(targets)
        self.assertEqual(selected["target"]["model_id"], "b")

    async def test_oauth_refresh_lease(self):
        first = await self.state.oauth_refresh("account", now=100)
        second = await self.state.oauth_refresh("account", now=101)
        self.assertTrue(first["ok"])
        self.assertFalse(second["ok"])
        third = await self.state.oauth_refresh("account", now=150)
        self.assertTrue(third["ok"])


if __name__ == "__main__":
    unittest.main()
