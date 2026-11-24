"""Main 3-Pane PySide6 Desktop GUI for Obol."""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtGui import QColor, QFont, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QCompleter,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QSizePolicy,
    QSplitter,
    QStackedWidget,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from obol.core.hub import ObolHub
from obol.core.models import ComponentSnapshot, VaultStatus
from obol.core.storage import Reconciler, discover_project_components, load_component_snapshot, save_component_snapshot
from obol.core.sync import SyncEngine
from obol.core.workspace import ObolWorkspace
from obol.gui.dialogs import (
    AddComponentDialog,
    CreditsReportDialog,
    IngestWorkingDialog,
    NewProjectWizardDialog,
    ProjectAndVaultManagerDialog,
    PublishDeliverableDialog,
    SmartIngestDialog,
    SoftwareSettingsDialog,
    TemplateManagerDialog,
)
from obol.gui.drop_widgets import DropTreeWidget, install_tab_hover_switch
from obol.gui.integrations_dialog import IntegrationsManagerDialog
from obol.gui.reaper_dialogs import ReaperExportPublishDialog
from obol.gui.resolve_dialogs import ResolveExportPublishDialog
from obol.gui.theme import apply_studio_dark_theme
from obol.plugins.integrations import DCCIntegrationManager
from obol.plugins.reaper_bridge import ReaperBridgeClient
from obol.plugins.reaper_installer import ReaperInstaller
from obol.plugins.resolve_bridge import ResolveBridgeClient
from obol.plugins.resolve_installer import ResolveInstaller


def _format_timestamp(iso_str: str) -> str:
    if not iso_str:
        return "—"
    return iso_str.replace("T", " ")[:16]


def _format_size(size_bytes: int) -> str:
    if size_bytes >= 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes} B"


def _repolish(widget: QWidget) -> None:
    style = widget.style()
    if style is not None:
        style.unpolish(widget)
        style.polish(widget)
    widget.update()


def _open_folder_in_os(folder: Path) -> None:
    if not folder.exists():
        return
    if sys.platform == "win32" and hasattr(os, "startfile"):
        os.startfile(str(folder))


