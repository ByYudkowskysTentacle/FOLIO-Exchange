"""Editing a feed's everyday settings (title, schedule, file prefix, filters, the item-count check and migrated
values), as the page's Settings tab does: fill in the form, review what changes ("before → after"), then save.

The form starts from ``setupops.feed_settings`` and remembers the file's version. The window saves with
``setupops.save_feed`` as a job; the core refuses when the file changed underneath the form, and then this
dialog offers to lay the same changes over the file as it is now and review them again. Nothing typed is lost
on a refusal. The layout (which columns the vendor gets) is the recipe's, and is not changed here.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QScrollArea,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from .. import setupops
from . import model
from .forms import (
    COMMAS,
    SCHEDULE_HELP,
    LegacyFields,
    PickList,
    Rows,
    SchedulePicker,
    announce,
    edit,
    location_options,
    note,
    problem_label,
    status_options,
    tell,
)
from .pages import _esc, actions_row, button, heading


class SettingsDialog(QDialog):
    save = Signal(dict, str)          # the values that changed, and the version of the file they were made on
    reapply = Signal(dict)            # read the file again and lay these changes over it

    def __init__(self, name: str, st: dict, choices: dict | None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.name = name
        self.saving = False
        self.send: dict = {}
        self.setWindowTitle(f"FOLIO-Exchange – settings of {name}")
        self.resize(720, 640)
        outer = QVBoxLayout(self)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages)
        self.pages.addWidget(self._form_page())
        self.pages.addWidget(self._review_page())
        self.set_choices(choices)
        self.rebase(st)

    # ------------------------------------------------------------ the form
    def _form_page(self) -> QWidget:
        w = QWidget()
        box = QVBoxLayout(w)
        box.addWidget(heading("Edit settings", 1.3))
        self.notice = note(about="Notice")
        self.notice.hide()
        box.addWidget(self.notice)
        self.lists_note = note(about="FOLIO's lists")
        self.lists_note.hide()
        box.addWidget(self.lists_note)
        box.addWidget(note("The layout (which columns and fields the vendor gets) comes from the recipe: see the "
                           "feed's Layout tab.", "About the settings"))
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        inner = QWidget()
        form = QFormLayout(inner)
        self.rows = Rows(form)
        self.title_edit = edit("Title")
        self.title_edit.setMaxLength(200)
        self.schedule = SchedulePicker("Settings")
        self.prefix = edit("File name prefix")
        self.rows.add("&Title:", self.title_edit, "How the feed is shown in this app and in alerts.")
        self.schedule_caption = self.rows.add("When it &runs:", self.schedule)
        self.schedule.custom.setAccessibleName("Another schedule")
        self.rows.problems[self.schedule.custom] = self.rows.problems.pop(self.schedule)
        self.rows.helps[self.schedule.custom] = SCHEDULE_HELP       # kept when a problem is cleared or shown
        self.rows.add("File name &prefix:", self.prefix, "The start of each file's name. Change it only if the vendor "
                      "asks.")
        form.addRow(heading("Which records", 1.0))
        self.loc = PickList("Locations to leave out")
        self.status = PickList("Item statuses to leave out")
        self.only = PickList("Only these locations")
        for caption, picker, help_text in (
                ("&Locations to leave out:", self.loc, "Items in these locations are not sent."),
                ("Item stat&uses to leave out:", self.status, "Items with these statuses are not sent, for example "
                 "withdrawn or lost items."),
                ("&Only these locations:", self.only, "When any are chosen, only items in those locations are sent. "
                 "Choose none to send every location.")):
            self.rows.add(caption, picker, help_text)
            picker.set_help(help_text)
        self.flags: dict[str, QCheckBox] = {}
        for key in model.FILTER_FLAGS:
            check = QCheckBox(model.setting_name(key))
            check.setAccessibleName(model.setting_name(key))
            self.flags[key] = check
            form.addRow(check)
        self.flag_help = note(about="The recipe's defaults")
        form.addRow(self.flag_help)
        form.addRow(heading("Check before sending", 1.0))
        ratio_row = QWidget()
        row = QHBoxLayout(ratio_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.ratio = edit("Block a delivery if the item count falls below this percentage of the last one")
        self.ratio.setMaximumWidth(80)
        row.addWidget(self.ratio)
        unit = QLabel("% of the last one")
        unit.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(unit)
        row.addStretch(1)
        self.rows.add("&Block a delivery if the item count falls below:", ratio_row,
                      "For example 90: a run with fewer than 90% of the items last delivered is held for review "
                      "instead of being sent. Leave it empty or 0 to switch the check off.", focus=self.ratio)
        form.addRow(heading("Items migrated from an older system", 1.0))
        self.legacy = LegacyFields("Settings")
        form.addRow(self.legacy)
        scroll.setWidget(inner)
        box.addWidget(scroll, 1)
        self.problem = problem_label()
        box.addWidget(self.problem)
        self.review_button = button("Re&view changes", "Review the changes before saving them")
        self.review_button.setDefault(True)
        self.review_button.clicked.connect(self.show_review)
        cancel = button("Cancel", "Close without saving")
        cancel.clicked.connect(self.reject)
        box.addLayout(actions_row(self.review_button, cancel))
        return w

    def _review_page(self) -> QWidget:
        w = QWidget()
        box = QVBoxLayout(w)
        self.review_title = heading("Review changes", 1.3)
        box.addWidget(self.review_title)
        self.review_text = QLabel()
        self.review_text.setWordWrap(True)
        self.review_text.setTextFormat(Qt.TextFormat.RichText)          # every value escaped
        self.review_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                                 | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.review_text.setAccessibleDescription("What changes")
        box.addWidget(self.review_text)
        self.review_note = note(about="After saving")
        box.addWidget(self.review_note)
        box.addStretch(1)
        self.save_button = button("&Save", "Save the settings")
        self.save_button.clicked.connect(self._save)
        self.apply_button = button("&Apply my changes to the file as it is now",
                                   "Apply my changes to the file as it is now, and review them again")
        self.apply_button.clicked.connect(self._reapply)
        self.back_button = button("Back to &editing", "Back to editing")
        self.back_button.clicked.connect(self.back_to_editing)
        self.close_button = button("Close", "Close without saving")
        self.close_button.clicked.connect(self.reject)
        box.addLayout(actions_row(self.save_button, self.apply_button, self.back_button, self.close_button))
        return w

    def set_choices(self, choices: dict | None) -> None:
        """{"data": setupops.tenant_choices} or {"error": ...}: without the lists, codes are typed in (and the
        form says so, as the page's does)."""
        self.choices = (choices or {}).get("data")
        error = (choices or {}).get("error")
        self.lists_note.setText(f"FOLIO's lists could not be read ({error}), so locations and statuses are typed as "
                                f"codes. {COMMAS}" if error else "")
        self.lists_note.setVisible(bool(error))

    def rebase(self, st: dict, overlay: dict | None = None) -> None:
        """Show the feed's settings ``st`` (with ``overlay``, the person's own changes, laid over them)."""
        self.st = st
        self.version = str(st.get("version") or "")
        self.base = model.settings_base(st)
        start = model.overlaid(self.base, overlay) if overlay else self.base
        c = self.choices
        self.title_edit.setText(start["title"])
        self.schedule.set_value(start["schedule"])
        self.prefix.setText(start["file_prefix"])
        f = start["filters"]
        self.loc.set_options(location_options(c), f.get("exclude_location_codes") or [], model.MISSING_LOCATION)
        self.status.set_options(status_options(c), f.get("exclude_item_statuses") or [], model.MISSING_STATUS)
        self.only.set_options(location_options(c), f.get("include_location_codes") or [], model.MISSING_LOCATION)
        defaults = st.get("filter_defaults") or {}
        for key, check in self.flags.items():
            check.setChecked(bool(f.get(key)))
            check.setAccessibleDescription(f"The recipe's default: {model.readable(bool(defaults.get(key)))}.")
        self.flag_help.setText("The recipe's defaults: " + "; ".join(
            f"{model.setting_name(k)}: {model.readable(bool(defaults.get(k)))}" for k in model.FILTER_FLAGS) + ".")
        ratio = start["checks"].get("min_item_ratio")
        self.ratio_text = f"{float(ratio) * 100:.1f}".rstrip("0").rstrip(".") if ratio else ""
        self.ratio_start = ratio or 0
        self.ratio.setText(self.ratio_text)
        self.legacy.set_values(start["legacy"], c.get("note_types") if c else None)
        self.notice.setText("The form now shows the file as it is now, with your changes laid over it. Check them, "
                            "then save." if overlay else "")
        self.notice.setVisible(bool(overlay))
        self.back_to_editing()

    def read(self) -> dict:
        t = self.ratio.text().strip()
        ratio = self.ratio_start if t == self.ratio_text else 0 if not t else float(t) / 100
        return {"title": self.title_edit.text().strip(), "schedule": self.schedule.value(),
                "file_prefix": self.prefix.text().strip(),
                "filters": {"exclude_location_codes": self.loc.value(), "exclude_item_statuses": self.status.value(),
                            "include_location_codes": self.only.value(),
                            **{k: c.isChecked() for k, c in self.flags.items()}},
                "checks": {"min_item_ratio": ratio}, "legacy": self.legacy.value()}

    # ------------------------------------------------------------ review, save
    def show_review(self) -> None:
        """Check the form and show what would change."""
        self.rows.clear()
        self.problem.hide()
        t = self.ratio.text().strip()
        try:
            n = float(t) if t else 0.0
        except ValueError:
            n = -1.0
        if not 0 <= n <= 100:
            self.rows.fail(self.ratio, "Enter a percentage from 0 to 100, or leave it empty to switch the check off.")
            return
        values = self.read()
        bad = ("Enter a schedule, or choose one above." if not values["schedule"]
               else setupops.schedule_problem(values["schedule"]))
        if bad:
            self.rows.fail(self.schedule.custom, bad)
            return
        self.send, changes = model.settings_changes(self.base, values)
        self.review_title.setText("Review changes" if changes else "Nothing has changed")
        self.review_text.setText("<ul>" + "".join(f"<li><b>{_esc(what)}:</b> {_esc(before)} → {_esc(after)}</li>"
                                                  for what, before, after in changes) + "</ul>" if changes else
                                 "The form is the same as the saved settings.")
        self.review_note.setText("Use Preview after saving to see the effect before the next delivery." if changes
                                 else "")
        self.save_button.setVisible(bool(changes))
        self.apply_button.hide()
        self.close_button.setVisible(not changes)
        self.pages.setCurrentIndex(1)
        (self.save_button if changes else self.back_button).setFocus(Qt.FocusReason.OtherFocusReason)

    def back_to_editing(self) -> None:
        self.pages.setCurrentIndex(0)
        self.review_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _save(self) -> None:
        if self.saving:
            return
        self.save.emit(self.send, self.version)

    def _reapply(self) -> None:
        """Read the file again and lay the reviewed changes over it. The form cannot be changed meanwhile, so
        nothing done while the file is read is lost when the answer redraws it."""
        if self.saving:
            return
        self.working(True, "Reading the file as it is now…")
        self.reapply.emit(self.send)

    def working(self, busy: bool, words: str = "Saving…") -> None:
        self.saving = busy
        self.save_button.setEnabled(not busy)
        self.apply_button.setEnabled(not busy)
        self.back_button.setEnabled(not busy)
        if busy:
            self.review_note.setText(words)

    def failed(self, message: str) -> None:
        """Not saved (a value the core refuses, or another action running): back to the form, which says why."""
        self.working(False)
        self.back_to_editing()
        tell(self.problem, f"Not saved. {message}")

    def conflict(self, message: str) -> None:
        """The file changed underneath the form: nothing was saved, and what was entered is still here."""
        self.working(False)
        self.review_title.setText("Not saved: the file changed")
        self.review_text.setText(_esc(f"{model.first_sentence(message)} Nothing was saved, and what you entered is "
                                      "still here."))
        self.review_note.setText("")
        self.save_button.hide()
        self.close_button.hide()
        self.apply_button.show()
        self.pages.setCurrentIndex(1)
        self.apply_button.setAccessibleDescription(f"{model.first_sentence(message)} Nothing was saved.")
        self.apply_button.setFocus(Qt.FocusReason.OtherFocusReason)
        announce(self.review_text)

    def reject(self) -> None:
        if not self.saving:                   # a save under way finishes first: its answer comes back here
            super().reject()
