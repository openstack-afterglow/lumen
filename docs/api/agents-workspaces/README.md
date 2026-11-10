# Agent, 메모리, Workspace, Asset

이 그룹은 대화 밖에서 쓰는 자원을 다룬다. 실행에 붙이는 agent 정의, 장기 메모리, 파일 workspace, code workspace, 업로드 asset이다. 대화 완료 요청이 이 자원들을 참조한다(`agent_id`, `code_workspace_id`, `skill_ids`).

## Agent

- agent는 프로젝트 안의 실행 설정 묶음이다. 생성·조회·수정·삭제는 `/v1/agents` 계열(reference 참조).
- 완료 요청에서 `agent_id`로 지정한다. 지정한 agent의 설정이 해당 실행에 적용된다.
- `agent_budget`은 한 실행 안에서 쓸 자식 실행 예산이다. 자식 실행(child run)은 부모 run과 같은 원장에 기록된다.

## 메모리

- `/v1/memories` 계열로 장기 메모리 항목을 만들고 조회·수정·삭제한다.
- 완료 요청의 `features.memory`가 `true`이면 메모리가 프롬프트에 들어간다. 기본값이 `true`이므로 최소 권한 키는 명시적으로 `false`를 보낸다.
- 메모리 접근 범위와 권한은 해당 연산의 `Scopes`와 정책을 따른다(reference 참조).

## Workspace와 Code workspace

- `/v1/workspaces`: 파일을 담는 작업 공간. 업로드·목록·삭제.
- `/v1/code-workspaces`와 `/v1/git-credentials`: 코드 실행용 workspace와 git 자격 증명. 자격 증명 값은 응답에 나오지 않는다.
- code workspace를 쓰는 run은 `execution_mode`를 코드 모드로 지정한다(`CompletionRequest.execution_mode`, `code_workspace_id`).

## Asset

- `POST /v1/assets`로 파일을 올린다. 파일은 MIME과 크기 검사, ClamAV 검사를 통과해야 저장된다.
- 저장 위치는 프로젝트별 결정적 S3 버킷이다. 버킷 이름은 프로젝트 ID의 해시에서 만들어져 원래 프로젝트 ID가 노출되지 않는다.
- 저장된 asset은 `parts`에서 참조한다(완료 요청의 asset part).

## 주의

- 자원 ID는 호출자 프로젝트 권한이 있을 때만 쓴다. 권한이 없을 때의 응답 코드는 연산별로 다르므로 reference를 본다.
- workspace와 asset 내용을 로그에 남기지 않는다.
- 정확한 요청·응답 필드는 [reference.md](reference.md)를 본다. 이 README는 사용 흐름만 설명한다.
