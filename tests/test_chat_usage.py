"""빌트인 AI 채팅 사용량 엔드포인트(/api/v1/chat/usage) 단위 테스트.

chat_usage_logs 원장 집계를 user_usage_summary 로 위임 — token_info 의 user_id/project_id 로만 스코프.
"""

import pytest
from httpx import ASGITransport, AsyncClient

import lumen.api.usage as usage_mod
from lumen.main import app


@pytest.mark.asyncio
async def test_get_chat_usage_unauthenticated():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/v1/usage")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_get_chat_usage_summary(client, monkeypatch):
    summary = {
        "found": True,
        "total_credited_cost": 3.5,
        "prompt_tokens": 120,
        "completion_tokens": 45,
        "request_count": 7,
    }

    async def fake_summary(user_id, project_id):
        return summary

    monkeypatch.setattr(usage_mod, "user_usage_summary", fake_summary)
    resp = await client.get("/api/v1/chat/usage")
    assert resp.status_code == 200
    assert resp.json() == summary


@pytest.mark.asyncio
async def test_get_chat_usage_scoped_to_token(client, monkeypatch):
    captured = {}

    async def fake_summary(user_id, project_id):
        captured["user_id"] = user_id
        captured["project_id"] = project_id
        return {
            "found": False,
            "total_credited_cost": 0.0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "request_count": 0,
        }

    monkeypatch.setattr(usage_mod, "user_usage_summary", fake_summary)
    resp = await client.get("/api/v1/chat/usage")
    assert resp.status_code == 200
    assert captured["user_id"] == "test-user-123"
    assert captured["project_id"] == "test-project-123"


@pytest.mark.asyncio
async def test_usage_timeseries(client, monkeypatch):
    from lumen.services import stats as stats_service

    captured = {}

    async def fake_ts(bucket, rng, pid, *, user_id, include_system):
        captured.update(bucket=bucket, rng=rng, user_id=user_id, include_system=include_system)
        return [{"bucket": "2026-07-21", "source": "api", "total_tokens": 10, "credited_cost": 0.1, "request_count": 2}]

    monkeypatch.setattr(stats_service, "timeseries", fake_ts)
    resp = await client.get("/api/v1/chat/usage/timeseries?bucket=day&range=30d")
    assert resp.status_code == 200
    body = resp.json()
    assert body["bucket"] == "day" and body["series"][0]["source"] == "api"
    # 본인 스코프 + 시스템 부담 제외
    assert captured["user_id"] == "test-user-123" and captured["include_system"] is False


@pytest.mark.asyncio
async def test_usage_timeseries_bucket_normalized(client, monkeypatch):
    from lumen.services import stats as stats_service

    seen = {}

    async def fake_ts(bucket, rng, pid, *, user_id, include_system):
        seen["bucket"] = bucket
        return []

    monkeypatch.setattr(stats_service, "timeseries", fake_ts)
    resp = await client.get("/api/v1/chat/usage/timeseries?bucket=bogus")
    assert resp.status_code == 200 and seen["bucket"] == "day"


@pytest.mark.asyncio
async def test_usage_keys(client, monkeypatch):
    from lumen.services import stats as stats_service

    captured = {}

    async def fake_keys(rng, pid, *, user_id, limit=50):
        captured["user_id"] = user_id
        return [{"api_key_id": 3, "name": "k", "total_tokens": 5}]

    monkeypatch.setattr(stats_service, "by_api_key", fake_keys)
    resp = await client.get("/api/v1/chat/usage/keys")
    assert resp.status_code == 200
    assert resp.json()["keys"][0]["api_key_id"] == 3
    assert captured["user_id"] == "test-user-123"
