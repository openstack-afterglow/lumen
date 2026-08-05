"""Fixed-argv child process for untrusted chat-media inspection."""

from __future__ import annotations

import json
import sys
import warnings
from pathlib import Path

_MAX_IMAGE_PIXELS = 40_000_000
_MAX_PDF_PAGES = 200
_MAX_AUDIO_DURATION_MS = 30 * 60 * 1000
_MAX_VIDEO_DURATION_MS = 10 * 60 * 1000


def _mime(path: Path) -> str:
    import puremagic

    matches = puremagic.magic_file(str(path))
    if not matches or not isinstance(getattr(matches[0], "mime_type", None), str):
        raise ValueError("unsupported file type")
    return matches[0].mime_type.lower()


def _image(path: Path) -> dict[str, int]:
    from PIL import Image

    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        width, height = image.size
    if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
        raise ValueError("image dimensions exceed limit")
    return {"width": width, "height": height}


def _pdf(path: Path) -> dict[str, int]:
    from pypdf import PdfReader

    page_count = len(PdfReader(str(path)).pages)
    if page_count > _MAX_PDF_PAGES:
        raise ValueError("PDF page count exceeds limit")
    return {"page_count": page_count}


def _audio(path: Path) -> dict[str, int]:
    from mutagen import File as MutagenFile

    audio = MutagenFile(path)
    duration_ms = int(float(audio.info.length) * 1000) if audio is not None and audio.info else -1
    if duration_ms < 0 or duration_ms > _MAX_AUDIO_DURATION_MS:
        raise ValueError("audio duration exceeds limit")
    return {"duration_ms": duration_ms}


def _video(path: Path) -> dict[str, int]:
    import subprocess

    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration:stream=width,height",
            "-of",
            "default=noprint_wrappers=1:nokey=0",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=8,
    )
    fields = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    duration_ms = int(float(fields["duration"]) * 1000)
    width, height = int(fields["width"]), int(fields["height"])
    if duration_ms < 0 or duration_ms > _MAX_VIDEO_DURATION_MS or width <= 0 or height <= 0:
        raise ValueError("video metadata exceeds limit")
    if width * height > _MAX_IMAGE_PIXELS:
        raise ValueError("video dimensions exceed limit")
    return {"duration_ms": duration_ms, "width": width, "height": height}


def inspect(path: Path) -> dict[str, object]:
    mime_type = _mime(path)
    if mime_type in {"image/jpeg", "image/png", "image/webp"}:
        metadata = _image(path)
    elif mime_type == "application/pdf":
        metadata = _pdf(path)
    elif mime_type in {"audio/mpeg", "audio/wav", "audio/x-wav", "audio/mp4", "audio/ogg", "audio/webm"}:
        metadata = _audio(path)
    elif mime_type in {"video/mp4", "video/webm"}:
        metadata = _video(path)
    else:
        raise ValueError("unsupported file type")
    return {"mime_type": mime_type, "metadata": metadata}


def _apply_limits() -> None:
    import resource

    resource.setrlimit(resource.RLIMIT_CPU, (5, 5))
    resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))


def main() -> int:
    try:
        _apply_limits()
        if len(sys.argv) != 2:
            raise ValueError("invalid inspector invocation")
        print(json.dumps(inspect(Path(sys.argv[1])), separators=(",", ":")))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
