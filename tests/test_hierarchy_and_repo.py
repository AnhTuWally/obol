"""Unit tests for arbitrary-depth component hierarchy, version-controlled repo/ assets, provenance metadata, and credits report."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from obol.core.hub import ObolHub
from obol.core.storage import discover_project_components, load_component_snapshot
from obol.core.workspace import ObolWorkspace


class TestHierarchyAndRepo(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_mixed_depth_hierarchy_and_versioned_repo_with_provenance(self) -> None:
        proj_root = self.ws.init_project(
            name="album_a",
            root_path=self.root / "album_a",
            project_template="music_album",
        )
        # Add a deeper nested component (depth 3) and a depth 1 component
        self.ws.create_component(proj_root, "song_1/vocals/lead_editing", task_template="reaper_daw")
        self.ws.create_component(proj_root, "master_bus", task_template="reaper_daw")

        comps = discover_project_components(proj_root)
        self.assertIn("cover_art", comps)
        self.assertIn("song_1/arrangement", comps)
        self.assertIn("song_1/mixing", comps)
        self.assertIn("song_1/vocals/lead_editing", comps)
        self.assertIn("master_bus", comps)

        # Ingest v001 of a repo asset with provenance metadata
        v1_wav = self.root / "vocal_raw.wav"
        v1_wav.write_bytes(b"RIFF-VOCAL-V1")
        asset, b1 = self.ws.ingest_repo_asset(
            project_root=proj_root,
            component_path="song_1/arrangement",
            source_file=v1_wav,
            asset_path="audio/lead_vocal",
            author="Sarah Jenkins",
            source_info="Studio Session A",
            license_info="Work for Hire",
            tags=["vocal", "128bpm"],
            comment="Dry vocal stem",
            custom={"bpm": 128, "key": "Am"},
        )
        self.assertEqual(b1.version, 1)
        self.assertTrue((proj_root / "song_1/arrangement/repo/audio/lead_vocal/v001/lead_vocal_v001.wav").is_file())

        # Ingest v002 of the same repo asset
        v2_wav = self.root / "vocal_tuned.wav"
        v2_wav.write_bytes(b"RIFF-VOCAL-V2-TUNED")
        asset, b2 = self.ws.ingest_repo_asset(
            project_root=proj_root,
            component_path="song_1/arrangement",
            source_file=v2_wav,
            asset_path="audio/lead_vocal",
            comment="Pitch-corrected chorus",
        )
        self.assertEqual(b2.version, 2)
        self.assertEqual(asset.latest_version, 2)
        self.assertTrue((proj_root / "song_1/arrangement/repo/audio/lead_vocal/v002/lead_vocal_v002.wav").is_file())

        # Update metadata (including URL and custom fields with replace_custom) and verify credits + index
        self.ws.update_repo_metadata(
            project_root=proj_root,
            component_path="song_1/arrangement",
            asset_path="audio/lead_vocal",
            url="https://freesound.org/people/sarah/sounds/12345/",
            license_info="Exclusive Commercial",
            custom={"bpm": "124", "key": "Am", "temp_key": "remove_me"},
            replace_custom=True,
        )
        self.ws.update_repo_metadata(
            project_root=proj_root,
            component_path="song_1/arrangement",
            asset_path="audio/lead_vocal",
            custom={"bpm": "124", "key": "Am"},
            replace_custom=True,
        )
        reloaded = load_component_snapshot(proj_root, "song_1/arrangement")
        self.assertEqual(reloaded.repo_assets["audio/lead_vocal"].url, "https://freesound.org/people/sarah/sounds/12345/")
        self.assertEqual(reloaded.repo_assets["audio/lead_vocal"].custom, {"bpm": "124", "key": "Am"})

        credits = self.ws.generate_credits_report(proj_root)
        self.assertEqual(len(credits), 1)
        self.assertEqual(credits[0]["author"], "Sarah Jenkins")
        self.assertEqual(credits[0]["url"], "https://freesound.org/people/sarah/sounds/12345/")
        self.assertEqual(credits[0]["license"], "Exclusive Commercial")
        self.assertEqual(credits[0]["latest_version"], "v002")

        from obol.core.index import ProjectIndex

        idx = ProjectIndex(proj_root)
        hits = idx.search_repo_assets(query="freesound.org")
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["url"], "https://freesound.org/people/sarah/sounds/12345/")


if __name__ == "__main__":
    unittest.main()
