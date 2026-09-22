"""Design tokens and the Qt stylesheet (light and dark).

Contrast: body text on surfaces is ≥ 7:1 and secondary text ≥ 4.5:1 in both
modes (WCAG 2.1 AA). Status colours are always paired with an icon or text.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Tokens:
    bg: str
    surface: str
    surface_2: str
    border: str
    text: str
    text_2: str
    accent: str
    accent_text: str
    accent_soft: str
    ok: str
    warn: str
    danger: str
    focus: str


# Palette aligned with the TimelinerX mark: brand blue (#1463e6 / #4c9dff) on neutral light
# surfaces, deep brand navy for dark mode.
LIGHT = Tokens(bg="#f3f5f9", surface="#ffffff", surface_2="#edf0f6", border="#d6dce6", text="#111827",
               text_2="#4a5466", accent="#1463e6", accent_text="#ffffff", accent_soft="#e2ecfd",
               ok="#1d7a3c", warn="#8a5a00", danger="#b3261e", focus="#1463e6")
DARK = Tokens(bg="#0b1020", surface="#111729", surface_2="#182036", border="#263049", text="#e8ecf4",
              text_2="#9ea9bf", accent="#4c9dff", accent_text="#06101f", accent_soft="#15294a",
              ok="#5fcf87", warn="#f0b44c", danger="#ff7b72", focus="#7db8ff")


def _arrow_files(color: str):
    """Write small chevron SVGs for combo/spin arrows into the cache dir (QSS needs files)."""
    from ..utils.paths import cache_dir
    d = cache_dir() / "ui"
    d.mkdir(parents=True, exist_ok=True)
    out = {}
    for name, path in (("down", "M3 5.5l4 4 4-4"), ("up", "M3 8.5l4-4 4 4")):
        f = d / f"chevron-{name}-{color.lstrip('#')}.svg"
        if not f.exists():
            f.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 14 14" width="14" height="14">'
                         f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.6" '
                         f'stroke-linecap="round" stroke-linejoin="round"/></svg>', encoding="utf-8")
        out[name] = f.as_posix()
    return out


def stylesheet(t: Tokens, font_family) -> str:
    a = _arrow_files(t.text_2)
    if isinstance(font_family, (list, tuple)):
        font_family = '", "'.join(font_family)
    return f"""
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 22px; border: none; }}
QComboBox::down-arrow {{ image: url("{a['down']}"); width: 12px; height: 12px; }}
QAbstractSpinBox::up-button, QDateEdit::up-button {{ subcontrol-origin: border; subcontrol-position: top right; width: 20px; border: none; }}
QAbstractSpinBox::down-button, QDateEdit::down-button {{ subcontrol-origin: border; subcontrol-position: bottom right; width: 20px; border: none; }}
QAbstractSpinBox::up-arrow {{ image: url("{a['up']}"); width: 10px; height: 10px; }}
QAbstractSpinBox::down-arrow {{ image: url("{a['down']}"); width: 10px; height: 10px; }}
* {{ font-family: "{font_family}"; font-size: 13px; color: {t.text}; }}
QMainWindow, QWidget#Root {{ background: {t.bg}; }}
QWidget#Sidebar {{ background: {t.surface}; border-right: 1px solid {t.border}; }}
QLabel#Brand {{ padding: 20px 16px 4px 18px; }}
QLabel#BrandSub {{ color: {t.text_2}; font-size: 11px; padding: 0 16px 14px 18px; }}
QListWidget#Nav {{ background: transparent; border: none; outline: none; padding: 4px 8px; }}
QListWidget#Nav::item {{ padding: 8px 10px; border-radius: 6px; margin: 1px 0; color: {t.text_2}; }}
QListWidget#Nav::item:hover {{ background: {t.surface_2}; color: {t.text}; }}
QListWidget#Nav::item:selected {{ background: {t.accent_soft}; color: {t.text}; font-weight: 600; }}
QListWidget#Nav:focus {{ border: none; }}
QLabel#PageTitle {{ font-size: 22px; font-weight: 700; }}
QLabel#PageSub {{ color: {t.text_2}; }}
QLabel#SectionTitle {{ font-size: 14px; font-weight: 650; }}
QLabel[muted="true"] {{ color: {t.text_2}; }}
QLabel#StatValue {{ font-size: 20px; font-weight: 700; }}
QLabel#StatLabel {{ color: {t.text_2}; font-size: 11px; }}
QFrame#Card {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 10px; }}
QFrame#Banner[kind="warn"] {{ background: {t.surface}; border: 1px solid {t.warn}; border-radius: 8px; }}
QFrame#Banner[kind="danger"] {{ background: {t.surface}; border: 1px solid {t.danger}; border-radius: 8px; }}
QFrame#Banner[kind="info"] {{ background: {t.surface}; border: 1px solid {t.accent}; border-radius: 8px; }}
QFrame#Banner[kind="ok"] {{ background: {t.surface}; border: 1px solid {t.ok}; border-radius: 8px; }}
QPushButton {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 6px; padding: 7px 14px; }}
QPushButton:hover {{ background: {t.surface_2}; }}
QPushButton:focus {{ border: 2px solid {t.focus}; padding: 6px 13px; }}
QPushButton:disabled {{ color: {t.text_2}; background: {t.surface_2}; }}
QPushButton[primary="true"] {{ background: {t.accent}; color: {t.accent_text}; border: 1px solid {t.accent}; font-weight: 600; }}
QPushButton[primary="true"]:hover {{ background: {t.accent}; }}
QPushButton[danger="true"] {{ color: {t.danger}; }}
QLabel#OptionTitle {{ font-weight: 650; }}
QWidget#FlowDetail {{ background: {t.surface_2}; border-radius: 10px; }}
QLabel#MadeWith {{ font-size: 15px; font-weight: 650; padding-top: 4px; }}
QPushButton#LinkButton {{ padding: 8px 16px; border-radius: 18px; }}
QPushButton[segment] {{ border-radius: 0; padding: 7px 10px; background: {t.surface}; }}
QPushButton[segment="first"] {{ border-top-left-radius: 7px; border-bottom-left-radius: 7px; }}
QPushButton[segment="last"] {{ border-top-right-radius: 7px; border-bottom-right-radius: 7px; }}
QPushButton[segment="mid"], QPushButton[segment="last"] {{ border-left: none; }}
QPushButton[segment]:hover {{ background: {t.surface_2}; }}
QPushButton[segment]:checked {{ background: {t.accent_soft}; color: {t.text}; font-weight: 650;
    border: 1px solid {t.accent}; }}
