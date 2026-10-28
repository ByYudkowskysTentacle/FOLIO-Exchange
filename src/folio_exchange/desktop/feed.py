"""A feed's page: what it is and does (Overview, Layout, Settings, Delivery, History) and its actions (preview,
run, build only, pause or resume, review and send a held run, test the delivery, open a run's folder), with the
result of the last action or of a run opened from its history below.

As on the page, nothing here decides what is safe to send: a run's first delivery is held, a build sends
nothing, and a release sends only what the person confirmed in the window's question; the core enforces each.
The page keeps each feed's last result while the window is open, so moving between feeds loses nothing.
"""
from __future__ import annotations

import time

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QApplication, QFormLayout, QFrame, QLabel, QTabWidget, QVBoxLayout, QWidget

from . import model
from .pages import (
    Facts,
    Page,
    actions_row,
    button,
    caption,
    fill_table,
    heading,
    lines_label,
    row_key,
    set_lines,
    table,
    text,
)
from .runview import RunView

TABS = ("Overview", "Layout", "Settings", "Delivery", "History")
NO_RESULT = ("No result yet. Preview builds a sample from live data. Run updates from FOLIO, builds the files and "
             "sends them, or holds them for you to review.")


def state_words(enabled: bool) -> str:
    return "On: scheduled runs include it" if enabled else "Paused: scheduled runs skip it"


def clear(box: QVBoxLayout) -> None:
    while box.count():
        item = box.takeAt(0)
        if item.widget() is not None:
            item.widget().deleteLater()


