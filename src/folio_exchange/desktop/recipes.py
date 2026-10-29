"""The Recipes view, as on the page: the recipes installed on this site (each version and where it came from,
which feeds use it, what it reads from FOLIO), and the vendors' recipe sources, each with a check for new
versions, what changed in them, and an install of exactly the commit that was reviewed.

The core decides what may be installed (``sources.check`` and ``sources.install``): only the curated sources and
the site's own, only the recipe ids a source lists, never a changed copy of an installed version, never a minor
version that changes what feeds send, and nothing that reads patron data. The window runs the check off its
thread and the install as a job; this page lays out what they answer.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from . import model
from .feed import clear
from .forms import announce
from .pages import Facts, Page, actions_row, button, heading, lines_label, set_lines, text


class RecipesPage(Page):
    check = Signal(str)                   # a source's id: what does it offer now?
    install = Signal(str, str)            # a source's id and the commit that was reviewed

    def __init__(self) -> None:
        super().__init__()
        self.data: dict | None = None
        self.checks: dict[str, dict] = {}
        self.idle = True
        self.box.addWidget(heading("Recipes"))
        self.box.addWidget(text("The file layouts vendors ask for, and where to get new ones.", "About recipes"))
        self.state = text(about="Recipes")
        self.box.addWidget(self.state)
        self.box.addWidget(heading("Installed on this site", 1.15))
        self.installed = QWidget()
        self.installed_box = QVBoxLayout(self.installed)
        self.installed_box.setContentsMargins(0, 0, 0, 0)
        self.box.addWidget(self.installed)
        self.problems_title = heading("Recipe files with problems", 1.0)
        self.problems = lines_label("Recipe files with problems")
        self.box.addWidget(self.problems_title)
        self.box.addWidget(self.problems)
        self.box.addWidget(heading("Get recipes from vendors", 1.15))
        self.get_about = text("Vendors publish their recipes on GitHub. FOLIO-Exchange downloads only from the "
                                "sources listed here, shows you what would change, and installs only what you "
                                "approve. An installed version never changes: a vendor publishes a new version "
                                "instead.", "About getting recipes")
        self.box.addWidget(self.get_about)
        self.sources = QWidget()
        self.sources_box = QVBoxLayout(self.sources)
        self.sources_box.setContentsMargins(0, 0, 0, 0)
        self.box.addWidget(self.sources)
        self.buttons: dict[str, dict] = {}        # a source's id: its check and install buttons, its review
        self.shown: object = None

    def loading(self) -> None:
        if self.data is None:
            self.state.setText("Loading the recipes…")

    def show_overview(self, data: dict | None, error: str | None = None) -> None:
        """views.recipes_overview, or why it could not be read."""
        if error:
            self.state.setText(f"The recipes could not be read: {error}")
            self.state.show()
            return
        self.state.hide()
        self.data = data
        self._draw()

    def _draw(self, focus: str | None = None) -> None:
        data = self.data or {"recipes": [], "sources": [], "problems": []}
        shown = (data, repr(self.checks), self.idle)
        if shown == self.shown and focus is None:
            return                                   # unchanged: the focus stays where it is
        self.shown = shown
        clear(self.installed_box)
        clear(self.sources_box)
        added: list[QWidget] = [self.state]
        sources = data.get("sources") or []
        for r in data.get("recipes") or []:
            facts = Facts()
            facts.show_rows(model.recipe_rows(r, sources))
            parts = [heading(r.get("title") or r["id"], 1.0),
                     text(" · ".join(x for x in (r.get("vendor"), r["id"]) if x), f"About {r['id']}")]
            parts += [text(r["description"], f"About {r['id']}")] if r.get("description") else []
            for w in [*parts, facts]:
                self.installed_box.addWidget(w)
            added += [*parts, *facts.values.values()]
        if not data.get("recipes"):
            none = text("None yet. Get one from a vendor below.", "Installed recipes")
            self.installed_box.addWidget(none)
            added.append(none)
        added += [self.problems, self.get_about]           # the page's own, between the two lists
        set_lines(self.problems, list(data.get("problems") or []), "")
        self.problems_title.setVisible(bool(data.get("problems")))
        self.problems.setVisible(bool(data.get("problems")))
        self.buttons = {}
        for s in sources:
            added += self._source(s)
        if not sources:
            none = text("No vendor sources are listed.", "Recipe sources")
            self.sources_box.addWidget(none)
            added.append(none)
        # Widgets added to a page already in the window go to the end of the Tab order: keep them in reading order.
        before: QWidget | None = None
        for w in added:
            if w.focusPolicy() & Qt.FocusPolicy.TabFocus:
                if before is not None:
                    QWidget.setTabOrder(before, w)
                before = w
        if focus and focus in self.buttons:
            # The source's newest line ("Checking GitHub…", the review, the failure), else its button: the button
            # pressed is gone with the redraw, and its new one is disabled while a check runs.
            target = self.buttons[focus].get("review") or self.buttons[focus]["check"]
            target.show()                           # added just now: Qt would show it only on the next turn
            target.setFocus(Qt.FocusReason.OtherFocusReason)
            if isinstance(target, QLabel):
                announce(target)

    def _source(self, s: dict) -> list[QWidget]:
        sid = s["id"]
        C = self.checks.get(sid) or {}
        facts = Facts()
        facts.show_rows(model.source_rows(s))
        parts: list[QWidget] = [heading(s.get("title") or sid, 1.0)]
        if s.get("description"):
            parts.append(text(s["description"], f"About {s.get('title') or sid}"))
        parts.append(facts)
        check = button("Check again" if C.get("result") or C.get("error") else "Check for updates",
                       f"Check {s.get('title') or sid} for new versions")
        check.clicked.connect(lambda _c=False, sid=sid: self._check(sid))
        check.setEnabled(not C.get("running") and not C.get("installing"))     # a check would undo the install's
        self.buttons[sid] = {"check": check}
        parts.append(check)
        if C.get("running"):
            checking = text("Checking GitHub…", "Checking")
            self.buttons[sid]["review"] = checking
            parts.append(checking)
        elif C.get("error"):
            review = text(f"The check failed: {C['error']}", f"What {s.get('title') or sid} offers")
            self.buttons[sid]["review"] = review
            parts.append(review)
        elif C.get("result"):
            parts += self._review(s, C)
        for w in parts:
            if w is check:
                row = QWidget()                     # a widget, so the next redraw deletes the button with it
                row.setLayout(actions_row(w))
                self.sources_box.addWidget(row)
            else:
                self.sources_box.addWidget(w)
        out: list[QWidget] = []
        for w in parts:
            out += [w, *w.values.values()] if isinstance(w, Facts) else [w]
        return out

    def _review(self, s: dict, C: dict) -> list[QWidget]:
        sid, r = s["id"], C["result"]
        out: list[QWidget] = [heading("Installed" if C.get("installed") is not None else
                                      f"What {s.get('title') or sid} offers now", 1.0)]
        words = [model.check_words(r)]
        if C.get("installed") is not None:
            got = C["installed"]
            words.append(f"Installed {', '.join(got)}. Feeds whose version range allows it use the new version from "
                         "their next run." if got else "Nothing new was installed.")
        review = text(" ".join(words), f"What {s.get('title') or sid} offers")
        self.buttons[sid]["review"] = review
        out.append(review)
        items = lines_label(f"Recipe files {s.get('title') or sid} offers")
        set_lines(items, model.review_lines(r), "No recipe files were found there.")
        out.append(items)
        if r.get("problems"):
            skipped = lines_label("Skipped")
            set_lines(skipped, [f"Skipped: {p}" for p in r["problems"]], "")
            out.append(skipped)
        if C.get("install_error"):
            out.append(text(f"Nothing was installed. {C['install_error']}", "Install problem"))
        n = len(r.get("new") or [])
        if C.get("installed") is None:
            if n:
                install = button(f"Install {n} new {'version' if n == 1 else 'versions'}",
                                 f"Install {n} new {'version' if n == 1 else 'versions'} from {s.get('title') or sid}")
                install.clicked.connect(lambda _c=False, sid=sid, commit=r["commit"]: self._install(sid, commit))
                install.setEnabled(self.idle and not C.get("installing"))
                self.buttons[sid]["install"] = install
                out.append(install)
            else:
                out.append(text("Nothing new to install.", "Nothing new"))
        if C.get("installing"):
            out.append(text("Installing…", "Installing"))
        return out

    # ------------------------------------------------------------ actions
    def _check(self, sid: str) -> None:
        C = self.checks.get(sid) or {}
        if C.get("running") or C.get("installing"):
            return
        self.checks[sid] = {"running": True}
        self._draw(focus=sid)
        self.check.emit(sid)

    def _install(self, sid: str, commit: str) -> None:
        C = self.checks.get(sid) or {}
        if C.get("installing") or not self.idle:
            return
        self.install.emit(sid, commit)

    def checked(self, sid: str, result: dict | None, error: str | None = None) -> None:
        self.checks[sid] = {"error": error} if error else {"result": result}
        self._draw(focus=sid)

    def installing(self, sid: str, busy: bool) -> None:
        C = self.checks.setdefault(sid, {})
        C["installing"] = busy
        C.pop("install_error", None)
        self._draw(focus=sid)

    def install_refused(self, sid: str, message: str) -> None:
        """The install was refused at once (another action runs) or failed: nothing was installed."""
        C = self.checks.setdefault(sid, {})
        C["installing"] = False
        C["install_error"] = message
        self._draw(focus=sid)

    def installed_now(self, sid: str, result: dict) -> None:
        self.checks[sid] = {"result": result, "installed": list(result.get("installed") or [])}
        self._draw(focus=sid)

    def set_idle(self, idle: bool) -> None:
        if idle != self.idle:
            self.idle = idle
            for b in self.buttons.values():
                if "install" in b:
                    b["install"].setEnabled(idle)
