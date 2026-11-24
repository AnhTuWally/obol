"""Automated unit and headless Qt integration tests for bidirectional REAPER DAW support."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QMimeData, QPoint, Qt, QUrl
from PySide6.QtGui import QDropEvent
from PySide6.QtWidgets import QApplication, QTreeWidgetItem

from obol.cli.main import run_cli
from obol.core.hub import ObolHub
from obol.core.workspace import ObolWorkspace
from obol.gui.app import ObolMainWindow
from obol.gui.reaper_dialogs import (
    ReaperExportPublishDialog,
    ReaperImportDialog,
    ReaperIntegrationDialog,
    ReaperOpenDialog,
)
from obol.plugins.reaper_bridge import ReaperBridgeClient, parse_reaper_render_presets
from obol.plugins.reaper_installer import ReaperInstaller
from obol.plugins.reaper_scripts import OBOL_ACTION_SPECS


class TestReaperIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)
        self.bridge = ReaperBridgeClient(self.hub)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_resolve_working_file_context_and_importable_assets(self) -> None:
        proj = self.ws.init_project(
            "album_test",
            self.root / "album_test",
            project_template="music_album",
        )
        rpp_v1 = proj / "song_1" / "arrangement" / "working" / "v001" / "arrangement_v001.rpp"
        self.assertTrue(rpp_v1.is_file())

        ctx = self.ws.resolve_working_file_context(rpp_v1)
        self.assertIsNotNone(ctx)
        assert ctx is not None
        self.assertEqual(ctx.project_name, "album_test")
        self.assertEqual(ctx.component_path, "song_1/arrangement")
        self.assertEqual(ctx.working_version, 1)

        # Outside project returns None
        outside = self.root / "random.rpp"
        outside.write_text("<REAPER_PROJECT\n>\n", encoding="utf-8")
        self.assertIsNone(self.ws.resolve_working_file_context(outside))

        # Ingest repo v1 -> v2 in song_1/mixing and publish stems v1 -> v2 in song_1/arrangement
        src1 = self.root / "kick_1.wav"
        src2 = self.root / "kick_2.wav"
        src1.write_bytes(b"WAV1")
        src2.write_bytes(b"WAV2")
        _, r_b1 = self.ws.ingest_repo_asset(proj, "song_1/mixing", src1, asset_path="audio/kick")
        _, r_b2 = self.ws.ingest_repo_asset(proj, "song_1/mixing", src2, asset_path="audio/kick")

        stem_v1 = self.root / "bass.wav"
        stem_v2 = self.root / "bass_new.wav"
        stem_v1.write_bytes(b"STEM1")
        stem_v2.write_bytes(b"STEM2")
        p_b1 = self.ws.publish_version(proj, "song_1/arrangement", [stem_v1], deliverable_name="stems")
        p_b2 = self.ws.publish_version(proj, "song_1/arrangement", [stem_v2], deliverable_name="stems")

        importable = self.ws.list_importable_assets(proj, current_component="song_1/mixing")
        scopes_and_keys = {(it["scope"], it["component"], it["asset_key"], it["version"]) for it in importable}
        self.assertIn(("repo", "song_1/mixing", "audio/kick", 2), scopes_and_keys)
        self.assertIn(("published", "song_1/arrangement", "stems", 2), scopes_and_keys)

        # Outdated repo asset check
        repo_v1_file = proj / "song_1" / "mixing" / r_b1.primary_file
        repo_v2_file = (proj / "song_1" / "mixing" / r_b2.primary_file).resolve()
        info_repo = self.ws.find_newer_asset_version(proj, repo_v1_file)
        self.assertIsNotNone(info_repo)
        assert info_repo is not None
        self.assertTrue(info_repo["update_available"])
        self.assertEqual(info_repo["current_version"], 1)
        self.assertEqual(info_repo["latest_version"], 2)
        self.assertEqual(info_repo["latest_file"], repo_v2_file)

        # Up-to-date repo asset check
        info_repo_latest = self.ws.find_newer_asset_version(proj, repo_v2_file)
        self.assertIsNotNone(info_repo_latest)
        assert info_repo_latest is not None
        self.assertFalse(info_repo_latest["update_available"])
        self.assertTrue(info_repo_latest["up_to_date"])

        # Outdated published stem check
        pub_v1_file = proj / "song_1" / "arrangement" / p_b1.primary_file
        pub_v2_file = (proj / "song_1" / "arrangement" / p_b2.primary_file).resolve()
        info_pub = self.ws.find_newer_asset_version(proj, pub_v1_file)
        self.assertIsNotNone(info_pub)
        assert info_pub is not None
        self.assertTrue(info_pub["update_available"])
        self.assertEqual(info_pub["kind"], "published")
        self.assertEqual(info_pub["latest_file"], pub_v2_file)

    def test_reaper_bridge_ipc_roundtrip_and_heartbeat(self) -> None:
        # Initially offline
        self.assertFalse(self.bridge.is_connected())

        # Write live heartbeat state
        self.bridge.state_path.write_text(
            json.dumps({
                "connected": True,
                "heartbeat": time.time(),
                "reaper_version": "7.28/win64",
                "active_rpp": "D:/proj/audio/working/v001/audio_v001.rpp",
                "dirty": False,
                "selected_tracks": 2,
                "render_presets": ["Wkly", "Master 48k"],
            }),
            encoding="utf-8",
        )
        st = self.bridge.get_state()
        self.assertTrue(st.connected)
        self.assertEqual(st.reaper_version, "7.28/win64")
        self.assertEqual(st.render_presets, ["Wkly", "Master 48k"])

        # Simulate Lua daemon answering inbox commands
        stop_evt = threading.Event()
        seen_actions: list[str] = []

        def _mock_daemon() -> None:
            while not stop_evt.is_set():
                for f in sorted(self.bridge.inbox_dir.glob("*.json")):
                    req = json.loads(f.read_text(encoding="utf-8"))
                    f.unlink(missing_ok=True)
                    action = req["action"]
                    seen_actions.append(action)
                    if action == "fail_test":
                        resp = {"id": req["id"], "ok": False, "error": "boom"}
                    elif action == "query_session_media":
                        resp = {
                            "id": req["id"],
                            "ok": True,
                            "result": {"items": [{"item_idx": 0, "take_idx": 0, "track_name": "Bass", "file_path": "C:/x.wav"}]},
                        }
                    else:
                        resp = {"id": req["id"], "ok": True, "result": {"echo": action, "params": req["params"]}}
                    out_f = self.bridge.outbox_dir / f"{req['id']}.json"
                    out_f.write_text(json.dumps(resp), encoding="utf-8")
                time.sleep(0.02)

        t = threading.Thread(target=_mock_daemon, daemon=True)
        t.start()
        try:
            rpp = self.root / "song.rpp"
            rpp.write_text("<REAPER_PROJECT\n>\n", encoding="utf-8")
            res_open = self.bridge.open_project(rpp, new_tab=True)
            self.assertEqual(res_open["echo"], "open_project")
            self.assertTrue(res_open["params"]["new_tab"])

            res_ins = self.bridge.insert_media([rpp], mode="cursor_track")
            self.assertEqual(res_ins["params"]["mode"], "cursor_track")

            media = self.bridge.query_session_media()
            self.assertEqual(len(media), 1)
            self.assertEqual(media[0]["track_name"], "Bass")

            res_upd = self.bridge.update_media_sources({str(rpp): str(rpp)})
            self.assertEqual(res_upd["echo"], "update_media_sources")

            res_ren = self.bridge.render_direct(self.root / "out", pattern="$track", render_mode="stems_selected")
            self.assertEqual(res_ren["params"]["render_mode"], "stems_selected")

            with self.assertRaises(RuntimeError):
                self.bridge.send_command("fail_test", {})
        finally:
            stop_evt.set()
            t.join(timeout=2.0)

        # Verify timeout cleans up stale inbox file when daemon is stopped
        with self.assertRaises(TimeoutError):
            self.bridge.send_command("ping", {}, timeout_s=0.25)
        self.assertEqual(list(self.bridge.inbox_dir.glob("*.json")), [])

    def test_parse_reaper_render_presets(self) -> None:
        res_dir = self.root / "reaper_res"
        res_dir.mkdir(parents=True, exist_ok=True)
        (res_dir / "reaper-render.ini").write_text(
            '<RENDERPRESET Wkly 44100 2 0 1 3 0 0\n  ZXZhdxgAAQ==\n>\n'
            '<RENDERPRESET Wkly 44100 2 0 1 3 0 0\n  ZXZhdxgAAQ==\n>\n'
            '<RENDERPRESET "Master 48k WAV" 48000 2 0 1 3 0 0\n  ZXZhdxgAAQ==\n>\n',
            encoding="utf-8",
        )
        presets = parse_reaper_render_presets(res_dir)
        self.assertEqual(presets, ["Wkly", "Master 48k WAV"])

    def test_reaper_installer_install_idempotent_and_uninstall(self) -> None:
        res_dir = self.root / "REAPER_RES"
        (res_dir / "Scripts").mkdir(parents=True, exist_ok=True)
        kb_ini = res_dir / "reaper-kb.ini"
        kb_ini.write_text('SCR 4 0 RS1234 "Custom: user_script.lua" user_script.lua\n', encoding="utf-8")
        startup_lua = res_dir / "Scripts" / "__startup.lua"
        startup_lua.write_text('reaper.ShowConsoleMsg("user startup\\n")\n', encoding="utf-8")

        fake_exe = self.root / "reaper.exe"
        fake_exe.write_bytes(b"MZ")

        inst = ReaperInstaller(self.hub)
        st1 = inst.install(resource_dir=res_dir, auto_start_bridge=True, executable_override=str(fake_exe))
        self.assertTrue(st1["all_scripts_installed"])
        self.assertTrue(st1["all_actions_registered"])
        self.assertTrue(st1["startup_hook_installed"])
        self.assertTrue((res_dir / "reaper-kb.ini.obol.bak").is_file())
        self.assertTrue((res_dir / "MenuSets" / "Obol_Toolbar.ReaperMenu").is_file())
        self.assertTrue((res_dir / "Scripts" / "Obol" / "obol_config.lua").is_file())
        self.assertTrue((res_dir / "Scripts" / "Obol" / "obol_lib.lua").is_file())
        self.assertEqual(self.hub.get_software_profile("reaper").executable, str(fake_exe))

        # Idempotent second install does not duplicate SCR lines or startup blocks
        st2 = inst.install(resource_dir=res_dir, auto_start_bridge=True)
        self.assertEqual(len(st2["actions_registered"]), len(OBOL_ACTION_SPECS))
        startup_txt = startup_lua.read_text(encoding="utf-8")
        self.assertEqual(startup_txt.count("-- BEGIN OBOL BRIDGE"), 1)
        self.assertIn("user startup", startup_txt)

        # Uninstall cleans only Obol lines and keeps user config intact
        st_un = inst.uninstall(resource_dir=res_dir)
        self.assertEqual(st_un["scripts_installed"], [])
        self.assertEqual(st_un["actions_registered"], [])
        self.assertFalse(st_un["startup_hook_installed"])
        self.assertFalse((res_dir / "Scripts" / "Obol").exists())
        self.assertIn("user_script.lua", kb_ini.read_text(encoding="utf-8"))
        self.assertIn("user startup", startup_lua.read_text(encoding="utf-8"))

    def test_reaper_cli_commands(self) -> None:
        proj = self.ws.init_project("cli_reaper", self.root / "cli_reaper")
        rpp_v1 = proj / "audio" / "working" / "v001" / "audio_v001.rpp"

        res_dir = self.root / "CLI_REAPER_RES"
        res_dir.mkdir(parents=True, exist_ok=True)
        self.assertEqual(run_cli(["reaper", "install", "--resource-dir", str(res_dir), "--json"], hub=self.hub), 0)
        self.assertEqual(run_cli(["reaper", "status", "--resource-dir", str(res_dir), "--json"], hub=self.hub), 0)
        self.assertEqual(run_cli(["reaper", "context", "--rpp", str(rpp_v1), "--json"], hub=self.hub), 0)
        self.assertEqual(
            run_cli(["reaper", "version-up", "--rpp", str(rpp_v1), "-m", "From REAPER", "--json"], hub=self.hub),
            0,
        )
        rpp_v2 = proj / "audio" / "working" / "v002" / "audio_v002.rpp"
        self.assertTrue(rpp_v2.is_file())

        self.assertEqual(run_cli(["reaper", "collect", "--rpp", str(rpp_v2), "--json"], hub=self.hub), 0)
        self.assertEqual(
            run_cli(["reaper", "prepare-publish", "--rpp", str(rpp_v2), "--name", "main", "--json"], hub=self.hub),
            0,
        )
        exp_dir = proj / "audio" / "published" / "main" / "v001"
        self.assertTrue(exp_dir.is_dir())
        (exp_dir / "mix_bounce.wav").write_bytes(b"RIFF-MIX")
        self.assertEqual(
            run_cli(
                [
                    "reaper",
                    "sync-publish",
                    "--rpp",
                    str(rpp_v2),
                    "--name",
                    "main",
                    "--version",
                    "1",
                    "-m",
                    "Master bounce",
                    "--tag",
                    "final",
                    "--json",
                ],
                hub=self.hub,
            ),
            0,
        )
        self.assertEqual(run_cli(["reaper", "uninstall", "--resource-dir", str(res_dir), "--json"], hub=self.hub), 0)

    def test_reaper_dialogs_and_drag_out_smoke(self) -> None:
        proj = self.ws.init_project("gui_reaper", self.root / "gui_reaper")
        rpp_v1 = proj / "audio" / "working" / "v001" / "audio_v001.rpp"
        sample = self.root / "lead.wav"
        sample.write_bytes(b"LEAD")
        self.ws.ingest_repo_asset(proj, "audio", sample, asset_path="audio/lead")
        pub_wav = self.root / "master.wav"
        pub_wav.write_bytes(b"MASTER")
        self.ws.publish_version(proj, "audio", [pub_wav], deliverable_name="main")

        d_open = ReaperOpenDialog(None, self.hub, self.ws, self.bridge, active_rpp=str(rpp_v1))
        self.assertGreater(d_open.ver_tree.topLevelItemCount(), 0)
        d_open.close()

        d_imp = ReaperImportDialog(None, self.hub, self.ws, self.bridge, active_rpp=str(rpp_v1))
        self.assertGreaterEqual(d_imp.asset_tree.topLevelItemCount(), 2)
        d_imp.close()

        d_pub = ReaperExportPublishDialog(None, self.hub, self.ws, self.bridge, active_rpp=str(rpp_v1))
        self.assertIsNotNone(d_pub._ctx)
        d_pub.close()

        d_setup = ReaperIntegrationDialog(None, self.hub, self.bridge)
        self.assertTrue(bool(d_setup.status_pill.text()))
        d_setup.close()

        win = ObolMainWindow(hub=self.hub, initial_project="gui_reaper", initial_component="audio")
        self.assertEqual(win.active_project_name, "gui_reaper")
        self.assertEqual(win.active_component_path, "audio")
        self.assertIn("REAPER:", win.reaper_status_pill.text())

        # Verify OS drag-out MIME data on working_list & published_version_tree
        w_item = win.working_list.topLevelItem(0)
        self.assertIsNotNone(w_item)
        assert w_item is not None
        mime = win.working_list.build_drag_mime_data([w_item])
        self.assertIsNotNone(mime)
        assert mime is not None
        self.assertEqual([Path(u.toLocalFile()).resolve() for u in mime.urls()], [rpp_v1.resolve()])

        # Verify internal self-drop guard ignores drops originating from within the same Obol window
        dropped: list[list[Path]] = []
        win.working_list.filesDropped.connect(lambda paths, _it: dropped.append(paths))
        internal_mime = QMimeData()
        internal_mime.setUrls([QUrl.fromLocalFile(str(sample))])
        ev = QDropEvent(
            QPoint(5, 5),
            Qt.DropAction.CopyAction,
            internal_mime,
            Qt.MouseButton.LeftButton,
            Qt.KeyboardModifier.NoModifier,
        )
        # Simulate drag from win.repo_version_list onto win.working_list
        orig_source = ev.source
        ev.source = lambda: win.repo_version_list  # type: ignore[method-assign]
        win.working_list.dropEvent(ev)
        self.assertEqual(dropped, [])
        del orig_source
        win.close()


if __name__ == "__main__":
    unittest.main()
