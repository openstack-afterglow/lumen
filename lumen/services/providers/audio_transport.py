"""Bounded direct OpenAI/Gemini speech with exact Lumen product-unit pricing.

Configured USD/second is the customer's frozen media-duration rate. It is not
a claim that the provider invoices in the same unit (some invoice tokens or
characters). Unsupported models, formats and missing prices fail before I/O.
"""

from __future__ import annotations

import base64
import binascii
import json
from decimal import Decimal

import httpx

from .errors import ProviderValidationError
from .pricing import exact_media_price

_MAX_INPUT_BYTES = 24_000_000  # Multipart overhead stays below OpenAI's 25 MB upload limit.
_MAX_GEMINI_INLINE_BYTES = 14 * 1024 * 1024  # Base64 JSON request stays below 20 MB.
_MAX_OUTPUT_BYTES = 10 * 1024 * 1024
_MAX_TEXT_CHARS = 4096
_MAX_TRANSCRIPT_BYTES = 256 * 1024
_MAX_INLINE_JSON_BYTES = (_MAX_OUTPUT_BYTES * 4 // 3) + 65536
_TIMEOUT = httpx.Timeout(120.0, connect=10.0, read=120.0, write=30.0, pool=10.0)
_OPENAI_STT_MODELS = frozenset({"whisper-1", "gpt-4o-transcribe", "gpt-4o-mini-transcribe"})
_OPENAI_TTS_MODELS = frozenset({"tts-1", "tts-1-hd", "gpt-4o-mini-tts"})
_GEMINI_STT_MODELS = frozenset({"gemini-2.5-flash", "gemini-2.5-pro", "gemini-3-flash-preview"})
_GEMINI_TTS_MODELS = frozenset({"gemini-2.5-flash-preview-tts", "gemini-2.5-pro-preview-tts", "gemini-3.8-flash-tts"})
_OPENAI_VOICES = frozenset({
    "alloy", "ash", "ballad", "coral", "echo", "fable", "onyx", "nova",
    "sage", "shimmer", "verse", "marin", "cedar",
})
_GEMINI_VOICES = frozenset({
    "Zephyr", "Puck", "Charon", "Kore", "Fenrir", "Leda", "Orus",
    "Aoede", "Callirrhoe", "Autonoe", "Enceladus", "Iapetus", "Umbriel",
    "Algieba", "Despina", "Erinome", "Algenib", "Rasalgethi", "Laomedeia",
    "Achernar", "Alnilam", "Schedar", "Gacrux", "Pulcherrima", "Achird",
    "Zubenelgenubi", "Vindemiatrix", "Sadachbia", "Sadaltager", "Sulafat",
})
# No path, URI, or external URL is accepted as media input or provider output.
_INPUT_FORMATS = {
    "audio/wav": "wav", "audio/x-wav": "wav", "audio/mpeg": "mp3", "audio/mp4": "m4a",
    "audio/flac": "flac", "audio/ogg": "ogg", "audio/webm": "webm",
}
_OUTPUT_FORMATS = {"mp3": "audio/mpeg", "wav": "audio/wav"}


class AudioTransportError(RuntimeError):
    """The direct provider failed or produced an invalid bounded result."""


def _model(route: dict, kind: str) -> tuple[str, str]:
    if not isinstance(route, dict) or route.get("model_kind") != kind or route.get("provider_auth") is not None:
        raise ProviderValidationError("audio route is not directly executable")
    provider, model = route.get("provider_type"), route.get("api_model_name")
    if not isinstance(model, str):
        raise ProviderValidationError("unsupported audio provider or model")
    if provider == "openai" and model in (_OPENAI_STT_MODELS if kind == "stt" else _OPENAI_TTS_MODELS):
        bases = {"https://api.openai.com", "https://api.openai.com/v1"}
    elif provider == "gemini" and model in (_GEMINI_STT_MODELS if kind == "stt" else _GEMINI_TTS_MODELS):
        bases = {"https://generativelanguage.googleapis.com", "https://generativelanguage.googleapis.com/v1beta"}
    else:
        raise ProviderValidationError("unsupported audio provider or model")
    base = route.get("api_base")
    if base is not None and (not isinstance(base, str) or base.rstrip("/") not in bases):
        raise ProviderValidationError("unsupported audio provider endpoint")
    if not isinstance(route.get("api_key"), str) or not route["api_key"].strip():
        raise ProviderValidationError("audio provider credential is unavailable")
    return provider, model


def validate_audio_request(route: dict, *, kind: str, format: str | None = None) -> Decimal:
    """Resolve the route's exact configured Lumen USD/second rate before I/O."""
    if not isinstance(kind, str) or kind not in ("stt", "tts"):
        raise ProviderValidationError("unsupported audio operation")
    provider, _ = _model(route, kind)
    if kind == "tts":
        if not isinstance(format, str) or format not in _OUTPUT_FORMATS or (provider == "gemini" and format != "wav"):
            raise ProviderValidationError("unsupported speech output format")
        field = "audio_output_per_second"
    else:
        if format is not None and (not isinstance(format, str) or format not in _INPUT_FORMATS):
            raise ProviderValidationError("unsupported audio input format")
        field = "audio_input_per_second"
    pricing = route.get("media_pricing")
    rate = exact_media_price(pricing, field)
    # Legacy registry entries are accepted only where their unit converts
    # exactly to the product second. Never silently choose conflicting prices.
    legacy_field = "audio_per_second" if kind == "tts" else "audio_per_minute"
    legacy = exact_media_price(pricing, legacy_field)
    if legacy is not None:
        legacy = legacy if kind == "tts" else legacy / 60
    if rate is not None and legacy is not None and rate != legacy:
        raise ProviderValidationError("conflicting audio duration prices")
    seconds = rate if rate is not None else legacy
    if (seconds is None or seconds <= 0 or seconds >= Decimal("100000000")
            or seconds != seconds.quantize(Decimal("0.0000000001"))):
        raise ProviderValidationError("exact configured audio price is unavailable")
    return seconds


def audio_route_ready(route: dict) -> bool:
    """Advertise only supported direct routes with a priced product media unit."""
    if not isinstance(route, dict):
        return False
    kind = route.get("model_kind")
    format = ("wav" if route.get("provider_type") == "gemini" else "mp3") if kind == "tts" else None
    try:
        validate_audio_request(route, kind=kind, format=format)
    except ProviderValidationError:
        return False
    return True

def available_speech_options(route: dict) -> tuple[list[str], list[str]]:
    """Expose executable voice and format choices, not cross-provider defaults."""
    provider, _ = _model(route, "tts")
    formats = ["wav"] if provider == "gemini" else ["mp3", "wav"]
    validate_audio_request(route, kind="tts", format=formats[0])
    voices = _GEMINI_VOICES if provider == "gemini" else _OPENAI_VOICES
    return sorted(voices), formats


def _wav_layout(data: bytes) -> tuple[bool, int | None] | None:
    if len(data) < 44 or data[:4] != b"RIFF" or data[8:12] != b"WAVE":
        return None
    riff_size = int.from_bytes(data[4:8], "little")
    streamed = riff_size == 0xffffffff
    if not streamed and riff_size != len(data) - 8:
        return None
    position, block_align, has_data = 12, None, False
    unknown_data_size_offset = None
    while position + 8 <= len(data):
        chunk_type = data[position:position + 4]
        size_offset = position + 4
        size = int.from_bytes(data[size_offset:size_offset + 4], "little")
        position += 8
        if size == 0xffffffff:
            if not streamed or chunk_type != b"data":
                return None
            size = len(data) - position
            if size & 1:
                return None
            unknown_data_size_offset = size_offset
        if position + size > len(data):
            return None
        if chunk_type == b"fmt ":
            if size < 16 or int.from_bytes(data[position:position + 2], "little") != 1:
                return None
            channels = int.from_bytes(data[position + 2:position + 4], "little")
            rate = int.from_bytes(data[position + 4:position + 8], "little")
            block_align = int.from_bytes(data[position + 12:position + 14], "little")
            bits = int.from_bytes(data[position + 14:position + 16], "little")
            if not 1 <= channels <= 2 or not 8000 <= rate <= 96000 or bits not in (8, 16, 24, 32) or block_align != channels * bits // 8:
                return None
        elif chunk_type == b"data":
            has_data = size > 0 and (block_align is not None and size % block_align == 0)
        position += size + (size & 1)
    return (streamed, unknown_data_size_offset) if position == len(data) and block_align is not None and has_data else None


def _valid_wav(data: bytes) -> bool:
    return _wav_layout(data) is not None


def _valid_mp3(data: bytes) -> bool:
    offset = 0
    if data.startswith(b"ID3"):
        if len(data) < 14 or any(byte & 0x80 for byte in data[6:10]):
            return False
        offset = 10 + sum(byte << shift for byte, shift in zip(data[6:10], (21, 14, 7, 0), strict=True))
        if data[5] & 0x10:  # ID3 footer.
            offset += 10
    if len(data) < offset + 4 or data[offset] != 0xff:
        return False
    second, third = data[offset + 1:offset + 3]
    version = (second >> 3) & 3
    if second & 0xe6 != 0xe2 or version == 1 or second & 0x06 != 0x02:
        return False  # MPEG Layer III only, with a valid version.
    bitrate_index, rate_index = third >> 4, (third >> 2) & 3
    if bitrate_index in (0, 15) or rate_index == 3:
        return False
    kbps = ((0, 32, 40, 48, 56, 64, 80, 96, 112, 128, 160, 192, 224, 256, 320, 0)
            if version == 3 else
            (0, 8, 16, 24, 32, 40, 48, 56, 64, 80, 96, 112, 128, 144, 160, 0))[bitrate_index]
    sample_rate = (44100, 48000, 32000)[rate_index] // (1 if version == 3 else 2 if version == 2 else 4)
    frame_length = (144 if version == 3 else 72) * kbps * 1000 // sample_rate + ((third >> 1) & 1)
    return len(data) >= offset + frame_length


def _signature(data: bytes, mime: str) -> bool:
    if mime in {"audio/wav", "audio/x-wav"}:
        return _valid_wav(data)
    if mime == "audio/mpeg":
        return _valid_mp3(data)
    if mime == "audio/mp4":
        return len(data) >= 12 and data[4:8] == b"ftyp" and 8 <= int.from_bytes(data[:4], "big") <= len(data)
    if mime == "audio/flac":
        return data.startswith(b"fLaC")
    if mime == "audio/ogg":
        return data.startswith(b"OggS\x00")
    if mime == "audio/webm":
        return data.startswith(b"\x1a\x45\xdf\xa3")
    return False


def _source_audio(data: bytes, mime_type: str) -> str:
    if not isinstance(mime_type, str) or mime_type not in _INPUT_FORMATS or not isinstance(data, bytes) or not 0 < len(data) <= _MAX_INPUT_BYTES or not _signature(data, mime_type):
        raise ProviderValidationError("unsupported or oversized source audio")
    return _INPUT_FORMATS[mime_type]


def _output_audio(data: bytes, format: str) -> tuple[bytes, str]:
    mime = _OUTPUT_FORMATS[format]
    if not data or len(data) > _MAX_OUTPUT_BYTES:
        raise AudioTransportError("audio provider returned invalid or oversized audio")
    if format == "wav":
        layout = _wav_layout(data)
        if layout is None:
            raise AudioTransportError("audio provider returned invalid or oversized audio")
        streamed, unknown_data_size_offset = layout
        if streamed:
            normalized = bytearray(data)
            normalized[4:8] = (len(data) - 8).to_bytes(4, "little")
            if unknown_data_size_offset is not None:
                normalized[unknown_data_size_offset:unknown_data_size_offset + 4] = (
                    len(data) - unknown_data_size_offset - 4
                ).to_bytes(4, "little")
            data = bytes(normalized)
    elif not _signature(data, mime):
        raise AudioTransportError("audio provider returned invalid or oversized audio")
    return data, mime


def _decode_audio(encoded: object, format: str, mime: object = None) -> tuple[bytes, str]:
    if mime is not None and mime != _OUTPUT_FORMATS[format]:
        raise AudioTransportError("audio provider returned an unexpected audio type")
    if not isinstance(encoded, str) or not encoded or len(encoded) > (_MAX_OUTPUT_BYTES * 4 // 3) + 8:
        raise AudioTransportError("audio provider returned invalid or oversized inline audio")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise AudioTransportError("audio provider returned invalid inline audio") from exc
    return _output_audio(data, format)


async def _post(url: str, *, headers: dict, max_bytes: int, json_body: dict | None = None,
                form: dict | None = None, files: dict | None = None) -> bytes:
    # Never interpolate credentials, URLs from a response, or provider bodies into errors.
    try:
        async with (
            httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False, trust_env=False) as client,
            client.stream("POST", url, headers=headers, json=json_body, data=form, files=files) as response,
        ):
            if response.status_code != 200:
                raise AudioTransportError(f"audio provider returned HTTP {response.status_code}")
            size = response.headers.get("content-length", "")
            if size.isdecimal() and int(size) > max_bytes:
                raise AudioTransportError("audio provider response exceeds size limit")
            chunks = bytearray()
            async for chunk in response.aiter_bytes():
                if len(chunks) + len(chunk) > max_bytes:
                    raise AudioTransportError("audio provider response exceeds size limit")
                chunks.extend(chunk)
            return bytes(chunks)
    except httpx.HTTPError:
        raise AudioTransportError("audio provider request failed") from None


def _json(data: bytes) -> dict:
    try:
        result = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise AudioTransportError("audio provider returned invalid JSON") from exc
    if not isinstance(result, dict):
        raise AudioTransportError("audio provider returned invalid JSON")
    return result


def _gemini_output(result: dict, content_type: str) -> dict:
    if result.get("status") != "completed" or not isinstance(result.get("steps"), list):
        raise AudioTransportError("audio provider did not complete")
    contents = []
    for step in result["steps"]:
        if not isinstance(step, dict):
            raise AudioTransportError("audio provider returned invalid output")
        if step.get("type") != "model_output":
            continue
        if not isinstance(step.get("content"), list):
            raise AudioTransportError("audio provider returned invalid output")
        for item in step["content"]:
            if not isinstance(item, dict):
                raise AudioTransportError("audio provider returned invalid output")
            if item.get("type") == content_type:
                if "url" in item or "uri" in item:
                    raise AudioTransportError("audio provider returned a URL instead of inline data")
                contents.append(item)
    if len(contents) != 1:
        raise AudioTransportError("audio provider returned an unexpected output count")
    return contents[0]


async def generate_speech(route: dict, *, text: str, voice: str, format: str = "mp3") -> tuple[bytes, str]:
    """Generate one bounded, signature-validated speech result on a priced route."""
    validate_audio_request(route, kind="tts", format=format)
    provider, model = _model(route, "tts")
    if not isinstance(text, str) or not text.strip() or len(text) > _MAX_TEXT_CHARS:
        raise ProviderValidationError("speech text is empty or too long")
    if not isinstance(voice, str) or voice not in (_OPENAI_VOICES if provider == "openai" else _GEMINI_VOICES):
        raise ProviderValidationError("unsupported speech voice")
    if provider == "openai":
        result = await _post(
            "https://api.openai.com/v1/audio/speech",
            headers={"Authorization": f"Bearer {route['api_key']}"}, max_bytes=_MAX_OUTPUT_BYTES,
            json_body={"model": model, "input": text, "voice": voice, "response_format": format},
        )
        return _output_audio(result, format)
    result = _json(await _post(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        headers={"x-goog-api-key": route["api_key"]}, max_bytes=_MAX_INLINE_JSON_BYTES,
        json_body={
            "model": model, "input": [{"type": "user_input", "content": [{"type": "text", "text": text}]}],
            "response_format": {"type": "audio"},
            "generation_config": {"speech_config": [{"voice": voice}]}, "store": False,
        },
    ))
    audio = _gemini_output(result, "audio")
    return _decode_audio(audio.get("data"), format, audio.get("mime_type"))


async def transcribe_audio(route: dict, *, data: bytes, mime_type: str,
                           language: str | None = None, prompt: str | None = None) -> str:
    """Transcribe bounded inline audio with a real, configured duration rate."""
    validate_audio_request(route, kind="stt", format=mime_type)
    provider, model = _model(route, "stt")
    extension = _source_audio(data, mime_type)
    if provider == "gemini" and len(data) > _MAX_GEMINI_INLINE_BYTES:
        raise ProviderValidationError("source audio exceeds inline request limit")
    if language is not None and (not isinstance(language, str) or not language.isascii() or not language.replace("-", "").isalpha() or len(language) > 16):
        raise ProviderValidationError("invalid transcription language")
    if prompt is not None and (not isinstance(prompt, str) or len(prompt) > _MAX_TEXT_CHARS):
        raise ProviderValidationError("invalid transcription prompt")
    if provider == "openai":
        form = {"model": model, "response_format": "json"}
        if language:
            form["language"] = language
        if prompt:
            form["prompt"] = prompt
        result = _json(await _post(
            "https://api.openai.com/v1/audio/transcriptions",
            headers={"Authorization": f"Bearer {route['api_key']}"}, max_bytes=_MAX_TRANSCRIPT_BYTES,
            form=form, files={"file": (f"audio.{extension}", data, mime_type)},
        ))
        text = result.get("text")
    else:
        instruction = "Generate a transcript of the speech."
        if language:
            instruction += f" Use {language}."
        if prompt:
            instruction += f" Context: {prompt}"
        result = _json(await _post(
            "https://generativelanguage.googleapis.com/v1beta/interactions",
            headers={"x-goog-api-key": route["api_key"]}, max_bytes=_MAX_TRANSCRIPT_BYTES,
            json_body={"model": model, "input": [
                {"type": "text", "text": instruction},
                {"type": "audio", "mime_type": mime_type, "data": base64.b64encode(data).decode("ascii")},
            ], "store": False},
        ))
        text = _gemini_output(result, "text").get("text")
    if not isinstance(text, str) or len(text.encode("utf-8")) > _MAX_TRANSCRIPT_BYTES:
        raise AudioTransportError("audio provider returned invalid transcription text")
    return text
