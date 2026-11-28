"""Automated unit and headless Qt GUI tests for the Unified DCC Media Management system."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from obol.core.hub import ObolHub
from obol.core.workspace import ObolWorkspace
from obol.gui.app import ObolMainWindow
from obol.gui.filmcraft_dialogs import FilmCraftImportDialog
from obol.gui.media_manager_dialog import DCCMediaManagerDialog
from obol.gui.resolve_dialogs import ResolveImportDialog
from obol.plugins.filmcraft import FilmCraftPlugin
from obol.plugins.filmcraft_bridge import FilmCraftBridgeClient
from obol.plugins.media_registry import (
    DCCMediaContext,
    DCCMediaRegistry,
    MediaStatus,
)
from obol.plugins.reaper_bridge import ReaperBridgeClient, ReaperSessionState
from obol.plugins.resolve_bridge import ResolveBridgeClient, ResolveSessionState
from obol.plugins.send_registry import DCCSendContext, ResolveSendTarget


class _MockResolveClip:
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

    def GetThirdPartyMetadata(self) -> dict[str, str]:
        return dict(self.meta)

    def ReplaceClipPreserveSubClip(self, new_path: str) -> bool:
        self._file_path = new_path
        self._name = Path(new_path).name
        return True


class _MockResolveFolder:
    def __init__(self, name: str) -> None:
        self._name = name
        self._clips: list[_MockResolveClip] = []
        self._subs: list[_MockResolveFolder] = []

    def GetName(self) -> str:
        return self._name

    def GetClipList(self) -> list[_MockResolveClip]:
        return list(self._clips)

    def GetSubFolderList(self) -> list[_MockResolveFolder]:
        return list(self._subs)


class _MockResolveMediaPool:
    def __init__(self) -> None:
        self.root = _MockResolveFolder("Master")
        self.current = self.root
        self._seq = 0

    def GetRootFolder(self) -> _MockResolveFolder:
        return self.root

    def GetCurrentFolder(self) -> _MockResolveFolder:
        return self.current

    def SetCurrentFolder(self, folder: _MockResolveFolder) -> bool:
        self.current = folder
        return True

    def AddSubFolder(self, parent: _MockResolveFolder, name: str) -> _MockResolveFolder:
        sub = _MockResolveFolder(name)
        parent._subs.append(sub)
        return sub

    def ImportMedia(self, paths: list[str]) -> list[_MockResolveClip]:
        out: list[_MockResolveClip] = []
        for p in paths:
            self._seq += 1
            clip = _MockResolveClip(Path(p).name, p, media_id=f"dvr_{self._seq}")
            self.current._clips.append(clip)
            out.append(clip)
        return out

    def AppendToTimeline(self, clips: list[_MockResolveClip]) -> list[str]:
        return [c.GetMediaId() for c in clips]


class _MockResolveProject:
    def __init__(self) -> None:
        self.media_pool = _MockResolveMediaPool()

    def GetName(self) -> str:
        return "ResolveMediaTest"

    def GetMediaPool(self) -> _MockResolveMediaPool:
        return self.media_pool

    def GetCurrentTimeline(self) -> None:
        return None

    def GetRenderPresetList(self) -> list[str]:
        return []

    def GetQuickExportRenderPresets(self) -> list[str]:
        return []


class _MockResolveProjectManager:
    def __init__(self, project: _MockResolveProject) -> None:
        self.project = project

    def GetCurrentProject(self) -> _MockResolveProject:
        return self.project


class _MockResolveApp:
    def __init__(self) -> None:
        self.project = _MockResolveProject()
        self.pm = _MockResolveProjectManager(self.project)

    def GetProductName(self) -> str:
        return "DaVinci Resolve Studio"

    def GetVersionString(self) -> str:
        return "19.1.0"

    def GetCurrentPage(self) -> str:
        return "media"

    def GetProjectManager(self) -> _MockResolveProjectManager:
        return self.pm


class TestDCCMediaManager(unittest.TestCase):
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

    def _seed_asset_versions(
        self,
        project_root: Path,
        component: str,
        asset_path: str,
        ext: str = ".wav",
        count: int = 3,
    ) -> list[Path]:
        paths: list[Path] = []
        for idx in range(1, count + 1):
            src = self.root / f"src_{asset_path.replace('/', '_')}_v{idx}{ext}"
            src.write_bytes(f"DATA_V{idx}".encode("utf-8"))
            _, bundle = self.ws.ingest_repo_asset(
                project_root,
                component,
                src,
                asset_path=asset_path,
                comment=f"Take v{idx}",
            )
            paths.append((project_root / component / bundle.primary_file).resolve())
        return paths

    def test_cross_project_resolution_and_version_metadata(self) -> None:
        proj_a = self.ws.init_project("proj_a", self.root / "proj_a", custom_components=[])
        self.ws.create_component(proj_a, "audio", task_template="reaper_daw")
        proj_b = self.ws.init_project("proj_b", self.root / "proj_b", custom_components=[])
        self.ws.create_component(proj_b, "sfx", task_template="reaper_daw")

        bass_vers = self._seed_asset_versions(proj_b, "sfx", "audio/bass", count=3)

        # Resolve v002 of proj_b even when preferred_project_root is proj_a
        info = self.ws.resolve_media_file_across_projects(bass_vers[1], preferred_project_root=proj_a)
        self.assertIsNotNone(info)
        assert info is not None
        self.assertEqual(info["project_name"], "proj_b")
        self.assertEqual(info["current_version"], 2)
        self.assertEqual(info["latest_version"], 3)
        self.assertTrue(info["update_available"])
        self.assertTrue(info["has_older_versions"])
        self.assertTrue(info["has_newer_versions"])
        self.assertTrue(info["current_exists"])
        self.assertEqual(info["prev_version"]["version"], 1)
        self.assertEqual(info["next_version"]["version"], 3)
        self.assertEqual([v["version"] for v in info["available_versions"]], [1, 2, 3])
        self.assertTrue(all("created_at" in v and "size_bytes" in v for v in info["available_versions"]))

    def test_reaper_media_adapter_live_and_offline_version_up_and_down(self) -> None:
        proj = self.ws.init_project("reap_mm", self.root / "reap_mm", custom_components=[])
        self.ws.create_component(proj, "audio", task_template="reaper_daw")
        rpp_v1 = (proj / "audio" / "working" / "v001" / "audio_v001.rpp").resolve()

        kick_vers = self._seed_asset_versions(proj, "audio", "audio/kick", count=3)
        snare_vers = self._seed_asset_versions(proj, "audio", "audio/snare", count=3)
        ext_file = (self.root / "external_pad.wav").resolve()
        ext_file.write_bytes(b"EXTPAD")

        # 1. Offline .rpp mode: kick at v001 (outdated), snare at v003 (latest, can version down), plus external
        rel_kick_v1 = os.path.relpath(kick_vers[0], rpp_v1.parent).replace("\\", "/")
        rel_snare_v3 = os.path.relpath(snare_vers[2], rpp_v1.parent).replace("\\", "/")
        rpp_v1.write_text(
            "<REAPER_PROJECT 0.1 \"7.0\"\n"
            "  <TRACK\n"
            "    <ITEM\n"
            "      <SOURCE WAVE\n"
            f'        FILE "{rel_kick_v1}"\n'
            "      >\n"
            "    >\n"
            "    <ITEM\n"
            "      <SOURCE WAVE\n"
            f'        FILE "{rel_snare_v3}"\n'
            "      >\n"
            "    >\n"
            "    <ITEM\n"
            "      <SOURCE WAVE\n"
            f'        FILE "{ext_file}"\n'
            "      >\n"
            "    >\n"
            "  >\n"
            ">\n",
            encoding="utf-8",
        )

        bridge = ReaperBridgeClient(self.hub)
        reg = DCCMediaRegistry(self.hub, reaper_bridge=bridge, allow_resolve_studio_api=False)

        ctx_tracked = DCCMediaContext(
            hub=self.hub,
            workspace=self.ws,
            project_root=proj,
            project_name="reap_mm",
            component_path="audio",
            target_file=rpp_v1,
            include_external=False,
        )
        scan_tracked = reg.scan("reaper", ctx_tracked)
        self.assertEqual(scan_tracked.session.mode, "offline_project")
        self.assertEqual(scan_tracked.total_count, 2)
        self.assertEqual(scan_tracked.outdated_count, 1)

        ctx_all = DCCMediaContext(
            hub=self.hub,
            workspace=self.ws,
            project_root=proj,
            project_name="reap_mm",
            component_path="audio",
            target_file=rpp_v1,
            include_external=True,
        )
        scan_all = reg.scan("reaper", ctx_all)
        self.assertEqual(scan_all.total_count, 3)
        self.assertEqual(scan_all.external_count, 1)

        kick_item = next(it for it in scan_all.items if it.asset_key == "audio/kick")
        snare_item = next(it for it in scan_all.items if it.asset_key == "audio/snare")
        self.assertEqual(kick_item.current_version, 1)
        self.assertEqual(snare_item.current_version, 3)
        self.assertTrue(snare_item.can_version_down)

        # Version up kick (v001 -> v003) and version down snare (v003 -> v002)
        sw_up = reg.build_switch_request(kick_item, 3, ctx_all)
        sw_down = reg.build_switch_request(snare_item, 2, ctx_all)
        self.assertIsNotNone(sw_up)
        self.assertIsNotNone(sw_down)
        assert sw_up is not None and sw_down is not None
        self.assertTrue(sw_up.is_upgrade)
        self.assertTrue(sw_down.is_downgrade)

        apply_off = reg.apply_switches("reaper", [sw_up, sw_down], ctx_all)
        self.assertTrue(apply_off.ok)
        self.assertEqual(apply_off.upgraded_count, 1)
        self.assertEqual(apply_off.downgraded_count, 1)

        rpp_text = rpp_v1.read_text(encoding="utf-8")
        self.assertIn("kick_v003.wav", rpp_text)
        self.assertIn("snare_v002.wav", rpp_text)

        # 2. Live mock REAPER bridge: scan and version down snare v002 -> v001
        bridge.state_path.write_text(
            json.dumps({
                "connected": True,
                "heartbeat": time.time(),
                "reaper_version": "7.34/win64",
                "active_rpp": str(rpp_v1),
            }),
            encoding="utf-8",
        )
        live_session = [
            {
                "item_idx": 0,
                "take_idx": 0,
                "item_guid": "{ITEM-SNARE}",
                "take_guid": "{TAKE-SNARE}",
                "track_name": "Drums",
                "take_name": snare_vers[1].name,
                "file_path": str(snare_vers[1]),
                "is_midi": False,
                "obol_ext": json.dumps(self.ws.describe_asset_file(proj, snare_vers[1])),
            },
            {
                "item_idx": 1,
                "take_idx": 0,
                "item_guid": "{ITEM-EXT}",
                "take_guid": "{TAKE-EXT}",
                "track_name": "Pad",
                "take_name": ext_file.name,
                "file_path": str(ext_file),
                "is_midi": False,
                "obol_ext": "",
            },
        ]
        stop_evt = threading.Event()

        def _mock_daemon() -> None:
            while not stop_evt.is_set():
                for f in sorted(bridge.inbox_dir.glob("*.json")):
                    req = json.loads(f.read_text(encoding="utf-8"))
                    f.unlink(missing_ok=True)
                    act = req["action"]
                    params = req.get("params") or {}
                    if act == "query_session_media":
                        res = {"items": live_session}
                    elif act == "update_media_sources":
                        upds = params.get("take_updates") or []
                        for u in upds:
                            for sit in live_session:
                                if sit["take_guid"] == u.get("take_guid"):
                                    sit["file_path"] = u["target_path"]
                                    sit["obol_ext"] = json.dumps(u.get("asset_meta") or {})
                        res = {"updated_takes": len(upds), "updated_items": len(upds)}
                    else:
                        res = {"ok": True}
                    (bridge.outbox_dir / f"{req['id']}.json").write_text(
                        json.dumps({"id": req["id"], "ok": True, "result": res}),
                        encoding="utf-8",
                    )
                time.sleep(0.02)

        t = threading.Thread(target=_mock_daemon, daemon=True)
        t.start()
        try:
            ctx_live = DCCMediaContext(
                hub=self.hub,
                workspace=self.ws,
                project_root=proj,
                project_name="reap_mm",
                include_external=True,
            )
            scan_live = reg.scan("reaper", ctx_live)
            self.assertEqual(scan_live.session.mode, "live")
            self.assertEqual(scan_live.total_count, 2)
            self.assertEqual(scan_live.external_count, 1)
            snare_live = next(it for it in scan_live.items if it.asset_key == "audio/snare")
            self.assertEqual(snare_live.current_version, 2)

            sw_live_down = reg.build_switch_request(snare_live, 1, ctx_live)
            assert sw_live_down is not None
            res_live = reg.apply_switches("reaper", [sw_live_down], ctx_live)
            self.assertTrue(res_live.ok)
            self.assertEqual(res_live.downgraded_count, 1)
            self.assertEqual(Path(live_session[0]["file_path"]).resolve(), snare_vers[0])

            scan_after = reg.scan("reaper", ctx_live)
            snare_after = next(it for it in scan_after.items if it.asset_key == "audio/snare")
            self.assertEqual(snare_after.current_version, 1)
        finally:
            stop_evt.set()
            t.join(timeout=2.0)

    def test_resolve_media_adapter_tagging_and_version_down(self) -> None:
        proj = self.ws.init_project("dvr_mm", self.root / "dvr_mm", custom_components=[])
        self.ws.create_component(proj, "finish", task_template="resolve_video")
        plate_vers = self._seed_asset_versions(proj, "finish", "video/plate", ext=".mov", count=3)
        ext_clip = (self.root / "ref_external.mov").resolve()
        ext_clip.write_bytes(b"EXTMOV")

        mock_res = _MockResolveApp()
        r_bridge = ResolveBridgeClient(self.hub, allow_studio_api=False, studio_resolve_obj=mock_res)

        # Send plate v003 via ResolveSendTarget and verify obol_* metadata is tagged on clip
        send_target = ResolveSendTarget(r_bridge)
        send_ctx = DCCSendContext(
            hub=self.hub,
            workspace=self.ws,
            project_root=proj,
            project_name="dvr_mm",
            component_path="finish",
        )
        send_res = send_target.send_files([plate_vers[2]], "media_pool", send_ctx)
        self.assertTrue(send_res.ok)
        r_bridge.import_media([ext_clip], bin_path="Obol/External")

        reg = DCCMediaRegistry(self.hub, resolve_bridge=r_bridge, allow_resolve_studio_api=False)
        ctx = DCCMediaContext(
            hub=self.hub,
            workspace=self.ws,
            project_root=proj,
            project_name="dvr_mm",
            include_external=True,
        )
        scan = reg.scan("resolve", ctx)
        self.assertEqual(scan.session.mode, "live")
        self.assertEqual(scan.total_count, 2)
        self.assertEqual(scan.external_count, 1)

        plate_item = next(it for it in scan.items if it.asset_key == "video/plate")
        self.assertEqual(plate_item.current_version, 3)
        self.assertEqual(plate_item.status, MediaStatus.UP_TO_DATE)
        self.assertTrue(plate_item.can_version_down)

        # Version down v003 -> v001 and verify Resolve clip path + obol_version metadata updated
        sw_down = reg.build_switch_request(plate_item, 1, ctx)
        assert sw_down is not None
        res_down = reg.apply_switches("resolve", [sw_down], ctx)
        self.assertTrue(res_down.ok)
        self.assertEqual(res_down.downgraded_count, 1)

        pool_after = r_bridge.query_media_pool()
        plate_clip = next(c for c in pool_after if "plate_v001" in c["file_path"])
        self.assertEqual(plate_clip["obol_meta"].get("version"), "1")

        # Scan again -> now reports update available (v001 -> v003)
        scan_after = reg.scan("resolve", ctx)
        plate_after = next(it for it in scan_after.items if it.asset_key == "video/plate")
        self.assertEqual(plate_after.current_version, 1)
        self.assertEqual(plate_after.status, MediaStatus.UPDATE_AVAILABLE)

    def test_filmcraft_media_adapter_offline_and_version_down(self) -> None:
        proj = self.ws.init_project("fc_mm", self.root / "fc_mm", custom_components=[])
        self.ws.create_component(proj, "edit", task_template="filmcraft_video")
        fcproj = (proj / "edit" / "working" / "v001" / "edit_v001.fcproj").resolve()

        clip_vers = self._seed_asset_versions(proj, "edit", "video/cut", ext=".mp4", count=3)
        ext_clip = (self.root / "scratch_broll.mp4").resolve()
        ext_clip.write_bytes(b"BROLL")

        data = json.loads(fcproj.read_text(encoding="utf-8"))
        data["project"]["root"]["children"] = [{"Bin": {"id": 1, "name": "Edit", "children": [{"Item": 2}, {"Item": 3}]}}]
        data["project"]["items"] = {
            "2": {
                "id": 2,
                "name": clip_vers[2].name,
                "kind": {"Media": {"media": {"File": {"path": os.path.relpath(clip_vers[2], fcproj.parent).replace("\\", "/")}}}},
            },
            "3": {
                "id": 3,
                "name": ext_clip.name,
                "kind": {"Media": {"media": {"File": {"path": str(ext_clip)}}}},
            },
        }
        fcproj.write_text(json.dumps(data), encoding="utf-8")

        f_bridge = FilmCraftBridgeClient(self.hub)
        reg = DCCMediaRegistry(self.hub, filmcraft_bridge=f_bridge, allow_resolve_studio_api=False)
        ctx = DCCMediaContext(
            hub=self.hub,
            workspace=self.ws,
            project_root=proj,
            project_name="fc_mm",
            component_path="edit",
            target_file=fcproj,
            include_external=True,
        )
        with patch.object(f_bridge, "resolve_cli_executable", return_value=None):
            scan = reg.scan("filmcraft", ctx)
            self.assertEqual(scan.total_count, 2)
            self.assertEqual(scan.external_count, 1)
            cut_item = next(it for it in scan.items if it.asset_key == "video/cut")
            self.assertEqual(cut_item.current_version, 3)

            # Version down v003 -> v002 using offline JSON fallback and verify relative path persisted
            sw_down = reg.build_switch_request(cut_item, 2, ctx)
            assert sw_down is not None
            res_down = reg.apply_switches("filmcraft", [sw_down], ctx)
            self.assertTrue(res_down.ok, res_down.message)
            self.assertEqual(res_down.downgraded_count, 1)

        items_after = FilmCraftPlugin.extract_media_items(fcproj)
        cut_after = next(it for it in items_after if it["id"] == 2)
        self.assertEqual(cut_after["raw_path"], "../../repo/video/cut/v002/cut_v002.mp4")

    def test_custom_plugin_media_adapter_discovery(self) -> None:
        plugin_file = self.hub.plugins_dir / "custom_media_ext.py"
        plugin_file.write_text(
            "from obol.plugins.media_registry import (\n"
            "    DCCMediaApplyResult, DCCMediaScanResult, DCCMediaSessionInfo\n"
            ")\n"
            "class CustomMediaAdapter:\n"
            "    key = 'custom_nle'\n"
            "    display_name = 'CustomNLE'\n"
            "    full_name = 'Custom NLE'\n"
            "    def get_session_info(self, ctx):\n"
            "        return DCCMediaSessionInfo(dcc_key='custom_nle', display_name='CustomNLE', mode='live', connected=True)\n"
            "    def scan_loaded_media(self, ctx):\n"
            "        return DCCMediaScanResult(session=self.get_session_info(ctx), items=[])\n"
            "    def apply_version_switches(self, switches, ctx):\n"
            "        return DCCMediaApplyResult(True, 'ok')\n"
            "class Plugin:\n"
            "    name = 'custom_nle_ext'\n"
            "    extensions = ('.cnle',)\n"
            "    def on_version_created(self, *a, **kw): pass\n"
            "    def inspect_dependencies(self, *a, **kw): return [], []\n"
            "    def get_inspector_actions(self, ctx): return []\n"
            "    def get_media_adapter(self, hub):\n"
            "        return CustomMediaAdapter()\n",
            encoding="utf-8",
        )
        hub2 = ObolHub(hub_dir=self.root / "hub")
        reg = DCCMediaRegistry(hub2, allow_resolve_studio_api=False)
        adapter = reg.get("custom_nle")
        self.assertIsNotNone(adapter)
        res = reg.scan("custom_nle", DCCMediaContext(hub=hub2, workspace=ObolWorkspace(hub2)))
        self.assertTrue(res.session.connected)

    def test_gui_media_manager_dialog_and_per_dcc_version_down(self) -> None:
        proj = self.ws.init_project("gui_mm", self.root / "gui_mm", custom_components=[])
        self.ws.create_component(proj, "finish", task_template="resolve_video")
        self.ws.create_component(proj, "edit", task_template="filmcraft_video")

        plate_vers = self._seed_asset_versions(proj, "finish", "video/plate", ext=".mov", count=3)
        ext_mov = (self.root / "ext_gui.mov").resolve()
        ext_mov.write_bytes(b"EXT")

        mock_res = _MockResolveApp()
        r_bridge = ResolveBridgeClient(self.hub, allow_studio_api=False, studio_resolve_obj=mock_res)
        r_bridge.import_media([plate_vers[2], ext_mov], bin_path="Master")

        reg = DCCMediaRegistry(self.hub, resolve_bridge=r_bridge, allow_resolve_studio_api=False)
        dlg = DCCMediaManagerDialog(
            None,
            self.hub,
            self.ws,
            reg,
            initial_dcc="resolve",
            project_root=proj,
            project_name="gui_mm",
            component_path="finish",
            auto_scan=True,
        )
        mw = dlg.manager_widget
        self.assertEqual(mw.media_table.rowCount(), 1)
        self.assertEqual(mw.media_table.item(0, 7).text(), "Up to Date")
        self.assertFalse(mw.apply_btn.isEnabled())

        # Toggle external media -> shows 2 rows
        mw.show_external_chk.setChecked(True)
        self.assertEqual(mw.media_table.rowCount(), 2)
        self.assertEqual(mw.media_table.item(1, 7).text(), "External")

        # Select row 0 and click Version Down (v-1): v003 -> v002
        mw.media_table.selectRow(0)
        mw.version_down_btn.click()
        self.assertEqual(mw.media_table.item(0, 7).text(), "Version Down → v002")
        self.assertTrue(mw.apply_btn.isEnabled())
        self.assertIn("1 ↓", mw.apply_btn.text())

        mw.apply_btn.click()
        self.assertEqual(mw.media_table.item(0, 5).text(), "v002")
        # After apply & rescan, v002 has v003 available so target defaults to v003 (Update Available)
        self.assertIn("Update Available", mw.media_table.item(0, 7).text())

        # Filter to external/unmanaged and verify tracked row hides
        idx_unm = mw.status_filter_combo.findData("unmanaged")
        mw.status_filter_combo.setCurrentIndex(idx_unm)
        self.assertTrue(mw.media_table.isRowHidden(0))
        self.assertFalse(mw.media_table.isRowHidden(1))
        dlg.close()
        dlg.deleteLater()

        # Verify MainWindow DCC Exchange Bar exposes Media button
        win = ObolMainWindow(hub=self.hub, initial_project="gui_mm", initial_component="finish")
        with (
            patch.object(win.reaper_bridge, "get_state", return_value=ReaperSessionState(connected=False)),
            patch.object(win.resolve_bridge, "get_state", return_value=ResolveSessionState(connected=True, mode="lua_bridge", project_name="ResolveMediaTest")),
            patch.object(win.filmcraft_bridge, "is_live_bridge_listening", return_value=False),
        ):
            win._refresh_dcc_target_ui()
            self.assertTrue(win.dcc_media_btn.isEnabled())
            self.assertEqual(win.dcc_media_btn.text(), "Media (Resolve)…")
        win.close()
        win.deleteLater()

        # Verify ResolveImportDialog Tab 2 supports Version Down
        d_res = ResolveImportDialog(
            None,
            self.hub,
            self.ws,
            r_bridge,
            default_project="gui_mm",
            default_component="finish",
        )
        d_res.scan_media_pool_updates()
        self.assertEqual(d_res.updates_table.rowCount(), 1)
        # Loaded is v002 so target defaults to v003 (Update Available); step down twice (v003 -> v002 -> v001)
        self.assertEqual(d_res.updates_table.item(0, 5).text(), "Update Available")
        d_res.updates_table.selectRow(0)
        d_res.version_down_btn.click()
        self.assertEqual(d_res.updates_table.item(0, 5).text(), "Up to Date")
        d_res.version_down_btn.click()
        self.assertEqual(d_res.updates_table.item(0, 5).text(), "Version Down → v001")
        with patch("obol.gui.resolve_dialogs.QMessageBox.information"):
            d_res._on_apply_updates()
        pool_now = r_bridge.query_media_pool()
        self.assertTrue(any("plate_v001" in c["file_path"] for c in pool_now))
        d_res.close()
        d_res.deleteLater()

        # Verify FilmCraftImportDialog Tab 2 supports Version Down
        fcproj = (proj / "edit" / "working" / "v001" / "edit_v001.fcproj").resolve()
        fc_vers = self._seed_asset_versions(proj, "edit", "video/shot", ext=".mp4", count=2)
        fc_data = json.loads(fcproj.read_text(encoding="utf-8"))
        fc_data["project"]["items"] = {
            "5": {
                "id": 5,
                "name": fc_vers[1].name,
                "kind": {"Media": {"media": {"File": {"path": os.path.relpath(fc_vers[1], fcproj.parent).replace("\\", "/")}}}},
            }
        }
        fcproj.write_text(json.dumps(fc_data), encoding="utf-8")

        f_bridge = FilmCraftBridgeClient(self.hub)
        with patch.object(f_bridge, "resolve_cli_executable", return_value=None):
            d_fc = FilmCraftImportDialog(
                None,
                self.hub,
                self.ws,
                f_bridge,
                active_fcproj=str(fcproj),
                default_project="gui_mm",
                default_component="edit",
            )
            d_fc.scan_project_updates()
            self.assertEqual(d_fc.updates_table.rowCount(), 1)
            self.assertEqual(d_fc.updates_table.item(0, 5).text(), "Up to Date")
            d_fc.updates_table.selectRow(0)
            d_fc.version_down_btn.click()
            self.assertEqual(d_fc.updates_table.item(0, 5).text(), "Version Down → v001")
            with patch("obol.gui.filmcraft_dialogs.QMessageBox.information"):
                d_fc._on_apply_updates()
            self.assertIn("shot_v001.mp4", fcproj.read_text(encoding="utf-8"))
            d_fc.close()
            d_fc.deleteLater()


if __name__ == "__main__":
    unittest.main()
