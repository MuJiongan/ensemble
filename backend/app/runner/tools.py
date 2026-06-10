"""Tool registry. Each tool is a plain Python callable, plus a JSON schema for LLM tool-calling."""
from __future__ import annotations
import base64
import os
import subprocess
from pathlib import Path

import httpx


def shell(command: str, timeout: int = 30) -> dict:
    """Run a shell command. Returns {stdout, stderr, returncode}."""
    try:
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True, timeout=timeout
        )
        return {
            "stdout": result.stdout,
            "stderr": result.stderr,
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired as e:
        return {
            "stdout": (e.stdout or "") if isinstance(e.stdout, str) else "",
            "stderr": "TIMEOUT",
            "returncode": -1,
        }


# read_file guards: a 4 KB sample is sniffed so binary files error instead of
# decoding to replacement-char noise, single lines are clipped so one minified
# bundle line can't flood the context, and total output is byte-capped with a
# `next_offset` continuation handle.
_READ_SAMPLE_BYTES = 4096
_READ_MAX_LINE_CHARS = 2000
_READ_MAX_BYTES = 50 * 1024
_BINARY_EXTENSIONS = {
    ".zip", ".tar", ".gz", ".exe", ".dll", ".so", ".class", ".jar", ".war",
    ".7z", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".odt", ".ods",
    ".odp", ".bin", ".dat", ".obj", ".o", ".a", ".lib", ".wasm", ".pyc", ".pyo",
}


# Image reading: sniff the MIME from magic bytes and return the file as a
# base64 attachment the LLM layer delivers as native image content blocks.
# Cap sized to the tightest provider limit — Anthropic allows 10 MB of base64
# per image (~7.5 MB raw).
_IMAGE_MAX_BYTES = 7 * 1024 * 1024


def _sniff_image_mime(sample: bytes) -> str | None:
    """MIME for the attachment-supported image formats (jpeg/png/gif/webp),
    detected from magic bytes; None for everything else."""
    if sample.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if sample.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if sample.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if sample[:4] == b"RIFF" and sample[8:12] == b"WEBP":
        return "image/webp"
    return None


def reject_image_attachments(tool: str, model: str) -> dict:
    """Replacement tool result when a tool returned an image to a model that
    can't accept image input. States the fact only — the node can't switch
    its own model, so no remedy is suggested here; the orchestrator routes
    model-capability failures to Settings."""
    return {
        "error": (
            f"{tool} returned an image, but the model handling this request "
            f"({model}) does not accept image input"
        )
    }


def strip_attachment_data(result):
    """Copy of a tool result with attachment base64 replaced by a size note —
    for event streams, run records, and provider paths that can't carry binary
    content. The original result is left untouched."""
    if not (isinstance(result, dict) and result.get("attachments")):
        return result
    stripped = []
    for a in result["attachments"]:
        a = {k: v for k, v in a.items() if k != "data"}
        a["note"] = "binary content delivered as a model-visible attachment"
        stripped.append(a)
    return {**result, "attachments": stripped}


def prepare_tool_result(result, *, tool: str, model: str, accepts_images: bool):
    """Split a tool result for the agent loops: returns ``(recorded,
    attachments)``. ``recorded`` is what events, run records, and the JSON the
    model reads carry (never base64); ``attachments`` ride the tool message
    for the protocol adapters, or None when the model can't accept images —
    in which case ``recorded`` is the replacement error result."""
    attachments = result.get("attachments") if isinstance(result, dict) else None
    if not attachments:
        return result, None
    if not accepts_images:
        return reject_image_attachments(tool, model), None
    return strip_attachment_data(result), attachments


def _looks_binary(p: Path, sample: bytes) -> bool:
    if p.suffix.lower() in _BINARY_EXTENSIONS:
        return True
    if not sample:
        return False
    if 0 in sample:
        return True
    non_printable = sum(1 for b in sample if b < 9 or 13 < b < 32)
    return non_printable / len(sample) > 0.3


def _suggest_similar(p: Path) -> list[str]:
    """Up to 3 sibling paths whose names overlap the missed basename."""
    base = p.name.lower()
    try:
        siblings = sorted(p.parent.iterdir())
    except OSError:
        return []
    return [
        str(s) for s in siblings
        if base in s.name.lower() or s.name.lower() in base
    ][:3]


def read_file(file_path: str, offset: int = 1, limit: int = 2000) -> dict:
    """Read a text file (or list a directory). Returns up to ``limit`` lines
    starting at 1-based line ``offset`` — page through large files rather than
    reading them whole. Expected failures (missing path, binary file,
    out-of-range offset) come back as {error: str} so an LLM caller can read
    and react. ``content`` is the file's raw text — no line-number prefixes —
    so slices can be passed straight to ``edit_file`` as ``old_string``."""
    p = Path(file_path)
    # Clamp the paging params: offset=0 means line 1, and a non-positive
    # limit would otherwise return an empty window whose next_offset never
    # advances — an LLM following the continuation contract would loop on
    # identical calls forever.
    offset = max(offset, 1)
    limit = max(limit, 1)
    start = offset - 1

    if p.is_dir():
        try:
            entries = sorted(
                e.name + "/" if e.is_dir() else e.name for e in p.iterdir()
            )
        except OSError as e:
            return {"error": f"{type(e).__name__}: {e}"}
        chunk = entries[start : start + limit]
        listing = {
            "path": str(p),
            "type": "directory",
            "entries": chunk,
            "total_entries": len(entries),
            "offset": offset,
            "truncated": start + len(chunk) < len(entries),
        }
        if listing["truncated"]:
            listing["next_offset"] = offset + len(chunk)
        return listing

    if not p.is_file():
        err = f"file not found: {file_path}"
        suggestions = _suggest_similar(p)
        if suggestions:
            err += "; did you mean one of: " + ", ".join(suggestions)
        return {"error": err}

    try:
        with p.open("rb") as f:
            sample = f.read(_READ_SAMPLE_BYTES)
    except OSError as e:
        return {"error": f"{type(e).__name__}: {e}"}

    mime = _sniff_image_mime(sample)
    if mime is not None:
        size = p.stat().st_size
        if size > _IMAGE_MAX_BYTES:
            return {
                "error": (
                    f"image is {size} bytes, over the {_IMAGE_MAX_BYTES} byte "
                    "attachment limit; downsample the image"
                )
            }
        try:
            raw = p.read_bytes()
        except OSError as e:
            return {"error": f"{type(e).__name__}: {e}"}
        if len(raw) > _IMAGE_MAX_BYTES:
            # The stat() pre-check raced a growing file — verify what we
            # actually read.
            return {
                "error": (
                    f"image is {len(raw)} bytes, over the {_IMAGE_MAX_BYTES} "
                    "byte attachment limit; downsample the image"
                )
            }
        return {
            "path": str(p),
            "type": "image",
            "mime": mime,
            "message": "Image read successfully",
            "attachments": [
                {
                    "type": "file",
                    "mime": mime,
                    "bytes": len(raw),
                    "data": base64.standard_b64encode(raw).decode("ascii"),
                }
            ],
        }

    if sample.startswith(b"%PDF-"):
        return {
            "error": (
                f"cannot read PDF: {file_path}; extract its text instead, "
                "or use web_fetch for remote PDFs"
            )
        }
    if _looks_binary(p, sample):
        return {"error": f"cannot read binary file: {file_path}"}

    raw: list[str] = []
    total = 0
    bytes_used = 0
    capped = False  # stopped before EOF (window full or byte cap)
    try:
        with p.open("r", encoding="utf-8", errors="replace") as f:
            for i, line in enumerate(f):
                total = i + 1
                if i < start:
                    continue
                if len(raw) >= limit:
                    # Window full and at least one more line exists — stop
                    # rather than decode the rest of the file just to count it.
                    capped = True
                    break
                line = line.rstrip("\n")
                if len(line) > _READ_MAX_LINE_CHARS:
                    line = (
                        line[:_READ_MAX_LINE_CHARS]
                        + f"... (line truncated to {_READ_MAX_LINE_CHARS} chars)"
                    )
                size = len(line.encode("utf-8")) + (1 if raw else 0)
                if bytes_used + size > _READ_MAX_BYTES:
                    capped = True
                    break
                raw.append(line)
                bytes_used += size
    except OSError as e:
        return {"error": f"{type(e).__name__}: {e}"}

    if total < offset and not (total == 0 and offset == 1):
        return {
            "error": f"offset {offset} is out of range for this file ({total} lines)"
        }

    result = {
        "path": str(p),
        "type": "file",
        "content": "\n".join(raw),
        "offset": offset,
        "lines_returned": len(raw),
        # Unknown when the scan stopped early — counting the rest would mean
        # decoding the whole file we just declined to return.
        "total_lines": None if capped else total,
        "truncated": capped,
    }
    if capped:
        result["next_offset"] = offset + len(raw)
    return result


def write_file(file_path: str, content: str) -> dict:
    """Write ``content`` to a file, creating parent directories and
    overwriting any existing file. Returns {path, bytes_written}."""
    p = Path(file_path)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        data = content.encode("utf-8")
        p.write_bytes(data)
    except OSError as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return {"path": str(p), "bytes_written": len(data)}


def edit_file(
    file_path: str, old_string: str, new_string: str, replace_all: bool = False
) -> dict:
    """Exact string replacement in a text file. ``old_string`` must match the
    file contents exactly (whitespace included) and, unless ``replace_all``,
    exactly once. Ambiguous or missing matches come back as {error: str}."""
    p = Path(file_path)
    if p.is_dir():
        return {"error": f"is a directory, not a file: {file_path}"}
    if not p.is_file():
        return {"error": f"file not found: {file_path}"}
    if old_string == new_string:
        return {"error": "old_string and new_string are identical"}
    if not old_string:
        return {"error": "old_string must not be empty"}
    try:
        raw = p.read_bytes()
    except OSError as e:
        return {"error": f"{type(e).__name__}: {e}"}
    # Strict decode: errors="replace" here would rewrite every undecodable
    # byte in the whole file as U+FFFD on write-back — silent corruption far
    # from the edited string. It also doubles as the binary-file guard.
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"error": f"cannot edit non-UTF-8 or binary file: {file_path}"}
    # Match on LF (read_file's content is newline-translated, so old_string
    # slices arrive LF-only) and restore CRLF on write so one edit doesn't
    # rewrite every line ending. Mixed-endings files come out uniformly CRLF.
    crlf = "\r\n" in text
    if crlf:
        text = text.replace("\r\n", "\n")
        old_string = old_string.replace("\r\n", "\n")
        new_string = new_string.replace("\r\n", "\n")
    count = text.count(old_string)
    if count == 0:
        return {"error": "old_string not found in file"}
    if count > 1 and not replace_all:
        return {
            "error": (
                f"old_string matches {count} times; add surrounding context to "
                "make it unique, or pass replace_all=True"
            )
        }
    new_text = text.replace(old_string, new_string)
    if crlf:
        new_text = new_text.replace("\n", "\r\n")
    try:
        p.write_bytes(new_text.encode("utf-8"))
    except OSError as e:
        return {"error": f"{type(e).__name__}: {e}"}
    return {"path": str(p), "replacements": count}


