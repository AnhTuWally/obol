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
from obol.gui.theme import apply_studio_dark_theme
from obol.plugins.base import launch_file_with_profile
from obol.plugins.reaper_bridge import (
    ReaperBridgeClient,
    detect_reaper_executable,
    detect_reaper_resource_dir,
    parse_reaper_render_presets,
)
from obol.plugins.reaper_installer import ReaperInstaller


def _repolish(widget: QWidget) -> None:
    st = widget.style()
    if st is not None:
        st.unpolish(widget)
        st.polish(widget)
    widget.update()


class ReaperOpenDialog(QDialog):
    """Browse Obol projects/components/versions and open or version-up directly in REAPER."""

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

        btns = QHBoxLayout()
        self.vup_open_btn = QPushButton("Version Up && Open")
        self.vup_open_btn.clicked.connect(self._on_vup_and_open)
        self.open_btn = QPushButton("Open in REAPER")
        self.open_btn.setObjectName("PrimaryButton")
        self.open_btn.clicked.connect(self._on_open_selected)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btns.addStretch()
        btns.addWidget(self.vup_open_btn)
        btns.addWidget(self.open_btn)
        btns.addWidget(cancel_btn)
        layout.addLayout(btns)

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

    def _on_vup_and_open(self) -> None:
        proot = self._current_project_root()
        comp_item = self.comp_tree.currentItem()
        if not proot or comp_item is None:
            return
        comp = str(comp_item.data(0, Qt.ItemDataRole.UserRole) or "")
        ver_item = self.ver_tree.currentItem()
        src_v = int(ver_item.data(0, Qt.ItemDataRole.UserRole)) if ver_item is not None else None
        bundle = self.workspace.version_up_working(proot, comp, source_version=src_v)
        target_file = (proot / comp / bundle.primary_file).resolve()
        self._open_rpp_target(target_file)


