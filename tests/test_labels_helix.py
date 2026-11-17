from __future__ import annotations

import csv
import json
import threading
import urllib.parse
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from peakreel.config import DetectConfig
from peakreel.detect.labels import (
    LabelError,
    evaluate,
    format_results,
    load_grid,
    read_labels,
    write_label_sheet,
)
from peakreel.detect.spikes import DetectionResult, Spike
from peakreel.twitch.helix import (
    Credentials,
    HelixClient,
    HelixError,
    Vod,
    VodLocator,
    parse_duration,
    vod_url,
)

T0 = 1_794_900_000.0


def spike(start: float, end: float, channel: str = "c", suppressed: str | None = None) -> Spike:
    return Spike(
        channel=channel,
        start_ts=T0 + start,
        end_ts=T0 + end,
        peak_ts=T0 + start,
        peak_count=40,
        messages=80,
        unique_chatters=60,
        baseline_median=6.0,
        baseline_mad=1.0,
        score=17.0,
        top_messages=(("あ", 39), ("lul", 3)),
        receive_delay_s=0.3,
        suppressed_by=suppressed,
    )


def set_labels(path: Path, labels: dict[str, str]) -> None:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        row["label"] = labels.get(row["event_id"], row["label"])
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def test_label_sheet_round_trip_and_merge_keeps_labels(tmp_path: Path) -> None:
    sheet = tmp_path / "labels.csv"
    first = [spike(0, 20), spike(100, 130), spike(0, 10, channel="d")]
    assert write_label_sheet(sheet, first, lambda s: (f"https://vod/{s.event_id}", "")) == 3
    raw = sheet.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")  # BOM, so Excel reads UTF-8
    assert "あ x39 | lul x3" in raw.decode("utf-8-sig")
    set_labels(sheet, {f"c-{int(T0)}": "good", f"c-{int(T0 + 100)}": "BAD"})

    # Re-run with shifted boundaries, one new event, and one old event gone.
    second = [spike(-10, 20), spike(300, 310)]
    assert write_label_sheet(sheet, second, lambda s: ("x", "")) == 1
    rows = read_labels(sheet)
    by_id = {r.event_id: r for r in rows}
    assert by_id[f"c-{int(T0)}"].label == "good"  # matched by overlap, label kept
    assert by_id[f"c-{int(T0 + 100)}"].label == "bad"  # no longer detected, still kept
    assert by_id[f"c-{int(T0 + 300)}"].fields["vod_url"] == "x"
    assert [r.start_ts for r in rows] == sorted(r.start_ts for r in rows)
    assert len(rows) == 4


def test_label_sheet_fills_missing_vod_links_later(tmp_path: Path) -> None:
    sheet = tmp_path / "labels.csv"
    write_label_sheet(sheet, [spike(0, 20)], lambda _s: ("", "no Twitch credentials"))
    write_label_sheet(sheet, [spike(0, 20)], lambda _s: ("https://vod/1", ""))
    (row,) = read_labels(sheet)
    assert row.fields["vod_url"] == "https://vod/1"


def test_read_labels_reports_every_bad_row(tmp_path: Path) -> None:
    sheet = tmp_path / "labels.csv"
    write_label_sheet(sheet, [spike(0, 20), spike(100, 120)])
    set_labels(sheet, {f"c-{int(T0)}": "maybe", f"c-{int(T0 + 100)}": "nope"})
    with pytest.raises(LabelError, match=r"line 2.*\n.*line 3"):
        read_labels(sheet)
    (tmp_path / "bad.csv").write_text("event_id,label\nx,good\n", encoding="utf-8")
    with pytest.raises(LabelError, match="missing column"):
        read_labels(tmp_path / "bad.csv")
    assert read_labels(tmp_path / "absent.csv") == []


def test_evaluate_precision_recall_and_rate(tmp_path: Path) -> None:
    sheet = tmp_path / "labels.csv"
    write_label_sheet(sheet, [spike(0, 20), spike(100, 120), spike(200, 220), spike(300, 320)])
    set_labels(
        sheet,
        {
            f"c-{int(T0)}": "good",
            f"c-{int(T0 + 100)}": "good",
            f"c-{int(T0 + 200)}": "bad",
            f"c-{int(T0 + 300)}": "unsure",
        },
    )
    labels = read_labels(sheet)
    result = DetectionResult(
        spikes=(
            spike(10, 30),  # good
            spike(200, 210),  # bad
            spike(300, 310),  # unsure -> unknown
            spike(500, 510),  # not in the sheet -> unknown
            spike(600, 610, suppressed="raid"),  # suppressed: not output
        ),
        valid_seconds={"c": 7200.0},
        active_seconds={"c": 3600.0},
    )
    score = evaluate("set", result, labels)
    assert (score.events, score.good, score.bad, score.unknown) == (4, 1, 1, 2)
    assert score.precision == 0.5
    assert score.recall == 0.5
    assert score.events_per_hour == 4.0
    table = format_results([score])
    assert "| set | 4 | 4.0 | 1 | 1 | 2 | 50% | 50% (1/2) |" in table


