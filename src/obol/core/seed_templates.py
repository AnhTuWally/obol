"""Scoped & Versioned Software Seed Template Engine (Project -> Client Root -> Global cascade)."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Optional

from obol.core.models import (
    ResolvedSeedTemplate,
    SeedTemplateStream,
    SeedTemplateVersion,
    SoftwareSeedManifest,
    TaskTemplate,
    TemplateScope,
    utc_now_iso,
)
from obol.core.storage import (
    VERSION_DIR_RE,
    atomic_write_toml,
    compute_file_sha256,
    load_toml_with_fallback,
    normalize_rel_path,
    safe_rmtree,
)
from obol.plugins.base import lock_file_readonly, matches_any_pattern, unlock_file_writable


def normalize_template_name(raw_name: str) -> str:
    """Normalizes a seed template stream name into a safe filesystem slug."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", (raw_name or "").strip()).strip("_").lower()
    return cleaned or "default"


def normalize_software_key(raw_sw: str) -> str:
    """Normalizes a software profile key."""
    cleaned = re.sub(r"[^a-zA-Z0-9_-]+", "_", (raw_sw or "").strip()).strip("_").lower()
    return cleaned or "generic"


def parse_seed_spec(spec: str) -> Optional[tuple[TemplateScope, str, str, Optional[int]]]:
    """Parses 'seed:<scope>:<software>:<template_name>[:vNNN]' into components."""
    if not spec or not spec.startswith("seed:"):
        return None
    parts = spec.split(":")
    if len(parts) not in (4, 5):
        return None
    _, scope_raw, sw_raw, name_raw = parts[:4]
    try:
        scope = TemplateScope(scope_raw.strip())
    except ValueError:
        return None
    sw = normalize_software_key(sw_raw)
    name = normalize_template_name(name_raw)
    version: Optional[int] = None
    if len(parts) == 5 and parts[4].strip():
        v_raw = parts[4].strip()
        m = re.match(r"^v?(\d+)$", v_raw, re.IGNORECASE)
        if not m:
            return None
        version = int(m.group(1))
        if version <= 0:
            version = None
    return scope, sw, name, version


