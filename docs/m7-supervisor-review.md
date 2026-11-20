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
Its **Pause recording** button does `run stop` (clips in flight are finished) and **Start recording** does `run start --detach`; posting, stats and auto review run in the review app, so the queue keeps posting while recording is off.
A status file older than 2 minutes counts as not running (a killed pipeline leaves `running: true` behind).

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

The page lists clips (tabs: To review, Check AI, Schedule, Posted, Approved, Rejected, Stats, Activity).
**Schedule** (`GET /api/schedule`) shows the pipeline as stages (clips made, AI checking, waiting for you, approved, posts queued, posts live), every queued post soonest first with when it is tried next and why it waits (a quota, a Meta block), and the posts of the last 24 hours.
Each card shows only what a decision needs:

- the vertical render (seekable), with its thumbnail as poster;
- the channel, the time (Brasília) and the length;
- what chat said most, each message with its count;
- safety flags (personal information, music), when there are any;
- two big buttons, **Approve** and **Reject**; Reject asks for the reason in their place.

Title, description, platforms and language are not on the card: Gemini writes the text after approval, the clip's language picks the account and `publish.platforms` the platforms (2026-11-19: the reviewer never changed them).
The API still accepts them (`POST /api/clips/{id}`).

**To review** holds only the clips auto review left for a person when it runs in `on` mode; the clips it is still checking are counted above the list, and shown anyway after `ai_wait_min` + 15 minutes, so a stalled auto review never hides a clip for good.
The tab shows how many clips wait.
Duplicates and All are no longer tabs (`/api/clips?state=duplicate` and `/api/clips` still list them).

It refreshes every 20 s without closing a card's reasons while they are open.

**Posted** lists clips on at least one platform (live, private or still processing), with a link and the views of each post.

### Keyboard shortcuts

Shortcuts act on the highlighted clip (click a card, or press `j`) and are off while typing in a text field.
Press `?` on the page for the list.

| Key | Action |
| --- | --- |
| `j` / `↓`, `k` / `↑` | Next, previous clip |
| `Space` | Play or pause (other videos pause) |
| `←` / `→` | Seek 3 s |
| `a` | Approve |
| `r`, then `1` to `6` | Reject, with the reason (`Esc` cancels) |
| `1` to `7` | Switch tab |

After approving or rejecting, the highlight moves to the clip that takes its place, so `a` or `r` then `Space` reviews the queue without the mouse.
A clip approved by mistake can be rejected until it is on a platform; rejecting cancels its pending publications.

### After approving

Approving queues the clip's posts (`docs/m9-publisher.md`).
Until 2026-11-19 it also copied the clip into `data/export/` for posting by hand; every post now goes through the platforms' APIs, so the copy was dropped (it doubled the disk used by approved clips).
A day after a clip's last post finished, its video files are deleted (`retention.posted_days`); its card keeps the post links and views.
If none of its posts went up (all failed or dropped as stale), the files are deleted `retention.rejected_days` after the last one, which leaves time to queue it again.
Clips in state `blocked` (takedowns) cannot be changed.

### Stats

The Stats tab (`/#stats`, data from `GET /api/dashboard`, `peakreel/review/dashboard.py`) shows how the pipeline is doing, read from the database on every refresh:

- clips made, approved and rejected, each split by who decided (auto review or a person);
- posts live, waiting and failed, and their views and likes, per language and platform;
- posts that went live per hour over the last 48 hours, in Brasília time;
- the review numbers: auto-rejected clips a person re-checked and how many were wrong (the Check tab), how well Gemini's score predicts 12 h views, and the reviewer's reject reasons;
- the 10 most viewed posts and the 10 channels with the most approved clips.

Views are each post's latest reading (12, 24 or 72 hours after posting, `peakreel/publish/stats.py`).
A post not read yet shows "–", never 0; Instagram and Facebook views stay "–" until `peakreel meta login --insights` grants the insights permission.

### Approving from a phone

The page needs no login while only this PC can reach it.
Once it can be reached from other devices it requires a password, and it refuses to start without one.
The recommended setup is Tailscale: the page stays bound to `127.0.0.1`, and only your own devices can reach it, over HTTPS.

1. Install Tailscale on the PC and the phone, and sign in to both with the same account.
2. Set a password (at least 12 characters):
   ```
   uv run peakreel review password
   ```
3. Serve the page to your tailnet:
   ```
   tailscale serve --bg 8765
   ```
   It prints the address, for example `https://pc.tail1234.ts.net/`.
4. Allow that name in `config.local.yaml`:
   ```yaml
   review:
     public_hosts: [pc.tail1234.ts.net]
   ```
5. Start the page (`uv run peakreel review --detach`), open the address on the phone, and log in.

A login lasts `review.session_days` (30) on that device.
Running `peakreel review password` again logs every device out within 10 seconds; `--clear` removes the password.
After 5 wrong passwords in 15 minutes, logins are refused for the rest of that window.

### Security

- **Login**: required whenever `review.host` (or `--host`) is not loopback or `review.public_hosts` is set.
  Only an scrypt hash of the password is stored, in the Windows Credential Manager.
  The session cookie is signed (HMAC-SHA256), `HttpOnly` and `SameSite=Strict`, and `Secure` over HTTPS.
  Logging out removes the cookie from that browser; a copied cookie stays valid until it expires or the password is changed.
- **Cross-site requests**: any web page you visit can make your browser send requests to the page.
  Every change requires a JSON body, which a browser cannot send to another origin without a CORS preflight that this server never grants.
- **DNS rebinding**: requests whose `Host` is not `127.0.0.1`, `localhost`, `review.host` or one of `review.public_hosts` are rejected.
- Media files are served only from inside `paths.data_dir`.
- Chat text in the page is inserted as text, never as HTML (chat is attacker-controlled).

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
