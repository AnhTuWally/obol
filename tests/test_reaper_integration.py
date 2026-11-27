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
from PySide6.QtWidgets import QApplication, QComboBox, QMessageBox, QTreeWidgetItem

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
from obol.plugins.base import TaskContext, unlock_file_writable
from obol.plugins.reaper import ReaperPlugin
from obol.plugins.reaper_bridge import ReaperBridgeClient, parse_reaper_render_presets
from obol.plugins.reaper_installer import ReaperInstaller
from obol.plugins.reaper_scripts import (
    OBOL_ACTION_SPECS,
    OBOL_INTEGRATION_VERSION,
    render_increment_save_action_lua,
    render_obol_lib_lua,
    render_obol_toolbar_menu,
    render_version_up_action_lua,
)


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
        self.assertFalse(hasattr(d_open, "vup_open_btn"))
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
        self.assertFalse(hasattr(win, "vup_launch_btn"))

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

    def test_session_item_resolution_midi_and_media_updates_end_to_end(self) -> None:
        # Verify generated Lua script capabilities & version
        self.assertEqual(OBOL_INTEGRATION_VERSION, "1.4")
        action_names = {name for _, name, _ in OBOL_ACTION_SPECS}
        self.assertIn("Obol - Update Session Media", action_names)
        self.assertIn("Obol - Increment Save", action_names)
        lib_lua = render_obol_lib_lua()
        for token in (
            "PCM_Source_CreateFromFileEx",
            "40684",
            "AddTakeToMediaItem",
            "take_updates",
            "P_EXT:OBOL_ASSET",
            "exec_query_session_media",
        ):
            self.assertIn(token, lib_lua)

        proj = self.ws.init_project("midi_media_proj", self.root / "midi_media_proj")
        rpp_v1 = proj / "audio" / "working" / "v001" / "audio_v001.rpp"

        wav1 = self.root / "bass1.wav"
        wav2 = self.root / "bass2.wav"
        wav1.write_bytes(b"BASS-1")
        wav2.write_bytes(b"BASS-2")
        _, w_b1 = self.ws.ingest_repo_asset(proj, "audio", wav1, asset_path="audio/bass")
        _, w_b2 = self.ws.ingest_repo_asset(proj, "audio", wav2, asset_path="audio/bass")

        mid1 = self.root / "chord_prog1.mid"
        mid2 = self.root / "chord_prog2.mid"
        mid1.write_bytes(b"MThd-V1")
        mid2.write_bytes(b"MThd-V2")
        _, m_b1 = self.ws.ingest_repo_asset(proj, "audio", mid1, asset_path="midi/chord_prog")
        _, m_b2 = self.ws.ingest_repo_asset(proj, "audio", mid2, asset_path="midi/chord_prog")

        lead_mid1 = self.root / "arp_lead_v001.mid"
        lead_mid2 = self.root / "arp_lead_v002.mid"
        lead_mid1.write_bytes(b"MThd-L1")
        lead_mid2.write_bytes(b"MThd-L2")
        _, l_b1 = self.ws.ingest_repo_asset(proj, "audio", lead_mid1, asset_path="midi/arp_lead")
        _, l_b2 = self.ws.ingest_repo_asset(proj, "audio", lead_mid2, asset_path="midi/arp_lead")

        wav_v1_path = (proj / "audio" / w_b1.primary_file).resolve()
        wav_v2_path = (proj / "audio" / w_b2.primary_file).resolve()
        mid_v1_path = (proj / "audio" / m_b1.primary_file).resolve()
        mid_v2_path = (proj / "audio" / m_b2.primary_file).resolve()
        lead_v1_path = (proj / "audio" / l_b1.primary_file).resolve()
        lead_v2_path = (proj / "audio" / l_b2.primary_file).resolve()

        # 1. Test 3-tier resolve_reaper_session_item + available_versions
        ext_meta = self.ws.describe_asset_file(proj, mid_v1_path)
        self.assertIsNotNone(ext_meta)
        assert ext_meta is not None
        self.assertEqual(ext_meta["media_kind"], "midi")

        res_tier1 = self.ws.resolve_reaper_session_item(
            proj,
            {
                "item_idx": 0,
                "take_idx": 0,
                "item_guid": "{ITEM-WAV}",
                "take_guid": "{TAKE-WAV}",
                "track_name": "Bass",
                "take_name": wav_v1_path.name,
                "file_path": str(wav_v1_path),
                "is_midi": False,
            },
        )
        self.assertIsNotNone(res_tier1)
        assert res_tier1 is not None
        self.assertEqual(res_tier1["matched_by"], "file_path")
        self.assertTrue(res_tier1["update_available"])
        self.assertEqual([v["version"] for v in res_tier1["available_versions"]], [1, 2])

        res_tier2 = self.ws.resolve_reaper_session_item(
            proj,
            {
                "item_idx": 1,
                "take_idx": 0,
                "item_guid": "{ITEM-MID-EXT}",
                "take_guid": "{TAKE-MID-EXT}",
                "track_name": "Keys",
                "take_name": "My Custom Keys",
                "file_path": "",
                "is_midi": True,
                "midi_storage": "in_project",
                "obol_ext": json.dumps(ext_meta),
            },
        )
        self.assertIsNotNone(res_tier2)
        assert res_tier2 is not None
        self.assertEqual(res_tier2["matched_by"], "obol_ext")
        self.assertTrue(res_tier2["is_midi"])
        self.assertEqual(res_tier2["midi_storage"], "in_project")
        self.assertEqual(res_tier2["latest_file"], mid_v2_path)

        res_tier3 = self.ws.resolve_reaper_session_item(
            proj,
            {
                "item_idx": 2,
                "take_idx": 0,
                "item_guid": "{ITEM-MID-NAME}",
                "take_guid": "{TAKE-MID-NAME}",
                "track_name": "Arp",
                "take_name": f"{lead_v1_path.stem} - Track 1",
                "file_path": "",
                "is_midi": True,
                "midi_storage": "in_project",
                "obol_ext": "",
            },
        )
        self.assertIsNotNone(res_tier3)
        assert res_tier3 is not None
        self.assertEqual(res_tier3["matched_by"], "take_name")
        self.assertEqual(res_tier3["latest_file"], lead_v2_path)

        # 2. Live mock daemon covering ReaperImportDialog, ObolMainWindow, and CLI update-media
        self.bridge.state_path.write_text(
            json.dumps({
                "connected": True,
                "heartbeat": time.time(),
                "reaper_version": "7.30/win64",
                "active_rpp": str(rpp_v1),
                "dirty": False,
                "selected_tracks": 1,
            }),
            encoding="utf-8",
        )

        session_state = [
            {
                "item_idx": 0,
                "take_idx": 0,
                "item_guid": "{ITEM-WAV}",
                "take_guid": "{TAKE-WAV}",
                "track_name": "Bass",
                "take_name": wav_v1_path.name,
                "file_path": str(wav_v1_path),
                "is_midi": False,
                "midi_storage": "",
                "obol_ext": json.dumps(self.ws.describe_asset_file(proj, wav_v1_path)),
            },
            {
                "item_idx": 1,
                "take_idx": 0,
                "item_guid": "{ITEM-MID-EXT}",
                "take_guid": "{TAKE-MID-EXT}",
                "track_name": "Keys",
                "take_name": "Chords",
                "file_path": "",
                "is_midi": True,
                "midi_storage": "in_project",
                "obol_ext": json.dumps(ext_meta),
            },
            {
                "item_idx": 2,
                "take_idx": 0,
                "item_guid": "{ITEM-MID-NAME}",
                "take_guid": "{TAKE-MID-NAME}",
                "track_name": "Arp",
                "take_name": f"{lead_v1_path.stem} - Track 1",
                "file_path": "",
                "is_midi": True,
                "midi_storage": "in_project",
                "obol_ext": "",
            },
        ]
        captured_commands: list[tuple[str, dict]] = []
        stop_evt = threading.Event()

        def _mock_daemon() -> None:
            while not stop_evt.is_set():
                for f in sorted(self.bridge.inbox_dir.glob("*.json")):
                    req = json.loads(f.read_text(encoding="utf-8"))
                    f.unlink(missing_ok=True)
                    action = req["action"]
                    params = req.get("params") or {}
                    captured_commands.append((action, params))
                    if action == "query_session_media":
                        res = {"items": session_state}
                    elif action == "insert_media":
                        res = {"inserted": len(params.get("file_paths") or []), "mode": params.get("mode", "new_track")}
                    elif action == "update_media_sources":
                        upds = params.get("take_updates") or []
                        upd_by_tg = {u.get("take_guid"): u for u in upds if u.get("take_guid")}
                        midi_cnt = 0
                        media_cnt = 0
                        for sit in session_state:
                            u = upd_by_tg.get(sit["take_guid"])
                            if u:
                                targ = Path(u["target_path"])
                                if u.get("asset_meta"):
                                    sit["obol_ext"] = json.dumps(u["asset_meta"])
                                if sit["is_midi"]:
                                    midi_cnt += 1
                                    if params.get("midi_mode") == "file_ref":
                                        sit["file_path"] = str(targ)
                                        sit["midi_storage"] = "file_ref"
                                else:
                                    media_cnt += 1
                                    sit["file_path"] = str(targ)
                        res = {
                            "updated_takes": len(upds),
                            "updated_items": len(upds),
                            "updated_midi_takes": midi_cnt,
                            "updated_media_takes": media_cnt,
                            "update_mode": params.get("update_mode", "replace_source"),
                        }
                    else:
                        res = {"ok": True}
                    out_f = self.bridge.outbox_dir / f"{req['id']}.json"
                    out_f.write_text(json.dumps({"id": req["id"], "ok": True, "result": res}), encoding="utf-8")
                time.sleep(0.02)

        orig_info = QMessageBox.information
        orig_warn = QMessageBox.warning
        QMessageBox.information = staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Ok)  # type: ignore[assignment]
        QMessageBox.warning = staticmethod(lambda *a, **kw: QMessageBox.StandardButton.Ok)  # type: ignore[assignment]

        t = threading.Thread(target=_mock_daemon, daemon=True)
        t.start()
        try:
            # Verify bridge insert_media forwards per-file asset_metas
            ins_res = self.bridge.insert_media(
                [wav_v1_path, mid_v1_path],
                mode="new_track",
                asset_metas=[self.ws.describe_asset_file(proj, wav_v1_path) or {}, ext_meta],
            )
            self.assertEqual(ins_res["inserted"], 2)
            self.assertEqual(len(captured_commands[-1][1]["asset_metas"]), 2)

            # Verify CLI dry-run check
            self.assertEqual(run_cli(["reaper", "update-media", "--rpp", str(rpp_v1), "--check", "--json"], hub=self.hub), 0)

            # Verify ReaperImportDialog(initial_tab=1) lists all 3 items & applies updates
            dlg = ReaperImportDialog(None, self.hub, self.ws, self.bridge, active_rpp=str(rpp_v1), initial_tab=1)
            self.assertEqual(dlg.tabs.currentIndex(), 1)
            self.assertEqual(dlg.updates_table.rowCount(), 3)
            combo0 = dlg.updates_table.cellWidget(0, 5)
            self.assertIsInstance(combo0, QComboBox)
            assert isinstance(combo0, QComboBox)
            self.assertEqual(combo0.count(), 2)
            dlg.update_mode_combo.setCurrentIndex(1)  # add_take
            dlg.midi_mode_combo.setCurrentIndex(1)  # in_project
            dlg._on_apply_updates()
            upd_cmds = [p for a, p in captured_commands if a == "update_media_sources"]
            self.assertEqual(len(upd_cmds), 1)
            last_cmd_params = upd_cmds[-1]
            self.assertEqual(last_cmd_params["update_mode"], "add_take")
            self.assertEqual(last_cmd_params["midi_mode"], "in_project")
            self.assertEqual(last_cmd_params["match_length"], "auto")
            self.assertEqual(len(last_cmd_params["take_updates"]), 3)
            dlg.close()

            # Reset session_state back to v1 and test ObolMainWindow direct update + CLI live update
            session_state[0]["file_path"] = str(wav_v1_path)
            session_state[1]["obol_ext"] = json.dumps(ext_meta)
            win = ObolMainWindow(hub=self.hub, initial_project="midi_media_proj", initial_component="audio")
            self.assertFalse(win.reaper_update_media_btn.isHidden())
            upd_res = win._action_update_asset_in_reaper(mid_v2_path, silent=True)
            self.assertEqual(upd_res["updated_takes"], 1)
            self.assertEqual(upd_res["updated_midi_takes"], 1)
            win.close()

            # CLI live update for remaining outdated takes
            self.assertEqual(run_cli(["reaper", "update-media", "--rpp", str(rpp_v1), "--json"], hub=self.hub), 0)
        finally:
            QMessageBox.information = orig_info  # type: ignore[assignment]
            QMessageBox.warning = orig_warn  # type: ignore[assignment]
            stop_evt.set()
            t.join(timeout=2.0)

        # Also test CLI --offline mode on .rpp file
        unlock_file_writable(rpp_v1)
        rpp_v1.write_text(
            f'<REAPER_PROJECT 0.1 "7.0"\n  <TRACK\n    <ITEM\n      <SOURCE MIDI\n        FILE "../../{m_b1.primary_file}"\n      >\n    >\n  >\n>\n',
            encoding="utf-8",
        )
        self.assertEqual(
            run_cli(["reaper", "update-media", "--offline", "--rpp", str(rpp_v1), "--json"], hub=self.hub),
            0,
        )
        self.assertIn(f"../../{m_b2.primary_file}", rpp_v1.read_text(encoding="utf-8"))

    def test_reaper_increment_save_workspace_bridge_gui_and_cli(self) -> None:
        lib_lua = render_obol_lib_lua()
        for token in ("Main_SaveProjectEx(0, path, 8)", "exec_save_project_as", "save_project_as", "switched_in_place"):
            self.assertIn(token, lib_lua)
        inc_lua = render_increment_save_action_lua()
        vup_lua = render_version_up_action_lua()
        self.assertIn("Main_SaveProjectEx(0, pdata.primary_file, 8)", inc_lua)
        self.assertIn("Main_SaveProjectEx(0, pdata.primary_file, 8)", vup_lua)
        self.assertIn("Main_openProject", inc_lua)
        self.assertIn("reaper_version = reaper.GetAppVersion()", lib_lua)
        toolbar = render_obol_toolbar_menu({fname: f"CMD_{i}" for i, (fname, _, _) in enumerate(OBOL_ACTION_SPECS)})
        self.assertIn("Obol: IncSave", toolbar)

        reaper_actions = ReaperPlugin().get_inspector_actions(
            TaskContext(
                project_root=self.root,
                component_path="audio",
                component_dir=self.root / "audio",
                software_profile=self.hub.get_software_profile("reaper"),
            )
        )
        self.assertNotIn("version_up_launch", [a.id for a in reaper_actions])

        proj = self.ws.init_project("inc_save_proj", self.root / "inc_save_proj")
        rpp_v1 = proj / "audio" / "working" / "v001" / "audio_v001.rpp"
        self.assertTrue(rpp_v1.is_file())
        v1_orig = rpp_v1.read_text(encoding="utf-8")

        # 1. Workspace prepare_next_working_version + simulated save + register_saved_working_version
        prof = self.hub.get_software_profile("reaper")
        prof.lock_previous_versions = True
        self.hub.set_software_profile(prof)

        prep_w = self.ws.prepare_next_working_version(proj, "audio", source_version=1, extension=".rpp")
        self.assertEqual(prep_w["version"], 2)
        target_v2 = Path(prep_w["primary_file"])
        target_v2.write_text(v1_orig + "; v2 unsaved edits\n", encoding="utf-8")
        reg_v2 = self.ws.register_saved_working_version(
            proj,
            "audio",
            version=2,
            primary_file=target_v2,
            parent_version=1,
            comment="Manual prepare+register",
        )
        self.assertEqual(reg_v2.version, 2)
        self.assertEqual(rpp_v1.read_text(encoding="utf-8"), v1_orig)
        self.assertFalse(os.access(rpp_v1, os.W_OK))

        # Reset to clean project for live mock REAPER daemon test (GUI + CLI)
        proj2 = self.ws.init_project("inc_live_proj", self.root / "inc_live_proj")
        live_v1 = proj2 / "audio" / "working" / "v001" / "audio_v001.rpp"
        live_v1_orig = live_v1.read_text(encoding="utf-8")
        session_state = {
            "active_rpp": str(live_v1),
            "reaper_version": "7.34/win64",
            "switched_in_place": True,
            "reloaded": False,
        }
        seen_actions: list[tuple[str, dict]] = []

        def _write_state() -> None:
            self.bridge.state_path.write_text(
                json.dumps({
                    "connected": True,
                    "heartbeat": time.time(),
                    "reaper_version": session_state["reaper_version"],
                    "active_rpp": session_state["active_rpp"],
                    "dirty": False,
                    "selected_tracks": 1,
                }),
                encoding="utf-8",
            )

        _write_state()
        stop_evt = threading.Event()

        def _mock_daemon() -> None:
            while not stop_evt.is_set():
                for f in sorted(self.bridge.inbox_dir.glob("*.json")):
                    req = json.loads(f.read_text(encoding="utf-8"))
                    f.unlink(missing_ok=True)
                    action = req["action"]
                    params = req.get("params") or {}
                    seen_actions.append((action, params))
                    if action == "save_project_as":
                        out_p = Path(params["path"])
                        out_p.parent.mkdir(parents=True, exist_ok=True)
                        out_p.write_text(
                            live_v1_orig.replace("</REAPER_PROJECT>", "") + "  ; increment saved live\n",
                            encoding="utf-8",
                        )
                        session_state["active_rpp"] = str(out_p)
                        _write_state()
                        res = {
                            "saved": True,
                            "path": str(out_p),
                            "active_rpp": str(out_p),
                            "switched_in_place": session_state["switched_in_place"],
                            "reloaded": session_state["reloaded"],
                            "dirty": False,
                            "reaper_version": session_state["reaper_version"],
                        }
                    else:
                        res = {"ok": True}
                    out_f = self.bridge.outbox_dir / f"{req['id']}.json"
                    out_f.write_text(json.dumps({"id": req["id"], "ok": True, "result": res}), encoding="utf-8")
                time.sleep(0.02)

        t = threading.Thread(target=_mock_daemon, daemon=True)
        t.start()
        try:
            # 2. GUI ObolMainWindow._action_reaper_increment_save -> v002 (keeps v002 open & active, no open_project reload)
            win = ObolMainWindow(hub=self.hub, initial_project="inc_live_proj", initial_component="audio")
            self.assertEqual(win.reaper_inc_save_btn.text(), "Increment Save (→ v002)")
            bundle_v2 = win._action_reaper_increment_save(silent=True)
            self.assertIsNotNone(bundle_v2)
            assert bundle_v2 is not None
            self.assertEqual(bundle_v2.version, 2)
            live_v2 = proj2 / "audio" / "working" / "v002" / "audio_v002.rpp"
            self.assertEqual(Path(session_state["active_rpp"]).resolve(), live_v2.resolve())
            self.assertTrue(seen_actions[-1][1].get("ensure_open"))
            self.assertEqual(win.working_list.topLevelItemCount(), 2)
            self.assertEqual(win.reaper_inc_save_btn.text(), "Increment Save (→ v003)")
            self.assertEqual(live_v1.read_text(encoding="utf-8"), live_v1_orig)
            self.assertNotIn("open_project", [a for a, _ in seen_actions])
            win.close()

            # 3. CLI obol reaper increment-save -> v003 (continues from open v002, keeps v003 open)
            self.assertEqual(
                run_cli(["reaper", "increment-save", "-m", "CLI inc save", "--json"], hub=self.hub),
                0,
            )
            live_v3 = proj2 / "audio" / "working" / "v003" / "audio_v003.rpp"
            self.assertTrue(live_v3.is_file())
            self.assertEqual(Path(session_state["active_rpp"]).resolve(), live_v3.resolve())
            self.assertTrue(seen_actions[-1][1].get("ensure_open"))
            self.assertNotIn("open_project", [a for a, _ in seen_actions])

            # 3b. Simulated REAPER 7.28 reload fallback reports REAPER version & 7.34+ upgrade hint
            session_state["reaper_version"] = "7.28/x64"
            session_state["switched_in_place"] = False
            session_state["reloaded"] = True
            _write_state()
            win_fallback = ObolMainWindow(hub=self.hub, initial_project="inc_live_proj", initial_component="audio")
            bundle_v4 = win_fallback._action_reaper_increment_save(silent=True)
            self.assertIsNotNone(bundle_v4)
            assert bundle_v4 is not None
            self.assertEqual(bundle_v4.version, 4)
            status_msg = win_fallback.status_feedback_label.text()
            self.assertIn("7.28/x64", status_msg)
            self.assertIn("7.34+", status_msg)
            win_fallback.close()
        finally:
            stop_evt.set()
            t.join(timeout=2.0)

        # 4. CLI prepare-working + file write + register-working -> v005
        live_v4 = proj2 / "audio" / "working" / "v004" / "audio_v004.rpp"
        self.assertEqual(
            run_cli(["reaper", "prepare-working", "--rpp", str(live_v4), "--json"], hub=self.hub),
            0,
        )
        live_v5 = proj2 / "audio" / "working" / "v005" / "audio_v005.rpp"
        live_v5.write_text(live_v1_orig, encoding="utf-8")
        self.assertEqual(
            run_cli(
                [
                    "reaper",
                    "register-working",
                    "--rpp",
                    str(live_v5),
                    "--parent-version",
                    "4",
                    "-m",
                    "Registered v5",
                    "--json",
                ],
                hub=self.hub,
            ),
            0,
        )
        snap = self.ws.load_component_snapshot_safe(proj2, "audio") if hasattr(self.ws, "load_component_snapshot_safe") else None
        from obol.core.storage import load_component_snapshot

        snap_loaded = load_component_snapshot(proj2, "audio")
        self.assertEqual([w.version for w in snap_loaded.working_versions], [1, 2, 3, 4, 5])
        del snap


if __name__ == "__main__":
    unittest.main()
