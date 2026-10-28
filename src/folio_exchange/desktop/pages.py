"""The window's content pages. Each lays out data it is given (from ``model.snapshot`` or a job) and asks for
anything else through signals, so the window decides what runs and when.

Text uses the system font at relative sizes, so it scales with Windows' text size; nothing is said by colour
alone; every control has an accessible name.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from . import model


def _esc(text: object) -> str:
    return str(text if text is not None else "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def heading(text: str, scale: float = 1.5) -> QLabel:
    """A heading. Plain text: a feed's or recipe's title is shown as written, never read as markup (which
    could make Qt fetch an image from another computer)."""
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    font = QFont(label.font())
    font.setPointSizeF(font.pointSizeF() * scale)
    font.setBold(True)
    label.setFont(font)
    label.setWordWrap(True)
    return label


def text(value: str = "", about: str | None = None) -> QLabel:
    """A wrapped label people can select and copy (folder paths, messages). ``about`` is its accessible
    description: a label's accessible name is what screen readers read, so it stays the label's own text."""
    label = QLabel(value)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                  | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    if about:
        label.setAccessibleDescription(about)
    return label


def button(caption: str, name: str) -> QPushButton:
    b = QPushButton(caption)
    b.setAccessibleName(name)
    return b


def lines_label(about: str) -> QLabel:
    """A list of lines (escaped with ``_esc``), described to screen readers as ``about``."""
    label = QLabel()
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.RichText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                  | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    label.setAccessibleDescription(about)
    return label


def alert(parent: QWidget | None, message: str, icon: QMessageBox.Icon = QMessageBox.Icon.Warning) -> None:
    """A message box whose text is shown as written: a folder name or an error's text is never read as markup."""
    box = QMessageBox(icon, "FOLIO-Exchange", message, QMessageBox.StandardButton.Ok, parent)
    box.setTextFormat(Qt.TextFormat.PlainText)
    box.exec()


def set_lines(label: QLabel, lines: list[str], empty: str) -> None:
    label.setText("<ul>" + "".join(f"<li>{_esc(x)}</li>" for x in lines) + "</ul>" if lines else _esc(empty))


class Page(QScrollArea):
    """A page that scrolls as one when the window is small or the text is large."""

    def __init__(self) -> None:
        super().__init__()
        self.setWidgetResizable(True)
        self.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        self.box = QVBoxLayout(inner)
        self.box.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.setWidget(inner)


FOLIO_ROWS = (("base_url", "Address"), ("tenant", "Tenant"), ("username", "Account"), ("password", "Password"))


def folio_rows(form: QFormLayout) -> dict[str, QLabel]:
    """The FOLIO connection's rows: where the password comes from, never the password."""
    labels = {}
    for key, caption in FOLIO_ROWS:
        labels[key] = text(about=caption)
        form.addRow(f"{caption}:", labels[key])
    return labels


def fill_folio(labels: dict[str, QLabel], folio: dict) -> None:
    for key, _caption in FOLIO_ROWS:
        value = folio.get(key)
        labels[key].setText(model.password_words(value) if key == "password" else str(value or "not set"))


