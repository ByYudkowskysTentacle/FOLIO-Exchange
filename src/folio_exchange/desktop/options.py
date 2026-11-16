"""Options: the site folder (and renaming it), the scheduled task that starts runs every hour, the alerts, the
tray (whether it watches this site, and whether it starts when the person signs in to Windows), and updates
(this version, when a newer one was last looked for, and how to get it).

Renaming works as on the page: the core (``siteops.rename_site``) refuses while a run holds the site, when a
setting names the folder by its full path, or when the name is taken, and says what is left to do. The window
runs it off its thread as the running action (``Jobs.holding``), so no other action starts meanwhile.

The scheduled task is registered by the command line (``folio-exchange schedule --register``) in a console window
of its own, where schtasks asks for the Windows password: the app never asks for it, sees it or keeps it. The
window waits for that console to close, then looks again whether the task is there.

Updates (phase U0 of docs/updates-plan.md) only tell: the newer version, its release notes as plain text, and the
pip command or the release page. Nothing is downloaded or installed from here. With ``FOLIO_EXCHANGE_UPDATES=off``
(or ``"check": false`` in updates.json) the section says IT manages updates and offers nothing else.
"""
from __future__ import annotations

import platform
import subprocess
import sys
import sysconfig
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QApplication, QCheckBox, QFormLayout

from .. import schedule, siteops, updates
from ..launcher import program_path
from . import model, theme
from .forms import Rows, edit, tell
from .pages import Facts, Page, actions_row, button, divider, heading, lines_label, set_lines, text

RENAME_ABOUT = ("The folder's name is the site's name. It shows at the top of this window and in alerts, and the "
                "scheduled task and desktop launcher are named after it. Renaming keeps the folder in the same place, "
                "and stored passwords keep working.")
REGISTER_ABOUT = ("A console window opens and asks for your Windows password, so the task can run when nobody is "
                  "signed in. Type it only in that window. FOLIO-Exchange never sees it. The task starts at 7 minutes "
                  "past every hour, even on battery, and runs the feeds that are due.")
TRAY_ABOUT = ("The FOLIO-Exchange icon in the taskbar's notification area shows each feed's state. It tells you when a "
              "scheduled run fails or is held for review, even when this window is closed.")
STARTUP_OTHER = ("When you sign in, Windows starts the tray from another copy of FOLIO-Exchange. Turn this off and on "
                 "again to use this copy.")
UPDATES_ABOUT = ("Once a day, FOLIO-Exchange asks GitHub whether a new version is out. It sends nothing about your "
                 "library.")
UPDATES_MANAGED = "Updates on this computer are managed by IT."
UPDATES_BY_IT = "Your IT staff install updates on this computer."
UPDATE_HOW = "To update, close FOLIO-Exchange and run this command in a command window:"
PRERELEASE_ABOUT = "Until version 1.0, every release is a pre-release."
NOTES_MOST = 3000                 # characters of release notes shown; the release page has the rest


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


def update_command(version: str) -> str:
    """The pip command that installs ``version`` over this install, run by this Python's full path (the console
    Python beside pythonw, so pip's progress shows)."""
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and (exe.parent / "python.exe").is_file():
        exe = exe.parent / "python.exe"
    return updates.pip_command(version, str(exe))


def cron_line(site_root: Path) -> str:
    return schedule.os_command(site_root, program_path(), platform.system())


