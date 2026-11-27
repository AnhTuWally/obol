"""Tests for real application icons on software profiles and the Launch button."""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication

from obol.core.hub import ObolHub
from obol.core.models import SoftwareProfile
from obol.core.workspace import ObolWorkspace
from obol.gui.icons import get_icon
from obol.gui.software_icons import get_software_icon, has_app_icon, resolve_software_icon

ASSET_PNG = Path(__file__).resolve().parents[1] / "src" / "obol" / "gui" / "assets" / "obol_icon.png"


class TestSoftwareIcons(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.app = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.app.processEvents()
        self.tmp.cleanup()

    def test_profile_icon_roundtrip_and_backcompat(self) -> None:
        prof = SoftwareProfile(key="reaper", display_name="REAPER", icon="C:/icons/reaper.ico")
        data = prof.to_dict()
        self.assertEqual(data["icon"], "C:/icons/reaper.ico")
        self.assertEqual(SoftwareProfile.from_dict("reaper", data).icon, "C:/icons/reaper.ico")
        # Blank icon serialises like other optional strings; legacy dicts without 'icon' still load
        self.assertEqual(SoftwareProfile(key="x", display_name="X").to_dict().get("icon", ""), "")
        self.assertEqual(SoftwareProfile.from_dict("x", {"display_name": "X"}).icon, "")

    def test_fallback_when_nothing_resolves(self) -> None:
        prof = SoftwareProfile(key="ghost", display_name="Ghost", executable=str(self.root / "missing.exe"))
        self.assertIsNone(resolve_software_icon(prof))
        self.assertFalse(has_app_icon(prof))
        fb = get_icon("play", "#ffffff")
        self.assertIs(get_software_icon(prof, fallback=fb), fb)
        self.assertTrue(get_software_icon(None).isNull())

    def test_custom_icon_override_wins(self) -> None:
        prof = SoftwareProfile(key="c", display_name="C", executable=sys.executable, icon=str(ASSET_PNG))
        icon = resolve_software_icon(prof)
        self.assertIsNotNone(icon)
        self.assertFalse(icon.pixmap(QSize(16, 16)).isNull())
        # Cached: repeated lookups return the same QIcon
        self.assertEqual(resolve_software_icon(prof).cacheKey(), icon.cacheKey())

    def test_executable_icon_extracted(self) -> None:
        prof = SoftwareProfile(key="py", display_name="Python", executable=sys.executable)
        self.assertTrue(has_app_icon(prof))
        self.assertFalse(get_software_icon(prof).pixmap(QSize(18, 18)).isNull())

    def test_sample_file_association_fallback(self) -> None:
        sample = self.root / "notes.txt"
        sample.write_text("hi", encoding="utf-8")
        prof = SoftwareProfile(key="generic", display_name="Generic")
        self.assertTrue(has_app_icon(prof, sample))

    def test_launch_button_shows_app_icon(self) -> None:
        from obol.gui.app import ObolMainWindow

        hub = ObolHub(hub_dir=self.root / "hub")
        ws = ObolWorkspace(hub)
        prof = hub.get_software_profile("reaper")
        prof.icon = str(ASSET_PNG)
        hub.set_software_profile(prof)
        ws.init_project("icon_proj", self.root / "icon_proj")

        win = ObolMainWindow(hub=hub, initial_project="icon_proj", initial_component="audio")
        try:
            self.assertEqual(win.active_component_path, "audio")
            self.assertEqual(win.active_snapshot.software, "reaper")
            win.triad_tabs.setCurrentIndex(0)
            win.working_list.setCurrentItem(win.working_list.topLevelItem(0))
            win._sync_inspector_to_active_tab()

            idx = win.insp_software_combo.findData("reaper")
            self.assertGreaterEqual(idx, 0)
            self.assertFalse(win.insp_software_combo.itemIcon(idx).isNull())

            self.assertTrue(win.launch_btn.isEnabled())
            self.assertEqual(win.launch_btn.iconSize(), QSize(18, 18))
            self.assertEqual(win.launch_btn.icon().cacheKey(), resolve_software_icon(prof).cacheKey())
            self.assertIn(prof.display_name, win.launch_btn.toolTip())

            # Group / idle state falls back to the monochrome play glyph
            win.launch_btn.setEnabled(False)
            win._refresh_launch_btn_icon()
            self.assertEqual(win.launch_btn.iconSize(), QSize(13, 13))
        finally:
            win.close()
            win.deleteLater()


if __name__ == "__main__":
    unittest.main()
