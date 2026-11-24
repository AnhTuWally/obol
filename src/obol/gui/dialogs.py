"""Dialogs for Obol GUI: Projects & Vault Manager, 3-Tier Template Wizard with Live Tree Preview, Smart Repo Ingest, Software Settings, Reaper Media Collector, and Credits Report."""

from __future__ import annotations

from collections import Counter
import csv
from pathlib import Path
import re
from typing import Optional

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from obol.core.hub import ObolHub
from obol.core.models import (
    ProjectTemplate,
    PublishItem,
    SoftwareProfile,
    TaskTemplate,
    VaultStatus,
    VersionUpStrategy,
)
from obol.core.sync import SyncEngine
from obol.core.workspace import ObolWorkspace
from obol.gui.drop_widgets import DropTableWidget, extract_local_drop_paths


class SmartIngestDialog(QDialog):
    """Focused metadata & naming dialog when ingesting single or multiple external files into repo/."""

    def __init__(
        self,
        parent: Optional[QWidget],
        source_file: Optional[Path] = None,
        suggested_asset_path: str = "",
        target_version_label: str = "v001",
        default_author: str = "",
        source_files: Optional[list[Path]] = None,
        categories: Optional[list[str]] = None,
        existing_assets: Optional[dict[str, int]] = None,
        lock_asset_name: bool = False,
        suggested_paths: Optional[list[str]] = None,
    ) -> None:
        super().__init__(parent)
        if source_files:
            self.source_files = [p for p in source_files if p]
        elif source_file is not None:
            self.source_files = [source_file]
        else:
            self.source_files = []
        self.source_file: Optional[Path] = self.source_files[0] if self.source_files else None
        self.existing_assets = dict(existing_assets or {})
        self.is_batch = len(self.source_files) > 1
        self._lock_asset_name = lock_asset_name
        self.setAcceptDrops(True)
        self.setWindowTitle(
            f"Repo Ingest — {len(self.source_files)} Files"
            if self.is_batch
            else "Repo Ingest — Name, Version & Provenance"
        )
        self.resize(640 if self.is_batch else 520, 460 if self.is_batch else 410)

        layout = QVBoxLayout(self)
        if self.is_batch:
            self.header_lbl = QLabel(f"Ingesting <b>{len(self.source_files)}</b> files into <code>repo/</code>")
        elif self.source_file is not None:
            self.header_lbl = QLabel(f"Ingesting file: <b>{self.source_file.name}</b>")
        else:
            self.header_lbl = QLabel("Name your new Repo Asset, then browse or drop a file below:")
        layout.addWidget(self.header_lbl)

        # Split suggested_asset_path into Category + Name
        norm_suggested = suggested_asset_path.replace("\\", "/").strip("/")
        if "/" in norm_suggested:
            init_cat, init_name = norm_suggested.rsplit("/", 1)
        else:
            init_cat, init_name = "", norm_suggested

        cat_choices = [""] + sorted({c.strip("/") for c in (categories or []) if c.strip("/")} | ({init_cat} if init_cat else set()))

        if self.is_batch:
            self.batch_table = QTableWidget(len(self.source_files), 4)
            self.batch_table.setHorizontalHeaderLabels(["Source File", "Name", "Category", "Target Version"])
            self.batch_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
            self.batch_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
            self.batch_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
            self.batch_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
            for idx, sf in enumerate(self.source_files):
                raw_path = (suggested_paths[idx] if suggested_paths and idx < len(suggested_paths) else sf.stem).replace("\\", "/").strip("/")
                if "/" in raw_path:
                    rcat, rname = raw_path.rsplit("/", 1)
                else:
                    rcat, rname = init_cat, raw_path
                src_item = QTableWidgetItem(sf.name)
                src_item.setFlags(src_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                name_item = QTableWidgetItem(rname)
                cat_item = QTableWidgetItem(rcat)
                ap = f"{rcat}/{rname}".strip("/") if rcat else rname
                next_v = self.existing_assets.get(ap, 0) + 1
                ver_item = QTableWidgetItem(f"v{next_v:03d}")
                ver_item.setFlags(ver_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self.batch_table.setItem(idx, 0, src_item)
                self.batch_table.setItem(idx, 1, name_item)
                self.batch_table.setItem(idx, 2, cat_item)
                self.batch_table.setItem(idx, 3, ver_item)
            self.batch_table.itemChanged.connect(self._on_batch_item_changed)
            layout.addWidget(self.batch_table)
            # Keep single-mode attributes populated for backwards-compat helpers
            self.category_combo = QComboBox()
            self.category_combo.setEditable(True)
            self.category_combo.addItems(cat_choices)
            self.category_combo.setCurrentText(init_cat)
            self.name_edit = QLineEdit(init_name)
            self.file_path_edit = QLineEdit(", ".join(p.name for p in self.source_files))
            self.version_edit = QLineEdit("Auto per asset")
            self.preview_label = QLabel("")
        else:
            form_top = QFormLayout()
            self.name_edit = QLineEdit(init_name)
            self.name_edit.setPlaceholderText("Enter asset name (e.g. lead_vocal, kick_drum)")

            self.category_combo = QComboBox()
            self.category_combo.setEditable(True)
            self.category_combo.addItems(cat_choices)
            self.category_combo.setCurrentText(init_cat)

            file_row = QHBoxLayout()
            self.file_path_edit = QLineEdit(str(self.source_file) if self.source_file else "")
            self.file_path_edit.setPlaceholderText("Drop a file here or click Browse...")
            self.file_path_edit.setReadOnly(True)
            browse_file_btn = QPushButton("Browse...")
            browse_file_btn.clicked.connect(self._browse_single_file)
            file_row.addWidget(self.file_path_edit, 1)
            file_row.addWidget(browse_file_btn)

            self.version_edit = QLineEdit(f"{target_version_label}/ (Auto-incremented)")
            self.version_edit.setReadOnly(True)
            self.preview_label = QLabel("")
            self.preview_label.setObjectName("MutedText")

            form_top.addRow("Name:", self.name_edit)
            form_top.addRow("Category (optional):", self.category_combo)
            form_top.addRow("Source File:", file_row)
            form_top.addRow("Target Version:", self.version_edit)
            form_top.addRow("Repo Target:", self.preview_label)
            layout.addLayout(form_top)

            self.category_combo.currentTextChanged.connect(self._refresh_single_preview)
            self.name_edit.textChanged.connect(self._refresh_single_preview)
            self._refresh_single_preview()

        form = QFormLayout()
        self.author_edit = QLineEdit(default_author)
        self.author_edit.setPlaceholderText("e.g. InspectorJ, Sarah Jenkins, Oliver")
        self.source_edit = QLineEdit(
            str(self.source_file.parent) if self.is_batch and self.source_file else (str(self.source_file) if self.source_file else "")
        )
        self.source_edit.setPlaceholderText("URL, Sample Pack, or Studio Session")
        self.license_combo = QComboBox()
        self.license_combo.setEditable(True)
        self.license_combo.addItems(["Original", "Royalty-Free", "CC-BY-4.0", "CC0", "Work for Hire", "Temp / Scratch Only"])
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("comma-separated tags, e.g. vocal, 128bpm, Am")
        default_comment = (
            f"Batch ingested {len(self.source_files)} files"
            if self.is_batch
            else (f"Ingested from {self.source_file.name}" if self.source_file else "")
        )
        self.comment_edit = QLineEdit(default_comment)
        self.comment_edit.setPlaceholderText("Optional version note")

        form.addRow("Original Author / Creator:", self.author_edit)
        form.addRow("Source (URL / Pack / Path):", self.source_edit)
        form.addRow("License / Usage Rights:", self.license_combo)
        form.addRow("Tags:", self.tags_edit)
        form.addRow("Version Comment:", self.comment_edit)
        layout.addLayout(form)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton(f"Ingest {len(self.source_files)} Files into repo/" if self.is_batch else "Ingest into repo/")
        ok_btn.setObjectName("PrimaryButton")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._validate_and_accept)
        btns.addWidget(cancel_btn)
        btns.addWidget(ok_btn)
        layout.addLayout(btns)

        if not self.is_batch:
            self.name_edit.setFocus()
            self.name_edit.selectAll()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        if not self.is_batch and extract_local_drop_paths(event):
            event.acceptProposedAction()
            return
        super().dragEnterEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        if not self.is_batch:
            paths = [p for p in extract_local_drop_paths(event) if p.is_file()]
            if paths:
                self.set_source_file(paths[0])
                event.acceptProposedAction()
                return
        super().dropEvent(event)

    def _browse_single_file(self) -> None:
        file_str, _ = QFileDialog.getOpenFileName(self, "Select File to Ingest into repo/")
        if file_str:
            self.set_source_file(Path(file_str))

    def set_source_file(self, path: Path) -> None:
        self.source_file = path
        self.source_files = [path]
        self.file_path_edit.setText(str(path))
        self.header_lbl.setText(f"Ingesting file: <b>{path.name}</b>")
        if not self.name_edit.text().strip() and not self._lock_asset_name:
            raw_stem = re.sub(r"_v\d+$", "", path.stem, flags=re.IGNORECASE)
            self.name_edit.setText(self._clean_slug(raw_stem, "asset"))
        if not self.source_edit.text().strip():
            self.source_edit.setText(str(path))
        if not self.comment_edit.text().strip():
            self.comment_edit.setText(f"Ingested from {path.name}")
        self._refresh_single_preview()

    def _clean_slug(self, text: str, fallback: str = "asset") -> str:
        cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", text.strip()).strip("_").lower()
        return cleaned or fallback

    def _current_asset_path(self) -> str:
        cat = "/".join(
            self._clean_slug(p, "")
            for p in self.category_combo.currentText().replace("\\", "/").split("/")
            if p.strip()
        ).strip("/")
        name = self._clean_slug(self.name_edit.text(), "asset")
        return f"{cat}/{name}" if cat else name

    def _refresh_single_preview(self) -> None:
        apath = self._current_asset_path()
        name_leaf = apath.split("/")[-1]
        if apath in self.existing_assets:
            ver_num = self.existing_assets[apath] + 1
            ver_lbl = f"v{ver_num:03d}"
            self.version_edit.setText(f"{ver_lbl}/ (Existing asset — next version)")
        else:
            ver_lbl = "v001"
            self.version_edit.setText(f"{ver_lbl}/ (New asset)")
        ext = self.source_file.suffix if self.source_file else ".ext"
        self.preview_label.setText(f"repo/{apath}/{ver_lbl}/{name_leaf}_{ver_lbl}{ext}")

    def _on_batch_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() not in (1, 2):
            return
        row = item.row()
        name_item = self.batch_table.item(row, 1)
        cat_item = self.batch_table.item(row, 2)
        ver_item = self.batch_table.item(row, 3)
        if not cat_item or not name_item or not ver_item:
            return
        cat = cat_item.text().replace("\\", "/").strip("/")
        name = self._clean_slug(name_item.text(), "asset")
        ap = f"{cat}/{name}" if cat else name
        next_v = self.existing_assets.get(ap, 0) + 1
        ver_item.setText(f"v{next_v:03d}")

    def _validate_and_accept(self) -> None:
        if not self.is_batch:
            if not self.name_edit.text().strip():
                QMessageBox.warning(self, "Validation", "Name is required.")
                self.name_edit.setFocus()
                return
            if self.source_file is None or not self.source_file.exists():
                QMessageBox.warning(self, "Validation", "Please browse or drop a source file to ingest.")
                return
        self.accept()

    def get_values(self) -> dict[str, object]:
        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        asset_path = self._current_asset_path()
        return {
            "asset_path": asset_path,
            "category": self.category_combo.currentText().replace("\\", "/").strip("/"),
            "name": asset_path.split("/")[-1],
            "author": self.author_edit.text().strip(),
            "source_info": self.source_edit.text().strip(),
            "license_info": self.license_combo.currentText().strip(),
            "tags": tags,
            "comment": self.comment_edit.text().strip(),
        }

    def get_batch_entries(self) -> list[dict[str, object]]:
        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        author = self.author_edit.text().strip()
        source_info = self.source_edit.text().strip()
        license_info = self.license_combo.currentText().strip()
        comment = self.comment_edit.text().strip()
        if not self.is_batch:
            vals = self.get_values()
            vals["source_file"] = self.source_file
            return [vals]

        entries: list[dict[str, object]] = []
        for idx, sf in enumerate(self.source_files):
            name_text = self.batch_table.item(idx, 1).text() if self.batch_table.item(idx, 1) else sf.stem
            cat_text = self.batch_table.item(idx, 2).text() if self.batch_table.item(idx, 2) else ""
            cat = "/".join(self._clean_slug(p, "") for p in cat_text.replace("\\", "/").split("/") if p.strip()).strip("/")
            name = self._clean_slug(name_text, "asset")
            apath = f"{cat}/{name}" if cat else name
            entries.append({
                "source_file": sf,
                "asset_path": apath,
                "category": cat,
                "name": name,
                "author": author,
                "source_info": str(sf) if (self.source_file and source_info == str(self.source_file.parent)) else source_info,
                "license_info": license_info,
                "tags": tags,
                "comment": comment or f"Ingested from {sf.name}",
            })
        return entries


class NewProjectWizardDialog(QDialog):
    """Create New Project dialog with 3-Tier Template Picker & Interactive Live Folder Tree Preview."""

    def __init__(self, parent: Optional[QWidget], hub: ObolHub, workspace: ObolWorkspace) -> None:
        super().__init__(parent)
        self.hub = hub
        self.workspace = workspace
        self.created_project_name: Optional[str] = None
        self.setWindowTitle("Create New Project")
        self.resize(860, 500)

        main_layout = QVBoxLayout(self)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left form: Project identity & 3-tier template pickers
        left_widget = QWidget()
        left_layout = QFormLayout(left_widget)
        self.name_edit = QLineEdit("new_project")
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("Auto-generated from name (e.g. npr)")

        root_row = QHBoxLayout()
        self.root_dir_edit = QLineEdit(str(self.hub.get_client_root()))
        browse_root_btn = QPushButton("Browse...")
        browse_root_btn.clicked.connect(self._browse_client_root)
        root_row.addWidget(self.root_dir_edit, 1)
        root_row.addWidget(browse_root_btn)
        self.remember_root_chk = QCheckBox("Remember as default Client Workspace Root")
        self.remember_root_chk.setChecked(True)

        self.proj_tpl_combo = QComboBox()
        for pt_name in self.hub.template_manager.list_project_templates().keys():
            self.proj_tpl_combo.addItem(pt_name)
        proj_tpl_row = QHBoxLayout()
        proj_tpl_row.addWidget(self.proj_tpl_combo, 1)
        edit_tpl_inline_btn = QPushButton("Configure Templates...")
        edit_tpl_inline_btn.setToolTip("Open the 3-Tier Template Manager to create, edit, or import templates")
        edit_tpl_inline_btn.clicked.connect(self._open_template_manager)
        proj_tpl_row.addWidget(edit_tpl_inline_btn)

        self.tpl_desc_label = QLabel("")
        self.tpl_desc_label.setObjectName("MutedText")
        self.tpl_desc_label.setWordWrap(True)

        self.target_path_label = QLabel("")
        self.target_path_label.setObjectName("MutedText")

        self.task_tpl_combo = QComboBox()
        for tt_name in self.hub.template_manager.list_task_templates().keys():
            self.task_tpl_combo.addItem(tt_name)

        self.seed_tpl_combo = QComboBox()
        for ft_name in self.hub.template_manager.list_file_templates().keys():
            self.seed_tpl_combo.addItem(ft_name)

        self.custom_comp_edit = QLineEdit()
        self.custom_comp_edit.setPlaceholderText("e.g. song_3/audio or sfx/foley")
        self.custom_comp_edit.returnPressed.connect(self._add_custom_component)
        comp_btn_row = QHBoxLayout()
        add_comp_btn = QPushButton("+ Add Component")
        add_comp_btn.clicked.connect(self._add_custom_component)
        apply_comp_btn = QPushButton("Apply Template to Selected")
        apply_comp_btn.setToolTip("Applies the selected Component Template & Seed File above to the highlighted component in the tree")
        apply_comp_btn.clicked.connect(self._apply_template_to_selected_component)
        remove_comp_btn = QPushButton("Remove Selected")
        remove_comp_btn.clicked.connect(self._remove_selected_component)
        comp_btn_row.addWidget(add_comp_btn)
        comp_btn_row.addWidget(apply_comp_btn)
        comp_btn_row.addWidget(remove_comp_btn)

        left_layout.addRow("Project Name:", self.name_edit)
        left_layout.addRow("Project Code (optional):", self.code_edit)
        left_layout.addRow("Client Root Path:", root_row)
        left_layout.addRow("", self.target_path_label)
        left_layout.addRow("", self.remember_root_chk)
        left_layout.addRow("1. Project Hierarchy Template:", proj_tpl_row)
        left_layout.addRow("", self.tpl_desc_label)
        left_layout.addRow("2. Component Template:", self.task_tpl_combo)
        left_layout.addRow("3. Working Seed File:", self.seed_tpl_combo)
        left_layout.addRow("Custom Component Path:", self.custom_comp_edit)
        left_layout.addRow("", comp_btn_row)

        # Right side: Live interactive folder tree preview
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        lbl = QLabel("Live Folder Tree Preview (Triads & Initial v001 Seed Files):")
        lbl.setObjectName("SectionHeader")
        self.preview_tree = QTreeWidget()
        self.preview_tree.setHeaderLabels(["Folder / Seed File", "Role / Template"])
        self.preview_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.preview_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.preview_tree.setColumnWidth(0, 230)
        right_layout.addWidget(lbl)
        right_layout.addWidget(self.preview_tree)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setSizes([350, 510])
        main_layout.addWidget(splitter)

        btns = QHBoxLayout()
        manage_tpl_btn = QPushButton("Manage Templates...")
        manage_tpl_btn.clicked.connect(self._open_template_manager)
        btns.addWidget(manage_tpl_btn)
        btns.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        create_btn = QPushButton("Initialize Project from Template")
        create_btn.setObjectName("PrimaryButton")
        create_btn.clicked.connect(self._create_project)
        btns.addWidget(cancel_btn)
        btns.addWidget(create_btn)
        main_layout.addLayout(btns)

        self._active_components: list[str] = []
        self._comp_task_overrides: dict[str, str] = {}
        self._comp_seed_overrides: dict[str, str] = {}
        self.proj_tpl_combo.currentTextChanged.connect(self._reset_components_from_template)
        self.task_tpl_combo.currentTextChanged.connect(self._on_task_template_changed)
        self.name_edit.textChanged.connect(self._refresh_preview_tree)
        self.root_dir_edit.textChanged.connect(self._refresh_target_caption)
        self._on_task_template_changed(self.task_tpl_combo.currentText())
        self._reset_components_from_template()
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def _refresh_target_caption(self) -> None:
        pname = self.name_edit.text().strip() or "new_project"
        root_txt = self.root_dir_edit.text().strip()
        dest = Path(root_txt) / pname if root_txt else Path(pname)
        self.target_path_label.setText(f"Will create: {dest}")

    def _on_task_template_changed(self, task_tpl_name: str) -> None:
        if not task_tpl_name:
            return
        tt = self.hub.template_manager.get_task_template(task_tpl_name)
        if tt.seed_file_template and self.seed_tpl_combo.findText(tt.seed_file_template) >= 0:
            self.seed_tpl_combo.setCurrentText(tt.seed_file_template)
        self._refresh_preview_tree()

    def _reset_components_from_template(self) -> None:
        pt = self.hub.template_manager.get_project_template(self.proj_tpl_combo.currentText())
        self.tpl_desc_label.setText(pt.description or "")
        self._active_components = list(pt.default_components)
        self._comp_task_overrides.clear()
        self._comp_seed_overrides.clear()
        self._refresh_preview_tree()

    def _add_custom_component(self) -> None:
        c = self.custom_comp_edit.text().strip().strip("/")
        if c and c not in self._active_components:
            self._active_components.append(c)
            self._comp_task_overrides[c] = self.task_tpl_combo.currentText()
            self._comp_seed_overrides[c] = self.seed_tpl_combo.currentText()
            self.custom_comp_edit.clear()
            self._refresh_preview_tree()

    def _apply_template_to_selected_component(self) -> None:
        item = self.preview_tree.currentItem()
        if not item:
            return
        comp_path = item.data(0, Qt.ItemDataRole.UserRole)
        if not comp_path and item.parent():
            comp_path = item.parent().data(0, Qt.ItemDataRole.UserRole)
        if comp_path and comp_path in self._active_components:
            self._comp_task_overrides[comp_path] = self.task_tpl_combo.currentText()
            self._comp_seed_overrides[comp_path] = self.seed_tpl_combo.currentText()
            self._refresh_preview_tree()

    def _remove_selected_component(self) -> None:
        item = self.preview_tree.currentItem()
        if not item:
            return
        comp_path = item.data(0, Qt.ItemDataRole.UserRole)
        if not comp_path and item.parent():
            comp_path = item.parent().data(0, Qt.ItemDataRole.UserRole)
        if comp_path and comp_path in self._active_components:
            self._active_components.remove(comp_path)
            self._comp_task_overrides.pop(comp_path, None)
            self._comp_seed_overrides.pop(comp_path, None)
            self._refresh_preview_tree()

    def _refresh_preview_tree(self) -> None:
        self._refresh_target_caption()
        self.preview_tree.clear()
        pname = self.name_edit.text().strip() or "new_project"
        preview = self.hub.template_manager.preview_project_tree(
            project_name=pname,
            project_template_name=self.proj_tpl_combo.currentText(),
            custom_components=self._active_components,
            component_task_overrides=self._comp_task_overrides,
            component_seed_overrides=self._comp_seed_overrides,
        )
        root_item = QTreeWidgetItem(self.preview_tree, [f"{pname}/", f"Project ({preview['project_template']})"])
        for shared in preview["shared_folders"]:
            QTreeWidgetItem(root_item, [f"{shared}/", "Shared Repo"])

        for comp_info in preview["components"]:
            cpath = comp_info["component_path"]
            c_item = QTreeWidgetItem(
                root_item,
                [f"{cpath}/", f"Component Triad [{comp_info['software']}]"],
            )
            c_item.setData(0, Qt.ItemDataRole.UserRole, cpath)
            w_item = QTreeWidgetItem(c_item, ["working/v001/", "Direct Version Container"])
            QTreeWidgetItem(w_item, [Path(comp_info["initial_working_file"]).name, f"Seed: {comp_info['seed_file']}"])
            QTreeWidgetItem(c_item, ["repo/", "Nested Versioned Repo Assets"])
            QTreeWidgetItem(c_item, ["published/", "Direct Version Container"])

        self.preview_tree.expandAll()

    def _browse_client_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Client Workspace Root", self.root_dir_edit.text())
        if folder:
            self.root_dir_edit.setText(folder)

    def _open_template_manager(self) -> None:
        dlg = TemplateManagerDialog(self, self.hub)
        dlg.exec()
        curr_pt = self.proj_tpl_combo.currentText()
        curr_tt = self.task_tpl_combo.currentText()
        curr_ft = self.seed_tpl_combo.currentText()
        self.proj_tpl_combo.blockSignals(True)
        self.proj_tpl_combo.clear()
        for pt_name in self.hub.template_manager.list_project_templates().keys():
            self.proj_tpl_combo.addItem(pt_name)
        if self.proj_tpl_combo.findText(curr_pt) >= 0:
            self.proj_tpl_combo.setCurrentText(curr_pt)
        self.proj_tpl_combo.blockSignals(False)

        self.task_tpl_combo.blockSignals(True)
        self.task_tpl_combo.clear()
        for tt_name in self.hub.template_manager.list_task_templates().keys():
            self.task_tpl_combo.addItem(tt_name)
        if self.task_tpl_combo.findText(curr_tt) >= 0:
            self.task_tpl_combo.setCurrentText(curr_tt)
        self.task_tpl_combo.blockSignals(False)

        self.seed_tpl_combo.clear()
        for ft_name in self.hub.template_manager.list_file_templates().keys():
            self.seed_tpl_combo.addItem(ft_name)
        if self.seed_tpl_combo.findText(curr_ft) >= 0:
            self.seed_tpl_combo.setCurrentText(curr_ft)
        self._reset_components_from_template()

    def _create_project(self) -> None:
        pname = self.name_edit.text().strip()
        if not pname:
            QMessageBox.warning(self, "Validation", "Project name is required.")
            return
        parent_dir = Path(self.root_dir_edit.text().strip())
        target_dir = parent_dir / pname
        if (target_dir / ".obol" / "project.toml").is_file():
            ans = QMessageBox.question(
                self,
                "Project Exists",
                f"An Obol project already exists at:\n{target_dir}\n\nRe-initialize components inside this folder?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
                QMessageBox.StandardButton.Cancel,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        if self.remember_root_chk.isChecked():
            self.hub.set_client_root(parent_dir)
        pcode = self.code_edit.text().strip() or None
        self.workspace.init_project(
            name=pname,
            root_path=target_dir,
            project_template=self.proj_tpl_combo.currentText(),
            code=pcode,
            custom_components=self._active_components,
            component_task_overrides=dict(self._comp_task_overrides),
            component_seed_overrides=dict(self._comp_seed_overrides),
        )
        self.hub.set_last_active_project(pname)
        self.created_project_name = pname
        self.accept()


class AddComponentDialog(QDialog):
    """Modal for creating a new Triad Component with template, software, and seed options."""

    def __init__(self, parent: Optional[QWidget], hub: ObolHub, initial_prefix: str = "") -> None:
        super().__init__(parent)
        self.hub = hub
        self._software_touched = False
        self.setWindowTitle("Add Triad Component")
        self.resize(480, 270)

        layout = QVBoxLayout(self)
        form = QFormLayout()

        prefix = initial_prefix.strip().strip("/")
        self.path_edit = QLineEdit(f"{prefix}/" if prefix else "")
        self.path_edit.setPlaceholderText("e.g. audio, song_2/mixing, vfx/comp")

        self.task_tpl_combo = QComboBox()
        for tt_name in sorted(self.hub.template_manager.list_task_templates().keys()):
            self.task_tpl_combo.addItem(tt_name)
        if self.task_tpl_combo.findText("reaper_daw") >= 0:
            self.task_tpl_combo.setCurrentText("reaper_daw")

        self.tpl_summary_label = QLabel("")
        self.tpl_summary_label.setObjectName("MutedText")
        self.tpl_summary_label.setWordWrap(True)

        self.software_combo = QComboBox()
        for sw_key in sorted(self.hub.config.software.keys()):
            prof = self.hub.config.software[sw_key]
            self.software_combo.addItem(f"{prof.display_name} ({prof.key})", prof.key)

        self.seed_chk = QCheckBox("Create initial working/v001 from template seed file")
        self.seed_chk.setChecked(True)

        form.addRow("Component Path:", self.path_edit)
        form.addRow("Component Template:", self.task_tpl_combo)
        form.addRow("", self.tpl_summary_label)
        form.addRow("Bound Software:", self.software_combo)
        form.addRow("", self.seed_chk)
        layout.addLayout(form)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        create_btn = QPushButton("Create Component")
        create_btn.setObjectName("PrimaryButton")
        create_btn.setDefault(True)
        create_btn.clicked.connect(self._validate_and_accept)
        btns.addWidget(cancel_btn)
        btns.addWidget(create_btn)
        layout.addLayout(btns)

        self.task_tpl_combo.currentTextChanged.connect(self._on_template_changed)
        self.software_combo.activated.connect(self._on_software_user_selected)
        self._on_template_changed(self.task_tpl_combo.currentText())
        self.path_edit.setFocus()
        self.path_edit.setCursorPosition(len(self.path_edit.text()))

    def _on_software_user_selected(self, _idx: int) -> None:
        self._software_touched = True

    def _on_template_changed(self, tpl_name: str) -> None:
        if not tpl_name:
            return
        tt = self.hub.template_manager.get_task_template(tpl_name)
        seed_txt = tt.seed_file_template or "none"
        desc = tt.description or "Component template"
        self.tpl_summary_label.setText(f"{desc}  •  Seed: {seed_txt}")
        if not self._software_touched:
            idx = self.software_combo.findData(tt.software)
            if idx >= 0:
                self.software_combo.setCurrentIndex(idx)

    def _validate_and_accept(self) -> None:
        cleaned = self.path_edit.text().strip().strip("/")
        if not cleaned:
            QMessageBox.warning(self, "Validation", "Component path is required (e.g. 'audio' or 'song_1/mixing').")
            self.path_edit.setFocus()
            return
        self.accept()

    def get_values(self) -> dict[str, object]:
        return {
            "component_path": self.path_edit.text().strip().strip("/"),
            "task_template": self.task_tpl_combo.currentText(),
            "software_override": str(self.software_combo.currentData() or "generic"),
            "seed_initial_working": self.seed_chk.isChecked(),
        }


_STRATEGY_LABELS = {
    VersionUpStrategy.CLONE_PRIMARY_FILE: "Clone Primary File (REAPER, Blender, Krita)",
    VersionUpStrategy.CLONE_ENTIRE_FOLDER: "Clone Entire Folder (Ableton Live bundles)",
    VersionUpStrategy.COPY_WITH_SIDECARS: "Copy Primary + Sidecar Files",
    VersionUpStrategy.FROM_SEED_TEMPLATE: "Fresh Copy From Seed Template",
}


class SoftwareSettingsDialog(QDialog):
    """GUI dialog for configuring 2-Tier Software Profiles & Version-Up Strategies without editing TOML by hand."""

    def __init__(self, parent: Optional[QWidget], hub: ObolHub) -> None:
        super().__init__(parent)
        self.hub = hub
        self.setWindowTitle("Software Integrations & Version-Up Strategies")
        self.resize(720, 410)

        layout = QHBoxLayout(self)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        self.sw_list = QListWidget()
        new_prof_btn = QPushButton("+ New Profile")
        new_prof_btn.clicked.connect(self._start_new_profile)
        left_layout.addWidget(self.sw_list, 1)
        left_layout.addWidget(new_prof_btn)
        layout.addWidget(left, 1)

        right = QWidget()
        form = QFormLayout(right)
        self.key_edit = QLineEdit()
        self.key_edit.setPlaceholderText("e.g. davinci, houdini, protools")
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Display name shown in UI")
        self.exe_edit = QLineEdit()
        self.exe_edit.setPlaceholderText("Optional path to .exe / app binary (blank = OS default)")
        exe_row = QHBoxLayout()
        exe_row.addWidget(self.exe_edit, 1)
        browse_exe_btn = QPushButton("Browse...")
        browse_exe_btn.clicked.connect(self._browse_executable)
        auto_exe_btn = QPushButton("Auto-Detect")
        auto_exe_btn.clicked.connect(self._auto_detect_executable)
        exe_row.addWidget(browse_exe_btn)
        exe_row.addWidget(auto_exe_btn)
        self.exe_status_label = QLabel("")
        self.exe_status_label.setObjectName("MutedText")

        self.ext_edit = QLineEdit()
        self.strategy_combo = QComboBox()
        for strat in VersionUpStrategy:
            self.strategy_combo.addItem(_STRATEGY_LABELS.get(strat, strat.value), strat.value)
        self.ignore_edit = QLineEdit()
        self.lock_chk = QCheckBox("Mark previous working versions Read-Only on Version Up")
        self.plugin_combo = QComboBox()
        for pl_name in self.hub.plugin_registry.list_plugins().keys():
            self.plugin_combo.addItem(pl_name)

        form.addRow("Profile Key:", self.key_edit)
        form.addRow("Display Name:", self.name_edit)
        form.addRow("Executable Path:", exe_row)
        form.addRow("", self.exe_status_label)
        form.addRow("Extensions (comma-sep):", self.ext_edit)
        form.addRow("Version-Up Strategy:", self.strategy_combo)
        form.addRow("Ignore Patterns:", self.ignore_edit)
        form.addRow("", self.lock_chk)
        form.addRow("Python Hook Plugin:", self.plugin_combo)

        btn_row = QHBoxLayout()
        save_btn = QPushButton("Save / Add Software Profile")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self._save_current)
        integ_btn = QPushButton("DCC Integrations...")
        integ_btn.clicked.connect(self._open_integrations_manager)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(save_btn)
        btn_row.addWidget(integ_btn)
        btn_row.addWidget(close_btn)
        form.addRow("", btn_row)

        layout.addWidget(right, 2)
        self.exe_edit.textChanged.connect(self._update_exe_status)
        self.sw_list.currentItemChanged.connect(self._on_list_item_changed)
        self._refresh_list()

    def _browse_executable(self) -> None:
        file_str, _ = QFileDialog.getOpenFileName(
            self,
            "Select Software Executable",
            self.exe_edit.text().strip() or "",
        )
        if file_str:
            self.exe_edit.setText(file_str)

    def _auto_detect_executable(self) -> None:
        from obol.plugins.integrations import DCCIntegrationManager

        key = self.key_edit.text().strip().lower()
        mgr = DCCIntegrationManager(self.hub, allow_resolve_studio_api=False)
        detected = mgr.auto_detect_paths(key).get("executable", "")
        if detected:
            self.exe_edit.setText(detected)
            self.exe_status_label.setText("✓ Auto-detected executable on disk")
        else:
            self.exe_status_label.setText(f"ℹ No auto-detector match for '{key or 'profile'}'")

    def _open_integrations_manager(self) -> None:
        from obol.gui.integrations_dialog import IntegrationsManagerDialog

        key = self.key_edit.text().strip().lower()
        initial = key if key in ("reaper", "resolve") else ""
        dlg = IntegrationsManagerDialog(self, self.hub, initial_dcc=initial)
        dlg.exec()
        self._refresh_list(select_key=key)

    def _update_exe_status(self, path_text: str) -> None:
        cleaned = path_text.strip()
        if not cleaned:
            self.exe_status_label.setText("ℹ Blank — files launch via OS default application")
            return
        if Path(cleaned).expanduser().is_file():
            self.exe_status_label.setText("✓ Executable found on disk")
        else:
            self.exe_status_label.setText("⚠ Executable path not found on this machine")

    def _start_new_profile(self) -> None:
        self.sw_list.clearSelection()
        self.key_edit.clear()
        self.name_edit.clear()
        self.exe_edit.clear()
        self.ext_edit.clear()
        self.ignore_edit.clear()
        self.strategy_combo.setCurrentIndex(0)
        self.lock_chk.setChecked(True)
        self.plugin_combo.setCurrentText("generic")
        self.key_edit.setFocus()

    def _refresh_list(self, select_key: str = "") -> None:
        self.sw_list.blockSignals(True)
        self.sw_list.clear()
        target_row = 0
        for idx, k in enumerate(sorted(self.hub.config.software.keys())):
            prof = self.hub.config.software[k]
            item = QListWidgetItem(f"{prof.display_name} [{k}]")
            item.setData(Qt.ItemDataRole.UserRole, k)
            self.sw_list.addItem(item)
            if select_key and k == select_key:
                target_row = idx
        self.sw_list.blockSignals(False)
        if self.sw_list.count() > 0:
            self.sw_list.setCurrentRow(target_row)
            self._on_list_item_changed(self.sw_list.currentItem(), None)

    def _on_list_item_changed(self, current: Optional[QListWidgetItem], _prev: Optional[QListWidgetItem]) -> None:
        if current is None:
            return
        key = str(current.data(Qt.ItemDataRole.UserRole) or current.text())
        self._load_selected(key)

    def _load_selected(self, key: str) -> None:
        if not key or key not in self.hub.config.software:
            return
        prof = self.hub.config.software[key]
        self.key_edit.setText(prof.key)
        self.name_edit.setText(prof.display_name)
        self.exe_edit.setText(prof.executable)
        self.ext_edit.setText(", ".join(prof.extensions))
        strat_idx = self.strategy_combo.findData(prof.version_up_strategy.value)
        if strat_idx >= 0:
            self.strategy_combo.setCurrentIndex(strat_idx)
        self.ignore_edit.setText(", ".join(prof.ignore_patterns))
        self.lock_chk.setChecked(prof.lock_previous_versions)
        self.plugin_combo.setCurrentText(prof.plugin)
        self._update_exe_status(prof.executable)

    def _save_current(self) -> None:
        k = self.key_edit.text().strip().lower()
        if not k:
            QMessageBox.warning(self, "Validation", "Profile Key is required (e.g. 'reaper' or 'davinci').")
            return
        exts = [e.strip() for e in self.ext_edit.text().split(",") if e.strip()]
        ignores = [p.strip() for p in self.ignore_edit.text().split(",") if p.strip()]
        strat_val = str(self.strategy_combo.currentData() or self.strategy_combo.currentText())
        prof = SoftwareProfile(
            key=k,
            display_name=self.name_edit.text().strip() or k.title(),
            executable=self.exe_edit.text().strip(),
            extensions=exts,
            version_up_strategy=VersionUpStrategy(strat_val),
            ignore_patterns=ignores,
            lock_previous_versions=self.lock_chk.isChecked(),
            plugin=self.plugin_combo.currentText(),
        )
        self.hub.set_software_profile(prof)
        self._refresh_list(select_key=k)


class ProjectAndVaultManagerDialog(QDialog):
    """Top-bar modal for setting the remembered Client Workspace Root, switching projects, and connecting an optional SMB Server Vault."""

    def __init__(self, parent: Optional[QWidget], hub: ObolHub, sync: SyncEngine) -> None:
        super().__init__(parent)
        self.hub = hub
        self.sync = sync
        self.selected_project_name: Optional[str] = None
        self.setWindowTitle("Client Workspace Root, Projects & Server Vault Manager")
        self.resize(720, 440)

        layout = QVBoxLayout(self)

        # Client Workspace Root Row (Always Remembered)
        client_row = QHBoxLayout()
        self.client_root_edit = QLineEdit(str(self.hub.get_client_root()))
        self.client_root_edit.setPlaceholderText("Local Client Root directory (e.g. D:\\ObolWork)")
        browse_client_btn = QPushButton("Browse...")
        browse_client_btn.clicked.connect(self._browse_client_root)
        save_client_btn = QPushButton("Set Client Root")
        save_client_btn.setObjectName("PrimaryButton")
        save_client_btn.clicked.connect(self._save_client_root)
        client_row.addWidget(QLabel("Client Root (Local):"))
        client_row.addWidget(self.client_root_edit, 1)
        client_row.addWidget(browse_client_btn)
        client_row.addWidget(save_client_btn)
        layout.addLayout(client_row)

        # Optional Vault Header Box
        vault_row = QHBoxLayout()
        self.vault_path_edit = QLineEdit(self.hub.config.vault.path)
        self.vault_path_edit.setPlaceholderText("Optional SMB/NAS path (e.g. \\\\NAS\\ObolVault or Z:\\ObolVault)")
        browse_btn = QPushButton("Browse...")
        browse_btn.clicked.connect(self._browse_vault)
        self.connect_btn = QPushButton()
        self.connect_btn.clicked.connect(self._toggle_vault)
        vault_row.addWidget(QLabel("Server Vault (Optional):"))
        vault_row.addWidget(self.vault_path_edit, 1)
        vault_row.addWidget(browse_btn)
        vault_row.addWidget(self.connect_btn)
        layout.addLayout(vault_row)

        # Projects Table
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Project", "Presence", "Local Path", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self.table)

        bottom = QHBoxLayout()
        new_proj_btn = QPushButton("+ New Project...")
        new_proj_btn.setObjectName("PrimaryButton")
        new_proj_btn.clicked.connect(self._create_new_project_from_manager)
        reg_proj_btn = QPushButton("Register Existing Project Folder...")
        reg_proj_btn.clicked.connect(self._register_existing_project_folder)
        bottom.addWidget(new_proj_btn)
        bottom.addWidget(reg_proj_btn)
        bottom.addStretch()
        close_btn = QPushButton("Done")
        close_btn.clicked.connect(self.accept)
        bottom.addWidget(close_btn)
        layout.addLayout(bottom)

        self._refresh_view()

    def _create_new_project_from_manager(self) -> None:
        ws = ObolWorkspace(self.hub)
        wiz = NewProjectWizardDialog(self, self.hub, ws)
        if wiz.exec() == NewProjectWizardDialog.DialogCode.Accepted and wiz.created_project_name:
            self.selected_project_name = wiz.created_project_name
            self._refresh_view()
            self.accept()

    def _register_existing_project_folder(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Existing Obol Project Folder",
            str(self.hub.get_client_root()),
        )
        if folder:
            entry = self.hub.register_project(Path(folder))
            self.hub.set_last_active_project(entry.name)
            self.selected_project_name = entry.name
            self._refresh_view()

    def _browse_client_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Client Workspace Root Folder", self.client_root_edit.text())
        if folder:
            self.client_root_edit.setText(folder)
            self._save_client_root()

    def _save_client_root(self) -> None:
        p = self.client_root_edit.text().strip()
        if not p:
            return
        new_root = self.hub.set_client_root(Path(p))
        self.client_root_edit.setText(str(new_root))
        self._refresh_view()

    def _browse_vault(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Server Vault Folder")
        if folder:
            self.vault_path_edit.setText(folder)

    def _toggle_vault(self) -> None:
        if self.hub.get_vault_status() == VaultStatus.ONLINE:
            self.hub.disconnect_vault()
        else:
            p = self.vault_path_edit.text().strip()
            if not p:
                QMessageBox.information(self, "Server Vault", "Enter or browse to a folder / SMB path to connect.")
                return
            self.hub.connect_vault(Path(p))
        self._refresh_view()

    def _refresh_view(self) -> None:
        st = self.hub.get_vault_status()
        self.connect_btn.setText("Disconnect Vault" if st == VaultStatus.ONLINE else "Connect Vault")
        projects = self.hub.list_projects()
        active_name = self.hub.config.last_active_project
        self.table.setRowCount(len(projects))

        for row_idx, entry in enumerate(projects):
            pres = self.sync.compute_project_presence(entry.name)
            display_name = f"★ {entry.name}" if entry.name == active_name else entry.name
            name_item = QTableWidgetItem(display_name)
            name_item.setData(Qt.ItemDataRole.UserRole, entry.name)
            name_item.setFlags(name_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            if entry.name == active_name:
                font = name_item.font()
                font.setBold(True)
                name_item.setFont(font)

            pres_item = QTableWidgetItem(pres.value)
            pres_item.setFlags(pres_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            path_item = QTableWidgetItem(entry.local_path)
            path_item.setFlags(path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self.table.setItem(row_idx, 0, name_item)
            self.table.setItem(row_idx, 1, pres_item)
            self.table.setItem(row_idx, 2, path_item)

            act_widget = QWidget()
            act_layout = QHBoxLayout(act_widget)
            act_layout.setContentsMargins(2, 2, 2, 2)
            open_btn = QPushButton("Open" if entry.name != active_name else "Active")
            if entry.name != active_name:
                open_btn.setObjectName("PrimaryButton")
            open_btn.clicked.connect(lambda _=False, n=entry.name: self._select_proj(n))
            act_layout.addWidget(open_btn)

            if st == VaultStatus.ONLINE:
                push_btn = QPushButton("Backup")
                push_btn.clicked.connect(lambda _=False, n=entry.name: self._push_proj(n, False))
                arch_btn = QPushButton("Archive (Free Space)")
                arch_btn.clicked.connect(lambda _=False, n=entry.name: self._push_proj(n, True))
                co_btn = QPushButton("Check Out")
                co_btn.clicked.connect(lambda _=False, n=entry.name: self._checkout_proj(n))
                act_layout.addWidget(push_btn)
                act_layout.addWidget(arch_btn)
                act_layout.addWidget(co_btn)

            del_btn = QPushButton("Remove...")
            del_btn.setObjectName("DangerButton")
            del_btn.clicked.connect(lambda _=False, n=entry.name: self._delete_proj(n))
            act_layout.addWidget(del_btn)

            self.table.setCellWidget(row_idx, 3, act_widget)
        self.table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)

    def _on_row_double_clicked(self, row: int, _col: int) -> None:
        item = self.table.item(row, 0)
        if item is not None:
            proj_name = str(item.data(Qt.ItemDataRole.UserRole) or item.text())
            self._select_proj(proj_name)

    def _select_proj(self, name: str) -> None:
        self.selected_project_name = name
        self.accept()

    def _delete_proj(self, name: str) -> None:
        entry = self.hub.get_project(name)
        if not entry:
            return
        box = QMessageBox(self)
        box.setWindowTitle("Remove Project")
        box.setIcon(QMessageBox.Icon.Warning)
        box.setText(f"How would you like to remove project '{entry.name}'?")
        box.setInformativeText(f"Local folder:\n{entry.local_path}")
        unreg_btn = box.addButton("Unregister Only (Keep Files)", QMessageBox.ButtonRole.AcceptRole)
        wipe_btn = box.addButton("Delete Files from Disk", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(cancel_btn)
        box.exec()

        clicked = box.clickedButton()
        if clicked is unreg_btn:
            self.hub.delete_project(entry.name, delete_files=False)
        elif clicked is wipe_btn:
            self.hub.delete_project(entry.name, delete_files=True)
        else:
            return

        if self.selected_project_name == entry.name:
            self.selected_project_name = None
        self._refresh_view()

    def _push_proj(self, name: str, evict: bool) -> None:
        self.sync.push_paths(name, ["**"], evict_local=evict)
        self._refresh_view()

    def _checkout_proj(self, name: str) -> None:
        self.sync.pull_paths(name, ["**"])
        self._refresh_view()


class CreditsReportDialog(QDialog):
    """Displays the project-wide Repo Asset Attribution & Credits Report with filter & export."""

    def __init__(self, parent: Optional[QWidget], project_name: str, rows: list[dict[str, object]]) -> None:
        super().__init__(parent)
        self.project_name = project_name
        self.rows = list(rows)
        self.setWindowTitle(f"Project Attribution & License Audit — {project_name}")
        self.resize(820, 420)
        layout = QVBoxLayout(self)

        top_bar = QHBoxLayout()
        self.filter_edit = QLineEdit()
        self.filter_edit.setObjectName("FilterInput")
        self.filter_edit.setPlaceholderText("Filter by component, asset, author, source, or license...")
        self.filter_edit.setClearButtonEnabled(True)
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("MutedText")
        top_bar.addWidget(self.filter_edit, 1)
        top_bar.addWidget(self.summary_label)
        layout.addLayout(top_bar)

        self.table = QTableWidget(len(self.rows), 7)
        self.table.setHorizontalHeaderLabels(
            ["Component", "Repo Asset", "Latest", "Used In", "Author", "Source", "License"]
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)

        warn_count = 0
        for i, r in enumerate(self.rows):
            used_raw = r.get("used_by_working", [])
            used_str = ", ".join(str(x) for x in used_raw) if isinstance(used_raw, list) else str(used_raw or "")
            lic_str = str(r.get("license", "Unspecified"))
            vals = [
                str(r.get("component", "")),
                f"repo/{r.get('asset_path', '')}",
                str(r.get("latest_version", "")),
                used_str or "—",
                str(r.get("author", "")),
                str(r.get("source", "")),
                lic_str,
            ]
            for col_idx, val in enumerate(vals):
                item = QTableWidgetItem(val)
                if col_idx == 6 and (lic_str == "Unspecified" or "temp" in lic_str.lower() or "scratch" in lic_str.lower()):
                    item.setForeground(QBrush(QColor("#fde68a")))
                    item.setBackground(QBrush(QColor("#451a03")))
                    item.setToolTip("Review license/clearance before publishing")
                    warn_count += 1
                self.table.setItem(i, col_idx, item)
        layout.addWidget(self.table, 1)

        if warn_count:
            self.summary_label.setText(f"{len(self.rows)} assets  •  ⚠ {warn_count} need license review")
        else:
            self.summary_label.setText(f"{len(self.rows)} repo assets")

        footer = QHBoxLayout()
        copy_md_btn = QPushButton("Copy Credits (Markdown)")
        copy_md_btn.clicked.connect(self._copy_markdown)
        export_csv_btn = QPushButton("Export CSV...")
        export_csv_btn.clicked.connect(self._export_csv)
        self.copy_status_lbl = QLabel("")
        self.copy_status_lbl.setObjectName("SuccessText")
        close_btn = QPushButton("Close")
        close_btn.setObjectName("PrimaryButton")
        close_btn.clicked.connect(self.accept)
        footer.addWidget(copy_md_btn)
        footer.addWidget(export_csv_btn)
        footer.addWidget(self.copy_status_lbl)
        footer.addStretch()
        footer.addWidget(close_btn)
        layout.addLayout(footer)

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        for row_idx in range(self.table.rowCount()):
            if not needle:
                self.table.setRowHidden(row_idx, False)
                continue
            matched = any(
                needle in (self.table.item(row_idx, c).text().lower() if self.table.item(row_idx, c) else "")
                for c in range(self.table.columnCount())
            )
            self.table.setRowHidden(row_idx, not matched)

    def format_markdown(self) -> str:
        lines = [f"# Attribution & Credits — {self.project_name}", ""]
        if not self.rows:
            lines.append("_No repo assets registered in this project._")
            return "\n".join(lines)
        for r in self.rows:
            lines.append(
                f"- **{r.get('asset_path', '')}** ({r.get('latest_version', '')}, `{r.get('component', '')}`) — "
                f"Author: {r.get('author', 'Unspecified')} | License: {r.get('license', 'Unspecified')} | "
                f"Source: {r.get('source', 'Unspecified')}"
            )
        return "\n".join(lines)

    def _copy_markdown(self) -> None:
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(self.format_markdown())
            self.copy_status_lbl.setText("✓ Copied Markdown to clipboard")

    def _export_csv(self) -> None:
        dest_str, _ = QFileDialog.getSaveFileName(
            self,
            "Export Credits Report CSV",
            f"{self.project_name}_credits.csv",
            "CSV Files (*.csv)",
        )
        if not dest_str:
            return
        with open(dest_str, "w", newline="", encoding="utf-8") as fh:
            writer = csv.writer(fh)
            writer.writerow(["Component", "Repo Asset", "Latest", "Used In", "Author", "Source", "License", "Tags"])
            for r in self.rows:
                used_raw = r.get("used_by_working", [])
                used_str = ", ".join(str(x) for x in used_raw) if isinstance(used_raw, list) else str(used_raw or "")
                writer.writerow([
                    r.get("component", ""),
                    f"repo/{r.get('asset_path', '')}",
                    r.get("latest_version", ""),
                    used_str,
                    r.get("author", ""),
                    r.get("source", ""),
                    r.get("license", ""),
                    r.get("tags", ""),
                ])
        self.copy_status_lbl.setText(f"✓ Exported {Path(dest_str).name}")


class TemplateManagerDialog(QDialog):
    """3-Tier Template Manager GUI for creating, editing, importing, and deleting Project, Component, and Seed File templates."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        active_project_root: Optional[Path] = None,
        active_component_path: str = "",
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.tm = hub.template_manager
        self.active_project_root = active_project_root
        self.active_component_path = active_component_path
        self.setWindowTitle("3-Tier Template Manager (Projects, Components & Working Seed Files)")
        self.resize(820, 480)

        from PySide6.QtWidgets import QPlainTextEdit, QTabWidget

        root_layout = QVBoxLayout(self)
        self.tabs = QTabWidget()

        # ---------------- Tab 1: Tier-1 Project Hierarchy Templates ----------------
        tab_proj = QWidget()
        tp_layout = QHBoxLayout(tab_proj)
        self.proj_tpl_list = QListWidget()
        self.proj_tpl_list.currentTextChanged.connect(self._load_project_template)
        tp_layout.addWidget(self.proj_tpl_list, 1)

        tp_form_widget = QWidget()
        tp_form = QFormLayout(tp_form_widget)
        self.pt_name_edit = QLineEdit()
        self.pt_name_edit.setPlaceholderText("e.g. weekly_show or game_sfx_pack")
        self.pt_desc_edit = QLineEdit()
        self.pt_shared_edit = QLineEdit("_repo")
        self.pt_comps_edit = QPlainTextEdit()
        self.pt_comps_edit.setPlaceholderText("One component path per line:\naudio\nvideo\nsong_1/mixing")
        self.pt_mappings_edit = QPlainTextEdit()
        self.pt_mappings_edit.setPlaceholderText("pattern = task_template (one per line):\naudio = reaper_daw\nvideo = generic_media\n* = reaper_daw")

        tp_form.addRow("Template Name:", self.pt_name_edit)
        tp_form.addRow("Description:", self.pt_desc_edit)
        tp_form.addRow("Shared Folders (comma-sep):", self.pt_shared_edit)
        tp_form.addRow("Default Components (1/line):", self.pt_comps_edit)
        tp_form.addRow("Task Template Rules (1/line):", self.pt_mappings_edit)

        tp_btns = QHBoxLayout()
        pt_save_btn = QPushButton("Save Project Template")
        pt_save_btn.setObjectName("PrimaryButton")
        pt_save_btn.clicked.connect(self._save_project_template)
        pt_cap_btn = QPushButton("Capture Active Project")
        pt_cap_btn.clicked.connect(self._capture_active_project)
        pt_del_btn = QPushButton("Delete")
        pt_del_btn.clicked.connect(self._delete_project_template)
        tp_btns.addWidget(pt_save_btn)
        tp_btns.addWidget(pt_cap_btn)
        tp_btns.addWidget(pt_del_btn)
        tp_form.addRow("", tp_btns)
        tp_layout.addWidget(tp_form_widget, 2)
        self.tabs.addTab(tab_proj, "1. Project Hierarchy Templates")

        # ---------------- Tab 2: Tier-2 Component / Task Templates ----------------
        tab_task = QWidget()
        tt_layout = QHBoxLayout(tab_task)
        self.task_tpl_list = QListWidget()
        self.task_tpl_list.currentTextChanged.connect(self._load_task_template)
        tt_layout.addWidget(self.task_tpl_list, 1)

        tt_form_widget = QWidget()
        tt_form = QFormLayout(tt_form_widget)
        self.tt_name_edit = QLineEdit()
        self.tt_name_edit.setPlaceholderText("e.g. reaper_podcast or davinci_color")
        self.tt_desc_edit = QLineEdit()
        self.tt_software_combo = QComboBox()
        for sw_k in sorted(self.hub.config.software.keys()):
            self.tt_software_combo.addItem(sw_k)
        self.tt_seed_combo = QComboBox()
        self.tt_routes_edit = QPlainTextEdit()
        self.tt_routes_edit.setPlaceholderText("category = .ext1, .ext2 (one per line):\naudio = .wav, .flac, .mp3\nmidi = .mid")

        tt_form.addRow("Template Name:", self.tt_name_edit)
        tt_form.addRow("Description:", self.tt_desc_edit)
        tt_form.addRow("Bound Software Profile:", self.tt_software_combo)
        tt_form.addRow("Default Working Seed File:", self.tt_seed_combo)
        tt_form.addRow("Repo Routing Rules (1/line):", self.tt_routes_edit)

        tt_btns = QHBoxLayout()
        tt_save_btn = QPushButton("Save Component Template")
        tt_save_btn.setObjectName("PrimaryButton")
        tt_save_btn.clicked.connect(self._save_task_template)
        tt_cap_btn = QPushButton("Capture Active Component")
        tt_cap_btn.clicked.connect(self._capture_active_component)
        tt_del_btn = QPushButton("Delete")
        tt_del_btn.clicked.connect(self._delete_task_template)
        tt_btns.addWidget(tt_save_btn)
        tt_btns.addWidget(tt_cap_btn)
        tt_btns.addWidget(tt_del_btn)
        tt_form.addRow("", tt_btns)
        tt_layout.addWidget(tt_form_widget, 2)
        self.tabs.addTab(tab_task, "2. Component / Task Templates")

        # ---------------- Tab 3: Tier-3 Working Seed Files ----------------
        tab_files = QWidget()
        tf_layout = QVBoxLayout(tab_files)
        self.seed_files_table = QTableWidget(0, 3)
        self.seed_files_table.setHorizontalHeaderLabels(["Seed Filename", "Extension", "Full Template Path"])
        self.seed_files_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tf_layout.addWidget(self.seed_files_table)

        tf_btns = QHBoxLayout()
        imp_seed_btn = QPushButton("+ Import Seed File from Disk...")
        imp_seed_btn.setObjectName("PrimaryButton")
        imp_seed_btn.clicked.connect(self._import_seed_file)
        edit_seed_btn = QPushButton("Open Selected Seed File in App")
        edit_seed_btn.clicked.connect(self._open_selected_seed_file)
        del_seed_btn = QPushButton("Delete Selected Seed File")
        del_seed_btn.clicked.connect(self._delete_selected_seed_file)
        tf_btns.addWidget(imp_seed_btn)
        tf_btns.addWidget(edit_seed_btn)
        tf_btns.addWidget(del_seed_btn)
        tf_btns.addStretch()
        tf_layout.addLayout(tf_btns)
        self.tabs.addTab(tab_files, "3. Working Seed Files")

        root_layout.addWidget(self.tabs)

        footer = QHBoxLayout()
        path_lbl = QLabel(f"Templates Directory: {self.tm.templates_root}")
        path_lbl.setObjectName("MutedText")
        open_dir_btn = QPushButton("Open Templates Folder in Explorer")
        open_dir_btn.clicked.connect(self._open_templates_folder)
        close_dlg_btn = QPushButton("Done")
        close_dlg_btn.clicked.connect(self.accept)
        footer.addWidget(path_lbl)
        footer.addStretch()
        footer.addWidget(open_dir_btn)
        footer.addWidget(close_dlg_btn)
        root_layout.addLayout(footer)

        self._refresh_all_lists()

    def _refresh_all_lists(self) -> None:
        # Seed files first so combo in tab 2 is populated
        self.tt_seed_combo.clear()
        file_tpls = self.tm.list_file_templates()
        self.seed_files_table.setRowCount(len(file_tpls))
        for idx, (fname, ft) in enumerate(sorted(file_tpls.items())):
            self.tt_seed_combo.addItem(fname)
            self.seed_files_table.setItem(idx, 0, QTableWidgetItem(fname))
            self.seed_files_table.setItem(idx, 1, QTableWidgetItem(ft.extension))
            self.seed_files_table.setItem(idx, 2, QTableWidgetItem(ft.path))

        # Project templates
        curr_p = self.proj_tpl_list.currentItem().text() if self.proj_tpl_list.currentItem() else ""
        self.proj_tpl_list.clear()
        for pt_name in sorted(self.tm.list_project_templates().keys()):
            self.proj_tpl_list.addItem(pt_name)
        if self.proj_tpl_list.count() > 0:
            items = self.proj_tpl_list.findItems(curr_p, Qt.MatchFlag.MatchExactly)
            self.proj_tpl_list.setCurrentItem(items[0] if items else self.proj_tpl_list.item(0))

        # Task templates
        curr_t = self.task_tpl_list.currentItem().text() if self.task_tpl_list.currentItem() else ""
        self.task_tpl_list.clear()
        for tt_name in sorted(self.tm.list_task_templates().keys()):
            self.task_tpl_list.addItem(tt_name)
        if self.task_tpl_list.count() > 0:
            items = self.task_tpl_list.findItems(curr_t, Qt.MatchFlag.MatchExactly)
            self.task_tpl_list.setCurrentItem(items[0] if items else self.task_tpl_list.item(0))

    def _load_project_template(self, name: str) -> None:
        if not name:
            return
        pt = self.tm.get_project_template(name)
        self.pt_name_edit.setText(pt.name)
        self.pt_desc_edit.setText(pt.description)
        self.pt_shared_edit.setText(", ".join(pt.shared_folders))
        self.pt_comps_edit.setPlainText("\n".join(pt.default_components))
        mapping_lines = [f"{k} = {v}" for k, v in pt.component_templates.items()]
        self.pt_mappings_edit.setPlainText("\n".join(mapping_lines))

    def _save_project_template(self) -> None:
        name = self.pt_name_edit.text().strip()
        if not name:
            return
        shared = [s.strip() for s in self.pt_shared_edit.text().split(",") if s.strip()]
        comps = [line.strip().strip("/") for line in self.pt_comps_edit.toPlainText().splitlines() if line.strip()]
        mappings: dict[str, str] = {}
        for line in self.pt_mappings_edit.toPlainText().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                mappings[k.strip()] = v.strip()
        pt = ProjectTemplate(
            name=name,
            description=self.pt_desc_edit.text().strip(),
            default_components=comps or ["audio", "video"],
            component_templates=mappings or {"*": "reaper_daw"},
            shared_folders=shared or ["_repo"],
        )
        self.tm.save_project_template(pt)
        self._refresh_all_lists()

    def _capture_active_project(self) -> None:
        if not self.active_project_root or not self.active_project_root.is_dir():
            QMessageBox.information(self, "Capture Project", "Open a project first to capture its hierarchy as a template.")
            return
        name = self.pt_name_edit.text().strip() or f"{self.active_project_root.name}_tpl"
        self.tm.capture_template("project", self.active_project_root, name)
        self._refresh_all_lists()

    def _delete_project_template(self) -> None:
        name = self.pt_name_edit.text().strip()
        if name:
            self.tm.delete_project_template(name)
            self._refresh_all_lists()

    def _load_task_template(self, name: str) -> None:
        if not name:
            return
        tt = self.tm.get_task_template(name)
        self.tt_name_edit.setText(tt.name)
        self.tt_desc_edit.setText(tt.description)
        self.tt_software_combo.setCurrentText(tt.software)
        if self.tt_seed_combo.findText(tt.seed_file_template) >= 0:
            self.tt_seed_combo.setCurrentText(tt.seed_file_template)
        route_lines = [f"{cat} = {', '.join(exts)}" for cat, exts in tt.repo_routes.items()]
        self.tt_routes_edit.setPlainText("\n".join(route_lines))

    def _save_task_template(self) -> None:
        name = self.tt_name_edit.text().strip()
        if not name:
            return
        routes: dict[str, list[str]] = {}
        for line in self.tt_routes_edit.toPlainText().splitlines():
            if "=" in line:
                cat, exts_raw = line.split("=", 1)
                exts = [e.strip() for e in exts_raw.split(",") if e.strip()]
                if cat.strip() and exts:
                    routes[cat.strip()] = exts
        tt = TaskTemplate(
            name=name,
            description=self.tt_desc_edit.text().strip(),
            software=self.tt_software_combo.currentText(),
            seed_file_template=self.tt_seed_combo.currentText(),
            repo_routes=routes or {"audio": [".wav", ".flac", ".mp3"], "midi": [".mid"]},
        )
        self.tm.save_task_template(tt)
        self._refresh_all_lists()

    def _capture_active_component(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            QMessageBox.information(self, "Capture Component", "Select a component in the main window first.")
            return
        comp_dir = self.active_project_root / self.active_component_path
        name = self.tt_name_edit.text().strip() or f"{comp_dir.name}_task"
        self.tm.capture_template("task", comp_dir, name)
        self._refresh_all_lists()

    def _delete_task_template(self) -> None:
        name = self.tt_name_edit.text().strip()
        if name:
            self.tm.delete_task_template(name)
            self._refresh_all_lists()

    def _import_seed_file(self) -> None:
        file_str, _ = QFileDialog.getOpenFileName(self, "Select Starter Working File (.rpp, .blend, .kra, .psd, etc.)")
        if file_str:
            self.tm.import_seed_file(Path(file_str))
            self._refresh_all_lists()

    def _open_selected_seed_file(self) -> None:
        row = self.seed_files_table.currentRow()
        if row < 0:
            return
        path_item = self.seed_files_table.item(row, 2)
        if not path_item:
            return
        fpath = Path(path_item.text())
        if fpath.is_file():
            from obol.plugins.base import launch_file_with_profile

            prof = self.hub.detect_software_for_extension(fpath.suffix)
            launch_file_with_profile(fpath, prof)

    def _delete_selected_seed_file(self) -> None:
        row = self.seed_files_table.currentRow()
        if row < 0:
            return
        name_item = self.seed_files_table.item(row, 0)
        if name_item:
            self.tm.delete_seed_file(name_item.text())
            self._refresh_all_lists()

    def _open_templates_folder(self) -> None:
        import os
        import sys

        if sys.platform == "win32" and hasattr(os, "startfile"):
            os.startfile(str(self.tm.templates_root))


class IngestWorkingDialog(QDialog):
    """Dialog for ingesting external working file(s) or folder bundles into working/vNNN/."""

    def __init__(
        self,
        parent: Optional[QWidget],
        source_paths: list[Path],
        component_leaf: str,
        next_version_num: int,
    ) -> None:
        super().__init__(parent)
        self.source_paths = list(source_paths)
        self.component_leaf = component_leaf
        self.next_version_num = next_version_num
        self.setWindowTitle(f"Ingest Working Version — working/v{next_version_num:03d}/")
        self.resize(480, 300)

        layout = QVBoxLayout(self)
        summary = ", ".join(p.name for p in self.source_paths[:3])
        if len(self.source_paths) > 3:
            summary += f" (+{len(self.source_paths) - 3} more)"
        layout.addWidget(QLabel(f"Ingesting into <b>working/v{next_version_num:03d}/</b>: {summary}"))

        items_list = QListWidget()
        items_list.setMaximumHeight(90)
        for p in self.source_paths:
            kind = "Folder" if p.is_dir() else "File"
            items_list.addItem(f"[{kind}] {p}")
        layout.addWidget(items_list)

        form = QFormLayout()
        self.rename_chk = QCheckBox(f"Rename primary file to '{component_leaf}_v{next_version_num:03d}<ext>'")
        self.rename_chk.setChecked(True)
        self.copy_mode_combo = QComboBox()
        self.copy_mode_combo.addItems(["copy", "move"])
        self.comment_edit = QLineEdit(f"Ingested {self.source_paths[0].name if len(self.source_paths) == 1 else f'{len(self.source_paths)} items'}")
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("comma-separated tags (optional)")

        form.addRow("", self.rename_chk)
        form.addRow("Transfer Mode:", self.copy_mode_combo)
        form.addRow("Version Note:", self.comment_edit)
        form.addRow("Tags:", self.tags_edit)
        layout.addLayout(form)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton(f"Ingest as working/v{next_version_num:03d}/")
        ok_btn.setObjectName("PrimaryButton")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        btns.addWidget(cancel_btn)
        btns.addWidget(ok_btn)
        layout.addLayout(btns)

    def get_values(self) -> dict[str, object]:
        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        return {
            "rename_to_convention": self.rename_chk.isChecked(),
            "copy_mode": self.copy_mode_combo.currentText(),
            "comment": self.comment_edit.text().strip(),
            "tags": tags,
        }


class PublishDeliverableDialog(QDialog):
    """Multi-file-type deliverable staging & publishing dialog for named published/<name>/vNNN/ bundles."""

    def __init__(
        self,
        parent: Optional[QWidget],
        initial_files: list[Path],
        component_leaf: str,
        working_versions: list[int],
        existing_deliverables: dict[str, list[int]],
        publish_formats: Optional[list[str]] = None,
        default_deliverable: str = "main",
        default_working_version: Optional[int] = None,
        default_target_version: Optional[int] = None,
    ) -> None:
        super().__init__(parent)
        self.component_leaf = component_leaf
        self.existing_deliverables = {k: list(v) for k, v in existing_deliverables.items()}
        self.publish_formats = [f.lower() if f.startswith(".") else f".{f.lower()}" for f in (publish_formats or [])]
        self._staged_rows: list[tuple[Path, str]] = []  # (path, output_label)

        self.setWindowTitle("Publish Multi-File Deliverables")
        self.resize(740, 480)

        layout = QVBoxLayout(self)

        top_form = QFormLayout()
        self.deliverable_combo = QComboBox()
        self.deliverable_combo.setEditable(True)
        names = sorted(set(["main"] + list(self.existing_deliverables.keys())))
        for n in names:
            self.deliverable_combo.addItem(n)
        self.deliverable_combo.setCurrentText(default_deliverable or "main")

        self.working_ver_combo = QComboBox()
        for wv in sorted(working_versions, reverse=True):
            self.working_ver_combo.addItem(f"working/v{wv:03d}", wv)
        if not working_versions:
            self.working_ver_combo.addItem("working/v001 (default)", 1)
        if default_working_version is not None:
            idx = self.working_ver_combo.findData(default_working_version)
            if idx >= 0:
                self.working_ver_combo.setCurrentIndex(idx)

        self.target_ver_combo = QComboBox()

        top_form.addRow("Deliverable Name:", self.deliverable_combo)
        top_form.addRow("Source Working Version:", self.working_ver_combo)
        top_form.addRow("Target Release Version:", self.target_ver_combo)
        layout.addLayout(top_form)

        # Format chips row
        if self.publish_formats:
            chips_row = QHBoxLayout()
            chips_lbl = QLabel("Expected Formats:")
            chips_lbl.setObjectName("MutedText")
            chips_row.addWidget(chips_lbl)
            self._format_chips: dict[str, QLabel] = {}
            for fmt in self.publish_formats:
                chip = QLabel(fmt.lstrip(".").upper())
                chip.setObjectName("FormatChipIdle")
                chips_row.addWidget(chip)
                self._format_chips[fmt] = chip
            chips_row.addStretch()
            layout.addLayout(chips_row)
        else:
            self._format_chips = {}

        # Staging table (supports drag-and-drop)
        self.staging_table = DropTableWidget(0, 5)
        self.staging_table.setHorizontalHeaderLabels(["Source File", "Type", "Output Label (optional)", "Target Filename", "Action"])
        self.staging_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.staging_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.staging_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Interactive)
        self.staging_table.horizontalHeader().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.staging_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self.staging_table.setColumnWidth(2, 140)
        self.staging_table.filesDropped.connect(self.add_files)
        self.staging_table.itemChanged.connect(self._on_table_item_changed)
        layout.addWidget(self.staging_table, 1)

        stage_btns = QHBoxLayout()
        add_files_btn = QPushButton("+ Add File(s)...")
        add_files_btn.clicked.connect(self._browse_add_files)
        self.keep_names_chk = QCheckBox("Keep original filenames")
        self.keep_names_chk.toggled.connect(self._refresh_target_previews)
        hint_lbl = QLabel("Drop multiple file types into the table above")
        hint_lbl.setObjectName("DropHint")
        stage_btns.addWidget(add_files_btn)
        stage_btns.addWidget(self.keep_names_chk)
        stage_btns.addWidget(hint_lbl, 1)
        layout.addLayout(stage_btns)

        bottom_form = QFormLayout()
        self.comment_edit = QLineEdit()
        self.comment_edit.setPlaceholderText("Release notes (e.g. Master WAV + MIDI stems)")
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("comma-separated tags (optional)")
        bottom_form.addRow("Publish Note:", self.comment_edit)
        bottom_form.addRow("Tags:", self.tags_edit)
        layout.addLayout(bottom_form)

        btns = QHBoxLayout()
        btns.addStretch()
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self.publish_btn = QPushButton("Publish Deliverables")
        self.publish_btn.setObjectName("PrimaryButton")
        self.publish_btn.setDefault(True)
        self.publish_btn.clicked.connect(self._validate_and_accept)
        btns.addWidget(cancel_btn)
        btns.addWidget(self.publish_btn)
        layout.addLayout(btns)

        self.deliverable_combo.currentTextChanged.connect(self._refresh_target_version_combo)
        self.target_ver_combo.currentIndexChanged.connect(self._refresh_target_previews)
        self._refresh_target_version_combo(self.deliverable_combo.currentText())
        if default_target_version is not None:
            idx = self.target_ver_combo.findData(default_target_version)
            if idx >= 0:
                self.target_ver_combo.setCurrentIndex(idx)

        self.add_files(initial_files)
        line_edit = self.deliverable_combo.lineEdit()
        if line_edit is not None:
            line_edit.setFocus()
            line_edit.selectAll()

    def _normalized_deliverable(self) -> str:
        return ObolWorkspace.normalize_deliverable_name(self.deliverable_combo.currentText())

    def _refresh_target_version_combo(self, _text: str = "") -> None:
        deliv = self._normalized_deliverable()
        vers = sorted(self.existing_deliverables.get(deliv, []))
        next_v = (vers[-1] + 1) if vers else 1
        prev_data = self.target_ver_combo.currentData()
        self.target_ver_combo.blockSignals(True)
        self.target_ver_combo.clear()
        self.target_ver_combo.addItem(f"New Release (published/{deliv}/v{next_v:03d}/)", None)
        for v in reversed(vers):
            self.target_ver_combo.addItem(f"Add to existing v{v:03d}/", v)
        if prev_data is not None:
            idx = self.target_ver_combo.findData(prev_data)
            if idx >= 0:
                self.target_ver_combo.setCurrentIndex(idx)
        self.target_ver_combo.blockSignals(False)
        self._refresh_target_previews()

    def _browse_add_files(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Add Exported Deliverable File(s)")
        if files:
            self.add_files([Path(f) for f in files])

    def add_files(self, paths: list[Path]) -> None:
        existing_set = {p for p, _ in self._staged_rows}
        for p in paths:
            if p.is_file() and p not in existing_set:
                self._staged_rows.append((p, ""))
                existing_set.add(p)
        self._rebuild_table()

    def _remove_row(self, row: int) -> None:
        if 0 <= row < len(self._staged_rows):
            self._staged_rows.pop(row)
            self._rebuild_table()

    def _on_table_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 2:
            return
        row = item.row()
        if 0 <= row < len(self._staged_rows):
            path, _ = self._staged_rows[row]
            self._staged_rows[row] = (path, item.text().strip())
            self._refresh_target_previews()

    def _rebuild_table(self) -> None:
        self.staging_table.blockSignals(True)
        self.staging_table.setRowCount(len(self._staged_rows))
        for row, (p, label) in enumerate(self._staged_rows):
            src_item = QTableWidgetItem(p.name)
            src_item.setToolTip(str(p))
            src_item.setFlags(src_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            ftype = p.suffix.lstrip(".").upper() or "FILE"
            type_item = QTableWidgetItem(ftype)
            type_item.setFlags(type_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            label_item = QTableWidgetItem(label)

            target_item = QTableWidgetItem("")
            target_item.setFlags(target_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            rm_btn = QPushButton("Remove")
            rm_btn.clicked.connect(lambda _=False, r=row: self._remove_row(r))

            self.staging_table.setItem(row, 0, src_item)
            self.staging_table.setItem(row, 1, type_item)
            self.staging_table.setItem(row, 2, label_item)
            self.staging_table.setItem(row, 3, target_item)
            self.staging_table.setCellWidget(row, 4, rm_btn)
        self.staging_table.blockSignals(False)
        self._refresh_target_previews()
        self._refresh_format_chips()

    def _refresh_target_previews(self) -> None:
        deliv = self._normalized_deliverable()
        deliv_leaf = deliv.split("/")[-1]
        target_v = self.target_ver_combo.currentData()
        if target_v is None:
            vers = self.existing_deliverables.get(deliv, [])
            v_num = (max(vers) + 1) if vers else 1
        else:
            v_num = int(target_v)
        ver_label = f"v{v_num:03d}"
        ext_counts = Counter(p.suffix.lower() for p, _ in self._staged_rows)
        keep_orig = self.keep_names_chk.isChecked()

        self.staging_table.blockSignals(True)
        seen: set[str] = set()
        for row, (p, label) in enumerate(self._staged_rows):
            name = ObolWorkspace.resolve_publish_target_filename(
                deliverable_leaf=deliv_leaf,
                version_label=ver_label,
                source_path=p,
                ext_counts=ext_counts,
                output_label=label,
                keep_original_name=keep_orig,
                existing_filenames=seen,
            )
            seen.add(name)
            item = self.staging_table.item(row, 3)
            if item is not None:
                item.setText(f"published/{deliv}/{ver_label}/{name}")
        self.staging_table.blockSignals(False)

    def _refresh_format_chips(self) -> None:
        staged_exts = {p.suffix.lower() for p, _ in self._staged_rows}
        for fmt, chip in self._format_chips.items():
            active = fmt in staged_exts
            chip.setObjectName("FormatChipActive" if active else "FormatChipIdle")
            style = chip.style()
            if style is not None:
                style.unpolish(chip)
                style.polish(chip)

    def _validate_and_accept(self) -> None:
        if not self._staged_rows:
            QMessageBox.warning(self, "Validation", "Add at least one deliverable file to publish.")
            return
        self.accept()

    def get_values(self) -> dict[str, object]:
        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        items = [
            PublishItem(
                source_path=p,
                output_label=label,
                keep_original_name=self.keep_names_chk.isChecked(),
            )
            for p, label in self._staged_rows
        ]
        return {
            "deliverable_name": self._normalized_deliverable(),
            "source_working_version": int(self.working_ver_combo.currentData() or 1),
            "target_version": self.target_ver_combo.currentData(),
            "publish_items": items,
            "keep_original_names": self.keep_names_chk.isChecked(),
            "comment": self.comment_edit.text().strip(),
            "tags": tags,
        }


