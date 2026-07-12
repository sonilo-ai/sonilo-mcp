"""Tests for sonilo_mcp.api."""
from __future__ import annotations


def test_package_imports():
    from sonilo_mcp import main, mcp
    assert callable(main)
    assert mcp.name == "Sonilo"


import asyncio
import base64
import json
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
    # Aligned with the backend fal read timeout (600s).
    assert cfg["timeout"] == 600.0


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


def test_slugify_caps_length():
    from sonilo_mcp.api import _slugify
    long_input = "thunderous booming explosion with reverb " * 10
    assert len(long_input) > 300
    slug = _slugify(long_input)
    assert len(slug) <= 80
    assert not slug.endswith("-")
    # Short inputs must be completely unaffected by the cap.
    assert _slugify("Happy Song Title") == "happy-song-title"


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


def test_make_output_path_absolute(tmp_path, monkeypatch):
    # Absolute paths under the base directory are allowed.
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(tmp_path))
    from sonilo_mcp.api import _make_output_path
    out = _make_output_path(str(tmp_path / "abs"))
    assert out == tmp_path / "abs"
    assert out.exists()


def test_make_output_path_outside_base_rejected(tmp_path, monkeypatch):
    # By default, writing outside SONILO_MCP_BASE_PATH is blocked.
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(base))
    from sonilo_mcp.api import _make_output_path
    with pytest.raises(Exception, match="outside the allowed base"):
        _make_output_path(str(tmp_path / "elsewhere"))


def test_make_output_path_outside_base_allowed_with_optout(tmp_path, monkeypatch):
    # SONILO_MCP_ALLOW_ANY_PATH restores writing anywhere.
    base = tmp_path / "base"
    base.mkdir()
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(base))
    monkeypatch.setenv("SONILO_MCP_ALLOW_ANY_PATH", "true")
    from sonilo_mcp.api import _make_output_path
    out = _make_output_path(str(tmp_path / "elsewhere"))
    assert out == tmp_path / "elsewhere"
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


def test_make_output_path_rejects_existing_file(tmp_path, monkeypatch):
    # If SONILO_MCP_BASE_PATH resolves to an existing writable FILE (not a
    # directory), _make_output_path must raise a clear error instead of
    # letting Path.mkdir() blow up with a raw FileExistsError.
    f = tmp_path / "base_is_a_file"
    f.write_text("i'm a file, not a directory")
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(f))
    from sonilo_mcp.api import _make_output_path
    with pytest.raises(Exception, match="not a directory") as exc:
        _make_output_path(None)
    assert not isinstance(exc.value, FileExistsError)


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


def test_resolve_input_file_outside_base_rejected(tmp_path):
    # An existing file outside the base directory is blocked by default,
    # preventing exfiltration of arbitrary on-disk files.
    base = tmp_path / "base"
    base.mkdir()
    f = tmp_path / "secret.mp3"
    f.write_bytes(b"x")
    from sonilo_mcp.api import _resolve_input_file
    with pytest.raises(Exception, match="outside the allowed base"):
        _resolve_input_file(str(f), str(base), {".mp3"}, "audio")


def test_resolve_input_file_outside_base_allowed_with_optout(tmp_path, monkeypatch):
    base = tmp_path / "base"
    base.mkdir()
    f = tmp_path / "secret.mp3"
    f.write_bytes(b"x")
    monkeypatch.setenv("SONILO_MCP_ALLOW_ANY_PATH", "1")
    from sonilo_mcp.api import _resolve_input_file
    out = _resolve_input_file(str(f), str(base), {".mp3"}, "audio")
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


def test_resolve_input_file_tilde_expansion(monkeypatch, tmp_path):
    """~/foo.mp3 should be expanded to $HOME/foo.mp3 before isabs check."""
    monkeypatch.setenv("HOME", str(tmp_path))
    f = tmp_path / "song.mp3"
    f.write_bytes(b"x")
    from sonilo_mcp.api import _resolve_input_file
    out = _resolve_input_file("~/song.mp3", None, {".mp3"}, "audio")
    assert out == f


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


@respx.mock
async def test_get_account_services(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={
            "available_services": ["text-to-music", "video-to-music"],
            "rpm_limit": 60,
            "concurrency_limit": 4,
            "discount_factor": 1.0,
            "max_upload_size_mb": 300,
        })
    )
    from sonilo_mcp.api import get_account_services
    out = await get_account_services()
    assert out["available_services"] == ["text-to-music", "video-to-music"]
    assert out["max_upload_size_mb"] == 300


@respx.mock
async def test_get_usage_default_days(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.get("https://api.test.local/v1/account/usage").mock(
        return_value=httpx.Response(200, json={
            "summary": {"total_requests": 0, "total_duration_seconds": 0.0,
                        "total_cost": "0", "period_start": "2026-05-01T00:00:00Z",
                        "period_end": "2026-05-18T00:00:00Z"},
            "daily": [],
        })
    )
    from sonilo_mcp.api import get_usage
    out = await get_usage()
    assert out["summary"]["total_requests"] == 0
    assert route.calls.last.request.url.params["days"] == "30"


@respx.mock
async def test_get_usage_custom_days(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.get("https://api.test.local/v1/account/usage").mock(
        return_value=httpx.Response(200, json={"summary": {}, "daily": []})
    )
    from sonilo_mcp.api import get_usage
    await get_usage(days=7)
    assert route.calls.last.request.url.params["days"] == "7"


async def test_get_usage_rejects_out_of_range():
    from sonilo_mcp.api import get_usage
    with pytest.raises(Exception, match="between 1 and 365"):
        await get_usage(days=0)
    with pytest.raises(Exception, match="between 1 and 365"):
        await get_usage(days=400)
    with pytest.raises(Exception, match="between 1 and 365"):
        await get_usage(days=-5)


@respx.mock
async def test_get_usage_boundary_values_accepted(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/account/usage").mock(
        return_value=httpx.Response(200, json={"summary": {}, "daily": []})
    )
    from sonilo_mcp.api import get_usage
    await get_usage(days=1)
    await get_usage(days=365)


async def _async_iter(items):
    for item in items:
        yield item


async def test_consume_ndjson_single_stream():
    audio = b"hello-audio"
    lines = [
        json.dumps({"type": "title", "title": "My Song"}),
        json.dumps({
            "type": "audio_chunk",
            "stream_index": 0,
            "num_streams": 1,
            "data": base64.b64encode(audio).decode(),
        }),
        json.dumps({"type": "complete"}),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    streams, num_streams, title = await _consume_ndjson_lines(_async_iter(lines))
    assert title == "My Song"
    assert num_streams == 1
    assert bytes(streams[0]) == audio


async def test_consume_ndjson_multiple_chunks_concatenate():
    audio_a = b"first-half"
    audio_b = b"-second-half"
    lines = [
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": base64.b64encode(audio_a).decode(),
        }),
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": base64.b64encode(audio_b).decode(),
        }),
        json.dumps({"type": "complete"}),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    streams, _, _ = await _consume_ndjson_lines(_async_iter(lines))
    assert bytes(streams[0]) == audio_a + audio_b


async def test_consume_ndjson_multi_stream():
    a = b"track-a"
    b = b"track-b"
    lines = [
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 2,
            "data": base64.b64encode(a).decode(),
        }),
        json.dumps({
            "type": "audio_chunk", "stream_index": 1, "num_streams": 2,
            "data": base64.b64encode(b).decode(),
        }),
        json.dumps({"type": "complete"}),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    streams, num_streams, _ = await _consume_ndjson_lines(_async_iter(lines))
    assert num_streams == 2
    assert bytes(streams[0]) == a
    assert bytes(streams[1]) == b


async def test_consume_ndjson_error_event_raises():
    lines = [
        json.dumps({"type": "stage_start", "stage": "init"}),
        json.dumps({"type": "error", "code": "UPSTREAM", "message": "Modal died"}),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    with pytest.raises(Exception, match="Modal died"):
        await _consume_ndjson_lines(_async_iter(lines))


async def test_consume_ndjson_no_complete_raises():
    lines = [
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": base64.b64encode(b"x").decode(),
        }),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    with pytest.raises(Exception, match="without `complete`"):
        await _consume_ndjson_lines(_async_iter(lines))


async def test_consume_ndjson_ignores_unknown_event_types():
    lines = [
        json.dumps({"type": "stage_start", "stage": "init"}),
        json.dumps({"type": "trace", "msg": "anything"}),
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": base64.b64encode(b"x").decode(),
        }),
        json.dumps({"type": "complete"}),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    streams, _, _ = await _consume_ndjson_lines(_async_iter(lines))
    assert bytes(streams[0]) == b"x"


