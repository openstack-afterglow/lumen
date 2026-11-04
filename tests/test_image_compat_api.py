"""OpenAI image HTTP contract with real PNG multipart bytes and an isolated durable boundary."""

from __future__ import annotations

import base64
import io
from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image

from lumen.api import images as native_images
from lumen.api.compat import images as image_api
from lumen.auth import get_principal
from lumen.service_authority import SERVICE_CAPABILITIES


def _png(rgb: tuple[int, int, int]) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", (1, 1), rgb).save(output, format="PNG")
    return output.getvalue()


def test_compat_generation_returns_real_png_after_terminal_run(monkeypatch):
    output = _png((250, 25, 10))
    statuses = iter(("queued", "completed"))
    calls = []

    async def admit_image_run(request, **owner):
        calls.append((request, owner))
        return SimpleNamespace(run_id="run-1")

    async def owned_run_response(**owner):
        return SimpleNamespace(status=next(statuses))

    async def image_result(**owner):
        return {
            "created": 1234567890,
            "data": [{"b64_json": base64.b64encode(output).decode(), "asset_id": "asset-1"}],
        }

    monkeypatch.setattr(native_images, "admit_image_run", admit_image_run)
    monkeypatch.setattr(image_api.queries, "owned_run_response", owned_run_response)
    monkeypatch.setattr(image_api, "image_result", image_result)
    app = FastAPI()
    app.include_router(image_api.router, prefix="/v1")
    app.dependency_overrides[get_principal] = _principal
    client = TestClient(app)

    response = client.post(
        "/v1/images/generations",
        headers={"Idempotency-Key": str(uuid4())},
        json={"model": "gpt-image-1", "prompt": "A red dot", "provider": "openai"},
    )
    assert response.status_code == 200
    result = response.json()
    assert result["created"] == 1234567890
    assert len(result["data"]) == 1
    assert set(result["data"][0]) == {"b64_json"}
    with Image.open(io.BytesIO(base64.b64decode(result["data"][0]["b64_json"]))) as image:
        assert image.getpixel((0, 0)) == (250, 25, 10)
    assert calls[0][0]["provider_id"] == "openai"
    assert calls[0][1]["client_request_id"]

    unsupported = client.post(
        "/v1/images/generations",
        json={"model": "gpt-image-1", "prompt": "A red dot", "response_format": "url"},
    )
    assert unsupported.status_code == 400
    assert unsupported.json()["error"]["message"] == "Only response_format=b64_json is supported"
    assert len(calls) == 1
    app.dependency_overrides[get_principal] = lambda: {**_principal(), "scopes": ("compat:completions:write",)}
    denied = client.post(
        "/v1/images/generations",
        json={"model": "gpt-image-1", "prompt": "A red dot"},
    )
    assert denied.status_code == 403
    assert "compat:images:write" in denied.json()["detail"]
    assert len(calls) == 1



def _principal():
    return {
        "auth_type": "api_key",
        "user_id": "user-1",
        "project_id": "project-1",
        "api_key_id": 4,
        "scopes": ("compat:images:write", "native:assets:write"),
        "source": "api",
        "roles": ["member", *SERVICE_CAPABILITIES], "is_system_admin": False,
    }


def test_compat_edit_scans_png_and_returns_generated_image(monkeypatch):
    source = _png((20, 30, 40))
    storage = {}
    output = None
    uploads = []

    async def create_uploaded_asset(*, path, original_name, user_id, project_id):
        with Image.open(path) as image:
            image.verify()
        assert path.read_bytes() == source
        assert (user_id, project_id) == ("user-1", "project-1")
        asset_id = str(uuid4())
        storage[asset_id] = source
        uploads.append(asset_id)
        return {"id": asset_id, "status": "clean"}

    async def admit_image_run(request, **owner):
        nonlocal output
        assert owner["user_id"] == "user-1"
        assert request["provider_id"] == "openai"
        assert request["prompt"] == "Change the background"
        with Image.open(io.BytesIO(storage[request["source_asset_id"]])) as image:
            red, green, blue = image.getpixel((0, 0))
        output = _png((red + 35, green + 36, blue + 37))
        return SimpleNamespace(run_id="run-edit")

    async def owned_run_response(**owner):
        return SimpleNamespace(status="completed")

    async def image_result(**owner):
        assert owner == {"run_id": "run-edit", "project_id": "project-1", "user_id": "user-1"}
        assert output is not None
        return {"created": 1234567890, "data": [{"b64_json": base64.b64encode(output).decode(), "asset_id": "out"}]}

    monkeypatch.setattr(image_api.assets, "create_uploaded_asset", create_uploaded_asset)
    monkeypatch.setattr(native_images, "admit_image_run", admit_image_run)
    monkeypatch.setattr(image_api.queries, "owned_run_response", owned_run_response)
    monkeypatch.setattr(image_api, "image_result", image_result)
    app = FastAPI()
    app.include_router(image_api.router, prefix="/v1")
    app.dependency_overrides[get_principal] = _principal
    client = TestClient(app)
    data = {"model": "gpt-image-1", "prompt": "Change the background", "provider": "openai"}
    url = "/v1/images/edits"

    response = client.post(url, data=data, files={"image": ("source.png", source, "image/png")})
    assert response.status_code == 200
    with Image.open(io.BytesIO(base64.b64decode(response.json()["data"][0]["b64_json"]))) as image:
        assert image.getpixel((0, 0)) == (55, 66, 77)
    unsupported = client.post(
        url, data={**data, "mask": "unsupported"}, files={"image": ("source.png", source, "image/png")}
    )
    assert unsupported.status_code == 400
    assert "mask" in unsupported.json()["error"]["message"]
    assert len(uploads) == 1
