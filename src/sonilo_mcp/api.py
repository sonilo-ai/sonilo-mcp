"""Sonilo MCP server — exposes Sonilo's /v1/* API as MCP tools over stdio."""
from __future__ import annotations

import asyncio
import json
import os
import re
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

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