def test_load_grid(tmp_path: Path) -> None:
    grid = tmp_path / "grid.yaml"
    grid.write_text("sets:\n  - name: k3\n    k: 3\n  - name: f\n    floor: 20\n", encoding="utf-8")
    sets = load_grid(grid, DetectConfig())
    assert [(name, p.k, p.floor) for name, p in sets] == [("k3", 3.0, 10), ("f", 4.0, 20)]
    for content, match in [
        ("sets:\n  - k: 3\n", "needs a `name`"),
        ("sets:\n  - name: x\n    kk: 3\n", "set 'x'"),
        ("- a\n", "`sets` list"),
    ]:
        grid.write_text(content, encoding="utf-8")
        with pytest.raises(LabelError, match=match):
            load_grid(grid, DetectConfig())


# -- Helix ---------------------------------------------------------------------------


@dataclass
class FakeTwitchApi:
    """Records requests; serves tokens, users and videos."""

    requests: list[str] = field(default_factory=list[str])
    tokens_issued: int = 0
    reject_token: str | None = None  # API answers 401 for this token once
    videos: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    streams: list[dict[str, object]] = field(default_factory=list[dict[str, object]])


@pytest.fixture
def api() -> Iterator[tuple[FakeTwitchApi, str]]:
    state = FakeTwitchApi()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def _send(self, code: int, body: object) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self) -> None:
            length = int(self.headers["Content-Length"])
            form = urllib.parse.parse_qs(self.rfile.read(length).decode())
            state.requests.append(f"POST {self.path}")
            if form.get("client_secret") != ["secret"]:
                self._send(400, {"message": "invalid client secret"})
                return
            state.tokens_issued += 1
            self._send(200, {"access_token": f"tok{state.tokens_issued}", "expires_in": 5000})

        def do_GET(self) -> None:
            state.requests.append(f"GET {self.path}")
            token = self.headers["Authorization"].removeprefix("Bearer ")
            if token == state.reject_token:
                state.reject_token = None
                self._send(401, {"message": "invalid oauth token"})
                return
            url = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(url.query)
            if url.path == "/helix/users":
                users = {"alpha": "42"}
                login = query["login"][0]
                self._send(200, {"data": [{"id": users[login]}] if login in users else []})
            elif url.path == "/helix/games":
                found = query["name"][0] == "Grand Theft Auto VI"
                self._send(200, {"data": [{"id": "516575"}] if found else []})
            elif url.path == "/helix/streams" and "game_id" in query:
                languages = set(query.get("language", []))
                rows = [
                    s for s in state.streams
                    if s.get("game_id") == query["game_id"][0]
                    and (not languages or s["language"] in languages)
                ]  # fmt: skip
                start = int(query.get("after", ["0"])[0])
                page = rows[start : start + 2]  # tiny pages, to exercise pagination
                cursor = str(start + 2) if start + 2 < len(rows) else None
                self._send(200, {"data": page, "pagination": {"cursor": cursor} if cursor else {}})
            elif url.path == "/helix/streams":
                logins = set(query["user_login"])
                rows = [s for s in state.streams if s["user_login"] in logins]
                self._send(200, {"data": rows})
            elif url.path == "/helix/videos":
                assert query["type"] == ["archive"]
                self._send(200, {"data": state.videos})
            else:
                self._send(404, {})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield state, base
    server.shutdown()
    server.server_close()


def client(base: str, secret: str = "secret") -> HelixClient:
    return HelixClient(
        Credentials("cid", secret), api_base=f"{base}/helix", auth_base=f"{base}/oauth2"
    )


def test_helix_token_reuse_and_401_refresh(api: tuple[FakeTwitchApi, str]) -> None:
    state, base = api
    helix = client(base)
    assert helix.user_id("alpha") == "42"
    assert helix.user_id("nobody") is None
    assert state.tokens_issued == 1
    state.reject_token = "tok1"
    assert helix.user_id("alpha") == "42"  # 401 -> new token -> retry
    assert state.tokens_issued == 2


def test_helix_bad_credentials(api: tuple[FakeTwitchApi, str]) -> None:
    _, base = api
    with pytest.raises(HelixError, match="HTTP 400"):
        client(base, secret="wrong").token()
    with pytest.raises(HelixError, match="cannot reach Twitch"):
        client("http://127.0.0.1:9").token()


