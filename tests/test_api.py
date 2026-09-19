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


def test_validate_variants_num_accepts_the_full_range():
    from sonilo_mcp.api import _validate_variants_num
    for n in (1, 2, 10):
        _validate_variants_num(n)  # must not raise


@pytest.mark.parametrize("bad", [0, -1, 11, 100])
def test_validate_variants_num_rejects_out_of_range(bad):
    from sonilo_mcp.api import _validate_variants_num
    with pytest.raises(Exception, match="variants_num must be between 1 and 10"):
        _validate_variants_num(bad)


def test_validate_prompt_influence_accepts_the_full_range():
    from sonilo_mcp.api import _validate_prompt_influence
    # Both endpoints are inclusive, and 0.0 in particular is a meaningful
    # value (loosest adherence) — it must never be treated as "unset".
    for v in (None, 0.0, 0.25, 0.5, 1.0):
        _validate_prompt_influence(v)  # must not raise


@pytest.mark.parametrize("bad", [-0.1, -1.0, 1.01, 2.0])
def test_validate_prompt_influence_rejects_out_of_range(bad):
    from sonilo_mcp.api import _validate_prompt_influence
    with pytest.raises(Exception, match="prompt_influence must be between 0 and 1"):
        _validate_prompt_influence(bad)


def test_music_audio_label_uses_title_when_present():
    from sonilo_mcp.api import _music_audio_label
    assert _music_audio_label({"title": {"title": "Sunrise"}}) == 'music audio — "Sunrise"'
    assert _music_audio_label({}) == "music audio"
    assert _music_audio_label({"title": {"title": "  "}}) == "music audio"
    assert _music_audio_label({"title": "not-a-dict"}) == "music audio"


def test_ducking_base_name_from_path():
    from sonilo_mcp.api import _ducking_base_name
    assert _ducking_base_name("/tmp/Interview Take 2.mp4", None, "t-abcdefgh") == (
        "interview-take-2-ducked"
    )


def test_ducking_base_name_from_url_strips_query():
    from sonilo_mcp.api import _ducking_base_name
    name = _ducking_base_name(
        None, "https://cdn.test/clips/voice.wav?sig=abc#frag", "t-abcdefgh"
    )
    assert name == "voice-ducked"


def test_ducking_base_name_falls_back_to_task_id():
    # A URL with no usable filename (bare host, trailing slash) leaves
    # nothing to name the file after. "t-abcdefgh"[:8] == "t-abcdef".
    from sonilo_mcp.api import _ducking_base_name
    assert _ducking_base_name(None, "https://cdn.test/", "t-abcdefgh") == (
        "ducked-t-abcdef"
    )


def test_ducking_base_name_truncates_long_stem():
    from sonilo_mcp.api import _ducking_base_name
    name = _ducking_base_name("/tmp/" + "a" * 200 + ".wav", None, "t-abcdefgh")
    # _slugify caps the stem at 80 chars; the suffix is added after.
    assert name == "a" * 80 + "-ducked"


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


def test_extract_detail_message_field():
    # The real backend's public /v1/* contract is {"code", "message"} — NOT
    # FastAPI's default {"detail"}. `message` must be preferred.
    from sonilo_mcp.api import _extract_detail
    assert _extract_detail('{"code":"not_found","message":"oops"}') == "oops"


def test_extract_detail_prefers_message_over_detail():
    from sonilo_mcp.api import _extract_detail
    assert _extract_detail(
        '{"code":"x","message":"real message","detail":"decoy"}'
    ) == "real message"


def test_extract_detail_detail_fallback():
    # Non-public FastAPI routes (and any other future shape) still use the
    # default `detail` key — the MCP client never calls one, but falling
    # back to it is harmless and keeps this robust.
    from sonilo_mcp.api import _extract_detail
    assert _extract_detail('{"detail":"oops"}') == "oops"


def test_extract_detail_non_string_message_not_crash():
    # A backend bug could send a non-string message (e.g. an int/dict) —
    # must stringify, never raise.
    from sonilo_mcp.api import _extract_detail
    assert _extract_detail('{"code":"x","message":123}') == "123"


def test_extract_detail_plain_text():
    from sonilo_mcp.api import _extract_detail
    assert _extract_detail("some plain body") == "some plain body"


def test_extract_detail_malformed_json():
    from sonilo_mcp.api import _extract_detail
    assert _extract_detail("{not json") == "{not json"


def test_raise_http_error_401():
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception, match="Invalid SONILO_API_KEY"):
        _raise_http_error(401, '{"code":"unauthorized","message":"Invalid API key"}')


def test_raise_http_error_401_detail_fallback():
    # Non-public path shape ({"detail": ...}) must still work.
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception, match="Invalid SONILO_API_KEY"):
        _raise_http_error(401, '{"detail":"Invalid API key"}')


def test_raise_http_error_402_minutes():
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception, match="Top up"):
        _raise_http_error(
            402,
            '{"code":"insufficient_credit","message":"Insufficient minutes: 30 needed"}',
        )


def test_raise_http_error_402_insufficient_balance():
    # The cash-wallet wording — by far the most common 402, and the one the
    # old "minute"/"credit" keyword gate silently failed to match.
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception) as exc:
        _raise_http_error(
            402,
            '{"code":"insufficient_balance",'
            '"message":"Insufficient balance: balance=0.0000 < needed=1.3308"}',
        )
    message = str(exc.value)
    assert "Insufficient balance" in message
    assert "Top up at" in message
    # detail has no trailing punctuation, so _end_sentence must supply it
    # rather than letting the two sentences run together.
    assert "needed=1.3308. Top up at" in message


def test_raise_http_error_402_trial_exhausted_keeps_one_billing_link():
    # This 402's message already ends in its own call to action carrying the
    # billing URL, so appending the generic "Top up at <same url>" would show
    # the same link twice in one sentence — and "top up" names a flow an
    # account that has never paid us hasn't reached.
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception) as exc:
        _raise_http_error(
            402,
            '{"code":"trial_exhausted","message":"You\'ve used your 2 free '
            "trial calls for text-to-music. Add a payment method to continue: "
            'https://platform.sonilo.com/dashboard/billing"}',
        )
    message = str(exc.value)
    assert "free trial calls for text-to-music" in message
    assert "Add a payment method to continue" in message
    assert "Top up at" not in message
    assert message.count("https://platform.sonilo.com/dashboard/billing") == 1
    # Ends on the URL, with no punctuation glued to it.
    assert message.endswith("/dashboard/billing")


def test_raise_http_error_402_suspended():
    # A suspended account is resolved on the same billing page, so it gets
    # the link too — every 402 but trial_exhausted is unconditional.
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception) as exc:
        _raise_http_error(
            402, '{"code":"account_suspended","message":"Account is suspended"}'
        )
    assert "suspended" in str(exc.value).lower()
    assert "top up at" in str(exc.value).lower()


def test_raise_http_error_402_does_not_double_punctuate():
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception) as exc:
        _raise_http_error(402, '{"message":"Account is suspended."}')
    assert ".. Top up" not in str(exc.value)
    assert "suspended. Top up at" in str(exc.value)


def test_raise_http_error_413():
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception, match="File too large"):
        _raise_http_error(413, '{"code":"payload_too_large","message":"Max 300MB"}')


def test_raise_http_error_422():
    # The backend's 422 body also carries an `errors` array; `message` is
    # still the right field to surface.
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception, match="Could not read video duration"):
        _raise_http_error(
            422,
            '{"code":"validation_error","message":"Could not read video duration",'
            '"errors":[]}',
        )


def test_raise_http_error_429():
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception, match="Rate limit exceeded"):
        _raise_http_error(429, '{"code":"rate_limited","message":"Rate limit exceeded"}')


@pytest.mark.parametrize(
    "message",
    [
        "Rate limit exceeded: your account allows 60 requests per minute. "
        "Please retry after 1 minute. To raise your limit, please contact "
        "info@sonilo.com.",
        "Too many concurrent generations: 5 of 5 in progress. Please wait for "
        "one to finish before starting another. To raise your limit, please "
        "contact info@sonilo.com.",
    ],
)
def test_raise_http_error_429_does_not_repeat_the_backend_phrase(message):
    """The backend's 429 already names the limit; do not say it a second time.

    Both sentences carry the account's numbers and the address to raise the
    limit, so they must arrive whole — an agent acts on which limit was hit.
    """
    from sonilo_mcp.api import _raise_http_error

    with pytest.raises(Exception) as exc_info:
        _raise_http_error(
            429, json.dumps({"code": "rate_limit_exceeded", "message": message})
        )

    assert str(exc_info.value) == message
    assert str(exc_info.value).lower().count("rate limit exceeded") <= 1


def test_raise_http_error_429_still_labels_an_unannounced_detail():
    """A 429 whose text doesn't say it was a rate limit still gets the label —
    the daily MCP cap, an older backend, or a proxy that ate the body."""
    from sonilo_mcp.api import _raise_http_error

    with pytest.raises(Exception, match="Rate limit exceeded: Daily MCP generation"):
        _raise_http_error(
            429,
            '{"code":"rate_limit_exceeded","message":"Daily MCP generation limit '
            'reached for this account. Try again tomorrow."}',
        )


def test_raise_http_error_500():
    from sonilo_mcp.api import _raise_http_error
    with pytest.raises(Exception, match="Server error.*retry"):
        _raise_http_error(500, '{"code":"internal_error","message":"internal"}')


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
        return_value=httpx.Response(
            401, json={"code": "unauthorized", "message": "Invalid API key"}
        )
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
async def test_get_account_services_passes_the_trial_quota_through(monkeypatch):
    # The whole point of the trial quota is that the assistant can see it and
    # warn before spending it, so it must survive the response envelope check.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={
            "available_services": ["text_to_music"],
            "rpm_limit": 60,
            "concurrency_limit": 4,
            "discount_factor": 1.0,
            "max_upload_size_mb": 300,
            "trial": {"text_to_music": {"granted": 2, "used": 1, "remaining": 1}},
        })
    )
    from sonilo_mcp.api import get_account_services
    out = await get_account_services()
    assert out["trial"]["text_to_music"] == {"granted": 2, "used": 1, "remaining": 1}


def test_extract_code_reads_the_public_error_envelope():
    from sonilo_mcp.api import _extract_code
    assert _extract_code('{"code":"trial_exhausted","message":"x"}') == "trial_exhausted"


def test_extract_code_returns_none_without_a_usable_code():
    from sonilo_mcp.api import _extract_code
    assert _extract_code('{"message":"no code here"}') is None
    assert _extract_code("not json at all") is None
    assert _extract_code('["not", "an", "object"]') is None
    # A non-string code is a backend bug; treat it as absent rather than
    # comparing it to the string literals in _raise_http_error.
    assert _extract_code('{"code":402}') is None


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


@respx.mock
async def test_get_account_services_rejects_non_dict_body(monkeypatch):
    # A 200 whose body isn't a JSON object (e.g. `null` or a bare list) must
    # not be handed to the MCP host as-is: FastMCP maps None -> [] and a
    # list into unlabeled text fragments, either of which is silently
    # indistinguishable from a legitimate empty result.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.get("https://api.test.local/v1/account/services")
    from sonilo_mcp.api import get_account_services

    route.mock(return_value=httpx.Response(
        200, content=b"null", headers={"content-type": "application/json"}
    ))
    with pytest.raises(Exception, match="account/services"):
        await get_account_services()

    route.mock(return_value=httpx.Response(200, json=["not", "a", "dict"]))
    with pytest.raises(Exception, match="account/services"):
        await get_account_services()


@respx.mock
async def test_get_usage_rejects_non_dict_body(monkeypatch):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.get("https://api.test.local/v1/account/usage")
    from sonilo_mcp.api import get_usage

    route.mock(return_value=httpx.Response(
        200, content=b"null", headers={"content-type": "application/json"}
    ))
    with pytest.raises(Exception, match="account/usage"):
        await get_usage()

    route.mock(return_value=httpx.Response(200, json=["not", "a", "dict"]))
    with pytest.raises(Exception, match="account/usage"):
        await get_usage()


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
        return_value=httpx.Response(
            401, json={"code": "unauthorized", "message": "Invalid API key"}
        )
    )
    from sonilo_mcp.api import text_to_music
    with pytest.raises(Exception, match="Invalid SONILO_API_KEY"):
        await text_to_music(prompt="x", duration=5)


@respx.mock
async def test_text_to_music_429_error(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(
            429, json={"code": "rate_limited", "message": "Rate limit exceeded"}
        )
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


@pytest.mark.parametrize("bad", [0, -1, 11, 100])
async def test_text_to_music_rejects_out_of_range_variants_num(output_dir, bad):
    from sonilo_mcp.api import text_to_music
    with pytest.raises(Exception, match="variants_num must be between 1 and 10"):
        await text_to_music(prompt="x", duration=5, variants_num=bad)


@respx.mock
async def test_text_to_music_variants_num_forces_async_and_sends_field(
    monkeypatch, output_dir
):
    # variants_num > 1 requires mode=async on the backend — must be sent
    # automatically, same as output_format='wav'.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp.api import text_to_music

    async def no_sleep(s):
        pass

    import sonilo_mcp.api as api
    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(202, json={"task_id": "vn-1", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/vn-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "vn-1", "status": "succeeded",
            "audio": [
                {"stream_index": 0, "url": "https://r2.test/v0.m4a",
                 "content_type": "audio/mp4", "file_size": 2,
                 "title": {"title": "Sunrise Drive", "summary": "s", "display_tags": []}},
                {"stream_index": 1, "url": "https://r2.test/v1.m4a",
                 "content_type": "audio/mp4", "file_size": 2,
                 "title": {"title": "Night Cruise", "summary": "s", "display_tags": []}},
                {"stream_index": 2, "url": "https://r2.test/v2.m4a",
                 "content_type": "audio/mp4", "file_size": 2},
            ],
        })
    )
    respx.get("https://r2.test/v0.m4a").mock(return_value=httpx.Response(200, content=b"v0"))
    respx.get("https://r2.test/v1.m4a").mock(return_value=httpx.Response(200, content=b"v1"))
    respx.get("https://r2.test/v2.m4a").mock(return_value=httpx.Response(200, content=b"v2"))

    result = await text_to_music(prompt="road trip", duration=20, variants_num=3)

    sent = submit.calls.last.request
    assert b"variants_num=3" in sent.content
    assert b"mode=async" in sent.content

    assert len(result) == 3
    assert (output_dir / "road-trip-0.m4a").read_bytes() == b"v0"
    assert (output_dir / "road-trip-1.m4a").read_bytes() == b"v1"
    assert (output_dir / "road-trip-2.m4a").read_bytes() == b"v2"
    texts = [t.text for t in result]
    assert any('"Sunrise Drive"' in t and "road-trip-0.m4a" in t for t in texts)
    assert any('"Night Cruise"' in t and "road-trip-1.m4a" in t for t in texts)
    # Untitled entry (variant 2) still saves, just without a title in the label.
    assert any(
        t.startswith("Success (music audio). File saved as:")
        and "road-trip-2.m4a" in t
        for t in texts
    )


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


@respx.mock
async def test_video_to_music_size_cap_enforced_on_actual_bytes(
    monkeypatch, output_dir, tmp_path
):
    # TOCTOU: the stat()-based size check runs before the ffprobe await (up
    # to 30s). If the file on disk is replaced with a larger one during
    # that window, the stale check must not let the oversized bytes bypass
    # the cap — the cap must be enforced on what's actually read.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    video = tmp_path / "clip.mp4"
    small = b"\x00" * 1024
    large = b"\x01" * (5 * 1024 * 1024)
    video.write_bytes(small)

    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": 2})
    )
    api._reset_services_cache()

    monkeypatch.setattr(api.shutil, "which", lambda name: "/usr/bin/ffprobe")

    async def fake_exec(*args, **kwargs):
        video.write_bytes(large)  # swap happens during the caller's await
        payload = json.dumps({"format": {"duration": "10.0"}}).encode()
        return _FakeProc(payload, returncode=0)

    monkeypatch.setattr(api.asyncio, "create_subprocess_exec", fake_exec)

    upload_route = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(200, content=b"")
    )

    with pytest.raises(Exception, match="too large"):
        await api.video_to_music(video_path=str(video))
    # Must NOT charge — the oversized (swapped) bytes must never be uploaded.
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


async def test_check_media_duration_rejects_too_long(monkeypatch):
    from sonilo_mcp.api import _check_media_duration
    _patch_ffprobe(monkeypatch, duration=400.0)
    with pytest.raises(Exception, match="exceeds the maximum"):
        await _check_media_duration("/tmp/clip.mp4")


async def test_check_media_duration_allows_within_limit(monkeypatch):
    from sonilo_mcp.api import _check_media_duration
    _patch_ffprobe(monkeypatch, duration=120.0)
    await _check_media_duration("/tmp/clip.mp4")  # must not raise


async def test_check_media_duration_skips_without_ffprobe(monkeypatch):
    from sonilo_mcp.api import _check_media_duration
    _patch_ffprobe(monkeypatch, duration=400.0, installed=False)
    # ffprobe missing -> fail open, no raise even though it would be too long.
    await _check_media_duration("/tmp/clip.mp4")


async def test_check_media_duration_fails_open_on_probe_error(monkeypatch):
    from sonilo_mcp.api import _check_media_duration
    _patch_ffprobe(monkeypatch, returncode=1)  # ffprobe couldn't read it
    await _check_media_duration("/tmp/clip.mp4")  # must not raise


async def test_check_media_duration_fails_open_on_nul_byte_source(monkeypatch):
    # A source containing an embedded NUL byte makes the REAL
    # create_subprocess_exec raise ValueError("embedded null byte") before it
    # spawns. This best-effort helper must fail open (stay silent) rather than
    # let that ValueError crash the whole request.
    from sonilo_mcp import api
    from sonilo_mcp.api import _check_media_duration
    # ffprobe "installed" so we don't short-circuit; use the real subprocess
    # launcher (not the fake) so the NUL byte actually triggers the ValueError.
    monkeypatch.setattr(api.shutil, "which", lambda name: "/usr/bin/ffprobe")
    await _check_media_duration("https://e.com/a\x00.mp4")  # must not raise


async def test_check_media_duration_sfx_cap_rejects_over_the_cap(monkeypatch):
    from sonilo_mcp.api import _check_media_duration, _SFX_MAX_VIDEO_DURATION_SECONDS
    _patch_ffprobe(monkeypatch, duration=500.0)
    with pytest.raises(Exception, match="exceeds the maximum"):
        await _check_media_duration(
            "/tmp/clip.mp4", max_seconds=_SFX_MAX_VIDEO_DURATION_SECONDS
        )


async def test_check_media_duration_music_cap_allows_200s(monkeypatch):
    from sonilo_mcp.api import _check_media_duration
    _patch_ffprobe(monkeypatch, duration=200.0)
    # Default cap stays 360s — 200s must not raise.
    await _check_media_duration("/tmp/clip.mp4")


async def test_check_media_duration_probes_plain_audio(monkeypatch):
    # ffprobe reports format.duration for audio files too — the ducking
    # tool relies on this to pre-check voice/music audio inputs.
    from sonilo_mcp.api import _check_media_duration
    _patch_ffprobe(monkeypatch, duration=400.0)
    with pytest.raises(Exception, match="exceeds the maximum"):
        await _check_media_duration("/tmp/voice.wav")


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


@respx.mock
async def test_video_to_music_preserve_speech_url_mode_submits_async_fields(
    monkeypatch, output_dir
):
    # preserve_speech=True must route through the task submit+poll path (mode
    # async), sending both mode and preserve_speech as form fields, instead
    # of the plain streaming POST.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-vocals-1", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-vocals-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-vocals-1", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a.m4a",
                "content_type": "audio/mp4", "sample_rate": 44100,
                "channels": 2, "file_size": 5,
            }],
        })
    )
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"audio")
    )
    result = await api.video_to_music(
        video_url="https://cdn.example.com/v.mp4",
        prompt="Chill Beat",
        preserve_speech=True,
    )
    from urllib.parse import parse_qs
    sent = parse_qs(submit.calls.last.request.content.decode())
    assert sent["mode"] == ["async"]
    assert sent["preserve_speech"] == ["true"]
    assert sent["video_url"] == ["https://cdn.example.com/v.mp4"]
    assert sent["prompt"] == ["Chill Beat"]
    assert len(result) == 1
    assert (output_dir / "chill-beat.m4a").read_bytes() == b"audio"


@respx.mock
async def test_video_to_music_preserve_speech_false_no_extra_fields(
    monkeypatch, output_dir
):
    # Default preserve_speech=False must keep streaming behavior UNCHANGED —
    # no mode/preserve_speech fields sent, no task_id round trip.
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
    await video_to_music(video_url="https://cdn.example.com/v.mp4")
    from urllib.parse import parse_qs
    sent = parse_qs(route.calls.last.request.content.decode())
    assert "mode" not in sent
    assert "preserve_speech" not in sent


@respx.mock
async def test_video_to_music_preserve_speech_path_mode_multipart(
    monkeypatch, output_dir, tmp_path
):
    # Local-file (multipart) submission must also carry mode/preserve_speech
    # as form fields alongside the uploaded video.
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
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"FAKE-MP4")
    submit = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-vocals-2", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-vocals-2").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-vocals-2", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
        })
    )
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"a")
    )
    api._reset_services_cache()
    result = await api.video_to_music(video_path=str(video), preserve_speech=True)
    sent = submit.calls.last.request
    assert sent.headers["content-type"].startswith("multipart/form-data")
    assert b"FAKE-MP4" in sent.content
    assert b'name="mode"' in sent.content and b"async" in sent.content
    assert b'name="preserve_speech"' in sent.content
    assert len(result) == 1
    assert (output_dir / "music-t-vocals.m4a").exists()


@respx.mock
async def test_video_to_music_preserve_speech_saves_audio_vocals_and_mux(
    monkeypatch, output_dir
):
    # Full succeeded envelope with multi-stream audio, a single vocals stem,
    # and multi-stream mux — must save all of them under the documented
    # naming convention and label each in the returned TextContent.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-vocals-3", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-vocals-3").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-vocals-3", "type": "video_to_music",
            "status": "succeeded",
            "audio": [
                {"stream_index": 0, "url": "https://r2.test/a0.m4a",
                 "content_type": "audio/mp4", "file_size": 2},
                {"stream_index": 1, "url": "https://r2.test/a1.m4a",
                 "content_type": "audio/mp4", "file_size": 2},
            ],
            "vocals": {
                "url": "https://r2.test/vocals.m4a",
                "content_type": "audio/mp4", "file_size": 3,
            },
            "mux": [
                {"stream_index": 0, "url": "https://r2.test/mux0.m4a",
                 "content_type": "audio/mp4", "file_size": 4},
                {"stream_index": 1, "url": "https://r2.test/mux1.m4a",
                 "content_type": "audio/mp4", "file_size": 4},
            ],
            "title": {"title": "Beat Drop", "summary": "s", "display_tags": []},
            "duration_seconds": 92.5,
        })
    )
    respx.get("https://r2.test/a0.m4a").mock(return_value=httpx.Response(200, content=b"a0"))
    respx.get("https://r2.test/a1.m4a").mock(return_value=httpx.Response(200, content=b"a1"))
    respx.get("https://r2.test/vocals.m4a").mock(return_value=httpx.Response(200, content=b"voc"))
    respx.get("https://r2.test/mux0.m4a").mock(return_value=httpx.Response(200, content=b"mx0"))
    respx.get("https://r2.test/mux1.m4a").mock(return_value=httpx.Response(200, content=b"mx1"))

    result = await api.video_to_music(
        video_url="https://cdn.example.com/v.mp4",
        prompt="Beat Drop",
        preserve_speech=True,
    )

    assert (output_dir / "beat-drop-0.m4a").read_bytes() == b"a0"
    assert (output_dir / "beat-drop-1.m4a").read_bytes() == b"a1"
    assert (output_dir / "beat-drop-vocals.m4a").read_bytes() == b"voc"
    assert (output_dir / "beat-drop-mux-0.m4a").read_bytes() == b"mx0"
    assert (output_dir / "beat-drop-mux-1.m4a").read_bytes() == b"mx1"

    assert len(result) == 5
    texts = [t.text for t in result]
    assert any("music audio" in t and "beat-drop-0.m4a" in t for t in texts)
    assert any("music audio" in t and "beat-drop-1.m4a" in t for t in texts)
    assert any("vocals" in t.lower() and "beat-drop-vocals.m4a" in t for t in texts)
    mux_texts = [t for t in texts if "beat-drop-mux-0.m4a" in t or "beat-drop-mux-1.m4a" in t]
    assert len(mux_texts) == 2
    assert all("mux" in t.lower() and "ready to use" in t.lower() for t in mux_texts)


