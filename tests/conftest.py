"""Common test fixtures."""
from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def reset_env(monkeypatch, tmp_path):
    """Clear Sonilo env vars before each test so tests must set what they need."""
    for key in (
        "SONILO_API_KEY",
        "SONILO_API_URL",
        "SONILO_MCP_BASE_PATH",
        "TIME_OUT_SECONDS",
    ):
        monkeypatch.delenv(key, raising=False)

    # The credential reader looks under $XDG_CONFIG_HOME / $HOME. Without this,
    # the suite would read the developer's real ~/.config/sonilo/credentials.json,
    # so results would depend on whether they happen to be signed in.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    from sonilo_mcp import credentials

    credentials.clear_cache()


@pytest.fixture
def output_dir(tmp_path, monkeypatch):
    """Provide a writable tmp directory wired up as SONILO_MCP_BASE_PATH."""
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(tmp_path))
    return tmp_path
