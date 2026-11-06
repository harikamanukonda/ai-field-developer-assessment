"""Local mock MCP downstream and OpenAI-compatible provider; no external API keys."""
import asyncio
import json

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

app = FastAPI(title="Assessment mock provider")


@app.post("/mcp")
async def mcp(request: Request):
    body = await request.json()
    if "id" not in body:
        return Response(status_code=204)
    method = body["method"]
    if method == "tools/list":
        result = {"tools": [{"name": name, "inputSchema": {"type": "object"}} for name in ["get_customer_record", "admin_reset_key"]]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "Mock tool executed"}], "isError": False}
    elif method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "mock", "version": "1.0"}}
    else:
        return {"jsonrpc": "2.0", "id": body["id"], "error": {"code": -32601, "message": "Method not found"}}
    return {"jsonrpc": "2.0", "id": body["id"], "result": result}


@app.post("/v1/chat/completions")
@app.post("/backup/v1/chat/completions")
async def completion(request: Request):
    body = await request.json()
    prompt = body["messages"][-1]["content"]
    if not request.url.path.startswith("/backup"):
        if prompt == "simulate-429":
            return JSONResponse({"error": "mock rate limited"}, status_code=429)
        if prompt == "simulate-timeout":
            await asyncio.sleep(4)
    if body.get("stream"):
        async def chunks():
            for text in ["Contact ", "alice@", "example.com", "; SSN ", "123-", "45-6789", "; card ", "4111 1111 ", "1111 1111", "; done."]:
                yield "data: " + json.dumps({"choices": [{"delta": {"content": text}}]}) + "\n\n"
                await asyncio.sleep(0.01)
            yield "data: [DONE]\n\n"
        return StreamingResponse(chunks(), media_type="text/event-stream")
    return {"choices": [{"message": {"role": "assistant", "content": "Mock completion"}}], "usage": {"total_tokens": 20}}
