"""Sonilo MCP server — exposes Sonilo's /v1/* API as MCP tools over stdio."""
from __future__ import annotations

import asyncio
import base64
import binascii
import io
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from pathlib import Path
from typing import AsyncIterator
from urllib.parse import urlparse

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

mcp = FastMCP("Sonilo")

try:
    _CLIENT_VERSION = _pkg_version("sonilo-mcp")
except PackageNotFoundError:  # running from source without an installed dist
    _CLIENT_VERSION = "unknown"

# Sent on every request so the backend can attribute traffic to the MCP.
# Only X-Sonilo-Client is trusted by the backend for statistics; User-Agent
# is a convenience so the marker also shows up in access logs.
_CLIENT_HEADERS = {
    "X-Sonilo-Client": "mcp",
    "X-Sonilo-Client-Version": _CLIENT_VERSION,
    "User-Agent": f"sonilo-mcp/{_CLIENT_VERSION}",
}


def _host_headers() -> dict[str, str]:
    """Best-effort headers identifying the MCP *host* app (Claude, Cursor,
    Codex, …).

    The host self-reports a ``clientInfo {name, version}`` during the MCP
    ``initialize`` handshake; we read it from the live session and forward it
    so the backend can attribute traffic to the host. Returns ``{}`` if the
    context/session/clientInfo is unavailable for any reason — host
    attribution must never break a request. Values are length-capped.
    """
    try:
        info = mcp.get_context().session.client_params.clientInfo
    except Exception:
        return {}
    out: dict[str, str] = {}
    name = getattr(info, "name", None)
    version = getattr(info, "version", None)
    if name:
        out["X-Sonilo-Client-Host"] = str(name)[:64]
    if version:
        out["X-Sonilo-Client-Host-Version"] = str(version)[:64]
    return out


# ---------- Configuration ----------

def _get_config() -> dict:
    """Read env vars on each call so tests can monkeypatch freely.

    Mureka reads at module load; we read per-call to keep tests simple.
    The cost is one os.getenv per tool call — negligible.
    """
    base_default = str(Path.home() / "Desktop")
    return {
        "api_key": os.getenv("SONILO_API_KEY"),
        "api_url": os.getenv("SONILO_API_URL", "https://api.sonilo.com"),
        "base_path": os.getenv("SONILO_MCP_BASE_PATH", base_default),
        # Default aligns with the backend's generation read timeout (600s): a
        # long generation can keep running—and charging—on the backend up to
        # 600s, so timing out the client sooner would orphan a paid request.
        "timeout": float(os.getenv("TIME_OUT_SECONDS", "600")),
        "allow_any_path": os.getenv(
            "SONILO_MCP_ALLOW_ANY_PATH", ""
        ).strip().lower() in ("1", "true", "yes", "on"),
    }


# ---------- Path helpers ----------

def _is_file_writeable(path: Path) -> bool:
    if path.exists():
        return os.access(path, os.W_OK)
    return os.access(path.parent if path.parent.exists() else path.parent.parent, os.W_OK)


def _is_within_base(path: Path, base_path: str | None) -> bool:
    """Whether `path` resolves to a location inside `base_path`.

    Confinement is the default: tools may only read/write under
    SONILO_MCP_BASE_PATH, so a confused/compromised client can't read or
    exfiltrate arbitrary files on disk. Returns True (no confinement) when
    SONILO_MCP_ALLOW_ANY_PATH is set, or when no base_path is available.
    Both paths are resolved first so symlinks can't escape the base.
    """
    if _get_config()["allow_any_path"] or not base_path:
        return True
    base = Path(os.path.expanduser(base_path)).resolve()
    try:
        path.resolve().relative_to(base)
        return True
    except ValueError:
        return False


def _make_output_path(output_directory: str | None) -> Path:
    """Resolve output directory per spec rules (see design doc §File I/O Helpers).

    1. absolute path → use as-is
    2. relative path + SONILO_MCP_BASE_PATH set → join
    3. None → SONILO_MCP_BASE_PATH (defaults to ~/Desktop)
    Tilde (~) in either base_path or output_directory is expanded.
    Raises if the resulting directory is not writeable.
    """
    base_path = _get_config()["base_path"]
    if output_directory is None:
        output_path = Path(os.path.expanduser(base_path))
    else:
        expanded = os.path.expanduser(output_directory)
        if os.path.isabs(expanded):
            output_path = Path(expanded)
        else:
            output_path = Path(os.path.expanduser(base_path)) / expanded

    if not _is_within_base(output_path, base_path):
        raise Exception(
            f"Output directory ({output_path}) is outside the allowed base "
            "directory (SONILO_MCP_BASE_PATH). Use a path under it, or set "
            "SONILO_MCP_ALLOW_ANY_PATH=true to allow writing elsewhere."
        )
    if not _is_file_writeable(output_path):
        raise Exception(f"Directory ({output_path}) is not writeable")
    if output_path.exists() and not output_path.is_dir():
        raise Exception(
            f"Output directory ({output_path}) exists but is not a directory"
        )
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


_SLUG_MAX_LEN = 80


def _slugify(text: str) -> str:
    """Filesystem-safe slug. Fallback to 'sonilo' on empty input.

    Capped at _SLUG_MAX_LEN characters so the resulting filename (slug +
    a "-12" collision suffix + an extension like ".m4a"/".mp4") stays well
    under the OS 255-byte filename limit — callers (text_to_sfx,
    video_to_sfx) accept prompts up to 2000 chars, and an uncapped slug of
    that length crashes _artifact_dest's exists() check with a bare OSError.
    `re.ASCII` above already strips non-ASCII characters, so one character
    is always one byte here and a character cap doubles as a byte cap.
    """
    safe = re.sub(r"[^\w\s-]", "", text, flags=re.ASCII).strip().lower()
    safe = re.sub(r"[-\s]+", "-", safe).strip("-")
    safe = safe[:_SLUG_MAX_LEN].rstrip("-")
    return safe or "sonilo"


def _ducking_base_name(
    voice_path: str | None, voice_url: str | None, task_id: str
) -> str:
    """Name a ducking result after its voice input: interview.mp4 ->
    interview-ducked(.mp4).

    The voice track is what the user recognizes — the music bed is
    interchangeable — so it is the half worth naming the output after. For
    a URL, the last path segment is used (query and fragment are dropped by
    urlparse). When neither input yields a usable stem, fall back to
    ducked-{task_id[:8]}, mirroring get_sfx_task's sfx-{task_id[:8]}.

    The extension is NOT decided here: it comes from the task envelope
    (.wav, or .mp4 when the voice input was a video) — see
    _normalize_task_envelope.
    """
    raw = ""
    if voice_path:
        raw = Path(os.path.expanduser(voice_path)).stem
    elif voice_url:
        raw = Path(urlparse(voice_url).path).stem
    if not raw.strip():
        return f"ducked-{task_id[:8]}"
    return f"{_slugify(raw)}-ducked"


_AUDIO_EXTS = frozenset({".wav", ".mp3", ".m4a", ".aac", ".ogg", ".flac"})
_VIDEO_EXTS = frozenset({".mp4", ".mov", ".avi", ".wmv", ".webm", ".mkv"})
# video-to-sfx (fal video-to-audio) accepts a narrower set than the locally
# ffprobe-readable formats above — mp4, mov, webm, m4v, and animated gif.
_SFX_VIDEO_EXTS = frozenset({".mp4", ".mov", ".webm", ".m4v", ".gif"})


def _resolve_input_file(
    file_path: str,
    base_path: str | None,
    allowed_exts: frozenset[str] | set[str],
    kind: str,
) -> Path:
    """Resolve a user-provided file path; verify it exists and matches an allowed extension.

    `kind` is used in error messages (e.g. "audio", "video").
    """
    expanded = os.path.expanduser(file_path)
    if not os.path.isabs(expanded):
        if not base_path:
            raise Exception(
                "File path must be absolute when SONILO_MCP_BASE_PATH is not set"
            )
        path = Path(os.path.expanduser(base_path)) / expanded
    else:
        path = Path(expanded)

    if not path.exists():
        raise Exception(f"File ({path}) does not exist")
    if not path.is_file():
        raise Exception(f"File ({path}) is not a file")
    if path.suffix.lower() not in allowed_exts:
        raise Exception(f"File ({path}) is not a recognized {kind} format")
    if not _is_within_base(path, base_path):
        raise Exception(
            f"File ({path}) is outside the allowed base directory "
            "(SONILO_MCP_BASE_PATH). Move it under that directory, or set "
            "SONILO_MCP_ALLOW_ANY_PATH=true to allow reading it."
        )
    return path


def _require_http_url(url: str, label: str) -> None:
    """Restrict a caller-supplied URL to http(s) before it reaches ffprobe or
    the backend — keeps file:// (local file disclosure), internal addresses
    (SSRF from this machine), and "-"-prefixed values (ffprobe flag injection)
    out.

    `label` names the offending argument (e.g. "video" -> "video_url must be
    an http:// or https:// URL"). The single copy of this guard: every tool
    that accepts a URL (video_to_music, video_to_sfx, audio_ducking) calls it.
    """
    if urlparse(url).scheme.lower() not in ("http", "https"):
        raise Exception(f"{label}_url must be an http:// or https:// URL")


def _read_capped(path: Path, max_mb: int, label: str) -> bytes:
    """Read a file for upload, enforcing the account's size cap on the bytes
    actually read.

    An earlier stat() is stale by the time we get here (an await on ffprobe
    sat in between, during which the file could have been replaced or grown —
    a still-writing export, a sync tool). Reading at most max_bytes + 1 both
    closes that race and bounds memory use.

    `label` names the input in the error message (e.g. "Video", "Voice",
    "Music"). The single copy of the read-time cap: every tool that uploads a
    local file calls it.
    """
    max_bytes = max_mb * 1024 * 1024
    with open(path, "rb") as fh:
        content = fh.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise Exception(f"{label} file is too large (> {max_mb} MB cap)")
    return content


# The backend rejects videos longer than this, so we pre-check locally to fail
# fast and skip a wasted upload. Keep in sync with the backend's limit.
_MAX_VIDEO_DURATION_SECONDS = 360  # 6 minutes — music endpoints
_SFX_MAX_VIDEO_DURATION_SECONDS = 180  # 3 minutes — /v1/video-to-sfx
_DUCKING_MAX_DURATION_SECONDS = 360  # 6 minutes — /v1/audio-ducking, per input


