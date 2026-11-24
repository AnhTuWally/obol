"""Multi-Project Global Hub, Software Profile Manager, and Optional Server Vault Connector."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

from obol.core.models import (
    FilePresence,
    GlobalConfig,
    ProjectConfig,
    ProjectRegistryEntry,
    SoftwareProfile,
    VaultStatus,
)
from obol.core.storage import atomic_write_toml, load_toml_with_fallback, safe_rmtree
from obol.core.templates import TemplateManager
from obol.plugins.base import PluginRegistry


def get_default_hub_dir() -> Path:
    env_home = os.environ.get("OBOL_HOME")
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".obol").resolve()


class ObolHub:
    """Manages global configuration, software profiles, multi-project registry, and optional Server Vault."""

    def __init__(self, hub_dir: Optional[Path] = None) -> None:
        self.hub_dir = (hub_dir or get_default_hub_dir()).resolve()
        self.config_path = self.hub_dir / "config.toml"
        self.projects_path = self.hub_dir / "projects.toml"
        self.plugins_dir = self.hub_dir / "plugins"
        self.hub_dir.mkdir(parents=True, exist_ok=True)
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

        self.config = self._load_config()
        self._projects = self._load_projects()
        self.plugin_registry = PluginRegistry(user_plugins_dir=self.plugins_dir)
        self.template_manager = TemplateManager(
            hub_dir=self.hub_dir,
            vault_dir=self.get_vault_path() if self.get_vault_status() == VaultStatus.ONLINE else None,
            client_root=self.get_client_root(),
        )

    def _load_config(self) -> GlobalConfig:
        raw = load_toml_with_fallback(self.config_path)
        if raw is not None:
            return GlobalConfig.from_dict(raw)
        cfg = GlobalConfig(default_workspace_root=str(Path.home() / "ObolProjects"))
        atomic_write_toml(self.config_path, cfg.to_dict())
        return cfg

    def save_config(self) -> None:
        atomic_write_toml(self.config_path, self.config.to_dict())

    def _load_projects(self) -> dict[str, ProjectRegistryEntry]:
        raw = load_toml_with_fallback(self.projects_path)
        entries: dict[str, ProjectRegistryEntry] = {}
        if raw and isinstance(raw.get("projects"), dict):
            for name, pdata in raw["projects"].items():
                if isinstance(pdata, dict):
                    entries[name] = ProjectRegistryEntry.from_dict(pdata)
        return entries

    def save_projects(self) -> None:
        data = {"projects": {k: v.to_dict() for k, v in sorted(self._projects.items())}}
        atomic_write_toml(self.projects_path, data)

    def get_client_root(self) -> Path:
        """Returns the remembered local Client Workspace Root directory."""
        if self.config.default_workspace_root:
            return Path(self.config.default_workspace_root).resolve()
        default_root = (Path.home() / "ObolProjects").resolve()
        self.config.default_workspace_root = str(default_root)
        self.save_config()
        return default_root

    def set_client_root(self, root_path: Path) -> Path:
        """Sets and persists the Client Workspace Root in config.toml and discovers any projects inside it."""
        resolved = root_path.expanduser().resolve()
        resolved.mkdir(parents=True, exist_ok=True)
        self.config.default_workspace_root = str(resolved)
        self.save_config()

        # Update local_path for any remote-only projects that have not been checked out yet
        changed = False
        for entry in self._projects.values():
            curr_local = Path(entry.local_path)
            if not curr_local.exists():
                entry.local_path = str(resolved / entry.name)
                changed = True
        if changed:
            self.save_projects()

        self.discover_client_root_projects()
        self.template_manager.client_root = resolved
        return resolved

    def set_last_active_project(self, project_name: str) -> None:
        """Persists the last active project name in config.toml."""
        if self.config.last_active_project != project_name:
            self.config.last_active_project = project_name
            self.save_config()

    def discover_client_root_projects(self) -> list[str]:
        """Scans the remembered Client Workspace Root for existing Obol project folders."""
        discovered: list[str] = []
        client_root = self.get_client_root()
        if not client_root.is_dir():
            return discovered
        for pdir in sorted(client_root.iterdir()):
            proj_toml = pdir / ".obol" / "project.toml"
            if pdir.is_dir() and proj_toml.is_file():
                raw = load_toml_with_fallback(proj_toml)
                if raw:
                    pcfg = ProjectConfig.from_dict(raw)
                    existing = self._projects.get(pcfg.name)
                    if existing is None or existing.local_path != str(pdir.resolve()):
                        self._projects[pcfg.name] = ProjectRegistryEntry(
                            name=pcfg.name,
                            code=pcfg.code,
                            local_path=str(pdir.resolve()),
                            created_at=pcfg.created_at,
                            status="active",
                            template_used=pcfg.template_used,
                        )
                        discovered.append(pcfg.name)
        if discovered:
            self.save_projects()
        return discovered

    def get_vault_path(self) -> Optional[Path]:
        if not self.config.vault.enabled or not self.config.vault.path:
            return None
        return Path(self.config.vault.path)

    def get_vault_status(self) -> VaultStatus:
        if not self.config.vault.enabled or not self.config.vault.path:
            return VaultStatus.DISABLED
        vp = Path(self.config.vault.path)
        if vp.is_dir():
            return VaultStatus.ONLINE
        return VaultStatus.OFFLINE

    def connect_vault(self, vault_path: Path) -> VaultStatus:
        """Connects an SMB share, NAS folder, or external archive drive as the Server Vault."""
        vault_path.mkdir(parents=True, exist_ok=True)
        (vault_path / "projects").mkdir(parents=True, exist_ok=True)
        (vault_path / "templates").mkdir(parents=True, exist_ok=True)
        self.config.vault.enabled = True
        self.config.vault.path = str(vault_path.resolve())
        self.save_config()
        self.template_manager.vault_dir = vault_path.resolve()
        self.discover_vault_projects()
        return self.get_vault_status()

    def disconnect_vault(self) -> None:
        """Switches back to Standalone Local Mode without deleting any local or vault files."""
        self.config.vault.enabled = False
        self.save_config()
        self.template_manager.vault_dir = None

    def discover_vault_projects(self) -> list[str]:
        """Discovers projects on the Server Vault and mirrors their lightweight .obol/ metadata locally."""
        discovered: list[str] = []
        if self.get_vault_status() != VaultStatus.ONLINE:
            return discovered
        vault_projects_dir = Path(self.config.vault.path) / "projects"
        if not vault_projects_dir.is_dir():
            return discovered

        for pdir in sorted(vault_projects_dir.iterdir()):
            proj_toml = pdir / ".obol" / "project.toml"
            if pdir.is_dir() and proj_toml.is_file():
                raw = load_toml_with_fallback(proj_toml)
                if raw:
                    pcfg = ProjectConfig.from_dict(raw)
                    if pcfg.name not in self._projects:
                        default_local = Path(self.config.default_workspace_root or (Path.home() / "ObolProjects")) / pcfg.name
                        self._projects[pcfg.name] = ProjectRegistryEntry(
                            name=pcfg.name,
                            code=pcfg.code,
                            local_path=str(default_local),
                            created_at=pcfg.created_at,
                            status="on_server",
                            template_used=pcfg.template_used,
                        )
                        discovered.append(pcfg.name)
        if discovered:
            self.save_projects()
        return discovered

    def register_project(
        self,
        project_root: Path,
        name: Optional[str] = None,
        code: Optional[str] = None,
        template_used: str = "simple_media",
    ) -> ProjectRegistryEntry:
        root_abs = project_root.resolve()
        proj_toml = root_abs / ".obol" / "project.toml"
        raw = load_toml_with_fallback(proj_toml)
        if raw:
            pcfg = ProjectConfig.from_dict(raw)
            pname = name or pcfg.name
            pcode = code or pcfg.code
            ptpl = pcfg.template_used
        else:
            pname = name or root_abs.name
            pcode = code or pname[:3].lower()
            ptpl = template_used

        entry = ProjectRegistryEntry(
            name=pname,
            code=pcode,
            local_path=str(root_abs),
            status="active",
            template_used=ptpl,
        )
        self._projects[pname] = entry
        self.save_projects()
        return entry

    def get_project(self, name_or_code: str) -> Optional[ProjectRegistryEntry]:
        if name_or_code in self._projects:
            return self._projects[name_or_code]
        for entry in self._projects.values():
            if entry.code.lower() == name_or_code.lower() or entry.name.lower() == name_or_code.lower():
                return entry
        return None

    def list_projects(self) -> list[ProjectRegistryEntry]:
        self.discover_client_root_projects()
        if self.get_vault_status() == VaultStatus.ONLINE:
            self.discover_vault_projects()
        return sorted(self._projects.values(), key=lambda x: x.name.lower())

    def delete_project(self, name_or_code: str, delete_files: bool = False) -> bool:
        """Unregisters a project from the Hub and optionally deletes its local directory tree."""
        entry = self.get_project(name_or_code)
        if entry is None:
            return False
        self._projects.pop(entry.name, None)
        self.save_projects()
        if self.config.last_active_project == entry.name:
            remaining = sorted(self._projects.keys())
            self.config.last_active_project = remaining[0] if remaining else ""
            self.save_config()
        if delete_files and entry.local_path:
            local_dir = Path(entry.local_path)
            if local_dir.exists():
                safe_rmtree(local_dir)
        return True

    def set_software_profile(self, profile: SoftwareProfile) -> None:
        self.config.software[profile.key] = profile
        self.save_config()

    def get_software_profile(self, key: str) -> SoftwareProfile:
        return self.config.software.get(key, self.config.software["generic"])

    def detect_software_for_extension(self, ext: str) -> SoftwareProfile:
        ext_low = ext.lower()
        for prof in self.config.software.values():
            if ext_low in [e.lower() for e in prof.extensions]:
                return prof
        return self.config.software["generic"]

