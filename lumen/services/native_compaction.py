"""Provider-native server-side compaction options and block round-tripping.

Anthropic exposes compaction as a request-level ``context_management`` edit that
the provider re-evaluates at the start of every sampling iteration, replacing the
compacted prefix with a single ``compaction`` content block
(https://platform.claude.com/docs/en/build-with-claude/compaction).  Two
properties of that contract drive everything here:

1. The trigger is an absolute input-token count with a documented floor, not a
   percentage, so a ratio has to be resolved against the route's own context
   window before it can be sent — and a route whose ratio lands under the floor
   cannot express it at all.
2. The returned ``compaction`` block is the new head of the conversation.  A
   caller that drops it re-sends the full history and pays for the same
   compaction again on the next request, so the block must survive every
   assistant-turn reconstruction.

This module only resolves and validates values.  It never calls a provider and
never mutates conversation state; the caller decides whether to attach them.

Layering with :mod:`lumen.services.context_manager`: Lumen's own map/reduce
compaction fences on ``input_budget`` (the window minus the output and safety
reserves) and therefore fires at a strictly lower absolute token count than a
native trigger resolved against the raw window.  Lumen stays the primary,
durable mechanism and the native edit is the in-request safety net that covers
growth Lumen cannot fence — tool results appended inside a single provider
request, and rounds where Lumen's summary route is unavailable.
"""

from __future__ import annotations

from typing import Any

# Anthropic rejects a trigger below this floor, so a route whose ratio lands
# under it cannot express the requested percentage.
MIN_TRIGGER_TOKENS = 50_000
EDIT_TYPE = "compact_20260112"

# OpenAI's Responses API expresses the same idea with a different wire shape and
# a far lower floor, so the two cannot share one constant.
RESPONSES_MIN_TRIGGER_TOKENS = 1_000
RESPONSES_EDIT_TYPE = "compaction"

# Only providers whose pinned LiteLLM chat transport declares
# ``context_management`` as a supported parameter.  Anything else is removed by
# ``litellm.drop_params`` before the request is sent, which would leave the
# caller believing compaction was armed when it was silently discarded.
SUPPORTED_PROVIDERS = frozenset({"anthropic"})

# The Responses transport is a separate LiteLLM surface with its own parameter
# list, so support there is tracked separately from the chat transport.
SUPPORTED_RESPONSES_PROVIDERS = frozenset({"openai"})

# A compaction summary is provider-authored text that is sent straight back on
# the next request.  Bound it so a malformed or hostile replay payload cannot
# grow the prompt without limit.
_MAX_BLOCK_CHARS = 200_000
_MAX_BLOCKS = 8


def supported(provider_type: str | None) -> bool:
    """Return whether this transport can actually activate native compaction."""
    return provider_type in SUPPORTED_PROVIDERS


def responses_supported(provider_type: str | None) -> bool:
    """Return whether the Responses transport forwards ``context_management``."""
    return provider_type in SUPPORTED_RESPONSES_PROVIDERS


def trigger_tokens(context_limit: object, ratio: float, *, floor: int = MIN_TRIGGER_TOKENS) -> int | None:
    """Resolve the absolute trigger, or ``None`` when the ratio is unusable.

    ``None`` is returned rather than a clamped value whenever the resolved
    trigger would sit at or beyond the window, because such a trigger can never
    fire and arming it would only add a beta header and a false claim that
    compaction is active.
    """
    if not isinstance(context_limit, int) or isinstance(context_limit, bool) or context_limit <= 0:
        return None
    if not isinstance(ratio, float) or not 0.0 < ratio < 1.0:
        return None
    value = max(floor, int(context_limit * ratio))
    if value >= context_limit:
        return None
    return value


def compaction_options(
    *,
    provider_type: str | None,
    context_limit: object,
    ratio: float,
    enabled: bool = True,
) -> dict[str, Any] | None:
    """Return the ``context_management`` request value, or ``None`` when unarmed."""
    if not enabled or not supported(provider_type):
        return None
    value = trigger_tokens(context_limit, ratio)
    if value is None:
        return None
    return {"edits": [{"type": EDIT_TYPE, "trigger": {"type": "input_tokens", "value": value}}]}


def responses_compaction_options(
    *,
    provider_type: str | None,
    context_limit: object,
    ratio: float,
    enabled: bool = True,
) -> list[dict[str, Any]] | None:
    """Return the Responses-shaped ``context_management`` value, or ``None``.

    The Responses API takes a list of entries keyed by ``compact_threshold``
    rather than Anthropic's nested ``edits``/``trigger`` object, so the two
    shapes are built separately instead of being translated at the transport.
    """
    if not enabled or not responses_supported(provider_type):
        return None
    value = trigger_tokens(context_limit, ratio, floor=RESPONSES_MIN_TRIGGER_TOKENS)
    if value is None:
        return None
    return [{"type": RESPONSES_EDIT_TYPE, "compact_threshold": value}]


