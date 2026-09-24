# Lumen 작업 규칙

## Architecture maintenance

- 작업 전에 루트 [`ARCHITECTURE.md`](ARCHITECTURE.md)를 읽는다. 이 문서가 현재 구현의 정본이며 계획·roadmap·예시 문서보다 source가 우선한다.
- code/config/schema/dependency/deploy/test를 바꾸면 영향받는 `ARCHITECTURE.md` 본문과 상세 문서를 같은 변경에서 갱신한다. 구조 영향이 없는 bugfix/refactor도 최신 review summary에 영향 없음의 이유를 남긴다.
- `lumen/api/`, `lumen/services/chat_admission.py`, `lumen/services/durable_runs/`, `lumen/services/providers/`, `lumen/services/tool_runtime/`, `sdk/`, `deploy/`의 책임·저장소·계약을 source에서 확인하고 문서의 status와 evidence를 실제로 관찰한 수준으로 구분한다. 테스트 파일 존재는 `test-defined`일 뿐 `test-passed`가 아니다.
- 실제 source를 검토한 뒤 canonical guard를 stamp하고, 작업 완료 또는 commit 전에 다음 검사를 실행한다.

```bash
python3 scripts/check_architecture.py --stamp --summary "검토한 변경 경로와 구조 영향 또는 영향 없음의 이유"
python3 scripts/check_architecture.py
```

staged 제출 범위만 검토할 때는 index source를 기준으로 stamp한 뒤 문서를 stage하고 다시 검사한다.

```bash
python3 scripts/check_architecture.py --stamp --staged --summary "검토한 staged 변경 경로와 구조 영향"
python3 scripts/check_architecture.py --staged
```

- 검토 marker의 digest·UTC timestamp·summary는 실제 guard stamp로만 갱신한다. 자격증명, token, raw secret을 architecture 문서나 로그에 넣지 않는다.
- API/SSE 연결은 durable execution lifetime을 소유하지 않는다. MariaDB journal이 정본이고 Redis는 wakeup/cache 최적화임을 유지한다.
- 외부 provider, Keystone/OpenStack, KVM, 실제 배포를 관찰하지 않았다면 `live-verified`로 쓰지 않는다. fake-provider system test를 live provider 증거로 승격하지 않는다.

## 변경 방향

HTTP route는 auth/scope, 입력 parsing, HTTP error/SSE만 맡긴다. `chat_admission`은 request-independent admission과 immutable snapshot을, `durable_runs`는 journal/admission/lifecycle/execution을, `tool_runtime`은 binding/selection/dispatch를, `providers`는 repository/routing을 소유한다. API가 ORM private helper를 우회하거나 불필요한 package facade를 추가하지 않는다.

Migration은 additive로 작성하고 `lumen/migrations/manifest.txt`의 checksum을 함께 갱신한다. 적용된 migration과 checksum은 수정하지 않는다. provider/extension/secret 변경은 encryption·scope·worker revalidation 경계를 함께 검토한다. 형제 checkout을 import하거나 network dependency를 새로 만들지 않는다.

## CI 파이프라인 성능 규정 (critical-path first)

근거: 2026-09 lumen CI 실측(워크플로우별 최근 성공 실행 20건), afterglow CI 실측, Linear의 CI 개편 사례. CI를 바꾸는 모든 변경은 아래 규칙을 따른다.

기준선(변경 전 실측):
- **12번 규칙의 기준**: `docker-build.yml` 테스트 구간(실행 생성부터 마지막 `test / *` 잡 종료까지, 성공 20건, 2026-09-11~09-23): 중앙값 176초, p90 217초.
- `docker-build.yml` 전체(실행 생성부터 Build & Push 종료까지, 같은 20건): 중앙값 558초, p90 665초.
- 크리티컬 잡은 `Process-system integration`이다. 잡 시간 중앙값은 `ci.yml` 20건에서 168초, `docker-build.yml` 20건에서 172초다.
- 과거 지표: 독립 `ci.yml` 크리티컬 패스(실행 생성부터 마지막 테스트 잡 종료까지, 성공 20건, 2026-09-07~09-23)는 중앙값 171초, p90 194초였다. 이 변경 뒤 `ci.yml`은 단독 push/PR 실행이 없으므로 이 지표는 다시 잴 수 없고 비교 기준으로 쓰지 않는다.

이 저장소는 public이고 GitHub-hosted runner만 쓴다.

