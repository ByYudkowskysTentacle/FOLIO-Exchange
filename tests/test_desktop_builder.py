"""The desktop app's recipe builder, run offscreen with pytest-qt against the mock FOLIO: a custom recipe built
from nothing, from a copy of an installed recipe, and changed in place; what is said beside a field before the core
is asked; the core's problems and its review; a file changed underneath; and a save refused while another action
runs. Each refusal changes nothing and loses nothing typed."""
from __future__ import annotations

import pytest
from conftest import QT_LOADS

if not QT_LOADS:
    pytest.skip("needs the desktop extra (PySide6) and pytest-qt, and a Qt that loads", allow_module_level=True)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QTextDocument  # noqa: E402
from PySide6.QtWidgets import QLineEdit  # noqa: E402
from sitekit import make_feed  # noqa: E402
from test_desktop_setup import (  # noqa: E402, F401
    LONG,
    audit,
    blocking_job,
    click,
    focused,
    heard,
    no_sleep,
    settled,
    shown_text,
    window,
)

from folio_exchange import recipeops, views  # noqa: E402
from folio_exchange.cli import main  # noqa: E402
from folio_exchange.config import read_yaml  # noqa: E402
from folio_exchange.desktop.builder import RecipeBuilder, header_for, slug  # noqa: E402


def open_builder(qtbot, w, press) -> RecipeBuilder:
    w.select("recipes")
    qtbot.waitUntil(lambda: w.recipes.data is not None, timeout=LONG)
    click(qtbot, press() if callable(press) else press)
    qtbot.waitUntil(lambda: w.builder_dialog is not None, timeout=LONG)
    return w.builder_dialog


def review_text(d: RecipeBuilder) -> str:
    doc = QTextDocument()
    doc.setHtml(d.review_text.text())
    return doc.toPlainText()


def reviewed(qtbot, d: RecipeBuilder) -> str:
    """Press Review recipe and wait for the core's answer."""
    click(qtbot, d.review_button)
    qtbot.waitUntil(lambda: d.pages.currentIndex() == 1 and not d.busy, timeout=LONG)
    return review_text(d)


def saved(qtbot, w, d: RecipeBuilder) -> None:
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: w.builder_dialog is None, timeout=LONG)
    settled(qtbot, w)


def custom_copy(site, feed: bool = True) -> None:
    """A custom copy of chq-academic, used by the feed chq, which has delivered once."""
    site_, library, feeds, _errors = views.load(site)
    form = recipeops.builder_form(site_, library, feeds, copy_of="chq-academic")
    recipeops.save_custom(site_, library, feeds, form["draft"], copied_from=form["copied_from"])
    if feed:
        make_feed(site, uses="chq-academic-custom@^1.0")
        assert main(["-s", str(site), "run", "chq"]) == 2
        assert main(["-s", str(site), "release", "chq", "--yes"]) == 0


