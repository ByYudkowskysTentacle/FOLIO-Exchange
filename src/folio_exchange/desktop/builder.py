"""Building a custom recipe: the files a vendor asks for, their columns and formats, which records go out, the
checks before sending and the schedule new feeds start with. It starts blank, from a copy of an installed recipe,
or from a custom recipe built here before, which is then changed in place.

The dialog only gathers a draft. The window has the core check it (``recipeops.review``, off its thread) and shows
the answer here for a last look: the recipe's problems in the core's words, or the files it writes, what it reads
from FOLIO and what changes for the feeds that use it. Saving is a job (``recipeops.save_custom``); the core
refuses a recipe that would stop a feed loading, and a file that changed underneath the form unless the person
chooses to replace it. Nothing typed is lost on a refusal. Every field a column can hold comes from the field
registry, and the core works out what the recipe reads from FOLIO, so a recipe built here can read no more than
its columns need and never patron data.
"""
from __future__ import annotations

import re

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from .. import setupops
from . import model, theme
from .forms import (
    COMMAS,
    PickList,
    Rows,
    SchedulePicker,
    announce,
    edit,
    location_options,
    note,
    problem_label,
    tell,
)
from .pages import _esc, actions_row, button, fill_table, heading, table

ID_HELP = "Use lowercase letters, digits and hyphens, such as acme-items."
FILE_NAME_HELP = ("{prefix} is the file name prefix and {date} is the run's date as YYYYMMDD. The extension is "
                  "added.")
DATE_HELP = "How dates are written. In a pattern, %Y is the year, %m the month and %d the day."
SEPARATORS = (("Comma", ","), ("Pipe |", "|"), ("Tab", "\t"), ("Semicolon", ";"))
DATES = (("2026-03-15 (%Y-%m-%d)", "%Y-%m-%d"), ("2026-03-15 13:45:30 (%Y-%m-%d %H:%M:%S)", "%Y-%m-%d %H:%M:%S"),
         ("03/15/2026 (%m/%d/%Y)", "%m/%d/%Y"), ("15/03/2026 (%d/%m/%Y)", "%d/%m/%Y"), ("20260315 (%Y%m%d)", "%Y%m%d"))
LINK_WORDS = {"999 ff $i": "Instance UUID, in MARC 999 ff $i", "001": "Instance HRID, in MARC 001"}
LAST_CIRC = (("The last checkout", "checkout"), ("The latest loan activity", "any"))
UPLOADS = (("Under a temporary name, then renamed", "staged"), ("Under the final name", "direct"))
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
TABS = ("About", "Files", "Records", "Checks and schedule")


def slug(words: str) -> str:
    """A recipe id made from a title or a vendor's name."""
    return re.sub(r"[^a-z0-9]+", "-", words.lower()).strip("-")[:40].strip("-")


def header_for(field: str) -> str:
    """The header a new column starts with: the field's name in words ("loans_total" becomes "Loans total")."""
    words = field.replace("_", " ")
    return words[:1].upper() + words[1:]


def scrolled(inner: QWidget) -> QScrollArea:
    area = QScrollArea()
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setWidget(inner)
    return area


def wrapping(form: QFormLayout) -> QFormLayout:
    """A form whose long rows put the field under its caption, so a tab never needs to scroll sideways."""
    form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
    return form


def check_box(words: str) -> QCheckBox:
    box = QCheckBox(words)
    box.setAccessibleName(words)
    return box


def combo(name: str, options) -> QComboBox:
    box = QComboBox()
    box.setAccessibleName(name)
    # As wide as it needs when there is room, and narrower when there is not, so a tab never scrolls sideways.
    box.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
    box.setMinimumContentsLength(14)
    box.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
    for words, value in options:
        box.addItem(words, value)
    return box


def spin(name: str, low: int, high: int, step: int = 1) -> QSpinBox:
    box = QSpinBox()
    box.setAccessibleName(name)
    box.setRange(low, high)
    box.setSingleStep(step)
    box.setMaximumWidth(140)
    return box


def choose(box: QComboBox, value: object) -> None:
    at = box.findData(value)
    if at >= 0:
        box.setCurrentIndex(at)


ENTER = (Qt.Key.Key_Return, Qt.Key.Key_Enter)


class EnterEdit(QLineEdit):
    """A field whose Enter key does something of its own. In a dialog, Enter in a plain field also presses the
    default button, which here would start the review while a column or subfield is being added."""

    entered = Signal()

    def __init__(self, name: str, placeholder: str = "") -> None:
        super().__init__()
        self.setAccessibleName(name)
        self.setPlaceholderText(placeholder)

    def keyPressEvent(self, event) -> None:  # noqa: N802 - Qt's naming
        if event.key() in ENTER:
            self.entered.emit()
            event.accept()
            return
        super().keyPressEvent(event)


class FieldList(QListWidget):
    """The fields to choose from. Enter adds the chosen one, and goes no further (see EnterEdit)."""

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() in ENTER and self.currentItem() is not None:
            self.itemActivated.emit(self.currentItem())
            event.accept()
            return
        super().keyPressEvent(event)


class ColumnTable(QTableWidget):
    """A file's columns. Enter on a row starts typing over its header, and goes no further (see EnterEdit)."""

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if (event.key() in ENTER and self.state() != QAbstractItemView.State.EditingState
                and self.item(self.currentRow(), 0) is not None):
            self.editItem(self.item(self.currentRow(), 0))
            event.accept()
            return
        super().keyPressEvent(event)


class ChoiceOrOther(QWidget):
    """Common values to choose from, and a field for any other value."""

    changed = Signal()

    def __init__(self, name: str, options, other: str, placeholder: str = "") -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.combo = combo(name, options)
        self.combo.addItem(other, None)
        self.box = edit(f"{name}: {other[:1].lower()}{other[1:]}", placeholder=placeholder)
        self.box.setMaximumWidth(200)
        row.addWidget(self.combo, 2)
        row.addWidget(self.box, 1)
        self.setFocusProxy(self.combo)
        self.combo.currentIndexChanged.connect(self._sync)
        self.box.textEdited.connect(lambda _t: self.changed.emit())
        self._sync()

    def _sync(self, *_args) -> None:
        self.box.setVisible(self.combo.currentData() is None)
        self.changed.emit()

    def set_value(self, value: object) -> None:
        at = self.combo.findData(value) if isinstance(value, str) else -1
        if at < 0:
            self.combo.setCurrentIndex(self.combo.count() - 1)
            self.box.setText("" if value is None else str(value))
        else:
            self.combo.setCurrentIndex(at)
            self.box.clear()
        self._sync()

    def value(self) -> str:
        chosen = self.combo.currentData()
        return self.box.text() if chosen is None else chosen


