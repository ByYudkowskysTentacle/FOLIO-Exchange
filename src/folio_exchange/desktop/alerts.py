"""Alerts: who hears about a run that needs a person, by email or in a chat channel (what `folio-exchange init`
asks, and what site.yaml's ``alerts`` holds).

The mail server's password and the webhook's address are secrets: they are typed into password boxes, which the
window empties as it takes them, and ``setupops.save_alerts`` keeps them in Windows Credential Manager (the
webhook under the site folder's name, as `secret set webhook` does), never in site.yaml. An empty box keeps the
one stored; the form is told only where each comes from.
"""
from __future__ import annotations

import re

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QCheckBox, QComboBox, QDialog, QFormLayout, QSpinBox, QVBoxLayout, QWidget

from .. import notify
from . import model
from .forms import Rows, edit, note, problem_label, secret_box, take, tell
from .pages import actions_row, button, heading

ADDRESS = re.compile(r"[^@\s]+@[^@\s]+")


class AlertsDialog(QDialog):
    save = Signal(dict, str)          # the form, and the version of site.yaml it was made on

    def __init__(self, settings: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.version = str(settings.get("version") or "")
        self.saving = False
        self.setWindowTitle("FOLIO-Exchange – alerts")
        self.resize(640, 640)
        box = QVBoxLayout(self)
        box.addWidget(heading("Change alerts", 1.3))
        if not settings.get("keyring"):
            box.addWidget(note("Windows Credential Manager isn't available on this computer, so FOLIO-Exchange can't "
                               "store a mail password or webhook address.",
                               "No Credential Manager"))
        form = QFormLayout()
        self.rows = Rows(form)
        self.on = QComboBox()
        self.on.setAccessibleName("When alerts are sent")
        for value, words in model.ALERT_ON:
            self.on.addItem(words, value)
        self.on.setCurrentIndex(max(0, self.on.findData(settings.get("on") or "failure")))
        self.rows.add("W&hen alerts go:", self.on)
        box.addLayout(form)

        email = settings.get("email") or {}
        self.use_email = QCheckBox("Send alerts by &email")
        self.use_email.setAccessibleName("Send alerts by email")
        self.use_email.setChecked(bool(settings.get("email")))
        box.addWidget(self.use_email)
        self.email_box = QWidget()
        eform = QFormLayout(self.email_box)
        self.email_rows = Rows(eform)
        self.to = edit("Send alerts to", ", ".join(email.get("to") or []))
        self.sender = edit("Alerts come from", str(email.get("from") or ""))
        self.host = edit("Mail server", str(email.get("smtp_host") or ""), placeholder="smtp.office365.com")
        self.port = QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(int(email.get("smtp_port") or 587))
        self.port.setAccessibleName("Mail server port")
        self.starttls = QCheckBox("Encrypt the connection with STARTTLS")
        self.starttls.setAccessibleName("Encrypt the connection with STARTTLS")
        self.starttls.setChecked(email.get("starttls", True) is not False)
        self.user = edit("Mail server user name", str(email.get("username") or ""))
        self.password = secret_box("Mail server password")
        stored = model.password_words(email.get("password")) if email.get("username") else None
        self.email_rows.add("&To:", self.to, "One or more addresses, separated by commas.")
        self.email_rows.add("&From:", self.sender, "The address alerts come from. Some mail servers require their own "
                            "address here.")
        self.email_rows.add("Mail &server (SMTP):", self.host, "Your mail server's name. Your email administrator can "
                            "tell you.")
        self.email_rows.add("&Port:", self.port, "Usually 587 (with STARTTLS), or 465.")
        eform.addRow(self.starttls)
        self.email_rows.add("&User name:", self.user, "Leave it empty if the mail server accepts alerts without a "
                            "login.")
        keyring = bool(settings.get("keyring"))         # without one, say where the core reads each secret instead
        self.email_rows.add("Pass&word:", self.password, (f"{stored}. Leave it empty to keep it." if stored and
                                                          stored != "Not stored yet" else
                                                          "Stored in Windows Credential Manager and never shown again."
                                                          if keyring else
                                                          "Read from the environment variable named in site.yaml's "
                                                          "alerts email settings (secret: env:NAME). Run "
                                                          "folio-exchange secret status to check it."))
        box.addWidget(self.email_box)

        hook = settings.get("webhook") or {}
        self.use_hook = QCheckBox("Send alerts to a &chat webhook")
        self.use_hook.setAccessibleName("Send alerts to a chat webhook")
        self.use_hook.setChecked(bool(hook))
        box.addWidget(self.use_hook)
        self.hook_box = QWidget()
        hform = QFormLayout(self.hook_box)
        self.hook_rows = Rows(hform)
        self.kind = QComboBox()
        self.kind.setAccessibleName("Kind of webhook")
        self.kind.addItem("Slack, or another webhook that accepts {\"text\"}, such as Mattermost or Google Chat", "slack")
        self.kind.addItem("A Microsoft Teams workflow", "teams")
        self.kind.setCurrentIndex(max(0, self.kind.findData(hook.get("format") or "slack")))
        self.url = secret_box("Webhook address")
        where = model.password_words(hook.get("url")) if hook else None
        self.hook_rows.add("&Kind:", self.kind)
        self.hook_rows.add("Webhook &address:", self.url, "Keep this address secret. Anyone who has it can post to "
                                                          "the channel. "
                           + ("It's stored in Windows Credential Manager under this site folder's name." if keyring else
                              "Without Credential Manager, it's read from the environment variable named in "
                              "site.yaml's alerts (webhook: env:NAME).")
                           + (f" {where}. Leave it empty to keep it." if where and where != "Not stored yet" else ""))
        box.addWidget(self.hook_box)
        box.addStretch(1)
        self.problem = problem_label()
        box.addWidget(self.problem)
        self.save_button = button("Sa&ve alerts", "Save alerts")
        self.save_button.setDefault(True)
        self.save_button.clicked.connect(self._save)
        cancel = button("Cancel", "Cancel without saving")
        cancel.clicked.connect(self.reject)
        box.addLayout(actions_row(self.save_button, cancel))
        self.use_email.toggled.connect(self._show)
        self.use_hook.toggled.connect(self._show)
        self._show()

    def _show(self, *_args) -> None:
        self.email_box.setVisible(self.use_email.isChecked())
        self.hook_box.setVisible(self.use_hook.isChecked())

    def form(self) -> dict | None:
        """The form for setupops.save_alerts (no secret in it), or None after showing what is wrong."""
        for rows in (self.rows, self.email_rows, self.hook_rows):
            rows.clear()
        self.problem.hide()
        out: dict = {"on": self.on.currentData(), "email": None, "webhook": None}
        if self.use_email.isChecked():
            to = [x for x in re.split(r"[,;\s]+", self.to.text()) if x]
            bad = next((x for x in to if not ADDRESS.fullmatch(x)), None)
            if not to or bad:
                return self._fail(self.email_rows, self.to, f"{bad} isn't an email address." if bad else
                                  "Enter the address to send alerts to.")
            if not ADDRESS.fullmatch(self.sender.text().strip()):
                return self._fail(self.email_rows, self.sender, "Enter the address the alerts come from.")
            if not self.host.text().strip():
                return self._fail(self.email_rows, self.host, "Enter the mail server's name.")
            user = self.user.text().strip()
            if user and not self.starttls.isChecked() and self.port.value() != 465:
                return self._fail(self.email_rows, self.port, "Logging in to the mail server needs STARTTLS or port "
                                  "465, so the password is never sent unencrypted.")
            if self.password.text() and not user:
                return self._fail(self.email_rows, self.user, "Enter the user name that goes with the password.")
            out["email"] = {"to": to, "from": self.sender.text().strip(), "smtp_host": self.host.text().strip(),
                            "smtp_port": self.port.value(), "starttls": self.starttls.isChecked(), "username": user}
        if self.use_hook.isChecked():
            if self.url.text():
                refused = notify.url_problem(self.url.text())
                if refused:
                    return self._fail(self.hook_rows, self.url, f"That webhook address isn't allowed: {refused}.")
            elif not self.settings.get("webhook"):
                return self._fail(self.hook_rows, self.url, "Enter the webhook address.")
            out["webhook"] = {"format": self.kind.currentData()}
        return out

    def _fail(self, rows: Rows, widget: QWidget, message: str) -> None:
        rows.fail(widget, message)
        return None

    def take_secrets(self) -> tuple[str | None, str | None]:
        """The typed mail password and webhook address (None when left empty); both boxes are emptied."""
        password = take(self.password) or None
        url = take(self.url) or None
        return (password if self.use_email.isChecked() else None), (url if self.use_hook.isChecked() else None)

    def _save(self) -> None:
        if self.saving:
            return
        form = self.form()
        if form is None:
            self.password.clear()
            self.url.clear()
            return
        self.save.emit(form, self.version)

    def working(self, busy: bool) -> None:
        self.saving = busy
        self.save_button.setEnabled(not busy)

    def failed(self, message: str) -> None:
        """Not saved: said under the form, announced and focused, so a screen reader reads it."""
        self.working(False)
        tell(self.problem, f"Not saved. {message}")

    def reject(self) -> None:
        if not self.saving:
            super().reject()
