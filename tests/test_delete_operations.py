"""Tests for deleting working, repo, and published versions, named repo assets/deliverables, components, and projects."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from obol.cli.main import run_cli
from obol.core.hub import ObolHub
from obol.core.storage import discover_project_components, get_component_meta_dir, load_component_snapshot
from obol.core.workspace import ObolWorkspace


class TestDeleteOperations(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)
        self.proj_root = self.ws.init_project("del_proj", self.root / "del_proj", project_template="single_track")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_delete_working_repo_published_versions_and_named_containers(self) -> None:
        # Working: create v002 (which locks v001 read-only on Windows), then delete v001
        self.ws.version_up_working(self.proj_root, "arrangement", comment="v2")
        self.assertTrue((self.proj_root / "arrangement" / "working" / "v001").is_dir())
        self.assertTrue(self.ws.delete_working_version(self.proj_root, "arrangement", 1))
        self.assertFalse((self.proj_root / "arrangement" / "working" / "v001").exists())
        snap = load_component_snapshot(self.proj_root, "arrangement")
        self.assertEqual([v.version for v in snap.working_versions], [2])

        # Repo: ingest v001 & v002 of audio/snare, delete v001, then delete entire asset (and verify empty category pruned)
        s1 = self.root / "snare1.wav"
        s2 = self.root / "snare2.wav"
        s1.write_bytes(b"S1")
        s2.write_bytes(b"S2")
        self.ws.ingest_repo_asset(self.proj_root, "arrangement", s1, asset_path="audio/snare")
        self.ws.ingest_repo_asset(self.proj_root, "arrangement", s2, asset_path="audio/snare")
        self.assertTrue(self.ws.delete_repo_version(self.proj_root, "arrangement", "audio/snare", 1))
        self.assertFalse((self.proj_root / "arrangement" / "repo" / "audio" / "snare" / "v001").exists())
        snap = load_component_snapshot(self.proj_root, "arrangement")
        self.assertEqual(snap.repo_assets["audio/snare"].latest_version, 2)

        self.assertTrue(self.ws.delete_repo_asset(self.proj_root, "arrangement", "audio/snare"))
        self.assertFalse((self.proj_root / "arrangement" / "repo" / "audio" / "snare").exists())
        self.assertFalse((self.proj_root / "arrangement" / "repo" / "audio").exists())
        snap = load_component_snapshot(self.proj_root, "arrangement")
        self.assertNotIn("audio/snare", snap.repo_assets)

        # Published: publish v001 & v002 into "main", delete v001, then delete deliverable "main"
        p1 = self.root / "m1.wav"
        p2 = self.root / "m2.wav"
        p1.write_bytes(b"P1")
        p2.write_bytes(b"P2")
        self.ws.publish_version(self.proj_root, "arrangement", source_files=[p1], deliverable_name="main")
        self.ws.publish_version(self.proj_root, "arrangement", source_files=[p2], deliverable_name="main")
        self.assertTrue(self.ws.delete_published_version(self.proj_root, "arrangement", "main", 1))
        self.assertFalse((self.proj_root / "arrangement" / "published" / "main" / "v001").exists())
        snap = load_component_snapshot(self.proj_root, "arrangement")
        self.assertEqual([v.version for v in snap.published_deliverables["main"].versions], [2])

        self.assertTrue(self.ws.delete_published_deliverable(self.proj_root, "arrangement", "main"))
        self.assertFalse((self.proj_root / "arrangement" / "published" / "main").exists())
        snap = load_component_snapshot(self.proj_root, "arrangement")
        self.assertNotIn("main", snap.published_deliverables)

    def test_delete_component_and_project_api_and_cli(self) -> None:
        # Delete component via workspace API
        self.assertTrue(self.ws.delete_component(self.proj_root, "mixing", delete_files=True))
        self.assertFalse((self.proj_root / "mixing").exists())
        self.assertFalse(get_component_meta_dir(self.proj_root, "mixing").exists())
        self.assertNotIn("mixing", discover_project_components(self.proj_root))

        # CLI delete working, repo, publish, component, project
        w2 = self.ws.version_up_working(self.proj_root, "mastering")
        self.assertEqual(w2.version, 2)
        self.assertEqual(run_cli(["delete", "working", "mastering", "-v", "1", "-p", "del_proj"], hub=self.hub), 0)
        self.assertFalse((self.proj_root / "mastering" / "working" / "v001").exists())

        wav = self.root / "kick.wav"
        wav.write_bytes(b"K")
        self.ws.ingest_repo_asset(self.proj_root, "mastering", wav, asset_path="drums/kick")
        self.assertEqual(run_cli(["delete", "repo", "drums/kick", "-t", "mastering", "-p", "del_proj"], hub=self.hub), 0)
        self.assertNotIn("drums/kick", load_component_snapshot(self.proj_root, "mastering").repo_assets)

        pub = self.root / "out.wav"
        pub.write_bytes(b"O")
        self.ws.publish_version(self.proj_root, "mastering", source_files=[pub], deliverable_name="radio")
        self.assertEqual(run_cli(["delete", "publish", "radio", "-t", "mastering", "-p", "del_proj"], hub=self.hub), 0)
        self.assertNotIn("radio", load_component_snapshot(self.proj_root, "mastering").published_deliverables)

        self.assertEqual(run_cli(["delete", "component", "mastering", "-p", "del_proj"], hub=self.hub), 0)
        self.assertFalse((self.proj_root / "mastering").exists())

        self.assertEqual(run_cli(["delete", "project", "del_proj", "--delete-files"], hub=self.hub), 0)
        self.assertIsNone(self.hub.get_project("del_proj"))
        self.assertFalse(self.proj_root.exists())


if __name__ == "__main__":
    unittest.main()
