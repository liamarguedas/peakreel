"""Minimal Twitch Helix client: app tokens, user lookup, live streams and archived VODs.

Only what Peakreel needs is implemented, with the standard library's HTTP client so
no extra dependency is pulled in. Credentials (client ID and secret of a
dev.twitch.tv application) live in the operating system's credential store via
:mod:`keyring` (Windows Credential Manager on Windows), never in config files.

The main entry point for the rest of the code is :class:`VodLocator`, which answers
"which VOD, at which offset, shows this moment?".
"""

from __future__ import annotations

import contextlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, cast

import keyring
import keyring.errors

from peakreel.log import get_logger

__all__ = [
    "KEYRING_SERVICE",
    "Credentials",
    "HelixClient",
    "HelixError",
    "StreamInfo",
    "Vod",
    "VodLocator",
    "delete_credentials",
    "load_credentials",
    "parse_duration",
    "save_credentials",
    "vod_url",
]

log = get_logger(__name__)

KEYRING_SERVICE = "peakreel"
_CLIENT_ID_KEY = "twitch_client_id"
_CLIENT_SECRET_KEY = "twitch_client_secret"  # noqa: S105 (a key name, not a secret)
API_BASE = "https://api.twitch.tv/helix"
AUTH_BASE = "https://id.twitch.tv/oauth2"
_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$")


class HelixError(Exception):
    """Raised when a Helix request fails. The message is safe to show to users."""


@dataclass(frozen=True, slots=True)
class Credentials:
    """Client credentials of a dev.twitch.tv application.

    Attributes
    ----------
    client_id : str
    client_secret : str
        Never logged or printed.
    """

    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        """Hide the secret."""
        return f"Credentials(client_id={self.client_id!r}, client_secret='***')"


def load_credentials() -> Credentials | None:
    """Read the stored Twitch credentials.

    Returns
    -------
    Credentials or None
        ``None`` when nothing is stored (run ``peakreel twitch login``) or the
        credential store is unavailable.
    """
    try:
        client_id = keyring.get_password(KEYRING_SERVICE, _CLIENT_ID_KEY)
        secret = keyring.get_password(KEYRING_SERVICE, _CLIENT_SECRET_KEY)
    except keyring.errors.KeyringError as exc:
        log.warning("credential store unavailable", error=str(exc))
        return None
    if not client_id or not secret:
        return None
    return Credentials(client_id, secret)


def save_credentials(credentials: Credentials) -> None:
    """Store Twitch credentials in the operating system's credential store.

    Parameters
    ----------
    credentials : Credentials

    Raises
    ------
    keyring.errors.KeyringError
        If the credential store rejects the write.
    """
    keyring.set_password(KEYRING_SERVICE, _CLIENT_ID_KEY, credentials.client_id)
    keyring.set_password(KEYRING_SERVICE, _CLIENT_SECRET_KEY, credentials.client_secret)


def delete_credentials() -> None:
    """Remove stored Twitch credentials. Missing entries are ignored."""
    for key in (_CLIENT_ID_KEY, _CLIENT_SECRET_KEY):
        with contextlib.suppress(keyring.errors.PasswordDeleteError):
            keyring.delete_password(KEYRING_SERVICE, key)


def parse_duration(value: str) -> int:
    """Parse a Helix duration such as ``"3h8m33s"`` into seconds.

    Parameters
    ----------
    value : str

    Returns
    -------
    int

    Raises
    ------
    ValueError
        If the format is not recognised.

    Examples
    --------
    >>> parse_duration("3h8m33s"), parse_duration("45s"), parse_duration("1h")
    (11313, 45, 3600)
    """
    match = _DURATION.match(value.strip())
    if not value.strip() or match is None:
        raise ValueError(f"not a Twitch duration: {value!r}")
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


