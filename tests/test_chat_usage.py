"""빌트인 AI 채팅 사용량 엔드포인트(/api/v1/chat/usage) 단위 테스트.

Usage routes delegate to payer-scoped ledger queries.
"""

import pytest
from httpx import ASGITransport, AsyncClient

import lumen.api.usage as usage_mod
from lumen.main import app
from lumen.services import usage as usage_service


@pytest.mark.asyncio
async def test_get_chat_usage_unauthenticated():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.get("/v1/usage")
    assert resp.status_code == 401


def test_empty_usage_summary_exposes_weekly_contract():
    assert usage_service._EMPTY["week_credited_cost"] == 0.0
    assert usage_service._EMPTY["quota_weekly_max"] == 0.0


@pytest.mark.asyncio
async def test_get_chat_usage_summary(client, monkeypatch):
    summary = {
        "found": True,
        "total_credited_cost": 3.5,
        "prompt_tokens": 120,
        "completion_tokens": 45,
        "request_count": 7,
    }

    async def fake_summary(view):
        assert view == usage_service.UsageView("test-user-123")
        return summary

    monkeypatch.setattr(usage_mod, "user_usage_summary", fake_summary)
    resp = await client.get("/api/v1/chat/usage")
    assert resp.status_code == 200
    assert resp.json() == summary


@pytest.mark.asyncio
async def test_get_chat_usage_scoped_to_token(client, monkeypatch):
    captured = {}

    async def fake_summary(view):
        captured["user_id"] = view.user_id
        captured["scope"] = view.scope
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
    # The default view is the caller's own usage, not the domain pool.
    assert captured["scope"] == "self"


@pytest.mark.asyncio
async def test_usage_timeseries(client, monkeypatch):
    from lumen.services import stats as stats_service

    captured = {}

    async def fake_ts(bucket, rng, pid, *, include_system, view):
        captured.update(bucket=bucket, rng=rng, user_id=view.user_id, include_system=include_system)
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

    async def fake_ts(bucket, rng, pid, *, include_system, view):
        seen["bucket"] = bucket
        return []

    monkeypatch.setattr(stats_service, "timeseries", fake_ts)
    resp = await client.get("/api/v1/chat/usage/timeseries?bucket=bogus")
    assert resp.status_code == 200 and seen["bucket"] == "day"


@pytest.mark.asyncio
async def test_usage_keys(client, monkeypatch):
    from lumen.services import stats as stats_service

    captured = {}

    async def fake_keys(rng, pid, *, view):
        captured["user_id"] = view.user_id
        return [{"api_key_id": 3, "name": "k", "total_tokens": 5}]

    monkeypatch.setattr(stats_service, "by_api_key", fake_keys)
    resp = await client.get("/api/v1/chat/usage/keys")
    assert resp.status_code == 200
    assert resp.json()["keys"][0]["api_key_id"] == 3
    assert captured["user_id"] == "test-user-123"
