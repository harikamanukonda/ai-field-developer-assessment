# AI Field Developer / Solutions Engineer Assessment

Four runnable Python tasks: a strict MCP server, an authorization proxy, a streaming PII guardrail, and a persistent token-aware model router. Includes a local mock provider, automated tests, and GitHub Actions. No paid API credentials are needed for the demo.

## Quick start

Requires Python 3.10+.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
python -m pip install --no-deps -e .
pytest -q
```

The first gateway startup downloads tiktoken's `cl100k_base` vocabulary and caches it in the system temporary directory. For offline deployment, prewarm/cache this vocabulary during image creation.

## Task 1: strict MCP server

```bash
python -m assessment.mcp_server
```

This process speaks newline-delimited MCP JSON-RPC on stdio, so a quiet terminal means it is waiting for a client. Use an MCP client rather than entering ordinary text. All logs go to stderr.

For a complete client/server demo, run `python -m assessment.demo_client` instead. It lists the tools, fetches a customer, simulates a refund, and shows an expected validation error.

Example client configuration (replace both absolute paths):

```json
{
  "mcpServers": {
    "customer-tools": {
      "command": "/absolute/path/to/repo/.venv/bin/python",
      "args": ["-m", "assessment.mcp_server"]
    }
  }
}
```

| Tool | Arguments | Behavior |
| --- | --- | --- |
| `get_customer_record` | `customer_id` | Returns the mock customer `CUST-00001` |
| `trigger_refund` | `customer_id`, `amount`, `reason` | Returns a simulated refund; never moves money |

Validation uses strict Pydantic models with extra fields forbidden. Customer IDs are interpreted as `CUST-` plus exactly five ASCII digits. Amount must be a positive finite JSON number (integer JSON numbers are accepted; booleans and numeric strings are rejected). Reason must contain at least 10 characters. Invalid arguments and unknown tools return JSON-RPC `-32602`. A valid but unknown customer returns a tool result with `isError: true`.

The official SDK implements initialization, discovery, transport, and dispatch. A typed call handler is registered directly because the SDK's usual decorator catches exceptions and returns tool-level errors; this assessment requires protocol-level validation errors.

## Run the HTTP demo

In terminal A:

```bash
source .venv/bin/activate
uvicorn assessment.mock_provider:app --host 127.0.0.1 --port 8001
```

In terminal B:

```bash
source .venv/bin/activate
export JWT_SECRET="$(python -c 'import secrets; print(secrets.token_hex(32))')"
export VIEWER_TOKEN="$(python -m assessment.token --role viewer)"
export ADMIN_TOKEN="$(python -m assessment.token --role admin)"
uvicorn assessment.gateway:create_app --factory --host 127.0.0.1 --port 8000
```

Run the following examples in terminal B before starting uvicorn, or in another terminal with the same exported variables. Tokens expire after one hour. Do not commit real tokens or secrets.

Interactive HTTP API documentation: <http://127.0.0.1:8000/docs>

## Task 2: MCP security gateway

```bash
curl -s http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $VIEWER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":1,"method":"tools/list"}'

curl -s http://127.0.0.1:8000/mcp \
  -H "Authorization: Bearer $VIEWER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":2,"method":"tools/call","params":{"name":"admin_reset_key","arguments":{}}}'
```

The second request returns `-32001: Unauthorized Tool Call` without contacting the downstream. Repeat with `$ADMIN_TOKEN` to allow it. `tools/list` responses are forwarded unchanged, as requested, including admin tool names. Authorization is enforced at invocation time. All authenticated non-tool methods are forwarded to permit MCP initialization and notifications.

Bearer tokens are signed HS256 JWTs, validated for signature, expiry, issuer, audience, subject and role. Tenant identity comes from the signed subject. Client credentials are not forwarded to the downstream. Content type and MCP session/protocol headers are preserved. JSON-RPC notifications receive no generated JSON-RPC response. Batch JSON-RPC requests are deliberately rejected as invalid requests.

This is the assessment's lightweight HTTP/JSON-RPC proxy to a mock downstream, not a complete production Streamable HTTP implementation: it does not proxy GET-based event streams, DELETE session termination, or server-to-client requests. Task 1 independently provides the real SDK-based MCP server over stdio.

## Task 3: streaming PII guardrail

```bash
curl -N http://127.0.0.1:8000/v1/stream \
  -H "Authorization: Bearer $VIEWER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"Show the demo"}],"max_tokens":100}'
