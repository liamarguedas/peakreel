"""Typed configuration loader.

Configuration is read from ``config.yaml`` and validated into :class:`Config`.
A sibling ``config.local.yaml`` (gitignored) is deep-merged on top, so machine-specific
values such as absolute tool paths never have to be committed.

Every relative path in the configuration is resolved against the directory containing
``config.yaml``, never against the current working directory, so the CLI behaves the same
regardless of where it is launched from.

Examples
--------
>>> from pathlib import Path
>>> config = load_config(Path("config.yaml"))  # doctest: +SKIP
>>> config.output.width  # doctest: +SKIP
1080
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path
from typing import Annotated, Any, Literal, cast

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from peakreel.platforms import (
    KICK_PREFIX,
    StreamPlatform,
    channel_name,
    channel_url,
    split_key,
)

__all__ = [
    "LOCAL_OVERLAY_NAME",
    "CaptionsConfig",
    "ChannelConfig",
    "ChatConfig",
    "ClipConfig",
    "Config",
    "ConfigError",
    "DetectConfig",
    "DetectParams",
    "FacecamRegion",
    "LoggingConfig",
    "NotifyConfig",
    "OutputConfig",
    "PathsConfig",
    "RecordConfig",
    "RenderConfig",
    "ReviewConfig",
    "SupervisorConfig",
    "ToolsConfig",
    "deep_merge",
    "load_config",
    "resolve_tool",
]

LOCAL_OVERLAY_NAME = "config.local.yaml"

_Fraction = Annotated[float, Field(ge=0.0, lt=1.0)]
_PositiveFloat = Annotated[float, Field(gt=0.0)]
_PositiveInt = Annotated[int, Field(gt=0)]


class ConfigError(Exception):
    """Raised when the configuration cannot be read or fails validation.

    The message is written for the end user: it names the file and lists every
    offending field, so it can be printed as-is by the CLI.
    """


class _Model(BaseModel):
    """Base model: unknown keys are errors, so typos in ``config.yaml`` are caught."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class ToolsConfig(_Model):
    """Locations of external tools.

    Attributes
    ----------
    ffmpeg, ffprobe, streamlink : str
        Either a path (absolute, or relative to the config directory) or a bare
        command name. See :func:`resolve_tool` for the lookup rules.
    whisper_model_dir : pathlib.Path
        Directory where faster-whisper models are cached (used from M6).
    """

    ffmpeg: str = "ffmpeg"
    ffprobe: str = "ffprobe"
    streamlink: str = "streamlink"
    whisper_model_dir: Path = Path("models/whisper")


class PathsConfig(_Model):
    """Filesystem locations owned by Peakreel.

    Attributes
    ----------
    data_dir : pathlib.Path
        Root for all runtime data: chat logs, recordings, clips, SQLite, log files.
    blocklist : pathlib.Path
        The takedown list, see :mod:`peakreel.blocklist`.
    safety_words : pathlib.Path
        Your own blocked and ignored words for the safety checks, see
        :mod:`peakreel.safety.words`. Re-read for every clip.
    """

    data_dir: Path = Path("data")
    blocklist: Path = Path("blocklist.yaml")
    safety_words: Path = Path("safety.yaml")


class FacecamRegion(_Model):
    """Where a streamer's facecam sits in the source frame, as fractions of it.

    Attributes
    ----------
    x, y : float
        Top-left corner, 0 to 1 of the frame width and height.
    w, h : float
        Size, 0 to 1 of the frame width and height.

    Examples
    --------
    A facecam in the bottom-right fifth of the frame::

        facecam: {x: 0.8, y: 0.6, w: 0.2, h: 0.3}
    """

    x: _Fraction
    y: _Fraction
    w: Annotated[float, Field(gt=0.0, le=1.0)]
    h: Annotated[float, Field(gt=0.0, le=1.0)]

    @field_validator("h")
    @classmethod
    def _inside_frame(cls, value: float, info: ValidationInfo) -> float:
        x, y, w = info.data.get("x"), info.data.get("y"), info.data.get("w")
        if x is not None and w is not None and x + w > 1.0 + 1e-9:
            raise ValueError("facecam region extends past the right edge (x + w > 1)")
        if y is not None and y + value > 1.0 + 1e-9:
            raise ValueError("facecam region extends past the bottom edge (y + h > 1)")
        return value


Platform = Literal["youtube", "tiktok", "instagram", "facebook"]
"""Platforms Peakreel publishes to."""

PLATFORMS: tuple[Platform, ...] = ("youtube", "tiktok", "instagram", "facebook")

API_PLATFORMS: frozenset[Platform] = frozenset({"youtube", "instagram", "facebook"})
"""Platforms with an API adapter (others are export only)."""

PRIVATE_PLATFORMS: frozenset[Platform] = frozenset({"youtube", "facebook"})
"""Platforms that can hold a post privately (YouTube private video, Facebook draft)."""

_HANDLE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_LANGUAGE_CODE = re.compile(r"^[a-z]{2,3}$")


def _language(value: str) -> str:
    code = value.strip().lower()
    if not _LANGUAGE_CODE.match(code):
        raise ValueError(f"not an ISO 639-1 language code: {value!r} (use for example en, es, pt)")
    return code


_HASHTAG = re.compile(r"^\w{1,100}$")


class Handles(_Model):
    """A streamer's own accounts, used for @mentions on each platform.

    A platform without a handle gets no mention: handles are never guessed.

    Attributes
    ----------
    youtube, tiktok, instagram, facebook : str or None
        Account names without the leading ``@`` (Facebook: the Page's username).
    """

    youtube: str | None = None
    tiktok: str | None = None
    instagram: str | None = None
    facebook: str | None = None

    @field_validator("youtube", "tiktok", "instagram", "facebook")
    @classmethod
    def _normalise(cls, value: str | None) -> str | None:
        if value is None:
            return None
        handle = value.strip().lstrip("@")
        if not _HANDLE.match(handle):
            raise ValueError(f"not a valid handle: {value!r}")
        return handle


