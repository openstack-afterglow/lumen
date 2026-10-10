"""Real API/worker -> installed Keystone SDK -> current graph -> synthetic provider.

All runtime calls use public HTTP. The queued revalidation cases copy a frozen
request from an HTTP-completed image into an offline-pool MariaDB fixture; they
prove worker new-I/O denial, not a second HTTP admission. Provider counters are
required because media/Responses stats intentionally omit raw prompt text.
"""
import json
import os
from uuid import uuid4

import httpx
import pytest
from system.fake_keystone import CONTROL_TOKEN, DIRECTORY_PROJECT_ID, DIRECTORY_TOKEN, OWNER_TOKEN, PROJECT_ID
from system.test_process_stack import _poll_run_until_terminal

pytestmark = pytest.mark.system


def test_system_admin_target_key_cannot_substitute_tenant_scope():
    api_url = os.environ.get("LUMEN_API_BASE_URL", "http://localhost:8012")
    directory_url = os.environ.get("LUMEN_FAKE_KEYSTONE_URL", "http://fake-keystone:5000")
    owner_headers = {
        "X-Auth-Token": DIRECTORY_TOKEN,
        "X-Project-Id": DIRECTORY_PROJECT_ID,
        "X-Target-Project-Id": PROJECT_ID,
    }
    control_headers = {"X-Test-Control-Token": CONTROL_TOKEN}
    created_id = None
    with httpx.Client(base_url=api_url, timeout=30, trust_env=False) as api, \
            httpx.Client(base_url=directory_url, timeout=10, trust_env=False) as directory:
        assert directory.post("/_control/reset", headers=control_headers, json={}).status_code == 200
        try:
            target_read = api.get("/v1/api-keys", headers=owner_headers)
            assert target_read.status_code == 200, target_read.text
            own_read = api.get("/v1/api-keys", headers={
                "X-Auth-Token": OWNER_TOKEN, "X-Project-Id": PROJECT_ID,
            })
            assert own_read.status_code == 200, own_read.text
            forbidden_target = api.get("/v1/api-keys", headers={
                "X-Auth-Token": OWNER_TOKEN, "X-Project-Id": PROJECT_ID,
                "X-Target-Project-Id": DIRECTORY_PROJECT_ID,
            })
            assert forbidden_target.status_code == 403, forbidden_target.text
            issued = api.post("/v1/api-keys", headers=owner_headers,
                              json={"name": "system target isolation", "scopes": ["models:read"]})
            assert issued.status_code == 201, issued.text
            key = issued.json()
            created_id = key["id"]
            for key_headers in ({"X-API-Key": key["key"]}, {"Authorization": "Bearer " + key["key"]}):
                assert api.get("/v1/models", headers=key_headers).status_code == 200
                for header in ("X-Project-Id", "X-Target-Project-Id"):
                    own = api.get("/v1/models", headers={**key_headers, header: PROJECT_ID})
                    assert own.status_code == 200, own.text
                    foreign = api.get("/v1/models", headers={**key_headers, header: DIRECTORY_PROJECT_ID})
                    assert foreign.status_code == 403, foreign.text
        finally:
            assert directory.post("/_control/reset", headers=control_headers, json={}).status_code == 200
            if created_id is not None:
                cleanup = api.delete(f"/v1/api-keys/{created_id}", headers=owner_headers)
                assert cleanup.status_code in {200, 204}, cleanup.text


