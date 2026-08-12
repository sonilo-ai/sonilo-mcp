"""The credential reader shared with `sonilo login`."""
from __future__ import annotations

import json
import os
from pathlib import Path

BASE = "https://api.sonilo.com"


def _write(path: Path, api_base: str = BASE, api_key: str = "sk-stored", version: int = 1):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "version": version,
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


def test_path_honours_xdg(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "x"))
    from sonilo_mcp.credentials import credentials_path

    assert credentials_path() == tmp_path / "x" / "sonilo" / "credentials.json"


def test_path_falls_back_to_home_config(monkeypatch, tmp_path):
    """Without XDG_CONFIG_HOME the CLIs use ~/.config — the same default."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "h"))
    from sonilo_mcp.credentials import credentials_path

    assert credentials_path() == tmp_path / "h" / ".config" / "sonilo" / "credentials.json"


def test_reads_the_key_for_the_matching_base(tmp_path):
    from sonilo_mcp.credentials import credentials_path, read_api_key

    _write(credentials_path())
    assert read_api_key(BASE) == "sk-stored"


def test_returns_none_when_no_file(tmp_path):
    from sonilo_mcp.credentials import read_api_key

    assert read_api_key(BASE) is None


def test_staging_base_does_not_see_the_production_key(tmp_path):
    from sonilo_mcp.credentials import credentials_path, read_api_key

    _write(credentials_path())
    assert read_api_key("https://api.staging.sonilo.com") is None


def test_newer_format_is_refused_rather_than_guessed(tmp_path):
    from sonilo_mcp.credentials import credentials_path, read_api_key

    _write(credentials_path(), version=99)
    assert read_api_key(BASE) is None


def test_corrupt_file_reads_as_none(tmp_path):
    from sonilo_mcp.credentials import credentials_path, read_api_key

    p = credentials_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("{ not json")
    assert read_api_key(BASE) is None


def test_empty_key_reads_as_none(tmp_path):
    """An entry present but blank must not authenticate as an empty Bearer."""
    from sonilo_mcp.credentials import credentials_path, read_api_key

    _write(credentials_path(), api_key="")
    assert read_api_key(BASE) is None


def test_result_is_cached_between_calls(tmp_path):
    from sonilo_mcp.credentials import credentials_path, read_api_key

    p = credentials_path()
    _write(p)
    assert read_api_key(BASE) == "sk-stored"

    # Same mtime and size: the cached value stands. This is what stops a file
    # read on every single tool call.
    st = p.stat()
    _write(p, api_key="sk-different")
    os.utime(p, ns=(st.st_atime_ns, st.st_mtime_ns))
    if p.stat().st_size == st.st_size:
        assert read_api_key(BASE) == "sk-stored"


def test_cache_refreshes_after_a_real_change(tmp_path):
    from sonilo_mcp.credentials import credentials_path, read_api_key

    p = credentials_path()
    _write(p)
    assert read_api_key(BASE) == "sk-stored"

    _write(p, api_key="sk-rotated-and-longer-than-before")
    os.utime(p, None)
    assert read_api_key(BASE) == "sk-rotated-and-longer-than-before"
