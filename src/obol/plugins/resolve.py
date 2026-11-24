"""DaVinci Resolve (.drp) plugin for Obol."""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from obol.plugins.base import ExternalDependency, PluginAction, TaskContext


class ResolvePlugin:
    name = "resolve"
    extensions = (".drp",)

    def on_version_created(
        self,
        version_dir: Path,
        primary_file: Path,
        prev_version_dir: Optional[Path],
        ctx: TaskContext,
    ) -> None:
        return

    def inspect_dependencies(
        self,
        primary_file: Path,
        ctx: TaskContext,
    ) -> tuple[list[ExternalDependency], list[str]]:
        return [], []

    def get_inspector_actions(self, ctx: TaskContext) -> list[PluginAction]:
        return [
            PluginAction(
                "resolve_import",
                "Load Media into Resolve Media Pool",
                "Import repo/ or published/ media into the running DaVinci Resolve Media Pool",
            ),
            PluginAction(
                "resolve_publish",
                "Export & Publish from Resolve",
                "Render or export from DaVinci Resolve into published/<deliverable>/vNNN/",
            ),
            PluginAction(
                "resolve_snapshot",
                "Snapshot .drp Working Version",
                "Export the current Resolve project as a versioned .drp file in working/vNNN/",
            ),
            PluginAction(
                "resolve_setup",
                "DaVinci Resolve Integration...",
                "Install Fusion/Resolve Lua scripts and configure Resolve bridge",
            ),
        ]
