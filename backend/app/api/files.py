"""File-viewer API — serve the bytes behind a path so the browser can render it.

The renderer is a sandboxed browser tab with no filesystem access, so a path
shown in the UI (a `path`-typed input, a path-valued output) is just a string
until something with disk access reads it. That's this endpoint: given an
absolute path, classify it (text / markdown / html / image / pdf / video /
binary / directory) and return the content the frontend needs to display it.

Limits are deliberately looser than the LLM-facing ``read_file`` tool — a human
scrolling a panel can afford a much bigger window than a model's context can.
"""
from __future__ import annotations

import base64
import mimetypes
import os
import subprocess
import sys
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel

from app.runner.tools import _looks_binary, _sniff_image_mime

router = APIRouter(prefix="/api/files", tags=["files"])

# A human can scroll far more than a model's context affords, but we still cap
# to keep one giant file from pinning memory or freezing the panel.
_TEXT_MAX_BYTES = 2 * 1024 * 1024          # 2 MB of decoded text
_INLINE_MAX_BYTES = 25 * 1024 * 1024       # base64-inlined image/pdf ceiling
_DIR_MAX_ENTRIES = 2000
_READ_SAMPLE_BYTES = 4096

# Videos are explicitly out of scope for preview — we report the kind so the
# UI can say so rather than trying to inline a multi-hundred-MB blob.
_VIDEO_EXTS = {
    ".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".wmv", ".flv", ".mpeg",
    ".mpg", ".3gp", ".ogv",
}
# Image extensions beyond what magic-byte sniffing covers (svg is text, bmp/ico/
# avif aren't in the attachment sniffer). data: URLs render all of these.
_IMAGE_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".bmp", ".ico", ".avif",
    ".apng", ".tif", ".tiff",
}
_MARKDOWN_EXTS = {".md", ".markdown", ".mdx"}
_HTML_EXTS = {".html", ".htm", ".xhtml"}


def _data_url(mime: str, raw: bytes) -> str:
    return f"data:{mime};base64,{base64.standard_b64encode(raw).decode('ascii')}"


def _classify_file(p: Path) -> dict:
    ext = p.suffix.lower()
    size = p.stat().st_size
    guessed, _ = mimetypes.guess_type(p.name)

    try:
        with p.open("rb") as f:
            sample = f.read(_READ_SAMPLE_BYTES)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")

    base = {"path": str(p), "name": p.name, "mime": guessed, "size": size}

    # Video — reported, never inlined.
    if ext in _VIDEO_EXTS or (guessed or "").startswith("video/"):
        return {**base, "kind": "video", "mime": guessed or f"video/{ext.lstrip('.')}"}

    # Image — magic bytes first, then extension/mime (covers svg/bmp/ico/avif).
    sniffed = _sniff_image_mime(sample)
    is_image = sniffed is not None or ext in _IMAGE_EXTS or (guessed or "").startswith("image/")
    if is_image:
        if size > _INLINE_MAX_BYTES:
            return {**base, "kind": "binary", "note": "image too large to preview inline"}
        mime = sniffed or guessed or (
            "image/svg+xml" if ext == ".svg" else f"image/{ext.lstrip('.') or 'png'}"
        )
        try:
            raw = p.read_bytes()
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")
        return {**base, "kind": "image", "mime": mime, "data_url": _data_url(mime, raw)}

    # PDF — inlined as a data: URL for <iframe>/<embed>.
    if sample.startswith(b"%PDF-") or ext == ".pdf":
        if size > _INLINE_MAX_BYTES:
            return {**base, "kind": "binary", "note": "pdf too large to preview inline"}
        try:
            raw = p.read_bytes()
        except OSError as e:
            raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")
        return {**base, "kind": "pdf", "mime": "application/pdf",
                "data_url": _data_url("application/pdf", raw)}

    # Non-text binary — report mime/size, no content.
    if _looks_binary(p, sample):
        return {**base, "kind": "binary"}

    # Text — decode up to the cap, flag truncation, and tag markdown/html so the
    # frontend can offer a rendered/source toggle.
    truncated = size > _TEXT_MAX_BYTES
    try:
        with p.open("rb") as f:
            data = f.read(_TEXT_MAX_BYTES)
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")
    content = data.decode("utf-8", errors="replace")

    if ext in _MARKDOWN_EXTS:
        kind = "markdown"
    elif ext in _HTML_EXTS:
        kind = "html"
    else:
        kind = "text"

    return {
        **base,
        "kind": kind,
        "content": content,
        "truncated": truncated,
        "total_lines": content.count("\n") + 1 if content else 0,
        "language": ext.lstrip(".") or None,
    }


def _list_directory(p: Path) -> dict:
    try:
        children = sorted(
            p.iterdir(), key=lambda e: (not e.is_dir(), e.name.lower())
        )
    except OSError as e:
        raise HTTPException(status_code=400, detail=f"{type(e).__name__}: {e}")
    entries = [
        {"name": c.name, "is_dir": c.is_dir()}
        for c in children[:_DIR_MAX_ENTRIES]
    ]
    out = {
        "path": str(p),
        "name": p.name or str(p),
        "kind": "directory",
        "entries": entries,
        "total_entries": len(children),
    }
    if len(children) > _DIR_MAX_ENTRIES:
        out["truncated"] = True
        out["note"] = f"showing first {_DIR_MAX_ENTRIES} of {len(children)} entries"
    return out


class OpenRequest(BaseModel):
    path: str
    # reveal=True highlights the file in the OS file manager instead of opening
    # it in its default application.
    reveal: bool = False


def _launch(p: Path, reveal: bool) -> None:
    """Hand the path to the OS so it opens in the user's default app (or the
    file manager when ``reveal``). Args are passed as a list — never a shell
    string — so a path can't inject a command. This is a single-user local
    tool; the backend and the desktop are the same machine."""
    try:
        if sys.platform == "darwin":
            cmd = ["open", "-R", str(p)] if reveal else ["open", str(p)]
            subprocess.run(cmd, check=True)
        elif sys.platform.startswith("win"):
            if reveal:
                subprocess.run(["explorer", f"/select,{p}"])
            else:
                os.startfile(str(p))  # type: ignore[attr-defined]  # Windows-only
        else:  # linux / *bsd
            target = p.parent if reveal else p
            subprocess.run(["xdg-open", str(target)], check=True)
    except (OSError, subprocess.CalledProcessError) as e:
        raise HTTPException(status_code=500, detail=f"could not open: {e}")


@router.post("/open")
def open_file(body: OpenRequest) -> dict:
    """Open ``path`` in the OS default app, or reveal it in the file manager.

    Refuses paths that don't exist so a stray path-shaped string can't launch
    anything unexpected."""
    p = Path(body.path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass
    if not p.exists():
        raise HTTPException(status_code=404, detail=f"no file or directory at: {body.path}")
    _launch(p, reveal=body.reveal)
    return {"ok": True}


@router.get("")
def get_file(path: str = Query(..., description="Absolute path to read")) -> dict:
    """Classify and return the contents of ``path`` for in-app preview.

    Resolves ``~`` and relative segments to an absolute path. 404 when nothing
    exists there (the caller may have offered a path-shaped string that isn't a
    real file — the UI falls back to showing the raw text)."""
    p = Path(path).expanduser()
    try:
        p = p.resolve()
    except OSError:
        pass

    if p.is_dir():
        return _list_directory(p)
    if p.is_file():
        return _classify_file(p)
    raise HTTPException(status_code=404, detail=f"no file or directory at: {path}")