class HomePage(Page):
    open_feed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.title = heading("Home")
        self.root = text(about="Site folder")
        self.box.addWidget(self.title)
        self.box.addWidget(self.root)
        self.box.addWidget(heading("FOLIO connection", 1.15))
        form = QFormLayout()
        self.folio = folio_rows(form)
        self.box.addLayout(form)
        self.box.addWidget(heading("Scheduled runs", 1.15))
        self.task = text(about="Scheduled task")
        self.box.addWidget(self.task)
        self.box.addWidget(heading("Before you leave it to run on its own", 1.15))
        self.warnings = lines_label("Warnings")
        self.box.addWidget(self.warnings)
        self.box.addWidget(heading("Feeds", 1.15))
        self.table = QTableWidget(0, 7)
        self.table.setAccessibleName("Feeds")
        self.table.setHorizontalHeaderLabels(["Feed", "Title", "Recipe", "State", "Last result", "When",
                                              "Newer recipe"])
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.cellActivated.connect(lambda row, _col: self._open(row))
        self.box.addWidget(self.table)
        self.no_feeds = text("No feeds yet. Add one in the browser page: it starts as a dry run that sends "
                             "nothing.", "No feeds")
        self.box.addWidget(self.no_feeds)
        self.problems_title = heading("Files that do not load", 1.15)
        self.problems = lines_label("Files that do not load")
        self.box.addWidget(self.problems_title)
        self.box.addWidget(self.problems)

    def _open(self, row: int) -> None:
        item = self.table.item(row, 0)
        if item is not None:
            self.open_feed.emit(item.text())

    def show_site(self, summary: dict) -> None:
        self.title.setText(summary.get("name") or "Home")
        self.root.setText(f"Site folder: {summary.get('root', '')}")
        fill_folio(self.folio, summary.get("folio") or {})
        self.task.setText((summary.get("task") or {}).get("text") or "No feed runs on a schedule yet.")
        set_lines(self.warnings, model.warnings(summary), "Nothing to look at: every feed is set up to run on its own.")
        feeds = summary.get("feeds") or []
        self.table.setRowCount(len(feeds))
        for row, f in enumerate(feeds):
            cells = [f["name"], f.get("title") or "", f.get("recipe") or "", "On" if f.get("enabled") else "Paused",
                     model.status_words(f.get("last_status")), model.when(f.get("last_started")) if
                     f.get("last_started") else "", f.get("newer_recipe") or ""]
            for col, value in enumerate(cells):
                self.table.setItem(row, col, QTableWidgetItem(value))
        self.table.setVisible(bool(feeds))
        self.no_feeds.setVisible(not feeds)
        problems = model.file_problems(summary)
        set_lines(self.problems, problems, "")
        self.problems_title.setVisible(bool(problems))
        self.problems.setVisible(bool(problems))


class FeedPage(Page):
    open_in_page = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.name = ""
        self.title = heading("Feed")
        self.box.addWidget(self.title)
        form = QFormLayout()
        self.fields: dict[str, QLabel] = {}
        for key, caption in (("name", "Feed"), ("recipe", "Recipe"), ("state", "State"),
                             ("schedule", "Schedule"), ("next", "Next run"), ("delivery", "Sends to"),
                             ("last", "Last run"), ("waiting", "Waiting"), ("newer", "Newer recipe")):
            self.fields[key] = text(about=caption)
            form.addRow(f"{caption}:", self.fields[key])
        self.form = form
        self.box.addLayout(form)
        self.more = text("Its layout, history and actions (preview, run, release, pause) are on the browser page "
                         "for now.", "About this page")
        self.box.addWidget(self.more)
        self.open_button = button("Open this feed in the &browser page", "Open this feed in the browser page")
        self.open_button.clicked.connect(lambda: self.open_in_page.emit(self.name))
        row = QHBoxLayout()
        row.addWidget(self.open_button)
        row.addStretch(1)
        self.box.addLayout(row)

    def show_feed(self, d: dict) -> None:
        self.name = d["name"]
        self.title.setText(d.get("title") or d["name"])
        recipe = d.get("recipe") or {}
        last = d.get("last_run")
        waiting = d.get("waiting")
        sched = d.get("schedule") or {}
        values = {
            "name": d["name"],
            "recipe": f"{recipe.get('title') or ''} ({recipe.get('ref')})" if recipe.get("title") else recipe.get("ref"),
            "state": "On: scheduled runs include it" if d.get("enabled") else "Paused: scheduled runs skip it",
            "schedule": sched.get("describe") or sched.get("text") or "",
            "next": model.when(sched.get("next")) if sched.get("next") else ("none while paused" if not
                                                                             d.get("enabled") else "none"),
            "delivery": (d.get("delivery") or {}).get("where") or "",
            "last": (f"{model.status_words(last['status'])}, {model.when(last.get('started'))}"
                     + (f": {last['error']}" if last.get("error") else "")) if last else "No runs yet",
            "waiting": (f"A run from {model.when(waiting.get('started'))} waits to be released"
                        if waiting else ""),
            "newer": d.get("newer_recipe") or "",
        }
        for key, value in values.items():
            self.fields[key].setText(value or "")
            self.form.setRowVisible(self.fields[key], bool(value))
        if d.get("overdue"):
            self.fields["next"].setText(f"{self.fields['next'].text()}. Warning: {d['overdue']}")


