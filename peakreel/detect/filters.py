"""Chat message filters.

Filters never drop a message. They return a reason, which the recorder stores in the
message's ``filtered`` field, and the spike detector skips flagged messages. Because
the raw message stays in the log, filter rules can be changed later and the logs
re-classified during replay.
"""

from __future__ import annotations

from dataclasses import dataclass

from peakreel.chat.base import ChatMessage
from peakreel.config import ChatConfig

__all__ = ["FILTER_BOT", "FILTER_COMMAND", "ChatFilter"]

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