def test_current_graph_downgrade_at_native_and_compat_http_boundaries():
    api_url = os.environ.get("LUMEN_API_BASE_URL", "http://localhost:8012")
    directory_url = os.environ.get("LUMEN_FAKE_KEYSTONE_URL", "http://fake-keystone:5000")
    provider_url = os.environ.get("LUMEN_FAKE_PROVIDER_URL", "http://fake-provider:8080")
    model = os.environ.get("LUMEN_MODEL_NAME", "fake-gpt-4")
    owner_headers = {"X-Auth-Token": OWNER_TOKEN, "X-Project-Id": PROJECT_ID}
    control_headers = {"X-Test-Control-Token": CONTROL_TOKEN}
    scopes = ["models:read", "compat:completions:write", "native:runs:write", "native:runs:read"]
    created_id = None
    with httpx.Client(base_url=api_url, timeout=60, trust_env=False) as api, \
            httpx.Client(base_url=directory_url, timeout=10, trust_env=False) as directory, \
            httpx.Client(base_url=provider_url, timeout=10, trust_env=False) as provider:
        reset = directory.post("/_control/reset", headers=control_headers, json={})
        assert reset.status_code == 200, reset.text
        try:
            issue = api.post("/v1/api-keys", headers=owner_headers, json={"name": "current-graph HTTP", "scopes": scopes})
            assert issue.status_code == 201, issue.text
            issued = issue.json()
            created_id = issued["id"]
            key_headers = {"Authorization": "Bearer " + issued["key"]}
            response = api.post("/v1/chat/completions", headers=key_headers,
                                json={"model": model, "messages": [{"role": "user", "content": "role boundary positive"}]})
            assert response.status_code == 200, response.text
            assert response.json()["choices"][0]["message"]["content"] == "Hello from fake provider!"
            change = directory.post("/_control/configure", headers=control_headers,
                json={"remove_edges": [{"prior": "lumen_editor", "implied": "lumen-keys_editor"}]})
            assert change.status_code == 200, change.text
            denied_issue = api.post("/v1/api-keys", headers=owner_headers, json={"name": "must deny", "scopes": scopes})
            assert denied_issue.status_code == 403, denied_issue.text
            # Existing key use does not require retained issuer keys-editor.
            allowed = api.post("/v1/chat/completions", headers=key_headers,
                json={"model": model, "messages": [{"role": "user", "content": "existing key remains usable"}]})
            assert allowed.status_code == 200, allowed.text
            change = directory.post("/_control/configure", headers=control_headers,
                json={"remove_edges": [{"prior": "lumen_user", "implied": "lumen-chat_user"}]})
            assert change.status_code == 200, change.text
            assert api.get("/v1/models", headers=key_headers).status_code == 200
            before = _provider_stats(provider)
            denied_marker = "SENTINEL_ROLE_DENIED_" + uuid4().hex
            compat = api.post("/v1/chat/completions", headers=key_headers,
                json={"model": model, "messages": [{"role": "user", "content": denied_marker}]})
            assert compat.status_code == 403, compat.text
            native = api.post("/v1/temp-completions", headers={**key_headers, "Idempotency-Key": str(uuid4())},
                json={"model_id": model, "parts": [{"type": "text", "text": denied_marker}],
                      "features": {"memory": False, "tool_policy": {"mode": "none"}}})
            assert native.status_code == 403, native.text
            _assert_no_provider_io(provider, before, [denied_marker])
            removed = directory.post("/_control/configure", headers=control_headers, json={"owner_roles": []})
            assert removed.status_code == 200, removed.text
            assert api.get("/v1/models", headers=key_headers).status_code == 401
        finally:
            restored = directory.post("/_control/reset", headers=control_headers, json={})
            assert restored.status_code == 200, restored.text
            if created_id is not None:
                cleanup = api.delete(f"/v1/api-keys/{created_id}", headers=owner_headers)
                assert cleanup.status_code in {200, 204}, cleanup.text


