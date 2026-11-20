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
import time
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
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_PREFIX = "openrouter/"
"""A model named ``openrouter/<id>`` is asked through OpenRouter (its free models
watch video too; 50 requests a day, 1000 once the account bought 10 credits)."""
_OPENROUTER_KEY = "openrouter-api-key"
# Models that said they are out of quota, and until when (Unix time): not asked again
# before then. The free daily quotas run out in the evening (2026-11-19: all three
# Gemini models), and every clip still tried each of them first.
_EXHAUSTED: dict[str, float] = {}
_DEFAULT_PAUSE_S = 3600.0
# A free model busy at its provider (429 without a reset time): back in minutes.
_BUSY_PAUSE_S = 120.0

_PROMPT = """You review short vertical clips cut from live game streams for a highlights channel.
Watch the clip (picture and sound). Judge it for a stranger scrolling Shorts or Reels
who has never seen this streamer.

A good clip has something happen: a funny fail, a big play, a scare, rage, a
surprising game moment, a great reaction to what is on screen. A weak clip is the
streamer talking with chat, reading donations or answering questions, a menu or
loading screen, or an inside joke that needs context.

Context: streamer {channel}; game {game}; chat at that moment: {chat}.
Give a score from 1 (nothing happens) to 10 (would go viral), one sentence on what
happens (plain and factual, for the channel owner), a title and a description written
in {language}, 3 to 5 hashtags in {language} people would search for this moment
(without "#", without the streamer's or the game's name: those are added separately),
the reason for the score, whether a viewer's personal information is readable on
screen (a real name with an address, a phone number, a private message), and whether
the clip shows or plays sexual content: nudity (in-game characters too, and posters or
pictures on screen), sex scenes or sex sounds, strippers or strip clubs. Instagram bans
accounts for it, so answer true when in doubt.

The title (at most 70 characters) and the description (one short line) are the caption
of an Instagram Reel. Write them like a viewer reacting in the comments, not like a news
report: very casual, the slang and laughs people really type in {language}, lowercase
is fine, one or two emoji at most. Never name the streamer or say "the streamer" (the
name is on the video), never describe the clip formally, no hashtags or links.
Not "The streamer crashes into a tree and dies" but "bro did NOT see that tree coming 💀"
or "he can't believe it lol"."""

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
        "sexual_content": {"type": "boolean"},
    },
    "required": [
        "score",
        "happens",
        "title",
        "description",
        "reason",
        "personal_info",
        "sexual_content",
    ],
}


class GeminiError(Exception):
    """Gemini could not judge the clip (no key, quota, network, a bad answer).

    Attributes
    ----------
    quota : bool
        The model is out of quota (HTTP 429); ``retry_after_s`` says for how long.
    """

    quota: bool = False
    retry_after_s: float = 0.0


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
    sexual_content : bool
        Nudity (in-game too), sex scenes or sounds, strippers: never posted, because
        Instagram blocks accounts for it (@sode.clipes, 2026-11-19).
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
    sexual_content: bool = False
    model: str = ""


def save_openrouter_key(key: str) -> None:
    """Store the OpenRouter API key in the credential store."""
    keyring.set_password(_KEYRING_SERVICE, _OPENROUTER_KEY, key.strip())


def load_openrouter_key() -> str | None:
    """Return the stored OpenRouter API key, or ``None``."""
    try:
        return keyring.get_password(_KEYRING_SERVICE, _OPENROUTER_KEY) or None
    except keyring.errors.KeyringError:
        return None


def exhausted_until() -> dict[str, float]:
    """Models out of quota and when they may be asked again (Unix time), for the page."""
    now = time.time()
    return {m: t for m, t in _EXHAUSTED.items() if t > now}


