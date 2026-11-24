"""Unit tests for the 3-Tier Composable Template Engine, interactive tree preview, and template capture."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from obol.core.hub import ObolHub
from obol.core.workspace import ObolWorkspace


class TestTemplates(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_three_tier_templates_preview_and_capture(self) -> None:
        tm = self.hub.template_manager
        preview = tm.preview_project_tree("my_album", "music_album")
        self.assertEqual(preview["project_name"], "my_album")
        comp_paths = [c["component_path"] for c in preview["components"]]
        self.assertIn("song_1/arrangement", comp_paths)

        # Initialize project, customize a component, and capture all 3 tiers
        proj_root = self.ws.init_project("custom_ost", self.root / "custom_ost", project_template="simple_media")
        self.ws.create_component(proj_root, "synths/lead", task_template="reaper_daw")

        seed_rpp = proj_root / "synths" / "lead" / "working" / "v001" / "lead_v001.rpp"
        tm.capture_template("file", seed_rpp, "custom_lead.rpp")
        self.assertIn("custom_lead.rpp", tm.list_file_templates())

        tm.capture_template("task", proj_root / "synths" / "lead", "synth_lead_task")
        self.assertIn("synth_lead_task", tm.list_task_templates())

        tm.capture_template("project", proj_root, "ost_layout")
        captured_pt = tm.get_project_template("ost_layout")
        self.assertIn("synths/lead", captured_pt.default_components)


if __name__ == "__main__":
    unittest.main()

