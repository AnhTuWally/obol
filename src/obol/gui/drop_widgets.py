"""Reusable OS drag-and-drop widgets and tab-bar hover auto-switcher for Obol PySide6 GUI."""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QEvent, QMimeData, QObject, QPoint, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import QColor, QDrag, QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QFont, QPainter, QPaintEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QListWidget,
    QTableWidget,
    QTabBar,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QWidget,
)

from obol.gui.tokens import ACCENT_BORDER, ACCENT_LIGHT, BG_BUTTON_HOVER, TEXT_MUTED, TEXT_SECONDARY


def _paint_empty_placeholder(widget: QAbstractItemView, title: str, subtitle: str) -> None:
    """Draws centered onboarding/drop hint card inside an empty list or tree viewport."""
    if not title and not subtitle:
        return
    vp = widget.viewport()
    if vp is None:
        return
    painter = QPainter(vp)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing)
    drag_active = bool(widget.property("dragActive"))
    title_color = QColor(ACCENT_LIGHT) if drag_active else QColor(TEXT_SECONDARY)
    sub_color = QColor("#bfdbfe") if drag_active else QColor(TEXT_MUTED)
    border_color = QColor(ACCENT_BORDER) if drag_active else QColor(BG_BUTTON_HOVER)

    rect = vp.rect().adjusted(28, 24, -28, -24)
    if rect.width() > 140 and rect.height() > 90:
        pen = painter.pen()
        pen.setColor(border_color)
        pen.setWidth(1)
        pen.setStyle(Qt.PenStyle.DashLine)
        painter.setPen(pen)
        box_w = min(rect.width(), 380)
        box_h = min(rect.height(), 116)
        box_x = rect.center().x() - box_w // 2
        box_y = rect.center().y() - box_h // 2
        painter.drawRoundedRect(box_x, box_y, box_w, box_h, 8, 8)

    center_y = rect.center().y()
    title_font = QFont(widget.font())
    title_font.setBold(True)
    title_font.setPointSize(max(10, title_font.pointSize() + 1))
    painter.setFont(title_font)
    painter.setPen(title_color)
    painter.drawText(rect.left(), center_y - 22, rect.width(), 24, Qt.AlignmentFlag.AlignCenter, title)

    if subtitle:
        sub_font = QFont(widget.font())
        painter.setFont(sub_font)
        painter.setPen(sub_color)
        painter.drawText(
            rect.left() + 12,
            center_y + 4,
            max(40, rect.width() - 24),
            44,
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
            subtitle,
        )
    painter.end()


def extract_local_drop_paths(event: QDragEnterEvent | QDragMoveEvent | QDropEvent) -> list[Path]:
    """Extracts existing local file/directory paths from Qt mimeData URLs."""
    mime = event.mimeData()
    if mime is None or not mime.hasUrls():
        return []
    paths: list[Path] = []
    for url in mime.urls():
        if url.isLocalFile():
            p = Path(url.toLocalFile())
            if p.exists():
                paths.append(p)
    return paths


def set_drag_active(widget: QWidget, active: bool) -> None:
    """Toggles the 'dragActive' dynamic QSS property and repolishes the widget."""
    if widget.property("dragActive") == active:
        return
    widget.setProperty("dragActive", active)
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


