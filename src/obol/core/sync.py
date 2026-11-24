"""Stateless Path-Based Server-Client Sync Engine ('Metadata Always Local, Media On-Demand')."""

from __future__ import annotations

import fnmatch
import os
import shutil
from pathlib import Path
from typing import Optional

from obol.core.hub import ObolHub
from obol.core.models import ComponentSnapshot, FilePresence, TrackedFile, VaultStatus
from obol.core.storage import (
    compute_file_sha256,
    discover_project_components,
    load_component_snapshot,
    normalize_rel_path,
)
from obol.plugins.base import unlock_file_writable


class SyncEngine:
    """Implements hash-verified delta backup, archival (eviction), and selective checkout between Local and Server Vault."""

    def __init__(self, hub: ObolHub) -> None:
        self.hub = hub

    def _require_online_vault(self) -> Path:
        status = self.hub.get_vault_status()
        if status == VaultStatus.DISABLED:
            raise RuntimeError("No Server Vault is configured. Run 'obol vault connect <path>' first.")
        if status == VaultStatus.OFFLINE:
            raise RuntimeError(f"Server Vault at '{self.hub.config.vault.path}' is currently offline/unreachable.")
        vp = self.hub.get_vault_path()
        assert vp is not None
        return vp

    def get_vault_project_root(self, project_name: str) -> Path:
        vault_root = self._require_online_vault()
        return vault_root / "projects" / project_name

    def sync_metadata(self, project_name: str) -> None:
        """Mirrors the lightweight .obol/ metadata (~10-50 KB) between Local Workspace and Server Vault."""
        if self.hub.get_vault_status() != VaultStatus.ONLINE:
            return
        entry = self.hub.get_project(project_name)
        if not entry:
            raise KeyError(f"Project '{project_name}' is not registered.")

        local_root = Path(entry.local_path)
        vault_proj = self.get_vault_project_root(entry.name)

        local_obol = local_root / ".obol"
        vault_obol = vault_proj / ".obol"

        # Copy project.toml and tasks/*/*.toml, *.jsonl bidirectionally (newest mtime wins, never copying index.db)
        for src_obol, dst_obol in ((local_obol, vault_obol), (vault_obol, local_obol)):
            if not src_obol.is_dir():
                continue
            for item in src_obol.rglob("*"):
                if item.is_file() and not item.name.startswith("index.db") and not item.name.endswith(".tmp"):
                    rel = item.relative_to(src_obol)
                    target = dst_obol / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.exists() or item.stat().st_mtime > target.stat().st_mtime:
                        shutil.copy2(item, target)

    @staticmethod
    def _atomic_verified_copy(src: Path, dst: Path, expected_sha256: str = "") -> str:
        """Copies src to dst.obol-part, verifies SHA-256 checksum, and atomically replaces dst."""
        dst.parent.mkdir(parents=True, exist_ok=True)
        src_sha = expected_sha256 or compute_file_sha256(src)
        if dst.is_file() and dst.stat().st_size == src.stat().st_size:
            if compute_file_sha256(dst) == src_sha:
                return src_sha

        part_path = dst.with_name(dst.name + ".obol-part")
        shutil.copy2(src, part_path)
        unlock_file_writable(part_path)
        part_sha = compute_file_sha256(part_path)
        if part_sha != src_sha:
            part_path.unlink(missing_ok=True)
            raise IOError(f"Checksum verification failed while copying '{src}' to '{dst}'.")

        if dst.exists():
            unlock_file_writable(dst)
        os.replace(part_path, dst)
        return part_sha

    @staticmethod
    def _matches_filters(rel_project_path: str, path_filters: list[str]) -> bool:
        norm = normalize_rel_path(rel_project_path)
        for pat in path_filters:
            norm_pat = normalize_rel_path(pat)
            if norm_pat in ("*", "**", ""):
                return True
            if norm == norm_pat or norm.startswith(norm_pat.rstrip("*") + "/"):
                return True
            if fnmatch.fnmatch(norm, norm_pat):
                return True
        return False

    def _collect_tracked_files(self, project_root: Path) -> list[tuple[str, TrackedFile]]:
        """Returns list of (project_rel_path, TrackedFile) from all component snapshots."""
        results: list[tuple[str, TrackedFile]] = []
        for comp in discover_project_components(project_root):
            snap = load_component_snapshot(project_root, comp)
            all_bundles = (
                list(snap.working_versions)
                + list(snap.published_versions)
                + [v for d in snap.published_deliverables.values() for v in d.versions]
                + [v for asset in snap.repo_assets.values() for v in asset.versions]
            )
            for bundle in all_bundles:
                for tf in bundle.files:
                    proj_rel = normalize_rel_path(f"{comp}/{tf.rel_path}")
                    results.append((proj_rel, tf))
        return results

    def push_paths(
        self,
        project_name: str,
        path_filters: Optional[list[str]] = None,
        evict_local: bool = False,
    ) -> dict[str, int]:
        """Pushes matching files from Local Workspace to Server Vault; if evict_local=True, frees local space after verifying server SHA-256."""
        filters = path_filters or ["**"]
        entry = self.hub.get_project(project_name)
        if not entry:
            raise KeyError(f"Project '{project_name}' is not registered.")

        local_root = Path(entry.local_path)
        vault_proj = self.get_vault_project_root(entry.name)
        vault_proj.mkdir(parents=True, exist_ok=True)

        # First sync metadata local -> vault
        self.sync_metadata(entry.name)

        pushed_count = 0
        evicted_count = 0
        tracked = self._collect_tracked_files(local_root)

        for proj_rel, tf in tracked:
            if not self._matches_filters(proj_rel, filters):
                continue
            local_file = local_root / proj_rel
            vault_file = vault_proj / proj_rel

            if local_file.is_file():
                actual_local_sha = compute_file_sha256(local_file)
                self._atomic_verified_copy(local_file, vault_file, expected_sha256=actual_local_sha)
                pushed_count += 1

            if evict_local and local_file.is_file():
                # Safety guard: NEVER delete a local file unless the server file exists and matches expected SHA-256
                expected_sha = tf.sha256 or compute_file_sha256(local_file)
                if not vault_file.is_file():
                    raise IOError(f"Refusing to evict '{local_file}': file missing on Server Vault.")
                vault_sha = compute_file_sha256(vault_file)
                if vault_sha != expected_sha:
                    raise IOError(
                        f"Refusing to evict '{local_file}': Server Vault SHA-256 mismatch ({vault_sha} != {expected_sha})."
                    )
                unlock_file_writable(local_file)
                local_file.unlink()
                evicted_count += 1

        self.sync_metadata(entry.name)
        return {"pushed": pushed_count, "evicted": evicted_count}

    def pull_paths(
        self,
        project_name: str,
        path_filters: Optional[list[str]] = None,
    ) -> dict[str, int]:
        """Selectively checks out (pulls) matching files from Server Vault to Local Workspace."""
        filters = path_filters or ["**"]
        entry = self.hub.get_project(project_name)
        if not entry:
            raise KeyError(f"Project '{project_name}' is not registered.")

        local_root = Path(entry.local_path)
        local_root.mkdir(parents=True, exist_ok=True)
        vault_proj = self.get_vault_project_root(entry.name)
        if not vault_proj.is_dir():
            raise FileNotFoundError(f"Project '{project_name}' does not exist on Server Vault.")

        # Always pull .obol/ metadata first so local snapshots are up-to-date
        self.sync_metadata(entry.name)

        pulled_count = 0
        tracked = self._collect_tracked_files(local_root)
        for proj_rel, tf in tracked:
            if not self._matches_filters(proj_rel, filters):
                continue
            vault_file = vault_proj / proj_rel
            local_file = local_root / proj_rel
            if vault_file.is_file():
                self._atomic_verified_copy(vault_file, local_file, expected_sha256=tf.sha256)
                pulled_count += 1

        # Ensure triad skeleton directories exist locally for any checked-out component
        for comp in discover_project_components(local_root):
            if any(self._matches_filters(f"{comp}/working", filters) or self._matches_filters(comp, filters) for _ in (1,)):
                for sub in ("working", "repo", "published"):
                    (local_root / comp / sub).mkdir(parents=True, exist_ok=True)

        return {"pulled": pulled_count}

    def compute_file_presence(self, project_name: str, proj_rel_path: str, expected_sha256: str = "") -> FilePresence:
        """Statelessly computes whether a single file is Local Only, Backed Up, Modified, or On Server."""
        entry = self.hub.get_project(project_name)
        if not entry:
            return FilePresence.REMOTE_ONLY
        local_file = Path(entry.local_path) / proj_rel_path
        local_exists = local_file.is_file()

        if self.hub.get_vault_status() != VaultStatus.ONLINE:
            return FilePresence.LOCAL_ONLY if local_exists else FilePresence.REMOTE_ONLY

        vault_file = self.get_vault_project_root(entry.name) / proj_rel_path
        vault_exists = vault_file.is_file()

        if local_exists and not vault_exists:
            return FilePresence.LOCAL_ONLY
        if not local_exists and vault_exists:
            return FilePresence.REMOTE_ONLY
        if local_exists and vault_exists:
            if local_file.stat().st_size != vault_file.stat().st_size:
                return FilePresence.MODIFIED_LOCAL
            return FilePresence.SYNCED
        return FilePresence.REMOTE_ONLY

    def compute_project_presence(self, project_name: str) -> FilePresence:
        """Rolls up presence across all tracked files in a project."""
        entry = self.hub.get_project(project_name)
        if not entry:
            return FilePresence.REMOTE_ONLY
        local_root = Path(entry.local_path)
        vault_status = self.hub.get_vault_status()

        if vault_status == VaultStatus.ONLINE and not (local_root / ".obol").is_dir():
            self.sync_metadata(entry.name)

        tracked = self._collect_tracked_files(local_root)
        if not tracked:
            if vault_status == VaultStatus.ONLINE and self.get_vault_project_root(entry.name).is_dir():
                return FilePresence.SYNCED if local_root.is_dir() else FilePresence.REMOTE_ONLY
            return FilePresence.LOCAL_ONLY

        states = {self.compute_file_presence(project_name, rel, tf.sha256) for rel, tf in tracked}
        if states == {FilePresence.REMOTE_ONLY}:
            return FilePresence.REMOTE_ONLY
        if states == {FilePresence.SYNCED}:
            return FilePresence.SYNCED
        if states == {FilePresence.LOCAL_ONLY}:
            return FilePresence.LOCAL_ONLY
        if FilePresence.REMOTE_ONLY in states and (FilePresence.SYNCED in states or FilePresence.LOCAL_ONLY in states):
            return FilePresence.SPARSE
        if FilePresence.MODIFIED_LOCAL in states:
            return FilePresence.MODIFIED_LOCAL
        return FilePresence.LOCAL_ONLY

