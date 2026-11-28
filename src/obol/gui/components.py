"""Shared reusable UI primitives, feedback banners, busy runners, and composite widgets."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

from PySide6.QtCore import QObject, QThread, Qt, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QVBoxLayout,
    QWidget,
)

from obol.gui.icons import get_icon
from obol.gui.theme import apply_studio_dark_theme
from obol.gui.tokens import ICON_SM, TEXT_MUTED

_ACTIVE_BUSY_WORKERS: list[tuple[QThread, QObject]] = []


def repolish(widget: QWidget) -> None:
    """Re-evaluates QSS styling on a widget after dynamic property or objectName changes."""
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


def open_folder_in_os(folder: Path) -> bool:
    """Opens a local folder in the OS file browser; returns False if missing or unopenable."""
    if not folder.exists():
        return False
    if sys.platform == "win32":
        try:
            os.startfile(str(folder))  # type: ignore[attr-defined]
            return True
        except OSError:
            return False
    return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder))))


class InlineStatusBanner(QLabel):
    """Non-blocking inline status banner styled via QSS feedback object names."""

    _LEVEL_OBJECT_NAMES: dict[str, str] = {
        "ok": "FeedbackSuccess",
        "success": "FeedbackSuccess",
        "warn": "FeedbackWarning",
        "warning": "FeedbackWarning",
        "err": "FeedbackError",
        "error": "FeedbackError",
        "info": "FeedbackInfo",
    }

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        word_wrap: bool = True,
        initially_hidden: bool = True,
    ) -> None:
        super().__init__(parent)
        self.setWordWrap(word_wrap)
        self.setObjectName("FeedbackInfo")
        if initially_hidden:
            self.hide()

    def set_feedback(self, message: str, level: str = "ok") -> None:
        """Displays a styled status message and refreshes QSS styling."""
        if not message:
            self.clear_feedback()
            return
        obj_name = self._LEVEL_OBJECT_NAMES.get(level.lower(), "FeedbackInfo")
        self.setObjectName(obj_name)
        self.setText(message)
        repolish(self)
        self.show()

    def clear_feedback(self) -> None:
        """Clears and hides the status banner."""
        self.clear()
        self.hide()


class DialogFooter(QWidget):
    """Consistent dialog footer enforcing [left widgets] [stretch] [Cancel/Close] [Primary]."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        left_widgets: Sequence[QWidget] = (),
        cancel_button: Optional[QPushButton] = None,
        primary_button: Optional[QPushButton] = None,
    ) -> None:
        super().__init__(parent)
        self._layout = QHBoxLayout(self)
        self._layout.setContentsMargins(0, 4, 0, 0)
        self._layout.setSpacing(8)

        for w in left_widgets:
            self._layout.addWidget(w)
        self._layout.addStretch()
        if cancel_button is not None:
            self._layout.addWidget(cancel_button)
        if primary_button is not None:
            self._layout.addWidget(primary_button)

    def add_left_widget(self, widget: QWidget) -> None:
        """Inserts a secondary action widget on the left side before the stretch spacer."""
        self._layout.insertWidget(max(0, self._layout.count() - 1), widget)


def confirm_destructive_action(
    parent: Optional[QWidget],
    title: str,
    message: str,
    confirm_label: str = "Delete",
    informative_text: str = "",
) -> bool:
    """Shows a confirmation dialog with an explicit destructive verb and Cancel as default."""
    box = QMessageBox(parent)
    box.setIcon(QMessageBox.Icon.Warning)
    box.setWindowTitle(title)
    box.setText(message)
    if informative_text:
        box.setInformativeText(informative_text)
    confirm_btn = box.addButton(confirm_label, QMessageBox.ButtonRole.DestructiveRole)
    cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
    box.setDefaultButton(cancel_btn)
    box.exec()
    return box.clickedButton() is confirm_btn


class _BusyWorker(QObject):
    finished = Signal(object)
    failed = Signal(object)

    def __init__(self, work_fn: Callable[[], Any]) -> None:
        super().__init__()
        self._work_fn = work_fn

    def run(self) -> None:
        try:
            res = self._work_fn()
            self.finished.emit(res)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(exc)


