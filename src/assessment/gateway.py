"""Tasks 2-4: authenticated MCP proxy and LLM gateway endpoints."""
import asyncio
import hashlib
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Annotated, Literal

import httpx
import jwt
import tiktoken
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .guardrail import StreamingRedactor
from .limiter import RateLimitExceeded, TokenLimiter


@dataclass
class Settings:
    jwt_secret: str
    downstream: str = "http://127.0.0.1:8001/mcp"
    primary: str = "http://127.0.0.1:8001/v1/chat/completions"
    backup: str = "http://127.0.0.1:8001/backup/v1/chat/completions"
    primary_key: str = ""
    backup_key: str = ""
    primary_model: str = "demo-primary"
    backup_model: str = "demo-backup"
    database: str = "data/usage.sqlite3"
    timeout: float = 3.0
    token_limit: int = 50_000

    @classmethod
    def from_env(cls):
        secret = os.environ.get("JWT_SECRET", "")
        if len(secret) < 32:
            raise RuntimeError("Set JWT_SECRET to at least 32 characters")
        return cls(jwt_secret=secret,
                   downstream=os.getenv("MCP_DOWNSTREAM_URL", cls.downstream),
                   primary=os.getenv("PRIMARY_URL", cls.primary),
                   backup=os.getenv("BACKUP_URL", cls.backup),
                   primary_key=os.getenv("PRIMARY_API_KEY", ""),
                   backup_key=os.getenv("BACKUP_API_KEY", ""),
                   primary_model=os.getenv("PRIMARY_MODEL", cls.primary_model),
                   backup_model=os.getenv("BACKUP_MODEL", cls.backup_model),
                   database=os.getenv("DATABASE_PATH", cls.database))