```

The mock deliberately splits an email, SSN and card number across deltas. All three become `[REDACTED]`. Output is SSE with text deltas and a final `[DONE]`. Provider metadata is not forwarded, preventing a second unfiltered output channel. Malformed/truncated streams produce a sanitized SSE error; a pending candidate is discarded on failure.

The parser handles arbitrary HTTP chunk boundaries, CRLF, multiline SSE data and bounded frame sizes. The redactor buffers only a possible candidate rather than the entire completion. Candidate state is capped at 512 characters; an overlong candidate is redacted and suppressed until a safe delimiter. Email-like words wait for a delimiter; numeric candidates also hold spaces because cards can contain spaces. This trades some token latency for preventing partial PII leakage. There is no whole-response buffer.

Scope: ASCII email patterns, formatted US SSNs, and 13-19 digit card candidates with optional spaces/hyphens. Card matches are conservative (no Luhn requirement). This is a pattern guardrail, not universal PII detection: it does not detect names, addresses, obfuscated emails, Unicode emails or arbitrary account numbers. Overlong non-PII words/numeric runs can be over-redacted. Only text completion deltas are supported; tool-call deltas are rejected.

## Task 4: rate limiting and model fallback

```bash
curl -s http://127.0.0.1:8000/v1/completions \
  -H "Authorization: Bearer $VIEWER_TOKEN" -H 'Content-Type: application/json' \
  -d '{"messages":[{"role":"user","content":"simulate-429"}],"max_tokens":100}'
```

Use `simulate-timeout` instead to demonstrate the 3000 ms primary deadline. Either returns a backup completion. Ordinary prompts use the primary. A backup call has its own 3000 ms deadline. Fallback occurs for primary HTTP 429 or timeout; other failures return sanitized errors. Both calls receive their configured provider model names and API keys.

SQLite stores a 60-second sliding window per tenant, with a default cap of 50,000 tokens. `BEGIN IMMEDIATE` makes check-and-reserve atomic across threads/processes sharing the same on-disk database. WAL and a busy timeout support concurrent access. Expired rows are evicted at the exact window boundary. Blocking SQLite calls run off the event loop.

Before contacting a provider, the router reserves estimated prompt tokens plus `max_tokens`; trustworthy integer `usage.total_tokens` settles the reservation after success. Missing usage, cancellation or failure retains the full reservation, avoiding free retries. Tokenization uses `cl100k_base` with a chat-envelope allowance; different providers may count prompts differently. Actual usage can exceed the estimate and is recorded, blocking subsequent admissions until capacity expires. A timed-out primary may still be billed upstream; the gateway cannot cancel remote billing or know its final usage. The local quota accounts for the logical request, not every physical provider attempt. These accounting choices are explicit tradeoffs for this assessment.

The limiter applies to `/v1/completions`. `/v1/stream` demonstrates the separate Task 3 guardrail and does not use this limiter or attempt mid-stream fallback. Fallback after partial output would risk duplicate content.

All gateway errors have the form:

```json
{"error":{"code":"upstream_error","message":"Provider request failed"}}
```

Codes include `unauthorized`, `invalid_request`, `rate_limit_exceeded`, `provider_rate_limit`, `upstream_error`, and `upstream_timeout`. Provider failure bodies, exception text and stack traces are never returned by these error paths.

## Configuration

| Environment variable | Default / purpose |
| --- | --- |
| `JWT_SECRET` | Required, at least 32 characters; demo HS256 signing secret |
| `MCP_DOWNSTREAM_URL` | `http://127.0.0.1:8001/mcp` |
| `PRIMARY_URL` | `http://127.0.0.1:8001/v1/chat/completions` |
| `BACKUP_URL` | `http://127.0.0.1:8001/backup/v1/chat/completions` |
| `PRIMARY_MODEL`, `BACKUP_MODEL` | `demo-primary`, `demo-backup` |
| `PRIMARY_API_KEY`, `BACKUP_API_KEY` | Empty for mocks; configure for real providers |
| `DATABASE_PATH` | `data/usage.sqlite3` |

Provider endpoints must implement the OpenAI-compatible chat completions shape. The SQLite file must be on local writable persistent disk. Do not use `:memory:` (operations use separate connections) or network filesystem locking for this design. Public deployment additionally needs TLS, request/body limits, operational logging, secret management and an appropriate identity provider. This demo binds to localhost by default.

## Repository map

```text
src/assessment/
  mcp_server.py      Task 1, real stdio MCP server
  gateway.py         Tasks 2-4, HTTP routes and provider handling
  guardrail.py       Task 3, incremental bounded redactor
  limiter.py         Task 4, persistent atomic token reservations
  mock_provider.py   Local HTTP downstream/provider test double
  token.py           Short-lived signed demo token issuer
tests/               Protocol, authorization, streaming, quota and fallback tests
```

## Validation

Run `pytest -q`. Tests exercise a real SDK stdio client/server session, strict field errors, admin call blocking before forwarding, JWT expiry, SSE chunk splits, every split point of representative PII strings, bounded candidate memory, persistent sliding-window eviction, concurrent reservations, fallback on 429 and both timeout types, and sanitized errors. GitHub Actions runs the same suite on Python 3.10 and 3.12.

## Design references

- [Official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)
- [MCP tools specification](https://modelcontextprotocol.io/specification/2025-06-18/server/tools)
- [JSON-RPC 2.0](https://www.jsonrpc.org/specification)

Implementation prepared with AI assistance; review the code and accounting/guardrail limitations before presenting or deploying it.