def run_with_busy_state(
    parent: Optional[QWidget],
    work_fn: Callable[[], Any],
    *,
    on_success: Optional[Callable[[Any], None]] = None,
    on_error: Optional[Callable[[Exception], None]] = None,
    disable_widgets: Sequence[QWidget] = (),
    busy_message: str = "",
    status_callback: Optional[Callable[[str], None]] = None,
) -> Any:
    """Executes work_fn synchronously in offscreen mode or in a QThread with WaitCursor on desktop."""
    app = QApplication.instance()
    prev_states: list[tuple[QWidget, bool]] = [(w, w.isEnabled()) for w in disable_widgets if w is not None]

    def _set_disabled(disabled: bool) -> None:
        for w, was_enabled in prev_states:
            w.setEnabled(False if disabled else was_enabled)

    if status_callback is not None and busy_message:
        status_callback(busy_message)

    if app is None or app.platformName() == "offscreen":
        _set_disabled(True)
        try:
            result = work_fn()
        except Exception as exc:  # noqa: BLE001
            _set_disabled(False)
            if on_error is not None:
                on_error(exc)
                return None
            raise
        _set_disabled(False)
        if on_success is not None:
            on_success(result)
        return result

    _set_disabled(True)
    QApplication.setOverrideCursor(Qt.CursorShape.WaitCursor)

    thread = QThread(parent)
    worker = _BusyWorker(work_fn)
    worker.moveToThread(thread)
    pair = (thread, worker)
    _ACTIVE_BUSY_WORKERS.append(pair)

    def _cleanup() -> None:
        QApplication.restoreOverrideCursor()
        _set_disabled(False)
        if pair in _ACTIVE_BUSY_WORKERS:
            _ACTIVE_BUSY_WORKERS.remove(pair)
        thread.quit()
        worker.deleteLater()
        thread.deleteLater()

    def _handle_ok(res: Any) -> None:
        _cleanup()
        if on_success is not None:
            on_success(res)

    def _handle_err(exc: Exception) -> None:
        _cleanup()
        if on_error is not None:
            on_error(exc)
        else:
            QMessageBox.warning(parent, "Operation Failed", str(exc))

    thread.started.connect(worker.run)
    worker.finished.connect(_handle_ok)
    worker.failed.connect(_handle_err)
    thread.start()
    return None


def attach_search_icon(line_edit: QLineEdit) -> QAction:
    """Attaches a leading monochrome search SVG icon to a filter QLineEdit."""
    return line_edit.addAction(
        get_icon("search", TEXT_MUTED, ICON_SM),
        QLineEdit.ActionPosition.LeadingPosition,
    )


class DCCAssetBrowserWidget(QWidget):
    """Shared importable-asset filter bar and multi-column tree for DCC import dialogs."""

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        *,
        search_placeholder: str = "Filter assets…",
    ) -> None:
        super().__init__(parent)
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(6)

        filter_row = QHBoxLayout()
        filter_row.setContentsMargins(0, 0, 0, 0)
        filter_row.setSpacing(6)

        filter_row.addWidget(QLabel("Project:"))
        self.project_combo = QComboBox()
        filter_row.addWidget(self.project_combo)

        filter_row.addWidget(QLabel("Scope:"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("All Repo & Published", "all")
        self.scope_combo.addItem("Component Repo Only", "repo")
        self.scope_combo.addItem("Published Deliverables Only", "published")
        filter_row.addWidget(self.scope_combo)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText(search_placeholder)
        self.search_edit.setClearButtonEnabled(True)
        attach_search_icon(self.search_edit)
        filter_row.addWidget(self.search_edit, 1)
        root.addLayout(filter_row)

        self.asset_tree = QTreeWidget()
        self.asset_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.asset_tree.setAlternatingRowColors(True)
        self.asset_tree.setHeaderLabels(["File / Asset", "Kind", "Component", "Version", "Details"])
        self.asset_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.asset_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.asset_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.asset_tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.asset_tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.asset_tree.setColumnWidth(4, 180)
        root.addWidget(self.asset_tree, 1)


class ShortcutsDialog(QDialog):
    """Keyboard shortcuts reference dialog for Obol Studio."""

    SHORTCUTS: list[tuple[str, str]] = [
        ("Ctrl+N", "New Project…"),
        ("Ctrl+Shift+N", "Add Component…"),
        ("Ctrl+U", "Version Up Working"),
        ("Ctrl+Shift+S", "REAPER Increment Save"),
        ("Ctrl+I", "Ingest File… (Working / Repo)"),
        ("Ctrl+P", "Publish Deliverable…"),
        ("Ctrl+D", "Open Active Working File in DCC"),
        ("Ctrl+M", "Open DCC Media Manager…"),
        ("Ctrl+S", "Save Inspector Metadata"),
        ("Ctrl+Shift+C", "Copy Metadata"),
        ("Ctrl+Shift+V", "Paste Metadata"),
        ("Ctrl+F", "Focus Active Search Filter"),
        ("F5", "Refresh & Reconcile"),
        ("Delete", "Delete Selected Item"),
        ("Ctrl+/", "Show Keyboard Shortcuts…"),
    ]

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Keyboard Shortcuts")
        self.resize(520, 440)
        apply_studio_dark_theme(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        hdr = QLabel("Obol Studio Keyboard Shortcuts")
        hdr.setObjectName("SectionHeader")
        layout.addWidget(hdr)

        self.table = QTableWidget(len(self.SHORTCUTS), 2)
        self.table.setHorizontalHeaderLabels(["Shortcut", "Action"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)

        for row_idx, (keys, desc) in enumerate(self.SHORTCUTS):
            k_item = QTableWidgetItem(keys)
            k_item.setFlags(k_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            d_item = QTableWidgetItem(desc)
            d_item.setFlags(d_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self.table.setItem(row_idx, 0, k_item)
            self.table.setItem(row_idx, 1, d_item)
        layout.addWidget(self.table, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(DialogFooter(self, primary_button=close_btn))
