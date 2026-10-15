"""Unit tests for InternalApiClient (Section 3).

Tests:
- Sends X-Internal-Key, X-Acting-Subject, X-Request-ID
- Retries idempotent GET once on transport error / 5xx
- Does NOT retry non-idempotent POST or PUT
- Maps transport failure to friendly envelope without stack trace leakage
- Forwards custom request_id
"""

import httpx
import pytest

from mcp_common.client import InternalApiClient


@pytest.mark.anyio
async def test_successful_get_and_post_headers():
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers.update(dict(request.headers))
        if request.method == "GET":
            return httpx.Response(200, json={"ok": True, "data": {"scheme": "old_age"}})
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True, "data": {"created": True}})
        return httpx.Response(404)

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = InternalApiClient(
        base_url="http://test-portal/internal/api/v1",
        internal_api_key="secret-key-123",
        client=mock_client,
    )

    # 1. GET
    res_get = await api.get("/scheme", subject="citizen:9876543210", request_id="custom-req-001")
    assert res_get["ok"] is True
    assert seen_headers["x-internal-key"] == "secret-key-123"
    assert seen_headers["x-acting-subject"] == "citizen:9876543210"
    assert seen_headers["x-request-id"] == "custom-req-001"

    # 2. POST
    res_post = await api.post("/me/draft", subject="citizen:9876543210", json_data={"name": "Nirmala"})
    assert res_post["ok"] is True
    assert seen_headers["x-acting-subject"] == "citizen:9876543210"


@pytest.mark.anyio
async def test_get_retries_once_on_transport_error_and_succeeds():
    attempts = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        attempts[0] += 1
        if attempts[0] == 1:
            raise httpx.ConnectError("Connection refused by test")
        return httpx.Response(200, json={"ok": True, "data": {"attempt": attempts[0]}})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = InternalApiClient(
        base_url="http://test-portal/internal/api/v1",
        internal_api_key="key",
        client=mock_client,
    )

    res = await api.get("/scheme", subject="citizen:1234567890")
    assert attempts[0] == 2
    assert res["ok"] is True
    assert res["data"]["attempt"] == 2


@pytest.mark.anyio
async def test_get_retries_once_on_502_and_succeeds():
    attempts = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        attempts[0] += 1
        if attempts[0] == 1:
            return httpx.Response(502, text="Bad Gateway")
        return httpx.Response(200, json={"ok": True, "data": {"recovered": True}})

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = InternalApiClient(
        base_url="http://test-portal/internal/api/v1",
        internal_api_key="key",
        client=mock_client,
    )

    res = await api.get("/scheme", subject="citizen:1234567890")
    assert attempts[0] == 2
    assert res["ok"] is True
    assert res["data"]["recovered"] is True


@pytest.mark.anyio
async def test_post_does_not_retry_on_failure():
    attempts = [0]

    def handler(request: httpx.Request) -> httpx.Response:
        attempts[0] += 1
        raise httpx.ConnectError("Connection failed")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = InternalApiClient(
        base_url="http://test-portal/internal/api/v1",
        internal_api_key="key",
        client=mock_client,
    )

    res = await api.post("/me/applications", subject="citizen:1234567890", json_data={"code": "123"})
    # Must only attempt once
    assert attempts[0] == 1
    assert res["ok"] is False
    assert res["code"] == "service_unavailable"
    assert "The pension portal is not responding right now" in res["message_for_user"]
    # No stack trace leaked
    assert "Traceback" not in res["message_for_user"]
    assert "ConnectError" not in res["message_for_user"]


@pytest.mark.anyio
async def test_persistent_transport_failure_returns_friendly_envelope():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("Read timed out after 5.0s")

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = InternalApiClient(
        base_url="http://test-portal/internal/api/v1",
        internal_api_key="key",
        client=mock_client,
    )

    res = await api.get("/scheme", subject="citizen:1234567890")
    assert res["ok"] is False
    assert res["code"] == "service_unavailable"
    assert "The pension portal is not responding right now; please try again in a minute." in res["message_for_user"]
    assert "TimeoutException" not in str(res)
    assert "Traceback" not in str(res)
