"""Local API -> synthetic Keystone/MariaDB -> isolated TLS Gemini HTTP."""
from __future__ import annotations

import os
from uuid import uuid4

import httpx
import pytest
from system.fake_keystone import DIRECTORY_PROJECT_ID, DIRECTORY_TOKEN, OWNER_TOKEN, PROJECT_ID

pytestmark = pytest.mark.system


def test_gemini_http_routes_keep_provider_and_lumen_credentials_separate():
    base = os.environ.get("LUMEN_API_BASE_URL", "http://localhost:8012")
    admin = {"X-Auth-Token": DIRECTORY_TOKEN, "X-Project-Id": DIRECTORY_PROJECT_ID}
    owner = {"X-Auth-Token": OWNER_TOKEN, "X-Project-Id": PROJECT_ID}
    model = "gemini/gemini-decision-1"
    registered_provider = None
    registered_key = None
    with httpx.Client(base_url=base, trust_env=False, timeout=30) as client:
        try:
            created = client.post("/v1/admin/providers", headers=admin, json={
                "name": f"system-decision-{uuid4().hex[:12]}", "provider_type": "gemini",
                "api_key": "system-gemini-decisions-fixture",
            })
            assert created.status_code == 201, created.text
            registered_provider = created.json()["id"]
            created = client.post("/v1/admin/models", headers=admin, json={
                "provider_id": registered_provider, "model_name": model, "model_kind": "decision",
            })
            assert created.status_code == 201, created.text
            model_id = created.json()["id"]
            selector = f"lumen/{registered_provider}/{model_id}"

            issued = client.post("/v1/api-keys", headers=owner, json={
                "name": "system Gemini Decisions", "scopes": ["compat:completions:write", "models:read"],
            })
            assert issued.status_code == 201, issued.text
            registered_key = issued.json()["id"]
            headers = {"x-goog-api-key": issued.json()["key"]}
            wire = {"model": selector, "input": "The package arrived damaged.", "questions": {
                "is_defect": {"type": "noul", "instructions": "Is the customer reporting a product defect?"},
            }}
            for path, auth in [("/v1/chat/decisions", owner), ("/v1beta/decisions", headers)]:
                response = client.post(path, headers=auth, json=wire)
                assert response.status_code == 200, response.text
                assert response.json() == {
                    "model": model,
                    "answers": {"is_defect": {"type": "noul", "noul": 0.03, "confidence": 0.97}},
                    "usage": {"total_input_tokens": 100, "input_tokens_by_modality": [{"modality": "text", "tokens": 100}],
                              "total_output_tokens": 0, "total_tokens": 100},
                }
            response = client.post("/v1/chat/decisions", headers=owner, json={**wire, "input": "synthetic safety rejection"})
            assert response.status_code == 400 and response.json() == {"detail": "image_safety"}
            visible = client.get("/v1/chat/models?model_kind=decision", headers=owner)
            assert visible.status_code == 200, visible.text
            registered = next(row for row in visible.json() if row["id"] == model_id)
            assert registered["capabilities"]["decisions"]
            assert float(registered["effective_input_price_per_million"]) == 0.10
            assert model not in client.get("/v1/models", headers=headers).text
            blocked = client.post("/v1/decisions", headers=headers, json={
                "model": selector, "input": "text", "questions": [{"type": "predicate", "instructions": "yes?"}],
            })
            assert blocked.status_code == 422 and "/v1beta/decisions" in blocked.json()["error"]["message"]
        finally:
            if registered_key is not None:
                revoked = client.delete(f"/v1/api-keys/{registered_key}", headers=owner)
                assert revoked.status_code in (200, 204), revoked.text
            if registered_provider is not None:
                deleted = client.delete(f"/v1/admin/providers/{registered_provider}", headers=admin)
                assert deleted.status_code == 204, deleted.text
