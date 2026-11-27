"""Zero-dependency monochrome SVG icon generator and cache for Obol PySide6 GUI."""

from __future__ import annotations

import atexit
import re
import tempfile
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QSize
from PySide6.QtGui import QIcon

from obol.gui.tokens import TEXT_DISABLED, TEXT_SECONDARY

_ICON_CACHE_DIR: Optional[Path] = None
_SVG_PATH_CACHE: dict[tuple[str, str, int], str] = {}
_QICON_CACHE: dict[tuple[str, str, int, str, Optional[str]], QIcon] = {}


def _clear_icon_cache() -> None:
    """Clears cached QIcons before Shiboken tears down QApplication at interpreter exit."""
    _QICON_CACHE.clear()


atexit.register(_clear_icon_cache)

# Clean Lucide-style 24x24 stroke-2 SVG inner paths
_SVG_SHAPES: dict[str, str] = {
    "chevron_right": '<polyline points="9 18 15 12 9 6"></polyline>',
    "chevron_down": '<polyline points="6 9 12 15 18 9"></polyline>',
    "plus": '<line x1="12" y1="5" x2="12" y2="19"></line><line x1="5" y1="12" x2="19" y2="12"></line>',
    "refresh": (
        '<polyline points="23 4 23 10 17 10"></polyline>'
        '<polyline points="1 20 1 14 7 14"></polyline>'
        '<path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"></path>'
    ),
    "settings": (
        '<circle cx="12" cy="12" r="3"></circle>'
        '<path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06'
        'a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09'
        'A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06'
        'a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09'
        'A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06'
        'a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09'
        'a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06'
        'a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09'
        'a1.65 1.65 0 0 0-1.51 1z"></path>'
    ),
    "folder": (
        '<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"></path>'
    ),
    "copy": (
        '<rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect>'
        '<path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path>'
    ),
    "trash": (
        '<polyline points="3 6 5 6 21 6"></polyline>'
        '<path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"></path>'
    ),
    "search": (
        '<circle cx="11" cy="11" r="8"></circle>'
        '<line x1="21" y1="21" x2="16.65" y2="16.65"></line>'
    ),
    "bolt": '<polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"></polygon>',
    "lock": (
        '<rect x="3" y="11" width="18" height="11" rx="2" ry="2"></rect>'
        '<path d="M7 11V7a5 5 0 0 1 10 0v4"></path>'
    ),
    "star": (
        '<polygon points="12 2 15.09 8.26 22 9.27 17 14.14 18.18 21.02 '
        '12 17.77 5.82 21.02 7 14.14 2 9.27 8.91 8.26 12 2"></polygon>'
    ),
    "send": (
        '<line x1="22" y1="2" x2="11" y2="13"></line>'
        '<polygon points="22 2 15 22 11 13 2 9 22 2"></polygon>'
    ),
    "export": (
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>'
        '<polyline points="17 8 12 3 7 8"></polyline>'
        '<line x1="12" y1="3" x2="12" y2="15"></line>'
    ),
    "more_horiz": (
        '<circle cx="5" cy="12" r="1.6" fill="{color}" stroke="none"></circle>'
        '<circle cx="12" cy="12" r="1.6" fill="{color}" stroke="none"></circle>'
        '<circle cx="19" cy="12" r="1.6" fill="{color}" stroke="none"></circle>'
    ),
    "version_up": (
        '<circle cx="12" cy="12" r="10"></circle>'
        '<polyline points="16 12 12 8 8 12"></polyline>'
        '<line x1="12" y1="16" x2="12" y2="8"></line>'
    ),
    "ingest": (
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>'
        '<polyline points="7 10 12 15 17 10"></polyline>'
        '<line x1="12" y1="15" x2="12" y2="3"></line>'
    ),
    "publish": (
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>'
        '<polyline points="17 8 12 3 7 8"></polyline>'
        '<line x1="12" y1="3" x2="12" y2="15"></line>'
    ),
    "dcc_pull": (
        '<path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"></path>'
        '<polyline points="17 7 7 17"></polyline>'
        '<polyline points="15 17 7 17 7 9"></polyline>'
    ),
    "keyboard": (
        '<rect x="2" y="4" width="20" height="16" rx="2" ry="2"></rect>'
        '<line x1="6" y1="8" x2="6.01" y2="8"></line>'
        '<line x1="10" y1="8" x2="10.01" y2="8"></line>'
        '<line x1="14" y1="8" x2="14.01" y2="8"></line>'
        '<line x1="18" y1="8" x2="18.01" y2="8"></line>'
        '<line x1="6" y1="12" x2="6.01" y2="12"></line>'
        '<line x1="10" y1="12" x2="10.01" y2="12"></line>'
        '<line x1="14" y1="12" x2="14.01" y2="12"></line>'
        '<line x1="18" y1="12" x2="18.01" y2="12"></line>'
        '<line x1="7" y1="16" x2="17" y2="16"></line>'
    ),
    "info": (
        '<circle cx="12" cy="12" r="10"></circle>'
        '<line x1="12" y1="16" x2="12" y2="12"></line>'
        '<line x1="12" y1="8" x2="12.01" y2="8"></line>'
    ),
    "check_circle": (
        '<path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"></path>'
        '<polyline points="22 4 12 14.01 9 11.01"></polyline>'
    ),
    "x_circle": (
        '<circle cx="12" cy="12" r="10"></circle>'
        '<line x1="15" y1="9" x2="9" y2="15"></line>'
        '<line x1="9" y1="9" x2="15" y2="15"></line>'
    ),
    "check": '<polyline points="20 6 9 17 4 12"></polyline>',
    "warning": (
        '<path d="M10.29 3.86L1.82 18a2 2 0 0 0 1.71 3h16.94a2 2 0 0 0 1.71-3L13.71 3.86a2 2 0 0 0-3.42 0z"></path>'
        '<line x1="12" y1="9" x2="12" y2="13"></line>'
        '<line x1="12" y1="17" x2="12.01" y2="17"></line>'
    ),
    "sliders": (
        '<line x1="4" y1="21" x2="4" y2="14"></line><line x1="4" y1="10" x2="4" y2="3"></line>'
        '<line x1="12" y1="21" x2="12" y2="12"></line><line x1="12" y1="8" x2="12" y2="3"></line>'
        '<line x1="20" y1="21" x2="20" y2="16"></line><line x1="20" y1="12" x2="20" y2="3"></line>'
        '<line x1="1" y1="14" x2="7" y2="14"></line><line x1="9" y1="8" x2="15" y2="8"></line>'
        '<line x1="17" y1="16" x2="23" y2="16"></line>'
    ),
    "layers": (
        '<polygon points="12 2 2 7 12 12 22 7 12 2"></polygon>'
        '<polyline points="2 17 12 22 22 17"></polyline>'
        '<polyline points="2 12 12 17 22 12"></polyline>'
    ),
    "file_text": (
        '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"></path>'
        '<polyline points="14 2 14 8 20 8"></polyline>'
        '<line x1="16" y1="13" x2="8" y2="13"></line>'
        '<line x1="16" y1="17" x2="8" y2="17"></line>'
    ),
    "database": (
        '<ellipse cx="12" cy="5" rx="9" ry="3"></ellipse>'
        '<path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3"></path>'
        '<path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5"></path>'
    ),
    "play": '<polygon points="5 3 19 12 5 21 5 3"></polygon>',
    "external_link": (
        '<path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"></path>'
        '<polyline points="15 3 21 3 21 9"></polyline>'
        '<line x1="10" y1="14" x2="21" y2="3"></line>'
    ),
    "clipboard": (
        '<path d="M16 4h2a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V6a2 2 0 0 1 2-2h2"></path>'
        '<rect x="8" y="2" width="8" height="4" rx="1" ry="1"></rect>'
    ),
    "x": '<line x1="18" y1="6" x2="6" y2="18"></line><line x1="6" y1="6" x2="18" y2="18"></line>',
}


