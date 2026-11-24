"""Automated unit, live headless FilmCraft CLI, and headless Qt tests for FilmCraft integration."""

from __future__ import annotations

import json
import os
import struct
import tempfile
import unittest
from unittest.mock import patch
import wave
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from obol.cli.main import run_cli
from obol.core.hub import ObolHub
from obol.core.models import default_software_profiles
from obol.core.storage import load_component_snapshot, save_component_snapshot
from obol.core.workspace import ObolWorkspace
from obol.gui.app import ObolMainWindow
from obol.gui.filmcraft_dialogs import FilmCraftExportPublishDialog, FilmCraftImportDialog
from obol.gui.integrations_dialog import IntegrationsManagerDialog
from obol.plugins.filmcraft import FilmCraftPlugin
from obol.plugins.filmcraft_bridge import (
    FilmCraftBridgeClient,
    detect_filmcraft_cli_executable,
    is_valid_control_token,
)
from obol.plugins.filmcraft_installer import FilmCraftInstaller


def _write_test_wav(path: Path, seconds: float = 0.25) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    frames = int(48000 * seconds)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(48000)
        wf.writeframes(struct.pack("<" + "h" * frames, *([0] * frames)))


class TestFilmCraftIntegration(unittest.TestCase):
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

    def test_profile_template_seed_and_on_version_created(self) -> None:
        profiles = default_software_profiles()
        self.assertIn("filmcraft", profiles)
        self.assertEqual(profiles["filmcraft"].plugin, "filmcraft")
        self.assertIn(".fcproj", profiles["filmcraft"].extensions)

        tm = self.hub.template_manager
        task_tpls = tm.list_task_templates()
        self.assertIn("filmcraft_video", task_tpls)
        self.assertEqual(task_tpls["filmcraft_video"].software, "filmcraft")
        self.assertIn("filmcraft_default.fcproj", tm.list_file_templates())

        plug = self.hub.plugin_registry.get("filmcraft")
        self.assertEqual(plug.name, "filmcraft")

        proj = self.ws.init_project("fc_proj", self.root / "fc_proj", custom_components=[])
        snap = self.ws.create_component(proj, "edit/trailer", task_template="filmcraft_video")
        self.assertEqual(snap.software, "filmcraft")

        v1_dir = proj / "edit" / "trailer" / "working" / "v001"
        v1_fcproj = v1_dir / "trailer_v001.fcproj"
        self.assertTrue(v1_fcproj.is_file())
        parsed = json.loads(v1_fcproj.read_text(encoding="utf-8"))
        self.assertEqual(parsed["format"], "filmcraft.project")
        self.assertEqual(parsed["project"]["name"], "trailer_v001")
        self.assertEqual(parsed["project"]["root"]["name"], "trailer_v001")

        # Stray backup files in v001 are ignored on version up, and tidy_workspace moves backups inside v002 into .backups/
        (v1_dir / "trailer_v001.fcproj-bak").write_text("bak", encoding="utf-8")
        b2 = self.ws.version_up_working(proj, "edit/trailer", comment="Cut v2")
        v2_dir = proj / "edit" / "trailer" / "working" / "v002"
        v2_fcproj = proj / "edit" / "trailer" / b2.primary_file
        self.assertTrue(v2_fcproj.is_file())
        self.assertFalse((v2_dir / "trailer_v001.fcproj-bak").exists())
        parsed2 = json.loads(v2_fcproj.read_text(encoding="utf-8"))
        self.assertEqual(parsed2["project"]["name"], "trailer_v002")

        (v2_dir / "trailer_v002.fcproj-bak").write_text("bak2", encoding="utf-8")
        self.assertEqual(FilmCraftPlugin.tidy_workspace(v2_dir), 1)
        self.assertTrue((v2_dir / ".backups" / "trailer_v002.fcproj-bak").is_file())

    def test_mismatched_task_template_does_not_leak_md_seed_to_filmcraft(self) -> None:
        proj = self.ws.init_project("wkly_leak", self.root / "wkly_leak", custom_components=[])

        # Case 1: Component created with generic_media template + software_override="filmcraft"
        self.ws.create_component(
            proj,
            "video_override",
            task_template="generic_media",
            software_override="filmcraft",
            seed_initial_working=True,
        )
        ov_v1 = proj / "video_override" / "working" / "v001" / "video_override_v001.fcproj"
        self.assertTrue(ov_v1.is_file())
        self.assertFalse((proj / "video_override" / "working" / "v001" / "video_override_v001.md").exists())
        self.assertEqual(json.loads(ov_v1.read_text(encoding="utf-8"))["format"], "filmcraft.project")

        # Case 2: Component created with generic_media (unseeded), rebound to filmcraft, then versioned up
        snap = self.ws.create_component(
            proj,
            "video",
            task_template="generic_media",
            seed_initial_working=False,
        )
        snap.software = "filmcraft"
        save_component_snapshot(proj, snap)
        bundle = self.ws.version_up_working(proj, "video")
        self.assertEqual(bundle.primary_file, "working/v001/video_v001.fcproj")
        vid_v1 = proj / "video" / bundle.primary_file
        self.assertTrue(vid_v1.is_file())
        self.assertFalse((proj / "video" / "working" / "v001" / "video_v001.md").exists())
        parsed = json.loads(vid_v1.read_text(encoding="utf-8"))
        self.assertEqual(parsed["format"], "filmcraft.project")
        self.assertEqual(parsed["project"]["name"], "video_v001")

    def test_dependency_scan_and_collect_relinks_relative_paths(self) -> None:
        proj = self.ws.init_project("fc_deps", self.root / "fc_deps", custom_components=[])
        self.ws.create_component(proj, "video", task_template="filmcraft_video")
        v1_fcproj = proj / "video" / "working" / "v001" / "video_v001.fcproj"

        ext_wav = self.root / "external_media" / "Dialogue_Take_01.wav"
        _write_test_wav(ext_wav)

        # Inject a media item referencing external file + a synthetic generator item (which must be ignored)
        data = json.loads(v1_fcproj.read_text(encoding="utf-8"))
        data["project"]["root"]["children"] = [{"Bin": {"id": 1, "name": "Audio", "children": [{"Item": 2}, {"Item": 3}]}}]
        data["project"]["items"] = {
            "2": {
                "id": 2,
                "name": "Dialogue_Take_01.wav",
                "label": "Caribbean",
                "kind": {
                    "Media": {
                        "media": {"File": {"path": str(ext_wav)}},
                        "offline": False,
                    }
                },
            },
            "3": {
                "id": 3,
                "name": "Bars and Tone",
                "label": "Lavender",
                "kind": {"Media": {"media": {"Generator": {"BarsAndTone": {}}}}},
            },
        }
        data["project"]["next_id"] = 4
        v1_fcproj.write_text(json.dumps(data), encoding="utf-8")

        externals = self.ws.scan_external_dependencies(proj, "video", working_version=1)
        self.assertEqual(len(externals), 1)
        self.assertEqual(externals[0].suggested_category, "audio")
        self.assertEqual(externals[0].suggested_asset_name, "dialogue_take_01")

        collected = self.ws.collect_external_dependencies(proj, "video", working_version=1)
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0].asset_path, "audio/dialogue_take_01")

        # Verify .fcproj was rewritten to a clean relative path inside repo/
        updated = json.loads(v1_fcproj.read_text(encoding="utf-8"))
        new_path = updated["project"]["items"]["2"]["kind"]["Media"]["media"]["File"]["path"]
        self.assertEqual(new_path, "../../repo/audio/dialogue_take_01/v001/dialogue_take_01_v001.wav")

        # Re-scanning should find 0 externals and record used_by_working_versions
        externals_after = self.ws.scan_external_dependencies(proj, "video", working_version=1)
        self.assertEqual(externals_after, [])
        snap = load_component_snapshot(proj, "video")
        self.assertIn(1, snap.repo_assets["audio/dialogue_take_01"].used_by_working_versions)

        items = FilmCraftPlugin.extract_media_items(v1_fcproj)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["bin_path"], "Audio")
        self.assertEqual(items[0]["id"], 2)

    def test_installer_status_drift_and_uninstall(self) -> None:
        fake_dir = self.root / "FilmCraftInstall"
        fake_dir.mkdir(parents=True, exist_ok=True)
        (fake_dir / "filmcraft.exe").write_bytes(b"MZ")
        (fake_dir / "filmcraft-cli.exe").write_bytes(b"MZ")

        installer = FilmCraftInstaller(self.hub)
        st0 = installer.status(install_dir=fake_dir)
        self.assertTrue(st0["install_dir_exists"])
        self.assertFalse(st0["config_installed"])

        st1 = installer.install(install_dir=fake_dir, control_port=48500)
        self.assertTrue(st1["config_installed"])
        self.assertTrue(st1["launcher_installed"])
        self.assertTrue(st1["config_in_sync"])
        self.assertEqual(st1["control_port"], 48500)
        self.assertEqual(self.hub.get_software_profile("filmcraft").executable, str((fake_dir / "filmcraft.exe").resolve()))

        # Simulate drift
        cfg_file = Path(st1["config_path"])
        cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
        cfg["integration_version"] = "0.1"
        cfg_file.write_text(json.dumps(cfg), encoding="utf-8")
        st_drift = installer.status(install_dir=fake_dir)
        self.assertFalse(st_drift["config_in_sync"])

        st2 = installer.uninstall(install_dir=fake_dir)
        self.assertFalse(st2["config_installed"])
        self.assertFalse(st2["launcher_installed"])

    def test_control_token_persistence_and_subprocess_env(self) -> None:
        fake_dir = self.root / "FilmCraftEnv"
        fake_dir.mkdir(parents=True, exist_ok=True)
        (fake_dir / "filmcraft.exe").write_bytes(b"MZ")
        (fake_dir / "filmcraft-cli.exe").write_bytes(b"MZ")
        fcproj = self.root / "env_test.fcproj"
        fcproj.write_text("{}", encoding="utf-8")

        installer = FilmCraftInstaller(self.hub)
        st = installer.install(install_dir=fake_dir, control_port=48600)
        self.assertTrue(st["control_token_valid"])
        self.assertTrue(st["config_in_sync"])

        cfg = json.loads(Path(st["config_path"]).read_text(encoding="utf-8"))
        tok = cfg["control_token"]
        self.assertTrue(is_valid_control_token(tok))

        # Re-installing preserves the same token; launcher script exports it
        st_again = installer.install(install_dir=fake_dir, control_port=48600)
        cfg_again = json.loads(Path(st_again["config_path"]).read_text(encoding="utf-8"))
        self.assertEqual(cfg_again["control_token"], tok)
        launcher_txt = Path(st["launcher_path"]).read_text(encoding="utf-8")
        self.assertIn(f"FILMCRAFT_CONTROL_TOKEN={tok}", launcher_txt)

        # Invalid/missing token causes inspect_config_sync to flag drift
        cfg_again["control_token"] = "too_short"
        Path(st["config_path"]).write_text(json.dumps(cfg_again), encoding="utf-8")
        ok_sync, _ = installer.inspect_config_sync(install_dir=fake_dir)
        self.assertFalse(ok_sync)

        # Restore valid token via bridge client and verify Popen & run receive env vars
        cfg_again.pop("control_token", None)
        Path(st["config_path"]).write_text(json.dumps(cfg_again), encoding="utf-8")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("FILMCRAFT_CONTROL_TOKEN", None)
            client = FilmCraftBridgeClient(
                self.hub,
                executable=fake_dir / "filmcraft.exe",
                cli_executable=fake_dir / "filmcraft-cli.exe",
            )
            generated_tok = client.ensure_control_token()
            self.assertTrue(is_valid_control_token(generated_tok))
            client2 = FilmCraftBridgeClient(self.hub)
            self.assertEqual(client2.ensure_control_token(), generated_tok)

            with patch("obol.plugins.filmcraft_bridge.subprocess.Popen") as mock_popen:
                client.launch_app(fcproj_path=fcproj, with_control=True)
                mock_popen.assert_called_once()
                popen_env = mock_popen.call_args.kwargs["env"]
                self.assertEqual(popen_env["FILMCRAFT_CONTROL_TOKEN"], generated_tok)
                self.assertEqual(popen_env["FILMCRAFT_CONTROL_PORT"], "48600")

            with patch("obol.plugins.filmcraft_bridge.subprocess.run") as mock_run:
                mock_run.return_value.returncode = 0
                mock_run.return_value.stdout = "{}"
                mock_run.return_value.stderr = ""
                client.run_cli_cmd(["inspect", "project"], project_path=fcproj)
                mock_run.assert_called_once()
                run_env = mock_run.call_args.kwargs["env"]
                self.assertEqual(run_env["FILMCRAFT_CONTROL_TOKEN"], generated_tok)
                self.assertEqual(run_env["FILMCRAFT_CONTROL_PORT"], "48600")

    def test_cross_component_fcproj_fallback_when_audio_selected(self) -> None:
        proj = self.ws.init_project("cross_comp", self.root / "cross_comp", custom_components=[])
        self.ws.create_component(proj, "audio", task_template="reaper_daw")
        self.ws.create_component(proj, "video", task_template="filmcraft_video")
        expected_fcproj = (proj / "video" / "working" / "v001" / "video_v001.fcproj").resolve()
        self.assertTrue(expected_fcproj.is_file())

        win = ObolMainWindow(hub=self.hub, initial_project="cross_comp", initial_component="audio")
        self.assertIsNone(win._active_working_fcproj(fallback_any_component=False))
        self.assertEqual(win._active_working_fcproj(fallback_any_component=True), expected_fcproj)
        win.close()
        win.deleteLater()

        imp_dlg = FilmCraftImportDialog(
            None,
            self.hub,
            self.ws,
            FilmCraftBridgeClient(self.hub),
            default_project="cross_comp",
            default_component="audio",
        )
        combo_paths = [
            Path(str(imp_dlg.target_fcproj_combo.itemData(i))).resolve()
            for i in range(imp_dlg.target_fcproj_combo.count())
            if imp_dlg.target_fcproj_combo.itemData(i) not in (None, "", "__live__")
        ]
        self.assertIn(expected_fcproj, combo_paths)
        imp_dlg.close()
        imp_dlg.deleteLater()
        self.app.processEvents()

    def test_live_headless_filmcraft_cli_workflow_and_dialogs(self) -> None:
        cli_exe = detect_filmcraft_cli_executable()
        if cli_exe is None:
            self.skipTest("filmcraft-cli.exe is not installed on this machine")

        proj = self.ws.init_project("fc_live", self.root / "fc_live", custom_components=[])
        self.ws.create_component(proj, "edit", task_template="filmcraft_video")
        v1_fcproj = proj / "edit" / "working" / "v001" / "edit_v001.fcproj"

        # Ingest v1 & v2 audio asset into repo/
        src_v1 = self.root / "src_audio" / "score_take1.wav"
        src_v2 = self.root / "src_audio" / "score_take2.wav"
        _write_test_wav(src_v1, seconds=0.2)
        _write_test_wav(src_v2, seconds=0.2)
        asset, b1 = self.ws.ingest_repo_asset(proj, "edit", src_v1, asset_path="audio/score")
        v1_repo_file = proj / "edit" / b1.primary_file

        bridge = FilmCraftBridgeClient(self.hub, cli_executable=cli_exe)
        presets = bridge.list_export_presets()
        self.assertGreater(len(presets), 0)

        # Import v1 repo audio into nested FilmCraft bin and create a sequence from it
        imp = bridge.import_media([v1_repo_file], bin_path="Obol/edit/repo", project_path=v1_fcproj, use_bridge=False)
        self.assertEqual(imp["imported"], 1)
        item_id = int(imp["items"][0])

        bridge.exec_command(
            "file.newSequenceFromClip",
            {"items": [item_id]},
            project_path=v1_fcproj,
            save=True,
            use_bridge=False,
        )
        seqs = bridge.list_sequences(project_path=v1_fcproj, use_bridge=False)
        self.assertGreaterEqual(len(seqs), 1)

        # Ingest v2 of the repo asset and test FilmCraftImportDialog update flow
        _, b2 = self.ws.ingest_repo_asset(proj, "edit", src_v2, asset_path="audio/score")
        imp_dlg = FilmCraftImportDialog(
            None,
            self.hub,
            self.ws,
            bridge,
            active_fcproj=str(v1_fcproj),
            default_project="fc_live",
            default_component="edit",
        )
        imp_dlg.scan_project_updates()
        self.assertEqual(imp_dlg.updates_table.rowCount(), 1)
        self.assertEqual(imp_dlg.updates_table.item(0, 5).text(), "Update Available")
        with patch("obol.gui.filmcraft_dialogs.QMessageBox.information"):
            imp_dlg._on_apply_updates()
        self.assertEqual(imp_dlg.updates_table.item(0, 5).text(), "Up to Date")
        imp_dlg.close()
        imp_dlg.deleteLater()
        self.app.processEvents()

        # Export directly via FilmCraftExportPublishDialog into published/main/v001/
        exp_dlg = FilmCraftExportPublishDialog(
            None,
            self.hub,
            self.ws,
            bridge,
            active_fcproj=str(v1_fcproj),
            default_project="fc_live",
            default_component="edit",
        )
        wav_idx = exp_dlg.format_combo.findText("wav")
        self.assertGreaterEqual(wav_idx, 0)
        exp_dlg.format_combo.setCurrentIndex(wav_idx)
        exp_dlg.note_edit.setText("Headless FilmCraft WAV export")
        with patch("obol.gui.filmcraft_dialogs.QMessageBox.information"):
            exp_dlg._on_execute()
        self.assertIsNotNone(exp_dlg.published_bundle)
        assert exp_dlg.published_bundle is not None
        self.assertEqual(exp_dlg.published_bundle.version, 1)
        self.assertTrue((proj / "edit" / exp_dlg.published_bundle.primary_file).is_file())
        exp_dlg.close()
        exp_dlg.deleteLater()
        self.app.processEvents()

        # Verify CLI commands: context, version-up, interchange export
        self.assertEqual(run_cli(["filmcraft", "context", "--fcproj", str(v1_fcproj), "--json"], hub=self.hub), 0)
        self.assertEqual(run_cli(["filmcraft", "version-up", "--fcproj", str(v1_fcproj), "-m", "CLI v2", "--json"], hub=self.hub), 0)
        v2_fcproj = proj / "edit" / "working" / "v002" / "edit_v002.fcproj"
        self.assertTrue(v2_fcproj.is_file())
        self.assertEqual(
            run_cli(
                ["filmcraft", "interchange", "--fcproj", str(v2_fcproj), "--format", "xml", "--name", "edl_pack", "--json"],
                hub=self.hub,
            ),
            0,
        )
        snap = load_component_snapshot(proj, "edit")
        self.assertIn("edl_pack", snap.published_deliverables)

    def test_main_window_and_integrations_dialog_filmcraft_ui(self) -> None:
        fake_dir = self.root / "UI_FC"
        fake_dir.mkdir(parents=True, exist_ok=True)
        (fake_dir / "filmcraft.exe").write_bytes(b"MZ")
        (fake_dir / "filmcraft-cli.exe").write_bytes(b"MZ")

        proj = self.ws.init_project("fc_ui", self.root / "fc_ui", custom_components=[])
        self.ws.create_component(proj, "edit", task_template="filmcraft_video")

        win = ObolMainWindow(hub=self.hub, initial_project="fc_ui", initial_component="edit")
        self.assertFalse(win.filmcraft_export_btn.isHidden())
        self.assertFalse(win.collect_media_btn.isHidden())
        self.assertIn(".fcproj", win.collect_media_btn.text())
        win.close()
        win.deleteLater()

        dlg = IntegrationsManagerDialog(
            None,
            self.hub,
            initial_dcc="filmcraft",
            allow_resolve_studio_api=False,
            target_dir_overrides={"filmcraft": fake_dir},
        )
        self.assertEqual(dlg._current_key, "filmcraft")
        dlg._on_install_current()
        self.assertIn("Installed", dlg.feedback_label.text())
        dlg.close()
        dlg.deleteLater()
        self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
