"""Minimalist studio dark-mode theme stylesheet and Fusion dark palette for Obol PySide6 Desktop GUI."""

from __future__ import annotations

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from obol.gui.icons import get_svg_path
from obol.gui.tokens import (
    ACCENT,
    ACCENT_BORDER,
    ACCENT_HOVER,
    ACCENT_LIGHT,
    BG_BUTTON,
    BG_BUTTON_HOVER,
    BG_CARD,
    BG_DRAG,
    BG_INPUT,
    BG_INPUT_READONLY,
    BG_LIST,
    BG_LIST_ALT,
    BG_SELECTED,
    BG_SUBTLE,
    BG_WINDOW,
    BG_WORKSPACE,
    BORDER_CONTROL,
    BORDER_DEFAULT,
    BORDER_FOCUS,
    BORDER_HOVER,
    BORDER_SUBTLE,
    DANGER_BG_HOVER,
    DANGER_BORDER,
    DANGER_TEXT,
    ERROR,
    ERROR_BG,
    ERROR_BORDER,
    ERROR_TEXT,
    INFO,
    SUCCESS,
    SUCCESS_BG,
    SUCCESS_BORDER,
    SUCCESS_TEXT,
    TEXT_DISABLED,
    TEXT_MUTED,
    TEXT_PRIMARY,
    TEXT_SECONDARY,
    WARNING,
    WARNING_BG,
    WARNING_BORDER,
    WARNING_TEXT,
)


def _ensure_branch_chevron_icons() -> tuple[str, str]:
    """Writes crisp monochrome SVG chevron icons for QTreeView and QComboBox controls."""
    return (
        get_svg_path("chevron_right", TEXT_MUTED),
        get_svg_path("chevron_down", BORDER_FOCUS),
    )


