"""Focused PySide6 companion dialogs for FilmCraft integration (Import/Update, Export/Publish, Setup)."""

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
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QSpinBox,
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
from obol.plugins.filmcraft_bridge import FilmCraftBridgeClient


def _repolish(widget: QWidget) -> None:
    st = widget.style()
    if st is not None:
        st.unpolish(widget)
        st.polish(widget)
    widget.update()


def _resolve_working_fcproj_paths(
    workspace: ObolWorkspace,
    proot: Path,
    component_path: str,
) -> list[tuple[int, str, Path]]:
    """Returns [(version, label, abs_path)] for existing .fcproj files in a component, newest first."""
    if not proot.is_dir() or not component_path:
        return []
    snap = load_component_snapshot(proot, component_path)
    out: list[tuple[int, str, Path]] = []
    for w in reversed(snap.working_versions):
        if not w.primary_file:
            continue
        p = (proot / component_path / w.primary_file).resolve()
        if p.suffix.lower() == ".fcproj" and p.is_file():
            out.append((w.version, f"{w.version_label} — {p.name}", p))
    return out


class FilmCraftImportDialog(QDialog):
    """Import repo/ & published/ media into FilmCraft (Live Bridge or Headless .fcproj) and update outdated clips."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        workspace: ObolWorkspace,
        bridge: FilmCraftBridgeClient,
        active_fcproj: str = "",
        default_project: str = "",
        default_component: str = "",
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.workspace = workspace
        self.bridge = bridge
        self.active_fcproj = Path(active_fcproj).resolve() if active_fcproj else None

        self.setWindowTitle("Obol — Import && Update Media in FilmCraft")
        self.resize(900, 600)

        ctx = self.workspace.resolve_working_file_context(self.active_fcproj) if self.active_fcproj else None
        init_project = default_project or (ctx.project_name if ctx else "") or self.hub.config.last_active_project
        self._init_component = default_component or (ctx.component_path if ctx else "")

        layout = QVBoxLayout(self)

        hdr = QHBoxLayout()
        target_txt = str(self.active_fcproj.name) if self.active_fcproj else "Live Session / Select Working .fcproj"
        hdr.addWidget(QLabel(f"FilmCraft Target: <b>{target_txt}</b>"), 1)
        self.bridge_pill = QLabel()
        self._refresh_bridge_pill()
        hdr.addWidget(self.bridge_pill)
        layout.addLayout(hdr)

        target_bar = QHBoxLayout()
        target_bar.addWidget(QLabel("Target .fcproj:"))
        self.target_fcproj_combo = QComboBox()
        target_bar.addWidget(self.target_fcproj_combo, 1)
        layout.addLayout(target_bar)

        self.tabs = QTabWidget()

        # Tab 1: Import Media
        tab_imp = QWidget()
        ti_layout = QVBoxLayout(tab_imp)

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
        self.search_edit.setPlaceholderText("Filter video, audio, stills, captions…")
        self.search_edit.setClearButtonEnabled(True)
        filter_row.addWidget(self.search_edit, 1)
        ti_layout.addLayout(filter_row)

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
        ti_layout.addWidget(self.asset_tree, 1)

        opts_form = QFormLayout()
        self.bin_combo = QComboBox()
        self.bin_combo.setEditable(True)
        self.bin_combo.addItem("(Root Bin)", "")
        self.bin_combo.addItem("Auto: Obol/<component>/<scope>", "__auto__")
        opts_form.addRow("Target Project Bin:", self.bin_combo)
        ti_layout.addLayout(opts_form)

        bottom_imp = QHBoxLayout()
        self.place_tl_chk = QCheckBox("Also place imported clip(s) on active timeline")
        bottom_imp.addWidget(self.place_tl_chk)
        bottom_imp.addStretch()
        self.import_btn = QPushButton("Import into FilmCraft")
        self.import_btn.setObjectName("PrimaryButton")
        self.import_btn.clicked.connect(self._on_import_selected)
        bottom_imp.addWidget(self.import_btn)
        ti_layout.addLayout(bottom_imp)
        self.tabs.addTab(tab_imp, "Import Media")

        # Tab 2: Project Media Updates
        tab_upd = QWidget()
        tu_layout = QVBoxLayout(tab_upd)
        tu_top = QHBoxLayout()
        tu_top.addWidget(QLabel("Scan FilmCraft project clips for newer versions in repo/ or published/:"))
        tu_top.addStretch()
        self.scan_btn = QPushButton("Scan Project Media")
        self.scan_btn.clicked.connect(self.scan_project_updates)
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
        self.apply_updates_btn = QPushButton("Update Selected Media to Latest")
        self.apply_updates_btn.setObjectName("PrimaryButton")
        self.apply_updates_btn.clicked.connect(self._on_apply_updates)
        tu_btns.addWidget(self.apply_updates_btn)
        tu_layout.addLayout(tu_btns)
        self.tabs.addTab(tab_upd, "Project Media Updates")

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

        self.project_combo.currentIndexChanged.connect(self._on_project_changed)
        self.scope_combo.currentIndexChanged.connect(self._populate_asset_tree)
        self.search_edit.textChanged.connect(self._populate_asset_tree)
        self._reload_target_fcproj_choices()
        self._populate_asset_tree()

    def _refresh_bridge_pill(self) -> None:
        if self.bridge.is_live_bridge_listening():
            self.bridge_pill.setText(f"FilmCraft Live ({self.bridge.control_host}:{self.bridge.control_port})")
            self.bridge_pill.setObjectName("StatusPillOnline")
        elif self.bridge.resolve_cli_executable():
            self.bridge_pill.setText("Headless CLI Ready")
            self.bridge_pill.setObjectName("StatusPillLocal")
        else:
            self.bridge_pill.setText("FilmCraft CLI Missing")
            self.bridge_pill.setObjectName("StatusPillOffline")
        _repolish(self.bridge_pill)

    def _on_project_changed(self) -> None:
        self._reload_target_fcproj_choices()
        self._populate_asset_tree()

    def _reload_target_fcproj_choices(self) -> None:
        self.target_fcproj_combo.clear()
        if self.bridge.is_live_bridge_listening():
            self.target_fcproj_combo.addItem("(Active Live FilmCraft Session)", "__live__")
        if self.active_fcproj and self.active_fcproj.is_file():
            self.target_fcproj_combo.addItem(f"{self.active_fcproj.name} (selected)", str(self.active_fcproj))
        raw_root = self.project_combo.currentData()
        if raw_root and Path(str(raw_root)).is_dir():
            proot = Path(str(raw_root))
            all_comps = discover_project_components(proot)
            if self._init_component and self._init_component in all_comps:
                comps = [self._init_component] + [c for c in all_comps if c != self._init_component]
            else:
                comps = all_comps
            for comp in comps:
                for _, lbl, p in _resolve_working_fcproj_paths(self.workspace, proot, comp):
                    p_str = str(p)
                    if self.target_fcproj_combo.findData(p_str) < 0:
                        self.target_fcproj_combo.addItem(f"{comp} / {lbl}", p_str)

    def _selected_target(self) -> tuple[Optional[Path], Optional[bool]]:
        data = str(self.target_fcproj_combo.currentData() or "")
        if data == "__live__":
            return None, True
        if data:
            return Path(data), False
        return None, None

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
        if data is not None and txt in ("(Root Bin)", "Auto: Obol/<component>/<scope>"):
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
            QMessageBox.information(self, "Import into FilmCraft", "Select one or more files first.")
            return
        target_path, use_bridge = self._selected_target()
        if target_path is None and use_bridge is not True:
            QMessageBox.warning(
                self,
                "No FilmCraft Target",
                "Select a working .fcproj file or launch FilmCraft with --control.",
            )
            return

        entries = [it.data(0, Qt.ItemDataRole.UserRole) for it in selected if it.data(0, Qt.ItemDataRole.UserRole)]
        paths = [Path(e["file_path"]) for e in entries]
        bin_path = self._resolved_bin_path(entries)
        place_tl = self.place_tl_chk.isChecked()
        try:
            res = self.bridge.import_media(
                paths,
                bin_path=bin_path,
                place_on_timeline=place_tl,
                project_path=target_path,
                use_bridge=use_bridge,
            )
            QMessageBox.information(
                self,
                "Imported into FilmCraft",
                f"Imported {res.get('imported', len(paths))} item(s) into bin '{res.get('bin', 'Root')}'.",
            )
            self.accept()
        except Exception as exc:
            QMessageBox.warning(self, "FilmCraft Import Failed", str(exc))

    def scan_project_updates(self) -> None:
        self._refresh_bridge_pill()
        raw_root = self.project_combo.currentData()
        if not raw_root:
            return
        proot = Path(str(raw_root))
        if not proot.is_dir():
            return
        target_path, use_bridge = self._selected_target()
        if target_path is None and use_bridge is not True:
            QMessageBox.warning(self, "Scan Project Media", "Select a working .fcproj file or connect to FilmCraft.")
            return
        try:
            clips = self.bridge.list_media_items(project_path=target_path, use_bridge=use_bridge)
        except Exception as exc:
            QMessageBox.warning(self, "Media Scan Failed", str(exc))
            return

        self.updates_table.setRowCount(0)
        for clip in clips:
            fpath_str = str(clip.get("file_path") or "")
            if not fpath_str:
                continue
            info = self.workspace.find_newer_asset_version(proot, Path(fpath_str))
            if info is None:
                continue
            row_idx = self.updates_table.rowCount()
            self.updates_table.insertRow(row_idx)

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled)
            chk.setCheckState(Qt.CheckState.Checked if info["update_available"] else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, (int(clip["id"]), info))

            bin_it = QTableWidgetItem(str(clip.get("bin_path") or "Root"))
            clip_it = QTableWidgetItem(str(clip.get("name") or Path(fpath_str).name))
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

    def _on_apply_updates(self) -> None:
        target_path, use_bridge = self._selected_target()
        if target_path is None and use_bridge is not True:
            return
        replacements: dict[int, Path] = {}
        for row in range(self.updates_table.rowCount()):
            chk = self.updates_table.item(row, 0)
            if chk and chk.checkState() == Qt.CheckState.Checked:
                payload = chk.data(Qt.ItemDataRole.UserRole)
                if isinstance(payload, tuple) and len(payload) == 2:
                    item_id, info = payload
                    if info and info.get("update_available"):
                        replacements[int(item_id)] = Path(str(info["latest_file"]))

        if not replacements:
            QMessageBox.information(self, "Project Media Updates", "No outdated clips are checked for update.")
            return
        try:
            res = self.bridge.replace_media_items(
                replacements,
                project_path=target_path,
                use_bridge=use_bridge,
                keep_relative=True,
            )
            QMessageBox.information(
                self,
                "Updated Project Media",
                f"Replaced {res.get('replaced', 0)} clip(s) with latest Obol versions.",
            )
            self.scan_project_updates()
        except Exception as exc:
            QMessageBox.warning(self, "Update Failed", str(exc))


class FilmCraftExportPublishDialog(QDialog):
    """Export media, render stills, or export interchange files from FilmCraft and auto-publish into Obol."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        workspace: ObolWorkspace,
        bridge: FilmCraftBridgeClient,
        active_fcproj: str = "",
        default_project: str = "",
        default_component: str = "",
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.workspace = workspace
        self.bridge = bridge
        self.active_fcproj = Path(active_fcproj).resolve() if active_fcproj else None
        self.published_bundle: Optional[VersionBundle] = None
        self._presets: list[dict[str, Any]] = []

        self.setWindowTitle("Obol — Export && Publish from FilmCraft")
        self.resize(700, 620)

        layout = QVBoxLayout(self)

        banner = QFrame()
        banner.setObjectName("PanelCard")
        b_layout = QVBoxLayout(banner)

        hdr_row = QHBoxLayout()
        hdr_row.addWidget(QLabel("<b>FilmCraft Render &amp; Publish</b>"), 1)
        self.bridge_pill = QLabel()
        self._refresh_bridge_pill()
        hdr_row.addWidget(self.bridge_pill)
        b_layout.addLayout(hdr_row)

        ctx_form = QFormLayout()
        self.project_combo = QComboBox()
        for p in self.hub.list_projects():
            self.project_combo.addItem(p.name, p.local_path)
        self.component_combo = QComboBox()
        self.working_ver_combo = QComboBox()
        ctx_form.addRow("Target Obol Project:", self.project_combo)
        ctx_form.addRow("Target Component:", self.component_combo)
        ctx_form.addRow("Source Working (.fcproj):", self.working_ver_combo)
        b_layout.addLayout(ctx_form)
        layout.addWidget(banner)

        deliv_form = QFormLayout()
        self.deliverable_combo = QComboBox()
        self.deliverable_combo.setEditable(True)
        self.target_ver_combo = QComboBox()
        self.note_edit = QLineEdit()
        self.note_edit.setPlaceholderText("Release notes (e.g., Rough cut v2, H.264 review)")
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("comma-separated tags")
        deliv_form.addRow("Deliverable Name:", self.deliverable_combo)
        deliv_form.addRow("Target Release:", self.target_ver_combo)
        deliv_form.addRow("Release Note:", self.note_edit)
        deliv_form.addRow("Tags:", self.tags_edit)
        layout.addLayout(deliv_form)

        seq_row = QHBoxLayout()
        seq_row.addWidget(QLabel("Sequence:"))
        self.sequence_combo = QComboBox()
        self.refresh_seq_btn = QPushButton("↻ Refresh")
        self.refresh_seq_btn.setObjectName("SubtleToolButton")
        self.refresh_seq_btn.clicked.connect(self._reload_sequences)
        seq_row.addWidget(self.sequence_combo, 1)
        seq_row.addWidget(self.refresh_seq_btn)
        layout.addLayout(seq_row)

        method_row = QHBoxLayout()
        method_row.addWidget(QLabel("Export Mode:"))
        self.method_combo = QComboBox()
        self.method_combo.addItem("Media Export (Preset or Format)", "media")
        self.method_combo.addItem("Render Still Frame (PNG)", "still")
        self.method_combo.addItem("Interchange Export (XML / FCPXML / EDL / OTIO / AAF / OMF)", "interchange")
        method_row.addWidget(self.method_combo, 1)
        layout.addLayout(method_row)

        self.method_stack = QStackedWidget()

        # Page 0: Media Export
        page_media = QFrame()
        page_media.setObjectName("PanelCard")
        pm_form = QFormLayout(page_media)
        self.preset_combo = QComboBox()
        self.preset_combo.addItem("(Custom Format Below)", "")
        self.format_combo = QComboBox()
        self.format_combo.addItems(["h264", "hevc", "prores", "dnxhr", "wav", "mp3", "flac", "png", "exr", "gif"])
        self.range_combo = QComboBox()
        self.range_combo.addItems(["entire", "inOut", "workArea", "custom"])
        range_custom_row = QHBoxLayout()
        self.start_sec_spin = QDoubleSpinBox()
        self.start_sec_spin.setRange(0.0, 86400.0)
        self.start_sec_spin.setDecimals(2)
        self.end_sec_spin = QDoubleSpinBox()
        self.end_sec_spin.setRange(0.0, 86400.0)
        self.end_sec_spin.setDecimals(2)
        self.end_sec_spin.setValue(10.0)
        range_custom_row.addWidget(QLabel("Start (s):"))
        range_custom_row.addWidget(self.start_sec_spin)
        range_custom_row.addWidget(QLabel("End (s):"))
        range_custom_row.addWidget(self.end_sec_spin)
        self.scale_combo = QComboBox()
        self.scale_combo.addItem("100% (1.0)", 1.0)
        self.scale_combo.addItem("50% (0.5)", 0.5)
        self.scale_combo.addItem("25% (0.25)", 0.25)
        self.output_name_edit = QLineEdit()
        self.include_audio_chk = QCheckBox("Include audio track")
        self.include_audio_chk.setChecked(True)
        self.allow_offline_chk = QCheckBox("Allow offline placeholder media during render")

        pm_form.addRow("Export Preset:", self.preset_combo)
        pm_form.addRow("Format (when no preset):", self.format_combo)
        pm_form.addRow("Range:", self.range_combo)
        pm_form.addRow("Custom Range:", range_custom_row)
        pm_form.addRow("Resolution Scale:", self.scale_combo)
        pm_form.addRow("Output Filename Base:", self.output_name_edit)
        pm_form.addRow("", self.include_audio_chk)
        pm_form.addRow("", self.allow_offline_chk)
        self.method_stack.addWidget(page_media)

        # Page 1: Render Still
        page_still = QFrame()
        page_still.setObjectName("PanelCard")
        ps_form = QFormLayout(page_still)
        self.still_seconds_spin = QDoubleSpinBox()
        self.still_seconds_spin.setRange(0.0, 86400.0)
        self.still_seconds_spin.setDecimals(2)
        self.still_scale_combo = QComboBox()
        self.still_scale_combo.addItem("100% (1.0)", 1.0)
        self.still_scale_combo.addItem("50% (0.5)", 0.5)
        self.still_allow_offline_chk = QCheckBox("Allow offline placeholder media")
        ps_form.addRow("Frame Time (seconds):", self.still_seconds_spin)
        ps_form.addRow("Scale:", self.still_scale_combo)
        ps_form.addRow("", self.still_allow_offline_chk)
        self.method_stack.addWidget(page_still)

        # Page 2: Interchange
        page_inter = QFrame()
        page_inter.setObjectName("PanelCard")
        pi_form = QFormLayout(page_inter)
        self.inter_fmt_combo = QComboBox()
        self.inter_fmt_combo.addItems(["xml", "fcpxml", "edl", "otio", "aaf", "omf"])
        pi_form.addRow("Interchange Format:", self.inter_fmt_combo)
        self.method_stack.addWidget(page_inter)

        layout.addWidget(self.method_stack, 1)

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
        self.preset_combo.currentIndexChanged.connect(self._on_preset_changed)
        self.project_combo.currentIndexChanged.connect(self._reload_components)
        self.component_combo.currentIndexChanged.connect(self._reload_component_details)
        self.working_ver_combo.currentIndexChanged.connect(self._reload_sequences)
        self.deliverable_combo.currentTextChanged.connect(self._reload_target_versions)

        self._load_presets()
        if default_project:
            idx = self.project_combo.findText(default_project)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)
        elif self.hub.config.last_active_project:
            idx = self.project_combo.findText(self.hub.config.last_active_project)
            if idx >= 0:
                self.project_combo.setCurrentIndex(idx)
        self._reload_components(preselect_comp=default_component)

    def _refresh_bridge_pill(self) -> None:
        if self.bridge.is_live_bridge_listening():
            self.bridge_pill.setText(f"FilmCraft Live ({self.bridge.control_host}:{self.bridge.control_port})")
            self.bridge_pill.setObjectName("StatusPillOnline")
        elif self.bridge.resolve_cli_executable():
            self.bridge_pill.setText("Headless CLI Ready")
            self.bridge_pill.setObjectName("StatusPillLocal")
        else:
            self.bridge_pill.setText("FilmCraft CLI Missing")
            self.bridge_pill.setObjectName("StatusPillOffline")
        _repolish(self.bridge_pill)

    def _load_presets(self) -> None:
        self._presets = []
        if not self.bridge.resolve_cli_executable():
            return
        try:
            self._presets = self.bridge.list_export_presets()
        except Exception:
            self._presets = []
        for p in self._presets:
            name = str(p.get("name") or "").strip()
            if not name:
                continue
            ext = str(p.get("extension") or "").strip()
            cat = str(p.get("category") or "").strip()
            lbl = f"{name} (.{ext})" if ext else name
            if cat:
                lbl = f"[{cat}] {lbl}"
            self.preset_combo.addItem(lbl, p)

    def _on_preset_changed(self) -> None:
        pdata = self.preset_combo.currentData()
        has_preset = isinstance(pdata, dict)
        self.format_combo.setEnabled(not has_preset)

    def _current_target(self) -> tuple[str, Optional[Path], str, int, Optional[Path]]:
        pname = self.project_combo.currentText()
        raw_root = self.project_combo.currentData()
        proot = Path(str(raw_root)) if raw_root else None
        comp = str(self.component_combo.currentData() or self.component_combo.currentText())
        w_data = self.working_ver_combo.currentData()
        if isinstance(w_data, tuple) and len(w_data) == 2:
            w_ver = int(w_data[0])
            fcproj_path = Path(str(w_data[1])) if w_data[1] else None
        else:
            w_ver = 1
            fcproj_path = self.active_fcproj
        return pname, proot, comp, w_ver, fcproj_path

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
        _, proot, comp, _, _ = self._current_target()
        self.working_ver_combo.blockSignals(True)
        self.working_ver_combo.clear()
        self.deliverable_combo.blockSignals(True)
        curr_deliv = self.deliverable_combo.currentText().strip() or "main"
        self.deliverable_combo.clear()
        defaults = ["main", "review", "dailies", "still", "interchange"]
        if proot and comp and proot.is_dir():
            snap = load_component_snapshot(proot, comp)
            for w in reversed(snap.working_versions):
                fc_abs = (proot / comp / w.primary_file).resolve() if w.primary_file else None
                fc_str = str(fc_abs) if (fc_abs and fc_abs.is_file()) else ""
                lbl = f"{w.version_label} ({Path(w.primary_file).name})" if w.primary_file else w.version_label
                self.working_ver_combo.addItem(lbl, (w.version, fc_str))
            if not snap.working_versions:
                self.working_ver_combo.addItem("v001", (1, ""))
            existing = list(snap.published_deliverables.keys())
            for d in list(dict.fromkeys(existing + defaults)):
                self.deliverable_combo.addItem(d)
        else:
            self.working_ver_combo.addItem("v001", (1, ""))
            for d in defaults:
                self.deliverable_combo.addItem(d)
        self.working_ver_combo.blockSignals(False)
        self.deliverable_combo.setCurrentText(curr_deliv)
        self.deliverable_combo.blockSignals(False)
        self._reload_target_versions()
        self._reload_sequences()

    def _reload_target_versions(self) -> None:
        _, proot, comp, _, _ = self._current_target()
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
        self.output_name_edit.setPlaceholderText(f"Default: {dname}_v{next_v:03d}")

    def _reload_sequences(self) -> None:
        self.sequence_combo.clear()
        self.sequence_combo.addItem("(Active Sequence)", None)
        _, _, _, _, fcproj_path = self._current_target()
        use_live = self.bridge.is_live_bridge_listening()
        proj_arg = None if use_live else fcproj_path
        if not use_live and (proj_arg is None or not proj_arg.is_file()):
            return
        if not self.bridge.resolve_cli_executable():
            return
        try:
            seqs = self.bridge.list_sequences(project_path=proj_arg, use_bridge=use_live)
            for s in seqs:
                sid = s.get("id")
                sname = str(s.get("name") or f"Sequence {sid}")
                suffix = " (active)" if s.get("active") else ""
                self.sequence_combo.addItem(f"{sname}{suffix}", sid)
        except Exception:
            pass

    @staticmethod
    def _extension_for_format(fmt: str) -> str:
        mapping = {
            "h264": "mp4",
            "hevc": "mp4",
            "prores": "mov",
            "dnxhr": "mov",
            "apv": "mov",
            "mjpeg": "mov",
            "mxf-op1a": "mxf",
            "mxf-opatom": "mxf",
            "xml": "xml",
            "fcpxml": "fcpxml",
            "edl": "edl",
            "otio": "otio",
            "aaf": "aaf",
            "omf": "omf",
        }
        return mapping.get(fmt.lower(), fmt.lower())

    def _on_execute(self) -> None:
        pname, proot, comp, work_ver, fcproj_path = self._current_target()
        if not pname or not proot or not comp:
            QMessageBox.warning(self, "Export & Publish", "Select a valid target Obol project and component.")
            return

        use_live = self.bridge.is_live_bridge_listening()
        proj_arg = None if use_live else fcproj_path
        if not use_live and (proj_arg is None or not proj_arg.is_file()):
            QMessageBox.warning(
                self,
                "FilmCraft Target Missing",
                "Select an existing working .fcproj file or launch FilmCraft with --control.",
            )
            return

        deliv_name = self.workspace.normalize_deliverable_name(self.deliverable_combo.currentText())
        target_ver = self.target_ver_combo.currentData()
        comment = self.note_edit.text().strip()
        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]

        deliv_path, ver_num, export_dir = self.workspace.prepare_publish_export_folder(
            proot, comp, deliverable_name=deliv_name, target_version=target_ver
        )
        base_name = self.output_name_edit.text().strip() or f"{deliv_path}_v{ver_num:03d}"
        seq_id = self.sequence_combo.currentData()
        mode = str(self.method_combo.currentData() or "media")

        try:
            if mode == "media":
                pdata = self.preset_combo.currentData()
                preset_name = str(pdata.get("name") or "") if isinstance(pdata, dict) else ""
                fmt = "" if preset_name else self.format_combo.currentText()
                ext = str(pdata.get("extension") or "") if isinstance(pdata, dict) else self._extension_for_format(fmt)
                ext = ext.lstrip(".") or "mp4"
                out_file = export_dir / f"{base_name}.{ext}"
                range_mode = self.range_combo.currentText()
                self.bridge.export_media(
                    out_file,
                    project_path=proj_arg,
                    preset=preset_name,
                    fmt=fmt,
                    sequence_id=int(seq_id) if seq_id is not None else None,
                    range_mode=range_mode,
                    start_seconds=self.start_sec_spin.value() if range_mode == "custom" else None,
                    end_seconds=self.end_sec_spin.value() if range_mode == "custom" else None,
                    scale=float(self.scale_combo.currentData() or 1.0),
                    no_audio=not self.include_audio_chk.isChecked(),
                    allow_offline=self.allow_offline_chk.isChecked(),
                    use_bridge=use_live,
                )
            elif mode == "still":
                out_file = export_dir / f"{base_name}_still.png"
                self.bridge.render_still(
                    out_file,
                    seconds=self.still_seconds_spin.value(),
                    scale=float(self.still_scale_combo.currentData() or 1.0),
                    project_path=proj_arg,
                    allow_offline=self.still_allow_offline_chk.isChecked(),
                    use_bridge=use_live,
                )
            else:
                ifmt = self.inter_fmt_combo.currentText()
                ext = self._extension_for_format(ifmt)
                out_file = export_dir / f"{base_name}.{ext}"
                self.bridge.export_interchange(
                    out_file,
                    fmt=ifmt,
                    sequence_id=int(seq_id) if seq_id is not None else None,
                    project_path=proj_arg,
                    use_bridge=use_live,
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
                QMessageBox.warning(self, "Export Empty", "No output file was produced in the export folder.")
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
            if export_dir.is_dir() and not any(export_dir.iterdir()):
                export_dir.rmdir()
            QMessageBox.warning(self, "FilmCraft Export & Publish Failed", str(exc))


def launch_filmcraft_dialog(
    hub: ObolHub,
    mode: str,
    active_fcproj: str = "",
    project: str = "",
    component: str = "",
) -> int:
    """Standalone PySide6 entrypoint for `obol filmcraft ui --mode ...`."""
    app = QApplication.instance() or QApplication(sys.argv)
    icon_path = Path(__file__).parent / "assets" / "obol_icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    apply_studio_dark_theme(app)

    workspace = ObolWorkspace(hub)
    bridge = FilmCraftBridgeClient(hub)
    if mode == "import":
        dlg: QDialog = FilmCraftImportDialog(
            None,
            hub,
            workspace,
            bridge,
            active_fcproj=active_fcproj,
            default_project=project,
            default_component=component,
        )
    elif mode == "publish":
        dlg = FilmCraftExportPublishDialog(
            None,
            hub,
            workspace,
            bridge,
            active_fcproj=active_fcproj,
            default_project=project,
            default_component=component,
        )
    else:
        from obol.gui.integrations_dialog import IntegrationsManagerDialog

        dlg = IntegrationsManagerDialog(None, hub, initial_dcc="filmcraft")
    dlg.exec()
    return 0
