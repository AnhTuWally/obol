"""Blender 3D (.blend) plugin for Obol."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Optional

from obol.plugins.base import ExternalDependency, PluginAction, TaskContext


class BlenderPlugin:
    name = "blender"
    extensions = (".blend",)

    def on_version_created(
        self,
        version_dir: Path,
        primary_file: Path,
        prev_version_dir: Optional[Path],
        ctx: TaskContext,
    ) -> None:
        self.tidy_workspace(version_dir)

    @staticmethod
    def tidy_workspace(version_dir: Path) -> int:
        if not version_dir.is_dir():
            return 0
        moved = 0
        backups_dir = version_dir / ".backups"
        for child in list(version_dir.iterdir()):
            if child.is_file() and child.name.endswith((".blend1", ".blend2")):
                backups_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(child), str(backups_dir / child.name))
                moved += 1
        return moved

    def inspect_dependencies(
        self,
        primary_file: Path,
        ctx: TaskContext,
    ) -> tuple[list[ExternalDependency], list[str]]:
        return [], []

    def get_inspector_actions(self, ctx: TaskContext) -> list[PluginAction]:
        return [
            PluginAction("launch", "Launch .blend in Blender", "Open selected Blender scene"),
            PluginAction("version_up_launch", "vUp + Launch", "Create next working version and launch"),
        ]

