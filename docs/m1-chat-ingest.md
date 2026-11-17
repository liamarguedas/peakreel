# M1: Twitch chat ingest

Peakreel records Twitch chat anonymously (a `justinfan` nickname, no token, read-only) and writes every message to JSONL.
These logs are the input of the M2 spike detector and its replay harness.

## Run it

```
uv run peakreel chat record -C xqc -C caseoh_ -C somestreamer
uv run peakreel chat record                     # uses `channels` from config.yaml
uv run peakreel chat record -C xqc -d 1800      # stop after 30 minutes
```

Stop with Ctrl+C (or Ctrl+Break, which is what a parent process sends with `CTRL_BREAK_EVENT`).
Both cancel the recorder cleanly: a `stopped` record is written for every channel and all files are closed.
A channel on `blocklist.yaml` is refused before anything connects.

One websocket carries every channel.
JOINs are sent in batches of 20 per 10.5 s to stay under Twitch's limit for anonymous connections.
A channel that is never confirmed within 15 s (usually a typo) is logged as a warning.

## Files

```
data/chat/<channel>/<YYYY-MM-DD>.jsonl     one file per channel per UTC day
data/logs/peakreel.jsonl                   application log (throughput every 60 s)
```

The day is the UTC date of each event's own timestamp, so file contents never depend on when the recorder ran.
Files are append-only and line-buffered, so a crash loses at most one line.
If a previous run was killed mid-line, the next write starts on a fresh line and replay skips the torn line with a warning.

## Record format

Messages:

```json
{"kind":"msg","ts":1794900538.824,"server_ts":1794900415.932,"channel":"xqc","user":"viewer1",
 "display_name":"Viewer1","user_id":"136857810","msg_id":"2bdd96e8-...","type":"privmsg",
 "text":"LETS GO","filtered":null,"tags":{"subscriber":"1","badges":"subscriber/18"}}
```

Connection events:

```json
{"kind":"conn","ts":1794900576.622,"channel":"xqc","event":"disconnected",
 "reason":"stalled: no pong within 3.0s","last_alive_ts":1794900573.1}
```

| Field | Meaning |
| --- | --- |
| `ts` | Local receive time, UTC epoch seconds. The alignment clock for video (M3). |
| `server_ts` | Twitch's `tmi-sent-ts`. Diagnostic only. |
| `type` | `privmsg` (chat line) or `usernotice` (raid, sub, gift; kind in `tags["msg-id"]`). |
| `filtered` | `null`, `"bot"` (sender in `chat.bots`) or `"command"` (starts with a `chat.command_prefixes` entry). |
| `event` | `joined`, `disconnected` (lost or judged dead) or `stopped` (shut down on purpose). |
| `last_alive_ts` | Last moment the connection was proven alive. Data may be missing from here to the next `joined`. |

Filtered messages are never dropped.
`peakreel chat replay --reclassify` re-applies the current filter config to old logs.

## Liveness and gaps

A gap between chat messages is not evidence of data loss: chat can simply be quiet.
The recorder proves liveness independently by sending a websocket ping every `chat.keepalive_interval_s` (2 s) and requiring a pong within `chat.keepalive_timeout_s` (3 s).
A missed pong closes the connection as stalled and reconnects with exponential backoff (0.5 s doubling, capped at 8 s, with jitter).
The server's `RECONNECT` command and closed sockets are handled the same way.
While connected, an outage is noticed within 5 s.

```
uv run peakreel chat gaps data/chat             # exits 1 on any failure
uv run peakreel chat gaps data/chat -t 10       # tolerate up to 10 s
```

For each channel it prints the recorded span, message count, sessions, every downtime with its reason, and the longest message gap (informational).
A downtime runs from `last_alive_ts` of a `disconnected` or `stopped` record to the next `joined`.
If a `joined` appears with no shutdown record before it, the recorder was killed, and the downtime is reported as an unclean stop starting at the last record.
Only unintentional downtimes longer than the threshold count as failures; `stopped` restarts are listed as intentional.

## Replay

```
uv run peakreel chat replay data/chat                    # every channel, merged by time
uv run peakreel chat replay data/chat/xqc --include-filtered
```

Replay is deterministic: within a channel events keep their recorded order, and channels are merged by `(ts, channel)`.
In code, `peakreel.chat.replay.read_log(paths)` yields the same `ChatMessage` objects; it is the input API for M2.

## Clock skew

`ts` comes from the local clock, so a skewed clock shifts every timestamp relative to Twitch.
`peakreel doctor` fails when the local clock differs from Twitch's by more than 2 s, and the 60 s throughput log line includes `median_receive_delay_s` (`ts - server_ts`), which should be well under a second.
Fix skew with `w32tm /resync` from an administrator shell, or enable "Set time automatically".
Video in M3 uses the same local clock, so chat-to-video alignment is self-consistent even when skewed, but VOD links and cross-machine comparisons are not.

## Acceptance (M1)

1. `uv run pytest`, `ruff check`, `ruff format --check`, `pyright` clean.
2. A 30 minute recording on 3 live channels; `peakreel chat gaps` reports no unintentional downtime over 5 s.
3. Network kill mid-run: the log shows `disconnected`, then `joined`, then messages resuming.
4. `peakreel chat replay` gives byte-identical output on two runs over the same files.
