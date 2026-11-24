"""Headless smoke tests for the 3-Pane PySide6 Desktop GUI and CLI interface."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication

from obol.cli.main import run_cli
from obol.core.hub import ObolHub
from obol.core.storage import load_component_snapshot
from obol.core.sync import SyncEngine
from obol.core.workspace import ObolWorkspace
from obol.gui.app import ObolMainWindow
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


class TestGuiAndCliSmoke(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)
        self.sync = SyncEngine(self.hub)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cli_end_to_end_commands(self) -> None:
        client_root = self.root / "client_projects"
        self.assertEqual(run_cli(["root", "set", str(client_root)], hub=self.hub), 0)
        self.assertEqual(
            run_cli(["project", "create", "cli_album", "--template", "music_album"], hub=self.hub),
            0,
        )
        self.assertTrue((client_root / "cli_album" / ".obol" / "project.toml").is_file())
        self.assertEqual(
            run_cli(["task", "create", "song_1/vocals", "-p", "cli_album", "--template", "reaper_daw"], hub=self.hub),
            0,
        )
        sample = self.root / "sample.wav"
        sample.write_bytes(b"SAMPLE")
        self.assertEqual(
            run_cli(
                [
                    "ingest",
                    str(sample),
                    "--task",
                    "song_1/vocals",
                    "-p",
                    "cli_album",
                    "--author",
                    "InspectorJ",
                    "--source",
                    "freesound.org",
                    "--license",
                    "CC-BY-4.0",
                ],
                hub=self.hub,
            ),
            0,
        )
        self.assertEqual(
            run_cli(["work", "up", "song_1/vocals", "-p", "cli_album", "-m", "v2 arrangement"], hub=self.hub),
            0,
        )
        render_out = self.root / "master.wav"
        render_out.write_bytes(b"MASTER-WAV")
        self.assertEqual(
            run_cli(["publish", str(render_out), "--task", "song_1/vocals", "-p", "cli_album"], hub=self.hub),
            0,
        )
        self.assertEqual(run_cli(["credits", "-p", "cli_album"], hub=self.hub), 0)

    def test_three_pane_main_window_and_dialogs_instantiate(self) -> None:
        self.ws.init_project("gui_proj", self.root / "gui_proj", project_template="music_album")
        win = ObolMainWindow(hub=self.hub)
        self.assertEqual(win.active_project_name, "gui_proj")
        self.assertGreater(win.component_tree.topLevelItemCount(), 0)
        self.assertEqual(win.triad_tabs.count(), 3)

        # Instantiate dialogs headlessly and test project creation via NewProjectWizardDialog
        wiz = NewProjectWizardDialog(win, self.hub, self.ws)
        self.assertGreater(wiz.preview_tree.topLevelItemCount(), 0)
        wiz.name_edit.setText("wizard_created_proj")
        wiz.root_dir_edit.setText(str(self.root))
        wiz._create_project()
        self.assertEqual(wiz.created_project_name, "wizard_created_proj")
        self.assertTrue((self.root / "wizard_created_proj" / ".obol" / "project.toml").is_file())

        dummy_file = self.root / "drop.wav"
        dummy_file.write_bytes(b"WAV")
        ing = SmartIngestDialog(win, dummy_file, "audio/drop", "v001", "Dan")
        vals = ing.get_values()
        self.assertEqual(vals["asset_path"], "audio/drop")

        sw_dlg = SoftwareSettingsDialog(win, self.hub)
        self.assertGreater(sw_dlg.sw_list.count(), 0)

        pv_dlg = ProjectAndVaultManagerDialog(win, self.hub, self.sync)
        self.assertEqual(pv_dlg.table.rowCount(), 2)

        cred_dlg = CreditsReportDialog(win, "gui_proj", [])
        self.assertIsNotNone(cred_dlg)

        tpl_dlg = TemplateManagerDialog(win, self.hub, active_project_root=win.active_project_root)
        self.assertGreater(tpl_dlg.proj_tpl_list.count(), 0)
        self.assertGreater(tpl_dlg.task_tpl_list.count(), 0)
        self.assertGreater(tpl_dlg.seed_files_table.rowCount(), 0)
        tpl_dlg.pt_name_edit.setText("weekly_show")
        tpl_dlg.pt_comps_edit.setPlainText("audio\nvideo\nthumbnail")
        tpl_dlg._save_project_template()
        self.assertIn("weekly_show", self.hub.template_manager.list_project_templates())

    def test_dialogs_and_headless_drag_and_drop_across_triad(self) -> None:
        self.ws.init_project("drop_proj", self.root / "drop_proj", project_template="single_track")
        self.hub.config.fast_drop = True
        self.hub.save_config()

        win = ObolMainWindow(hub=self.hub)
        comp = win.active_component_path

        def _drop_files(widget: object, files: list[Path], pos: QPointF | None = None) -> None:
            mime = QMimeData()
            mime.setUrls([QUrl.fromLocalFile(str(f)) for f in files])
            drop_pos = pos if pos is not None else QPointF(5.0, 5.0)
            evt = QDropEvent(
                drop_pos,
                Qt.DropAction.CopyAction,
                mime,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            widget.dropEvent(evt)  # type: ignore[attr-defined]

        # 1. Drop working file onto working_list
        drop_rpp = self.root / "dropped_work.rpp"
        drop_rpp.write_text("<REAPER_PROJECT\n>\n", encoding="utf-8")
        _drop_files(win.working_list, [drop_rpp])
        snap = load_component_snapshot(win.active_project_root, comp)  # type: ignore[arg-type]
        self.assertEqual(len(snap.working_versions), 2)

        # 2. Drop repo files onto repo_asset_tree (new) and repo_version_list (v002)
        drop_wav = self.root / "snare_hit.wav"
        drop_wav.write_bytes(b"SNARE-V1")
        _drop_files(win.repo_asset_tree, [drop_wav], pos=QPointF(5.0, 200.0))
        snap = load_component_snapshot(win.active_project_root, comp)  # type: ignore[arg-type]
        self.assertIn("audio/snare_hit", snap.repo_assets)

        drop_wav_v2 = self.root / "snare_hit_bright.wav"
        drop_wav_v2.write_bytes(b"SNARE-V2")
        _drop_files(win.repo_version_list, [drop_wav_v2])
        snap = load_component_snapshot(win.active_project_root, comp)  # type: ignore[arg-type]
        self.assertEqual(snap.repo_assets["audio/snare_hit"].latest_version, 2)

        # 3. Drop multi-file-type deliverables (.wav + .mid) onto published_deliverable_tree
        pub_wav = self.root / "mix_out.wav"
        pub_mid = self.root / "mix_out.mid"
        pub_mp3 = self.root / "mix_out.mp3"
        pub_wav.write_bytes(b"PUB-WAV")
        pub_mid.write_bytes(b"PUB-MID")
        pub_mp3.write_bytes(b"PUB-MP3")

        _drop_files(win.published_deliverable_tree, [pub_wav, pub_mid], pos=QPointF(5.0, 200.0))
        snap = load_component_snapshot(win.active_project_root, comp)  # type: ignore[arg-type]
        self.assertIn("main", snap.published_deliverables)
        self.assertEqual(len(snap.published_deliverables["main"].versions[0].files), 2)

        # Drop .mp3 directly onto the v001 row in published_version_tree to attach it
        v1_item = win.published_version_tree.topLevelItem(0)
        self.assertIsNotNone(v1_item)
        rect = win.published_version_tree.visualItemRect(v1_item)
        _drop_files(win.published_version_tree, [pub_mp3], pos=QPointF(rect.center()))
        snap = load_component_snapshot(win.active_project_root, comp)  # type: ignore[arg-type]
        self.assertEqual(len(snap.published_deliverables["main"].versions[0].files), 3)

        # 4. Instantiate SmartIngestDialog batch mode, IngestWorkingDialog, and PublishDeliverableDialog
        batch_dlg = SmartIngestDialog(
            win,
            source_file=drop_wav,
            suggested_asset_path="audio/snare_hit",
            source_files=[drop_wav, pub_mid],
            categories=["audio", "midi"],
            suggested_paths=["audio/snare_hit", "midi/mix_out"],
        )
        self.assertTrue(batch_dlg.is_batch)
        self.assertEqual(len(batch_dlg.get_batch_entries()), 2)

        work_dlg = IngestWorkingDialog(win, [drop_rpp], comp, 3)
        self.assertTrue(work_dlg.get_values()["rename_to_convention"])

        pub_dlg = PublishDeliverableDialog(
            win,
            initial_files=[pub_wav, pub_mid],
            component_leaf=comp,
            working_versions=[1, 2],
            existing_deliverables={"main": [1]},
            publish_formats=[".wav", ".mid", ".mp3"],
        )
        pvals = pub_dlg.get_values()
        self.assertEqual(pvals["deliverable_name"], "main")
        self.assertEqual(len(pvals["publish_items"]), 2)  # type: ignore[arg-type]

        # 5. Name-first SmartIngestDialog and PublishDeliverableDialog with no initial files
        name_first_repo = SmartIngestDialog(win, source_file=None, categories=["audio", "midi"])
        self.assertFalse(name_first_repo.name_edit.isReadOnly())
        name_first_repo.name_edit.setText("lead_synth")
        name_first_repo.category_combo.setCurrentText("audio")
        name_first_repo.set_source_file(drop_wav)
        self.assertEqual(name_first_repo.get_values()["asset_path"], "audio/lead_synth")

        # Even when lock_asset_name=True is passed, Name remains editable
        unlocked_repo = SmartIngestDialog(
            win, source_file=drop_wav, suggested_asset_path="audio/snare_hit", lock_asset_name=True
        )
        self.assertFalse(unlocked_repo.name_edit.isReadOnly())

        name_first_pub = PublishDeliverableDialog(
            win,
            initial_files=[],
            component_leaf=comp,
            working_versions=[1, 2],
            existing_deliverables={"main": [1]},
        )
        name_first_pub.deliverable_combo.setCurrentText("stems")
        name_first_pub.add_files([pub_wav])
        self.assertEqual(name_first_pub.get_values()["deliverable_name"], "stems")

    def test_ui_ux_hierarchy_inspector_and_dialogs(self) -> None:
        # 1. Zero-project welcome screen
        win = ObolMainWindow(hub=self.hub)
        self.assertEqual(win.central_stack.currentIndex(), 0)

        # 2. Nested component hierarchy & search filtering
        proj_root = self.ws.init_project("album_ux", self.root / "album_ux", project_template="music_album")
        win.active_project_name = "album_ux"
        win.refresh_all()
        self.assertEqual(win.central_stack.currentIndex(), 1)

        # Find song_1/ group item and verify child components
        group_items = [
            win.component_tree.topLevelItem(i)
            for i in range(win.component_tree.topLevelItemCount())
            if win.component_tree.topLevelItem(i).text(0) == "song_1/"
        ]
        self.assertEqual(len(group_items), 1)
        self.assertEqual(group_items[0].childCount(), 2)

        win.comp_filter_edit.setText("cover")
        self.assertTrue(group_items[0].isHidden())
        win.comp_filter_edit.clear()
        self.assertFalse(group_items[0].isHidden())

        # 3. NewProjectWizardDialog per-component template & seed overrides persist to disk
        wiz = NewProjectWizardDialog(win, self.hub, self.ws)
        wiz.proj_tpl_combo.setCurrentText("simple_media")
        wiz.name_edit.setText("override_proj")
        wiz.root_dir_edit.setText(str(self.root))
        wiz._comp_task_overrides["audio"] = "blender_3d"
        wiz._comp_seed_overrides["audio"] = "notes.md"
        wiz._create_project()
        audio_snap = load_component_snapshot(self.root / "override_proj", "audio")
        self.assertEqual(audio_snap.task_template, "blender_3d")
        self.assertEqual(audio_snap.software, "blender")
        self.assertTrue(audio_snap.working_versions[0].primary_file.endswith(".md"))

        # 4. AddComponentDialog defaults & creation
        add_dlg = AddComponentDialog(win, self.hub, initial_prefix="song_1")
        self.assertEqual(add_dlg.path_edit.text(), "song_1/")
        add_dlg.path_edit.setText("song_1/vocals")
        add_dlg.task_tpl_combo.setCurrentText("krita_art")
        vals = add_dlg.get_values()
        self.assertEqual(vals["component_path"], "song_1/vocals")
        self.assertEqual(vals["software_override"], "krita")

        # 5. Context-aware Inspector metadata saving across all 3 Triad tabs
        comp = "song_1/arrangement"
        win.active_component_path = comp
        win._load_active_project()

        # Tab 0: Working metadata
        win.triad_tabs.setCurrentIndex(0)
        self.assertEqual(win.insp_context_badge.text(), "Working Version")
        win.insp_comment_edit.setText("Arrangement pass 1")
        win.insp_tags_edit.setText("rough, tempo120")
        win._save_inspector_metadata()
        snap = load_component_snapshot(proj_root, comp)
        self.assertEqual(snap.working_versions[0].comment, "Arrangement pass 1")
        self.assertEqual(snap.working_versions[0].tags, ["rough", "tempo120"])

        # Tab 1: Repo metadata
        bass_wav = self.root / "bass.wav"
        bass_wav.write_bytes(b"BASS")
        self.ws.ingest_repo_asset(proj_root, comp, bass_wav, asset_path="audio/bass")
        win.selected_repo_asset_path = "audio/bass"
        win._refresh_component_triad()
        win.triad_tabs.setCurrentIndex(1)
        self.assertEqual(win.insp_context_badge.text(), "Repo Asset")
        win.insp_author_edit.setText("Jane Doe")
        win.insp_license_combo.setCurrentText("CC-BY-4.0")
        win.insp_comment_edit.setText("Clean DI bass")
        win._save_inspector_metadata()
        snap = load_component_snapshot(proj_root, comp)
        self.assertEqual(snap.repo_assets["audio/bass"].author, "Jane Doe")
        self.assertEqual(snap.repo_assets["audio/bass"].license, "CC-BY-4.0")
        self.assertEqual(snap.repo_assets["audio/bass"].versions[0].comment, "Clean DI bass")
        # Working metadata remains untouched
        self.assertEqual(snap.working_versions[0].comment, "Arrangement pass 1")

        # Tab 2: Published metadata
        pub_wav = self.root / "arr_mix.wav"
        pub_wav.write_bytes(b"MIX")
        self.ws.publish_version(proj_root, comp, source_files=[pub_wav], deliverable_name="main")
        win.selected_deliverable_path = "main"
        win._refresh_component_triad()
        win.triad_tabs.setCurrentIndex(2)
        self.assertEqual(win.insp_context_badge.text(), "Published")
        win.insp_comment_edit.setText("Client review v1")
        win.insp_tags_edit.setText("approved")
        win._save_inspector_metadata()
        snap = load_component_snapshot(proj_root, comp)
        self.assertEqual(snap.published_deliverables["main"].versions[0].comment, "Client review v1")
        self.assertEqual(snap.published_deliverables["main"].versions[0].tags, ["approved"])

        # Bound software change updates component_tree immediately
        sw_idx = win.insp_software_combo.findData("blender")
        win.insp_software_combo.setCurrentIndex(sw_idx)
        curr_comp_item = win.component_tree.currentItem()
        self.assertIsNotNone(curr_comp_item)
        self.assertEqual(curr_comp_item.text(1), "blender")

        # 6. CreditsReportDialog filter & Markdown formatting
        rows = self.ws.generate_credits_report(proj_root)
        cred_dlg = CreditsReportDialog(win, "album_ux", rows)
        self.assertIn("Jane Doe", cred_dlg.format_markdown())
        cred_dlg.filter_edit.setText("nonexistent_xyz")
        self.assertTrue(cred_dlg.table.isRowHidden(0))
        cred_dlg.filter_edit.clear()
        self.assertFalse(cred_dlg.table.isRowHidden(0))


if __name__ == "__main__":
    unittest.main()
