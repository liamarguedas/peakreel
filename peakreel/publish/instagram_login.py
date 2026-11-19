"""Instagram without a Facebook Page: the "Instagram API with Instagram Login".

The Page route (:mod:`peakreel.publish.meta`) needs a Facebook Page per Instagram
account, and Facebook lets a person create only a few Pages a day. This route signs
in with the Instagram account itself, so a new account needs no Page.

Differences from the Page route:

- Instagram fetches the video from a public URL (this route cannot take an upload).
  The review app serves each clip at an unguessable link under ``/ig/v/`` while
  Instagram fetches it, through the Tailscale Funnel at ``publish.public_url``; the
  file is removed once the container is processed (or failed), and any left over
  after a day.
- The login (a 60-day token) is renewed here at least weekly.
- Instagram only: Facebook Reels still need a Page.

The login is finished by the review app (``/ig/callback``, reached through the
Funnel): ``peakreel instagram login`` stores a pending login and opens Instagram's
consent page; the callback checks it, exchanges the code and stores the token.

While the Meta app is in development mode, each Instagram account must be an
Instagram tester of the app (App roles, Instagram testers; accept the invite in the
Instagram app, Settings, Apps and websites).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import json
import os
import shutil
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any, cast

import keyring
import keyring.errors

from peakreel.log import get_logger
from peakreel.publish.meta import GRAPH_VERSION, InstagramLogin, MetaApi, MetaApp
from peakreel.publish.oauth import new_state
from peakreel.publish.publishers import ClipPackage, PublishError

__all__ = [
    "SCOPES",
    "DirectRoute",
    "InstagramLoginApi",
    "finish_login",
    "load_app",
    "load_login",
    "public_name",
    "save_app",
    "save_login",
    "start_login",
]

log = get_logger(__name__)

AUTHORIZE_URL = "https://www.instagram.com/oauth/authorize"
TOKEN_URL = "https://api.instagram.com/oauth/access_token"  # noqa: S105 - a URL, not a secret
GRAPH_ROOT = "https://graph.instagram.com"
SCOPES = (
    "instagram_business_basic",
    "instagram_business_content_publish",
    "instagram_business_manage_insights",
)
CALLBACK_PATH = "/ig/callback"
MEDIA_PATH = "/ig/v/"

_KEYRING_SERVICE = "peakreel"
_APP_KEY = "instagram_app"
_REFRESH_AFTER_S = 7 * 86400  # tokens last 60 days; renewed well before
_PENDING_S = 600.0  # a started login must be finished within 10 minutes
_STAGED_S = 86400.0  # links left over (a crash) are removed after a day


# -- credentials --------------------------------------------------------------------------------


def _login_key(account: str) -> str:
    return f"instagram_login_{account}"


def save_app(app: MetaApp) -> None:
    """Store the Instagram app id and secret (Instagram, API setup with Instagram login)."""
    payload = json.dumps({"app_id": app.app_id, "app_secret": app.app_secret})
    keyring.set_password(_KEYRING_SERVICE, _APP_KEY, payload)


def load_app() -> MetaApp | None:
    """Return the stored Instagram app, or ``None``."""
    try:
        raw = keyring.get_password(_KEYRING_SERVICE, _APP_KEY)
    except keyring.errors.KeyringError:
        return None
    if not raw:
        return None
    data = json.loads(raw)
    return MetaApp(str(data["app_id"]), str(data["app_secret"]))


def save_login(login: InstagramLogin) -> None:
    """Store an account's Instagram login."""
    keyring.set_password(_KEYRING_SERVICE, _login_key(login.account), json.dumps(asdict(login)))


def load_login(account: str) -> InstagramLogin | None:
    """Return the Instagram login of ``account``, or ``None``."""
    try:
        raw = keyring.get_password(_KEYRING_SERVICE, _login_key(account))
    except keyring.errors.KeyringError:
        return None
    return InstagramLogin(**json.loads(raw)) if raw else None


