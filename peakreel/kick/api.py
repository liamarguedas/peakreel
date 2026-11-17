"""Kick's official API (api.kick.com): channels, category streams, event subscriptions.

Uses an app access token (client credentials flow at id.kick.com) from a Kick
developer app, stored in the operating system's credential store by
``peakreel kick setup``. Standard library HTTP only, like the Twitch client.
"""

from __future__ import annotations

import contextlib
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

import keyring
import keyring.errors

from peakreel.platforms import channel_key
from peakreel.twitch.helix import StreamInfo

__all__ = [
    "API_BASE",
    "AUTH_BASE",
    "CHAT_EVENTS",
    "KickApi",
    "KickChannel",
    "KickCredentials",
    "KickError",
    "delete_credentials",
    "load_credentials",
    "save_credentials",
]

API_BASE = "https://api.kick.com"
AUTH_BASE = "https://id.kick.com"
_KEYRING_SERVICE = "peakreel"
_KEY = "kick_app"

CHAT_EVENTS: tuple[tuple[str, int], ...] = (
    ("chat.message.sent", 1),
    ("channel.subscription.new", 1),
    ("channel.subscription.renewal", 1),
    ("channel.subscription.gifts", 1),
    ("kicks.gifted", 1),
)
"""Events Peakreel subscribes to per channel: chat, and the ones that show on-screen alerts."""


class KickError(Exception):
    """A Kick API request failed; the message is safe to show."""


@dataclass(frozen=True, slots=True)
class KickCredentials:
    """A Kick developer app (kick.com/settings/developer)."""

    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        """Hide the secret."""
        return f"KickCredentials(client_id={self.client_id!r}, client_secret='***')"


def save_credentials(credentials: KickCredentials) -> None:
    """Store the Kick app in the credential store."""
    payload = json.dumps(
        {"client_id": credentials.client_id, "client_secret": credentials.client_secret}
    )
    keyring.set_password(_KEYRING_SERVICE, _KEY, payload)


def load_credentials() -> KickCredentials | None:
    """Return the stored Kick app, or ``None``."""
    try:
        raw = keyring.get_password(_KEYRING_SERVICE, _KEY)
    except keyring.errors.KeyringError:
        return None
    if not raw:
        return None
    data = json.loads(raw)
    return KickCredentials(data["client_id"], data["client_secret"])


def delete_credentials() -> None:
    """Forget the Kick app (missing entries are ignored)."""
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(_KEYRING_SERVICE, _KEY)


@dataclass(frozen=True, slots=True)
class KickChannel:
    """A Kick channel as the API reports it."""

    slug: str
    broadcaster_user_id: int
    live: bool
    title: str
    category: str
    viewers: int
    language: str
    started_ts: float | None

    def stream(self) -> StreamInfo | None:
        """Return the live stream as a :class:`StreamInfo` (key ``kick-<slug>``), or ``None``."""
        if not self.live:
            return None
        return StreamInfo(
            channel_key("kick", self.slug), self.category, self.title, self.viewers,
            self.started_ts or 0.0, self.language,
        )  # fmt: skip


def _ts(value: object) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _language(value: object) -> str:
    # BCP 47 ("en", "pt-BR") to the ISO 639-1 code accounts use.
    return str(value or "").split("-")[0].lower()


