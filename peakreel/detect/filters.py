"""Chat message filters.

Filters never drop a message. They return a reason, which the recorder stores in the
message's ``filtered`` field, and the spike detector skips flagged messages. Because
the raw message stays in the log, filter rules can be changed later and the logs
re-classified during replay.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from peakreel.chat.base import ChatMessage
from peakreel.config import ChatConfig

__all__ = ["FILTER_BOT", "FILTER_COMMAND", "ChatFilter", "excitement", "shared_reaction"]

FILTER_BOT = "bot"
FILTER_COMMAND = "command"


@dataclass(frozen=True, slots=True)
class ChatFilter:
    """Classify messages that should not count towards spike detection.

    Parameters
    ----------
    bots : frozenset of str
        Lower-case logins of known bots.
    command_prefixes : tuple of str
        Prefixes that mark a chat command, typically ``("!",)``.

    Examples
    --------
    >>> chat_filter = ChatFilter(bots=frozenset({"nightbot"}), command_prefixes=("!",))
    >>> chat_filter.classify(ChatMessage(ts=0.0, channel="c", user="nightbot", text="hi"))
    'bot'
    >>> chat_filter.classify(ChatMessage(ts=0.0, channel="c", user="viewer", text=" !drops"))
    'command'
    >>> chat_filter.classify(ChatMessage(ts=0.0, channel="c", user="viewer", text="wow!")) is None
    True
    """

    bots: frozenset[str] = frozenset()
    command_prefixes: tuple[str, ...] = ("!",)

    @classmethod
    def from_config(cls, chat: ChatConfig) -> ChatFilter:
        """Build a filter from the ``chat:`` section of the configuration.

        Parameters
        ----------
        chat : ChatConfig

        Returns
        -------
        ChatFilter
        """
        return cls(bots=chat.bots, command_prefixes=chat.command_prefixes)

    def classify(self, message: ChatMessage) -> str | None:
        """Return why ``message`` is excluded from detection, or ``None``.

        Parameters
        ----------
        message : ChatMessage

        Returns
        -------
        str or None
            ``"bot"`` if the sender is a known bot, ``"command"`` if the text
            (ignoring leading whitespace) starts with a command prefix, otherwise
            ``None``. The bot check takes precedence.

        Notes
        -----
        Platform notices (``type == "usernotice"``, for example raids and subs) are
        never flagged here. They are real audience activity, and the detector in M2
        handles raid and sub-train spikes with its own filters.
        """
        if message.type != "privmsg":
            return None
        if message.user.lower() in self.bots:
            return FILTER_BOT
        text = message.text.lstrip()
        if self.command_prefixes and text.startswith(self.command_prefixes):
            return FILTER_COMMAND
        return None


def excitement(top_messages: Sequence[Sequence[Any]], words: Sequence[str]) -> float:
    """Return the share of chat messages that contain an excited word.

    Parameters
    ----------
    top_messages : sequence of (text, count)
        The spike's most repeated (normalised) messages.
    words : sequence of str
        Regexes, each matched against a whole word, case-insensitively.

    Returns
    -------
    float
        Weighted by count; ``0.0`` without messages.

    Examples
    --------
    >>> words = ["lo+l+", "no{3,}", "gg+", "kekw?"]
    >>> excitement([["LOOOL", 6], ["nooooo", 3], ["yes", 1]], words)
    0.9
    >>> excitement([["yes", 17], ["no", 9]], words)
    0.0
    """
    pattern = re.compile("|".join(f"(?:{word})" for word in words), re.IGNORECASE)
    total = excited = 0
    for text, count in top_messages:
        total += int(count)
        if any(pattern.fullmatch(token) for token in str(text).split()):
            excited += int(count)
    return excited / total if total else 0.0


def shared_reaction(top_messages: Sequence[Sequence[Any]], words: Sequence[str]) -> int:
    """Return how many messages share the biggest reaction.

    Messages with the same excited word count as one reaction however they are spelled
    (``jajaja``, ``jajajaja`` and ``jajaj`` all match ``(ja){2,}``); other messages
    count with their exact copies only. A small number means everyone typed something
    different: chatting, not a moment.

    Parameters
    ----------
    top_messages : sequence of (text, count)
        The spike's most repeated (normalised) messages.
    words : sequence of str
        Regexes of excited words, as for :func:`excitement`.

    Returns
    -------
    int
        Messages in the biggest group; ``0`` without messages.

    Examples
    --------
    >>> words = ["a?(?:[jh]+a+){2,}[jh]*", "w{3,}", "lo+l+"]
    >>> shared_reaction([["jajajaa", 1], ["jajajaja", 1], ["jajaj", 1], ["gg", 2]], words)
    3
    >>> shared_reaction([["coge dinero", 1], ["dinero", 1], ["dios mio...", 1]], words)
    1
    """
    patterns = [re.compile(word, re.IGNORECASE) for word in words]
    groups: dict[str, int] = {}
    for text, count in top_messages:
        key = f"text:{text}"
        for token in str(text).lower().split():
            match = next((i for i, p in enumerate(patterns) if p.fullmatch(token)), None)
            if match is not None:
                key = f"word:{match}"
                break
        groups[key] = groups.get(key, 0) + int(count)
    return max(groups.values(), default=0)
