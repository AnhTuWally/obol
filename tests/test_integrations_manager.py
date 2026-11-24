"""Automated tests for the unified DCC Integration Manager, drift diagnostics, CLI, and UI."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from obol.cli.main import run_cli
from obol.core.hub import ObolHub
from obol.core.workspace import ObolWorkspace
from obol.gui.app import ObolMainWindow
from obol.gui.dialogs import SoftwareSettingsDialog
from obol.gui.integrations_dialog import IntegrationsManagerDialog
from obol.plugins.integrations import DCCIntegrationManager, REAPER_INTEGRATION_VERSION
from obol.plugins.reaper_bridge import ReaperBridgeClient
from obol.plugins.resolve_bridge import ResolveBridgeClient


class TestIntegrationsManager(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)
        self.mgr = DCCIntegrationManager(self.hub, allow_resolve_studio_api=False)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_manager_reports_drift_detection_and_install_all(self) -> None:
        reaper_res = self.root / "REAPER_RES"
        resolve_scripts = self.root / "ResolveScripts"
        filmcraft_dir = self.root / "FilmCraftDir"
        reaper_res.mkdir(parents=True, exist_ok=True)
        resolve_scripts.mkdir(parents=True, exist_ok=True)
        filmcraft_dir.mkdir(parents=True, exist_ok=True)
        (filmcraft_dir / "filmcraft.exe").write_bytes(b"MZ")
        (filmcraft_dir / "filmcraft-cli.exe").write_bytes(b"MZ")
        overrides = {"reaper": reaper_res, "resolve": resolve_scripts, "filmcraft": filmcraft_dir}

        rep_r0 = self.mgr.get_report("reaper", target_dir=reaper_res)
        rep_d0 = self.mgr.get_report("resolve", target_dir=resolve_scripts)
        rep_f0 = self.mgr.get_report("filmcraft", target_dir=filmcraft_dir)
        self.assertTrue(rep_r0.detected)
        self.assertFalse(rep_r0.installed)
        self.assertEqual(rep_r0.overall_state, "not_installed")
        self.assertTrue(rep_d0.detected)
        self.assertFalse(rep_d0.installed)
        self.assertTrue(rep_f0.detected)
        self.assertFalse(rep_f0.installed)

        # Batch install all detected
        reports = self.mgr.install_all_detected(target_dir_overrides=overrides)
        self.assertEqual(len(reports), 3)
        for rep in reports:
            self.assertTrue(rep.installed)
            self.assertFalse(rep.needs_update)
            self.assertEqual(rep.overall_state, "installed")
            self.assertGreater(len(rep.installed_files), 0)

        # Simulate config drift in REAPER obol_config.lua
        cfg_lua = reaper_res / "Scripts" / "Obol" / "obol_config.lua"
        txt = cfg_lua.read_text(encoding="utf-8").replace(
            f"INTEGRATION_VERSION = [=[{REAPER_INTEGRATION_VERSION}]=]",
            "INTEGRATION_VERSION = [=[0.9]=]",
        )
        if "INTEGRATION_VERSION = [=[0.9]=]" not in txt:
            txt = cfg_lua.read_text(encoding="utf-8").replace(REAPER_INTEGRATION_VERSION, "0.9", 1)
        cfg_lua.write_text(txt, encoding="utf-8")

        drifted = self.mgr.get_report("reaper", target_dir=reaper_res)
        self.assertTrue(drifted.needs_update)
        self.assertEqual(drifted.overall_state, "needs_update")
        self.assertTrue(any(c.label == "Runtime Config Sync" and c.state == "warn" for c in drifted.checks))

        # Re-install repairs drift
        repaired = self.mgr.install("reaper", target_dir=reaper_res)
        self.assertFalse(repaired.needs_update)
        self.assertEqual(repaired.overall_state, "installed")

        # Uninstall all
        un_r = self.mgr.uninstall("reaper", target_dir=reaper_res)
        un_d = self.mgr.uninstall("resolve", target_dir=resolve_scripts)
        un_f = self.mgr.uninstall("filmcraft", target_dir=filmcraft_dir)
        self.assertFalse(un_r.installed)
        self.assertFalse(un_d.installed)
        self.assertFalse(un_f.installed)

    def test_manager_test_connection_and_resolve_bindings(self) -> None:
        # Resolve bindings management
        rbridge = ResolveBridgeClient(self.hub, allow_studio_api=False)
        rbridge.set_project_binding("ResolveProj_1", "mv_proj", "finish")
        rbridge.set_project_binding("ResolveProj_2", "doc_proj", "color")
        self.assertEqual(len(rbridge.list_project_bindings()), 2)
        self.assertTrue(rbridge.remove_project_binding("ResolveProj_1"))
        self.assertNotIn("ResolveProj_1", rbridge.list_project_bindings())
        self.assertFalse(rbridge.remove_project_binding("MissingProj"))

        # REAPER live ping test
        reap_client = ReaperBridgeClient(self.hub)
        reap_client.state_path.write_text(
            json.dumps({
                "connected": True,
                "heartbeat": time.time(),
                "reaper_version": "7.28/win64",
                "active_rpp": "D:/demo/audio/working/v001/audio_v001.rpp",
            }),
            encoding="utf-8",
        )
        stop_evt = threading.Event()

        def _ping_daemon() -> None:
            while not stop_evt.is_set():
                for f in reap_client.inbox_dir.glob("*.json"):
                    req = json.loads(f.read_text(encoding="utf-8"))
                    f.unlink(missing_ok=True)
                    resp = {
                        "id": req["id"],
                        "ok": True,
                        "result": {"pong": True, "reaper_version": "7.28/win64", "active_rpp": "D:/demo/audio/working/v001/audio_v001.rpp"},
                    }
                    (reap_client.outbox_dir / f"{req['id']}.json").write_text(json.dumps(resp), encoding="utf-8")
                time.sleep(0.02)

        t = threading.Thread(target=_ping_daemon, daemon=True)
        t.start()
        try:
            ok, msg = self.mgr.test_connection("reaper", timeout_s=2.0)
            self.assertTrue(ok)
            self.assertIn("7.28", msg)
        finally:
            stop_evt.set()
            t.join(timeout=2.0)

    def test_integrations_cli_commands(self) -> None:
        r_dir = self.root / "CLI_REAPER"
        d_dir = self.root / "CLI_RESOLVE"
        r_dir.mkdir(parents=True, exist_ok=True)
        d_dir.mkdir(parents=True, exist_ok=True)

        self.assertEqual(run_cli(["integrations", "status", "--no-studio-api", "--json"], hub=self.hub), 0)
        self.assertEqual(
            run_cli(
                ["integrations", "install", "reaper", "--target-dir", str(r_dir), "--no-studio-api", "--json"],
                hub=self.hub,
            ),
            0,
        )
        self.assertEqual(
            run_cli(
                ["integrations", "install", "resolve", "--target-dir", str(d_dir), "--no-studio-api", "--json"],
                hub=self.hub,
            ),
            0,
        )
        self.assertTrue((r_dir / "Scripts" / "Obol" / "obol_config.lua").is_file())
        self.assertTrue((d_dir / "Utility" / "Obol" / "obol_resolve_config.lua").is_file())
        self.assertEqual(
            run_cli(
                ["integrations", "uninstall", "reaper", "--target-dir", str(r_dir), "--no-studio-api", "--json"],
                hub=self.hub,
            ),
            0,
        )
        self.assertEqual(
            run_cli(
                ["integrations", "uninstall", "resolve", "--target-dir", str(d_dir), "--no-studio-api", "--json"],
                hub=self.hub,
            ),
            0,
        )

    def test_integrations_manager_dialog_and_main_window_smoke(self) -> None:
        r_dir = self.root / "UI_REAPER"
        d_dir = self.root / "UI_RESOLVE"
        f_dir = self.root / "UI_FILMCRAFT"
        r_dir.mkdir(parents=True, exist_ok=True)
        d_dir.mkdir(parents=True, exist_ok=True)
        f_dir.mkdir(parents=True, exist_ok=True)
        (f_dir / "filmcraft.exe").write_bytes(b"MZ")
        (f_dir / "filmcraft-cli.exe").write_bytes(b"MZ")

        ResolveBridgeClient(self.hub, allow_studio_api=False).set_project_binding("DVR_Test", "p1", "edit")

        dlg = IntegrationsManagerDialog(
            None,
            self.hub,
            initial_dcc="reaper",
            allow_resolve_studio_api=False,
            target_dir_overrides={"reaper": r_dir, "resolve": d_dir, "filmcraft": f_dir},
        )
        self.assertEqual(dlg.dcc_list.topLevelItemCount(), 3)
        self.assertGreaterEqual(dlg.health_table.rowCount(), 6)

        # Install REAPER directly from UI
        dlg._on_install_current()
        self.assertIn("Installed", dlg.feedback_label.text())
        self.assertTrue((r_dir / "Scripts" / "Obol" / "obol_bridge.lua").is_file())

        # Switch to DaVinci Resolve row, install & inspect bindings
        dlg.dcc_list.setCurrentItem(dlg.dcc_list.topLevelItem(1))
        self.assertEqual(dlg._current_key, "resolve")
        self.assertEqual(dlg.bindings_table.rowCount(), 1)
        dlg._on_install_current()
        self.assertTrue((d_dir / "Deliver" / "Obol - Export and Publish.lua").is_file())

        # Remove binding from UI
        dlg.bindings_table.selectRow(0)
        dlg._on_remove_selected_binding()
        self.assertEqual(dlg.bindings_table.rowCount(), 0)

        # Uninstall without blocking QMessageBox
        dlg._on_uninstall_current(confirm=False)
        self.assertFalse((d_dir / "Utility" / "Obol").exists())
        dlg.close()

        # Verify SoftwareSettingsDialog auto-detect helper & ObolMainWindow DCC status pill
        sw_dlg = SoftwareSettingsDialog(None, self.hub)
        sw_dlg._load_selected("reaper")
        sw_dlg._auto_detect_executable()
        sw_dlg.close()

        self.ws.init_project("dcc_pill_proj", self.root / "dcc_pill_proj")
        win = ObolMainWindow(hub=self.hub, initial_project="dcc_pill_proj")
        self.assertTrue(win.dcc_status_pill.text().startswith("DCC:"))
        win.close()


if __name__ == "__main__":
    unittest.main()
