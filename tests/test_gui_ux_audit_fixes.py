"""Regression and verification tests for Obol UI/UX Audit Improvement Plan (Phases 1-3)."""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QCoreApplication, QEvent, QEventLoop, QMimeData, QPointF, QSize, Qt, QThread, QTimer, QUrl
from PySide6.QtGui import QCloseEvent, QDropEvent, QIcon
from PySide6.QtWidgets import QApplication, QDialog, QPushButton, QTreeWidgetItem, QWidget
from shiboken6 import isValid as qt_is_valid

from obol.core.hub import ObolHub
from obol.core.storage import load_component_snapshot
from obol.core.workspace import ObolWorkspace
from obol.gui import tokens
from obol.gui.app import ObolMainWindow
from obol.gui.components import (
    _ACTIVE_BUSY_WORKERS,
    InlineStatusBanner,
    ShortcutsDialog,
    open_folder_in_os,
    run_with_busy_state,
)
from obol.gui.dialogs import (
    IngestWorkingDialog,
    PublishDeliverableDialog,
    SmartIngestDialog,
    TemplateManagerDialog,
)
from obol.gui.drop_widgets import DropListWidget, DropTreeWidget
from obol.gui.filmcraft_dialogs import FilmCraftExportPublishDialog
from obol.gui.icons import get_icon, get_svg_path
from obol.gui.reaper_dialogs import ReaperIntegrationDialog
from obol.gui.resolve_dialogs import ResolveIntegrationDialog
from obol.gui.theme import get_studio_dark_stylesheet
from obol.plugins.filmcraft_bridge import FilmCraftBridgeClient
from obol.plugins.reaper_bridge import ReaperBridgeClient
from obol.plugins.resolve_bridge import ResolveBridgeClient


