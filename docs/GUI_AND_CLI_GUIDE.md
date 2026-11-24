# 3-Pane PySide6 Desktop GUI & CLI Reference

This document describes the architecture of the PySide6 Desktop GUI (`src/obol/gui/`) and the command-line interface (`src/obol/cli/main.py`).

---

## 1. Desktop GUI Architecture (`src/obol/gui/`)

### A. Main Window Layout (`ObolMainWindow` in `src/obol/gui/app.py`)
To keep day-to-day navigation effortless and clutter-free, the main window uses a **Streamlined Top Bar + Central Stack (Welcome Screen / 3-Pane Splitter) + Bottom Status Bar**:

1. **Top Header Bar (`QFrame#TopBar`)**:
   - **Left (Project Navigation)**: `project_combo` (searchable dropdown switcher), `+ New Project` (`Ctrl+N`), and `Projects & Vault...` (`ProjectAndVaultManagerDialog`).
   - **Right (Workflow & Studio Settings)**:
     - `Fast Drop: OFF` / `⚡ Fast Drop: ON` toggle button (highlighted in amber when `ON`). When `OFF`, dropping or ingesting opens the corresponding staging/naming dialog (`IngestWorkingDialog`, `SmartIngestDialog`, or `PublishDeliverableDialog`). When `ON`, dropped files ingest/publish immediately with auto-routed defaults.
     - `↻ Refresh` (`F5`): Runs `Reconciler.reconcile_project()` to pick up external disk changes and refreshes the UI.
     - `Credits`: Opens `CreditsReportDialog`.
     - `⚙ Settings ▾` menu: Consolidates `3-Tier Templates...`, `Software Profiles...`, `DCC Integrations...`, `Change Client Workspace Root...`, and `Open Global Hub Folder (~/.obol)`.

2. **Bottom Status Bar (`QFrame#StatusBar`)**:
   - Clickable path breadcrumb button (opens active folder in OS Explorer), transient status confirmation messages (`✓ Saved metadata...`), `Root: <folder>` quick selector, clickable `DCC` summary pill (`DCC: Setup…` / `DCC: <N> Ready` / `DCC: ... Live ●`) + live connected pills, and clickable `Vault` status pill (`Standalone Local` / `Vault Online` / `Vault Offline`).

3. **Zero-Project Welcome Screen (`central_stack` page 0)**:
   - When no project is loaded, displays an onboarding card explaining the Triad workflow with `+ Create New Project`, `Register Existing Project Folder...`, and `Projects & Vault...` buttons.

4. **Pane 1 (Left) — Hierarchical Component Tree & Search (`self.component_tree`)**:
   - Renders multi-level component paths (`song_1/arrangement`, `song_1/mixing`) as collapsible group folders (`song_1/`) alongside their bound software key (`reaper`, `krita`, `blender`).
   - Includes a live `Filter components…` search bar and `+ Add` button (`AddComponentDialog` with template/software picker, `Ctrl+Shift+N`), plus right-click context menu (`+ Add Component...`, `Open Folder in Explorer`, `Copy Folder Path`, `Capture as Component Template...`, `Delete Component...`).

5. **Pane 2 (Center) — Tabbed Triad Workspace with OS Drag-and-Drop (`self.triad_tabs`)**:
   - Powered by `DropTreeWidget` (`src/obol/gui/drop_widgets.py`) with `dragActive` drop-zone highlighting, painted empty-state drop guidance, and tab-bar hover auto-switching (~350 ms hover switches tabs mid-drag):
   - **Tab 0 (`Working`)**: Multi-column table (`Version` | `Primary File` | `Created` | `Note`) showing `★` on latest and `🔒` on read-only locked versions. Double-click launches that exact working version; right-click offers `Open`, `Version Up from vNNN...`, `Publish from vNNN...`, `Save as Working Seed Template...`, `Open Folder`, `Copy Path`, and `Delete`.
   - **Tab 1 (`Repo Assets`)**: Horizontal `QSplitter` Master–Detail layout:
     - *Left (`self.repo_asset_tree`)*: Collapsible category folders (`audio/` → `lead_vocal`) showing `Ver` and `Used` counts, with `+ Slot` and `+ Ingest...`. Dropping onto empty space or a category folder creates a new named Repo Asset; dropping onto an existing asset adds `v(N+1)`.
     - *Right (`self.repo_version_list`)*: Multi-column version list (`Version` | `File` | `Size` | `Note`) with double-click to open and right-click actions.
   - **Tab 2 (`Published`)**: Horizontal `QSplitter` Master–Detail layout:
     - *Left (`self.published_deliverable_tree`)*: Named Deliverable streams (`main`, `stems`, `midi_pack`, etc.) with `+ Slot` and `+ Publish...` (`Ctrl+P`).
     - *Right (`self.published_version_tree`)*: Expandable version tree (`Version / File` | `Types` | `Source / Size` | `Note`) showing format badges (`WAV, MID, MP3`), working lineage (`← working/v002`), child files (double-click to open), `1. Export Folder` / `2. Sync Exports` buttons for direct multi-format DCC exports, and right-click context menus.

