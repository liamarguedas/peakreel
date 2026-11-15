# Peakreel: Build Plan for Claude Code
Project name is Peakreel: Python package `peakreel`, CLI command `peakreel`.

## Goal
Watch live Twitch streams, detect moments from chat spikes, and produce captioned vertical clips (1080x1920) for TikTok, Instagram Reels and YouTube Shorts, with credits, tags and per-platform upload handling. Clips are reviewed manually before posting. First targets: GTA 6 launch streamers on Twitch.

## Ground rules for every milestone
- Python 3.12, asyncio, `pathlib` everywhere, `pytest`, `ruff`. Native Windows development, so no shell-specific code: call ffmpeg and streamlink with `subprocess` argument lists, never `shell=True`.
- Paths to `ffmpeg`, `streamlink` and the whisper model directory come from `config.yaml`, not PATH assumptions.
- All times are UTC wall-clock epoch seconds, stored with every chat message and every recorded segment. Alignment between chat and video depends on this.
- Every milestone ends with its acceptance checks passing and a short `docs/` note on how to run it.
- Windows gotchas to handle: ffmpeg `subtitles=` filter paths (run with `cwd` set and use relative paths, since `C:\` breaks the filter syntax), libass font location (pass `fontsdir`), graceful Ctrl+C shutdown of child processes.
- Rights handling: credit overlay on every clip, a `blocklist.yaml` takedown list checked before any clip is written or kept, and a per-channel `credit` string in config. A credit does not replace permission; keep this as an open item in the README.
- Platform specs (length caps, safe zones) change. Keep them in `config.yaml` under `output:` so they can be edited without code changes. Defaults: 1080x1920, 30 fps, H.264 yuv420p, AAC, clips 20 to 45 s, captions and credit kept out of the bottom 20% and right 12% of the frame.

## Repo layout
```
peakreel/
  config.py  db.py  cli.py
  chat/ twitch.py  base.py
  detect/ spikes.py  filters.py
  record/ recorder.py  buffer.py
  render/ cut.py  vertical.py  captions.py
  review/ app.py
  supervisor.py
tests/  docs/  config.yaml  blocklist.yaml
```

## M0: Scaffold and environment
Tasks: project setup, typed config loader, structured logging, SQLite schema (channels, chat_events, spikes, clips), CLI skeleton, a `doctor` command that checks ffmpeg, streamlink, Python version and writable data directory.
Accept: `peakreel doctor` reports pass or a clear failure per dependency on this Windows machine; tests run in CI-less local `pytest`.

## M1: Twitch chat ingest
Tasks: anonymous IRC over websocket (`justinfan` nickname, no token), join N channels, parse PRIVMSG, auto-reconnect with backoff, write JSONL per channel per day with UTC timestamps, drop known bots and `!commands` at the filter layer (logged, not discarded, so they can be replayed).
Accept: 30 minutes from 3 live channels with no gaps over 5 s; kill the network mid-run and it reconnects and resumes; log files replay deterministically.
Start this one first: the logs it records are the test data for M2.

## M2: Spike detector and replay harness
Tasks: 10 s buckets per channel; rolling baseline (median and MAD over the last ~5 min); trigger when count exceeds `max(floor, median + k*MAD)` and unique chatters exceed a minimum; merge adjacent hot buckets into one event; cooldown (default 60 s); score output; filters for raid, sub-train and copy-paste bot spam. `peakreel replay <logs>` runs the detector offline and prints events with timestamps and top messages. A labeling sheet (CSV) for marking events good or bad, and a script reporting precision per parameter set.
Accept: unit tests on synthetic traffic; on at least 2 hours of real logs, replay finds events a human marks as real moments with a documented precision, and parameters live in config.

## M3: Recorder and rolling buffer
Tasks: streamlink piped into ffmpeg writing 10 s segments with recorded start times, rolling buffer of ~10 min with pruning, handle stream offline, online and reconnect, handle ad breaks without crashing, expose `get_window(start, end)` returning a segment list.
Accept: pick any 30 s window within the last 10 min and the cut matches the requested time within 1 s; buffer disk use stays flat over a 1 hour run.

## M4: Horizontal clipper, end to end
Tasks: detector trigger fires, wait ~10 s for post-roll, cut window (default 20 s before to 10 s after the spike start, configurable), write the clip and a JSON sidecar (channel, spike score, time, top chat messages) to the outbox, record in SQLite, enforce the blocklist.
Accept: 1 hour unattended run on one live channel produces clips with correct content timing and no crashes.

## M5: Vertical renderer
Tasks: 9:16 layout with gameplay centered over a blurred, scaled copy of itself; optional per-channel facecam crop region (config) for a split layout; credit overlay (`credit` string plus channel name) inside the safe zone; thumbnail export.
Accept: output verified 1080x1920 with `ffprobe`; manual check of 5 clips from different channels shows no content in unsafe zones; audio intact.

## M6: Captions
Tasks: faster-whisper `small` (int8, CPU) with word timestamps; model downloaded once and cached in config path; generate ASS subtitles with a bold highlighted-word style; burn in during the vertical render; run in a worker queue so slow transcription never blocks detection or recording; skip captions on clips with no speech.
Accept: caption timing within 200 ms in a manual check of 10 clips; worker keeps up with clips arriving every minute on this machine; failure falls back to an uncaptioned clip.

## M7: Review tool and multi-channel supervisor
Tasks: supervisor runs recorder, chat and detector per channel with restart on failure; per-channel allowlist and the takedown blocklist; a small local web page listing clips with preview, approve, reject, and a title and description field; export of approved clips to a folder; a desktop or phone notification when a new clip is ready.
Accept: 5 channels run for 2 hours, a crash in one channel does not stop others, approved clips land in the export folder.

## M7a: Go-live detection, cross-channel dedupe and safety checks
Tasks:
- Go-live detection: poll Twitch (Helix API, free app credentials) to see which channels in `channels.yaml` are live, start and stop their workers automatically, and optionally discover top live channels in a configured category.
- Cross-channel dedupe: when several watched streamers spike on the same moment within a short window, keep the best clip (highest score and cleanest audio) and flag the rest as duplicates. Also skip near-identical clips already approved or posted.
- Safety checks before a clip reaches the review queue: transcript scan for slurs and configured blocked words, a flag for likely licensed music (streamers often play it and platforms mute or strike for it), and a manual-review reminder for on-screen personal information such as addresses, donation alerts with names, and private messages. Flags go to the reviewer; they do not auto-reject.
Accept: a channel going live and offline starts and stops its worker without intervention; a synthetic double-spike produces one clip; flagged clips show their reasons in the review tool.

## M8: Launch hardening
Tasks: 8 hour soak test, disk cleanup policy, log rotation, resource monitoring (CPU, disk, worker backlog), a dry run on the busiest channel available, a config preset for the launch channels, a `README` runbook (start, stop, what to do when a streamer asks for removal).
Accept: soak test finishes with no manual intervention and a summary report.

## M9: Publisher framework, metadata, credits and tags
Purpose: one pipeline from an approved clip to a post on any platform, with the same credit and tagging rules everywhere. Build this before any platform adapter.
Tasks:
- Data: a `publications` table (clip_id, platform, state: pending, approved, uploading, processing, published, failed, private_only; remote_id, url, error, attempts, scheduled_at). A `channels.yaml` with per streamer: Twitch name, display name, handles on TikTok, Instagram and YouTube (optional), default hashtags, credit text, permission status (`granted`, `credit_only`, `unknown`).
- Metadata builder: per-platform templates with variables (streamer, game, handles, source link, hashtags, a hook line taken from the transcript or top chat message). Enforce each platform's limits in code and keep the numbers in `config.yaml` (check current docs; they change): YouTube title and description and total tag length, Instagram caption length and hashtag count, TikTok caption length. Deterministic truncation with tests.
- Credit rules: every post carries a "Clip from twitch.tv/<name>" line in the caption or description, not only the on-video overlay. @mention a streamer only when their handle for that platform is in `channels.yaml`; never guess handles.
- Publisher interface: `validate(clip)`, `publish(clip, meta, mode)`, `poll_status()`. Modes: `export` (default), `private` (or draft), `public`. `export` writes a per-platform folder with the video, `caption.txt`, tags and a short checklist, for manual upload.
- Queue: per-platform daily caps and minimum spacing between posts (configurable), retries with backoff, idempotency (a clip is never posted twice to the same platform), `--dry-run` that does everything except the network call.
- Secrets: OAuth tokens in Windows Credential Manager via `keyring`, never in the repo or config; automatic token refresh.
- Review tool (extends M7): choose platforms per clip, edit metadata per platform, approve to queue, see publication state and errors.
Accept: approving a clip in `export` mode produces correct, within-limit packages for all three platforms; killing the process mid-queue and restarting never duplicates a post; metadata tests pass.

## M10: YouTube Shorts adapter
Tasks: OAuth installed-app flow, resumable `videos.insert` upload, title, description, tags, `categoryId` 20 (Gaming), `selfDeclaredMadeForKids` false, optional `publishAt`, processing status polling. There is no separate Shorts endpoint, so Shorts are identified by vertical format and length; verify current duration limit and keep it in config. Track quota against the limits in config. Detect forced-private uploads by reading back the video status and mark the publication `private_only`.
Accept: a test clip uploads privately with correct metadata visible in YouTube Studio; failures map to clear error states.

## M11: TikTok adapter
Tasks: developer app with Login Kit and Content Posting API, OAuth, call `creator_info/query` before every post to get allowed privacy options and limits, chunked video upload, status polling. Support both Direct Post and upload-to-inbox (draft, finished manually in the app). Expose the settings TikTok requires users to control (visibility, comments, duet, stitch, commercial content disclosure) in the review tool, and only send the video after the user has confirmed them there; TikTok's guidelines require this. Treat per-user daily post caps as config.
Accept: a test clip reaches a test account as a private post and as an inbox draft.

## M12: Instagram Reels adapter
Tasks: Meta app with `instagram_content_publish`, token handling and refresh, publishing flow: create a REELS container with `upload_type=resumable`, upload to the `rupload.facebook.com` endpoint, poll container status until finished, then `media_publish`. Check `content_publishing_limit` before each publish. Caption from the metadata builder, optional `collaborators` field when the streamer's handle is known (collaboration needs their acceptance). Confirm in current Meta docs which account types are supported (professional versus business only) and whether a linked Facebook Page is required, and document the result in `docs/`.
Accept: a Reel publishes to a test professional account with the right caption and cover.

## M13: Audits and account setup (mostly non-code, start immediately)
All three platforms keep API-posted content private until the app passes a review, so these reviews gate public auto-posting and run in parallel with everything else. Per sources checked on Nov 15 2026: TikTok unaudited clients post private only and are capped at a few users; YouTube projects created after 28 July 2020 without a compliance audit upload as private only; Meta needs app review for publishing. TikTok's review is stated as several days to about two weeks.
Tasks: create `docs/audits.md` with one section per platform covering app registration, required URLs (privacy policy, terms, data deletion, TikTok domain or URL verification), scopes, screen-recorded demo of the posting flow including the consent and disclosure UI, and submission status. Build a simple public privacy-policy and terms page. Risk to record in the file: reviewers may object to reposting third-party streamer content, and TikTok's posting guidelines describe the API as for creators posting original content, so a rejection is possible. Fallbacks: stay in `export` or `private` or draft mode with manual publishing, or add a provider adapter for an already-audited posting service (several exist; this adds cost and does not remove the rights question).
Accept: every platform has a submitted audit or review with its demo, or a recorded reason why not.

## Later (not launch-critical)
YouTube live chat adapter, Azure Linux VM deployment (same code, config only), Kick adapter, 9:16 split layouts per streamer, audio-loudness cross-check to cut false positives, links to the exact VOD timestamp in the description, post-performance tracking to feed back into detector scoring.

## Suggested order given the Nov 19 launch (per the reel)
Critical path: M0, M1 (start recording chat logs now), M2, M3, M4, M5, M6, a minimal M7, and M9 in `export` mode only. M2 tuning needs real chat data, so M1 should run against live channels while the later milestones are being built.
Public auto-posting will not be ready for launch: the platform reviews alone can take days to weeks. For launch, publish manually from the export packages (about a minute or two per clip), and use `private` mode on YouTube where available so you can finish in Studio. M10 to M12 follow once the credentials exist, and M13 should be started today because its review clocks are the real deadline.
