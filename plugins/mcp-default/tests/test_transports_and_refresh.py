"""Remote MCP transports (streamable HTTP, SSE) and at-use OAuth refresh in the execution path.

Properties:
- only hosted HTTPS streamable HTTP and SSE are built; local process (stdio) is refused;
- a bearer token that is about to expire is refreshed when the server is resolved, and the returned credential
  epoch matches the stored row, so a later revalidation does not reject the same run;
- a token that cannot be refreshed fails closed instead of sending an expired bearer.
"""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from lumen_mcp_default.remote import RemoteMcp, connection
from lumen_plugin_api.contracts import PluginError

URL = "https://mcp.example.com/mcp"


def _factory():
    return None


def test_streamable_http_and_sse_are_built_and_stdio_is_refused():
    assert connection({"url": URL}, _factory)["transport"] == "streamable_http"
    assert connection({"url": URL, "transport": "http"}, _factory)["transport"] == "streamable_http"
    sse = connection({"url": URL, "transport": "sse"}, _factory)
    assert sse["transport"] == "sse"
    assert sse["url"] == URL
    assert sse["sse_read_timeout"] > 0
    with pytest.raises(ValueError):
        connection({"url": URL, "transport": "stdio"}, _factory)


def test_sse_refuses_non_https_destinations():
    with pytest.raises(ValueError):
        connection({"url": "http://mcp.example.com/sse", "transport": "sse"}, _factory)


class _Transport:
    def __init__(self):
        self._http = SimpleNamespace(validate_url=lambda url: url)

    def _http_factory(self, headers=None, timeout=None, auth=None):
        return None


class _Extensions:
    def __init__(self, server):
        self._server = server

    async def resolve(self, kind, identifier, namespace):
        return dict(self._server)


class _Store:
    """Returns the row as it currently is, so a re-read after refresh sees the new token."""

    def __init__(self, credential, refreshed_token=None):
        self.credential = credential
        self._refreshed_token = refreshed_token

    async def connection(self, server_id, namespace):
        return self.credential


class _OAuth:
    def __init__(self, store, new_token=None, fail=False):
        self._store = store
        self._new_token = new_token
        self._fail = fail
        self.calls = []

    async def refresh(self, connection_id):
        self.calls.append(connection_id)
        if self._fail:
            return None
        self._store.credential = SimpleNamespace(
            **{
                **vars(self._store.credential),
                "token": {"access_token": self._new_token},
                "expires_at": datetime.now(UTC) + timedelta(hours=1),
            }
        )
        return {"access_token": self._new_token}


def _plugin(credential, *, new_token=None, fail=False):
    server = {"id": 9, "url": URL, "auth_mode": "oauth", "config_version": 3, "is_active": True, "transport": "http"}
    store = _Store(credential)
    oauth = _OAuth(store, new_token=new_token, fail=fail)
    plugin = RemoteMcp()
    plugin._extensions = _Extensions(server)
    plugin._connections = store
    plugin.transport = _Transport()
    plugin._oauth = oauth
    return plugin, oauth, store


def _credential(expires_in_seconds, *, epoch=4):
    return SimpleNamespace(
        id="conn-1",
        status="active",
        config_version=3,
        credential_epoch=epoch,
        token={"access_token": "old"},
        expires_at=datetime.now(UTC) + timedelta(seconds=expires_in_seconds),
    )


async def test_expiring_token_is_refreshed_at_use_and_the_epoch_matches_the_stored_row():
    plugin, oauth, store = _plugin(_credential(10), new_token="fresh")
    server, epoch = await plugin._server(9, SimpleNamespace(user_id="u", project_id="p"))
    assert oauth.calls == ["conn-1"]
    assert server["headers"] == {"Authorization": "Bearer fresh"}
    assert epoch == store.credential.credential_epoch


async def test_fresh_token_is_used_without_refresh():
    plugin, oauth, _ = _plugin(_credential(3600))
    server, _ = await plugin._server(9, SimpleNamespace(user_id="u", project_id="p"))
    assert oauth.calls == []
    assert server["headers"] == {"Authorization": "Bearer old"}