def _retry_after(detail: str) -> float:
    """Seconds until a 429 says to retry ("Please retry in 9h12m40s"), else an hour."""
    found = re.search(r"retry in (?:(\d+)h)?(?:(\d+)m)?(?:(\d+)(?:\.\d+)?s)?", detail)
    if not found or not any(found.groups()):
        return _DEFAULT_PAUSE_S
    h, m, s = (int(g or 0) for g in found.groups())
    return float(h * 3600 + m * 60 + s)


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
    if not key and not load_openrouter_key():
        raise GeminiError("no Gemini or OpenRouter API key: run `peakreel gemini setup`")
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
            sexual_content=bool(data.get("sexual_content", False)),
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
    router_key = load_openrouter_key()
    errors: list[str] = []
    now = time.time()
    for model in models:
        if _EXHAUSTED.get(model, 0.0) > now:
            errors.append(f"{model}: out of quota for {(_EXHAUSTED[model] - now) / 3600:.1f} h")
            continue
        try:
            if model.startswith(OPENROUTER_PREFIX):
                if not router_key:
                    raise GeminiError("no OpenRouter key: run `peakreel openrouter setup`")
                answer = _post_openrouter(
                    router_key, model.removeprefix(OPENROUTER_PREFIX), parts, schema, timeout_s
                )
            else:
                if not key:
                    raise GeminiError("no Gemini API key: run `peakreel gemini setup`")
                answer = _post(url, key, model, parts, schema, timeout_s)
            return answer, model
        except GeminiError as exc:
            if exc.quota:
                _EXHAUSTED[model] = now + exc.retry_after_s
                log.info(
                    "model out of quota", model=model, hours=round(exc.retry_after_s / 3600, 1)
                )
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
        error = GeminiError(f"Gemini HTTP {exc.code}: {detail}")
        if exc.code == 429:
            error.quota, error.retry_after_s = True, _retry_after(detail)
        raise error from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise GeminiError(f"cannot reach Gemini: {exc}") from exc
    try:
        data = json.loads(output_text(answer))
    except ValueError as exc:
        raise GeminiError(f"answer is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GeminiError("answer is not a JSON object")
    return cast("dict[str, Any]", data)


def _post_openrouter(
    key: str,
    model: str,
    parts: Sequence[Mapping[str, Any]],
    schema: Mapping[str, Any],
    timeout_s: float,
    url: str = OPENROUTER_URL,
) -> dict[str, Any]:
    """Ask an OpenRouter model (Chat Completions, the video as a data URL) for JSON."""
    content: list[dict[str, Any]] = []
    for part in parts:
        if part.get("type") == "video":
            data_url = f"data:{part.get('mime_type', 'video/mp4')};base64,{part['data']}"
            content.append({"type": "video_url", "video_url": {"url": data_url}})
        elif part.get("type") == "text":
            content.append({"type": "text", "text": str(part["text"])})
    # Not every free model honours response_format: the schema is in the prompt too.
    content.append({"type": "text", "text": "Answer with one JSON object only, matching this "
                    f"JSON schema: {json.dumps(schema)}"})  # fmt: skip
    body = {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(  # noqa: S310 - fixed https endpoint (http only in tests)
        url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:  # noqa: S310
            answer = cast("dict[str, Any]", json.loads(response.read()))
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:300].decode("utf-8", "replace")
        error = GeminiError(f"OpenRouter HTTP {exc.code}: {detail}")
        if exc.code == 402:  # no credit for this (video needs a balance of at least $1)
            error.quota, error.retry_after_s = True, _DEFAULT_PAUSE_S
        elif exc.code == 429:
            reset = exc.headers.get("X-RateLimit-Reset") if exc.headers else None
            error.quota = True
            error.retry_after_s = (
                max(60.0, int(reset) / 1000 - time.time())
                if reset and reset.isdigit()
                else _BUSY_PAUSE_S
            )
        raise error from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise GeminiError(f"cannot reach OpenRouter: {exc}") from exc
    if answer.get("error"):
        raise GeminiError(f"OpenRouter: {str(answer['error'])[:200]}")
    try:
        text = str(answer["choices"][0]["message"]["content"] or "")
        text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```")
        data = json.loads(text[text.find("{") : text.rfind("}") + 1])
    except (KeyError, IndexError, ValueError) as exc:
        raise GeminiError(f"OpenRouter answer is not JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise GeminiError("OpenRouter answer is not a JSON object")
    return cast("dict[str, Any]", data)
