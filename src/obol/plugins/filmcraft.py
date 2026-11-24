"""First-class FilmCraft (.fcproj) plugin: JSON inspection, relative path relinking, and workspace tidying."""

from __future__ import annotations

import json
import os
import re
import shutil
from pathlib import Path
from typing import Any, Optional

from obol.core.storage import normalize_rel_path
from obol.plugins.base import ExternalDependency, PluginAction, TaskContext, unlock_file_writable

_VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".mxf", ".avi", ".webm"}
_AUDIO_EXTS = {".wav", ".flac", ".mp3", ".aiff", ".aac", ".ogg"}
_STILLS_EXTS = {".png", ".jpg", ".jpeg", ".exr", ".dpx", ".tif", ".tiff", ".bmp", ".tga"}
_CAPTION_EXTS = {".srt", ".vtt"}
_LUT_EXTS = {".cube", ".3dl"}


def _suggest_category_for_ext(ext: str) -> str:
    ext_l = ext.lower()
    if ext_l in _VIDEO_EXTS:
        return "video"
    if ext_l in _AUDIO_EXTS:
        return "audio"
    if ext_l in _STILLS_EXTS:
        return "stills"
    if ext_l in _CAPTION_EXTS:
        return "captions"
    if ext_l in _LUT_EXTS:
        return "luts"
    return "references"


