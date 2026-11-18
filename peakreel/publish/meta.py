"""Instagram Reels and Facebook Page Reels publishers (M12), through one Meta app.

Each posting account (language) has a Facebook Page with its Instagram
professional account linked to it. Signing in once with Facebook gives:

- a Page access token that does not expire (derived from a long-lived user
  token), stored in the operating system's credential store;
- the Instagram account linked to the Page.

Both publishers upload the video directly to Meta (``rupload.facebook.com``), so
no public hosting is needed. Standard Access is enough because the Pages and
Instagram accounts are our own (people with a role on the app); App Review and
Business Verification are only for other people's accounts.

Instagram: create a Reels container with ``upload_type=resumable``, upload the
bytes, wait until Meta has processed the container, then publish it once. A
container already published (the process died after publishing) is recognised
and never published again.

Facebook: start a Reel upload on the Page, upload the bytes, finish with the
description; ``public`` publishes it, ``private`` leaves it as a draft on the Page.

Standard library only, like the other platform clients.
"""

from __future__ import annotations

import contextlib
import json
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
    "GRAPH_VERSION",
    "LOGIN_PORT",
    "SCOPES",
    "FacebookPublisher",
    "InstagramPublisher",
    "MetaApi",
    "MetaApp",
    "PageToken",
    "delete_page",
    "load_app",
    "load_page",
    "save_app",
    "save_page",
    "sign_in",
]

log = get_logger(__name__)

GRAPH_VERSION = "v26.0"
GRAPH_URL = f"https://graph.facebook.com/{GRAPH_VERSION}"
DIALOG_URL = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
LOGIN_PORT = 53682
"""Fixed loopback port: ``http://localhost:53682/`` must be a valid OAuth redirect URI."""

SCOPES = (
    "pages_show_list",
    "pages_read_engagement",
    "pages_manage_posts",
    "instagram_basic",
    "instagram_content_publish",
    "business_management",
)

_KEYRING_SERVICE = "peakreel"
_APP_KEY = "meta_app"
_RATE_LIMIT_CODES = frozenset({4, 17, 32, 613, 80001, 80002})
_RATE_RETRY_S = 3600.0


@dataclass(frozen=True, slots=True)
class MetaApp:
    """The Meta app (developers.facebook.com) used to publish.

    ``config_id`` is the Facebook Login for Business configuration, which Business
    apps use instead of a list of permissions; without it the classic dialog with
    :data:`SCOPES` is used.
    """

    app_id: str
    app_secret: str
    config_id: str | None = None

    def __repr__(self) -> str:
        """Hide the secret."""
        return f"MetaApp(app_id={self.app_id!r}, app_secret='***', config_id={self.config_id!r})"


@dataclass(frozen=True, slots=True)
class PageToken:
    """A posting account's Facebook Page and the Instagram account linked to it."""

    account: str
    page_id: str
    page_name: str
    page_token: str
    instagram_id: str | None
    instagram_username: str | None

    def __repr__(self) -> str:
        """Hide the token."""
        return (
            f"PageToken({self.account!r}, page={self.page_name!r}, "
            f"instagram={self.instagram_username!r}, token='***')"
        )


def save_app(app: MetaApp) -> None:
    """Store the Meta app in the credential store."""
    payload = json.dumps(
        {"app_id": app.app_id, "app_secret": app.app_secret, "config_id": app.config_id}
    )
    keyring.set_password(_KEYRING_SERVICE, _APP_KEY, payload)


def load_app() -> MetaApp | None:
    """Return the stored Meta app, or ``None``."""
    try:
        raw = keyring.get_password(_KEYRING_SERVICE, _APP_KEY)
    except keyring.errors.KeyringError:
        return None
    if not raw:
        return None
    data = json.loads(raw)
    return MetaApp(data["app_id"], data["app_secret"], data.get("config_id"))


def _page_key(account: str) -> str:
    return f"meta_page:{account}"


def save_page(token: PageToken) -> None:
    """Store a posting account's Page token."""
    payload = json.dumps(
        {
            "page_id": token.page_id,
            "page_name": token.page_name,
            "page_token": token.page_token,
            "instagram_id": token.instagram_id,
            "instagram_username": token.instagram_username,
        }
    )
    keyring.set_password(_KEYRING_SERVICE, _page_key(token.account), payload)


