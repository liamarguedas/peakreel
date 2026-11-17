# M3: Stream recorder and rolling buffer

Peakreel keeps the last few minutes of each live channel on disk, so the clipper (M4) can cut a moment after chat reacts to it.

## Run it

```
uv run peakreel record start -C summit1g -C gaules      # until Ctrl+C
uv run peakreel record start                            # channels from config.yaml
uv run peakreel record status                           # what each buffer holds
uv run peakreel record stop                             # graceful stop of a detached recorder
```

For long runs, start it detached with `python -m peakreel` (see the M1 guide, "Long runs"); `record stop` works like `chat stop`.
Offline channels are checked every `record.offline_retry_s` (30 s) and recorded as soon as they go live.
A channel on `blocklist.yaml` is refused.

## Design: native HLS segments with broadcast time

The milestone plan said "streamlink piped into ffmpeg writing 10 s segments".
That pipeline loses the one thing alignment depends on.
Once streamlink's output is a plain byte stream, the only clock left is when bytes arrived.
streamlink removes the ads Twitch stitches into the stream for anonymous viewers, so every ad break would silently shift the video timeline against chat by the length of the ad.

Instead, streamlink runs in-process, and a small writer subclass receives each HLS segment together with its metadata:

- Each content segment (about 2 s of MPEG-TS, exactly as Twitch sent it) is saved unchanged.
- Its `EXT-X-PROGRAM-DATE-TIME` (PDT), the broadcast time Twitch stamps on it, goes into the session index.
- Ad segments are not saved; they are indexed as `"skipped": "ad"`, so ad breaks are explicit gaps.
- No ffmpeg process runs while recording.

Verified live (Nov 17): segments are 2.0 s, PDTs advance exactly 2 s per segment, the TS timestamps inside the files advance in lockstep, and concatenated segments decode without errors.

The writer hook is streamlink internal API.
`uv.lock` pins streamlink, and `tests/test_recorder.py` drives the real Twitch stream classes against a local playlist with PDTs and ad markup, so an incompatible upgrade fails the tests.
streamlink loads plugins through its own importer, so the capture classes are built at runtime from the live stream's own reader and writer, never from a normal import (a regression test covers this).

## Layout

```
data/buffer/<channel>/<session>/<seq>.ts
data/buffer/<channel>/<session>/index.jsonl
```

A session is one continuous recording, named by its UTC start time, for example `20261117T084040Z`.
A new session starts each time the channel is found live, including after a reconnect.

```json
{"seq":3396,"pdt":1794904295.275,"duration":2.0,"arrival":1794904302.312,"file":"3396.ts"}
{"seq":3400,"pdt":1794904303.275,"duration":0.0,"arrival":1794904309.1,"skipped":"ad"}
```

| Field | Meaning |
| --- | --- |
| `pdt` | Broadcast time of the segment's first frame, Twitch's clock, UTC epoch seconds. `null` if Twitch sent none; such segments cannot be placed in time. |
| `duration` | Segment length. streamlink reports `0.0` for ad segments; only content durations are meaningful. |
| `arrival` | Local download time. `arrival - pdt` is the delivery latency (3 to 7 s measured). |

Segment files are written under a temporary name and renamed, and the index line comes after the rename, so readers never see a partial segment.

## Pruning and disk

Every `record.prune_interval_s` (30 s), segments ending more than `record.buffer_minutes` (10) before the channel's newest video are deleted.
Age is measured against the newest stored video, not the wall clock, so the last minutes of a broadcast stay available after the channel goes offline.
Session directories left empty are removed, except the newest.
Each prune logs `freed_mb` and `usage_mb` per channel to `data/logs/peakreel.jsonl`.

Budget: 1080p60 is about 1 MB/s, so a 10-minute buffer is about 600 MB per channel.
Set `record.quality: 720p60` to roughly halve that.

## Cutting a window

```
uv run peakreel record cut summit1g now-300 now-270 data/cuts/test.mp4
uv run peakreel record cut summit1g 2026-11-17T08:45:00Z 2026-11-17T08:45:30Z out.mp4
```

Times are on the broadcast clock.
`peakreel.record.buffer.get_window(buffer_dir, channel, start, end)` returns the segments, the offset into the first segment, and every gap inside the range (before the first segment, ad breaks, reconnects, after the last).
A partly stored window is returned with its gaps listed, never silently shortened.
`record cut` warns about gaps, then re-encodes the range frame-accurately (`cut_window`).
This is a verification helper; M4 renders the real clips.

## Clocks

Video time is Twitch's clock (PDT), so a local clock correction cannot corrupt it.
Chat time is the local clock.
`peakreel doctor` fails if the local clock is more than 2 s off Twitch's, and each chat spike carries its measured receive delay, so M4 can convert chat time to broadcast time exactly.
