"""Browser sign-in for desktop apps: catch the OAuth redirect on a loopback port.

Used by the YouTube (Google) and Meta adapters: the browser opens the
provider's consent page, the provider redirects to ``http://<host>:<port>/``, and
a one-shot local server reads the authorization code from that request.
"""

from __future__ import annotations

import secrets
import time
import urllib.parse
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, HTTPServer

from peakreel.publish.publishers import PublishError

__all__ = ["catch_redirect", "new_state"]


def new_state() -> str:
    """Return a random ``state`` value that ties the redirect to this sign-in."""
    return secrets.token_urlsafe(24)


def catch_redirect(
    consent_url: Callable[[str], str],
    state: str,
    *,
    open_browser: Callable[[str], object],
    host: str = "127.0.0.1",
    port: int = 0,
    redirect_host: str | None = None,
    timeout_s: float = 300.0,
) -> tuple[str, str]:
    """Open the consent page and wait for the provider's redirect.

    Parameters
    ----------
    consent_url : callable
        ``consent_url(redirect_uri) -> str``: the provider's consent page URL.
    state : str
        The ``state`` sent in the consent URL; the redirect must echo it.
    open_browser : callable
        Opens a URL (``webbrowser.open``; injectable for tests).
    host : str, default "127.0.0.1"
        Interface the one-shot server listens on.
    port : int, default 0
        ``0`` picks a free port (Google allows any loopback port); Meta needs the
        fixed port registered in the app.
    redirect_host : str, optional
        Host name in the redirect URI (Meta expects ``localhost``); defaults to ``host``.
    timeout_s : float, default 300.0

    Returns
    -------
    tuple of (str, str)
        The authorization code and the redirect URI used.

    Raises
    ------
    PublishError
        If consent is refused, nothing arrives in time, or ``state`` does not match.
    """
    received: dict[str, str] = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            if not query:  # favicon and the like: not the redirect
                self.send_response(404)
                self.end_headers()
                return
            received.update({k: v[0] for k, v in query.items()})
            ok = "code" in received and received.get("state") == state
            message = (
                "Peakreel: connected. You can close this tab."
                if ok
                else "Peakreel: sign-in failed. Go back to the terminal."
            )
            payload = message.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = HTTPServer((host, port), Handler)
    redirect_uri = f"http://{redirect_host or host}:{server.server_address[1]}/"
    deadline = time.monotonic() + timeout_s
    try:
        open_browser(consent_url(redirect_uri))
        # Other requests (a favicon) may come first: wait for the redirect itself.
        while not received and time.monotonic() < deadline:
            server.timeout = max(0.1, deadline - time.monotonic())
            server.handle_request()
    finally:
        server.server_close()
    if "error" in received:
        detail = received.get("error_description") or received.get("error_reason") or ""
        raise PublishError(f"authorization refused: {received['error']} {detail}".strip())
    if "code" not in received:
        raise PublishError("no answer from the browser in time")
    if received.get("state") != state:
        raise PublishError("authorization answer did not match this request (state)")
    return received["code"], redirect_uri