def test_vod_locator_uses_cache_and_refreshes_for_new_vods(
    api: tuple[FakeTwitchApi, str], tmp_path: Path
) -> None:
    state, base = api
    state.videos = [{"id": "v1", "created_at": "2026-11-17T07:00:00Z", "duration": "1h0m0s"}]
    now = [1_000_000.0]
    locator = VodLocator(client(base), tmp_path, refresh_after_s=600, clock=lambda: now[0])
    created = 1_794_898_800.0  # 2026-11-17T07:00:00Z

    vod, reason = locator.locate("alpha", created + 120)
    assert vod == Vod("v1", created, 3600)
    assert vod is not None
    assert reason == ""
    assert vod_url(vod, 120 - 15) == "https://www.twitch.tv/videos/v1?t=0h01m45s"
    calls = len(state.requests)
    locator.locate("alpha", created + 300)  # served from cache
    assert len(state.requests) == calls

    # A moment after the cached VOD's end: still fresh, so one refetch is not done yet.
    vod, reason = locator.locate("alpha", created + 4000)
    assert vod is None
    assert "no archived VOD" in reason
    # The broadcast grew; once the cache is stale it is refetched and found.
    state.videos = [{"id": "v1", "created_at": "2026-11-17T07:00:00Z", "duration": "2h0m0s"}]
    now[0] += 601
    vod, _ = locator.locate("alpha", created + 4000)
    assert vod is not None
    assert vod.duration_s == 7200
    assert locator.locate("nobody", created) == (None, "no Twitch user named 'nobody'")


@pytest.mark.parametrize(
    ("value", "seconds"), [("3h8m33s", 11313), ("45s", 45), ("2m", 120), ("1h", 3600)]
)
def test_parse_duration(value: str, seconds: int) -> None:
    assert parse_duration(value) == seconds


@pytest.mark.parametrize("value", ["", "abc", "1d"])
def test_parse_duration_rejects(value: str) -> None:
    with pytest.raises(ValueError, match="not a Twitch duration"):
        parse_duration(value)


def test_credentials_repr_hides_secret() -> None:
    assert "hunter2" not in repr(Credentials("cid", "hunter2"))


def test_live_streams_batches_logins_and_parses(api: tuple[FakeTwitchApi, str]) -> None:
    state, base = api
    state.streams = [
        {"user_login": "alpha", "type": "live", "game_name": "Grand Theft Auto VI",
         "title": "launch day", "viewer_count": 51234, "started_at": "2026-11-19T12:00:00Z",
         "language": "pt"},
        {"user_login": "c150", "type": "live", "game_name": "", "title": "",
         "viewer_count": 3, "started_at": "2026-11-19T13:00:00Z", "language": "en"},
    ]  # fmt: skip
    logins = ["Alpha", "beta"] + [f"c{i}" for i in range(200)]
    live = client(base).live_streams(logins)
    assert sorted(live) == ["alpha", "c150"]
    alpha = live["alpha"]
    assert (alpha.game, alpha.viewers, alpha.language) == ("Grand Theft Auto VI", 51234, "pt")
    assert alpha.started_ts == 1_795_089_600.0
    assert alpha.to_json()["title"] == "launch day"
    stream_calls = [r for r in state.requests if r.startswith("GET /helix/streams")]
    assert len(stream_calls) == 3  # 202 logins, 100 per call


def test_category_and_top_streams(api: tuple[FakeTwitchApi, str]) -> None:
    state, base = api

    def stream(login: str, viewers: int, language: str) -> dict[str, object]:
        return {"user_login": login, "type": "live", "game_id": "516575",
                "game_name": "Grand Theft Auto VI", "title": "", "viewer_count": viewers,
                "started_at": "2026-11-19T12:00:00Z", "language": language}  # fmt: skip

    state.streams = [
        stream("small", 10, "en"), stream("big", 90000, "en"), stream("mid", 5000, "es"),
        stream("other_lang", 70000, "de"), stream("top_pt", 30000, "pt"),
    ]  # fmt: skip
    helix = client(base)
    assert helix.game_id("Grand Theft Auto VI") == "516575"
    assert helix.game_id("Grand Theft Auto 6") is None
    everything = helix.top_streams("516575")
    assert [s.login for s in everything] == ["big", "other_lang", "top_pt", "mid", "small"]
    filtered = helix.top_streams("516575", ["en", "es", "pt"], limit=3)
    assert [s.login for s in filtered] == ["big", "top_pt", "mid"]
    pages = [r for r in state.requests if r.startswith("GET /helix/streams?game_id")]
    assert len(pages) >= 3  # followed the cursor
