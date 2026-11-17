# M5: Vertical renderer

Every clip is rendered as a 1080x1920 vertical video with a credit overlay and a thumbnail, ready for TikTok, Instagram Reels and YouTube Shorts.

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
| `blur` | Default | The gameplay scaled to full width, centred over a blurred, darkened copy of itself that fills the frame. |
| `split` | The channel has a `facecam` region | The facecam crop on top (`render.facecam_height_fraction`, 35% of the height), the centre of the gameplay below. |

The facecam region is configured per channel as fractions of the source frame:

```yaml
channels:
  - name: kato_junichi0817
    credit: "加藤純一 / twitch.tv/kato_junichi0817"
    facecam: {x: 0.80, y: 0.52, w: 0.20, h: 0.40}
```

To find the numbers, take a frame from a clip (for example the thumbnail of a horizontal render) and read off where the camera sits.
Streamers move their camera between games, so check it when a channel's layout changes.

## Credit overlay and safe zones

The credit is the channel's `credit`, with `twitch.tv/<channel>` appended if it does not already name the channel.
It is drawn lower-left on a translucent box, just above the bottom unsafe zone (`output.safe_zone_bottom`, 20%) and clear of the right one (`output.safe_zone_right`, 12%), where platform UI covers the video.

The font size (at most `render.credit_max_font_px`) is computed from an upper-bound width estimate (CJK characters 1 em, others 0.65 em), so the box cannot reach the right zone; text too long even at 20 px is truncated with an ellipsis.
A test asserts the box stays inside the safe area for long Latin, long Japanese and all-`W` credits, with default and enlarged safe zones.

The default font is the first installed of Yu Gothic Bold, Malgun Gothic Bold, MS Gothic and Arial Bold, so Japanese and Korean channel names render; set `render.font_file` to override.

## Windows details

ffmpeg's filter syntax uses `:` as a separator, so drive-letter paths break it (one of the Windows gotchas in the plan).
The credit text is passed with `textfile=` under a relative name (ffmpeg runs with `cwd` set to a temporary directory), and the font path is escaped (`C\:/Windows/Fonts/...`).

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
