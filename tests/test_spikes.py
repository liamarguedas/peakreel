from __future__ import annotations

from collections.abc import Iterator

import pytest

from peakreel.chat.base import ChatEvent, ChatMessage, ConnectionEvent
from peakreel.config import DetectConfig, DetectParams
from peakreel.detect.spikes import Spike, SpikeDetector, detect_spikes, normalize_text

T0 = 1_794_900_000.0  # a multiple of 10
P = DetectParams(floor=10, k=4.0, min_unique=5, warmup_buckets=12, cooldown_s=60)


def bucket_messages(
    index: int,
    count: int,
    *,
    users: int | None = None,
    text: str = "hi",
    channel: str = "c",
) -> Iterator[ChatMessage]:
    """`count` messages spread evenly inside bucket `index` from `users` distinct users."""
    users = count if users is None else users
    for i in range(count):
        ts = T0 + index * 10 + (i + 0.5) * 10 / max(count, 1)
        yield ChatMessage(
            ts=ts,
            server_ts=ts - 0.25,
            channel=channel,
            user=f"u{i % max(users, 1)}",
            text=text if i % 2 else text.upper(),
        )


def traffic(levels: list[int], *, users: int | None = None, text: str = "hi") -> list[ChatEvent]:
    events: list[ChatEvent] = [ConnectionEvent(ts=T0, channel="c", event="joined")]
    for index, count in enumerate(levels):
        events.extend(bucket_messages(index, count, users=users, text=text))
    return events


def run(events: list[ChatEvent], params: DetectParams = P) -> list[Spike]:
    return list(detect_spikes(events, params).spikes)


