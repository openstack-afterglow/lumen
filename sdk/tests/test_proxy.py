from __future__ import annotations

import hashlib
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import MagicMock
from urllib.parse import urlsplit

import pytest
from keystoneauth1 import session, token_endpoint
from openstack.connection import Connection

from lumen_sdk import register
from lumen_sdk.proxy import Proxy
from lumen_sdk.service import LumenService


def _response(status_code=200, *, payload=None, text=""):
    content = text.encode() if text else (b"json" if payload is not None else b"")
    return SimpleNamespace(
        status_code=status_code,
        content=content,
        text=text,
        json=lambda: payload,
    )


# Every non-text, non-streaming route: (method_name, args, kwargs, http_method, path, body, params).
JSON_METHOD_TABLE = [
    # -- Conversations & Messages --
    ("conversations", (), {}, "GET", "/v1/conversations", None, None),
    ("conversations", (), {"limit": 10, "offset": 20}, "GET", "/v1/conversations", None, {"limit": 10, "offset": 20}),
    ("create_conversation", (), {"title": "Hello"}, "POST", "/v1/conversations", {"title": "Hello"}, None),
    ("search_conversations", (), {"q": "test"}, "GET", "/v1/conversations/search", None, {"q": "test"}),
    ("get_conversation", ("conv-1",), {}, "GET", "/v1/conversations/conv-1", None, None),
    ("delete_conversation", ("conv-1",), {}, "DELETE", "/v1/conversations/conv-1", None, None),
    (
        "set_conversation_workspace",
        ("conv-1",),
        {"workspace_id": 5},
        "PATCH",
        "/v1/conversations/conv-1/workspace",
        {"workspace_id": 5},
        None,
    ),
    ("list_message_page", ("conv-1",), {"anchor": "latest"}, "GET", "/v1/conversations/conv-1/messages", None, {"anchor": "latest"}),
    (
        "set_active_leaf",
        ("conv-1",),
        {"message_id": 12, "descend": True},
        "PATCH",
        "/v1/conversations/conv-1/active-leaf",
        {"message_id": 12, "descend": True},
        None,
    ),
    (
        "fork_conversation",
        ("conv-1",),
        {"message_id": 3},
        "POST",
        "/v1/conversations/conv-1/fork",
        {"message_id": 3},
        None,
    ),
    (
        "preview_conversation_context",
        ("conv-1",),
        {"model_id": 1},
        "POST",
        "/v1/conversations/conv-1/context-preview",
        {"model_id": 1},
        None,
    ),
    (
        "compact_conversation",
        ("conv-1",),
        {"model_id": 1, "expected_context_revision": "rev-1"},
        "POST",
        "/v1/conversations/conv-1/compactions",
        {"model_id": 1, "expected_context_revision": "rev-1"},
        None,
    ),
    (
        "preview_temp_context",
        ("temp-1",),
        {"model_id": 1},
        "POST",
        "/v1/temp-threads/temp-1/context-preview",
        {"model_id": 1},
        None,
    ),
    (
        "compact_temp_thread",
        ("temp-1",),
        {"model_id": 1, "expected_context_revision": "rev-1"},
        "POST",
        "/v1/temp-threads/temp-1/compactions",
        {"model_id": 1, "expected_context_revision": "rev-1"},
        None,
    ),
    # -- Completions & Runs --
    (
        "create_completion",
        ("conv-1",),
        {"model_id": 1, "parts": [{"type": "text", "text": "hello"}]},
        "POST",
        "/v1/conversations/conv-1/completions",
        {"model_id": 1, "parts": [{"type": "text", "text": "hello"}]},
        None,
    ),
    ("regenerate_message", ("conv-1", 2), {}, "POST", "/v1/conversations/conv-1/messages/2/regenerate", None, None),
    ("retry_run", ("conv-1", "run-1"), {}, "POST", "/v1/conversations/conv-1/runs/run-1/retry", None, None),
    (
        "temp_completion",
        (),
        {"model_id": 1, "parts": [{"type": "text", "text": "hello"}]},
        "POST",
        "/v1/temp-completions",
        {"model_id": 1, "parts": [{"type": "text", "text": "hello"}]},
        None,
    ),
    ("list_conversation_runs", ("conv-1",), {}, "GET", "/v1/conversations/conv-1/runs", None, None),
    ("get_temp_thread", ("temp-1",), {}, "GET", "/v1/temp-threads/temp-1", None, None),
    ("active_runs", (), {}, "GET", "/v1/runs", None, None),
    ("get_run", ("run-1",), {}, "GET", "/v1/runs/run-1", None, None),
    (
        "resolve_tool_approval",
        ("run-1", "call-1"),
        {"decision": "approved"},
        "POST",
        "/v1/runs/run-1/approvals/call-1",
        {"decision": "approved"},
        None,
    ),
    (
        "resolve_run_interaction",
        ("run-1", "inter-1"),
        {"response": {"option_ids": [1]}},
        "POST",
        "/v1/runs/run-1/interactions/inter-1",
        {"response": {"option_ids": [1]}},
        None,
    ),
    ("cancel_run", ("run-1",), {}, "POST", "/v1/runs/run-1/cancel", None, None),
    # -- Models & Capabilities --
    ("discovery", (), {}, "GET", "/v1/", None, None),
    ("models", (), {}, "GET", "/v1/models", None, None),
    ("chat_models", (), {}, "GET", "/v1/chat/models", None, None),
    ("capabilities", (), {"model_id": 1}, "GET", "/v1/capabilities", None, {"model_id": 1}),
    # -- Agents --
    ("agents", (), {}, "GET", "/v1/agents", None, None),
    ("create_agent", (), {"name": "Agent1"}, "POST", "/v1/agents", {"name": "Agent1"}, None),
    ("agent_hub", (), {"query": "coder"}, "GET", "/v1/agents/hub", None, {"query": "coder"}),
    ("get_agent", (1,), {}, "GET", "/v1/agents/1", None, None),
    ("update_agent", (1,), {"name": "Updated"}, "PATCH", "/v1/agents/1", {"name": "Updated"}, None),
    ("delete_agent", (1,), {}, "DELETE", "/v1/agents/1", None, None),
    ("clone_agent", (1,), {}, "POST", "/v1/agents/1/clone", None, None),
    # -- Workspaces --
    ("workspaces", (), {}, "GET", "/v1/workspaces", None, None),
    ("create_workspace", (), {"name": "WS1"}, "POST", "/v1/workspaces", {"name": "WS1"}, None),
    ("get_workspace", (1,), {}, "GET", "/v1/workspaces/1", None, None),
    ("update_workspace", (1,), {"name": "WS2"}, "PATCH", "/v1/workspaces/1", {"name": "WS2"}, None),
    ("delete_workspace", (1,), {}, "DELETE", "/v1/workspaces/1", None, None),
    # -- Memories --
    ("memories", (), {}, "GET", "/v1/memories", None, None),
    ("memory_document", (), {}, "GET", "/v1/memories/document", None, None),
    ("create_memory", (), {"content": "mem"}, "POST", "/v1/memories", {"content": "mem"}, None),
    ("search_memories", (), {"query": "mem"}, "POST", "/v1/memories/search", {"query": "mem"}, None),
    ("update_memory", (1,), {"content": "updated"}, "PATCH", "/v1/memories/1", {"content": "updated"}, None),
    ("delete_memory", (1,), {}, "DELETE", "/v1/memories/1", None, None),
    # -- Code Workspaces & Git Credentials --
    ("git_credentials", (), {}, "GET", "/v1/git-credentials", None, None),
    (
        "create_git_credential",
        (),
        {"host": "github.com", "username": "git", "token": "secret"},
        "POST",
        "/v1/git-credentials",
        {"host": "github.com", "username": "git", "token": "secret"},
        None,
    ),
    (
        "update_git_credential",
        (1,),
        {"username": "git", "token": "updated"},
        "PUT",
        "/v1/git-credentials/1",
        {"username": "git", "token": "updated"},
        None,
    ),
    ("delete_git_credential", (1,), {}, "DELETE", "/v1/git-credentials/1", None, None),
    ("code_workspaces", (), {}, "GET", "/v1/code-workspaces", None, None),
    ("create_code_workspace", (), {"name": "code"}, "POST", "/v1/code-workspaces", {"name": "code"}, None),
    ("get_code_workspace", ("cw-1",), {}, "GET", "/v1/code-workspaces/cw-1", None, None),
    ("delete_code_workspace", ("cw-1",), {}, "DELETE", "/v1/code-workspaces/cw-1", None, None),
    (
        "set_conversation_code_workspace",
        ("conv-1",),
        {"workspace_id": "cw-1"},
        "PUT",
        "/v1/conversations/conv-1/code-workspace",
        {"workspace_id": "cw-1"},
        None,
    ),
    # -- Assets --
    ("get_asset", ("asset-1",), {}, "GET", "/v1/assets/asset-1", None, None),
    ("delete_asset", ("asset-1",), {}, "DELETE", "/v1/assets/asset-1", None, None),
    # -- Extensions, MCP & Custom Tools --
    ("mcp_servers", (), {}, "GET", "/v1/mcp-servers", None, None),
    ("create_mcp_server", (), {"url": "http://mcp"}, "POST", "/v1/mcp-servers", {"url": "http://mcp"}, None),
    ("update_mcp_server", (1,), {"url": "http://mcp2"}, "PATCH", "/v1/mcp-servers/1", {"url": "http://mcp2"}, None),
    ("delete_mcp_server", (1,), {}, "DELETE", "/v1/mcp-servers/1", None, None),
    ("mcp_server_oauth_status", (1,), {}, "GET", "/v1/mcp-servers/1/oauth", None, None),
    ("start_mcp_server_oauth", (1,), {}, "POST", "/v1/mcp-servers/1/oauth/start", None, None),
    ("disconnect_mcp_server_oauth", (1,), {}, "DELETE", "/v1/mcp-servers/1/oauth", None, None),
    ("custom_tools", (), {}, "GET", "/v1/custom-tools", None, None),
    ("create_custom_tool", (), {"name": "t1"}, "POST", "/v1/custom-tools", {"name": "t1"}, None),
    ("update_custom_tool", (1,), {"name": "t2"}, "PATCH", "/v1/custom-tools/1", {"name": "t2"}, None),
    ("delete_custom_tool", (1,), {}, "DELETE", "/v1/custom-tools/1", None, None),
    ("skills", (), {}, "GET", "/v1/skills", None, None),
    ("create_skill", (), {"name": "s1"}, "POST", "/v1/skills", {"name": "s1"}, None),
    ("update_skill", (1,), {"name": "s2"}, "PATCH", "/v1/skills/1", {"name": "s2"}, None),
    ("delete_skill", (1,), {}, "DELETE", "/v1/skills/1", None, None),
    ("admin_mcp_servers", (), {}, "GET", "/v1/admin/mcp-servers", None, None),
    ("admin_create_mcp_server", (), {"name": "m1"}, "POST", "/v1/admin/mcp-servers", {"name": "m1"}, None),
    ("admin_update_mcp_server", (1,), {"name": "m2"}, "PATCH", "/v1/admin/mcp-servers/1", {"name": "m2"}, None),
    ("admin_delete_mcp_server", (1,), {}, "DELETE", "/v1/admin/mcp-servers/1", None, None),
    ("admin_custom_tools", (), {}, "GET", "/v1/admin/custom-tools", None, None),
    ("admin_create_custom_tool", (), {"name": "ct1"}, "POST", "/v1/admin/custom-tools", {"name": "ct1"}, None),
    ("admin_update_custom_tool", (1,), {"name": "ct2"}, "PATCH", "/v1/admin/custom-tools/1", {"name": "ct2"}, None),
    ("admin_delete_custom_tool", (1,), {}, "DELETE", "/v1/admin/custom-tools/1", None, None),
    ("admin_skills", (), {}, "GET", "/v1/admin/skills", None, None),
    ("admin_create_skill", (), {"name": "sk1"}, "POST", "/v1/admin/skills", {"name": "sk1"}, None),
    ("admin_update_skill", (1,), {"name": "sk2"}, "PATCH", "/v1/admin/skills/1", {"name": "sk2"}, None),
    ("admin_delete_skill", (1,), {}, "DELETE", "/v1/admin/skills/1", None, None),
    # -- Usage & API Keys --
    ("usage", (), {}, "GET", "/v1/usage", None, None),
    ("usage_timeseries", (), {"bucket": "day"}, "GET", "/v1/usage/timeseries", None, {"bucket": "day"}),
    ("usage_keys", (), {"range": "all"}, "GET", "/v1/usage/keys", None, {"range": "all"}),
    (
        "usage_records",
        (),
        {"limit": 10, "before_id": 20},
        "GET",
        "/v1/usage/records",
        None,
        {"limit": 10, "before_id": 20},
    ),
    ("api_keys", (), {}, "GET", "/v1/api-keys", None, None),
    ("create_api_key", (), {"name": "key1"}, "POST", "/v1/api-keys", {"name": "key1"}, None),
    ("revoke_api_key", (1,), {}, "DELETE", "/v1/api-keys/1", None, None),
    # -- Admin Providers, Models & Stats --
    ("admin_providers", (), {}, "GET", "/v1/admin/providers", None, None),
    ("admin_create_provider", (), {"name": "p1"}, "POST", "/v1/admin/providers", {"name": "p1"}, None),
    ("admin_update_provider", (1,), {"name": "p2"}, "PATCH", "/v1/admin/providers/1", {"name": "p2"}, None),
    ("admin_delete_provider", (1,), {}, "DELETE", "/v1/admin/providers/1", None, None),
    ("admin_provider_available_models", (1,), {}, "GET", "/v1/admin/providers/1/available-models", None, None),
    ("admin_provider_accounts", (1,), {}, "GET", "/v1/admin/providers/1/accounts", None, None),
    ("admin_create_provider_account", (1,), {"name": "a"}, "POST", "/v1/admin/providers/1/accounts", {"name": "a"}, None),
    ("admin_update_provider_account", (1, 2), {"weight": 3}, "PATCH", "/v1/admin/providers/1/accounts/2", {"weight": 3}, None),
    ("admin_delete_provider_account", (1, 2), {}, "DELETE", "/v1/admin/providers/1/accounts/2", None, None),
    ("admin_provider_account_usage", (1, 2), {}, "GET", "/v1/admin/providers/1/accounts/2/usage", None, None),
    ("admin_create_provider_credential", (1, 2), {"name": "c"}, "POST", "/v1/admin/providers/1/accounts/2/credentials", {"name": "c"}, None),
    ("admin_update_provider_credential", (1, 2, 3), {"priority": 1}, "PATCH", "/v1/admin/providers/1/accounts/2/credentials/3", {"priority": 1}, None),
    ("admin_delete_provider_credential", (1, 2, 3), {}, "DELETE", "/v1/admin/providers/1/accounts/2/credentials/3", None, None),
    ("admin_set_provider_credential_token", (1, 2, 3), {"token": "t"}, "PUT", "/v1/admin/providers/1/accounts/2/credentials/3/auth/token", {"token": "t"}, None),
    ("admin_disconnect_provider_credential", (1, 2, 3), {}, "DELETE", "/v1/admin/providers/1/accounts/2/credentials/3/auth", None, None),
    ("admin_begin_provider_credential_device_auth", (1, 2, 3), {}, "POST", "/v1/admin/providers/1/accounts/2/credentials/3/auth/device", None, None),
    ("admin_poll_provider_credential_device_auth", (1, 2, 3, "att"), {}, "POST", "/v1/admin/providers/1/accounts/2/credentials/3/auth/device/att/poll", None, None),
    ("admin_cancel_provider_credential_device_auth", (1, 2, 3, "att"), {}, "DELETE", "/v1/admin/providers/1/accounts/2/credentials/3/auth/device/att", None, None),
    ("admin_models", (), {"active_only": True}, "GET", "/v1/admin/models", None, {"active_only": True}),
    ("admin_create_model", (), {"name": "m1"}, "POST", "/v1/admin/models", {"name": "m1"}, None),
    ("admin_update_model", (1,), {"name": "m2"}, "PATCH", "/v1/admin/models/1", {"name": "m2"}, None),
    ("admin_delete_model", (1,), {}, "DELETE", "/v1/admin/models/1", None, None),
    (
        "admin_models_dev_providers",
        (),
        {"local_provider_id": 1},
        "GET",
        "/v1/admin/models/pricing/models-dev/providers",
        None,
        {"local_provider_id": 1},
    ),
    (
        "admin_get_models_dev_provider",
        ("prov-1",),
        {"refresh": True},
        "GET",
        "/v1/admin/models/pricing/models-dev/providers/prov-1",
        None,
        {"refresh": True},
    ),
    (
        "admin_import_models_dev_prices",
        (),
        {"provider_id": "p1"},
        "POST",
        "/v1/admin/models/pricing/models-dev/import",
        {"provider_id": "p1"},
        None,
    ),
    ("admin_set_title_model", (), {"model_id": 1}, "PUT", "/v1/admin/title-model", {"model_id": 1}, None),
    ("admin_set_memory_model", (), {"model_id": 2}, "PUT", "/v1/admin/memory-model", {"model_id": 2}, None),
    ("admin_stats", (), {"range": "all"}, "GET", "/v1/admin/stats", None, {"range": "all"}),
    ("admin_stats_timeseries", (), {"bucket": "day"}, "GET", "/v1/admin/stats/timeseries", None, {"bucket": "day"}),
    ("admin_stats_users", (), {"range": "all"}, "GET", "/v1/admin/stats/users", None, {"range": "all"}),
    ("openai_chat_completions", (), {"model": "gpt-4"}, "POST", "/v1/chat/completions", {"model": "gpt-4"}, None),
    ("anthropic_messages", (), {"model": "claude-3"}, "POST", "/v1/messages", {"model": "claude-3"}, None),
]

