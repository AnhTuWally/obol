"""Dual-mode FilmCraft bridge: Live TCP control server (--bridge) + Headless filmcraft-cli engine."""

from __future__ import annotations

import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from obol.core.hub import ObolHub
from obol.plugins.filmcraft import FilmCraftPlugin

DEFAULT_CONTROL_HOST = "127.0.0.1"
DEFAULT_CONTROL_PORT = 47420
FILMCRAFT_INTEGRATION_VERSION = "1.1"

_CONTROL_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{64}$")


def is_valid_control_token(token: str) -> bool:
    """Returns True when token is a 64-character hex string required by FilmCraft."""
    return bool(_CONTROL_TOKEN_RE.fullmatch((token or "").strip()))


def generate_control_token() -> str:
    """Generates a cryptographically random 64-character hex control token."""
    return secrets.token_hex(32)

_CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
_CLI_VERSION_CACHE: dict[tuple[str, int, int], str] = {}


def _exe_names(base: str) -> tuple[str, ...]:
    if sys.platform == "win32":
        return (f"{base}.exe", base)
    return (base,)


def _find_in_dir(directory: Path, names: tuple[str, ...]) -> Optional[Path]:
    if not directory.is_dir():
        return None
    for name in names:
        cand = directory / name
        if cand.is_file():
            return cand.resolve()
    return None


def detect_filmcraft_install_dir(hub: Optional[ObolHub] = None) -> Optional[Path]:
    """Detects the FilmCraft installation folder containing filmcraft / filmcraft-cli."""
    candidates: list[Path] = []

    if hub is not None:
        try:
            prof = hub.get_software_profile("filmcraft")
            if prof.executable:
                p = Path(prof.executable).expanduser()
                candidates.append(p.parent if p.suffix else p)
        except Exception:
            pass
        cfg_file = hub.hub_dir / "bridge" / "filmcraft" / "config.json"
        if cfg_file.is_file():
            try:
                raw = json.loads(cfg_file.read_text(encoding="utf-8"))
                if isinstance(raw, dict) and raw.get("install_dir"):
                    candidates.append(Path(str(raw["install_dir"])).expanduser())
            except (OSError, json.JSONDecodeError):
                pass

    for env_key in ("FILMCRAFT_HOME", "FILMCRAFT_DIR"):
        val = os.environ.get(env_key, "").strip()
        if val:
            candidates.append(Path(val).expanduser())

    for exe in ("filmcraft.exe", "filmcraft", "filmcraft-cli.exe", "filmcraft-cli"):
        found = shutil.which(exe)
        if found:
            candidates.append(Path(found).resolve().parent)

    if sys.platform == "win32":
        local_app = os.environ.get("LOCALAPPDATA", "")
        prog_files = os.environ.get("ProgramFiles", r"C:\Program Files")
        prog_w64 = os.environ.get("ProgramW6432", "")
        candidates.extend([
            Path(r"D:\FilmCraft"),
            Path(r"C:\FilmCraft"),
            Path(prog_files) / "FilmCraft",
        ])
        if prog_w64:
            candidates.append(Path(prog_w64) / "FilmCraft")
        if local_app:
            candidates.append(Path(local_app) / "FilmCraft")
            candidates.append(Path(local_app) / "Programs" / "FilmCraft")
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/FilmCraft.app/Contents/MacOS"))
    else:
        candidates.extend([Path("/opt/filmcraft"), Path.home() / ".local" / "share" / "filmcraft"])

    seen: set[str] = set()
    for cand in candidates:
        key = str(cand).lower() if sys.platform == "win32" else str(cand)
        if key in seen:
            continue
        seen.add(key)
        if _find_in_dir(cand, _exe_names("filmcraft")) or _find_in_dir(cand, _exe_names("filmcraft-cli")):
            return cand.resolve()
    return None


def detect_filmcraft_executable(install_dir: Optional[Path] = None, hub: Optional[ObolHub] = None) -> Optional[Path]:
    """Detects filmcraft.exe / filmcraft binary."""
    if install_dir is not None:
        found = _find_in_dir(Path(install_dir).expanduser(), _exe_names("filmcraft"))
        if found:
            return found
    idir = detect_filmcraft_install_dir(hub=hub)
    if idir is not None:
        found = _find_in_dir(idir, _exe_names("filmcraft"))
        if found:
            return found
    for name in _exe_names("filmcraft"):
        which = shutil.which(name)
        if which:
            return Path(which).resolve()
    return None


