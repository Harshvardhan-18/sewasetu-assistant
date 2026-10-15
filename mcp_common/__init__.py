"""Shared package for Sewa Setu MCP servers."""

from .client import InternalApiClient
from .envelope import envelope, render_text_summary
from .jwt_verifier import (
    PortalTokenVerifier,
    current_access_token,
    current_subject,
)
from .server import (
    EnsureWWWAuthenticateScopeMiddleware,
    ServerAppWrapper,
    StructuredLoggingMiddleware,
    create_server,
    mask_subject,
)

__all__ = [
    "PortalTokenVerifier",
    "current_access_token",
    "current_subject",
    "InternalApiClient",
    "envelope",
    "render_text_summary",
    "create_server",
    "ServerAppWrapper",
    "EnsureWWWAuthenticateScopeMiddleware",
    "StructuredLoggingMiddleware",
    "mask_subject",
]