def delete_login(account: str) -> None:
    """Forget an account's Instagram login (missing entries are ignored)."""
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(_KEYRING_SERVICE, _login_key(account))


# -- API ----------------------------------------------------------------------------------------


class InstagramLoginApi(MetaApi):
    """Graph calls on ``graph.instagram.com``, plus the Instagram Login sign-in.

    The container, status, publish, permalink and stats calls are the Page route's
    (:class:`MetaApi`), made against Instagram's own Graph host.

    Parameters
    ----------
    app : MetaApp
        The Instagram app id and secret.
    graph_root, authorize_url, token_url : str
        Endpoints; overridable for tests.
    """

    def __init__(
        self,
        app: MetaApp,
        *,
        graph_root: str = GRAPH_ROOT,
        authorize_url: str = AUTHORIZE_URL,
        token_url: str = TOKEN_URL,
        timeout_s: float = 120.0,
    ) -> None:
        super().__init__(app, graph_url=f"{graph_root}/{GRAPH_VERSION}", timeout_s=timeout_s)
        self.graph_root = graph_root.rstrip("/")
        self.authorize_url = authorize_url
        self.token_url = token_url

    def consent_url(self, redirect_uri: str, state: str, extra: object = ()) -> str:
        """URL of Instagram's consent page for :data:`SCOPES`."""
        query = urllib.parse.urlencode({
            "client_id": self.app.app_id, "redirect_uri": redirect_uri, "state": state,
            "response_type": "code", "scope": ",".join(SCOPES),
        })  # fmt: skip
        return f"{self.authorize_url}?{query}"

    def long_token(self, code: str, redirect_uri: str) -> tuple[str, str]:
        """Exchange the sign-in code for a long-lived (60-day) token; return it and the user id."""
        form = urllib.parse.urlencode({
            "client_id": self.app.app_id, "client_secret": self.app.app_secret,
            "grant_type": "authorization_code", "redirect_uri": redirect_uri, "code": code,
        }).encode()  # fmt: skip
        data = self._call(
            "POST", self.token_url, body=form,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )  # fmt: skip
        rows = cast("list[dict[str, Any]]", data.get("data") or [data])  # both answer shapes
        short = str(rows[0]["access_token"])
        long = self._call(
            "GET", f"{self.graph_root}/access_token",
            {"grant_type": "ig_exchange_token", "client_secret": self.app.app_secret,
             "access_token": short},
        )  # fmt: skip
        return str(long["access_token"]), str(rows[0].get("user_id", ""))

    def refresh(self, token: str) -> str:
        """Renew a long-lived token for another 60 days."""
        data = self._call(
            "GET", f"{self.graph_root}/refresh_access_token",
            {"grant_type": "ig_refresh_token", "access_token": token},
        )  # fmt: skip
        return str(data["access_token"])

    def me(self, token: str) -> tuple[str, str]:
        """Return the professional account's id and username."""
        data = self._call(
            "GET", f"{self.graph_url}/me", {"fields": "user_id,username", "access_token": token}
        )
        return str(data["user_id"]), str(data["username"])


# -- login through the review app ---------------------------------------------------------------


def _pending_path(run_dir: Path) -> Path:
    return run_dir / "instagram_login.json"


def start_login(
    api: InstagramLoginApi, account: str, public_url: str, run_dir: Path, *, now: float
) -> str:
    """Record a pending login for ``account`` and return Instagram's consent URL.

    The review app's callback (:func:`finish_login`) completes it within 10 minutes.
    """
    state = new_state()
    path = _pending_path(run_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"state": state, "account": account, "at": now}), encoding="utf-8")
    return api.consent_url(public_url.rstrip("/") + CALLBACK_PATH, state)


