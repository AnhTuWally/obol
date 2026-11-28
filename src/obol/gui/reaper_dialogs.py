"""Focused PySide6 companion dialogs for REAPER DAW integration (Open, Import/Update, Hybrid Export/Publish, Setup)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from obol.core.hub import ObolHub
from obol.core.models import VersionBundle
from obol.core.storage import discover_project_components, load_component_snapshot
from obol.core.workspace import ObolWorkspace, WorkingFileContext
from obol.gui.components import (
    DCCAssetBrowserWidget,
    DialogFooter,
    confirm_destructive_action,
    repolish,
    run_with_busy_state,
)
from obol.gui.theme import apply_studio_dark_theme
from obol.plugins.base import launch_file_with_profile
from obol.plugins.reaper import ReaperPlugin
from obol.plugins.reaper_bridge import (
    ReaperBridgeClient,
    detect_reaper_executable,
    detect_reaper_resource_dir,
    parse_reaper_render_presets,
)
from obol.plugins.reaper_installer import ReaperInstaller
from obol.plugins.reaper_scripts import OBOL_ACTION_SPECS


_repolish = repolish


class ReaperOpenDialog(QDialog):
    """Browse Obol projects/components/versions and open directly in REAPER."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        workspace: ObolWorkspace,
        bridge: ReaperBridgeClient,
        active_rpp: str = "",
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.workspace = workspace
        self.bridge = bridge
        self.opened_path: Optional[Path] = None

        self.setWindowTitle("Obol — Open Working .rpp in REAPER")
        self.resize(780, 500)

        ctx = self.workspace.resolve_working_file_context(Path(active_rpp)) if active_rpp else None

        layout = QVBoxLayout(self)
        top_row = QHBoxLayout()
        top_row.addWidget(QLabel("Project:"))
        self.project_combo = QComboBox()
        for p in self.hub.list_projects():
            self.project_combo.addItem(p.name, p.local_path)
        top_row.addWidget(self.project_combo, 1)

        self.bridge_pill = QLabel()
        self._update_bridge_pill()
        top_row.addWidget(self.bridge_pill)
        layout.addLayout(top_row)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        self.comp_tree = QTreeWidget()
        self.comp_tree.setHeaderLabels(["Component", "Software"])
        self.comp_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.comp_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        splitter.addWidget(self.comp_tree)

        self.ver_tree = QTreeWidget()
        self.ver_tree.setRootIsDecorated(False)
        self.ver_tree.setAlternatingRowColors(True)
        self.ver_tree.setHeaderLabels(["Version", "File", "Created", "Note"])
        self.ver_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.ver_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.ver_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.ver_tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.ver_tree.setColumnWidth(1, 180)
        splitter.addWidget(self.ver_tree)
        splitter.setSizes([250, 510])
        layout.addWidget(splitter, 1)

        opts_row = QHBoxLayout()
        self.new_tab_chk = QCheckBox("Open in new REAPER project tab")
        opts_row.addWidget(self.new_tab_chk)
        opts_row.addStretch()
        layout.addLayout(opts_row)

        self.open_btn = QPushButton("Open in REAPER")
        self.open_btn.setObjectName("PrimaryButton")
        self.open_btn.clicked.connect(self._on_open_selected)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(
            DialogFooter(
                self,
                cancel_button=cancel_btn,
                primary_button=self.open_btn,
            )
        )

        self.project_combo.currentIndexChanged.connect(self._reload_components)
        self.comp_tree.currentItemChanged.connect(self._reload_versions)
        self.ver_tree.itemDoubleClicked.connect(lambda _it, _c: self._on_open_selected())

        # Pre-select context or last active project
        target_proj = ctx.project_name if ctx else self.hub.config.last_active_project
        if target_proj:
            idx = self.project_combo.findText(target_proj)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)
        self._reload_components(preselect_comp=(ctx.component_path if ctx else ""), preselect_ver=(ctx.working_version if ctx else None))

    def _update_bridge_pill(self) -> None:
        st = self.bridge.get_state()
        if st.connected:
            self.bridge_pill.setText(f"REAPER Connected ({st.reaper_version or 'live'})")
            self.bridge_pill.setObjectName("StatusPillOnline")
        else:
            self.bridge_pill.setText("REAPER Bridge Offline (OS launch fallback)")
            self.bridge_pill.setObjectName("StatusPillLocal")
        _repolish(self.bridge_pill)

    def _current_project_root(self) -> Optional[Path]:
        raw = self.project_combo.currentData()
        return Path(str(raw)) if raw else None

    def _reload_components(self, _idx: int = 0, preselect_comp: str = "", preselect_ver: Optional[int] = None) -> None:
        self.comp_tree.clear()
        self.ver_tree.clear()
        proot = self._current_project_root()
        if not proot or not proot.is_dir():
            return
        first_item: Optional[QTreeWidgetItem] = None
        target_item: Optional[QTreeWidgetItem] = None
        for comp in discover_project_components(proot):
            snap = load_component_snapshot(proot, comp)
            it = QTreeWidgetItem(self.comp_tree, [comp, snap.software])
            it.setData(0, Qt.ItemDataRole.UserRole, comp)
            if first_item is None:
                first_item = it
            if preselect_comp and comp == preselect_comp:
                target_item = it
        chosen = target_item or first_item
        if chosen is not None:
            self.comp_tree.setCurrentItem(chosen)
            self._reload_versions(chosen, None, preselect_ver=preselect_ver)

    def _reload_versions(
        self,
        current: Optional[QTreeWidgetItem],
        _prev: Optional[QTreeWidgetItem] = None,
        preselect_ver: Optional[int] = None,
    ) -> None:
        self.ver_tree.clear()
        proot = self._current_project_root()
        if not proot or current is None:
            return
        comp = str(current.data(0, Qt.ItemDataRole.UserRole) or "")
        if not comp:
            return
        snap = load_component_snapshot(proot, comp)
        target_item: Optional[QTreeWidgetItem] = None
        for w in reversed(snap.working_versions):
            it = QTreeWidgetItem(
                self.ver_tree,
                [
                    w.version_label,
                    Path(w.primary_file).name if w.primary_file else "—",
                    w.created_at.replace("T", " ")[:16] if w.created_at else "—",
                    w.comment,
                ],
            )
            it.setData(0, Qt.ItemDataRole.UserRole, w.version)
            it.setData(1, Qt.ItemDataRole.UserRole, str((proot / comp / w.primary_file).resolve()) if w.primary_file else "")
            if target_item is None or (preselect_ver is not None and w.version == preselect_ver):
                target_item = it
        if target_item is not None:
            self.ver_tree.setCurrentItem(target_item)

    def _open_rpp_target(self, target_file: Path) -> None:
        if not target_file.is_file():
            QMessageBox.warning(self, "Open in REAPER", f"Working file does not exist on disk:\n{target_file}")
            return
        if self.bridge.is_connected():
            try:
                self.bridge.open_project(target_file, new_tab=self.new_tab_chk.isChecked())
                self.opened_path = target_file
                self.accept()
                return
            except Exception as exc:
                QMessageBox.warning(self, "REAPER Bridge Error", str(exc))
                return
        prof = self.hub.get_software_profile("reaper")
        launch_file_with_profile(target_file, prof)
        self.opened_path = target_file
        self.accept()

    def _on_open_selected(self) -> None:
        curr = self.ver_tree.currentItem()
        if curr is None:
            return
        raw_path = str(curr.data(1, Qt.ItemDataRole.UserRole) or "")
        if raw_path:
            self._open_rpp_target(Path(raw_path))


