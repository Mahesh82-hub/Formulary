from functools import lru_cache
from typing import Any

from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError

from app.mcp_gateway.models import MCPToolDefinition, MCPToolResult
from app.mcp_gateway.server import get_internal_mcp_server


class MCPToolInvocationError(RuntimeError):
    """Raised when an MCP tool cannot be invoked successfully."""


class FastMCPToolClient:
    """Small application-owned boundary around the FastMCP client."""

    def __init__(self, server: FastMCP[None]) -> None:
        self._server = server

    async def list_tools(self) -> list[MCPToolDefinition]:
        async with Client(self._server) as client:
            tools = await client.list_tools()
        return [
            MCPToolDefinition(
                name=tool.name,
                description=tool.description or "",
                input_schema=tool.inputSchema,
                output_schema=tool.outputSchema,
            )
            for tool in tools
        ]

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
    ) -> MCPToolResult:
        try:
            async with Client(self._server) as client:
                result = await client.call_tool(name, arguments or {})
        except ToolError as error:
            raise MCPToolInvocationError(f"MCP tool {name!r} failed") from error

        return MCPToolResult(
            tool_name=name,
            data=(
                result.structured_content if result.structured_content is not None else result.data
            ),
            structured_content=result.structured_content,
            is_error=result.is_error,
        )


@lru_cache
def get_mcp_tool_client() -> FastMCPToolClient:
    return FastMCPToolClient(get_internal_mcp_server())
