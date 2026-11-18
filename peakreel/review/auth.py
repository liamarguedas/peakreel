"""Password login for the review page, for approving clips from a phone.

The review page needs no login while only this machine can reach it. Once it can be
reached from elsewhere (``review.host`` not loopback, or ``review.public_hosts`` set,
for example a Tailscale name), :func:`peakreel.review.app.create_app` requires one.

Design
------
- **Password**: set with ``peakreel review password``; only an scrypt hash is kept,
  in the system credential store (Windows Credential Manager), never in files.
- **Sessions**: a cookie ``<expiry>.<nonce>.<HMAC-SHA256>`` signed with a random
  key stored next to the hash. Nothing is kept server side, so sessions survive a
  restart; setting a new password replaces the key and ends every session.
- **Cookie flags**: ``HttpOnly`` (scripts cannot read it), ``SameSite=Strict``
  (other sites cannot send it), ``Secure`` when the request came over HTTPS.
- **Guessing**: :class:`LoginThrottle` refuses logins for a while after repeated
  wrong passwords.
"""

from __future__ import annotations

import base64
import collections
import contextlib
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, field

import keyring
import keyring.errors

__all__ = [
    "MIN_PASSWORD_LENGTH",
    "Credentials",
    "LoginThrottle",
    "clear_password",
    "load_credentials",
    "set_password",
]

_KEYRING_SERVICE = "peakreel"
_KEY = "review-login"
MIN_PASSWORD_LENGTH = 12
"""Shortest password accepted by :func:`set_password`."""

# scrypt cost: 16 MiB and about 50 ms per check, which is what makes guessing slow.
_N, _R, _P = 2**14, 8, 1


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _scrypt(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=32)


@dataclass(frozen=True, slots=True)
class Credentials:
    """The stored login: password hash and session signing key.

    Attributes
    ----------
    salt : bytes
    password_hash : bytes
        ``scrypt(password, salt)``.
    session_key : bytes
        Signs session cookies; replaced whenever the password is set.
    """

    salt: bytes
    password_hash: bytes
    session_key: bytes

    def __repr__(self) -> str:
        """Hide the secrets."""
        return "Credentials(***)"

    def check_password(self, password: str) -> bool:
        """Return whether ``password`` is the stored one (constant-time comparison)."""
        return hmac.compare_digest(_scrypt(password, self.salt), self.password_hash)

    def new_session(self, now: float, lifetime_s: float) -> str:
        """Return a signed session token valid until ``now + lifetime_s``.

        Examples
        --------
        >>> creds = Credentials(b"s", b"h", b"k" * 32)
        >>> token = creds.new_session(1000.0, 60)
        >>> creds.session_valid(token, 1059.0), creds.session_valid(token, 1061.0)
        (True, False)
        """
        body = f"{int(now + lifetime_s)}.{_b64(secrets.token_bytes(16))}"
        return f"{body}.{self._sign(body)}"

    def session_valid(self, token: str | None, now: float) -> bool:
        """Return whether ``token`` was signed with this key and has not expired.

        Examples
        --------
        >>> creds = Credentials(b"s", b"h", b"k" * 32)
        >>> token = creds.new_session(1000.0, 60)
        >>> creds.session_valid(token[:-1] + ("A" if token[-1] != "A" else "B"), 1000.0)
        False
        >>> Credentials(b"s", b"h", b"x" * 32).session_valid(token, 1000.0)
        False
        >>> creds.session_valid(None, 1000.0), creds.session_valid("junk", 1000.0)
        (False, False)
        """
        if not token:
            return False
        body, _, signature = token.rpartition(".")
        expiry, _, _nonce = body.partition(".")
        if not hmac.compare_digest(self._sign(body), signature):
            return False
        try:
            return now < int(expiry)
        except ValueError:
            return False

    def _sign(self, body: str) -> str:
        return _b64(hmac.new(self.session_key, body.encode("ascii"), hashlib.sha256).digest())


def set_password(password: str) -> Credentials:
    """Store a new review password, ending every existing session.

    Parameters
    ----------
    password : str
        At least :data:`MIN_PASSWORD_LENGTH` characters.

    Returns
    -------
    Credentials

    Raises
    ------
    ValueError
        If the password is too short.
    keyring.errors.KeyringError
        If the credential store cannot be written.
    """
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"use at least {MIN_PASSWORD_LENGTH} characters")
    salt = secrets.token_bytes(16)
    creds = Credentials(salt, _scrypt(password, salt), secrets.token_bytes(32))
    payload = {
        "salt": _b64(creds.salt),
        "hash": _b64(creds.password_hash),
        "key": _b64(creds.session_key),
    }
    keyring.set_password(_KEYRING_SERVICE, _KEY, json.dumps(payload))
    return creds


def load_credentials() -> Credentials | None:
    """Return the stored login, or ``None`` if no password is set or the store is unreadable."""
    try:
        raw = keyring.get_password(_KEYRING_SERVICE, _KEY)
    except keyring.errors.KeyringError:
        return None
    if not raw:
        return None
    data = json.loads(raw)
    return Credentials(_unb64(data["salt"]), _unb64(data["hash"]), _unb64(data["key"]))


def clear_password() -> None:
    """Remove the review password (missing entries are ignored)."""
    with contextlib.suppress(keyring.errors.PasswordDeleteError):
        keyring.delete_password(_KEYRING_SERVICE, _KEY)


@dataclass(slots=True)
class LoginThrottle:
    """Refuse logins after too many wrong passwords in a window.

    The count is global, not per address: behind ``tailscale serve`` every request
    comes from 127.0.0.1. Someone guessing can therefore lock the owner out for
    ``window_s``, which is preferable to letting them keep guessing.

    Parameters
    ----------
    max_failures : int, default 5
    window_s : float, default 900.0

    Examples
    --------
    >>> throttle = LoginThrottle(max_failures=2, window_s=60)
    >>> throttle.failed(0.0); throttle.failed(10.0)
    >>> throttle.retry_after(20.0)
    40
    >>> throttle.retry_after(61.0) is None
    True
    """

    max_failures: int = 5
    window_s: float = 900.0
    _failures: collections.deque[float] = field(default_factory=collections.deque[float])

    def failed(self, now: float) -> None:
        """Record a wrong password at ``now``."""
        self._failures.append(now)

    def retry_after(self, now: float) -> int | None:
        """Return seconds until a login may be tried again, or ``None`` if it may now."""
        while self._failures and now - self._failures[0] >= self.window_s:
            self._failures.popleft()
        if len(self._failures) < self.max_failures:
            return None
        return max(1, round(self._failures[0] + self.window_s - now))
