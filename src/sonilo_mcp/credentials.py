"""Read-only access to the credential file written by `sonilo login`.

Format and location are a cross-repo contract shared with the CLIs
(`sonilo-js/packages/cli/src/credentials.ts`, and the Python CLI once it lands).
This server only ever reads: it never creates, rewrites or repairs the file.

The path is fixed in code and cannot be influenced by any tool argument, which
is why it is read directly instead of through `api._is_within_base` — that
confinement exists to stop *caller-supplied* paths escaping
SONILO_MCP_BASE_PATH, and a credential living outside that base is deliberate.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

FORMAT_VERSION = 1

# (path, mtime_ns, size) -> parsed credentials mapping. `_get_config()` runs on
# every tool call, so an uncached reader would open this file constantly.
_cache: Dict[Tuple[str, int, int], Dict[str, Any]] = {}


def credentials_path() -> Path:
    """Where the CLIs store credentials: $XDG_CONFIG_HOME/sonilo, else ~/.config/sonilo."""
    base = os.environ.get("XDG_CONFIG_HOME")
    root = Path(base) if base else Path(os.environ.get("HOME", str(Path.home()))) / ".config"
    return root / "sonilo" / "credentials.json"


def clear_cache() -> None:
    """Drop the cache. Used by tests; not needed at runtime."""
    _cache.clear()


def _load() -> Dict[str, Any]:
    path = credentials_path()
    try:
        st = path.stat()
    except OSError:
        return {}
    key = (str(path), st.st_mtime_ns, st.st_size)
    cached = _cache.get(key)
    if cached is not None:
        return cached

    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Unreadable or malformed reads as "no credential" — the server still
        # works for anyone using SONILO_API_KEY, and `sonilo login` will
        # rewrite the file.
        parsed = {}

    creds: Dict[str, Any] = {}
    if isinstance(parsed, dict):
        version = parsed.get("version")
        # A newer format may mean fields we would misread. Ignoring the file is
        # safer than guessing at a credential.
        if not (isinstance(version, int) and version > FORMAT_VERSION):
            found = parsed.get("credentials")
            if isinstance(found, dict):
                creds = found

    _cache.clear()  # only ever one file; keep the cache single-entry
    _cache[key] = creds
    return creds


def read_api_key(api_base: str) -> Optional[str]:
    """The stored key for `api_base`, or None. Never raises."""
    entry = _load().get(api_base)
    if isinstance(entry, dict):
        key = entry.get("api_key")
        if isinstance(key, str) and key:
            return key
    return None
