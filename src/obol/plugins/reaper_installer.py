"""1-Click REAPER ReaScript installer, status inspector, and clean uninstaller."""

from __future__ import annotations

import hashlib
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Optional

from obol.core.hub import ObolHub
from obol.plugins.reaper_bridge import (
    ReaperBridgeClient,
    detect_reaper_executable,
    detect_reaper_resource_dir,
    get_bridge_dir,
)
from obol.plugins.reaper_scripts import (
    OBOL_ACTION_SPECS,
    render_obol_config_lua,
    render_obol_lib_lua,
    render_obol_toolbar_menu,
)

STARTUP_BEGIN_MARKER = "-- BEGIN OBOL BRIDGE"
STARTUP_END_MARKER = "-- END OBOL BRIDGE"


def compute_reaper_cmd_id(script_rel_path: str) -> str:
    """Computes a deterministic REAPER `RS...` command ID matching `reaper-kb.ini` format."""
    digest = hashlib.sha1(f"obol:{script_rel_path}".encode("utf-8")).hexdigest()
    return f"RS{digest}"


def _resolve_python_executables() -> tuple[str, str]:
    py_exe = Path(sys.executable).resolve()
    pyw_exe = py_exe
    if sys.platform == "win32" and py_exe.name.lower() == "python.exe":
        cand = py_exe.with_name("pythonw.exe")
        if cand.is_file():
            pyw_exe = cand
    return str(py_exe), str(pyw_exe)


def _resolve_pythonpath() -> str:
    # When running from a source checkout (e.g., d:\tu_tools\obol\src\obol\...), include src root
    obol_pkg_dir = Path(__file__).resolve().parent.parent
    src_root = obol_pkg_dir.parent
    return str(src_root)


def _strip_startup_block(text: str) -> str:
    pattern = re.compile(
        rf"\r?\n?{re.escape(STARTUP_BEGIN_MARKER)}.*?{re.escape(STARTUP_END_MARKER)}\r?\n?",
        re.DOTALL,
    )
    cleaned = pattern.sub("\n", text)
    return cleaned.strip()


def _is_obol_kb_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped.startswith("SCR "):
        return False
    return '"Obol/' in stripped or " Obol/" in stripped or '"Custom: Obol - ' in stripped


