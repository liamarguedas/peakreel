"""Peakreel: detect Twitch chat spikes and turn them into captioned vertical clips.

The package is organised by pipeline stage:

- :mod:`peakreel.config` loads and validates ``config.yaml``.
- :mod:`peakreel.db` owns the SQLite schema.
- :mod:`peakreel.chat` ingests live chat and replays recorded logs.
- :mod:`peakreel.detect` holds chat filters (and, from M2, the spike detector).
- :mod:`peakreel.cli` is the ``peakreel`` command line entry point.
"""

__version__ = "0.1.0"
