"""Unit tests for the file runtime tools (read_file / write_file / edit_file).

Expected failures (missing file, ambiguous match) return {error: str} rather
than raising, so an LLM-mediated caller sees them as tool results it can react
to — these tests pin that contract.
"""
from __future__ import annotations

import base64
import json

from app.runner.tools import (
    REGISTRY,
    TOOL_SCHEMAS,
    edit_file,
    read_file,
    strip_attachment_data,
    write_file,
)


_TOOL_MSG_WITH_ATTACHMENT = {
    "role": "tool",
    "tool_call_id": "tc1",
    "content": '{"message": "Image read successfully"}',
    "attachments": [{"type": "file", "mime": "image/png", "bytes": 3, "data": "QUJD"}],
}


def test_anthropic_adapter_renders_attachments_as_tool_result_blocks():
    from app.llm.anthropic_messages import _lower_messages

    _, msgs = _lower_messages([dict(_TOOL_MSG_WITH_ATTACHMENT)])
    [user_msg] = msgs
    [tr] = user_msg["content"]
    assert tr["type"] == "tool_result"
    text_block, image_block = tr["content"]
    assert text_block["type"] == "text"
    assert image_block == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }


def test_gemini_adapter_renders_attachments_as_inline_data():
    from app.llm.gemini import _lower

    _, contents = _lower([dict(_TOOL_MSG_WITH_ATTACHMENT)])
    [user_turn] = contents
    fn_part, inline_part = user_turn["parts"]
    assert "functionResponse" in fn_part
    assert inline_part == {"inlineData": {"mimeType": "image/png", "data": "QUJD"}}


def test_openai_adapter_moves_attachments_to_user_message():
    from app.llm.openai_chat import _lower_attachments

    lowered = _lower_attachments([dict(_TOOL_MSG_WITH_ATTACHMENT)])
    tool_msg, user_msg = lowered
    assert tool_msg["role"] == "tool" and "attachments" not in tool_msg
    assert user_msg["role"] == "user"
    image_part = user_msg["content"][1]
    assert image_part["type"] == "image_url"
    assert image_part["image_url"]["url"] == "data:image/png;base64,QUJD"


def test_codex_adapter_moves_attachments_to_input_image():
    from app.auth.codex_api import _to_responses_input

    _, items = _to_responses_input([dict(_TOOL_MSG_WITH_ATTACHMENT)])
    fco, user_item = items
    assert fco["type"] == "function_call_output"
    assert "attachments" not in json.dumps(fco)
    assert user_item["type"] == "message" and user_item["role"] == "user"
    image_part = user_item["content"][1]
    assert image_part == {"type": "input_image", "image_url": "data:image/png;base64,QUJD"}


def test_supports_image_input():
    from dataclasses import replace

    from app.catalog.models_dev import CatalogModel, supports_image_input

    base = CatalogModel(id="m", name="m", provider_id="p", api_id="m", npm="", api_url="")
    # catalog miss → assume capable, the provider enforces its own limits
    assert supports_image_input(None) is True
    # explicit modalities is the only blocking signal
    assert supports_image_input(replace(base, modalities={"input": ["text", "image"]})) is True
    assert supports_image_input(replace(base, modalities={"input": ["text"]})) is False
    # no modality metadata (synthetic codex entries default attachment=False)
    # is absence of information, not a text-only signal — must pass through
    assert supports_image_input(replace(base, attachment=False, modalities=None)) is True


def test_reject_image_attachments_states_fact_without_remedy():
    from app.runner.tools import reject_image_attachments

    out = reject_image_attachments("read_file", "gpt-text-only")
    assert "does not accept image input" in out["error"]
    assert "gpt-text-only" in out["error"]
    # the node can't change its own model — no switch-model advice in-band
    assert "switch" not in out["error"].lower()
    assert "settings" not in out["error"].lower()


def test_prune_drops_attachments():
    from app.compaction import TOOL_OUTPUT_PRUNED, prune_messages

    msg = dict(_TOOL_MSG_WITH_ATTACHMENT)
    msg["content"] = "x" * 40000  # big enough to exceed the prune minimum
    pruned = prune_messages([msg], protect=0, minimum=1)
    assert pruned == 1
    assert msg["content"] == TOOL_OUTPUT_PRUNED
    assert "attachments" not in msg


def test_file_tools_registered():
    for name in ("read_file", "write_file", "edit_file"):
        assert name in REGISTRY
        assert TOOL_SCHEMAS[name]["function"]["name"] == name