def web_search(query: str, max_results: int = 10) -> dict:
    """Web search via parallel.ai."""
    api_key = os.getenv("PARALLEL_API_KEY", "")
    if not api_key:
        return {"error": "PARALLEL_API_KEY not set", "results": []}
    with httpx.Client(timeout=60) as client:
        r = client.post(
            "https://api.parallel.ai/v1beta/search",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={"objective": query, "max_results": max_results},
        )
        if r.status_code >= 400:
            return {"error": f"parallel.ai {r.status_code}: {r.text}", "results": []}
        try:
            return r.json()
        except Exception:
            return {"error": "non-json response", "raw": r.text}


def web_fetch(urls: list[str], objective: str, full_content: bool) -> dict:
    """Fetch URL(s) as LLM-clean markdown via parallel.ai Extract.

    The caller chooses ``full_content``: ``True`` returns the entire page
    markdown (use when reading an article/paper/doc end-to-end); ``False``
    returns only objective-targeted excerpts (cheaper; use when looking up
    a specific fact).
    """
    api_key = os.getenv("PARALLEL_API_KEY", "")
    if not api_key:
        return {"error": "PARALLEL_API_KEY not set", "results": []}
    body: dict = {"urls": urls, "objective": objective}
    if full_content:
        body["advanced_settings"] = {"full_content": True}
    with httpx.Client(timeout=120) as client:
        r = client.post(
            "https://api.parallel.ai/v1/extract",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json=body,
        )
        if r.status_code >= 400:
            return {"error": f"parallel.ai {r.status_code}: {r.text}", "results": []}
        try:
            return r.json()
        except Exception:
            return {"error": "non-json response", "raw": r.text}


