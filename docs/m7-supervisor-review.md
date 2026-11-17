# M7: Supervisor and review tool

M7 turns the live pipeline into something that runs unattended on many channels, and adds the human step: a local review page where clips are approved, annotated and exported.

## Channels: the allowlist

`peakreel run start` only runs channels listed under `channels:` in the config with `enabled: true` (the default).
Passing an unlisted or disabled channel fails, so a typo cannot start recording an unintended streamer.

```yaml
channels:
  - name: kato_junichi0817
    credit: "加藤純一 / twitch.tv/kato_junichi0817"
  - name: gaules
  - name: summit1g
    enabled: false        # configured, not run
```

Set `supervisor.require_allowlist: false` to allow any channel on the command line.

## Running

```
uv run peakreel run start                 # every enabled channel
uv run peakreel run start -C gaules       # a subset (must still be on the allowlist)
uv run peakreel run stop                  # graceful: finishes clips in flight first
```

For long runs start it detached through `python -m peakreel` (see the M1 guide, "Long runs").

## Supervision

One process supervises every channel, and each channel is isolated from the others:

| Failure | What happens |
| --- | --- |
| A channel's spike detector raises | Logged, that detector is rebuilt, other channels are untouched (`detector_restarts`). |
| A channel's recorder thread dies | The watchdog restarts it after 5 s, doubling up to 300 s (`supervisor.restart_initial_s`, `restart_max_s`; `restarts`). |
| A clip fails to cut, caption or render | Logged; the clip falls back (M4 to M6 behaviour); nothing else is affected. |
| The shared chat connection drops | Reconnects with backoff (M1); every channel's downtime is recorded in its log. |

### Takedowns while running

`blocklist.yaml` is re-read every `supervisor.blocklist_check_s` (30 s).
When a running channel appears on it, the supervisor immediately:

1. stops that channel's recorder,
2. stops logging its chat,
3. deletes its buffered video,
4. marks every clip it already cut as `blocked` and deletes their exports,
5. reports it as `blocked` in the status file.

No new clip of it can be made either: the clipper re-checks the blocklist before and after every cut (M4).
The review tool re-reads the blocklist too, so takedowns also apply to old clips when no pipeline is running: listing clips blocks them, and approving one is refused.
If `blocklist.yaml` becomes unreadable, clips fail closed (none are kept) and the review tool refuses approvals, while recording keeps its current state rather than stopping every channel over a typo; the error is logged every check.

### One owner per channel

Each channel's chat log and buffer has exactly one writer.
`run start` and `chat record` both take an operating-system lock per channel (`data/run/locks/<channel>.lock`) and refuse to start if another process holds it:

```
#gaules is already recorded by process 120724; stop it first (`peakreel chat stop` / `peakreel run stop`)
```

The OS releases the lock when the process exits for any reason, including a crash, so a lock can never go stale.

### Status

`data/run/status.json` is rewritten every `supervisor.status_interval_s` (30 s):

```json
{"running": true, "pid": 120724, "started_utc": "...", "updated_utc": "...",
 "clips_in_flight": 0, "captions_backlog": 0,
 "channels": {"gaules": {"state": "running", "video_age_s": 6.2, "chat_per_min": 310,
                         "restarts": 0, "detector_restarts": 0, "clips": 2}}}
```

`state` is `running`, `restarting` (recorder died, restart pending), `blocked` (takedown) or, after shutdown, `stopped`.
`video_age_s` is how old the newest buffered video is; above a minute means the channel is offline or recording is stuck.
`chat_per_min` is measured since the previous write.
The review page shows this as a status bar.

## Notifications

When a clip is ready the supervisor sends:

- a Windows desktop notification (`notify.desktop`, on by default); clicking it opens the review page;
- optionally a phone push through [ntfy](https://ntfy.sh): set `notify.ntfy_topic` and subscribe to the topic in the ntfy app.
  On the public server anyone who knows the topic can read it, so use a long random name, or self-host and set `notify.ntfy_server`.

Notifications are best effort; a failure is logged and never affects the clip.

## Review tool

```
uv run peakreel review            # http://127.0.0.1:8765/
```

The page lists clips (tabs: New, Duplicates, Approved, Published, Rejected, All) with:

- the vertical render (seekable), with its thumbnail as poster;
- time, length, caption status, score, chat peak and chatters, and the top chat messages;
- editable title and description; the title placeholder suggests the most repeated chat message, skipping messages with links or `@mentions`, and is used on export when the title is left empty;
- **Approve**, **Reject** and **Save** buttons.

It refreshes every 20 s without clobbering a card you are editing.

**Published** lists clips on at least one platform (live, private or still processing), with a link to each post.
Clips only exported for manual upload are not listed there.

### Keyboard shortcuts

Shortcuts act on the highlighted clip (click a card, or press `j`) and are off while typing in a text field.
Press `?` on the page for the list.

| Key | Action |
| --- | --- |
| `j` / `↓`, `k` / `↑` | Next, previous clip |
| `Space` | Play or pause (other videos pause) |
| `←` / `→` | Seek 3 s |
| `a`, `r`, `s` | Approve, reject, save |
| `e` | Edit the title (`Esc` leaves the field) |
| `1` to `6` | Switch tab |

After approving or rejecting, the highlight moves to the clip that takes its place, so `a` or `r` then `Space` reviews the queue without the mouse.
A clip approved by mistake can be rejected until it is on a platform; rejecting cancels its pending publications.

### Export

Approving exports the clip; changing it back to new or rejected removes the export.
Approving again (button "Re-export") rewrites it with the current title and description.

```
data/export/<YYYY-MM-DD>/<clip>/clip.mp4         vertical render (horizontal if none)
                               thumbnail.jpg
                               captions.ass      when captioned
                               metadata.json     title, description, channel, credit, source, score, chat
                               caption.txt       title, description and credit line, ready to paste
```

Exports are written to a temporary folder and renamed, so a half-written export is never visible.
Clips in state `blocked` (takedowns) cannot be changed.

### Security

The review tool has no login.

- It binds to `127.0.0.1` only. `--host 0.0.0.0` (for example to review from a phone on the LAN) prints a warning: anyone who can reach the port can approve and export clips.
- **Cross-site requests**: any web page you visit can make your browser send requests to localhost. Every change requires a JSON body, which a browser cannot send to another origin without a CORS preflight that this server never grants.
- **DNS rebinding**: requests whose `Host` is not `127.0.0.1`/`localhost` are rejected.
- Media files are served only from inside `paths.data_dir`.
- Chat text in the page is inserted as text, never as HTML (chat is attacker-controlled).

Before exposing it beyond this machine, add authentication; that is out of scope for M7.

## Configuration

```yaml
supervisor:
  require_allowlist: true
  restart_initial_s: 5
  restart_max_s: 300
  blocklist_check_s: 30
  status_interval_s: 30
review:
  host: 127.0.0.1
  port: 8765
  export_dir: export
notify:
  desktop: true
  ntfy_server: https://ntfy.sh
  ntfy_topic: null
```
