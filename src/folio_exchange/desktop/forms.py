"""Parts the setup forms share (connecting to FOLIO, the new-feed wizard, a feed's settings and delivery, alerts,
the site folder): a form's rows, each with its caption, help and a place for its problem; password boxes; pick
lists of codes; the schedule picker; and the fields for values migrated from an older system.

As everywhere in the app, labels are plain text (a value from FOLIO is never read as markup), every field has an
accessible name, and a problem is shown as written beside its field, which takes the focus so a screen reader
reads both. A password box shows dots, offers nothing to copy, and is emptied by the form as soon as its value is
taken (``take``), whatever happens next.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QAccessible, QAccessibleEvent
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from . import model

SCHEDULE_HELP = ("For example daily 06:00, Mon,Thu 06:00 or monthly 15 01:00 (days 1 to 28). Times are in the site's "
                 "own time zone.")
COMMAS = "Separate them with commas."


def edit(name: str, value: str = "", *, placeholder: str | None = None) -> QLineEdit:
    box = QLineEdit(value)
    box.setAccessibleName(name)
    if placeholder:
        box.setPlaceholderText(placeholder)
    return box


def secret_box(name: str) -> QLineEdit:
    """A password box: shows dots (screen readers hear none of it), offers nothing to copy or drag out."""
    box = QLineEdit()
    box.setAccessibleName(name)
    box.setEchoMode(QLineEdit.EchoMode.Password)
    box.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
    box.setDragEnabled(False)
    box.setAttribute(Qt.WidgetAttribute.WA_InputMethodEnabled, False)      # no input method keeps what is typed
    return box


def take(box: QLineEdit) -> str:
    """A password box's value, emptied at once: the form keeps nothing that was typed into it."""
    value = box.text()
    box.clear()
    return value


def note(text: str = "", about: str | None = None) -> QLabel:
    """Help under a field, or a notice: plain text, wrapped, selectable."""
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                  | Qt.TextInteractionFlag.TextSelectableByKeyboard)
    if about:
        label.setAccessibleDescription(about)
    return label


def problem_label() -> QLabel:
    label = note(about="Problem")
    label.hide()
    return label


def announce(label: QLabel) -> None:
    """Tell screen readers a problem appeared (an alert event on the label that shows it)."""
    try:
        QAccessible.updateAccessibility(QAccessibleEvent(label, QAccessible.Event.Alert))
    except (RuntimeError, TypeError):       # no accessibility on this platform: the text is still shown
        pass


def tell(label: QLabel, message: str) -> None:
    """A problem with a form as a whole (FOLIO turned the login down, a save failed or was refused): shown on
    ``label`` and announced. The label (keyboard-selectable) takes the focus when it can be seen, so a screen
    reader reads it as the focus lands, rather than only the name of a button."""
    label.setText(message)
    label.show()
    if label.isVisible():
        label.setFocus(Qt.FocusReason.OtherFocusReason)
    announce(label)


class Rows:
    """A form's rows on a QFormLayout: a caption (its mnemonic moves to the field), the field, help under it and
    a problem line, which ``fail`` fills and ``clear`` empties."""

    def __init__(self, form: QFormLayout) -> None:
        self.form = form
        self.problems: dict[QWidget, QLabel] = {}
        self.helps: dict[QWidget, str] = {}
        self.help_labels: dict[QWidget, QLabel] = {}

    def add(self, caption: str, widget: QWidget, help_text: str | None = None, focus: QWidget | None = None
            ) -> QLabel:
        """``focus``: the widget the caption's mnemonic and a problem move to, when not ``widget`` itself."""
        label = QLabel(caption)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setBuddy(focus or widget)
        box = QVBoxLayout()
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(widget)
        target = focus or widget
        if help_text:
            self.help_labels[target] = note(help_text, f"About {caption.replace('&', '').rstrip(':')}")
            box.addWidget(self.help_labels[target])
            target.setAccessibleDescription(help_text)
            self.helps[target] = help_text
        problem = problem_label()
        box.addWidget(problem)
        self.problems[target] = problem
        self.form.addRow(label, box)
        return label

    def set_help(self, widget: QWidget, help_text: str) -> None:
        self.helps[widget] = help_text
        widget.setAccessibleDescription(help_text)
        if widget in self.help_labels:
            self.help_labels[widget].setText(help_text)

    def fail(self, widget: QWidget, message: str) -> None:
        """Show ``message`` beside ``widget`` and put the focus there."""
        problem = self.problems.get(widget)
        if problem is not None:
            problem.setText(message)
            problem.show()
            widget.setAccessibleDescription(f"{message} {self.helps.get(widget, '')}".strip())
            announce(problem)
        widget.setFocus(Qt.FocusReason.OtherFocusReason)

    def clear(self) -> None:
        for widget, problem in self.problems.items():
            problem.clear()
            problem.hide()
            widget.setAccessibleDescription(self.helps.get(widget, ""))

    def shown_problems(self) -> list[str]:
        return [p.text() for p in self.problems.values() if not p.isHidden()]


