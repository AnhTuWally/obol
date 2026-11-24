"""3-Tier Composable Template Engine (Project Hierarchy, Component/Task, and Working Seed File templates)."""

from __future__ import annotations

import fnmatch
import shutil
from pathlib import Path
from typing import Any, Optional

from obol.core.models import (
    FileTemplate,
    ProjectTemplate,
    ResolvedSeedTemplate,
    TaskTemplate,
)
from obol.core.seed_templates import SeedTemplateManager
from obol.core.storage import (
    atomic_write_toml,
    discover_project_components,
    load_component_snapshot,
    load_toml_with_fallback,
    normalize_rel_path,
)

DEFAULT_REAPER_RPP_CONTENT = """<REAPER_PROJECT 0.1 "7.0" 1732320000
  RIPPLE 0
  GROUPOVERRIDE 0 0 0
  AUTOXFADE 129
  ENVATTACH 3
  RECORD_PATH "../../repo/recorded" ""
  RENDER_FILE "../../published"
  SAMPLERATE 48000 0 0
  TEMPO 120 4 4
  <TRACK {11111111-2222-3333-4444-555555555555}
    NAME "Main Track"
    VOLPAN 1 0 -1 -1 1
    MUTESOLO 0 0 0
  >
>
"""

DEFAULT_NOTES_MD_CONTENT = """# Working Notes
Created with Obol Pipeline.
"""

DEFAULT_FILMCRAFT_FCPROJ_CONTENT = (
    '{"format":"filmcraft.project","schema_version":17,"generator":"FilmCraft 0.20.1",'
    '"project":{"name":"filmcraft_default","settings":{"renderer":"FilmCraft GPU Acceleration (wgpu)",'
    '"video_display":"Timecode","audio_display_samples":true,"default_still_duration":1270080000000,'
    '"default_transition_duration_frames":24,"default_audio_transition_duration":254016000000,'
    '"ingest":{"enabled":false,"action":"createProxies","destination":null,"preset":""},'
    '"title_safe":[20.0,20.0],"action_safe":[10.0,10.0],"capture_format":"DV",'
    '"scratch":{"captured":null,"videoPreviews":null,"audioPreviews":null,"autoSave":null}},'
    '"root":{"id":0,"name":"filmcraft_default","children":[]},"items":{},"next_id":1},'
    '"view":{"open_sequences":[],"active_sequence":null,"sequences":{}}}\n'
)


def builtin_project_templates() -> dict[str, ProjectTemplate]:
    return {
        "simple_media": ProjectTemplate(
            name="simple_media",
            description="Simple 1-level project with audio/ and video/ triad components",
            default_components=["audio", "video"],
            component_templates={
                "audio": "reaper_daw",
                "video": "generic_media",
            },
            shared_folders=["_repo"],
        ),
        "music_album": ProjectTemplate(
            name="music_album",
            description="Multi-song album hierarchy with cover_art and song_1/{arrangement,mixing}",
            default_components=[
                "cover_art",
                "song_1/arrangement",
                "song_1/mixing",
                "song_2/arrangement",
            ],
            component_templates={
                "cover_art": "krita_art",
                "*arrangement*": "reaper_daw",
                "*mixing*": "reaper_daw",
                "*": "reaper_daw",
            },
            shared_folders=["_repo", "_deliverables"],
        ),
        "single_track": ProjectTemplate(
            name="single_track",
            description="Single track music production with arrangement, mixing, and mastering",
            default_components=["arrangement", "mixing", "mastering"],
            component_templates={"*": "reaper_daw"},
            shared_folders=["_repo"],
        ),
    }


