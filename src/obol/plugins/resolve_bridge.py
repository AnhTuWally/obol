"""Dual-mode DaVinci Resolve bridge: Direct Studio Python API + Atomic File JSON IPC for Free & Studio."""

from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from obol.core.hub import ObolHub


def detect_resolve_scripts_dir() -> Optional[Path]:
    """Detects DaVinci Resolve's Fusion/Scripts directory for menu script installation."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "Blackmagic Design" / "DaVinci Resolve" / "Support" / "Fusion" / "Scripts")
        candidates.append(
            Path.home() / "AppData" / "Roaming" / "Blackmagic Design" / "DaVinci Resolve" / "Support" / "Fusion" / "Scripts"
        )
        progdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        candidates.append(Path(progdata) / "Blackmagic Design" / "DaVinci Resolve" / "Fusion" / "Scripts")
    elif sys.platform == "darwin":
        candidates.append(
            Path.home() / "Library" / "Application Support" / "Blackmagic Design" / "DaVinci Resolve" / "Fusion" / "Scripts"
        )
        candidates.append(Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Fusion/Scripts"))
    else:
        candidates.append(Path.home() / ".local" / "share" / "DaVinciResolve" / "Fusion" / "Scripts")
        candidates.append(Path("/opt/resolve/Fusion/Scripts"))

    for cand in candidates:
        if cand.is_dir():
            return cand.resolve()
    return None


def detect_resolve_script_api_dir() -> Optional[Path]:
    """Detects DaVinci Resolve's Developer/Scripting directory."""
    env_api = os.environ.get("RESOLVE_SCRIPT_API")
    candidates: list[Path] = [Path(env_api)] if env_api else []
    if sys.platform == "win32":
        progdata = os.environ.get("PROGRAMDATA", r"C:\ProgramData")
        candidates.append(Path(progdata) / "Blackmagic Design" / "DaVinci Resolve" / "Support" / "Developer" / "Scripting")
    elif sys.platform == "darwin":
        candidates.append(Path("/Library/Application Support/Blackmagic Design/DaVinci Resolve/Developer/Scripting"))
    else:
        candidates.append(Path("/opt/resolve/Developer/Scripting"))

    for cand in candidates:
        if cand.is_dir():
            return cand.resolve()
    return None