def load_page(account: str) -> PageToken | None:
    """Return the stored Page token of ``account``, or ``None``."""
    try:
        raw = keyring.get_password(_KEYRING_SERVICE, _page_key(account))
    except keyring.errors.KeyringError:
        return None
    if not raw:
        return None
    data = json.loads(raw)
    return PageToken(
        account, data["page_id"], data["page_name"], data["page_token"],
        data.get("instagram_id"), data.get("instagram_username"),
    )  # fmt: skip


def delete_page(account: str) -> None:
    """Forget the Page token of ``account`` (missing entries are ignored)."""
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(_KEYRING_SERVICE, _page_key(account))


def _meta_error(status: int, body: bytes) -> PublishError:
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return PublishError(f"Meta HTTP {status}: {body[:200].decode('utf-8', 'replace')}")
    error: dict[str, Any] = {}
    if isinstance(data, dict):
        error = cast("dict[str, Any]", cast("dict[str, Any]", data).get("error") or {})
    code = int(error.get("code") or 0)
    message = str(error.get("error_user_msg") or error.get("message") or f"HTTP {status}")
    if code in _RATE_LIMIT_CODES:
        result = PublishError(
            f"Meta rate limit ({message}); trying again in an hour", retry_after_s=_RATE_RETRY_S
        )
    elif code == 190:
        result = PublishError(
            "Meta authorization expired or was revoked; run "
            "`peakreel meta login --account <account>` again"
        )
    else:
        result = PublishError(f"Meta: {message}")
    result.status = status
    result.reason = str(code)
    return result