# ------------------------------------------------------------ from nothing
def test_a_custom_recipe_is_built_from_nothing_and_offered_to_new_feeds(qtbot, site):
    w = window(qtbot, site)
    d = open_builder(qtbot, w, w.recipes.build_button)
    assert d.windowTitle() == "FOLIO-Exchange – build a custom recipe" and not d.rid.isReadOnly()
    assert d.files.count() == 1 and d.files.item(0).text() == "items – delimited" and d.link_box.isHidden()
    d.vendor.setFocus()
    qtbot.keyClicks(d.vendor, "Acme Analytics")
    assert d.rid.text() == "acme-analytics"                        # suggested from the vendor until a title is typed
    qtbot.keyClicks(d.title_edit, "Acme items")                    # QTest types ASCII only
    assert d.rid.text() == "acme-items"
    d.title_edit.setText("Acme – items")
    d.tabs.setCurrentIndex(1)
    editor = d.delimited
    qtbot.keyClicks(editor.filter, "barcode")
    shown = [editor.available.item(i) for i in range(editor.available.count()) if not editor.available.item(i).isHidden()]
    assert [i.data(Qt.ItemDataRole.UserRole) for i in shown] == ["barcode"]
    editor.available.setCurrentItem(shown[0])
    click(qtbot, editor.add_button)
    editor.filter.clear()
    for field in ("title", "loans_total"):
        editor.add_column(field)
    editor.columns.item(2, 0).setText("Checkouts")                  # a header typed over the suggested one
    editor.columns.item(0, 2).setCheckState(Qt.CheckState.Checked)
    editor.columns.selectRow(2)
    click(qtbot, editor.up_button)
    assert [c["header"] for c in editor.cols()] == ["Barcode", "Checkouts", "Title"]
    editor.separator.set_value("|")
    text = reviewed(qtbot, d)
    assert d.review_title.text() == "Review the recipe" and d.save_button.hasFocus()
    assert "Acme – items · Acme Analytics · acme-items" in text
    assert "items · acme-items_items_YYYYMMDD.csv" in text and "3 columns: Barcode, Checkouts, Title." in text
    assert "Reads from FOLIO: instances, holdings, items, loans. Patron data: none." in text
    assert d.review_note.text().startswith("Saving adds the recipe to this site and sends nothing.")
    assert not (site / "recipes").exists()                          # a review saves nothing
    saved(qtbot, w, d)
    data = read_yaml(site / "recipes" / "acme-items" / "1.0.0.yaml")
    assert data["vendor"] == "Acme Analytics" and data["reads"] == ["instances", "holdings", "items", "loans"]
    assert data["outputs"][0]["delimiter"] == "|" and data["outputs"][0]["columns"] == [
        {"header": "Barcode", "field": "barcode", "required": True},
        {"header": "Checkouts", "field": "loans_total"}, {"header": "Title", "field": "title"}]
    assert w.recipes.notice.text() == "Saved acme-items. Use Add a vendor feed to try it with Preview."
    entry = next(a for a in audit(site) if a["action"] == "custom recipe added from the app")
    assert entry["detail"]["recipe"] == "acme-items@1.0.0"
    qtbot.waitUntil(lambda: "edit" in w.recipes.recipe_buttons.get("acme-items", {}), timeout=LONG)
    assert any(t == "1.0.0 – built on this site" for t in shown_text(w.recipes))
    assert "edit" not in w.recipes.recipe_buttons["chq-academic"]   # not built here: it can only be copied
    w.select("add")
    qtbot.waitUntil(lambda: w.wizard.recipes is not None, timeout=LONG)
    assert "acme-items" in [r["id"] for r in w.wizard.recipes]


def test_problems_the_form_can_tell_are_said_beside_their_field(qtbot, site, heard):  # noqa: F811
    w = window(qtbot, site)
    d = open_builder(qtbot, w, w.recipes.build_button)
    asked = []
    d.review.connect(asked.append)

    def said() -> str:
        click(qtbot, d.review_button)
        shown = [p for rows in (d.about_rows, d.link_rows, d.check_rows, d.delimited.rows, d.delimited.format_rows,
                                d.marc.rows, d.marc.item_rows) for p in rows.shown_problems()]
        shown += [x.text() for x in (d.files_problem, d.marc.sub_problem) if not x.isHidden()]
        assert len(shown) == 1 and heard[-1] == shown[0], shown
        return shown[0]

    assert said() == "Enter the vendor's name." and d.vendor.hasFocus()
    d.vendor.setText("Acme")
    assert said() == "Enter a title."
    d.title_edit.setText("Acme items")
    d.rid.setText("Acme Items")
    assert said().startswith("Use lowercase letters, digits and hyphens")
    d.rid.setText("chq-academic")
    assert said() == "There's already a recipe called chq-academic. Choose another id." and d.rid.hasFocus()
    d.rid.setText("acme-items")
    assert said() == "Add at least one column to items." and d.tabs.currentIndex() == 1
    d.delimited.add_column("barcode", "Code")
    d.delimited.add_column("item_hrid", "Code")
    d.delimited.columns.item(1, 0).setText("Code")
    assert said() == "Two columns of items have the header Code. Headers must differ."
    d.delimited.columns.item(1, 0).setText("")
    assert said() == "Give every column of items a header."
    d.delimited.columns.item(1, 0).setText("HRID")
    d.delimited.separator.set_value("||")
    assert said() == "Enter one character to put between values."
    d.delimited.separator.set_value(";")
    click(qtbot, d.add_marc)
    assert not d.add_marc.isEnabled() and d.link_box.isVisibleTo(d) and d.files.currentRow() == 1
    assert said() == ("Add a column for the field instance_id to a delimited file. The vendor needs it to match "
                      "items to bibs.")
    d.marc.embed.setChecked(True)
    assert said() == "Add at least one subfield, or don't write items into the bib records."
    click(qtbot, d.marc.add_button)
    assert d.marc.sub_problem.text() == "Enter one letter or digit for the subfield code."
    d.marc.code.setText("b")
    d.marc.field.setCurrentIndex(d.marc.field.findData("barcode"))
    click(qtbot, d.marc.add_button)
    assert d.marc.value()["item_field"]["subfields"] == {"b": "barcode"}
    d.files.setCurrentRow(0)
    d.delimited.add_column("instance_id", "Bib")
    assert d.link_column.count() == 1 and d.link_column.currentData() == "Bib"
    d.tabs.setCurrentIndex(3)
    d.ratio.setText("lots")
    assert said().startswith("Enter a percentage from 0 to 100") and d.tabs.currentIndex() == 3
    d.ratio.setText("80")
    d.schedule.other.setChecked(True)
    d.schedule.custom.setText("someday")
    assert "someday" in said()
    d.schedule.custom.setText("daily 05:00")
    assert asked == []                                              # the core was never asked meanwhile
    text = reviewed(qtbot, d)
    assert "Items match bibs by MARC 999 ff $i and the column Bib." in text
    assert "bibs · MARC 21" in text and "Items embedded in 949: $b barcode." in text
    draft = asked[-1]
    assert draft["checks"]["min_item_ratio"] == 0.8 and draft["default_schedule"] == "daily 05:00"
    assert draft["link"] == {"bib_field": "999 ff $i", "item_column": "Bib", "value": "instance_id"}
    click(qtbot, d.back_button)
    assert d.pages.currentIndex() == 0 and d.vendor.text() == "Acme"       # nothing lost
    click(qtbot, d.cancel_button)
    qtbot.waitUntil(lambda: w.builder_dialog is None, timeout=5000)
    assert not (site / "recipes").exists()