@respx.mock
async def test_video_to_music_preserve_speech_single_stream_no_suffix(
    monkeypatch, output_dir
):
    # A single audio stream / single mux stream must use the unsuffixed
    # {base}.m4a / {base}-mux.m4a naming (matches the streaming convention's
    # num_streams == 1 case).
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-vocals-4", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-vocals-4").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-vocals-4", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
            "vocals": {
                "url": "https://r2.test/vocals.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            },
            "mux": [{
                "stream_index": 0, "url": "https://r2.test/mux.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
        })
    )
    respx.get("https://r2.test/a.m4a").mock(return_value=httpx.Response(200, content=b"a"))
    respx.get("https://r2.test/vocals.m4a").mock(return_value=httpx.Response(200, content=b"v"))
    respx.get("https://r2.test/mux.m4a").mock(return_value=httpx.Response(200, content=b"m"))

    await api.video_to_music(
        video_url="https://cdn.example.com/v.mp4", preserve_speech=True
    )
    assert (output_dir / "music-t-vocals.m4a").read_bytes() == b"a"
    assert (output_dir / "music-t-vocals-vocals.m4a").read_bytes() == b"v"
    assert (output_dir / "music-t-vocals-mux.m4a").read_bytes() == b"m"


@respx.mock
async def test_video_to_music_preserve_speech_task_failed(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-vocals-5", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-vocals-5").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-vocals-5", "status": "failed",
            "error": {"code": "GENERATION_FAILED", "message": "boom"},
            "refunded": True,
        })
    )
    with pytest.raises(Exception, match="boom"):
        await api.video_to_music(
            video_url="https://cdn.example.com/v.mp4", preserve_speech=True
        )


@respx.mock
async def test_video_to_music_preserve_speech_no_audio_raises(monkeypatch, output_dir):
    # Contract says `audio` is ALWAYS a list for async v2m — an empty/missing
    # list is a backend contract violation and must raise clearly rather
    # than silently report zero files saved.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-vocals-6", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-vocals-6").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-vocals-6", "status": "succeeded", "audio": [],
        })
    )
    with pytest.raises(Exception, match="no audio artifact"):
        await api.video_to_music(
            video_url="https://cdn.example.com/v.mp4", preserve_speech=True
        )


@respx.mock
async def test_video_to_music_ducking_uses_async_path(monkeypatch, output_dir):
    # ducking=False must route through the task submit+poll path (mode
    # async), forwarding ducking as a form field, even though
    # preserve_speech is unset.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-duck-1", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-duck-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-duck-1", "type": "video_to_music", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
        })
    )
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"a")
    )
    await api.video_to_music(
        video_url="https://cdn.example.com/v.mp4", ducking=False
    )
    from urllib.parse import parse_qs
    sent = parse_qs(submit.calls.last.request.content.decode())
    assert sent["mode"] == ["async"]
    assert sent["ducking"] == ["false"]
    assert "preserve_speech" not in sent


@respx.mock
async def test_video_to_music_ducking_unset_omits_field(monkeypatch, output_dir):
    # ducking left unset must NOT be sent at all, so the backend's default-off
    # behavior applies — sending "ducking" here would force a value.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-duck-2", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-duck-2").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-duck-2", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a2.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
        })
    )
    respx.get("https://r2.test/a2.m4a").mock(
        return_value=httpx.Response(200, content=b"a")
    )
    await api.video_to_music(
        video_url="https://cdn.example.com/v.mp4", preserve_speech=True
    )
    from urllib.parse import parse_qs
    sent = parse_qs(submit.calls.last.request.content.decode())
    assert "ducking" not in sent
    assert sent["preserve_speech"] == ["true"]


@respx.mock
async def test_video_to_music_output_format_wav_uses_async_path(monkeypatch, output_dir):
    # output_format="wav" alone (no preserve_speech/ducking) must also
    # trigger the async submit+poll path.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-wav-1", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-wav-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-wav-1", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a3.wav",
                "content_type": "audio/wav", "file_size": 1,
            }],
        })
    )
    respx.get("https://r2.test/a3.wav").mock(
        return_value=httpx.Response(200, content=b"w")
    )
    await api.video_to_music(
        video_url="https://cdn.example.com/v.mp4", output_format="wav"
    )
    from urllib.parse import parse_qs
    sent = parse_qs(submit.calls.last.request.content.decode())
    assert sent["mode"] == ["async"]
    assert sent["output_format"] == ["wav"]


@pytest.mark.parametrize("bad", [0, -1, 11])
async def test_video_to_music_rejects_out_of_range_variants_num(output_dir, bad):
    from sonilo_mcp.api import video_to_music
    with pytest.raises(Exception, match="variants_num must be between 1 and 10"):
        await video_to_music(
            video_url="https://example.com/v.mp4", variants_num=bad
        )


@respx.mock
async def test_video_to_music_variants_num_forces_async_and_sends_field(
    monkeypatch, output_dir
):
    # variants_num > 1 alone (no preserve_speech/ducking/output_format) must
    # also trigger the async submit+poll path, mirroring output_format="wav".
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-vn-1", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-vn-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-vn-1", "status": "succeeded",
            "audio": [
                {"stream_index": 0, "url": "https://r2.test/vm0.m4a",
                 "content_type": "audio/mp4", "file_size": 1},
                {"stream_index": 1, "url": "https://r2.test/vm1.m4a",
                 "content_type": "audio/mp4", "file_size": 1},
            ],
        })
    )
    respx.get("https://r2.test/vm0.m4a").mock(return_value=httpx.Response(200, content=b"m0"))
    respx.get("https://r2.test/vm1.m4a").mock(return_value=httpx.Response(200, content=b"m1"))

    result = await api.video_to_music(
        video_url="https://cdn.example.com/v.mp4", prompt="Two Takes", variants_num=2
    )
    from urllib.parse import parse_qs
    sent = parse_qs(submit.calls.last.request.content.decode())
    assert sent["mode"] == ["async"]
    assert sent["variants_num"] == ["2"]
    assert len(result) == 2
    assert (output_dir / "two-takes-0.m4a").read_bytes() == b"m0"
    assert (output_dir / "two-takes-1.m4a").read_bytes() == b"m1"


@pytest.mark.parametrize("bad", [-0.1, 1.5])
async def test_video_to_music_rejects_out_of_range_prompt_influence(output_dir, bad):
    from sonilo_mcp.api import video_to_music
    with pytest.raises(Exception, match="prompt_influence must be between 0 and 1"):
        await video_to_music(
            video_url="https://example.com/v.mp4", prompt_influence=bad
        )


@respx.mock
async def test_video_to_music_prompt_influence_streams_and_sends_field(
    monkeypatch, output_dir
):
    # prompt_influence is an upstream generation param the backend accepts
    # on the STREAM path too — unlike preserve_speech/ducking/output_format/
    # variants_num>1 it must NOT force mode=async, and the value goes out as
    # a form field.
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
    from urllib.parse import parse_qs
    await video_to_music(
        video_url="https://cdn.example.com/v.mp4", prompt_influence=0.8
    )
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["prompt_influence"] == ["0.8"]
    assert "mode" not in sent


@respx.mock
async def test_video_to_music_prompt_influence_zero_is_sent(
    monkeypatch, output_dir
):
    # 0.0 is a meaningful value (loosest adherence) — the `is not None`
    # gate must put it on the wire; a truthiness gate would drop it and
    # silently give the caller the API's 0.5 default instead.
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
    from urllib.parse import parse_qs
    await video_to_music(
        video_url="https://cdn.example.com/v.mp4", prompt_influence=0.0
    )
    sent = parse_qs(route.calls.last.request.content.decode())
    assert sent["prompt_influence"] == ["0.0"]


@respx.mock
async def test_video_to_music_prompt_influence_unset_omits_field(
    monkeypatch, output_dir
):
    # Left unset, the field must not be sent at all — the API's own 0.5
    # default is the long-standing behavior and stays the backend's call.
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
    await video_to_music(video_url="https://cdn.example.com/v.mp4")
    assert b"prompt_influence" not in route.calls.last.request.content


@respx.mock
async def test_video_to_music_prompt_influence_rides_along_on_async_path(
    monkeypatch, output_dir
):
    # When something else (here variants_num) forces the async submit+poll
    # path, prompt_influence still goes out on the same form.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-pi-1", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-pi-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-pi-1", "status": "succeeded",
            "audio": [
                {"stream_index": 0, "url": "https://r2.test/pi0.m4a",
                 "content_type": "audio/mp4", "file_size": 1},
                {"stream_index": 1, "url": "https://r2.test/pi1.m4a",
                 "content_type": "audio/mp4", "file_size": 1},
            ],
        })
    )
    respx.get("https://r2.test/pi0.m4a").mock(return_value=httpx.Response(200, content=b"a"))
    respx.get("https://r2.test/pi1.m4a").mock(return_value=httpx.Response(200, content=b"b"))
    await api.video_to_music(
        video_url="https://cdn.example.com/v.mp4", variants_num=2,
        prompt_influence=0.25,
    )
    from urllib.parse import parse_qs
    sent = parse_qs(submit.calls.last.request.content.decode())
    assert sent["mode"] == ["async"]
    assert sent["prompt_influence"] == ["0.25"]


@respx.mock
async def test_video_to_music_ducked_stems_saved(monkeypatch, output_dir):
    # A `ducked` list on the async result must be saved alongside audio,
    # each labeled distinctly (Task C1's plumbing, exercised end-to-end
    # through the tool).
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "t-duck-3", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/t-duck-3").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-duck-3", "type": "video_to_music", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a4.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
            "ducked": [{
                "stream_index": 0, "url": "https://r2.test/d4.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
        })
    )
    respx.get("https://r2.test/a4.m4a").mock(return_value=httpx.Response(200, content=b"a"))
    respx.get("https://r2.test/d4.m4a").mock(return_value=httpx.Response(200, content=b"d"))
    result = await api.video_to_music(
        video_url="https://cdn.example.com/v.mp4", prompt="Duck Test", ducking=True
    )
    assert (output_dir / "duck-test.m4a").read_bytes() == b"a"
    assert (output_dir / "duck-test-ducked.m4a").read_bytes() == b"d"
    texts = [t.text for t in result]
    assert any("ducked" in t.lower() and "duck-test-ducked.m4a" in t for t in texts)


def _stem_asset(name: str) -> dict:
    return {
        "url": f"https://r2.test/{name}.m4a",
        "content_type": "audio/mp4",
        "file_size": 1,
    }


def _mock_stem_downloads() -> None:
    for name in ("drums", "bass", "vocals", "other"):
        respx.get(f"https://r2.test/{name}.m4a").mock(
            return_value=httpx.Response(200, content=name.encode())
        )


@respx.mock
async def test_text_to_music_stems_forces_async_and_saves_stem_files(
    monkeypatch, output_dir
):
    # stems=True requires mode=async on the backend (separation runs at
    # finalize time) — must be sent automatically, same as
    # output_format='wav', together with the stems form field.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "st-1", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/st-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "st-1", "type": "text_to_music", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
            "stems": [{
                "stream_index": 0,
                "drums": _stem_asset("drums"), "bass": _stem_asset("bass"),
                "vocals": _stem_asset("vocals"), "other": _stem_asset("other"),
            }],
        })
    )
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"a")
    )
    _mock_stem_downloads()

    result = await api.text_to_music(prompt="lofi beat", duration=20, stems=True)

    from urllib.parse import parse_qs
    sent = parse_qs(submit.calls.last.request.content.decode())
    assert sent["mode"] == ["async"]
    assert sent["stems"] == ["true"]
    assert (output_dir / "lofi-beat.m4a").read_bytes() == b"a"
    for name in ("drums", "bass", "vocals", "other"):
        assert (
            output_dir / f"lofi-beat-stems-{name}.m4a"
        ).read_bytes() == name.encode()
    texts = [t.text for t in result]
    for name in ("drums", "bass", "vocals", "other"):
        assert any(
            f"stem — {name}" in t and f"lofi-beat-stems-{name}.m4a" in t
            for t in texts
        )


@respx.mock
async def test_text_to_music_stems_unset_still_streams(monkeypatch, output_dir):
    # Default stems=False must keep streaming behavior UNCHANGED — no
    # mode/stems fields sent, no task_id round trip.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    ndjson = _ndjson_bytes([
        {"type": "audio_chunk", "stream_index": 0, "num_streams": 1,
         "data": base64.b64encode(b"x").decode()},
        {"type": "complete"},
    ])
    route = respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(200, content=ndjson)
    )
    from sonilo_mcp.api import text_to_music
    await text_to_music(prompt="lofi", duration=10)
    from urllib.parse import parse_qs
    sent = parse_qs(route.calls.last.request.content.decode())
    assert "mode" not in sent
    assert "stems" not in sent


@respx.mock
async def test_video_to_music_stems_url_mode_submits_async_fields(
    monkeypatch, output_dir
):
    # stems=True must route through the task submit+poll path (mode async),
    # sending both mode and stems as form fields, instead of the plain
    # streaming POST — same as preserve_speech.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "st-2", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/st-2").mock(
        return_value=httpx.Response(200, json={
            "task_id": "st-2", "type": "video_to_music", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
            "stems": [{
                "stream_index": 0,
                "drums": _stem_asset("drums"), "bass": _stem_asset("bass"),
                "vocals": _stem_asset("vocals"), "other": _stem_asset("other"),
            }],
        })
    )
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"a")
    )
    _mock_stem_downloads()

    result = await api.video_to_music(
        video_url="https://cdn.example.com/v.mp4", prompt="Score", stems=True
    )

    from urllib.parse import parse_qs
    sent = parse_qs(submit.calls.last.request.content.decode())
    assert sent["mode"] == ["async"]
    assert sent["stems"] == ["true"]
    assert (output_dir / "score.m4a").read_bytes() == b"a"
    for name in ("drums", "bass", "vocals", "other"):
        assert (
            output_dir / f"score-stems-{name}.m4a"
        ).read_bytes() == name.encode()
    texts = [t.text for t in result]
    assert any("stem — drums" in t for t in texts)


@respx.mock
async def test_video_to_music_stems_unset_still_streams(monkeypatch, output_dir):
    # Default stems=False must keep streaming behavior UNCHANGED — no
    # mode/stems fields sent, no task_id round trip.
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
    await video_to_music(video_url="https://cdn.example.com/v.mp4")
    from urllib.parse import parse_qs
    sent = parse_qs(route.calls.last.request.content.decode())
    assert "mode" not in sent
    assert "stems" not in sent


@respx.mock
async def test_video_to_music_stems_path_mode_multipart(
    monkeypatch, output_dir, tmp_path
):
    # Local-file (multipart) submission must also carry mode/stems as form
    # fields alongside the uploaded video.
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
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"FAKE-MP4")
    submit = respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "st-3", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/st-3").mock(
        return_value=httpx.Response(200, json={
            "task_id": "st-3", "type": "video_to_music", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
        })
    )
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"a")
    )
    api._reset_services_cache()
    result = await api.video_to_music(video_path=str(video), stems=True)
    sent = submit.calls.last.request
    assert sent.headers["content-type"].startswith("multipart/form-data")
    assert b"FAKE-MP4" in sent.content
    assert b'name="mode"' in sent.content and b"async" in sent.content
    assert b'name="stems"' in sent.content
    assert len(result) == 1
    assert (output_dir / "music-st-3.m4a").exists()