class ChannelConfig(_Model):
    """One watched channel, and how its clips are credited and published.

    Attributes
    ----------
    name : str
        Twitch login. Normalised to lower case without a leading ``#``.
    platform : {"twitch", "kick"}
        Where the channel streams. A Kick channel's ``name`` (its slug) becomes
        the key ``kick-<slug>`` used everywhere inside Peakreel.
    credit : str or None
        Credit text burned into every clip from this channel. When ``None``, use
        :attr:`credit_text`, which falls back to ``"Clip from twitch.tv/<name>"``
        (``kick.com/<slug>`` on Kick).
    facecam : FacecamRegion or None
    enabled : bool
        Allowed to run (the allowlist).
    display_name : str or None
        How the streamer is named in post text; defaults to :attr:`name`.
    handles : Handles
        The streamer's accounts per platform, for @mentions.
    hashtags : tuple of str
        Hashtags for every clip of this channel, without ``#``.
    language : str or None
        The language the streamer speaks (ISO 639-1, for example ``pt``). It picks
        which of your accounts their clips are posted to; see
        :class:`PublishAccount`.
    permission : {"granted", "credit_only", "unknown"}
        What the streamer agreed to. ``granted``: reposting allowed;
        ``credit_only``: allowed with credit; ``unknown``: not asked yet. Export
        packages are made for all three, with the status in their checklist;
        public posting through the APIs (later milestones) will require
        ``granted`` or ``credit_only``.
    """

    name: str
    platform: StreamPlatform = "twitch"
    credit: str | None = None
    facecam: FacecamRegion | None = None
    enabled: bool = True
    display_name: str | None = None
    handles: Handles = Handles()
    hashtags: tuple[str, ...] = ()
    language: str | None = None
    permission: Literal["granted", "credit_only", "unknown"] = "unknown"

    @field_validator("language")
    @classmethod
    def _check_language(cls, value: str | None) -> str | None:
        return _language(value) if value is not None else None

    @field_validator("hashtags")
    @classmethod
    def _clean_hashtags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        tags = tuple(tag.strip().lstrip("#") for tag in value)
        bad = [tag for tag in tags if not _HASHTAG.match(tag)]
        if bad:
            raise ValueError(f"hashtags may contain only letters, digits and _: {bad}")
        return tags

    @property
    def display(self) -> str:
        """The streamer's name for post text."""
        return self.display_name or channel_name(self.name)

    @model_validator(mode="before")
    @classmethod
    def _platform_key(cls, data: object) -> object:
        # `name: westcol, platform: kick` becomes the key `kick-westcol`.
        if not isinstance(data, dict):
            return data
        fields = cast("dict[str, object]", data)
        name = fields.get("name")
        if fields.get("platform") == "kick" and isinstance(name, str):
            slug = name.strip().lstrip("@").lower()
            if not slug.startswith(KICK_PREFIX):
                return {**fields, "name": KICK_PREFIX + slug}
        return fields

    @field_validator("name")
    @classmethod
    def _normalise_name(cls, value: str) -> str:
        name = value.strip().lstrip("#@").lower()
        if name.startswith(KICK_PREFIX):
            slug = name[len(KICK_PREFIX) :]
            if not slug or not all(c.isalnum() or c in "_-" for c in slug):
                raise ValueError(f"not a valid Kick channel: {value!r}")
            return name
        if not name or not all(c.isalnum() or c == "_" for c in name):
            raise ValueError(f"not a valid Twitch login: {value!r}")
        return name

    @model_validator(mode="after")
    def _platform_matches(self) -> ChannelConfig:
        if split_key(self.name)[0] != self.platform:
            raise ValueError(f"{self.name}: set `platform: kick` for a Kick channel")
        return self

    @property
    def credit_text(self) -> str:
        """Credit string to display, with the default applied."""
        return self.credit or f"Clip from {channel_url(self.name)}"


class ChatConfig(_Model):
    """Chat ingest settings.

    Attributes
    ----------
    url : str
        Websocket URL of the Twitch IRC gateway.
    bots : frozenset of str
        Logins flagged as ``filtered: bot``. Normalised to lower case.
    command_prefixes : tuple of str
        A message starting with any of these is flagged ``filtered: command``.
    keepalive_interval_s, keepalive_timeout_s : float
        Websocket ping cadence and pong deadline. Their sum bounds how long an
        outage can go undetected.
    connect_timeout_s : float
        Deadline for opening the websocket.
    reconnect_initial_delay_s, reconnect_max_delay_s : float
        Exponential backoff bounds for reconnects.
    join_batch_size : int
        Channels joined per batch (Twitch limits JOINs per 10 seconds).
    join_batch_interval_s : float
        Pause between JOIN batches.
    """

    url: str = "wss://irc-ws.chat.twitch.tv:443"
    bots: frozenset[str] = frozenset()
    command_prefixes: tuple[str, ...] = ("!",)
    keepalive_interval_s: _PositiveFloat = 2.0
    keepalive_timeout_s: _PositiveFloat = 3.0
    connect_timeout_s: _PositiveFloat = 5.0
    reconnect_initial_delay_s: _PositiveFloat = 0.5
    reconnect_max_delay_s: _PositiveFloat = 8.0
    join_batch_size: _PositiveInt = 20
    join_batch_interval_s: _PositiveFloat = 10.5

    @field_validator("bots", mode="before")
    @classmethod
    def _lower_bots(cls, value: object) -> object:
        if isinstance(value, list | set | frozenset | tuple):
            return frozenset(str(v).strip().lower() for v in cast("list[object]", value))
        return value


class OutputConfig(_Model):
    """Rendering targets and platform limits.

    Attributes
    ----------
    width, height, fps : int
        Output frame geometry and rate. Defaults: 1080x1920 at 30 fps.
    video_codec, pixel_format, audio_codec : str
        ffmpeg encoder settings.
    clip_min_s, clip_max_s : float
        Allowed clip length range in seconds.
    safe_zone_bottom, safe_zone_right : float
        Fractions of the frame, measured from the bottom and right edges, that
        captions and credit must stay out of (platform UI covers them).
    safe_zone_top : float
        Fraction of the frame from the top edge that the credit stays below
        (TikTok's "Following | For You" tabs, Instagram's top bar).
    """

    width: _PositiveInt = 1080
    height: _PositiveInt = 1920
    fps: _PositiveInt = 30
    video_codec: str = "libx264"
    pixel_format: str = "yuv420p"
    audio_codec: str = "aac"
    clip_min_s: _PositiveFloat = 20.0
    clip_max_s: _PositiveFloat = 45.0
    safe_zone_bottom: _Fraction = 0.20
    safe_zone_right: _Fraction = 0.12
    safe_zone_top: _Fraction = 0.10


class DetectParams(_Model):
    """Spike detector parameters.

    See :mod:`peakreel.detect.spikes` for how each one is used.

    Attributes
    ----------
    bucket_s : float
        Bucket width in seconds. Buckets are aligned to multiples of this value.
    baseline_buckets : int
        Number of previous valid buckets in the rolling baseline.
    warmup_buckets : int
        Valid buckets required in the baseline before anything can trigger.
    floor : int
        Minimum messages in a bucket for it to be hot, whatever the baseline.
    k : float
        A bucket is hot when its count exceeds ``median + k * MAD``.
    min_unique : int
        Minimum distinct chatters in a hot bucket.
    merge_gap : int
        Cold buckets allowed between hot buckets of one event.
    max_event_s : float
        An event is closed once it is this long.
    cooldown_s : float
        Quiet period after an event ends during which nothing triggers.
    raid_suppress_s : float
        An event starting within this long after a raid is suppressed.
    sub_train_min : int
        Sub, resub or gift notices within ``sub_train_window_s`` before or during an
        event that mark it as a sub train.
    sub_train_window_s : float
        Look-back for the sub train filter.
    min_unique_ratio : float
        Events with fewer distinct chatters per message than this are spam.
    reset_after_downtime_s : float
        A downtime longer than this clears the baseline, since chat levels may have
        changed (stream ended, new stream).
    top_messages : int
        Number of most frequent messages kept per event.
    """

    bucket_s: _PositiveFloat = 10.0
    baseline_buckets: _PositiveInt = 30
    warmup_buckets: _PositiveInt = 12
    floor: _PositiveInt = 10
    k: _PositiveFloat = 4.0
    min_unique: _PositiveInt = 5
    merge_gap: Annotated[int, Field(ge=0)] = 1
    max_event_s: _PositiveFloat = 120.0
    cooldown_s: Annotated[float, Field(ge=0.0)] = 60.0
    raid_suppress_s: Annotated[float, Field(ge=0.0)] = 90.0
    sub_train_min: _PositiveInt = 5
    sub_train_window_s: _PositiveFloat = 30.0
    min_unique_ratio: _Fraction = 0.3
    reset_after_downtime_s: _PositiveFloat = 300.0
    top_messages: _PositiveInt = 5


