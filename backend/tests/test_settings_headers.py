"""Middleware that copies browser settings headers into process env."""
from __future__ import annotations

import os

import pytest
from fastapi.testclient import TestClient

from app.main import _CUSTOM_INSTRUCTIONS_ENV, app


@pytest.fixture()
def client():
    with TestClient(app) as c:
        yield c


@pytest.fixture(autouse=True)
def clear_custom_instructions_env():
    os.environ.pop(_CUSTOM_INSTRUCTIONS_ENV, None)
    yield
    os.environ.pop(_CUSTOM_INSTRUCTIONS_ENV, None)


def test_empty_custom_instructions_sentinel_clears_env(client):
    os.environ[_CUSTOM_INSTRUCTIONS_ENV] = "always parallelize"

    r = client.get("/api/health", headers={"X-Custom-Instructions": "."})
    assert r.status_code == 200
    assert _CUSTOM_INSTRUCTIONS_ENV not in os.environ


def test_base64_custom_instructions_header_sets_env(client):
    import base64

    encoded = base64.b64encode(b"prefer terse replies").decode("ascii")
    r = client.get("/api/health", headers={"X-Custom-Instructions": encoded})
    assert r.status_code == 200
    assert os.environ[_CUSTOM_INSTRUCTIONS_ENV] == "prefer terse replies"