STREAM_METHOD_TABLE = [
    (
        "run_events",
        ("run-1",),
        {"after_seq": 5, "last_event_id": "evt-10"},
        "GET",
        "/v1/runs/run-1/events",
        None,
        {"after_seq": 5},
        {"Last-Event-ID": "evt-10"},
    ),
    (
        "openai_chat_completions",
        (),
        {"stream": True, "model": "gpt-4"},
        "POST",
        "/v1/chat/completions",
        {"stream": True, "model": "gpt-4"},
        None,
        None,
    ),
    (
        "anthropic_messages",
        (),
        {"stream": True, "model": "claude-3"},
        "POST",
        "/v1/messages",
        {"stream": True, "model": "claude-3"},
        None,
        None,
    ),
]
OTHER_METHOD_TABLE = [
    ("upload_asset", (), {"files": {"file": ("a.txt", b"data")}}, "POST", "/v1/assets"),
    ("download_asset", ("asset-1",), {}, "GET", "/v1/assets/asset-1/download"),
]



@pytest.mark.parametrize("method_name,args,kwargs,http_method,path,body,params", JSON_METHOD_TABLE)
def test_json_methods_issue_expected_request(method_name, args, kwargs, http_method, path, body, params):
    proxy = Proxy(session=MagicMock(), service_type="lumen")
    status = 204 if http_method == "DELETE" else 200
    response = _response(status, payload={"ok": True}) if status != 204 else _response(204)
    proxy.request = MagicMock(return_value=response)

    result = getattr(proxy, method_name)(*args, **kwargs)

    expected_kwargs = {"raise_exc": True}
    if body is not None:
        expected_kwargs["json"] = body
    if params is not None:
        expected_kwargs["params"] = params
    proxy.request.assert_called_once_with(path, http_method, **expected_kwargs)
    assert result == ({"ok": True} if status != 204 else None)