def builtin_task_templates() -> dict[str, TaskTemplate]:
    return {
        "reaper_daw": TaskTemplate(
            name="reaper_daw",
            description="Reaper DAW audio production with audio/MIDI/recorded repo routing",
            software="reaper",
            seed_file_template="reaper_default.rpp",
            repo_routes={
                "audio": [".wav", ".flac", ".mp3", ".ogg", ".aiff"],
                "midi": [".mid", ".midi"],
                "presets": [".fxp", ".fxb", ".vstpreset", ".rfxchain"],
                "references": [".mp4", ".mov", ".pdf", ".txt", ".md"],
            },
            extra_working_dirs=[],
            publish_formats=[".wav", ".mid", ".mp3", ".flac"],
        ),
        "blender_3d": TaskTemplate(
            name="blender_3d",
            description="Blender 3D scene/asset with textures, models, and references routing",
            software="blender",
            seed_file_template="blank.blend",
            repo_routes={
                "textures": [".png", ".jpg", ".jpeg", ".exr", ".hdr", ".tif"],
                "models": [".fbx", ".obj", ".gltf", ".glb", ".abc"],
                "audio": [".wav", ".mp3"],
            },
            extra_working_dirs=[],
            publish_formats=[".fbx", ".glb", ".png", ".mp4"],
        ),
        "krita_art": TaskTemplate(
            name="krita_art",
            description="2D artwork / illustration component",
            software="krita",
            seed_file_template="canvas.kra",
            repo_routes={
                "references": [".png", ".jpg", ".jpeg", ".webp", ".pur"],
                "brushes": [".abr", ".bundle"],
            },
            extra_working_dirs=[],
            publish_formats=[".png", ".jpg", ".webp", ".psd"],
        ),
        "resolve_video": TaskTemplate(
            name="resolve_video",
            description="DaVinci Resolve editing/color/finishing with video, audio, stills, and LUTs routing",
            software="resolve",
            seed_file_template="",
            repo_routes={
                "video": [".mp4", ".mov", ".mkv", ".mxf", ".braw", ".r3d", ".avi"],
                "audio": [".wav", ".flac", ".mp3", ".aiff"],
                "stills": [".png", ".jpg", ".jpeg", ".exr", ".dpx", ".tif"],
                "luts": [".cube", ".3dl"],
                "references": [".pdf", ".txt", ".md"],
            },
            extra_working_dirs=[],
            publish_formats=[".mp4", ".mov", ".wav", ".png"],
        ),
        "filmcraft_video": TaskTemplate(
            name="filmcraft_video",
            description="FilmCraft NLE video editing with video, audio, stills, captions, and LUTs routing",
            software="filmcraft",
            seed_file_template="filmcraft_default.fcproj",
            repo_routes={
                "video": [".mp4", ".mov", ".mkv", ".mxf", ".avi", ".webm"],
                "audio": [".wav", ".flac", ".mp3", ".aiff", ".aac", ".ogg"],
                "stills": [".png", ".jpg", ".jpeg", ".exr", ".dpx", ".tif", ".tiff", ".bmp", ".tga"],
                "captions": [".srt", ".vtt"],
                "luts": [".cube", ".3dl"],
                "references": [".pdf", ".txt", ".md"],
            },
            extra_working_dirs=[],
            publish_formats=[".mp4", ".mov", ".wav", ".png", ".xml"],
        ),
        "generic_media": TaskTemplate(
            name="generic_media",
            description="Generic media production component",
            software="generic",
            seed_file_template="notes.md",
            repo_routes={
                "audio": [".wav", ".flac", ".mp3", ".ogg"],
                "video": [".mp4", ".mov", ".mkv"],
                "docs": [".txt", ".md", ".pdf"],
            },
            extra_working_dirs=[],
            publish_formats=[".mp4", ".wav", ".pdf"],
        ),
    }


