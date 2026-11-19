## Context

Remote dev c4527de follows published v0.6.12 (9d94d0b). The primary release/lumen-0.6.5 checkout has a separate unpublished Pi engine and conflicting historical027 source. This candidate is isolated on current dev and ports only authority changes.

## Goals / Non-Goals

**Goals:** Native admin/manager/member ownership-scoped management; reader nonsecret reads; original credential attenuation and human-only global authority; one patch release.

**Non-Goals:** Pi, new chat/durable protocol identities, provider-account/schema changes, SDK/plugin version changes, IAM grant changes, publication or operations by this worker.

## Decisions

Use existing effective user/project assignment envelopes and exact native role IDs. Do not infer a role from a project name or filter legacy roots: a current native implication reaching member/reader remains authoritative. Remove special personal-project provenance because normal project membership now suffices.

Generate standard-role defaults from the existing action vocabulary. Add a non-overridable native role floor before oslo.policy so operator `@` cannot elevate reader mutations, a legacy-only role, a private/foreign owner, a key scope, or global administration. Operator rules may still narrow authority. Internal capability labels are UI labels, not IAM roles.

Recognize `openid` in the existing known-human methods set; retain all original credential metadata restrictions and the separate fresh exact system:all administrator proof. No SQL or dependency changes.

## Risks / Trade-offs

Existing operator YAML can continue denying newly valid members; deliberately inventory and update it across all API/worker/controller/bootstrap processes. Existing issued keys never acquire unstored scopes. Existing legacy grants are neither deleted nor translated; use their current native implications. Coordinate the cohort after normal drain. Published027/028 and Bifrost prerequisites remain; this release must not substitute primary-checkout SQL or Pi code.
