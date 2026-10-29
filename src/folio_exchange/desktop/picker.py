"""Choosing a site when none is named: a recent one, a folder that holds site.yaml, or a new site."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QStandardPaths, Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from .. import setupops, siteops
from ..errors import ConfigError
from . import places
from .pages import heading


def _error_label() -> QLabel:
    label = QLabel()
    label.setWordWrap(True)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    label.setAccessibleDescription("Problem")
    label.hide()
    return label


def _show_error(label: QLabel, text: str | None) -> None:
    label.setText(f"<b>Problem:</b> {_html(text)}" if text else "")
    label.setVisible(bool(text))


def _html(text: str | None) -> str:
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def default_parent() -> Path | None:
    """Where a new site goes unless the person chooses: Documents, or the home folder when Documents is synced
    (OneDrive moves it there on many computers, and a site must not live in a synced folder)."""
    for where in (QStandardPaths.StandardLocation.DocumentsLocation, QStandardPaths.StandardLocation.HomeLocation):
        folder = QStandardPaths.writableLocation(where)
        if folder and Path(folder).is_dir() and not setupops.synced_folder(folder):
            return Path(folder)
    return None


class NewSiteDialog(QDialog):
    """A new site's folder: where to put it and its name. The folder is made only inside one that exists."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Set up a new site")
        self.chosen: Path | None = None
        intro = QLabel("A site is a folder that holds a library's FOLIO connection, its feeds and their runs. "
                       "Choose where to put it and name it, for example after the library.")
        intro.setWordWrap(True)
        self.parent_edit = QLineEdit()
        self.parent_edit.setAccessibleName("Folder to put the new site in")
        browse = QPushButton("&Browse…")
        browse.setAccessibleName("Browse for the folder to put the new site in")
        browse.clicked.connect(self._browse)
        row = QHBoxLayout()
        row.addWidget(self.parent_edit, 1)
        row.addWidget(browse)
        self.name_edit = QLineEdit()
        self.name_edit.setAccessibleName("Name of the new site folder")
        self.full = QLabel()
        self.full.setWordWrap(True)
        self.full.setTextFormat(Qt.TextFormat.PlainText)       # a folder name, shown as written
        self.full.setAccessibleDescription("The new site folder")
        self.error = _error_label()
        form = QFormLayout()
        in_label = QLabel("&In folder:")
        in_label.setBuddy(self.parent_edit)
        name_label = QLabel("&Name:")
        name_label.setBuddy(self.name_edit)
        form.addRow(in_label, row)
        form.addRow(name_label, self.name_edit)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.create = buttons.addButton("&Create the site", QDialogButtonBox.ButtonRole.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout = QVBoxLayout(self)
        layout.addWidget(intro)
        layout.addLayout(form)
        layout.addWidget(self.full)
        layout.addWidget(self.error)
        layout.addWidget(buttons)
        self.parent_edit.textChanged.connect(self._preview)
        self.name_edit.textChanged.connect(self._preview)
        self.setTabOrder(self.parent_edit, browse)
        self.setTabOrder(browse, self.name_edit)
        start = default_parent()
        if start is not None:                           # a place to start; the name is what most people type
            self.parent_edit.setText(str(start))
            self.name_edit.setFocus(Qt.FocusReason.OtherFocusReason)
        self._preview()
        self.resize(560, self.sizeHint().height())

    def ask_folder(self) -> str:
        """The folder dialog (tests replace this)."""
        return QFileDialog.getExistingDirectory(self, "Choose where to put the new site", self.parent_edit.text())

    def _browse(self) -> None:
        folder = self.ask_folder()
        if folder:
            self.parent_edit.setText(str(Path(folder)))

    def target(self) -> Path:
        return Path(self.parent_edit.text().strip()).expanduser() / self.name_edit.text()

    def _preview(self) -> None:
        ready = bool(self.parent_edit.text().strip() and self.name_edit.text())
        self.full.setText(f"The site will be {self.target()}" if ready else "")
        _show_error(self.error, None)

    def problem(self) -> str | None:
        return places.new_site_problem(self.parent_edit.text().strip(), self.name_edit.text())

    def accept(self) -> None:
        problem = self.problem()
        if not problem:
            try:
                siteops.make_site_folder(self.target())
            except (ConfigError, OSError) as exc:
                problem = str(exc)
        if problem:
            _show_error(self.error, problem)
            return
        self.chosen = self.target().resolve()
        super().accept()


class SitePicker(QDialog):
    """Opens a recent site, a site folder, or a new site; ``chosen`` holds the folder once accepted."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("FOLIO-Exchange – choose a site")
        self.chosen: Path | None = None
        self.is_new = False
        title = heading("FOLIO-Exchange")
        about = QLabel("Scheduled exports from FOLIO to your vendors, such as collectionHQ: you choose what each "
                       "vendor gets, preview it, and nothing is sent until you say so.")
        about.setWordWrap(True)
        intro = QLabel("Each library's FOLIO-Exchange lives in a site folder. Open one, or set up a new site.")
        intro.setWordWrap(True)
        recent_label = QLabel("&Recent sites:")
        self.recent = QListWidget()
        self.recent.setAccessibleName("Recent sites")
        recent_label.setBuddy(self.recent)
        for root in places.recent_sites():
            item = QListWidgetItem(f"{root.name} – {root}")
            item.setData(Qt.ItemDataRole.UserRole, str(root))
            item.setToolTip(str(root))
            self.recent.addItem(item)
        if self.recent.count():
            self.recent.setCurrentRow(0)
        else:
            recent_label.setText("No recent sites yet.")
            self.recent.hide()
        self.recent.itemActivated.connect(lambda _item: self.open_recent())
        self.open_button = QPushButton("O&pen")
        self.open_button.setAccessibleName("Open the selected recent site")
        self.open_button.setEnabled(self.recent.count() > 0)
        self.open_button.clicked.connect(self.open_recent)
        folder_button = QPushButton("&Open a site folder…")
        folder_button.setAccessibleName("Open a site folder")
        folder_button.clicked.connect(self.open_folder)
        new_button = QPushButton("&Set up a new site…")
        new_button.setAccessibleName("Set up a new site")
        new_button.clicked.connect(self.new_site)
        cancel = QPushButton("Cancel")
        cancel.clicked.connect(self.reject)
        self.error = _error_label()
        row = QHBoxLayout()
        row.addWidget(self.open_button)
        row.addStretch(1)
        row.addWidget(folder_button)
        row.addWidget(new_button)
        row.addWidget(cancel)
        if not self.recent.count():                     # a first start: setting up is what Enter does
            for b in (self.open_button, folder_button, cancel):
                b.setAutoDefault(False)
            new_button.setDefault(True)
            new_button.setFocus(Qt.FocusReason.OtherFocusReason)
        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addWidget(about)
        layout.addWidget(intro)
        layout.addWidget(recent_label)
        layout.addWidget(self.recent)
        layout.addWidget(self.error)
        if not self.recent.count():
            layout.addStretch(1)
        layout.addLayout(row)
        self.resize(640, 360)

    def ask_folder(self) -> str:
        """The folder dialog (tests replace this)."""
        return QFileDialog.getExistingDirectory(self, "Choose the site folder (the one that holds site.yaml)")

    def new_site_dialog(self) -> NewSiteDialog:
        return NewSiteDialog(self)

    def open_recent(self) -> None:
        item = self.recent.currentItem()
        if item is not None:
            self.choose(Path(item.data(Qt.ItemDataRole.UserRole)))

    def open_folder(self) -> None:
        folder = self.ask_folder()
        if folder:
            self.choose(Path(folder))

    def choose(self, folder: Path) -> bool:
        problem = places.folder_problem(folder)
        _show_error(self.error, problem)
        if problem:
            return False
        self.chosen, self.is_new = folder.resolve(), False
        self.accept()
        return True

    def new_site(self) -> None:
        dialog = self.new_site_dialog()
        if dialog.exec() == QDialog.DialogCode.Accepted and dialog.chosen is not None:
            self.chosen, self.is_new = dialog.chosen, True
            self.accept()