class DropListWidget(QListWidget):
    """QListWidget accepting external OS file/folder drops and emitting filesDropped(paths, target_item)."""

    filesDropped = Signal(list, object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self._empty_title: str = ""
        self._empty_subtitle: str = ""

    def set_empty_placeholder(self, title: str, subtitle: str = "") -> None:
        self._empty_title = title
        self._empty_subtitle = subtitle
        if self.viewport() is not None:
            self.viewport().update()

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        total = self.topLevelItemCount()
        if total == 0:
            if self._empty_title or self._empty_subtitle:
                _paint_empty_placeholder(self, self._empty_title, self._empty_subtitle)
        elif self._filter_active:
            all_hidden = all(
                self.topLevelItem(i).isHidden()
                for i in range(total)
                if self.topLevelItem(i) is not None
            )
            if all_hidden:
                title = (
                    f"No matches for '{self._filter_query}'"
                    if self._filter_query
                    else "No matching items"
                )
                _paint_empty_placeholder(self, title, "Clear the search filter to show all items")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if extract_local_drop_paths(event):
            set_drag_active(self, True)
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if extract_local_drop_paths(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        set_drag_active(self, False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        set_drag_active(self, False)
        paths = extract_local_drop_paths(event)
        if paths:
            pos = event.position().toPoint() if hasattr(event, "position") else QPoint()
            target_item = self.itemAt(pos)
            event.acceptProposedAction()
            self.filesDropped.emit(paths, target_item)
            return
        super().dropEvent(event)


class DropTreeWidget(QTreeWidget):
    """QTreeWidget accepting external OS file/folder drops and optional OS drag-out of local files."""

    filesDropped = Signal(list, object)

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)
        self._empty_title: str = ""
        self._empty_subtitle: str = ""
        self._filter_active: bool = False
        self._filter_query: str = ""
        self._drag_path_resolver: Optional[Callable[[QTreeWidgetItem], Optional[Path]]] = None

    def set_empty_placeholder(self, title: str, subtitle: str = "") -> None:
        self._empty_title = title
        self._empty_subtitle = subtitle
        if self.viewport() is not None:
            self.viewport().update()

    def set_filter_active(self, active: bool, query: str = "") -> None:
        """Marks whether an active search filter is hiding items so empty results can be painted."""
        self._filter_active = bool(active)
        self._filter_query = query.strip()
        if self.viewport() is not None:
            self.viewport().update()

    def count(self) -> int:
        """List-compatible helper returning top-level row count."""
        return self.topLevelItemCount()

    def currentRow(self) -> int:
        """List-compatible helper returning index of selected top-level item."""
        curr = self.currentItem()
        return self.indexOfTopLevelItem(curr) if curr is not None else -1

    def setCurrentRow(self, row: int) -> None:
        """List-compatible helper selecting a top-level item by row index."""
        item = self.topLevelItem(row)
        if item is not None:
            self.setCurrentItem(item)

    def enable_file_drag_out(self, resolver: Callable[[QTreeWidgetItem], Optional[Path]]) -> None:
        """Enables dragging items out to REAPER or OS Explorer using local file URLs."""
        self._drag_path_resolver = resolver
        self.setDragEnabled(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.CopyAction)

    def build_drag_mime_data(self, items: list[QTreeWidgetItem]) -> Optional[QMimeData]:
        """Builds QMimeData containing local file URLs for the given items."""
        if not self._drag_path_resolver or not items:
            return None
        urls: list[QUrl] = []
        seen: set[str] = set()
        for it in items:
            resolved = self._drag_path_resolver(it)
            if resolved and resolved.is_file():
                s = str(resolved.resolve())
                if s not in seen:
                    seen.add(s)
                    urls.append(QUrl.fromLocalFile(s))
        if not urls:
            return None
        mime = QMimeData()
        mime.setUrls(urls)
        return mime

    def startDrag(self, supportedActions: Qt.DropAction) -> None:  # noqa: N802
        mime = self.build_drag_mime_data(self.selectedItems())
        if mime is not None:
            drag = QDrag(self)
            drag.setMimeData(mime)
            drag.exec(Qt.DropAction.CopyAction)
            return
        super().startDrag(supportedActions)

    def _is_internal_source(self, event: QDragEnterEvent | QDragMoveEvent | QDropEvent) -> bool:
        src = event.source()
        if src is None:
            return False
        if src is self:
            return True
        if isinstance(src, QWidget) and src.window() is self.window():
            return True
        return False

    def paintEvent(self, event: QPaintEvent) -> None:
        super().paintEvent(event)
        total = self.topLevelItemCount()
        if total == 0:
            if self._empty_title or self._empty_subtitle:
                _paint_empty_placeholder(self, self._empty_title, self._empty_subtitle)
        elif self._filter_active:
            all_hidden = all(
                self.topLevelItem(i).isHidden()
                for i in range(total)
                if self.topLevelItem(i) is not None
            )
            if all_hidden:
                title = (
                    f"No matches for '{self._filter_query}'"
                    if self._filter_query
                    else "No matching items"
                )
                _paint_empty_placeholder(self, title, "Clear the search filter to show all items")

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if self._is_internal_source(event):
            event.ignore()
            return
        if extract_local_drop_paths(event):
            set_drag_active(self, True)
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._is_internal_source(event):
            event.ignore()
            return
        if extract_local_drop_paths(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        set_drag_active(self, False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        set_drag_active(self, False)
        if self._is_internal_source(event):
            event.ignore()
            return
        paths = extract_local_drop_paths(event)
        if paths:
            pos = event.position().toPoint() if hasattr(event, "position") else QPoint()
            target_item = self.itemAt(pos)
            event.acceptProposedAction()
            self.filesDropped.emit(paths, target_item)
            return
        super().dropEvent(event)


class DropTableWidget(QTableWidget):
    """QTableWidget accepting external OS file drops and emitting filesDropped(paths)."""

    filesDropped = Signal(list)

    def __init__(self, rows: int = 0, cols: int = 0, parent: Optional[QWidget] = None) -> None:
        super().__init__(rows, cols, parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DropOnly)

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if extract_local_drop_paths(event):
            set_drag_active(self, True)
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if extract_local_drop_paths(event):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        set_drag_active(self, False)
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        set_drag_active(self, False)
        paths = extract_local_drop_paths(event)
        if paths:
            event.acceptProposedAction()
            self.filesDropped.emit(paths)
            return
        super().dropEvent(event)


class _TabHoverSwitchFilter(QObject):
    """Switches QTabWidget tab automatically when dragging files over a tab header."""

    def __init__(self, tab_widget: QTabWidget, delay_ms: int = 350) -> None:
        super().__init__(tab_widget)
        self.tab_widget = tab_widget
        self.tab_bar: QTabBar = tab_widget.tabBar()
        self.tab_bar.setAcceptDrops(True)
        self._pending_idx: int = -1
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(delay_ms)
        self._timer.timeout.connect(self._switch_to_pending)

    def _switch_to_pending(self) -> None:
        if 0 <= self._pending_idx < self.tab_widget.count():
            self.tab_widget.setCurrentIndex(self._pending_idx)

    def eventFilter(self, obj: QObject, event: QEvent) -> bool:
        if obj is self.tab_bar:
            etype = event.type()
            if etype in (QEvent.Type.DragEnter, QEvent.Type.DragMove):
                drag_evt = event  # type: ignore[assignment]
                if extract_local_drop_paths(drag_evt):  # type: ignore[arg-type]
                    pos = drag_evt.position().toPoint() if hasattr(drag_evt, "position") else QPoint()
                    idx = self.tab_bar.tabAt(pos)
                    if idx >= 0 and idx != self.tab_widget.currentIndex():
                        if idx != self._pending_idx:
                            self._pending_idx = idx
                            self._timer.start()
                    elif idx == self.tab_widget.currentIndex():
                        self._pending_idx = -1
                        self._timer.stop()
                    drag_evt.acceptProposedAction()  # type: ignore[attr-defined]
                    return True
            elif etype in (QEvent.Type.DragLeave, QEvent.Type.Drop):
                self._pending_idx = -1
                self._timer.stop()
        return super().eventFilter(obj, event)


def install_tab_hover_switch(tab_widget: QTabWidget, delay_ms: int = 350) -> _TabHoverSwitchFilter:
    """Installs hover-to-switch drag behavior on a QTabWidget's tab bar."""
    flt = _TabHoverSwitchFilter(tab_widget, delay_ms=delay_ms)
    tab_widget.tabBar().installEventFilter(flt)
    return flt