def get_studio_dark_stylesheet() -> str:
    right_chevron, down_chevron = _ensure_branch_chevron_icons()
    check_svg = get_svg_path("check", "#ffffff")
    return f"""
QMainWindow, QDialog, QWidget {{
    background-color: {BG_WINDOW};
    color: {TEXT_PRIMARY};
    font-family: "Segoe UI", "Inter", sans-serif;
    font-size: 12px;
}}
QFrame#TopBar {{
    background-color: {BG_WINDOW};
    border-bottom: 1px solid {BORDER_DEFAULT};
}}
QFrame#PanelCard {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 8px;
}}
QFrame#WorkspaceCard {{
    background-color: {BG_WORKSPACE};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 8px;
}}
QFrame#ComponentHeader {{
    background-color: transparent;
    border: none;
    border-bottom: 1px solid {BORDER_DEFAULT};
}}
QFrame#DividerLine {{
    background-color: {BORDER_DEFAULT};
    max-height: 1px;
    min-height: 1px;
    border: none;
}}
QFrame#VerticalDivider {{
    background-color: {BORDER_DEFAULT};
    max-width: 1px;
    min-width: 1px;
    border: none;
}}
QScrollArea#InspectorScroll {{
    background-color: transparent;
    border: none;
}}
QScrollArea#InspectorScroll > QWidget > QWidget {{
    background-color: transparent;
}}
QLabel#BrandWordmark {{
    background-color: transparent;
    color: {TEXT_PRIMARY};
    font-weight: 700;
    font-size: 13px;
    letter-spacing: 1.5px;
    padding: 0px 2px;
}}
QLabel#BrandBadge {{
    background-color: transparent;
    color: {TEXT_PRIMARY};
    font-weight: 700;
    letter-spacing: 1px;
    padding: 2px 4px;
}}
QLabel#SectionHeader {{
    color: {TEXT_MUTED};
    font-weight: 600;
    font-size: 11px;
    letter-spacing: 0.6px;
    text-transform: uppercase;
}}
QLabel#MutedText {{
    color: {TEXT_MUTED};
}}
QLabel#DropHint {{
    color: {TEXT_MUTED};
    background-color: transparent;
    border: none;
    padding: 0px 2px;
    font-size: 11px;
}}
QLabel#FormatChipActive {{
    background-color: #14532d;
    color: {SUCCESS_TEXT};
    border: 1px solid {SUCCESS_BORDER};
    border-radius: 10px;
    padding: 2px 8px;
    font-weight: 600;
    font-size: 11px;
}}
QLabel#FormatChipIdle {{
    background-color: {BG_CARD};
    color: {TEXT_DISABLED};
    border: 1px solid {BORDER_CONTROL};
    border-radius: 10px;
    padding: 2px 8px;
    font-size: 11px;
}}
QPushButton {{
    background-color: {BG_BUTTON};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_CONTROL};
    border-radius: 5px;
    padding: 5px 11px;
}}
QPushButton:hover {{
    border-color: {BORDER_HOVER};
    background-color: {BG_BUTTON_HOVER};
}}
QPushButton:pressed {{
    background-color: #18181f;
}}
QPushButton:focus {{
    border: 1px solid {BORDER_FOCUS};
    outline: none;
}}
QPushButton#GhostButton {{
    background-color: transparent;
    color: {TEXT_SECONDARY};
    border: 1px solid transparent;
    border-radius: 5px;
    padding: 4px 9px;
}}
QPushButton#GhostButton:hover {{
    background-color: {BG_BUTTON};
    color: #ffffff;
    border-color: {BORDER_CONTROL};
}}
QPushButton#GhostButton:focus {{
    border: 1px solid {BORDER_FOCUS};
    outline: none;
}}
QPushButton#PrimaryButton {{
    background-color: {ACCENT};
    color: #ffffff;
    border: 1px solid {ACCENT_BORDER};
    border-radius: 5px;
    font-weight: 600;
    padding: 5px 12px;
}}
QPushButton#PrimaryButton:hover {{
    background-color: {ACCENT_HOVER};
    border-color: {BORDER_FOCUS};
}}
QPushButton#PrimaryButton:focus {{
    border: 1px solid {ACCENT_LIGHT};
    outline: none;
}}
QPushButton#AccentOutlineButton {{
    background-color: #172554;
    color: #dbeafe;
    border: 1px solid {ACCENT_BORDER};
    border-radius: 5px;
    font-weight: 600;
    padding: 5px 12px;
}}
QPushButton#AccentOutlineButton:hover {{
    background-color: {BG_SELECTED};
    border-color: {BORDER_FOCUS};
    color: #ffffff;
}}
QPushButton#AccentOutlineButton:pressed {{
    background-color: #1e3a8a;
}}
QPushButton#AccentOutlineButton:focus {{
    border: 1px solid {ACCENT_LIGHT};
    outline: none;
}}
QPushButton#AccentOutlineButton:disabled {{
    background-color: #141418;
    color: {TEXT_DISABLED};
    border-color: #202028;
}}
QPushButton#DangerButton {{
    background-color: transparent;
    color: {DANGER_TEXT};
    border: 1px solid {DANGER_BORDER};
    border-radius: 5px;
}}
QPushButton#DangerButton:hover {{
    background-color: {DANGER_BG_HOVER};
    color: #ffffff;
    border-color: {ERROR_BORDER};
}}
QPushButton#DangerButton:focus {{
    border: 1px solid {ERROR};
    outline: none;
}}
QPushButton#SubtleToolButton {{
    background-color: {BG_SUBTLE};
    color: {TEXT_SECONDARY};
    border: 1px solid {BORDER_CONTROL};
    border-radius: 5px;
    padding: 4px 10px;
}}
QPushButton#SubtleToolButton:hover {{
    background-color: #252530;
    border-color: {BORDER_HOVER};
    color: #ffffff;
}}
QPushButton#SubtleToolButton:focus {{
    border: 1px solid {BORDER_FOCUS};
    outline: none;
}}
QPushButton#FastDropActive {{
    background-color: {WARNING_BG};
    color: {WARNING_TEXT};
    border: 1px solid {WARNING_BORDER};
    border-radius: 5px;
    font-weight: 600;
    padding: 4px 10px;
}}
QPushButton#FastDropActive:hover {{
    background-color: #78350f;
    border-color: {WARNING};
}}
QPushButton#BreadcrumbButton {{
    background-color: transparent;
    border: 1px solid transparent;
    border-radius: 4px;
    color: #e4e4e7;
    padding: 3px 6px;
    text-align: left;
    font-weight: 600;
    font-size: 12px;
}}
QPushButton#BreadcrumbButton:hover {{
    background-color: {BG_BUTTON};
    border-color: {BORDER_CONTROL};
    color: #ffffff;
}}
QPushButton#BreadcrumbButton:focus {{
    border: 1px solid {BORDER_FOCUS};
    outline: none;
}}
QPushButton:disabled {{
    background-color: #141418;
    color: {TEXT_DISABLED};
    border-color: #202028;
}}
QPushButton#GhostButton:disabled {{
    background-color: transparent;
    color: {TEXT_DISABLED};
    border-color: transparent;
}}
QLineEdit, QComboBox, QTextEdit, QPlainTextEdit, QSpinBox, QDoubleSpinBox {{
    background-color: {BG_INPUT};
    color: {TEXT_PRIMARY};
    border: 1px solid #2a2a34;
    border-radius: 5px;
    padding: 4px 8px;
    selection-background-color: {ACCENT};
    selection-color: #ffffff;
}}
QLineEdit:focus, QComboBox:focus, QTextEdit:focus, QPlainTextEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus {{
    border-color: {BORDER_FOCUS};
}}
QLineEdit:read-only {{
    background-color: {BG_INPUT_READONLY};
    color: #a1a1aa;
    border-color: #202028;
}}
QSpinBox:disabled, QDoubleSpinBox:disabled, QLineEdit:disabled, QComboBox:disabled {{
    background-color: {BG_INPUT_READONLY};
    color: {TEXT_DISABLED};
    border-color: #202028;
}}
QLineEdit#FilterInput {{
    background-color: {BG_INPUT};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 5px;
    padding: 4px 8px;
}}
QLineEdit#FilterInput:focus {{
    border-color: {BORDER_FOCUS};
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
    background-color: {BG_WORKSPACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_CONTROL};
    border-radius: 5px;
    padding: 3px;
    selection-background-color: {BG_SELECTED};
    selection-color: #ffffff;
    outline: none;
}}
QHeaderView::section {{
    background-color: {BG_CARD};
    color: {TEXT_MUTED};
    font-weight: 600;
    font-size: 11px;
    padding: 5px 8px;
    border: none;
    border-right: 1px solid {BORDER_SUBTLE};
    border-bottom: 1px solid {BORDER_DEFAULT};
}}
QTreeWidget, QListWidget, QTableWidget {{
    background-color: {BG_LIST};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_SUBTLE};
    border-radius: 6px;
    outline: none;
    alternate-background-color: {BG_LIST_ALT};
}}
QTreeWidget:focus, QListWidget:focus, QTableWidget:focus {{
    border-color: {BORDER_FOCUS};
}}
QTreeWidget[dragActive="true"], QListWidget[dragActive="true"], QTableWidget[dragActive="true"] {{
    background-color: {BG_DRAG};
    border: 2px dashed {ACCENT_BORDER};
}}
QTreeWidget::item, QListWidget::item {{
    padding: 5px 6px;
    border-radius: 4px;
}}
QTreeWidget::item:hover, QListWidget::item:hover {{
    background-color: {BG_BUTTON};
}}
QTreeWidget::item:selected, QListWidget::item:selected {{
    background-color: {BG_SELECTED};
    color: #ffffff;
}}
QTableWidget::item {{
    padding: 4px 6px;
}}
QTableWidget::item:selected {{
    background-color: {BG_SELECTED};
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
    border: none;
    border-top: 1px solid {BORDER_DEFAULT};
    background-color: transparent;
}}
QTabBar::tab {{
    background-color: transparent;
    color: {TEXT_MUTED};
    border: none;
    border-bottom: 2px solid transparent;
    padding: 7px 14px;
    margin-right: 4px;
}}
QTabBar::tab:hover {{
    color: #e4e4e7;
}}
QTabBar::tab:selected {{
    background-color: transparent;
    color: #ffffff;
    font-weight: 600;
    border-bottom: 2px solid {ACCENT_BORDER};
}}
QTabBar::tab:focus {{
    color: #ffffff;
    border-bottom: 2px solid {BORDER_FOCUS};
}}
QSplitter::handle {{
    background-color: #1c1c24;
    width: 6px;
    height: 6px;
}}
QSplitter::handle:hover {{
    background-color: {ACCENT_BORDER};
}}
QScrollBar:vertical {{
    background: transparent;
    width: 8px;
    margin: 2px 1px 2px 1px;
}}
QScrollBar::handle:vertical {{
    background: {BORDER_CONTROL};
    min-height: 24px;
    border-radius: 3px;
}}
QScrollBar::handle:vertical:hover {{
    background: {BORDER_HOVER};
}}
QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{
    height: 0px;
}}
QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{
    background: transparent;
}}
QScrollBar:horizontal {{
    background: transparent;
    height: 8px;
    margin: 1px 2px 1px 2px;
}}
QScrollBar::handle:horizontal {{
    background: {BORDER_CONTROL};
    min-width: 24px;
    border-radius: 3px;
}}
QScrollBar::handle:horizontal:hover {{
    background: {BORDER_HOVER};
}}
QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal {{
    width: 0px;
}}
QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal {{
    background: transparent;
}}
QCheckBox {{
    spacing: 7px;
    color: #e4e4e7;
}}
QCheckBox:focus {{
    color: #ffffff;
}}
QCheckBox::indicator {{
    width: 15px;
    height: 15px;
    border-radius: 4px;
    border: 1px solid #3f3f4a;
    background-color: {BG_INPUT};
}}
QCheckBox::indicator:hover {{
    border-color: {ACCENT_BORDER};
}}
QCheckBox::indicator:focus {{
    border-color: {BORDER_FOCUS};
}}
QCheckBox::indicator:checked {{
    background-color: {ACCENT};
    border-color: {ACCENT_BORDER};
    image: url("{check_svg}");
}}
QToolTip {{
    background-color: {BG_BUTTON};
    color: {TEXT_PRIMARY};
    border: 1px solid #32323e;
    border-radius: 5px;
    padding: 5px 8px;
}}
QFrame#StatusBar {{
    background-color: {BG_WINDOW};
    border-top: 1px solid {BORDER_DEFAULT};
}}
QLabel#StatusPillLocal {{
    background-color: {BG_CARD};
    color: {TEXT_MUTED};
    border: 1px solid {BORDER_CONTROL};
    border-radius: 6px;
    padding: 2px 9px;
    font-size: 11px;
}}
QPushButton#StatusPillLocal {{
    background-color: {BG_CARD};
    color: {TEXT_SECONDARY};
    border: 1px solid {BORDER_CONTROL};
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 11px;
}}
QPushButton#StatusPillLocal:hover {{
    border-color: {ACCENT_BORDER};
    background-color: {BG_SUBTLE};
    color: #ffffff;
}}
QPushButton#StatusPillLocal:pressed {{
    background-color: {BG_BUTTON};
}}
QPushButton#StatusPillLocal:focus {{
    border: 1px solid {BORDER_FOCUS};
    outline: none;
}}
QLabel#StatusPillOnline {{
    background-color: {SUCCESS_BG};
    color: {SUCCESS_TEXT};
    border: 1px solid {SUCCESS_BORDER};
    border-radius: 6px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 600;
}}
QPushButton#StatusPillOnline {{
    background-color: {SUCCESS_BG};
    color: {SUCCESS_TEXT};
    border: 1px solid {SUCCESS_BORDER};
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
}}
QPushButton#StatusPillOnline:hover {{
    background-color: #14532d;
    border-color: {SUCCESS};
}}
QPushButton#StatusPillOnline:pressed {{
    background-color: #0b2d1a;
}}
QPushButton#StatusPillOnline:focus {{
    border: 1px solid {SUCCESS};
    outline: none;
}}
QLabel#StatusPillOffline {{
    background-color: {WARNING_BG};
    color: {WARNING_TEXT};
    border: 1px solid {WARNING_BORDER};
    border-radius: 6px;
    padding: 2px 9px;
    font-size: 11px;
    font-weight: 600;
}}
QPushButton#StatusPillOffline {{
    background-color: {WARNING_BG};
    color: {WARNING_TEXT};
    border: 1px solid {WARNING_BORDER};
    border-radius: 10px;
    padding: 2px 10px;
    font-size: 11px;
    font-weight: 600;
}}
QPushButton#StatusPillOffline:hover {{
    background-color: #78350f;
    border-color: {WARNING};
}}
QPushButton#StatusPillOffline:pressed {{
    background-color: #3a1402;
}}
QPushButton#StatusPillOffline:focus {{
    border: 1px solid {WARNING};
    outline: none;
}}
QLabel#ContextBadge {{
    background-color: #172554;
    color: #bfdbfe;
    border: 1px solid {ACCENT};
    border-radius: 8px;
    padding: 2px 8px;
    font-size: 11px;
    font-weight: 600;
}}
QLabel#MetaDirtyBadge {{
    color: {WARNING};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#MetaSavedBadge {{
    color: {SUCCESS};
    font-size: 11px;
}}
QLabel#WarningText {{
    color: {WARNING};
    font-size: 11px;
}}
QLabel#SuccessText {{
    color: {SUCCESS};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#ErrorText {{
    color: {ERROR};
    font-size: 11px;
    font-weight: 600;
}}
QLabel#FeedbackSuccess {{
    background-color: {SUCCESS_BG};
    color: {SUCCESS_TEXT};
    border: 1px solid {SUCCESS_BORDER};
    border-radius: 5px;
    padding: 5px 10px;
    font-weight: 600;
}}
QLabel#FeedbackWarning {{
    background-color: {WARNING_BG};
    color: {WARNING_TEXT};
    border: 1px solid {WARNING_BORDER};
    border-radius: 5px;
    padding: 5px 10px;
    font-weight: 600;
}}
QLabel#FeedbackError {{
    background-color: {ERROR_BG};
    color: {ERROR_TEXT};
    border: 1px solid {ERROR_BORDER};
    border-radius: 5px;
    padding: 5px 10px;
    font-weight: 600;
}}
QLabel#FeedbackInfo {{
    background-color: {BG_SUBTLE};
    color: {INFO};
    border: 1px solid {BORDER_CONTROL};
    border-radius: 5px;
    padding: 5px 10px;
    font-weight: 600;
}}
QFrame#EmptyStateCard {{
    background-color: {BG_CARD};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 10px;
}}
QFrame#DccExchangeBar {{
    background-color: {BG_LIST};
    border: 1px solid {BORDER_DEFAULT};
    border-radius: 6px;
}}
QMenu {{
    background-color: {BG_WORKSPACE};
    color: {TEXT_PRIMARY};
    border: 1px solid {BORDER_CONTROL};
    border-radius: 6px;
    padding: 4px;
}}
QMenu::item {{
    padding: 5px 22px 5px 12px;
    border-radius: 4px;
}}
QMenu::item:selected {{
    background-color: {BG_SELECTED};
    color: #ffffff;
}}
QMenu::item:disabled {{
    color: {TEXT_DISABLED};
    background-color: transparent;
}}
QMenu::separator {{
    height: 1px;
    background-color: {BORDER_DEFAULT};
    margin: 4px 8px;
}}
"""