def test_upload_asset_sends_files_multipart():
    proxy = Proxy(session=MagicMock(), service_type="lumen")
    proxy.request = MagicMock(return_value=_response(200, payload={"id": "asset-1"}))

    files = {"file": ("a.txt", b"hello", "text/plain")}
    res = proxy.upload_asset(files=files)

    proxy.request.assert_called_once_with("/v1/assets", "POST", files=files, raise_exc=True)
    assert res == {"id": "asset-1"}


def test_download_asset_returns_same_origin_streamed_content():
    proxy = Proxy(session=MagicMock(), service_type="lumen")
    content_resp = SimpleNamespace(status_code=200, content=b"asset-bytes", headers={})
    proxy.request = MagicMock(return_value=content_resp)

    content = proxy.download_asset("asset-1")

    proxy.request.assert_called_once_with("/v1/assets/asset-1/download", "GET", raise_exc=True)
    assert content == b"asset-bytes"


@pytest.mark.parametrize("method_name,args,kwargs,http_method,path,body,params,headers", STREAM_METHOD_TABLE)
def test_stream_methods_request_without_buffering(method_name, args, kwargs, http_method, path, body, params, headers):
    proxy = Proxy(session=MagicMock(), service_type="lumen")
    lines = ['data: {"step": 1}', 'data: {"step": 2}']
    response = SimpleNamespace(iter_lines=MagicMock(return_value=iter(lines)))
    proxy.request = MagicMock(return_value=response)

    result = getattr(proxy, method_name)(*args, **kwargs)

    expected_kwargs = {"raise_exc": True, "stream": True}
    if body is not None:
        expected_kwargs["json"] = body
    if params is not None:
        expected_kwargs["params"] = params
    if headers is not None:
        expected_kwargs["headers"] = headers
    proxy.request.assert_called_once_with(path, http_method, **expected_kwargs)
    response.iter_lines.assert_called_once_with(decode_unicode=True)
    assert list(result) == lines