def test_read_file_basic(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("one\ntwo\nthree\n")
    out = read_file(str(p))
    assert out == {
        "path": str(p),
        "type": "file",
        "content": "one\ntwo\nthree",
        "offset": 1,
        "lines_returned": 3,
        "total_lines": 3,
        "truncated": False,
    }


def test_read_file_offset_and_limit(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("\n".join(f"line{i}" for i in range(1, 11)))
    out = read_file(str(p), offset=3, limit=2)
    assert out["content"] == "line3\nline4"
    assert out["total_lines"] == 10
    assert out["lines_returned"] == 2
    assert out["truncated"] is True
    assert out["next_offset"] == 5


def test_read_file_offset_out_of_range(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("one\ntwo\n")
    assert "out of range" in read_file(str(p), offset=10)["error"]
    # offset=1 on an empty file is fine, not out of range
    empty = tmp_path / "empty.txt"
    empty.write_text("")
    out = read_file(str(empty))
    assert out["content"] == "" and out["total_lines"] == 0


def test_read_file_missing_suggests_siblings(tmp_path):
    # The match is name containment (either direction), so a partial name
    # surfaces the real file; unrelated misses get a plain error.
    (tmp_path / "config.yaml").write_text("x")
    out = read_file(str(tmp_path / "config"))
    assert "file not found" in out["error"]
    assert "config.yaml" in out["error"]
    assert "did you mean" not in read_file(str(tmp_path / "zzz.txt"))["error"]


def test_read_file_directory_listing(tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "a.txt").write_text("x")
    (tmp_path / "b.txt").write_text("x")
    out = read_file(str(tmp_path))
    assert out["type"] == "directory"
    assert out["entries"] == ["a.txt", "b.txt", "sub/"]
    assert out["total_entries"] == 3
    assert out["truncated"] is False


def test_read_file_rejects_binary(tmp_path):
    by_ext = tmp_path / "blob.zip"
    by_ext.write_bytes(b"not really a zip")
    assert "binary" in read_file(str(by_ext))["error"]
    by_content = tmp_path / "blob.txt"
    by_content.write_bytes(b"text\x00with null bytes")
    assert "binary" in read_file(str(by_content))["error"]


# 1x1 transparent PNG
_PNG = base64.standard_b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
    "YPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)


def test_read_file_image_returns_attachment(tmp_path):
    p = tmp_path / "pixel.png"
    p.write_bytes(_PNG)
    out = read_file(str(p))
    assert out["type"] == "image"
    assert out["mime"] == "image/png"
    assert out["message"] == "Image read successfully"
    [att] = out["attachments"]
    assert att["mime"] == "image/png"
    assert att["bytes"] == len(_PNG)
    assert base64.standard_b64decode(att["data"]) == _PNG


def test_read_file_pdf_errors_with_extraction_hint(tmp_path):
    p = tmp_path / "doc.pdf"
    p.write_bytes(b"%PDF-1.4 fake pdf body")
    out = read_file(str(p))
    assert "cannot read PDF" in out["error"]
    assert "extract its text" in out["error"]
    # no specific extraction library named — the model would fixate on it
    assert "pdftotext" not in out["error"]


def test_read_file_oversized_image_errors(tmp_path, monkeypatch):
    monkeypatch.setattr("app.runner.tools._IMAGE_MAX_BYTES", 10)
    p = tmp_path / "big.png"
    p.write_bytes(_PNG)
    out = read_file(str(p))
    assert "error" in out and "attachment limit" in out["error"]


def test_strip_attachment_data():
    result = {
        "type": "image",
        "attachments": [{"type": "file", "mime": "image/png", "bytes": 3, "data": "AAAA"}],
    }
    stripped = strip_attachment_data(result)
    assert "data" not in stripped["attachments"][0]
    assert stripped["attachments"][0]["bytes"] == 3
    assert "note" in stripped["attachments"][0]
    # original untouched; non-attachment results pass through unchanged
    assert result["attachments"][0]["data"] == "AAAA"
    plain = {"content": "x"}
    assert strip_attachment_data(plain) is plain


def test_read_file_clips_long_lines(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x" * 5000 + "\nshort\n")
    out = read_file(str(p))
    first = out["content"].splitlines()[0]
    assert first.startswith("x" * 2000)
    assert "truncated to 2000 chars" in first
    assert len(first) < 5000


def test_read_file_byte_cap(tmp_path):
    p = tmp_path / "f.txt"
    line = "y" * 1000
    p.write_text("\n".join(line for _ in range(100)))  # ~100 KB
    out = read_file(str(p))
    assert out["truncated"] is True
    assert out["total_lines"] is None, "total unknown when scan stops at byte cap"
    assert 0 < out["lines_returned"] < 100
    assert out["next_offset"] == out["lines_returned"] + 1


def test_write_file_creates_parents_and_overwrites(tmp_path):
    p = tmp_path / "a" / "b" / "f.txt"
    out = write_file(str(p), "hello")
    assert out == {"path": str(p), "bytes_written": 5}
    assert p.read_text() == "hello"
    write_file(str(p), "replaced")
    assert p.read_text() == "replaced"


def test_edit_file_unique_replacement(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("alpha\nbeta\ngamma\n")
    out = edit_file(str(p), "beta", "BETA")
    assert out == {"path": str(p), "replacements": 1}
    assert p.read_text() == "alpha\nBETA\ngamma\n"


def test_edit_file_ambiguous_requires_replace_all(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("x x x")
    out = edit_file(str(p), "x", "y")
    assert "error" in out and "3 times" in out["error"]
    assert p.read_text() == "x x x", "file must be untouched on error"
    out = edit_file(str(p), "x", "y", replace_all=True)
    assert out == {"path": str(p), "replacements": 3}
    assert p.read_text() == "y y y"


def test_edit_file_error_contract(tmp_path):
    p = tmp_path / "f.txt"
    p.write_text("content")
    assert "error" in edit_file(str(p), "absent", "x")
    assert "error" in edit_file(str(p), "content", "content")
    assert "error" in edit_file(str(p), "", "x")
    assert "error" in edit_file(str(tmp_path / "nope.txt"), "a", "b")
