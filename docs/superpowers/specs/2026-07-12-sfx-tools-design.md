# SFX Tools Design — sonilo-mcp

**Date:** 2026-07-12
**Branch:** `feat/sfx-tools`
**Status:** Approved

## Goal

Expose the backend's three new sound-effects APIs as MCP tools:

| Backend endpoint | Method | Pattern |
|---|---|---|
| `/v1/text-to-sfx` | POST (form) | Async: 202 + `task_id` |
| `/v1/video-to-sfx` | POST (multipart) | Async: 202 + `task_id` |
| `/v1/tasks/{task_id}` | GET | Poll: `processing` / `succeeded` / `failed` |

Unlike the existing music endpoints (streaming NDJSON, consumed in one call),
SFX is task-based: submit → poll → download presigned R2 URLs. This is a new
pattern for the MCP.

## Decisions (confirmed with user)

1. **Tool shape: blocking + recovery tool.** `text_to_sfx` / `video_to_sfx`
   submit, poll internally until terminal, download artifacts, and return
   local file paths — same UX as the existing music tools. A third tool,
   `get_sfx_task`, does a one-shot status check so a timed-out client can
   recover its result. Three tools total.
2. **`video_to_sfx` downloads both artifacts** — the SFX audio file and the
   finished video (SFX mixed in, `.mp4`). Two paths returned.
3. **`segments` is exposed as a pass-through.** The MCP does not reimplement
   the backend's strict validation (first start == 0, contiguous, prompts
   non-empty, ≤ 30 segments, within video duration); the backend rejects bad
   segments with a 400 *before charging*. The rules are documented in the
   tool description so AI clients can construct valid input.

## Architecture

All code stays in `src/sonilo_mcp/api.py` (single-file convention, ~250 new
lines). New async-task plumbing sits alongside the existing streaming
plumbing (`_post_streaming_generation`).

### New internal helpers

**`_post_task_submit(path, data=None, files=None) -> str`**
POST a form/multipart request, expect 202 with `{"task_id": ...}`, return the
task id. **No retry** — generation endpoints charge; same policy as
`_post_streaming_generation`. Uses `cfg["timeout"]` for the request timeout
(video uploads can be large). Errors map through `_raise_http_error`.

