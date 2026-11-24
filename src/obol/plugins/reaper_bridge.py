"""Zero-dependency atomic file-based JSON IPC bridge and configuration detectors for REAPER DAW."""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from obol.core.hub import ObolHub

RENDER_PRESET_RE = re.compile(r'^<RENDERPRESET\s+(?:"([^"]+)"|(\S+))')


def detect_reaper_resource_dir() -> Optional[Path]:
    """Detects the standard REAPER resource directory for the current OS."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "REAPER")
        candidates.append(Path.home() / "AppData" / "Roaming" / "REAPER")
    elif sys.platform == "darwin":
        candidates.append(Path.home() / "Library" / "Application Support" / "REAPER")
    else:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        if xdg:
            candidates.append(Path(xdg) / "REAPER")
        candidates.append(Path.home() / ".config" / "REAPER")

    for cand in candidates:
        if cand.is_dir():
            return cand.resolve()
    return None


def detect_reaper_executable() -> Optional[Path]:
    """Detects the installed REAPER executable path if present."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        for env_key in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)"):
            base = os.environ.get(env_key)
            if base:
                candidates.append(Path(base) / "REAPER (x64)" / "reaper.exe")
                candidates.append(Path(base) / "REAPER" / "reaper.exe")
        candidates.extend([
            Path(r"C:\Program Files\REAPER (x64)\reaper.exe"),
            Path(r"C:\Program Files\REAPER\reaper.exe"),
            Path(r"C:\Program Files (x86)\REAPER\reaper.exe"),
        ])
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/REAPER.app/Contents/MacOS/REAPER"))

    for exe_name in ("reaper", "reaper.exe"):
        found = shutil.which(exe_name)
        if found:
            candidates.append(Path(found))

    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return None


def parse_reaper_render_presets(resource_dir: Optional[Path]) -> list[str]:
    """Parses saved <RENDERPRESET ...> names from REAPER's reaper-render.ini."""
    if not resource_dir:
        return []
    ini_path = Path(resource_dir) / "reaper-render.ini"
    if not ini_path.is_file():
        return []
    presets: list[str] = []
    seen: set[str] = set()
    for line in ini_path.read_text(encoding="utf-8", errors="replace").splitlines():
        m = RENDER_PRESET_RE.match(line.strip())
        if not m:
            continue
        name = (m.group(1) or m.group(2) or "").strip()
        if name and name not in seen:
            seen.add(name)
            presets.append(name)
    return presets


def get_bridge_dir(hub: ObolHub) -> Path:
    """Returns `~/.obol/bridge/reaper` and ensures inbox/ and outbox/ directories exist."""
    bdir = (hub.hub_dir / "bridge" / "reaper").resolve()
    (bdir / "inbox").mkdir(parents=True, exist_ok=True)
    (bdir / "outbox").mkdir(parents=True, exist_ok=True)
    return bdir


@dataclass
class ReaperSessionState:
    """Snapshot of the live REAPER bridge state read from state.json."""

    connected: bool = False
    heartbeat: float = 0.0
    heartbeat_age_s: float = 9999.0
    reaper_version: str = ""
    active_rpp: str = ""
    dirty: bool = False
    selected_tracks: int = 0
    render_presets: list[str] = field(default_factory=list)


