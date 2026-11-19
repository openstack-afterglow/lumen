## Why

The owner requires actual Playwright and Afterglow tool calls through Lumen chat, with monitored execution. The latest direction supersedes the earlier permanent Playwright gateway/broker, Zun runtime installation and compute-only digest patch: use a Kubernetes cluster provisioned and lifecycle-managed by Drover in the existing `lumen-service` OpenStack project, create isolated execution sandboxes on demand, prepare only the tools needed by that execution, and reclaim each sandbox afterwards. Future server-side tools and MCP subprocesses must reuse this mechanism rather than each requiring a permanent dedicated server.

## What Changes

- Preserve legacy v1 explicit-read-only remote MCP dispatch, frozen owner/project/configuration/credential epochs, typed failures and existing v2 approval decisions. Reconcile checkpointed approval requests against the existing MariaDB ledger before resuming after a worker crash; never replay a tool or borrow a previous approval round's decisions.
- Preserve ordinary v2 chat wallet billing without requiring an implicit managed-agent budget. Explicit root/child/plan/code ceilings and operator sandbox quotas remain separate enforced limits.
- Extend the existing durable resource intent, pool ownership/fences, unknown-create reconciliation, readiness, run assignment and terminal deletion path with a typed Kubernetes sandbox provider. No second run queue, authority ledger or tool-specific allocator.
- Use an operator-approved immutable execution image with a generic agent-server/MCP process boundary. Launch only selected approved tool commands inside the execution sandbox; preserve per-run process/workspace/browser state across calls and destroy it at the terminal/deadline boundary. Tools are prepared in pinned images or an explicitly bounded, verified preparation step, never arbitrary model-selected host installs.
- Separate Kubernetes-native isolation from the existing root/delegated-cgroup Nova sandbox contract. Require a qualified sandbox RuntimeClass, enforced namespace network policy, non-root processes, dropped capabilities, no privilege escalation, read-only root filesystem, finite CPU/memory/PID/workspace/lifetime bounds, no host namespaces/hostPath/Docker socket, and no workload service-account or operator/cloud/provider credentials. Do not claim a policy object proves CNI enforcement.
- Keep browser egress public-only and DNS/IP-rebinding safe. Existing no-network code execution must not silently gain browsing egress. Do not expose a new public MCP endpoint or require DNS/HAProxy changes for sandbox-internal stdio tools.
- Gate lazy tool resource preparation and dispatch by the current authenticated run lease and approved tool identity. Fixed Kolla workers need a dedicated authenticated controller route; they must not receive the controller's cluster credential, CA private key or operator dispatch key.
- Reuse existing native catalog/selection and approval semantics for generic sandbox-backed tools; preserve remote Afterglow MCP authority and OAuth boundaries. Protocol 1 and sandbox activation remain default-off until the matching API/worker/controller/runtime cohort is qualified.
- Remove superseded permanent Playwright/Zun deployment assets, digest image publication and protected permanent-endpoint cleanup settings and callers. Retain historical private receipts only as evidence of the superseded design.

## Capabilities

### New Capabilities

- Run-scoped, on-demand Kubernetes execution sandbox with generic approved tool/MCP preparation, authenticated dispatch, bounded lifecycle and deterministic reclamation.
- Playwright as the first sandbox-hosted stdio MCP workload, with real Chromium navigation, multi-call state, tenant separation and public-only egress.

### Modified Capabilities

- Existing native tool discovery/selection, frozen authority, errors, approval/recovery, resource waiting and terminal cleanup.
- Ordinary native v2 chat remains usable without an explicit managed-agent budget; explicit managed allocations remain enforced.

## Impact

Affected core/plugin contracts, infrastructure configuration/provider/controller, standalone execution runtime, canonical image build/publication, Kolla inputs and architecture/security/operations documentation change together. Candidate release is 0.6.9; immutable changed wheel versions and all plugin pins/manifests/allowlists must match the final source cohort. Schema impact must be established from the implementation and compared with the deployed migration ledger; do not assume the earlier no-migration assessment covers the new architecture.

OpenHands V1 informs the agent-server boundary, prepared images, readiness and lifecycle management, not a dependency on its Enterprise Runtime API, CRDs, warm pools, default unauthenticated service or V0 dynamic runtime-image workflow. Primary sources: https://docs.openhands.dev/openhands/usage/advanced/custom-sandbox-guide ; https://docs.openhands.dev/sdk/arch/agent-server ; https://docs.openhands.dev/sdk/guides/agent-server/docker-sandbox .

## Evidence and activation boundary

Existing private `chat-mcp-20261015/{browser-linux,oci-linux,lumen-native,native-zun-digest}` receipts concern the superseded design and do not qualify Kubernetes, the final generic runtime or production tool execution. Common approval/frozen-authority fixes are retained but require final native acceptance. Kubernetes provider/runtime source is implemented; live Drover cluster creation, credentials, reachability, RuntimeClass and actual network enforcement remain qualification gates. An unrelated Afterglow Notion worker namespace is not evidence of a Lumen sandbox cluster.

Enable in order: identify the explicit connected target and safe local qualification environment → update source/configuration and remove superseded paths → actual multiarchitecture image builds and canonical local Kubernetes deployment → authenticated lazy provision/prepare/call/approval/recovery/cleanup and isolation proof → required gates/exact-SHA CI and immutable publication → approved recovery-tested Kolla/controller/cluster activation → explicitly selected real Playwright and Afterglow chat calls and monitored logs/state. No production Zun, Docker runtime, HAProxy or DNS mutation is part of this replacement design.

### Owner-selected Drover boundary (2026-10-15)

- The selected project is `lumen-service` (`f767dc0f940d4fca87ff6a79d86e7a35`), confirmed through Keystone and project-scoped Drover API inventory. Its inventory was empty before provisioning; do not adopt or delete the two unrelated ERROR clusters.
- Drover owns durable cluster/node creation and the kubeconfig handoff. Lumen's existing controller owns only bounded per-run Pods and their exact-owned Secrets/NetworkPolicies; chat admission does not create clusters or receive cloud credentials.
- Preserve the existing `lumen` service account and role assignments. The dedicated `lumen-sandbox-infra` principal has project-only `member`, `drover-inventory_reader`, `drover-clusters_editor`, and `drover-access_admin`; no `admin`/`manager`, system assignment, or foreign-project role. Its credential remains in protected operator storage outside container mounts and source control.
- Use the explicitly selected private provider network and a dedicated restricted sandbox namespace. Controller credentials must be namespace-scoped, not the Drover administrator kubeconfig. Stock runc and a NetworkPolicy object's existence are not isolation qualification; keep activation fail-closed until measured runtime and CNI evidence exists.

