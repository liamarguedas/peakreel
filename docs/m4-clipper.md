# M4: Live clipper

`peakreel run` is the first end-to-end pipeline: live chat goes in, horizontal clips with sidecars come out.

## Run it

```
uv run peakreel run start -C kato_junichi0817              # until Ctrl+C or `run stop`
uv run peakreel run start -C summit1g -d 3600              # stop after an hour
uv run peakreel run stop                                   # graceful stop of a detached run
```

For long runs, start it detached with `python -m peakreel` (see the M1 guide, "Long runs").
`run stop` finishes the clips in flight before stopping video.

## What one process does

| Part | Behaviour |
| --- | --- |
| Chat | One anonymous Twitch connection records every channel to `data/chat/` exactly like `chat record`, and feeds each event to the channel's spike detector. |
| Ticker | Advances every detector to the current time once a second, so an event finishes even if chat goes quiet. |
| Video | One recorder thread per channel fills `data/buffer/` (see the M3 guide); pruned every 30 s. |
| Clipper | Each finished spike becomes a task that records it, waits for the video, cuts it and publishes the clip. |

Shutdown is ordered:
1. Chat stops, and its `stopped` records are written.
2. The detectors are flushed.
3. Clips in flight finish while video is still being recorded.
4. The recorders stop.

## From spike to clip

1. **Recorded.** Every spike goes into the `spikes` table, including suppressed ones (`suppressed_by`), so every decision can be traced.
2. **Skipped** if it is suppressed (raid, sub train, spam) or the channel is on `blocklist.yaml`.
   **Clip budget**: a channel gets at most `clip.max_per_hour` clips (3) in any rolling hour, so busy chats do not flood review.
   A spike over budget is recorded with `suppressed_by: budget` and not cut, unless it scores at least `clip.keep_score` (10, about the top 10 percent), so a huge moment is never lost.
   The budget is first come, first served: an early weaker spike can take a slot before a stronger one.
   On 23 channels' recorded chat, busy channels spiked 8 to 12 times per hour; the budget kept 114 of 311 spikes.
   Skipped spikes per channel appear as `budget_skipped` in `status.json`; `max_per_hour: null` turns the budget off.
3. **Window.** The spike start is converted from the local chat clock to the broadcast clock by subtracting the spike's measured receive delay. The window is `[moment - clip.pre_roll_s, moment + clip.post_roll_s]`, 20 s before and 10 s after by default.
   The pre-roll is longer because chat reacts late: viewers see the stream several seconds behind, then type.
4. **Wait.** Segments arrive 3 to 7 s after broadcast, so instead of a fixed delay the clipper waits until the buffer covers the window, up to `clip.max_wait_s` (90 s).
   If it is still short, it cuts what is there and records the gap in the sidecar.
5. **Cut.** ffmpeg re-encodes the window frame-accurately into `data/outbox/<channel>/<start>-<channel>-<spike_id>.part.mp4`, at most `clip.concurrent_cuts` (1) at a time.
6. **Blocklist again.** A takedown may have arrived during the cut; if so the file is deleted.
7. **Publish.** The sidecar is written, the clip is renamed to its final `.mp4` name, and a `clips` row (state `new`) is added.

The blocklist is re-read for every spike, so adding a streamer takes effect without a restart.
If the file cannot be read, every channel is treated as blocked (fail closed).

Operational failures (no video, ffmpeg error) are logged and reported, never raised: one bad clip cannot stop the pipeline.

## Sidecar

```json
{
  "clip": "20261117T101500Z-kato_junichi0817-12.mp4",
  "spike_id": 12,
  "channel": "kato_junichi0817",
  "source": "https://www.twitch.tv/kato_junichi0817",
  "credit": "Clip from twitch.tv/kato_junichi0817",
  "window": {"start_utc": "...", "end_utc": "...", "start_ts": 0, "end_ts": 0,
             "clock": "broadcast (Twitch PROGRAM-DATE-TIME)", "gaps": []},
  "spike": {"score": 14.6, "peak_count": 83, "unique_chatters": 246, "receive_delay_s": 0.3,
            "top_messages": [{"text": "あ", "count": 39}], "...": "..."},
  "created_utc": "..."
}
```

## Configuration

```yaml
clip:
  pre_roll_s: 20
  post_roll_s: 10
  max_wait_s: 90
  concurrent_cuts: 1
  outbox: outbox        # under paths.data_dir
```

Detector parameters come from `detect:`; tune them with the M2 workflow before trusting the output.

## One process per channel's logs

Run each channel in exactly one process that writes chat logs: `chat record` or `run start`, not both.
Two processes appending to the same `data/chat/<channel>/<day>.jsonl` would duplicate every message and corrupt the detector's input.
The M7 supervisor will own this; until then, give a separate run its own `paths.data_dir`.