class KickApi:
    """The Kick API calls Peakreel needs.

    Parameters
    ----------
    credentials : KickCredentials
    api_base, auth_base : str
        Endpoints; overridable for tests.
    timeout_s : float, default 15.0
    clock : callable, optional
    """

    def __init__(
        self,
        credentials: KickCredentials,
        *,
        api_base: str = API_BASE,
        auth_base: str = AUTH_BASE,
        timeout_s: float = 15.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.credentials = credentials
        self.api_base = api_base.rstrip("/")
        self.auth_base = auth_base.rstrip("/")
        self.timeout_s = timeout_s
        self._clock = clock
        self._token: str | None = None
        self._expires = 0.0

    def _http(
        self, method: str, url: str, headers: Mapping[str, str], body: bytes | None = None
    ) -> Any:
        request = urllib.request.Request(  # noqa: S310 - https endpoints (http only in tests)
            url, data=body, method=method,
            headers={"Accept": "application/json", "User-Agent": "peakreel", **headers},
        )  # fmt: skip
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            detail = exc.read()[:300].decode("utf-8", "replace")
            raise KickError(
                f"Kick HTTP {exc.code} {urllib.parse.urlsplit(url).path}: {detail}"
            ) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise KickError(f"cannot reach Kick: {exc}") from exc
        if not raw:
            return {}
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return raw.decode("utf-8", "replace")

    def token(self) -> str:
        """Return a valid app access token (client credentials), refreshed when needed."""
        if self._token and self._clock() < self._expires - 60:
            return self._token
        body = urllib.parse.urlencode(
            {"grant_type": "client_credentials", "client_id": self.credentials.client_id,
             "client_secret": self.credentials.client_secret}
        ).encode()  # fmt: skip
        data = self._http(
            "POST", f"{self.auth_base}/oauth/token",
            {"Content-Type": "application/x-www-form-urlencoded"}, body,
        )  # fmt: skip
        if not isinstance(data, dict) or "access_token" not in data:
            raise KickError("Kick token response has no access_token (check the app id and secret)")
        payload = cast("dict[str, Any]", data)
        self._token = str(payload["access_token"])
        self._expires = self._clock() + float(payload.get("expires_in", 3600))
        return self._token

    def _call(
        self,
        method: str,
        path: str,
        params: Sequence[tuple[str, str]] = (),
        body: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        url = f"{self.api_base}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(list(params))
        headers = {"Authorization": f"Bearer {self.token()}"}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        result = self._http(method, url, headers, data)
        return cast("dict[str, Any]", result) if isinstance(result, dict) else {}

    def channels(self, slugs: Sequence[str]) -> dict[str, KickChannel]:
        """Look up channels by slug (50 per request); unknown slugs are absent."""
        found: dict[str, KickChannel] = {}
        names = sorted({s.lower() for s in slugs})
        for start in range(0, len(names), 50):
            data = self._call(
                "GET", "/public/v1/channels", [("slug", n) for n in names[start : start + 50]]
            )
            for row in cast("list[dict[str, Any]]", data.get("data") or []):
                stream = cast("dict[str, Any]", row.get("stream") or {})
                category = cast("dict[str, Any]", row.get("category") or {})
                slug = str(row.get("slug") or "").lower()
                found[slug] = KickChannel(
                    slug=slug,
                    broadcaster_user_id=int(row["broadcaster_user_id"]),
                    live=bool(stream.get("is_live")),
                    title=str(row.get("stream_title") or ""),
                    category=str(category.get("name") or ""),
                    viewers=int(stream.get("viewer_count") or 0),
                    language=_language(stream.get("language")),
                    started_ts=_ts(stream.get("start_time")),
                )
        return found

    def category_id(self, name: str) -> int | None:
        """Return the id of the category called exactly ``name``, or ``None``."""
        data = self._call("GET", "/public/v2/categories", [("name", name)])
        rows = cast("list[dict[str, Any]]", data.get("data") or [])
        return int(rows[0]["id"]) if rows else None

    def top_streams(
        self, category_id: int, languages: Sequence[str] | None = None, limit: int = 100
    ) -> list[StreamInfo]:
        """Return the category's live streams, most viewers first (keys ``kick-<slug>``).

        The API does not sort, so every page (100 streams each, up to 10) is read
        and sorted here.
        """
        streams: list[StreamInfo] = []
        cursor = ""
        for _page in range(10):
            params = [("category_id", str(category_id)), ("limit", "100")]
            params += [("language_code", code) for code in languages or ()]
            if cursor:
                params.append(("cursor", cursor))
            data = self._call("GET", "/public/v2/livestreams", params)
            for row in cast("list[dict[str, Any]]", data.get("data") or []):
                channel = cast("dict[str, Any]", row.get("channel") or {})
                category = cast("dict[str, Any]", row.get("category") or {})
                streams.append(
                    StreamInfo(
                        channel_key("kick", str(channel.get("slug") or "")),
                        str(category.get("name") or ""),
                        str(row.get("title") or ""),
                        int(row.get("viewer_count") or 0),
                        _ts(row.get("started_at")) or 0.0,
                        _language(row.get("language_code")),
                    )
                )
            cursor = str(
                cast("dict[str, Any]", data.get("pagination") or {}).get("next_cursor") or ""
            )
            if not cursor or not data.get("data"):
                break
        streams.sort(key=lambda s: s.viewers, reverse=True)
        return streams[:limit]

    def subscribe(
        self, broadcaster_user_id: int, events: Sequence[tuple[str, int]] = CHAT_EVENTS
    ) -> list[str]:
        """Subscribe this app's webhook to ``events`` of a channel; return subscription ids.

        Raises
        ------
        KickError
            If Kick refuses any of the events.
        """
        body = {
            "broadcaster_user_id": broadcaster_user_id,
            "events": [{"name": name, "version": version} for name, version in events],
            "method": "webhook",
        }
        data = self._call("POST", "/public/v1/events/subscriptions", body=body)
        ids: list[str] = []
        errors: list[str] = []
        for row in cast("list[dict[str, Any]]", data.get("data") or []):
            if row.get("error"):
                errors.append(f"{row.get('name')}: {row['error']}")
            elif row.get("subscription_id"):
                ids.append(str(row["subscription_id"]))
        if errors:
            raise KickError("Kick refused subscriptions: " + "; ".join(errors))
        return ids

    def subscriptions(self) -> list[dict[str, Any]]:
        """Return the app's event subscriptions (id, broadcaster_user_id, event, ...)."""
        data = self._call("GET", "/public/v1/events/subscriptions")
        return cast("list[dict[str, Any]]", data.get("data") or [])

    def unsubscribe(self, ids: Sequence[str]) -> None:
        """Delete event subscriptions."""
        if ids:
            self._call("DELETE", "/public/v1/events/subscriptions", [("id", i) for i in ids])

    def public_key(self) -> str:
        """Kick's current webhook signing key (PEM). Never hard-code it: it rotates."""
        data = self._http("GET", f"{self.api_base}/public/v1/public-key", {})
        if isinstance(data, str):
            return data
        payload = cast("dict[str, Any]", data)
        inner: object = payload.get("data")
        key: object = (
            cast("dict[str, Any]", inner).get("public_key")
            if isinstance(inner, dict)
            else payload.get("public_key")
        )
        if not key:
            raise KickError("Kick returned no public key")
        return str(key)