class MetaApi:
    """The Graph API calls Peakreel needs (Facebook Login, Pages, Instagram).

    Parameters
    ----------
    app : MetaApp
    graph_url, dialog_url : str
        Endpoints; overridable for tests.
    timeout_s : float, default 120.0
        Per request; uploads of a whole clip go in one request.
    """

    def __init__(
        self,
        app: MetaApp,
        *,
        graph_url: str = GRAPH_URL,
        dialog_url: str = DIALOG_URL,
        timeout_s: float = 120.0,
    ) -> None:
        self.app = app
        self.graph_url = graph_url.rstrip("/")
        self.dialog_url = dialog_url
        self.timeout_s = timeout_s

    def _call(
        self,
        method: str,
        url: str,
        params: Mapping[str, str] | None = None,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
    ) -> dict[str, Any]:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params)}"
        request = urllib.request.Request(  # noqa: S310 - https endpoints (http only in tests)
            url, data=body, method=method, headers=dict(headers or {})
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout_s) as response:  # noqa: S310
                raw = response.read()
        except urllib.error.HTTPError as exc:
            raise _meta_error(exc.code, exc.read()) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PublishError(f"cannot reach Meta: {exc}") from exc
        data = json.loads(raw or b"{}")
        return cast("dict[str, Any]", data) if isinstance(data, dict) else {}

    # -- sign-in --------------------------------------------------------------------------

    def consent_url(self, redirect_uri: str, state: str) -> str:
        """URL of the Facebook Login dialog for the publishing permissions."""
        params = {
            "client_id": self.app.app_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "response_type": "code",
        }
        if self.app.config_id:
            params["config_id"] = self.app.config_id  # Facebook Login for Business
        else:
            params["scope"] = ",".join(SCOPES)
        query = urllib.parse.urlencode(params)
        return f"{self.dialog_url}?{query}"

    def user_token(self, code: str, redirect_uri: str) -> str:
        """Exchange the sign-in code for a long-lived (60-day) user token."""
        app = {"client_id": self.app.app_id, "client_secret": self.app.app_secret}
        short = self._call(
            "GET", f"{self.graph_url}/oauth/access_token",
            {**app, "redirect_uri": redirect_uri, "code": code},
        )["access_token"]  # fmt: skip
        long = self._call(
            "GET", f"{self.graph_url}/oauth/access_token",
            {**app, "grant_type": "fb_exchange_token", "fb_exchange_token": str(short)},
        )["access_token"]  # fmt: skip
        return str(long)

    def granted_permissions(self, user_token: str) -> set[str]:
        """Permissions the user actually granted (``status: granted``) to the app."""
        data = self._call("GET", f"{self.graph_url}/me/permissions", {"access_token": user_token})
        rows = cast("list[dict[str, Any]]", data.get("data") or [])
        return {str(r["permission"]) for r in rows if r.get("status") == "granted"}

    def pages(self, user_token: str) -> list[dict[str, Any]]:
        """Pages the user manages, with their (non-expiring) Page token and Instagram account."""
        data = self._call(
            "GET",
            f"{self.graph_url}/me/accounts",
            {
                "fields": "id,name,access_token,instagram_business_account{id,username}",
                "access_token": user_token,
                "limit": "100",
            },
        )
        return cast("list[dict[str, Any]]", data.get("data") or [])

    # -- uploads ---------------------------------------------------------------------------

    def rupload(self, upload_url: str, token: str, video: Path) -> None:
        """Send the whole video to Meta's upload service in one request."""
        data = video.read_bytes()
        self._call(
            "POST",
            upload_url,
            headers={
                "Authorization": f"OAuth {token}",
                "offset": "0",
                "file_size": str(len(data)),
                "Content-Type": "application/octet-stream",
            },
            body=data,
        )

    # -- Instagram -------------------------------------------------------------------------

    def instagram_container(self, ig_user: str, token: str, caption: str) -> tuple[str, str]:
        """Create a Reels container for a direct upload; return (container id, upload URL)."""
        data = self._call(
            "POST",
            f"{self.graph_url}/{ig_user}/media",
            {
                "media_type": "REELS",
                "upload_type": "resumable",
                "caption": caption,
                "share_to_feed": "true",
                "access_token": token,
            },
        )
        return str(data["id"]), str(data["uri"])

    def instagram_status(self, container: str, token: str) -> str:
        """Return the container status (``IN_PROGRESS``, ``FINISHED``, ``PUBLISHED``, ...)."""
        data = self._call(
            "GET",
            f"{self.graph_url}/{container}",
            {"fields": "status_code,status", "access_token": token},
        )
        status = str(data.get("status_code") or "")
        if status == "ERROR":
            raise PublishError(f"Instagram could not process the video: {data.get('status', '')}")
        return status

    def instagram_publish(self, ig_user: str, container: str, token: str) -> str:
        """Publish a processed container; return the media id."""
        data = self._call(
            "POST",
            f"{self.graph_url}/{ig_user}/media_publish",
            {"creation_id": container, "access_token": token},
        )
        return str(data["id"])

    def permalink(self, media_id: str, token: str) -> str | None:
        """Return the public link of a published Instagram post."""
        data = self._call(
            "GET", f"{self.graph_url}/{media_id}", {"fields": "permalink", "access_token": token}
        )
        link = data.get("permalink")
        return str(link) if link else None

    # -- Facebook --------------------------------------------------------------------------

    def reel_start(self, page_id: str, token: str) -> tuple[str, str]:
        """Start a Page Reel upload; return (video id, upload URL)."""
        data = self._call(
            "POST",
            f"{self.graph_url}/{page_id}/video_reels",
            {"upload_phase": "start", "access_token": token},
        )
        return str(data["video_id"]), str(data["upload_url"])

    def reel_finish(
        self, page_id: str, token: str, video_id: str, description: str, *, draft: bool
    ) -> None:
        """Finish the upload: publish it, or keep it as a draft."""
        data = self._call(
            "POST",
            f"{self.graph_url}/{page_id}/video_reels",
            {
                "upload_phase": "finish",
                "video_id": video_id,
                "video_state": "DRAFT" if draft else "PUBLISHED",
                "description": description,
                "access_token": token,
            },
        )
        if not data.get("success", True):
            raise PublishError(f"Facebook did not accept the Reel: {data}")

    def video_status(self, video_id: str, token: str) -> dict[str, Any]:
        """Return the Reel's processing and publishing status."""
        data = self._call(
            "GET", f"{self.graph_url}/{video_id}", {"fields": "status", "access_token": token}
        )
        return cast("dict[str, Any]", data.get("status") or {})


