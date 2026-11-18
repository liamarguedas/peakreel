"""New-clip notifications: Windows toast and optional ntfy phone push.

Notifications are best effort: a failure is logged and never affects the clip.
They run on a worker thread because both channels block (PowerShell for the toast,
HTTP for ntfy).
"""

from __future__ import annotations

import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

from peakreel.config import NotifyConfig
from peakreel.log import get_logger

__all__ = ["Notification", "Notifier"]

log = get_logger(__name__)

Sender = Callable[["Notification"], None]


@dataclass(frozen=True, slots=True)
class Notification:
    """One notification.

    Attributes
    ----------
    title : str
    body : str
    url : str or None
        Opened when the notification is clicked (the review tool).
    """

    title: str
    body: str
    url: str | None = None


def _desktop(notification: Notification) -> None:
    if sys.platform != "win32":
        return
    from winotify import Notification as Toast  # pyright: ignore[reportMissingTypeStubs]

    toast = Toast(
        app_id="Peakreel",
        title=notification.title,
        msg=notification.body,
        launch=notification.url or "",
    )
    toast.show()


def _ntfy(server: str, topic: str) -> Sender:
    def send(notification: Notification) -> None:
        headers = {"Title": notification.title.encode("utf-8").decode("latin-1", "replace")}
        if notification.url:
            headers["Click"] = notification.url
        request = urllib.request.Request(  # noqa: S310 (configured https URL)
            f"{server.rstrip('/')}/{topic}",
            data=notification.body.encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=10):  # noqa: S310
            pass

    return send


class Notifier:
    """Send a notification through every configured channel.

    Parameters
    ----------
    settings : NotifyConfig
    senders : list of callable, optional
        Overrides the senders built from ``settings`` (tests).
    """

    def __init__(self, settings: NotifyConfig, *, senders: list[Sender] | None = None) -> None:
        if senders is None:
            senders = []
            if settings.desktop:
                senders.append(_desktop)
            if settings.ntfy_topic:
                senders.append(_ntfy(settings.ntfy_server, settings.ntfy_topic))
        self._senders = senders

    @property
    def enabled(self) -> bool:
        """Whether any channel is configured."""
        return bool(self._senders)

    def send(self, notification: Notification) -> int:
        """Send ``notification`` (blocking). Failures are logged, not raised.

        Parameters
        ----------
        notification : Notification

        Returns
        -------
        int
            Number of channels that accepted it.
        """
        delivered = 0
        for sender in self._senders:
            try:
                sender(notification)
                delivered += 1
            except (OSError, urllib.error.URLError, RuntimeError, ImportError) as exc:
                log.warning(
                    "notification failed", sender=getattr(sender, "__name__", "?"), error=str(exc)
                )
        return delivered
