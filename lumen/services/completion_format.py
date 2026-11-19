"""Transport-neutral OpenAI chat response projection."""

from __future__ import annotations


def prompt_token_details(result: dict) -> dict:
    details = {"cached_tokens": result.get("cache_read_input_tokens", 0)}
    short = result.get("cache_creation_5m_input_tokens", 0)
    long = result.get("cache_creation_1h_input_tokens", 0)
    if result.get("openai_cache_ttl") and (short or long):
        details["cache_write_tokens"] = short + long
        details["cache_write_tokens_details"] = {"ttl_30m_tokens": short, "ttl_12h_tokens": long}
    return details


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
            "prompt_tokens_details": prompt_token_details(result),
        },
    }