def test_the_cores_problems_are_listed_and_nothing_is_saved(qtbot, site, heard):  # noqa: F811
    w = window(qtbot, site)
    d = open_builder(qtbot, w, w.recipes.build_button)
    d.vendor.setText("Acme")
    d.title_edit.setText("Acme items")
    d.rid.setText("acme-items")
    d.delimited.add_column("barcode")
    d.zone.setText("Mars/Phobos")
    d.delimited.dates.set_value("no date parts")
    text = reviewed(qtbot, d)
    assert d.review_title.text() == "Fix these before saving" and not d.save_button.isVisible()
    assert "options.timezone: unknown time zone 'Mars/Phobos'" in text and "outputs.items.date_format" in text
    assert d.review_note.text() == "Nothing was saved." and d.back_button.hasFocus()
    assert heard[-1] == d.review_text.text()
    click(qtbot, d.back_button)
    assert d.zone.text() == "Mars/Phobos"                           # what was typed stays
    d.zone.setText("America/Detroit")
    d.delimited.dates.set_value("%d.%m.%Y")
    assert d.delimited.dates.box.isVisibleTo(d.delimited) and d.delimited.dates.value() == "%d.%m.%Y"
    assert "Review the recipe" == d.review_title.text() or reviewed(qtbot, d)
    saved(qtbot, w, d)
    data = read_yaml(site / "recipes" / "acme-items" / "1.0.0.yaml")
    assert data["options"]["timezone"] == "America/Detroit" and data["outputs"][0]["date_format"] == "%d.%m.%Y"


