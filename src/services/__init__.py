"""服务层按需导出，避免轻量协议工具加载整个服务进程。"""

from importlib import import_module
from typing import TYPE_CHECKING

_EXPORTS = {
    "ApiKeyService": "api_key_service",
    "AuthGroupService": "auth_group_service",
    "AuthenticationService": "authentication_service",
    "ClaudeOAuthService": "claude_oauth_service",
    "ClaudeProxyService": "claude_proxy_service",
    "CodexOAuthAuthenticationError": "codex_oauth_service",
    "CodexOAuthService": "codex_oauth_service",
    "CodexProxyService": "codex_proxy_service",
    "LLMRecordingService": "llm_recording",
    "LogService": "log_service",
    "ModelCatalogService": "model_catalog_service",
    "ModelDiscoveryService": "model_discovery_service",
    "ModelMappingService": "model_mapping_service",
    "ProviderModelTestService": "provider_model_test_service",
    "ProviderService": "provider_service",
    "ProxyService": "proxy_service",
    "SettingsService": "settings_service",
    "UserService": "user_service",
}

__all__ = list(_EXPORTS)

if TYPE_CHECKING:
    from .api_key_service import ApiKeyService as ApiKeyService
    from .auth_group_service import AuthGroupService as AuthGroupService
    from .authentication_service import AuthenticationService as AuthenticationService
    from .claude_oauth_service import ClaudeOAuthService as ClaudeOAuthService
    from .claude_proxy_service import ClaudeProxyService as ClaudeProxyService
    from .codex_oauth_service import CodexOAuthAuthenticationError as CodexOAuthAuthenticationError
    from .codex_oauth_service import CodexOAuthService as CodexOAuthService
    from .codex_proxy_service import CodexProxyService as CodexProxyService
    from .llm_recording import LLMRecordingService as LLMRecordingService
    from .log_service import LogService as LogService
    from .model_catalog_service import ModelCatalogService as ModelCatalogService
    from .model_discovery_service import ModelDiscoveryService as ModelDiscoveryService
    from .model_mapping_service import ModelMappingService as ModelMappingService
    from .provider_model_test_service import ProviderModelTestService as ProviderModelTestService
    from .provider_service import ProviderService as ProviderService
    from .proxy_service import ProxyService as ProxyService
    from .settings_service import SettingsService as SettingsService
    from .user_service import UserService as UserService


def __getattr__(name: str):
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    return getattr(import_module(f".{_EXPORTS[name]}", __name__), name)
