"""Live stream recording into a rolling buffer of broadcast-timestamped segments.

- :mod:`peakreel.record.segments` defines the on-disk segment index.
- :mod:`peakreel.record.recorder` captures Twitch HLS segments through streamlink.
- :mod:`peakreel.record.buffer` prunes the buffer and extracts time windows.
"""
