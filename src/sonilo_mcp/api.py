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


# The backend rejects videos longer than this, so we pre-check locally to fail
# fast and skip a wasted upload. Keep in sync with the backend's limit.
_MAX_VIDEO_DURATION_SECONDS = 360  # 6 minutes — music endpoints
_SFX_MAX_VIDEO_DURATION_SECONDS = 180  # 3 minutes — /v1/video-to-sfx


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
    except (asyncio.TimeoutError, OSError):
        return  # fail open — let the backend decide
    if proc.returncode != 0:
        return
    try:
        duration = float(json.loads(stdout)["format"]["duration"])
    except (json.JSONDecodeError, KeyError, ValueError, TypeError):
        return
    if duration > max_seconds:
        raise Exception(
            f"Video duration {duration:.1f}s exceeds the maximum of "
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

    Only a 404 means that: a bad/typo'd id, or a non-SFX (e.g. music) task
    id — /v1/tasks is SFX-only and 404s those. Every OTHER error (401, 402,
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
# video. Its envelope carries no content_type, so the type is fixed here
# from output_type — these strings feed _ext_from_content_type, so
# "audio/wav" must stay a key of _AUDIO_CONTENT_TYPE_EXTS.
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

    Returns `(body, False)` when there is no usable `output_url` — every SFX
    body, and any malformed ducking body, which is then held to the SFX
    contract and falls through to _save_task_artifacts' missing-artifact
    error, task_id and all. Never mutates the caller's dict.
    """
    url = body.get("output_url")
    if not isinstance(url, str) or not url:
        return body, False
    output_type = body.get("output_type")
    slot = "video" if output_type == "video" else "audio"
    normalized = dict(body)
    normalized[slot] = {"url": url, "content_type": _DUCKING_CONTENT_TYPES[slot]}
    return normalized, True


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
    artifact is required: an SFX task always has audio (plus video for
    video-to-sfx). The sole exemption is an audio-ducking task with a video
    voice input, whose only artifact is the re-muxed mp4 (see
    _normalize_task_envelope, which reports whether it saw that envelope).

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

    body, is_ducking = _normalize_task_envelope(body)

    def _has_artifact(slot: object) -> bool:
        return isinstance(slot, dict) and bool(slot.get("url"))

    audio = body.get("audio")
    video = body.get("video")
    # An audio artifact is required, with exactly one exemption: a ducking
    # task whose voice input was a video renders a single artifact, the
    # re-muxed mp4, and has no audio slot at all. The exemption is keyed off
    # _normalize_task_envelope having recognized a ducking envelope — NOT off
    # "some artifact is present". An SFX body is still held to the audio
    # requirement even when it carries a video: a video-without-audio SFX
    # result means the audio half of an already-charged generation went
    # missing, and quietly downloading just the mp4 would report success
    # while losing it, with no recovery hint. Raising here keeps the task_id
    # and the get_sfx_task call in the user's hands, which is the only way a
    # paid result stays recoverable from the error message alone.
    if not _has_artifact(audio) and not (is_ducking and _has_artifact(video)):
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
        "    output_directory (str, optional): Absolute path, or relative "
        "to SONILO_MCP_BASE_PATH. Defaults to SONILO_MCP_BASE_PATH "
        "(~/Desktop unless overridden).\n\n"
        "Returns:\n"
        "    One TextContent per generated audio stream, each containing "
        "the absolute path of the saved .m4a file (AAC in MP4 container)."
    )
)
async def text_to_music(
    prompt: str,
    duration: int,
    output_directory: str | None = None,
) -> list[TextContent]:
    out_path = _make_output_path(output_directory)
    # The backend's text-to-music endpoint expects form fields, not a JSON
    # body (same as video-to-music). Sending JSON yields a 422
    # "Field required" for prompt/duration.
    return await _post_streaming_generation(
        "/v1/text-to-music",
        out_path,
        data={"prompt": prompt, "duration": duration},
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
        "    output_directory (str, optional): Where to save the resulting "
        "audio file(s). Defaults to SONILO_MCP_BASE_PATH.\n\n"
        "Exactly one of video_path and video_url must be provided.\n\n"
        "Returns:\n"
        "    One TextContent per generated audio stream."
    )
)
async def video_to_music(
    video_path: str | None = None,
    video_url: str | None = None,
    prompt: str | None = None,
    output_directory: str | None = None,
) -> list[TextContent]:
    if (video_path and video_url) or (not video_path and not video_url):
        raise Exception(
            "Provide either video_path or video_url (exactly one, not both)"
        )

    if video_url:
        # Restrict to http(s) before the URL ever reaches the local ffprobe
        # pre-check or the backend. Without this, a caller (e.g. via prompt
        # injection) could pass file:// to probe local files, an internal
        # address to trigger SSRF from this machine, or a "-"-prefixed value
        # to inject ffprobe flags.
        scheme = urlparse(video_url).scheme.lower()
        if scheme not in ("http", "https"):
            raise Exception("video_url must be an http:// or https:// URL")

    out_path = _make_output_path(output_directory)
    cfg = _get_config()

    if video_path:
        resolved = _resolve_input_file(
            video_path, cfg["base_path"], _VIDEO_EXTS, "video"
        )
        max_mb = await _get_max_upload_size_mb()
        size_mb = resolved.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            # Cheap fail-fast: avoids a wasted ffprobe run for an
            # obviously-oversized file. NOT the authoritative check — see
            # the read-time check below.
            raise Exception(
                f"Video file is too large ({size_mb:.1f} MB > {max_mb} MB cap)"
            )
        await _check_media_duration(str(resolved))
        data = {"prompt": prompt} if prompt else None
        mime, _ = mimetypes.guess_type(resolved.name)
        # Authoritative check: the stat() above is now stale — an await on
        # ffprobe sat between it and this read, during which the file could
        # have been replaced or grown (a video export still writing, a sync
        # tool, a concurrent process). Enforce the cap on the bytes actually
        # read for upload, not on the earlier stat(). Reading at most
        # max_bytes + 1 both closes that race and bounds memory use.
        max_bytes = max_mb * 1024 * 1024
        with open(resolved, "rb") as fh:
            content = fh.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise Exception(f"Video file is too large (> {max_mb} MB cap)")
        files = {"video": (resolved.name, content, mime or "application/octet-stream")}
        return await _post_streaming_generation(
            "/v1/video-to-music",
            out_path,
            data=data,
            files=files,
        )

    # video_url path — backend expects multipart form, not JSON
    await _check_media_duration(video_url)
    form: dict = {"video_url": video_url}
    if prompt:
        form["prompt"] = prompt
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
        "creates matching SFX. Returns BOTH the sound-effects audio file "
        "and the finished video with the effects mixed in. Provide either a "
        "local video file path or a publicly accessible video URL. "
        "Generation is asynchronous on the backend; this tool waits for "
        "completion and returns the saved file paths.\n\n"
        "⚠️ COST WARNING: This tool makes an API call to Sonilo which may "
        "incur charges. Only use when explicitly requested by the user.\n\n"
        "Args:\n"
        "    video_path (str, optional): Absolute local path, or relative "
        "to SONILO_MCP_BASE_PATH. Supports .mp4/.mov/.avi/.wmv/.webm/.mkv. "
        "Subject to the account's max upload size (typically 300 MB). "
        "Maximum video duration is 180 seconds (3 minutes).\n"
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
        "    Two TextContents: the saved audio file path and the saved "
        ".mp4 video path. If the call times out, the error message "
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
        scheme = urlparse(video_url).scheme.lower()
        if scheme not in ("http", "https"):
            raise Exception("video_url must be an http:// or https:// URL")

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
            video_path, cfg["base_path"], _VIDEO_EXTS, "video"
        )
        max_mb = await _get_max_upload_size_mb()
        size_mb = resolved.stat().st_size / (1024 * 1024)
        if size_mb > max_mb:
            # Cheap fail-fast: avoids a wasted ffprobe run for an
            # obviously-oversized file. NOT the authoritative check — see
            # the read-time check below.
            raise Exception(
                f"Video file is too large ({size_mb:.1f} MB > {max_mb} MB cap)"
            )
        await _check_media_duration(
            str(resolved), max_seconds=_SFX_MAX_VIDEO_DURATION_SECONDS
        )
        mime, _ = mimetypes.guess_type(resolved.name)
        # Authoritative check: the stat() above is now stale — an await on
        # ffprobe sat between it and this read, during which the file could
        # have been replaced or grown. Enforce the cap on the bytes actually
        # read for upload, not on the earlier stat(). Reading at most
        # max_bytes + 1 both closes that race and bounds memory use.
        max_bytes = max_mb * 1024 * 1024
        with open(resolved, "rb") as fh:
            content = fh.read(max_bytes + 1)
        if len(content) > max_bytes:
            raise Exception(f"Video file is too large (> {max_mb} MB cap)")
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


@mcp.tool(
    description=(
        "Check a sound-effects or audio-ducking generation task and, if "
        "finished, download its result file(s). Use this to recover a "
        "result when text_to_sfx, video_to_sfx, or audio_ducking timed out "
        "— their error message contains the task_id. Does not poll: a "
        "single status check per call. This tool itself never charges.\n\n"
        "Args:\n"
        "    task_id (str): The task id returned in the timeout message.\n"
        "    output_directory (str, optional): Where to save result files. "
        "Defaults to SONILO_MCP_BASE_PATH.\n\n"
        "Returns:\n"
        "    Still processing -> a status message; try again later. "
        "Succeeded -> the saved file path(s): audio, plus video for "
        "video_to_sfx tasks; a single .wav or .mp4 for audio_ducking "
        "tasks. Failed -> an error including whether the charge was "
        "refunded."
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
            # A 404 for a bad/typo'd id, or a music task's id — /v1/tasks
            # is SFX-only and 404s those. Retrying can never help, so raise
            # the original error as-is with no "call get_sfx_task again"
            # advice attached.
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
    return await _save_task_artifacts(
        body, out_path, f"sfx-{task_id[:8]}", task_id, reuse_existing=True
    )


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
