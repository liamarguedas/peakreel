# M2: Spike detector and replay harness

The detector turns recorded chat into scored events ("spikes") that are candidate clip moments.
It is a streaming state machine (`peakreel.detect.spikes.SpikeDetector`), so the exact same code runs offline over logs now and live inside the clipper in M4.

## How it decides

1. Unfiltered chat messages are counted in 10 s buckets aligned to epoch time.
   Bot and `!command` messages (flagged in M1) and platform notices do not count.
2. Each closed bucket is compared with a rolling baseline: the median and MAD of the previous 30 valid buckets (5 minutes).
3. A bucket is hot when `count > max(floor, median + k * MAD)` and it has at least `min_unique` distinct chatters, once the baseline holds 12 buckets (2 minutes of warm-up).
4. Hot buckets with at most `merge_gap` cold buckets between them form one event.
   An event ends when it goes cold or reaches `max_event_s`, and is followed by a `cooldown_s` quiet period.
5. Score is `(peak - median) / (MAD + 1)`.
   Peak, unique chatters, baseline and the top messages are kept with every event, so each score can be explained.

### Missing data is unknown, not zero

A bucket overlapping a recorder downtime (from M1's connection events) is invalid.
It never enters the baseline and never triggers.
Counting it as zero would fake a quiet period, and the next normal minute would look like a spike.
A downtime longer than `reset_after_downtime_s` (5 minutes) clears the baseline.

### Suppression

These events are kept but marked, and hidden unless `--show-suppressed` is given:

| `suppressed_by` | Rule |
| --- | --- |
| `raid` | A raid arrived within `raid_suppress_s` (90 s) before or during the event. |
| `sub_train` | At least `sub_train_min` (5) sub, resub or gift notices in the `sub_train_window_s` (30 s) before or during the event. |
| `spam` | Fewer than `min_unique_ratio` (0.3) distinct chatters per message: a few accounts pasting the same text. |

All parameters live under `detect:` in `config.yaml`.

## Commands

```
uv run peakreel detect replay data/chat                     # events with the configured parameters
uv run peakreel detect replay data/chat --set k=3 --set floor=15
uv run peakreel detect replay data/chat --candidate --show-suppressed
```

Each line shows the start time, channel, length, peak count with baseline, unique chatters, score and the top messages.

## Tuning workflow

The goal is a documented precision on real chat, with parameters chosen from evidence rather than guessed.

1. **Credentials (once).**
   Register an app at dev.twitch.tv/console (redirect URL `http://localhost`, category "Analytics Tool"), then run `uv run peakreel twitch login`.
   The client ID and secret are verified and stored in Windows Credential Manager, never in a file.
   `uv run peakreel twitch status` checks them; `uv run peakreel twitch logout` removes them.
2. **Build the sheet.**

   ```
   uv run peakreel detect label-sheet data/chat            # writes data/labels/labels.csv
   ```

   It runs the permissive `detect.candidate` overrides, so the sheet is a superset of what stricter settings find.
   Each row has a VOD link starting 15 s before the event, converted to Twitch's clock with the event's receive delay.
   When no link is possible, `notes` says why (no credentials, no archived VOD).
   Re-running it after more chat is recorded adds new rows and fills missing links, and never loses existing labels.
3. **Label.**
   Open the CSV, watch each link, and set `label` to `good` (a moment worth clipping), `bad` or `unsure`.
   In Excel, save as "CSV UTF-8" so Japanese and other chat stays readable.
4. **Evaluate.**

   ```
   uv run peakreel detect evaluate data/labels/labels.csv data/chat --grid docs/m2-grid.yaml --out docs/m2-results.md
   ```

   Every set in the grid (plus `default` and `candidate`) is scored.
   Events match labels by channel and time overlap, since parameters shift boundaries by a bucket or two.

### Reading the results

| Column | Meaning |
| --- | --- |
| Events/active h | Events per hour of connected time that had chat. Offline channels do not dilute it. |
| Good, Bad | Events overlapping a `good` row, or only `bad` rows. |
| Unknown | Events with no decisive label (blank, `unsure`, or not in the sheet). Never counted as either. |
| Precision | Good / (Good + Bad). |
| Recall vs candidate | Share of `good` rows this set still finds. It is relative to the candidate set, not to every real moment. |

Precision alone is easy to game: raise the thresholds until only a few obvious events remain.
Choose the operating point on precision together with events per hour and recall.
The review queue has to stay small enough to clear by hand, and good moments must not be thrown away.

## Known false-positive patterns

- **Stream endings.** Chat floods goodbyes ("乙", "gn", "bye") as the stream signs off.
- **On-screen prompts.** Polls, predictions and "type 1 in chat" spikes reflect instructions, not moments.

These are what labeling is for: if they dominate the `bad` rows, they get their own filter.
