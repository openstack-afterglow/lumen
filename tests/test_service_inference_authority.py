"""Synthetic HTTP and pre-I/O fences; no Keystone, provider, DB, or worker credentials."""
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from lumen import auth
from lumen.api import audio, images
from lumen.api.compat import anthropic, openai, responses
from lumen.auth import ensure_scopes, get_principal
from lumen.models.chat_contracts import ChatFeatureOptions, ChatRunDescriptor, ToolPolicy
from lumen.services import api_key_store, chat_admission, completion_api, inference_authority
from lumen.services.durable_runs import execution
from lumen.services.durable_runs.errors import DurableRunInputError


def principal(*roles, key=False):
    return {"user_id": "owner", "project_id": "project", "roles": ["member", *roles],
            "auth_type": "api_key" if key else "keystone", "api_key_id": 7 if key else None,
            "source": "api" if key else "web", "is_system_admin": False,
            "scopes": ("compat:completions:write", "native:tools:execute", "compat:images:write",
                       "compat:audio:write", "native:images:write", "native:audio:write")}


@pytest.mark.parametrize(("path", "body"), [
    ("/chat/images/generations", {"model_id": "image", "prompt": "dot"}),
    ("/chat/audio/speech", {"model_id": "voice", "input": "hi", "voice": "alloy"}),
])
def test_chat_only_denies_direct_native_media(monkeypatch, path, body):
    app = FastAPI()
    app.include_router(images.router)
    app.include_router(audio.router)
    app.dependency_overrides[get_principal] = lambda: principal("lumen-chat_user")
    image_admit = AsyncMock(side_effect=AssertionError("denied request reached media admission"))
    audio_admit = AsyncMock(side_effect=AssertionError("denied request reached media admission"))
    monkeypatch.setattr(images, "admit", image_admit)
    monkeypatch.setattr(audio, "admit", audio_admit)
    with TestClient(app) as client:
        response = client.post(path, json=body, headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 403
    image_admit.assert_not_awaited()
    audio_admit.assert_not_awaited()


def test_independent_images_leaf_admits_without_chat_or_editor(monkeypatch):
    app = FastAPI()
    app.include_router(images.router)
    app.dependency_overrides[get_principal] = lambda: principal("lumen-images_user")
    admitted = AsyncMock(return_value=ChatRunDescriptor(
        run_id=str(uuid4()), status="queued", events_url="/runs/image/events", cancel_url="/runs/image/cancel"))
    monkeypatch.setattr(images, "admit", admitted)
    with TestClient(app) as client:
        response = client.post("/chat/images/generations", json={"model_id": "image", "prompt": "dot"},
                               headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 202
    admitted.assert_awaited_once()


@pytest.mark.parametrize(("router", "path", "body"), [
    (openai.router, "/chat/completions", {"model": "model", "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "f", "parameters": {"type": "object"}}}]}),
    (responses.router, "/responses", {"model": "model", "input": "hi", "tools": [{"type": "web_search"}]}),
    (anthropic.router, "/messages", {"model": "model", "max_tokens": 20,
        "messages": [{"role": "user", "content": "hi"}], "tools": [{"type": "web_search_20250305", "name": "web_search"}]}),
])
def test_chat_only_denies_compat_tools_before_resolution(monkeypatch, router, path, body):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_principal] = lambda: principal("lumen-chat_user", key=True)
    resolve = AsyncMock(side_effect=AssertionError("tool-denied request resolved provider"))
    monkeypatch.setattr(completion_api, "resolve_api", resolve)
    with TestClient(app) as client:
        response = client.post(path, json=body)
    assert response.status_code == 403
    resolve.assert_not_awaited()


@pytest.mark.parametrize("selection", ["provider_search", "builtin", "stored_tool", "stored_mcp", "plugin", "skill", "agent"])
def test_all_native_selected_tools_require_independent_tools_leaf(selection):
    features = ChatFeatureOptions(memory=False, tool_policy=ToolPolicy(mode="none"))
    extensions = {"tools": [], "mcp": []}
    kwargs = dict(parts=[], execution_mode="chat", skill_ids=[], plugin_tool_snapshots=[],
                  plugin_skill_snapshots=[], agent=None, extension_selection=extensions)
    if selection == "provider_search":
        features.web_search.enabled = True
    elif selection == "builtin":
        features.tool_policy.mode = "agent_default"
    elif selection == "stored_tool":
        extensions["tools"] = [{"id": 1}]
    elif selection == "stored_mcp":
        extensions["mcp"] = [{"id": 1}]
    elif selection == "plugin":
        kwargs["plugin_tool_snapshots"] = [{"id": "bound"}]
    elif selection == "skill":
        kwargs["skill_ids"] = [1]
    else:
        kwargs["agent"] = {"id": 1}
    with pytest.raises(HTTPException) as error:
        chat_admission._require_native_admission_scopes(principal("lumen-chat_user", "lumen-inventory_reader"), features, **kwargs)
    assert error.value.status_code == 403
    assert "native:tools:execute" in error.value.detail


def test_intrinsic_provider_search_requires_tools_without_explicit_options():
    resolved = {"capabilities": {"web_search_required": True}}
    scopes = inference_authority.completion_scopes({}, resolved=resolved)
    assert scopes == ("compat:completions:write", "native:tools:execute")
    features = ChatFeatureOptions(memory=False, tool_policy=ToolPolicy(mode="none"))
    kwargs = dict(parts=[], execution_mode="chat", skill_ids=[], plugin_tool_snapshots=[],
                  plugin_skill_snapshots=[], agent=None, extension_selection={"tools": [], "mcp": []}, resolved=resolved)
    with pytest.raises(HTTPException) as error:
        chat_admission._require_native_admission_scopes(principal("lumen-chat_user"), features, **kwargs)
    assert error.value.status_code == 403
    chat_admission._require_native_admission_scopes(principal("lumen-chat_user", "lumen-tools_user"), features, **kwargs)
    assert inference_authority.completion_scopes({}, resolved={"capabilities": {"web_search": True}}) == ("compat:completions:write",)


async def test_direct_intrinsic_search_fence_passes_independent_tools_scope(monkeypatch):
    authorize = AsyncMock()
    monkeypatch.setattr(completion_api, "authorize_new_io", authorize)
    await completion_api._authorize_completion("owner", "project", 7, {}, {"capabilities": {"web_search_required": True}})
    assert "native:tools:execute" in authorize.await_args.kwargs["required_scopes"]


def test_plain_text_and_automatic_memory_do_not_require_history_editor():
    features = ChatFeatureOptions(memory=True, tool_policy=ToolPolicy(mode="none"))
    chat_admission._require_native_admission_scopes(principal("lumen-chat_user", "lumen-history_reader"), features, parts=[], execution_mode="chat",
        skill_ids=[], plugin_tool_snapshots=[], plugin_skill_snapshots=[], agent=None,
        extension_selection={"tools": [], "mcp": []})
    scopes = inference_authority.native_run_scopes({"features": features.model_dump()})
    assert "native:memory:write" not in scopes
    assert "native:tools:execute" not in scopes


@pytest.mark.parametrize(("modalities", "scope"), [(["image"], "compat:images:write"), (["audio"], "compat:audio:write")])
def test_compat_generated_modalities_are_independent(modalities, scope):
    scopes = inference_authority.completion_scopes({"modalities": modalities})
    assert scope in scopes
    with pytest.raises(HTTPException):
        ensure_scopes(principal("lumen-chat_user", key=True), *scopes)


class Session:
    def __init__(self, value):
        self.value = value
    async def __aenter__(self):
        return self
    async def __aexit__(self, *args):
        return False
    def begin(self):
        return self
    async def execute(self, *args, **kwargs):
        return SimpleNamespace(scalar_one=lambda: self.value, scalar_one_or_none=lambda: self.value)


@pytest.mark.parametrize("api_key_id", [None, 7])
@pytest.mark.parametrize("roles", [["member", "lumen-chat_user"], []])
async def test_downgraded_owner_denied_before_new_io(monkeypatch, api_key_id, roles):
    authority = AsyncMock(return_value={"roles": roles, "is_system_admin": False})
    monkeypatch.setattr(auth, "resolve_project_authority", authority)
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.authorize_api_key_in_transaction(Session(None), user_id="owner", project_id="project",
            api_key_id=api_key_id, required_scopes=("native:tools:execute",))


async def test_revoked_key_denied_before_new_io(monkeypatch):
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(return_value={
        "roles": ["member", "lumen-tools_user"], "is_system_admin": False}))
    key = SimpleNamespace(owner_user_id="owner", owner_project_id="project", is_active=False,
                          revoked_at=None, expires_at=None)
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await api_key_store.authorize_api_key_in_transaction(Session(key), user_id="owner", project_id="project",
            api_key_id=7, required_scopes=("native:tools:execute",))


@pytest.mark.parametrize("state", ["prepared", "completed", "provider_started"])
async def test_durable_authority_only_checked_for_new_io(monkeypatch, state):
    run = SimpleNamespace(id="run", user_id="owner", project_id="project", api_key_id=None, agent_id=None,
                          capability_snapshot={})
    segment = SimpleNamespace(status=state, result_payload="{}", usage_payload="{}")
    monkeypatch.setattr(execution, "_factory", lambda: lambda: Session(run))
    monkeypatch.setattr(execution, "_require_owned_running_lease", lambda *args: None)
    monkeypatch.setattr(execution, "prepare_segment", AsyncMock(return_value=segment))
    monkeypatch.setattr(execution, "_payload", lambda _: {"features": {"tool_policy": {"mode": "none"}}})
    monkeypatch.setattr(execution, "load_segment_payload", lambda _: {})
    monkeypatch.setattr(execution, "fail_unresolved_segment", lambda *args, **kwargs: None)
    authorization = AsyncMock(side_effect=api_key_store.ApiKeyForbidden("downgraded"))
    monkeypatch.setattr(execution, "authorize_api_key_in_transaction", authorization)
    hooks = execution._DurableExecutionHooks(run_id="run", owner="worker-authority")
    if state == "prepared":
        with pytest.raises(DurableRunInputError, match="inference_authority_revoked"):
            await hooks._start(segment_id="tool:1", ordinal=1, endpoint="tool", turn_ordinal=1, call_id="call")
        authorization.assert_awaited_once()
        assert "native:tools:execute" in authorization.await_args.kwargs["required_scopes"]
    else:
        await hooks._start(segment_id="tool:1", ordinal=1, endpoint="tool", turn_ordinal=1, call_id="call")
        authorization.assert_not_awaited()


async def test_stateless_provider_denied_before_new_call(monkeypatch):
    monkeypatch.setattr(completion_api, "billing_route", lambda route, *args, **kwargs: route)
    monkeypatch.setattr(inference_authority, "get_session_factory", lambda: lambda: Session(None))
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(return_value={
        "roles": ["member", "lumen-chat_user"], "is_system_admin": False}))
    provider = AsyncMock(side_effect=AssertionError("provider invoked after downgrade"))
    monkeypatch.setattr(completion_api, "invoke_chat_once", provider)
    with pytest.raises(completion_api.CompletionError) as error:
        await completion_api.complete_once(resolved={}, messages=[], user_id="owner", project_id="project",
            api_key_id=None, max_tokens=10, temperature=None, tools=[{"type": "function"}])
    assert error.value.status_code == 403
    provider.assert_not_awaited()


def test_independent_audio_leaf_admits_without_chat_or_editor(monkeypatch):
    from fastapi.responses import Response
    app = FastAPI()
    app.include_router(audio.router)
    app.dependency_overrides[get_principal] = lambda: principal("lumen-audio_user")
    admitted = AsyncMock(return_value="run")
    monkeypatch.setattr(audio, "admit", admitted)
    monkeypatch.setattr(audio, "completed_audio", AsyncMock(return_value={"kind": "tts"}))
    monkeypatch.setattr(audio, "speech_stream", AsyncMock(return_value=Response(content=b"synthetic", media_type="audio/mpeg")))
    with TestClient(app) as client:
        response = client.post("/chat/audio/speech", json={"model_id": "voice", "input": "hi", "voice": "alloy"},
                               headers={"Idempotency-Key": str(uuid4())})
    assert response.status_code == 200
    admitted.assert_awaited_once()


async def test_non_durable_tool_dispatch_revalidates_current_tools_cap(monkeypatch):
    from lumen.services.tool_runtime import dispatch
    from lumen.services.tools import ToolContext
    monkeypatch.setattr(inference_authority, "get_session_factory", lambda: lambda: Session(None))
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(return_value={
        "roles": ["member", "lumen-chat_user"], "is_system_admin": False}))
    provider = AsyncMock(side_effect=AssertionError("MCP I/O started after downgrade"))
    monkeypatch.setattr(dispatch.mcp_client, "call_tool", provider)
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await dispatch.context_execute_result("mcp__1__search", {}, ToolContext(user_id="owner", project_id="project"))
    provider.assert_not_awaited()


