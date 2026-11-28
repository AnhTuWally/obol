"""Focused PySide6 companion dialogs for FilmCraft integration (Import/Update, Export/Publish, Setup)."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QSize, Qt
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
from obol.gui.components import (
    DCCAssetBrowserWidget,
    DialogFooter,
    repolish,
    run_with_busy_state,
)
from obol.gui.icons import get_icon
from obol.gui.theme import apply_studio_dark_theme
from obol.gui.tokens import ICON_SM, TEXT_SECONDARY
from obol.plugins.filmcraft_bridge import FilmCraftBridgeClient


_repolish = repolish


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

        self.browser = DCCAssetBrowserWidget(self, search_placeholder="Filter video, audio, stills, captions…")
        self.project_combo = self.browser.project_combo
        for p in self.hub.list_projects():
            self.project_combo.addItem(p.name, p.local_path)
        self.scope_combo = self.browser.scope_combo
        self.search_edit = self.browser.search_edit
        self.asset_tree = self.browser.asset_tree
        self.asset_tree.setHeaderLabels(["File / Asset", "Kind", "Component", "Version", "Note"])
        ti_layout.addWidget(self.browser, 1)

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
        tu_top.addWidget(QLabel("Inspect FilmCraft clips, check for updates, and version up or down:"))
        tu_top.addStretch()
        self.show_external_chk = QCheckBox("Show external / unmanaged")
        self.show_external_chk.toggled.connect(lambda _c: self.scan_project_updates())
        tu_top.addWidget(self.show_external_chk)
        self.scan_btn = QPushButton("Scan Project Media")
        self.scan_btn.clicked.connect(self.scan_project_updates)
        tu_top.addWidget(self.scan_btn)
        tu_layout.addLayout(tu_top)

        self.updates_table = QTableWidget(0, 7)
        self.updates_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.updates_table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.updates_table.setHorizontalHeaderLabels(
            ["Apply", "Bin", "Clip", "Obol Asset", "Loaded", "Status", "Target Version"]
        )
        self.updates_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.updates_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
        self.updates_table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        tu_layout.addWidget(self.updates_table, 1)

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
        self.apply_updates_btn = QPushButton("Apply Version Changes")
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

    def _set_updates_selection(self, outdated_only: bool) -> None:
        for row in range(self.updates_table.rowCount()):
            chk = self.updates_table.item(row, 0)
            if chk is None or not (chk.flags() & Qt.ItemFlag.ItemIsUserCheckable):
                continue
            payload = chk.data(Qt.ItemDataRole.UserRole)
            combo = self.updates_table.cellWidget(row, 6)
            target_ver = combo.currentData() if isinstance(combo, QComboBox) else None
            if outdated_only and isinstance(payload, tuple) and len(payload) == 2 and isinstance(target_ver, dict):
                _item_id, info = payload
                differs = int(target_ver["version"]) != int(info.get("current_version", 0))
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
            combo = self.updates_table.cellWidget(row, 6)
            if not isinstance(combo, QComboBox):
                continue
            tracked_rows.append(row)
            chk = self.updates_table.item(row, 0)
            if chk is not None and chk.checkState() == Qt.CheckState.Checked:
                checked_rows.append(row)
        return checked_rows or tracked_rows

    def _on_version_down_selected(self) -> None:
        for row in self._target_rows_for_batch():
            combo = self.updates_table.cellWidget(row, 6)
            if isinstance(combo, QComboBox) and combo.currentIndex() > 0:
                combo.setCurrentIndex(combo.currentIndex() - 1)

    def _on_set_latest_selected(self) -> None:
        for row in self._target_rows_for_batch():
            combo = self.updates_table.cellWidget(row, 6)
            if isinstance(combo, QComboBox) and combo.count() > 0:
                combo.setCurrentIndex(combo.count() - 1)

    def _on_target_version_changed(self, row_idx: int) -> None:
        chk = self.updates_table.item(row_idx, 0)
        combo = self.updates_table.cellWidget(row_idx, 6)
        status_it = self.updates_table.item(row_idx, 5)
        if not chk or not isinstance(combo, QComboBox) or not status_it:
            return
        payload = chk.data(Qt.ItemDataRole.UserRole)
        ver_entry = combo.currentData()
        if not isinstance(payload, tuple) or len(payload) != 2 or not isinstance(ver_entry, dict):
            return
        _item_id, info = payload
        curr_v = int(info["current_version"])
        targ_v = int(ver_entry["version"])
        if targ_v > curr_v:
            if info.get("update_available") and targ_v == int(info.get("latest_version", targ_v)):
                status_it.setText("Update Available")
            else:
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

    def scan_project_updates(self) -> None:
        self._refresh_bridge_pill()
        raw_root = self.project_combo.currentData()
        proot = Path(str(raw_root)) if raw_root and Path(str(raw_root)).is_dir() else None
        target_path, use_bridge = self._selected_target()
        if target_path is None and use_bridge is not True:
            QMessageBox.warning(self, "Scan Project Media", "Select a working .fcproj file or connect to FilmCraft.")
            return
        try:
            clips = self.bridge.list_media_items(project_path=target_path, use_bridge=use_bridge)
        except Exception as exc:
            QMessageBox.warning(self, "Media Scan Failed", str(exc))
            return

        include_external = hasattr(self, "show_external_chk") and self.show_external_chk.isChecked()
        self.updates_table.setRowCount(0)
        for clip in clips:
            fpath_str = str(clip.get("file_path") or "")
            if not fpath_str:
                continue
            info = self.workspace.resolve_media_file_across_projects(Path(fpath_str), preferred_project_root=proot)
            if info is None:
                if not include_external:
                    continue
                row_idx = self.updates_table.rowCount()
                self.updates_table.insertRow(row_idx)
                chk = QTableWidgetItem()
                chk.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
                exists = Path(fpath_str).is_file() and not bool(clip.get("offline", False))
                status_it = QTableWidgetItem("External" if exists else "Missing")
                status_it.setForeground(QColor("#9ca3af") if exists else QColor("#f87171"))
                self.updates_table.setItem(row_idx, 0, chk)
                self.updates_table.setItem(row_idx, 1, QTableWidgetItem(str(clip.get("bin_path") or "Root")))
                self.updates_table.setItem(row_idx, 2, QTableWidgetItem(str(clip.get("name") or Path(fpath_str).name)))
                self.updates_table.setItem(row_idx, 3, QTableWidgetItem("External (Unmanaged)"))
                self.updates_table.setItem(row_idx, 4, QTableWidgetItem("—"))
                self.updates_table.setItem(row_idx, 5, status_it)
                self.updates_table.setItem(row_idx, 6, QTableWidgetItem("—"))
                continue

            row_idx = self.updates_table.rowCount()
            self.updates_table.insertRow(row_idx)

            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemFlag.ItemIsUserCheckable | Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsSelectable)
            chk.setCheckState(Qt.CheckState.Checked if info["update_available"] else Qt.CheckState.Unchecked)
            chk.setData(Qt.ItemDataRole.UserRole, (int(clip["id"]), info))

            bin_it = QTableWidgetItem(str(clip.get("bin_path") or "Root"))
            clip_it = QTableWidgetItem(str(clip.get("name") or Path(fpath_str).name))
            asset_it = QTableWidgetItem(f"{info['component']} / {info['kind']} / {info['asset_key']}")
            loaded_it = QTableWidgetItem(f"v{info['current_version']:03d}")
            status_it = QTableWidgetItem("Update Available" if info["update_available"] else "Up to Date")
            status_it.setForeground(QColor("#fbbf24") if info["update_available"] else QColor("#4ade80"))

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

            default_ver = int(info["latest_version"]) if info.get("update_available") else int(info["current_version"])
            sel_idx = 0
            for idx, v in enumerate(versions):
                lbl = f"{v['version_label']}{' ★' if v.get('is_latest') else ''}"
                ver_combo.addItem(lbl, v)
                if int(v["version"]) == default_ver:
                    sel_idx = idx
            ver_combo.setCurrentIndex(sel_idx)
            ver_combo.currentIndexChanged.connect(lambda _idx, r=row_idx: self._on_target_version_changed(r))

            self.updates_table.setItem(row_idx, 0, chk)
            self.updates_table.setItem(row_idx, 1, bin_it)
            self.updates_table.setItem(row_idx, 2, clip_it)
            self.updates_table.setItem(row_idx, 3, asset_it)
            self.updates_table.setItem(row_idx, 4, loaded_it)
            self.updates_table.setItem(row_idx, 5, status_it)
            self.updates_table.setCellWidget(row_idx, 6, ver_combo)

    def _on_apply_updates(self) -> None:
        target_path, use_bridge = self._selected_target()
        if target_path is None and use_bridge is not True:
            return
        replacements: dict[int, Path] = {}
        for row in range(self.updates_table.rowCount()):
            chk = self.updates_table.item(row, 0)
            if not chk or chk.checkState() != Qt.CheckState.Checked:
                continue
            payload = chk.data(Qt.ItemDataRole.UserRole)
            if not (isinstance(payload, tuple) and len(payload) == 2):
                continue
            item_id, info = payload
            if not isinstance(info, dict):
                continue
            combo = self.updates_table.cellWidget(row, 6)
            ver_entry = combo.currentData() if isinstance(combo, QComboBox) else None
            if isinstance(ver_entry, dict):
                target_file = Path(str(ver_entry["file_path"])).resolve()
                target_ver = int(ver_entry["version"])
            else:
                target_file = Path(str(info["latest_file"])).resolve()
                target_ver = int(info["latest_version"])

            curr_file = Path(str(info["current_file"])).resolve()
            if target_ver == int(info["current_version"]) and target_file == curr_file:
                continue
            replacements[int(item_id)] = target_file

        if not replacements:
            QMessageBox.information(self, "Project Media Updates", "No clips with a changed target version are checked.")
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
                f"Replaced {res.get('replaced', 0)} clip(s) in FilmCraft.",
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
        self.refresh_seq_btn = QPushButton("Refresh")
        self.refresh_seq_btn.setObjectName("SubtleToolButton")
        self.refresh_seq_btn.setIcon(get_icon("refresh", TEXT_SECONDARY, ICON_SM))
        self.refresh_seq_btn.setIconSize(QSize(ICON_SM, ICON_SM))
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
        self.range_combo.addItem("Entire Sequence", "entire")
        self.range_combo.addItem("In / Out Range", "inOut")
        self.range_combo.addItem("Work Area", "workArea")
        self.range_combo.addItem("Custom Range", "custom")
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
        self.range_combo.currentIndexChanged.connect(self._on_range_changed)
        self._on_range_changed()
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

        self.export_btn = QPushButton("Export && Publish")
        self.export_btn.setObjectName("PrimaryButton")
        self.export_btn.clicked.connect(self._on_execute)
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        layout.addWidget(DialogFooter(self, cancel_button=cancel_btn, primary_button=self.export_btn))

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

    def _on_range_changed(self) -> None:
        is_custom = str(self.range_combo.currentData() or "") == "custom"
        self.start_sec_spin.setEnabled(is_custom)
        self.end_sec_spin.setEnabled(is_custom)

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

        pdata = self.preset_combo.currentData()
        fmt_choice = self.format_combo.currentText()
        range_mode = str(self.range_combo.currentData() or "entire")
        start_sec = self.start_sec_spin.value()
        end_sec = self.end_sec_spin.value()
        scale_val = float(self.scale_combo.currentData() or 1.0)
        no_audio = not self.include_audio_chk.isChecked()
        allow_offline = self.allow_offline_chk.isChecked()
        still_seconds = self.still_seconds_spin.value()
        still_scale = float(self.still_scale_combo.currentData() or 1.0)
        still_allow_offline = self.still_allow_offline_chk.isChecked()
        inter_fmt = self.inter_fmt_combo.currentText()

        def _work() -> VersionBundle:
            if mode == "media":
                preset_name = str(pdata.get("name") or "") if isinstance(pdata, dict) else ""
                fmt = "" if preset_name else fmt_choice
                ext = str(pdata.get("extension") or "") if isinstance(pdata, dict) else self._extension_for_format(fmt)
                ext = ext.lstrip(".") or "mp4"
                out_file = export_dir / f"{base_name}.{ext}"
                self.bridge.export_media(
                    out_file,
                    project_path=proj_arg,
                    preset=preset_name,
                    fmt=fmt,
                    sequence_id=int(seq_id) if seq_id is not None else None,
                    range_mode=range_mode,
                    start_seconds=start_sec if range_mode == "custom" else None,
                    end_seconds=end_sec if range_mode == "custom" else None,
                    scale=scale_val,
                    no_audio=no_audio,
                    allow_offline=allow_offline,
                    use_bridge=use_live,
                )
            elif mode == "still":
                out_file = export_dir / f"{base_name}_still.png"
                self.bridge.render_still(
                    out_file,
                    seconds=still_seconds,
                    scale=still_scale,
                    project_path=proj_arg,
                    allow_offline=still_allow_offline,
                    use_bridge=use_live,
                )
            else:
                ext = self._extension_for_format(inter_fmt)
                out_file = export_dir / f"{base_name}.{ext}"
                self.bridge.export_interchange(
                    out_file,
                    fmt=inter_fmt,
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
                raise RuntimeError("No output file was produced in the export folder.")
            if tags:
                self.workspace.update_published_metadata(
                    project_root=proot,
                    component_path=comp,
                    deliverable_name=deliv_path,
                    version=bundle.version,
                    tags=list(dict.fromkeys(list(bundle.tags) + tags)),
                )
            return bundle

        def _on_success(bundle: VersionBundle) -> None:
            self.published_bundle = bundle
            QMessageBox.information(
                self,
                "Published Deliverable",
                f"Published {comp}/published/{deliv_path}/{bundle.version_label} ({len(bundle.files)} file(s))!",
            )
            self.accept()

        def _on_error(exc: Exception) -> None:
            if export_dir.is_dir() and not any(export_dir.iterdir()):
                export_dir.rmdir()
            title = "Export Empty" if "No output file" in str(exc) else "FilmCraft Export & Publish Failed"
            QMessageBox.warning(self, title, str(exc))

        run_with_busy_state(
            self,
            _work,
            on_success=_on_success,
            on_error=_on_error,
            disable_widgets=[self.export_btn],
        )


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
