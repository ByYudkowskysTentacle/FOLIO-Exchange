"""Options: the site folder (and renaming it), the scheduled task that starts runs every hour, and the alerts.

Renaming works as on the page: the core (``siteops.rename_site``) refuses while a run holds the site, when a
setting names the folder by its full path, or when the name is taken, and says what is left to do. The window
runs it off its thread as the running action (``Jobs.holding``), so no other action starts meanwhile.

The scheduled task is registered by the command line (``folio-exchange schedule --register``) in a console window
of its own, where schtasks asks for the Windows password: the app never asks for it, sees it or keeps it. The
window waits for that console to close, then looks again whether the task is there.
"""
from __future__ import annotations

import platform
import subprocess
import sys
import sysconfig
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout

from .. import schedule, siteops
from ..launcher import program_path
from . import model
from .forms import Rows, edit
from .pages import Facts, Page, actions_row, button, heading, lines_label, set_lines, text

RENAME_ABOUT = ("The folder's name is this site's name: it shows at the top of this window and in alert messages, "
                "and the scheduled task and the desktop launcher are named after it. Renaming keeps the folder where "
                "it is. The FOLIO and vendor passwords are stored by account, so they carry on as they are.")
REGISTER_ABOUT = ("A console window opens and asks for your Windows password, so the task can run while nobody is "
                  "signed in. FOLIO-Exchange never sees the password. The task starts every hour at minute 7, also on "
                  "battery power, and runs the feeds that are due.")


def task_command(site_root: Path) -> tuple[str, list[str], str]:
    """How to start ``folio-exchange --site <root> schedule --register`` in a console window of its own, and the
    folder to start it in: this install's own program, never one found in the current folder (which may be a
    shared folder others can write to). Without the program, this Python with ``-m`` (and ``-P`` from Python
    3.11, which leaves the current folder off the path); python.exe rather than pythonw.exe, so there is a
    console for schtasks to ask in."""
    exe = Path(sys.executable)
    here = str(exe.parent)
    args = ["--site", str(site_root), "schedule", "--register"]
    script = Path(sysconfig.get_path("scripts")) / ("folio-exchange.exe" if sys.platform == "win32"
                                                    else "folio-exchange")
    if script.is_file():
        return str(script), args, here
    if sys.platform == "win32" and exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        exe = exe.parent / "python.exe"
    safe = ["-P"] if sys.version_info >= (3, 11) else []
    return str(exe), [*safe, "-m", "folio_exchange", *args], here


def start_console(exe: str, args: list[str], folder: str) -> int:
    """Run a command in a new, visible console window and wait for it to end (in an errand's thread). Nothing
    it reads or prints passes through the app: the console is its own."""
    done = subprocess.run([exe, *args], cwd=folder, stdin=None, stdout=None, stderr=None,
                          creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0))
    return done.returncode


def cron_line(site_root: Path) -> str:
    return schedule.os_command(site_root, program_path(), platform.system())


