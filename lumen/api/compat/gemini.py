"""Google Gemini API ``v1beta`` model catalog and text generation.

Gemini routes keep the native request, parts and usage metadata. OpenAI,
Anthropic and Perplexity text routes are translated through the stateless chat
core. Both use the same reservation, settlement and payer rules as the other
compatibility APIs. Only caller-declared functions are relayed; no server tool
is ever executed.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Any

from fastapi import APIRouter, Depends, Header, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from lumen.auth import require_api_key_scopes
from lumen.services import completion_api as core
from lumen.services import openai_compat
from lumen.services.inference_authority import HOSTED_TOOL_CODE, hosted_tool_message, hosted_tools, principal_may_use
from lumen.services.infrastructure.api_load import admit_sse, register_sse_resource
from lumen.services.providers import errors, routing

from .google import GoogleModelRoute, google_error, google_error_response

router = APIRouter(route_class=GoogleModelRoute)

_PROVIDER_HEADER = Header(default=None, alias="X-Lumen-Provider", min_length=1, max_length=40,
                          pattern=r"^[a-z0-9][a-z0-9_-]*$")
_TEXT_METHODS = ["generateContent", "streamGenerateContent"]
_EMBEDDING_METHODS = ["embedContent", "batchEmbedContents"]
_TRANSLATED_PROVIDERS = frozenset({"openai", "anthropic", "perplexity"})
_NATIVE_IMAGES = frozenset({"image/png", "image/jpeg", "image/webp", "image/heic", "image/heif"})
_TRANSLATED_IMAGES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})
_COMMON_CONFIG = frozenset({"temperature", "topP", "maxOutputTokens", "stopSequences", "candidateCount",
                            "responseMimeType", "responseSchema", "responseJsonSchema", "responseModalities"})
_NATIVE_CONFIG = _COMMON_CONFIG | {"topK", "presencePenalty", "frequencyPenalty", "seed", "thinkingConfig"}
_FINISH = {"stop": "STOP", "tool_calls": "STOP", "function_call": "STOP", "length": "MAX_TOKENS",
           "content_filter": "SAFETY"}


class GeminiGenerateRequest(BaseModel):
    """Closed request: ``cachedContent`` and other unsupported fields are named in a 400."""

    contents: list[dict[str, Any]] = Field(min_length=1)
    systemInstruction: dict[str, Any] | None = None
    generationConfig: dict[str, Any] | None = None
    safetySettings: list[dict[str, Any]] | None = None
    tools: list[dict[str, Any]] | None = None
    toolConfig: dict[str, Any] | None = None

    model_config = {"extra": "forbid"}


def _reject(message: str) -> core.CompletionError:
    return core.CompletionError(400, message)


# ── Catalog ───────────────────────────────────────────────────────────────────
async def _catalog(provider: str | None, principal: dict) -> list[dict]:
    try:
        text = await routing.list_gemini_api_models()
        embeddings = [row for row in await routing.list_api_models(model_kind="embedding")
                      if row["provider_type"] == "gemini"]
    except errors.ChatStorageUnavailable as exc:
        raise core.CompletionError(503, "model catalog unavailable") from exc
    # Hide routes whose intrinsic scopes (Sonar built-in search) this caller cannot hold.
    text = [row for row in text if principal_may_use(principal, row["required_scopes"])]
    embeddings = [row for row in embeddings if principal_may_use(principal, row["required_scopes"])]
    if provider is not None:
        text = [row for row in text if row["provider"] == provider]
        embeddings = [row for row in embeddings if row["api_provider"] == provider]
    # ``/v1beta/models`` is one namespace: a public ID shared by any two listed rows,
    # of any kind, is issued only as each row's exact route instead of guessing.
    counts: dict[str, int] = {}
    for row in (*text, *embeddings):
        counts[row["api_model_name"]] = counts.get(row["api_model_name"], 0) + 1
    models = []
    for row in text:
        public = row["api_model_name"]
        caps = row["capabilities"]
        model = {
            "name": f"models/{public if counts[public] == 1 else row['id']}",
            "baseModelId": public,
            "version": "lumen",
            "displayName": row["display_name"],
            "description": f"{row['provider_name']} ({row['provider']})",
            "supportedGenerationMethods": _TEXT_METHODS if row["streaming"] else _TEXT_METHODS[:1],
        }
        input_limit = caps.get("max_input_tokens") or caps.get("context_limit")
        if isinstance(input_limit, int):
            model["inputTokenLimit"] = input_limit
        if isinstance(caps.get("max_output_tokens"), int):
            model["outputTokenLimit"] = caps["max_output_tokens"]
        models.append(model)
    models.extend({
        "name": "models/" + (row["api_model_name"] if counts[row["api_model_name"]] == 1
                             else routing.cli_route_id(row["provider_id"], row["model_id"])),
        "baseModelId": row["api_model_name"], "version": "lumen",
        "displayName": row["api_model_name"], "description": row["api_provider"],
        "supportedGenerationMethods": _EMBEDDING_METHODS,
    } for row in embeddings)
    return models


@router.get("/v1beta/models")
async def list_models(
    pageSize: int = Query(default=50, ge=1, le=1000),
    pageToken: str | None = Query(default=None, max_length=12),
    x_lumen_provider: str | None = _PROVIDER_HEADER,
    token_info: dict = Depends(require_api_key_scopes("models:read")),
):
    if pageToken is not None and not pageToken.isdecimal():
        raise _reject("pageToken is invalid")
    offset = int(pageToken or 0)
    models = await _catalog(x_lumen_provider, token_info)
    page = {"models": models[offset:offset + pageSize]}
    if offset + pageSize < len(models):
        page["nextPageToken"] = str(offset + pageSize)
    return JSONResponse(page, headers={"Cache-Control": "no-store", "Vary": "X-Lumen-Provider"})


@router.get("/v1beta/models/{model:path}")
async def get_model(
    model: str,
    x_lumen_provider: str | None = _PROVIDER_HEADER,
    token_info: dict = Depends(require_api_key_scopes("models:read")),
):
    selector = model.removeprefix("models/")
    matches = [row for row in await _catalog(x_lumen_provider, token_info)
               if row["name"] == f"models/{selector}" or row["baseModelId"] == selector]
    exact = [row for row in matches if row["name"] == f"models/{selector}"]
    if len(exact or matches) > 1:
        raise core.CompletionError(409, "model_route_ambiguous: use the listed exact name or X-Lumen-Provider")
    if not matches:
        raise core.CompletionError(404, f"model not found: {selector}")
    return JSONResponse((exact or matches)[0], headers={"Cache-Control": "no-store", "Vary": "X-Lumen-Provider"})


# ── Request validation and translation ───────────────────────────────────────
def _inline_image(data: Any, allowed: frozenset[str]) -> tuple[str, str]:
    # Proto JSON accepts both spellings; the Python SDK sends ``mime_type``.
    if not isinstance(data, dict) or set(data) - {"mimeType", "mime_type", "data"}:
        raise _reject("inlineData must contain mimeType and data")
    mime, payload = data.get("mimeType", data.get("mime_type")), data.get("data")
    if mime not in allowed:
        raise _reject(f"inlineData mimeType {mime!r} is unsupported; supported: {', '.join(sorted(allowed))}")
    if not isinstance(payload, str) or not payload:
        raise _reject("inlineData.data must be base64 text")
    return mime, payload


def _function_part(part: dict, kind: str) -> dict:
    """Closed functionCall/functionResponse: nested ``parts`` (file references,
    non-image bytes) would reach the provider with its credential, unmetered."""
    value = part[kind]
    allowed, payload = ({"id", "name", "args"}, "args") if kind == "functionCall" else ({"id", "name", "response"}, "response")
    if not isinstance(value, dict) or set(value) - allowed or not isinstance(value.get("name"), str):
        raise _reject(f"{kind} supports only {', '.join(sorted(allowed))}")
    if not isinstance(value.get(payload, {}), dict) or not isinstance(value.get("id", ""), str):
        raise _reject(f"{kind}.{payload} must be an object")
    return value


def _validate_native_parts(parts: Any, role: str) -> None:
    if not isinstance(parts, list) or not parts:
        raise _reject(f"{role} content needs at least one part")
    for part in parts:
        if not isinstance(part, dict):
            raise _reject("each part must be an object")
        kinds = set(part) & {"text", "inlineData", "functionCall", "functionResponse"}
        extra = set(part) - {"text", "inlineData", "functionCall", "functionResponse", "thought", "thoughtSignature"}
        if extra or len(kinds) != 1:
            raise _reject(f"unsupported part fields: {', '.join(sorted(extra)) or 'empty part'}")
        if "inlineData" in part:
            _inline_image(part["inlineData"], _NATIVE_IMAGES)
        if "text" in part and not isinstance(part["text"], str):
            raise _reject("part text must be a string")
        for kind in kinds & {"functionCall", "functionResponse"}:
            _function_part(part, kind)


def _openai_schema(schema: Any) -> Any:
    """Gemini OpenAPI schema subset → JSON Schema for translated function/output schemas."""
    if isinstance(schema, list):
        return [_openai_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result: dict[str, Any] = {}
    for key, value in schema.items():
        if key in {"propertyOrdering", "example"}:
            continue
        if key == "type" and isinstance(value, str):
            result[key] = value.lower()
        elif key in {"properties", "$defs", "definitions"} and isinstance(value, dict):
            result[key] = {name: _openai_schema(item) for name, item in value.items()}
        elif key in {"items", "anyOf", "oneOf", "allOf", "not", "additionalProperties"}:
            result[key] = _openai_schema(value)
        elif key != "nullable":
            result[key] = value
    if schema.get("nullable") is True and isinstance(result.get("type"), str):
        result["type"] = [result["type"], "null"]
    return result


def _function_tools(tools: list[dict] | None) -> list[dict]:
    """Client function declarations; hosted tools were already refused by ``hosted_tools``."""
    declarations: list[dict] = []
    for tool in tools or ():
        key = "functionDeclarations" if "functionDeclarations" in tool else "function_declarations"
        if set(tool) != {key}:
            raise _reject("each tool must declare only functionDeclarations")
        if not isinstance(tool[key], list):
            raise _reject("functionDeclarations must be a list")
        for declaration in tool[key]:
            if not isinstance(declaration, dict) or not isinstance(declaration.get("name"), str):
                raise _reject("each function declaration needs a name")
            if set(declaration) - {"name", "description", "parameters", "parametersJsonSchema"}:
                raise _reject("function declarations support name, description, parameters and parametersJsonSchema")
            declarations.append(declaration)
    return declarations


def _tool_choice(config: dict | None) -> Any:
    if config is None:
        return None
    calling = config.get("functionCallingConfig")
    if set(config) != {"functionCallingConfig"} or not isinstance(calling, dict):
        raise _reject("only toolConfig.functionCallingConfig is supported")
    if set(calling) - {"mode", "allowedFunctionNames"}:
        raise _reject("functionCallingConfig supports mode and allowedFunctionNames")
    mode = str(calling.get("mode", "AUTO")).upper()
    names = calling.get("allowedFunctionNames") or []
    if mode == "ANY" and len(names) == 1:
        return {"type": "function", "function": {"name": names[0]}}
    if names:
        raise _reject("allowedFunctionNames supports exactly one name on this model")
    choice = {"AUTO": "auto", "ANY": "required", "NONE": "none", "VALIDATED": "auto"}.get(mode)
    if choice is None:
        raise _reject(f"functionCallingConfig mode {mode} is unsupported")
    return choice


def _config(body: GeminiGenerateRequest, *, native: bool) -> dict:
    config = dict(body.generationConfig or {})
    allowed = _NATIVE_CONFIG if native else _COMMON_CONFIG
    unsupported = sorted(set(config) - allowed)
    if unsupported:
        raise _reject(f"generationConfig fields unsupported on this model: {', '.join(unsupported)}")
    if config.get("candidateCount", 1) != 1:
        raise _reject("candidateCount must be 1")
    modalities = config.get("responseModalities")
    if modalities is not None and [str(item).upper() for item in modalities] != ["TEXT"]:
        raise _reject("only TEXT responseModalities are supported")
    mime = config.get("responseMimeType")
    if mime not in (None, "text/plain", "application/json") and not (native and mime == "text/x.enum"):
        raise _reject(f"responseMimeType {mime!r} is unsupported")
    max_tokens = config.get("maxOutputTokens")
    if max_tokens is not None and (type(max_tokens) is not int or max_tokens <= 0):
        raise _reject("maxOutputTokens must be a positive integer")
    return config


def _chat_messages(body: GeminiGenerateRequest, *, images: frozenset[str], drop_thoughts: bool) -> list[dict]:
    """Contents → OpenAI Chat messages for translated execution and billing evidence."""
    messages: list[dict] = []
    if body.systemInstruction is not None:
        parts = body.systemInstruction.get("parts")
        if not isinstance(parts, list) or any(not isinstance(part, dict) or set(part) != {"text"} for part in parts):
            raise _reject("systemInstruction supports text parts only")
        messages.append({"role": "system", "content": "\n\n".join(str(part["text"]) for part in parts)})
    pending: list[tuple[str, str]] = []
    for turn, content in enumerate(body.contents):
        role = content.get("role", "user")
        if set(content) - {"role", "parts"} or role not in {"user", "model", "function"}:
            raise _reject("contents support role (user, model) and parts")
        parts = content.get("parts")
        if not isinstance(parts, list) or not parts or not all(isinstance(part, dict) for part in parts):
            raise _reject("each content needs at least one object part")
        if role == "model":
            text, calls = [], []
            for index, part in enumerate(parts):
                if part.get("thought") and drop_thoughts:
                    continue
                if "text" in part:
                    text.append(str(part["text"]))
                elif "functionCall" in part:
                    call = _function_part(part, "functionCall")
                    call_id = str(call.get("id") or f"call_{turn}_{index}")
                    pending.append((str(call.get("name")), call_id))
                    calls.append({"id": call_id, "type": "function", "function": {
                        "name": call.get("name"), "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False)}})
                else:
                    raise _reject("model turns support text and functionCall parts")
            message: dict[str, Any] = {"role": "assistant", "content": "".join(text) or None}
            if calls:
                message["tool_calls"] = calls
            messages.append(message)
            continue
        user_parts: list[dict] = []
        for part in parts:
            if "functionResponse" in part:
                response = _function_part(part, "functionResponse")
                name = str(response.get("name"))
                call_id = response.get("id")
                if call_id is None:
                    match = next((item for item in pending if item[0] == name), None)
                    call_id = match[1] if match else f"call_{name}"
                pending = [item for item in pending if item[1] != call_id]
                messages.append({"role": "tool", "tool_call_id": str(call_id),
                                 "content": json.dumps(response.get("response") or {}, ensure_ascii=False)})
            elif "text" in part:
                user_parts.append({"type": "text", "text": str(part["text"])})
            elif "inlineData" in part:
                mime, data = _inline_image(part["inlineData"], images)
                user_parts.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{data}"}})
            else:
                raise _reject("user turns support text, inlineData images and functionResponse parts")
        if user_parts:
            only_text = all(item["type"] == "text" for item in user_parts)
            messages.append({"role": "user", "content": "".join(item["text"] for item in user_parts)
                             if only_text else user_parts})
    return messages


def _translated_options(config: dict) -> dict:
    options: dict[str, Any] = {}
    if "topP" in config:
        options["top_p"] = config["topP"]
    if config.get("stopSequences"):
        options["stop"] = list(config["stopSequences"])
    if config.get("responseMimeType") == "application/json":
        schema = config.get("responseJsonSchema") or _openai_schema(config.get("responseSchema"))
        options["response_format"] = (
            {"type": "json_schema", "json_schema": {"name": "response", "schema": schema}}
            if schema else {"type": "json_object"}
        )
    elif config.get("responseSchema") or config.get("responseJsonSchema"):
        raise _reject("responseSchema requires responseMimeType application/json")
    return options


# ── Responses ─────────────────────────────────────────────────────────────────
def _usage(prompt: int, completion: int, cached: int = 0) -> dict:
    usage = {"promptTokenCount": prompt, "candidatesTokenCount": completion, "totalTokenCount": prompt + completion}
    if cached:
        usage["cachedContentTokenCount"] = cached
    return usage


def _call_parts(calls: list[dict] | None) -> list[dict]:
    parts = []
    for call in calls or ():
        function = call.get("function") or {}
        try:
            args = json.loads(function.get("arguments") or "{}")
        except ValueError:
            args = {}
        parts.append({"functionCall": {"id": call.get("id"), "name": function.get("name"), "args": args}})
    return parts


def _candidate(parts: list[dict], finish: str | None = None) -> dict:
    candidate: dict[str, Any] = {"content": {"role": "model", "parts": parts}, "index": 0}
    if finish is not None:
        candidate["finishReason"] = _FINISH.get(finish, "OTHER")
    return candidate


def _frame(payload: dict) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\r\n\r\n"


def _error_frame(event: dict) -> str:
    code = 502 if event.get("code") in {None, "provider_result_unknown"} else 400
    return _frame(google_error(code, str(event.get("code") or event.get("message") or "upstream model error")))


# ── Generation ────────────────────────────────────────────────────────────────
async def _prepare(model: str, body: GeminiGenerateRequest, provider: str | None, token_info: dict) -> dict:
    selector = model.removeprefix("models/")
    # Same key tool policy as every compatibility route, before any provider resolution.
    if hosted := hosted_tools(body.model_dump(exclude_none=True)):
        raise core.CompletionError(403, f"{HOSTED_TOOL_CODE}: {hosted_tool_message(hosted)}", code=HOSTED_TOOL_CODE)
    if selector == openai_compat.VIRTUAL_MODEL_ID:
        raise core.CompletionError(404, f"model not found: {selector}")
    resolved = await core.resolve_api(selector, provider=provider, operation="chat")
    if resolved["provider_type"] not in _TRANSLATED_PROVIDERS | {"gemini"}:
        raise core.CompletionError(400, "this model is not available through the Gemini API")
    native = resolved["provider_type"] == "gemini"
    config = _config(body, native=native)
    tools = _function_tools(body.tools)
    if body.safetySettings is not None and not native:
        raise _reject("safetySettings are supported only on Gemini models")
    if native:
        for content in body.contents:
            if set(content) - {"role", "parts"}:
                raise _reject("contents support role and parts")
            _validate_native_parts(content.get("parts"), str(content.get("role", "user")))
        _tool_choice(body.toolConfig)
    await core.precheck(token_info["user_id"], token_info["project_id"], api_key_id=token_info.get("api_key_id"),
                        payer=token_info.get("payer"))
    messages = _chat_messages(body, images=_NATIVE_IMAGES if native else _TRANSLATED_IMAGES, drop_thoughts=True)
    return {"resolved": resolved, "native": native, "config": config, "tools": tools, "messages": messages}


def _native_payload(body: GeminiGenerateRequest, config: dict, resolved: dict) -> tuple[dict, int]:
    payload = body.model_dump(exclude_none=True)
    max_tokens = config.get("maxOutputTokens")
    if max_tokens is None:
        # Bound the provider to the same output budget the credit hold reserves.
        limit = (resolved.get("capabilities") or {}).get("max_output_tokens")
        max_tokens = limit if type(limit) is int and limit > 0 else core.clamp_max_tokens(None)
        payload["generationConfig"] = {**config, "maxOutputTokens": max_tokens}
    return payload, max_tokens


def _translated_call(prepared: dict, token_info: dict) -> dict:
    config = prepared["config"]
    tools = [{"type": "function", "function": {
        "name": item["name"], "description": item.get("description") or "",
        "parameters": item.get("parametersJsonSchema") or _openai_schema(item.get("parameters"))
        or {"type": "object", "properties": {}},
    }} for item in prepared["tools"]]
    return {
        "resolved": prepared["resolved"], "messages": prepared["messages"],
        "user_id": token_info["user_id"], "project_id": token_info["project_id"],
        "api_key_id": token_info.get("api_key_id"), "payer": token_info.get("payer"),
        "max_tokens": config.get("maxOutputTokens"), "temperature": config.get("temperature"),
        "tools": tools or None, "tool_choice": _tool_choice(None if not tools else prepared["toolConfig"]),
        "options": _translated_options(config), "translated_options": True,
    }


@router.post("/v1beta/models/{model:path}:generateContent")
async def generate_content(
    model: str,
    body: GeminiGenerateRequest,
    x_lumen_provider: str | None = _PROVIDER_HEADER,
    token_info: dict = Depends(require_api_key_scopes("compat:completions:write")),
):
    prepared = await _prepare(model, body, x_lumen_provider, token_info)
    if prepared["native"]:
        payload, max_tokens = _native_payload(body, prepared["config"], prepared["resolved"])
        result = await core.complete_gemini(
            resolved=prepared["resolved"], payload=payload, messages=prepared["messages"], max_tokens=max_tokens,
            stream=False, user_id=token_info["user_id"], project_id=token_info["project_id"],
            api_key_id=token_info.get("api_key_id"), payer=token_info.get("payer"),
        )
        return JSONResponse(dict(result))
    result = await core.complete_once(**_translated_call({**prepared, "toolConfig": body.toolConfig}, token_info))
    parts = ([{"text": result["content"]}] if result["content"] else []) + _call_parts(result["tool_calls"])
    return JSONResponse({
        "candidates": [_candidate(parts or [{"text": ""}], result["finish_reason"])],
        "usageMetadata": _usage(result["prompt_tokens"], result["completion_tokens"],
                                result.get("cache_read_input_tokens") or 0),
        "modelVersion": result["model"],
        "responseId": uuid.uuid4().hex,
    })


@router.post("/v1beta/models/{model:path}:streamGenerateContent")
@admit_sse
async def stream_generate_content(
    model: str,
    payload: GeminiGenerateRequest,
    request: Request,
    alt: str | None = Query(default=None),
    x_lumen_provider: str | None = _PROVIDER_HEADER,
    token_info: dict = Depends(require_api_key_scopes("compat:completions:write")),
):
    if alt != "sse":
        raise _reject("streamGenerateContent requires alt=sse")
    prepared = await _prepare(model, payload, x_lumen_provider, token_info)
    if (prepared["resolved"].get("capabilities") or {}).get("streaming") is False:
        raise _reject("this model does not support streamGenerateContent; use generateContent")
    if prepared["native"]:
        wire, max_tokens = _native_payload(payload, prepared["config"], prepared["resolved"])
        events = await core.complete_gemini(
            resolved=prepared["resolved"], payload=wire, messages=prepared["messages"], max_tokens=max_tokens,
            stream=True, user_id=token_info["user_id"], project_id=token_info["project_id"],
            api_key_id=token_info.get("api_key_id"), payer=token_info.get("payer"),
        )
    else:
        events = await core.complete_stream(
            **_translated_call({**prepared, "toolConfig": payload.toolConfig}, token_info))
    register_sse_resource(request, events)
    model_version = prepared["resolved"]["api_model_name"]
    response_id = uuid.uuid4().hex

    async def native_body() -> AsyncIterator[str]:
        async with aclosing(events) as stream:
            try:
                async for frame in stream:
                    yield _frame(dict(frame))
            except core.CompletionError as exc:
                yield _frame(google_error(exc.status_code, exc.message))
            except Exception:
                yield _frame(google_error(502, "upstream model error"))

    async def translated_body() -> AsyncIterator[str]:
        calls: dict[int, dict] = {}
        finish = None
        async with aclosing(events) as stream:
            try:
                async for event in stream:
                    if event.get("type") == "error":
                        yield _error_frame(event)
                        return
                    if event.get("type") == "done":
                        frame = {"candidates": [_candidate(
                            _call_parts([calls[index] for index in sorted(calls)]),
                            finish or event.get("finish_reason"))],
                            "usageMetadata": _usage(event["prompt_tokens"], event["completion_tokens"],
                                                    event.get("cache_read_input_tokens") or 0),
                            "modelVersion": model_version, "responseId": response_id}
                        yield _frame(frame)
                        return
                    for delta in event.get("tool_calls") or ():
                        call = calls.setdefault(int(delta.get("index") or 0), {
                            "id": None, "function": {"name": "", "arguments": ""}})
                        call["id"] = delta.get("id") or call["id"]
                        function = delta.get("function") or {}
                        call["function"]["name"] += function.get("name") or ""
                        call["function"]["arguments"] += function.get("arguments") or ""
                    finish = event.get("finish_reason") or finish
                    if event.get("content"):
                        yield _frame({"candidates": [_candidate([{"text": event["content"]}])],
                                      "modelVersion": model_version, "responseId": response_id})
            except core.CompletionError as exc:
                yield _frame(google_error(exc.status_code, exc.message))
            except Exception:
                yield _frame(google_error(502, "upstream model error"))

    return StreamingResponse(native_body() if prepared["native"] else translated_body(),
                             media_type="text/event-stream", headers={"Cache-Control": "no-store"})


@router.post("/v1beta/models/{model:path}:countTokens")
async def count_tokens(model: str, token_info: dict = Depends(require_api_key_scopes("compat:completions:write"))):
    del model, token_info
    return google_error_response(501, "countTokens is not supported by Lumen")
