"""Translated copies of approved clips, for the other language accounts.

When a clip is approved (by a person or by auto review) on its own language's
account, each other posting account gets a copy in its language:

1. Gemini translates the subtitle lines (timing kept), the title, the description
   and the hashtags (one text request per language, no video).
2. The clip is rendered again with the translated subtitles burned in and the credit
   line of the target account (``Clip de kick.com/...``).
3. The copy becomes its own clip row (``translation_of`` = the source, ``language`` =
   the target) and is approved at once, so the normal queue posts it on the target
   account; it shows in Approved and Published, where it can be rejected before it
   posts like any clip.

Translation runs in the review page's background loop, one copy at a time, so it
never delays publishing. A busy Gemini is retried on later passes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from peakreel.config import Config, PublishAccount
from peakreel.db import insert_clip
from peakreel.log import get_logger
from peakreel.publish.queue import clip_route
from peakreel.render.vertical import render_vertical
from peakreel.review.gemini import GeminiError, ask_json, clean_hashtags
from peakreel.review.store import get_clip, update_clip, view_of

log = get_logger(__name__)

__all__ = ["Chunk", "build_ass", "read_ass", "translate_once"]

LANGUAGE_NAMES = {"en": "English", "es": "Spanish", "pt": "Brazilian Portuguese", "ja": "Japanese"}
_RETRY_S = 600.0  # a failed copy is tried again after this long

_PROMPT = """Translate the subtitles of a short gaming clip into {language}.
Streamer: {channel}. Return exactly {count} subtitle lines, in the same order, each a
natural spoken {language} line about as short as the original (subtitles), keeping
names, game terms and the tone (jokes, swearing). Also translate the post title (at most
70 characters) and description, and give 3 to 5 hashtags in {language} that people would
search (without "#", without the streamer's or the game's name).