6. **Pane 3 (Right) — Context-Aware Inspector & Actions**:
   - Automatically adapts to the active Triad tab (`Working Version`, `Repo Asset`, or `Published`):
     - **Component Settings**: `Bound Software` selector (updates `component_tree` immediately and shows executable status hint).
     - **Selection Inspector**: Shows lineage/usage (`Used in working: v001, v002` or `Source: working/v002`), shows `Author` / `Source` / `License` fields on the Repo Assets tab, and edits `Tags` & `Note` across all 3 tabs (`Save Metadata (Ctrl+S)`).
     - **Contextual Actions**: `Open <Selected>` (launches the selected Working version, Repo file, or Published file), `Version Up + Open`, `Publish from Working...`, `Scan & Collect External .rpp Media` (for Reaper), `Open Folder`, `Copy Path`, granular Vault `Check Out Item` / `Offload Item` (when `VaultStatus.ONLINE`), and `Delete Selected...`.

7. **Keyboard Shortcuts**:
   - `Ctrl+N` New Project • `Ctrl+Shift+N` Add Component • `Ctrl+U` Version Up • `Ctrl+I` Ingest into active tab • `Ctrl+P` Publish • `Ctrl+S` Save Inspector Metadata • `F5` Refresh & Reconcile • `Delete` Delete focused item.

---

### B. Dialogs (`src/obol/gui/dialogs.py`)
- **`AddComponentDialog` ("Add Triad Component")**:
  - Path field (auto-prefixed when a group folder is selected), Component Template dropdown with live software/seed summary, optional Bound Software override, and initial `working/v001` seed toggle.
- **`NewProjectWizardDialog` ("Create New Project")**:
  - Left side: Project Name, optional Project Code, Client Root Path picker (with live destination path preview and `"Remember as default Client Workspace Root"` checkbox), Tier-1 Project Template selector (with template description and inline `Configure Templates...` button), Tier-2 Component Template selector, Tier-3 Working Seed File selector, and custom component adder (`+ Add Component`, `Apply Template to Selected`, `Remove Selected`). Per-component overrides are persisted when creating the project.
  - Right side: Live interactive `QTreeWidget` preview of the folders and initial `v001` seed files that will be generated.
- **`TemplateManagerDialog` ("3-Tier Template Manager")**:
  - **Tab 1 (`1. Project Hierarchy Templates`)**: Create, edit, delete, or capture (`Capture Active Project`) Tier-1 project templates.
  - **Tab 2 (`2. Component / Task Templates`)**: Create, edit, delete, or capture (`Capture Active Component`) Tier-2 component templates (bound software, default seed file, and `repo/` category routing rules).
  - **Tab 3 (`3. Working Seed Files`)**: Import starter files (`.rpp`, `.blend`, `.kra`, etc.) from disk, launch a seed file directly in its associated app to edit its default setup, delete seed files, or open `~/.obol/templates/` in Explorer.
- **`ProjectAndVaultManagerDialog`**:
  - Manage the remembered **Client Root (Local)**, optional **Server Vault**, and registered projects (highlights the active `★` project, double-click to switch, 1-click `Open`, `Backup`, `Archive (Free Space)`, `Check Out`, safe `Remove...` offering `Unregister Only (Keep Files)` vs `Delete Files from Disk`, `+ New Project...`, and `Register Existing Project Folder...`).
- **`SmartIngestDialog`**:
  - Name-first Repo ingest modal supporting both single-file (`Name` + optional `Category`, live `repo/` target preview, `Browse...` or dialog-level drag-and-drop) and multi-file batch table mode (`Source File`, `Name`, `Category`, `Target Version`), plus `Author`, `Source`, `License`, `Tags`, and `Comment`.