def sign_in(
    api: MetaApp | MetaApi,
    *,
    open_browser: Callable[[str], object] = webbrowser.open,
    port: int = LOGIN_PORT,
    timeout_s: float = 300.0,
) -> tuple[str, list[dict[str, Any]]]:
    """Sign in with Facebook in the browser; return the user token and the Pages.

    Raises
    ------
    PublishError
        If consent is refused, times out, or the state does not match; or if any
        of :data:`SCOPES` was not granted. Meta silently drops permissions the
        app has not enabled (and a Facebook Login for Business configuration
        grants only those it lists), which would otherwise surface later as a
        Page without Instagram or a refused post.
    """
    meta = api if isinstance(api, MetaApi) else MetaApi(api)
    state = new_state()
    code, redirect_uri = catch_redirect(
        lambda redirect: meta.consent_url(redirect, state),
        state,
        open_browser=open_browser,
        host="127.0.0.1",
        port=port,
        redirect_host="localhost",
        timeout_s=timeout_s,
    )
    user_token = meta.user_token(code, redirect_uri)
    granted = meta.granted_permissions(user_token)
    missing = [scope for scope in SCOPES if scope not in granted]
    if missing:
        raise PublishError(
            f"Facebook did not grant: {', '.join(missing)}. In the Meta app, Use cases, "
            "Customize: add these permissions (Instagram: API setup with Facebook login); "
            "with a Login for Business configuration, tick them there too. Then log in "
            "again and allow everything (Edit access)."
        )
    return user_token, meta.pages(user_token)


def page_token_from(account: str, page: Mapping[str, Any]) -> PageToken:
    """Build the stored token of ``account`` from one entry of :meth:`MetaApi.pages`."""
    instagram = cast("dict[str, Any] | None", page.get("instagram_business_account"))
    return PageToken(
        account=account,
        page_id=str(page["id"]),
        page_name=str(page["name"]),
        page_token=str(page["access_token"]),
        instagram_id=str(instagram["id"]) if instagram else None,
        instagram_username=str(instagram.get("username")) if instagram else None,
    )


# -- publishers -----------------------------------------------------------------------------


def _missing(account: str) -> str:
    return (
        f"no Facebook Page connected for account {account!r}: "
        f"run `peakreel meta login --account {account}`"
    )


class InstagramPublisher:
    """Posts Reels to the Instagram account linked to each posting account's Page.

    Instagram has no private posts, so only ``public`` mode exists; a clip that
    must not go public is exported instead (see the queue).
    """

    platform: Platform = "instagram"
    modes: frozenset[PublishMode] = frozenset({"public"})

    def __init__(self, api: MetaApi, tokens: Callable[[str], PageToken | None] = load_page) -> None:
        self.api = api
        self._tokens = tokens

    def _token(self, account: str) -> PageToken:
        token = self._tokens(account)
        if token is None:
            raise PublishError(_missing(account))
        if token.instagram_id is None:
            raise PublishError(
                f"the Page {token.page_name!r} of account {account!r} has no Instagram account "
                "linked: link it in Meta Accounts Center, then `peakreel meta login` again"
            )
        return token

    def validate(self, clip: ClipPackage, meta: PostMetadata) -> list[str]:
        """Return problems: a missing video, no Page, or no linked Instagram account."""
        problems = [] if clip.video.is_file() else [f"video file missing: {clip.video}"]
        try:
            self._token(clip.account)
        except PublishError as exc:
            problems.append(str(exc))
        return problems

    def publish(self, clip: ClipPackage, meta: PostMetadata, mode: PublishMode) -> PublishResult:
        """Upload the Reel into a container; it is published once Meta has processed it."""
        if mode not in self.modes:
            raise PublishError("Instagram has no private posts")
        token = self._token(clip.account)
        assert token.instagram_id is not None  # noqa: S101 - checked by _token
        container, upload_url = self.api.instagram_container(
            token.instagram_id, token.page_token, meta.text
        )
        self.api.rupload(upload_url, token.page_token, clip.video)
        log.info("instagram upload done", account=clip.account, container=container)
        return PublishResult("processing", remote_id=container)

    def poll_status(self, clip: ClipPackage, remote_id: str | None) -> PublishResult | None:
        """Publish the container once processed; report ``published`` with its link.

        A container already ``PUBLISHED`` (the process died right after publishing)
        is reported as published, never published twice.
        """
        if remote_id is None:
            return None
        token = self._token(clip.account)
        assert token.instagram_id is not None  # noqa: S101 - checked by _token
        status = self.api.instagram_status(remote_id, token.page_token)
        if status == "IN_PROGRESS":
            return PublishResult("processing", remote_id)
        if status == "PUBLISHED":
            return PublishResult(
                "published", remote_id, f"https://www.instagram.com/{token.instagram_username}/"
            )
        if status == "EXPIRED":
            raise PublishError("the Instagram upload expired before it was published (24 h)")
        if status != "FINISHED":
            return PublishResult("processing", remote_id)
        media_id = self.api.instagram_publish(token.instagram_id, remote_id, token.page_token)
        link = self.api.permalink(media_id, token.page_token)
        log.info("instagram reel published", account=clip.account, media_id=media_id, url=link)
        return PublishResult("published", media_id, link)

    def remove(self, clip: ClipPackage, url: str | None) -> bool:
        """Never deletes a post automatically: report it for removal by hand."""
        return False