# ------------------------------------------------------------ from a copy
def test_a_copy_of_an_installed_recipe_starts_with_its_layout(qtbot, site):
    w = window(qtbot, site)
    d = open_builder(qtbot, w, lambda: w.recipes.recipe_buttons["chq-academic"]["copy"])
    assert d.copied_from == "chq-academic@1.0.0" and d.rid.text() == "chq-academic-custom"
    assert [d.files.item(i).text() for i in range(d.files.count())] == ["bibs – MARC records", "items – delimited"]
    assert not d.link_box.isHidden() and d.link_column.currentData() == "Bib record ID"
    assert set(d.must.value()) == {"instance_id", "status", "material_type"}
    d.tabs.setCurrentIndex(1)
    d.files.setCurrentRow(1)
    assert d.delimited.separator.value() == "|" and d.delimited.empty.text() == "None"
    assert len(d.delimited.cols()) == 20 and d.delimited.cols()[0] == {
        "header": "Bib record ID", "field": "instance_id", "required": True}
    d.rid.setText("chq-ours")
    d.delimited.columns.selectRow(19)
    click(qtbot, d.delimited.remove_button)
    text = reviewed(qtbot, d)
    assert "19 columns: Bib record ID" in text and "Number of renewals" not in text
    saved(qtbot, w, d)
    data = read_yaml(site / "recipes" / "chq-ours" / "1.0.0.yaml")
    original = read_yaml(recipeops.BUNDLED_RECIPES / "chq-academic" / "1.0.0.yaml")
    assert data["link"] == original["link"] and data["checks"] == original["checks"]
    assert data["outputs"][1]["columns"] == original["outputs"][1]["columns"][:19]
    assert data["changelog"][0]["changes"].startswith("Copied from chq-academic@1.0.0")
    assert not read_yaml(site / "feeds" / "chq.yaml")["uses"].startswith("chq-ours")      # no feed was touched


# ------------------------------------------------------------ changed in place
def test_a_custom_recipe_is_changed_in_place_and_says_which_delivery_is_held(qtbot, site):
    custom_copy(site)
    w = window(qtbot, site)
    d = open_builder(qtbot, w, lambda: w.recipes.recipe_buttons["chq-academic-custom"]["edit"])
    assert d.windowTitle() == "FOLIO-Exchange – change the recipe chq-academic-custom" and d.rid.isReadOnly()
    assert any(t.startswith("Used by chq. If you change what the files look like") for t in shown_text(d))
    assert reviewed(qtbot, d) == "The form matches the saved recipe."
    assert d.review_title.text() == "Nothing has changed" and d.close_button.isVisible()
    assert not d.save_button.isVisible()
    click(qtbot, d.back_button)
    d.tabs.setCurrentIndex(1)
    d.files.setCurrentRow(1)
    d.delimited.columns.item(1, 0).setText("Item ID")
    text = reviewed(qtbot, d)
    assert ("chq uses this recipe. Its next delivery is held for your review because the layout changes: items "
            "file: columns changed: 'Item record ID' renamed to 'Item ID'.") in text
    assert d.review_note.text() == "Saving changes the recipe for chq from the next run."
    saved(qtbot, w, d)
    assert w.recipes.notice.text() == ("Saved chq-academic-custom. The layout changed, so the next delivery of chq "
                                       "is held for your review.")
    data = read_yaml(site / "recipes" / "chq-academic-custom" / "1.0.0.yaml")
    assert data["outputs"][1]["columns"][1]["header"] == "Item ID" and data["version"] == "1.0.0"
    assert main(["-s", str(site), "run", "chq"]) == 2               # and so it is


def test_a_change_that_would_stop_a_feed_loading_is_refused_in_the_review(qtbot, site):
    custom_copy(site)
    make_feed(site, uses="chq-academic-custom@^1.0", outputs={"items": {"empty_value": ""}})
    before = (site / "recipes" / "chq-academic-custom" / "1.0.0.yaml").read_bytes()
    w = window(qtbot, site)
    d = open_builder(qtbot, w, lambda: w.recipes.recipe_buttons["chq-academic-custom"]["edit"])
    d.files.setCurrentRow(1)
    d.delimited.name.setText("rows")
    d.delimited.name.textEdited.emit("rows")
    assert d.files.item(1).text() == "rows – delimited"
    text = reviewed(qtbot, d)
    assert d.review_title.text() == "Fix these before saving" and "The feed chq would stop loading" in text
    assert not d.save_button.isVisible()
    click(qtbot, d.back_button)
    click(qtbot, d.cancel_button)
    assert (site / "recipes" / "chq-academic-custom" / "1.0.0.yaml").read_bytes() == before


