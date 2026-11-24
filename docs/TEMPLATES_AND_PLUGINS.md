# 3-Tier Templates, 2-Tier Software Integration & DCC Plugins

This document covers how Obol initializes projects, components, and working files using the **3-Tier Composable Template System**, and how creative applications (like Reaper DAW, Blender, Ableton Live, Krita) integrate via the **2-Tier Software & Plugin Engine**.

---

## 1. The 3-Tier Composable Template System (`src/obol/core/templates.py`)

Managed by `TemplateManager` and stored in `~/.obol/templates/` (and mirrored to `<vault>/templates/` when a Server Vault is connected):

```text
~/.obol/templates/
├── projects/                   # Tier 1: Project Hierarchy Templates (*.toml)
│   ├── simple_media.toml
│   ├── music_album.toml
│   └── single_track.toml
├── tasks/                      # Tier 2: Component / Task Templates (*.toml)
│   ├── reaper_daw.toml
│   ├── blender_3d.toml
│   ├── krita_art.toml
│   └── generic_media.toml
└── files/                      # Tier 3: Starter Working Seed Files (*.*)
    ├── reaper_default.rpp
    ├── blank.blend
    ├── canvas.kra
    └── notes.md
```

### Tier 1: Project Hierarchy Templates (`ProjectTemplate`)
Defines the initial component paths and shared folders for a new project, plus glob rules mapping component paths to Tier-2 Task Templates:
```toml
name = "music_album"
description = "Multi-song album hierarchy with cover_art and song_1/{arrangement,mixing}"
default_components = ["cover_art", "song_1/arrangement", "song_1/mixing", "song_2/arrangement"]
shared_folders = ["_repo", "_deliverables"]

[component_templates]
cover_art = "krita_art"
"*arrangement*" = "reaper_daw"
"*mixing*" = "reaper_daw"
"*" = "reaper_daw"
```

### Tier 2: Component / Task Templates (`TaskTemplate`)
Defines how a single `{working, repo, published}` component behaves:
- `software`: Key of the bound `SoftwareProfile` (e.g., `"reaper"`).
- `seed_file_template`: Filename in `templates/files/` copied into `working/v001/<leaf>_v001.<ext>` when the component is initialized.
- `repo_routes`: Maps file extensions to category prefixes inside `repo/` for automatic ingestion routing (e.g., `.wav` $\rightarrow$ `repo/audio/<asset_name>/v001/`).
- `publish_formats`: List of expected deliverable extensions surfaced as status chips in `PublishDeliverableDialog` (e.g., `reaper_daw` defaults to `[".wav", ".mid", ".mp3", ".flac"]`).

### Tier 3: Working Seed File Templates (`FileTemplate`)
Actual starter project files stored in `~/.obol/templates/files/`.
- When a component is created with `seed_initial_working=True`, Obol copies the seed file into `working/v001/<component_leaf>_v001.<ext>` and runs the bound plugin's `on_version_created()` hook.

### Template CRUD & Capture API
`TemplateManager` provides:
- `list_project_templates()`, `save_project_template(pt)`, `delete_project_template(name)`
- `list_task_templates()`, `save_task_template(tt)`, `delete_task_template(name)`
- `list_file_templates()`, `import_seed_file(path, name)`, `delete_seed_file(name)`
- `preview_project_tree(project_name, project_template_name, custom_components, component_task_overrides, component_seed_overrides)`: Generates the dry-run folder and seed-file structure displayed in the GUI's **Create New Project** preview tree.
- `capture_template(tier="project"|"task"|"file", source_path, template_name)`: Promotes any existing project root, component folder, or working file into a reusable template.

---

## 2. The 2-Tier Software Integration Engine (`src/obol/plugins/`)

Obol separates software support into **Tier A (Declarative Zero-Code Profiles)** and **Tier B (Python Hook Plugins)**.

### Tier A: Declarative `SoftwareProfile` & The 4 Version-Up Strategies
Stored in `~/.obol/config.toml` under `[software.<key>]` and editable in the GUI via **Software...** (`SoftwareSettingsDialog`).

When `ObolWorkspace.version_up_working()` is called, `execute_version_up()` (`src/obol/plugins/base.py`) executes the profile's `VersionUpStrategy`:

