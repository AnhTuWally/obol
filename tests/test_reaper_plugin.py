"""Unit tests for the Reaper DAW plugin: .rpp path patching and external media scanner/collector."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from obol.core.hub import ObolHub
from obol.core.workspace import ObolWorkspace
from obol.plugins.base import unlock_file_writable


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


if __name__ == "__main__":
    unittest.main()

