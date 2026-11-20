"""上游执行器契约与按需 HTTP 实现。"""

from typing import TYPE_CHECKING

from .contracts import Executor, OpenedUpstreamResponse

if TYPE_CHECKING:
    from .registry import ExecutorRegistry, HttpExecutor, build_default_executor_registry

__all__ = [
    "Executor",
    "ExecutorRegistry",
    "HttpExecutor",
    "OpenedUpstreamResponse",
    "build_default_executor_registry",
]


def __getattr__(name: str):
    if name in {"ExecutorRegistry", "HttpExecutor", "build_default_executor_registry"}:
        from . import registry

        return getattr(registry, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
