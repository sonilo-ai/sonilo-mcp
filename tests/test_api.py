"""Tests for sonilo_mcp.api."""
from __future__ import annotations


def test_package_imports():
    from sonilo_mcp import main, mcp
    assert callable(main)
    assert mcp.name == "Sonilo"


import os
from pathlib import Path

import pytest


def test_get_config_defaults(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k1")
    from sonilo_mcp.api import _get_config
    cfg = _get_config()
    assert cfg["api_key"] == "k1"
    assert cfg["api_url"] == "https://api.sonilo.com"
    assert cfg["base_path"] == str(Path.home() / "Desktop")
    assert cfg["timeout"] == 300.0


def test_get_config_overrides(monkeypatch, tmp_path):
    monkeypatch.setenv("SONILO_API_KEY", "k2")
    monkeypatch.setenv("SONILO_API_URL", "http://localhost:8000")
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(tmp_path))
    monkeypatch.setenv("TIME_OUT_SECONDS", "60")
    from sonilo_mcp.api import _get_config
    cfg = _get_config()
    assert cfg["api_url"] == "http://localhost:8000"
    assert cfg["base_path"] == str(tmp_path)
    assert cfg["timeout"] == 60.0


def test_slugify_basic():
    from sonilo_mcp.api import _slugify
    assert _slugify("Happy Song Title") == "happy-song-title"
    assert _slugify("Café — Day 1!") == "caf-day-1"
    assert _slugify("") == "sonilo"
    assert _slugify("   ") == "sonilo"


def test_is_file_writeable_existing(tmp_path):
    from sonilo_mcp.api import _is_file_writeable
    f = tmp_path / "x.txt"
    f.write_text("ok")
    assert _is_file_writeable(f) is True


def test_is_file_writeable_nonexistent_writable_parent(tmp_path):
    from sonilo_mcp.api import _is_file_writeable
    assert _is_file_writeable(tmp_path / "new.txt") is True


def test_make_output_path_default(monkeypatch, tmp_path):
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(tmp_path))
    from sonilo_mcp.api import _make_output_path
    out = _make_output_path(None)
    assert out == tmp_path
    assert out.exists()


def test_make_output_path_absolute(tmp_path):
    from sonilo_mcp.api import _make_output_path
    out = _make_output_path(str(tmp_path / "abs"))
    assert out == tmp_path / "abs"
    assert out.exists()


def test_make_output_path_relative_with_base(monkeypatch, tmp_path):
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(tmp_path))
    from sonilo_mcp.api import _make_output_path
    out = _make_output_path("sub")
    assert out == tmp_path / "sub"
    assert out.exists()


def test_make_output_path_unwriteable(tmp_path, monkeypatch):
    if os.geteuid() == 0:
        pytest.skip("root bypasses permission checks")
    locked = tmp_path / "locked"
    locked.mkdir()
    locked.chmod(0o500)
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(tmp_path))
    from sonilo_mcp.api import _make_output_path
    with pytest.raises(Exception, match="not writeable"):
        _make_output_path(str(locked / "child"))
    locked.chmod(0o700)


def test_resolve_input_file_absolute(tmp_path):
    from sonilo_mcp.api import _resolve_input_file
    f = tmp_path / "song.mp3"
    f.write_bytes(b"x")
    out = _resolve_input_file(str(f), None, {".mp3"}, "audio")
    assert out == f


def test_resolve_input_file_relative_needs_base(tmp_path):
    from sonilo_mcp.api import _resolve_input_file
    with pytest.raises(Exception, match="absolute"):
        _resolve_input_file("song.mp3", None, {".mp3"}, "audio")


def test_resolve_input_file_relative_with_base(monkeypatch, tmp_path):
    from sonilo_mcp.api import _resolve_input_file
    f = tmp_path / "song.mp3"
    f.write_bytes(b"x")
    out = _resolve_input_file("song.mp3", str(tmp_path), {".mp3"}, "audio")
    assert out == f


def test_resolve_input_file_missing(tmp_path):
    from sonilo_mcp.api import _resolve_input_file
    with pytest.raises(Exception, match="does not exist"):
        _resolve_input_file(str(tmp_path / "nope.mp3"), None, {".mp3"}, "audio")


def test_resolve_input_file_wrong_extension(tmp_path):
    from sonilo_mcp.api import _resolve_input_file
    f = tmp_path / "song.txt"
    f.write_text("not audio")
    with pytest.raises(Exception, match="not a recognized audio format"):
        _resolve_input_file(str(f), None, {".mp3"}, "audio")


def test_make_output_path_tilde_expansion(monkeypatch, tmp_path):
    # Simulate ~ expanding to tmp_path by patching HOME
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(tmp_path))
    from sonilo_mcp.api import _make_output_path
    out = _make_output_path("~/music")
    assert out == tmp_path / "music"
    assert out.exists()