def _load_fcproj_json(fcproj_file: Path) -> Optional[dict[str, Any]]:
    if not fcproj_file.is_file():
        return None
    try:
        data = json.loads(fcproj_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


class FilmCraftPlugin:
    """Obol plugin for FilmCraft NLE (.fcproj) working projects."""

    name = "filmcraft"
    extensions = (".fcproj",)

    def on_version_created(
        self,
        version_dir: Path,
        primary_file: Path,
        prev_version_dir: Optional[Path],
        ctx: TaskContext,
    ) -> None:
        """Synchronizes internal project name to the versioned .fcproj stem and tidies backups."""
        if primary_file.is_file() and primary_file.suffix.lower() == ".fcproj":
            data = _load_fcproj_json(primary_file)
            if data is None:
                from obol.core.templates import DEFAULT_FILMCRAFT_FCPROJ_CONTENT

                data = json.loads(DEFAULT_FILMCRAFT_FCPROJ_CONTENT)
                if isinstance(data, dict) and isinstance(data.get("project"), dict):
                    data["project"]["name"] = primary_file.stem
                    if isinstance(data["project"].get("root"), dict):
                        data["project"]["root"]["name"] = primary_file.stem
                unlock_file_writable(primary_file)
                primary_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            elif isinstance(data.get("project"), dict):
                proj = data["project"]
                changed = False
                if proj.get("name") != primary_file.stem:
                    proj["name"] = primary_file.stem
                    changed = True
                root = proj.get("root")
                if isinstance(root, dict) and root.get("name") != primary_file.stem:
                    root["name"] = primary_file.stem
                    changed = True
                if changed:
                    unlock_file_writable(primary_file)
                    primary_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        self.tidy_workspace(version_dir)

    @staticmethod
    def tidy_workspace(version_dir: Path) -> int:
        """Moves stray .fcproj-bak and .fcproj.tmp files into working/vNNN/.backups/."""
        if not version_dir.is_dir():
            return 0
        moved = 0
        backups_dir = version_dir / ".backups"
        for child in list(version_dir.iterdir()):
            if child.is_file() and child.name.endswith((".fcproj-bak", ".fcproj.tmp")):
                backups_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(child), str(backups_dir / child.name))
                moved += 1
        return moved

    @staticmethod
    def _iter_media_refs(media_kind: dict[str, Any]) -> list[str]:
        refs: list[str] = []
        media_node = media_kind.get("media")
        if isinstance(media_node, dict):
            file_node = media_node.get("File")
            if isinstance(file_node, dict) and isinstance(file_node.get("path"), str) and file_node["path"].strip():
                refs.append(file_node["path"].strip())
            img_seq = media_node.get("ImageSequence")
            if isinstance(img_seq, dict):
                for key in ("path", "first_file"):
                    val = img_seq.get(key)
                    if isinstance(val, str) and val.strip():
                        refs.append(val.strip())
                        break
        proxy_node = media_kind.get("proxy")
        if isinstance(proxy_node, dict) and isinstance(proxy_node.get("path"), str) and proxy_node["path"].strip():
            refs.append(proxy_node["path"].strip())
        elif isinstance(proxy_node, str) and proxy_node.strip():
            refs.append(proxy_node.strip())
        return refs

    def inspect_dependencies(
        self,
        primary_file: Path,
        ctx: TaskContext,
    ) -> tuple[list[ExternalDependency], list[str]]:
        """Scans a .fcproj file for file references, separating external files from in-repo assets."""
        externals: list[ExternalDependency] = []
        used_repo_assets: set[str] = set()
        if not primary_file.is_file() or primary_file.suffix.lower() != ".fcproj":
            return externals, []

        data = _load_fcproj_json(primary_file)
        if not data:
            return externals, []

        items = data.get("project", {}).get("items", {})
        if not isinstance(items, dict):
            return externals, []

        repo_root = (ctx.component_dir / "repo").resolve()
        fc_dir = primary_file.parent.resolve()
        seen_refs: set[str] = set()

        for item in items.values():
            if not isinstance(item, dict):
                continue
            media_kind = item.get("kind", {}).get("Media")
            if not isinstance(media_kind, dict):
                continue
            for raw_ref in self._iter_media_refs(media_kind):
                if raw_ref in seen_refs:
                    continue
                seen_refs.add(raw_ref)

                ref_path = Path(raw_ref)
                resolved = (fc_dir / ref_path).resolve() if not ref_path.is_absolute() else ref_path.resolve()

                try:
                    rel_to_repo = resolved.relative_to(repo_root)
                    parts = normalize_rel_path(str(rel_to_repo)).split("/")
                    asset_parts: list[str] = []
                    for part in parts[:-1]:
                        if re.match(r"^v\d+$", part, re.IGNORECASE):
                            break
                        asset_parts.append(part)
                    if asset_parts:
                        used_repo_assets.add("/".join(asset_parts))
                except ValueError:
                    cat = _suggest_category_for_ext(resolved.suffix)
                    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", resolved.stem).strip("_").lower() or "media"
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
    def relink_fcproj_references(
        fcproj_file: Path,
        replacement_map: dict[str, Path],
        relative: bool = True,
    ) -> int:
        """Rewrites media File.path (and matching proxy paths) inside a .fcproj JSON file."""
        if not replacement_map:
            return 0
        data = _load_fcproj_json(fcproj_file)
        if not data:
            return 0

        items = data.get("project", {}).get("items", {})
        if not isinstance(items, dict):
            return 0

        unlock_file_writable(fcproj_file)
        fc_dir = fcproj_file.parent.resolve()
        replaced_count = 0

        def _format_target(new_target: Path) -> str:
            resolved = new_target.resolve()
            if relative:
                return os.path.relpath(resolved, fc_dir).replace("\\", "/")
            return str(resolved)

        for item in items.values():
            if not isinstance(item, dict):
                continue
            media_kind = item.get("kind", {}).get("Media")
            if not isinstance(media_kind, dict):
                continue
            media_node = media_kind.get("media")
            if isinstance(media_node, dict):
                file_node = media_node.get("File")
                if isinstance(file_node, dict):
                    old_path = file_node.get("path")
                    if isinstance(old_path, str) and old_path in replacement_map:
                        file_node["path"] = _format_target(replacement_map[old_path])
                        replaced_count += 1
                img_seq = media_node.get("ImageSequence")
                if isinstance(img_seq, dict):
                    for key in ("path", "first_file"):
                        old_path = img_seq.get(key)
                        if isinstance(old_path, str) and old_path in replacement_map:
                            img_seq[key] = _format_target(replacement_map[old_path])
                            replaced_count += 1
            proxy_node = media_kind.get("proxy")
            if isinstance(proxy_node, dict):
                old_p = proxy_node.get("path")
                if isinstance(old_p, str) and old_p in replacement_map:
                    proxy_node["path"] = _format_target(replacement_map[old_p])
                    replaced_count += 1
            elif isinstance(proxy_node, str) and proxy_node in replacement_map:
                media_kind["proxy"] = _format_target(replacement_map[proxy_node])
                replaced_count += 1

        if replaced_count > 0:
            fcproj_file.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        return replaced_count

    @staticmethod
    def extract_media_items(fcproj_file: Path) -> list[dict[str, Any]]:
        """Extracts media items with bin paths and resolved file paths directly from a .fcproj JSON file."""
        data = _load_fcproj_json(fcproj_file)
        if not data:
            return []
        project = data.get("project", {})
        items = project.get("items", {})
        root = project.get("root", {})
        if not isinstance(items, dict):
            return []

        bin_by_item: dict[str, str] = {}

        def _walk_bin(node: dict[str, Any], prefix: str) -> None:
            for child in node.get("children", []) or []:
                if not isinstance(child, dict):
                    continue
                if "Bin" in child and isinstance(child["Bin"], dict):
                    bnode = child["Bin"]
                    bname = str(bnode.get("name") or "")
                    next_prefix = f"{prefix}/{bname}" if prefix else bname
                    _walk_bin(bnode, next_prefix)
                elif "Item" in child:
                    bin_by_item[str(child["Item"])] = prefix or "Root"

        if isinstance(root, dict):
            _walk_bin(root, "")

        fc_dir = fcproj_file.parent.resolve()
        results: list[dict[str, Any]] = []
        for item_id_str, item in items.items():
            if not isinstance(item, dict):
                continue
            media_kind = item.get("kind", {}).get("Media")
            if not isinstance(media_kind, dict):
                continue
            media_node = media_kind.get("media")
            if not isinstance(media_node, dict):
                continue
            raw_path = ""
            if isinstance(media_node.get("File"), dict):
                raw_path = str(media_node["File"].get("path") or "")
            elif isinstance(media_node.get("ImageSequence"), dict):
                seq = media_node["ImageSequence"]
                raw_path = str(seq.get("path") or seq.get("first_file") or "")
            if not raw_path:
                continue

            p = Path(raw_path)
            resolved = str(((fc_dir / p).resolve() if not p.is_absolute() else p.resolve())).replace("\\", "/")
            info = media_kind.get("info") if isinstance(media_kind.get("info"), dict) else {}
            try:
                numeric_id = int(item.get("id", item_id_str))
            except (TypeError, ValueError):
                continue
            results.append({
                "id": numeric_id,
                "name": str(item.get("name") or Path(raw_path).name),
                "raw_path": raw_path,
                "file_path": resolved,
                "bin_path": bin_by_item.get(str(numeric_id), "Root"),
                "offline": bool(media_kind.get("offline", False)),
                "type": str(info.get("kind") or "Media"),
            })
        return results

    def get_inspector_actions(self, ctx: TaskContext) -> list[PluginAction]:
        return [
            PluginAction("launch", "Launch .fcproj in FilmCraft", "Open selected working .fcproj file in FilmCraft"),
            PluginAction("version_up_launch", "vUp + Launch", "Create next working version and launch in FilmCraft"),
            PluginAction(
                "filmcraft_import",
                "Import Media into FilmCraft",
                "Load repo/ or published/ media into FilmCraft and update outdated project footage",
            ),
            PluginAction(
                "filmcraft_publish",
                "Export & Publish from FilmCraft",
                "Render media, stills, or interchange files from FilmCraft into published/<deliverable>/vNNN/",
            ),
            PluginAction(
                "collect_media",
                "Scan & Collect External Media",
                "Find media linked outside repo/, ingest into repo/, and relink .fcproj",
            ),
            PluginAction("filmcraft_setup", "FilmCraft Integration...", "Configure FilmCraft executable and control bridge"),
        ]
