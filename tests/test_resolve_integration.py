"""Automated unit and headless Qt integration tests for DaVinci Resolve integration."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from obol.cli.main import run_cli
from obol.core.hub import ObolHub
from obol.core.models import default_software_profiles
from obol.core.workspace import ObolWorkspace
from obol.gui.app import ObolMainWindow
from obol.gui.resolve_dialogs import (
    ResolveExportPublishDialog,
    ResolveImportDialog,
    ResolveIntegrationDialog,
)
from obol.plugins.resolve_bridge import ResolveBridgeClient
from obol.plugins.resolve_installer import ResolveInstaller
from obol.plugins.resolve_scripts import RESOLVE_UTILITY_SCRIPTS


class _MockClip:
    def __init__(self, name: str, file_path: str, media_id: str = "m1") -> None:
        self._name = name
        self._file_path = file_path
        self._media_id = media_id
        self.meta: dict[str, str] = {}

    def GetName(self) -> str:
        return self._name

    def GetClipProperty(self, key: str) -> str:
        if key == "File Path":
            return self._file_path
        return ""

    def GetMediaId(self) -> str:
        return self._media_id

    def SetThirdPartyMetadata(self, data: dict[str, str]) -> bool:
        self.meta.update(data)
        return True

    def ReplaceClipPreserveSubClip(self, new_path: str) -> bool:
        self._file_path = new_path
        return True


class _MockFolder:
    def __init__(self, name: str) -> None:
        self._name = name
        self._clips: list[_MockClip] = []
        self._subs: list[_MockFolder] = []

    def GetName(self) -> str:
        return self._name

    def GetClipList(self) -> list[_MockClip]:
        return list(self._clips)

    def GetSubFolderList(self) -> list[_MockFolder]:
        return list(self._subs)


class _MockMediaPool:
    def __init__(self) -> None:
        self.root = _MockFolder("Master")
        self.current = self.root
        self.appended: list[_MockClip] = []

    def GetRootFolder(self) -> _MockFolder:
        return self.root

    def GetCurrentFolder(self) -> _MockFolder:
        return self.current

    def SetCurrentFolder(self, folder: _MockFolder) -> bool:
        self.current = folder
        return True

    def AddSubFolder(self, parent: _MockFolder, name: str) -> _MockFolder:
        sub = _MockFolder(name)
        parent._subs.append(sub)
        return sub

    def ImportMedia(self, paths: list[str]) -> list[_MockClip]:
        imported: list[_MockClip] = []
        for idx, p in enumerate(paths):
            c = _MockClip(Path(p).name, p, media_id=f"clip_{idx}")
            self.current._clips.append(c)
            imported.append(c)
        return imported

    def AppendToTimeline(self, clips: list[_MockClip]) -> list[str]:
        self.appended.extend(clips)
        return ["tl_item"]


class _MockTimeline:
    def GetName(self) -> str:
        return "Edit_v001"


class _MockProject:
    def __init__(self) -> None:
        self.media_pool = _MockMediaPool()
        self.timeline = _MockTimeline()
        self.render_settings: dict[str, Any] = {}
        self.loaded_preset = ""
        self.jobs: list[str] = []

    def GetName(self) -> str:
        return "MusicVideo_Resolve"

    def GetMediaPool(self) -> _MockMediaPool:
        return self.media_pool

    def GetCurrentTimeline(self) -> _MockTimeline:
        return self.timeline

    def GetRenderPresetList(self) -> list[str]:
        return ["H.264 Master", "ProRes 422 HQ"]

    def GetQuickExportRenderPresets(self) -> list[str]:
        return ["H.264 Master", "YouTube 1080p"]

    def LoadRenderPreset(self, name: str) -> bool:
        self.loaded_preset = name
        return True

    def SetRenderSettings(self, settings: dict[str, Any]) -> bool:
        self.render_settings.update(settings)
        return True

    def AddRenderJob(self) -> str:
        jid = f"job_{len(self.jobs) + 1}"
        self.jobs.append(jid)
        return jid

    def StartRendering(self, job_id: str) -> bool:
        out_dir = Path(self.render_settings["TargetDir"])
        cname = self.render_settings.get("CustomName") or "render"
        (out_dir / f"{cname}.mp4").write_bytes(b"MP4DATA")
        return True

    def IsRenderingInProgress(self) -> bool:
        return False

    def GetRenderJobStatus(self, job_id: str) -> dict[str, Any]:
        return {"JobStatus": "Complete", "CompletionPercentage": 100}

    def RenderWithQuickExport(self, preset: str, params: dict[str, Any]) -> dict[str, Any]:
        out_dir = Path(params["TargetDir"])
        cname = params.get("CustomName") or "quick"
        (out_dir / f"{cname}.mov").write_bytes(b"MOVDATA")
        return {"status": "Complete"}

    def ExportCurrentFrameAsStill(self, file_path: str) -> bool:
        Path(file_path).write_bytes(b"PNGDATA")
        return True


class _MockProjectManager:
    def __init__(self, project: _MockProject) -> None:
        self.project = project
        self.exported_drp = ""

    def GetCurrentProject(self) -> _MockProject:
        return self.project

    def ExportProject(self, proj_name: str, file_path: str, with_stills: bool = True) -> bool:
        self.exported_drp = file_path
        Path(file_path).write_bytes(b"DRPDATA")
        return True


class _MockResolveApp:
    def __init__(self) -> None:
        self.project = _MockProject()
        self.pm = _MockProjectManager(self.project)
        self.opened_page = "edit"

    def GetProductName(self) -> str:
        return "DaVinci Resolve Studio"

    def GetVersionString(self) -> str:
        return "19.1.0"

    def GetCurrentPage(self) -> str:
        return self.opened_page

    def OpenPage(self, page: str) -> bool:
        self.opened_page = page
        return True

    def GetProjectManager(self) -> _MockProjectManager:
        return self.pm


class TestResolveIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)
        self.bridge = ResolveBridgeClient(self.hub, allow_studio_api=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_resolve_profile_template_and_plugin(self) -> None:
        profiles = default_software_profiles()
        self.assertIn("resolve", profiles)
        self.assertEqual(profiles["resolve"].plugin, "resolve")
        self.assertIn(".drp", profiles["resolve"].extensions)

        tm = self.hub.template_manager
        task_tpls = tm.list_task_templates()
        self.assertIn("resolve_video", task_tpls)
        self.assertEqual(task_tpls["resolve_video"].software, "resolve")

        plug = self.hub.plugin_registry.get("resolve")
        self.assertEqual(plug.name, "resolve")
        proj = self.ws.init_project("dvr_proj", self.root / "dvr_proj")
        snap = self.ws.create_component(proj, "edit", task_template="resolve_video")
        self.assertEqual(snap.software, "resolve")
        self.assertTrue((proj / "edit" / "working" / "v001" / "edit_v001.drp").is_file())

    def test_resolve_bridge_file_ipc_and_bindings(self) -> None:
        self.bridge.set_project_binding("ResolveProjA", "mv_proj", "video/edit")
        self.assertEqual(
            self.bridge.get_project_binding("ResolveProjA"),
            {"project_name": "mv_proj", "component_path": "video/edit"},
        )

        self.assertFalse(self.bridge.is_connected())
        self.bridge.state_path.write_text(
            json.dumps({
                "connected": True,
                "heartbeat": time.time(),
                "product_name": "DaVinci Resolve",
                "version": "19.1",
                "project_name": "ResolveProjA",
                "timeline_name": "MainCut",
                "current_page": "edit",
                "current_bin": "Master",
                "render_presets": ["H.264 Master"],
                "quick_export_presets": ["ProRes 422"],
            }),
            encoding="utf-8",
        )
        st = self.bridge.get_state()
        self.assertTrue(st.connected)
        self.assertEqual(st.mode, "lua_bridge")
        self.assertEqual(st.project_name, "ResolveProjA")

        stop_evt = threading.Event()

        def _mock_lua_daemon() -> None:
            while not stop_evt.is_set():
                for f in sorted(self.bridge.inbox_dir.glob("*.json")):
                    req = json.loads(f.read_text(encoding="utf-8"))
                    f.unlink(missing_ok=True)
                    act = req["action"]
                    if act == "query_media_pool":
                        resp = {
                            "id": req["id"],
                            "ok": True,
                            "result": {"items": [{"name": "a.wav", "file_path": "D:/a.wav", "bin_path": "Master"}]},
                        }
                    else:
                        resp = {"id": req["id"], "ok": True, "result": {"echo": act, "params": req["params"]}}
                    (self.bridge.outbox_dir / f"{req['id']}.json").write_text(json.dumps(resp), encoding="utf-8")
                time.sleep(0.02)

        t = threading.Thread(target=_mock_lua_daemon, daemon=True)
        t.start()
        try:
            clip = self.root / "clip.mov"
            clip.write_bytes(b"MOV")
            r_imp = self.bridge.import_media([clip], bin_path="Obol/edit/repo", append_to_timeline=True)
            self.assertEqual(r_imp["params"]["bin_path"], "Obol/edit/repo")
            self.assertTrue(r_imp["params"]["append_to_timeline"])

            items = self.bridge.query_media_pool()
            self.assertEqual(len(items), 1)

            r_rep = self.bridge.replace_media_pool_clips({str(clip): str(clip)})
            self.assertEqual(r_rep["echo"], "replace_media_pool_clips")

            r_ren = self.bridge.render_deliver_job(self.root / "pub", custom_name="main_v001", preset_name="H.264 Master")
            self.assertEqual(r_ren["params"]["preset_name"], "H.264 Master")

            r_qe = self.bridge.render_quick_export(self.root / "pub", preset_name="ProRes 422", custom_name="qe_v001")
            self.assertEqual(r_qe["echo"], "render_quick_export")

            r_st = self.bridge.stage_deliver_page(self.root / "pub", custom_name="stage_v001", add_job=True)
            self.assertTrue(r_st["params"]["add_job"])

            r_still = self.bridge.export_current_still(self.root / "pub" / "still.png")
            self.assertEqual(r_still["echo"], "export_current_still")

            r_drp = self.bridge.export_project_drp(self.root / "snap.drp")
            self.assertEqual(r_drp["echo"], "export_project_drp")
        finally:
            stop_evt.set()
            t.join(timeout=2.0)

        self.bridge.stop_lua_bridge()
        self.assertTrue(self.bridge.stop_flag_path.is_file())

        with self.assertRaises(TimeoutError):
            self.bridge.send_command("query_media_pool", {}, timeout_s=0.25)
        self.assertEqual(list(self.bridge.inbox_dir.glob("*.json")), [])

    def test_resolve_bridge_direct_studio_mock(self) -> None:
        mock_res = _MockResolveApp()
        studio_bridge = ResolveBridgeClient(self.hub, allow_studio_api=False, studio_resolve_obj=mock_res)

        st = studio_bridge.get_state()
        self.assertTrue(st.connected)
        self.assertEqual(st.mode, "studio_api")
        self.assertEqual(st.project_name, "MusicVideo_Resolve")
        self.assertEqual(st.render_presets, ["H.264 Master", "ProRes 422 HQ"])

        v1_clip = self.root / "audio_v001.wav"
        v2_clip = self.root / "audio_v002.wav"
        v1_clip.write_bytes(b"WAV1")
        v2_clip.write_bytes(b"WAV2")

        res_imp = studio_bridge.import_media(
            [v1_clip],
            bin_path="Obol/audio/published",
            append_to_timeline=True,
            asset_meta={"component": "audio"},
        )
        self.assertEqual(res_imp["imported"], 1)
        self.assertEqual(res_imp["bin"], "Obol/audio/published")
        self.assertEqual(len(mock_res.project.media_pool.appended), 1)

        pool_items = studio_bridge.query_media_pool()
        self.assertEqual(len(pool_items), 1)
        self.assertEqual(pool_items[0]["bin_path"], "Master/Obol/audio/published")

        res_rep = studio_bridge.replace_media_pool_clips({str(v1_clip): str(v2_clip)})
        self.assertEqual(res_rep["replaced"], 1)

        out_dir = self.root / "deliver_out"
        r_del = studio_bridge.render_deliver_job(out_dir, custom_name="main_v001", preset_name="ProRes 422 HQ")
        self.assertEqual(len(r_del["rendered_files"]), 1)
        self.assertTrue((out_dir / "main_v001.mp4").is_file())

        r_qe = studio_bridge.render_quick_export(out_dir, preset_name="H.264 Master", custom_name="quick_v001")
        self.assertEqual(len(r_qe["rendered_files"]), 1)

        r_stage = studio_bridge.stage_deliver_page(out_dir, custom_name="staged_v001", add_job=True)
        self.assertEqual(mock_res.opened_page, "deliver")
        self.assertTrue(r_stage["job_id"])

        still_file = out_dir / "frame.png"
        studio_bridge.export_current_still(still_file)
        self.assertTrue(still_file.is_file())

        drp_file = self.root / "proj.drp"
        studio_bridge.export_project_drp(drp_file)
        self.assertTrue(drp_file.is_file())

    def test_resolve_installer_install_idempotent_and_uninstall(self) -> None:
        scripts_dir = self.root / "ResolveScripts"
        (scripts_dir / "Utility").mkdir(parents=True, exist_ok=True)
        user_script = scripts_dir / "Utility" / "MyCustomTool.lua"
        user_script.write_text("-- keep me\n", encoding="utf-8")

        fake_exe = self.root / "Resolve.exe"
        fake_exe.write_bytes(b"MZ")

        inst = ResolveInstaller(self.hub)
        st1 = inst.install(scripts_dir=scripts_dir, executable_override=str(fake_exe), allow_studio_api=False)
        self.assertTrue(st1["all_scripts_installed"])
        self.assertEqual(len(st1["scripts_installed"]), len(RESOLVE_UTILITY_SCRIPTS))
        self.assertTrue(st1["deliver_script_installed"])
        self.assertTrue((scripts_dir / "Utility" / "Obol" / "obol_resolve_config.lua").is_file())
        self.assertTrue((scripts_dir / "Utility" / "Obol" / "obol_resolve_lib.lua").is_file())
        self.assertEqual(self.hub.get_software_profile("resolve").executable, str(fake_exe))

        # Idempotent re-install
        st2 = inst.install(scripts_dir=scripts_dir, allow_studio_api=False)
        self.assertTrue(st2["all_scripts_installed"])

        # Uninstall leaves unrelated user scripts untouched
        st_un = inst.uninstall(scripts_dir=scripts_dir, allow_studio_api=False)
        self.assertEqual(st_un["scripts_installed"], [])
        self.assertFalse(st_un["deliver_script_installed"])
        self.assertFalse((scripts_dir / "Utility" / "Obol").exists())
        self.assertTrue(user_script.is_file())

    def test_resolve_cli_commands(self) -> None:
        proj = self.ws.init_project("cli_dvr", self.root / "cli_dvr")
        self.ws.create_component(proj, "video/grade", task_template="resolve_video")

        scripts_dir = self.root / "CLI_ResolveScripts"
        self.assertEqual(
            run_cli(["resolve", "install", "--scripts-dir", str(scripts_dir), "--no-studio-api", "--json"], hub=self.hub),
            0,
        )
        self.assertEqual(
            run_cli(["resolve", "status", "--scripts-dir", str(scripts_dir), "--no-studio-api", "--json"], hub=self.hub),
            0,
        )
        self.assertEqual(
            run_cli(
                ["resolve", "bind", "--resolve-project", "CLI_Resolve", "-p", "cli_dvr", "-t", "video/grade", "--json"],
                hub=self.hub,
            ),
            0,
        )
        self.assertEqual(
            run_cli(
                ["resolve", "prepare-publish", "-p", "cli_dvr", "-t", "video/grade", "--name", "main", "--json"],
                hub=self.hub,
            ),
            0,
        )
        exp_dir = proj / "video" / "grade" / "published" / "main" / "v001"
        self.assertTrue(exp_dir.is_dir())
        (exp_dir / "graded_master.mp4").write_bytes(b"MP4")
        self.assertEqual(
            run_cli(
                [
                    "resolve",
                    "sync-publish",
                    "-p",
                    "cli_dvr",
                    "-t",
                    "video/grade",
                    "--name",
                    "main",
                    "--version",
                    "1",
                    "--from-working",
                    "1",
                    "-m",
                    "Final color",
                    "--tag",
                    "hdr",
                    "--json",
                ],
                hub=self.hub,
            ),
            0,
        )
        self.assertEqual(
            run_cli(["resolve", "uninstall", "--scripts-dir", str(scripts_dir), "--no-studio-api", "--json"], hub=self.hub),
            0,
        )

    def test_resolve_dialogs_and_main_window_smoke(self) -> None:
        proj = self.ws.init_project("gui_dvr", self.root / "gui_dvr")
        self.ws.create_component(proj, "finish", task_template="resolve_video")

        stem_v1 = self.root / "mix_v1.wav"
        stem_v2 = self.root / "mix_v2.wav"
        stem_v1.write_bytes(b"WAV1")
        stem_v2.write_bytes(b"WAV2")
        b_mix1 = self.ws.publish_version(proj, "audio", [stem_v1], deliverable_name="main")
        self.ws.publish_version(proj, "audio", [stem_v2], deliverable_name="main")
        mix_v1_path = (proj / "audio" / b_mix1.primary_file).resolve()

        # Test ResolveImportDialog in Resolve Lua script context_file/result_file mode
        res_imp_file = self.root / "imp_result.json"
        ctx_data = {
            "project_name": "DVR_Clip_Test",
            "current_bin": "Master",
            "render_presets": ["H.264 Master"],
            "quick_export_presets": ["ProRes 422"],
            "media_pool_clips": [
                {"name": mix_v1_path.name, "file_path": str(mix_v1_path), "bin_path": "Master/Audio"}
            ],
        }
        d_imp = ResolveImportDialog(
            None,
            self.hub,
            self.ws,
            self.bridge,
            context_data=ctx_data,
            result_file=res_imp_file,
            default_project="gui_dvr",
            default_component="finish",
        )
        self.assertGreaterEqual(d_imp.asset_tree.topLevelItemCount(), 2)
        self.assertEqual(d_imp.updates_table.rowCount(), 1)
        self.assertEqual(d_imp.updates_table.item(0, 5).text(), "Update Available")

        # Select an importable file and confirm result JSON is written
        d_imp.asset_tree.topLevelItem(0).setSelected(True)
        d_imp._on_import_selected()
        self.assertTrue(res_imp_file.is_file())
        imp_plan = json.loads(res_imp_file.read_text(encoding="utf-8"))
        self.assertEqual(imp_plan["action"], "import_media")
        self.assertTrue(imp_plan["params"]["file_paths"])
        d_imp.close()

        # Test ResolveExportPublishDialog in result_file mode
        res_pub_file = self.root / "pub_result.json"
        d_pub = ResolveExportPublishDialog(
            None,
            self.hub,
            self.ws,
            self.bridge,
            context_data=ctx_data,
            result_file=res_pub_file,
            default_project="gui_dvr",
            default_component="finish",
        )
        d_pub.note_edit.setText("Review cut")
        d_pub._on_execute()
        self.assertTrue(res_pub_file.is_file())
        pub_plan = json.loads(res_pub_file.read_text(encoding="utf-8"))
        self.assertEqual(pub_plan["action"], "render_deliver_job")
        self.assertEqual(pub_plan["sync_args"]["component_path"], "finish")
        self.assertEqual(
            self.bridge.get_project_binding("DVR_Clip_Test"),
            {"project_name": "gui_dvr", "component_path": "finish"},
        )
        d_pub.close()

        d_setup = ResolveIntegrationDialog(None, self.hub, self.bridge)
        self.assertIn("Offline", d_setup.status_pill.text())
        d_setup.close()

        win = ObolMainWindow(hub=self.hub, initial_project="gui_dvr", initial_component="finish")
        self.assertIn("Resolve:", win.resolve_status_pill.text())
        self.assertIn("Resolve", win.dcc_export_btn.text())
        win.close()


if __name__ == "__main__":
    unittest.main()
