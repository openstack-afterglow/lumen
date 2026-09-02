"""Version-2 policy-aware tool dispatch.

``graph.py`` imports this module but calls it only for
``execution_protocol_version=2`` bindings. Those runs require the encrypted
PostgreSQL checkpointer that provides v2 pause/resume semantics.
"""

from __future__ import annotations

import logging

from lumen.services.agent_protocol import ToolBinding, ToolExecutionResult, validate_tool_arguments

logger = logging.getLogger(__name__)


async def dispatch_tool_call(binding: ToolBinding, arguments: object, context: object) -> ToolExecutionResult:
    """Validate frozen arguments before calling a server-only binding.

    Invalid model input and handler failures become typed, non-retryable results.
    Neither path invokes a fallback tool or widens authority.
    """
    try:
        validated_arguments = validate_tool_arguments(binding.definition.input_schema, arguments)
    except ValueError:
        return ToolExecutionResult(
            status="failed",
            model_content="Tool arguments do not match the required schema.",
            error_code="invalid_tool_arguments",
            retryable=False,
        )
    try:
        result = await binding.execute(validated_arguments, context)
    except Exception:
        logger.warning("v2 tool handler failed name=%s", binding.definition.name, exc_info=True)
        return ToolExecutionResult(
            status="failed",
            model_content="Tool execution failed.",
            error_code="tool_execution_failed",
            retryable=False,
        )
    if not isinstance(result, ToolExecutionResult):
        logger.error("v2 tool handler returned an invalid result name=%s", binding.definition.name)
        return ToolExecutionResult(
            status="failed",
            model_content="Tool execution returned an invalid result.",
            error_code="invalid_tool_result",
            retryable=False,
        )
    return result
