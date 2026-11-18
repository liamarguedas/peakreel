"""Review flags: what a reviewer should look at before approving a clip.

A flag never rejects a clip; it appears on the clip in the review tool with its
reason. Kinds:

``blocked_words``
    A blocked word (slur, configured term) in the transcript or the top chat.
``music``
    A steady musical beat: listen for licensed music, which platforms mute or strike.
``alerts``
    Subs, gifts, cheers or a raid during the clip: their on-screen alerts usually
    show viewer names.
``checks_incomplete``
    A check could not run (word lists not downloaded, audio unreadable), so the
    absence of other flags means less.

On-screen personal information in general (addresses, private messages, a phone
on camera) cannot be detected reliably; the review tool shows a reminder on every
clip instead.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from peakreel.chat.base import ChatMessage
from peakreel.safety.words import WordList, find_words

__all__ = ["ALERT_NOTICES", "Flag", "alert_flag", "music_flag", "word_flags"]

ALERT_NOTICES: dict[str, str] = {
    "sub": "sub",
    "resub": "sub",
    "subgift": "gift",
    "submysterygift": "gift",
    "giftpaidupgrade": "sub",
    "primepaidupgrade": "sub",
    "anongiftpaidupgrade": "sub",
    "raid": "raid",
}
"""Chat notices (``msg-id``) that come with an on-screen alert, by alert kind."""


@dataclass(frozen=True, slots=True)
class Flag:
    """One reason for the reviewer to look closely.

    Attributes
    ----------
    kind : str
        ``blocked_words``, ``music``, ``alerts`` or ``checks_incomplete``.
    message : str
        Shown to the reviewer as is.
    """

    kind: str
    message: str

    def to_json(self) -> dict[str, str]:
        """Return the flag as a JSON-ready mapping."""
        return {"kind": self.kind, "message": self.message}


def word_flags(words: WordList, transcript: str | None, chat: Sequence[str]) -> list[Flag]:
    """Flag blocked words in the transcript and in the clip's top chat messages.

    Parameters
    ----------
    words : WordList
    transcript : str or None
        ``None`` when the clip was not transcribed.
    chat : sequence of str
        The top chat messages of the spike.

    Returns
    -------
    list of Flag
    """
    flags: list[Flag] = []
    if words.missing:
        flags.append(
            Flag(
                "checks_incomplete",
                f"word lists not downloaded ({', '.join(words.missing)}): "
                "run `peakreel safety download`",
            )
        )
    for source, text in (("transcript", transcript or ""), ("top chat", "\n".join(chat))):
        found = find_words(text, words)
        if found:
            flags.append(Flag("blocked_words", f"blocked words in {source}: {', '.join(found)}"))
    return flags


def music_flag(score: float | None, threshold: float) -> Flag | None:
    """Flag a likely music track.

    Parameters
    ----------
    score : float or None
        From :func:`peakreel.safety.music.music_score`; ``None`` if the audio
        could not be read.
    threshold : float

    Returns
    -------
    Flag or None
    """
    if score is None:
        return Flag("checks_incomplete", "music check failed: audio could not be read")
    if score >= threshold:
        return Flag(
            "music",
            f"music likely (beat score {score:.2f}): listen for licensed tracks, "
            "which platforms mute or strike",
        )
    return None


def alert_flag(events: Iterable[ChatMessage]) -> Flag | None:
    """Flag on-screen alerts likely to show viewer names.

    Parameters
    ----------
    events : iterable of ChatMessage
        Chat of the clip's channel during the clip.

    Returns
    -------
    Flag or None

    Examples
    --------
    >>> sub = ChatMessage(0.0, "ch", "fan", "", "usernotice", tags={"msg-id": "resub"})
    >>> cheer = ChatMessage(1.0, "ch", "fan2", "Cheer100 gg", tags={"bits": "100"})
    >>> alert_flag([sub, cheer]).message
    'on-screen alerts likely (1 cheer, 1 sub): they usually show viewer names'
    """
    counts: dict[str, int] = {}
    for event in events:
        if event.type == "usernotice":
            kind = ALERT_NOTICES.get(event.tags.get("msg-id", ""))
        else:
            kind = "cheer" if event.tags.get("bits") else None
        if kind is not None:
            counts[kind] = counts.get(kind, 0) + 1
    if not counts:
        return None
    summary = ", ".join(f"{n} {kind}{'s' if n > 1 else ''}" for kind, n in sorted(counts.items()))
    return Flag("alerts", f"on-screen alerts likely ({summary}): they usually show viewer names")
