"""Workers 入口、DO 导出与静态资源边界。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from app import WorkersApplication
from state import GatewayState

from workers import Response, WorkerEntrypoint


class GatewayDurableObject(GatewayState):
    """按业务实体保存共享运行状态。"""


_application: WorkersApplication | None = None


class Default(WorkerEntrypoint):
    """处理同源管理 API、页面和数据面请求。"""

    async def fetch(self, request: Any) -> Any:
        global _application
        logging.getLogger("workers").setLevel(logging.INFO)
        if _application is None:
            _application = WorkersApplication(self.env, Path(__file__).resolve().parent, self.ctx)
        return await _application.handle(request)


async def serve_asset(env: Any, request: Any) -> Any:
    """只从显式 assets binding 获取公开前端文件。"""
    assets = getattr(env, "ASSETS", None)
    if assets is None:
        return Response("Static assets are not configured", status=503)
    return await assets.fetch(request)