async def test_consume_ndjson_ignores_malformed_lines():
    lines = [
        "not-json",
        "",
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": base64.b64encode(b"x").decode(),
        }),
        json.dumps({"type": "complete"}),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    streams, _, _ = await _consume_ndjson_lines(_async_iter(lines))
    assert bytes(streams[0]) == b"x"


async def test_consume_ndjson_empty_title_keeps_none():
    lines = [
        json.dumps({"type": "title", "title": ""}),
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": base64.b64encode(b"x").decode(),
        }),
        json.dumps({"type": "complete"}),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    _, _, title = await _consume_ndjson_lines(_async_iter(lines))
    assert title is None


async def test_consume_ndjson_error_event_no_message_or_code():
    lines = [
        json.dumps({"type": "error"}),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    with pytest.raises(Exception, match="stream error"):
        await _consume_ndjson_lines(_async_iter(lines))


async def test_consume_ndjson_skips_bad_stream_index():
    lines = [
        json.dumps({
            "type": "audio_chunk", "stream_index": "abc", "num_streams": 1,
            "data": base64.b64encode(b"x").decode(),
        }),
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": base64.b64encode(b"y").decode(),
        }),
        json.dumps({"type": "complete"}),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    streams, _, _ = await _consume_ndjson_lines(_async_iter(lines))
    assert bytes(streams[0]) == b"y"


async def test_consume_ndjson_skips_null_num_streams():
    lines = [
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": None,
            "data": base64.b64encode(b"x").decode(),
        }),
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": base64.b64encode(b"y").decode(),
        }),
        json.dumps({"type": "complete"}),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    streams, _, _ = await _consume_ndjson_lines(_async_iter(lines))
    assert bytes(streams[0]) == b"y"


async def test_consume_ndjson_skips_non_string_data():
    lines = [
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": None,
        }),
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": base64.b64encode(b"x").decode(),
        }),
        json.dumps({"type": "complete"}),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    streams, _, _ = await _consume_ndjson_lines(_async_iter(lines))
    assert bytes(streams[0]) == b"x"


async def test_consume_ndjson_skips_malformed_base64():
    lines = [
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": "!!! not valid base64 !!!",
        }),
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": base64.b64encode(b"x").decode(),
        }),
        json.dumps({"type": "complete"}),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    streams, _, _ = await _consume_ndjson_lines(_async_iter(lines))
    assert bytes(streams[0]) == b"x"


async def test_consume_ndjson_skips_negative_stream_index():
    lines = [
        json.dumps({
            "type": "audio_chunk", "stream_index": -1, "num_streams": 1,
            "data": base64.b64encode(b"x").decode(),
        }),
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": base64.b64encode(b"y").decode(),
        }),
        json.dumps({"type": "complete"}),
    ]
    from sonilo_mcp.api import _consume_ndjson_lines
    streams, _, _ = await _consume_ndjson_lines(_async_iter(lines))
    assert -1 not in streams
    assert bytes(streams[0]) == b"y"


import time as _time


def _ndjson_bytes(events: list[dict]) -> bytes:
    return b"".join(json.dumps(e).encode() + b"\n" for e in events)


@respx.mock
async def test_text_to_music_writes_file(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    audio = b"\x00\x01\x02fake-mp3-bytes"
    ndjson = _ndjson_bytes([
        {"type": "title", "title": "Happy Tune"},
        {"type": "audio_chunk", "stream_index": 0, "num_streams": 1,
         "data": base64.b64encode(audio).decode()},
        {"type": "complete"},
    ])
    route = respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(200, content=ndjson)
    )
    from sonilo_mcp.api import text_to_music
    result = await text_to_music(prompt="happy", duration=10)

    # The backend expects form fields, not JSON — guard against regressing.
    sent = route.calls.last.request
    assert sent.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )
    assert b"prompt=happy" in sent.content
    assert b"duration=10" in sent.content

    assert len(result) == 1
    expected = output_dir / "happy-tune.m4a"
    assert expected.exists()
    assert expected.read_bytes() == audio
    assert "happy-tune.m4a" in result[0].text


@respx.mock
async def test_text_to_music_multi_stream(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    a = b"track-a"
    b_bytes = b"track-b"
    ndjson = _ndjson_bytes([
        {"type": "title", "title": "Twin"},
        {"type": "audio_chunk", "stream_index": 0, "num_streams": 2,
         "data": base64.b64encode(a).decode()},
        {"type": "audio_chunk", "stream_index": 1, "num_streams": 2,
         "data": base64.b64encode(b_bytes).decode()},
        {"type": "complete"},
    ])
    respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(200, content=ndjson)
    )
    from sonilo_mcp.api import text_to_music
    result = await text_to_music(prompt="happy", duration=10)
    assert len(result) == 2
    assert (output_dir / "twin-0.m4a").read_bytes() == a
    assert (output_dir / "twin-1.m4a").read_bytes() == b_bytes


@respx.mock
async def test_text_to_music_no_title_fallback(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    audio = b"x"
    ndjson = _ndjson_bytes([
        {"type": "audio_chunk", "stream_index": 0, "num_streams": 1,
         "data": base64.b64encode(audio).decode()},
        {"type": "complete"},
    ])
    respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(200, content=ndjson)
    )
    from sonilo_mcp.api import text_to_music
    result = await text_to_music(prompt="x", duration=5)
    assert len(result) == 1
    # Fallback name pattern: sonilo-<unix-timestamp>.m4a
    name = Path(result[0].text.split("File saved as: ")[1]).name
    assert name.startswith("sonilo-")
    assert name.endswith(".m4a")


@respx.mock
async def test_text_to_music_error_event_no_file(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    ndjson = _ndjson_bytes([
        {"type": "error", "code": "MODAL_DEAD", "message": "upstream failure"},
    ])
    respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(200, content=ndjson)
    )
    from sonilo_mcp.api import text_to_music
    with pytest.raises(Exception, match="upstream failure"):
        await text_to_music(prompt="x", duration=5)
    assert list(output_dir.iterdir()) == []


@respx.mock
async def test_text_to_music_401_error(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "bad")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(401, json={"detail": "Invalid API key"})
    )
    from sonilo_mcp.api import text_to_music
    with pytest.raises(Exception, match="Invalid SONILO_API_KEY"):
        await text_to_music(prompt="x", duration=5)


@respx.mock
async def test_text_to_music_429_error(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(429, json={"detail": "Rate limit exceeded"})
    )
    from sonilo_mcp.api import text_to_music
    with pytest.raises(Exception, match="Rate limit exceeded"):
        await text_to_music(prompt="x", duration=5)


async def test_text_to_music_missing_api_key(output_dir):
    from sonilo_mcp.api import text_to_music
    with pytest.raises(Exception, match="SONILO_API_KEY"):
        await text_to_music(prompt="x", duration=5)


@respx.mock
async def test_text_to_music_sends_correct_body(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    audio = b"x"
    ndjson = _ndjson_bytes([
        {"type": "audio_chunk", "stream_index": 0, "num_streams": 1,
         "data": base64.b64encode(audio).decode()},
        {"type": "complete"},
    ])
    route = respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(200, content=ndjson)
    )
    from sonilo_mcp.api import text_to_music
    from urllib.parse import parse_qs
    await text_to_music(prompt="energetic rock", duration=42)
    req = route.calls.last.request
    # Backend expects form fields, not JSON.
    assert req.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )
    body = parse_qs(req.content.decode())
    assert body == {"prompt": ["energetic rock"], "duration": ["42"]}
    auth = req.headers["authorization"]
    assert auth == "Bearer k"


@respx.mock
async def test_video_to_music_url_mode(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    audio = b"mp3"
    ndjson = _ndjson_bytes([
        {"type": "title", "title": "From URL"},
        {"type": "audio_chunk", "stream_index": 0, "num_streams": 1,
         "data": base64.b64encode(audio).decode()},
        {"type": "complete"},
    ])
    route = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(200, content=ndjson)
    )
    from sonilo_mcp.api import video_to_music
    from urllib.parse import parse_qs
    result = await video_to_music(video_url="https://cdn.example.com/v.mp4")
    assert len(result) == 1
    body = route.calls.last.request.content.decode()
    parsed = parse_qs(body)
    assert parsed["video_url"] == ["https://cdn.example.com/v.mp4"]