def detect_resolve_script_lib() -> Optional[Path]:
    """Detects DaVinci Resolve's fusionscript shared library."""
    env_lib = os.environ.get("RESOLVE_SCRIPT_LIB")
    candidates: list[Path] = [Path(env_lib)] if env_lib else []
    if sys.platform == "win32":
        for env_key in ("ProgramFiles", "ProgramW6432"):
            base = os.environ.get(env_key)
            if base:
                candidates.append(Path(base) / "Blackmagic Design" / "DaVinci Resolve" / "fusionscript.dll")
        candidates.append(Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\fusionscript.dll"))
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/Libraries/Fusion/fusionscript.so"))
    else:
        candidates.append(Path("/opt/resolve/libs/Fusion/fusionscript.so"))

    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return None


def detect_resolve_executable() -> Optional[Path]:
    """Detects the installed DaVinci Resolve executable."""
    candidates: list[Path] = []
    if sys.platform == "win32":
        for env_key in ("ProgramFiles", "ProgramW6432"):
            base = os.environ.get(env_key)
            if base:
                candidates.append(Path(base) / "Blackmagic Design" / "DaVinci Resolve" / "Resolve.exe")
        candidates.append(Path(r"C:\Program Files\Blackmagic Design\DaVinci Resolve\Resolve.exe"))
        for exe in ("Resolve.exe", "Resolve"):
            found = shutil.which(exe)
            if found:
                candidates.append(Path(found))
    elif sys.platform == "darwin":
        candidates.append(Path("/Applications/DaVinci Resolve/DaVinci Resolve.app/Contents/MacOS/Resolve"))
    else:
        candidates.append(Path("/opt/resolve/bin/resolve"))
        found = shutil.which("resolve")
        if found:
            candidates.append(Path(found))

    for cand in candidates:
        if cand.is_file():
            return cand.resolve()
    return None


def get_resolve_bridge_dir(hub: ObolHub) -> Path:
    """Returns `~/.obol/bridge/resolve` and ensures inbox/, outbox/, tmp/ exist."""
    bdir = (hub.hub_dir / "bridge" / "resolve").resolve()
    (bdir / "inbox").mkdir(parents=True, exist_ok=True)
    (bdir / "outbox").mkdir(parents=True, exist_ok=True)
    (bdir / "tmp").mkdir(parents=True, exist_ok=True)
    return bdir


def _try_connect_studio_api() -> Any:
    """Best-effort connection to a running DaVinci Resolve Studio external scripting instance."""
    try:
        api_dir = detect_resolve_script_api_dir()
        if api_dir:
            mod_dir = str(api_dir / "Modules")
            if mod_dir not in sys.path and Path(mod_dir).is_dir():
                sys.path.append(mod_dir)
        lib_path = detect_resolve_script_lib()
        if lib_path and not os.environ.get("RESOLVE_SCRIPT_LIB"):
            os.environ["RESOLVE_SCRIPT_LIB"] = str(lib_path)
        dvr = importlib.import_module("DaVinciResolveScript")
        return dvr.scriptapp("Resolve")
    except Exception:
        return None


def _norm_path_key(path_str: str) -> str:
    s = path_str.replace("\\", "/")
    return s.lower() if sys.platform == "win32" else s


@dataclass
class ResolveSessionState:
    """Snapshot of the live DaVinci Resolve session."""

    connected: bool = False
    mode: str = "offline"  # "studio_api" | "lua_bridge" | "offline"
    heartbeat: float = 0.0
    heartbeat_age_s: float = 9999.0
    product_name: str = ""
    version: str = ""
    project_name: str = ""
    timeline_name: str = ""
    current_page: str = ""
    current_bin: str = ""
    render_presets: list[str] = field(default_factory=list)
    quick_export_presets: list[str] = field(default_factory=list)


class ResolveBridgeClient:
    """Dual-mode Resolve client supporting Direct Studio API and Atomic File IPC (Free & Studio)."""

    def __init__(
        self,
        hub: ObolHub,
        bridge_dir: Optional[Path] = None,
        allow_studio_api: bool = True,
        studio_resolve_obj: Any = None,
    ) -> None:
        self.hub = hub
        self.bridge_dir = bridge_dir.resolve() if bridge_dir else get_resolve_bridge_dir(hub)
        self.inbox_dir = self.bridge_dir / "inbox"
        self.outbox_dir = self.bridge_dir / "outbox"
        self.tmp_dir = self.bridge_dir / "tmp"
        self.state_path = self.bridge_dir / "state.json"
        self.bindings_path = self.bridge_dir / "project_bindings.json"
        self.stop_flag_path = self.bridge_dir / "stop.flag"
        self.allow_studio_api = allow_studio_api
        self._studio_resolve_override = studio_resolve_obj

        self.inbox_dir.mkdir(parents=True, exist_ok=True)
        self.outbox_dir.mkdir(parents=True, exist_ok=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    def _get_studio_resolve(self) -> Any:
        if self._studio_resolve_override is not None:
            return self._studio_resolve_override
        if not self.allow_studio_api:
            return None
        return _try_connect_studio_api()

    def list_project_bindings(self) -> dict[str, dict[str, str]]:
        if not self.bindings_path.is_file():
            return {}
        try:
            data = json.loads(self.bindings_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(data, dict):
            return {}
        out: dict[str, dict[str, str]] = {}
        for rproj, entry in sorted(data.items()):
            if isinstance(entry, dict) and entry.get("project_name") and entry.get("component_path"):
                out[str(rproj)] = {
                    "project_name": str(entry["project_name"]),
                    "component_path": str(entry["component_path"]),
                }
        return out

    def remove_project_binding(self, resolve_project_name: str) -> bool:
        if not resolve_project_name or not self.bindings_path.is_file():
            return False
        data = self.list_project_bindings()
        if resolve_project_name not in data:
            return False
        data.pop(resolve_project_name, None)
        tmp = self.bindings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.bindings_path)
        return True

    def get_project_binding(self, resolve_project_name: str) -> Optional[dict[str, str]]:
        if not resolve_project_name:
            return None
        return self.list_project_bindings().get(resolve_project_name)

    def set_project_binding(self, resolve_project_name: str, project_name: str, component_path: str) -> None:
        if not resolve_project_name or not project_name or not component_path:
            return
        data: dict[str, Any] = {}
        if self.bindings_path.is_file():
            try:
                loaded = json.loads(self.bindings_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
            except (OSError, json.JSONDecodeError):
                data = {}
        data[resolve_project_name] = {
            "project_name": project_name,
            "component_path": component_path,
        }
        tmp = self.bindings_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, self.bindings_path)

    def get_state(self, stale_after_s: float = 6.0) -> ResolveSessionState:
        res = self._get_studio_resolve()
        if res is not None:
            try:
                pm = res.GetProjectManager()
                proj = pm.GetCurrentProject() if pm else None
                tl = proj.GetCurrentTimeline() if proj else None
                mp = proj.GetMediaPool() if proj else None
                curr_bin = mp.GetCurrentFolder() if mp else None
                raw_presets = proj.GetRenderPresetList() if proj else []
                presets = self._extract_preset_names(raw_presets)
                raw_qe = proj.GetQuickExportRenderPresets() if proj and hasattr(proj, "GetQuickExportRenderPresets") else []
                qe_presets = self._extract_preset_names(raw_qe)
                return ResolveSessionState(
                    connected=True,
                    mode="studio_api",
                    heartbeat=time.time(),
                    heartbeat_age_s=0.0,
                    product_name=str(res.GetProductName() or "DaVinci Resolve Studio"),
                    version=str(res.GetVersionString() or ""),
                    project_name=str(proj.GetName() if proj else ""),
                    timeline_name=str(tl.GetName() if tl else ""),
                    current_page=str(res.GetCurrentPage() or ""),
                    current_bin=str(curr_bin.GetName() if curr_bin else ""),
                    render_presets=presets,
                    quick_export_presets=qe_presets,
                )
            except Exception:
                pass

        if not self.state_path.is_file():
            return ResolveSessionState()
        try:
            raw = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return ResolveSessionState()
        if not isinstance(raw, dict):
            return ResolveSessionState()

        try:
            hb = float(raw.get("heartbeat", 0.0))
        except (TypeError, ValueError):
            hb = 0.0
        age = max(0.0, time.time() - hb) if hb > 0 else 9999.0
        connected = bool(raw.get("connected", True)) and hb > 0 and age <= stale_after_s
        return ResolveSessionState(
            connected=connected,
            mode=("lua_bridge" if connected else "offline"),
            heartbeat=hb,
            heartbeat_age_s=age,
            product_name=str(raw.get("product_name", "")),
            version=str(raw.get("version", "")),
            project_name=str(raw.get("project_name", "")),
            timeline_name=str(raw.get("timeline_name", "")),
            current_page=str(raw.get("current_page", "")),
            current_bin=str(raw.get("current_bin", "")),
            render_presets=[str(p) for p in (raw.get("render_presets") or []) if str(p).strip()],
            quick_export_presets=[str(p) for p in (raw.get("quick_export_presets") or []) if str(p).strip()],
        )

    @staticmethod
    def _extract_preset_names(raw: Any) -> list[str]:
        if not raw:
            return []
        names: list[str] = []
        values = raw.values() if isinstance(raw, dict) else raw
        for item in values:
            if isinstance(item, dict) and item.get("Name"):
                names.append(str(item["Name"]))
            elif isinstance(item, str) and item.strip():
                names.append(item.strip())
        return list(dict.fromkeys(names))

    def is_connected(self, stale_after_s: float = 6.0) -> bool:
        return self.get_state(stale_after_s=stale_after_s).connected

    def stop_lua_bridge(self) -> None:
        self.stop_flag_path.write_text("stop\n", encoding="utf-8")

    def send_command(
        self,
        action: str,
        params: Optional[dict[str, Any]] = None,
        timeout_s: float = 20.0,
        poll_interval_s: float = 0.05,
    ) -> dict[str, Any]:
        """Executes an action via Direct Studio API when available, else atomic file IPC."""
        studio_res = self._get_studio_resolve()
        if studio_res is not None:
            return self._exec_direct_studio(studio_res, action, dict(params or {}))

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
                    resp = json.loads(outbox_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    time.sleep(poll_interval_s)
                    continue
                try:
                    outbox_file.unlink(missing_ok=True)
                except OSError:
                    pass
                if not isinstance(resp, dict):
                    raise RuntimeError(f"Malformed Resolve bridge response for '{action}'.")
                if not resp.get("ok", False):
                    raise RuntimeError(str(resp.get("error") or f"Resolve command '{action}' failed."))
                result = resp.get("result")
                return dict(result) if isinstance(result, dict) else {"value": result}
            time.sleep(poll_interval_s)

        try:
            inbox_file.unlink(missing_ok=True)
        except OSError:
            pass
        raise TimeoutError(
            f"Timed out waiting for DaVinci Resolve bridge response to '{action}' after {timeout_s:.1f}s. "
            "Run Workspace → Scripts → Utility → Obol - Live Bridge in Resolve, or use Obol scripts directly inside Resolve."
        )

    @staticmethod
    def _ensure_bin(media_pool: Any, bin_path: str) -> Any:
        clean = bin_path.replace("\\", "/").strip("/")
        if not clean:
            return media_pool.GetCurrentFolder()
        curr = media_pool.GetRootFolder()
        if clean.lower() in ("root", "master"):
            media_pool.SetCurrentFolder(curr)
            return curr
        for seg in [s for s in clean.split("/") if s.strip()]:
            sub_folders = curr.GetSubFolderList() or []
            found = next((f for f in sub_folders if f.GetName() == seg), None)
            if found is None:
                found = media_pool.AddSubFolder(curr, seg)
            if found is None:
                raise RuntimeError(f"Failed to create or open Media Pool bin '{seg}'.")
            curr = found
        media_pool.SetCurrentFolder(curr)
        return curr

    @classmethod
    def _walk_media_pool_clips(cls, folder: Any, prefix: str = "") -> list[tuple[str, Any]]:
        results: list[tuple[str, Any]] = []
        fname = str(folder.GetName() or "")
        curr_path = f"{prefix}/{fname}" if prefix else fname
        for clip in folder.GetClipList() or []:
            results.append((curr_path, clip))
        for sub in folder.GetSubFolderList() or []:
            results.extend(cls._walk_media_pool_clips(sub, curr_path))
        return results

    def _exec_direct_studio(self, res: Any, action: str, params: dict[str, Any]) -> dict[str, Any]:
        pm = res.GetProjectManager()
        project = pm.GetCurrentProject() if pm else None
        if project is None:
            raise RuntimeError("No DaVinci Resolve project is currently open.")
        media_pool = project.GetMediaPool()

        if action == "import_media":
            file_paths = [str(p) for p in params.get("file_paths", []) if str(p).strip()]
            if not file_paths:
                raise RuntimeError("No file paths provided to import_media.")
            bin_path = str(params.get("bin_path", ""))
            if bin_path:
                self._ensure_bin(media_pool, bin_path)
            clips = media_pool.ImportMedia(file_paths) or []
            asset_metas = params.get("asset_metas") or []
            asset_meta = params.get("asset_meta") or {}
            if clips and (asset_metas or asset_meta):
                for idx, clip in enumerate(clips):
                    meta = (
                        asset_metas[idx]
                        if idx < len(asset_metas) and isinstance(asset_metas[idx], dict)
                        else asset_meta
                    )
                    if not meta:
                        continue
                    try:
                        clip.SetThirdPartyMetadata({f"obol_{k}": str(v) for k, v in meta.items()})
                    except Exception:
                        pass
            if params.get("append_to_timeline") and clips:
                media_pool.AppendToTimeline(clips)
            return {
                "imported": len(clips),
                "bin": bin_path or str(media_pool.GetCurrentFolder().GetName()),
            }

        if action == "query_media_pool":
            root = media_pool.GetRootFolder()
            items: list[dict[str, Any]] = []
            for bpath, clip in self._walk_media_pool_clips(root):
                fpath = ""
                try:
                    fpath = str(clip.GetClipProperty("File Path") or "")
                except Exception:
                    pass
                obol_meta: dict[str, Any] = {}
                try:
                    if hasattr(clip, "GetThirdPartyMetadata"):
                        raw_meta = clip.GetThirdPartyMetadata() or {}
                        if isinstance(raw_meta, dict):
                            for mk, mv in raw_meta.items():
                                sk = str(mk)
                                if sk.startswith("obol_"):
                                    obol_meta[sk[5:]] = mv
                except Exception:
                    obol_meta = {}
                items.append({
                    "name": str(clip.GetName() or ""),
                    "file_path": fpath.replace("\\", "/"),
                    "bin_path": bpath,
                    "media_id": str(clip.GetMediaId() or "") if hasattr(clip, "GetMediaId") else "",
                    "obol_meta": obol_meta,
                })
            return {"items": items}

        if action == "replace_media_pool_clips":
            rep_map = {_norm_path_key(k): str(v) for k, v in (params.get("replacements") or {}).items()}
            by_media_id: dict[str, dict[str, Any]] = {}
            by_old_path: dict[str, dict[str, Any]] = {}
            for upd in (params.get("clip_updates") or []):
                if not isinstance(upd, dict) or not upd.get("target_path"):
                    continue
                mid = str(upd.get("media_id") or "").strip()
                if mid:
                    by_media_id[mid] = upd
                old_p = str(upd.get("old_path") or "").strip()
                if old_p:
                    by_old_path[_norm_path_key(old_p)] = upd

            root = media_pool.GetRootFolder()
            replaced = 0
            for _bpath, clip in self._walk_media_pool_clips(root):
                fpath = str(clip.GetClipProperty("File Path") or "")
                media_id = str(clip.GetMediaId() or "") if hasattr(clip, "GetMediaId") else ""
                upd = (by_media_id.get(media_id) if media_id else None) or by_old_path.get(_norm_path_key(fpath))
                target = str(upd["target_path"]) if upd else rep_map.get(_norm_path_key(fpath), "")
                if not target:
                    continue
                ok = False
                if hasattr(clip, "ReplaceClipPreserveSubClip"):
                    ok = bool(clip.ReplaceClipPreserveSubClip(target))
                if not ok and hasattr(clip, "ReplaceClip"):
                    ok = bool(clip.ReplaceClip(target))
                if ok:
                    replaced += 1
                    asset_meta = (upd.get("asset_meta") or {}) if upd else {}
                    if asset_meta and hasattr(clip, "SetThirdPartyMetadata"):
                        try:
                            clip.SetThirdPartyMetadata({f"obol_{k}": str(v) for k, v in asset_meta.items()})
                        except Exception:
                            pass
            return {"replaced": replaced}

        if action == "render_deliver_job":
            out_dir = Path(str(params["output_dir"])).resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            before = {p.name for p in out_dir.iterdir() if p.is_file()}
            preset = str(params.get("preset_name", ""))
            if preset:
                project.LoadRenderPreset(preset)
            settings: dict[str, Any] = {
                "TargetDir": str(out_dir),
                "SelectAllFrames": bool(params.get("select_all_frames", True)),
            }
            if params.get("custom_name"):
                settings["CustomName"] = str(params["custom_name"])
            project.SetRenderSettings(settings)
            job_id = project.AddRenderJob()
            if not job_id:
                raise RuntimeError("Failed to add render job in DaVinci Resolve.")
            project.StartRendering(job_id)
            while project.IsRenderingInProgress():
                time.sleep(0.25)
            status = project.GetRenderJobStatus(job_id) or {}
            after = sorted(str(p.resolve()) for p in out_dir.iterdir() if p.is_file() and p.name not in before)
            return {"job_id": job_id, "status": status, "rendered_files": after}

        if action == "render_quick_export":
            out_dir = Path(str(params["output_dir"])).resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            before = {p.name for p in out_dir.iterdir() if p.is_file()}
            q_params: dict[str, Any] = {"TargetDir": str(out_dir)}
            if params.get("custom_name"):
                q_params["CustomName"] = str(params["custom_name"])
            status = project.RenderWithQuickExport(str(params.get("preset_name", "")), q_params)
            after = sorted(str(p.resolve()) for p in out_dir.iterdir() if p.is_file() and p.name not in before)
            return {"status": status, "rendered_files": after}

        if action == "stage_deliver_page":
            out_dir = Path(str(params["output_dir"])).resolve()
            out_dir.mkdir(parents=True, exist_ok=True)
            res.OpenPage("deliver")
            preset = str(params.get("preset_name", ""))
            if preset:
                project.LoadRenderPreset(preset)
            settings: dict[str, Any] = {"TargetDir": str(out_dir)}
            if params.get("custom_name"):
                settings["CustomName"] = str(params["custom_name"])
            project.SetRenderSettings(settings)
            job_id = project.AddRenderJob() if params.get("add_job") else ""
            return {"staged_dir": str(out_dir), "job_id": job_id}

        if action == "export_current_still":
            out_file = Path(str(params["output_file"])).resolve()
            out_file.parent.mkdir(parents=True, exist_ok=True)
            ok = bool(project.ExportCurrentFrameAsStill(str(out_file)))
            if not ok:
                raise RuntimeError("Resolve failed to export current frame still.")
            return {"output_file": str(out_file)}

        if action == "export_project_drp":
            out_file = Path(str(params["output_file"])).resolve()
            out_file.parent.mkdir(parents=True, exist_ok=True)
            ok = bool(
                pm.ExportProject(
                    project.GetName(),
                    str(out_file),
                    bool(params.get("with_stills_and_luts", True)),
                )
            )
            if not ok:
                raise RuntimeError("Resolve failed to export .drp project snapshot.")
            return {"output_file": str(out_file)}

        raise RuntimeError(f"Unsupported Resolve Studio action: {action}")

    def import_media(
        self,
        file_paths: list[Path],
        bin_path: str = "",
        append_to_timeline: bool = False,
        asset_meta: Optional[dict[str, Any]] = None,
        asset_metas: Optional[list[dict[str, Any]]] = None,
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "file_paths": [str(Path(p).resolve()) for p in file_paths],
            "bin_path": bin_path,
            "append_to_timeline": bool(append_to_timeline),
            "asset_meta": asset_meta or {},
        }
        if asset_metas is not None:
            params["asset_metas"] = [dict(m) for m in asset_metas]
        return self.send_command("import_media", params, timeout_s=timeout_s)

    def query_media_pool(self, timeout_s: float = 20.0) -> list[dict[str, Any]]:
        res = self.send_command("query_media_pool", {}, timeout_s=timeout_s)
        items = res.get("items", [])
        return [dict(it) for it in items if isinstance(it, dict)]

    def replace_media_pool_clips(
        self,
        replacements: Optional[dict[str, str]] = None,
        *,
        clip_updates: Optional[list[dict[str, Any]]] = None,
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        norm_map = {str(Path(k).resolve()): str(Path(v).resolve()) for k, v in (replacements or {}).items()}
        payload: dict[str, Any] = {"replacements": norm_map}
        if clip_updates:
            norm_updates: list[dict[str, Any]] = []
            for upd in clip_updates:
                entry = dict(upd)
                if entry.get("old_path"):
                    entry["old_path"] = str(Path(str(entry["old_path"])).resolve())
                if entry.get("target_path"):
                    entry["target_path"] = str(Path(str(entry["target_path"])).resolve())
                norm_updates.append(entry)
            payload["clip_updates"] = norm_updates
        return self.send_command(
            "replace_media_pool_clips",
            payload,
            timeout_s=timeout_s,
        )

    def render_deliver_job(
        self,
        output_dir: Path,
        custom_name: str = "",
        preset_name: str = "",
        select_all_frames: bool = True,
        timeout_s: float = 1800.0,
    ) -> dict[str, Any]:
        return self.send_command(
            "render_deliver_job",
            {
                "output_dir": str(Path(output_dir).resolve()),
                "custom_name": custom_name,
                "preset_name": preset_name,
                "select_all_frames": bool(select_all_frames),
            },
            timeout_s=timeout_s,
        )

    def render_quick_export(
        self,
        output_dir: Path,
        preset_name: str,
        custom_name: str = "",
        timeout_s: float = 1800.0,
    ) -> dict[str, Any]:
        return self.send_command(
            "render_quick_export",
            {
                "output_dir": str(Path(output_dir).resolve()),
                "preset_name": preset_name,
                "custom_name": custom_name,
            },
            timeout_s=timeout_s,
        )

    def stage_deliver_page(
        self,
        output_dir: Path,
        custom_name: str = "",
        preset_name: str = "",
        add_job: bool = False,
        timeout_s: float = 20.0,
    ) -> dict[str, Any]:
        return self.send_command(
            "stage_deliver_page",
            {
                "output_dir": str(Path(output_dir).resolve()),
                "custom_name": custom_name,
                "preset_name": preset_name,
                "add_job": bool(add_job),
            },
            timeout_s=timeout_s,
        )

    def export_current_still(self, output_file: Path, timeout_s: float = 30.0) -> dict[str, Any]:
        return self.send_command(
            "export_current_still",
            {"output_file": str(Path(output_file).resolve())},
            timeout_s=timeout_s,
        )

    def export_project_drp(
        self,
        output_file: Path,
        with_stills_and_luts: bool = True,
        timeout_s: float = 30.0,
    ) -> dict[str, Any]:
        return self.send_command(
            "export_project_drp",
            {
                "output_file": str(Path(output_file).resolve()),
                "with_stills_and_luts": bool(with_stills_and_luts),
            },
            timeout_s=timeout_s,
        )
