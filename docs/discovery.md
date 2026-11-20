# Category discovery: record the top streamers of a game

Besides the streamers listed by hand under `channels:`, Peakreel can follow a Twitch category (for example Grand Theft Auto VI) and record its most watched live streams, starting and stopping them while it runs.

Needs `peakreel twitch login` (Twitch API).

## Configure

```yaml
discover:
  enabled: true
  category: "Grand Theft Auto VI"   # exact Twitch name
  max_channels: 10                  # recorded at once, manual channels included
  min_viewers: 500
  languages: null                   # null: the languages of publish.accounts (en, es, pt, ja)
  refresh_min: 5
  keep_min: 20
  permission: credit_only           # given to discovered streamers (they were not asked)
  exclude: []                       # never discover these
```

Check the choice before turning it on:

```
uv run peakreel discover preview
```

It lists the category's top live streams and, for each, whether it would be recorded, or why not (manual, blocked, no account for its language, few viewers, no slot).

## How it chooses

- Every `refresh_min` minutes Twitch is asked for the category's live streams, most viewers first.
- Candidates speak a language that has a posting account (or one of `languages`), have at least `min_viewers`, and are not blocklisted, excluded or already configured by hand.
- Manual channels always run and use slots first; discovered ones fill the rest up to `max_channels`.
- A recorded channel stays while it is live and within the wider top (`max_channels * 1.5` places); once it falls further it stays `keep_min` more minutes, so a small ranking change never cuts a recording.
  It is stopped at once when it goes offline or leaves the category.
- A channel already recorded by another Peakreel process is skipped.

Adding a channel takes its lock, starts its recorder and joins its chat; removing it does the reverse.
Its buffered video ages out like any other.
The status file and the review page show each channel's `source` (`manual` or `discovered`).

## Language and permission

A discovered channel's clips are routed by Twitch's broadcast language to the account for that language (M9), like any channel without a configured `language`.

Discovered streamers were never asked, so they get `discover.permission`.
With `credit_only` their approved clips post publicly with the credit line; with `unknown` they would stay private on YouTube, a draft on Facebook and an export for Instagram.
Every clip still needs your approval in the review page, and a streamer who asks for removal goes on the blocklist: discovery never picks them again, and their clips are taken down.

## Capacity

Each channel costs a recorder (network, little CPU) and, for each clip, captions and a render.
The M8 soak ran 8 channels with memory flat around 460 MB; watch the review page's health warnings ("captions behind", "clips piling up") before raising `max_channels`.

## A language at its Instagram cap gives way (2026-11-20)

When every connected Instagram account of a language has posted its `daily_cap` in the 24 hours that will still count 2 hours from now, discovery stops recording that language's streamers: their clips would only wait and be dropped as stale.
Their channels stop at the next discovery pass, and `language_max` is lifted so English and Spanish streamers take the slots.
When the accounts have room again, the language comes back on its own (`peakreel.publish.queue.full_languages`; logged as "discovery: languages at their Instagram cap").
