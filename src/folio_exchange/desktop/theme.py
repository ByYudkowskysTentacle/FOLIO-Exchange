"""How the app looks: the browser page's design (web/app.css) with FOLIO's own (its Stripes components), so the app
sits well beside FOLIO.

From the page: its symbol (web/logo.svg, the window's icon and the top bar's mark), its panels, lines and muted
text, and its orange on the chosen tab and the chosen sidebar entry. From FOLIO: the dark main navigation bar, blue
primary buttons with bold labels (``--primary``), the 6px radius, the light blue of a chosen row, and the colours
of success, warning and error. FOLIO's own logo is not used: FOLIO-Exchange is not a FOLIO product.

Every pair of colours used for text meets WCAG AA (4.5:1), and borders of fields and the focus ring 3:1, in light
and dark alike (tests/test_desktop_theme.py checks them). Qt's Fusion style draws the controls, so the look is
the same on every Windows version. The app is dark unless the person turns that off (Site, Dark mode), which is
remembered for them (places.dark_mode).
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QFont, QIcon, QPalette
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from . import places

LOGO = Path(__file__).resolve().parent.parent / "web" / "logo.svg"
CHECK = Path(__file__).resolve().parent / "check.svg"     # a ticked box's white tick
MIN_POINTS = 10.5                     # the page's 14px; a larger font chosen in Windows is kept

LIGHT = {
    "bg": "#f6f6f6", "panel": "#ffffff", "panel2": "#fafafa", "line": "#e4e4e4",
    "text": "#212121", "muted": "#616161",                  # FOLIO's secondary text: rgba(0 0 0 / 62%)
    "field_line": "#8c8c8c", "focus": "#2b6cb0", "link": "#2f609f",
    "mark": "#d9531e",          # the symbol's orange (#ff6c37), deep enough for 3:1 as the chosen tab's mark
    "primary": "#1960a4", "primary_hover": "#154f88", "on_primary": "#ffffff",   # FOLIO's --primary
    "select": "#d3e3f3",                                    # FOLIO's chosen row: rgba(37 118 195 / 20%)
    "hover": "#f0f4f9",
    "nav": "#333333", "nav_line": "#1a1a1a", "nav_text": "#ffffff", "nav_muted": "#dddddd",
    "nav_chip": "#6b6b6b",
    "error": "#990000", "warn": "#943e00", "success": "#007700",     # FOLIO's --error, --warn, --success
    "error_fill": "#f4dcda", "warn_fill": "#f9eee6", "success_fill": "#e0eadd",
    "disabled": "#6b6b6b",
}
DARK = {
    "bg": "#181818", "panel": "#212121", "panel2": "#262626", "line": "#353535",
    "text": "#ececec", "muted": "#a3a3a3",
    "field_line": "#858585", "focus": "#7aa7e0", "link": "#93b4ff",
    "mark": "#ff6c37",
    "primary": "#2f6db0", "primary_hover": "#285f99", "on_primary": "#ffffff",
    "select": "#22374f",
    "hover": "#2a2f36",
    "nav": "#0d0d0d", "nav_line": "#000000", "nav_text": "#ffffff", "nav_muted": "#dddddd",
    "nav_chip": "#5a5a5a",
    "error": "#f87171", "warn": "#fbbf24", "success": "#4ade80",
    "error_fill": "#3a1919", "warn_fill": "#332a12", "success_fill": "#16301f",
    "disabled": "#8a8a8a",
}


def palette(t: dict[str, str]) -> QPalette:
    p = QPalette()
    roles = {
        QPalette.ColorRole.Window: t["panel"], QPalette.ColorRole.WindowText: t["text"],
        QPalette.ColorRole.Base: t["panel"], QPalette.ColorRole.AlternateBase: t["panel2"],
        QPalette.ColorRole.Text: t["text"], QPalette.ColorRole.Button: t["panel"],
        QPalette.ColorRole.ButtonText: t["text"], QPalette.ColorRole.PlaceholderText: t["muted"],
        QPalette.ColorRole.ToolTipBase: t["text"], QPalette.ColorRole.ToolTipText: t["panel"],
        QPalette.ColorRole.Highlight: t["select"], QPalette.ColorRole.HighlightedText: t["text"],
        QPalette.ColorRole.Link: t["link"], QPalette.ColorRole.LinkVisited: t["link"],
        QPalette.ColorRole.Mid: t["line"], QPalette.ColorRole.Midlight: t["panel2"],
        QPalette.ColorRole.Dark: t["field_line"], QPalette.ColorRole.Light: t["panel"],
    }
    for role, colour in roles.items():
        p.setColor(role, QColor(colour))
    for role in (QPalette.ColorRole.WindowText, QPalette.ColorRole.Text, QPalette.ColorRole.ButtonText):
        p.setColor(QPalette.ColorGroup.Disabled, role, QColor(t["disabled"]))
    return p


def stylesheet(t: dict[str, str]) -> str:
    return f"""