class ReaperImportDialog(QDialog):
    """Browse repo/ & published/ deliverables to import into REAPER, and update outdated takes in the active session."""

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
        self.active_rpp = active_rpp
        self._ctx: Optional[WorkingFileContext] = (
            self.workspace.resolve_working_file_context(Path(active_rpp)) if active_rpp else None
        )

        self.setWindowTitle("Obol — Import & Update Assets in REAPER")
        self.resize(860, 560)

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

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Project:"))
        self.project_combo = QComboBox()
        for p in self.hub.list_projects():
            self.project_combo.addItem(p.name, p.local_path)
        filter_row.addWidget(self.project_combo)

        filter_row.addWidget(QLabel("Scope:"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("All Repo & Published", "all")
        self.scope_combo.addItem("Current Component Repo", "repo")
        self.scope_combo.addItem("Project Deliverables (Published)", "published")
        filter_row.addWidget(self.scope_combo)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter assets or filenames…")
        self.search_edit.setClearButtonEnabled(True)
        filter_row.addWidget(self.search_edit, 1)
        tb_layout.addLayout(filter_row)

        self.asset_tree = QTreeWidget()
        self.asset_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.asset_tree.setAlternatingRowColors(True)
        self.asset_tree.setHeaderLabels(["File / Asset", "Kind", "Component", "Version", "Note"])
        self.asset_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.asset_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.asset_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.asset_tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.asset_tree.header().setSectionResizeMode(4, QHeaderView.ResizeMode.Interactive)
        self.asset_tree.setColumnWidth(4, 180)
        tb_layout.addWidget(self.asset_tree, 1)

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
        tu_top.addWidget(QLabel("Scan takes in the open REAPER session for newer repo/ or published/ versions:"))
        tu_top.addStretch()
        self.scan_btn = QPushButton("Scan Open REAPER Session")
        self.scan_btn.clicked.connect(self.scan_session_updates)
        tu_top.addWidget(self.scan_btn)
        tu_layout.addLayout(tu_top)

        self.updates_table = QTableWidget(0, 6)
        self.updates_table.setHorizontalHeaderLabels(["Update", "Track", "Obol Asset", "Current", "Latest", "Status"])
        self.updates_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.updates_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        tu_layout.addWidget(self.updates_table, 1)

        tu_btns = QHBoxLayout()
        tu_btns.addStretch()
        self.apply_updates_btn = QPushButton("Update Selected to Latest")
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
        self._populate_asset_tree()

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
        meta = (
            {
                "scope": entries[0]["scope"],
                "component": entries[0]["component"],
                "asset_key": entries[0]["asset_key"],
                "version": entries[0]["version"],
            }
            if len(entries) == 1
            else {}
        )
        try:
            res = self.bridge.insert_media(paths, mode=mode, asset_meta=meta)
            QMessageBox.information(
                self,
                "Imported into REAPER",
                f"Inserted {res.get('inserted', len(paths))} file(s) into REAPER ({mode}).",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Import Failed", str(exc))

    def scan_session_updates(self) -> None:
        self._refresh_bridge_pill()
        if not self.bridge.is_connected():
            QMessageBox.warning(self, "REAPER Bridge Offline", "Open REAPER with Obol Bridge active to scan the session.")
            return
        raw_root = self.project_combo.currentData()
        proot = self._ctx.project_root if self._ctx else (Path(str(raw_root)) if raw_root else None)
        if not proot or not proot.is_dir():
            return

        try:
            session_items = self.bridge.query_session_media()
        except Exception as exc:
            QMessageBox.warning(self, "Scan Failed", str(exc))
            return

        self.updates_table.setRowCount(0)
        seen_paths: set[str] = set()
        for sit in session_items:
            fpath_str = str(sit.get("file_path", ""))
            if not fpath_str or fpath_str in seen_paths:
                continue
            info = self.workspace.find_newer_asset_version(proot, Path(fpath_str))
            if info is None:
                continue
            seen_paths.add(fpath_str)
            row_idx = self.updates_table.rowCount()
            self.updates_table.insertRow(row_idx)

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if info["update_available"] else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, info)

            track_it = QTableWidgetItem(str(sit.get("track_name", "") or "—"))
            asset_it = QTableWidgetItem(f"{info['component']} / {info['kind']} / {info['asset_key']}")
            curr_it = QTableWidgetItem(f"v{info['current_version']:03d}")
            lat_it = QTableWidgetItem(f"v{info['latest_version']:03d}")
            status_it = QTableWidgetItem("Update Available" if info["update_available"] else "Up to Date")
            if info["update_available"]:
                status_it.setForeground(QColor("#fbbf24"))
            else:
                status_it.setForeground(QColor("#4ade80"))

            self.updates_table.setItem(row_idx, 0, chk)
            self.updates_table.setItem(row_idx, 1, track_it)
            self.updates_table.setItem(row_idx, 2, asset_it)
            self.updates_table.setItem(row_idx, 3, curr_it)
            self.updates_table.setItem(row_idx, 4, lat_it)
            self.updates_table.setItem(row_idx, 5, status_it)

    def _on_apply_updates(self) -> None:
        replacements: dict[str, str] = {}
        for row in range(self.updates_table.rowCount()):
            chk = self.updates_table.item(row, 0)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                info = chk.data(Qt.ItemDataRole.UserRole)
                if info and info.get("update_available"):
                    replacements[str(info["current_file"])] = str(info["latest_file"])

        if not replacements:
            QMessageBox.information(self, "Session Asset Updates", "No outdated items are checked for update.")
            return

        try:
            res = self.bridge.update_media_sources(replacements)
            QMessageBox.information(
                self,
                "Updated REAPER Takes",
                f"Updated {res.get('updated_takes', 0)} take(s) across {res.get('updated_items', 0)} media item(s).",
            )
            self.scan_session_updates()
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

        btns = QHBoxLayout()
        btns.addStretch()
        self.export_btn = QPushButton("Export && Publish")
        self.export_btn.setObjectName("PrimaryButton")
        self.export_btn.clicked.connect(self._on_execute)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        btns.addWidget(self.export_btn)
        btns.addWidget(cancel_btn)
        layout.addLayout(btns)

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

        try:
            if self.bridge.is_connected() and self.save_before_chk.isChecked() and method in ("direct", "native_dialog"):
                self.bridge.save_project()

            if self.collect_before_chk.isChecked() and self._ctx:
                collected = self.workspace.collect_external_dependencies(
                    proot, comp, working_version=work_ver
                )
                if collected and self.bridge.is_connected() and self.active_rpp:
                    self.bridge.reload_project(Path(self.active_rpp))

            if method == "existing":
                files = [Path(self.existing_files_list.item(i).text()) for i in range(self.existing_files_list.count())]
                if not files:
                    QMessageBox.warning(self, "Publish Files", "Add at least one file to publish.")
                    return
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
                    pattern = self.direct_pattern_edit.text().strip()
                    if not pattern:
                        src_mode = str(self.render_source_combo.currentData() or "master")
                        pattern = "$track" if src_mode == "stems_selected" else f"{deliv_path}_v{ver_num:03d}"
                    self.bridge.render_direct(
                        output_dir=export_dir,
                        pattern=pattern,
                        render_mode=str(self.render_source_combo.currentData() or "master"),
                        bounds=str(self.render_bounds_combo.currentData() or "entire"),
                        preset_name=str(self.preset_combo.currentData() or ""),
                    )
                else:
                    self.bridge.open_render_dialog(
                        output_dir=export_dir,
                        pattern=self.native_pattern_edit.text().strip(),
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
                    QMessageBox.information(
                        self,
                        "Export Cancelled",
                        "No rendered files were found in the export folder.",
                    )
                    return
                if tags:
                    self.workspace.update_published_metadata(
                        project_root=proot,
                        component_path=comp,
                        deliverable_name=deliv_path,
                        version=bundle.version,
                        tags=list(dict.fromkeys(list(bundle.tags) + tags)),
                    )

            if self.vup_after_chk.isChecked():
                next_bundle = self.workspace.version_up_working(
                    proot,
                    comp,
                    comment=f"Post-publish version up after {deliv_name}/{bundle.version_label}",
                    source_version=work_ver,
                )
                if self.bridge.is_connected():
                    self.bridge.open_project((proot / comp / next_bundle.primary_file).resolve(), noprompt=True)

            self.published_bundle = bundle
            QMessageBox.information(
                self,
                "Published Deliverable",
                f"Published {comp}/published/{deliv_name}/{bundle.version_label} ({len(bundle.files)} file(s))!",
            )
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "Export & Publish Failed", str(exc))


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
            f"Installed Scripts: {len(st['scripts_installed'])} / 7\n"
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

    def _on_uninstall(self) -> None:
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
        dlg = ReaperImportDialog(None, hub, workspace, bridge, active_rpp=active_rpp)
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
        dlg = ReaperIntegrationDialog(None, hub, bridge)

    dlg.exec()
    return 0