def test_run_events_resume_parameters():
    proxy = Proxy(session=MagicMock(), service_type="lumen")
    lines = ['data: {"step": 1}']
    response = SimpleNamespace(iter_lines=MagicMock(return_value=iter(lines)))
    proxy.request = MagicMock(return_value=response)

    result = proxy.run_events("run-1", after_seq=10, last_event_id="evt-100")
    proxy.request.assert_called_with(
        "/v1/runs/run-1/events",
        "GET",
        params={"after_seq": 10},
        headers={"Last-Event-ID": "evt-100"},
        raise_exc=True,
        stream=True,
    )
    assert list(result) == lines


def test_identifiers_are_escaped_as_single_path_segments():
    proxy = Proxy(session=MagicMock(), service_type="lumen")
    proxy.request = MagicMock(return_value=_response(200, payload={"ok": True}))

    proxy.get_conversation("conv/../../other")

    proxy.request.assert_called_once_with(
        "/v1/conversations/conv%2F..%2F..%2Fother",
        "GET",
        raise_exc=True,
    )


def test_completion_and_code_workspace_idempotency_headers():
    proxy = Proxy(session=MagicMock(), service_type="lumen")
    proxy.request = MagicMock(return_value=_response(200, payload={"ok": True}))

    proxy.create_completion("conv-1", idempotency_key="ik-1", model="gpt-4")
    proxy.request.assert_called_with(
        "/v1/conversations/conv-1/completions",
        "POST",
        json={"model": "gpt-4"},
        headers={"Idempotency-Key": "ik-1"},
        raise_exc=True,
    )

    proxy.create_code_workspace(idempotency_key="ik-2", name="workspace")
    proxy.request.assert_called_with(
        "/v1/code-workspaces",
        "POST",
        json={"name": "workspace"},
        headers={"Idempotency-Key": "ik-2"},
        raise_exc=True,
    )


