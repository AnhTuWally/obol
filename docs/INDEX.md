# Obol — Developer & AI Agent Onboarding Guide

Welcome to the **Obol** codebase (`d:\tu_tools\obol`). This document is designed so any developer or AI coding agent can immediately understand the system's invariants, navigate every module, run verification tests, and extend the project safely.

---

## 1. Project Philosophy & Core Invariants

Before modifying Obol, keep these **5 core architectural invariants** in mind:

1. **Zero External Runtime Dependencies Beyond Python Standard Library + `PySide6`**:
   - Reading TOML uses Python 3.11+ standard library `tomllib`.
   - Writing TOML uses our built-in pure-Python serializer (`src/obol/core/toml_io.py`).
   - Database indexing uses standard library `sqlite3` (always wrapped in `contextlib.closing()` so Windows file handles are released immediately).
   - CLI parsing uses standard library `argparse`.
   - Desktop GUI uses `PySide6`.
2. **Rule 1 — Arbitrary-Depth Triad Components**:
   - Any folder in a project directory tree that contains `{working, repo, published}` is a **Component (Task)** (`is_triad_component_dir()`).
   - Folders above a Component are simply grouping folders (`Group`). A single project can mix 1-level components (`audio/`) and multi-level components (`song_1/arrangement/`, `song_1/vocals/lead_editing/`).
3. **Rule 2 — Universal `v001/`, `v002/` Version Folder Containers**:
   - Every versioned entity in Obol stores its versions inside `v001/`, `v002/`, `v003/` subfolders (`VERSION_DIR_RE = r"^v(\d+)$"`):
     - `<component>/working/v001/`, `v002/`, ...
     - `<component>/published/<deliverable_name>/v001/`, `v002/`, ... (default deliverable `"main"`, bundling one or more output file types like `.wav` + `.mid` + `.mp3` per release; legacy flat `published/v001/` folders remain supported)
     - `<component>/repo/<asset_path>/v001/`, `v002/`, ... (where `<asset_path>` can be 1-level like `lead_vocal` or grouped by category like `audio/lead_vocal` or `midi/chord_prog`).
4. **Zero Sidecar Clutter & Self-Healing Per-Component Storage**:
   - **Never write metadata sidecar files inside `working/`, `repo/`, or `published/`.**
   - All component metadata is sharded per component inside `<project_root>/.obol/tasks/<component_slug>/` (`snapshot.toml`, `snapshot.toml.bak`, `events.jsonl`, and `archive/`).
   - `<project_root>/.obol/index.db` is a **disposable local-only SQLite read cache** that is never synced over SMB and can be regenerated at any time via `ProjectIndex(project_root).rebuild_all()`.
5. **"Local-First, Vault-Ready" Optional Server–Client Architecture**:
   - Obol works 100% standalone locally out of the box (`VaultStatus.DISABLED`) using the remembered **Client Workspace Root** (`ObolHub.get_client_root()`).
   - Users can optionally connect a filesystem / SMB / NAS **Server Vault** at any time (`ObolHub.connect_vault(path)`).
   - Selective checkout uses the **"Metadata Always Local, Media On-Demand"** principle: `.obol/` metadata (~10–50 KB) is mirrored locally while heavy media files are pulled or evicted via `SyncEngine.pull_paths()` and `SyncEngine.push_paths(evict_local=True)`.

---

## 2. Repository File Map