class TemplateManager:
    """Manages Tier-1 (Project), Tier-2 (Task/Component), and Tier-3 (Seed Working File) templates."""

    def __init__(
        self,
        hub_dir: Path,
        vault_dir: Optional[Path] = None,
        client_root: Optional[Path] = None,
    ) -> None:
        self.hub_dir = hub_dir
        self._vault_dir = vault_dir
        self._client_root = client_root
        self.templates_root = hub_dir / "templates"
        self.projects_dir = self.templates_root / "projects"
        self.tasks_dir = self.templates_root / "tasks"
        self.files_dir = self.templates_root / "files"
        self.seeds = SeedTemplateManager(
            hub_dir=hub_dir,
            vault_dir=vault_dir,
            client_root=client_root,
        )
        self.ensure_defaults()

    @property
    def vault_dir(self) -> Optional[Path]:
        return self._vault_dir

    @vault_dir.setter
    def vault_dir(self, value: Optional[Path]) -> None:
        self._vault_dir = value
        self.seeds.vault_dir = value.resolve() if value else None

    @property
    def client_root(self) -> Optional[Path]:
        return self._client_root

    @client_root.setter
    def client_root(self, value: Optional[Path]) -> None:
        self._client_root = value
        self.seeds.client_root = value.expanduser().resolve() if value else None

    def ensure_defaults(self) -> None:
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.files_dir.mkdir(parents=True, exist_ok=True)

        for name, pt in builtin_project_templates().items():
            p_file = self.projects_dir / f"{name}.toml"
            if not p_file.exists():
                atomic_write_toml(p_file, pt.to_dict())

        for name, tt in builtin_task_templates().items():
            t_file = self.tasks_dir / f"{name}.toml"
            if not t_file.exists():
                atomic_write_toml(t_file, tt.to_dict())

        rpp_seed = self.files_dir / "reaper_default.rpp"
        if not rpp_seed.exists():
            rpp_seed.write_text(DEFAULT_REAPER_RPP_CONTENT, encoding="utf-8")

        md_seed = self.files_dir / "notes.md"
        if not md_seed.exists():
            md_seed.write_text(DEFAULT_NOTES_MD_CONTENT, encoding="utf-8")

        blend_seed = self.files_dir / "blank.blend"
        if not blend_seed.exists():
            blend_seed.write_bytes(b"BLENDER-v402-OBOL-SEED\n")

        kra_seed = self.files_dir / "canvas.kra"
        if not kra_seed.exists():
            kra_seed.write_bytes(b"KRITA-OBOL-SEED\n")

        fc_seed = self.files_dir / "filmcraft_default.fcproj"
        if not fc_seed.exists():
            fc_seed.write_text(DEFAULT_FILMCRAFT_FCPROJ_CONTENT, encoding="utf-8")

    def list_project_templates(self) -> dict[str, ProjectTemplate]:
        res: dict[str, ProjectTemplate] = {}
        for f in sorted(self.projects_dir.glob("*.toml")):
            raw = load_toml_with_fallback(f)
            if raw:
                pt = ProjectTemplate.from_dict(f.stem, raw)
                res[pt.name] = pt
        return res or builtin_project_templates()

    def list_task_templates(self) -> dict[str, TaskTemplate]:
        res: dict[str, TaskTemplate] = {}
        for f in sorted(self.tasks_dir.glob("*.toml")):
            raw = load_toml_with_fallback(f)
            if raw:
                tt = TaskTemplate.from_dict(f.stem, raw)
                res[tt.name] = tt
        return res or builtin_task_templates()

    def list_file_templates(self) -> dict[str, FileTemplate]:
        res: dict[str, FileTemplate] = {}
        for f in sorted(self.files_dir.iterdir()):
            if f.is_file() and not f.name.startswith("."):
                res[f.name] = FileTemplate(
                    name=f.name,
                    extension=f.suffix.lower(),
                    path=str(f),
                    description=f"Seed working file ({f.suffix})",
                )
        return res

    def get_project_template(self, name: str) -> ProjectTemplate:
        templates = self.list_project_templates()
        return templates.get(name, builtin_project_templates()["simple_media"])

    def get_task_template(self, name: str) -> TaskTemplate:
        templates = self.list_task_templates()
        return templates.get(name, builtin_task_templates()["reaper_daw"])

    def resolve_task_template_for_component(self, proj_tpl: ProjectTemplate, component_path: str) -> TaskTemplate:
        norm = normalize_rel_path(component_path)
        for pattern, task_tpl_name in proj_tpl.component_templates.items():
            if pattern == norm or fnmatch.fnmatch(norm, pattern):
                return self.get_task_template(task_tpl_name)
        return self.get_task_template("reaper_daw")

    def get_seed_file_path(self, seed_filename: str) -> Optional[Path]:
        if not seed_filename:
            return None
        candidate = self.files_dir / seed_filename
        if candidate.is_file():
            return candidate
        return None

    def resolve_seed_template(
        self,
        software: str,
        project_root: Optional[Path] = None,
        client_root: Optional[Path] = None,
        task_template: Optional[TaskTemplate] = None,
        seed_override: Optional[str] = None,
    ) -> Optional[ResolvedSeedTemplate]:
        """Resolves the active seed template via Project -> Client Root -> Global -> Legacy cascade."""
        from obol.core.seed_templates import normalize_software_key

        effective_task_template = task_template
        if task_template is not None:
            target_sw = normalize_software_key(software)
            if normalize_software_key(task_template.software) != target_sw:
                effective_task_template = next(
                    (
                        t
                        for t in self.list_task_templates().values()
                        if normalize_software_key(t.software) == target_sw and t.seed_file_template
                    ),
                    None,
                )
        return self.seeds.resolve_seed_template(
            software=software,
            project_root=project_root,
            client_root=client_root,
            task_template=effective_task_template,
            seed_override=seed_override,
            legacy_files_dir=self.files_dir,
        )

    def save_project_template(self, pt: ProjectTemplate) -> Path:
        """Creates or updates a Tier-1 Project Hierarchy Template (.toml)."""
        dest = self.projects_dir / f"{pt.name}.toml"
        atomic_write_toml(dest, pt.to_dict())
        self._sync_to_vault_if_available(dest, "projects")
        return dest

    def delete_project_template(self, name: str) -> bool:
        target = self.projects_dir / f"{name}.toml"
        if target.is_file():
            target.unlink()
            return True
        return False

    def save_task_template(self, tt: TaskTemplate) -> Path:
        """Creates or updates a Tier-2 Component/Task Template (.toml)."""
        dest = self.tasks_dir / f"{tt.name}.toml"
        atomic_write_toml(dest, tt.to_dict())
        self._sync_to_vault_if_available(dest, "tasks")
        return dest

    def delete_task_template(self, name: str) -> bool:
        target = self.tasks_dir / f"{name}.toml"
        if target.is_file():
            target.unlink()
            return True
        return False

    def import_seed_file(self, source_file: Path, template_filename: Optional[str] = None) -> Path:
        """Imports a starter working file (.rpp, .blend, .kra, etc.) into the Tier-3 Seed File library."""
        return self.capture_template("file", source_file, template_filename or source_file.name)

    def delete_seed_file(self, filename: str) -> bool:
        target = self.files_dir / filename
        if target.is_file():
            target.unlink()
            return True
        return False

    def preview_project_tree(
        self,
        project_name: str,
        project_template_name: str = "simple_media",
        custom_components: Optional[list[str]] = None,
        component_task_overrides: Optional[dict[str, str]] = None,
        component_seed_overrides: Optional[dict[str, str]] = None,
        client_root: Optional[Path] = None,
        project_root: Optional[Path] = None,
    ) -> dict[str, Any]:
        """Builds an interactive preview structure showing folders and seed v001 files before creation."""
        pt = self.get_project_template(project_template_name)
        comps = custom_components if custom_components is not None else pt.default_components
        task_overrides = component_task_overrides or {}
        seed_overrides = component_seed_overrides or {}
        comp_details: list[dict[str, Any]] = []
        for c in comps:
            norm_c = normalize_rel_path(c)
            if norm_c in task_overrides:
                tt = self.get_task_template(task_overrides[norm_c])
            else:
                tt = self.resolve_task_template_for_component(pt, norm_c)
            explicit_seed = seed_overrides.get(norm_c) or None
            resolved = self.resolve_seed_template(
                software=tt.software,
                project_root=project_root,
                client_root=client_root,
                task_template=tt,
                seed_override=explicit_seed,
            )
            seed_label = (
                resolved.display_label()
                if resolved is not None
                else (explicit_seed or tt.seed_file_template or "(blank)")
            )
            seed_spec = resolved.reference_spec() if resolved is not None else (explicit_seed or tt.seed_file_template)
            seed_scope = resolved.scope.value if resolved is not None else ""
            leaf = norm_c.split("/")[-1]
            if resolved is not None:
                ext = resolved.primary_file.suffix
            elif explicit_seed:
                ext = Path(explicit_seed).suffix or ".rpp"
            elif tt.seed_file_template:
                ext = Path(tt.seed_file_template).suffix or ".rpp"
            else:
                ext = ".rpp"
            comp_details.append({
                "component_path": norm_c,
                "task_template": tt.name,
                "software": tt.software,
                "seed_file": seed_label,
                "seed_spec": seed_spec,
                "seed_scope": seed_scope,
                "initial_working_file": f"{norm_c}/working/v001/{leaf}_v001{ext}",
                "triad": [f"{norm_c}/working", f"{norm_c}/repo", f"{norm_c}/published"],
            })
        return {
            "project_name": project_name,
            "project_template": pt.name,
            "shared_folders": pt.shared_folders,
            "components": comp_details,
        }

    def capture_template(
        self,
        tier: str,
        source_path: Path,
        template_name: str,
        description: str = "",
    ) -> Path:
        """Promotes an existing working file (file), component (task), or project (project) into a reusable template."""
        tier_norm = tier.lower().strip()
        if tier_norm == "file":
            if not source_path.is_file():
                raise FileNotFoundError(f"Source seed file not found: {source_path}")
            dest_name = template_name if Path(template_name).suffix else f"{template_name}{source_path.suffix}"
            dest = self.files_dir / dest_name
            shutil.copy2(source_path, dest)
            self._sync_to_vault_if_available(dest, "files")
            return dest

        if tier_norm == "task":
            # Expects source_path to be a component dir inside a project or standalone
            project_root = self._find_project_root(source_path)
            software = "reaper"
            seed_name = "reaper_default.rpp"
            repo_routes = builtin_task_templates()["reaper_daw"].repo_routes

            if project_root is not None:
                comp_rel = normalize_rel_path(str(source_path.relative_to(project_root)))
                snap = load_component_snapshot(project_root, comp_rel)
                software = snap.software
                repo_routes = snap.repo_routes
                if snap.working_versions:
                    latest_w = snap.working_versions[-1]
                    primary_abs = source_path / latest_w.primary_file
                    if primary_abs.is_file():
                        seed_name = f"{template_name}_seed{primary_abs.suffix}"
                        shutil.copy2(primary_abs, self.files_dir / seed_name)

            tt = TaskTemplate(
                name=template_name,
                description=description or f"Captured task template from {source_path.name}",
                software=software,
                seed_file_template=seed_name,
                repo_routes=repo_routes,
            )
            dest = self.tasks_dir / f"{template_name}.toml"
            atomic_write_toml(dest, tt.to_dict())
            self._sync_to_vault_if_available(dest, "tasks")
            return dest

        if tier_norm == "project":
            comps = discover_project_components(source_path)
            comp_tpls: dict[str, str] = {}
            for c in comps:
                snap = load_component_snapshot(source_path, c)
                comp_tpls[c] = snap.task_template or "reaper_daw"
            pt = ProjectTemplate(
                name=template_name,
                description=description or f"Captured project template from {source_path.name}",
                default_components=comps or ["audio", "video"],
                component_templates=comp_tpls,
                shared_folders=["_repo"],
            )
            dest = self.projects_dir / f"{template_name}.toml"
            atomic_write_toml(dest, pt.to_dict())
            self._sync_to_vault_if_available(dest, "projects")
            return dest

        raise ValueError(f"Unknown template tier '{tier}'; expected 'project', 'task', or 'file'.")

    @staticmethod
    def _find_project_root(path: Path) -> Optional[Path]:
        curr = path.resolve()
        for parent in [curr] + list(curr.parents):
            if (parent / ".obol" / "project.toml").exists():
                return parent
        return None

    def _sync_to_vault_if_available(self, local_file: Path, subdir: str) -> None:
        if self.vault_dir and self.vault_dir.is_dir():
            vault_tpl_dir = self.vault_dir / "templates" / subdir
            vault_tpl_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(local_file, vault_tpl_dir / local_file.name)

