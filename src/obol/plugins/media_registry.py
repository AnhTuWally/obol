"""Unified DCC Media Management Registry, loaded media inspection, and version-up / version-down adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional, Protocol

from obol.core.hub import ObolHub
from obol.core.storage import discover_project_components, load_component_snapshot
from obol.core.workspace import ObolWorkspace
from obol.plugins.filmcraft_bridge import FilmCraftBridgeClient
from obol.plugins.reaper import ReaperPlugin
from obol.plugins.reaper_bridge import ReaperBridgeClient
from obol.plugins.resolve_bridge import ResolveBridgeClient


class MediaStatus(str, Enum):
    """Version status of a media item loaded inside a DCC."""

    UP_TO_DATE = "up_to_date"
    UPDATE_AVAILABLE = "update_available"
    MISSING = "missing"
    EXTERNAL = "external"


@dataclass(frozen=True)
class MediaVersionChoice:
    """One selectable asset version (v001, v002, ...) for upgrading or versioning down."""

    version: int
    version_label: str
    file_path: Path
    file_name: str
    is_latest: bool = False
    comment: str = ""
    created_at: str = ""
    size_bytes: int = 0


@dataclass
class LoadedMediaItem:
    """Normalized representation of a single media clip/take loaded inside a DCC."""

    dcc_key: str
    item_id: str
    display_name: str
    location: str
    media_type: str
    file_path: Optional[Path]
    raw_reference: str = ""
    tracked: bool = False
    status: MediaStatus = MediaStatus.EXTERNAL
    project_name: str = ""
    project_root: Optional[Path] = None
    component: str = ""
    scope: str = ""
    asset_key: str = ""
    current_version: int = 0
    latest_version: int = 0
    latest_file: Optional[Path] = None
    available_versions: list[MediaVersionChoice] = field(default_factory=list)
    matched_by: str = ""
    can_switch_version: bool = False
    can_collect: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def asset_label(self) -> str:
        if self.tracked:
            return f"{self.component} / {self.scope} / {self.asset_key}"
        return "External (Unmanaged)"

    @property
    def update_available(self) -> bool:
        return self.status == MediaStatus.UPDATE_AVAILABLE

    @property
    def can_version_down(self) -> bool:
        return self.tracked and any(v.version < self.current_version for v in self.available_versions)

    @property
    def can_version_up(self) -> bool:
        return self.tracked and any(v.version > self.current_version for v in self.available_versions)

    def version_for(self, version: int) -> Optional[MediaVersionChoice]:
        for v in self.available_versions:
            if v.version == version:
                return v
        return None

    def prev_version(self) -> Optional[MediaVersionChoice]:
        older = [v for v in self.available_versions if v.version < self.current_version]
        return max(older, key=lambda v: v.version) if older else None

    def next_version(self) -> Optional[MediaVersionChoice]:
        newer = [v for v in self.available_versions if v.version > self.current_version]
        return min(newer, key=lambda v: v.version) if newer else None

    def latest_version_choice(self) -> Optional[MediaVersionChoice]:
        if not self.available_versions:
            return None
        return max(self.available_versions, key=lambda v: v.version)

    def to_dict(self) -> dict[str, Any]:
        return {
            "dcc_key": self.dcc_key,
            "item_id": self.item_id,
            "display_name": self.display_name,
            "location": self.location,
            "media_type": self.media_type,
            "file_path": str(self.file_path) if self.file_path else "",
            "raw_reference": self.raw_reference,
            "tracked": self.tracked,
            "status": self.status.value,
            "project_name": self.project_name,
            "project_root": str(self.project_root) if self.project_root else "",
            "component": self.component,
            "scope": self.scope,
            "asset_key": self.asset_key,
            "current_version": self.current_version,
            "latest_version": self.latest_version,
            "latest_file": str(self.latest_file) if self.latest_file else "",
            "available_versions": [
                {
                    "version": v.version,
                    "version_label": v.version_label,
                    "file_path": str(v.file_path),
                    "file_name": v.file_name,
                    "is_latest": v.is_latest,
                    "comment": v.comment,
                    "created_at": v.created_at,
                    "size_bytes": v.size_bytes,
                }
                for v in self.available_versions
            ],
            "matched_by": self.matched_by,
            "can_switch_version": self.can_switch_version,
            "can_collect": self.can_collect,
        }


@dataclass(frozen=True)
class MediaSwitchRequest:
    """Instruction to switch a loaded DCC media item to a specific target asset version."""

    item: LoadedMediaItem
    target_version: int
    target_file: Path
    target_meta: dict[str, Any] = field(default_factory=dict)

    @property
    def is_upgrade(self) -> bool:
        return self.target_version > self.item.current_version

    @property
    def is_downgrade(self) -> bool:
        return self.target_version < self.item.current_version


@dataclass
class DCCMediaSessionInfo:
    """Describes the active or selectable DCC target for media inspection."""

    dcc_key: str
    display_name: str
    mode: str = "offline"  # "live" | "offline_project" | "script_context" | "offline"
    connected: bool = False
    session_label: str = ""
    project_file: Optional[Path] = None
    available_targets: list[tuple[str, str]] = field(default_factory=list)


@dataclass
class DCCMediaScanResult:
    """Full scan result of loaded media in a DCC session or project file."""

    session: DCCMediaSessionInfo
    items: list[LoadedMediaItem] = field(default_factory=list)
    error: str = ""

    @property
    def total_count(self) -> int:
        return len(self.items)

    @property
    def tracked_count(self) -> int:
        return sum(1 for it in self.items if it.tracked)

    @property
    def outdated_count(self) -> int:
        return sum(1 for it in self.items if it.status == MediaStatus.UPDATE_AVAILABLE)

    @property
    def up_to_date_count(self) -> int:
        return sum(1 for it in self.items if it.status == MediaStatus.UP_TO_DATE)

    @property
    def external_count(self) -> int:
        return sum(1 for it in self.items if not it.tracked and it.status == MediaStatus.EXTERNAL)

    @property
    def missing_count(self) -> int:
        return sum(1 for it in self.items if it.status == MediaStatus.MISSING)


@dataclass
class DCCMediaApplyResult:
    """Outcome of applying version-up / version-down media switches in a DCC."""

    ok: bool
    message: str
    switched_count: int = 0
    upgraded_count: int = 0
    downgraded_count: int = 0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class DCCMediaContext:
    """Caller context supplied when scanning or switching media versions in a DCC."""

    hub: ObolHub
    workspace: ObolWorkspace
    project_root: Optional[Path] = None
    project_name: str = ""
    component_path: str = ""
    target_file: Optional[Path] = None
    include_external: bool = False
    options: dict[str, Any] = field(default_factory=dict)


class DCCMediaAdapter(Protocol):
    """Protocol implemented by built-in and plugin-provided DCC media managers."""

    key: str
    display_name: str
    full_name: str

    def get_session_info(self, ctx: DCCMediaContext) -> DCCMediaSessionInfo:
        ...

    def scan_loaded_media(self, ctx: DCCMediaContext) -> DCCMediaScanResult:
        ...

    def apply_version_switches(
        self,
        switches: list[MediaSwitchRequest],
        ctx: DCCMediaContext,
    ) -> DCCMediaApplyResult:
        ...


def build_asset_meta_dict(
    item: LoadedMediaItem,
    target_version: int,
    target_file: Path,
    ctx: DCCMediaContext,
) -> dict[str, Any]:
    """Builds standardized Obol tracking metadata for a target asset version."""
    proj_root = item.project_root or ctx.project_root
    return {
        "project_root": str(proj_root.resolve()) if proj_root else "",
        "scope": item.scope,
        "component": item.component,
        "asset_key": item.asset_key,
        "version": int(target_version),
        "file_path": str(target_file.resolve()),
        "file_name": target_file.name,
        "media_kind": "midi" if target_file.suffix.lower() in (".mid", ".midi") else "media",
    }


def loaded_item_from_resolved_info(
    *,
    dcc_key: str,
    item_id: str,
    display_name: str,
    location: str,
    media_type: str,
    info: dict[str, Any],
    raw_reference: str = "",
    extra: Optional[dict[str, Any]] = None,
) -> LoadedMediaItem:
    """Converts a workspace `find_newer_asset_version` / `resolve_reaper_session_item` dict into a LoadedMediaItem."""
    curr_file = Path(str(info["current_file"])) if info.get("current_file") else None
    latest_file = Path(str(info["latest_file"])) if info.get("latest_file") else curr_file
    curr_exists = bool(info.get("current_exists", curr_file.is_file() if curr_file else False))
    update_avail = bool(info.get("update_available"))

    choices: list[MediaVersionChoice] = []
    for raw_v in info.get("available_versions") or []:
        if not isinstance(raw_v, dict) or not raw_v.get("file_path"):
            continue
        fp = Path(str(raw_v["file_path"]))
        ver_num = int(raw_v["version"])
        choices.append(
            MediaVersionChoice(
                version=ver_num,
                version_label=str(raw_v.get("version_label") or f"v{ver_num:03d}"),
                file_path=fp,
                file_name=str(raw_v.get("file_name") or fp.name),
                is_latest=bool(raw_v.get("is_latest")),
                comment=str(raw_v.get("comment") or ""),
                created_at=str(raw_v.get("created_at") or ""),
                size_bytes=int(raw_v.get("size_bytes") or 0),
            )
        )
    choices.sort(key=lambda c: c.version)

    if not curr_exists and curr_file is not None:
        status = MediaStatus.MISSING
    elif update_avail:
        status = MediaStatus.UPDATE_AVAILABLE
    else:
        status = MediaStatus.UP_TO_DATE

    proj_root_val = info.get("project_root")
    proj_root = Path(str(proj_root_val)).resolve() if proj_root_val else None

    return LoadedMediaItem(
        dcc_key=dcc_key,
        item_id=item_id,
        display_name=display_name,
        location=location,
        media_type=media_type,
        file_path=curr_file,
        raw_reference=raw_reference or (str(curr_file) if curr_file else ""),
        tracked=True,
        status=status,
        project_name=str(info.get("project_name") or ""),
        project_root=proj_root,
        component=str(info.get("component") or ""),
        scope=str(info.get("kind") or ""),
        asset_key=str(info.get("asset_key") or ""),
        current_version=int(info.get("current_version") or 0),
        latest_version=int(info.get("latest_version") or 0),
        latest_file=latest_file,
        available_versions=choices,
        matched_by=str(info.get("matched_by") or "file_path"),
        can_switch_version=bool(choices),
        can_collect=False,
        extra=dict(extra or {}),
    )


def _summarize_switch_counts(switches: list[MediaSwitchRequest]) -> tuple[int, int]:
    upgraded = sum(1 for sw in switches if sw.is_upgrade)
    downgraded = sum(1 for sw in switches if sw.is_downgrade)
    return upgraded, downgraded


class ReaperMediaAdapter:
    """Media management adapter for REAPER DAW (live bridge session or offline .rpp file)."""

    key = "reaper"
    display_name = "REAPER"
    full_name = "REAPER DAW"

    def __init__(self, bridge: ReaperBridgeClient) -> None:
        self.bridge = bridge

    @staticmethod
    def _list_project_rpp_files(ctx: DCCMediaContext) -> list[tuple[str, str]]:
        if not ctx.project_root or not ctx.project_root.is_dir():
            return []
        comps = discover_project_components(ctx.project_root)
        if ctx.component_path and ctx.component_path in comps:
            comps = [ctx.component_path] + [c for c in comps if c != ctx.component_path]
        targets: list[tuple[str, str]] = []
        seen: set[Path] = set()
        for comp in comps:
            snap = load_component_snapshot(ctx.project_root, comp)
            for w in reversed(snap.working_versions):
                if not w.primary_file:
                    continue
                p = (ctx.project_root / comp / w.primary_file).resolve()
                if p.suffix.lower() == ".rpp" and p.is_file() and p not in seen:
                    seen.add(p)
                    targets.append((f"{comp} — {w.version_label} ({p.name})", str(p)))
        return targets

    def _resolve_target_rpp(self, ctx: DCCMediaContext, session: DCCMediaSessionInfo) -> Optional[Path]:
        if ctx.target_file is not None and Path(ctx.target_file).is_file():
            return Path(ctx.target_file).resolve()
        if session.project_file is not None and session.project_file.is_file():
            return session.project_file
        if session.available_targets:
            cand = Path(session.available_targets[0][1])
            if cand.is_file():
                return cand
        return None

    def get_session_info(self, ctx: DCCMediaContext) -> DCCMediaSessionInfo:
        st = self.bridge.get_state()
        targets = self._list_project_rpp_files(ctx)
        if ctx.target_file is not None:
            tf = Path(ctx.target_file).resolve()
            return DCCMediaSessionInfo(
                dcc_key=self.key,
                display_name=self.display_name,
                mode="offline_project",
                connected=False,
                session_label=tf.name,
                project_file=tf,
                available_targets=targets,
            )
        if st.connected:
            active_p = Path(st.active_rpp).resolve() if st.active_rpp and Path(st.active_rpp).is_file() else None
            lbl = Path(st.active_rpp).name if st.active_rpp else (st.reaper_version or "Active Session")
            return DCCMediaSessionInfo(
                dcc_key=self.key,
                display_name=self.display_name,
                mode="live",
                connected=True,
                session_label=lbl,
                project_file=active_p,
                available_targets=targets,
            )
        if targets:
            first_p = Path(targets[0][1])
            return DCCMediaSessionInfo(
                dcc_key=self.key,
                display_name=self.display_name,
                mode="offline_project",
                connected=False,
                session_label=first_p.name,
                project_file=first_p,
                available_targets=targets,
            )
        return DCCMediaSessionInfo(
            dcc_key=self.key,
            display_name=self.display_name,
            mode="offline",
            connected=False,
            session_label="Offline",
            available_targets=targets,
        )

    def scan_loaded_media(self, ctx: DCCMediaContext) -> DCCMediaScanResult:
        session = self.get_session_info(ctx)
        if session.mode == "live":
            try:
                session_items = self.bridge.query_session_media()
            except Exception as exc:
                return DCCMediaScanResult(session=session, items=[], error=str(exc))

            items: list[LoadedMediaItem] = []
            seen_keys: set[tuple[str, str]] = set()
            for sit in session_items:
                resolved = ctx.workspace.resolve_reaper_session_item(ctx.project_root, sit)
                take_guid = str(sit.get("take_guid") or "")
                item_guid = str(sit.get("item_guid") or "")
                item_id = take_guid or f"{sit.get('item_idx', -1)}:{sit.get('take_idx', -1)}"
                track_name = str(sit.get("track_name") or "").strip()
                take_name = str(sit.get("take_name") or "").strip()
                location = track_name or "Session"

                if resolved is not None:
                    curr_file_str = str(resolved.get("current_file") or "")
                    dedup_key = (item_id, curr_file_str)
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)

                    is_midi = bool(resolved.get("is_midi")) or str(resolved.get("media_kind")) == "midi"
                    storage = str(resolved.get("midi_storage") or "")
                    if is_midi:
                        mtype = (
                            "MIDI (in-proj)"
                            if storage == "in_project"
                            else ("MIDI (.mid)" if storage == "file_ref" else "MIDI")
                        )
                    else:
                        mtype = "Media"

                    disp = take_name or (Path(curr_file_str).name if curr_file_str else resolved["asset_key"])
                    items.append(
                        loaded_item_from_resolved_info(
                            dcc_key=self.key,
                            item_id=item_id,
                            display_name=disp,
                            location=location,
                            media_type=mtype,
                            info=resolved,
                            raw_reference=curr_file_str,
                            extra={
                                "take_guid": take_guid,
                                "item_guid": item_guid,
                                "is_midi": is_midi,
                                "midi_storage": storage,
                                "take_name": take_name,
                                "track_name": track_name,
                                "obol_ext": resolved.get("obol_ext") or {},
                            },
                        )
                    )
                elif ctx.include_external:
                    fpath_str = str(sit.get("file_path") or "").strip()
                    dedup_key = (item_id, fpath_str)
                    if dedup_key in seen_keys:
                        continue
                    seen_keys.add(dedup_key)
                    fp = Path(fpath_str).expanduser().resolve() if fpath_str else None
                    is_midi = bool(sit.get("is_midi"))
                    storage = str(sit.get("midi_storage") or "")
                    mtype = (
                        "MIDI (in-proj)"
                        if (is_midi and storage == "in_project")
                        else ("MIDI (.mid)" if is_midi else "Media")
                    )
                    exists = fp.is_file() if fp is not None else True
                    status = MediaStatus.EXTERNAL if exists else MediaStatus.MISSING
                    items.append(
                        LoadedMediaItem(
                            dcc_key=self.key,
                            item_id=item_id,
                            display_name=take_name or (fp.name if fp else "MIDI Item"),
                            location=location,
                            media_type=mtype,
                            file_path=fp,
                            raw_reference=fpath_str,
                            tracked=False,
                            status=status,
                            matched_by="untracked",
                            can_switch_version=False,
                            can_collect=bool(fp and fp.is_file()),
                            extra={"take_guid": take_guid, "item_guid": item_guid},
                        )
                    )
            return DCCMediaScanResult(session=session, items=items)

        target_rpp = self._resolve_target_rpp(ctx, session)
        if target_rpp is None:
            return DCCMediaScanResult(session=session, items=[])

        refs = ReaperPlugin.find_outdated_rpp_references(
            target_rpp,
            ctx.project_root,
            ctx.workspace,
            include_external=ctx.include_external,
        )
        items = []
        for idx, ref in enumerate(refs):
            raw_ref = str(ref.get("raw_reference") or "")
            resolved_p = Path(str(ref.get("resolved_path") or ref.get("current_file") or raw_ref))
            is_midi = str(ref.get("media_kind")) == "midi"
            mtype = "MIDI (.mid)" if is_midi else "Media"
            if ref.get("tracked", True):
                items.append(
                    loaded_item_from_resolved_info(
                        dcc_key=self.key,
                        item_id=raw_ref or f"rpp:{idx}",
                        display_name=resolved_p.name,
                        location=target_rpp.name,
                        media_type=mtype,
                        info={**ref, "matched_by": "offline .rpp"},
                        raw_reference=raw_ref,
                    )
                )
            else:
                exists = bool(ref.get("current_exists", resolved_p.is_file()))
                items.append(
                    LoadedMediaItem(
                        dcc_key=self.key,
                        item_id=raw_ref or f"rpp:{idx}",
                        display_name=resolved_p.name,
                        location=target_rpp.name,
                        media_type=mtype,
                        file_path=resolved_p,
                        raw_reference=raw_ref,
                        tracked=False,
                        status=MediaStatus.EXTERNAL if exists else MediaStatus.MISSING,
                        matched_by="offline .rpp",
                        can_switch_version=False,
                        can_collect=exists,
                    )
                )
        return DCCMediaScanResult(session=session, items=items)

    def apply_version_switches(
        self,
        switches: list[MediaSwitchRequest],
        ctx: DCCMediaContext,
    ) -> DCCMediaApplyResult:
        if not switches:
            return DCCMediaApplyResult(True, "No version changes selected.")

        session = self.get_session_info(ctx)
        upgraded, downgraded = _summarize_switch_counts(switches)

        if session.mode == "live":
            take_updates: list[dict[str, Any]] = []
            replacements: dict[str, str] = {}
            for sw in switches:
                curr_file = sw.item.file_path or Path(sw.item.raw_reference)
                target_file = Path(sw.target_file).resolve()
                if curr_file:
                    replacements[str(curr_file)] = str(target_file)
                meta = dict(sw.target_meta or build_asset_meta_dict(sw.item, sw.target_version, target_file, ctx))
                take_updates.append({
                    "take_guid": str(sw.item.extra.get("take_guid") or ""),
                    "item_guid": str(sw.item.extra.get("item_guid") or ""),
                    "old_path": str(curr_file) if curr_file else "",
                    "target_path": str(target_file),
                    "asset_meta": meta,
                })

            update_mode = str(ctx.options.get("update_mode") or "replace_source")
            midi_mode = str(ctx.options.get("midi_mode") or "preserve")
            match_length = str(ctx.options.get("match_length") or "auto")
            res = self.bridge.update_media_sources(
                replacements=replacements,
                take_updates=take_updates,
                update_mode=update_mode,
                midi_mode=midi_mode,
                match_length=match_length,
            )
            switched = int(res.get("updated_takes", len(switches)))
            return DCCMediaApplyResult(
                ok=True,
                message=f"Updated {switched} take(s) in REAPER ({upgraded} up, {downgraded} down).",
                switched_count=switched,
                upgraded_count=upgraded,
                downgraded_count=downgraded,
                details=res,
            )

        target_rpp = self._resolve_target_rpp(ctx, session)
        if target_rpp is None:
            return DCCMediaApplyResult(False, "REAPER is offline and no target .rpp working file was selected.")

        offline_map: dict[str, Path] = {}
        for sw in switches:
            if sw.item.raw_reference:
                offline_map[sw.item.raw_reference] = Path(sw.target_file).resolve()
        if not offline_map:
            return DCCMediaApplyResult(False, "No relinkable .rpp file references were found.")

        proot = ctx.project_root or switches[0].item.project_root or target_rpp.parent
        res = ReaperPlugin.update_rpp_references(target_rpp, proot, ctx.workspace, replacements=offline_map)
        switched = int(res.get("updated_count", 0))
        return DCCMediaApplyResult(
            ok=True,
            message=f"Relinked {switched} reference(s) in {target_rpp.name} ({upgraded} up, {downgraded} down).",
            switched_count=switched,
            upgraded_count=upgraded,
            downgraded_count=downgraded,
            details=res,
        )


class ResolveMediaAdapter:
    """Media management adapter for DaVinci Resolve (Studio API, Lua bridge, or script context)."""

    key = "resolve"
    display_name = "Resolve"
    full_name = "DaVinci Resolve"

    def __init__(self, bridge: ResolveBridgeClient) -> None:
        self.bridge = bridge

    def get_session_info(self, ctx: DCCMediaContext) -> DCCMediaSessionInfo:
        if isinstance(ctx.options.get("context_clips"), list):
            proj_name = str(ctx.options.get("resolve_project_name") or "Resolve Script Session")
            return DCCMediaSessionInfo(
                dcc_key=self.key,
                display_name=self.display_name,
                mode="script_context",
                connected=True,
                session_label=proj_name,
            )
        st = self.bridge.get_state()
        if st.connected:
            mode_lbl = "Studio API" if st.mode == "studio_api" else "Lua Bridge"
            label = st.project_name or mode_lbl
            return DCCMediaSessionInfo(
                dcc_key=self.key,
                display_name=self.display_name,
                mode="live",
                connected=True,
                session_label=label,
            )
        return DCCMediaSessionInfo(
            dcc_key=self.key,
            display_name=self.display_name,
            mode="offline",
            connected=False,
            session_label="Offline",
        )

    @staticmethod
    def _resolve_clip(clip: dict[str, Any], ctx: DCCMediaContext) -> Optional[dict[str, Any]]:
        fpath_str = str(clip.get("file_path") or "").strip()
        if fpath_str:
            info = ctx.workspace.resolve_media_file_across_projects(
                Path(fpath_str),
                preferred_project_root=ctx.project_root,
            )
            if info is not None:
                info = dict(info)
                info["matched_by"] = "file_path"
                return info

        meta = clip.get("obol_meta")
        if isinstance(meta, dict) and meta:
            meta_fpath = str(meta.get("file_path") or "").strip()
            if meta_fpath:
                meta_root = Path(str(meta["project_root"])) if meta.get("project_root") else ctx.project_root
                info = ctx.workspace.resolve_media_file_across_projects(
                    Path(meta_fpath),
                    preferred_project_root=meta_root,
                )
                if info is not None:
                    info = dict(info)
                    info["matched_by"] = "obol_meta"
                    return info
        return None

    def scan_loaded_media(self, ctx: DCCMediaContext) -> DCCMediaScanResult:
        session = self.get_session_info(ctx)
        ctx_clips = ctx.options.get("context_clips")
        if isinstance(ctx_clips, list):
            clips = [dict(c) for c in ctx_clips if isinstance(c, dict)]
        elif session.connected:
            try:
                clips = self.bridge.query_media_pool()
            except Exception as exc:
                return DCCMediaScanResult(session=session, items=[], error=str(exc))
        else:
            return DCCMediaScanResult(session=session, items=[])

        items: list[LoadedMediaItem] = []
        seen_keys: set[tuple[str, str]] = set()
        for idx, clip in enumerate(clips):
            fpath_str = str(clip.get("file_path") or "").strip()
            media_id = str(clip.get("media_id") or "")
            bin_path = str(clip.get("bin_path") or "Master")
            clip_name = str(clip.get("name") or (Path(fpath_str).name if fpath_str else f"Clip {idx + 1}"))
            item_id = media_id or fpath_str or f"resolve:{idx}"
            dedup_key = (media_id or f"{bin_path}:{clip_name}", fpath_str)
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)

            resolved = self._resolve_clip(clip, ctx)
            if resolved is not None:
                items.append(
                    loaded_item_from_resolved_info(
                        dcc_key=self.key,
                        item_id=item_id,
                        display_name=clip_name,
                        location=bin_path,
                        media_type="Clip",
                        info=resolved,
                        raw_reference=fpath_str,
                        extra={"media_id": media_id, "bin_path": bin_path, "obol_meta": clip.get("obol_meta") or {}},
                    )
                )
            elif ctx.include_external and fpath_str:
                fp = Path(fpath_str).expanduser().resolve()
                exists = fp.is_file()
                items.append(
                    LoadedMediaItem(
                        dcc_key=self.key,
                        item_id=item_id,
                        display_name=clip_name,
                        location=bin_path,
                        media_type="Clip",
                        file_path=fp,
                        raw_reference=fpath_str,
                        tracked=False,
                        status=MediaStatus.EXTERNAL if exists else MediaStatus.MISSING,
                        matched_by="untracked",
                        can_switch_version=False,
                        can_collect=exists,
                        extra={"media_id": media_id, "bin_path": bin_path},
                    )
                )
        return DCCMediaScanResult(session=session, items=items)

    def apply_version_switches(
        self,
        switches: list[MediaSwitchRequest],
        ctx: DCCMediaContext,
    ) -> DCCMediaApplyResult:
        if not switches:
            return DCCMediaApplyResult(True, "No version changes selected.")

        upgraded, downgraded = _summarize_switch_counts(switches)
        replacements: dict[str, str] = {}
        clip_updates: list[dict[str, Any]] = []
        for sw in switches:
            curr_file_str = str(sw.item.file_path) if sw.item.file_path else sw.item.raw_reference
            target_file = Path(sw.target_file).resolve()
            if curr_file_str:
                replacements[curr_file_str] = str(target_file)
            meta = dict(sw.target_meta or build_asset_meta_dict(sw.item, sw.target_version, target_file, ctx))
            clip_updates.append({
                "media_id": str(sw.item.extra.get("media_id") or ""),
                "old_path": curr_file_str,
                "target_path": str(target_file),
                "asset_meta": meta,
            })

        if ctx.options.get("return_plan_only"):
            return DCCMediaApplyResult(
                ok=True,
                message=f"Prepared {len(switches)} Resolve clip replacement(s).",
                switched_count=len(switches),
                upgraded_count=upgraded,
                downgraded_count=downgraded,
                details={
                    "plan": {
                        "action": "replace_media_pool_clips",
                        "params": {
                            "replacements": replacements,
                            "clip_updates": clip_updates,
                        },
                    }
                },
            )

        res = self.bridge.replace_media_pool_clips(replacements, clip_updates=clip_updates)
        switched = int(res.get("replaced", 0))
        return DCCMediaApplyResult(
            ok=True,
            message=f"Replaced {switched} clip(s) in Resolve Media Pool ({upgraded} up, {downgraded} down).",
            switched_count=switched,
            upgraded_count=upgraded,
            downgraded_count=downgraded,
            details=res,
        )


class FilmCraftMediaAdapter:
    """Media management adapter for FilmCraft NLE (live --control bridge, headless CLI, or .fcproj JSON)."""

    key = "filmcraft"
    display_name = "FilmCraft"
    full_name = "FilmCraft"

    def __init__(self, bridge: FilmCraftBridgeClient) -> None:
        self.bridge = bridge

    @staticmethod
    def _list_project_fcproj_files(ctx: DCCMediaContext) -> list[tuple[str, str]]:
        if not ctx.project_root or not ctx.project_root.is_dir():
            return []
        comps = discover_project_components(ctx.project_root)
        if ctx.component_path and ctx.component_path in comps:
            comps = [ctx.component_path] + [c for c in comps if c != ctx.component_path]
        targets: list[tuple[str, str]] = []
        seen: set[Path] = set()
        for comp in comps:
            snap = load_component_snapshot(ctx.project_root, comp)
            for w in reversed(snap.working_versions):
                if not w.primary_file:
                    continue
                p = (ctx.project_root / comp / w.primary_file).resolve()
                if p.suffix.lower() == ".fcproj" and p.is_file() and p not in seen:
                    seen.add(p)
                    targets.append((f"{comp} — {w.version_label} ({p.name})", str(p)))
        return targets

    def _resolve_target_fcproj(self, ctx: DCCMediaContext, session: DCCMediaSessionInfo) -> tuple[Optional[Path], bool]:
        if ctx.target_file is not None and Path(ctx.target_file).is_file():
            return Path(ctx.target_file).resolve(), False
        if session.mode == "live":
            return None, True
        if session.project_file is not None and session.project_file.is_file():
            return session.project_file, False
        if session.available_targets:
            cand = Path(session.available_targets[0][1])
            if cand.is_file():
                return cand, False
        return None, False

    def get_session_info(self, ctx: DCCMediaContext) -> DCCMediaSessionInfo:
        targets = self._list_project_fcproj_files(ctx)
        if ctx.target_file is not None:
            tf = Path(ctx.target_file).resolve()
            return DCCMediaSessionInfo(
                dcc_key=self.key,
                display_name=self.display_name,
                mode="offline_project",
                connected=bool(self.bridge.resolve_cli_executable()),
                session_label=tf.name,
                project_file=tf,
                available_targets=targets,
            )
        if self.bridge.is_live_bridge_listening(timeout_s=0.05):
            return DCCMediaSessionInfo(
                dcc_key=self.key,
                display_name=self.display_name,
                mode="live",
                connected=True,
                session_label=f"Live :{self.bridge.control_port}",
                available_targets=targets,
            )
        if targets:
            first_p = Path(targets[0][1])
            return DCCMediaSessionInfo(
                dcc_key=self.key,
                display_name=self.display_name,
                mode="offline_project",
                connected=bool(self.bridge.resolve_cli_executable()),
                session_label=first_p.name,
                project_file=first_p,
                available_targets=targets,
            )
        return DCCMediaSessionInfo(
            dcc_key=self.key,
            display_name=self.display_name,
            mode="offline",
            connected=False,
            session_label="Offline",
            available_targets=targets,
        )

    def scan_loaded_media(self, ctx: DCCMediaContext) -> DCCMediaScanResult:
        session = self.get_session_info(ctx)
        target_fcproj, use_live = self._resolve_target_fcproj(ctx, session)
        if target_fcproj is None and not use_live:
            return DCCMediaScanResult(session=session, items=[])

        try:
            clips = self.bridge.list_media_items(project_path=target_fcproj, use_bridge=use_live)
        except Exception as exc:
            return DCCMediaScanResult(session=session, items=[], error=str(exc))

        items: list[LoadedMediaItem] = []
        for clip in clips:
            fpath_str = str(clip.get("file_path") or "").strip()
            if not fpath_str:
                continue
            item_id = str(clip.get("id") or fpath_str)
            bin_path = str(clip.get("bin_path") or "Root")
            clip_name = str(clip.get("name") or Path(fpath_str).name)
            mtype = str(clip.get("type") or "Media")
            raw_ref = str(clip.get("raw_path") or fpath_str)

            resolved = ctx.workspace.resolve_media_file_across_projects(
                Path(fpath_str),
                preferred_project_root=ctx.project_root,
            )
            if resolved is not None:
                resolved = dict(resolved)
                resolved["matched_by"] = "live_bridge" if use_live else ".fcproj"
                items.append(
                    loaded_item_from_resolved_info(
                        dcc_key=self.key,
                        item_id=item_id,
                        display_name=clip_name,
                        location=bin_path,
                        media_type=mtype,
                        info=resolved,
                        raw_reference=raw_ref,
                        extra={"fc_item_id": clip.get("id")},
                    )
                )
            elif ctx.include_external:
                fp = Path(fpath_str).expanduser().resolve()
                exists = fp.is_file() and not bool(clip.get("offline", False))
                items.append(
                    LoadedMediaItem(
                        dcc_key=self.key,
                        item_id=item_id,
                        display_name=clip_name,
                        location=bin_path,
                        media_type=mtype,
                        file_path=fp,
                        raw_reference=raw_ref,
                        tracked=False,
                        status=MediaStatus.EXTERNAL if exists else MediaStatus.MISSING,
                        matched_by="untracked",
                        can_switch_version=False,
                        can_collect=fp.is_file(),
                        extra={"fc_item_id": clip.get("id")},
                    )
                )
        return DCCMediaScanResult(session=session, items=items)

    def apply_version_switches(
        self,
        switches: list[MediaSwitchRequest],
        ctx: DCCMediaContext,
    ) -> DCCMediaApplyResult:
        if not switches:
            return DCCMediaApplyResult(True, "No version changes selected.")

        session = self.get_session_info(ctx)
        target_fcproj, use_live = self._resolve_target_fcproj(ctx, session)
        if target_fcproj is None and not use_live:
            return DCCMediaApplyResult(False, "No live FilmCraft session or working .fcproj file found.")

        upgraded, downgraded = _summarize_switch_counts(switches)
        replacements: dict[int, Path] = {}
        for sw in switches:
            raw_id = sw.item.extra.get("fc_item_id", sw.item.item_id)
            replacements[int(raw_id)] = Path(sw.target_file).resolve()

        res = self.bridge.replace_media_items(
            replacements,
            project_path=target_fcproj,
            use_bridge=use_live,
            keep_relative=True,
        )
        switched = int(res.get("replaced", 0))
        return DCCMediaApplyResult(
            ok=True,
            message=f"Replaced {switched} clip(s) in FilmCraft ({upgraded} up, {downgraded} down).",
            switched_count=switched,
            upgraded_count=upgraded,
            downgraded_count=downgraded,
            details=res,
        )


class DCCMediaRegistry:
    """Central registry of DCC media inspection and version-switching adapters."""

    def __init__(
        self,
        hub: ObolHub,
        reaper_bridge: Optional[ReaperBridgeClient] = None,
        resolve_bridge: Optional[ResolveBridgeClient] = None,
        filmcraft_bridge: Optional[FilmCraftBridgeClient] = None,
        allow_resolve_studio_api: bool = True,
    ) -> None:
        self.hub = hub
        self._adapters: dict[str, DCCMediaAdapter] = {}

        r_bridge = reaper_bridge or ReaperBridgeClient(hub)
        d_bridge = resolve_bridge or ResolveBridgeClient(hub, allow_studio_api=allow_resolve_studio_api)
        f_bridge = filmcraft_bridge or FilmCraftBridgeClient(hub)

        for builtin in (
            ReaperMediaAdapter(r_bridge),
            ResolveMediaAdapter(d_bridge),
            FilmCraftMediaAdapter(f_bridge),
        ):
            self.register(builtin)

        self._discover_plugin_adapters()

    def _discover_plugin_adapters(self) -> None:
        for plugin in self.hub.plugin_registry.list_plugins().values():
            candidate = None
            get_hook = getattr(plugin, "get_media_adapter", None)
            if callable(get_hook):
                try:
                    candidate = get_hook(self.hub)
                except Exception:
                    candidate = None
            else:
                candidate = getattr(plugin, "media_adapter", None)

            if (
                candidate is not None
                and hasattr(candidate, "key")
                and callable(getattr(candidate, "get_session_info", None))
                and callable(getattr(candidate, "scan_loaded_media", None))
                and callable(getattr(candidate, "apply_version_switches", None))
            ):
                self.register(candidate)

    def register(self, adapter: DCCMediaAdapter) -> None:
        self._adapters[str(adapter.key)] = adapter

    def unregister(self, key: str) -> None:
        self._adapters.pop(key, None)

    def get(self, key: str) -> Optional[DCCMediaAdapter]:
        return self._adapters.get(key)

    def list_adapters(self) -> list[DCCMediaAdapter]:
        return list(self._adapters.values())

    def scan(self, dcc_key: str, ctx: DCCMediaContext) -> DCCMediaScanResult:
        adapter = self.get(dcc_key)
        if adapter is None:
            empty_sess = DCCMediaSessionInfo(dcc_key=dcc_key, display_name=dcc_key)
            return DCCMediaScanResult(session=empty_sess, error=f"Unknown DCC media adapter '{dcc_key}'.")
        try:
            return adapter.scan_loaded_media(ctx)
        except Exception as exc:
            try:
                sess = adapter.get_session_info(ctx)
            except Exception:
                sess = DCCMediaSessionInfo(dcc_key=dcc_key, display_name=adapter.display_name)
            return DCCMediaScanResult(session=sess, error=str(exc))

    @staticmethod
    def build_switch_request(
        item: LoadedMediaItem,
        target_version: int,
        ctx: DCCMediaContext,
    ) -> Optional[MediaSwitchRequest]:
        choice = item.version_for(target_version)
        if choice is None:
            return None
        if choice.version == item.current_version and item.file_path and choice.file_path == item.file_path:
            return None
        meta = build_asset_meta_dict(item, choice.version, choice.file_path, ctx)
        return MediaSwitchRequest(
            item=item,
            target_version=choice.version,
            target_file=choice.file_path,
            target_meta=meta,
        )

    def apply_switches(
        self,
        dcc_key: str,
        switches: list[MediaSwitchRequest],
        ctx: DCCMediaContext,
    ) -> DCCMediaApplyResult:
        adapter = self.get(dcc_key)
        if adapter is None:
            return DCCMediaApplyResult(False, f"Unknown DCC media adapter '{dcc_key}'.")
        if not switches:
            return DCCMediaApplyResult(True, "No version changes selected.")
        try:
            return adapter.apply_version_switches(list(switches), ctx)
        except Exception as exc:
            return DCCMediaApplyResult(False, str(exc))