class Message(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role: Literal["system", "user", "assistant"]
    content: Annotated[str, Field(max_length=100_000)]


class Completion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    messages: Annotated[list[Message], Field(min_length=1, max_length=100)]
    max_tokens: Annotated[int, Field(gt=0, le=8192)] = 256


class GatewayFailure(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def rpc_error(request_id, code, message, status=200):
    return JSONResponse({"jsonrpc": "2.0", "id": request_id,
                         "error": {"code": code, "message": message}}, status_code=status)


def authenticate(request: Request, secret: str):
    value = request.headers.get("authorization", "")
    scheme, _, token = value.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise GatewayFailure(401, "unauthorized", "A valid bearer token is required")
    try:
        claims = jwt.decode(token, secret, algorithms=["HS256"],
                            issuer="assessment", audience="assessment-gateway",
                            options={"require": ["exp", "sub", "role", "iss", "aud"]})
        if claims["role"] not in {"viewer", "admin"} or not isinstance(claims["sub"], str) or not claims["sub"]:
            raise ValueError()
        return claims
    except (jwt.PyJWTError, ValueError):
        raise GatewayFailure(401, "unauthorized", "A valid bearer token is required") from None


async def sse_data(response: httpx.Response):
    """Parse SSE frames, independent of HTTP chunk boundaries; cap frame memory."""
    buffer = ""
    data = []
    size = 0
    async for text in response.aiter_text():
        buffer += text
        if len(buffer) > 1_048_576:
            raise ValueError("Oversize SSE frame")
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            line = line.rstrip("\r")
            if not line:
                if data:
                    yield "\n".join(data)
                    data, size = [], 0
            elif line.startswith("data:"):
                value = line[5:].lstrip(" ")
                size += len(value)
                if size > 1_048_576:
                    raise ValueError("Oversize SSE frame")
                data.append(value)
    if buffer.startswith("data:"):
        data.append(buffer[5:].lstrip(" ").rstrip("\r"))
    if data:
        yield "\n".join(data)


def event(text):
    return "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n"


def create_app(settings: Settings | None = None, client: httpx.AsyncClient | None = None):
    settings = settings or Settings.from_env()
    limiter = TokenLimiter(settings.database, limit=settings.token_limit)
    tokenizer = tiktoken.get_encoding("cl100k_base")
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=httpx.Timeout(30.0), follow_redirects=False)

    @asynccontextmanager
    async def lifespan(app):
        yield
        if own_client:
            await client.aclose()

    app = FastAPI(title="AI Field Developer Assessment", lifespan=lifespan)

    @app.exception_handler(GatewayFailure)
    async def failure_handler(request, exc):
        return JSONResponse({"error": {"code": exc.code, "message": exc.message}}, status_code=exc.status)

    @app.exception_handler(Exception)
    async def unexpected_failure(request, exc):
        return JSONResponse({"error": {"code": "internal_error", "message": "Gateway request failed"}}, status_code=500)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/mcp")
    async def mcp_proxy(request: Request):
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            return rpc_error(None, -32700, "Parse error")
        if (not isinstance(body, dict) or body.get("jsonrpc") != "2.0"
                or not isinstance(body.get("method"), str)
                or ("id" in body and (isinstance(body["id"], bool) or not isinstance(body["id"], (str, int, type(None)))))):
            return rpc_error(None, -32600, "Invalid Request")
        request_id = body.get("id")
        try:
            claims = authenticate(request, settings.jwt_secret)
        except GatewayFailure:
            return rpc_error(request_id, -32001, "Unauthorized", 401)
        params = body.get("params", {})
        if not isinstance(params, dict):
            return rpc_error(request_id, -32602, "Invalid params")
        notification = "id" not in body
        if body["method"] == "tools/call":
            name = params.get("name")
            if not isinstance(name, str) or not isinstance(params.get("arguments", {}), dict):
                return Response(status_code=204) if notification else rpc_error(request_id, -32602, "Invalid params")
            if name.startswith("admin_") and claims["role"] != "admin":
                return Response(status_code=204) if notification else rpc_error(request_id, -32001, "Unauthorized Tool Call")
        headers = {key: request.headers[key] for key in ("accept", "mcp-session-id", "mcp-protocol-version") if key in request.headers}
        try:
            response = await client.post(settings.downstream, json=body, headers=headers)
            forwarded_headers = {key: response.headers[key] for key in ("content-type", "mcp-session-id", "mcp-protocol-version") if key in response.headers}
            return Response(content=response.content, status_code=response.status_code, headers=forwarded_headers)
        except httpx.HTTPError:
            return Response(status_code=204) if notification else rpc_error(request_id, -32002, "Downstream unavailable", 502)

    async def parse_completion(request):
        try:
            return Completion.model_validate(await request.json())
        except (ValidationError, ValueError, UnicodeDecodeError):
            raise GatewayFailure(400, "invalid_request", "Invalid completion request") from None

    async def invoke(body, url, key, model):
        payload = {**body.model_dump(), "model": model, "stream": False}
        response = await client.post(url, json=payload, headers={"Authorization": f"Bearer {key}"} if key else {})
        if response.status_code == 429:
            raise GatewayFailure(429, "provider_rate_limit", "Provider rate limited")
        if response.status_code >= 400:
            raise GatewayFailure(502, "upstream_error", "Provider request failed")
        result = response.json()
        if not isinstance(result, dict) or not isinstance(result.get("choices"), list):
            raise ValueError("Invalid provider response")
        return result

    @app.post("/v1/completions")
    async def complete(request: Request):
        claims = authenticate(request, settings.jwt_secret)
        body = await parse_completion(request)
        # Chat envelope allowance is an estimate; provider-reported usage settles it.
        prompt_tokens = 3 + sum(4 + len(tokenizer.encode(m.content, disallowed_special=())) + len(tokenizer.encode(m.role)) for m in body.messages)
        tenant = hashlib.sha256(claims["sub"].encode()).hexdigest()
        try:
            reservation = await asyncio.to_thread(limiter.reserve, tenant, prompt_tokens + body.max_tokens)
        except RateLimitExceeded:
            raise GatewayFailure(429, "rate_limit_exceeded", "Tenant token budget exceeded") from None
        # Keep the reservation if upstream outcome/usage is unknown: never undercount.
        try:
            provider = "primary"
            try:
                result = await asyncio.wait_for(invoke(body, settings.primary, settings.primary_key, settings.primary_model), settings.timeout)
            except (asyncio.TimeoutError, httpx.TimeoutException):
                provider = "backup"
                result = await asyncio.wait_for(invoke(body, settings.backup, settings.backup_key, settings.backup_model), settings.timeout)
            except GatewayFailure as exc:
                if exc.status != 429:
                    raise
                provider = "backup"
                result = await asyncio.wait_for(invoke(body, settings.backup, settings.backup_key, settings.backup_model), settings.timeout)
            usage = result.get("usage", {}).get("total_tokens")
            if isinstance(usage, int) and not isinstance(usage, bool) and usage >= 0:
                await asyncio.to_thread(limiter.settle, reservation, usage)
            # Allowlist fields rather than forwarding arbitrary provider diagnostics.
            return {"provider": provider, "choices": result["choices"], "usage": result.get("usage", {})}
        except GatewayFailure:
            raise
        except (asyncio.TimeoutError, httpx.TimeoutException):
            raise GatewayFailure(504, "upstream_timeout", "Providers did not respond in time") from None
        except (httpx.HTTPError, ValueError, TypeError, AttributeError):
            raise GatewayFailure(502, "upstream_error", "Provider request failed") from None

    @app.post("/v1/stream")
    async def stream(request: Request):
        authenticate(request, settings.jwt_secret)
        body = await parse_completion(request)
        async def generate():
            redactor = StreamingRedactor()
            try:
                payload = {**body.model_dump(), "model": settings.primary_model, "stream": True}
                headers = {"Authorization": f"Bearer {settings.primary_key}"} if settings.primary_key else {}
                async with client.stream("POST", settings.primary, json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        raise ValueError("Upstream error")
                    done = False
                    async for data in sse_data(response):
                        if data == "[DONE]":
                            done = True
                            break
                        chunk = json.loads(data)
                        for choice in chunk.get("choices", []):
                            delta = choice.get("delta", {})
                            if delta.get("tool_calls") or delta.get("function_call"):
                                raise ValueError("Only text deltas supported")
                            content = delta.get("content")
                            if content is not None:
                                if not isinstance(content, str):
                                    raise ValueError("Invalid content")
                                safe = redactor.feed(content)
                                if safe:
                                    yield event(safe)
                    if not done:
                        raise ValueError("Truncated stream")
                    tail = redactor.finish()
                    if tail:
                        yield event(tail)
                    yield "data: [DONE]\n\n"
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                # Never flush an incomplete PII candidate on provider failure.
                yield 'event: error\ndata: {"error":{"code":"upstream_error","message":"Stream interrupted"}}\n\n'
        return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    return app
