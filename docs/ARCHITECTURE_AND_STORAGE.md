# Architecture, Directory Rules & Fault-Tolerant Storage

This document explains Obol's filesystem layout, TOML and JSONL data schemas, pure-Python TOML serialization, CRC32 fault-isolated event ledger, automatic snapshot compaction, self-healing reconciler, and disposable SQLite cache.

---

## 1. The Two Universal Filesystem Rules

### Rule 1: Any Folder Containing `{working, repo, published}` is a Component
Implemented in `src/obol/core/storage.py` (`is_triad_component_dir()` and `discover_project_components()`):
- Obol does **not** enforce a fixed folder depth (like `Category -> Entity -> Task`).
- Instead, `discover_project_components(project_root)` walks the project tree (ignoring folders starting with `.` or `_`) and identifies any folder that contains the three subdirectories `working/`, `repo/`, and `published/` as a **Component**.
- A Component is always referenced by its normalized POSIX relative path from `project_root` (e.g., `"audio"`, `"song_1/arrangement"`, or `"act_1/scene_2/sfx/foley"`).
- When storing metadata inside `<project_root>/.obol/tasks/`, the component path is converted to a directory-safe slug by replacing `/` with `__` (`component_path_to_slug("song_1/arrangement") -> "song_1__arrangement"`). The canonical `component_path` is also stored inside `snapshot.toml`.

### Rule 2: Universal `v001/`, `v002/` Version Folder Containers
Every versioned unit across `working/`, `published/`, and `repo/` uses `vNNN/` version subfolders (`v001/`, `v002/`, ...):

```text
<project_root>/
├── .obol/
│   ├── project.toml
│   ├── index.db                          # Disposable local-only SQLite query cache
│   └── tasks/
│       └── song_1__audio/
│           ├── snapshot.toml             # Authoritative compacted state for song_1/audio
│           ├── snapshot.toml.bak         # Atomic backup copy of previous snapshot.toml
│           ├── events.jsonl              # Recent events since last compaction (CRC32 verified)
│           └── archive/
│               └── events_001.jsonl      # Rotated historical event batches
│
└── song_1/                               # Grouping folder
    └── audio/                            # Component Triad
        ├── working/                      # Direct Version Container
        │   ├── v001/
        │   │   └── audio_v001.rpp
        │   └── v002/
        │       └── audio_v002.rpp
        ├── published/                    # Named Deliverable Streams -> Multi-Type Version Bundles
        │   ├── main/
        │   │   └── v001/
        │   │       ├── main_v001.wav
        │   │       └── main_v001.mid
        │   └── stems/
        │       └── v001/
        │           ├── stems_drums_v001.wav
        │           └── stems_bass_v001.wav
        └── repo/                         # Nested Repo Asset Tree -> Version Containers
            ├── audio/
            │   └── lead_vocal/           # Repo Asset ("audio/lead_vocal")
            │       ├── v001/
            │       │   └── lead_vocal_v001.wav
            │       └── v002/
            │           └── lead_vocal_v002.wav
            └── midi/
                └── chord_prog/           # Repo Asset ("midi/chord_prog")
                    └── v001/
                        └── chord_prog_v001.mid
```

Why `v001/`, `v002/` folders everywhere?
1. **Supports both Single-File and Multi-File/Folder Bundles**: Whether an app saves one file (`.rpp`, `.blend`, `.kra`), a multi-type deliverable release (`.wav` + `.mid` + `.mp3` inside `published/main/v001/`), or an entire directory tree (Ableton Live project folders), `v001/` holds it cleanly without colliding with `v002/`.
2. **Self-Identifying Repo Assets & Published Deliverables**: Inside both `repo/` and `published/`, `Reconciler` can distinguish category/grouping folders from actual `RepoAsset`s or `PublishedDeliverable`s simply by checking whether a folder's children match `^v\d+$` (while remaining backwards-compatible with legacy flat `published/vNNN/` folders)!

---

## 2. Configuration & Metadata Schemas