@respx.mock
async def test_text_to_music_stems_polls_for_at_least_forty_minutes(
    monkeypatch, output_dir
):
    """TIME_OUT_SECONDS defaults to 600, but the backend gives the free
    separation alone up to 1800s on top of the generation. Giving up at 10
    minutes would routinely abandon a result the caller already paid for —
    same rationale as dubbing's two-hour floor."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    monkeypatch.setenv("TIME_OUT_SECONDS", "600")
    from sonilo_mcp import api
    respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "st-4", "status": "processing"}
        )
    )
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"a")
    )
    seen: dict = {}

    async def fake_poll(task_id, timeout):
        seen["timeout"] = timeout
        return {
            "task_id": task_id, "type": "text_to_music", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
        }

    monkeypatch.setattr(api, "_poll_task", fake_poll)
    await api.text_to_music(prompt="lofi", duration=10, stems=True)
    assert seen["timeout"] == 2400.0


@respx.mock
async def test_video_to_music_stems_polls_for_at_least_forty_minutes(
    monkeypatch, output_dir
):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    monkeypatch.setenv("TIME_OUT_SECONDS", "600")
    from sonilo_mcp import api
    respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "st-5", "status": "processing"}
        )
    )
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"a")
    )
    seen: dict = {}

    async def fake_poll(task_id, timeout):
        seen["timeout"] = timeout
        return {
            "task_id": task_id, "type": "video_to_music", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
        }

    monkeypatch.setattr(api, "_poll_task", fake_poll)
    await api.video_to_music(
        video_url="https://cdn.example.com/v.mp4", stems=True
    )
    assert seen["timeout"] == 2400.0


@respx.mock
async def test_text_to_music_stems_honours_a_larger_operator_timeout(
    monkeypatch, output_dir
):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    monkeypatch.setenv("TIME_OUT_SECONDS", "3600")
    from sonilo_mcp import api
    respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "st-6", "status": "processing"}
        )
    )
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"a")
    )
    seen: dict = {}

    async def fake_poll(task_id, timeout):
        seen["timeout"] = timeout
        return {
            "task_id": task_id, "type": "text_to_music", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
        }

    monkeypatch.setattr(api, "_poll_task", fake_poll)
    await api.text_to_music(prompt="lofi", duration=10, stems=True)
    assert seen["timeout"] == 3600.0


@respx.mock
async def test_video_to_music_stems_without_floor_when_unset(
    monkeypatch, output_dir
):
    # A stems-less async call (here: ducking) must keep the plain operator
    # timeout — the 40-minute floor is a stems-only concession.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    monkeypatch.setenv("TIME_OUT_SECONDS", "600")
    from sonilo_mcp import api
    respx.post("https://api.test.local/v1/video-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "st-7", "status": "processing"}
        )
    )
    respx.get("https://r2.test/a.m4a").mock(
        return_value=httpx.Response(200, content=b"a")
    )
    seen: dict = {}

    async def fake_poll(task_id, timeout):
        seen["timeout"] = timeout
        return {
            "task_id": task_id, "type": "video_to_music", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
        }

    monkeypatch.setattr(api, "_poll_task", fake_poll)
    await api.video_to_music(
        video_url="https://cdn.example.com/v.mp4", ducking=True
    )
    assert seen["timeout"] == 600.0


@respx.mock
async def test_text_to_music_output_format_wav_uses_async_path(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "tm-1", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/tm-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "tm-1", "type": "text_to_music", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/tm.wav",
                "content_type": "audio/wav", "file_size": 1,
            }],
        })
    )
    respx.get("https://r2.test/tm.wav").mock(
        return_value=httpx.Response(200, content=b"w")
    )
    result = await api.text_to_music(prompt="lofi", duration=10, output_format="wav")
    from urllib.parse import parse_qs
    sent = parse_qs(submit.calls.last.request.content.decode())
    assert sent["mode"] == ["async"]
    assert sent["output_format"] == ["wav"]
    assert sent["prompt"] == ["lofi"]
    assert sent["duration"] == ["10"]
    assert len(result) == 1
    assert (output_dir / "lofi.wav").read_bytes() == b"w"


@respx.mock
async def test_text_to_music_no_output_format_still_streams(monkeypatch, output_dir):
    # Default (no output_format) must keep streaming behavior UNCHANGED —
    # no mode/output_format fields sent, no task_id round trip.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    ndjson = _ndjson_bytes([
        {"type": "audio_chunk", "stream_index": 0, "num_streams": 1,
         "data": base64.b64encode(b"x").decode()},
        {"type": "complete"},
    ])
    route = respx.post("https://api.test.local/v1/text-to-music").mock(
        return_value=httpx.Response(200, content=ndjson)
    )
    from sonilo_mcp.api import text_to_music
    await text_to_music(prompt="lofi", duration=10)
    from urllib.parse import parse_qs
    sent = parse_qs(route.calls.last.request.content.decode())
    assert "mode" not in sent
    assert "output_format" not in sent


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


def test_describe_exc_empty_message():
    from sonilo_mcp.api import _describe_exc
    # httpx.ReadError (and several other transport errors) stringify to ''
    # — falling back to the class name avoids a hole in user-facing messages.
    assert _describe_exc(httpx.ReadError("")) == "ReadError"
    # A normal exception with a real message is passed through unchanged.
    assert _describe_exc(ValueError("bad input")) == "bad input"


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
        return_value=httpx.Response(
            422,
            json={
                "code": "validation_error",
                "message": "duration too long",
                "errors": [],
            },
        )
    )
    from sonilo_mcp.api import _post_task_submit
    with pytest.raises(Exception, match="duration too long"):
        await _post_task_submit("/v1/text-to-sfx", data={"prompt": "x"})


@respx.mock
async def test_post_task_submit_read_error_message(monkeypatch):
    # Regression: httpx.ReadError (and other transport errors) can stringify
    # to '', which used to leave a hole in the message ("failed: . Verify
    # ..."). It's also a common, retryable failure mid-upload — the message
    # must say nothing was charged and that the caller should retry.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.post("https://api.test.local/v1/text-to-sfx").mock(
        side_effect=httpx.ReadError("")
    )
    from sonilo_mcp.api import _post_task_submit
    with pytest.raises(Exception) as exc:
        await _post_task_submit("/v1/text-to-sfx", data={"prompt": "x"})
    message = str(exc.value)
    assert "ReadError" in message
    assert "nothing was charged" in message.lower() or "not charged" in message.lower() or "not been charged" in message.lower()
    assert "retry" in message.lower()
    assert ": ." not in message
    assert "failed: ." not in message


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
async def test_post_task_submit_non_dict_body(monkeypatch):
    # Regression: a 202 with a JSON body that parses but isn't a dict (e.g.
    # a list) must not crash with a bare AttributeError from `.get` — it
    # should fall into the same clear "no task_id" error as an unparseable
    # or task_id-less body, since no task_id ever existed either way.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.post("https://api.test.local/v1/text-to-sfx").mock(
        return_value=httpx.Response(202, json=[1, 2, 3])
    )
    from sonilo_mcp.api import _post_task_submit
    with pytest.raises(Exception, match="task_id") as exc:
        await _post_task_submit("/v1/text-to-sfx", data={"prompt": "x"})
    assert not isinstance(exc.value, AttributeError)


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
            httpx.Response(
                402,
                json={"code": "account_suspended", "message": "Account is suspended"},
            ),
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
            httpx.Response(
                401, json={"code": "unauthorized", "message": "Invalid API key"}
            ),
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
async def test_poll_task_non_dict_body_mentions_task_id(monkeypatch):
    # A 200 response whose JSON body isn't a dict (e.g. a bare list) must
    # not crash with a bare AttributeError from body.get(...) — the task
    # was already charged, so the raised message must carry the task_id
    # and the get_sfx_task recovery call.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-nondict").mock(
        return_value=httpx.Response(200, json=[1, 2, 3])
    )
    from sonilo_mcp.api import _poll_task
    with pytest.raises(Exception) as exc:
        await _poll_task("t-nondict", timeout_seconds=600)
    msg = str(exc.value)
    assert "t-nondict" in msg
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


def test_ext_from_content_type_non_string():
    # A truthy non-string content_type (a backend bug) must not crash with
    # an AttributeError from `.lower()` — fall back to the unknown default.
    from sonilo_mcp.api import _ext_from_content_type
    assert _ext_from_content_type(123) == ".m4a"
    assert _ext_from_content_type(["x"]) == ".m4a"


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


def test_normalize_task_envelope_audio_output():
    # Ducking's flat envelope for an audio voice input becomes the SFX
    # audio shape, with a .wav-implying content_type (ducking always
    # renders wav; the envelope carries no content_type of its own).
    from sonilo_mcp.api import _normalize_task_envelope
    body = {
        "task_id": "d-1", "status": "succeeded", "type": "audio_ducking",
        "output_url": "https://r2.test/ducked.wav", "output_type": "audio",
    }
    out, is_ducking = _normalize_task_envelope(body)
    assert is_ducking is True
    assert out["audio"] == {
        "url": "https://r2.test/ducked.wav", "content_type": "audio/wav"
    }
    assert "video" not in out
    # The input body must not be mutated in place.
    assert "audio" not in body


def test_normalize_task_envelope_video_output():
    # A video voice input yields ONE artifact: the re-muxed mp4. It maps to
    # the video slot, and there is deliberately no audio slot — the ducked
    # audio is inside the mp4.
    from sonilo_mcp.api import _normalize_task_envelope
    body = {
        "task_id": "d-2", "status": "succeeded", "type": "audio_ducking",
        "output_url": "https://r2.test/ducked.mp4", "output_type": "video",
    }
    out, is_ducking = _normalize_task_envelope(body)
    # The flag is what earns this envelope its no-audio exemption downstream.
    assert is_ducking is True
    assert out["video"] == {
        "url": "https://r2.test/ducked.mp4", "content_type": "video/mp4"
    }
    assert "audio" not in out


def test_normalize_task_envelope_passes_sfx_body_through():
    from sonilo_mcp.api import _normalize_task_envelope
    body = {
        "status": "succeeded",
        "audio": {"url": "https://r2.test/a.m4a", "content_type": "audio/mp4"},
    }
    # is_ducking is False, which is what holds SFX bodies to the audio
    # requirement in _save_task_artifacts.
    assert _normalize_task_envelope(body) == (body, False)


@pytest.mark.parametrize(
    "output_type",
    [None, "", "audio/wav", "AUDIO", "image", 7, ["audio"]],
)
def test_normalize_task_envelope_rejects_unknown_output_type(output_type):
    # An unrecognized (or missing, or non-string) output_type alongside a
    # valid output_url must NOT be normalized. Defaulting it to "audio" would
    # download the artifact, write it with a fabricated .wav extension and
    # report success — silently mislabeling a paid result. It must fall
    # through as a non-ducking body instead, so _save_task_artifacts raises
    # its charged-and-recoverable error carrying the task_id.
    from sonilo_mcp.api import _normalize_task_envelope
    body = {
        "task_id": "d-6", "status": "succeeded",
        "output_url": "https://r2.test/ducked.bin",
    }
    if output_type is not None:
        body["output_type"] = output_type
    out, is_ducking = _normalize_task_envelope(body)
    assert is_ducking is False
    assert out == body
    assert "audio" not in out
    assert "video" not in out


def test_is_video_to_video_envelope():
    from sonilo_mcp.api import _is_video_to_video_envelope
    assert _is_video_to_video_envelope(
        {"type": "video_to_video_music", "status": "succeeded", "video": {"url": "u"}}
    )
    assert _is_video_to_video_envelope(
        {"type": "video_to_video_sfx", "status": "succeeded", "video": {"url": "u"}}
    )
    # Ducking's normalized envelope: video present, no audio, but its
    # original output_url survives normalization — must not be mistaken
    # for video-to-video.
    assert not _is_video_to_video_envelope(
        {"type": "audio_ducking", "status": "succeeded", "output_url": "u"}
    )
    # An explicit, different type is authoritative — must never be
    # overridden by shape-sniffing, even when the shape matches (video
    # present, no audio, no output_url).
    assert not _is_video_to_video_envelope(
        {"type": "video_to_sfx", "status": "succeeded", "video": {"url": "u"}}
    )
    # No `type` at all (defensive fallback for a malformed/older body):
    # shape-sniffing kicks in.
    assert _is_video_to_video_envelope({"status": "succeeded", "video": {"url": "u"}})
    assert not _is_video_to_video_envelope(
        {"status": "succeeded", "video": {"url": "u"}, "audio": {"url": "a"}}
    )


@respx.mock
async def test_save_task_artifacts_saves_video_only(monkeypatch, tmp_path):
    # A video-to-video result (`{"video": {...}}`, no `audio`) must be saved
    # like the ducking video-only case — no "no audio artifact" error.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/v2v.mp4").mock(
        return_value=httpx.Response(200, content=b"v2v-bytes")
    )
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "v1", "type": "video_to_video_music", "status": "succeeded",
        "video": {"url": "https://r2.test/v2v.mp4", "content_type": "video/mp4", "file_size": 9},
    }
    result = await _save_task_artifacts(body, tmp_path, "v2v-scene", "v1")
    assert len(result) == 1
    expected = tmp_path / "v2v-scene.mp4"
    assert expected.read_bytes() == b"v2v-bytes"
    assert str(expected) in result[0].text


@respx.mock
async def test_save_music_task_artifacts_saves_ducked_stems(monkeypatch, tmp_path):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/a.m4a").mock(return_value=httpx.Response(200, content=b"a"))
    respx.get("https://r2.test/d0.m4a").mock(return_value=httpx.Response(200, content=b"d0"))
    respx.get("https://r2.test/d1.m4a").mock(return_value=httpx.Response(200, content=b"d1"))
    from sonilo_mcp.api import _save_music_task_artifacts
    body = {
        "task_id": "m-duck-1", "type": "video_to_music", "status": "succeeded",
        "audio": [
            {"stream_index": 0, "url": "https://r2.test/a.m4a",
             "content_type": "audio/mp4", "file_size": 1},
        ],
        "ducked": [
            {"stream_index": 0, "url": "https://r2.test/d0.m4a",
             "content_type": "audio/mp4", "file_size": 2},
            {"stream_index": 1, "url": "https://r2.test/d1.m4a",
             "content_type": "audio/mp4", "file_size": 2},
        ],
    }
    result = await _save_music_task_artifacts(body, tmp_path, "score", "m-duck-1")
    assert (tmp_path / "score-ducked-0.m4a").read_bytes() == b"d0"
    assert (tmp_path / "score-ducked-1.m4a").read_bytes() == b"d1"
    texts = [t.text for t in result]
    ducked_texts = [t for t in texts if "score-ducked-0.m4a" in t or "score-ducked-1.m4a" in t]
    assert len(ducked_texts) == 2
    assert all("ducked" in t.lower() for t in ducked_texts)


def _stems_entry(idx: int, prefix: str) -> dict:
    return {
        "stream_index": idx,
        **{
            name: {
                "url": f"https://r2.test/{prefix}-{name}.m4a",
                "content_type": "audio/mp4",
                "file_size": 1,
            }
            for name in ("drums", "bass", "vocals", "other")
        },
    }


def _mock_stems_entry_downloads(prefix: str) -> None:
    for name in ("drums", "bass", "vocals", "other"):
        respx.get(f"https://r2.test/{prefix}-{name}.m4a").mock(
            return_value=httpx.Response(200, content=f"{prefix}-{name}".encode())
        )


@respx.mock
async def test_save_music_task_artifacts_saves_stems_per_variant(
    monkeypatch, tmp_path
):
    # Multi-variant audio with stems for every stream: each stem file must
    # carry its variant index (score-stems-<idx>-<name>) and its own label.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/a0.m4a").mock(return_value=httpx.Response(200, content=b"a0"))
    respx.get("https://r2.test/a1.m4a").mock(return_value=httpx.Response(200, content=b"a1"))
    _mock_stems_entry_downloads("s0")
    _mock_stems_entry_downloads("s1")
    from sonilo_mcp.api import _save_music_task_artifacts
    body = {
        "task_id": "m-stems-1", "type": "video_to_music", "status": "succeeded",
        "audio": [
            {"stream_index": 0, "url": "https://r2.test/a0.m4a",
             "content_type": "audio/mp4", "file_size": 2},
            {"stream_index": 1, "url": "https://r2.test/a1.m4a",
             "content_type": "audio/mp4", "file_size": 2},
        ],
        "stems": [_stems_entry(0, "s0"), _stems_entry(1, "s1")],
    }
    result = await _save_music_task_artifacts(body, tmp_path, "score", "m-stems-1")
    for idx, prefix in ((0, "s0"), (1, "s1")):
        for name in ("drums", "bass", "vocals", "other"):
            assert (
                tmp_path / f"score-stems-{idx}-{name}.m4a"
            ).read_bytes() == f"{prefix}-{name}".encode()
    texts = [t.text for t in result]
    assert any("stem — bass" in t and "score-stems-1-bass.m4a" in t for t in texts)
    # No stems_error on a fully-successful separation — no note either.
    assert not any("separation" in t for t in texts)


@respx.mock
async def test_save_music_task_artifacts_partial_stems_keep_variant_index(
    monkeypatch, tmp_path
):
    # The stems list may be SHORTER than audio (per-variant separation
    # failures), and then arrives alongside stems_error. The surviving
    # entry must keep its variant index in the filename — the multi/single
    # decision follows the audio list, not the stems list — and the error
    # must come back as a note that does not fail the save.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/a0.m4a").mock(return_value=httpx.Response(200, content=b"a0"))
    respx.get("https://r2.test/a1.m4a").mock(return_value=httpx.Response(200, content=b"a1"))
    _mock_stems_entry_downloads("s1")
    from sonilo_mcp.api import _save_music_task_artifacts
    body = {
        "task_id": "m-stems-2", "type": "video_to_music", "status": "succeeded",
        "audio": [
            {"stream_index": 0, "url": "https://r2.test/a0.m4a",
             "content_type": "audio/mp4", "file_size": 2},
            {"stream_index": 1, "url": "https://r2.test/a1.m4a",
             "content_type": "audio/mp4", "file_size": 2},
        ],
        "stems": [_stems_entry(1, "s1")],
        "stems_error": "Stem separation failed for stream(s) 0.",
    }
    result = await _save_music_task_artifacts(body, tmp_path, "score", "m-stems-2")
    assert (tmp_path / "score-stems-1-drums.m4a").read_bytes() == b"s1-drums"
    assert not (tmp_path / "score-stems-drums.m4a").exists()
    texts = [t.text for t in result]
    note = [t for t in texts if "Stem separation failed for stream(s) 0." in t]
    assert len(note) == 1
    assert "missing extra" in note[0]
    assert "not as a failed generation" in note[0]


@respx.mock
async def test_save_music_task_artifacts_stems_error_alone_is_a_note(
    monkeypatch, tmp_path
):
    # A wholly-failed (or skipped) separation returns stems_error with no
    # stems at all. The audio is still the complete paid result: it must
    # save normally, with the error passed through as a trailing note.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/a.m4a").mock(return_value=httpx.Response(200, content=b"a"))
    from sonilo_mcp.api import _save_music_task_artifacts
    body = {
        "task_id": "m-stems-3", "type": "text_to_music", "status": "succeeded",
        "audio": [
            {"stream_index": 0, "url": "https://r2.test/a.m4a",
             "content_type": "audio/mp4", "file_size": 1},
        ],
        "stems_error": (
            "Stem separation was skipped for this task. The generated audio "
            "is unaffected."
        ),
    }
    result = await _save_music_task_artifacts(body, tmp_path, "score", "m-stems-3")
    assert (tmp_path / "score.m4a").read_bytes() == b"a"
    texts = [t.text for t in result]
    assert any(
        "Stem separation was skipped for this task." in t
        and "missing extra" in t
        for t in texts
    )


@respx.mock
async def test_save_music_task_artifacts_stems_vocals_never_collides_with_speech(
    monkeypatch, tmp_path
):
    # preserve_speech's speech stem is saved as <base>-vocals; the stems
    # vocals track must land elsewhere (<base>-stems-vocals) so a
    # preserve_speech+stems result keeps both files intact.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/a.m4a").mock(return_value=httpx.Response(200, content=b"a"))
    respx.get("https://r2.test/speech.m4a").mock(
        return_value=httpx.Response(200, content=b"speech")
    )
    _mock_stems_entry_downloads("s0")
    from sonilo_mcp.api import _save_music_task_artifacts
    body = {
        "task_id": "m-stems-4", "type": "video_to_music", "status": "succeeded",
        "audio": [
            {"stream_index": 0, "url": "https://r2.test/a.m4a",
             "content_type": "audio/mp4", "file_size": 1},
        ],
        "vocals": {"url": "https://r2.test/speech.m4a",
                   "content_type": "audio/mp4", "file_size": 6},
        "stems": [_stems_entry(0, "s0")],
    }
    await _save_music_task_artifacts(body, tmp_path, "score", "m-stems-4")
    assert (tmp_path / "score-vocals.m4a").read_bytes() == b"speech"
    assert (tmp_path / "score-stems-vocals.m4a").read_bytes() == b"s0-vocals"


@respx.mock
async def test_save_task_artifacts_unknown_output_type_raises_recoverable(
    monkeypatch, tmp_path
):
    # End-to-end consequence of the above: nothing is downloaded, nothing is
    # written, and the error keeps the task_id + get_sfx_task hint — the
    # result is still on the backend.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    download = respx.get("https://r2.test/ducked.bin").mock(
        return_value=httpx.Response(200, content=b"mystery")
    )
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "d-7", "status": "succeeded",
        "output_url": "https://r2.test/ducked.bin", "output_type": "waveform",
    }
    with pytest.raises(Exception) as exc:
        await _save_task_artifacts(body, tmp_path, "interview-ducked", "d-7")
    msg = str(exc.value)
    # get_sfx_task would re-run the same normalization and loop forever on this
    # body, so the message must instead surface the output_url + output_type so
    # the paid result can be fetched manually — while still carrying the task_id.
    assert "https://r2.test/ducked.bin" in msg
    assert "waveform" in msg
    assert "d-7" in msg
    assert download.call_count == 0
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_save_task_artifacts_ducking_audio_envelope(monkeypatch, tmp_path):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/ducked.wav").mock(
        return_value=httpx.Response(200, content=b"ducked-wav-bytes")
    )
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "d-3", "status": "succeeded",
        "output_url": "https://r2.test/ducked.wav", "output_type": "audio",
    }
    result = await _save_task_artifacts(body, tmp_path, "interview-ducked", "d-3")
    assert len(result) == 1
    expected = tmp_path / "interview-ducked.wav"
    assert expected.read_bytes() == b"ducked-wav-bytes"
    assert str(expected) in result[0].text


@respx.mock
async def test_save_task_artifacts_ducking_video_envelope(monkeypatch, tmp_path):
    # The video-only case: no audio artifact exists, and that is CORRECT —
    # it must not raise "no audio artifact was returned".
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/ducked.mp4").mock(
        return_value=httpx.Response(200, content=b"ducked-mp4-bytes")
    )
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "d-4", "status": "succeeded",
        "output_url": "https://r2.test/ducked.mp4", "output_type": "video",
    }
    result = await _save_task_artifacts(body, tmp_path, "interview-ducked", "d-4")
    assert len(result) == 1
    expected = tmp_path / "interview-ducked.mp4"
    assert expected.read_bytes() == b"ducked-mp4-bytes"
    assert str(expected) in result[0].text


@respx.mock
async def test_save_task_artifacts_no_artifacts_still_raises(monkeypatch, tmp_path):
    # A succeeded body with NEITHER artifact is still a contract violation,
    # and the message must keep the task_id + recovery call: the task was
    # already charged.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp.api import _save_task_artifacts
    body = {"task_id": "d-5", "status": "succeeded"}
    with pytest.raises(Exception) as exc:
        await _save_task_artifacts(body, tmp_path, "x", "d-5")
    msg = str(exc.value)
    assert "no audio artifact" in msg
    assert "d-5" in msg
    assert "get_sfx_task" in msg


@respx.mock
async def test_save_task_artifacts_sfx_video_without_audio_still_raises(
    monkeypatch, tmp_path
):
    # An SFX-shaped body is NOT a ducking or video-to-video envelope: it must
    # always carry an audio artifact. A video-only SFX body means the audio
    # half of a paid result went missing — downloading just the mp4 and
    # reporting success would silently lose it, so this still raises the
    # charged-and-recoverable error. (The video-only exemption belongs to
    # ducking and video-to-video alone.) `type` is set to a real, unrelated
    # task type — the backend's /v1/tasks/{id} always includes `type` — so
    # this pins that an explicit non-v2v type is never overridden by the
    # video-to-video shape-sniffing fallback (which only applies when `type`
    # is missing entirely).
    monkeypatch.setenv("SONILO_API_KEY", "k")
    route = respx.get("https://r2.test/orphan.mp4").mock(
        return_value=httpx.Response(200, content=b"mp4-bytes")
    )
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "t-orphan", "type": "video_to_sfx", "status": "succeeded",
        "video": {"url": "https://r2.test/orphan.mp4", "content_type": "video/mp4"},
    }
    with pytest.raises(Exception) as exc:
        await _save_task_artifacts(body, tmp_path, "scene", "t-orphan")
    msg = str(exc.value)
    assert "no audio artifact was returned" in msg
    assert "t-orphan" in msg
    assert 'get_sfx_task("t-orphan")' in msg
    # Nothing was downloaded or written: the guard fires before any artifact
    # work, so the user isn't handed a half-result presented as success.
    assert route.call_count == 0
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_save_task_artifacts_ducking_video_only_download_failure(
    monkeypatch, tmp_path
):
    # A video-only ducking result whose single artifact fails to download.
    # There is no audio_dest, so the message must NOT claim an audio file was
    # saved — it must be the plain charged-and-recoverable wording, carrying
    # the task_id and the get_sfx_task retry call.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    respx.get("https://r2.test/ducked-fail.mp4").mock(
        return_value=httpx.Response(500, text="boom")
    )
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "d-7", "status": "succeeded",
        "output_url": "https://r2.test/ducked-fail.mp4", "output_type": "video",
    }
    with pytest.raises(Exception) as exc:
        await _save_task_artifacts(body, tmp_path, "interview-ducked", "d-7")
    msg = str(exc.value)
    assert "d-7" in msg
    assert 'get_sfx_task("d-7")' in msg
    assert "The audio file was already saved" not in msg
    # The reserved-but-unpopulated file is cleaned up, so a retry lands on the
    # canonical path rather than an orphaning -1 suffix.
    assert list(tmp_path.iterdir()) == []


@respx.mock
async def test_save_task_artifacts_ducking_reuse_existing(monkeypatch, tmp_path):
    # Ducking envelopes carry no file_size, so reuse falls back to the
    # documented "non-empty means already downloaded" branch.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    route = respx.get("https://r2.test/ducked.wav").mock(
        return_value=httpx.Response(200, content=b"fresh")
    )
    (tmp_path / "interview-ducked.wav").write_bytes(b"already-here")
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "d-6", "status": "succeeded",
        "output_url": "https://r2.test/ducked.wav", "output_type": "audio",
    }
    result = await _save_task_artifacts(
        body, tmp_path, "interview-ducked", "d-6", reuse_existing=True
    )
    assert route.call_count == 0
    assert (tmp_path / "interview-ducked.wav").read_bytes() == b"already-here"
    assert "Already downloaded" in result[0].text


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


async def test_save_task_artifacts_refunded_string_false_is_not_refunded(tmp_path):
    # Regression: the backend may send the STRING "false" instead of the
    # boolean False. A bare truthiness check treats any non-empty string as
    # truthy, which would tell the user they were refunded when they were
    # not — the opposite of the truth, and real financial harm.
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "t-str-refund", "status": "failed",
        "error": {"code": "X", "message": "y"},
        "refunded": "false",
    }
    with pytest.raises(Exception) as exc:
        await _save_task_artifacts(body, tmp_path, "x", "t-str-refund")
    msg = str(exc.value)
    assert "has not been reversed" in msg
    assert "you were not billed" not in msg


async def test_save_task_artifacts_refunded_true_bool(tmp_path):
    # Guard against over-tightening: the literal boolean True must still
    # report the refunded branch.
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "t-bool-refund", "status": "failed",
        "error": {"code": "X", "message": "y"},
        "refunded": True,
    }
    with pytest.raises(Exception) as exc:
        await _save_task_artifacts(body, tmp_path, "x", "t-bool-refund")
    msg = str(exc.value)
    assert "you were not billed" in msg


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


async def test_save_task_artifacts_failed_non_dict_error_mentions_task_id(tmp_path):
    # Regression: a truthy non-dict "error" (e.g. a bare string) must not
    # crash with a bare AttributeError — the task was charged AND failed, so
    # the user needs the task_id, a failure code, the refund status, and the
    # get_sfx_task recovery call just like every other failure path.
    from sonilo_mcp.api import _save_task_artifacts
    body = {
        "task_id": "t-boom", "status": "failed",
        "error": "boom",
        "refunded": False,
    }
    with pytest.raises(Exception) as exc:
        await _save_task_artifacts(body, tmp_path, "x", "t-boom")
    assert not isinstance(exc.value, AttributeError)
    msg = str(exc.value)
    assert "t-boom" in msg
    assert "get_sfx_task" in msg
    assert "GENERATION_FAILED" in msg
    assert "has not" in msg  # refund line: not reversed
    assert "boom" in msg


async def test_save_task_artifacts_succeeded_without_audio(tmp_path):
    from sonilo_mcp.api import _save_task_artifacts
    body = {"task_id": "t-5", "status": "succeeded"}
    with pytest.raises(Exception, match="no audio"):
        await _save_task_artifacts(body, tmp_path, "x", "t-5")


async def test_save_task_artifacts_no_audio_mentions_task_id(tmp_path):
    # The task was already charged and succeeded by the time we get here —
    # a missing/malformed audio envelope must still carry the task_id and
    # the get_sfx_task recovery call, not just the bare fact of the defect.
    from sonilo_mcp.api import _save_task_artifacts
    body = {"task_id": "t-no-audio", "status": "succeeded"}
    with pytest.raises(Exception, match="no audio") as exc:
        await _save_task_artifacts(body, tmp_path, "x", "t-no-audio")
    msg = str(exc.value)
    assert "t-no-audio" in msg
    assert "get_sfx_task" in msg


async def test_save_task_artifacts_unexpected_status(tmp_path):
    from sonilo_mcp.api import _save_task_artifacts
    body = {"task_id": "t-6", "status": "cancelled"}
    with pytest.raises(Exception, match="Unexpected task status.*cancelled") as exc:
        await _save_task_artifacts(body, tmp_path, "x", "t-6")
    # The task was already charged, and get_sfx_task is the only recovery
    # path — the task_id and the recovery call must be in the message.
    assert "t-6" in str(exc.value)
    assert "get_sfx_task" in str(exc.value)


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
            400,
            json={
                "code": "bad_request",
                "message": "audio_format must be one of wav, mp3, aac, flac",
            },
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


def test_sfx_video_exts_are_the_fal_accepted_set():
    # video-to-sfx (fal video-to-audio) accepts exactly this set — narrower than
    # the shared _VIDEO_EXTS used by video-to-music / audio-ducking.
    from sonilo_mcp.api import _SFX_VIDEO_EXTS

    assert _SFX_VIDEO_EXTS == {".mp4", ".mov", ".webm", ".m4v", ".gif"}


async def test_video_to_sfx_rejects_avi_extension(monkeypatch, tmp_path):
    # .avi is in the shared _VIDEO_EXTS but NOT accepted by video-to-audio, so
    # video_to_sfx must reject it locally (fail fast, before any upload).
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp.api import video_to_sfx

    clip = tmp_path / "clip.avi"
    clip.write_bytes(b"x")
    with pytest.raises(Exception, match="not a recognized video format"):
        await video_to_sfx(video_path=str(clip))


@pytest.mark.parametrize("bad_url", ["file:///etc/passwd", "ftp://x/y.mp4"])
async def test_video_to_sfx_rejects_non_http_url(monkeypatch, output_dir, bad_url):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp.api import video_to_sfx
    with pytest.raises(Exception, match="http"):
        await video_to_sfx(video_url=bad_url)


@respx.mock
async def test_video_to_sfx_rejects_video_over_the_cap(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp.api import video_to_sfx
    # 500s is past the SFX cap (480). Deliberately also past the music cap
    # (360), so a regression that reverted this gate to the music constant
    # would still be caught here.
    _patch_ffprobe(monkeypatch, duration=500.0)
    submit_route = respx.post("https://api.test.local/v1/video-to-sfx").mock(
        return_value=httpx.Response(202, json={"task_id": "t-should-not-charge"})
    )
    with pytest.raises(Exception, match="exceeds the maximum"):
        await video_to_sfx(video_url="https://example.com/long.mp4")
    # Must NOT charge — the duration check must reject before the submit POST.
    assert submit_route.call_count == 0


@respx.mock
async def test_video_to_sfx_size_cap_enforced_on_actual_bytes(
    monkeypatch, output_dir, tmp_path
):
    # Same TOCTOU race as video_to_music: the stat()-based size check runs
    # before the ffprobe await, and a file swapped for a larger one during
    # that window must not bypass the cap. The authoritative check must be
    # on the bytes actually read for upload, and no charge (submit POST)
    # may happen when it fails.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    video = tmp_path / "clip.mp4"
    small = b"\x00" * 1024
    large = b"\x01" * (5 * 1024 * 1024)
    video.write_bytes(small)

    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": 2})
    )
    api._reset_services_cache()

    monkeypatch.setattr(api.shutil, "which", lambda name: "/usr/bin/ffprobe")

    async def fake_exec(*args, **kwargs):
        video.write_bytes(large)  # swap happens during the caller's await
        payload = json.dumps({"format": {"duration": "10.0"}}).encode()
        return _FakeProc(payload, returncode=0)

    monkeypatch.setattr(api.asyncio, "create_subprocess_exec", fake_exec)

    submit_route = respx.post("https://api.test.local/v1/video-to-sfx").mock(
        return_value=httpx.Response(202, json={"task_id": "t-toctou"})
    )

    with pytest.raises(Exception, match="too large"):
        await api.video_to_sfx(video_path=str(video))
    # Must NOT charge — the oversized (swapped) bytes must never be uploaded.
    assert submit_route.call_count == 0


# ---------- video-to-video ----------

@respx.mock
async def test_video_to_video_music_url_mode_submits_and_saves(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-video-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "vv-1", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/vv-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "vv-1", "type": "video_to_video_music", "status": "succeeded",
            "video": {
                "url": "https://r2.test/o.mp4", "content_type": "video/mp4",
                "file_size": 3,
            },
            "duration_seconds": 5.0,
        })
    )
    respx.get("https://r2.test/o.mp4").mock(
        return_value=httpx.Response(200, content=b"video-bytes")
    )
    result = await api.video_to_video_music(
        video_url="https://example.com/clip.mp4",
        prompt="Cinematic",
        preserve_speech=True,
    )
    sent = submit.calls.last.request
    assert b"video_url=" in sent.content
    assert b"Cinematic" in sent.content
    assert b"preserve_speech=true" in sent.content
    assert len(result) == 1
    assert (output_dir / "cinematic.mp4").read_bytes() == b"video-bytes"


@respx.mock
async def test_video_to_video_music_path_mode_uploads_multipart(
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
    submit = respx.post("https://api.test.local/v1/video-to-video-music").mock(
        return_value=httpx.Response(
            202, json={"task_id": "vv-2", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/vv-2").mock(
        return_value=httpx.Response(200, json={
            "task_id": "vv-2", "type": "video_to_video_music", "status": "succeeded",
            "video": {
                "url": "https://r2.test/o2.mp4", "content_type": "video/mp4",
                "file_size": 3,
            },
        })
    )
    respx.get("https://r2.test/o2.mp4").mock(
        return_value=httpx.Response(200, content=b"v2")
    )
    api._reset_services_cache()
    result = await api.video_to_video_music(video_path=str(video))
    sent = submit.calls.last.request
    assert sent.headers["content-type"].startswith("multipart/form-data")
    assert b"FAKE-MP4" in sent.content
    assert len(result) == 1
    # No prompt -> base name falls back to v2v-music-{task_id[:8]}.
    assert (output_dir / "v2v-music-vv-2.mp4").exists()


@pytest.mark.parametrize("bad", [0, -1, 11])
async def test_video_to_video_music_rejects_out_of_range_variants_num(output_dir, bad):
    from sonilo_mcp.api import video_to_video_music
    with pytest.raises(Exception, match="variants_num must be between 1 and 10"):
        await video_to_video_music(
            video_url="https://example.com/v.mp4", variants_num=bad
        )


@respx.mock
async def test_video_to_video_music_variants_num_sends_field_and_saves_every_variant(
    monkeypatch, output_dir
):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-video-music").mock(
        return_value=httpx.Response(202, json={"task_id": "vv-3", "status": "processing"})
    )
    # `videos` carries one entry per variant, no variant_index of its own —
    # list position IS variant order (see VideoToVideoService.transfer_outputs).
    # `video` stays as the variant-0 alias.
    respx.get("https://api.test.local/v1/tasks/vv-3").mock(
        return_value=httpx.Response(200, json={
            "task_id": "vv-3", "type": "video_to_video_music", "status": "succeeded",
            "videos": [
                {"url": "https://r2.test/vv3-0.mp4", "content_type": "video/mp4", "file_size": 3},
                {"url": "https://r2.test/vv3-1.mp4", "content_type": "video/mp4", "file_size": 3},
            ],
            "video": {"url": "https://r2.test/vv3-0.mp4", "content_type": "video/mp4", "file_size": 3},
        })
    )
    respx.get("https://r2.test/vv3-0.mp4").mock(return_value=httpx.Response(200, content=b"vid0"))
    respx.get("https://r2.test/vv3-1.mp4").mock(return_value=httpx.Response(200, content=b"vid1"))

    result = await api.video_to_video_music(
        video_url="https://example.com/clip.mp4", prompt="Two Cuts", variants_num=2
    )
    from urllib.parse import parse_qs
    sent = parse_qs(submit.calls.last.request.content.decode())
    assert sent["variants_num"] == ["2"]
    assert len(result) == 2
    assert (output_dir / "two-cuts-0.mp4").read_bytes() == b"vid0"
    assert (output_dir / "two-cuts-1.mp4").read_bytes() == b"vid1"
    texts = [t.text for t in result]
    assert any("variant 0" in t for t in texts)
    assert any("variant 1" in t for t in texts)


async def test_video_to_video_music_both_inputs_rejected(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp.api import video_to_video_music
    with pytest.raises(Exception, match="exactly one"):
        await video_to_video_music(
            video_path="/tmp/a.mp4", video_url="https://example.com/b.mp4"
        )
    with pytest.raises(Exception, match="exactly one"):
        await video_to_video_music()


@pytest.mark.parametrize("bad_url", ["file:///etc/passwd", "ftp://x/y.mp4"])
async def test_video_to_video_music_rejects_non_http_url(monkeypatch, output_dir, bad_url):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp.api import video_to_video_music
    with pytest.raises(Exception, match="http"):
        await video_to_video_music(video_url=bad_url)


@respx.mock
async def test_video_to_video_sfx_url_mode_segments_passthrough(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-video-sfx").mock(
        return_value=httpx.Response(
            202, json={"task_id": "vvs-1", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/vvs-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "vvs-1", "type": "video_to_video_sfx", "status": "succeeded",
            "video": {
                "url": "https://r2.test/sfx.mp4", "content_type": "video/mp4",
                "file_size": 3,
            },
        })
    )
    respx.get("https://r2.test/sfx.mp4").mock(
        return_value=httpx.Response(200, content=b"sfx-video")
    )
    segs = [
        {"start": 0, "end": 2, "prompt": "whoosh"},
        {"start": 2, "end": 5, "prompt": "boom"},
    ]
    result = await api.video_to_video_sfx(
        video_url="https://example.com/clip.mp4", segments=segs
    )
    from urllib.parse import parse_qs
    sent_fields = parse_qs(submit.calls.last.request.content.decode())
    assert json.loads(sent_fields["segments"][0]) == segs
    assert len(result) == 1
    # No prompt -> base name falls back to v2v-sfx-{task_id[:8]}.
    assert (output_dir / "v2v-sfx-vvs-1.mp4").read_bytes() == b"sfx-video"


@respx.mock
async def test_video_to_video_sfx_rejects_video_over_the_cap(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp.api import video_to_video_sfx
    # 500s is past the SFX cap (480), and past the music cap too so a
    # regression to the music constant would still fail here.
    _patch_ffprobe(monkeypatch, duration=500.0)
    submit_route = respx.post("https://api.test.local/v1/video-to-video-sfx").mock(
        return_value=httpx.Response(202, json={"task_id": "t-should-not-charge"})
    )
    with pytest.raises(Exception, match="exceeds the maximum"):
        await video_to_video_sfx(video_url="https://example.com/long.mp4")
    # Must NOT charge — the duration check must reject before the submit POST.
    assert submit_route.call_count == 0


async def test_video_to_video_sfx_both_inputs_rejected(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp.api import video_to_video_sfx
    with pytest.raises(Exception, match="exactly one"):
        await video_to_video_sfx(
            video_path="/tmp/a.mp4", video_url="https://example.com/b.mp4"
        )
    with pytest.raises(Exception, match="exactly one"):
        await video_to_video_sfx()


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
    assert "t-30" in result[0].text


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
async def test_get_sfx_task_unexpected_status_mentions_task_id(monkeypatch, output_dir):
    # A terminal-looking status this client doesn't recognize (e.g. a new
    # status the backend adds later, or "cancelled") must still surface the
    # task_id and the get_sfx_task recovery call — get_sfx_task is the only
    # way to recover a charged task's result.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-6").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t-6", "status": "cancelled",
        })
    )
    from sonilo_mcp.api import get_sfx_task
    with pytest.raises(Exception) as exc:
        await get_sfx_task("t-6")
    assert "t-6" in str(exc.value)
    assert "get_sfx_task" in str(exc.value)


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
        return_value=httpx.Response(
            404, json={"code": "not_found", "message": "Task not found"}
        )
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
        return_value=httpx.Response(
            404, json={"code": "not_found", "message": "Task not found"}
        )
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
        return_value=httpx.Response(
            404, json={"code": "not_found", "message": "Task not found"}
        )
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
        return_value=httpx.Response(
            402, json={"code": "account_suspended", "message": "Account is suspended"}
        )
    )
    from sonilo_mcp.api import get_sfx_task
    with pytest.raises(Exception) as exc:
        await get_sfx_task("t-402b")
    msg = str(exc.value)
    assert "t-402b" in msg
    assert "get_sfx_task" in msg


@respx.mock
async def test_get_sfx_task_non_dict_body_mentions_task_id(monkeypatch, output_dir):
    # Same non-dict-body hazard as _poll_task, but on get_sfx_task's own
    # status GET — must not crash with a bare AttributeError, and must
    # keep the task_id + recovery call in the raised message.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/t-nondict2").mock(
        return_value=httpx.Response(200, json=[1, 2, 3])
    )
    from sonilo_mcp.api import get_sfx_task
    with pytest.raises(Exception) as exc:
        await get_sfx_task("t-nondict2")
    msg = str(exc.value)
    assert "t-nondict2" in msg
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


@respx.mock
async def test_get_sfx_task_recovers_ducking_task(monkeypatch, output_dir):
    # Regression: /v1/tasks serves SFX *and* audio-ducking tasks (the
    # backend's POLLABLE_TASK_TYPES covers both), but get_sfx_task used to
    # fetch a valid ducking body and then fail with "no audio artifact was
    # returned". It must download the result instead.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/d-99").mock(
        return_value=httpx.Response(200, json={
            "task_id": "d-99", "type": "audio_ducking", "status": "succeeded",
            "output_url": "https://r2.test/ducked.wav",
            "output_type": "audio",
        })
    )
    respx.get("https://r2.test/ducked.wav").mock(
        return_value=httpx.Response(200, content=b"ducked-bytes")
    )
    from sonilo_mcp.api import get_sfx_task
    result = await get_sfx_task("d-99")
    saved = output_dir / "sfx-d-99.wav"
    assert saved.read_bytes() == b"ducked-bytes"
    assert str(saved) in result[0].text


@respx.mock
async def test_get_sfx_task_recovers_multi_variant_sound_task(monkeypatch, output_dir):
    # A timed-out video_to_sound(variants_num=2) call is recovered here too:
    # _save_task_artifacts' new outputs-list branch must fire on the
    # get_sfx_task path exactly as it does on the direct video_to_sound call.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/sd-rec-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "sd-rec-1", "type": "video_to_sound", "status": "succeeded",
            "variants_num": 2,
            "outputs": [
                {"variant_index": 0, "output_url": "https://r2.test/rec0.wav",
                 "output_type": "audio", "output_bytes": 2},
                {"variant_index": 1, "output_url": "https://r2.test/rec1.wav",
                 "output_type": "audio", "output_bytes": 2},
            ],
            "output_url": "https://r2.test/rec0.wav", "output_type": "audio",
            "output_bytes": 2,
        })
    )
    respx.get("https://r2.test/rec0.wav").mock(return_value=httpx.Response(200, content=b"r0"))
    respx.get("https://r2.test/rec1.wav").mock(return_value=httpx.Response(200, content=b"r1"))
    from sonilo_mcp.api import get_sfx_task
    result = await get_sfx_task("sd-rec-1")
    assert len(result) == 2
    assert (output_dir / "sfx-sd-rec-1-0.wav").read_bytes() == b"r0"
    assert (output_dir / "sfx-sd-rec-1-1.wav").read_bytes() == b"r1"


@respx.mock
async def test_get_sfx_task_recovers_preserve_speech_music_task(monkeypatch, output_dir):
    # /v1/tasks also serves async (preserve_speech) video-to-music tasks now.
    # Their envelope shapes `audio` as a LIST plus optional `vocals`/`mux` —
    # get_sfx_task must detect that shape and route to
    # _save_music_task_artifacts instead of the SFX/ducking single-dict
    # _save_task_artifacts path (which would otherwise wrongly report "no
    # audio artifact was returned").
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/m-vocals-99").mock(
        return_value=httpx.Response(200, json={
            "task_id": "m-vocals-99", "type": "video_to_music",
            "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
            "vocals": {
                "url": "https://r2.test/vocals.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            },
            "mux": [{
                "stream_index": 0, "url": "https://r2.test/mux.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
        })
    )
    respx.get("https://r2.test/a.m4a").mock(return_value=httpx.Response(200, content=b"a"))
    respx.get("https://r2.test/vocals.m4a").mock(return_value=httpx.Response(200, content=b"v"))
    respx.get("https://r2.test/mux.m4a").mock(return_value=httpx.Response(200, content=b"m"))
    from sonilo_mcp.api import get_sfx_task
    result = await get_sfx_task("m-vocals-99")
    assert (output_dir / "music-m-vocals.m4a").read_bytes() == b"a"
    assert (output_dir / "music-m-vocals-vocals.m4a").read_bytes() == b"v"
    assert (output_dir / "music-m-vocals-mux.m4a").read_bytes() == b"m"
    assert len(result) == 3
    texts = [t.text for t in result]
    assert any("music audio" in t for t in texts)
    assert any("vocals" in t.lower() for t in texts)
    assert any("ready to use" in t.lower() for t in texts)


@respx.mock
async def test_get_sfx_task_recovers_music_task_without_type_field(
    monkeypatch, output_dir
):
    # Detection must not rely solely on `type` — fall back to shape-sniffing
    # (a list-shaped `audio`) so recovery still works if the backend ever
    # omits `type`.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/m-notype-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "m-notype-1", "status": "succeeded",
            "audio": [{
                "stream_index": 0, "url": "https://r2.test/a2.m4a",
                "content_type": "audio/mp4", "file_size": 1,
            }],
        })
    )
    respx.get("https://r2.test/a2.m4a").mock(return_value=httpx.Response(200, content=b"aa"))
    from sonilo_mcp.api import get_sfx_task
    result = await get_sfx_task("m-notype-1")
    assert (output_dir / "music-m-notype.m4a").read_bytes() == b"aa"
    assert len(result) == 1


@respx.mock
async def test_get_sfx_task_music_task_failed_still_reports_refund(
    monkeypatch, output_dir
):
    # A failed music task must raise the same shared failure/refund message
    # as SFX/ducking, task_id included — envelope detection must not change
    # failure handling.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    respx.get("https://api.test.local/v1/tasks/m-failed-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "m-failed-1", "type": "video_to_music",
            "status": "failed",
            "error": {"code": "GENERATION_FAILED", "message": "boom"},
            "refunded": False,
        })
    )
    from sonilo_mcp.api import get_sfx_task
    with pytest.raises(Exception, match="boom"):
        await get_sfx_task("m-failed-1")


# ---------- audio_ducking ----------

async def test_audio_ducking_requires_exactly_one_voice_input(output_dir):
    from sonilo_mcp.api import audio_ducking
    with pytest.raises(Exception, match="voice_path or voice_url"):
        await audio_ducking(music_url="https://cdn.test/m.mp3")
    with pytest.raises(Exception, match="voice_path or voice_url"):
        await audio_ducking(
            voice_path="/tmp/v.wav",
            voice_url="https://cdn.test/v.wav",
            music_url="https://cdn.test/m.mp3",
        )


async def test_audio_ducking_requires_exactly_one_music_input(output_dir):
    from sonilo_mcp.api import audio_ducking
    with pytest.raises(Exception, match="music_path or music_url"):
        await audio_ducking(voice_url="https://cdn.test/v.wav")
    with pytest.raises(Exception, match="music_path or music_url"):
        await audio_ducking(
            voice_url="https://cdn.test/v.wav",
            music_path="/tmp/m.mp3",
            music_url="https://cdn.test/m.mp3",
        )


async def test_audio_ducking_validates_missing_music_before_voice_io(
    monkeypatch, output_dir
):
    # Both pairs are checked BEFORE any I/O, so a missing music input is
    # reported even when the voice input would itself fail to resolve.
    from sonilo_mcp.api import audio_ducking
    with pytest.raises(Exception, match="music_path or music_url"):
        await audio_ducking(voice_path="/nonexistent/voice.wav")


async def test_audio_ducking_rejects_non_http_url(output_dir):
    from sonilo_mcp.api import audio_ducking
    with pytest.raises(Exception, match="http:// or https://"):
        await audio_ducking(
            voice_url="file:///etc/passwd", music_url="https://cdn.test/m.mp3"
        )
    with pytest.raises(Exception, match="http:// or https://"):
        await audio_ducking(
            voice_url="https://cdn.test/v.wav", music_url="file:///etc/passwd"
        )


@respx.mock
async def test_audio_ducking_urls_only(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    _patch_ffprobe(monkeypatch, duration=30.0)
    submit = respx.post("https://api.test.local/v1/audio-ducking").mock(
        return_value=httpx.Response(
            202, json={"task_id": "d-10", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/d-10").mock(
        return_value=httpx.Response(200, json={
            "task_id": "d-10", "type": "audio_ducking", "status": "succeeded",
            "output_url": "https://r2.test/out.wav", "output_type": "audio",
        })
    )
    respx.get("https://r2.test/out.wav").mock(
        return_value=httpx.Response(200, content=b"ducked")
    )
    from sonilo_mcp.api import audio_ducking
    result = await audio_ducking(
        voice_url="https://cdn.test/podcast.wav",
        music_url="https://cdn.test/bed.mp3",
    )
    sent = submit.calls.last.request
    assert b"voice_url" in sent.content
    assert b"music_url" in sent.content
    saved = output_dir / "podcast-ducked.wav"
    assert saved.read_bytes() == b"ducked"
    assert str(saved) in result[0].text


@respx.mock
async def test_audio_ducking_video_voice_file_and_music_url(
    monkeypatch, output_dir, tmp_path
):
    # Mixed submission: a local video voice file uploads as voice_file while
    # music stays a URL. The result is the re-muxed mp4.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    voice = tmp_path / "interview.mp4"
    voice.write_bytes(b"FAKE-MP4")
    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": 300})
    )
    _patch_ffprobe(monkeypatch, duration=45.0)
    submit = respx.post("https://api.test.local/v1/audio-ducking").mock(
        return_value=httpx.Response(
            202, json={"task_id": "d-11", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/d-11").mock(
        return_value=httpx.Response(200, json={
            "task_id": "d-11", "type": "audio_ducking", "status": "succeeded",
            "output_url": "https://r2.test/out.mp4", "output_type": "video",
        })
    )
    respx.get("https://r2.test/out.mp4").mock(
        return_value=httpx.Response(200, content=b"muxed")
    )
    from sonilo_mcp.api import _reset_services_cache, audio_ducking
    _reset_services_cache()
    result = await audio_ducking(
        voice_path=str(voice), music_url="https://cdn.test/bed.mp3"
    )
    body = submit.calls.last.request.content
    assert b"voice_file" in body
    assert b"FAKE-MP4" in body
    assert b"music_url" in body
    saved = output_dir / "interview-ducked.mp4"
    assert saved.read_bytes() == b"muxed"
    assert str(saved) in result[0].text


@respx.mock
async def test_audio_ducking_rejects_too_long_voice_before_upload(
    monkeypatch, output_dir, tmp_path
):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    voice = tmp_path / "long.wav"
    voice.write_bytes(b"FAKE-WAV")
    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": 300})
    )
    submit = respx.post("https://api.test.local/v1/audio-ducking").mock(
        return_value=httpx.Response(202, json={"task_id": "nope"})
    )
    _patch_ffprobe(monkeypatch, duration=400.0)
    from sonilo_mcp.api import _reset_services_cache, audio_ducking
    _reset_services_cache()
    with pytest.raises(Exception, match="exceeds the maximum"):
        await audio_ducking(
            voice_path=str(voice), music_url="https://cdn.test/bed.mp3"
        )
    assert submit.call_count == 0


@respx.mock
async def test_audio_ducking_rejects_oversized_voice_file(
    monkeypatch, output_dir, tmp_path
):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    voice = tmp_path / "big.wav"
    voice.write_bytes(b"x" * (2 * 1024 * 1024))
    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": 1})
    )
    submit = respx.post("https://api.test.local/v1/audio-ducking").mock(
        return_value=httpx.Response(202, json={"task_id": "nope"})
    )
    _patch_ffprobe(monkeypatch, duration=10.0)
    from sonilo_mcp.api import _reset_services_cache, audio_ducking
    _reset_services_cache()
    with pytest.raises(Exception, match="too large"):
        await audio_ducking(
            voice_path=str(voice), music_url="https://cdn.test/bed.mp3"
        )
    assert submit.call_count == 0


async def test_audio_ducking_rejects_video_music_file(
    monkeypatch, output_dir, tmp_path
):
    # The backend never probes the music input for a video stream, so a
    # video there is a mistake — reject it locally with a clear message.
    music = tmp_path / "bed.mp4"
    music.write_bytes(b"FAKE-MP4")
    from sonilo_mcp.api import audio_ducking
    with pytest.raises(Exception, match="not a recognized audio format"):
        await audio_ducking(
            voice_url="https://cdn.test/v.wav", music_path=str(music)
        )


def _patch_ffprobe_per_source(monkeypatch, durations: dict, default=30.0):
    """Fake ffprobe whose reported duration depends on the source argument.

    `durations` maps a substring of the source (a path or URL) to the
    duration to report for it; anything unmatched gets `default`. Needed to
    exercise the MUSIC-side duration cap without the VOICE input tripping
    first.
    """
    from sonilo_mcp import api

    monkeypatch.setattr(api.shutil, "which", lambda name: "/usr/bin/ffprobe")

    async def fake_exec(*args, **kwargs):
        source = args[-1]
        duration = default
        for needle, value in durations.items():
            if needle in source:
                duration = value
        payload = json.dumps({"format": {"duration": str(duration)}}).encode()
        return _FakeProc(payload, returncode=0)

    monkeypatch.setattr(api.asyncio, "create_subprocess_exec", fake_exec)


@respx.mock
async def test_audio_ducking_music_file_and_voice_url(
    monkeypatch, output_dir, tmp_path
):
    # Mixed submission the other way round: the music bed is a local file
    # (uploaded as the music_file part) while the voice stays a URL.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    music = tmp_path / "bed.mp3"
    music.write_bytes(b"FAKE-MP3")
    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": 300})
    )
    _patch_ffprobe(monkeypatch, duration=30.0)
    submit = respx.post("https://api.test.local/v1/audio-ducking").mock(
        return_value=httpx.Response(
            202, json={"task_id": "d-12", "status": "processing"}
        )
    )
    respx.get("https://api.test.local/v1/tasks/d-12").mock(
        return_value=httpx.Response(200, json={
            "task_id": "d-12", "type": "audio_ducking", "status": "succeeded",
            "output_url": "https://r2.test/out.wav", "output_type": "audio",
        })
    )
    respx.get("https://r2.test/out.wav").mock(
        return_value=httpx.Response(200, content=b"ducked")
    )
    from sonilo_mcp.api import _reset_services_cache, audio_ducking
    _reset_services_cache()
    result = await audio_ducking(
        voice_url="https://cdn.test/podcast.wav", music_path=str(music)
    )
    body = submit.calls.last.request.content
    # The music file rides as the `music_file` multipart part, alongside the
    # voice_url form field.
    assert b'name="music_file"' in body
    assert b"FAKE-MP3" in body
    assert b'name="voice_url"' in body
    assert b"https://cdn.test/podcast.wav" in body
    saved = output_dir / "podcast-ducked.wav"
    assert saved.read_bytes() == b"ducked"
    assert str(saved) in result[0].text


@respx.mock
async def test_audio_ducking_rejects_oversized_music_file(
    monkeypatch, output_dir, tmp_path
):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    music = tmp_path / "big.mp3"
    music.write_bytes(b"x" * (2 * 1024 * 1024))
    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": 1})
    )
    submit = respx.post("https://api.test.local/v1/audio-ducking").mock(
        return_value=httpx.Response(202, json={"task_id": "nope"})
    )
    _patch_ffprobe(monkeypatch, duration=10.0)
    from sonilo_mcp.api import _reset_services_cache, audio_ducking
    _reset_services_cache()
    with pytest.raises(Exception, match="Music file is too large"):
        await audio_ducking(
            voice_url="https://cdn.test/podcast.wav", music_path=str(music)
        )
    # Rejected before the charge.
    assert submit.call_count == 0


@respx.mock
async def test_audio_ducking_rejects_too_long_music_before_upload(
    monkeypatch, output_dir, tmp_path
):
    # The music input has its own 360s cap: an over-long bed must be caught
    # even when the voice input is well within the limit.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    music = tmp_path / "long-bed.mp3"
    music.write_bytes(b"FAKE-MP3")
    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": 300})
    )
    submit = respx.post("https://api.test.local/v1/audio-ducking").mock(
        return_value=httpx.Response(202, json={"task_id": "nope"})
    )
    _patch_ffprobe_per_source(
        monkeypatch, {"long-bed.mp3": 420.0}, default=30.0
    )
    from sonilo_mcp.api import _reset_services_cache, audio_ducking
    _reset_services_cache()
    with pytest.raises(Exception, match="exceeds the maximum"):
        await audio_ducking(
            voice_url="https://cdn.test/podcast.wav", music_path=str(music)
        )
    # Rejected before the charge.
    assert submit.call_count == 0


@respx.mock
async def test_audio_ducking_size_cap_enforced_on_actual_bytes(
    monkeypatch, output_dir, tmp_path
):
    # TOCTOU: the stat()-based check runs before the ffprobe await (up to
    # 30s). If the file grows during that window, the stale check must not
    # let the oversized bytes bypass the cap — _read_capped enforces it on
    # what is actually read, before anything is uploaded (or charged).
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    voice = tmp_path / "voice.wav"
    voice.write_bytes(b"\x00" * 1024)

    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": 2})
    )
    api._reset_services_cache()

    real_check = api._check_media_duration

    async def growing_check(source, **kwargs):
        # The file grows while the caller awaits the duration pre-check.
        voice.write_bytes(b"\x01" * (5 * 1024 * 1024))
        return await real_check(source, **kwargs)

    _patch_ffprobe(monkeypatch, duration=30.0)
    monkeypatch.setattr(api, "_check_media_duration", growing_check)

    submit = respx.post("https://api.test.local/v1/audio-ducking").mock(
        return_value=httpx.Response(202, json={"task_id": "nope"})
    )

    with pytest.raises(Exception, match="Voice file is too large"):
        await api.audio_ducking(
            voice_path=str(voice), music_url="https://cdn.test/bed.mp3"
        )
    # Must NOT charge — the oversized bytes must never be uploaded.
    assert submit.call_count == 0


@respx.mock
async def test_audio_ducking_rejects_too_long_voice_url_before_post(
    monkeypatch, output_dir, tmp_path
):
    # URL-input mirror of the file-input over-long guard: an over-long
    # voice_url must be caught by the ffprobe pre-check BEFORE the POST that
    # charges. Without this, a future refactor could drop the URL-branch check
    # and silently bill the user for an over-long URL input.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    submit = respx.post("https://api.test.local/v1/audio-ducking").mock(
        return_value=httpx.Response(202, json={"task_id": "nope"})
    )
    # Only the voice URL is over-long (music well within the cap), so the
    # rejection is unambiguously the voice-URL check.
    _patch_ffprobe_per_source(monkeypatch, {"podcast": 420.0}, default=30.0)
    from sonilo_mcp.api import _reset_services_cache, audio_ducking
    _reset_services_cache()
    with pytest.raises(Exception, match="exceeds the maximum"):
        await audio_ducking(
            voice_url="https://cdn.test/podcast.wav",
            music_url="https://cdn.test/bed.mp3",
        )
    assert submit.call_count == 0


@respx.mock
async def test_audio_ducking_rejects_too_long_music_url_before_post(
    monkeypatch, output_dir, tmp_path
):
    # Same as above for the music_url input: its own 360s cap must be enforced
    # before the charge even when the voice URL is well within the limit.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    submit = respx.post("https://api.test.local/v1/audio-ducking").mock(
        return_value=httpx.Response(202, json={"task_id": "nope"})
    )
    # Only the music URL is over-long — pins the music-URL check independently.
    _patch_ffprobe_per_source(monkeypatch, {"bed": 420.0}, default=30.0)
    from sonilo_mcp.api import _reset_services_cache, audio_ducking
    _reset_services_cache()
    with pytest.raises(Exception, match="exceeds the maximum"):
        await audio_ducking(
            voice_url="https://cdn.test/podcast.wav",
            music_url="https://cdn.test/bed.mp3",
        )
    assert submit.call_count == 0


@respx.mock
async def test_audio_ducking_stat_fail_fast_rejects_oversized_voice(
    monkeypatch, output_dir, tmp_path
):
    # Pins the stat() fail-fast guard INDEPENDENTLY of _read_capped: stat sees
    # an oversized file, but by the time the bytes are read the file is small,
    # so only the fail-fast stat check can catch it. If that guard were deleted,
    # _read_capped would read the (now small) bytes and let the charge through.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    voice = tmp_path / "voice.wav"
    voice.write_bytes(b"x" * (2 * 1024 * 1024))

    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": 1})
    )
    api._reset_services_cache()

    real_check = api._check_media_duration

    async def shrinking_check(source, **kwargs):
        # The file shrinks to a handful of bytes while the caller awaits the
        # pre-check — after stat() has already seen it oversized.
        if "voice.wav" in source:
            voice.write_bytes(b"y" * 16)
        return await real_check(source, **kwargs)

    _patch_ffprobe(monkeypatch, duration=30.0)
    monkeypatch.setattr(api, "_check_media_duration", shrinking_check)

    submit = respx.post("https://api.test.local/v1/audio-ducking").mock(
        return_value=httpx.Response(202, json={"task_id": "nope"})
    )

    with pytest.raises(Exception, match="Voice file is too large"):
        await api.audio_ducking(
            voice_path=str(voice), music_url="https://cdn.test/bed.mp3"
        )
    assert submit.call_count == 0


@respx.mock
async def test_audio_ducking_stat_fail_fast_rejects_oversized_music(
    monkeypatch, output_dir, tmp_path
):
    # Music-side mirror of the stat() fail-fast guard: stat sees the music file
    # oversized, but it shrinks before its bytes are read — only the fail-fast
    # stat check can catch it.
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    music = tmp_path / "bed.mp3"
    music.write_bytes(b"x" * (2 * 1024 * 1024))

    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": 1})
    )
    api._reset_services_cache()

    real_check = api._check_media_duration

    async def shrinking_check(source, **kwargs):
        # Shrink ONLY the music file, and only when its own pre-check runs (the
        # voice pre-check runs first with a different source). This keeps the
        # music file oversized at the moment its stat() guard runs.
        if "bed.mp3" in source:
            music.write_bytes(b"y" * 16)
        return await real_check(source, **kwargs)

    _patch_ffprobe(monkeypatch, duration=30.0)
    monkeypatch.setattr(api, "_check_media_duration", shrinking_check)

    submit = respx.post("https://api.test.local/v1/audio-ducking").mock(
        return_value=httpx.Response(202, json={"task_id": "nope"})
    )

    with pytest.raises(Exception, match="Music file is too large"):
        await api.audio_ducking(
            voice_url="https://cdn.test/podcast.wav", music_path=str(music)
        )
    assert submit.call_count == 0


@respx.mock
async def test_video_to_sound_url_mode_submits_and_saves(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-sound").mock(
        return_value=httpx.Response(202, json={"task_id": "sd-1", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/sd-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "sd-1", "type": "video_to_sound", "status": "succeeded",
            "output_url": "https://r2.test/sound.wav",
            "output_type": "audio",
            "output_bytes": 11,
            "music": {"url": "https://r2.test/music.m4a", "content_type": "audio/mp4"},
            "sfx": {"url": "https://r2.test/sfx.wav", "content_type": "audio/wav"},
            "duration_seconds": 5.0,
        })
    )
    respx.get("https://r2.test/sound.wav").mock(
        return_value=httpx.Response(200, content=b"sound-bytes")
    )
    result = await api.video_to_sound(
        video_url="https://example.com/clip.mp4",
        music_prompt="Cinematic",
        sfx_prompt="footsteps",
        segments=[{"start": 0, "end": 2, "prompt": "whoosh"}],
        preserve_speech=True,
        ducking=False,
    )
    sent = submit.calls.last.request
    assert b"music_prompt=" in sent.content
    assert b"sfx_prompt=" in sent.content
    assert b"segments=" in sent.content
    assert b"preserve_speech=true" in sent.content
    assert b"ducking=false" in sent.content
    assert b"isolate_vocals" not in sent.content
    # Only the combined artifact is written; the stems are left on the backend.
    assert len(result) == 1
    assert (output_dir / "cinematic.wav").read_bytes() == b"sound-bytes"
    assert not (output_dir / "cinematic.m4a").exists()


@respx.mock
async def test_video_to_video_sound_saves_mp4_and_defaults_ducking_off(
    monkeypatch, output_dir
):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-video-sound").mock(
        return_value=httpx.Response(202, json={"task_id": "sd-2", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/sd-2").mock(
        return_value=httpx.Response(200, json={
            "task_id": "sd-2", "type": "video_to_video_sound", "status": "succeeded",
            "output_url": "https://r2.test/sound.mp4",
            "output_type": "video",
            "output_bytes": 11,
        })
    )
    respx.get("https://r2.test/sound.mp4").mock(
        return_value=httpx.Response(200, content=b"video-bytes")
    )
    result = await api.video_to_video_sound(video_url="https://example.com/clip.mp4")
    # The sound path always states ducking explicitly, and the backend default
    # is now off, so the default call sends "false".
    assert b"ducking=false" in submit.calls.last.request.content
    assert len(result) == 1
    assert (output_dir / "v2v-sound-sd-2.mp4").read_bytes() == b"video-bytes"


@pytest.mark.parametrize("bad", [0, -1, 11])
async def test_video_to_sound_rejects_out_of_range_variants_num(output_dir, bad):
    from sonilo_mcp import api
    with pytest.raises(Exception, match="variants_num must be between 1 and 10"):
        await api.video_to_sound(
            video_url="https://example.com/clip.mp4", variants_num=bad
        )


@respx.mock
async def test_video_to_sound_variants_num_sends_field_and_saves_every_variant(
    monkeypatch, output_dir
):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-sound").mock(
        return_value=httpx.Response(202, json={"task_id": "sd-3", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/sd-3").mock(
        return_value=httpx.Response(200, json={
            "task_id": "sd-3", "type": "video_to_sound", "status": "succeeded",
            "outputs": [
                {"variant_index": 0, "output_url": "https://r2.test/sd3-0.wav",
                 "output_type": "audio", "output_bytes": 3,
                 "music": {"url": "https://r2.test/m0.m4a"}, "sfx": {"url": "https://r2.test/s0.wav"}},
                {"variant_index": 1, "output_url": "https://r2.test/sd3-1.wav",
                 "output_type": "audio", "output_bytes": 3,
                 "music": {"url": "https://r2.test/m1.m4a"}, "sfx": {"url": "https://r2.test/s1.wav"}},
            ],
            # Pre-variants aliases, kept for older/single-variant clients.
            "output_url": "https://r2.test/sd3-0.wav", "output_type": "audio",
            "output_bytes": 3,
        })
    )
    respx.get("https://r2.test/sd3-0.wav").mock(return_value=httpx.Response(200, content=b"o0"))
    respx.get("https://r2.test/sd3-1.wav").mock(return_value=httpx.Response(200, content=b"o1"))

    result = await api.video_to_sound(
        video_url="https://example.com/clip.mp4",
        music_prompt="Two Mixes",
        variants_num=2,
    )
    from urllib.parse import parse_qs
    sent = parse_qs(submit.calls.last.request.content.decode())
    assert sent["variants_num"] == ["2"]
    # Only the combined artifact per variant is saved — stems stay on the backend.
    assert len(result) == 2
    assert (output_dir / "two-mixes-0.wav").read_bytes() == b"o0"
    assert (output_dir / "two-mixes-1.wav").read_bytes() == b"o1"
    assert not (output_dir / "two-mixes-0.m4a").exists()


async def test_video_to_sound_rejects_both_inputs(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp import api
    with pytest.raises(Exception, match="exactly one"):
        await api.video_to_sound(
            video_path="/tmp/a.mp4", video_url="https://example.com/clip.mp4"
        )


DUBBING_BODY = {
    "task_id": "db-1",
    "type": "dubbing",
    "status": "succeeded",
    "outputs": {"fr": "https://r2.test/fr.mp4", "es": "https://r2.test/es.mp4"},
}


def test_is_dubbing_envelope_recognizes_the_outputs_map():
    from sonilo_mcp import api
    assert api._is_dubbing_envelope(DUBBING_BODY) is True
    assert api._is_dubbing_envelope({"task_id": "x", "status": "succeeded"}) is False
    assert api._is_dubbing_envelope({"outputs": {}}) is False
    assert api._is_dubbing_envelope({"outputs": {"es": ""}}) is False
    assert api._is_dubbing_envelope({"outputs": ["https://r2.test/es.mp4"]}) is False
    # An SFX body must never be mistaken for one.
    assert api._is_dubbing_envelope(
        {"status": "succeeded", "audio": {"url": "https://r2.test/a.wav"}}
    ) is False
    # An explicit, different type is authoritative and short-circuits to
    # False even when `outputs` happens to look dubbing-shaped — mirrors
    # _is_video_to_video_envelope's short-circuit on a non-matching type.
    assert api._is_dubbing_envelope(
        {"type": "video_to_sfx", "outputs": {"es": "https://r2.test/es.mp4"}}
    ) is False


@respx.mock
async def test_save_dubbing_artifacts_writes_one_file_per_language(tmp_path):
    from sonilo_mcp import api
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    respx.get("https://r2.test/fr.mp4").mock(
        return_value=httpx.Response(200, content=b"fr-bytes")
    )
    result = await api._save_dubbing_artifacts(
        DUBBING_BODY, tmp_path, "dubbing-db-1", "db-1"
    )
    assert len(result) == 2
    assert (tmp_path / "dubbing-db-1.es.mp4").read_bytes() == b"es-bytes"
    assert (tmp_path / "dubbing-db-1.fr.mp4").read_bytes() == b"fr-bytes"
    # Sorted, so the reported order is stable across runs.
    assert "es" in result[0].text and "fr" in result[1].text


@respx.mock
async def test_save_dubbing_artifacts_reports_the_free_preview(tmp_path):
    """A preview run translated only the first 15 s: the agent must be told,
    with the full-video quote, rather than presenting the clip as the whole
    translation."""
    from sonilo_mcp import api
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    body = {
        "type": "dubbing", "status": "succeeded",
        "outputs": {"es": "https://r2.test/es.mp4"},
        "trial_preview": {
            "preview_seconds": 15, "source_duration_seconds": 60.0, "trimmed": True,
            "languages": 1, "full_video_cost_usd": 3.49,
            "message": "Free preview: the first 15 seconds of your 60-second video, in 1 language. Translating the full video costs $3.49 — add funds at https://platform.sonilo.com/dashboard/billing",
        },
    }
    result = await api._save_dubbing_artifacts(body, tmp_path, "dubbing-p", "p-1")
    assert (tmp_path / "dubbing-p.es.mp4").read_bytes() == b"es-bytes"
    assert "first 15 seconds of your 60-second video" in result[-1].text
    assert "$3.49" in result[-1].text
    assert "billed" in result[-1].text


async def test_dubbing_description_explains_the_free_preview():
    from sonilo_mcp import api
    desc = {
        t.name: (t.description or "") for t in await api.mcp.list_tools()
    }["dubbing"]
    assert "ZERO" not in desc
    assert "first 15 seconds" in desc
    assert "trial_preview" in desc
    assert "PER LANGUAGE" in desc


@respx.mock
async def test_save_dubbing_artifacts_skips_blank_urls_in_a_mixed_map(tmp_path):
    from sonilo_mcp import api
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    result = await api._save_dubbing_artifacts(
        {
            "task_id": "db-1",
            "status": "succeeded",
            "outputs": {"es": "https://r2.test/es.mp4", "fr": ""},
        },
        tmp_path,
        "dubbing-db-1",
        "db-1",
    )
    # The blank "fr" entry is filtered out of the save loop — no download
    # attempted, no error raised — but a warning naming the dropped language
    # and the task_id is appended so the caller knows they were billed for
    # a language they did not receive.
    assert len(result) == 2
    assert "es" in result[0].text
    assert (tmp_path / "dubbing-db-1.es.mp4").read_bytes() == b"es-bytes"
    assert not (tmp_path / "dubbing-db-1.fr.mp4").exists()
    assert "fr" in result[1].text
    assert "db-1" in result[1].text
    assert "Warning" in result[1].text


@respx.mock
async def test_save_dubbing_artifacts_keeps_the_task_id_and_saved_paths_on_failure(tmp_path):
    from sonilo_mcp import api
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    respx.get("https://r2.test/fr.mp4").mock(return_value=httpx.Response(500))
    with pytest.raises(Exception) as exc:
        await api._save_dubbing_artifacts(
            DUBBING_BODY, tmp_path, "dubbing-db-1", "db-1"
        )
    message = str(exc.value)
    assert "db-1" in message
    assert "dubbing-db-1.es.mp4" in message
    # The failed download must not leave a half-written file behind.
    assert not (tmp_path / "dubbing-db-1.fr.mp4").exists()


async def test_save_dubbing_artifacts_raises_on_a_failed_task(tmp_path):
    from sonilo_mcp import api
    with pytest.raises(Exception) as exc:
        await api._save_dubbing_artifacts(
            {
                "task_id": "db-1",
                "status": "failed",
                "error": {"code": "DUBBING_FAILED", "message": "dubbing job failed"},
                "refunded": True,
            },
            tmp_path,
            "dubbing-db-1",
            "db-1",
        )
    assert "DUBBING_FAILED" in str(exc.value)


async def test_save_dubbing_artifacts_raises_when_outputs_is_empty(tmp_path):
    from sonilo_mcp import api
    with pytest.raises(Exception) as exc:
        await api._save_dubbing_artifacts(
            {"task_id": "db-1", "status": "succeeded", "outputs": {}},
            tmp_path,
            "dubbing-db-1",
            "db-1",
        )
    assert "db-1" in str(exc.value)


@respx.mock
async def test_save_dubbing_artifacts_saves_the_exported_srts(tmp_path):
    """export_srt adds one .srt per language, named to match the video it
    belongs to. The pipeline returns its numbers as STRINGS on a finished
    task, so alignment_loss arrives as "0.6305176995017312" and must still be
    reported rather than crashing a save of videos already paid for."""
    from sonilo_mcp import api
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    respx.get("https://r2.test/fr.mp4").mock(
        return_value=httpx.Response(200, content=b"fr-bytes")
    )
    respx.get("https://r2.test/es.srt").mock(
        return_value=httpx.Response(200, content=b"es-cues")
    )
    respx.get("https://r2.test/fr.srt").mock(
        return_value=httpx.Response(200, content=b"fr-cues")
    )
    result = await api._save_dubbing_artifacts(
        {
            **DUBBING_BODY,
            "subtitles": {
                "es": "https://r2.test/es.srt",
                "fr": "https://r2.test/fr.srt",
            },
            "subtitle_export": {
                "es": {"status": "exported", "alignment_loss": 0.42},
                # A string, as the finished task hands it back.
                "fr": {
                    "status": "exported_review_required",
                    "alignment_loss": "0.6305176995017312",
                },
            },
        },
        tmp_path,
        "dubbing-db-1",
        "db-1",
    )
    assert (tmp_path / "dubbing-db-1.es.srt").read_bytes() == b"es-cues"
    assert (tmp_path / "dubbing-db-1.fr.srt").read_bytes() == b"fr-cues"
    # Every video first, then the subtitles — both in sorted language order.
    assert len(result) == 4
    assert "dubbing-db-1.es.mp4" in result[0].text
    assert "dubbing-db-1.fr.mp4" in result[1].text
    assert "dubbing-db-1.es.srt" in result[2].text
    assert "exported" in result[2].text
    assert "alignment loss 0.420" in result[2].text
    assert "dubbing-db-1.fr.srt" in result[3].text
    assert "alignment loss 0.631" in result[3].text


@respx.mock
async def test_save_dubbing_artifacts_downloads_every_video_before_any_srt(tmp_path):
    """A bad script URL must not cost the caller a video. Interleaved, a 500
    on the first language's .srt abandons the second language's .mp4 — and
    the advertised recovery (get_sfx_task) re-enters this function and dies
    at the same URL, so a delivered, charged video would be unreachable for
    as long as that one script URL stays broken."""
    from sonilo_mcp import api
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    respx.get("https://r2.test/fr.mp4").mock(
        return_value=httpx.Response(200, content=b"fr-bytes")
    )
    respx.get("https://r2.test/es.srt").mock(return_value=httpx.Response(500))
    respx.get("https://r2.test/fr.srt").mock(
        return_value=httpx.Response(200, content=b"fr-cues")
    )
    result = await api._save_dubbing_artifacts(
        {
            **DUBBING_BODY,
            "subtitles": {
                "es": "https://r2.test/es.srt",
                "fr": "https://r2.test/fr.srt",
            },
        },
        tmp_path,
        "dubbing-db-1",
        "db-1",
    )
    # Both videos, and the one good script, are on disk.
    assert (tmp_path / "dubbing-db-1.es.mp4").read_bytes() == b"es-bytes"
    assert (tmp_path / "dubbing-db-1.fr.mp4").read_bytes() == b"fr-bytes"
    assert (tmp_path / "dubbing-db-1.fr.srt").read_bytes() == b"fr-cues"
    assert not (tmp_path / "dubbing-db-1.es.srt").exists()
    assert len(result) == 4
    failed = result[2].text
    assert failed.startswith("Note (es)")
    assert "unaffected" in failed
    assert "db-1" in failed


@respx.mock
async def test_save_dubbing_artifacts_saves_an_srt_for_a_language_with_no_video(tmp_path):
    """The script exists and was paid for, so it is delivered even though
    that language's video URL came back blank — iterating the subtitle map
    rather than the video map is what makes that possible."""
    from sonilo_mcp import api
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    respx.get("https://r2.test/fr.srt").mock(
        return_value=httpx.Response(200, content=b"fr-cues")
    )
    result = await api._save_dubbing_artifacts(
        {
            "task_id": "db-1",
            "status": "succeeded",
            "outputs": {"es": "https://r2.test/es.mp4", "fr": ""},
            "subtitles": {"fr": "https://r2.test/fr.srt"},
        },
        tmp_path,
        "dubbing-db-1",
        "db-1",
    )
    assert (tmp_path / "dubbing-db-1.fr.srt").read_bytes() == b"fr-cues"
    # The missing-video warning still fires, and is last.
    assert len(result) == 3
    assert "dubbing-db-1.fr.srt" in result[1].text
    assert result[2].text.startswith("Warning")


@respx.mock
async def test_save_dubbing_artifacts_reports_a_blocked_export_as_a_note(tmp_path):
    """A blocked export is not a failed task: the dubbed video is delivered
    and charged, and only that language's script is missing."""
    from sonilo_mcp import api
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    respx.get("https://r2.test/fr.mp4").mock(
        return_value=httpx.Response(200, content=b"fr-bytes")
    )
    respx.get("https://r2.test/es.srt").mock(
        return_value=httpx.Response(200, content=b"es-cues")
    )
    result = await api._save_dubbing_artifacts(
        {
            **DUBBING_BODY,
            "subtitles": {"es": "https://r2.test/es.srt"},
            "subtitle_export": {
                "es": {"status": "exported", "alignment_loss": "0.1"},
                "fr": {
                    "status": "blocked",
                    "issues": ["ALIGNMENT_FAILED"],
                    "report_url": "https://r2.test/fr-report.json",
                },
            },
        },
        tmp_path,
        "dubbing-db-1",
        "db-1",
    )
    assert (tmp_path / "dubbing-db-1.fr.mp4").read_bytes() == b"fr-bytes"
    assert not (tmp_path / "dubbing-db-1.fr.srt").exists()
    note = result[3].text
    assert note.startswith("Note (fr)")
    assert "blocked" in note
    assert "unaffected" in note
    # "blocked" alone leaves the user with no reason and nothing to read.
    assert "ALIGNMENT_FAILED" in note
    assert "https://r2.test/fr-report.json" in note


