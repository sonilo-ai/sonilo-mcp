"""Sonilo MCP server — exposes Sonilo's /v1/* API as MCP tools over stdio."""
from __future__ import annotations

import asyncio
import base64
import binascii
import json
import os
import re
import time
from pathlib import Path
from typing import AsyncIterator

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.types import TextContent

mcp = FastMCP("Sonilo")


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
        "timeout": float(os.getenv("TIME_OUT_SECONDS", "300")),
    }


# ---------- Path helpers ----------

def _is_file_writeable(path: Path) -> bool:
    if path.exists():
        return os.access(path, os.W_OK)
    return os.access(path.parent if path.parent.exists() else path.parent.parent, os.W_OK)


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
    if not os.path.isabs(file_path):
        if not base_path:
            raise Exception(
                "File path must be absolute when SONILO_MCP_BASE_PATH is not set"
            )
        path = Path(os.path.expanduser(base_path)) / file_path
    else:
        path = Path(os.path.expanduser(file_path))

    if not path.exists():
        raise Exception(f"File ({path}) does not exist")
    if not path.is_file():
        raise Exception(f"File ({path}) is not a file")
    if path.suffix.lower() not in allowed_exts:
        raise Exception(f"File ({path}) is not a recognized {kind} format")
    return path


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
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}

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

    Event types per backend/app/services/stream_events.py:
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
    headers = {"Authorization": f"Bearer {cfg['api_key']}"}

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
        out_file = output_path / f"{safe_title}{suffix}.mp3"
        out_file.write_bytes(bytes(buf))
        saved.append(TextContent(
            type="text",
            text=f"Success. File saved as: {out_file}",
        ))
    if not saved:
        raise Exception("Stream completed but no audio chunks were received")
    return saved


# ---------- Tools: generation ----------

@mcp.tool(
    description=(
        "Generate music from a text prompt and save the resulting audio "
        "file(s) to a local directory.\n\n"
        "⚠️ COST WARNING: This tool makes an API call to Sonilo which may "
        "incur charges. Only use when explicitly requested by the user.\n\n"
        "Args:\n"
        "    prompt (str): Description of the music to generate "
        "(1–1000 chars).\n"
        "    duration (int): Length in seconds (1–300).\n"
        "    output_directory (str, optional): Absolute path, or relative "
        "to SONILO_MCP_BASE_PATH. Defaults to SONILO_MCP_BASE_PATH "
        "(~/Desktop unless overridden).\n\n"
        "Returns:\n"
        "    One TextContent per generated audio stream, each containing "
        "the absolute path of the saved .mp3 file."
    )
)
async def text_to_music(
    prompt: str,
    duration: int,
    output_directory: str | None = None,
) -> list[TextContent]:
    out_path = _make_output_path(output_directory)
    return await _post_streaming_generation(
        "/v1/text-to-music",
        out_path,
        json_body={"prompt": prompt, "duration": duration},
    )


_services_cache: dict | None = None
_services_cache_expiry: float = 0.0
_SERVICES_CACHE_TTL = 300  # seconds


async def _get_max_upload_size_mb() -> int:
    """Cached lookup of /v1/account/services.max_upload_size_mb.

    Returns 300 if the call fails (matches the backend default in
    backend/app/routers/v1/video_to_music.py).
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
    except (TypeError, ValueError):
        return 300


@mcp.tool(
    description=(
        "Generate music that matches the soundtrack of a video. Provide "
        "either a local video file path or a publicly accessible video URL.\n\n"
        "⚠️ COST WARNING: This tool makes an API call to Sonilo which may "
        "incur charges. Only use when explicitly requested by the user.\n\n"
        "Args:\n"
        "    video_path (str, optional): Absolute local path, or relative "
        "to SONILO_MCP_BASE_PATH. Supports .mp4/.mov/.avi/.wmv/.webm/.mkv.\n"
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
        data = {"prompt": prompt} if prompt else None
        with open(resolved, "rb") as fh:
            files = {"video": (resolved.name, fh.read(), "video/mp4")}
        return await _post_streaming_generation(
            "/v1/video-to-music",
            out_path,
            data=data,
            files=files,
        )

    # video_url path — backend expects multipart form, not JSON
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


# ---------- Entry point ----------

def main() -> None:
    """Run the MCP server over stdio transport."""
    print("Starting Sonilo MCP server", flush=True)
    mcp.run()


if __name__ == "__main__":
    main()
