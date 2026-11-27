"""Reusable dynamic key-value custom metadata editor widget for Obol PySide6 GUI."""

from __future__ import annotations

from typing import Any, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtWidgets import (
    QCompleter,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from obol.gui.icons import get_icon
from obol.gui.tokens import ICON_BTN_SIZE, ICON_SM, TEXT_MUTED, TEXT_SECONDARY

DEFAULT_CUSTOM_META_KEYS: list[str] = [
    "bpm",
    "key",
    "sample_rate",
    "fps",
    "resolution",
    "codec",
    "duration",
    "vendor",
    "pack",
    "invoice_id",
    "notes_url",
]


class CustomMetadataEditor(QWidget):
    """Dynamic inline key-value editor for custom metadata fields with key autocomplete."""

    edited = Signal()
    committed = Signal()

    def __init__(
        self,
        parent: Optional[QWidget] = None,
        hidden_keys: Optional[set[str]] = None,
        compact: bool = True,
    ) -> None:
        super().__init__(parent)
        self._hidden_keys: set[str] = set(hidden_keys) if hidden_keys is not None else {"seed_template"}
        self._preserved_hidden: dict[str, Any] = {}
        self._suggested_keys: list[str] = list(DEFAULT_CUSTOM_META_KEYS)
        self._rows: list[tuple[QWidget, QLineEdit, QLineEdit, QPushButton]] = []
        self._loading: bool = False
        self._enabled_editing: bool = True

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 2)
        root.setSpacing(4 if compact else 6)

        hdr = QHBoxLayout()
        hdr.setContentsMargins(0, 0, 0, 0)
        hdr.setSpacing(6)
        title = QLabel("Custom Fields")
        title.setObjectName("SectionHeader")
        self.add_field_btn = QPushButton("Field")
        self.add_field_btn.setObjectName("SubtleToolButton")
        self.add_field_btn.setIcon(get_icon("plus", TEXT_SECONDARY, ICON_SM))
        self.add_field_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.add_field_btn.setToolTip("Add a custom metadata field (key + value)")
        self.add_field_btn.clicked.connect(self._on_add_clicked)
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(self.add_field_btn)
        root.addLayout(hdr)

        self._rows_container = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_container)
        self._rows_layout.setContentsMargins(0, 0, 0, 0)
        self._rows_layout.setSpacing(4)
        root.addWidget(self._rows_container)

        self._empty_lbl = QLabel("No custom fields")
        self._empty_lbl.setObjectName("MutedText")
        root.addWidget(self._empty_lbl)

        self._sync_empty_label()

    def _on_add_clicked(self) -> None:
        key_edit, _ = self.add_row("", "", focus_key=True)
        key_edit.setFocus()
        self.edited.emit()

    def _sync_empty_label(self) -> None:
        self._empty_lbl.setVisible(len(self._rows) == 0)

    def _clear_rows(self) -> None:
        while self._rows:
            row_w, _, _, _ = self._rows.pop()
            self._rows_layout.removeWidget(row_w)
            row_w.deleteLater()
        self._sync_empty_label()

    def set_suggested_keys(self, keys: Optional[list[str]]) -> None:
        merged = list(dict.fromkeys([*DEFAULT_CUSTOM_META_KEYS, *(keys or [])]))
        self._suggested_keys = [k for k in merged if k and k not in self._hidden_keys]
        for _, key_edit, _, _ in self._rows:
            self._attach_completer(key_edit)

    def _attach_completer(self, key_edit: QLineEdit) -> None:
        comp = QCompleter(self._suggested_keys, key_edit)
        comp.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        comp.setFilterMode(Qt.MatchFlag.MatchContains)
        key_edit.setCompleter(comp)

    def set_data(
        self,
        data: Optional[dict[str, Any]],
        suggested_keys: Optional[list[str]] = None,
    ) -> None:
        self._loading = True
        try:
            if suggested_keys is not None:
                merged = list(dict.fromkeys([*DEFAULT_CUSTOM_META_KEYS, *suggested_keys]))
                self._suggested_keys = [k for k in merged if k and k not in self._hidden_keys]
            self._clear_rows()
            self._preserved_hidden = {}
            raw = dict(data or {})
            for k in sorted(raw.keys()):
                if k in self._hidden_keys:
                    self._preserved_hidden[k] = raw[k]
                    continue
                self.add_row(str(k), "" if raw[k] is None else str(raw[k]), focus_key=False)
            self._sync_empty_label()
            self.set_enabled_editing(self._enabled_editing)
        finally:
            self._loading = False

    def add_row(
        self,
        key: str = "",
        value: str = "",
        focus_key: bool = True,
    ) -> tuple[QLineEdit, QLineEdit]:
        row_w = QWidget(self._rows_container)
        hl = QHBoxLayout(row_w)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)

        key_edit = QLineEdit(key)
        key_edit.setPlaceholderText("Field")
        self._attach_completer(key_edit)

        val_edit = QLineEdit(value)
        val_edit.setPlaceholderText("Value")

        rm_btn = QPushButton()
        rm_btn.setObjectName("GhostButton")
        rm_btn.setIcon(get_icon("x", TEXT_MUTED, ICON_SM))
        rm_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        rm_btn.setFixedSize(ICON_BTN_SIZE, ICON_BTN_SIZE)
        rm_btn.setToolTip("Remove field")

        hl.addWidget(key_edit, 4)
        hl.addWidget(val_edit, 6)
        hl.addWidget(rm_btn)

        entry = (row_w, key_edit, val_edit, rm_btn)
        self._rows.append(entry)
        self._rows_layout.addWidget(row_w)
        self._sync_empty_label()

        key_edit.setEnabled(self._enabled_editing)
        val_edit.setEnabled(self._enabled_editing)
        rm_btn.setEnabled(self._enabled_editing)

        key_edit.textEdited.connect(self._emit_edited)
        val_edit.textEdited.connect(self._emit_edited)
        key_edit.returnPressed.connect(val_edit.setFocus)
        val_edit.returnPressed.connect(self._emit_committed)
        key_edit.editingFinished.connect(self._emit_committed)
        val_edit.editingFinished.connect(self._emit_committed)
        rm_btn.clicked.connect(lambda _=False, rw=row_w: self.remove_row(rw))

        if focus_key:
            key_edit.setFocus()
        return key_edit, val_edit

    def remove_row(self, row_widget: QWidget) -> None:
        idx = next((i for i, (rw, _, _, _) in enumerate(self._rows) if rw is row_widget), -1)
        if idx < 0:
            return
        rw, _, _, _ = self._rows.pop(idx)
        self._rows_layout.removeWidget(rw)
        rw.deleteLater()
        self._sync_empty_label()
        if not self._loading:
            self.edited.emit()
            self.committed.emit()

    def _emit_edited(self, *_args: object) -> None:
        if not self._loading:
            self.edited.emit()

    def _emit_committed(self, *_args: object) -> None:
        if not self._loading:
            self.committed.emit()

    def get_data(self, include_hidden: bool = True) -> dict[str, Any]:
        out: dict[str, Any] = dict(self._preserved_hidden) if include_hidden else {}
        for _, key_edit, val_edit, _ in self._rows:
            k = key_edit.text().strip()
            if not k or k in self._hidden_keys:
                continue
            out[k] = val_edit.text().strip()
        return out

    def row_count(self) -> int:
        return len(self._rows)

    def set_enabled_editing(self, enabled: bool) -> None:
        self._enabled_editing = bool(enabled)
        self.add_field_btn.setEnabled(self._enabled_editing)
        for _, key_edit, val_edit, rm_btn in self._rows:
            key_edit.setEnabled(self._enabled_editing)
            val_edit.setEnabled(self._enabled_editing)
            rm_btn.setEnabled(self._enabled_editing)
