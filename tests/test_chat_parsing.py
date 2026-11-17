from __future__ import annotations

import pytest

from peakreel.chat.base import (
    ChatMessage,
    ConnectionEvent,
    RecordError,
    event_from_record,
    event_to_record,
)
from peakreel.chat.irc import IrcParseError, parse_line, unescape_tag_value
from peakreel.chat.twitch import message_from_irc
from peakreel.config import ChatConfig
from peakreel.detect.filters import ChatFilter

# Shapes taken from real Twitch IRC traffic (values anonymised).
PRIVMSG = (
    "@badge-info=subscriber/18;badges=subscriber/18,premium/1;color=#FF0000;"
    "display-name=Viewer_One;emotes=;first-msg=0;flags=;id=2bdd96e8-db22-4c8e-accc-19f6e7c4e0da;"
    "mod=0;returning-chatter=0;room-id=71092938;subscriber=1;tmi-sent-ts=1794900415932;"
    "turbo=0;user-id=136857810;user-type= "
    ":viewer_one!viewer_one@viewer_one.tmi.twitch.tv PRIVMSG #xqc :@someone LETS GO"
)
ACTION = ":viewer!viewer@viewer.tmi.twitch.tv PRIVMSG #chan :\x01ACTION dances\x01"
RAID = (
    "@badges=;display-name=Raider;id=r1;login=raider;msg-id=raid;"
    "msg-param-viewerCount=1234;system-msg=1234\\sraiders\\sfrom\\sRaider\\shave\\sjoined!;"
    "tmi-sent-ts=1794900000000;user-id=9 :tmi.twitch.tv USERNOTICE #chan"
)
RESUB = (
    "@display-name=Sub;login=sub;msg-id=resub;msg-param-cumulative-months=6;"
    "tmi-sent-ts=1794900001000 :tmi.twitch.tv USERNOTICE #chan :still here"
)


def test_parse_privmsg() -> None:
    msg = parse_line(PRIVMSG)
    assert msg.command == "PRIVMSG"
    assert msg.params == ("#xqc", "@someone LETS GO")
    assert msg.nick == "viewer_one"
    assert msg.tags["display-name"] == "Viewer_One"
    assert msg.tags["user-type"] == ""
    assert msg.tags["emotes"] == ""


@pytest.mark.parametrize(
    ("line", "command", "params", "prefix"),
    [
        ("PING :tmi.twitch.tv", "PING", ("tmi.twitch.tv",), None),
        (":tmi.twitch.tv RECONNECT", "RECONNECT", (), "tmi.twitch.tv"),
        (
            ":tmi.twitch.tv 001 justinfan1 :Welcome, GLHF!",
            "001",
            ("justinfan1", "Welcome, GLHF!"),
            "tmi.twitch.tv",
        ),
        (":a!a@a.tmi.twitch.tv JOIN #chan", "JOIN", ("#chan",), "a!a@a.tmi.twitch.tv"),
        ("privmsg #c :lower case command", "PRIVMSG", ("#c", "lower case command"), None),
        ("CMD a b :trailing with :colon", "CMD", ("a", "b", "trailing with :colon"), None),
        ("CMD a :", "CMD", ("a", ""), None),
        ("PING :x\r\n", "PING", ("x",), None),
    ],
)
def test_parse_shapes(line: str, command: str, params: tuple[str, ...], prefix: str | None) -> None:
    msg = parse_line(line)
    assert (msg.command, msg.params, msg.prefix) == (command, params, prefix)


@pytest.mark.parametrize("line", ["", "   ", "@a=b", ":prefix.only", "@a=b :prefix"])
def test_parse_rejects_lines_without_command(line: str) -> None:
    with pytest.raises(IrcParseError):
        parse_line(line)


@pytest.mark.parametrize(
    ("raw", "value"),
    [
        ("plain", "plain"),
        (r"a\sb", "a b"),
        (r"a\:b", "a;b"),
        (r"a\\b", "a\\b"),
        (r"a\r\nb", "a\r\nb"),
        (r"unknown\xescape", "unknownxescape"),
        ("trailing\\", "trailing"),
    ],
)
def test_unescape_tag_value(raw: str, value: str) -> None:
    assert unescape_tag_value(raw) == value