```text
d:\tu_tools\obol\
├── pyproject.toml                        # Package metadata & `obol` console script entrypoint
├── README.md                             # Project summary & quick start
├── docs/                                 # Comprehensive developer & agent documentation
│   ├── INDEX.md                          # (This file) Onboarding guide & repository map
│   ├── ARCHITECTURE_AND_STORAGE.md       # Folder rules, TOML/JSONL schemas, storage & reconciler
│   ├── TEMPLATES_AND_PLUGINS.md          # 3-Tier templates, 2-Tier software profiles & DCC plugins
│   ├── SERVER_CLIENT_SYNC.md             # Client root, optional SMB Vault & selective checkout
│   └── GUI_AND_CLI_GUIDE.md              # 3-Pane PySide6 GUI, dark theme, dialogs & CLI reference
├── src/
│   └── obol/
│       ├── __init__.py                   # Package version
│       ├── core/
│       │   ├── models.py                 # Dataclasses, enums, and TOML-safe dict serialization
│       │   ├── toml_io.py                # Pure-Python TOML writer (compatible with stdlib tomllib)
│       │   ├── storage.py                # Atomic TOML I/O, CRC32 events.jsonl, compaction & Reconciler
│       │   ├── index.py                  # Disposable local SQLite cache (.obol/index.db)
│       │   ├── templates.py              # 3-Tier TemplateManager (Project, Task, Seed File)
│       │   ├── seed_templates.py         # Scoped & versioned software seed templates (Project -> Root -> Global)
│       │   ├── hub.py                    # Global ObolHub (~/.obol/ config, client root, projects, vault)
│       │   ├── sync.py                   # Stateless Server-Client SyncEngine (pull_paths / push_paths)
│       │   └── workspace.py              # High-level ObolWorkspace pipeline operations facade
│       ├── plugins/
│       │   ├── base.py                   # ObolPlugin protocol, PluginRegistry & 4 VersionUpStrategies
│       │   ├── generic.py                # Fallback OS / declarative software plugin
│       │   ├── reaper.py                 # Reaper DAW (.rpp) path patcher & external media collector
│       │   ├── reaper_bridge.py          # Atomic file-based JSON IPC bridge & render preset parser
│       │   ├── reaper_scripts.py         # Pure-Lua ReaScript & toolbar menu generators
│       │   ├── reaper_installer.py       # 1-click REAPER ReaScript & bridge installer/uninstaller
│       │   ├── resolve.py                # DaVinci Resolve (.drp) plugin
│       │   ├── resolve_bridge.py         # Dual-mode Resolve client (Studio Python API + Atomic JSON IPC)
│       │   ├── resolve_scripts.py        # Pure-Lua Resolve menu & live bridge script generators
│       │   ├── resolve_installer.py      # 1-click DaVinci Resolve script installer/uninstaller
│       │   ├── filmcraft.py              # FilmCraft NLE (.fcproj) JSON plugin & external media collector
│       │   ├── filmcraft_bridge.py       # Dual-mode FilmCraft client (TCP --control bridge + headless CLI)
│       │   ├── filmcraft_installer.py    # FilmCraft bridge config & --control launcher installer
│       │   ├── integrations.py           # Unified DCCIntegrationManager & config-drift diagnostics
│       │   ├── media_registry.py         # Unified DCCMediaRegistry, loaded-media scan & version up/down
│       │   ├── send_registry.py          # Unified DCCSendRegistry, target ranking & built-in send adapters
│       │   └── blender.py                # Blender 3D (.blend) plugin
│       ├── cli/
│       │   └── main.py                   # Pure-Python argparse CLI (`obol ...`)
│       └── gui/
│           ├── theme.py                  # Fusion dark QPalette, QSS stylesheet & SVG branch chevrons
│           ├── drop_widgets.py           # OS drag-and-drop / drag-out widgets & tab hover switcher
│           ├── dialogs.py                # Project/Vault, Create Project, Template, Software, Ingest & Publish modals
│           ├── integrations_dialog.py    # Unified Master-Detail DCC Integrations Manager dialog
│           ├── media_manager_dialog.py   # Unified DCC Media Manager dialog (list, update check, version up/down)
│           ├── reaper_dialogs.py         # Focused REAPER companion dialogs (Open, Import/Update, Export, Setup)
│           ├── resolve_dialogs.py        # Focused DaVinci Resolve dialogs (Media Pool Load/Update, Export, Setup)
│           ├── filmcraft_dialogs.py      # Focused FilmCraft dialogs (Import/Update, Export/Publish, Setup)
│           └── app.py                    # Main 3-Pane ObolMainWindow + DCC Exchange Bar
└── tests/                                # Automated unittest / pytest suite
    ├── test_storage_resilience.py        # Fault isolation, compaction, reconciler & SQLite rebuild
    ├── test_hierarchy_and_repo.py        # Mixed-depth components, versioned repo & provenance credits
    ├── test_software_and_version_up.py   # Software profiles, version-up strategies & user plugins
    ├── test_reaper_plugin.py             # .rpp path patching & external media collection into repo/
    ├── test_reaper_integration.py        # REAPER IPC bridge, Lua installer, CLI, dialogs & drag-out
    ├── test_resolve_integration.py       # DaVinci Resolve dual-mode bridge, Lua installer, CLI & dialogs
    ├── test_filmcraft_integration.py     # FilmCraft dual-mode bridge, headless CLI, dialogs & relinking
    ├── test_integrations_manager.py      # Unified DCC integration manager, drift detection & UI smoke
    ├── test_dcc_media_manager.py         # Unified DCCMediaRegistry, loaded media scan & version up/down
    ├── test_dcc_send_registry.py         # Unified DCCSendRegistry, target ranking & DCC Exchange Bar UI
    ├── test_templates.py                 # 3-tier template preview, CRUD & capture
    ├── test_seed_templates.py            # Scoped & versioned seed template cascade, pinning & CLI/GUI
    ├── test_server_client_sync.py        # Client root persistence, vault archival & selective checkout
    ├── test_ingest_and_multipublish.py   # Working ingest, named multi-file deliverables, export sync & CLI
    ├── test_delete_operations.py         # Safe deletion of versions, named assets/deliverables, components & projects
    └── test_gui_smoke.py                 # Headless Qt GUI, drag-and-drop & CLI end-to-end verification
```

---

## 3. Running & Testing Obol

### Run the Test Suite
Always run the full test suite after making any changes:
```powershell
$env:PYTHONPATH="src"; python -m unittest discover -s tests -v
```
All headless GUI tests (including `tests/test_gui_smoke.py` and `tests/test_dcc_send_registry.py`) set `QT_QPA_PLATFORM=offscreen` automatically so they run headlessly in CI/terminal environments without popping up blocking windows.

### Launch the Desktop GUI
```powershell
$env:PYTHONPATH="src"; python -m obol.cli.main gui
```

### Run CLI Commands
```powershell
$env:PYTHONPATH="src"; python -m obol.cli.main --help
```
*(In tests or isolated environments, set `OBOL_HOME` to a temporary directory or pass `ObolHub(hub_dir=...)` so you don't mutate the user's real `~/.obol/` directory.)*

---

## 4. Recommended Next Enhancements (Phase 2 Roadmap)

When extending Obol in future sessions, here are the highest-value Phase 2 features already prepared by the current architecture:

1. **Smart Dependency-Driven Selective Checkout (`SyncEngine` + `ObolPlugin.inspect_dependencies`)**:
   - Allow checking out a single working version (e.g., `working/v004/song_v004.rpp`) from the Server Vault and automatically inspecting its `.rpp` contents to pull **only** the `repo/` asset versions referenced by `v004`.
2. **Auto-Eviction Retention Rules**:
   - Add per-project or per-component retention policies (e.g., *"Keep only the latest 3 working versions locally; offload older versions to Server Vault on push"*).
3. **In-App DCC Companion Scripts (ReaScript / Blender Add-on)**:
   - *(Implemented for REAPER)* — 1-click Lua ReaScript suite + atomic JSON IPC bridge (`obol reaper install`), with a companion Blender Python add-on remaining on the Phase 2 roadmap.
