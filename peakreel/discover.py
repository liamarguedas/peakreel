"""Choose which top streamers of a category to record (``discover:`` in the config).

A pure function over Twitch's stream list, so the rules are easy to test and to
reason about; the supervisor applies its result (start and stop channels).

Rules:

- Candidates are the category's live streams in the allowed languages with at
  least ``min_viewers``, most viewers first; blocklisted, excluded and manually
  configured channels are not candidates (manual channels always run).
- Discovered channels fill the slots left by manual ones, up to ``max_channels``.
- A running discovered channel is kept while it is live and within the wider
  top (``slots * 1.5``); once it falls out of that, it is kept ``keep_min`` more
  minutes, so a passing ranking change does not cut a recording. It is dropped
  at once when it goes offline or leaves the category.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field

from peakreel.config import DiscoverConfig
from peakreel.platforms import split_key
from peakreel.twitch.helix import StreamInfo

__all__ = ["DiscoveryPlan", "plan_discovery"]


@dataclass(frozen=True, slots=True)
class DiscoveryPlan:
    """What to change.

    Attributes
    ----------
    add : list of StreamInfo
        Streams to start recording, most viewers first.
    remove : dict of str to str
        Discovered channels to stop, with the reason.
    seen : dict of str to float
        For each discovered channel kept, when it was last within the wider top.
    """

    add: list[StreamInfo] = field(default_factory=list[StreamInfo])
    remove: dict[str, str] = field(default_factory=dict[str, str])
    seen: dict[str, float] = field(default_factory=dict[str, float])


def plan_discovery(
    streams: Sequence[StreamInfo],
    *,
    settings: DiscoverConfig,
    languages: Collection[str],
    manual: Collection[str],
    discovered: Mapping[str, float],
    blocked: Collection[str],
    now: float,
) -> DiscoveryPlan:
    """Decide which discovered channels to start and stop.

    Parameters
    ----------
    streams : sequence of StreamInfo
        The category's live streams (any order).
    settings : DiscoverConfig
    languages : collection of str
        Stream languages allowed.
    manual : collection of str
        Channels configured by hand (always run, use slots first).
    discovered : mapping of str to float
        Discovered channels running now, with when each was last in the wider top.
    blocked : collection of str
        The takedown blocklist.
    now : float

    Returns
    -------
    DiscoveryPlan

    Examples
    --------
    >>> from peakreel.config import DiscoverConfig
    >>> def s(login, viewers): return StreamInfo(login, "GTA VI", "", viewers, 0.0, "en")
    >>> plan = plan_discovery(
    ...     [s("a", 9000), s("b", 5000), s("c", 100)], settings=DiscoverConfig(max_channels=2),
    ...     languages={"en"}, manual=[], discovered={}, blocked=[], now=0.0)
    >>> [x.login for x in plan.add]
    ['a', 'b']

    With ``max_per_group``, each platform and language fills its own slots, under
    the overall ``max_channels``:

    >>> def k(login, viewers, lang): return StreamInfo(login, "GTA VI", "", viewers, 0.0, lang)
    >>> plan = plan_discovery(
    ...     [s("a", 9000), s("b", 5000), k("kick-k", 900, "en"), k("p", 800, "pt")],
    ...     settings=DiscoverConfig(max_per_group=1, max_channels=3),
    ...     languages={"en", "pt"}, manual=[], discovered={}, blocked=[], now=0.0)
    >>> [x.login for x in plan.add]
    ['a', 'kick-k', 'p']
    """
    if settings.max_per_group is not None:
        return _per_group(
            streams, settings=settings, languages=languages, manual=manual,
            discovered=discovered, blocked=blocked, now=now,
        )  # fmt: skip
    skip = {*blocked, *settings.exclude, *manual}
    eligible = sorted(
        (
            s
            for s in streams
            if s.language in languages and s.viewers >= settings.min_viewers and s.login not in skip
        ),
        key=lambda s: s.viewers,
        reverse=True,
    )
    slots = max(0, settings.max_channels - len(manual))
    wide = {s.login for s in eligible[: math.ceil(slots * 1.5)]}
    live = {s.login for s in streams}
    plan = DiscoveryPlan()
    for login, last_seen in discovered.items():
        if login in blocked:
            plan.remove[login] = "blocklisted"
        elif login not in live:
            plan.remove[login] = "offline or left the category"
        elif login in wide:
            plan.seen[login] = now
        elif now - last_seen > settings.keep_min * 60:
            plan.remove[login] = f"out of the top {len(wide)} for {settings.keep_min:g} min"
        else:
            plan.seen[login] = last_seen
    running = len(plan.seen)
    for stream in eligible[:slots]:
        if running >= slots:
            break
        if stream.login in plan.seen:
            continue
        plan.add.append(stream)
        plan.seen[stream.login] = now
        running += 1
    return plan


def _per_group(
    streams: Sequence[StreamInfo],
    *,
    settings: DiscoverConfig,
    languages: Collection[str],
    manual: Collection[str],
    discovered: Mapping[str, float],
    blocked: Collection[str],
    now: float,
) -> DiscoveryPlan:
    """Plan each (platform, language) with ``max_per_group`` slots, then apply the cap."""
    group_size = settings.max_per_group or settings.max_channels
    each = settings.model_copy(update={"max_channels": group_size, "max_per_group": None})
    language_of = {s.login: s.language for s in streams}

    def group(key: str) -> tuple[str, str]:
        return split_key(key)[0], language_of.get(key, "")

    plan = DiscoveryPlan()
    rank: dict[str, int] = {}  # a new stream's place within its group
    for g in sorted({group(s.login) for s in streams} | {group(k) for k in discovered}):
        part = plan_discovery(
            [s for s in streams if group(s.login) == g],
            settings=each, languages=languages, manual=manual,
            discovered={key: seen for key, seen in discovered.items() if group(key) == g},
            blocked=blocked, now=now,
        )  # fmt: skip
        rank.update({stream.login: i for i, stream in enumerate(part.add)})
        plan.add.extend(part.add)
        plan.remove.update(part.remove)
        plan.seen.update(part.seen)
    # Overall cap: channels already running stay; new ones are taken in turns across the
    # groups (every group's first, then every group's second...), so a small language
    # is not starved by the biggest ones.
    room = max(0, settings.max_channels - len(manual) - (len(plan.seen) - len(plan.add)))
    plan.add.sort(key=lambda s: (rank[s.login], -s.viewers))
    for dropped in plan.add[room:]:
        del plan.seen[dropped.login]
    del plan.add[room:]
    return plan
