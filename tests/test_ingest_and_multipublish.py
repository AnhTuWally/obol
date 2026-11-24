"""Tests for working ingestion, named multi-file-type publishing, export-folder sync, and CLI parity."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from obol.cli.main import run_cli
from obol.core.hub import ObolHub
from obol.core.models import PublishItem
from obol.core.storage import Reconciler, get_component_meta_dir, load_component_snapshot
from obol.core.workspace import ObolWorkspace


class TestIngestAndMultiPublish(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)
        self.proj_root = self.ws.init_project("multi_proj", self.root / "multi_proj", project_template="single_track")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ingest_working_single_file_and_folder_bundle(self) -> None:
        ext_rpp = self.root / "external_sketch.rpp"
        ext_rpp.write_text('<REAPER_PROJECT 0.1 "7.0" 1\n  SAMPLERATE 48000 0 0\n>\n', encoding="utf-8")

        b2 = self.ws.ingest_working_files(
            project_root=self.proj_root,
            component_path="arrangement",
            source_paths=[ext_rpp],
            comment="Dropped sketch rpp",
            rename_to_convention=True,
        )
        self.assertEqual(b2.version, 2)
        self.assertEqual(b2.primary_file, "working/v002/arrangement_v002.rpp")
        rpp_text = (self.proj_root / "arrangement" / b2.primary_file).read_text(encoding="utf-8")
        self.assertIn('RECORD_PATH "../../repo/recorded"', rpp_text)
        self.assertIn('RENDER_FILE "../../published"', rpp_text)

        # Folder bundle ingest keeping original names
        bundle_dir = self.root / "ableton_sketch"
        bundle_dir.mkdir()
        (bundle_dir / "mysong.als").write_bytes(b"ALS-DATA")
        (bundle_dir / "Samples").mkdir()
        (bundle_dir / "Samples" / "kick.wav").write_bytes(b"WAV")

        b3 = self.ws.ingest_working_files(
            project_root=self.proj_root,
            component_path="arrangement",
            source_paths=[bundle_dir],
            rename_to_convention=False,
        )
        self.assertEqual(b3.version, 3)
        self.assertTrue((self.proj_root / "arrangement" / "working" / "v003" / "mysong.als").is_file())
        self.assertTrue((self.proj_root / "arrangement" / "working" / "v003" / "Samples" / "kick.wav").is_file())

    def test_named_deliverables_multi_file_types_and_append(self) -> None:
        wav_file = self.root / "mixdown.wav"
        mid_file = self.root / "chords.mid"
        mp3_file = self.root / "preview.mp3"
        wav_file.write_bytes(b"WAV-DATA")
        mid_file.write_bytes(b"MID-DATA")
        mp3_file.write_bytes(b"MP3-DATA")

        # Publish WAV + MID together into named deliverable "main"
        b1 = self.ws.publish_version(
            project_root=self.proj_root,
            component_path="arrangement",
            source_files=[wav_file, mid_file],
            deliverable_name="main",
            comment="Master WAV + MIDI",
        )
        self.assertEqual(b1.version, 1)
        self.assertEqual(b1.folder_rel, "published/main/v001")
        names_v1 = {Path(tf.rel_path).name for tf in b1.files}
        self.assertEqual(names_v1, {"main_v001.wav", "main_v001.mid"})
        self.assertEqual({tf.file_type for tf in b1.files}, {"wav", "mid"})

        # Append MP3 into existing main/v001 without bumping version
        b1_appended = self.ws.publish_version(
            project_root=self.proj_root,
            component_path="arrangement",
            source_files=[mp3_file],
            deliverable_name="main",
            target_version=1,
        )
        self.assertEqual(b1_appended.version, 1)
        self.assertEqual(
            {Path(tf.rel_path).name for tf in b1_appended.files},
            {"main_v001.wav", "main_v001.mid", "main_v001.mp3"},
        )

        # Separate named deliverable "stems" with two same-extension .wav files
        drums_wav = self.root / "drums.wav"
        bass_wav = self.root / "bass.wav"
        drums_wav.write_bytes(b"DRUMS")
        bass_wav.write_bytes(b"BASS")

        stems_b1 = self.ws.publish_version(
            project_root=self.proj_root,
            component_path="arrangement",
            publish_items=[
                PublishItem(drums_wav, output_label="drums"),
                PublishItem(bass_wav, output_label="bass"),
            ],
            deliverable_name="stems",
            comment="Drum + Bass stems",
        )
        self.assertEqual(stems_b1.folder_rel, "published/stems/v001")
        self.assertEqual(
            {Path(tf.rel_path).name for tf in stems_b1.files},
            {"stems_drums_v001.wav", "stems_bass_v001.wav"},
        )

        # Verify snapshot round-trip and Reconciler recovery
        meta_dir = get_component_meta_dir(self.proj_root, "arrangement")
        (meta_dir / "snapshot.toml").unlink(missing_ok=True)
        (meta_dir / "snapshot.toml.bak").unlink(missing_ok=True)
        healed = Reconciler.reconcile_component(self.proj_root, "arrangement")
        self.assertIn("main", healed.published_deliverables)
        self.assertIn("stems", healed.published_deliverables)
        self.assertEqual(len(healed.published_deliverables["main"].versions[0].files), 3)
        self.assertEqual(healed.published_deliverables["stems"].versions[0].comment, "Drum + Bass stems")

    def test_prepare_and_sync_export_folder_and_cli(self) -> None:
        deliv_path, ver_num, export_dir = self.ws.prepare_publish_export_folder(
            self.proj_root, "mixing", deliverable_name="radio_edit"
        )
        self.assertEqual(deliv_path, "radio_edit")
        self.assertEqual(ver_num, 1)
        (export_dir / "radio.wav").write_bytes(b"WAV")
        (export_dir / "radio.mid").write_bytes(b"MID")

        synced = self.ws.sync_published_folder(
            self.proj_root, "mixing", deliverable_name="radio_edit", version=1, comment="Direct DCC render"
        )
        self.assertIsNotNone(synced)
        assert synced is not None
        self.assertEqual(len(synced.files), 2)

        # CLI work ingest + publish --name --into-version
        cli_rpp = self.root / "cli_work.rpp"
        cli_rpp.write_text("<REAPER_PROJECT\n>\n", encoding="utf-8")
        self.assertEqual(
            run_cli(["work", "ingest", str(cli_rpp), "-t", "mastering", "-p", "multi_proj"], hub=self.hub),
            0,
        )
        snap = load_component_snapshot(self.proj_root, "mastering")
        self.assertEqual(len(snap.working_versions), 2)

        out_wav = self.root / "master_out.wav"
        out_mid = self.root / "master_out.mid"
        out_wav.write_bytes(b"W")
        out_mid.write_bytes(b"M")
        self.assertEqual(
            run_cli(["publish", str(out_wav), "-t", "mastering", "-p", "multi_proj", "--name", "album_master"], hub=self.hub),
            0,
        )
        self.assertEqual(
            run_cli(
                ["publish", str(out_mid), "-t", "mastering", "-p", "multi_proj", "--name", "album_master", "--into-version", "1"],
                hub=self.hub,
            ),
            0,
        )
        snap_m = load_component_snapshot(self.proj_root, "mastering")
        self.assertEqual(len(snap_m.published_deliverables["album_master"].versions[0].files), 2)

    def test_create_named_repo_asset_and_deliverable_before_ingest(self) -> None:
        asset = self.ws.create_repo_asset(self.proj_root, "arrangement", "audio/lead_synth")
        self.assertEqual(asset.asset_path, "audio/lead_synth")
        self.assertEqual(asset.latest_version, 0)
        self.assertTrue((self.proj_root / "arrangement" / "repo" / "audio" / "lead_synth").is_dir())

        synth_wav = self.root / "raw_take_09.wav"
        synth_wav.write_bytes(b"SYNTH")
        asset_v1, b1 = self.ws.ingest_repo_asset(
            self.proj_root, "arrangement", synth_wav, asset_path=asset.asset_path
        )
        self.assertEqual(b1.version, 1)
        self.assertEqual(asset_v1.latest_version, 1)

        deliv = self.ws.create_published_deliverable(self.proj_root, "arrangement", "karaoke_mix")
        self.assertEqual(deliv.deliverable_path, "karaoke_mix")
        self.assertEqual(deliv.latest_version, 0)
        self.assertTrue((self.proj_root / "arrangement" / "published" / "karaoke_mix").is_dir())

        k_wav = self.root / "k.wav"
        k_mid = self.root / "k.mid"
        k_wav.write_bytes(b"KW")
        k_mid.write_bytes(b"KM")
        pub_b1 = self.ws.publish_version(
            self.proj_root, "arrangement", source_files=[k_wav, k_mid], deliverable_name=deliv.deliverable_path
        )
        self.assertEqual(pub_b1.folder_rel, "published/karaoke_mix/v001")
        self.assertEqual(len(pub_b1.files), 2)


if __name__ == "__main__":
    unittest.main()