class TestGuiUxAuditFixes(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)

    def tearDown(self) -> None:
        self.app.processEvents()
        self.tmp.cleanup()

    def test_tokens_icons_components_and_theme(self) -> None:
        self.assertEqual(tokens.TEXT_MUTED, "#9ca3af")
        self.assertEqual(tokens.TEXT_DISABLED, "#71717a")
        self.assertEqual(tokens.ICON_BTN_SIZE, 28)

        # Filled more_horiz SVG + size-aware SVG generation
        more_svg = Path(get_svg_path("more_horiz", tokens.TEXT_SECONDARY, size=20)).read_text(encoding="utf-8")
        self.assertIn('width="20" height="20"', more_svg)
        self.assertIn(f'fill="{tokens.TEXT_SECONDARY}"', more_svg)
        self.assertIn('r="1.6"', more_svg)

        # Multi-state QIcon (Normal + Disabled)
        icon = get_icon("version_up", tokens.TEXT_PRIMARY, size=14, disabled_color=tokens.TEXT_DISABLED)
        self.assertFalse(icon.isNull())
        self.assertFalse(icon.pixmap(QSize(14, 14), QIcon.Mode.Normal).isNull())
        self.assertFalse(icon.pixmap(QSize(14, 14), QIcon.Mode.Disabled).isNull())
        for wf_icon in ("ingest", "publish", "dcc_pull", "keyboard", "info", "check_circle", "x_circle"):
            self.assertFalse(get_icon(wf_icon).isNull())

        # InlineStatusBanner
        banner = InlineStatusBanner()
        self.assertTrue(banner.isHidden())
        banner.set_feedback("Saved!", "ok")
        self.assertEqual(banner.objectName(), "FeedbackSuccess")
        self.assertEqual(banner.text(), "Saved!")
        banner.set_feedback("Warning!", "warn")
        self.assertEqual(banner.objectName(), "FeedbackWarning")
        banner.set_feedback("Error!", "error")
        self.assertEqual(banner.objectName(), "FeedbackError")
        banner.close()
        banner.deleteLater()

        # ShortcutsDialog
        sc_dlg = ShortcutsDialog()
        self.assertEqual(sc_dlg.table.rowCount(), len(ShortcutsDialog.SHORTCUTS))
        sc_dlg.close()
        sc_dlg.deleteLater()
        self.app.processEvents()

        # open_folder_in_os handles missing folder safely
        self.assertFalse(open_folder_in_os(self.root / "does_not_exist"))

        # Theme stylesheet checks
        qss = get_studio_dark_stylesheet()
        for snippet in (
            ":focus",
            "QPushButton#PrimaryButton:focus",
            "QLineEdit:read-only",
            "QDoubleSpinBox",
            "QPushButton#StatusPillOffline:hover",
            "#ErrorText",
            "#FeedbackSuccess",
            "#AccentOutlineButton",
            "#9ca3af",
        ):
            self.assertIn(snippet, qss)

    def test_drop_list_and_tree_filter_empty_state(self) -> None:
        dl = DropListWidget()
        dl.addItem("alpha")
        self.assertEqual(dl.count(), 1)
        dl.setCurrentRow(0)
        self.assertEqual(dl.currentRow(), 0)
        dl.close()
        dl.deleteLater()

        dt = DropTreeWidget()
        item = QTreeWidgetItem(dt, ["kick"])
        item.setHidden(True)
        dt.set_filter_active(True, "snare")
        dt.resize(300, 200)
        dt.grab()  # triggers paintEvent headlessly
        self.assertTrue(dt._filter_active)
        self.assertEqual(dt._filter_query, "snare")
        dt.close()
        dt.deleteLater()
        self.app.processEvents()

    def test_destructive_confirmations_abort_on_cancel(self) -> None:
        # TemplateManagerDialog delete confirmations
        tpl_dlg = TemplateManagerDialog(None, self.hub)
        tpl_dlg.pt_name_edit.setText("temp_proj_tpl")
        tpl_dlg.pt_comps_edit.setPlainText("audio")
        tpl_dlg._save_project_template()
        self.assertIn("temp_proj_tpl", self.hub.template_manager.list_project_templates())

        with patch("obol.gui.dialogs.confirm_destructive_action", return_value=False) as mock_confirm:
            tpl_dlg._delete_project_template()
            mock_confirm.assert_called_once()
        self.assertIn("temp_proj_tpl", self.hub.template_manager.list_project_templates())

        with patch("obol.gui.dialogs.confirm_destructive_action", return_value=True):
            tpl_dlg._delete_project_template()
        self.assertNotIn("temp_proj_tpl", self.hub.template_manager.list_project_templates())
        tpl_dlg.close()
        tpl_dlg.deleteLater()

        # ReaperIntegrationDialog & ResolveIntegrationDialog uninstall confirmations
        reap_dir = self.root / "REAPER_RES"
        reap_dir.mkdir()
        r_dlg = ReaperIntegrationDialog(None, self.hub, ReaperBridgeClient(self.hub))
        r_dlg.res_dir_edit.setText(str(reap_dir))
        with patch("obol.gui.reaper_dialogs.QMessageBox.information"):
            r_dlg._on_install()
        self.assertTrue((reap_dir / "Scripts" / "Obol" / "obol_bridge.lua").is_file())
        with patch("obol.gui.reaper_dialogs.confirm_destructive_action", return_value=False) as mock_r_conf:
            r_dlg._on_uninstall()
            mock_r_conf.assert_called_once()
        self.assertTrue((reap_dir / "Scripts" / "Obol" / "obol_bridge.lua").is_file())
        r_dlg.close()
        r_dlg.deleteLater()

        res_dir = self.root / "RESOLVE_SCRIPTS"
        res_dir.mkdir()
        d_dlg = ResolveIntegrationDialog(None, self.hub, ResolveBridgeClient(self.hub, allow_studio_api=False))
        d_dlg.scripts_dir_edit.setText(str(res_dir))
        with patch("obol.gui.resolve_dialogs.QMessageBox.information"):
            d_dlg._on_install()
        self.assertTrue((res_dir / "Utility" / "Obol - Load Media into Media Pool.lua").is_file())
        with patch("obol.gui.resolve_dialogs.confirm_destructive_action", return_value=False) as mock_d_conf:
            d_dlg._on_uninstall()
            mock_d_conf.assert_called_once()
        self.assertTrue((res_dir / "Utility" / "Obol - Load Media into Media Pool.lua").is_file())
        d_dlg.close()
        d_dlg.deleteLater()
        self.app.processEvents()

    def test_unsaved_metadata_guard_when_autosave_off(self) -> None:
        proj_root = self.ws.init_project("guard_proj", self.root / "guard_proj", project_template="single_track")
        self.hub.config.auto_save_metadata = False
        self.hub.save_config()

        win = ObolMainWindow(hub=self.hub, initial_project="guard_proj")
        try:
            win.triad_tabs.setCurrentIndex(0)
            win.insp_comment_edit.setText("Unsaved guard draft")
            win._on_metadata_field_edited()
            self.assertTrue(win._meta_dirty)

            # Cancel on tab switch keeps tab 0 and preserves dirty text
            with patch.object(win, "_confirm_discard_or_save_dirty_metadata", return_value=False):
                win.triad_tabs.setCurrentIndex(1)
                self.assertEqual(win.triad_tabs.currentIndex(), 0)
                self.assertTrue(win._meta_dirty)

            # Cancel on closeEvent blocks close
            with patch.object(win, "_confirm_discard_or_save_dirty_metadata", return_value=False):
                evt = QCloseEvent()
                win.closeEvent(evt)
                self.assertFalse(evt.isAccepted())

            # Save branch persists changes cleanly
            class _FakeMsgBox:
                class Icon:
                    Warning = 1

                class ButtonRole:
                    AcceptRole = 0
                    DestructiveRole = 2

                class StandardButton:
                    Cancel = 4

                def __init__(self, *_a, **_kw) -> None:
                    self._buttons: dict[str, object] = {}

                def setIcon(self, *_a) -> None:
                    pass

                def setWindowTitle(self, *_a) -> None:
                    pass

                def setText(self, *_a) -> None:
                    pass

                def setInformativeText(self, *_a) -> None:
                    pass

                def addButton(self, label, _role=None):
                    btn = object()
                    self._buttons[str(label)] = btn
                    return btn

                def setDefaultButton(self, *_a) -> None:
                    pass

                def exec(self) -> int:
                    return 0

                def clickedButton(self):
                    return self._buttons["Save"]

            with patch("obol.gui.app.QMessageBox", _FakeMsgBox):
                win.triad_tabs.setCurrentIndex(1)
            self.assertEqual(win.triad_tabs.currentIndex(), 1)
            self.assertFalse(win._meta_dirty)
            snap = load_component_snapshot(proj_root, win.active_component_path)
            self.assertEqual(snap.working_versions[-1].comment, "Unsaved guard draft")
        finally:
            win.close()
            win.deleteLater()
            self.app.processEvents()

    def test_group_and_category_selection_clears_stale_inspector(self) -> None:
        proj_root = self.ws.init_project("cat_proj", self.root / "cat_proj", project_template="music_album")
        comp = "song_1/arrangement"
        wav = self.root / "kick.wav"
        wav.write_bytes(b"KICK")
        self.ws.ingest_repo_asset(proj_root, comp, wav, asset_path="drums/kick")
        pub = self.root / "mix.wav"
        pub.write_bytes(b"MIX")
        self.ws.publish_version(proj_root, comp, [pub], deliverable_name="stems/drums")

        win = ObolMainWindow(hub=self.hub, initial_project="cat_proj", initial_component=comp)
        try:
            # Select Group node in component_tree
            song_group = next(
                win.component_tree.topLevelItem(i)
                for i in range(win.component_tree.topLevelItemCount())
                if win.component_tree.topLevelItem(i).text(0) == "song_1/"
            )
            win.component_tree.setCurrentItem(song_group)
            self.assertEqual(win.insp_context_badge.text(), "Group")
            self.assertEqual(win.selected_item_label.text(), "song_1/")
            self.assertIsNone(win._insp_target_ctx)
            self.assertFalse(win.save_meta_btn.isEnabled())
            self.assertFalse(win.delete_selected_btn.isEnabled())

            # Return to component via Open Selected Component button
            win.group_children_list.setCurrentRow(0)
            self.assertTrue(win.open_group_child_btn.isEnabled())
            win.open_group_child_btn.click()
            self.assertEqual(win.workspace_stack.currentIndex(), 0)

            # Select Repo Category node
            win.triad_tabs.setCurrentIndex(1)
            repo_cat = win.repo_asset_tree.topLevelItem(0)
            self.assertEqual(repo_cat.data(0, Qt.ItemDataRole.UserRole + 1), "repo_category")
            win.repo_asset_tree.setCurrentItem(repo_cat)
            self.assertEqual(win.insp_context_badge.text(), "Category")
            self.assertEqual(win.selected_item_label.text(), "repo/drums/")
            self.assertEqual(win.repo_version_list.topLevelItemCount(), 0)
            self.assertIsNone(win._insp_target_ctx)
            self.assertFalse(win.save_meta_btn.isEnabled())

            # Select Deliverable Category node
            win.triad_tabs.setCurrentIndex(2)
            deliv_cat = win.published_deliverable_tree.topLevelItem(0)
            self.assertEqual(deliv_cat.data(0, Qt.ItemDataRole.UserRole + 1), "deliv_category")
            win.published_deliverable_tree.setCurrentItem(deliv_cat)
            self.assertEqual(win.insp_context_badge.text(), "Category")
            self.assertEqual(win.selected_item_label.text(), "published/stems/")
            self.assertEqual(win.published_version_tree.topLevelItemCount(), 0)
            self.assertIsNone(win._insp_target_ctx)
            self.assertFalse(win.save_meta_btn.isEnabled())
        finally:
            win.close()
            win.deleteLater()
            self.app.processEvents()

    def test_fast_drop_decoupled_from_toolbar_buttons(self) -> None:
        proj_root = self.ws.init_project("fd_proj", self.root / "fd_proj", project_template="single_track")
        self.hub.config.fast_drop = True
        self.hub.save_config()

        win = ObolMainWindow(hub=self.hub, initial_project="fd_proj")
        try:
            comp = win.active_component_path
            # Toolbar Ingest… / Publish… still open dialogs even with Fast Drop ON
            with patch("obol.gui.app.SmartIngestDialog") as mock_ing_dlg:
                mock_ing_dlg.return_value.exec.return_value = SmartIngestDialog.DialogCode.Rejected
                win._action_ingest_repo_file()
                mock_ing_dlg.assert_called_once()

            with patch("obol.gui.app.PublishDeliverableDialog") as mock_pub_dlg:
                mock_pub_dlg.return_value.exec.return_value = PublishDeliverableDialog.DialogCode.Rejected
                win._action_publish_deliverable()
                mock_pub_dlg.assert_called_once()

            # Drag-and-drop still fast-ingests when Fast Drop ON
            wav = self.root / "fast_drop_hit.wav"
            wav.write_bytes(b"WAV")
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(wav))])
            evt = QDropEvent(
                QPointF(5.0, 200.0),
                Qt.DropAction.CopyAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            win.repo_asset_tree.dropEvent(evt)
            snap = load_component_snapshot(proj_root, comp)
            self.assertIn("audio/fast_drop_hit", snap.repo_assets)

            # IngestWorkingDialog returns data ("copy"/"move"), not display text
            rpp = self.root / "test.rpp"
            rpp.write_text("<REAPER_PROJECT\n>\n", encoding="utf-8")
            wdlg = IngestWorkingDialog(win, [rpp], comp, 2)
            wdlg.copy_mode_combo.setCurrentIndex(1)
            self.assertEqual(wdlg.get_values()["copy_mode"], "move")
            wdlg.close()
        finally:
            win.close()
            win.deleteLater()
            self.app.processEvents()

    def test_filmcraft_range_combo_controls_custom_spins(self) -> None:
        proj = self.ws.init_project("fc_range", self.root / "fc_range", custom_components=[])
        self.ws.create_component(proj, "edit", task_template="filmcraft_video")
        bridge = FilmCraftBridgeClient(self.hub)
        exp_dlg = FilmCraftExportPublishDialog(
            None,
            self.hub,
            self.ws,
            bridge,
            default_project="fc_range",
            default_component="edit",
        )
        try:
            self.assertEqual(exp_dlg.range_combo.currentData(), "entire")
            self.assertFalse(exp_dlg.start_sec_spin.isEnabled())
            self.assertFalse(exp_dlg.end_sec_spin.isEnabled())

            custom_idx = exp_dlg.range_combo.findData("custom")
            self.assertGreaterEqual(custom_idx, 0)
            exp_dlg.range_combo.setCurrentIndex(custom_idx)
            self.assertTrue(exp_dlg.start_sec_spin.isEnabled())
            self.assertTrue(exp_dlg.end_sec_spin.isEnabled())

            in_out_idx = exp_dlg.range_combo.findData("inOut")
            exp_dlg.range_combo.setCurrentIndex(in_out_idx)
            self.assertFalse(exp_dlg.start_sec_spin.isEnabled())
            self.assertFalse(exp_dlg.end_sec_spin.isEnabled())
        finally:
            exp_dlg.close()
            exp_dlg.deleteLater()
            self.app.processEvents()

    def test_run_with_busy_state_threaded_main_thread_and_lifecycle(self) -> None:
        main_thread = self.app.thread()

        def _wait_until(cond, timeout_ms: int = 5000) -> None:
            loop = QEventLoop()
            timed_out = False

            def _poll() -> None:
                if cond():
                    loop.quit()

            def _on_timeout() -> None:
                nonlocal timed_out
                timed_out = True
                loop.quit()

            poll_timer = QTimer()
            poll_timer.setInterval(15)
            poll_timer.timeout.connect(_poll)
            poll_timer.start()

            timeout_timer = QTimer()
            timeout_timer.setSingleShot(True)
            timeout_timer.timeout.connect(_on_timeout)
            timeout_timer.start(timeout_ms)

            if not cond():
                loop.exec()
            poll_timer.stop()
            timeout_timer.stop()
            self.assertFalse(timed_out, "Timed out waiting for busy worker completion")

        with patch.object(self.app, "platformName", return_value="windows"):
            # 1. Success path: work runs off main thread, on_success runs on main thread & can parent widgets
            parent = QWidget()
            btn = QPushButton("Sync", parent)
            work_threads: list[bool] = []
            cb_threads: list[bool] = []
            results: list[object] = []
            done_flag = [False]

            def _work_ok() -> str:
                work_threads.append(QThread.currentThread() is main_thread)
                return "ok_payload"

            def _on_ok(res: object) -> None:
                cb_threads.append(QThread.currentThread() is main_thread)
                child = QPushButton("child", parent)
                self.assertIs(child.parent(), parent)
                results.append(res)
                done_flag[0] = True

            run_with_busy_state(parent, _work_ok, on_success=_on_ok, disable_widgets=[btn])
            _wait_until(lambda: done_flag[0] and not _ACTIVE_BUSY_WORKERS)
            self.app.processEvents()
            self.assertEqual(work_threads, [False])
            self.assertEqual(cb_threads, [True])
            self.assertEqual(results, ["ok_payload"])
            self.assertTrue(btn.isEnabled())
            self.assertIsNone(QApplication.overrideCursor())
            self.assertEqual(_ACTIVE_BUSY_WORKERS, [])
            parent.close()
            parent.deleteLater()
            self.app.processEvents()

            # 2. Error path: exception delivered to on_error on main thread
            err_parent = QWidget()
            err_btn = QPushButton("Err", err_parent)
            err_threads: list[bool] = []
            err_msgs: list[str] = []
            err_done = [False]

            def _work_fail() -> None:
                raise RuntimeError("boom")

            def _on_err(exc: Exception) -> None:
                err_threads.append(QThread.currentThread() is main_thread)
                err_msgs.append(str(exc))
                err_done[0] = True

            run_with_busy_state(err_parent, _work_fail, on_error=_on_err, disable_widgets=[err_btn])
            _wait_until(lambda: err_done[0] and not _ACTIVE_BUSY_WORKERS)
            self.app.processEvents()
            self.assertEqual(err_threads, [True])
            self.assertEqual(err_msgs, ["boom"])
            self.assertTrue(err_btn.isEnabled())
            self.assertIsNone(QApplication.overrideCursor())
            self.assertEqual(_ACTIVE_BUSY_WORKERS, [])
            err_parent.close()
            err_parent.deleteLater()
            self.app.processEvents()

            # 3. Real ObolMainWindow._action_refresh_and_reconcile() (Sync button)
            sync_proj_root = self.ws.init_project("sync_proj", self.root / "sync_proj", project_template="single_track")
            win = ObolMainWindow(hub=self.hub, initial_project="sync_proj")
            try:
                comp = win.active_component_path
                ext_dir = sync_proj_root / comp / "working" / "v002"
                ext_dir.mkdir(parents=True, exist_ok=True)
                (ext_dir / "ext_sync.rpp").write_text("<REAPER_PROJECT\n>\n", encoding="utf-8")

                win._action_refresh_and_reconcile()
                _wait_until(lambda: win.refresh_btn.isEnabled() and not _ACTIVE_BUSY_WORKERS)
                self.app.processEvents()
                self.assertTrue(win.refresh_btn.isEnabled())
                self.assertIn("Synced & refreshed from disk", win.status_feedback_label.text())
                self.assertIsNone(QApplication.overrideCursor())
                self.assertEqual(_ACTIVE_BUSY_WORKERS, [])
                snap = load_component_snapshot(sync_proj_root, comp)
                self.assertEqual([w.version for w in snap.working_versions], [1, 2])
            finally:
                win.close()
                win.deleteLater()
                self.app.processEvents()

            # 4. Destroyed parent dialog while background thread is still running
            dlg = QDialog()
            started_evt = threading.Event()
            release_evt = threading.Event()
            cb_called = [False]

            def _blocking_work() -> str:
                started_evt.set()
                release_evt.wait(timeout=4.0)
                return "finished_after_close"

            def _never_called(_res: object) -> None:
                cb_called[0] = True

            try:
                run_with_busy_state(dlg, _blocking_work, on_success=_never_called)
                self.assertTrue(started_evt.wait(timeout=2.0))
                dlg.close()
                dlg.deleteLater()
                QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
                self.app.processEvents()
                self.assertFalse(qt_is_valid(dlg))
            finally:
                release_evt.set()

            _wait_until(lambda: not _ACTIVE_BUSY_WORKERS)
            self.app.processEvents()
            self.assertFalse(cb_called[0])
            self.assertIsNone(QApplication.overrideCursor())
            self.assertEqual(_ACTIVE_BUSY_WORKERS, [])


if __name__ == "__main__":
    unittest.main()
