"""Focused PySide6 companion dialogs for DaVinci Resolve integration (Import/Update, Export/Publish, Setup)."""

from __future__ import annotations

import json
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
from obol.core.workspace import ObolWorkspace
from obol.gui.theme import apply_studio_dark_theme
from obol.plugins.resolve_bridge import (
    ResolveBridgeClient,
    detect_resolve_executable,
    detect_resolve_scripts_dir,
)
from obol.plugins.resolve_installer import ResolveInstaller


def _repolish(widget: QWidget) -> None:
    st = widget.style()
    if st is not None:
        st.unpolish(widget)
        st.polish(widget)
    widget.update()


def _write_result_json(result_file: Path, payload: dict[str, Any]) -> None:
    result_file.parent.mkdir(parents=True, exist_ok=True)
    result_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


class ResolveImportDialog(QDialog):
    """Load repo/ & published/ media into Resolve's Media Pool and update outdated Media Pool clips."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        workspace: ObolWorkspace,
        bridge: ResolveBridgeClient,
        context_data: Optional[dict[str, Any]] = None,
        result_file: Optional[Path] = None,
        default_project: str = "",
        default_component: str = "",
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.workspace = workspace
        self.bridge = bridge
        self.context_data = dict(context_data or {})
        self.result_file = result_file

        self.setWindowTitle("Obol — Load Media into DaVinci Resolve")
        self.resize(880, 580)

        resolve_proj = str(self.context_data.get("project_name", "")) or self.bridge.get_state().project_name
        binding = self.bridge.get_project_binding(resolve_proj) if resolve_proj else None
        init_project = default_project or (binding["project_name"] if binding else "") or self.hub.config.last_active_project
        self._init_component = default_component or (binding["component_path"] if binding else "")

        layout = QVBoxLayout(self)

        hdr = QHBoxLayout()
        sess_txt = (
            f"Resolve Session: <b>{resolve_proj or '(Untitled)'}</b>"
            + (f"  •  Bin: <code>{self.context_data.get('current_bin')}</code>" if self.context_data.get("current_bin") else "")
        )
        hdr.addWidget(QLabel(sess_txt), 1)
        self.bridge_pill = QLabel()
        self._refresh_bridge_pill()
        hdr.addWidget(self.bridge_pill)
        layout.addLayout(hdr)

        self.tabs = QTabWidget()

        # Tab 1: Load Media into Media Pool
        tab_load = QWidget()
        tl_layout = QVBoxLayout(tab_load)

        filter_row = QHBoxLayout()
        filter_row.addWidget(QLabel("Project:"))
        self.project_combo = QComboBox()
        for p in self.hub.list_projects():
            self.project_combo.addItem(p.name, p.local_path)
        filter_row.addWidget(self.project_combo)

        filter_row.addWidget(QLabel("Scope:"))
        self.scope_combo = QComboBox()
        self.scope_combo.addItem("All Repo & Published", "all")
        self.scope_combo.addItem("Component Repo Only", "repo")
        self.scope_combo.addItem("Published Deliverables Only", "published")
        filter_row.addWidget(self.scope_combo)

        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Filter media, stems, renders…")
        self.search_edit.setClearButtonEnabled(True)
        filter_row.addWidget(self.search_edit, 1)
        tl_layout.addLayout(filter_row)

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
        tl_layout.addWidget(self.asset_tree, 1)

        opts_form = QFormLayout()
        self.bin_combo = QComboBox()
        self.bin_combo.setEditable(True)
        self.bin_combo.addItem("(Current Media Pool Bin)", "")
        self.bin_combo.addItem("Root Bin (Master)", "root")
        self.bin_combo.addItem("Auto: Obol/<component>/<scope>", "__auto__")
        opts_form.addRow("Target Media Pool Bin:", self.bin_combo)
        tl_layout.addLayout(opts_form)

        bottom_load = QHBoxLayout()
        self.append_tl_chk = QCheckBox("Also append imported clip(s) to current timeline")
        bottom_load.addWidget(self.append_tl_chk)
        bottom_load.addStretch()
        self.import_btn = QPushButton("Load into Resolve Media Pool")
        self.import_btn.setObjectName("PrimaryButton")
        self.import_btn.clicked.connect(self._on_import_selected)
        bottom_load.addWidget(self.import_btn)
        tl_layout.addLayout(bottom_load)
        self.tabs.addTab(tab_load, "Load into Media Pool")

        # Tab 2: Media Pool Asset Updates
        tab_upd = QWidget()
        tu_layout = QVBoxLayout(tab_upd)
        tu_top = QHBoxLayout()
        tu_top.addWidget(QLabel("Scan Media Pool clips for newer versions in repo/ or published/:"))
        tu_top.addStretch()
        self.scan_btn = QPushButton("Scan Media Pool")
        self.scan_btn.clicked.connect(self.scan_media_pool_updates)
        tu_top.addWidget(self.scan_btn)
        tu_layout.addLayout(tu_top)

        self.updates_table = QTableWidget(0, 6)
        self.updates_table.setHorizontalHeaderLabels(["Update", "Bin", "Clip", "Obol Asset", "Current → Latest", "Status"])
        self.updates_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.updates_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        tu_layout.addWidget(self.updates_table, 1)

        tu_btns = QHBoxLayout()
        tu_btns.addStretch()
        self.apply_updates_btn = QPushButton("Update Selected Clips to Latest")
        self.apply_updates_btn.setObjectName("PrimaryButton")
        self.apply_updates_btn.clicked.connect(self._on_apply_updates)
        tu_btns.addWidget(self.apply_updates_btn)
        tu_layout.addLayout(tu_btns)
        self.tabs.addTab(tab_upd, "Media Pool Version Updates")

        layout.addWidget(self.tabs, 1)

        close_row = QHBoxLayout()
        close_row.addStretch()
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.reject)
        close_row.addWidget(close_btn)
        layout.addLayout(close_row)

        if init_project:
            idx = self.project_combo.findText(init_project)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)

        self.project_combo.currentIndexChanged.connect(self._populate_asset_tree)
        self.scope_combo.currentIndexChanged.connect(self._populate_asset_tree)
        self.search_edit.textChanged.connect(self._populate_asset_tree)
        self._populate_asset_tree()

        if "media_pool_clips" in self.context_data:
            self._populate_updates_from_clips(list(self.context_data.get("media_pool_clips") or []))

    def _refresh_bridge_pill(self) -> None:
        if self.result_file is not None:
            self.bridge_pill.setText("Resolve Script Session")
            self.bridge_pill.setObjectName("StatusPillOnline")
        else:
            st = self.bridge.get_state()
            if st.connected:
                label = "Studio API" if st.mode == "studio_api" else "Lua Bridge"
                self.bridge_pill.setText(f"Resolve Connected ({label})")
                self.bridge_pill.setObjectName("StatusPillOnline")
            else:
                self.bridge_pill.setText("Resolve Offline")
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
        items = self.workspace.list_importable_assets(
            proot,
            current_component=(self._init_component if scope == "repo" else ""),
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

    def _resolved_bin_path(self, entries: list[dict[str, Any]]) -> str:
        data = self.bin_combo.currentData()
        txt = self.bin_combo.currentText().strip()
        if data is not None and txt in (
            "(Current Media Pool Bin)",
            "Root Bin (Master)",
            "Auto: Obol/<component>/<scope>",
        ):
            val = str(data)
        else:
            val = txt
        if val == "__auto__" and entries:
            first = entries[0]
            return f"Obol/{first['component']}/{first['scope']}"
        return val

    def _on_import_selected(self) -> None:
        selected = self.asset_tree.selectedItems()
        if not selected:
            QMessageBox.information(self, "Load into Resolve", "Select one or more files first.")
            return
        entries = [it.data(0, Qt.ItemDataRole.UserRole) for it in selected if it.data(0, Qt.ItemDataRole.UserRole)]
        paths = [Path(e["file_path"]) for e in entries]
        bin_path = self._resolved_bin_path(entries)
        append_tl = self.append_tl_chk.isChecked()
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

        if self.result_file is not None:
            _write_result_json(
                self.result_file,
                {
                    "action": "import_media",
                    "params": {
                        "file_paths": [str(p.resolve()) for p in paths],
                        "bin_path": bin_path,
                        "append_to_timeline": append_tl,
                        "asset_meta": meta,
                    },
                },
            )
            self.accept()
            return

        if not self.bridge.is_connected():
            QMessageBox.warning(
                self,
                "Resolve Offline",
                "DaVinci Resolve is not connected. Open Resolve or run Workspace → Scripts → Utility → Obol - Load Media into Media Pool.",
            )
            return

        try:
            res = self.bridge.import_media(paths, bin_path=bin_path, append_to_timeline=append_tl, asset_meta=meta)
            QMessageBox.information(
                self,
                "Loaded into Resolve",
                f"Imported {res.get('imported', len(paths))} clip(s) into Media Pool bin '{res.get('bin', 'Current')}'.",
            )
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "Resolve Import Failed", str(exc))

    def _populate_updates_from_clips(self, clips: list[dict[str, Any]]) -> None:
        raw_root = self.project_combo.currentData()
        if not raw_root:
            return
        proot = Path(str(raw_root))
        if not proot.is_dir():
            return

        self.updates_table.setRowCount(0)
        seen_paths: set[str] = set()
        for clip in clips:
            fpath_str = str(clip.get("file_path", ""))
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

            bin_it = QTableWidgetItem(str(clip.get("bin_path", "") or "Master"))
            clip_it = QTableWidgetItem(str(clip.get("name", "") or Path(fpath_str).name))
            asset_it = QTableWidgetItem(f"{info['component']} / {info['kind']} / {info['asset_key']}")
            ver_it = QTableWidgetItem(f"v{info['current_version']:03d} → v{info['latest_version']:03d}")
            status_it = QTableWidgetItem("Update Available" if info["update_available"] else "Up to Date")
            status_it.setForeground(QColor("#fbbf24") if info["update_available"] else QColor("#4ade80"))

            self.updates_table.setItem(row_idx, 0, chk)
            self.updates_table.setItem(row_idx, 1, bin_it)
            self.updates_table.setItem(row_idx, 2, clip_it)
            self.updates_table.setItem(row_idx, 3, asset_it)
            self.updates_table.setItem(row_idx, 4, ver_it)
            self.updates_table.setItem(row_idx, 5, status_it)

    def scan_media_pool_updates(self) -> None:
        self._refresh_bridge_pill()
        if "media_pool_clips" in self.context_data and self.result_file is not None:
            self._populate_updates_from_clips(list(self.context_data.get("media_pool_clips") or []))
            return
        if not self.bridge.is_connected():
            QMessageBox.warning(self, "Resolve Offline", "Connect to DaVinci Resolve first to scan the Media Pool.")
            return
        try:
            clips = self.bridge.query_media_pool()
        except Exception as exc:
            QMessageBox.warning(self, "Media Pool Scan Failed", str(exc))
            return
        self._populate_updates_from_clips(clips)

    def _on_apply_updates(self) -> None:
        replacements: dict[str, str] = {}
        for row in range(self.updates_table.rowCount()):
            chk = self.updates_table.item(row, 0)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                info = chk.data(Qt.ItemDataRole.UserRole)
                if info and info.get("update_available"):
                    replacements[str(info["current_file"])] = str(info["latest_file"])

        if not replacements:
            QMessageBox.information(self, "Media Pool Updates", "No outdated clips are checked for update.")
            return

        if self.result_file is not None:
            _write_result_json(
                self.result_file,
                {
                    "action": "replace_media_pool_clips",
                    "params": {"replacements": replacements},
                },
            )
            self.accept()
            return

        try:
            res = self.bridge.replace_media_pool_clips(replacements)
            QMessageBox.information(
                self,
                "Updated Media Pool Clips",
                f"Replaced {res.get('replaced', 0)} clip(s) with latest Obol versions.",
            )
            self.scan_media_pool_updates()
        except Exception as exc:
            QMessageBox.warning(self, "Update Failed", str(exc))


class ResolveExportPublishDialog(QDialog):
    """Export and publish renders, Quick Exports, stills, or existing bounces from DaVinci Resolve."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        workspace: ObolWorkspace,
        bridge: ResolveBridgeClient,
        context_data: Optional[dict[str, Any]] = None,
        result_file: Optional[Path] = None,
        default_project: str = "",
        default_component: str = "",
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.workspace = workspace
        self.bridge = bridge
        self.context_data = dict(context_data or {})
        self.result_file = result_file
        self.published_bundle: Optional[VersionBundle] = None

        st = self.bridge.get_state() if not self.context_data else None
        self.resolve_project = str(
            self.context_data.get("project_name", "") or (st.project_name if st else "")
        )
        self.resolve_timeline = str(
            self.context_data.get("timeline_name", "") or (st.timeline_name if st else "")
        )
        render_presets = list(
            self.context_data.get("render_presets") or (st.render_presets if st else []) or []
        )
        qe_presets = list(
            self.context_data.get("quick_export_presets") or (st.quick_export_presets if st else []) or []
        )

        binding = self.bridge.get_project_binding(self.resolve_project) if self.resolve_project else None
        init_proj = default_project or (binding["project_name"] if binding else "") or self.hub.config.last_active_project
        init_comp = default_component or (binding["component_path"] if binding else "")

        self.setWindowTitle("Obol — Export && Publish from DaVinci Resolve")
        self.resize(670, 620)

        layout = QVBoxLayout(self)

        banner = QFrame()
        banner.setObjectName("PanelCard")
        b_layout = QVBoxLayout(banner)
        b_lbl = QLabel(
            f"Resolve Project: <b>{self.resolve_project or '(Untitled)'}</b>  •  "
            f"Timeline: <b>{self.resolve_timeline or '(None)'}</b>"
        )
        b_layout.addWidget(b_lbl)

        ctx_form = QFormLayout()
        self.project_combo = QComboBox()
        for p in self.hub.list_projects():
            self.project_combo.addItem(p.name, p.local_path)
        self.component_combo = QComboBox()
        self.working_ver_combo = QComboBox()
        ctx_form.addRow("Target Obol Project:", self.project_combo)
        ctx_form.addRow("Target Component:", self.component_combo)
        ctx_form.addRow("Source Working:", self.working_ver_combo)
        b_layout.addLayout(ctx_form)
        layout.addWidget(banner)

        deliv_form = QFormLayout()
        self.deliverable_combo = QComboBox()
        self.deliverable_combo.setEditable(True)
        self.target_ver_combo = QComboBox()
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("Release notes (e.g., Rough cut v2, Final grade ProRes)")
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("comma-separated tags")
        deliv_form.addRow("Deliverable Name:", self.deliverable_combo)
        deliv_form.addRow("Target Release:", self.target_ver_combo)
        deliv_form.addRow("Release Note:", self.note_edit)
        deliv_form.addRow("Tags:", self.tags_edit)
        layout.addLayout(deliv_form)

        method_row = QHBoxLayout()
        method_row.addWidget(QLabel("Export Method:"))
        self.method_combo = QComboBox()
        self.method_combo.addItem("Deliver Queue Render (Add Job & Render Now)", "deliver")
        self.method_combo.addItem("Quick Export (Current Timeline)", "quick_export")
        self.method_combo.addItem("Stage Deliver Page (Pre-Fill TargetDir)", "stage_deliver")
        self.method_combo.addItem("Export Current Frame Still", "still")
        self.method_combo.addItem("Publish Existing Rendered File(s)...", "existing")
        method_row.addWidget(self.method_combo, 1)
        layout.addLayout(method_row)

        self.method_stack = QStackedWidget()

        # Page 0: Deliver Queue Render
        page_deliver = QFrame()
        page_deliver.setObjectName("PanelCard")
        pd_form = QFormLayout(page_deliver)
        self.render_preset_combo = QComboBox()
        self.render_preset_combo.addItem("(Current Deliver Settings)", "")
        for pr in render_presets:
            self.render_preset_combo.addItem(pr, pr)
        self.bounds_combo = QComboBox()
        self.bounds_combo.addItem("Entire Timeline", True)
        self.bounds_combo.addItem("Timeline In/Out Range", False)
        self.deliver_name_edit = QLineEdit()
        pd_form.addRow("Render Preset:", self.render_preset_combo)
        pd_form.addRow("Range:", self.bounds_combo)
        pd_form.addRow("Output Filename:", self.deliver_name_edit)
        self.method_stack.addWidget(page_deliver)

        # Page 1: Quick Export
        page_qe = QFrame()
        page_qe.setObjectName("PanelCard")
        pq_form = QFormLayout(page_qe)
        self.qe_preset_combo = QComboBox()
        self.qe_preset_combo.setEditable(True)
        for qp in qe_presets:
            self.qe_preset_combo.addItem(qp, qp)
        if not qe_presets:
            self.qe_preset_combo.addItems(["H.264 Master", "H.265 Master", "ProRes 422"])
        self.qe_name_edit = QLineEdit()
        pq_form.addRow("Quick Export Preset:", self.qe_preset_combo)
        pq_form.addRow("Output Filename:", self.qe_name_edit)
        self.method_stack.addWidget(page_qe)

        # Page 2: Stage Deliver Page
        page_stage = QFrame()
        page_stage.setObjectName("PanelCard")
        ps_layout = QVBoxLayout(page_stage)
        ps_info = QLabel(
            "Pre-creates <code>published/&lt;deliverable&gt;/vNNN/</code>, switches Resolve to the "
            "<b>Deliver</b> page, and sets <code>TargetDir</code> + <code>CustomName</code> so you can "
            "fine-tune codecs before rendering. Use <b>2. Sync Exports</b> in Obol afterward."
        )
        ps_info.setWordWrap(True)
        ps_layout.addWidget(ps_info)
        ps_form = QFormLayout()
        self.stage_preset_combo = QComboBox()
        self.stage_preset_combo.addItem("(Keep Current Settings)", "")
        for pr in render_presets:
            self.stage_preset_combo.addItem(pr, pr)
        self.stage_name_edit = QLineEdit()
        self.stage_add_job_chk = QCheckBox("Also add job to Render Queue")
        ps_form.addRow("Render Preset:", self.stage_preset_combo)
        ps_form.addRow("Output Filename:", self.stage_name_edit)
        ps_form.addRow("", self.stage_add_job_chk)
        ps_layout.addLayout(ps_form)
        ps_layout.addStretch()
        self.method_stack.addWidget(page_stage)

        # Page 3: Current Frame Still
        page_still = QFrame()
        page_still.setObjectName("PanelCard")
        pst_form = QFormLayout(page_still)
        self.still_fmt_combo = QComboBox()
        self.still_fmt_combo.addItems(["png", "jpg", "tif", "exr", "dpx"])
        pst_form.addRow("Still Format:", self.still_fmt_combo)
        self.method_stack.addWidget(page_still)

        # Page 4: Existing Files
        page_existing = QFrame()
        page_existing.setObjectName("PanelCard")
        pe_layout = QVBoxLayout(page_existing)
        self.existing_files_list = QListWidget()
        pe_layout.addWidget(self.existing_files_list, 1)
        pe_btns = QHBoxLayout()
        browse_btn = QPushButton("Browse Files...")
        browse_btn.clicked.connect(self._browse_existing_files)
        rem_btn = QPushButton("Remove Selected")
        rem_btn.clicked.connect(self._remove_existing_file)
        pe_btns.addWidget(browse_btn)
        pe_btns.addWidget(rem_btn)
        pe_btns.addStretch()
        pe_layout.addLayout(pe_btns)
        self.method_stack.addWidget(page_existing)

        layout.addWidget(self.method_stack, 1)

        self.drp_snapshot_chk = QCheckBox("Also export .drp working snapshot into working/vNNN/")
        layout.addWidget(self.drp_snapshot_chk)

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

        if init_proj:
            idx = self.project_combo.findText(init_proj)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)
        self._reload_components(preselect_comp=init_comp)

    def _current_target(self) -> tuple[str, Optional[Path], str, int]:
        pname = self.project_combo.currentText()
        raw_root = self.project_combo.currentData()
        proot = Path(str(raw_root)) if raw_root else None
        comp = str(self.component_combo.currentData() or self.component_combo.currentText())
        w_ver = int(self.working_ver_combo.currentData() or 1)
        return pname, proot, comp, w_ver

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
        _, proot, comp, _ = self._current_target()
        self.working_ver_combo.clear()
        self.deliverable_combo.blockSignals(True)
        curr_deliv = self.deliverable_combo.currentText().strip() or "main"
        self.deliverable_combo.clear()
        defaults = ["main", "review", "dailies", "still"]
        if proot and comp and proot.is_dir():
            snap = load_component_snapshot(proot, comp)
            for w in reversed(snap.working_versions):
                self.working_ver_combo.addItem(w.version_label, w.version)
            if not snap.working_versions:
                self.working_ver_combo.addItem("v001 (new)", 1)
            existing = list(snap.published_deliverables.keys())
            for d in list(dict.fromkeys(existing + defaults)):
                self.deliverable_combo.addItem(d)
        else:
            self.working_ver_combo.addItem("v001", 1)
            for d in defaults:
                self.deliverable_combo.addItem(d)
        self.deliverable_combo.setCurrentText(curr_deliv)
        self.deliverable_combo.blockSignals(False)
        self._reload_target_versions()

    def _reload_target_versions(self) -> None:
        _, proot, comp, _ = self._current_target()
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
        dname = self.workspace.normalize_deliverable_name(self.deliverable_combo.currentText())
        default_fname = f"{dname}_v{next_v:03d}"
        self.deliver_name_edit.setPlaceholderText(f"Default: {default_fname}")
        self.qe_name_edit.setPlaceholderText(f"Default: {default_fname}")
        self.stage_name_edit.setPlaceholderText(f"Default: {default_fname}")

    def _browse_existing_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Select Rendered Deliverable Files")
        for f in files:
            self.existing_files_list.addItem(f)

    def _remove_existing_file(self) -> None:
        for it in self.existing_files_list.selectedItems():
            self.existing_files_list.takeItem(self.existing_files_list.row(it))

    def _on_execute(self) -> None:
        pname, proot, comp, work_ver = self._current_target()
        if not pname or not proot or not comp:
            QMessageBox.warning(self, "Export & Publish", "Select a valid target Obol project and component.")
            return

        if self.resolve_project:
            self.bridge.set_project_binding(self.resolve_project, pname, comp)

        method = str(self.method_combo.currentData() or "deliver")
        deliv_name = self.workspace.normalize_deliverable_name(self.deliverable_combo.currentText())
        target_ver = self.target_ver_combo.currentData()
        comment = self.note_edit.text().strip()
        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        want_drp = self.drp_snapshot_chk.isChecked()

        # Mode: existing files
        if method == "existing":
            files = [Path(self.existing_files_list.item(i).text()) for i in range(self.existing_files_list.count())]
            if not files:
                QMessageBox.warning(self, "Publish Files", "Add at least one file to publish.")
                return
            try:
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
                self.published_bundle = bundle
                if self.result_file is not None:
                    plan: dict[str, Any] = {"action": "noop", "params": {}}
                    if want_drp:
                        plan["drp_snapshot"] = {
                            "project_name": pname,
                            "component_path": comp,
                            "comment": f"Resolve snapshot for {deliv_name}/{bundle.version_label}",
                        }
                    _write_result_json(self.result_file, plan)
                    self.accept()
                    return
                QMessageBox.information(
                    self,
                    "Published Deliverable",
                    f"Published {comp}/published/{deliv_name}/{bundle.version_label} ({len(bundle.files)} file(s))!",
                )
                self.accept()
            except Exception as exc:
                QMessageBox.warning(self, "Publish Failed", str(exc))
            return

        deliv_path, ver_num, export_dir = self.workspace.prepare_publish_export_folder(
            proot, comp, deliverable_name=deliv_name, target_version=target_ver
        )
        default_cname = f"{deliv_path}_v{ver_num:03d}"

        if method == "deliver":
            action = "render_deliver_job"
            params: dict[str, Any] = {
                "output_dir": str(export_dir.resolve()),
                "custom_name": self.deliver_name_edit.text().strip() or default_cname,
                "preset_name": str(self.render_preset_combo.currentData() or ""),
                "select_all_frames": bool(self.bounds_combo.currentData()),
            }
            need_sync = True
        elif method == "quick_export":
            action = "render_quick_export"
            params = {
                "output_dir": str(export_dir.resolve()),
                "preset_name": str(self.qe_preset_combo.currentText() or "").strip(),
                "custom_name": self.qe_name_edit.text().strip() or default_cname,
            }
            need_sync = True
        elif method == "stage_deliver":
            action = "stage_deliver_page"
            params = {
                "output_dir": str(export_dir.resolve()),
                "preset_name": str(self.stage_preset_combo.currentData() or ""),
                "custom_name": self.stage_name_edit.text().strip() or default_cname,
                "add_job": self.stage_add_job_chk.isChecked(),
            }
            need_sync = False
        else:  # still
            fmt = self.still_fmt_combo.currentText().strip().lstrip(".") or "png"
            action = "export_current_still"
            params = {
                "output_file": str((export_dir / f"{default_cname}.{fmt}").resolve()),
            }
            need_sync = True

        # Launched from Resolve Lua menu script -> hand execution plan back to Lua
        if self.result_file is not None:
            plan = {"action": action, "params": params}
            if need_sync:
                plan["sync_args"] = {
                    "project_name": pname,
                    "component_path": comp,
                    "deliverable_name": deliv_path,
                    "version": ver_num,
                    "source_working_version": work_ver,
                    "comment": comment,
                    "tags": tags,
                }
            if want_drp:
                plan["drp_snapshot"] = {
                    "project_name": pname,
                    "component_path": comp,
                    "comment": f"Resolve snapshot for {deliv_path}/v{ver_num:03d}",
                }
            _write_result_json(self.result_file, plan)
            self.accept()
            return

        # Launched from Obol GUI -> execute via live bridge
        if not self.bridge.is_connected():
            if export_dir.is_dir() and not any(export_dir.iterdir()):
                export_dir.rmdir()
            QMessageBox.warning(
                self,
                "Resolve Offline",
                "DaVinci Resolve is not connected. Run Workspace → Scripts → Utility → Obol - Export and Publish inside Resolve, "
                "or start Obol - Live Bridge in Resolve.",
            )
            return

        try:
            if want_drp:
                tmp_drp = self.bridge.tmp_dir / f"{comp.split('/')[-1]}_snapshot.drp"
                self.bridge.export_project_drp(tmp_drp)
                if tmp_drp.is_file():
                    w_bundle = self.workspace.ingest_working_files(
                        project_root=proot,
                        component_path=comp,
                        source_paths=[tmp_drp],
                        comment=f"Resolve .drp snapshot before {deliv_path}/v{ver_num:03d}",
                        copy_mode="move",
                    )
                    work_ver = w_bundle.version

            self.bridge.send_command(action, params, timeout_s=1800.0)
            if not need_sync:
                QMessageBox.information(
                    self,
                    "Deliver Page Staged",
                    f"Resolve Deliver page pointed to:\n{export_dir}\n\nClick '2. Sync Exports' in Obol after rendering.",
                )
                self.accept()
                return

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
                QMessageBox.warning(self, "Export Empty", "No rendered files were produced in the export folder.")
                return
            if tags:
                self.workspace.update_published_metadata(
                    project_root=proot,
                    component_path=comp,
                    deliverable_name=deliv_path,
                    version=bundle.version,
                    tags=list(dict.fromkeys(list(bundle.tags) + tags)),
                )
            self.published_bundle = bundle
            QMessageBox.information(
                self,
                "Published Deliverable",
                f"Published {comp}/published/{deliv_path}/{bundle.version_label} ({len(bundle.files)} file(s))!",
            )
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "Export & Publish Failed", str(exc))


