"""Run Task 1 end-to-end with the official SDK client."""
import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError


async def main():
    params = StdioServerParameters(command=sys.executable, args=["-m", "assessment.mcp_server"])
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            print("Tools:", [tool.name for tool in (await session.list_tools()).tools])
            result = await session.call_tool("get_customer_record", {"customer_id": "CUST-00001"})
            print("Customer:", result.content[0].text)
            result = await session.call_tool("trigger_refund", {"customer_id": "CUST-00001", "amount": 12.5, "reason": "Product was damaged"})
            print("Refund:", result.content[0].text)
            try:
                await session.call_tool("get_customer_record", {"customer_id": "invalid"})
            except McpError as exc:
                print("Expected validation error:", exc.error.code, exc.error.message)


if __name__ == "__main__":
    asyncio.run(main())
