"""Pure-Python TOML serializer compatible with standard library tomllib."""

from __future__ import annotations

import re
from enum import Enum
from typing import Any

BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _format_key(key: str) -> str:
    s = str(key)
    if s and BARE_KEY_RE.match(s):
        return s
    escaped = s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def _format_scalar(val: Any) -> str:
    if isinstance(val, Enum):
        val = val.value
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, int):
        return str(val)
    if isinstance(val, float):
        return repr(val)
    if isinstance(val, str):
        escaped = (
            val.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
            .replace("\t", "\\t")
        )
        return f'"{escaped}"'
    if isinstance(val, (list, tuple)):
        items = [_format_scalar(x) for x in val if x is not None]
        return "[" + ", ".join(items) + "]"
    if isinstance(val, dict):
        pairs = [f"{_format_key(k)} = {_format_scalar(v)}" for k, v in val.items() if v is not None]
        return "{ " + ", ".join(pairs) + " }"
    return _format_scalar(str(val))


def _is_array_of_tables(val: Any) -> bool:
    return isinstance(val, list) and len(val) > 0 and all(isinstance(x, dict) for x in val)


def dumps_toml(data: dict[str, Any]) -> str:
    """Serializes a nested Python dict to a valid TOML document string."""
    lines: list[str] = []

    def _emit_table(table: dict[str, Any], path: list[str]) -> None:
        scalars: list[tuple[str, Any]] = []
        subtables: list[tuple[str, dict[str, Any]]] = []
        table_arrays: list[tuple[str, list[dict[str, Any]]]] = []

        for k, v in table.items():
            if v is None:
                continue
            if isinstance(v, dict):
                subtables.append((str(k), v))
            elif _is_array_of_tables(v):
                table_arrays.append((str(k), v))
            else:
                scalars.append((str(k), v))

        if path and (scalars or (not subtables and not table_arrays)):
            header = ".".join(_format_key(p) for p in path)
            lines.append(f"[{header}]")

        for k, v in scalars:
            lines.append(f"{_format_key(k)} = {_format_scalar(v)}")

        if scalars and (subtables or table_arrays):
            lines.append("")

        for k, arr in table_arrays:
            arr_path = path + [k]
            header = ".".join(_format_key(p) for p in arr_path)
            for item in arr:
                lines.append(f"[[{header}]]")
                item_scalars = [(ik, iv) for ik, iv in item.items() if iv is not None and not isinstance(iv, dict) and not _is_array_of_tables(iv)]
                item_subtables = [(ik, iv) for ik, iv in item.items() if isinstance(iv, dict)]
                item_arrays = [(ik, iv) for ik, iv in item.items() if _is_array_of_tables(iv)]
                for ik, iv in item_scalars:
                    lines.append(f"{_format_key(ik)} = {_format_scalar(iv)}")
                for ik, iv in item_arrays:
                    # Inline array of inline tables must be emitted before any subtable headers
                    lines.append(f"{_format_key(ik)} = {_format_scalar(iv)}")
                for ik, iv in item_subtables:
                    if iv:
                        lines.append("")
                        _emit_table(iv, arr_path + [str(ik)])
                lines.append("")

        for idx, (k, sub) in enumerate(subtables):
            _emit_table(sub, path + [k])
            if idx < len(subtables) - 1:
                lines.append("")

    _emit_table(data, [])
    return "\n".join(lines).strip() + "\n"
