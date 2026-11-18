"""Process-stack fixtures for isolated domain billing."""

import os
from urllib.parse import urlsplit

import httpx
import pytest
from system.fake_keystone import DIRECTORY_PROJECT_ID, DIRECTORY_TOKEN


@pytest.fixture(scope="session", autouse=True)
def fund_system_domain():
    api_base_url = os.environ["LUMEN_API_BASE_URL"]
    assert urlsplit(api_base_url).hostname == "lumen-api"
    assert urlsplit(os.environ["DATABASE_URL"]).hostname == "mariadb"
    with httpx.Client(base_url=api_base_url, timeout=30, trust_env=False) as api:
        response = api.post("/v1/admin/domain-pools/default/topups", headers={
            "X-Auth-Token": DIRECTORY_TOKEN, "X-Project-Id": DIRECTORY_PROJECT_ID,
            "Idempotency-Key": "system-test-domain-funding",
        }, json={"amount_credits": "100000"})
    assert response.status_code == 200, response.text
