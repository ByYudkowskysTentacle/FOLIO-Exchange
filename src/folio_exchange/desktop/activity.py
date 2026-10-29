"""Activity: the latest runs of every feed, and every change to the site's settings (releases, overrides, secrets
stored, feeds paused and resumed), newest first. A run opened from the list shows below, with its rows."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QTabWidget, QVBoxLayout, QWidget

from . import model
from .pages import Page, fill_table, heading, row_key, table, text
from .runview import RunView


class ActivityPage(Page):
    open_run = Signal(int)                    # a run's id
    open_folder = Signal(int)
    rows_wanted = Signal(int, str)

    def __init__(self) -> None:
        super().__init__()
        self.shown: dict | None = None
        self.listed: dict | None = None
        self.delivers: dict[str, bool] = {}
        self.box.addWidget(heading("Activity"))
        self.box.addWidget(text("Every feed's runs and every change to the site, newest first.", "About Activity"))
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("Activity")
        runs = QWidget()
        rbox = QVBoxLayout(runs)
        self.runs = table(["Status", "Feed", "Started", "By", "Items", "Bibs", "Note"], "Runs of every feed",
                          "Newest first, previews included. Press Enter to open one below.")
        self.runs.cellActivated.connect(self._open_run)
        rbox.addWidget(self.runs)
        self.no_runs = text("No runs yet.", "Runs of every feed")
        rbox.addWidget(self.no_runs)
        changes = QWidget()
        cbox = QVBoxLayout(changes)
        self.changes = table(["When", "Who", "Feed", "What", "Details"], "Changes to the site",
                             "Settings changes, releases, overrides and secrets stored, newest first.")
        cbox.addWidget(self.changes)
        self.no_changes = text("Nothing recorded yet.", "Changes to the site")
        cbox.addWidget(self.no_changes)
        self.tabs.addTab(runs, "Runs")
        self.tabs.addTab(changes, "Changes")
        self.box.addWidget(self.tabs)
        self.notice = text(about="What just happened")
        self.notice.hide()
        self.box.addWidget(self.notice)
        self.result = RunView("Open a run in the list to see its rows and summary here.")
        self.result.open_folder.connect(self.open_folder)
        self.result.rows_wanted.connect(self.rows_wanted)
        self.box.addWidget(self.result)

    def show_activity(self, activity: dict, delivers: dict[str, bool]) -> None:
        """``activity``: {runs, audit} from ``model.snapshot``; ``delivers``: whether each feed has somewhere to
        send (for a run's next step)."""
        self.delivers = delivers
        if activity == self.listed:
            return                                  # unchanged: the selection and focus stay put
        self.listed = activity
        runs = activity.get("runs") or []
        fill_table(self.runs, [[model.status_words(r.get("status")), r.get("feed") or "", model.when(r.get("started")),
                                r.get("trigger") or "", model.num(r.get("items")), model.num(r.get("bibs")),
                                model.run_note(r)] for r in runs], [r["id"] for r in runs])
        self.runs.setVisible(bool(runs))
        self.no_runs.setVisible(not runs)
        audit = activity.get("audit") or []
        fill_table(self.changes, [[model.when(a.get("ts")), a.get("actor") or "", a.get("feed") or "",
                                   a.get("action") or "", model.detail_text(a.get("detail"))[:240]] for a in audit])
        self.changes.setVisible(bool(audit))
        self.no_changes.setVisible(not audit)
        self.tabs.setTabText(0, f"Runs ({len(runs)})")
        self.tabs.setTabText(1, f"Changes ({len(audit)})")

    def say(self, message: str | None) -> None:
        self.notice.setText(message or "")
        self.notice.setVisible(bool(message))

    def show_run(self, detail: dict, rows: dict | None, rows_error: str | None = None) -> None:
        self.shown = {"state": "done", "run": detail, "report": detail.get("report") or {}, "rows": rows,
                      "rows_error": rows_error}
        self.say(f"Showing the run of {detail.get('feed')} from {model.when(detail.get('started'))} below.")
        self.result.show_result(self.shown, self.delivers.get(detail.get("feed"), True))

    def show_rows(self, run_id: int, rows: dict | None, error: str | None = None) -> None:
        if not self.shown or (self.shown.get("run") or {}).get("id") != run_id:
            return
        self.shown["rows"], self.shown["rows_error"] = rows, error
        self.result.show_result(self.shown, self.delivers.get(self.shown["run"].get("feed"), True))

    def _open_run(self, row: int, _col: int) -> None:
        run_id = row_key(self.runs, row)
        if run_id is not None:
            self.open_run.emit(int(run_id))