def finish_login(
    api: InstagramLoginApi,
    code: str,
    state: str,
    public_url: str,
    run_dir: Path,
    *,
    now: float,
    save: Callable[[InstagramLogin], None] = save_login,
) -> InstagramLogin:
    """Complete the pending login: check ``state``, exchange ``code``, store the token.

    Raises
    ------
    PublishError
        Without a matching pending login started in the last 10 minutes (each is
        used once), or when Instagram refuses the code.
    """
    path = _pending_path(run_dir)
    try:
        pending = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise PublishError(
            "no Instagram login was started: run `peakreel instagram login`"
        ) from exc
    path.unlink(missing_ok=True)  # one use, whatever happens next
    if not hmac.compare_digest(str(pending.get("state", "")), state):
        raise PublishError("this login link does not match the one started; start it again")
    if now - float(pending.get("at", 0)) > _PENDING_S:
        raise PublishError("the login took longer than 10 minutes; start it again")
    token, _ = api.long_token(code, public_url.rstrip("/") + CALLBACK_PATH)
    user_id, username = api.me(token)
    login = InstagramLogin(str(pending["account"]), user_id, username, token, now)
    save(login)
    log.info("instagram login stored", account=login.account, username=username)
    return login


# -- the publishing route -----------------------------------------------------------------------


def public_name(secret: str, clip: ClipPackage) -> str:
    """Return the unguessable file name a clip is served under (the same in every process)."""
    digest = hmac.new(secret.encode(), f"{clip.clip_id}:{clip.account}".encode(), hashlib.sha256)
    return f"{digest.hexdigest()[:32]}.mp4"


class DirectRoute:
    """Posting through Instagram Login for accounts without a Page.

    Parameters
    ----------
    api : InstagramLoginApi
    public_url : str or None
        The Funnel base that reaches the review app's ``/ig/`` paths.
    media_dir : pathlib.Path
        Where clips are staged for Instagram to fetch (the review app serves it).
    logins : callable
        Loads an account's login; ``save`` stores a renewed one.
    """

    def __init__(
        self,
        api: InstagramLoginApi,
        public_url: str | None,
        media_dir: Path,
        *,
        logins: Callable[[str], InstagramLogin | None] = load_login,
        save: Callable[[InstagramLogin], None] = save_login,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.api = api
        self.public_url = public_url
        self.media_dir = media_dir
        self._logins = logins
        self._save = save
        self._clock = clock

    def login(self, account: str) -> InstagramLogin | None:
        """Return the account's login, renewed first when it is a week old."""
        login = self._logins(account)
        if login is None:
            return None
        now = self._clock()
        if now - login.refreshed_at >= _REFRESH_AFTER_S:
            try:
                login = InstagramLogin(
                    login.account, login.user_id, login.username,
                    self.api.refresh(login.token), now,
                )  # fmt: skip
                self._save(login)
            except PublishError:
                log.exception("instagram login renewal failed; the old token is kept",
                              account=account)  # fmt: skip
        return login

    def stage(self, clip: ClipPackage) -> str:
        """Put the clip where Instagram can fetch it; return the public URL."""
        if not self.public_url:
            raise PublishError("publish.public_url is not set (the Funnel to the review app)")
        self.media_dir.mkdir(parents=True, exist_ok=True)
        now = self._clock()
        for old in self.media_dir.glob("*.mp4"):
            with contextlib.suppress(OSError):
                if now - old.stat().st_mtime > _STAGED_S:
                    old.unlink()
        name = public_name(self.api.app.app_secret, clip)
        target = self.media_dir / name
        if not target.exists():
            try:
                os.link(clip.video, target)  # same disk: no copy
            except OSError:
                shutil.copyfile(clip.video, target)
        return f"{self.public_url.rstrip('/')}{MEDIA_PATH}{name}"

    def unstage(self, clip: ClipPackage) -> None:
        """Remove the clip's public copy (done or failed: Instagram no longer needs it)."""
        (self.media_dir / public_name(self.api.app.app_secret, clip)).unlink(missing_ok=True)
