# Peakreel

Peakreel watches live Twitch streams, detects moments from chat spikes, and produces captioned vertical clips (1080x1920) for TikTok, Instagram Reels and YouTube Shorts.
Clips are reviewed manually before posting.

The full build plan lives in [plan/MILESTONES.md](plan/MILESTONES.md).

## Status

| Milestone | Scope | State |
| --- | --- | --- |
| M0 | Scaffold, config, SQLite schema, CLI, `doctor` | Done |
| M1 | Twitch chat ingest (`peakreel chat record`, `replay`, `gaps`, `stop`) | Done |
| M2 | Spike detector (`peakreel detect replay`, `label-sheet`, `evaluate`) | Awaiting labels |
| M3 | Stream recorder and rolling buffer (`peakreel record start`, `status`, `cut`, `stop`) | In review |
| M4 | Live clipper (`peakreel run start`, `stop`) | In review |
| M5 | Vertical renderer (`peakreel render`, automatic in `run`) | In review |
| M6 | Captions (`peakreel captions download`, `make`; automatic in `run` and `render`) | In review |
| M7 | Supervisor (`run start`), review tool (`peakreel review`), export, notifications | In review |
| M7a+ | Go-live detection, dedupe, safety checks, publishing | Planned |

## Quick start

Requirements: Windows 10/11 (Linux works too), [uv](https://docs.astral.sh/uv/), and ffmpeg/ffprobe.

```
uv sync
uv run peakreel doctor
```

`doctor` checks every external dependency and prints one pass or fail line per check.
See [docs/m0-setup.md](docs/m0-setup.md) for configuration details, [docs/m1-chat-ingest.md](docs/m1-chat-ingest.md) for recording chat, [docs/m2-detector.md](docs/m2-detector.md) for spike detection, [docs/m3-recorder.md](docs/m3-recorder.md) for video recording, [docs/m4-clipper.md](docs/m4-clipper.md) for the live clipper, [docs/m5-renderer.md](docs/m5-renderer.md) for vertical rendering, [docs/m6-captions.md](docs/m6-captions.md) for captions and [docs/m7-supervisor-review.md](docs/m7-supervisor-review.md) for running unattended and reviewing clips.

## Configuration

- `config.yaml` is committed and holds portable defaults.
- `config.local.yaml` is gitignored and is deep-merged on top of `config.yaml`.
  Put machine-specific values there, such as an absolute ffmpeg path or a different data directory.
- `blocklist.yaml` is the takedown list.
  It is checked before any clip is written or kept.

## Development

```
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

## Open item: rights and permission

Every clip carries a credit overlay and a "Clip from twitch.tv/<name>" line, and every channel has a `credit` string in config.
A credit does not replace permission.
Whether and how to obtain permission from each streamer is an open decision that must be settled before public posting.
When a streamer asks for removal, add them to `blocklist.yaml` first, then remove any published clips.
