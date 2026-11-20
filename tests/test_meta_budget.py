from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from peakreel.publish import meta
from peakreel.publish.publishers import PublishError


@pytest.fixture
def limited() -> Iterator[tuple[str, list[str]]]:
    """A Graph API that refuses with (#4) and says access returns in 17 minutes."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            seen.append(self.path)
            body = json.dumps(
                {"error": {"message": "Application request limit reached", "code": 4}}
            )
            self.send_response(400)
            usage = {"1": [{"type": "instagram", "call_count": 100, "total_cputime": 4,
                            "total_time": 9, "estimated_time_to_regain_access": 17}]}  # fmt: skip
            self.send_header("X-Business-Use-Case-Usage", json.dumps(usage))
            self.end_headers()
            self.wfile.write(body.encode())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", seen
    server.shutdown()


def test_a_request_limit_pauses_every_request_for_as_long_as_meta_says(
    limited: tuple[str, list[str]],
) -> None:
    base, seen = limited
    api = meta.MetaApi(meta.MetaApp("1", "s"), graph_url=base)
    with pytest.raises(PublishError) as first:
        api.instagram_status("C1", "t")
    assert 16 * 60 < (first.value.retry_after_s or 0) <= 17 * 60  # Meta's 17 minutes
    assert "paused until" in str(first.value) and meta.BUDGET.used_pct == 100
    # Every other request now waits without being sent (another account too).
    with pytest.raises(PublishError, match="paused until") as second:
        api.instagram_publish("IG2", "C2", "other-token")
    assert second.value.retry_after_s and len(seen) == 1
    assert meta.BUDGET.busy() and meta.BUDGET.to_json()["paused_until"]
