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


async def _check_video_duration(
    source: str, max_seconds: int = _MAX_VIDEO_DURATION_SECONDS
) -> None:
    """Best-effort local ffprobe pre-check of a video's duration.

    Raises if the duration is known to exceed `max_seconds`, so the caller
    fails fast instead of uploading a video the backend will reject.
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
    """Pull the `detail` field out of a FastAPI error body, falling back to the raw body."""
    try:
        parsed = json.loads(body)
        if isinstance(parsed, dict) and "detail" in parsed:
            return str(parsed["detail"])
        return body
    except (json.JSONDecodeError, TypeError):
        return body


_BILLING_URL = "https://platform.sonilo.com/dashboard/billing"
_API_KEYS_URL = "https://platform.sonilo.com/dashboard/api-keys"


def _raise_http_error(status_code: int, body: str) -> None:
    """Map a backend HTTP error to a clear user-facing exception. Always raises."""
    detail = _extract_detail(body)
    if status_code == 401:
        raise Exception(
            f"Invalid SONILO_API_KEY — verify the key at {_API_KEYS_URL}"
        )
    if status_code == 402:
        if "minute" in detail.lower() or "credit" in detail.lower():
            raise Exception(f"{detail}. Top up at {_BILLING_URL}")
        raise Exception(detail)
    if status_code == 413:
        raise Exception(f"File too large: {detail}")
    if status_code == 422:
        raise Exception(detail)
    if status_code == 429:
        raise Exception(f"Rate limit exceeded: {detail}")
    if 400 <= status_code < 500:
        raise Exception(detail)
    if 500 <= status_code:
        raise Exception(
            f"Server error ({status_code}): {detail}. Please retry shortly."
        )
    raise Exception(f"Unexpected status {status_code}: {detail}")


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
            raise Exception(f"HTTP request failed: {e}") from e


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
            f"HTTP request failed: {e}. Verify SONILO_API_URL "
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
        raise Exception(
            f"HTTP request failed: {e}. Verify SONILO_API_URL "
            f"({cfg['api_url']}) is reachable."
        ) from e
    if r.status_code >= 400:
        _raise_http_error(r.status_code, r.text)
    try:
        task_id = r.json().get("task_id")
    except json.JSONDecodeError:
        task_id = None
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
            raise Exception(
                f"{_end_sentence(e)} The generation task {task_id} may still "
                f'be running on the backend — call get_sfx_task("{task_id}") '
                "later to retrieve the result."
            ) from e
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
    return _AUDIO_CONTENT_TYPE_EXTS.get((content_type or "").lower(), ".m4a")


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
        raise Exception(f"Artifact download failed: {e}") from e
    except BaseException:
        dest.unlink(missing_ok=True)
        raise


async def _save_task_artifacts(
    body: dict, output_path: Path, base_name: str, task_id: str
) -> list[TextContent]:
    """Turn a terminal /v1/tasks/{id} body into saved local files.

    failed -> raise with the backend's error code/message and whether the
    charge was refunded. succeeded -> download audio (always present) and
    video (video-to-sfx only), one TextContent per saved file.

    task_id must be the caller's own known-good id (from _post_task_submit
    or the tool's own task_id argument), NOT derived from body — the
    backend's terminal body is not a trustworthy source for the recovery
    id, and a missing/incorrect id here would make a paid result
    unrecoverable.
    """
    task_status = body.get("status")
    if task_status == "failed":
        err = body.get("error") or {}
        code = err.get("code") or "GENERATION_FAILED"
        message = err.get("message") or "Generation failed"
        if body.get("refunded"):
            refund_line = "The charge was reversed — you were not billed."
        else:
            refund_line = (
                "The charge has not been reversed — check get_usage to "
                "reconcile."
            )
        raise Exception(
            f"Generation failed ({code}): {message}. {refund_line} "
            f"Task id: {task_id}."
        )
    if task_status != "succeeded":
        raise Exception(f"Unexpected task status: {task_status}")

    audio = body.get("audio")
    if not isinstance(audio, dict) or not audio.get("url"):
        raise Exception("Task succeeded but no audio artifact was returned")

    saved: list[TextContent] = []
    try:
        dest = _artifact_dest(
            output_path, base_name, _ext_from_content_type(audio.get("content_type"))
        )
        await _download_artifact(audio["url"], dest)
    except Exception as e:
        # The backend already succeeded and charged for this task by the
        # time we get here, so ANY failure in this block — computing the
        # destination path or downloading — must still carry the task_id
        # and recovery hint. Otherwise a paid result becomes unrecoverable
        # from the error message alone. _download_artifact (and any other
        # failure here, e.g. an OSError from _artifact_dest) states only the
        # bare fact of the failure — this is the one layer that owns the
        # single, complete recovery instruction, so it isn't repeated.
        raise Exception(
            f"{_end_sentence(e)} The result is still stored on the backend "
            f'— call get_sfx_task("{task_id}") to retry.'
        ) from e
    saved.append(TextContent(type="text", text=f"Success. File saved as: {dest}"))
    audio_dest = dest

    video = body.get("video")
    if isinstance(video, dict) and video.get("url"):
        try:
            dest = _artifact_dest(output_path, base_name, ".mp4")
            await _download_artifact(video["url"], dest)
        except Exception as e:
            raise Exception(
                f"{_end_sentence(e)} The audio file was already saved as: "
                f"{audio_dest}. The video is still stored on the backend — "
                f'call get_sfx_task("{task_id}") to retry the download.'
            ) from e
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
            raise Exception(
                f"Video file is too large ({size_mb:.1f} MB > {max_mb} MB cap)"
            )
        await _check_video_duration(str(resolved))
        data = {"prompt": prompt} if prompt else None
        mime, _ = mimetypes.guess_type(resolved.name)
        with open(resolved, "rb") as fh:
            files = {"video": (resolved.name, fh.read(), mime or "application/octet-stream")}
        return await _post_streaming_generation(
            "/v1/video-to-music",
            out_path,
            data=data,
            files=files,
        )

    # video_url path — backend expects multipart form, not JSON
    await _check_video_duration(video_url)
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
            raise Exception(
                f"Video file is too large ({size_mb:.1f} MB > {max_mb} MB cap)"
            )
        await _check_video_duration(
            str(resolved), max_seconds=_SFX_MAX_VIDEO_DURATION_SECONDS
        )
        mime, _ = mimetypes.guess_type(resolved.name)
        with open(resolved, "rb") as fh:
            files = {
                "video": (
                    resolved.name, fh.read(), mime or "application/octet-stream"
                )
            }
        task_id = await _post_task_submit(
            "/v1/video-to-sfx", data=form or None, files=files
        )
    else:
        await _check_video_duration(
            video_url, max_seconds=_SFX_MAX_VIDEO_DURATION_SECONDS
        )
        form["video_url"] = video_url
        task_id = await _post_task_submit("/v1/video-to-sfx", data=form)

    body = await _poll_task(task_id, cfg["timeout"])
    base = _slugify(prompt) if prompt and prompt.strip() else f"sfx-{task_id[:8]}"
    return await _save_task_artifacts(body, out_path, base, task_id)


@mcp.tool(
    description=(
        "Check a sound-effects generation task and, if finished, download "
        "its result file(s). Use this to recover a result when text_to_sfx "
        "or video_to_sfx timed out — their error message contains the "
        "task_id. Does not poll: a single status check per call. This tool "
        "itself never charges.\n\n"
        "Args:\n"
        "    task_id (str): The task id returned in the timeout message.\n"
        "    output_directory (str, optional): Where to save result files. "
        "Defaults to SONILO_MCP_BASE_PATH.\n\n"
        "Returns:\n"
        "    Still processing -> a status message; try again later. "
        "Succeeded -> the saved file path(s) (audio, plus video for "
        "video_to_sfx tasks). Failed -> an error including whether the "
        "charge was refunded."
    )
)
async def get_sfx_task(
    task_id: str,
    output_directory: str | None = None,
) -> list[TextContent]:
    try:
        body = await _http_get_json(f"/v1/tasks/{task_id}")
    except Exception as e:
        # Structurally identical to _poll_task's GET — a transient failure
        # here (e.g. backend 5xx) must not read as if the paid result is
        # lost: reassure the caller it's still on the backend and point
        # them back at this same tool.
        raise Exception(
            f"{_end_sentence(e)} The result for task {task_id} may still be "
            f'available on the backend — call get_sfx_task("{task_id}") '
            "again shortly."
        ) from e
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
    return await _save_task_artifacts(body, out_path, f"sfx-{task_id[:8]}", task_id)


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
    return await _http_get_json("/v1/account/services")


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
    return await _http_get_json("/v1/account/usage", params={"days": days})


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
