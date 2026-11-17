"""Chat ingest: live sources, JSONL logging, replay and liveness analysis.

- :mod:`peakreel.chat.base` defines the platform-neutral event types.
- :mod:`peakreel.chat.irc` parses IRCv3 lines.
- :mod:`peakreel.chat.twitch` is the anonymous Twitch chat client.
- :mod:`peakreel.chat.writer` appends events to per-channel, per-day JSONL files.
- :mod:`peakreel.chat.replay` reads those files back deterministically.
- :mod:`peakreel.chat.gaps` measures ingest downtime from the recorded events.
"""
