# M5: Vertical renderer

Every clip is rendered as a 1080x1920 vertical video with the channel name, its platform icon and a thumbnail, ready for TikTok, Instagram Reels and YouTube Shorts.

## Run it

The live pipeline (`peakreel run start`, M4) renders each clip automatically right after it is published.
To render or re-render clips by hand:

```
uv run peakreel render data/outbox                       # every clip in the outbox
uv run peakreel render data/outbox/summit1g/<clip>.mp4   # one clip (channel from its sidecar)
uv run peakreel render some-clip.mp4 -C summit1g         # a clip without a sidecar
```

Outputs go next to the clip:

| File | Content |
| --- | --- |
| `<name>.vertical.mp4` | 1080x1920, 30 fps, H.264 `yuv420p`, AAC 48 kHz (all from `output:`). |
| `<name>.jpg` | Thumbnail: the vertical frame at the spike moment. |
| `<name>.json` | The sidecar gains `"vertical": {"file", "thumbnail", "layout"}`, or `"render_error"` if rendering failed. |

The `clips` table records `vertical_path` and `thumbnail_path`.
A render failure keeps the horizontal clip; the reason is in the sidecar and the log.

## Layouts

| Layout | When | Composition |
| --- | --- | --- |
| `split` | The streamer's webcam is found | The webcam on top (`render.facecam_height_fraction`, 35% of the height), the name band, then the gameplay zoomed in by `render.game_zoom` (1.4), over a blurred copy of the stream. |
| `blur` | No webcam overlay | The gameplay at full width, centred over a blurred, darkened copy of itself, with the name band right above it. |

The gameplay zoom cuts on the webcam's side first, so the webcam is never shown a second time inside the gameplay.

### Finding the webcam

Most channels are discovered automatically, so the webcam is found per clip (`peakreel/render/facecam.py`):

- Every keyframe (every 2 s) is searched for faces with YuNet (`face_detection_yunet_2023mar.onnx`, OpenCV model zoo, MIT licence).
- The webcam is the face found at the same place in at least 60% of the keyframes: an overlay stays put, game characters move.
- A face in the middle third of the frame is the whole stream (just chatting, IRL), not an overlay: the clip keeps the `blur` layout.
- The crop snaps to the overlay's border: edges are combined over time with a median, which keeps only lines present in every keyframe.
  A side without such a line (a webcam in a corner, a cut-out webcam) gets a margin around the face instead.

Measured on the latest clip of 16 launch-day channels: the webcam was found in 12; the other 4 (no webcam, a webcam mid-frame, a cut-out webcam, an IRL stream) kept the `blur` layout.
Detection takes about half a second per clip; if it fails, the clip renders with `blur`.

A channel's `facecam` region in the config overrides the detection, as fractions of the source frame:

```yaml
channels:
  - name: kato_junichi0817
    facecam: {x: 0.80, y: 0.52, w: 0.20, h: 0.40}
```

The region used is recorded in the clip's sidecar (`vertical.facecam`).

## Name band and safe zones

The video shows the channel's name (the Twitch login or Kick slug, without `twitch.tv/` or `kick.com/`) next to its platform's icon, in a dark band across the frame.
The full credit with the channel's address stays in every post's text (`docs/m9-publisher.md`).

Icon and name are centred together: the name's real width is measured by drawing it once with ffmpeg (`bbox`).
The font size (at most `render.credit_max_font_px`, 52) is chosen so icon and name stay clear of the side safe zones (`output.safe_zone_right`, 12%, on both sides); a name too long even at 20 px is cut with an ellipsis.

The default font is the first installed of Yu Gothic Bold, Malgun Gothic Bold, MS Gothic and Arial Bold, so Japanese and Korean channel names render; set `render.font_file` to override.

## Windows details

ffmpeg's filter syntax uses `:` as a separator, so drive-letter paths break it (one of the Windows gotchas in the plan).
The channel name is passed with `textfile=` under a relative name (ffmpeg runs with `cwd` set to a temporary directory), and the font path is escaped (`C\:/Windows/Fonts/...`).

## Verified (Nov 17)

Five clips from five channels (kato_junichi0817, summit1g as fragmented MP4, gaules, otplol_, eslcs):

- All outputs 1080x1920, `yuv420p`, 30 fps, 30.000 s.
- Audio intact: mean loudness of each render matches its source within 0.1 dB over the full 30 s.
- Thumbnails checked with the unsafe zones overlaid: the credit is inside the safe area in every case.
- The split layout with a Japanese credit renders correctly in Yu Gothic.

A render takes about 11 s per 30 s clip at `preset: veryfast` on this machine.

## Open choice

In the `blur` layout the gameplay band is 608 px of the 1920 px height, which is small on a phone.
Cropping the sides to zoom (for example 1.4x) makes the action bigger but can cut off HUD elements at the edges.
Not enabled; a `render.zoom` setting is the place to add it once a direction is chosen.