class ResolveIntegrationDialog(QDialog):
    """1-Click DaVinci Resolve script installer, bridge monitor, and uninstaller."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        bridge: ResolveBridgeClient,
        installer: Optional[ResolveInstaller] = None,
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.bridge = bridge
        self.installer = installer or ResolveInstaller(hub)

        self.setWindowTitle("DaVinci Resolve Integration — Scripts && Bridge")
        self.resize(640, 470)

        layout = QVBoxLayout(self)

        hdr = QHBoxLayout()
        title = QLabel("DaVinci Resolve Integration")
        title.setObjectName("SectionHeader")
        self.status_pill = QLabel()
        hdr.addWidget(title)
        hdr.addStretch()
        hdr.addWidget(self.status_pill)
        layout.addLayout(hdr)

        form = QFormLayout()
        sdir_row = QHBoxLayout()
        self.scripts_dir_edit = QLineEdit()
        sdir_browse = QPushButton("Browse...")
        sdir_browse.clicked.connect(self._browse_scripts_dir)
        sdir_row.addWidget(self.scripts_dir_edit, 1)
        sdir_row.addWidget(sdir_browse)
        form.addRow("Resolve Fusion Scripts Folder:", sdir_row)

        exe_row = QHBoxLayout()
        self.exe_edit = QLineEdit()
        exe_browse = QPushButton("Browse...")
        exe_browse.clicked.connect(self._browse_exe)
        exe_row.addWidget(self.exe_edit, 1)
        exe_row.addWidget(exe_browse)
        form.addRow("DaVinci Resolve Executable:", exe_row)
        layout.addLayout(form)

        self.save_exe_chk = QCheckBox("Save Resolve executable in Obol Software Profiles")
        self.save_exe_chk.setChecked(True)
        layout.addWidget(self.save_exe_chk)

        self.details_label = QLabel("")
        self.details_label.setObjectName("MutedText")
        self.details_label.setWordWrap(True)
        layout.addWidget(self.details_label, 1)

        btns = QHBoxLayout()
        self.install_btn = QPushButton("Install / Update Resolve Integration")
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

    def _browse_scripts_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self, "Select Resolve Fusion Scripts Directory", self.scripts_dir_edit.text()
        )
        if folder:
            self.scripts_dir_edit.setText(folder)
            self._load_status(override_dir=Path(folder))

    def _browse_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select DaVinci Resolve Executable", self.exe_edit.text())
        if path:
            self.exe_edit.setText(path)

    def _load_status(self, override_dir: Optional[Path] = None) -> None:
        sdir = override_dir or (
            Path(self.scripts_dir_edit.text().strip()) if self.scripts_dir_edit.text().strip() else detect_resolve_scripts_dir()
        )
        st = self.installer.status(scripts_dir=sdir, allow_studio_api=self.bridge.allow_studio_api)
        if not self.scripts_dir_edit.text().strip() and st["scripts_dir"]:
            self.scripts_dir_edit.setText(st["scripts_dir"])
        if not self.exe_edit.text().strip():
            self.exe_edit.setText(st["executable"] or str(detect_resolve_executable() or ""))

        if st["bridge_connected"]:
            mode_lbl = "Studio API" if st["bridge_mode"] == "studio_api" else "Lua Bridge"
            ver_txt = f" {st['resolve_version']}" if st["resolve_version"] else ""
            self.status_pill.setText(f"Connected ({mode_lbl}{ver_txt})")
            self.status_pill.setObjectName("StatusPillOnline")
        else:
            self.status_pill.setText("Resolve Offline")
            self.status_pill.setObjectName("StatusPillLocal")
        _repolish(self.status_pill)

        self.details_label.setText(
            f"Installed Utility Scripts: {len(st['scripts_installed'])} / 5\n"
            f"Deliver Menu Script: {'Installed' if st['deliver_script_installed'] else 'Not Installed'}\n"
            f"Active Resolve Project: {st['active_project'] or 'None'} | Timeline: {st['active_timeline'] or 'None'}\n\n"
            "Usage inside DaVinci Resolve (Free & Studio):\n"
            "• Workspace → Scripts → Utility → Obol - Load Media into Media Pool\n"
            "• Workspace → Scripts → Utility / Deliver → Obol - Export and Publish\n"
            "• (Optional for Obol GUI → Resolve Free push) Run 'Obol - Live Bridge' inside Resolve."
        )

    def _on_install(self) -> None:
        sdir_txt = self.scripts_dir_edit.text().strip()
        if not sdir_txt:
            QMessageBox.warning(self, "Resolve Integration", "Select DaVinci Resolve's Fusion/Scripts folder first.")
            return
        try:
            st = self.installer.install(
                scripts_dir=Path(sdir_txt),
                configure_executable=self.save_exe_chk.isChecked(),
                executable_override=self.exe_edit.text().strip(),
                allow_studio_api=self.bridge.allow_studio_api,
            )
            self._load_status(override_dir=Path(sdir_txt))
            QMessageBox.information(
                self,
                "DaVinci Resolve Integration Installed",
                f"Installed {len(st['scripts_installed'])} Utility scripts and Deliver menu script.\n\n"
                "Access them in DaVinci Resolve under:\nWorkspace → Scripts → Utility (and Deliver).",
            )
        except Exception as exc:
            QMessageBox.warning(self, "Install Failed", str(exc))

    def _on_uninstall(self) -> None:
        sdir_txt = self.scripts_dir_edit.text().strip()
        sdir = Path(sdir_txt) if sdir_txt else None
        self.installer.uninstall(scripts_dir=sdir, allow_studio_api=self.bridge.allow_studio_api)
        self._load_status(override_dir=sdir)
        QMessageBox.information(self, "Uninstalled", "Removed Obol DaVinci Resolve scripts.")


def launch_resolve_dialog(
    hub: ObolHub,
    mode: str,
    context_file: str = "",
    result_file: str = "",
    project: str = "",
    component: str = "",
) -> int:
    """Standalone PySide6 entrypoint for DaVinci Resolve Lua scripts (`obol resolve ui --mode ...`)."""
    app = QApplication.instance() or QApplication(sys.argv)
    icon_path = Path(__file__).parent / "assets" / "obol_icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    apply_studio_dark_theme(app)

    ctx_data: dict[str, Any] = {}
    if context_file and Path(context_file).is_file():
        try:
            loaded = json.loads(Path(context_file).read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                ctx_data = loaded
        except (OSError, json.JSONDecodeError):
            ctx_data = {}

    res_path = Path(result_file) if result_file else None
    workspace = ObolWorkspace(hub)
    bridge = ResolveBridgeClient(hub, allow_studio_api=(QApplication.platformName() != "offscreen"))

    if mode == "import":
        dlg: QDialog = ResolveImportDialog(
            None,
            hub,
            workspace,
            bridge,
            context_data=ctx_data,
            result_file=res_path,
            default_project=project,
            default_component=component,
        )
    elif mode == "publish":
        dlg = ResolveExportPublishDialog(
            None,
            hub,
            workspace,
            bridge,
            context_data=ctx_data,
            result_file=res_path,
            default_project=project,
            default_component=component,
        )
    else:
        dlg = ResolveIntegrationDialog(None, hub, bridge)

    dlg.exec()
    return 0