1. **`VersionUpStrategy.CLONE_PRIMARY_FILE`** *(Default for single-file apps like Reaper `.rpp`, Blender `.blend`, Krita `.kra`, Photoshop `.psd`)*:
   - Creates `working/vMMM/`.
   - Copies only the primary file from `working/vNNN/<leaf>_vNNN.<ext>` to `working/vMMM/<leaf>_vMMM.<ext>`.
   - Automatically skips backup/junk files (`*.rpp-bak`, `*.reapeaks`, `*.blend1`).
   - If `lock_previous_versions = True`, marks files in `working/vNNN/` as **Read-Only** (`os.chmod(..., stat.S_IREAD)`) so the user cannot accidentally overwrite historical versions inside their DAW.
2. **`VersionUpStrategy.CLONE_ENTIRE_FOLDER`** *(For folder/bundle apps like Ableton Live project directories, Godot/Unreal scenes)*:
   - Recursively copies all files and subdirectories from `working/vNNN/` to `working/vMMM/`, filtering out any paths matching `ignore_patterns` (such as `Backup/**` or `Cache/**`).
   - Renames the primary entry file from `_vNNN` to `_vMMM`.
3. **`VersionUpStrategy.COPY_WITH_SIDECARS`**:
   - Copies the primary file plus any companion files matching `sidecar_extensions`.
4. **`VersionUpStrategy.FROM_SEED_TEMPLATE`**:
   - Populates `working/vMMM/` from the component's Tier-3 Working Seed File Template rather than cloning the previous working version.

---

## 3. Tier B: Authoring a New Python Plugin (`ObolPlugin`)

Plugins implement the `ObolPlugin` protocol defined in `src/obol/plugins/base.py`:

```python
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
        """Called right after a new working/vNNN/ version is created."""

    def inspect_dependencies(
        self,
        primary_file: Path,
        ctx: TaskContext,
    ) -> tuple[list[ExternalDependency], list[str]]:
        """Scans primary_file and returns (external_dependencies_outside_repo, used_in_repo_asset_paths)."""

    def get_inspector_actions(self, ctx: TaskContext) -> list[PluginAction]:
        """Returns custom action metadata for the GUI Inspector."""
```

### Where to Place New Plugins
- **Built-in plugins**: Add to `src/obol/plugins/<name>.py` and register in `PluginRegistry._register_builtins()` (`src/obol/plugins/base.py`).
- **User dynamic plugins**: Drop any `.py` file into `~/.obol/plugins/<name>.py` defining either a `register_plugin() -> ObolPlugin` function or a `Plugin` class. `PluginRegistry.load_user_plugins()` loads it automatically at startup.

---

## 4. Deep Dive: The Reaper DAW Plugin (`src/obol/plugins/reaper.py`)

Because Reaper's `.rpp` format is human-readable plain text, `ReaperPlugin` provides deep pipeline automation without requiring Reaper to be open:

1. **Automatic Relative Path Patching (`patch_rpp_project_paths`)**:
   - Whenever `working/vNNN/<name>_vNNN.rpp` is created, `on_version_created()` rewrites (or inserts) the top-level project headers:
     - `RECORD_PATH "../../repo/recorded" ""`
     - `RENDER_FILE "../../published"`
   - Because `working/vNNN/` is always two directory levels below the component root (`<component>/working/vNNN/`), `../../repo/recorded` and `../../published` always resolve to `<component>/repo/recorded` and `<component>/published`!
2. **External Media Dependency Scanner (`inspect_dependencies`)**:
   - Scans `<SOURCE ... FILE "..."` lines inside `.rpp` files.
   - Resolves each referenced file path against the `.rpp` parent folder (`working/vNNN/`).
   - If the resolved path is inside `<component>/repo/`, it extracts the asset sub-path (everything before `/vNNN/`) and adds the working version number to `RepoAsset.used_by_working_versions`.
   - If the resolved path is **outside** `<component>/repo/` (for example, `C:\Users\...\Downloads\sub_drop_808.wav`), it returns an `ExternalDependency` with `suggested_category` (`"audio"` or `"midi"`) and `suggested_asset_name` (`"sub_drop_808"`).