def _ensure_icon_dir() -> Path:
    global _ICON_CACHE_DIR
    if _ICON_CACHE_DIR is None or not _ICON_CACHE_DIR.exists():
        _ICON_CACHE_DIR = Path(tempfile.gettempdir()) / "obol_gui_icons"
        _ICON_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    return _ICON_CACHE_DIR


def get_svg_path(name: str, color: str = TEXT_SECONDARY, size: int = 16) -> str:
    """Returns a POSIX path to a cached monochrome SVG icon file for QSS/QIcon usage."""
    sz = max(8, int(size))
    key = (name, color, sz)
    cached = _SVG_PATH_CACHE.get(key)
    if cached and Path(cached).is_file():
        return cached

    shape = _SVG_SHAPES.get(name, _SVG_SHAPES["check"]).replace("{color}", color)
    safe_color = re.sub(r"[^a-zA-Z0-9]", "", color) or "default"
    icon_dir = _ensure_icon_dir()
    svg_file = icon_dir / f"{name}_{safe_color}_{sz}.svg"
    svg_markup = (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{sz}" height="{sz}" viewBox="0 0 24 24" '
        f'fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">'
        f"{shape}</svg>"
    )
    svg_file.write_text(svg_markup, encoding="utf-8")
    posix_path = svg_file.as_posix()
    _SVG_PATH_CACHE[key] = posix_path
    return posix_path


def get_icon(
    name: str,
    color: str = TEXT_SECONDARY,
    size: int = 16,
    disabled_color: str = TEXT_DISABLED,
    active_color: Optional[str] = None,
) -> QIcon:
    """Returns a cached multi-state QIcon backed by crisp monochrome SVGs."""
    sz = max(8, int(size))
    key = (name, color, sz, disabled_color, active_color)
    cached = _QICON_CACHE.get(key)
    if cached is not None:
        return cached
    qsize = QSize(sz, sz)
    icon = QIcon()
    icon.addFile(get_svg_path(name, color=color, size=sz), qsize, QIcon.Mode.Normal, QIcon.State.Off)
    icon.addFile(
        get_svg_path(name, color=disabled_color, size=sz),
        qsize,
        QIcon.Mode.Disabled,
        QIcon.State.Off,
    )
    if active_color:
        icon.addFile(
            get_svg_path(name, color=active_color, size=sz),
            qsize,
            QIcon.Mode.Active,
            QIcon.State.Off,
        )
    _QICON_CACHE[key] = icon
    return icon