class DetectConfig(DetectParams):
    """The ``detect:`` section: default parameters plus a candidate overlay.

    Attributes
    ----------
    candidate : dict of str to float
        Overrides applied on top of the defaults to get a deliberately permissive
        parameter set. It produces the label sheet, a superset of what stricter
        settings detect.
    """

    candidate: dict[str, float] = Field(default_factory=dict[str, float])

    @field_validator("candidate")
    @classmethod
    def _known_candidate_keys(cls, value: dict[str, float]) -> dict[str, float]:
        unknown = sorted(set(value) - set(DetectParams.model_fields))
        if unknown:
            raise ValueError(f"unknown detector parameter(s): {', '.join(unknown)}")
        return value

    def params(self, overrides: dict[str, object] | None = None) -> DetectParams:
        """Return the default parameters with ``overrides`` applied.

        Parameters
        ----------
        overrides : dict, optional
            Parameter names to values. Validated like the config itself.

        Returns
        -------
        DetectParams

        Raises
        ------
        pydantic.ValidationError
            If a name is unknown or a value is out of range.
        """
        base = self.model_dump(exclude={"candidate"})
        return DetectParams.model_validate({**base, **(overrides or {})})

    def candidate_params(self) -> DetectParams:
        """Return the permissive parameter set used for the label sheet."""
        return self.params(dict(self.candidate))


class ClipConfig(_Model):
    """Live clipper settings (M4).

    Attributes
    ----------
    pre_roll_s : float
        Seconds of video before the spike start. Chat reacts late (stream delay plus
        typing), so the moment itself is usually well before the spike.
    post_roll_s : float
        Seconds of video after the spike start.
    max_wait_s : float
        How long to wait for the buffer to cover the window before cutting what is
        there (the gap is recorded in the sidecar).
    concurrent_cuts : int
        ffmpeg encodes allowed at once.
    outbox : pathlib.Path
        Where clips go, relative to ``paths.data_dir``.
    max_per_hour : int or None
        Clip budget: at most this many clips per channel in any rolling hour, so
        busy chats do not flood the review queue. A spike over budget is still
        recorded (``suppressed_by: budget``) but not cut. ``None``: no budget.
        Measured on 23 channels' chat: busy channels spike 8 to 12 times per hour;
        3 per hour (with ``keep_score`` 10) kept 114 of 311 spikes.
    keep_score : float
        A spike scoring at least this is clipped even over budget (about the top
        10 percent of spikes), so a huge moment is never lost.
    """

    pre_roll_s: Annotated[float, Field(ge=0.0)] = 20.0
    post_roll_s: Annotated[float, Field(ge=0.0)] = 10.0
    max_wait_s: _PositiveFloat = 90.0
    concurrent_cuts: _PositiveInt = 1
    outbox: Path = Path("outbox")
    max_per_hour: Annotated[int, Field(ge=1)] | None = 3
    keep_score: _PositiveFloat = 10.0


class CaptionsConfig(_Model):
    """Speech captions (M6).

    Attributes
    ----------
    enabled : bool
        Caption clips at all. When false (or on any failure) clips render without.
    model : str
        faster-whisper model name, for example ``"small"``.
    device, compute_type : str
        ``"cpu"`` and ``"int8"`` run on any machine.
    cpu_threads : int
        Threads for transcription; leave cores for recording and encoding.
    beam_size : int
        Beam search width. 1 (greedy) drops whole segments on noisy game audio.
    language : str or None
        Force a language code; ``None`` detects it per clip.
    vad_filter : bool
        Skip non-speech audio before transcribing.
    min_words : int
        Fewer words than this means "no speech": the clip renders without captions.
    max_line_chars : float
        Caption line width in Latin characters (CJK characters count 2).
    font_px, outline_px : int
        Caption size and outline width in output pixels.
    highlight_color : str
        ``#RRGGBB`` colour of the word being spoken.
    bottom_fraction : float
        Caption baseline as a fraction of the frame height. Must be above the
        bottom unsafe zone; the default also clears the credit overlay.
    side_margin_px : int
        Gap between the caption and the right unsafe zone; the left margin is
        the same, so captions stay centred on the frame.
    linger_s : float
        How long a line stays after its last word, unless the next line starts.
    """

    enabled: bool = True
    model: str = "small"
    device: str = "cpu"
    compute_type: str = "int8"
    cpu_threads: Annotated[int, Field(ge=0)] = 4
    beam_size: _PositiveInt = 5
    language: str | None = None
    vad_filter: bool = True
    min_words: Annotated[int, Field(ge=1)] = 3
    max_line_chars: _PositiveFloat = 20.0
    font_px: _PositiveInt = 64
    outline_px: Annotated[int, Field(ge=0)] = 5
    highlight_color: Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}$")] = "#FFE135"
    bottom_fraction: Annotated[float, Field(gt=0.3, lt=1.0)] = 0.70
    side_margin_px: Annotated[int, Field(ge=0)] = 48
    linger_s: Annotated[float, Field(ge=0.0)] = 0.3


class RenderConfig(_Model):
    """Vertical renderer settings (M5).

    Attributes
    ----------
    font_file : pathlib.Path or None
        Font for the credit overlay and captions. ``None`` picks the first installed of
        a list of fonts that also cover Japanese and Korean (channel names can be CJK).
    font_name : str or None
        Family name of ``font_file`` for libass (captions). Needed only for a custom
        ``font_file``; known fonts are mapped automatically.
    preset : str
        x264 preset; slower presets give smaller files at the same quality.
    crf : int
        x264 quality, lower is better (18 to 23 is typical).
    audio_bitrate : str
        AAC bitrate.
    blur_sigma : float
        Background blur strength for the ``blur`` layout.
    facecam_height_fraction : float
        Share of the output height given to the facecam in the ``split`` layout.
    credit_max_font_px : int
        Largest credit font size; smaller when the text would not fit the safe width.
    margin_px : int
        Distance of the credit from the left edge and from the bottom unsafe zone.
    """

    font_file: Path | None = None
    font_name: str | None = None
    preset: str = "veryfast"
    crf: Annotated[int, Field(ge=0, le=51)] = 20
    audio_bitrate: str = "160k"
    blur_sigma: _PositiveFloat = 30.0
    facecam_height_fraction: Annotated[float, Field(gt=0.1, lt=0.6)] = 0.35
    credit_max_font_px: _PositiveInt = 42
    margin_px: _PositiveInt = 48


class RecordConfig(_Model):
    """Stream recorder and rolling buffer settings (M3).

    Attributes
    ----------
    buffer_minutes : float
        How much recent video is kept per channel, measured on the broadcast clock.
    quality : str
        streamlink quality name, for example ``"best"`` or ``"720p60"``. At 1080p60
        Twitch sends about 1 MB/s, so 10 minutes is about 600 MB per channel.
    offline_retry_s : float
        How often an offline channel is checked again.
    prune_interval_s : float
        How often old segments are deleted.
    """

    buffer_minutes: _PositiveFloat = 10.0
    quality: str = "best"
    offline_retry_s: _PositiveFloat = 30.0
    prune_interval_s: _PositiveFloat = 30.0