class ReaperBridgeClient:
    """Atomic file-based JSON IPC client communicating with REAPER's obol_bridge.lua daemon."""

    def __init__(self, hub: ObolHub, bridge_dir: Optional[Path] = None) -> None:
        self.hub = hub
        self.bridge_dir = (bridge_dir.resolve() if bridge_dir else get_bridge_dir(hub))
        self.inbox_dir = self.bridge_dir / "inbox"
        self.outbox_dir = self.bridge_dir / "outbox"
        self.state_path = self.bridge_dir / "state.json"
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)

    def get_state(self, stale_after_s: float = 5.0) -> ReaperSessionState:
        if not self.state_path.is_file():
            return ReaperSessionState()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ReaperSessionState()
        if not isinstance(raw, dict):
            return ReaperSessionState()

        try:
            hb = float(raw.get("heartbeat", 0.0))
        except (TypeError, ValueError):
            hb = 0.0
        age = max(0.0, time.time() - hb) if hb > 0 else 9999.0
        alive_flag = raw.get("connected", True)
        connected = bool(alive_flag) and hb > 0 and age <= stale_after_s
        presets = raw.get("render_presets") or []
        return ReaperSessionState(
            connected=connected,
            heartbeat=hb,
            heartbeat_age_s=age,
            reaper_version=str(raw.get("reaper_version", "")),
            active_rpp=str(raw.get("active_rpp", "")),
            dirty=bool(raw.get("dirty", False)),
            selected_tracks=int(raw.get("selected_tracks", 0) or 0),
            render_presets=[str(p) for p in presets if str(p).strip()],
        )

    def is_connected(self, stale_after_s: float = 5.0) -> bool:
        return self.get_state(stale_after_s=stale_after_s).connected

    def send_command(
        self,
        action: str,
        params: Optional[dict[str, Any]] = None,
        timeout_s: float = 15.0,
        poll_interval_s: float = 0.05,
    ) -> dict[str, Any]:
        """Sends a command to REAPER via atomic inbox JSON and waits for matching outbox JSON."""
        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)

        cmd_id = f"{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"
        payload = {
            "id": cmd_id,
            "action": action,
            "params": dict(params or {}),
            "ts": time.time(),
        }
        inbox_file = self.inbox_dir / f"{cmd_id}.json"
        tmp_file = self.inbox_dir / f"{cmd_id}.json.tmp"
        tmp_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp_file, inbox_file)

        outbox_file = self.outbox_dir / f"{cmd_id}.json"
        deadline = time.monotonic() + max(0.2, timeout_s)

        while time.monotonic() < deadline:
            if outbox_file.is_file():
                try:
                    raw = outbox_file.read_text(encoding="utf-8")
                    resp = json.loads(raw)
                except (OSError, json.JSONDecodeError):
                    time.sleep(poll_interval_s)
                    continue
                try:
                    outbox_file.unlink(missing_ok=True)
                except OSError:
                    pass
                if not isinstance(resp, dict):
                    raise RuntimeError(f"Malformed REAPER bridge response for '{action}'.")
                if not resp.get("ok", False):
                    raise RuntimeError(str(resp.get("error") or f"REAPER command '{action}' failed."))
                result = resp.get("result")
                return dict(result) if isinstance(result, dict) else {"value": result}
            time.sleep(poll_interval_s)

        # Clean up unclaimed command if REAPER did not consume it
        try:
            inbox_file.unlink(missing_ok=True)
        except OSError:
            pass
        raise TimeoutError(
            f"Timed out waiting for REAPER bridge response to '{action}' after {timeout_s:.1f}s. "
            "Ensure REAPER is open and Obol Bridge is running."
        )

    def open_project(
        self,
        rpp_path: Path,
        new_tab: bool = False,
        noprompt: bool = False,
        timeout_s: float = 15.0,
    ) -> dict[str, Any]:
        return self.send_command(
            "open_project",
            {
                "path": str(Path(rpp_path).resolve()),
                "new_tab": bool(new_tab),
                "noprompt": bool(noprompt),
            },
            timeout_s=timeout_s,
        )

    def save_project(self, timeout_s: float = 15.0) -> dict[str, Any]:
        return self.send_command("save_project", {}, timeout_s=timeout_s)

    def reload_project(self, rpp_path: Path, timeout_s: float = 15.0) -> dict[str, Any]:
        return self.open_project(rpp_path, new_tab=False, noprompt=True, timeout_s=timeout_s)

    def insert_media(
        self,
        file_paths: list[Path],
        mode: str = "new_track",
        asset_meta: Optional[dict[str, Any]] = None,
        timeout_s: float = 20.0,
    ) -> dict[str, Any]:
        paths = [str(Path(p).resolve()) for p in file_paths]
        return self.send_command(
            "insert_media",
            {
                "file_paths": paths,
                "mode": mode,
                "asset_meta": asset_meta or {},
            },
            timeout_s=timeout_s,
        )

    def query_session_media(self, timeout_s: float = 15.0) -> list[dict[str, Any]]:
        res = self.send_command("query_session_media", {}, timeout_s=timeout_s)
        items = res.get("items", [])
        return [dict(it) for it in items if isinstance(it, dict)]

    def update_media_sources(self, replacements: dict[str, str], timeout_s: float = 20.0) -> dict[str, Any]:
        norm_map = {str(Path(k).resolve()): str(Path(v).resolve()) for k, v in replacements.items()}
        return self.send_command(
            "update_media_sources",
            {"replacements": norm_map},
            timeout_s=timeout_s,
        )

    def render_direct(
        self,
        output_dir: Path,
        pattern: str = "",
        render_mode: str = "master",
        bounds: str = "entire",
        preset_name: str = "",
        timeout_s: float = 300.0,
    ) -> dict[str, Any]:
        return self.send_command(
            "render_direct",
            {
                "output_dir": str(Path(output_dir).resolve()),
                "pattern": pattern,
                "render_mode": render_mode,
                "bounds": bounds,
                "preset_name": preset_name,
            },
            timeout_s=timeout_s,
        )

    def open_render_dialog(
        self,
        output_dir: Path,
        pattern: str = "",
        restore_after: bool = True,
        timeout_s: float = 600.0,
    ) -> dict[str, Any]:
        return self.send_command(
            "open_render_dialog",
            {
                "output_dir": str(Path(output_dir).resolve()),
                "pattern": pattern,
                "restore_after": bool(restore_after),
            },
            timeout_s=timeout_s,
        )