QPushButton[segment]:focus {{ border: 2px solid {t.focus}; padding: 6px 9px; }}
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QDateEdit, QPlainTextEdit, QTextBrowser {{
    background: {t.surface}; border: 1px solid {t.border}; border-radius: 6px; padding: 5px 8px;
    selection-background-color: {t.accent}; selection-color: {t.accent_text}; }}
QLineEdit:focus, QComboBox:focus, QSpinBox:focus, QDoubleSpinBox:focus, QDateEdit:focus, QPlainTextEdit:focus {{
    border: 2px solid {t.focus}; padding: 4px 7px; }}
QComboBox QAbstractItemView {{ background: {t.surface}; border: 1px solid {t.border}; selection-background-color: {t.accent_soft}; selection-color: {t.text}; }}
QCheckBox {{ spacing: 8px; }}
QCheckBox:focus {{ color: {t.accent}; }}
QTabWidget::pane {{ border: 1px solid {t.border}; border-radius: 8px; background: {t.surface}; top: -1px; }}
QTabBar::tab {{ padding: 7px 14px; color: {t.text_2}; border: none; }}
QTabBar::tab:selected {{ color: {t.text}; border-bottom: 2px solid {t.accent}; font-weight: 600; }}
QTabBar::tab:focus {{ color: {t.accent}; }}
QProgressBar {{ background: {t.surface_2}; border: none; border-radius: 4px; height: 8px; text-align: center; color: transparent; }}
QProgressBar::chunk {{ background: {t.accent}; border-radius: 4px; }}
QTableWidget, QTreeWidget, QListWidget {{ background: {t.surface}; border: 1px solid {t.border}; border-radius: 8px;
    gridline-color: {t.border}; alternate-background-color: {t.surface_2}; }}
QHeaderView::section {{ background: {t.surface_2}; border: none; border-bottom: 1px solid {t.border}; padding: 6px; font-weight: 600; }}
QScrollArea {{ border: none; background: transparent; }}
QScrollBar:vertical {{ background: transparent; width: 10px; }}
QScrollBar::handle:vertical {{ background: {t.border}; border-radius: 5px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; }}
QSlider::groove:horizontal {{ height: 4px; background: {t.border}; border-radius: 2px; }}
QSlider::handle:horizontal {{ background: {t.accent}; width: 14px; height: 14px; margin: -5px 0; border-radius: 7px; }}
QSlider:focus::handle:horizontal {{ border: 2px solid {t.focus}; }}
QToolTip {{ background: {t.surface}; color: {t.text}; border: 1px solid {t.border}; padding: 4px; }}
QStatusBar {{ background: {t.surface}; border-top: 1px solid {t.border}; color: {t.text_2}; }}
"""
