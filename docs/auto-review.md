# Auto review

Every finished clip gets one automatic decision: **approve**, **reject**, or **leave for a person**.
The review page applies it in its background loop (every 30 s), after publishing.
Each card shows the decision and why.

## Modes (`review.auto.mode`)

| Mode | What happens |
| --- | --- |
| `off` | Nothing is decided. |
| `shadow` (default) | The decision is shown on the card only ("would approve: ..."). Use it to compare with your own judgment. |
| `on` | Decisions are applied exactly like a click on Approve or Reject: exports, the publication queue and the blocklist check run as usual. Switching to `on` also applies the decisions already made in shadow mode. |

A click by a person always wins: auto never touches a clip that is no longer new, and checks again right before applying.

## The two stages

### 1. Chat rules (always, free, instant)

| Decision | Rule |
| --- | --- |
| Reject | a flag in `reject_flags` (`music`: copyright risk); no speech; chat not excited (under `reject_below_excitement`, 15 %, of the top messages contain an excitement word: catches polls such as "yes / no", chat bots such as "!drops") |
| Approve (without Gemini) | at least `approve_excitement` (50 %) excitement, spike score of at least `approve_score` (8), no flags |

Excitement words are regexes in `excitement_words` (LOL, KEKW, NOOO, GG, W, POG, clip, jajaja, kkkk, 草, and more).

### 2. Gemini watches the clip (free tier)

Clips the rules did not reject are sent to Gemini, picture and sound, as a small copy (640 px, 2 fps, about 0.6 MB).
Gemini answers with a score from 1 to 10, what happens, a title, a post description, 3 to 5 hashtags, and whether personal information is readable on screen.

| Decision | Rule |
| --- | --- |
| Reject | Gemini score at most `ai_reject` (3) |
| Approve | Gemini score at least `ai_approve` (7), no personal information on screen |

With a Gemini verdict, its score replaces the chat thresholds, and the `alerts` and `blocked_words` flags no longer hold a clip (big streams show alerts almost constantly; Gemini checks the screen itself).

Gemini's **title and description** are written into the clip as soon as it has judged it, unless you already wrote your own, so a manual approval uses them too.
Its **hashtags** go into every post, after the streamer's and before the game's; each platform's limit (Instagram: 5) drops the generic ones (`publish.hashtags`) first.

Every decision, in both stages, also needs: the streamer's permission `granted` or `credit_only`, a posting account for the clip's language, captions, and less than `daily_cap` auto-approvals for the account today.

## When Gemini is busy or out of quota

The free tier is often busy ("high demand", HTTP 503) and has daily limits (HTTP 429).

- Models in `gemini_models` are tried in order: `gemini-4-hydrogen`, `gemini-3.8-flash`, `gemini-3.5-flash-lite`.
- If none answers, the clip waits and is tried again on the next pass.
- After `ai_wait_min` (30 minutes) without an answer, the chat rules decide alone, as if Gemini were not there; the card says "Gemini unavailable".
- At most `ai_per_pass` (3) Gemini calls per pass, so a slow Gemini never holds up publishing.
- Clips the chat rules reject never use a Gemini request.

Without a key (`peakreel gemini logout`), or with `gemini_models: []`, only the chat rules run.

## Setup

1. Create a free key at https://aistudio.google.com/apikey.
2. `uv run peakreel gemini setup` in a terminal (it asks for the key and stores it in the Windows Credential Manager).
3. `uv run peakreel gemini judge <clip.mp4> --channel <name>` to see one judgment.

On the free tier, Google may use what is sent to improve its products.

## Measured on launch day (2026-11-18)

- Chat rules over 45 clips: 19 rejected, 24 left for a person, 2 approved; polls and chat bots were the main rejections.
- Gemini on 7 clips: a highway crash scored 7/10; a bank-loan cutscene with a chat poll 2/10; shopping and walking around 2/10; the clip already posted from stableronaldo 2/10.
- Gemini took 17 to 99 seconds per clip, and the free tier was often busy (503).