def detect_filmcraft_cli_executable(install_dir: Optional[Path] = None, hub: Optional[ObolHub] = None) -> Optional[Path]:
    """Detects filmcraft-cli.exe / filmcraft-cli binary."""
    if install_dir is not None:
        found = _find_in_dir(Path(install_dir).expanduser(), _exe_names("filmcraft-cli"))
        if found:
            return found
    idir = detect_filmcraft_install_dir(hub=hub)
    if idir is not None:
        found = _find_in_dir(idir, _exe_names("filmcraft-cli"))
        if found:
            return found
    for name in _exe_names("filmcraft-cli"):
        which = shutil.which(name)
        if which:
            return Path(which).resolve()
    return None


def detect_filmcraft_data_dir() -> Optional[Path]:
    """Detects FilmCraft's data directory (crash recovery, auto-save, logs, user presets)."""
    env_dir = os.environ.get("FILMCRAFT_DATA_DIR", "").strip()
    candidates: list[Path] = [Path(env_dir).expanduser()] if env_dir else []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA", "")
        if appdata:
            candidates.append(Path(appdata) / "FilmCraft")
        candidates.append(Path.home() / "AppData" / "Roaming" / "FilmCraft")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "FilmCraft")
    else:
        xdg = os.environ.get("XDG_DATA_HOME", "")
        if xdg:
            candidates.append(Path(xdg) / "FilmCraft")
        candidates.append(Path.home() / ".local" / "share" / "FilmCraft")

    for cand in candidates:
        if cand.is_dir():
            return cand.resolve()
    return None


def get_filmcraft_bridge_dir(hub: ObolHub) -> Path:
    """Returns `~/.obol/bridge/filmcraft` and ensures the directory exists."""
    bdir = (hub.hub_dir / "bridge" / "filmcraft").resolve()
    bdir.mkdir(parents=True, exist_ok=True)
    return bdir


@dataclass
class FilmCraftSessionState:
    """Snapshot of the FilmCraft bridge or headless CLI availability."""

    connected: bool = False
    mode: str = "offline"  # "live_bridge" | "headless_cli" | "offline"
    version: str = ""
    control_host: str = DEFAULT_CONTROL_HOST
    control_port: int = DEFAULT_CONTROL_PORT
    project_name: str = ""
    project_path: str = ""
    active_sequence_id: Optional[int] = None
    active_sequence_name: str = ""
    sequences: list[dict[str, Any]] = field(default_factory=list)
    dirty: bool = False


def _extract_sequences_from_root(root_node: Any) -> list[dict[str, Any]]:
    seqs: list[dict[str, Any]] = []
    if not isinstance(root_node, dict):
        return seqs
    for child in root_node.get("children", []) or []:
        if not isinstance(child, dict):
            continue
        if "children" in child:
            seqs.extend(_extract_sequences_from_root(child))
        elif child.get("type") == "Sequence":
            seqs.append({
                "id": child.get("item"),
                "name": str(child.get("name") or f"Sequence {child.get('item')}"),
                "duration": child.get("duration", 0),
            })
    return seqs


