"""Tests for sonilo_mcp.api."""
from __future__ import annotations


def test_package_imports():
    from sonilo_mcp import main, mcp
    assert callable(main)
    assert mcp.name == "Sonilo"


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
    respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(200, content=ndjson)
    )
    from sonilo_mcp.api import text_to_music
    result = await text_to_music(prompt="happy", duration=10)

    assert len(result) == 1
    expected = output_dir / "happy-tune.mp3"
    assert expected.exists()
    assert expected.read_bytes() == audio
    assert "happy-tune.mp3" in result[0].text


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
    assert (output_dir / "twin-0.mp3").read_bytes() == a
    assert (output_dir / "twin-1.mp3").read_bytes() == b_bytes


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
    # Fallback name pattern: sonilo-<unix-timestamp>.mp3
    name = Path(result[0].text.split("File saved as: ")[1]).name
    assert name.startswith("sonilo-")
    assert name.endswith(".mp3")


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
    await text_to_music(prompt="energetic rock", duration=42)
    body = json.loads(route.calls.last.request.content)
    assert body == {"prompt": "energetic rock", "duration": 42}
    auth = route.calls.last.request.headers["authorization"]
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
    import sonilo_mcp.api as api_mod
    api_mod._services_cache = None
    api_mod._services_cache_expiry = 0.0
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
    import sonilo_mcp.api as api_mod
    api_mod._services_cache = None
    api_mod._services_cache_expiry = 0.0
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
