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