3. **1-Click External Media Collector (`ObolWorkspace.collect_external_dependencies` + `ReaperPlugin.relink_rpp_references`)**:
   - Ingests each external file into `repo/<category>/<asset_name>/v001/<asset_name>_v001.<ext>` via `ingest_repo_asset()`.
   - Rewrites the `FILE "..."` line inside the `.rpp` file to use a relative path (`../../repo/audio/sub_drop_808/v001/sub_drop_808_v001.wav`).
   - Ensures the project is 100% self-contained before archiving to a Server Vault.

---

## 5. Bidirectional Live REAPER Integration (`reaper_bridge.py`, `reaper_scripts.py`, `reaper_installer.py`)

Obol pairs offline `.rpp` intelligence with a **zero-dependency Lua ReaScript + Atomic JSON IPC Bridge** under `~/.obol/bridge/reaper/`:

- **1-Click Installer (`ReaperInstaller` / `obol reaper install`)**:
  - Auto-detects REAPER's resource directory (`%APPDATA%\REAPER`) and executable (`reaper.exe`).
  - Installs pure-Lua scripts into `Scripts/Obol/` (`obol_config.lua`, `obol_lib.lua`, `obol_bridge.lua`, plus 7 REAPER Actions: Open Working File, Save & Version Up, Import Asset, Export & Publish, Collect External Media, Open Obol Studio, and Toggle Bridge).
  - Backs up `reaper-kb.ini` → `reaper-kb.ini.obol.bak` before registering deterministic `SCR` actions, optionally adds a fenced auto-start hook to `Scripts/__startup.lua`, and exports an importable `MenuSets/Obol_Toolbar.ReaperMenu` toolbar.
- **IPC Bridge (`ReaperBridgeClient`)**:
  - `obol_bridge.lua` runs a throttled `reaper.defer()` loop (~150 ms polling) writing heartbeats + active `.rpp` state to `state.json` and dispatching commands from `inbox/*.json` to `outbox/*.json`:
    - `open_project` (current tab or new project tab), `save_project`, `insert_media` (new track / cursor track / new take), `query_session_media`, `update_media_sources` (undoable `v001 → v002` take hot-swap + peak rebuild), `render_direct` (auto-close headless render `42230` with preset support from `reaper-render.ini`), and `open_render_dialog` (pre-filled native Render dialog `40015`).

---

## 6. DaVinci Resolve Integration (`resolve.py`, `resolve_bridge.py`, `resolve_scripts.py`, `resolve_installer.py`)

Obol supports both **DaVinci Resolve Free** and **DaVinci Resolve Studio** via a dual-mode architecture:

- **1-Click Installer (`ResolveInstaller` / `obol resolve install`)**:
  - Auto-detects Resolve's `Fusion/Scripts/` folder and `Resolve.exe`.
  - Installs pure-Lua menu scripts into `Utility/` (`Obol - Load Media into Media Pool.lua`, `Obol - Export and Publish.lua`, `Obol - Live Bridge.lua`, `Obol - Stop Bridge.lua`, `Obol - Open Obol Studio.lua`) and `Deliver/` (`Obol - Export and Publish.lua`), plus helper config/lib scripts under `Utility/Obol/`.
- **Dual-Mode Bridge (`ResolveBridgeClient`)**:
  - **In-Resolve Menu Scripts (Free & Studio)**: Capture session context (active project, timeline, bins, Media Pool clips, render presets, Quick Export presets) into temporary JSON, launch Obol's PySide6 dialog synchronously, and execute Media Pool imports (`ImportMedia` / `AppendToTimeline`), version updates (`ReplaceClipPreserveSubClip`), or renders (`AddRenderJob` + `StartRendering`, `RenderWithQuickExport`, Deliver page staging, `ExportCurrentFrameAsStill`, `.drp` `ExportProject`) directly inside Resolve before syncing outputs into `published/<deliverable>/vNNN/`.
  - **Studio External API / Optional Live Bridge**: Connects directly via `DaVinciResolveScript` on Resolve Studio (or via the optional `Obol - Live Bridge.lua` atomic JSON IPC loop on Resolve Free) so Obol's main GUI can push `repo/` / `published/` clips straight into Resolve's Media Pool and trigger exports.