class OptionsPage(Page):
    rename = Signal(str)
    register = Signal()
    change_alerts = Signal()
    test_alert = Signal()
    watch = Signal(bool)
    startup = Signal(bool)
    check_updates = Signal()
    update_choice = Signal(str, bool)          # "prereleases" or "automatic", and whether it's ticked
    open_release = Signal(str)                 # the release page's address

    def __init__(self) -> None:
        super().__init__()
        self.windows = platform.system() == "Windows"
        self.site: dict = {}
        self.idle = True
        self.busy = {"rename": False, "register": False, "test": False, "update": False}
        self.update_state: dict = {}
        self.box.addWidget(heading("Options"))
        self.box.addWidget(text("The site folder, scheduled runs, alerts, the tray and updates.", "About options"))

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
        self.rows.add("Folder &name:", self.name, "For example, Lakeside College Library. "
                      "Letters, numbers, spaces, periods, hyphens and parentheses work everywhere.")
        self.box.addLayout(form)
        self.becomes = text(about="The new folder")
        self.box.addWidget(self.becomes)
        self.name.textChanged.connect(self._preview)
        self.name.returnPressed.connect(self._rename)
        self.rename_button = theme.primary(button("&Rename folder", "Rename folder"))
        self.rename_button.clicked.connect(self._rename)
        self.box.addLayout(actions_row(self.rename_button))
        self.box.addWidget(divider())

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
        self.box.addWidget(divider())

        self.box.addWidget(heading("Alerts", 1.15))
        self.box.addWidget(text("Who hears about a run that failed, was blocked or is waiting for review, by email or "
                                "chat. The mail server password and webhook address are stored in Windows Credential "
                                "Manager.", "About alerts"))
        self.alerts = Facts()
        self.box.addWidget(self.alerts)
        self.alerts_state = text(about="Alert settings")
        self.box.addWidget(self.alerts_state)
        self.alerts_button = button("Change &alerts…", "Change alerts")
        self.alerts_button.clicked.connect(self.change_alerts.emit)
        self.test_button = button("Send a &test alert", "Send a test alert")
        self.test_button.clicked.connect(self._test)
        self.box.addLayout(actions_row(self.alerts_button, self.test_button))
        self.test_result = text(about="Test alert")
        self.test_result.hide()
        self.box.addWidget(self.test_result)
        self.box.addWidget(divider())

        self.box.addWidget(heading("Tray and notifications", 1.15))
        self.box.addWidget(text(TRAY_ABOUT, "About the tray"))
        self.watch_box = QCheckBox("&Watch this site from the tray")
        self.watch_box.setAccessibleName("Watch this site from the tray")
        self.watch_box.clicked.connect(lambda *_: self.watch.emit(self.watch_box.isChecked()))
        self.box.addWidget(self.watch_box)
        self.startup_box = QCheckBox("Start the tray when I sign &in to Windows")
        self.startup_box.setAccessibleName("Start the tray when I sign in to Windows")
        self.startup_box.clicked.connect(lambda *_: self.startup.emit(self.startup_box.isChecked()))
        self.box.addWidget(self.startup_box)
        self.startup_note = text(about="Starting the tray")
        self.startup_note.hide()
        self.box.addWidget(self.startup_note)
        self.tray_result = text(about="Tray settings")
        self.tray_result.hide()
        self.box.addWidget(self.tray_result)
        self.box.addWidget(divider())

        self.box.addWidget(heading("Updates", 1.15))
        self.updates_about = text(UPDATES_ABOUT, "About updates")
        self.box.addWidget(self.updates_about)
        self.update_facts = Facts()
        self.box.addWidget(self.update_facts)
        self.update_managed = text(about="Who installs updates")
        self.update_available = text(about="New version")
        self.update_notes = text(about="Release notes")
        self.update_how = text(UPDATE_HOW, "How to update")
        self.update_line = text(about="The command that updates FOLIO-Exchange")
        for w in (self.update_managed, self.update_available, self.update_notes, self.update_how, self.update_line):
            w.hide()
            self.box.addWidget(w)
        self.copy_button = button("Cop&y the command", "Copy the command")
        self.copy_button.clicked.connect(self._copy_command)
        self.release_button = button("&Open the release page", "Open the release page")
        self.release_button.clicked.connect(self._open_release)
        self.copy_button.hide()
        self.release_button.hide()
        self.box.addLayout(actions_row(self.copy_button, self.release_button))
        self.check_button = button("Check for &updates", "Check for updates")
        self.check_button.clicked.connect(self._check_updates)
        self.box.addLayout(actions_row(self.check_button))
        self.update_result = text(about="Checking for updates")
        self.update_result.hide()
        self.box.addWidget(self.update_result)
        self.prerelease_box = QCheckBox("Get &pre-releases")
        self.prerelease_box.setAccessibleName("Get pre-releases")
        self.prerelease_box.clicked.connect(
            lambda *_: self.update_choice.emit("prereleases", self.prerelease_box.isChecked()))
        self.box.addWidget(self.prerelease_box)
        self.prerelease_about = text(PRERELEASE_ABOUT, "About pre-releases")
        self.box.addWidget(self.prerelease_about)
        self.automatic_box = QCheckBox("Check for updates automati&cally")
        self.automatic_box.setAccessibleName("Check for updates automatically")
        self.automatic_box.clicked.connect(
            lambda *_: self.update_choice.emit("automatic", self.automatic_box.isChecked()))
        self.box.addWidget(self.automatic_box)
        self.box.addWidget(divider())
        self.set_platform(self.windows)

    def set_platform(self, windows: bool) -> None:
        """Task Scheduler on Windows; elsewhere the crontab line to add (tests choose either)."""
        self.windows = windows
        for w in (self.register_button, self.register_about, self.registered, self.startup_box):
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
            self.cron.setText(f"Add this line with crontab -e so this computer's scheduler starts the runs every "
                              f"hour:\n{cron_line(Path(summary.get('root') or '.'))}")

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
            self.done.setText(f"Renamed to {res['name']}, but a later step failed: {res['failed']}")
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
            self.registered.setText(f"Couldn't check Task Scheduler: {error}")
        elif registered is None:
            self.registered.setText("Checking Task Scheduler…")
        else:
            self.registered.setText(f"Task Scheduler has the task \"{schedule.task_name(Path(self.site.get('root') or '.'))}\"."
                                    if registered else
                                    "Task Scheduler doesn't have this site's task yet. Register it below.")

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
            self.alerts_state.setText(f"Couldn't load the alert settings: {error}")
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

    # ------------------------------------------------------------ the tray
    def show_tray(self, watched: bool, startup: str | None) -> None:
        """Whether the tray watches this site (places.is_watched), and the Startup entry's state
        (startup.startup_state: "on", "other", "off", or None off Windows or when it can't be read)."""
        self.watch_box.setChecked(watched)
        self.startup_box.setChecked(startup in ("on", "other"))
        self.startup_note.setText(STARTUP_OTHER if startup == "other" else "")
        self.startup_note.setVisible(self.windows and startup == "other")

    def tray_said(self, message: str | None) -> None:
        self.tray_result.setText(message or "")
        self.tray_result.setVisible(bool(message))

    # ------------------------------------------------------------ updates
    def show_updates(self, status: dict, result: dict | None = None) -> None:
        """updates.status(): this version, when a newer one was last looked for, the newer version and how to get
        it, and the two choices. ``result``: the answer to a check the person asked for (updates.check's)."""
        self.update_state = status
        off = status.get("mode") == "off"
        new = status.get("available")
        rows = [("This version", str(status.get("version") or ""))]
        if not off:
            checked = model.when(status["checked"]) if status.get("checked") else "Not yet"
            rows.append(("Last checked", f"{checked}. The last check didn't work." if status.get("failed") else checked))
        self.update_facts.show_rows(rows)
        self.update_managed.setText(UPDATES_MANAGED if off else UPDATES_BY_IT)
        self.update_managed.setVisible(off or (bool(new) and status.get("mode") == "notify"))
        self.update_available.setText(f"FOLIO-Exchange {new['version']} is available." if new else "")
        self.update_available.setVisible(bool(new))
        notes = str(new.get("notes") or "") if new else ""
        if len(notes) > NOTES_MOST:
            notes = notes[:NOTES_MOST].rstrip() + "…\n\nThe release page has the rest."
        self.update_notes.setText(notes)
        self.update_notes.setVisible(bool(notes))
        command = bool(new) and status.get("mode") == "on"       # "notify": tell, but never offer to install
        self.update_line.setText(update_command(new["version"]) if command else "")
        for w in (self.update_how, self.update_line, self.copy_button):
            w.setVisible(command)
        self.release_button.setVisible(bool(new))
        for w in (self.updates_about, self.check_button, self.prerelease_box, self.automatic_box):
            w.setVisible(not off)
        self.prerelease_about.setVisible(not off and (updates.running_version() or (1, 0, 0)) < (1, 0, 0))
        self.prerelease_box.setChecked(bool(status.get("prereleases")))
        self.automatic_box.setChecked(bool(status.get("automatic")))
        if off:
            self.update_result.hide()
        if result is None:
            return
        self.busy["update"] = False
        self._enable()
        if result.get("problem"):
            tell(self.update_result, f"Couldn't check for updates. {result['problem']}")
        elif new:
            self.update_result.hide()
            tell(self.update_available, self.update_available.text())
        else:
            tell(self.update_result, "You have the newest version.")

    def checking_updates(self) -> None:
        self.busy["update"] = True
        self.update_result.setText("Checking for updates…")
        self.update_result.show()
        self._enable()

    def updates_said(self, message: str | None) -> None:
        self.update_result.setText(message or "")
        self.update_result.setVisible(bool(message))

    def _check_updates(self) -> None:
        if not self.busy["update"]:
            self.check_updates.emit()

    def _copy_command(self) -> None:
        QApplication.clipboard().setText(self.update_line.text())
        self.updates_said("Copied the command.")

    def _open_release(self) -> None:
        new = self.update_state.get("available")
        if new:
            self.open_release.emit(new["page"])

    def set_idle(self, idle: bool) -> None:
        self.idle = idle
        self._enable()

    def _enable(self) -> None:
        self.rename_button.setEnabled(self.idle and not self.busy["rename"])
        self.register_button.setEnabled(not self.busy["register"])
        self.alerts_button.setEnabled(self.idle)
        self.test_button.setEnabled(self.idle and not self.busy["test"])
        self.check_button.setEnabled(not self.busy["update"])

    def clear_typed(self) -> None:
        self.rows.clear()
