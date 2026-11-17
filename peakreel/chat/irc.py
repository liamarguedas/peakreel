"""IRCv3 line parser.

Pure functions with no I/O, so the wire format can be tested exhaustively.
Only what Twitch chat needs is implemented: message tags, prefix, command and
parameters. See https://ircv3.net/specs/extensions/message-tags.html.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = ["IrcMessage", "IrcParseError", "parse_line", "unescape_tag_value"]

_TAG_ESCAPES = {":": ";", "s": " ", "\\": "\\", "r": "\r", "n": "\n"}


class IrcParseError(ValueError):
    """Raised when a line is not a well-formed IRC message."""


@dataclass(frozen=True, slots=True)
class IrcMessage:
    """A parsed IRC line.

    Attributes
    ----------
    command : str
        Command or numeric, upper case (``PRIVMSG``, ``PING``, ``001``, ...).
    params : tuple of str
        Parameters, with the trailing parameter (the part after `` :``) last.
    prefix : str or None
        Message source, for example ``nick!user@host`` or ``tmi.twitch.tv``.
    tags : dict of str to str
        IRCv3 tags with values unescaped. Tags without a value map to ``""``.
    """

    command: str
    params: tuple[str, ...] = ()
    prefix: str | None = None
    tags: dict[str, str] = field(default_factory=dict[str, str])

    @property
    def nick(self) -> str | None:
        """Nickname part of the prefix, or ``None`` when there is no prefix."""
        if self.prefix is None:
            return None
        return self.prefix.split("!", 1)[0]


def unescape_tag_value(value: str) -> str:
    r"""Unescape an IRCv3 tag value.

    Parameters
    ----------
    value : str
        Raw value as it appears on the wire.

    Returns
    -------
    str
        The value with ``\:`` -> ``;``, ``\s`` -> space, ``\\`` -> ``\``,
        ``\r`` -> CR and ``\n`` -> LF. An unknown escape drops the backslash, and a
        trailing lone backslash is removed, as the specification requires.

    Examples
    --------
    >>> unescape_tag_value(r"5\sraiders\sfrom\sA\:B")
    '5 raiders from A;B'
    """
    if "\\" not in value:
        return value
    out: list[str] = []
    chars = iter(value)
    for char in chars:
        if char != "\\":
            out.append(char)
            continue
        escaped = next(chars, None)
        if escaped is not None:
            out.append(_TAG_ESCAPES.get(escaped, escaped))
    return "".join(out)


def parse_line(line: str) -> IrcMessage:
    """Parse one IRC line.

    Parameters
    ----------
    line : str
        A single message without the trailing CR LF.

    Returns
    -------
    IrcMessage

    Raises
    ------
    IrcParseError
        If the line is empty or has no command.

    Examples
    --------
    >>> msg = parse_line("@id=1;display-name=Vi :vi!vi@vi.tmi.twitch.tv PRIVMSG #chan :hello there")
    >>> msg.command, msg.params, msg.nick, msg.tags["display-name"]
    ('PRIVMSG', ('#chan', 'hello there'), 'vi', 'Vi')
    """
    rest = line.rstrip("\r\n")
    tags: dict[str, str] = {}
    prefix: str | None = None

    if rest.startswith("@"):
        raw_tags, _, rest = rest[1:].partition(" ")
        for item in raw_tags.split(";"):
            if not item:
                continue
            key, _, value = item.partition("=")
            tags[key] = unescape_tag_value(value)
        rest = rest.lstrip(" ")

    if rest.startswith(":"):
        prefix, _, rest = rest[1:].partition(" ")
        rest = rest.lstrip(" ")

    head, separator, trailing = rest.partition(" :")
    parts = head.split()
    if not parts:
        raise IrcParseError(f"no command in line: {line!r}")
    params = parts[1:]
    if separator:
        params.append(trailing)
    return IrcMessage(command=parts[0].upper(), params=tuple(params), prefix=prefix, tags=tags)