class OptionsPage(Page):
    rename = Signal(str)
    register = Signal()
    change_alerts = Signal()
    test_alert = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.windows = platform.system() == "Windows"
        self.site: dict = {}
        self.idle = True
        self.busy = {"rename": False, "register": False, "test": False}
        self.box.addWidget(heading("Options"))
        self.box.addWidget(text("This site's folder, its scheduled runs and its alerts.", "About options"))

        self.box.addWidget(heading("Rename the site folder", 1.15))
        self.done = text(about="Renamed")
        self.done.hide()
        self.box.addWidget(self.done)
        self.done_notes = lines_label("What is left to do")
        self.done_notes.hide()
        self.box.addWidget(self.done_notes)
        self.folder = Facts()
        self.box.addWidget(self.folder)
        self.box.addWidget(text(RENAME_ABOUT, "About renaming"))
        form = QFormLayout()
        self.rows = Rows(form)
        self.name = edit("New name of the site folder")
        self.rows.add("Folder &name:", self.name, "For example the library's name, such as Lakeside College Library. "
                      "Letters, numbers, spaces, dots, hyphens and brackets work everywhere.")
        self.box.addLayout(form)
        self.becomes = text(about="The new folder")
        self.box.addWidget(self.becomes)
        self.name.textChanged.connect(self._preview)
        self.name.returnPressed.connect(self._rename)
        self.rename_button = button("&Rename folder", "Rename the site folder")
        self.rename_button.clicked.connect(self._rename)
        self.box.addLayout(actions_row(self.rename_button))

        self.box.addWidget(heading("Scheduled runs", 1.15))
        self.task = text(about="Scheduled task")
        self.box.addWidget(self.task)
        self.registered = text(about="Task Scheduler")
        self.box.addWidget(self.registered)
        self.register_button = button("Re&gister the scheduled task…", "Register the scheduled task")
        self.register_button.clicked.connect(self._register)
        self.register_about = text(REGISTER_ABOUT, "About registering the task")
        self.box.addLayout(actions_row(self.register_button))
        self.box.addWidget(self.register_about)
        self.register_result = text(about="Registering the task")
        self.register_result.hide()
        self.box.addWidget(self.register_result)
        self.cron = text(about="The line to add with crontab -e")
        self.box.addWidget(self.cron)
        self.set_platform(self.windows)

        self.box.addWidget(heading("Alerts", 1.15))
        self.box.addWidget(text("Who hears about a run that failed, was blocked or waits for review, by email or in a "
                                "chat channel. The mail server's password and the webhook's address are kept in "
                                "Windows Credential Manager.", "About alerts"))
        self.alerts = Facts()
        self.box.addWidget(self.alerts)
        self.alerts_state = text(about="Alert settings")
        self.box.addWidget(self.alerts_state)
        self.alerts_button = button("Change &alerts…", "Change the alert settings")
        self.alerts_button.clicked.connect(self.change_alerts.emit)
        self.test_button = button("Send a &test alert", "Send a test alert")
        self.test_button.clicked.connect(self._test)
        self.box.addLayout(actions_row(self.alerts_button, self.test_button))
        self.test_result = text(about="Test alert")
        self.test_result.hide()
        self.box.addWidget(self.test_result)

    def set_platform(self, windows: bool) -> None:
        """Task Scheduler on Windows; elsewhere the crontab line to add (tests choose either)."""
        self.windows = windows
        for w in (self.register_button, self.register_about, self.registered):
            w.setVisible(windows)
        self.cron.setVisible(not windows)
        if not windows and self.site.get("root"):
            self.show_site(self.site)

    # ------------------------------------------------------------ the site
    def show_site(self, summary: dict) -> None:
        """views.site_summary: the folder, its parent (for the new name), the scheduled task's state."""
        first = summary.get("root") != self.site.get("root")
        self.site = summary
        self.folder.show_rows([("Site folder", str(summary.get("root") or ""))])
        if first:
            self.name.setText(str(summary.get("name") or ""))
        self._preview()
        self.task.setText(model.task_words(summary.get("task"), self.windows))
        if not self.windows:
            self.cron.setText(f"Nothing starts the runs until this computer's scheduler does, every hour. Add this "
                              f"line with crontab -e:\n{cron_line(Path(summary.get('root') or '.'))}")

    def _preview(self) -> None:
        parent = str(self.site.get("parent") or "")
        name = self.name.text().strip() or str(self.site.get("name") or "")
        sep = "\\" if "\\" in parent else "/"
        self.becomes.setText(f"The folder becomes: {parent if parent.endswith(sep) else parent + sep}{name}"
                             if parent else "")

    def _rename(self) -> None:
        self.rows.clear()
        if self.busy["rename"] or not self.rename_button.isEnabled():
            return
        name = self.name.text().strip()
        bad = ("Enter a name for the folder." if not name else
               "The folder already has that name." if name == self.site.get("name") else siteops.name_problem(name))
        if bad:
            self.rows.fail(self.name, bad)
            return
        self.rename.emit(name)

    def renaming(self, busy: bool) -> None:
        self.busy["rename"] = busy
        self._enable()

    def rename_failed(self, message: str) -> None:
        self.renaming(False)
        self.rows.fail(self.name, message)

    def renamed(self, res: dict) -> None:
        """siteops.rename_site's answer: what was carried along, and what is left (the task's two commands). With
        ``failed``, the folder moved but a step after it did not work (the window follows the folder anyway)."""
        self.renaming(False)
        if res.get("failed"):
            self.done.setText(f"Renamed to {res['name']}, but not everything after that worked: {res['failed']}")
        else:
            self.done.setText(f"Renamed to {res['name']}. " + ("One thing is left to do:" if res.get("task") else
                                                               "Nothing else needs doing."))
        set_lines(self.done_notes, list(res.get("notes") or []), "")
        self.done.show()
        self.done_notes.setVisible(bool(res.get("notes")))
        self.name.setText(res["name"])
        self.done.setFocus()

    # ------------------------------------------------------------ the scheduled task
    def show_registered(self, registered: bool | None, error: str | None = None) -> None:
        """Whether Task Scheduler has this site's task (None: still looking, or not Windows)."""
        if error:
            self.registered.setText(f"Could not ask Task Scheduler: {error}")
        elif registered is None:
            self.registered.setText("Looking in Task Scheduler…")
        else:
            self.registered.setText(f"Task Scheduler has the task \"{schedule.task_name(Path(self.site.get('root') or '.'))}\"."
                                    if registered else
                                    "Task Scheduler does not have this site's task yet, so no feed runs on its own. "
                                    "Register it below.")

    def _register(self) -> None:
        if not self.busy["register"] and self.register_button.isEnabled():
            self.register.emit()

    def registering(self, busy: bool, message: str | None = None) -> None:
        self.busy["register"] = busy
        self.register_result.setText(message or "")
        self.register_result.setVisible(bool(message))
        self._enable()

    # ------------------------------------------------------------ alerts
    def show_alerts(self, settings: dict | None, error: str | None = None) -> None:
        if error:
            self.alerts_state.setText(f"The alert settings could not be read: {error}")
            self.alerts_state.show()
            return
        self.alerts_state.hide()
        self.alerts.show_rows(model.alert_rows(settings or {}))

    def alert_said(self, message: str | None) -> None:
        self.alerts_state.setText(message or "")
        self.alerts_state.setVisible(bool(message))

    def _test(self) -> None:
        if not self.busy["test"] and self.test_button.isEnabled():
            self.test_alert.emit()

    def testing(self, busy: bool, message: str | None = None) -> None:
        self.busy["test"] = busy
        self.test_result.setText(message or ("Sending a test alert…" if busy else ""))
        self.test_result.setVisible(bool(self.test_result.text()))
        self._enable()

    def set_idle(self, idle: bool) -> None:
        self.idle = idle
        self._enable()

    def _enable(self) -> None:
        self.rename_button.setEnabled(self.idle and not self.busy["rename"])
        self.register_button.setEnabled(not self.busy["register"])
        self.alerts_button.setEnabled(self.idle)
        self.test_button.setEnabled(self.idle and not self.busy["test"])

    def clear_typed(self) -> None:
        self.rows.clear()
