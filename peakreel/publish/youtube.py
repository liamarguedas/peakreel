"""YouTube Shorts publisher (M10): OAuth, resumable upload, processing status.

Standard library only (``urllib``), like the Twitch client.

Setup, once (see ``docs/m10-youtube.md``):

1. A Google Cloud project with the YouTube Data API v3 enabled, an OAuth client
   of type "Desktop app", and the consent screen **published to In production**.
   In "Testing" Google expires refresh tokens after 7 days, which would silently
   stop unattended uploads; production may stay unverified for our own channels.
2. ``peakreel youtube setup client_secret.json`` stores the client in the
   operating system's credential store.
3. ``peakreel youtube login --account en`` (per posting account) opens the
   browser, you pick the channel, and its refresh token is stored the same way.

Publishing: :class:`YouTubePublisher` uploads with the resumable protocol (8 MiB
chunks, resumed after a dropped connection), category Gaming, not made for kids,
no synthetic media. A vertical video of 3 minutes or less is a Short; there is
no separate endpoint. After the upload the video is ``processing`` until YouTube
reports it processed: then ``published`` if it is public, ``private_only`` if it
is private (private mode, or YouTube kept it private).

Quota: since 2026-06-01 ``videos.insert`` has its own daily bucket (100 calls per
project by default). A quota error does not use up retries: the upload waits an
hour and tries again.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import keyring
import keyring.errors

from peakreel.config import Platform
from peakreel.log import get_logger
from peakreel.publish.metadata import PostMetadata
from peakreel.publish.oauth import catch_redirect, new_state
from peakreel.publish.publishers import ClipPackage, PublishError, PublishMode, PublishResult

__all__ = [
    "SCOPES",
    "ChannelToken",
    "OAuthClient",
    "YouTubeApi",
    "YouTubePublisher",
    "authorize",
    "delete_token",
    "load_client",
    "load_token",
    "save_client",
    "save_token",
]

log = get_logger(__name__)

SCOPES = (
    "https://www.googleapis.com/auth/youtube.upload",
    "https://www.googleapis.com/auth/youtube.readonly",
)
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"  # noqa: S105 - a URL, not a secret
API_BASE = "https://www.googleapis.com/youtube/v3"
UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"

_KEYRING_SERVICE = "peakreel"
_CLIENT_KEY = "youtube_client"
_CHUNK = 8 * 1024 * 1024
_QUOTA_REASONS = frozenset({"quotaExceeded", "uploadLimitExceeded", "rateLimitExceeded"})
_QUOTA_RETRY_S = 3600.0
GAMING_CATEGORY = "20"


# -- credentials --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class OAuthClient:
    """The Google Cloud OAuth client (type "Desktop app")."""

    client_id: str
    client_secret: str

    def __repr__(self) -> str:
        """Hide the secret."""
        return f"OAuthClient(client_id={self.client_id!r}, client_secret='***')"

    @classmethod
    def from_file(cls, path: Path) -> OAuthClient:
        """Read the ``client_secret_*.json`` downloaded from Google Cloud.

        Raises
        ------
        ValueError
            If it is not a Desktop app client file.
        """
        data: object = json.loads(path.read_text(encoding="utf-8"))
        installed = (
            cast("dict[str, Any]", data).get("installed") if isinstance(data, dict) else None
        )
        if not isinstance(installed, dict):
            raise ValueError(
                f"{path.name} is not a 'Desktop app' OAuth client (no 'installed' section)"
            )
        fields = cast("dict[str, Any]", installed)
        return cls(str(fields["client_id"]), str(fields["client_secret"]))


@dataclass(frozen=True, slots=True)
class ChannelToken:
    """A posting account's authorization for one YouTube channel."""

    account: str
    refresh_token: str
    channel_id: str
    channel_title: str

    def __repr__(self) -> str:
        """Hide the token."""
        return f"ChannelToken({self.account!r}, channel={self.channel_title!r}, token='***')"


def save_client(client: OAuthClient) -> None:
    """Store the OAuth client in the operating system's credential store."""
    payload = json.dumps({"client_id": client.client_id, "client_secret": client.client_secret})
    keyring.set_password(_KEYRING_SERVICE, _CLIENT_KEY, payload)


def load_client() -> OAuthClient | None:
    """Return the stored OAuth client, or ``None``."""
    try:
        raw = keyring.get_password(_KEYRING_SERVICE, _CLIENT_KEY)
    except keyring.errors.KeyringError:
        return None
    if not raw:
        return None
    data = json.loads(raw)
    return OAuthClient(data["client_id"], data["client_secret"])


def _token_key(account: str) -> str:
    return f"youtube_token:{account}"


