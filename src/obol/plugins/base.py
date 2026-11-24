"""Plugin protocol, dynamic registry, and the 4 configurable Version-Up Strategy executors."""

from __future__ import annotations

import fnmatch
import importlib.util
import os
import re
import shutil
import stat
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional, Protocol

from obol.core.models import SoftwareProfile, VersionUpStrategy


@dataclass
class TaskContext:
    project_root: Path
    component_path: str
    component_dir: Path
    software_profile: SoftwareProfile


@dataclass
class ExternalDependency:
    raw_reference: str
    resolved_path: Path
    exists: bool
    suggested_category: str = "audio"
    suggested_asset_name: str = ""


@dataclass
class PluginAction:
    id: str
    label: str
    tooltip: str = ""


class ObolPlugin(Protocol):
    name: str
    extensions: tuple[str, ...]

    def on_version_created(
        self,
        version_dir: Path,
        primary_file: Path,
        prev_version_dir: Optional[Path],
        ctx: TaskContext,
    ) -> None:
        ...

    def inspect_dependencies(
        self,
        primary_file: Path,
        ctx: TaskContext,
    ) -> tuple[list[ExternalDependency], list[str]]:
        """Returns (external_dependencies_outside_repo, used_in_repo_asset_paths)."""
        ...

    def get_inspector_actions(self, ctx: TaskContext) -> list[PluginAction]:
        ...


def unlock_file_writable(path: Path) -> None:
    if path.exists():
        try:
            current = path.stat().st_mode
            os.chmod(path, current | stat.S_IWRITE | stat.S_IWUSR)
        except OSError:
            pass