Subtitle lines (JSON): {lines}
Title: {title}
Description: {description}"""


@dataclass(frozen=True, slots=True)
class Chunk:
    """One subtitle line on screen.

    Attributes
    ----------
    start, end : str
        ASS timestamps (``0:00:01.20``).
    style : str
    text : str
        Plain text, without override tags.
    """

    start: str
    end: str
    style: str
    text: str


def read_ass(text: str) -> tuple[str, list[Chunk]]:
    """Split ASS subtitles into their header and the lines shown on screen.

    The captioner writes one event per highlighted word (karaoke); consecutive
    events with the same plain text are one line on screen, from the first event's
    start to the last one's end.

    Examples
    --------
    >>> header, chunks = read_ass(
    ...     "[Events]" + chr(10)
    ...     + "Dialogue: 0,0:00:00.05,0:00:00.43,Caption,,0,0,0,,{b1}But{b0} it's" + chr(10)
    ...     + "Dialogue: 0,0:00:00.43,0:00:00.57,Caption,,0,0,0,,But {b1}it's{b0}" + chr(10)
    ...     + "Dialogue: 0,0:00:00.79,0:00:01.29,Caption,,0,0,0,,so hard")
    >>> [(c.start, c.end, c.text) for c in chunks]
    [('0:00:00.05', '0:00:00.57', "But it's"), ('0:00:00.79', '0:00:01.29', 'so hard')]
    """
    lines = text.splitlines()
    first = next((i for i, line in enumerate(lines) if line.startswith("Dialogue:")), len(lines))
    chunks: list[Chunk] = []
    for line in lines[first:]:
        if not line.startswith("Dialogue:"):
            continue
        fields = line[len("Dialogue:") :].split(",", 9)
        if len(fields) < 10:
            continue
        start, end, style = fields[1].strip(), fields[2].strip(), fields[3].strip()
        plain = re.sub(r"\{[^}]*\}", "", fields[9]).strip()
        if chunks and chunks[-1].text == plain and chunks[-1].style == style:
            chunks[-1] = Chunk(chunks[-1].start, end, style, plain)
        else:
            chunks.append(Chunk(start, end, style, plain))
    return "\n".join(lines[:first]), chunks


def build_ass(header: str, chunks: list[Chunk], texts: list[str]) -> str:
    """Return ASS subtitles with ``texts`` in place of the chunks' text, same timing.

    Examples
    --------
    >>> print(build_ass("[Events]", [Chunk("0:00:00.05", "0:00:00.57", "Caption", "hi")], ["hola"]))
    [Events]
    Dialogue: 0,0:00:00.05,0:00:00.57,Caption,,0,0,0,,hola
    <BLANKLINE>
    """
    events = [
        f"Dialogue: 0,{c.start},{c.end},{c.style},,0,0,0,,{t.replace(chr(10), ' ').strip()}"
        for c, t in zip(chunks, texts, strict=True)
    ]
    return "\n".join([header, *events]) + "\n"


_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "lines": {"type": "array", "items": {"type": "string"}},
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
    "required": ["lines", "title", "description", "hashtags"],
}


def _translate(
    config: Config, *, chunks: list[Chunk], title: str, description: str, channel: str,
    language: str, key: str,
) -> dict[str, Any]:  # fmt: skip
    prompt = _PROMPT.format(
        language=LANGUAGE_NAMES.get(language, language),
        channel=channel,
        count=len(chunks),
        lines=json.dumps([c.text for c in chunks], ensure_ascii=False),
        title=title or "(none)",
        description=description or "(none)",
    )
    data, _model = ask_json(
        [{"type": "text", "text": prompt}],
        _SCHEMA,
        key=key,
        models=config.review.auto.gemini_models,
    )
    lines = [str(x) for x in cast("list[object]", data.get("lines") or [])]
    if len(lines) != len(chunks):
        raise GeminiError(f"got {len(lines)} subtitle lines for {len(chunks)}")
    return {**data, "lines": lines}


def _targets(config: Config) -> list[tuple[PublishAccount, str]]:
    """Each posting account with the language its copies are made in."""
    return [(a, a.languages[0]) for a in config.publish.accounts if a.languages]


def _link(source: Path, target: Path) -> None:
    """Hard-link ``source`` to ``target`` (no extra disk), copying where links fail."""
    target.unlink(missing_ok=True)
    try:
        os.link(source, target)
    except OSError:
        shutil.copyfile(source, target)


def translate_once(
    config: Config,
    conn: sqlite3.Connection,
    *,
    ffmpeg: Path,
    key: str,
    failed: dict[tuple[int, str], float],
    now: float | None = None,
) -> tuple[int, str, int] | None:
    """Make the next missing translated copy, if any.

    Parameters
    ----------
    config : Config
    conn : sqlite3.Connection
    ffmpeg : pathlib.Path
    key : str
        Gemini API key.
    failed : dict
        ``(clip id, language)`` to the time a copy last failed; updated here, so a
        broken copy is retried only after ``_RETRY_S`` (the caller keeps the dict).
    now : float, optional

    Returns
    -------
    (int, str, int) or None
        Source clip id, language, and the new clip's id; ``None`` when nothing was
        made (no work, or it failed: the error is logged).
    """
    # shortcut: rejecting a source after its copies exist does not cancel the copies;
    # cascade the rejection in update_clip if copies posted by mistake become a problem.
    settings = config.review.translate
    if not settings.enabled:
        return None
    now = time.time() if now is None else now
    targets = _targets(config)
    rows = conn.execute(
        "SELECT id FROM clips WHERE state = 'approved' AND translation_of IS NULL "
        "AND purged_at IS NULL AND vertical_path IS NOT NULL AND reviewed_at >= ? "
        "ORDER BY reviewed_at",
        (now - settings.max_age_h * 3600,),
    ).fetchall()
    for (source_id,) in rows:
        row = get_clip(conn, int(source_id))
        if row is None:
            continue
        view = view_of(row, conn)
        route = clip_route(config, view, row)
        done = {
            str(r[0])
            for r in conn.execute(
                "SELECT language FROM clips WHERE translation_of = ?", (source_id,)
            )
        }
        for account, language in targets:
            if language == route.language or language in done:
                continue
            if route.account is not None and account.id == route.account.id:
                continue
            if now - failed.get((int(source_id), language), -1e18) < _RETRY_S:
                continue
            try:
                new_id = _make_copy(config, conn, row, view, account, language, ffmpeg, key, now)
            except Exception as exc:  # one broken copy must not stop the others
                log.warning(
                    "translation failed", clip_id=source_id, language=language, error=str(exc)[:300]
                )
                conn.rollback()
                failed[(int(source_id), language)] = now
                return None
            log.info("translation made", clip_id=source_id, language=language, copy_id=new_id)
            return int(source_id), language, new_id
    return None


def _make_copy(
    config: Config,
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    view: Any,
    account: PublishAccount,
    language: str,
    ffmpeg: Path,
    key: str,
    now: float,
) -> int:
    source = Path(row["path"])
    sidecar_path = Path(row["sidecar_path"])
    sidecar: dict[str, Any] = (
        json.loads(sidecar_path.read_text(encoding="utf-8")) if sidecar_path.exists() else {}
    )
    captions = cast("dict[str, Any]", sidecar.get("captions") or {})
    ass_source = source.parent / str(captions.get("file", "")) if captions.get("file") else None
    header, chunks = ("", [])
    if ass_source is not None and ass_source.is_file():
        header, chunks = read_ass(ass_source.read_text(encoding="utf-8"))
    data = _translate(
        config, chunks=chunks, title=view.title or view.suggested_title,
        description=view.description, channel=view.channel, language=language, key=key,
    )  # fmt: skip

    stem = f"{source.stem}.{language}"
    clip = source.with_name(f"{stem}.mp4")
    _link(source, clip)
    subtitles: Path | None = None
    if chunks:
        subtitles = source.with_name(f"{stem}.ass")
        subtitles.write_text(build_ass(header, chunks, data["lines"]), encoding="utf-8")
    vertical = source.with_name(f"{stem}.vertical.mp4")
    thumbnail = source.with_name(f"{stem}.jpg")
    channel_config = config.channel(view.channel)
    streamer = channel_config.display if channel_config else view.channel
    render_vertical(
        clip, vertical, thumbnail,
        channel=view.channel,
        credit=account.credit_line(view.channel, streamer),
        facecam=channel_config.facecam if channel_config else None,
        output=config.output, settings=config.render, ffmpeg=ffmpeg,
        thumbnail_at_s=config.clip.pre_roll_s, subtitles=subtitles,
    )  # fmt: skip
    copy_sidecar = {
        **sidecar,
        "clip": clip.name,
        "vertical": vertical.name,
        "captions": {**captions, "file": subtitles.name if subtitles else None,
                     "language": language, "text": " ".join(data["lines"])},
        "translation": {"of": int(row["id"]), "language": language},
    }  # fmt: skip
    copy_sidecar_path = source.with_name(f"{stem}.json")
    copy_sidecar_path.write_text(
        json.dumps(copy_sidecar, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    channel_id, spike_id = conn.execute(
        "SELECT channel_id, spike_id FROM clips WHERE id = ?", (row["id"],)
    ).fetchone()
    new_id = insert_clip(
        conn, channel_id=int(channel_id), spike_id=spike_id,
        start_ts=float(row["start_ts"]), end_ts=float(row["end_ts"]),
        path=clip, sidecar_path=copy_sidecar_path,
    )  # fmt: skip
    record = {
        "mode": "translation",
        "decision": "approve",
        "reason": f"{LANGUAGE_NAMES.get(language, language)} copy of #{row['id']}",
        "account": account.id,
        "applied": True,
        "at": now,
        "gemini": {"hashtags": list(clean_hashtags(data.get("hashtags") or []))},
    }
    conn.execute(
        "UPDATE clips SET vertical_path = ?, thumbnail_path = ?, title = ?, description = ?, "
        "language = ?, flags = '[]', translation_of = ?, auto = ? WHERE id = ?",
        (
            str(vertical), str(thumbnail), str(data.get("title", ""))[:100],
            str(data.get("description", ""))[:1000], language, int(row["id"]),
            json.dumps(record, ensure_ascii=False), new_id,
        ),
    )  # fmt: skip
    conn.commit()
    update_clip(config, conn, new_id, state="approved")
    return new_id