def save_token(token: ChannelToken) -> None:
    """Store a channel's refresh token for a posting account."""
    payload = json.dumps(
        {
            "refresh_token": token.refresh_token,
            "channel_id": token.channel_id,
            "channel_title": token.channel_title,
        }
    )
    keyring.set_password(_KEYRING_SERVICE, _token_key(token.account), payload)


def load_token(account: str) -> ChannelToken | None:
    """Return the stored token of ``account``, or ``None``."""
    try:
        raw = keyring.get_password(_KEYRING_SERVICE, _token_key(account))
    except keyring.errors.KeyringError:
        return None
    if not raw:
        return None
    data = json.loads(raw)
    return ChannelToken(account, data["refresh_token"], data["channel_id"], data["channel_title"])


def delete_token(account: str) -> None:
    """Forget the token of ``account`` (missing entries are ignored)."""
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(_KEYRING_SERVICE, _token_key(account))


# -- HTTP ---------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _Response:
    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> dict[str, Any]:
        data = json.loads(self.body or b"{}")
        return cast("dict[str, Any]", data) if isinstance(data, dict) else {}


def _google_error(body: bytes) -> tuple[str, str]:
    """Return (reason, message) from a Google error body."""
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return "", body[:200].decode("utf-8", "replace")
    if not isinstance(data, dict):
        return "", ""
    error = cast("dict[str, Any]", data).get("error")
    if isinstance(error, str):  # OAuth endpoint: {"error": "invalid_grant", ...}
        detail = cast("dict[str, Any]", data).get("error_description", "")
        return error, f"{error}: {detail}".strip(": ")
    if isinstance(error, dict):
        err = cast("dict[str, Any]", error)
        items = cast("list[dict[str, Any]]", err.get("errors") or [])
        reason = str(items[0].get("reason", "")) if items else ""
        return reason, str(err.get("message", reason))
    return "", ""


