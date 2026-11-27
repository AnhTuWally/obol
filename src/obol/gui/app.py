"""Main 3-Pane PySide6 Desktop GUI for Obol."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QPoint, QSettings, QSize, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QFont, QKeySequence, QPixmap, QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,
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
    QListWidget,
    QListWidgetItem,
    QMainWindow,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
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
from obol.gui.components import (
    ShortcutsDialog,
    attach_search_icon,
    confirm_destructive_action,
    open_folder_in_os,
    repolish,
    run_with_busy_state,
)
from obol.gui.dialogs import (
    AddComponentDialog,
    CreditsReportDialog,
    IngestWorkingDialog,
    NewProjectWizardDialog,
    ProjectAndVaultManagerDialog,
    PublishDeliverableDialog,
    SaveSeedTemplateDialog,
    SmartIngestDialog,
    SoftwareSettingsDialog,
    TemplateManagerDialog,
)
from obol.gui.drop_widgets import DropTreeWidget, install_tab_hover_switch
from obol.gui.filmcraft_dialogs import FilmCraftExportPublishDialog, FilmCraftImportDialog
from obol.gui.icons import get_icon
from obol.gui.integrations_dialog import IntegrationsManagerDialog
from obol.gui.metadata_widgets import CustomMetadataEditor
from obol.gui.reaper_dialogs import ReaperExportPublishDialog, ReaperImportDialog
from obol.gui.resolve_dialogs import ResolveExportPublishDialog
from obol.gui.theme import apply_studio_dark_theme
from obol.gui.tokens import (
    ACCENT_LIGHT,
    DANGER_TEXT,
    ICON_BTN_SIZE,
    ICON_SM,
    TEXT_MUTED,
    TEXT_SECONDARY,
    WARNING_TEXT,
)
from obol.plugins.filmcraft_bridge import FilmCraftBridgeClient
from obol.plugins.filmcraft_installer import FilmCraftInstaller
from obol.plugins.integrations import DCCIntegrationManager
from obol.plugins.reaper_bridge import ReaperBridgeClient
from obol.plugins.reaper_installer import ReaperInstaller
from obol.plugins.resolve_bridge import ResolveBridgeClient
from obol.plugins.resolve_installer import ResolveInstaller
from obol.plugins.send_registry import DCCSendContext, DCCSendRegistry, DCCSendResult, ResolvedDCCTarget


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


_repolish = repolish
_open_folder_in_os = open_folder_in_os


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
        self.filmcraft_bridge = FilmCraftBridgeClient(self.hub)
        self.filmcraft_installer = FilmCraftInstaller(self.hub)
        self.integration_manager = DCCIntegrationManager(self.hub, allow_resolve_studio_api=allow_studio)
        self.send_registry = DCCSendRegistry(
            self.hub,
            reaper_bridge=self.reaper_bridge,
            resolve_bridge=self.resolve_bridge,
            filmcraft_bridge=self.filmcraft_bridge,
            allow_resolve_studio_api=allow_studio,
        )

        self.active_project_name: str = initial_project or self.hub.config.last_active_project
        self.active_project_root: Optional[Path] = None
        self.active_component_path: str = initial_component
        self.active_snapshot: Optional[ComponentSnapshot] = None
        self.selected_repo_asset_path: str = ""
        self.selected_deliverable_path: str = ""
        self._selected_group_prefix: str = ""
        self._selected_repo_category: str = ""
        self._selected_deliv_category: str = ""
        self._last_triad_tab_idx: int = 0
        self._last_dcc_target_key: str = ""
        self._cached_dcc_targets: Optional[
            tuple[Optional[ResolvedDCCTarget], list[ResolvedDCCTarget], list[ResolvedDCCTarget]]
        ] = None
        self._meta_loading: bool = False
        self._meta_dirty: bool = False
        self._insp_target_ctx: Optional[tuple[Any, ...]] = None
        self._copied_metadata: Optional[dict[str, Any]] = None

        self._mono_font = QFont("Consolas")
        self._mono_font.setStyleHint(QFont.StyleHint.Monospace)
        self._mono_font.setPointSize(max(9, self.font().pointSize() - 1))

        self._meta_autosave_timer = QTimer(self)
        self._meta_autosave_timer.setSingleShot(True)
        self._meta_autosave_timer.setInterval(500)
        self._meta_autosave_timer.timeout.connect(self._on_autosave_timer_fired)

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
        top_layout.setContentsMargins(14, 7, 14, 7)
        top_layout.setSpacing(8)

        logo_path = Path(__file__).parent / "assets" / "obol_icon.png"
        if logo_path.is_file():
            logo_lbl = QLabel()
            pix = QPixmap(str(logo_path)).scaled(
                20, 20, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation
            )
            logo_lbl.setPixmap(pix)
            top_layout.addWidget(logo_lbl)

        brand = QLabel("OBOL")
        brand.setObjectName("BrandWordmark")
        top_layout.addWidget(brand)

        vsep = QFrame()
        vsep.setObjectName("VerticalDivider")
        vsep.setFixedHeight(18)
        top_layout.addWidget(vsep)

        self.project_combo = QComboBox()
        self.project_combo.setMinimumWidth(210)
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

        new_proj_top_btn = QPushButton("New Project")
        new_proj_top_btn.setObjectName("SubtleToolButton")
        new_proj_top_btn.setIcon(get_icon("plus", "#d4d4d8"))
        new_proj_top_btn.setIconSize(QSize(14, 14))
        new_proj_top_btn.setToolTip("Create a new Obol project (Ctrl+N)")
        new_proj_top_btn.clicked.connect(self.open_new_project_wizard)
        top_layout.addWidget(new_proj_top_btn)

        top_layout.addStretch(1)

        self.fast_drop_btn = QPushButton()
        self.fast_drop_btn.setIconSize(QSize(14, 14))
        self.fast_drop_btn.setToolTip(
            "When OFF: dropping files opens naming & metadata dialogs.\n"
            "When ON: dropped files ingest or publish immediately with auto-routed defaults."
        )
        self.fast_drop_btn.clicked.connect(self._toggle_fast_drop)
        self._update_fast_drop_button()
        top_layout.addWidget(self.fast_drop_btn)

        self.cred_btn = QPushButton("Credits")
        self.cred_btn.setObjectName("GhostButton")
        self.cred_btn.setIcon(get_icon("file_text", TEXT_MUTED, ICON_SM))
        self.cred_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.cred_btn.setToolTip("Open Project Attribution & License Audit")
        self.cred_btn.clicked.connect(self.open_credits_dialog)
        top_layout.addWidget(self.cred_btn)

        self.refresh_btn = QPushButton("Sync")
        self.refresh_btn.setObjectName("GhostButton")
        self.refresh_btn.setIcon(get_icon("refresh", TEXT_MUTED, ICON_SM))
        self.refresh_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.refresh_btn.setToolTip("Scan disk for external file changes and reload view (F5)")
        self.refresh_btn.clicked.connect(self._action_refresh_and_reconcile)
        top_layout.addWidget(self.refresh_btn)

        settings_btn = QPushButton("Settings")
        settings_btn.setObjectName("GhostButton")
        settings_btn.setIcon(get_icon("settings", TEXT_MUTED, ICON_SM))
        settings_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        settings_btn.setToolTip("Configure Auto-Save, Templates, Software Profiles, Vault & DCC Integrations")
        settings_menu = QMenu(settings_btn)

        self.auto_save_meta_action = settings_menu.addAction("Auto-Save Inspector Metadata")
        self.auto_save_meta_action.setCheckable(True)
        self.auto_save_meta_action.setChecked(self.hub.config.auto_save_metadata)
        self.auto_save_meta_action.toggled.connect(self._toggle_auto_save_metadata)
        settings_menu.addSeparator()

        act_pv = settings_menu.addAction("Projects && Vault Manager…")
        act_pv.triggered.connect(self.open_projects_and_vault_dialog)
        act_tpl = settings_menu.addAction("3-Tier Templates…")
        act_tpl.triggered.connect(lambda: self.open_template_manager_dialog(initial_tab=0))
        act_seeds = settings_menu.addAction("Software Seed Templates…")
        act_seeds.triggered.connect(lambda: self.open_template_manager_dialog(initial_tab=2))
        act_sw = settings_menu.addAction("Software Profiles…")
        act_sw.triggered.connect(self.open_software_settings_dialog)
        act_dcc = settings_menu.addAction("DCC Integrations…")
        act_dcc.triggered.connect(lambda: self.open_integrations_manager_dialog(""))
        act_root = settings_menu.addAction("Change Client Workspace Root…")
        act_root.triggered.connect(self._prompt_change_client_root)
        settings_menu.addSeparator()
        act_shortcuts = settings_menu.addAction("Keyboard Shortcuts…")
        act_shortcuts.setIcon(get_icon("keyboard", TEXT_SECONDARY, ICON_SM))
        act_shortcuts.triggered.connect(self.open_shortcuts_dialog)
        act_hub = settings_menu.addAction("Open Global Hub Folder (~/.obol)")
        act_hub.triggered.connect(lambda: open_folder_in_os(self.hub.hub_dir))
        settings_btn.setMenu(settings_menu)
        top_layout.addWidget(settings_btn)

        root_layout.addWidget(top_bar)

        # ---------------- Central Stack: Welcome Empty State vs 3-Pane Workspace ----------------
        self.central_stack = QStackedWidget()
        self.central_stack.addWidget(self._build_welcome_page())

        self.main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.main_splitter.setContentsMargins(10, 8, 10, 8)
        self.main_splitter.setHandleWidth(6)

        # Pane 1: Arbitrary-Depth Component Hierarchy Tree + Filter
        pane1 = QFrame()
        pane1.setObjectName("PanelCard")
        p1_layout = QVBoxLayout(pane1)
        p1_layout.setContentsMargins(10, 10, 10, 10)
        p1_layout.setSpacing(8)
        p1_header = QHBoxLayout()
        p1_title = QLabel("Components")
        p1_title.setObjectName("SectionHeader")
        add_comp_btn = QPushButton()
        add_comp_btn.setObjectName("GhostButton")
        add_comp_btn.setIcon(get_icon("plus", TEXT_SECONDARY, ICON_SM))
        add_comp_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        add_comp_btn.setFixedSize(ICON_BTN_SIZE, ICON_BTN_SIZE)
        add_comp_btn.setToolTip("Add a new Triad Component (Ctrl+Shift+N)")
        add_comp_btn.clicked.connect(lambda: self._prompt_add_component())
        p1_header.addWidget(p1_title)
        p1_header.addStretch()
        p1_header.addWidget(add_comp_btn)
        p1_layout.addLayout(p1_header)

        self.comp_filter_edit = QLineEdit()
        self.comp_filter_edit.setObjectName("FilterInput")
        self.comp_filter_edit.setPlaceholderText("Filter components…")
        self.comp_filter_edit.setClearButtonEnabled(True)
        attach_search_icon(self.comp_filter_edit)
        self.comp_filter_edit.textChanged.connect(self._filter_component_tree)
        p1_layout.addWidget(self.comp_filter_edit)

        self.component_tree = DropTreeWidget()
        self.component_tree.setAcceptDrops(False)
        self.component_tree.setIndentation(14)
        self.component_tree.setHeaderLabels(["Component", "App"])
        self.component_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.component_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.component_tree.set_empty_placeholder("No components yet", "Click '+' above to create a Triad component")
        self.component_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.component_tree.customContextMenuRequested.connect(self._ctx_component_tree)
        self.component_tree.currentItemChanged.connect(self._on_component_tree_selected)
        p1_layout.addWidget(self.component_tree, 1)

        # Hidden legacy label kept for compatibility; Vault status lives cleanly in bottom status pill
        self.vault_status_label = QLabel("")
        self.vault_status_label.hide()
        self.main_splitter.addWidget(pane1)

        # Pane 2: Center Triad Workspace vs Group Overview Stack
        pane2 = QFrame()
        pane2.setObjectName("WorkspaceCard")
        p2_outer = QVBoxLayout(pane2)
        p2_outer.setContentsMargins(10, 8, 10, 10)
        p2_outer.setSpacing(6)

        self.workspace_stack = QStackedWidget()
        triad_page = QWidget()
        p2_layout = QVBoxLayout(triad_page)
        p2_layout.setContentsMargins(0, 0, 0, 0)
        p2_layout.setSpacing(6)

        # Component Context Header (Breadcrumb + Bound Software + Overflow Menu)
        comp_hdr = QFrame()
        comp_hdr.setObjectName("ComponentHeader")
        comp_hdr_layout = QHBoxLayout(comp_hdr)
        comp_hdr_layout.setContentsMargins(2, 0, 2, 6)
        comp_hdr_layout.setSpacing(8)

        self.comp_breadcrumb_btn = QPushButton("Select a component")
        self.comp_breadcrumb_btn.setObjectName("BreadcrumbButton")
        self.comp_breadcrumb_btn.setIcon(get_icon("folder", TEXT_MUTED, ICON_SM))
        self.comp_breadcrumb_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.comp_breadcrumb_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.comp_breadcrumb_btn.setToolTip("Open component folder in OS Explorer")
        self.comp_breadcrumb_btn.clicked.connect(self._open_active_component_folder)
        comp_hdr_layout.addWidget(self.comp_breadcrumb_btn, 1)

        sw_lbl = QLabel("App:")
        sw_lbl.setObjectName("MutedText")
        comp_hdr_layout.addWidget(sw_lbl)

        self.insp_software_combo = QComboBox()
        self.insp_software_combo.setMinimumWidth(145)
        self.insp_software_combo.setToolTip("Software bound to this component")
        self.insp_software_combo.currentIndexChanged.connect(self._on_component_software_changed)
        comp_hdr_layout.addWidget(self.insp_software_combo)

        self.sw_exe_hint_label = QLabel("")
        self.sw_exe_hint_label.setObjectName("MutedText")
        comp_hdr_layout.addWidget(self.sw_exe_hint_label)

        self.comp_more_btn = QPushButton()
        self.comp_more_btn.setObjectName("GhostButton")
        self.comp_more_btn.setIcon(get_icon("more_horiz", TEXT_MUTED, 15))
        self.comp_more_btn.setIconSize(QSize(15, 15))
        self.comp_more_btn.setFixedSize(ICON_BTN_SIZE, ICON_BTN_SIZE)
        self.comp_more_btn.setToolTip("Component actions")
        comp_menu = QMenu(self.comp_more_btn)
        act_c_open = comp_menu.addAction("Open Component Folder in Explorer")
        act_c_open.triggered.connect(self._open_active_component_folder)
        act_c_copy = comp_menu.addAction("Copy Component Folder Path")
        act_c_copy.triggered.connect(self._copy_active_component_path)
        act_c_cap = comp_menu.addAction("Capture as Component Template…")
        act_c_cap.triggered.connect(lambda: self._capture_component_as_template(self.active_component_path) if self.active_component_path else None)
        comp_menu.addSeparator()
        act_c_del = comp_menu.addAction("Delete Component…")
        act_c_del.triggered.connect(self._action_delete_component)
        self.comp_more_btn.setMenu(comp_menu)
        comp_hdr_layout.addWidget(self.comp_more_btn)

        p2_layout.addWidget(comp_hdr)

        self.triad_tabs = QTabWidget()
        install_tab_hover_switch(self.triad_tabs)

        # Tab 0: Working
        working_tab = QWidget()
        w_layout = QVBoxLayout(working_tab)
        w_layout.setContentsMargins(0, 8, 0, 0)
        w_layout.setSpacing(6)
        w_top = QHBoxLayout()
        w_top.setSpacing(6)
        self.working_filter_edit = QLineEdit()
        self.working_filter_edit.setObjectName("FilterInput")
        self.working_filter_edit.setPlaceholderText("Filter working versions…")
        self.working_filter_edit.setClearButtonEnabled(True)
        self.working_filter_edit.setMaximumWidth(230)
        attach_search_icon(self.working_filter_edit)
        self.working_filter_edit.textChanged.connect(self._filter_working_list)
        w_top.addWidget(self.working_filter_edit)
        w_top.addStretch()
        ing_work_btn = QPushButton("Ingest File…")
        ing_work_btn.setObjectName("SubtleToolButton")
        ing_work_btn.setIcon(get_icon("ingest", TEXT_SECONDARY, ICON_SM))
        ing_work_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        ing_work_btn.setToolTip("Ingest an external file or folder bundle as the next working version (Ctrl+I)")
        ing_work_btn.clicked.connect(self._action_ingest_working_file)
        w_top.addWidget(ing_work_btn)
        vup_btn = QPushButton("Version Up")
        vup_btn.setObjectName("PrimaryButton")
        vup_btn.setIcon(get_icon("version_up", "#ffffff", ICON_SM))
        vup_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        vup_btn.setToolTip("Create next working/vNNN/ immediately (Ctrl+U)")
        vup_btn.clicked.connect(lambda: self._action_version_up_working())
        w_top.addWidget(vup_btn)
        w_layout.addLayout(w_top)

        self.working_list = DropTreeWidget()
        self.working_list.setRootIsDecorated(False)
        self.working_list.setAlternatingRowColors(True)
        self.working_list.setHeaderLabels(["Version", "Primary File", "Created", "Note"])
        self.working_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.working_list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.working_list.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.working_list.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.working_list.setColumnWidth(1, 190)
        self.working_list.set_empty_placeholder(
            "No working versions yet",
            "Click 'Version Up' or drop project files/folders here",
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
        r_layout.setContentsMargins(0, 8, 0, 0)
        r_layout.setSpacing(6)
        self.r_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.r_splitter.setHandleWidth(6)

        r_left = QWidget()
        rl_layout = QVBoxLayout(r_left)
        rl_layout.setContentsMargins(0, 0, 2, 0)
        rl_layout.setSpacing(6)
        rl_top = QHBoxLayout()
        rl_top.setSpacing(4)
        self.repo_filter_edit = QLineEdit()
        self.repo_filter_edit.setObjectName("FilterInput")
        self.repo_filter_edit.setPlaceholderText("Filter assets…")
        self.repo_filter_edit.setClearButtonEnabled(True)
        attach_search_icon(self.repo_filter_edit)
        self.repo_filter_edit.textChanged.connect(self._filter_repo_asset_tree)
        rl_top.addWidget(self.repo_filter_edit, 1)

        new_asset_btn = QPushButton("New Slot")
        new_asset_btn.setObjectName("SubtleToolButton")
        new_asset_btn.setIcon(get_icon("plus", TEXT_SECONDARY, ICON_SM))
        new_asset_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        new_asset_btn.setToolTip("Create an empty named Repo Asset slot before dropping files into it")
        new_asset_btn.clicked.connect(self._action_new_repo_asset)
        ing_new_btn = QPushButton("Ingest…")
        ing_new_btn.setObjectName("PrimaryButton")
        ing_new_btn.setIcon(get_icon("ingest", "#ffffff", ICON_SM))
        ing_new_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        ing_new_btn.setToolTip("Ingest file(s) into repo/ with provenance & credits (Ctrl+I)")
        ing_new_btn.clicked.connect(self._action_ingest_repo_file)
        rl_top.addWidget(new_asset_btn)
        rl_top.addWidget(ing_new_btn)
        rl_layout.addLayout(rl_top)

        self.repo_asset_tree = DropTreeWidget()
        self.repo_asset_tree.setIndentation(14)
        self.repo_asset_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.repo_asset_tree.setHeaderLabels(["Asset", "Ver", "Used"])
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
        self.repo_asset_tree.itemSelectionChanged.connect(self._update_copy_paste_buttons)
        self.repo_asset_tree.filesDropped.connect(self._handle_repo_tree_drop)
        rl_layout.addWidget(self.repo_asset_tree, 1)

        r_right = QWidget()
        rr_layout = QVBoxLayout(r_right)
        rr_layout.setContentsMargins(2, 0, 0, 0)
        rr_layout.setSpacing(6)
        rr_top = QHBoxLayout()
        self.repo_ver_header = QLabel("Versions in repo/")
        self.repo_ver_header.setObjectName("SectionHeader")
        self.repo_ver_header.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)
        ing_ver_btn = QPushButton("+ New Version…")
        ing_ver_btn.setObjectName("SubtleToolButton")
        ing_ver_btn.setToolTip("Ingest next version v(N+1) into the selected Repo Asset")
        ing_ver_btn.clicked.connect(self._action_ingest_repo_version)
        rr_top.addWidget(self.repo_ver_header, 1)
        rr_top.addWidget(ing_ver_btn)
        rr_layout.addLayout(rr_top)

        self.repo_version_list = DropTreeWidget()
        self.repo_version_list.setRootIsDecorated(False)
        self.repo_version_list.setAlternatingRowColors(True)
        self.repo_version_list.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.repo_version_list.setHeaderLabels(["Version", "File", "Size", "Note"])
        self.repo_version_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.repo_version_list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Interactive)
        self.repo_version_list.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.repo_version_list.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.repo_version_list.setColumnWidth(1, 160)
        self.repo_version_list.set_empty_placeholder(
            "No asset versions",
            "Select an asset or drop a file here for v(N+1)",
        )
        self.repo_version_list.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.repo_version_list.customContextMenuRequested.connect(self._ctx_repo_version_list)
        self.repo_version_list.currentItemChanged.connect(self._on_repo_version_selected)
        self.repo_version_list.itemSelectionChanged.connect(lambda: self._refresh_dcc_target_ui(refresh_bridges=False))
        self.repo_version_list.itemDoubleClicked.connect(self._on_repo_version_double_clicked)
        self.repo_version_list.filesDropped.connect(self._handle_repo_version_drop)
        self.repo_version_list.enable_file_drag_out(self._drag_path_for_repo_item)
        rr_layout.addWidget(self.repo_version_list, 1)

        self.r_splitter.addWidget(r_left)
        self.r_splitter.addWidget(r_right)
        self.r_splitter.setSizes([240, 360])
        r_layout.addWidget(self.r_splitter)
        self.triad_tabs.addTab(repo_tab, "Repo Assets (0)")

        # Tab 2: Published
        published_tab = QWidget()
        p_layout = QVBoxLayout(published_tab)
        p_layout.setContentsMargins(0, 8, 0, 0)
        p_layout.setSpacing(6)
        self.p_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.p_splitter.setHandleWidth(6)

        p_left = QWidget()
        pl_layout = QVBoxLayout(p_left)
        pl_layout.setContentsMargins(0, 0, 2, 0)
        pl_layout.setSpacing(6)
        pl_top = QHBoxLayout()
        pl_top.setSpacing(4)
        self.pub_filter_edit = QLineEdit()
        self.pub_filter_edit.setObjectName("FilterInput")
        self.pub_filter_edit.setPlaceholderText("Filter deliverables…")
        self.pub_filter_edit.setClearButtonEnabled(True)
        attach_search_icon(self.pub_filter_edit)
        self.pub_filter_edit.textChanged.connect(self._filter_published_deliverable_tree)
        pl_top.addWidget(self.pub_filter_edit, 1)

        new_deliv_btn = QPushButton("New Slot")
        new_deliv_btn.setObjectName("SubtleToolButton")
        new_deliv_btn.setIcon(get_icon("plus", TEXT_SECONDARY, ICON_SM))
        new_deliv_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        new_deliv_btn.setToolTip("Create a named Deliverable stream before publishing into it")
        new_deliv_btn.clicked.connect(self._action_new_deliverable)
        pub_btn = QPushButton("Publish…")
        pub_btn.setObjectName("PrimaryButton")
        pub_btn.setIcon(get_icon("publish", "#ffffff", ICON_SM))
        pub_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        pub_btn.setToolTip("Stage & publish deliverable file(s) (Ctrl+P)")
        pub_btn.clicked.connect(self._action_publish_deliverable)
        pl_top.addWidget(new_deliv_btn)
        pl_top.addWidget(pub_btn)
        pl_layout.addLayout(pl_top)

        self.published_deliverable_tree = DropTreeWidget()
        self.published_deliverable_tree.setIndentation(14)
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
        pr_layout.setContentsMargins(2, 0, 0, 0)
        pr_layout.setSpacing(6)
        pr_top = QHBoxLayout()
        self.pub_ver_header = QLabel("Releases in published/")
        self.pub_ver_header.setObjectName("SectionHeader")
        self.pub_ver_header.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Fixed)

        manual_export_btn = QPushButton("Manual Export")
        manual_export_btn.setObjectName("SubtleToolButton")
        manual_export_btn.setToolTip("Render into a staging folder manually when not using live DCC Export")
        manual_menu = QMenu(manual_export_btn)
        act_exp_dir = manual_menu.addAction("1. Prepare & Open Export Folder…")
        act_exp_dir.triggered.connect(self._action_open_publish_export_folder)
        act_sync_pub = manual_menu.addAction("2. Sync Rendered Exports")
        act_sync_pub.triggered.connect(self._action_sync_published_folder)
        manual_export_btn.setMenu(manual_menu)

        pr_top.addWidget(self.pub_ver_header, 1)
        pr_top.addWidget(manual_export_btn)
        pr_layout.addLayout(pr_top)

        self.published_version_tree = DropTreeWidget()
        self.published_version_tree.setAlternatingRowColors(True)
        self.published_version_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.published_version_tree.setHeaderLabels(["Version / File", "Types", "Source / Size", "Note"])
        self.published_version_tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Interactive)
        self.published_version_tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.published_version_tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        self.published_version_tree.header().setSectionResizeMode(3, QHeaderView.ResizeMode.Stretch)
        self.published_version_tree.setColumnWidth(0, 150)
        self.published_version_tree.set_empty_placeholder(
            "No releases published yet",
            "Click 'Publish…' or drop exported files here (or onto an existing vNNN row)",
        )
        self.published_version_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.published_version_tree.customContextMenuRequested.connect(self._ctx_published_version_tree)
        self.published_version_tree.currentItemChanged.connect(self._on_published_version_selected)
        self.published_version_tree.itemSelectionChanged.connect(lambda: self._refresh_dcc_target_ui(refresh_bridges=False))
        self.published_version_tree.itemDoubleClicked.connect(self._on_published_item_double_clicked)
        self.published_version_tree.filesDropped.connect(self._handle_published_version_drop)
        self.published_version_tree.enable_file_drag_out(self._drag_path_for_published_item)
        pr_layout.addWidget(self.published_version_tree, 1)

        self.p_splitter.addWidget(p_left)
        self.p_splitter.addWidget(p_right)
        self.p_splitter.setSizes([220, 380])
        p_layout.addWidget(self.p_splitter)
        self.triad_tabs.addTab(published_tab, "Published (0)")

        self.triad_tabs.currentChanged.connect(self._on_triad_tab_changed)
        p2_layout.addWidget(self.triad_tabs, 1)

        self.dcc_exchange_bar = QFrame()
        self.dcc_exchange_bar.setObjectName("DccExchangeBar")
        dcc_bar_layout = QHBoxLayout(self.dcc_exchange_bar)
        dcc_bar_layout.setContentsMargins(10, 6, 10, 6)
        dcc_bar_layout.setSpacing(6)

        dcc_lbl = QLabel("DCC")
        dcc_lbl.setObjectName("SectionHeader")
        dcc_bar_layout.addWidget(dcc_lbl)

        self.dcc_target_combo = QComboBox()
        self.dcc_target_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.dcc_target_combo.setMinimumContentsLength(15)
        self.dcc_target_combo.setToolTip("Choose Auto (follow live DCC) or pin a specific DCC target")
        self.dcc_target_combo.currentIndexChanged.connect(self._on_dcc_target_combo_changed)
        dcc_bar_layout.addWidget(self.dcc_target_combo, 2)

        self.dcc_mode_combo = QComboBox()
        self.dcc_mode_combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.dcc_mode_combo.setMinimumContentsLength(13)
        self.dcc_mode_combo.setToolTip("Choose how media is sent into the selected DCC")
        self.dcc_mode_combo.currentIndexChanged.connect(self._on_dcc_mode_combo_changed)
        dcc_bar_layout.addWidget(self.dcc_mode_combo, 2)

        self.dcc_send_btn = QPushButton("Send Media")
        self.dcc_send_btn.setIcon(get_icon("send", TEXT_SECONDARY, ICON_SM))
        self.dcc_send_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.dcc_send_btn.setToolTip("Send selected Repo Assets or Published file(s) to the target DCC (Ctrl+D)")
        self.dcc_send_btn.clicked.connect(self._action_send_selected_to_active_dcc)
        dcc_bar_layout.addWidget(self.dcc_send_btn, 2)

        self.dcc_export_btn = QPushButton("Export…")
        self.dcc_export_btn.setIcon(get_icon("dcc_pull", TEXT_SECONDARY, ICON_SM))
        self.dcc_export_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.dcc_export_btn.setToolTip("Export / render & publish from the selected DCC")
        self.dcc_export_btn.clicked.connect(self._action_export_from_active_dcc)
        dcc_bar_layout.addWidget(self.dcc_export_btn, 1)

        self.dcc_refresh_btn = QPushButton()
        self.dcc_refresh_btn.setObjectName("GhostButton")
        self.dcc_refresh_btn.setIcon(get_icon("refresh", TEXT_MUTED, ICON_SM))
        self.dcc_refresh_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.dcc_refresh_btn.setFixedSize(ICON_BTN_SIZE, ICON_BTN_SIZE)
        self.dcc_refresh_btn.setToolTip("Refresh live DCC connection status")
        self.dcc_refresh_btn.clicked.connect(self._action_refresh_dcc_targets)
        dcc_bar_layout.addWidget(self.dcc_refresh_btn)

        p2_layout.addWidget(self.dcc_exchange_bar)
        self.workspace_stack.addWidget(triad_page)
        self.workspace_stack.addWidget(self._build_group_overview_page())
        p2_outer.addWidget(self.workspace_stack, 1)
        self.main_splitter.addWidget(pane2)

        # Pane 3: Scrollable, Action-First, Context-Aware Inspector
        pane3 = QFrame()
        pane3.setObjectName("PanelCard")
        p3_outer = QVBoxLayout(pane3)
        p3_outer.setContentsMargins(0, 0, 0, 0)
        p3_outer.setSpacing(0)

        insp_scroll = QScrollArea()
        insp_scroll.setObjectName("InspectorScroll")
        insp_scroll.setWidgetResizable(True)
        insp_scroll.setFrameShape(QFrame.Shape.NoFrame)
        insp_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        insp_body = QWidget()
        p3_layout = QVBoxLayout(insp_body)
        p3_layout.setContentsMargins(12, 12, 12, 12)
        p3_layout.setSpacing(10)

        insp_hdr_row = QHBoxLayout()
        insp_hdr_row.setSpacing(6)
        self.insp_context_badge = QLabel("Working")
        self.insp_context_badge.setObjectName("ContextBadge")
        self.meta_state_label = QLabel("")
        self.meta_state_label.setObjectName("MetaSavedBadge")
        insp_hdr_row.addWidget(self.insp_context_badge)
        insp_hdr_row.addStretch()
        insp_hdr_row.addWidget(self.meta_state_label)
        p3_layout.addLayout(insp_hdr_row)

        self.selected_item_label = QLabel("No version selected")
        sel_font = QFont(self.font())
        sel_font.setBold(True)
        sel_font.setPointSize(max(10, sel_font.pointSize() + 1))
        self.selected_item_label.setFont(sel_font)
        self.selected_item_label.setWordWrap(True)
        p3_layout.addWidget(self.selected_item_label)

        self.insp_lineage_label = QLabel("")
        self.insp_lineage_label.setObjectName("MutedText")
        self.insp_lineage_label.setWordWrap(True)
        p3_layout.addWidget(self.insp_lineage_label)

        # Action-First Primary Controls
        self.launch_btn = QPushButton("Launch Working File")
        self.launch_btn.setObjectName("PrimaryButton")
        self.launch_btn.setIcon(get_icon("play", "#ffffff"))
        self.launch_btn.setIconSize(QSize(13, 13))
        self.launch_btn.clicked.connect(self._action_primary_open)
        p3_layout.addWidget(self.launch_btn)

        # Working-Tab Only Actions
        self.working_actions_box = QWidget()
        wa_layout = QVBoxLayout(self.working_actions_box)
        wa_layout.setContentsMargins(0, 0, 0, 0)
        wa_layout.setSpacing(6)

        self.pub_from_working_btn = QPushButton("Publish from Working…")
        self.pub_from_working_btn.setObjectName("SubtleToolButton")
        self.pub_from_working_btn.setToolTip("Publish deliverables linked to this working version")
        self.pub_from_working_btn.clicked.connect(self._action_publish_from_selected_working)
        wa_layout.addWidget(self.pub_from_working_btn)

        row_reaper = QHBoxLayout()
        row_reaper.setContentsMargins(0, 0, 0, 0)
        row_reaper.setSpacing(6)
        self.reaper_inc_save_btn = QPushButton("Increment Save (→ v002)")
        self.reaper_inc_save_btn.setObjectName("SubtleToolButton")
        self.reaper_inc_save_btn.setToolTip(
            "Use REAPER Save As to save directly to the next working version while keeping it open (Ctrl+Shift+S)"
        )
        self.reaper_inc_save_btn.clicked.connect(self._action_reaper_increment_save)
        row_reaper.addWidget(self.reaper_inc_save_btn, 1)

        self.reaper_update_media_btn = QPushButton("Update Media/MIDI…")
        self.reaper_update_media_btn.setObjectName("SubtleToolButton")
        self.reaper_update_media_btn.setToolTip(
            "Scan the open REAPER session (or active .rpp) for newer Obol MIDI & media versions"
        )
        self.reaper_update_media_btn.clicked.connect(self._action_open_reaper_update_dialog)
        row_reaper.addWidget(self.reaper_update_media_btn, 1)
        wa_layout.addLayout(row_reaper)

        self.collect_media_btn = QPushButton("Scan && Collect External .rpp Media")
        self.collect_media_btn.setObjectName("SubtleToolButton")
        self.collect_media_btn.clicked.connect(self._action_collect_external_media)
        wa_layout.addWidget(self.collect_media_btn)

        self.save_seed_btn = QPushButton("Save as Seed Template…")
        self.save_seed_btn.setObjectName("SubtleToolButton")
        self.save_seed_btn.clicked.connect(
            lambda: (
                self._save_working_as_seed_template(self._selected_working_version())  # type: ignore[arg-type]
                if self._selected_working_version() is not None
                else None
            )
        )
        wa_layout.addWidget(self.save_seed_btn)
        p3_layout.addWidget(self.working_actions_box)

        # Repo-Tab Only Actions
        self.repo_actions_box = QWidget()
        ra_layout = QVBoxLayout(self.repo_actions_box)
        ra_layout.setContentsMargins(0, 0, 0, 0)
        ra_layout.setSpacing(6)
        self.repo_new_ver_btn = QPushButton("+ Ingest New Version…")
        self.repo_new_ver_btn.setObjectName("SubtleToolButton")
        self.repo_new_ver_btn.clicked.connect(self._action_ingest_repo_version)
        ra_layout.addWidget(self.repo_new_ver_btn)
        p3_layout.addWidget(self.repo_actions_box)

        # Published-Tab Only Actions
        self.published_actions_box = QWidget()
        pa_layout = QVBoxLayout(self.published_actions_box)
        pa_layout.setContentsMargins(0, 0, 0, 0)
        pa_layout.setSpacing(6)
        self.pub_attach_btn = QPushButton("+ Attach More Files…")
        self.pub_attach_btn.setObjectName("SubtleToolButton")
        self.pub_attach_btn.clicked.connect(self._action_attach_to_selected_release)
        pa_layout.addWidget(self.pub_attach_btn)
        p3_layout.addWidget(self.published_actions_box)

        sep1 = QFrame()
        sep1.setObjectName("DividerLine")
        p3_layout.addWidget(sep1)

        meta_hdr_row = QHBoxLayout()
        meta_hdr_row.setSpacing(4)
        meta_hdr = QLabel("Metadata")
        meta_hdr.setObjectName("SectionHeader")
        self.copy_meta_btn = QPushButton()
        self.copy_meta_btn.setObjectName("GhostButton")
        self.copy_meta_btn.setIcon(get_icon("copy", TEXT_MUTED, ICON_SM))
        self.copy_meta_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.copy_meta_btn.setFixedSize(ICON_BTN_SIZE, ICON_BTN_SIZE)
        self.copy_meta_btn.setToolTip("Copy metadata from selected item (Ctrl+Shift+C)")
        self.copy_meta_btn.clicked.connect(self._action_copy_metadata)
        self.paste_meta_btn = QPushButton()
        self.paste_meta_btn.setObjectName("GhostButton")
        self.paste_meta_btn.setIcon(get_icon("clipboard", TEXT_MUTED, ICON_SM))
        self.paste_meta_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.paste_meta_btn.setFixedSize(ICON_BTN_SIZE, ICON_BTN_SIZE)
        self.paste_meta_btn.setToolTip("Paste copied metadata to selected item(s) (Ctrl+Shift+V)")
        self.paste_meta_btn.clicked.connect(lambda: self._action_paste_metadata(merge=False))
        self.paste_meta_btn.setEnabled(False)
        meta_hdr_row.addWidget(meta_hdr)
        meta_hdr_row.addStretch()
        meta_hdr_row.addWidget(self.copy_meta_btn)
        meta_hdr_row.addWidget(self.paste_meta_btn)
        p3_layout.addLayout(meta_hdr_row)

        self.meta_form = QFormLayout()
        self.meta_form.setSpacing(6)
        self.insp_author_edit = QLineEdit()
        self.insp_source_edit = QLineEdit()

        self.insp_url_container = QWidget()
        url_row = QHBoxLayout(self.insp_url_container)
        url_row.setContentsMargins(0, 0, 0, 0)
        url_row.setSpacing(4)
        self.insp_url_edit = QLineEdit()
        self.insp_url_edit.setPlaceholderText("https://...")
        self.open_url_btn = QPushButton()
        self.open_url_btn.setObjectName("GhostButton")
        self.open_url_btn.setIcon(get_icon("external_link", ACCENT_LIGHT, ICON_SM))
        self.open_url_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.open_url_btn.setFixedSize(ICON_BTN_SIZE, ICON_BTN_SIZE)
        self.open_url_btn.setToolTip("Open URL in browser")
        self.open_url_btn.setEnabled(False)
        self.open_url_btn.clicked.connect(self._open_insp_url)
        self.insp_url_edit.textChanged.connect(lambda txt: self.open_url_btn.setEnabled(bool(txt.strip())))
        url_row.addWidget(self.insp_url_edit, 1)
        url_row.addWidget(self.open_url_btn)

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

        for edit_w in (
            self.insp_author_edit,
            self.insp_source_edit,
            self.insp_url_edit,
            self.insp_tags_edit,
            self.insp_comment_edit,
        ):
            edit_w.textEdited.connect(self._on_metadata_field_edited)
            edit_w.editingFinished.connect(self._on_metadata_editing_finished)
        if self.insp_license_edit is not None:
            self.insp_license_edit.textEdited.connect(self._on_metadata_field_edited)
            self.insp_license_edit.editingFinished.connect(self._on_metadata_editing_finished)
        self.insp_license_combo.activated.connect(self._on_license_activated)

        self.meta_form.addRow("Author", self.insp_author_edit)
        self.meta_form.addRow("Source", self.insp_source_edit)
        self.meta_form.addRow("URL", self.insp_url_container)
        self.meta_form.addRow("License", self.insp_license_combo)
        self.meta_form.addRow("Tags", self.insp_tags_edit)
        self.insp_comment_label = QLabel("Note")
        self.meta_form.addRow(self.insp_comment_label, self.insp_comment_edit)
        p3_layout.addLayout(self.meta_form)

        self.insp_custom_meta = CustomMetadataEditor(self, hidden_keys={"seed_template"})
        self.insp_custom_meta.edited.connect(self._on_metadata_field_edited)
        self.insp_custom_meta.committed.connect(self._on_metadata_editing_finished)
        p3_layout.addWidget(self.insp_custom_meta)

        self.save_meta_btn = QPushButton("Save Metadata (Ctrl+S)")
        self.save_meta_btn.clicked.connect(lambda: self._save_inspector_metadata(silent_refresh=False))
        p3_layout.addWidget(self.save_meta_btn)

        p3_layout.addStretch(1)

        sep2 = QFrame()
        sep2.setObjectName("DividerLine")
        p3_layout.addWidget(sep2)

        folder_row = QHBoxLayout()
        folder_row.setSpacing(6)
        self.open_explorer_btn = QPushButton("Folder")
        self.open_explorer_btn.setObjectName("SubtleToolButton")
        self.open_explorer_btn.setIcon(get_icon("folder", "#d4d4d8"))
        self.open_explorer_btn.setIconSize(QSize(13, 13))
        self.open_explorer_btn.setToolTip("Open the selected item's folder in OS Explorer")
        self.open_explorer_btn.clicked.connect(self._action_open_in_explorer)
        self.copy_path_btn = QPushButton("Copy Path")
        self.copy_path_btn.setObjectName("SubtleToolButton")
        self.copy_path_btn.setIcon(get_icon("copy", "#d4d4d8"))
        self.copy_path_btn.setIconSize(QSize(13, 13))
        self.copy_path_btn.setToolTip("Copy the selected item's full path to clipboard")
        self.copy_path_btn.clicked.connect(self._action_copy_selected_path)
        folder_row.addWidget(self.open_explorer_btn)
        folder_row.addWidget(self.copy_path_btn)
        p3_layout.addLayout(folder_row)

        self.vault_actions_box = QWidget()
        va_layout = QHBoxLayout(self.vault_actions_box)
        va_layout.setContentsMargins(0, 0, 0, 0)
        va_layout.setSpacing(6)
        co_ver_btn = QPushButton("Check Out")
        co_ver_btn.setObjectName("SubtleToolButton")
        co_ver_btn.clicked.connect(self._action_checkout_selected)
        off_ver_btn = QPushButton("Offload")
        off_ver_btn.setObjectName("SubtleToolButton")
        off_ver_btn.clicked.connect(self._action_offload_selected)
        va_layout.addWidget(co_ver_btn)
        va_layout.addWidget(off_ver_btn)
        p3_layout.addWidget(self.vault_actions_box)

        self.delete_selected_btn = QPushButton("Delete Selected…")
        self.delete_selected_btn.setObjectName("DangerButton")
        self.delete_selected_btn.setIcon(get_icon("trash", DANGER_TEXT, ICON_SM))
        self.delete_selected_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.delete_selected_btn.setToolTip("Delete the currently selected item (Del)")
        self.delete_selected_btn.clicked.connect(self._action_delete_active_selection)
        p3_layout.addWidget(self.delete_selected_btn)

        insp_scroll.setWidget(insp_body)
        p3_outer.addWidget(insp_scroll, 1)

        self.main_splitter.addWidget(pane3)
        self.main_splitter.setSizes([240, 670, 310])
        self.central_stack.addWidget(self.main_splitter)
        root_layout.addWidget(self.central_stack, 1)

        # ---------------- Minimalist Bottom Status Bar ----------------
        status_bar = QFrame()
        status_bar.setObjectName("StatusBar")
        sb_layout = QHBoxLayout(status_bar)
        sb_layout.setContentsMargins(14, 5, 14, 5)
        sb_layout.setSpacing(10)

        # Hidden legacy path widgets preserved for internal/test compatibility
        self.active_path_btn = QPushButton("")
        self.active_path_btn.hide()
        self.active_path_label = QLabel("")
        self.active_path_label.hide()

        self.client_root_btn = QPushButton()
        self.client_root_btn.setObjectName("BreadcrumbButton")
        self.client_root_btn.setIcon(get_icon("folder", TEXT_MUTED, ICON_SM))
        self.client_root_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.client_root_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.client_root_btn.clicked.connect(self._prompt_change_client_root)

        self.status_feedback_label = QLabel("")
        self.status_feedback_label.setObjectName("SuccessText")
        self._feedback_timer = QTimer(self)
        self._feedback_timer.setSingleShot(True)
        self._feedback_timer.timeout.connect(lambda: self.status_feedback_label.setText(""))

        self.dcc_status_pill = QPushButton("DCC: Setup…")
        self.dcc_status_pill.setObjectName("StatusPillLocal")
        self.dcc_status_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self.dcc_status_pill.setToolTip("Manage REAPER, DaVinci Resolve, FilmCraft, and DCC integrations")
        self.dcc_status_pill.clicked.connect(lambda: self.open_integrations_manager_dialog(""))

        # Hidden per-DCC pills kept updated for test compatibility without cluttering the status bar
        self.reaper_status_pill = QPushButton("REAPER: Offline", self)
        self.reaper_status_pill.hide()
        self.resolve_status_pill = QPushButton("Resolve: Offline", self)
        self.resolve_status_pill.hide()
        self.filmcraft_status_pill = QPushButton("FilmCraft: Offline", self)
        self.filmcraft_status_pill.hide()

        self.vault_status_pill = QPushButton("Standalone Local")
        self.vault_status_pill.setObjectName("StatusPillLocal")
        self.vault_status_pill.setCursor(Qt.CursorShape.PointingHandCursor)
        self.vault_status_pill.setToolTip("Click to manage Projects & Server Vault")
        self.vault_status_pill.clicked.connect(self.open_projects_and_vault_dialog)

        sb_layout.addWidget(self.client_root_btn)
        sb_layout.addWidget(self.status_feedback_label, 1)
        sb_layout.addWidget(self.dcc_status_pill)
        sb_layout.addWidget(self.vault_status_pill)
        root_layout.addWidget(status_bar)

        self._reaper_poll_timer = QTimer(self)
        self._reaper_poll_timer.setInterval(3000)
        self._reaper_poll_timer.timeout.connect(self._refresh_reaper_status_pill)
        if QApplication.platformName() != "offscreen":
            self._reaper_poll_timer.start()

        self._install_shortcuts()
        self._restore_window_geometry()
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
        create_btn = QPushButton("New Project…")
        create_btn.setObjectName("PrimaryButton")
        create_btn.setIcon(get_icon("plus", "#ffffff", ICON_SM))
        create_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        create_btn.clicked.connect(self.open_new_project_wizard)
        reg_btn = QPushButton("Register Existing Project Folder…")
        reg_btn.clicked.connect(self._register_existing_project_shortcut)
        vault_btn = QPushButton("Projects && Vault…")
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

    def _build_group_overview_page(self) -> QWidget:
        page = QWidget()
        outer = QVBoxLayout(page)
        outer.setContentsMargins(24, 24, 24, 24)
        outer.addStretch(1)

        card = QFrame()
        card.setObjectName("EmptyStateCard")
        card.setMaximumWidth(520)
        c_layout = QVBoxLayout(card)
        c_layout.setContentsMargins(22, 20, 22, 20)
        c_layout.setSpacing(10)

        self.group_title_label = QLabel("Group")
        tfont = QFont(self.font())
        tfont.setPointSize(14)
        tfont.setBold(True)
        self.group_title_label.setFont(tfont)
        c_layout.addWidget(self.group_title_label)

        sub = QLabel("Component group — double-click or choose a component below to open its Triad workspace.")
        sub.setObjectName("MutedText")
        sub.setWordWrap(True)
        c_layout.addWidget(sub)

        self.group_children_list = QListWidget()
        self.group_children_list.setMaximumHeight(210)
        self.group_children_list.itemActivated.connect(self._on_group_child_activated)
        c_layout.addWidget(self.group_children_list)

        btns = QHBoxLayout()
        self.open_group_child_btn = QPushButton("Open Selected Component")
        self.open_group_child_btn.setObjectName("PrimaryButton")
        self.open_group_child_btn.setEnabled(False)
        self.open_group_child_btn.clicked.connect(self._open_selected_group_child)
        self.group_children_list.currentItemChanged.connect(self._on_group_child_current_changed)

        add_btn = QPushButton("Add Component in Group…")
        add_btn.setObjectName("SubtleToolButton")
        add_btn.setIcon(get_icon("plus", TEXT_SECONDARY, ICON_SM))
        add_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        add_btn.clicked.connect(lambda: self._prompt_add_component(self._selected_group_prefix))
        open_btn = QPushButton("Open Group Folder")
        open_btn.setObjectName("SubtleToolButton")
        open_btn.setIcon(get_icon("folder", TEXT_SECONDARY, ICON_SM))
        open_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        open_btn.clicked.connect(
            lambda: open_folder_in_os(self.active_project_root / self._selected_group_prefix)
            if (self.active_project_root and self._selected_group_prefix)
            else None
        )
        btns.addWidget(self.open_group_child_btn)
        btns.addWidget(add_btn)
        btns.addWidget(open_btn)
        btns.addStretch()
        c_layout.addLayout(btns)

        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(card)
        row.addStretch(1)
        outer.addLayout(row)
        outer.addStretch(1)
        return page

    def _on_group_child_current_changed(
        self, cur: Optional[QListWidgetItem], _prev: Optional[QListWidgetItem]
    ) -> None:
        self.open_group_child_btn.setEnabled(cur is not None)

    def _open_selected_group_child(self) -> None:
        item = self.group_children_list.currentItem()
        if item is not None:
            self._on_group_child_activated(item)

    def _on_group_child_activated(self, item: QListWidgetItem) -> None:
        comp_path = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not comp_path:
            return
        found = self._find_component_tree_item(comp_path)
        if found is not None:
            self.component_tree.setCurrentItem(found)

    def _find_component_tree_item(self, comp_path: str) -> Optional[QTreeWidgetItem]:
        def _visit(it: QTreeWidgetItem) -> Optional[QTreeWidgetItem]:
            if it.data(0, Qt.ItemDataRole.UserRole + 1) == "component" and it.data(0, Qt.ItemDataRole.UserRole) == comp_path:
                return it
            for idx in range(it.childCount()):
                res = _visit(it.child(idx))
                if res is not None:
                    return res
            return None

        for i in range(self.component_tree.topLevelItemCount()):
            res = _visit(self.component_tree.topLevelItem(i))
            if res is not None:
                return res
        return None

    def _install_shortcuts(self) -> None:
        QShortcut(QKeySequence("Ctrl+N"), self, activated=self.open_new_project_wizard)
        QShortcut(QKeySequence("Ctrl+Shift+N"), self, activated=self._prompt_add_component)
        QShortcut(QKeySequence("Ctrl+U"), self, activated=lambda: self._action_version_up_working())
        QShortcut(QKeySequence("Ctrl+Shift+S"), self, activated=self._shortcut_reaper_increment_save)
        QShortcut(QKeySequence("Ctrl+I"), self, activated=self._action_context_ingest)
        QShortcut(QKeySequence("Ctrl+P"), self, activated=self._action_publish_deliverable)
        QShortcut(QKeySequence("Ctrl+D"), self, activated=self._action_send_selected_to_active_dcc)
        QShortcut(QKeySequence("Ctrl+S"), self, activated=lambda: self._save_inspector_metadata(silent_refresh=False))
        QShortcut(QKeySequence("Ctrl+Shift+C"), self, activated=self._action_copy_metadata)
        QShortcut(QKeySequence("Ctrl+Shift+V"), self, activated=lambda: self._action_paste_metadata(merge=False))
        QShortcut(QKeySequence("Ctrl+F"), self, activated=self._focus_active_filter)
        QShortcut(QKeySequence("F5"), self, activated=self._action_refresh_and_reconcile)
        QShortcut(QKeySequence("Delete"), self, activated=self._action_delete_focused_widget)
        QShortcut(QKeySequence("Ctrl+/"), self, activated=self.open_shortcuts_dialog)

    def _shortcut_reaper_increment_save(self) -> None:
        is_reaper_comp = bool(self.active_snapshot and self.active_snapshot.software == "reaper")
        if not is_reaper_comp:
            st = self.reaper_bridge.get_state()
            has_live_reaper = bool(self.reaper_bridge.is_connected() and st.active_rpp)
            if not has_live_reaper:
                self.show_status_message("Increment Save (Ctrl+Shift+S) is available for REAPER components")
                return
        self._action_reaper_increment_save()

    def open_shortcuts_dialog(self) -> None:
        dlg = ShortcutsDialog(self)
        dlg.exec()

    def _focus_active_filter(self) -> None:
        if self.focusWidget() is self.component_tree:
            self.comp_filter_edit.setFocus()
            self.comp_filter_edit.selectAll()
            return
        idx = self.triad_tabs.currentIndex()
        target = self.working_filter_edit if idx == 0 else (self.repo_filter_edit if idx == 1 else self.pub_filter_edit)
        target.setFocus()
        target.selectAll()

    def _restore_window_geometry(self) -> None:
        if QApplication.platformName() == "offscreen":
            return
        settings = QSettings("Obol", "ObolDesktop")
        geom = settings.value("window_geometry")
        if geom is not None:
            self.restoreGeometry(geom)  # type: ignore[arg-type]
        for key, splitter in (
            ("main_splitter_state", self.main_splitter),
            ("repo_splitter_state", self.r_splitter),
            ("pub_splitter_state", self.p_splitter),
        ):
            state = settings.value(key)
            if state is not None:
                splitter.restoreState(state)  # type: ignore[arg-type]

    def _save_window_geometry(self) -> None:
        if QApplication.platformName() == "offscreen":
            return
        settings = QSettings("Obol", "ObolDesktop")
        settings.setValue("window_geometry", self.saveGeometry())
        settings.setValue("main_splitter_state", self.main_splitter.saveState())
        settings.setValue("repo_splitter_state", self.r_splitter.saveState())
        settings.setValue("pub_splitter_state", self.p_splitter.saveState())

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
        if hasattr(self, "_meta_autosave_timer"):
            self._meta_autosave_timer.stop()
        self._flush_pending_metadata_if_autosave()
        if not self._confirm_discard_or_save_dirty_metadata():
            if hasattr(event, "ignore"):
                event.ignore()  # type: ignore[attr-defined]
            return
        self._save_window_geometry()
        if hasattr(self, "_reaper_poll_timer"):
            self._reaper_poll_timer.stop()
        super().closeEvent(event)  # type: ignore[arg-type]

    def _confirm_discard_or_save_dirty_metadata(self) -> bool:
        if self._meta_loading:
            return True
        if not (self._meta_dirty and not self.hub.config.auto_save_metadata and self._insp_target_ctx is not None):
            return True
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Unsaved Metadata Changes")
        box.setText("You have unsaved metadata changes in the Inspector.")
        box.setInformativeText("Save your changes before switching?")
        save_btn = box.addButton("Save", QMessageBox.ButtonRole.AcceptRole)
        discard_btn = box.addButton("Discard", QMessageBox.ButtonRole.DestructiveRole)
        cancel_btn = box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(save_btn)
        box.exec()
        clicked = box.clickedButton()
        if clicked is save_btn:
            self._save_inspector_metadata(silent_refresh=True, target_ctx=self._insp_target_ctx)
            return True
        if clicked is discard_btn:
            self._meta_dirty = False
            self._update_metadata_save_ui()
            return True
        if clicked is cancel_btn or clicked is None:
            return False
        return False

    def _refresh_reaper_status_pill(self) -> None:
        st = self.reaper_bridge.get_state()
        if st.connected:
            ver_txt = f" ({st.reaper_version})" if st.reaper_version else ""
            self.reaper_status_pill.setText(f"REAPER: Connected{ver_txt}")
        else:
            self.reaper_status_pill.setText("REAPER: Offline")

        rst = self.resolve_bridge.get_state()
        if rst.connected:
            mode_txt = "Studio" if rst.mode == "studio_api" else "Bridge"
            self.resolve_status_pill.setText(f"Resolve: {mode_txt}")
        else:
            self.resolve_status_pill.setText("Resolve: Offline")

        fst = self.filmcraft_bridge.get_state(query_live=False)
        if fst.connected:
            self.filmcraft_status_pill.setText(f"FilmCraft: Live (:{fst.control_port})")
        else:
            self.filmcraft_status_pill.setText("FilmCraft: Offline")

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
        tooltip_lines = [
            f"• {self.reaper_status_pill.text()}",
            f"• {self.resolve_status_pill.text()}",
            f"• {self.filmcraft_status_pill.text()}",
            "",
            "Click to manage DCC Integrations",
        ]
        self.dcc_status_pill.setToolTip("\n".join(tooltip_lines))
        _repolish(self.dcc_status_pill)
        self._refresh_dcc_target_ui()

    def _update_fast_drop_button(self) -> None:
        on = self.hub.config.fast_drop
        self.fast_drop_btn.setText("Fast Drop: ON" if on else "Fast Drop: OFF")
        self.fast_drop_btn.setIcon(get_icon("bolt", "#fde68a" if on else "#8e8e98"))
        self.fast_drop_btn.setObjectName("FastDropActive" if on else "GhostButton")
        _repolish(self.fast_drop_btn)

    def _toggle_fast_drop(self) -> None:
        self.hub.config.fast_drop = not self.hub.config.fast_drop
        self.hub.save_config()
        self._update_fast_drop_button()
        state = "ON (auto-ingest on drop)" if self.hub.config.fast_drop else "OFF (prompt on drop)"
        self.show_status_message(f"Fast Drop: {state}")

    def _toggle_auto_save_metadata(self, checked: bool) -> None:
        self.hub.config.auto_save_metadata = bool(checked)
        self.hub.save_config()
        if checked and self._meta_dirty:
            self._save_inspector_metadata(silent_refresh=True)
        self._update_metadata_save_ui()
        state = "ON" if checked else "OFF (use Ctrl+S to save)"
        self.show_status_message(f"Inspector Auto-Save: {state}")

    def refresh_all(self) -> None:
        if hasattr(self, "auto_save_meta_action"):
            self.auto_save_meta_action.blockSignals(True)
            self.auto_save_meta_action.setChecked(self.hub.config.auto_save_metadata)
            self.auto_save_meta_action.blockSignals(False)
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
        proot = self.active_project_root

        def _do_reconcile() -> None:
            if proot and proot.is_dir():
                Reconciler.reconcile_project(proot)

        def _done(_res: object) -> None:
            self.refresh_all()
            self.show_status_message("✓ Synced & refreshed from disk")

        run_with_busy_state(
            self,
            _do_reconcile,
            on_success=_done,
            disable_widgets=[self.refresh_btn],
            busy_message="Syncing & reconciling from disk…",
            status_callback=self.show_status_message,
        )

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
        if proj_name and proj_name != self.active_project_name:
            self._flush_pending_metadata_if_autosave()
            if not self._confirm_discard_or_save_dirty_metadata():
                self.project_combo.blockSignals(True)
                self.project_combo.setCurrentText(self.active_project_name)
                self.project_combo.blockSignals(False)
                return
            self.active_project_name = proj_name
            self.active_component_path = ""
            self.hub.set_last_active_project(proj_name)
            self._load_active_project()

    def _clear_component_triad(self) -> None:
        self.active_component_path = ""
        self.active_snapshot = None
        self.selected_repo_asset_path = ""
        self.selected_deliverable_path = ""
        self.comp_breadcrumb_btn.setText("No component selected")
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
        self.workspace_stack.setCurrentIndex(0)
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
            item.setFont(1, self._mono_font)
            item.setForeground(1, QColor("#71717a"))
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
        self.component_tree.set_filter_active(bool(needle), text)

    def _filter_working_list(self, text: str) -> None:
        needle = text.strip().lower()
        for i in range(self.working_list.topLevelItemCount()):
            it = self.working_list.topLevelItem(i)
            hay = " ".join(it.text(c).lower() for c in range(4))
            it.setHidden(bool(needle) and needle not in hay)
        self.working_list.set_filter_active(bool(needle), text)

    def _filter_repo_asset_tree(self, text: str) -> None:
        needle = text.strip().lower()

        def _visit(item: QTreeWidgetItem) -> bool:
            kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if kind == "repo_asset":
                apath = str(item.data(0, Qt.ItemDataRole.UserRole) or "").lower()
                visible = (not needle) or (needle in apath) or (needle in item.text(0).lower())
                item.setHidden(not visible)
                return visible
            any_child = False
            for idx in range(item.childCount()):
                if _visit(item.child(idx)):
                    any_child = True
            gpath = str(item.data(0, Qt.ItemDataRole.UserRole) or "").lower()
            visible = any_child or (bool(needle) and needle in gpath)
            item.setHidden(not visible)
            if visible and needle:
                item.setExpanded(True)
            return visible

        for i in range(self.repo_asset_tree.topLevelItemCount()):
            _visit(self.repo_asset_tree.topLevelItem(i))
        self.repo_asset_tree.set_filter_active(bool(needle), text)

    def _filter_published_deliverable_tree(self, text: str) -> None:
        needle = text.strip().lower()

        def _visit(item: QTreeWidgetItem) -> bool:
            kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if kind == "deliverable":
                dpath = str(item.data(0, Qt.ItemDataRole.UserRole) or "").lower()
                visible = (not needle) or (needle in dpath) or (needle in item.text(0).lower())
                item.setHidden(not visible)
                return visible
            any_child = False
            for idx in range(item.childCount()):
                if _visit(item.child(idx)):
                    any_child = True
            gpath = str(item.data(0, Qt.ItemDataRole.UserRole) or "").lower()
            visible = any_child or (bool(needle) and needle in gpath)
            item.setHidden(not visible)
            if visible and needle:
                item.setExpanded(True)
            return visible

        for i in range(self.published_deliverable_tree.topLevelItemCount()):
            _visit(self.published_deliverable_tree.topLevelItem(i))
        self.published_deliverable_tree.set_filter_active(bool(needle), text)

    def _open_active_component_folder(self) -> None:
        if self.active_project_root and self.active_component_path:
            _open_folder_in_os(self.active_project_root / self.active_component_path)
        elif self.active_project_root:
            _open_folder_in_os(self.active_project_root)

    def _copy_active_component_path(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        cb = QApplication.clipboard()
        if cb is not None:
            cb.setText(str(self.active_project_root / self.active_component_path))
            self.show_status_message("✓ Copied component path")

    def _populate_group_overview(self, group_prefix: str) -> None:
        self.group_title_label.setText(f"{group_prefix}/")
        self.group_children_list.clear()
        if not self.active_project_root:
            return
        prefix_slash = f"{group_prefix}/"
        for comp in discover_project_components(self.active_project_root):
            if comp.startswith(prefix_slash):
                snap = load_component_snapshot(self.active_project_root, comp)
                rel = comp[len(prefix_slash) :]
                it = QListWidgetItem(f"{rel}   [{snap.software}]")
                it.setData(Qt.ItemDataRole.UserRole, comp)
                self.group_children_list.addItem(it)

    def _on_component_tree_selected(self, current: Optional[QTreeWidgetItem], prev: Optional[QTreeWidgetItem]) -> None:
        if current is None or self.active_project_root is None:
            return
        self._flush_pending_metadata_if_autosave()
        if prev is not None and not self._confirm_discard_or_save_dirty_metadata():
            self.component_tree.blockSignals(True)
            self.component_tree.setCurrentItem(prev)
            self.component_tree.blockSignals(False)
            return
        kind = current.data(0, Qt.ItemDataRole.UserRole + 1)
        if kind == "group":
            self._selected_group_prefix = str(current.data(0, Qt.ItemDataRole.UserRole) or "")
            self._selected_repo_category = ""
            self._selected_deliv_category = ""
            self._populate_group_overview(self._selected_group_prefix)
            self.workspace_stack.setCurrentIndex(1)
            self._sync_inspector_to_active_tab()
            return
        comp_path = current.data(0, Qt.ItemDataRole.UserRole)
        if not comp_path:
            return
        self._selected_group_prefix = ""
        self._selected_repo_category = ""
        self._selected_deliv_category = ""
        self.workspace_stack.setCurrentIndex(0)
        self.active_component_path = str(comp_path)
        full_path = self.active_project_root / self.active_component_path
        self.comp_breadcrumb_btn.setText(self.active_component_path.replace("/", " / "))
        self.comp_breadcrumb_btn.setToolTip(f"{full_path}\nClick to open in OS Explorer")
        self.active_path_btn.setText(f"📁 {self.active_project_name} / {self.active_component_path}   ({full_path})")
        self.active_path_label.setText(str(full_path))
        self._refresh_component_triad()

    def _refresh_component_triad(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        snap = load_component_snapshot(self.active_project_root, self.active_component_path)
        self.active_snapshot = snap
        self.comp_breadcrumb_btn.setText(self.active_component_path.replace("/", " / "))

        self.insp_software_combo.blockSignals(True)
        sw_idx = self.insp_software_combo.findData(snap.software)
        if sw_idx >= 0:
            self.insp_software_combo.setCurrentIndex(sw_idx)
        else:
            self.insp_software_combo.setCurrentText(snap.software)
        self.insp_software_combo.blockSignals(False)
        self._update_software_exe_hint(snap.software)
        self._sync_software_action_visibility(snap.software)

        # Populate Working tab (multi-column)
        prev_working_ver = self._selected_working_version()
        self.working_list.blockSignals(True)
        self.working_list.clear()
        latest_w_ver = max((w.version for w in snap.working_versions), default=0)
        selected_w_item: Optional[QTreeWidgetItem] = None
        for w in reversed(snap.working_versions):
            primary_abs = self.active_project_root / self.active_component_path / w.primary_file
            is_locked = primary_abs.is_file() and not os.access(primary_abs, os.W_OK)
            is_latest = w.version == latest_w_ver
            latest_badge = " ★" if is_latest else ""
            note_txt = w.comment
            if w.tags:
                note_txt = f"{note_txt}  [{', '.join(w.tags)}]" if note_txt else f"[{', '.join(w.tags)}]"
            item = QTreeWidgetItem(
                self.working_list,
                [
                    f"{w.version_label}{latest_badge}",
                    Path(w.primary_file).name if w.primary_file else "—",
                    _format_timestamp(w.created_at),
                    note_txt,
                ],
            )
            if is_locked:
                item.setIcon(0, get_icon("lock", TEXT_MUTED, ICON_SM))
            item.setData(0, Qt.ItemDataRole.UserRole, w.version)
            item.setFont(0, self._mono_font)
            item.setForeground(0, QColor("#93c5fd" if is_latest else "#e4e4e7"))
            item.setForeground(2, QColor("#8e8e98"))
            item.setForeground(3, QColor("#a1a1aa"))
            item.setToolTip(0, f"{w.folder_rel}/{Path(w.primary_file).name}")
            if selected_w_item is None or w.version == prev_working_ver:
                selected_w_item = item
        self.working_list.blockSignals(False)
        if selected_w_item is not None:
            self.working_list.setCurrentItem(selected_w_item)
        if self.working_filter_edit.text().strip():
            self._filter_working_list(self.working_filter_edit.text())
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
            aitem.setFont(1, self._mono_font)
            aitem.setForeground(1, QColor("#8e8e98"))
            aitem.setForeground(2, QColor("#8e8e98"))
            aitem.setToolTip(0, f"repo/{apath}/")
            if first_asset_item is None or apath == self.selected_repo_asset_path:
                first_asset_item = aitem
        self.repo_asset_tree.expandAll()
        self.repo_asset_tree.blockSignals(False)
        if self.repo_filter_edit.text().strip():
            self._filter_repo_asset_tree(self.repo_filter_edit.text())
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
            ditem.setFont(1, self._mono_font)
            ditem.setForeground(1, QColor("#8e8e98"))
            ditem.setToolTip(0, f"published/{dpath}/")
            if first_deliv_item is None or dpath == self.selected_deliverable_path:
                first_deliv_item = ditem
        if snap.published_versions:
            total_pub_releases += len(snap.published_versions)
            litem = QTreeWidgetItem(self.published_deliverable_tree, ["(legacy)", f"{len(snap.published_versions)}v"])
            litem.setData(0, Qt.ItemDataRole.UserRole, "__legacy__")
            litem.setData(0, Qt.ItemDataRole.UserRole + 1, "deliverable")
            litem.setFont(1, self._mono_font)
            litem.setForeground(1, QColor("#8e8e98"))
            if first_deliv_item is None or self.selected_deliverable_path == "__legacy__":
                first_deliv_item = litem
        self.published_deliverable_tree.expandAll()
        self.published_deliverable_tree.blockSignals(False)
        if self.pub_filter_edit.text().strip():
            self._filter_published_deliverable_tree(self.pub_filter_edit.text())

        self.triad_tabs.setTabText(2, f"Published ({total_pub_releases})")
        if first_deliv_item is not None:
            self.published_deliverable_tree.setCurrentItem(first_deliv_item)
            self._on_published_deliverable_selected(first_deliv_item, None)
        else:
            self.selected_deliverable_path = ""
            self.published_version_tree.clear()
            self.pub_ver_header.setText("Releases in published/")

        self._sync_inspector_to_active_tab()

    def _on_triad_tab_changed(self, idx: int) -> None:
        self._flush_pending_metadata_if_autosave()
        if idx != self._last_triad_tab_idx and not self._confirm_discard_or_save_dirty_metadata():
            self.triad_tabs.blockSignals(True)
            self.triad_tabs.setCurrentIndex(self._last_triad_tab_idx)
            self.triad_tabs.blockSignals(False)
            return
        self._last_triad_tab_idx = idx
        self._sync_inspector_to_active_tab()

    def _on_repo_asset_selected(self, current: Optional[QTreeWidgetItem], prev: Optional[QTreeWidgetItem]) -> None:
        if current is None or self.active_snapshot is None:
            return
        self._flush_pending_metadata_if_autosave()
        if prev is not None and prev is not current and not self._confirm_discard_or_save_dirty_metadata():
            self.repo_asset_tree.blockSignals(True)
            self.repo_asset_tree.setCurrentItem(prev)
            self.repo_asset_tree.blockSignals(False)
            return
        if current.data(0, Qt.ItemDataRole.UserRole + 1) == "repo_category":
            cat = str(current.data(0, Qt.ItemDataRole.UserRole) or "")
            self.selected_repo_asset_path = ""
            self._selected_repo_category = cat
            self.repo_version_list.blockSignals(True)
            self.repo_version_list.clear()
            self.repo_version_list.blockSignals(False)
            self.repo_ver_header.setText(f"Category: {cat}/")
            self._sync_inspector_to_active_tab()
            return
        apath = str(current.data(0, Qt.ItemDataRole.UserRole) or "")
        if not apath:
            return
        self._selected_repo_category = ""
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
            item.setFont(0, self._mono_font)
            item.setFont(2, self._mono_font)
            item.setForeground(2, QColor("#8e8e98"))
            item.setForeground(3, QColor("#a1a1aa"))
            self.repo_version_list.addTopLevelItem(item)
        self.repo_version_list.blockSignals(False)
        if self.repo_version_list.count() > 0:
            self.repo_version_list.setCurrentRow(0)
        self._sync_inspector_to_active_tab()

    def _on_repo_version_selected(self, current: Optional[QTreeWidgetItem], prev: Optional[QTreeWidgetItem]) -> None:
        self._flush_pending_metadata_if_autosave()
        if prev is not None and prev is not current and not self._confirm_discard_or_save_dirty_metadata():
            self.repo_version_list.blockSignals(True)
            self.repo_version_list.setCurrentItem(prev)
            self.repo_version_list.blockSignals(False)
            return
        self._sync_inspector_to_active_tab()

    def _on_working_version_selected(self, current: Optional[QTreeWidgetItem], prev: Optional[QTreeWidgetItem]) -> None:
        self._flush_pending_metadata_if_autosave()
        if prev is not None and prev is not current and not self._confirm_discard_or_save_dirty_metadata():
            self.working_list.blockSignals(True)
            self.working_list.setCurrentItem(prev)
            self.working_list.blockSignals(False)
            return
        self._sync_inspector_to_active_tab()

    def _on_published_deliverable_selected(
        self, current: Optional[QTreeWidgetItem], prev: Optional[QTreeWidgetItem]
    ) -> None:
        if current is None or self.active_snapshot is None:
            return
        self._flush_pending_metadata_if_autosave()
        if prev is not None and prev is not current and not self._confirm_discard_or_save_dirty_metadata():
            self.published_deliverable_tree.blockSignals(True)
            self.published_deliverable_tree.setCurrentItem(prev)
            self.published_deliverable_tree.blockSignals(False)
            return
        if current.data(0, Qt.ItemDataRole.UserRole + 1) == "deliv_category":
            cat = str(current.data(0, Qt.ItemDataRole.UserRole) or "")
            self.selected_deliverable_path = ""
            self._selected_deliv_category = cat
            self.published_version_tree.blockSignals(True)
            self.published_version_tree.clear()
            self.published_version_tree.blockSignals(False)
            self.pub_ver_header.setText(f"Category: {cat}/")
            self._sync_inspector_to_active_tab()
            return
        self._selected_deliv_category = ""
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
            ver_item.setForeground(1, QColor("#93c5fd"))
            ver_item.setForeground(2, QColor("#8e8e98"))
            ver_item.setForeground(3, QColor("#a1a1aa"))
            if first_ver_item is None:
                first_ver_item = ver_item
            for tf in pv.files:
                ftype = (tf.file_type or Path(tf.rel_path).suffix.lstrip(".")).upper()
                child = QTreeWidgetItem(
                    ver_item,
                    [Path(tf.rel_path).name, ftype, _format_size(tf.size_bytes), tf.output_label],
                )
                child.setData(0, Qt.ItemDataRole.UserRole, ("file", dpath, pv.version, tf.rel_path))
                child.setFont(2, self._mono_font)
                child.setForeground(1, QColor("#8e8e98"))
                child.setForeground(2, QColor("#8e8e98"))
        self.published_version_tree.expandAll()
        self.published_version_tree.blockSignals(False)
        if first_ver_item is not None:
            self.published_version_tree.setCurrentItem(first_ver_item)
        self._sync_inspector_to_active_tab()

    def _on_published_version_selected(
        self, current: Optional[QTreeWidgetItem], prev: Optional[QTreeWidgetItem]
    ) -> None:
        self._flush_pending_metadata_if_autosave()
        if prev is not None and prev is not current and not self._confirm_discard_or_save_dirty_metadata():
            self.published_version_tree.blockSignals(True)
            self.published_version_tree.setCurrentItem(prev)
            self.published_version_tree.blockSignals(False)
            return
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
        for widget in (self.insp_author_edit, self.insp_source_edit, self.insp_url_container, self.insp_license_combo):
            self.meta_form.setRowVisible(widget, visible)

    def _collect_project_custom_keys(self) -> list[str]:
        keys: set[str] = set()
        snap = self.active_snapshot
        if snap is not None:
            for asset in snap.repo_assets.values():
                keys.update(str(k) for k in asset.custom.keys())
            for w in snap.working_versions:
                keys.update(str(k) for k in w.custom.keys())
            for deliv in snap.published_deliverables.values():
                for pv in deliv.versions:
                    keys.update(str(k) for k in pv.custom.keys())
        return sorted(keys)

    def _open_insp_url(self) -> None:
        url_txt = self.insp_url_edit.text().strip()
        if url_txt:
            QDesktopServices.openUrl(QUrl.fromUserInput(url_txt))

    def _clear_inspector_metadata_fields(self, suggested_keys: list[str]) -> None:
        self.insp_author_edit.clear()
        self.insp_source_edit.clear()
        self.insp_url_edit.clear()
        self.insp_license_combo.setCurrentText("")
        self.insp_tags_edit.clear()
        self.insp_comment_edit.clear()
        self.insp_custom_meta.set_data({}, suggested_keys=suggested_keys)
        self.insp_custom_meta.set_enabled_editing(False)
        self.save_meta_btn.setEnabled(False)
        self.meta_state_label.setText("")

    def _sync_inspector_to_active_tab(self) -> None:
        tab_idx = self.triad_tabs.currentIndex()
        snap = self.active_snapshot
        suggested_keys = self._collect_project_custom_keys()

        self._meta_loading = True
        self._meta_dirty = False
        self._meta_autosave_timer.stop()
        try:
            if self._selected_group_prefix:
                self._insp_target_ctx = None
                self._set_repo_fields_visible(False)
                child_count = self.group_children_list.count()
                self.insp_context_badge.setText("Group")
                self.selected_item_label.setText(f"{self._selected_group_prefix}/")
                self.insp_lineage_label.setText(f"{child_count} component(s) in group")
                self._clear_inspector_metadata_fields(suggested_keys)
                self.launch_btn.setEnabled(False)
                self.launch_btn.setText("Select a component to open")
                self.working_actions_box.setVisible(False)
                self.repo_actions_box.setVisible(False)
                self.published_actions_box.setVisible(False)
                self.reaper_inc_save_btn.setVisible(False)
                self.reaper_inc_save_btn.setEnabled(False)
                self.pub_from_working_btn.setEnabled(False)
                self.open_explorer_btn.setEnabled(False)
                self.copy_path_btn.setEnabled(False)
                self.delete_selected_btn.setEnabled(False)
                self.delete_selected_btn.setText("Delete Selected…")
                self.collect_media_btn.setVisible(False)
                self.reaper_update_media_btn.setVisible(False)
                self._update_metadata_save_ui()
                self._refresh_dcc_target_ui(refresh_bridges=False)
                return

            if snap is None or not self.active_project_root or not self.active_component_path:
                self._insp_target_ctx = None
                self._set_repo_fields_visible(False)
                self.insp_context_badge.setText("Idle")
                self.selected_item_label.setText("No component selected")
                self.insp_lineage_label.setText("")
                self._clear_inspector_metadata_fields(suggested_keys)
                self.launch_btn.setEnabled(False)
                self.working_actions_box.setVisible(False)
                self.repo_actions_box.setVisible(False)
                self.published_actions_box.setVisible(False)
                self.reaper_inc_save_btn.setVisible(False)
                self.reaper_inc_save_btn.setEnabled(False)
                self.pub_from_working_btn.setEnabled(False)
                self.open_explorer_btn.setEnabled(False)
                self.copy_path_btn.setEnabled(False)
                self.delete_selected_btn.setEnabled(False)
                self.delete_selected_btn.setText("Delete Selected…")
                self.collect_media_btn.setVisible(False)
                self.reaper_update_media_btn.setVisible(False)
                self._update_metadata_save_ui()
                self._refresh_dcc_target_ui(refresh_bridges=False)
                return

            prof = self.hub.get_software_profile(snap.software)
            latest_w = max((w.version for w in snap.working_versions), default=0)
            self.reaper_inc_save_btn.setEnabled(snap.software == "reaper")
            self.reaper_inc_save_btn.setText(f"Increment Save (→ v{latest_w + 1:03d})")
            self.open_explorer_btn.setEnabled(True)
            self.copy_path_btn.setEnabled(True)
            self._sync_software_action_visibility(snap.software)

            self.working_actions_box.setVisible(tab_idx == 0)
            self.repo_actions_box.setVisible(tab_idx == 1)
            self.published_actions_box.setVisible(tab_idx == 2)

            if tab_idx == 0:
                # Working Tab
                self._set_repo_fields_visible(False)
                self.insp_context_badge.setText("Working Version")
                self.insp_comment_label.setText("Note")
                ver = self._selected_working_version()
                bundle = next((v for v in snap.working_versions if v.version == ver), None) if ver is not None else None
                if bundle is None:
                    self._insp_target_ctx = None
                    self.selected_item_label.setText("No working version selected")
                    self.insp_lineage_label.setText("Click 'Version Up' or drop files to create working/v001/")
                    self._clear_inspector_metadata_fields(suggested_keys)
                    self.launch_btn.setEnabled(False)
                    self.pub_from_working_btn.setEnabled(False)
                    self.save_seed_btn.setEnabled(False)
                    self.delete_selected_btn.setEnabled(bool(self.active_component_path))
                    self.delete_selected_btn.setText("Delete Component…")
                else:
                    self._insp_target_ctx = (0, self.active_project_root, self.active_component_path, bundle.version)
                    self.selected_item_label.setText(f"{bundle.version_label}  •  {Path(bundle.primary_file).name}")
                    parent_txt = f"Branched from v{bundle.parent_version:03d}" if bundle.parent_version else "Initial version"
                    self.insp_lineage_label.setText(f"{parent_txt}  •  {_format_timestamp(bundle.created_at)}")
                    self.insp_tags_edit.setText(", ".join(bundle.tags))
                    self.insp_comment_edit.setText(bundle.comment)
                    self.insp_custom_meta.set_data(bundle.custom, suggested_keys=suggested_keys)
                    self.insp_custom_meta.set_enabled_editing(True)
                    self.save_meta_btn.setEnabled(True)
                    self.launch_btn.setEnabled(True)
                    self.launch_btn.setText(f"Open {bundle.version_label} in {prof.display_name}")
                    self.pub_from_working_btn.setEnabled(True)
                    self.pub_from_working_btn.setText(f"Publish from {bundle.version_label}…")
                    self.save_seed_btn.setEnabled(True)
                    self.delete_selected_btn.setEnabled(True)
                    self.delete_selected_btn.setText(f"Delete {bundle.version_label}…")
                    self.meta_state_label.setText("✓ Saved" if self.hub.config.auto_save_metadata else "")

            elif tab_idx == 1:
                # Repo Assets Tab
                if self._selected_repo_category:
                    self._insp_target_ctx = None
                    self._set_repo_fields_visible(False)
                    cat = self._selected_repo_category.strip("/")
                    prefix = f"{cat}/"
                    cat_count = sum(1 for ap in snap.repo_assets if ap.startswith(prefix))
                    self.insp_context_badge.setText("Category")
                    self.selected_item_label.setText(f"repo/{cat}/")
                    self.insp_lineage_label.setText(f"{cat_count} asset(s) in category")
                    self._clear_inspector_metadata_fields(suggested_keys)
                    self.repo_new_ver_btn.setEnabled(False)
                    self.launch_btn.setEnabled(False)
                    self.launch_btn.setText("Select an asset version")
                    self.delete_selected_btn.setEnabled(False)
                    self.delete_selected_btn.setText("Delete Selected…")
                    self._update_metadata_save_ui()
                    self._refresh_dcc_target_ui(refresh_bridges=False)
                    return

                self._set_repo_fields_visible(True)
                self.insp_context_badge.setText("Repo Asset")
                self.insp_comment_label.setText("Note")
                asset = snap.repo_assets.get(self.selected_repo_asset_path) if self.selected_repo_asset_path else None
                if asset is None:
                    self._insp_target_ctx = None
                    self.selected_item_label.setText("No repo asset selected")
                    self.insp_lineage_label.setText("Select or drop an asset to inspect provenance & credits")
                    self._clear_inspector_metadata_fields(suggested_keys)
                    self.repo_new_ver_btn.setEnabled(False)
                    self.launch_btn.setEnabled(bool(latest_w))
                    self.launch_btn.setText(f"Open Latest Working (v{latest_w:03d})" if latest_w else "Launch Working File")
                    self.delete_selected_btn.setEnabled(False)
                    self.delete_selected_btn.setText("Delete Selected…")
                else:
                    ver = self._selected_repo_version()
                    bundle = next((v for v in asset.versions if v.version == ver), None) if ver is not None else None
                    self._insp_target_ctx = (1, self.active_project_root, self.active_component_path, asset.asset_path, ver)
                    used_txt = (
                        ", ".join(f"v{u:03d}" for u in asset.used_by_working_versions)
                        if asset.used_by_working_versions
                        else "Not linked to a working version yet"
                    )
                    self.insp_author_edit.setText(asset.author)
                    self.insp_source_edit.setText(asset.source)
                    self.insp_url_edit.setText(asset.url)
                    self.insp_license_combo.setCurrentText(asset.license)
                    self.insp_tags_edit.setText(", ".join(asset.tags))
                    self.insp_custom_meta.set_data(asset.custom, suggested_keys=suggested_keys)
                    self.insp_custom_meta.set_enabled_editing(True)
                    self.repo_new_ver_btn.setEnabled(True)
                    if bundle is not None:
                        self.selected_item_label.setText(f"repo/{asset.asset_path}/{bundle.version_label}/")
                        self.insp_lineage_label.setText(f"Used in working: {used_txt}")
                        self.insp_comment_edit.setText(bundle.comment)
                        self.launch_btn.setEnabled(True)
                        self.launch_btn.setText(f"Open {bundle.version_label} File")
                        self.delete_selected_btn.setEnabled(True)
                        self.delete_selected_btn.setText(f"Delete {asset.name} {bundle.version_label}…")
                    else:
                        self.selected_item_label.setText(f"repo/{asset.asset_path}/ (Empty slot)")
                        self.insp_lineage_label.setText("Drop a file into the version list to populate v001")
                        self.insp_comment_edit.clear()
                        self.launch_btn.setEnabled(bool(latest_w))
                        self.launch_btn.setText(f"Open Latest Working (v{latest_w:03d})" if latest_w else "Launch Working File")
                        self.delete_selected_btn.setEnabled(True)
                        self.delete_selected_btn.setText(f"Delete Asset '{asset.name}'…")
                    self.save_meta_btn.setEnabled(True)
                    self.meta_state_label.setText("✓ Saved" if self.hub.config.auto_save_metadata else "")

            else:
                # Published Tab
                if self._selected_deliv_category:
                    self._insp_target_ctx = None
                    self._set_repo_fields_visible(False)
                    cat = self._selected_deliv_category.strip("/")
                    prefix = f"{cat}/"
                    cat_count = sum(1 for dp in snap.published_deliverables if dp.startswith(prefix))
                    self.insp_context_badge.setText("Category")
                    self.selected_item_label.setText(f"published/{cat}/")
                    self.insp_lineage_label.setText(f"{cat_count} deliverable(s) in category")
                    self._clear_inspector_metadata_fields(suggested_keys)
                    self.pub_attach_btn.setEnabled(False)
                    self.launch_btn.setEnabled(False)
                    self.launch_btn.setText("Select a release to open")
                    self.delete_selected_btn.setEnabled(False)
                    self.delete_selected_btn.setText("Delete Selected…")
                    self._update_metadata_save_ui()
                    self._refresh_dcc_target_ui(refresh_bridges=False)
                    return

                self._set_repo_fields_visible(False)
                self.insp_context_badge.setText("Published")
                self.insp_comment_label.setText("Note")
                target = self._selected_published_target()
                if target is None:
                    self._insp_target_ctx = None
                    if self.selected_deliverable_path and self.selected_deliverable_path != "__legacy__":
                        self.selected_item_label.setText(f"published/{self.selected_deliverable_path}/")
                        self.insp_lineage_label.setText("Click 'Publish…' or drop exports to create the next release")
                        self.delete_selected_btn.setEnabled(True)
                        self.delete_selected_btn.setText(f"Delete '{self.selected_deliverable_path}'…")
                    else:
                        self.selected_item_label.setText("No published release selected")
                        self.insp_lineage_label.setText("")
                        self.delete_selected_btn.setEnabled(False)
                        self.delete_selected_btn.setText("Delete Selected…")
                    self._clear_inspector_metadata_fields(suggested_keys)
                    self.pub_attach_btn.setEnabled(False)
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
                        self._insp_target_ctx = None
                        self._clear_inspector_metadata_fields(suggested_keys)
                        self.pub_attach_btn.setEnabled(False)
                        self.delete_selected_btn.setEnabled(False)
                    else:
                        self._insp_target_ctx = (2, self.active_project_root, self.active_component_path, dpath, ver)
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
                        self.insp_custom_meta.set_data(bundle.custom, suggested_keys=suggested_keys)
                        self.insp_custom_meta.set_enabled_editing(True)
                        self.save_meta_btn.setEnabled(True)
                        self.pub_attach_btn.setEnabled(dpath != "__legacy__")
                        self.delete_selected_btn.setEnabled(True)
                        self.delete_selected_btn.setText(f"Delete {dpath} {bundle.version_label}…")
                        self.meta_state_label.setText("✓ Saved" if self.hub.config.auto_save_metadata else "")
        finally:
            self._meta_loading = False
            self._update_metadata_save_ui()

        self._refresh_dcc_target_ui(refresh_bridges=False)

    def _update_software_exe_hint(self, sw_key: str) -> None:
        prof = self.hub.get_software_profile(sw_key)
        if prof.executable:
            exe_p = Path(prof.executable).expanduser()
            if exe_p.is_file():
                self.sw_exe_hint_label.setText("Ready")
                self.sw_exe_hint_label.setObjectName("SuccessText")
                self.sw_exe_hint_label.setToolTip(f"Executable ready: {exe_p}")
            else:
                self.sw_exe_hint_label.setText("Missing Exe")
                self.sw_exe_hint_label.setObjectName("WarningText")
                self.sw_exe_hint_label.setToolTip(f"Missing executable: {prof.executable}")
        else:
            self.sw_exe_hint_label.setText("OS Default")
            self.sw_exe_hint_label.setObjectName("MutedText")
            self.sw_exe_hint_label.setToolTip("Uses OS default app (configure executable in Settings → Software Profiles)")
        repolish(self.sw_exe_hint_label)

    def _sync_software_action_visibility(self, sw_key: str) -> None:
        self.collect_media_btn.setVisible(sw_key in ("reaper", "filmcraft"))
        if sw_key == "filmcraft":
            self.collect_media_btn.setText("Scan && Collect External .fcproj Media")
        else:
            self.collect_media_btn.setText("Scan && Collect External .rpp Media")
        self.reaper_inc_save_btn.setVisible(sw_key == "reaper")
        self.reaper_update_media_btn.setVisible(sw_key == "reaper")

    def _on_component_software_changed(self, _idx: int) -> None:
        if not self.active_project_root or not self.active_snapshot:
            return
        sw_key = str(self.insp_software_combo.currentData() or self.insp_software_combo.currentText())
        if not sw_key:
            return
        self.active_snapshot.software = sw_key
        save_component_snapshot(self.active_project_root, self.active_snapshot)
        self._update_software_exe_hint(sw_key)
        self._sync_software_action_visibility(sw_key)

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

    def _update_copy_paste_buttons(self) -> None:
        has_target = self._insp_target_ctx is not None
        self.copy_meta_btn.setEnabled(has_target)
        payload = self._read_copied_metadata_payload(check_clipboard=False)
        can_paste = bool(payload) and (
            has_target or (self.triad_tabs.currentIndex() == 1 and bool(self._selected_repo_asset_paths()))
        )
        self.paste_meta_btn.setEnabled(can_paste)
        if payload:
            src_lbl = str(payload.get("source_label") or payload.get("source_kind") or "copied item")
            self.paste_meta_btn.setToolTip(f"Paste metadata from {src_lbl} (Ctrl+Shift+V)")
        else:
            self.paste_meta_btn.setToolTip("Paste copied metadata to selected item(s) (Ctrl+Shift+V)")

    def _update_metadata_save_ui(self) -> None:
        auto_on = self.hub.config.auto_save_metadata
        has_target = self._insp_target_ctx is not None
        self._update_copy_paste_buttons()
        if not has_target:
            self.meta_state_label.setText("")
            self.save_meta_btn.setVisible(not auto_on)
            self.save_meta_btn.setEnabled(False)
            self.save_meta_btn.setObjectName("")
            _repolish(self.save_meta_btn)
            return

        if self._meta_dirty:
            self.meta_state_label.setText("● Unsaved")
            self.meta_state_label.setObjectName("MetaDirtyBadge")
            self.save_meta_btn.setText("● Save Metadata (Ctrl+S)")
            self.save_meta_btn.setObjectName("PrimaryButton" if not auto_on else "")
            self.save_meta_btn.setVisible(True)
            self.save_meta_btn.setEnabled(True)
        else:
            self.meta_state_label.setText("✓ Saved" if auto_on else "")
            self.meta_state_label.setObjectName("MetaSavedBadge")
            self.save_meta_btn.setText("Save Metadata (Ctrl+S)")
            self.save_meta_btn.setObjectName("")
            self.save_meta_btn.setVisible(not auto_on)
            self.save_meta_btn.setEnabled(True)
        _repolish(self.meta_state_label)
        _repolish(self.save_meta_btn)

    def _on_metadata_field_edited(self, *_args: object) -> None:
        if self._meta_loading or self._insp_target_ctx is None:
            return
        self._meta_dirty = True
        self._update_metadata_save_ui()
        if self.hub.config.auto_save_metadata:
            self._meta_autosave_timer.start()

    def _on_metadata_editing_finished(self) -> None:
        if self._meta_loading or not self._meta_dirty:
            return
        if self.hub.config.auto_save_metadata:
            self._meta_autosave_timer.stop()
            self._save_inspector_metadata(silent_refresh=True)

    def _on_license_activated(self, _idx: int) -> None:
        if self._meta_loading or self._insp_target_ctx is None:
            return
        self._meta_dirty = True
        self._update_metadata_save_ui()
        if self.hub.config.auto_save_metadata:
            self._meta_autosave_timer.stop()
            self._save_inspector_metadata(silent_refresh=True)

    def _on_autosave_timer_fired(self) -> None:
        if self._meta_dirty and self.hub.config.auto_save_metadata:
            self._save_inspector_metadata(silent_refresh=True)

    def _flush_pending_metadata_if_autosave(self) -> None:
        if self._meta_dirty and self.hub.config.auto_save_metadata and self._insp_target_ctx is not None:
            self._meta_autosave_timer.stop()
            self._save_inspector_metadata(silent_refresh=True, target_ctx=self._insp_target_ctx)

    def _update_active_note_in_place(self, tab_idx: int, comment: str, tags: list[str]) -> None:
        """Updates tree item note text in-place during autosave so active QLineEdit focus is preserved."""
        if tab_idx == 0:
            curr = self.working_list.currentItem()
            if curr is not None:
                note_txt = f"{comment}  [{', '.join(tags)}]" if (comment and tags) else (f"[{', '.join(tags)}]" if tags else comment)
                curr.setText(3, note_txt)
        elif tab_idx == 1:
            curr = self.repo_version_list.currentItem()
            if curr is not None:
                curr.setText(3, comment)
        elif tab_idx == 2:
            curr = self.published_version_tree.currentItem()
            if curr is not None:
                data = curr.data(0, Qt.ItemDataRole.UserRole)
                target_item = curr.parent() if (isinstance(data, tuple) and data[0] == "file" and curr.parent() is not None) else curr
                if target_item is not None:
                    target_item.setText(3, comment)

    def _save_inspector_metadata(
        self,
        silent_refresh: bool = False,
        target_ctx: Optional[tuple[Any, ...]] = None,
    ) -> None:
        ctx = target_ctx or self._insp_target_ctx
        tags = [t.strip() for t in self.insp_tags_edit.text().split(",") if t.strip()]
        comment = self.insp_comment_edit.text().strip()
        custom_data = self.insp_custom_meta.get_data(include_hidden=True)

        if ctx is None:
            if not self.active_project_root or not self.active_component_path:
                return
            tab_idx = self.triad_tabs.currentIndex()
            proot = self.active_project_root
            comp_path = self.active_component_path
        else:
            tab_idx = int(ctx[0])
            proot = ctx[1]
            comp_path = str(ctx[2])

        if not proot or not comp_path:
            return

        if tab_idx == 0:
            ver = int(ctx[3]) if (ctx is not None and len(ctx) > 3) else self._selected_working_version()
            if ver is None:
                return
            self.workspace.update_working_metadata(
                project_root=proot,
                component_path=comp_path,
                version=ver,
                comment=comment,
                tags=tags,
                custom=custom_data,
                replace_custom=True,
            )
            self._meta_dirty = False
            if silent_refresh:
                self._update_active_note_in_place(0, comment, tags)
                self._update_metadata_save_ui()
            else:
                self._refresh_component_triad()
                self.show_status_message(f"✓ Saved metadata for working/v{ver:03d}")
            return

        if tab_idx == 1:
            asset_path = str(ctx[3]) if (ctx is not None and len(ctx) > 3) else self.selected_repo_asset_path
            if not asset_path:
                return
            ver = ctx[4] if (ctx is not None and len(ctx) > 4) else self._selected_repo_version()
            self.workspace.update_repo_metadata(
                project_root=proot,
                component_path=comp_path,
                asset_path=asset_path,
                author=self.insp_author_edit.text().strip(),
                source_info=self.insp_source_edit.text().strip(),
                url=self.insp_url_edit.text().strip(),
                license_info=self.insp_license_combo.currentText().strip(),
                tags=tags,
                comment=comment,
                version=ver,
                custom=custom_data,
                replace_custom=True,
            )
            self._meta_dirty = False
            if silent_refresh:
                self._update_active_note_in_place(1, comment, tags)
                self._update_metadata_save_ui()
            else:
                self._refresh_component_triad()
                self.show_status_message(f"✓ Saved metadata for repo/{asset_path}")
            return

        if ctx is not None and len(ctx) >= 5:
            dpath = str(ctx[3])
            ver = int(ctx[4])
        else:
            target = self._selected_published_target()
            if target is None:
                return
            _, dpath, ver, _ = target
        self.workspace.update_published_metadata(
            project_root=proot,
            component_path=comp_path,
            deliverable_name=dpath,
            version=ver,
            comment=comment,
            tags=tags,
            custom=custom_data,
            replace_custom=True,
        )
        self._meta_dirty = False
        if silent_refresh:
            self._update_active_note_in_place(2, comment, tags)
            self._update_metadata_save_ui()
        else:
            self._refresh_component_triad()
            self.show_status_message(f"✓ Saved metadata for published/{dpath}/v{ver:03d}")

    def _selected_repo_asset_paths(self) -> list[str]:
        out: list[str] = []
        for it in self.repo_asset_tree.selectedItems():
            if it.data(0, Qt.ItemDataRole.UserRole + 1) == "repo_asset":
                ap = str(it.data(0, Qt.ItemDataRole.UserRole) or "")
                if ap and ap not in out:
                    out.append(ap)
        if not out and self.selected_repo_asset_path:
            out.append(self.selected_repo_asset_path)
        return out

    def _build_current_metadata_payload(self) -> Optional[dict[str, Any]]:
        if self._insp_target_ctx is None:
            return None
        tab_idx = int(self._insp_target_ctx[0])
        tags = [t.strip() for t in self.insp_tags_edit.text().split(",") if t.strip()]
        comment = self.insp_comment_edit.text().strip()
        custom_visible = self.insp_custom_meta.get_data(include_hidden=False)
        if tab_idx == 1:
            asset_path = str(self._insp_target_ctx[3])
            return {
                "obol_metadata_v1": True,
                "source_kind": "repo",
                "source_label": f"repo/{asset_path}",
                "author": self.insp_author_edit.text().strip(),
                "source": self.insp_source_edit.text().strip(),
                "url": self.insp_url_edit.text().strip(),
                "license": self.insp_license_combo.currentText().strip(),
                "tags": tags,
                "comment": comment,
                "custom": custom_visible,
            }
        if tab_idx == 0:
            ver = int(self._insp_target_ctx[3])
            return {
                "obol_metadata_v1": True,
                "source_kind": "working",
                "source_label": f"working/v{ver:03d}",
                "tags": tags,
                "comment": comment,
                "custom": custom_visible,
            }
        dpath = str(self._insp_target_ctx[3])
        ver = int(self._insp_target_ctx[4])
        return {
            "obol_metadata_v1": True,
            "source_kind": "published",
            "source_label": f"published/{dpath}/v{ver:03d}",
            "tags": tags,
            "comment": comment,
            "custom": custom_visible,
        }

    def _read_copied_metadata_payload(self, check_clipboard: bool = True) -> Optional[dict[str, Any]]:
        if self._copied_metadata is not None:
            return self._copied_metadata
        if not check_clipboard:
            return None
        cb = QApplication.clipboard()
        if cb is None:
            return None
        raw = cb.text().strip()
        if raw.startswith("{"):
            try:
                parsed = json.loads(raw)
                if isinstance(parsed, dict) and parsed.get("obol_metadata_v1"):
                    return parsed
            except Exception:
                return None
        return None

    def _action_copy_metadata(self) -> None:
        payload = self._build_current_metadata_payload()
        if payload is None:
            return
        self._copied_metadata = payload
        cb = QApplication.clipboard()
        if cb is not None and QApplication.platformName() != "offscreen":
            cb.setText(json.dumps(payload, ensure_ascii=False))
        self._update_copy_paste_buttons()
        self.show_status_message(f"✓ Copied metadata from {payload['source_label']}")

    def _paste_metadata_to_repo_assets(self, asset_paths: list[str], merge: bool = False) -> int:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return 0
        payload = self._read_copied_metadata_payload()
        if not payload:
            return 0
        count = 0
        for apath in asset_paths:
            asset = self.active_snapshot.repo_assets.get(apath)
            if asset is None:
                continue
            author_val: Optional[str] = None
            source_val: Optional[str] = None
            url_val: Optional[str] = None
            license_val: Optional[str] = None
            if "author" in payload:
                cand = str(payload.get("author") or "")
                if not merge or not asset.author:
                    author_val = cand
            if "source" in payload:
                cand = str(payload.get("source") or "")
                if not merge or not asset.source:
                    source_val = cand
            if "url" in payload:
                cand = str(payload.get("url") or "")
                if not merge or not asset.url:
                    url_val = cand
            if "license" in payload:
                cand = str(payload.get("license") or "")
                if not merge or not asset.license or asset.license == "Unspecified":
                    license_val = cand

            new_tags = list(asset.tags)
            if isinstance(payload.get("tags"), list):
                p_tags = [str(t).strip() for t in payload["tags"] if str(t).strip()]
                if merge:
                    for t in p_tags:
                        if t not in new_tags:
                            new_tags.append(t)
                else:
                    new_tags = p_tags

            p_custom = {str(k): v for k, v in dict(payload.get("custom") or {}).items() if str(k).strip()}
            if merge:
                merged_custom = dict(asset.custom)
                merged_custom.update(p_custom)
                custom_val = merged_custom
                replace_custom = False
            else:
                custom_val = p_custom
                replace_custom = True

            self.workspace.update_repo_metadata(
                project_root=self.active_project_root,
                component_path=self.active_component_path,
                asset_path=apath,
                author=author_val,
                source_info=source_val,
                url=url_val,
                license_info=license_val,
                tags=new_tags,
                custom=custom_val,
                replace_custom=replace_custom,
            )
            count += 1
        return count

    def _action_paste_metadata(self, merge: bool = False) -> None:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return
        payload = self._read_copied_metadata_payload()
        if not payload:
            self.show_status_message("Copy metadata from an item first (Ctrl+Shift+C)")
            return

        tab_idx = self.triad_tabs.currentIndex()
        if tab_idx == 1:
            selected_assets = self._selected_repo_asset_paths()
            if len(selected_assets) > 1 or (len(selected_assets) == 1 and self._insp_target_ctx is None):
                cnt = self._paste_metadata_to_repo_assets(selected_assets, merge=merge)
                if cnt:
                    self._refresh_component_triad()
                    self.show_status_message(f"✓ Pasted metadata to {cnt} repo asset(s)")
                return

        if self._insp_target_ctx is None:
            return

        if tab_idx == 1:
            cnt = self._paste_metadata_to_repo_assets([str(self._insp_target_ctx[3])], merge=merge)
            if cnt:
                self._refresh_component_triad()
                self.show_status_message(f"✓ Pasted metadata from {payload.get('source_label', 'clipboard')}")
            return

        # Working (tab 0) or Published (tab 2)
        p_tags = [str(t).strip() for t in (payload.get("tags") or []) if str(t).strip()]
        p_comment = str(payload.get("comment") or "").strip()
        p_custom = {str(k): v for k, v in dict(payload.get("custom") or {}).items() if str(k).strip()}

        if tab_idx == 0:
            ver = int(self._insp_target_ctx[3])
            bundle = next((v for v in self.active_snapshot.working_versions if v.version == ver), None)
            if bundle is None:
                return
            tags = list(bundle.tags) if merge else p_tags
            if merge:
                for t in p_tags:
                    if t not in tags:
                        tags.append(t)
            custom_out = dict(bundle.custom) if merge else {}
            if "seed_template" in bundle.custom:
                custom_out["seed_template"] = bundle.custom["seed_template"]
            custom_out.update(p_custom)
            comment_out = bundle.comment if (merge and bundle.comment) else (p_comment or bundle.comment)
            self.workspace.update_working_metadata(
                project_root=self.active_project_root,
                component_path=self.active_component_path,
                version=ver,
                comment=comment_out,
                tags=tags,
                custom=custom_out,
                replace_custom=True,
            )
            self._refresh_component_triad()
            self.show_status_message(f"✓ Pasted metadata to working/v{ver:03d}")
            return

        dpath = str(self._insp_target_ctx[3])
        ver = int(self._insp_target_ctx[4])
        if dpath == "__legacy__":
            bundle = next((v for v in self.active_snapshot.published_versions if v.version == ver), None)
        else:
            deliv = self.active_snapshot.published_deliverables.get(dpath)
            bundle = next((v for v in deliv.versions if v.version == ver), None) if deliv else None
        if bundle is None:
            return
        tags = list(bundle.tags) if merge else p_tags
        if merge:
            for t in p_tags:
                if t not in tags:
                    tags.append(t)
        custom_out = dict(bundle.custom) if merge else {}
        custom_out.update(p_custom)
        comment_out = bundle.comment if (merge and bundle.comment) else (p_comment or bundle.comment)
        self.workspace.update_published_metadata(
            project_root=self.active_project_root,
            component_path=self.active_component_path,
            deliverable_name=dpath,
            version=ver,
            comment=comment_out,
            tags=tags,
            custom=custom_out,
            replace_custom=True,
        )
        self._refresh_component_triad()
        self.show_status_message(f"✓ Pasted metadata to published/{dpath}/v{ver:03d}")

    def _prompt_add_component(self, prefix_override: Optional[str] = None) -> None:
        if not self.active_project_root:
            return
        initial_prefix = prefix_override if prefix_override is not None else self._selected_group_prefix
        dlg = AddComponentDialog(
            self,
            self.hub,
            initial_prefix=initial_prefix,
            project_root=self.active_project_root,
        )
        if dlg.exec() == AddComponentDialog.DialogCode.Accepted:
            vals = dlg.get_values()
            cpath = str(vals["component_path"])
            seed_override = vals.get("seed_override")
            self.workspace.create_component(
                project_root=self.active_project_root,
                component_path=cpath,
                task_template=str(vals["task_template"]),
                software_override=str(vals["software_override"]),
                seed_initial_working=bool(vals["seed_initial_working"]),
                seed_override=str(seed_override) if seed_override else None,
            )
            self.active_component_path = cpath
            self._load_active_project()
            self.show_status_message(f"✓ Created component '{cpath}'")

    def _action_version_up_working(self, source_version: Optional[int] = None) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        self._flush_pending_metadata_if_autosave()
        src_v = source_version if source_version is not None else self._selected_working_version()
        bundle = self.workspace.version_up_working(
            self.active_project_root,
            self.active_component_path,
            comment="",
            source_version=src_v,
        )
        self._refresh_component_triad()
        for i in range(self.working_list.topLevelItemCount()):
            it = self.working_list.topLevelItem(i)
            if it.data(0, Qt.ItemDataRole.UserRole) == bundle.version:
                self.working_list.setCurrentItem(it)
                break
        self.show_status_message(f"✓ Created working/{bundle.version_label}")

    def _action_attach_to_selected_release(self) -> None:
        target = self._selected_published_target()
        if target is not None and target[1] != "__legacy__":
            self._publish_flow([], default_deliverable=target[1], default_target_version=target[2])
        else:
            self._action_publish_deliverable()

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
        self._ingest_working_flow([Path(f) for f in files], allow_fast_drop=False)

    def _handle_working_drop(self, paths: list[Path], _target_item: object) -> None:
        self._ingest_working_flow(paths, allow_fast_drop=True)

    def _ingest_working_flow(self, source_paths: list[Path], allow_fast_drop: bool = False) -> None:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return
        if not source_paths:
            return
        if allow_fast_drop and self.hub.config.fast_drop:
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
        self._ingest_repo_flow(existing_asset_path=None, allow_fast_drop=False)

    def _action_ingest_repo_version(self) -> None:
        if not self.selected_repo_asset_path:
            self._ingest_repo_flow(existing_asset_path=None, allow_fast_drop=False)
        else:
            self._ingest_repo_flow(existing_asset_path=self.selected_repo_asset_path, allow_fast_drop=False)

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
            allow_fast_drop=True,
        )

    def _handle_repo_version_drop(self, paths: list[Path], _target_item: object) -> None:
        target_asset = self.selected_repo_asset_path or None
        self._ingest_repo_flow(existing_asset_path=target_asset, source_paths=paths, allow_fast_drop=True)

    def _ingest_repo_flow(
        self,
        existing_asset_path: Optional[str],
        source_paths: Optional[list[Path]] = None,
        category_prefix: Optional[str] = None,
        allow_fast_drop: bool = False,
    ) -> None:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return
        if source_paths is None:
            if allow_fast_drop and self.hub.config.fast_drop:
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

        if allow_fast_drop and self.hub.config.fast_drop:
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
            default_url=existing_asset.url if existing_asset else "",
            default_custom=dict(existing_asset.custom) if existing_asset else None,
            copied_metadata=self._read_copied_metadata_payload(check_clipboard=False),
            suggested_custom_keys=self._collect_project_custom_keys(),
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
                    url=str(entry.get("url", "")),
                    license_info=str(entry["license_info"]),
                    tags=list(entry["tags"]),  # type: ignore[arg-type]
                    comment=str(entry["comment"]),
                    custom=dict(entry.get("custom") or {}),  # type: ignore[arg-type]
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
        self._publish_flow([], default_deliverable=default_deliv, allow_fast_drop=False)

    def _action_publish_from_selected_working(self) -> None:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return
        selected_w = self._selected_working_version()
        default_deliv = self.selected_deliverable_path if self.selected_deliverable_path and self.selected_deliverable_path != "__legacy__" else "main"
        self._publish_flow(
            [],
            default_deliverable=default_deliv,
            default_working_version=selected_w,
            allow_fast_drop=False,
        )

    def _handle_published_deliverable_drop(self, paths: list[Path], target_item: object) -> None:
        default_deliv = "main"
        if isinstance(target_item, QTreeWidgetItem):
            if target_item.data(0, Qt.ItemDataRole.UserRole + 1) != "deliv_category":
                raw = str(target_item.data(0, Qt.ItemDataRole.UserRole) or "")
                if raw and raw != "__legacy__":
                    default_deliv = raw
        self._publish_flow([p for p in paths if p.is_file()], default_deliverable=default_deliv, allow_fast_drop=True)

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
            allow_fast_drop=True,
        )

    def _publish_flow(
        self,
        source_files: list[Path],
        default_deliverable: str = "main",
        default_working_version: Optional[int] = None,
        default_target_version: Optional[int] = None,
        allow_fast_drop: bool = False,
    ) -> None:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return

        if allow_fast_drop and self.hub.config.fast_drop:
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
        if open_folder_in_os(export_dir):
            self.show_status_message(
                f"Opened export folder {deliv_path}/v{ver_num:03d}/ — click 'Manual Export → 2. Sync Rendered Exports' after rendering"
            )
        else:
            QMessageBox.information(
                self,
                "Export Folder Ready",
                f"Export multiple file types into:\n{export_dir}\n\nThen click 'Manual Export → 2. Sync Rendered Exports' to register published/{deliv_path}/v{ver_num:03d}/.",
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
            and self.active_snapshot.software == "filmcraft"
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
                        res = self.filmcraft_bridge.open_project(target)
                        mode_msg = "in running FilmCraft" if res.get("mode") == "live_bridge" else "in FilmCraft (--control)"
                        self.show_status_message(f"✓ Opened {bundle.version_label} {mode_msg}")
                        return
                    except Exception:
                        pass
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

    # ---------------- Unified Active Target & Smart DCC Send ----------------

    def _choose_dcc_option(self, title: str, prompt: str, labels: list[str]) -> Optional[int]:
        chosen, ok = QInputDialog.getItem(self, title, prompt, labels, 0, False)
        if not ok or not chosen or chosen not in labels:
            return None
        return labels.index(chosen)

    def _make_dcc_send_context(self) -> DCCSendContext:
        return DCCSendContext(
            hub=self.hub,
            workspace=self.workspace,
            project_root=self.active_project_root,
            project_name=self.active_project_name,
            component_path=self.active_component_path,
            component_software=(self.active_snapshot.software if self.active_snapshot else ""),
            choose_option=self._choose_dcc_option,
        )

    def _send_paths_for_published_item(self, item: QTreeWidgetItem) -> list[Path]:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return []
        data = item.data(0, Qt.ItemDataRole.UserRole)
        if not isinstance(data, tuple) or len(data) < 3:
            return []
        comp_dir = self.active_project_root / self.active_component_path
        kind, dpath, ver = str(data[0]), str(data[1]), int(data[2])
        if kind == "file" and len(data) > 3 and data[3]:
            p = comp_dir / str(data[3])
            return [p] if p.is_file() else []

        deliv = self.active_snapshot.published_deliverables.get(dpath)
        versions = self.active_snapshot.published_versions if dpath == "__legacy__" else (deliv.versions if deliv else [])
        bundle = next((v for v in versions if v.version == ver), None)
        if not bundle:
            return []

        paths: list[Path] = []
        for tf in bundle.files:
            if tf.rel_path:
                cand = comp_dir / tf.rel_path
                if cand.is_file():
                    paths.append(cand)
        if not paths and bundle.primary_file:
            cand = comp_dir / bundle.primary_file
            if cand.is_file():
                paths.append(cand)
        return paths

    @staticmethod
    def _dedupe_paths(paths: list[Path]) -> list[Path]:
        seen: set[Path] = set()
        out: list[Path] = []
        for p in paths:
            rp = p.resolve()
            if rp not in seen and p.is_file():
                seen.add(rp)
                out.append(p)
        return out

    def _selected_sendable_paths(self) -> list[Path]:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return []
        tab_idx = self.triad_tabs.currentIndex()
        collected: list[Path] = []
        if tab_idx == 1:
            items = self.repo_version_list.selectedItems()
            if not items and self.repo_version_list.currentItem() is not None:
                items = [self.repo_version_list.currentItem()]  # type: ignore[list-item]
            for it in items:
                p = self._drag_path_for_repo_item(it)
                if p is not None:
                    collected.append(p)
        elif tab_idx == 2:
            items = self.published_version_tree.selectedItems()
            if not items and self.published_version_tree.currentItem() is not None:
                items = [self.published_version_tree.currentItem()]  # type: ignore[list-item]
            for it in items:
                collected.extend(self._send_paths_for_published_item(it))
        return self._dedupe_paths(collected)

    def _resolve_export_dcc_key(self, active: Optional[ResolvedDCCTarget]) -> str:
        if active is not None:
            return active.target.key
        if self.active_snapshot and self.active_snapshot.software in ("reaper", "resolve", "filmcraft"):
            return self.active_snapshot.software
        return ""

    def _refresh_dcc_target_ui(self, refresh_bridges: bool = True) -> None:
        if not hasattr(self, "dcc_exchange_bar"):
            return
        if refresh_bridges or self._cached_dcc_targets is None:
            self._cached_dcc_targets = self.send_registry.resolve_targets(self._make_dcc_send_context())
        active, live_targets, offline_targets = self._cached_dcc_targets
        pinned_key = self.hub.config.pinned_dcc_target.strip()

        # Populate Target DCC dropdown
        self.dcc_target_combo.blockSignals(True)
        self.dcc_target_combo.clear()
        if active is not None and not active.is_pinned and active.status.live:
            auto_lbl = f"Auto → {active.target.display_name} ● ({active.status.mode_badge})"
        elif active is not None and not active.is_pinned and active.status.can_send_offline:
            auto_lbl = f"Auto → {active.target.display_name} ({active.status.mode_badge})"
        elif live_targets:
            first_live = live_targets[0]
            auto_lbl = f"Auto → {first_live.target.display_name} ● ({first_live.status.mode_badge})"
        else:
            auto_lbl = "Auto (No Live DCC)"
        self.dcc_target_combo.addItem(auto_lbl, "")

        for rt in live_targets:
            self.dcc_target_combo.addItem(
                f"{rt.target.display_name} ● ({rt.status.mode_badge})",
                rt.target.key,
            )
        for rt in offline_targets:
            self.dcc_target_combo.addItem(
                f"{rt.target.display_name} ({rt.status.mode_badge})",
                rt.target.key,
            )

        sel_target_idx = 0
        if pinned_key:
            found_idx = self.dcc_target_combo.findData(pinned_key)
            if found_idx >= 0:
                sel_target_idx = found_idx
        self.dcc_target_combo.setCurrentIndex(sel_target_idx)
        self.dcc_target_combo.blockSignals(False)

        # Populate Mode dropdown for active target
        prev_mode_id = str(self.dcc_mode_combo.currentData() or "")
        self.dcc_mode_combo.blockSignals(True)
        self.dcc_mode_combo.clear()
        if active is not None:
            modes = active.target.get_modes()
            same_target = self._last_dcc_target_key == active.target.key
            desired_mode_id = (
                prev_mode_id
                if (same_target and any(m.id == prev_mode_id for m in modes))
                else active.default_mode.id
            )
            self._last_dcc_target_key = active.target.key
            for m in modes:
                self.dcc_mode_combo.addItem(m.label, m.id)
            mode_idx = self.dcc_mode_combo.findData(desired_mode_id)
            if mode_idx >= 0:
                self.dcc_mode_combo.setCurrentIndex(mode_idx)
            self.dcc_mode_combo.setEnabled(True)
        else:
            self._last_dcc_target_key = ""
            self.dcc_mode_combo.addItem("— No active DCC —", "")
            self.dcc_mode_combo.setEnabled(False)
        self.dcc_mode_combo.blockSignals(False)

        # Update Send button
        tab_idx = self.triad_tabs.currentIndex()
        paths = self._selected_sendable_paths()
        has_files = bool(paths)
        can_send = bool(self.active_snapshot is not None and has_files and active is not None)
        self.dcc_send_btn.setEnabled(can_send)

        if tab_idx == 0:
            self.dcc_send_btn.setText("Send (Repo/Published)")
            self.dcc_send_btn.setIcon(get_icon("send", TEXT_SECONDARY, ICON_SM))
            if self.dcc_send_btn.objectName():
                self.dcc_send_btn.setObjectName("")
                repolish(self.dcc_send_btn)
            self.dcc_send_btn.setToolTip(
                "Switch to Repo Assets or Published tab to send media to a DCC (Ctrl+D)"
            )
        elif active is not None:
            modes = active.target.get_modes()
            sel_mode_id = str(self.dcc_mode_combo.currentData() or "")
            sel_mode = next((m for m in modes if m.id == sel_mode_id), active.default_mode)
            count_txt = f" {len(paths)} Files" if len(paths) > 1 else ""
            if sel_mode.is_update_action:
                self.dcc_send_btn.setText(f"Update in {active.target.display_name}")
                self.dcc_send_btn.setIcon(get_icon("refresh", "#dbeafe" if can_send else TEXT_SECONDARY, ICON_SM))
            else:
                self.dcc_send_btn.setText(f"Send{count_txt} to {active.target.display_name}")
                self.dcc_send_btn.setIcon(get_icon("send", "#dbeafe" if can_send else TEXT_SECONDARY, ICON_SM))
            desired_obj = "AccentOutlineButton" if can_send else ""
            if self.dcc_send_btn.objectName() != desired_obj:
                self.dcc_send_btn.setObjectName(desired_obj)
                repolish(self.dcc_send_btn)
            if has_files:
                self.dcc_send_btn.setToolTip(
                    f"Send selected media to {active.target.full_name} using '{sel_mode.label}' (Ctrl+D)"
                )
            else:
                self.dcc_send_btn.setToolTip(
                    f"Select file(s) in Repo Assets or Published tab to send to {active.target.full_name} (Ctrl+D)"
                )
        else:
            self.dcc_send_btn.setText("Send Media")
            self.dcc_send_btn.setIcon(get_icon("send", TEXT_SECONDARY, ICON_SM))
            if self.dcc_send_btn.objectName():
                self.dcc_send_btn.setObjectName("")
                repolish(self.dcc_send_btn)
            self.dcc_send_btn.setToolTip(
                "Open a live DCC or pick a DCC in the dropdown to send Repo/Published media (Ctrl+D)"
            )

        # Update Export button
        export_key = self._resolve_export_dcc_key(active)
        self.dcc_export_btn.setEnabled(self.active_snapshot is not None)
        self.dcc_export_btn.setIcon(get_icon("dcc_pull", TEXT_SECONDARY, ICON_SM))
        if export_key:
            label_map = {"reaper": "REAPER", "resolve": "Resolve", "filmcraft": "FilmCraft"}
            exp_label = label_map.get(export_key) or (active.target.display_name if active else export_key.title())
            self.dcc_export_btn.setText(f"Export ({exp_label})…")
            self.dcc_export_btn.setToolTip(f"Export / render & publish directly from {exp_label}")
        else:
            self.dcc_export_btn.setText("Export / Publish…")
            self.dcc_export_btn.setToolTip("Publish deliverables into the active component")

    def _set_pinned_dcc_target(self, target_key: str, refresh_bridges: bool = True) -> None:
        self.hub.set_pinned_dcc_target(target_key)
        self._refresh_dcc_target_ui(refresh_bridges=refresh_bridges)
        if not target_key:
            self.show_status_message("✓ Active target DCC: Auto (Follow Live)")
        else:
            target = self.send_registry.get(target_key)
            name = target.display_name if target else target_key
            self.show_status_message(f"✓ Pinned target DCC: {name}")

    def _on_dcc_target_combo_changed(self, _idx: int) -> None:
        key = str(self.dcc_target_combo.currentData() or "")
        self._set_pinned_dcc_target(key, refresh_bridges=True)

    def _on_dcc_mode_combo_changed(self, _idx: int) -> None:
        mode_id = str(self.dcc_mode_combo.currentData() or "")
        if self._cached_dcc_targets is not None and mode_id:
            active = self._cached_dcc_targets[0]
            if active is not None:
                chosen = next((m for m in active.target.get_modes() if m.id == mode_id), None)
                if chosen is not None and chosen.remember_as_default:
                    self.hub.set_preferred_dcc_mode(active.target.key, chosen.id)
                    active.default_mode = chosen
        self._refresh_dcc_target_ui(refresh_bridges=False)

    def _dispatch_send_to_dcc(
        self,
        target_key: str,
        paths: list[Path],
        mode_id: Optional[str] = None,
        silent: bool = False,
    ) -> DCCSendResult:
        res = self.send_registry.send(target_key, paths, mode_id, self._make_dcc_send_context())
        self._refresh_dcc_target_ui(refresh_bridges=True)
        if res.ok:
            self.show_status_message(f"✓ {res.message}")
        elif not res.details.get("cancelled") and not silent:
            target = self.send_registry.get(target_key)
            title = f"Send to {target.display_name}" if target else "Send to DCC"
            if res.offline_hint:
                QMessageBox.information(self, title, res.message)
            else:
                QMessageBox.warning(self, title, res.message)
        return res

    def _action_send_selected_to_active_dcc(self) -> None:
        paths = self._selected_sendable_paths()
        if not paths:
            return
        if self._cached_dcc_targets is None:
            self._cached_dcc_targets = self.send_registry.resolve_targets(self._make_dcc_send_context())
        active = self._cached_dcc_targets[0]
        if active is None:
            self.show_status_message("No active DCC target — select a DCC in the DCC bar first")
            return
        mode_id = str(self.dcc_mode_combo.currentData() or active.default_mode.id)
        self._dispatch_send_to_dcc(active.target.key, paths, mode_id=mode_id)

    def _action_export_from_active_dcc(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        if self._cached_dcc_targets is None:
            self._cached_dcc_targets = self.send_registry.resolve_targets(self._make_dcc_send_context())
        export_key = self._resolve_export_dcc_key(self._cached_dcc_targets[0])
        if export_key == "reaper":
            self._action_export_from_reaper()
        elif export_key == "resolve":
            self._action_export_from_resolve()
        elif export_key == "filmcraft":
            self._action_export_from_filmcraft()
        else:
            self._action_publish_deliverable()

    def _action_refresh_dcc_targets(self) -> None:
        self._refresh_reaper_status_pill()
        self.show_status_message("✓ Refreshed DCC status")

    def _action_import_files_into_reaper(self, paths: list[Path], mode: str = "new_track") -> None:
        self._dispatch_send_to_dcc("reaper", paths, mode_id=mode)

    def _action_open_reaper_update_dialog(self) -> None:
        active_rpp = self.reaper_bridge.get_state().active_rpp
        if not active_rpp and self.active_project_root and self.active_component_path and self.active_snapshot:
            ver = self._selected_working_version()
            bundle = (
                next((v for v in self.active_snapshot.working_versions if v.version == ver), None)
                if ver is not None
                else (self.active_snapshot.working_versions[-1] if self.active_snapshot.working_versions else None)
            )
            if bundle and bundle.primary_file:
                cand = (self.active_project_root / self.active_component_path / bundle.primary_file).resolve()
                if cand.is_file():
                    active_rpp = str(cand)
        dlg = ReaperImportDialog(
            self,
            self.hub,
            self.workspace,
            self.reaper_bridge,
            active_rpp=active_rpp,
            initial_tab=1,
        )
        dlg.exec()

    def _action_update_asset_in_reaper(
        self,
        target_path: Path,
        update_mode: str = "replace_source",
        silent: bool = False,
    ) -> dict[str, Any]:
        if update_mode == "replace_source":
            res = self._dispatch_send_to_dcc("reaper", [target_path], mode_id="update_existing", silent=silent)
            if res.details:
                return dict(res.details)
            return {"updated_takes": res.sent_count, "updated_items": 0}
        if not target_path.is_file() or not self.active_project_root:
            if not silent:
                QMessageBox.warning(self, "Update in REAPER", "Target file is not available locally on disk.")
            return {"updated_takes": 0, "updated_items": 0}

        target_meta = self.workspace.describe_asset_file(self.active_project_root, target_path)
        if target_meta is None:
            if not silent:
                QMessageBox.warning(self, "Update in REAPER", "Could not resolve target file to an Obol repo/ or published/ asset.")
            return {"updated_takes": 0, "updated_items": 0}

        if not self.reaper_bridge.is_connected():
            if not silent:
                QMessageBox.information(
                    self,
                    "REAPER Bridge Offline",
                    "REAPER Bridge is not currently running.\n\n"
                    "Open REAPER (with Obol Bridge running) to update imported MIDI or media takes.",
                )
            return {"updated_takes": 0, "updated_items": 0}

        try:
            session_items = self.reaper_bridge.query_session_media()
            take_updates: list[dict[str, Any]] = []
            replacements: dict[str, str] = {}
            target_key = (target_meta["component"], target_meta["scope"], target_meta["asset_key"])
            for sit in session_items:
                resolved = self.workspace.resolve_reaper_session_item(self.active_project_root, sit)
                if resolved is None:
                    continue
                if (resolved["component"], resolved["kind"], resolved["asset_key"]) != target_key:
                    continue
                curr_file = Path(str(resolved["current_file"]))
                replacements[str(curr_file)] = str(target_path.resolve())
                take_updates.append({
                    "take_guid": str(resolved.get("take_guid") or ""),
                    "item_guid": str(resolved.get("item_guid") or ""),
                    "old_path": str(curr_file),
                    "target_path": str(target_path.resolve()),
                    "asset_meta": target_meta,
                })

            if not take_updates:
                if not silent:
                    QMessageBox.information(
                        self,
                        "Update in REAPER",
                        f"No takes matching '{target_meta['component']} / {target_meta['asset_key']}' were found in the active REAPER session.",
                    )
                return {"updated_takes": 0, "updated_items": 0}

            res = self.reaper_bridge.update_media_sources(
                replacements=replacements,
                take_updates=take_updates,
                update_mode=update_mode,
                midi_mode="preserve",
                match_length="auto",
            )
            self.show_status_message(
                f"✓ Updated {res.get('updated_takes', 0)} take(s) in REAPER to v{int(target_meta['version']):03d}"
            )
            return res
        except Exception as exc:
            if not silent:
                QMessageBox.warning(self, "Update in REAPER", str(exc))
            return {"updated_takes": 0, "updated_items": 0}

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
        mode = "timeline" if append_to_timeline else "media_pool"
        self._dispatch_send_to_dcc("resolve", paths, mode_id=mode)

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

    def _list_project_filmcraft_files(self) -> list[tuple[str, Path]]:
        if not self.active_project_root or not self.active_project_root.is_dir():
            return []
        found: list[tuple[str, Path]] = []
        for comp in discover_project_components(self.active_project_root):
            snap = load_component_snapshot(self.active_project_root, comp)
            if not snap.working_versions:
                continue
            latest = snap.working_versions[-1]
            if not latest.primary_file:
                continue
            p = (self.active_project_root / comp / latest.primary_file).resolve()
            if (snap.software == "filmcraft" or p.suffix.lower() == ".fcproj") and p.is_file():
                found.append((comp, p))
        return found

    def _active_working_fcproj(
        self,
        version: Optional[int] = None,
        fallback_any_component: bool = False,
    ) -> Optional[Path]:
        if self.active_project_root and self.active_component_path and self.active_snapshot:
            if self.active_snapshot.software == "filmcraft" and self.active_snapshot.working_versions:
                ver = version if version is not None else self._selected_working_version()
                bundle = (
                    next((v for v in self.active_snapshot.working_versions if v.version == ver), None)
                    if ver is not None
                    else self.active_snapshot.working_versions[-1]
                )
                if bundle and bundle.primary_file:
                    p = (self.active_project_root / self.active_component_path / bundle.primary_file).resolve()
                    if p.suffix.lower() == ".fcproj" and p.is_file():
                        return p
        if fallback_any_component:
            candidates = self._list_project_filmcraft_files()
            if len(candidates) == 1:
                return candidates[0][1]
        return None

    def _action_import_files_into_filmcraft(
        self,
        paths: list[Path],
        place_on_timeline: bool = False,
    ) -> None:
        mode = "timeline" if place_on_timeline else "project_bin"
        self._dispatch_send_to_dcc("filmcraft", paths, mode_id=mode)

    def _action_import_dialog_filmcraft(self, version: Optional[int] = None) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        fcproj = self._active_working_fcproj(version=version)
        dlg = FilmCraftImportDialog(
            self,
            self.hub,
            self.workspace,
            self.filmcraft_bridge,
            active_fcproj=str(fcproj) if fcproj else "",
            default_project=self.active_project_name,
            default_component=self.active_component_path,
        )
        dlg.exec()

    def _action_export_from_filmcraft(self, version: Optional[int] = None) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        fcproj = self._active_working_fcproj(version=version)
        dlg = FilmCraftExportPublishDialog(
            self,
            self.hub,
            self.workspace,
            self.filmcraft_bridge,
            active_fcproj=str(fcproj) if fcproj else "",
            default_project=self.active_project_name,
            default_component=self.active_component_path,
        )
        if dlg.exec() == FilmCraftExportPublishDialog.DialogCode.Accepted:
            self._refresh_component_triad()
            self.show_status_message("✓ Exported & published from FilmCraft")

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

    def _action_reaper_increment_save(
        self,
        comment: str = "",
        silent: bool = False,
    ) -> Optional[VersionBundle]:
        if not self.reaper_bridge.is_connected():
            if not silent:
                QMessageBox.information(
                    self,
                    "REAPER Bridge Offline",
                    "REAPER Bridge is not currently running.\n\n"
                    "Start REAPER (with Obol Bridge enabled) to increment-save the active session directly.",
                )
            return None

        st = self.reaper_bridge.get_state()
        rpp_ctx = self.workspace.resolve_working_file_context(Path(st.active_rpp)) if st.active_rpp else None
        if rpp_ctx is not None:
            target_proot = rpp_ctx.project_root
            target_comp = rpp_ctx.component_path
            src_ver: Optional[int] = rpp_ctx.working_version
        else:
            target_proot = self.active_project_root
            target_comp = self.active_component_path
            src_ver = self._selected_working_version() if self.triad_tabs.currentIndex() == 0 else None

        if not target_proot or not target_comp:
            if not silent:
                QMessageBox.warning(
                    self,
                    "Increment Save in REAPER",
                    "Select an Obol component (or open a working .rpp inside an Obol project) first.",
                )
            return None

        prep = self.workspace.prepare_next_working_version(
            target_proot,
            target_comp,
            source_version=src_ver,
            extension=".rpp",
        )
        target_rpp = Path(prep["primary_file"])
        ver_dir = Path(prep["version_dir"])
        try:
            save_res = self.reaper_bridge.save_project_as(target_rpp, ensure_open=True)
            if not target_rpp.is_file():
                raise FileNotFoundError(f"REAPER did not write target file: {target_rpp}")
        except Exception as exc:
            if ver_dir.is_dir() and not any(ver_dir.iterdir()):
                ver_dir.rmdir()
            if not silent:
                QMessageBox.warning(self, "Increment Save Failed", str(exc))
            return None

        bundle = self.workspace.register_saved_working_version(
            target_proot,
            target_comp,
            version=int(prep["version"]),
            primary_file=target_rpp,
            parent_version=int(prep["parent_version"]),
            comment=comment or f"Increment saved from REAPER ({prep['version_label']})",
        )
        self._refresh_component_triad()
        if save_res.get("reloaded") or not save_res.get("switched_in_place", True):
            ver_txt = str(save_res.get("reaper_version") or st.reaper_version or "").strip()
            ver_label = f"REAPER {ver_txt}" if ver_txt else "this REAPER version"
            self.show_status_message(
                f"✓ Increment saved {target_comp}/working/{bundle.version_label} "
                f"(reloaded — {ver_label} lacks in-place Save As; upgrade to REAPER 7.34+)"
            )
        else:
            self.show_status_message(f"✓ Increment saved REAPER session as {target_comp}/working/{bundle.version_label}")
        return bundle

    def _action_collect_external_media(self) -> None:
        if not self.active_project_root or not self.active_component_path:
            return
        ver = self._selected_working_version() if self.triad_tabs.currentIndex() == 0 else None
        sw = self.active_snapshot.software if self.active_snapshot else ""
        dcc_lbl = "FilmCraft" if sw == "filmcraft" else "Reaper"
        ext_lbl = ".fcproj" if sw == "filmcraft" else ".rpp"
        externals = self.workspace.scan_external_dependencies(
            self.active_project_root, self.active_component_path, working_version=ver
        )
        if not externals:
            QMessageBox.information(
                self,
                f"{dcc_lbl} Dependency Audit",
                f"All media files in the {ext_lbl} are already inside repo/!",
            )
            return
        msg = "\n".join(f"• {d.raw_reference} → repo/{d.suggested_category}/{d.suggested_asset_name}/v001/" for d in externals)
        ans = QMessageBox.question(
            self,
            "Collect External Media into Repo",
            f"Found {len(externals)} external reference(s) outside repo/:\n\n{msg}\n\nIngest all into repo/ and relink {ext_lbl}?",
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
        proj_name = self.active_project_name
        glob_pat = self._selected_vault_glob()

        def _work() -> None:
            self.sync.pull_paths(proj_name, [glob_pat])

        def _done(_res: object) -> None:
            self._refresh_component_triad()
            self.show_status_message(f"✓ Checked out {glob_pat}")

        run_with_busy_state(
            self,
            _work,
            on_success=_done,
            disable_widgets=[self.vault_actions_box],
            busy_message=f"Checking out {glob_pat}…",
            status_callback=self.show_status_message,
        )

    def _action_offload_selected(self) -> None:
        if not self.active_project_name or not self.active_component_path:
            return
        proj_name = self.active_project_name
        glob_pat = self._selected_vault_glob()

        def _work() -> None:
            self.sync.push_paths(proj_name, [glob_pat], evict_local=True)

        def _done(_res: object) -> None:
            self._refresh_component_triad()
            self.show_status_message(f"✓ Offloaded {glob_pat}")

        run_with_busy_state(
            self,
            _work,
            on_success=_done,
            disable_widgets=[self.vault_actions_box],
            busy_message=f"Offloading {glob_pat}…",
            status_callback=self.show_status_message,
        )

    def _save_working_as_seed_template(self, ver: int) -> None:
        if not self.active_project_root or not self.active_component_path or not self.active_snapshot:
            return
        bundle = next((v for v in self.active_snapshot.working_versions if v.version == ver), None)
        if not bundle or not bundle.primary_file:
            return
        src_abs = self.active_project_root / self.active_component_path / bundle.primary_file
        if not src_abs.is_file():
            return
        dlg = SaveSeedTemplateDialog(
            self,
            self.hub,
            source_file=src_abs,
            software_key=self.active_snapshot.software,
            project_root=self.active_project_root,
            client_root=self.hub.get_client_root(),
            default_comment=f"Captured from {self.active_component_path} v{ver:03d}",
        )
        if dlg.exec() == SaveSeedTemplateDialog.DialogCode.Accepted and dlg.saved_stream and dlg.saved_version:
            self.show_status_message(
                f"✓ Saved seed '{dlg.saved_stream.name}' {dlg.saved_version.version_label} ({dlg.saved_scope.value})"
            )

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
            act_add = menu.addAction("+ Add Component…")
            if menu.exec(self.component_tree.viewport().mapToGlobal(pos)) == act_add:
                self._prompt_add_component("")
            return

        kind = item.data(0, Qt.ItemDataRole.UserRole + 1)
        raw_path = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
        if kind == "group":
            act_add = menu.addAction(f"+ Add Component in '{raw_path}/'…")
            act_open = menu.addAction("Open Group Folder in Explorer")
            chosen = menu.exec(self.component_tree.viewport().mapToGlobal(pos))
            if chosen == act_add:
                self._prompt_add_component(raw_path)
            elif chosen == act_open and self.active_project_root:
                open_folder_in_os(self.active_project_root / raw_path)
            return

        act_open = menu.addAction("Open Folder in Explorer")
        act_copy = menu.addAction("Copy Folder Path")
        act_cap = menu.addAction("Capture as Component Template…")
        menu.addSeparator()
        act_del = menu.addAction("Delete Component…")
        chosen = menu.exec(self.component_tree.viewport().mapToGlobal(pos))
        if chosen == act_open and self.active_project_root:
            open_folder_in_os(self.active_project_root / raw_path)
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
        is_fc = bool(self.active_snapshot and self.active_snapshot.software == "filmcraft")
        act_fc_imp = menu.addAction("Import Media into FilmCraft…") if is_fc else None
        act_fc_pub = menu.addAction("Export && Publish from FilmCraft…") if is_fc else None
        act_vup = menu.addAction(f"Version Up from v{ver:03d}…")
        act_pub = menu.addAction(f"Publish from v{ver:03d}…")
        act_seed = menu.addAction("Save as Working Seed Template…")
        menu.addSeparator()
        act_copy_meta = menu.addAction("Copy Metadata")
        act_paste_meta = menu.addAction("Paste Metadata")
        act_paste_meta.setEnabled(bool(self._read_copied_metadata_payload(check_clipboard=False)))
        menu.addSeparator()
        act_exp = menu.addAction("Open Version Folder in Explorer")
        act_copy = menu.addAction("Copy Primary File Path")
        menu.addSeparator()
        act_del = menu.addAction(f"Delete working/v{ver:03d}…")
        chosen = menu.exec(self.working_list.viewport().mapToGlobal(pos))
        if chosen == act_open:
            self._action_launch_working(ver)
        elif act_open_tab is not None and chosen == act_open_tab:
            self._action_launch_working(ver, new_tab=True)
        elif act_fc_imp is not None and chosen == act_fc_imp:
            self._action_import_dialog_filmcraft(version=ver)
        elif act_fc_pub is not None and chosen == act_fc_pub:
            self._action_export_from_filmcraft(version=ver)
        elif chosen == act_vup:
            self._action_version_up_working(source_version=ver)
        elif chosen == act_pub:
            self._publish_flow([], default_deliverable="main", default_working_version=ver)
        elif chosen == act_seed:
            self._save_working_as_seed_template(ver)
        elif chosen == act_copy_meta:
            self._action_copy_metadata()
        elif chosen == act_paste_meta:
            self._action_paste_metadata(merge=False)
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
        copied = self._read_copied_metadata_payload(check_clipboard=False)
        if item.data(0, Qt.ItemDataRole.UserRole + 1) == "repo_category":
            cat = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
            menu = QMenu(self)
            act_ing = menu.addAction(f"+ Ingest into '{cat}/'…")
            act_cat_paste = None
            if copied and self.active_snapshot:
                act_cat_paste = menu.addAction(f"Paste Metadata to All in '{cat}/'…")
            chosen = menu.exec(self.repo_asset_tree.viewport().mapToGlobal(pos))
            if chosen == act_ing:
                self._ingest_repo_flow(existing_asset_path=None, category_prefix=cat)
            elif act_cat_paste is not None and chosen == act_cat_paste and self.active_snapshot:
                prefix = f"{cat.strip('/')}/"
                targets = [ap for ap in sorted(self.active_snapshot.repo_assets.keys()) if ap.startswith(prefix)]
                cnt = self._paste_metadata_to_repo_assets(targets, merge=False)
                if cnt:
                    self._refresh_component_triad()
                    self.show_status_message(f"✓ Pasted metadata to {cnt} asset(s) in '{cat}/'")
            return
        menu = QMenu(self)
        selected_assets = self._selected_repo_asset_paths()
        act_new_v = menu.addAction("+ Ingest New Version…")
        act_exp = menu.addAction("Open Asset Folder in Explorer")
        menu.addSeparator()
        act_copy_meta = menu.addAction("Copy Metadata")
        paste_lbl = (
            f"Paste Metadata to {len(selected_assets)} Assets"
            if len(selected_assets) > 1
            else "Paste Metadata"
        )
        act_paste_meta = menu.addAction(paste_lbl)
        act_paste_meta.setEnabled(bool(copied))
        act_merge_meta = menu.addAction("Merge Metadata (Fill Blank / Add Custom)")
        act_merge_meta.setEnabled(bool(copied))
        menu.addSeparator()
        act_del = menu.addAction("Delete Named Repo Asset…")
        chosen = menu.exec(self.repo_asset_tree.viewport().mapToGlobal(pos))
        if chosen == act_new_v:
            self._action_ingest_repo_version()
        elif chosen == act_exp:
            self._action_open_in_explorer()
        elif chosen == act_copy_meta:
            self._action_copy_metadata()
        elif chosen == act_paste_meta:
            self._action_paste_metadata(merge=False)
        elif chosen == act_merge_meta:
            self._action_paste_metadata(merge=True)
        elif chosen == act_del:
            self._action_delete_repo_asset()

    def _ctx_repo_version_list(self, pos: QPoint) -> None:
        item = self.repo_version_list.itemAt(pos)
        if item is None:
            return
        ver = int(item.data(0, Qt.ItemDataRole.UserRole))
        menu = QMenu(self)
        act_open = menu.addAction(f"Open v{ver:03d} File")
        act_new_v = menu.addAction("+ Ingest New Version…")
        act_exp = menu.addAction("Open Version Folder in Explorer")
        act_copy = menu.addAction("Copy File Path")
        meta_sub = menu.addMenu("Metadata")
        act_copy_meta = meta_sub.addAction("Copy Metadata")
        has_copied = bool(self._read_copied_metadata_payload(check_clipboard=False))
        act_paste_meta = meta_sub.addAction("Paste Metadata")
        act_paste_meta.setEnabled(has_copied)
        act_merge_meta = meta_sub.addAction("Merge Metadata")
        act_merge_meta.setEnabled(has_copied)
        menu.addSeparator()
        act_del = menu.addAction(f"Delete Repo v{ver:03d}…")
        chosen = menu.exec(self.repo_version_list.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if chosen == act_open:
            self._on_repo_version_double_clicked(item, 0)
        elif chosen == act_new_v:
            self._action_ingest_repo_version()
        elif chosen == act_exp:
            self._action_open_in_explorer()
        elif chosen == act_copy:
            self._action_copy_selected_path()
        elif chosen == act_copy_meta:
            self._action_copy_metadata()
        elif chosen == act_paste_meta:
            self._action_paste_metadata(merge=False)
        elif chosen == act_merge_meta:
            self._action_paste_metadata(merge=True)
        elif chosen == act_del:
            self._action_delete_repo_version()

    def _ctx_published_deliverable_tree(self, pos: QPoint) -> None:
        item = self.published_deliverable_tree.itemAt(pos)
        if item is None or item.data(0, Qt.ItemDataRole.UserRole + 1) == "deliv_category":
            return
        menu = QMenu(self)
        act_pub = menu.addAction("+ Publish New Release…")
        act_exp = menu.addAction("Open Deliverable Folder in Explorer")
        menu.addSeparator()
        act_del = menu.addAction("Delete Named Deliverable…")
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
        menu = QMenu(self)
        act_open = menu.addAction("Open File") if (isinstance(data, tuple) and data[0] == "file") else None
        act_add = menu.addAction("+ Attach More Files to Release…")
        act_exp = menu.addAction("Open Release Folder in Explorer")
        act_copy = menu.addAction("Copy Path")
        meta_sub = menu.addMenu("Metadata")
        act_copy_meta = meta_sub.addAction("Copy Metadata")
        has_copied = bool(self._read_copied_metadata_payload(check_clipboard=False))
        act_paste_meta = meta_sub.addAction("Paste Metadata")
        act_paste_meta.setEnabled(has_copied)
        act_merge_meta = meta_sub.addAction("Merge Metadata")
        act_merge_meta.setEnabled(has_copied)
        menu.addSeparator()
        act_del = menu.addAction("Delete Published Release…")
        chosen = menu.exec(self.published_version_tree.viewport().mapToGlobal(pos))
        if chosen is None:
            return
        if act_open is not None and chosen == act_open:
            self._on_published_item_double_clicked(item, 0)
        elif chosen == act_add and isinstance(data, tuple) and len(data) >= 3 and data[1] != "__legacy__":
            self._publish_flow([], default_deliverable=str(data[1]), default_target_version=int(data[2]))
        elif chosen == act_exp:
            self._action_open_in_explorer()
        elif chosen == act_copy:
            self._action_copy_selected_path()
        elif chosen == act_copy_meta:
            self._action_copy_metadata()
        elif chosen == act_paste_meta:
            self._action_paste_metadata(merge=False)
        elif chosen == act_merge_meta:
            self._action_paste_metadata(merge=True)
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
        if confirm_destructive_action(
            self,
            "Delete Component",
            f"Delete component '{comp}' and all its working, repo, and published files on disk?",
            confirm_label="Delete Component",
            informative_text="This cannot be undone.",
        ):
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
        if confirm_destructive_action(
            self,
            "Delete Working Version",
            f"Delete working/v{ver:03d}/ from '{self.active_component_path}'?",
            confirm_label=f"Delete v{ver:03d}",
            informative_text="This cannot be undone.",
        ):
            self.workspace.delete_working_version(self.active_project_root, self.active_component_path, ver)
            self._refresh_component_triad()
            self.show_status_message(f"Deleted working/v{ver:03d}")

    def _action_delete_repo_asset(self) -> None:
        if not self.active_project_root or not self.active_component_path or not self.selected_repo_asset_path:
            return
        apath = self.selected_repo_asset_path
        if confirm_destructive_action(
            self,
            "Delete Repo Asset",
            f"Delete named Repo Asset 'repo/{apath}/' and all its versions?",
            confirm_label="Delete Asset",
            informative_text="This cannot be undone.",
        ):
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
        if confirm_destructive_action(
            self,
            "Delete Repo Version",
            f"Delete repo/{apath}/v{ver:03d}/?",
            confirm_label=f"Delete v{ver:03d}",
            informative_text="This cannot be undone.",
        ):
            self.workspace.delete_repo_version(self.active_project_root, self.active_component_path, apath, ver)
            self._refresh_component_triad()
            self.show_status_message(f"Deleted repo/{apath}/v{ver:03d}")

    def _action_delete_published_deliverable(self) -> None:
        if not self.active_project_root or not self.active_component_path or not self.selected_deliverable_path:
            return
        dpath = self.selected_deliverable_path
        if dpath == "__legacy__":
            return
        if confirm_destructive_action(
            self,
            "Delete Published Deliverable",
            f"Delete deliverable 'published/{dpath}/' and all its releases?",
            confirm_label="Delete Deliverable",
            informative_text="This cannot be undone.",
        ):
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
        if confirm_destructive_action(
            self,
            "Delete Published Version",
            f"Delete published/{dpath}/v{ver:03d}/?",
            confirm_label=f"Delete v{ver:03d}",
            informative_text="This cannot be undone.",
        ):
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

    def open_filmcraft_integration_dialog(self) -> None:
        self.open_integrations_manager_dialog(initial_dcc="filmcraft")

    def open_template_manager_dialog(self, initial_tab: int = 0) -> None:
        dlg = TemplateManagerDialog(
            self,
            self.hub,
            active_project_root=self.active_project_root,
            active_component_path=self.active_component_path,
            initial_tab=initial_tab,
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