**`_poll_task(task_id, deadline) -> dict`**
Loop `GET /v1/tasks/{task_id}` via `_http_get_json` (which already retries
once on 5xx — safe, GETs are idempotent) every **5 seconds** until the public
status is `succeeded` or `failed`, or `time.monotonic()` passes `deadline`.
The overall budget is `cfg["timeout"]` (default 600 s, matching the music
tools and the backend's generation read timeout). On timeout, raise with the
task_id embedded and instructions to recover via `get_sfx_task`:

> `Timed out after {N}s waiting for task {task_id}. The generation may still
> complete on the backend — call get_sfx_task("{task_id}") later to retrieve
> the result.`

Returns the terminal task body (the `GET /v1/tasks/{id}` JSON).

**`_download_artifact(url, dest_path) -> None`**
Stream-download a presigned R2 URL to a local file. **Sends no Authorization
header and no client-attribution headers** — presigned URLs carry their own
auth, and the API key must not be sent to the storage domain. Timeout:
`cfg["timeout"]`. Raises a clear exception on non-2xx or network failure
(the task result still exists; `get_sfx_task` can re-download).

### Result handling (shared by blocking tools and `get_sfx_task`)

A `succeeded` task body contains an envelope:

```json
{
  "task_id": "...", "type": "...", "status": "succeeded",
  "audio": {"url": "...", "content_type": "...", "file_size": ...},
  "video": {"url": "...", "content_type": "video/mp4", "file_size": ...}
}
```

(`video` present only for `video_to_sfx` tasks; `cost` may appear for
whitelisted accounts and is ignored by the MCP.)

For each artifact entry present, download to the output directory and return
one `TextContent` per saved file (`Success. File saved as: {path}` — same
shape as the music tools).

**File naming:** base name is `_slugify(prompt)` when a prompt was given,
else `sfx-{task_id[:8]}`. Audio extension follows the requested
`audio_format` via the backend's mapping — `wav→.wav`, `mp3→.mp3`,
`aac→.m4a` (default), `flac→.flac`. Video is always `.mp4`. If both an audio
and video file would collide, the video gets the same base name (extensions
differ, so no collision). If a file already exists at the destination,
append the first free numeric suffix (`-1`, `-2`, …) rather than overwrite.

`get_sfx_task` has no prompt available, so it always names files
`sfx-{task_id[:8]}.{ext}`; the audio extension is derived from the
envelope's `content_type` (fall back to `.m4a` if unrecognized).

## Tools

### `text_to_sfx(prompt, duration, audio_format=None, output_directory=None)`

Submit → poll → download audio → return path.

- `prompt` (str): description of the sound effect, 1–2000 chars.
- `duration` (int): seconds, 1–180.
- `audio_format` (str, optional): one of `wav`, `mp3`, `aac`, `flac`.
  Default (omitted) yields AAC (`.m4a`).
- `output_directory` (str, optional): same semantics as existing tools
  (absolute, or relative to `SONILO_MCP_BASE_PATH`; defaults to base path).

Validation is delegated to the backend (rejects before charging). Tool
description carries the parameter ranges and the standard ⚠️ COST WARNING
block used by the music tools.

### `video_to_sfx(video_path=None, video_url=None, prompt=None, segments=None, audio_format=None, output_directory=None)`

Submit (file upload or URL) → poll → download **audio + finished video** →
return both paths.

- Exactly one of `video_path` / `video_url` (same rule and error message as
  `video_to_music`).
- `video_path`: reuses `_resolve_input_file` (extension allowlist,
  BASE_PATH confinement) and the account `max_upload_size_mb` pre-check.
- `video_url`: scheme restricted to http/https before it reaches ffprobe or
  the backend (same SSRF/flag-injection guard as `video_to_music`).
- **Local ffprobe pre-check:** `_check_video_duration` gains a
  `max_seconds` parameter. `video_to_music` passes 360 (unchanged
  behavior); `video_to_sfx` passes **180** — the backend's
  `SFX_MAX_VIDEO_DURATION_SECONDS`. Still best-effort/fail-open.
- `prompt` (str, optional): overall SFX description, ≤ 2000 chars.
- `segments` (list of `{start, end, prompt}` objects, optional):
  per-segment SFX descriptions. Serialized with `json.dumps` into the
  `segments` form field, no local validation. Tool description documents
  the backend's rules: first `start` must be 0; segments contiguous (each
  `end` == next `start`); every `end` > `start`; every segment `prompt`
  non-empty (≤ 200 chars); last `end` ≤ video duration; ≤ 30 segments.
- `audio_format`, `output_directory`: as above.

### `get_sfx_task(task_id, output_directory=None)`

One-shot status check (no polling). Recovery path for timed-out blocking
calls — the tool description says exactly that.

- `processing` → return a text message: still processing, try again later.
- `succeeded` → download artifact(s) to the output directory, return paths.
- `failed` → raise with the backend's error code and message, plus refund
  status: if `refunded` is true, state that the charge was reversed; if
  false, state that it was not (or not yet).

## Error handling

- HTTP errors route through the existing `_raise_http_error` (401/402/413/
  422/429 already have clear user-facing messages).
- Task `failed` → exception `Generation failed ({code}): {message}.` plus
  the refund line above.
- Poll timeout → exception with task_id + `get_sfx_task` recovery hint (see
  `_poll_task`).
- Download failure after a succeeded task → exception noting the result is
  safe and retrievable via `get_sfx_task`.

## Testing

Follow the existing `tests/test_api.py` monkeypatch style (httpx transport /
function patching). Cover:

1. `text_to_sfx` happy path: 202 → poll (processing → succeeded) → audio
   file written with correct name/extension.
2. `video_to_sfx` happy path with local file: upload form contains the
   video; both audio and video artifacts downloaded.
3. `segments` pass-through: list serialized to the exact JSON the backend
   expects.
4. Poll timeout: exception contains the task_id and mentions
   `get_sfx_task`.
5. Failed task: exception contains code, message, and refund status (both
   refunded true/false variants).
6. `_download_artifact` sends no `Authorization` header.
7. ffprobe pre-check: 200 s video rejected for SFX (180 cap) but accepted
   for music (360 cap).
8. `get_sfx_task`: all three status branches (processing message,
   succeeded download, failed raise).
9. `audio_format` validation is backend-delegated: an invalid format's 400
   surfaces cleanly.

## Out of scope

- Splitting `api.py` into modules (defer until it becomes painful).
- Exposing music tasks via `get_sfx_task` (backend 404s non-SFX task types).
- Cost display (admin-gated per-account backend feature; MCP ignores the
  field).
- Retry/resume of interrupted downloads.
