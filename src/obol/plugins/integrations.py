"""Unified DCC Integration Manager, health/drift diagnostics, and lifecycle actions."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from obol.core.hub import ObolHub
from obol.plugins.reaper_bridge import (
    ReaperBridgeClient,
    detect_reaper_executable,
    detect_reaper_resource_dir,
)
from obol.plugins.reaper_installer import (
    ReaperInstaller,
    _resolve_python_executables,
    _resolve_pythonpath,
)
from obol.plugins.reaper_scripts import OBOL_ACTION_SPECS
from obol.plugins.reaper_scripts import OBOL_INTEGRATION_VERSION as REAPER_INTEGRATION_VERSION
from obol.plugins.resolve_bridge import (
    ResolveBridgeClient,
    detect_resolve_executable,
    detect_resolve_scripts_dir,
)
from obol.plugins.resolve_installer import ResolveInstaller
from obol.plugins.resolve_scripts import RESOLVE_UTILITY_SCRIPTS
from obol.plugins.resolve_scripts import OBOL_INTEGRATION_VERSION as RESOLVE_INTEGRATION_VERSION

_LUA_CONFIG_RE = re.compile(r"^\s*([A-Z0-9_]+)\s*=\s*\[(=*)\[(.*?)\]\2\]\s*,?\s*$")


def _norm_path_for_compare(raw: str) -> str:
    if not raw:
        return ""
    s = raw.replace("\\", "/").rstrip("/")
    return os.path.normcase(s) if sys.platform == "win32" else s


def inspect_lua_config_sync(
    config_path: Path,
    expected_version: str,
    expected_python: str,
    expected_pythonpath: str,
    expected_hub: str,
) -> tuple[bool, str]:
    """Checks whether a generated Lua config file matches the current Obol runtime and version."""
    if not config_path.is_file():
        return False, "Runtime config file missing"
    parsed: dict[str, str] = {}
    for line in config_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = _LUA_CONFIG_RE.match(line)
        if m:
            parsed[m.group(1)] = m.group(3)

    ver = parsed.get("INTEGRATION_VERSION", "")
    if ver != expected_version:
        return False, f"Version mismatch ({ver or 'legacy'} != {expected_version})"
    if _norm_path_for_compare(parsed.get("PYTHON_EXE", "")) != _norm_path_for_compare(expected_python):
        return False, "Python executable path changed"
    if _norm_path_for_compare(parsed.get("PYTHONPATH", "")) != _norm_path_for_compare(expected_pythonpath):
        return False, "Obol source path (PYTHONPATH) changed"
    if _norm_path_for_compare(parsed.get("OBOL_HOME", "")) != _norm_path_for_compare(expected_hub):
        return False, "OBOL_HOME hub directory changed"
    return True, f"Up to date (v{expected_version})"


@dataclass
class DCCHealthCheck:
    label: str
    state: str  # "ok" | "warn" | "missing" | "info"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"label": self.label, "state": self.state, "detail": self.detail}


@dataclass
class DCCIntegrationReport:
    key: str
    display_name: str
    tagline: str
    overall_state: str  # "connected" | "installed" | "needs_update" | "not_installed" | "not_detected"
    detected: bool
    installed: bool
    needs_update: bool
    bridge_connected: bool
    bridge_mode: str
    bridge_detail: str
    target_dir: str
    target_dir_label: str
    executable: str
    executable_exists: bool
    checks: list[DCCHealthCheck] = field(default_factory=list)
    installed_files: list[str] = field(default_factory=list)
    quick_links: list[tuple[str, str]] = field(default_factory=list)
    usage_steps: list[str] = field(default_factory=list)
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "display_name": self.display_name,
            "tagline": self.tagline,
            "overall_state": self.overall_state,
            "detected": self.detected,
            "installed": self.installed,
            "needs_update": self.needs_update,
            "bridge_connected": self.bridge_connected,
            "bridge_mode": self.bridge_mode,
            "bridge_detail": self.bridge_detail,
            "target_dir": self.target_dir,
            "target_dir_label": self.target_dir_label,
            "executable": self.executable,
            "executable_exists": self.executable_exists,
            "checks": [c.to_dict() for c in self.checks],
            "installed_files": self.installed_files,
            "quick_links": [{"label": lbl, "path": p} for lbl, p in self.quick_links],
            "usage_steps": self.usage_steps,
            "extra": self.extra,
        }


class DCCIntegrationManager:
    """Unified manager for inspecting, installing, updating, testing, and removing DCC integrations."""

    SUPPORTED_DCCS = ("reaper", "resolve")

    def __init__(self, hub: ObolHub, allow_resolve_studio_api: bool = True) -> None:
        self.hub = hub
        self.allow_resolve_studio_api = allow_resolve_studio_api
        self.reaper_installer = ReaperInstaller(hub)
        self.resolve_installer = ResolveInstaller(hub)

    def get_report(self, key: str, target_dir: Optional[Path] = None) -> DCCIntegrationReport:
        if key == "reaper":
            return self._build_reaper_report(target_dir=target_dir)
        if key == "resolve":
            return self._build_resolve_report(target_dir=target_dir)
        raise KeyError(f"Unsupported DCC integration key: '{key}'")

    def list_reports(
        self,
        target_dir_overrides: Optional[dict[str, Path]] = None,
    ) -> list[DCCIntegrationReport]:
        overrides = target_dir_overrides or {}
        return [self.get_report(k, target_dir=overrides.get(k)) for k in self.SUPPORTED_DCCS]

    def _build_reaper_report(self, target_dir: Optional[Path] = None) -> DCCIntegrationReport:
        st = self.reaper_installer.status(resource_dir=target_dir)
        res_dir = Path(st["resource_dir"]) if st["resource_dir"] else None
        detected = bool(st["resource_dir_exists"] or st["executable_exists"])
        all_scripts = bool(st["all_scripts_installed"])
        all_actions = bool(st["all_actions_registered"])
        partial_install = bool(st["scripts_installed"]) or bool(st["actions_registered"])
        installed = all_scripts and all_actions

        py_exe, _ = _resolve_python_executables()
        py_path = _resolve_pythonpath()
        cfg_ok = False
        cfg_detail = "Not installed"
        if res_dir and (res_dir / "Scripts" / "Obol" / "obol_config.lua").is_file():
            cfg_ok, cfg_detail = inspect_lua_config_sync(
                res_dir / "Scripts" / "Obol" / "obol_config.lua",
                expected_version=REAPER_INTEGRATION_VERSION,
                expected_python=py_exe,
                expected_pythonpath=py_path,
                expected_hub=str(self.hub.hub_dir),
            )

        needs_update = (partial_install and not installed) or (installed and not cfg_ok)
        bridge_connected = bool(st["bridge_connected"])
        bridge_detail = (
            f"REAPER {st['reaper_version']} ({st['active_rpp'] or 'no project open'})"
            if bridge_connected
            else "Bridge offline (start REAPER or run 'Obol - Toggle Bridge')"
        )

        if bridge_connected:
            overall = "connected"
        elif installed and needs_update:
            overall = "needs_update"
        elif installed:
            overall = "installed"
        elif partial_install:
            overall = "needs_update"
        elif detected:
            overall = "not_installed"
        else:
            overall = "not_detected"

        checks = [
            DCCHealthCheck(
                "REAPER Executable",
                "ok" if st["executable_exists"] else "warn",
                st["executable"] or "Not found (OS default file association used)",
            ),
            DCCHealthCheck(
                "REAPER Resource Folder",
                "ok" if st["resource_dir_exists"] else "missing",
                st["resource_dir"] or "Not detected",
            ),
            DCCHealthCheck(
                "Companion Lua Scripts",
                "ok" if all_scripts else ("warn" if st["scripts_installed"] else "missing"),
                f"{len(st['scripts_installed'])} / {len(OBOL_ACTION_SPECS)} installed",
            ),
            DCCHealthCheck(
                "Action List (reaper-kb.ini)",
                "ok" if all_actions else ("warn" if st["actions_registered"] else "missing"),
                f"{len(st['actions_registered'])} / {len(OBOL_ACTION_SPECS)} registered",
            ),
            DCCHealthCheck(
                "Startup Auto-Bridge",
                "ok" if st["startup_hook_installed"] else "info",
                "Enabled in Scripts/__startup.lua" if st["startup_hook_installed"] else "Manual toggle only",
            ),
            DCCHealthCheck(
                "Runtime Config Sync",
                "ok" if cfg_ok else ("warn" if partial_install else "missing"),
                cfg_detail,
            ),
            DCCHealthCheck(
                "Live Bridge Heartbeat",
                "ok" if bridge_connected else "info",
                bridge_detail,
            ),
        ]

        installed_files: list[str] = []
        quick_links: list[tuple[str, str]] = []
        if res_dir and res_dir.is_dir():
            scripts_obol = res_dir / "Scripts" / "Obol"
            if scripts_obol.is_dir():
                quick_links.append(("Open Scripts/Obol", str(scripts_obol)))
                installed_files = sorted(str(p) for p in scripts_obol.iterdir() if p.is_file())
            menusets = res_dir / "MenuSets"
            if menusets.is_dir():
                quick_links.append(("Open MenuSets", str(menusets)))
            if st["toolbar_menu_path"]:
                installed_files.append(st["toolbar_menu_path"])

        usage_steps = [
            "Open REAPER (the Obol Bridge starts automatically if Startup Auto-Bridge is enabled).",
            "Press <b>?</b> in REAPER to open the Action List and search for <b>Obol</b> (Open, Save and Version Up, Import Asset, Export and Publish, Collect External Media).",
            "<i>Optional toolbar</i>: In REAPER open <b>Options → Customize menus/toolbars → Import…</b> and select <code>MenuSets/Obol_Toolbar.ReaperMenu</code>.",
            "Drag audio/MIDI/.rpp files straight from Obol's GUI into REAPER, or click <b>Export from REAPER...</b> on the Published tab.",
        ]

        return DCCIntegrationReport(
            key="reaper",
            display_name="REAPER DAW",
            tagline="Bidirectional .rpp open/version-up, media import & take updater, and hybrid direct/native render publishing.",
            overall_state=overall,
            detected=detected,
            installed=installed or partial_install,
            needs_update=needs_update,
            bridge_connected=bridge_connected,
            bridge_mode=("lua_bridge" if bridge_connected else "offline"),
            bridge_detail=bridge_detail,
            target_dir=st["resource_dir"],
            target_dir_label="REAPER Resource Folder:",
            executable=st["executable"],
            executable_exists=bool(st["executable_exists"]),
            checks=checks,
            installed_files=installed_files,
            quick_links=quick_links,
            usage_steps=usage_steps,
            extra={
                "startup_hook_installed": bool(st["startup_hook_installed"]),
                "toolbar_menu_path": st["toolbar_menu_path"],
                "active_rpp": st["active_rpp"],
            },
        )

    def _build_resolve_report(self, target_dir: Optional[Path] = None) -> DCCIntegrationReport:
        st = self.resolve_installer.status(
            scripts_dir=target_dir,
            allow_studio_api=self.allow_resolve_studio_api,
        )
        sdir = Path(st["scripts_dir"]) if st["scripts_dir"] else None
        detected = bool(st["scripts_dir_exists"] or st["executable_exists"])
        all_scripts = bool(st["all_scripts_installed"])
        partial_install = bool(st["scripts_installed"]) or bool(st["deliver_script_installed"])
        installed = all_scripts

        py_exe, _ = _resolve_python_executables()
        py_path = _resolve_pythonpath()
        cfg_path = (sdir / "Utility" / "Obol" / "obol_resolve_config.lua") if sdir else None
        cfg_ok = False
        cfg_detail = "Not installed"
        if cfg_path and cfg_path.is_file():
            cfg_ok, cfg_detail = inspect_lua_config_sync(
                cfg_path,
                expected_version=RESOLVE_INTEGRATION_VERSION,
                expected_python=py_exe,
                expected_pythonpath=py_path,
                expected_hub=str(self.hub.hub_dir),
            )

        needs_update = (partial_install and not installed) or (installed and not cfg_ok)
        bridge_connected = bool(st["bridge_connected"])
        bridge_mode = str(st["bridge_mode"])
        if bridge_connected:
            mode_lbl = "Studio Python API" if bridge_mode == "studio_api" else "Lua Bridge"
            bridge_detail = f"{mode_lbl} ({st['active_project'] or 'no project open'})"
        else:
            bridge_detail = "Offline (Menu scripts inside Resolve work anytime on Free & Studio)"

        if bridge_connected:
            overall = "connected"
        elif installed and needs_update:
            overall = "needs_update"
        elif installed:
            overall = "installed"
        elif partial_install:
            overall = "needs_update"
        elif detected:
            overall = "not_installed"
        else:
            overall = "not_detected"

        checks = [
            DCCHealthCheck(
                "DaVinci Resolve Executable",
                "ok" if st["executable_exists"] else "warn",
                st["executable"] or "Not found",
            ),
            DCCHealthCheck(
                "Fusion Scripts Folder",
                "ok" if st["scripts_dir_exists"] else "missing",
                st["scripts_dir"] or "Not detected",
            ),
            DCCHealthCheck(
                "Utility Menu Scripts",
                "ok" if len(st["scripts_installed"]) == len(RESOLVE_UTILITY_SCRIPTS) else (
                    "warn" if st["scripts_installed"] else "missing"
                ),
                f"{len(st['scripts_installed'])} / {len(RESOLVE_UTILITY_SCRIPTS)} installed",
            ),
            DCCHealthCheck(
                "Deliver Menu Script",
                "ok" if st["deliver_script_installed"] else "missing",
                "Deliver/Obol - Export and Publish.lua" if st["deliver_script_installed"] else "Missing",
            ),
            DCCHealthCheck(
                "Runtime Config Sync",
                "ok" if cfg_ok else ("warn" if partial_install else "missing"),
                cfg_detail,
            ),
            DCCHealthCheck(
                "Live Bridge / Studio API",
                "ok" if bridge_connected else "info",
                bridge_detail,
            ),
        ]

        installed_files: list[str] = []
        quick_links: list[tuple[str, str]] = []
        if sdir and sdir.is_dir():
            util_dir = sdir / "Utility"
            deliv_dir = sdir / "Deliver"
            if util_dir.is_dir():
                quick_links.append(("Open Utility Scripts", str(util_dir)))
                for fname in RESOLVE_UTILITY_SCRIPTS:
                    p = util_dir / fname
                    if p.is_file():
                        installed_files.append(str(p))
                lib_dir = util_dir / "Obol"
                if lib_dir.is_dir():
                    installed_files.extend(sorted(str(p) for p in lib_dir.iterdir() if p.is_file()))
            if deliv_dir.is_dir():
                quick_links.append(("Open Deliver Scripts", str(deliv_dir)))
                dp = deliv_dir / "Obol - Export and Publish.lua"
                if dp.is_file():
                    installed_files.append(str(dp))

        bindings = ResolveBridgeClient(self.hub, allow_studio_api=False).list_project_bindings()
        usage_steps = [
            "Open DaVinci Resolve (works on both <b>Resolve Free</b> and <b>Resolve Studio</b>).",
            "To load <code>repo/</code> or <code>published/</code> media (or update existing Media Pool clips): <b>Workspace → Scripts → Utility → Obol - Load Media into Media Pool</b>.",
            "To render & publish from Resolve: <b>Workspace → Scripts → Utility</b> (or <b>Deliver</b>) <b>→ Obol - Export and Publish</b>.",
            "<i>Optional for pushing from Obol GUI into Resolve Free</i>: Run <b>Obol - Live Bridge</b> from Utility scripts (stop via <b>Obol - Stop Bridge</b>).",
        ]

        return DCCIntegrationReport(
            key="resolve",
            display_name="DaVinci Resolve",
            tagline="Load repo/ & published/ media into Media Pool, update clip versions, and publish renders, Quick Exports & stills.",
            overall_state=overall,
            detected=detected,
            installed=installed or partial_install,
            needs_update=needs_update,
            bridge_connected=bridge_connected,
            bridge_mode=bridge_mode,
            bridge_detail=bridge_detail,
            target_dir=st["scripts_dir"],
            target_dir_label="Resolve Fusion Scripts Folder:",
            executable=st["executable"],
            executable_exists=bool(st["executable_exists"]),
            checks=checks,
            installed_files=installed_files,
            quick_links=quick_links,
            usage_steps=usage_steps,
            extra={
                "bindings": bindings,
                "active_project": st["active_project"],
                "active_timeline": st["active_timeline"],
                "studio_api_available": bool(st["studio_api_available"]),
            },
        )

    def install(
        self,
        key: str,
        target_dir: Optional[Path] = None,
        executable: str = "",
        options: Optional[dict[str, Any]] = None,
    ) -> DCCIntegrationReport:
        opts = dict(options or {})
        if key == "reaper":
            self.reaper_installer.install(
                resource_dir=target_dir,
                auto_start_bridge=bool(opts.get("auto_start_bridge", True)),
                configure_executable=bool(opts.get("configure_executable", True)),
                executable_override=executable,
            )
            return self.get_report("reaper", target_dir=target_dir)
        if key == "resolve":
            self.resolve_installer.install(
                scripts_dir=target_dir,
                configure_executable=bool(opts.get("configure_executable", True)),
                executable_override=executable,
                allow_studio_api=self.allow_resolve_studio_api,
            )
            return self.get_report("resolve", target_dir=target_dir)
        raise KeyError(f"Unsupported DCC integration key: '{key}'")

    def uninstall(self, key: str, target_dir: Optional[Path] = None) -> DCCIntegrationReport:
        if key == "reaper":
            self.reaper_installer.uninstall(resource_dir=target_dir)
            return self.get_report("reaper", target_dir=target_dir)
        if key == "resolve":
            self.resolve_installer.uninstall(scripts_dir=target_dir, allow_studio_api=self.allow_resolve_studio_api)
            return self.get_report("resolve", target_dir=target_dir)
        raise KeyError(f"Unsupported DCC integration key: '{key}'")

    def install_all_detected(
        self,
        target_dir_overrides: Optional[dict[str, Path]] = None,
    ) -> list[DCCIntegrationReport]:
        overrides = target_dir_overrides or {}
        results: list[DCCIntegrationReport] = []
        for rep in self.list_reports(target_dir_overrides=overrides):
            if rep.detected:
                results.append(self.install(rep.key, target_dir=overrides.get(rep.key)))
            else:
                results.append(rep)
        return results

    def test_connection(self, key: str, timeout_s: float = 2.0) -> tuple[bool, str]:
        if key == "reaper":
            client = ReaperBridgeClient(self.hub)
            st = client.get_state()
            if not st.connected:
                return False, "REAPER bridge heartbeat not found. Start REAPER or run 'Custom: Obol - Toggle Bridge'."
            try:
                res = client.send_command("ping", {}, timeout_s=timeout_s)
                ver = res.get("reaper_version") or st.reaper_version or "REAPER"
                rpp = res.get("active_rpp") or st.active_rpp or "no project open"
                return True, f"Ping OK — REAPER {ver} ({rpp})"
            except Exception as exc:
                return False, f"Bridge heartbeat seen, but ping failed: {exc}"

        if key == "resolve":
            client = ResolveBridgeClient(self.hub, allow_studio_api=self.allow_resolve_studio_api)
            st = client.get_state()
            if not st.connected:
                return (
                    False,
                    "Resolve bridge offline. Inside Resolve, run Workspace → Scripts → Utility → Obol - Live Bridge "
                    "(or use menu scripts directly — they work without a background bridge).",
                )
            mode_lbl = "Studio Python API" if st.mode == "studio_api" else "Lua Bridge"
            return True, f"Connected via {mode_lbl} — {st.product_name or 'DaVinci Resolve'} {st.version} (Project: {st.project_name or 'None'})"

        raise KeyError(f"Unsupported DCC integration key: '{key}'")

    def stop_bridge(self, key: str) -> tuple[bool, str]:
        if key == "resolve":
            ResolveBridgeClient(self.hub, allow_studio_api=False).stop_lua_bridge()
            return True, "Sent stop signal to DaVinci Resolve Lua Bridge."
        if key == "reaper":
            return False, "Toggle REAPER's bridge from inside REAPER via 'Custom: Obol - Toggle Bridge'."
        raise KeyError(f"Unsupported DCC integration key: '{key}'")

    def launch_dcc(self, key: str) -> tuple[bool, str]:
        prof = self.hub.get_software_profile(key)
        exe_str = prof.executable
        if not exe_str:
            detected = self.auto_detect_paths(key).get("executable", "")
            exe_str = detected
        if not exe_str or not Path(exe_str).expanduser().is_file():
            return False, f"Executable for '{key}' not found. Set or Auto-Detect the executable path first."
        exe_path = Path(exe_str).expanduser().resolve()
        subprocess.Popen([str(exe_path)], cwd=str(exe_path.parent))
        return True, f"Launched {exe_path.name}"

    def auto_detect_paths(self, key: str) -> dict[str, str]:
        if key == "reaper":
            rdir = detect_reaper_resource_dir()
            exe = detect_reaper_executable()
            return {
                "target_dir": str(rdir) if rdir else "",
                "executable": str(exe) if exe else "",
            }
        if key == "resolve":
            sdir = detect_resolve_scripts_dir()
            exe = detect_resolve_executable()
            return {
                "target_dir": str(sdir) if sdir else "",
                "executable": str(exe) if exe else "",
            }
        return {"target_dir": "", "executable": ""}