class FilmCraftBridgeClient:
    """Dual-mode FilmCraft client supporting Live Control Bridge (--bridge) and Headless CLI (--project)."""

    def __init__(
        self,
        hub: ObolHub,
        bridge_dir: Optional[Path] = None,
        cli_executable: Optional[Path] = None,
        executable: Optional[Path] = None,
        control_port: Optional[int] = None,
    ) -> None:
        self.hub = hub
        self.bridge_dir = bridge_dir.resolve() if bridge_dir else get_filmcraft_bridge_dir(hub)
        self.bridge_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = self.bridge_dir / "config.json"
        self._cli_override = Path(cli_executable).expanduser().resolve() if cli_executable else None
        self._exe_override = Path(executable).expanduser().resolve() if executable else None
        self._port_override: Optional[int] = int(control_port) if control_port is not None else None

        self.control_host = DEFAULT_CONTROL_HOST
        self.control_port = self._port_override if self._port_override is not None else DEFAULT_CONTROL_PORT
        self.reload_config()
        self._cached_cli_version: str = ""

    def load_config(self) -> dict[str, Any]:
        if not self.config_path.is_file():
            return {}
        try:
            data = json.loads(self.config_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def reload_config(self) -> dict[str, Any]:
        cfg = self.load_config()
        self.control_host = str(cfg.get("control_host") or DEFAULT_CONTROL_HOST)
        if self._port_override is not None:
            self.control_port = self._port_override
        else:
            try:
                self.control_port = int(cfg.get("control_port") or DEFAULT_CONTROL_PORT)
            except (TypeError, ValueError):
                self.control_port = DEFAULT_CONTROL_PORT
        return cfg

    def save_config(self, payload: dict[str, Any]) -> Path:
        self.bridge_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.config_path)
        return self.config_path

    def ensure_control_token(self) -> str:
        """Returns a valid 64-hex FILMCRAFT_CONTROL_TOKEN, persisting one in config.json if needed."""
        env_tok = os.environ.get("FILMCRAFT_CONTROL_TOKEN", "").strip()
        if is_valid_control_token(env_tok):
            return env_tok
        cfg = self.load_config()
        cfg_tok = str(cfg.get("control_token") or "").strip()
        if is_valid_control_token(cfg_tok):
            return cfg_tok
        tok = generate_control_token()
        cfg.setdefault("integration_version", FILMCRAFT_INTEGRATION_VERSION)
        cfg.setdefault("control_host", self.control_host)
        cfg.setdefault("control_port", int(self.control_port))
        cfg["control_token"] = tok
        self.save_config(cfg)
        return tok

    def _build_subprocess_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["FILMCRAFT_CONTROL_TOKEN"] = self.ensure_control_token()
        env["FILMCRAFT_CONTROL_PORT"] = str(int(self.control_port))
        return env

    def resolve_app_executable(self) -> Optional[Path]:
        if self._exe_override and self._exe_override.is_file():
            return self._exe_override
        prof = self.hub.get_software_profile("filmcraft")
        if prof.executable:
            cand = Path(prof.executable).expanduser()
            if cand.is_file():
                return cand.resolve()
        cfg = self.load_config()
        if cfg.get("executable"):
            cand = Path(str(cfg["executable"])).expanduser()
            if cand.is_file():
                return cand.resolve()
        return detect_filmcraft_executable(hub=self.hub)

    def resolve_cli_executable(self) -> Optional[Path]:
        if self._cli_override and self._cli_override.is_file():
            return self._cli_override
        cfg = self.load_config()
        if cfg.get("cli_executable"):
            cand = Path(str(cfg["cli_executable"])).expanduser()
            if cand.is_file():
                return cand.resolve()
        app_exe = self.resolve_app_executable()
        if app_exe is not None:
            sibling = _find_in_dir(app_exe.parent, _exe_names("filmcraft-cli"))
            if sibling:
                return sibling
        return detect_filmcraft_cli_executable(hub=self.hub)

    def is_live_bridge_listening(self, timeout_s: float = 0.15) -> bool:
        """Fast non-blocking TCP probe checking whether `filmcraft --control <port>` is listening."""
        self.reload_config()
        try:
            with socket.create_connection((self.control_host, self.control_port), timeout=timeout_s):
                return True
        except OSError:
            return False

    def is_connected(self) -> bool:
        return self.is_live_bridge_listening()

    def get_cli_version(self, timeout_s: float = 5.0) -> str:
        if self._cached_cli_version:
            return self._cached_cli_version
        cli = self.resolve_cli_executable()
        if cli is None or not cli.is_file():
            return ""
        try:
            st = cli.stat()
            cache_key = (str(cli), st.st_mtime_ns, st.st_size)
        except OSError:
            cache_key = None
        if cache_key is not None and cache_key in _CLI_VERSION_CACHE:
            self._cached_cli_version = _CLI_VERSION_CACHE[cache_key]
            return self._cached_cli_version
        try:
            proc = subprocess.run(
                [str(cli), "--version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_s,
                creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
            if proc.returncode == 0:
                ver = proc.stdout.strip()
                if ver:
                    self._cached_cli_version = ver
                    if cache_key is not None:
                        _CLI_VERSION_CACHE[cache_key] = ver
                    return ver
        except Exception:
            pass
        return ""

    def get_state(self, query_live: bool = False) -> FilmCraftSessionState:
        cli = self.resolve_cli_executable()
        live = self.is_live_bridge_listening()
        if live:
            state = FilmCraftSessionState(
                connected=True,
                mode="live_bridge",
                control_host=self.control_host,
                control_port=self.control_port,
                version=self.get_cli_version() if query_live else "",
            )
            if query_live and cli is not None:
                try:
                    insp = self.inspect_project(use_bridge=True)
                    seqs = _extract_sequences_from_root(insp.get("root"))
                    active_id = insp.get("activeSequence")
                    active_name = next((s["name"] for s in seqs if s.get("id") == active_id), "")
                    state.project_name = str(insp.get("name") or "")
                    state.project_path = str(insp.get("path") or "")
                    state.active_sequence_id = active_id
                    state.active_sequence_name = active_name
                    state.sequences = seqs
                    state.dirty = bool(insp.get("dirty", False))
                except Exception:
                    pass
            return state

        if cli is not None and cli.is_file():
            return FilmCraftSessionState(
                connected=False,
                mode="headless_cli",
                control_host=self.control_host,
                control_port=self.control_port,
            )
        return FilmCraftSessionState(
            connected=False,
            mode="offline",
            control_host=self.control_host,
            control_port=self.control_port,
        )

    def run_cli_cmd(
        self,
        args: list[str],
        *,
        project_path: Optional[Path] = None,
        save: bool = False,
        save_as: Optional[Path] = None,
        use_bridge: Optional[bool] = None,
        require_project: bool = True,
        timeout_s: float = 120.0,
    ) -> Any:
        cli = self.resolve_cli_executable()
        if cli is None or not cli.is_file():
            raise FileNotFoundError(
                "FilmCraft CLI (filmcraft-cli) was not found. Configure FilmCraft in DCC Integrations."
            )

        cmd: list[str] = [str(cli)]
        live = self.is_live_bridge_listening() if use_bridge is None else bool(use_bridge)
        if project_path is not None and use_bridge is not True:
            cmd.extend(["--project", str(Path(project_path).resolve())])
            if save:
                cmd.append("--save")
            if save_as is not None:
                cmd.extend(["--save-as", str(Path(save_as).resolve())])
        elif live and use_bridge is not False:
            cmd.extend(["--bridge", f"{self.control_host}:{self.control_port}"])
        elif require_project:
            raise RuntimeError(
                "No live FilmCraft control bridge or target .fcproj file available. "
                "Launch FilmCraft via Obol (with --control) or select a working .fcproj file."
            )

        cmd.extend(args)
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=self._build_subprocess_env(),
            timeout=timeout_s,
            creationflags=_CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        stdout = proc.stdout.strip()
        stderr = proc.stderr.strip()

        parsed: Any = None
        if stdout:
            try:
                parsed = json.loads(stdout)
            except json.JSONDecodeError:
                parsed = {"raw": stdout}

        if proc.returncode != 0:
            err_detail = stderr or stdout or f"exit code {proc.returncode}"
            if isinstance(parsed, dict) and parsed.get("offlineMedia"):
                raise RuntimeError(f"FilmCraft command failed with offline media: {err_detail}")
            raise RuntimeError(f"FilmCraft CLI error: {err_detail}")

        return parsed if parsed is not None else {}

    def exec_command(
        self,
        command_id: str,
        params: Optional[dict[str, Any]] = None,
        *,
        project_path: Optional[Path] = None,
        save: bool = False,
        use_bridge: Optional[bool] = None,
        timeout_s: float = 120.0,
    ) -> Any:
        args = ["exec", command_id]
        if params is not None:
            args.append(json.dumps(params, ensure_ascii=False))
        return self.run_cli_cmd(
            args,
            project_path=project_path,
            save=save,
            use_bridge=use_bridge,
            timeout_s=timeout_s,
        )

    def inspect_project(
        self,
        project_path: Optional[Path] = None,
        *,
        use_bridge: Optional[bool] = None,
    ) -> dict[str, Any]:
        res = self.run_cli_cmd(["inspect", "project"], project_path=project_path, use_bridge=use_bridge)
        return res if isinstance(res, dict) else {}

    def inspect_sequence(
        self,
        project_path: Optional[Path] = None,
        *,
        use_bridge: Optional[bool] = None,
    ) -> dict[str, Any]:
        res = self.run_cli_cmd(["inspect", "sequence"], project_path=project_path, use_bridge=use_bridge)
        return res if isinstance(res, dict) else {}

    def list_sequences(
        self,
        project_path: Optional[Path] = None,
        *,
        use_bridge: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        insp = self.inspect_project(project_path=project_path, use_bridge=use_bridge)
        seqs = _extract_sequences_from_root(insp.get("root"))
        active_id = insp.get("activeSequence")
        for s in seqs:
            s["active"] = s.get("id") == active_id
        return seqs

    def list_media_items(
        self,
        project_path: Optional[Path] = None,
        *,
        use_bridge: Optional[bool] = None,
    ) -> list[dict[str, Any]]:
        target = project_path
        if target is None:
            insp = self.inspect_project(use_bridge=use_bridge)
            raw_p = str(insp.get("path") or "").strip()
            if raw_p:
                target = Path(raw_p)
        if target is not None and Path(target).is_file():
            return FilmCraftPlugin.extract_media_items(Path(target))
        return []

    def list_export_presets(self, query: str = "") -> list[dict[str, Any]]:
        args = ["export", "--list-presets"]
        if query.strip():
            args.append(query.strip())
        res = self.run_cli_cmd(args, use_bridge=False, require_project=False, timeout_s=30.0)
        return [dict(it) for it in res if isinstance(it, dict)] if isinstance(res, list) else []

    def ensure_bin(
        self,
        bin_path: str,
        *,
        project_path: Optional[Path] = None,
        use_bridge: Optional[bool] = None,
    ) -> Optional[int]:
        clean = bin_path.replace("\\", "/").strip("/")
        if not clean or clean.lower() in ("root", "master"):
            return None

        insp = self.inspect_project(project_path=project_path, use_bridge=use_bridge)
        curr_node: Any = insp.get("root") or {}
        curr_bin_id: Optional[int] = None

        for seg in [s.strip() for s in clean.split("/") if s.strip()]:
            found_node = None
            for child in (curr_node.get("children") or []) if isinstance(curr_node, dict) else []:
                if isinstance(child, dict) and "children" in child and child.get("name") == seg:
                    found_node = child
                    break
            if found_node is not None:
                curr_bin_id = int(found_node["id"])
                curr_node = found_node
            else:
                params: dict[str, Any] = {"name": seg}
                if curr_bin_id is not None:
                    params["parent"] = curr_bin_id
                created = self.exec_command(
                    "file.newBin",
                    params,
                    project_path=project_path,
                    save=(project_path is not None and use_bridge is not True),
                    use_bridge=use_bridge,
                )
                curr_bin_id = int(created["bin"]) if isinstance(created, dict) and "bin" in created else None
                curr_node = {"id": curr_bin_id, "name": seg, "children": []}
        return curr_bin_id

    def import_media(
        self,
        file_paths: list[Path],
        bin_path: str = "",
        place_on_timeline: bool = False,
        project_path: Optional[Path] = None,
        use_bridge: Optional[bool] = None,
    ) -> dict[str, Any]:
        resolved_paths = [str(Path(p).resolve()) for p in file_paths if Path(p).exists()]
        if not resolved_paths:
            raise ValueError("No existing media file paths provided to import into FilmCraft.")

        target_bin_id = (
            self.ensure_bin(bin_path, project_path=project_path, use_bridge=use_bridge)
            if bin_path.strip()
            else None
        )
        save_flag = project_path is not None and use_bridge is not True

        if place_on_timeline:
            insp = self.inspect_project(project_path=project_path, use_bridge=use_bridge)
            if insp.get("activeSequence") is not None:
                params: dict[str, Any] = {"paths": resolved_paths, "place": True}
                if target_bin_id is not None:
                    params["bin"] = target_bin_id
                res = self.exec_command(
                    "edit.pasteMedia",
                    params,
                    project_path=project_path,
                    save=save_flag,
                    use_bridge=use_bridge,
                )
                return {"imported": len(resolved_paths), "placed": True, "bin": bin_path or "Root", "result": res}

        params = {"paths": resolved_paths}
        if target_bin_id is not None:
            params["bin"] = target_bin_id
        res = self.exec_command(
            "file.import",
            params,
            project_path=project_path,
            save=save_flag,
            use_bridge=use_bridge,
        )
        items = res.get("items", []) if isinstance(res, dict) else []
        return {
            "imported": len(items) if items else len(resolved_paths),
            "placed": False,
            "bin": bin_path or "Root",
            "items": items,
            "result": res,
        }

    def replace_media_items(
        self,
        replacements: dict[int, Path],
        project_path: Optional[Path] = None,
        use_bridge: Optional[bool] = None,
        keep_relative: bool = True,
    ) -> dict[str, Any]:
        if not replacements:
            return {"replaced": 0}
        save_flag = project_path is not None and use_bridge is not True
        replaced = 0
        relink_rel_map: dict[str, Path] = {}

        for item_id, new_path in replacements.items():
            resolved = Path(new_path).resolve()
            self.exec_command(
                "media.replaceFootage",
                {"item": int(item_id), "path": str(resolved)},
                project_path=project_path,
                save=save_flag,
                use_bridge=use_bridge,
            )
            replaced += 1
            relink_rel_map[str(resolved)] = resolved

        if keep_relative and project_path is not None and use_bridge is not True and relink_rel_map:
            proj_resolved = Path(project_path).resolve()
            if proj_resolved.is_file():
                FilmCraftPlugin.relink_fcproj_references(proj_resolved, relink_rel_map, relative=True)

        return {"replaced": replaced}

    def open_project(self, fcproj_path: Path, timeout_s: float = 30.0) -> dict[str, Any]:
        target = Path(fcproj_path).resolve()
        if not target.is_file():
            raise FileNotFoundError(f"FilmCraft project file not found: {target}")
        if self.is_live_bridge_listening():
            res = self.exec_command("file.open", {"path": str(target)}, use_bridge=True, timeout_s=timeout_s)
            return {"mode": "live_bridge", "path": str(target), "result": res}
        proc = self.launch_app(fcproj_path=target, with_control=True)
        return {"mode": "launched", "path": str(target), "pid": proc.pid}

    def save_project(self, save_as: Optional[Path] = None) -> dict[str, Any]:
        if save_as is not None:
            res = self.exec_command("file.saveAs", {"path": str(Path(save_as).resolve())}, use_bridge=True)
        else:
            res = self.exec_command("file.save", {}, use_bridge=True)
        return res if isinstance(res, dict) else {"result": res}

    def export_media(
        self,
        output_path: Path,
        *,
        project_path: Optional[Path] = None,
        preset: str = "",
        fmt: str = "",
        sequence_id: Optional[int] = None,
        range_mode: str = "entire",
        start_seconds: Optional[float] = None,
        end_seconds: Optional[float] = None,
        scale: Optional[float] = None,
        quality: Optional[int] = None,
        no_audio: bool = False,
        allow_offline: bool = False,
        use_bridge: Optional[bool] = None,
        timeout_s: float = 1800.0,
    ) -> dict[str, Any]:
        out_abs = Path(output_path).resolve()
        out_abs.parent.mkdir(parents=True, exist_ok=True)

        if sequence_id is not None:
            params: dict[str, Any] = {
                "path": str(out_abs),
                "sequence": int(sequence_id),
                "wait": True,
            }
            if preset.strip():
                params["preset"] = preset.strip()
            if fmt.strip():
                params["format"] = fmt.strip()
            if range_mode:
                params["range"] = range_mode
            if range_mode == "custom":
                if start_seconds is not None:
                    params["startSeconds"] = float(start_seconds)
                if end_seconds is not None:
                    params["endSeconds"] = float(end_seconds)
            if scale is not None:
                params["scale"] = float(scale)
            if quality is not None:
                params["quality"] = int(quality)
            if no_audio:
                params["audio"] = False
            res = self.exec_command(
                "file.exportMedia",
                params,
                project_path=project_path,
                save=False,
                use_bridge=use_bridge,
                timeout_s=timeout_s,
            )
            return res if isinstance(res, dict) else {"path": str(out_abs), "result": res}

        args = ["export", str(out_abs)]
        if preset.strip():
            args.extend(["--preset", preset.strip()])
        if fmt.strip():
            args.extend(["--format", fmt.strip()])
        if range_mode and range_mode != "custom":
            args.extend(["--range", range_mode])
        elif range_mode == "custom":
            if start_seconds is not None:
                args.extend(["--start", str(start_seconds)])
            if end_seconds is not None:
                args.extend(["--end", str(end_seconds)])
        if scale is not None:
            args.extend(["--scale", str(scale)])
        if quality is not None:
            args.extend(["--quality", str(int(quality))])
        if no_audio:
            args.append("--no-audio")
        if allow_offline:
            args.append("--allow-offline")

        res = self.run_cli_cmd(args, project_path=project_path, use_bridge=use_bridge, timeout_s=timeout_s)
        return res if isinstance(res, dict) else {"path": str(out_abs), "result": res}

    def render_still(
        self,
        output_path: Path,
        *,
        seconds: float = 0.0,
        scale: Optional[float] = None,
        project_path: Optional[Path] = None,
        allow_offline: bool = False,
        use_bridge: Optional[bool] = None,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        out_abs = Path(output_path).resolve()
        out_abs.parent.mkdir(parents=True, exist_ok=True)
        args = ["render", "--seconds", str(float(seconds)), "--out", str(out_abs)]
        if scale is not None:
            args.extend(["--scale", str(float(scale))])
        if allow_offline:
            args.append("--allow-offline")
        res = self.run_cli_cmd(args, project_path=project_path, use_bridge=use_bridge, timeout_s=timeout_s)
        return res if isinstance(res, dict) else {"path": str(out_abs), "result": res}

    def export_interchange(
        self,
        output_path: Path,
        *,
        fmt: str = "xml",
        sequence_id: Optional[int] = None,
        project_path: Optional[Path] = None,
        use_bridge: Optional[bool] = None,
        timeout_s: float = 120.0,
    ) -> dict[str, Any]:
        out_abs = Path(output_path).resolve()
        out_abs.parent.mkdir(parents=True, exist_ok=True)
        params: dict[str, Any] = {"format": fmt, "path": str(out_abs)}
        if sequence_id is not None:
            params["sequence"] = int(sequence_id)
        res = self.exec_command(
            "file.exportInterchange",
            params,
            project_path=project_path,
            save=False,
            use_bridge=use_bridge,
            timeout_s=timeout_s,
        )
        return res if isinstance(res, dict) else {"path": str(out_abs), "result": res}

    def probe_media(self, media_path: Path, timeout_s: float = 30.0) -> dict[str, Any]:
        res = self.run_cli_cmd(
            ["probe", str(Path(media_path).resolve())],
            use_bridge=False,
            require_project=False,
            timeout_s=timeout_s,
        )
        return res if isinstance(res, dict) else {}

    def launch_app(
        self,
        fcproj_path: Optional[Path] = None,
        with_control: bool = True,
    ) -> subprocess.Popen[bytes]:
        self.reload_config()
        exe = self.resolve_app_executable()
        if exe is None or not exe.is_file():
            raise FileNotFoundError(
                "FilmCraft executable (filmcraft.exe) not found. Configure it in DCC Integrations."
            )
        cmd: list[str] = [str(exe)]
        if with_control and not self.is_live_bridge_listening():
            cmd.extend(["--control", str(self.control_port)])
        if fcproj_path is not None:
            cmd.append(str(Path(fcproj_path).resolve()))
        cwd = str(Path(fcproj_path).resolve().parent) if fcproj_path is not None else str(exe.parent)
        env = self._build_subprocess_env() if with_control else dict(os.environ)
        return subprocess.Popen(cmd, cwd=cwd, env=env)