async def test_authority_outage_is_explicit_and_pre_io(monkeypatch):
    monkeypatch.setattr(inference_authority, "get_session_factory", lambda: lambda: Session(None))
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(side_effect=
        HTTPException(status_code=503, detail="directory unavailable")))
    with pytest.raises(completion_api.CompletionError) as error:
        await completion_api._authorize_completion("owner", "project", None, {}, {})
    assert error.value.status_code == 503


def test_batch_and_legacy_virtual_run_freeze_actual_tools_and_media_only():
    from lumen.services import batches
    scopes = batches._scopes("responses", "native", {"tools": [{"type": "web_search"}]})
    assert "native:tools:execute" in scopes
    assert "native:images:write" not in scopes and "native:audio:write" not in scopes
    virtual = inference_authority.native_run_scopes({"required_scopes": ["compat:completions:write"],
        "features": {"tool_policy": {"mode": "none"}, "memory": False}})
    assert virtual == ("compat:completions:write",)


@pytest.mark.parametrize(("family", "path", "body"), [
    ("images", "/images/generations", {"model": "image", "prompt": "dot"}),
    ("audio", "/audio/speech", {"model": "voice", "input": "hi", "voice": "alloy"}),
])
def test_chat_only_denies_compat_media_with_broad_key_scopes(family, path, body):
    from lumen.api.compat import audio as compat_audio
    from lumen.api.compat import images as compat_images
    app = FastAPI()
    app.include_router(compat_images.router if family == "images" else compat_audio.router)
    app.dependency_overrides[get_principal] = lambda: principal("lumen-chat_user", key=True)
    with TestClient(app) as client:
        response = client.post(path, json=body)
    assert response.status_code == 403