class FacebookPublisher:
    """Posts Reels to each posting account's Facebook Page.

    ``public`` publishes the Reel; ``private`` leaves it as a draft on the Page.
    """

    platform: Platform = "facebook"
    modes: frozenset[PublishMode] = frozenset({"public", "private"})

    def __init__(self, api: MetaApi, tokens: Callable[[str], PageToken | None] = load_page) -> None:
        self.api = api
        self._tokens = tokens

    def _token(self, account: str) -> PageToken:
        token = self._tokens(account)
        if token is None:
            raise PublishError(_missing(account))
        return token

    def validate(self, clip: ClipPackage, meta: PostMetadata) -> list[str]:
        """Return problems: a missing video or no Page."""
        problems = [] if clip.video.is_file() else [f"video file missing: {clip.video}"]
        if self._tokens(clip.account) is None:
            problems.append(_missing(clip.account))
        return problems

    def publish(self, clip: ClipPackage, meta: PostMetadata, mode: PublishMode) -> PublishResult:
        """Upload the Reel to the Page and publish it (or keep it as a draft)."""
        if mode not in self.modes:
            raise PublishError(f"Facebook publisher cannot {mode}")
        token = self._token(clip.account)
        video_id, upload_url = self.api.reel_start(token.page_id, token.page_token)
        self.api.rupload(upload_url, token.page_token, clip.video)
        draft = mode == "private"
        self.api.reel_finish(token.page_id, token.page_token, video_id, meta.text, draft=draft)
        log.info("facebook reel uploaded", account=clip.account, video_id=video_id, draft=draft)
        return PublishResult(
            "processing",
            f"{'draft:' if draft else ''}{video_id}",
            f"https://www.facebook.com/reel/{video_id}",
        )

    def poll_status(self, clip: ClipPackage, remote_id: str | None) -> PublishResult | None:
        """Report ``published`` (or ``private_only`` for a draft) once Facebook is done."""
        if remote_id is None:
            return None
        draft = remote_id.startswith("draft:")
        video_id = remote_id.removeprefix("draft:")
        url = f"https://www.facebook.com/reel/{video_id}"
        status = self.api.video_status(video_id, self._token(clip.account).page_token)
        processing = cast("dict[str, Any]", status.get("processing_phase") or {})
        publishing = cast("dict[str, Any]", status.get("publishing_phase") or {})
        if status.get("video_status") == "error" or processing.get("status") == "error":
            raise PublishError(
                f"Facebook could not process the Reel: {processing.get('error') or status}"
            )
        if publishing.get("status") == "error":
            raise PublishError(
                f"Facebook could not publish the Reel: {publishing.get('error') or status}"
            )
        if draft and processing.get("status") == "complete":
            return PublishResult("private_only", remote_id, url)
        if publishing.get("status") == "complete":
            return PublishResult("published", remote_id, url)
        return PublishResult("processing", remote_id, url)

    def remove(self, clip: ClipPackage, url: str | None) -> bool:
        """Never deletes a post automatically: report it for removal by hand."""
        return False
