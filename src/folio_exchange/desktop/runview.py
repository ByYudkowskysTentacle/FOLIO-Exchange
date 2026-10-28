"""One run's result, as the page's result panel shows it: an action under way with its log streaming, an action
that did not run, or a run with the rows it sent, its summary and its log.

A feed's page shows the result of its last action or of a run opened from its history; Activity shows a run
opened from its list. The view only lays out what it is given; the rows of another file of the run are asked
for with ``rows_wanted`` and its folder with ``open_folder``.
"""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QPlainTextEdit, QTabWidget, QVBoxLayout, QWidget

from . import model
from .pages import Facts, button, fill_table, heading, lines_label, set_lines, table, text

TICK_MS = 1000


class RunView(QWidget):
    open_folder = Signal(int)                 # the run's id
    rows_wanted = Signal(int, str)            # the run's id, the output whose rows to show

    def __init__(self, empty: str) -> None:
        super().__init__()
        self.empty_words = empty
        self.run_id: int | None = None
        self.kind = ""
        self.started = 0.0
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.title = heading("Result", 1.15)
        box.addWidget(self.title)
        self.head = text(about="Result")
        self.folder_button = button("&Open folder", "Open the run's folder")
        self.folder_button.clicked.connect(self._open_folder)
        row = QHBoxLayout()
        row.addWidget(self.head, 1)
        row.addWidget(self.folder_button)
        box.addLayout(row)

        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("Result views")
        rows = QWidget()
        rbox = QVBoxLayout(rows)
        self.rows_line = text(about="Rows shown")
        self.output = QComboBox()
        self.output.setAccessibleName("Which file")
        self.output.currentTextChanged.connect(self._output_chosen)
        orow = QHBoxLayout()
        orow.addWidget(self.rows_line, 1)
        orow.addWidget(self.output)
        rbox.addLayout(orow)
        # Named for any run: a preview or a build shows its rows too, and those sent nothing.
        self.rows = table([], "Rows of the run's file", "The first rows of the run's file, as the vendor would "
                          "receive them.")
        self.rows.setMinimumHeight(160)
        rbox.addWidget(self.rows)
        summary = QWidget()
        sbox = QVBoxLayout(summary)
        self.summary = Facts()
        sbox.addWidget(self.summary)
        self.warnings_title = heading("Warnings", 1.0)
        self.warnings = lines_label("Warnings of this run")
        self.files_title = heading("Files", 1.0)
        self.files = lines_label("Files of this run")
        self.sent_title = heading("Sent to", 1.0)
        self.sent = lines_label("Where the files were sent")
        for w in (self.warnings_title, self.warnings, self.files_title, self.files, self.sent_title, self.sent):
            sbox.addWidget(w)
        sbox.addStretch(1)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setAccessibleName("Log of the action")
        self.log.setMinimumHeight(140)
        self.tabs.addTab(rows, "Rows")
        self.tabs.addTab(summary, "Summary")
        self.tabs.addTab(self.log, "Log")
        box.addWidget(self.tabs)
        self.tick = QTimer(self)
        self.tick.setInterval(TICK_MS)
        self.tick.timeout.connect(self._show_time)
        self.show_empty()

    # ------------------------------------------------------------ states
    def _tabs(self, rows: bool, summary: bool, log: bool) -> None:
        for i, on in enumerate((rows, summary, log)):
            self.tabs.setTabVisible(i, on)
        self.tabs.setVisible(rows or summary or log)

    def show_empty(self, words: str | None = None) -> None:
        self.tick.stop()
        self.run_id = None
        self.head.setText(words or self.empty_words)
        self.folder_button.hide()
        self._tabs(False, False, False)

    def show_result(self, result: dict | None, delivers: bool = True) -> None:
        """Lay out a result as the feed page keeps it: {state: running|error|done, kind, lines, seconds, error,
        run, report, rows, rows_error}. None is no result yet."""
        if not result:
            self.show_empty()
            return
        state = result.get("state")
        lines = result.get("lines")
        if state == "running":
            self.run_id = None
            self.started = result.get("since") or time.monotonic()
            self.kind = result.get("kind")
            self._show_time()
            self.tick.start()
            self.folder_button.hide()
            self.log.setPlainText("\n".join(lines or []) or "Starting…")
            self._tabs(False, False, True)
            self.tabs.setCurrentWidget(self.log)
            return
        self.tick.stop()
        if state == "error":
            self.run_id = None
            self.head.setText(f"Did not run: {model.page_words(result.get('error')) or 'no reason was given'}")
            self.folder_button.hide()
            self.log.setPlainText("\n".join(lines or []))
            self._tabs(False, False, bool(lines))
            return
        self._show_run(result, delivers)

    def _show_time(self) -> None:
        doing = model.DOING_WORDS.get(self.kind, "Working")
        self.head.setText(f"{doing}… Time {round(time.monotonic() - self.started)} s. Other actions wait until it "
                          "finishes.")

    def _show_run(self, result: dict, delivers: bool) -> None:
        run = result.get("run") or {}
        rep = result.get("report") or {}
        b = rep.get("build") or {}
        self.run_id = run.get("id")
        when = (f"Time {result['seconds']} s" if result.get("seconds") is not None else
                model.when(run.get("started")))
        self.head.setText(" · ".join([
            model.status_words(run.get("status") or result.get("status")), when,
            f"Bibs {model.num(run.get('bibs') if run.get('bibs') is not None else b.get('bibs_written'))}",
            f"Items {model.num(run.get('items') if run.get('items') is not None else b.get('items_written'))}",
            f"Files {len(run.get('files') or [])}"]))
        self.folder_button.setVisible(bool(run.get("id") and run.get("folder")))
        self.folder_button.setToolTip(str(run.get("folder") or ""))
        self.summary.show_rows(model.run_summary(run, rep, delivers))
        warnings = [model.page_words(w) for w in rep.get("warnings") or []]
        set_lines(self.warnings, warnings, "")
        self.warnings_title.setVisible(bool(warnings))
        self.warnings.setVisible(bool(warnings))
        set_lines(self.files, [f"{f['name']}  {model.size(f.get('bytes'))}" for f in run.get("files") or []],
                  "None.")
        sent = [str(d) for d in run.get("destinations") or []]
        set_lines(self.sent, sent, "")
        self.sent_title.setVisible(bool(sent))
        self.sent.setVisible(bool(sent))
        lines = result.get("lines")
        self.log.setPlainText("\n".join(lines or []))
        self._tabs(True, True, lines is not None)
        self._show_rows(result)

    def _show_rows(self, result: dict) -> None:
        r = result.get("rows")
        self.output.blockSignals(True)
        self.output.clear()
        if not r:
            self.rows_line.setText(result.get("rows_error") or "Loading rows…")
            fill_table(self.rows, [])
            self.rows.setColumnCount(0)
            self.output.hide()
        elif not r.get("header"):
            self.rows_line.setText("This run's file is no longer there." if r.get("missing") else
                                   "This run has no item rows to show.")
            self.rows.setRowCount(0)
            self.rows.setColumnCount(0)
            self.output.hide()
        else:
            shown, total = len(r["rows"]), r.get("total") or 0
            self.rows_line.setText(f"{r.get('file')}: " + (f"first {model.num(shown)} of {model.num(total)} rows"
                                                           if total > shown else f"{model.num(total)} rows"))
            self.rows.setColumnCount(len(r["header"]))
            self.rows.setHorizontalHeaderLabels([str(h) for h in r["header"]])
            fill_table(self.rows, [[str(c) for c in row] for row in r["rows"]])
            outputs = r.get("outputs") or []
            self.output.addItems(outputs)
            self.output.setCurrentText(r.get("output") or "")
            self.output.setVisible(len(outputs) > 1)
        self.output.blockSignals(False)

    def show_lines(self, lines: list[str]) -> None:
        """New log lines of the action under way."""
        if self.log.toPlainText() == "Starting…":
            self.log.clear()
        for line in lines:
            self.log.appendPlainText(line)

    def _open_folder(self) -> None:
        if self.run_id is not None:
            self.open_folder.emit(self.run_id)

    def _output_chosen(self, output: str) -> None:
        if self.run_id is not None and output:
            self.rows_wanted.emit(self.run_id, output)
