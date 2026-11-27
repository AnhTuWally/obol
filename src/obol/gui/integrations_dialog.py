"""Unified studio-grade Master-Detail DCC Integrations Manager dialog."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QFont, QIcon
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from obol.core.hub import ObolHub
from obol.gui.components import (
    DialogFooter,
    InlineStatusBanner,
    confirm_destructive_action,
    open_folder_in_os,
    repolish,
)
from obol.gui.icons import get_icon
from obol.gui.theme import apply_studio_dark_theme
from obol.gui.tokens import (
    ACCENT_LIGHT,
    ERROR,
    ICON_SM,
    INFO,
    SUCCESS,
    TEXT_DISABLED,
    TEXT_SECONDARY,
    WARNING,
)
from obol.plugins.integrations import DCCIntegrationManager, DCCIntegrationReport
from obol.plugins.resolve_bridge import ResolveBridgeClient

_STATE_DISPLAY: dict[str, tuple[str, str, str]] = {
    "connected": ("Live ●", SUCCESS, "StatusPillOnline"),
    "installed": ("Installed ✓", ACCENT_LIGHT, "StatusPillOnline"),
    "needs_update": ("Update Needed ⚠", WARNING, "StatusPillOffline"),
    "not_installed": ("Detected", TEXT_SECONDARY, "StatusPillLocal"),
    "not_detected": ("Not Found", TEXT_DISABLED, "StatusPillLocal"),
}

_CHECK_BADGES: dict[str, tuple[str, str]] = {
    "ok": ("✓", SUCCESS),
    "warn": ("⚠", WARNING),
    "missing": ("✗", ERROR),
    "info": ("ℹ", INFO),
}


class IntegrationsManagerDialog(QDialog):
    """Master-Detail hub for installing, updating, diagnosing, and managing all DCC integrations."""

    def __init__(
        self,
        parent: Optional[QWidget],
        hub: ObolHub,
        initial_dcc: str = "",
        allow_resolve_studio_api: Optional[bool] = None,
        target_dir_overrides: Optional[dict[str, Path]] = None,
    ) -> None:
        super().__init__(parent)
        self.hub = hub
        if allow_resolve_studio_api is None:
            allow_resolve_studio_api = QApplication.platformName() != "offscreen"
        self.manager = DCCIntegrationManager(hub, allow_resolve_studio_api=allow_resolve_studio_api)
        self._custom_target_dirs: dict[str, Path] = dict(target_dir_overrides or {})
        self._reports: dict[str, DCCIntegrationReport] = {}
        self._current_key: str = initial_dcc or "reaper"

        self.setWindowTitle("Obol — DCC Integrations Manager")
        self.resize(940, 620)

        root = QVBoxLayout(self)
        root.setSpacing(10)

        # Top Action & Summary Header
        top_bar = QFrame()
        top_bar.setObjectName("PanelCard")
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 8, 12, 8)
        hdr_title = QLabel("DCC INTEGRATIONS")
        hdr_title.setObjectName("SectionHeader")
        self.summary_label = QLabel("")
        self.summary_label.setObjectName("MutedText")
        self.install_all_btn = QPushButton("Install / Update All Detected")
        self.install_all_btn.setObjectName("SubtleToolButton")
        self.install_all_btn.clicked.connect(self._on_install_all)
        self.rescan_btn = QPushButton("Re-Scan")
        self.rescan_btn.setIcon(get_icon("refresh", TEXT_SECONDARY, ICON_SM))
        self.rescan_btn.setIconSize(QSize(ICON_SM, ICON_SM))
        self.rescan_btn.clicked.connect(self._on_rescan)
        top_layout.addWidget(hdr_title)
        top_layout.addSpacing(8)
        top_layout.addWidget(self.summary_label, 1)
        top_layout.addWidget(self.install_all_btn)
        top_layout.addWidget(self.rescan_btn)
        root.addWidget(top_bar)

        # Master-Detail Splitter
        splitter = QSplitter(Qt.Orientation.Horizontal)

        # Left Master List
        left_card = QFrame()
        left_card.setObjectName("PanelCard")
        left_layout = QVBoxLayout(left_card)
        left_layout.setContentsMargins(8, 8, 8, 8)
        left_hdr = QLabel("Applications")
        left_hdr.setObjectName("SectionHeader")
        left_layout.addWidget(left_hdr)

        self.dcc_list = QTreeWidget()
        self.dcc_list.setRootIsDecorated(False)
        self.dcc_list.setHeaderLabels(["Application", "Status"])
        self.dcc_list.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.dcc_list.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.dcc_list.currentItemChanged.connect(self._on_dcc_selected)
        left_layout.addWidget(self.dcc_list, 1)
        splitter.addWidget(left_card)

        # Right Detail Area
        right_card = QFrame()
        right_card.setObjectName("PanelCard")
        right_layout = QVBoxLayout(right_card)
        right_layout.setContentsMargins(12, 10, 12, 10)
        right_layout.setSpacing(8)

        # Detail Header
        d_hdr = QHBoxLayout()
        title_col = QVBoxLayout()
        title_col.setSpacing(2)
        self.detail_title = QLabel("")
        tfont = QFont(self.font())
        tfont.setPointSize(13)
        tfont.setBold(True)
        self.detail_title.setFont(tfont)
        self.detail_tagline = QLabel("")
        self.detail_tagline.setObjectName("MutedText")
        self.detail_tagline.setWordWrap(True)
        title_col.addWidget(self.detail_title)
        title_col.addWidget(self.detail_tagline)
        d_hdr.addLayout(title_col, 1)

        self.detail_status_pill = QLabel("")
        d_hdr.addWidget(self.detail_status_pill, 0, Qt.AlignmentFlag.AlignTop)
        right_layout.addLayout(d_hdr)

        # Primary Action Toolbar
        act_row = QHBoxLayout()
        self.primary_install_btn = QPushButton("Install Integration")
        self.primary_install_btn.setObjectName("PrimaryButton")
        self.primary_install_btn.clicked.connect(self._on_install_current)
        self.test_conn_btn = QPushButton("Test Connection")
        self.test_conn_btn.clicked.connect(self._on_test_connection)
        self.launch_app_btn = QPushButton("Launch App")
        self.launch_app_btn.clicked.connect(self._on_launch_app)
        self.stop_bridge_btn = QPushButton("Stop Lua Bridge")
        self.stop_bridge_btn.clicked.connect(self._on_stop_bridge)
        self.uninstall_btn = QPushButton("Uninstall")
        self.uninstall_btn.setObjectName("DangerButton")
        self.uninstall_btn.clicked.connect(lambda: self._on_uninstall_current(confirm=True))
        act_row.addWidget(self.primary_install_btn)
        act_row.addWidget(self.test_conn_btn)
        act_row.addWidget(self.launch_app_btn)
        act_row.addWidget(self.stop_bridge_btn)
        act_row.addStretch()
        act_row.addWidget(self.uninstall_btn)
        right_layout.addLayout(act_row)

        # Inline Feedback Banner
        self.feedback_label = InlineStatusBanner(self)
        right_layout.addWidget(self.feedback_label)

        # Detail Tabs
        self.detail_tabs = QTabWidget()

        # Tab 0: Setup & Health
        tab_health = QWidget()
        th_layout = QVBoxLayout(tab_health)
        th_layout.setSpacing(8)

        self.health_table = QTableWidget(0, 3)
        self.health_table.setHorizontalHeaderLabels(["", "Check", "Details"])
        self.health_table.verticalHeader().setVisible(False)
        self.health_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.health_table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self.health_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.health_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.health_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        th_layout.addWidget(self.health_table, 1)

        cfg_form = QFormLayout()
        self.target_dir_label = QLabel("Scripts / Resource Folder:")
        tdir_row = QHBoxLayout()
        self.target_dir_edit = QLineEdit()
        self.target_dir_edit.editingFinished.connect(self._on_target_dir_edited)
        tdir_browse = QPushButton("Browse…")
        tdir_browse.clicked.connect(self._browse_target_dir)
        tdir_auto = QPushButton("Auto-Detect")
        tdir_auto.setObjectName("SubtleToolButton")
        tdir_auto.clicked.connect(self._on_auto_detect_target)
        tdir_row.addWidget(self.target_dir_edit, 1)
        tdir_row.addWidget(tdir_browse)
        tdir_row.addWidget(tdir_auto)
        cfg_form.addRow(self.target_dir_label, tdir_row)

        exe_row = QHBoxLayout()
        self.exe_edit = QLineEdit()
        exe_browse = QPushButton("Browse…")
        exe_browse.clicked.connect(self._browse_exe)
        exe_auto = QPushButton("Auto-Detect")
        exe_auto.setObjectName("SubtleToolButton")
        exe_auto.clicked.connect(self._on_auto_detect_exe)
        exe_row.addWidget(self.exe_edit, 1)
        exe_row.addWidget(exe_browse)
        exe_row.addWidget(exe_auto)
        cfg_form.addRow("Executable:", exe_row)
        th_layout.addLayout(cfg_form)

        opts_row = QHBoxLayout()
        self.autostart_chk = QCheckBox("Auto-start Obol Bridge on REAPER launch (Scripts/__startup.lua)")
        self.autostart_chk.setChecked(True)
        self.save_exe_chk = QCheckBox("Save executable in Obol Software Profiles")
        self.save_exe_chk.setChecked(True)
        opts_row.addWidget(self.autostart_chk)
        opts_row.addWidget(self.save_exe_chk)
        opts_row.addStretch()
        th_layout.addLayout(opts_row)

        self.quick_links_layout = QHBoxLayout()
        th_layout.addLayout(self.quick_links_layout)
        self.detail_tabs.addTab(tab_health, "Setup && Health")

        # Tab 1: Guide & Installed Files
        tab_guide = QWidget()
        tg_layout = QVBoxLayout(tab_guide)
        guide_hdr = QLabel("Quick Start")
        guide_hdr.setObjectName("SectionHeader")
        tg_layout.addWidget(guide_hdr)
        self.guide_label = QLabel("")
        self.guide_label.setWordWrap(True)
        self.guide_label.setTextFormat(Qt.TextFormat.RichText)
        tg_layout.addWidget(self.guide_label)

        files_hdr = QLabel("Installed Files")
        files_hdr.setObjectName("SectionHeader")
        tg_layout.addWidget(files_hdr)
        self.files_list = QListWidget()
        tg_layout.addWidget(self.files_list, 1)
        self.detail_tabs.addTab(tab_guide, "Guide && Files")

        # Tab 2: Resolve Project Bindings
        tab_bindings = QWidget()
        tb_layout = QVBoxLayout(tab_bindings)
        tb_info = QLabel(
            "Remembered mappings between DaVinci Resolve project names and Obol components "
            "(used automatically by Export & Open Obol Studio scripts):"
        )
        tb_info.setObjectName("MutedText")
        tb_info.setWordWrap(True)
        tb_layout.addWidget(tb_info)

        self.bindings_table = QTableWidget(0, 3)
        self.bindings_table.setHorizontalHeaderLabels(["Resolve Project", "Obol Project", "Obol Component"])
        self.bindings_table.verticalHeader().setVisible(False)
        self.bindings_table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.bindings_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.bindings_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.bindings_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.bindings_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        tb_layout.addWidget(self.bindings_table, 1)

        tb_btns = QHBoxLayout()
        tb_btns.addStretch()
        self.remove_binding_btn = QPushButton("Remove Selected Binding")
        self.remove_binding_btn.setObjectName("DangerButton")
        self.remove_binding_btn.clicked.connect(self._on_remove_selected_binding)
        tb_btns.addWidget(self.remove_binding_btn)
        tb_layout.addLayout(tb_btns)
        self.detail_tabs.addTab(tab_bindings, "Project Bindings")

        right_layout.addWidget(self.detail_tabs, 1)
        splitter.addWidget(right_card)
        splitter.setSizes([240, 680])
        root.addWidget(splitter, 1)

        close_btn = QPushButton("Close")
        close_btn.clicked.connect(self.accept)
        root.addWidget(DialogFooter(self, cancel_button=close_btn))

        self._refresh_reports(select_key=self._current_key)

    def set_feedback(self, message: str, level: str = "ok") -> None:
        self.feedback_label.set_feedback(message, level)

    def _refresh_reports(self, select_key: Optional[str] = None) -> None:
        reports = self.manager.list_reports(target_dir_overrides=self._custom_target_dirs)
        self._reports = {r.key: r for r in reports}

        detected_cnt = sum(1 for r in reports if r.detected)
        installed_cnt = sum(1 for r in reports if r.installed)
        live_cnt = sum(1 for r in reports if r.bridge_connected)
        drift_cnt = sum(1 for r in reports if r.needs_update)
        summary = f"{detected_cnt} detected  •  {installed_cnt} installed  •  {live_cnt} live"
        if drift_cnt:
            summary += f"  •  {drift_cnt} update(s) available"
        self.summary_label.setText(summary)
        self.install_all_btn.setEnabled(detected_cnt > 0)

        target_key = select_key or self._current_key
        self.dcc_list.blockSignals(True)
        self.dcc_list.clear()
        chosen_item: Optional[QTreeWidgetItem] = None
        for rep in reports:
            badge_txt, badge_color, _ = _STATE_DISPLAY.get(rep.overall_state, ("Unknown", "#a1a1aa", "StatusPillLocal"))
            it = QTreeWidgetItem(self.dcc_list, [rep.display_name, badge_txt])
            it.setData(0, Qt.ItemDataRole.UserRole, rep.key)
            it.setForeground(1, QColor(badge_color))
            if chosen_item is None or rep.key == target_key:
                chosen_item = it
        self.dcc_list.blockSignals(False)
        if chosen_item is not None:
            self.dcc_list.setCurrentItem(chosen_item)
            self._populate_detail(str(chosen_item.data(0, Qt.ItemDataRole.UserRole)))

    def _on_dcc_selected(self, current: Optional[QTreeWidgetItem], _prev: Optional[QTreeWidgetItem]) -> None:
        if current is None:
            return
        key = str(current.data(0, Qt.ItemDataRole.UserRole) or "")
        if key:
            self._current_key = key
            self._populate_detail(key)

    def _populate_detail(self, key: str) -> None:
        rep = self._reports.get(key)
        if rep is None:
            return
        self._current_key = key
        self.detail_title.setText(rep.display_name)
        self.detail_tagline.setText(rep.tagline)

        badge_txt, _, pill_obj = _STATE_DISPLAY.get(rep.overall_state, ("Offline", "#a1a1aa", "StatusPillLocal"))
        self.detail_status_pill.setText(badge_txt)
        self.detail_status_pill.setObjectName(pill_obj)
        repolish(self.detail_status_pill)

        if rep.needs_update:
            self.primary_install_btn.setText("Update / Repair Integration")
        elif rep.installed:
            self.primary_install_btn.setText("Reinstall Integration")
        else:
            self.primary_install_btn.setText("Install Integration")

        self.uninstall_btn.setEnabled(rep.installed)
        self.launch_app_btn.setEnabled(rep.executable_exists)
        self.stop_bridge_btn.setVisible(key == "resolve" and rep.bridge_mode == "lua_bridge")

        # Health Table
        self.health_table.setRowCount(0)
        for chk in rep.checks:
            row = self.health_table.rowCount()
            self.health_table.insertRow(row)
            sym, color = _CHECK_BADGES.get(chk.state, ("•", "#a1a1aa"))
            s_it = QTableWidgetItem(sym)
            s_it.setForeground(QColor(color))
            s_it.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
            l_it = QTableWidgetItem(chk.label)
            d_it = QTableWidgetItem(chk.detail)
            self.health_table.setItem(row, 0, s_it)
            self.health_table.setItem(row, 1, l_it)
            self.health_table.setItem(row, 2, d_it)

        self.target_dir_label.setText(rep.target_dir_label)
        self.target_dir_edit.setText(rep.target_dir)
        self.exe_edit.setText(rep.executable)
        self.autostart_chk.setVisible(key == "reaper")
        if key == "reaper":
            self.autostart_chk.setChecked(bool(rep.extra.get("startup_hook_installed", True)) or not rep.installed)

        # Quick folder buttons
        while self.quick_links_layout.count():
            item = self.quick_links_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        for lbl, fpath in rep.quick_links:
            btn = QPushButton(lbl)
            btn.setObjectName("SubtleToolButton")
            btn.clicked.connect(lambda _chk=False, p=fpath: open_folder_in_os(Path(p)))
            self.quick_links_layout.addWidget(btn)
        self.quick_links_layout.addStretch()

        # Guide & Files tab
        steps_html = "<ol>" + "".join(f"<li>{step}</li>" for step in rep.usage_steps) + "</ol>"
        self.guide_label.setText(steps_html)
        self.files_list.clear()
        for fpath in rep.installed_files:
            self.files_list.addItem(fpath)

        # Bindings tab (Resolve only)
        is_resolve = key == "resolve"
        self.detail_tabs.setTabVisible(2, is_resolve)
        if is_resolve:
            bindings = dict(rep.extra.get("bindings") or {})
            self.bindings_table.setRowCount(0)
            for rproj, info in sorted(bindings.items()):
                r = self.bindings_table.rowCount()
                self.bindings_table.insertRow(r)
                self.bindings_table.setItem(r, 0, QTableWidgetItem(rproj))
                self.bindings_table.setItem(r, 1, QTableWidgetItem(str(info.get("project_name", ""))))
                self.bindings_table.setItem(r, 2, QTableWidgetItem(str(info.get("component_path", ""))))
            self.remove_binding_btn.setEnabled(bool(bindings))

    def _current_target_path(self) -> Optional[Path]:
        txt = self.target_dir_edit.text().strip()
        return Path(txt) if txt else None

    def _on_target_dir_edited(self) -> None:
        tdir = self._current_target_path()
        if tdir:
            self._custom_target_dirs[self._current_key] = tdir
        else:
            self._custom_target_dirs.pop(self._current_key, None)
        self._refresh_reports(select_key=self._current_key)

    def _browse_target_dir(self) -> None:
        folder = QFileDialog.getExistingDirectory(
            self,
            f"Select {self.target_dir_label.text().rstrip(':')}",
            self.target_dir_edit.text(),
        )
        if folder:
            self.target_dir_edit.setText(folder)
            self._custom_target_dirs[self._current_key] = Path(folder)
            self._refresh_reports(select_key=self._current_key)

    def _browse_exe(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Select Application Executable", self.exe_edit.text())
        if path:
            self.exe_edit.setText(path)

    def _on_auto_detect_target(self) -> None:
        self._custom_target_dirs.pop(self._current_key, None)
        paths = self.manager.auto_detect_paths(self._current_key)
        if paths["target_dir"]:
            self.target_dir_edit.setText(paths["target_dir"])
            self._refresh_reports(select_key=self._current_key)
            self.set_feedback(f"✓ Auto-detected folder: {paths['target_dir']}", "ok")
        else:
            self.set_feedback("Could not auto-detect target folder on this machine.", "warn")

    def _on_auto_detect_exe(self) -> None:
        paths = self.manager.auto_detect_paths(self._current_key)
        if paths["executable"]:
            self.exe_edit.setText(paths["executable"])
            self.set_feedback(f"✓ Auto-detected executable: {paths['executable']}", "ok")
        else:
            self.set_feedback("Could not auto-detect executable on this machine.", "warn")

    def _on_rescan(self) -> None:
        self._refresh_reports(select_key=self._current_key)
        self.set_feedback("✓ Re-scanned DCC installations and bridge status.", "info")

    def _on_install_current(self) -> None:
        tdir = self._current_target_path()
        if tdir is None:
            self.set_feedback("Select or auto-detect a target folder first.", "warn")
            return
        try:
            self._custom_target_dirs[self._current_key] = tdir
            rep = self.manager.install(
                self._current_key,
                target_dir=tdir,
                executable=self.exe_edit.text().strip(),
                options={
                    "auto_start_bridge": self.autostart_chk.isChecked(),
                    "configure_executable": self.save_exe_chk.isChecked(),
                },
            )
            self._refresh_reports(select_key=self._current_key)
            self.set_feedback(
                f"✓ Installed / updated {rep.display_name} integration ({len(rep.installed_files)} files).",
                "ok",
            )
        except Exception as exc:
            self.set_feedback(f"Install failed: {exc}", "error")

    def _on_install_all(self) -> None:
        try:
            updated = self.manager.install_all_detected(target_dir_overrides=self._custom_target_dirs)
            count = sum(1 for r in updated if r.installed)
            self._refresh_reports(select_key=self._current_key)
            self.set_feedback(f"✓ Installed / updated {count} detected DCC integration(s).", "ok")
        except Exception as exc:
            self.set_feedback(f"Batch install failed: {exc}", "error")

    def _on_uninstall_current(self, confirm: bool = True) -> None:
        rep = self._reports.get(self._current_key)
        if not rep:
            return
        if confirm and not confirm_destructive_action(
            self,
            "Uninstall Integration",
            f"Remove Obol scripts and hooks for {rep.display_name}?",
            confirm_label="Uninstall",
        ):
            return
        self.manager.uninstall(self._current_key, target_dir=self._current_target_path())
        self._refresh_reports(select_key=self._current_key)
        self.set_feedback(f"Removed Obol integration for {rep.display_name}.", "info")

    def _on_test_connection(self) -> None:
        ok, msg = self.manager.test_connection(self._current_key)
        self._refresh_reports(select_key=self._current_key)
        self.set_feedback(msg, "ok" if ok else "warn")

    def _on_launch_app(self) -> None:
        ok, msg = self.manager.launch_dcc(self._current_key)
        self.set_feedback(msg, "ok" if ok else "warn")

    def _on_stop_bridge(self) -> None:
        ok, msg = self.manager.stop_bridge(self._current_key)
        self._refresh_reports(select_key=self._current_key)
        self.set_feedback(msg, "info" if ok else "warn")

    def _on_remove_selected_binding(self) -> None:
        row = self.bindings_table.currentRow()
        if row < 0:
            return
        item = self.bindings_table.item(row, 0)
        if not item:
            return
        rproj = item.text()
        if ResolveBridgeClient(self.hub, allow_studio_api=False).remove_project_binding(rproj):
            self._refresh_reports(select_key="resolve")
            self.set_feedback(f"✓ Removed Resolve binding for '{rproj}'.", "info")


def launch_integrations_manager(hub: ObolHub, initial_dcc: str = "") -> int:
    """Standalone Qt launcher for `obol integrations ui`."""
    app = QApplication.instance() or QApplication(sys.argv)
    icon_path = Path(__file__).parent / "assets" / "obol_icon.png"
    if icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))
    apply_studio_dark_theme(app)
    dlg = IntegrationsManagerDialog(None, hub, initial_dcc=initial_dcc)
    dlg.exec()
    return 0
