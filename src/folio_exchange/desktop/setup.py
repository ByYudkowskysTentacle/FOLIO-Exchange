"""Connecting a site to FOLIO: the first page of a new site, and "Change connection or password" on the
Connection page.

The form checks what it can before anything is sent (the address, the tenant, the user name), then the window
logs in with ``setupops.connect`` as a job: FOLIO must accept the details before the password is stored in
Windows Credential Manager and site.yaml is written. The password box is emptied the moment its value is
taken, so a password is never kept by the form, whatever FOLIO answers.
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QFormLayout, QVBoxLayout, QWidget

from .. import setupops
from . import theme
from .forms import Rows, edit, note, problem_label, secret_box, take, tell
from .pages import Page, actions_row, button, heading, text

INTRO = ("FOLIO-Exchange reads your library's records from FOLIO with a read-only account and sends each vendor the "
         "files it asks for. Start by connecting it to FOLIO: the details are tested with FOLIO before anything is "
         "saved.")
NO_KEYRING = ("This computer has no Windows Credential Manager that FOLIO-Exchange can use, so the password cannot "
              "be stored from this app. Whoever looks after this computer needs to set the FOLIO_PASSWORD environment "
              "variable for this account; FOLIO-Exchange then uses it.")
SYNCED = ("This folder is inside OneDrive or Dropbox. Keep the site in a folder that is not synced: syncing can copy "
          "half-written files and the run history between computers.")


class ConnectForm(QWidget):
    """The FOLIO connection: address, tenant, account and password. ``submitted`` asks the window to log in; it
    then takes ``values()`` and ``take_password()`` (which empties the box) and reports back with ``done`` or
    ``failed``."""

    submitted = Signal()
    cancelled = Signal()

    def __init__(self, first: bool) -> None:
        super().__init__()
        self.first = first
        self.folio: dict = {}
        self.keyring = True
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        if not first:
            box.addWidget(heading("Change connection or password", 1.15))
        self.no_keyring = note(NO_KEYRING, "No Credential Manager")
        self.no_keyring.hide()
        box.addWidget(self.no_keyring)
        form = self.form_layout = QFormLayout()
        self.rows = Rows(form)
        self.url = edit("FOLIO API address", placeholder="https://")
        self.tenant = edit("Tenant id")
        self.user = edit("User name")
        self.password = secret_box("FOLIO password")
        self.rows.add("API &address:", self.url,
                      "On Eureka, the Kong gateway URL, starting https://. Your FOLIO administrator can tell you it.")
        self.rows.add("Te&nant id:", self.tenant, "The FOLIO tenant this library uses.")
        self.rows.add("&User name:", self.user, "A read-only service account, not a person's own login.")
        self.password_caption = self.rows.add("&Password:", self.password, self._password_help())
        box.addLayout(form)
        self.problem = problem_label()
        box.addWidget(self.problem)
        self.status = note(about="Connection status")
        self.status.hide()
        box.addWidget(self.status)
        self.save_button = theme.primary(button("&Test and save", "Test the connection with FOLIO, then save it"))
        self.save_button.clicked.connect(self._submit)
        self.cancel_button = button("Cancel", "Cancel changing the connection")
        self.cancel_button.clicked.connect(self.cancelled.emit)
        self.cancel_button.setVisible(not first)
        box.addLayout(actions_row(self.save_button, self.cancel_button))
        for w in (self.url, self.tenant, self.user, self.password):
            w.returnPressed.connect(self._submit)

    def _password_help(self) -> str:
        if not self.first:
            return "Leave empty to keep the stored password. A new one is checked with FOLIO before it replaces the old one."
        return ("Checked with FOLIO, then kept in Windows Credential Manager on this computer. It is never written to "
                "a file." + (" Leave it empty to use a password stored here before." if self.folio.get("username")
                             else ""))

    def fill(self, folio: dict, keyring: bool) -> None:
        """Start from the connection as it is (never its password, which the form is never told)."""
        self.folio, self.keyring = dict(folio or {}), keyring
        self.url.setText(str(self.folio.get("base_url") or ""))
        self.tenant.setText(str(self.folio.get("tenant") or ""))
        self.user.setText(str(self.folio.get("username") or ""))
        self.password.clear()
        self.form_layout.setRowVisible(self.password_caption, keyring)      # the field, its help and its problem
        self.rows.set_help(self.password, self._password_help())
        self.no_keyring.setVisible(not keyring)
        self.rows.clear()
        self.say(None)
        self.status.hide()                  # no login runs for a form that is just opened

    def values(self) -> dict[str, str]:
        return {"base_url": self.url.text().strip(), "tenant": self.tenant.text().strip(),
                "username": self.user.text().strip()}

    def take_password(self) -> str | None:
        """The typed password, or None; the box is empty afterwards."""
        return (take(self.password) or None) if self.keyring else None

    def _submit(self) -> None:
        """Check what can be checked here; the password box is emptied when the check fails too."""
        if not self.save_button.isEnabled():
            return
        self.rows.clear()
        self.say(None)
        v = self.values()
        bad = setupops.url_problem(v["base_url"].rstrip("/"))
        if bad:
            self.password.clear()
            self.rows.fail(self.url, bad)
        elif not v["tenant"]:
            self.password.clear()
            self.rows.fail(self.tenant, "Enter the tenant id.")
        elif not v["username"]:
            self.password.clear()
            self.rows.fail(self.user, "Enter the user name.")
        elif self.first and self.keyring and not self.password.text() and not self.folio.get("username"):
            self.rows.fail(self.password, "Enter the password for this account.")
        else:
            self.submitted.emit()

    def working(self, busy: bool) -> None:
        self.save_button.setEnabled(not busy)
        self.status.setText("Logging in to FOLIO…")
        self.status.setVisible(busy)

    def set_idle(self, idle: bool) -> None:
        """Another action is running (here or on the browser page): the form waits for it."""
        self.save_button.setEnabled(idle)

    def failed(self, message: str) -> None:
        """FOLIO turned the login down, or another action runs: said under the form, announced, and focused, so
        a screen reader reads it (the button only says its own name)."""
        self.password.clear()
        self.status.hide()
        tell(self.problem, message)

    def say(self, message: str | None) -> None:
        self.problem.setText(message or "")
        self.problem.setVisible(bool(message))


class SetupPage(Page):
    """A folder that is not a working site yet: connect it to FOLIO, here in the app."""

    def __init__(self) -> None:
        super().__init__()
        self.root: str | None = None
        self.title = heading("Connect to FOLIO")
        self.box.addWidget(self.title)
        self.intro = text(INTRO, "About connecting")
        self.box.addWidget(self.intro)
        self.what = text(about="What is wrong")
        self.what.hide()
        self.box.addWidget(self.what)
        self.synced = text(SYNCED, "Synced folder")
        self.synced.hide()
        self.box.addWidget(self.synced)
        self.form = ConnectForm(first=True)
        self.box.addWidget(self.form)
        self.folder = text(about="Site folder")
        self.box.addWidget(self.folder)

    def show_state(self, state: dict) -> None:
        """setupops.site_state of the folder. What was typed stays while the same folder is shown."""
        if state.get("problem"):
            self.title.setText("This site's settings do not load")
            self.what.setText(f"This folder's site.yaml could not be used. {state['problem']}\n\nCorrect the FOLIO "
                              "connection below, or correct site.yaml in a text editor and press F5.")
        else:
            self.title.setText("Connect to FOLIO")
        self.what.setVisible(bool(state.get("problem")))
        self.synced.setVisible(bool(state.get("synced_folder")))
        self.folder.setText(f"Site folder: {state.get('root')}")
        if state.get("root") != self.root:
            self.root = state.get("root")
            self.form.fill(state.get("folio") or {}, bool(state.get("keyring")))
