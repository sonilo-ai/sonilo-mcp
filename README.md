# Sonilo MCP Server

<!-- mcp-name: io.github.sonilo-ai/sonilo-mcp -->

An MCP (Model Context Protocol) server that exposes [Sonilo](https://sonilo.com)'s licensed music and sound-effects API to MCP-compatible clients (Claude Code, Claude Desktop, Codex).

The flagship tool is **`video_to_music`**: hand it your finished video and it composes an original soundtrack matched to the cut — the music follows the pacing, emotion, and edits because the model saw them. Length matches the video automatically. Every track is licensed and safe for commercial use (terms apply). `text_to_music` is also available for fixed-length tracks with no video to match.

For sound design, **`video_to_sfx`** watches your video and generates matching sound effects, returned as a standalone audio file. `text_to_sfx` generates a standalone effect from a description.

**▶ [Example result](https://github.com/cindyxu1030/sonilo-video-to-music-cookbook/blob/main/assets/demo-trailer.mp4)** — an AI-generated trailer with its soundtrack composed by `video_to_music` from the assembled cut. For recipes covering any AI-video pipeline (stitch → grade → add music → mux), see the [Sonilo video-to-music cookbook](https://github.com/cindyxu1030/sonilo-video-to-music-cookbook).

## Pricing and getting started

- **Commercial use:** every track is licensed and safe for commercial use (terms apply).
- **Pricing:** API usage is billed per second of generated audio. Current rates: [sonilo.com](https://sonilo.com/?utm_source=github&utm_medium=oss&utm_campaign=sonilo-mcp).
- **Free credits at sign-up:** new accounts start with free credits — try `video_to_music` on your own cut before paying anything.

## Quickstart with Claude Code

```bash
claude mcp add sonilo --env SONILO_API_KEY=sks_... -- uvx sonilo-mcp
```

Get your API key from the [Sonilo dashboard](https://platform.sonilo.com/dashboard/api-keys), then start a session and ask, e.g. *"Make background music that matches this video: `~/Desktop/promo.mp4`."*

## Why Sonilo

- **Video-to-music** — give it a video and Sonilo composes a full-length score matched to its pacing, motion, and emotion. Transitions and beat drops align to your cut points, and the track matches the video's duration exactly — no prompts or manual syncing required.
- **Text-to-music** — generate tracks from a text description (genre, mood, tempo, instrumentation) at an exact duration (1–360s).
- **Video-to-SFX** — Sonilo watches the video and generates sound effects for what it sees. You get the SFX as a standalone audio file. Optional `segments` let you script effects to specific time ranges (`[{start, end, prompt}]`).
- **Text-to-SFX** — generate a standalone sound effect from a description (1–180s), in `wav`, `mp3`, `aac`, or `flac`.
- **Fully licensed, commercial-safe** — music licensed via Shutterstock; every generated track is cleared for commercial use on social, brand content, and advertising, with no Content ID worries.
- **Video-to-sound** — generate music **and** sound effects for the same clip in one call, mixed into a single balanced soundtrack. Get back the mixed audio, or a new video with it muxed in.
- **Pay as you go** — billed only for the seconds of music you generate. Self-serve accounts start with free runs on every endpoint, no card required: 2 each on text-to-music, text-to-sfx and audio-ducking, and 1 each on video-to-music, video-to-sfx, video-to-video-music, video-to-video-sfx, video-to-sound and video-to-video-sound. After that, calls bill at the normal rate.

### Audio Playback Dependencies

The `play_audio` tool requires PortAudio at runtime (for `sounddevice`). On macOS/Linux, install via:

- **macOS**: `brew install portaudio`
- **Debian/Ubuntu**: `sudo apt-get install libportaudio2`

`uvx sonilo-mcp` and `pip install` will pull the Python bindings, but the system PortAudio library must be installed separately. The other tools (`text_to_music`, `video_to_music`, `text_to_sfx`, `video_to_sfx`, `audio_ducking`, `get_sfx_task`, `get_account_services`, `get_usage`) work without PortAudio.

## Quickstart with Claude Desktop

1. **Get your API key** from the [Sonilo dashboard](https://platform.sonilo.com/dashboard/api-keys).

2. **Install the `uv` package manager** (provides `uvx`):

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

   See the [uv repo](https://github.com/astral-sh/uv) for other install methods.

3. **Go to Claude > Settings > Developer > Edit Config > claude_desktop_config.json to include the following:**

   ```json
   {
     "mcpServers": {
       "sonilo": {
         "command": "uvx",
         "args": ["sonilo-mcp"],
         "env": {
           "SONILO_API_KEY": "sks_...",
           "SONILO_API_URL": "https://api.sonilo.com",
           "TIME_OUT_SECONDS": "600"
         }
       }
     }
   }
   ```

4. **Restart Claude Desktop.** You should see the Sonilo tools available in the tool menu.

## Quickstart with Codex

1. **Get your API key** from the [Sonilo dashboard](https://platform.sonilo.com/dashboard/api-keys).

2. **Install the `uv` package manager** (provides `uvx`):

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

3. **Go to Codex > Settings > MCP servers to fill out the following:

![alt text](assets/CodexInstruction.png)


  Or you can add the server** to `~/.codex/config.toml`:

   ```toml
   [mcp_servers.sonilo]
   command = "uvx"
   args = ["sonilo-mcp"]

   [mcp_servers.sonilo.env]
   SONILO_API_KEY = "sk_..."
   SONILO_API_URL = "https://api.sonilo.com"
   TIME_OUT_SECONDS = "600"
   ```

4. **Restart Codex** (or start a new session), then run `/mcp` to confirm `sonilo` is connected and its tools are listed.

## Example usage

Once the server is connected, just ask your assistant in natural language. For example:

- *"Make background music that matches this video: `~/Desktop/promo.mp4`."*
- *"Compose music for `https://example.com/clip.mp4` with a calm, ambient style."*
- *"I stitched my AI-generated clips into `~/Desktop/trailer.mp4` — add a soundtrack that matches the cut."*
- *"Use Sonilo mcp to generate 30 seconds of upbeat lo-fi hip-hop for a study playlist and save it to my Desktop."*
- *"Use Sonilo to write an epic orchestral cinematic track, about 60 seconds long."*
- *"What Sonilo services and limits does my account have?"*
- *"Show my Sonilo usage for the last 7 days."*
- *"Play the track you just generated."*

The assistant will call the matching tool (`text_to_music`, `video_to_music`, `text_to_sfx`, `video_to_sfx`, `audio_ducking`, `get_sfx_task`, `get_account_services`, `get_usage`, or `play_audio`) and save generated audio to your configured output directory.

## Configuration

### Environment Variables

| Variable | Default | Description |
|---|---|---|
| `SONILO_API_KEY` | _(required)_ | Bearer token. |
| `SONILO_API_URL` | `https://api.sonilo.com` | Public API base URL. |
| `SONILO_MCP_BASE_PATH` | `~/Desktop` | Default output directory and base for relative input paths. Also the confinement boundary (see below). |
| `SONILO_MCP_ALLOW_ANY_PATH` | `false` | Set to `true` to let tools read/write files outside `SONILO_MCP_BASE_PATH`. |
| `TIME_OUT_SECONDS` | `600` | Generation timeout, in seconds. Aligned with the backend's read timeout. |

### File access & confinement

By default, the file tools (`video_to_music` input, `play_audio`, and any
`output_directory`) are **confined to `SONILO_MCP_BASE_PATH`**. Paths that
resolve outside it (after symlink resolution) are rejected. This limits the
blast radius if a client is tricked into reading or exfiltrating arbitrary
files. To opt out — e.g. to read a video from elsewhere on disk — set
`SONILO_MCP_ALLOW_ANY_PATH=true`.

## Tools

| Tool | Description | Cost |
|---|---|---|
| `text_to_music(prompt, duration, output_directory?)` | Generate music from a text prompt. | ✅ |
| `video_to_music(video_path? \| video_url?, prompt?, preserve_speech?, output_directory?)` | Generate music matched to a video. Max duration **360s (6 min)**; subject to the account's upload-size cap (typically 300 MB). `preserve_speech` (default `false`) keeps the source speech in the result — you also get a `vocals` speech stem and a ready-to-use mux; when set, the call runs the backend's async generation mode internally instead of streaming. | ✅ |
| `text_to_sfx(prompt, duration, audio_format?, output_directory?)` | Generate a sound effect from text. Duration 1–180s; formats wav/mp3/aac/flac (default aac). | ✅ |
| `video_to_sfx(video_path? \| video_url?, prompt?, segments?, audio_format?, output_directory?)` | Generate SFX for a video; saves the generated SFX audio. Max video duration **180s (3 min)**. | ✅ |
| `video_to_video_music(video_path? \| video_url?, prompt?, preserve_speech?, output_directory?)` | Generate music for a video and return a new `.mp4` with it muxed in. Max video duration **360s (6 min)**. | ✅ |
| `video_to_video_sfx(video_path? \| video_url?, prompt?, segments?, output_directory?)` | Generate SFX for a video and return a new `.mp4` with them muxed in. Max video duration **180s (3 min)**. | ✅ |
| `video_to_sound(video_path? \| video_url?, music_prompt?, sfx_prompt?, segments?, preserve_speech?, ducking?, output_directory?)` | Generate music **and** SFX for a video in one call and save the mixed audio track. One charge instead of two, with the two layers balanced by the backend. `ducking` (default `true`) dips the music under the source speech. Max video duration **180s (3 min)**. | ✅ |
| `video_to_video_sound(video_path? \| video_url?, music_prompt?, sfx_prompt?, segments?, preserve_speech?, ducking?, output_directory?)` | Same as `video_to_sound`, but returns a new `.mp4` with the mixed soundtrack muxed in. Max video duration **180s (3 min)**. | ✅ |
| `audio_ducking(voice_path? \| voice_url?, music_path? \| music_url?, output_directory?)` | Duck a music bed under a voice track. The voice input may be a video — the ducked mix is muxed back into a new `.mp4`. Each input max **360s (6 min)**; subject to the account's upload-size cap. | ✅ |
| `get_sfx_task(task_id, output_directory?)` | Check an SFX, audio-ducking, video-to-video, video-to-sound, or async video-to-music task and download its result — recovery for timed-out `text_to_sfx`, `video_to_sfx`, `audio_ducking`, `video_to_video_music`, `video_to_video_sfx`, `video_to_sound`, `video_to_video_sound`, and `video_to_music(preserve_speech=true)` calls. | ❌ |
| `get_account_services()` | List available services and limits. | ❌ |
| `get_usage(days=30)` | Show usage summary + per-day breakdown. | ❌ |
| `play_audio(input_file_path)` | Play a local audio file. | ❌ |

Tools marked ✅ make API calls that incur charges on your Sonilo account.

> **Optional:** if [`ffprobe`](https://ffmpeg.org/) (part of FFmpeg) is installed, `video_to_music` checks a video's duration locally and rejects anything over 360s before uploading. `video_to_sfx` performs the same local check with its 180s cap. `audio_ducking` does the same for both of its inputs against its 360s cap. Without it, the same limits are still enforced by the backend.

### Sound effects and ducking run as tasks

The music tools stream their result and finish in one call — with one exception: `video_to_music(preserve_speech=true)` submits a *task* and polls it internally instead, because keeping the source speech is only available in the backend's async mode. You still get the saved file paths back from a single call; you just don't see the task, and — like the SFX tools — it uses the same `get_sfx_task` recovery path if the call times out. The SFX tools submit a *task*, then poll it until it completes — `text_to_sfx` and `video_to_sfx` do this for you and return the saved file paths, so you normally never see the task. `audio_ducking` uses the same submit-then-poll flow and the same `get_sfx_task` recovery path.

If a call times out, the generation keeps running (and is already charged). The error message carries the task id, and `get_sfx_task("<id>")` retrieves the result once it's ready. The task id is also printed to stderr the moment a task is submitted, so it survives even a cancelled call. `get_sfx_task` is safe to call repeatedly: if the file is already on disk it reports that instead of downloading a second copy.

## Output Format

**Music** is saved as `.m4a` (AAC in MP4 container). File names use the title returned by the backend (slugified), or a `sonilo-<timestamp>.m4a` fallback. When multiple parallel streams are returned, a `-<index>` suffix is appended.

**`video_to_music(preserve_speech=true)`** saves up to three kinds of file, each labeled in the returned text: the generated music audio (same naming as above, based on `prompt` or falling back to `music-<first 8 chars of the task id>`), the preserved speech stem as `<base>-vocals.<ext>`, and the mux — speech and music already mixed together, the ready-to-use combined result — as `<base>-mux.<ext>` (or `<base>-mux-<index>.<ext>` for multiple streams). The speech stem and mux extensions come from the backend's reported `content_type` (typically `.m4a`).

**Sound effects** are saved in the requested `audio_format` — `wav`, `mp3`, `flac`, or `aac` (the default, written as `.m4a`); `video_to_sfx` saves audio only, not the source video.

**Combined sound** (`video_to_sound` / `video_to_video_sound`) is saved as a single file: a `.wav` for `video_to_sound`, a `.mp4` for `video_to_video_sound`. The name comes from `music_prompt`, falling back to `sfx_prompt` and then to `sound-<first 8 chars of the task id>` / `v2v-sound-<first 8 chars of the task id>`. The separate music and SFX stems stay on the backend — only the mixed result is downloaded.

File names come from the prompt (slugified, truncated to 80 characters). When there is no prompt to name a file after — `video_to_sfx` without one, or an SFX/ducking file recovered via `get_sfx_task` — the name is `sfx-<first 8 chars of the task id>` instead. A `video_to_music(preserve_speech=true)` task recovered via `get_sfx_task` is named `music-<first 8 chars of the task id>` instead (`get_sfx_task` detects the music envelope shape and saves audio/vocals/mux the same way as a direct `video_to_music` call). Existing files are never overwritten: a `-1`, `-2`, … suffix is added instead.

**Ducking** results are saved as a single file: a `.wav`, or a `.mp4` when the voice input was a video (the ducked mix is muxed back into it). The file name is the voice input's name plus `-ducked` (e.g. `interview.mp4` → `interview-ducked.mp4`), falling back to `ducked-<first 8 chars of the task id>` when there is no usable name. A ducking result recovered via `get_sfx_task` is named `sfx-<first 8 chars of the task id>` instead, since that tool has no voice file name to work from.

## Common Errors

| Message | What to do |
|---|---|
| `Invalid SONILO_API_KEY` | Verify the key at <https://platform.sonilo.com/dashboard/api-keys>. |
| `Insufficient minutes` / `Credit limit exceeded` | Top up at <https://platform.sonilo.com/dashboard/billing>. |
| `Rate limit exceeded` | Check `get_account_services` for your rpm/concurrency limits. |
| `Generation timed out` (music, `text_to_music`/`video_to_music` without `preserve_speech`) | Raise `TIME_OUT_SECONDS`. Check `get_usage` to confirm whether the backend completed and charged. |
| `Timed out … waiting for task <id>` (SFX, ducking, or `video_to_music(preserve_speech=true)`) | The generation is still running. Call `get_sfx_task("<id>")` to retrieve the result — nothing is lost, including for a timed-out `preserve_speech` music task (`get_sfx_task` recognizes its result shape and saves audio/vocals/mux the same way `video_to_music` itself would). |
| `Task not found` | The task id doesn't exist, or belongs to a purely streaming generation (`text_to_music`, or `video_to_music` without `preserve_speech`), which isn't pollable. Check the id. |
