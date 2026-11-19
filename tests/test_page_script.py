from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

PAGE = Path(__file__).parent.parent / "peakreel" / "review" / "static" / "index.html"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_review_page_script_parses(tmp_path: Path) -> None:
    # A syntax error anywhere leaves the whole page stuck on "Loading…" (seen: PR #34).
    script = re.search(r"<script>(.*)</script>", PAGE.read_text(encoding="utf-8"), re.S)
    assert script is not None
    file = tmp_path / "page.js"
    file.write_text(script[1], encoding="utf-8")
    node = shutil.which("node")
    assert node is not None
    result = subprocess.run([node, "--check", str(file)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