def test_message_from_privmsg() -> None:
    message = message_from_irc(parse_line(PRIVMSG), ts=1794900416.5)
    assert message is not None
    assert message.channel == "xqc"
    assert message.user == "viewer_one"
    assert message.display_name == "Viewer_One"
    assert message.text == "@someone LETS GO"
    assert message.type == "privmsg"
    assert message.server_ts == 1794900415.932
    assert message.user_id == "136857810"
    assert message.msg_id == "2bdd96e8-db22-4c8e-accc-19f6e7c4e0da"
    # Only kept, non-empty tags.
    assert dict(message.tags) == {
        "badges": "subscriber/18,premium/1",
        "first-msg": "0",
        "mod": "0",
        "returning-chatter": "0",
        "subscriber": "1",
    }


def test_message_from_action_strips_ctcp() -> None:
    message = message_from_irc(parse_line(ACTION), ts=1.0)
    assert message is not None
    assert message.text == "dances"
    assert message.server_ts is None


def test_message_from_usernotice() -> None:
    raid = message_from_irc(parse_line(RAID), ts=1.0)
    assert raid is not None
    assert (raid.type, raid.user, raid.text) == ("usernotice", "raider", "")
    assert raid.tags["msg-id"] == "raid"
    assert raid.tags["msg-param-viewerCount"] == "1234"
    assert raid.tags["system-msg"] == "1234 raiders from Raider have joined!"
    resub = message_from_irc(parse_line(RESUB), ts=1.0)
    assert resub is not None
    assert resub.text == "still here"


@pytest.mark.parametrize(
    "line",
    ["PING :x", ":tmi.twitch.tv USERNOTICE #chan :no login tag", ":tmi.twitch.tv PRIVMSG"],
)
def test_message_from_irc_ignores_other_lines(line: str) -> None:
    assert message_from_irc(parse_line(line), ts=1.0) is None


def test_filter_rules() -> None:
    chat_filter = ChatFilter.from_config(ChatConfig(bots=frozenset(["NightBot"])))

    def classify(user: str, text: str, kind: str = "privmsg") -> str | None:
        message = ChatMessage(ts=0.0, channel="c", user=user, text=text, type=kind)  # type: ignore[arg-type]
        return chat_filter.classify(message)

    assert classify("nightbot", "Follow the stream") == "bot"
    assert classify("nightbot", "!cmd") == "bot"
    assert classify("viewer", "!drops") == "command"
    assert classify("viewer", "   !drops") == "command"
    assert classify("viewer", "wow!") is None
    assert classify("viewer", "") is None
    assert classify("viewer", "!raid", kind="usernotice") is None
    assert (
        ChatFilter(command_prefixes=()).classify(
            ChatMessage(ts=0.0, channel="c", user="v", text="!x")
        )
        is None
    )


def test_record_round_trip() -> None:
    events = [
        ChatMessage(
            ts=1.5,
            channel="c",
            user="u",
            text="hi ünïcode 😀",
            server_ts=1.25,
            display_name="U",
            user_id="1",
            msg_id="m",
            filtered="command",
            tags={"z": "1", "a": "2"},
        ),
        ChatMessage(ts=2.0, channel="c", user="raider", text="", type="usernotice"),
        ConnectionEvent(ts=3.0, channel="c", event="joined"),
        ConnectionEvent(ts=4.0, channel="c", event="disconnected", reason="x", last_alive_ts=3.5),
    ]
    for event in events:
        record = event_to_record(event)
        assert event_from_record(record) == event
    assert list(event_to_record(events[0])["tags"]) == ["a", "z"]


@pytest.mark.parametrize(
    "record",
    [
        {"kind": "other", "ts": 1, "channel": "c"},
        {"kind": "msg", "channel": "c", "user": "u", "text": "t", "type": "privmsg"},
        {"kind": "msg", "ts": "1", "channel": "c", "user": "u", "text": "t", "type": "privmsg"},
        {"kind": "msg", "ts": True, "channel": "c", "user": "u", "text": "t", "type": "privmsg"},
        {"kind": "msg", "ts": 1, "channel": "c", "user": "u", "text": "t", "type": "whisper"},
        {
            "kind": "msg",
            "ts": 1,
            "channel": "c",
            "user": "u",
            "text": "t",
            "type": "privmsg",
            "tags": [],
        },
        {"kind": "conn", "ts": 1, "channel": "c", "event": "exploded"},
        {"kind": "conn", "ts": 1, "event": "joined"},
    ],
)
def test_bad_records_are_rejected(record: dict[str, object]) -> None:
    with pytest.raises(RecordError):
        event_from_record(record)
