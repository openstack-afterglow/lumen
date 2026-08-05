"""빌트인 AI 채팅 사용량 조회 엔드포인트 (사용자 본인).

- GET /usage           : 누적/월 요약 + 접근경로(web/api) 분해.
- GET /usage/timeseries: 시간버킷(hour/day/month) × source 시계열(본인).
- GET /usage/keys      : API 키별 사용량(본인).
모두 chat_usage_logs 원장에서 user_id 기준 집계. 저장소 장애 시에도 안전(빈/found=False) 200.
"""

from fastapi import APIRouter, Depends, Query

from lumen.auth import get_token_info
from lumen.services import stats as stats_service
from lumen.services.usage import user_usage_summary

router = APIRouter()

_RANGES = ("30d", "90d", "1y", "all")
_BUCKETS = ("hour", "day", "month")


@router.get("/usage")
async def get_chat_usage(token_info: dict = Depends(get_token_info)):
    """로그인 사용자 본인의 빌트인 채팅 사용량 요약. 데이터/DB 없으면 found=False 로 200 반환."""
    return await user_usage_summary(token_info.get("user_id", ""), token_info.get("project_id", ""))


@router.get("/usage/timeseries")
async def get_usage_timeseries(
    bucket: str = Query(default="day"),
    range: str = Query(default="30d"),
    token_info: dict = Depends(get_token_info),
):
    """본인 사용량 시계열 — 시간/일/월 버킷 × source(web/api). 시스템 부담 제외."""
    b = bucket if bucket in _BUCKETS else "day"
    rng = range if range in _RANGES else "30d"
    series = await stats_service.timeseries(b, rng, None, user_id=token_info.get("user_id", ""), include_system=False)
    return {"bucket": b, "range": rng, "series": series}


@router.get("/usage/keys")
async def get_usage_keys(
    range: str = Query(default="all"),
    token_info: dict = Depends(get_token_info),
):
    """본인 API 키별 사용량(source="api")."""
    rng = range if range in _RANGES else "all"
    return {"keys": await stats_service.by_api_key(rng, None, user_id=token_info.get("user_id", ""))}
