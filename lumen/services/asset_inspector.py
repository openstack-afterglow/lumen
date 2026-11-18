"""Fixed-argv child process for untrusted chat-media inspection."""

from __future__ import annotations

import json
import re
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
    mime = matches[0].mime_type.lower()
    return "audio/wav" if mime in {"audio/wave", "audio/x-wav", "audio/vnd.wave"} else mime


def _image(path: Path) -> dict[str, int]:
    from PIL import Image

    warnings.simplefilter("error", Image.DecompressionBombWarning)
    with Image.open(path) as image:
        image.verify()
    with Image.open(path) as image:
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
            raise ValueError("image dimensions exceed limit")
        if image.getexif().get(274) in (5, 6, 7, 8):
            width, height = height, width
        # An edit overlay needs a real transparent pixel, not just an alpha channel.
        transparent = (image.format == "PNG" and ("A" in image.getbands() or "transparency" in image.info)
                       and image.convert("RGBA").getchannel("A").getextrema()[0] == 0)
    return {"width": width, "height": height, **({"has_transparency": 1} if transparent else {})}


def _gif(path: Path) -> dict[str, int]:
    from PIL import Image

    with path.open("rb") as source:
        source.seek(-1, 2)
        if source.read(1) != b";":
            raise ValueError("GIF trailer is missing")

    # Decode bounded frames, not merely the GIF marker and canvas dimensions.
    with Image.open(path, formats=["GIF"]) as image:
        width, height = image.size
        if not (0 < width <= 3840 and 0 < height <= 3840 and width * height <= 8_294_400):
            raise ValueError("GIF frame dimensions exceed limit")
        for frame in range(17):
            try:
                image.seek(frame)
            except EOFError:
                if frame < 2:
                    raise ValueError("GIF must be animated") from None
                return {"width": width, "height": height, "frame_count": frame}
            if frame == 16 or image.size != (width, height) or width * height * (frame + 1) > 10 * 1024 * 1024:
                raise ValueError("GIF frame bounds exceeded")
            image.load()
    raise ValueError("GIF frame bounds exceeded")


_SVG_NAMESPACE = "http://www.w3.org/2000/svg "
_SVG_ELEMENTS = frozenset({
    "svg", "g", "defs", "title", "desc", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "linearGradient", "radialGradient", "stop", "clipPath", "mask", "pattern",
})
_SVG_ATTRIBUTES = frozenset({
    "id", "role", "aria-label", "aria-labelledby", "aria-describedby", "viewBox", "preserveAspectRatio",
    "version", "baseProfile",
    "x", "y", "x1", "y1", "x2", "y2", "cx", "cy", "r", "rx", "ry", "width", "height", "d", "points", "transform",
    "textLength", "lengthAdjust", "dx", "dy", "rotate",
    "gradientUnits", "gradientTransform", "fx", "fy", "fr", "spreadMethod", "offset",
    "patternUnits", "patternContentUnits", "patternTransform", "clipPathUnits", "maskUnits", "maskContentUnits",
    "fill", "stroke", "fill-rule", "clip-rule", "clip-path", "mask", "opacity", "fill-opacity", "stroke-opacity",
    "stroke-width", "stroke-linejoin", "stroke-linecap", "stroke-dasharray", "stroke-dashoffset", "stroke-miterlimit",
    "stop-color", "stop-opacity", "color", "font-family", "font-size", "font-weight", "font-style", "text-anchor",
    "dominant-baseline", "alignment-baseline", "letter-spacing", "vector-effect", "display", "visibility",
})
_LOCAL_SVG_REFERENCE = re.compile(r"url\(#[a-zA-Z_][a-zA-Z_0-9.-]*\)")
_SVG_LOCAL_REFERENCE_ATTRIBUTES = frozenset({"fill", "stroke", "clip-path", "mask"})
_SVG_CLIP_ATTRIBUTES = frozenset({"clip-path", "mask"})
_SVG_IMAGE_FUNCTION = re.compile(r"(?:url|(?:-webkit-)?image-set|image|src|cross-fade)\s*\(", re.IGNORECASE)


def _svg(path: Path) -> dict[str, int]:
    from xml.parsers import expat

    # Validate original bytes as UTF-8 and static SVG; never serve a modified
    # document whose syntax might differ under a browser's XML decoder.
    text = path.read_bytes().decode("utf-8")
    parser = expat.ParserCreate(encoding="UTF-8", namespace_separator=" ")
    depth = nodes = 0

    def reject(*_args) -> None:
        raise ValueError("unsupported SVG content")

    def xml_decl(_version, encoding, _standalone) -> None:
        if encoding is not None and encoding.lower() != "utf-8":
            reject()

    def start(name: str, attrs: dict[str, str]) -> None:
        nonlocal depth, nodes
        local = name.removeprefix(_SVG_NAMESPACE)
        if (not name.startswith(_SVG_NAMESPACE) or local not in _SVG_ELEMENTS
                or (depth == 0 and local != "svg") or depth >= 64 or nodes >= 50_000):
            reject()
        depth += 1
        nodes += 1
        for key, value in attrs.items():
            if key not in _SVG_ATTRIBUTES:
                reject()
            if key in _SVG_LOCAL_REFERENCE_ATTRIBUTES and _LOCAL_SVG_REFERENCE.fullmatch(value):
                continue
            if key in _SVG_CLIP_ATTRIBUTES and value != "none":
                reject()
            if "\\" in value or "/*" in value or _SVG_IMAGE_FUNCTION.search(value):
                reject()

    def end(_name: str) -> None:
        nonlocal depth
        depth -= 1

    parser.XmlDeclHandler = xml_decl
    parser.StartElementHandler = start
    parser.EndElementHandler = end
    parser.StartDoctypeDeclHandler = reject
    parser.ProcessingInstructionHandler = reject
    parser.ExternalEntityRefHandler = reject
    parser.Parse(text, True)
    return {}


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


def inspect(path: Path, special_image: str | None = None) -> dict[str, object]:
    if special_image in ("image/gif", "image/svg+xml"):
        metadata = _gif(path) if special_image == "image/gif" else _svg(path)
        return {"mime_type": special_image, "metadata": metadata}
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
        if len(sys.argv) not in (2, 3) or len(sys.argv) == 3 and sys.argv[2] not in ("image/gif", "image/svg+xml"):
            raise ValueError("invalid inspector invocation")
        print(json.dumps(inspect(Path(sys.argv[1]), *sys.argv[2:]), separators=(",", ":")))
        return 0
    except Exception:
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