def test_service_description_constructs_with_required_service_type():
    description = LumenService()
    assert description.service_type == "lumen"
    assert description.supported_versions == {"1": Proxy}


def test_register_enables_non_official_service_before_attaching_proxy():
    """Regression guard: 'lumen' is not an os-service-types-official type.

    Without an explicit conn.config.enable_service("lumen") call before
    conn.add_service(), CloudRegion.has_service() falls back to
    os_service_types.is_official("lumen") (always False for this service),
    every conn.lumen.* attribute access would raise ServiceDisabledException,
    and add_service() would never be reachable with a "known" service.
    """

    class Config:
        def __init__(self):
            self.enabled = set()

        def enable_service(self, service_type):
            self.enabled.add(service_type)

        def has_service(self, service_type):
            return service_type in self.enabled

    class Connection:
        def __init__(self):
            self.config = Config()
            self.lumen = None

        def add_service(self, service):
            assert self.config.has_service(service.service_type)
            self.lumen = Proxy(session=MagicMock(), service_type=service.service_type)

    connection = Connection()
    assert not hasattr(connection.config, "has_lumen")

    proxy = register(connection)

    assert isinstance(proxy, Proxy)
    assert connection.config.has_service("lumen")


# -- Real OpenStack adapter discovery: the facade /v1 must not be duplicated --