class DelimitedEditor(QWidget):
    """One delimited file: its name and format, and its columns, each a field from the registry under a header."""

    changed = Signal()

    def __init__(self, fields: list[dict]) -> None:
        super().__init__()
        self.about = {f["name"]: f.get("description") or "" for f in fields}
        self.loading = False
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        form = wrapping(QFormLayout())
        self.rows = Rows(form)
        self.name = edit("File's short name")
        self.file_name = edit("File name")
        self.extension = edit("File extension")
        self.extension.setMaximumWidth(120)
        self.separator = ChoiceOrOther("Separator", SEPARATORS, "Another character")
        self.header = check_box("Write a header row")
        self.empty = edit("Text for empty values")
        self.empty.setMaximumWidth(200)
        self.dates = ChoiceOrOther("Date format", DATES, "Another pattern", "%Y-%m-%d")
        self.rows.add("Short name:", self.name, "What this file is called in the recipe, such as items.")
        self.rows.add("File name:", self.file_name, FILE_NAME_HELP)
        box.addLayout(form)
        format_form = wrapping(QFormLayout())
        self.format_rows = Rows(format_form)
        self.format_rows.add("Extension:", self.extension, "Such as csv, txt or psv.")
        self.format_rows.add("Separator:", self.separator, focus=self.separator.combo)
        format_form.addRow(self.header)
        self.format_rows.add("Empty values:", self.empty, "What to write when a value is empty. Leave it empty to "
                             "write nothing.")
        self.format_rows.add("Dates:", self.dates, DATE_HELP, focus=self.dates.combo)
        box.addWidget(heading("Columns", 1.0))
        box.addWidget(note("Choose a field and add it as a column. Columns are written in the order shown.",
                           "About columns"))
        both = QHBoxLayout()
        left = QVBoxLayout()
        self.filter = EnterEdit("Filter fields", "Type to filter the fields")
        self.filter.setAccessibleDescription("Press Enter to move to the first field shown.")
        self.filter.textChanged.connect(self._filter)
        self.filter.entered.connect(self._to_first_shown)
        self.available = FieldList()
        self.available.setAccessibleName("Fields you can add")
        self.available.setAccessibleDescription("Press Enter to add the field as a column.")
        self.available.setWordWrap(True)                # what a field holds stays readable in a narrow list
        self.available.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        for f in fields:
            item = QListWidgetItem(f"{f['name']} – {f.get('description') or ''}")
            item.setData(Qt.ItemDataRole.UserRole, f["name"])
            self.available.addItem(item)
        self.available.itemActivated.connect(lambda _item: self.add_column())
        self.add_button = button("Add column", "Add column for the chosen field")
        self.add_button.clicked.connect(lambda: self.add_column())
        left.addWidget(self.filter)
        left.addWidget(self.available, 1)
        right = QVBoxLayout()
        self.columns = ColumnTable(0, 3)
        self.columns.setAccessibleName("Columns of this file")
        self.columns.setAccessibleDescription("Press Enter to change a column's header. Check Required for a column "
                                              "that feeds must keep.")
        self.columns.setHorizontalHeaderLabels(["Header", "Field", "Required"])
        self.columns.horizontalHeaderItem(2).setToolTip("Feeds that use the recipe can't remove a required column.")
        self.columns.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.columns.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.columns.setTabKeyNavigation(False)
        self.columns.verticalHeader().hide()
        self.columns.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.columns.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.columns.itemChanged.connect(self._edited)
        self.up_button = button("Move up", "Move up the chosen column")
        self.down_button = button("Move down", "Move down the chosen column")
        self.remove_button = button("Remove column", "Remove column from this file")
        self.up_button.clicked.connect(lambda: self.move_column(-1))
        self.down_button.clicked.connect(lambda: self.move_column(1))
        self.remove_button.clicked.connect(self.remove_column)
        right.addWidget(self.columns, 1)
        both.addLayout(left, 2)
        both.addLayout(right, 3)
        box.addLayout(both, 1)
        # One row under both, so neither the list nor the table is held wider than its share by its buttons.
        box.addLayout(actions_row(self.add_button, self.up_button, self.down_button, self.remove_button))
        self.available.setMinimumHeight(220)
        box.addWidget(heading("Format", 1.0))
        box.addLayout(format_form)
        for w in (self.name, self.file_name, self.extension, self.empty):
            w.textEdited.connect(lambda _t: self.changed.emit())
        self.header.toggled.connect(lambda _on: self.changed.emit())
        self.separator.changed.connect(self.changed.emit)
        self.dates.changed.connect(self.changed.emit)

    def _filter(self, text_: str) -> None:
        q = text_.strip().lower()
        for i in range(self.available.count()):
            item = self.available.item(i)
            item.setHidden(bool(q) and q not in item.text().lower())

    def _to_first_shown(self) -> None:
        """Enter in the filter: on to the first field it left in the list, where Enter adds it."""
        for i in range(self.available.count()):
            if not self.available.item(i).isHidden():
                self.available.setCurrentRow(i)
                self.available.setFocus(Qt.FocusReason.OtherFocusReason)
                return

    def _edited(self, _item: QTableWidgetItem) -> None:
        if not self.loading:
            self.changed.emit()

    # ------------------------------------------------------------ columns
    def cols(self) -> list[dict]:
        out = []
        for r in range(self.columns.rowCount()):
            col = {"header": self.columns.item(r, 0).text().strip(),
                   "field": self.columns.item(r, 1).data(Qt.ItemDataRole.UserRole)}
            if self.columns.item(r, 2).checkState() == Qt.CheckState.Checked:
                col["required"] = True
            out.append(col)
        return out

    def set_cols(self, cols: list[dict], select: int | None = None) -> None:
        self.loading = True
        self.columns.setRowCount(0)
        for col in cols:
            r = self.columns.rowCount()
            self.columns.insertRow(r)
            field = str(col.get("field") or "")
            head = QTableWidgetItem(str(col.get("header") or ""))
            name = QTableWidgetItem(field)
            name.setData(Qt.ItemDataRole.UserRole, field)
            name.setToolTip(self.about.get(field, "Not a field FOLIO-Exchange knows"))    # plain text: no markup
            keep = QTableWidgetItem()                # a check box under the Required header
            for item in (name, keep):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            keep.setFlags(keep.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            keep.setCheckState(Qt.CheckState.Checked if col.get("required") else Qt.CheckState.Unchecked)
            for c, item in enumerate((head, name, keep)):
                self.columns.setItem(r, c, item)
        self.loading = False
        if select is not None and 0 <= select < self.columns.rowCount():
            self.columns.selectRow(select)

    def add_column(self, field: str | None = None, header: str | None = None) -> None:
        if field is None:
            item = self.available.currentItem()
            if item is None or item.isHidden():
                return
            field = item.data(Qt.ItemDataRole.UserRole)
        cols = self.cols()
        taken = {c["header"] for c in cols}
        base = header or header_for(field)
        name = next(n for n in (base, *(f"{base} {i}" for i in range(2, 1000))) if n not in taken)
        cols.append({"header": name, "field": field})
        self.set_cols(cols, len(cols) - 1)
        self.changed.emit()

    def move_column(self, by: int) -> None:
        cols, at = self.cols(), self.columns.currentRow()
        to = at + by
        if at < 0 or not 0 <= to < len(cols):
            return
        cols[at], cols[to] = cols[to], cols[at]
        self.set_cols(cols, to)
        self.changed.emit()

    def remove_column(self) -> None:
        cols, at = self.cols(), self.columns.currentRow()
        if at < 0:
            return
        del cols[at]
        self.set_cols(cols, min(at, len(cols) - 1))
        self.changed.emit()

    # ------------------------------------------------------------ the file
    def load(self, out: dict) -> None:
        self.rows.clear()
        self.format_rows.clear()
        self.name.setText(str(out.get("name") or ""))
        self.file_name.setText(str(out.get("file_name") or ""))
        self.extension.setText(str(out.get("extension") or ""))
        self.separator.set_value(out.get("delimiter"))
        self.header.setChecked(out.get("header") is not False)
        self.empty.setText(str(out.get("empty_value") or ""))
        self.dates.set_value(out.get("date_format"))
        self.filter.clear()
        self.set_cols([c for c in out.get("columns") or [] if isinstance(c, dict)])

    def value(self) -> dict:
        return {"name": self.name.text().strip(), "format": "delimited", "file_name": self.file_name.text().strip(),
                "extension": self.extension.text().strip(), "delimiter": self.separator.value(),
                "header": self.header.isChecked(), "empty_value": self.empty.text(),
                "date_format": self.dates.value(), "columns": self.cols()}

    def check(self) -> bool:
        """Show the first problem that can be told without the core, beside its field. True when there is none."""
        self.rows.clear()
        self.format_rows.clear()
        if not NAME_RE.match(self.name.text().strip()):
            self.rows.fail(self.name, "Use lowercase letters, digits, hyphens or underscores, such as items.")
        elif not self.file_name.text().strip():
            self.rows.fail(self.file_name, "Enter a file name, such as {prefix}_items_{date}.")
        elif len(self.separator.value()) != 1:
            self.format_rows.fail(self.separator.combo, "Enter one character to put between values.")
        elif not self.extension.text().strip().isalnum():
            self.format_rows.fail(self.extension, "Enter an extension of letters and digits, such as csv.")
        else:
            return True
        return False


class MarcEditor(QWidget):
    """The MARC file: one record per instance, with or without each item written into its bib record."""

    changed = Signal()

    def __init__(self, fields: list[dict]) -> None:
        super().__init__()
        self.about = {f["name"]: f.get("description") or "" for f in fields}
        self.subs: dict[str, str] = {}
        box = QVBoxLayout(self)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(note("One record per instance, from FOLIO's MARC records. Field 001 holds the instance HRID "
                           "and 999 ff $i the instance UUID.", "About the MARC file"))
        form = wrapping(QFormLayout())
        self.rows = Rows(form)
        self.name = edit("File's short name")
        self.file_name = edit("File name")
        self.split = spin("Records per file", 0, 10_000_000, 1000)
        self.rows.add("Short name:", self.name, "What this file is called in the recipe, such as bibs.")
        self.rows.add("File name:", self.file_name, FILE_NAME_HELP.replace("The extension", ".mrc"))
        self.rows.add("Records per file:", self.split, "Starts a new file after this many records. 0 writes one "
                      "file.")
        box.addLayout(form)
        box.addWidget(heading("Items in the bib records", 1.0))
        item_form = wrapping(QFormLayout())
        self.item_rows = Rows(item_form)
        self.tag = edit("MARC field for items")
        self.tag.setMaximumWidth(80)
        self.tag.setMaxLength(3)
        self.strip = check_box("Remove this field from FOLIO's records before writing")
        self.embed = check_box("Write each item into its bib record")
        self.most = spin("Most items per record", 1, 9999)
        self.dates = ChoiceOrOther("Date format in item subfields", DATES, "Another pattern", "%Y-%m-%d")
        self.item_rows.add("MARC field:", self.tag, "A field tag from 010 to 999, such as 949.")
        item_form.addRow(self.strip)
        item_form.addRow(self.embed)
        self.item_rows.add("Most items per record:", self.most)
        self.item_rows.add("Dates:", self.dates, DATE_HELP, focus=self.dates.combo)
        box.addLayout(item_form)
        self.table = table(["Subfield", "Field", "What it holds"], "Subfields of the item field")
        self.table.setMinimumHeight(140)
        box.addWidget(self.table)
        self.code = EnterEdit("Subfield code", "a")
        self.code.entered.connect(self.add_subfield)
        self.code.setMaxLength(1)
        self.code.setMaximumWidth(60)
        self.field = combo("Field for the subfield", [(f"{f['name']} – {f.get('description') or ''}", f["name"])
                                                      for f in fields])
        self.add_button = button("Add subfield", "Add subfield to the item field")
        self.remove_button = button("Remove subfield", "Remove subfield from the item field")
        self.add_button.clicked.connect(self.add_subfield)
        self.remove_button.clicked.connect(self.remove_subfield)
        row = QHBoxLayout()
        for w in (self.code, self.field, self.add_button, self.remove_button):
            row.addWidget(w)
        row.addStretch(1)
        box.addLayout(row)
        self.sub_problem = problem_label()
        box.addWidget(self.sub_problem)
        box.addStretch(1)
        self.embed.toggled.connect(lambda _on: self.changed.emit())
        self.name.textEdited.connect(lambda _t: self.changed.emit())

    def _fill(self) -> None:
        codes = list(self.subs)
        fill_table(self.table, [[f"${c}", self.subs[c], self.about.get(self.subs[c], "")] for c in codes], codes)

    def add_subfield(self) -> None:
        code = self.code.text().strip().lower()
        self.sub_problem.hide()
        if not re.fullmatch(r"[a-z0-9]", code):
            tell(self.sub_problem, "Enter one letter or digit for the subfield code.")
            self.code.setFocus(Qt.FocusReason.OtherFocusReason)
            return
        self.subs[code] = self.field.currentData()
        self.code.clear()
        self._fill()
        self.changed.emit()

    def remove_subfield(self) -> None:
        at = self.table.currentRow()
        if at >= 0:
            del self.subs[list(self.subs)[at]]
            self._fill()
            self.changed.emit()

    def load(self, out: dict) -> None:
        self.rows.clear()
        self.item_rows.clear()
        self.sub_problem.hide()
        itf = out.get("item_field") if isinstance(out.get("item_field"), dict) else {}
        self.name.setText(str(out.get("name") or ""))
        self.file_name.setText(str(out.get("file_name") or ""))
        self.split.setValue(int(out.get("split_every") or 0))
        self.tag.setText(str(itf.get("tag") or "949"))
        self.strip.setChecked(itf.get("strip_existing") is not False)
        self.embed.setChecked(bool(out.get("embed_items")))
        self.most.setValue(int(itf.get("max_per_record") or 500))
        self.dates.set_value(out.get("date_format"))
        subs = itf.get("subfields") if isinstance(itf.get("subfields"), dict) else {}
        self.subs = {str(k): str(v) for k, v in subs.items()}
        self._fill()

    def value(self) -> dict:
        return {"name": self.name.text().strip(), "format": "marc21", "file_name": self.file_name.text().strip(),
                "split_every": self.split.value(), "embed_items": self.embed.isChecked(),
                "date_format": self.dates.value(),
                "item_field": {"tag": self.tag.text().strip(), "strip_existing": self.strip.isChecked(),
                               "max_per_record": self.most.value(), "subfields": dict(self.subs)}}

    def check(self) -> bool:
        self.rows.clear()
        self.item_rows.clear()
        self.sub_problem.hide()
        tag = self.tag.text().strip()
        if not NAME_RE.match(self.name.text().strip()):
            self.rows.fail(self.name, "Use lowercase letters, digits, hyphens or underscores, such as bibs.")
        elif not self.file_name.text().strip():
            self.rows.fail(self.file_name, "Enter a file name, such as {prefix}_bibs_{date}.")
        elif not (re.fullmatch(r"\d{3}", tag) and 10 <= int(tag) <= 999):
            self.item_rows.fail(self.tag, "Enter a field tag from 010 to 999.")
        elif self.embed.isChecked() and not self.subs:
            tell(self.sub_problem, "Add at least one subfield, or don't write items into the bib records.")
        else:
            return True
        return False


class RecipeBuilder(QDialog):
    review = Signal(dict)             # the draft, for the core to check and describe
    save = Signal(dict, str)          # the reviewed draft, and its file's version ("": replace whatever is there)

    def __init__(self, form: dict, choices: dict | None = None, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.editing: str | None = form.get("editing")
        self.copied_from: str | None = form.get("copied_from")
        self.version = str(form.get("version") or "")
        self.taken = set(form.get("taken") or [])
        self.fields = list(form.get("fields") or [])
        self.statuses = list(form.get("statuses") or [])
        self.links = list(form.get("links") or [])
        self.used_by = list(form.get("feeds") or [])
        self.busy = False
        self.sent: dict = {}
        self.outputs: list[dict] = []
        self.current = -1
        self.showing = False              # a file is being put into its editor: nothing it signals is a change
        self.link_wanted: str | None = None
        self.link_value: str | None = None
        self.id_typed = bool(self.editing or self.copied_from)
        self.setWindowTitle(f"FOLIO-Exchange – change the recipe {self.editing}" if self.editing else
                            "FOLIO-Exchange – build a custom recipe")
        self.resize(1120, 700)
        outer = QVBoxLayout(self)
        self.pages = QStackedWidget()
        outer.addWidget(self.pages)
        self.pages.addWidget(self._form_page())
        self.pages.addWidget(self._review_page())
        self.set_choices(choices)
        self.load(form.get("draft") or {})

    # ------------------------------------------------------------ the form
    def _form_page(self) -> QWidget:
        w = QWidget()
        box = QVBoxLayout(w)
        box.addWidget(heading("Change a custom recipe" if self.editing else "Build a custom recipe", 1.3))
        if self.editing and self.used_by:
            words = (f"Used by {', '.join(self.used_by)}. If you change what the files look like, the next delivery "
                     "is held for your review.")
        elif self.editing:
            words = "No feed uses this recipe yet."
        elif self.copied_from:
            words = f"This starts as a copy of {self.copied_from}. Saving adds it as a recipe of its own."
        else:
            words = ("A recipe is the file layout a vendor asks for. Build one when the vendor hasn't published "
                     "a recipe.")
        box.addWidget(note(words, "About this recipe"))
        self.lists_note = note(about="FOLIO's lists")
        self.lists_note.hide()
        box.addWidget(self.lists_note)
        self.tabs = QTabWidget()
        self.tabs.setAccessibleName("Recipe sections")
        for build, title in zip((self._about_tab, self._files_tab, self._records_tab, self._checks_tab), TABS,
                                strict=True):
            self.tabs.addTab(scrolled(build()), title)
        self.tabs.currentChanged.connect(self._tab_changed)
        box.addWidget(self.tabs, 1)
        self.problem = problem_label()
        box.addWidget(self.problem)
        self.state = note(about="What is happening")
        self.state.hide()
        box.addWidget(self.state)
        self.review_button = theme.primary(button("Re&view recipe", "Review recipe before saving"))
        self.review_button.setDefault(True)
        self.review_button.clicked.connect(self.ask_review)
        self.cancel_button = button("Cancel", "Cancel without saving")
        self.cancel_button.clicked.connect(self.reject)
        box.addLayout(actions_row(self.review_button, self.cancel_button))
        return w

    def _about_tab(self) -> QWidget:
        w = QWidget()
        form = wrapping(QFormLayout(w))
        self.about_rows = Rows(form)
        self.vendor = edit("Vendor")
        self.title_edit = edit("Title")
        self.title_edit.setMaxLength(200)
        self.rid = edit("Recipe id")
        self.rid.setMaxLength(60)
        self.rid.setReadOnly(bool(self.editing))
        self.description = QPlainTextEdit()
        self.description.setAccessibleName("Description")
        self.description.setTabChangesFocus(True)
        self.description.setMaximumHeight(90)
        self.source = edit("Where the layout comes from")
        self.prefix = edit("File name prefix")
        self.about_rows.add("Vendor:", self.vendor, "Who gets the files, such as collectionHQ.")
        self.about_rows.add("Title:", self.title_edit, "How the recipe is listed, such as Acme Analytics – items.")
        self.about_rows.add("Recipe id:", self.rid, "The id can't change once the recipe is saved." if self.editing
                            else f"A short name for the recipe. {ID_HELP}")
        self.about_rows.add("Description:", self.description, "Optional. What the vendor gets, in a sentence or two.")
        self.about_rows.add("Layout source:", self.source, "Optional. Where the layout comes from, such as the "
                            "vendor's file specification and its date.")
        self.about_rows.add("File name prefix:", self.prefix, "The start of each file's name. Leave it empty to use "
                            "the recipe id.")
        self.title_edit.textEdited.connect(self._suggest_id)
        self.vendor.textEdited.connect(self._suggest_id)
        self.rid.textEdited.connect(self._id_typed)
        return w

    def _id_typed(self, _text: str) -> None:
        self.id_typed = True

    def _suggest_id(self, _text: str = "") -> None:
        if not self.id_typed:
            self.rid.setText(slug(self.title_edit.text()) or slug(self.vendor.text()))

    def _files_tab(self) -> QWidget:
        w = QWidget()
        across = QHBoxLayout(w)
        side = QWidget()
        side.setMaximumWidth(260)
        box = QVBoxLayout(side)
        box.setContentsMargins(0, 0, 0, 0)
        box.addWidget(note("Each run writes these files. A recipe can have one MARC file and any number of delimited "
                           "files.", "About files"))
        self.files = QListWidget()
        self.files.setAccessibleName("Files this recipe writes")
        self.files.setMaximumHeight(96)
        self.files.currentRowChanged.connect(self._file_chosen)
        box.addWidget(self.files)
        self.add_delimited = button("Add a delimited file", "Add a delimited file")
        self.add_marc = button("Add a MARC file", "Add a MARC file")
        self.remove_file = button("Remove this file", "Remove this file from the recipe")
        self.add_delimited.clicked.connect(lambda: self.add_file("delimited"))
        self.add_marc.clicked.connect(lambda: self.add_file("marc21"))
        self.remove_file.clicked.connect(self.remove_current_file)
        for b in (self.add_delimited, self.add_marc, self.remove_file):
            box.addWidget(b)
        self.files_problem = problem_label()
        box.addWidget(self.files_problem)
        self.link_box = QWidget()
        link_form = QFormLayout(self.link_box)
        link_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapAllRows)
        link_form.setContentsMargins(0, 0, 0, 0)
        link_form.addRow(heading("How items match bibs", 1.0))
        self.link_rows = Rows(link_form)
        self.link_kind = combo("What links an item to its bib", [(LINK_WORDS.get(k["bib_field"], k["bib_field"]),
                                                                  k["bib_field"]) for k in self.links])
        self.link_column = combo("Item column that holds the link", [])
        self.link_rows.add("Linked by:", self.link_kind, "The vendor matches each item row to its bib record by "
                           "this value.")
        self.link_rows.add("Item column:", self.link_column, "The column of a delimited file that holds the same "
                           "value.")
        self.link_kind.currentIndexChanged.connect(lambda _i: self._fill_link_columns())
        box.addWidget(self.link_box)
        box.addStretch(1)
        self.editors = QStackedWidget()
        self.no_file = note("Add a file to start.", "No file chosen")
        self.no_file.setAlignment(Qt.AlignmentFlag.AlignTop)
        self.delimited = DelimitedEditor(self.fields)
        self.marc = MarcEditor(self.fields)
        for editor in (self.no_file, self.delimited, self.marc):
            self.editors.addWidget(editor)
        self.delimited.changed.connect(self._editor_changed)
        self.marc.changed.connect(self._editor_changed)
        across.addWidget(side)
        across.addWidget(self.editors, 1)
        return w

    def _records_tab(self) -> QWidget:
        w = QWidget()
        form = wrapping(QFormLayout(w))
        self.record_rows = Rows(form)
        form.addRow(note("These are the recipe's defaults. Each feed can change them in its settings.",
                         "About these settings"))
        self.flags: dict[str, QCheckBox] = {}
        for key in model.FILTER_FLAGS:
            self.flags[key] = check_box(model.setting_name(key))
            form.addRow(self.flags[key])
        self.status = PickList("Item statuses to leave out")
        self.loc = PickList("Locations to leave out")
        self.only = PickList("Only these locations")
        for caption, picker, help_text in (
                ("Item statuses to leave out:", self.status, "Items with these statuses aren't sent."),
                ("Locations to leave out:", self.loc, "Items in these locations aren't sent."),
                ("Only these locations:", self.only, "If you choose any, only items in those locations are sent.")):
            self.record_rows.add(caption, picker, help_text)
            picker.set_help(help_text)
        form.addRow(heading("Loans and dates", 1.0))
        self.last_circ = combo("Last loan date comes from", LAST_CIRC)
        self.loan_window = spin("Months of recent loans to count", 1, 600)
        self.zone = edit("Time zone")
        self.record_rows.add("Last loan date:", self.last_circ, "What the last_circ_date field holds.")
        self.record_rows.add("Recent loans:", self.loan_window, "How many months the loans_window field counts.")
        self.record_rows.add("Time zone:", self.zone, "Dates in the files use this time zone, such as "
                             "America/Detroit or UTC.")
        return w

    def _checks_tab(self) -> QWidget:
        w = QWidget()
        form = wrapping(QFormLayout(w))
        self.check_rows = Rows(form)
        form.addRow(heading("Check before sending", 1.0))
        ratio_row = QWidget()
        row = QHBoxLayout(ratio_row)
        row.setContentsMargins(0, 0, 0, 0)
        self.ratio = edit("Block a delivery if the item count falls below this percentage of the last one")
        self.ratio.setMaximumWidth(80)
        unit = QLabel("% of the last one")
        unit.setTextFormat(Qt.TextFormat.PlainText)
        row.addWidget(self.ratio)
        row.addWidget(unit)
        row.addStretch(1)
        self.check_rows.add("Block a delivery if the item count falls below:", ratio_row,
                            "Leave it empty or 0 to turn the check off.", focus=self.ratio)
        self.must = PickList("Fields that must be filled")
        must_help = "A delivery is blocked if any row has one of these fields empty."
        self.check_rows.add("Fields that must be filled:", self.must, must_help)
        self.must.set_help(must_help)
        form.addRow(heading("Delivery and schedule", 1.0))
        self.upload = combo("How files are uploaded", UPLOADS)
        self.check_rows.add("Upload each file:", self.upload, "Choose the final name if the vendor's server doesn't "
                            "allow renaming.")
        self.schedule = SchedulePicker("New feeds")
        self.check_rows.add("New feeds run:", self.schedule, "A feed can set its own schedule.")
        self.check_rows.problems[self.schedule.custom] = self.check_rows.problems.pop(self.schedule)
        self.check_rows.helps[self.schedule.custom] = self.schedule.custom.accessibleDescription()
        return w

    def _review_page(self) -> QWidget:
        w = QWidget()
        box = QVBoxLayout(w)
        self.review_title = heading("Review the recipe", 1.3)
        box.addWidget(self.review_title)
        self.review_text = QLabel()
        self.review_text.setWordWrap(True)
        self.review_text.setTextFormat(Qt.TextFormat.RichText)          # every value escaped
        self.review_text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse
                                                 | Qt.TextInteractionFlag.TextSelectableByKeyboard)
        self.review_text.setAccessibleDescription("What the recipe does")
        self.review_text.setAlignment(Qt.AlignmentFlag.AlignTop)
        box.addWidget(scrolled(self.review_text), 1)
        self.review_note = note(about="After saving")
        box.addWidget(self.review_note)
        self.save_button = theme.primary(button("&Save recipe", "Save recipe"))
        self.save_button.clicked.connect(self._save)
        self.replace_button = button("&Replace the changed file", "Replace the changed file with this recipe")
        self.replace_button.clicked.connect(self._replace)
        self.back_button = button("Back to &editing", "Back to editing")
        self.back_button.clicked.connect(self.back_to_editing)
        self.close_button = button("Close", "Close without saving")
        self.close_button.clicked.connect(self.reject)
        box.addLayout(actions_row(self.save_button, self.replace_button, self.back_button, self.close_button))
        return w

    def set_choices(self, choices: dict | None) -> None:
        """{"data": setupops.tenant_choices} or {"error": ...}: without FOLIO's lists, location codes are typed."""
        self.choices = (choices or {}).get("data")
        error = (choices or {}).get("error")
        self.lists_note.setText(f"Couldn't read FOLIO's locations ({error}). On the Records tab, type locations as "
                                f"codes. {COMMAS}" if error else "")
        self.lists_note.setVisible(bool(error))

    # ------------------------------------------------------------ the draft, in and out
    def load(self, draft: dict) -> None:
        """Show ``draft`` (recipeops.blank_draft, or a recipe as recipeops.draft_of gives it)."""
        self.base = draft
        self.vendor.setText(str(draft.get("vendor") or ""))
        self.title_edit.setText(str(draft.get("title") or ""))
        self.rid.setText(str(draft.get("recipe") or ""))
        self.description.setPlainText(str(draft.get("description") or ""))
        self.source.setText(str(draft.get("spec_source") or ""))
        self.prefix.setText(str(draft.get("file_prefix") or ""))
        sel = draft.get("selection") or {}
        for key, box in self.flags.items():
            box.setChecked(bool(sel.get(key)))
        self.status.set_options([(s, s) for s in self.statuses] or None, sel.get("exclude_item_statuses") or [],
                                model.MISSING_STATUS)
        locations = location_options(self.choices)
        self.loc.set_options(locations, sel.get("exclude_location_codes") or [], model.MISSING_LOCATION)
        self.only.set_options(locations, sel.get("include_location_codes") or [], model.MISSING_LOCATION)
        opts = draft.get("options") or {}
        choose(self.last_circ, opts.get("last_circ_date_from") or "checkout")
        self.loan_window.setValue(int(opts.get("loan_window_months") or 12))
        self.zone.setText(str(opts.get("timezone") or "UTC"))
        checks = draft.get("checks") or {}
        ratio = checks.get("min_item_ratio")
        self.ratio_start = ratio or 0
        self.ratio_text = f"{float(ratio) * 100:.1f}".rstrip("0").rstrip(".") if ratio else ""
        self.ratio.setText(self.ratio_text)
        self.must.set_options([(f["name"], f"{f['name']} – {f.get('description') or ''}") for f in self.fields],
                              list(checks.get("must_be_filled") or []), "not a field FOLIO-Exchange knows")
        delivery = draft.get("delivery") or {}
        choose(self.upload, delivery.get("upload_mode") or "staged")
        self.schedule.set_value(str(draft.get("default_schedule") or "manual"))
        self.outputs = [dict(o) for o in draft.get("outputs") or [] if isinstance(o, dict)]
        link = draft.get("link") or {}
        self.link_wanted = link.get("item_column")
        self.current = -1
        choose(self.link_kind, link.get("bib_field"))
        self._fill_files(0 if self.outputs else -1)

    def _store(self) -> None:
        """Keep what the shown file's editor holds."""
        if 0 <= self.current < len(self.outputs):
            editor = self.editors.currentWidget()
            if editor in (self.delimited, self.marc):
                self.outputs[self.current] = editor.value()

    def _caption(self, out: dict) -> str:
        kind = "MARC records" if out.get("format") == "marc21" else "delimited"
        return f"{out.get('name') or 'unnamed'} – {kind}"

    def _fill_files(self, select: int) -> None:
        self.files.blockSignals(True)
        self.files.clear()
        for out in self.outputs:
            item = QListWidgetItem(self._caption(out))
            item.setData(Qt.ItemDataRole.AccessibleTextRole, self._caption(out))
            self.files.addItem(item)
        self.files.setCurrentRow(select)
        self.files.blockSignals(False)
        self._show_file(select)

    def _show_file(self, row: int) -> None:
        self.current = row if 0 <= row < len(self.outputs) else -1
        if self.current < 0:
            self.editors.setCurrentWidget(self.no_file)
        else:
            out = self.outputs[self.current]
            editor = self.marc if out.get("format") == "marc21" else self.delimited
            self.showing = True
            try:
                editor.load(out)
            finally:
                self.showing = False
            self.editors.setCurrentWidget(editor)
        self.remove_file.setEnabled(self.current >= 0)
        self.add_marc.setEnabled(not any(o.get("format") == "marc21" for o in self.outputs))
        self._fill_link_columns()

    def _file_chosen(self, row: int) -> None:
        self._store()
        self._show_file(row)

    def _editor_changed(self) -> None:
        if self.showing:
            return
        self._store()
        if 0 <= self.current < self.files.count():
            caption = self._caption(self.outputs[self.current])
            self.files.item(self.current).setText(caption)
            self.files.item(self.current).setData(Qt.ItemDataRole.AccessibleTextRole, caption)
        self._fill_link_columns()

    def add_file(self, kind: str) -> None:
        self._store()
        self.files_problem.hide()
        names = {o.get("name") for o in self.outputs}
        base = "bibs" if kind == "marc21" else "items"
        name = next(n for n in (base, *(f"{base}-{i}" for i in range(2, 100))) if n not in names)
        if kind == "marc21":
            out = {"name": name, "format": "marc21", "file_name": f"{{prefix}}_{name}_{{date}}", "split_every": 0,
                   "embed_items": False, "date_format": "%Y-%m-%d",
                   "item_field": {"tag": "949", "strip_existing": True, "max_per_record": 500, "subfields": {}}}
        else:
            out = {"name": name, "format": "delimited", "file_name": f"{{prefix}}_{name}_{{date}}",
                   "extension": "csv", "delimiter": ",", "header": True, "empty_value": "",
                   "date_format": "%Y-%m-%d", "columns": []}
        self.outputs.append(out)
        self._fill_files(len(self.outputs) - 1)
        self.editors.currentWidget().name.setFocus(Qt.FocusReason.OtherFocusReason)

    def remove_current_file(self) -> None:
        if not 0 <= self.current < len(self.outputs):
            return
        at = self.current
        del self.outputs[at]
        self.current = -1                               # nothing of the removed file is stored again
        self._fill_files(min(at, len(self.outputs) - 1))
        self.files.setFocus(Qt.FocusReason.OtherFocusReason)

    def _linked(self) -> bool:
        kinds = {o.get("format") for o in self.outputs}
        return {"marc21", "delimited"} <= kinds

    def _fill_link_columns(self) -> None:
        """The columns that can hold the link: those of a delimited file whose field is the link's value."""
        self.link_box.setVisible(self._linked())
        value = next((k["value"] for k in self.links if k["bib_field"] == self.link_kind.currentData()), None)
        keep = self.link_column.currentData() or self.link_wanted
        self.link_column.blockSignals(True)
        self.link_column.clear()
        for out in self.outputs:
            for col in out.get("columns") or [] if out.get("format") == "delimited" else []:
                if col.get("field") == value and self.link_column.findData(col.get("header")) < 0:
                    self.link_column.addItem(f"{col.get('header')} ({out.get('name')})", col.get("header"))
        choose(self.link_column, keep)
        self.link_column.blockSignals(False)
        self.link_value = value
        if self.link_column.count():
            self.link_wanted = self.link_column.currentData()

    def _tab_changed(self, _index: int) -> None:
        self._store()

    def draft(self) -> dict:
        """What the form holds, as recipeops takes it."""
        self._store()
        t = self.ratio.text().strip()
        try:
            ratio = self.ratio_start if t == self.ratio_text else float(t) / 100 if t else 0
        except ValueError:
            ratio = -1
        link = None
        if self._linked():
            link = {"bib_field": self.link_kind.currentData(), "item_column": self.link_column.currentData() or "",
                    "value": self.link_value}
        base_delivery = self.base.get("delivery") or {}
        sel, checks = self.base.get("selection") or {}, self.base.get("checks") or {}

        def kept(start: object, chosen: list[str]) -> list[str]:
            """``chosen`` in the order the recipe had them, new ones last, so an unchanged list is unchanged."""
            was = [v for v in start or [] if v in chosen] if isinstance(start, list) else []
            return was + [v for v in chosen if v not in was]

        return {
            "recipe": self.rid.text().strip(), "vendor": self.vendor.text().strip(),
            "title": self.title_edit.text().strip(), "description": self.description.toPlainText().strip(),
            "spec_source": self.source.text().strip(), "link": link, "file_prefix": self.prefix.text().strip(),
            "selection": {**{k: box.isChecked() for k, box in self.flags.items()},
                          "exclude_item_statuses": kept(sel.get("exclude_item_statuses"), self.status.value()),
                          "include_location_codes": kept(sel.get("include_location_codes"), self.only.value()),
                          "exclude_location_codes": kept(sel.get("exclude_location_codes"), self.loc.value())},
            "options": {"last_circ_date_from": self.last_circ.currentData(), "loan_window_months": self.loan_window.value(),
                        "timezone": self.zone.text().strip() or "UTC"},
            "outputs": [dict(o) for o in self.outputs],
            "checks": {"min_item_ratio": ratio, "must_be_filled": kept(checks.get("must_be_filled"), self.must.value())},
            "delivery": {"transport": base_delivery.get("transport", "sftp"), "upload_mode": self.upload.currentData()},
            "default_schedule": self.schedule.value(),
        }

    # ------------------------------------------------------------ review, save
    def _fail(self, tab: int, rows: Rows, widget: QWidget, message: str) -> None:
        self.tabs.setCurrentIndex(tab)
        rows.fail(widget, message)

    def _file_problem(self, row: int, message: str | None = None) -> None:
        """Show the file with a problem, and say what it is when its editor has not."""
        self.tabs.setCurrentIndex(1)
        self._store()
        self.files.setCurrentRow(row)
        if message:
            tell(self.files_problem, message)
        else:
            self.editors.currentWidget().check()

    def check(self) -> bool:
        """What can be told without the core, said beside its field. True when the draft can go to the core."""
        for rows in (self.about_rows, self.link_rows, self.record_rows, self.check_rows):
            rows.clear()
        self.problem.hide()
        self.files_problem.hide()
        self._store()
        rid = self.rid.text().strip()
        if not self.vendor.text().strip():
            self._fail(0, self.about_rows, self.vendor, "Enter the vendor's name.")
        elif not self.title_edit.text().strip():
            self._fail(0, self.about_rows, self.title_edit, "Enter a title.")
        elif not ID_RE.match(rid):
            self._fail(0, self.about_rows, self.rid, ID_HELP)
        elif not self.editing and rid in self.taken:
            self._fail(0, self.about_rows, self.rid, f"There's already a recipe called {rid}. Choose another id.")
        elif self.prefix.text().strip() and not re.fullmatch(r"[A-Za-z0-9_-]+", self.prefix.text().strip()):
            self._fail(0, self.about_rows, self.prefix, "Use letters, digits, hyphens and underscores.")
        elif not self.outputs:
            self.tabs.setCurrentIndex(1)
            tell(self.files_problem, "Add at least one file.")
        elif not self._files_ok():
            pass
        elif self._linked() and not self.link_column.count():
            self.tabs.setCurrentIndex(1)
            self.link_rows.fail(self.link_column, f"Add a column for the field {self.link_value} to a delimited "
                                "file. The vendor needs it to match items to bibs.")
        elif not self._ratio_ok():
            self._fail(3, self.check_rows, self.ratio, "Enter a percentage from 0 to 100, or leave it empty to turn "
                       "the check off.")
        elif not self.schedule.value() or setupops.schedule_problem(self.schedule.value()):
            self._fail(3, self.check_rows, self.schedule.custom, "Enter a schedule, or choose one above."
                       if not self.schedule.value() else setupops.schedule_problem(self.schedule.value()))
        else:
            return True
        return False

    def _ratio_ok(self) -> bool:
        t = self.ratio.text().strip()
        try:
            return 0 <= (float(t) if t else 0.0) <= 100
        except ValueError:
            return False

    def _files_ok(self) -> bool:
        names: set[str] = set()
        for row, out in enumerate(self.outputs):
            editor = self.marc if out.get("format") == "marc21" else self.delimited
            self.files.setCurrentRow(row)
            if not editor.check():
                self.tabs.setCurrentIndex(1)
                return False
            if out.get("name") in names:
                self._file_problem(row, f"Two files are called {out.get('name')}. Give each file its own short name.")
                return False
            names.add(out.get("name"))
            cols = out.get("columns") or []
            headers = [c.get("header") for c in cols]
            if out.get("format") != "delimited":
                continue
            if not cols:
                self._file_problem(row, f"Add at least one column to {out.get('name')}.")
                return False
            if "" in headers:
                self._file_problem(row, f"Give every column of {out.get('name')} a header.")
                return False
            twice = next((h for h in headers if headers.count(h) > 1), None)
            if twice:
                self._file_problem(row, f"Two columns of {out.get('name')} have the header {twice}. Headers must "
                                   "differ.")
                return False
        return True

    def ask_review(self) -> None:
        if self.busy or not self.check():
            return
        self.sent = self.draft()
        self.working(True, "Checking the recipe…")
        self.review.emit(self.sent)

    def show_review(self, result: dict | None, message: str | None = None) -> None:
        """The core's look at the draft (recipeops.review), or why it could not look."""
        self.working(False)
        if message:
            tell(self.problem, f"Couldn't check the recipe. {message}")
            return
        result = result or {}
        problems = list(result.get("problems") or [])
        changed = bool(result.get("changed"))
        if problems:
            self.review_title.setText("Fix these before saving")
            lines = problems
            after = "Nothing was saved."
        elif not changed:
            self.review_title.setText("Nothing has changed")
            lines = []
            after = ""
        else:
            self.review_title.setText("Review the recipe")
            lines = model.recipe_review_lines(result)
            after = model.recipe_review_note(result)
        self.review_text.setText("<ul>" + "".join(f"<li>{_esc(x)}</li>" for x in lines) + "</ul>" if lines else
                                 "The form matches the saved recipe.")
        self.review_note.setText(after)
        ready = changed and not problems
        self.save_button.setVisible(ready)
        self.replace_button.hide()
        self.close_button.setVisible(not changed and not problems)
        self.pages.setCurrentIndex(1)
        (self.save_button if ready else self.back_button).setFocus(Qt.FocusReason.OtherFocusReason)
        if problems:
            announce(self.review_text)

    def back_to_editing(self) -> None:
        self.pages.setCurrentIndex(0)
        self.review_button.setFocus(Qt.FocusReason.OtherFocusReason)

    def _save(self) -> None:
        if not self.busy:
            self.save.emit(self.sent, self.version)

    def _replace(self) -> None:
        if not self.busy:
            self.save.emit(self.sent, "")

    def working(self, busy: bool, words: str = "Saving…") -> None:
        self.busy = busy
        for b in (self.review_button, self.save_button, self.replace_button, self.back_button):
            b.setEnabled(not busy)
        if busy and self.pages.currentIndex() == 1:
            self.review_note.setText(words)
        self.state.setText(words if busy else "")
        self.state.setVisible(busy)

    def failed(self, message: str) -> None:
        """Not saved (the core refuses it, or another action is running): back to the form, which says why."""
        self.working(False)
        self.back_to_editing()
        tell(self.problem, message if message.startswith("Not saved") else f"Not saved. {message}")

    def conflict(self, message: str) -> None:
        """The file changed underneath the form: nothing was saved, and what was entered is still here."""
        self.working(False)
        self.review_title.setText("Not saved because the file changed")
        self.review_text.setText(_esc(f"{model.first_sentence(message)} What you entered is still here."))
        self.review_note.setText("Replacing the file keeps your version and drops the other change.")
        self.save_button.hide()
        self.close_button.hide()
        self.replace_button.show()
        self.pages.setCurrentIndex(1)
        self.replace_button.setAccessibleDescription(f"{model.first_sentence(message)} Nothing was saved.")
        self.back_button.setFocus(Qt.FocusReason.OtherFocusReason)
        announce(self.review_text)

    def reject(self) -> None:
        if not self.busy:                     # a check or save under way finishes first: its answer comes back here
            super().reject()
