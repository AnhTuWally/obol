"""Studio dark-mode theme stylesheet and Fusion dark palette for Obol PySide6 Desktop GUI."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

_ICON_DIR: Optional[Path] = None


def _ensure_branch_chevron_icons() -> tuple[str, str]:
    """Writes crisp light-colored SVG chevron icons for QTreeView expand/collapse branches."""
    global _ICON_DIR
    if _ICON_DIR is None or not _ICON_DIR.exists():
        _ICON_DIR = Path(tempfile.gettempdir()) / "obol_gui_icons"
        _ICON_DIR.mkdir(parents=True, exist_ok=True)

    right_svg = _ICON_DIR / "chevron_right.svg"
    down_svg = _ICON_DIR / "chevron_down.svg"

    right_svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" '
        'fill="none" stroke="#e4e4e7" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
        '<polyline points="9 18 15 12 9 6"></polyline></svg>',
        encoding="utf-8",
    )
    down_svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="12" height="12" viewBox="0 0 24 24" '
        'fill="none" stroke="#60a5fa" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">'
        '<polyline points="6 9 12 15 18 9"></polyline></svg>',
        encoding="utf-8",
    )
    return right_svg.as_posix(), down_svg.as_posix()


def get_studio_dark_stylesheet() -> str:
    right_chevron, down_chevron = _ensure_branch_chevron_icons()
    return f"""