async def test_compat_realtime_checks_owner_audio_not_only_key_scope(monkeypatch):
    from lumen.api.compat import realtime
    monkeypatch.setattr(realtime, "get_settings", lambda: SimpleNamespace(chat_api_hosts=""))
    monkeypatch.setattr(realtime, "origin_allowed", lambda _: True)
    monkeypatch.setattr(realtime.api_key_store, "verify_key", AsyncMock(return_value={
        **principal("lumen-chat_user", key=True), "scopes": ["compat:realtime:write"]}))
    websocket = SimpleNamespace(headers={"authorization": "Bearer sk-afgl-synthetic"})
    assert await realtime._principal(websocket) is None


def test_history_continuation_requires_read_but_fresh_chat_does_not():
    request = {"features": {"memory": False, "tool_policy": {"mode": "none"}}}
    ensure_scopes(principal("lumen-chat_user"), *inference_authority.native_run_scopes(request))
    request["context_source"] = {"messages": [{"role": "user", "content": "stored private history"}]}
    with pytest.raises(HTTPException):
        ensure_scopes(principal("lumen-chat_user"), *inference_authority.native_run_scopes(request))


@pytest.mark.parametrize("cause", ["downgraded", "revoked"])
async def test_automatic_title_fence_checks_root_owner_and_key_before_new_io(monkeypatch, cause):
    from lumen.models.chat_jobs import ChatJob
    from lumen.services import title_jobs
    job = SimpleNamespace(status="running", lease_owner="worker", run_id="run", conversation_id="conv", progress={})
    run = SimpleNamespace(user_id="owner", project_id="project", api_key_id=7 if cause == "revoked" else None,
                          source="web", run_scope="persistent", conversation_id="conv")
    class JobSession(Session):
        async def get(self, model, key, **kwargs):
            return job if model is ChatJob else run
    session = JobSession(SimpleNamespace(owner_user_id="owner", owner_project_id="project", is_active=False,
                                        revoked_at=None, expires_at=None))
    monkeypatch.setattr("lumen.db.get_session_factory", lambda: lambda: session)
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(return_value={
        "roles": ["member", "lumen-history_reader"] if cause == "downgraded" else ["member", "lumen-chat_user", "lumen-history_reader"],
        "is_system_admin": False}))
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await title_jobs._mark_provider_started.__wrapped__("job", owner="worker", expected_revision=1)
    assert job.progress == {}


