import importlib
from uuid import UUID

from fastapi.testclient import TestClient

from lumen_console.app import create_app


def test_local_operator_bootstrap_login_and_connection_lifecycle(tmp_path):
    app = create_app(tmp_path / "console.sqlite")
    client = TestClient(app)

    assert client.get("/api/bootstrap").json() == {"needs_bootstrap": True}
    created = client.post("/api/auth/bootstrap", json={"username": "operator", "password": "long-local-password"})
    assert created.status_code == 201
    assert created.json() == {"username": "operator"}
    assert client.get("/api/auth/me").json() == {"username": "operator"}

    configured = client.put(
        "/api/connection",
        json={
            "base_url": "http://lumen-api:8012/",
            "auth_mode": "api_key",
            "credential": "sk-afgl-local-test-key",
        },
    )
    assert configured.status_code == 200
    assert configured.json() == {
        "base_url": "http://lumen-api:8012",
        "auth_mode": "api_key",
        "project_id": None,
    }
    assert client.get("/api/connection").json()["connected"] is True

    assert client.post("/api/auth/logout").status_code == 204
    assert client.get("/api/auth/me").status_code == 401
    assert client.get("/api/connection").status_code == 401


def test_connection_rejects_embedded_credentials(tmp_path):
    app = create_app(tmp_path / "console.sqlite")
    client = TestClient(app)
    client.post("/api/auth/bootstrap", json={"username": "operator", "password": "long-local-password"})

    response = client.put(
        "/api/connection",
        json={
            "base_url": "https://user:secret@lumen.example",
            "auth_mode": "keystone",
            "credential": "token",
        },
    )

    assert response.status_code == 422
    assert "embedded credentials" in response.json()["detail"]


def test_gateway_proxies_models_and_runs_with_session_scoped_api_key(tmp_path, monkeypatch):
    console_app = importlib.import_module("lumen_console.app")
    requests: list[dict[str, object]] = []

    class FakeResponse:
        status_code = 200

        def __init__(self, payload: object):
            self._payload = payload

        def json(self) -> object:
            return self._payload


    class FakeEventStream:
        status_code = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def aiter_raw(self):
            yield b"id: 8\nevent: part.delta\ndata: {\"payload\":{\"text\":\"done\"}}\n\n"

    class FakeAsyncClient:
        def __init__(self, **_kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def request(self, method, url, headers, json=None):
            requests.append({"method": method, "url": url, "headers": headers, "json": json})
            if method == "GET":
                return FakeResponse({"data": [{"id": "local-model"}]})
            return FakeResponse({"run_id": "run-local", "status": "queued"})

        def stream(self, method, url, headers):
            requests.append({"method": method, "url": url, "headers": headers})
            return FakeEventStream()


    monkeypatch.setattr(console_app.httpx, "AsyncClient", FakeAsyncClient)
    client = TestClient(create_app(tmp_path / "console.sqlite"))
    client.post("/api/auth/bootstrap", json={"username": "operator", "password": "long-local-password"})
    client.put(
        "/api/connection",
        json={
            "base_url": "http://lumen-api:8012",
            "auth_mode": "api_key",
            "credential": "sk-afgl-local-test-key",
        },
    )

    assert client.get("/api/models").json() == {"data": [{"id": "local-model"}]}
    created = client.post(
        "/api/chat/runs",
        json={"model_id": "local-model", "text": "inspect the deployment", "memory": True, "tools": True},
    )

    assert created.status_code == 202
    assert created.json() == {"run_id": "run-local", "status": "queued"}
    assert requests[0]["headers"] == {
        "Accept": "application/json",
        "Authorization": "Bearer sk-afgl-local-test-key",
    }
    assert requests[1]["method"] == "POST"
    assert requests[1]["url"] == "http://lumen-api:8012/v1/temp-completions"
    assert requests[1]["json"] == {
        "parts": [{"type": "text", "text": "inspect the deployment"}],
        "model_id": "local-model",
        "features": {"memory": True, "tool_policy": {"mode": "agent_default"}},
    }
    assert UUID(requests[1]["headers"]["Idempotency-Key"]).version == 4
    replay = client.get("/api/chat/runs/run-local/events", headers={"Last-Event-ID": "7"})

    assert replay.status_code == 200
    assert replay.headers["content-type"].startswith("text/event-stream")
    assert replay.text == 'id: 8\nevent: part.delta\ndata: {"payload":{"text":"done"}}\n\n'
    assert requests[2] == {
        "method": "GET",
        "url": "http://lumen-api:8012/v1/runs/run-local/events",
        "headers": {
            "Accept": "text/event-stream",
            "Authorization": "Bearer sk-afgl-local-test-key",
            "Last-Event-ID": "7",
        },
    }