class SupervisorConfig(_Model):
    """Live pipeline supervision (M7).

    Attributes
    ----------
    require_allowlist : bool
        Only channels listed (and enabled) under ``channels:`` may be run.
    restart_initial_s, restart_max_s : float
        Backoff between restarts of a crashed channel component.
    blocklist_check_s : float
        How often the takedown blocklist is re-read while running.
    status_interval_s : float
        How often ``data/run/status.json`` is rewritten.
    """

    require_allowlist: bool = True
    restart_initial_s: _PositiveFloat = 5.0
    restart_max_s: _PositiveFloat = 300.0
    blocklist_check_s: _PositiveFloat = 30.0
    status_interval_s: _PositiveFloat = 30.0


LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
"""Interfaces only this machine can reach."""


# Chat words and emotes that mean "something just happened" (one regex per word,
# matched against each word of a chat message, case-insensitive).
_EXCITEMENT_WORDS = (
    r"lo+l+",
    r"lmf?ao+",
    r"rofl",
    r"kekw?",
    r"lu+l+",
    r"omegalul",
    r"icant",
    r"no{3,}",
    r"gg+",
    r"w+",
    r"pog\w*",
    r"holy",
    r"omf?g+",
    r"wtf+",
    r"clip\w*",
    r"bro+",
    r"ayo+",
    r"(ha){2,}h?",
    r"xd+",
    r"(ja){2,}",
    r"(js){2,}",
    r"k{3,}",
    r"(ks){2,}",
    r"(rs){2,}",
    r"kkona\w*",
    r"\w*dance",
    r"\w*jam",
    r"pepeds",
    r"o{3,}",
    r"ayaya\w*",
    r"monka\w*",
    r"megalul",
    r"d:",
    r"u+f{2,}",
    r"\?{2,}",
    r"banger+",
    r"goty",
    r"sheesh+",
    r"insane",
    r"crazy",
    r"vamo+s?",
    r"caralho",
    r"nossa",
    r"草+",
    r"ｗ+",  # noqa: RUF001 - full-width w is Japanese laughter
    r"💀+",
    r"😭+",
    r"😂+",
    r"🤣+",
    r"🔥+",
)


class AutoReviewConfig(_Model):
    """Automatic review decisions, by rules (no AI).

    A finished clip is auto-rejected, auto-approved, or left for a person. In
    ``shadow`` mode the decision is only shown on the card, so it can be checked
    against your own decisions before it acts.

    Attributes
    ----------
    mode : {"off", "shadow", "on"}
    apply_rejects : bool
        Apply rejections even in ``shadow`` mode: the chat rules' (unexcited chat,
        music, no speech) and Gemini's (score at most ``ai_reject``). Those clips leave
        the New tab and retention deletes them; approvals stay advice until ``on``.
    excitement_words : list of str
        Regexes for excited chat words (``LOL``, ``NOOO``, ``GG``, ``KEKW``, ``jajaja``,
        ``kkkk``, ``草``...). Excitement is the share of the moment's top chat messages
        that contain one.
    reject_below_excitement : float
        Auto-reject when excitement is below this (a chat poll: "yes", "no").
    approve_excitement : float
        Auto-approve needs at least this excitement.
    approve_score : float
        Auto-approve needs at least this chat spike score.
    reject_flags : list of str
        Safety flags that auto-reject (``music``: copyright risk). Any other flag
        (``alerts``, ``blocked_words``) leaves the clip for a person: big streams show
        alerts almost constantly (measured: 28 of 45 launch-day clips).
    daily_cap : int
        Most auto-approvals per posting account per UTC day.
    gemini_models : list of str
        Gemini models (free tier) that watch each clip the rules did not reject, once
        a key is stored with ``peakreel gemini setup``; tried in order when one is
        busy. Without a key, or an empty list, the rules decide alone.
    ai_wait_min : float
        How long a clip may wait for Gemini (busy, quota) before the rules decide it
        alone.
    ai_per_pass : int
        Most Gemini calls per review pass (every 30 s), so a slow Gemini never delays
        anything else for long.
    ai_approve, ai_reject : int
        Gemini score (1 to 10) at or above which a clip may be auto-approved, and at
        or below which it is auto-rejected.
    """

    mode: Literal["off", "shadow", "on"] = "shadow"
    apply_rejects: bool = True
    excitement_words: list[str] = Field(default_factory=lambda: list(_EXCITEMENT_WORDS))
    reject_below_excitement: Annotated[float, Field(ge=0.0, le=1.0)] = 0.15
    approve_excitement: Annotated[float, Field(ge=0.0, le=1.0)] = 0.5
    approve_score: _PositiveFloat = 8.0
    reject_flags: list[str] = Field(default_factory=lambda: ["music"])
    daily_cap: Annotated[int, Field(ge=0)] = 6
    gemini_models: list[str] = Field(
        default_factory=lambda: ["gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.5-flash-lite"]
    )
    ai_wait_min: _PositiveFloat = 30.0
    ai_per_pass: Annotated[int, Field(ge=1)] = 3
    ai_approve: Annotated[int, Field(ge=1, le=10)] = 7
    ai_reject: Annotated[int, Field(ge=0, le=10)] = 4

    @field_validator("excitement_words")
    @classmethod
    def _compiles(cls, words: list[str]) -> list[str]:
        for word in words:
            try:
                re.compile(word)
            except re.error as exc:
                raise ValueError(f"not a valid regex: {word!r} ({exc})") from exc
        return words


class ReviewConfig(_Model):
    """Local review tool (M7).

    Attributes
    ----------
    host : str
        Interface to bind. ``127.0.0.1`` keeps it on this machine. Any other
        address makes the page require a password (``peakreel review password``).
    port : int
    export_dir : pathlib.Path
        Where approved clips are copied, relative to ``paths.data_dir``.
    public_hosts : list of str
        Extra host names the page answers to, such as this PC's Tailscale name
        (``pc.tail1234.ts.net``) when it is served with ``tailscale serve``.
        Setting any makes the page require a password.
    session_days : float
        How long a login lasts on a device.
    auto : AutoReviewConfig
        Automatic approve and reject decisions.
    """

    host: str = "127.0.0.1"
    port: Annotated[int, Field(gt=0, lt=65536)] = 8765
    export_dir: Path = Path("export")
    public_hosts: list[Annotated[str, Field(pattern=r"^[A-Za-z0-9.:\-\[\]]+$")]] = Field(
        default_factory=list[str]
    )
    session_days: _PositiveFloat = 30.0
    auto: AutoReviewConfig = Field(default_factory=lambda: AutoReviewConfig())

    def login_required(self, bind: str | None = None) -> bool:
        """Return whether the page can be reached from elsewhere, so needs a login.

        Parameters
        ----------
        bind : str, optional
            The interface actually bound (``--host``), default ``host``.

        Examples
        --------
        >>> ReviewConfig().login_required()
        False
        >>> ReviewConfig().login_required("0.0.0.0")
        True
        >>> ReviewConfig(public_hosts=["pc.tail1234.ts.net"]).login_required()
        True
        """
        return (bind or self.host) not in LOOPBACK_HOSTS or bool(self.public_hosts)


