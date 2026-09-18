# 아키텍처 상세 안내

Lumen 아키텍처의 정본은 루트 [`ARCHITECTURE.md`](../ARCHITECTURE.md)다. 현재 구현·ownership·저장소 정합성·검증 수준·maintenance 절차는 root 문서를 먼저 읽는다.

이 페이지는 에이전트/도구 경계의 상세 설명으로 남긴다.

- [`docs/agent-platform.md`](agent-platform.md): tool binding/selection/dispatch, MCP, skill, memory scope와 protocol v1/v2
- [`docs/afterglow-integration.md`](afterglow-integration.md): OpenAI/Anthropic compatibility, native durable API, 인증·scope·SSE 계약
- [`docs/operations.md`](operations.md): MariaDB journal, Redis wakeup, worker lease, migration과 deployment
- [`docs/security.md`](security.md): principal, secret, SSRF/MCP, durable snapshot trust boundary

문서 갱신 시 root `ARCHITECTURE.md`를 source와 함께 검토하고, detail 문서의 설명이 root와 다르면 현재 source에 맞춰 둘 다 갱신한다. 계획이나 테스트 정의를 구현·test-passed·live-verified 증거로 승격하지 않는다.
