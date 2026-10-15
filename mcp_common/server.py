"""MCP application factory for Sewa Setu servers.

Configures:
- AuthSettings (issuer_url, resource_server_url, required_scopes, validate_token_resource)
- PortalTokenVerifier (RS256 verification against internal JWKS)
- Stateless streamable HTTP with DNS rebinding protection disabled for reverse proxying
- /healthz endpoint (publicly accessible, bypassing token auth)
- RFC 9728 protected-resource metadata route
- EnsureWWWAuthenticateScopeMiddleware (adds scope="<role>" to 401/403 WWW-Authenticate)
- Structured request logging (request id, tool name, masked subject, latency, outcome)
"""

import json
import logging
import os
import re
import time
import uuid
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.settings import AuthSettings
from mcp.server.mcpserver.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from .jwt_verifier import PortalTokenVerifier, current_subject

logger = logging.getLogger("mcp_common.server")


def mask_subject(sub: str | None) -> str:
    """Mask subject for safe logging: citizen:1234567890 -> citizen:******7890."""
    if not sub:
        return "anonymous"
    m = re.match(r"^(citizen:)(\d{6})(\d{4})$", sub)
    if m:
        return f"{m.group(1)}******{m.group(3)}"
    return sub


class EnsureWWWAuthenticateScopeMiddleware:
    """Ensure that 401 and 403 responses advertise scope="<role>" in WWW-Authenticate.

    This ensures OAuth clients (including Claude Code and browser clients) know to request
    scope=officer or scope=citizen rather than falling back to the portal's default.
    """

    def __init__(self, app: ASGIApp, role: str):
        self.app = app
        self.role = role

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status = message.get("status", 200)
                if status in (401, 403):
                    headers = list(message.get("headers", []))
                    has_auth_header = False
                    scope_clause = f'scope="{self.role}"'

                    for i, (k, v) in enumerate(headers):
                        if k.lower() == b"www-authenticate":
                            has_auth_header = True
                            val_str = v.decode("utf-8", errors="ignore")
                            if f'scope="{self.role}"' not in val_str:
                                new_val = f"{val_str}, {scope_clause}"
                                headers[i] = (k, new_val.encode("utf-8"))
                            break

                    if not has_auth_header:
                        err_val = f'Bearer error="invalid_token", error_description="Authentication required", {scope_clause}'
                        headers.append((b"www-authenticate", err_val.encode("utf-8")))

                    message = dict(message)
                    message["headers"] = headers

            await send(message)

        await self.app(scope, receive, send_wrapper)


class StructuredLoggingMiddleware:
    """Structured request logging capturing request ID, tool name, masked subject, latency, outcome."""

    def __init__(self, app: ASGIApp, server_name: str, role: str):
        self.app = app
        self.server_name = server_name
        self.role = role

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 1. Request ID
        req_id = None
        for k, v in scope.get("headers", []):
            if k.lower() == b"x-request-id":
                req_id = v.decode("utf-8", errors="ignore")
                break
        if not req_id:
            req_id = uuid.uuid4().hex[:12]

        t0 = time.time()
        method = scope.get("method", "UNKNOWN")
        path = scope.get("path", "")
        tool_name = None

        # 2. Extract tool name from JSON POST body if tools/call
        receive_to_use = receive
        if method == "POST":
            buffered_messages: list[dict[str, Any]] = []
            more_body = True
            body_bytes = b""

            while more_body:
                msg = await receive()
                buffered_messages.append(msg)
                more_body = msg.get("more_body", False)
                body_bytes += msg.get("body", b"")
                # Don't buffer huge files indefinitely
                if len(body_bytes) > 65536:
                    break

            if b"tools/call" in body_bytes or b'"name"' in body_bytes:
                try:
                    payload = json.loads(body_bytes.decode("utf-8", errors="ignore"))
                    if payload.get("method") == "tools/call":
                        tool_name = payload.get("params", {}).get("name")
                except Exception:
                    pass

            idx = 0
            async def replay_receive() -> dict[str, Any]:
                nonlocal idx
                if idx < len(buffered_messages):
                    m = buffered_messages[idx]
                    idx += 1
                    return m
                return await receive()

            receive_to_use = replay_receive

        status_code = [200]

        async def send_wrapper(message: dict[str, Any]) -> None:
            if message["type"] == "http.response.start":
                status_code[0] = message.get("status", 200)
                # Inject X-Request-ID into response
                headers = list(message.get("headers", []))
                headers.append((b"x-request-id", req_id.encode("utf-8")))
                message = dict(message)
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive_to_use, send_wrapper)
        finally:
            latency_ms = (time.time() - t0) * 1000
            sub = current_subject()
            masked_sub = mask_subject(sub)
            outcome = "ok" if status_code[0] < 400 else "error"

            logger.info(
                "[REQ] server=%s role=%s request_id=%s method=%s path=%s "
                "tool=%s subject=%s status=%d latency_ms=%.1f outcome=%s",
                self.server_name,
                self.role,
                req_id,
                method,
                path,
                tool_name or "-",
                masked_sub,
                status_code[0],
                latency_ms,
                outcome,
            )