class ReaperInstaller:
    """Installs, inspects, and uninstalls Obol's Lua ReaScripts and bridge in REAPER."""

    def __init__(self, hub: ObolHub) -> None:
        self.hub = hub

    def status(self, resource_dir: Optional[Path] = None) -> dict[str, Any]:
        res_dir = Path(resource_dir).expanduser().resolve() if resource_dir else detect_reaper_resource_dir()
        prof = self.hub.get_software_profile("reaper")
        detected_exe = detect_reaper_executable()
        configured_exe = prof.executable or (str(detected_exe) if detected_exe else "")
        exe_exists = bool(configured_exe and Path(configured_exe).expanduser().is_file())

        scripts_installed: list[str] = []
        actions_registered: list[str] = []
        startup_hook = False
        toolbar_menu_path = ""

        if res_dir and res_dir.is_dir():
            obol_scripts_dir = res_dir / "Scripts" / "Obol"
            for fname, _, _ in OBOL_ACTION_SPECS:
                if (obol_scripts_dir / fname).is_file():
                    scripts_installed.append(fname)

            kb_path = res_dir / "reaper-kb.ini"
            if kb_path.is_file():
                kb_lines = kb_path.read_text(encoding="utf-8", errors="replace").splitlines()
                for line in kb_lines:
                    if _is_obol_kb_line(line):
                        actions_registered.append(line.strip())

            startup_path = res_dir / "Scripts" / "__startup.lua"
            if startup_path.is_file():
                st_text = startup_path.read_text(encoding="utf-8", errors="replace")
                startup_hook = STARTUP_BEGIN_MARKER in st_text and STARTUP_END_MARKER in st_text

            menu_file = res_dir / "MenuSets" / "Obol_Toolbar.ReaperMenu"
            if menu_file.is_file():
                toolbar_menu_path = str(menu_file)

        bridge_state = ReaperBridgeClient(self.hub).get_state()

        return {
            "resource_dir": str(res_dir) if res_dir else "",
            "resource_dir_exists": bool(res_dir and res_dir.is_dir()),
            "executable": configured_exe,
            "executable_exists": exe_exists,
            "scripts_installed": scripts_installed,
            "all_scripts_installed": len(scripts_installed) == len(OBOL_ACTION_SPECS),
            "actions_registered": actions_registered,
            "all_actions_registered": len(actions_registered) >= len(OBOL_ACTION_SPECS),
            "startup_hook_installed": startup_hook,
            "toolbar_menu_path": toolbar_menu_path,
            "bridge_connected": bridge_state.connected,
            "reaper_version": bridge_state.reaper_version,
            "active_rpp": bridge_state.active_rpp,
        }

    def install(
        self,
        resource_dir: Optional[Path] = None,
        auto_start_bridge: bool = True,
        configure_executable: bool = True,
        executable_override: str = "",
    ) -> dict[str, Any]:
        res_dir = Path(resource_dir).expanduser().resolve() if resource_dir else detect_reaper_resource_dir()
        if res_dir is None:
            raise FileNotFoundError(
                "Could not auto-detect REAPER resource directory. Pass --resource-dir or select it manually."
            )
        res_dir.mkdir(parents=True, exist_ok=True)

        scripts_dir = res_dir / "Scripts" / "Obol"
        scripts_dir.mkdir(parents=True, exist_ok=True)

        action_cmd_ids: dict[str, str] = {}
        for fname, _, _ in OBOL_ACTION_SPECS:
            rel = f"Obol/{fname}"
            action_cmd_ids[fname] = compute_reaper_cmd_id(rel)

        py_exe, pyw_exe = _resolve_python_executables()
        py_path = _resolve_pythonpath()
        bridge_dir = str(get_bridge_dir(self.hub))
        obol_home = str(self.hub.hub_dir)

        (scripts_dir / "obol_config.lua").write_text(
            render_obol_config_lua(
                python_exe=py_exe,
                pythonw_exe=pyw_exe,
                pythonpath=py_path,
                obol_home=obol_home,
                bridge_dir=bridge_dir,
                bridge_cmd_id=action_cmd_ids["obol_bridge.lua"],
            ),
            encoding="utf-8",
        )
        (scripts_dir / "obol_lib.lua").write_text(render_obol_lib_lua(), encoding="utf-8")

        for fname, _, builder in OBOL_ACTION_SPECS:
            (scripts_dir / fname).write_text(builder(), encoding="utf-8")

        # Update reaper-kb.ini safely (create first-time backup)
        kb_path = res_dir / "reaper-kb.ini"
        kb_bak = res_dir / "reaper-kb.ini.obol.bak"
        existing_kb_lines: list[str] = []
        if kb_path.is_file():
            if not kb_bak.exists():
                shutil.copy2(kb_path, kb_bak)
            existing_kb_lines = kb_path.read_text(encoding="utf-8", errors="replace").splitlines()

        filtered_kb = [ln for ln in existing_kb_lines if not _is_obol_kb_line(ln)]
        for fname, display_name, _ in OBOL_ACTION_SPECS:
            cmd_id = action_cmd_ids[fname]
            filtered_kb.append(f'SCR 4 0 {cmd_id} "Custom: {display_name}" "Obol/{fname}"')
        kb_path.write_text("\n".join(filtered_kb) + "\n", encoding="utf-8")

        # Manage fenced __startup.lua hook
        startup_path = res_dir / "Scripts" / "__startup.lua"
        startup_path.parent.mkdir(parents=True, exist_ok=True)
        existing_startup = startup_path.read_text(encoding="utf-8", errors="replace") if startup_path.is_file() else ""
        base_startup = _strip_startup_block(existing_startup)

        if auto_start_bridge:
            bridge_cid = action_cmd_ids["obol_bridge.lua"]
            hook_block = "\n".join([
                STARTUP_BEGIN_MARKER,
                f'local _obol_bridge_cmd = reaper.NamedCommandLookup("_{bridge_cid}")',
                "if _obol_bridge_cmd ~= 0 then",
                "  reaper.Main_OnCommand(_obol_bridge_cmd, 0)",
                "end",
                STARTUP_END_MARKER,
            ])
            new_startup = f"{base_startup}\n\n{hook_block}\n" if base_startup else f"{hook_block}\n"
            startup_path.write_text(new_startup, encoding="utf-8")
        elif startup_path.is_file():
            if base_startup:
                startup_path.write_text(base_startup + "\n", encoding="utf-8")
            else:
                startup_path.unlink(missing_ok=True)

        # Export importable toolbar MenuSet (without touching reaper-menu.ini)
        menusets_dir = res_dir / "MenuSets"
        menusets_dir.mkdir(parents=True, exist_ok=True)
        menu_path = menusets_dir / "Obol_Toolbar.ReaperMenu"
        menu_path.write_text(render_obol_toolbar_menu(action_cmd_ids), encoding="utf-8")

        # Optionally populate SoftwareProfile("reaper").executable
        if configure_executable:
            prof = self.hub.get_software_profile("reaper")
            chosen_exe = executable_override.strip()
            if not chosen_exe and not prof.executable:
                detected = detect_reaper_executable()
                if detected:
                    chosen_exe = str(detected)
            if chosen_exe:
                prof.executable = chosen_exe
                self.hub.set_software_profile(prof)

        return self.status(resource_dir=res_dir)

    def uninstall(self, resource_dir: Optional[Path] = None) -> dict[str, Any]:
        res_dir = Path(resource_dir).expanduser().resolve() if resource_dir else detect_reaper_resource_dir()
        if res_dir is None or not res_dir.is_dir():
            return {"uninstalled": False, "reason": "REAPER resource directory not found"}

        scripts_dir = res_dir / "Scripts" / "Obol"
        if scripts_dir.exists():
            shutil.rmtree(scripts_dir, ignore_errors=True)

        kb_path = res_dir / "reaper-kb.ini"
        if kb_path.is_file():
            lines = kb_path.read_text(encoding="utf-8", errors="replace").splitlines()
            kept = [ln for ln in lines if not _is_obol_kb_line(ln)]
            kb_path.write_text(("\n".join(kept) + "\n") if kept else "", encoding="utf-8")

        startup_path = res_dir / "Scripts" / "__startup.lua"
        if startup_path.is_file():
            cleaned = _strip_startup_block(startup_path.read_text(encoding="utf-8", errors="replace"))
            if cleaned:
                startup_path.write_text(cleaned + "\n", encoding="utf-8")
            else:
                startup_path.unlink(missing_ok=True)

        menu_path = res_dir / "MenuSets" / "Obol_Toolbar.ReaperMenu"
        menu_path.unlink(missing_ok=True)

        return self.status(resource_dir=res_dir)