async def _check_media_duration(
    source: str, max_seconds: int = _MAX_VIDEO_DURATION_SECONDS
) -> None:
    """Best-effort local ffprobe pre-check of a media file's duration.

    Reads `format.duration`, which ffprobe reports for audio files as well
    as videos — so this serves the video endpoints and the audio-ducking
    endpoint alike.

    Raises if the duration is known to exceed `max_seconds`, so the caller
    fails fast instead of uploading media the backend will reject.
    `source` may be a local path or a URL (ffprobe handles both).

    The check is best-effort: if ffprobe is not installed, times out, or
    cannot parse the source, we stay silent and let the backend make the
    final call rather than block a legitimate request.
    """
    if shutil.which("ffprobe") is None:
        return
    try:
        proc = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v", "quiet",
            "-print_format", "json",
            "-show_format",
            source,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
    except (asyncio.TimeoutError, OSError, ValueError):
        # ValueError: create_subprocess_exec rejects a source containing an
        # embedded NUL byte (or similar odd chars) before spawning. This
        # helper is best-effort, so fail open and let the backend decide
        # rather than crash the whole request with a raw traceback.
        return  # fail open — let the backend decide
    if proc.returncode != 0:
        return
    try:
        duration = float(json.loads(stdout)["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return
    if duration > max_seconds:
        raise Exception(
            f"Media duration {duration:.1f}s exceeds the maximum of "
            f"{max_seconds}s"
        )


# ---------- HTTP plumbing ----------

def _extract_detail(body: str) -> str:
    """Pull the human-readable error text out of a backend error body.

    The real backend's public /v1/* contract (see factory.py's exception
    handlers) is `{"code": ..., "message": ...}` — including for 422
    validation errors, whose body also carries an `errors` array alongside
    `message`. `message` is preferred. `detail` is checked as a fallback:
    it's FastAPI's default shape for non-public paths, which the MCP client
    never calls today, but falling back to it is harmless and keeps this
    robust if that ever changes. Falls back to the raw body if neither key
    is present or the body isn't JSON.

    The value may be a non-string (a backend bug) — stringify it rather
    than let a `str.lower()` call elsewhere crash on it.
    """
    try:
        parsed = json.loads(body)
    except (json.JSONDecodeError, TypeError):
        return body
    if isinstance(parsed, dict):
        if "message" in parsed:
            return str(parsed["message"])
        if "detail" in parsed:
            return str(parsed["detail"])
    return body


def _describe_exc(e: BaseException) -> str:
    """Render an exception for a user-facing message.

    Several httpx transport errors (notably ReadError) stringify to '',
    which would leave a hole in the message (e.g. "failed: . Verify ...");
    fall back to the exception's class name in that case.
    """
    text = str(e).strip()
    return text or type(e).__name__


_BILLING_URL = "https://platform.sonilo.com/dashboard/billing"
_API_KEYS_URL = "https://platform.sonilo.com/dashboard/api-keys"


class SoniloHTTPError(Exception):
    """A backend HTTP error, carrying the status code.

    Subclasses Exception so every existing `pytest.raises(Exception, ...)` /
    generic `except Exception` still works unchanged. The status_code lets
    callers that wrap this error (e.g. _poll_task, get_sfx_task) distinguish
    a permanent 4xx (retrying can never help — bad id, revoked key, ...)
    from a transient 429/5xx (retrying may well succeed), so they only
    attach "try again" recovery advice when it's actually true.
    """

    def __init__(self, message: str, status_code: int):
        super().__init__(message)
        self.status_code = status_code


def _raise_http_error(status_code: int, body: str) -> None:
    """Map a backend HTTP error to a clear user-facing exception. Always raises."""
    detail = _extract_detail(body)
    if status_code == 401:
        raise SoniloHTTPError(
            f"Invalid SONILO_API_KEY — verify the key at {_API_KEYS_URL}",
            status_code,
        )
    if status_code == 402:
        if "minute" in detail.lower() or "credit" in detail.lower():
            raise SoniloHTTPError(f"{detail}. Top up at {_BILLING_URL}", status_code)
        raise SoniloHTTPError(detail, status_code)
    if status_code == 413:
        raise SoniloHTTPError(f"File too large: {detail}", status_code)
    if status_code == 422:
        raise SoniloHTTPError(detail, status_code)
    if status_code == 429:
        raise SoniloHTTPError(f"Rate limit exceeded: {detail}", status_code)
    if 400 <= status_code < 500:
        raise SoniloHTTPError(detail, status_code)
    if 500 <= status_code:
        raise SoniloHTTPError(
            f"Server error ({status_code}): {detail}. Please retry shortly.",
            status_code,
        )
    raise SoniloHTTPError(f"Unexpected status {status_code}: {detail}", status_code)


def _is_task_not_found(e: BaseException) -> bool:
    """Whether `e` means the task_id itself is gone/invalid — the only case
    where recovery advice should be omitted.

    Only a 404 means that: a bad/typo'd id, or an id for a task type
    /v1/tasks does not serve. It serves SFX, audio-ducking, and — since
    isolate_vocals — async video-to-music tasks; a purely streaming
    generation (text_to_music, or video_to_music without isolate_vocals) has
    no task at all, so its id 404s here. Every OTHER error (401, 402,
    413, 422, 429, 5xx, or a network-level failure with no status at all,
    e.g. httpx.RequestError wrapped as a bare Exception by _http_get_json)
    still refers to a task that EXISTS and was already CHARGED — the cause
    is something the caller can fix (renew the key, settle the bill, wait
    out the rate limit, retry a transient blip) and then recover the paid
    result, so those must always keep the task_id + get_sfx_task advice.
    """
    return getattr(e, "status_code", None) == 404


def _is_transient_error(e: BaseException) -> bool:
    """Whether `e` is a rate-limit/server/network failure worth an immediate
    retry, as opposed to an auth/billing failure the caller must fix first.

    This no longer decides WHETHER recovery advice is attached (see
    _is_task_not_found, which owns that) — every non-404 error keeps the
    task_id. This only picks the PHRASING: transient errors get "try again
    shortly" wording, while 401/402/413/422 get "resolve the issue above,
    then retry" wording, since blindly telling someone to retry a revoked
    key or a suspended account moments later is misleading.
    """
    status = getattr(e, "status_code", None)
    return status is None or status == 429 or status >= 500


# Short timeout for lightweight GETs (services, usage). Streaming
# generation calls use cfg["timeout"] (default 300s) instead.
_GET_TIMEOUT_SECONDS = 30


async def _http_get_json(path: str, params: dict | None = None) -> dict:
    """GET helper with one-shot retry on 5xx / network errors.

    Retries are safe for GET tools (idempotent). Do NOT use for generation
    endpoints (would risk double-charging on transient 5xx).
    """
    cfg = _get_config()
    if not cfg["api_key"]:
        raise Exception(f"SONILO_API_KEY not set — see {_API_KEYS_URL}")
    url = cfg["api_url"].rstrip("/") + path
    headers = {"Authorization": f"Bearer {cfg['api_key']}", **_CLIENT_HEADERS, **_host_headers()}

    for attempt in (1, 2):
        try:
            async with httpx.AsyncClient(timeout=_GET_TIMEOUT_SECONDS) as client:
                r = await client.get(url, headers=headers, params=params)
            if r.status_code >= 500 and attempt == 1:
                await asyncio.sleep(1)
                continue
            if r.status_code >= 400:
                _raise_http_error(r.status_code, r.text)
            return r.json()
        except httpx.RequestError as e:
            if attempt == 1:
                await asyncio.sleep(1)
                continue
            raise Exception(f"HTTP request failed: {_describe_exc(e)}") from e


# ---------- Streaming consumer ----------

async def _consume_ndjson_lines(
    lines: AsyncIterator[str],
) -> tuple[dict[int, bytearray], int, str | None]:
    """Consume an NDJSON event stream, accumulating audio bytes by stream_index.

    Returns:
        (streams_by_index, num_streams, title_or_none)
    Raises:
        Exception if an error event is seen, or if the stream ends without
        a `complete` event.

    Event types in the backend's NDJSON stream:
        - audio_chunk: append base64-decoded bytes to streams[stream_index]
        - title: capture optional title for filename
        - complete: terminal success
        - error: terminal failure → raise
        - others (stage_start, stage_complete, trace, final_inputs): ignore
    Malformed JSON lines are silently dropped (matches backend's fail-closed).
    """
    streams: dict[int, bytearray] = {}
    num_streams = 1
    title: str | None = None
    completed = False
    error_msg: str | None = None

    async for line in lines:
        if not line.strip():
            continue
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(evt, dict):
            continue
        t = evt.get("type")
        if t == "audio_chunk":
            try:
                idx = int(evt.get("stream_index", 0))
                num_streams = int(evt.get("num_streams", 1))
            except (TypeError, ValueError):
                continue
            if idx < 0:
                continue
            data = evt.get("data")
            if not isinstance(data, str):
                continue
            try:
                decoded = base64.b64decode(data, validate=True)
            except (binascii.Error, ValueError):
                continue
            streams.setdefault(idx, bytearray()).extend(decoded)
        elif t == "title":
            t_val = evt.get("title")
            if isinstance(t_val, str) and t_val.strip():
                title = t_val
        elif t == "complete":
            completed = True
        elif t == "error":
            error_msg = (
                evt.get("message") or evt.get("code") or "stream error"
            )
            break
        # All other event types are silently ignored.

    if not completed:
        raise Exception(error_msg or "Stream ended without `complete` event")
    return streams, num_streams, title


# ---------- Generation streaming ----------

async def _post_streaming_generation(
    path: str,
    output_path: Path,
    json_body: dict | None = None,
    data: dict | None = None,
    files: dict | None = None,
) -> list[TextContent]:
    """Open a streaming POST, consume the NDJSON stream, write each audio
    stream to disk under output_path.

    No retry — generation endpoints are non-idempotent and could double-charge.
    """
    cfg = _get_config()
    if not cfg["api_key"]:
        raise Exception(f"SONILO_API_KEY not set — see {_API_KEYS_URL}")
    url = cfg["api_url"].rstrip("/") + path
    headers = {"Authorization": f"Bearer {cfg['api_key']}", **_CLIENT_HEADERS, **_host_headers()}

    try:
        async with httpx.AsyncClient(timeout=cfg["timeout"]) as client:
            async with client.stream(
                "POST", url, headers=headers,
                json=json_body, data=data, files=files,
            ) as r:
                if r.status_code >= 400:
                    body = (await r.aread()).decode("utf-8", errors="replace")
                    _raise_http_error(r.status_code, body)
                streams, num_streams, title = await _consume_ndjson_lines(
                    r.aiter_lines()
                )
    except httpx.TimeoutException as e:
        raise Exception(
            f"Generation timed out after {cfg['timeout']}s. The backend may "
            "have completed and charged your account — check `get_usage` to "
            "reconcile."
        ) from e
    except httpx.RequestError as e:
        raise Exception(
            f"HTTP request failed: {_describe_exc(e)}. Verify SONILO_API_URL "
            f"({cfg['api_url']}) is reachable."
        ) from e

    safe_title = _slugify(title) if title else f"sonilo-{int(time.time())}"
    saved: list[TextContent] = []
    for idx, buf in sorted(streams.items()):
        suffix = f"-{idx}" if num_streams > 1 else ""
        out_file = output_path / f"{safe_title}{suffix}.m4a"
        out_file.write_bytes(bytes(buf))
        saved.append(TextContent(
            type="text",
            text=f"Success. File saved as: {out_file}",
        ))
    if not saved:
        raise Exception("Stream completed but no audio chunks were received")
    return saved


# ---------- SFX task pipeline ----------


async def _post_task_submit(
    path: str,
    data: dict | None = None,
    files: dict | None = None,
) -> str:
    """POST a task-based generation request; expect 202 with {"task_id": ...}.

    No retry — SFX endpoints charge on acceptance, same policy as
    _post_streaming_generation. Uses cfg["timeout"] (video uploads are slow).
    """
    cfg = _get_config()
    if not cfg["api_key"]:
        raise Exception(f"SONILO_API_KEY not set — see {_API_KEYS_URL}")
    url = cfg["api_url"].rstrip("/") + path
    headers = {"Authorization": f"Bearer {cfg['api_key']}", **_CLIENT_HEADERS, **_host_headers()}
    try:
        async with httpx.AsyncClient(timeout=cfg["timeout"]) as client:
            r = await client.post(url, headers=headers, data=data, files=files)
    except httpx.RequestError as e:
        # A connection reset/timeout here means the request never fully
        # reached the backend — the backend only creates and charges a task
        # once it has fully received and accepted the request, so this is
        # safe to retry (unlike a failure *after* a 202 response, which would
        # mean the task was already created).
        raise Exception(
            f"Upload failed before the request completed "
            f"({_describe_exc(e)}). No task was created and nothing was "
            "charged — retry. If this keeps happening with large videos, "
            "the connection is being reset during upload."
        ) from e
    if r.status_code >= 400:
        _raise_http_error(r.status_code, r.text)
    try:
        parsed_body = r.json()
    except json.JSONDecodeError:
        parsed_body = None
    task_id = parsed_body.get("task_id") if isinstance(parsed_body, dict) else None
    if not task_id:
        raise Exception(
            f"Backend accepted the request (status {r.status_code}) but "
            "returned no task_id"
        )
    task_id = str(task_id)
    # The user is charged as of this point. If the tool call is cancelled
    # before anything else records the id (e.g. asyncio.CancelledError
    # during polling/download, which propagates as a BaseException and
    # bypasses the recovery-wrapping Exception handlers), this stderr line
    # is the only surviving record — without it a cancelled call makes a
    # paid task genuinely unrecoverable (no list-tasks endpoint exists).
    print(
        f"[sonilo-mcp] task submitted: {task_id} (recover with get_sfx_task)",
        file=sys.stderr,
        flush=True,
    )
    return task_id


_POLL_INTERVAL_SECONDS = 5.0
# Test seam: monkeypatch api._poll_sleep to avoid real 5s waits in tests.
_poll_sleep = asyncio.sleep


def _end_sentence(value: object) -> str:
    """Stringify `value` and ensure it ends with sentence punctuation.

    Used when composing wrapped error messages (`f"{_end_sentence(e)} ..."`)
    so that appending more prose after an exception's message never produces
    a run-on sentence — e.g. a bare `OSError("No space left on device")`
    would otherwise read as "No space left on device Task id: ..." with no
    separator.
    """
    text = str(value).rstrip()
    if text and text[-1] not in ".!?":
        return text + "."
    return text


def _require_json_object(body: object, error_message: str) -> dict:
    """Validate that a 200 response body is a JSON object.

    A 200 with a body that isn't a dict (e.g. `null` or a bare list) is a
    backend contract violation, not a 4xx/5xx — _http_get_json returns it
    unchanged with no exception to catch. Left unguarded, callers either
    crash with a bare AttributeError from body.get(...), or — for tools
    that hand the body straight to the MCP host — silently succeed with
    empty output (FastMCP maps None -> [], and a list destructures into
    unlabeled text fragments), indistinguishable from a legitimate empty
    result.
    """
    if not isinstance(body, dict):
        raise Exception(error_message)
    return body


def _require_task_body(body: object, task_id: str) -> dict:
    """Validate that a /v1/tasks/{task_id} 200 response body is a JSON
    object, as both _poll_task and get_sfx_task assume when they call
    body.get(...) right after fetching it.
    """
    return _require_json_object(
        body,
        f"Unexpected response from the backend for task {task_id} "
        "(expected a JSON object). The task was already submitted and "
        f'charged — call get_sfx_task("{task_id}") to check for the '
        "result.",
    )


def _require_account_body(body: object, endpoint: str) -> dict:
    """Validate that a free, read-only account-endpoint 200 response body
    is a JSON object before it's handed straight to the MCP host.

    Unlike _require_task_body, these endpoints are free and read-only, so
    the message carries no charge/recovery language — just what went
    wrong and where.
    """
    return _require_json_object(
        body,
        f"Unexpected response from the backend for {endpoint} "
        "(expected a JSON object).",
    )


async def _poll_task(task_id: str, timeout_seconds: float) -> dict:
    """Poll GET /v1/tasks/{task_id} every _POLL_INTERVAL_SECONDS until the
    task is terminal (succeeded/failed) or timeout_seconds elapses.

    Returns the terminal task body. On timeout, raises with the task_id and
    a get_sfx_task recovery hint — the backend keeps running (and charging),
    so the result stays retrievable.
    """
    deadline = time.monotonic() + timeout_seconds
    while True:
        try:
            body = await _http_get_json(f"/v1/tasks/{task_id}")
        except Exception as e:
            if _is_task_not_found(e):
                # The task_id itself is gone/invalid (404) — no amount of
                # retrying will change that, so raise as-is with no advice.
                raise
            if _is_transient_error(e):
                raise Exception(
                    f"{_end_sentence(e)} The generation task {task_id} may "
                    "still be running on the backend — call "
                    f'get_sfx_task("{task_id}") later to retrieve the '
                    "result."
                ) from e
            # A non-404 4xx (401 key rotated, 402 billing suspended, ...) —
            # the task still exists and was already charged. The caller
            # must fix the underlying cause first, then recover the result.
            raise Exception(
                f"{_end_sentence(e)} Task {task_id} was already submitted "
                "and charged — resolve the issue above, then call "
                f'get_sfx_task("{task_id}") to retrieve the result.'
            ) from e
        body = _require_task_body(body, task_id)
        if body.get("status") in ("succeeded", "failed"):
            return body
        if time.monotonic() >= deadline:
            raise Exception(
                f"Timed out after {timeout_seconds:.0f}s waiting for task "
                f"{task_id}. The generation may still complete on the "
                f'backend — call get_sfx_task("{task_id}") later to '
                "retrieve the result."
            )
        await _poll_sleep(_POLL_INTERVAL_SECONDS)


# content_type -> extension for SFX audio artifacts. The backend sets
# content_type from the requested audio_format, so deriving the extension
# here matches the caller's requested format. Video artifacts are always mp4.
_AUDIO_CONTENT_TYPE_EXTS = {
    "audio/wav": ".wav",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/flac": ".flac",
}


def _ext_from_content_type(content_type: str | None) -> str:
    # The backend is expected to send a string, but a truthy non-string
    # value (a backend bug) must not crash this with an AttributeError from
    # `.lower()` — treat it as unknown and fall back to the default, same
    # as a missing/empty content_type.
    if not isinstance(content_type, str):
        return ".m4a"
    return _AUDIO_CONTENT_TYPE_EXTS.get(content_type.lower(), ".m4a")


# Ducking renders wav, and re-muxes into mp4 when the voice input was a
# video. Its envelope carries no content_type, so the type is synthesized
# here from output_type. Only the "audio" value is ever read back:
# _save_task_artifacts derives the audio extension from it via
# _ext_from_content_type, so "audio/wav" must stay a key of
# _AUDIO_CONTENT_TYPE_EXTS. The video branch hard-codes ".mp4" and never
# looks at content_type — the "video/mp4" entry is carried only so the
# normalized slot is shaped like an SFX one.
#
# This dict is also the authoritative set of output_type values: an
# output_type outside these keys is NOT a ducking envelope (see
# _normalize_task_envelope).
_DUCKING_CONTENT_TYPES = {"audio": "audio/wav", "video": "video/mp4"}


def _normalize_task_envelope(body: dict) -> tuple[dict, bool]:
    """Rewrite audio-ducking's flat success envelope into the SFX shape.

    Returns `(body, is_ducking)`, where `is_ducking` says whether a ducking
    envelope was actually recognized and normalized.

    SFX tasks return `{"audio": {...}, "video": {...}}`; audio-ducking
    returns `{"output_url": ..., "output_type": "audio" | "video"}` — one
    artifact, never both. Normalizing here means _save_task_artifacts and
    everything under it (extension, reuse check, atomic name reservation,
    download, recovery wording) stays single-path.

    A video output is the ONLY artifact of that task: the ducked audio is
    inside the re-muxed mp4, so no audio slot is produced. The `is_ducking`
    flag is what lets _save_task_artifacts allow that video-only case
    WITHOUT weakening the audio requirement for SFX bodies (where a missing
    audio slot means half a paid result went missing) — it is reported from
    here, the one place that knows which envelope it saw, rather than
    re-sniffed downstream.

    Returns `(body, False)` when there is no usable `output_url`, or when
    `output_type` is anything other than "audio" or "video" — every SFX
    body, and any malformed ducking body, which is then held to the SFX
    contract and falls through to _save_task_artifacts' missing-artifact
    error, task_id and all. Never mutates the caller's dict.

    output_type is matched STRICTLY (no defaulting to "audio"): a missing or
    unrecognized type alongside a valid output_url would otherwise be
    downloaded and written with a fabricated .wav extension and reported as a
    success — a paid result silently mislabeled. Refusing it here routes the
    body to the charged-and-recoverable error path instead, which hands the
    caller the task_id and the get_sfx_task hint; the artifact is still on
    the backend, so nothing is lost.
    """
    url = body.get("output_url")
    if not isinstance(url, str) or not url:
        return body, False
    slot = body.get("output_type")
    # isinstance first: a non-string (an unhashable list/dict from a backend
    # bug) would make the membership test raise instead of falling through.
    if not isinstance(slot, str) or slot not in _DUCKING_CONTENT_TYPES:
        return body, False
    normalized = dict(body)
    normalized[slot] = {"url": url, "content_type": _DUCKING_CONTENT_TYPES[slot]}
    return normalized, True


def _has_artifact(slot: object) -> bool:
    """Whether a task envelope's audio/video slot actually carries a URL."""
    return isinstance(slot, dict) and bool(slot.get("url"))


_ARTIFACT_DEST_MAX_ATTEMPTS = 10_000


def _artifact_dest(output_path: Path, base_name: str, ext: str) -> Path:
    """Reserve and return the first available path for base_name+ext,
    appending -1, -2, … on collision.

    Reservation is ATOMIC: each candidate is claimed with
    os.O_CREAT | os.O_EXCL, which fails if the file already exists, instead
    of a plain exists() probe. This closes a TOCTOU race: the caller's
    actual write happens later, after an `await` on the network GET — with
    a mere existence check, two concurrent callers (e.g. an MCP host
    retrying a slow call while the first is still in flight) can both pick
    the same free name and one clobbers the other's paid result. Reserving
    the name up front (via the exclusive create) means the second caller's
    O_EXCL fails and it moves on to the next suffix.

    The returned path exists (empty) by the time this returns; the caller
    is responsible for removing it on any failure to actually populate it
    (see _download_artifact).
    """
    for n in range(_ARTIFACT_DEST_MAX_ATTEMPTS):
        dest = output_path / (f"{base_name}{ext}" if n == 0 else f"{base_name}-{n}{ext}")
        try:
            fd = os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            continue
        os.close(fd)
        return dest
    raise Exception(
        f"Could not reserve a free filename for {base_name}{ext} in "
        f"{output_path} after {_ARTIFACT_DEST_MAX_ATTEMPTS} attempts"
    )


async def _download_artifact(url: str, dest: Path) -> None:
    """Stream-download a presigned result URL to dest.

    Sends NO Authorization / X-Sonilo-Client / custom User-Agent headers:
    presigned URLs carry their own auth, and the API key must never be sent
    to the storage domain.

    On failure, raises stating only the FACT of the failure — no recovery
    advice. This function doesn't know the caller's task_id and isn't the
    right layer to own the recovery instruction; _save_task_artifacts (which
    has the task_id) wraps these failures with the single, complete recovery
    instruction. Duplicating that instruction here would just make the
    caller's wrapped message repeat itself.

    `dest` is expected to already exist (empty) — _artifact_dest reserves it
    atomically before this is called, to close a TOCTOU race where two
    concurrent callers could otherwise pick the same free path. On ANY
    failure here (bad status, a network error, or the write loop dying
    mid-stream), the reserved file is removed: leaving it behind would both
    hand the user a corrupt/empty artifact and make a retry pick a new
    suffixed path from _artifact_dest, permanently orphaning the file.
    """
    cfg = _get_config()
    try:
        async with httpx.AsyncClient(timeout=cfg["timeout"]) as client:
            async with client.stream("GET", url) as r:
                if r.status_code >= 400:
                    raise Exception(
                        f"Artifact download failed (status {r.status_code})."
                    )
                with open(dest, "wb") as fh:
                    async for chunk in r.aiter_bytes():
                        fh.write(chunk)
    except httpx.RequestError as e:
        dest.unlink(missing_ok=True)
        raise Exception(f"Artifact download failed: {_describe_exc(e)}") from e
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


def _existing_canonical_dest(
    output_path: Path, base_name: str, ext: str, expected_size: int | None
) -> Path | None:
    """Return the UNSUFFIXED base_name+ext path if it's safe to reuse
    instead of re-downloading, else None.

    Used only by the reuse_existing path in _save_task_artifacts: it is the
    "did I already download exactly this task's result" check, deliberately
    distinct from _artifact_dest's collision-avoidance (which always finds a
    *new* free name and never revisits an existing one).

    A hard process kill (SIGKILL/OOM/host crash) mid-write can leave a
    non-empty, TRUNCATED file at the canonical path — _download_artifact's
    exception handlers never ran, so nothing cleaned it up. A bare
    exists()-and-non-empty check would silently hand that corrupt file back
    forever. When `expected_size` (the backend envelope's file_size) is
    known, this verifies it: reuse only on an EXACT size match; on a
    mismatch the file is corrupt, so it is removed here so the caller falls
    through to a fresh download at the SAME canonical path (not a `-1`
    suffix — the canonical file was garbage, not a distinct result).

    When `expected_size` is None (older/other backend responses without
    file_size), we cannot verify — trade-off: fall back to the previous
    non-empty check rather than unconditionally re-downloading, since
    always re-downloading would defeat the idempotency this function
    exists for.
    """
    candidate = output_path / f"{base_name}{ext}"
    if not candidate.exists():
        return None
    size = candidate.stat().st_size
    if size == 0:
        return None
    if expected_size is None:
        return candidate
    if size == expected_size:
        return candidate
    candidate.unlink(missing_ok=True)
    return None


def _raise_if_task_not_succeeded(body: dict, task_id: str) -> None:
    """Raise if a terminal task body's status isn't "succeeded".

    Shared by every save-artifacts layer — SFX/ducking's
    _save_task_artifacts and video-to-music's _save_music_task_artifacts —
    since failure/unexpected-status handling only reads `status`, `error`,
    and `refunded`, none of which depend on the envelope's audio/video shape.

    failed -> raise with the backend's error code/message and whether the
    charge was refunded. Anything else non-terminal (a caller bug — _poll_task
    only returns terminal bodies) -> raise generically. Does nothing when
    status is "succeeded".
    """
    task_status = body.get("status")
    if task_status == "failed":
        err = body.get("error")
        if isinstance(err, dict):
            code = err.get("code") or "GENERATION_FAILED"
            message = err.get("message") or "Generation failed"
        elif isinstance(err, str) and err:
            # Backend sent a truthy non-dict error (e.g. a bare string) —
            # the shape is untrustworthy, but the string is still useful as
            # a message. Do not let .get() on a non-dict crash here: this
            # task was charged AND failed, so the task_id/recovery call
            # below is the only way the user gets it back.
            code = "GENERATION_FAILED"
            message = err
        else:
            code = "GENERATION_FAILED"
            message = "Generation failed"
        if body.get("refunded") is True:
            refund_line = "The charge was reversed — you were not billed."
        else:
            refund_line = (
                "The charge has not been reversed — check get_usage to "
                "reconcile."
            )
        raise Exception(
            f"Generation failed ({code}): {message}. {refund_line} "
            f'Task id: {task_id}. Call get_sfx_task("{task_id}") to check '
            "for updates."
        )
    if task_status != "succeeded":
        raise Exception(
            f"Unexpected task status: {task_status}. Task id: {task_id}. "
            "This may be transient — check again with "
            f'get_sfx_task("{task_id}").'
        )


async def _save_task_artifacts(
    body: dict,
    output_path: Path,
    base_name: str,
    task_id: str,
    reuse_existing: bool = False,
) -> list[TextContent]:
    """Turn a terminal /v1/tasks/{id} body into saved local files.

    failed -> raise with the backend's error code/message and whether the
    charge was refunded. succeeded -> download whichever artifacts the task
    produced — audio and/or video — one TextContent per saved file. An audio
    artifact is required: an SFX task always has audio (video-to-sfx returns
    audio only). There are two exemptions: an audio-ducking task with a
    video voice input, whose only artifact is the re-muxed mp4 (see
    _normalize_task_envelope, which reports whether it saw that envelope);
    and a video-to-video task (video_to_video_music/_sfx), whose only
    artifact is the generated video with the new audio muxed in (see
    _is_video_to_video_envelope).

    task_id must be the caller's own known-good id (from _post_task_submit
    or the tool's own task_id argument), NOT derived from body — the
    backend's terminal body is not a trustworthy source for the recovery
    id, and a missing/incorrect id here would make a paid result
    unrecoverable.

    reuse_existing: when True (get_sfx_task's recovery path only — never
    text_to_sfx/video_to_sfx), an artifact whose canonical unsuffixed path
    already exists and is non-empty is treated as already downloaded rather
    than re-fetched into a new -1/-2 suffixed file. get_sfx_task is the
    documented recovery tool and its own messages actively invite repeat
    calls ("still processing, try again later"), so without this, every
    extra call after success would silently pile up duplicate downloads of
    the same paid result. text_to_sfx/video_to_sfx must NOT set this: two
    calls with the same prompt are two different generations and must
    always land in two distinct files (see _artifact_dest's atomic
    reservation), so they keep the default of False.

    This is NOT used for async (isolate_vocals) video-to-music tasks — their
    envelope shapes `audio` as a LIST (one entry per stream_index) plus
    optional single `vocals` and list `mux` slots, which _has_artifact/
    _normalize_task_envelope don't model. See _save_music_task_artifacts.
    """
    _raise_if_task_not_succeeded(body, task_id)

    body, is_ducking = _normalize_task_envelope(body)

    audio = body.get("audio")
    video = body.get("video")
    # An audio artifact is required, with exactly two exemptions: a ducking
    # task whose voice input was a video, and a video-to-video task — both
    # render a single artifact, the (re-)muxed mp4, and have no audio slot
    # at all. Each exemption is keyed off its own recognizer having
    # positively identified that envelope (_normalize_task_envelope's
    # is_ducking / _is_video_to_video_envelope) — NOT off "some artifact is
    # present". An SFX body is still held to the audio requirement even when
    # it carries a video: a video-without-audio SFX result means the audio
    # half of an already-charged generation went missing, and quietly
    # downloading just the mp4 would report success while losing it, with no
    # recovery hint. Raising here keeps the task_id and the get_sfx_task call
    # in the user's hands, which is the only way a paid result stays
    # recoverable from the error message alone.
    is_v2v = _is_video_to_video_envelope(body)
    if not _has_artifact(audio) and not ((is_ducking or is_v2v) and _has_artifact(video)):
        # Distinguish an unrecognized-output_type ducking result from a genuine
        # missing artifact. When there is a usable output_url but the
        # output_type is not one _normalize_task_envelope handles, get_sfx_task
        # would re-run the same normalization and produce this identical error
        # forever — so instead of the (useless) recovery hint, surface the
        # output_url and output_type so the user can fetch the paid result
        # manually. The genuine no-artifact case (no usable output_url) keeps
        # the original message unchanged.
        url = body.get("output_url")
        if isinstance(url, str) and url:
            slot = body.get("output_type")
            raise Exception(
                f"Task succeeded but its output_type {slot!r} is not "
                "recognized, so the result could not be saved automatically. "
                f"Download it directly from: {url}. Task id: {task_id}."
            )
        raise Exception(
            "Task succeeded but no audio artifact was returned. Task id: "
            f'{task_id} — call get_sfx_task("{task_id}") to check for the '
            "result."
        )

    saved: list[TextContent] = []
    # Stays None for a video-only ducking result, where no audio artifact
    # exists — the video block's failure message keys off this so it never
    # claims an audio file was saved when none was.
    audio_dest: Path | None = None
    if _has_artifact(audio):
        try:
            # Everything below — deriving the extension, checking for a
            # reusable existing file, computing the destination path, and the
            # actual download — must stay inside this wrap. The backend already
            # succeeded and charged for this task by the time we get here, so
            # ANY failure here must still carry the task_id and recovery hint.
            # _download_artifact (and any other failure here, e.g. an OSError
            # from _artifact_dest) states only the bare fact of the failure —
            # this is the one layer that owns the single, complete recovery
            # instruction, so it isn't repeated.
            audio_ext = _ext_from_content_type(audio.get("content_type"))
            existing = (
                _existing_canonical_dest(
                    output_path, base_name, audio_ext, audio.get("file_size")
                )
                if reuse_existing
                else None
            )
            if existing is None:
                dest = _artifact_dest(output_path, base_name, audio_ext)
                await _download_artifact(audio["url"], dest)
            else:
                dest = existing
        except Exception as e:
            raise Exception(
                f"{_end_sentence(e)} The result is still stored on the backend "
                f'— call get_sfx_task("{task_id}") to retry.'
            ) from e
        if existing is not None:
            saved.append(
                TextContent(
                    type="text", text=f"Already downloaded. File saved as: {dest}"
                )
            )
        else:
            saved.append(
                TextContent(type="text", text=f"Success. File saved as: {dest}")
            )
        audio_dest = dest

    if _has_artifact(video):
        try:
            existing_video = (
                _existing_canonical_dest(
                    output_path, base_name, ".mp4", video.get("file_size")
                )
                if reuse_existing
                else None
            )
            if existing_video is None:
                dest = _artifact_dest(output_path, base_name, ".mp4")
                await _download_artifact(video["url"], dest)
            else:
                dest = existing_video
        except Exception as e:
            if audio_dest is not None:
                raise Exception(
                    f"{_end_sentence(e)} The audio file was already saved as: "
                    f"{audio_dest}. The video is still stored on the backend — "
                    f'call get_sfx_task("{task_id}") to retry the download.'
                ) from e
            raise Exception(
                f"{_end_sentence(e)} The result is still stored on the backend "
                f'— call get_sfx_task("{task_id}") to retry.'
            ) from e
        if existing_video is not None:
            saved.append(
                TextContent(
                    type="text", text=f"Already downloaded. File saved as: {dest}"
                )
            )
            return saved
        saved.append(
            TextContent(type="text", text=f"Success. File saved as: {dest}")
        )
    return saved


def _is_video_to_video_envelope(body: dict) -> bool:
    """Whether a terminal /v1/tasks/{id} body is a video-to-video result:
    a single `video` object and no `audio`. Prefers the backend `type`
    field, falling back to shape-sniffing only for bodies that omit `type`
    entirely.

    An explicit, different `type` (e.g. "video_to_sfx", "audio_ducking") is
    always authoritative and short-circuits straight to False: the backend's
    /v1/tasks/{id} always sets `type` (see routers/v1/tasks.py), so shape-
    sniffing over an explicitly-typed body would misclassify a genuine
    video-only-without-audio SFX failure (audio half of a paid result went
    missing — see _save_task_artifacts) as a healthy video-to-video result.
    Ducking's normalized envelope is also excluded this way, though it is
    additionally distinguishable by shape: it keeps its `output_url` key
    even after _normalize_task_envelope adds the `video` slot, while a real
    video-to-video body never has one.
    """
    t = body.get("type")
    if isinstance(t, str) and t:
        return t in ("video_to_video_music", "video_to_video_sfx")
    return bool(body.get("video")) and body.get("audio") is None and "output_url" not in body


def _is_music_task_envelope(body: dict) -> bool:
    """Whether a terminal /v1/tasks/{id} body is an async video-to-music
    envelope (list-shaped `audio`, optional single `vocals` and list `mux`)
    rather than the SFX/ducking single-dict-`audio` shape.

    get_sfx_task's recovery path checks this to route between
    _save_task_artifacts (SFX/ducking) and _save_music_task_artifacts
    (async video-to-music). Prefers the backend's own `type` field —
    ducking bodies already carry `type: "audio_ducking"`, so video-to-music
    carrying `type: "video_to_music"` follows the same convention — but
    falls back to shape-sniffing (`audio` as a list, or a `vocals`/`mux`
    key present) for a body that omits `type`, so recovery keeps working
    even then. A failed task's body normally has none of these markers;
    that's harmless here, since failure/refund handling
    (_raise_if_task_not_succeeded) is identical on both routes.
    """
    if body.get("type") == "video_to_music":
        return True
    if isinstance(body.get("audio"), list):
        return True
    return "vocals" in body or "mux" in body


# ---------- video-to-music (isolate_vocals) task pipeline ----------
#
# Async video-to-music (mode=async, isolate_vocals=true) shares
# _post_task_submit/_poll_task with SFX/ducking, but its succeeded envelope
# is a different shape: `audio` is ALWAYS a list (one entry per
# stream_index, never a single dict), and — only with isolate_vocals — a
# single `vocals` object and a `mux` list are also present. That shape
# breaks _has_artifact/_normalize_task_envelope's single-dict assumptions
# (a list `audio` is not a dict, so _has_artifact(audio) is always False),
# so the save layer below is new rather than forcing _save_task_artifacts to
# handle a second envelope shape.


async def _save_music_task_artifacts(
    body: dict,
    output_path: Path,
    base_name: str,
    task_id: str,
) -> list[TextContent]:
    """Turn a terminal /v1/tasks/{id} body from an async (isolate_vocals)
    video-to-music task into saved local files.

    failed/unexpected status -> see _raise_if_task_not_succeeded (shared
    with _save_task_artifacts; this part is envelope-agnostic).

    succeeded -> download every entry in the `audio` list, then the single
    `vocals` object if present, then every entry in the `mux` list if
    present, then every entry in the `ducked` list if present — one
    TextContent per saved file, each labeled so the caller can tell audio/
    vocals/mux/ducked apart. The mux files (vocals+music already mixed) are
    called out as the ready-to-use combined result; `ducked` files are the
    generated music lowered under the source voice (free, best-effort —
    present only when the backend's `ducking` option ran).

    Naming follows the existing streaming convention: `{base}.m4a` for a
    single audio stream, `{base}-{idx}.m4a` when there is more than one.
    `vocals` is saved as `{base}-vocals.{ext}`; `mux` and `ducked` follow
    the same single/multi-stream pattern as audio: `{base}-mux.{ext}` or
    `{base}-mux-{idx}.{ext}`, and likewise `{base}-ducked.{ext}` /
    `{base}-ducked-{idx}.{ext}`.

    task_id must be the caller's own known-good id (from _post_task_submit),
    same rule as _save_task_artifacts — the terminal body is not a
    trustworthy source for the recovery id.

    Unlike _save_task_artifacts, this has no reuse_existing mode: video-to-
    music (isolate_vocals or not) has no dedicated recovery tool (scope is
    the video_to_music tool only), so there is no repeat-call path to
    dedupe downloads for.
    """
    _raise_if_task_not_succeeded(body, task_id)

    audio = body.get("audio")
    valid_audio = (
        [a for a in audio if _has_artifact(a)] if isinstance(audio, list) else []
    )
    if not valid_audio:
        raise Exception(
            "Task succeeded but no audio artifact was returned. Task id: "
            f"{task_id}."
        )

    saved: list[TextContent] = []
    saved_paths: list[Path] = []

    async def _save_one(entry: dict, dest_base: str, label: str) -> None:
        # Everything here can fail after the task already succeeded and was
        # charged, so any failure must keep the task_id (and, if some files
        # already made it to disk, list them) rather than silently drop
        # them — mirrors _save_task_artifacts' per-artifact wrapping.
        try:
            ext = _ext_from_content_type(entry.get("content_type"))
            dest = _artifact_dest(output_path, dest_base, ext)
            await _download_artifact(entry["url"], dest)
        except Exception as e:
            already = (
                " Already saved: " + ", ".join(str(p) for p in saved_paths) + "."
                if saved_paths
                else ""
            )
            raise Exception(
                f"{_end_sentence(e)} The rest of this result is still "
                f"stored on the backend — task id: {task_id}.{already}"
            ) from e
        saved_paths.append(dest)
        saved.append(TextContent(
            type="text", text=f"Success ({label}). File saved as: {dest}",
        ))

    multi_audio = len(valid_audio) > 1
    for entry in sorted(valid_audio, key=lambda a: a.get("stream_index") or 0):
        idx = entry.get("stream_index") or 0
        suffix = f"-{idx}" if multi_audio else ""
        await _save_one(entry, f"{base_name}{suffix}", "music audio")

    vocals = body.get("vocals")
    if _has_artifact(vocals):
        await _save_one(vocals, f"{base_name}-vocals", "preserved speech")

    mux = body.get("mux")
    valid_mux = [m for m in mux if _has_artifact(m)] if isinstance(mux, list) else []
    multi_mux = len(valid_mux) > 1
    for entry in sorted(valid_mux, key=lambda m: m.get("stream_index") or 0):
        idx = entry.get("stream_index") or 0
        suffix = f"-{idx}" if multi_mux else ""
        await _save_one(
            entry,
            f"{base_name}-mux{suffix}",
            "mux — speech + music mixed, ready to use",
        )

    ducked = body.get("ducked")
    valid_ducked = (
        [d for d in ducked if _has_artifact(d)] if isinstance(ducked, list) else []
    )
    multi_ducked = len(valid_ducked) > 1
    for entry in sorted(valid_ducked, key=lambda d: d.get("stream_index") or 0):
        idx = entry.get("stream_index") or 0
        suffix = f"-{idx}" if multi_ducked else ""
        await _save_one(
            entry,
            f"{base_name}-ducked{suffix}",
            "ducked — music lowered under the source voice",
        )

    return saved


# ---------- Tools: generation ----------

@mcp.tool(
    description=(
        "Generate music from a text prompt and save the resulting audio "
        "file(s) to a local directory. Generated tracks are fully licensed "
        "(music licensed via Shutterstock) and cleared for commercial use "
        "on social, brand content, and advertising.\n\n"
        "⚠️ COST WARNING: This tool makes an API call to Sonilo which may "
        "incur charges. Only use when explicitly requested by the user.\n\n"
        "Args:\n"
        "    prompt (str): Description of the music to generate "
        "(1–1000 chars).\n"
        "    duration (int): Length in seconds (1–360).\n"
        "    output_format (str, optional): 'm4a' (default) or 'wav'. "
        "'wav' requires the backend's async generation mode (submit + "
        "poll) instead of streaming — selected automatically, no "
        "user-facing mode param needed.\n"
        "    output_directory (str, optional): Absolute path, or relative "
        "to SONILO_MCP_BASE_PATH. Defaults to SONILO_MCP_BASE_PATH "
        "(~/Desktop unless overridden).\n\n"
        "Returns:\n"
        "    One TextContent per generated audio stream, each containing "
        "the absolute path of the saved audio file (.m4a by default, "
        ".wav when output_format='wav')."
    )
)
async def text_to_music(
    prompt: str,
    duration: int,
    output_format: str | None = None,
    output_directory: str | None = None,
) -> list[TextContent]:
    out_path = _make_output_path(output_directory)
    # The backend's text-to-music endpoint expects form fields, not a JSON
    # body (same as video-to-music). Sending JSON yields a 422
    # "Field required" for prompt/duration.
    data: dict = {"prompt": prompt, "duration": duration}
    if output_format == "wav":
        # 'wav' requires mode=async on the backend (else a 400) — always
        # send both together, no user-facing mode param (mirrors
        # video_to_music's preserve_speech/ducking handling).
        data["mode"] = "async"
        data["output_format"] = output_format
        task_id = await _post_task_submit("/v1/text-to-music", data=data)
        body = await _poll_task(task_id, _get_config()["timeout"])
        base = _slugify(prompt) if prompt else f"music-{task_id[:8]}"
        return await _save_music_task_artifacts(body, out_path, base, task_id)
    if output_format:
        data["output_format"] = output_format
    return await _post_streaming_generation(
        "/v1/text-to-music",
        out_path,
        data=data,
    )


_services_cache: dict | None = None
_services_cache_expiry: float = 0.0
_SERVICES_CACHE_TTL = 300  # seconds


def _reset_services_cache() -> None:
    """Test helper: force the next _get_max_upload_size_mb() call to refetch."""
    global _services_cache, _services_cache_expiry
    _services_cache = None
    _services_cache_expiry = 0.0


async def _get_max_upload_size_mb() -> int:
    """Cached lookup of /v1/account/services.max_upload_size_mb.

    Returns 300 if the call fails (matches the backend's default cap).
    """
    global _services_cache, _services_cache_expiry
    now = time.time()
    if _services_cache is None or now > _services_cache_expiry:
        try:
            _services_cache = await _http_get_json("/v1/account/services")
            _services_cache_expiry = now + _SERVICES_CACHE_TTL
        except Exception:
            return 300
    try:
        return int(_services_cache.get("max_upload_size_mb") or 300)
    except (TypeError, ValueError, AttributeError):
        return 300


@mcp.tool(
    description=(
        "Generate an original score for a video: Sonilo analyzes the video's "
        "pacing, motion, and emotion, aligns transitions and beat drops to "
        "its cut points, and matches the video's duration exactly. Provide "
        "either a local video file path or a publicly accessible video URL. "
        "Generated tracks are fully licensed (music licensed via "
        "Shutterstock) and cleared for commercial use on social, brand "
        "content, and advertising.\n\n"
        "⚠️ COST WARNING: This tool makes an API call to Sonilo which may "
        "incur charges. Only use when explicitly requested by the user.\n\n"
        "Args:\n"
        "    video_path (str, optional): Absolute local path, or relative "
        "to SONILO_MCP_BASE_PATH. Supports .mp4/.mov/.avi/.wmv/.webm/.mkv. "
        "Subject to the account's max upload size (typically 300 MB). "
        "Maximum video duration is 360 seconds (6 minutes).\n"
        "    video_url (str, optional): HTTPS URL to a video file.\n"
        "    prompt (str, optional): Style hint for the generated music.\n"
        "    preserve_speech (bool, optional): Keep the source speech from "
        "the video audible in the result. When set, in addition to the "
        "generated music you also get a 'vocals' speech stem and a "
        "ready-to-use 'mux' (speech+music, already mixed). Defaults to "
        "False.\n"
        "    output_format (str, optional): 'm4a' (default) or 'wav'.\n"
        "    ducking (bool, optional): Duck the generated music under the "
        "source voice at finalize time. Default-ON server-side: leave "
        "unset to keep it on, pass False to opt out. Free, best-effort.\n"
        "    Any of preserve_speech/output_format='wav'/ducking makes this "
        "tool internally use the backend's async generation mode (submit + "
        "poll) instead of streaming — the call takes longer but the tool "
        "still waits for completion. Subject to the same 360-second video "
        "duration cap as the plain case.\n"
        "    output_directory (str, optional): Where to save the resulting "
        "audio file(s). Defaults to SONILO_MCP_BASE_PATH.\n\n"
        "Exactly one of video_path and video_url must be provided.\n\n"
        "Returns:\n"
        "    Plain case (no async-triggering param set): one TextContent "
        "per generated audio stream, unchanged from before.\n"
        "    preserve_speech=True: one TextContent per generated audio "
        "stream, plus one for the speech ('vocals') file, plus one per "
        "mux stream (speech+music mixed — this is the ready-to-use "
        "combined result).\n"
        "    ducking (default-on in async mode): also one TextContent per "
        "ducked stream (music lowered under the source voice), when the "
        "backend rendered one.\n"
        "    Each TextContent's label says which kind of file it is."
    )
)
async def video_to_music(
    video_path: str | None = None,
    video_url: str | None = None,
    prompt: str | None = None,
    preserve_speech: bool = False,
    output_format: str | None = None,
    ducking: bool | None = None,
    output_directory: str | None = None,
) -> list[TextContent]:
    if (video_path and video_url) or (not video_path and not video_url):
        raise Exception(
            "Provide either video_path or video_url (exactly one, not both)"
        )

    if video_url:
        # Restrict to http(s) before the URL ever reaches the local ffprobe
        # pre-check or the backend (see _require_http_url).
        _require_http_url(video_url, "video")

    out_path = _make_output_path(output_directory)
    cfg = _get_config()

    # preserve_speech/ducking/output_format="wav" all require mode=async on
    # the backend (else a 400) — always send it together, no user-facing
    # mode param.
    use_async = (
        preserve_speech
        or output_format == "wav"
        or ducking is not None
    )

    if video_path:
        resolved = _resolve_input_file(
            video_path, cfg["base_path"], _VIDEO_EXTS, "video"
        )
        max_mb = await _get_max_upload_size_mb()
        size_mb = resolved.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            # Cheap fail-fast: avoids a wasted ffprobe run for an
            # obviously-oversized file. NOT the authoritative check — see
            # _read_capped, which enforces the cap on the bytes actually read.
            raise Exception(
                f"Video file is too large ({size_mb:.1f} MB > {max_mb} MB cap)"
            )
        await _check_media_duration(str(resolved))
        data: dict = {"prompt": prompt} if prompt else {}
        if preserve_speech:
            data["preserve_speech"] = "true"
        if output_format:
            data["output_format"] = output_format
        if ducking is not None:
            data["ducking"] = "true" if ducking else "false"
        if use_async:
            data["mode"] = "async"
        mime, _ = mimetypes.guess_type(resolved.name)
        content = _read_capped(resolved, max_mb, "Video")
        files = {"video": (resolved.name, content, mime or "application/octet-stream")}
        if use_async:
            task_id = await _post_task_submit(
                "/v1/video-to-music", data=data, files=files
            )
            body = await _poll_task(task_id, cfg["timeout"])
            base = _slugify(prompt) if prompt else f"music-{task_id[:8]}"
            return await _save_music_task_artifacts(body, out_path, base, task_id)
        return await _post_streaming_generation(
            "/v1/video-to-music",
            out_path,
            data=data or None,
            files=files,
        )

    # video_url path — backend expects multipart form, not JSON
    await _check_media_duration(video_url)
    form: dict = {"video_url": video_url}
    if prompt:
        form["prompt"] = prompt
    if preserve_speech:
        form["preserve_speech"] = "true"
    if output_format:
        form["output_format"] = output_format
    if ducking is not None:
        form["ducking"] = "true" if ducking else "false"
    if use_async:
        form["mode"] = "async"
        task_id = await _post_task_submit("/v1/video-to-music", data=form)
        body = await _poll_task(task_id, cfg["timeout"])
        base = _slugify(prompt) if prompt else f"music-{task_id[:8]}"
        return await _save_music_task_artifacts(body, out_path, base, task_id)
    # Use `data` for form fields without files; httpx will use
    # application/x-www-form-urlencoded. The backend `video_to_music`
    # endpoint accepts both multipart and urlencoded for the URL mode.
    return await _post_streaming_generation(
        "/v1/video-to-music",
        out_path,
        data=form,
    )


# ---------- Tools: sound effects ----------

@mcp.tool(
    description=(
        "Generate a sound effect from a text prompt and save the audio file "
        "to a local directory. Generation is asynchronous on the backend; "
        "this tool waits for completion (typically well under the timeout) "
        "and returns the saved file path.\n\n"
        "⚠️ COST WARNING: This tool makes an API call to Sonilo which may "
        "incur charges. Only use when explicitly requested by the user.\n\n"
        "Args:\n"
        "    prompt (str): Description of the sound effect "
        "(1–2000 chars).\n"
        "    duration (int): Length in seconds (1–180).\n"
        "    audio_format (str, optional): One of wav, mp3, aac, flac. "
        "Defaults to aac (.m4a file).\n"
        "    output_directory (str, optional): Absolute path, or relative "
        "to SONILO_MCP_BASE_PATH. Defaults to SONILO_MCP_BASE_PATH "
        "(~/Desktop unless overridden).\n\n"
        "Returns:\n"
        "    TextContent with the absolute path of the saved audio file. "
        "If the call times out, the error message includes the task_id — "
        "recover the result later with get_sfx_task."
    )
)
async def text_to_sfx(
    prompt: str,
    duration: int,
    audio_format: str | None = None,
    output_directory: str | None = None,
) -> list[TextContent]:
    out_path = _make_output_path(output_directory)
    data: dict = {"prompt": prompt, "duration": duration}
    if audio_format:
        data["audio_format"] = audio_format
    task_id = await _post_task_submit("/v1/text-to-sfx", data=data)
    body = await _poll_task(task_id, _get_config()["timeout"])
    return await _save_task_artifacts(body, out_path, _slugify(prompt), task_id)


@mcp.tool(
    description=(
        "Generate sound effects for a video: Sonilo analyzes the video and "
        "creates matching SFX. Returns the generated sound-effects audio "
        "file. Provide either a "
        "local video file path or a publicly accessible video URL. "
        "Generation is asynchronous on the backend; this tool waits for "
        "completion and returns the saved file paths.\n\n"
        "⚠️ COST WARNING: This tool makes an API call to Sonilo which may "
        "incur charges. Only use when explicitly requested by the user.\n\n"
        "Args:\n"
        "    video_path (str, optional): Absolute local path, or relative "
        "to SONILO_MCP_BASE_PATH. Supports .mp4/.mov/.webm/.m4v/.gif "
        "(gif must be animated). Subject to the account's max upload size "
        "(typically 300 MB). Maximum video duration is 180 seconds "
        "(3 minutes).\n"
        "    video_url (str, optional): HTTPS URL to a video file.\n"
        "    prompt (str, optional): Overall description of the desired "
        "sound effects (max 2000 chars).\n"
        "    segments (list, optional): Per-segment SFX descriptions, each "
        '{"start": float, "end": float, "prompt": str}. Backend rules: '
        "first start must be 0; segments must be contiguous (each end == "
        "next start); every end > start; every prompt non-empty (max 200 "
        "chars); last end must not exceed the video duration; max 30 "
        "segments. Invalid segments are rejected before any charge.\n"
        "    audio_format (str, optional): One of wav, mp3, aac, flac. "
        "Defaults to aac (.m4a file).\n"
        "    output_directory (str, optional): Where to save the resulting "
        "files. Defaults to SONILO_MCP_BASE_PATH.\n\n"
        "Exactly one of video_path and video_url must be provided.\n\n"
        "Returns:\n"
        "    One TextContent: the saved audio file path. If the call times "
        "out, the error message "
        "includes the task_id — recover with get_sfx_task."
    )
)
async def video_to_sfx(
    video_path: str | None = None,
    video_url: str | None = None,
    prompt: str | None = None,
    segments: list[dict] | None = None,
    audio_format: str | None = None,
    output_directory: str | None = None,
) -> list[TextContent]:
    if (video_path and video_url) or (not video_path and not video_url):
        raise Exception(
            "Provide either video_path or video_url (exactly one, not both)"
        )

    if video_url:
        # Same scheme guard as video_to_music: keeps file:// and flag-like
        # values away from ffprobe and the backend.
        _require_http_url(video_url, "video")

    out_path = _make_output_path(output_directory)
    cfg = _get_config()

    form: dict = {}
    if prompt and prompt.strip():
        form["prompt"] = prompt
    if segments is not None:
        # Pass-through: the backend validates segments strictly and rejects
        # bad input with a 400 before charging.
        form["segments"] = json.dumps(segments)
    if audio_format:
        form["audio_format"] = audio_format

    if video_path:
        resolved = _resolve_input_file(
            video_path, cfg["base_path"], _SFX_VIDEO_EXTS, "video"
        )
        max_mb = await _get_max_upload_size_mb()
        size_mb = resolved.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            # Cheap fail-fast: avoids a wasted ffprobe run for an
            # obviously-oversized file. NOT the authoritative check — see
            # _read_capped, which enforces the cap on the bytes actually read.
            raise Exception(
                f"Video file is too large ({size_mb:.1f} MB > {max_mb} MB cap)"
            )
        await _check_media_duration(
            str(resolved), max_seconds=_SFX_MAX_VIDEO_DURATION_SECONDS
        )
        mime, _ = mimetypes.guess_type(resolved.name)
        content = _read_capped(resolved, max_mb, "Video")
        files = {
            "video": (
                resolved.name, content, mime or "application/octet-stream"
            )
        }
        task_id = await _post_task_submit(
            "/v1/video-to-sfx", data=form or None, files=files
        )
    else:
        await _check_media_duration(
            video_url, max_seconds=_SFX_MAX_VIDEO_DURATION_SECONDS
        )
        form["video_url"] = video_url
        task_id = await _post_task_submit("/v1/video-to-sfx", data=form)

    body = await _poll_task(task_id, cfg["timeout"])
    base = _slugify(prompt) if prompt and prompt.strip() else f"sfx-{task_id[:8]}"
    return await _save_task_artifacts(body, out_path, base, task_id)


# ---------- Tools: video-to-video ----------

@mcp.tool(
    description=(
        "Generate an original score for a video and return a NEW VIDEO with "
        "the music muxed in (not just an audio file). Provide either a local "
        "video path or a public video URL. Generation is asynchronous; this "
        "tool waits for completion and returns the saved video path. Tracks "
        "are fully licensed (via Shutterstock) and cleared for commercial "
        "use.\n\n"
        "⚠️ COST WARNING: This tool makes an API call to Sonilo which may "
        "incur charges. Only use when explicitly requested by the user.\n\n"
        "Args:\n"
        "    video_path (str, optional): Absolute local path, or relative to "
        "SONILO_MCP_BASE_PATH. Subject to the account upload cap. Maximum "
        "video duration is 360 seconds (6 minutes).\n"
        "    video_url (str, optional): HTTPS URL to a video file.\n"
        "    prompt (str, optional): Style hint for the generated music.\n"
        "    preserve_speech (bool, optional): Keep the source speech/vocals "
        "in the output. Defaults to False.\n"
        "    output_directory (str, optional): Where to save the result. "
        "Defaults to SONILO_MCP_BASE_PATH.\n\n"
        "Exactly one of video_path and video_url must be provided.\n\n"
        "Returns:\n"
        "    TextContent with the saved .mp4 path. On timeout the error "
        "message includes the task_id — recover with get_sfx_task."
    )
)
async def video_to_video_music(
    video_path: str | None = None,
    video_url: str | None = None,
    prompt: str | None = None,
    preserve_speech: bool = False,
    output_directory: str | None = None,
) -> list[TextContent]:
    if (video_path and video_url) or (not video_path and not video_url):
        raise Exception(
            "Provide either video_path or video_url (exactly one, not both)"
        )

    if video_url:
        # Same scheme guard as video_to_music/video_to_sfx: keeps file://
        # and flag-like values away from ffprobe and the backend.
        _require_http_url(video_url, "video")

    out_path = _make_output_path(output_directory)
    cfg = _get_config()

    form: dict = {}
    if prompt:
        form["prompt"] = prompt
    if preserve_speech:
        form["preserve_speech"] = "true"

    if video_path:
        resolved = _resolve_input_file(
            video_path, cfg["base_path"], _VIDEO_EXTS, "video"
        )
        max_mb = await _get_max_upload_size_mb()
        size_mb = resolved.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            # Cheap fail-fast: avoids a wasted ffprobe run for an
            # obviously-oversized file. NOT the authoritative check — see
            # _read_capped, which enforces the cap on the bytes actually read.
            raise Exception(
                f"Video file is too large ({size_mb:.1f} MB > {max_mb} MB cap)"
            )
        await _check_media_duration(str(resolved))
        mime, _ = mimetypes.guess_type(resolved.name)
        content = _read_capped(resolved, max_mb, "Video")
        files = {
            "video": (
                resolved.name, content, mime or "application/octet-stream"
            )
        }
        task_id = await _post_task_submit(
            "/v1/video-to-video-music", data=form or None, files=files
        )
    else:
        await _check_media_duration(video_url)
        form["video_url"] = video_url
        task_id = await _post_task_submit("/v1/video-to-video-music", data=form)

    body = await _poll_task(task_id, cfg["timeout"])
    base = _slugify(prompt) if prompt else f"v2v-music-{task_id[:8]}"
    return await _save_task_artifacts(body, out_path, base, task_id)


@mcp.tool(
    description=(
        "Generate sound effects for a video and return a NEW VIDEO with the "
        "SFX muxed in (not just an audio file). Provide either a local "
        "video path or a public video URL. Generation is asynchronous; this "
        "tool waits for completion and returns the saved video path.\n\n"
        "⚠️ COST WARNING: This tool makes an API call to Sonilo which may "
        "incur charges. Only use when explicitly requested by the user.\n\n"
        "Args:\n"
        "    video_path (str, optional): Absolute local path, or relative "
        "to SONILO_MCP_BASE_PATH. Supports .mp4/.mov/.webm/.m4v/.gif (gif "
        "must be animated). Subject to the account's max upload size "
        "(typically 300 MB). Maximum video duration is 180 seconds "
        "(3 minutes).\n"
        "    video_url (str, optional): HTTPS URL to a video file.\n"
        "    prompt (str, optional): Overall description of the desired "
        "sound effects (max 2000 chars).\n"
        "    segments (list, optional): Per-segment SFX descriptions, each "
        '{"start": float, "end": float, "prompt": str}. Backend rules: '
        "first start must be 0; segments must be contiguous (each end == "
        "next start); every end > start; every prompt non-empty (max 200 "
        "chars); last end must not exceed the video duration; max 30 "
        "segments. Invalid segments are rejected before any charge.\n"
        "    output_directory (str, optional): Where to save the result. "
        "Defaults to SONILO_MCP_BASE_PATH.\n\n"
        "Exactly one of video_path and video_url must be provided.\n\n"
        "Returns:\n"
        "    TextContent with the saved .mp4 path. On timeout the error "
        "message includes the task_id — recover with get_sfx_task."
    )
)
async def video_to_video_sfx(
    video_path: str | None = None,
    video_url: str | None = None,
    prompt: str | None = None,
    segments: list[dict] | None = None,
    output_directory: str | None = None,
) -> list[TextContent]:
    if (video_path and video_url) or (not video_path and not video_url):
        raise Exception(
            "Provide either video_path or video_url (exactly one, not both)"
        )

    if video_url:
        # Same scheme guard as video_to_video_music/video_to_sfx: keeps
        # file:// and flag-like values away from ffprobe and the backend.
        _require_http_url(video_url, "video")

    out_path = _make_output_path(output_directory)
    cfg = _get_config()

    form: dict = {}
    if prompt and prompt.strip():
        form["prompt"] = prompt
    if segments is not None:
        # Pass-through: the backend validates segments strictly and rejects
        # bad input with a 400 before charging.
        form["segments"] = json.dumps(segments)

    if video_path:
        resolved = _resolve_input_file(
            video_path, cfg["base_path"], _SFX_VIDEO_EXTS, "video"
        )
        max_mb = await _get_max_upload_size_mb()
        size_mb = resolved.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            # Cheap fail-fast: avoids a wasted ffprobe run for an
            # obviously-oversized file. NOT the authoritative check — see
            # _read_capped, which enforces the cap on the bytes actually read.
            raise Exception(
                f"Video file is too large ({size_mb:.1f} MB > {max_mb} MB cap)"
            )
        await _check_media_duration(
            str(resolved), max_seconds=_SFX_MAX_VIDEO_DURATION_SECONDS
        )
        mime, _ = mimetypes.guess_type(resolved.name)
        content = _read_capped(resolved, max_mb, "Video")
        files = {
            "video": (
                resolved.name, content, mime or "application/octet-stream"
            )
        }
        task_id = await _post_task_submit(
            "/v1/video-to-video-sfx", data=form or None, files=files
        )
    else:
        await _check_media_duration(
            video_url, max_seconds=_SFX_MAX_VIDEO_DURATION_SECONDS
        )
        form["video_url"] = video_url
        task_id = await _post_task_submit("/v1/video-to-video-sfx", data=form)

    body = await _poll_task(task_id, cfg["timeout"])
    base = _slugify(prompt) if prompt and prompt.strip() else f"v2v-sfx-{task_id[:8]}"
    return await _save_task_artifacts(body, out_path, base, task_id)


@mcp.tool(
    description=(
        "Check a sound-effects, audio-ducking, video-to-video, or async "
        "video-to-music generation task and, if finished, download its "
        "result file(s). Use this to recover a result when text_to_sfx, "
        "video_to_sfx, audio_ducking, video_to_video_music, "
        "video_to_video_sfx, or video_to_music(preserve_speech=true) timed "
        "out — their error message contains the task_id. Does not poll: a "
        "single status check per call. This tool itself never charges.\n\n"
        "Args:\n"
        "    task_id (str): The task id returned in the timeout message.\n"
        "    output_directory (str, optional): Where to save result files. "
        "Defaults to SONILO_MCP_BASE_PATH.\n\n"
        "Returns:\n"
        "    Still processing -> a status message; try again later. "
        "Succeeded -> the saved file path(s): audio, plus video for "
        "video_to_sfx tasks; a single .wav or .mp4 for audio_ducking "
        "tasks; a single .mp4 for video_to_video_music/video_to_video_sfx "
        "tasks; for a video_to_music(preserve_speech=true) task, the audio "
        "stream(s) plus the preserved speech ('vocals') stem plus the mux "
        "(speech+music mixed — the ready-to-use combined result). "
        "Failed -> an error including whether the charge was refunded."
    )
)
async def get_sfx_task(
    task_id: str,
    output_directory: str | None = None,
) -> list[TextContent]:
    try:
        body = await _http_get_json(f"/v1/tasks/{task_id}")
    except Exception as e:
        if _is_task_not_found(e):
            # A 404 for a bad/typo'd id, or for an id /v1/tasks does not
            # serve — it serves SFX and audio-ducking tasks, so a streaming
            # (e.g. music) generation's id 404s here. Retrying can never
            # help, so raise the original error as-is with no "call
            # get_sfx_task again" advice attached.
            raise
        if _is_transient_error(e):
            # Structurally identical to _poll_task's GET — a transient
            # failure here (e.g. backend 5xx) must not read as if the paid
            # result is lost: reassure the caller it's still on the backend
            # and point them back at this same tool.
            raise Exception(
                f"{_end_sentence(e)} The result for task {task_id} may "
                "still be available on the backend — call "
                f'get_sfx_task("{task_id}") again shortly.'
            ) from e
        # A non-404 4xx (401 key rotated, 402 billing suspended, ...) — the
        # task was already charged and its result still exists on the
        # backend. The caller must fix the underlying cause first, then
        # recover the result with this same tool.
        raise Exception(
            f"{_end_sentence(e)} Task {task_id} was already charged — "
            f'resolve the issue above, then call get_sfx_task("{task_id}") '
            "to retrieve the result."
        ) from e
    body = _require_task_body(body, task_id)
    if body.get("status") == "processing":
        return [TextContent(
            type="text",
            text=(
                f"Task {task_id} is still processing. Try again in a "
                "little while."
            ),
        )]
    out_path = _make_output_path(output_directory)
    # No prompt available on recovery — name by task id; extension comes
    # from the envelope's content_type.
    if _is_music_task_envelope(body):
        # Async (isolate_vocals) video-to-music: list-shaped `audio` plus
        # optional `vocals`/`mux` — needs the music-aware save layer, not
        # _save_task_artifacts's single-dict-audio assumption. No
        # reuse_existing here: _save_music_task_artifacts doesn't support it
        # (see its docstring — video-to-music has no dedicated recovery
        # tool of its own to dedupe repeat calls for), so a second
        # get_sfx_task call on an already-recovered music task lands in
        # freshly -1/-2-suffixed files rather than being detected as a
        # duplicate.
        return await _save_music_task_artifacts(
            body, out_path, f"music-{task_id[:8]}", task_id
        )
    return await _save_task_artifacts(
        body, out_path, f"sfx-{task_id[:8]}", task_id, reuse_existing=True
    )


# ---------- Tools: audio ducking ----------


def _require_exactly_one(path: str | None, url: str | None, label: str) -> None:
    """Exactly-one-of check for a ducking input pair.

    Called for BOTH inputs before any I/O, mirroring the backend's
    _validate_presence: a missing music input must be reported even when the
    voice input is what would have failed to resolve.
    """
    if path and url:
        raise Exception(
            f"Provide either {label}_path or {label}_url (exactly one, not both)"
        )
    if not path and not url:
        raise Exception(f"Provide either {label}_path or {label}_url")


@mcp.tool(
    description=(
        "Duck a music bed under a voice track: Sonilo lowers the music "
        "wherever the voice is speaking and lifts it back in the gaps, then "
        "returns the mixed result. The voice input may be a video — its "
        "audio track is used as the voice, and the ducked mix is muxed back "
        "into a new video.\n\n"
        "⚠️ COST WARNING: This tool makes an API call to Sonilo which may "
        "incur charges. Only use when explicitly requested by the user.\n\n"
        "Args:\n"
        "    voice_path (str, optional): Absolute local path, or relative to "
        "SONILO_MCP_BASE_PATH. Audio (.wav/.mp3/.m4a/.aac/.ogg/.flac) or "
        "video (.mp4/.mov/.avi/.wmv/.webm/.mkv).\n"
        "    voice_url (str, optional): HTTPS URL to the voice audio/video.\n"
        "    music_path (str, optional): Absolute local path, or relative to "
        "SONILO_MCP_BASE_PATH. Audio only.\n"
        "    music_url (str, optional): HTTPS URL to the music audio.\n"
        "    output_directory (str, optional): Where to save the result. "
        "Defaults to SONILO_MCP_BASE_PATH.\n\n"
        "Exactly one of voice_path/voice_url, and exactly one of "
        "music_path/music_url, must be provided. A local file and a URL may "
        "be mixed across the two inputs. Each input is capped at 360 seconds "
        "(6 minutes) and by the account's upload-size limit (typically "
        "300 MB).\n\n"
        "Returns:\n"
        "    TextContent with the absolute path of the saved file: a .wav, "
        "or a .mp4 when the voice input was a video. If the call times out, "
        "the error message includes the task_id — recover the result later "
        "with get_sfx_task."
    )
)
async def audio_ducking(
    voice_path: str | None = None,
    voice_url: str | None = None,
    music_path: str | None = None,
    music_url: str | None = None,
    output_directory: str | None = None,
) -> list[TextContent]:
    # Both pairs are validated before any I/O — see _require_exactly_one.
    _require_exactly_one(voice_path, voice_url, "voice")
    _require_exactly_one(music_path, music_url, "music")
    if voice_url:
        _require_http_url(voice_url, "voice")
    if music_url:
        _require_http_url(music_url, "music")

    out_path = _make_output_path(output_directory)
    cfg = _get_config()

    # Resolve local inputs (existence + extension + base-path confinement)
    # before anything hits the network: a bad path is a caller error and
    # should not cost an /v1/account/services round trip.
    #
    # A voice input may be audio OR video: the backend extracts a video's
    # audio track and re-muxes the ducked result back into it. Music is audio
    # only — the backend never probes it for a video stream, so a video there
    # would be silently mishandled.
    voice_file = (
        _resolve_input_file(
            voice_path,
            cfg["base_path"],
            _AUDIO_EXTS | _VIDEO_EXTS,
            "audio or video",
        )
        if voice_path
        else None
    )
    music_file = (
        _resolve_input_file(music_path, cfg["base_path"], _AUDIO_EXTS, "audio")
        if music_path
        else None
    )

    data: dict = {}
    files: dict = {}
    max_mb = await _get_max_upload_size_mb() if (voice_file or music_file) else 0

    if voice_file is not None:
        size_mb = voice_file.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            # Cheap fail-fast before a wasted ffprobe run. NOT authoritative —
            # see _read_capped.
            raise Exception(
                f"Voice file is too large ({size_mb:.1f} MB > {max_mb} MB cap)"
            )
        await _check_media_duration(
            str(voice_file), max_seconds=_DUCKING_MAX_DURATION_SECONDS
        )
        mime, _ = mimetypes.guess_type(voice_file.name)
        files["voice_file"] = (
            voice_file.name,
            _read_capped(voice_file, max_mb, "Voice"),
            mime or "application/octet-stream",
        )
    else:
        await _check_media_duration(
            voice_url, max_seconds=_DUCKING_MAX_DURATION_SECONDS
        )
        data["voice_url"] = voice_url

    if music_file is not None:
        size_mb = music_file.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            raise Exception(
                f"Music file is too large ({size_mb:.1f} MB > {max_mb} MB cap)"
            )
        await _check_media_duration(
            str(music_file), max_seconds=_DUCKING_MAX_DURATION_SECONDS
        )
        mime, _ = mimetypes.guess_type(music_file.name)
        files["music_file"] = (
            music_file.name,
            _read_capped(music_file, max_mb, "Music"),
            mime or "application/octet-stream",
        )
    else:
        await _check_media_duration(
            music_url, max_seconds=_DUCKING_MAX_DURATION_SECONDS
        )
        data["music_url"] = music_url

    # Everything below this line can charge the user: no validation past here.
    task_id = await _post_task_submit(
        "/v1/audio-ducking", data=data or None, files=files or None
    )
    body = await _poll_task(task_id, cfg["timeout"])
    base = _ducking_base_name(voice_path, voice_url, task_id)
    return await _save_task_artifacts(body, out_path, base, task_id)


# ---------- Tools: account ----------

@mcp.tool(
    description=(
        "Get the authenticated account's available Sonilo services, rate limits, "
        "concurrency limit, discount factor, and max video upload size. "
        "Use this to discover what generation endpoints are available before "
        "calling them."
    )
)
async def get_account_services() -> dict:
    body = await _http_get_json("/v1/account/services")
    return _require_account_body(body, "/v1/account/services")


@mcp.tool(
    description=(
        "Get the authenticated account's usage summary and per-day breakdown. "
        "Useful for cost reconciliation and tracking generation history.\n\n"
        "Args:\n"
        "    days (int, optional): Lookback window in days, 1–365. Defaults to 30."
    )
)
async def get_usage(days: int = 30) -> dict:
    if not (1 <= days <= 365):
        raise Exception(f"days must be between 1 and 365 (got {days})")
    body = await _http_get_json("/v1/account/usage", params={"days": days})
    return _require_account_body(body, "/v1/account/usage")


# ---------- Tools: local playback ----------

@mcp.tool(
    description=(
        "Play a local audio file through the system's default output device. "
        "Supports WAV, MP3, M4A, AAC, OGG, FLAC.\n\n"
        "Args:\n"
        "    input_file_path (str): Absolute path or relative to "
        "SONILO_MCP_BASE_PATH.\n\n"
        "Returns:\n"
        "    Success message including the played path."
    )
)
def play_audio(input_file_path: str) -> TextContent:
    cfg = _get_config()
    path = _resolve_input_file(
        input_file_path, cfg["base_path"], _AUDIO_EXTS, "audio"
    )

    # Prefer a system audio player — handles mp3/aac/m4a natively via OS
    # codecs. `soundfile`'s bundled libsndfile has weak/no mp3 support
    # depending on platform, so falling through to it for mp3 fails with
    # "Format not recognised".
    player_cmd: list[str] | None = None
    if sys.platform == "darwin" and shutil.which("afplay"):
        player_cmd = ["afplay", str(path)]
    elif sys.platform.startswith("linux"):
        if path.suffix.lower() == ".mp3" and shutil.which("mpg123"):
            player_cmd = ["mpg123", "-q", str(path)]
        elif shutil.which("aplay"):
            player_cmd = ["aplay", "-q", str(path)]

    if player_cmd is not None:
        try:
            subprocess.run(player_cmd, check=True, capture_output=True)
        except subprocess.CalledProcessError as e:
            stderr = e.stderr.decode("utf-8", errors="replace") if e.stderr else ""
            raise Exception(f"{player_cmd[0]} failed: {stderr.strip() or e}") from e
        return TextContent(
            type="text",
            text=f"Successfully played audio file: {path}",
        )

    # Fallback for platforms without a known system player: try
    # sounddevice + soundfile (works reliably for WAV/FLAC/OGG; mp3
    # support depends on the bundled libsndfile build).
    try:
        import sounddevice as sd
        import soundfile as sf
    except ModuleNotFoundError as e:
        raise Exception(
            "Audio playback requires a system player (afplay on macOS, "
            "mpg123/aplay on Linux) or the `sounddevice` and `soundfile` "
            "Python packages."
        ) from e

    audio_bytes = path.read_bytes()
    data, samplerate = sf.read(io.BytesIO(audio_bytes))
    sd.play(data, samplerate)
    sd.wait()
    return TextContent(
        type="text",
        text=f"Successfully played audio file: {path}",
    )


# ---------- Entry point ----------

def main() -> None:
    """Run the MCP server over stdio transport."""
    print("Starting Sonilo MCP server", flush=True, file=sys.stderr)
    mcp.run()


if __name__ == "__main__":
    main()
