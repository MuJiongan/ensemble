"""Image attachments: validation/normalization, persistence round-trip, and
provider lowering. No API keys / no LLM calls."""
from __future__ import annotations

import base64
import io
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import images, models
from app.db import Base
from app.llm import anthropic_messages, gemini
from app.orchestrator import agent as orch_agent
from app.orchestrator.agent.persistence import (
    USER_PARTS_MARKER,
    _persist_user,
    _row_to_message,
    user_bubble_fields,
)

PDF_DATA_URL = "data:application/pdf;base64," + base64.b64encode(b"%PDF-1.4 fake").decode()


@pytest.fixture()
def db():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


@pytest.fixture()
def session_row(db):
    w = models.Workflow(name="test wf")
    db.add(w)
    db.commit()
    s = models.Session(workflow_id=w.id)
    db.add(s)
    db.commit()
    db.refresh(s)
    return s


def _png_data_url(width: int = 8, height: int = 8) -> str:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (200, 30, 30)).save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


# --- normalize_data_url ---

def test_normalize_rejects_non_data_url():
    with pytest.raises(images.ImageError):
        images.normalize_data_url("https://example.com/cat.png")


def test_normalize_rejects_empty_payload():
    with pytest.raises(images.ImageError):
        images.normalize_data_url("data:image/png;base64,")


def test_normalize_rejects_invalid_base64():
    with pytest.raises(images.ImageError):
        images.normalize_data_url("data:image/png;base64,!!!not-base64!!!")


def test_normalize_rejects_undecodable_image():
    junk = base64.b64encode(b"this is not a png").decode()
    with pytest.raises(images.ImageError):
        images.normalize_data_url(f"data:image/png;base64,{junk}")


def test_normalize_passes_small_image_through():
    url = _png_data_url()
    assert images.normalize_data_url(url) == url


def test_normalize_downscales_oversized_image():
    url = _png_data_url(width=images.MAX_WIDTH + 500, height=64)
    out = images.normalize_data_url(url)
    assert out != url
    mime, b64 = images.parse_data_url(out)
    assert mime in ("image/png", "image/jpeg")
    assert len(b64) <= images.MAX_BASE64_BYTES

    from PIL import Image

    img = Image.open(io.BytesIO(base64.b64decode(b64)))
    assert img.size[0] <= images.MAX_WIDTH
    assert img.size[1] <= images.MAX_HEIGHT


# --- persistence round-trip ---

def test_persist_user_plain_text_stays_raw_string(db, session_row):
    m = _persist_user(db, session_row.id, "hello")
    assert m.name is None
    assert m.content == "hello"
    assert _row_to_message(m) == {"role": "user", "content": "hello"}


def test_persist_user_with_images_round_trips(db, session_row):
    url = _png_data_url()
    m = _persist_user(db, session_row.id, "what is this?", [{"data_url": url}])
    assert m.name == USER_PARTS_MARKER

    replayed = _row_to_message(m)
    assert replayed["role"] == "user"
    assert replayed["content"] == [
        {"type": "text", "text": "what is this?"},
        {"type": "image_url", "image_url": {"url": url}},
    ]

    text, imgs, files = user_bubble_fields(m)
    assert text == "what is this?"
    assert imgs == [url]
    assert files == []


def test_persist_user_image_only_has_no_text_part(db, session_row):
    url = _png_data_url()
    m = _persist_user(db, session_row.id, "", [{"data_url": url}])
    assert json.loads(m.content) == [{"type": "image_url", "image_url": {"url": url}}]


def test_persist_user_pdf_becomes_file_part(db, session_row):
    m = _persist_user(
        db, session_row.id, "summarize", [{"data_url": PDF_DATA_URL, "filename": "report.pdf"}]
    )
    replayed = _row_to_message(m)
    assert replayed["content"] == [
        {"type": "text", "text": "summarize"},
        {"type": "file", "file": {"filename": "report.pdf", "file_data": PDF_DATA_URL}},
    ]
    text, imgs, files = user_bubble_fields(m)
    assert (text, imgs, files) == ("summarize", [], [{"name": "report.pdf", "kind": "pdf"}])


TXT_DATA_URL = "data:text/plain;base64," + base64.b64encode(b"alpha,beta\n1,2\n").decode()


def test_persist_user_text_file_inlines_on_replay(db, session_row):
    m = _persist_user(
        db, session_row.id, "analyze", [{"data_url": TXT_DATA_URL, "filename": "data.csv"}]
    )
    # Replay inlines the contents as a text part — adapters never see a
    # binary part, so text files work with every model.
    replayed = _row_to_message(m)
    assert replayed["content"] == [
        {"type": "text", "text": "analyze"},
        {"type": "text", "text": "<file: data.csv>\nalpha,beta\n1,2\n"},
    ]
    # The bubble still shows a tile, not the inlined contents.
    text, imgs, files = user_bubble_fields(m)
    assert (text, imgs, files) == ("analyze", [], [{"name": "data.csv", "kind": "txt"}])


def test_normalize_attachment_accepts_text():
    assert images.normalize_attachment(TXT_DATA_URL) == TXT_DATA_URL


def test_normalize_attachment_caps_text_size():
    big = "data:text/plain;base64," + base64.b64encode(
        b"x" * (images.TEXT_MAX_BASE64_BYTES)
    ).decode()
    with pytest.raises(images.ImageError):
        images.normalize_attachment(big)


def test_render_history_includes_user_images(db, session_row):
    url = _png_data_url()
    _persist_user(db, session_row.id, "look", [{"data_url": url}])
    bubbles = orch_agent.render_history(db, session_row.id)
    assert bubbles == [{"role": "user", "text": "look", "images": [url]}]


