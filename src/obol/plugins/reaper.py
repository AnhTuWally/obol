"""First-class Reaper DAW (.rpp) plugin: relative path patching, external media scanner/collector, and workspace tidying."""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from obol.core.storage import normalize_rel_path
from obol.plugins.base import ExternalDependency, PluginAction, TaskContext, unlock_file_writable

FILE_REF_RE = re.compile(r'^(\s*FILE\s+)"([^"]+)"(.*)$')
RECORD_PATH_RE = re.compile(r'^(\s*RECORD_PATH\s+)"([^"]*)"(\s+"[^"]*".*)$')
RENDER_FILE_RE = re.compile(r'^(\s*RENDER_FILE\s+)"([^"]*)"(.*)$')


class ReaperPlugin:
    name = "reaper"
    extensions = (".rpp",)

    def on_version_created(
        self,
        version_dir: Path,
        primary_file: Path,
        prev_version_dir: Optional[Path],
        ctx: TaskContext,
    ) -> None:
        """Patches RECORD_PATH and RENDER_FILE inside the .rpp file relative to working/vNNN/."""
        if not primary_file.is_file() or primary_file.suffix.lower() != ".rpp":
            return

        self.patch_rpp_project_paths(
            rpp_file=primary_file,
            record_path="../../repo/recorded",
            render_path="../../published",
        )
        self.tidy_workspace(version_dir)

    @staticmethod
    def patch_rpp_project_paths(
        rpp_file: Path,
        record_path: str = "../../repo/recorded",
        render_path: str = "../../published",
    ) -> None:
        unlock_file_writable(rpp_file)
        text = rpp_file.read_text(encoding="utf-8", errors="replace")
        lines = text.splitlines()
        found_record = False
        found_render = False
        new_lines: list[str] = []

        for line in lines:
            m_rec = RECORD_PATH_RE.match(line)
            if m_rec:
                new_lines.append(f'{m_rec.group(1)}"{record_path}"{m_rec.group(3)}')
                found_record = True
                continue
            m_ren = RENDER_FILE_RE.match(line)
            if m_ren:
                new_lines.append(f'{m_ren.group(1)}"{render_path}"{m_ren.group(3)}')
                found_render = True
                continue
            new_lines.append(line)

        if new_lines and new_lines[0].lstrip().startswith("<REAPER_PROJECT"):
            insert_idx = 1
            if not found_render:
                new_lines.insert(insert_idx, f'  RENDER_FILE "{render_path}"')
            if not found_record:
                new_lines.insert(insert_idx, f'  RECORD_PATH "{record_path}" ""')

        new_text = "\n".join(new_lines) + "\n"
        if new_text != text:
            rpp_file.write_text(new_text, encoding="utf-8")

    def inspect_dependencies(
        self,
        primary_file: Path,
        ctx: TaskContext,
    ) -> tuple[list[ExternalDependency], list[str]]:
        """Scans a .rpp file for FILE "..." references, separating external files from in-repo assets."""
        externals: list[ExternalDependency] = []
        used_repo_assets: set[str] = set()
        if not primary_file.is_file() or primary_file.suffix.lower() != ".rpp":
            return externals, []

        repo_root = (ctx.component_dir / "repo").resolve()
        rpp_dir = primary_file.parent.resolve()
        seen_refs: set[str] = set()

        for line in primary_file.read_text(encoding="utf-8", errors="replace").splitlines():
            m = FILE_REF_RE.match(line)
            if not m:
                continue
            raw_ref = m.group(2)
            if raw_ref in seen_refs:
                continue
            seen_refs.add(raw_ref)

            ref_path = Path(raw_ref)
            if not ref_path.is_absolute():
                resolved = (rpp_dir / ref_path).resolve()
            else:
                resolved = ref_path.resolve()

            try:
                rel_to_repo = resolved.relative_to(repo_root)
                # Extract repo asset path (everything before /vNNN/)
                parts = normalize_rel_path(str(rel_to_repo)).split("/")
                asset_parts: list[str] = []
                for part in parts[:-1]:
                    if re.match(r"^v\d+$", part, re.IGNORECASE):
                        break
                    asset_parts.append(part)
                if asset_parts:
                    used_repo_assets.add("/".join(asset_parts))
            except ValueError:
                ext = resolved.suffix.lower()
                cat = "midi" if ext in (".mid", ".midi") else "audio"
                slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", resolved.stem).strip("_").lower() or "sample"
                externals.append(
                    ExternalDependency(
                        raw_reference=raw_ref,
                        resolved_path=resolved,
                        exists=resolved.is_file(),
                        suggested_category=cat,
                        suggested_asset_name=slug,
                    )
                )

        return externals, sorted(used_repo_assets)

    @staticmethod
    def relink_rpp_references(rpp_file: Path, replacement_map: dict[str, Path]) -> int:
        """Rewrites FILE "..." references inside an .rpp file to clean relative paths."""
        if not replacement_map or not rpp_file.is_file():
            return 0
        unlock_file_writable(rpp_file)
        rpp_dir = rpp_file.parent.resolve()
        lines = rpp_file.read_text(encoding="utf-8", errors="replace").splitlines()
        replaced_count = 0
        new_lines: list[str] = []

        for line in lines:
            m = FILE_REF_RE.match(line)
            if m and m.group(2) in replacement_map:
                new_target = replacement_map[m.group(2)].resolve()
                rel_str = os.path.relpath(new_target, rpp_dir).replace("\\", "/")
                new_lines.append(f'{m.group(1)}"{rel_str}"{m.group(3)}')
                replaced_count += 1
            else:
                new_lines.append(line)

        if replaced_count > 0:
            rpp_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
        return replaced_count

    @staticmethod
    def find_outdated_rpp_references(
        rpp_file: Path,
        project_root: Optional[Path],
        workspace: Any,
        include_external: bool = False,
    ) -> list[dict[str, Any]]:
        """Scans FILE "..." references in a .rpp file and resolves Obol repo/published version status."""
        results: list[dict[str, Any]] = []
        if not rpp_file.is_file() or rpp_file.suffix.lower() != ".rpp":
            return results

        rpp_dir = rpp_file.parent.resolve()
        seen_refs: set[str] = set()
        for line in rpp_file.read_text(encoding="utf-8", errors="replace").splitlines():
            m = FILE_REF_RE.match(line)
            if not m:
                continue
            raw_ref = m.group(2)
            if raw_ref in seen_refs:
                continue
            seen_refs.add(raw_ref)

            ref_path = Path(raw_ref)
            resolved = (rpp_dir / ref_path).resolve() if not ref_path.is_absolute() else ref_path.resolve()
            if hasattr(workspace, "resolve_media_file_across_projects"):
                info = workspace.resolve_media_file_across_projects(resolved, preferred_project_root=project_root)
            elif project_root is not None:
                info = workspace.find_newer_asset_version(project_root, resolved)
            else:
                info = None
            if info is None:
                if include_external:
                    media_kind = "midi" if resolved.suffix.lower() in (".mid", ".midi") else "media"
                    results.append({
                        "tracked": False,
                        "raw_reference": raw_ref,
                        "resolved_path": resolved,
                        "current_file": resolved,
                        "current_exists": resolved.is_file(),
                        "media_kind": media_kind,
                        "update_available": False,
                        "up_to_date": True,
                        "available_versions": [],
                    })
                continue
            entry = dict(info)
            entry["tracked"] = True
            entry["raw_reference"] = raw_ref
            entry["resolved_path"] = resolved
            entry.setdefault("current_exists", resolved.is_file())
            results.append(entry)
        return results

    @staticmethod
    def update_rpp_references(
        rpp_file: Path,
        project_root: Path,
        workspace: Any,
        replacements: Optional[dict[str, Path]] = None,
    ) -> dict[str, Any]:
        """Relinks outdated FILE "..." references inside a .rpp file to newer (or specified) Obol versions."""
        refs = ReaperPlugin.find_outdated_rpp_references(rpp_file, project_root, workspace)
        if replacements is None:
            relink_map: dict[str, Path] = {
                str(r["raw_reference"]): Path(r["latest_file"])
                for r in refs
                if r.get("update_available")
            }
            updated_entries = [r for r in refs if r.get("update_available")]
        else:
            relink_map = {str(k): Path(v) for k, v in replacements.items()}
            updated_entries = [r for r in refs if r.get("raw_reference") in relink_map]

        updated_count = ReaperPlugin.relink_rpp_references(rpp_file, relink_map) if relink_map else 0
        return {
            "updated_count": updated_count,
            "updated": updated_entries,
        }

    @staticmethod
    def tidy_workspace(version_dir: Path) -> int:
        """Moves stray .rpp-bak and .reapeaks files into working/vNNN/.backups/."""
        if not version_dir.is_dir():
            return 0
        moved = 0
        backups_dir = version_dir / ".backups"
        for child in list(version_dir.iterdir()):
            if child.is_file() and child.name.endswith((".rpp-bak", ".reapeaks")):
                backups_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(child), str(backups_dir / child.name))
                moved += 1
        return moved

    def get_inspector_actions(self, ctx: TaskContext) -> list[PluginAction]:
        return [
            PluginAction("launch", "Launch .rpp in REAPER", "Open selected working .rpp file in REAPER"),
            PluginAction("open_in_reaper_tab", "Open in New REAPER Tab", "Open .rpp in a new REAPER project tab via bridge"),
            PluginAction(
                "increment_save_reaper",
                "Increment Save in REAPER",
                "Save the open REAPER session directly as the next working version without reloading",
            ),
            PluginAction(
                "import_into_reaper",
                "Import into REAPER",
                "Insert selected repo/ or published/ media into the active REAPER session",
            ),
            PluginAction(
                "update_reaper_media",
                "Update REAPER Media / MIDI",
                "Scan and update imported audio or MIDI in REAPER to newer versions",
            ),
            PluginAction(
                "collect_media",
                "Scan & Collect External Media",
                "Find audio/MIDI linked outside repo/, ingest into repo/, and relink .rpp",
            ),
            PluginAction("reaper_setup", "REAPER Integration...", "Configure REAPER ReaScripts and live bridge"),
        ]

