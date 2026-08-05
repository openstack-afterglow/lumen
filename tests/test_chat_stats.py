"""빌트인 AI 채팅 관리자 통계 라우터 테스트.

DB 없이 stats 서비스를 monkeypatch 하여 라우터 계약만 검증:
- require_admin 게이트(비관리자 403)
- range 정규화(미지원 값 → all), project_id 전달
- 응답 묶음(overview/by_model/monthly/by_user/projects) 구조
- 사용자 페이지네이션 엔드포인트
"""

from types import SimpleNamespace

import pytest

from lumen.services import stats as stats_service

_URL = "/api/v1/chat/admin/stats"


class TestAdminGate:
    async def test_stats_forbidden_for_non_admin(self, non_admin_client):
        resp = await non_admin_client.get(_URL)
        assert resp.status_code == 403

    async def test_stats_users_forbidden_for_non_admin(self, non_admin_client):
        resp = await non_admin_client.get(f"{_URL}/users")
        assert resp.status_code == 403


class TestStatsBundle:
    async def test_bundle_shape_and_range_normalization(self, admin_client, monkeypatch):
        captured = {}

        async def fake_overview(rng, pid):
            captured["overview"] = (rng, pid)
            return {"total_tokens": 42, "active_users": 3}

        async def fake_by_model(rng, pid):
            return [{"model_name": "gpt-4o", "total_tokens": 42}]

        async def fake_monthly(rng, pid):
            return [{"month": "2026-07", "ts": 1, "total_tokens": 42}]

        async def fake_by_user(rng, pid, limit=20, offset=0):
            captured["by_user_limit"] = limit
            return [{"user_id": "u1", "total_tokens": 42}]

        async def fake_projects(rng):
            return ["p1", "p2"]

        monkeypatch.setattr(stats_service, "overview", fake_overview)
        monkeypatch.setattr(stats_service, "by_model", fake_by_model)
        monkeypatch.setattr(stats_service, "monthly", fake_monthly)
        monkeypatch.setattr(stats_service, "by_user", fake_by_user)
        monkeypatch.setattr(stats_service, "projects_with_usage", fake_projects)

        # 미지원 range 는 'all' 로 정규화되어야 함
        resp = await admin_client.get(f"{_URL}?range=bogus&project_id=p1&user_limit=5")
        assert resp.status_code == 200
        body = resp.json()
        assert body["range"] == "all"
        assert body["project_id"] == "p1"
        assert captured["overview"] == ("all", "p1")
        assert captured["by_user_limit"] == 5
        assert body["overview"]["total_tokens"] == 42
        assert body["by_model"][0]["model_name"] == "gpt-4o"
        assert body["monthly"][0]["month"] == "2026-07"
        assert body["by_user"][0]["user_id"] == "u1"
        assert body["projects"] == [{"id": "p1", "name": None}, {"id": "p2", "name": None}]

    async def test_valid_range_preserved(self, admin_client, monkeypatch):
        captured = {}

        async def fake_overview(rng, pid):
            captured["rng"] = rng
            return {}

        async def _empty(*_args, **_kwargs):
            return []

        monkeypatch.setattr(stats_service, "overview", fake_overview)
        monkeypatch.setattr(stats_service, "by_model", _empty)
        monkeypatch.setattr(stats_service, "monthly", _empty)
        monkeypatch.setattr(stats_service, "by_user", _empty)
        monkeypatch.setattr(stats_service, "projects_with_usage", _empty)

        resp = await admin_client.get(f"{_URL}?range=30d")
        assert resp.status_code == 200
        assert captured["rng"] == "30d"

    async def test_bundle_returns_display_names_for_user_and_project(self, admin_client, mock_conn, monkeypatch):
        async def _empty(*_args, **_kwargs):
            return []

        async def users(*_args, **_kwargs):
            return [{"user_id": "u1", "total_tokens": 42}]

        async def projects(*_args, **_kwargs):
            return ["p1"]

        for name in ("overview", "by_model", "monthly", "timeseries", "by_api_key"):
            monkeypatch.setattr(stats_service, name, _empty)
        monkeypatch.setattr(stats_service, "by_user", users)
        monkeypatch.setattr(stats_service, "projects_with_usage", projects)
        mock_conn.identity.users.return_value = [SimpleNamespace(id="u1", name="홍길동")]
        mock_conn.identity.projects.return_value = [SimpleNamespace(id="p1", name="개발 프로젝트")]

        response = await admin_client.get(_URL)

        assert response.status_code == 200
        assert response.json()["by_user"][0]["user_name"] == "홍길동"
        assert response.json()["projects"] == [{"id": "p1", "name": "개발 프로젝트"}]