async def test_title_completed_result_replay_does_not_reauthorize_or_invoke(monkeypatch):
    from lumen.services import title_jobs
    apply = AsyncMock(return_value=True)
    started = AsyncMock(side_effect=AssertionError("committed result reauthorized"))
    provider = AsyncMock(side_effect=AssertionError("committed result regenerated"))
    monkeypatch.setattr(title_jobs, "_apply_result", apply)
    monkeypatch.setattr(title_jobs, "_mark_provider_started", started)
    monkeypatch.setattr(title_jobs.title_summary, "generate_title", provider)
    assert await title_jobs._process_claimed({"job_id": "job", "payload": {}, "replay": True}, owner="worker")
    apply.assert_awaited_once()
    started.assert_not_awaited()
    provider.assert_not_awaited()


@pytest.mark.parametrize("cause", ["downgraded", "revoked"])
async def test_automatic_memory_generation_denies_current_downgraded_owner(monkeypatch, cause):
    from lumen.services import memory_extract
    run = SimpleNamespace(user_id="owner", project_id="project", api_key_id=7 if cause == "revoked" else None,
                          source="web", run_scope="persistent")
    class RunSession(Session):
        async def get(self, *args, **kwargs):
            return run
    key = SimpleNamespace(owner_user_id="owner", owner_project_id="project", is_active=False,
                          revoked_at=None, expires_at=None)
    monkeypatch.setattr(inference_authority, "get_session_factory", lambda: lambda: RunSession(key))
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(return_value={
        "roles": ["member", "lumen-history_reader"] if cause == "downgraded" else ["member", "lumen-chat_user", "lumen-history_reader"],
        "is_system_admin": False}))
    monkeypatch.setattr(memory_extract.ps, "resolve_memory_model", AsyncMock(return_value={"model_name": "small"}))
    monkeypatch.setattr(memory_extract.ms, "list_memories", AsyncMock(return_value=[]))
    monkeypatch.setattr(memory_extract.cs, "list_messages_for_run", AsyncMock(return_value=[{"role": "user", "content": "remember"}]))
    provider = AsyncMock(side_effect=AssertionError("memory provider started after downgrade"))
    monkeypatch.setattr(memory_extract.litellm_client, "acompletion", provider)
    assert await memory_extract.generate_memory_if_applicable(conversation_id="conv", project_id="project", run_id="run", user_id="owner") is None
    provider.assert_not_awaited()


