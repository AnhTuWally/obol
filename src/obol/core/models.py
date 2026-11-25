"""Domain models, enums, and TOML-safe serialization for Obol."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Optional


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_for_toml(obj: Any) -> Any:
    """Recursively strips None values so tomli_w can serialize cleanly."""
    if isinstance(obj, dict):
        return {str(k): clean_for_toml(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, (list, tuple)):
        return [clean_for_toml(v) for v in obj if v is not None]
    if isinstance(obj, Enum):
        return obj.value
    return obj


class VersionUpStrategy(str, Enum):
    CLONE_PRIMARY_FILE = "clone_primary_file"
    CLONE_ENTIRE_FOLDER = "clone_entire_folder"
    COPY_WITH_SIDECARS = "copy_with_sidecars"
    FROM_SEED_TEMPLATE = "from_seed_template"


class FilePresence(str, Enum):
    LOCAL_ONLY = "Local Only"
    SYNCED = "Backed Up"
    MODIFIED_LOCAL = "Modified Locally"
    REMOTE_ONLY = "On Server"
    SPARSE = "Sparse Checkout"


class VaultStatus(str, Enum):
    DISABLED = "disabled"
    ONLINE = "online"
    OFFLINE = "offline"


@dataclass
class SoftwareProfile:
    key: str
    display_name: str
    executable: str = ""
    extensions: list[str] = field(default_factory=list)
    launch_args: list[str] = field(default_factory=lambda: ["{file}"])
    version_up_strategy: VersionUpStrategy = VersionUpStrategy.CLONE_PRIMARY_FILE
    naming_pattern: str = "{component_leaf}_{version}{ext}"
    ignore_patterns: list[str] = field(default_factory=list)
    sidecar_extensions: list[str] = field(default_factory=list)
    lock_previous_versions: bool = True
    plugin: str = "generic"

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "key": self.key,
            "display_name": self.display_name,
            "executable": self.executable,
            "extensions": self.extensions,
            "launch_args": self.launch_args,
            "version_up_strategy": self.version_up_strategy.value,
            "naming_pattern": self.naming_pattern,
            "ignore_patterns": self.ignore_patterns,
            "sidecar_extensions": self.sidecar_extensions,
            "lock_previous_versions": self.lock_previous_versions,
            "plugin": self.plugin,
        })

    @classmethod
    def from_dict(cls, key: str, data: dict[str, Any]) -> SoftwareProfile:
        strat_raw = data.get("version_up_strategy", VersionUpStrategy.CLONE_PRIMARY_FILE.value)
        try:
            strat = VersionUpStrategy(strat_raw)
        except ValueError:
            strat = VersionUpStrategy.CLONE_PRIMARY_FILE
        return cls(
            key=data.get("key", key),
            display_name=data.get("display_name", key.title()),
            executable=data.get("executable", ""),
            extensions=list(data.get("extensions", [])),
            launch_args=list(data.get("launch_args", ["{file}"])),
            version_up_strategy=strat,
            naming_pattern=data.get("naming_pattern", "{component_leaf}_{version}{ext}"),
            ignore_patterns=list(data.get("ignore_patterns", [])),
            sidecar_extensions=list(data.get("sidecar_extensions", [])),
            lock_previous_versions=bool(data.get("lock_previous_versions", True)),
            plugin=data.get("plugin", "generic"),
        )


def default_software_profiles() -> dict[str, SoftwareProfile]:
    return {
        "reaper": SoftwareProfile(
            key="reaper",
            display_name="REAPER DAW",
            executable="",
            extensions=[".rpp"],
            launch_args=["{file}"],
            version_up_strategy=VersionUpStrategy.CLONE_PRIMARY_FILE,
            ignore_patterns=["*.rpp-bak", "*.reapeaks", ".backups/**"],
            lock_previous_versions=True,
            plugin="reaper",
        ),
        "blender": SoftwareProfile(
            key="blender",
            display_name="Blender 3D",
            executable="",
            extensions=[".blend"],
            launch_args=["{file}"],
            version_up_strategy=VersionUpStrategy.CLONE_PRIMARY_FILE,
            ignore_patterns=["*.blend1", "*.blend2"],
            lock_previous_versions=True,
            plugin="blender",
        ),
        "ableton": SoftwareProfile(
            key="ableton",
            display_name="Ableton Live",
            executable="",
            extensions=[".als"],
            launch_args=["{file}"],
            version_up_strategy=VersionUpStrategy.CLONE_ENTIRE_FOLDER,
            ignore_patterns=["Backup/**"],
            lock_previous_versions=False,
            plugin="generic",
        ),
        "krita": SoftwareProfile(
            key="krita",
            display_name="Krita / 2D Art",
            executable="",
            extensions=[".kra", ".psd"],
            launch_args=["{file}"],
            version_up_strategy=VersionUpStrategy.CLONE_PRIMARY_FILE,
            ignore_patterns=["*.kra~"],
            lock_previous_versions=True,
            plugin="generic",
        ),
        "resolve": SoftwareProfile(
            key="resolve",
            display_name="DaVinci Resolve",
            executable="",
            extensions=[".drp"],
            launch_args=["{file}"],
            version_up_strategy=VersionUpStrategy.CLONE_PRIMARY_FILE,
            ignore_patterns=["*.drp-bak", "CacheClip/**"],
            lock_previous_versions=True,
            plugin="resolve",
        ),
        "filmcraft": SoftwareProfile(
            key="filmcraft",
            display_name="FilmCraft",
            executable="",
            extensions=[".fcproj"],
            launch_args=["{file}"],
            version_up_strategy=VersionUpStrategy.CLONE_PRIMARY_FILE,
            ignore_patterns=["*.fcproj-bak", "*.fcproj.tmp", "Recovery/**", ".backups/**"],
            lock_previous_versions=True,
            plugin="filmcraft",
        ),
        "generic": SoftwareProfile(
            key="generic",
            display_name="Generic / OS Default",
            executable="",
            extensions=[".txt", ".md", ".mp4", ".wav"],
            launch_args=["{file}"],
            version_up_strategy=VersionUpStrategy.CLONE_PRIMARY_FILE,
            ignore_patterns=[],
            lock_previous_versions=False,
            plugin="generic",
        ),
    }


@dataclass
class VaultConfig:
    enabled: bool = False
    path: str = ""
    auto_sync_metadata: bool = True

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "enabled": self.enabled,
            "path": self.path,
            "auto_sync_metadata": self.auto_sync_metadata,
        })

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VaultConfig:
        return cls(
            enabled=bool(data.get("enabled", False)),
            path=str(data.get("path", "")),
            auto_sync_metadata=bool(data.get("auto_sync_metadata", True)),
        )


@dataclass
class GlobalConfig:
    default_workspace_root: str = ""
    last_active_project: str = ""
    author_name: str = ""
    fast_drop: bool = False
    pinned_dcc_target: str = ""
    last_used_dcc_target: str = ""
    dcc_default_modes: dict[str, str] = field(default_factory=dict)
    vault: VaultConfig = field(default_factory=VaultConfig)
    software: dict[str, SoftwareProfile] = field(default_factory=default_software_profiles)

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "default_workspace_root": self.default_workspace_root,
            "last_active_project": self.last_active_project,
            "author_name": self.author_name,
            "fast_drop": self.fast_drop,
            "pinned_dcc_target": self.pinned_dcc_target,
            "last_used_dcc_target": self.last_used_dcc_target,
            "dcc_default_modes": self.dcc_default_modes,
            "vault": self.vault.to_dict(),
            "software": {k: v.to_dict() for k, v in self.software.items()},
        })

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> GlobalConfig:
        sw_raw = data.get("software", {})
        sw = default_software_profiles()
        for k, v in sw_raw.items():
            if isinstance(v, dict):
                sw[k] = SoftwareProfile.from_dict(k, v)
        modes_raw = data.get("dcc_default_modes", {})
        dcc_default_modes: dict[str, str] = {}
        if isinstance(modes_raw, dict):
            for k, v in modes_raw.items():
                ks = str(k).strip()
                vs = str(v).strip()
                if ks and vs:
                    dcc_default_modes[ks] = vs
        return cls(
            default_workspace_root=str(data.get("default_workspace_root", "")),
            last_active_project=str(data.get("last_active_project", "")),
            author_name=str(data.get("author_name", "")),
            fast_drop=bool(data.get("fast_drop", False)),
            pinned_dcc_target=str(data.get("pinned_dcc_target", "")),
            last_used_dcc_target=str(data.get("last_used_dcc_target", "")),
            dcc_default_modes=dcc_default_modes,
            vault=VaultConfig.from_dict(data.get("vault", {})),
            software=sw,
        )


@dataclass
class ProjectRegistryEntry:
    name: str
    code: str
    local_path: str
    created_at: str = field(default_factory=utc_now_iso)
    status: str = "active"
    template_used: str = "simple_media"

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "name": self.name,
            "code": self.code,
            "local_path": self.local_path,
            "created_at": self.created_at,
            "status": self.status,
            "template_used": self.template_used,
        })

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectRegistryEntry:
        return cls(
            name=str(data["name"]),
            code=str(data.get("code", data["name"][:3].lower())),
            local_path=str(data.get("local_path", "")),
            created_at=str(data.get("created_at", utc_now_iso())),
            status=str(data.get("status", "active")),
            template_used=str(data.get("template_used", "simple_media")),
        )


@dataclass
class TrackedFile:
    rel_path: str
    sha256: str
    size_bytes: int
    file_type: str = ""
    output_label: str = ""

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "rel_path": self.rel_path,
            "sha256": self.sha256,
            "size_bytes": int(self.size_bytes),
            "file_type": self.file_type or Path(self.rel_path).suffix.lstrip(".").lower(),
            "output_label": self.output_label,
        })

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TrackedFile:
        rel_path = str(data["rel_path"])
        return cls(
            rel_path=rel_path,
            sha256=str(data.get("sha256", "")),
            size_bytes=int(data.get("size_bytes", 0)),
            file_type=str(data.get("file_type", Path(rel_path).suffix.lstrip(".").lower())),
            output_label=str(data.get("output_label", "")),
        )


@dataclass
class PublishItem:
    """Represents a staged deliverable file to publish inside a version bundle."""

    source_path: Path
    output_label: str = ""
    keep_original_name: bool = False


@dataclass
class VersionBundle:
    """Represents a single v001/, v002/ folder inside working/, published/, or repo/<asset>/."""

    version: int
    folder_rel: str
    primary_file: str
    files: list[TrackedFile] = field(default_factory=list)
    created_at: str = field(default_factory=utc_now_iso)
    comment: str = ""
    parent_version: int = 0
    source_working_version: int = 0
    tags: list[str] = field(default_factory=list)
    custom: dict[str, Any] = field(default_factory=dict)

    @property
    def version_label(self) -> str:
        return f"v{self.version:03d}"

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "version": int(self.version),
            "folder_rel": self.folder_rel,
            "primary_file": self.primary_file,
            "files": [f.to_dict() for f in self.files],
            "created_at": self.created_at,
            "comment": self.comment,
            "parent_version": int(self.parent_version),
            "source_working_version": int(self.source_working_version),
            "tags": self.tags,
            "custom": self.custom,
        })

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> VersionBundle:
        return cls(
            version=int(data["version"]),
            folder_rel=str(data["folder_rel"]),
            primary_file=str(data.get("primary_file", "")),
            files=[TrackedFile.from_dict(f) for f in data.get("files", [])],
            created_at=str(data.get("created_at", "")),
            comment=str(data.get("comment", "")),
            parent_version=int(data.get("parent_version", 0)),
            source_working_version=int(data.get("source_working_version", 0)),
            tags=list(data.get("tags", [])),
            custom=dict(data.get("custom", {})),
        )


@dataclass
class RepoAsset:
    """Represents a named external asset inside repo/ (e.g., 'lead_vocal' or 'audio/lead_vocal')."""

    asset_path: str
    name: str
    category: str = ""
    author: str = ""
    source: str = ""
    license: str = ""
    tags: list[str] = field(default_factory=list)
    custom: dict[str, Any] = field(default_factory=dict)
    latest_version: int = 0
    used_by_working_versions: list[int] = field(default_factory=list)
    versions: list[VersionBundle] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "asset_path": self.asset_path,
            "name": self.name,
            "category": self.category,
            "author": self.author,
            "source": self.source,
            "license": self.license,
            "tags": self.tags,
            "custom": self.custom,
            "latest_version": int(self.latest_version),
            "used_by_working_versions": [int(v) for v in self.used_by_working_versions],
            "versions": [v.to_dict() for v in self.versions],
        })

    @classmethod
    def from_dict(cls, asset_path: str, data: dict[str, Any]) -> RepoAsset:
        versions = [VersionBundle.from_dict(v) for v in data.get("versions", [])]
        versions.sort(key=lambda v: v.version)
        latest = int(data.get("latest_version", versions[-1].version if versions else 0))
        return cls(
            asset_path=str(data.get("asset_path", asset_path)),
            name=str(data.get("name", asset_path.split("/")[-1])),
            category=str(data.get("category", "")),
            author=str(data.get("author", "")),
            source=str(data.get("source", "")),
            license=str(data.get("license", "")),
            tags=list(data.get("tags", [])),
            custom=dict(data.get("custom", {})),
            latest_version=latest,
            used_by_working_versions=[int(x) for x in data.get("used_by_working_versions", [])],
            versions=versions,
        )


@dataclass
class PublishedDeliverable:
    """Represents a named deliverable stream inside published/ (e.g., 'main', 'stems', 'midi_pack')."""

    deliverable_path: str
    name: str
    description: str = ""
    tags: list[str] = field(default_factory=list)
    custom: dict[str, Any] = field(default_factory=dict)
    latest_version: int = 0
    versions: list[VersionBundle] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "deliverable_path": self.deliverable_path,
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "custom": self.custom,
            "latest_version": int(self.latest_version),
            "versions": [v.to_dict() for v in sorted(self.versions, key=lambda x: x.version)],
        })

    @classmethod
    def from_dict(cls, deliverable_path: str, data: dict[str, Any]) -> PublishedDeliverable:
        versions = [VersionBundle.from_dict(v) for v in data.get("versions", [])]
        versions.sort(key=lambda v: v.version)
        latest = int(data.get("latest_version", versions[-1].version if versions else 0))
        return cls(
            deliverable_path=str(data.get("deliverable_path", deliverable_path)),
            name=str(data.get("name", deliverable_path.split("/")[-1])),
            description=str(data.get("description", "")),
            tags=list(data.get("tags", [])),
            custom=dict(data.get("custom", {})),
            latest_version=latest,
            versions=versions,
        )


@dataclass
class ComponentSnapshot:
    """In-memory representation of .obol/tasks/<component_slug>/snapshot.toml."""

    component_path: str
    task_template: str = "reaper_daw"
    software: str = "reaper"
    created_at: str = field(default_factory=utc_now_iso)
    working_versions: list[VersionBundle] = field(default_factory=list)
    published_versions: list[VersionBundle] = field(default_factory=list)
    published_deliverables: dict[str, PublishedDeliverable] = field(default_factory=dict)
    repo_assets: dict[str, RepoAsset] = field(default_factory=dict)
    repo_routes: dict[str, list[str]] = field(default_factory=lambda: {
        "audio": [".wav", ".flac", ".mp3", ".ogg", ".aiff"],
        "midi": [".mid", ".midi"],
        "references": [".mp4", ".mov", ".pdf", ".txt", ".png", ".jpg"],
    })
    last_event_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "component_path": self.component_path,
            "task_template": self.task_template,
            "software": self.software,
            "created_at": self.created_at,
            "last_event_id": self.last_event_id,
            "repo_routes": self.repo_routes,
            "working_versions": [v.to_dict() for v in sorted(self.working_versions, key=lambda x: x.version)],
            "published_versions": [v.to_dict() for v in sorted(self.published_versions, key=lambda x: x.version)],
            "published_deliverables": {k: v.to_dict() for k, v in sorted(self.published_deliverables.items())},
            "repo_assets": {k: v.to_dict() for k, v in sorted(self.repo_assets.items())},
        })

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ComponentSnapshot:
        w_vers = [VersionBundle.from_dict(v) for v in data.get("working_versions", [])]
        w_vers.sort(key=lambda x: x.version)
        p_vers = [VersionBundle.from_dict(v) for v in data.get("published_versions", [])]
        p_vers.sort(key=lambda x: x.version)
        p_delivs = {
            k: PublishedDeliverable.from_dict(k, v)
            for k, v in data.get("published_deliverables", {}).items()
            if isinstance(v, dict)
        }
        r_assets = {
            k: RepoAsset.from_dict(k, v)
            for k, v in data.get("repo_assets", {}).items()
            if isinstance(v, dict)
        }
        default_routes = {
            "audio": [".wav", ".flac", ".mp3", ".ogg", ".aiff"],
            "midi": [".mid", ".midi"],
            "references": [".mp4", ".mov", ".pdf", ".txt", ".png", ".jpg"],
        }
        return cls(
            component_path=str(data.get("component_path", "")),
            task_template=str(data.get("task_template", "reaper_daw")),
            software=str(data.get("software", "reaper")),
            created_at=str(data.get("created_at", utc_now_iso())),
            working_versions=w_vers,
            published_versions=p_vers,
            published_deliverables=p_delivs,
            repo_assets=r_assets,
            repo_routes=dict(data.get("repo_routes", default_routes)),
            last_event_id=str(data.get("last_event_id", "")),
        )


@dataclass
class ProjectConfig:
    name: str
    code: str
    schema_version: int = 1
    created_at: str = field(default_factory=utc_now_iso)
    template_used: str = "simple_media"
    shared_folders: list[str] = field(default_factory=lambda: ["_repo"])
    version_padding: int = 3
    lock_previous_versions: bool = True

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "project": {
                "name": self.name,
                "code": self.code,
                "schema_version": self.schema_version,
                "created_at": self.created_at,
                "template_used": self.template_used,
                "shared_folders": self.shared_folders,
                "version_padding": self.version_padding,
                "lock_previous_versions": self.lock_previous_versions,
            }
        })

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ProjectConfig:
        p = data.get("project", data)
        name = str(p.get("name", "Untitled"))
        return cls(
            name=name,
            code=str(p.get("code", name[:3].lower())),
            schema_version=int(p.get("schema_version", 1)),
            created_at=str(p.get("created_at", utc_now_iso())),
            template_used=str(p.get("template_used", "simple_media")),
            shared_folders=list(p.get("shared_folders", ["_repo"])),
            version_padding=int(p.get("version_padding", 3)),
            lock_previous_versions=bool(p.get("lock_previous_versions", True)),
        )


@dataclass
class LedgerEvent:
    id: str
    ts: str
    event: str
    component: str
    payload: dict[str, Any] = field(default_factory=dict)
    crc: str = ""


@dataclass
class ProjectTemplate:
    name: str
    description: str
    default_components: list[str] = field(default_factory=list)
    component_templates: dict[str, str] = field(default_factory=dict)
    shared_folders: list[str] = field(default_factory=lambda: ["_repo"])

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "name": self.name,
            "description": self.description,
            "default_components": self.default_components,
            "component_templates": self.component_templates,
            "shared_folders": self.shared_folders,
        })

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> ProjectTemplate:
        return cls(
            name=str(data.get("name", name)),
            description=str(data.get("description", "")),
            default_components=list(data.get("default_components", ["audio", "video"])),
            component_templates=dict(data.get("component_templates", {})),
            shared_folders=list(data.get("shared_folders", ["_repo"])),
        )


@dataclass
class TaskTemplate:
    name: str
    description: str
    software: str = "reaper"
    seed_file_template: str = "reaper_default.rpp"
    repo_routes: dict[str, list[str]] = field(default_factory=lambda: {
        "audio": [".wav", ".flac", ".mp3", ".ogg", ".aiff"],
        "midi": [".mid", ".midi"],
        "references": [".mp4", ".mov", ".pdf", ".txt", ".png", ".jpg"],
    })
    extra_working_dirs: list[str] = field(default_factory=list)
    publish_formats: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "name": self.name,
            "description": self.description,
            "software": self.software,
            "seed_file_template": self.seed_file_template,
            "repo_routes": self.repo_routes,
            "extra_working_dirs": self.extra_working_dirs,
            "publish_formats": self.publish_formats,
        })

    @classmethod
    def from_dict(cls, name: str, data: dict[str, Any]) -> TaskTemplate:
        return cls(
            name=str(data.get("name", name)),
            description=str(data.get("description", "")),
            software=str(data.get("software", "reaper")),
            seed_file_template=str(data.get("seed_file_template", "")),
            repo_routes=dict(data.get("repo_routes", {
                "audio": [".wav", ".flac", ".mp3", ".ogg", ".aiff"],
                "midi": [".mid", ".midi"],
                "references": [".mp4", ".mov", ".pdf", ".txt", ".png", ".jpg"],
            })),
            extra_working_dirs=list(data.get("extra_working_dirs", [])),
            publish_formats=list(data.get("publish_formats", [])),
        )


@dataclass
class FileTemplate:
    name: str
    extension: str
    path: str
    description: str = ""


class TemplateScope(str, Enum):
    PROJECT = "project"
    CLIENT_ROOT = "client_root"
    GLOBAL = "global"


@dataclass
class SeedTemplateVersion:
    """Represents a single vNNN/ version of a named software seed template."""

    version: int
    folder_rel: str
    primary_file: str
    sha256: str = ""
    size_bytes: int = 0
    created_at: str = field(default_factory=utc_now_iso)
    comment: str = ""
    author: str = ""
    source: str = ""
    tags: list[str] = field(default_factory=list)

    @property
    def version_label(self) -> str:
        return f"v{self.version:03d}"

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "version": int(self.version),
            "folder_rel": self.folder_rel,
            "primary_file": self.primary_file,
            "sha256": self.sha256,
            "size_bytes": int(self.size_bytes),
            "created_at": self.created_at,
            "comment": self.comment,
            "author": self.author,
            "source": self.source,
            "tags": self.tags,
        })

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SeedTemplateVersion:
        return cls(
            version=int(data["version"]),
            folder_rel=str(data.get("folder_rel", "")),
            primary_file=str(data.get("primary_file", "")),
            sha256=str(data.get("sha256", "")),
            size_bytes=int(data.get("size_bytes", 0)),
            created_at=str(data.get("created_at", "")),
            comment=str(data.get("comment", "")),
            author=str(data.get("author", "")),
            source=str(data.get("source", "")),
            tags=list(data.get("tags", [])),
        )


@dataclass
class SeedTemplateStream:
    """Represents a named versioned seed template stream (e.g., 'default', 'podcast_mix') for one software."""

    name: str
    software: str
    description: str = ""
    default_version: int = 0  # 0 = Always Latest; >0 = pinned to vNNN
    versions: list[SeedTemplateVersion] = field(default_factory=list)

    def latest_version_number(self) -> int:
        return max((v.version for v in self.versions), default=0)

    def resolve_version(self, version: Optional[int] = None) -> Optional[SeedTemplateVersion]:
        if not self.versions:
            return None
        sorted_vers = sorted(self.versions, key=lambda v: v.version)
        if version is not None and version > 0:
            return next((v for v in sorted_vers if v.version == version), None)
        if self.default_version > 0:
            pinned = next((v for v in sorted_vers if v.version == self.default_version), None)
            if pinned is not None:
                return pinned
        return sorted_vers[-1]

    def to_dict(self) -> dict[str, Any]:
        sorted_vers = sorted(self.versions, key=lambda v: v.version)
        return clean_for_toml({
            "name": self.name,
            "software": self.software,
            "description": self.description,
            "default_version": int(self.default_version),
            "versions": [v.to_dict() for v in sorted_vers],
        })

    @classmethod
    def from_dict(cls, name: str, software: str, data: dict[str, Any]) -> SeedTemplateStream:
        versions = [SeedTemplateVersion.from_dict(v) for v in data.get("versions", []) if isinstance(v, dict)]
        versions.sort(key=lambda v: v.version)
        return cls(
            name=str(data.get("name", name)),
            software=str(data.get("software", software)),
            description=str(data.get("description", "")),
            default_version=int(data.get("default_version", 0)),
            versions=versions,
        )


@dataclass
class SoftwareSeedManifest:
    """Manifest stored at <seeds_dir>/<software>/manifest.toml."""

    software: str
    schema_version: int = 1
    default_template: str = ""  # "" = inherit from parent scope
    templates: dict[str, SeedTemplateStream] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return clean_for_toml({
            "software": self.software,
            "schema_version": int(self.schema_version),
            "default_template": self.default_template,
            "templates": {k: v.to_dict() for k, v in sorted(self.templates.items())},
        })

    @classmethod
    def from_dict(cls, software: str, data: dict[str, Any]) -> SoftwareSeedManifest:
        sw = str(data.get("software", software))
        raw_tpls = data.get("templates", {})
        templates: dict[str, SeedTemplateStream] = {}
        if isinstance(raw_tpls, dict):
            for k, v in raw_tpls.items():
                if isinstance(v, dict):
                    templates[str(k)] = SeedTemplateStream.from_dict(str(k), sw, v)
        return cls(
            software=sw,
            schema_version=int(data.get("schema_version", 1)),
            default_template=str(data.get("default_template", "")),
            templates=templates,
        )


@dataclass
class ResolvedSeedTemplate:
    """Resolved seed template reference returned by the Project -> Root -> Global cascade."""

    scope: TemplateScope
    scope_root: Path
    software: str
    template_name: str
    version: int
    primary_file: Path
    version_dir: Optional[Path] = None
    is_legacy: bool = False
    is_pinned_version: bool = False
    comment: str = ""

    def stream_spec(self) -> str:
        if self.is_legacy:
            return self.template_name
        return f"seed:{self.scope.value}:{self.software}:{self.template_name}"

    def reference_spec(self) -> str:
        if self.is_legacy:
            return self.template_name
        return f"seed:{self.scope.value}:{self.software}:{self.template_name}:v{self.version:03d}"

    def display_label(self) -> str:
        scope_map = {
            TemplateScope.PROJECT: "Project",
            TemplateScope.CLIENT_ROOT: "Root",
            TemplateScope.GLOBAL: "Global",
        }
        scope_lbl = scope_map.get(self.scope, self.scope.value)
        if self.is_legacy:
            return f"[{scope_lbl}] {self.template_name}"
        mode = "pinned" if self.is_pinned_version else "latest"
        return f"[{scope_lbl}] {self.template_name} v{self.version:03d} ({mode})"