class NotifyConfig(_Model):
    """New-clip notifications (M7).

    Attributes
    ----------
    desktop : bool
        Windows toast notification for each new clip.
    ntfy_server : str
        ntfy server for phone push notifications.
    ntfy_topic : str or None
        ntfy topic; ``None`` disables phone pushes. On the public server anyone who
        knows the topic name can read it, so use a long random name or self-host.
    """

    desktop: bool = True
    ntfy_server: str = "https://ntfy.sh"
    ntfy_topic: str | None = None


class SafetyConfig(_Model):
    """Checks that flag a clip for the reviewer before it reaches the queue (M7a).

    Flags never reject a clip; they tell the reviewer what to look at.

    Attributes
    ----------
    languages : tuple of str
        Word lists downloaded by ``peakreel safety download`` (LDNOOBW language
        codes, for example ``en``, ``pt``, ``ja``). Every list is applied to every
        clip, because streams mix languages.
    word_lists_dir : pathlib.Path
        Where the downloaded lists live, relative to ``paths.data_dir``.
    music_threshold : float
        Beat-periodicity score (0 to 1) at or above which a clip is flagged as
        likely containing music. Speech and game audio measured 0.04 to 0.21;
        music with a steady beat scores far higher.
    alerts : bool
        Flag clips whose chat window contains subs, gifts, cheers or raids: their
        on-screen alerts usually show viewer names. Off by default since launch day:
        big streams show alerts almost constantly, and Gemini checks the screen for
        personal information itself.
    words : bool
        Flag clips with blocked words (LDNOOBW lists) in the transcript or top chat.
        Off by default since launch day: GTA streams swear constantly and YouTube
        allows it, so the flag only held good clips back.
    """

    languages: tuple[str, ...] = ("en", "pt", "es", "ja")
    word_lists_dir: Path = Path("safety")
    music_threshold: Annotated[float, Field(gt=0, le=1)] = 0.35
    alerts: bool = False
    words: bool = False


class DedupeConfig(_Model):
    """Cross-channel duplicate detection (M7a).

    Attributes
    ----------
    enabled : bool
    window_s : float
        Clips whose start times are this close are compared.
    min_strength : float
        Audio match strength (see :mod:`peakreel.dedupe`) at or above which two
        clips are the same moment. Unrelated clips measured at most 13.2; co-streams
        14 to 45 depending on how loud the co-streamer is over the shared audio.
    max_offset_s : float
        Largest delay between two streams of the same moment that is searched.
    """

    enabled: bool = True
    window_s: _PositiveFloat = 60.0
    min_strength: _PositiveFloat = 16.0
    max_offset_s: _PositiveFloat = 12.0


class GoLiveConfig(_Model):
    """Twitch Helix stream polling (M7a). Used only after ``peakreel twitch login``.

    Attributes
    ----------
    poll_s : float
        How often live state, game and title are fetched for all channels.
    """

    poll_s: _PositiveFloat = 60.0


class PlatformLimits(_Model):
    """Text limits and posting cadence of one platform.

    Limits change; keep them here, not in code. Each is enforced in the unit
    the platform counts in (characters, UTF-16 code units or UTF-8 bytes).

    Attributes
    ----------
    title_max : int or None
        Title length in characters (YouTube: 100). ``None``: no separate title.
    text_max : int
        Description or caption length, in ``text_unit``.
    text_unit : {"chars", "utf16", "bytes"}
        How ``text_max`` is counted: TikTok counts UTF-16 code units (an emoji
        is 2), YouTube's API counts description bytes.
    tags_max_chars : int or None
        Total length of the tag list (YouTube: 500, commas included).
    hashtags_max : int or None
        Most hashtags allowed in the text (Instagram: 5 since December 2025).
    mentions_max : int or None
        Most @mentions allowed (Instagram: 20).
    daily_cap : int
        Most posts per day on this platform.
    min_spacing_min : float
        Minimum minutes between two posts on this platform.
    """

    title_max: int | None = None
    text_max: _PositiveInt = 2200
    text_unit: Literal["chars", "utf16", "bytes"] = "chars"
    tags_max_chars: int | None = None
    hashtags_max: int | None = None
    mentions_max: int | None = None
    daily_cap: _PositiveInt = 10
    min_spacing_min: Annotated[float, Field(ge=0)] = 30.0


def _youtube() -> PlatformLimits:
    return PlatformLimits(title_max=100, text_max=5000, text_unit="bytes", tags_max_chars=500)


def _tiktok() -> PlatformLimits:
    return PlatformLimits(text_max=2200, text_unit="utf16")


def _instagram() -> PlatformLimits:
    return PlatformLimits(text_max=2200, hashtags_max=5, mentions_max=20)


def _facebook() -> PlatformLimits:
    # Reels description; Facebook allows far longer posts, but a Reel's text is read
    # on a phone over the video, so the same budget as Instagram.
    return PlatformLimits(text_max=2200)