async def test_as_number_tolerates_both_wire_shapes():
    """The acceptance response carries real numbers, the finished task the
    same fields as strings. Anything else must degrade to "not reported"
    instead of raising on a result the caller has already paid for."""
    from sonilo_mcp import api
    assert api._as_number(0.42) == 0.42
    assert api._as_number("0.6305176995017312") == 0.6305176995017312
    assert api._as_number("5") == 5.0
    assert api._as_number(None) is None
    assert api._as_number("n/a") is None
    assert api._as_number(True) is None


def test_subtitle_export_note_carries_every_field_it_is_given():
    """status, alignment loss, issues, error and the report link — a blocked
    export is the one case where the user has nothing else to go on."""
    from sonilo_mcp import api
    note = api._subtitle_export_note({
        "status": "blocked",
        "alignment_loss": "1.66",
        "issues": ["LOSS_TOO_HIGH", "SHORT_CUES"],
        "error": "alignment did not converge",
        "report_url": "https://r2.test/report.json",
    })
    assert "blocked" in note
    assert "alignment loss 1.660" in note
    assert "LOSS_TOO_HIGH" in note and "SHORT_CUES" in note
    assert "alignment did not converge" in note
    assert "https://r2.test/report.json" in note


def test_subtitle_export_note_survives_junk_fields():
    """These come off the wire: a wrongly-typed field must drop out of the
    note, never raise while saving a result already charged for."""
    from sonilo_mcp import api
    assert api._subtitle_export_note(None) == ""
    assert api._subtitle_export_note({}) == ""
    assert api._subtitle_export_note(
        {"status": 7, "alignment_loss": "n/a", "issues": {"a": 1},
         "error": "", "report_url": 3}
    ) == ""
    assert api._subtitle_export_note({"issues": "LOSS_TOO_HIGH"}) == (
        "issues: LOSS_TOO_HIGH"
    )