def test_render_history_plain_user_has_no_images_key(db, session_row):
    _persist_user(db, session_row.id, "hi")
    bubbles = orch_agent.render_history(db, session_row.id)
    assert bubbles == [{"role": "user", "text": "hi"}]


# --- attachment normalization (non-image) ---

def test_normalize_attachment_accepts_pdf():
    assert images.normalize_attachment(PDF_DATA_URL) == PDF_DATA_URL


def test_normalize_attachment_rejects_unsupported_type():
    junk = "data:application/zip;base64," + base64.b64encode(b"PK").decode()
    with pytest.raises(images.ImageError):
        images.normalize_attachment(junk)


def test_normalize_attachment_routes_images_through_resize():
    url = _png_data_url(width=images.MAX_WIDTH + 500, height=64)
    assert images.normalize_attachment(url) != url


# --- modality gate ---

def _fake_model(modalities):
    from app.catalog.models_dev import CatalogModel

    return CatalogModel(
        id="m", name="m", provider_id="openai", api_id="m",
        npm="@ai-sdk/openai", api_url="", modalities=modalities,
    )


def test_strip_unsupported_attachments_replaces_parts(monkeypatch):
    fake = _fake_model({"input": ["text"], "output": ["text"]})
    monkeypatch.setenv("LLM_PROVIDER_ID", "openai")
    monkeypatch.setattr("app.catalog.models_dev.get_model", lambda p, m: fake)

    msgs = [{"role": "user", "content": [
        {"type": "text", "text": "hi"},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
        {"type": "file", "file": {"filename": "a.pdf", "file_data": PDF_DATA_URL}},
    ]}]
    out = orch_agent._strip_unsupported_attachments(msgs, "m")
    texts = [p["text"] for p in out[0]["content"] if p["type"] == "text"]
    assert texts[0] == "hi"
    assert "does not support image input" in texts[1]
    assert "a.pdf" in texts[2] and "does not support pdf input" in texts[2]


def test_strip_unsupported_attachments_passes_capable_model(monkeypatch):
    fake = _fake_model({"input": ["text", "image", "pdf"], "output": ["text"]})
    monkeypatch.setenv("LLM_PROVIDER_ID", "openai")
    monkeypatch.setattr("app.catalog.models_dev.get_model", lambda p, m: fake)

    msgs = [{"role": "user", "content": PARTS}]
    assert orch_agent._strip_unsupported_attachments(msgs, "m") == msgs


def test_strip_unsupported_attachments_no_metadata_passthrough(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ID", "openai")
    monkeypatch.setattr("app.catalog.models_dev.get_model", lambda p, m: None)
    msgs = [{"role": "user", "content": PARTS}]
    assert orch_agent._strip_unsupported_attachments(msgs, "m") == msgs


# --- provider lowering ---

PARTS = [
    {"type": "text", "text": "what is this?"},
    {"type": "image_url", "image_url": {"url": "data:image/png;base64,QUJD"}},
]
PDF_PART = {"type": "file", "file": {"filename": "report.pdf", "file_data": PDF_DATA_URL}}
PDF_B64 = PDF_DATA_URL.split(";base64,")[1]


def test_anthropic_lowers_image_parts_to_image_blocks():
    _, msgs = anthropic_messages._lower_messages([{"role": "user", "content": PARTS}])
    assert msgs == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "what is this?"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
                },
            ],
        }
    ]


def test_anthropic_string_content_unchanged():
    _, msgs = anthropic_messages._lower_messages([{"role": "user", "content": "hi"}])
    assert msgs == [{"role": "user", "content": [{"type": "text", "text": "hi"}]}]


def test_gemini_lowers_image_parts_to_inline_data():
    _, contents = gemini._lower([{"role": "user", "content": PARTS}])
    assert contents == [
        {
            "role": "user",
            "parts": [
                {"text": "what is this?"},
                {"inlineData": {"mimeType": "image/png", "data": "QUJD"}},
            ],
        }
    ]


def test_gemini_string_content_unchanged():
    _, contents = gemini._lower([{"role": "user", "content": "hi"}])
    assert contents == [{"role": "user", "parts": [{"text": "hi"}]}]


def test_codex_responses_lowers_image_parts_to_input_image():
    from app.auth.codex_api import _to_responses_input

    instructions, items = _to_responses_input([{"role": "user", "content": PARTS}])
    assert items == [
        {
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_text", "text": "what is this?"},
                {"type": "input_image", "image_url": "data:image/png;base64,QUJD"},
            ],
        }
    ]
    # The instructions fallback uses only the visible text, never image bytes.
    assert instructions == "what is this?"


def test_codex_responses_string_content_unchanged():
    from app.auth.codex_api import _to_responses_input

    _, items = _to_responses_input([{"role": "user", "content": "hi"}])
    assert items == [
        {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "hi"}]}
    ]


def test_anthropic_lowers_pdf_to_document_block():
    _, msgs = anthropic_messages._lower_messages([{"role": "user", "content": [PDF_PART]}])
    assert msgs[0]["content"] == [
        {
            "type": "document",
            "source": {"type": "base64", "media_type": "application/pdf", "data": PDF_B64},
        }
    ]


def test_gemini_lowers_pdf_to_inline_data():
    _, contents = gemini._lower([{"role": "user", "content": [PDF_PART]}])
    assert contents[0]["parts"] == [
        {"inlineData": {"mimeType": "application/pdf", "data": PDF_B64}}
    ]


def test_codex_responses_lowers_pdf_to_input_file():
    from app.auth.codex_api import _to_responses_input

    _, items = _to_responses_input([{"role": "user", "content": [PDF_PART]}])
    assert items[0]["content"] == [
        {"type": "input_file", "filename": "report.pdf", "file_data": PDF_DATA_URL}
    ]
