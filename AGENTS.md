# AGENTS.md

Notes for coding agents working on `sonilo-mcp`, and for agents connecting to it.

## What this repo is

A local stdio MCP server (Python, FastMCP) that wraps Sonilo's public API:
music, sound effects, combined music + SFX, video analysis, audio ducking,
dubbing and proofreading. It uploads local files, waits on async tasks and
saves results to disk. Published to PyPI as `sonilo-mcp` and to the MCP
Registry as `io.github.sonilo-ai/sonilo-mcp`.

There is also a hosted, OAuth-authenticated server at
`https://api.sonilo.com/mcp`. It is not built from this repo.

## Layout

```
src/sonilo_mcp/api.py          every tool, HTTP calls, polling, downloads, path confinement; entry point `main`
src/sonilo_mcp/credentials.py  reads the `sonilo login` credential file
tests/test_api.py              tool tests (respx mocks the API; no network)
tests/test_credentials.py
tests/conftest.py              clears SONILO_* env vars and points HOME at a tmp dir
pyproject.toml                 package metadata and the real version
server.json                    MCP Registry manifest; release CI rewrites its version
uv.lock                        pins mcp to 1.x (2.x removed mcp.server.fastmcp)
```

`setup.py` is legacy and its version is not used. `pyproject.toml` is the
source of truth.

## Commands

```bash
uv sync --extra dev --frozen   # install, honouring uv.lock
uv run pytest -v               # what CI runs
uv run sonilo-mcp              # start the server on stdio
```

Use `uv sync --frozen`, not a plain `pip install`: an unpinned resolve can pull
`mcp` 2.x and every import fails. There is no PR CI in this repo; tests run in
`.github/workflows/release.yml` before publishing, so run them locally.

## Releasing

1. Bump `version` in `pyproject.toml`; put the version in the commit title,
   e.g. `proofread: ... (0.26.0)`.
2. Push a `vX.Y.Z` tag that matches it. The release workflow tests, publishes
   to PyPI (trusted publishing), waits until PyPI serves the version, then
   syncs `server.json` and publishes to the MCP Registry.

## Conventions for changes

- A new tool or parameter mirrors the public API's field names and limits. Add
  it to the Tools table in `README.md`, and update the
  [skills](https://github.com/sonilo-ai/skills) repo: its CI diffs this
  package's tool surface weekly and fails on drift.
- Duration caps are constants near the top of `api.py`
  (`_MAX_VIDEO_DURATION_SECONDS` and friends). Keep them equal to the backend.
- Map errors by the API's `code`, not by message text.
- File reads and writes stay inside `SONILO_MCP_BASE_PATH` unless
  `SONILO_MCP_ALLOW_ANY_PATH=true`. Do not add a path that skips this check.
- Never overwrite an existing output file; add a `-1`, `-2` suffix.
- Every request carries `X-Sonilo-Client: mcp`; keep it.

## Connecting the server

```bash
npm install -g sonilo-cli && sonilo login     # or: pip install sonilo-cli
claude mcp add sonilo -- uvx sonilo-mcp
# or with a key instead of a sign-in:
claude mcp add sonilo --env SONILO_API_KEY=sk-... -- uvx sonilo-mcp
```

The key comes from `SONILO_API_KEY` if set, otherwise from
`~/.config/sonilo/credentials.json` (written by `sonilo login`, 90-day key).
Create keys at https://platform.sonilo.com/dashboard/api-keys.

| Variable | Default | |
|---|---|---|
| `SONILO_API_KEY` | from `sonilo login` | Bearer token |
| `SONILO_API_URL` | `https://api.sonilo.com` | API base URL |
| `SONILO_MCP_BASE_PATH` | `~/Desktop` | Output directory and file access boundary |
| `SONILO_MCP_ALLOW_ANY_PATH` | `false` | Allow files outside the base path |
| `TIME_OUT_SECONDS` | `600` | Timeout for streaming music, uploads and downloads; task polls wait at least 2700 s (stems 4500 s, dubbing 7200 s) |

## Tools

| Tool | Endpoint | Video cap |
|---|---|---|
| `text_to_music` | `POST /v1/text-to-music` | |
| `video_to_music` | `POST /v1/video-to-music` | 360 s |
| `video_to_video_music` | `POST /v1/video-to-video-music` | 360 s |
| `text_to_sfx` | `POST /v1/text-to-sfx` | |
| `video_to_sfx`, `video_to_video_sfx` | `POST /v1/video-to-sfx`, `/v1/video-to-video-sfx` | 480 s |
| `video_to_sound`, `video_to_video_sound` | `POST /v1/video-to-sound`, `/v1/video-to-video-sound` | 480 s |
| `analyze_video` | `POST /v1/video-analysis` | 480 s |
| `audio_ducking` | `POST /v1/audio-ducking` | 360 s per input |
| `dubbing`, `proofread` | `POST /v1/dubbing`, `/v1/proofread` | 300 s |
| `get_sfx_task` | `GET /v1/tasks/{id}` | free |
| `get_account_services`, `get_usage` | `GET /v1/account/services`, `/v1/account/usage` | free |
| `play_audio` | local playback only | free |

Every tool except the last three rows is billed when the request is accepted.
A timeout on our side does not cancel or refund it. For task-based calls the
error carries the task id, and `get_sfx_task("<id>")` fetches the result (the
hosted server calls the same tool `get_generation_task`). If the MCP host's own
tool-call timeout cuts the call off first, the id is in the server's stderr log
as `[sonilo-mcp] task submitted: <id>`. Streaming music
calls (`text_to_music`, and `video_to_music` without `preserve_speech`) have
no task id; check `get_usage` to see whether one completed.

## Common errors

| Message | Cause | Fix |
|---|---|---|
| `SONILO_API_KEY not set and no stored credential found` | No key, no sign-in | `sonilo login`, or set `SONILO_API_KEY` |
| `Invalid SONILO_API_KEY` (401) | Wrong, revoked or expired key | New key, or `sonilo login` again |
| 402 with `trial_exhausted` | Free runs for that service are used up | Add a payment method at https://platform.sonilo.com/dashboard/billing |
| 402 `Insufficient balance ... Top up at ...` | Balance ran out | Top up at the billing page |
| `File too large` (413) | Over `max_upload_size_mb` from `get_account_services` | Compress or trim |
| 422 over a cap | Video longer than the table above | Trim; nothing was charged |
| `Rate limit exceeded` / `Too many concurrent generations` (429) | RPM or concurrency limit | Wait a full minute, or for a running task to finish |
| `Timed out ... waiting for task <id>` | Still running | `get_sfx_task("<id>")` |
| Path rejected outside the base path | File confinement | Move the file, change `SONILO_MCP_BASE_PATH`, or set `SONILO_MCP_ALLOW_ANY_PATH=true` |
