# Sonilo MCP Server

An MCP (Model Context Protocol) server that exposes [Sonilo](https://platform.sonilo.com)'s AI music generation API to MCP-compatible clients (Claude Desktop, Cursor, etc.).

## Install

```bash
uvx sonilo-mcp
```

Or install with pip:

```bash
pip install sonilo-mcp
```

### Audio Playback Dependencies

The `play_audio` tool requires PortAudio at runtime (for `sounddevice`). On macOS/Linux, install via:

- **macOS**: `brew install portaudio`
- **Debian/Ubuntu**: `sudo apt-get install libportaudio2`

`uvx sonilo-mcp` and `pip install` will pull the Python bindings, but the system PortAudio library must be installed separately. The other tools (`text_to_music`, `video_to_music`, `get_account_services`, `get_usage`) work without PortAudio.

## Configuration

### Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json` (macOS) or `%APPDATA%\Claude\claude_desktop_config.json` (Windows):

```json
{
  "mcpServers": {
    "sonilo": {
      "command": "uvx",
      "args": ["sonilo-mcp"],
      "env": {
        "SONILO_API_KEY": "sk_live_..."
      }
    }
  }
}
```

Get your API key at <https://platform.sonilo.com/dashboard/api-keys>.

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SONILO_API_KEY` | _(required)_ | Bearer token. |
| `SONILO_API_URL` | `https://api.sonilo.com` | Public API base URL. |
| `SONILO_MCP_BASE_PATH` | `~/Desktop` | Default output directory and base for relative input paths. |
| `TIME_OUT_SECONDS` | `300` | Generation timeout. |

## Tools

| Tool | Description | Cost |
|---|---|---|
| `text_to_music(prompt, duration, output_directory?)` | Generate music from a text prompt. | ✅ |
| `video_to_music(video_path? \| video_url?, prompt?, output_directory?)` | Generate music matched to a video. | ✅ |
| `get_account_services()` | List available services and limits. | ❌ |
| `get_usage(days=30)` | Show usage summary + per-day breakdown. | ❌ |
| `play_audio(input_file_path)` | Play a local audio file. | ❌ |

Tools marked ✅ make API calls that incur charges on your Sonilo account.

## Output Format

Generated audio is currently saved as `.mp3`. File names use the title returned by the backend (slugified) or a `sonilo-<timestamp>.mp3` fallback. When multiple parallel streams are returned, a `-<index>` suffix is appended.

## Common Errors

| Message | What to do |
|---|---|
| `Invalid SONILO_API_KEY` | Verify the key at <https://platform.sonilo.com/dashboard/api-keys>. |
| `Insufficient minutes` / `Credit limit exceeded` | Top up at <https://platform.sonilo.com/dashboard/billing>. |
| `Rate limit exceeded` | Check `get_account_services` for your rpm/concurrency limits. |
| `Generation timed out` | Raise `TIME_OUT_SECONDS`. Check `get_usage` to confirm whether the backend completed and charged. |

## Development

```bash
cd mcp
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
pytest
```

## License

MIT