class PublishAccount(_Model):
    """One of your own posting accounts (a set of channels on the platforms).

    Clips are routed by language: a clip goes to the account whose ``languages``
    contain the clip's language. An account with no ``languages`` takes clips of
    any language that no other account takes. A clip that no account takes is
    held for the reviewer, who can pick its language (and so its account) by hand.

    Attributes
    ----------
    id : str
        Short name, used in folder names and the database (for example ``en``).
    name : str
        Display name in the review tool.
    languages : tuple of str
        ISO 639-1 codes this account posts; empty means "any other language".
    platforms : tuple of str
        Platforms this account exists on.
    handles : Handles
        The account's own names per platform (shown in checklists; used by the
        platform adapters later).
    credit : str
        Credit line template in the account's language; must contain
        ``{channel_url}`` (``twitch.tv/<login>`` or ``kick.com/<slug>``; the older
        ``twitch.tv/{channel}`` is read the same way). ``{streamer}`` is the
        display name.
    hashtags : tuple of str
        Added to every post of this account, after the clip's own hashtags.
    templates : dict
        Per-platform text templates for this account (see ``publish.templates``).
    """

    id: Annotated[str, Field(pattern=r"^[a-z0-9_-]{1,32}$")]
    name: str = ""
    languages: tuple[str, ...] = ()
    platforms: tuple[Platform, ...] = PLATFORMS
    handles: Handles = Handles()
    credit: str = "Clip from {channel_url}"
    hashtags: tuple[str, ...] = ()
    templates: dict[Platform, dict[Literal["title", "text"], str]] = Field(
        default_factory=dict[Platform, dict[Literal["title", "text"], str]]
    )

    @field_validator("languages")
    @classmethod
    def _check_languages(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(_language(code) for code in value)

    @field_validator("credit")
    @classmethod
    def _check_credit(cls, value: str) -> str:
        if "{channel_url}" not in value and "twitch.tv/{channel}" not in value:
            raise ValueError("credit must contain {channel_url}")
        unknown = set(re.findall(r"{(\w*)}", value)) - {"channel", "streamer", "channel_url"}
        if unknown:
            raise ValueError(f"credit: unknown variables {sorted(unknown)}")
        return value

    @field_validator("hashtags")
    @classmethod
    def _clean_hashtags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return ChannelConfig._clean_hashtags(value)  # pyright: ignore[reportPrivateUsage]

    @field_validator("templates")
    @classmethod
    def _check_templates(
        cls, value: dict[Platform, dict[Literal["title", "text"], str]]
    ) -> dict[Platform, dict[Literal["title", "text"], str]]:
        return _check_templates(value)

    @property
    def label(self) -> str:
        """Name for display."""
        return self.name or self.id

    def credit_line(self, key: str, streamer: str) -> str:
        """Return the credit line for channel ``key`` (always with its address)."""
        template = self.credit.replace("twitch.tv/{channel}", "{channel_url}")
        return template.format(
            channel=channel_name(key), channel_url=channel_url(key), streamer=streamer
        )


def _check_templates(
    value: dict[Platform, dict[Literal["title", "text"], str]],
) -> dict[Platform, dict[Literal["title", "text"], str]]:
    allowed = {
        "title", "description", "streamer", "channel", "game", "mention", "credit",
        "source", "hashtags",
    }  # fmt: skip
    for platform, parts in value.items():
        if "{credit}" not in parts.get("text", "{credit}"):
            raise ValueError(f"templates.{platform}.text must contain {{credit}}")
        for name, template in parts.items():
            unknown = set(re.findall(r"{(\w*)}", template)) - allowed
            if unknown:
                raise ValueError(
                    f"templates.{platform}.{name}: unknown variables {sorted(unknown)}"
                )
    return value


def _default_accounts() -> tuple[PublishAccount, ...]:
    return (PublishAccount(id="main", name="Main"),)


class PublishConfig(_Model):
    """Publishing: metadata, queue and export packages (M9).

    Attributes
    ----------
    mode : {"export", "private", "public"}
        Default for every platform. ``export`` writes a package per platform for
        manual upload; ``private`` and ``public`` post through the platform's API.
    modes : dict
        Per-platform override, for example ``{youtube: public}`` while TikTok and
        Instagram stay on export. Only platforms with an adapter (YouTube) can post
        through the API (YouTube, Instagram, Facebook). A clip of a streamer whose
        ``permission`` is ``unknown`` is never posted publicly: it is posted
        ``private`` where the platform can (YouTube, Facebook draft), and exported
        otherwise (Instagram).
    platforms : tuple of str
        Platforms an approved clip is queued for by default.
    dir : pathlib.Path
        Where export packages go, relative to ``paths.data_dir``.
    hashtags : tuple of str
        Hashtags added to every clip, after the channel's and the game's.
    game_hashtag : bool
        Add a hashtag made from the stream's game (``#GrandTheftAutoVI``) when
        Twitch stream status is known.
    max_attempts : int
        Attempts before a publication is marked ``failed``.
    retry_initial_s : float
        Backoff after the first failure, doubling per attempt.
    templates : dict
        Per-platform overrides of the post templates, ``{platform: {"title": ...,
        "text": ...}}``; see :mod:`peakreel.publish.metadata` for the variables.
        ``text`` must contain ``{credit}``. An account's own templates win.
    accounts : tuple of PublishAccount
        Your posting accounts, routed by clip language. Each language may belong
        to one account only, and at most one account may have no languages.
        Spacing and daily caps apply per account and platform.
    youtube, tiktok, instagram, facebook : PlatformLimits
    """

    mode: Literal["export", "private", "public"] = "export"
    modes: dict[Platform, Literal["export", "private", "public"]] = Field(
        default_factory=dict[Platform, Literal["export", "private", "public"]]
    )
    platforms: tuple[Platform, ...] = PLATFORMS
    dir: Path = Path("publish")
    hashtags: tuple[str, ...] = ("twitch", "clips")
    game_hashtag: bool = True
    max_attempts: _PositiveInt = 5
    retry_initial_s: _PositiveFloat = 60.0
    templates: dict[Platform, dict[Literal["title", "text"], str]] = Field(
        default_factory=dict[Platform, dict[Literal["title", "text"], str]]
    )
    accounts: tuple[PublishAccount, ...] = Field(default_factory=_default_accounts)
    youtube: PlatformLimits = Field(default_factory=_youtube)
    tiktok: PlatformLimits = Field(default_factory=_tiktok)
    instagram: PlatformLimits = Field(default_factory=_instagram)
    facebook: PlatformLimits = Field(default_factory=_facebook)

    @model_validator(mode="before")
    @classmethod
    def _merge_platform_defaults(cls, data: object) -> object:
        # `tiktok: {daily_cap: 3}` must keep TikTok's own limits (UTF-16, 2200), not
        # fall back to the generic defaults of PlatformLimits.
        if not isinstance(data, dict):
            return data
        merged = dict(cast("dict[str, object]", data))
        for name, default in (
            ("youtube", _youtube),
            ("tiktok", _tiktok),
            ("instagram", _instagram),
            ("facebook", _facebook),
        ):
            override = merged.get(name)
            if isinstance(override, dict):
                merged[name] = {**default().model_dump(), **cast("dict[str, object]", override)}
        return merged

    @field_validator("hashtags")
    @classmethod
    def _clean_hashtags(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return ChannelConfig._clean_hashtags(value)  # pyright: ignore[reportPrivateUsage]

    @field_validator("templates")
    @classmethod
    def _check_templates(
        cls, value: dict[Platform, dict[Literal["title", "text"], str]]
    ) -> dict[Platform, dict[Literal["title", "text"], str]]:
        return _check_templates(value)

    @field_validator("accounts")
    @classmethod
    def _check_accounts(cls, value: tuple[PublishAccount, ...]) -> tuple[PublishAccount, ...]:
        ids = [a.id for a in value]
        if len(set(ids)) != len(ids):
            raise ValueError(f"account ids must be unique: {ids}")
        seen: dict[str, str] = {}
        for account in value:
            for code in account.languages:
                if code in seen:
                    raise ValueError(
                        f"language {code!r} is on accounts {seen[code]} and {account.id}"
                    )
                seen[code] = account.id
        if sum(1 for a in value if not a.languages) > 1:
            raise ValueError("at most one account may take every other language (no `languages`)")
        return value

    @model_validator(mode="after")
    def _check_adapters(self) -> PublishConfig:
        for platform in PLATFORMS:
            mode = self.mode_for(platform)
            if mode != "export" and platform not in API_PLATFORMS:
                raise ValueError(
                    f"{platform} can only be exported for now (no API adapter yet); "
                    f"set publish.modes.{platform}: export"
                )
            if mode == "private" and platform not in PRIVATE_PLATFORMS:
                raise ValueError(f"{platform} has no private posts; use public or export")
        return self

    def mode_for(self, platform: Platform) -> Literal["export", "private", "public"]:
        """Return how ``platform`` is published (``modes`` override, else ``mode``)."""
        return self.modes.get(platform, self.mode)

    def account(self, account_id: str) -> PublishAccount | None:
        """Return the account with ``account_id``, or ``None``."""
        return next((a for a in self.accounts if a.id == account_id), None)

    def account_for(self, language: str | None) -> PublishAccount | None:
        """Return the account that posts clips in ``language``, or ``None`` (hold).

        The account listing the language wins; otherwise the catch-all account
        (one with no ``languages``), if any.
        """
        if language is not None:
            for account in self.accounts:
                if language in account.languages:
                    return account
        return next((a for a in self.accounts if not a.languages), None)

    def limits(self, platform: Platform) -> PlatformLimits:
        """Return the limits of ``platform``."""
        limits: PlatformLimits = getattr(self, platform)
        return limits


class KickConfig(_Model):
    """Kick chat through Kick's official webhooks (docs/kick.md).

    Kick calls one public HTTPS address per developer app; a tunnel (Cloudflare
    Tunnel or ngrok) forwards it to the endpoint Peakreel serves here.

    Attributes
    ----------
    webhook_host : str
        Interface the endpoint listens on (the tunnel connects to it).
    webhook_port : int
    webhook_path : str
        Path of the endpoint; the app's webhook URL is ``<public address><path>``.
    public_url : str or None
        The tunnel's public address, for display only.
    max_age_s : float
        Webhook calls older than this (by their signed timestamp) are refused.
    """

    webhook_host: str = "127.0.0.1"
    webhook_port: Annotated[int, Field(ge=0, lt=65536)] = 8770  # 0: any free port (tests)
    webhook_path: str = "/kick/webhook"
    public_url: str | None = None
    max_age_s: _PositiveFloat = 600.0


class DiscoverConfig(_Model):
    """Record the top live streamers of a Twitch category automatically.

    Needs ``peakreel twitch login``. Every ``refresh_min`` the supervisor asks
    Twitch for the category's live streams (by viewers), keeps those in
    ``languages`` with at least ``min_viewers``, and runs the top ones next to the
    channels listed under ``channels:``, up to ``max_channels`` in total. A
    discovered channel is dropped when it goes offline or stays out of the top
    ``max_channels * 1.5`` for ``keep_min`` minutes, so a small ranking change does
    not cut a recording. Blocklisted and ``exclude`` channels are never taken.

    Attributes
    ----------
    enabled : bool
    platforms : tuple of {"twitch", "kick"}
        Where to discover; Kick needs ``peakreel kick setup`` and the webhook.
    category : str
        Twitch category (game) name, exactly as on Twitch.
    kick_category : str
        The same category's name on Kick, exactly as on Kick.
    languages : tuple of str or None
        Stream languages to take; ``None``: the languages of ``publish.accounts``
        (clips in other languages could not be posted).
    max_channels : int
        Most channels recorded at once, manual ones included.
    min_viewers : int
    refresh_min : float
    keep_min : float
    permission : {"granted", "credit_only", "unknown"}
        Permission given to discovered streamers (they were not asked).
    exclude : tuple of str
        Channels never to discover.
    """

    enabled: bool = False
    platforms: tuple[StreamPlatform, ...] = ("twitch",)
    category: str = "Grand Theft Auto VI"
    kick_category: str = "Grand Theft Auto VI"
    languages: tuple[str, ...] | None = None
    max_channels: _PositiveInt = 10
    min_viewers: Annotated[int, Field(ge=0)] = 500
    refresh_min: _PositiveFloat = 5.0
    keep_min: Annotated[float, Field(ge=0)] = 20.0
    permission: Literal["granted", "credit_only", "unknown"] = "credit_only"
    exclude: tuple[str, ...] = ()

    @field_validator("languages")
    @classmethod
    def _check_languages(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        return None if value is None else tuple(_language(code) for code in value)

    @field_validator("exclude")
    @classmethod
    def _lower(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(name.strip().lstrip("#").lower() for name in value)


class RetentionConfig(_Model):
    """What is deleted, and when, so an unattended run never fills the disk (M8).

    Approved clips are never deleted. A deleted ("purged") clip keeps its database
    row and sidecar, so every decision stays traceable; only the media files go.

    Attributes
    ----------
    rejected_days : float
        Rejected and taken-down clips are purged after this many days.
    new_days : float
        Clips never reviewed (including duplicates) are purged after this many days.
    publish_days : float
        Export packages are deleted this many days after they were written.
    chat_days : float
        Chat logs older than this are deleted (M2 labeling needs recent ones).
    min_free_gb : float
        Below this much free disk, the oldest clips that are not approved are
        purged early (oldest first, never one younger than an hour) until there
        is room again, and the status shows a warning.
    interval_min : float
        How often the running pipeline cleans up.
    """

    rejected_days: _PositiveFloat = 2.0
    new_days: _PositiveFloat = 7.0
    publish_days: _PositiveFloat = 14.0
    chat_days: _PositiveFloat = 90.0
    min_free_gb: Annotated[float, Field(ge=0)] = 20.0
    interval_min: _PositiveFloat = 30.0


class HealthConfig(_Model):
    """Thresholds for the warnings in ``status.json`` and the review page (M8).

    Attributes
    ----------
    max_memory_mb : float
        Warn when the process uses more memory than this.
    max_handles : int
        Warn above this many OS handles (Windows) or open files: a steady climb
        means a leak.
    max_captions_backlog : int
        Warn when more clips than this wait for captions (CPU cannot keep up).
    max_clips_in_flight : int
        Warn when more clips than this are being cut or rendered at once.
    """

    max_memory_mb: _PositiveFloat = 3000.0
    max_handles: _PositiveInt = 5000
    max_captions_backlog: _PositiveInt = 6
    max_clips_in_flight: _PositiveInt = 10


class LoggingConfig(_Model):
    """Logging settings.

    Attributes
    ----------
    level : {"DEBUG", "INFO", "WARNING", "ERROR"}
        Minimum level for both console and file output.
    file : pathlib.Path
        JSON-lines log file, relative to ``paths.data_dir``.
    max_bytes : int
        Size at which the log file rotates.
    backup_count : int
        Number of rotated files kept.
    """

    level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    file: Path = Path("logs/peakreel.jsonl")
    max_bytes: _PositiveInt = 10 * 1024 * 1024
    backup_count: Annotated[int, Field(ge=0)] = 5


class Config(_Model):
    """Validated Peakreel configuration.

    Build it with :func:`load_config` rather than directly, so that ``root`` is set
    and the local overlay is applied.

    Attributes
    ----------
    root : pathlib.Path
        Directory containing ``config.yaml``. All relative paths resolve against it.
    tools : ToolsConfig
    paths : PathsConfig
    channels : tuple of ChannelConfig
    chat : ChatConfig
    detect : DetectConfig
    record : RecordConfig
    clip : ClipConfig
    render : RenderConfig
    captions : CaptionsConfig
    supervisor : SupervisorConfig
    review : ReviewConfig
    notify : NotifyConfig
    safety : SafetyConfig
    dedupe : DedupeConfig
    golive : GoLiveConfig
    discover : DiscoverConfig
    kick : KickConfig
    publish : PublishConfig
    retention : RetentionConfig
    health : HealthConfig
    output : OutputConfig
    logging : LoggingConfig
    """

    root: Path = Field(default_factory=Path.cwd, exclude=True)
    tools: ToolsConfig = ToolsConfig()
    paths: PathsConfig = PathsConfig()
    channels: tuple[ChannelConfig, ...] = ()
    chat: ChatConfig = ChatConfig()
    detect: DetectConfig = DetectConfig()
    record: RecordConfig = RecordConfig()
    clip: ClipConfig = ClipConfig()
    render: RenderConfig = RenderConfig()
    captions: CaptionsConfig = CaptionsConfig()
    supervisor: SupervisorConfig = SupervisorConfig()
    review: ReviewConfig = ReviewConfig()
    notify: NotifyConfig = NotifyConfig()
    safety: SafetyConfig = SafetyConfig()
    dedupe: DedupeConfig = DedupeConfig()
    golive: GoLiveConfig = GoLiveConfig()
    discover: DiscoverConfig = DiscoverConfig()
    kick: KickConfig = KickConfig()
    publish: PublishConfig = PublishConfig()
    retention: RetentionConfig = RetentionConfig()
    health: HealthConfig = HealthConfig()
    output: OutputConfig = OutputConfig()
    logging: LoggingConfig = LoggingConfig()

    @field_validator("channels")
    @classmethod
    def _unique_channels(cls, value: tuple[ChannelConfig, ...]) -> tuple[ChannelConfig, ...]:
        names = [c.name for c in value]
        duplicates = sorted({n for n in names if names.count(n) > 1})
        if duplicates:
            raise ValueError(f"duplicate channels: {', '.join(duplicates)}")
        return value

    @model_validator(mode="after")
    def _captions_above_unsafe_zone(self) -> Config:
        limit = 1.0 - self.output.safe_zone_bottom
        if self.captions.bottom_fraction > limit:
            raise ValueError(
                f"captions.bottom_fraction {self.captions.bottom_fraction} puts captions in the "
                f"bottom unsafe zone (must be <= {limit:.2f})"
            )
        return self

    def resolve(self, path: Path) -> Path:
        """Resolve ``path`` against the config directory.

        Parameters
        ----------
        path : pathlib.Path
            Absolute paths are returned unchanged; relative ones are joined to
            :attr:`root`.

        Returns
        -------
        pathlib.Path
        """
        return path if path.is_absolute() else self.root / path

    @property
    def data_dir(self) -> Path:
        """Absolute runtime data directory."""
        return self.resolve(self.paths.data_dir)

    @property
    def chat_log_dir(self) -> Path:
        """Directory holding per-channel chat JSONL logs."""
        return self.data_dir / "chat"

    @property
    def db_path(self) -> Path:
        """Location of the SQLite database."""
        return self.data_dir / "peakreel.db"

    @property
    def blocklist_path(self) -> Path:
        """Absolute path of the takedown blocklist."""
        return self.resolve(self.paths.blocklist)

    @property
    def safety_words_path(self) -> Path:
        """Absolute path of the user's safety word file."""
        return self.resolve(self.paths.safety_words)

    @property
    def word_lists_dir(self) -> Path:
        """Directory holding the downloaded blocked-word lists."""
        return self.data_dir / self.safety.word_lists_dir

    @property
    def publish_dir(self) -> Path:
        """Where per-platform export packages are written."""
        return self.data_dir / self.publish.dir

    @property
    def run_dir(self) -> Path:
        """Runtime coordination files: stop files, locks, status."""
        return self.data_dir / "run"

    @property
    def export_dir(self) -> Path:
        """Where approved clips are exported."""
        return self.data_dir / self.review.export_dir

    @property
    def outbox_dir(self) -> Path:
        """Directory receiving finished clips and their sidecars."""
        return self.data_dir / self.clip.outbox

    @property
    def buffer_dir(self) -> Path:
        """Directory holding the rolling video buffer, one subdirectory per channel."""
        return self.data_dir / "buffer"

    @property
    def log_file(self) -> Path:
        """Absolute path of the JSON-lines log file."""
        return self.data_dir / self.logging.file

    def channel(self, name: str) -> ChannelConfig | None:
        """Return the configured channel called ``name``, or ``None``.

        Parameters
        ----------
        name : str
            Channel key: a Twitch login, or ``kick-<slug>``; case-insensitive,
            with or without a leading ``#``.
        """
        wanted = name.strip().lstrip("#").lower()
        return next((c for c in self.channels if c.name == wanted), None)


def deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Merge ``overlay`` into ``base`` without mutating either.

    Mappings are merged key by key, recursively. Any other value in ``overlay``
    (scalars and lists included) replaces the value in ``base``.

    Parameters
    ----------
    base : dict
        Lower-priority values.
    overlay : dict
        Higher-priority values.

    Returns
    -------
    dict
        A new merged mapping.

    Examples
    --------
    >>> deep_merge({"a": {"x": 1, "y": 2}, "b": [1]}, {"a": {"y": 3}, "b": [2]})
    {'a': {'x': 1, 'y': 3}, 'b': [2]}
    """
    merged = dict(base)
    for key, value in overlay.items():
        current = merged.get(key)
        if isinstance(current, dict) and isinstance(value, dict):
            merged[key] = deep_merge(cast("dict[str, Any]", current), cast("dict[str, Any]", value))
        else:
            merged[key] = value
    return merged


def _read_yaml_mapping(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")
    return cast("dict[str, Any]", raw)


def load_config(path: Path, *, use_local_overlay: bool = True) -> Config:
    """Load, merge and validate the configuration.

    Parameters
    ----------
    path : pathlib.Path
        Path to ``config.yaml``.
    use_local_overlay : bool, default True
        When true and a ``config.local.yaml`` exists next to ``path``, it is
        deep-merged on top (see :func:`deep_merge`).

    Returns
    -------
    Config
        The validated configuration, with ``root`` set to the directory of ``path``.

    Raises
    ------
    ConfigError
        If a file is missing, unreadable, not valid YAML, or fails validation.
        The message lists every invalid field.

    Examples
    --------
    >>> config = load_config(Path("config.yaml"))  # doctest: +SKIP
    >>> config.data_dir.is_absolute()  # doctest: +SKIP
    True
    """
    path = path.resolve()
    if not path.is_file():
        raise ConfigError(f"config file not found: {path}")
    data = _read_yaml_mapping(path)
    sources = str(path)
    overlay_path = path.with_name(LOCAL_OVERLAY_NAME)
    if use_local_overlay and overlay_path.is_file():
        data = deep_merge(data, _read_yaml_mapping(overlay_path))
        sources = f"{path} (+ {overlay_path.name})"
    if "root" in data:
        raise ConfigError(f"invalid configuration in {sources}:\n  root: not a configurable key")
    try:
        return Config.model_validate({**data, "root": path.parent})
    except ValidationError as exc:
        lines = [
            f"  {'.'.join(str(p) for p in err['loc']) or '<root>'}: {err['msg']}"
            for err in exc.errors()
        ]
        raise ConfigError(f"invalid configuration in {sources}:\n" + "\n".join(lines)) from exc


def resolve_tool(value: str, root: Path) -> Path | None:
    """Locate an external tool from its configured value.

    Parameters
    ----------
    value : str
        The configured value. If it contains a path separator or is absolute it is
        treated as a path (relative paths resolve against ``root``). Otherwise it is
        a bare command name.
    root : pathlib.Path
        The config directory.

    Returns
    -------
    pathlib.Path or None
        The executable path, or ``None`` when it cannot be found.

    Notes
    -----
    Bare command names are looked up next to the running Python interpreter first,
    which is where tools installed into the project virtualenv live (for example
    ``streamlink``), and only then on ``PATH``. This keeps the lookup independent of
    whether the virtualenv is activated.
    """
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute() or len(candidate.parts) > 1:
        candidate = candidate if candidate.is_absolute() else root / candidate
        return candidate if candidate.is_file() else None
    scripts_dir = Path(sys.executable).parent
    found = shutil.which(value, path=str(scripts_dir)) or shutil.which(value)
    return Path(found) if found else None
