"""Unit tests for the Optional Server-Client Vault architecture: Standalone Local to Vault onboarding, multi-project archival, and granular selective checkout."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from obol.core.hub import ObolHub
from obol.core.models import FilePresence, VaultStatus
from obol.core.sync import SyncEngine
from obol.core.workspace import ObolWorkspace


class TestServerClientSync(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.hub = ObolHub(hub_dir=self.root / "hub")
        self.ws = ObolWorkspace(self.hub)
        self.sync = SyncEngine(self.hub)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_optional_vault_multi_project_archive_and_granular_checkout(self) -> None:
        # 1. Start in Standalone Local Mode (no vault connected)
        self.assertEqual(self.hub.get_vault_status(), VaultStatus.DISABLED)

        # Create 3 local projects
        p1 = self.ws.init_project("proj_1", self.root / "local" / "proj_1")
        p2 = self.ws.init_project("proj_2", self.root / "local" / "proj_2")
        p3 = self.ws.init_project("proj_3", self.root / "local" / "proj_3")

        # Add v001 and v002 repo assets + working v002 to proj_1
        s1 = self.root / "v1.wav"
        s1.write_bytes(b"VOCAL-V1-DATA")
        s2 = self.root / "v2.wav"
        s2.write_bytes(b"VOCAL-V2-DATA")
        self.ws.ingest_repo_asset(p1, "audio", s1, asset_path="audio/lead_vocal", author="Sarah")
        self.ws.ingest_repo_asset(p1, "audio", s2, asset_path="audio/lead_vocal", author="Sarah")
        self.ws.version_up_working(p1, "audio", comment="Working v002")

        # 2. Connect an SMB / Filesystem Server Vault later on
        vault_dir = self.root / "smb_server_vault"
        status = self.hub.connect_vault(vault_dir)
        self.assertEqual(status, VaultStatus.ONLINE)

        # 3. Send 2 projects (proj_1 and proj_2) to the Server Vault for archival while keeping proj_3 local
        res1 = self.sync.push_paths("proj_1", ["**"], evict_local=True)
        res2 = self.sync.push_paths("proj_2", ["**"], evict_local=True)
        self.assertGreater(res1["pushed"], 0)
        self.assertEqual(res1["pushed"], res1["evicted"])
        self.assertGreater(res2["evicted"], 0)

        self.assertEqual(self.sync.compute_project_presence("proj_1"), FilePresence.REMOTE_ONLY)
        self.assertEqual(self.sync.compute_project_presence("proj_2"), FilePresence.REMOTE_ONLY)
        self.assertEqual(self.sync.compute_project_presence("proj_3"), FilePresence.LOCAL_ONLY)

        # Verify local media is gone for proj_1, while .obol/ metadata is still locally browsable!
        v1_local_wav = p1 / "audio/repo/audio/lead_vocal/v001/lead_vocal_v001.wav"
        v2_local_wav = p1 / "audio/repo/audio/lead_vocal/v002/lead_vocal_v002.wav"
        self.assertFalse(v1_local_wav.exists())
        self.assertFalse(v2_local_wav.exists())
        self.assertTrue((p1 / ".obol/tasks/audio/snapshot.toml").is_file())

        # 4. Granular Version-Level Selective Checkout: pull ONLY v002 of lead_vocal from proj_1
        self.sync.pull_paths("proj_1", ["audio/repo/audio/lead_vocal/v002/**"])
        self.assertTrue(v2_local_wav.is_file())
        self.assertFalse(v1_local_wav.exists())
        self.assertEqual(self.sync.compute_project_presence("proj_1"), FilePresence.SPARSE)

        # 5. Component-Level Selective Checkout on proj_2: pull ONLY audio/**, leaving video/** on server
        self.sync.pull_paths("proj_2", ["audio/**"])
        self.assertTrue((p2 / "audio/working/v001/audio_v001.rpp").is_file())
        self.assertFalse((p2 / "video/working/v001/video_v001.md").exists())
        self.assertEqual(self.sync.compute_project_presence("proj_2"), FilePresence.SPARSE)

        # 6. Full Project Checkout on proj_1
        self.sync.pull_paths("proj_1", ["**"])
        self.assertTrue(v1_local_wav.is_file())
        self.assertEqual(self.sync.compute_project_presence("proj_1"), FilePresence.SYNCED)

    def test_select_and_remember_client_root_across_sessions(self) -> None:
        custom_client_root = self.root / "my_remembered_client_root"
        self.hub.set_client_root(custom_client_root)
        self.assertEqual(self.hub.get_client_root(), custom_client_root.resolve())

        # Create a project inside the client root and set it as last active
        self.ws.init_project("remembered_proj", custom_client_root / "remembered_proj")
        self.hub.set_last_active_project("remembered_proj")

        # Simulate restarting the app by instantiating a brand new ObolHub pointing to the same hub_dir
        reloaded_hub = ObolHub(hub_dir=self.root / "hub")
        self.assertEqual(reloaded_hub.get_client_root(), custom_client_root.resolve())
        self.assertEqual(reloaded_hub.config.last_active_project, "remembered_proj")
        self.assertIsNotNone(reloaded_hub.get_project("remembered_proj"))


if __name__ == "__main__":
    unittest.main()
