"""Request-derived inference actions; credentials always come from trusted owners."""
from __future__ import annotations

from dataclasses import dataclass
from time import monotonic

from lumen.db import get_session_factory
from lumen.models.chat_runs import ChatRun, ChatRunSegment
from lumen.services import api_key_store
from lumen.services.api_key_store import ApiKeyAuthorityUnavailable, ApiKeyForbidden, authorize_api_key_in_transaction

_DISPATCH_APPROVAL_SECONDS = 5


def dispatch_deadline() -> float:
    return monotonic() + _DISPATCH_APPROVAL_SECONDS


def dispatch_expired(deadline: float) -> bool:
    return monotonic() >= deadline


def _dispatch_identity(run: ChatRun) -> tuple:
    return (
        run.id, run.user_id, run.project_id, run.api_key_id, run.agent_id, run.payer_domain_id,
        run.request_fingerprint, run.capability_snapshot,
    )


@dataclass(frozen=True)
class DispatchApproval:
    """A process-local approval for one frozen run; never persisted or reused across calls."""

    identity: tuple
    required_scopes: tuple[str, ...]
    expires_at: float

    def require_fresh(self) -> None:
        if monotonic() >= self.expires_at:
            raise ApiKeyForbidden("stale inference authorization")

    async def check(self, session, run: ChatRun) -> None:
        """Recheck only local identity and current key state under the caller's locks."""
        self.require_fresh()
        if _dispatch_identity(run) != self.identity:
            raise ApiKeyForbidden("stale inference authorization")
        if run.api_key_id is not None:
            scopes = await api_key_store.active_key_scopes_db(
                session, run.api_key_id, user_id=run.user_id, project_id=run.project_id,
            )
            if scopes is None or not scopes.issuperset(self.required_scopes):
                raise ApiKeyForbidden("API key no longer authorizes inference")
        self.require_fresh()


async def read_dispatch_candidate(session, run_id: str, segment_id: str) -> ChatRun | None:
    """Read without row locks; a checkpointed boundary does not need fresh authority."""
    run = await session.get(ChatRun, run_id)
    if run is None or run.cancel_requested_at is not None:
        return None
    segment = await session.get(ChatRunSegment, (run_id, segment_id))
    return run if segment is None or segment.status == "prepared" else None


async def preflight_dispatch(session, run: ChatRun, scopes: tuple[str, ...]) -> DispatchApproval:
    """Use live authority without any billing, run, provider or batch row locks."""
    expires_at = dispatch_deadline()
    await authorize_api_key_in_transaction(
        session, api_key_id=run.api_key_id, user_id=run.user_id, project_id=run.project_id,
        required_scopes=scopes, credential_authority=(run.capability_snapshot or {}).get("credential_authority"),
    )
    return DispatchApproval(_dispatch_identity(run), scopes, expires_at)


async def check_dispatch(session, run: ChatRun, approval: DispatchApproval | None) -> DispatchApproval:
    if approval is None:
        raise ApiKeyForbidden("new inference is not preauthorized")
    await approval.check(session, run)
    return approval


def model_required_scopes(resolved: dict | None) -> tuple[str, ...]:
    """Intrinsic model search is a tool action even without explicit request options."""
    capabilities = (resolved or {}).get("capabilities") or {}
    return ("native:tools:execute",) if capabilities.get("web_search_required") is True else ()


def cancel_run_scopes(run, payload: dict) -> tuple[str, ...]:
    """Cancel only the owned operation's generation authority, never a text-write alias."""
    kind = run.run_kind
    family = "images" if kind == "image" else "audio" if kind in {"tts", "stt"} else "realtime" if kind == "realtime" else None
    if family is not None:
        primary = {f"native:{family}:write", f"compat:{family}:write"}
        frozen = set(payload.get("required_scopes") or ()) & primary
        return tuple(sorted(frozen)) if frozen else (f"native:{family}:write",)
    return ("compat:completions:write",) if kind == "api_completion" or run.run_scope == "api" else ("native:runs:write",)


