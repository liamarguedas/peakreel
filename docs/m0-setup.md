# M0: Setup and environment

This note covers installing Peakreel on a development machine and verifying the environment with `peakreel doctor`.

## Install

1. Install [uv](https://docs.astral.sh/uv/).
2. Install ffmpeg (which includes ffprobe), for example with `scoop install ffmpeg`.
3. From the repository root, run `uv sync`.

`uv sync` creates `.venv`, installs Python 3.12 if needed, and installs all dependencies from `uv.lock`.
Streamlink is a locked project dependency, so it is installed into `.venv` by the same command.

## Verify

```
uv run peakreel doctor
```

Expected output is one line per check:

```
[PASS] config      D:\Peakreel\config.yaml
[PASS] python      3.12.12 (...)
[PASS] clock       local clock is 0.3s ahead of https://api.twitch.tv
[PASS] ffmpeg      ffmpeg version ... (C:\...\ffmpeg.exe)
[PASS] ffprobe     ffprobe version ... (C:\...\ffprobe.exe)
[PASS] streamlink  streamlink 8.6.3 (D:\Peakreel\.venv\Scripts\streamlink.exe)
[PASS] data dir    writable (D:\Peakreel\data)
[PASS] database    schema v1 (D:\Peakreel\data\peakreel.db)
[PASS] blocklist   0 blocked channel(s) (D:\Peakreel\blocklist.yaml)
```

The command exits with code 1 if any check fails, and each failing line says what is wrong and how to fix it.
The clock check needs the internet; `peakreel doctor --offline` skips it.
`doctor` creates the data directory and the SQLite database if they do not exist.

## Configuration files

| File | Committed | Purpose |
| --- | --- | --- |
| `config.yaml` | Yes | Portable defaults for every setting. |
| `config.local.yaml` | No (gitignored) | Machine-specific overrides, deep-merged on top of `config.yaml`. |
| `blocklist.yaml` | Yes | Takedown list. |

Merge rules for `config.local.yaml`: mappings merge key by key, lists and scalars replace the value in `config.yaml`.
Unknown keys are rejected, so a typo fails loudly instead of being ignored.
Relative paths resolve against the directory containing `config.yaml`, not the current working directory.

### Tool paths

`tools.ffmpeg`, `tools.ffprobe` and `tools.streamlink` accept either a path or a bare command name.
A bare name is looked up next to the Python interpreter first (the project virtualenv), then on `PATH`.
`doctor` prints the resolved path, so there is never doubt about which binary runs.
To pin exact binaries on a machine, create `config.local.yaml`:

```yaml
tools:
  ffmpeg: C:/Users/you/scoop/apps/ffmpeg/current/bin/ffmpeg.exe
  ffprobe: C:/Users/you/scoop/apps/ffmpeg/current/bin/ffprobe.exe
```

### Channels

```yaml
channels:
  - name: somestreamer
    credit: "Clip from twitch.tv/somestreamer"
```

`credit` is optional and defaults to `Clip from twitch.tv/<name>`.
`doctor` fails if a configured channel is also on the blocklist.

## Data layout

Everything Peakreel writes at runtime lives under `paths.data_dir` (default `data/`, gitignored):

| Path | Contents |
| --- | --- |
| `data/peakreel.db` | SQLite database: `channels`, `chat_events`, `spikes`, `clips`. |
| `data/chat/<channel>/<YYYY-MM-DD>.jsonl` | Raw chat logs (M1). |
| `data/logs/peakreel.jsonl` | Structured application log, rotated at 10 MiB, 5 backups. |

All timestamps in data files and the database are UTC wall-clock epoch seconds.

## Database

The schema is versioned with `PRAGMA user_version`.
`uv run peakreel db init` creates the database or upgrades it, and is safe to run repeatedly.
To change the schema, append a new entry to `MIGRATIONS` in `peakreel/db.py`; never edit an existing entry.

## Quality checks

```
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

The same four commands run in GitHub Actions on `windows-latest` for every push to `main` and every pull request.