QMainWindow, QDialog, QWidget {{
    background-color: #18181b;
    color: #f4f4f5;
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 12px;
}}
QFrame#TopBar {{
    background-color: #09090b;
    border-bottom: 1px solid #27272a;
}}
QFrame#PanelCard {{
    background-color: #202024;
    border: 1px solid #27272a;
    border-radius: 6px;
}}
QLabel#BrandBadge {{
    background-color: #2563eb;
    color: #ffffff;
    font-weight: bold;
    padding: 3px 8px;
    border-radius: 4px;
}}
QLabel#SectionHeader {{
    color: #a1a1aa;
    font-weight: bold;
    font-size: 11px;
    text-transform: uppercase;
}}
QLabel#MutedText {{
    color: #a1a1aa;
}}
QLabel#DropHint {{
    color: #71717a;
    background-color: transparent;
    border: none;
    padding: 0px 2px;
    font-size: 11px;
}}
QLabel#FormatChipActive {{
    background-color: #14532d;
    color: #bbf7d0;
    border: 1px solid #22c55e;
    border-radius: 10px;
    padding: 2px 8px;
    font-weight: 600;
    font-size: 11px;
}}
QLabel#FormatChipIdle {{
    background-color: #18181b;
    color: #71717a;
    border: 1px solid #3f3f46;
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
}}
QPushButton {{
    background-color: #27272a;
    color: #f4f4f5;
    border: 1px solid #3f3f46;
    border-radius: 4px;
    padding: 5px 12px;
}}
QPushButton:hover {{
    border-color: #3b82f6;
    background-color: #323238;
}}
QPushButton#PrimaryButton {{
    background-color: #2563eb;
    color: #ffffff;
    border: 1px solid #3b82f6;
    font-weight: 600;
}}
QPushButton#PrimaryButton:hover {{
    background-color: #1d4ed8;
}}
QPushButton#DangerButton {{
    background-color: #27272a;
    color: #fca5a5;
    border: 1px solid #7f1d1d;
}}
QPushButton#DangerButton:hover {{
    background-color: #991b1b;
    color: #ffffff;
    border-color: #ef4444;
}}
QLineEdit, QComboBox, QTextEdit, QPlainTextEdit {{
    background-color: #09090b;
    color: #f4f4f5;
    border: 1px solid #3f3f46;
    border-radius: 4px;
    padding: 4px 8px;
}}
QComboBox::drop-down {{
    border: none;
    width: 22px;
}}
QComboBox::down-arrow {{
    image: url("{down_chevron}");
    width: 11px;
    height: 11px;
}}
QComboBox QAbstractItemView {{
    background-color: #18181b;
    color: #f4f4f5;
    border: 1px solid #3f3f46;
    selection-background-color: #1e3a8a;
    selection-color: #ffffff;
}}
QHeaderView::section {{
    background-color: #1f1f23;
    color: #d4d4d8;
    font-weight: 600;
    padding: 5px 8px;
    border: none;
    border-right: 1px solid #27272a;
    border-bottom: 1px solid #27272a;
}}
QTreeWidget, QListWidget, QTableWidget {{
    background-color: #121215;
    color: #f4f4f5;
    border: 1px solid #27272a;
    border-radius: 4px;
    outline: none;
    alternate-background-color: #16161a;
}}
QTreeWidget[dragActive="true"], QListWidget[dragActive="true"], QTableWidget[dragActive="true"] {{
    background-color: #172554;
    border: 2px dashed #3b82f6;
}}
QTreeWidget::item, QListWidget::item {{
    padding: 4px 6px;
    border-radius: 3px;
}}
QTreeWidget::item:hover, QListWidget::item:hover {{
    background-color: #27272a;
}}
QTreeWidget::item:selected, QListWidget::item:selected {{
    background-color: #1e3a8a;
    color: #ffffff;
}}
QTreeView::branch {{
    background-color: transparent;
}}
QTreeView::branch:has-children:!has-siblings:closed,
QTreeView::branch:closed:has-children:has-siblings {{
    border-image: none;
    image: url("{right_chevron}");
}}
QTreeView::branch:open:has-children:!has-siblings,
QTreeView::branch:open:has-children:has-siblings {{
    border-image: none;
    image: url("{down_chevron}");
}}
QTabWidget::pane {{
    border: 1px solid #27272a;
    background-color: #141418;
    border-radius: 4px;
}}
QTabBar::tab {{
    background-color: #09090b;
    color: #a1a1aa;
    border: 1px solid #27272a;
    border-bottom: none;
    padding: 7px 16px;
    margin-right: 2px;
    border-top-left-radius: 4px;
    border-top-right-radius: 4px;
}}
QTabBar::tab:selected {{
    background-color: #141418;
    color: #ffffff;
    font-weight: bold;
    border-top: 2px solid #3b82f6;
}}
QSplitter::handle {{
    background-color: #27272a;
}}
QFrame#StatusBar {{
    background-color: #09090b;
    border-top: 1px solid #27272a;
}}
QLabel#StatusPillLocal, QPushButton#StatusPillLocal {{
    background-color: #18181b;
    color: #a1a1aa;
    border: 1px solid #3f3f46;
    border-radius: 9px;
    padding: 2px 10px;
    font-size: 11px;
}}
QPushButton#StatusPillLocal:hover {{
    border-color: #3b82f6;
    color: #f4f4f5;
}}
QLabel#StatusPillOnline, QPushButton#StatusPillOnline {{
    background-color: #14532d;
    color: #bbf7d0;
    border: 1px solid #22c55e;
    border-radius: 9px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
}}
QPushButton#StatusPillOnline:hover {{
    background-color: #166534;
}}
QLabel#StatusPillOffline, QPushButton#StatusPillOffline {{
    background-color: #78350f;
    color: #fde68a;
    border: 1px solid #f59e0b;
    border-radius: 9px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#ContextBadge {{
    background-color: #1e3a8a;
    color: #bfdbfe;
    border: 1px solid #3b82f6;
    border-radius: 8px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#WarningText {{
    color: #fbbf24;
    font-size: 11px;
}}
QLabel#SuccessText {{
    color: #4ade80;
    font-size: 11px;
    font-weight: 600;
}}
QPushButton#FastDropActive {{
    background-color: #78350f;
    color: #fef3c7;
    border: 1px solid #f59e0b;
    font-weight: 600;
}}
QPushButton#FastDropActive:hover {{
    background-color: #92400e;
    border-color: #fbbf24;
}}
QPushButton#BreadcrumbButton {{
    background-color: transparent;
    border: none;
    color: #a1a1aa;
    padding: 2px 6px;
    text-align: left;
    font-size: 11px;
}}
QPushButton#BreadcrumbButton:hover {{
    background-color: #27272a;
    color: #f4f4f5;
}}
QPushButton#SubtleToolButton {{
    background-color: #18181b;
    color: #d4d4d8;
    border: 1px solid #3f3f46;
    padding: 4px 10px;
}}
QPushButton#SubtleToolButton:hover {{
    background-color: #27272a;
    border-color: #3b82f6;
}}
QPushButton:disabled {{
    background-color: #1f1f23;
    color: #52525b;
    border-color: #27272a;
}}
QFrame#EmptyStateCard {{
    background-color: #18181c;
    border: 1px dashed #3f3f46;
    border-radius: 8px;
}}
QLineEdit#FilterInput {{
    background-color: #09090b;
    border: 1px solid #27272a;
    border-radius: 4px;
    padding: 4px 8px;
}}
QLineEdit#FilterInput:focus {{
    border-color: #3b82f6;
}}
"""


STUDIO_DARK_STYLESHEET = get_studio_dark_stylesheet()


def apply_studio_dark_theme(app_or_widget: object) -> None:
    """Applies Fusion style, dark QPalette, and high-contrast chevron/header QSS."""
    app = QApplication.instance()
    if isinstance(app, QApplication):
        app.setStyle("Fusion")
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor("#18181b"))
        palette.setColor(QPalette.ColorRole.WindowText, QColor("#f4f4f5"))
        palette.setColor(QPalette.ColorRole.Base, QColor("#121215"))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#18181b"))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor("#27272a"))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor("#f4f4f5"))
        palette.setColor(QPalette.ColorRole.Text, QColor("#f4f4f5"))
        palette.setColor(QPalette.ColorRole.Button, QColor("#27272a"))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor("#f4f4f5"))
        palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor("#2563eb"))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Mid, QColor("#a1a1aa"))
        palette.setColor(QPalette.ColorRole.Light, QColor("#71717a"))
        app.setPalette(palette)
    if hasattr(app_or_widget, "setStyleSheet"):
        app_or_widget.setStyleSheet(get_studio_dark_stylesheet())