class ObolMainWindow(QMainWindow):
    """3-Pane Desktop GUI: (1) Component Hierarchy Tree, (2) Tabbed Triad Workspace, (3) Context-Aware Inspector."""

    def __init__(
        self,
        hub: Optional[ObolHub] = None,
        initial_project: str = "",
        initial_component: str = "",
    ) -> None:
        super().__init__()
        self.hub = hub or ObolHub()
        self.workspace = ObolWorkspace(self.hub)
        self.sync = SyncEngine(self.hub)
        self.reaper_bridge = ReaperBridgeClient(self.hub)
        self.reaper_installer = ReaperInstaller(self.hub)
        allow_studio = QApplication.platformName() != "offscreen"
        self.resolve_bridge = ResolveBridgeClient(
            self.hub,
            allow_studio_api=allow_studio,
        )
        self.resolve_installer = ResolveInstaller(self.hub)
        self.integration_manager = DCCIntegrationManager(self.hub, allow_resolve_studio_api=allow_studio)

        self.active_project_name: str = initial_project or self.hub.config.last_active_project
        self.active_project_root: Optional[Path] = None
        self.active_component_path: str = initial_component
        self.active_snapshot: Optional[ComponentSnapshot] = None
        self.selected_repo_asset_path: str = ""
        self.selected_deliverable_path: str = ""
        self._selected_group_prefix: str = ""

        self.setWindowTitle("Obol — Personal Media Production Pipeline")
        self.resize(1240, 760)
        apply_studio_dark_theme(self)

        central = QWidget()
        self.setCentralWidget(central)
        root_layout = QVBoxLayout(central)
        root_layout.setContentsMargins(0, 0, 0, 0)
        root_layout.setSpacing(0)

        # ---------------- Streamlined Top Header Bar ----------------
        top_bar = QFrame()
        top_bar.setObjectName("TopBar")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 8, 12, 8)
        top_layout.setSpacing(8)

        brand = QLabel("OBOL")
        brand.setObjectName("BrandBadge")
        top_layout.addWidget(brand)

        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(200)
        self.project_combo.setEditable(True)
        self.project_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        self.project_combo.lineEdit().setPlaceholderText("Search projects…")
        completer = self.project_combo.completer()
        completer.setFilterMode(Qt.MatchFlag.MatchContains)
        completer.setCaseSensitivity(Qt.CaseSensitivity.CaseInsensitive)
        completer.setCompletionMode(QCompleter.CompletionMode.PopupCompletion)
        self.project_combo.setCompleter(completer)
        self.project_combo.textActivated.connect(self._on_project_combo_changed)
        self.project_combo.lineEdit().editingFinished.connect(self._restore_project_text)
        top_layout.addWidget(self.project_combo)

        new_proj_top_btn = QPushButton("+ New Project")
        new_proj_top_btn.setObjectName("PrimaryButton")
        new_proj_top_btn.setToolTip("Create a new Obol project (Ctrl+N)")
        new_proj_top_btn.clicked.connect(self.open_new_project_wizard)
        top_layout.addWidget(new_proj_top_btn)

        proj_mgr_btn = QPushButton("Projects && Vault...")
        proj_mgr_btn.setToolTip("Manage Client Root, switch/register projects, and connect an optional Server Vault")
        proj_mgr_btn.clicked.connect(self.open_projects_and_vault_dialog)
        top_layout.addWidget(proj_mgr_btn)

        top_layout.addStretch(1)

        self.fast_drop_btn = QPushButton()
        self.fast_drop_btn.setToolTip(
            "When OFF: dropping files opens naming & metadata dialogs.\n"
            "When ON: dropped files ingest or publish immediately with auto-routed defaults."
        )
        self.fast_drop_btn.clicked.connect(self._toggle_fast_drop)
        self._update_fast_drop_button()
        top_layout.addWidget(self.fast_drop_btn)

        refresh_btn = QPushButton("↻ Refresh")
        refresh_btn.setToolTip("Scan disk for external file changes and reload view (F5)")
        refresh_btn.clicked.connect(self._action_refresh_and_reconcile)
        top_layout.addWidget(refresh_btn)

        self.cred_btn = QPushButton("Credits")
        self.cred_btn.setToolTip("Open Project Attribution & License Audit")
        self.cred_btn.clicked.connect(self.open_credits_dialog)
        top_layout.addWidget(self.cred_btn)

        settings_btn = QPushButton("⚙ Settings ▾")
        settings_btn.setToolTip("Configure 3-Tier Templates, Software Profiles, and Client Workspace Root")
        settings_menu = QMenu(settings_btn)
        act_tpl = settings_menu.addAction("3-Tier Templates...")
        act_tpl.triggered.connect(self.open_template_manager_dialog)
        act_sw = settings_menu.addAction("Software Profiles...")
        act_sw.triggered.connect(self.open_software_settings_dialog)
        act_dcc = settings_menu.addAction("DCC Integrations...")
        act_dcc.triggered.connect(lambda: self.open_integrations_manager_dialog(""))
        act_root = settings_menu.addAction("Change Client Workspace Root...")
        act_root.triggered.connect(self._prompt_change_client_root)
        settings_menu.addSeparator()
        act_hub = settings_menu.addAction("Open Global Hub Folder (~/.obol)")
        act_hub.triggered.connect(lambda: _open_folder_in_os(self.hub.hub_dir))
        settings_btn.setMenu(settings_menu)
        top_layout.addWidget(settings_btn)

        root_layout.addWidget(top_bar)

        # ---------------- Central Stack: Welcome Empty State vs 3-Pane Workspace ----------------
        self.central_stack = QStackedWidget()
        self.central_stack.addWidget(self._build_welcome_page())

        main_splitter = QSplitter(Qt.Orientation.Horizontal)
        main_splitter.setContentsMargins(8, 8, 8, 6)

        # Pane 1: Arbitrary-Depth Component Hierarchy Tree + Filter
        pane1 = QFrame()
        pane1.setObjectName("PanelCard")
        p1_layout = QVBoxLayout(pane1)
        p1_layout.setSpacing(6)
        p1_header = QHBoxLayout()
        p1_title = QLabel("Components")
        p1_title.setObjectName("SectionHeader")
        add_comp_btn = QPushButton("+ Add")
        add_comp_btn.setObjectName("SubtleToolButton")
        add_comp_btn.setToolTip("Add a new Triad Component (Ctrl+Shift+N)")
        add_comp_btn.clicked.connect(self._prompt_add_component)
        p1_header.addWidget(p1_title)
        p1_header.addStretch()
        p1_header.addWidget(add_comp_btn)
        p1_layout.addLayout(p1_header)

        self.comp_filter_edit = QLineEdit()
        self.comp_filter_edit.setObjectName("FilterInput")
        self.comp_filter_edit.setPlaceholderText("Filter components…")
        self.comp_filter_edit.setClearButtonEnabled(True)
        self.comp_filter_edit.textChanged.connect(self._filter_component_tree)
        p1_layout.addWidget(self.comp_filter_edit)

        self.component_tree = DropTreeWidget()
        self.component_tree.setAcceptDrops(False)
        self.component_tree.setHeaderLabels(["Component / Group", "Software"])
        self.component_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.component_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.component_tree.set_empty_placeholder("No components yet", "Click '+ Add' to create a Triad component")
        self.component_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.component_tree.customContextMenuRequested.connect(self._ctx_component_tree)
        self.component_tree.currentItemChanged.connect(self._on_component_tree_selected)
        p1_layout.addWidget(self.component_tree, 1)

        self.vault_status_label = QLabel("")
        self.vault_status_label.setObjectName("MutedText")
        self.vault_status_label.setWordWrap(True)
        p1_layout.addWidget(self.vault_status_label)
        main_splitter.addWidget(pane1)

        # Pane 2: Center Tabbed Triad Workspace (Working | Repo Assets | Published)
        pane2 = QFrame()
        pane2.setObjectName("PanelCard")
        p2_layout = QVBoxLayout(pane2)
        self.triad_tabs = QTabWidget()
        install_tab_hover_switch(self.triad_tabs)

        def _make_drop_hint(text: str) -> QLabel:
            lbl = QLabel(text)
            lbl.setObjectName("DropHint")
            lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
            return lbl

        # Tab 0: Working
        working_tab = QWidget()
        w_layout = QVBoxLayout(working_tab)
        w_layout.setSpacing(6)
        w_top = QHBoxLayout()
        w_lbl = QLabel("Working Versions")
        w_lbl.setObjectName("SectionHeader")
        w_top.addWidget(w_lbl)
        w_top.addStretch()
        ing_work_btn = QPushButton("+ Ingest File...")
        ing_work_btn.setToolTip("Ingest an external file or folder bundle as the next working version (Ctrl+I)")
        ing_work_btn.clicked.connect(self._action_ingest_working_file)
        w_top.addWidget(ing_work_btn)
        vup_btn = QPushButton("+ Version Up (vUp)")
        vup_btn.setObjectName("PrimaryButton")
        vup_btn.setToolTip("Create next working/vNNN/ version using the component's Version-Up Strategy (Ctrl+U)")
        vup_btn.clicked.connect(self._action_version_up_working)
        w_top.addWidget(vup_btn)
        w_layout.addLayout(w_top)
        w_layout.addWidget(_make_drop_hint("Double-click a version to open • Drop files or folders here to ingest as next vNNN"))
        self.working_list = DropTreeWidget()
        self.working_list.setRootIsDecorated(False)
        self.working_list.setAlternatingRowColors(True)
        self.working_list.setHeaderLabels(["Version", "Primary File", "Created", "Note"])
        self.working_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.working_list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.working_list.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.working_list.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.working_list.setColumnWidth(1, 180)
        self.working_list.set_empty_placeholder(
            "No working versions yet",
            "Click '+ Version Up (vUp)' or drop project files/folders here",
        )
        self.working_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.working_list.customContextMenuRequested.connect(self._ctx_working_list)
        self.working_list.currentItemChanged.connect(self._on_working_version_selected)
        self.working_list.itemDoubleClicked.connect(self._on_working_item_double_clicked)
        self.working_list.filesDropped.connect(self._handle_working_drop)
        self.working_list.enable_file_drag_out(self._drag_path_for_working_item)
        w_layout.addWidget(self.working_list, 1)
        self.triad_tabs.addTab(working_tab, "Working (0)")

        # Tab 1: Repo Assets (Master–Detail)
        repo_tab = QWidget()
        r_layout = QVBoxLayout(repo_tab)
        r_layout.setSpacing(6)
        r_layout.addWidget(_make_drop_hint("Drop on empty space/category for a new asset, or onto an asset/version list for v(N+1)"))
        r_splitter = QSplitter(Qt.Orientation.Horizontal)

        r_left = QWidget()
        rl_layout = QVBoxLayout(r_left)
        rl_layout.setContentsMargins(0, 0, 4, 0)
        rl_top = QHBoxLayout()
        rl_lbl = QLabel("Repo Assets")
        rl_lbl.setObjectName("SectionHeader")
        new_asset_btn = QPushButton("+ Slot")
        new_asset_btn.setObjectName("SubtleToolButton")
        new_asset_btn.setToolTip("Create an empty named Repo Asset slot before dropping files into it")
        new_asset_btn.clicked.connect(self._action_new_repo_asset)
        ing_new_btn = QPushButton("+ Ingest...")
        ing_new_btn.setObjectName("PrimaryButton")
        ing_new_btn.setToolTip("Ingest file(s) into repo/ with provenance & credits")
        ing_new_btn.clicked.connect(self._action_ingest_repo_file)
        rl_top.addWidget(rl_lbl)
        rl_top.addStretch()
        rl_top.addWidget(new_asset_btn)
        rl_top.addWidget(ing_new_btn)
        rl_layout.addLayout(rl_top)

        self.repo_asset_tree = DropTreeWidget()
        self.repo_asset_tree.setHeaderLabels(["Asset / Category", "Ver", "Used"])
        self.repo_asset_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.repo_asset_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.repo_asset_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.repo_asset_tree.set_empty_placeholder(
            "No repo assets yet",
            "Drop audio, MIDI, textures, or reference files here",
        )
        self.repo_asset_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.repo_asset_tree.customContextMenuRequested.connect(self._ctx_repo_asset_tree)
        self.repo_asset_tree.currentItemChanged.connect(self._on_repo_asset_selected)
        self.repo_asset_tree.filesDropped.connect(self._handle_repo_tree_drop)
        rl_layout.addWidget(self.repo_asset_tree, 1)

        r_right = QWidget()
        rr_layout = QVBoxLayout(r_right)
        rr_layout.setContentsMargins(4, 0, 0, 0)
        rr_top = QHBoxLayout()
        self.repo_ver_header = QLabel("Versions in repo/")
        self.repo_ver_header.setObjectName("SectionHeader")
        self.repo_ver_header.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        ing_ver_btn = QPushButton("+ New Version...")
        ing_ver_btn.setToolTip("Ingest next version v(N+1) into the selected Repo Asset")
        ing_ver_btn.clicked.connect(self._action_ingest_repo_version)
        rr_top.addWidget(self.repo_ver_header, 1)
        rr_top.addWidget(ing_ver_btn)
        rr_layout.addLayout(rr_top)

        self.repo_version_list = DropTreeWidget()
        self.repo_version_list.setRootIsDecorated(False)
        self.repo_version_list.setAlternatingRowColors(True)
        self.repo_version_list.setHeaderLabels(["Version", "File", "Size", "Note"])
        self.repo_version_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.repo_version_list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.repo_version_list.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.repo_version_list.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.repo_version_list.setColumnWidth(1, 160)
        self.repo_version_list.set_empty_placeholder(
            "No asset versions",
            "Select an asset on the left or drop a file here for v(N+1)",
        )
        self.repo_version_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.repo_version_list.customContextMenuRequested.connect(self._ctx_repo_version_list)
        self.repo_version_list.currentItemChanged.connect(self._on_repo_version_selected)
        self.repo_version_list.itemDoubleClicked.connect(self._on_repo_version_double_clicked)
        self.repo_version_list.filesDropped.connect(self._handle_repo_version_drop)
        self.repo_version_list.enable_file_drag_out(self._drag_path_for_repo_item)
        rr_layout.addWidget(self.repo_version_list, 1)

        r_splitter.addWidget(r_left)
        r_splitter.addWidget(r_right)
        r_splitter.setSizes([240, 360])
        r_layout.addWidget(r_splitter)
        self.triad_tabs.addTab(repo_tab, "Repo Assets (0)")

        # Tab 2: Published
        published_tab = QWidget()
        p_layout = QVBoxLayout(published_tab)
        p_layout.setSpacing(6)
        p_layout.addWidget(_make_drop_hint("Drop files to publish, or onto an existing vNNN row to attach more formats • Double-click a file to open"))
        p_splitter = QSplitter(Qt.Orientation.Horizontal)

        p_left = QWidget()
        pl_layout = QVBoxLayout(p_left)
        pl_layout.setContentsMargins(0, 0, 4, 0)
        pl_top = QHBoxLayout()
        pl_lbl = QLabel("Deliverables")
        pl_lbl.setObjectName("SectionHeader")
        new_deliv_btn = QPushButton("+ Slot")
        new_deliv_btn.setObjectName("SubtleToolButton")
        new_deliv_btn.setToolTip("Create a named Deliverable stream before publishing into it")
        new_deliv_btn.clicked.connect(self._action_new_deliverable)
        pub_btn = QPushButton("+ Publish...")
        pub_btn.setObjectName("PrimaryButton")
        pub_btn.setToolTip("Stage & publish deliverable file(s) (Ctrl+P)")
        pub_btn.clicked.connect(self._action_publish_deliverable)
        pl_top.addWidget(pl_lbl)
        pl_top.addStretch()
        pl_top.addWidget(new_deliv_btn)
        pl_top.addWidget(pub_btn)
        pl_layout.addLayout(pl_top)

        self.published_deliverable_tree = DropTreeWidget()
        self.published_deliverable_tree.setHeaderLabels(["Deliverable", "Releases"])
        self.published_deliverable_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.published_deliverable_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.published_deliverable_tree.set_empty_placeholder(
            "No deliverables yet",
            "Drop exports (.wav, .mp4, .png) here to publish",
        )
        self.published_deliverable_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.published_deliverable_tree.customContextMenuRequested.connect(self._ctx_published_deliverable_tree)
        self.published_deliverable_tree.currentItemChanged.connect(self._on_published_deliverable_selected)
        self.published_deliverable_tree.filesDropped.connect(self._handle_published_deliverable_drop)
        pl_layout.addWidget(self.published_deliverable_tree, 1)

        p_right = QWidget()
        pr_layout = QVBoxLayout(p_right)
        pr_layout.setContentsMargins(4, 0, 0, 0)
        pr_top = QHBoxLayout()
        self.pub_ver_header = QLabel("Releases in published/")
        self.pub_ver_header.setObjectName("SectionHeader")
        self.pub_ver_header.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        export_dir_btn = QPushButton("1. Export Folder")
        export_dir_btn.setToolTip(
            "Create & open the next published/<deliverable>/vNNN/ folder so your DAW/DCC can render directly into it"
        )
        export_dir_btn.clicked.connect(self._action_open_publish_export_folder)
        sync_pub_btn = QPushButton("2. Sync Exports")
        sync_pub_btn.setToolTip("Scan & register files rendered directly into published/")
        sync_pub_btn.clicked.connect(self._action_sync_published_folder)
        self.reaper_export_btn = QPushButton("Export from REAPER...")
        self.reaper_export_btn.setObjectName("PrimaryButton")
        self.reaper_export_btn.setToolTip("Render directly from the running REAPER session and auto-publish")
        self.reaper_export_btn.clicked.connect(self._action_export_from_reaper)
        self.resolve_export_btn = QPushButton("Export from Resolve...")
        self.resolve_export_btn.setObjectName("PrimaryButton")
        self.resolve_export_btn.setToolTip("Render or export from DaVinci Resolve and auto-publish")
        self.resolve_export_btn.clicked.connect(self._action_export_from_resolve)
        pr_top.addWidget(self.pub_ver_header, 1)
        pr_top.addWidget(self.reaper_export_btn)
        pr_top.addWidget(self.resolve_export_btn)
        pr_top.addWidget(export_dir_btn)
        pr_top.addWidget(sync_pub_btn)
        pr_layout.addLayout(pr_top)

        self.published_version_tree = DropTreeWidget()
        self.published_version_tree.setAlternatingRowColors(True)
        self.published_version_tree.setHeaderLabels(["Version / File", "Types", "Source / Size", "Note"])
        self.published_version_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.published_version_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.published_version_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.published_version_tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.published_version_tree.setColumnWidth(0, 145)
        self.published_version_tree.set_empty_placeholder(
            "No releases published yet",
            "Click '+ Publish...' or drop exported files here (or onto an existing vNNN row)",
        )
        self.published_version_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.published_version_tree.customContextMenuRequested.connect(self._ctx_published_version_tree)
        self.published_version_tree.currentItemChanged.connect(self._on_published_version_selected)
        self.published_version_tree.itemDoubleClicked.connect(self._on_published_item_double_clicked)
        self.published_version_tree.filesDropped.connect(self._handle_published_version_drop)
        self.published_version_tree.enable_file_drag_out(self._drag_path_for_published_item)
        pr_layout.addWidget(self.published_version_tree, 1)

        p_splitter.addWidget(p_left)
        p_splitter.addWidget(p_right)
        p_splitter.setSizes([210, 390])
        p_layout.addWidget(p_splitter)
        self.triad_tabs.addTab(published_tab, "Published (0)")

        self.triad_tabs.currentChanged.connect(lambda _idx: self._sync_inspector_to_active_tab())
        p2_layout.addWidget(self.triad_tabs)
        main_splitter.addWidget(pane2)

        # Pane 3: Context-Aware Inspector & Actions
        pane3 = QFrame()
        pane3.setObjectName("PanelCard")
        p3_layout = QVBoxLayout(pane3)
        p3_layout.setSpacing(8)

        comp_cfg_header = QLabel("Component Settings")
        comp_cfg_header.setObjectName("SectionHeader")
        p3_layout.addWidget(comp_cfg_header)

        sw_form = QFormLayout()
        self.insp_software_combo = QComboBox()
        self.insp_software_combo.currentIndexChanged.connect(self._on_component_software_changed)
        sw_form.addRow("Bound Software:", self.insp_software_combo)
        p3_layout.addLayout(sw_form)

        self.sw_exe_hint_label = QLabel("")
        self.sw_exe_hint_label.setObjectName("MutedText")
        self.sw_exe_hint_label.setWordWrap(True)
        p3_layout.addWidget(self.sw_exe_hint_label)

        sep1 = QFrame()
        sep1.setFrameShape(QFrame.Shape.HLine)
        sep1.setStyleSheet("color: #27272a;")
        p3_layout.addWidget(sep1)

        insp_hdr_row = QHBoxLayout()
        insp_title = QLabel("Selection Inspector")
        insp_title.setObjectName("SectionHeader")
        self.insp_context_badge = QLabel("Working")
        self.insp_context_badge.setObjectName("ContextBadge")
        insp_hdr_row.addWidget(insp_title)
        insp_hdr_row.addStretch()
        insp_hdr_row.addWidget(self.insp_context_badge)
        p3_layout.addLayout(insp_hdr_row)

        self.selected_item_label = QLabel("No version selected")
        self.selected_item_label.setWordWrap(True)
        p3_layout.addWidget(self.selected_item_label)

        self.insp_lineage_label = QLabel("")
        self.insp_lineage_label.setObjectName("MutedText")
        self.insp_lineage_label.setWordWrap(True)
        p3_layout.addWidget(self.insp_lineage_label)

        self.meta_form = QFormLayout()
        self.insp_author_edit = QLineEdit()
        self.insp_source_edit = QLineEdit()
        self.insp_license_combo = QComboBox()
        self.insp_license_combo.setEditable(True)
        self.insp_license_combo.addItems(
            ["", "Original", "Royalty-Free", "CC-BY-4.0", "CC0", "Work for Hire", "Temp / Scratch Only", "Unspecified"]
        )
        self.insp_license_edit = self.insp_license_combo.lineEdit()
        self.insp_tags_edit = QLineEdit()
        self.insp_tags_edit.setPlaceholderText("comma-separated tags")
        self.insp_comment_edit = QLineEdit()
        self.insp_comment_edit.setPlaceholderText("Notes for this version / release")

        self.meta_form.addRow("Author:", self.insp_author_edit)
        self.meta_form.addRow("Source:", self.insp_source_edit)
        self.meta_form.addRow("License:", self.insp_license_combo)
        self.meta_form.addRow("Tags:", self.insp_tags_edit)
        self.insp_comment_label = QLabel("Note:")
        self.meta_form.addRow(self.insp_comment_label, self.insp_comment_edit)
        p3_layout.addLayout(self.meta_form)

        self.save_meta_btn = QPushButton("Save Metadata (Ctrl+S)")
        self.save_meta_btn.clicked.connect(self._save_inspector_metadata)
        p3_layout.addWidget(self.save_meta_btn)

        p3_layout.addStretch(1)

        sep2 = QFrame()
        sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet("color: #27272a;")
        p3_layout.addWidget(sep2)

        act_header = QLabel("Actions")
        act_header.setObjectName("SectionHeader")
        p3_layout.addWidget(act_header)

        self.launch_btn = QPushButton("Launch Working File")
        self.launch_btn.setObjectName("PrimaryButton")
        self.launch_btn.clicked.connect(self._action_primary_open)
        p3_layout.addWidget(self.launch_btn)

        self.vup_launch_btn = QPushButton("Version Up + Open")
        self.vup_launch_btn.clicked.connect(self._action_vup_and_launch)
        p3_layout.addWidget(self.vup_launch_btn)

        self.pub_from_working_btn = QPushButton("Publish from Working...")
        self.pub_from_working_btn.clicked.connect(self._action_publish_from_selected_working)
        p3_layout.addWidget(self.pub_from_working_btn)

        self.collect_media_btn = QPushButton("Scan && Collect External .rpp Media")
        self.collect_media_btn.clicked.connect(self._action_collect_external_media)
        p3_layout.addWidget(self.collect_media_btn)

        folder_row = QHBoxLayout()
        self.open_explorer_btn = QPushButton("Open Folder")
        self.open_explorer_btn.setToolTip("Open the selected item's folder in OS Explorer")
        self.open_explorer_btn.clicked.connect(self._action_open_in_explorer)
        self.copy_path_btn = QPushButton("Copy Path")
        self.copy_path_btn.setToolTip("Copy the selected item's full path to clipboard")
        self.copy_path_btn.clicked.connect(self._action_copy_selected_path)
        folder_row.addWidget(self.open_explorer_btn)
        folder_row.addWidget(self.copy_path_btn)
        p3_layout.addLayout(folder_row)

        self.vault_actions_box = QWidget()
        va_layout = QHBoxLayout(self.vault_actions_box)
        va_layout.setContentsMargins(0, 2, 0, 0)
        co_ver_btn = QPushButton("Check Out Item")
        co_ver_btn.clicked.connect(self._action_checkout_selected)
        off_ver_btn = QPushButton("Offload Item")
        off_ver_btn.clicked.connect(self._action_offload_selected)
        va_layout.addWidget(co_ver_btn)
        va_layout.addWidget(off_ver_btn)
        p3_layout.addWidget(self.vault_actions_box)

        self.delete_selected_btn = QPushButton("Delete Selected...")
        self.delete_selected_btn.setObjectName("DangerButton")
        self.delete_selected_btn.setToolTip("Delete the currently selected item (Del)")
        self.delete_selected_btn.clicked.connect(self._action_delete_active_selection)
        p3_layout.addWidget(self.delete_selected_btn)

        main_splitter.addWidget(pane3)
        main_splitter.setSizes([250, 650, 320])
        self.central_stack.addWidget(main_splitter)
        root_layout.addWidget(self.central_stack, 1)

        # ---------------- Bottom Status Bar ----------------
        status_bar = QFrame()
        status_bar.setObjectName("StatusBar")
        sb_layout = QHBoxLayout(status_bar)
        sb_layout.setContentsMargins(12, 5, 12, 5)
        sb_layout.setSpacing(10)

        self.active_path_btn = QPushButton("")
        self.active_path_btn.setObjectName("BreadcrumbButton")
        self.active_path_btn.setToolTip("Click to open active folder in Explorer")
        self.active_path_btn.clicked.connect(self._action_open_in_explorer)
        self.active_path_label = QLabel("")
        self.active_path_label.setObjectName("MutedText")
        self.active_path_label.hide()

        self.status_feedback_label = QLabel("")
        self.status_feedback_label.setObjectName("SuccessText")
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.timeout.connect(lambda: self.status_feedback_label.setText(""))

        self.client_root_btn = QPushButton()
        self.client_root_btn.setObjectName("BreadcrumbButton")
        self.client_root_btn.clicked.connect(self._prompt_change_client_root)

        self.dcc_status_pill = QPushButton("DCC: Setup…")
        self.dcc_status_pill.setObjectName("StatusPillLocal")
        self.dcc_status_pill.setToolTip("Manage REAPER, DaVinci Resolve, and DCC script integrations")
        self.dcc_status_pill.clicked.connect(lambda: self.open_integrations_manager_dialog(""))

        self.reaper_status_pill = QPushButton("REAPER: Offline")
        self.reaper_status_pill.setObjectName("StatusPillLocal")
        self.reaper_status_pill.setToolTip("REAPER DAW Live Bridge status — click to install or configure")
        self.reaper_status_pill.clicked.connect(self.open_reaper_integration_dialog)

        self.resolve_status_pill = QPushButton("Resolve: Offline")
        self.resolve_status_pill.setObjectName("StatusPillLocal")
        self.resolve_status_pill.setToolTip("DaVinci Resolve Bridge status — click to install or configure")
        self.resolve_status_pill.clicked.connect(self.open_resolve_integration_dialog)

        self.vault_status_pill = QPushButton("Standalone Local")
        self.vault_status_pill.setObjectName("StatusPillLocal")
        self.vault_status_pill.setToolTip("Click to manage Projects & Server Vault")
        self.vault_status_pill.clicked.connect(self.open_projects_and_vault_dialog)

        sb_layout.addWidget(self.active_path_btn, 1)
        sb_layout.addWidget(self.status_feedback_label)
        sb_layout.addWidget(self.client_root_btn)
        sb_layout.addWidget(self.dcc_status_pill)
        sb_layout.addWidget(self.reaper_status_pill)
        sb_layout.addWidget(self.resolve_status_pill)
        sb_layout.addWidget(self.vault_status_pill)
        root_layout.addWidget(status_bar)

        self._reaper_poll_timer = QTimer(self)
        self._reaper_poll_timer.setInterval(3000)
        self._reaper_poll_timer.timeout.connect(self._refresh_reaper_status_pill)
        if QApplication.platformName() != "offscreen":
            self._reaper_poll_timer.start()

        self._install_shortcuts()
        self.refresh_all()

    def _build_welcome_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.addStretch(1)

        card = QFrame()
        card.setObjectName("EmptyStateCard")
        card.setMaximumWidth(560)
        c_layout = QVBoxLayout(card)
        c_layout.setContentsMargins(28, 28, 28, 28)
        c_layout.setSpacing(12)

        title = QLabel("Welcome to Obol")
        title_font = QFont(self.font())
        title_font.setPointSize(16)
        title_font.setBold(True)
        title.setFont(title_font)
        c_layout.addWidget(title)

        sub = QLabel(
            "Organize any creative project into Triad Components without cluttering your working folders:\n\n"
            "• Working (working/v001/…) — Editable project files (.rpp, .blend, .kra) with 1-click Version Up\n"
            "• Repo Assets (repo/<asset>/v001/…) — Versioned stems, samples & references with author/license credits\n"
            "• Published (published/<deliverable>/v001/…) — Multi-format final deliverables linked to their source"
        )
        sub.setObjectName("MutedText")
        sub.setWordWrap(True)
        c_layout.addWidget(sub)

        btns = QHBoxLayout()
        create_btn = QPushButton("+ Create New Project")
        create_btn.setObjectName("PrimaryButton")
        create_btn.clicked.connect(self.open_new_project_wizard)
        reg_btn = QPushButton("Register Existing Project Folder...")
        reg_btn.clicked.connect(self._register_existing_project_shortcut)
        vault_btn = QPushButton("Projects && Vault...")
        vault_btn.clicked.connect(self.open_projects_and_vault_dialog)
        btns.addWidget(create_btn)
        btns.addWidget(reg_btn)
        btns.addWidget(vault_btn)
        btns.addStretch()
        c_layout.addLayout(btns)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)
        return page

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.open_new_project_wizard)
        QShortcut(QKeySequence("Ctrl+Shift+N"), self, activated=self._prompt_add_component)
        QShortcut(QKeySequence("Ctrl+U"), self, activated=self._action_version_up_working)
        QShortcut(QKeySequence("Ctrl+I"), self, activated=self._action_context_ingest)
        QShortcut(QKeySequence("Ctrl+P"), self, activated=self._action_publish_deliverable)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=self._save_inspector_metadata)
        QShortcut(QKeySequence("F5"), self, activated=self._action_refresh_and_reconcile)
        QShortcut(QKeySequence("Delete"), self, activated=self._action_delete_focused_widget)

    def show_status_message(self, message: str, duration_ms: int = 4000) -> None:
        self.status_feedback_label.setText(message)
        self._feedback_timer.start(duration_ms)

    def _register_existing_project_shortcut(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            "Select Existing Obol Project Folder",
            str(self.hub.get_client_root()),
        )
        if folder:
            entry = self.hub.register_project(Path(folder))
            self.active_project_name = entry.name
            self.hub.set_last_active_project(entry.name)
            self.refresh_all()
            self.show_status_message(f"✓ Registered '{entry.name}'")

    def closeEvent(self, event: object) -> None:
        if hasattr(self, "_reaper_poll_timer"):
            self._reaper_poll_timer.stop()
        super().closeEvent(event)  # type: ignore[arg-type]

    def _refresh_reaper_status_pill(self) -> None:
        st = self.reaper_bridge.get_state()
        if st.connected:
            ver_txt = f" ({st.reaper_version})" if st.reaper_version else ""
            self.reaper_status_pill.setText(f"REAPER: Connected{ver_txt}")
            self.reaper_status_pill.setObjectName("StatusPillOnline")
            self.reaper_status_pill.setVisible(True)
        else:
            self.reaper_status_pill.setText("REAPER: Offline")
            self.reaper_status_pill.setObjectName("StatusPillLocal")
            self.reaper_status_pill.setVisible(False)
        _repolish(self.reaper_status_pill)

        rst = self.resolve_bridge.get_state()
        if rst.connected:
            mode_txt = "Studio" if rst.mode == "studio_api" else "Bridge"
            self.resolve_status_pill.setText(f"Resolve: {mode_txt}")
            self.resolve_status_pill.setObjectName("StatusPillOnline")
            self.resolve_status_pill.setVisible(True)
        else:
            self.resolve_status_pill.setText("Resolve: Offline")
            self.resolve_status_pill.setObjectName("StatusPillLocal")
            self.resolve_status_pill.setVisible(False)
        _repolish(self.resolve_status_pill)

        reports = self.integration_manager.list_reports()
        live_names = [r.display_name.split()[0] for r in reports if r.bridge_connected]
        drift_cnt = sum(1 for r in reports if r.needs_update)
        inst_cnt = sum(1 for r in reports if r.installed)
        if live_names:
            self.dcc_status_pill.setText(f"DCC: {', '.join(live_names)} Live ●")
            self.dcc_status_pill.setObjectName("StatusPillOnline")
        elif drift_cnt:
            self.dcc_status_pill.setText(f"DCC: {drift_cnt} Update Needed ⚠")
            self.dcc_status_pill.setObjectName("StatusPillOffline")
        elif inst_cnt:
            self.dcc_status_pill.setText(f"DCC: {inst_cnt} Ready")
            self.dcc_status_pill.setObjectName("StatusPillLocal")
        else:
            self.dcc_status_pill.setText("DCC: Setup…")
            self.dcc_status_pill.setObjectName("StatusPillLocal")
        _repolish(self.dcc_status_pill)

    def _update_fast_drop_button(self) -> None:
        on = self.hub.config.fast_drop
        self.fast_drop_btn.setText("⚡ Fast Drop: ON" if on else "Fast Drop: OFF")
        self.fast_drop_btn.setObjectName("FastDropActive" if on else "")
        _repolish(self.fast_drop_btn)

    def _toggle_fast_drop(self) -> None:
        self.hub.config.fast_drop = not self.hub.config.fast_drop
        self.hub.save_config()
        self._update_fast_drop_button()
        state = "ON (auto-ingest on drop)" if self.hub.config.fast_drop else "OFF (prompt on drop)"
        self.show_status_message(f"Fast Drop: {state}")

    def refresh_all(self) -> None:
        self._refresh_reaper_status_pill()
        self.insp_software_combo.blockSignals(True)
        self.insp_software_combo.clear()
        for k in sorted(self.hub.config.software.keys()):
            prof = self.hub.config.software[k]
            self.insp_software_combo.addItem(f"{prof.display_name} ({k})", k)
        self.insp_software_combo.blockSignals(False)

        v_status = self.hub.get_vault_status()
        vault_online = v_status == VaultStatus.ONLINE
        self.vault_actions_box.setVisible(vault_online)
        client_root = self.hub.get_client_root()
        self.client_root_btn.setText(f"Root: {client_root.name or client_root}")
        self.client_root_btn.setToolTip(f"Client Workspace Root: {client_root} (Click to change)")

        if v_status == VaultStatus.ONLINE:
            self.vault_status_pill.setText("Vault Online")
            self.vault_status_pill.setObjectName("StatusPillOnline")
            self.vault_status_label.setText(f"Vault: {self.hub.config.vault.path}")
        elif v_status == VaultStatus.OFFLINE:
            self.vault_status_pill.setText("Vault Offline")
            self.vault_status_pill.setObjectName("StatusPillOffline")
            self.vault_status_label.setText(f"Vault offline: {self.hub.config.vault.path}")
        else:
            self.vault_status_pill.setText("Standalone Local")
            self.vault_status_pill.setObjectName("StatusPillLocal")
            self.vault_status_label.setText("Mode: Standalone Local")
        _repolish(self.vault_status_pill)

        projects = self.hub.list_projects()
        self.project_combo.blockSignals(True)
        self.project_combo.clear()
        for p in projects:
            self.project_combo.addItem(p.name)
        if self.active_project_name and self.project_combo.findText(self.active_project_name) >= 0:
            self.project_combo.setCurrentText(self.active_project_name)
        elif self.hub.config.last_active_project and self.project_combo.findText(self.hub.config.last_active_project) >= 0:
            self.active_project_name = self.hub.config.last_active_project
            self.project_combo.setCurrentText(self.active_project_name)
        elif projects:
            self.active_project_name = projects[0].name
            self.project_combo.setCurrentText(self.active_project_name)
        else:
            self.active_project_name = ""
        self.project_combo.blockSignals(False)

        if self.active_project_name:
            self.hub.set_last_active_project(self.active_project_name)

        self._load_active_project()

    def _action_refresh_and_reconcile(self) -> None:
        if self.active_project_root and self.active_project_root.is_dir():
            Reconciler.reconcile_project(self.active_project_root)
        self.refresh_all()
        self.show_status_message("✓ Synced & refreshed from disk")

    def _prompt_change_client_root(self) -> None:
        current_root = str(self.hub.get_client_root())
        folder = QFileDialog.getExistingDirectory(self, "Select Local Client Workspace Root", current_root)
        if folder:
            self.hub.set_client_root(Path(folder))
            self.refresh_all()
            self.show_status_message(f"✓ Client Root set to {folder}")

    def _restore_project_text(self) -> None:
        if self.project_combo.findText(self.project_combo.currentText()) < 0:
            self.project_combo.setCurrentText(self.active_project_name)

    def _on_project_combo_changed(self, proj_name: str) -> None:
        if proj_name:
            self.active_project_name = proj_name
            self.active_component_path = ""
            self.hub.set_last_active_project(proj_name)
            self._load_active_project()

    def _clear_component_triad(self) -> None:
        self.active_component_path = ""
        self.active_snapshot = None
        self.selected_repo_asset_path = ""
        self.selected_deliverable_path = ""
        self.working_list.clear()
        self.repo_asset_tree.clear()
        self.repo_version_list.clear()
        self.published_deliverable_tree.clear()
        self.published_version_tree.clear()
        self.triad_tabs.setTabText(0, "Working (0)")
        self.triad_tabs.setTabText(1, "Repo Assets (0)")
        self.triad_tabs.setTabText(2, "Published (0)")
        self._sync_inspector_to_active_tab()

    def _build_nested_tree_parent(
        self,
        tree: QTreeWidget,
        rel_path: str,
        group_cache: dict[str, QTreeWidgetItem],
        group_role_tag: str,
    ) -> tuple[QTreeWidget | QTreeWidgetItem, str]:
        """Creates or reuses collapsible folder nodes for slash-nested paths, returning (parent, leaf_name)."""
        parts = [p for p in rel_path.split("/") if p]
        if len(parts) <= 1:
            return tree, rel_path
        parent: QTreeWidget | QTreeWidgetItem = tree
        prefix = ""
        for part in parts[:-1]:
            prefix = f"{prefix}/{part}" if prefix else part
            if prefix not in group_cache:
                g_item = QTreeWidgetItem(parent, [f"{part}/", ""])
                g_item.setData(0, Qt.ItemDataRole.UserRole, prefix)
                g_item.setData(0, Qt.ItemDataRole.UserRole + 1, group_role_tag)
                font = g_item.font(0)
                font.setBold(True)
                g_item.setFont(0, font)
                g_item.setForeground(0, QColor("#a1a1aa"))
                group_cache[prefix] = g_item
            parent = group_cache[prefix]
        return parent, parts[-1]

    def _load_active_project(self) -> None:
        self.component_tree.clear()
        self._selected_group_prefix = ""
        if not self.active_project_name:
            self.active_project_root = None
            self.active_path_btn.setText("No project loaded")
            self.active_path_label.setText("No project loaded — click '+ New Project' to create one")
            self.cred_btn.setEnabled(False)
            self.central_stack.setCurrentIndex(0)
            self._clear_component_triad()
            return

        entry = self.hub.get_project(self.active_project_name)
        if not entry:
            self.active_project_root = None
            self.cred_btn.setEnabled(False)
            self.central_stack.setCurrentIndex(0)
            self._clear_component_triad()
            return

        self.cred_btn.setEnabled(True)
        self.central_stack.setCurrentIndex(1)
        self.active_project_root = Path(entry.local_path)
        if self.hub.get_vault_status() == VaultStatus.ONLINE and not (self.active_project_root / ".obol").is_dir():
            self.sync.sync_metadata(entry.name)

        components = discover_project_components(self.active_project_root)
        group_cache: dict[str, QTreeWidgetItem] = {}
        first_item: Optional[QTreeWidgetItem] = None
        for comp in components:
            snap = load_component_snapshot(self.active_project_root, comp)
            parent_node, leaf_name = self._build_nested_tree_parent(self.component_tree, comp, group_cache, "group")
            item = QTreeWidgetItem(parent_node, [leaf_name, snap.software])
            item.setData(0, Qt.ItemDataRole.UserRole, comp)
            item.setData(0, Qt.ItemDataRole.UserRole + 1, "component")
            item.setToolTip(0, comp)
            if first_item is None or comp == self.active_component_path:
                first_item = item

        self.component_tree.expandAll()
        if self.comp_filter_edit.text().strip():
            self._filter_component_tree(self.comp_filter_edit.text())

        if first_item is not None:
            self.component_tree.setCurrentItem(first_item)
        else:
            self.active_path_btn.setText(f"📁 {self.active_project_root}")
            self.active_path_label.setText(str(self.active_project_root))
            self._clear_component_triad()

    def _filter_component_tree(self, text: str) -> None:
        needle = text.strip().lower()

        def _visit(item: QTreeWidgetItem) -> bool:
            kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if kind == "component":
                comp_path = str(item.data(0, Qt.ItemDataRole.UserRole) or "").lower()
                sw = item.text(1).lower()
                visible = (not needle) or (needle in comp_path) or (needle in sw)
                item.setHidden(not visible)
                return visible
            any_child_visible = False
            for idx in range(item.childCount()):
                if _visit(item.child(idx)):
                    any_child_visible = True
            group_path = str(item.data(0, Qt.ItemDataRole.UserRole) or "").lower()
            visible = any_child_visible or (bool(needle) and needle in group_path)
            item.setHidden(not visible)
            if visible and needle:
                item.setExpanded(True)
            return visible

        for i in range(self.component_tree.topLevelItemCount()):
            _visit(self.component_tree.topLevelItem(i))

    def _on_component_tree_selected(self, current: Optional[QTreeWidgetItem], _prev: Optional[QTreeWidgetItem]) -> None:
        if current is None or self.active_project_root is None:
            return
        kind = current.data(0, Qt.ItemDataRole.UserRole + 1)
        if kind == "group":
            self._selected_group_prefix = str(current.data(0, Qt.ItemDataRole.UserRole) or "")
            return
        comp_path = current.data(0, Qt.ItemDataRole.UserRole)
        if not comp_path:
            return
        self._selected_group_prefix = ""
        self.active_component_path = str(comp_path)
        full_path = self.active_project_root / self.active_component_path
        self.active_path_btn.setText(f"📁 {self.active_project_name} / {self.active_component_path}   ({full_path})")
        self.active_path_label.setText(str(full_path))
        self._refresh_component_triad()

    def _refresh_component_triad(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        snap = load_component_snapshot(self.active_project_root, self.active_component_path)
        self.active_snapshot = snap

        self.insp_software_combo.blockSignals(True)
        sw_idx = self.insp_software_combo.findData(snap.software)
        if sw_idx >= 0:
            self.insp_software_combo.setCurrentIndex(sw_idx)
        else:
            self.insp_software_combo.setCurrentText(snap.software)
        self.insp_software_combo.blockSignals(False)
        self._update_software_exe_hint(snap.software)
        self.collect_media_btn.setVisible(snap.software == "reaper")
        self.reaper_export_btn.setVisible(snap.software == "reaper")
        self.resolve_export_btn.setVisible(snap.software == "resolve")

        # Populate Working tab (multi-column)
        prev_working_ver = self._selected_working_version()
        self.working_list.blockSignals(True)
        self.working_list.clear()
        latest_w_ver = max((w.version for w in snap.working_versions), default=0)
        selected_w_item: Optional[QTreeWidgetItem] = None
        for w in reversed(snap.working_versions):
            primary_abs = self.active_project_root / self.active_component_path / w.primary_file
            locked_badge = " 🔒" if (primary_abs.is_file() and not os.access(primary_abs, os.W_OK)) else ""
            latest_badge = " ★" if w.version == latest_w_ver else ""
            note_txt = w.comment
            if w.tags:
                note_txt = f"{note_txt}  [{', '.join(w.tags)}]" if note_txt else f"[{', '.join(w.tags)}]"
            item = QTreeWidgetItem(
                self.working_list,
                [
                    f"{w.version_label}{latest_badge}{locked_badge}",
                    Path(w.primary_file).name if w.primary_file else "—",
                    _format_timestamp(w.created_at),
                    note_txt,
                ],
            )
            item.setData(0, Qt.ItemDataRole.UserRole, w.version)
            item.setToolTip(0, f"{w.folder_rel}/{Path(w.primary_file).name}")
            if selected_w_item is None or w.version == prev_working_ver:
                selected_w_item = item
        self.working_list.blockSignals(False)
        if selected_w_item is not None:
            self.working_list.setCurrentItem(selected_w_item)
        self.triad_tabs.setTabText(0, f"Working ({len(snap.working_versions)})")

        # Populate Repo Assets tab (nested categories)
        self.repo_asset_tree.blockSignals(True)
        self.repo_asset_tree.clear()
        repo_groups: dict[str, QTreeWidgetItem] = {}
        first_asset_item: Optional[QTreeWidgetItem] = None
        for apath, asset in sorted(snap.repo_assets.items()):
            parent_node, leaf_name = self._build_nested_tree_parent(
                self.repo_asset_tree, apath, repo_groups, "repo_category"
            )
            used_str = f"{len(asset.used_by_working_versions)}w" if asset.used_by_working_versions else "—"
            aitem = QTreeWidgetItem(parent_node, [leaf_name, f"{len(asset.versions)}v", used_str])
            aitem.setData(0, Qt.ItemDataRole.UserRole, apath)
            aitem.setData(0, Qt.ItemDataRole.UserRole + 1, "repo_asset")
            aitem.setToolTip(0, f"repo/{apath}/")
            if first_asset_item is None or apath == self.selected_repo_asset_path:
                first_asset_item = aitem
        self.repo_asset_tree.expandAll()
        self.repo_asset_tree.blockSignals(False)
        self.triad_tabs.setTabText(1, f"Repo Assets ({len(snap.repo_assets)})")
        if first_asset_item is not None:
            self.repo_asset_tree.setCurrentItem(first_asset_item)
            self._on_repo_asset_selected(first_asset_item, None)
        else:
            self.selected_repo_asset_path = ""
            self.repo_ver_header.setText("Versions in repo/")
            self.repo_version_list.clear()

        # Populate Published tab (named deliverables + legacy)
        self.published_deliverable_tree.blockSignals(True)
        self.published_deliverable_tree.clear()
        deliv_groups: dict[str, QTreeWidgetItem] = {}
        first_deliv_item: Optional[QTreeWidgetItem] = None
        total_pub_releases = 0
        for dpath, deliv in sorted(snap.published_deliverables.items()):
            total_pub_releases += len(deliv.versions)
            parent_node, leaf_name = self._build_nested_tree_parent(
                self.published_deliverable_tree, dpath, deliv_groups, "deliv_category"
            )
            ditem = QTreeWidgetItem(parent_node, [leaf_name, f"{len(deliv.versions)}v"])
            ditem.setData(0, Qt.ItemDataRole.UserRole, dpath)
            ditem.setData(0, Qt.ItemDataRole.UserRole + 1, "deliverable")
            ditem.setToolTip(0, f"published/{dpath}/")
            if first_deliv_item is None or dpath == self.selected_deliverable_path:
                first_deliv_item = ditem
        if snap.published_versions:
            total_pub_releases += len(snap.published_versions)
            litem = QTreeWidgetItem(self.published_deliverable_tree, ["(legacy)", f"{len(snap.published_versions)}v"])
            litem.setData(0, Qt.ItemDataRole.UserRole, "__legacy__")
            litem.setData(0, Qt.ItemDataRole.UserRole + 1, "deliverable")
            if first_deliv_item is None or self.selected_deliverable_path == "__legacy__":
                first_deliv_item = litem
        self.published_deliverable_tree.expandAll()
        self.published_deliverable_tree.blockSignals(False)

        self.triad_tabs.setTabText(2, f"Published ({total_pub_releases})")
        if first_deliv_item is not None:
            self.published_deliverable_tree.setCurrentItem(first_deliv_item)
            self._on_published_deliverable_selected(first_deliv_item, None)
        else:
            self.selected_deliverable_path = ""
            self.published_version_tree.clear()
            self.pub_ver_header.setText("Releases in published/")

        self._sync_inspector_to_active_tab()

    def _on_repo_asset_selected(self, current: Optional[QTreeWidgetItem], _prev: Optional[QTreeWidgetItem]) -> None:
        if current is None or self.active_snapshot is None:
            return
        if current.data(0, Qt.ItemDataRole.UserRole + 1) == "repo_category":
            return
        apath = str(current.data(0, Qt.ItemDataRole.UserRole) or "")
        if not apath:
            return
        self.selected_repo_asset_path = apath
        asset = self.active_snapshot.repo_assets.get(apath)
        self.repo_version_list.blockSignals(True)
        self.repo_version_list.clear()
        if not asset:
            self.repo_version_list.blockSignals(False)
            self._sync_inspector_to_active_tab()
            return
        self.repo_ver_header.setText(f"Versions in repo/{apath}/")
        for rv in reversed(asset.versions):
            primary_tf = rv.files[0] if rv.files else None
            size_txt = _format_size(primary_tf.size_bytes) if primary_tf else "—"
            fname = Path(rv.primary_file).name if rv.primary_file else "—"
            item = QTreeWidgetItem(
                self.repo_version_list,
                [rv.version_label, fname, size_txt, rv.comment],
            )
            item.setData(0, Qt.ItemDataRole.UserRole, rv.version)
            self.repo_version_list.addTopLevelItem(item)
        self.repo_version_list.blockSignals(False)
        if self.repo_version_list.count() > 0:
            self.repo_version_list.setCurrentRow(0)
        self._sync_inspector_to_active_tab()

    def _on_repo_version_selected(self, _current: Optional[QTreeWidgetItem], _prev: Optional[QTreeWidgetItem]) -> None:
        self._sync_inspector_to_active_tab()

    def _on_working_version_selected(self, _current: Optional[QTreeWidgetItem], _prev: Optional[QTreeWidgetItem]) -> None:
        self._sync_inspector_to_active_tab()

    def _on_published_deliverable_selected(
        self, current: Optional[QTreeWidgetItem], _prev: Optional[QTreeWidgetItem]
    ) -> None:
        if current is None or self.active_snapshot is None:
            return
        if current.data(0, Qt.ItemDataRole.UserRole + 1) == "deliv_category":
            return
        self.published_version_tree.blockSignals(True)
        self.published_version_tree.clear()
        dpath = str(current.data(0, Qt.ItemDataRole.UserRole) or "")
        self.selected_deliverable_path = dpath
        if dpath == "__legacy__":
            versions = self.active_snapshot.published_versions
            self.pub_ver_header.setText("Legacy Releases in published/")
        else:
            deliv = self.active_snapshot.published_deliverables.get(dpath)
            if not deliv:
                self.published_version_tree.blockSignals(False)
                self._sync_inspector_to_active_tab()
                return
            versions = deliv.versions
            self.pub_ver_header.setText(f"Releases in published/{dpath}/")

        first_ver_item: Optional[QTreeWidgetItem] = None
        for pv in reversed(versions):
            ftypes = sorted({(tf.file_type or Path(tf.rel_path).suffix.lstrip(".")).upper() for tf in pv.files if tf.rel_path})
            type_badges = ", ".join(ftypes) if ftypes else "EMPTY"
            src_str = f"← working/v{pv.source_working_version:03d}" if pv.source_working_version else ""
            ver_item = QTreeWidgetItem(
                self.published_version_tree,
                [f"{pv.version_label}/ ({len(pv.files)} file{'s' if len(pv.files) != 1 else ''})", type_badges, src_str, pv.comment],
            )
            ver_item.setData(0, Qt.ItemDataRole.UserRole, ("version", dpath, pv.version))
            if first_ver_item is None:
                first_ver_item = ver_item
            for tf in pv.files:
                ftype = (tf.file_type or Path(tf.rel_path).suffix.lstrip(".")).upper()
                child = QTreeWidgetItem(
                    ver_item,
                    [Path(tf.rel_path).name, ftype, _format_size(tf.size_bytes), tf.output_label],
                )
                child.setData(0, Qt.ItemDataRole.UserRole, ("file", dpath, pv.version, tf.rel_path))
        self.published_version_tree.expandAll()
        self.published_version_tree.blockSignals(False)
        if first_ver_item is not None:
            self.published_version_tree.setCurrentItem(first_ver_item)
        self._sync_inspector_to_active_tab()

    def _on_published_version_selected(
        self, _current: Optional[QTreeWidgetItem], _prev: Optional[QTreeWidgetItem]
    ) -> None:
        self._sync_inspector_to_active_tab()

    def _selected_working_version(self) -> Optional[int]:
        curr = self.working_list.currentItem()
        if curr is None:
            return None
        raw = curr.data(0, Qt.ItemDataRole.UserRole)
        return int(raw) if raw is not None else None

    def _selected_repo_version(self) -> Optional[int]:
        curr = self.repo_version_list.currentItem()
        if curr is None:
            return None
        raw = curr.data(0, Qt.ItemDataRole.UserRole)
        return int(raw) if raw is not None else None

    def _selected_published_target(self) -> Optional[tuple[str, str, int, str]]:
        curr = self.published_version_tree.currentItem()
        if curr is None:
            return None
        data = curr.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(data, tuple) or len(data) < 3:
            return None
        kind = str(data[0])
        dpath = str(data[1])
        ver = int(data[2])
        rel_path = str(data[3]) if len(data) > 3 else ""
        return kind, dpath, ver, rel_path

    def _set_repo_fields_visible(self, visible: bool) -> None:
        for widget in (self.insp_author_edit, self.insp_source_edit, self.insp_license_combo):
            self.meta_form.setRowVisible(widget, visible)

    def _sync_inspector_to_active_tab(self) -> None:
        tab_idx = self.triad_tabs.currentIndex()
        snap = self.active_snapshot

        if snap is None or not self.active_project_root or not self.active_component_path:
            self._set_repo_fields_visible(False)
            self.insp_context_badge.setText("Idle")
            self.selected_item_label.setText("No component selected")
            self.insp_lineage_label.setText("")
            self.insp_author_edit.clear()
            self.insp_source_edit.clear()
            self.insp_license_combo.setCurrentText("")
            self.insp_tags_edit.clear()
            self.insp_comment_edit.clear()
            self.save_meta_btn.setEnabled(False)
            self.launch_btn.setEnabled(False)
            self.vup_launch_btn.setEnabled(False)
            self.pub_from_working_btn.setEnabled(False)
            self.open_explorer_btn.setEnabled(False)
            self.copy_path_btn.setEnabled(False)
            self.delete_selected_btn.setEnabled(False)
            self.reaper_export_btn.setVisible(False)
            self.resolve_export_btn.setVisible(False)
            return

        prof = self.hub.get_software_profile(snap.software)
        latest_w = max((w.version for w in snap.working_versions), default=0)
        self.vup_launch_btn.setEnabled(True)
        self.vup_launch_btn.setText(f"Version Up (→ v{latest_w + 1:03d}) + Open")
        self.open_explorer_btn.setEnabled(True)
        self.copy_path_btn.setEnabled(True)

        if tab_idx == 0:
            # Working Tab
            self._set_repo_fields_visible(False)
            self.insp_context_badge.setText("Working Version")
            self.insp_comment_label.setText("Version Note:")
            ver = self._selected_working_version()
            bundle = next((v for v in snap.working_versions if v.version == ver), None) if ver is not None else None
            if bundle is None:
                self.selected_item_label.setText("No working version selected")
                self.insp_lineage_label.setText("Click '+ Version Up' or drop files to create working/v001/")
                self.insp_tags_edit.clear()
                self.insp_comment_edit.clear()
                self.save_meta_btn.setEnabled(False)
                self.launch_btn.setEnabled(False)
                self.pub_from_working_btn.setEnabled(False)
                self.delete_selected_btn.setEnabled(bool(self.active_component_path))
                self.delete_selected_btn.setText("Delete Component...")
            else:
                self.selected_item_label.setText(f"{bundle.version_label}  •  {Path(bundle.primary_file).name}")
                parent_txt = f"Branched from v{bundle.parent_version:03d}" if bundle.parent_version else "Initial version"
                self.insp_lineage_label.setText(f"{parent_txt}  •  {_format_timestamp(bundle.created_at)}")
                self.insp_tags_edit.setText(", ".join(bundle.tags))
                self.insp_comment_edit.setText(bundle.comment)
                self.save_meta_btn.setEnabled(True)
                self.launch_btn.setEnabled(True)
                self.launch_btn.setText(f"Open {bundle.version_label} in {prof.display_name}")
                self.pub_from_working_btn.setEnabled(True)
                self.pub_from_working_btn.setText(f"Publish from {bundle.version_label}...")
                self.delete_selected_btn.setEnabled(True)
                self.delete_selected_btn.setText(f"Delete {bundle.version_label}...")

        elif tab_idx == 1:
            # Repo Assets Tab
            self._set_repo_fields_visible(True)
            self.insp_context_badge.setText("Repo Asset")
            self.insp_comment_label.setText("Version Note:")
            asset = snap.repo_assets.get(self.selected_repo_asset_path) if self.selected_repo_asset_path else None
            if asset is None:
                self.selected_item_label.setText("No repo asset selected")
                self.insp_lineage_label.setText("Select or drop an asset to inspect provenance & credits")
                self.insp_author_edit.clear()
                self.insp_source_edit.clear()
                self.insp_license_combo.setCurrentText("")
                self.insp_tags_edit.clear()
                self.insp_comment_edit.clear()
                self.save_meta_btn.setEnabled(False)
                self.launch_btn.setEnabled(bool(latest_w))
                self.launch_btn.setText(f"Open Latest Working (v{latest_w:03d})" if latest_w else "Launch Working File")
                self.pub_from_working_btn.setEnabled(bool(latest_w))
                self.delete_selected_btn.setEnabled(False)
                self.delete_selected_btn.setText("Delete Selected...")
            else:
                ver = self._selected_repo_version()
                bundle = next((v for v in asset.versions if v.version == ver), None) if ver is not None else None
                used_txt = (
                    ", ".join(f"v{u:03d}" for u in asset.used_by_working_versions)
                    if asset.used_by_working_versions
                    else "Not linked to a working version yet"
                )
                self.insp_author_edit.setText(asset.author)
                self.insp_source_edit.setText(asset.source)
                self.insp_license_combo.setCurrentText(asset.license)
                self.insp_tags_edit.setText(", ".join(asset.tags))
                if bundle is not None:
                    self.selected_item_label.setText(f"repo/{asset.asset_path}/{bundle.version_label}/")
                    self.insp_lineage_label.setText(f"Used in working: {used_txt}")
                    self.insp_comment_edit.setText(bundle.comment)
                    self.launch_btn.setEnabled(True)
                    self.launch_btn.setText(f"Open {bundle.version_label} File")
                    self.delete_selected_btn.setEnabled(True)
                    self.delete_selected_btn.setText(f"Delete {asset.name} {bundle.version_label}...")
                else:
                    self.selected_item_label.setText(f"repo/{asset.asset_path}/ (Empty slot)")
                    self.insp_lineage_label.setText("Drop a file into the version list to populate v001")
                    self.insp_comment_edit.clear()
                    self.launch_btn.setEnabled(bool(latest_w))
                    self.launch_btn.setText(f"Open Latest Working (v{latest_w:03d})" if latest_w else "Launch Working File")
                    self.delete_selected_btn.setEnabled(True)
                    self.delete_selected_btn.setText(f"Delete Asset '{asset.name}'...")
                self.save_meta_btn.setEnabled(True)
                self.pub_from_working_btn.setEnabled(bool(latest_w))
                self.pub_from_working_btn.setText(f"Publish from v{latest_w:03d}..." if latest_w else "Publish from Working...")

        else:
            # Published Tab
            self._set_repo_fields_visible(False)
            self.insp_context_badge.setText("Published")
            self.insp_comment_label.setText("Release Note:")
            target = self._selected_published_target()
            if target is None:
                if self.selected_deliverable_path and self.selected_deliverable_path != "__legacy__":
                    self.selected_item_label.setText(f"published/{self.selected_deliverable_path}/")
                    self.insp_lineage_label.setText("Click '+ Publish...' or drop exports to create the next release")
                    self.delete_selected_btn.setEnabled(True)
                    self.delete_selected_btn.setText(f"Delete '{self.selected_deliverable_path}'...")
                else:
                    self.selected_item_label.setText("No published release selected")
                    self.insp_lineage_label.setText("")
                    self.delete_selected_btn.setEnabled(False)
                    self.delete_selected_btn.setText("Delete Selected...")
                self.insp_tags_edit.clear()
                self.insp_comment_edit.clear()
                self.save_meta_btn.setEnabled(False)
                self.launch_btn.setEnabled(bool(latest_w))
                self.launch_btn.setText(f"Open Latest Working (v{latest_w:03d})" if latest_w else "Launch Working File")
            else:
                kind, dpath, ver, rel_path = target
                if dpath == "__legacy__":
                    bundle = next((v for v in snap.published_versions if v.version == ver), None)
                else:
                    deliv = snap.published_deliverables.get(dpath)
                    bundle = next((v for v in deliv.versions if v.version == ver), None) if deliv else None

                if bundle is None:
                    self.save_meta_btn.setEnabled(False)
                    self.delete_selected_btn.setEnabled(False)
                else:
                    src_w = f"Source: working/v{bundle.source_working_version:03d}" if bundle.source_working_version else "Source: unspecified"
                    if kind == "file" and rel_path:
                        tf = next((f for f in bundle.files if f.rel_path == rel_path), None)
                        sha_short = tf.sha256[:12] if tf and tf.sha256 else "n/a"
                        self.selected_item_label.setText(f"{rel_path}")
                        self.insp_lineage_label.setText(f"{src_w}  •  sha256:{sha_short}…")
                        self.launch_btn.setEnabled(True)
                        self.launch_btn.setText("Open Published File")
                    else:
                        self.selected_item_label.setText(f"{bundle.folder_rel}/ ({len(bundle.files)} files)")
                        self.insp_lineage_label.setText(f"{src_w}  •  {_format_timestamp(bundle.created_at)}")
                        self.launch_btn.setEnabled(bool(bundle.files))
                        self.launch_btn.setText(f"Open {bundle.version_label} Primary File")
                    self.insp_tags_edit.setText(", ".join(bundle.tags))
                    self.insp_comment_edit.setText(bundle.comment)
                    self.save_meta_btn.setEnabled(True)
                    self.delete_selected_btn.setEnabled(True)
                    self.delete_selected_btn.setText(f"Delete {dpath} {bundle.version_label}...")
            self.pub_from_working_btn.setEnabled(bool(latest_w))
            self.pub_from_working_btn.setText(f"Publish from v{latest_w:03d}..." if latest_w else "Publish from Working...")

    def _update_software_exe_hint(self, sw_key: str) -> None:
        prof = self.hub.get_software_profile(sw_key)
        if prof.executable:
            if Path(prof.executable).expanduser().is_file():
                self.sw_exe_hint_label.setText(f"✓ App: {Path(prof.executable).name}")
            else:
                self.sw_exe_hint_label.setText(f"⚠ Missing executable: {prof.executable}")
        else:
            self.sw_exe_hint_label.setText("ℹ Uses OS default app (configure .exe in ⚙ Settings → Software)")

    def _on_component_software_changed(self, _idx: int) -> None:
        if not self.active_project_root or not self.active_snapshot:
            return
        sw_key = str(self.insp_software_combo.currentData() or self.insp_software_combo.currentText())
        if not sw_key:
            return
        self.active_snapshot.software = sw_key
        save_component_snapshot(self.active_project_root, self.active_snapshot)
        self._update_software_exe_hint(sw_key)
        self.collect_media_btn.setVisible(sw_key == "reaper")
        self.reaper_export_btn.setVisible(sw_key == "reaper")
        self.resolve_export_btn.setVisible(sw_key == "resolve")

        # Update software badge in component_tree immediately
        def _update_tree_item(item: QTreeWidgetItem) -> bool:
            if item.data(0, Qt.ItemDataRole.UserRole + 1) == "component" and item.data(0, Qt.ItemDataRole.UserRole) == self.active_component_path:
                item.setText(1, sw_key)
                return True
            for i in range(item.childCount()):
                if _update_tree_item(item.child(i)):
                    return True
            return False

        for i in range(self.component_tree.topLevelItemCount()):
            if _update_tree_item(self.component_tree.topLevelItem(i)):
                break

        self._sync_inspector_to_active_tab()
        self.show_status_message(f"✓ Bound '{self.active_component_path}' to {sw_key}")

    def _save_inspector_metadata(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        tab_idx = self.triad_tabs.currentIndex()
        tags = [t.strip() for t in self.insp_tags_edit.text().split(",") if t.strip()]
        comment = self.insp_comment_edit.text().strip()

        if tab_idx == 0:
            ver = self._selected_working_version()
            if ver is None:
                return
            self.workspace.update_working_metadata(
                project_root=self.active_project_root,
                component_path=self.active_component_path,
                version=ver,
                comment=comment,
                tags=tags,
            )
            self._refresh_component_triad()
            self.show_status_message(f"✓ Saved metadata for working/v{ver:03d}")
            return

        if tab_idx == 1:
            if not self.selected_repo_asset_path:
                return
            ver = self._selected_repo_version()
            self.workspace.update_repo_metadata(
                project_root=self.active_project_root,
                component_path=self.active_component_path,
                asset_path=self.selected_repo_asset_path,
                author=self.insp_author_edit.text().strip(),
                source_info=self.insp_source_edit.text().strip(),
                license_info=self.insp_license_combo.currentText().strip(),
                tags=tags,
                comment=comment,
                version=ver,
            )
            self._refresh_component_triad()
            self.show_status_message(f"✓ Saved metadata for repo/{self.selected_repo_asset_path}")
            return

        target = self._selected_published_target()
        if target is None:
            return
        _, dpath, ver, _ = target
        self.workspace.update_published_metadata(
            project_root=self.active_project_root,
            component_path=self.active_component_path,
            deliverable_name=dpath,
            version=ver,
            comment=comment,
            tags=tags,
        )
        self._refresh_component_triad()
        self.show_status_message(f"✓ Saved metadata for published/{dpath}/v{ver:03d}")

    def _prompt_add_component(self, prefix_override: Optional[str] = None) -> None:
        if not self.active_project_root:
            return
        initial_prefix = prefix_override if prefix_override is not None else self._selected_group_prefix
        dlg = AddComponentDialog(self, self.hub, initial_prefix=initial_prefix)
        if dlg.exec() == AddComponentDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            cpath = str(vals["component_path"])
            self.workspace.create_component(
                project_root=self.active_project_root,
                component_path=cpath,
                task_template=str(vals["task_template"]),
                software_override=str(vals["software_override"]),
                seed_initial_working=bool(vals["seed_initial_working"]),
            )
            self.active_component_path = cpath
            self._load_active_project()
            self.show_status_message(f"✓ Created component '{cpath}'")

    def _action_version_up_working(self, source_version: Optional[int] = None) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        comment, ok = QInputDialog.getText(self, "Version Up Working", "Version note (optional):")
        if not ok:
            return
        src_v = source_version if source_version is not None else self._selected_working_version()
        bundle = self.workspace.version_up_working(
            self.active_project_root,
            self.active_component_path,
            comment=comment,
            source_version=src_v,
        )
        self._refresh_component_triad()
        self.show_status_message(f"✓ Created working/{bundle.version_label}")

    def _action_context_ingest(self) -> None:
        idx = self.triad_tabs.currentIndex()
        if idx == 0:
            self._action_ingest_working_file()
        elif idx == 1:
            self._action_ingest_repo_file()
        else:
            self._action_publish_deliverable()

    def _action_ingest_working_file(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Select Working File(s) to Ingest into working/")
        if not files:
            return
        self._ingest_working_flow([Path(f) for f in files])

    def _handle_working_drop(self, paths: list[Path], _target_item: object) -> None:
        self._ingest_working_flow(paths)

    def _ingest_working_flow(self, source_paths: list[Path]) -> None:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return
        if not source_paths:
            return
        if self.hub.config.fast_drop:
            bundle = self.workspace.ingest_working_files(
                project_root=self.active_project_root,
                component_path=self.active_component_path,
                source_paths=source_paths,
            )
            self._refresh_component_triad()
            self.show_status_message(f"✓ Ingested working/{bundle.version_label}")
            return

        next_v = max((v.version for v in self.active_snapshot.working_versions), default=0) + 1
        comp_leaf = self.active_component_path.split("/")[-1]
        dlg = IngestWorkingDialog(self, source_paths=source_paths, component_leaf=comp_leaf, next_version_num=next_v)
        if dlg.exec() == IngestWorkingDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            bundle = self.workspace.ingest_working_files(
                project_root=self.active_project_root,
                component_path=self.active_component_path,
                source_paths=source_paths,
                comment=str(vals["comment"]),
                tags=list(vals["tags"]),  # type: ignore[arg-type]
                copy_mode=str(vals["copy_mode"]),
                rename_to_convention=bool(vals["rename_to_convention"]),
            )
            self._refresh_component_triad()
            self.show_status_message(f"✓ Ingested working/{bundle.version_label}")

    def _action_new_repo_asset(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        name, ok = QInputDialog.getText(
            self,
            "New Repo Asset Slot",
            "Enter asset name (or category/name, e.g. 'lead_vocal' or 'audio/lead_vocal'):",
        )
        if ok and name.strip():
            asset = self.workspace.create_repo_asset(
                self.active_project_root,
                self.active_component_path,
                asset_path=name.strip(),
            )
            self.selected_repo_asset_path = asset.asset_path
            self._refresh_component_triad()
            self.show_status_message(f"✓ Created repo/{asset.asset_path}/")

    def _action_new_deliverable(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        name, ok = QInputDialog.getText(
            self,
            "New Published Deliverable Slot",
            "Enter deliverable name (e.g. 'main', 'stems', 'midi_pack'):",
        )
        if ok and name.strip():
            deliv = self.workspace.create_published_deliverable(
                self.active_project_root,
                self.active_component_path,
                deliverable_name=name.strip(),
            )
            self.selected_deliverable_path = deliv.deliverable_path
            self._refresh_component_triad()
            self.show_status_message(f"✓ Created deliverable published/{deliv.deliverable_path}/")

    def _action_ingest_repo_file(self) -> None:
        self._ingest_repo_flow(existing_asset_path=None)

    def _action_ingest_repo_version(self) -> None:
        if not self.selected_repo_asset_path:
            self._ingest_repo_flow(existing_asset_path=None)
        else:
            self._ingest_repo_flow(existing_asset_path=self.selected_repo_asset_path)

    def _handle_repo_tree_drop(self, paths: list[Path], target_item: object) -> None:
        target_asset: Optional[str] = None
        category_prefix: Optional[str] = None
        if isinstance(target_item, QTreeWidgetItem):
            kind = target_item.data(0, Qt.ItemDataRole.UserRole + 1)
            raw = target_item.data(0, Qt.ItemDataRole.UserRole)
            if kind == "repo_category" and raw:
                category_prefix = str(raw)
            elif raw:
                target_asset = str(raw)
        self._ingest_repo_flow(
            existing_asset_path=target_asset,
            source_paths=paths,
            category_prefix=category_prefix,
        )

    def _handle_repo_version_drop(self, paths: list[Path], _target_item: object) -> None:
        target_asset = self.selected_repo_asset_path or None
        self._ingest_repo_flow(existing_asset_path=target_asset, source_paths=paths)

    def _ingest_repo_flow(
        self,
        existing_asset_path: Optional[str],
        source_paths: Optional[list[Path]] = None,
        category_prefix: Optional[str] = None,
    ) -> None:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return
        if source_paths is None:
            if self.hub.config.fast_drop:
                files, _ = QFileDialog.getOpenFileNames(self, "Select File(s) to Ingest into repo/")
                if not files:
                    return
                resolved_paths = [Path(f) for f in files]
            else:
                resolved_paths = []
        else:
            resolved_paths = [p for p in source_paths if p.is_file()]
            if not resolved_paths:
                return

        def _suggest_for_file(sf: Path) -> str:
            auto_path, _, slug = self.workspace._auto_route_repo_asset_path(
                self.active_snapshot, sf, explicit_asset_path=existing_asset_path
            )
            if not existing_asset_path and category_prefix:
                return f"{category_prefix.strip('/')}/{slug}"
            return auto_path

        if self.hub.config.fast_drop:
            for src_path in resolved_paths:
                suggested_path = _suggest_for_file(src_path)
                asset, _ = self.workspace.ingest_repo_asset(
                    project_root=self.active_project_root,
                    component_path=self.active_component_path,
                    source_file=src_path,
                    asset_path=suggested_path,
                )
                self.selected_repo_asset_path = asset.asset_path
            self._refresh_component_triad()
            self.show_status_message(f"✓ Ingested {len(resolved_paths)} repo file(s)")
            return

        suggested_list = [_suggest_for_file(p) for p in resolved_paths]
        suggested_path = (
            suggested_list[0]
            if suggested_list
            else (existing_asset_path or (f"{category_prefix}/" if category_prefix else ""))
        )
        existing_asset = self.active_snapshot.repo_assets.get(suggested_path)
        next_v = f"v{(existing_asset.latest_version + 1) if existing_asset else 1:03d}"
        categories = sorted(
            set(self.active_snapshot.repo_routes.keys())
            | {a.category for a in self.active_snapshot.repo_assets.values() if a.category}
        )
        existing_map = {k: v.latest_version for k, v in self.active_snapshot.repo_assets.items()}

        dlg = SmartIngestDialog(
            self,
            source_file=resolved_paths[0] if resolved_paths else None,
            suggested_asset_path=suggested_path,
            target_version_label=next_v,
            default_author=existing_asset.author if existing_asset else self.hub.config.author_name,
            source_files=resolved_paths or None,
            categories=categories,
            existing_assets=existing_map,
            lock_asset_name=bool(existing_asset_path),
            suggested_paths=suggested_list or None,
        )
        if dlg.exec() == SmartIngestDialog.DialogCode.Accepted:
            count = 0
            for entry in dlg.get_batch_entries():
                if not entry.get("source_file"):
                    continue
                asset, _ = self.workspace.ingest_repo_asset(
                    project_root=self.active_project_root,
                    component_path=self.active_component_path,
                    source_file=Path(entry["source_file"]),  # type: ignore[arg-type]
                    asset_path=str(entry["asset_path"]),
                    author=str(entry["author"]),
                    source_info=str(entry["source_info"]),
                    license_info=str(entry["license_info"]),
                    tags=list(entry["tags"]),  # type: ignore[arg-type]
                    comment=str(entry["comment"]),
                )
                self.selected_repo_asset_path = asset.asset_path
                count += 1
            self._refresh_component_triad()
            if count:
                self.show_status_message(f"✓ Ingested {count} file(s) into repo/")

    def _action_publish_deliverable(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        default_deliv = self.selected_deliverable_path if self.selected_deliverable_path and self.selected_deliverable_path != "__legacy__" else "main"
        if self.hub.config.fast_drop:
            files, _ = QFileDialog.getOpenFileNames(self, "Select Exported Deliverable File(s) to Publish")
            if not files:
                return
            self._publish_flow([Path(f) for f in files], default_deliverable=default_deliv)
            return
        self._publish_flow([], default_deliverable=default_deliv)

    def _action_publish_from_selected_working(self) -> None:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return
        selected_w = self._selected_working_version()
        default_deliv = self.selected_deliverable_path if self.selected_deliverable_path and self.selected_deliverable_path != "__legacy__" else "main"
        if self.hub.config.fast_drop:
            files, _ = QFileDialog.getOpenFileNames(self, "Select Exported Deliverable File(s) for Working Version")
            if not files:
                return
            self._publish_flow(
                [Path(f) for f in files],
                default_deliverable=default_deliv,
                default_working_version=selected_w,
            )
            return
        self._publish_flow(
            [],
            default_deliverable=default_deliv,
            default_working_version=selected_w,
        )

    def _handle_published_deliverable_drop(self, paths: list[Path], target_item: object) -> None:
        default_deliv = "main"
        if isinstance(target_item, QTreeWidgetItem):
            if target_item.data(0, Qt.ItemDataRole.UserRole + 1) != "deliv_category":
                raw = str(target_item.data(0, Qt.ItemDataRole.UserRole) or "")
                if raw and raw != "__legacy__":
                    default_deliv = raw
        self._publish_flow([p for p in paths if p.is_file()], default_deliverable=default_deliv)

    def _handle_published_version_drop(self, paths: list[Path], target_item: object) -> None:
        default_deliv = (
            self.selected_deliverable_path
            if self.selected_deliverable_path and self.selected_deliverable_path != "__legacy__"
            else "main"
        )
        target_ver: Optional[int] = None
        if isinstance(target_item, QTreeWidgetItem):
            data = target_item.data(0, Qt.ItemDataRole.UserRole)
            if isinstance(data, tuple) and len(data) >= 3 and data[1] != "__legacy__":
                default_deliv = str(data[1])
                target_ver = int(data[2])
        self._publish_flow(
            [p for p in paths if p.is_file()],
            default_deliverable=default_deliv,
            default_target_version=target_ver,
        )

    def _publish_flow(
        self,
        source_files: list[Path],
        default_deliverable: str = "main",
        default_working_version: Optional[int] = None,
        default_target_version: Optional[int] = None,
    ) -> None:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return

        if self.hub.config.fast_drop:
            if not source_files:
                return
            deliv_path = self.workspace.normalize_deliverable_name(default_deliverable)
            bundle = self.workspace.publish_version(
                project_root=self.active_project_root,
                component_path=self.active_component_path,
                source_files=source_files,
                source_working_version=default_working_version,
                deliverable_name=deliv_path,
                target_version=default_target_version,
            )
            self.selected_deliverable_path = deliv_path
            self._refresh_component_triad()
            self.show_status_message(f"✓ Published {deliv_path}/{bundle.version_label}")
            return

        working_vers = [w.version for w in self.active_snapshot.working_versions]
        existing_delivs = {
            k: [v.version for v in d.versions]
            for k, d in self.active_snapshot.published_deliverables.items()
        }
        tt = self.hub.template_manager.get_task_template(self.active_snapshot.task_template)
        comp_leaf = self.active_component_path.split("/")[-1]
        dlg = PublishDeliverableDialog(
            self,
            initial_files=source_files,
            component_leaf=comp_leaf,
            working_versions=working_vers,
            existing_deliverables=existing_delivs,
            publish_formats=tt.publish_formats,
            default_deliverable=default_deliverable,
            default_working_version=default_working_version,
            default_target_version=default_target_version,
        )
        if dlg.exec() == PublishDeliverableDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            deliv_name = str(vals["deliverable_name"])
            bundle = self.workspace.publish_version(
                project_root=self.active_project_root,
                component_path=self.active_component_path,
                source_working_version=int(vals["source_working_version"]),
                comment=str(vals["comment"]),
                tags=list(vals["tags"]),  # type: ignore[arg-type]
                deliverable_name=deliv_name,
                publish_items=list(vals["publish_items"]),  # type: ignore[arg-type]
                target_version=vals["target_version"],  # type: ignore[arg-type]
                keep_original_names=bool(vals["keep_original_names"]),
            )
            self.selected_deliverable_path = deliv_name
            self._refresh_component_triad()
            self.show_status_message(f"✓ Published {deliv_name}/{bundle.version_label}")

    def _action_open_publish_export_folder(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        deliv = (
            self.selected_deliverable_path
            if self.selected_deliverable_path and self.selected_deliverable_path != "__legacy__"
            else "main"
        )
        deliv_path, ver_num, export_dir = self.workspace.prepare_publish_export_folder(
            self.active_project_root,
            self.active_component_path,
            deliverable_name=deliv,
        )
        self.selected_deliverable_path = deliv_path
        if sys.platform == "win32" and hasattr(os, "startfile"):
            os.startfile(str(export_dir))
            self.show_status_message(f"Opened export folder {deliv_path}/v{ver_num:03d}/ — click '2. Sync Exports' after rendering")
        else:
            QMessageBox.information(
                self,
                "Export Folder Ready",
                f"Export multiple file types into:\n{export_dir}\n\nThen click '2. Sync Exports' to register published/{deliv_path}/v{ver_num:03d}/.",
            )

    def _action_sync_published_folder(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        deliv = (
            self.selected_deliverable_path
            if self.selected_deliverable_path and self.selected_deliverable_path != "__legacy__"
            else "main"
        )
        bundle = self.workspace.sync_published_folder(
            self.active_project_root,
            self.active_component_path,
            deliverable_name=deliv,
        )
        if bundle is None:
            QMessageBox.information(
                self,
                "Sync Published Folder",
                f"No new exported files found in published/{deliv}/.",
            )
            return
        self._refresh_component_triad()
        self.show_status_message(f"✓ Synced published/{deliv}/{bundle.version_label}/")

    def _action_primary_open(self) -> None:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return
        tab_idx = self.triad_tabs.currentIndex()
        if tab_idx == 1:
            asset = self.active_snapshot.repo_assets.get(self.selected_repo_asset_path)
            ver = self._selected_repo_version()
            if asset and ver is not None:
                bundle = next((v for v in asset.versions if v.version == ver), None)
                if bundle and bundle.primary_file:
                    self._launch_arbitrary_file(self.active_project_root / self.active_component_path / bundle.primary_file)
                    return
        elif tab_idx == 2:
            target = self._selected_published_target()
            if target is not None:
                kind, dpath, ver, rel_path = target
                if kind == "file" and rel_path:
                    self._launch_arbitrary_file(self.active_project_root / self.active_component_path / rel_path)
                    return
                deliv = self.active_snapshot.published_deliverables.get(dpath)
                versions = self.active_snapshot.published_versions if dpath == "__legacy__" else (deliv.versions if deliv else [])
                bundle = next((v for v in versions if v.version == ver), None)
                if bundle and bundle.primary_file:
                    self._launch_arbitrary_file(self.active_project_root / self.active_component_path / bundle.primary_file)
                    return
        self._action_launch_working(self._selected_working_version())

    def _launch_arbitrary_file(self, target: Path) -> None:
        if not target.is_file():
            QMessageBox.warning(self, "Open File", f"File not found on disk:\n{target}")
            return
        from obol.plugins.base import launch_file_with_profile

        prof = self.hub.detect_software_for_extension(target.suffix)
        launch_file_with_profile(target, prof)

    def _action_launch_working(self, version: Optional[int] = None, new_tab: bool = False) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        ver = version if version is not None else self._selected_working_version()
        if (
            self.active_snapshot
            and self.active_snapshot.software == "reaper"
            and self.reaper_bridge.is_connected()
        ):
            bundle = (
                next((v for v in self.active_snapshot.working_versions if v.version == ver), None)
                if ver is not None
                else (self.active_snapshot.working_versions[-1] if self.active_snapshot.working_versions else None)
            )
            if bundle and bundle.primary_file:
                target = self.active_project_root / self.active_component_path / bundle.primary_file
                if target.is_file():
                    try:
                        self.reaper_bridge.open_project(target, new_tab=new_tab)
                        tab_msg = " in new REAPER tab" if new_tab else " in running REAPER"
                        self.show_status_message(f"✓ Opened {bundle.version_label}{tab_msg}")
                        return
                    except Exception:
                        pass
        try:
            self.workspace.launch_working_version(
                self.active_project_root,
                self.active_component_path,
                version=ver,
            )
        except Exception as exc:
            QMessageBox.warning(self, "Launch", str(exc))

    def _drag_path_for_working_item(self, item: QTreeWidgetItem) -> Optional[Path]:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return None
        raw = item.data(0, Qt.ItemDataRole.UserRole)
        if raw is None:
            return None
        ver = int(raw)
        bundle = next((v for v in self.active_snapshot.working_versions if v.version == ver), None)
        if bundle and bundle.primary_file:
            return self.active_project_root / self.active_component_path / bundle.primary_file
        return None

    def _drag_path_for_repo_item(self, item: QTreeWidgetItem) -> Optional[Path]:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return None
        raw = item.data(0, Qt.ItemDataRole.UserRole)
        if raw is None or not self.selected_repo_asset_path:
            return None
        asset = self.active_snapshot.repo_assets.get(self.selected_repo_asset_path)
        if not asset:
            return None
        bundle = next((v for v in asset.versions if v.version == int(raw)), None)
        if bundle and bundle.primary_file:
            return self.active_project_root / self.active_component_path / bundle.primary_file
        return None

    def _drag_path_for_published_item(self, item: QTreeWidgetItem) -> Optional[Path]:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return None
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(data, tuple) or len(data) < 3:
            return None
        kind, dpath, ver = str(data[0]), str(data[1]), int(data[2])
        if kind == "file" and len(data) > 3 and data[3]:
            return self.active_project_root / self.active_component_path / str(data[3])
        deliv = self.active_snapshot.published_deliverables.get(dpath)
        versions = self.active_snapshot.published_versions if dpath == "__legacy__" else (deliv.versions if deliv else [])
        bundle = next((v for v in versions if v.version == ver), None)
        if bundle and bundle.primary_file:
            return self.active_project_root / self.active_component_path / bundle.primary_file
        return None

    def _action_import_files_into_reaper(self, paths: list[Path], mode: str = "new_track") -> None:
        existing = [p for p in paths if p.is_file()]
        if not existing:
            QMessageBox.warning(self, "Import into REAPER", "Target file(s) are not available locally on disk.")
            return
        if not self.reaper_bridge.is_connected():
            QMessageBox.information(
                self,
                "REAPER Bridge Offline",
                "REAPER Bridge is not currently running.\n\n"
                "Open REAPER (with Obol Bridge installed via ⚙ Settings → REAPER Integration...), "
                "or drag the file directly from this list into REAPER's Arrange view.",
            )
            return
        try:
            res = self.reaper_bridge.insert_media(existing, mode=mode)
            self.show_status_message(f"✓ Imported {res.get('inserted', len(existing))} file(s) into REAPER")
        except Exception as exc:
            QMessageBox.warning(self, "Import into REAPER", str(exc))

    def _action_export_from_reaper(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        active_rpp = self.reaper_bridge.get_state().active_rpp
        dlg = ReaperExportPublishDialog(
            self,
            self.hub,
            self.workspace,
            self.reaper_bridge,
            active_rpp=active_rpp,
            default_project=self.active_project_name,
            default_component=self.active_component_path,
        )
        if dlg.exec() == ReaperExportPublishDialog.DialogCode.Accepted:
            self._refresh_component_triad()
            self.show_status_message("✓ Exported & published from REAPER")

    def _action_load_files_into_resolve(self, paths: list[Path], append_to_timeline: bool = False) -> None:
        existing = [p for p in paths if p.is_file()]
        if not existing:
            QMessageBox.warning(self, "Load into Resolve", "Target file(s) are not available locally on disk.")
            return
        if not self.resolve_bridge.is_connected():
            QMessageBox.information(
                self,
                "DaVinci Resolve Offline",
                "DaVinci Resolve is not currently connected.\n\n"
                "Inside Resolve, run Workspace → Scripts → Utility → Obol - Load Media into Media Pool "
                "(or start Obol - Live Bridge in Resolve for direct GUI push).",
            )
            return
        try:
            bin_path = f"Obol/{self.active_component_path}" if self.active_component_path else ""
            res = self.resolve_bridge.import_media(
                existing,
                bin_path=bin_path,
                append_to_timeline=append_to_timeline,
            )
            self.show_status_message(f"✓ Loaded {res.get('imported', len(existing))} clip(s) into Resolve Media Pool")
        except Exception as exc:
            QMessageBox.warning(self, "Load into Resolve", str(exc))

    def _action_export_from_resolve(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        dlg = ResolveExportPublishDialog(
            self,
            self.hub,
            self.workspace,
            self.resolve_bridge,
            default_project=self.active_project_name,
            default_component=self.active_component_path,
        )
        if dlg.exec() == ResolveExportPublishDialog.DialogCode.Accepted:
            self._refresh_component_triad()
            self.show_status_message("✓ Exported & published from DaVinci Resolve")

    def _on_working_item_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        raw = item.data(0, Qt.ItemDataRole.UserRole)
        if raw is not None:
            self._action_launch_working(int(raw))

    def _on_repo_version_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return
        raw = item.data(0, Qt.ItemDataRole.UserRole)
        if raw is None:
            return
        ver = int(raw)
        asset = self.active_snapshot.repo_assets.get(self.selected_repo_asset_path)
        if not asset:
            return
        bundle = next((v for v in asset.versions if v.version == ver), None)
        if bundle and bundle.primary_file:
            self._launch_arbitrary_file(self.active_project_root / self.active_component_path / bundle.primary_file)

    def _on_published_item_double_clicked(self, item: QTreeWidgetItem, _col: int) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if isinstance(data, tuple) and data[0] == "file":
            target = self.active_project_root / self.active_component_path / str(data[3])
            self._launch_arbitrary_file(target)

    def _action_vup_and_launch(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        src_v = self._selected_working_version() if self.triad_tabs.currentIndex() == 0 else None
        bundle = self.workspace.version_up_working(
            self.active_project_root,
            self.active_component_path,
            source_version=src_v,
        )
        self._refresh_component_triad()
        self._action_launch_working(bundle.version)
        self.show_status_message(f"✓ Created & launched working/{bundle.version_label}")

    def _action_collect_external_media(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        ver = self._selected_working_version() if self.triad_tabs.currentIndex() == 0 else None
        externals = self.workspace.scan_external_dependencies(
            self.active_project_root, self.active_component_path, working_version=ver
        )
        if not externals:
            QMessageBox.information(
                self,
                "Reaper Dependency Audit",
                "All audio and MIDI files in the .rpp are already inside repo/!",
            )
            return
        msg = "\n".join(f"• {d.raw_reference} → repo/{d.suggested_category}/{d.suggested_asset_name}/v001/" for d in externals)
        ans = QMessageBox.question(
            self,
            "Collect External Media into Repo",
            f"Found {len(externals)} external reference(s) outside repo/:\n\n{msg}\n\nIngest all into repo/ and relink .rpp?",
        )
        if ans == QMessageBox.StandardButton.Yes:
            collected = self.workspace.collect_external_dependencies(
                self.active_project_root, self.active_component_path, working_version=ver
            )
            self._refresh_component_triad()
            self.show_status_message(f"✓ Collected {len(collected)} media file(s) into repo/")

    def _selected_item_folder(self) -> Optional[Path]:
        if not self.active_project_root or not self.active_component_path:
            return self.active_project_root
        comp_dir = self.active_project_root / self.active_component_path
        snap = self.active_snapshot
        if snap is None:
            return comp_dir
        tab_idx = self.triad_tabs.currentIndex()
        if tab_idx == 0:
            ver = self._selected_working_version()
            bundle = next((v for v in snap.working_versions if v.version == ver), None) if ver is not None else None
            if bundle:
                candidate = comp_dir / bundle.folder_rel
                if candidate.is_dir():
                    return candidate
        elif tab_idx == 1 and self.selected_repo_asset_path:
            ver = self._selected_repo_version()
            asset = snap.repo_assets.get(self.selected_repo_asset_path)
            if asset and ver is not None:
                bundle = next((v for v in asset.versions if v.version == ver), None)
                if bundle and (comp_dir / bundle.folder_rel).is_dir():
                    return comp_dir / bundle.folder_rel
            asset_dir = comp_dir / "repo" / self.selected_repo_asset_path
            if asset_dir.is_dir():
                return asset_dir
        elif tab_idx == 2:
            target = self._selected_published_target()
            if target is not None:
                _, dpath, ver, _ = target
                deliv = snap.published_deliverables.get(dpath)
                versions = snap.published_versions if dpath == "__legacy__" else (deliv.versions if deliv else [])
                bundle = next((v for v in versions if v.version == ver), None)
                if bundle and (comp_dir / bundle.folder_rel).is_dir():
                    return comp_dir / bundle.folder_rel
            if self.selected_deliverable_path and self.selected_deliverable_path != "__legacy__":
                ddir = comp_dir / "published" / self.selected_deliverable_path
                if ddir.is_dir():
                    return ddir
        return comp_dir

    def _selected_item_path(self) -> Optional[Path]:
        if not self.active_project_root or not self.active_component_path:
            return self.active_project_root
        comp_dir = self.active_project_root / self.active_component_path
        snap = self.active_snapshot
        if snap is not None:
            tab_idx = self.triad_tabs.currentIndex()
            if tab_idx == 0:
                ver = self._selected_working_version()
                bundle = next((v for v in snap.working_versions if v.version == ver), None) if ver is not None else None
                if bundle and bundle.primary_file:
                    return comp_dir / bundle.primary_file
            elif tab_idx == 1 and self.selected_repo_asset_path:
                ver = self._selected_repo_version()
                asset = snap.repo_assets.get(self.selected_repo_asset_path)
                bundle = next((v for v in asset.versions if v.version == ver), None) if (asset and ver is not None) else None
                if bundle and bundle.primary_file:
                    return comp_dir / bundle.primary_file
            elif tab_idx == 2:
                target = self._selected_published_target()
                if target is not None and target[0] == "file" and target[3]:
                    return comp_dir / target[3]
        return self._selected_item_folder()

    def _action_open_in_explorer(self) -> None:
        folder = self._selected_item_folder()
        if folder is not None:
            _open_folder_in_os(folder)

    def _action_copy_selected_path(self) -> None:
        path = self._selected_item_path()
        if path is None:
            return
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(str(path))
            self.show_status_message(f"✓ Copied path: {path.name}")

    def _selected_vault_glob(self) -> str:
        folder = self._selected_item_folder()
        if self.active_project_root and folder is not None:
            try:
                rel = folder.resolve().relative_to(self.active_project_root.resolve()).as_posix()
                if rel and rel != ".":
                    return f"{rel}/**"
            except ValueError:
                pass
        return f"{self.active_component_path}/**"

    def _action_checkout_selected(self) -> None:
        if not self.active_project_name or not self.active_component_path:
            return
        glob_pat = self._selected_vault_glob()
        self.sync.pull_paths(self.active_project_name, [glob_pat])
        self._refresh_component_triad()
        self.show_status_message(f"✓ Checked out {glob_pat}")

    def _action_offload_selected(self) -> None:
        if not self.active_project_name or not self.active_component_path:
            return
        glob_pat = self._selected_vault_glob()
        self.sync.push_paths(self.active_project_name, [glob_pat], evict_local=True)
        self._refresh_component_triad()
        self.show_status_message(f"✓ Offloaded {glob_pat}")

    def _save_working_as_seed_template(self, ver: int) -> None:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return
        bundle = next((v for v in self.active_snapshot.working_versions if v.version == ver), None)
        if not bundle or not bundle.primary_file:
            return
        src_abs = self.active_project_root / self.active_component_path / bundle.primary_file
        if not src_abs.is_file():
            return
        default_name = f"{self.active_component_path.split('/')[-1]}_seed{src_abs.suffix}"
        tpl_name, ok = QInputDialog.getText(
            self,
            "Save as Working Seed Template",
            "Seed filename in ~/.obol/templates/files/:",
            text=default_name,
        )
        if ok and tpl_name.strip():
            dest = self.hub.template_manager.import_seed_file(src_abs, tpl_name.strip())
            self.show_status_message(f"✓ Saved seed template '{dest.name}'")

    def _capture_component_as_template(self, comp_path: str) -> None:
        if not self.active_project_root:
            return
        default_name = f"{comp_path.split('/')[-1]}_task"
        tpl_name, ok = QInputDialog.getText(
            self,
            "Capture Component Template",
            "Save component configuration as Task Template named:",
            text=default_name,
        )
        if ok and tpl_name.strip():
            self.hub.template_manager.capture_template(
                "task",
                self.active_project_root / comp_path,
                tpl_name.strip(),
            )
            self.show_status_message(f"✓ Captured template '{tpl_name.strip()}'")

    # ---------------- Delete Handlers & Rich Context Menus ----------------

    def _ctx_component_tree(self, pos: QPoint) -> None:
        item = self.component_tree.itemAt(pos)
        menu = QMenu(self)
        if item is None:
            act_add = menu.addAction("+ Add Component...")
            if menu.exec(self.component_tree.viewport().mapToGlobal(pos)) == act_add:
                self._prompt_add_component("")
            return

        kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
        raw_path = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
        if kind == "group":
            act_add = menu.addAction(f"+ Add Component in '{raw_path}/'...")
            act_open = menu.addAction("Open Group Folder in Explorer")
            chosen = menu.exec(self.component_tree.viewport().mapToGlobal(pos))
            if chosen == act_add:
                self._prompt_add_component(raw_path)
            elif chosen == act_open and self.active_project_root:
                _open_folder_in_os(self.active_project_root / raw_path)
            return

        act_open = menu.addAction("Open Folder in Explorer")
        act_copy = menu.addAction("Copy Folder Path")
        act_cap = menu.addAction("Capture as Component Template...")
        menu.addSeparator()
        act_del = menu.addAction("Delete Component...")
        chosen = menu.exec(self.component_tree.viewport().mapToGlobal(pos))
        if chosen == act_open and self.active_project_root:
            _open_folder_in_os(self.active_project_root / raw_path)
        elif chosen == act_copy and self.active_project_root:
            cb = QApplication.clipboard()
            if cb is not None:
                cb.setText(str(self.active_project_root / raw_path))
                self.show_status_message("✓ Copied component path")
        elif chosen == act_cap:
            self._capture_component_as_template(raw_path)
        elif chosen == act_del:
            self._action_delete_component()

    def _ctx_working_list(self, pos: QPoint) -> None:
        item = self.working_list.itemAt(pos)
        if item is None:
            return
        ver = int(item.data(0, Qt.ItemDataRole.UserRole))
        menu = QMenu(self)
        act_open = menu.addAction(f"Open v{ver:03d}")
        act_open_tab = (
            menu.addAction(f"Open v{ver:03d} in New REAPER Tab")
            if (self.active_snapshot and self.active_snapshot.software == "reaper")
            else None
        )
        act_vup = menu.addAction(f"Version Up from v{ver:03d}...")
        act_pub = menu.addAction(f"Publish from v{ver:03d}...")
        act_seed = menu.addAction("Save as Working Seed Template...")
        menu.addSeparator()
        act_exp = menu.addAction("Open Version Folder in Explorer")
        act_copy = menu.addAction("Copy Primary File Path")
        menu.addSeparator()
        act_del = menu.addAction(f"Delete working/v{ver:03d}...")
        chosen = menu.exec(self.working_list.viewport().mapToGlobal(pos))
        if chosen == act_open:
            self._action_launch_working(ver)
        elif act_open_tab is not None and chosen == act_open_tab:
            self._action_launch_working(ver, new_tab=True)
        elif chosen == act_vup:
            self._action_version_up_working(source_version=ver)
        elif chosen == act_pub:
            self._publish_flow([], default_deliverable="main", default_working_version=ver)
        elif chosen == act_seed:
            self._save_working_as_seed_template(ver)
        elif chosen == act_exp:
            self._action_open_in_explorer()
        elif chosen == act_copy:
            self._action_copy_selected_path()
        elif chosen == act_del:
            self._action_delete_working_version()

    def _ctx_repo_asset_tree(self, pos: QPoint) -> None:
        item = self.repo_asset_tree.itemAt(pos)
        if item is None:
            return
        if item.data(0, Qt.ItemDataRole.UserRole + 1) == "repo_category":
            cat = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
            menu = QMenu(self)
            act_ing = menu.addAction(f"+ Ingest into '{cat}/'...")
            if menu.exec(self.repo_asset_tree.viewport().mapToGlobal(pos)) == act_ing:
                self._ingest_repo_flow(existing_asset_path=None, category_prefix=cat)
            return
        menu = QMenu(self)
        act_new_v = menu.addAction("+ Ingest New Version...")
        act_exp = menu.addAction("Open Asset Folder in Explorer")
        menu.addSeparator()
        act_del = menu.addAction("Delete Named Repo Asset...")
        chosen = menu.exec(self.repo_asset_tree.viewport().mapToGlobal(pos))
        if chosen == act_new_v:
            self._action_ingest_repo_version()
        elif chosen == act_exp:
            self._action_open_in_explorer()
        elif chosen == act_del:
            self._action_delete_repo_asset()

    def _ctx_repo_version_list(self, pos: QPoint) -> None:
        item = self.repo_version_list.itemAt(pos)
        if item is None:
            return
        ver = int(item.data(0, Qt.ItemDataRole.UserRole))
        target_path = self._drag_path_for_repo_item(item)
        menu = QMenu(self)
        act_open = menu.addAction(f"Open v{ver:03d} File")
        act_reap_new = menu.addAction("Import into REAPER (New Track)")
        act_reap_cur = menu.addAction("Import into REAPER (At Cursor)")
        act_dvr_pool = menu.addAction("Load into Resolve Media Pool")
        act_dvr_tl = menu.addAction("Load into Resolve + Timeline")
        act_new_v = menu.addAction("+ Ingest New Version...")
        act_exp = menu.addAction("Open Version Folder in Explorer")
        act_copy = menu.addAction("Copy File Path")
        menu.addSeparator()
        act_del = menu.addAction(f"Delete Repo v{ver:03d}...")
        chosen = menu.exec(self.repo_version_list.viewport().mapToGlobal(pos))
        if chosen == act_open:
            self._on_repo_version_double_clicked(item, 0)
        elif chosen == act_reap_new and target_path:
            self._action_import_files_into_reaper([target_path], mode="new_track")
        elif chosen == act_reap_cur and target_path:
            self._action_import_files_into_reaper([target_path], mode="cursor_track")
        elif chosen == act_dvr_pool and target_path:
            self._action_load_files_into_resolve([target_path], append_to_timeline=False)
        elif chosen == act_dvr_tl and target_path:
            self._action_load_files_into_resolve([target_path], append_to_timeline=True)
        elif chosen == act_new_v:
            self._action_ingest_repo_version()
        elif chosen == act_exp:
            self._action_open_in_explorer()
        elif chosen == act_copy:
            self._action_copy_selected_path()
        elif chosen == act_del:
            self._action_delete_repo_version()

    def _ctx_published_deliverable_tree(self, pos: QPoint) -> None:
        item = self.published_deliverable_tree.itemAt(pos)
        if item is None or item.data(0, Qt.ItemDataRole.UserRole + 1) == "deliv_category":
            return
        menu = QMenu(self)
        act_pub = menu.addAction("+ Publish New Release...")
        act_exp = menu.addAction("Open Deliverable Folder in Explorer")
        menu.addSeparator()
        act_del = menu.addAction("Delete Named Deliverable...")
        chosen = menu.exec(self.published_deliverable_tree.viewport().mapToGlobal(pos))
        if chosen == act_pub:
            self._action_publish_deliverable()
        elif chosen == act_exp:
            self._action_open_in_explorer()
        elif chosen == act_del:
            self._action_delete_published_deliverable()

    def _ctx_published_version_tree(self, pos: QPoint) -> None:
        item = self.published_version_tree.itemAt(pos)
        if item is None:
            return
        data = item.data(0, Qt.ItemDataRole.UserRole)
        target_path = self._drag_path_for_published_item(item)
        menu = QMenu(self)
        act_open = menu.addAction("Open File") if (isinstance(data, tuple) and data[0] == "file") else None
        act_reap_new = menu.addAction("Import into REAPER (New Track)") if target_path else None
        act_reap_cur = menu.addAction("Import into REAPER (At Cursor)") if target_path else None
        act_dvr_pool = menu.addAction("Load into Resolve Media Pool") if target_path else None
        act_dvr_tl = menu.addAction("Load into Resolve + Timeline") if target_path else None
        act_add = menu.addAction("+ Attach More Files to Release...")
        act_exp = menu.addAction("Open Release Folder in Explorer")
        act_copy = menu.addAction("Copy Path")
        menu.addSeparator()
        act_del = menu.addAction("Delete Published Release...")
        chosen = menu.exec(self.published_version_tree.viewport().mapToGlobal(pos))
        if chosen is not None and chosen == act_open:
            self._on_published_item_double_clicked(item, 0)
        elif act_reap_new is not None and chosen == act_reap_new and target_path:
            self._action_import_files_into_reaper([target_path], mode="new_track")
        elif act_reap_cur is not None and chosen == act_reap_cur and target_path:
            self._action_import_files_into_reaper([target_path], mode="cursor_track")
        elif act_dvr_pool is not None and chosen == act_dvr_pool and target_path:
            self._action_load_files_into_resolve([target_path], append_to_timeline=False)
        elif act_dvr_tl is not None and chosen == act_dvr_tl and target_path:
            self._action_load_files_into_resolve([target_path], append_to_timeline=True)
        elif chosen == act_add and isinstance(data, tuple) and len(data) >= 3 and data[1] != "__legacy__":
            self._publish_flow([], default_deliverable=str(data[1]), default_target_version=int(data[2]))
        elif chosen == act_exp:
            self._action_open_in_explorer()
        elif chosen == act_copy:
            self._action_copy_selected_path()
        elif chosen == act_del:
            self._action_delete_published_version()

    def _action_delete_focused_widget(self) -> None:
        focus = self.focusWidget()
        if focus is self.component_tree:
            self._action_delete_component()
        elif focus is self.working_list:
            self._action_delete_working_version()
        elif focus is self.repo_asset_tree:
            self._action_delete_repo_asset()
        elif focus is self.repo_version_list:
            self._action_delete_repo_version()
        elif focus is self.published_deliverable_tree:
            self._action_delete_published_deliverable()
        elif focus is self.published_version_tree:
            self._action_delete_published_version()

    def _action_delete_active_selection(self) -> None:
        tab_idx = self.triad_tabs.currentIndex()
        if tab_idx == 0:
            if self._selected_working_version() is not None:
                self._action_delete_working_version()
            else:
                self._action_delete_component()
        elif tab_idx == 1:
            if self._selected_repo_version() is not None:
                self._action_delete_repo_version()
            elif self.selected_repo_asset_path:
                self._action_delete_repo_asset()
        else:
            if self._selected_published_target() is not None:
                self._action_delete_published_version()
            elif self.selected_deliverable_path and self.selected_deliverable_path != "__legacy__":
                self._action_delete_published_deliverable()

    def _action_delete_component(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        comp = self.active_component_path
        ans = QMessageBox.question(
            self,
            "Delete Component",
            f"Delete component '{comp}' and all its working, repo, and published files on disk?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.workspace.delete_component(self.active_project_root, comp, delete_files=True)
            self.active_component_path = ""
            self._load_active_project()
            self.show_status_message(f"Deleted component '{comp}'")

    def _action_delete_working_version(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        ver = self._selected_working_version()
        if ver is None:
            return
        ans = QMessageBox.question(
            self,
            "Delete Working Version",
            f"Delete working/v{ver:03d}/ from '{self.active_component_path}'?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.workspace.delete_working_version(self.active_project_root, self.active_component_path, ver)
            self._refresh_component_triad()
            self.show_status_message(f"Deleted working/v{ver:03d}")

    def _action_delete_repo_asset(self) -> None:
        if not self.active_project_root or not self.active_component_path or not self.selected_repo_asset_path:
            return
        apath = self.selected_repo_asset_path
        ans = QMessageBox.question(
            self,
            "Delete Repo Asset",
            f"Delete named Repo Asset 'repo/{apath}/' and all its versions?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.workspace.delete_repo_asset(self.active_project_root, self.active_component_path, apath)
            self.selected_repo_asset_path = ""
            self._refresh_component_triad()
            self.show_status_message(f"Deleted repo/{apath}")

    def _action_delete_repo_version(self) -> None:
        if not self.active_project_root or not self.active_component_path or not self.selected_repo_asset_path:
            return
        ver = self._selected_repo_version()
        if ver is None:
            return
        apath = self.selected_repo_asset_path
        ans = QMessageBox.question(
            self,
            "Delete Repo Version",
            f"Delete repo/{apath}/v{ver:03d}/?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.workspace.delete_repo_version(self.active_project_root, self.active_component_path, apath, ver)
            self._refresh_component_triad()
            self.show_status_message(f"Deleted repo/{apath}/v{ver:03d}")

    def _action_delete_published_deliverable(self) -> None:
        if not self.active_project_root or not self.active_component_path or not self.selected_deliverable_path:
            return
        dpath = self.selected_deliverable_path
        if dpath == "__legacy__":
            return
        ans = QMessageBox.question(
            self,
            "Delete Published Deliverable",
            f"Delete deliverable 'published/{dpath}/' and all its releases?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.workspace.delete_published_deliverable(self.active_project_root, self.active_component_path, dpath)
            self.selected_deliverable_path = ""
            self._refresh_component_triad()
            self.show_status_message(f"Deleted published/{dpath}")

    def _action_delete_published_version(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        target = self._selected_published_target()
        if target is None:
            return
        _, dpath, ver, _ = target
        ans = QMessageBox.question(
            self,
            "Delete Published Version",
            f"Delete published/{dpath}/v{ver:03d}/?\n\nThis cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if ans == QMessageBox.StandardButton.Yes:
            self.workspace.delete_published_version(self.active_project_root, self.active_component_path, dpath, ver)
            self._refresh_component_triad()
            self.show_status_message(f"Deleted published/{dpath}/v{ver:03d}")

    def open_new_project_wizard(self) -> None:
        dlg = NewProjectWizardDialog(self, self.hub, self.workspace)
        if dlg.exec() == NewProjectWizardDialog.DialogCode.Accepted and dlg.created_project_name:
            self.active_project_name = dlg.created_project_name
            self.active_component_path = ""
            self.refresh_all()
            self.show_status_message(f"✓ Created project '{dlg.created_project_name}'")

    def open_software_settings_dialog(self) -> None:
        dlg = SoftwareSettingsDialog(self, self.hub)
        dlg.exec()
        self.refresh_all()

    def open_integrations_manager_dialog(self, initial_dcc: str = "") -> None:
        dlg = IntegrationsManagerDialog(
            self,
            self.hub,
            initial_dcc=initial_dcc,
            allow_resolve_studio_api=(QApplication.platformName() != "offscreen"),
        )
        dlg.exec()
        self.refresh_all()

    def open_reaper_integration_dialog(self) -> None:
        self.open_integrations_manager_dialog(initial_dcc="reaper")

    def open_resolve_integration_dialog(self) -> None:
        self.open_integrations_manager_dialog(initial_dcc="resolve")

    def open_template_manager_dialog(self) -> None:
        dlg = TemplateManagerDialog(
            self,
            self.hub,
            active_project_root=self.active_project_root,
            active_component_path=self.active_component_path,
        )
        dlg.exec()
        self.refresh_all()

    def open_projects_and_vault_dialog(self) -> None:
        dlg = ProjectAndVaultManagerDialog(self, self.hub, self.sync)
        dlg.exec()
        if dlg.selected_project_name:
            self.active_project_name = dlg.selected_project_name
            self.active_component_path = ""
        elif self.active_project_name and self.hub.get_project(self.active_project_name) is None:
            self.active_project_name = ""
            self.active_component_path = ""
        self.refresh_all()

    def open_credits_dialog(self) -> None:
        if not self.active_project_root:
            return
        rows = self.workspace.generate_credits_report(self.active_project_root)
        dlg = CreditsReportDialog(self, self.active_project_name, rows)
        dlg.exec()


def launch_gui(
    hub: Optional[ObolHub] = None,
    initial_project: str = "",
    initial_component: str = "",
) -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    from PySide6.QtGui import QIcon

    icon_path = Path(__file__).parent / "assets" / "obol_icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    win = ObolMainWindow(
        hub=hub,
        initial_project=initial_project,
        initial_component=initial_component,
    )
    win.show()
    return app.exec()
