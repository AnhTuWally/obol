"""Dialogs for Obol GUI: Projects & Vault Manager, 3-Tier Template Wizard with Live Tree Preview, Smart Repo Ingest, Software Settings, Reaper Media Collector, and Credits Report."""

from __future__ import annotations

from collections import Counter
import csv
from pathlib import Path
import re
from typing import Any, Optional

from PySide6.QtCore import QSize, Qt, QUrl
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import (
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
    QListWidgetItem,
    QMenu,
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
    FilePresence,
    ProjectTemplate,
    PublishItem,
    SoftwareProfile,
    TaskTemplate,
    TemplateScope,
    VaultStatus,
    VersionUpStrategy,
)
from obol.core.seed_templates import normalize_software_key, normalize_template_name
from obol.core.sync import SyncEngine
from obol.core.workspace import ObolWorkspace
from obol.gui.components import (
    DialogFooter,
    InlineStatusBanner,
    attach_search_icon,
    confirm_destructive_action,
    open_folder_in_os,
    run_with_busy_state,
)
from obol.gui.drop_widgets import DropTableWidget, extract_local_drop_paths
from obol.gui.icons import get_icon
from obol.gui.metadata_widgets import CustomMetadataEditor
from obol.gui.software_icons import get_software_icon, resolve_software_icon
from obol.gui.tokens import ACCENT_LIGHT, ICON_BTN_SIZE, ICON_SM, TEXT_MUTED, TEXT_SECONDARY, WARNING_BG, WARNING_TEXT


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
        default_url: str = "",
        default_custom: Optional[dict[str, Any]] = None,
        copied_metadata: Optional[dict[str, Any]] = None,
        suggested_custom_keys: Optional[list[str]] = None,
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
        self._copied_metadata = dict(copied_metadata) if copied_metadata else None
        self.setAcceptDrops(True)
        self.setWindowTitle(
            f"Repo Ingest — {len(self.source_files)} Files"
            if self.is_batch
            else "Repo Ingest — Name, Version & Provenance"
        )
        self.resize(660 if self.is_batch else 540, 560 if self.is_batch else 520)

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
            self.batch_table.verticalHeader().setVisible(False)
            self.batch_table.setAlternatingRowColors(True)
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
            self.file_path_edit.setPlaceholderText("Drop a file here or click Browse…")
            self.file_path_edit.setReadOnly(True)
            browse_file_btn = QPushButton("Browse…")
            browse_file_btn.clicked.connect(self._browse_single_file)
            file_row.addWidget(self.file_path_edit, 1)
            file_row.addWidget(browse_file_btn)

            self.version_edit = QLineEdit(f"{target_version_label}/ (Auto-incremented)")
            self.version_edit.setReadOnly(True)
            self.preview_label = QLabel("")
            self.preview_label.setObjectName("MutedText")

            form_top.addRow("Name:", self.name_edit)
            if self._lock_asset_name:
                self.lock_hint_label = QLabel(
                    "Keep the current name to add the next version, or change it to create a new asset."
                )
                self.lock_hint_label.setObjectName("MutedText")
                self.lock_hint_label.setWordWrap(True)
                form_top.addRow("", self.lock_hint_label)
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
        self.source_edit.setPlaceholderText("Sample Pack, Studio Session, or Path")
        self.url_edit = QLineEdit(default_url)
        self.url_edit.setPlaceholderText("https://... (optional download or credit page)")
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
        form.addRow("Source (Pack / Path):", self.source_edit)
        form.addRow("URL:", self.url_edit)
        form.addRow("License / Usage Rights:", self.license_combo)
        form.addRow("Tags:", self.tags_edit)
        form.addRow("Version Comment:", self.comment_edit)
        layout.addLayout(form)

        self.custom_meta_editor = CustomMetadataEditor(self, hidden_keys={"seed_template"})
        self.custom_meta_editor.set_data(default_custom or {}, suggested_keys=suggested_custom_keys)
        layout.addWidget(self.custom_meta_editor)

        self.paste_meta_btn = QPushButton("Paste Copied Metadata")
        self.paste_meta_btn.setObjectName("SubtleToolButton")
        self.paste_meta_btn.setIcon(get_icon("clipboard", TEXT_SECONDARY, ICON_SM))
        self.paste_meta_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.paste_meta_btn.clicked.connect(self._apply_copied_metadata)
        self.paste_meta_btn.setVisible(bool(self._copied_metadata))
        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton(f"Ingest {len(self.source_files)} Files into repo/" if self.is_batch else "Ingest into repo/")
        ok_btn.setObjectName("PrimaryButton")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self._validate_and_accept)
        layout.addWidget(
            DialogFooter(
                self,
                left_widgets=[self.paste_meta_btn],
                cancel_button=cancel_btn,
                primary_button=ok_btn,
            )
        )

        if not self.is_batch:
            self.name_edit.setFocus()
            self.name_edit.selectAll()

    def _apply_copied_metadata(self) -> None:
        if not self._copied_metadata:
            return
        meta = self._copied_metadata
        if "author" in meta:
            self.author_edit.setText(str(meta.get("author") or ""))
        if "source" in meta and str(meta.get("source") or "").strip():
            self.source_edit.setText(str(meta.get("source") or ""))
        if "url" in meta:
            self.url_edit.setText(str(meta.get("url") or ""))
        if "license" in meta and str(meta.get("license") or "").strip():
            self.license_combo.setCurrentText(str(meta.get("license") or ""))
        if "tags" in meta and isinstance(meta.get("tags"), list):
            self.tags_edit.setText(", ".join(str(t) for t in meta["tags"] if str(t).strip()))
        if "comment" in meta and str(meta.get("comment") or "").strip() and not self.comment_edit.text().strip():
            self.comment_edit.setText(str(meta.get("comment") or ""))
        if isinstance(meta.get("custom"), dict):
            merged = dict(self.custom_meta_editor.get_data(include_hidden=False))
            for k, v in meta["custom"].items():
                if str(k).strip():
                    merged[str(k).strip()] = "" if v is None else str(v)
            self.custom_meta_editor.set_data(merged)

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
            "url": self.url_edit.text().strip(),
            "license_info": self.license_combo.currentText().strip(),
            "tags": tags,
            "comment": self.comment_edit.text().strip(),
            "custom": self.custom_meta_editor.get_data(include_hidden=False),
        }

    def get_batch_entries(self) -> list[dict[str, object]]:
        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        author = self.author_edit.text().strip()
        source_info = self.source_edit.text().strip()
        url_val = self.url_edit.text().strip()
        license_info = self.license_combo.currentText().strip()
        comment = self.comment_edit.text().strip()
        custom_vals = self.custom_meta_editor.get_data(include_hidden=False)
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
                "url": url_val,
                "license_info": license_info,
                "tags": tags,
                "comment": comment or f"Ingested from {sf.name}",
                "custom": dict(custom_vals),
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
        main_layout.setContentsMargins(14, 14, 14, 14)
        main_layout.setSpacing(10)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setHandleWidth(6)

        # Left form: Project identity & 3-tier template pickers
        left_widget = QWidget()
        left_outer = QVBoxLayout(left_widget)
        left_outer.setContentsMargins(0, 0, 6, 0)
        left_outer.setSpacing(10)

        left_layout = QFormLayout()
        left_layout.setSpacing(7)
        self.name_edit = QLineEdit("new_project")
        self.code_edit = QLineEdit()
        self.code_edit.setPlaceholderText("Auto-generated from name (e.g. npr)")

        root_row = QHBoxLayout()
        self.root_dir_edit = QLineEdit(str(self.hub.get_client_root()))
        browse_root_btn = QPushButton("Browse…")
        browse_root_btn.clicked.connect(self._browse_client_root)
        root_tpl_btn = QPushButton("Root Seed Templates…")
        root_tpl_btn.setObjectName("SubtleToolButton")
        root_tpl_btn.setToolTip("Manage per-software seed templates for this Client Workspace Root")
        root_tpl_btn.clicked.connect(self._open_root_seed_manager)
        root_row.addWidget(self.root_dir_edit, 1)
        root_row.addWidget(browse_root_btn)
        root_row.addWidget(root_tpl_btn)
        self.remember_root_chk = QCheckBox("Remember as default Client Workspace Root")
        self.remember_root_chk.setChecked(True)

        self.proj_tpl_combo = QComboBox()
        for pt_name in self.hub.template_manager.list_project_templates().keys():
            self.proj_tpl_combo.addItem(pt_name)
        proj_tpl_row = QHBoxLayout()
        proj_tpl_row.addWidget(self.proj_tpl_combo, 1)
        edit_tpl_inline_btn = QPushButton("Configure Templates…")
        edit_tpl_inline_btn.setObjectName("SubtleToolButton")
        edit_tpl_inline_btn.setToolTip("Open the 3-Tier Template Manager to create, edit, or import templates")
        edit_tpl_inline_btn.clicked.connect(self._open_template_manager)
        proj_tpl_row.addWidget(edit_tpl_inline_btn)

        self.tpl_desc_label = QLabel("")
        self.tpl_desc_label.setObjectName("MutedText")
        self.tpl_desc_label.setWordWrap(True)

        self.target_path_label = QLabel("")
        self.target_path_label.setObjectName("MutedText")

        left_layout.addRow("Project Name:", self.name_edit)
        left_layout.addRow("Project Code:", self.code_edit)
        left_layout.addRow("Client Root:", root_row)
        left_layout.addRow("", self.target_path_label)
        left_layout.addRow("", self.remember_root_chk)
        left_layout.addRow("Project Template:", proj_tpl_row)
        left_layout.addRow("", self.tpl_desc_label)
        left_outer.addLayout(left_layout)

        # Grouped Customize Components Box
        comp_box = QFrame()
        comp_box.setObjectName("PanelCard")
        comp_box_layout = QVBoxLayout(comp_box)
        comp_box_layout.setContentsMargins(10, 10, 10, 10)
        comp_box_layout.setSpacing(6)

        comp_hdr = QLabel("Customize Components")
        comp_hdr.setObjectName("SectionHeader")
        comp_box_layout.addWidget(comp_hdr)

        comp_hint = QLabel("Applies when adding a custom component or updating the highlighted one in the preview.")
        comp_hint.setObjectName("MutedText")
        comp_hint.setWordWrap(True)
        comp_box_layout.addWidget(comp_hint)

        comp_form = QFormLayout()
        comp_form.setSpacing(6)
        self.task_tpl_combo = QComboBox()
        for tt_name in self.hub.template_manager.list_task_templates().keys():
            self.task_tpl_combo.addItem(tt_name)

        self.seed_tpl_combo = QComboBox()
        for ft_name in self.hub.template_manager.list_file_templates().keys():
            self.seed_tpl_combo.addItem(ft_name)

        self.custom_comp_edit = QLineEdit()
        self.custom_comp_edit.setPlaceholderText("e.g. song_3/audio or sfx/foley")
        self.custom_comp_edit.returnPressed.connect(self._add_custom_component)
        comp_form.addRow("Component Template:", self.task_tpl_combo)
        comp_form.addRow("Working Seed:", self.seed_tpl_combo)
        comp_form.addRow("Component Path:", self.custom_comp_edit)
        comp_box_layout.addLayout(comp_form)

        comp_btn_row = QHBoxLayout()
        add_comp_btn = QPushButton("+ Add")
        add_comp_btn.setObjectName("SubtleToolButton")
        add_comp_btn.clicked.connect(self._add_custom_component)
        apply_comp_btn = QPushButton("Apply to Selected")
        apply_comp_btn.setObjectName("SubtleToolButton")
        apply_comp_btn.setToolTip("Applies the selected Component Template & Seed File above to the highlighted component in the tree")
        apply_comp_btn.clicked.connect(self._apply_template_to_selected_component)
        remove_comp_btn = QPushButton("Remove")
        remove_comp_btn.setObjectName("DangerButton")
        remove_comp_btn.clicked.connect(self._remove_selected_component)
        comp_btn_row.addWidget(add_comp_btn)
        comp_btn_row.addWidget(apply_comp_btn)
        comp_btn_row.addStretch()
        comp_btn_row.addWidget(remove_comp_btn)
        comp_box_layout.addLayout(comp_btn_row)
        left_outer.addWidget(comp_box)
        left_outer.addStretch(1)

        # Right side: Live interactive folder tree preview
        right_widget = QWidget()
        right_layout = QVBoxLayout(right_widget)
        right_layout.setContentsMargins(6, 0, 0, 0)
        lbl = QLabel("Live Folder Tree Preview")
        lbl.setObjectName("SectionHeader")
        self.preview_tree = QTreeWidget()
        self.preview_tree.setAlternatingRowColors(True)
        self.preview_tree.setIndentation(14)
        self.preview_tree.setHeaderLabels(["Folder / Seed File", "Role / Template"])
        self.preview_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.preview_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.preview_tree.setColumnWidth(0, 230)
        right_layout.addWidget(lbl)
        right_layout.addWidget(self.preview_tree)

        splitter.addWidget(left_widget)
        splitter.addWidget(right_widget)
        splitter.setSizes([380, 480])
        main_layout.addWidget(splitter, 1)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        create_btn = QPushButton("Create Project")
        create_btn.setObjectName("PrimaryButton")
        create_btn.clicked.connect(self._create_project)
        main_layout.addWidget(DialogFooter(self, cancel_button=cancel_btn, primary_button=create_btn))

        self._active_components: list[str] = []
        self._comp_task_overrides: dict[str, str] = {}
        self._comp_seed_overrides: dict[str, str] = {}
        self.proj_tpl_combo.currentTextChanged.connect(self._reset_components_from_template)
        self.task_tpl_combo.currentTextChanged.connect(self._on_task_template_changed)
        self.name_edit.textChanged.connect(self._refresh_preview_tree)
        self.root_dir_edit.textChanged.connect(self._on_root_dir_changed)
        self._on_task_template_changed(self.task_tpl_combo.currentText())
        self._reset_components_from_template()
        self.name_edit.setFocus()
        self.name_edit.selectAll()

    def _current_client_root(self) -> Optional[Path]:
        txt = self.root_dir_edit.text().strip()
        return Path(txt) if txt else None

    def _on_root_dir_changed(self) -> None:
        self._refresh_seed_combo()
        self._refresh_preview_tree()

    def _refresh_target_caption(self) -> None:
        pname = self.name_edit.text().strip() or "new_project"
        root_txt = self.root_dir_edit.text().strip()
        dest = Path(root_txt) / pname if root_txt else Path(pname)
        self.target_path_label.setText(f"Will create: {dest}")

    def _refresh_seed_combo(self) -> None:
        curr_data = self.seed_tpl_combo.currentData()
        tt_name = self.task_tpl_combo.currentText()
        tt = self.hub.template_manager.get_task_template(tt_name) if tt_name else None
        sw_key = tt.software if tt else "generic"
        prof = self.hub.get_software_profile(sw_key)
        croot = self._current_client_root()

        self.seed_tpl_combo.blockSignals(True)
        self.seed_tpl_combo.clear()
        resolved_auto = self.hub.template_manager.resolve_seed_template(
            software=sw_key,
            client_root=croot,
            task_template=tt,
        )
        auto_lbl = (
            f"Auto — {resolved_auto.display_label()}"
            if resolved_auto is not None
            else "Auto — (Root / Global Default)"
        )
        self.seed_tpl_combo.addItem(auto_lbl, "")

        avail = self.hub.template_manager.seeds.list_available_for_software(
            sw_key,
            client_root=croot,
            legacy_files_dir=self.hub.template_manager.files_dir,
            preferred_extensions=prof.extensions,
        )
        seen_specs: set[str] = set()
        for r in avail:
            spec = r.stream_spec()
            if spec in seen_specs:
                continue
            seen_specs.add(spec)
            self.seed_tpl_combo.addItem(r.display_label(), spec)

        for fname in sorted(self.hub.template_manager.list_file_templates().keys()):
            if fname not in seen_specs:
                self.seed_tpl_combo.addItem(f"[Legacy] {fname}", fname)
                seen_specs.add(fname)

        if curr_data:
            idx = self.seed_tpl_combo.findData(curr_data)
            if idx >= 0:
                self.seed_tpl_combo.setCurrentIndex(idx)
        self.seed_tpl_combo.blockSignals(False)

    def _on_task_template_changed(self, task_tpl_name: str) -> None:
        if not task_tpl_name:
            return
        self._refresh_seed_combo()
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
            seed_spec = str(self.seed_tpl_combo.currentData() or "").strip()
            if seed_spec:
                self._comp_seed_overrides[c] = seed_spec
            else:
                self._comp_seed_overrides.pop(c, None)
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
            seed_spec = str(self.seed_tpl_combo.currentData() or "").strip()
            if seed_spec:
                self._comp_seed_overrides[comp_path] = seed_spec
            else:
                self._comp_seed_overrides.pop(comp_path, None)
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
            client_root=self._current_client_root(),
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

    def _open_root_seed_manager(self) -> None:
        self._open_template_manager(initial_tab=2)

    def _open_template_manager(self, initial_tab: int = 0) -> None:
        dlg = TemplateManagerDialog(
            self,
            self.hub,
            initial_tab=initial_tab,
            initial_root=self._current_client_root(),
        )
        dlg.exec()
        curr_pt = self.proj_tpl_combo.currentText()
        curr_tt = self.task_tpl_combo.currentText()
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

        self._refresh_seed_combo()
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

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        initial_prefix: str = "",
        project_root: Optional[Path] = None,
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.project_root = project_root
        self._software_touched = False
        self.setWindowTitle("Add Triad Component")
        self.resize(520, 310)

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
            self.software_combo.addItem(get_software_icon(prof), f"{prof.display_name} ({prof.key})", prof.key)

        self.seed_combo = QComboBox()

        self.seed_chk = QCheckBox("Create initial working/v001 from template seed file")
        self.seed_chk.setChecked(True)

        form.addRow("Component Path:", self.path_edit)
        form.addRow("Component Template:", self.task_tpl_combo)
        form.addRow("", self.tpl_summary_label)
        form.addRow("Bound Software:", self.software_combo)
        form.addRow("Initial Seed Template:", self.seed_combo)
        form.addRow("", self.seed_chk)
        layout.addLayout(form)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        create_btn = QPushButton("Create Component")
        create_btn.setObjectName("PrimaryButton")
        create_btn.setDefault(True)
        create_btn.clicked.connect(self._validate_and_accept)
        layout.addWidget(DialogFooter(self, cancel_button=cancel_btn, primary_button=create_btn))

        self.task_tpl_combo.currentTextChanged.connect(self._on_template_changed)
        self.software_combo.activated.connect(self._on_software_user_selected)
        self.software_combo.currentIndexChanged.connect(self._refresh_seed_choices)
        self.seed_chk.toggled.connect(self.seed_combo.setEnabled)
        self._on_template_changed(self.task_tpl_combo.currentText())
        self.path_edit.setFocus()
        self.path_edit.setCursorPosition(len(self.path_edit.text()))

    def _on_software_user_selected(self, _idx: int) -> None:
        self._software_touched = True

    def _on_template_changed(self, tpl_name: str) -> None:
        if not tpl_name:
            return
        tt = self.hub.template_manager.get_task_template(tpl_name)
        if not self._software_touched:
            idx = self.software_combo.findData(tt.software)
            if idx >= 0:
                self.software_combo.blockSignals(True)
                self.software_combo.setCurrentIndex(idx)
                self.software_combo.blockSignals(False)
        self._refresh_seed_choices()

    def _refresh_seed_choices(self) -> None:
        sw_key = str(self.software_combo.currentData() or "generic")
        tt = self.hub.template_manager.get_task_template(self.task_tpl_combo.currentText())
        prof = self.hub.get_software_profile(sw_key)
        resolved_auto = self.hub.template_manager.resolve_seed_template(
            software=sw_key,
            project_root=self.project_root,
            task_template=tt,
        )
        desc = tt.description or "Component template"
        seed_txt = resolved_auto.display_label() if resolved_auto is not None else (tt.seed_file_template or "none")
        self.tpl_summary_label.setText(f"{desc}  •  Active Seed: {seed_txt}")

        prev_data = self.seed_combo.currentData()
        self.seed_combo.blockSignals(True)
        self.seed_combo.clear()
        auto_label = f"Auto — {resolved_auto.display_label()}" if resolved_auto is not None else "Auto — (blank file)"
        self.seed_combo.addItem(auto_label, "")

        scope_names = {
            TemplateScope.PROJECT: "Project",
            TemplateScope.CLIENT_ROOT: "Root",
            TemplateScope.GLOBAL: "Global",
        }
        avail = self.hub.template_manager.seeds.list_available_for_software(
            sw_key,
            project_root=self.project_root,
            legacy_files_dir=self.hub.template_manager.files_dir,
            preferred_extensions=prof.extensions,
            include_all_versions=True,
        )
        seen_specs: set[str] = set()
        for r in avail:
            if r.is_legacy:
                if r.template_name not in seen_specs:
                    self.seed_combo.addItem(f"[Legacy] {r.template_name}", r.template_name)
                    seen_specs.add(r.template_name)
            else:
                spec = r.reference_spec()
                if spec not in seen_specs:
                    sc = scope_names.get(r.scope, r.scope.value)
                    pin_tag = " ★default" if r.is_pinned_version else ""
                    self.seed_combo.addItem(f"[{sc}] {r.template_name} v{r.version:03d}{pin_tag}", spec)
                    seen_specs.add(spec)

        if prev_data:
            idx = self.seed_combo.findData(prev_data)
            if idx >= 0:
                self.seed_combo.setCurrentIndex(idx)
        self.seed_combo.blockSignals(False)

    def _validate_and_accept(self) -> None:
        cleaned = self.path_edit.text().strip().strip("/")
        if not cleaned:
            QMessageBox.warning(self, "Validation", "Component path is required (e.g. 'audio' or 'song_1/mixing').")
            self.path_edit.setFocus()
            return
        self.accept()

    def get_values(self) -> dict[str, object]:
        seed_override = str(self.seed_combo.currentData() or "").strip() or None
        return {
            "component_path": self.path_edit.text().strip().strip("/"),
            "task_template": self.task_tpl_combo.currentText(),
            "software_override": str(self.software_combo.currentData() or "generic"),
            "seed_initial_working": self.seed_chk.isChecked(),
            "seed_override": seed_override,
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
        self.app_icon_preview = QLabel()
        self.app_icon_preview.setFixedSize(32, 32)
        self.app_icon_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.app_icon_preview.setToolTip("Icon shown on the Launch button for this software")
        exe_row.addWidget(self.app_icon_preview)
        exe_row.addWidget(self.exe_edit, 1)
        browse_exe_btn = QPushButton("Browse…")
        browse_exe_btn.clicked.connect(self._browse_executable)
        auto_exe_btn = QPushButton("Auto-Detect")
        auto_exe_btn.clicked.connect(self._auto_detect_executable)
        exe_row.addWidget(browse_exe_btn)
        exe_row.addWidget(auto_exe_btn)
        self.exe_status_label = QLabel("")
        self.exe_status_label.setObjectName("MutedText")

        self.icon_edit = QLineEdit()
        self.icon_edit.setPlaceholderText("Optional .ico / .png / .svg / .exe (blank = use executable's icon)")
        icon_row = QHBoxLayout()
        icon_row.addWidget(self.icon_edit, 1)
        browse_icon_btn = QPushButton("Browse…")
        browse_icon_btn.clicked.connect(self._browse_icon)
        icon_row.addWidget(browse_icon_btn)

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
        form.addRow("Icon (optional):", icon_row)
        form.addRow("Extensions (comma-sep):", self.ext_edit)
        form.addRow("Version-Up Strategy:", self.strategy_combo)
        form.addRow("Ignore Patterns:", self.ignore_edit)
        form.addRow("", self.lock_chk)
        form.addRow("Python Hook Plugin:", self.plugin_combo)

        self.feedback_banner = InlineStatusBanner(self)
        form.addRow("", self.feedback_banner)

        save_btn = QPushButton("Save / Add Software Profile")
        save_btn.setObjectName("PrimaryButton")
        save_btn.clicked.connect(self._save_current)
        integ_btn = QPushButton("DCC Integrations…")
        integ_btn.clicked.connect(self._open_integrations_manager)
        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        form.addRow(
            "",
            DialogFooter(
                self,
                left_widgets=[integ_btn],
                cancel_button=close_btn,
                primary_button=save_btn,
            ),
        )

        layout.addWidget(right, 2)
        self.exe_edit.textChanged.connect(self._update_exe_status)
        self.exe_edit.textChanged.connect(self._update_icon_preview)
        self.icon_edit.textChanged.connect(self._update_icon_preview)
        self.sw_list.currentItemChanged.connect(self._on_list_item_changed)
        self._refresh_list()

    def _browse_icon(self) -> None:
        file_str, _ = QFileDialog.getOpenFileName(
            self,
            "Select Software Icon",
            self.icon_edit.text().strip() or self.exe_edit.text().strip() or "",
            "Icons & Apps (*.ico *.png *.svg *.exe *.icns);;All Files (*)",
        )
        if file_str:
            self.icon_edit.setText(file_str)

    def _update_icon_preview(self, *_args: object) -> None:
        probe = SoftwareProfile(
            key="preview",
            display_name="preview",
            executable=self.exe_edit.text().strip(),
            icon=self.icon_edit.text().strip(),
        )
        icon = resolve_software_icon(probe)
        if icon is None:
            icon = get_icon("play", TEXT_MUTED, 20)
            self.app_icon_preview.setToolTip("No app icon found — the Launch button will use the default glyph")
        else:
            self.app_icon_preview.setToolTip("Icon shown on the Launch button for this software")
        self.app_icon_preview.setPixmap(icon.pixmap(QSize(28, 28)))

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
        initial = key if key in ("reaper", "resolve", "filmcraft") else ""
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
        self.icon_edit.clear()
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
            item = QListWidgetItem(get_software_icon(prof), f"{prof.display_name} [{k}]")
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
        self.icon_edit.setText(prof.icon)
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
            self.feedback_banner.set_feedback("Profile Key is required (e.g. 'reaper' or 'davinci').", "warn")
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
            icon=self.icon_edit.text().strip(),
        )
        self.hub.set_software_profile(prof)
        self._refresh_list(select_key=k)
        self.feedback_banner.set_feedback(f"✓ Saved software profile '{k}'", "ok")


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
        browse_client_btn = QPushButton("Browse…")
        browse_client_btn.clicked.connect(self._browse_client_root)
        save_client_btn = QPushButton("Set Client Root")
        save_client_btn.setObjectName("SubtleToolButton")
        save_client_btn.clicked.connect(self._save_client_root)
        root_tpls_btn = QPushButton("Root Seed Templates…")
        root_tpls_btn.setObjectName("SubtleToolButton")
        root_tpls_btn.setToolTip("Manage versioned per-software seed templates for this Client Workspace Root")
        root_tpls_btn.clicked.connect(self._open_root_templates)
        client_row.addWidget(QLabel("Client Root (Local):"))
        client_row.addWidget(self.client_root_edit, 1)
        client_row.addWidget(browse_client_btn)
        client_row.addWidget(save_client_btn)
        client_row.addWidget(root_tpls_btn)
        layout.addLayout(client_row)

        # Optional Vault Header Box
        vault_row = QHBoxLayout()
        self.vault_path_edit = QLineEdit(self.hub.config.vault.path)
        self.vault_path_edit.setPlaceholderText("Optional SMB/NAS path (e.g. \\\\NAS\\ObolVault or Z:\\ObolVault)")
        browse_btn = QPushButton("Browse…")
        browse_btn.clicked.connect(self._browse_vault)
        self.connect_btn = QPushButton()
        self.connect_btn.clicked.connect(self._toggle_vault)
        vault_row.addWidget(QLabel("Server Vault (Optional):"))
        vault_row.addWidget(self.vault_path_edit, 1)
        vault_row.addWidget(browse_btn)
        vault_row.addWidget(self.connect_btn)
        layout.addLayout(vault_row)

        self.feedback_banner = InlineStatusBanner(self)
        layout.addWidget(self.feedback_banner)

        # Projects Table
        self.table = QTableWidget(0, 4)
        self.table.setAlternatingRowColors(True)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setHorizontalHeaderLabels(["Project", "Presence", "Local Path", "Actions"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.table.cellDoubleClicked.connect(self._on_row_double_clicked)
        layout.addWidget(self.table)

        new_proj_btn = QPushButton("+ New Project…")
        new_proj_btn.setObjectName("PrimaryButton")
        new_proj_btn.clicked.connect(self._create_new_project_from_manager)
        reg_proj_btn = QPushButton("Register Existing Project Folder…")
        reg_proj_btn.clicked.connect(self._register_existing_project_folder)
        close_btn = QPushButton("Done")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(
            DialogFooter(
                self,
                left_widgets=[new_proj_btn, reg_proj_btn],
                cancel_button=close_btn,
            )
        )

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
            self.feedback_banner.set_feedback("Enter a Client Workspace Root folder first.", "warn")
            return
        new_root = self.hub.set_client_root(Path(p))
        self.client_root_edit.setText(str(new_root))
        self._refresh_view()
        self.feedback_banner.set_feedback(f"✓ Client Root set to {new_root}", "ok")

    def _open_root_templates(self) -> None:
        p = self.client_root_edit.text().strip()
        root_dir = Path(p) if p else self.hub.get_client_root()
        dlg = TemplateManagerDialog(self, self.hub, initial_tab=2, initial_root=root_dir)
        dlg.exec()

    def _browse_vault(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Server Vault Folder")
        if folder:
            self.vault_path_edit.setText(folder)

    def _toggle_vault(self) -> None:
        if self.hub.get_vault_status() == VaultStatus.ONLINE:
            self.hub.disconnect_vault()
            self.feedback_banner.set_feedback("Disconnected Server Vault (Local Mode)", "info")
        else:
            p = self.vault_path_edit.text().strip()
            if not p:
                self.feedback_banner.set_feedback("Enter or browse to a folder / SMB path to connect.", "warn")
                return
            self.hub.connect_vault(Path(p))
            self.feedback_banner.set_feedback(f"✓ Connected Server Vault: {p}", "ok")
        self._refresh_view()

    def _refresh_view(self) -> None:
        st = self.hub.get_vault_status()
        self.connect_btn.setText("Disconnect Vault" if st == VaultStatus.ONLINE else "Connect Vault")
        projects = self.hub.list_projects()
        active_name = self.hub.config.last_active_project
        self.table.setRowCount(len(projects))

        presence_labels = {
            FilePresence.LOCAL_ONLY: "Local Only",
            FilePresence.SYNCED: "Synced",
            FilePresence.MODIFIED_LOCAL: "Modified Locally",
            FilePresence.REMOTE_ONLY: "Vault Only",
            FilePresence.SPARSE: "Partial (Sparse)",
        }

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

            pres_label = presence_labels.get(pres, pres.value)
            pres_item = QTableWidgetItem(pres_label)
            pres_item.setToolTip(pres.value)
            pres_item.setFlags(pres_item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            path_item = QTableWidgetItem(entry.local_path)
            path_item.setFlags(path_item.flags() & ~Qt.ItemFlag.ItemIsEditable)

            self.table.setItem(row_idx, 0, name_item)
            self.table.setItem(row_idx, 1, pres_item)
            self.table.setItem(row_idx, 2, path_item)

            act_widget = QWidget()
            act_layout = QHBoxLayout(act_widget)
            act_layout.setContentsMargins(4, 2, 4, 2)
            act_layout.setSpacing(4)
            open_btn = QPushButton("Open" if entry.name != active_name else "Active")
            open_btn.setObjectName("SubtleToolButton")
            if entry.name == active_name:
                open_btn.setEnabled(False)
            open_btn.clicked.connect(lambda _=False, n=entry.name: self._select_proj(n))
            act_layout.addWidget(open_btn)

            more_btn = QPushButton()
            more_btn.setObjectName("SubtleToolButton")
            more_btn.setIcon(get_icon("more_horiz", TEXT_MUTED, ICON_SM))
            more_btn.setIconSize(QSize(ICON_SM, ICON_SM))
            more_btn.setFixedSize(ICON_BTN_SIZE, ICON_BTN_SIZE)
            more_btn.setToolTip("Vault & project actions")
            row_menu = QMenu(more_btn)
            if st == VaultStatus.ONLINE:
                act_push = row_menu.addAction("Backup to Vault")
                act_push.triggered.connect(lambda _=False, n=entry.name: self._push_proj(n, False))
                act_arch = row_menu.addAction("Archive (Free Space)")
                act_arch.triggered.connect(lambda _=False, n=entry.name: self._push_proj(n, True))
                act_co = row_menu.addAction("Check Out from Vault")
                act_co.triggered.connect(lambda _=False, n=entry.name: self._checkout_proj(n))
                row_menu.addSeparator()
            act_del = row_menu.addAction("Remove Project…")
            act_del.triggered.connect(lambda _=False, n=entry.name: self._delete_proj(n))
            more_btn.setMenu(row_menu)
            act_layout.addWidget(more_btn)

            self.table.setCellWidget(row_idx, 3, act_widget)
            self.table.setRowHeight(row_idx, 36)
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
        self.feedback_banner.set_feedback(f"Removed project '{entry.name}'", "info")

    def _push_proj(self, name: str, evict: bool) -> None:
        run_with_busy_state(
            self,
            lambda: self.sync.push_paths(name, ["**"], evict_local=evict),
            on_success=lambda _res: (
                self._refresh_view(),
                self.feedback_banner.set_feedback(
                    f"✓ {'Archived' if evict else 'Backed up'} '{name}' to Vault", "ok"
                ),
            ),
            on_error=lambda exc: self.feedback_banner.set_feedback(str(exc), "err"),
            disable_widgets=[self.table],
            busy_message=f"{'Archiving' if evict else 'Backing up'} '{name}'…",
            status_callback=lambda msg: self.feedback_banner.set_feedback(msg, "info"),
        )

    def _checkout_proj(self, name: str) -> None:
        run_with_busy_state(
            self,
            lambda: self.sync.pull_paths(name, ["**"]),
            on_success=lambda _res: (
                self._refresh_view(),
                self.feedback_banner.set_feedback(f"✓ Checked out '{name}' from Vault", "ok"),
            ),
            on_error=lambda exc: self.feedback_banner.set_feedback(str(exc), "err"),
            disable_widgets=[self.table],
            busy_message=f"Checking out '{name}'…",
            status_callback=lambda msg: self.feedback_banner.set_feedback(msg, "info"),
        )


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
        self.filter_edit.setPlaceholderText("Filter by component, asset, author, source, or license…")
        self.filter_edit.setClearButtonEnabled(True)
        attach_search_icon(self.filter_edit)
        self.filter_edit.textChanged.connect(self._apply_filter)
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("MutedText")
        top_bar.addWidget(self.filter_edit, 1)
        top_bar.addWidget(self.summary_label)
        layout.addLayout(top_bar)

        self.table = QTableWidget(len(self.rows), 8)
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setHorizontalHeaderLabels(
            ["Component", "Repo Asset", "Latest", "Used In", "Author", "Source", "URL", "License"]
        )
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(6, QHeaderView.ResizeMode.Stretch)

        warn_count = 0
        for i, r in enumerate(self.rows):
            used_raw = r.get("used_by_working", [])
            used_str = ", ".join(str(x) for x in used_raw) if isinstance(used_raw, list) else str(used_raw or "")
            lic_str = str(r.get("license", "Unspecified"))
            url_str = str(r.get("url", ""))
            vals = [
                str(r.get("component", "")),
                f"repo/{r.get('asset_path', '')}",
                str(r.get("latest_version", "")),
                used_str or "—",
                str(r.get("author", "")),
                str(r.get("source", "")),
                url_str,
                lic_str,
            ]
            for col_idx, val in enumerate(vals):
                item = QTableWidgetItem(val)
                if col_idx == 6 and url_str:
                    item.setForeground(QBrush(QColor(ACCENT_LIGHT)))
                    item.setToolTip(f"Click to open: {url_str}")
                if col_idx == 7 and (lic_str == "Unspecified" or "temp" in lic_str.lower() or "scratch" in lic_str.lower()):
                    item.setForeground(QBrush(QColor(WARNING_TEXT)))
                    item.setBackground(QBrush(QColor(WARNING_BG)))
                    item.setToolTip("Review license/clearance before publishing")
                    warn_count += 1
                self.table.setItem(i, col_idx, item)
        self.table.cellClicked.connect(self._on_cell_clicked)
        self.table.cellDoubleClicked.connect(self._on_cell_double_clicked)
        layout.addWidget(self.table, 1)

        self.empty_filter_label = QLabel("No matching credits")
        self.empty_filter_label.setObjectName("MutedText")
        self.empty_filter_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_filter_label.hide()
        layout.addWidget(self.empty_filter_label)

        if warn_count:
            self.summary_label.setText(f"{len(self.rows)} assets  •  ⚠ {warn_count} need license review")
        else:
            self.summary_label.setText(f"{len(self.rows)} repo assets")

        copy_md_btn = QPushButton("Copy Credits (Markdown)")
        copy_md_btn.clicked.connect(self._copy_markdown)
        export_csv_btn = QPushButton("Export CSV…")
        export_csv_btn.clicked.connect(self._export_csv)
        self.copy_status_lbl = QLabel("")
        self.copy_status_lbl.setObjectName("SuccessText")
        close_btn = QPushButton("Close")
        close_btn.setObjectName("SubtleToolButton")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(
            DialogFooter(
                self,
                left_widgets=[copy_md_btn, export_csv_btn, self.copy_status_lbl],
                cancel_button=close_btn,
            )
        )

    def _on_cell_clicked(self, row: int, col: int) -> None:
        self._on_cell_double_clicked(row, col)

    def _on_cell_double_clicked(self, row: int, col: int) -> None:
        if col != 6:
            return
        item = self.table.item(row, col)
        url_txt = item.text().strip() if item else ""
        if url_txt:
            QDesktopServices.openUrl(QUrl.fromUserInput(url_txt))

    def _apply_filter(self, text: str) -> None:
        needle = text.strip().lower()
        visible_count = 0
        for row_idx in range(self.table.rowCount()):
            if not needle:
                self.table.setRowHidden(row_idx, False)
                visible_count += 1
                continue
            matched = any(
                needle in (self.table.item(row_idx, c).text().lower() if self.table.item(row_idx, c) else "")
                for c in range(self.table.columnCount())
            )
            self.table.setRowHidden(row_idx, not matched)
            if matched:
                visible_count += 1
        self.empty_filter_label.setVisible(bool(needle) and self.table.rowCount() > 0 and visible_count == 0)

    def format_markdown(self) -> str:
        lines = [f"# Attribution & Credits — {self.project_name}", ""]
        if not self.rows:
            lines.append("_No repo assets registered in this project._")
            return "\n".join(lines)
        for r in self.rows:
            url_part = f" | URL: {r.get('url')}" if r.get("url") else ""
            lines.append(
                f"- **{r.get('asset_path', '')}** ({r.get('latest_version', '')}, `{r.get('component', '')}`) — "
                f"Author: {r.get('author', 'Unspecified')} | License: {r.get('license', 'Unspecified')} | "
                f"Source: {r.get('source', 'Unspecified')}{url_part}"
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
            writer.writerow(["Component", "Repo Asset", "Latest", "Used In", "Author", "Source", "URL", "License", "Tags"])
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
                    r.get("url", ""),
                    r.get("license", ""),
                    r.get("tags", ""),
                ])
        self.copy_status_lbl.setText(f"✓ Exported {Path(dest_str).name}")


class SaveSeedTemplateDialog(QDialog):
    """Modal for saving a working file (or legacy seed) as a scoped, versioned software seed template."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        source_file: Path,
        software_key: str = "reaper",
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
        default_comment: str = "",
        default_template_name: str = "default",
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.source_file = source_file
        self.project_root = project_root
        self.client_root = client_root or hub.get_client_root()
        self.saved_stream = None
        self.saved_version = None
        self.saved_scope = TemplateScope.CLIENT_ROOT

        self.setWindowTitle("Save as Scoped Versioned Seed Template")
        self.resize(520, 340)

        layout = QVBoxLayout(self)
        header = QLabel(f"Source File: <b>{source_file.name}</b>")
        header.setWordWrap(True)
        layout.addWidget(header)

        form = QFormLayout()

        self.software_combo = QComboBox()
        for sw_k in sorted(self.hub.config.software.keys()):
            prof = self.hub.config.software[sw_k]
            self.software_combo.addItem(f"{prof.display_name} ({prof.key})", prof.key)
        sw_idx = self.software_combo.findData(normalize_software_key(software_key))
        if sw_idx >= 0:
            self.software_combo.setCurrentIndex(sw_idx)

        self.scope_combo = QComboBox()
        croot_lbl = self.client_root.name or str(self.client_root)
        self.scope_combo.addItem(f"Client Workspace Root ({croot_lbl})", TemplateScope.CLIENT_ROOT.value)
        if self.project_root is not None:
            self.scope_combo.addItem(f"Project ({self.project_root.name})", TemplateScope.PROJECT.value)
        self.scope_combo.addItem("Global (~/.obol)", TemplateScope.GLOBAL.value)

        self.template_name_combo = QComboBox()
        self.template_name_combo.setEditable(True)
        self.template_name_combo.setEditText(default_template_name or "default")

        self.next_version_label = QLabel("")
        self.next_version_label.setObjectName("MutedText")

        self.comment_edit = QLineEdit(default_comment)
        self.comment_edit.setPlaceholderText("Optional version note (e.g. '48kHz show bus routing')")
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("Optional comma-separated tags (e.g. stereo, podcast)")

        self.set_default_tpl_chk = QCheckBox("Set as default template for this software in selected scope")
        self.pin_version_chk = QCheckBox("Pin this version as default (unchecked = Always Latest)")
        self.pin_version_chk.setChecked(False)

        form.addRow("Software Profile:", self.software_combo)
        form.addRow("Target Scope:", self.scope_combo)
        form.addRow("Template Name:", self.template_name_combo)
        form.addRow("Target Version:", self.next_version_label)
        form.addRow("Version Note:", self.comment_edit)
        form.addRow("Tags:", self.tags_edit)
        form.addRow("", self.set_default_tpl_chk)
        form.addRow("", self.pin_version_chk)
        layout.addLayout(form)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        save_btn = QPushButton("Save Seed Template")
        save_btn.setObjectName("PrimaryButton")
        save_btn.setDefault(True)
        save_btn.clicked.connect(self._save)
        layout.addWidget(DialogFooter(self, cancel_button=cancel_btn, primary_button=save_btn))

        self.software_combo.currentIndexChanged.connect(self._refresh_streams)
        self.scope_combo.currentIndexChanged.connect(self._refresh_streams)
        self.template_name_combo.currentTextChanged.connect(self._refresh_version_preview)
        self._refresh_streams()

    def _current_scope(self) -> TemplateScope:
        return TemplateScope(str(self.scope_combo.currentData() or TemplateScope.CLIENT_ROOT.value))

    def _current_software(self) -> str:
        return str(self.software_combo.currentData() or "generic")

    def _refresh_streams(self) -> None:
        sw = self._current_software()
        scope = self._current_scope()
        curr_txt = self.template_name_combo.currentText().strip() or "default"
        manifest = self.hub.template_manager.seeds.load_manifest(
            sw,
            scope,
            project_root=self.project_root,
            client_root=self.client_root,
        )
        self.template_name_combo.blockSignals(True)
        self.template_name_combo.clear()
        names = sorted(manifest.templates.keys())
        if "default" not in names:
            names.insert(0, "default")
        for n in names:
            self.template_name_combo.addItem(n)
        self.template_name_combo.setEditText(curr_txt)
        self.template_name_combo.blockSignals(False)
        self.set_default_tpl_chk.setChecked(not bool(manifest.default_template))
        self._refresh_version_preview()

    def _refresh_version_preview(self) -> None:
        sw = self._current_software()
        scope = self._current_scope()
        tname = normalize_template_name(self.template_name_combo.currentText())
        manifest = self.hub.template_manager.seeds.load_manifest(
            sw,
            scope,
            project_root=self.project_root,
            client_root=self.client_root,
        )
        stream = manifest.templates.get(tname)
        if stream is None:
            self.next_version_label.setText(f"Will create new template '{tname}' (v001)")
        else:
            next_v = stream.latest_version_number() + 1
            self.next_version_label.setText(f"Will add v{next_v:03d} to existing template '{tname}'")

    def _save(self) -> None:
        sw = self._current_software()
        scope = self._current_scope()
        tname = normalize_template_name(self.template_name_combo.currentText())
        prof = self.hub.get_software_profile(sw)
        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        stream, ver = self.hub.template_manager.seeds.add_version(
            software=sw,
            template_name=tname,
            source_path=self.source_file,
            scope=scope,
            comment=self.comment_edit.text().strip(),
            author=self.hub.config.author_name,
            tags=tags,
            set_as_default_template=self.set_default_tpl_chk.isChecked(),
            set_as_default_version=self.pin_version_chk.isChecked(),
            project_root=self.project_root,
            client_root=self.client_root,
            ignore_patterns=prof.ignore_patterns,
            lock_previous=prof.lock_previous_versions,
            preferred_extensions=prof.extensions,
        )
        self.saved_stream = stream
        self.saved_version = ver
        self.saved_scope = scope
        self.accept()


class TemplateManagerDialog(QDialog):
    """3-Tier Template Manager GUI for Project, Component, Scoped Versioned Software Seeds, and Legacy Seed Files."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        active_project_root: Optional[Path] = None,
        active_component_path: str = "",
        initial_tab: int = 0,
        initial_root: Optional[Path] = None,
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        self.tm = hub.template_manager
        self.active_project_root = active_project_root
        self.active_component_path = active_component_path
        self._seed_client_root = (initial_root or hub.get_client_root()).expanduser().resolve()
        self.setWindowTitle("3-Tier Template Manager (Projects, Components & Scoped Software Seeds)")
        self.resize(940, 560)

        from PySide6.QtWidgets import QInputDialog, QPlainTextEdit, QTabWidget

        self._QInputDialog = QInputDialog

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

        # ---------------- Tab 3: Scoped & Versioned Software Seed Templates ----------------
        tab_seeds = QWidget()
        seeds_layout = QVBoxLayout(tab_seeds)

        scope_bar = QHBoxLayout()
        scope_bar.addWidget(QLabel("Scope:"))
        self.seed_scope_combo = QComboBox()
        self.seed_scope_combo.addItem("Client Workspace Root", TemplateScope.CLIENT_ROOT.value)
        if self.active_project_root is not None:
            self.seed_scope_combo.addItem(f"Active Project ({self.active_project_root.name})", TemplateScope.PROJECT.value)
        self.seed_scope_combo.addItem("Global (~/.obol)", TemplateScope.GLOBAL.value)
        scope_bar.addWidget(self.seed_scope_combo)

        self.seed_root_lbl = QLabel("Root:")
        self.seed_root_edit = QLineEdit(str(self._seed_client_root))
        self.seed_browse_root_btn = QPushButton("Browse Root…")
        self.seed_browse_root_btn.clicked.connect(self._browse_seed_root)
        scope_bar.addWidget(self.seed_root_lbl)
        scope_bar.addWidget(self.seed_root_edit, 1)
        scope_bar.addWidget(self.seed_browse_root_btn)
        seeds_layout.addLayout(scope_bar)

        self.seed_effective_banner = QLabel("")
        self.seed_effective_banner.setObjectName("MutedText")
        self.seed_effective_banner.setWordWrap(True)
        seeds_layout.addWidget(self.seed_effective_banner)

        seed_splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left pane: Software list
        sw_pane = QWidget()
        sw_layout = QVBoxLayout(sw_pane)
        sw_layout.setContentsMargins(0, 0, 0, 0)
        sw_hdr = QLabel("Software")
        sw_hdr.setObjectName("SectionHeader")
        sw_layout.addWidget(sw_hdr)
        self.seed_software_list = QListWidget()
        sw_layout.addWidget(self.seed_software_list, 1)
        seed_splitter.addWidget(sw_pane)

        # Middle pane: Named templates for (scope, software)
        stream_pane = QWidget()
        stream_layout = QVBoxLayout(stream_pane)
        stream_layout.setContentsMargins(0, 0, 0, 0)
        stream_hdr = QLabel("Named Templates")
        stream_hdr.setObjectName("SectionHeader")
        stream_layout.addWidget(stream_hdr)
        self.seed_stream_list = QListWidget()
        stream_layout.addWidget(self.seed_stream_list, 1)

        stream_btns_top = QHBoxLayout()
        self.seed_import_btn = QPushButton("+ Import / New…")
        self.seed_import_btn.setObjectName("PrimaryButton")
        self.seed_import_btn.clicked.connect(self._seed_import_new)
        self.seed_set_default_btn = QPushButton("Set Default")
        self.seed_set_default_btn.clicked.connect(self._seed_set_default_stream)
        stream_btns_top.addWidget(self.seed_import_btn)
        stream_btns_top.addWidget(self.seed_set_default_btn)
        stream_layout.addLayout(stream_btns_top)

        stream_btns_bot = QHBoxLayout()
        self.seed_inherit_btn = QPushButton("Inherit (Clear)")
        self.seed_inherit_btn.setToolTip("Clear this scope's default override so it inherits from parent scope")
        self.seed_inherit_btn.clicked.connect(self._seed_clear_default_stream)
        self.seed_del_stream_btn = QPushButton("Delete")
        self.seed_del_stream_btn.setObjectName("DangerButton")
        self.seed_del_stream_btn.clicked.connect(self._seed_delete_stream)
        stream_btns_bot.addWidget(self.seed_inherit_btn)
        stream_btns_bot.addWidget(self.seed_del_stream_btn)
        stream_layout.addLayout(stream_btns_bot)
        seed_splitter.addWidget(stream_pane)

        # Right pane: Versions + default version policy
        ver_pane = QWidget()
        ver_layout = QVBoxLayout(ver_pane)
        ver_layout.setContentsMargins(0, 0, 0, 0)

        ver_top_row = QHBoxLayout()
        ver_hdr = QLabel("Template Versions")
        ver_hdr.setObjectName("SectionHeader")
        ver_top_row.addWidget(ver_hdr)
        ver_top_row.addStretch()
        ver_top_row.addWidget(QLabel("Default Version:"))
        self.seed_default_ver_combo = QComboBox()
        self.seed_default_ver_combo.activated.connect(self._seed_apply_default_version)
        ver_top_row.addWidget(self.seed_default_ver_combo)
        ver_layout.addLayout(ver_top_row)

        self.seed_versions_table = QTableWidget(0, 5)
        self.seed_versions_table.verticalHeader().setVisible(False)
        self.seed_versions_table.setAlternatingRowColors(True)
        self.seed_versions_table.setHorizontalHeaderLabels(["Version", "Primary File", "Default", "Created", "Note"])
        self.seed_versions_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.seed_versions_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.seed_versions_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.seed_versions_table.horizontalHeader().setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        ver_layout.addWidget(self.seed_versions_table, 1)

        ver_btns = QHBoxLayout()
        ver_btns.setSpacing(6)
        self.seed_vup_open_btn = QPushButton("Version Up && Open")
        self.seed_vup_open_btn.setObjectName("PrimaryButton")
        self.seed_vup_open_btn.clicked.connect(lambda: self._seed_version_up(open_in_app=True))
        self.seed_add_ver_btn = QPushButton("+ Add Version…")
        self.seed_add_ver_btn.setObjectName("SubtleToolButton")
        self.seed_add_ver_btn.clicked.connect(self._seed_add_version_to_stream)

        seed_more_btn = QPushButton("More")
        seed_more_btn.setObjectName("SubtleToolButton")
        seed_more_menu = QMenu(seed_more_btn)
        act_s_open = seed_more_menu.addAction("Open in App")
        act_s_open.triggered.connect(self._seed_open_selected_version)
        act_s_note = seed_more_menu.addAction("Edit Version Note…")
        act_s_note.triggered.connect(self._seed_edit_version_note)
        act_s_dir = seed_more_menu.addAction("Open Scope Folder")
        act_s_dir.triggered.connect(self._seed_open_scope_folder)
        seed_more_menu.addSeparator()
        act_s_del = seed_more_menu.addAction("Delete Version")
        act_s_del.triggered.connect(self._seed_delete_version)
        seed_more_btn.setMenu(seed_more_menu)

        ver_btns.addWidget(self.seed_vup_open_btn)
        ver_btns.addWidget(self.seed_add_ver_btn)
        ver_btns.addStretch()
        ver_btns.addWidget(seed_more_btn)
        ver_layout.addLayout(ver_btns)
        seed_splitter.addWidget(ver_pane)

        seed_splitter.setSizes([190, 230, 480])
        seeds_layout.addWidget(seed_splitter, 1)
        self.tabs.addTab(tab_seeds, "3. Software Seed Templates")

        # ---------------- Tab 4: Legacy Working Seed Files ----------------
        tab_files = QWidget()
        tf_layout = QVBoxLayout(tab_files)
        self.seed_files_table = QTableWidget(0, 3)
        self.seed_files_table.verticalHeader().setVisible(False)
        self.seed_files_table.setAlternatingRowColors(True)
        self.seed_files_table.setHorizontalHeaderLabels(["Seed Filename", "Extension", "Full Template Path"])
        self.seed_files_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tf_layout.addWidget(self.seed_files_table)

        tf_btns = QHBoxLayout()
        imp_seed_btn = QPushButton("+ Import Seed File from Disk…")
        imp_seed_btn.setObjectName("PrimaryButton")
        imp_seed_btn.clicked.connect(self._import_seed_file)
        edit_seed_btn = QPushButton("Open Selected Seed File in App")
        edit_seed_btn.clicked.connect(self._open_selected_seed_file)
        mig_seed_btn = QPushButton("Migrate to Versioned Seed…")
        mig_seed_btn.clicked.connect(self._migrate_selected_legacy_seed)
        del_seed_btn = QPushButton("Delete Selected Seed File")
        del_seed_btn.clicked.connect(self._delete_selected_seed_file)
        tf_btns.addWidget(imp_seed_btn)
        tf_btns.addWidget(edit_seed_btn)
        tf_btns.addWidget(mig_seed_btn)
        tf_btns.addWidget(del_seed_btn)
        tf_btns.addStretch()
        tf_layout.addLayout(tf_btns)
        self.tabs.addTab(tab_files, "4. Legacy Seed Files")

        root_layout.addWidget(self.tabs)

        self.feedback_banner = InlineStatusBanner(self)
        root_layout.addWidget(self.feedback_banner)

        path_lbl = QLabel(f"Global Templates: {self.tm.templates_root}")
        path_lbl.setObjectName("MutedText")
        open_dir_btn = QPushButton("Open Templates Folder in Explorer")
        open_dir_btn.clicked.connect(self._open_templates_folder)
        close_dlg_btn = QPushButton("Done")
        close_dlg_btn.clicked.connect(self.accept)
        root_layout.addWidget(
            DialogFooter(
                self,
                left_widgets=[path_lbl, open_dir_btn],
                cancel_button=close_dlg_btn,
            )
        )

        self.seed_scope_combo.currentIndexChanged.connect(self._on_seed_scope_changed)
        self.seed_root_edit.editingFinished.connect(self._on_seed_root_edited)
        self.seed_software_list.currentItemChanged.connect(self._on_seed_software_changed)
        self.seed_stream_list.currentItemChanged.connect(self._on_seed_stream_changed)

        self._refresh_all_lists()
        if 0 <= initial_tab < self.tabs.count():
            self.tabs.setCurrentIndex(initial_tab)

    def _seed_scope(self) -> TemplateScope:
        return TemplateScope(str(self.seed_scope_combo.currentData() or TemplateScope.CLIENT_ROOT.value))

    def _seed_root_path(self) -> Path:
        txt = self.seed_root_edit.text().strip()
        if txt:
            return Path(txt).expanduser().resolve()
        return self._seed_client_root

    def _selected_seed_software(self) -> str:
        item = self.seed_software_list.currentItem()
        if item is not None:
            return str(item.data(Qt.ItemDataRole.UserRole) or "reaper")
        return "reaper"

    def _selected_seed_stream_name(self) -> str:
        item = self.seed_stream_list.currentItem()
        if item is not None:
            return str(item.data(Qt.ItemDataRole.UserRole) or "")
        return ""

    def _browse_seed_root(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Select Client Workspace Root", str(self._seed_root_path()))
        if folder:
            self._seed_client_root = Path(folder).resolve()
            self.seed_root_edit.setText(str(self._seed_client_root))
            self._refresh_seed_tab()

    def _on_seed_root_edited(self) -> None:
        self._seed_client_root = self._seed_root_path()
        self._refresh_seed_tab()

    def _on_seed_scope_changed(self) -> None:
        is_root = self._seed_scope() == TemplateScope.CLIENT_ROOT
        self.seed_root_lbl.setVisible(is_root)
        self.seed_root_edit.setVisible(is_root)
        self.seed_browse_root_btn.setVisible(is_root)
        self._refresh_seed_tab()

    def _refresh_seed_tab(self) -> None:
        scope = self._seed_scope()
        croot = self._seed_root_path()
        prev_sw = self._selected_seed_software()

        sw_keys = sorted(
            set(self.hub.config.software.keys())
            | set(self.tm.seeds.list_scope_software(scope, project_root=self.active_project_root, client_root=croot))
        )
        self.seed_software_list.blockSignals(True)
        self.seed_software_list.clear()
        target_row = 0
        for idx, sw_k in enumerate(sw_keys):
            prof = self.hub.get_software_profile(sw_k)
            man = self.tm.seeds.load_manifest(
                sw_k,
                scope,
                project_root=self.active_project_root,
                client_root=croot,
                preferred_extensions=prof.extensions,
            )
            if man.default_template and man.default_template in man.templates:
                stream = man.templates[man.default_template]
                vobj = stream.resolve_version()
                v_str = vobj.version_label if vobj else "—"
                badge = f"★ {man.default_template} {v_str}"
            elif man.templates:
                badge = f"{len(man.templates)} tpl (inherited)"
            else:
                badge = "↳ inherited"
            item = QListWidgetItem(f"{prof.display_name} [{sw_k}]\n  {badge}")
            item.setData(Qt.ItemDataRole.UserRole, sw_k)
            self.seed_software_list.addItem(item)
            if sw_k == prev_sw:
                target_row = idx
        self.seed_software_list.blockSignals(False)

        if self.seed_software_list.count() > 0:
            self.seed_software_list.setCurrentRow(target_row)
        self._refresh_seed_streams()

    def _on_seed_software_changed(self, _curr: Optional[QListWidgetItem], _prev: Optional[QListWidgetItem]) -> None:
        self._refresh_seed_streams()

    def _refresh_seed_streams(self) -> None:
        scope = self._seed_scope()
        croot = self._seed_root_path()
        sw = self._selected_seed_software()
        prof = self.hub.get_software_profile(sw)

        # Effective cascade banner
        proj_arg = self.active_project_root if scope == TemplateScope.PROJECT else None
        resolved_eff = self.tm.resolve_seed_template(
            software=sw,
            project_root=proj_arg,
            client_root=croot,
        )
        if resolved_eff is not None:
            self.seed_effective_banner.setText(
                f"Effective Default for {prof.display_name}:  {resolved_eff.display_label()}  →  {resolved_eff.primary_file}"
            )
        else:
            self.seed_effective_banner.setText(
                f"Effective Default for {prof.display_name}:  (None configured — falls back to TaskTemplate or blank file)"
            )

        prev_stream = self._selected_seed_stream_name()
        man = self.tm.seeds.load_manifest(
            sw,
            scope,
            project_root=self.active_project_root,
            client_root=croot,
            preferred_extensions=prof.extensions,
        )
        self.seed_stream_list.blockSignals(True)
        self.seed_stream_list.clear()
        target_row = 0
        for idx, tname in enumerate(sorted(man.templates.keys())):
            st = man.templates[tname]
            is_def = tname == man.default_template
            prefix = "★ " if is_def else "  "
            pin_info = f"pinned v{st.default_version:03d}" if st.default_version > 0 else "latest"
            item = QListWidgetItem(f"{prefix}{tname}  ({len(st.versions)}v, {pin_info})")
            item.setData(Qt.ItemDataRole.UserRole, tname)
            self.seed_stream_list.addItem(item)
            if tname == prev_stream:
                target_row = idx
        self.seed_stream_list.blockSignals(False)

        if self.seed_stream_list.count() > 0:
            self.seed_stream_list.setCurrentRow(target_row)
        self._refresh_seed_versions()

    def _on_seed_stream_changed(self, _curr: Optional[QListWidgetItem], _prev: Optional[QListWidgetItem]) -> None:
        self._refresh_seed_versions()

    def _refresh_seed_versions(self) -> None:
        scope = self._seed_scope()
        croot = self._seed_root_path()
        sw = self._selected_seed_software()
        tname = self._selected_seed_stream_name()
        prof = self.hub.get_software_profile(sw)

        self.seed_default_ver_combo.blockSignals(True)
        self.seed_default_ver_combo.clear()
        self.seed_versions_table.setRowCount(0)

        if not tname:
            self.seed_default_ver_combo.addItem("Always Latest (Auto)", 0)
            self.seed_default_ver_combo.blockSignals(False)
            return

        man = self.tm.seeds.load_manifest(
            sw,
            scope,
            project_root=self.active_project_root,
            client_root=croot,
            preferred_extensions=prof.extensions,
        )
        stream = man.templates.get(tname)
        if stream is None:
            self.seed_default_ver_combo.addItem("Always Latest (Auto)", 0)
            self.seed_default_ver_combo.blockSignals(False)
            return

        sw_dir = self.tm.seeds.get_seeds_dir(scope, project_root=self.active_project_root, client_root=croot) / sw
        self.seed_default_ver_combo.addItem("Always Latest (Auto)", 0)
        vers_desc = sorted(stream.versions, key=lambda v: v.version, reverse=True)
        for v in vers_desc:
            self.seed_default_ver_combo.addItem(f"Pin {v.version_label}", v.version)
        def_idx = self.seed_default_ver_combo.findData(stream.default_version)
        self.seed_default_ver_combo.setCurrentIndex(def_idx if def_idx >= 0 else 0)
        self.seed_default_ver_combo.blockSignals(False)

        resolved_ver = stream.resolve_version()
        self.seed_versions_table.setRowCount(len(vers_desc))
        for row_idx, v in enumerate(vers_desc):
            p_abs = sw_dir / v.primary_file
            is_ro = p_abs.is_file() and not (p_abs.stat().st_mode & 0o200)
            ver_item = QTableWidgetItem(v.version_label)
            if is_ro:
                ver_item.setIcon(get_icon("lock", TEXT_MUTED, ICON_SM))
            ver_item.setData(Qt.ItemDataRole.UserRole, v.version)

            file_item = QTableWidgetItem(Path(v.primary_file).name)
            file_item.setData(Qt.ItemDataRole.UserRole, str(p_abs))

            if resolved_ver and v.version == resolved_ver.version:
                def_txt = "★ Pinned" if stream.default_version > 0 else "★ Latest"
            else:
                def_txt = ""
            def_item = QTableWidgetItem(def_txt)
            created_item = QTableWidgetItem(v.created_at[:19].replace("T", " "))
            note_item = QTableWidgetItem(v.comment)

            self.seed_versions_table.setItem(row_idx, 0, ver_item)
            self.seed_versions_table.setItem(row_idx, 1, file_item)
            self.seed_versions_table.setItem(row_idx, 2, def_item)
            self.seed_versions_table.setItem(row_idx, 3, created_item)
            self.seed_versions_table.setItem(row_idx, 4, note_item)

    def _selected_seed_version_num(self) -> Optional[int]:
        row = self.seed_versions_table.currentRow()
        if row < 0 and self.seed_versions_table.rowCount() > 0:
            row = 0
        if row < 0:
            return None
        item = self.seed_versions_table.item(row, 0)
        if item is None:
            return None
        val = item.data(Qt.ItemDataRole.UserRole)
        return int(val) if val is not None else None

    def _seed_import_new(self) -> None:
        sw = self._selected_seed_software()
        scope = self._seed_scope()
        croot = self._seed_root_path()
        prof = self.hub.get_software_profile(sw)
        file_str, _ = QFileDialog.getOpenFileName(
            self,
            f"Select Starter Template File for {prof.display_name}",
        )
        if not file_str:
            return
        dlg = SaveSeedTemplateDialog(
            self,
            self.hub,
            source_file=Path(file_str),
            software_key=sw,
            project_root=self.active_project_root,
            client_root=croot,
        )
        scope_idx = dlg.scope_combo.findData(scope.value)
        if scope_idx >= 0:
            dlg.scope_combo.setCurrentIndex(scope_idx)
        if dlg.exec() == SaveSeedTemplateDialog.DialogCode.Accepted:
            self._refresh_seed_tab()

    def _seed_add_version_to_stream(self) -> None:
        sw = self._selected_seed_software()
        tname = self._selected_seed_stream_name()
        if not tname:
            self._seed_import_new()
            return
        scope = self._seed_scope()
        croot = self._seed_root_path()
        prof = self.hub.get_software_profile(sw)
        file_str, _ = QFileDialog.getOpenFileName(
            self,
            f"Select File for New Version of '{tname}' ({prof.display_name})",
        )
        if not file_str:
            return
        note, ok = self._QInputDialog.getText(self, "Add Template Version", "Version note (optional):")
        if not ok:
            return
        self.tm.seeds.add_version(
            software=sw,
            template_name=tname,
            source_path=Path(file_str),
            scope=scope,
            comment=note.strip(),
            author=self.hub.config.author_name,
            project_root=self.active_project_root,
            client_root=croot,
            ignore_patterns=prof.ignore_patterns,
            lock_previous=prof.lock_previous_versions,
            preferred_extensions=prof.extensions,
        )
        self._refresh_seed_tab()

    def _seed_version_up(self, open_in_app: bool = True) -> None:
        sw = self._selected_seed_software()
        tname = self._selected_seed_stream_name()
        if not tname:
            return
        scope = self._seed_scope()
        croot = self._seed_root_path()
        prof = self.hub.get_software_profile(sw)
        src_ver = self._selected_seed_version_num()
        note, ok = self._QInputDialog.getText(self, "Version Up Template", "New version note (optional):")
        if not ok:
            return
        _, new_ver = self.tm.seeds.version_up_template(
            software=sw,
            template_name=tname,
            scope=scope,
            source_version=src_ver,
            comment=note.strip(),
            author=self.hub.config.author_name,
            project_root=self.active_project_root,
            client_root=croot,
            ignore_patterns=prof.ignore_patterns,
            lock_previous=prof.lock_previous_versions,
            preferred_extensions=prof.extensions,
        )
        self._refresh_seed_tab()
        if open_in_app:
            sw_dir = self.tm.seeds.get_seeds_dir(scope, project_root=self.active_project_root, client_root=croot) / sw
            p_abs = sw_dir / new_ver.primary_file
            if p_abs.is_file():
                from obol.plugins.base import launch_file_with_profile

                launch_file_with_profile(p_abs, prof)

    def _seed_open_selected_version(self) -> None:
        row = self.seed_versions_table.currentRow()
        if row < 0 and self.seed_versions_table.rowCount() > 0:
            row = 0
        if row < 0:
            return
        file_item = self.seed_versions_table.item(row, 1)
        if file_item is None:
            return
        p_abs = Path(str(file_item.data(Qt.ItemDataRole.UserRole) or ""))
        if p_abs.is_file():
            from obol.plugins.base import launch_file_with_profile

            prof = self.hub.get_software_profile(self._selected_seed_software())
            launch_file_with_profile(p_abs, prof)

    def _seed_edit_version_note(self) -> None:
        sw = self._selected_seed_software()
        tname = self._selected_seed_stream_name()
        ver = self._selected_seed_version_num()
        if not tname or ver is None:
            return
        scope = self._seed_scope()
        croot = self._seed_root_path()
        row = self.seed_versions_table.currentRow()
        curr_note = self.seed_versions_table.item(row, 4).text() if row >= 0 and self.seed_versions_table.item(row, 4) else ""
        new_note, ok = self._QInputDialog.getText(self, f"Edit Note for v{ver:03d}", "Version note:", text=curr_note)
        if ok:
            self.tm.seeds.update_metadata(
                sw,
                tname,
                scope,
                version=ver,
                comment=new_note.strip(),
                project_root=self.active_project_root,
                client_root=croot,
            )
            self._refresh_seed_versions()

    def _seed_set_default_stream(self) -> None:
        sw = self._selected_seed_software()
        tname = self._selected_seed_stream_name()
        if not tname:
            return
        self.tm.seeds.set_default_template(
            sw,
            tname,
            self._seed_scope(),
            project_root=self.active_project_root,
            client_root=self._seed_root_path(),
        )
        self._refresh_seed_tab()

    def _seed_clear_default_stream(self) -> None:
        sw = self._selected_seed_software()
        self.tm.seeds.set_default_template(
            sw,
            "",
            self._seed_scope(),
            project_root=self.active_project_root,
            client_root=self._seed_root_path(),
        )
        self._refresh_seed_tab()

    def _seed_apply_default_version(self, _idx: int = 0) -> None:
        sw = self._selected_seed_software()
        tname = self._selected_seed_stream_name()
        if not tname:
            return
        ver_val = int(self.seed_default_ver_combo.currentData() or 0)
        self.tm.seeds.set_default_version(
            sw,
            tname,
            ver_val,
            self._seed_scope(),
            project_root=self.active_project_root,
            client_root=self._seed_root_path(),
        )
        self._refresh_seed_tab()

    def _seed_delete_version(self, confirm: bool = True) -> None:
        sw = self._selected_seed_software()
        tname = self._selected_seed_stream_name()
        ver = self._selected_seed_version_num()
        if not tname or ver is None:
            return
        if confirm and not confirm_destructive_action(
            self,
            "Delete Seed Version",
            f"Delete seed template '{tname}' v{ver:03d} ({sw})?",
            confirm_label=f"Delete v{ver:03d}",
            informative_text="This cannot be undone.",
        ):
            return
        self.tm.seeds.delete_version(
            sw,
            tname,
            ver,
            self._seed_scope(),
            project_root=self.active_project_root,
            client_root=self._seed_root_path(),
        )
        self._refresh_seed_tab()
        self.feedback_banner.set_feedback(f"Deleted seed '{tname}' v{ver:03d}", "info")

    def _seed_delete_stream(self, confirm: bool = True) -> None:
        sw = self._selected_seed_software()
        tname = self._selected_seed_stream_name()
        if not tname:
            return
        if confirm and not confirm_destructive_action(
            self,
            "Delete Seed Template",
            f"Delete named seed template '{tname}' ({sw}) and all its versions?",
            confirm_label="Delete Template",
            informative_text="This cannot be undone.",
        ):
            return
        self.tm.seeds.delete_template(
            sw,
            tname,
            self._seed_scope(),
            project_root=self.active_project_root,
            client_root=self._seed_root_path(),
        )
        self._refresh_seed_tab()
        self.feedback_banner.set_feedback(f"Deleted seed template '{tname}'", "info")

    def _seed_open_scope_folder(self) -> None:
        seeds_dir = self.tm.seeds.get_seeds_dir(
            self._seed_scope(),
            project_root=self.active_project_root,
            client_root=self._seed_root_path(),
        )
        seeds_dir.mkdir(parents=True, exist_ok=True)
        open_folder_in_os(seeds_dir)

    def _migrate_selected_legacy_seed(self) -> None:
        row = self.seed_files_table.currentRow()
        if row < 0:
            return
        path_item = self.seed_files_table.item(row, 2)
        if not path_item:
            return
        fpath = Path(path_item.text())
        if not fpath.is_file():
            return
        prof = self.hub.detect_software_for_extension(fpath.suffix)
        dlg = SaveSeedTemplateDialog(
            self,
            self.hub,
            source_file=fpath,
            software_key=prof.key,
            project_root=self.active_project_root,
            client_root=self._seed_root_path(),
            default_comment=f"Migrated from legacy {fpath.name}",
            default_template_name="default",
        )
        if dlg.exec() == SaveSeedTemplateDialog.DialogCode.Accepted:
            self._refresh_seed_tab()
            self.tabs.setCurrentIndex(2)

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

        self._on_seed_scope_changed()

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
        matches = self.proj_tpl_list.findItems(name, Qt.MatchFlag.MatchExactly)
        if matches:
            self.proj_tpl_list.setCurrentItem(matches[0])
        self.feedback_banner.set_feedback(f"✓ Saved project template '{name}'", "ok")

    def _capture_active_project(self) -> None:
        if not self.active_project_root or not self.active_project_root.is_dir():
            self.feedback_banner.set_feedback("Open a project first to capture its hierarchy as a template.", "warn")
            return
        name = self.pt_name_edit.text().strip() or f"{self.active_project_root.name}_tpl"
        self.tm.capture_template("project", self.active_project_root, name)
        self._refresh_all_lists()
        self.feedback_banner.set_feedback(f"✓ Captured project template '{name}'", "ok")

    def _delete_project_template(self, confirm: bool = True) -> None:
        name = self.pt_name_edit.text().strip()
        if not name:
            return
        if confirm and not confirm_destructive_action(
            self,
            "Delete Project Template",
            f"Delete project template '{name}'?",
            confirm_label="Delete Template",
            informative_text="This cannot be undone.",
        ):
            return
        self.tm.delete_project_template(name)
        self._refresh_all_lists()
        self.feedback_banner.set_feedback(f"Deleted project template '{name}'", "info")

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
        matches = self.task_tpl_list.findItems(name, Qt.MatchFlag.MatchExactly)
        if matches:
            self.task_tpl_list.setCurrentItem(matches[0])
        self.feedback_banner.set_feedback(f"✓ Saved component template '{name}'", "ok")

    def _capture_active_component(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            self.feedback_banner.set_feedback("Select a component in the main window first.", "warn")
            return
        comp_dir = self.active_project_root / self.active_component_path
        name = self.tt_name_edit.text().strip() or f"{comp_dir.name}_task"
        self.tm.capture_template("task", comp_dir, name)
        self._refresh_all_lists()
        self.feedback_banner.set_feedback(f"✓ Captured component template '{name}'", "ok")

    def _delete_task_template(self, confirm: bool = True) -> None:
        name = self.tt_name_edit.text().strip()
        if not name:
            return
        if confirm and not confirm_destructive_action(
            self,
            "Delete Component Template",
            f"Delete component template '{name}'?",
            confirm_label="Delete Template",
            informative_text="This cannot be undone.",
        ):
            return
        self.tm.delete_task_template(name)
        self._refresh_all_lists()
        self.feedback_banner.set_feedback(f"Deleted component template '{name}'", "info")

    def _import_seed_file(self) -> None:
        file_str, _ = QFileDialog.getOpenFileName(self, "Select Starter Working File (.rpp, .blend, .kra, .psd, etc.)")
        if file_str:
            self.tm.import_seed_file(Path(file_str))
            self._refresh_all_lists()
            self.feedback_banner.set_feedback(f"✓ Imported seed file '{Path(file_str).name}'", "ok")

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

    def _delete_selected_seed_file(self, confirm: bool = True) -> None:
        row = self.seed_files_table.currentRow()
        if row < 0:
            return
        name_item = self.seed_files_table.item(row, 0)
        if not name_item:
            return
        fname = name_item.text()
        if confirm and not confirm_destructive_action(
            self,
            "Delete Seed File",
            f"Delete legacy seed file '{fname}'?",
            confirm_label="Delete Seed File",
            informative_text="This cannot be undone.",
        ):
            return
        self.tm.delete_seed_file(fname)
        self._refresh_all_lists()
        self.feedback_banner.set_feedback(f"Deleted seed file '{fname}'", "info")

    def _open_templates_folder(self) -> None:
        open_folder_in_os(self.tm.templates_root)


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
        self.copy_mode_combo.addItem("Copy (keep original)", "copy")
        self.copy_mode_combo.addItem("Move (remove original)", "move")
        self.comment_edit = QLineEdit(f"Ingested {self.source_paths[0].name if len(self.source_paths) == 1 else f'{len(self.source_paths)} items'}")
        self.tags_edit = QLineEdit()
        self.tags_edit.setPlaceholderText("comma-separated tags (optional)")

        form.addRow("", self.rename_chk)
        form.addRow("Transfer Mode:", self.copy_mode_combo)
        form.addRow("Version Note:", self.comment_edit)
        form.addRow("Tags:", self.tags_edit)
        layout.addLayout(form)

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        ok_btn = QPushButton(f"Ingest as working/v{next_version_num:03d}/")
        ok_btn.setObjectName("PrimaryButton")
        ok_btn.setDefault(True)
        ok_btn.clicked.connect(self.accept)
        layout.addWidget(DialogFooter(self, cancel_button=cancel_btn, primary_button=ok_btn))

    def get_values(self) -> dict[str, object]:
        tags = [t.strip() for t in self.tags_edit.text().split(",") if t.strip()]
        return {
            "rename_to_convention": self.rename_chk.isChecked(),
            "copy_mode": str(self.copy_mode_combo.currentData() or "copy"),
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
        self.staging_table.verticalHeader().setVisible(False)
        self.staging_table.setAlternatingRowColors(True)
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
        add_files_btn = QPushButton("+ Add File(s)…")
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

        cancel_btn = QPushButton("Cancel")
        cancel_btn.clicked.connect(self.reject)
        self.publish_btn = QPushButton("Publish Deliverables")
        self.publish_btn.setObjectName("PrimaryButton")
        self.publish_btn.setDefault(True)
        self.publish_btn.clicked.connect(self._validate_and_accept)
        layout.addWidget(DialogFooter(self, cancel_button=cancel_btn, primary_button=self.publish_btn))

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


