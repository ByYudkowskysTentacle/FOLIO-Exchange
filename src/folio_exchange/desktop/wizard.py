"""Adding a vendor feed, in five steps as on the page: the recipe, a name (with a check that the FOLIO account
can read what the recipe needs), which records to leave out (from the tenant's own locations and FOLIO's item
statuses), the schedule, then a last look before it is added.

The wizard only gathers the answers. The window reads what it needs (the recipes, the account's permissions,
the tenant's lists) off its thread and adds the feed with ``setupops.add_feed`` as a job. A new feed is always
saved paused and with nowhere to send files: a dry run that can send nothing until its delivery is set up.
Everything works from the keyboard: Enter in a field or on a recipe moves on, and Back keeps what was entered.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFormLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .. import setupops
from . import model, theme
from .forms import (
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
from .pages import Facts, Page, _esc, actions_row, button, heading, lines_label, set_lines

STEPS = ("Recipe", "Name", "Records", "Schedule", "Add")


def _with_default(words: str, default: list[str], what: str) -> str:
    """Help for a pick list, naming the recipe's own default when it has one (the page's withDefault)."""
    if not default:
        return words
    return (f"{words} The recipe {what} {', '.join(default)} by default, so "
            f"{'it starts' if len(default) == 1 else 'they start'} checked.")


class OneStep(QWidget):
    """The wizard's steps, one shown at a time. Not a QStackedWidget: a stack is as tall as its tallest page, so the
    long lists of Records would push a short step's fields (the schedule) below the window. Hidden steps here take
    no room."""

    def __init__(self) -> None:
        super().__init__()
        self.box = QVBoxLayout(self)
        self.box.setContentsMargins(0, 0, 0, 0)
        self.pages: list[QWidget] = []
        self.current = 0

    def addWidget(self, page: QWidget) -> None:  # noqa: N802 - as QStackedWidget's
        page.setVisible(not self.pages)
        self.pages.append(page)
        self.box.addWidget(page)

    def setCurrentIndex(self, n: int) -> None:  # noqa: N802
        self.current = n
        for i, page in enumerate(self.pages):
            page.setVisible(i == n)

    def currentIndex(self) -> int:  # noqa: N802
        return self.current

    def currentWidget(self) -> QWidget:  # noqa: N802
        return self.pages[self.current]


class FeedWizard(Page):
    want_recipes = Signal()
    want_permissions = Signal(str)          # the recipe's uses, e.g. chq-academic@^1.0
    want_choices = Signal(bool)             # True: read them again even when read before
    add = Signal()                          # the window takes request() and adds the feed
    get_recipes = Signal()                  # to the Recipes view, to download one
    cancelled = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.taken: list[str] = []
        self.recipes: list[dict] | None = None
        self.choices: dict | None = None    # {"data": ...} or {"error": ...}
        self.reading = False                # the lists are being read again ("Read the lists again")
        self.busy = False
        self.idle = True
        self.box.addWidget(heading("Add a vendor feed"))
        self.notice = note(about="Notice")
        self.notice.hide()
        self.box.addWidget(self.notice)
        self.where = QLabel()
        self.where.setTextFormat(Qt.TextFormat.PlainText)
        self.where.setAccessibleDescription("Step")
        self.box.addWidget(self.where)
        self.steps = OneStep()
        self.steps.setAccessibleName("Steps of adding a feed")
        for build in (self._recipe_step, self._name_step, self._records_step, self._schedule_step, self._add_step):
            step = build()
            step.layout().addStretch(1)             # a short step keeps its rows together at the top
            self.steps.addWidget(step)
        self.box.addWidget(self.steps)
        self.problem = problem_label()
        self.box.addWidget(self.problem)
        self.back_button = button("&Back", "Back to the previous step")
        self.back_button.clicked.connect(self.back)
        self.next_button = theme.primary(button("&Next", "Next step"))
        self.next_button.clicked.connect(self.next)
        self.add_button = theme.primary(button("&Add feed", "Add feed, paused and as a dry run"))
        self.add_button.clicked.connect(self._add)
        self.cancel_button = button("Cancel", "Cancel adding a feed")
        self.cancel_button.clicked.connect(self.cancelled.emit)
        self.box.addLayout(actions_row(self.back_button, self.next_button, self.add_button, self.cancel_button))
        self.reset()

    # ------------------------------------------------------------ the steps
    def _step(self, title: str, intro: str | None) -> tuple[QWidget, QVBoxLayout]:
        w = QWidget()
        box = QVBoxLayout(w)
        box.setContentsMargins(0, 0, 0, 0)
        heading_label = heading(title, 1.15)
        box.addWidget(heading_label)
        if intro:
            box.addWidget(note(intro, "About this step"))
        w.heading = heading_label
        return w, box

    def _recipe_step(self) -> QWidget:
        w, box = self._step("1. Choose the vendor's recipe", "A recipe is the file layout a vendor asks for. Each "
                                                              "feed uses one.")
        self.recipe_state = note(about="Recipes")
        box.addWidget(self.recipe_state)
        self.recipe_list = QListWidget()
        self.recipe_list.setAccessibleName("Installed recipes")
        self.recipe_list.setAccessibleDescription("Press Enter to choose a recipe and continue.")
        self.recipe_list.setWordWrap(True)
        self.recipe_list.itemActivated.connect(lambda _item: self.next())
        box.addWidget(self.recipe_list)
        self.get_button = button("&Get recipes from vendors", "Get recipes from vendors")
        self.get_button.clicked.connect(self.get_recipes.emit)
        box.addLayout(actions_row(self.get_button))
        box.addWidget(note("Not listed? Get recipes from vendors downloads a vendor's recipes into this site.",
                           "Not listed?"))
        return w

    def _name_step(self) -> QWidget:
        w, box = self._step("2. Name the feed", None)
        self.uses_text = note(about="Recipe")
        box.addWidget(self.uses_text)
        form = QFormLayout()
        self.name_rows = Rows(form)
        self.name_edit = edit("Feed name")
        self.name_edit.setMaxLength(60)
        self.title_edit = edit("Title")
        self.title_edit.setMaxLength(200)
        self.name_rows.add("Feed na&me:", self.name_edit, "Lowercase letters, digits and hyphens. It's used for the "
                           "feed's settings file and its run folder.")
        self.name_rows.add("T&itle:", self.title_edit, "How the feed is shown in this app and in alerts.")
        for e in (self.name_edit, self.title_edit):
            e.returnPressed.connect(self.next)
        box.addLayout(form)
        box.addWidget(heading("Can the FOLIO account read what this recipe needs?", 1.0))
        self.perm_state = note(about="Permission check")
        box.addWidget(self.perm_state)
        self.perm_lines = lines_label("What the account can't read")
        box.addWidget(self.perm_lines)
        self.perm_button = button("Check &permissions again", "Check permissions again")
        self.perm_button.clicked.connect(self._check_permissions)
        box.addLayout(actions_row(self.perm_button))
        return w

    def _records_step(self) -> QWidget:
        w, box = self._step("3. Choose which records to send", "Choose what the vendor shouldn't get. You can change "
                                                                "this later on the feed's Settings tab.")
        self.choices_state = note(about="FOLIO's lists")
        box.addWidget(self.choices_state)
        self.choices_button = button("&Read the lists again", "Read the lists again")
        self.choices_button.clicked.connect(self._read_again)
        box.addLayout(actions_row(self.choices_button))
        form = QFormLayout()
        self.record_rows = Rows(form)
        self.loc = PickList("Locations to leave out")
        self.status = PickList("Item statuses to leave out")
        self.only = PickList("Only these locations")
        self.loc_caption = self.record_rows.add("&Locations to leave out:", self.loc)
        self.status_caption = self.record_rows.add("Item s&tatuses to leave out:", self.status)
        self.only_caption = self.record_rows.add("&Only these locations:", self.only)
        box.addLayout(form)
        box.addWidget(heading("Items migrated from an older system", 1.0))
        self.legacy = LegacyFields("New feed")
        box.addWidget(self.legacy)
        return w

    def _schedule_step(self) -> QWidget:
        w, box = self._step("4. Choose when it runs", "The feed starts paused. It runs on this schedule once you "
                                                      "resume it.")
        self.schedule = SchedulePicker("New feed")
        box.addWidget(self.schedule)
        self.schedule_problem = problem_label()
        box.addWidget(self.schedule_problem)
        self.schedule.custom.returnPressed.connect(self.next)
        return w

    def _add_step(self) -> QWidget:
        w, box = self._step("5. Check and add", None)
        self.summary = Facts()
        box.addWidget(self.summary)
        box.addWidget(note("The feed starts paused, as a dry run with no delivery. You can preview and run it right "
                           "away. Nothing is sent to the vendor until you set up its delivery.",
                           "What adding does"))
        return w

    # ------------------------------------------------------------ moving through it
    def reset(self) -> None:
        """A fresh wizard (after a feed was added, or when cancelled)."""
        self.say(None)
        self.W: dict = {"step": 0, "recipe": None, "name": "", "title": "", "perm": None, "legacy": {},
                        "schedule": model.SCHEDULES[0][0], **model.recipe_lists(None)}
        self.busy = False
        self.problem.hide()
        self.recipe_list.clearSelection()
        self._show_step()

    def say(self, message: str | None) -> None:
        """A line above the steps ("Connected to FOLIO. Next, add a vendor feed.")."""
        self.notice.setText(message or "")
        self.notice.setVisible(bool(message))

    def start(self, taken: list[str]) -> None:
        """The wizard is shown: ``taken`` names every feed there is (loaded or not). What was entered stays."""
        self.taken = list(taken)
        if self.recipes is None:
            self.recipe_state.setText("Loading the recipes…")
            self.want_recipes.emit()
        self._show_step()

    def step(self) -> int:
        return self.W["step"]

    def _show_step(self) -> None:
        n = self.W["step"]
        self.steps.setCurrentIndex(n)
        self.verticalScrollBar().setValue(0)            # each step starts at its top
        after = f" Next: {STEPS[n + 1]}." if n + 1 < len(STEPS) else ""
        self.where.setText(f"Step {n + 1} of {len(STEPS)}: {STEPS[n]}.{after}")
        self.back_button.setVisible(n > 0)
        self.next_button.setVisible(n < len(STEPS) - 1)
        self.add_button.setVisible(n == len(STEPS) - 1)
        if n == 1:
            self._show_name()
        elif n == 2:
            self._show_records()
        elif n == 3:
            self.schedule.set_value(self.W["schedule"])
            self.schedule_problem.hide()
            self.schedule.custom.setAccessibleDescription(SCHEDULE_HELP)
        elif n == 4:
            self._show_summary()
        self._enable()
        if self.isVisible():
            self._focus_step()

    def arrive(self) -> None:
        """The window brought the person here itself (after connecting to FOLIO): the focus goes to the step's
        first field, and the notice above the steps is read out."""
        self._focus_step()
        if not self.notice.isHidden():
            announce(self.notice)

    def _focus_step(self) -> None:
        n = self.W["step"]
        first = {0: self.recipe_list, 1: self.name_edit, 2: self.loc, 3: self.schedule, 4: self.add_button}[n]
        first.setFocus(Qt.FocusReason.OtherFocusReason)

    def _enable(self) -> None:
        loading = self.W["step"] == 2 and (self.choices is None or self.reading)
        self.next_button.setEnabled(not self.busy and not loading)
        self.back_button.setEnabled(not self.busy)
        self.add_button.setEnabled(not self.busy and self.idle)
        self.cancel_button.setEnabled(not self.busy)

    def set_idle(self, idle: bool) -> None:
        """Another action runs (here or on the browser page): adding waits for it."""
        self.idle = idle
        self._enable()

    def _keep(self) -> None:
        """Keep what the current step shows, so Back and Next lose nothing."""
        n = self.W["step"]
        if n == 1:
            self.W["name"], self.W["title"] = self.name_edit.text().strip(), self.title_edit.text().strip()
        elif n == 2:
            # Also while FOLIO's lists are first read: the step then shows fields for codes, which can be typed in.
            self.W.update(exclude_location_codes=self.loc.value(), exclude_item_statuses=self.status.value(),
                          include_location_codes=self.only.value(), legacy=self.legacy.value())
        elif n == 3:
            self.W["schedule"] = self.schedule.value()

    def back(self) -> None:
        if self.busy or self.W["step"] == 0:
            return
        self._keep()
        self.problem.hide()
        self.W["step"] -= 1
        self._show_step()

    def next(self) -> None:
        if self.busy or not self.next_button.isEnabled() or self.W["step"] >= len(STEPS) - 1:
            return
        self.problem.hide()
        n = self.W["step"]
        if n == 0:
            item = self.recipe_list.currentItem()
            if item is None:
                self._fail("Choose a recipe first.", self.recipe_list)
                return
            self._choose(item.data(Qt.ItemDataRole.UserRole))
        elif n == 1:
            self._keep()
            self.name_rows.clear()
            bad = model.feed_name_problem(self.W["name"], self.taken)
            if bad:
                self.name_rows.fail(self.name_edit, bad)
                return
        elif n == 2:
            self._keep()
        elif n == 3:
            self._keep()
            bad = ("Enter a schedule, or choose one above." if not self.W["schedule"]
                   else setupops.schedule_problem(self.W["schedule"]))
            if bad:
                # The field may have the focus already (Enter in it), so focusing it reads nothing new: the
                # problem is announced, and read with the field from now on.
                self.schedule_problem.setText(bad)
                self.schedule_problem.show()
                self.schedule.custom.setAccessibleDescription(f"{bad} {SCHEDULE_HELP}")
                announce(self.schedule_problem)
                self.schedule.custom.setFocus()
                return
        self.W["step"] = n + 1
        self._show_step()

    def _fail(self, message: str, focus: QWidget | None = None) -> None:
        """A problem with the step as a whole, announced; ``focus`` takes the focus (else the problem does)."""
        if focus is None:
            tell(self.problem, message)
            return
        self.problem.setText(message)
        self.problem.show()
        focus.setFocus(Qt.FocusReason.OtherFocusReason)
        announce(self.problem)

    # ------------------------------------------------------------ step 1: the recipe
    def show_recipes(self, data: dict | None, error: str | None = None) -> None:
        """views.recipes_overview, or why it could not be read."""
        if error:
            self.recipes = None
            self.recipe_state.setText(f"Couldn't load the recipes: {error}")
            return
        self.recipes = list((data or {}).get("recipes") or [])
        self.recipe_list.clear()
        chosen = (self.W.get("recipe") or {}).get("id")
        for r in self.recipes:
            latest = r["versions"][-1]["version"] if r.get("versions") else ""
            used = f"Already used by {', '.join(r['feeds'])}" if r.get("feeds") else "Not used by any feed yet"
            lines = [r.get("title") or r["id"], f"{r.get('vendor') or 'vendor not named'} · {r['id']} {latest}"]
            lines += [r["description"]] if r.get("description") else []
            item = QListWidgetItem("\n".join([*lines, used]))
            item.setData(Qt.ItemDataRole.UserRole, r)
            item.setData(Qt.ItemDataRole.AccessibleTextRole, ". ".join([*lines, used]))
            self.recipe_list.addItem(item)
            if r["id"] == chosen:
                self.recipe_list.setCurrentItem(item)
        if self.recipes and self.recipe_list.currentRow() < 0:
            self.recipe_list.setCurrentRow(0)
        self.recipe_state.setText("" if self.recipes else "No recipes are installed yet. Get one from a vendor first.")
        self.recipe_state.setVisible(not self.recipes)

    def _choose(self, r: dict) -> None:
        if (self.W.get("recipe") or {}).get("id") != r["id"]:
            self.W.update(recipe=r, name=model.free_name(r.get("default_name"), self.taken), title=r.get("title") or "",
                          perm=None, **model.recipe_lists(r.get("selection")))

    # ------------------------------------------------------------ step 2: the name, and the permission check
    def _show_name(self) -> None:
        r = self.W["recipe"]
        vendor = f" ({r['vendor']})" if r.get("vendor") else ""
        self.uses_text.setText(f"It uses {r.get('title')}{vendor}.")
        self.name_edit.setText(self.W["name"])
        self.title_edit.setText(self.W["title"])
        self.name_rows.clear()
        perm = self.W.get("perm")
        if not perm or perm.get("uses") != r["default_uses"]:
            self._check_permissions()
        else:
            self._draw_perm()

    def _check_permissions(self) -> None:
        uses = self.W["recipe"]["default_uses"]
        self.W["perm"] = {"running": True, "uses": uses}
        self._draw_perm()
        self.want_permissions.emit(uses)

    def show_permissions(self, uses: str, result: dict | None, error: str | None = None) -> None:
        """setupops.recipe_permissions for ``uses`` (an answer for another recipe is dropped)."""
        if (self.W.get("recipe") or {}).get("default_uses") != uses:
            return
        self.W["perm"] = {"uses": uses, "result": result, "error": error}
        self._draw_perm()

    def _draw_perm(self) -> None:
        p = self.W.get("perm") or {}
        if p.get("running"):
            self.perm_state.setText("Checking with FOLIO…")
            set_lines(self.perm_lines, [], "")
        elif p.get("error"):
            self.perm_state.setText(f"The check couldn't run: {p['error']}")
            set_lines(self.perm_lines, [], "")
        else:
            words, lines = model.permission_words(p.get("result") or {})
            self.perm_state.setText(words)
            set_lines(self.perm_lines, lines, "")
        self.perm_lines.setVisible(bool(self.perm_lines.text()))
        self.perm_button.setEnabled(not p.get("running"))

    # ------------------------------------------------------------ step 3: which records
    def _show_records(self) -> None:
        asking = self.choices is None
        if asking:
            self.choices_button.hide()
        self._fill_records()                # first: lists the window has already come back at once, and are
        if asking:                          # drawn over what the step shows (show_choices keeps it first)
            self.want_choices.emit(False)

    def _read_again(self) -> None:
        """"Read the lists again" (the page's Try again): what this step shows is kept first, so codes typed while
        the lists were missing are still there whatever the answer."""
        self._keep()
        self.reading = True
        self.choices_state.setText("Reading locations and note types from FOLIO…")
        self.choices_state.show()
        self.choices_button.hide()
        self._enable()
        self.choices_state.setFocus(Qt.FocusReason.OtherFocusReason)
        announce(self.choices_state)
        self.want_choices.emit(True)

    def show_choices(self, choices: dict) -> None:
        """{"data": setupops.tenant_choices} or {"error": why}; shared with the settings form. What step 3 shows
        is kept before it is drawn again from the answer (codes typed while the lists were read or missing)."""
        if self.W["step"] == 2:
            self._keep()
        self.choices = choices
        self.reading = False
        if self.W["step"] == 2:
            waited = self.choices_state.hasFocus()          # "Reading…" had the focus: say how it went
            self._fill_records()
            self._enable()
            if waited and self.choices_state.isHidden():
                self.loc.setFocus(Qt.FocusReason.OtherFocusReason)
            elif waited:
                announce(self.choices_state)

    def forget_choices(self) -> None:
        """The FOLIO connection changed: the lists read before may be another tenant's. Step 3, when shown, keeps
        what it has and reads them again."""
        if self.W["step"] == 2:
            self._keep()
        self.choices = None
        self.reading = False
        if self.W["step"] == 2:
            self._show_records()
            self._enable()

    def _fill_records(self) -> None:
        c = (self.choices or {}).get("data")
        error = (self.choices or {}).get("error")
        self.choices_state.setText("Reading locations and note types from FOLIO…" if self.reading or self.choices is None
                                   else f"Couldn't read the lists from FOLIO ({error}). You can type codes "
                                   "instead, separated by commas, or continue and set this later." if error else "")
        self.choices_state.setVisible(bool(self.choices_state.text()))
        self.choices_button.setVisible(bool(error) and not self.reading)
        default = model.recipe_lists((self.W.get("recipe") or {}).get("selection"))
        for picker, caption, key, words, what, missing in (
                (self.loc, self.loc_caption, "exclude_location_codes", "Items in these locations aren't sent.",
                 "leaves out", model.MISSING_LOCATION),
                (self.status, self.status_caption, "exclude_item_statuses", "Items with these statuses aren't "
                 "sent, such as withdrawn or lost items.", "leaves out", model.MISSING_STATUS),
                (self.only, self.only_caption, "include_location_codes", "If you choose any, only items in those "
                 "locations are sent. Choose none to send every location.", "sends only", model.MISSING_LOCATION)):
            options = (status_options(c) if key == "exclude_item_statuses" else location_options(c)) if c else None
            picker.set_options(options, self.W[key], missing)
            help_text = _with_default(words, default[key], what)
            picker.set_help(help_text)
            # A recipe's codes are data: escaped inside <qt>, so a tooltip never reads them as markup.
            caption.setToolTip(f"<qt>{_esc(help_text)}</qt>")
        self.legacy.set_values(self.W["legacy"], c.get("note_types") if c else None)

    # ------------------------------------------------------------ step 5: the summary, and adding
    def _show_summary(self) -> None:
        W, r = self.W, self.W["recipe"]
        default = model.recipe_lists(r.get("selection"))
        legacy = [f"{model.setting_name(k)}: {W['legacy'][k]}" for k in model.LEGACY_KEYS if W["legacy"].get(k)]
        perm = W.get("perm") or {}
        result = perm.get("result")
        self.summary.show_rows([
            ("Recipe", f"{r.get('title')} ({r['default_uses']})"), ("Name", W["name"]), ("Title", W["title"] or W["name"]),
            ("Locations left out", model.left_out_text(W["exclude_location_codes"], default["exclude_location_codes"])),
            ("Item statuses left out", model.left_out_text(W["exclude_item_statuses"],
                                                           default["exclude_item_statuses"])),
            ("Locations sent", f"only {model.left_out_text(W['include_location_codes'], default['include_location_codes'])}"
             if W["include_location_codes"] else "every location not left out"),
            ("Migrated values", "; ".join(legacy) or "none"),
            ("Schedule", model.schedule_words(W["schedule"])),
            ("Permissions", "not checked" if not result else "the account can read everything it needs"
             if result.get("ok") else "missing some, so runs will fail until your FOLIO administrator adds them"),
        ])

    def request(self) -> dict:
        """What to add: the name, the recipe's default range, and the settings (the full lists always go; the
        core drops a value equal to the recipe's default)."""
        W = self.W
        values: dict = {"title": W["title"], "schedule": W["schedule"],
                        "filters": {k: list(W[k]) for k in model.FILTER_LISTS}}
        legacy = {k: v for k, v in W["legacy"].items() if v}
        if legacy:
            values["legacy"] = legacy
        return {"name": W["name"], "uses": W["recipe"]["default_uses"], "values": values}

    def _add(self) -> None:
        if self.busy or self.W["step"] != len(STEPS) - 1 or not self.add_button.isEnabled():
            return
        self.problem.hide()
        self.add.emit()

    def adding(self, busy: bool) -> None:
        self.busy = busy
        if busy:
            self.problem.setText("Adding…")
            self.problem.show()
        self._enable()

    def failed(self, message: str) -> None:
        self.busy = False
        self._enable()
        self._fail(f"The feed wasn't added. {message}")
