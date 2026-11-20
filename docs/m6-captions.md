# M6: Captions

Every clip with speech gets burned-in captions: short lines, the spoken word highlighted, placed inside the safe area.

## Setup (once)

```
uv run peakreel captions download        # faster-whisper `small`, about 464 MB, into tools.whisper_model_dir
uv run peakreel doctor                   # [PASS] captions  model 'small' cached
```

After the download the model is loaded with `local_files_only`, so captioning never needs the network.

## Use

The live pipeline (`peakreel run start`) captions every clip automatically before rendering it.
By hand:

```
uv run peakreel render data/outbox --captions          # caption + render (default from config)
uv run peakreel render data/outbox --no-captions
uv run peakreel captions make clip.mp4                 # transcribe only: prints word timings, writes clip.ass
```

`captions make` is the tool for a manual timing check: the printed start times can be compared with the audio, and the `.ass` file can be edited and the clip re-rendered.

## How it works

1. **Audio.** The configured ffmpeg decodes the clip to 16 kHz mono samples.
   faster-whisper's own decoder (PyAV) is bypassed: PyAV 19 removed an argument faster-whisper 1.2.1 still passes, and one decoder (ffmpeg) for everything is simpler anyway.
2. **Transcription.** Whisper `small`, `int8` on CPU, with word timestamps and voice-activity filtering.
   The language is detected per clip unless `captions.language` is set.
   `beam_size: 5`: greedy decoding (`1`) dropped whole segments on noisy game audio (3 words instead of 71 on a casting clip).
3. **No speech.** Fewer than `captions.min_words` (3) words means the clip renders without captions (`status: no_speech`).
4. **Lines.** Words are grouped into one-line chunks of at most `captions.max_line_chars` (20; CJK characters count 2), broken at pauses over 0.8 s and after sentence ends.
   Japanese, Chinese and Thai join without spaces.
5. **Style.** One ASS event per word, lasting until the next word: the line stays on screen and the highlight (`captions.highlight_color`) moves.
   Bold, outlined, centred between the left margin and the right unsafe zone, baseline at `captions.bottom_fraction` (70%) of the height, above the credit overlay.
   Config validation rejects a placement inside the bottom unsafe zone.
6. **Burn-in.** The vertical render (M5) adds libass's `subtitles` filter under the credit.
   The font is copied into a temporary `fonts` directory passed as a relative `fontsdir`: no drive-letter escaping, and libass does not scan the whole system font directory.

The sidecar records the outcome:

```json
"captions": {"status": "ok", "language": "ja", "words": 66, "seconds": 3.7, "file": "<name>.ass"}
```

## Worker queue

Transcription runs on one dedicated worker thread that owns the model.
Clips queue in front of it; chat ingest, detection and recording never wait for it.
Each clip logs `captioning queued` and `captions done` with the current `backlog`.
The model is loaded when the pipeline starts, so a missing model shows up immediately (`captions unavailable; clips will render without`), not on the first clip.

## Failure behaviour

| Failure | Result |
| --- | --- |
| Model missing or unloadable, decode error | `status: failed` with the error; the clip renders without captions. |
| Too little speech | `status: no_speech`; rendered without captions. |
| No usable font | Captions disabled at start; clips render without. |
| Render error | Horizontal clip kept (M5 behaviour). |

## Verified (Nov 17)

**Speed.** Transcription takes 3 to 4 s per 30 s clip; caption plus vertical render is about 16 s per clip.
With a clip arriving every minute the worker is busy about a quarter of the time, so the backlog cannot grow.

**Timing against known word times.** Windows' speech synthesizer reports the audio position of each word it speaks.
Whisper's word starts against those positions (35 words): median error 44 ms, 94% within 200 ms, worst 289 ms; captions appear on average 63 ms early, the right direction for a viewer.
(SAPI reports positions as if the audio were 16 kHz while writing 22.05 kHz files; the positions must be scaled by 16000/22050.)

**Timing on 10 real clips** (6 Japanese, 2 English, Portuguese, French): for every word that follows a pause, the distance to the nearest rise in speech-band audio energy.
Median 30 ms, 83% within 200 ms over 63 words.
Game sounds also create energy rises, so this measure is noisy and understates accuracy.

**Weak spot.** With two voices overlapping at similar volume (synthetic speech mixed under a full-volume esports cast), only 14 of 35 words were recovered and timing degraded.
Streams usually mix the streamer's own voice on top, but caster-heavy clips caption worse.

The milestone's acceptance asks for a manual check of 10 clips.
Ten captioned renders for that check are in `data/m6-review/` (gitignored).

## Known limits

- Whisper splits Japanese into sub-word tokens, so a line can break inside a word.
- The `small` model mishears names and game terms; `captions.model: medium` is more accurate and roughly 2 to 3 times slower.

## On the graphics card (2026-11-20)

Captions run on the NVIDIA card (`captions.device: cuda`, `compute_type: int8_float16`).
cuBLAS and cuDNN come as pip packages (`nvidia-cublas-cu12` 12.4, `nvidia-cudnn-cu12` 9.1, the last with Windows builds); Peakreel puts their DLL folders on the search path itself, so no CUDA install is needed.
Measured on one clip while recording: 2.4 s on the card, 5.1 s on 4 CPU cores, the same text.
Loading transcribes a second of silence to prove cuDNN works; if anything fails, captions fall back to the CPU (`int8`, `cpu_threads`) with a warning in the log.
