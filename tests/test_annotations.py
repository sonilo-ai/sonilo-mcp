"""MCP directories (the Claude connector directory first) and clients read
tool annotations to tell free lookups from paid or file-writing calls, and
read the server instructions before the first call."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from sonilo_mcp.api import mcp

READ_ONLY = {"get_account_services", "get_usage", "play_audio"}
TAKES_A_URL = {
    "video_to_music", "video_to_sfx", "video_to_video_music", "video_to_video_sfx",
    "video_to_sound", "video_to_video_sound", "dubbing", "proofread",
    "analyze_video", "audio_ducking",
}


def _tools():
    return asyncio.run(mcp.list_tools())


def test_every_tool_has_a_title_and_annotations():
    tools = _tools()
    assert len(tools) == 16
    for tool in tools:
        a = tool.annotations
        assert tool.title, tool.name
        assert a is not None, tool.name
        assert a.destructiveHint is False, tool.name  # outputs never overwrite a file
        assert a.readOnlyHint == (tool.name in READ_ONLY), tool.name
        assert a.openWorldHint == (tool.name in TAKES_A_URL), tool.name
        if not a.readOnlyHint:
            assert a.idempotentHint is False, tool.name


def test_instructions_name_the_balance_check_and_the_recovery_tool():
    names = {tool.name for tool in _tools()}
    for name in ("get_account_services", "get_sfx_task", "video_to_sound"):
        assert name in mcp.instructions
        assert name in names


def test_registry_entry_lists_the_hosted_server_and_the_package():
    server = json.loads((Path(__file__).parent.parent / "server.json").read_text())
    assert server["remotes"] == [{"type": "streamable-http", "url": "https://api.sonilo.com/mcp"}]
    assert server["packages"][0]["identifier"] == "sonilo-mcp"
