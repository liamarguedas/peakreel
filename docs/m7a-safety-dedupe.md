# M7a: Go-live tracking, duplicate detection and safety flags

M7a adds three things between a spike and the review queue: Twitch stream status, cross-channel duplicate detection, and safety flags that tell the reviewer what to check.
Nothing here rejects a clip on its own; duplicates and flags are shown to the reviewer, who decides.

## Setup

```
uv run peakreel safety download     # blocked-word lists (once)
uv run peakreel doctor              # checks word lists and ffmpeg's Chromaprint support
uv run peakreel twitch login        # optional: stream status through the Twitch API
```

## Go-live

Each channel's recorder checks for a live stream every `record.offline_retry_s` (30 s) and starts recording on its own when the channel goes live, and stops when it ends.
The M7 run showed this unattended: kato_junichi0817 went offline at 14:19 and otplol_ came back with a new recording session, with no intervention.

After `peakreel twitch login`, the supervisor also polls the Twitch API every `golive.poll_s` (60 s) for all channels in one call.
This adds to `status.json` and the review page, per channel, `live` and `game`, and logs `channel is live` (with game, title and viewers) and `channel went offline`.
Each clip's sidecar gets a `stream` section with game, title, viewers and language, for titles and tags in M9.
Without credentials polling is off and everything else works as before.

## Duplicates

When several watched channels show the same moment (co-streams of one tournament, a shared watch party), only the best clip should reach the queue.
Matching by time alone would be wrong: two streamers who both react to a game launch at the same second are two different clips.
So clips are matched by their audio.

- Every clip gets a Chromaprint fingerprint (`<clip>.fp`, made by ffmpeg).
- A new clip is compared with kept clips that start within `dedupe.window_s` (60 s), at every offset up to `dedupe.max_offset_s` (12 s), because streams reach viewers with different delays.
- The match strength is how far the best offset stands out from all the others (details in `peakreel/dedupe.py`).
- Of a matching pair, the kept clip is the approved one if either is approved, else the one with the higher spike score, else the earlier one.
  The other gets `duplicate_of`; it is not rendered or captioned (saving CPU), no notification is sent, and it appears under the review page's **Duplicates** tab, where it can still be approved.

### How well it works

Measured on the 46 clips of the M7 run (1035 unrelated pairs, many from the same channel) and on co-streams built from those clips (one clip delayed 3 s with a whole other clip, game audio and voice, mixed over it):

| Pair | Match strength |
| --- | --- |
| Unrelated clips | at most 13.2 |
| Co-stream, other audio at most half as loud as the shared audio | 13 to 45 (all but one above 16) |
| Co-stream, other audio as loud as the shared audio | 14 to 40 |
| Co-stream, other audio twice as loud | 6 to 31 |

The threshold `dedupe.min_strength` is 16, chosen for precision: a false duplicate would hide a good clip, while a missed one only shows two clips of the same moment in review.
The limit to know: a co-streamer who talks loudly over quiet shared audio is often not matched, and two such co-streams are matched with each other even less reliably.
Check any two clips with:

```
uv run peakreel safety compare a.mp4 b.mp4
```

## Safety flags

After captioning, every clip is checked and gets flags with a reason, shown in amber on its card in the review tool, stored in the database and the sidecar, and copied into the export's `metadata.json`.

| Flag | Raised when |
| --- | --- |
| `blocked_words` | A blocked word is in the transcript or in the top chat messages. |
| `music` | The audio has a steady musical beat: listen for licensed music, which platforms mute or strike. |
| `alerts` | Subs, gifts, cheers or a raid happened during the clip: their on-screen alerts usually show viewer names. |
| `checks_incomplete` | A check could not run (word lists not downloaded, audio unreadable), so a clip without flags is less certain. |

Every card also carries a reminder to check the video for personal information (alerts with names, addresses, private messages): that cannot be detected reliably, so the reviewer checks it.

### Blocked words

Two sources, both re-read for every clip:

- The LDNOOBW lists ("List of Dirty, Naughty, Obscene and Otherwise Bad Words", CC BY 4.0), pinned to one commit, for the languages in `safety.languages` (default en, pt, es, ja; 715 terms).
  Streams mix languages, so every list is applied to every clip.
- `safety.yaml` in the repository root: `blocked_words` adds your own terms, `ignore_words` removes terms that only cause false alarms on your channels.

Matching ignores case.
Space-separated languages match whole words only (`ass` does not match `class`); Japanese, Chinese, Korean and Thai match anywhere, because they do not separate words.
On the 46 M7 clips (all transcribed) one clip was flagged: `殺人事件` ("murder case") in game dialogue.

### Music

A local tool cannot tell which song is playing (that needs a song catalogue), so this flag answers "is there music with a steady beat?" and the reviewer listens.
Game soundtracks with a beat are flagged too.
The score is the autocorrelation of the audio's onset envelope at 70 to 180 beats per minute.

| Audio | Score |
| --- | --- |
| The 46 M7 clips (talk, gameplay) | 0.04 to 0.21 |
| A drum track | 0.79 to 0.85 |
| Real speech with a drum track under it at a quarter of its loudness | 0.49 |
| The same at equal loudness | 0.63 |

`safety.music_threshold` is 0.35.
Recall on real songs is not validated yet; calibrate on your own clips with:

```
uv run peakreel safety scan --transcribe data/outbox/*/*[0-9].mp4
```

### Alerts

The supervisor keeps each channel's recent subs, gifts, cheers and raids from chat and flags clips that contain one, converting the clip's video time to chat time with the spike's measured receive delay.
Chat notices without an on-screen alert (watch streaks, announcements) are ignored.

## Review tool

- **New** lists new clips that are not duplicates; **Duplicates** lists new clips that duplicate another, with the clip they duplicate and the match strength.
- Flags appear above the chat chips; `checks_incomplete` in grey.
- Approving is never blocked by a flag.

## Configuration

```yaml
paths:
  safety_words: safety.yaml
safety:
  languages: [en, pt, es, ja]
  word_lists_dir: safety      # under data_dir
  music_threshold: 0.35
  alerts: true
dedupe:
  enabled: true
  window_s: 60
  min_strength: 16
  max_offset_s: 12
golive:
  poll_s: 60
```

Duplicate detection needs an ffmpeg build with Chromaprint (gyan.dev and BtbN builds have it); `peakreel doctor` checks.