class PickList(QWidget):
    """Codes to choose: a list of check boxes (with a filter box when it is long), or, when FOLIO's list could
    not be read, a field for codes separated by commas (which its help, placeholder and a line under it say, as
    the page's does). A chosen value FOLIO's list does not have stays chosen, labelled with ``missing`` (for
    example "not a location in this FOLIO")."""

    changed = Signal()

    def __init__(self, name: str) -> None:
        super().__init__()
        self.name = name
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.filter = edit(f"Filter {name.lower()}", placeholder="Type to filter the list")
        self.filter.textChanged.connect(self._filter)
        self.list = QListWidget()
        self.list.setAccessibleName(name)
        self.list.itemChanged.connect(self._count)
        self.codes = edit(name, placeholder="Codes, separated by commas")
        self.codes.setAccessibleDescription(COMMAS)
        self.commas = note(COMMAS, f"About {name.lower()}")
        self.count = note(about=f"How many {name.lower()} are chosen")
        for w in (self.filter, self.list, self.codes, self.commas, self.count):
            self.box.addWidget(w)
        self.help = ""
        self.typed = False
        self.set_options(None, [])

    def set_help(self, help_text: str) -> None:
        """What the list is for, read with the list (and with the codes field, which adds how to separate them)."""
        self.help = help_text
        self.setAccessibleDescription(help_text)
        self.list.setAccessibleDescription(help_text)
        self.codes.setAccessibleDescription(f"{help_text} {COMMAS}".strip())

    def focus_widget(self) -> QWidget:
        return self.codes if self.typed else self.list

    def set_options(self, options: list[tuple[str, str]] | None, selected: list[str],
                    missing: str = "not in FOLIO's list") -> None:
        chosen = list(dict.fromkeys(selected or []))
        self.typed = options is None
        self.setFocusProxy(self.codes if self.typed else self.list)     # a caption's mnemonic and a problem land here
        self.codes.setVisible(self.typed)
        self.commas.setVisible(self.typed)
        self.list.setVisible(not self.typed)
        self.filter.setVisible(not self.typed and len(options or []) + len(chosen) > 12)
        self.count.setVisible(not self.typed)
        if self.typed:
            self.codes.setText(", ".join(chosen))
            return
        everything = list(options or [])
        known = {value for value, _label in everything}
        everything += [(v, f"{v} ({missing})") for v in chosen if v not in known]
        self.list.blockSignals(True)
        self.list.clear()
        for value, label in everything:
            item = QListWidgetItem(label)
            item.setData(Qt.ItemDataRole.UserRole, value)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if value in chosen else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        self.list.blockSignals(False)
        self.list.setMinimumHeight(min(220, 24 + 22 * max(1, len(everything))))
        self._count()

    def value(self) -> list[str]:
        if self.typed:
            return [c.strip() for c in self.codes.text().split(",") if c.strip()]
        return [self.list.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]

    def set_checked(self, values: list[str]) -> None:
        """Tick exactly ``values`` (tests and a re-applied form use this)."""
        if self.typed:
            self.codes.setText(", ".join(values))
            return
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setCheckState(Qt.CheckState.Checked if item.data(Qt.ItemDataRole.UserRole) in values
                               else Qt.CheckState.Unchecked)

    def _filter(self, text: str) -> None:
        q = text.strip().lower()
        for i in range(self.list.count()):
            item = self.list.item(i)
            item.setHidden(bool(q) and q not in f"{item.data(Qt.ItemDataRole.UserRole)} {item.text()}".lower())

    def _count(self, *_args) -> None:
        n = len(self.value())
        self.count.setText(f"{n} chosen" if n else "None chosen")
        self.changed.emit()