### A. Global Config (`~/.obol/config.toml`)
Managed by `ObolHub` (`src/obol/core/hub.py`) and modeled by `GlobalConfig` (`src/obol/core/models.py`):
```toml
default_workspace_root = "D:/pROD/the_wkly"
last_active_project = "wkly_41"
author_name = "Dan Choi"
fast_drop = false

[vault]
enabled = true
path = "//NAS/ObolVault"
auto_sync_metadata = true

[software.reaper]
key = "reaper"
display_name = "REAPER DAW"
executable = "C:/Program Files/REAPER (x64)/reaper.exe"
extensions = [".rpp"]
launch_args = ["{file}"]
version_up_strategy = "clone_primary_file"
naming_pattern = "{component_leaf}_{version}{ext}"
ignore_patterns = ["*.rpp-bak", "*.reapeaks", ".backups/**"]
sidecar_extensions = []
lock_previous_versions = true
plugin = "reaper"
```

### B. Component Snapshot (`<project>/.obol/tasks/<slug>/snapshot.toml`)
Managed by `src/obol/core/storage.py` and modeled by `ComponentSnapshot`:
```toml
component_path = "song_1/audio"
task_template = "reaper_daw"
software = "reaper"
created_at = "2026-11-23T09:00:00+00:00"
last_event_id = "evt_9a8b7c6d5e4f"

[[working_versions]]
version = 1
folder_rel = "working/v001"
primary_file = "working/v001/audio_v001.rpp"
created_at = "2026-11-23T09:00:00+00:00"
comment = "Initial v001 from seed template 'reaper_default.rpp'"
parent_version = 0
source_working_version = 0
tags = []
files = [{ rel_path = "working/v001/audio_v001.rpp", sha256 = "e3b0c442...", size_bytes = 312 }]

[repo_routes]
audio = [".wav", ".flac", ".mp3", ".ogg", ".aiff"]
midi = [".mid", ".midi"]
references = [".mp4", ".mov", ".pdf", ".txt"]

[repo_assets."audio/lead_vocal"]
asset_path = "audio/lead_vocal"
name = "lead_vocal"
category = "audio"
author = "Sarah Jenkins"
source = "Studio Session A"
license = "Work for Hire"
tags = ["vocal", "128bpm"]
latest_version = 1
used_by_working_versions = [1]

[[repo_assets."audio/lead_vocal".versions]]
version = 1
folder_rel = "repo/audio/lead_vocal/v001"
primary_file = "repo/audio/lead_vocal/v001/lead_vocal_v001.wav"
created_at = "2026-11-23T09:15:00+00:00"
comment = "Dry vocal stem"
parent_version = 0
source_working_version = 0
tags = ["vocal", "128bpm"]
files = [{ rel_path = "repo/audio/lead_vocal/v001/lead_vocal_v001.wav", sha256 = "2c26b46b...", size_bytes = 42150400 }]

[repo_assets."audio/lead_vocal".versions.custom]
bpm = 128
key = "Am"

[published_deliverables.main]
deliverable_path = "main"
name = "main"
latest_version = 1

[[published_deliverables.main.versions]]
version = 1
folder_rel = "published/main/v001"
primary_file = "published/main/v001/main_v001.wav"
created_at = "2026-11-23T09:30:00+00:00"
comment = "Master WAV + MIDI"
parent_version = 0
source_working_version = 1
tags = []
files = [
  { rel_path = "published/main/v001/main_v001.wav", sha256 = "9f86d081...", size_bytes = 24000000, file_type = "wav" },
  { rel_path = "published/main/v001/main_v001.mid", sha256 = "4b227777...", size_bytes = 1240, file_type = "mid" },
]
```

### C. Pure-Python TOML Serializer (`src/obol/core/toml_io.py`)
> [!IMPORTANT]
> **Maintainability Note on `toml_io.py`**:
> - TOML has no `null` / `None` type. Always run dictionaries through `clean_for_toml()` (`src/obol/core/models.py`) before serializing.
> - Inside an Array-of-Tables item (`[[working_versions]]` or `[[repo_assets."...".versions]]`), inline array fields like `files = [{ ... }]` **must** be emitted *before* any nested subtable header like `[working_versions.custom]`. `toml_io.py` enforces this ordering so keys are never accidentally swallowed by a preceding subtable header.

---

## 3. Fault-Tolerant Event Ledger & Auto-Compaction (`src/obol/core/storage.py`)