async def test_auxiliary_generation_does_not_require_old_selected_tools(monkeypatch):
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(return_value={
        "roles": ["member", "lumen-chat_user", "lumen-history_reader"], "is_system_admin": False}))
    run = SimpleNamespace(user_id="owner", project_id="project", api_key_id=None, source="web",
        run_scope="persistent", conversation_id="conv",
        capability_snapshot={"required_scopes": ["native:tools:execute"]})
    await inference_authority.authorize_run_generation(Session(None), run)


@pytest.mark.parametrize("required", [False, True])
@pytest.mark.parametrize("tools", [False, True])
async def test_auxiliary_model_uses_its_own_intrinsic_capability(monkeypatch, required, tools):
    roles = ["member", "lumen-chat_user", "lumen-history_reader", *(["lumen-tools_user"] if tools else [])]
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(return_value={"roles": roles, "is_system_admin": False}))
    run = SimpleNamespace(user_id="owner", project_id="project", api_key_id=None, source="web",
                          run_scope="persistent", conversation_id="conv")
    resolved = {"capabilities": {"web_search_required": required}}
    if required and not tools:
        with pytest.raises(api_key_store.ApiKeyForbidden):
            await inference_authority.authorize_run_generation(Session(None), run, resolved=resolved)
    else:
        await inference_authority.authorize_run_generation(Session(None), run, resolved=resolved)


