"""Internal API client for MCP servers calling the Sewa Setu portal.

Communicates with http://app:8000/internal/api/v1 carrying:
- X-Internal-Key: INTERNAL_API_KEY
- X-Acting-Subject: verified subject from current token (e.g. citizen:<mobile> or officer:<user>)
- X-Request-ID: forwarded or generated request ID

Enforces:
- 5.0 s timeout
- Idempotent GET retry (once only on network failure / 5xx)
- Safe transport failure mapping to a friendly user envelope
- No stack trace leakage
"""

import logging
import os
import uuid
from typing import Any

import httpx

from .jwt_verifier import current_subject

logger = logging.getLogger("mcp_common.client")

FRIENDLY_UNAVAILABLE_ENVELOPE = {
    "ok": False,
    "code": "service_unavailable",
    "message_for_user": "The pension portal is not responding right now; please try again in a minute.",
    "data": {},
    "problems": [],
    "next_step": "Please try again in a minute.",
}


class InternalApiClient:
    """Async client for calling the portal's private /internal/api/v1 endpoints."""

    def __init__(
        self,
        base_url: str | None = None,
        internal_api_key: str | None = None,
        timeout: float = 5.0,
        client: httpx.AsyncClient | None = None,
    ):
        self.base_url = (
            base_url
            or os.environ.get("INTERNAL_API_URL", "http://app:8000/internal/api/v1")
        ).rstrip("/")
        self.internal_api_key = (
            internal_api_key
            or os.environ.get("INTERNAL_API_KEY", "dev-internal-key-change-in-prod")
        )
        self.timeout = timeout
        self._custom_client = client

    async def _get_client(self) -> httpx.AsyncClient:
        if self._custom_client is not None:
            return self._custom_client
        return httpx.AsyncClient(timeout=self.timeout)

    async def request(
        self,
        method: str,
        path: str,
        subject: str | None = None,
        params: dict[str, Any] | None = None,
        json_data: dict[str, Any] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """Send an authenticated request to the internal API with retry on GET."""
        method = method.upper()
        acting_subject = subject or current_subject() or ""
        req_id = request_id or uuid.uuid4().hex[:12]

        clean_path = path.lstrip("/")
        url = f"{self.base_url}/{clean_path}"

        headers = {
            "X-Internal-Key": self.internal_api_key,
            "X-Acting-Subject": acting_subject,
            "X-Request-ID": req_id,
            "Accept": "application/json",
        }

        # Idempotent GET retry once
        max_attempts = 2 if method == "GET" else 1

        for attempt in range(1, max_attempts + 1):
            client = await self._get_client()
            should_close = (self._custom_client is None)
            try:
                resp = await client.request(
                    method=method,
                    url=url,
                    headers=headers,
                    params=params,
                    json=json_data,
                )

                # If server error on GET and we have retries left, retry once
                if resp.status_code >= 500 and method == "GET" and attempt < max_attempts:
                    logger.warning(
                        "Internal API GET %s returned %s on attempt %d; retrying once",
                        url, resp.status_code, attempt
                    )
                    continue

                try:
                    data = resp.json()
                    if isinstance(data, dict) and "ok" in data:
                        return data
                    return {
                        "ok": resp.status_code < 400,
                        "code": "ok" if resp.status_code < 400 else "error",
                        "message_for_user": str(data),
                        "data": data if isinstance(data, dict) else {"response": data},
                        "problems": [],
                        "next_step": "",
                    }
                except Exception:
                    return {
                        "ok": resp.status_code < 400,
                        "code": f"http_{resp.status_code}",
                        "message_for_user": resp.text[:200],
                        "data": {},
                        "problems": [],
                        "next_step": "",
                    }

            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if method == "GET" and attempt < max_attempts:
                    logger.warning(
                        "Internal API transport error on GET %s (attempt %d): %s; retrying once",
                        url, attempt, exc
                    )
                    continue

                logger.error("Internal API transport failure calling %s %s: %s", method, url, exc)
                # Friendly fallback envelope, zero stack traces leaked
                res = dict(FRIENDLY_UNAVAILABLE_ENVELOPE)
                res["request_id"] = req_id
                return res

            except Exception as exc:
                logger.error("Unexpected error calling internal API %s %s: %s", method, url, exc)
                res = dict(FRIENDLY_UNAVAILABLE_ENVELOPE)
                res["request_id"] = req_id
                return res

            finally:
                if should_close:
                    await client.aclose()

        res = dict(FRIENDLY_UNAVAILABLE_ENVELOPE)
        res["request_id"] = req_id
        return res

    async def get(self, path: str, **kwargs) -> dict[str, Any]:
        return await self.request("GET", path, **kwargs)

    async def post(self, path: str, **kwargs) -> dict[str, Any]:
        return await self.request("POST", path, **kwargs)

    async def put(self, path: str, **kwargs) -> dict[str, Any]:
        return await self.request("PUT", path, **kwargs)