QMainWindow, QDialog {{ background: {t['panel']}; }}
QScrollArea {{ background: {t['panel']}; border: 0; }}
QWidget#topbar {{ background: {t['nav']}; border-bottom: 1px solid {t['nav_line']}; }}
QWidget#topbar QLabel {{ color: {t['nav_text']}; }}
QLabel#brand {{ font-weight: 700; }}
QLabel#sitechip {{ color: {t['nav_muted']}; border: 1px solid {t['nav_chip']}; border-radius: 10px;
  padding: 2px 10px; }}
QWidget#topbar QMenuBar {{ background: transparent; color: {t['nav_text']}; }}
QWidget#topbar QMenuBar::item {{ background: transparent; color: {t['nav_text']}; padding: 6px 10px;
  border-radius: 4px; }}
QWidget#topbar QMenuBar::item:selected, QWidget#topbar QMenuBar::item:pressed {{ background: {t['nav_chip']}; }}
QMenu {{ background: {t['panel']}; color: {t['text']}; border: 1px solid {t['line']}; padding: 4px; }}
QMenu::item {{ padding: 6px 24px 6px 12px; border-radius: 4px; }}
QMenu::item:selected {{ background: {t['select']}; color: {t['text']}; }}
QMenu::separator {{ height: 1px; background: {t['line']}; margin: 4px 6px; }}

QListWidget#sidebar {{ background: {t['bg']}; border: 0; border-right: 1px solid {t['line']}; padding: 8px 6px; }}
QListWidget#sidebar:focus {{ border: 2px solid {t['focus']}; }}
QListWidget#sidebar::item {{ padding: 7px 8px; border-radius: 6px; color: {t['text']};
  border-left: 3px solid transparent; }}
QListWidget#sidebar::item:hover {{ background: {t['hover']}; }}
QListWidget#sidebar::item:selected {{ background: {t['select']}; color: {t['text']};
  border-left: 3px solid {t['mark']}; }}

QPushButton {{ background: {t['panel']}; color: {t['text']}; border: 1px solid {t['field_line']};
  border-radius: 6px; padding: 6px 14px; font-weight: 700; }}
QPushButton:hover {{ background: {t['hover']}; }}
QPushButton:pressed {{ background: {t['select']}; }}
QPushButton:focus {{ border: 2px solid {t['focus']}; padding: 5px 13px; }}
QPushButton:disabled {{ color: {t['disabled']}; background: {t['panel2']}; border: 1px solid {t['line']};
  padding: 6px 14px; }}
QPushButton[primary="true"] {{ background: {t['primary']}; color: {t['on_primary']};
  border: 1px solid {t['primary']}; }}
QPushButton[primary="true"]:hover {{ background: {t['primary_hover']}; border-color: {t['primary_hover']}; }}
QPushButton[primary="true"]:focus {{ border: 2px solid {t['text']}; padding: 5px 13px; }}
QPushButton[primary="true"]:disabled {{ color: {t['disabled']}; background: {t['panel2']};
  border: 1px solid {t['line']}; padding: 6px 14px; }}

QLineEdit, QSpinBox, QComboBox, QPlainTextEdit {{ background: {t['panel']}; color: {t['text']};
  border: 1px solid {t['field_line']}; border-radius: 6px; padding: 4px 8px;
  selection-background-color: {t['select']}; selection-color: {t['text']}; }}
QPlainTextEdit {{ padding: 2px 4px; }}
QLineEdit:focus, QSpinBox:focus, QComboBox:focus, QPlainTextEdit:focus {{ border: 2px solid {t['focus']};
  padding: 3px 7px; }}
QPlainTextEdit:focus {{ padding: 1px 3px; }}
QLineEdit:disabled, QSpinBox:disabled, QComboBox:disabled {{ background: {t['panel2']}; color: {t['disabled']};
  border-color: {t['line']}; }}

QTabWidget::pane {{ border: 0; border-top: 1px solid {t['line']}; }}
QTabBar::tab {{ background: transparent; color: {t['muted']}; padding: 8px 12px; border: 0;
  border-bottom: 3px solid transparent; }}
QTabBar::tab:hover {{ color: {t['text']}; }}
QTabBar::tab:selected {{ color: {t['text']}; border-bottom: 3px solid {t['mark']}; }}
QTabBar::tab:selected:focus {{ background: {t['select']}; border-radius: 4px; }}

