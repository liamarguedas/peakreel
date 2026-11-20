# M9: Publisher framework, metadata, credits

M9 turns an approved clip into posts: one per platform (YouTube Shorts, TikTok, Instagram Reels) on the posting account for the clip's language, with the same credit rules everywhere, text kept within each platform's limits, and a queue that never posts a clip twice to the same account and platform.
Only `export` mode exists so far: it writes a ready-to-post package per platform for manual upload.
The platform adapters (M10 to M12) plug into the same queue for `private` and `public` posting, once the platform audits (M13) allow it.

## Accounts per language

Each language gets its own posting accounts (for example an English set of YouTube, TikTok and Instagram channels, and a Spanish set), so languages are never mixed on one channel.

```yaml
channels:
  - name: gaules
    language: pt            # what the streamer speaks
publish:
  accounts:
    - {id: en, name: English, languages: [en], credit: "Clip from twitch.tv/{channel}"}
    - {id: es, name: Español, languages: [es], credit: "Clip de twitch.tv/{channel}"}
    - {id: pt, name: Português, languages: [pt], credit: "Clipe de twitch.tv/{channel}"}
    - {id: ja, name: 日本語, languages: [ja], credit: "クリップ元: twitch.tv/{channel}"}
```

- **A clip's language** is, in order: the reviewer's choice on the card, the channel's `language`, Twitch's broadcast language (after `peakreel twitch login`), the language Whisper heard in the clip.
  The card shows which one applied ("channel", "Twitch", "speech", "chosen").
- **The account** is the one whose `languages` contain it.
  A clip whose language has no account is **held**: approving it queues nothing, and the card says so; pick a language on the card to post it anyway.
  An account without `languages` would take every other language instead (none is configured, so unmatched clips are held).
- Each account has its own credit line in its language (it must contain `twitch.tv/{channel}`), and optionally its own `hashtags`, `platforms` (where it exists), `handles` and `templates`.
- Daily caps and spacing apply per account and platform, so the Spanish TikTok's schedule does not delay the English one.
- Changing a queued clip's language moves it: packages on the old account are deleted (posts already on a platform are reported, never removed silently).
- Captions stay in the spoken language: a clip moved to another language's account keeps its original subtitles.

## Flow

1. In the review tool, a clip's card shows its **Language** and the account it posts to, then **Post to**: one checkbox per platform (default: `publish.platforms`) and, once queued, each platform's state and suggested posting time.
2. **Post text per platform** opens the generated text for each platform, editable, with a live counter in the platform's own unit and any problems.
3. **Approve** validates every chosen platform first; if any text breaks a limit or lacks the credit line, nothing is queued and the reason is shown.
   Otherwise each platform gets a publication, and in `export` mode its package is written at once.
4. You post from `data/publish/<account>/<platform>/<date>/<clip>/`, following its `checklist.md`.

Reject (or moving an approved clip back to new) cancels its publications and deletes their packages.
A takedown (`blocklist.yaml`) does the same for every clip of the channel.
A post that may already be on a platform is never removed silently: it is logged and reported for removal by hand.

## Export packages

```
data/publish/es/tiktok/2026-11-19/20261119T201500Z-somestreamer-42/
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
Hashtags are the channel's, then the game's (`#GrandTheftAutoVI`, from Twitch stream status, with a bracketed abbreviation like "(GTA)" dropped), then `publish.hashtags` (GTA6, GTAVI, GTA, twitch: Instagram keeps only the first 5), without duplicates.

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

Each clip has at most one publication per account and platform (a database constraint).
States: `pending` (draft text saved), `approved` (queued), `uploading`, then `exported`, `published`, `processing` or `private_only`; or `failed`, `cancelled`.

- **Never twice:** a row is marked `uploading` and committed before the publisher is called.
  After a crash, an `uploading` row is resolved by asking the platform what happened; for a network adapter an unknown outcome becomes `failed` ("check the platform before retrying"), never a second upload.
  Exporting is local and simply runs again.
- **Retries:** failures retry with backoff (`retry_initial_s`, doubling) up to `max_attempts`, then `failed`.
- **Instagram first, then promotion:** with `publish.platforms: [instagram]` and `publish.promote_to: [youtube, facebook]`, approving posts to Instagram only.
  Three hours later (`stats.CHECKPOINTS_H`) a post whose likes + 3 x comments beat the median of its language's Instagram posts of the last 24 hours is queued for YouTube and Facebook (`peakreel/publish/promote.py`), once; while that median is 0, any engagement counts.
  YouTube (100 uploads a day for the whole project) and Facebook (20 a Page here) take far fewer than Instagram, so they get the clips that did well instead of a queue days long.
  A promoted post expires `max_wait_h` after it was promoted, not after its clip.
- **Several accounts per language:** a language may have more accounts (`en2`, `en3`, usually Instagram-only through Instagram Login).
  The first listed is the main one; for each platform a clip goes to the language's account on that platform with the fewest posts there in the last 24 hours, and keeps it when its posts are updated.
  Each account posts different clips (the same clip on three accounts looks like spam) and has its own daily cap.
- **Pacing, newest first:** each pass posts, per account and platform, the newest waiting clip once `min_spacing_min` has passed since the last post there and fewer than `daily_cap` went out in the last 24 hours.
  A post still waiting `publish.max_wait_h` (12 h) after its clip was made is cancelled (`expired: ...`): the platforms' caps let through less than is approved, and a late clip is one other accounts posted long ago.
  Until 2026-11-19 each post got a fixed slot when queued, first come first served; behind a backlog every clip went out days late.
  In `export` mode the time goes into the checklist; `private` and `public` modes will wait for it.

The review tool resolves interrupted publications when it starts.
From the command line:

```
uv run peakreel publish preview 42          # the text each platform would get
uv run peakreel publish run --dry-run       # what is due, without changing anything
uv run peakreel publish run                 # process due publications once
uv run peakreel publish run --watch         # keep processing (for scheduled modes)
uv run peakreel publish status              # recent publications
uv run peakreel publish requeue 1115 1131    # put failed or dropped Instagram posts back; their 6 h wait starts now
```

## Secrets

The platform adapters will keep OAuth tokens in Windows Credential Manager (`keyring`), like the Twitch credentials; never in the repository or config.
`export` mode needs no credentials.
