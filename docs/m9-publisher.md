# M9: Publisher framework, metadata, credits

M9 turns an approved clip into posts: one per platform (YouTube Shorts, TikTok, Instagram Reels), with the same credit rules everywhere, text kept within each platform's limits, and a queue that never posts a clip twice to the same platform.
Only `export` mode exists so far: it writes a ready-to-post package per platform for manual upload.
The platform adapters (M10 to M12) plug into the same queue for `private` and `public` posting, once the platform audits (M13) allow it.

## Flow

1. In the review tool, a clip's card shows **Post to**: one checkbox per platform (default: `publish.platforms`) and, once queued, each platform's state and suggested posting time.
2. **Post text per platform** opens the generated text for each platform, editable, with a live counter in the platform's own unit and any problems.
3. **Approve** validates every chosen platform first; if any text breaks a limit or lacks the credit line, nothing is queued and the reason is shown.
   Otherwise each platform gets a publication, and in `export` mode its package is written at once.
4. You post from `data/publish/<platform>/<date>/<clip>/`, following its `checklist.md`.

Reject (or moving an approved clip back to new) cancels its publications and deletes their packages.
A takedown (`blocklist.yaml`) does the same for every clip of the channel.
A post that may already be on a platform is never removed silently: it is logged and reported for removal by hand.

## Export packages

```
data/publish/tiktok/2026-11-19/20261119T201500Z-somestreamer-42/
    video.mp4          vertical render (horizontal if none)
    thumbnail.jpg
    description.txt    the caption (YouTube: the description)
    title.txt          YouTube only
    tags.txt           YouTube only
    checklist.md       suggested post time, permission, flags, steps for this platform
    metadata.json      all of the above, machine-readable
```

Packages are written to a temporary folder and renamed, so a half-written one is never visible.
Re-exporting replaces a package in place.

## Streamer data

Per channel in `config.yaml` (one list for the allowlist, credit and publishing):

```yaml
channels:
  - name: somestreamer
    credit: "Clip from twitch.tv/somestreamer"
    display_name: SomeStreamer
    handles: {tiktok: somestreamer, youtube: somestreamer, instagram: somestreamer}
    hashtags: [someStreamer]
    permission: unknown          # granted | credit_only | unknown
```

- **Credit:** every post contains the credit line (always including `twitch.tv/<name>`), not only the on-video overlay.
  Validation refuses text without it, including text the reviewer edited.
- **Mentions:** `@handle` is added only for a platform where the streamer's handle is configured.
  Handles are never guessed.
- **Permission:** packages are made whatever the status; `checklist.md` and the card show it, with a warning for `unknown`.
  The API adapters (M10 to M12) will post publicly only for `granted` or `credit_only`.

## Post text

Each platform's text comes from a template (overridable in `publish.templates`) with these variables: `{title}` (the reviewer's title, else the top chat message with the streamer's display name), `{description}`, `{streamer}`, `{channel}`, `{game}`, `{mention}`, `{credit}`, `{source}`, `{hashtags}`.
Hashtags are the channel's, then the game's (`#GrandTheftAutoVI`, from Twitch stream status), then `publish.hashtags`, without duplicates.

Limits (verified 2026-11-17, kept in `config.yaml` because they change):

| Platform | Limits | Counted in |
| --- | --- | --- |
| YouTube Shorts | title 100, description 5000, tags 500 in total (commas and quotes count), no `<` `>` | characters, description in UTF-8 bytes |
| TikTok | caption 2200 | UTF-16 code units (an emoji counts 2) |
| Instagram Reels | caption 2200, 5 hashtags (lowered from 30 in December 2025), 20 mentions | characters |

When text is too long it is shortened deterministically, in this order: the description (at a word boundary, ending in "..."), then hashtags from the end, then the title.
The credit line is never shortened; if the text cannot fit with it, the clip is not queued.
Every change is listed in the package's checklist and the review card.

## Queue

Each clip has at most one publication per platform (a database constraint).
States: `pending` (draft text saved), `approved` (queued), `uploading`, then `exported`, `published`, `processing` or `private_only`; or `failed`, `cancelled`.

- **Never twice:** a row is marked `uploading` and committed before the publisher is called.
  After a crash, an `uploading` row is resolved by asking the platform what happened; for a network adapter an unknown outcome becomes `failed` ("check the platform before retrying"), never a second upload.
  Exporting is local and simply runs again.
- **Retries:** failures retry with backoff (`retry_initial_s`, doubling) up to `max_attempts`, then `failed`.
- **Pacing:** each platform gets a suggested time that keeps `min_spacing_min` between posts and at most `daily_cap` per UTC day.
  In `export` mode the time goes into the checklist; `private` and `public` modes will wait for it.

The review tool resolves interrupted publications when it starts.
From the command line:

```
uv run peakreel publish preview 42          # the text each platform would get
uv run peakreel publish run --dry-run       # what is due, without changing anything
uv run peakreel publish run                 # process due publications once
uv run peakreel publish run --watch         # keep processing (for scheduled modes)
uv run peakreel publish status              # recent publications
```

## Secrets

The platform adapters will keep OAuth tokens in Windows Credential Manager (`keyring`), like the Twitch credentials; never in the repository or config.
`export` mode needs no credentials.