def lock_file_readonly(path: Path) -> None:
    if path.exists():
        try:
            os.chmod(path, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass


def matches_any_pattern(rel_path: str, filename: str, patterns: list[str]) -> bool:
    for pat in patterns:
        clean_pat = pat.rstrip("/*")
        if (
            fnmatch.fnmatch(filename, pat)
            or fnmatch.fnmatch(rel_path, pat)
            or fnmatch.fnmatch(rel_path, clean_pat)
            or rel_path.startswith(clean_pat + "/")
        ):
            return True
    return False


def replace_version_in_filename(filename: str, new_version_num: int, component_leaf: str) -> str:
    ver_tag = f"v{new_version_num:03d}"
    stem = Path(filename).stem
    ext = Path(filename).suffix
    if re.search(r"_v\d+$", stem, re.IGNORECASE):
        new_stem = re.sub(r"_v\d+$", f"_{ver_tag}", stem, flags=re.IGNORECASE)
        return f"{new_stem}{ext}"
    return f"{component_leaf}_{ver_tag}{ext}"


def execute_version_up(
    ctx: TaskContext,
    plugin: ObolPlugin,
    source_version_dir: Optional[Path],
    source_primary_file: Optional[Path],
    target_version_num: int,
    target_version_dir: Path,
    strategy: Optional[VersionUpStrategy] = None,
    seed_file_path: Optional[Path] = None,
) -> Path:
    """Executes one of the 4 configurable Version-Up strategies and returns the new primary file path."""
    active_strategy = strategy or ctx.software_profile.version_up_strategy
    target_version_dir.mkdir(parents=True, exist_ok=True)
    comp_leaf = ctx.component_path.replace("\\", "/").strip("/").split("/")[-1]
    ver_tag = f"v{target_version_num:03d}"

    new_primary: Optional[Path] = None

    if active_strategy == VersionUpStrategy.FROM_SEED_TEMPLATE or source_version_dir is None or source_primary_file is None:
        if seed_file_path and seed_file_path.is_file():
            ext = seed_file_path.suffix
            new_primary = target_version_dir / f"{comp_leaf}_{ver_tag}{ext}"
            shutil.copy2(seed_file_path, new_primary)
            unlock_file_writable(new_primary)
        elif source_primary_file and source_primary_file.is_file():
            new_name = replace_version_in_filename(source_primary_file.name, target_version_num, comp_leaf)
            new_primary = target_version_dir / new_name
            shutil.copy2(source_primary_file, new_primary)
            unlock_file_writable(new_primary)
        else:
            ext = ctx.software_profile.extensions[0] if ctx.software_profile.extensions else ".txt"
            new_primary = target_version_dir / f"{comp_leaf}_{ver_tag}{ext}"
            new_primary.write_text(f"# {comp_leaf} {ver_tag}\n", encoding="utf-8")

    elif active_strategy == VersionUpStrategy.CLONE_PRIMARY_FILE:
        new_name = replace_version_in_filename(source_primary_file.name, target_version_num, comp_leaf)
        new_primary = target_version_dir / new_name
        shutil.copy2(source_primary_file, new_primary)
        unlock_file_writable(new_primary)

    elif active_strategy == VersionUpStrategy.COPY_WITH_SIDECARS:
        new_name = replace_version_in_filename(source_primary_file.name, target_version_num, comp_leaf)
        new_primary = target_version_dir / new_name
        shutil.copy2(source_primary_file, new_primary)
        unlock_file_writable(new_primary)
        allowed_exts = {e.lower() for e in ctx.software_profile.sidecar_extensions}
        for item in source_version_dir.iterdir():
            if item.is_file() and item != source_primary_file:
                if not allowed_exts or item.suffix.lower() in allowed_exts:
                    if not matches_any_pattern(item.name, item.name, ctx.software_profile.ignore_patterns):
                        sidecar_name = replace_version_in_filename(item.name, target_version_num, comp_leaf)
                        dest_sidecar = target_version_dir / sidecar_name
                        shutil.copy2(item, dest_sidecar)
                        unlock_file_writable(dest_sidecar)

    elif active_strategy == VersionUpStrategy.CLONE_ENTIRE_FOLDER:
        for item in source_version_dir.rglob("*"):
            rel = item.relative_to(source_version_dir).as_posix()
            if matches_any_pattern(rel, item.name, ctx.software_profile.ignore_patterns):
                continue
            if item.is_dir():
                (target_version_dir / rel).mkdir(parents=True, exist_ok=True)
            elif item.is_file():
                dest_item = target_version_dir / rel
                dest_item.parent.mkdir(parents=True, exist_ok=True)
                if item == source_primary_file:
                    new_name = replace_version_in_filename(item.name, target_version_num, comp_leaf)
                    dest_item = dest_item.parent / new_name
                    new_primary = dest_item
                shutil.copy2(item, dest_item)
                unlock_file_writable(dest_item)
        if new_primary is None:
            files = [p for p in target_version_dir.rglob("*") if p.is_file()]
            new_primary = files[0] if files else (target_version_dir / f"{comp_leaf}_{ver_tag}.txt")

    assert new_primary is not None

    # Lock previous version's files as Read-Only if configured
    if ctx.software_profile.lock_previous_versions and source_version_dir and source_version_dir.is_dir():
        for old_file in source_version_dir.rglob("*"):
            if old_file.is_file():
                lock_file_readonly(old_file)

    plugin.on_version_created(target_version_dir, new_primary, source_version_dir, ctx)
    return new_primary


def launch_file_with_profile(file_path: Path, profile: SoftwareProfile) -> Optional[subprocess.Popen]:
    """Launches a working file using the configured SoftwareProfile executable or OS default."""
    if profile.executable and Path(profile.executable).exists():
        args = [profile.executable] + [
            arg.replace("{file}", str(file_path)) for arg in (profile.launch_args or ["{file}"])
        ]
        return subprocess.Popen(args, cwd=str(file_path.parent))

    if sys.platform == "win32" and hasattr(os, "startfile"):
        os.startfile(str(file_path))
        return None

    opener = "open" if sys.platform == "darwin" else "xdg-open"
    return subprocess.Popen([opener, str(file_path)], cwd=str(file_path.parent))


class PluginRegistry:
    """Manages built-in DCC plugins and dynamically loads custom user plugins from ~/.obol/plugins/."""

    def __init__(self, user_plugins_dir: Optional[Path] = None) -> None:
        self._plugins: dict[str, ObolPlugin] = {}
        self.user_plugins_dir = user_plugins_dir
        self._register_builtins()
        if user_plugins_dir:
            self.load_user_plugins(user_plugins_dir)

    def _register_builtins(self) -> None:
        from obol.plugins.blender import BlenderPlugin
        from obol.plugins.generic import GenericPlugin
        from obol.plugins.reaper import ReaperPlugin
        from obol.plugins.resolve import ResolvePlugin

        for p in (GenericPlugin(), ReaperPlugin(), BlenderPlugin(), ResolvePlugin()):
            self._plugins[p.name] = p

    def register(self, plugin: ObolPlugin) -> None:
        self._plugins[plugin.name] = plugin

    def get(self, name: str) -> ObolPlugin:
        return self._plugins.get(name, self._plugins["generic"])

    def list_plugins(self) -> dict[str, ObolPlugin]:
        return dict(self._plugins)

    def load_user_plugins(self, directory: Path) -> list[str]:
        loaded: list[str] = []
        if not directory.is_dir():
            return loaded
        for py_file in sorted(directory.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            spec = importlib.util.spec_from_file_location(f"obol_user_plugin_{py_file.stem}", py_file)
            if spec and spec.loader:
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                if hasattr(module, "register_plugin"):
                    plugin_obj = module.register_plugin()
                    if plugin_obj and hasattr(plugin_obj, "name"):
                        self.register(plugin_obj)
                        loaded.append(plugin_obj.name)
                elif hasattr(module, "Plugin"):
                    plugin_obj = module.Plugin()
                    if hasattr(plugin_obj, "name"):
                        self.register(plugin_obj)
                        loaded.append(plugin_obj.name)
        return loaded