async def test_auxiliary_intrinsic_search_preserves_key_scope_attenuation(monkeypatch):
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(return_value={
        "roles": ["member", "lumen-chat_user", "lumen-history_reader", "lumen-tools_user"], "is_system_admin": False}))
    key = SimpleNamespace(owner_user_id="owner", owner_project_id="project", is_active=True, revoked_at=None,
                          expires_at=None, scopes=["native:runs:write", "native:conversations:read"])
    run = SimpleNamespace(user_id="owner", project_id="project", api_key_id=7, source="api",
                          run_scope="persistent", conversation_id="conv")
    with pytest.raises(api_key_store.ApiKeyForbidden):
        await inference_authority.authorize_run_generation(Session(key), run,
            resolved={"capabilities": {"web_search_required": True}})


async def test_denied_intrinsic_title_terminalizes_only_title_not_completed_chat(monkeypatch):
    from lumen.models.chat_db import ChatConversation
    from lumen.models.chat_jobs import ChatJob
    from lumen.services import title_jobs

    payload = {"user_id": "owner", "project_id": "project", "expected_title_revision": 1,
               "summary_route": {"model_name": "administrator-selected-model", "capabilities": {"web_search_required": True}},
               "exchange": [{"role": "assistant", "content": "already delivered answer"}]}
    job = SimpleNamespace(status="running", lease_owner="worker", run_id="run", conversation_id="conv",
                          progress={}, payload="synthetic encrypted payload")
    conversation = SimpleNamespace(title_revision=1, title_status="pending", title=None)
    run = SimpleNamespace(user_id="owner", project_id="project", api_key_id=None, source="web",
                          run_scope="persistent", conversation_id="conv", status="completed")
    class JobSession(Session):
        async def get(self, model, key, **kwargs):
            return job if model is ChatJob else conversation if model is ChatConversation else run
    monkeypatch.setattr("lumen.db.get_session_factory", lambda: lambda: JobSession(None))
    monkeypatch.setattr(title_jobs, "_json_load", lambda _: payload)
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(return_value={
        "roles": ["member", "lumen-chat_user", "lumen-history_reader"], "is_system_admin": False}))
    monkeypatch.setattr(title_jobs.credit, "precheck", AsyncMock())
    provider = AsyncMock(side_effect=AssertionError("intrinsic title provider started without tools authority"))
    monkeypatch.setattr(title_jobs.title_summary, "generate_title", provider)
    assert await title_jobs._process_claimed({"job_id": "job", "payload": payload, "replay": False}, owner="worker")
    assert job.status == "failed" and job.error_code == "inference_authority_revoked"
    assert conversation.title_status == "failed" and conversation.title is None
    assert run.status == "completed" and payload["exchange"][0]["content"] == "already delivered answer"
    auth.resolve_project_authority.assert_awaited_once_with("owner", "project")
    provider.assert_not_awaited()