class TestStatsUsers:
    async def test_users_pagination(self, admin_client, monkeypatch):
        captured = {}

        async def fake_by_user(rng, pid, limit=20, offset=0):
            captured.update({"rng": rng, "pid": pid, "limit": limit, "offset": offset})
            return [{"user_id": "u1"}]

        monkeypatch.setattr(stats_service, "by_user", fake_by_user)
        resp = await admin_client.get(f"{_URL}/users?range=90d&project_id=p9&limit=50&offset=100")
        assert resp.status_code == 200
        assert resp.json()["users"][0]["user_id"] == "u1"
        assert captured == {"rng": "90d", "pid": "p9", "limit": 50, "offset": 100}


class TestAccessPathStats:
    async def test_bundle_includes_timeseries_and_by_api_key(self, admin_client, monkeypatch):
        captured = {}

        async def _empty(*a, **k):
            return []

        async def fake_ts(bucket, rng, pid, *, model_name=None):
            captured["bucket"] = bucket
            return [{"bucket": "2026-07-21 10:00:00", "source": "api", "total_tokens": 5}]

        async def fake_keys(rng, pid):
            return [{"api_key_id": 1, "name": "k", "total_tokens": 5}]

        for name in ("overview", "by_model", "monthly", "by_user", "projects_with_usage"):
            monkeypatch.setattr(stats_service, name, _empty)
        monkeypatch.setattr(stats_service, "timeseries", fake_ts)
        monkeypatch.setattr(stats_service, "by_api_key", fake_keys)

        resp = await admin_client.get(f"{_URL}?bucket=hour")
        assert resp.status_code == 200
        body = resp.json()
        assert body["bucket"] == "hour" and captured["bucket"] == "hour"
        assert body["timeseries"][0]["source"] == "api"
        assert body["by_api_key"][0]["api_key_id"] == 1

    async def test_timeseries_endpoint(self, admin_client, monkeypatch):
        async def fake_ts(bucket, rng, pid, *, model_name=None):
            return [{"bucket": "2026-07-21", "source": "web", "total_tokens": 9}]

        monkeypatch.setattr(stats_service, "timeseries", fake_ts)
        resp = await admin_client.get(f"{_URL}/timeseries?bucket=day&range=30d")
        assert resp.status_code == 200 and resp.json()["series"][0]["source"] == "web"

    async def test_timeseries_model_filter(self, admin_client, monkeypatch):
        captured = {}

        async def fake_ts(bucket, rng, pid, *, model_name=None):
            captured.update({"bucket": bucket, "model_name": model_name})
            return []

        monkeypatch.setattr(stats_service, "timeseries", fake_ts)
        resp = await admin_client.get(f"{_URL}/timeseries?bucket=hour&model_name=gpt-5.4")
        assert resp.status_code == 200
        assert captured == {"bucket": "hour", "model_name": "gpt-5.4"}

    @pytest.mark.parametrize("bucket", ["5m", "15m"])
    async def test_timeseries_fine_bucket(self, admin_client, monkeypatch, bucket):
        captured = {}

        async def fake_ts(actual_bucket, rng, pid, *, model_name=None):
            captured["bucket"] = actual_bucket
            return []

        monkeypatch.setattr(stats_service, "timeseries", fake_ts)
        resp = await admin_client.get(f"{_URL}/timeseries?bucket={bucket}")
        assert resp.status_code == 200
        assert captured["bucket"] == bucket

    async def test_timeseries_forbidden_for_non_admin(self, non_admin_client):
        assert (await non_admin_client.get(f"{_URL}/timeseries")).status_code == 403
