"""D1 异步访问与事务封装。

普通 Flask/gevent 仓储继续使用 sqlite3；本模块只服务 Workers。
"""

from __future__ import annotations

import json
import time
from typing import Any


def value(row: Any, key: str, default: Any = None) -> Any:
    """兼容 Workers SDK 返回的属性行与字典行。"""
    if isinstance(row, dict):
        return row.get(key, default)
    return getattr(row, key, default)


class D1:
    """将 D1 binding 显式映射为 Python 查询接口。"""

    def __init__(self, binding: Any):
        self.binding = binding

    async def execute(self, sql: str, params: tuple[Any, ...] = ()) -> Any:
        statement = self.binding.prepare(sql)
        if params:
            statement = statement.bind(*params)
        return await statement.run()

    async def all(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        result = await self.execute(sql, params)
        rows = getattr(result, "results", None) or []
        return [{key: value(row, key) for key in row} if not isinstance(row, dict) else row for row in rows]

    async def one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = await self.all(f"{sql} LIMIT 1", params)
        return rows[0] if rows else None

    async def batch(self, statements: list[tuple[str, tuple[Any, ...]]]) -> None:
        """在一个 D1 事务中提交相关写操作。"""
        prepared = []
        for sql, params in statements:
            statement = self.binding.prepare(sql)
            if params:
                statement = statement.bind(*params)
            prepared.append(statement)
        if prepared:
            await self.binding.batch(prepared)


class StateStore:
    """管理面配置、认证和观测数据的持久化接口。"""

    def __init__(self, db: D1):
        self.db = db
        self._ready = False

    async def initialize(self) -> None:
        if self._ready:
            return
        await self.db.batch(
            [
                (
                    """CREATE TABLE IF NOT EXISTS gateway_config (
                        kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1, updated_at REAL NOT NULL,
                        PRIMARY KEY (kind, id)
                    )""",
                    (),
                ),
                (
                    """CREATE TABLE IF NOT EXISTS gateway_logs (
                        request_id TEXT PRIMARY KEY, created_at REAL NOT NULL,
                        client_ip TEXT, api_key_id TEXT, request_model TEXT,
                        response_model TEXT, provider TEXT, outcome TEXT,
                        prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
                        total_tokens INTEGER DEFAULT 0, status INTEGER, error TEXT,
                        duration_ms INTEGER DEFAULT 0, payload TEXT
                    )""",
                    (),
                ),
                (
                    """CREATE TABLE IF NOT EXISTS gateway_daily_stats (
                        day TEXT NOT NULL, client_ip TEXT, request_model TEXT,
                        response_model TEXT, requests INTEGER DEFAULT 0,
                        prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
                        total_tokens INTEGER DEFAULT 0,
                        PRIMARY KEY(day, client_ip, request_model, response_model)
                    )""",
                    (),
                ),
                (
                    "CREATE INDEX IF NOT EXISTS idx_gateway_logs_created ON gateway_logs(created_at DESC)",
                    (),
                ),
                (
                    """CREATE TABLE IF NOT EXISTS gateway_record_failures (
                        request_id TEXT PRIMARY KEY, created_at REAL NOT NULL, error TEXT NOT NULL
                    )""",
                    (),
                ),
            ]
        )
        self._ready = True

    async def list(self, kind: str) -> list[dict[str, Any]]:
        await self.initialize()
        rows = await self.db.all("SELECT payload FROM gateway_config WHERE kind = ? ORDER BY id", (kind,))
        return [json.loads(row["payload"]) for row in rows]

    async def get(self, kind: str, identifier: str) -> dict[str, Any] | None:
        await self.initialize()
        row = await self.db.one("SELECT payload FROM gateway_config WHERE kind = ? AND id = ?", (kind, identifier))
        return json.loads(row["payload"]) if row else None

    async def put(self, kind: str, identifier: str, payload: dict[str, Any]) -> dict[str, Any]:
        await self.initialize()
        await self.db.execute(
            """INSERT INTO gateway_config(kind, id, payload, version, updated_at) VALUES(?, ?, ?, 1, ?)
               ON CONFLICT(kind, id) DO UPDATE SET payload=excluded.payload,
               version=gateway_config.version+1, updated_at=excluded.updated_at""",
            (kind, identifier, json.dumps(payload, ensure_ascii=False), time.time()),
        )
        return payload

    async def delete(self, kind: str, identifier: str) -> None:
        await self.db.execute("DELETE FROM gateway_config WHERE kind = ? AND id = ?", (kind, identifier))

    async def record(self, entry: dict[str, Any]) -> None:
        """同一事务保存请求日志与日聚合；失败只影响观测，不改客户端响应。"""
        await self.initialize()
        day = time.strftime("%Y-%m-%d", time.gmtime(entry["created_at"]))
        await self.db.batch(
            [
                (
                    """INSERT OR REPLACE INTO gateway_logs(
                        request_id,created_at,client_ip,api_key_id,request_model,response_model,provider,outcome,
                        prompt_tokens,completion_tokens,total_tokens,status,error,duration_ms,payload
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    tuple(
                        entry.get(key)
                        for key in (
                            "request_id",
                            "created_at",
                            "client_ip",
                            "api_key_id",
                            "request_model",
                            "response_model",
                            "provider",
                            "outcome",
                            "prompt_tokens",
                            "completion_tokens",
                            "total_tokens",
                            "status",
                            "error",
                            "duration_ms",
                        )
                    )
                    + (json.dumps(entry, ensure_ascii=False),),
                ),
                (
                    """INSERT INTO gateway_daily_stats(day,client_ip,request_model,response_model,requests,
                        prompt_tokens,completion_tokens,total_tokens) VALUES(?,?,?,?,1,?,?,?)
                        ON CONFLICT(day,client_ip,request_model,response_model) DO UPDATE SET
                        requests=requests+1,prompt_tokens=prompt_tokens+excluded.prompt_tokens,
                        completion_tokens=completion_tokens+excluded.completion_tokens,
                        total_tokens=total_tokens+excluded.total_tokens""",
                    (
                        day,
                        entry.get("client_ip"),
                        entry.get("request_model"),
                        entry.get("response_model"),
                        entry.get("prompt_tokens", 0),
                        entry.get("completion_tokens", 0),
                        entry.get("total_tokens", 0),
                    ),
                ),
            ]
        )