def test_media_only_cancel_is_owner_bound_and_idempotent_in_persistent_store():
    """A queued image in an offline pool remains cancelable without chat authority."""
    from urllib.parse import unquote, urlsplit

    import pymysql
    from system.fake_keystone import DIRECTORY_PROJECT_ID, DIRECTORY_TOKEN, OWNER_ID

    api_url = os.environ["LUMEN_API_BASE_URL"]
    directory_url = os.environ["LUMEN_FAKE_KEYSTONE_URL"]
    database = urlsplit(os.environ["DATABASE_URL"])
    assert database.hostname == "mariadb" and urlsplit(directory_url).hostname == "fake-keystone"
    run_id, text_id, pool_id = (str(uuid4()) for _ in range(3))
    control = {"X-Test-Control-Token": CONTROL_TOKEN}
    headers = {"X-Auth-Token": OWNER_TOKEN, "X-Project-Id": PROJECT_ID}
    foreign = {"X-Auth-Token": DIRECTORY_TOKEN, "X-Project-Id": DIRECTORY_PROJECT_ID}
    db = pymysql.connect(host=database.hostname, port=database.port or 3306,
                         user=unquote(database.username), password=unquote(database.password),
                         database=database.path.lstrip("/"), autocommit=True)
    with httpx.Client(base_url=api_url, timeout=30, trust_env=False) as api, \
            httpx.Client(base_url=directory_url, timeout=10, trust_env=False) as directory:
        try:
            assert directory.post("/_control/reset", headers=control, json={}).status_code == 200
            with db.cursor() as cursor:
                for identifier, kind in ((run_id, "image"), (text_id, "completion")):
                    cursor.execute("""INSERT INTO chat_runs
                        (id, run_scope, run_kind, workload_class, worker_pool_id, project_id, user_id,
                         model_name, source, capability_snapshot, pricing_snapshot, client_request_id,
                         request_fingerprint, fingerprint_version, status, execution_protocol_version,
                         execution_mode, depth, last_seq, current_ordinal, reserved_credits,
                         descendant_credits_reserved, sandbox_seconds_reserved, created_at, updated_at)
                        VALUES (%s,'temporary',%s,%s,%s,%s,%s,'synthetic-model','web','{}','{}',%s,%s,1,'queued',
                                1,'chat',0,0,0,0,0,0,UTC_TIMESTAMP(),UTC_TIMESTAMP())""",
                        (identifier, kind, "online_media" if kind == "image" else "online_text",
                         pool_id, PROJECT_ID, OWNER_ID, str(uuid4()), identifier))
            downgrade = directory.post("/_control/configure", headers=control,
                json={"owner_roles": ["project_member", "lumen-images_user"]})
            assert downgrade.status_code == 200, downgrade.text
            foreign_cancel = api.post(f"/v1/runs/{run_id}/cancel", headers=foreign)
            assert foreign_cancel.status_code == 404, foreign_cancel.text
            denied_text = api.post(f"/v1/runs/{text_id}/cancel", headers=headers)
            assert denied_text.status_code == 403, denied_text.text
            first = api.post(f"/v1/runs/{run_id}/cancel", headers=headers)
            second = api.post(f"/v1/runs/{run_id}/cancel", headers=headers)
            assert first.status_code == second.status_code == 200, (first.text, second.text)
            assert first.json() == second.json()
            assert first.json()["status"] == "canceled" and first.json()["terminal"] is True
            with db.cursor() as cursor:
                cursor.execute("SELECT status,cancel_requested_at,last_seq FROM chat_runs WHERE id=%s", (run_id,))
                stored = cursor.fetchone()
                assert stored[0] == "canceled" and stored[1] is not None
                assert stored[2] == first.json()["last_seq"]
                cursor.execute("SELECT status,cancel_requested_at FROM chat_runs WHERE id=%s", (text_id,))
                assert cursor.fetchone() == ("queued", None)
        finally:
            assert directory.post("/_control/reset", headers=control, json={}).status_code == 200
            with db.cursor() as cursor:
                cursor.execute("DELETE FROM chat_runs WHERE id IN (%s,%s)", (run_id, text_id))
            db.close()


_OWNER_HEADERS = {"X-Auth-Token": OWNER_TOKEN, "X-Project-Id": PROJECT_ID}
_CONTROL_HEADERS = {"X-Test-Control-Token": CONTROL_TOKEN}
_ACTION_SCOPES = {
    "chat": ("native:runs:write", "compat:completions:write"),
    "images": ("native:images:write", "compat:images:write"),
    "audio": ("native:audio:write", "compat:audio:write"),
    "tools": ("native:tools:execute",),
}
_BROAD_SCOPES = [
    "models:read", "native:runs:read", "native:extensions:read",
    *(scope for scopes in _ACTION_SCOPES.values() for scope in scopes),
]


def _directory_change(directory, payload):
    response = directory.post("/_control/configure", headers=_CONTROL_HEADERS, json=payload)
    assert response.status_code == 200, response.text


def _provider_stats(provider):
    response = provider.get("/_control/stats")
    assert response.status_code == 200, response.text
    return response.json()


def _assert_no_provider_io(provider, before, markers):
    after = _provider_stats(provider)
    assert after["request_count"] == before["request_count"], (before, after)
    assert after["operation_counts"] == before["operation_counts"], (before, after)
    # Chat stats retain SENTINEL_ markers, but media and Responses do not. The
    # counters above make marker absence meaningful for those protocols too.
    for marker in markers:
        assert marker not in json.dumps(after)


