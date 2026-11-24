"""Generic fallback software plugin for Obol."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from obol.plugins.base import ExternalDependency, PluginAction, TaskContext


class GenericPlugin:
    name = "generic"
    extensions = (".txt", ".md", ".psd", ".kra", ".als", ".mp4", ".wav")

    def on_version_created(
        self,
        version_dir: Path,
        primary_file: Path,
        prev_version_dir: Optional[Path],
        ctx: TaskContext,
    ) -> None:
        pass

    def inspect_dependencies(
        self,
        primary_file: Path,
        ctx: TaskContext,
    ) -> tuple[list[ExternalDependency], list[str]]:
        return [], []

    def get_inspector_actions(self, ctx: TaskContext) -> list[PluginAction]:
        return [
            PluginAction("launch", f"Launch in {ctx.software_profile.display_name}", "Open the selected working file"),
            PluginAction("version_up", "Version Up (+vUp)", "Create next working version"),
        ]

