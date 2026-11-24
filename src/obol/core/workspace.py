"""Core Pipeline Workspace API: project init, arbitrary-depth components, working versioning, repo ingestion, publishing, and credits."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import os
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from obol.core.hub import ObolHub
from obol.core.index import ProjectIndex
from obol.core.models import (
    ComponentSnapshot,
    ProjectConfig,
    PublishedDeliverable,
    PublishItem,
    RepoAsset,
    TrackedFile,
    VersionBundle,
    VersionUpStrategy,
    utc_now_iso,
)
from obol.core.storage import (
    Reconciler,
    append_event,
    atomic_write_toml,
    compute_file_sha256,
    discover_project_components,
    get_component_meta_dir,
    load_component_snapshot,
    load_toml_with_fallback,
    normalize_rel_path,
    safe_rmtree,
    save_component_snapshot,
)
from obol.plugins.base import (
    ExternalDependency,
    TaskContext,
    execute_version_up,
    launch_file_with_profile,
    lock_file_readonly,
    matches_any_pattern,
    unlock_file_writable,
)
from obol.plugins.reaper import ReaperPlugin


@dataclass
class WorkingFileContext:
    """Resolved Obol project, component, and working version context for an active DCC file."""

    project_name: str
    project_root: Path
    component_path: str
    working_version: int
    working_file: Path
    snapshot: ComponentSnapshot


class ObolWorkspace:
    """High-level facade for all Obol project, component, working, repo, and publish operations."""

    def __init__(self, hub: Optional[ObolHub] = None) -> None:
        self.hub = hub or ObolHub()

    def init_project(
        self,
        name: str,
        root_path: Path,
        project_template: str = "simple_media",
        code: Optional[str] = None,
        custom_components: Optional[list[str]] = None,
        seed_initial_working: bool = True,
        component_task_overrides: Optional[dict[str, str]] = None,
        component_seed_overrides: Optional[dict[str, str]] = None,
    ) -> Path:
        """Initializes a project from a Tier-1 Project Template, creating components and seed v001 files."""
        project_root = root_path.resolve()
        project_root.mkdir(parents=True, exist_ok=True)
        obol_dir = project_root / ".obol"
        obol_dir.mkdir(parents=True, exist_ok=True)
        (obol_dir / "tasks").mkdir(parents=True, exist_ok=True)

        pt = self.hub.template_manager.get_project_template(project_template)
        pcode = code or re.sub(r"[^a-zA-Z0-9]", "", name)[:4].lower() or "prj"
        pcfg = ProjectConfig(
            name=name,
            code=pcode,
            template_used=pt.name,
            shared_folders=pt.shared_folders,
        )
        atomic_write_toml(obol_dir / "project.toml", pcfg.to_dict())

        for shared in pt.shared_folders:
            (project_root / shared).mkdir(parents=True, exist_ok=True)

        task_overrides = {normalize_rel_path(k): v for k, v in (component_task_overrides or {}).items()}
        seed_overrides = {normalize_rel_path(k): v for k, v in (component_seed_overrides or {}).items()}

        components = custom_components if custom_components is not None else pt.default_components
        for comp_path in components:
            norm_comp = normalize_rel_path(comp_path)
            if norm_comp in task_overrides:
                tt = self.hub.template_manager.get_task_template(task_overrides[norm_comp])
            else:
                tt = self.hub.template_manager.resolve_task_template_for_component(pt, comp_path)
            self.create_component(
                project_root=project_root,
                component_path=comp_path,
                task_template=tt.name,
                seed_initial_working=seed_initial_working,
                seed_override=seed_overrides.get(norm_comp),
            )

        self.hub.register_project(project_root, name=name, code=pcode, template_used=pt.name)
        ProjectIndex(project_root).rebuild_all()
        return project_root

    def create_component(
        self,
        project_root: Path,
        component_path: str,
        task_template: str = "reaper_daw",
        software_override: Optional[str] = None,
        seed_initial_working: bool = True,
        seed_override: Optional[str] = None,
    ) -> ComponentSnapshot:
        """Creates a Triad Component ({working, repo, published}) at any hierarchy depth."""
        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        for triad_sub in ("working", "repo", "published"):
            (comp_dir / triad_sub).mkdir(parents=True, exist_ok=True)

        tt = self.hub.template_manager.get_task_template(task_template)
        sw_key = software_override or tt.software
        snapshot = ComponentSnapshot(
            component_path=norm_comp,
            task_template=tt.name,
            software=sw_key,
            repo_routes=tt.repo_routes,
        )
        evt = append_event(
            project_root,
            norm_comp,
            "COMPONENT_CREATED",
            {"task_template": tt.name, "software": sw_key},
        )
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)

        if seed_initial_working:
            used_seed_name = (
                seed_override
                if seed_override and self.hub.template_manager.get_seed_file_path(seed_override)
                else tt.seed_file_template
            )
            self.version_up_working(
                project_root=project_root,
                component_path=norm_comp,
                comment=f"Initial v001 from seed template '{used_seed_name}'",
                strategy=VersionUpStrategy.FROM_SEED_TEMPLATE,
                seed_file_override=seed_override,
            )
            snapshot = load_component_snapshot(project_root, norm_comp)

        ProjectIndex(project_root).index_component(snapshot)
        return snapshot

    def version_up_working(
        self,
        project_root: Path,
        component_path: str,
        comment: str = "",
        strategy: Optional[VersionUpStrategy] = None,
        source_version: Optional[int] = None,
        tags: Optional[list[str]] = None,
        seed_file_override: Optional[str] = None,
    ) -> VersionBundle:
        """Creates the next working/vNNN/ version folder using the component's configured Version-Up Strategy."""
        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)
        profile = self.hub.get_software_profile(snapshot.software)
        plugin = self.hub.plugin_registry.get(profile.plugin)

        tt = self.hub.template_manager.get_task_template(snapshot.task_template)
        seed_path = (
            self.hub.template_manager.get_seed_file_path(seed_file_override)
            if seed_file_override
            else None
        ) or self.hub.template_manager.get_seed_file_path(tt.seed_file_template)

        existing_vers = {v.version: v for v in snapshot.working_versions}
        next_ver_num = max(existing_vers.keys(), default=0) + 1
        target_ver_dir = comp_dir / "working" / f"v{next_ver_num:03d}"

        parent_bundle: Optional[VersionBundle] = None
        if existing_vers:
            src_v = source_version if (source_version and source_version in existing_vers) else max(existing_vers.keys())
            parent_bundle = existing_vers[src_v]

        src_ver_dir = (comp_dir / parent_bundle.folder_rel) if parent_bundle else None
        src_primary = (comp_dir / parent_bundle.primary_file) if (parent_bundle and parent_bundle.primary_file) else None

        ctx = TaskContext(
            project_root=project_root,
            component_path=norm_comp,
            component_dir=comp_dir,
            software_profile=profile,
        )

        new_primary_abs = execute_version_up(
            ctx=ctx,
            plugin=plugin,
            source_version_dir=src_ver_dir,
            source_primary_file=src_primary,
            target_version_num=next_ver_num,
            target_version_dir=target_ver_dir,
            strategy=strategy,
            seed_file_path=seed_path,
        )

        tracked: list[TrackedFile] = []
        for fpath in sorted(target_ver_dir.rglob("*")):
            if fpath.is_file() and not fpath.name.startswith(".") and ".backups" not in fpath.parts:
                rel = normalize_rel_path(str(fpath.relative_to(comp_dir)))
                tracked.append(
                    TrackedFile(
                        rel_path=rel,
                        sha256=compute_file_sha256(fpath),
                        size_bytes=fpath.stat().st_size,
                        file_type=fpath.suffix.lstrip(".").lower(),
                    )
                )

        primary_rel = normalize_rel_path(str(new_primary_abs.relative_to(comp_dir)))
        folder_rel = normalize_rel_path(str(target_ver_dir.relative_to(comp_dir)))
        bundle = VersionBundle(
            version=next_ver_num,
            folder_rel=folder_rel,
            primary_file=primary_rel,
            files=tracked,
            created_at=utc_now_iso(),
            comment=comment or f"Working version v{next_ver_num:03d}",
            parent_version=parent_bundle.version if parent_bundle else 0,
            tags=list(tags or []),
        )
        snapshot.working_versions.append(bundle)

        # Update used_by_working_versions via plugin dependency scan
        _, used_repo_paths = plugin.inspect_dependencies(new_primary_abs, ctx)
        for rpath in used_repo_paths:
            if rpath in snapshot.repo_assets:
                used_list = snapshot.repo_assets[rpath].used_by_working_versions
                if next_ver_num not in used_list:
                    used_list.append(next_ver_num)

        evt = append_event(
            project_root,
            norm_comp,
            "WORKING_VERSIONED",
            {
                "version": next_ver_num,
                "parent_version": bundle.parent_version,
                "folder_rel": folder_rel,
                "primary_file": primary_rel,
                "strategy": (strategy or profile.version_up_strategy).value,
                "comment": bundle.comment,
            },
        )
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)
        ProjectIndex(project_root).index_component(snapshot)
        return bundle

    def launch_working_version(
        self,
        project_root: Path,
        component_path: str,
        version: Optional[int] = None,
    ) -> Path:
        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)
        if not snapshot.working_versions:
            raise FileNotFoundError(f"Component '{norm_comp}' has no working versions.")

        bundle = (
            next((v for v in snapshot.working_versions if v.version == version), None)
            if version is not None
            else snapshot.working_versions[-1]
        )
        if bundle is None:
            raise FileNotFoundError(f"Working version v{version:03d} not found in '{norm_comp}'.")

        target_file = comp_dir / bundle.primary_file
        if not target_file.is_file():
            raise FileNotFoundError(
                f"Working file '{bundle.primary_file}' is not checked out locally. Check it out from the Server Vault first."
            )
        profile = self.hub.get_software_profile(snapshot.software)
        launch_file_with_profile(target_file, profile)
        return target_file

    def ingest_working_files(
        self,
        project_root: Path,
        component_path: str,
        source_paths: list[Path],
        comment: str = "",
        tags: Optional[list[str]] = None,
        copy_mode: str = "copy",
        rename_to_convention: bool = True,
    ) -> VersionBundle:
        """Ingests external working file(s) or a folder bundle into the next working/vNNN/ version folder."""
        if not source_paths:
            raise ValueError("At least one working file or folder must be provided to ingest.")
        for sp in source_paths:
            if not sp.exists():
                raise FileNotFoundError(f"Working source path does not exist: {sp}")

        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)
        profile = self.hub.get_software_profile(snapshot.software)
        plugin = self.hub.plugin_registry.get(profile.plugin)

        existing_vers = {v.version: v for v in snapshot.working_versions}
        next_ver_num = max(existing_vers.keys(), default=0) + 1
        ver_label = f"v{next_ver_num:03d}"
        target_ver_dir = comp_dir / "working" / ver_label
        target_ver_dir.mkdir(parents=True, exist_ok=True)

        prev_bundle = existing_vers[max(existing_vers.keys())] if existing_vers else None
        prev_ver_dir = (comp_dir / prev_bundle.folder_rel) if prev_bundle else None

        comp_leaf = norm_comp.split("/")[-1]
        preferred_exts = {e.lower() for e in profile.extensions}
        copied_files: list[Path] = []

        def _transfer(src: Path, dst: Path) -> None:
            dst.parent.mkdir(parents=True, exist_ok=True)
            if copy_mode == "move":
                shutil.move(str(src), str(dst))
            else:
                shutil.copy2(src, dst)
            unlock_file_writable(dst)

        for sp in source_paths:
            if sp.is_dir():
                for item in sorted(sp.rglob("*")):
                    rel = item.relative_to(sp).as_posix()
                    if matches_any_pattern(rel, item.name, profile.ignore_patterns):
                        continue
                    dest_item = target_ver_dir / (rel if len(source_paths) == 1 else f"{sp.name}/{rel}")
                    if item.is_dir():
                        dest_item.mkdir(parents=True, exist_ok=True)
                    elif item.is_file():
                        _transfer(item, dest_item)
                        copied_files.append(dest_item)
            elif sp.is_file():
                dest_file = target_ver_dir / sp.name
                _transfer(sp, dest_file)
                copied_files.append(dest_file)

        if not copied_files:
            raise ValueError("No ingestible files were found in the selected source path(s).")

        primary_abs = next(
            (f for f in copied_files if f.suffix.lower() in preferred_exts),
            copied_files[0],
        )

        if rename_to_convention:
            desired_name = f"{comp_leaf}_{ver_label}{primary_abs.suffix}"
            desired_path = primary_abs.parent / desired_name
            if desired_path != primary_abs:
                if desired_path.exists():
                    unlock_file_writable(desired_path)
                    desired_path.unlink()
                os.replace(primary_abs, desired_path)
                primary_abs = desired_path

        if profile.lock_previous_versions and prev_ver_dir and prev_ver_dir.is_dir():
            for old_file in prev_ver_dir.rglob("*"):
                if old_file.is_file():
                    lock_file_readonly(old_file)

        ctx = TaskContext(
            project_root=project_root,
            component_path=norm_comp,
            component_dir=comp_dir,
            software_profile=profile,
        )
        plugin.on_version_created(target_ver_dir, primary_abs, prev_ver_dir, ctx)

        tracked: list[TrackedFile] = []
        for fpath in sorted(target_ver_dir.rglob("*")):
            if fpath.is_file() and not fpath.name.startswith(".") and ".backups" not in fpath.parts:
                rel = normalize_rel_path(str(fpath.relative_to(comp_dir)))
                tracked.append(
                    TrackedFile(
                        rel_path=rel,
                        sha256=compute_file_sha256(fpath),
                        size_bytes=fpath.stat().st_size,
                        file_type=fpath.suffix.lstrip(".").lower(),
                    )
                )

        primary_rel = normalize_rel_path(str(primary_abs.relative_to(comp_dir)))
        folder_rel = normalize_rel_path(str(target_ver_dir.relative_to(comp_dir)))
        src_summary = source_paths[0].name if len(source_paths) == 1 else f"{len(source_paths)} items"
        bundle = VersionBundle(
            version=next_ver_num,
            folder_rel=folder_rel,
            primary_file=primary_rel,
            files=tracked,
            created_at=utc_now_iso(),
            comment=comment or f"Ingested working {src_summary}",
            parent_version=prev_bundle.version if prev_bundle else 0,
            tags=list(tags or []),
        )
        snapshot.working_versions.append(bundle)

        _, used_repo_paths = plugin.inspect_dependencies(primary_abs, ctx)
        for rpath in used_repo_paths:
            if rpath in snapshot.repo_assets:
                used_list = snapshot.repo_assets[rpath].used_by_working_versions
                if next_ver_num not in used_list:
                    used_list.append(next_ver_num)

        evt = append_event(
            project_root,
            norm_comp,
            "WORKING_INGESTED",
            {
                "version": next_ver_num,
                "parent_version": bundle.parent_version,
                "folder_rel": folder_rel,
                "primary_file": primary_rel,
                "comment": bundle.comment,
            },
        )
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)
        ProjectIndex(project_root).index_component(snapshot)
        return bundle

    def _auto_route_repo_asset_path(
        self,
        snapshot: ComponentSnapshot,
        source_file: Path,
        explicit_asset_path: Optional[str] = None,
    ) -> tuple[str, str, str]:
        """Returns (asset_path, category, asset_name) inside repo/."""
        if explicit_asset_path:
            norm = normalize_rel_path(explicit_asset_path)
            parts = norm.split("/")
            cat = "/".join(parts[:-1]) if len(parts) > 1 else ""
            return norm, cat, parts[-1]

        raw_stem = re.sub(r"_v\d+$", "", source_file.stem, flags=re.IGNORECASE)
        slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_stem).strip("_").lower() or "asset"
        ext = source_file.suffix.lower()

        matched_category = ""
        for cat, exts in snapshot.repo_routes.items():
            if ext in [e.lower() for e in exts]:
                matched_category = normalize_rel_path(cat)
                break

        asset_path = f"{matched_category}/{slug}" if matched_category else slug
        return asset_path, matched_category, slug

    def create_repo_asset(
        self,
        project_root: Path,
        component_path: str,
        asset_path: str,
        author: str = "",
        source_info: str = "",
        license_info: str = "",
        tags: Optional[list[str]] = None,
    ) -> RepoAsset:
        """Creates a named empty RepoAsset container in repo/<asset_path>/ before files are ingested."""
        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)

        parts = [
            re.sub(r"[^a-zA-Z0-9_-]+", "_", p).strip("_").lower()
            for p in normalize_rel_path(asset_path).split("/")
            if p.strip()
        ]
        parts = [p for p in parts if p]
        if not parts:
            raise ValueError("Asset Name is required.")
        norm_asset_path = "/".join(parts)
        category = "/".join(parts[:-1]) if len(parts) > 1 else ""
        asset_name = parts[-1]

        (comp_dir / "repo" / norm_asset_path).mkdir(parents=True, exist_ok=True)
        asset = snapshot.repo_assets.get(norm_asset_path)
        if asset is None:
            asset = RepoAsset(
                asset_path=norm_asset_path,
                name=asset_name,
                category=category,
                author=author or self.hub.config.author_name,
                source=source_info,
                license=license_info or "Unspecified",
                tags=list(tags or []),
                latest_version=0,
            )
            snapshot.repo_assets[norm_asset_path] = asset
            evt = append_event(
                project_root,
                norm_comp,
                "REPO_ASSET_CREATED",
                {
                    "asset_path": norm_asset_path,
                    "category": category,
                    "author": asset.author,
                    "source": asset.source,
                    "license": asset.license,
                    "tags": asset.tags,
                },
            )
            snapshot.last_event_id = evt.id
            save_component_snapshot(project_root, snapshot)
            ProjectIndex(project_root).index_component(snapshot)
        return asset

    def create_published_deliverable(
        self,
        project_root: Path,
        component_path: str,
        deliverable_name: str,
        description: str = "",
        tags: Optional[list[str]] = None,
    ) -> PublishedDeliverable:
        """Creates a named empty PublishedDeliverable container in published/<deliverable_name>/ before publishing."""
        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)

        deliv_path = self.normalize_deliverable_name(deliverable_name)
        deliv_leaf = deliv_path.split("/")[-1]
        (comp_dir / "published" / deliv_path).mkdir(parents=True, exist_ok=True)
        deliv = snapshot.published_deliverables.get(deliv_path)
        if deliv is None:
            deliv = PublishedDeliverable(
                deliverable_path=deliv_path,
                name=deliv_leaf,
                description=description,
                tags=list(tags or []),
                latest_version=0,
            )
            snapshot.published_deliverables[deliv_path] = deliv
            evt = append_event(
                project_root,
                norm_comp,
                "DELIVERABLE_CREATED",
                {
                    "deliverable_path": deliv_path,
                    "description": description,
                    "tags": deliv.tags,
                },
            )
            snapshot.last_event_id = evt.id
            save_component_snapshot(project_root, snapshot)
            ProjectIndex(project_root).index_component(snapshot)
        return deliv

    def ingest_repo_asset(
        self,
        project_root: Path,
        component_path: str,
        source_file: Path,
        asset_path: Optional[str] = None,
        author: str = "",
        source_info: str = "",
        license_info: str = "",
        tags: Optional[list[str]] = None,
        comment: str = "",
        custom: Optional[dict[str, Any]] = None,
        copy_mode: str = "copy",
    ) -> tuple[RepoAsset, VersionBundle]:
        """Ingests an external file into a versioned Repo Asset container (repo/<asset_path>/vNNN/) with provenance metadata."""
        if not source_file.exists():
            raise FileNotFoundError(f"Source file to ingest does not exist: {source_file}")

        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)

        resolved_asset_path, category, asset_name = self._auto_route_repo_asset_path(
            snapshot, source_file, explicit_asset_path=asset_path
        )

        asset = snapshot.repo_assets.get(resolved_asset_path)
        if asset is None:
            asset = RepoAsset(
                asset_path=resolved_asset_path,
                name=asset_name,
                category=category,
                author=author or self.hub.config.author_name,
                source=source_info or str(source_file.resolve()),
                license=license_info or "Unspecified",
                tags=list(tags or []),
                custom=dict(custom or {}),
                latest_version=0,
            )
            snapshot.repo_assets[resolved_asset_path] = asset
        else:
            if author:
                asset.author = author
            if source_info:
                asset.source = source_info
            if license_info:
                asset.license = license_info
            if tags:
                for t in tags:
                    if t not in asset.tags:
                        asset.tags.append(t)
            if custom:
                asset.custom.update(custom)

        next_ver = max((v.version for v in asset.versions), default=0) + 1
        ver_label = f"v{next_ver:03d}"
        ver_dir = comp_dir / "repo" / resolved_asset_path / ver_label
        ver_dir.mkdir(parents=True, exist_ok=True)

        dest_filename = f"{asset_name}_{ver_label}{source_file.suffix}"
        dest_file = ver_dir / dest_filename

        if copy_mode == "move":
            shutil.move(str(source_file), str(dest_file))
        elif copy_mode == "hardlink":
            try:
                os.link(source_file, dest_file)
            except OSError:
                shutil.copy2(source_file, dest_file)
        else:
            shutil.copy2(source_file, dest_file)

        sha = compute_file_sha256(dest_file)
        size = dest_file.stat().st_size
        rel_file = normalize_rel_path(str(dest_file.relative_to(comp_dir)))
        folder_rel = normalize_rel_path(str(ver_dir.relative_to(comp_dir)))

        bundle = VersionBundle(
            version=next_ver,
            folder_rel=folder_rel,
            primary_file=rel_file,
            files=[
                TrackedFile(
                    rel_path=rel_file,
                    sha256=sha,
                    size_bytes=size,
                    file_type=dest_file.suffix.lstrip(".").lower(),
                )
            ],
            created_at=utc_now_iso(),
            comment=comment or f"Ingested from {source_file.name}",
            parent_version=next_ver - 1 if next_ver > 1 else 0,
            tags=list(tags or []),
            custom=dict(custom or {}),
        )
        asset.versions.append(bundle)
        asset.latest_version = next_ver

        evt = append_event(
            project_root,
            norm_comp,
            "REPO_INGESTED",
            {
                "asset_path": resolved_asset_path,
                "category": category,
                "version": next_ver,
                "primary_file": rel_file,
                "sha256": sha,
                "author": asset.author,
                "source": asset.source,
                "license": asset.license,
                "tags": asset.tags,
                "custom": asset.custom,
                "comment": bundle.comment,
            },
        )
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)
        ProjectIndex(project_root).index_component(snapshot)
        return asset, bundle

    def update_repo_metadata(
        self,
        project_root: Path,
        component_path: str,
        asset_path: str,
        author: Optional[str] = None,
        source_info: Optional[str] = None,
        license_info: Optional[str] = None,
        tags: Optional[list[str]] = None,
        comment: Optional[str] = None,
        version: Optional[int] = None,
        custom: Optional[dict[str, Any]] = None,
    ) -> RepoAsset:
        """Updates provenance metadata (Author, Source, License, Tags, Custom) on a RepoAsset without mutating history."""
        norm_comp = normalize_rel_path(component_path)
        norm_asset = normalize_rel_path(asset_path)
        snapshot = load_component_snapshot(project_root, norm_comp)
        if norm_asset not in snapshot.repo_assets:
            raise KeyError(f"Repo asset '{norm_asset}' not found in component '{norm_comp}'.")

        asset = snapshot.repo_assets[norm_asset]
        payload: dict[str, Any] = {"asset_path": norm_asset}
        if author is not None:
            asset.author = author
            payload["author"] = author
        if source_info is not None:
            asset.source = source_info
            payload["source"] = source_info
        if license_info is not None:
            asset.license = license_info
            payload["license"] = license_info
        if tags is not None:
            asset.tags = list(tags)
            payload["tags"] = asset.tags
        if custom is not None:
            asset.custom.update(custom)
            payload["custom"] = asset.custom
        if comment is not None and asset.versions:
            target_v = (
                next((v for v in asset.versions if v.version == version), asset.versions[-1])
                if version is not None
                else asset.versions[-1]
            )
            target_v.comment = comment
            payload["version"] = target_v.version
            payload["comment"] = comment

        evt = append_event(project_root, norm_comp, "REPO_METADATA_UPDATED", payload)
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)
        ProjectIndex(project_root).index_component(snapshot)
        return asset

    def update_working_metadata(
        self,
        project_root: Path,
        component_path: str,
        version: int,
        comment: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> VersionBundle:
        """Updates comment and tags on a working/vNNN/ version without mutating file history."""
        norm_comp = normalize_rel_path(component_path)
        snapshot = load_component_snapshot(project_root, norm_comp)
        bundle = next((v for v in snapshot.working_versions if v.version == version), None)
        if bundle is None:
            raise KeyError(f"Working version v{version:03d} not found in component '{norm_comp}'.")

        payload: dict[str, Any] = {"version": version}
        if comment is not None:
            bundle.comment = comment
            payload["comment"] = comment
        if tags is not None:
            bundle.tags = list(tags)
            payload["tags"] = bundle.tags

        evt = append_event(project_root, norm_comp, "WORKING_METADATA_UPDATED", payload)
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)
        ProjectIndex(project_root).index_component(snapshot)
        return bundle

    def update_published_metadata(
        self,
        project_root: Path,
        component_path: str,
        deliverable_name: str,
        version: int,
        comment: Optional[str] = None,
        tags: Optional[list[str]] = None,
    ) -> VersionBundle:
        """Updates release note and tags on a published/ version without mutating deliverable files."""
        norm_comp = normalize_rel_path(component_path)
        snapshot = load_component_snapshot(project_root, norm_comp)
        if deliverable_name == "__legacy__":
            bundle = next((v for v in snapshot.published_versions if v.version == version), None)
            deliv_key = "__legacy__"
            deliverable = None
        else:
            deliv_path = self.normalize_deliverable_name(deliverable_name)
            deliverable = snapshot.published_deliverables.get(deliv_path)
            if deliverable is None:
                raise KeyError(f"Deliverable '{deliv_path}' not found in component '{norm_comp}'.")
            bundle = next((v for v in deliverable.versions if v.version == version), None)
            deliv_key = deliv_path

        if bundle is None:
            raise KeyError(f"Published version v{version:03d} not found in '{deliv_key}'.")

        payload: dict[str, Any] = {"deliverable_path": deliv_key, "version": version}
        if comment is not None:
            bundle.comment = comment
            payload["comment"] = comment
        if tags is not None:
            bundle.tags = list(tags)
            payload["tags"] = bundle.tags
            if deliverable is not None:
                for t in bundle.tags:
                    if t not in deliverable.tags:
                        deliverable.tags.append(t)

        evt = append_event(project_root, norm_comp, "PUBLISHED_METADATA_UPDATED", payload)
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)
        ProjectIndex(project_root).index_component(snapshot)
        return bundle

    @staticmethod
    def normalize_deliverable_name(deliverable_name: str) -> str:
        raw = normalize_rel_path(deliverable_name or "main")
        parts = [re.sub(r"[^a-zA-Z0-9_-]+", "_", p).strip("_").lower() for p in raw.split("/") if p.strip()]
        parts = [p for p in parts if p]
        return "/".join(parts) if parts else "main"

    @staticmethod
    def resolve_publish_target_filename(
        deliverable_leaf: str,
        version_label: str,
        source_path: Path,
        ext_counts: Counter[str],
        output_label: str = "",
        keep_original_name: bool = False,
        existing_filenames: Optional[set[str]] = None,
    ) -> str:
        if keep_original_name:
            return source_path.name
        ext = source_path.suffix
        ext_lower = ext.lower()
        clean_label = re.sub(r"[^a-zA-Z0-9_-]+", "_", output_label).strip("_").lower() if output_label else ""
        if clean_label:
            candidate = f"{deliverable_leaf}_{clean_label}_{version_label}{ext}"
        elif ext_counts.get(ext_lower, 1) == 1:
            candidate = f"{deliverable_leaf}_{version_label}{ext}"
        else:
            raw_stem = re.sub(r"_v\d+$", "", source_path.stem, flags=re.IGNORECASE)
            clean_stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_stem).strip("_").lower() or "output"
            candidate = f"{deliverable_leaf}_{clean_stem}_{version_label}{ext}"

        if existing_filenames and candidate in existing_filenames:
            raw_stem = re.sub(r"_v\d+$", "", source_path.stem, flags=re.IGNORECASE)
            clean_stem = re.sub(r"[^a-zA-Z0-9_-]+", "_", raw_stem).strip("_").lower() or "output"
            stem_candidate = f"{deliverable_leaf}_{clean_stem}_{version_label}{ext}"
            if stem_candidate not in existing_filenames:
                return stem_candidate
            return source_path.name
        return candidate

    def publish_version(
        self,
        project_root: Path,
        component_path: str,
        source_files: Optional[list[Path]] = None,
        source_working_version: Optional[int] = None,
        comment: str = "",
        tags: Optional[list[str]] = None,
        deliverable_name: str = "main",
        publish_items: Optional[list[PublishItem]] = None,
        target_version: Optional[int] = None,
        keep_original_names: bool = False,
    ) -> VersionBundle:
        """Publishes exported deliverable(s) into published/<deliverable_name>/vNNN/ with lineage binding."""
        items = list(publish_items or [])
        if not items and source_files:
            items = [PublishItem(source_path=sf) for sf in source_files]
        if not items:
            raise ValueError("At least one file must be provided to publish.")
        for it in items:
            if not it.source_path.exists():
                raise FileNotFoundError(f"Publish source file not found: {it.source_path}")

        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)

        deliv_path = self.normalize_deliverable_name(deliverable_name)
        deliv_leaf = deliv_path.split("/")[-1]
        deliverable = snapshot.published_deliverables.get(deliv_path)
        if deliverable is None:
            deliverable = PublishedDeliverable(
                deliverable_path=deliv_path,
                name=deliv_leaf,
                tags=list(tags or []),
            )
            snapshot.published_deliverables[deliv_path] = deliverable
        elif tags:
            for t in tags:
                if t not in deliverable.tags:
                    deliverable.tags.append(t)

        bound_working_ver = source_working_version
        if bound_working_ver is None:
            bound_working_ver = snapshot.working_versions[-1].version if snapshot.working_versions else 1

        existing_bundle = (
            next((v for v in deliverable.versions if v.version == target_version), None)
            if target_version is not None
            else None
        )

        if existing_bundle is not None:
            pub_ver = existing_bundle.version
            ver_label = existing_bundle.version_label
            pub_ver_dir = comp_dir / existing_bundle.folder_rel
            pub_ver_dir.mkdir(parents=True, exist_ok=True)
            existing_names = {Path(tf.rel_path).name for tf in existing_bundle.files}
        else:
            pub_ver = max((v.version for v in deliverable.versions), default=0) + 1
            ver_label = f"v{pub_ver:03d}"
            pub_ver_dir = comp_dir / "published" / deliv_path / ver_label
            pub_ver_dir.mkdir(parents=True, exist_ok=True)
            existing_names = set()

        ext_counts = Counter(it.source_path.suffix.lower() for it in items)
        new_tracked: list[TrackedFile] = []

        for it in items:
            dest_name = self.resolve_publish_target_filename(
                deliverable_leaf=deliv_leaf,
                version_label=ver_label,
                source_path=it.source_path,
                ext_counts=ext_counts,
                output_label=it.output_label,
                keep_original_name=it.keep_original_name or keep_original_names,
                existing_filenames=existing_names,
            )
            existing_names.add(dest_name)
            dest_path = pub_ver_dir / dest_name
            shutil.copy2(it.source_path, dest_path)
            rel = normalize_rel_path(str(dest_path.relative_to(comp_dir)))
            sha = compute_file_sha256(dest_path)
            new_tracked.append(
                TrackedFile(
                    rel_path=rel,
                    sha256=sha,
                    size_bytes=dest_path.stat().st_size,
                    file_type=dest_path.suffix.lstrip(".").lower(),
                    output_label=it.output_label,
                )
            )

        folder_rel = normalize_rel_path(str(pub_ver_dir.relative_to(comp_dir)))
        if existing_bundle is not None:
            by_rel = {tf.rel_path: tf for tf in existing_bundle.files}
            for tf in new_tracked:
                by_rel[tf.rel_path] = tf
            existing_bundle.files = list(by_rel.values())
            if not existing_bundle.primary_file and new_tracked:
                existing_bundle.primary_file = new_tracked[0].rel_path
            if comment:
                existing_bundle.comment = comment
            if tags:
                for t in tags:
                    if t not in existing_bundle.tags:
                        existing_bundle.tags.append(t)
            bundle = existing_bundle
            event_type = "FILE_PUBLISHED_ADDED"
        else:
            bundle = VersionBundle(
                version=pub_ver,
                folder_rel=folder_rel,
                primary_file=new_tracked[0].rel_path,
                files=new_tracked,
                created_at=utc_now_iso(),
                comment=comment or f"Published from working v{bound_working_ver:03d}",
                source_working_version=bound_working_ver,
                tags=list(tags or []),
            )
            deliverable.versions.append(bundle)
            deliverable.latest_version = max(deliverable.latest_version, pub_ver)
            event_type = "FILE_PUBLISHED"

        evt = append_event(
            project_root,
            norm_comp,
            event_type,
            {
                "deliverable_path": deliv_path,
                "version": pub_ver,
                "source_working_version": bound_working_ver,
                "folder_rel": folder_rel,
                "primary_file": bundle.primary_file,
                "files": [f.to_dict() for f in new_tracked],
                "comment": bundle.comment,
            },
        )
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)
        ProjectIndex(project_root).index_component(snapshot)
        return bundle

    def prepare_publish_export_folder(
        self,
        project_root: Path,
        component_path: str,
        deliverable_name: str = "main",
        target_version: Optional[int] = None,
    ) -> tuple[str, int, Path]:
        """Creates and returns (deliverable_path, version_num, export_dir) for direct DCC multi-format exports."""
        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)
        deliv_path = self.normalize_deliverable_name(deliverable_name)
        deliv = snapshot.published_deliverables.get(deliv_path)
        if target_version is not None:
            ver_num = target_version
        else:
            ver_num = (max((v.version for v in deliv.versions), default=0) if deliv else 0) + 1
        export_dir = comp_dir / "published" / deliv_path / f"v{ver_num:03d}"
        export_dir.mkdir(parents=True, exist_ok=True)
        return deliv_path, ver_num, export_dir

    def sync_published_folder(
        self,
        project_root: Path,
        component_path: str,
        deliverable_name: str = "main",
        version: Optional[int] = None,
        source_working_version: Optional[int] = None,
        comment: str = "",
    ) -> Optional[VersionBundle]:
        """Scans an export folder in published/<deliverable>/vNNN/ and registers all exported files."""
        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        deliv_path = self.normalize_deliverable_name(deliverable_name)
        deliv_dir = comp_dir / "published" / deliv_path

        snapshot = load_component_snapshot(project_root, norm_comp)
        bound_working = source_working_version
        if bound_working is None:
            bound_working = snapshot.working_versions[-1].version if snapshot.working_versions else 1

        if version is None:
            if not deliv_dir.is_dir():
                return None
            vers = sorted(
                int(m.group(1))
                for c in deliv_dir.iterdir()
                if c.is_dir() and (m := re.match(r"^v(\d+)$", c.name, re.IGNORECASE))
            )
            if not vers:
                return None
            version = vers[-1]

        ver_dir = deliv_dir / f"v{version:03d}"
        if not ver_dir.is_dir():
            return None

        files_on_disk = [f for f in sorted(ver_dir.rglob("*")) if f.is_file() and not f.name.startswith(".")]
        if not files_on_disk:
            return None

        Reconciler.reconcile_component(project_root, norm_comp)
        snapshot = load_component_snapshot(project_root, norm_comp)
        deliv = snapshot.published_deliverables.get(deliv_path)
        if not deliv:
            return None
        bundle = next((v for v in deliv.versions if v.version == version), None)
        if bundle is None:
            return None
        bundle.source_working_version = bound_working
        if comment:
            bundle.comment = comment
        elif bundle.comment == "Discovered on disk by Reconciler":
            bundle.comment = f"Exported from working v{bound_working:03d}"

        evt = append_event(
            project_root,
            norm_comp,
            "FILE_PUBLISHED",
            {
                "deliverable_path": deliv_path,
                "version": version,
                "source_working_version": bound_working,
                "folder_rel": bundle.folder_rel,
                "primary_file": bundle.primary_file,
                "files": [f.to_dict() for f in bundle.files],
                "comment": bundle.comment,
            },
        )
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)
        ProjectIndex(project_root).index_component(snapshot)
        return bundle

    def scan_external_dependencies(
        self,
        project_root: Path,
        component_path: str,
        working_version: Optional[int] = None,
    ) -> list[ExternalDependency]:
        """Scans a working version via its bound software plugin (e.g. Reaper .rpp) for external files outside repo/."""
        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)
        if not snapshot.working_versions:
            return []

        bundle = (
            next((v for v in snapshot.working_versions if v.version == working_version), None)
            if working_version is not None
            else snapshot.working_versions[-1]
        )
        if not bundle or not bundle.primary_file:
            return []

        primary_abs = comp_dir / bundle.primary_file
        profile = self.hub.get_software_profile(snapshot.software)
        plugin = self.hub.plugin_registry.get(profile.plugin)
        ctx = TaskContext(
            project_root=project_root,
            component_path=norm_comp,
            component_dir=comp_dir,
            software_profile=profile,
        )
        externals, used_repo = plugin.inspect_dependencies(primary_abs, ctx)
        changed = False
        for rpath in used_repo:
            if rpath in snapshot.repo_assets and bundle.version not in snapshot.repo_assets[rpath].used_by_working_versions:
                snapshot.repo_assets[rpath].used_by_working_versions.append(bundle.version)
                changed = True
        if changed:
            save_component_snapshot(project_root, snapshot)
        return externals

    def collect_external_dependencies(
        self,
        project_root: Path,
        component_path: str,
        working_version: Optional[int] = None,
    ) -> list[RepoAsset]:
        """Ingests external dependencies referenced in a working file (e.g. Reaper .rpp) into repo/ and relinks relative paths."""
        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)
        if not snapshot.working_versions:
            return []

        bundle = (
            next((v for v in snapshot.working_versions if v.version == working_version), None)
            if working_version is not None
            else snapshot.working_versions[-1]
        )
        if not bundle or not bundle.primary_file:
            return []

        primary_abs = comp_dir / bundle.primary_file
        externals = self.scan_external_dependencies(project_root, norm_comp, bundle.version)
        collected_assets: list[RepoAsset] = []
        relink_map: dict[str, Path] = {}

        for dep in externals:
            if not dep.exists:
                continue
            target_asset_path = (
                f"{dep.suggested_category}/{dep.suggested_asset_name}"
                if dep.suggested_category
                else dep.suggested_asset_name
            )
            asset, ver_bundle = self.ingest_repo_asset(
                project_root=project_root,
                component_path=norm_comp,
                source_file=dep.resolved_path,
                asset_path=target_asset_path,
                source_info=str(dep.resolved_path),
                comment=f"Auto-collected from working {bundle.version_label}",
            )
            collected_assets.append(asset)
            relink_map[dep.raw_reference] = comp_dir / ver_bundle.primary_file

        if relink_map and primary_abs.suffix.lower() == ".rpp":
            ReaperPlugin.relink_rpp_references(primary_abs, relink_map)
            Reconciler.reconcile_component(project_root, norm_comp)

        return collected_assets

    def generate_credits_report(self, project_root: Path) -> list[dict[str, Any]]:
        """Generates a complete attribution & license audit across all repo/ assets in the project."""
        rows: list[dict[str, Any]] = []
        for comp in discover_project_components(project_root):
            snap = load_component_snapshot(project_root, comp)
            for apath, asset in sorted(snap.repo_assets.items()):
                rows.append({
                    "component": comp,
                    "asset_path": apath,
                    "name": asset.name,
                    "latest_version": f"v{asset.latest_version:03d}",
                    "author": asset.author or "Unspecified",
                    "source": asset.source or "Unspecified",
                    "license": asset.license or "Unspecified",
                    "tags": ", ".join(asset.tags),
                    "used_by_working": [f"v{v:03d}" for v in asset.used_by_working_versions],
                })
        return rows

    @staticmethod
    def _prune_empty_parents(start_dir: Path, stop_at: Path) -> None:
        curr = start_dir
        stop_resolved = stop_at.resolve()
        while curr.exists():
            try:
                if curr.resolve() == stop_resolved:
                    break
                if any(curr.iterdir()):
                    break
                curr.rmdir()
                curr = curr.parent
            except OSError:
                break

    def delete_component(self, project_root: Path, component_path: str, delete_files: bool = True) -> bool:
        """Deletes a Triad component's metadata and (optionally) its folder tree on disk."""
        norm_comp = normalize_rel_path(component_path)
        meta_dir = get_component_meta_dir(project_root, norm_comp)
        comp_dir = project_root / norm_comp
        existed = meta_dir.exists() or comp_dir.exists()
        if not existed:
            return False
        if meta_dir.exists():
            safe_rmtree(meta_dir)
        if delete_files and comp_dir.exists():
            safe_rmtree(comp_dir)
        ProjectIndex(project_root).rebuild_all()
        return True

    def delete_working_version(self, project_root: Path, component_path: str, version: int) -> bool:
        """Deletes a single working/vNNN/ version folder and updates snapshot + index."""
        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)
        bundle = next((v for v in snapshot.working_versions if v.version == version), None)
        if bundle is None:
            return False
        safe_rmtree(comp_dir / bundle.folder_rel)
        snapshot.working_versions = [v for v in snapshot.working_versions if v.version != version]
        for asset in snapshot.repo_assets.values():
            if version in asset.used_by_working_versions:
                asset.used_by_working_versions = [v for v in asset.used_by_working_versions if v != version]
        evt = append_event(project_root, norm_comp, "WORKING_VERSION_DELETED", {"version": version})
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)
        ProjectIndex(project_root).index_component(snapshot)
        return True

    def delete_repo_asset(self, project_root: Path, component_path: str, asset_path: str) -> bool:
        """Deletes a named Repo Asset and all its versions from repo/."""
        norm_comp = normalize_rel_path(component_path)
        norm_asset = normalize_rel_path(asset_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)
        if norm_asset not in snapshot.repo_assets:
            return False
        snapshot.repo_assets.pop(norm_asset, None)
        asset_dir = comp_dir / "repo" / norm_asset
        if asset_dir.exists():
            safe_rmtree(asset_dir)
            self._prune_empty_parents(asset_dir.parent, comp_dir / "repo")
        evt = append_event(project_root, norm_comp, "REPO_ASSET_DELETED", {"asset_path": norm_asset})
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)
        ProjectIndex(project_root).index_component(snapshot)
        return True

    def delete_repo_version(self, project_root: Path, component_path: str, asset_path: str, version: int) -> bool:
        """Deletes a single version vNNN/ of a Repo Asset."""
        norm_comp = normalize_rel_path(component_path)
        norm_asset = normalize_rel_path(asset_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)
        asset = snapshot.repo_assets.get(norm_asset)
        if asset is None:
            return False
        bundle = next((v for v in asset.versions if v.version == version), None)
        if bundle is None:
            return False
        safe_rmtree(comp_dir / bundle.folder_rel)
        asset.versions = [v for v in asset.versions if v.version != version]
        asset.latest_version = max((v.version for v in asset.versions), default=0)
        evt = append_event(
            project_root,
            norm_comp,
            "REPO_VERSION_DELETED",
            {"asset_path": norm_asset, "version": version},
        )
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)
        ProjectIndex(project_root).index_component(snapshot)
        return True

    def delete_published_deliverable(self, project_root: Path, component_path: str, deliverable_name: str) -> bool:
        """Deletes a named Published Deliverable and all its releases from published/."""
        norm_comp = normalize_rel_path(component_path)
        deliv_path = self.normalize_deliverable_name(deliverable_name)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)
        if deliv_path not in snapshot.published_deliverables:
            return False
        snapshot.published_deliverables.pop(deliv_path, None)
        deliv_dir = comp_dir / "published" / deliv_path
        if deliv_dir.exists():
            safe_rmtree(deliv_dir)
            self._prune_empty_parents(deliv_dir.parent, comp_dir / "published")
        evt = append_event(project_root, norm_comp, "DELIVERABLE_DELETED", {"deliverable_path": deliv_path})
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)
        ProjectIndex(project_root).index_component(snapshot)
        return True

    def delete_published_version(
        self,
        project_root: Path,
        component_path: str,
        deliverable_name: str,
        version: int,
    ) -> bool:
        """Deletes a single published/vNNN/ release from a named Deliverable (or legacy published_versions)."""
        norm_comp = normalize_rel_path(component_path)
        comp_dir = project_root / norm_comp
        snapshot = load_component_snapshot(project_root, norm_comp)
        if deliverable_name == "__legacy__":
            bundle = next((v for v in snapshot.published_versions if v.version == version), None)
            if bundle is None:
                return False
            safe_rmtree(comp_dir / bundle.folder_rel)
            snapshot.published_versions = [v for v in snapshot.published_versions if v.version != version]
            deliv_key = "__legacy__"
        else:
            deliv_path = self.normalize_deliverable_name(deliverable_name)
            deliv = snapshot.published_deliverables.get(deliv_path)
            if deliv is None:
                return False
            bundle = next((v for v in deliv.versions if v.version == version), None)
            if bundle is None:
                return False
            safe_rmtree(comp_dir / bundle.folder_rel)
            deliv.versions = [v for v in deliv.versions if v.version != version]
            deliv.latest_version = max((v.version for v in deliv.versions), default=0)
            deliv_key = deliv_path

        evt = append_event(
            project_root,
            norm_comp,
            "PUBLISHED_VERSION_DELETED",
            {"deliverable_path": deliv_key, "version": version},
        )
        snapshot.last_event_id = evt.id
        save_component_snapshot(project_root, snapshot)
        ProjectIndex(project_root).index_component(snapshot)
        return True

    def resolve_working_file_context(self, file_path: Path) -> Optional[WorkingFileContext]:
        """Resolves any working file inside `<project>/<component>/working/vNNN/...` to its Obol context."""
        if not file_path:
            return None
        resolved = Path(file_path).expanduser().resolve()
        project_root: Optional[Path] = None
        for candidate in [resolved.parent] + list(resolved.parent.parents):
            if (candidate / ".obol" / "project.toml").is_file():
                project_root = candidate
                break
        if project_root is None:
            return None

        raw_proj = load_toml_with_fallback(project_root / ".obol" / "project.toml")
        proj_cfg = ProjectConfig.from_dict(raw_proj) if raw_proj else ProjectConfig(
            name=project_root.name,
            code=project_root.name[:4].lower() or "prj",
        )
        entry = self.hub.register_project(
            project_root,
            name=proj_cfg.name,
            code=proj_cfg.code,
            template_used=proj_cfg.template_used,
        )

        try:
            rel_parts = resolved.relative_to(project_root).parts
        except ValueError:
            return None

        # Locate `<component>/working/vNNN/...` (search from right in case a group folder is named 'working')
        working_idx: Optional[int] = None
        ver_num: Optional[int] = None
        for idx in range(len(rel_parts) - 2, 0, -1):
            if rel_parts[idx].lower() == "working":
                m = re.match(r"^v(\d+)$", rel_parts[idx + 1], re.IGNORECASE)
                if m:
                    working_idx = idx
                    ver_num = int(m.group(1))
                    break

        if working_idx is None or ver_num is None:
            return None

        component_path = normalize_rel_path("/".join(rel_parts[:working_idx]))
        comp_dir = project_root / component_path
        if not (comp_dir / "working").is_dir():
            return None

        snapshot = load_component_snapshot(project_root, component_path)
        return WorkingFileContext(
            project_name=entry.name,
            project_root=project_root,
            component_path=component_path,
            working_version=ver_num,
            working_file=resolved,
            snapshot=snapshot,
        )

    def list_importable_assets(
        self,
        project_root: Path,
        current_component: str = "",
    ) -> list[dict[str, Any]]:
        """Lists importable repo/ assets (for current component or all) and published/ deliverables across the project."""
        items: list[dict[str, Any]] = []
        norm_curr = normalize_rel_path(current_component) if current_component else ""
        components = discover_project_components(project_root)

        for comp in components:
            snap = load_component_snapshot(project_root, comp)
            comp_dir = project_root / comp

            if not norm_curr or comp == norm_curr:
                for apath, asset in sorted(snap.repo_assets.items()):
                    for bundle in asset.versions:
                        tfs = bundle.files or (
                            [TrackedFile(rel_path=bundle.primary_file, sha256="", size_bytes=0)]
                            if bundle.primary_file
                            else []
                        )
                        for tf in tfs:
                            fpath = (comp_dir / tf.rel_path).resolve()
                            items.append({
                                "scope": "repo",
                                "component": comp,
                                "asset_key": apath,
                                "name": asset.name,
                                "version": bundle.version,
                                "version_label": bundle.version_label,
                                "latest_version": asset.latest_version,
                                "is_latest": bundle.version == asset.latest_version,
                                "file_path": fpath,
                                "file_name": fpath.name,
                                "rel_path": tf.rel_path,
                                "file_type": tf.file_type or fpath.suffix.lstrip(".").lower(),
                                "output_label": tf.output_label,
                                "note": bundle.comment,
                                "author": asset.author,
                                "license": asset.license,
                                "exists": fpath.is_file(),
                            })

            for dpath, deliv in sorted(snap.published_deliverables.items()):
                for bundle in deliv.versions:
                    tfs = bundle.files or (
                        [TrackedFile(rel_path=bundle.primary_file, sha256="", size_bytes=0)]
                        if bundle.primary_file
                        else []
                    )
                    for tf in tfs:
                        fpath = (comp_dir / tf.rel_path).resolve()
                        items.append({
                            "scope": "published",
                            "component": comp,
                            "asset_key": dpath,
                            "name": deliv.name,
                            "version": bundle.version,
                            "version_label": bundle.version_label,
                            "latest_version": deliv.latest_version,
                            "is_latest": bundle.version == deliv.latest_version,
                            "file_path": fpath,
                            "file_name": fpath.name,
                            "rel_path": tf.rel_path,
                            "file_type": tf.file_type or fpath.suffix.lstrip(".").lower(),
                            "output_label": tf.output_label,
                            "note": bundle.comment,
                            "source_working_version": bundle.source_working_version,
                            "exists": fpath.is_file(),
                        })

            if snap.published_versions:
                latest_leg = max((v.version for v in snap.published_versions), default=0)
                for bundle in snap.published_versions:
                    tfs = bundle.files or (
                        [TrackedFile(rel_path=bundle.primary_file, sha256="", size_bytes=0)]
                        if bundle.primary_file
                        else []
                    )
                    for tf in tfs:
                        fpath = (comp_dir / tf.rel_path).resolve()
                        items.append({
                            "scope": "published",
                            "component": comp,
                            "asset_key": "__legacy__",
                            "name": "legacy",
                            "version": bundle.version,
                            "version_label": bundle.version_label,
                            "latest_version": latest_leg,
                            "is_latest": bundle.version == latest_leg,
                            "file_path": fpath,
                            "file_name": fpath.name,
                            "rel_path": tf.rel_path,
                            "file_type": tf.file_type or fpath.suffix.lstrip(".").lower(),
                            "output_label": tf.output_label,
                            "note": bundle.comment,
                            "source_working_version": bundle.source_working_version,
                            "exists": fpath.is_file(),
                        })

        return items

    @staticmethod
    def _match_bundle_file(
        comp_dir: Path,
        current_file: Path,
        current_tf: Optional[TrackedFile],
        latest_bundle: VersionBundle,
    ) -> Optional[Path]:
        if not latest_bundle.files:
            if latest_bundle.primary_file:
                cand = (comp_dir / latest_bundle.primary_file).resolve()
                return cand if cand.is_file() else None
            return None

        curr_ext = current_file.suffix.lower()
        if current_tf and current_tf.output_label:
            for tf in latest_bundle.files:
                if tf.output_label == current_tf.output_label and Path(tf.rel_path).suffix.lower() == curr_ext:
                    cand = (comp_dir / tf.rel_path).resolve()
                    if cand.is_file():
                        return cand

        curr_stem = re.sub(r"_v\d+$", "", current_file.stem, flags=re.IGNORECASE).lower()
        for tf in latest_bundle.files:
            p = Path(tf.rel_path)
            tf_stem = re.sub(r"_v\d+$", "", p.stem, flags=re.IGNORECASE).lower()
            if p.suffix.lower() == curr_ext and tf_stem == curr_stem:
                cand = (comp_dir / tf.rel_path).resolve()
                if cand.is_file():
                    return cand

        same_ext = [tf for tf in latest_bundle.files if Path(tf.rel_path).suffix.lower() == curr_ext]
        if len(same_ext) == 1:
            cand = (comp_dir / same_ext[0].rel_path).resolve()
            if cand.is_file():
                return cand

        if latest_bundle.primary_file:
            cand = (comp_dir / latest_bundle.primary_file).resolve()
            if cand.suffix.lower() == curr_ext and cand.is_file():
                return cand
        return None

    def find_newer_asset_version(self, project_root: Path, media_file_path: Path) -> Optional[dict[str, Any]]:
        """Inspects a media file path to determine if it belongs to repo/ or published/ and whether a newer version exists."""
        proj_resolved = project_root.resolve()
        media_resolved = Path(media_file_path).expanduser().resolve()
        try:
            rel_to_proj = normalize_rel_path(str(media_resolved.relative_to(proj_resolved)))
        except ValueError:
            return None

        components = sorted(discover_project_components(proj_resolved), key=len, reverse=True)
        matched_comp = ""
        sub_rel = ""
        for comp in components:
            prefix = f"{comp}/"
            if rel_to_proj.startswith(prefix):
                matched_comp = comp
                sub_rel = rel_to_proj[len(prefix):]
                break
        if not matched_comp:
            return None

        parts = sub_rel.split("/")
        if len(parts) < 3 or parts[0] not in ("repo", "published"):
            return None

        kind = parts[0]
        ver_idx: Optional[int] = None
        curr_ver = 0
        for idx in range(1, len(parts) - 1):
            m = re.match(r"^v(\d+)$", parts[idx], re.IGNORECASE)
            if m:
                ver_idx = idx
                curr_ver = int(m.group(1))
                break
        if ver_idx is None:
            return None

        comp_dir = proj_resolved / matched_comp
        snap = load_component_snapshot(proj_resolved, matched_comp)
        asset_key = "/".join(parts[1:ver_idx]) or "__legacy__"

        if kind == "repo":
            asset = snap.repo_assets.get(asset_key)
            if not asset or not asset.versions:
                return None
            curr_bundle = next((v for v in asset.versions if v.version == curr_ver), None)
            latest_bundle = max(asset.versions, key=lambda v: v.version)
            curr_tf = (
                next((f for f in curr_bundle.files if normalize_rel_path(f.rel_path) == sub_rel), None)
                if curr_bundle
                else None
            )
            latest_file = (
                (comp_dir / latest_bundle.primary_file).resolve()
                if latest_bundle.primary_file
                else media_resolved
            )
            if not latest_file.is_file():
                latest_file = self._match_bundle_file(comp_dir, media_resolved, curr_tf, latest_bundle) or media_resolved
            up_to_date = latest_bundle.version <= curr_ver
            return {
                "component": matched_comp,
                "kind": "repo",
                "asset_key": asset_key,
                "current_version": curr_ver,
                "latest_version": latest_bundle.version,
                "current_file": media_resolved,
                "latest_file": latest_file,
                "up_to_date": up_to_date,
                "update_available": (not up_to_date) and latest_file.is_file() and latest_file != media_resolved,
            }

        # kind == "published"
        if asset_key == "__legacy__":
            versions = snap.published_versions
        else:
            deliv = snap.published_deliverables.get(asset_key)
            if not deliv:
                return None
            versions = deliv.versions
        if not versions:
            return None

        curr_bundle = next((v for v in versions if v.version == curr_ver), None)
        latest_bundle = max(versions, key=lambda v: v.version)
        curr_tf = (
            next((f for f in curr_bundle.files if normalize_rel_path(f.rel_path) == sub_rel), None)
            if curr_bundle
            else None
        )
        matched_latest = self._match_bundle_file(comp_dir, media_resolved, curr_tf, latest_bundle)
        up_to_date = latest_bundle.version <= curr_ver
        latest_file = matched_latest or media_resolved
        return {
            "component": matched_comp,
            "kind": "published",
            "asset_key": asset_key,
            "current_version": curr_ver,
            "latest_version": latest_bundle.version,
            "current_file": media_resolved,
            "latest_file": latest_file,
            "up_to_date": up_to_date,
            "update_available": (not up_to_date) and (matched_latest is not None) and (matched_latest != media_resolved),
        }
