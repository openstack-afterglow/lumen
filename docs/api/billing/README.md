# 사용량, 쿼터, 통계

## 사용량 기록

`GET /v1/usage/records?limit=1..100&before_id=&source=web|api`는 최신순 기록과 `next_before_id`를 돌려준다.

- API 키 호출자는 자기 `api_key_id`에 해당하는 기록만 본다. `source`는 `api`로 강제된다.
- 각 기록의 `uncached_input_tokens`, `cache_read_input_tokens`, `cache_creation_5m_input_tokens`, `cache_creation_1h_input_tokens`로 프롬프트 캐시 적중을 확인한다.
- `credited_cost`는 해당 호출의 크레딧 차감액이다.

```sh
curl -sS "http://127.0.0.1:8012/v1/usage/records?limit=10" \
  -H "Authorization: Bearer $LUMEN_API_KEY"
```

## 쿼터

- API 키에 `monthly_credit_limit`, `weekly_credit_limit`을 설정할 수 있다. 미래의 시각만 `expires_at`으로 허용되고 UTC로 정규화된다.
- 프로젝트 쿼터는 관리자 `/v1/admin/quotas` 계열로 설정한다. 사용자별 `NULL`은 상속, 양수는 재정의, `0`은 무제한이다.
- 한도에 도달하면 완료 요청이 402로 거부된다. 사유는 `detail`에 있다.
- 예약(hold)은 실행 전에 잡히고 실행 후 실제 사용량으로 정산된다. 취소 전에 제공자 결과가 이미 관측된 과금은 취소로 되돌아가지 않는다.

## 통계

`GET /v1/admin/stats`는 관리자용 집계다. 사용자별 기간, 모델, 웹/API 구분, 토큰, 비용 드릴다운을 제공한다. 일반 사용자는 접근할 수 없다. 응답 필드는 [reference.md](reference.md)를 본다.

## 주의

- 사용량 기록은 과금 근거다. 제공자 청구서와 자동으로 대조되지 않는다. 대조는 운영자가 한다.
- 단가가 없는 모델은 비용이 0으로 계산되지만 partial로 표시된다. 무료 사용으로 간주하지 않는다. 단가 설정은 [admin](../admin/README.md)을 본다.