REGISTRY = {
    "shell": shell,
    "read_file": read_file,
    "write_file": write_file,
    "edit_file": edit_file,
    "web_search": web_search,
    "web_fetch": web_fetch,
}

# MCP tools, grouped for the dotted direct-call form
# ``ctx.tools.<server>.<tool>(...)``. Populated at run start by
# ``app.runner.mcp.register_runtime_tools``. Shape: {server_attr: {tool_attr:
# qualified_registry_key}}.
MCP_NAMESPACES: dict[str, dict[str, str]] = {}


TOOL_SCHEMAS = {
    "shell": {
        "type": "function",
        "function": {
            "name": "shell",
            "description": "Execute a shell command and return stdout/stderr/returncode.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to execute"},
                    "timeout": {"type": "integer", "description": "Timeout seconds", "default": 30},
                },
                "required": ["command"],
            },
        },
    },
    "read_file": {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": (
                "Read a file (or list a directory) from the local filesystem. "
                "Text files return up to `limit` lines starting at 1-based "
                "line `offset`; when `truncated` is true, call again with "
                "`offset` set to the returned `next_offset`. Lines longer than "
                "2000 chars are clipped and output is capped at 50 KB. Images "
                "(png/jpeg/gif/webp) are returned as attachments you can see "
                "directly. PDFs and other binary files return an error."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the file or directory to read"},
                    "offset": {
                        "type": "integer",
                        "description": "1-based line number to start reading from",
                        "default": 1,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Maximum number of lines to return",
                        "default": 2000,
                    },
                },
                "required": ["file_path"],
            },
        },
    },
    "write_file": {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": (
                "Write content to a file, creating parent directories and "
                "overwriting any existing file. For partial changes to an "
                "existing file, use edit_file instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the file to write"},
                    "content": {"type": "string", "description": "Content to write to the file"},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    "edit_file": {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": (
                "Exact string replacement in a text file. old_string must match "
                "the file contents exactly (whitespace included) and exactly "
                "once — include surrounding lines to disambiguate, or pass "
                "replace_all=true to replace every occurrence."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "Path to the file to modify"},
                    "old_string": {"type": "string", "description": "Exact text to replace"},
                    "new_string": {
                        "type": "string",
                        "description": "Text to replace it with (must differ from old_string)",
                    },
                    "replace_all": {
                        "type": "boolean",
                        "description": "Replace every occurrence of old_string",
                        "default": False,
                    },
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    },
    "web_search": {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for the given query.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "max_results": {"type": "integer", "default": 10},
                },
                "required": ["query"],
            },
        },
    },
    "web_fetch": {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": (
                "Fetch one or more URLs as LLM-clean markdown via parallel.ai Extract. "
                "Handles JS-rendered pages and PDFs."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "urls": {"type": "array", "items": {"type": "string"}},
                    "objective": {
                        "type": "string",
                        "description": "What you're trying to extract; narrows the excerpts.",
                    },
                    "full_content": {
                        "type": "boolean",
                        "description": (
                            "True = return the entire page markdown (use when you need "
                            "to read a page end-to-end: articles, papers, docs). "
                            "False = return only objective-targeted excerpts (cheaper; "
                            "use when looking up a specific fact). Decide per call."
                        ),
                    },
                },
                "required": ["urls", "objective", "full_content"],
            },
        },
    },
}