STUDIO_DARK_STYLESHEET = get_studio_dark_stylesheet()


def apply_studio_dark_theme(app_or_widget: object) -> None:
    """Applies Fusion style, dark QPalette, and high-contrast chevron/header QSS."""
    app = QApplication.instance()
    if isinstance(app, QApplication) and not app.property("_obol_dark_theme_applied"):
        app.setStyle("Fusion")
        palette = QPalette()
        palette.setColor(QPalette.ColorRole.Window, QColor(BG_WINDOW))
        palette.setColor(QPalette.ColorRole.WindowText, QColor(TEXT_PRIMARY))
        palette.setColor(QPalette.ColorRole.Base, QColor(BG_LIST))
        palette.setColor(QPalette.ColorRole.AlternateBase, QColor(BG_LIST_ALT))
        palette.setColor(QPalette.ColorRole.ToolTipBase, QColor(BG_BUTTON))
        palette.setColor(QPalette.ColorRole.ToolTipText, QColor(TEXT_PRIMARY))
        palette.setColor(QPalette.ColorRole.Text, QColor(TEXT_PRIMARY))
        palette.setColor(QPalette.ColorRole.Button, QColor(BG_BUTTON))
        palette.setColor(QPalette.ColorRole.ButtonText, QColor(TEXT_PRIMARY))
        palette.setColor(QPalette.ColorRole.BrightText, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Highlight, QColor(ACCENT))
        palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#ffffff"))
        palette.setColor(QPalette.ColorRole.Mid, QColor(TEXT_MUTED))
        palette.setColor(QPalette.ColorRole.Light, QColor(TEXT_DISABLED))
        app.setPalette(palette)
        app.setProperty("_obol_dark_theme_applied", True)
    if hasattr(app_or_widget, "setStyleSheet"):
        app_or_widget.setStyleSheet(get_studio_dark_stylesheet())
