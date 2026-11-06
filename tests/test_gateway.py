import asyncio
import json
import time

import httpx
import jwt
import pytest

from assessment.gateway import Settings, create_app, sse_data

SECRET = "test-secret-at-least-thirty-two-characters"


def auth(role="viewer", **overrides):
    claims = {"sub": "tenant-a", "role": role, "exp": time.time() + 60,
              "iss": "assessment", "aud": "assessment-gateway", **overrides}
    return {"Authorization": "Bearer " + jwt.encode(claims, SECRET, algorithm="HS256")}


def setup(tmp_path, handler, **kwargs):
    upstream = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    settings = Settings(jwt_secret=SECRET, database=str(tmp_path / "usage.db"), **kwargs)
    app = create_app(settings, upstream)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test"), upstream


async def test_proxy_role_filter_and_transparent_list(tmp_path):
    calls = []
    def handler(request):
        calls.append(json.loads(request.content))
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": 7, "result": {"tools": []}}, headers={"mcp-session-id": "demo"})
    client, upstream = setup(tmp_path, handler)
    async with client, upstream:
        body = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "admin_reset_key"}}
        response = await client.post("/mcp", json=body, headers=auth())
        assert response.json()["error"] == {"code": -32001, "message": "Unauthorized Tool Call"}
        assert not calls
        assert (await client.post("/mcp", json=body, headers=auth("admin"))).status_code == 200
        listing = {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}
        response = await client.post("/mcp", json=listing, headers=auth())
        assert calls[-1] == listing
        assert response.headers["mcp-session-id"] == "demo"
        assert response.json()["result"] == {"tools": []}
        assert (await client.post("/mcp", json=body)).status_code == 401
        assert (await client.post("/mcp", json=body, headers=auth(exp=0))).status_code == 401
        assert (await client.post("/mcp", content="{", headers=auth())).json()["error"]["code"] == -32700
        assert (await client.post("/mcp", json=[], headers=auth())).json()["error"]["code"] == -32600
        assert (await client.post("/mcp", json={**body, "params": []}, headers=auth())).json()["error"]["code"] == -32602


@pytest.mark.parametrize("mode", ["429", "timeout", "http_timeout"])
async def test_fallback(tmp_path, mode):
    calls = []
    async def handler(request):
        calls.append(str(request.url))
        if "/backup/" not in str(request.url):
            if mode == "429":
                return httpx.Response(429)
            if mode == "http_timeout":
                raise httpx.ReadTimeout("private diagnostic")
            await asyncio.sleep(0.1)
        return httpx.Response(200, json={"choices": [], "usage": {"total_tokens": 5}})
    client, upstream = setup(tmp_path, handler, timeout=0.01)
    async with client, upstream:
        response = await client.post("/v1/completions", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth())
        assert response.status_code == 200
        assert response.json()["provider"] == "backup"
        assert len(calls) == 2


async def test_sanitized_errors_and_local_budget(tmp_path):
    def handler(request):
        return httpx.Response(500, text="SECRET STACK TRACE")
    client, upstream = setup(tmp_path, handler, token_limit=30)
    async with client, upstream:
        payload = {"messages": [{"role": "user", "content": "hello"}], "max_tokens": 10}
        response = await client.post("/v1/completions", json=payload, headers=auth())
        assert response.status_code == 502
        assert "SECRET" not in response.text
        response = await client.post("/v1/completions", json=payload, headers=auth())
        assert response.status_code == 429
        assert response.json()["error"]["code"] == "rate_limit_exceeded"


async def test_stream_redacts_without_forwarding_raw_metadata(tmp_path):
    frames = ["alice@", "example.com", "; 123-", "45-6789", "; 4111 1111 ", "1111 1111", "; done"]
    text = "".join("data: " + json.dumps({"choices": [{"delta": {"content": p}}], "debug": "secret"}) + "\n\n" for p in frames) + "data: [DONE]\n\n"
    client, upstream = setup(tmp_path, lambda r: httpx.Response(200, text=text))
    async with client, upstream:
        response = await client.post("/v1/stream", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth())
        data = [line[6:] for line in response.text.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        content = "".join(json.loads(d)["choices"][0]["delta"]["content"] for d in data)
        assert content == "[REDACTED]; [REDACTED]; [REDACTED]; done"
        assert "secret" not in response.text


class Fragments(httpx.AsyncByteStream):
    async def __aiter__(self):
        for chunk in [b'da', b'ta: {"x":', b'1}\r', b'\n\r\n', b'data: [DONE]\n\n']:
            yield chunk


async def test_sse_parser_arbitrary_http_splits():
    response = httpx.Response(200, stream=Fragments())
    assert [x async for x in sse_data(response)] == ['{"x":1}', '[DONE]']


async def test_both_providers_timeout(tmp_path):
    async def handler(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={"choices": []})
    client, upstream = setup(tmp_path, handler, timeout=0.005)
    async with client, upstream:
        response = await client.post("/v1/completions", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth())
        assert response.status_code == 504
        assert response.json() == {"error": {"code": "upstream_timeout", "message": "Providers did not respond in time"}}


async def test_blocked_notification_never_reaches_downstream(tmp_path):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(204)
    client, upstream = setup(tmp_path, handler)
    async with client, upstream:
        response = await client.post("/mcp", json={"jsonrpc": "2.0", "method": "tools/call", "params": {"name": "admin_reset_key"}}, headers=auth())
        assert response.status_code == 204
        assert not response.content
        assert not calls


async def test_truncated_stream_does_not_flush_pii(tmp_path):
    text = 'data: {"choices":[{"delta":{"content":"alice@"}}]}\n\n'
    client, upstream = setup(tmp_path, lambda r: httpx.Response(200, text=text))
    async with client, upstream:
        response = await client.post("/v1/stream", json={"messages": [{"role": "user", "content": "hi"}]}, headers=auth())
        assert "alice" not in response.text
        assert "event: error" in response.text
        assert "[DONE]" not in response.text


async def test_full_mock_provider_demo(tmp_path):
    from assessment.mock_provider import app as mock_app
    upstream = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_app))
    settings = Settings(jwt_secret=SECRET, database=str(tmp_path / "usage.db"))
    app = create_app(settings, upstream)
    async with upstream, httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        listing = await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=auth())
        assert len(listing.json()["result"]["tools"]) == 2
        stream = await client.post("/v1/stream", json={"messages": [{"role": "user", "content": "demo"}]}, headers=auth())
        assert stream.status_code == 200
        assert stream.text.count("[REDACTED]") == 3
        assert "alice" not in stream.text
        fallback = await client.post("/v1/completions", json={"messages": [{"role": "user", "content": "simulate-429"}]}, headers=auth())
        assert fallback.json()["provider"] == "backup"
