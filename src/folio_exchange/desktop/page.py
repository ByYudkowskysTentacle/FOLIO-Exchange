"""The browser page, a second way in (Ctrl+B): served from this process, sharing its job runner.

The page's server starts the first time it is needed, on a free 127.0.0.1 port, with the app's own ``Jobs``,
so the app and the page run one action at a time between them. Each open uses a fresh one-time link, so the
page's sign-in stays as it is. No Qt here: the window passes in how to open a link.
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from http.server import ThreadingHTTPServer
from pathlib import Path

from .. import ui
from ..jobs import Jobs

log = logging.getLogger(__name__)

VIEWS = {"home": "", "connection": "#connection", "activity": "#activity", "recipes": "#recipes",
         "options": "#options"}


class PageFallback:
    def __init__(self, site_root: Path, jobs: Jobs, open_url: Callable[[str], object]) -> None:
        self.start_root = Path(site_root)
        self.jobs = jobs
        self.open_url = open_url
        self.httpd: ThreadingHTTPServer | None = None
        self.app: ui.App | None = None

    @property
    def site_root(self) -> Path:
        """The folder the page serves: it follows a rename made on the page."""
        return self.app.site_root if self.app is not None else self.start_root

    def follow(self, site_root: Path) -> None:
        """The window moved to ``site_root`` (renamed): the page serves it from now on too."""
        self.start_root = Path(site_root)
        if self.app is not None:
            self.app.site_root = Path(site_root)

    def open(self, view: str = "home", feed: str | None = None) -> str:
        """Open the page at ``view`` (a key of VIEWS) or at ``feed``'s page in the browser. Returns the link."""
        if self.app is None:
            self.httpd, self.app = ui.serve_in_thread(self.start_root, jobs=self.jobs)
            log.info("The browser page for %s is served on port %s", self.start_root, self.app.port)
        where = f"#feed/{feed}" if feed else VIEWS.get(view, "")
        url = self.app.link() + where
        self.open_url(url)
        return url

    def stop(self) -> None:
        if self.httpd is not None:
            self.httpd.shutdown()
            self.httpd.server_close()
            self.httpd = None
