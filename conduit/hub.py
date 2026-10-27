"""Claude Desktop's way in: a stdio MCP server that forwards every list, call and resource read to
the local gateway at /mcp, so Desktop users get the same blocks, delete approval, messages and
audit as every other client."""

from __future__ import annotations

import os
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession, types
from mcp.client.streamable_http import streamable_http_client
from mcp.server import NotificationOptions, Server
from mcp.server.stdio import stdio_server


def gateway_url(url: str) -> str:
    parts = urlsplit(url)
    if parts.scheme != "http" or parts.hostname not in ("localhost", "127.0.0.1", "::1") or parts.username:
        raise ValueError("The hub connects only to a Seira gateway on this computer")
    return url.rstrip("/") + "/mcp"


async def serve_hub(url: str, key: str | None = None):
    endpoint = gateway_url(url)
    key = key or os.environ.get("SEIRA_KEY") or None
    headers = {"Authorization": "Bearer " + key} if key else {}
    downstream = []

    async def forward_notifications(message):
        if isinstance(message, types.ServerNotification) and isinstance(message.root, types.ToolListChangedNotification):
            for session in list(downstream):
                try:
                    await session.send_tool_list_changed()
                except Exception:
                    downstream.remove(session)

    async with httpx.AsyncClient(headers=headers, timeout=httpx.Timeout(30, read=None), trust_env=False) as http:
        async with streamable_http_client(endpoint, http_client=http) as (read, write, _):
            async with ClientSession(read, write, message_handler=forward_notifications) as upstream:
                init = await upstream.initialize()
                server = Server("seira", version=init.serverInfo.version, instructions=init.instructions)

                def remember():
                    try:
                        session = server.request_context.session
                    except LookupError:
                        return
                    if session not in downstream:
                        downstream.append(session)

                @server.list_tools()
                async def list_tools():
                    remember()
                    return (await upstream.list_tools()).tools

                @server.call_tool(validate_input=False)
                async def call_tool(name, arguments):
                    remember()
                    try:
                        return await upstream.call_tool(name, arguments or {})
                    except Exception:
                        return types.CallToolResult(content=[types.TextContent(type="text", text=(
                            "Seira stopped answering. If this was a change, check it before trying again."))],
                            isError=True)

                @server.list_resources()
                async def list_resources():
                    return (await upstream.list_resources()).resources

                @server.read_resource()
                async def read_resource(uri):
                    result = await upstream.read_resource(uri)
                    return "".join(getattr(content, "text", "") for content in result.contents)

                async with stdio_server() as (stream_in, stream_out):
                    await server.run(stream_in, stream_out, server.create_initialization_options(
                        NotificationOptions(tools_changed=True)))
