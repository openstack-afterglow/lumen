"""Transport-neutral OpenAI chat response projection."""

from __future__ import annotations


def nonstream_response(result: dict, *, cmpl_id: str, created: int) -> dict:
    msg: dict = {"role": "assistant", "content": result["content"] or None}
    if result.get("tool_calls"):
        msg["tool_calls"] = [
            {
                "id": tc.get("id") or f"call_{i}",
                "type": "function",
                "function": {"name": tc["function"].get("name"), "arguments": tc["function"].get("arguments") or "{}"},
            }
            for i, tc in enumerate(result["tool_calls"])
        ]
    return {
        "id": cmpl_id,
        "object": "chat.completion",
        "created": created,
        "model": result["model"],
        "choices": [{"index": 0, "message": msg, "finish_reason": result["finish_reason"]}],
        "usage": {
            "prompt_tokens": result["prompt_tokens"],
            "completion_tokens": result["completion_tokens"],
            "total_tokens": result["prompt_tokens"] + result["completion_tokens"],
            "prompt_tokens_details": {"cached_tokens": result.get("cache_read_input_tokens", 0)},
        },
    }
