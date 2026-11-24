"""Unit tests for the Reaper DAW plugin: .rpp path patching and external media scanner/collector."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from obol.core.hub import ObolHub
from obol.core.workspace import ObolWorkspace
from obol.plugins.base import unlock_file_writable
from obol.plugins.reaper import ReaperPlugin


class TestReaperPlugin(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rpp_relative_paths_and_external_media_collection(self) -> None:
        proj_root = self.ws.init_project("reaper_proj", self.root / "reaper_proj")
        v1_rpp = proj_root / "audio" / "working" / "v001" / "audio_v001.rpp"
        content = v1_rpp.read_text(encoding="utf-8")
        self.assertIn('RECORD_PATH "../../repo/recorded"', content)
        self.assertIn('RENDER_FILE "../../published"', content)

        # Simulate dragging an external sample from a Downloads folder into the .rpp
        downloads_dir = self.root / "Downloads"
        downloads_dir.mkdir(parents=True, exist_ok=True)
        ext_sample = downloads_dir / "Sub_Drop_808.wav"
        ext_sample.write_bytes(b"RIFF-808-SUB")

        unlock_file_writable(v1_rpp)
        v1_rpp.write_text(
            f"""<REAPER_PROJECT 0.1 "7.0"
  RECORD_PATH "../../repo/recorded" ""
  RENDER_FILE "../../published"
  <TRACK
    <ITEM
      <SOURCE WAVE
        FILE "{ext_sample.as_posix()}"
      >
    >
  >
>
""",
            encoding="utf-8",
        )

        externals = self.ws.scan_external_dependencies(proj_root, "audio")
        self.assertEqual(len(externals), 1)
        self.assertEqual(externals[0].suggested_asset_name, "sub_drop_808")

        collected = self.ws.collect_external_dependencies(proj_root, "audio")
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0].asset_path, "audio/sub_drop_808")

        # Verify the sample is now inside repo/audio/sub_drop_808/v001/ and the .rpp uses a relative path
        expected_repo_file = proj_root / "audio" / "repo" / "audio" / "sub_drop_808" / "v001" / "sub_drop_808_v001.wav"
        self.assertTrue(expected_repo_file.is_file())
        updated_rpp = v1_rpp.read_text(encoding="utf-8")
        self.assertIn("../../repo/audio/sub_drop_808/v001/sub_drop_808_v001.wav", updated_rpp)

        # Re-scanning should now report 0 external dependencies
        self.assertEqual(len(self.ws.scan_external_dependencies(proj_root, "audio")), 0)

    def test_offline_rpp_detects_and_updates_newer_midi_and_audio_versions(self) -> None:
        proj_root = self.ws.init_project("reaper_upd_proj", self.root / "reaper_upd_proj")
        v1_rpp = proj_root / "audio" / "working" / "v001" / "audio_v001.rpp"

        kick1 = self.root / "kick1.wav"
        kick2 = self.root / "kick2.wav"
        kick1.write_bytes(b"WAV1")
        kick2.write_bytes(b"WAV2")
        _, k_b1 = self.ws.ingest_repo_asset(proj_root, "audio", kick1, asset_path="audio/kick")
        _, k_b2 = self.ws.ingest_repo_asset(proj_root, "audio", kick2, asset_path="audio/kick")

        mid1 = self.root / "lead1.mid"
        mid2 = self.root / "lead2.mid"
        mid1.write_bytes(b"MThd-1")
        mid2.write_bytes(b"MThd-2")
        _, m_b1 = self.ws.ingest_repo_asset(proj_root, "audio", mid1, asset_path="midi/lead_melody")
        _, m_b2 = self.ws.ingest_repo_asset(proj_root, "audio", mid2, asset_path="midi/lead_melody")

        unlock_file_writable(v1_rpp)
        v1_rpp.write_text(
            f"""<REAPER_PROJECT 0.1 "7.0"
  RECORD_PATH "../../repo/recorded" ""
  RENDER_FILE "../../published"
  <TRACK
    <ITEM
      <SOURCE WAVE
        FILE "../../{k_b1.primary_file}"
      >
    >
  >
  <TRACK
    <ITEM
      <SOURCE MIDI
        FILE "../../{m_b1.primary_file}"
      >
    >
  >
>
""",
            encoding="utf-8",
        )

        refs = ReaperPlugin.find_outdated_rpp_references(v1_rpp, proj_root, self.ws)
        self.assertEqual(len(refs), 2)
        self.assertTrue(all(r["update_available"] for r in refs))
        by_kind = {r["media_kind"]: r for r in refs}
        self.assertEqual(by_kind["midi"]["latest_version"], 2)
        self.assertEqual(by_kind["media"]["latest_version"], 2)

        upd = ReaperPlugin.update_rpp_references(v1_rpp, proj_root, self.ws)
        self.assertEqual(upd["updated_count"], 2)
        text = v1_rpp.read_text(encoding="utf-8")
        self.assertIn(f"../../{k_b2.primary_file}", text)
        self.assertIn(f"../../{m_b2.primary_file}", text)

        refs_after = ReaperPlugin.find_outdated_rpp_references(v1_rpp, proj_root, self.ws)
        self.assertTrue(all(not r["update_available"] for r in refs_after))


if __name__ == "__main__":
    unittest.main()

