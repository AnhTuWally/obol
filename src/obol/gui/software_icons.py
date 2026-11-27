"""Resolves real application icons for SoftwareProfiles (exe icon, custom icon, or OS file-type icon)."""

from __future__ import annotations

import atexit
from pathlib import Path
from typing import Optional, Union

from PySide6.QtCore import QFileInfo, QSize
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QFileIconProvider

from obol.core.models import SoftwareProfile

_IMAGE_SUFFIXES = {".ico", ".png", ".svg", ".jpg", ".jpeg", ".bmp", ".icns"}
_CACHE: dict[tuple[str, float], Optional[QIcon]] = {}
_PROVIDER: Optional[QFileIconProvider] = None

PathLike = Union[str, Path]


def _clear_cache() -> None:
    """Drops cached QIcons before Shiboken tears down QApplication at interpreter exit."""
    global _PROVIDER
    _CACHE.clear()
    _PROVIDER = None


atexit.register(_clear_cache)


def _provider() -> QFileIconProvider:
    global _PROVIDER
    if _PROVIDER is None:
        _PROVIDER = QFileIconProvider()
    return _PROVIDER


def _usable(icon: Optional[QIcon]) -> bool:
    return icon is not None and not icon.isNull() and not icon.pixmap(QSize(16, 16)).isNull()


def _resolve_path(raw: Optional[PathLike]) -> Optional[Path]:
    if not raw:
        return None
    try:
        p = Path(str(raw).strip().strip('"')).expanduser()
        return p if p.exists() else None
    except (OSError, ValueError):
        return None


def icon_for_path(raw: Optional[PathLike]) -> Optional[QIcon]:
    """Returns a usable icon for an image file, or the shell icon for an exe/app/document; cached by (path, mtime)."""
    p = _resolve_path(raw)
    if p is None or QApplication.instance() is None:
        return None
    try:
        key = (str(p.resolve()), p.stat().st_mtime)
    except OSError:
        return None
    if key in _CACHE:
        return _CACHE[key]

    icon: Optional[QIcon] = None
    try:
        if p.is_file() and p.suffix.lower() in _IMAGE_SUFFIXES:
            icon = QIcon(str(p))
        else:
            icon = _provider().icon(QFileInfo(str(p)))
    except Exception:  # pragma: no cover - shell/icon backends are best-effort
        icon = None
    if not _usable(icon):
        icon = None
    _CACHE[key] = icon
    return icon


def resolve_software_icon(
    profile: Optional[SoftwareProfile],
    sample_file: Optional[PathLike] = None,
) -> Optional[QIcon]:
    """profile.icon -> profile.executable -> sample_file OS association. Returns None when nothing resolves."""
    if profile is not None:
        for candidate in (getattr(profile, "icon", ""), profile.executable):
            icon = icon_for_path(candidate)
            if icon is not None:
                return icon
    return icon_for_path(sample_file)


def get_software_icon(
    profile: Optional[SoftwareProfile],
    sample_file: Optional[PathLike] = None,
    fallback: Optional[QIcon] = None,
) -> QIcon:
    """Like resolve_software_icon but always returns a QIcon (fallback or an empty QIcon)."""
    icon = resolve_software_icon(profile, sample_file)
    if icon is not None:
        return icon
    return fallback if fallback is not None else QIcon()


def has_app_icon(profile: Optional[SoftwareProfile], sample_file: Optional[PathLike] = None) -> bool:
    return resolve_software_icon(profile, sample_file) is not None