def native_run_scopes(payload: dict, *, agent_id: object = None) -> tuple[str, ...]:
    required = set(payload.get("required_scopes") or ())
    if "compat:completions:write" not in required:
        required.add("native:runs:write")
    features = payload.get("features") or {}
    extensions = payload.get("extension_snapshot") or {}
    policy = features.get("tool_policy") or {}
    if (policy.get("mode", "none") != "none"
            or any((features.get(name) or {}).get("enabled") for name in ("web_search", "web_fetch", "advisor"))
            or extensions.get("tools") or extensions.get("mcp")
            or payload.get("plugin_tool_snapshots") or payload.get("tool_schemas") or payload.get("skill_snapshot")
            or payload.get("skill_ids") or payload.get("plugin_skill_ids")
            or agent_id is not None or payload.get("execution_mode", "chat") != "chat"):
        required.add("native:tools:execute")
    if agent_id is not None:
        required.add("native:agents:use")
    if features.get("memory"):
        required.add("native:memory:read")
    if extensions.get("tools") or extensions.get("mcp") or payload.get("skill_snapshot") or payload.get("plugin_tool_snapshots") or payload.get("plugin_skill_snapshots"):
        required.add("native:extensions:read")
    context = payload.get("context_source") or {}
    if (context.get("messages") or context.get("message_ids")) and not required & {"native:conversations:read", "compat:completions:write"}:
        required.add("native:runs:read")
    modalities = set(features.get("output_modalities") or ["text"])
    if modalities & {"image", "video"}:
        required.add("native:images:write")
    if modalities & {"audio", "video"}:
        required.add("native:audio:write")
    return tuple(sorted(required | set(payload.get("required_scopes") or ())))


def completion_scopes(request: dict, *, base: tuple[str, ...] = ("compat:completions:write",), resolved: dict | None = None) -> tuple[str, ...]:
    """Includes client functions and provider builtins/search, independent of tool_choice."""
    required = set(base) | set(model_required_scopes(resolved))
    if request.get("tools") or request.get("functions") or request.get("web_search_options"):
        required.add("native:tools:execute")
    modalities = set(request.get("modalities") or request.get("output_modalities") or ())
    if modalities & {"image", "video"} or any(tool.get("type") == "image_generation" for tool in request.get("tools") or []):
        required.add("compat:images:write")
    if modalities & {"audio", "video"} or request.get("audio"):
        required.add("compat:audio:write")
    return tuple(sorted(required))


async def authorize_new_io(*, user_id: str, project_id: str, api_key_id: int | None,
                           required_scopes: tuple[str, ...], credential_authority: dict | None = None) -> None:
    factory = get_session_factory()
    if factory is None:
        raise ApiKeyAuthorityUnavailable("inference authority is unavailable")
    async with factory() as session, session.begin():
        await authorize_api_key_in_transaction(session, user_id=user_id, project_id=project_id,
            api_key_id=api_key_id, required_scopes=required_scopes, credential_authority=credential_authority)


async def authorize_tool_dispatch(ctx) -> None:
    """Non-durable dispatch revalidates; durable dispatch was fenced by tool_started.

    Rechecking after that committed intent would retroactively cancel authorized I/O.
    """
    if ctx.execution_hooks is not None:
        return
    api_key_id = None
    credential_authority = None
    if ctx.run_id is not None:
        from lumen.models.chat_runs import ChatRun
        factory = get_session_factory()
        if factory is None:
            raise ApiKeyAuthorityUnavailable("tool authority is unavailable")
        async with factory() as session:
            run = await session.get(ChatRun, ctx.run_id)
            if run is None or run.user_id != ctx.user_id or run.project_id != ctx.project_id:
                raise ApiKeyForbidden("tool owner is unavailable")
            api_key_id = run.api_key_id
            credential_authority = (run.capability_snapshot or {}).get("credential_authority")
    await authorize_new_io(user_id=ctx.user_id, project_id=ctx.project_id, api_key_id=api_key_id,
                           required_scopes=("native:tools:execute",), credential_authority=credential_authority)


def run_generation_scopes(run, *, resolved: dict | None = None) -> tuple[str, ...]:
    """Auxiliary calls need generation/history and their own intrinsic tools."""
    generation = "compat:completions:write" if run.source == "api" and run.run_scope == "api" else "native:runs:write"
    history = "native:conversations:read" if getattr(run, "conversation_id", None) is not None else "native:runs:read"
    return generation, history, *model_required_scopes(resolved)


async def authorize_run_generation(session, run, *, resolved: dict | None = None) -> None:
    await authorize_api_key_in_transaction(session, user_id=run.user_id, project_id=run.project_id,
        api_key_id=run.api_key_id, required_scopes=run_generation_scopes(run, resolved=resolved),
        credential_authority=(run.capability_snapshot or {}).get("credential_authority"))


async def authorize_run_generation_by_id(*, run_id: str, user_id: str, project_id: str, resolved: dict | None = None) -> None:
    from lumen.models.chat_runs import ChatRun

    factory = get_session_factory()
    if factory is None:
        raise ApiKeyAuthorityUnavailable("inference authority is unavailable")
    async with factory() as session, session.begin():
        run = await session.get(ChatRun, run_id)
        if run is None or run.user_id != user_id or run.project_id != project_id:
            raise ApiKeyForbidden("inference owner is unavailable")
        await authorize_run_generation(session, run, resolved=resolved)