QTableView, QListWidget {{ background: {t['panel']}; color: {t['text']}; border: 1px solid {t['line']};
  gridline-color: {t['line']}; selection-background-color: {t['select']}; selection-color: {t['text']};
  alternate-background-color: {t['panel2']}; }}
QTableView:focus, QListWidget:focus {{ border: 2px solid {t['focus']}; }}
QListWidget::item {{ padding: 4px; }}
QListWidget::item:selected {{ background: {t['select']}; color: {t['text']}; }}
QHeaderView::section {{ background: {t['panel2']}; color: {t['text']}; border: 0;
  border-bottom: 1px solid {t['line']}; border-right: 1px solid {t['line']}; padding: 6px 10px;
  font-weight: 700; }}
QTableView#rows, QPlainTextEdit#log {{ font-family: "Cascadia Mono", Consolas, monospace; }}

QCheckBox::indicator, QAbstractItemView::indicator {{ width: 16px; height: 16px; border-radius: 4px;
  border: 1px solid {t['field_line']}; background: {t['panel']}; }}
QCheckBox::indicator:checked, QAbstractItemView::indicator:checked {{ background: {t['primary']};
  border-color: {t['primary']}; image: url({CHECK.as_posix()}); }}
QRadioButton::indicator {{ width: 16px; height: 16px; border-radius: 9px; border: 1px solid {t['field_line']};
  background: {t['panel']}; }}
QRadioButton::indicator:checked {{ border: 1px solid {t['primary']}; background: qradialgradient(cx:0.5, cy:0.5,
  radius:0.5, fx:0.5, fy:0.5, stop:0 {t['primary']}, stop:0.5 {t['primary']}, stop:0.62 {t['panel']},
  stop:1 {t['panel']}); }}
QCheckBox::indicator:disabled, QRadioButton::indicator:disabled {{ border-color: {t['line']};
  background: {t['panel2']}; }}
QRadioButton, QCheckBox {{ padding: 2px 4px; spacing: 8px; }}
QRadioButton:focus, QCheckBox:focus {{ background: {t['select']}; border-radius: 4px; }}

QSplitter::handle {{ background: {t['panel']}; }}
QSplitter::handle:vertical {{ height: 5px; background: {t['bg']}; border-top: 1px solid {t['line']};
  border-bottom: 1px solid {t['line']}; }}
QLabel[role="problem"] {{ color: {t['error']}; font-weight: 700; }}
QLabel[role="help"] {{ color: {t['muted']}; }}
QLabel#banner {{ background: {t['warn_fill']}; color: {t['text']}; border: 0; border-radius: 6px;
  margin: 8px 8px 0 8px; }}
QFrame#waiting {{ background: {t['warn_fill']}; border: 0; border-radius: 8px; }}
QToolTip {{ background: {t['text']}; color: {t['panel']}; border: 0; padding: 4px 8px; }}
"""


def logo_icon() -> QIcon:
    return QIcon(str(LOGO))


def mark(size: int = 22) -> QLabel:
    """FOLIO-Exchange's symbol, as the page's top bar shows it: decoration, so screen readers skip it."""
    label = QLabel()
    label.setPixmap(logo_icon().pixmap(size, size))
    label.setAccessibleName("")
    label.setFixedSize(size, size)
    return label


def is_dark(app: QApplication) -> bool:
    return bool(app.property("fx_dark"))


def tokens(app: QApplication) -> dict[str, str]:
    return DARK if is_dark(app) else LIGHT


def apply(app: QApplication, dark: bool | None = None) -> None:
    """Give the whole app the look: dark, unless the person chose light (``dark`` None: as they chose)."""
    app.setStyle("Fusion")
    font = QFont(app.font())
    if font.pointSizeF() < MIN_POINTS:
        font.setPointSizeF(MIN_POINTS)
        app.setFont(font)
    app.setWindowIcon(logo_icon())
    colour(app, places.dark_mode() if dark is None else dark)


def colour(app: QApplication, dark: bool) -> None:
    """Light or dark, at once, in every window."""
    app.setProperty("fx_dark", dark)
    t = DARK if dark else LIGHT
    app.setPalette(palette(t))
    app.setStyleSheet(stylesheet(t))


def set_dark(app: QApplication, dark: bool) -> None:
    """The person's choice (Site, Dark mode): shown now and remembered for the next start."""
    colour(app, dark)
    places.set_dark_mode(dark)


def primary(button: QPushButton) -> QPushButton:
    """The view's main action, as the page's (and FOLIO's) primary buttons: filled with the primary blue."""
    button.setProperty("primary", True)
    return button


def role(widget: QWidget, name: str) -> QWidget:
    """A label's part in a form ("problem", "help"), which sets its colour."""
    widget.setProperty("role", name)
    return widget
