"""Unit tests for 2-Tier Software Integration, all 4 Version-Up strategies, read-only locking, and custom user plugins."""

from __future__ import annotations

import os
import stat
import tempfile
import unittest
from pathlib import Path

from obol.core.hub import ObolHub
from obol.core.models import SoftwareProfile, VersionUpStrategy
from obol.core.workspace import ObolWorkspace


class TestSoftwareAndVersionUp(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_clone_primary_file_ignores_backups_and_locks_previous_version(self) -> None:
        proj_root = self.ws.init_project("sw_test", self.root / "sw_test")
        v1_dir = proj_root / "audio" / "working" / "v001"
        v1_rpp = v1_dir / "audio_v001.rpp"
        # Create a junk .rpp-bak file that should be ignored on version up
        (v1_dir / "audio_v001.rpp-bak").write_text("backup junk", encoding="utf-8")

        b2 = self.ws.version_up_working(proj_root, "audio", comment="Second version")
        self.assertEqual(b2.version, 2)
        v2_dir = proj_root / "audio" / "working" / "v002"
        self.assertTrue((v2_dir / "audio_v002.rpp").is_file())
        self.assertFalse((v2_dir / "audio_v001.rpp-bak").exists())

        # Verify v001 primary file was locked read-only
        mode = v1_rpp.stat().st_mode
        self.assertFalse(bool(mode & stat.S_IWUSR))

    def test_clone_entire_folder_strategy_for_bundle_apps(self) -> None:
        self.hub.set_software_profile(
            SoftwareProfile(
                key="ableton",
                display_name="Ableton Live",
                extensions=[".als"],
                version_up_strategy=VersionUpStrategy.CLONE_ENTIRE_FOLDER,
                ignore_patterns=["Backup/**"],
                lock_previous_versions=False,
            )
        )
        proj_root = self.ws.init_project("bundle_proj", self.root / "bundle_proj", custom_components=[])
        self.ws.create_component(proj_root, "live_set", software_override="ableton", seed_initial_working=True)

        v1_dir = proj_root / "live_set" / "working" / "v001"
        (v1_dir / "Samples").mkdir(exist_ok=True)
        (v1_dir / "Samples" / "loop.wav").write_bytes(b"LOOP")
        (v1_dir / "Backup").mkdir(exist_ok=True)
        (v1_dir / "Backup" / "old.als").write_bytes(b"OLD")

        b2 = self.ws.version_up_working(proj_root, "live_set", comment="Cloned bundle")
        v2_dir = proj_root / "live_set" / "working" / "v002"
        self.assertEqual(b2.version, 2)
        self.assertTrue((v2_dir / "Samples" / "loop.wav").is_file())
        self.assertFalse((v2_dir / "Backup").exists())

    def test_dynamic_custom_user_plugin_loading(self) -> None:
        plugin_code = '''
from pathlib import Path
from obol.plugins.base import PluginAction

class CustomSynthPlugin:
    name = "custom_synth"
    extensions = (".syn",)
    def on_version_created(self, version_dir, primary_file, prev_version_dir, ctx):
        (version_dir / "patched_by_plugin.txt").write_text("ok", encoding="utf-8")
    def inspect_dependencies(self, primary_file, ctx):
        return [], []
    def get_inspector_actions(self, ctx):
        return [PluginAction("synth_act", "Run Synth Action")]

def register_plugin():
    return CustomSynthPlugin()
'''
        (self.hub.plugins_dir / "custom_synth.py").write_text(plugin_code, encoding="utf-8")
        loaded = self.hub.plugin_registry.load_user_plugins(self.hub.plugins_dir)
        self.assertIn("custom_synth", loaded)


if __name__ == "__main__":
    unittest.main()
