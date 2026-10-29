"""The question before a held, blocked or built run is sent: what it would send, where, and why it waited.

It shows ``views.release_summary`` (which looks only) as the page's "Send these files?" does, and names the feed
in its title and first row, since the question can come a moment after it was asked for. Cancel has the focus
and is the default button, so Enter or Escape sends nothing; Enter acts on the focused button, as on the page,
so only "Send files" (focused, or Alt+S) accepts. The window then releases the run shown only if the core,
looking again, finds the same summary (its files unchanged, the same destination, no newer run).
"""
from __future__ import annotations

from PySide6.QtWidgets import QDialog, QVBoxLayout, QWidget

from . import model, theme
from .pages import Facts, actions_row, button, heading, lines_label, set_lines, text


class ReleaseDialog(QDialog):
    def __init__(self, summary: dict, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"FOLIO-Exchange – send files for {summary.get('feed')}?")
        self.setModal(True)
        box = QVBoxLayout(self)
        box.addWidget(heading("Send these files?", 1.25))
        self.facts = Facts()
        self.facts.show_rows(model.release_rows(summary))
        box.addWidget(self.facts)
        self.files = lines_label("Files to send")
        set_lines(self.files, [f"{name}  {model.size(size)}" for name, size in summary.get("files") or []], "None.")
        box.addWidget(self.files)
        self.newer = text("A newer run was delivered since this one was built.", "Warning")
        self.newer.setVisible(bool(summary.get("newer_runs")))
        box.addWidget(self.newer)
        self.cancel_button = button("&Cancel", "Cancel and send nothing")
        self.send_button = theme.primary(button("&Send files", "Send files"))
        self.cancel_button.setDefault(True)
        for b in (self.cancel_button, self.send_button):
            b.setAutoDefault(True)                     # Enter acts on the focused button
        self.cancel_button.clicked.connect(self.reject)
        self.send_button.clicked.connect(self.accept)
        box.addLayout(actions_row(self.cancel_button, self.send_button))
        self.cancel_button.setFocus()
