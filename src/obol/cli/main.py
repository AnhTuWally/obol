"""Obol Command-Line Interface (pure Python argparse implementation)."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

from obol.core.hub import ObolHub
from obol.core.models import SoftwareProfile, VersionUpStrategy
from obol.core.storage import discover_project_components, load_component_snapshot
from obol.core.sync import SyncEngine
from obol.core.workspace import ObolWorkspace


def _resolve_project_root(hub: ObolHub, project_arg: Optional[str]) -> tuple[str, Path]:
    if project_arg:
        entry = hub.get_project(project_arg)
        if not entry:
            raise SystemExit(f"Error: Project '{project_arg}' is not registered.")
        return entry.name, Path(entry.local_path)

    cwd = Path.cwd().resolve()
    for candidate in [cwd] + list(cwd.parents):
        if (candidate / ".obol" / "project.toml").is_file():
            entry = hub.register_project(candidate)
            return entry.name, candidate

    projects = hub.list_projects()
    if len(projects) == 1:
        return projects[0].name, Path(projects[0].local_path)
    raise SystemExit("Error: Could not detect active project. Pass -p / --project <name> or cd into a project folder.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="obol",
        description="Obol — Personal Media Production Pipeline & Multi-Project File Manager",
    )
    sub = parser.add_subparsers(dest="command")

    # obol init
    p_init = sub.add_parser("init", help="Initialize a new Obol project from a template")
    p_init.add_argument("name", help="Project name")
    p_init.add_argument("--path", help="Target directory (defaults to <client_root>/<name>)")
    p_init.add_argument("--template", default="simple_media", help="Tier-1 Project template (simple_media, music_album, single_track)")
    p_init.add_argument("--code", help="Short project code")

    # obol root
    p_root = sub.add_parser("root", help="View or set the remembered Client Workspace Root location")
    root_sub = p_root.add_subparsers(dest="root_cmd")
    root_sub.add_parser("show", help="Show the currently remembered Client Workspace Root")
    p_rset = root_sub.add_parser("set", help="Set and remember the Client Workspace Root directory")
    p_rset.add_argument("path", help="Directory path for local client projects")

    # obol project
    p_proj = sub.add_parser("project", help="Create, list, or register projects")
    proj_sub = p_proj.add_subparsers(dest="project_cmd")
    proj_sub.add_parser("list", help="List all registered local and vault projects")
    for create_alias in ("create", "new"):
        p_pnew = proj_sub.add_parser(create_alias, help="Create a new project in the Client Workspace Root")
        p_pnew.add_argument("name", help="Project name")
        p_pnew.add_argument("--path", help="Target directory (defaults to <client_root>/<name>)")
        p_pnew.add_argument(
            "--template",
            default="simple_media",
            help="Tier-1 Project template (simple_media, music_album, single_track)",
        )
        p_pnew.add_argument("--code", help="Short project code")
    p_reg = proj_sub.add_parser("register", help="Register an existing project directory")
    p_reg.add_argument("path", help="Path to existing project root containing .obol/")

    # obol task
    p_task = sub.add_parser("task", help="Create or list Triad Components (Tasks)")
    task_sub = p_task.add_subparsers(dest="task_cmd")
    p_tc = task_sub.add_parser("create", help="Create a new component triad at any depth (e.g. audio or song_1/mixing)")
    p_tc.add_argument("component_path", help="Relative component path (e.g. 'audio' or 'song_1/mixing')")
    p_tc.add_argument("-p", "--project", help="Project name or code")
    p_tc.add_argument("--template", default="reaper_daw", help="Tier-2 Task template (reaper_daw, blender_3d, krita_art, generic_media)")
    p_tl = task_sub.add_parser("list", help="List all components in a project")
    p_tl.add_argument("-p", "--project", help="Project name or code")

    # obol work
    p_work = sub.add_parser("work", help="Manage working/ versions and software launches")
    work_sub = p_work.add_subparsers(dest="work_cmd")
    p_wup = work_sub.add_parser("up", help="Version up a component's working folder (v001 -> v002)")
    p_wup.add_argument("component_path", help="Component path")
    p_wup.add_argument("-p", "--project", help="Project name or code")
    p_wup.add_argument("-m", "--comment", default="", help="Version comment")
    p_wup.add_argument("--strategy", choices=[s.value for s in VersionUpStrategy], help="Override Version-Up strategy")

    p_wopen = work_sub.add_parser("open", help="Launch a working version in its bound software")
    p_wopen.add_argument("component_path", help="Component path")
    p_wopen.add_argument("-p", "--project", help="Project name or code")
    p_wopen.add_argument("-v", "--version", type=int, help="Specific version number (defaults to latest)")

    p_wscan = work_sub.add_parser("scan", help="Scan working file (e.g. Reaper .rpp) for external dependencies outside repo/")
    p_wscan.add_argument("component_path", help="Component path")
    p_wscan.add_argument("-p", "--project", help="Project name or code")
    p_wscan.add_argument("--collect", action="store_true", help="Auto-ingest external media into repo/ and relink working file")

    p_wing = work_sub.add_parser("ingest", help="Ingest external working file(s) or folder into the next working/vNNN/")
    p_wing.add_argument("files", nargs="+", help="Working file(s) or folder to ingest")
    p_wing.add_argument("-t", "--task", required=True, help="Target component path")
    p_wing.add_argument("-p", "--project", help="Project name or code")
    p_wing.add_argument("-m", "--comment", default="", help="Version comment")
    p_wing.add_argument("--keep-name", action="store_true", help="Keep original filename instead of renaming to component convention")

    # obol ingest
    p_ing = sub.add_parser("ingest", help="Ingest an external file into a versioned repo/ asset container")
    p_ing.add_argument("file", help="Path to external file to ingest")
    p_ing.add_argument("-t", "--task", required=True, help="Target component path (e.g. 'audio' or 'song_1/audio')")
    p_ing.add_argument("-p", "--project", help="Project name or code")
    p_ing.add_argument("--asset", help="Explicit asset sub-path inside repo/ (e.g. 'audio/lead_vocal' or 'midi/chords')")
    p_ing.add_argument("--author", default="", help="Original author / creator")
    p_ing.add_argument("--source", default="", help="Original source URL, pack, or session")
    p_ing.add_argument("--license", default="", help="License or usage rights (e.g. CC-BY-4.0, Royalty-Free)")
    p_ing.add_argument("--tag", action="append", default=[], help="Tag (can be specified multiple times)")
    p_ing.add_argument("-m", "--comment", default="", help="Version comment")
    p_ing.add_argument("--meta", action="append", default=[], help="Custom metadata key=value")

    # obol publish
    p_pub = sub.add_parser("publish", help="Publish exported deliverable(s) into published/<name>/vNNN/")
    p_pub.add_argument("files", nargs="+", help="File(s) to publish")
    p_pub.add_argument("-t", "--task", required=True, help="Component path")
    p_pub.add_argument("-p", "--project", help="Project name or code")
    p_pub.add_argument("--name", default="main", help="Deliverable name inside published/ (default: main)")
    p_pub.add_argument("--from-working", type=int, help="Source working version number (defaults to latest)")
    p_pub.add_argument("--into-version", type=int, help="Append file(s) into an existing published version instead of bumping")
    p_pub.add_argument("--keep-names", action="store_true", help="Keep original filenames inside the release bundle")
    p_pub.add_argument("-m", "--comment", default="", help="Publish comment")

    # obol credits
    p_cred = sub.add_parser("credits", help="Generate project attribution & license report from repo/ assets")
    p_cred.add_argument("-p", "--project", help="Project name or code")

    # obol template
    p_tpl = sub.add_parser("template", help="List or capture 3-tier templates")
    tpl_sub = p_tpl.add_subparsers(dest="tpl_cmd")
    tpl_sub.add_parser("list", help="List available Project, Task, and Seed File templates")
    p_cap = tpl_sub.add_parser("capture", help="Capture an existing file, component, or project as a reusable template")
    p_cap.add_argument("source", help="Path to source file, component folder, or project root")
    p_cap.add_argument("--name", required=True, help="Template name")
    p_cap.add_argument("--tier", choices=["project", "task", "file"], required=True, help="Template tier")

    # obol software
    p_sw = sub.add_parser("software", help="List or configure software profiles and version-up strategies")
    sw_sub = p_sw.add_subparsers(dest="sw_cmd")
    sw_sub.add_parser("list", help="List configured software profiles")
    p_swset = sw_sub.add_parser("set", help="Add or update a software profile")
    p_swset.add_argument("key", help="Software key (e.g. reaper, blender, ableton, krita)")
    p_swset.add_argument("--name", help="Display name")
    p_swset.add_argument("--exe", default="", help="Executable path")
    p_swset.add_argument("--ext", action="append", default=[], help="File extension (e.g. .rpp)")
    p_swset.add_argument(
        "--strategy",
        choices=[s.value for s in VersionUpStrategy],
        default=VersionUpStrategy.CLONE_PRIMARY_FILE.value,
        help="Version-Up strategy",
    )

    # obol vault / push / archive / checkout
    p_vault = sub.add_parser("vault", help="Manage optional SMB / Network Server Vault connection")
    vault_sub = p_vault.add_subparsers(dest="vault_cmd")
    p_vconn = vault_sub.add_parser("connect", help="Connect a folder or SMB path as the Server Vault")
    p_vconn.add_argument("path", help="Path to Server Vault directory")
    vault_sub.add_parser("disconnect", help="Disconnect Server Vault and return to Standalone Local Mode")
    vault_sub.add_parser("status", help="Show Server Vault connection status")

    p_push = sub.add_parser("push", help="Backup (push) project or filtered paths to the Server Vault")
    p_push.add_argument("project", help="Project name or code")
    p_push.add_argument("--filter", action="append", default=[], help="Optional path glob filter (e.g. 'song_1/audio/**')")

    p_arch = sub.add_parser("archive", help="Push to Server Vault, verify SHA-256, and offload local media files")
    p_arch.add_argument("project", help="Project name or code")
    p_arch.add_argument("--filter", action="append", default=[], help="Optional path glob filter to selectively offload")

    p_co = sub.add_parser("checkout", help="Selectively check out project, component, repo asset, version, or file from Server Vault")
    p_co.add_argument("project", help="Project name or code")
    p_co.add_argument("--filter", action="append", default=[], help="Optional path glob filter (e.g. '*/repo/audio/lead_vocal/v002/*')")

    # obol delete
    p_del = sub.add_parser("delete", help="Delete a working/repo/published version, named repo/deliverable, component, or project")
    del_sub = p_del.add_subparsers(dest="delete_cmd")

    p_dp = del_sub.add_parser("project", help="Unregister a project (and optionally delete local files)")
    p_dp.add_argument("name", help="Project name or code")
    p_dp.add_argument("--delete-files", action="store_true", help="Also delete the local project directory tree on disk")

    p_dc = del_sub.add_parser("component", help="Delete a component triad and its metadata")
    p_dc.add_argument("component_path", help="Component path")
    p_dc.add_argument("-p", "--project", help="Project name or code")
    p_dc.add_argument("--keep-files", action="store_true", help="Keep component files on disk and only remove .obol metadata")

    p_dw = del_sub.add_parser("working", help="Delete a working version (working/vNNN/)")
    p_dw.add_argument("component_path", help="Component path")
    p_dw.add_argument("-v", "--version", type=int, required=True, help="Version number to delete")
    p_dw.add_argument("-p", "--project", help="Project name or code")

    p_dr = del_sub.add_parser("repo", help="Delete a named repo asset or a specific repo version")
    p_dr.add_argument("asset_path", help="Repo asset path (e.g. audio/lead_vocal)")
    p_dr.add_argument("-t", "--task", required=True, help="Component path")
    p_dr.add_argument("-v", "--version", type=int, help="Version number to delete (omit to delete entire named asset)")
    p_dr.add_argument("-p", "--project", help="Project name or code")

    p_dpub = del_sub.add_parser("publish", help="Delete a named deliverable or a specific published version")
    p_dpub.add_argument("deliverable_name", help="Deliverable name (e.g. main, stems)")
    p_dpub.add_argument("-t", "--task", required=True, help="Component path")
    p_dpub.add_argument("-v", "--version", type=int, help="Version number to delete (omit to delete entire deliverable)")
    p_dpub.add_argument("-p", "--project", help="Project name or code")

    # obol reaper
    p_reap = sub.add_parser("reaper", help="REAPER DAW bridge, ReaScript installer, and session helpers")
    reap_sub = p_reap.add_subparsers(dest="reaper_cmd")

    p_rstat = reap_sub.add_parser("status", help="Inspect REAPER integration & live bridge status")
    p_rstat.add_argument("--resource-dir", help="Override REAPER resource directory")
    p_rstat.add_argument("--json", action="store_true", help="Output JSON")

    p_rinst = reap_sub.add_parser("install", help="Install Obol Lua ReaScripts, Actions, and bridge into REAPER")
    p_rinst.add_argument("--resource-dir", help="Override REAPER resource directory")
    p_rinst.add_argument("--exe", default="", help="Optional path to reaper.exe")
    p_rinst.add_argument("--no-autostart", action="store_true", help="Do not add bridge auto-start to __startup.lua")
    p_rinst.add_argument("--json", action="store_true", help="Output JSON")

    p_runinst = reap_sub.add_parser("uninstall", help="Remove Obol Lua ReaScripts and hooks from REAPER")
    p_runinst.add_argument("--resource-dir", help="Override REAPER resource directory")
    p_runinst.add_argument("--json", action="store_true", help="Output JSON")

    p_rctx = reap_sub.add_parser("context", help="Resolve an .rpp path to its Obol project, component, and working version")
    p_rctx.add_argument("--rpp", required=True, help="Path to .rpp file")
    p_rctx.add_argument("--json", action="store_true", help="Output JSON")

    p_rvup = reap_sub.add_parser("version-up", help="Version up the Obol component owning the given .rpp file")
    p_rvup.add_argument("--rpp", required=True, help="Path to active working .rpp file")
    p_rvup.add_argument("-m", "--comment", default="", help="Version note")
    p_rvup.add_argument("--json", action="store_true", help="Output JSON")

    p_rcol = reap_sub.add_parser("collect", help="Scan and collect external media referenced in an .rpp into repo/")
    p_rcol.add_argument("--rpp", required=True, help="Path to active working .rpp file")
    p_rcol.add_argument("--json", action="store_true", help="Output JSON")

    p_rprep = reap_sub.add_parser("prepare-publish", help="Create the next published/<deliverable>/vNNN/ export folder for an .rpp")
    p_rprep.add_argument("--rpp", required=True, help="Path to active working .rpp file")
    p_rprep.add_argument("--name", default="main", help="Deliverable name (default: main)")
    p_rprep.add_argument("--into-version", type=int, help="Target existing version number")
    p_rprep.add_argument("--json", action="store_true", help="Output JSON")

    p_rsync = reap_sub.add_parser("sync-publish", help="Register files rendered into published/<deliverable>/vNNN/")
    p_rsync.add_argument("--rpp", required=True, help="Path to active working .rpp file")
    p_rsync.add_argument("--name", default="main", help="Deliverable name (default: main)")
    p_rsync.add_argument("--version", type=int, required=True, help="Prepared published version number")
    p_rsync.add_argument("-m", "--comment", default="", help="Release note")
    p_rsync.add_argument("--tag", action="append", default=[], help="Tag (can be specified multiple times)")
    p_rsync.add_argument("--json", action="store_true", help="Output JSON")

    p_rui = reap_sub.add_parser("ui", help="Launch a focused Obol PySide6 companion dialog for REAPER")
    p_rui.add_argument("--mode", choices=["open", "import", "publish", "setup"], required=True, help="Companion dialog mode")
    p_rui.add_argument("--rpp", default="", help="Active REAPER .rpp path")
    p_rui.add_argument("-p", "--project", default="", help="Project name override")
    p_rui.add_argument("-c", "--component", default="", help="Component path override")

    # obol resolve
    p_dvr = sub.add_parser("resolve", help="DaVinci Resolve bridge, Lua script installer, and session helpers")
    dvr_sub = p_dvr.add_subparsers(dest="resolve_cmd")

    p_dstat = dvr_sub.add_parser("status", help="Inspect DaVinci Resolve integration & live bridge status")
    p_dstat.add_argument("--scripts-dir", help="Override Resolve Fusion/Scripts directory")
    p_dstat.add_argument("--no-studio-api", action="store_true", help="Skip external Studio Python API check")
    p_dstat.add_argument("--json", action="store_true", help="Output JSON")

    p_dinst = dvr_sub.add_parser("install", help="Install Obol Lua scripts into DaVinci Resolve")
    p_dinst.add_argument("--scripts-dir", help="Override Resolve Fusion/Scripts directory")
    p_dinst.add_argument("--exe", default="", help="Optional path to Resolve executable")
    p_dinst.add_argument("--no-studio-api", action="store_true", help="Skip external Studio Python API check")
    p_dinst.add_argument("--json", action="store_true", help="Output JSON")

    p_duninst = dvr_sub.add_parser("uninstall", help="Remove Obol Lua scripts from DaVinci Resolve")
    p_duninst.add_argument("--scripts-dir", help="Override Resolve Fusion/Scripts directory")
    p_duninst.add_argument("--no-studio-api", action="store_true", help="Skip external Studio Python API check")
    p_duninst.add_argument("--json", action="store_true", help="Output JSON")

    p_dbind = dvr_sub.add_parser("bind", help="Get or set remembered Resolve project -> Obol component binding")
    p_dbind.add_argument("--resolve-project", required=True, help="DaVinci Resolve project name")
    p_dbind.add_argument("-p", "--project", default="", help="Obol project name")
    p_dbind.add_argument("-t", "--task", default="", help="Obol component path")
    p_dbind.add_argument("--json", action="store_true", help="Output JSON")

    p_dprep = dvr_sub.add_parser("prepare-publish", help="Create the next published/<deliverable>/vNNN/ export folder")
    p_dprep.add_argument("-p", "--project", help="Project name or code")
    p_dprep.add_argument("-t", "--task", required=True, help="Component path")
    p_dprep.add_argument("--name", default="main", help="Deliverable name (default: main)")
    p_dprep.add_argument("--into-version", type=int, help="Target existing version number")
    p_dprep.add_argument("--json", action="store_true", help="Output JSON")

    p_dsync = dvr_sub.add_parser("sync-publish", help="Register files rendered by Resolve into published/<deliverable>/vNNN/")
    p_dsync.add_argument("-p", "--project", help="Project name or code")
    p_dsync.add_argument("-t", "--task", required=True, help="Component path")
    p_dsync.add_argument("--name", default="main", help="Deliverable name (default: main)")
    p_dsync.add_argument("--version", type=int, required=True, help="Prepared published version number")
    p_dsync.add_argument("--from-working", type=int, help="Source working version number")
    p_dsync.add_argument("-m", "--comment", default="", help="Release note")
    p_dsync.add_argument("--tag", action="append", default=[], help="Tag (can be specified multiple times)")
    p_dsync.add_argument("--json", action="store_true", help="Output JSON")

    p_dui = dvr_sub.add_parser("ui", help="Launch a focused Obol PySide6 companion dialog for DaVinci Resolve")
    p_dui.add_argument("--mode", choices=["import", "publish", "setup"], required=True, help="Companion dialog mode")
    p_dui.add_argument("--context-file", default="", help="Path to JSON session context exported by Resolve Lua")
    p_dui.add_argument("--result-file", default="", help="Path to write execution plan JSON back to Resolve Lua")
    p_dui.add_argument("-p", "--project", default="", help="Project name override")
    p_dui.add_argument("-c", "--component", default="", help="Component path override")

    # obol integrations
    p_int = sub.add_parser("integrations", help="Unified DCC integration manager (REAPER, DaVinci Resolve)")
    int_sub = p_int.add_subparsers(dest="int_cmd")

    p_istat = int_sub.add_parser("status", help="Show health, drift, and bridge status for all DCC integrations")
    p_istat.add_argument("--no-studio-api", action="store_true", help="Skip external Resolve Studio API check")
    p_istat.add_argument("--json", action="store_true", help="Output JSON")

    p_iinst = int_sub.add_parser("install", help="Install or update one or all detected DCC integrations")
    p_iinst.add_argument("dcc", nargs="?", choices=["reaper", "resolve"], help="DCC key (omit with --all)")
    p_iinst.add_argument("--all", action="store_true", help="Install or update all detected DCCs")
    p_iinst.add_argument("--target-dir", help="Override target resource/scripts directory for a single DCC")
    p_iinst.add_argument("--exe", default="", help="Optional executable path override")
    p_iinst.add_argument("--no-autostart", action="store_true", help="REAPER: do not add __startup.lua bridge hook")
    p_iinst.add_argument("--no-studio-api", action="store_true", help="Skip external Resolve Studio API check")
    p_iinst.add_argument("--json", action="store_true", help="Output JSON")

    p_iuninst = int_sub.add_parser("uninstall", help="Uninstall a DCC integration")
    p_iuninst.add_argument("dcc", choices=["reaper", "resolve"], help="DCC key to uninstall")
    p_iuninst.add_argument("--target-dir", help="Override target resource/scripts directory")
    p_iuninst.add_argument("--no-studio-api", action="store_true", help="Skip external Resolve Studio API check")
    p_iuninst.add_argument("--json", action="store_true", help="Output JSON")

    p_iui = int_sub.add_parser("ui", help="Launch the unified DCC Integrations Manager window")
    p_iui.add_argument("--dcc", default="", choices=["", "reaper", "resolve"], help="Initial DCC to select")

    # obol gui
    p_gui = sub.add_parser("gui", help="Launch the 3-Pane Obol Desktop GUI")
    p_gui.add_argument("-p", "--project", default="", help="Initial project name to open")
    p_gui.add_argument("-c", "--component", default="", help="Initial component path to select")

    return parser


def run_cli(argv: Optional[list[str]] = None, hub: Optional[ObolHub] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    active_hub = hub or ObolHub()
    ws = ObolWorkspace(active_hub)
    sync = SyncEngine(active_hub)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "init":
        base_dir = Path(args.path) if args.path else (active_hub.get_client_root() / args.name)
        proj_root = ws.init_project(
            name=args.name,
            root_path=base_dir,
            project_template=args.template,
            code=args.code,
        )
        active_hub.set_last_active_project(args.name)
        print(f"Initialized project '{args.name}' at {proj_root} (template: {args.template})")
        return 0

    if args.command == "root":
        if args.root_cmd == "set":
            new_root = active_hub.set_client_root(Path(args.path))
            print(f"Remembered Client Workspace Root: {new_root}")
            return 0
        print(f"Client Workspace Root: {active_hub.get_client_root()}")
        return 0

    if args.command == "project":
        if args.project_cmd in ("create", "new"):
            base_dir = Path(args.path) if args.path else (active_hub.get_client_root() / args.name)
            proj_root = ws.init_project(
                name=args.name,
                root_path=base_dir,
                project_template=args.template,
                code=args.code,
            )
            active_hub.set_last_active_project(args.name)
            print(f"Created project '{args.name}' at {proj_root} (template: {args.template})")
            return 0
        if args.project_cmd == "register":
            entry = active_hub.register_project(Path(args.path))
            active_hub.set_last_active_project(entry.name)
            print(f"Registered project '{entry.name}' ({entry.local_path})")
            return 0
        for p in active_hub.list_projects():
            pres = sync.compute_project_presence(p.name).value
            print(f"- {p.name} [{p.code}] | {pres} | {p.local_path}")
        return 0

    if args.command == "task":
        pname, proot = _resolve_project_root(active_hub, getattr(args, "project", None))
        if args.task_cmd == "create":
            snap = ws.create_component(proot, args.component_path, task_template=args.template)
            print(f"Created component '{snap.component_path}' in project '{pname}' (software: {snap.software})")
            return 0
        for comp in discover_project_components(proot):
            snap = load_component_snapshot(proot, comp)
            w_cnt = len(snap.working_versions)
            r_cnt = len(snap.repo_assets)
            p_cnt = sum(len(d.versions) for d in snap.published_deliverables.values()) + len(snap.published_versions)
            print(f"- {comp} [{snap.software}] | working: {w_cnt}v | repo assets: {r_cnt} | published: {p_cnt}v")
        return 0

    if args.command == "work":
        pname, proot = _resolve_project_root(active_hub, getattr(args, "project", None))
        if args.work_cmd == "up":
            strat = VersionUpStrategy(args.strategy) if args.strategy else None
            bundle = ws.version_up_working(proot, args.component_path, comment=args.comment, strategy=strat)
            print(f"Created {args.component_path}/{bundle.folder_rel} -> {bundle.primary_file}")
            return 0
        if args.work_cmd == "open":
            launched = ws.launch_working_version(proot, args.component_path, version=args.version)
            print(f"Launched {launched}")
            return 0
        if args.work_cmd == "scan":
            if args.collect:
                collected = ws.collect_external_dependencies(proot, args.component_path)
                print(f"Collected {len(collected)} external files into repo/ and relinked working file.")
            else:
                exts = ws.scan_external_dependencies(proot, args.component_path)
                print(f"Found {len(exts)} external media references outside repo/:")
                for d in exts:
                    print(f"  - {d.raw_reference} -> suggested repo/{d.suggested_category}/{d.suggested_asset_name}")
            return 0
        if args.work_cmd == "ingest":
            bundle = ws.ingest_working_files(
                project_root=proot,
                component_path=args.task,
                source_paths=[Path(f) for f in args.files],
                comment=args.comment,
                rename_to_convention=not args.keep_name,
            )
            print(f"Ingested {args.task}/{bundle.folder_rel} -> {bundle.primary_file}")
            return 0

    if args.command == "ingest":
        _, proot = _resolve_project_root(active_hub, args.project)
        custom_meta: dict[str, Any] = {}
        for item in args.meta:
            if "=" in item:
                k, v = item.split("=", 1)
                custom_meta[k.strip()] = v.strip()
        asset, bundle = ws.ingest_repo_asset(
            project_root=proot,
            component_path=args.task,
            source_file=Path(args.file),
            asset_path=args.asset,
            author=args.author,
            source_info=args.source,
            license_info=args.license,
            tags=args.tag,
            comment=args.comment,
            custom=custom_meta,
        )
        print(
            f"Ingested repo/{asset.asset_path}/{bundle.version_label} "
            f"(author='{asset.author}', source='{asset.source}', license='{asset.license}')"
        )
        return 0

    if args.command == "publish":
        _, proot = _resolve_project_root(active_hub, args.project)
        src_files = [Path(f) for f in args.files]
        bundle = ws.publish_version(
            project_root=proot,
            component_path=args.task,
            source_files=src_files,
            source_working_version=args.from_working,
            comment=args.comment,
            deliverable_name=args.name,
            target_version=args.into_version,
            keep_original_names=args.keep_names,
        )
        print(
            f"Published {args.task}/{bundle.folder_rel} "
            f"(bound to working v{bundle.source_working_version:03d})"
        )
        return 0

    if args.command == "credits":
        pname, proot = _resolve_project_root(active_hub, args.project)
        rows = ws.generate_credits_report(proot)
        print(f"=== Attribution & Credits Report for Project '{pname}' ===")
        for r in rows:
            print(
                f"- [{r['component']}] repo/{r['asset_path']} ({r['latest_version']}) | "
                f"Author: {r['author']} | Source: {r['source']} | License: {r['license']}"
            )
        return 0

    if args.command == "template":
        tm = active_hub.template_manager
        if args.tpl_cmd == "capture":
            dest = tm.capture_template(args.tier, Path(args.source), args.name)
            print(f"Captured {args.tier} template '{args.name}' -> {dest}")
            return 0
        print("Tier-1 Project Templates:", ", ".join(tm.list_project_templates().keys()))
        print("Tier-2 Task Templates:   ", ", ".join(tm.list_task_templates().keys()))
        print("Tier-3 Seed Files:       ", ", ".join(tm.list_file_templates().keys()))
        return 0

    if args.command == "software":
        if args.sw_cmd == "set":
            prof = SoftwareProfile(
                key=args.key,
                display_name=args.name or args.key.title(),
                executable=args.exe,
                extensions=args.ext or [".txt"],
                version_up_strategy=VersionUpStrategy(args.strategy),
            )
            active_hub.set_software_profile(prof)
            print(f"Configured software profile '{prof.key}' (strategy: {prof.version_up_strategy.value})")
            return 0
        for k, prof in active_hub.config.software.items():
            print(f"- {k}: {prof.display_name} | exts={prof.extensions} | strategy={prof.version_up_strategy.value}")
        return 0

    if args.command == "vault":
        if args.vault_cmd == "connect":
            st = active_hub.connect_vault(Path(args.path))
            print(f"Server Vault connected: {active_hub.config.vault.path} ({st.value})")
            return 0
        if args.vault_cmd == "disconnect":
            active_hub.disconnect_vault()
            print("Server Vault disconnected (Standalone Local Mode active).")
            return 0
        print(f"Vault status: {active_hub.get_vault_status().value} ({active_hub.config.vault.path or 'None'})")
        return 0

    if args.command == "push":
        res = sync.push_paths(args.project, path_filters=args.filter or ["**"], evict_local=False)
        print(f"Pushed {res['pushed']} file(s) to Server Vault.")
        return 0

    if args.command == "archive":
        res = sync.push_paths(args.project, path_filters=args.filter or ["**"], evict_local=True)
        print(f"Archived '{args.project}': pushed {res['pushed']} file(s), offloaded {res['evicted']} local file(s).")
        return 0

    if args.command == "checkout":
        res = sync.pull_paths(args.project, path_filters=args.filter or ["**"])
        print(f"Checked out {res['pulled']} file(s) from Server Vault.")
        return 0

    if args.command == "delete":
        if args.delete_cmd == "project":
            ok = active_hub.delete_project(args.name, delete_files=args.delete_files)
            if not ok:
                raise SystemExit(f"Error: Project '{args.name}' not found.")
            print(f"Deleted project '{args.name}' (delete_files={args.delete_files})")
            return 0
        _, proot = _resolve_project_root(active_hub, getattr(args, "project", None))
        if args.delete_cmd == "component":
            ok = ws.delete_component(proot, args.component_path, delete_files=not args.keep_files)
            if not ok:
                raise SystemExit(f"Error: Component '{args.component_path}' not found.")
            print(f"Deleted component '{args.component_path}'")
            return 0
        if args.delete_cmd == "working":
            ok = ws.delete_working_version(proot, args.component_path, args.version)
            if not ok:
                raise SystemExit(f"Error: Working v{args.version:03d} not found in '{args.component_path}'.")
            print(f"Deleted {args.component_path}/working/v{args.version:03d}")
            return 0
        if args.delete_cmd == "repo":
            if args.version is not None:
                ok = ws.delete_repo_version(proot, args.task, args.asset_path, args.version)
                msg = f"Deleted {args.task}/repo/{args.asset_path}/v{args.version:03d}"
            else:
                ok = ws.delete_repo_asset(proot, args.task, args.asset_path)
                msg = f"Deleted {args.task}/repo/{args.asset_path}"
            if not ok:
                raise SystemExit("Error: Target repo asset/version not found.")
            print(msg)
            return 0
        if args.delete_cmd == "publish":
            if args.version is not None:
                ok = ws.delete_published_version(proot, args.task, args.deliverable_name, args.version)
                msg = f"Deleted {args.task}/published/{args.deliverable_name}/v{args.version:03d}"
            else:
                ok = ws.delete_published_deliverable(proot, args.task, args.deliverable_name)
                msg = f"Deleted {args.task}/published/{args.deliverable_name}"
            if not ok:
                raise SystemExit("Error: Target published deliverable/version not found.")
            print(msg)
            return 0

    if args.command == "reaper":
        from obol.plugins.reaper_installer import ReaperInstaller

        installer = ReaperInstaller(active_hub)
        res_dir = Path(args.resource_dir) if getattr(args, "resource_dir", None) else None

        if args.reaper_cmd == "status" or not args.reaper_cmd:
            st = installer.status(resource_dir=res_dir)
            if getattr(args, "json", False):
                print(json.dumps(st))
            else:
                print(f"REAPER Resource Dir: {st['resource_dir'] or 'Not found'} (exists={st['resource_dir_exists']})")
                print(f"REAPER Executable:   {st['executable'] or 'Not configured'} (exists={st['executable_exists']})")
                print(f"Scripts Installed:   {len(st['scripts_installed'])} ({'OK' if st['all_scripts_installed'] else 'Incomplete'})")
                print(f"Actions Registered:  {len(st['actions_registered'])}")
                print(f"Startup Auto-Bridge: {st['startup_hook_installed']}")
                print(f"Bridge Connected:    {st['bridge_connected']} ({st['reaper_version'] or 'offline'})")
            return 0

        if args.reaper_cmd == "install":
            st = installer.install(
                resource_dir=res_dir,
                auto_start_bridge=not args.no_autostart,
                executable_override=args.exe,
            )
            if args.json:
                print(json.dumps({"ok": True, "status": st}))
            else:
                print(f"Installed Obol REAPER integration into {st['resource_dir']}")
                print(f"Actions registered: {len(st['actions_registered'])} | Startup bridge: {st['startup_hook_installed']}")
                if st["toolbar_menu_path"]:
                    print(f"Toolbar MenuSet exported: {st['toolbar_menu_path']}")
            return 0

        if args.reaper_cmd == "uninstall":
            st = installer.uninstall(resource_dir=res_dir)
            if args.json:
                print(json.dumps({"ok": True, "status": st}))
            else:
                print(f"Uninstalled Obol REAPER integration from {st.get('resource_dir') or 'REAPER'}")
            return 0

        if args.reaper_cmd == "ui":
            from obol.gui.reaper_dialogs import launch_reaper_dialog

            return launch_reaper_dialog(
                hub=active_hub,
                mode=args.mode,
                active_rpp=args.rpp,
                project=args.project,
                component=args.component,
            )

        rpp_path = Path(args.rpp)
        ctx = ws.resolve_working_file_context(rpp_path)

        if args.reaper_cmd == "context":
            if ctx is None:
                out = {"found": False, "rpp": str(rpp_path)}
            else:
                out = {
                    "found": True,
                    "project_name": ctx.project_name,
                    "project_root": str(ctx.project_root),
                    "component_path": ctx.component_path,
                    "working_version": ctx.working_version,
                    "working_label": f"v{ctx.working_version:03d}",
                    "deliverables": sorted(ctx.snapshot.published_deliverables.keys()),
                }
            if args.json:
                print(json.dumps(out))
            elif ctx is None:
                print(f"Not an Obol working file: {rpp_path}")
            else:
                print(f"{ctx.project_name} / {ctx.component_path} / working/v{ctx.working_version:03d}")
            return 0

        if ctx is None:
            err = {"ok": False, "error": f"File is not inside an Obol working/vNNN/ folder: {rpp_path}"}
            if getattr(args, "json", False):
                print(json.dumps(err))
                return 1
            raise SystemExit(f"Error: {err['error']}")

        if args.reaper_cmd == "version-up":
            bundle = ws.version_up_working(
                project_root=ctx.project_root,
                component_path=ctx.component_path,
                comment=args.comment,
                source_version=ctx.working_version,
            )
            primary_abs = (ctx.project_root / ctx.component_path / bundle.primary_file).resolve()
            res = {
                "ok": True,
                "version": bundle.version,
                "version_label": bundle.version_label,
                "primary_file": str(primary_abs),
                "component_path": ctx.component_path,
            }
            if args.json:
                print(json.dumps(res))
            else:
                print(f"Created {ctx.component_path}/{bundle.folder_rel} -> {primary_abs}")
            return 0

        if args.reaper_cmd == "collect":
            collected = ws.collect_external_dependencies(
                project_root=ctx.project_root,
                component_path=ctx.component_path,
                working_version=ctx.working_version,
            )
            res = {
                "ok": True,
                "collected_count": len(collected),
                "assets": [a.asset_path for a in collected],
            }
            if args.json:
                print(json.dumps(res))
            else:
                print(f"Collected {len(collected)} external media file(s) into repo/.")
            return 0

        if args.reaper_cmd == "prepare-publish":
            deliv_path, ver_num, export_dir = ws.prepare_publish_export_folder(
                project_root=ctx.project_root,
                component_path=ctx.component_path,
                deliverable_name=args.name,
                target_version=args.into_version,
            )
            res = {
                "ok": True,
                "deliverable_path": deliv_path,
                "version": ver_num,
                "version_label": f"v{ver_num:03d}",
                "export_dir": str(export_dir.resolve()),
                "source_working_version": ctx.working_version,
            }
            if args.json:
                print(json.dumps(res))
            else:
                print(f"Prepared export folder: {export_dir}")
            return 0

        if args.reaper_cmd == "sync-publish":
            deliv_path = ws.normalize_deliverable_name(args.name)
            ver_dir = ctx.project_root / ctx.component_path / "published" / deliv_path / f"v{args.version:03d}"
            bundle = ws.sync_published_folder(
                project_root=ctx.project_root,
                component_path=ctx.component_path,
                deliverable_name=deliv_path,
                version=args.version,
                source_working_version=ctx.working_version,
                comment=args.comment,
            )
            if bundle is None:
                if ver_dir.is_dir() and not any(ver_dir.iterdir()):
                    ver_dir.rmdir()
                res = {"ok": False, "empty": True, "error": "No rendered files found in export folder."}
                if args.json:
                    print(json.dumps(res))
                    return 1
                raise SystemExit("Error: No rendered files found in export folder.")
            if args.tag:
                ws.update_published_metadata(
                    project_root=ctx.project_root,
                    component_path=ctx.component_path,
                    deliverable_name=deliv_path,
                    version=bundle.version,
                    tags=list(dict.fromkeys(list(bundle.tags) + list(args.tag))),
                )
            res = {
                "ok": True,
                "deliverable_path": deliv_path,
                "version": bundle.version,
                "version_label": bundle.version_label,
                "files": [f.rel_path for f in bundle.files],
                "source_working_version": bundle.source_working_version,
            }
            if args.json:
                print(json.dumps(res))
            else:
                print(f"Synced published/{deliv_path}/{bundle.version_label} ({len(bundle.files)} file(s))")
            return 0

    if args.command == "resolve":
        from obol.plugins.resolve_bridge import ResolveBridgeClient
        from obol.plugins.resolve_installer import ResolveInstaller

        installer = ResolveInstaller(active_hub)
        sdir = Path(args.scripts_dir) if getattr(args, "scripts_dir", None) else None
        allow_studio = not getattr(args, "no_studio_api", False)

        if args.resolve_cmd == "status" or not args.resolve_cmd:
            st = installer.status(scripts_dir=sdir, allow_studio_api=allow_studio)
            if getattr(args, "json", False):
                print(json.dumps(st))
            else:
                print(f"Resolve Scripts Dir: {st['scripts_dir'] or 'Not found'} (exists={st['scripts_dir_exists']})")
                print(f"Resolve Executable:  {st['executable'] or 'Not configured'} (exists={st['executable_exists']})")
                print(f"Utility Scripts:     {len(st['scripts_installed'])}/5 | Deliver: {st['deliver_script_installed']}")
                print(f"Bridge Status:       {st['bridge_mode']} (connected={st['bridge_connected']})")
            return 0

        if args.resolve_cmd == "install":
            st = installer.install(
                scripts_dir=sdir,
                executable_override=args.exe,
                allow_studio_api=allow_studio,
            )
            if args.json:
                print(json.dumps({"ok": True, "status": st}))
            else:
                print(f"Installed Obol DaVinci Resolve scripts into {st['scripts_dir']}")
            return 0

        if args.resolve_cmd == "uninstall":
            st = installer.uninstall(scripts_dir=sdir, allow_studio_api=allow_studio)
            if args.json:
                print(json.dumps({"ok": True, "status": st}))
            else:
                print(f"Uninstalled Obol scripts from {st.get('scripts_dir') or 'DaVinci Resolve'}")
            return 0

        if args.resolve_cmd == "bind":
            bridge = ResolveBridgeClient(active_hub, allow_studio_api=False)
            if args.project and args.task:
                bridge.set_project_binding(args.resolve_project, args.project, args.task)
            binding = bridge.get_project_binding(args.resolve_project)
            out = {"resolve_project": args.resolve_project, "binding": binding}
            if args.json:
                print(json.dumps(out))
            else:
                print(f"{args.resolve_project} -> {binding}")
            return 0

        if args.resolve_cmd == "ui":
            from obol.gui.resolve_dialogs import launch_resolve_dialog

            return launch_resolve_dialog(
                hub=active_hub,
                mode=args.mode,
                context_file=args.context_file,
                result_file=args.result_file,
                project=args.project,
                component=args.component,
            )

        _, proot = _resolve_project_root(active_hub, getattr(args, "project", None))
        if args.resolve_cmd == "prepare-publish":
            deliv_path, ver_num, export_dir = ws.prepare_publish_export_folder(
                project_root=proot,
                component_path=args.task,
                deliverable_name=args.name,
                target_version=args.into_version,
            )
            res = {
                "ok": True,
                "deliverable_path": deliv_path,
                "version": ver_num,
                "version_label": f"v{ver_num:03d}",
                "export_dir": str(export_dir.resolve()),
            }
            if args.json:
                print(json.dumps(res))
            else:
                print(f"Prepared export folder: {export_dir}")
            return 0

        if args.resolve_cmd == "sync-publish":
            deliv_path = ws.normalize_deliverable_name(args.name)
            ver_dir = proot / args.task / "published" / deliv_path / f"v{args.version:03d}"
            bundle = ws.sync_published_folder(
                project_root=proot,
                component_path=args.task,
                deliverable_name=deliv_path,
                version=args.version,
                source_working_version=args.from_working,
                comment=args.comment,
            )
            if bundle is None:
                if ver_dir.is_dir() and not any(ver_dir.iterdir()):
                    ver_dir.rmdir()
                res = {"ok": False, "empty": True, "error": "No rendered files found in export folder."}
                if args.json:
                    print(json.dumps(res))
                    return 1
                raise SystemExit("Error: No rendered files found in export folder.")
            if args.tag:
                ws.update_published_metadata(
                    project_root=proot,
                    component_path=args.task,
                    deliverable_name=deliv_path,
                    version=bundle.version,
                    tags=list(dict.fromkeys(list(bundle.tags) + list(args.tag))),
                )
            res = {
                "ok": True,
                "deliverable_path": deliv_path,
                "version": bundle.version,
                "version_label": bundle.version_label,
                "files": [f.rel_path for f in bundle.files],
                "source_working_version": bundle.source_working_version,
            }
            if args.json:
                print(json.dumps(res))
            else:
                print(f"Synced published/{deliv_path}/{bundle.version_label} ({len(bundle.files)} file(s))")
            return 0

    if args.command == "integrations":
        from obol.plugins.integrations import DCCIntegrationManager

        allow_studio = not getattr(args, "no_studio_api", False)
        mgr = DCCIntegrationManager(active_hub, allow_resolve_studio_api=allow_studio)

        if args.int_cmd == "status" or not args.int_cmd:
            reps = mgr.list_reports()
            if getattr(args, "json", False):
                print(json.dumps({"integrations": [r.to_dict() for r in reps]}))
            else:
                for r in reps:
                    print(f"- {r.display_name} [{r.key}]: {r.overall_state} | dir={r.target_dir or 'N/A'}")
            return 0

        if args.int_cmd == "install":
            tdir = Path(args.target_dir) if args.target_dir else None
            if args.all or not args.dcc:
                reps = mgr.install_all_detected()
                if args.json:
                    print(json.dumps({"ok": True, "integrations": [r.to_dict() for r in reps]}))
                else:
                    for r in reps:
                        print(f"- {r.display_name}: {r.overall_state}")
                return 0
            rep = mgr.install(
                args.dcc,
                target_dir=tdir,
                executable=args.exe,
                options={"auto_start_bridge": not args.no_autostart},
            )
            if args.json:
                print(json.dumps({"ok": True, "integration": rep.to_dict()}))
            else:
                print(f"Installed {rep.display_name}: {rep.overall_state} ({rep.target_dir})")
            return 0

        if args.int_cmd == "uninstall":
            tdir = Path(args.target_dir) if args.target_dir else None
            rep = mgr.uninstall(args.dcc, target_dir=tdir)
            if args.json:
                print(json.dumps({"ok": True, "integration": rep.to_dict()}))
            else:
                print(f"Uninstalled {rep.display_name}: {rep.overall_state}")
            return 0

        if args.int_cmd == "ui":
            from obol.gui.integrations_dialog import launch_integrations_manager

            return launch_integrations_manager(active_hub, initial_dcc=args.dcc)

    if args.command == "gui":
        from obol.gui.app import launch_gui

        return launch_gui(
            active_hub,
            initial_project=getattr(args, "project", ""),
            initial_component=getattr(args, "component", ""),
        )

    return 0


def main() -> None:
    sys.exit(run_cli())


if __name__ == "__main__":
    main()