async def test_unrefreshable_token_fails_closed():
    plugin, _, _ = _plugin(_credential(10), fail=True)
    with pytest.raises(PluginError):
        await plugin._server(9, SimpleNamespace(user_id="u", project_id="p"))


def _adapter_tool_error(*texts: str):
    """Raise through the adapter's real isError=True conversion, not a hand-built exception."""
    from langchain_mcp_adapters.tools import _convert_call_tool_result
    from mcp.types import CallToolResult, TextContent

    result = CallToolResult(content=[TextContent(type="text", text=text) for text in texts], isError=True)
    try:
        _convert_call_tool_result(result)
    except Exception as exc:  # the adapter's ToolException subclass
        return exc
    raise AssertionError("isError=True must be converted into a tool exception")


def test_adapter_tool_error_exposes_code_field_message_and_retry_only():
    from lumen_mcp_default.remote import remote_failure

    exc = _adapter_tool_error(
        '{"error":{"code":"invalid_scan_id","field":"scanId","message":"Use the UUID.","retryable":false}}'
    )
    mapped = remote_failure(exc)
    assert mapped.code == "mcp_tool_error"
    assert str(mapped) == "원격 MCP 도구가 오류를 반환했습니다 [invalid_scan_id] (scanId): Use the UUID. (재시도 불가)"


def test_multi_block_adapter_error_is_joined_text_not_a_python_repr():
    from lumen_mcp_default.remote import remote_failure

    mapped = remote_failure(_adapter_tool_error("first line", "second block"))
    assert mapped.code == "mcp_tool_error"
    assert str(mapped) == "원격 MCP 도구가 오류를 반환했습니다: first line second block"


def test_tool_error_text_is_single_line_and_bounded():
    from lumen_mcp_default.remote import remote_failure

    mapped = remote_failure(_adapter_tool_error("line1\nline2" + "a" * 2000))
    assert "\n" not in str(mapped) and len(str(mapped)) < 700


def test_timeout_is_a_fixed_retryable_server_message():
    from lumen_mcp_default.remote import _TIMEOUT, remote_failure

    mapped = remote_failure(TimeoutError())
    assert mapped.code == "mcp_server_error"
    assert str(mapped) == f"The MCP server did not respond within {_TIMEOUT} seconds. Try again later."


def test_timeout_is_found_inside_an_exception_group():
    from lumen_mcp_default.remote import remote_failure

    assert remote_failure(ExceptionGroup("wrapped", [TimeoutError()])).code == "mcp_server_error"


def test_http_status_maps_to_rejection_or_status_text_without_the_url():
    import httpx
    from lumen_mcp_default.remote import remote_failure

    def status(code: int):
        request = httpx.Request("POST", "https://internal.example/mcp?token=secret")
        return httpx.HTTPStatusError("boom", request=request, response=httpx.Response(code, request=request))

    rejected = remote_failure(status(401))
    assert rejected.code == "mcp_server_error" and "rejected the connection's credentials" in str(rejected)
    server_error = remote_failure(status(500))
    assert str(server_error) == "The MCP server returned HTTP 500. Try again later."
    assert "internal.example" not in str(rejected) + str(server_error)


def test_unreachable_server_is_fixed_text_without_the_url():
    import httpx
    from lumen_mcp_default.remote import remote_failure

    mapped = remote_failure(httpx.ConnectError("refused https://internal.example/mcp"))
    assert mapped.code == "mcp_server_error" and "could not be reached" in str(mapped)
    assert "internal.example" not in str(mapped)


def test_unknown_failures_and_host_plugin_errors_are_not_reclassified():
    from lumen_mcp_default.remote import remote_failure

    assert remote_failure(ConnectionError("https://internal.example/secret")) is None
    assert remote_failure(PluginError("plugin_unavailable", "Public HTTP capability is required")) is None
