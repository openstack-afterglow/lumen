"""Version-2 policy-aware tool dispatch.

``graph.py`` imports this module but calls it only for
``execution_protocol_version=2`` bindings. Those runs require the encrypted
PostgreSQL checkpointer that provides v2 pause/resume semantics.
"""

from __future__ import annotations

import logging

from lumen.services import assets
from lumen.services.agent_protocol import ToolArtifactRef, ToolBinding, ToolExecutionResult, validate_tool_arguments

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
    if result.generated_files:
        user_id = getattr(context, "user_id", None)
        project_id = getattr(context, "project_id", None)
        run_id = getattr(context, "run_id", None)
        if (
            result.status != "completed"
            or not isinstance(user_id, str)
            or not isinstance(project_id, str)
            or not isinstance(run_id, str)
            or len(result.artifacts) + len(result.generated_files) > 20
        ):
            return ToolExecutionResult(
                status="failed",
                model_content="Tool-generated files could not be persisted.",
                error_code="tool_artifact_persistence_failed",
                retryable=False,
            )
        artifact_refs = list(result.artifacts)
        try:
            for generated in result.generated_files:
                common = {
                    "original_name": generated.name,
                    "media_type": generated.media_type,
                    "user_id": user_id,
                    "project_id": project_id,
                    "run_id": run_id,
                }
                if generated.path is not None:
                    stored = await assets.create_generated_asset(path=generated.path, **common)
                else:
                    stored = await assets.create_generated_asset_bytes(data=generated.data or b"", **common)
                artifact_refs.append(
                    ToolArtifactRef(
                        asset_id=stored["id"],
                        kind="generated_file",
                        name=stored["name"],
                        media_type=stored["mime_type"],
                        size_bytes=stored["size_bytes"],
                        sha256=stored["sha256"],
                    )
                )
        except Exception:
            logger.warning(
                "v2 tool artifact persistence failed name=%s",
                binding.definition.name,
                exc_info=True,
            )
            return ToolExecutionResult(
                status="failed",
                model_content="Tool-generated files could not be persisted.",
                error_code="tool_artifact_persistence_failed",
                retryable=False,
            )
        return ToolExecutionResult(
            **result.model_dump(exclude={"artifacts", "generated_files"}),
            artifacts=artifact_refs,
        )
    return result
