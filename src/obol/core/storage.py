"""Fault-tolerant per-component storage engine, CRC32 event ledger, auto-compaction, and self-healing reconciler."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import tomllib
import uuid
import zlib
from pathlib import Path
from typing import Any, Optional

from obol.core.models import (
    ComponentSnapshot,
    LedgerEvent,
    PublishedDeliverable,
    RepoAsset,
    TrackedFile,
    VersionBundle,
    clean_for_toml,
    utc_now_iso,
)
from obol.core.toml_io import dumps_toml

VERSION_DIR_RE = re.compile(r"^v(\d+)$", re.IGNORECASE)
COMPACTION_THRESHOLD = 100


def normalize_rel_path(path_str: str) -> str:
    return path_str.replace("\\", "/").strip("/")


def component_path_to_slug(component_path: str) -> str:
    norm = normalize_rel_path(component_path)
    return norm.replace("/", "__")


def slug_to_component_path(slug: str) -> str:
    return slug.replace("__", "/")


def safe_rmtree(path: Path) -> None:
    """Removes a file or directory tree safely, clearing read-only attributes first."""
    if not path.exists() and not path.is_symlink():
        return
    if path.is_file() or path.is_symlink():
        try:
            os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
        path.unlink(missing_ok=True)
        return
    for item in path.rglob("*"):
        try:
            os.chmod(item, stat.S_IWRITE | stat.S_IREAD)
        except OSError:
            pass
    try:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=False)


def compute_file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def atomic_write_toml(path: Path, data: dict[str, Any]) -> None:
    """Writes TOML atomically using a .tmp file and rotates existing file to .bak."""
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = clean_for_toml(data)
    content = dumps_toml(cleaned)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    bak_path = path.with_suffix(path.suffix + ".bak")
    tmp_path.write_text(content, encoding="utf-8")
    if path.exists():
        try:
            shutil.copy2(path, bak_path)
        except OSError:
            pass
    os.replace(tmp_path, path)


def load_toml_with_fallback(path: Path) -> Optional[dict[str, Any]]:
    """Loads TOML from path, falling back to path.bak if corrupted or missing."""
    bak_path = path.with_suffix(path.suffix + ".bak")
    for candidate in (path, bak_path):
        if candidate.exists():
            try:
                return tomllib.loads(candidate.read_text(encoding="utf-8"))
            except Exception:
                continue
    return None


def compute_event_crc(event_id: str, ts: str, event_type: str, component: str, payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        {
            "id": event_id,
            "ts": ts,
            "event": event_type,
            "component": component,
            "payload": payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return format(zlib.crc32(canonical) & 0xFFFFFFFF, "08x")


def get_component_meta_dir(project_root: Path, component_path: str) -> Path:
    slug = component_path_to_slug(component_path)
    return project_root / ".obol" / "tasks" / slug


def append_event(
    project_root: Path,
    component_path: str,
    event_type: str,
    payload: dict[str, Any],
) -> LedgerEvent:
    """Appends a CRC32-protected event line to .obol/tasks/<slug>/events.jsonl."""
    meta_dir = get_component_meta_dir(project_root, component_path)
    meta_dir.mkdir(parents=True, exist_ok=True)
    events_path = meta_dir / "events.jsonl"

    evt_id = f"evt_{uuid.uuid4().hex[:12]}"
    ts = utc_now_iso()
    norm_comp = normalize_rel_path(component_path)
    crc = compute_event_crc(evt_id, ts, event_type, norm_comp, payload)

    record = {
        "id": evt_id,
        "ts": ts,
        "event": event_type,
        "component": norm_comp,
        "payload": payload,
        "crc": crc,
    }
    line = json.dumps(record, separators=(",", ":")) + "\n"
    with events_path.open("a", encoding="utf-8") as f:
        f.write(line)
        f.flush()

    return LedgerEvent(
        id=evt_id,
        ts=ts,
        event=event_type,
        component=norm_comp,
        payload=payload,
        crc=crc,
    )


def read_events_fault_tolerant(events_path: Path) -> tuple[list[LedgerEvent], list[str]]:
    """Reads events.jsonl line-by-line, isolating any corrupted or CRC-mismatched line without crashing."""
    events: list[LedgerEvent] = []
    warnings: list[str] = []
    if not events_path.exists():
        return events, warnings

    lines = events_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for idx, raw_line in enumerate(lines, start=1):
        stripped = raw_line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
            if not isinstance(obj, dict):
                raise ValueError("JSON line is not an object")
            evt_id = str(obj["id"])
            ts = str(obj["ts"])
            event_type = str(obj["event"])
            comp = str(obj.get("component", ""))
            payload = dict(obj.get("payload", {}))
            crc = str(obj.get("crc", ""))
            expected_crc = compute_event_crc(evt_id, ts, event_type, comp, payload)
            if crc != expected_crc:
                warnings.append(f"Line {idx}: CRC mismatch (got {crc}, expected {expected_crc}); line skipped.")
                continue
            events.append(
                LedgerEvent(
                    id=evt_id,
                    ts=ts,
                    event=event_type,
                    component=comp,
                    payload=payload,
                    crc=crc,
                )
            )
        except Exception as exc:
            warnings.append(f"Line {idx}: Malformed JSON line ({exc}); line skipped.")
            continue

    return events, warnings


def save_component_snapshot(
    project_root: Path,
    snapshot: ComponentSnapshot,
    compaction_threshold: int = COMPACTION_THRESHOLD,
) -> bool:
    """Saves snapshot.toml atomically and compacts events.jsonl if it exceeds threshold."""
    meta_dir = get_component_meta_dir(project_root, snapshot.component_path)
    meta_dir.mkdir(parents=True, exist_ok=True)
    snap_path = meta_dir / "snapshot.toml"
    atomic_write_toml(snap_path, snapshot.to_dict())

    events_path = meta_dir / "events.jsonl"
    if events_path.exists():
        lines = [ln for ln in events_path.read_text(encoding="utf-8", errors="replace").splitlines() if ln.strip()]
        if len(lines) >= compaction_threshold:
            archive_dir = meta_dir / "archive"
            archive_dir.mkdir(parents=True, exist_ok=True)
            existing_archives = sorted(archive_dir.glob("events_*.jsonl"))
            next_idx = len(existing_archives) + 1
            archive_file = archive_dir / f"events_{next_idx:03d}.jsonl"
            shutil.move(str(events_path), str(archive_file))
            events_path.write_text("", encoding="utf-8")
            return True
    return False


def load_component_snapshot(project_root: Path, component_path: str) -> ComponentSnapshot:
    """Loads snapshot.toml for a component, healing from .bak, events.jsonl, or disk if needed."""
    norm_comp = normalize_rel_path(component_path)
    meta_dir = get_component_meta_dir(project_root, norm_comp)
    snap_path = meta_dir / "snapshot.toml"
    raw = load_toml_with_fallback(snap_path)
    if raw is not None:
        snap = ComponentSnapshot.from_dict(raw)
        if not snap.component_path:
            snap.component_path = norm_comp
        return snap

    return Reconciler.reconcile_component(project_root, norm_comp)


def is_triad_component_dir(folder: Path) -> bool:
    """Rule 1: Any folder containing {working, repo, published} is a Component."""
    return (
        folder.is_dir()
        and (folder / "working").is_dir()
        and (folder / "repo").is_dir()
        and (folder / "published").is_dir()
    )


def discover_project_components(project_root: Path) -> list[str]:
    """Walks the project directory tree to discover all Triad Components at any depth."""
    components: list[str] = []
    if not project_root.exists():
        return components

    tasks_meta_root = project_root / ".obol" / "tasks"
    known_from_meta: set[str] = set()
    if tasks_meta_root.is_dir():
        for slug_dir in tasks_meta_root.iterdir():
            if slug_dir.is_dir():
                raw = load_toml_with_fallback(slug_dir / "snapshot.toml")
                if raw and raw.get("component_path"):
                    known_from_meta.add(normalize_rel_path(str(raw["component_path"])))
                else:
                    known_from_meta.add(slug_to_component_path(slug_dir.name))

    def _walk(curr: Path) -> None:
        if curr.name.startswith(".") or curr.name.startswith("_"):
            return
        if is_triad_component_dir(curr):
            rel = normalize_rel_path(str(curr.relative_to(project_root)))
            components.append(rel)
            return
        try:
            for child in sorted(curr.iterdir()):
                if child.is_dir():
                    _walk(child)
        except OSError:
            pass

    for child in sorted(project_root.iterdir()):
        if child.is_dir() and not child.name.startswith(".") and not child.name.startswith("_"):
            _walk(child)

    all_comps = sorted(set(components) | known_from_meta)
    return all_comps


class Reconciler:
    """Self-healing reconciler that synchronizes snapshot.toml with actual files on disk and ledger events."""

    @staticmethod
    def _scan_version_folder(component_dir: Path, ver_dir: Path, ver_num: int) -> VersionBundle:
        tracked: list[TrackedFile] = []
        primary_rel = ""
        for file_path in sorted(ver_dir.rglob("*")):
            if file_path.is_file() and not file_path.name.startswith(".") and ".backups" not in file_path.parts:
                rel = normalize_rel_path(str(file_path.relative_to(component_dir)))
                sha = compute_file_sha256(file_path)
                size = file_path.stat().st_size
                ftype = file_path.suffix.lstrip(".").lower()
                tracked.append(TrackedFile(rel_path=rel, sha256=sha, size_bytes=size, file_type=ftype))
                if not primary_rel and not file_path.name.endswith((".rpp-bak", ".reapeaks", ".bak")):
                    primary_rel = rel
        if not primary_rel and tracked:
            primary_rel = tracked[0].rel_path
        folder_rel = normalize_rel_path(str(ver_dir.relative_to(component_dir)))
        return VersionBundle(
            version=ver_num,
            folder_rel=folder_rel,
            primary_file=primary_rel,
            files=tracked,
            created_at=utc_now_iso(),
            comment="Discovered on disk by Reconciler",
        )

    @classmethod
    def reconcile_component(cls, project_root: Path, component_path: str) -> ComponentSnapshot:
        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        meta_dir = get_component_meta_dir(project_root, norm_comp)

        raw = load_toml_with_fallback(meta_dir / "snapshot.toml")
        if raw is not None:
            snapshot = ComponentSnapshot.from_dict(raw)
            snapshot.component_path = norm_comp
        else:
            snapshot = ComponentSnapshot(component_path=norm_comp)

        events_to_replay: list[LedgerEvent] = []
        archive_dir = meta_dir / "archive"
        if archive_dir.is_dir():
            for arch_file in sorted(archive_dir.glob("events_*.jsonl")):
                evts, _ = read_events_fault_tolerant(arch_file)
                events_to_replay.extend(evts)
        curr_evts, _ = read_events_fault_tolerant(meta_dir / "events.jsonl")
        events_to_replay.extend(curr_evts)

        for evt in events_to_replay:
            p = evt.payload
            if evt.event == "REPO_INGESTED":
                apath = str(p.get("asset_path", ""))
                if apath and apath not in snapshot.repo_assets:
                    snapshot.repo_assets[apath] = RepoAsset(
                        asset_path=apath,
                        name=apath.split("/")[-1],
                        category=str(p.get("category", "")),
                        author=str(p.get("author", "")),
                        source=str(p.get("source", "")),
                        license=str(p.get("license", "")),
                        tags=list(p.get("tags", [])),
                        custom=dict(p.get("custom", {})),
                    )
            elif evt.event == "REPO_METADATA_UPDATED":
                apath = str(p.get("asset_path", ""))
                if apath in snapshot.repo_assets:
                    asset = snapshot.repo_assets[apath]
                    for field_name in ("author", "source", "license"):
                        if field_name in p:
                            setattr(asset, field_name, str(p[field_name]))
                    if "tags" in p:
                        asset.tags = list(p["tags"])
                    if "custom" in p and isinstance(p["custom"], dict):
                        asset.custom.update(p["custom"])
            elif evt.event in ("FILE_PUBLISHED", "FILE_PUBLISHED_ADDED"):
                dpath = str(p.get("deliverable_path", "")).strip()
                if dpath:
                    deliv = snapshot.published_deliverables.get(dpath)
                    if deliv is None:
                        deliv = PublishedDeliverable(
                            deliverable_path=dpath,
                            name=dpath.split("/")[-1],
                            description=str(p.get("description", "")),
                            tags=list(p.get("tags", [])),
                        )
                        snapshot.published_deliverables[dpath] = deliv
                    ver_num = int(p.get("version", 0))
                    src_w = int(p.get("source_working_version", 0))
                    if ver_num > 0:
                        existing_b = next((v for v in deliv.versions if v.version == ver_num), None)
                        if existing_b is not None and src_w:
                            existing_b.source_working_version = src_w

        working_dir = comp_dir / "working"
        if working_dir.is_dir():
            known_w = {v.version: v for v in snapshot.working_versions}
            max_ver = max(known_w.keys(), default=0)
            for child in sorted(working_dir.iterdir()):
                if child.is_dir():
                    m = VERSION_DIR_RE.match(child.name)
                    if m:
                        ver_num = int(m.group(1))
                        max_ver = max(max_ver, ver_num)
                        if ver_num not in known_w:
                            bundle = cls._scan_version_folder(comp_dir, child, ver_num)
                            snapshot.working_versions.append(bundle)
                            known_w[ver_num] = bundle
                        else:
                            existing_bundle = known_w[ver_num]
                            if any(child.iterdir()):
                                scanned = cls._scan_version_folder(comp_dir, child, ver_num)
                                if scanned.files:
                                    existing_bundle.files = scanned.files
                                    if not existing_bundle.primary_file:
                                        existing_bundle.primary_file = scanned.primary_file

            for child in sorted(working_dir.iterdir()):
                if child.is_file() and not child.name.startswith(".") and not child.name.endswith((".rpp-bak", ".reapeaks")):
                    max_ver += 1
                    new_ver_dir = working_dir / f"v{max_ver:03d}"
                    new_ver_dir.mkdir(parents=True, exist_ok=True)
                    dest = new_ver_dir / child.name
                    shutil.move(str(child), str(dest))
                    bundle = cls._scan_version_folder(comp_dir, new_ver_dir, max_ver)
                    bundle.comment = "Auto-wrapped from in-app save in working/"
                    snapshot.working_versions.append(bundle)
                    append_event(
                        project_root,
                        norm_comp,
                        "WORKING_DISCOVERED",
                        {"version": max_ver, "primary_file": bundle.primary_file},
                    )

        published_dir = comp_dir / "published"
        if published_dir.is_dir():
            known_p = {v.version: v for v in snapshot.published_versions}
            for child in sorted(published_dir.iterdir()):
                if child.is_dir():
                    m = VERSION_DIR_RE.match(child.name)
                    if m:
                        ver_num = int(m.group(1))
                        if ver_num not in known_p:
                            bundle = cls._scan_version_folder(comp_dir, child, ver_num)
                            snapshot.published_versions.append(bundle)
                            known_p[ver_num] = bundle
                        else:
                            scanned = cls._scan_version_folder(comp_dir, child, ver_num)
                            if scanned.files:
                                known_p[ver_num].files = scanned.files
                                if not known_p[ver_num].primary_file:
                                    known_p[ver_num].primary_file = scanned.primary_file

            def _walk_published(curr: Path) -> None:
                if curr.name.startswith("."):
                    return
                ver_children = [
                    (int(m.group(1)), c)
                    for c in sorted(curr.iterdir())
                    if c.is_dir() and (m := VERSION_DIR_RE.match(c.name))
                ]
                if ver_children and curr != published_dir:
                    deliv_rel = normalize_rel_path(str(curr.relative_to(published_dir)))
                    deliv = snapshot.published_deliverables.get(deliv_rel)
                    if deliv is None:
                        deliv = PublishedDeliverable(
                            deliverable_path=deliv_rel,
                            name=deliv_rel.split("/")[-1],
                        )
                        snapshot.published_deliverables[deliv_rel] = deliv
                    known_dv = {v.version: v for v in deliv.versions}
                    for v_num, v_dir in ver_children:
                        if v_num not in known_dv:
                            bundle = cls._scan_version_folder(comp_dir, v_dir, v_num)
                            deliv.versions.append(bundle)
                            known_dv[v_num] = bundle
                        else:
                            scanned = cls._scan_version_folder(comp_dir, v_dir, v_num)
                            if scanned.files:
                                old_meta = {tf.rel_path: tf for tf in known_dv[v_num].files}
                                for tf in scanned.files:
                                    if tf.rel_path in old_meta and old_meta[tf.rel_path].output_label:
                                        tf.output_label = old_meta[tf.rel_path].output_label
                                known_dv[v_num].files = scanned.files
                                if not known_dv[v_num].primary_file:
                                    known_dv[v_num].primary_file = scanned.primary_file
                    deliv.versions.sort(key=lambda x: x.version)
                    deliv.latest_version = deliv.versions[-1].version if deliv.versions else 0
                    return

                for c in sorted(curr.iterdir()):
                    if c.is_dir() and not VERSION_DIR_RE.match(c.name):
                        _walk_published(c)

            _walk_published(published_dir)

        # Recover source_working_version from ledger if missing on reconciled deliverable bundles
        for evt in events_to_replay:
            if evt.event in ("FILE_PUBLISHED", "FILE_PUBLISHED_ADDED"):
                p = evt.payload
                dpath = str(p.get("deliverable_path", "")).strip()
                ver_num = int(p.get("version", 0))
                src_w = int(p.get("source_working_version", 0))
                if dpath and dpath in snapshot.published_deliverables and ver_num > 0:
                    b = next((v for v in snapshot.published_deliverables[dpath].versions if v.version == ver_num), None)
                    if b is not None:
                        if src_w and not b.source_working_version:
                            b.source_working_version = src_w
                        if p.get("comment") and b.comment == "Discovered on disk by Reconciler":
                            b.comment = str(p["comment"])

        repo_dir = comp_dir / "repo"
        if repo_dir.is_dir():
            def _walk_repo(curr: Path) -> None:
                if curr.name.startswith("."):
                    return
                ver_children = [
                    (int(m.group(1)), c)
                    for c in sorted(curr.iterdir())
                    if c.is_dir() and (m := VERSION_DIR_RE.match(c.name))
                ]
                if ver_children and curr != repo_dir:
                    asset_rel = normalize_rel_path(str(curr.relative_to(repo_dir)))
                    parts = asset_rel.split("/")
                    cat = "/".join(parts[:-1]) if len(parts) > 1 else ""
                    asset = snapshot.repo_assets.get(asset_rel)
                    if asset is None:
                        asset = RepoAsset(
                            asset_path=asset_rel,
                            name=parts[-1],
                            category=cat,
                        )
                        snapshot.repo_assets[asset_rel] = asset
                    known_av = {v.version: v for v in asset.versions}
                    for v_num, v_dir in ver_children:
                        if v_num not in known_av:
                            bundle = cls._scan_version_folder(comp_dir, v_dir, v_num)
                            asset.versions.append(bundle)
                            known_av[v_num] = bundle
                        else:
                            scanned = cls._scan_version_folder(comp_dir, v_dir, v_num)
                            if scanned.files:
                                known_av[v_num].files = scanned.files
                                if not known_av[v_num].primary_file:
                                    known_av[v_num].primary_file = scanned.primary_file
                    asset.versions.sort(key=lambda x: x.version)
                    asset.latest_version = asset.versions[-1].version if asset.versions else 0
                    return

                for c in sorted(curr.iterdir()):
                    if c.is_dir():
                        _walk_repo(c)

            _walk_repo(repo_dir)

        snapshot.working_versions.sort(key=lambda x: x.version)
        snapshot.published_versions.sort(key=lambda x: x.version)
        save_component_snapshot(project_root, snapshot)
        return snapshot

    @classmethod
    def reconcile_project(cls, project_root: Path) -> list[ComponentSnapshot]:
        """Reconciles every discovered component under project_root against disk and ledger events."""
        return [
            cls.reconcile_component(project_root, comp)
            for comp in discover_project_components(project_root)
        ]
