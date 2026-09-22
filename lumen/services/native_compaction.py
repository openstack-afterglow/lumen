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

# Only providers whose pinned LiteLLM chat transport declares
# ``context_management`` as a supported parameter.  Anything else is removed by
# ``litellm.drop_params`` before the request is sent, which would leave the
# caller believing compaction was armed when it was silently discarded.
SUPPORTED_PROVIDERS = frozenset({"anthropic"})

# A compaction summary is provider-authored text that is sent straight back on
# the next request.  Bound it so a malformed or hostile replay payload cannot
# grow the prompt without limit.
_MAX_BLOCK_CHARS = 200_000
_MAX_BLOCKS = 8


def supported(provider_type: str | None) -> bool:
    """Return whether this transport can actually activate native compaction."""
    return provider_type in SUPPORTED_PROVIDERS


def trigger_tokens(context_limit: object, ratio: float) -> int | None:
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
    value = max(MIN_TRIGGER_TOKENS, int(context_limit * ratio))
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
    if not isinstance(usage, dict):
        return None
    iterations = usage.get("iterations")
    if not isinstance(iterations, list) or not iterations:
        return None
    input_tokens = 0
    output_tokens = 0
    for iteration in iterations:
        if not isinstance(iteration, dict):
            return None
        value_in = iteration.get("input_tokens")
        value_out = iteration.get("output_tokens")
        if value_in is not None:
            if not isinstance(value_in, int) or isinstance(value_in, bool) or value_in < 0:
                return None
            input_tokens += value_in
        if value_out is not None:
            if not isinstance(value_out, int) or isinstance(value_out, bool) or value_out < 0:
                return None
            output_tokens += value_out
    return input_tokens, output_tokens
