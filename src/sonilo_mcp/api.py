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
    output_path.mkdir(parents=True, exist_ok=True)
    return output_path


def _slugify(text: str) -> str:
    """Filesystem-safe slug. Fallback to 'sonilo' on empty input."""
    safe = re.sub(r"[^\w\s-]", "", text, flags=re.ASCII).strip().lower()
    safe = re.sub(r"[-\s]+", "-", safe).strip("-")
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

    Raises if the duration is known to exceed the backend's 360s cap, so the
    caller fails fast instead of uploading a video the backend will reject.
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
    return str(task_id)


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