class ReaperImportDialog(QDialog):
    """Browse repo/ & published/ deliverables to import into REAPER, and update outdated MIDI/media takes in the active session."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        workspace: ObolWorkspace,
        bridge: ReaperBridgeClient,
        active_rpp: str = "",
        initial_tab: int = 0,
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.workspace = workspace
        self.bridge = bridge
        self.active_rpp = active_rpp
        self._offline_rpp_mode = False
        self._ctx: Optional[WorkingFileContext] = (
            self.workspace.resolve_working_file_context(Path(active_rpp)) if active_rpp else None
        )

        self.setWindowTitle("Obol — Import && Update MIDI / Media in REAPER")
        self.resize(940, 590)

        layout = QVBoxLayout(self)

        hdr = QHBoxLayout()
        ctx_text = (
            f"Active Session: <b>{self._ctx.project_name} / {self._ctx.component_path}</b> (working/v{self._ctx.working_version:03d})"
            if self._ctx
            else "Active Session: Outside an Obol working folder"
        )
        self.ctx_label = QLabel(ctx_text)
        hdr.addWidget(self.ctx_label, 1)
        self.bridge_pill = QLabel()
        self._refresh_bridge_pill()
        hdr.addWidget(self.bridge_pill)
        layout.addLayout(hdr)

        self.tabs = QTabWidget()

        # Tab 1: Browse & Import
        tab_browse = QWidget()
        tb_layout = QVBoxLayout(tab_browse)

        self.browser = DCCAssetBrowserWidget(self, search_placeholder="Filter assets or filenames…")
        self.project_combo = self.browser.project_combo
        for p in self.hub.list_projects():
            self.project_combo.addItem(p.name, p.local_path)
        self.scope_combo = self.browser.scope_combo
        self.search_edit = self.browser.search_edit
        self.asset_tree = self.browser.asset_tree
        self.asset_tree.setHeaderLabels(["File / Asset", "Kind", "Component", "Version", "Note"])
        tb_layout.addWidget(self.browser, 1)

        import_row = QHBoxLayout()
        import_row.addWidget(QLabel("Insert Mode:"))
        self.insert_mode_combo = QComboBox()
        self.insert_mode_combo.addItem("New Track(s)", "new_track")
        self.insert_mode_combo.addItem("Selected Track at Edit Cursor", "cursor_track")
        self.insert_mode_combo.addItem("New Take on Selected Item", "new_take")
        import_row.addWidget(self.insert_mode_combo)
        import_row.addStretch()
        self.import_btn = QPushButton("Import Selected into REAPER")
        self.import_btn.setObjectName("PrimaryButton")
        self.import_btn.clicked.connect(self._on_import_selected)
        import_row.addWidget(self.import_btn)
        tb_layout.addLayout(import_row)
        self.tabs.addTab(tab_browse, "Browse && Import")

        # Tab 2: Session Asset Updates
        tab_updates = QWidget()
        tu_layout = QVBoxLayout(tab_updates)
        tu_top = QHBoxLayout()
        tu_top.addWidget(QLabel("Scan MIDI (in-project or .mid) & media takes in REAPER to version up or down:"))
        tu_top.addStretch()
        self.show_external_chk = QCheckBox("Show external / unmanaged")
        self.show_external_chk.toggled.connect(lambda _c: self.scan_session_updates(silent_if_offline=True))
        tu_top.addWidget(self.show_external_chk)
        self.scan_btn = QPushButton("Scan Open REAPER Session")
        self.scan_btn.clicked.connect(lambda: self.scan_session_updates(silent_if_offline=False))
        tu_top.addWidget(self.scan_btn)
        tu_layout.addLayout(tu_top)

        self.updates_summary_label = QLabel("")
        self.updates_summary_label.setObjectName("MutedText")
        tu_layout.addWidget(self.updates_summary_label)

        self.updates_table = QTableWidget(0, 8)
        self.updates_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.updates_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.updates_table.setHorizontalHeaderLabels(
            ["Update", "Track / Take", "Type", "Obol Asset", "Current", "Target Version", "Status", "Match"]
        )
        self.updates_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.updates_table.setColumnWidth(1, 160)
        self.updates_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.updates_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(7, QHeaderView.ResizeMode.ResizeToContents)
        tu_layout.addWidget(self.updates_table, 1)

        opts_row = QHBoxLayout()
        opts_row.addWidget(QLabel("Update Mode:"))
        self.update_mode_combo = QComboBox()
        self.update_mode_combo.addItem("Replace Take Source (In-Place)", "replace_source")
        self.update_mode_combo.addItem("Add as New Active Take (A/B Compare)", "add_take")
        opts_row.addWidget(self.update_mode_combo)

        opts_row.addWidget(QLabel("MIDI Storage:"))
        self.midi_mode_combo = QComboBox()
        self.midi_mode_combo.addItem("Preserve MIDI Mode (In-Project / File Ref)", "preserve")
        self.midi_mode_combo.addItem("Convert to In-Project MIDI", "in_project")
        self.midi_mode_combo.addItem("Link as External .mid File", "file_ref")
        opts_row.addWidget(self.midi_mode_combo)

        self.match_length_chk = QCheckBox("Auto-resize untrimmed items to new file length")
        self.match_length_chk.setChecked(True)
        opts_row.addWidget(self.match_length_chk)
        opts_row.addStretch()
        tu_layout.addLayout(opts_row)

        tu_btns = QHBoxLayout()
        self.select_outdated_btn = QPushButton("Select Outdated")
        self.select_outdated_btn.clicked.connect(lambda: self._set_updates_selection(outdated_only=True))
        self.select_none_btn = QPushButton("Select None")
        self.select_none_btn.clicked.connect(lambda: self._set_updates_selection(outdated_only=False))
        self.version_down_btn = QPushButton("Version Down (v-1)")
        self.version_down_btn.clicked.connect(self._on_version_down_selected)
        self.set_latest_btn = QPushButton("Set Latest")
        self.set_latest_btn.clicked.connect(self._on_set_latest_selected)
        tu_btns.addWidget(self.select_outdated_btn)
        tu_btns.addWidget(self.select_none_btn)
        tu_btns.addWidget(self.version_down_btn)
        tu_btns.addWidget(self.set_latest_btn)
        tu_btns.addStretch()
        self.apply_updates_btn = QPushButton("Update Selected")
        self.apply_updates_btn.setObjectName("PrimaryButton")
        self.apply_updates_btn.clicked.connect(self._on_apply_updates)
        tu_btns.addWidget(self.apply_updates_btn)
        tu_layout.addLayout(tu_btns)
        self.tabs.addTab(tab_updates, "Session Asset Updates")

        layout.addWidget(self.tabs, 1)

        bottom = QHBoxLayout()
        bottom.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)

        if self._ctx:
            idx = self.project_combo.findText(self._ctx.project_name)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)
        elif self.hub.config.last_active_project:
            idx = self.project_combo.findText(self.hub.config.last_active_project)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)

        self.project_combo.currentIndexChanged.connect(self._populate_asset_tree)
        self.scope_combo.currentIndexChanged.connect(self._populate_asset_tree)
        self.search_edit.textChanged.connect(self._populate_asset_tree)
        self.tabs.currentChanged.connect(self._on_tab_changed)
        self._populate_asset_tree()

        if 0 <= initial_tab < self.tabs.count():
            self.tabs.setCurrentIndex(initial_tab)
        if initial_tab == 1:
            self.scan_session_updates(silent_if_offline=True)

    def _refresh_bridge_pill(self) -> None:
        st = self.bridge.get_state()
        if st.connected:
            self.bridge_pill.setText(f"REAPER Connected ({st.reaper_version or 'live'})")
            self.bridge_pill.setObjectName("StatusPillOnline")
        else:
            self.bridge_pill.setText("REAPER Bridge Offline")
            self.bridge_pill.setObjectName("StatusPillOffline")
        _repolish(self.bridge_pill)

    def _populate_asset_tree(self) -> None:
        self.asset_tree.clear()
        raw_root = self.project_combo.currentData()
        if not raw_root:
            return
        proot = Path(str(raw_root))
        if not proot.is_dir():
            return

        scope = str(self.scope_combo.currentData() or "all")
        needle = self.search_edit.text().strip().lower()
        curr_comp = self._ctx.component_path if (self._ctx and self._ctx.project_root.resolve() == proot.resolve()) else ""
        # When scope is 'repo' and we know curr_comp, restrict repo assets to curr_comp; otherwise list all
        items = self.workspace.list_importable_assets(
            proot,
            current_component=(curr_comp if scope == "repo" else ""),
        )

        for entry in items:
            if scope == "repo" and entry["scope"] != "repo":
                continue
            if scope == "published" and entry["scope"] != "published":
                continue
            if not entry["exists"]:
                continue

            hay = f"{entry['file_name']} {entry['asset_key']} {entry['component']} {entry['note']}".lower()
            if needle and needle not in hay:
                continue

            ver_txt = f"{entry['version_label']}{' ★' if entry['is_latest'] else ''}"
            row = QTreeWidgetItem(
                self.asset_tree,
                [
                    entry["file_name"],
                    entry["scope"],
                    entry["component"],
                    ver_txt,
                    entry["note"],
                ],
            )
            row.setData(0, Qt.ItemDataRole.UserRole, entry)

    def _on_import_selected(self) -> None:
        selected = self.asset_tree.selectedItems()
        if not selected:
            QMessageBox.information(self, "Import into REAPER", "Select one or more files first.")
            return
        if not self.bridge.is_connected():
            QMessageBox.warning(
                self,
                "REAPER Bridge Offline",
                "REAPER Bridge is not running. Start REAPER (with Obol Bridge enabled) first.",
            )
            return

        entries = [it.data(0, Qt.ItemDataRole.UserRole) for it in selected if it.data(0, Qt.ItemDataRole.UserRole)]
        paths = [Path(e["file_path"]) for e in entries]
        mode = str(self.insert_mode_combo.currentData() or "new_track")
        raw_root = self.project_combo.currentData()
        proj_root_str = str(Path(str(raw_root)).resolve()) if raw_root else ""
        metas = [
            {
                "project_root": proj_root_str,
                "scope": e["scope"],
                "component": e["component"],
                "asset_key": e["asset_key"],
                "version": e["version"],
                "file_path": str(Path(e["file_path"]).resolve()),
                "file_name": e["file_name"],
                "media_kind": "midi" if Path(str(e["file_path"])).suffix.lower() in (".mid", ".midi") else "media",
            }
            for e in entries
        ]
        meta = metas[0] if len(metas) == 1 else {}
        try:
            res = self.bridge.insert_media(paths, mode=mode, asset_meta=meta, asset_metas=metas)
            QMessageBox.information(
                self,
                "Imported into REAPER",
                f"Inserted {res.get('inserted', len(paths))} file(s) into REAPER ({mode}).",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Import Failed", str(exc))

    def _on_tab_changed(self, idx: int) -> None:
        if idx == 1:
            self.scan_session_updates(silent_if_offline=True)

    def _set_updates_selection(self, outdated_only: bool) -> None:
        for row in range(self.updates_table.rowCount()):
            chk = self.updates_table.item(row, 0)
            if chk is None or not (chk.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                continue
            info = chk.data(Qt.ItemDataRole.UserRole)
            combo = self.updates_table.cellWidget(row, 5)
            target_ver = combo.currentData() if isinstance(combo, QComboBox) else None
            if outdated_only and isinstance(target_ver, dict) and info and info.get("tracked", True):
                differs = int(target_ver["version"]) != int(info["current_version"])
                chk.setCheckState(Qt.CheckState.Checked if differs else Qt.CheckState.Unchecked)
            else:
                chk.setCheckState(Qt.CheckState.Unchecked)

    def _target_rows_for_batch(self) -> list[int]:
        sel_model = self.updates_table.selectionModel()
        selected_rows = sorted({idx.row() for idx in sel_model.selectedRows()}) if sel_model else []
        if not selected_rows:
            selected_rows = sorted({it.row() for it in self.updates_table.selectedItems()})
        if selected_rows:
            return selected_rows
        checked_rows: list[int] = []
        tracked_rows: list[int] = []
        for row in range(self.updates_table.rowCount()):
            combo = self.updates_table.cellWidget(row, 5)
            if not isinstance(combo, QComboBox):
                continue
            tracked_rows.append(row)
            chk = self.updates_table.item(row, 0)
            if chk is not None and chk.checkState() == Qt.CheckState.Checked:
                checked_rows.append(row)
        return checked_rows or tracked_rows

    def _on_version_down_selected(self) -> None:
        for row in self._target_rows_for_batch():
            combo = self.updates_table.cellWidget(row, 5)
            if isinstance(combo, QComboBox) and combo.currentIndex() > 0:
                combo.setCurrentIndex(combo.currentIndex() - 1)

    def _on_set_latest_selected(self) -> None:
        for row in self._target_rows_for_batch():
            combo = self.updates_table.cellWidget(row, 5)
            if isinstance(combo, QComboBox) and combo.count() > 0:
                combo.setCurrentIndex(combo.count() - 1)

    def _on_target_version_changed(self, row_idx: int) -> None:
        chk = self.updates_table.item(row_idx, 0)
        combo = self.updates_table.cellWidget(row_idx, 5)
        status_it = self.updates_table.item(row_idx, 6)
        if not chk or not isinstance(combo, QComboBox) or not status_it:
            return
        info = chk.data(Qt.ItemDataRole.UserRole)
        ver_entry = combo.currentData()
        if not isinstance(info, dict) or not isinstance(ver_entry, dict):
            return
        curr_v = int(info["current_version"])
        targ_v = int(ver_entry["version"])
        if targ_v > curr_v:
            status_it.setText(f"Upgrade → v{targ_v:03d}")
            status_it.setForeground(QColor("#fbbf24"))
            chk.setCheckState(Qt.CheckState.Checked)
        elif targ_v < curr_v:
            status_it.setText(f"Version Down → v{targ_v:03d}")
            status_it.setForeground(QColor("#60a5fa"))
            chk.setCheckState(Qt.CheckState.Checked)
        else:
            status_it.setText("Up to Date")
            status_it.setForeground(QColor("#4ade80"))
            chk.setCheckState(Qt.CheckState.Unchecked)

    def _populate_updates_row(self, info: dict[str, Any], offline: bool = False) -> None:
        row_idx = self.updates_table.rowCount()
        self.updates_table.insertRow(row_idx)

        tracked = bool(info.get("tracked", True))
        chk = QTableWidgetItem()
        if tracked:
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Checked if info.get("update_available") else Qt.CheckState.Unchecked)
        else:
            chk.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
        chk.setData(Qt.ItemDataRole.UserRole, info)

        track_name = str(info.get("track_name") or "").strip()
        take_name = str(info.get("take_name") or "").strip()
        if offline:
            track_txt = Path(str(info.get("raw_reference") or info.get("current_file") or "")).name
        elif track_name and take_name:
            track_txt = f"{track_name} / {take_name}"
        else:
            track_txt = track_name or take_name or "—"

        is_midi = bool(info.get("is_midi")) or str(info.get("media_kind")) == "midi"
        if is_midi:
            storage = str(info.get("midi_storage") or ("file_ref" if offline else ""))
            if storage == "in_project":
                type_txt = "MIDI (in-proj)"
            elif storage == "file_ref":
                type_txt = "MIDI (.mid)"
            else:
                type_txt = "MIDI"
        else:
            type_txt = "Media"

        match_src = "offline .rpp" if offline else str(info.get("matched_by") or "file_path")

        if not tracked:
            exists = bool(info.get("current_exists", True))
            asset_it = QTableWidgetItem("External (Unmanaged)")
            asset_it.setForeground(QColor("#9ca3af"))
            curr_it = QTableWidgetItem("—")
            ver_it = QTableWidgetItem("—")
            status_it = QTableWidgetItem("External" if exists else "Missing")
            status_it.setForeground(QColor("#9ca3af") if exists else QColor("#f87171"))
            self.updates_table.setItem(row_idx, 0, chk)
            self.updates_table.setItem(row_idx, 1, QTableWidgetItem(track_txt))
            self.updates_table.setItem(row_idx, 2, QTableWidgetItem(type_txt))
            self.updates_table.setItem(row_idx, 3, asset_it)
            self.updates_table.setItem(row_idx, 4, curr_it)
            self.updates_table.setItem(row_idx, 5, ver_it)
            self.updates_table.setItem(row_idx, 6, status_it)
            self.updates_table.setItem(row_idx, 7, QTableWidgetItem(match_src))
            return

        asset_it = QTableWidgetItem(f"{info['component']} / {info['kind']} / {info['asset_key']}")
        curr_it = QTableWidgetItem(f"v{int(info['current_version']):03d}")

        ver_combo = QComboBox()
        versions = list(info.get("available_versions") or [])
        if not versions:
            versions = [
                {
                    "version": int(info["current_version"]),
                    "version_label": f"v{int(info['current_version']):03d}",
                    "file_path": info["current_file"],
                    "file_name": Path(str(info["current_file"])).name,
                    "is_latest": not info.get("update_available"),
                }
            ]
            if info.get("update_available"):
                versions.append({
                    "version": int(info["latest_version"]),
                    "version_label": f"v{int(info['latest_version']):03d}",
                    "file_path": info["latest_file"],
                    "file_name": Path(str(info["latest_file"])).name,
                    "is_latest": True,
                })

        latest_idx = 0
        for idx, v in enumerate(versions):
            lbl = f"{v['version_label']}{' ★' if v.get('is_latest') else ''}"
            ver_combo.addItem(lbl, v)
            if int(v["version"]) == int(info["latest_version"]):
                latest_idx = idx
        ver_combo.setCurrentIndex(latest_idx)
        ver_combo.currentIndexChanged.connect(lambda _idx, r=row_idx: self._on_target_version_changed(r))

        if info.get("update_available"):
            status_it = QTableWidgetItem(f"Upgrade → v{int(info['latest_version']):03d}")
            status_it.setForeground(QColor("#fbbf24"))
        else:
            status_it = QTableWidgetItem("Up to Date")
            status_it.setForeground(QColor("#4ade80"))

        self.updates_table.setItem(row_idx, 0, chk)
        self.updates_table.setItem(row_idx, 1, QTableWidgetItem(track_txt))
        self.updates_table.setItem(row_idx, 2, QTableWidgetItem(type_txt))
        self.updates_table.setItem(row_idx, 3, asset_it)
        self.updates_table.setItem(row_idx, 4, curr_it)
        self.updates_table.setCellWidget(row_idx, 5, ver_combo)
        self.updates_table.setItem(row_idx, 6, status_it)
        self.updates_table.setItem(row_idx, 7, QTableWidgetItem(match_src))

    def scan_session_updates(self, silent_if_offline: bool = False) -> None:
        self._refresh_bridge_pill()
        raw_root = self.project_combo.currentData()
        proot = self._ctx.project_root if self._ctx else (Path(str(raw_root)) if raw_root else None)
        include_external = hasattr(self, "show_external_chk") and self.show_external_chk.isChecked()

        self.updates_table.setRowCount(0)
        self._offline_rpp_mode = False

        if not self.bridge.is_connected():
            rpp_cand = Path(self.active_rpp) if self.active_rpp else None
            if rpp_cand and rpp_cand.is_file() and rpp_cand.suffix.lower() == ".rpp":
                self._offline_rpp_mode = True
                refs = ReaperPlugin.find_outdated_rpp_references(
                    rpp_cand,
                    proot,
                    self.workspace,
                    include_external=include_external,
                )
                for ref in refs:
                    self._populate_updates_row(ref, offline=True)
                tracked_cnt = sum(1 for r in refs if r.get("tracked", True))
                outdated_cnt = sum(1 for r in refs if r.get("update_available"))
                self.updates_summary_label.setText(
                    f"Offline .rpp scan ({rpp_cand.name}): {tracked_cnt} Obol-tracked reference(s), {outdated_cnt} update(s) available."
                )
                return

            self.updates_summary_label.setText(
                "REAPER Bridge offline — start REAPER (with Obol Bridge) or open a working .rpp to scan."
            )
            if not silent_if_offline:
                QMessageBox.warning(self, "REAPER Bridge Offline", "Open REAPER with Obol Bridge active to scan the session.")
            return

        try:
            session_items = self.bridge.query_session_media()
        except Exception as exc:
            if not silent_if_offline:
                QMessageBox.warning(self, "Scan Failed", str(exc))
            self.updates_summary_label.setText(f"Scan failed: {exc}")
            return

        matched_rows: list[dict[str, Any]] = []
        ext_count = 0
        seen_takes: set[tuple[str, str]] = set()
        for sit in session_items:
            info = self.workspace.resolve_reaper_session_item(proot, sit)
            if info is None:
                if include_external:
                    fpath_str = str(sit.get("file_path") or "").strip()
                    take_key = str(sit.get("take_guid") or f"{sit.get('item_idx')}:{sit.get('take_idx')}")
                    dedup_key = (take_key, fpath_str)
                    if dedup_key in seen_takes:
                        continue
                    seen_takes.add(dedup_key)
                    fp = Path(fpath_str).expanduser().resolve() if fpath_str else None
                    ext_row = {
                        "tracked": False,
                        "track_name": str(sit.get("track_name") or ""),
                        "take_name": str(sit.get("take_name") or ""),
                        "is_midi": bool(sit.get("is_midi")),
                        "midi_storage": str(sit.get("midi_storage") or ""),
                        "current_file": fp or "",
                        "current_exists": fp.is_file() if fp is not None else True,
                        "matched_by": "untracked",
                    }
                    ext_count += 1
                    self._populate_updates_row(ext_row, offline=False)
                continue
            take_key = str(info.get("take_guid") or f"{info.get('item_idx')}:{info.get('take_idx')}")
            dedup_key = (take_key, str(info.get("current_file") or ""))
            if dedup_key in seen_takes:
                continue
            seen_takes.add(dedup_key)
            matched_rows.append(info)
            self._populate_updates_row(info, offline=False)

        midi_cnt = sum(1 for r in matched_rows if r.get("is_midi"))
        media_cnt = len(matched_rows) - midi_cnt
        outdated_cnt = sum(1 for r in matched_rows if r.get("update_available"))
        ext_suffix = f", {ext_count} external" if ext_count else ""
        self.updates_summary_label.setText(
            f"Scanned {len(session_items)} session take(s): matched {len(matched_rows)} Obol asset(s) "
            f"({midi_cnt} MIDI, {media_cnt} media), {outdated_cnt} update(s) available{ext_suffix}."
        )

    def _on_apply_updates(self) -> None:
        raw_root = self.project_combo.currentData()
        proot = self._ctx.project_root if self._ctx else (Path(str(raw_root)) if raw_root else None)

        offline_replacements: dict[str, Path] = {}
        take_updates: list[dict[str, Any]] = []
        legacy_replacements: dict[str, str] = {}

        for row in range(self.updates_table.rowCount()):
            chk = self.updates_table.item(row, 0)
            if not chk or chk.checkState() != Qt.CheckState.Checked:
                continue
            info = chk.data(Qt.ItemDataRole.UserRole)
            if not info or not isinstance(info, dict) or not info.get("tracked", True):
                continue
            combo = self.updates_table.cellWidget(row, 5)
            ver_entry = combo.currentData() if isinstance(combo, QComboBox) else None
            if isinstance(ver_entry, dict):
                target_file = Path(str(ver_entry["file_path"]))
                target_ver = int(ver_entry["version"])
            else:
                target_file = Path(str(info["latest_file"]))
                target_ver = int(info["latest_version"])

            curr_file = Path(str(info["current_file"]))
            if target_ver == int(info["current_version"]) and target_file == curr_file:
                continue

            if self._offline_rpp_mode and not self.bridge.is_connected():
                raw_ref = str(info.get("raw_reference") or "")
                if raw_ref:
                    offline_replacements[raw_ref] = target_file
            else:
                legacy_replacements[str(curr_file)] = str(target_file)
                new_meta = {
                    "project_root": str(info.get("project_root") or (proot.resolve() if proot else "") or str((info.get("obol_ext") or {}).get("project_root") or "")),
                    "scope": str(info["kind"]),
                    "component": str(info["component"]),
                    "asset_key": str(info["asset_key"]),
                    "version": target_ver,
                    "file_path": str(target_file.resolve()),
                    "file_name": target_file.name,
                    "media_kind": str(info.get("media_kind") or ("midi" if target_file.suffix.lower() in (".mid", ".midi") else "media")),
                }
                take_updates.append({
                    "take_guid": str(info.get("take_guid") or ""),
                    "item_guid": str(info.get("item_guid") or ""),
                    "old_path": str(curr_file),
                    "target_path": str(target_file),
                    "asset_meta": new_meta,
                })

        if self._offline_rpp_mode and not self.bridge.is_connected():
            if not offline_replacements:
                QMessageBox.information(self, "Session Asset Updates", "No outdated items are checked for update.")
                return
            if not self.active_rpp:
                return
            rpp_path = Path(self.active_rpp)
            rel_proot = proot or (Path(str(next(iter(offline_replacements.values())))).parent)
            try:
                res = ReaperPlugin.update_rpp_references(
                    rpp_path, rel_proot, self.workspace, replacements=offline_replacements
                )
                QMessageBox.information(
                    self,
                    "Updated .rpp References",
                    f"Relinked {res.get('updated_count', 0)} reference(s) in {Path(self.active_rpp).name}.",
                )
                self.scan_session_updates(silent_if_offline=True)
            except Exception as exc:
                QMessageBox.warning(self, "Update Failed", str(exc))
            return

        if not take_updates:
            QMessageBox.information(self, "Session Asset Updates", "No items with a changed target version are checked.")
            return

        update_mode = str(self.update_mode_combo.currentData() or "replace_source")
        midi_mode = str(self.midi_mode_combo.currentData() or "preserve")
        match_length = "auto" if self.match_length_chk.isChecked() else "never"

        try:
            res = self.bridge.update_media_sources(
                replacements=legacy_replacements,
                take_updates=take_updates,
                update_mode=update_mode,
                midi_mode=midi_mode,
                match_length=match_length,
            )
            midi_upd = int(res.get("updated_midi_takes", 0))
            media_upd = int(res.get("updated_media_takes", 0))
            detail = f" ({midi_upd} MIDI, {media_upd} media)" if (midi_upd or media_upd) else ""
            QMessageBox.information(
                self,
                "Updated REAPER Takes",
                f"Updated {res.get('updated_takes', 0)} take(s){detail} across {res.get('updated_items', 0)} media item(s).",
            )
            self.scan_session_updates(silent_if_offline=True)
        except Exception as exc:
            QMessageBox.warning(self, "Update Failed", str(exc))


class ReaperExportPublishDialog(QDialog):
    """Hybrid REAPER Export & Publish dialog (One-Click Direct Render, Pre-Filled Native Dialog, or Existing Files)."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        workspace: ObolWorkspace,
        bridge: ReaperBridgeClient,
        active_rpp: str = "",
        default_project: str = "",
        default_component: str = "",
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.workspace = workspace
        self.bridge = bridge
        self.published_bundle: Optional[VersionBundle] = None

        st = self.bridge.get_state()
        resolved_rpp = active_rpp or st.active_rpp
        self.active_rpp = resolved_rpp
        self._ctx: Optional[WorkingFileContext] = (
            self.workspace.resolve_working_file_context(Path(resolved_rpp)) if resolved_rpp else None
        )

        self.setWindowTitle("Obol — Export && Publish from REAPER")
        self.resize(650, 580)

        layout = QVBoxLayout(self)

        # Context Banner + Manual Fallback
        banner = QFrame()
        banner.setObjectName("PanelCard")
        b_layout = QVBoxLayout(banner)
        if self._ctx:
            b_lbl = QLabel(
                f"Resolved Obol Context: <b>{self._ctx.project_name} / {self._ctx.component_path}</b> "
                f"← <code>working/v{self._ctx.working_version:03d}</code>"
            )
            b_layout.addWidget(b_lbl)
        else:
            b_lbl = QLabel("Active .rpp is outside an Obol working folder — choose target Project & Component:")
            b_lbl.setObjectName("WarningText")
            b_layout.addWidget(b_lbl)

        ctx_form = QFormLayout()
        self.project_combo = QComboBox()
        for p in self.hub.list_projects():
            self.project_combo.addItem(p.name, p.local_path)
        self.component_combo = QComboBox()
        self.working_ver_combo = QComboBox()
        ctx_form.addRow("Project:", self.project_combo)
        ctx_form.addRow("Component:", self.component_combo)
        ctx_form.addRow("Source Working:", self.working_ver_combo)
        b_layout.addLayout(ctx_form)
        if self._ctx:
            self.project_combo.setVisible(False)
            self.component_combo.setVisible(False)
            self.working_ver_combo.setVisible(False)
            for row_idx in range(ctx_form.rowCount()):
                lbl = ctx_form.itemAt(row_idx, QFormLayout.ItemRole.LabelRole)
                if lbl and lbl.widget():
                    lbl.widget().setVisible(False)
        layout.addWidget(banner)

        # Deliverable Metadata
        deliv_form = QFormLayout()
        self.deliverable_combo = QComboBox()
        self.deliverable_combo.setEditable(True)
        self.target_ver_combo = QComboBox()
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("Release notes (e.g., Rough mix v2, Mastered 24-bit WAV)")
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("comma-separated tags")
        deliv_form.addRow("Deliverable Name:", self.deliverable_combo)
        deliv_form.addRow("Target Release:", self.target_ver_combo)
        deliv_form.addRow("Release Note:", self.note_edit)
        deliv_form.addRow("Tags:", self.tags_edit)
        layout.addLayout(deliv_form)

        # Hybrid Render Method
        method_row = QHBoxLayout()
        method_row.addWidget(QLabel("Export Method:"))
        self.method_combo = QComboBox()
        self.method_combo.addItem("One-Click Direct Render (Headless REAPER Render)", "direct")
        self.method_combo.addItem("REAPER Native Render Dialog (Pre-Filled Path)", "native_dialog")
        self.method_combo.addItem("Publish Existing Rendered File(s)...", "existing")
        method_row.addWidget(self.method_combo, 1)
        layout.addLayout(method_row)

        self.method_stack = QStackedWidget()

        # Page 0: One-Click Direct Render
        page_direct = QFrame()
        page_direct.setObjectName("PanelCard")
        pd_form = QFormLayout(page_direct)
        self.render_source_combo = QComboBox()
        self.render_source_combo.addItem("Master Mix (Current Render Settings)", "master")
        self.render_source_combo.addItem("Selected Tracks (Stems)", "stems_selected")
        self.render_source_combo.addItem("Master Mix + Stems", "master_and_stems")
        self.render_source_combo.addItem("Region Render Matrix", "regions")

        self.render_bounds_combo = QComboBox()
        self.render_bounds_combo.addItem("Entire Project", "entire")
        self.render_bounds_combo.addItem("Time Selection", "time_selection")
        self.render_bounds_combo.addItem("All Project Regions", "regions")

        self.preset_combo = QComboBox()
        self.preset_combo.addItem("(Current Project Render Format)", "")
        presets = st.render_presets or parse_reaper_render_presets(detect_reaper_resource_dir())
        for pr in presets:
            self.preset_combo.addItem(pr, pr)

        self.direct_pattern_edit = QLineEdit()
        self.direct_pattern_edit.setPlaceholderText("Leave blank for default or use $track / $project")
        pd_form.addRow("Render Source:", self.render_source_combo)
        pd_form.addRow("Render Bounds:", self.render_bounds_combo)
        pd_form.addRow("Render Preset:", self.preset_combo)
        pd_form.addRow("Filename Pattern:", self.direct_pattern_edit)
        self.method_stack.addWidget(page_direct)

        # Page 1: Native REAPER Render Dialog
        page_native = QFrame()
        page_native.setObjectName("PanelCard")
        pn_layout = QVBoxLayout(page_native)
        pn_info = QLabel(
            "Prepares <code>published/&lt;deliverable&gt;/vNNN/</code>, points REAPER's Render Output Directory "
            "directly to it, opens REAPER's native <b>Render to File</b> dialog so you can adjust any format/wildcards, "
            "and automatically registers all rendered outputs when the dialog closes."
        )
        pn_info.setWordWrap(True)
        pn_layout.addWidget(pn_info)
        pn_form = QFormLayout()
        self.native_pattern_edit = QLineEdit()
        self.native_pattern_edit.setPlaceholderText("Optional pre-filled filename/wildcard pattern (e.g., $project)")
        pn_form.addRow("Initial Pattern:", self.native_pattern_edit)
        pn_layout.addLayout(pn_form)
        pn_layout.addStretch()
        self.method_stack.addWidget(page_native)

        # Page 2: Existing Files
        page_existing = QFrame()
        page_existing.setObjectName("PanelCard")
        pe_layout = QVBoxLayout(page_existing)
        self.existing_files_list = QListWidget()
        pe_layout.addWidget(self.existing_files_list, 1)
        pe_btns = QHBoxLayout()
        add_files_btn = QPushButton("Browse Files...")
        add_files_btn.clicked.connect(self._browse_existing_files)
        rem_file_btn = QPushButton("Remove Selected")
        rem_file_btn.clicked.connect(self._remove_existing_file)
        pe_btns.addWidget(add_files_btn)
        pe_btns.addWidget(rem_file_btn)
        pe_btns.addStretch()
        pe_layout.addLayout(pe_btns)
        self.method_stack.addWidget(page_existing)

        layout.addWidget(self.method_stack, 1)

        # Pipeline Options
        self.save_before_chk = QCheckBox("Save active .rpp before rendering")
        self.save_before_chk.setChecked(True)
        self.collect_before_chk = QCheckBox("Scan && collect external media into repo/ before publishing")
        self.vup_after_chk = QCheckBox("Version Up working .rpp after publish")
        layout.addWidget(self.save_before_chk)
        layout.addWidget(self.collect_before_chk)
        layout.addWidget(self.vup_after_chk)

        self.export_btn = QPushButton("Export && Publish")
        self.export_btn.setObjectName("PrimaryButton")
        self.export_btn.clicked.connect(self._on_execute)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(DialogFooter(self, cancel_button=cancel_btn, primary_button=self.export_btn))

        self.method_combo.currentIndexChanged.connect(self.method_stack.setCurrentIndex)
        self.project_combo.currentIndexChanged.connect(self._reload_components)
        self.component_combo.currentIndexChanged.connect(self._reload_component_details)
        self.deliverable_combo.currentTextChanged.connect(self._reload_target_versions)

        init_proj = self._ctx.project_name if self._ctx else (default_project or self.hub.config.last_active_project)
        if init_proj:
            idx = self.project_combo.findText(init_proj)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)
        self._reload_components(preselect_comp=(self._ctx.component_path if self._ctx else default_component))

    def _current_target(self) -> tuple[Optional[Path], str, int]:
        if self._ctx:
            return self._ctx.project_root, self._ctx.component_path, self._ctx.working_version
        raw_root = self.project_combo.currentData()
        proot = Path(str(raw_root)) if raw_root else None
        comp = str(self.component_combo.currentData() or self.component_combo.currentText())
        w_ver = int(self.working_ver_combo.currentData() or 1)
        return proot, comp, w_ver

    def _reload_components(self, _idx: int = 0, preselect_comp: str = "") -> None:
        self.component_combo.blockSignals(True)
        self.component_combo.clear()
        raw_root = self.project_combo.currentData()
        if raw_root and Path(str(raw_root)).is_dir():
            for comp in discover_project_components(Path(str(raw_root))):
                self.component_combo.addItem(comp, comp)
        if preselect_comp:
            idx = self.component_combo.findData(preselect_comp)
            if idx >= 0:
                self.component_combo.setCurrentIndex(idx)
        self.component_combo.blockSignals(False)
        self._reload_component_details()

    def _reload_component_details(self) -> None:
        proot, comp, _ = self._current_target()
        self.working_ver_combo.clear()
        self.deliverable_combo.blockSignals(True)
        curr_deliv = self.deliverable_combo.currentText().strip() or "main"
        self.deliverable_combo.clear()
        defaults = ["main", "stems", "midi"]
        if proot and comp and proot.is_dir():
            snap = load_component_snapshot(proot, comp)
            for w in reversed(snap.working_versions):
                self.working_ver_combo.addItem(w.version_label, w.version)
            existing = list(snap.published_deliverables.keys())
            for d in list(dict.fromkeys(existing + defaults)):
                self.deliverable_combo.addItem(d)
        else:
            for d in defaults:
                self.deliverable_combo.addItem(d)
        self.deliverable_combo.setCurrentText(curr_deliv)
        self.deliverable_combo.blockSignals(False)
        self._reload_target_versions()

    def _reload_target_versions(self) -> None:
        proot, comp, _ = self._current_target()
        self.target_ver_combo.clear()
        next_v = 1
        existing_vers: list[int] = []
        if proot and comp and proot.is_dir():
            snap = load_component_snapshot(proot, comp)
            deliv_key = self.workspace.normalize_deliverable_name(self.deliverable_combo.currentText())
            deliv = snap.published_deliverables.get(deliv_key)
            if deliv and deliv.versions:
                existing_vers = sorted((v.version for v in deliv.versions), reverse=True)
                next_v = max(existing_vers) + 1
        self.target_ver_combo.addItem(f"New Release (v{next_v:03d})", None)
        for ev in existing_vers:
            self.target_ver_combo.addItem(f"Append to v{ev:03d}", ev)
        if not self.direct_pattern_edit.text().strip():
            dname = self.workspace.normalize_deliverable_name(self.deliverable_combo.currentText())
            self.direct_pattern_edit.setPlaceholderText(f"Default: {dname}_v{next_v:03d} (or $track for stems)")

    def _browse_existing_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Select Rendered Deliverable Files")
        for f in files:
            self.existing_files_list.addItem(f)

    def _remove_existing_file(self) -> None:
        for it in self.existing_files_list.selectedItems():
            self.existing_files_list.takeItem(self.existing_files_list.row(it))

    def _on_execute(self) -> None:
        proot, comp, work_ver = self._current_target()
        if not proot or not comp:
            QMessageBox.warning(self, "Export & Publish", "Please select a valid Obol project and component.")
            return

        method = str(self.method_combo.currentData() or "direct")
        deliv_name = self.workspace.normalize_deliverable_name(self.deliverable_combo.currentText())
        target_ver = self.target_ver_combo.currentData()
        comment = self.note_edit.text().strip()
        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]

        if method in ("direct", "native_dialog") and not self.bridge.is_connected():
            QMessageBox.warning(
                self,
                "REAPER Bridge Offline",
                "REAPER Bridge is not running. Start REAPER (with Obol Bridge) or choose 'Publish Existing Rendered File(s)'.",
            )
            return

        if method == "existing":
            files = [Path(self.existing_files_list.item(i).text()) for i in range(self.existing_files_list.count())]
            if not files:
                QMessageBox.warning(self, "Publish Files", "Add at least one file to publish.")
                return
        else:
            files = []

        save_before = self.save_before_chk.isChecked()
        collect_before = self.collect_before_chk.isChecked()
        vup_after = self.vup_after_chk.isChecked()
        direct_pattern = self.direct_pattern_edit.text().strip()
        render_mode = str(self.render_source_combo.currentData() or "master")
        bounds = str(self.render_bounds_combo.currentData() or "entire")
        preset_name = str(self.preset_combo.currentData() or "")
        native_pattern = self.native_pattern_edit.text().strip()

        def _work() -> Optional[VersionBundle]:
            if self.bridge.is_connected() and save_before and method in ("direct", "native_dialog"):
                self.bridge.save_project()

            if collect_before and self._ctx:
                collected = self.workspace.collect_external_dependencies(
                    proot, comp, working_version=work_ver
                )
                if collected and self.bridge.is_connected() and self.active_rpp:
                    self.bridge.reload_project(Path(self.active_rpp))

            if method == "existing":
                bundle = self.workspace.publish_version(
                    project_root=proot,
                    component_path=comp,
                    source_files=files,
                    source_working_version=work_ver,
                    comment=comment,
                    tags=tags,
                    deliverable_name=deliv_name,
                    target_version=target_ver,
                )
            else:
                deliv_path, ver_num, export_dir = self.workspace.prepare_publish_export_folder(
                    proot, comp, deliverable_name=deliv_name, target_version=target_ver
                )
                if method == "direct":
                    pattern = direct_pattern or ("$track" if render_mode == "stems_selected" else f"{deliv_path}_v{ver_num:03d}")
                    self.bridge.render_direct(
                        output_dir=export_dir,
                        pattern=pattern,
                        render_mode=render_mode,
                        bounds=bounds,
                        preset_name=preset_name,
                    )
                else:
                    self.bridge.open_render_dialog(
                        output_dir=export_dir,
                        pattern=native_pattern,
                        restore_after=True,
                    )

                bundle = self.workspace.sync_published_folder(
                    project_root=proot,
                    component_path=comp,
                    deliverable_name=deliv_path,
                    version=ver_num,
                    source_working_version=work_ver,
                    comment=comment,
                )
                if bundle is None:
                    if export_dir.is_dir() and not any(export_dir.iterdir()):
                        export_dir.rmdir()
                    return None
                if tags:
                    self.workspace.update_published_metadata(
                        project_root=proot,
                        component_path=comp,
                        deliverable_name=deliv_path,
                        version=bundle.version,
                        tags=list(dict.fromkeys(list(bundle.tags) + tags)),
                    )

            if vup_after:
                next_bundle = self.workspace.version_up_working(
                    proot,
                    comp,
                    comment=f"Post-publish version up after {deliv_name}/{bundle.version_label}",
                    source_version=work_ver,
                )
                if self.bridge.is_connected():
                    self.bridge.open_project((proot / comp / next_bundle.primary_file).resolve(), noprompt=True)

            return bundle

        def _on_success(bundle: Optional[VersionBundle]) -> None:
            if bundle is None:
                QMessageBox.information(
                    self,
                    "Export Cancelled",
                    "No rendered files were found in the export folder.",
                )
                return
            self.published_bundle = bundle
            QMessageBox.information(
                self,
                "Published Deliverable",
                f"Published {comp}/published/{deliv_name}/{bundle.version_label} ({len(bundle.files)} file(s))!",
            )
            self.accept()

        def _on_error(exc: Exception) -> None:
            QMessageBox.warning(self, "Export & Publish Failed", str(exc))

        run_with_busy_state(
            self,
            _work,
            on_success=_on_success,
            on_error=_on_error,
            disable_widgets=[self.export_btn],
        )