@dataclass(frozen=True, slots=True)
class Vod:
    """An archived broadcast (past broadcast VOD).

    Attributes
    ----------
    id : str
    created_at : float
        Broadcast start, UTC epoch seconds (Twitch's clock).
    duration_s : int
        Current length. Grows while the broadcast is live.
    """

    id: str
    created_at: float
    duration_s: int

    def covers(self, ts: float) -> bool:
        """Whether Twitch-clock time ``ts`` falls inside this VOD."""
        return self.created_at <= ts < self.created_at + self.duration_s


@dataclass(frozen=True, slots=True)
class StreamInfo:
    """A live stream as Helix reports it.

    Attributes
    ----------
    login : str
    game : str
        Category name, for example ``"Grand Theft Auto VI"``.
    title : str
    viewers : int
    started_ts : float
        Stream start, UTC epoch seconds.
    language : str
        Broadcast language code.
    """

    login: str
    game: str
    title: str
    viewers: int
    started_ts: float
    language: str

    def to_json(self) -> dict[str, Any]:
        """Return the fields as a JSON-ready mapping (stored in clip sidecars)."""
        return {
            "game": self.game,
            "title": self.title,
            "viewers": self.viewers,
            "started_ts": self.started_ts,
            "language": self.language,
        }


def _stream_info(row: Mapping[str, Any]) -> StreamInfo:
    started = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
    return StreamInfo(
        login=str(row["user_login"]).lower(),
        game=str(row.get("game_name") or ""),
        title=str(row.get("title") or ""),
        viewers=int(row.get("viewer_count") or 0),
        started_ts=started.timestamp(),
        language=str(row.get("language") or ""),
    )


def vod_url(vod: Vod, offset_s: float) -> str:
    """Link to ``offset_s`` seconds into ``vod``.

    Parameters
    ----------
    vod : Vod
    offset_s : float
        Clamped to the VOD's range.

    Returns
    -------
    str

    Examples
    --------
    >>> vod_url(Vod("123", 0.0, 7200), 3725.9)
    'https://www.twitch.tv/videos/123?t=1h02m05s'
    """
    seconds = int(min(max(offset_s, 0.0), max(vod.duration_s - 1, 0)))
    hours, rest = divmod(seconds, 3600)
    minutes, secs = divmod(rest, 60)
    return f"https://www.twitch.tv/videos/{vod.id}?t={hours}h{minutes:02d}m{secs:02d}s"