def test_a_file_changed_underneath_keeps_what_was_entered_and_can_be_replaced(qtbot, site, heard):  # noqa: F811
    custom_copy(site, feed=False)
    path = site / "recipes" / "chq-academic-custom" / "1.0.0.yaml"
    w = window(qtbot, site)
    d = open_builder(qtbot, w, lambda: w.recipes.recipe_buttons["chq-academic-custom"]["edit"])
    d.vendor.setText("Ours")
    path.write_text(path.read_text(encoding="utf-8").replace("default_schedule: weekly", "default_schedule: daily"),
                    encoding="utf-8")                               # by hand, while the form is open
    reviewed(qtbot, d)
    click(qtbot, d.save_button)
    qtbot.waitUntil(lambda: d.review_title.text() == "Not saved because the file changed", timeout=LONG)
    assert "What you entered is still here." in d.review_text.text() and heard[-1] == d.review_text.text()
    assert d.replace_button.isVisible() and not d.save_button.isVisible()
    assert read_yaml(path)["vendor"] == "collectionHQ"
    click(qtbot, d.replace_button)
    qtbot.waitUntil(lambda: w.builder_dialog is None, timeout=LONG)
    settled(qtbot, w)
    data = read_yaml(path)
    assert data["vendor"] == "Ours" and data["default_schedule"] == "weekly"


def test_a_save_is_refused_while_another_action_runs_and_loses_nothing(qtbot, site, blocking_job, heard):  # noqa: F811
    w = window(qtbot, site)
    d = open_builder(qtbot, w, w.recipes.build_button)
    d.vendor.setText("Acme")
    d.title_edit.setText("Acme items")
    d.rid.setText("acme-items")
    d.delimited.add_column("barcode")
    reviewed(qtbot, d)
    _job, go = blocking_job(w.jobs)
    click(qtbot, d.save_button)
    assert d.pages.currentIndex() == 0 and d.problem.text().startswith("Not saved. Another action is still running")
    assert heard[-1] == d.problem.text() and d.vendor.text() == "Acme" and not (site / "recipes").exists()
    go.set()
    settled(qtbot, w)
    reviewed(qtbot, d)
    saved(qtbot, w, d)
    assert (site / "recipes" / "acme-items" / "1.0.0.yaml").is_file()


def test_enter_adds_a_column_or_a_subfield_without_starting_the_review(qtbot, site):
    """In a dialog, Enter also presses the default button: here it must only do what the focused control offers."""
    w = window(qtbot, site)
    d = open_builder(qtbot, w, w.recipes.build_button)
    asked = []
    d.review.connect(asked.append)
    d.tabs.setCurrentIndex(1)
    editor = d.delimited
    editor.filter.setFocus()
    qtbot.keyClicks(editor.filter, "barc")
    qtbot.keyClick(editor.filter, Qt.Key.Key_Return)
    assert focused(editor.available) and editor.available.currentItem().data(Qt.ItemDataRole.UserRole) == "barcode"
    qtbot.keyClick(editor.available, Qt.Key.Key_Return)
    assert editor.cols() == [{"header": "Barcode", "field": "barcode"}]
    editor.columns.setFocus()
    editor.columns.setCurrentCell(0, 0)
    qtbot.keyClick(editor.columns, Qt.Key.Key_Return)                  # Enter on a row: type over its header
    box = editor.columns.findChild(QLineEdit)
    assert box is not None and box.text() == "Barcode"
    box.setText("Code")
    qtbot.keyClick(box, Qt.Key.Key_Return)                             # Qt takes the typed header a moment later
    qtbot.waitUntil(lambda: editor.cols() == [{"header": "Code", "field": "barcode"}], timeout=5000)
    click(qtbot, d.add_marc)
    d.marc.code.setFocus()
    qtbot.keyClicks(d.marc.code, "b")
    qtbot.keyClick(d.marc.code, Qt.Key.Key_Return)
    assert list(d.marc.value()["item_field"]["subfields"]) == ["b"]
    assert asked == [] and d.pages.currentIndex() == 0 and not d.busy


def test_a_recipe_that_cannot_be_opened_says_why(qtbot, site):
    w = window(qtbot, site)
    w.select("recipes")
    qtbot.waitUntil(lambda: w.recipes.data is not None, timeout=LONG)
    w.build_recipe(edit="chq-academic")                             # not built here
    qtbot.waitUntil(lambda: w.recipes.notice.text().startswith("Couldn't open"), timeout=LONG)
    assert "wasn't built on this site" in w.recipes.notice.text() and w.builder_dialog is None


def test_ids_and_headers_are_suggested_from_plain_words():
    assert slug("Acme Analytics – items & bibs!") == "acme-analytics-items-bibs" and slug("  ") == ""
    assert header_for("loans_total") == "Loans total" and header_for("barcode") == "Barcode"