async def test_denied_intrinsic_memory_completes_optional_job_without_mutating_chat(monkeypatch):
    from lumen.models.chat_jobs import ChatJob
    from lumen.services import memory_extract, memory_jobs

    run = SimpleNamespace(user_id="owner", project_id="project", api_key_id=None, source="web",
                          run_scope="persistent", conversation_id="conv", status="completed")
    job = SimpleNamespace(status="running", lease_owner="worker")
    class MemorySession(Session):
        async def get(self, model, key, **kwargs):
            return job if model is ChatJob else run
    def factory():
        return MemorySession(None)
    monkeypatch.setattr(inference_authority, "get_session_factory", lambda: factory)
    monkeypatch.setattr("lumen.db.get_session_factory", lambda: factory)
    monkeypatch.setattr(auth, "resolve_project_authority", AsyncMock(return_value={
        "roles": ["member", "lumen-chat_user", "lumen-history_reader"], "is_system_admin": False}))
    monkeypatch.setattr(memory_extract.ps, "resolve_memory_model", AsyncMock(return_value={
        "model_name": "administrator-selected-model", "capabilities": {"web_search_required": True}}))
    monkeypatch.setattr(memory_extract.ms, "list_memories", AsyncMock(return_value=[]))
    messages = [{"role": "user", "content": "remember"}, {"role": "assistant", "content": "already delivered answer"}]
    monkeypatch.setattr(memory_extract.cs, "list_messages_for_run", AsyncMock(return_value=messages))
    provider = AsyncMock(side_effect=AssertionError("intrinsic memory provider started without tools authority"))
    apply = AsyncMock(side_effect=AssertionError("denied extraction mutated stored memory"))
    retry = AsyncMock()
    monkeypatch.setattr(memory_extract.litellm_client, "acompletion", provider)
    monkeypatch.setattr(memory_jobs.ms, "apply_automatic_ops_in_transaction", apply)
    monkeypatch.setattr(memory_jobs, "_retry", retry)
    assert await memory_jobs._process_claimed(("job", "run", "conv", "project", "owner"), owner="worker")
    assert job.status == "completed" and job.progress == {"applied": {"add": 0, "update": 0, "delete": 0}}
    assert run.status == "completed" and messages[-1]["content"] == "already delivered answer"
    auth.resolve_project_authority.assert_awaited_once_with("owner", "project")
    provider.assert_not_awaited()
    apply.assert_not_awaited()
    retry.assert_not_awaited()


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("segment_state", ["prepared", "completed", "provider_started"])
async def test_intrinsic_compaction_fences_only_new_intent_including_old_snapshots(monkeypatch, legacy, segment_state):
    summary = {"model_name": "administrator-selected-model", "provider_id": 1, "model_id": 2}
    if not legacy:
        summary["capabilities"] = {"web_search_required": True}
    run = SimpleNamespace(id="run", user_id="owner", project_id="project", api_key_id=None, agent_id=None,
                          source="web", run_scope="persistent", conversation_id="conv", capability_snapshot={"summary_route": summary})
    segment = SimpleNamespace(status=segment_state, result_payload="{}", usage_payload="{}")
    monkeypatch.setattr(execution, "_factory", lambda: lambda: Session(run))
    monkeypatch.setattr(execution, "_require_owned_running_lease", lambda *_args: None)
    monkeypatch.setattr(execution, "prepare_segment", AsyncMock(return_value=segment))
    monkeypatch.setattr(execution, "_payload", lambda _: {"features": {"tool_policy": {"mode": "none"}}})
    monkeypatch.setattr(execution, "load_segment_payload", lambda _: {})
    monkeypatch.setattr(execution, "fail_unresolved_segment", lambda *args, **kwargs: None)
    authority = AsyncMock(return_value={"roles": ["member", "lumen-chat_user", "lumen-history_reader"], "is_system_admin": False})
    resolver = AsyncMock(return_value={"capabilities": {"web_search_required": True}})
    monkeypatch.setattr(auth, "resolve_project_authority", authority)
    monkeypatch.setattr(execution.ps, "resolve_model_snapshot", resolver)
    hooks = execution._DurableExecutionHooks(run_id="run", owner="worker")
    if segment_state == "prepared":
        with pytest.raises(DurableRunInputError, match="inference_authority_revoked"):
            await hooks._start(segment_id="context:1", ordinal=100, endpoint="context_compaction", turn_ordinal=0)
        assert segment.status == "prepared"
        assert authority.await_count == 1 and resolver.await_count == int(legacy)
    else:
        await hooks._start(segment_id="context:1", ordinal=100, endpoint="context_compaction", turn_ordinal=0)
        authority.assert_not_awaited()
        resolver.assert_not_awaited()


