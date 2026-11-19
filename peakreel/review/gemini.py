"""Gemini watches a clip and judges it (free tier of the Gemini API).

The clip itself (picture and sound) is sent, not only its transcript, so Gemini
can see whether something actually happens on screen. Before sending, ffmpeg makes
a small copy (640 px wide, 2 fps, low bitrate): Gemini samples about one frame per
second anyway, and the request must stay under the inline size limit (20 MB).

The API key lives in the system credential store (``peakreel gemini setup``).
Free-tier requests may be used by Google to improve its products.
"""

from __future__ import annotations

import base64
import contextlib
import json
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import keyring
import keyring.errors

from peakreel.log import get_logger

log = get_logger(__name__)

__all__ = ["GeminiError", "Verdict", "ask_json", "clear_key", "judge_clip", "load_key", "save_key"]

_KEYRING_SERVICE = "peakreel"
LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "pt": "Brazilian Portuguese", "ja": "Japanese"}
_KEY = "gemini-api-key"
API_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"

_PROMPT = """You review short vertical clips cut from live game streams for a highlights channel.
Watch the clip (picture and sound). Judge it for a stranger scrolling Shorts or Reels
who has never seen this streamer.

A good clip has something happen: a funny fail, a big play, a scare, rage, a
surprising game moment, a great reaction to what is on screen. A weak clip is the
streamer talking with chat, reading donations or answering questions, a menu or
loading screen, or an inside joke that needs context.

Context: streamer {channel}; game {game}; chat at that moment: {chat}.
Give a score from 1 (nothing happens) to 10 (would go viral), one sentence on what
happens, a short title (at most 70 characters, written in {language}), a post
description (one or two sentences written in {language} that make a stranger want
to watch, without hashtags, links or the streamer's channel: those are added
separately), 3 to 5 hashtags in {language} people would search for this moment (without "#",
without the streamer's or the game's name: those are added separately), the reason
for the score, and whether a viewer's personal information
is readable on screen (a real name with an address, a phone number, a private message)."""

# Measured: without examples Gemini scored 37 of 40 clips 5 to 7, whatever the
# reviewer decided, so the score could not separate good from weak clips.
_TASTE = """

The channel owner reviewed these recent clips themselves (what happened, the chat,
their decision and, for a rejection, why):
{examples}

Score this clip by how likely the owner is to approve it, judged against those
decisions: 10 = they surely approve it, 1 = they surely reject it. Use the whole range;
do not settle on a middle score."""

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "score": {"type": "integer", "minimum": 1, "maximum": 10},
        "happens": {"type": "string"},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
        "reason": {"type": "string"},
        "personal_info": {"type": "boolean"},
    },
    "required": ["score", "happens", "title", "description", "reason", "personal_info"],
}


class GeminiError(Exception):
    """Gemini could not judge the clip (no key, quota, network, a bad answer)."""


@dataclass(frozen=True, slots=True)
class Verdict:
    """Gemini's judgment of one clip.

    Attributes
    ----------
    score : int
        1 (nothing happens) to 10 (would go viral).
    happens : str
        One sentence on what happens.
    title : str
        A suggested post title.
    description : str
        A suggested post description (credit and hashtags are added separately).
    hashtags : tuple of str
        Up to 5 hashtag bodies for the moment (no ``#``; letters, digits, ``_``).
    reason : str
        Why that score.
    personal_info : bool
        A viewer's personal information is readable on screen.
    model : str
        The model that answered.
    """

    score: int
    happens: str
    title: str
    reason: str
    description: str = ""
    hashtags: tuple[str, ...] = ()
    personal_info: bool = False
    model: str = ""


def save_key(key: str) -> None:
    """Store the Gemini API key in the credential store."""
    keyring.set_password(_KEYRING_SERVICE, _KEY, key.strip())


def load_key() -> str | None:
    """Return the stored Gemini API key, or ``None``."""
    try:
        return keyring.get_password(_KEYRING_SERVICE, _KEY) or None
    except keyring.errors.KeyringError:
        return None


def clear_key() -> None:
    """Forget the Gemini API key (missing entries are ignored)."""
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(_KEYRING_SERVICE, _KEY)


def _small_copy(ffmpeg: Path, clip: Path, out: Path) -> None:
    subprocess.run(
        [str(ffmpeg), "-v", "error", "-y", "-i", str(clip), "-vf", "scale=640:-2", "-r", "2",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "32",
         "-c:a", "aac", "-b:a", "64k", "-ac", "1", str(out)],
        check=True, capture_output=True, timeout=120,
    )  # fmt: skip


def clean_hashtags(tags: Sequence[object], limit: int = 5) -> tuple[str, ...]:
    """Return usable hashtag bodies: no ``#`` or spaces, unique, at most ``limit``.

    Examples
    --------
    >>> clean_hashtags(["#GTA6 Fails", "carcrash", "gta6fails", "!!", 7])
    ('GTA6Fails', 'carcrash', '7')
    """
    seen: set[str] = set()
    out: list[str] = []
    for tag in tags:
        body = re.sub(r"\W", "", str(tag))
        if body and body.casefold() not in seen:
            seen.add(body.casefold())
            out.append(body)
    return tuple(out[:limit])