class ConnectionPage(Page):
    check = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.box.addWidget(heading("Connection"))
        form = QFormLayout()
        self.folio = folio_rows(form)
        self.box.addLayout(form)
        self.check_button = button("&Check the connection", "Check the connection")
        self.check_button.clicked.connect(self.check.emit)
        row = QHBoxLayout()
        row.addWidget(self.check_button)
        row.addStretch(1)
        self.box.addLayout(row)
        self.state = text(about="Check status")
        self.box.addWidget(self.state)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setAccessibleName("Log of the check")
        self.log.setMinimumHeight(120)
        self.log.hide()
        self.box.addWidget(self.log)
        self.result = lines_label("Result of the check")
        self.box.addWidget(self.result)

    def show_site(self, summary: dict) -> None:
        fill_folio(self.folio, summary.get("folio") or {})

    def started(self) -> None:
        self.log.clear()
        self.log.show()
        self.result.clear()
        self.state.setText("Checking: logging in to FOLIO and reading one page of each kind of record…")

    def add_lines(self, lines: list[str]) -> None:
        for line in lines:
            self.log.appendPlainText(line)

    def refused(self, message: str) -> None:
        self.state.setText(message)

    def finished(self, job: dict) -> None:
        if job["status"] != "done":
            self.state.setText(f"The check failed after {job['seconds']} s: {job.get('error') or 'no reason given'}")
            self.result.clear()
            return
        res = job.get("result") or {}
        rows = res.get("endpoints") or []
        good = [r for r in rows if r.get("status") == "ok"]
        self.state.setText(("Logged in. Every kind of record the feeds need can be read." if res.get("ok") else
                            f"Logged in, but {len(rows) - len(good)} of {len(rows)} kinds of record could not be "
                            "read.") + f" ({job['seconds']} s)")
        parts = []
        if res.get("note"):
            parts.append(f"<p>{_esc(res['note'])}</p>")
        parts.append("<p><b>What this account can read</b></p><ul>")
        for r in rows:
            if r.get("status") == "ok":
                count = "" if r.get("count") is None else f": {r['count']:,} records"
                parts.append(f"<li>OK – {_esc(r['name'])} ({_esc(r['path'])}){count}</li>")
            else:
                word = "Permission missing" if r.get("status") == "denied" else "Failed"
                parts.append(f"<li>{word} – {_esc(r['name'])} ({_esc(r['path'])}): {_esc(r.get('error'))}</li>")
        parts.append("</ul>")
        if res.get("denied"):
            parts.append("<p><b>Ask your FOLIO administrator for these permissions</b></p><ul>"
                         + "".join(f"<li>{_esc(p)}</li>" for p in res["denied"]) + "</ul>")
        problems = res.get("problems") or {}
        if problems:
            parts.append("<p><b>Feed files that do not load</b> (their needs were not checked)</p><ul>"
                         + "".join(f"<li>{_esc(k)}: {_esc(v)}</li>" for k, v in problems.items()) + "</ul>")
        self.result.setText("".join(parts))


class LaterPage(Page):
    """A part of the app still to come: says so, and opens it on the browser page."""

    open_in_page = Signal(str)

    def __init__(self, view: str, title: str, about: str, mnemonic_caption: str) -> None:
        super().__init__()
        self.view = view
        self.box.addWidget(heading(title))
        self.box.addWidget(text(f"{about} It comes to this app in a later version; until then it is on the "
                                "browser page.", f"About {title}"))
        b = button(mnemonic_caption, f"Open {title} in the browser page")
        b.clicked.connect(lambda: self.open_in_page.emit(self.view))
        row = QHBoxLayout()
        row.addWidget(b)
        row.addStretch(1)
        self.box.addLayout(row)


class NotReadyPage(Page):
    """A folder that is not a working site yet: what is wrong, and where to set it up."""

    set_up = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.title = heading("This site is not set up yet")
        self.box.addWidget(self.title)
        self.what = text(about="What is wrong")
        self.box.addWidget(self.what)
        self.set_up_button = button("Set it &up in the browser page", "Set up this site in the browser page")
        self.set_up_button.clicked.connect(self.set_up.emit)
        row = QHBoxLayout()
        row.addWidget(self.set_up_button)
        row.addStretch(1)
        self.box.addLayout(row)
        self.box.addWidget(text("Setting up a site in this app comes in a later version.", "Coming later"))

    def show_state(self, state: dict) -> None:
        if state.get("problem"):
            self.title.setText("This site's settings do not load")
            self.what.setText(f"{state['problem']}\n\nThe browser page shows the FOLIO connection so it can be "
                              "put right, or correct site.yaml in a text editor.")
        else:
            self.title.setText("This site is not set up yet")
            self.what.setText(f"{state.get('root')} has no site.yaml yet. The browser page starts with the "
                              "connection to FOLIO, then a vendor feed.")
