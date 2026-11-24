"""Unit tests for fault-tolerant storage, CRC32 line-level isolation, 100-line auto-compaction, and self-healing reconciler."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from obol.core.hub import ObolHub
from obol.core.index import ProjectIndex
from obol.core.storage import (
    Reconciler,
    append_event,
    get_component_meta_dir,
    load_component_snapshot,
    read_events_fault_tolerant,
    save_component_snapshot,
)
from obol.core.workspace import ObolWorkspace


class TestStorageResilience(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)
        self.proj_root = self.ws.init_project("resilience_proj", self.root / "resilience_proj")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_corrupted_event_line_is_isolated_without_crashing(self) -> None:
        append_event(self.proj_root, "audio", "CUSTOM_TEST", {"step": 1})
        meta_dir = get_component_meta_dir(self.proj_root, "audio")
        events_file = meta_dir / "events.jsonl"

        # Inject a corrupted JSON line and a tampered CRC line in the middle
        with events_file.open("a", encoding="utf-8") as f:
            f.write("{corrupted_json_line_here\n")
            f.write('{"id":"evt_bad","ts":"2026-11-23T00:00:00+00:00","event":"TAMPERED","component":"audio","payload":{},"crc":"00000000"}\n')

        # Append a valid event after the corrupted lines
        append_event(self.proj_root, "audio", "CUSTOM_TEST", {"step": 2})

        events, warnings = read_events_fault_tolerant(events_file)
        self.assertEqual(len(warnings), 2)
        custom_steps = [e.payload.get("step") for e in events if e.event == "CUSTOM_TEST"]
        self.assertEqual(custom_steps, [1, 2])

    def test_auto_compaction_at_threshold(self) -> None:
        for i in range(15):
            append_event(self.proj_root, "audio", "PING", {"idx": i})

        snap = load_component_snapshot(self.proj_root, "audio")
        compacted = save_component_snapshot(self.proj_root, snap, compaction_threshold=10)
        self.assertTrue(compacted)

        meta_dir = get_component_meta_dir(self.proj_root, "audio")
        archives = list((meta_dir / "archive").glob("events_*.jsonl"))
        self.assertEqual(len(archives), 1)
        remaining_events, _ = read_events_fault_tolerant(meta_dir / "events.jsonl")
        self.assertEqual(len(remaining_events), 0)

    def test_self_healing_reconciler_and_disposable_sqlite_rebuild(self) -> None:
        # Ingest a repo asset with provenance
        sample = self.root / "kick.wav"
        sample.write_bytes(b"RIFF-WAVE-KICK")
        self.ws.ingest_repo_asset(
            self.proj_root,
            "audio",
            sample,
            asset_path="drums/kick",
            author="Dan Choi",
            source_info="Drum Session",
            license_info="Original",
        )

        # Manually drop a v002 working folder and a new repo asset version on disk via Explorer simulation
        manual_w2 = self.proj_root / "audio" / "working" / "v002"
        manual_w2.mkdir(parents=True, exist_ok=True)
        (manual_w2 / "audio_v002.rpp").write_text("<REAPER_PROJECT\n>\n", encoding="utf-8")

        # Delete both snapshot.toml, snapshot.toml.bak, and index.db to simulate total metadata loss
        meta_dir = get_component_meta_dir(self.proj_root, "audio")
        (meta_dir / "snapshot.toml").unlink(missing_ok=True)
        (meta_dir / "snapshot.toml.bak").unlink(missing_ok=True)
        index = ProjectIndex(self.proj_root)
        index.db_path.unlink(missing_ok=True)

        # Reconcile and verify full self-healing from disk + events.jsonl
        healed = Reconciler.reconcile_component(self.proj_root, "audio")
        self.assertEqual([v.version for v in healed.working_versions], [1, 2])
        self.assertIn("drums/kick", healed.repo_assets)
        self.assertEqual(healed.repo_assets["drums/kick"].author, "Dan Choi")

        # Rebuild SQLite index and verify query
        index.rebuild_all()
        rows = index.search_repo_assets(author="Dan Choi")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["asset_path"], "drums/kick")


if __name__ == "__main__":
    unittest.main()