@pytest.fixture
def native_accounts():
    state = {"account": None, "credential": None, "key_digest": None}

    class NativeBoundary(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def respond(self, status, payload=None):
            raw = json.dumps(payload).encode() if payload is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            self.dispatch()

        def do_POST(self):
            self.dispatch()

        def do_PATCH(self):
            self.dispatch()

        def do_DELETE(self):
            self.dispatch()

        def dispatch(self):
            path = urlsplit(self.path).path.rstrip("/")
            version = {"id": "v1.0", "status": "CURRENT", "links": [{"rel": "self", "href": base + "/v1/"}]}
            if self.command == "GET" and path in ("", "/v1"):
                self.respond(200, {"versions": [version]} if path == "" else {"version": version})
                return
            if self.headers.get("X-Auth-Token") != "sdk-fixture-token":
                self.respond(401, {"error": "authentication required"})
                return
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            accounts = "/v1/admin/providers/7/accounts"
            credential = accounts + "/41/credentials/51"
            if path == accounts and self.command == "POST":
                if not isinstance(body.get("name"), str):
                    self.respond(422, {"error": "name required"})
                    return
                state["account"] = {"id": 41, "provider_id": 7, "name": body["name"]}
                self.respond(201, {**state["account"], "credentials": []})
            elif path == accounts and self.command == "GET":
                self.respond(200, [{**state["account"], "credentials": [state["credential"]] if state["credential"] else []}]
                             if state["account"] else [])
            elif path == accounts + "/41/credentials" and self.command == "POST":
                if not isinstance(body.get("api_key"), str) or not body.get("name"):
                    self.respond(422, {"error": "credential required"})
                    return
                state["key_digest"] = hashlib.sha256(body["api_key"].encode()).hexdigest()
                state["credential"] = {"id": 51, "account_id": 41, "name": body["name"],
                                       "generation": 0, "api_key_configured": True, "api_key_source": "database"}
                self.respond(201, state["credential"])
            elif path == credential and self.command == "PATCH":
                if not isinstance(body.get("api_key"), str):
                    self.respond(422, {"error": "rotation key required"})
                    return
                state["key_digest"] = hashlib.sha256(body["api_key"].encode()).hexdigest()
                state["credential"]["generation"] += 1
                self.respond(200, state["credential"])
            elif path == credential and self.command == "DELETE":
                state["credential"] = None
                self.respond(204)
            elif path == accounts + "/41/usage" and self.command == "GET":
                self.respond(200, {"account_id": 41, "raw_cost": "0.1250000000", "events": 3})
            elif path == accounts + "/41" and self.command == "DELETE":
                state["account"] = None
                self.respond(204)
            else:
                self.respond(404, {"error": "unknown native resource"})

    server = ThreadingHTTPServer(("127.0.0.1", 0), NativeBoundary)
    base = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield base, state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


@pytest.mark.parametrize("endpoint_suffix", ["", "/v1"])
def test_discovered_proxy_preserves_account_lifecycle_without_duplicating_version(native_accounts, endpoint_suffix):
    base, state = native_accounts
    auth = token_endpoint.Token(base, "sdk-fixture-token")
    conn = Connection(session=session.Session(auth=auth), lumen_endpoint_override=base + endpoint_suffix,
                      lumen_api_version="1")
    try:
        api = register(conn)
        account = api.admin_create_provider_account(7, name="SDK account")
        credential = api.admin_create_provider_credential(7, account["id"], name="primary", api_key="first-synthetic-key")
        assert credential["api_key_configured"] and credential["generation"] == 0
        rotated = api.admin_update_provider_credential(7, account["id"], credential["id"], api_key="rotated-synthetic-key")
        assert rotated["generation"] == 1
        assert state["key_digest"] == hashlib.sha256(b"rotated-synthetic-key").hexdigest()
        saved = api.admin_provider_accounts(7)
        assert saved[0]["credentials"][0]["generation"] == 1
        assert "synthetic-key" not in json.dumps(saved)
        assert api.admin_provider_account_usage(7, account["id"])["raw_cost"] == "0.1250000000"
        assert api.admin_delete_provider_credential(7, account["id"], credential["id"]) is None
        assert api.admin_provider_accounts(7)[0]["credentials"] == []
        assert api.admin_delete_provider_account(7, account["id"]) is None
        assert api.admin_provider_accounts(7) == []
    finally:
        conn.close()