@pytest.fixture
def authority_http():
    """Own only this test's issued keys; always restore the synthetic directory."""
    created_ids = []
    with httpx.Client(base_url=os.environ["LUMEN_API_BASE_URL"], timeout=90, trust_env=False) as api, \
            httpx.Client(base_url=os.environ["LUMEN_FAKE_KEYSTONE_URL"], timeout=10, trust_env=False) as directory, \
            httpx.Client(base_url=os.environ["LUMEN_FAKE_PROVIDER_URL"], timeout=10, trust_env=False) as provider:
        reset = directory.post("/_control/reset", headers=_CONTROL_HEADERS, json={})
        assert reset.status_code == 200, reset.text
        try:
            reset = provider.post("/_control/reset", json={})
            assert reset.status_code == 200, reset.text
            yield api, directory, provider, created_ids
        finally:
            restored = directory.post("/_control/reset", headers=_CONTROL_HEADERS, json={})
            assert restored.status_code == 200, restored.text
            for key_id in created_ids:
                cleanup = api.delete(f"/v1/api-keys/{key_id}", headers=_OWNER_HEADERS)
                assert cleanup.status_code in {200, 204}, cleanup.text


def _issue_owned_key(api, created_ids, scopes):
    response = api.post("/v1/api-keys", headers=_OWNER_HEADERS,
                        json={"name": "authority HTTP " + str(uuid4()), "scopes": scopes})
    assert response.status_code == 201, response.text
    issued = response.json()
    created_ids.append(issued["id"])
    assert set(issued["scopes"]) == set(scopes), issued
    return issued["id"], {"Authorization": "Bearer " + issued["key"]}


def _authority_payload(action, boundary, marker):
    native = boundary == "native"
    model = os.environ.get("LUMEN_MODEL_NAME", "fake-gpt-4")
    if action in {"chat", "tools"}:
        if native:
            return "/v1/temp-completions", {
                "model_id": model, "parts": [{"type": "text", "text": marker}],
                "features": {"memory": False, "tool_policy": {
                    "mode": "agent_default" if action == "tools" else "none",
                    "enabled_tool_ids": [], "enabled_mcp_ids": [],
                }},
            }
        payload = {"model": model, "messages": [{"role": "user", "content": marker}]}
        if action == "tools":
            payload.update(tools=[{"type": "function", "function": {
                "name": "authority_probe", "description": "Synthetic client function",
                "parameters": {"type": "object", "properties": {}},
            }}], tool_choice="none")
        return "/v1/chat/completions", payload
    if action == "images":
        return ("/v1/chat/images/generations" if native else "/v1/images/generations"), {
            "model_id" if native else "model": "gpt-image-1", "prompt": marker,
            "size": "1024x1024", "quality": "high", "n": 1,
        }
    assert action == "audio"
    return ("/v1/chat/audio/speech" if native else "/v1/audio/speech"), {
        "model_id" if native else "model": "tts-1", "input": marker,
        "voice": "alloy", "response_format": "wav",
    }


def _authority_post(api, headers, action, boundary, marker):
    path, payload = _authority_payload(action, boundary, marker)
    return api.post(path, headers={**headers, "Idempotency-Key": str(uuid4())}, json=payload)


def _assert_positive_response(api, headers, action, boundary, response):
    if boundary == "native" and action != "audio":
        assert response.status_code == 202, response.text
        run_id = response.json()["run_id"]
        terminal = _poll_run_until_terminal(api, run_id, headers)
        assert terminal["status"] == "completed" and terminal["terminal"] is True, terminal
        return run_id
    assert response.status_code == 200, response.text
    if action in {"chat", "tools"}:
        assert response.json()["choices"][0]["message"]["content"] == "Hello from fake provider!"
    elif action == "images":
        import base64
        import io

        from PIL import Image

        image = base64.b64decode(response.json()["data"][0]["b64_json"], validate=True)
        with Image.open(io.BytesIO(image)) as decoded:
            decoded.load()
            assert decoded.format == "PNG" and decoded.size == (1024, 1024)
    else:
        import io
        import wave

        with wave.open(io.BytesIO(response.content), "rb") as audio:
            assert audio.getnchannels() == 1 and audio.getsampwidth() == 2
            assert audio.getnframes() == audio.getframerate() == 24000
            assert len(audio.readframes(audio.getnframes())) == 48000
    return None


@pytest.mark.parametrize("requested_scope", [scope for scopes in _ACTION_SCOPES.values() for scope in scopes])
def test_key_issuance_requires_requested_scope_subset_of_current_owner_leaves(authority_http, requested_scope):
    api, directory, provider, created_ids = authority_http
    # The owner still has issuer authority and inventory/history, but none of
    # the independently named generation/tools leaves. Issuer is not an alias.
    _directory_change(directory, {"owner_roles": ["project_member", "lumen-keys_editor"]})
    _issue_owned_key(api, created_ids, ["models:read"])
    listed = api.get("/v1/api-keys", headers=_OWNER_HEADERS)
    assert listed.status_code == 200, listed.text
    before = _provider_stats(provider)
    denied = api.post("/v1/api-keys", headers=_OWNER_HEADERS,
                      json={"name": "denied requested subset", "scopes": ["models:read", requested_scope]})
    assert denied.status_code == 403, denied.text
    after = api.get("/v1/api-keys", headers=_OWNER_HEADERS)
    assert after.status_code == 200, after.text
    assert after.json() == listed.json(), (listed.text, after.text)
    _assert_no_provider_io(provider, before, [])


