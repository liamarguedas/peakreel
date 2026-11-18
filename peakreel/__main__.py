"""Allow ``python -m peakreel``.

Long-running processes should be started this way rather than through the
``peakreel`` console script: on Windows a running ``peakreel.exe`` is locked, which
blocks ``uv sync`` from reinstalling the project. ``python.exe`` is never replaced
by a sync.
"""

from peakreel.cli import app

app(prog_name="peakreel")
