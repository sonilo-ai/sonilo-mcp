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


import httpx
import respx


def test_extract_detail_json():
    from sonilo_mcp.api import _extract_detail
    assert _extract_detail('{"detail":"oops"}') == "oops"


def test_extract_detail_plain_text():
    from sonilo_mcp.api import _extract_detail
    assert _extract_detail("some plain body") == "some plain body"


def test_extract_detail_malformed_json():
    from sonilo_mcp.api import _extract_detail
    assert _extract_detail("{not json") == "{not json"


def test_raise_http_error_401():
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception, match="Invalid SONILO_API_KEY"):
        _raise_http_error(401, '{"detail":"Invalid API key"}')


def test_raise_http_error_402_minutes():
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception, match="Top up"):
        _raise_http_error(402, '{"detail":"Insufficient minutes: 30 needed"}')


def test_raise_http_error_402_suspended():
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception) as exc:
        _raise_http_error(402, '{"detail":"Account is suspended"}')
    assert "suspended" in str(exc.value).lower()
    assert "top up" not in str(exc.value).lower()


def test_raise_http_error_413():
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception, match="File too large"):
        _raise_http_error(413, '{"detail":"Max 300MB"}')


def test_raise_http_error_422():
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception, match="Could not read video duration"):
        _raise_http_error(422, '{"detail":"Could not read video duration"}')


def test_raise_http_error_429():
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception, match="Rate limit exceeded"):
        _raise_http_error(429, '{"detail":"Rate limit exceeded"}')


def test_raise_http_error_500():
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception, match="Server error.*retry"):
        _raise_http_error(500, '{"detail":"internal"}')


@respx.mock
async def test_http_get_json_success(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/foo").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    from sonilo_mcp.api import _http_get_json
    out = await _http_get_json("/v1/foo")
    assert out == {"ok": True}


@respx.mock
async def test_http_get_json_missing_key():
    from sonilo_mcp.api import _http_get_json
    with pytest.raises(Exception, match="SONILO_API_KEY"):
        await _http_get_json("/v1/foo")


@respx.mock
async def test_http_get_json_5xx_retries_once(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.get("https://api.test.local/v1/foo").mock(
        side_effect=[
            httpx.Response(503, text="busy"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    from sonilo_mcp.api import _http_get_json
    out = await _http_get_json("/v1/foo")
    assert out == {"ok": True}
    assert route.call_count == 2


@respx.mock
async def test_http_get_json_5xx_retry_then_fail(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/foo").mock(
        return_value=httpx.Response(503, text="busy")
    )
    from sonilo_mcp.api import _http_get_json
    with pytest.raises(Exception, match="Server error"):
        await _http_get_json("/v1/foo")


@respx.mock
async def test_http_get_json_401_no_retry(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "bad")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.get("https://api.test.local/v1/foo").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid API key"})
    )
    from sonilo_mcp.api import _http_get_json
    with pytest.raises(Exception, match="Invalid SONILO_API_KEY"):
        await _http_get_json("/v1/foo")
    assert route.call_count == 1  # no retry on 4xx


@respx.mock
async def test_http_get_json_network_error_retries_once(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.get("https://api.test.local/v1/foo").mock(
        side_effect=[
            httpx.ConnectError("boom"),
            httpx.Response(200, json={"ok": True}),
        ]
    )
    from sonilo_mcp.api import _http_get_json
    out = await _http_get_json("/v1/foo")
    assert out == {"ok": True}
    assert route.call_count == 2


@respx.mock
async def test_http_get_json_forwards_params(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.get("https://api.test.local/v1/foo").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    from sonilo_mcp.api import _http_get_json
    await _http_get_json("/v1/foo", params={"days": 7, "filter": "x"})
    sent = route.calls.last.request.url.params
    assert sent["days"] == "7"
    assert sent["filter"] == "x"
