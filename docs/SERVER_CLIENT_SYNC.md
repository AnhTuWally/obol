# Client Workspace Root & Optional Server–Client Sync Architecture

This document details how Obol manages the local **Client Workspace Root** and how the optional **Filesystem / SMB Server Vault** enables multi-project archival and granular selective checkout.

---

## 1. Remembered Client Workspace Root (`ObolHub`)

Implemented in `src/obol/core/hub.py`:

- **`ObolHub.get_client_root() -> Path`**:
  Returns the persisted `default_workspace_root` from `~/.obol/config.toml` (defaulting to `~/ObolProjects` on first run).
- **`ObolHub.set_client_root(root_path: Path) -> Path`**:
  1. Resolves and creates `root_path`.
  2. Persists `default_workspace_root` to `~/.obol/config.toml`.
  3. Updates `local_path` for any `REMOTE_ONLY` (un-checked-out) projects in `projects.toml` so future checkouts land in the new Client Root.
  4. Runs `discover_client_root_projects()`, which scans immediate subdirectories of `root_path` for `<child>/.obol/project.toml` and registers them automatically.
- **`ObolHub.set_last_active_project(project_name: str)`**:
  Persists `last_active_project` in `~/.obol/config.toml` so reopening `obol gui` restores the user's last active project automatically.

---

## 2. "Local-First, Vault-Ready" Progression

Obol never requires a server to be configured before a user can start working:

1. **Standalone Local Mode (`VaultStatus.DISABLED`)**:
   - Default state when `config.vault.enabled == False`.
   - The GUI hides remote sync controls and operates purely on local projects inside the Client Workspace Root.
2. **Vault-Connected Mode (`VaultStatus.ONLINE` / `VaultStatus.OFFLINE`)**:
   - Enabled at any time via `hub.connect_vault(Path("\\\\NAS\\ObolVault"))` (or via `obol vault connect <path>` / the GUI `Projects / Vault...` dialog).
   - Creates `<vault_path>/projects/` and `<vault_path>/templates/`.
   - Because projects on the Server Vault use the **exact same folder structure** (`.obol/` + component triads) as local projects, existing local projects can be pushed or archived to the Vault with **zero migration or folder restructuring**.
   - If the SMB share or external drive is temporarily disconnected (` not vault_path.is_dir()`), `get_vault_status()` returns `VaultStatus.OFFLINE` and Obol continues working on local checked-out files without errors.

---

## 3. Stateless Selective Checkout: "Metadata Always Local, Media On-Demand" (`src/obol/core/sync.py`)

Most selective-checkout systems become fragile because they maintain a separate state database tracking which folders or versions are checked out. Obol avoids that completely:

### How Presence is Computed Statelessly
1. **`SyncEngine.sync_metadata(project_name)`**:
   - Mirrors `<project>/.obol/project.toml` and `<project>/.obol/tasks/*/snapshot.toml` + `events.jsonl` bidirectionally between the Local Client Workspace and `<vault>/projects/<project>/.obol/` (never copying the local-only `index.db`).
   - Because `.obol/` is only a few kilobytes of text, the client always has the complete manifest of every component, `working` version, `repo` asset version, and `published` version—even when `0 MB` of media files are present on the local drive.
2. **`SyncEngine.compute_file_presence(project_name, proj_rel_path)`**:
   - Checks `local_file.is_file()` and `vault_file.is_file()`:
     - Local exists, Vault does not $\rightarrow$ `FilePresence.LOCAL_ONLY`
     - Local does not exist, Vault exists $\rightarrow$ `FilePresence.REMOTE_ONLY` (`"On Server"`)
     - Both exist with matching size $\rightarrow$ `FilePresence.SYNCED` (`"Backed Up"`)
     - Both exist with differing size $\rightarrow$ `FilePresence.MODIFIED_LOCAL`
3. **`SyncEngine.compute_project_presence(project_name)`**:
   - Aggregates file presence across all tracked files in the project's component snapshots:
     - All `REMOTE_ONLY` $\rightarrow$ `FilePresence.REMOTE_ONLY` (Archived on Server)
     - All `SYNCED` $\rightarrow$ `FilePresence.SYNCED`
     - Mix of `REMOTE_ONLY` and `LOCAL_ONLY`/`SYNCED` $\rightarrow$ `FilePresence.SPARSE` (Selective / Sparse Checkout)

### The Single Path-Filter Primitive (`push_paths` & `pull_paths`)
Both backup/archival and checkout operate on a list of glob `path_filters` relative to the project root:

| Target Granularity | Example `path_filters` |
| :--- | :--- |
| **Whole Project** | `["**"]` |
| **Group (e.g., all of Song 1)** | `["song_1/**"]` |
| **Single Component** | `["song_1/audio/**"]` |
| **Single Triad Branch** | `["song_1/audio/published/**"]` |
| **Single Repo Asset (all versions)** | `["song_1/audio/repo/audio/lead_vocal/**"]` |
| **Single Version (`v002`)** | `["song_1/audio/repo/audio/lead_vocal/v002/**"]` or `["song_1/audio/working/v002/**"]` |
| **Single File** | `["song_1/audio/working/v002/audio_v002.rpp"]` |

### Hash-Verified Atomic SMB Transfer & Safe Eviction Guard
In `SyncEngine._atomic_verified_copy(src, dst, expected_sha256)` and `push_paths(..., evict_local=True)`:
1. Files are never copied directly over the destination filename on the SMB share. Instead, Obol copies to `<dst>.obol-part`.
2. Obol computes the SHA-256 hash of `<dst>.obol-part` on the destination and verifies it matches the source SHA-256 hash.
3. Only after checksum verification passes does Obol call `os.replace("<dst>.obol-part", "<dst>")`.
4. **Eviction Safety Guard**: When `evict_local=True` (used by `obol archive` / GUI **"Archive (Free Space)"** and **"Offload Item"**), Obol verifies that `vault_file.is_file()` is `True` and `compute_file_sha256(vault_file) == expected_sha` **before** calling `local_file.unlink()`. If an SMB transfer is interrupted or corrupted, zero local files are deleted.
