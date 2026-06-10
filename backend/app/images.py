"""Inbound chat-attachment validation + normalization.

Attachments arrive as base64 data URLs. Images over the dimension / payload
caps are progressively downscaled (PNG first, then a JPEG quality ladder)
until they fit; Pillow is the resizer, and when it isn't importable an
oversized image passes through untouched and the provider enforces its own
limits. PDFs can't be downscaled, so they get a plain byte cap.
"""
from __future__ import annotations

import base64
import binascii
import io
import re

MAX_BASE64_BYTES = 5 * 1024 * 1024
MAX_WIDTH = 2000
MAX_HEIGHT = 2000
JPEG_QUALITIES = (80, 85, 70, 55, 40)
# ~15 MB of PDF binary; under Anthropic's 32 MB request cap and Gemini's
# 20 MB inline-data cap with headroom for the rest of the prompt.
PDF_MAX_BASE64_BYTES = 20 * 1024 * 1024
# Text files are inlined into the prompt as plain text, so the cap is a
# token-budget guard, not a transport limit (~384 KB of text ≈ 100k tokens).
TEXT_MAX_BASE64_BYTES = 512 * 1024

_DATA_URL_RE = re.compile(r"^data:((?:image/|application/|text/)[\w.+-]+);base64,(.*)$", re.DOTALL)


class ImageError(ValueError):
    """The attachment is not usable (bad shape, empty, undecodable, an
    unsupported type, or impossible to fit under the size caps)."""


def parse_data_url(url: str) -> tuple[str, str]:
    """Split a ``data:<mime>;base64,...`` URL into (mime, base64 payload)."""
    m = _DATA_URL_RE.match(url or "")
    if not m:
        raise ImageError("attachment must be a base64 data URL (data:image/...;base64,...)")
    mime, b64 = m.group(1).lower(), m.group(2).strip()
    if not b64:
        raise ImageError("attachment data is empty")
    return mime, b64


def normalize_attachment(url: str) -> str:
    """Validate one attachment data URL: images run the downscale pipeline,
    PDFs and text files get byte caps, anything else is rejected."""
    mime, b64 = parse_data_url(url)
    if mime.startswith("image/"):
        return normalize_data_url(url)
    if mime != "application/pdf" and not mime.startswith("text/"):
        raise ImageError(f"unsupported attachment type {mime} (images, PDFs, and text files only)")
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise ImageError("attachment data is not valid base64") from None
    if not raw:
        raise ImageError("attachment data is empty")
    if mime == "application/pdf":
        if len(b64) > PDF_MAX_BASE64_BYTES:
            raise ImageError(
                f"PDF exceeds the {PDF_MAX_BASE64_BYTES // (1024 * 1024)} MB attachment cap"
            )
    elif len(b64) > TEXT_MAX_BASE64_BYTES:
        raise ImageError(
            f"text file exceeds the {TEXT_MAX_BASE64_BYTES // 1024} KB attachment cap"
        )
    return url


def normalize_data_url(url: str) -> str:
    """Validate one image data URL and downscale it when it exceeds the caps."""
    mime, b64 = parse_data_url(url)
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        raise ImageError("image data is not valid base64") from None
    if not raw:
        raise ImageError("image data is empty")

    try:
        from PIL import Image
    except ImportError:
        return url

    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
    except Exception:
        raise ImageError("image could not be decoded") from None

    width, height = img.size
    if width <= MAX_WIDTH and height <= MAX_HEIGHT and len(b64) <= MAX_BASE64_BYTES:
        return url

    scale = min(1.0, MAX_WIDTH / width, MAX_HEIGHT / height)
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    seen: set[tuple[int, int]] = set()
    for _ in range(32):
        if size in seen:
            break
        seen.add(size)
        resized = img.resize(size, Image.LANCZOS)
        if candidate := _encode_under_cap(resized):
            return candidate
        size = (
            1 if size[0] == 1 else max(1, int(size[0] * 0.75)),
            1 if size[1] == 1 else max(1, int(size[1] * 0.75)),
        )
    raise ImageError(
        f"image {width}x{height} exceeds the size caps and could not be "
        f"resized under {MAX_WIDTH}x{MAX_HEIGHT}/{MAX_BASE64_BYTES} base64 bytes"
    )


def _encode_under_cap(img) -> str | None:
    """Re-encode one resized frame as PNG, then down the JPEG quality ladder,
    returning the first data URL whose base64 payload fits the byte cap."""
    candidates: list[tuple[str, dict]] = [("image/png", {"format": "PNG"})]
    candidates += [
        ("image/jpeg", {"format": "JPEG", "quality": q}) for q in JPEG_QUALITIES
    ]
    for mime, kwargs in candidates:
        frame = img.convert("RGB") if kwargs["format"] == "JPEG" and img.mode not in ("RGB", "L") else img
        buf = io.BytesIO()
        frame.save(buf, **kwargs)
        b64 = base64.b64encode(buf.getvalue()).decode()
        if len(b64) <= MAX_BASE64_BYTES:
            return f"data:{mime};base64,{b64}"
    return None
