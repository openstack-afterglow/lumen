"""An SSE connection row reaches the remote plugin through the real run selection gate.

Before this gate accepted ``sse``, a connection could be created, listed and catalog-refreshed, yet no run ever bound
its tools. The path here is real except for two replaced hops, documented on each fixture:

- the network hop: ``langchain_mcp_adapters`` opens SSE to the public HTTPS URL. The test forwards that call to a
  real FastMCP SSE server on 127.0.0.1, because a loopback destination is refused by the pinned public transport.
  The original configuration (https URL, bearer header, timeouts) is captured and asserted;
- the public-address policy: ``ssrf.validate_url`` is replaced for the fixture host, as the integration tests do.
"""

from __future__ import annotations

import socket
import threading
import time

import langchain_mcp_adapters.sessions as adapter_sessions
import pytest
import uvicorn
from lumen_plugin_api.contracts import ExecutionContext
from mcp.client.sse import sse_client as real_sse_client
from mcp.server.fastmcp import FastMCP

from lumen.plugins.registry import get_plugin
from lumen.services import extensions_store as es
from lumen.services import ssrf
from lumen.services.tool_runtime import selection
from lumen.services.tools import ToolContext

PUBLIC_URL = "https://events.example.com/sse"


@pytest.fixture(scope="module")
def sse_origin():
    server_app = FastMCP("events")

    @server_app.tool()
    def echo(text: str) -> str:
        """Echo the text back."""
        return text

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    server = uvicorn.Server(uvicorn.Config(server_app.sse_app(), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    assert server.started, "local SSE server did not start"
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


@pytest.fixture
def captured(monkeypatch, sse_origin):
    calls: list[dict] = []

    def forward(url, headers=None, timeout=5, sse_read_timeout=300, auth=None, **kwargs):
        calls.append(
            {"url": url, "headers": dict(headers or {}), "timeout": timeout, "sse_read_timeout": sse_read_timeout}
        )
        local = sse_origin + "/sse"
        return real_sse_client(local, headers=headers, timeout=timeout, sse_read_timeout=sse_read_timeout, auth=auth)

    monkeypatch.setattr(adapter_sessions, "sse_client", forward)
    monkeypatch.setattr(ssrf, "validate_url", lambda url: url)
    return calls


def _sse_row() -> dict:
    return {
        "id": 42,
        "name": "Events",
        "transport": "sse",
        "url": PUBLIC_URL,
        "auth_mode": "admin",
        "headers": {"Authorization": "Bearer read-only"},
        "is_active": True,
        "config_version": 1,
        "load_policy": "on_demand",
        "effect_overrides": None,
        "implicit_selection": True,
    }


async def test_sse_row_passes_the_gate_and_its_tools_are_discovered_and_called(monkeypatch, captured):
    async def visible(kind, **_kwargs):
        return [_sse_row()] if kind == "mcp" else []

    monkeypatch.setattr(es, "list_for_user", visible)
    ctx = ToolContext(project_id="p1", user_id="u1", selected_mcp_ids=(42,))

    usable = await selection._load_mcp(ctx)
    assert [server["id"] for server in usable] == [42], "the selection gate must admit an sse row"

    plugin = get_plugin("mcp", "remote-mcp")
    tools = await plugin.list_tools(usable[0])
    assert [tool["name"] for tool in tools] == ["echo"]

    context = ExecutionContext(user_id="u1", project_id="p1")
    output = await plugin.call_tool(
        usable[0], "echo", {"text": "hello-sse"}, context=context, expected_credential_epoch=None
    )
    assert output.text == "hello-sse"

    assert captured, "the SSE transport must be used for sse rows"
    first = captured[0]
    assert first["url"] == PUBLIC_URL
    assert first["headers"] == {"Authorization": "Bearer read-only"}
    assert first["timeout"] > 0
    assert first["sse_read_timeout"] > 0


async def test_non_https_sse_row_is_still_refused_by_the_gate(monkeypatch, captured):
    row = {**_sse_row(), "url": "http://events.example.com/sse"}

    async def visible(kind, **_kwargs):
        return [row] if kind == "mcp" else []

    monkeypatch.setattr(es, "list_for_user", visible)
    ctx = ToolContext(project_id="p1", user_id="u1", selected_mcp_ids=(42,))
    assert await selection._load_mcp(ctx) == []


async def test_stdio_row_is_refused_by_the_gate(monkeypatch, captured):
    row = {**_sse_row(), "transport": "stdio"}

    async def visible(kind, **_kwargs):
        return [row] if kind == "mcp" else []

    monkeypatch.setattr(es, "list_for_user", visible)
    ctx = ToolContext(project_id="p1", user_id="u1", selected_mcp_ids=(42,))
    assert await selection._load_mcp(ctx) == []