def baseline(n: int = 20) -> list[int]:
    return [6, 5, 7, 6, 5, 6, 7, 5, 6, 6] * (n // 10)


def test_burst_is_detected_with_explainable_stats() -> None:
    levels = [*baseline(20), 40, 45, 6, 6, 6, 6]
    (spike,) = run(traffic(levels, text="LUL LUL"))
    assert spike.start_ts == T0 + 200
    assert spike.end_ts == T0 + 220
    assert spike.peak_ts == T0 + 210
    assert spike.peak_count == 45
    assert spike.messages == 85
    assert spike.baseline_median == 6
    assert spike.baseline_mad == 0.5
    assert spike.score == round((45 - 6) / 1.5, 2)
    assert spike.top_messages == (("lul", 85),)
    assert spike.receive_delay_s == 0.25
    assert spike.suppressed_by is None
    assert spike.event_id == f"c-{int(T0 + 200)}"


def test_slow_ramp_does_not_trigger() -> None:
    ramp = [6 + i // 3 for i in range(90)]  # chat doubles over 15 minutes
    assert run(traffic(ramp)) == []


def test_needs_warmup_floor_and_unique_chatters() -> None:
    assert run(traffic([6] * 5 + [60])) == []  # baseline not warm yet
    assert run(traffic([1] * 20 + [9])) == []  # above median + k*MAD but under the floor
    assert run(traffic([*baseline(), 40], users=4)) == []  # 4 accounts are not a crowd


def test_hot_buckets_within_merge_gap_form_one_event() -> None:
    levels = [*baseline(), 40, 6, 40, 6, 6, 6]
    (spike,) = run(traffic(levels))
    assert spike.duration_s == 30
    assert spike.messages == 86  # the cold bucket in between is part of the event
    two_gaps = [*baseline(), 40, 6, 6, 40, 6, 6]
    assert len(run(traffic(two_gaps), DetectParams(**{**P.model_dump(), "cooldown_s": 0}))) == 2


def test_cooldown_blocks_retrigger() -> None:
    levels = [*baseline(), 40, 6, 6, 6, 6, 40, 6, 6, 6, 6, 6, 6, 6, 40, 6, 6]
    spikes = run(traffic(levels))
    # second burst is 40 s after the first ended (inside 60 s), third is 120 s after
    assert [s.start_ts - T0 for s in spikes] == [200, 330]


def test_max_event_length_splits_long_hype() -> None:
    params = DetectParams(**{**P.model_dump(), "max_event_s": 30, "cooldown_s": 0})
    spikes = run(traffic([*baseline(), *[40] * 7, 6, 6]), params)
    assert [s.duration_s for s in spikes] == [30, 30, 10]


def test_burst_during_downtime_never_triggers_and_is_not_zero() -> None:
    events = traffic([*baseline(), 6, 6, 6])
    # Disconnected at bucket 23, rejoined 25 s later.
    down = T0 + 230
    events.append(
        ConnectionEvent(
            ts=down + 4, channel="c", event="disconnected", reason="x", last_alive_ts=down
        )
    )
    events.append(ConnectionEvent(ts=down + 25, channel="c", event="joined"))
    for index, count in enumerate([40, 6, 6, 6], start=26):
        events.extend(bucket_messages(index, count))
    result = detect_spikes(events, P)
    # Bucket 26 starts after the rejoin and is hot: detected. Buckets 23-25 are invalid.
    assert [s.start_ts - T0 for s in result.spikes] == [260]
    # Buckets 0-22 and 26-28 are valid; bucket 29 is still open when the input ends.
    assert result.valid_seconds["c"] == 10 * (23 + 3)


def test_message_burst_inside_downtime_bucket_is_ignored() -> None:
    events = traffic(baseline())
    events.append(
        ConnectionEvent(
            ts=T0 + 205, channel="c", event="disconnected", reason="x", last_alive_ts=T0 + 202
        )
    )
    events.extend(bucket_messages(20, 80))  # arrives in a bucket that overlaps the outage
    events.append(ConnectionEvent(ts=T0 + 209, channel="c", event="joined"))
    events.extend(bucket_messages(21, 6))
    events.extend(bucket_messages(22, 6))
    assert run(events) == []


def test_long_downtime_resets_baseline() -> None:
    events = traffic(baseline())
    events.append(
        ConnectionEvent(ts=T0 + 200, channel="c", event="stopped", last_alive_ts=T0 + 200)
    )
    events.append(ConnectionEvent(ts=T0 + 1000, channel="c", event="joined"))
    for index in range(100, 106):  # a burst right after restart: baseline is empty again
        events.extend(bucket_messages(index, 60 if index == 103 else 6))
    assert run(events) == []


def test_log_starting_without_join_warms_up_from_first_message() -> None:
    events = traffic([*baseline(), 40, 6, 6])[1:]  # no `joined` record
    assert len(run(events)) == 1


@pytest.mark.parametrize(
    ("notice", "count", "expected"),
    [
        ("raid", 1, "raid"),
        ("subgift", 5, "sub_train"),
        ("subgift", 4, None),
        ("announcement", 9, None),
    ],
)
def test_raid_and_sub_train_suppression(notice: str, count: int, expected: str | None) -> None:
    events = traffic(baseline())
    for i in range(count):
        events.append(
            ChatMessage(
                ts=T0 + 195 + i * 0.1,
                channel="c",
                user=f"n{i}",
                text="",
                type="usernotice",
                tags={"msg-id": notice},
            )
        )
    events.extend(bucket_messages(20, 40))
    events.extend(bucket_messages(21, 6))
    events.extend(bucket_messages(22, 6))
    (spike,) = run(events)
    assert spike.suppressed_by == expected


def test_copy_paste_spam_is_suppressed() -> None:
    events = traffic([*baseline(), 6, 6])
    events.extend(bucket_messages(22, 60, users=6, text="buy followers at example dot com"))
    events.extend(bucket_messages(23, 6))
    events.extend(bucket_messages(24, 6))
    (spike,) = run(events)
    assert spike.suppressed_by == "spam"
    assert spike.unique_chatters == 6


def test_filtered_messages_do_not_count() -> None:
    events = traffic(baseline())
    for message in bucket_messages(20, 60):
        events.append(
            ChatMessage(ts=message.ts, channel="c", user="nightbot", text="x", filtered="bot")
        )
    events.extend(bucket_messages(21, 6))
    assert run(events) == []


def test_channels_are_independent_and_output_is_deterministic() -> None:
    a = traffic([*baseline(), 40, 6])
    b: list[ChatEvent] = [ConnectionEvent(ts=T0, channel="d", event="joined")]
    for index, count in enumerate([*baseline(), 6, 40, 6]):
        b.extend(bucket_messages(index, count, channel="d"))
    merged = sorted(a + b, key=lambda e: (e.ts, e.channel))
    first = detect_spikes(merged, P)
    assert [s.event_id for s in first.spikes] == [f"c-{int(T0 + 200)}", f"d-{int(T0 + 210)}"]
    assert detect_spikes(merged, P) == first


def test_feed_rejects_other_channel_and_tolerates_clock_going_back() -> None:
    detector = SpikeDetector("c", P)
    with pytest.raises(ValueError, match="fed to detector"):
        detector.feed(ChatMessage(ts=T0, channel="x", user="u", text="t"))
    detector.feed(ConnectionEvent(ts=T0, channel="c", event="joined"))
    detector.feed(ChatMessage(ts=T0 + 100, channel="c", user="u", text="t"))
    detector.feed(ChatMessage(ts=T0 + 50, channel="c", user="u", text="t"))  # clock stepped back
    assert detector.advance(T0 + 120) == []


def test_normalize_text() -> None:
    assert normalize_text("KEKW KEKW kekw") == "kekw"
    assert normalize_text("a  b   a") == "a b a"
    assert len(normalize_text("x" * 200)) == 80


def test_candidate_params() -> None:
    config = DetectConfig(candidate={"k": 2.0, "floor": 5})
    assert config.candidate_params().k == 2.0
    assert config.params().k == 4.0
    assert config.params({"min_unique": 2}).min_unique == 2
    with pytest.raises(ValueError, match="unknown detector parameter"):
        DetectConfig(candidate={"kk": 1.0})
