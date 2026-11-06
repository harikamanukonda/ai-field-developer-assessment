import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError


async def test_real_stdio_tools_and_validation():
    params = StdioServerParameters(command=sys.executable, args=["-m", "assessment.mcp_server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            assert {t.name for t in tools.tools} == {"get_customer_record", "trigger_refund"}
            result = await session.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
            assert json.loads(result.content[0].text)["name"] == "Demo Customer"
            refund = await session.call_tool("trigger_refund", {"customer_id": "CUST-00001", "amount": 2.5, "reason": "Item was damaged"})
            assert json.loads(refund.content[0].text)["status"] == "simulated"
            missing = await session.call_tool("get_customer_record", {"customer_id": "CUST-99999"})
            assert missing.isError
            cases = [
                ("get_customer_record", {"customer_id": "bad"}),
                ("get_customer_record", {"customer_id": 1}),
                ("get_customer_record", {"customer_id": "CUST-00001", "extra": True}),
                ("trigger_refund", {"customer_id": "CUST-00001", "amount": -1.0, "reason": "Item was damaged"}),
                ("trigger_refund", {"customer_id": "CUST-00001", "amount": "2.5", "reason": "Item was damaged"}),
                ("trigger_refund", {"customer_id": "CUST-00001", "amount": True, "reason": "Item was damaged"}),
                ("trigger_refund", {"customer_id": "CUST-00001", "amount": 1.0, "reason": "short"}),
                ("missing_tool", {}),
            ]
            for name, arguments in cases:
                with pytest.raises(McpError) as exc:
                    await session.call_tool(name, arguments)
                assert exc.value.error.code == -32602


async def test_stdout_contains_only_jsonrpc():
    import asyncio
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "assessment.mcp_server",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "1"}}}
    proc.stdin.write((json.dumps(init) + "\n").encode())
    await proc.stdin.drain()
    first = await asyncio.wait_for(proc.stdout.readline(), 5)
    assert json.loads(first)["jsonrpc"] == "2.0"
    proc.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    proc.stdin.write(b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}\n')
    await proc.stdin.drain()
    second = await asyncio.wait_for(proc.stdout.readline(), 5)
    assert json.loads(second)["id"] == 2
    proc.stdin.close()
    await asyncio.wait_for(proc.wait(), 5)
    assert not await proc.stdout.read()
    assert b"INFO" in await proc.stderr.read()