class ReaperIntegrationDialog(QDialog):
    """1-Click REAPER ReaScript installer, status monitor, and uninstaller."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        bridge: ReaperBridgeClient,
        installer: Optional[ReaperInstaller] = None,
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.bridge = bridge
        self.installer = installer or ReaperInstaller(hub)

        self.setWindowTitle("REAPER DAW Integration — Scripts && Live Bridge")
        self.resize(620, 460)

        layout = QVBoxLayout(self)

        hdr = QHBoxLayout()
        title = QLabel("REAPER Integration")
        title.setObjectName("SectionHeader")
        self.status_pill = QLabel()
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(self.status_pill)
        layout.addLayout(hdr)

        form = QFormLayout()
        res_row = QHBoxLayout()
        self.res_dir_edit = QLineEdit()
        res_browse = QPushButton("Browse...")
        res_browse.clicked.connect(self._browse_resource_dir)
        res_row.addWidget(self.res_dir_edit, 1)
        res_row.addWidget(res_browse)
        form.addRow("REAPER Resource Folder:", res_row)

        exe_row = QHBoxLayout()
        self.exe_edit = QLineEdit()
        exe_browse = QPushButton("Browse...")
        exe_browse.clicked.connect(self._browse_exe)
        exe_row.addWidget(self.exe_edit, 1)
        exe_row.addWidget(exe_browse)
        form.addRow("REAPER Executable:", exe_row)
        layout.addLayout(form)

        self.autostart_chk = QCheckBox("Auto-start Obol Bridge on REAPER launch (Scripts/__startup.lua)")
        self.autostart_chk.setChecked(True)
        self.save_exe_chk = QCheckBox("Save REAPER executable in Obol Software Profiles")
        self.save_exe_chk.setChecked(True)
        layout.addWidget(self.autostart_chk)
        layout.addWidget(self.save_exe_chk)

        self.details_label = QLabel("")
        self.details_label.setObjectName("MutedText")
        self.details_label.setWordWrap(True)
        layout.addWidget(self.details_label, 1)

        btns = QHBoxLayout()
        self.install_btn = QPushButton("Install / Update REAPER Integration")
        self.install_btn.setObjectName("PrimaryButton")
        self.install_btn.clicked.connect(self._on_install)
        self.uninstall_btn = QPushButton("Uninstall")
        self.uninstall_btn.setObjectName("DangerButton")
        self.uninstall_btn.clicked.connect(self._on_uninstall)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btns.addWidget(self.install_btn)
        btns.addWidget(self.uninstall_btn)
        btns.addStretch()
        btns.addWidget(close_btn)
        layout.addLayout(btns)

        self._load_status()

    def _browse_resource_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select REAPER Resource Directory", self.res_dir_edit.text())
        if folder:
            self.res_dir_edit.setText(folder)
            self._load_status(override_res=Path(folder))

    def _browse_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select REAPER Executable", self.exe_edit.text())
        if path:
            self.exe_edit.setText(path)

    def _load_status(self, override_res: Optional[Path] = None) -> None:
        res_dir = override_res or (
            Path(self.res_dir_edit.text().strip()) if self.res_dir_edit.text().strip() else detect_reaper_resource_dir()
        )
        st = self.installer.status(resource_dir=res_dir)
        if not self.res_dir_edit.text().strip() and st["resource_dir"]:
            self.res_dir_edit.setText(st["resource_dir"])
        if not self.exe_edit.text().strip():
            det_exe = st["executable"] or (str(detect_reaper_executable() or ""))
            self.exe_edit.setText(det_exe)

        if st["bridge_connected"]:
            self.status_pill.setText(f"Bridge Connected (REAPER {st['reaper_version']})")
            self.status_pill.setObjectName("StatusPillOnline")
        else:
            self.status_pill.setText("Bridge Offline")
            self.status_pill.setObjectName("StatusPillLocal")
        _repolish(self.status_pill)

        menu_tip = (
            f"\nToolbar MenuSet: {st['toolbar_menu_path']}\n"
            "(In REAPER: Options → Customize menus/toolbars → Import to add an Obol toolbar)"
            if st["toolbar_menu_path"]
            else ""
        )
        self.details_label.setText(
            f"Installed Scripts: {len(st['scripts_installed'])} / {len(OBOL_ACTION_SPECS)}\n"
            f"Registered Actions (reaper-kb.ini): {len(st['actions_registered'])}\n"
            f"Startup Auto-Bridge: {'Enabled' if st['startup_hook_installed'] else 'Disabled'}\n"
            f"Active .rpp: {st['active_rpp'] or 'None'}"
            f"{menu_tip}"
        )

    def _on_install(self) -> None:
        res_txt = self.res_dir_edit.text().strip()
        if not res_txt:
            QMessageBox.warning(self, "REAPER Integration", "Select a REAPER resource folder first.")
            return
        try:
            st = self.installer.install(
                resource_dir=Path(res_txt),
                auto_start_bridge=self.autostart_chk.isChecked(),
                configure_executable=self.save_exe_chk.isChecked(),
                executable_override=self.exe_edit.text().strip(),
            )
            self._load_status(override_res=Path(res_txt))
            QMessageBox.information(
                self,
                "REAPER Integration Installed",
                f"Installed {len(st['scripts_installed'])} Obol scripts and registered {len(st['actions_registered'])} REAPER actions.\n\n"
                "Restart REAPER (or run 'Custom: Obol - Toggle Bridge') to activate the live bridge.",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Install Failed", str(exc))

    def _on_uninstall(self, confirm: bool = True) -> None:
        if confirm and not confirm_destructive_action(
            self,
            "Uninstall REAPER Integration",
            "Remove Obol REAPER scripts and startup hooks?",
            confirm_label="Uninstall",
        ):
            return
        res_txt = self.res_dir_edit.text().strip()
        res_dir = Path(res_txt) if res_txt else None
        self.installer.uninstall(resource_dir=res_dir)
        self._load_status(override_res=res_dir)
        QMessageBox.information(self, "Uninstalled", "Removed Obol REAPER scripts and startup hooks.")


def launch_reaper_dialog(
    hub: ObolHub,
    mode: str,
    active_rpp: str = "",
    project: str = "",
    component: str = "",
) -> int:
    """Standalone PySide6 entrypoint for REAPER action scripts (`obol reaper ui --mode ...`)."""
    app = QApplication.instance() or QApplication(sys.argv)
    icon_path = Path(__file__).parent / "assets" / "obol_icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    apply_studio_dark_theme(app)

    workspace = ObolWorkspace(hub)
    bridge = ReaperBridgeClient(hub)

    if mode == "open":
        dlg: QDialog = ReaperOpenDialog(None, hub, workspace, bridge, active_rpp=active_rpp)
    elif mode == "import":
        dlg = ReaperImportDialog(None, hub, workspace, bridge, active_rpp=active_rpp, initial_tab=0)
    elif mode == "update":
        dlg = ReaperImportDialog(None, hub, workspace, bridge, active_rpp=active_rpp, initial_tab=1)
    elif mode == "publish":
        dlg = ReaperExportPublishDialog(
            None,
            hub,
            workspace,
            bridge,
            active_rpp=active_rpp,
            default_project=project,
            default_component=component,
        )
    else:
        from obol.gui.integrations_dialog import IntegrationsManagerDialog

        dlg = IntegrationsManagerDialog(None, hub, initial_dcc="reaper")

    dlg.exec()
    return 0
