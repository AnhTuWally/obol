# Obol

**Obol** (`v0.1.0`) is a pure-Python personal media production pipeline and multi-project file manager inspired by [Prism Pipeline](https://prism-pipeline.com/). It has zero external runtime dependencies beyond the Python 3.11+ standard library and `PySide6` (`>=6.6.0`).

---

## Core Architecture

- **Arbitrary-Depth Triad Components**: Any directory containing `{working, repo, published}` is recognized as a Component (Task), allowing flat (`audio/`) and deeply nested (`song_1/arrangement/`, `song_1/vocals/lead_editing/`) structures within the same project.
- **Universal `v001/` Version Containers**: Every versioned entity across `working/`, `repo/<asset_path>/`, and `published/<deliverable_name>/` stores releases in `vNNN/` folders—supporting single files, multi-file deliverables, and full directory bundles—with provenance metadata (`author`, `source`, `license`, `tags`, custom key-value fields), attribution/credits reporting, and working-to-published lineage.
- **Zero Sidecar Clutter & Self-Healing Storage**: Component metadata is sharded per component under `<project>/.obol/tasks/<slug>/` (`snapshot.toml` + `.bak`, CRC32-verified `events.jsonl` with 100-line auto-compaction, a self-healing filesystem `Reconciler`, and a disposable local SQLite cache at `.obol/index.db`).
- **Local-First, Vault-Ready Sync**: Works 100% standalone locally via a remembered Client Workspace Root (`~/.obol/config.toml`), and optionally connects to an SMB/NAS/filesystem Server Vault with stateless path-filter selective checkout (`pull_paths` / `push_paths`), SHA-256 verified atomic transfers, and safe local offloading/archival.

---

## Current Capabilities

### 3-Pane PySide6 Desktop GUI (`obol gui`)
- Studio dark Fusion theme with custom SVG branch chevrons and packaged app icon (`src/obol/gui/assets/obol_icon.{ico,png}`).
- Zero-project welcome screen, searchable hierarchical component tree, and tabbed Triad workspace (`Working`, `Repo Assets`, `Published`) featuring OS drag-and-drop/drag-out, multi-selection on `Repo Assets` / `Published`, tab-bar hover switching, and a `Fast Drop` toggle.
- Dedicated **DCC Exchange Bar** docked below the Triad tabs with an Auto/Pinned Target DCC selector, per-DCC Send Mode selector (remembered per DCC), 1-click `→ Send` / `↻ Update` (`Ctrl+D`), `← Export (<DCC>)…`, and `↻` live status refresh, keeping right-click menus focused and instant.
- Context-aware Inspector & Actions pane, keyboard shortcuts (`Ctrl+N`, `Ctrl+Shift+N`, `Ctrl+U`, `Ctrl+I`, `Ctrl+P`, `Ctrl+D`, `Ctrl+S`, `Ctrl+Shift+S`, `F5`, `Delete`), and safe read-only-aware deletion across versions, named assets/deliverables, components, and projects.
- Built-in dialogs for New Project Wizard (with live folder/seed preview), 3-Tier Template & Scoped Seed Manager, Software Profiles, Projects & Vault Manager, Credits Report (Markdown copy & CSV export), and the Unified DCC Integrations Manager.

### 3-Tier Composable Template Engine
- **Tier 1 (Project Hierarchy)**: `simple_media`, `music_album`, `single_track`
- **Tier 2 (Component / Task)**: `reaper_daw`, `resolve_video`, `filmcraft_video`, `blender_3d`, `krita_art`, `generic_media`
- **Tier 3 (Scoped & Versioned Software Seed Templates)**: Project → Client Workspace Root → Global resolution cascade with multiple named versioned seed streams (`v001/`, `v002/`, ...), `Always Latest` or pinned `vNNN` defaults, plus legacy flat starter seed files (`reaper_default.rpp`, `filmcraft_default.fcproj`, `blank.blend`, `canvas.kra`, `notes.md`).
- Supports dry-run tree previews and capturing existing projects, components, or files into reusable templates.

### 2-Tier Software Profiles & DCC Integrations
- Declarative `SoftwareProfile` entries supporting 4 `VersionUpStrategy` modes (`clone_primary_file`, `clone_entire_folder`, `copy_with_sidecars`, `from_seed_template`), ignore patterns, optional read-only locking of previous working versions, and dynamic user plugins loaded from `~/.obol/plugins/`.
- **REAPER DAW**: Offline `.rpp` relative path patching (`RECORD_PATH`, `RENDER_FILE`), external media scanner & 1-click collector/relinker into `repo/`, 1-click Lua ReaScript installer (9 registered Actions, `Obol_Toolbar.ReaperMenu`, optional `__startup.lua` auto-bridge), atomic JSON IPC bridge, **Increment Save** (`Ctrl+Shift+S` / `Obol: IncSave`, saving directly to `working/vNNN/` via `Main_SaveProjectEx` while keeping the new version active), **Session Media & MIDI Version Updater** (`P_EXT:OBOL_ASSET` take tracking, live or offline `.rpp` updates via in-place replacement or new A/B takes, and configurable MIDI handling), and focused companion dialogs (Open, Import/Update takes, Hybrid Direct/Native Render & Publish).
- **DaVinci Resolve (Free & Studio)**: 1-click Lua menu script installer (`Utility/` and `Deliver/`), Media Pool import & clip version updating (`ReplaceClipPreserveSubClip`), Render Queue / Quick Export / Deliver staging / Still / `.drp` publishing, Studio Python API + optional Free live IPC bridge, and remembered Resolve-project-to-Obol-component bindings.
- **FilmCraft NLE**: Dual-mode `.fcproj` JSON plugin + `filmcraft-cli` headless & token-authenticated `--control` live bridge integration, relative path external media scanning & 1-click collector/relinker into `repo/`, project media version updater (`media.replaceFootage`), preset/format sequence exports, still renders, and XML/FCPXML/EDL/OTIO/AAF/OMF interchange publishing.
- **Blender 3D**: `.blend` plugin with automatic `.blend1`/`.blend2` backup tidying into `.backups/`.
- **Unified DCC Send Registry & Integrations Manager**: Extensible `DCCSendRegistry` (`src/obol/plugins/send_registry.py`) backing the GUI's DCC Exchange Bar (supporting built-in REAPER, Resolve, and FilmCraft adapters plus custom user plugin `get_send_target(hub)` hooks), alongside centralized health/config-drift diagnostics, 1-click install/update/uninstall, and connection testing via the GUI or `obol integrations`.

### Command-Line Interface (`obol`)
Pure-Python `argparse` CLI covering `root`, `init`, `project`, `task`, `work`, `ingest`, `publish`, `credits`, `template`, `seed`, `software`, `vault`, `push`, `archive`, `checkout`, `delete`, `reaper`, `resolve`, `filmcraft`, `integrations`, and `gui`.

---

## Installation & Quick Start

**Requirements**: Python `>= 3.11` and `PySide6 >= 6.6.0` (optional `Pillow` and `numpy` only if regenerating icons with `scripts/make_icon.py`).

### Option 1: Editable Install (Recommended)

```powershell
pip install -e .

# Launch the 3-Pane Desktop GUI
obol gui

# Inspect CLI commands and DCC integration health
obol --help
obol integrations status
```

### Option 2: Run Directly from Source

```powershell
# Launch the 3-Pane PySide6 Desktop GUI
$env:PYTHONPATH="src"; python -m obol.cli.main gui

# Run CLI commands
$env:PYTHONPATH="src"; python -m obol.cli.main --help

# Run the automated unit test suite (59 tests across 15 modules; GUI tests run headless via QT_QPA_PLATFORM=offscreen)
$env:PYTHONPATH="src"; python -m unittest discover -s tests -v
```

---

## Developer & AI Agent Documentation (`docs/`)

Start with **[docs/INDEX.md](docs/INDEX.md)** for core invariants, the repository file map, and the Phase 2 roadmap:

- **[docs/INDEX.md](docs/INDEX.md)** — Onboarding guide, core invariants, repository file map, and development roadmap.
- **[docs/ARCHITECTURE_AND_STORAGE.md](docs/ARCHITECTURE_AND_STORAGE.md)** — Arbitrary-depth Triad hierarchy, universal `v001/` version containers, TOML/JSONL schemas, CRC32 fault-isolated ledger, auto-compaction, self-healing reconciler, and disposable SQLite cache.
- **[docs/TEMPLATES_AND_PLUGINS.md](docs/TEMPLATES_AND_PLUGINS.md)** — 3-Tier Composable Template Engine, Scoped & Versioned Software Seed Templates, 2-Tier Software Integration, the 4 `VersionUpStrategy` modes, REAPER `.rpp` & ReaScript bridge internals, DaVinci Resolve dual-mode integration, FilmCraft NLE dual-mode integration, and guide for authoring new DCC plugins.
- **[docs/SERVER_CLIENT_SYNC.md](docs/SERVER_CLIENT_SYNC.md)** — Remembered Client Workspace Root, "Local-First, Vault-Ready" optional SMB/NAS Server Vault, and stateless path-based selective checkout/archival (`pull_paths` & `push_paths`).
- **[docs/GUI_AND_CLI_GUIDE.md](docs/GUI_AND_CLI_GUIDE.md)** — 3-Pane PySide6 Desktop GUI architecture, dialogs, Fusion dark theme + SVG chevrons, headless Qt testing, and complete CLI reference.

---

## Roadmap (Planned)

- **Smart Dependency-Driven Selective Checkout**: Automatically inspect working files (e.g., `.rpp`) when checking out from a Server Vault to pull only the referenced `repo/` asset versions.
- **Auto-Eviction Retention Rules**: Per-project or per-component policies to keep the latest *N* working versions locally and offload older versions to the Server Vault.
- **Blender Python Add-on**: Companion in-app add-on for Blender matching the REAPER and DaVinci Resolve workflows.
