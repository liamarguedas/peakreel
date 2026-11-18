"""Per-platform post text: title, description or caption, tags, hashtags, credit.

One clip becomes one :class:`PostMetadata` per platform, built from a template in
``publish.templates`` with a fixed set of variables:

``{title}``
    The reviewer's title, or the suggestion from chat.
``{description}``
    The reviewer's description (may be empty).
``{streamer}``, ``{channel}``
    Display name, and the login (Twitch) or slug (Kick).
``{game}``
    The stream's game, when Twitch stream status is known.
``{mention}``
    ``@handle`` of the streamer on this platform, or empty. Handles come only from
    ``channels[].handles``; they are never guessed.
``{credit}``
    The credit line, always containing the channel's address (``twitch.tv/<login>``
    or ``kick.com/<slug>``).
``{source}``
    The channel's URL.
``{hashtags}``
    ``#tag #tag ...``: the channel's, the game's, then ``publish.hashtags``.

Lines that render empty are dropped, and runs of blank lines collapse to one.

Limits are enforced in the unit each platform counts in (see
:class:`peakreel.config.PlatformLimits`). When text is too long it is shortened
deterministically, in this order, until it fits: the description (at a word
boundary, ending in "..."), then hashtags from the end, then the title. The
credit line is never shortened: if the text cannot fit with it, validation fails
instead of publishing without credit.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, replace
from typing import Any, Literal

from peakreel.config import (
    ChannelConfig,
    Platform,
    PlatformLimits,
    PublishAccount,
    PublishConfig,
)
from peakreel.platforms import channel_link, channel_name
from peakreel.render.vertical import credit_text

__all__ = [
    "DEFAULT_TEMPLATES",
    "ClipFacts",
    "PostMetadata",
    "build_metadata",
    "game_hashtag",
    "measure",
    "validate",
    "with_edits",
]

DEFAULT_TEMPLATES: dict[Platform, dict[str, str]] = {
    "youtube": {
        "title": "{title}",
        "text": "{description}\n\n{credit} {mention}\n{source}\n\n{hashtags}",
    },
    "tiktok": {"text": "{title}\n\n{description}\n\n{credit} {mention}\n\n{hashtags}"},
    "instagram": {"text": "{title}\n\n{description}\n\n{credit} {mention}\n\n{hashtags}"},
    "facebook": {"text": "{title}\n\n{description}\n\n{credit}\n{source}\n\n{hashtags}"},
}
"""Templates per platform; ``title`` exists only where the platform has a title field."""

_ELLIPSIS = "..."
_YOUTUBE_FORBIDDEN = re.compile(r"[<>]")


def measure(text: str, unit: Literal["chars", "utf16", "bytes"]) -> int:
    """Length of ``text`` in the unit a platform counts in.

    Examples
    --------
    >>> measure("gg 🎉", "chars"), measure("gg 🎉", "utf16"), measure("gg 🎉", "bytes")
    (4, 5, 7)
    """
    if unit == "utf16":
        return len(text.encode("utf-16-le")) // 2
    if unit == "bytes":
        return len(text.encode("utf-8"))
    return len(text)


def game_hashtag(game: str) -> str | None:
    """Turn a game name into a hashtag body, or ``None`` if nothing usable is left.

    Examples
    --------
    >>> game_hashtag("Grand Theft Auto VI")
    'GrandTheftAutoVI'
    >>> game_hashtag("League of Legends: Wild Rift")
    'LeagueOfLegendsWildRift'
    >>> game_hashtag("Just Chatting")
    'JustChatting'
    >>> game_hashtag("  ") is None
    True
    """
    words = re.findall(r"\w+", game)
    tag = "".join(w[:1].upper() + w[1:] for w in words)
    return tag or None


@dataclass(frozen=True, slots=True)
class ClipFacts:
    """What is known about a clip, independent of the platform.

    Attributes
    ----------
    channel : ChannelConfig
    title : str
        The reviewer's title, or the chat suggestion.
    description : str
    game : str or None
        From Twitch stream status, when known.
    """

    channel: ChannelConfig
    title: str
    description: str = ""
    game: str | None = None


@dataclass(frozen=True, slots=True)
class PostMetadata:
    """Post text for one platform.

    Attributes
    ----------
    platform : str
    title : str or None
        YouTube only.
    text : str
        Description (YouTube) or caption (TikTok, Instagram).
    tags : tuple of str
        YouTube tags (not hashtags).
    hashtags : tuple of str
        Hashtags placed in ``text``, without ``#``.
    credit : str
        The credit line that ``text`` must contain.
    notes : tuple of str
        What was shortened or dropped to fit, for the reviewer.
    """

    platform: Platform
    title: str | None
    text: str
    tags: tuple[str, ...] = ()
    hashtags: tuple[str, ...] = ()
    credit: str = ""
    notes: tuple[str, ...] = field(default=())

    def to_json(self) -> dict[str, Any]:
        """JSON-ready form, stored with the publication."""
        data = asdict(self)
        data["tags"] = list(self.tags)
        data["hashtags"] = list(self.hashtags)
        data["notes"] = list(self.notes)
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> PostMetadata:
        """Inverse of :meth:`to_json`."""
        return cls(
            platform=data["platform"],
            title=data.get("title"),
            text=str(data["text"]),
            tags=tuple(data.get("tags") or ()),
            hashtags=tuple(data.get("hashtags") or ()),
            credit=str(data.get("credit") or ""),
            notes=tuple(data.get("notes") or ()),
        )


def _hashtags(
    facts: ClipFacts, settings: PublishConfig, account: PublishAccount | None
) -> list[str]:
    candidates = list(facts.channel.hashtags)
    if settings.game_hashtag and facts.game:
        tag = game_hashtag(facts.game)
        if tag:
            candidates.append(tag)
    if account is not None:
        candidates += account.hashtags
    candidates += settings.hashtags
    seen: set[str] = set()
    unique: list[str] = []
    for tag in candidates:
        if tag.casefold() not in seen:
            seen.add(tag.casefold())
            unique.append(tag)
    return unique


def _render(template: str, values: Mapping[str, str]) -> str:
    text = template.format_map(values)
    lines = [" ".join(line.split()) for line in text.splitlines()]
    out: list[str] = []
    for line in lines:
        if not line and (not out or not out[-1]):
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out)


def _shorten(text: str, fits: Callable[[str], bool]) -> str:
    """Longest word-boundary prefix of ``text`` + "..." for which ``fits`` holds."""
    words = text.split(" ")
    for count in range(len(words) - 1, 0, -1):
        candidate = " ".join(words[:count]).rstrip(" ,.;:!?") + _ELLIPSIS
        if fits(candidate):
            return candidate
    # One very long word (or unspaced text such as Japanese): cut characters.
    for size in range(len(text) - 1, 0, -1):
        candidate = text[:size].rstrip() + _ELLIPSIS
        if fits(candidate):
            return candidate
    return ""


def _youtube_tags(facts: ClipFacts, hashtags: list[str], limit: int | None) -> tuple[str, ...]:
    wanted = [facts.channel.display, facts.channel.name]
    if facts.game:
        wanted.append(facts.game)
    wanted += [*hashtags, "twitch clips"]
    tags: list[str] = []
    seen: set[str] = set()
    for tag in wanted:
        tag = _YOUTUBE_FORBIDDEN.sub("", tag).strip()
        if not tag or tag.casefold() in seen:
            continue
        if limit is not None and _tags_length([*tags, tag]) > limit:
            continue
        seen.add(tag.casefold())
        tags.append(tag)
    return tuple(tags)


def _tags_length(tags: list[str]) -> int:
    # YouTube counts the commas between tags, and quotes around tags with spaces.
    return sum(len(t) + (2 if " " in t else 0) for t in tags) + max(0, len(tags) - 1)


def build_metadata(
    facts: ClipFacts,
    platform: Platform,
    settings: PublishConfig,
    templates: Mapping[Literal["title", "text"], str] | None = None,
    account: PublishAccount | None = None,
) -> PostMetadata:
    """Build the post text of one clip for one platform, within its limits.

    Parameters
    ----------
    facts : ClipFacts
    platform : {"youtube", "tiktok", "instagram", "facebook"}
    settings : PublishConfig
        Limits per platform and global hashtags.
    account : PublishAccount, optional
        The account posting it: its credit line (in its language), hashtags and
        templates apply. Without one, the channel's credit text is used.
    templates : mapping, optional
        ``{"title": ..., "text": ...}``; defaults to :data:`DEFAULT_TEMPLATES`.

    Returns
    -------
    PostMetadata
        Run :func:`validate` on it before publishing; a credit line that cannot
        fit is reported there, never silently dropped.

    Examples
    --------
    >>> from peakreel.config import ChannelConfig, PublishConfig
    >>> facts = ClipFacts(ChannelConfig(name="alpha", handles={"tiktok": "alpha_tt"}), "No way")
    >>> print(build_metadata(facts, "tiktok", PublishConfig()).text)
    No way
    <BLANKLINE>
    Clip from twitch.tv/alpha @alpha_tt
    <BLANKLINE>
    #twitch #clips
    """
    limits = settings.limits(platform)
    account_templates = account.templates.get(platform) if account is not None else None
    chosen = account_templates or templates or DEFAULT_TEMPLATES[platform]
    handle = getattr(facts.channel.handles, platform)
    channel = facts.channel.name
    credit = (
        account.credit_line(channel, facts.channel.display)
        if account is not None
        else credit_text(channel, facts.channel.credit)
    )
    hashtags = _hashtags(facts, settings, account)
    notes: list[str] = []
    if limits.hashtags_max is not None and len(hashtags) > limits.hashtags_max:
        notes.append(f"kept the first {limits.hashtags_max} of {len(hashtags)} hashtags")
        hashtags = hashtags[: limits.hashtags_max]
    title = facts.title.strip()
    description = facts.description.strip()
    if platform == "youtube":
        title = _YOUTUBE_FORBIDDEN.sub("", title)
        description = _YOUTUBE_FORBIDDEN.sub("", description)

    def values(title: str, description: str, tags: list[str]) -> dict[str, str]:
        return {
            "title": title,
            "description": description,
            "streamer": facts.channel.display,
            "channel": channel_name(channel),
            "game": facts.game or "",
            "mention": f"@{handle}" if handle else "",
            "credit": credit,
            "source": channel_link(channel),
            "hashtags": " ".join(f"#{t}" for t in tags),
        }

    def text_of(title: str, description: str, tags: list[str]) -> str:
        return _render(chosen["text"], values(title, description, tags))

    def text_fits(title: str, description: str, tags: list[str]) -> bool:
        return measure(text_of(title, description, tags), limits.text_unit) <= limits.text_max

    if description and not text_fits(title, description, hashtags):
        shorter = _shorten(description, lambda d: text_fits(title, d, hashtags))
        notes.append("description shortened" if shorter else "description dropped")
        description = shorter
    dropped = 0
    while hashtags and not text_fits(title, description, hashtags):
        hashtags = hashtags[:-1]
        dropped += 1
    if dropped:
        notes.append(f"dropped {dropped} hashtag(s) to fit")
    if "{title}" in chosen["text"] and not text_fits(title, description, hashtags):
        title = _shorten(title, lambda t: text_fits(t, description, hashtags))
        notes.append("title shortened to fit the caption")

    post_title: str | None = None
    if "title" in chosen:
        post_title = _render(chosen["title"], values(title, description, hashtags))
        if limits.title_max is not None and len(post_title) > limits.title_max:
            post_title = _shorten(post_title, lambda t: len(t) <= (limits.title_max or 0))
            notes.append(f"title shortened to {limits.title_max} characters")
    tags = _youtube_tags(facts, hashtags, limits.tags_max_chars) if platform == "youtube" else ()
    return PostMetadata(
        platform=platform,
        title=post_title,
        text=text_of(title, description, hashtags),
        tags=tags,
        hashtags=tuple(hashtags),
        credit=credit,
        notes=tuple(notes),
    )


def validate(meta: PostMetadata, limits: PlatformLimits) -> list[str]:
    """Check post text against the platform's limits and the credit rule.

    Run on generated text and again on text the reviewer edited.

    Parameters
    ----------
    meta : PostMetadata
    limits : PlatformLimits

    Returns
    -------
    list of str
        Problems; empty when the post may go out.
    """
    problems: list[str] = []
    if meta.credit and meta.credit not in meta.text:
        problems.append(f"the credit line is missing: {meta.credit!r}")
    size = measure(meta.text, limits.text_unit)
    if size > limits.text_max:
        problems.append(f"text is {size} {limits.text_unit}, limit {limits.text_max}")
    if limits.title_max is not None:
        if not meta.title:
            problems.append("title is empty")
        elif len(meta.title) > limits.title_max:
            problems.append(f"title is {len(meta.title)} characters, limit {limits.title_max}")
    if meta.platform == "youtube":
        for name, value in (("title", meta.title or ""), ("description", meta.text)):
            if _YOUTUBE_FORBIDDEN.search(value):
                problems.append(f"YouTube does not allow < or > in the {name}")
    if limits.tags_max_chars is not None and _tags_length(list(meta.tags)) > limits.tags_max_chars:
        problems.append(f"tags exceed {limits.tags_max_chars} characters")
    hashtags = re.findall(r"(?<!\w)#\w+", meta.text)
    if limits.hashtags_max is not None and len(hashtags) > limits.hashtags_max:
        problems.append(f"{len(hashtags)} hashtags, limit {limits.hashtags_max}")
    mentions = re.findall(r"(?<![\w@])@[\w.]+", meta.text)
    if limits.mentions_max is not None and len(mentions) > limits.mentions_max:
        problems.append(f"{len(mentions)} @mentions, limit {limits.mentions_max}")
    return problems


def with_edits(meta: PostMetadata, title: str | None, text: str | None) -> PostMetadata:
    """Return ``meta`` with the reviewer's edited title and/or text."""
    return replace(
        meta,
        title=meta.title if title is None else title,
        text=meta.text if text is None else text,
        notes=(*meta.notes, "edited by reviewer") if (title, text) != (None, None) else meta.notes,
    )
