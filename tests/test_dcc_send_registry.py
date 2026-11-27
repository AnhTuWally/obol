"""Tests for the unified DCCSendRegistry, target resolution, config persistence, and DCC Exchange Bar."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication, QMenu

from obol.core.hub import ObolHub
from obol.core.models import GlobalConfig
from obol.core.workspace import ObolWorkspace
from obol.gui.app import ObolMainWindow
from obol.plugins.reaper_bridge import ReaperSessionState
from obol.plugins.resolve_bridge import ResolveSessionState
from obol.plugins.send_registry import (
    DCCSendContext,
    DCCSendMode,
    DCCSendRegistry,
    DCCSendResult,
    DCCSendStatus,
)


class _StubSendTarget:
    def __init__(
        self,
        key: str,
        display_name: str,
        *,
        live: bool = False,
        can_send_offline: bool = False,
        mode_badge: str = "",
        modes: list[DCCSendMode] | None = None,
        ok: bool = True,
    ) -> None:
        self.key = key
        self.display_name = display_name
        self.full_name = f"{display_name} App"
        self._live = live
        self._can_send_offline = can_send_offline
        self._mode_badge = mode_badge or ("Live" if live else "Offline")
        self._modes = modes or [
            DCCSendMode("default_mode", "Default Import", "Default"),
            DCCSendMode("alt_mode", "Alternate Import", "Alt"),
            DCCSendMode(
                "update_existing",
                "Update Existing",
                "Update",
                is_update_action=True,
                remember_as_default=False,
            ),
        ]
        self.default_mode_id = self._modes[0].id
        self._ok = ok
        self.calls: list[tuple[list[Path], str]] = []

    def get_modes(self) -> list[DCCSendMode]:
        return list(self._modes)

    def get_status(self, ctx: DCCSendContext) -> DCCSendStatus:
        return DCCSendStatus(
            live=self._live,
            can_send_offline=self._can_send_offline,
            mode_badge=self._mode_badge,
            detail="",
        )

    def send_files(self, paths: list[Path], mode_id: str, ctx: DCCSendContext) -> DCCSendResult:
        self.calls.append((list(paths), mode_id))
        if not self._ok:
            return DCCSendResult(False, "Send failed")
        return DCCSendResult(True, f"Sent {len(paths)} file(s) to {self.display_name}", sent_count=len(paths))


class TestDCCSendRegistry(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_config_persistence_and_backward_compatibility(self) -> None:
        legacy = GlobalConfig.from_dict({
            "default_workspace_root": str(self.root),
            "last_active_project": "p1",
            "fast_drop": True,
        })
        self.assertEqual(legacy.pinned_dcc_target, "")
        self.assertEqual(legacy.last_used_dcc_target, "")
        self.assertEqual(legacy.dcc_default_modes, {})

        self.hub.set_pinned_dcc_target("reaper")
        self.hub.record_dcc_send("reaper", "cursor_track", remember_mode=True)
        self.hub.record_dcc_send("reaper", "update_existing", remember_mode=False)
        self.hub.record_dcc_send("resolve", "timeline", remember_mode=True)

        reloaded = ObolHub(hub_dir=self.root / "hub")
        self.assertEqual(reloaded.config.pinned_dcc_target, "reaper")
        self.assertEqual(reloaded.config.last_used_dcc_target, "resolve")
        self.assertEqual(reloaded.get_preferred_dcc_mode("reaper"), "cursor_track")
        self.assertEqual(reloaded.get_preferred_dcc_mode("resolve"), "timeline")

    def test_auto_resolution_pinning_and_mode_memory(self) -> None:
        registry = DCCSendRegistry(self.hub, allow_resolve_studio_api=False)
        for key in ("reaper", "resolve", "filmcraft"):
            registry.unregister(key)

        t_alpha = _StubSendTarget("alpha", "Alpha", live=False)
        t_beta = _StubSendTarget("beta", "Beta", live=False, can_send_offline=True, mode_badge="Headless")
        t_gamma = _StubSendTarget("gamma", "Gamma", live=False)
        registry.register(t_alpha)
        registry.register(t_beta)
        registry.register(t_gamma)

        ctx = DCCSendContext(hub=self.hub, workspace=self.ws, component_software="alpha")

        # 0 live, but Beta supports headless offline send -> Beta auto-selected
        active, live, offline = registry.resolve_targets(ctx)
        self.assertEqual(live, [])
        self.assertEqual(len(offline), 3)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.target.key, "beta")
        self.assertFalse(active.is_pinned)

        # 0 live and no headless -> active is None
        t_beta._can_send_offline = False
        active, live, _ = registry.resolve_targets(ctx)
        self.assertIsNone(active)

        # 1 live -> auto-selects live target
        t_gamma._live = True
        active, live, _ = registry.resolve_targets(ctx)
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.target.key, "gamma")
        self.assertEqual([r.target.key for r in live], ["gamma"])

        # 2 live -> prefers component_software ('alpha') when last_used is empty
        t_alpha._live = True
        active, live, _ = registry.resolve_targets(ctx)
        assert active is not None
        self.assertEqual(active.target.key, "alpha")

        # Send to gamma with alt_mode -> last_used becomes gamma and alt_mode is remembered
        dummy = self.root / "clip.wav"
        dummy.write_bytes(b"WAV")
        res = registry.send("gamma", [dummy], "alt_mode", ctx)
        self.assertTrue(res.ok)
        self.assertEqual(self.hub.config.last_used_dcc_target, "gamma")
        self.assertEqual(self.hub.get_preferred_dcc_mode("gamma"), "alt_mode")

        # Send with update_existing (remember_as_default=False) -> does NOT overwrite alt_mode
        res_upd = registry.send("gamma", [dummy], "update_existing", ctx)
        self.assertTrue(res_upd.ok)
        self.assertEqual(self.hub.get_preferred_dcc_mode("gamma"), "alt_mode")

        # Now 2 live prefers last_used ('gamma') over component_software ('alpha')
        active, live, _ = registry.resolve_targets(ctx)
        assert active is not None
        self.assertEqual(active.target.key, "gamma")
        self.assertEqual(active.default_mode.id, "alt_mode")

        # Pinning overrides Auto even when pinned target is offline
        self.hub.set_pinned_dcc_target("beta")
        active, _, _ = registry.resolve_targets(ctx)
        assert active is not None
        self.assertEqual(active.target.key, "beta")
        self.assertTrue(active.is_pinned)

    def test_custom_user_plugin_send_target_auto_discovery(self) -> None:
        plugin_file = self.hub.plugins_dir / "blender_send_ext.py"
        plugin_file.write_text(
            "from obol.plugins.send_registry import DCCSendMode, DCCSendResult, DCCSendStatus\n"
            "class CustomTarget:\n"
            "    key = 'custom_blender'\n"
            "    display_name = 'Blender'\n"
            "    full_name = 'Blender 3D'\n"
            "    default_mode_id = 'vse'\n"
            "    def get_modes(self):\n"
            "        return [DCCSendMode('vse', 'Video Sequencer', 'VSE')]\n"
            "    def get_status(self, ctx):\n"
            "        return DCCSendStatus(live=True, mode_badge='Live')\n"
            "    def send_files(self, paths, mode_id, ctx):\n"
            "        return DCCSendResult(True, f'Sent {len(paths)} to Blender', sent_count=len(paths))\n"
            "class Plugin:\n"
            "    name = 'custom_blender_ext'\n"
            "    extensions = ('.blend',)\n"
            "    def on_version_created(self, *a, **kw): pass\n"
            "    def inspect_dependencies(self, *a, **kw): return [], []\n"
            "    def get_inspector_actions(self, ctx): return []\n"
            "    def get_send_target(self, hub):\n"
            "        return CustomTarget()\n",
            encoding="utf-8",
        )
        hub2 = ObolHub(hub_dir=self.root / "hub")
        reg = DCCSendRegistry(hub2, allow_resolve_studio_api=False)
        self.assertIsNotNone(reg.get("custom_blender"))
        ctx = DCCSendContext(hub=hub2, workspace=ObolWorkspace(hub2))
        active, live, _ = reg.resolve_targets(ctx)
        self.assertIn("custom_blender", [r.target.key for r in live])
        self.assertIsNotNone(active)
        assert active is not None
        self.assertEqual(active.target.key, "custom_blender")

    def test_gui_dcc_exchange_bar_and_clean_context_menus(self) -> None:
        proj = self.ws.init_project("send_ui_proj", self.root / "send_ui_proj", custom_components=[])
        self.ws.create_component(proj, "audio", task_template="reaper_daw")

        wav = self.root / "snare.wav"
        wav.write_bytes(b"RIFF")
        self.ws.ingest_repo_asset(proj, "audio", wav, asset_path="audio/snare")

        pub_wav = self.root / "mix.wav"
        pub_mp3 = self.root / "mix.mp3"
        pub_wav.write_bytes(b"MIXWAV")
        pub_mp3.write_bytes(b"MIXMP3")
        self.ws.publish_version(proj, "audio", source_files=[pub_wav, pub_mp3], deliverable_name="main")

        win = ObolMainWindow(hub=self.hub, initial_project="send_ui_proj", initial_component="audio")
        try:
            # 0 live DCCs
            with (
                patch.object(win.reaper_bridge, "get_state", return_value=ReaperSessionState(connected=False)),
                patch.object(win.resolve_bridge, "get_state", return_value=ResolveSessionState(connected=False)),
                patch.object(win.filmcraft_bridge, "is_live_bridge_listening", return_value=False),
            ):
                win._refresh_dcc_target_ui()
                win.triad_tabs.setCurrentIndex(0)
                self.assertFalse(win.dcc_exchange_bar.isHidden())
                self.assertEqual(win.dcc_target_combo.itemText(0), "Auto (No Live DCC)")
                self.assertFalse(win.dcc_mode_combo.isEnabled())
                self.assertFalse(win.dcc_send_btn.isEnabled())
                self.assertEqual(win.dcc_export_btn.text(), "Export (REAPER)…")
                self.assertFalse(win.dcc_export_btn.icon().isNull())
                self.assertFalse(win.dcc_send_btn.icon().isNull())

                win.triad_tabs.setCurrentIndex(1)
                self.assertFalse(win.dcc_send_btn.isEnabled())

            # Tab 2 (Published): select v001 bundle -> gathers 2 files
            win.triad_tabs.setCurrentIndex(2)
            v1_row = win.published_version_tree.topLevelItem(0)
            self.assertIsNotNone(v1_row)
            win.published_version_tree.setCurrentItem(v1_row)
            self.assertEqual(len(win._selected_sendable_paths()), 2)

            # 1 live (REAPER)
            reaper_live = ReaperSessionState(connected=True, reaper_version="7.22", active_rpp="song.rpp")
            with (
                patch.object(win.reaper_bridge, "get_state", return_value=reaper_live),
                patch.object(win.reaper_bridge, "is_connected", return_value=True),
                patch.object(win.resolve_bridge, "get_state", return_value=ResolveSessionState(connected=False)),
                patch.object(win.filmcraft_bridge, "is_live_bridge_listening", return_value=False),
            ):
                win._refresh_dcc_target_ui()
                self.assertIn("Auto → REAPER ●", win.dcc_target_combo.itemText(0))
                self.assertTrue(win.dcc_mode_combo.isEnabled())
                self.assertTrue(win.dcc_send_btn.isEnabled())
                self.assertEqual(win.dcc_send_btn.text(), "Send 2 Files to REAPER")
                self.assertFalse(win.dcc_send_btn.icon().isNull())
                self.assertEqual(win.dcc_export_btn.text(), "Export (REAPER)…")
                self.assertFalse(win.dcc_export_btn.icon().isNull())

                # Switch mode combo to 'At Cursor' -> persists and updates Send
                cursor_idx = win.dcc_mode_combo.findData("cursor_track")
                self.assertGreaterEqual(cursor_idx, 0)
                win.dcc_mode_combo.setCurrentIndex(cursor_idx)
                self.assertEqual(win.hub.get_preferred_dcc_mode("reaper"), "cursor_track")

                with patch.object(
                    win.reaper_bridge,
                    "insert_media",
                    return_value={"inserted": 2},
                ) as mock_insert:
                    win.dcc_send_btn.click()
                    mock_insert.assert_called_once()
                    self.assertEqual(mock_insert.call_args.kwargs["mode"], "cursor_track")

                # Switching to Update Existing updates button label without overwriting default insert mode
                upd_idx = win.dcc_mode_combo.findData("update_existing")
                self.assertGreaterEqual(upd_idx, 0)
                win.dcc_mode_combo.setCurrentIndex(upd_idx)
                self.assertEqual(win.dcc_send_btn.text(), "Update in REAPER")
                self.assertFalse(win.dcc_send_btn.icon().isNull())
                self.assertEqual(win.hub.get_preferred_dcc_mode("reaper"), "cursor_track")

            # 2 live (REAPER + Resolve) and pinning Resolve
            resolve_live = ResolveSessionState(connected=True, mode="lua_bridge", project_name="Edit01")
            with (
                patch.object(win.reaper_bridge, "get_state", return_value=reaper_live),
                patch.object(win.resolve_bridge, "get_state", return_value=resolve_live),
                patch.object(win.filmcraft_bridge, "is_live_bridge_listening", return_value=False),
            ):
                win._refresh_dcc_target_ui()
                res_idx = win.dcc_target_combo.findData("resolve")
                self.assertGreaterEqual(res_idx, 0)
                win.dcc_target_combo.setCurrentIndex(res_idx)
                self.assertEqual(win.hub.config.pinned_dcc_target, "resolve")
                self.assertEqual(win.dcc_send_btn.text(), "Send 2 Files to Resolve")
                self.assertFalse(win.dcc_send_btn.icon().isNull())
                self.assertEqual(win.dcc_export_btn.text(), "Export (Resolve)…")

                with patch.object(win, "_action_export_from_resolve") as mock_export_resolve:
                    win.dcc_export_btn.click()
                    mock_export_resolve.assert_called_once()

            # Right-click context menus do NOT call resolve_targets (zero bridge latency) and stay compact
            opened_menus: list[list[str]] = []

            class _NonBlockingMenu(QMenu):
                def exec(self, *args, **kwargs):  # type: ignore[override]
                    opened_menus.append([a.text() for a in self.actions() if not a.isSeparator()])
                    return None

            with (
                patch.object(win.send_registry, "resolve_targets", side_effect=AssertionError("must not probe bridges")),
                patch("obol.gui.app.QMenu", _NonBlockingMenu),
            ):
                win.triad_tabs.setCurrentIndex(1)
                rv_item = win.repo_version_list.topLevelItem(0)
                self.assertIsNotNone(rv_item)
                rv_pos = win.repo_version_list.visualItemRect(rv_item).center()
                win._ctx_repo_version_list(rv_pos if not rv_pos.isNull() else QPoint(5, 5))

                win.triad_tabs.setCurrentIndex(2)
                pv_pos = win.published_version_tree.visualItemRect(v1_row).center()
                win._ctx_published_version_tree(pv_pos if not pv_pos.isNull() else QPoint(5, 5))

            self.assertEqual(len(opened_menus), 2)
            self.assertLessEqual(len(opened_menus[0]), 6)
            self.assertLessEqual(len(opened_menus[1]), 6)
            self.assertFalse(any("REAPER" in lbl or "Resolve" in lbl or "FilmCraft" in lbl for m in opened_menus for lbl in m))
        finally:
            win.close()
            win.deleteLater()
            self.app.processEvents()


if __name__ == "__main__":
    unittest.main()