async def test_denied_automatic_intrinsic_compaction_preserves_safe_plain_chat_input(monkeypatch):
    summary = {"model_name": "administrator-selected-model", "context_limit": 4000,
               "capabilities": {"web_search_required": True}}
    run = SimpleNamespace(id="run", status="running", user_id="owner", project_id="project", api_key_id=None,
        agent_id=None, source="web", run_scope="persistent", conversation_id="conv", temp_thread_id=None,
        model_name="ordinary-chat", capability_snapshot={"capabilities": {"context_limit": 1000}, "summary_route": summary},
        pricing_snapshot={"summary_route": {"token_rates": {}}})
    segment = SimpleNamespace(status="prepared")
    state = SimpleNamespace(measurement="exact", input_budget=100, input_tokens=80, recommendation="required",
        model_copy=lambda **kwargs: state, model_dump=lambda **kwargs: {"input_budget": 100, "input_tokens": 80})
    events = []
    async def append(_run_id, event_type, payload, **kwargs):
        events.append((event_type, payload))
    async def prepare(*args, compactor, **kwargs):
        return await compactor(["stored older history"], {"round": 0})
    monkeypatch.setattr(execution, "_factory", lambda: lambda: Session(run))
    monkeypatch.setattr(execution, "_payload", lambda _: {"max_tokens": 10, "features": {"memory": False, "tool_policy": {"mode": "none"}}})
    monkeypatch.setattr(execution, "_append", append)
    monkeypatch.setattr(execution, "_require_owned_running_lease", lambda *_args: None)
    monkeypatch.setattr(execution, "prepare_segment", AsyncMock(return_value=segment))
    monkeypatch.setattr(execution, "_cancel_requested", AsyncMock(return_value=False))
    monkeypatch.setattr(execution.context_manager, "context_state", lambda *args, **kwargs: state)
    monkeypatch.setattr(execution.context_manager, "prepare_model_messages", prepare)
    monkeypatch.setattr(execution.credit, "precheck", AsyncMock())
    authority = AsyncMock(return_value={"roles": ["member", "lumen-chat_user", "lumen-history_reader"], "is_system_admin": False})
    monkeypatch.setattr(auth, "resolve_project_authority", authority)
    provider = AsyncMock(side_effect=AssertionError("intrinsic summary provider started without tools authority"))
    monkeypatch.setattr(execution.litellm_client, "acompletion", provider)
    messages = [{"role": "user", "content": "accepted plain chat"}]
    result = await execution._DurableExecutionHooks(run_id="run", owner="worker").prepare_context(
        messages=messages, tool_schemas=[], round_index=0)
    assert result is messages and run.status == "running" and segment.status == "prepared"
    assert events[-1][1]["phase"] == "failed" and events[-1][1]["after_tokens"] == 80
    ensure_scopes(principal("lumen-chat_user"), *inference_authority.native_run_scopes({"features": {"memory": False, "tool_policy": {"mode": "none"}}}))
    provider.assert_not_awaited()
    authority.assert_awaited_once_with("owner", "project")

