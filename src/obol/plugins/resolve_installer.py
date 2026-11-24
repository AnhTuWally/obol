"""1-Click DaVinci Resolve script installer, status inspector, and clean uninstaller."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any, Optional

from obol.core.hub import ObolHub
from obol.plugins.reaper_installer import _resolve_python_executables, _resolve_pythonpath
from obol.plugins.resolve_bridge import (
    ResolveBridgeClient,
    _try_connect_studio_api,
    detect_resolve_executable,
    detect_resolve_scripts_dir,
    get_resolve_bridge_dir,
)
from obol.plugins.resolve_scripts import (
    RESOLVE_UTILITY_SCRIPTS,
    render_resolve_config_lua,
    render_resolve_lib_lua,
    render_resolve_publish_action_lua,
)


class ResolveInstaller:
    """Installs, inspects, and uninstalls Obol's DaVinci Resolve Lua menu scripts."""

    def __init__(self, hub: ObolHub) -> None:
        self.hub = hub

    def status(
        self,
        scripts_dir: Optional[Path] = None,
        allow_studio_api: bool = True,
    ) -> dict[str, Any]:
        sdir = Path(scripts_dir).expanduser().resolve() if scripts_dir else detect_resolve_scripts_dir()
        prof = self.hub.get_software_profile("resolve")
        detected_exe = detect_resolve_executable()
        configured_exe = prof.executable or (str(detected_exe) if detected_exe else "")
        exe_exists = bool(configured_exe and Path(configured_exe).expanduser().is_file())

        scripts_installed: list[str] = []
        deliver_installed = False
        if sdir and sdir.is_dir():
            util_dir = sdir / "Utility"
            for fname in RESOLVE_UTILITY_SCRIPTS:
                if (util_dir / fname).is_file():
                    scripts_installed.append(fname)
            deliver_installed = (sdir / "Deliver" / "Obol - Export and Publish.lua").is_file()

        bridge_state = ResolveBridgeClient(self.hub, allow_studio_api=allow_studio_api).get_state()
        studio_available = bool(_try_connect_studio_api() is not None) if allow_studio_api else False

        return {
            "scripts_dir": str(sdir) if sdir else "",
            "scripts_dir_exists": bool(sdir and sdir.is_dir()),
            "executable": configured_exe,
            "executable_exists": exe_exists,
            "studio_api_available": studio_available,
            "scripts_installed": scripts_installed,
            "deliver_script_installed": deliver_installed,
            "all_scripts_installed": len(scripts_installed) == len(RESOLVE_UTILITY_SCRIPTS) and deliver_installed,
            "bridge_connected": bridge_state.connected,
            "bridge_mode": bridge_state.mode,
            "resolve_version": bridge_state.version,
            "active_project": bridge_state.project_name,
            "active_timeline": bridge_state.timeline_name,
        }

    def install(
        self,
        scripts_dir: Optional[Path] = None,
        configure_executable: bool = True,
        executable_override: str = "",
        allow_studio_api: bool = True,
    ) -> dict[str, Any]:
        sdir = Path(scripts_dir).expanduser().resolve() if scripts_dir else detect_resolve_scripts_dir()
        if sdir is None:
            raise FileNotFoundError(
                "Could not auto-detect DaVinci Resolve Fusion/Scripts directory. Pass --scripts-dir or select it manually."
            )

        util_dir = sdir / "Utility"
        obol_lib_dir = util_dir / "Obol"
        deliver_dir = sdir / "Deliver"
        util_dir.mkdir(parents=True, exist_ok=True)
        obol_lib_dir.mkdir(parents=True, exist_ok=True)
        deliver_dir.mkdir(parents=True, exist_ok=True)

        py_exe, pyw_exe = _resolve_python_executables()
        py_path = _resolve_pythonpath()
        bridge_dir = str(get_resolve_bridge_dir(self.hub))
        obol_home = str(self.hub.hub_dir)

        (obol_lib_dir / "obol_resolve_config.lua").write_text(
            render_resolve_config_lua(
                python_exe=py_exe,
                pythonw_exe=pyw_exe,
                pythonpath=py_path,
                obol_home=obol_home,
                bridge_dir=bridge_dir,
            ),
            encoding="utf-8",
        )
        (obol_lib_dir / "obol_resolve_lib.lua").write_text(render_resolve_lib_lua(), encoding="utf-8")

        for fname, builder in RESOLVE_UTILITY_SCRIPTS.items():
            (util_dir / fname).write_text(builder(), encoding="utf-8")

        (deliver_dir / "Obol - Export and Publish.lua").write_text(
            render_resolve_publish_action_lua(lib_rel_path="../Utility/Obol/obol_resolve_lib.lua"),
            encoding="utf-8",
        )

        if configure_executable:
            prof = self.hub.get_software_profile("resolve")
            chosen_exe = executable_override.strip()
            if not chosen_exe and not prof.executable:
                detected = detect_resolve_executable()
                if detected:
                    chosen_exe = str(detected)
            if chosen_exe:
                prof.executable = chosen_exe
                self.hub.set_software_profile(prof)

        return self.status(scripts_dir=sdir, allow_studio_api=allow_studio_api)

    def uninstall(
        self,
        scripts_dir: Optional[Path] = None,
        allow_studio_api: bool = True,
    ) -> dict[str, Any]:
        sdir = Path(scripts_dir).expanduser().resolve() if scripts_dir else detect_resolve_scripts_dir()
        if sdir is None or not sdir.is_dir():
            return {"uninstalled": False, "reason": "Resolve Scripts directory not found"}

        obol_lib_dir = sdir / "Utility" / "Obol"
        if obol_lib_dir.exists():
            shutil.rmtree(obol_lib_dir, ignore_errors=True)

        util_dir = sdir / "Utility"
        for fname in RESOLVE_UTILITY_SCRIPTS:
            (util_dir / fname).unlink(missing_ok=True)

        (sdir / "Deliver" / "Obol - Export and Publish.lua").unlink(missing_ok=True)
        return self.status(scripts_dir=sdir, allow_studio_api=allow_studio_api)