def location_options(choices: dict | None) -> list[tuple[str, str]] | None:
    if not choices:
        return None
    return [(loc["code"], f"{loc['code']} – {loc['name']}" if loc.get("name") else loc["code"])
            for loc in choices.get("locations") or []]


def status_options(choices: dict | None) -> list[tuple[str, str]] | None:
    return [(s, s) for s in choices.get("statuses") or []] if choices else None


class SchedulePicker(QWidget):
    """When a feed runs: the page's four common schedules, or another one typed in."""

    def __init__(self, prefix: str) -> None:
        super().__init__()
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        self.group = QButtonGroup(self)
        self.radios: dict[str, QRadioButton] = {}
        for value, words in model.SCHEDULES:
            r = QRadioButton(words)
            r.setAccessibleName(words)
            self.group.addButton(r)
            self.radios[value] = r
            box.addWidget(r)
        self.other = QRadioButton("Another time")
        self.other.setAccessibleName("Another time")
        self.group.addButton(self.other)
        box.addWidget(self.other)
        row = QHBoxLayout()
        row.setContentsMargins(24, 0, 0, 0)
        self.custom = edit(f"{prefix}: another schedule", placeholder="e.g. Mon,Thu 06:00")
        self.custom.setAccessibleDescription(SCHEDULE_HELP)
        self.custom.textEdited.connect(lambda _t: self.other.setChecked(True))
        row.addWidget(self.custom)
        box.addLayout(row)
        box.addWidget(note(SCHEDULE_HELP, "About another schedule"))

    def set_value(self, text: str | None) -> None:
        preset = model.SCHEDULES[0][0] if text is None else model.schedule_preset(text)
        if preset:
            self.radios[preset].setChecked(True)
            self.custom.clear()
        else:
            self.other.setChecked(True)
            self.custom.setText(text or "")
        self.setFocusProxy(self.radios[preset] if preset else self.custom)

    def value(self) -> str:
        for v, r in self.radios.items():
            if r.isChecked():
                return v
        return self.custom.text().strip()


class LegacyFields(QWidget):
    """The item note types that hold loan counts and dates from an older system, or None."""

    def __init__(self, prefix: str) -> None:
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        self.rows = Rows(form)
        form.addRow(note("If loan counts and dates from your old system were kept in item notes when you moved to "
                         "FOLIO, choose the note types that hold them, so the vendor gets the whole history. "
                         "Otherwise leave these as None.", "Items migrated from an older system"))
        self.boxes: dict[str, QComboBox] = {}
        for key in model.LEGACY_KEYS:
            box = QComboBox()
            box.setAccessibleName(f"{prefix}: {model.setting_name(key)}")
            self.boxes[key] = box
            self.rows.add(f"{model.setting_name(key)}:", box, model.LEGACY_HELP[key])

    def set_values(self, legacy: dict, types: list[str] | None) -> None:
        for key, box in self.boxes.items():
            current = (legacy or {}).get(key) or ""
            box.clear()
            box.setEditable(types is None)          # FOLIO's list could not be read: type the name
            if box.isEditable():
                box.lineEdit().setAccessibleName(box.accessibleName())
            box.addItem("None", "")
            for t in list(types or []) + ([current] if current and current not in (types or []) else []):
                box.addItem(t, t)
            at = box.findData(current)
            if at >= 0:
                box.setCurrentIndex(at)
            elif types is None:
                box.setEditText(current)

    def value(self) -> dict[str, str | None]:
        out = {}
        for key, box in self.boxes.items():
            text = box.currentText().strip() if box.isEditable() else str(box.currentData() or "")
            out[key] = None if text in ("", "None") else text
        return out
