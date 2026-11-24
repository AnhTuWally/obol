"""1-Click FilmCraft integration installer, status/drift inspector, and clean uninstaller."""

from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Any, Optional

from obol.core.hub import ObolHub
from obol.plugins.filmcraft_bridge import (
    DEFAULT_CONTROL_HOST,
    DEFAULT_CONTROL_PORT,
    FILMCRAFT_INTEGRATION_VERSION,
    FilmCraftBridgeClient,
    detect_filmcraft_cli_executable,
    detect_filmcraft_data_dir,
    detect_filmcraft_executable,
    detect_filmcraft_install_dir,
    generate_control_token,
    get_filmcraft_bridge_dir,
    is_valid_control_token,
)


def _norm_compare(raw: str) -> str:
    if not raw:
        return ""
    s = raw.replace("\\", "/").rstrip("/")
    return os.path.normcase(s) if sys.platform == "win32" else s


class FilmCraftInstaller:
    """Inspects, configures, and manages Obol's FilmCraft bridge config and launcher."""

    def __init__(self, hub: ObolHub) -> None:
        self.hub = hub

    @staticmethod
    def _launcher_filename() -> str:
        return "Launch_FilmCraft_Obol_Bridge.bat" if sys.platform == "win32" else "launch_filmcraft_obol_bridge.sh"

    def inspect_config_sync(
        self,
        install_dir: Optional[Path] = None,
    ) -> tuple[bool, str]:
        bridge_dir = get_filmcraft_bridge_dir(self.hub)
        cfg_path = bridge_dir / "config.json"
        if not cfg_path.is_file():
            return False, "Bridge config not installed"

        client = FilmCraftBridgeClient(self.hub, bridge_dir=bridge_dir)
        cfg = client.load_config()
        if not cfg:
            return False, "Bridge config unreadable"
        if str(cfg.get("integration_version") or "") != FILMCRAFT_INTEGRATION_VERSION:
            return False, f"Version mismatch ({cfg.get('integration_version') or 'legacy'} != {FILMCRAFT_INTEGRATION_VERSION})"
        if _norm_compare(str(cfg.get("obol_home") or "")) != _norm_compare(str(self.hub.hub_dir)):
            return False, "OBOL_HOME hub directory changed"

        resolved_dir = Path(install_dir).expanduser().resolve() if install_dir else detect_filmcraft_install_dir(self.hub)
        if resolved_dir and _norm_compare(str(cfg.get("install_dir") or "")) != _norm_compare(str(resolved_dir)):
            return False, "FilmCraft install directory changed"

        if not is_valid_control_token(str(cfg.get("control_token") or "")):
            return False, "Control token missing or invalid (64 hex chars required)"

        launcher = bridge_dir / self._launcher_filename()
        if not launcher.is_file():
            return False, "Bridge launcher script missing"

        return True, f"Up to date (v{FILMCRAFT_INTEGRATION_VERSION})"

    def status(
        self,
        install_dir: Optional[Path] = None,
        query_live: bool = False,
    ) -> dict[str, Any]:
        idir = Path(install_dir).expanduser().resolve() if install_dir else detect_filmcraft_install_dir(self.hub)
        prof = self.hub.get_software_profile("filmcraft")
        detected_exe = detect_filmcraft_executable(install_dir=idir, hub=self.hub)
        configured_exe = prof.executable or (str(detected_exe) if detected_exe else "")
        exe_exists = bool(configured_exe and Path(configured_exe).expanduser().is_file())

        cli_exe = detect_filmcraft_cli_executable(install_dir=idir, hub=self.hub)
        cli_exists = bool(cli_exe and cli_exe.is_file())

        data_dir = detect_filmcraft_data_dir()
        bridge_dir = get_filmcraft_bridge_dir(self.hub)
        cfg_path = bridge_dir / "config.json"
        launcher_path = bridge_dir / self._launcher_filename()

        client = FilmCraftBridgeClient(
            self.hub,
            bridge_dir=bridge_dir,
            cli_executable=cli_exe,
            executable=Path(configured_exe) if exe_exists else None,
        )
        cfg = client.load_config()
        cfg_ok, cfg_detail = self.inspect_config_sync(install_dir=idir)
        session = client.get_state(query_live=query_live)
        cli_version = client.get_cli_version() if cli_exists else ""

        return {
            "install_dir": str(idir) if idir else "",
            "install_dir_exists": bool(idir and idir.is_dir()),
            "executable": configured_exe,
            "executable_exists": exe_exists,
            "cli_executable": str(cli_exe) if cli_exe else "",
            "cli_exists": cli_exists,
            "cli_version": cli_version,
            "data_dir": str(data_dir) if data_dir else "",
            "data_dir_exists": bool(data_dir and data_dir.is_dir()),
            "bridge_dir": str(bridge_dir),
            "config_installed": cfg_path.is_file(),
            "config_path": str(cfg_path),
            "launcher_installed": launcher_path.is_file(),
            "launcher_path": str(launcher_path),
            "config_in_sync": cfg_ok,
            "config_detail": cfg_detail,
            "control_host": str(cfg.get("control_host") or session.control_host or DEFAULT_CONTROL_HOST),
            "control_port": int(cfg.get("control_port") or session.control_port or DEFAULT_CONTROL_PORT),
            "control_token_valid": is_valid_control_token(str(cfg.get("control_token") or "")),
            "bridge_connected": session.connected,
            "bridge_mode": session.mode,
            "active_project": session.project_name,
            "active_project_path": session.project_path,
            "active_sequence": session.active_sequence_name,
        }

    def install(
        self,
        install_dir: Optional[Path] = None,
        configure_executable: bool = True,
        executable_override: str = "",
        control_port: int = DEFAULT_CONTROL_PORT,
    ) -> dict[str, Any]:
        exe_override_path = Path(executable_override).expanduser().resolve() if executable_override.strip() else None
        idir: Optional[Path] = Path(install_dir).expanduser().resolve() if install_dir else None
        if idir is None and exe_override_path is not None:
            idir = exe_override_path.parent
        if idir is None:
            idir = detect_filmcraft_install_dir(self.hub)
        if idir is None or not idir.is_dir():
            raise FileNotFoundError(
                "Could not locate FilmCraft install directory. Browse to D:\\FilmCraft or pass --install-dir."
            )

        detected_exe = detect_filmcraft_executable(install_dir=idir, hub=self.hub)
        chosen_exe = (
            str(exe_override_path)
            if (exe_override_path and exe_override_path.is_file())
            else (str(detected_exe) if detected_exe else "")
        )
        cli_exe = detect_filmcraft_cli_executable(install_dir=idir, hub=self.hub)

        bridge_dir = get_filmcraft_bridge_dir(self.hub)
        client = FilmCraftBridgeClient(
            self.hub,
            bridge_dir=bridge_dir,
            cli_executable=cli_exe,
            executable=Path(chosen_exe) if chosen_exe else None,
            control_port=control_port,
        )
        existing_cfg = client.load_config()
        existing_tok = str(existing_cfg.get("control_token") or "").strip()
        control_token = existing_tok if is_valid_control_token(existing_tok) else generate_control_token()
        client.save_config({
            "integration_version": FILMCRAFT_INTEGRATION_VERSION,
            "install_dir": str(idir),
            "executable": chosen_exe,
            "cli_executable": str(cli_exe) if cli_exe else "",
            "control_host": DEFAULT_CONTROL_HOST,
            "control_port": int(control_port),
            "control_token": control_token,
            "obol_home": str(self.hub.hub_dir),
        })

        launcher_path = bridge_dir / self._launcher_filename()
        app_cmd = chosen_exe or str(idir / ("filmcraft.exe" if sys.platform == "win32" else "filmcraft"))
        if sys.platform == "win32":
            launcher_path.write_text(
                f'@echo off\r\n'
                f'set "FILMCRAFT_CONTROL_TOKEN={control_token}"\r\n'
                f'set "FILMCRAFT_CONTROL_PORT={int(control_port)}"\r\n'
                f'start "" "{app_cmd}" --control {int(control_port)} %*\r\n',
                encoding="utf-8",
            )
        else:
            launcher_path.write_text(
                f'#!/usr/bin/env sh\n'
                f'export FILMCRAFT_CONTROL_TOKEN="{control_token}"\n'
                f'export FILMCRAFT_CONTROL_PORT="{int(control_port)}"\n'
                f'exec "{app_cmd}" --control {int(control_port)} "$@"\n',
                encoding="utf-8",
            )
            os.chmod(launcher_path, launcher_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

        if configure_executable and chosen_exe:
            prof = self.hub.get_software_profile("filmcraft")
            prof.executable = chosen_exe
            self.hub.set_software_profile(prof)

        return self.status(install_dir=idir)

    def uninstall(self, install_dir: Optional[Path] = None) -> dict[str, Any]:
        bridge_dir = get_filmcraft_bridge_dir(self.hub)
        (bridge_dir / "config.json").unlink(missing_ok=True)
        (bridge_dir / "Launch_FilmCraft_Obol_Bridge.bat").unlink(missing_ok=True)
        (bridge_dir / "launch_filmcraft_obol_bridge.sh").unlink(missing_ok=True)
        return self.status(install_dir=install_dir)
