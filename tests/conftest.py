"""Common test fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def reset_env(monkeypatch):
    """Clear Sonilo env vars before each test so tests must set what they need."""
    for key in (
        "SONILO_API_KEY",
        "SONILO_API_URL",
        "SONILO_MCP_BASE_PATH",
        "TIME_OUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    """Provide a writable tmp directory wired up as SONILO_MCP_BASE_PATH."""
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(tmp_path))
    return tmp_path
