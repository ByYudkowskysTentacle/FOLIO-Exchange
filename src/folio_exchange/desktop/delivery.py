"""Where a feed's files go, and how it logs in to the vendor: the page's "Change delivery" form and its login
section, for the app.

``DeliveryDialog`` sets the destination: the vendor's SFTP server, a folder, or none (a dry run). For SFTP the
window saves with ``setupops.save_delivery`` as a job, which reads every key the server offers and saves only
when the fingerprint the vendor gave matches one of them (or, when the person has no fingerprint yet, keeps the
server's key unconfirmed and the feed paused, so nothing logs in). A key that matches none refuses the change.

``LoginBox`` stores the SFTP password (in Windows Credential Manager) or a private key file (read here, kept as
an owner-only file in the site's keys folder, with its passphrase in Credential Manager). The boxes are emptied
the moment their values are taken; the key file is read only when it is saved, and only its text is handed on.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QButtonGroup,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QRadioButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from . import model, theme
from .forms import Rows, announce, edit, note, problem_label, secret_box, take, tell
from .pages import actions_row, button, heading, text

TRANSPORTS = (("sftp", "The vendor's SFTP server"), ("local", "A folder on this computer or the network"),
              ("none", "Not yet: keep it a dry run"))


class DeliveryDialog(QDialog):
    save = Signal(dict, str)              # the form, and the version of the feed file it was made on
    refresh_version = Signal()            # read the file again, then save the same choices on it

    def __init__(self, name: str, st: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.saving = False
        self.setWindowTitle(f"FOLIO-Exchange – delivery of {name}")
        self.resize(640, 560)
        box = QVBoxLayout(self)
        box.addWidget(heading("Change delivery", 1.3))
        box.addWidget(note("Where should the files go?", "Where the files go"))
        self.group = QButtonGroup(self)
        self.radios: dict[str, QRadioButton] = {}
        for value, words in TRANSPORTS:
            r = QRadioButton(words)
            r.setAccessibleName(words)
            self.group.addButton(r)
            self.radios[value] = r
            r.toggled.connect(self._show_box)
            box.addWidget(r)
        self.sftp_box = QWidget()
        form = QFormLayout(self.sftp_box)
        self.rows = Rows(form)
        self.host = edit("SFTP server name", placeholder="sftp.vendor.com")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setAccessibleName("Port")
        self.user = edit("SFTP user name")
        self.remote = edit("Folder on the server")
        self.fingerprint = edit("Server key fingerprint from the vendor", placeholder="SHA256:…")
        self.later = QCheckBox("I don't have it yet: save the server, but don't connect until I confirm it")
        self.later.setAccessibleName("I don't have the fingerprint yet: save the server, but don't connect until I "
                                     "confirm it")
        self.rows.add("Server &name:", self.host, "The SFTP server the vendor gave you, such as sftp.vendor.com.")
        self.rows.add("&Port:", self.port, "Usually 22.")
        self.rows.add("&User name:", self.user, "The SFTP user name the vendor gave you.")
        self.rows.add("&Folder on the server:", self.remote, "Where the files go on the vendor's server. A dot (.) is "
                      "the folder you land in when you log in.")
        self.fp_caption = self.rows.add("Server &key fingerprint from the vendor:", self.fingerprint, "Ask the "
                                        "vendor for it.")
        form.addRow(self.later)
        form.addRow(note("The feed is paused, and nothing logs in to the server, until you come back and enter the "
                         "vendor's fingerprint.", "About confirming later"))
        box.addWidget(self.sftp_box)
        self.local_box = QWidget()
        lform = QFormLayout(self.local_box)
        self.local_rows = Rows(lform)
        folder_row = QWidget()
        row = QHBoxLayout(folder_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.folder = edit("Folder to copy the files into", placeholder="D:\\Exports\\vendor")
        browse = button("&Browse…", "Browse for the folder to copy the files into")
        browse.clicked.connect(self._browse)
        row.addWidget(self.folder, 1)
        row.addWidget(browse)
        self.local_rows.add("F&older:", folder_row, "The folder's full path, on this computer or the network, such as "
                            "D:\\Exports\\vendor or \\\\server\\share\\vendor. It is made if it does not exist.",
                            focus=self.folder)
        box.addWidget(self.local_box)
        self.none_box = note("The feed keeps building its files on this computer only. Preview and Run still work.",
                             "A dry run")
        box.addWidget(self.none_box)
        box.addStretch(1)
        self.problem = problem_label()
        box.addWidget(self.problem)
        self.status = note(about="Saving")
        self.status.hide()
        box.addWidget(self.status)
        self.again_button = button("Save my choices on the file as it is no&w",
                                   "Save my choices on the feed file as it is now")
        self.again_button.clicked.connect(self.refresh_version.emit)
        self.again_button.hide()
        self.save_button = theme.primary(button("&Save delivery", "Save the delivery"))
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save)
        cancel = button("Cancel", "Close without saving")
        cancel.clicked.connect(self.reject)
        box.addLayout(actions_row(self.save_button, self.again_button, cancel))
        self.rebase(st)
        mode = (st.get("delivery") or {}).get("transport")
        self.radios[mode if mode in self.radios else "none"].setChecked(True)
        self._show_box()

    def rebase(self, st: dict, keep_form: bool = False) -> None:
        """The feed's settings (setupops.feed_settings): its delivery and the file's version."""
        self.st, self.version = st, str(st.get("version") or "")
        d = st.get("delivery") or {}
        self.known = (d.get("transport") == "sftp" and bool(d.get("host_key_confirmed"))
                      and bool(d.get("host_key_fingerprint")))
        if not keep_form:
            self.host.setText(str(d.get("host") or ""))
            self.port.setValue(int(d.get("port") or 22))
            self.user.setText(str(d.get("username") or ""))
            self.remote.setText(str(d.get("remote_dir") or "."))
            self.folder.setText(str(d.get("directory") or ""))
        help_text = ("Ask the vendor for it: it starts SHA256: (or is an older value such as 3f:a2:…:9c). On saving, "
                     "FOLIO-Exchange reads the server's own key and saves only if they match, so files cannot go to a "
                     "server pretending to be the vendor's." + (" This server's key is already confirmed: leave this "
                                                                "empty to keep it." if self.known else ""))
        self.rows.set_help(self.fingerprint, help_text)

    def transport(self) -> str:
        return next((v for v, r in self.radios.items() if r.isChecked()), "none")

    def _show_box(self, *_args) -> None:
        t = self.transport()
        self.sftp_box.setVisible(t == "sftp")
        self.local_box.setVisible(t == "local")
        self.none_box.setVisible(t == "none")

    def ask_folder(self) -> str:
        """The folder dialog (tests replace this)."""
        return QFileDialog.getExistingDirectory(self, "Choose the folder to copy the files into", self.folder.text())

    def _browse(self) -> None:
        folder = self.ask_folder()
        if folder:
            self.folder.setText(str(Path(folder)))

    def form(self) -> dict | None:
        """The form for setupops.save_delivery, or None after showing what is wrong (as the page checks it)."""
        self.rows.clear()
        self.local_rows.clear()
        self.problem.hide()
        t = self.transport()
        if t == "sftp":
            host = self.host.text().strip()
            fp = self.fingerprint.text().strip()
            if not host:
                return self._fail(self.rows, self.host, "Enter the SFTP server name the vendor gave you.")
            if not self.user.text().strip():
                return self._fail(self.rows, self.user, "Enter the SFTP user name the vendor gave you.")
            bad = model.fingerprint_problem(fp)
            if bad:
                return self._fail(self.rows, self.fingerprint, bad)
            d = self.st.get("delivery") or {}
            same = (self.known and host.lower() == str(d.get("host") or "").lower()
                    and self.port.value() == int(d.get("port") or 22))
            if not fp and not self.later.isChecked() and not same:
                return self._fail(self.rows, self.fingerprint, "Enter the fingerprint the vendor gave you, or tick the "
                                  "box below to confirm it later.")
            return {"transport": "sftp", "host": host, "port": self.port.value(), "username": self.user.text().strip(),
                    "remote_dir": self.remote.text().strip() or ".", "vendor_fingerprint": fp,
                    "confirm_later": self.later.isChecked() and not fp}
        if t == "local":
            bad = model.folder_problem(self.folder.text())
            if bad:
                return self._fail(self.local_rows, self.folder, bad)
            return {"transport": "local", "directory": self.folder.text().strip()}
        return {"transport": "none"}

    def _fail(self, rows: Rows, widget: QWidget, message: str) -> None:
        rows.fail(widget, message)
        return None

    def _save(self) -> None:
        if self.saving:
            return
        form = self.form()
        if form is not None:
            self.save.emit(form, self.version)

    def working(self, busy: bool) -> None:
        self.saving = busy
        self.save_button.setEnabled(not busy)
        self.again_button.setEnabled(not busy)
        self.status.setText("Reading the server's key…" if self.transport() == "sftp" else "Saving…")
        self.status.setVisible(busy)

    def failed(self, message: str) -> None:
        """Not saved (the server's key matches no fingerprint entered, a value refused, another action running):
        said under the form, announced and focused, so a screen reader reads it."""
        self.working(False)
        self.again_button.hide()
        tell(self.problem, f"Not saved. {message}")

    def conflict(self, message: str) -> None:
        """The feed file changed underneath the form: nothing was saved and what was entered stays. The button
        that saves the same choices on the file as it is now takes the focus, and carries the reason."""
        self.working(False)
        words = f"Not saved: {model.first_sentence(message)} Nothing was saved, and what you entered is still here."
        self.problem.setText(words)
        self.problem.show()
        self.again_button.show()
        self.again_button.setAccessibleDescription(words)
        self.again_button.setFocus(Qt.FocusReason.OtherFocusReason)
        announce(self.problem)

    def reject(self) -> None:
        if not self.saving:
            super().reject()


class LoginBox(QWidget):
    """How a feed logs in to the vendor's SFTP server: a password, or a private key file with its passphrase.
    ``save_password`` and ``save_key`` ask the window to store them; it then calls ``take_password`` or
    ``take_key``, which empty the boxes."""

    save_password = Signal()
    save_key = Signal()

    def __init__(self) -> None:
        super().__init__()
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(heading("Login to the vendor's server", 1.15))
        self.no_keyring = text("This computer has no Windows Credential Manager that FOLIO-Exchange can use, so "
                               "passwords cannot be stored from this app.", "No Credential Manager")
        self.no_keyring.hide()
        box.addWidget(self.no_keyring)
        self.key_saved = text(about="Key saved")
        self.key_saved.hide()
        box.addWidget(self.key_saved)
        self.group = QButtonGroup(self)
        self.use_password = QRadioButton("A password")
        self.use_password.setAccessibleName("Log in with a password")
        self.use_key = QRadioButton("A private key file")
        self.use_key.setAccessibleName("Log in with a private key file")
        for r in (self.use_password, self.use_key):
            self.group.addButton(r)
            r.toggled.connect(self._show_mode)
        box.addWidget(note("Log in with:", "Log in with"))
        box.addWidget(self.use_password)
        box.addWidget(self.use_key)

        self.password_box = QWidget()
        pform = QFormLayout(self.password_box)
        self.password_rows = Rows(pform)
        self.password = secret_box("SFTP password")
        self.password_rows.add("SFTP pass&word:", self.password, "Kept in Windows Credential Manager on this "
                               "computer. It is never shown again or written to a file.")
        self.password.returnPressed.connect(self._save_password)
        self.password_button = theme.primary(button("Save in Cre&dential Manager", "Save the SFTP password in Credential Manager"))
        self.password_button.clicked.connect(self._save_password)
        pform.addRow(actions_row(self.password_button))
        box.addWidget(self.password_box)

        self.key_box = QWidget()
        kform = QFormLayout(self.key_box)
        self.key_rows = Rows(kform)
        file_row = QWidget()
        row = QHBoxLayout(file_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.key_file = edit("Private key file")
        self.key_file.setReadOnly(True)
        choose = self.choose_button = button("C&hoose the key file…", "Choose the private key file")
        choose.clicked.connect(self._choose)
        row.addWidget(self.key_file, 1)
        row.addWidget(choose)
        self.key_rows.add("Private key file:", file_row, "The private key whose public half the vendor has (OpenSSH "
                          "or PEM format). A PuTTY .ppk key needs converting first; the message says how. It is kept "
                          "in this site's keys folder, readable only by this Windows user.", focus=choose)
        self.key_rows.problems[self.key_file] = self.key_rows.problems.pop(choose)
        self.passphrase = secret_box("Key passphrase")
        self.key_rows.add("Passphrase (if the key has one):", self.passphrase, "Kept in Windows Credential Manager.")
        self.key_button = theme.primary(button("Save &key", "Save the private key"))
        self.key_button.clicked.connect(self._save_key)
        kform.addRow(actions_row(self.key_button))
        box.addWidget(self.key_box)
        self.problem = problem_label()
        box.addWidget(self.problem)
        self.use_password.setChecked(True)
        self.idle = True
        self.saving = False
        self.shown: tuple | None = None

    def show_login(self, name: str, d: dict, keyring: bool) -> None:
        """A feed's delivery (setupops.feed_settings' delivery, which holds no secret). The choice of password or
        key follows the feed's own until the person changes it; a refresh of the same feed keeps their choice."""
        self.no_keyring.setVisible(not keyring)
        shown = (name, d.get("auth"))
        if shown != self.shown:
            self.shown = shown
            (self.use_key if d.get("auth") == "key" else self.use_password).setChecked(True)

    def _show_mode(self, *_args) -> None:
        self.password_box.setVisible(self.use_password.isChecked())
        self.key_box.setVisible(self.use_key.isChecked())

    def ask_key_file(self) -> str:
        """The file dialog (tests replace this)."""
        name, _filter = QFileDialog.getOpenFileName(self, "Choose the private key file")
        return name

    def _choose(self) -> None:
        name = self.ask_key_file()
        if name:
            self.key_file.setText(str(Path(name)))

    def clear_secrets(self) -> None:
        """Nothing typed or chosen here outlives the feed shown (another feed, another view)."""
        self.password.clear()
        self.passphrase.clear()
        self.key_file.clear()
        self.password_rows.clear()
        self.key_rows.clear()
        self.problem.hide()
        self.key_saved.hide()

    def take_password(self) -> str:
        return take(self.password)

    def take_key(self) -> tuple[str, str | None]:
        """The chosen file's path and the passphrase (None when none was typed); both boxes are emptied."""
        path = self.key_file.text()
        self.key_file.clear()
        return path, take(self.passphrase) or None

    def _save_password(self) -> None:
        self.password_rows.clear()
        self.problem.hide()
        if self.saving or not self.password_button.isEnabled():
            return
        if not self.password.text():
            self.password_rows.fail(self.password, "Enter the password the vendor gave you.")
            return
        self.save_password.emit()

    def _save_key(self) -> None:
        self.key_rows.clear()
        self.problem.hide()
        if self.saving or not self.key_button.isEnabled():
            return
        if not self.key_file.text():
            self.passphrase.clear()
            self.key_rows.fail(self.key_file, "Choose the private key file.")
            return
        self.save_key.emit()

    def working(self, busy: bool) -> None:
        self.saving = busy
        self._enable()

    def set_idle(self, idle: bool) -> None:
        self.idle = idle
        self._enable()

    def _enable(self) -> None:
        on = self.idle and not self.saving
        self.password_button.setEnabled(on)
        self.key_button.setEnabled(on)

    def failed(self, message: str, key: bool = False) -> None:
        """Not stored: said under the box, announced and focused (keyboard-selectable), so a screen reader reads
        it; the boxes are already empty."""
        self.working(False)
        tell(self.problem, f"{message} Choose the file again to retry." if key else message)

    def saved_key(self, public: str) -> None:
        self.working(False)
        self.key_saved.setText(f"Key saved. Its public fingerprint is {public}. The vendor's server must know the "
                               "matching public key (the .pub file).")
        self.key_saved.show()
