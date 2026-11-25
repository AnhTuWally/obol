"""Unified DCC Send Registry, active-target resolution, and built-in DCC send adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional, Protocol

from obol.core.hub import ObolHub
from obol.core.storage import discover_project_components, load_component_snapshot
from obol.core.workspace import ObolWorkspace
from obol.plugins.filmcraft_bridge import FilmCraftBridgeClient
from obol.plugins.reaper_bridge import ReaperBridgeClient
from obol.plugins.resolve_bridge import ResolveBridgeClient


@dataclass(frozen=True)
class DCCSendMode:
    """Describes one way of sending media files to a DCC target."""

    id: str
    label: str
    short_label: str = ""
    tooltip: str = ""
    requires_live: bool = True
    is_update_action: bool = False
    remember_as_default: bool = True

    @property
    def effective_short_label(self) -> str:
        return self.short_label or self.label


@dataclass
class DCCSendStatus:
    """Live/offline readiness snapshot of a DCC send target."""

    live: bool = False
    can_send_offline: bool = False
    mode_badge: str = "Offline"
    detail: str = ""


@dataclass
class DCCSendContext:
    """Context supplied by GUI/CLI callers when resolving targets or sending media."""

    hub: ObolHub
    workspace: ObolWorkspace
    project_root: Optional[Path] = None
    project_name: str = ""
    component_path: str = ""
    component_software: str = ""
    choose_option: Optional[Callable[[str, str, list[str]], Optional[int]]] = None


@dataclass
class DCCSendResult:
    """Outcome of a DCC send operation."""

    ok: bool
    message: str
    sent_count: int = 0
    offline_hint: bool = False
    details: dict[str, Any] = field(default_factory=dict)


class DCCSendTarget(Protocol):
    """Protocol implemented by built-in and plugin-provided DCC send destinations."""

    key: str
    display_name: str
    full_name: str
    default_mode_id: str

    def get_modes(self) -> list[DCCSendMode]:
        ...

    def get_status(self, ctx: DCCSendContext) -> DCCSendStatus:
        ...

    def send_files(self, paths: list[Path], mode_id: str, ctx: DCCSendContext) -> DCCSendResult:
        ...


@dataclass
class ResolvedDCCTarget:
    """Resolved target status paired with active/pinned flags and remembered default mode."""

    target: DCCSendTarget
    status: DCCSendStatus
    default_mode: DCCSendMode
    is_pinned: bool = False
    is_active_target: bool = False


class ReaperSendTarget:
    """Send target adapter for REAPER DAW via ReaperBridgeClient."""

    key = "reaper"
    display_name = "REAPER"
    full_name = "REAPER DAW"
    default_mode_id = "new_track"

    _MODES = [
        DCCSendMode("new_track", "New Track", "New Track", "Insert media on new track(s)"),
        DCCSendMode(
            "cursor_track",
            "At Cursor (Selected Track)",
            "At Cursor",
            "Insert media on selected track at edit cursor",
        ),
        DCCSendMode(
            "new_take",
            "New Take (Selected Item)",
            "New Take",
            "Add media as a new take on selected item",
        ),
        DCCSendMode(
            "update_existing",
            "Update Existing in Session",
            "Update Existing",
            "Update matching Obol takes in the active REAPER session",
            is_update_action=True,
            remember_as_default=False,
        ),
    ]

    def __init__(self, bridge: ReaperBridgeClient) -> None:
        self.bridge = bridge

    def get_modes(self) -> list[DCCSendMode]:
        return list(self._MODES)

    def get_status(self, ctx: DCCSendContext) -> DCCSendStatus:
        st = self.bridge.get_state()
        detail = Path(st.active_rpp).name if st.active_rpp else st.reaper_version
        return DCCSendStatus(
            live=st.connected,
            can_send_offline=False,
            mode_badge="Live" if st.connected else "Offline",
            detail=detail,
        )

    def send_files(self, paths: list[Path], mode_id: str, ctx: DCCSendContext) -> DCCSendResult:
        existing = [p for p in paths if p.is_file()]
        if not existing:
            return DCCSendResult(False, "Target file(s) are not available locally on disk.")

        if not self.bridge.is_connected():
            if mode_id == "update_existing":
                return DCCSendResult(
                    False,
                    "REAPER Bridge is not currently running.\n\n"
                    "Open REAPER (with Obol Bridge running) to update imported MIDI or media takes.",
                    offline_hint=True,
                    details={"updated_takes": 0, "updated_items": 0},
                )
            return DCCSendResult(
                False,
                "REAPER Bridge is not currently running.\n\n"
                "Open REAPER (with Obol Bridge installed via ⚙ Settings → DCC Integrations...), "
                "or drag the file directly from this list into REAPER's Arrange view.",
                offline_hint=True,
            )

        if mode_id == "update_existing":
            return self._update_existing(existing, ctx)

        metas: list[dict[str, Any]] = []
        for p in existing:
            meta = (
                ctx.workspace.describe_asset_file(ctx.project_root, p)
                if ctx.project_root
                else None
            )
            if meta is None:
                meta = {
                    "file_path": str(p.resolve()),
                    "file_name": p.name,
                    "media_kind": "midi" if p.suffix.lower() in (".mid", ".midi") else "media",
                }
            metas.append(meta)

        res = self.bridge.insert_media(
            existing,
            mode=mode_id,
            asset_meta=(metas[0] if len(metas) == 1 else {}),
            asset_metas=metas,
        )
        inserted = int(res.get("inserted", len(existing)))
        return DCCSendResult(
            True,
            f"Imported {inserted} file(s) into REAPER",
            sent_count=inserted,
            details=res,
        )

    def _update_existing(self, existing: list[Path], ctx: DCCSendContext) -> DCCSendResult:
        if not ctx.project_root:
            return DCCSendResult(
                False,
                "Could not resolve target file to an Obol repo/ or published/ asset.",
                details={"updated_takes": 0, "updated_items": 0},
            )

        target_by_key: dict[tuple[str, str, str], tuple[Path, dict[str, Any]]] = {}
        for p in existing:
            target_meta = ctx.workspace.describe_asset_file(ctx.project_root, p)
            if target_meta is not None:
                key = (
                    str(target_meta["component"]),
                    str(target_meta["scope"]),
                    str(target_meta["asset_key"]),
                )
                target_by_key[key] = (p, target_meta)

        if not target_by_key:
            return DCCSendResult(
                False,
                "Could not resolve target file to an Obol repo/ or published/ asset.",
                details={"updated_takes": 0, "updated_items": 0},
            )

        session_items = self.bridge.query_session_media()
        take_updates: list[dict[str, Any]] = []
        replacements: dict[str, str] = {}
        for sit in session_items:
            resolved = ctx.workspace.resolve_reaper_session_item(ctx.project_root, sit)
            if resolved is None:
                continue
            item_key = (
                str(resolved["component"]),
                str(resolved["kind"]),
                str(resolved["asset_key"]),
            )
            matched = target_by_key.get(item_key)
            if matched is None:
                continue
            target_path, target_meta = matched
            curr_file = Path(str(resolved["current_file"]))
            replacements[str(curr_file)] = str(target_path.resolve())
            take_updates.append({
                "take_guid": str(resolved.get("take_guid") or ""),
                "item_guid": str(resolved.get("item_guid") or ""),
                "old_path": str(curr_file),
                "target_path": str(target_path.resolve()),
                "asset_meta": target_meta,
            })

        if not take_updates:
            first_meta = next(iter(target_by_key.values()))[1]
            return DCCSendResult(
                False,
                f"No takes matching '{first_meta['component']} / {first_meta['asset_key']}' "
                "were found in the active REAPER session.",
                offline_hint=True,
                details={"updated_takes": 0, "updated_items": 0},
            )

        res = self.bridge.update_media_sources(
            replacements=replacements,
            take_updates=take_updates,
            update_mode="replace_source",
            midi_mode="preserve",
            match_length="auto",
        )
        updated_takes = int(res.get("updated_takes", 0))
        if len(target_by_key) == 1:
            ver_num = int(next(iter(target_by_key.values()))[1]["version"])
            msg = f"Updated {updated_takes} take(s) in REAPER to v{ver_num:03d}"
        else:
            msg = f"Updated {updated_takes} take(s) in REAPER"
        return DCCSendResult(True, msg, sent_count=updated_takes, details=res)


class ResolveSendTarget:
    """Send target adapter for DaVinci Resolve via ResolveBridgeClient."""

    key = "resolve"
    display_name = "Resolve"
    full_name = "DaVinci Resolve"
    default_mode_id = "media_pool"

    _MODES = [
        DCCSendMode("media_pool", "Media Pool", "Media Pool", "Load media into Resolve Media Pool"),
        DCCSendMode(
            "timeline",
            "Media Pool + Timeline",
            "+ Timeline",
            "Load media into Media Pool and append to active timeline",
        ),
    ]

    def __init__(self, bridge: ResolveBridgeClient) -> None:
        self.bridge = bridge

    def get_modes(self) -> list[DCCSendMode]:
        return list(self._MODES)

    def get_status(self, ctx: DCCSendContext) -> DCCSendStatus:
        st = self.bridge.get_state()
        if st.connected:
            badge = "Studio" if st.mode == "studio_api" else "Bridge"
        else:
            badge = "Offline"
        return DCCSendStatus(
            live=st.connected,
            can_send_offline=False,
            mode_badge=badge,
            detail=st.project_name,
        )

    def send_files(self, paths: list[Path], mode_id: str, ctx: DCCSendContext) -> DCCSendResult:
        existing = [p for p in paths if p.is_file()]
        if not existing:
            return DCCSendResult(False, "Target file(s) are not available locally on disk.")
        if not self.bridge.is_connected():
            return DCCSendResult(
                False,
                "DaVinci Resolve is not currently connected.\n\n"
                "Inside Resolve, run Workspace → Scripts → Utility → Obol - Load Media into Media Pool "
                "(or start Obol - Live Bridge in Resolve for direct GUI push).",
                offline_hint=True,
            )
        bin_path = f"Obol/{ctx.component_path}" if ctx.component_path else ""
        res = self.bridge.import_media(
            existing,
            bin_path=bin_path,
            append_to_timeline=(mode_id == "timeline"),
        )
        imported = int(res.get("imported", len(existing)))
        return DCCSendResult(
            True,
            f"Loaded {imported} clip(s) into Resolve Media Pool",
            sent_count=imported,
            details=res,
        )


class FilmCraftSendTarget:
    """Send target adapter for FilmCraft NLE via FilmCraftBridgeClient (live or headless .fcproj)."""

    key = "filmcraft"
    display_name = "FilmCraft"
    full_name = "FilmCraft"
    default_mode_id = "project_bin"

    _MODES = [
        DCCSendMode(
            "project_bin",
            "Project Bin",
            "Project Bin",
            "Import media into FilmCraft Project Bin",
            requires_live=False,
        ),
        DCCSendMode(
            "timeline",
            "Project Bin + Timeline",
            "+ Timeline",
            "Import media into FilmCraft and place on timeline",
            requires_live=False,
        ),
    ]

    def __init__(self, bridge: FilmCraftBridgeClient) -> None:
        self.bridge = bridge

    def get_modes(self) -> list[DCCSendMode]:
        return list(self._MODES)

    @staticmethod
    def list_project_fcproj_files(project_root: Optional[Path]) -> list[tuple[str, Path]]:
        if not project_root or not project_root.is_dir():
            return []
        found: list[tuple[str, Path]] = []
        for comp in discover_project_components(project_root):
            snap = load_component_snapshot(project_root, comp)
            if not snap.working_versions:
                continue
            latest = snap.working_versions[-1]
            if not latest.primary_file:
                continue
            p = (project_root / comp / latest.primary_file).resolve()
            if (snap.software == "filmcraft" or p.suffix.lower() == ".fcproj") and p.is_file():
                found.append((comp, p))
        return found

    @classmethod
    def _active_component_fcproj(cls, ctx: DCCSendContext) -> Optional[Path]:
        if not ctx.project_root or not ctx.component_path:
            return None
        snap = load_component_snapshot(ctx.project_root, ctx.component_path)
        if snap.software != "filmcraft" or not snap.working_versions:
            return None
        latest = snap.working_versions[-1]
        if not latest.primary_file:
            return None
        p = (ctx.project_root / ctx.component_path / latest.primary_file).resolve()
        if p.suffix.lower() == ".fcproj" and p.is_file():
            return p
        return None

    def get_status(self, ctx: DCCSendContext) -> DCCSendStatus:
        if self.bridge.is_live_bridge_listening(timeout_s=0.05):
            return DCCSendStatus(
                live=True,
                can_send_offline=False,
                mode_badge="Live",
                detail=f":{self.bridge.control_port}",
            )
        active_fcproj = self._active_component_fcproj(ctx)
        if active_fcproj is not None:
            return DCCSendStatus(
                live=False,
                can_send_offline=True,
                mode_badge="Headless .fcproj",
                detail=active_fcproj.name,
            )
        candidates = self.list_project_fcproj_files(ctx.project_root)
        if candidates:
            return DCCSendStatus(
                live=False,
                can_send_offline=True,
                mode_badge="Headless .fcproj",
                detail=candidates[0][1].name,
            )
        return DCCSendStatus(
            live=False,
            can_send_offline=False,
            mode_badge="Offline",
            detail="",
        )

    def send_files(self, paths: list[Path], mode_id: str, ctx: DCCSendContext) -> DCCSendResult:
        existing = [p for p in paths if p.is_file()]
        if not existing:
            return DCCSendResult(False, "Target file(s) are not available locally on disk.")

        live = self.bridge.is_live_bridge_listening()
        fcproj: Optional[Path] = None if live else self._active_component_fcproj(ctx)

        if not live and fcproj is None:
            candidates = self.list_project_fcproj_files(ctx.project_root)
            if len(candidates) == 1:
                fcproj = candidates[0][1]
            elif len(candidates) > 1:
                labels = [f"{comp} ({p.name})" for comp, p in candidates]
                if ctx.choose_option is not None:
                    chosen_idx = ctx.choose_option(
                        "Select Target FilmCraft Project",
                        "FilmCraft live bridge is offline. Select which component's .fcproj to import into:",
                        labels,
                    )
                    if chosen_idx is None or not (0 <= chosen_idx < len(candidates)):
                        return DCCSendResult(False, "Cancelled.", details={"cancelled": True})
                    fcproj = candidates[chosen_idx][1]
                else:
                    fcproj = candidates[0][1]

        if not live and fcproj is None:
            return DCCSendResult(
                False,
                "No live FilmCraft --control session or FilmCraft working .fcproj was found in this project.\n\n"
                "Open a FilmCraft working version from Obol or create a FilmCraft component first.",
                offline_hint=True,
            )

        bin_path = f"Obol/{ctx.component_path}" if ctx.component_path else ""
        res = self.bridge.import_media(
            existing,
            bin_path=bin_path,
            place_on_timeline=(mode_id == "timeline"),
            project_path=fcproj,
            use_bridge=live,
        )
        count = int(res.get("imported", len(existing)))
        if live:
            msg = f"Imported {count} file(s) into live FilmCraft"
        else:
            assert fcproj is not None
            msg = (
                f"Imported {count} file(s) into {fcproj.name} "
                "(headless — reopen in FilmCraft or open via Obol for live bridge)"
            )
        return DCCSendResult(True, msg, sent_count=count, details=res)


class DCCSendRegistry:
    """Central registry of DCC media send destinations, status ranking, and dispatch."""

    def __init__(
        self,
        hub: ObolHub,
        reaper_bridge: Optional[ReaperBridgeClient] = None,
        resolve_bridge: Optional[ResolveBridgeClient] = None,
        filmcraft_bridge: Optional[FilmCraftBridgeClient] = None,
        allow_resolve_studio_api: bool = True,
    ) -> None:
        self.hub = hub
        self._targets: dict[str, DCCSendTarget] = {}

        r_bridge = reaper_bridge or ReaperBridgeClient(hub)
        d_bridge = resolve_bridge or ResolveBridgeClient(hub, allow_studio_api=allow_resolve_studio_api)
        f_bridge = filmcraft_bridge or FilmCraftBridgeClient(hub)

        for builtin in (
            ReaperSendTarget(r_bridge),
            ResolveSendTarget(d_bridge),
            FilmCraftSendTarget(f_bridge),
        ):
            self.register(builtin)

        self._discover_plugin_targets()

    def _discover_plugin_targets(self) -> None:
        for plugin in self.hub.plugin_registry.list_plugins().values():
            candidate = None
            get_hook = getattr(plugin, "get_send_target", None)
            if callable(get_hook):
                try:
                    candidate = get_hook(self.hub)
                except Exception:
                    candidate = None
            else:
                candidate = getattr(plugin, "send_target", None)

            if (
                candidate is not None
                and hasattr(candidate, "key")
                and callable(getattr(candidate, "get_modes", None))
                and callable(getattr(candidate, "get_status", None))
                and callable(getattr(candidate, "send_files", None))
            ):
                self.register(candidate)

    def register(self, target: DCCSendTarget) -> None:
        self._targets[str(target.key)] = target

    def unregister(self, key: str) -> None:
        self._targets.pop(key, None)

    def get(self, key: str) -> Optional[DCCSendTarget]:
        return self._targets.get(key)

    def list_targets(self) -> list[DCCSendTarget]:
        return list(self._targets.values())

    def get_default_mode(self, target: DCCSendTarget) -> DCCSendMode:
        modes = target.get_modes()
        if not modes:
            return DCCSendMode(id="default", label="Send")
        preferred_id = self.hub.get_preferred_dcc_mode(target.key, fallback=target.default_mode_id)
        for m in modes:
            if m.id == preferred_id and not m.is_update_action:
                return m
        for m in modes:
            if m.id == target.default_mode_id:
                return m
        return modes[0]

    def resolve_targets(
        self,
        ctx: DCCSendContext,
    ) -> tuple[Optional[ResolvedDCCTarget], list[ResolvedDCCTarget], list[ResolvedDCCTarget]]:
        """Resolves active target and returns (active_target, live_targets, offline_targets)."""
        pinned_key = self.hub.config.pinned_dcc_target.strip()
        last_key = self.hub.config.last_used_dcc_target.strip()
        comp_sw = ctx.component_software.strip()

        resolved_all: list[tuple[int, ResolvedDCCTarget]] = []
        for idx, target in enumerate(self._targets.values()):
            try:
                status = target.get_status(ctx)
            except Exception as exc:
                status = DCCSendStatus(live=False, can_send_offline=False, mode_badge="Error", detail=str(exc))
            rt = ResolvedDCCTarget(
                target=target,
                status=status,
                default_mode=self.get_default_mode(target),
                is_pinned=(bool(pinned_key) and target.key == pinned_key),
                is_active_target=False,
            )
            resolved_all.append((idx, rt))

        live_pairs = [(idx, rt) for idx, rt in resolved_all if rt.status.live]
        offline_pairs = [(idx, rt) for idx, rt in resolved_all if not rt.status.live]

        active_target: Optional[ResolvedDCCTarget] = None
        if pinned_key:
            active_target = next((rt for _, rt in resolved_all if rt.target.key == pinned_key), None)

        if active_target is None:
            if live_pairs:
                live_items = [rt for _, rt in live_pairs]
                active_target = (
                    next((rt for rt in live_items if rt.target.key == last_key), None)
                    or next((rt for rt in live_items if rt.target.key == comp_sw), None)
                    or live_items[0]
                )
            else:
                headless_items = [rt for _, rt in offline_pairs if rt.status.can_send_offline]
                if headless_items:
                    active_target = (
                        next((rt for rt in headless_items if rt.target.key == comp_sw), None)
                        or next((rt for rt in headless_items if rt.target.key == last_key), None)
                        or headless_items[0]
                    )

        if active_target is not None:
            active_target.is_active_target = True

        live_pairs.sort(
            key=lambda pair: (
                0 if pair[1].is_active_target else 1,
                0 if pair[1].target.key == last_key else 1,
                0 if pair[1].target.key == comp_sw else 1,
                pair[0],
            )
        )
        offline_pairs.sort(
            key=lambda pair: (
                0 if pair[1].status.can_send_offline else 1,
                0 if pair[1].is_pinned else 1,
                0 if pair[1].target.key == comp_sw else 1,
                0 if pair[1].target.key == last_key else 1,
                pair[0],
            )
        )

        return (
            active_target,
            [rt for _, rt in live_pairs],
            [rt for _, rt in offline_pairs],
        )

    def send(
        self,
        target_key: str,
        paths: list[Path],
        mode_id: Optional[str],
        ctx: DCCSendContext,
    ) -> DCCSendResult:
        target = self.get(target_key)
        if target is None:
            return DCCSendResult(False, f"Unknown DCC target '{target_key}'.")

        modes = target.get_modes()
        if mode_id:
            mode = next((m for m in modes if m.id == mode_id), None)
            if mode is None:
                mode = self.get_default_mode(target)
        else:
            mode = self.get_default_mode(target)

        try:
            result = target.send_files(list(paths), mode.id, ctx)
        except Exception as exc:
            return DCCSendResult(False, str(exc))

        if result.ok:
            self.hub.record_dcc_send(target.key, mode.id, remember_mode=mode.remember_as_default)
        return result
