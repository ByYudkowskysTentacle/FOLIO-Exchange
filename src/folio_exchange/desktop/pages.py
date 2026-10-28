"""The window's content pages, and the parts the others (``feed.py``, ``activity.py``) are made of. Each page
lays out data it is given (from ``model.snapshot`` or a job) and asks for anything else through signals, so the
window decides what runs and when.

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
    QListWidget,
    QListWidgetItem,
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


def caption(words: str) -> QLabel:
    """A form row's caption, plain text like every label."""
    label = QLabel(words)
    label.setTextFormat(Qt.TextFormat.PlainText)
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


def table(headers: list[str], name: str, about: str | None = None) -> QTableWidget:
    """A read-only table that screen readers can walk, a whole row selected at a time."""
    t = QTableWidget(0, len(headers))
    t.setAccessibleName(name)
    if about:
        t.setAccessibleDescription(about)
    t.setHorizontalHeaderLabels(headers)
    t.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    t.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    t.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
    t.setTabKeyNavigation(False)                   # Tab leaves the table; the arrow keys move inside it
    t.verticalHeader().hide()
    t.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    t.horizontalHeader().setStretchLastSection(True)
    return t


def fill_table(t: QTableWidget, rows: list[list[str]], keys: list[object] | None = None) -> None:
    """Put ``rows`` (plain text) in ``t``; ``keys[i]`` is kept on row i's first cell (Qt.UserRole)."""
    t.setRowCount(len(rows))
    for r, cells in enumerate(rows):
        for c, value in enumerate(cells):
            item = QTableWidgetItem(str(value))
            if c == 0 and keys is not None:
                item.setData(Qt.ItemDataRole.UserRole, keys[r])
            t.setItem(r, c, item)


def row_key(t: QTableWidget, row: int) -> object:
    item = t.item(row, 0)
    return item.data(Qt.ItemDataRole.UserRole) if item is not None else None


def actions_row(*widgets: QWidget) -> QHBoxLayout:
    row = QHBoxLayout()
    for w in widgets:
        row.addWidget(w)
    row.addStretch(1)
    return row


class Facts(QWidget):
    """(label, value) rows, each value a selectable plain-text label; shown again, the rows are replaced."""

    def __init__(self) -> None:
        super().__init__()
        self.form = QFormLayout(self)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.values: dict[str, QLabel] = {}
        self.rows: list[tuple[str, str]] | None = None

    def show_rows(self, rows: list[tuple[str, str]]) -> None:
        if rows == self.rows:
            return                                  # unchanged: the focus and a selection stay put
        self.rows = list(rows)
        while self.form.rowCount():
            self.form.removeRow(0)
        self.values = {}
        for words, value in rows:
            self.values[words] = text(value, words)
            self.form.addRow(caption(f"{words}:"), self.values[words])


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
    for key, words in FOLIO_ROWS:
        labels[key] = text(about=words)
        form.addRow(caption(f"{words}:"), labels[key])
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
        self.box.addWidget(heading("Needs you", 1.15))
        self.attention = QListWidget()
        self.attention.setAccessibleName("Feeds that need you")
        self.attention.setAccessibleDescription("Runs waiting for you to review and send them, runs that failed "
                                                "or were blocked, and feeds the schedule has not run. Press Enter "
                                                "to open the feed.")
        self.attention.itemActivated.connect(self._open_attention)
        self.attention_shown: list[tuple[str, str]] | None = None
        self.box.addWidget(self.attention)
        self.calm = text("Nothing waits for you: no run is held, blocked or failed.", "Feeds that need you")
        self.box.addWidget(self.calm)
        self.box.addWidget(heading("Scheduled runs", 1.15))
        self.task = text(about="Scheduled task")
        self.box.addWidget(self.task)
        self.box.addWidget(heading("Before you leave it to run on its own", 1.15))
        self.warnings = lines_label("Warnings")
        self.box.addWidget(self.warnings)
        self.box.addWidget(heading("Feeds", 1.15))
        self.table = table(["Feed", "Title", "Recipe", "State", "Last result", "When", "Newer recipe"], "Feeds",
                           "Press Enter to open the feed.")
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

    def _open_attention(self, item: QListWidgetItem) -> None:
        self.open_feed.emit(item.data(Qt.ItemDataRole.UserRole))

    def show_site(self, summary: dict, attention: list[tuple[str, str]] | None = None) -> None:
        """``attention``: (feed, what) for each feed that needs a person (``model.attention``)."""
        self.title.setText(summary.get("name") or "Home")
        self.root.setText(f"Site folder: {summary.get('root', '')}")
        attention = list(attention or [])
        if attention != self.attention_shown:      # unchanged: the current line stays for the arrow keys
            self.attention_shown = attention
            self.attention.clear()
            for feed, what in attention:
                item = QListWidgetItem(what)
                item.setData(Qt.ItemDataRole.UserRole, feed)
                item.setData(Qt.ItemDataRole.AccessibleTextRole, what)
                self.attention.addItem(item)
        if attention:                              # as tall as its lines, so the page scrolls as one
            self.attention.setMaximumHeight(len(attention) * self.attention.sizeHintForRow(0)
                                            + 2 * self.attention.frameWidth() + 4)
        self.attention.setVisible(bool(attention))
        self.calm.setVisible(not attention)
        fill_folio(self.folio, summary.get("folio") or {})
        self.task.setText((summary.get("task") or {}).get("text") or "No feed runs on a schedule yet.")
        set_lines(self.warnings, model.warnings(summary), "Nothing to look at: every feed is set up to run on its own.")
        feeds = summary.get("feeds") or []
        fill_table(self.table, [[f["name"], f.get("title") or "", f.get("recipe") or "",
                                 "On" if f.get("enabled") else "Paused", model.status_words(f.get("last_status")),
                                 model.when(f.get("last_started")) if f.get("last_started") else "",
                                 f.get("newer_recipe") or ""] for f in feeds])
        self.table.setVisible(bool(feeds))
        self.no_feeds.setVisible(not feeds)
        problems = model.file_problems(summary)
        set_lines(self.problems, problems, "")
        self.problems_title.setVisible(bool(problems))
        self.problems.setVisible(bool(problems))


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