@pytest.mark.parametrize("removed_action", ["chat", "images", "audio", "tools"])
def test_current_owner_leaves_are_independent_at_native_and_compat_http(authority_http, removed_action):
    api, directory, provider, created_ids = authority_http
    _, headers = _issue_owned_key(api, created_ids, _BROAD_SCOPES)
    _directory_change(directory, {"remove_edges": [
        {"prior": "lumen_user", "implied": f"lumen-{removed_action}_user"},
    ]})
    assert api.get("/v1/models", headers=headers).status_code == 200
    before = _provider_stats(provider)
    denied_markers = []
    for boundary in ("native", "compat"):
        marker = "SENTINEL_AUTHORITY_DENIED_" + uuid4().hex
        denied_markers.append(marker)
        denied = _authority_post(api, headers, removed_action, boundary, marker)
        assert denied.status_code == 403, (removed_action, boundary, denied.text)
    _assert_no_provider_io(provider, before, denied_markers)

    # Losing one leaf must not revoke any other modality. Tool-bearing chat
    # is deliberately conjunctive: it cannot bypass the missing chat leaf.
    for action in _ACTION_SCOPES:
        if action == removed_action:
            continue
        if action == "tools" and removed_action == "chat":
            before = _provider_stats(provider)
            markers = []
            for boundary in ("native", "compat"):
                marker = "SENTINEL_TOOLS_WITHOUT_CHAT_DENIED_" + uuid4().hex
                markers.append(marker)
                denied = _authority_post(api, headers, action, boundary, marker)
                assert denied.status_code == 403, denied.text
            _assert_no_provider_io(provider, before, markers)
            continue
        for boundary in ("native", "compat"):
            before = _provider_stats(provider)
            response = _authority_post(api, headers, action, boundary,
                                       "SENTINEL_AUTHORITY_ALLOWED_" + uuid4().hex)
            _assert_positive_response(api, headers, action, boundary, response)
            after = _provider_stats(provider)
            assert after["request_count"] > before["request_count"], (action, boundary, before, after)
            if action in {"images", "audio"}:
                operation = "images.generations" if action == "images" else "audio.speech"
                assert after["operation_counts"][operation] == before["operation_counts"][operation] + 1
                call = after["calls"][-1]
                assert call["operation"] == operation and call["status"] == 200, call
                assert call["host"] == "api.openai.com" and call["tls"] is True, call
    _assert_no_provider_io(provider, _provider_stats(provider), denied_markers)


def test_revoked_key_is_denied_before_provider_at_all_native_and_compat_boundaries(authority_http):
    api, _, provider, created_ids = authority_http
    key_id, headers = _issue_owned_key(api, created_ids, _BROAD_SCOPES)
    revoked = api.delete(f"/v1/api-keys/{key_id}", headers=_OWNER_HEADERS)
    assert revoked.status_code in {200, 204}, revoked.text
    created_ids.remove(key_id)
    assert api.get("/v1/models", headers=headers).status_code == 401
    before = _provider_stats(provider)
    markers = []
    for action in _ACTION_SCOPES:
        for boundary in ("native", "compat"):
            marker = "SENTINEL_REVOKED_KEY_DENIED_" + uuid4().hex
            markers.append(marker)
            denied = _authority_post(api, headers, action, boundary, marker)
            assert denied.status_code == 401, (action, boundary, denied.text)
    _assert_no_provider_io(provider, before, markers)