워크플로우 구조:
- `docker-build.yml`은 `main`/`dev` push·PR의 유일한 테스트 진입점이다. 재사용 워크플로우 `ci.yml`(`workflow_call`/`workflow_dispatch` 전용)을 한 번 호출하고, 이미지 빌드를 그 결과로 게이트한다.
- `v*` tag push에서는 `docker-build.yml`과 `release.yml`이 각각 `ci.yml`을 호출하므로 테스트가 두 번 돈다(알려진 중복, 미해결).
- `ci.yml`의 모든 잡은 `if: ${{ !cancelled() }}`를 갖는다.
  - 호출자 `test` 잡은 push·tag·dispatch에서 skip되는 `dedup`을 `needs`로 가진다. 암묵적 `success()`는 skip된 조상 때문에 잡을 건너뛸 수 있다(actions/runner#2205). GitHub이 호출자의 조상을 재사용 워크플로우 내부 잡에 반영하는지는 미확인이므로 명시적 status 함수로 막는다.
  - 내부 잡끼리는 `needs`가 없으므로 이 조건이 실패를 가리지 않는다.
  - 이 변경 뒤 첫 `dev` push 실행에서 `test / *` 잡이 모두 실제로 실행되고 `build-and-push`가 게시했는지 확인한다.
- 이 구조는 `tests/test_ci_shape.py`와 `tests/test_test_layers.py`가 고정한다.

1. **측정 먼저, 추정 금지.**
   - CI를 바꾸기 전과 후에 최근 20회 이상 실행의 잡·스텝 시간을 `gh run list --workflow docker-build.yml`과 `gh api repos/openstack-afterglow/lumen/actions/runs/<id>/jobs`로 수집한다.
   - 크리티컬 패스의 중앙값과 p90을 변경 기록(OpenSpec proposal, PR, 커밋 본문)에 남긴다.
   - 크리티컬 패스는 `docker-build.yml` 실행 생성부터 마지막 `test / *` 잡 종료까지로 잰다. `dedup`이 중복으로 판정해 테스트 잡이 없는 PR 실행은 표본에서 뺀다.
   - 절감 효과는 합산되지 않는다. 가장 긴 잡부터 줄이고, 효과는 실제 CI 전후 수치로만 주장한다.
2. **목표 지표를 먼저 정한다.**
   - public 저장소가 무료 GitHub-hosted runner를 쓰면 wall-clock(대기 시간)이 목표다.
   - 그래도 공유 자원인 잡 동시성(무료 플랜 약 20개)과 10GB GHA cache quota는 낭비하지 않는다.
   - private 저장소나 유료 runner라면 runner-minutes(비용)도 함께 본다.
3. **게이트 잡을 다른 잡 앞에 두지 않는다.**
   - architecture guard 같은 fail-fast 검사는 테스트 잡의 `needs:`로 걸지 않고 병렬 잡 안에서 실행한다. 현재 guard는 `Service tests`의 첫 step이다.
   - 빌드·배포 게이팅은 테스트 워크플로우 전체 결과로 한다(`needs.test.result == 'success'`).
   - 승인된 예외는 PR 전용 `dedup` 잡(9번) 하나다. `test`가 `needs: dedup`을 갖지만, push·tag·dispatch에서는 `dedup`이 skip되어 테스트 앞 대기가 없다.
     - non-duplicate PR에서 늘어나는 대기는 약 6~9초로 추정한다(추정치, 미측정). 근거는 기준선 20건의 첫 테스트 잡 대기 중앙값 3초, `Set up job` 약 1초, tree 조회 2회, 다음 잡 대기 약 3초다.
     - `dedup` 도입 뒤 첫 non-duplicate PR들에서 `dedup` 대기+실행 시간을 실측해 변경 기록에 남긴다. 중복 PR 1건이 아끼는 시간(테스트와 이미지 빌드 검증 전체)보다 이 비용이 의미 있게 커지면 재검토한다.
4. **잡당 고정비를 측정한다.**
   - checkout, 의존성 설치, 서비스 준비 시간을 잰다. 캐시 복원이 재설치보다 느리면 캐시를 쓰지 않는다.
   - 서비스 컨테이너 health-check는 짧은 interval(2초)과 충분한 retries·start-period로 설정한다. 현재 `Datastore integration`은 interval 2초, retries 50, start-period 5초로, 기존 10초×10회(약 100초)보다 좁지 않은 약 105초 창을 유지한다.
   - 잡이 쓰지 않는 host venv는 설치하지 않는다. system 잡은 stdlib-only인 `lumen.scripts.test_layers`를 `python3`로 직접 실행한다.
   - 이미지 layer는 캐시가 실제로 hit하게 만든다.
     - apt·사용자 layer가 source COPY나 floating tag(`uv:latest`)에 의존하지 않게 한다. uv는 tag와 index digest로 고정하고 둘을 함께 수동으로 갱신한다.
     - 재귀 `chown -R` 대신 `COPY --chown`을 쓴다.
     - GHA cache entry는 그 entry를 쓴 ref, default branch, PR이면 base branch에서만 복원된다. 그래서 `dev`/`main` 실행만 export하고, PR·tag·feature branch dispatch 실행은 읽기만 한다.
   - teardown은 로그 수집과 종료 코드 결정 뒤 `--timeout 1`로 한다.
5. **샤딩은 고정비가 작을 때만 한다.**
   - pytest가 작업을 나누는 단위(테스트 파일·node)로 균형을 맞춘다.
   - 샤드 명령은 러너를 직접 호출한다. `lumen-test` 같은 래퍼 뒤에 샤드 인자를 붙이면 인자가 전달되지 않아 전체 스위트가 조용히 돌 수 있다. 샤드별 수집 테스트 수를 CI에서 검증한다.
   - 현재 service 스위트(약 1200건, 21초)는 크리티컬 패스가 아니므로 샤딩하지 않는다.
6. **격리 해제는 opt-in으로만 한다.**
   - 워커 간 모듈·전역 상태 공유 최적화(`get_settings` cache, `lumen.cache._client` 등)는 전역에 적용하지 않는다.
   - 먼저 순서를 섞어 2회 이상 실행해 상태 누수를 확인하고, 안전한 파일만 명시적으로 opt-in한다.
   - 전역 상태를 바꾼 테스트는 `monkeypatch`나 autouse fixture(`_reset_lumen_state`)로 반드시 복원한다.
7. **테스트는 hermetic해야 병렬화할 수 있다.**
   - 단위·contract 테스트는 실제 Keystone, provider, MariaDB, Redis에 접속하지 않는다. fake 또는 in-process 경계를 쓴다.
   - 로컬 `lumen.conf`나 환경 변수가 있느냐에 따라 결과나 시간이 달라지면 결함이다.
   - 병렬 실행(pytest-xdist 등)의 워커 수는 CI vCPU에 맞춰 명시한다(`-n auto` 금지).
8. **변경 감지의 diff 기준을 정확히 한다.**
   - push는 `github.event.before..github.sha`로 비교하고, zero SHA·forced push·fetch 실패 시에는 전체를 대상으로 한다. PR은 base..head로 비교한다.
   - `HEAD^1..HEAD`처럼 마지막 커밋만 보는 비교는 금지한다.
   - 발행 산출물(이미지 등)은 실제 발행된 revision을 기준으로 판단한다.
   - 현재 lumen에는 변경 감지가 없다.
9. **중복 실행은 입력 동일성으로만 제거한다.**
   - PR 테스트를 건너뛰는 조건은 모두 충족해야 한다. 같은 저장소의 브랜치에서 온 PR이고, head가 push 실행이 있는 `dev`/`main`이며, merge 트리가 head 트리와 같아야 한다. `docker-build.yml`의 `dedup` 잡이 이를 판정한다.
   - fork PR과 dependabot PR, 판정 오류는 항상 테스트한다.
   - 브랜치 이름만으로 판단하지 않는다(fork의 동명 브랜치 우회).
   - 같은 이벤트를 두 워크플로우가 각각 테스트하지 않게 `ci.yml`에는 push/PR trigger를 두지 않는다.
   - `dedup`은 3번 규칙의 승인된 예외이며 비용은 3번에 기록한다.
   - 중복 PR의 자기 `pull_request` check는 skipped로 보이고, GitHub은 skipped를 통과로 친다. 그래서 merge 전에 head SHA의 push 실행 check suite가 성공했는지 확인한다. required status check를 추가하면 이 dedup을 다시 검토한다(2026-09-24 읽기 전용 확인 시 `dev`·`main` 모두 required check가 없었다).
10. **보안: public 저장소의 `pull_request` 코드를 self-hosted runner에서 실행하지 않는다.**
    - 워크플로우 YAML의 `if:`는 PR이 수정할 수 있으므로, runner group의 저장소 제한과 fork PR 승인 설정으로도 보장한다.
    - PR이 제어하는 값(`github.head_ref`, head repository 등)은 `env:`로만 script에 전달한다.
11. **CI 형태는 계약 테스트로 고정한다.**
    - 샤드 수, 게이트 병렬성, dedup 조건, diff 기준, health interval, cache export, Dockerfile layer 순서 같은 불변식을 저장소의 테스트(`tests/test_ci_shape.py`)로 검증해 회귀를 막는다.
12. **지속 개선.**
    - CI를 바꾸는 변경에는 전후 실측을 첨부한다.
    - 다음 중 하나라도 해당하면 1번 절차로 다시 측정하고 가장 긴 잡부터 개선한다.
      - 크리티컬 패스 중앙값이 기록된 기준(`docker-build.yml` 테스트 구간 중앙값 176초)보다 20% 이상 나빠진다.
      - 테스트 수가 크게 는다.
      - 새 테스트 계층을 추가한다.