- **`IngestWorkingDialog`**:
  - Confirms external working file(s) or folder bundles dropped onto `working/` (`Rename primary file to component convention`, `copy`/`move` mode, `Version Note`, and `Tags`).
- **`PublishDeliverableDialog`**:
  - Multi-file-type deliverable staging modal featuring editable `Deliverable Name`, `Source Working Version` binding, `New Release` vs. `Add to existing vNNN/` selector, expected format chips (`WAV`, `MID`, `MP3`, etc.), a drag-and-drop `DropTableWidget` staging table with per-file `Output Label` and live target path preview, and `Keep original filenames` toggle.
- **`SoftwareSettingsDialog`**:
  - Visual editor for `SoftwareProfile` entries with `+ New Profile`, `Browse...` + `Auto-Detect` executable picker with disk verification hint, `DCC Integrations...` shortcut, friendly `VersionUpStrategy` labels, `extensions`, `ignore_patterns`, `lock_previous_versions`, and `plugin`.
- **`IntegrationsManagerDialog` (`src/obol/gui/integrations_dialog.py`)**:
  - Unified master-detail **DCC Integrations Manager** (`⚙ Settings → DCC Integrations...` or bottom-bar `DCC` pill) showing live connection status, config-drift/health diagnostics (`✓` / `⚠` / `✗`), `Install / Update All Detected`, `Test Connection`, `Launch App`, quick folder links, quick-start guides, and Resolve project-to-component bindings management.
- **`ReaperOpenDialog` / `ReaperImportDialog` / `ReaperExportPublishDialog` / `ReaperIntegrationDialog` (`src/obol/gui/reaper_dialogs.py`)**:
  - Focused REAPER companion dialogs launched either directly from REAPER Actions (`obol reaper ui --mode open|import|publish|setup`) or from Obol's GUI (`Export from REAPER...`, `REAPER Integration...`), supporting live `.rpp` tab opening, `repo/` + cross-component `published/` media import & session take version updating (`v001 → v002`), hybrid direct/native rendering into `published/`, and 1-click Lua ReaScript installation.
- **`ResolveImportDialog` / `ResolveExportPublishDialog` / `ResolveIntegrationDialog` (`src/obol/gui/resolve_dialogs.py`)**:
  - Focused DaVinci Resolve companion dialogs (Free & Studio compatible) for loading `repo/` and cross-component `published/` media into Resolve's Media Pool (with bin targeting, timeline append, and Media Pool clip version updating), exporting/publishing renders, Quick Exports, Deliver staging, stills, and `.drp` snapshots, and 1-click script installation.
- **`CreditsReportDialog`**:
  - Searchable/filterable table of `ObolWorkspace.generate_credits_report()` with `Used In` column, amber highlights on `Unspecified` / `Temp` licenses, `Copy Credits (Markdown)`, and `Export CSV...`.

---

### C. Dark Theme, Drop-Zone Highlights & SVG Icons (`src/obol/gui/theme.py`)
> [!WARNING]
> **Windows Native Style Pitfall in Qt/PySide6**:
> On Windows, Qt's default `"windowsvista"` style draws native black tree branch expand/collapse arrows and white `QHeaderView` backgrounds when custom QSS dark backgrounds are used.
>
> To prevent dark-on-dark chevrons and white-on-white headers, `apply_studio_dark_theme()` in `src/obol/gui/theme.py`:
> 1. Sets `app.setStyle("Fusion")` and applies a complete dark `QPalette`.
> 2. Generates high-contrast SVG chevron icons (`chevron_right.svg` in `#e4e4e7` and `chevron_down.svg` in `#60a5fa`) in `%TEMP%/obol_gui_icons/` and binds them to `QTreeView::branch` and `QComboBox::down-arrow`.
> 3. Explicitly styles `QHeaderView::section` with a `#1f1f23` dark surface and `#d4d4d8` text, `[dragActive="true"]` drop highlights, `#DangerButton` delete buttons, compact `#DropHint` captions, and `#FormatChipActive`/`#FormatChipIdle` deliverable badges.

---

## 2. Command-Line Interface Reference (`src/obol/cli/main.py`)

All CLI commands can be invoked via `python -m obol.cli.main <command>` (or `obol <command>` when installed via `pip install -e .`):

