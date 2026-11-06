"""Task 1: official SDK, stdio, strict Pydantic validation."""
import asyncio
import json
import logging
import sys
from typing import Annotated

from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server
from mcp.shared.exceptions import McpError
from mcp.types import ErrorData, TextContent, Tool, INVALID_PARAMS, CallToolRequest, CallToolResult, ServerResult
from pydantic import BaseModel, ConfigDict, Field, ValidationError

CustomerID = Annotated[str, Field(strict=True, pattern=r"^CUST-[0-9]{5}$")]


class CustomerInput(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    customer_id: CustomerID


class RefundInput(CustomerInput):
    amount: Annotated[float, Field(gt=0, allow_inf_nan=False)]
    reason: Annotated[str, Field(min_length=10)]


CUSTOMERS = {"CUST-00001": {"customer_id": "CUST-00001", "name": "Demo Customer"}}
MODELS = {"get_customer_record": CustomerInput, "trigger_refund": RefundInput}
server = Server("customer-tools")


@server.list_tools()
async def list_tools():
    return [Tool(name=name, description=("Look up a mock customer." if name == "get_customer_record"
                else "Simulate a refund; no payment is made."), inputSchema=model.model_json_schema())
            for name, model in MODELS.items()]


async def call_tool(name: str, arguments: dict):
    if name not in MODELS:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="Unknown tool"))
    try:
        data = MODELS[name].model_validate(arguments)
    except ValidationError:
        raise McpError(ErrorData(code=INVALID_PARAMS, message="Invalid tool arguments")) from None
    customer = CUSTOMERS.get(data.customer_id)
    if customer is None:
        return CallToolResult(isError=True, content=[TextContent(type="text", text="Customer not found")])
    result = customer if name == "get_customer_record" else {
        "status": "simulated", **data.model_dump()}
    return [TextContent(type="text", text=json.dumps(result))]


async def handle_call(request: CallToolRequest):
    # The SDK decorator converts exceptions to isError tool results. The
    # assessment explicitly requires JSON-RPC -32602 for invalid arguments,
    # so register a typed handler that lets McpError reach the SDK dispatcher.
    result = await call_tool(request.params.name, request.params.arguments or {})
    if not isinstance(result, CallToolResult):
        result = CallToolResult(content=result, isError=False)
    return ServerResult(result)


server.request_handlers[CallToolRequest] = handle_call


async def run():
    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


def main():
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    asyncio.run(run())


if __name__ == "__main__":
    main()