### Line-Level CRC32 Protection
Every mutation (`COMPONENT_CREATED`, `WORKING_VERSIONED`, `WORKING_INGESTED`, `WORKING_DISCOVERED`, `WORKING_VERSION_DELETED`, `REPO_ASSET_CREATED`, `REPO_INGESTED`, `REPO_METADATA_UPDATED`, `REPO_VERSION_DELETED`, `REPO_ASSET_DELETED`, `DELIVERABLE_CREATED`, `FILE_PUBLISHED`, `FILE_PUBLISHED_ADDED`, `PUBLISHED_VERSION_DELETED`, `DELIVERABLE_DELETED`) appends a single line to `<project>/.obol/tasks/<slug>/events.jsonl`:

```json
{"id":"evt_4f81a2c90123","ts":"2026-11-23T09:15:00+00:00","event":"REPO_INGESTED","component":"song_1/audio","payload":{"asset_path":"audio/lead_vocal","version":1},"crc":"a1b2c3d4"}
```

- `compute_event_crc()` computes an 8-character lowercase hex CRC32 (`zlib.crc32`) over the canonical sorted-key JSON of `{id, ts, event, component, payload}`.
- `read_events_fault_tolerant()` reads `events.jsonl` line-by-line:
  - If a line has invalid JSON syntax (e.g., power loss mid-write) or its `crc` field does not match `compute_event_crc()`, the reader records a warning and **skips only that single line**.
  - All preceding and following lines remain 100% readable.

### 100-Line Auto-Compaction
In `save_component_snapshot(project_root, snapshot, compaction_threshold=100)`:
1. Writes `snapshot.toml.tmp`, copies the old `snapshot.toml` to `snapshot.toml.bak`, and calls `os.replace()` for an atomic update.
2. Checks the line count of `events.jsonl`. If it reaches `100` lines, moves `events.jsonl` to `archive/events_NNN.jsonl` and resets `events.jsonl` to an empty file.

---

## 4. Self-Healing Filesystem Reconciler (`Reconciler.reconcile_component()`)

If `snapshot.toml` and `snapshot.toml.bak` are both deleted—or if a user manually creates/drops files into `working/`, `repo/`, or `published/` using Windows Explorer or Reaper's native *Save As* dialog—`Reconciler.reconcile_component(project_root, component_path)` heals the state automatically:

1. **Replays Ledger History**: Replays any archived `archive/events_*.jsonl` plus current `events.jsonl` to recover provenance metadata (`author`, `source`, `license`, `tags`, `custom`) for `RepoAsset`s and `source_working_version` / comments for `PublishedDeliverable`s.
2. **Scans `working/`**:
   - Discovers any `vNNN/` directories on disk not yet in `snapshot.working_versions` and hashes their files (populating `TrackedFile.file_type`).
   - Detects any loose working files saved directly in `working/` (e.g. `working/my_new_save.rpp`), automatically creates the next `working/vNNN/` subfolder, moves the file into it, and logs a `WORKING_DISCOVERED` event.
3. **Scans `published/` Recursively**: Discovers named deliverables (`published/<deliverable_name>/vNNN/`) into `snapshot.published_deliverables` (and refreshes `files` when additional file types are added on disk), while preserving backwards compatibility with any legacy flat `published/vNNN/` directories.
4. **Scans `repo/` Recursively**: Finds every directory under `repo/` that contains `vNNN/` children, registers/updates the corresponding `RepoAsset` and `VersionBundle`s, and saves the healed `snapshot.toml`.
5. **Safe Read-Only Deletion (`safe_rmtree`)**: All `delete_*` APIs in `ObolHub` and `ObolWorkspace` clear Windows read-only attributes before removing historical version folders (`working/vNNN/`, `repo/.../vNNN/`, `published/.../vNNN/`), named assets/deliverables, components, or projects.

---

## 5. Disposable Local SQLite Cache (`src/obol/core/index.py`)

- `ProjectIndex` manages `<project_root>/.obol/index.db` in WAL mode (`PRAGMA journal_mode=WAL;`).
- **Windows File-Locking Rule**: Every `sqlite3` operation in `index.py` uses `with contextlib.closing(self._connect()) as conn:` followed by `with conn:`. Never use bare `with sqlite3.connect(...) as conn:` without `contextlib.closing()`, because Python's `sqlite3.Connection.__exit__` commits the transaction without closing the file descriptor, which locks `index.db` on Windows.
- If `index.db` is missing or raises `sqlite3.DatabaseError`, calling `ProjectIndex(project_root).rebuild_all()` unlinks `index.db` (and `-wal`/`-shm` files) and reconstructs all tables (`components`, `versions`, `repo_assets`) from `snapshot.toml` files.