class HelixClient:
    """Authenticated access to the Helix endpoints Peakreel uses.

    Uses an app access token (client-credentials flow), fetched lazily and refreshed
    when it expires or the API answers 401.

    Parameters
    ----------
    credentials : Credentials
    api_base, auth_base : str
        Endpoints. Overridable for tests.
    timeout_s : float, default 10.0
        Per-request timeout.
    clock : callable, optional
        Returns epoch seconds; used for token expiry.
    """

    def __init__(
        self,
        credentials: Credentials,
        *,
        api_base: str = API_BASE,
        auth_base: str = AUTH_BASE,
        timeout_s: float = 10.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._credentials = credentials
        self._api_base = api_base.rstrip("/")
        self._auth_base = auth_base.rstrip("/")
        self._timeout_s = timeout_s
        self._clock = clock
        self._token: str | None = None
        self._token_expires = 0.0

    def _http(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None
    ) -> Any:
        request = urllib.request.Request(url, data=body, method=method, headers=dict(headers))  # noqa: S310
        try:
            with urllib.request.urlopen(request, timeout=self._timeout_s) as response:  # noqa: S310
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            raise HelixError(
                f"HTTP {exc.code} from {urllib.parse.urlsplit(url).path}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise HelixError(f"cannot reach Twitch: {exc}") from exc
        except json.JSONDecodeError as exc:
            raise HelixError(f"Twitch sent invalid JSON: {exc}") from exc

    def token(self) -> str:
        """Return a valid app access token, fetching a new one when needed.

        Returns
        -------
        str

        Raises
        ------
        HelixError
            If Twitch rejects the credentials or cannot be reached.
        """
        if self._token is not None and self._clock() < self._token_expires - 60:
            return self._token
        body = urllib.parse.urlencode(
            {
                "client_id": self._credentials.client_id,
                "client_secret": self._credentials.client_secret,
                "grant_type": "client_credentials",
            }
        ).encode()
        data = self._http(
            "POST",
            f"{self._auth_base}/token",
            {"Content-Type": "application/x-www-form-urlencoded"},
            body,
        )
        if not isinstance(data, dict) or "access_token" not in data:
            raise HelixError("Twitch token response has no access_token")
        payload = cast("dict[str, Any]", data)
        self._token = str(payload["access_token"])
        self._token_expires = self._clock() + float(payload.get("expires_in", 3600))
        return self._token

    def get(
        self, path: str, params: Mapping[str, str] | Sequence[tuple[str, str]]
    ) -> dict[str, Any]:
        """Call a Helix GET endpoint.

        Parameters
        ----------
        path : str
            Endpoint path such as ``"/users"``.
        params : mapping or sequence of (str, str)
            Query parameters; use pairs to repeat a key (``user_login=a&user_login=b``).

        Returns
        -------
        dict
            The decoded JSON body.

        Raises
        ------
        HelixError
            On HTTP errors (after one token refresh on 401) or network failures.
        """
        url = f"{self._api_base}{path}?{urllib.parse.urlencode(params)}"
        for attempt in range(2):
            headers = {
                "Client-Id": self._credentials.client_id,
                "Authorization": f"Bearer {self.token()}",
            }
            try:
                data = self._http("GET", url, headers)
            except HelixError as exc:
                if attempt == 0 and str(exc).startswith("HTTP 401"):
                    self._token = None  # expired or revoked: fetch a new one and retry once
                    continue
                raise
            if not isinstance(data, dict):
                raise HelixError(f"unexpected response from {path}")
            return cast("dict[str, Any]", data)
        raise HelixError("unreachable")  # pragma: no cover

    def user_id(self, login: str) -> str | None:
        """Return the numeric user id of ``login``, or ``None`` if it does not exist."""
        rows = cast("list[dict[str, Any]]", self.get("/users", {"login": login}).get("data") or [])
        return str(rows[0]["id"]) if rows else None

    def live_streams(self, logins: Sequence[str]) -> dict[str, StreamInfo]:
        """Return the live streams among ``logins`` (offline channels are absent).

        Parameters
        ----------
        logins : sequence of str
            Channel logins; any number (queried 100 at a time).

        Returns
        -------
        dict of str to StreamInfo
            Keyed by lower-case login.

        Raises
        ------
        HelixError
        """
        live: dict[str, StreamInfo] = {}
        names = sorted({login.lower() for login in logins})
        for start in range(0, len(names), 100):
            params = [("user_login", name) for name in names[start : start + 100]]
            params.append(("first", "100"))
            for row in cast("list[dict[str, Any]]", self.get("/streams", params).get("data") or []):
                if row.get("type") != "live":
                    continue
                info = _stream_info(row)
                live[info.login] = info
        return live

    def game_id(self, name: str) -> str | None:
        """Return the id of the category called ``name`` (exact Twitch name), or ``None``."""
        rows = cast("list[dict[str, Any]]", self.get("/games", {"name": name}).get("data") or [])
        return str(rows[0]["id"]) if rows else None

    def top_streams(
        self, game_id: str, languages: Sequence[str] | None = None, limit: int = 100
    ) -> list[StreamInfo]:
        """Return the category's live streams, most viewers first.

        Parameters
        ----------
        game_id : str
        languages : sequence of str, optional
            Stream languages to keep (Twitch filters them).
        limit : int, default 100
            Most streams returned (fetched 100 per page).

        Raises
        ------
        HelixError
        """
        streams: list[StreamInfo] = []
        cursor: str | None = None
        while len(streams) < limit:
            params = [("game_id", game_id), ("type", "live"), ("first", "100")]
            params += [("language", code) for code in languages or ()]
            if cursor:
                params.append(("after", cursor))
            data = self.get("/streams", params)
            for row in cast("list[dict[str, Any]]", data.get("data") or []):
                streams.append(_stream_info(row))
            cursor = cast("dict[str, Any]", data.get("pagination") or {}).get("cursor")
            if not cursor or not data.get("data"):
                break
        streams.sort(key=lambda s: s.viewers, reverse=True)
        return streams[:limit]

    def archived_vods(self, user_id: str) -> list[Vod]:
        """Return the user's past-broadcast VODs, newest first (up to 100)."""
        data = self.get("/videos", {"user_id": user_id, "type": "archive", "first": "100"})
        vods: list[Vod] = []
        for row in cast("list[dict[str, Any]]", data.get("data") or []):
            created = datetime.fromisoformat(str(row["created_at"]).replace("Z", "+00:00"))
            vods.append(
                Vod(str(row["id"]), created.timestamp(), parse_duration(str(row["duration"])))
            )
        return vods


class VodLocator:
    """Find the VOD and offset for a moment, with an on-disk cache.

    Parameters
    ----------
    client : HelixClient
    cache_dir : pathlib.Path
        Holds ``users.json`` (login to id, permanent) and ``vods-<login>.json``.
    refresh_after_s : float, default 600
        A cached VOD list older than this is refetched when it does not cover the
        requested time (the broadcast may still be growing, or be new).
    clock : callable, optional
        Returns epoch seconds.
    """

    def __init__(
        self,
        client: HelixClient,
        cache_dir: Path,
        *,
        refresh_after_s: float = 600.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._client = client
        self._dir = cache_dir
        self._refresh_after_s = refresh_after_s
        self._clock = clock

    def _read(self, name: str) -> dict[str, Any]:
        path = self._dir / name
        try:
            return cast("dict[str, Any]", json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write(self, name: str, data: dict[str, Any]) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)
        (self._dir / name).write_text(json.dumps(data, indent=1), encoding="utf-8")

    def _user_id(self, login: str) -> str | None:
        users = self._read("users.json")
        if login not in users:
            users[login] = self._client.user_id(login)
            self._write("users.json", users)
        value = users[login]
        return str(value) if value else None

    def _vods(self, login: str, user_id: str, *, refresh: bool) -> list[Vod]:
        name = f"vods-{login}.json"
        cached = self._read(name)
        fresh = self._clock() - float(cached.get("fetched_at", 0)) < self._refresh_after_s
        if (refresh and not fresh) or "vods" not in cached:
            vods = self._client.archived_vods(user_id)
            cached = {
                "fetched_at": self._clock(),
                "vods": [[v.id, v.created_at, v.duration_s] for v in vods],
            }
            self._write(name, cached)
        rows = cast("list[list[Any]]", cached["vods"])
        return [Vod(str(row[0]), float(row[1]), int(row[2])) for row in rows]

    def locate(self, login: str, twitch_ts: float) -> tuple[Vod | None, str]:
        """Find the VOD that contains ``twitch_ts``.

        Parameters
        ----------
        login : str
            Channel login.
        twitch_ts : float
            The moment on Twitch's clock, UTC epoch seconds.

        Returns
        -------
        tuple of (Vod or None, str)
            The VOD and an empty string, or ``None`` and the reason no link can be
            given (unknown channel, no archive covering that time).

        Raises
        ------
        HelixError
            If Twitch cannot be queried.
        """
        user_id = self._user_id(login)
        if user_id is None:
            return None, f"no Twitch user named {login!r}"
        for refresh in (False, True):
            for vod in self._vods(login, user_id, refresh=refresh):
                if vod.covers(twitch_ts):
                    return vod, ""
        return None, "no archived VOD covers this time (archives disabled, deleted, or expired)"
