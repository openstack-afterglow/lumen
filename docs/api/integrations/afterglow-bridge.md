# Afterglow 브리지 계약

Afterglow는 위임 MCP 권한(delegated grant)의 소유자다. Lumen은 이 권한을 직접 저장하지 않는다. Lumen은 실행 시점마다 Afterglow의 브리지 엔드포인트를 호출해 현재 선택된 권한을 확인하고, 도구 호출을 중계한다. 이 문서는 두 저장소가 맞춰야 할 계약을 정리한다. Afterglow 수정 작업의 기준이다.

## 1. Lumen → Afterglow 호출

- 기준 URL: Lumen 설정 `mcp_control_plane_url` + `/api/v1/mcp/lumen`. 소스: `lumen/services/mcp_adapter.py::_bridge_base_url`.
- 인증: `Authorization: Bearer <lumen_mcp_service_token>`. 토큰은 Lumen 설정 `lumen_mcp_service_token`과 Afterglow 설정 `lumen_mcp_service_token`이 같아야 한다.
- 엔드포인트: `POST /snapshot`, `POST /preview`, `POST /execute`.

### `POST /api/v1/mcp/lumen/snapshot`

요청: `{"user_id": "<owner>", "project_id": "<project>"}`

응답(현재 계약):

```json
{
  "snapshots": [
    {
      "snapshot": {"grant_id": "...", "user_id": "...", "project_id": "...",
                   "credential_epoch": 3, "selection_generation": 7},
      "entries": [{"name": "afterglow_vm_list", "description": "...",
                   "input_schema": {"type": "object", "properties": {}}, "effect": "read"}],
      "service_fingerprint": "<64 hex>"
    }
  ],
  "snapshot": {"grant_id": "...", "...": "첫 번째 권한과 동일"},
  "entries": [],
  "service_fingerprint": "<64 hex>"
}
```

- `snapshots`: 소유자가 켠 권한 전부. 순서는 Afterglow가 정한다. 잘못된 권한은 빠지고 나머지는 남는다(fail-closed per grant).
- 최상위 `snapshot`, `entries`, `service_fingerprint`: 이전 Lumen과의 호환용. 첫 번째 권한과 같다. 권한이 없으면 `snapshot`은 `null`, `entries`는 `[]`, `snapshots`는 `[]`다.
- Lumen은 `snapshots`가 있으면 그 목록을 쓰고, 없으면 최상위 필드만 본다.

### `POST /preview`, `POST /execute`

요청: `{"snapshot": {...}, "name": "<tool>", "arguments": {...}}`. 변경 도구는 `idempotency_key`를 추가로 보낸다. Afterglow는 스냅샷의 `grant_id`, `credential_epoch`, `selection_generation`이 현재 값과 같은지 확인한 뒤에만 실행한다. 하나라도 다르면 실행하지 않는다.

## 2. 권한 선택 API (사용자 브라우저 세션)

Afterglow 제품 UI가 호출하는 경로다.

| 메서드 | 경로 | 동작 |
| --- | --- | --- |
| `PUT` | `/api/v1/auth/mcp-tokens/{token_id}/lumen-default` | 이 토큰의 권한을 Lumen 선택에 추가한다. 소유자의 다른 권한은 유지된다. |
| `DELETE` | `/api/v1/auth/mcp-tokens/{token_id}/lumen-default` | 이 권한 하나만 끈다. 다른 권한은 유지된다. |
| `DELETE` | `/api/v1/auth/mcp-tokens/lumen-default` | 소유자의 모든 Lumen 선택을 끈다. |

모든 변경은 `_require_browser_mutation`을 통과해야 한다(브라우저 세션에서만 호출).

## 3. 저장 계약

- 테이블 `mcp_lumen_selections`는 기본키 `(owner_user_id, owner_project_id, grant_id)`를 쓴다. 마이그레이션 `backend/migrations/110_mcp_lumen_multigrant.sql`이 이를 바꾼다. 적용 전에는 권한이 소유자당 하나로 제한된다.
- 선택이 바뀌면 소유자 전체의 `lumen_selection_generation`이 올라간다. 그 소유자의 모든 이전 스냅샷은 무효가 된다. 보수적인 동작이며 의도된 것이다.
- 기본 선택 흐름(`defaults._select`)은 오래된 선택만 교체하고, 사용자가 켠 다른 권한을 끄지 않는다.

## 4. 배포 순서

1. Afterglow 마이그레이션 110 적용.
2. Afterglow 브리지(`/snapshot`의 `snapshots` 필드) 배포.
3. Lumen 배포.

반대 순서로 배포해도 Lumen은 최상위 필드로 동작한다. 다만 소유자가 여러 권한을 켜도 첫 번째 권한만 쓰인다.

## 5. Afterglow에 남은 작업

- 프론트엔드: 새 권한별 `DELETE`를 호출하는 UI 연결은 이전 작업에서 하지 않은 남은 항목이다. 현재 프론트엔드가 실제로 호출하는 엔드포인트는 이 문서 작성 때 확인하지 않았다. 연결 후 검증이 필요하다.
- 문서와 변경 이력: 마이그레이션 110과 브리지 `snapshots` 필드가 Afterglow 문서에 없다. 이 절을 Afterglow 연동 가이드에 옮긴다.
- 운영 검증: 실제 Afterglow DB에서 110을 적용하고 두 권한을 켜서 `/snapshot`이 두 항목을 돌려주는지 확인한 기록이 없다. 로컬 격리 MariaDB(11.4)에서는 확인했다.

## 6. Lumen 쪽 동작 요약

- 권한이 하나면 도구 이름은 그대로다(`afterglow_vm_list`).
- 권한이 둘 이상이면 도구 이름에 grant ID 앞 8자리를 붙인다(`afterglow_vm_list__1a2b3c4d`). 이름은 각 권한 안에서 고유하다.
- 권한 하나가 해석되지 않으면 그 권한의 도구만 빠지고 나머지 접미사는 그대로 유지된다.
- 실행 시점 스냅샷은 목록으로 고정된다. 예전 단일 객체 형태도 읽는다.
