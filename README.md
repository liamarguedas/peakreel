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
| M7a | Stream status, duplicate detection, safety flags (`peakreel safety download`, `scan`, `compare`) | In review |
| M8 | Launch hardening: retention (`peakreel cleanup`), health warnings, `--detach`, runbook below | In review (8 h soak running) |
| M9 | Publisher: per-platform metadata, queue, export packages (`peakreel publish`) | In review |
| M10 | YouTube Shorts publishing (`peakreel youtube setup`, `login`, `status`) | In review |
| M11+ | Instagram adapter, TikTok inbox drafts, privacy pages (see [docs/platforms.md](docs/platforms.md)) | Planned |

## Quick start

Requirements: Windows 10/11 (Linux works too), [uv](https://docs.astral.sh/uv/), and ffmpeg/ffprobe.

```
uv sync
uv run peakreel doctor
```

`doctor` checks every external dependency and prints one pass or fail line per check.
See [docs/m0-setup.md](docs/m0-setup.md) for configuration details, [docs/m1-chat-ingest.md](docs/m1-chat-ingest.md) for recording chat, [docs/m2-detector.md](docs/m2-detector.md) for spike detection, [docs/m3-recorder.md](docs/m3-recorder.md) for video recording, [docs/m4-clipper.md](docs/m4-clipper.md) for the live clipper, [docs/m5-renderer.md](docs/m5-renderer.md) for vertical rendering, [docs/m6-captions.md](docs/m6-captions.md) for captions, [docs/m7-supervisor-review.md](docs/m7-supervisor-review.md) for running unattended and reviewing clips, [docs/m7a-safety-dedupe.md](docs/m7a-safety-dedupe.md) for duplicates and safety flags, [docs/m9-publisher.md](docs/m9-publisher.md) for publishing, and [docs/m10-youtube.md](docs/m10-youtube.md) for YouTube.

## Runbook

Commands are run from the repository folder.
`uv run peakreel` works too; `python -m peakreel` (inside the project environment, `.venv\Scripts\python -m peakreel`) is used here because a running `peakreel.exe` is locked by Windows and blocks `uv sync`.

### Before a session

```
uv run peakreel doctor                 # every check must pass (clock, ffmpeg, model, word lists, disk)
uv run peakreel cleanup --dry-run      # what retention would delete now
```

Channels to run are the enabled entries under `channels:` in `config.yaml` (or `config.local.yaml`), each with its `language`.

### Start and stop

```
.venv\Scripts\python -m peakreel run start --detach      # all enabled channels, in the background
.venv\Scripts\python -m peakreel review --detach         # review page on http://127.0.0.1:8765/
.venv\Scripts\python -m peakreel run stop                # graceful: finishes clips in flight (up to a minute)
```

`--detach` prints the process id and checks that the process is still running a few seconds later; a start-up failure (a channel already recorded by another process, a port in use) is reported at once.
Background processes log only to `data/logs/peakreel.jsonl` (rotated at 10 MB, 5 files kept), including any crash.
To stop the review page, end its process (`Stop-Process -Id <pid>`); it holds no state.

### Health

The review page's status bar shows each channel (state, video age, chat rate), memory, OS handles, free disk, and warnings in amber.
The same is in `data/run/status.json`, rewritten every 30 s:

| Warning | Meaning | Do |
| --- | --- | --- |
| `disk low` | Free disk below `retention.min_free_gb` (20 GB); unapproved clips are already being purged early, oldest first | Free space, or lower `retention.new_days` |
| `memory high`, `handles high` | Above `health.max_memory_mb` / `health.max_handles`; a steady climb is a leak | Note the time, restart (`run stop`, `run start --detach`), report it |
| `captions behind` | Whisper cannot keep up with clips | Fewer channels, or a smaller `captions.model` |
| `clips piling up` | Cutting or rendering is behind | Fewer channels |
| `<channel>: recorder restarting` | Its recorder crashed and restarts with backoff | Usually transient; check the log if it repeats |

A channel whose `video` age passes a minute is offline or stuck; offline channels are picked up again automatically when they go live.

### Disk

Retention runs every 30 minutes (`retention:` in `config.yaml`): rejected and taken-down clips go after 2 days, unreviewed clips after 7, export packages after 14, chat logs after 90; approved clips are never deleted.
The video buffer keeps only the last `record.buffer_minutes` per channel.
`uv run peakreel cleanup` applies the rules at once.

### A streamer asks for removal

Act on it immediately, in this order:

1. Add them to `blocklist.yaml` (with the date and reason).
   Within 30 s the running pipeline stops recording them, stops logging their chat, deletes their buffered video, blocks all their clips and deletes every export and package of those clips.
   The review page does the same the next time it lists clips, even if the pipeline is not running.
2. Delete what was already posted: `uv run peakreel publish status` lists every post by account and platform; packages exported for manual upload are already deleted, posts on the platforms must be removed by hand in each app.
3. Remove them from `channels:` in the config, so they are not started again.
4. Reply to them confirming the removal.

### After a crash or a restart

Nothing needs repairing by hand.
Locks are released by the operating system when a process dies.
A publication interrupted mid-way is resolved on the next start of the review page or `uv run peakreel publish run`: an export is written again; a platform upload with an unknown outcome is marked failed for you to check, never sent twice.

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