async def test_stage_video_input_returns_form_fields_for_a_url(monkeypatch, tmp_path):
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)
    files, extra = await api._stage_video_input(
        None, "https://example.com/clip.mp4", str(tmp_path), 180
    )
    assert files is None
    assert extra == {"video_url": "https://example.com/clip.mp4"}


@respx.mock
async def test_stage_video_input_returns_a_files_mapping_for_a_local_path(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    # Established pattern (see test_video_to_sfx_path_mode_uploads_multipart):
    # _get_max_upload_size_mb is backed by a cached GET to /v1/account/services,
    # so tests mock that route and reset the cache rather than stubbing the
    # function directly.
    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": 300})
    )
    api._reset_services_cache()

    clip = tmp_path / "clip.mp4"
    clip.write_bytes(b"video-bytes")
    files, extra = await api._stage_video_input("clip.mp4", None, str(tmp_path), 180)
    assert extra == {}
    assert files is not None
    assert files["video"][0] == "clip.mp4"
    assert files["video"][1] == b"video-bytes"


async def test_stage_video_input_rejects_an_over_long_video(monkeypatch, tmp_path):
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=200.0)
    with pytest.raises(Exception):
        await api._stage_video_input(
            None, "https://example.com/clip.mp4", str(tmp_path), 180
        )