class SeedTemplateManager:
    """Manages versioned per-software seed templates across Project, Client Workspace Root, and Global scopes."""

    def __init__(
        self,
        hub_dir: Path,
        vault_dir: Optional[Path] = None,
        client_root: Optional[Path] = None,
    ) -> None:
        self.hub_dir = hub_dir.resolve()
        self.vault_dir = vault_dir.resolve() if vault_dir else None
        self.client_root = client_root.expanduser().resolve() if client_root else None

    def resolve_enclosing_client_root(
        self,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
    ) -> Optional[Path]:
        """Determines the Client Workspace Root for cascade resolution without leaking across unrelated roots."""
        if client_root is not None:
            return client_root.expanduser().resolve()
        if project_root is not None:
            pr = project_root.expanduser().resolve()
            if (pr.parent / ".obol" / "templates" / "seeds").is_dir():
                return pr.parent
            if self.client_root is not None:
                cr = self.client_root.expanduser().resolve()
                try:
                    pr.relative_to(cr)
                    return cr
                except ValueError:
                    pass
            return pr.parent
        if self.client_root is not None:
            return self.client_root.expanduser().resolve()
        return None

    def get_scope_root(
        self,
        scope: TemplateScope,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
    ) -> Path:
        if scope == TemplateScope.PROJECT:
            if project_root is None:
                raise ValueError("project_root is required for project-scoped templates.")
            return project_root.expanduser().resolve()
        if scope == TemplateScope.CLIENT_ROOT:
            resolved = self.resolve_enclosing_client_root(project_root=project_root, client_root=client_root)
            if resolved is None:
                raise ValueError("client_root could not be resolved for client_root-scoped templates.")
            return resolved
        return self.hub_dir

    def get_seeds_dir(
        self,
        scope: TemplateScope,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
    ) -> Path:
        scope_root = self.get_scope_root(scope, project_root=project_root, client_root=client_root)
        if scope in (TemplateScope.PROJECT, TemplateScope.CLIENT_ROOT):
            return scope_root / ".obol" / "templates" / "seeds"
        return scope_root / "templates" / "seeds"

    def list_scope_software(
        self,
        scope: TemplateScope,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
    ) -> list[str]:
        try:
            seeds_dir = self.get_seeds_dir(scope, project_root=project_root, client_root=client_root)
        except ValueError:
            return []
        if not seeds_dir.is_dir():
            return []
        return sorted(p.name for p in seeds_dir.iterdir() if p.is_dir() and not p.name.startswith("."))

    def load_manifest(
        self,
        software: str,
        scope: TemplateScope,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
        reconcile: bool = True,
        preferred_extensions: Optional[list[str]] = None,
    ) -> SoftwareSeedManifest:
        """Loads <seeds_dir>/<software>/manifest.toml; does NOT create directories when absent."""
        sw = normalize_software_key(software)
        try:
            seeds_dir = self.get_seeds_dir(scope, project_root=project_root, client_root=client_root)
        except ValueError:
            return SoftwareSeedManifest(software=sw)

        sw_dir = seeds_dir / sw
        if not sw_dir.is_dir():
            return SoftwareSeedManifest(software=sw)

        manifest_path = sw_dir / "manifest.toml"
        raw = load_toml_with_fallback(manifest_path)
        manifest = SoftwareSeedManifest.from_dict(sw, raw) if raw else SoftwareSeedManifest(software=sw)

        if reconcile:
            changed = self._reconcile_manifest(sw_dir, manifest, preferred_extensions=preferred_extensions)
            if changed:
                self.save_manifest(manifest, scope, project_root=project_root, client_root=client_root)

        return manifest

    def _reconcile_manifest(
        self,
        sw_dir: Path,
        manifest: SoftwareSeedManifest,
        preferred_extensions: Optional[list[str]] = None,
    ) -> bool:
        """Heals manifest against vNNN/ folders on disk without creating missing dirs."""
        changed = False
        pref_exts = {e.lower() for e in (preferred_extensions or [])}

        def _pick_primary(ver_dir: Path) -> Optional[Path]:
            files = sorted(
                p
                for p in ver_dir.rglob("*")
                if p.is_file() and not p.name.startswith(".") and ".backups" not in p.parts
            )
            if not files:
                return None
            if pref_exts:
                match = next((f for f in files if f.suffix.lower() in pref_exts), None)
                if match is not None:
                    return match
            return files[0]

        # 1. Validate recorded versions
        for tname in list(manifest.templates.keys()):
            stream = manifest.templates[tname]
            valid_vers: list[SeedTemplateVersion] = []
            for v in stream.versions:
                p_abs = sw_dir / v.primary_file if v.primary_file else None
                v_dir = sw_dir / v.folder_rel if v.folder_rel else None
                if p_abs and p_abs.is_file():
                    valid_vers.append(v)
                elif v_dir and v_dir.is_dir():
                    healed = _pick_primary(v_dir)
                    if healed is not None:
                        v.primary_file = normalize_rel_path(str(healed.relative_to(sw_dir)))
                        v.sha256 = compute_file_sha256(healed)
                        v.size_bytes = healed.stat().st_size
                        valid_vers.append(v)
                        changed = True
                    else:
                        changed = True
                else:
                    changed = True
            if len(valid_vers) != len(stream.versions):
                stream.versions = valid_vers
                changed = True

        # 2. Discover untracked stream/vNNN directories on disk
        for stream_dir in sorted(sw_dir.iterdir()):
            if not stream_dir.is_dir() or stream_dir.name.startswith("."):
                continue
            tname = normalize_template_name(stream_dir.name)
            for v_dir in sorted(stream_dir.iterdir()):
                if not v_dir.is_dir():
                    continue
                m = VERSION_DIR_RE.match(v_dir.name)
                if not m:
                    continue
                ver_num = int(m.group(1))
                primary = _pick_primary(v_dir)
                if primary is None:
                    continue
                stream = manifest.templates.get(tname)
                if stream is None:
                    stream = SeedTemplateStream(name=tname, software=manifest.software)
                    manifest.templates[tname] = stream
                    changed = True
                if not any(v.version == ver_num for v in stream.versions):
                    stream.versions.append(
                        SeedTemplateVersion(
                            version=ver_num,
                            folder_rel=normalize_rel_path(str(v_dir.relative_to(sw_dir))),
                            primary_file=normalize_rel_path(str(primary.relative_to(sw_dir))),
                            sha256=compute_file_sha256(primary),
                            size_bytes=primary.stat().st_size,
                            created_at=utc_now_iso(),
                            comment="Discovered on disk",
                        )
                    )
                    changed = True

        # 3. Clean up empty streams and stale default pointers
        for tname in list(manifest.templates.keys()):
            stream = manifest.templates[tname]
            stream.versions.sort(key=lambda v: v.version)
            if not stream.versions:
                manifest.templates.pop(tname)
                changed = True
            elif stream.default_version > 0 and not any(v.version == stream.default_version for v in stream.versions):
                stream.default_version = 0
                changed = True

        if manifest.default_template and manifest.default_template not in manifest.templates:
            manifest.default_template = ""
            changed = True

        return changed

    def save_manifest(
        self,
        manifest: SoftwareSeedManifest,
        scope: TemplateScope,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
    ) -> Path:
        seeds_dir = self.get_seeds_dir(scope, project_root=project_root, client_root=client_root)
        sw_dir = seeds_dir / normalize_software_key(manifest.software)
        sw_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = sw_dir / "manifest.toml"
        atomic_write_toml(manifest_path, manifest.to_dict())
        self._mirror_global_to_vault(manifest.software, sw_dir, scope)
        return manifest_path

    def _mirror_global_to_vault(self, software: str, sw_dir: Path, scope: TemplateScope) -> None:
        if scope != TemplateScope.GLOBAL or not self.vault_dir or not self.vault_dir.is_dir():
            return
        vault_sw_dir = self.vault_dir / "templates" / "seeds" / normalize_software_key(software)
        vault_sw_dir.mkdir(parents=True, exist_ok=True)
        for item in sw_dir.rglob("*"):
            if item.is_file() and not item.name.endswith(".tmp"):
                rel = item.relative_to(sw_dir)
                dest = vault_sw_dir / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dest)

    def add_version(
        self,
        software: str,
        template_name: str,
        source_path: Path,
        scope: TemplateScope,
        *,
        comment: str = "",
        author: str = "",
        source_label: str = "",
        tags: Optional[list[str]] = None,
        description: Optional[str] = None,
        set_as_default_template: Optional[bool] = None,
        set_as_default_version: bool = False,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
        ignore_patterns: Optional[list[str]] = None,
        lock_previous: bool = True,
        preferred_extensions: Optional[list[str]] = None,
    ) -> tuple[SeedTemplateStream, SeedTemplateVersion]:
        """Imports a starter file or folder bundle as the next vNNN/ version of a named seed template."""
        if not source_path.exists():
            raise FileNotFoundError(f"Source seed path not found: {source_path}")

        sw = normalize_software_key(software)
        tname = normalize_template_name(template_name)
        manifest = self.load_manifest(
            sw,
            scope,
            project_root=project_root,
            client_root=client_root,
            preferred_extensions=preferred_extensions,
        )
        stream = manifest.templates.get(tname)
        if stream is None:
            stream = SeedTemplateStream(name=tname, software=sw)
            manifest.templates[tname] = stream

        prev_ver_obj = stream.versions[-1] if stream.versions else None
        next_ver = stream.latest_version_number() + 1
        ver_label = f"v{next_ver:03d}"

        seeds_dir = self.get_seeds_dir(scope, project_root=project_root, client_root=client_root)
        sw_dir = seeds_dir / sw
        ver_dir = sw_dir / tname / ver_label
        ver_dir.mkdir(parents=True, exist_ok=True)

        pref_exts = {e.lower() for e in (preferred_extensions or [])}
        ignores = list(ignore_patterns or [])

        if source_path.is_file():
            dest_file = ver_dir / f"{tname}_{ver_label}{source_path.suffix}"
            shutil.copy2(source_path, dest_file)
            unlock_file_writable(dest_file)
            primary_abs = dest_file
        else:
            copied: list[Path] = []
            for item in sorted(source_path.rglob("*")):
                rel = item.relative_to(source_path).as_posix()
                if item.name.startswith(".") or ".backups" in item.parts:
                    continue
                if matches_any_pattern(rel, item.name, ignores):
                    continue
                dest_item = ver_dir / rel
                if item.is_dir():
                    dest_item.mkdir(parents=True, exist_ok=True)
                elif item.is_file():
                    dest_item.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, dest_item)
                    unlock_file_writable(dest_item)
                    copied.append(dest_item)
            if not copied:
                safe_rmtree(ver_dir)
                raise ValueError(f"No usable seed files found in directory: {source_path}")
            primary_abs = next((f for f in copied if f.suffix.lower() in pref_exts), copied[0])
            desired_primary = primary_abs.parent / f"{tname}_{ver_label}{primary_abs.suffix}"
            if desired_primary != primary_abs:
                if desired_primary.exists():
                    unlock_file_writable(desired_primary)
                    desired_primary.unlink()
                primary_abs.replace(desired_primary)
                primary_abs = desired_primary

        if lock_previous and prev_ver_obj is not None and prev_ver_obj.folder_rel:
            prev_dir = sw_dir / prev_ver_obj.folder_rel
            if prev_dir.is_dir():
                for old_file in prev_dir.rglob("*"):
                    if old_file.is_file():
                        lock_file_readonly(old_file)

        ver_obj = SeedTemplateVersion(
            version=next_ver,
            folder_rel=normalize_rel_path(str(ver_dir.relative_to(sw_dir))),
            primary_file=normalize_rel_path(str(primary_abs.relative_to(sw_dir))),
            sha256=compute_file_sha256(primary_abs),
            size_bytes=primary_abs.stat().st_size,
            created_at=utc_now_iso(),
            comment=comment or f"Added from {source_path.name}",
            author=author,
            source=source_label or str(source_path),
            tags=list(tags or []),
        )
        stream.versions.append(ver_obj)
        if description is not None:
            stream.description = description
        if set_as_default_version:
            stream.default_version = next_ver
        if set_as_default_template is True or (set_as_default_template is None and not manifest.default_template):
            manifest.default_template = tname

        self.save_manifest(manifest, scope, project_root=project_root, client_root=client_root)
        return stream, ver_obj

    def version_up_template(
        self,
        software: str,
        template_name: str,
        scope: TemplateScope,
        *,
        source_version: Optional[int] = None,
        comment: str = "",
        author: str = "",
        set_as_default_version: bool = False,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
        ignore_patterns: Optional[list[str]] = None,
        lock_previous: bool = True,
        preferred_extensions: Optional[list[str]] = None,
    ) -> tuple[SeedTemplateStream, SeedTemplateVersion]:
        """Clones an existing seed template version (vNNN -> v(N+1)), filtering backup/ignore files."""
        sw = normalize_software_key(software)
        tname = normalize_template_name(template_name)
        manifest = self.load_manifest(
            sw,
            scope,
            project_root=project_root,
            client_root=client_root,
            preferred_extensions=preferred_extensions,
        )
        stream = manifest.templates.get(tname)
        if stream is None:
            raise KeyError(f"Seed template '{tname}' not found for '{sw}' in scope '{scope.value}'.")

        src_ver = stream.resolve_version(source_version)
        if src_ver is None:
            raise KeyError(f"Source version not found in seed template '{tname}'.")

        sw_dir = self.get_seeds_dir(scope, project_root=project_root, client_root=client_root) / sw
        src_ver_dir = sw_dir / src_ver.folder_rel
        src_primary = sw_dir / src_ver.primary_file

        ignores = list(ignore_patterns or [])
        non_ignored_files = (
            [
                p
                for p in src_ver_dir.rglob("*")
                if p.is_file()
                and not p.name.startswith(".")
                and ".backups" not in p.parts
                and not matches_any_pattern(p.relative_to(src_ver_dir).as_posix(), p.name, ignores)
            ]
            if src_ver_dir.is_dir()
            else []
        )
        source_target = src_ver_dir if len(non_ignored_files) > 1 else src_primary

        return self.add_version(
            software=sw,
            template_name=tname,
            source_path=source_target,
            scope=scope,
            comment=comment or f"Versioned up from {src_ver.version_label}",
            author=author or src_ver.author,
            source_label=f"cloned:{src_ver.version_label}",
            tags=list(src_ver.tags),
            set_as_default_template=False,
            set_as_default_version=set_as_default_version,
            project_root=project_root,
            client_root=client_root,
            ignore_patterns=ignores,
            lock_previous=lock_previous,
            preferred_extensions=preferred_extensions,
        )

    def set_default_template(
        self,
        software: str,
        template_name: str,
        scope: TemplateScope,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
    ) -> SoftwareSeedManifest:
        """Sets the default template stream for a software in a scope, or '' to inherit from parent scope."""
        sw = normalize_software_key(software)
        manifest = self.load_manifest(sw, scope, project_root=project_root, client_root=client_root)
        if not template_name.strip():
            manifest.default_template = ""
            sw_dir = self.get_seeds_dir(scope, project_root=project_root, client_root=client_root) / sw
            if sw_dir.is_dir():
                self.save_manifest(manifest, scope, project_root=project_root, client_root=client_root)
            return manifest

        tname = normalize_template_name(template_name)
        if tname not in manifest.templates:
            raise KeyError(f"Seed template '{tname}' not found for '{sw}' in scope '{scope.value}'.")
        manifest.default_template = tname
        self.save_manifest(manifest, scope, project_root=project_root, client_root=client_root)
        return manifest

    def set_default_version(
        self,
        software: str,
        template_name: str,
        version: int,
        scope: TemplateScope,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
    ) -> SeedTemplateStream:
        """Pins default_version to vNNN (>0) or sets Always Latest (0)."""
        sw = normalize_software_key(software)
        tname = normalize_template_name(template_name)
        manifest = self.load_manifest(sw, scope, project_root=project_root, client_root=client_root)
        stream = manifest.templates.get(tname)
        if stream is None:
            raise KeyError(f"Seed template '{tname}' not found for '{sw}' in scope '{scope.value}'.")
        if version > 0 and not any(v.version == version for v in stream.versions):
            raise KeyError(f"Version v{version:03d} not found in seed template '{tname}'.")
        stream.default_version = max(0, int(version))
        self.save_manifest(manifest, scope, project_root=project_root, client_root=client_root)
        return stream

    def update_metadata(
        self,
        software: str,
        template_name: str,
        scope: TemplateScope,
        *,
        description: Optional[str] = None,
        version: Optional[int] = None,
        comment: Optional[str] = None,
        tags: Optional[list[str]] = None,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
    ) -> SeedTemplateStream:
        sw = normalize_software_key(software)
        tname = normalize_template_name(template_name)
        manifest = self.load_manifest(sw, scope, project_root=project_root, client_root=client_root)
        stream = manifest.templates.get(tname)
        if stream is None:
            raise KeyError(f"Seed template '{tname}' not found for '{sw}' in scope '{scope.value}'.")
        if description is not None:
            stream.description = description
        if version is not None:
            vobj = next((v for v in stream.versions if v.version == version), None)
            if vobj is None:
                raise KeyError(f"Version v{version:03d} not found in seed template '{tname}'.")
            if comment is not None:
                vobj.comment = comment
            if tags is not None:
                vobj.tags = list(tags)
        self.save_manifest(manifest, scope, project_root=project_root, client_root=client_root)
        return stream

    def delete_version(
        self,
        software: str,
        template_name: str,
        version: int,
        scope: TemplateScope,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
    ) -> bool:
        sw = normalize_software_key(software)
        tname = normalize_template_name(template_name)
        manifest = self.load_manifest(sw, scope, project_root=project_root, client_root=client_root)
        stream = manifest.templates.get(tname)
        if stream is None:
            return False
        target = next((v for v in stream.versions if v.version == version), None)
        if target is None:
            return False

        sw_dir = self.get_seeds_dir(scope, project_root=project_root, client_root=client_root) / sw
        if target.folder_rel:
            safe_rmtree(sw_dir / target.folder_rel)
        stream.versions = [v for v in stream.versions if v.version != version]
        if stream.default_version == version:
            stream.default_version = 0
        if not stream.versions:
            safe_rmtree(sw_dir / tname)
            manifest.templates.pop(tname, None)
            if manifest.default_template == tname:
                manifest.default_template = ""

        self.save_manifest(manifest, scope, project_root=project_root, client_root=client_root)
        return True

    def delete_template(
        self,
        software: str,
        template_name: str,
        scope: TemplateScope,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
    ) -> bool:
        sw = normalize_software_key(software)
        tname = normalize_template_name(template_name)
        manifest = self.load_manifest(sw, scope, project_root=project_root, client_root=client_root)
        if tname not in manifest.templates:
            return False
        sw_dir = self.get_seeds_dir(scope, project_root=project_root, client_root=client_root) / sw
        safe_rmtree(sw_dir / tname)
        manifest.templates.pop(tname, None)
        if manifest.default_template == tname:
            manifest.default_template = ""
        self.save_manifest(manifest, scope, project_root=project_root, client_root=client_root)
        return True

    def _scope_chain(
        self,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
    ) -> list[TemplateScope]:
        scopes: list[TemplateScope] = []
        if project_root is not None:
            scopes.append(TemplateScope.PROJECT)
        if self.resolve_enclosing_client_root(project_root=project_root, client_root=client_root) is not None:
            scopes.append(TemplateScope.CLIENT_ROOT)
        scopes.append(TemplateScope.GLOBAL)
        return scopes

    def _build_resolved(
        self,
        scope: TemplateScope,
        scope_root: Path,
        sw_dir: Path,
        stream: SeedTemplateStream,
        ver_obj: SeedTemplateVersion,
        is_pinned: bool,
    ) -> Optional[ResolvedSeedTemplate]:
        primary_abs = sw_dir / ver_obj.primary_file
        if not primary_abs.is_file():
            return None
        ver_dir = sw_dir / ver_obj.folder_rel if ver_obj.folder_rel else primary_abs.parent
        return ResolvedSeedTemplate(
            scope=scope,
            scope_root=scope_root,
            software=stream.software,
            template_name=stream.name,
            version=ver_obj.version,
            primary_file=primary_abs,
            version_dir=ver_dir if ver_dir.is_dir() else None,
            is_legacy=False,
            is_pinned_version=is_pinned,
            comment=ver_obj.comment,
        )

    def list_available_for_software(
        self,
        software: str,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
        legacy_files_dir: Optional[Path] = None,
        preferred_extensions: Optional[list[str]] = None,
        include_all_versions: bool = False,
    ) -> list[ResolvedSeedTemplate]:
        """Lists available seed templates for a software across Project -> Root -> Global -> Legacy."""
        sw = normalize_software_key(software)
        results: list[ResolvedSeedTemplate] = []

        for scope in self._scope_chain(project_root=project_root, client_root=client_root):
            try:
                scope_root = self.get_scope_root(scope, project_root=project_root, client_root=client_root)
                sw_dir = self.get_seeds_dir(scope, project_root=project_root, client_root=client_root) / sw
            except ValueError:
                continue
            manifest = self.load_manifest(
                sw,
                scope,
                project_root=project_root,
                client_root=client_root,
                preferred_extensions=preferred_extensions,
            )
            for tname in sorted(manifest.templates.keys()):
                stream = manifest.templates[tname]
                if include_all_versions:
                    for vobj in sorted(stream.versions, key=lambda v: v.version, reverse=True):
                        r = self._build_resolved(
                            scope,
                            scope_root,
                            sw_dir,
                            stream,
                            vobj,
                            is_pinned=(stream.default_version == vobj.version and stream.default_version > 0),
                        )
                        if r is not None:
                            results.append(r)
                else:
                    vobj = stream.resolve_version()
                    if vobj is not None:
                        r = self._build_resolved(
                            scope,
                            scope_root,
                            sw_dir,
                            stream,
                            vobj,
                            is_pinned=(stream.default_version > 0),
                        )
                        if r is not None:
                            results.append(r)

        if legacy_files_dir and legacy_files_dir.is_dir():
            pref_exts = {e.lower() for e in (preferred_extensions or [])}
            for f in sorted(legacy_files_dir.iterdir()):
                if not f.is_file() or f.name.startswith("."):
                    continue
                if pref_exts and f.suffix.lower() not in pref_exts:
                    continue
                results.append(
                    ResolvedSeedTemplate(
                        scope=TemplateScope.GLOBAL,
                        scope_root=self.hub_dir,
                        software=sw,
                        template_name=f.name,
                        version=0,
                        primary_file=f,
                        version_dir=None,
                        is_legacy=True,
                        is_pinned_version=False,
                        comment="Legacy seed file",
                    )
                )

        return results

    def _resolve_explicit_ref(
        self,
        ref: str,
        software: str,
        *,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
        legacy_files_dir: Optional[Path] = None,
    ) -> Optional[ResolvedSeedTemplate]:
        parsed = parse_seed_spec(ref)
        if parsed is not None:
            scope, spec_sw, tname, req_ver = parsed
            try:
                scope_root = self.get_scope_root(scope, project_root=project_root, client_root=client_root)
                sw_dir = self.get_seeds_dir(scope, project_root=project_root, client_root=client_root) / spec_sw
            except ValueError:
                return None
            manifest = self.load_manifest(spec_sw, scope, project_root=project_root, client_root=client_root)
            stream = manifest.templates.get(tname)
            if stream is None:
                return None
            vobj = stream.resolve_version(req_ver)
            if vobj is None:
                return None
            return self._build_resolved(
                scope,
                scope_root,
                sw_dir,
                stream,
                vobj,
                is_pinned=(req_ver is not None or stream.default_version > 0),
            )

        # Legacy filename lookup inside ~/.obol/templates/files/
        if legacy_files_dir is not None:
            candidate = legacy_files_dir / ref
            if candidate.is_file():
                return ResolvedSeedTemplate(
                    scope=TemplateScope.GLOBAL,
                    scope_root=self.hub_dir,
                    software=normalize_software_key(software),
                    template_name=ref,
                    version=0,
                    primary_file=candidate,
                    version_dir=None,
                    is_legacy=True,
                    is_pinned_version=False,
                    comment="Legacy seed file",
                )

        # Bare stream name lookup ('podcast_mix' or 'podcast_mix:v002') across cascade
        sw = normalize_software_key(software)
        bare_name = ref
        req_ver_bare: Optional[int] = None
        if ":" in ref:
            lhs, rhs = ref.rsplit(":", 1)
            m = re.match(r"^v?(\d+)$", rhs.strip(), re.IGNORECASE)
            if m:
                bare_name = lhs
                req_ver_bare = int(m.group(1))
        tname = normalize_template_name(bare_name)
        for scope in self._scope_chain(project_root=project_root, client_root=client_root):
            try:
                scope_root = self.get_scope_root(scope, project_root=project_root, client_root=client_root)
                sw_dir = self.get_seeds_dir(scope, project_root=project_root, client_root=client_root) / sw
            except ValueError:
                continue
            manifest = self.load_manifest(sw, scope, project_root=project_root, client_root=client_root)
            stream = manifest.templates.get(tname)
            if stream is None:
                continue
            vobj = stream.resolve_version(req_ver_bare)
            if vobj is not None:
                return self._build_resolved(
                    scope,
                    scope_root,
                    sw_dir,
                    stream,
                    vobj,
                    is_pinned=(req_ver_bare is not None or stream.default_version > 0),
                )
        return None

    def resolve_seed_template(
        self,
        software: str,
        *,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
        task_template: Optional[TaskTemplate] = None,
        seed_override: Optional[str] = None,
        legacy_files_dir: Optional[Path] = None,
    ) -> Optional[ResolvedSeedTemplate]:
        """Resolves the initial seed template via explicit override -> TaskTemplate spec -> Project -> Root -> Global -> Legacy."""
        sw = normalize_software_key(software)
        task_matches_software = (
            task_template is not None and normalize_software_key(task_template.software) == sw
        )

        # 1. Explicit override (from wizard, Add Component, CLI --seed, etc.)
        if seed_override and seed_override.strip():
            resolved = self._resolve_explicit_ref(
                seed_override.strip(),
                sw,
                project_root=project_root,
                client_root=client_root,
                legacy_files_dir=legacy_files_dir,
            )
            if resolved is not None:
                return resolved

        # 2. TaskTemplate explicitly bound to a scoped seed:... spec
        if task_matches_software and task_template and task_template.seed_file_template.startswith("seed:"):
            resolved = self._resolve_explicit_ref(
                task_template.seed_file_template,
                sw,
                project_root=project_root,
                client_root=client_root,
                legacy_files_dir=legacy_files_dir,
            )
            if resolved is not None:
                return resolved

        # 3. Scoped cascade: Project -> Client Workspace Root -> Global versioned seeds
        for scope in self._scope_chain(project_root=project_root, client_root=client_root):
            try:
                scope_root = self.get_scope_root(scope, project_root=project_root, client_root=client_root)
                sw_dir = self.get_seeds_dir(scope, project_root=project_root, client_root=client_root) / sw
            except ValueError:
                continue
            manifest = self.load_manifest(sw, scope, project_root=project_root, client_root=client_root)
            if manifest.default_template and manifest.default_template in manifest.templates:
                stream = manifest.templates[manifest.default_template]
                vobj = stream.resolve_version()
                if vobj is not None:
                    resolved = self._build_resolved(
                        scope,
                        scope_root,
                        sw_dir,
                        stream,
                        vobj,
                        is_pinned=(stream.default_version > 0),
                    )
                    if resolved is not None:
                        return resolved

        # 4. Legacy flat file fallback from TaskTemplate.seed_file_template
        if task_matches_software and task_template and task_template.seed_file_template and legacy_files_dir is not None:
            candidate = legacy_files_dir / task_template.seed_file_template
            if candidate.is_file():
                return ResolvedSeedTemplate(
                    scope=TemplateScope.GLOBAL,
                    scope_root=self.hub_dir,
                    software=sw,
                    template_name=task_template.seed_file_template,
                    version=0,
                    primary_file=candidate,
                    version_dir=None,
                    is_legacy=True,
                    is_pinned_version=False,
                    comment="Legacy seed file",
                )

        return None
