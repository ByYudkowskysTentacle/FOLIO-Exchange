"""The desktop app's look (desktop/theme.py): the page's design with FOLIO's, readable in light and dark."""
from __future__ import annotations

import pytest
from conftest import QT_LOADS

if not QT_LOADS:
    pytest.skip("needs the desktop extra (PySide6) and pytest-qt, and a Qt that loads", allow_module_level=True)

from PySide6.QtCore import QtMsgType, qInstallMessageHandler  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton, QWidget  # noqa: E402
from test_desktop_feeds import window  # noqa: E402

from folio_exchange.desktop import places, theme  # noqa: E402
from folio_exchange.desktop.picker import SitePicker  # noqa: E402


def _luminance(colour: str) -> float:
    rgb = [int(colour.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    lin = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in rgb]
    return 0.2126 * lin[0] + 0.7152 * lin[1] + 0.0722 * lin[2]


def contrast(a: str, b: str) -> float:
    hi, lo = sorted((_luminance(a), _luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


TEXT_PAIRS = [("text", "panel"), ("text", "bg"), ("text", "panel2"), ("text", "select"), ("text", "hover"),
              ("text", "warn_fill"), ("muted", "panel"), ("muted", "bg"), ("link", "panel"), ("error", "panel"),
              ("on_primary", "primary"), ("on_primary", "primary_hover"), ("nav_text", "nav"),
              ("nav_muted", "nav"), ("nav_text", "nav_chip")]
MARK_PAIRS = [("field_line", "panel"), ("focus", "panel"), ("focus", "bg"), ("mark", "panel"), ("mark", "select"),
              ("primary", "panel")]


@pytest.mark.parametrize("name", ["LIGHT", "DARK"])
def test_every_colour_pair_is_readable(name):
    """WCAG AA: 4.5:1 for text, 3:1 for field borders, the focus ring and the chosen tab's and entry's mark."""
    t = getattr(theme, name)
    assert set(theme.LIGHT) == set(theme.DARK)
    low = [(a, b, round(contrast(t[a], t[b]), 2)) for a, b in TEXT_PAIRS if contrast(t[a], t[b]) < 4.5]
    low += [(a, b, round(contrast(t[a], t[b]), 2)) for a, b in MARK_PAIRS if contrast(t[a], t[b]) < 3]
    assert low == []


def test_the_primary_blue_is_folios_and_the_symbol_is_the_pages():
    assert theme.LIGHT["primary"] == "#1960a4"                           # FOLIO's --primary (Stripes)
    assert theme.LOGO.is_file() and "#FF6C37" in theme.LOGO.read_text()  # the page's own symbol
    assert theme.CHECK.is_file()
    assert not theme.logo_icon().isNull()


@pytest.mark.parametrize("name", ["LIGHT", "DARK"])
def test_the_stylesheet_parses(qtbot, name):
    """Qt drops a stylesheet it cannot parse with only a warning: none here."""
    warnings: list[str] = []

    def handler(kind, _context, message):
        if kind in (QtMsgType.QtWarningMsg, QtMsgType.QtCriticalMsg):
            warnings.append(message)

    previous = qInstallMessageHandler(handler)
    try:
        w = QWidget()
        qtbot.addWidget(w)
        w.setStyleSheet(theme.stylesheet(getattr(theme, name)))
        w.ensurePolished()
        QPushButton("x", w).ensurePolished()
    finally:
        qInstallMessageHandler(previous)
    assert not [m for m in warnings if "style" in m.lower()]


@pytest.fixture
def app_look():
    """The app's look as a test found it, put back afterwards (apply and the menu change it for every window)."""
    app = QApplication.instance()
    kept = (app.style().name(), app.styleSheet(), app.palette(), app.font(), app.windowIcon(), app.property("fx_dark"))
    yield app
    style, sheet, palette, font, icon, dark = kept
    app.setStyle(style)
    app.setStyleSheet(sheet)
    app.setPalette(palette)
    app.setFont(font)
    app.setWindowIcon(icon)
    app.setProperty("fx_dark", dark)


def test_the_app_is_dark_unless_the_person_chose_light(qtbot, app_look):
    app = app_look
    assert places.dark_mode()                                            # nothing chosen yet: dark
    theme.apply(app)
    assert theme.is_dark(app) and not app.windowIcon().isNull()
    assert app.font().pointSizeF() >= theme.MIN_POINTS
    assert app.palette().color(app.palette().ColorRole.Base).name() == theme.DARK["panel"]
    assert theme.DARK["primary"] in app.styleSheet()
    places.set_dark_mode(False)
    theme.apply(app)                                                     # the next start remembers it
    assert not theme.is_dark(app) and app.palette().color(app.palette().ColorRole.Base).name() == "#ffffff"


def test_the_site_menu_switches_between_dark_and_light_and_remembers(qtbot, site, app_look):
    app = app_look
    theme.apply(app)
    w = window(qtbot, site)
    assert w.dark_action.isCheckable() and w.dark_action.isChecked() and w.dark_action in w.menu_bar.actions()[
        0].menu().actions()
    w.dark_action.trigger()                                              # off: light, at once
    assert not theme.is_dark(app) and theme.LIGHT["primary"] in app.styleSheet() and not places.dark_mode()
    w.dark_action.trigger()                                              # and back
    assert theme.is_dark(app) and places.dark_mode()


def test_the_main_actions_are_primary_and_the_top_bar_names_the_site(qtbot, site):
    w = window(qtbot, site)
    w.select("feed:chq")
    assert w.feed.run_button.property("primary") is True                # as the page's Run
    assert not w.feed.preview_button.property("primary")
    assert w.wizard.next_button.property("primary") is True
    assert w.setup.form.save_button.property("primary") is True
    assert w.site_chip.text() == site.name and w.menuWidget().objectName() == "topbar"
    assert w.menu_bar.actions()[0].text() == "&Site"                    # the menu lives in the top bar
    assert w.menuBar() is w.menu_bar and w.menuWidget().objectName() == "topbar"   # and asking never replaces it


def test_a_first_start_makes_setting_up_the_primary_action(qtbot):
    picker = SitePicker()
    qtbot.addWidget(picker)
    picker.show()
    new = next(b for b in picker.findChildren(QPushButton) if b.accessibleName() == "Set up a new site")
    assert new.property("primary") is True and new.isDefault()
    qtbot.waitUntil(new.hasFocus, timeout=5000)                          # where Enter goes, the focus is