@pytest.mark.parametrize("loss", ["graph", "revocation"])
def test_queued_media_worker_revalidates_current_authority_before_new_io(authority_http, loss):
    """A copied frozen HTTP image request, not a mocked worker/admission, is released."""
    from urllib.parse import unquote, urlsplit

    import pymysql
    from system.fake_keystone import OWNER_ID

    api, directory, provider, created_ids = authority_http
    key_id, headers = _issue_owned_key(api, created_ids, _BROAD_SCOPES)
    response = _authority_post(api, headers, "images", "native", "SENTINEL_QUEUED_MEDIA_" + uuid4().hex)
    source_id = _assert_positive_response(api, headers, "images", "native", response)
    before = _provider_stats(provider)
    assert before["operation_counts"]["images.generations"] == 1, before
    database = urlsplit(os.environ["DATABASE_URL"])
    assert database.hostname == "mariadb"
    assert urlsplit(os.environ["LUMEN_FAKE_KEYSTONE_URL"]).hostname == "fake-keystone"
    queued_id, pool_id = str(uuid4()), str(uuid4())
    db = pymysql.connect(host=database.hostname, port=database.port or 3306,
                         user=unquote(database.username), password=unquote(database.password),
                         database=database.path.lstrip("/"), autocommit=False)
    try:
        # Copy only frozen admission/provenance, never the source's paid result,
        # provider-start/checkpoint, reservation, lease, or terminal journal.
        with db.cursor() as cursor:
            cursor.execute("""INSERT INTO chat_runs
                (id, run_scope, run_kind, workload_class, worker_pool_id, project_id, user_id,
                 model_name, source, api_key_id, capability_snapshot, pricing_snapshot,
                 request_payload, execution_protocol_version, execution_mode, required_plugin_digest,
                 client_request_id, request_fingerprint, fingerprint_version, status, depth, last_seq,
                 current_ordinal, reserved_credits, descendant_credits_reserved,
                 sandbox_seconds_reserved, created_at, updated_at)
                SELECT %s, run_scope, run_kind, workload_class, %s, project_id, user_id,
                       model_name, source, api_key_id, capability_snapshot, pricing_snapshot,
                       request_payload, execution_protocol_version, execution_mode, required_plugin_digest,
                       %s, %s, fingerprint_version, 'queued', 0, 0, 0, 0, 0, 0, UTC_TIMESTAMP(), UTC_TIMESTAMP()
                FROM chat_runs WHERE id=%s AND project_id=%s AND user_id=%s AND status='completed'""",
                (queued_id, pool_id, str(uuid4()), queued_id, source_id, PROJECT_ID, OWNER_ID))
            assert cursor.rowcount == 1
        db.commit()
        queued = api.get(f"/v1/runs/{queued_id}", headers=_OWNER_HEADERS)
        assert queued.status_code == 200 and queued.json()["status"] == "queued", queued.text
        if loss == "graph":
            _directory_change(directory, {"remove_edges": [
                {"prior": "lumen_user", "implied": "lumen-images_user"},
            ]})
        else:
            revoked = api.delete(f"/v1/api-keys/{key_id}", headers=_OWNER_HEADERS)
            assert revoked.status_code in {200, 204}, revoked.text
            created_ids.remove(key_id)
        with db.cursor() as cursor:
            cursor.execute("""UPDATE chat_runs SET worker_pool_id=NULL
                WHERE id=%s AND status='queued' AND worker_pool_id=%s""", (queued_id, pool_id))
            assert cursor.rowcount == 1
        db.commit()
        terminal = _poll_run_until_terminal(api, queued_id, _OWNER_HEADERS)
        assert terminal["status"] == "failed" and terminal["terminal"] is True, terminal
        journal = api.get(f"/v1/runs/{queued_id}/events", headers=_OWNER_HEADERS)
        assert journal.status_code == 200, journal.text
        events = [json.loads(line[5:].strip()) for line in journal.text.splitlines() if line.startswith("data:")]
        failed = [event for event in events if event.get("type") == "run.failed"]
        assert len(failed) == 1 and failed[0]["payload"]["error_code"] == "api_key_unauthorized", events
        with db.cursor() as cursor:
            cursor.execute("""SELECT status,worker_registration_id,provider_started_at
                FROM chat_runs WHERE id=%s""", (queued_id,))
            status, registration, started = cursor.fetchone()
            assert status == "failed" and registration is not None and started is None
            cursor.execute("SELECT COUNT(*) FROM chat_model_call_reservations WHERE run_id=%s", (queued_id,))
            assert cursor.fetchone()[0] == 0
        db.commit()
        _assert_no_provider_io(provider, before, [])
    finally:
        db.rollback()
        with db.cursor() as cursor:
            cursor.execute("DELETE FROM chat_runs WHERE id=%s AND project_id=%s AND user_id=%s",
                           (queued_id, PROJECT_ID, OWNER_ID))
        db.commit()
        db.close()