class ServerAppWrapper:
    """Wrapper that combines the MCPServer instance and Starlette ASGI application."""

    def __init__(
        self,
        mcp_server: MCPServer,
        app: Starlette,
        token_verifier: PortalTokenVerifier,
        canonical_url: str,
        role: str,
    ):
        self.mcp_server = mcp_server
        self.app = app
        self.token_verifier = token_verifier
        self.canonical_url = canonical_url
        self.role = role

        # Forward tool registration decorators
        self.tool = mcp_server.tool
        self.prompt = mcp_server.prompt
        self.resource = mcp_server.resource
        self.add_tool = mcp_server.add_tool

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


def create_server(
    name: str,
    canonical_url: str,
    role: str,
    instructions: str,
    public_base_url: str | None = None,
    jwks_url: str | None = None,
    internal_api_url: str | None = None,
    internal_api_key: str | None = None,
    token_verifier: PortalTokenVerifier | None = None,
    jwks_client: Any | None = None,
) -> ServerAppWrapper:
    """Create and configure a production-ready MCP streamable HTTP server application.

    Args:
        name: Server name (e.g. 'sewasetu-citizen-mcp')
        canonical_url: Canonical audience/resource URL (e.g. 'https://HOST/citizen/mcp')
        role: 'citizen' or 'officer'
        instructions: Instructions provided to AI clients during initialize handshake
        public_base_url: The portal's OAuth issuer URL (defaults to env PUBLIC_BASE_URL)
        jwks_url: The portal's internal JWKS URL (defaults to http://app:8000/.well-known/jwks.json)
        internal_api_url: The portal's internal API URL (defaults to http://app:8000/internal/api/v1)
        internal_api_key: Internal API key (defaults to env INTERNAL_API_KEY)
        token_verifier: Optional custom PortalTokenVerifier instance (e.g. for testing)
        jwks_client: Optional httpx.AsyncClient for JWKS/session fetching (e.g. for testing)

    Returns:
        ServerAppWrapper (ASGI application that also exposes .tool, .mcp_server, etc.)
    """
    issuer_url = (
        public_base_url or os.environ.get("PUBLIC_BASE_URL", "http://localhost:8000")
    ).rstrip("/")

    # Path on which the streamable HTTP endpoint listens
    parsed_path = urlparse(canonical_url).path
    if not parsed_path:
        parsed_path = f"/{role}/mcp"

    auth_settings = AuthSettings(
        issuer_url=issuer_url,
        resource_server_url=canonical_url,
        required_scopes=[role],
        validate_token_resource=True,
    )

    verifier = token_verifier or PortalTokenVerifier(
        public_base_url=issuer_url,
        canonical_url=canonical_url,
        role=role,
        jwks_url=jwks_url or os.environ.get("INTERNAL_JWKS_URL", "http://app:8000/.well-known/jwks.json"),
        internal_api_url=internal_api_url or os.environ.get("INTERNAL_API_URL", "http://app:8000/internal/api/v1"),
        internal_api_key=internal_api_key or os.environ.get("INTERNAL_API_KEY", ""),
        jwks_client=jwks_client,
    )

    mcp_server = MCPServer(
        name=name,
        instructions=instructions,
        token_verifier=verifier,
        auth=auth_settings,
    )

    # Healthcheck endpoint (bypasses Bearer auth)
    @mcp_server.custom_route("/healthz", methods=["GET"])
    async def healthz(request: Any) -> JSONResponse:
        return JSONResponse({
            "status": "ok",
            "server": name,
            "role": role,
            "canonical_url": canonical_url,
        })

    # Build the Starlette streamable HTTP app
    base_app = mcp_server.streamable_http_app(
        streamable_http_path=parsed_path,
        stateless_http=True,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    # Wrap with middlewares
    auth_scope_app = EnsureWWWAuthenticateScopeMiddleware(base_app, role=role)
    logging_app = StructuredLoggingMiddleware(auth_scope_app, server_name=name, role=role)

    # Starlette ASGI app with lifespan support from base_app
    app_wrapper = ServerAppWrapper(
        mcp_server=mcp_server,
        app=logging_app,
        token_verifier=verifier,
        canonical_url=canonical_url,
        role=role,
    )

    return app_wrapper