```powershell
# Client Workspace Root (Remembered in ~/.obol/config.toml)
obol root show
obol root set D:\pROD\the_wkly

# Project Creation & Management
obol init <name> [--path <dir>] [--template simple_media|music_album|single_track] [--code <code>]
obol project create <name> [--template ...]
obol project list
obol project register <existing_project_path>

# Component (Task) Management
obol task create <component_path> [-p <project>] [--template reaper_daw|blender_3d|krita_art|generic_media]
obol task list [-p <project>]

# Working File Versioning, Ingest & Reaper Media Collection
obol work up <component_path> [-p <project>] [-m "Version comment"] [--strategy clone_primary_file|clone_entire_folder|copy_with_sidecars|from_seed_template]
obol work ingest <file_or_dir>... -t <component_path> [-p <project>] [-m "Version comment"] [--keep-name]
obol work open <component_path> [-p <project>] [-v <version_num>]
obol work scan <component_path> [-p <project>] [--collect]

# Version-Controlled Repo Ingestion & Provenance Credits
obol ingest <file_path> --task <component_path> [-p <project>] [--asset <subpath>] [--author "..."] [--source "..."] [--license "..."] [--tag <tag>] [-m "comment"] [--meta key=val]
obol credits [-p <project>]

# Publishing Named Multi-File Deliverables
obol publish <file1> [<file2> ...] --task <component_path> [-p <project>] [--name main|stems|...] [--from-working <ver_num>] [--into-version <pub_ver>] [--keep-names] [-m "comment"]

# Deleting Versions, Named Repo Assets/Deliverables, Components & Projects
obol delete working <component_path> -v <ver_num> [-p <project>]
obol delete repo <asset_path> -t <component_path> [-v <ver_num>] [-p <project>]
obol delete publish <deliverable_name> -t <component_path> [-v <ver_num>] [-p <project>]
obol delete component <component_path> [-p <project>] [--keep-files]
obol delete project <name_or_code> [--delete-files]

# 3-Tier Templates & 2-Tier Software Profiles
obol template list
obol template capture <source_path> --name <template_name> --tier project|task|file
obol software list
obol software set <key> --name "Display Name" --exe "C:\path\to\app.exe" --ext .ext --strategy clone_primary_file

# Optional SMB / Filesystem Server Vault & Selective Checkout
obol vault connect \\NAS\ObolVault
obol vault status
obol vault disconnect
obol push <project> [--filter <glob>]
obol archive <project> [--filter <glob>]
obol checkout <project> [--filter <glob>]

# REAPER DAW Integration & ReaScript Bridge
obol reaper status [--resource-dir <dir>] [--json]
obol reaper install [--resource-dir <dir>] [--exe <path>] [--no-autostart] [--json]
obol reaper uninstall [--resource-dir <dir>] [--json]
obol reaper context --rpp <path> [--json]
obol reaper version-up --rpp <path> [-m "Note"] [--json]
obol reaper collect --rpp <path> [--json]
obol reaper prepare-publish --rpp <path> [--name main] [--into-version <ver>] [--json]
obol reaper sync-publish --rpp <path> --name main --version <ver> [-m "Note"] [--json]
obol reaper ui --mode open|import|publish|setup [--rpp <path>]

# DaVinci Resolve Integration & Media Pool / Publish Bridge
obol resolve status [--scripts-dir <dir>] [--json]
obol resolve install [--scripts-dir <dir>] [--exe <path>] [--json]
obol resolve uninstall [--scripts-dir <dir>] [--json]
obol resolve bind --resolve-project <name> -p <project> -t <component_path> [--json]
obol resolve prepare-publish -p <project> -t <component_path> [--name main] [--into-version <ver>] [--json]
obol resolve sync-publish -p <project> -t <component_path> --name main --version <ver> [--from-working <ver>] [-m "Note"] [--json]
obol resolve ui --mode import|publish|setup [--context-file <json>] [--result-file <json>]

# Unified DCC Integrations Manager
obol integrations status [--no-studio-api] [--json]
obol integrations install [reaper|resolve] [--all] [--target-dir <dir>] [--exe <path>] [--no-autostart] [--json]
obol integrations uninstall reaper|resolve [--target-dir <dir>] [--json]
obol integrations ui [--dcc reaper|resolve]

# Launch 3-Pane Desktop GUI
obol gui [-p <project>] [-c <component_path>]
```