@respx.mock
async def test_video_to_music_url_with_prompt(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    ndjson = _ndjson_bytes([
        {"type": "audio_chunk", "stream_index": 0, "num_streams": 1,
         "data": base64.b64encode(b"x").decode()},
        {"type": "complete"},
    ])
    route = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(200, content=ndjson)
    )
    from sonilo_mcp.api import video_to_music
    await video_to_music(video_url="https://x.com/v.mp4", prompt="upbeat")
    body = route.calls.last.request.content.decode()
    assert "upbeat" in body


async def test_video_to_music_both_inputs_rejected(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp.api import video_to_music
    with pytest.raises(Exception, match="Provide either"):
        await video_to_music(video_url="https://x", video_path="/tmp/x.mp4")


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "ftp://example.com/v.mp4",
        "-i",  # would inject an ffprobe flag; urlparse yields empty scheme
        "/etc/passwd",
        "169.254.169.254/latest/meta-data/",
    ],
)
async def test_video_to_music_rejects_non_http_url(monkeypatch, output_dir, bad_url):
    """video_url must be http(s) — guards against local file probing, SSRF,
    and ffprobe argument injection before the value reaches ffprobe/backend."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp.api import video_to_music
    with pytest.raises(Exception, match="http"):
        await video_to_music(video_url=bad_url)


async def test_video_to_music_no_input_rejected(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp.api import video_to_music
    with pytest.raises(Exception, match="Provide either"):
        await video_to_music()


@respx.mock
async def test_video_to_music_path_mode(monkeypatch, output_dir, tmp_path):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"FAKE-MP4-BYTES")

    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={
            "available_services": [], "rpm_limit": 60,
            "concurrency_limit": 1, "discount_factor": 1.0,
            "max_upload_size_mb": 300,
        })
    )
    ndjson = _ndjson_bytes([
        {"type": "audio_chunk", "stream_index": 0, "num_streams": 1,
         "data": base64.b64encode(b"x").decode()},
        {"type": "complete"},
    ])
    route = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(200, content=ndjson)
    )
    # Reset the services cache so this test gets a fresh lookup
    from sonilo_mcp.api import _reset_services_cache
    _reset_services_cache()
    from sonilo_mcp.api import video_to_music
    await video_to_music(video_path=str(video))
    # multipart upload should include the file content
    assert b"FAKE-MP4-BYTES" in route.calls.last.request.content


@respx.mock
async def test_video_to_music_path_too_large(monkeypatch, output_dir, tmp_path):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    video = tmp_path / "big.mp4"
    # 2 MB file
    video.write_bytes(b"\x00" * (2 * 1024 * 1024))

    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={
            "available_services": [], "rpm_limit": 60,
            "concurrency_limit": 1, "discount_factor": 1.0,
            "max_upload_size_mb": 1,  # cap = 1 MB
        })
    )
    upload_route = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(200, content=b"")
    )
    from sonilo_mcp.api import _reset_services_cache
    _reset_services_cache()
    from sonilo_mcp.api import video_to_music
    with pytest.raises(Exception, match="too large"):
        await video_to_music(video_path=str(video))
    # Must NOT upload
    assert upload_route.call_count == 0


async def test_video_to_music_path_does_not_exist(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp.api import video_to_music
    with pytest.raises(Exception, match="does not exist"):
        await video_to_music(video_path="/tmp/__definitely_not_real_video__.mp4")


class _FakeProc:
    """Stand-in for an asyncio subprocess returned by create_subprocess_exec."""

    def __init__(self, stdout: bytes, returncode: int = 0):
        self._stdout = stdout
        self.returncode = returncode

    async def communicate(self):
        return self._stdout, b""


def _patch_ffprobe(monkeypatch, *, duration=None, returncode=0, installed=True):
    """Wire up a fake ffprobe that reports `duration` seconds."""
    from sonilo_mcp import api

    monkeypatch.setattr(
        api.shutil, "which",
        lambda name: "/usr/bin/ffprobe" if installed else None,
    )
    payload = (
        json.dumps({"format": {"duration": str(duration)}}).encode()
        if duration is not None else b""
    )

    async def fake_exec(*args, **kwargs):
        return _FakeProc(payload, returncode=returncode)

    monkeypatch.setattr(api.asyncio, "create_subprocess_exec", fake_exec)


async def test_check_video_duration_rejects_too_long(monkeypatch):
    from sonilo_mcp.api import _check_video_duration
    _patch_ffprobe(monkeypatch, duration=400.0)
    with pytest.raises(Exception, match="exceeds the maximum"):
        await _check_video_duration("/tmp/clip.mp4")


async def test_check_video_duration_allows_within_limit(monkeypatch):
    from sonilo_mcp.api import _check_video_duration
    _patch_ffprobe(monkeypatch, duration=120.0)
    await _check_video_duration("/tmp/clip.mp4")  # must not raise


async def test_check_video_duration_skips_without_ffprobe(monkeypatch):
    from sonilo_mcp.api import _check_video_duration
    _patch_ffprobe(monkeypatch, duration=400.0, installed=False)
    # ffprobe missing -> fail open, no raise even though it would be too long.
    await _check_video_duration("/tmp/clip.mp4")


async def test_check_video_duration_fails_open_on_probe_error(monkeypatch):
    from sonilo_mcp.api import _check_video_duration
    _patch_ffprobe(monkeypatch, returncode=1)  # ffprobe couldn't read it
    await _check_video_duration("/tmp/clip.mp4")  # must not raise


async def test_check_video_duration_sfx_cap_rejects_200s(monkeypatch):
    from sonilo_mcp.api import _check_video_duration, _SFX_MAX_VIDEO_DURATION_SECONDS
    _patch_ffprobe(monkeypatch, duration=200.0)
    with pytest.raises(Exception, match="exceeds the maximum"):
        await _check_video_duration(
            "/tmp/clip.mp4", max_seconds=_SFX_MAX_VIDEO_DURATION_SECONDS
        )


async def test_check_video_duration_music_cap_allows_200s(monkeypatch):
    from sonilo_mcp.api import _check_video_duration
    _patch_ffprobe(monkeypatch, duration=200.0)
    # Default cap stays 360s — 200s must not raise.
    await _check_video_duration("/tmp/clip.mp4")


@respx.mock
async def test_video_to_music_path_too_long(monkeypatch, output_dir, tmp_path):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    video = tmp_path / "long.mp4"
    video.write_bytes(b"FAKE-MP4-BYTES")

    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={
            "available_services": [], "rpm_limit": 60,
            "concurrency_limit": 1, "discount_factor": 1.0,
            "max_upload_size_mb": 300,
        })
    )
    upload_route = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(200, content=b"")
    )
    _patch_ffprobe(monkeypatch, duration=400.0)
    from sonilo_mcp.api import _reset_services_cache, video_to_music
    _reset_services_cache()
    with pytest.raises(Exception, match="exceeds the maximum"):
        await video_to_music(video_path=str(video))
    # Must NOT upload an over-length video.
    assert upload_route.call_count == 0


async def test_play_audio_rejects_nonexistent(monkeypatch):
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", "/tmp")
    from sonilo_mcp.api import play_audio
    with pytest.raises(Exception, match="does not exist"):
        play_audio("/tmp/__not_a_real_audio__.mp3")


async def test_play_audio_rejects_wrong_extension(monkeypatch, tmp_path):
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(tmp_path))
    text = tmp_path / "note.txt"
    text.write_text("hi")
    from sonilo_mcp.api import play_audio
    with pytest.raises(Exception, match="not a recognized audio format"):
        play_audio(str(text))


async def test_play_audio_uses_afplay_on_macos(monkeypatch, tmp_path):
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(tmp_path))
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"FAKE")

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(
        "shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "afplay" else None,
    )

    called = {}
    import subprocess as _sp

    def fake_run(cmd, **kwargs):
        called["cmd"] = cmd
        called["check"] = kwargs.get("check")
        return _sp.CompletedProcess(cmd, 0, b"", b"")

    monkeypatch.setattr("subprocess.run", fake_run)

    from sonilo_mcp.api import play_audio
    out = play_audio(str(audio))
    assert called["cmd"][0] == "afplay"
    assert called["cmd"][1] == str(audio)
    assert called["check"] is True
    assert "Successfully played audio file" in out.text


async def test_play_audio_falls_back_to_sounddevice(monkeypatch, tmp_path):
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(tmp_path))
    audio = tmp_path / "song.wav"
    audio.write_bytes(b"FAKE")

    # Pretend we're on an OS with no recognised system player.
    monkeypatch.setattr("sys.platform", "freebsd")
    monkeypatch.setattr("shutil.which", lambda name: None)

    called = {}

    def fake_play(data, samplerate):
        called["sr"] = samplerate

    def fake_wait():
        called["waited"] = True

    def fake_read(buf):
        return ([0.0, 0.1, 0.2], 44100)

    import sys
    fake_sd = type(sys)("sounddevice")
    fake_sd.play = fake_play
    fake_sd.wait = fake_wait
    fake_sf = type(sys)("soundfile")
    fake_sf.read = fake_read
    monkeypatch.setitem(sys.modules, "sounddevice", fake_sd)
    monkeypatch.setitem(sys.modules, "soundfile", fake_sf)

    from sonilo_mcp.api import play_audio
    out = play_audio(str(audio))
    assert called["sr"] == 44100
    assert called.get("waited") is True
    assert "Successfully played audio file" in out.text


async def test_play_audio_propagates_afplay_failure(monkeypatch, tmp_path):
    monkeypatch.setenv("SONILO_MCP_BASE_PATH", str(tmp_path))
    audio = tmp_path / "song.mp3"
    audio.write_bytes(b"FAKE")

    monkeypatch.setattr("sys.platform", "darwin")
    monkeypatch.setattr(
        "shutil.which",
        lambda name: f"/usr/bin/{name}" if name == "afplay" else None,
    )

    import subprocess as _sp

    def fake_run(cmd, **kwargs):
        raise _sp.CalledProcessError(1, cmd, output=b"", stderr=b"bad file")

    monkeypatch.setattr("subprocess.run", fake_run)

    from sonilo_mcp.api import play_audio
    with pytest.raises(Exception, match="afplay failed.*bad file"):
        play_audio(str(audio))


@respx.mock
async def test_get_max_upload_size_mb_falls_back_on_failure(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(500, text="boom")
    )
    from sonilo_mcp.api import _get_max_upload_size_mb, _reset_services_cache
    _reset_services_cache()
    out = await _get_max_upload_size_mb()
    assert out == 300


@respx.mock
async def test_http_get_json_sends_client_headers(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k1")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.get("https://api.test.local/v1/foo").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    from sonilo_mcp.api import _http_get_json
    await _http_get_json("/v1/foo")
    req = route.calls[0].request
    assert req.headers["X-Sonilo-Client"] == "mcp"
    assert req.headers["X-Sonilo-Client-Version"]  # non-empty
    assert req.headers["User-Agent"].startswith("sonilo-mcp/")


@respx.mock
async def test_post_streaming_sends_client_headers(monkeypatch, output_dir):
    import base64
    import json as _json
    monkeypatch.setenv("SONILO_API_KEY", "k1")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    ndjson = (
        _json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": base64.b64encode(b"x").decode(),
        }) + "\n"
        + _json.dumps({"type": "complete"}) + "\n"
    ).encode()
    route = respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(200, content=ndjson)
    )
    from sonilo_mcp.api import _post_streaming_generation
    await _post_streaming_generation(
        "/v1/text-to-music", output_dir, data={"prompt": "p", "duration": 5}
    )
    req = route.calls[0].request
    assert req.headers["X-Sonilo-Client"] == "mcp"
    assert req.headers["X-Sonilo-Client-Version"]
    assert req.headers["User-Agent"].startswith("sonilo-mcp/")


@respx.mock
async def test_http_get_json_sends_host_headers_from_clientinfo(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k1")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    import sonilo_mcp.api as api

    class _Info:
        name = "claude-ai"
        version = "1.2.3"

    class _Params:
        clientInfo = _Info()

    class _Session:
        client_params = _Params()

    class _Ctx:
        session = _Session()

    monkeypatch.setattr(api.mcp, "get_context", lambda: _Ctx())
    route = respx.get("https://api.test.local/v1/foo").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    await api._http_get_json("/v1/foo")
    req = route.calls[0].request
    assert req.headers["X-Sonilo-Client-Host"] == "claude-ai"
    assert req.headers["X-Sonilo-Client-Host-Version"] == "1.2.3"


@respx.mock
async def test_http_get_json_omits_host_headers_when_no_context(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k1")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    import sonilo_mcp.api as api

    def _boom():
        raise RuntimeError("no active request context")

    monkeypatch.setattr(api.mcp, "get_context", _boom)
    route = respx.get("https://api.test.local/v1/foo").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    await api._http_get_json("/v1/foo")
    req = route.calls[0].request
    assert "X-Sonilo-Client-Host" not in req.headers
    # The base client marker is still present — only host attribution is absent.
    assert req.headers["X-Sonilo-Client"] == "mcp"


# ---------- SFX task pipeline ----------


def test_end_sentence_trailing_whitespace():
    from sonilo_mcp.api import _end_sentence
    # Trailing whitespace must be stripped before inspecting the last char,
    # otherwise appended prose produces a dangling space or a doubled period.
    assert _end_sentence("Failed with trailing space ") == "Failed with trailing space."
    assert _end_sentence("Failed. ") == "Failed."
    # Existing behavior for non-whitespace-trailing input stays unaffected.
    assert _end_sentence("Failed") == "Failed."
    assert _end_sentence("Failed.") == "Failed."
    assert _end_sentence("Failed!") == "Failed!"
    assert _end_sentence("Failed?") == "Failed?"


@respx.mock
async def test_post_task_submit_returns_task_id(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.post("https://api.test.local/v1/text-to-sfx").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-123", "status": "processing"}
        )
    )
    from sonilo_mcp.api import _post_task_submit
    task_id = await _post_task_submit(
        "/v1/text-to-sfx", data={"prompt": "boom", "duration": 5}
    )
    assert task_id == "t-123"
    sent = route.calls.last.request
    assert sent.headers["authorization"] == "Bearer k"
    assert b"prompt=boom" in sent.content


@respx.mock
async def test_post_task_submit_logs_task_id(monkeypatch, capsys):
    # After a submit succeeds, the user is charged and the task_id is the
    # only way to recover the result. If the tool call is cancelled before
    # anything else records it (see cancellation-during-poll concern), a
    # stderr line is the last line of defense — so it must be emitted right
    # after the task_id is known, unconditionally.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.post("https://api.test.local/v1/text-to-sfx").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-log-1", "status": "processing"}
        )
    )
    from sonilo_mcp.api import _post_task_submit
    task_id = await _post_task_submit("/v1/text-to-sfx", data={"prompt": "x"})
    assert task_id == "t-log-1"
    captured = capsys.readouterr()
    assert "t-log-1" in captured.err
    assert "get_sfx_task" in captured.err


@respx.mock
async def test_post_task_submit_missing_api_key():
    from sonilo_mcp.api import _post_task_submit
    with pytest.raises(Exception, match="SONILO_API_KEY"):
        await _post_task_submit("/v1/text-to-sfx", data={"prompt": "x"})


@respx.mock
async def test_post_task_submit_maps_errors(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.post("https://api.test.local/v1/text-to-sfx").mock(
        return_value=httpx.Response(422, json={"detail": "duration too long"})
    )
    from sonilo_mcp.api import _post_task_submit
    with pytest.raises(Exception, match="duration too long"):
        await _post_task_submit("/v1/text-to-sfx", data={"prompt": "x"})


@respx.mock
async def test_post_task_submit_no_retry_on_5xx(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.post("https://api.test.local/v1/text-to-sfx").mock(
        return_value=httpx.Response(503, text="busy")
    )
    from sonilo_mcp.api import _post_task_submit
    with pytest.raises(Exception, match="Server error"):
        await _post_task_submit("/v1/text-to-sfx", data={"prompt": "x"})
    assert route.call_count == 1  # generation submits must never retry


@respx.mock
async def test_post_task_submit_missing_task_id(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.post("https://api.test.local/v1/text-to-sfx").mock(
        return_value=httpx.Response(202, json={"status": "processing"})
    )
    from sonilo_mcp.api import _post_task_submit
    with pytest.raises(Exception, match="task_id"):
        await _post_task_submit("/v1/text-to-sfx", data={"prompt": "x"})


@respx.mock
async def test_poll_task_returns_on_succeeded(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.get("https://api.test.local/v1/tasks/t-1").mock(
        side_effect=[
            httpx.Response(200, json={"task_id": "t-1", "status": "processing"}),
            httpx.Response(200, json={"task_id": "t-1", "status": "processing"}),
            httpx.Response(200, json={
                "task_id": "t-1", "status": "succeeded",
                "audio": {"url": "https://r2.test/audio", "content_type": "audio/mp4"},
            }),
        ]
    )
    from sonilo_mcp import api
    sleeps: list[float] = []

    async def fake_sleep(s):
        sleeps.append(s)

    monkeypatch.setattr(api, "_poll_sleep", fake_sleep)
    body = await api._poll_task("t-1", timeout_seconds=600)
    assert body["status"] == "succeeded"
    assert route.call_count == 3
    assert sleeps == [5.0, 5.0]


@respx.mock
async def test_poll_task_returns_failed_body(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-2").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-2", "status": "failed",
            "error": {"code": "UPSTREAM_MALFORMED", "message": "bad output"},
            "refunded": True,
        })
    )
    from sonilo_mcp.api import _poll_task
    body = await _poll_task("t-2", timeout_seconds=600)
    assert body["status"] == "failed"
    assert body["refunded"] is True


@respx.mock
async def test_poll_task_timeout_mentions_recovery(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-3").mock(
        return_value=httpx.Response(200, json={"task_id": "t-3", "status": "processing"})
    )
    from sonilo_mcp.api import _poll_task
    # timeout_seconds=0 -> deadline already passed after the first check.
    with pytest.raises(Exception) as exc:
        await _poll_task("t-3", timeout_seconds=0)
    assert "t-3" in str(exc.value)
    assert "get_sfx_task" in str(exc.value)


@respx.mock
async def test_poll_task_http_error_mentions_task_id(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-err").mock(
        return_value=httpx.Response(500, text="boom")
    )
    from sonilo_mcp.api import _poll_task
    with pytest.raises(Exception) as exc:
        await _poll_task("t-err", timeout_seconds=600)
    msg = str(exc.value)
    assert "t-err" in msg
    assert "get_sfx_task" in msg


@respx.mock
async def test_poll_task_402_keeps_task_id(monkeypatch):
    # 402 mid-poll means the account was suspended for billing AFTER the
    # task was already submitted and charged (see _raise_http_error's
    # "Account is suspended" 402 case) — the task_id must survive so the
    # paid result stays recoverable once billing is fixed.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-402").mock(
        side_effect=[
            httpx.Response(200, json={"task_id": "t-402", "status": "processing"}),
            httpx.Response(402, json={"detail": "Account is suspended"}),
        ]
    )
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    with pytest.raises(Exception) as exc:
        await api._poll_task("t-402", timeout_seconds=600)
    msg = str(exc.value)
    assert "t-402" in msg
    assert "get_sfx_task" in msg


@respx.mock
async def test_poll_task_401_keeps_task_id(monkeypatch):
    # 401 mid-poll means the API key was rotated/revoked between submit and
    # a later poll — same recoverability requirement as 402.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-401").mock(
        side_effect=[
            httpx.Response(200, json={"task_id": "t-401", "status": "processing"}),
            httpx.Response(401, json={"detail": "Invalid API key"}),
        ]
    )
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    with pytest.raises(Exception) as exc:
        await api._poll_task("t-401", timeout_seconds=600)
    msg = str(exc.value)
    assert "t-401" in msg
    assert "get_sfx_task" in msg


@respx.mock
async def test_download_artifact_writes_file_without_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("SONILO_API_KEY", "secret-key")
    route = respx.get("https://r2.test/sfx-results/t-1/audio.m4a").mock(
        return_value=httpx.Response(200, content=b"fake-audio-bytes")
    )
    from sonilo_mcp.api import _download_artifact
    dest = tmp_path / "boom.m4a"
    await _download_artifact("https://r2.test/sfx-results/t-1/audio.m4a", dest)
    assert dest.read_bytes() == b"fake-audio-bytes"
    # Presigned URLs carry their own auth; the API key and client markers
    # must never be sent to the storage domain.
    sent = route.calls.last.request
    assert "authorization" not in sent.headers
    assert "x-sonilo-client" not in sent.headers


@respx.mock
async def test_download_artifact_error_states_status_code(monkeypatch, tmp_path):
    # _download_artifact doesn't know the caller's task_id, so it states only
    # the bare fact of the failure — no recovery advice. That's owned by
    # _save_task_artifacts, which does have the task_id (see
    # test_save_task_artifacts_video_failure_reports_saved_audio etc.).
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/gone").mock(
        return_value=httpx.Response(403, text="expired")
    )
    from sonilo_mcp.api import _download_artifact
    with pytest.raises(Exception, match=r"download failed \(status 403\)"):
        await _download_artifact("https://r2.test/gone", tmp_path / "x.m4a")


@respx.mock
async def test_download_artifact_cleans_up_partial_file_on_midstream_failure(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SONILO_API_KEY", "k")

    # respx can't take a raising generator as `content` (it drains it while
    # building the response), so use an httpx.AsyncByteStream that yields one
    # chunk and then dies — the failure happens mid `aiter_bytes()`, after
    # bytes have already been written to dest.
    class _BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"first-chunk"
            raise httpx.ReadError("connection lost")

    respx.get("https://r2.test/broken").mock(
        return_value=httpx.Response(200, stream=_BrokenStream())
    )
    from sonilo_mcp.api import _download_artifact
    dest = tmp_path / "x.m4a"
    with pytest.raises(Exception, match="download failed"):
        await _download_artifact("https://r2.test/broken", dest)
    # A mid-stream failure must not leave a corrupt partial file behind —
    # otherwise a retry would get a new suffixed path from _artifact_dest
    # and the truncated file would be permanently orphaned.
    assert not dest.exists()


def test_ext_from_content_type():
    from sonilo_mcp.api import _ext_from_content_type
    assert _ext_from_content_type("audio/wav") == ".wav"
    assert _ext_from_content_type("audio/mpeg") == ".mp3"
    assert _ext_from_content_type("audio/mp4") == ".m4a"
    assert _ext_from_content_type("audio/flac") == ".flac"
    assert _ext_from_content_type(None) == ".m4a"
    assert _ext_from_content_type("application/octet-stream") == ".m4a"


def test_artifact_dest_avoids_collisions(tmp_path):
    from sonilo_mcp.api import _artifact_dest
    first = _artifact_dest(tmp_path, "boom", ".m4a")
    assert first == tmp_path / "boom.m4a"
    first.write_bytes(b"x")
    second = _artifact_dest(tmp_path, "boom", ".m4a")
    assert second == tmp_path / "boom-1.m4a"
    second.write_bytes(b"y")
    assert _artifact_dest(tmp_path, "boom", ".m4a") == tmp_path / "boom-2.m4a"


def test_artifact_dest_reserves_path_atomically(tmp_path):
    # _artifact_dest must RESERVE the path it picks (by creating the file)
    # rather than merely checking exists() — otherwise two concurrent
    # callers racing between "pick a path" and "actually write the file"
    # (an await boundary sits in between in _download_artifact) both pick
    # the same free name and one silently clobbers the other's paid result.
    #
    # With no file written in between, two sequential calls must still
    # return DIFFERENT paths — proving the first call already claimed
    # "boom.m4a" for itself.
    from sonilo_mcp.api import _artifact_dest
    first = _artifact_dest(tmp_path, "boom", ".m4a")
    second = _artifact_dest(tmp_path, "boom", ".m4a")
    assert first == tmp_path / "boom.m4a"
    assert second == tmp_path / "boom-1.m4a"
    assert first != second
    # The reserved paths must exist on disk (empty) immediately, since the
    # reservation itself is what prevents the race.
    assert first.exists()
    assert second.exists()


@respx.mock
async def test_save_task_artifacts_concurrent_calls_get_distinct_files(
    monkeypatch, tmp_path
):
    # Regression test for the TOCTOU race: two concurrent _save_task_artifacts
    # calls with the SAME base_name (e.g. an MCP host retrying a slow
    # text_to_sfx call while the first is still in flight, or simply two
    # calls sharing a prompt) must not clobber each other. The first
    # download is held open past the point where the second call resolves
    # its destination path, mimicking the await boundary between path
    # selection and file write.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp.api import _save_task_artifacts

    start_event = asyncio.Event()
    release_event = asyncio.Event()
    call_count = 0

    async def slow_side_effect(request):
        nonlocal call_count
        call_count += 1
        n = call_count
        if n == 1:
            start_event.set()
            await release_event.wait()
        return httpx.Response(200, content=f"AUDIO-DATA-{n}".encode())

    respx.get("https://r2.test/race.wav").mock(side_effect=slow_side_effect)

    body = {
        "status": "succeeded",
        "audio": {"url": "https://r2.test/race.wav", "content_type": "audio/wav"},
    }

    async def call1():
        return await _save_task_artifacts(body, tmp_path, "thunder", "task-1")

    async def call2():
        await start_event.wait()
        result = await _save_task_artifacts(body, tmp_path, "thunder", "task-2")
        release_event.set()
        return result

    r1, r2 = await asyncio.gather(call1(), call2())
    assert len(r1) == 1 and len(r2) == 1

    files = sorted(tmp_path.iterdir())
    assert [p.name for p in files] == ["thunder-1.wav", "thunder.wav"]
    payloads = {p.read_bytes() for p in files}
    # Both payloads must survive intact — neither call's paid result was
    # silently destroyed by the other.
    assert payloads == {b"AUDIO-DATA-1", b"AUDIO-DATA-2"}


@respx.mock
async def test_save_task_artifacts_audio_only(monkeypatch, tmp_path):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"audio-bytes")
    )
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "t-1", "status": "succeeded",
        "audio": {"url": "https://r2.test/a.m4a", "content_type": "audio/mp4"},
    }
    result = await _save_task_artifacts(body, tmp_path, "boom", "t-1")
    assert len(result) == 1
    expected = tmp_path / "boom.m4a"
    assert expected.read_bytes() == b"audio-bytes"
    assert str(expected) in result[0].text


@respx.mock
async def test_save_task_artifacts_audio_and_video(monkeypatch, tmp_path):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/a.wav").mock(
        return_value=httpx.Response(200, content=b"wav-bytes")
    )
    respx.get("https://r2.test/v.mp4").mock(
        return_value=httpx.Response(200, content=b"mp4-bytes")
    )
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "t-2", "status": "succeeded",
        "audio": {"url": "https://r2.test/a.wav", "content_type": "audio/wav"},
        "video": {"url": "https://r2.test/v.mp4", "content_type": "video/mp4"},
    }
    result = await _save_task_artifacts(body, tmp_path, "scene", "t-2")
    assert len(result) == 2
    assert (tmp_path / "scene.wav").read_bytes() == b"wav-bytes"
    assert (tmp_path / "scene.mp4").read_bytes() == b"mp4-bytes"


@respx.mock
async def test_save_task_artifacts_video_failure_reports_saved_audio(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/a2.wav").mock(
        return_value=httpx.Response(200, content=b"wav-bytes-2")
    )
    respx.get("https://r2.test/v2.mp4").mock(
        return_value=httpx.Response(403, text="expired")
    )
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "t-77", "status": "succeeded",
        "audio": {"url": "https://r2.test/a2.wav", "content_type": "audio/wav"},
        "video": {"url": "https://r2.test/v2.mp4", "content_type": "video/mp4"},
    }
    audio_dest = tmp_path / "scene2.wav"
    with pytest.raises(Exception) as exc:
        await _save_task_artifacts(body, tmp_path, "scene2", "t-77")
    msg = str(exc.value)
    assert "t-77" in msg
    assert str(audio_dest) in msg
    assert audio_dest.read_bytes() == b"wav-bytes-2"


async def test_save_task_artifacts_dest_error_mentions_task_id(monkeypatch, tmp_path):
    # Any exception raised while computing the destination path (e.g. an
    # OSError from a too-long filename) must still be re-raised with the
    # task_id and get_sfx_task recovery hint — the backend already
    # succeeded and charged the user by this point.
    from sonilo_mcp import api

    def boom(output_path, base_name, ext):
        raise OSError("boom")

    monkeypatch.setattr(api, "_artifact_dest", boom)
    body = {
        "task_id": "t-dest-err", "status": "succeeded",
        "audio": {"url": "https://r2.test/a.m4a", "content_type": "audio/mp4"},
    }
    with pytest.raises(Exception) as exc:
        await api._save_task_artifacts(body, tmp_path, "x", "t-dest-err")
    msg = str(exc.value)
    assert "t-dest-err" in msg
    assert "get_sfx_task" in msg


@respx.mock
async def test_save_task_artifacts_uses_caller_task_id_when_body_omits_it(
    monkeypatch, tmp_path
):
    # If the backend's terminal body ever omits task_id, the caller-supplied
    # task_id (already known-good from submission/the tool arg) must still
    # be used for the recovery hint — never body.get("task_id") -> None.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/a3.m4a").mock(
        return_value=httpx.Response(403, text="expired")
    )
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "status": "succeeded",
        "audio": {"url": "https://r2.test/a3.m4a", "content_type": "audio/mp4"},
    }
    with pytest.raises(Exception) as exc:
        await _save_task_artifacts(body, tmp_path, "x", "t-caller")
    msg = str(exc.value)
    assert "t-caller" in msg
    assert "get_sfx_task" in msg
    assert "None" not in msg


async def test_save_task_artifacts_failed_refunded(tmp_path):
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "t-3", "status": "failed",
        "error": {"code": "UPSTREAM_MALFORMED", "message": "bad output"},
        "refunded": True,
    }
    with pytest.raises(Exception) as exc:
        await _save_task_artifacts(body, tmp_path, "x", "t-3")
    msg = str(exc.value)
    assert "UPSTREAM_MALFORMED" in msg
    assert "bad output" in msg
    assert "you were not billed" in msg  # unique to the refunded branch
    assert "t-3" in msg


async def test_save_task_artifacts_failed_not_refunded(tmp_path):
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "t-4", "status": "failed",
        "error": {"code": "GENERATION_FAILED", "message": "boom"},
        "refunded": False,
    }
    with pytest.raises(Exception, match="not .*reversed|has not") as exc:
        await _save_task_artifacts(body, tmp_path, "x", "t-4")
    assert "t-4" in str(exc.value)


async def test_save_task_artifacts_failed_includes_task_id(tmp_path):
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "status": "failed",
        "error": {"code": "GENERATION_FAILED", "message": "boom"},
        "refunded": False,
    }
    with pytest.raises(Exception) as exc:
        await _save_task_artifacts(body, tmp_path, "x", "t-caller-2")
    msg = str(exc.value)
    assert "t-caller-2" in msg


async def test_save_task_artifacts_succeeded_without_audio(tmp_path):
    from sonilo_mcp.api import _save_task_artifacts
    body = {"task_id": "t-5", "status": "succeeded"}
    with pytest.raises(Exception, match="no audio"):
        await _save_task_artifacts(body, tmp_path, "x", "t-5")


async def test_save_task_artifacts_unexpected_status(tmp_path):
    from sonilo_mcp.api import _save_task_artifacts
    body = {"task_id": "t-6", "status": "cancelled"}
    with pytest.raises(Exception, match="Unexpected task status.*cancelled"):
        await _save_task_artifacts(body, tmp_path, "x", "t-6")


def _sfx_submit_then_poll(task_id: str, envelope: dict):
    """Mock POST 202 + one processing poll + terminal poll for an SFX flow."""
    submit = respx.post("https://api.test.local/v1/text-to-sfx").mock(
        return_value=httpx.Response(
            202, json={"task_id": task_id, "status": "processing"}
        )
    )
    poll = respx.get(f"https://api.test.local/v1/tasks/{task_id}").mock(
        side_effect=[
            httpx.Response(200, json={"task_id": task_id, "status": "processing"}),
            httpx.Response(200, json=envelope),
        ]
    )
    return submit, poll


@respx.mock
async def test_text_to_sfx_happy_path(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit, poll = _sfx_submit_then_poll("t-9", {
        "task_id": "t-9", "status": "succeeded",
        "audio": {"url": "https://r2.test/a.m4a", "content_type": "audio/mp4"},
    })
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"sfx-bytes")
    )
    result = await api.text_to_sfx(prompt="Thunder Clap", duration=8)

    sent = submit.calls.last.request
    assert sent.headers["content-type"].startswith(
        "application/x-www-form-urlencoded"
    )
    assert b"prompt=Thunder+Clap" in sent.content
    assert b"duration=8" in sent.content
    assert poll.call_count == 2
    expected = output_dir / "thunder-clap.m4a"
    assert expected.read_bytes() == b"sfx-bytes"
    assert len(result) == 1
    assert str(expected) in result[0].text


@respx.mock
async def test_text_to_sfx_long_prompt_writes_file(monkeypatch, output_dir):
    # Regression test: a realistic ~300-char prompt (well within the
    # documented 1-2000 char range) used to produce a slug long enough that
    # the OS-level filename (slug + collision suffix + extension) exceeded
    # 255 bytes, crashing _artifact_dest with a bare OSError.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    long_prompt = ("thunderous booming explosion with reverb " * 60)[:300]
    submit, poll = _sfx_submit_then_poll("t-long", {
        "task_id": "t-long", "status": "succeeded",
        "audio": {"url": "https://r2.test/a.m4a", "content_type": "audio/mp4"},
    })
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"sfx-bytes")
    )
    result = await api.text_to_sfx(prompt=long_prompt, duration=8)

    assert len(result) == 1
    saved_files = list(output_dir.iterdir())
    assert len(saved_files) == 1
    path = saved_files[0]
    assert path.read_bytes() == b"sfx-bytes"
    assert len(path.name.encode()) < 255


@respx.mock
async def test_text_to_sfx_sends_audio_format(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit, _ = _sfx_submit_then_poll("t-10", {
        "task_id": "t-10", "status": "succeeded",
        "audio": {"url": "https://r2.test/a.wav", "content_type": "audio/wav"},
    })
    respx.get("https://r2.test/a.wav").mock(
        return_value=httpx.Response(200, content=b"wav")
    )
    await api.text_to_sfx(prompt="beep", duration=2, audio_format="wav")
    assert b"audio_format=wav" in submit.calls.last.request.content
    assert (output_dir / "beep.wav").exists()


@respx.mock
async def test_text_to_sfx_same_prompt_twice_writes_two_files(monkeypatch, output_dir):
    # Guard against the get_sfx_task idempotency fix leaking into the
    # generation path: two text_to_sfx calls with the same prompt are two
    # DIFFERENT paid results and must never collapse onto one file.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)

    submit_route = respx.post("https://api.test.local/v1/text-to-sfx").mock(
        side_effect=[
            httpx.Response(202, json={"task_id": "t-echo-1"}),
            httpx.Response(202, json={"task_id": "t-echo-2"}),
        ]
    )
    respx.get("https://api.test.local/v1/tasks/t-echo-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-echo-1", "status": "succeeded",
            "audio": {"url": "https://r2.test/echo-1.m4a", "content_type": "audio/mp4"},
        })
    )
    respx.get("https://api.test.local/v1/tasks/t-echo-2").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-echo-2", "status": "succeeded",
            "audio": {"url": "https://r2.test/echo-2.m4a", "content_type": "audio/mp4"},
        })
    )
    respx.get("https://r2.test/echo-1.m4a").mock(
        return_value=httpx.Response(200, content=b"echo-payload-1")
    )
    respx.get("https://r2.test/echo-2.m4a").mock(
        return_value=httpx.Response(200, content=b"echo-payload-2")
    )

    result1 = await api.text_to_sfx(prompt="echo", duration=2)
    result2 = await api.text_to_sfx(prompt="echo", duration=2)

    assert submit_route.call_count == 2
    files = sorted(output_dir.iterdir())
    assert len(files) == 2
    payloads = {p.read_bytes() for p in files}
    assert payloads == {b"echo-payload-1", b"echo-payload-2"}
    assert "Success" in result1[0].text
    assert "Success" in result2[0].text


@respx.mock
async def test_text_to_sfx_backend_rejection_surfaces(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.post("https://api.test.local/v1/text-to-sfx").mock(
        return_value=httpx.Response(
            400, json={"detail": "audio_format must be one of wav, mp3, aac, flac"}
        )
    )
    from sonilo_mcp.api import text_to_sfx
    with pytest.raises(Exception, match="audio_format must be one of"):
        await text_to_sfx(prompt="beep", duration=2, audio_format="ogg")


@respx.mock
async def test_video_to_sfx_url_mode_downloads_both(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-sfx").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-20", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-20").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-20", "status": "succeeded",
            "audio": {"url": "https://r2.test/a.m4a", "content_type": "audio/mp4"},
            "video": {"url": "https://r2.test/v.mp4", "content_type": "video/mp4"},
        })
    )
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"audio")
    )
    respx.get("https://r2.test/v.mp4").mock(
        return_value=httpx.Response(200, content=b"video")
    )
    result = await api.video_to_sfx(
        video_url="https://example.com/clip.mp4", prompt="City Rain"
    )
    sent = submit.calls.last.request
    assert b"video_url=" in sent.content
    assert b"City+Rain" in sent.content
    assert len(result) == 2
    assert (output_dir / "city-rain.m4a").read_bytes() == b"audio"
    assert (output_dir / "city-rain.mp4").read_bytes() == b"video"


@respx.mock
async def test_video_to_sfx_segments_passthrough(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-sfx").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-21", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-21").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-21", "status": "succeeded",
            "audio": {"url": "https://r2.test/a.m4a", "content_type": "audio/mp4"},
            "video": {"url": "https://r2.test/v.mp4", "content_type": "video/mp4"},
        })
    )
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"a")
    )
    respx.get("https://r2.test/v.mp4").mock(
        return_value=httpx.Response(200, content=b"v")
    )
    segs = [
        {"start": 0, "end": 3, "prompt": "footsteps"},
        {"start": 3, "end": 8, "prompt": "door slam"},
    ]
    await api.video_to_sfx(
        video_url="https://example.com/clip.mp4", segments=segs
    )
    from urllib.parse import parse_qs
    sent_fields = parse_qs(submit.calls.last.request.content.decode())
    assert json.loads(sent_fields["segments"][0]) == segs


@respx.mock
async def test_video_to_sfx_path_mode_uploads_multipart(
    monkeypatch, output_dir, tmp_path
):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": 300})
    )
    video = output_dir / "clip.mp4"
    video.write_bytes(b"FAKE-MP4")
    submit = respx.post("https://api.test.local/v1/video-to-sfx").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-22", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-22").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-22", "status": "succeeded",
            "audio": {"url": "https://r2.test/a.m4a", "content_type": "audio/mp4"},
            "video": {"url": "https://r2.test/v.mp4", "content_type": "video/mp4"},
        })
    )
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"a")
    )
    respx.get("https://r2.test/v.mp4").mock(
        return_value=httpx.Response(200, content=b"v")
    )
    api._reset_services_cache()
    result = await api.video_to_sfx(video_path=str(video))
    sent = submit.calls.last.request
    assert sent.headers["content-type"].startswith("multipart/form-data")
    assert b"FAKE-MP4" in sent.content
    assert len(result) == 2
    # No prompt -> base name falls back to sfx-{task_id[:8]}.
    assert (output_dir / "sfx-t-22.m4a").exists()


async def test_video_to_sfx_both_inputs_rejected(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp.api import video_to_sfx
    with pytest.raises(Exception, match="exactly one"):
        await video_to_sfx(
            video_path="/tmp/a.mp4", video_url="https://example.com/b.mp4"
        )
    with pytest.raises(Exception, match="exactly one"):
        await video_to_sfx()


@pytest.mark.parametrize("bad_url", ["file:///etc/passwd", "ftp://x/y.mp4"])
async def test_video_to_sfx_rejects_non_http_url(monkeypatch, output_dir, bad_url):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp.api import video_to_sfx
    with pytest.raises(Exception, match="http"):
        await video_to_sfx(video_url=bad_url)


@respx.mock
async def test_video_to_sfx_rejects_video_over_180s(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp.api import video_to_sfx
    # 200s passes the music cap (360) but must fail the SFX cap (180).
    _patch_ffprobe(monkeypatch, duration=200.0)
    submit_route = respx.post("https://api.test.local/v1/video-to-sfx").mock(
        return_value=httpx.Response(202, json={"task_id": "t-should-not-charge"})
    )
    with pytest.raises(Exception, match="exceeds the maximum"):
        await video_to_sfx(video_url="https://example.com/long.mp4")
    # Must NOT charge — the duration check must reject before the submit POST.
    assert submit_route.call_count == 0


@respx.mock
async def test_get_sfx_task_processing(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-30").mock(
        return_value=httpx.Response(
            200, json={"task_id": "t-30", "status": "processing"}
        )
    )
    from sonilo_mcp.api import get_sfx_task
    result = await get_sfx_task("t-30")
    assert len(result) == 1
    assert "still processing" in result[0].text


@respx.mock
async def test_get_sfx_task_succeeded_downloads(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-31-abcdef99").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-31-abcdef99", "status": "succeeded",
            "audio": {"url": "https://r2.test/a.mp3", "content_type": "audio/mpeg"},
        })
    )
    respx.get("https://r2.test/a.mp3").mock(
        return_value=httpx.Response(200, content=b"mp3-bytes")
    )
    from sonilo_mcp.api import get_sfx_task
    result = await get_sfx_task("t-31-abcdef99")
    assert len(result) == 1
    # No prompt available here -> sfx-{task_id[:8]}; ext from content_type.
    expected = output_dir / "sfx-t-31-abc.mp3"
    assert expected.read_bytes() == b"mp3-bytes"


@respx.mock
async def test_get_sfx_task_twice_reuses_existing_file(monkeypatch, output_dir):
    # get_sfx_task is the documented recovery tool and its own messages
    # invite repeat calls ("still processing, try again later"). Once the
    # task has succeeded, a second call must NOT re-download a duplicate
    # copy of the same paid result.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-99").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-99", "status": "succeeded",
            "audio": {
                "url": "https://r2.test/dup.mp3", "content_type": "audio/mpeg",
                "file_size": len(b"dup-bytes"),
            },
        })
    )
    route = respx.get("https://r2.test/dup.mp3").mock(
        return_value=httpx.Response(200, content=b"dup-bytes")
    )
    from sonilo_mcp.api import get_sfx_task

    first = await get_sfx_task("t-99")
    second = await get_sfx_task("t-99")

    files = list(output_dir.iterdir())
    assert len(files) == 1
    assert files[0].read_bytes() == b"dup-bytes"
    assert route.call_count == 1
    assert "Success" in first[0].text
    assert "already downloaded" in second[0].text.lower()


@respx.mock
async def test_get_sfx_task_failed_raises_with_refund(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-32").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-32", "status": "failed",
            "error": {"code": "TIMEOUT", "message": "upstream timeout"},
            "refunded": True,
        })
    )
    from sonilo_mcp.api import get_sfx_task
    with pytest.raises(Exception, match="TIMEOUT.*you were not billed"):
        await get_sfx_task("t-32")


@respx.mock
async def test_get_sfx_task_status_get_transient_failure_mentions_task_id(
    monkeypatch, output_dir
):
    # The status GET can fail transiently (backend 5xx) even after the
    # generation itself succeeded and charged — the raised message must
    # still carry the task_id so the paid result stays recoverable.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-33").mock(
        return_value=httpx.Response(500, text="boom")
    )
    from sonilo_mcp.api import get_sfx_task
    with pytest.raises(Exception) as exc:
        await get_sfx_task("t-33")
    assert "t-33" in str(exc.value)


@respx.mock
async def test_get_sfx_task_unknown_task_404(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/nope").mock(
        return_value=httpx.Response(404, json={"detail": "Task not found"})
    )
    from sonilo_mcp.api import get_sfx_task
    with pytest.raises(Exception, match="Task not found"):
        await get_sfx_task("nope")


@respx.mock
async def test_get_sfx_task_404_gives_no_retry_advice(monkeypatch, output_dir):
    # A 404 is permanent (bad/typo'd id, or a non-SFX task id — /v1/tasks is
    # SFX-only). Retrying can never help, so the message must NOT tell the
    # caller to call get_sfx_task again / that the result "may still be
    # available" — that advice is only true for transient failures.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/music-task-1").mock(
        return_value=httpx.Response(404, json={"detail": "Task not found"})
    )
    from sonilo_mcp.api import get_sfx_task
    with pytest.raises(Exception) as exc:
        await get_sfx_task("music-task-1")
    msg = str(exc.value)
    assert "Task not found" in msg
    assert "again shortly" not in msg
    assert "may still be available" not in msg


@respx.mock
async def test_get_sfx_task_transient_5xx_keeps_retry_advice(monkeypatch, output_dir):
    # A 500 is transient — _http_get_json retries once internally, so mock
    # two 500 responses. The recovery advice must still be present.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-transient").mock(
        side_effect=[
            httpx.Response(500, text="boom"),
            httpx.Response(500, text="boom"),
        ]
    )
    from sonilo_mcp.api import get_sfx_task
    with pytest.raises(Exception) as exc:
        await get_sfx_task("t-transient")
    msg = str(exc.value)
    assert "t-transient" in msg
    assert "again shortly" in msg
    assert "may still be available" in msg


@respx.mock
async def test_poll_task_404_gives_no_retry_advice(monkeypatch):
    # Same permanent-vs-transient distinction as get_sfx_task, but during
    # polling (e.g. auth revoked or the task id vanishes mid-poll).
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-404-poll").mock(
        return_value=httpx.Response(404, json={"detail": "Task not found"})
    )
    from sonilo_mcp.api import _poll_task
    with pytest.raises(Exception) as exc:
        await _poll_task("t-404-poll", timeout_seconds=600)
    msg = str(exc.value)
    assert "Task not found" in msg
    assert "may still be running" not in msg


@respx.mock
async def test_get_sfx_task_402_keeps_task_id(monkeypatch, output_dir):
    # get_sfx_task's own status GET can 402 if billing is suspended after
    # the original task was already charged — the task_id must still be
    # recoverable from the error message.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-402b").mock(
        return_value=httpx.Response(402, json={"detail": "Account is suspended"})
    )
    from sonilo_mcp.api import get_sfx_task
    with pytest.raises(Exception) as exc:
        await get_sfx_task("t-402b")
    msg = str(exc.value)
    assert "t-402b" in msg
    assert "get_sfx_task" in msg


@respx.mock
async def test_get_sfx_task_reuse_rejects_size_mismatch(monkeypatch, output_dir):
    # A hard process kill mid-write can leave a non-empty, TRUNCATED file at
    # the canonical path with no handler ever having cleaned it up. The
    # reuse path must verify size against the envelope's file_size and,
    # on a mismatch, treat the file as corrupt: remove it and re-download
    # into the SAME canonical path (not a `-1` duplicate).
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    real_bytes = b"REAL-AUDIO-PAYLOAD-BYTES"
    respx.get("https://api.test.local/v1/tasks/t-corrupt").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-corrupt", "status": "succeeded",
            "audio": {
                "url": "https://r2.test/real.mp3",
                "content_type": "audio/mpeg",
                "file_size": len(real_bytes),
            },
        })
    )
    route = respx.get("https://r2.test/real.mp3").mock(
        return_value=httpx.Response(200, content=real_bytes)
    )
    canonical = output_dir / "sfx-t-corrup.mp3"
    canonical.write_bytes(b"CORRUPT-PARTIAL-GARBAGE")

    from sonilo_mcp.api import get_sfx_task
    result = await get_sfx_task("t-corrupt")

    assert route.call_count == 1
    assert canonical.read_bytes() == real_bytes
    files = list(output_dir.iterdir())
    assert len(files) == 1
    assert "Success" in result[0].text


@respx.mock
async def test_get_sfx_task_reuse_accepts_size_match(monkeypatch, output_dir):
    # When the on-disk size exactly matches the envelope's file_size, the
    # existing file is genuine and must be reused without re-downloading.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    real_bytes = b"REAL-AUDIO-BYTES-MATCH"
    respx.get("https://api.test.local/v1/tasks/t-match").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-match", "status": "succeeded",
            "audio": {
                "url": "https://r2.test/match.mp3",
                "content_type": "audio/mpeg",
                "file_size": len(real_bytes),
            },
        })
    )
    route = respx.get("https://r2.test/match.mp3").mock(
        return_value=httpx.Response(200, content=real_bytes)
    )
    canonical = output_dir / "sfx-t-match.mp3"
    canonical.write_bytes(real_bytes)

    from sonilo_mcp.api import get_sfx_task
    result = await get_sfx_task("t-match")

    assert route.call_count == 0
    assert "already downloaded" in result[0].text.lower()
    assert canonical.read_bytes() == real_bytes


@respx.mock
async def test_get_sfx_task_video_task_reuse_returns_both_paths(monkeypatch, output_dir):
    # A video_to_sfx-style envelope (audio + video) recovered twice via
    # get_sfx_task: the second call must return BOTH the audio and video
    # paths and download NEITHER. Pins the early `return saved` in the
    # video reuse branch of _save_task_artifacts across all four
    # (audio present/absent) x (video present/absent) combinations.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    audio_bytes = b"AUDIO-BYTES-VIDEO-TASK"
    video_bytes = b"VIDEO-BYTES-VIDEO-TASK"
    respx.get("https://api.test.local/v1/tasks/t-vidtask").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-vidtask", "status": "succeeded",
            "audio": {
                "url": "https://r2.test/vt-audio.mp3",
                "content_type": "audio/mpeg",
                "file_size": len(audio_bytes),
            },
            "video": {
                "url": "https://r2.test/vt-video.mp4",
                "content_type": "video/mp4",
                "file_size": len(video_bytes),
            },
        })
    )
    audio_route = respx.get("https://r2.test/vt-audio.mp3").mock(
        return_value=httpx.Response(200, content=audio_bytes)
    )
    video_route = respx.get("https://r2.test/vt-video.mp4").mock(
        return_value=httpx.Response(200, content=video_bytes)
    )
    from sonilo_mcp.api import get_sfx_task

    first = await get_sfx_task("t-vidtask")
    assert len(first) == 2
    assert audio_route.call_count == 1
    assert video_route.call_count == 1

    second = await get_sfx_task("t-vidtask")
    assert len(second) == 2
    assert audio_route.call_count == 1
    assert video_route.call_count == 1
    assert sum("already downloaded" in c.text.lower() for c in second) == 2
