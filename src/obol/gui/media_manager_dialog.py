"""Unified DCC Media Manager widget and dialog for inspecting loaded media, updates, and version-up / version-down."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from obol.core.hub import ObolHub
from obol.core.workspace import ObolWorkspace
from obol.gui.components import (
    DialogFooter,
    InlineStatusBanner,
    attach_search_icon,
    open_folder_in_os,
    repolish,
)
from obol.gui.icons import get_icon
from obol.gui.theme import apply_studio_dark_theme
from obol.gui.tokens import ERROR, ICON_SM, SUCCESS, TEXT_MUTED, TEXT_SECONDARY, WARNING
from obol.plugins.media_registry import (
    DCCMediaApplyResult,
    DCCMediaContext,
    DCCMediaRegistry,
    DCCMediaScanResult,
    DCCMediaSessionInfo,
    LoadedMediaItem,
    MediaStatus,
    MediaSwitchRequest,
    MediaVersionChoice,
)

_DOWNGRADE_COLOR = "#60a5fa"


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes > 0:
        return f"{size_bytes} B"
    return ""


class DCCMediaManagerWidget(QWidget):
    """Reusable table + controls to inspect loaded DCC media, check updates, and version up or down."""

    scanCompleted = Signal(object)
    changesApplied = Signal(object)

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        workspace: ObolWorkspace,
        registry: DCCMediaRegistry,
        *,
        dcc_key: str = "reaper",
        project_root: Optional[Path] = None,
        project_name: str = "",
        component_path: str = "",
        target_file: Optional[Path] = None,
        extra_options: Optional[dict[str, Any]] = None,
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.workspace = workspace
        self.registry = registry
        self.dcc_key = dcc_key
        self.project_root = project_root
        self.project_name = project_name
        self.component_path = component_path
        self.target_file = target_file
        self.extra_options: dict[str, Any] = dict(extra_options or {})
        self.last_scan: Optional[DCCMediaScanResult] = None
        self.last_apply: Optional[DCCMediaApplyResult] = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(8)

        # Filter & Scan Row
        filter_row = QHBoxLayout()
        filter_row.setSpacing(6)

        self.search_edit = QLineEdit()
        self.search_edit.setObjectName("FilterInput")
        self.search_edit.setPlaceholderText("Filter clips, tracks, bins, or Obol assets…")
        self.search_edit.setClearButtonEnabled(True)
        attach_search_icon(self.search_edit)
        self.search_edit.textChanged.connect(self._apply_row_filters)
        filter_row.addWidget(self.search_edit, 1)

        filter_row.addWidget(QLabel("Status:"))
        self.status_filter_combo = QComboBox()
        self.status_filter_combo.addItem("All Loaded Media", "all")
        self.status_filter_combo.addItem("Updates Available", "outdated")
        self.status_filter_combo.addItem("Pending Changes", "pending")
        self.status_filter_combo.addItem("Up to Date", "up_to_date")
        self.status_filter_combo.addItem("External / Missing", "unmanaged")
        self.status_filter_combo.currentIndexChanged.connect(self._apply_row_filters)
        filter_row.addWidget(self.status_filter_combo)

        self.show_external_chk = QCheckBox("Show external / unmanaged")
        self.show_external_chk.setToolTip("Include media files loaded from outside Obol repo/ and published/")
        self.show_external_chk.toggled.connect(lambda _checked: self.scan(silent=True))
        filter_row.addWidget(self.show_external_chk)

        self.scan_btn = QPushButton("Scan")
        self.scan_btn.setObjectName("SubtleToolButton")
        self.scan_btn.setIcon(get_icon("refresh", TEXT_SECONDARY, ICON_SM))
        self.scan_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.scan_btn.clicked.connect(lambda: self.scan(silent=False))
        filter_row.addWidget(self.scan_btn)
        layout.addLayout(filter_row)

        self.summary_label = QLabel("")
        self.summary_label.setObjectName("MutedText")
        layout.addWidget(self.summary_label)

        # Loaded Media Table
        self.media_table = QTableWidget(0, 8)
        self.media_table.setAlternatingRowColors(True)
        self.media_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.media_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.media_table.setHorizontalHeaderLabels(
            ["Apply", "Location", "Clip / Take", "Type", "Obol Asset", "Loaded", "Target Version", "Status"]
        )
        hdr = self.media_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.media_table.setColumnWidth(1, 130)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.media_table.setColumnWidth(2, 165)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        self.media_table.itemChanged.connect(self._on_table_item_changed)
        self.media_table.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.media_table.customContextMenuRequested.connect(self._on_table_context_menu)
        layout.addWidget(self.media_table, 1)

        # REAPER-specific live options
        self.reaper_opts_widget = QWidget()
        ro_layout = QHBoxLayout(self.reaper_opts_widget)
        ro_layout.setContentsMargins(0, 0, 0, 0)
        ro_layout.setSpacing(8)
        ro_layout.addWidget(QLabel("Update Mode:"))
        self.update_mode_combo = QComboBox()
        self.update_mode_combo.addItem("Replace Take Source (In-Place)", "replace_source")
        self.update_mode_combo.addItem("Add as New Active Take (A/B Compare)", "add_take")
        ro_layout.addWidget(self.update_mode_combo)

        ro_layout.addWidget(QLabel("MIDI Storage:"))
        self.midi_mode_combo = QComboBox()
        self.midi_mode_combo.addItem("Preserve MIDI Mode (In-Project / File Ref)", "preserve")
        self.midi_mode_combo.addItem("Convert to In-Project MIDI", "in_project")
        self.midi_mode_combo.addItem("Link as External .mid File", "file_ref")
        ro_layout.addWidget(self.midi_mode_combo)

        self.match_length_chk = QCheckBox("Auto-resize untrimmed items")
        self.match_length_chk.setChecked(True)
        ro_layout.addWidget(self.match_length_chk)
        ro_layout.addStretch()
        layout.addWidget(self.reaper_opts_widget)

        self.feedback_banner = InlineStatusBanner(self)
        layout.addWidget(self.feedback_banner)

        # Action Row (Selection + Version Down / Up / Latest + Apply)
        btn_row = QHBoxLayout()
        btn_row.setSpacing(6)
        self.select_outdated_btn = QPushButton("Select Outdated")
        self.select_outdated_btn.setObjectName("SubtleToolButton")
        self.select_outdated_btn.clicked.connect(lambda: self._set_selection_mode(outdated_only=True))
        self.select_none_btn = QPushButton("Select None")
        self.select_none_btn.setObjectName("SubtleToolButton")
        self.select_none_btn.clicked.connect(lambda: self._set_selection_mode(outdated_only=False))

        self.version_down_btn = QPushButton("Version Down (v-1)")
        self.version_down_btn.setObjectName("SubtleToolButton")
        self.version_down_btn.setToolTip("Step selected or checked asset(s) back one version")
        self.version_down_btn.clicked.connect(lambda: self.step_versions(-1))

        self.version_up_btn = QPushButton("Version Up (v+1)")
        self.version_up_btn.setObjectName("SubtleToolButton")
        self.version_up_btn.setToolTip("Step selected or checked asset(s) forward one version")
        self.version_up_btn.clicked.connect(lambda: self.step_versions(1))

        self.set_latest_btn = QPushButton("Set Latest")
        self.set_latest_btn.setObjectName("SubtleToolButton")
        self.set_latest_btn.setToolTip("Point selected or checked asset(s) to their latest version")
        self.set_latest_btn.clicked.connect(self.set_selected_to_latest)

        btn_row.addWidget(self.select_outdated_btn)
        btn_row.addWidget(self.select_none_btn)
        btn_row.addWidget(self.version_down_btn)
        btn_row.addWidget(self.version_up_btn)
        btn_row.addWidget(self.set_latest_btn)
        btn_row.addStretch()

        self.apply_btn = QPushButton("Apply Version Changes")
        self.apply_btn.setObjectName("PrimaryButton")
        self.apply_btn.setEnabled(False)
        self.apply_btn.clicked.connect(self.apply_changes)
        btn_row.addWidget(self.apply_btn)
        layout.addLayout(btn_row)

        self._sync_dcc_option_visibility()

    def build_context(self) -> DCCMediaContext:
        opts = dict(self.extra_options)
        if self.dcc_key == "reaper":
            opts.setdefault("update_mode", str(self.update_mode_combo.currentData() or "replace_source"))
            opts.setdefault("midi_mode", str(self.midi_mode_combo.currentData() or "preserve"))
            opts.setdefault("match_length", "auto" if self.match_length_chk.isChecked() else "never")
        return DCCMediaContext(
            hub=self.hub,
            workspace=self.workspace,
            project_root=self.project_root,
            project_name=self.project_name,
            component_path=self.component_path,
            target_file=self.target_file,
            include_external=self.show_external_chk.isChecked(),
            options=opts,
        )

    def set_context(
        self,
        *,
        dcc_key: Optional[str] = None,
        project_root: Optional[Path] = None,
        project_name: Optional[str] = None,
        component_path: Optional[str] = None,
        target_file: Optional[Path] = None,
        clear_target_file: bool = False,
        extra_options: Optional[dict[str, Any]] = None,
    ) -> None:
        if dcc_key is not None:
            self.dcc_key = dcc_key
        if project_root is not None:
            self.project_root = project_root
        if project_name is not None:
            self.project_name = project_name
        if component_path is not None:
            self.component_path = component_path
        if clear_target_file:
            self.target_file = None
        elif target_file is not None:
            self.target_file = target_file
        if extra_options is not None:
            self.extra_options = dict(extra_options)
        self._sync_dcc_option_visibility()

    def _sync_dcc_option_visibility(self, session: Optional[DCCMediaSessionInfo] = None) -> None:
        show_reaper = self.dcc_key == "reaper" and (session is None or session.mode == "live")
        self.reaper_opts_widget.setVisible(show_reaper)

    def scan(self, silent: bool = False, *, preserve_banner: bool = False) -> DCCMediaScanResult:
        if not preserve_banner:
            self.feedback_banner.clear_feedback()
        ctx = self.build_context()
        result = self.registry.scan(self.dcc_key, ctx)
        self.last_scan = result
        self._sync_dcc_option_visibility(result.session)
        self._populate_table(result)
        self.scanCompleted.emit(result)
        if result.error:
            self.feedback_banner.set_feedback(result.error, "err")
            if not silent:
                QMessageBox.warning(self, "Media Scan Failed", result.error)
        return result

    def _populate_table(self, result: DCCMediaScanResult) -> None:
        self.media_table.blockSignals(True)
        self.media_table.setRowCount(0)

        for item in result.items:
            row_idx = self.media_table.rowCount()
            self.media_table.insertRow(row_idx)

            chk = QTableWidgetItem()
            if item.tracked and item.can_switch_version:
                chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                chk.setCheckState(Qt.CheckState.Checked if item.update_available else Qt.CheckState.Unchecked)
            else:
                chk.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            chk.setData(Qt.ItemDataRole.UserRole, item)

            loc_it = QTableWidgetItem(item.location or "—")
            name_it = QTableWidgetItem(item.display_name)
            if item.file_path:
                name_it.setToolTip(str(item.file_path))
            type_it = QTableWidgetItem(item.media_type)
            asset_text = item.asset_label
            if item.project_name and self.project_name and item.project_name != self.project_name:
                asset_text = f"[{item.project_name}] {asset_text}"
            asset_it = QTableWidgetItem(asset_text)
            if not item.tracked:
                asset_it.setForeground(QColor(TEXT_MUTED))

            loaded_txt = f"v{item.current_version:03d}" if item.tracked and item.current_version > 0 else "—"
            loaded_it = QTableWidgetItem(loaded_txt)

            self.media_table.setItem(row_idx, 0, chk)
            self.media_table.setItem(row_idx, 1, loc_it)
            self.media_table.setItem(row_idx, 2, name_it)
            self.media_table.setItem(row_idx, 3, type_it)
            self.media_table.setItem(row_idx, 4, asset_it)
            self.media_table.setItem(row_idx, 5, loaded_it)

            if item.tracked and item.available_versions:
                ver_combo = QComboBox()
                default_ver = item.latest_version if item.update_available else item.current_version
                sel_idx = 0
                for c_idx, choice in enumerate(item.available_versions):
                    lbl = f"{choice.version_label}{' ★' if choice.is_latest else ''}"
                    ver_combo.addItem(lbl, choice)
                    tip_parts = [choice.file_name]
                    sz = _format_size(choice.size_bytes)
                    if sz:
                        tip_parts.append(sz)
                    if choice.comment:
                        tip_parts.append(choice.comment)
                    ver_combo.setItemData(c_idx, " — ".join(tip_parts), Qt.ItemDataRole.ToolTipRole)
                    if choice.version == default_ver:
                        sel_idx = c_idx
                ver_combo.setCurrentIndex(sel_idx)
                ver_combo.currentIndexChanged.connect(lambda _idx, r=row_idx: self._on_target_version_changed(r))
                self.media_table.setCellWidget(row_idx, 6, ver_combo)
            else:
                na_it = QTableWidgetItem("—")
                na_it.setForeground(QColor(TEXT_MUTED))
                self.media_table.setItem(row_idx, 6, na_it)

            status_it = QTableWidgetItem()
            self.media_table.setItem(row_idx, 7, status_it)
            self._update_row_status_cell(row_idx)

        self.media_table.blockSignals(False)
        self._apply_row_filters()
        self._refresh_summary_and_apply_btn()

    def _row_item(self, row_idx: int) -> Optional[LoadedMediaItem]:
        chk = self.media_table.item(row_idx, 0)
        if chk is None:
            return None
        val = chk.data(Qt.ItemDataRole.UserRole)
        return val if isinstance(val, LoadedMediaItem) else None

    def _row_choice(self, row_idx: int) -> Optional[MediaVersionChoice]:
        combo = self.media_table.cellWidget(row_idx, 6)
        if not isinstance(combo, QComboBox):
            return None
        val = combo.currentData()
        return val if isinstance(val, MediaVersionChoice) else None

    def _update_row_status_cell(self, row_idx: int) -> None:
        item = self._row_item(row_idx)
        status_it = self.media_table.item(row_idx, 7)
        if item is None or status_it is None:
            return

        if not item.tracked:
            if item.status == MediaStatus.MISSING:
                status_it.setText("Missing")
                status_it.setForeground(QColor(ERROR))
            else:
                status_it.setText("External")
                status_it.setForeground(QColor(TEXT_MUTED))
            return

        choice = self._row_choice(row_idx)
        if choice is None:
            if item.status == MediaStatus.MISSING:
                status_it.setText("Missing")
                status_it.setForeground(QColor(ERROR))
            else:
                status_it.setText("Up to Date")
                status_it.setForeground(QColor(SUCCESS))
            return

        targ_v = choice.version
        curr_v = item.current_version
        if targ_v > curr_v:
            if item.update_available and targ_v == item.latest_version:
                status_it.setText(f"Update Available (→ v{targ_v:03d})")
            else:
                status_it.setText(f"Upgrade → v{targ_v:03d}")
            status_it.setForeground(QColor(WARNING))
        elif targ_v < curr_v:
            status_it.setText(f"Version Down → v{targ_v:03d}")
            status_it.setForeground(QColor(_DOWNGRADE_COLOR))
        elif item.status == MediaStatus.MISSING:
            status_it.setText("Missing")
            status_it.setForeground(QColor(ERROR))
        else:
            status_it.setText("Up to Date")
            status_it.setForeground(QColor(SUCCESS))

    def _on_target_version_changed(self, row_idx: int) -> None:
        item = self._row_item(row_idx)
        choice = self._row_choice(row_idx)
        chk = self.media_table.item(row_idx, 0)
        if item is None or choice is None or chk is None:
            return
        self.media_table.blockSignals(True)
        differs = choice.version != item.current_version or (
            item.file_path is not None and choice.file_path != item.file_path
        )
        chk.setCheckState(Qt.CheckState.Checked if differs else Qt.CheckState.Unchecked)
        self._update_row_status_cell(row_idx)
        self.media_table.blockSignals(False)
        self._apply_row_filters()
        self._refresh_summary_and_apply_btn()

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() == 0:
            self._apply_row_filters()
            self._refresh_summary_and_apply_btn()

    def _apply_row_filters(self) -> None:
        needle = self.search_edit.text().strip().lower()
        mode = str(self.status_filter_combo.currentData() or "all")

        for row_idx in range(self.media_table.rowCount()):
            item = self._row_item(row_idx)
            if item is None:
                continue
            choice = self._row_choice(row_idx)
            chk = self.media_table.item(row_idx, 0)
            is_checked = bool(chk and chk.checkState() == Qt.CheckState.Checked)
            is_pending = bool(
                is_checked
                and item.tracked
                and choice is not None
                and (choice.version != item.current_version or (item.file_path and choice.file_path != item.file_path))
            )

            match_status = True
            if mode == "outdated":
                match_status = item.status == MediaStatus.UPDATE_AVAILABLE
            elif mode == "pending":
                match_status = is_pending
            elif mode == "up_to_date":
                match_status = item.status == MediaStatus.UP_TO_DATE
            elif mode == "unmanaged":
                match_status = (not item.tracked) or item.status == MediaStatus.MISSING

            hay = " ".join([
                item.display_name,
                item.location,
                item.asset_label,
                item.project_name,
                item.media_type,
                str(item.file_path or ""),
            ]).lower()
            match_text = (not needle) or (needle in hay)
            self.media_table.setRowHidden(row_idx, not (match_status and match_text))

    def _refresh_summary_and_apply_btn(self) -> None:
        scan = self.last_scan
        if scan is None:
            self.summary_label.setText("")
            self.apply_btn.setEnabled(False)
            return

        sess = scan.session
        if sess.mode == "offline" and not scan.items:
            self.summary_label.setText(
                f"{sess.display_name} is offline — open a session or select a working project file to scan."
            )
        else:
            parts = [
                f"{scan.tracked_count} tracked",
                f"{scan.outdated_count} update(s) available",
            ]
            if scan.external_count:
                parts.append(f"{scan.external_count} external")
            if scan.missing_count:
                parts.append(f"{scan.missing_count} missing")
            prefix = f"{sess.display_name} ({sess.session_label})" if sess.session_label else sess.display_name
            self.summary_label.setText(f"{prefix}: {', '.join(parts)}.")

        switches = self.pending_switches()
        up_cnt = sum(1 for sw in switches if sw.is_upgrade)
        down_cnt = sum(1 for sw in switches if sw.is_downgrade)
        self.apply_btn.setEnabled(bool(switches))
        if switches:
            detail_parts: list[str] = []
            if up_cnt:
                detail_parts.append(f"{up_cnt} ↑")
            if down_cnt:
                detail_parts.append(f"{down_cnt} ↓")
            suffix = f" ({', '.join(detail_parts)})" if detail_parts else f" ({len(switches)})"
            self.apply_btn.setText(f"Apply Changes{suffix}")
        else:
            self.apply_btn.setText("Apply Version Changes")

    def pending_switches(self) -> list[MediaSwitchRequest]:
        ctx = self.build_context()
        switches: list[MediaSwitchRequest] = []
        for row_idx in range(self.media_table.rowCount()):
            chk = self.media_table.item(row_idx, 0)
            if chk is None or chk.checkState() != Qt.CheckState.Checked:
                continue
            item = self._row_item(row_idx)
            choice = self._row_choice(row_idx)
            if item is None or choice is None or not item.tracked:
                continue
            sw = self.registry.build_switch_request(item, choice.version, ctx)
            if sw is not None:
                switches.append(sw)
        return switches

    def _set_selection_mode(self, *, outdated_only: bool) -> None:
        self.media_table.blockSignals(True)
        for row_idx in range(self.media_table.rowCount()):
            chk = self.media_table.item(row_idx, 0)
            item = self._row_item(row_idx)
            choice = self._row_choice(row_idx)
            if chk is None or item is None or not item.tracked or not item.can_switch_version:
                continue
            if outdated_only and choice is not None:
                differs = choice.version != item.current_version
                chk.setCheckState(Qt.CheckState.Checked if differs else Qt.CheckState.Unchecked)
            else:
                chk.setCheckState(Qt.CheckState.Unchecked)
        self.media_table.blockSignals(False)
        self._apply_row_filters()
        self._refresh_summary_and_apply_btn()

    def _target_rows_for_batch(self) -> list[int]:
        sel_model = self.media_table.selectionModel()
        selected_rows = sorted({idx.row() for idx in sel_model.selectedRows()}) if sel_model else []
        if not selected_rows:
            selected_rows = sorted({it.row() for it in self.media_table.selectedItems()})
        if selected_rows:
            return [r for r in selected_rows if not self.media_table.isRowHidden(r)]

        checked_rows: list[int] = []
        visible_tracked: list[int] = []
        for row_idx in range(self.media_table.rowCount()):
            if self.media_table.isRowHidden(row_idx):
                continue
            item = self._row_item(row_idx)
            if item is None or not item.tracked or not item.available_versions:
                continue
            visible_tracked.append(row_idx)
            chk = self.media_table.item(row_idx, 0)
            if chk is not None and chk.checkState() == Qt.CheckState.Checked:
                checked_rows.append(row_idx)
        return checked_rows or visible_tracked

    def _set_row_target_version(self, row_idx: int, target_version: int) -> bool:
        combo = self.media_table.cellWidget(row_idx, 6)
        if not isinstance(combo, QComboBox):
            return False
        for i in range(combo.count()):
            ch = combo.itemData(i)
            if isinstance(ch, MediaVersionChoice) and ch.version == target_version:
                combo.setCurrentIndex(i)
                return True
        return False

    def step_versions(self, delta: int) -> int:
        """Steps target version by `delta` (-1 for Version Down, +1 for Version Up) on targeted rows."""
        changed = 0
        for row_idx in self._target_rows_for_batch():
            combo = self.media_table.cellWidget(row_idx, 6)
            if not isinstance(combo, QComboBox) or combo.count() == 0:
                continue
            new_idx = max(0, min(combo.count() - 1, combo.currentIndex() + delta))
            if new_idx != combo.currentIndex():
                combo.setCurrentIndex(new_idx)
                changed += 1
        if changed == 0 and delta < 0:
            self.feedback_banner.set_feedback("No older version available for the selected item(s).", "info")
        elif changed == 0 and delta > 0:
            self.feedback_banner.set_feedback("Selected item(s) are already at the latest version.", "info")
        return changed

    def set_selected_to_latest(self) -> int:
        changed = 0
        for row_idx in self._target_rows_for_batch():
            item = self._row_item(row_idx)
            if item is None or not item.tracked:
                continue
            if self._set_row_target_version(row_idx, item.latest_version):
                changed += 1
        return changed

    def _on_table_context_menu(self, pos: Any) -> None:
        item_at = self.media_table.itemAt(pos)
        if item_at is None:
            return
        row_idx = item_at.row()
        item = self._row_item(row_idx)
        if item is None:
            return

        menu = QMenu(self)
        if item.tracked and item.available_versions:
            act_latest = menu.addAction(f"Switch to Latest (v{item.latest_version:03d})")
            act_latest.triggered.connect(lambda: self._set_row_target_version(row_idx, item.latest_version))

            prev_v = item.prev_version()
            if prev_v is not None:
                act_down = menu.addAction(f"Version Down → {prev_v.version_label}")
                act_down.triggered.connect(lambda: self._set_row_target_version(row_idx, prev_v.version))

            next_v = item.next_version()
            if next_v is not None:
                act_up = menu.addAction(f"Version Up → {next_v.version_label}")
                act_up.triggered.connect(lambda: self._set_row_target_version(row_idx, next_v.version))

            act_reset = menu.addAction(f"Reset to Loaded (v{item.current_version:03d})")
            act_reset.triggered.connect(lambda: self._set_row_target_version(row_idx, item.current_version))
            menu.addSeparator()

        if item.file_path is not None:
            act_open = menu.addAction("Open Loaded File Folder")
            act_open.triggered.connect(lambda: open_folder_in_os(item.file_path.parent) if item.file_path else None)

        if not menu.isEmpty():
            menu.exec(self.media_table.viewport().mapToGlobal(pos))

    def apply_changes(self) -> Optional[DCCMediaApplyResult]:
        switches = self.pending_switches()
        if not switches:
            self.feedback_banner.set_feedback("No checked items have a changed target version.", "info")
            return None

        ctx = self.build_context()
        res = self.registry.apply_switches(self.dcc_key, switches, ctx)
        self.last_apply = res
        if res.ok:
            if not ctx.options.get("return_plan_only"):
                self.scan(silent=True, preserve_banner=True)
            self.feedback_banner.set_feedback(res.message, "ok")
        else:
            self.feedback_banner.set_feedback(res.message, "err")
        self.changesApplied.emit(res)
        return res


class DCCMediaManagerDialog(QDialog):
    """Unified DCC Media Manager dialog accessible from the DCC Exchange Bar (Ctrl+M)."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        workspace: ObolWorkspace,
        registry: DCCMediaRegistry,
        *,
        initial_dcc: str = "",
        project_root: Optional[Path] = None,
        project_name: str = "",
        component_path: str = "",
        target_file: Optional[Path] = None,
        auto_scan: bool = True,
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.workspace = workspace
        self.registry = registry
        self._project_root = project_root
        self._project_name = project_name
        self._component_path = component_path
        self._initial_target_file = target_file

        self.setWindowTitle("Obol — DCC Media Manager")
        self.resize(1020, 620)
        apply_studio_dark_theme(self)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        top_bar = QHBoxLayout()
        top_bar.setSpacing(8)
        top_bar.addWidget(QLabel("Target DCC:"))
        self.dcc_combo = QComboBox()
        self.dcc_combo.setMinimumWidth(180)
        top_bar.addWidget(self.dcc_combo)

        top_bar.addWidget(QLabel("Session / File:"))
        self.target_file_combo = QComboBox()
        self.target_file_combo.setMinimumWidth(260)
        top_bar.addWidget(self.target_file_combo, 1)

        self.status_pill = QLabel("Offline")
        self.status_pill.setObjectName("StatusPillLocal")
        top_bar.addWidget(self.status_pill)
        layout.addLayout(top_bar)

        self.manager_widget = DCCMediaManagerWidget(
            self,
            hub=self.hub,
            workspace=self.workspace,
            registry=self.registry,
            dcc_key=initial_dcc or "reaper",
            project_root=project_root,
            project_name=project_name,
            component_path=component_path,
            target_file=target_file,
        )
        layout.addWidget(self.manager_widget, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(DialogFooter(self, primary_button=close_btn))

        self._populate_dcc_combo(initial_dcc=initial_dcc)
        self.dcc_combo.currentIndexChanged.connect(self._on_dcc_changed)
        self.target_file_combo.currentIndexChanged.connect(self._on_target_file_changed)

        self._reload_target_files(initial_file=target_file)
        if auto_scan:
            self.scan_now(silent=True)

    def _base_context(self, dcc_key: str) -> DCCMediaContext:
        return DCCMediaContext(
            hub=self.hub,
            workspace=self.workspace,
            project_root=self._project_root,
            project_name=self._project_name,
            component_path=self._component_path,
        )

    def _populate_dcc_combo(self, initial_dcc: str = "") -> None:
        self.dcc_combo.blockSignals(True)
        self.dcc_combo.clear()
        live_idx = -1
        wanted_idx = -1
        for idx, adapter in enumerate(self.registry.list_adapters()):
            sess = adapter.get_session_info(self._base_context(adapter.key))
            if sess.mode == "live":
                lbl = f"{adapter.display_name} ● (Live)"
                if live_idx < 0:
                    live_idx = idx
            elif sess.mode == "offline_project":
                lbl = f"{adapter.display_name} (Project File)"
            else:
                lbl = f"{adapter.display_name} (Offline)"
            self.dcc_combo.addItem(lbl, adapter.key)
            if initial_dcc and adapter.key == initial_dcc:
                wanted_idx = idx
        if wanted_idx >= 0:
            self.dcc_combo.setCurrentIndex(wanted_idx)
        elif live_idx >= 0:
            self.dcc_combo.setCurrentIndex(live_idx)
        self.dcc_combo.blockSignals(False)

    def _reload_target_files(self, initial_file: Optional[Path] = None) -> None:
        dcc_key = str(self.dcc_combo.currentData() or "reaper")
        adapter = self.registry.get(dcc_key)
        self.target_file_combo.blockSignals(True)
        self.target_file_combo.clear()
        if adapter is None:
            self.target_file_combo.blockSignals(False)
            return

        sess = adapter.get_session_info(self._base_context(dcc_key))
        if sess.mode == "live":
            live_lbl = f"(Active Live Session — {sess.session_label})" if sess.session_label else "(Active Live Session)"
            self.target_file_combo.addItem(live_lbl, "")
        for label, fpath in sess.available_targets:
            self.target_file_combo.addItem(label, fpath)
        if self.target_file_combo.count() == 0:
            self.target_file_combo.addItem("(No local session or project file)", "")

        if initial_file is not None:
            norm_target = str(Path(initial_file).resolve())
            idx = self.target_file_combo.findData(norm_target)
            if idx >= 0:
                self.target_file_combo.setCurrentIndex(idx)
        self.target_file_combo.blockSignals(False)
        self._sync_widget_context()

    def _sync_widget_context(self) -> None:
        dcc_key = str(self.dcc_combo.currentData() or "reaper")
        raw_tf = str(self.target_file_combo.currentData() or "").strip()
        target_p = Path(raw_tf) if raw_tf else None
        self.manager_widget.set_context(
            dcc_key=dcc_key,
            project_root=self._project_root,
            project_name=self._project_name,
            component_path=self._component_path,
            target_file=target_p,
            clear_target_file=(target_p is None),
        )
        adapter = self.registry.get(dcc_key)
        if adapter is not None:
            sess = adapter.get_session_info(self.manager_widget.build_context())
            self._update_status_pill(sess)

    def _update_status_pill(self, sess: DCCMediaSessionInfo) -> None:
        if sess.mode in ("live", "script_context"):
            self.status_pill.setText(f"{sess.display_name} Live ({sess.session_label})")
            self.status_pill.setObjectName("StatusPillOnline")
        elif sess.mode == "offline_project":
            self.status_pill.setText(f"Project: {sess.session_label}")
            self.status_pill.setObjectName("StatusPillLocal")
        else:
            self.status_pill.setText(f"{sess.display_name} Offline")
            self.status_pill.setObjectName("StatusPillOffline")
        repolish(self.status_pill)

    def _on_dcc_changed(self, _idx: int) -> None:
        self._reload_target_files()
        self.scan_now(silent=True)

    def _on_target_file_changed(self, _idx: int) -> None:
        self._sync_widget_context()
        self.scan_now(silent=True)

    def scan_now(self, silent: bool = True) -> DCCMediaScanResult:
        res = self.manager_widget.scan(silent=silent)
        self._update_status_pill(res.session)
        return res