def output_text(body: dict[str, Any]) -> str:
    """Return the generated text of an Interactions API response.

    The SDKs expose it as ``output_text``; the raw body may instead hold it in
    ``outputs`` or ``steps`` entries with a ``text`` field.

    Examples
    --------
    >>> output_text({"output_text": '{"a": 1}'})
    '{"a": 1}'
    >>> output_text({"outputs": [{"type": "thought"}, {"type": "text", "text": "hi"}]})
    'hi'
    """
    if isinstance(body.get("output_text"), str):
        return str(body["output_text"])
    for key in ("outputs", "steps"):
        for item in cast("list[dict[str, Any]]", body.get(key) or [])[::-1]:
            text = item.get("text")
            if isinstance(text, str) and text.strip():
                return text
            for part in cast("list[dict[str, Any]]", item.get("content") or []):
                if isinstance(part.get("text"), str):
                    return str(part["text"])
    raise GeminiError(f"no text in the answer: {json.dumps(body)[:300]}")


def judge_clip(
    clip: Path,
    *,
    ffmpeg: Path,
    models: Sequence[str],
    channel: str,
    game: str | None,
    chat: str,
    language: str | None = None,
    taste: str = "",
    key: str | None = None,
    url: str = API_URL,
    timeout_s: float = 120.0,
) -> Verdict:
    """Have Gemini watch ``clip`` and judge it.

    Parameters
    ----------
    clip : pathlib.Path
        The clip (the horizontal original shows the full frame).
    ffmpeg : pathlib.Path
    models : sequence of str
        Gemini models with a free tier, tried in order until one answers (the free
        tier is often busy: HTTP 503).
    channel, game, chat : str
        Context for the prompt; ``chat`` is the top chat messages.
    language : str, optional
        ISO 639-1 code of the posting account; title, description and hashtags are
        written in it.
    taste : str, optional
        The reviewer's past decisions, one per line
        (:func:`peakreel.review.auto.taste_examples`). With them the score means
        how likely the reviewer approves this clip, judged against those choices.
    key : str, optional
        API key; default the stored one.
    url : str
        Interactions endpoint (tests point it at a fake server).
    timeout_s : float, default 120.0

    Returns
    -------
    Verdict

    Raises
    ------
    GeminiError
        No key, or no model answered (busy, quota, network, an answer that did
        not parse); the message lists each model's error.
    """
    key = key or load_key()
    if not key:
        raise GeminiError("no Gemini API key: run `peakreel gemini setup`")
    with tempfile.TemporaryDirectory(prefix="peakreel-gemini-") as tmp:
        small = Path(tmp) / "clip.mp4"
        try:
            _small_copy(ffmpeg, clip, small)
        except (OSError, subprocess.SubprocessError) as exc:
            raise GeminiError(f"could not prepare the clip: {exc}") from exc
        video = base64.b64encode(small.read_bytes()).decode("ascii")
    prompt = _PROMPT.format(
        channel=channel, game=game or "unknown", chat=chat or "none",
        # The post language is the account's, not a guess from the audio (seen: an
        # English title on a Spanish clip).
        language=LANGUAGE_NAMES.get(language or "", "the language spoken in the clip"),
    )  # fmt: skip
    if taste:
        prompt += _TASTE.format(examples=taste)
    parts = [
        {"type": "video", "data": video, "mime_type": "video/mp4"},
        {"type": "text", "text": prompt},
    ]
    data, model = ask_json(parts, _SCHEMA, key=key, models=models, url=url, timeout_s=timeout_s)
    try:
        return Verdict(
            score=max(1, min(10, int(data["score"]))),
            happens=str(data["happens"]),
            title=str(data["title"])[:100],
            reason=str(data["reason"]),
            description=str(data.get("description", ""))[:1000],
            hashtags=clean_hashtags(data.get("hashtags") or []),
            personal_info=bool(data.get("personal_info", False)),
            model=model,
        )
    except (ValueError, KeyError, TypeError) as exc:
        raise GeminiError(f"unexpected answer: {exc}") from exc


def ask_json(
    parts: Sequence[Mapping[str, Any]],
    schema: Mapping[str, Any],
    *,
    key: str | None = None,
    models: Sequence[str],
    url: str = API_URL,
    timeout_s: float = 120.0,
) -> tuple[dict[str, Any], str]:
    """Send ``parts`` to Gemini and return its JSON answer and the model that gave it.

    Models are tried in order until one answers (the free tier is often busy).

    Parameters
    ----------
    parts : sequence of mapping
        Interactions API input parts (``{"type": "text", "text": ...}``, video...).
    schema : mapping
        JSON schema the answer must follow.
    key : str, optional
        API key; default the stored one.
    models : sequence of str
    url : str
    timeout_s : float, default 120.0

    Returns
    -------
    (dict, str)

    Raises
    ------
    GeminiError
        No key, or no model answered with JSON; the message lists each model's error.
    """
    key = key or load_key()
    if not key:
        raise GeminiError("no Gemini API key: run `peakreel gemini setup`")
    errors: list[str] = []
    for model in models:
        try:
            return _post(url, key, model, parts, schema, timeout_s), model
        except GeminiError as exc:
            errors.append(f"{model}: {exc}")
    raise GeminiError("; ".join(errors) or "no Gemini model configured")


def _post(
    url: str,
    key: str,
    model: str,
    parts: Sequence[Mapping[str, Any]],
    schema: Mapping[str, Any],
    timeout_s: float,
) -> dict[str, Any]:
    body = {
        "model": model,
        "input": list(parts),
        "response_format": {"type": "text", "mime_type": "application/json", "schema": schema},
        "store": False,
    }
    request = urllib.request.Request(  # noqa: S310 - fixed https endpoint (http only in tests)
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            answer = cast("dict[str, Any]", json.loads(response.read()))
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        raise GeminiError(f"Gemini HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise GeminiError(f"cannot reach Gemini: {exc}") from exc
    try:
        data = json.loads(output_text(answer))
    except ValueError as exc:
        raise GeminiError(f"answer is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GeminiError("answer is not a JSON object")
    return cast("dict[str, Any]", data)