@respx.mock
async def test_dubbing_url_mode_submits_and_saves_each_language(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/dubbing").mock(
        return_value=httpx.Response(202, json={"task_id": "db-1", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/db-1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "db-1", "type": "dubbing", "status": "succeeded",
            "outputs": {
                "es": "https://r2.test/es.mp4",
                "fr": "https://r2.test/fr.mp4",
            },
        })
    )
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    respx.get("https://r2.test/fr.mp4").mock(
        return_value=httpx.Response(200, content=b"fr-bytes")
    )
    result = await api.dubbing(
        video_url="https://example.com/clip.mp4", languages=["es", "fr"]
    )
    # URL-only submissions ride as application/x-www-form-urlencoded (same as
    # every other tool's URL mode), so the JSON array arrives percent-encoded
    # on the wire — decode before checking it reached the backend intact.
    from urllib.parse import unquote_plus
    sent = unquote_plus(submit.calls.last.request.content.decode())
    assert '["es", "fr"]' in sent
    assert len(result) == 2
    assert (output_dir / "dubbing-db-1.es.mp4").read_bytes() == b"es-bytes"
    assert (output_dir / "dubbing-db-1.fr.mp4").read_bytes() == b"fr-bytes"


@respx.mock
async def test_dubbing_omits_languages_when_unset(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/dubbing").mock(
        return_value=httpx.Response(202, json={"task_id": "db-2", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/db-2").mock(
        return_value=httpx.Response(200, json={
            "task_id": "db-2", "type": "dubbing", "status": "succeeded",
            "outputs": {"es": "https://r2.test/es.mp4"},
        })
    )
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    await api.dubbing(video_url="https://example.com/clip.mp4")
    assert b"languages" not in submit.calls.last.request.content
    # ducking unset → omitted so the server default (off) applies.
    assert b"ducking" not in submit.calls.last.request.content


@respx.mock
async def test_dubbing_sends_ducking_when_set(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/dubbing").mock(
        return_value=httpx.Response(202, json={"task_id": "db-3", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/db-3").mock(
        return_value=httpx.Response(200, json={
            "task_id": "db-3", "type": "dubbing", "status": "succeeded",
            "outputs": {"es": "https://r2.test/es.mp4"},
        })
    )
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    await api.dubbing(video_url="https://example.com/clip.mp4", ducking=True)
    assert b'name="ducking"\r\n\r\ntrue' in submit.calls.last.request.content or \
        b"ducking=true" in submit.calls.last.request.content


def _mock_upload_cap(api, mb: int = 300) -> None:
    """Wire up the cached /v1/account/services read that backs
    _get_max_upload_size_mb — the established pattern for anything that
    uploads a local file (see test_video_to_sfx_path_mode_uploads_multipart).
    """
    respx.get("https://api.test.local/v1/account/services").mock(
        return_value=httpx.Response(200, json={"max_upload_size_mb": mb})
    )
    api._reset_services_cache()


async def test_stage_subtitles_sends_an_https_value_as_a_form_field(tmp_path):
    """An https script is a text field; nothing is read from disk for it —
    and no upload cap is fetched, since nothing is being uploaded."""
    from sonilo_mcp import api
    files, form = await api._stage_subtitles(
        {"es": "https://example.com/es.srt"}, str(tmp_path)
    )
    assert files == {}
    assert form == {"subtitles[es]": "https://example.com/es.srt"}


@respx.mock
async def test_stage_subtitles_uploads_a_local_path_as_a_file_part(
    monkeypatch, tmp_path
):
    """Anything that is not an https URL is a local path — this server runs
    on the caller's own machine and already uploads their video, so a script
    path is uploaded the same way. The field name is bracketed either way."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _mock_upload_cap(api)
    script = tmp_path / "es.srt"
    script.write_bytes(b"1\n00:00:00,000 --> 00:00:01,000\nhola\n")
    files, form = await api._stage_subtitles({"es": "es.srt"}, str(tmp_path))
    assert form == {}
    assert files["subtitles[es]"][0] == "es.srt"
    assert files["subtitles[es]"][1] == script.read_bytes()


@respx.mock
async def test_stage_subtitles_bounds_the_read_at_the_account_cap(
    monkeypatch, tmp_path
):
    """Not a copy of the server's own per-script limit — just the refusal to
    pull an unbounded file into memory (and up the wire) because somebody
    renamed it .srt. _read_capped is the single copy of that rule, and every
    tool that uploads a local file goes through it."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _mock_upload_cap(api, mb=1)
    (tmp_path / "es.srt").write_bytes(b"x" * (1024 * 1024 + 1))
    with pytest.raises(Exception, match="too large"):
        await api._stage_subtitles({"es": "es.srt"}, str(tmp_path))


async def test_stage_subtitles_rejects_a_non_subtitle_suffix(tmp_path):
    """A .txt script is a guaranteed 422 after a full video upload, so it is
    refused here — and named, even when the file exists."""
    from sonilo_mcp import api
    (tmp_path / "es.txt").write_text("hola")
    with pytest.raises(Exception, match=r"\.srt or \.vtt"):
        await api._stage_subtitles({"es": "es.txt"}, str(tmp_path))
    with pytest.raises(Exception, match="es.txt"):
        await api._stage_subtitles({"es": "es.txt"}, str(tmp_path))


async def test_stage_subtitles_does_not_check_the_language_codes(tmp_path):
    """The backend owns the supported list and the languages-must-match rule;
    a copy here would reject a language added later, or a caller relying on
    the server's own default language set."""
    from sonilo_mcp import api
    _, form = await api._stage_subtitles(
        {"xx_yy": "https://example.com/xx.vtt"}, str(tmp_path)
    )
    assert form == {"subtitles[xx_yy]": "https://example.com/xx.vtt"}


async def test_stage_subtitles_refuses_a_path_outside_the_base_directory(tmp_path):
    """A script path is the one new surface here that reads the user's own
    files, so it is confined like every other input: SONILO_MCP_BASE_PATH is
    the boundary, and neither ../ nor a symlink pointing out of it escapes
    (_is_within_base resolves both sides before comparing)."""
    from sonilo_mcp import api
    base = tmp_path / "base"
    base.mkdir()
    secret = tmp_path / "secret.srt"
    secret.write_text("private")

    # Traversal: an existing file reached by climbing out of the base.
    with pytest.raises(Exception, match="outside the allowed base"):
        await api._stage_subtitles({"es": "../secret.srt"}, str(base))
    with pytest.raises(Exception, match="outside the allowed base"):
        await api._stage_subtitles({"es": str(secret)}, str(base))

    # Symlink: a link INSIDE the base pointing at the same file outside it.
    link = base / "link.srt"
    link.symlink_to(secret)
    with pytest.raises(Exception, match="outside the allowed base"):
        await api._stage_subtitles({"es": "link.srt"}, str(base))


@respx.mock
async def test_stage_subtitles_honours_the_any_path_optout(monkeypatch, tmp_path):
    """The same documented escape hatch every other input has — otherwise a
    subtitle would be confined more tightly than the video it belongs to."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    monkeypatch.setenv("SONILO_MCP_ALLOW_ANY_PATH", "true")
    from sonilo_mcp import api
    _mock_upload_cap(api)
    base = tmp_path / "base"
    base.mkdir()
    outside = tmp_path / "es.srt"
    outside.write_bytes(b"cues")
    files, _ = await api._stage_subtitles({"es": str(outside)}, str(base))
    assert files["subtitles[es]"][1] == b"cues"


@respx.mock
async def test_dubbing_sends_subtitle_parts_and_export_srt(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    _mock_upload_cap(api)
    (output_dir / "es.srt").write_bytes(b"es-script")
    submit = respx.post("https://api.test.local/v1/dubbing").mock(
        return_value=httpx.Response(202, json={"task_id": "db-5", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/db-5").mock(
        return_value=httpx.Response(200, json={
            "task_id": "db-5", "type": "dubbing", "status": "succeeded",
            "outputs": {"es": "https://r2.test/es.mp4"},
            "subtitles": {"es": "https://r2.test/es.srt"},
            "subtitle_export": {
                "es": {"status": "exported", "alignment_loss": "0.25"},
            },
        })
    )
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    respx.get("https://r2.test/es.srt").mock(
        return_value=httpx.Response(200, content=b"es-cues")
    )
    result = await api.dubbing(
        video_url="https://example.com/clip.mp4",
        languages=["es", "fr"],
        subtitles={"es": "es.srt", "fr": "https://example.com/fr.vtt"},
        export_srt=True,
    )
    sent = submit.calls.last.request.content
    # The local path rides as a file part, the https value as a text field —
    # both under the bracketed field name the backend parses.
    assert b'name="subtitles[es]"; filename="es.srt"' in sent
    assert b"es-script" in sent
    assert b'name="subtitles[fr]"\r\n\r\nhttps://example.com/fr.vtt' in sent
    assert b'name="export_srt"\r\n\r\ntrue' in sent
    assert (output_dir / "dubbing-db-5.es.srt").read_bytes() == b"es-cues"
    assert "alignment loss 0.250" in result[1].text
    # The submit response is not the finished task: no preflight is surfaced
    # here, only what the terminal body reports.
    assert len(result) == 2


@respx.mock
async def test_dubbing_sends_lipsync_only_when_set(monkeypatch, output_dir):
    """The mirror of ducking, with the default the other way up: absent must
    mean lip sync ON, which is what every dubbing call did before the
    parameter existed."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/dubbing").mock(
        return_value=httpx.Response(202, json={"task_id": "db-4", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/db-4").mock(
        return_value=httpx.Response(200, json={
            "task_id": "db-4", "type": "dubbing", "status": "succeeded",
            "outputs": {"es": "https://r2.test/es.mp4"},
        })
    )
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )

    await api.dubbing(video_url="https://example.com/clip.mp4", lipsync=False)
    body = submit.calls.last.request.content
    assert b'name="lipsync"\r\n\r\nfalse' in body or b"lipsync=false" in body

    await api.dubbing(video_url="https://example.com/clip.mp4")
    assert b"lipsync" not in submit.calls.last.request.content


@respx.mock
async def test_dubbing_omits_the_subtitle_fields_when_unset(monkeypatch, output_dir):
    """Unset means not on the wire at all, so a plain dub is byte-for-byte
    the request it was before these parameters existed."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/dubbing").mock(
        return_value=httpx.Response(202, json={"task_id": "db-6", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/db-6").mock(
        return_value=httpx.Response(200, json={
            "task_id": "db-6", "type": "dubbing", "status": "succeeded",
            "outputs": {"es": "https://r2.test/es.mp4"},
        })
    )
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    await api.dubbing(video_url="https://example.com/clip.mp4")
    assert b"export_srt" not in submit.calls.last.request.content
    assert b"subtitles" not in submit.calls.last.request.content


async def test_dubbing_rejects_export_srt_true_without_subtitles(
    monkeypatch, output_dir
):
    """A guaranteed 422 — there is nothing to align against — so it never
    leaves the machine.

    Matched on the message, not merely on the word "export_srt": a bare
    `match="export_srt"` is satisfied by TypeError: unexpected keyword
    argument, so the test would pass against a build that never had the
    parameter at all."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp import api
    with pytest.raises(Exception, match="export_srt requires subtitles") as exc:
        await api.dubbing(
            video_url="https://example.com/clip.mp4", export_srt=True
        )
    assert not isinstance(exc.value, TypeError)


@respx.mock
async def test_dubbing_allows_export_srt_false_without_subtitles(
    monkeypatch, output_dir
):
    """export_srt=false without scripts is an ordinary dub — the backend
    reads it as a plain boolean — and an agent that fills in every parameter
    explicitly sends exactly that. Refusing it would block a legal call."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/dubbing").mock(
        return_value=httpx.Response(202, json={"task_id": "db-7", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/db-7").mock(
        return_value=httpx.Response(200, json={
            "task_id": "db-7", "type": "dubbing", "status": "succeeded",
            "outputs": {"es": "https://r2.test/es.mp4"},
        })
    )
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    result = await api.dubbing(
        video_url="https://example.com/clip.mp4",
        languages=["es"],
        ducking=False,
        export_srt=False,
    )
    assert b"export_srt=false" in submit.calls.last.request.content
    assert len(result) == 1


async def test_dubbing_description_documents_the_subtitle_parameters():
    """Read off the FastMCP registry, not the function — @mcp.tool keeps the
    description on the registered tool, not as an attribute of the callable.
    An agent that cannot see these parameters will never offer them."""
    from sonilo_mcp import api

    desc = {t.name: (t.description or "") for t in await api.mcp.list_tools()}["dubbing"]
    assert "subtitles (dict, optional)" in desc
    assert "export_srt (bool, optional)" in desc
    # The scripts are TARGET-language, and export_srt needs them: both are
    # what an agent would otherwise guess wrong.
    assert "TARGET-language" in desc
    assert "Requires subtitles" in desc


async def test_dubbing_rejects_both_inputs(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp import api
    with pytest.raises(Exception, match="exactly one"):
        await api.dubbing(video_path="clip.mp4", video_url="https://example.com/clip.mp4")
    with pytest.raises(Exception, match="exactly one"):
        await api.dubbing()


async def test_dubbing_rejects_a_non_https_url(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp import api
    with pytest.raises(Exception, match="must use https"):
        await api.dubbing(video_url="http://example.com/clip.mp4")


@respx.mock
async def test_dubbing_rejects_a_video_over_300_seconds(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=301.0)
    submit = respx.post("https://api.test.local/v1/dubbing")
    with pytest.raises(Exception):
        await api.dubbing(video_url="https://example.com/clip.mp4")
    # Nothing may be submitted, so nothing is charged.
    assert not submit.called


@respx.mock
async def test_dubbing_sends_a_video_at_the_cap_to_the_backend(monkeypatch, output_dir):
    """The local pre-check exists to save a wasted upload, so its only failure
    mode that costs the caller anything is rejecting what the API would have
    taken. 300s is exactly the length dubbing was raised to accept; the submit
    below 500s on purpose, because reaching the network at all is the proof."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=300.0)
    submit = respx.post("https://api.test.local/v1/dubbing").mock(
        return_value=httpx.Response(500, json={"message": "upstream is down"})
    )
    with pytest.raises(Exception):
        await api.dubbing(video_url="https://example.com/clip.mp4")
    assert submit.called


def test_dubbing_cap_matches_the_backend():
    """A literal, not a re-export of anything: this pre-check exists only to
    save a wasted upload, so it is right exactly when it equals the number the
    backend enforces. Set it too low and we reject videos the API accepts --
    the caller cannot appeal a rejection that never left their machine."""
    from sonilo_mcp import api
    assert api._DUBBING_MAX_VIDEO_DURATION_SECONDS == 300


def test_documented_dubbing_cap_matches_the_code():
    """README and context7.json both state the cap in prose, and an agent
    reading either will refuse a video rather than send it. Prose drifts from
    constants silently -- nothing else in this repo compares the two."""
    from pathlib import Path
    from sonilo_mcp import api

    cap = api._DUBBING_MAX_VIDEO_DURATION_SECONDS
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    context7 = (root / "context7.json").read_text(encoding="utf-8")

    dubbing_line = next(
        line for line in readme.splitlines()
        if line.startswith("| `dubbing(")
    )
    assert f"{cap}s" in dubbing_line, dubbing_line
    assert f"{cap}s for dubbing" in context7


@respx.mock
async def test_dubbing_polls_for_at_least_two_hours(monkeypatch, output_dir):
    """TIME_OUT_SECONDS defaults to 600, but the dubbing backend polls its own
    pipeline for up to 7200s. Giving up at 10 minutes would leave the caller
    charged for videos they never receive."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    monkeypatch.setenv("TIME_OUT_SECONDS", "600")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)
    respx.post("https://api.test.local/v1/dubbing").mock(
        return_value=httpx.Response(202, json={"task_id": "db-3", "status": "processing"})
    )
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    seen: dict = {}

    async def fake_poll(task_id, timeout):
        seen["timeout"] = timeout
        return {
            "task_id": task_id, "type": "dubbing", "status": "succeeded",
            "outputs": {"es": "https://r2.test/es.mp4"},
        }

    monkeypatch.setattr(api, "_poll_task", fake_poll)
    await api.dubbing(video_url="https://example.com/clip.mp4")
    assert seen["timeout"] == 7200.0


@respx.mock
async def test_dubbing_honours_a_larger_operator_timeout(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    monkeypatch.setenv("TIME_OUT_SECONDS", "10800")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=60.0)
    respx.post("https://api.test.local/v1/dubbing").mock(
        return_value=httpx.Response(202, json={"task_id": "db-4", "status": "processing"})
    )
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    seen: dict = {}

    async def fake_poll(task_id, timeout):
        seen["timeout"] = timeout
        return {
            "task_id": task_id, "type": "dubbing", "status": "succeeded",
            "outputs": {"es": "https://r2.test/es.mp4"},
        }

    monkeypatch.setattr(api, "_poll_task", fake_poll)
    await api.dubbing(video_url="https://example.com/clip.mp4")
    assert seen["timeout"] == 10800.0


@respx.mock
async def test_get_sfx_task_recovers_a_dubbing_task(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    respx.get("https://api.test.local/v1/tasks/db-9").mock(
        return_value=httpx.Response(200, json={
            "task_id": "db-9", "type": "dubbing", "status": "succeeded",
            "outputs": {
                "es": "https://r2.test/es.mp4",
                "ja": "https://r2.test/ja.mp4",
            },
        })
    )
    respx.get("https://r2.test/es.mp4").mock(
        return_value=httpx.Response(200, content=b"es-bytes")
    )
    respx.get("https://r2.test/ja.mp4").mock(
        return_value=httpx.Response(200, content=b"ja-bytes")
    )
    result = await api.get_sfx_task("db-9")
    assert len(result) == 2
    assert (output_dir / "dubbing-db-9.es.mp4").read_bytes() == b"es-bytes"
    assert (output_dir / "dubbing-db-9.ja.mp4").read_bytes() == b"ja-bytes"


# ---------- mp3 container, v2s output_format, v2v-music ducking/segments ----------

def _v2v_music_stub(monkeypatch, output_dir):
    """Wire up the common respx/ffprobe stubs for a video-to-video-music call
    and return the submit route so a test can inspect the multipart body."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-to-video-music").mock(
        return_value=httpx.Response(202, json={"task_id": "d1", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/d1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "d1", "type": "video_to_video_music", "status": "succeeded",
            "video": {
                "url": "https://r2.test/o.mp4", "content_type": "video/mp4",
                "file_size": 3,
            },
            "duration_seconds": 5.0,
        })
    )
    respx.get("https://r2.test/o.mp4").mock(
        return_value=httpx.Response(200, content=b"video-bytes")
    )
    return api, submit


@respx.mock
async def test_v2v_music_omits_ducking_by_default(monkeypatch, output_dir):
    """ducking defaults False to match the backend, so the default call must
    send nothing. An explicit "false" would be pointless, and an explicit
    "true" would pin a default that belongs to the server."""
    api, submit = _v2v_music_stub(monkeypatch, output_dir)
    await api.video_to_video_music(video_url="https://example.com/c.mp4")
    assert b"ducking" not in submit.calls.last.request.content


@respx.mock
async def test_v2v_music_omits_keep_original_sound_by_default(monkeypatch, output_dir):
    """Default-OFF on the backend, so the default call sends nothing and the
    delivered video's audio is the generated music alone."""
    api, submit = _v2v_music_stub(monkeypatch, output_dir)
    await api.video_to_video_music(video_url="https://example.com/c.mp4")
    assert b"keep_original_sound" not in submit.calls.last.request.content


@respx.mock
async def test_v2v_music_sends_keep_original_sound_when_opted_in(monkeypatch, output_dir):
    api, submit = _v2v_music_stub(monkeypatch, output_dir)
    await api.video_to_video_music(
        video_url="https://example.com/c.mp4", keep_original_sound=True
    )
    body = submit.calls.last.request.content
    assert b'name="keep_original_sound"\r\n\r\ntrue' in body or \
        b"keep_original_sound=true" in body


@respx.mock
async def test_v2v_music_static_mix_row(monkeypatch, output_dir):
    """keep_original_sound alone is the static-mix row: the whole original
    track, mixed at a fixed offset rather than ducked. Now that ducking is
    default-off it takes no second flag, and nothing pins it on the wire."""
    api, submit = _v2v_music_stub(monkeypatch, output_dir)
    await api.video_to_video_music(
        video_url="https://example.com/c.mp4",
        keep_original_sound=True,
    )
    body = submit.calls.last.request.content
    assert b"keep_original_sound" in body
    assert b"ducking" not in body


def test_audio_sound_tool_does_not_expose_keep_original_sound():
    """keep_original_sound is video-only: it only means something when the
    deliverable is a video whose own audio could be preserved. video_to_sound
    must not expose it — the mirror of how output_format is kept off
    video_to_video_sound. Asserted on the signatures so adding it by reflex
    fails here rather than shipping a field the backend silently drops."""
    import inspect

    from sonilo_mcp import api

    assert "keep_original_sound" not in inspect.signature(api.video_to_sound).parameters
    assert "keep_original_sound" in inspect.signature(api.video_to_video_sound).parameters
    assert "keep_original_sound" in inspect.signature(api.video_to_video_music).parameters


def test_prompt_influence_only_on_the_two_video_music_tools():
    """prompt_influence is a music-generation upstream param: only
    video_to_music and video_to_video_music forward it. Asserted on the
    signatures so adding it by reflex to a tool whose backend endpoint
    silently drops (or 422s on) the field fails here first — the mirror of
    the keep_original_sound exposure test above."""
    import inspect

    from sonilo_mcp import api

    assert "prompt_influence" in inspect.signature(api.video_to_music).parameters
    assert "prompt_influence" in inspect.signature(api.video_to_video_music).parameters
    for tool in (
        api.text_to_music,
        api.text_to_sfx,
        api.video_to_sfx,
        api.video_to_video_sfx,
        api.video_to_sound,
        api.video_to_video_sound,
        api.dubbing,
    ):
        assert "prompt_influence" not in inspect.signature(tool).parameters


def test_stems_only_on_the_two_music_tools():
    """stems is accepted only by /v1/text-to-music and /v1/video-to-music.
    Asserted on the signatures so adding it by reflex to a tool whose
    backend endpoint silently drops (or 422s on) the field fails here
    first — the mirror of the prompt_influence exposure test above."""
    import inspect

    from sonilo_mcp import api

    assert "stems" in inspect.signature(api.text_to_music).parameters
    assert "stems" in inspect.signature(api.video_to_music).parameters
    for tool in (
        api.text_to_sfx,
        api.video_to_sfx,
        api.video_to_video_music,
        api.video_to_video_sfx,
        api.video_to_sound,
        api.video_to_video_sound,
        api.dubbing,
        api.analyze_video,
        api.audio_ducking,
    ):
        assert "stems" not in inspect.signature(tool).parameters


async def test_stems_descriptions_carry_the_stems_error_guidance():
    """An agent that gets audio plus a bare stems_error string would
    otherwise report the whole generation as broken — the descriptions must
    teach that stems_error means only the free separation failed or was
    skipped. Asserted on the REGISTERED tool descriptions (what the MCP
    client actually sees), not on any docstring."""
    from sonilo_mcp import api

    tools = {t.name: t for t in await api.mcp.list_tools()}
    for name in ("text_to_music", "video_to_music"):
        desc = tools[name].description
        assert "stems_error" in desc
        assert "missing extra, not as a failed generation" in desc
        assert "drums, bass, vocals and other" in desc
        assert "no extra charge" in desc
    # video_to_music must additionally say WHAT gets split: the generated
    # music, never the video's own audio.
    assert "never the video's own audio" in tools["video_to_music"].description
    assert "never the video's own audio" not in tools["text_to_music"].description


@respx.mock
async def test_v2v_music_sends_prompt_influence_when_set(monkeypatch, output_dir):
    api, submit = _v2v_music_stub(monkeypatch, output_dir)
    await api.video_to_video_music(
        video_url="https://example.com/c.mp4", prompt_influence=0.9
    )
    assert b"prompt_influence=0.9" in submit.calls.last.request.content


@respx.mock
async def test_v2v_music_sends_prompt_influence_zero(monkeypatch, output_dir):
    """0.0 is a meaningful value — the `is not None` gate must send it; a
    truthiness gate would drop it and hand back the API's 0.5 default."""
    api, submit = _v2v_music_stub(monkeypatch, output_dir)
    await api.video_to_video_music(
        video_url="https://example.com/c.mp4", prompt_influence=0.0
    )
    assert b"prompt_influence=0.0" in submit.calls.last.request.content


@respx.mock
async def test_v2v_music_omits_prompt_influence_by_default(monkeypatch, output_dir):
    """Unset means not on the wire at all: the API's own 0.5 default is the
    long-standing behavior and stays the backend's call."""
    api, submit = _v2v_music_stub(monkeypatch, output_dir)
    await api.video_to_video_music(video_url="https://example.com/c.mp4")
    assert b"prompt_influence" not in submit.calls.last.request.content


@pytest.mark.parametrize("bad", [-0.1, 1.5])
async def test_v2v_music_rejects_out_of_range_prompt_influence(output_dir, bad):
    from sonilo_mcp.api import video_to_video_music
    with pytest.raises(Exception, match="prompt_influence must be between 0 and 1"):
        await video_to_video_music(
            video_url="https://example.com/c.mp4", prompt_influence=bad
        )


@respx.mock
async def test_v2v_music_sends_ducking_true_when_opted_in(monkeypatch, output_dir):
    api, submit = _v2v_music_stub(monkeypatch, output_dir)
    await api.video_to_video_music(
        video_url="https://example.com/c.mp4", ducking=True
    )
    assert b"ducking=true" in submit.calls.last.request.content


@respx.mock
async def test_v2v_music_omits_ducking_when_explicitly_false(monkeypatch, output_dir):
    """An explicit False matches the backend default, so it stays off the wire
    — the tool pins nothing the server already decides."""
    api, submit = _v2v_music_stub(monkeypatch, output_dir)
    await api.video_to_video_music(
        video_url="https://example.com/c.mp4", ducking=False
    )
    assert b"ducking" not in submit.calls.last.request.content


@respx.mock
async def test_v2v_music_serializes_segments(monkeypatch, output_dir):
    api, submit = _v2v_music_stub(monkeypatch, output_dir)
    await api.video_to_video_music(
        video_url="https://example.com/c.mp4",
        segments=[{"start": 0, "prompt": "sparse pads", "label": "intro"}],
    )
    # The body is form-encoded, so decode before asserting on the JSON.
    from urllib.parse import parse_qs

    fields = parse_qs(submit.calls.last.request.content.decode())
    assert json.loads(fields["segments"][0]) == [
        {"start": 0, "prompt": "sparse pads", "label": "intro"}
    ]


def test_non_m4a_output_format_forces_async_on_text_to_music():
    """The gate used to name 'wav' specifically; mp3 would have streamed and
    been silently ignored. Asserted on the source since the branch is a plain
    conditional with no seam to stub."""
    import inspect

    from sonilo_mcp import api

    src = inspect.getsource(api.text_to_music)
    assert 'output_format != "m4a"' in src
    assert 'output_format == "wav"' not in src


def test_video_to_video_sound_never_exposes_output_format():
    """Only the audio endpoint takes it — video_to_video_sound always muxes
    the mix into an mp4."""
    import inspect

    from sonilo_mcp import api

    assert "output_format" in inspect.signature(api.video_to_sound).parameters
    assert (
        "output_format" not in inspect.signature(api.video_to_video_sound).parameters
    )


# ---------- Authenticating from the `sonilo login` credential ----------

def _write_credentials(path, *, api_key: str, api_base: str = "https://api.sonilo.com"):
    """Write the shared credential file the CLIs produce (see credentials.py)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "credentials": {
                    api_base: {
                        "api_key": api_key,
                        "key_id": "key-1",
                        "account_id": "acct-1",
                        "account_name": "Acme",
                        "expires_at": "2026-11-09T04:12:00Z",
                        "created_at": "2026-08-11T04:12:00Z",
                        "created_by": "sonilo-cli/0.12.0",
                    }
                },
            }
        )
    )


def test_get_config_prefers_the_env_var_over_the_stored_credential(tmp_path, monkeypatch):
    """The compatibility guarantee: an existing host config keeps working."""
    monkeypatch.setenv("SONILO_API_KEY", "sk-from-env")
    from sonilo_mcp.credentials import credentials_path
    _write_credentials(credentials_path(), api_key="sk-from-file")

    from sonilo_mcp.api import _get_config
    assert _get_config()["api_key"] == "sk-from-env"


def test_get_config_falls_back_to_the_stored_credential(tmp_path):
    from sonilo_mcp.credentials import credentials_path
    _write_credentials(credentials_path(), api_key="sk-from-file")

    from sonilo_mcp.api import _get_config
    assert _get_config()["api_key"] == "sk-from-file"


def test_get_config_matches_the_credential_to_the_configured_api_url(tmp_path, monkeypatch):
    monkeypatch.setenv("SONILO_API_URL", "https://api.staging.sonilo.com")
    from sonilo_mcp.credentials import credentials_path
    _write_credentials(credentials_path(), api_key="sk-prod")  # written for prod

    from sonilo_mcp.api import _get_config
    assert _get_config()["api_key"] is None


def test_get_config_tolerates_a_trailing_slash_on_the_api_url(tmp_path, monkeypatch):
    """The CLIs strip trailing slashes before keying the store, so the server
    must too or a `SONILO_API_URL=…/` config would never find its credential."""
    monkeypatch.setenv("SONILO_API_URL", "https://api.sonilo.com/")
    from sonilo_mcp.credentials import credentials_path
    _write_credentials(credentials_path(), api_key="sk-from-file")

    from sonilo_mcp.api import _get_config
    assert _get_config()["api_key"] == "sk-from-file"


@respx.mock
async def test_requests_use_the_stored_credential(tmp_path):
    from sonilo_mcp.credentials import credentials_path
    _write_credentials(credentials_path(), api_key="sk-from-file")

    route = respx.get("https://api.sonilo.com/v1/account/usage").mock(
        return_value=httpx.Response(200, json={"ok": True})
    )
    from sonilo_mcp.api import _http_get_json
    await _http_get_json("/v1/account/usage")
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-from-file"


@respx.mock
async def test_missing_key_message_mentions_both_ways_in(tmp_path):
    from sonilo_mcp.api import _http_get_json
    with pytest.raises(Exception, match="SONILO_API_KEY"):
        await _http_get_json("/v1/account/usage")
    with pytest.raises(Exception, match="sonilo login"):
        await _http_get_json("/v1/account/usage")


@respx.mock
async def test_task_submit_uses_the_stored_credential(tmp_path):
    """The gate is duplicated across three helpers; a missed one would leave
    one tool family unable to use a sign-in."""
    from sonilo_mcp.credentials import credentials_path
    _write_credentials(credentials_path(), api_key="sk-from-file")

    route = respx.post("https://api.sonilo.com/v1/text-to-sfx").mock(
        return_value=httpx.Response(202, json={"task_id": "t1"})
    )
    from sonilo_mcp.api import _post_task_submit
    assert await _post_task_submit("/v1/text-to-sfx", data={"prompt": "x"}) == "t1"
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-from-file"


@respx.mock
async def test_streaming_generation_uses_the_stored_credential(output_dir):
    from sonilo_mcp.credentials import credentials_path
    _write_credentials(credentials_path(), api_key="sk-from-file")

    ndjson = (
        json.dumps({
            "type": "audio_chunk", "stream_index": 0, "num_streams": 1,
            "data": base64.b64encode(b"x").decode(),
        }) + "\n"
        + json.dumps({"type": "complete"}) + "\n"
    ).encode()
    route = respx.post("https://api.sonilo.com/v1/text-to-music").mock(
        return_value=httpx.Response(200, content=ndjson)
    )
    from sonilo_mcp.api import _post_streaming_generation
    await _post_streaming_generation(
        "/v1/text-to-music", output_dir, data={"prompt": "p", "duration": 5}
    )
    assert route.calls.last.request.headers["authorization"] == "Bearer sk-from-file"


# ---------- video analysis ----------

ANALYSIS_BODY = {
    "task_id": "va-1",
    "type": "video_analysis",
    "status": "succeeded",
    "variants_num": 2,
    "segments": [
        {"start": 0, "end": 12, "label": "intro", "prompt": "sparse piano"},
        {"start": 12, "end": 30, "label": "none", "prompt": "full strings"},
    ],
    "variations": [
        {"prompt": "cinematic strings, 90bpm"},
        {"prompt": "lo-fi hip hop, warm keys"},
    ],
    "duration_seconds": 30.0,
}


def test_is_analysis_envelope_recognizes_the_brief():
    from sonilo_mcp import api
    assert api._is_analysis_envelope(ANALYSIS_BODY) is True
    assert api._is_analysis_envelope({"task_id": "x", "status": "succeeded"}) is False
    # An explicit, different type is authoritative and short-circuits, exactly
    # as _is_dubbing_envelope does — a task carrying a `variations`-shaped
    # field must not get misrouted to this envelope's renderer.
    assert api._is_analysis_envelope(
        {"type": "video_to_sfx", "variations": [{"prompt": "p"}]}
    ) is False
    # No `type` at all -> fall back to sniffing the shape.
    assert api._is_analysis_envelope({"variations": [{"prompt": "p"}]}) is True
    assert api._is_analysis_envelope({"variations": []}) is False
    assert api._is_analysis_envelope({"variations": [{"no_prompt": 1}]}) is False


@respx.mock
async def test_analyze_video_returns_the_brief_inline(monkeypatch, output_dir):
    """The result is a brief, not media: it comes back as text and nothing is
    written to SONILO_MCP_BASE_PATH."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=30.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    respx.post("https://api.test.local/v1/video-analysis").mock(
        return_value=httpx.Response(202, json={"task_id": "va-1", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/va-1").mock(
        return_value=httpx.Response(200, json=ANALYSIS_BODY)
    )

    result = await api.analyze_video(video_url="https://example.com/clip.mp4")

    assert len(result) == 1
    brief = json.loads(result[0].text)
    assert brief["task_id"] == "va-1"
    assert brief["segments"][0] == {
        "start": 0, "end": 12, "label": "intro", "prompt": "sparse piano",
    }
    assert [v["prompt"] for v in brief["variations"]] == [
        "cinematic strings, 90bpm", "lo-fi hip hop, warm keys",
    ]
    assert list(output_dir.iterdir()) == []


@respx.mock
async def test_analyze_video_sends_prompt_and_variants(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=30.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-analysis").mock(
        return_value=httpx.Response(202, json={"task_id": "va-1", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/va-1").mock(
        return_value=httpx.Response(200, json=ANALYSIS_BODY)
    )

    await api.analyze_video(
        video_url="https://example.com/clip.mp4",
        prompt="focus on the chase",
        variants_num=2,
    )

    from urllib.parse import unquote_plus
    sent = unquote_plus(submit.calls.last.request.content.decode())
    assert "prompt=focus on the chase" in sent
    assert "variants_num=2" in sent


@respx.mock
async def test_analyze_video_omits_unset_optionals(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=30.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-analysis").mock(
        return_value=httpx.Response(202, json={"task_id": "va-1", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/va-1").mock(
        return_value=httpx.Response(200, json=ANALYSIS_BODY)
    )

    await api.analyze_video(video_url="https://example.com/clip.mp4")

    sent = submit.calls.last.request.content.decode()
    assert "prompt" not in sent
    assert "variants_num" not in sent


@respx.mock
async def test_analyze_video_sends_mode_when_set(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=30.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-analysis").mock(
        return_value=httpx.Response(202, json={"task_id": "va-1", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/va-1").mock(
        return_value=httpx.Response(200, json=ANALYSIS_BODY)
    )

    await api.analyze_video(video_url="https://example.com/clip.mp4", mode="sfx")

    from urllib.parse import unquote_plus
    sent = unquote_plus(submit.calls.last.request.content.decode())
    assert "mode=sfx" in sent


@respx.mock
async def test_analyze_video_omits_unset_mode(monkeypatch, output_dir):
    """The server default (both) applies when the field is absent, and an
    unset call stays byte-identical to what it sent before mode existed."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=30.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/video-analysis").mock(
        return_value=httpx.Response(202, json={"task_id": "va-1", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/va-1").mock(
        return_value=httpx.Response(200, json=ANALYSIS_BODY)
    )

    await api.analyze_video(video_url="https://example.com/clip.mp4")

    sent = submit.calls.last.request.content.decode()
    assert "mode" not in sent


async def test_analyze_video_rejects_an_unknown_mode(output_dir):
    """Checked before any I/O and case-sensitively: the backend would 422 a
    'MUSIC' too, but only after the video had been uploaded."""
    from sonilo_mcp import api
    with pytest.raises(Exception, match="mode must be one of"):
        await api.analyze_video(video_url="https://x/c.mp4", mode="MUSIC")
    with pytest.raises(Exception, match="mode must be one of"):
        await api.analyze_video(video_url="https://x/c.mp4", mode="sound")


def test_analysis_brief_passes_the_sound_design_brief_through():
    """_analysis_brief re-emits the wire shape instead of forwarding the
    body, so the `both`-mode keys have to be listed explicitly or the caller
    pays for a sound-design brief that is silently dropped. `sfx_prompt` is
    one string, not one per variation."""
    from sonilo_mcp import api
    body = dict(
        ANALYSIS_BODY,
        mode="both",
        sfx_segments=[
            {"start": 0, "end": 12, "label": "none", "prompt": "wind, distant traffic"},
            {"start": 12, "end": 30, "prompt": "tyre squeal, engine roar"},
        ],
        sfx_prompt="urban chase: engines, horns, glass",
    )

    brief = json.loads(api._analysis_brief(body)[0].text)

    assert brief["mode"] == "both"
    assert brief["sfx_prompt"] == "urban chase: engines, horns, glass"
    assert brief["sfx_segments"] == [
        {"start": 0, "end": 12, "label": "none", "prompt": "wind, distant traffic"},
        {"start": 12, "end": 30, "label": "none", "prompt": "tyre squeal, engine roar"},
    ]
    # music / sfx mode bodies carry neither key, and the brief must not
    # invent them.
    music = json.loads(api._analysis_brief(dict(ANALYSIS_BODY, mode="music"))[0].text)
    assert music["mode"] == "music"
    assert "sfx_segments" not in music
    assert "sfx_prompt" not in music


async def test_analyze_video_requires_exactly_one_input(output_dir):
    from sonilo_mcp import api
    with pytest.raises(Exception, match="exactly one"):
        await api.analyze_video()
    with pytest.raises(Exception, match="exactly one"):
        await api.analyze_video(video_path="clip.mp4", video_url="https://x/c.mp4")


async def test_analyze_video_rejects_variants_outside_1_to_5(output_dir):
    """This endpoint caps variants at 5, unlike the music endpoints' 10 — so
    the shared _validate_variants_num cannot be reused here."""
    from sonilo_mcp import api
    with pytest.raises(Exception, match="between 1 and 5"):
        await api.analyze_video(video_url="https://x/c.mp4", variants_num=6)
    with pytest.raises(Exception, match="between 1 and 5"):
        await api.analyze_video(video_url="https://x/c.mp4", variants_num=0)


async def test_analyze_video_rejects_an_over_long_video(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=500.0)
    with pytest.raises(Exception, match="480"):
        await api.analyze_video(video_url="https://example.com/clip.mp4")


@respx.mock
async def test_analyze_video_sends_a_video_at_the_cap_to_the_backend(
    monkeypatch, output_dir
):
    """The pre-check's only failure mode that costs the caller anything is
    rejecting what the API would have taken. 480s is exactly the length
    /v1/video-analysis accepts; the submit 500s on purpose, because reaching
    the network at all is the proof."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=480.0)
    submit = respx.post("https://api.test.local/v1/video-analysis").mock(
        return_value=httpx.Response(500, json={"message": "upstream is down"})
    )
    with pytest.raises(Exception):
        await api.analyze_video(video_url="https://example.com/clip.mp4")
    assert submit.called


def test_analysis_cap_matches_the_backend():
    """A literal, not a re-export: this pre-check is right exactly when it
    equals the number the backend enforces. Set it too low and we reject
    videos the API accepts -- the caller cannot appeal a rejection that never
    left their machine. 480 is the backend's cap since video-analysis
    gained mode=both; the shared ffprobe ceiling was raised with it."""
    from sonilo_mcp import api
    assert api._ANALYSIS_MAX_VIDEO_DURATION_SECONDS == 480


def _context7_cap_clause(context7: str, cap: int) -> str:
    """The tools context7.json lists under one duration cap: the text between
    `<cap>s for ` and the next `;` in the per-tool caps rule. Several tools
    share a cap, so a test pins membership in the clause rather than a fixed
    position in it."""
    marker = f"{cap}s for "
    assert marker in context7, marker
    rest = context7.split(marker, 1)[1]
    return rest.split(";", 1)[0]


def test_documented_analysis_cap_matches_the_code():
    """README and context7.json both state the cap in prose, and an agent
    reading either will refuse a video rather than send it. Prose drifts from
    constants silently."""
    from pathlib import Path
    from sonilo_mcp import api

    cap = api._ANALYSIS_MAX_VIDEO_DURATION_SECONDS
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    context7 = (root / "context7.json").read_text(encoding="utf-8")

    analyze_line = next(
        line for line in readme.splitlines()
        if line.startswith("| `analyze_video(")
    )
    assert f"{cap}s" in analyze_line, analyze_line
    assert "analyze_video" in _context7_cap_clause(context7, cap)


@respx.mock
async def test_get_sfx_task_recovers_an_analysis_task(monkeypatch, output_dir):
    """A video-analysis task has no artifact at all — without its own branch
    the recovery tool would report a missing artifact for a task that was
    charged and whose brief is sitting right there in the body."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    respx.get("https://api.test.local/v1/tasks/va-1").mock(
        return_value=httpx.Response(200, json=ANALYSIS_BODY)
    )

    result = await api.get_sfx_task("va-1")

    brief = json.loads(result[0].text)
    assert [v["prompt"] for v in brief["variations"]] == [
        "cinematic strings, 90bpm", "lo-fi hip hop, warm keys",
    ]
    assert list(output_dir.iterdir()) == []


def test_sfx_and_sound_caps_match_the_backend():
    """These two gate locally, BEFORE any request leaves the machine, so a
    stale constant refuses a video the API would have accepted and the caller
    cannot appeal a rejection that never reached us. The backend raised both
    from 180 to 480 sec (api-dashboard PR #328)."""
    from sonilo_mcp import api
    assert api._SFX_MAX_VIDEO_DURATION_SECONDS == 480
    assert api._SOUND_MAX_VIDEO_DURATION_SECONDS == 480


def test_documented_sfx_and_sound_caps_match_the_code():
    """Same drift risk the dubbing guard covers: an agent reads the README row
    or the context7 sentence and refuses a video rather than sending it."""
    from pathlib import Path
    from sonilo_mcp import api

    cap = api._SFX_MAX_VIDEO_DURATION_SECONDS
    assert api._SOUND_MAX_VIDEO_DURATION_SECONDS == cap
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    context7 = (root / "context7.json").read_text(encoding="utf-8")

    for tool in (
        "video_to_sfx(", "video_to_video_sfx(",
        "video_to_sound(", "video_to_video_sound(",
    ):
        line = next(
            l for l in readme.splitlines() if l.startswith(f"| `{tool}")
        )
        assert f"{cap}s" in line, line
    clause = _context7_cap_clause(context7, cap)
    for tool in ("video_to_sfx", "video_to_video_sfx", "video_to_sound", "video_to_video_sound"):
        assert tool in clause, clause


async def test_tool_descriptions_state_each_cap_as_enforced():
    """The description is what an agent reads before deciding whether to even
    try, so it drifting from the enforced constant is a real failure mode:
    dubbing's said 180 sec while its own constant enforced 300, and an agent
    reading it would trim a 4-minute video for no reason.

    Read off the FastMCP registry, not the function — @mcp.tool keeps the
    description on the registered tool, not as an attribute of the callable."""
    from sonilo_mcp import api

    expected = {
        "video_to_sfx": api._SFX_MAX_VIDEO_DURATION_SECONDS,
        "video_to_video_sfx": api._SFX_MAX_VIDEO_DURATION_SECONDS,
        "video_to_sound": api._SOUND_MAX_VIDEO_DURATION_SECONDS,
        "video_to_video_sound": api._SOUND_MAX_VIDEO_DURATION_SECONDS,
        "dubbing": api._DUBBING_MAX_VIDEO_DURATION_SECONDS,
        "proofread": api._PROOFREAD_MAX_VIDEO_DURATION_SECONDS,
    }
    by_name = {t.name: (t.description or "") for t in await api.mcp.list_tools()}
    for name, cap in expected.items():
        assert name in by_name, name
        assert f"Maximum video duration is {cap} seconds" in by_name[name], name


# --- optional duration ------------------------------------------------------
# The API resolves an omitted duration itself (text-to-music from the prompt,
# text-to-sfx with its own default), so the tool must leave the field out
# rather than send a stand-in.

@respx.mock
async def test_text_to_music_omits_an_absent_duration(monkeypatch, output_dir):
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
    await text_to_music(prompt="happy")

    assert b"duration" not in route.calls.last.request.content


@respx.mock
async def test_text_to_sfx_omits_an_absent_duration(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.post("https://api.test.local/v1/text-to-sfx").mock(
        return_value=httpx.Response(202, json={"task_id": "t1", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/t1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t1", "status": "succeeded",
            "audio": {"url": "https://cdn.test.local/a.m4a",
                      "content_type": "audio/mp4"},
        })
    )
    respx.get("https://cdn.test.local/a.m4a").mock(
        return_value=httpx.Response(200, content=b"sfxbytes")
    )
    from sonilo_mcp.api import text_to_sfx
    await text_to_sfx(prompt="a door latch")

    assert b"duration" not in route.calls.last.request.content


@respx.mock
async def test_text_to_sfx_sends_a_fractional_duration(monkeypatch, output_dir):
    """The API's floor is 0.5 sec, so this is a number, not an integer."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    route = respx.post("https://api.test.local/v1/text-to-sfx").mock(
        return_value=httpx.Response(202, json={"task_id": "t1", "status": "processing"})
    )
    respx.get("https://api.test.local/v1/tasks/t1").mock(
        return_value=httpx.Response(200, json={
            "task_id": "t1", "status": "succeeded",
            "audio": {"url": "https://cdn.test.local/a.m4a",
                      "content_type": "audio/mp4"},
        })
    )
    respx.get("https://cdn.test.local/a.m4a").mock(
        return_value=httpx.Response(200, content=b"sfxbytes")
    )
    from sonilo_mcp.api import text_to_sfx
    await text_to_sfx(prompt="a door latch", duration=0.5)

    assert b"duration=0.5" in route.calls.last.request.content


# ---------- proofread ----------

# The exact finished-task envelope a real /v1/proofread task returned in
# production (URLs shortened): 7 languages including the detected source
# language, 65 cues, and one non-blocking warning. Copied verbatim rather
# than hand-written so the save layer is exercised against the shape the
# backend actually sends, not a shape convenient to test.
_PROOFREAD_BODY = {
    "task_id": "pr-1",
    "type": "proofread",
    "status": "succeeded",
    "duration_seconds": 206.32,
    "source_language": "en",
    "subtitles": {
        "en": "https://r2.test/en.srt",
        "ko": "https://r2.test/ko.srt",
        "fr": "https://r2.test/fr.srt",
        "de": "https://r2.test/de.srt",
        "ar": "https://r2.test/ar.srt",
        "th": "https://r2.test/th.srt",
        "ru": "https://r2.test/ru.srt",
    },
    "cue_count": 65,
    "warnings": {
        "fr": [
            {
                "cue": 33,
                "code": "high_text_speed",
                "severity": "warning",
                "characters_per_second": 26.92,
            }
        ]
    },
}


def _mock_proofread_srt_downloads(languages=None) -> None:
    """Serve one .srt per language in the finished-task envelope."""
    for language in languages or _PROOFREAD_BODY["subtitles"]:
        respx.get(f"https://r2.test/{language}.srt").mock(
            return_value=httpx.Response(200, content=f"{language}-cues".encode())
        )


def _proofread_stub(monkeypatch, body=None, task_id="pr-1"):
    """Wire up the common respx/ffprobe stubs for a proofread call and return
    (api, submit route)."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api

    _patch_ffprobe(monkeypatch, duration=60.0)

    async def no_sleep(s):
        pass

    monkeypatch.setattr(api, "_poll_sleep", no_sleep)
    submit = respx.post("https://api.test.local/v1/proofread").mock(
        return_value=httpx.Response(
            202, json={"task_id": task_id, "status": "processing"}
        )
    )
    respx.get(f"https://api.test.local/v1/tasks/{task_id}").mock(
        return_value=httpx.Response(200, json=body or _PROOFREAD_BODY)
    )
    return api, submit


@respx.mock
async def test_proofread_url_mode_saves_every_language_and_reports_the_result(
    monkeypatch, output_dir
):
    """The whole point of the tool is the files: one .srt per entry of
    `subtitles`, which always includes the DETECTED source language on top of
    the requested targets. The summary and the warning ride after them."""
    api, submit = _proofread_stub(monkeypatch)
    _mock_proofread_srt_downloads()

    result = await api.proofread(
        video_url="https://example.com/clip.mp4",
        languages=["ko", "fr", "de", "ar", "th", "ru"],
    )

    # URL-only submissions ride as application/x-www-form-urlencoded, so the
    # JSON array arrives percent-encoded — decode before checking it landed.
    from urllib.parse import unquote_plus
    sent = unquote_plus(submit.calls.last.request.content.decode())
    assert '["ko", "fr", "de", "ar", "th", "ru"]' in sent
    assert "video_url=https://example.com/clip.mp4" in sent

    for language in _PROOFREAD_BODY["subtitles"]:
        assert (output_dir / f"proofread-pr-1.{language}.srt").read_bytes() == \
            f"{language}-cues".encode()
    # 7 files + the summary + the one warning.
    assert len(result) == 9
    summary = result[7].text
    assert "Source language: en" in summary
    assert "65 cues" in summary
    # The workflow an agent would otherwise have to guess at.
    assert "dubbing" in summary and "subtitles" in summary
    warning = result[8].text
    assert "fr" in warning and "cue 33" in warning
    assert "high_text_speed" in warning and "warning" in warning
    # Unknown extras are carried through rather than dropped.
    assert "characters_per_second 26.92" in warning


@respx.mock
async def test_proofread_path_mode_uploads_the_video(monkeypatch, output_dir):
    """A local file rides as a multipart file part, with `languages` and
    `source_language` as ordinary text fields beside it."""
    api, submit = _proofread_stub(monkeypatch)
    _mock_upload_cap(api)
    _mock_proofread_srt_downloads()
    (output_dir / "clip.mp4").write_bytes(b"video-bytes")

    await api.proofread(
        video_path="clip.mp4", languages=["ja"], source_language="en"
    )

    sent = submit.calls.last.request.content
    assert b'name="video"; filename="clip.mp4"' in sent
    assert b"video-bytes" in sent
    assert b'name="languages"\r\n\r\n["ja"]' in sent
    assert b'name="source_language"\r\n\r\nen' in sent


@respx.mock
async def test_proofread_omits_the_optional_fields_when_unset(
    monkeypatch, output_dir
):
    """Unset means not on the wire at all, so the server's own defaults apply:
    transcript only, and a detected source language."""
    api, submit = _proofread_stub(monkeypatch)
    _mock_proofread_srt_downloads()
    await api.proofread(video_url="https://example.com/clip.mp4")
    sent = submit.calls.last.request.content
    assert b"languages" not in sent
    assert b"source_language" not in sent


@respx.mock
async def test_proofread_sends_an_explicit_empty_language_list(
    monkeypatch, output_dir
):
    """`[]` is a real request — the transcript alone — and must not collapse
    into "not sent". Both mean transcript-only today, but the caller asked
    for one of them explicitly and the backend reads them apart."""
    api, submit = _proofread_stub(monkeypatch)
    _mock_proofread_srt_downloads()
    await api.proofread(video_url="https://example.com/clip.mp4", languages=[])
    from urllib.parse import unquote_plus
    assert "languages=[]" in unquote_plus(submit.calls.last.request.content.decode())


@respx.mock
async def test_proofread_does_not_check_the_language_codes(monkeypatch, output_dir):
    """Same rule as dubbing: the backend owns the supported list and rejects
    an unknown code with a 422 before charging. A hardcoded copy here would
    reject a language added later."""
    api, submit = _proofread_stub(monkeypatch)
    _mock_proofread_srt_downloads()
    await api.proofread(
        video_url="https://example.com/clip.mp4",
        languages=["xx_yy"],
        source_language="xx_yy",
    )
    assert submit.called


@respx.mock
async def test_proofread_handles_a_body_without_warnings(monkeypatch, output_dir):
    """`warnings` is empty or absent on a clean run — the common case — and
    the files plus the summary must still come back."""
    body = {k: v for k, v in _PROOFREAD_BODY.items() if k != "warnings"}
    body["subtitles"] = {"en": "https://r2.test/en.srt"}
    api, _ = _proofread_stub(monkeypatch, body=body)
    _mock_proofread_srt_downloads(["en"])
    result = await api.proofread(video_url="https://example.com/clip.mp4")
    assert len(result) == 2
    assert (output_dir / "proofread-pr-1.en.srt").read_bytes() == b"en-cues"
    assert "Source language: en" in result[1].text


@respx.mock
async def test_proofread_reports_a_failed_download_as_a_note(
    monkeypatch, output_dir
):
    """One bad URL must not strand the languages that downloaded fine — the
    task is already charged, and the note carries the task id so the rest can
    be retried."""
    body = dict(_PROOFREAD_BODY)
    body["subtitles"] = {
        "en": "https://r2.test/en.srt",
        "fr": "https://r2.test/fr.srt",
    }
    api, _ = _proofread_stub(monkeypatch, body=body)
    respx.get("https://r2.test/en.srt").mock(
        return_value=httpx.Response(200, content=b"en-cues")
    )
    respx.get("https://r2.test/fr.srt").mock(
        return_value=httpx.Response(500, text="nope")
    )
    result = await api.proofread(video_url="https://example.com/clip.mp4")
    assert (output_dir / "proofread-pr-1.en.srt").read_bytes() == b"en-cues"
    note = next(c.text for c in result if c.text.startswith("Note (fr)"))
    assert "could not be downloaded" in note
    assert 'get_sfx_task("pr-1")' in note


@respx.mock
async def test_proofread_raises_when_no_subtitle_was_returned(
    monkeypatch, output_dir
):
    """A succeeded task with nothing to download is the one case that raises:
    there is no partial result to protect, and the caller needs their task
    id."""
    body = dict(_PROOFREAD_BODY)
    body["subtitles"] = {}
    api, _ = _proofread_stub(monkeypatch, body=body)
    with pytest.raises(Exception, match="no subtitle file was returned"):
        await api.proofread(video_url="https://example.com/clip.mp4")


async def test_proofread_rejects_both_inputs(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp import api
    with pytest.raises(Exception, match="exactly one"):
        await api.proofread(
            video_path="clip.mp4", video_url="https://example.com/clip.mp4"
        )
    with pytest.raises(Exception, match="exactly one"):
        await api.proofread()


async def test_proofread_rejects_a_non_https_url(monkeypatch, output_dir):
    """Same rule as dubbing: the backend fetches the source itself and refuses
    plain http, so an http URL is a guaranteed 422."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    from sonilo_mcp import api
    with pytest.raises(Exception, match="must use https"):
        await api.proofread(video_url="http://example.com/clip.mp4")


@respx.mock
async def test_proofread_rejects_a_video_over_300_seconds(monkeypatch, output_dir):
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=301.0)
    submit = respx.post("https://api.test.local/v1/proofread")
    with pytest.raises(Exception):
        await api.proofread(video_url="https://example.com/clip.mp4")
    # Nothing may be submitted, so nothing is charged.
    assert not submit.called


@respx.mock
async def test_proofread_sends_a_video_at_the_cap_to_the_backend(
    monkeypatch, output_dir
):
    """The local pre-check exists to save a wasted upload, so its only failure
    mode that costs the caller anything is rejecting what the API would have
    taken. 300s is exactly the length /v1/proofread accepts; the submit fails
    with a 500 on purpose, because reaching the network at all is the proof."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    _patch_ffprobe(monkeypatch, duration=300.0)
    submit = respx.post("https://api.test.local/v1/proofread").mock(
        return_value=httpx.Response(500, json={"message": "upstream is down"})
    )
    with pytest.raises(Exception):
        await api.proofread(video_url="https://example.com/clip.mp4")
    assert submit.called


def test_proofread_cap_matches_the_backend():
    """A literal, not a re-export of anything: this pre-check exists only to
    save a wasted upload, so it is right exactly when it equals the number the
    backend enforces."""
    from sonilo_mcp import api
    assert api._PROOFREAD_MAX_VIDEO_DURATION_SECONDS == 300


@respx.mock
async def test_proofread_uses_the_ordinary_timeout(monkeypatch, output_dir):
    """Not dubbing's two-hour floor: proofread only transcribes and
    translates, finishing in well under 90 seconds, so it polls on
    TIME_OUT_SECONDS like every other ordinary task."""
    monkeypatch.setenv("TIME_OUT_SECONDS", "600")
    api, _ = _proofread_stub(monkeypatch)
    _mock_proofread_srt_downloads()
    seen: dict = {}

    async def fake_poll(task_id, timeout):
        seen["timeout"] = timeout
        return _PROOFREAD_BODY

    monkeypatch.setattr(api, "_poll_task", fake_poll)
    await api.proofread(video_url="https://example.com/clip.mp4")
    assert seen["timeout"] == 600.0


@respx.mock
async def test_get_sfx_task_recovers_a_proofread_task(monkeypatch, output_dir):
    """A timed-out proofread is recovered the same way a dubbing task is —
    the scripts are on the backend and the caller was already charged."""
    monkeypatch.setenv("SONILO_API_KEY", "k")
    monkeypatch.setenv("SONILO_API_URL", "https://api.test.local")
    from sonilo_mcp import api
    body = dict(_PROOFREAD_BODY)
    body["task_id"] = "pr-9"
    respx.get("https://api.test.local/v1/tasks/pr-9").mock(
        return_value=httpx.Response(200, json=body)
    )
    _mock_proofread_srt_downloads()
    result = await api.get_sfx_task("pr-9")
    assert len(result) == 9
    assert (output_dir / "proofread-pr-9.en.srt").read_bytes() == b"en-cues"
    assert (output_dir / "proofread-pr-9.ru.srt").read_bytes() == b"ru-cues"
    assert "Source language: en" in result[7].text


async def test_proofread_envelope_never_captures_a_dubbing_task():
    """A dubbing task run with export_srt carries a `subtitles` map too.
    Routing it here would save its scripts and silently drop the dubbed
    videos it was billed for, so the sniff requires that no `outputs` key is
    present — and an explicit type always wins."""
    from sonilo_mcp import api
    dubbed = {
        "outputs": {"es": "https://r2.test/es.mp4"},
        "subtitles": {"es": "https://r2.test/es.srt"},
    }
    assert not api._is_proofread_envelope(dubbed)
    assert not api._is_proofread_envelope({**dubbed, "type": "dubbing"})
    assert api._is_proofread_envelope({"subtitles": {"en": "https://r2.test/en.srt"}})
    assert api._is_proofread_envelope({"type": "proofread", "subtitles": {}})


async def test_proofread_description_documents_the_parameters():
    """Read off the FastMCP registry, not the function — @mcp.tool keeps the
    description on the registered tool, not as an attribute of the callable.
    An agent that cannot see these parameters will never offer them."""
    from sonilo_mcp import api

    desc = {
        t.name: (t.description or "") for t in await api.mcp.list_tools()
    }["proofread"]
    assert "languages (list, optional)" in desc
    assert "source_language (str, optional)" in desc
    assert "output_directory (str, optional)" in desc
    # The three things an agent would otherwise get wrong: it bills per
    # language, it has exactly 2 free runs, and the files are meant to go
    # back into dubbing.
    assert "PER LANGUAGE" in desc
    assert "2 free-trial runs" in desc
    assert "`subtitles` on the dubbing tool" in desc


def test_documented_proofread_cap_matches_the_code():
    """Same drift risk the dubbing guard covers: an agent reads the README row
    or the context7 sentence and refuses a video rather than sending it."""
    from pathlib import Path
    from sonilo_mcp import api

    cap = api._PROOFREAD_MAX_VIDEO_DURATION_SECONDS
    root = Path(__file__).resolve().parent.parent
    readme = (root / "README.md").read_text(encoding="utf-8")
    context7 = (root / "context7.json").read_text(encoding="utf-8")

    line = next(
        l for l in readme.splitlines() if l.startswith("| `proofread(")
    )
    assert f"{cap}s" in line, line
    assert "proofread" in _context7_cap_clause(context7, cap)