class FeedPage(Page):
    preview = Signal(str)
    run = Signal(str, bool)                   # the feed, and whether to send (False: build only)
    set_enabled = Signal(str, bool)           # the feed, and True to resume or False to pause
    review = Signal(str)                      # look at what a release would send, then ask
    check_delivery = Signal(str)
    open_folder = Signal(int)                 # a run's id
    open_run = Signal(str, int)               # the feed, a run's id
    rows_wanted = Signal(str, int, str)       # the feed, a run's id, an output
    open_in_page = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.name = ""
        self.detail: dict = {}
        self.settings: dict = {}
        self.results: dict[str, dict] = {}    # each feed's last result while the window is open
        self.checks: dict[str, str] = {}      # each feed's last delivery test, in words
        self.idle = True
        self.reviewing = self.toggling = False
        self.unconfirmed = False              # an SFTP server key not confirmed yet: no delivery test
        self.waiting_id: int | None = None
        self.layout_shown: object = None
        self.runs_shown: list[dict] | None = None
        self.title = heading("Feed")
        self.box.addWidget(self.title)
        self.sub = text(about="Feed")
        self.box.addWidget(self.sub)
        self.preview_button = button("Pre&view", "Preview: build sample files and send nothing")
        self.preview_button.setToolTip("Preview (Ctrl+Enter): builds sample files from live data and sends nothing")
        self.preview_button.clicked.connect(self._preview)
        self.run_button = button("&Run", "Run: update from FOLIO, build, check, then send or hold for review")
        self.run_button.setToolTip("Update from FOLIO, build, check, then send (or hold for review)")
        self.run_button.clicked.connect(self._run)
        self.build_button = button("&Build only", "Build only: update and build the files, and send nothing")
        self.build_button.setToolTip("Update and build the files; send nothing")
        self.build_button.clicked.connect(self._build)
        self.pause_button = button("Pa&use", "Pause this feed")
        self.pause_button.clicked.connect(self._toggle)
        self.page_button = button("Open this feed in the browser pa&ge", "Open this feed in the browser page")
        self.page_button.clicked.connect(self._open_in_page)
        self.box.addLayout(actions_row(self.preview_button, self.run_button, self.build_button, self.pause_button,
                                       self.page_button))
        self.notice = text(about="What just happened")
        self.notice.hide()
        self.box.addWidget(self.notice)

        self.waiting = QFrame()
        self.waiting.setFrameShape(QFrame.Shape.StyledPanel)
        wbox = QVBoxLayout(self.waiting)
        self.waiting_text = text(about="A run waits for you")
        wbox.addWidget(self.waiting_text)
        self.waiting_folder = button("Open its &folder", "Open the waiting run's folder")
        self.waiting_folder.clicked.connect(self._open_waiting_folder)
        self.review_button = button("Review and s&end…", "Review and send the waiting run")
        self.review_button.clicked.connect(self._review)
        wbox.addLayout(actions_row(self.waiting_folder, self.review_button))
        self.waiting.hide()
        self.box.addWidget(self.waiting)

        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("Feed sections")
        self.tabs.addTab(self._overview(), TABS[0])
        self.tabs.addTab(self._layout(), TABS[1])
        self.tabs.addTab(self._settings(), TABS[2])
        self.tabs.addTab(self._delivery(), TABS[3])
        self.tabs.addTab(self._history(), TABS[4])
        self.box.addWidget(self.tabs)
        self.result = RunView(NO_RESULT)
        self.result.open_folder.connect(self.open_folder)
        self.result.rows_wanted.connect(self._rows_wanted)
        self.box.addWidget(self.result)

    # ------------------------------------------------------------ the tabs
    def _overview(self) -> QWidget:
        w = QWidget()
        box = QVBoxLayout(w)
        self.notices = lines_label("About this feed")
        box.addWidget(self.notices)
        form = QFormLayout()
        self.fields: dict[str, QLabel] = {}
        for key, words in (("recipe", "Recipe"), ("uses", "Uses"), ("state", "State"), ("schedule", "Schedule"),
                           ("delivery", "Sends to"), ("last", "Last run"), ("step", "Next step"),
                           ("newer", "Newer recipe"), ("file", "Feed file")):
            self.fields[key] = text(about=words)
            form.addRow(caption(f"{words}:"), self.fields[key])
        self.form = form
        box.addLayout(form)
        box.addStretch(1)
        return w

    def _layout(self) -> QWidget:
        w = QWidget()
        self.layout_box = QVBoxLayout(w)
        return w

    def _settings(self) -> QWidget:
        w = QWidget()
        box = QVBoxLayout(w)
        box.addWidget(text("The layout (which columns and fields the vendor gets) comes from the recipe: see the "
                           "Layout tab. Changing these settings comes to this app later; until then, use Open this "
                           "feed in the browser page.", "About the settings"))
        self.basics = Facts()
        box.addWidget(self.basics)
        box.addWidget(heading("Filters", 1.0))
        self.filters = lines_label("Filters")
        box.addWidget(self.filters)
        box.addWidget(heading("Options", 1.0))
        self.options = lines_label("Options")
        box.addWidget(self.options)
        box.addWidget(heading("Checks before sending", 1.0))
        self.checks_facts = Facts()
        box.addWidget(self.checks_facts)
        self.legacy_title = heading("Migrated values (item note types)", 1.0)
        self.legacy = lines_label("Migrated values")
        box.addWidget(self.legacy_title)
        box.addWidget(self.legacy)
        box.addStretch(1)
        return w

    def _delivery(self) -> QWidget:
        w = QWidget()
        box = QVBoxLayout(w)
        box.addWidget(heading("Where the files go", 1.15))
        self.delivery_problem = text(about="Problem")
        box.addWidget(self.delivery_problem)
        self.where = Facts()
        box.addWidget(self.where)
        self.test_button = button("&Test delivery", "Test delivery: log in and look at the folder, send nothing")
        self.test_button.setToolTip("Logs in and looks at the folder; it sends nothing.")
        self.test_button.clicked.connect(self._test)
        self.test_why = text("The test waits until the server key is confirmed.", "Why the test waits")
        box.addLayout(actions_row(self.test_button, self.test_why))
        self.test_result = text(about="Delivery test")
        box.addWidget(self.test_result)
        self.paused_title = heading("This feed is paused", 1.0)
        self.paused_text = text(about="This feed is paused")
        box.addWidget(self.paused_title)
        box.addWidget(self.paused_text)
        box.addWidget(text("Changing where the files go, and storing the password or key, comes to this app "
                           "later; until then, use Open this feed in the browser page.", "About delivery"))
        box.addStretch(1)
        return w

    def _history(self) -> QWidget:
        w = QWidget()
        box = QVBoxLayout(w)
        self.history = table(["Status", "Started", "By", "Items", "Bibs", "Folder", "Note"], "Runs of this feed",
                             "The latest runs, previews included, newest first. Press Enter to open one below.")
        self.history.cellActivated.connect(self._open_run)
        box.addWidget(self.history)
        self.no_runs = text("No runs yet.", "Runs of this feed")
        box.addWidget(self.no_runs)
        return w

    # ------------------------------------------------------------ showing a feed
    def show_feed(self, d: dict, runs: list[dict], settings: dict | None = None,
                  warnings: list[dict] | None = None) -> None:
        """Lay out a feed: ``d`` its views.feed_detail, ``runs`` its latest runs, ``settings`` its
        setupops.feed_settings (or {error}) and ``warnings`` the site's warnings about it."""
        another = d["name"] != self.name
        if another:
            self.notice.hide()
        self.name, self.detail, self.settings = d["name"], d, settings or {}
        recipe = d.get("recipe") or {}
        dl = d.get("delivery") or {}
        self.title.setText(d.get("title") or d["name"])
        self.sub.setText(f"{d['name']} · {recipe.get('ref') or ''} → {dl.get('where') or ''}")
        self._show_enabled(bool(d.get("enabled")))
        self._show_waiting(d.get("waiting"))
        self._show_overview(d, warnings or [])
        self._show_layout(d)
        self._show_settings(d)
        self._show_delivery(d)
        if another or runs != self.runs_shown:          # unchanged: the selection and focus stay put
            self.runs_shown = runs
            fill_table(self.history, [[model.status_words(r.get("status")), model.when(r.get("started")),
                                       r.get("trigger") or "", model.num(r.get("items")), model.num(r.get("bibs")),
                                       r.get("folder") or "", model.run_note(r)] for r in runs],
                       [r["id"] for r in runs])
        self.history.setVisible(bool(runs))
        self.no_runs.setVisible(not runs)
        self.tabs.setTabText(4, f"History ({len(runs)})")
        if another:                                     # a result changes through job_*, show_run and show_rows
            self.result.show_result(self.results.get(self.name), self.delivers())
        self.set_idle(self.idle, reviewing=self.reviewing, toggling=self.toggling)

    def delivers(self) -> bool:
        return bool((self.detail.get("delivery") or {}).get("delivers"))

    def _show_enabled(self, enabled: bool) -> None:
        self.pause_button.setText("Pa&use" if enabled else "Res&ume")
        self.pause_button.setAccessibleName("Pause this feed" if enabled else "Resume this feed")

    def show_enabled(self, name: str, enabled: bool) -> None:
        """A pause or resume of ``name`` was written: show it at once, so the button says what it does next even
        before the site is read again (a second press then resumes, rather than pausing once more)."""
        if name != self.name:
            return
        self.detail["enabled"] = enabled
        self._show_enabled(enabled)
        self.fields["state"].setText(state_words(enabled))
        self._show_delivery(self.detail)

    def _show_waiting(self, w: dict | None) -> None:
        self.waiting_id = w["id"] if w else None
        self.waiting.setVisible(bool(w))
        if not w:
            return
        why = model.page_words(w.get("held") or w.get("error")) or (
            "built without sending" if w.get("status") == "built" else "")
        self.waiting_text.setText(f"{model.status_words(w['status'])}. The run of {model.when(w.get('started'))} "
                                  f"is waiting for you{f': {why}' if why else '.'}")         # as Home words it
        self.waiting_folder.setVisible(bool(w.get("folder")))

    def _show_overview(self, d: dict, warnings: list[dict]) -> None:
        notes = []
        last = d.get("last_run")
        if not d.get("waiting") and last and last.get("status") == "failed":
            notes.append(f"The last run failed. {model.page_words(last.get('error'))}".rstrip())
        transport = (d.get("delivery") or {}).get("transport")
        if not transport or transport == "none":
            notes.append("This feed has nowhere to send files yet: it is a dry run. Preview and Run build its files "
                         "on this computer only.")
        for w in warnings:
            if w.get("feed") == d["name"] and w.get("code") != "feed-file":
                level = model.LEVEL_WORDS.get(w.get("level"), "Note")
                notes.append(f"{level}: {model.warning_text(w, True, d.get('newer_recipe'))}")
        set_lines(self.notices, notes, "")
        self.notices.setVisible(bool(notes))
        recipe = d.get("recipe") or {}
        sched = d.get("schedule") or {}
        describe = sched.get("describe") or sched.get("text") or ""
        step = model.next_step(last.get("status"), self.delivers()) if last and not d.get("waiting") else None
        values = {
            "recipe": (f"{recipe.get('title')} ({recipe.get('vendor')}), {recipe.get('ref')}" if recipe.get("title")
                       else recipe.get("ref")),
            "uses": d.get("uses"),
            "state": state_words(bool(d.get("enabled"))),
            "schedule": (f"{describe}; next {model.when(sched['next'])}" if d.get("enabled") and sched.get("next")
                         else describe),
            "delivery": (d.get("delivery") or {}).get("where"),
            "last": model.last_run_words(last),
            "step": step,
            "newer": d.get("newer_recipe"),
            "file": d.get("file"),
        }
        for key, value in values.items():
            self.fields[key].setText(str(value or ""))
            self.form.setRowVisible(self.fields[key], bool(value))

    def _show_layout(self, d: dict) -> None:
        shown = (d.get("file_prefix"), d.get("layout"))
        if shown == self.layout_shown:                  # unchanged: keep the scroll position and the focus
            return
        self.layout_shown = shown
        clear(self.layout_box)
        added: list[QWidget] = []
        for o in d.get("layout") or []:
            title, about = model.layout_about(d, o)
            added += [heading(title, 1.0), text(about, f"About {o['name']}")]
            if o.get("format") == "delimited":
                t = table(["#", "Header", "Field", "What it holds"], f"Columns of {o['name']}",
                          "Columns marked required must be filled, as the recipe says.")
                fill_table(t, [[str(i + 1), f"{c['header']}{' (required)' if c.get('required') else ''}",
                                c["field"], c.get("description") or ""] for i, c in enumerate(o["columns"])])
                t.setMinimumHeight(min(400, 40 + 26 * len(o["columns"])))
                added.append(t)
        for w in added:
            self.layout_box.addWidget(w)
        self.layout_box.addStretch(1)
        # Widgets added to a page already in the window go to the end of its Tab order, after the result: put
        # them straight after the section tabs, where the Layout tab is.
        before: QWidget = self.tabs.tabBar()
        for w in added:
            if w.focusPolicy() & Qt.FocusPolicy.TabFocus:
                QWidget.setTabOrder(before, w)
                before = w

    def _show_settings(self, d: dict) -> None:
        sched = d.get("schedule") or {}
        self.basics.show_rows([("Title", d.get("title") or ""), ("Schedule", sched.get("describe") or ""),
                               ("File name prefix", d.get("file_prefix") or "not set")])
        set_lines(self.filters, model.setting_lines(d.get("filters") or {}), "Every record the recipe selects.")
        set_lines(self.options, model.setting_lines(d.get("options") or {}), "The recipe's defaults.")
        checks = d.get("checks") or {}
        ratio = checks.get("min_item_ratio")
        self.checks_facts.show_rows([
            ("Minimum item count", f"{round(ratio * 100)}% of the last delivery" if ratio else "off"),
            ("Must be filled", ", ".join(checks.get("must_be_filled") or []) or "nothing")])
        legacy = d.get("legacy") or {}
        set_lines(self.legacy, model.setting_lines(legacy), "")
        self.legacy_title.setVisible(bool(legacy))
        self.legacy.setVisible(bool(legacy))

    def _show_delivery(self, d: dict) -> None:
        st = self.settings
        problem = st.get("error") or st.get("problem")
        self.delivery_problem.setText(problem or "")
        self.delivery_problem.setVisible(bool(problem))
        dl = st.get("delivery") or {"transport": (d.get("delivery") or {}).get("transport") or "none"}
        self.where.show_rows(model.delivery_rows(dl))
        transport = dl.get("transport") or "none"
        self.unconfirmed = transport == "sftp" and not dl.get("host_key_confirmed")
        self.test_button.setVisible(transport != "none")
        self.test_why.setVisible(self.unconfirmed)
        self.test_result.setText(self.checks.get(self.name, ""))
        self.test_result.setVisible(bool(self.checks.get(self.name)))
        paused = not d.get("enabled")
        self.paused_title.setVisible(paused)
        self.paused_text.setVisible(paused)
        if paused:
            parts = ["Its schedule does not run it, but you can still Preview and Run it by hand."]
            parts.append("It has no delivery yet: resumed, its scheduled runs only build files on this computer."
                         if transport == "none" else
                         "Resuming lets the schedule run it. The first delivery to a new destination is held for "
                         "your review anyway: nothing reaches the vendor until you have looked at the files and "
                         "chosen Review and send…")
            if self.unconfirmed:
                parts.append("The server key is not confirmed yet, so nothing is sent even when it runs. Enter the "
                             "vendor's fingerprint under Change delivery on the feed's page in the browser page "
                             "first.")
            self.paused_text.setText(" ".join(parts))

    # ------------------------------------------------------------ actions under way
    def set_idle(self, idle: bool, *, reviewing: bool = False, toggling: bool = False) -> None:
        """Actions only while nothing runs, here or on the page (the window says what runs); none that runs long
        while a release question is on the way (it would stop the release), and Pause or Resume not while the
        last press is being written."""
        self.idle = idle
        self.reviewing, self.toggling = reviewing, toggling
        long = idle and not reviewing
        wanted = {self.preview_button: long, self.run_button: long, self.build_button: long,
                  self.pause_button: idle and not toggling, self.review_button: idle and not reviewing,
                  self.test_button: long and not self.unconfirmed}
        focused = QApplication.focusWidget()
        if focused in wanted and not wanted[focused] and self.result.head.isVisible():
            # Disabling the focused button would move the focus on to the next one ("Open this feed in the
            # browser page", which the next Enter would press): keep it on the result, which says what runs.
            self.result.head.setFocus(Qt.FocusReason.OtherFocusReason)
        for b, on in wanted.items():
            b.setEnabled(on)

    def say(self, message: str | None) -> None:
        """What just happened, above the sections (the page's short notices): plain text, as written."""
        self.notice.setText(message or "")
        self.notice.setVisible(bool(message))

    def job_started(self, name: str, kind: str) -> None:
        self.results[name] = {"state": "running", "kind": kind, "lines": [], "since": time.monotonic()}
        if name == self.name:
            self.say(None)
            self.result.show_result(self.results[name], self.delivers())

    def job_lines(self, name: str, lines: list[str]) -> None:
        res = self.results.get(name)
        if res is None or res.get("state") != "running":
            return
        res["lines"].extend(lines)
        if name == self.name:
            self.result.show_lines(lines)

    def job_done(self, name: str, state: dict) -> int | None:
        """An action of ``name`` ended (its job's final as_dict). Returns the id of the run whose rows to read."""
        lines = list((self.results.get(name) or {}).get("lines") or [])     # the watcher sent them all already
        if state.get("status") != "done":
            self.results[name] = {"state": "error", "error": state.get("error"), "lines": lines}
            words = f"Did not run: {model.page_words(state.get('error'))}"
        else:
            res = state.get("result") or {}
            self.results[name] = {"state": "done", "status": res.get("status"), "run": res.get("run") or {},
                                  "report": res.get("report") or {}, "lines": lines, "seconds": state.get("seconds")}
            words = model.DONE_WORDS.get(res.get("status"), str(res.get("status")))
        if name == self.name:
            self.say(words)
            self.result.show_result(self.results[name], self.delivers())
        run = (self.results[name].get("run") or {})
        return run.get("id")

    def show_run(self, name: str, detail: dict, rows: dict | None, rows_error: str | None = None) -> bool:
        """A run opened from the History tab, with its first rows. False (nothing shown) while an action of the
        feed is under way: its result stays, with its log, until it ends."""
        if (self.results.get(name) or {}).get("state") == "running":
            return False
        self.results[name] = {"state": "done", "run": detail, "report": detail.get("report") or {}, "rows": rows,
                              "rows_error": rows_error}
        if name == self.name:
            self.say(f"Showing the run of {model.when(detail.get('started'))} below.")
            self.result.show_result(self.results[name], self.delivers())
        return True

    def show_rows(self, name: str, run_id: int, rows: dict | None, error: str | None = None) -> None:
        res = self.results.get(name) or {}
        if (res.get("run") or {}).get("id") != run_id:
            return                                      # another result is shown by now
        res["rows"], res["rows_error"] = rows, error
        if name == self.name:
            self.result.show_result(res, self.delivers())

    def running_here(self) -> bool:
        return (self.results.get(self.name) or {}).get("state") == "running"

    def delivery_check_started(self, name: str) -> None:
        self.checks[name] = "Testing the delivery. It logs in and looks at the folder; it sends nothing."
        if name == self.name:
            self.test_result.setText(self.checks[name])
            self.test_result.show()

    def delivery_check_done(self, name: str, state: dict) -> None:
        self.checks[name] = model.delivery_check_words(state)
        if name == self.name:
            self.test_result.setText(self.checks[name])
            self.test_result.show()
            self.say("The delivery test worked." if state.get("status") == "done" else "The delivery test failed.")

    # ------------------------------------------------------------ signals out
    def _preview(self) -> None:
        self.preview.emit(self.name)

    def _run(self) -> None:
        self.run.emit(self.name, True)

    def _build(self) -> None:
        self.run.emit(self.name, False)

    def _toggle(self) -> None:
        self.set_enabled.emit(self.name, not self.detail.get("enabled"))

    def _review(self) -> None:
        self.review.emit(self.name)

    def _test(self) -> None:
        self.check_delivery.emit(self.name)

    def _open_waiting_folder(self) -> None:
        if self.waiting_id is not None:
            self.open_folder.emit(self.waiting_id)

    def _open_run(self, row: int, _col: int) -> None:
        run_id = row_key(self.history, row)
        if run_id is not None:
            self.open_run.emit(self.name, int(run_id))

    def _rows_wanted(self, run_id: int, output: str) -> None:
        self.rows_wanted.emit(self.name, run_id, output)

    def _open_in_page(self) -> None:
        self.open_in_page.emit(self.name)
