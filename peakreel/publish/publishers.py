"""Publishers: the interface every platform adapter implements, and the export one.

A publisher turns an approved clip plus its :class:`PostMetadata` into a post.
Only :class:`ExportPublisher` exists so far: it writes a package per platform
for manual upload. The YouTube, TikTok and Instagram adapters (M10 to M12)
implement the same :class:`Publisher` protocol with ``private`` and ``public``
modes.

Export package (``data/publish/<account>/<platform>/<YYYY-MM-DD>/<clip>/``)::

    video.mp4         the vertical render (horizontal if there is none)
    thumbnail.jpg     when rendered
    title.txt         YouTube only
    description.txt   YouTube: the description; TikTok and Instagram: the caption
    tags.txt          YouTube only, comma-separated
    checklist.md      steps to post, suggested time, permission, flags
    metadata.json     everything above, machine-readable

Packages are written to a temporary folder and renamed, so a half-written one is
never visible, and writing one again replaces it (exporting is idempotent).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, Protocol

from peakreel.config import ChannelConfig, Platform
from peakreel.publish.metadata import PostMetadata

__all__ = [
    "PLATFORM_NAMES",
    "ClipPackage",
    "ExportPublisher",
    "PublishError",
    "PublishMode",
    "PublishResult",
    "Publisher",
]

PublishMode = Literal["export", "private", "public"]

PLATFORM_NAMES: dict[Platform, str] = {
    "youtube": "YouTube Shorts",
    "tiktok": "TikTok",
    "instagram": "Instagram Reels",
}

_STEPS: dict[Platform, list[str]] = {
    "youtube": [
        "Open YouTube Studio > Create > Upload videos, choose `video.mp4`.",
        "Paste `title.txt` as the title and `description.txt` as the description.",
        "Paste `tags.txt` into Tags (Show more).",
        "Audience: No, it's not made for kids. Category: Gaming.",
        "Visibility: as planned (Private to check first, or Schedule at the suggested time).",
    ],
    "tiktok": [
        "Open TikTok > + > Upload, choose `video.mp4`.",
        "Paste `description.txt` as the caption.",
        "Check visibility, comments, duet and stitch settings.",
        "Content disclosure: this is a third-party clip; do not mark it as your own original.",
        "Post, or save as draft and post at the suggested time.",
    ],
    "instagram": [
        "Open Instagram > + > Reel, choose `video.mp4`.",
        "Paste `description.txt` as the caption (at most 5 hashtags).",
        "Optionally choose `thumbnail.jpg` as the cover.",
        "Share at the suggested time.",
    ],
}


class PublishError(RuntimeError):
    """Raised when a publication cannot be made; the message is shown to the reviewer.

    Parameters
    ----------
    message : str
    retry_after_s : float or None, optional
        Set for a temporary refusal (a quota): the queue tries again after this
        long without counting an attempt.

    Attributes
    ----------
    status : int or None
        HTTP status, for network publishers.
    reason : str
        Platform error reason, for network publishers.
    """

    def __init__(self, message: str, *, retry_after_s: float | None = None) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s
        self.status: int | None = None
        self.reason = ""


@dataclass(frozen=True, slots=True)
class ClipPackage:
    """Everything a publisher needs about one clip.

    Attributes
    ----------
    clip_id : int
    name : str
        Clip file stem, unique per clip.
    video : pathlib.Path
        The file to post (vertical render, else the horizontal clip).
    thumbnail : pathlib.Path or None
    channel : ChannelConfig
    start_utc : str or None
    flags : list of dict
        Safety flags, shown in the checklist.
    scheduled_utc : str or None
        Suggested posting time.
    account : str
        Id of the posting account (one of yours, per language).
    """

    clip_id: int
    name: str
    video: Path
    thumbnail: Path | None
    channel: ChannelConfig
    start_utc: str | None
    flags: list[dict[str, str]]
    scheduled_utc: str | None = None
    account: str = "main"


@dataclass(frozen=True, slots=True)
class PublishResult:
    """Outcome of :meth:`Publisher.publish`.

    Attributes
    ----------
    state : {"exported", "published", "processing", "private_only"}
    remote_id : str or None
        Platform video id (network publishers).
    url : str or None
        Post URL, or the export folder.
    """

    state: Literal["exported", "published", "processing", "private_only"]
    remote_id: str | None = None
    url: str | None = None


class Publisher(Protocol):
    """What every platform adapter provides."""

    platform: Platform
    modes: frozenset[PublishMode]

    def validate(self, clip: ClipPackage, meta: PostMetadata) -> list[str]:
        """Return problems that prevent publishing ``clip`` (empty if none)."""
        ...

    def publish(self, clip: ClipPackage, meta: PostMetadata, mode: PublishMode) -> PublishResult:
        """Post ``clip``; raise :class:`PublishError` on failure."""
        ...

    def poll_status(self, clip: ClipPackage, remote_id: str | None) -> PublishResult | None:
        """Return the current state of an earlier publish, or ``None`` if unknown."""
        ...

    def remove(self, clip: ClipPackage, url: str | None) -> bool:
        """Undo a publication where possible; ``False`` if it must be removed by hand."""
        ...


def _write_atomic(target: Path, files: dict[str, bytes | Path]) -> None:
    temp = target.with_name(target.name + ".part")
    shutil.rmtree(temp, ignore_errors=True)
    temp.mkdir(parents=True)
    for name, content in files.items():
        if isinstance(content, Path):
            shutil.copyfile(content, temp / name)
        else:
            (temp / name).write_bytes(content)
    shutil.rmtree(target, ignore_errors=True)
    temp.rename(target)


class ExportPublisher:
    """Writes a ready-to-post package for manual upload.

    Parameters
    ----------
    platform : {"youtube", "tiktok", "instagram"}
    root : pathlib.Path
        ``config.publish_dir``.
    """

    modes: frozenset[PublishMode] = frozenset({"export"})

    def __init__(self, platform: Platform, root: Path) -> None:
        self.platform: Platform = platform
        self.root = root

    def package_dir(self, clip: ClipPackage) -> Path:
        """Where the package of ``clip`` is written."""
        day = (clip.start_utc or "unknown")[:10]
        return self.root / clip.account / self.platform / day / clip.name

    def validate(self, clip: ClipPackage, meta: PostMetadata) -> list[str]:
        """Return problems that prevent exporting (a missing video file)."""
        return [] if clip.video.is_file() else [f"video file missing: {clip.video}"]

    def _checklist(self, clip: ClipPackage, meta: PostMetadata) -> str:
        name = PLATFORM_NAMES[self.platform]
        channel = clip.channel
        lines = [f"# {name}: {clip.name}", ""]
        if clip.scheduled_utc:
            lines.append(f"Suggested post time: {clip.scheduled_utc}")
        permission = {
            "granted": "granted: reposting allowed",
            "credit_only": "credit only: allowed with credit (it is in the text)",
            "unknown": "UNKNOWN: you have not asked this streamer; post at your own risk",
        }[channel.permission]
        lines += [f"Streamer: {channel.display} (twitch.tv/{channel.name})"]
        lines += [f"Permission: {permission}", ""]
        if clip.flags:
            lines.append("Flags to check first:")
            lines += [f"- [ ] {flag['message']}" for flag in clip.flags]
            lines.append("")
        lines.append("Before posting:")
        lines.append("- [ ] Watch it: no personal information (alerts with names, addresses, DMs).")
        lines.append(f"- [ ] The credit line is in the text: {meta.credit}")
        lines += ["", "Steps:"]
        lines += [f"{n}. {step}" for n, step in enumerate(_STEPS[self.platform], 1)]
        if meta.notes:
            lines += ["", "Notes on the text:"]
            lines += [f"- {note}" for note in meta.notes]
        return "\n".join(lines) + "\n"

    def publish(self, clip: ClipPackage, meta: PostMetadata, mode: PublishMode) -> PublishResult:
        """Write (or rewrite) the package.

        Raises
        ------
        PublishError
            For a mode other than ``export``, or a missing video.
        """
        if mode != "export":
            raise PublishError(f"{PLATFORM_NAMES[self.platform]}: only export mode exists yet")
        problems = self.validate(clip, meta)
        if problems:
            raise PublishError("; ".join(problems))
        files: dict[str, bytes | Path] = {"video.mp4": clip.video}
        if clip.thumbnail is not None and clip.thumbnail.is_file():
            files["thumbnail.jpg"] = clip.thumbnail
        if meta.title is not None:
            files["title.txt"] = (meta.title + "\n").encode("utf-8")
        files["description.txt"] = (meta.text + "\n").encode("utf-8")
        if meta.tags:
            files["tags.txt"] = (", ".join(meta.tags) + "\n").encode("utf-8")
        files["checklist.md"] = self._checklist(clip, meta).encode("utf-8")
        record = {
            "platform": self.platform,
            "clip_id": clip.clip_id,
            "clip": clip.name,
            "channel": clip.channel.name,
            "permission": clip.channel.permission,
            "suggested_post_utc": clip.scheduled_utc,
            "post": meta.to_json(),
            "flags": clip.flags,
            "exported_utc": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        files["metadata.json"] = (json.dumps(record, ensure_ascii=False, indent=2) + "\n").encode()
        target = self.package_dir(clip)
        _write_atomic(target, files)
        return PublishResult("exported", url=str(target))

    def poll_status(self, clip: ClipPackage, remote_id: str | None) -> PublishResult | None:
        """Return ``exported`` if the package exists, else ``None``."""
        target = self.package_dir(clip)
        return PublishResult("exported", url=str(target)) if target.is_dir() else None

    def remove(self, clip: ClipPackage, url: str | None) -> bool:
        """Delete the package (the export has not been posted by Peakreel)."""
        shutil.rmtree(self.package_dir(clip), ignore_errors=True)
        return True