class YouTubeApi:
    """The few YouTube and Google OAuth calls Peakreel needs.

    Parameters
    ----------
    client : OAuthClient
    auth_url, token_url, api_base, upload_url : str
        Endpoints; overridable for tests.
    timeout_s : float, default 60.0
    clock : callable, optional
    """

    def __init__(
        self,
        client: OAuthClient,
        *,
        auth_url: str = AUTH_URL,
        token_url: str = TOKEN_URL,
        api_base: str = API_BASE,
        upload_url: str = UPLOAD_URL,
        timeout_s: float = 60.0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.client = client
        self.auth_url = auth_url
        self.token_url = token_url
        self.api_base = api_base.rstrip("/")
        self.upload_url = upload_url
        self.timeout_s = timeout_s
        self._clock = clock
        self._access: dict[str, tuple[str, float]] = {}

    def _send(
        self,
        method: str,
        url: str,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        *,
        ok: tuple[int, ...] = (200, 201),
    ) -> _Response:
        request = urllib.request.Request(  # noqa: S310 - https endpoints (http only in tests)
            url, data=body, method=method, headers=dict(headers or {})
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                result = _Response(response.status, dict(response.headers), response.read())
        except urllib.error.HTTPError as exc:
            result = _Response(exc.code, dict(exc.headers or {}), exc.read())
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PublishError(f"cannot reach YouTube: {exc}") from exc
        if result.status not in ok:
            reason, message = _google_error(result.body)
            error = PublishError(f"YouTube HTTP {result.status}: {message or reason}")
            if reason in _QUOTA_REASONS:
                error = PublishError(
                    f"YouTube quota reached ({reason}); trying again in an hour",
                    retry_after_s=_QUOTA_RETRY_S,
                )
            elif reason == "invalid_grant":
                error = PublishError(
                    "YouTube authorization expired or was revoked; run "
                    "`peakreel youtube login --account <account>` again (if this happens "
                    "every 7 days, publish the OAuth consent screen to In production)"
                )
            error.status = result.status
            error.reason = reason
            raise error
        return result

    # -- OAuth --------------------------------------------------------------------------

    def _token_request(self, fields: Mapping[str, str]) -> dict[str, Any]:
        body = urllib.parse.urlencode(
            {"client_id": self.client.client_id, "client_secret": self.client.client_secret,
             **fields}
        ).encode()  # fmt: skip
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        return self._send("POST", self.token_url, headers, body).json()

    def authorization_url(self, redirect_uri: str, state: str, challenge: str) -> str:
        """URL of Google's consent page (PKCE, offline access for a refresh token)."""
        query = urllib.parse.urlencode(
            {
                "client_id": self.client.client_id,
                "redirect_uri": redirect_uri,
                "response_type": "code",
                "scope": " ".join(SCOPES),
                "access_type": "offline",
                "prompt": "consent select_account",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{self.auth_url}?{query}"

    def exchange_code(self, code: str, redirect_uri: str, verifier: str) -> tuple[str, str]:
        """Exchange an authorization code; return (refresh token, access token)."""
        data = self._token_request(
            {"code": code, "redirect_uri": redirect_uri, "grant_type": "authorization_code",
             "code_verifier": verifier}
        )  # fmt: skip
        refresh = data.get("refresh_token")
        if not refresh:
            raise PublishError("Google returned no refresh token; try the login again")
        return str(refresh), str(data["access_token"])

    def access_token(self, token: ChannelToken) -> str:
        """Return a valid access token for ``token``'s channel, refreshed when needed."""
        cached = self._access.get(token.refresh_token)
        if cached and self._clock() < cached[1] - 60:
            return cached[0]
        data = self._token_request(
            {"refresh_token": token.refresh_token, "grant_type": "refresh_token"}
        )
        access = str(data["access_token"])
        self._access[token.refresh_token] = (
            access,
            self._clock() + float(data.get("expires_in", 3600)),
        )
        return access

    # -- API ------------------------------------------------------------------------------

    def my_channel(self, access: str) -> tuple[str, str]:
        """Return (id, label) of the channel the access token belongs to.

        The label is the title plus the @handle when the channel has one, since
        several channels may share a title (``"sode @sode.clips"``).
        """
        url = f"{self.api_base}/channels?part=snippet&mine=true"
        items = cast(
            "list[dict[str, Any]]", self._send("GET", url, _auth(access)).json().get("items") or []
        )
        if not items:
            raise PublishError("this Google account has no YouTube channel")
        snippet = items[0]["snippet"]
        handle = snippet.get("customUrl")
        label = f"{snippet['title']} {handle}" if handle else str(snippet["title"])
        return str(items[0]["id"]), label

    def upload(self, access: str, video: Path, resource: Mapping[str, Any]) -> str:
        """Upload ``video`` with ``resource`` (snippet, status); return the video id.

        Uses the resumable protocol in 8 MiB chunks; a dropped connection is
        resumed from the byte YouTube confirms, up to 3 times.
        """
        size = video.stat().st_size
        init = self._send(
            "POST",
            f"{self.upload_url}?uploadType=resumable&part=snippet,status",
            {**_auth(access), "Content-Type": "application/json; charset=UTF-8",
             "X-Upload-Content-Length": str(size), "X-Upload-Content-Type": "video/mp4"},
            json.dumps(resource).encode("utf-8"),
        )  # fmt: skip
        session = _header(init.headers, "Location")
        if not session:
            raise PublishError("YouTube did not return an upload session")
        offset = 0
        failures = 0
        with video.open("rb") as handle:
            while True:
                handle.seek(offset)
                chunk = handle.read(_CHUNK)
                end = offset + len(chunk) - 1
                headers = {**_auth(access), "Content-Range": f"bytes {offset}-{end}/{size}"}
                try:
                    response = self._send("PUT", session, headers, chunk, ok=(200, 201, 308))
                except PublishError as exc:
                    if exc.status is not None and exc.status < 500:
                        raise
                    failures += 1
                    if failures > 3:
                        raise
                    offset = self._uploaded(access, session, size)
                    continue
                if response.status in (200, 201):
                    return str(response.json()["id"])
                offset = _next_offset(response.headers)

    def _uploaded(self, access: str, session: str, size: int) -> int:
        headers = {**_auth(access), "Content-Range": f"bytes */{size}"}
        response = self._send("PUT", session, headers, b"", ok=(200, 201, 308))
        return _next_offset(response.headers)

    def video(self, access: str, video_id: str) -> dict[str, Any] | None:
        """Return the video's ``status`` and ``processingDetails``, or ``None`` if gone."""
        query = urllib.parse.urlencode({"part": "status,processingDetails", "id": video_id})
        items = cast(
            "list[dict[str, Any]]",
            self._send("GET", f"{self.api_base}/videos?{query}", _auth(access)).json().get("items")
            or [],
        )
        return items[0] if items else None

    def statistics(self, access: str, video_id: str) -> dict[str, Any]:
        """Return the video's ``statistics`` (viewCount, likeCount, commentCount)."""
        query = urllib.parse.urlencode({"part": "statistics", "id": video_id})
        items = cast(
            "list[dict[str, Any]]",
            self._send("GET", f"{self.api_base}/videos?{query}", _auth(access)).json().get("items")
            or [],
        )
        return cast("dict[str, Any]", items[0].get("statistics") or {}) if items else {}


def _auth(access: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {access}"}


def _header(headers: Mapping[str, str], name: str) -> str | None:
    for key, value in headers.items():
        if key.lower() == name.lower():
            return value
    return None


def _next_offset(headers: Mapping[str, str]) -> int:
    # "Range: bytes=0-1234" means bytes 0..1234 are stored; no header means none yet.
    value = _header(headers, "Range")
    return int(value.rsplit("-", 1)[1]) + 1 if value else 0


# -- login ----------------------------------------------------------------------------------


def authorize(
    api: YouTubeApi,
    *,
    open_browser: Callable[[str], object] = webbrowser.open,
    timeout_s: float = 300.0,
) -> tuple[str, str]:
    """Run Google's installed-app flow on a loopback port; return (refresh, access).

    PKCE and a random ``state`` protect the exchange.

    Raises
    ------
    PublishError
        If consent is refused, times out, or the state does not match.
    """
    verifier = secrets.token_urlsafe(64)
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    state = new_state()
    code, redirect_uri = catch_redirect(
        lambda redirect: api.authorization_url(redirect, state, challenge),
        state,
        open_browser=open_browser,
        timeout_s=timeout_s,
    )
    return api.exchange_code(code, redirect_uri, verifier)


# -- publisher --------------------------------------------------------------------------------


class YouTubePublisher:
    """Posts clips to the YouTube channel of their posting account.

    Parameters
    ----------
    api : YouTubeApi
    tokens : callable, optional
        ``tokens(account) -> ChannelToken | None``; defaults to :func:`load_token`.
    """

    platform: Platform = "youtube"
    modes: frozenset[PublishMode] = frozenset({"private", "public"})

    def __init__(
        self, api: YouTubeApi, tokens: Callable[[str], ChannelToken | None] = load_token
    ) -> None:
        self.api = api
        self._tokens = tokens

    def _token(self, account: str) -> ChannelToken:
        token = self._tokens(account)
        if token is None:
            raise PublishError(
                f"no YouTube channel connected for account {account!r}: "
                f"run `peakreel youtube login --account {account}`"
            )
        return token

    def validate(self, clip: ClipPackage, meta: PostMetadata) -> list[str]:
        """Return problems: a missing video, or no channel for the account."""
        problems: list[str] = []
        if not clip.video.is_file():
            problems.append(f"video file missing: {clip.video}")
        if self._tokens(clip.account) is None:
            problems.append(
                f"no YouTube channel connected for account {clip.account!r}: "
                f"run `peakreel youtube login --account {clip.account}`"
            )
        return problems

    def publish(self, clip: ClipPackage, meta: PostMetadata, mode: PublishMode) -> PublishResult:
        """Upload ``clip`` as a Short; the result is ``processing`` until polled."""
        if mode not in self.modes:
            raise PublishError(f"YouTube publisher cannot {mode}")
        token = self._token(clip.account)
        access = self.api.access_token(token)
        resource = {
            "snippet": {
                "title": meta.title or "",
                "description": meta.text,
                "tags": list(meta.tags),
                "categoryId": GAMING_CATEGORY,
            },
            "status": {
                "privacyStatus": "public" if mode == "public" else "private",
                "selfDeclaredMadeForKids": False,
                "containsSyntheticMedia": False,
            },
        }
        video_id = self.api.upload(access, clip.video, resource)
        log.info("youtube upload done", account=clip.account, video_id=video_id, mode=mode)
        return PublishResult(
            "processing", remote_id=video_id, url=f"https://youtube.com/shorts/{video_id}"
        )

    def poll_status(self, clip: ClipPackage, remote_id: str | None) -> PublishResult | None:
        """Report processing progress; ``None`` when the outcome cannot be known.

        Raises
        ------
        PublishError
            If YouTube rejected or failed the video, or it was deleted.
        """
        if remote_id is None:
            return None  # interrupted before YouTube gave an id: check the channel by hand
        access = self.api.access_token(self._token(clip.account))
        video = self.api.video(access, remote_id)
        url = f"https://youtube.com/shorts/{remote_id}"
        if video is None:
            raise PublishError(f"video {remote_id} is not on the channel (deleted?)")
        status = cast("dict[str, Any]", video.get("status") or {})
        upload = status.get("uploadStatus")
        if upload in ("rejected", "failed", "deleted"):
            reason = status.get("rejectionReason") or status.get("failureReason") or upload
            raise PublishError(f"YouTube {upload} the video: {reason}")
        if upload != "processed":
            return PublishResult("processing", remote_id, url)
        if status.get("privacyStatus") in ("public", "unlisted"):
            return PublishResult("published", remote_id, url)
        return PublishResult("private_only", remote_id, url)

    def remove(self, clip: ClipPackage, url: str | None) -> bool:
        """Never deletes a post automatically: report it for removal by hand."""
        return False