def resolved_context_limit(resolved: object) -> object:
    """Read the route window off a resolved compatibility model route."""
    if not isinstance(resolved, dict):
        return None
    capabilities = resolved.get("capabilities")
    if not isinstance(capabilities, dict):
        return None
    return capabilities.get("context_limit")


def _with_default(options: dict[str, Any], value: object) -> dict[str, Any]:
    # A caller that sent its own configuration owns its context strategy; this
    # only fills the gap for a caller that sent none. Overriding an explicit
    # value would silently change a compatibility client's contract.
    if value is None or "context_management" in options:
        return options
    return {**options, "context_management": value}


def anthropic_passthrough_options(
    options: dict[str, Any],
    *,
    resolved: object,
    ratio: float,
    enabled: bool = True,
) -> dict[str, Any]:
    """Arm compaction on an Anthropic passthrough request the caller left unset."""
    provider_type = resolved.get("provider_type") if isinstance(resolved, dict) else None
    return _with_default(
        options,
        compaction_options(
            provider_type=provider_type,
            context_limit=resolved_context_limit(resolved),
            ratio=ratio,
            enabled=enabled,
        ),
    )


def responses_passthrough_options(
    options: dict[str, Any],
    *,
    resolved: object,
    ratio: float,
    enabled: bool = True,
) -> dict[str, Any]:
    """Arm compaction on a Responses passthrough request the caller left unset."""
    provider_type = resolved.get("provider_type") if isinstance(resolved, dict) else None
    return _with_default(
        options,
        responses_compaction_options(
            provider_type=provider_type,
            context_limit=resolved_context_limit(resolved),
            ratio=ratio,
            enabled=enabled,
        ),
    )


def sanitize_blocks(value: object) -> list[dict[str, Any]]:
    """Return bounded, well-formed compaction blocks from provider or replay data.

    Anything that is not an exact ``{"type": "compaction", "content": <str>}``
    block is dropped rather than repaired: a half-understood block sent back to
    the provider is worse than no block, because the API treats it as the new
    head of the conversation.
    """
    if not isinstance(value, list):
        return []
    blocks: list[dict[str, Any]] = []
    for item in value:
        if len(blocks) >= _MAX_BLOCKS:
            break
        if not isinstance(item, dict) or item.get("type") != "compaction":
            continue
        content = item.get("content")
        if not isinstance(content, str) or not content or len(content) > _MAX_BLOCK_CHARS:
            continue
        blocks.append({"type": "compaction", "content": content})
    return blocks


def assistant_provider_fields(blocks: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Return the assistant-message field LiteLLM replays compaction blocks from.

    LiteLLM re-injects these blocks at the head of the assistant content only
    when the stored message carries them under this exact key, so a rebuilt
    assistant turn has to reproduce it verbatim.
    """
    if not blocks:
        return None
    return {"compaction_blocks": [dict(block) for block in blocks]}


def usage_iteration_totals(usage: object) -> tuple[int, int] | None:
    """Return ``(input, output)`` summed over ``usage.iterations``.

    Anthropic's top-level ``input_tokens``/``output_tokens`` cover non-compaction
    iterations only, so a caller that reads them alone bills the compaction pass
    at zero.  ``None`` means the payload carries no usable iteration breakdown
    and the caller's existing accounting is already correct.
    """
    totals = usage_iteration_breakdown(usage)
    return None if totals is None else (totals[0], totals[1])


def usage_iteration_breakdown(usage: object) -> tuple[int, int, int, int] | None:
    """Return ``(input, output, cache_read, cache_creation)`` summed over ``usage.iterations``.

    The cache counters are summed per iteration the same way LiteLLM's
    ``calculate_usage`` does, because the top-level cache counters exclude the
    compaction iteration exactly like the top-level input counter does.
    ``input`` stays Anthropic's uncached quantity; callers add the cache
    counters themselves when they need total input.
    """
    if not isinstance(usage, dict):
        return None
    iterations = usage.get("iterations")
    if not isinstance(iterations, list) or not iterations:
        return None
    totals = [0, 0, 0, 0]
    keys = ("input_tokens", "output_tokens", "cache_read_input_tokens", "cache_creation_input_tokens")
    for iteration in iterations:
        if not isinstance(iteration, dict):
            return None
        for index, key in enumerate(keys):
            value = iteration.get(key)
            if value is None:
                continue
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                # A malformed cache counter is ignored rather than voiding the
                # whole breakdown, so input/output accounting stays unchanged.
                if index >= 2:
                    continue
                return None
            totals[index] += value
    return totals[0], totals[1], totals[2], totals[3]
