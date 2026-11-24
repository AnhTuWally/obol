"""Disposable local-only SQLite query cache (index.db) rebuilt from component snapshots."""

from __future__ import annotations

import contextlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from obol.core.models import ComponentSnapshot
from obol.core.storage import discover_project_components, load_component_snapshot


class ProjectIndex:
    """Manages <project_root>/.obol/index.db as a fast, disposable read cache."""

    def __init__(self, project_root: Path) -> None:
        self.project_root = project_root
        self.db_path = project_root / ".obol" / "index.db"

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        return conn

    def _init_schema(self, conn: sqlite3.Connection) -> None:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS components (
                component_path TEXT PRIMARY KEY,
                task_template TEXT,
                software TEXT,
                latest_working_ver INTEGER,
                latest_published_ver INTEGER,
                repo_asset_count INTEGER
            );
            CREATE TABLE IF NOT EXISTS versions (
                component_path TEXT,
                triad_type TEXT,
                asset_path TEXT,
                version INTEGER,
                folder_rel TEXT,
                primary_file TEXT,
                created_at TEXT,
                comment TEXT,
                source_working_version INTEGER,
                PRIMARY KEY (component_path, triad_type, asset_path, version)
            );
            CREATE TABLE IF NOT EXISTS repo_assets (
                component_path TEXT,
                asset_path TEXT,
                name TEXT,
                category TEXT,
                author TEXT,
                source TEXT,
                license TEXT,
                tags_json TEXT,
                custom_json TEXT,
                latest_version INTEGER,
                used_by_json TEXT,
                PRIMARY KEY (component_path, asset_path)
            );
            """
        )

    def index_component(self, snapshot: ComponentSnapshot) -> None:
        try:
            with contextlib.closing(self._connect()) as conn:
                with conn:
                    self._init_schema(conn)
                    self._write_component(conn, snapshot)
        except sqlite3.DatabaseError:
            self.rebuild_all()

    def _write_component(self, conn: sqlite3.Connection, snapshot: ComponentSnapshot) -> None:
        cpath = snapshot.component_path
        conn.execute("DELETE FROM components WHERE component_path = ?", (cpath,))
        conn.execute("DELETE FROM versions WHERE component_path = ?", (cpath,))
        conn.execute("DELETE FROM repo_assets WHERE component_path = ?", (cpath,))

        latest_w = snapshot.working_versions[-1].version if snapshot.working_versions else 0
        latest_p = max(
            [snapshot.published_versions[-1].version if snapshot.published_versions else 0]
            + [d.latest_version for d in snapshot.published_deliverables.values()],
            default=0,
        )
        conn.execute(
            """
            INSERT INTO components (
                component_path, task_template, software, latest_working_ver, latest_published_ver, repo_asset_count
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (cpath, snapshot.task_template, snapshot.software, latest_w, latest_p, len(snapshot.repo_assets)),
        )

        for w in snapshot.working_versions:
            conn.execute(
                """
                INSERT INTO versions (
                    component_path, triad_type, asset_path, version, folder_rel, primary_file, created_at, comment, source_working_version
                ) VALUES (?, 'working', '', ?, ?, ?, ?, ?, 0)
                """,
                (cpath, w.version, w.folder_rel, w.primary_file, w.created_at, w.comment),
            )

        for p in snapshot.published_versions:
            conn.execute(
                """
                INSERT INTO versions (
                    component_path, triad_type, asset_path, version, folder_rel, primary_file, created_at, comment, source_working_version
                ) VALUES (?, 'published', '', ?, ?, ?, ?, ?, ?)
                """,
                (cpath, p.version, p.folder_rel, p.primary_file, p.created_at, p.comment, p.source_working_version),
            )

        for dpath, deliv in snapshot.published_deliverables.items():
            for pv in deliv.versions:
                conn.execute(
                    """
                    INSERT INTO versions (
                        component_path, triad_type, asset_path, version, folder_rel, primary_file, created_at, comment, source_working_version
                    ) VALUES (?, 'published', ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (cpath, dpath, pv.version, pv.folder_rel, pv.primary_file, pv.created_at, pv.comment, pv.source_working_version),
                )

        for apath, asset in snapshot.repo_assets.items():
            conn.execute(
                """
                INSERT INTO repo_assets (
                    component_path, asset_path, name, category, author, source, license, tags_json, custom_json, latest_version, used_by_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    cpath,
                    apath,
                    asset.name,
                    asset.category,
                    asset.author,
                    asset.source,
                    asset.license,
                    json.dumps(asset.tags),
                    json.dumps(asset.custom),
                    asset.latest_version,
                    json.dumps(asset.used_by_working_versions),
                ),
            )
            for rv in asset.versions:
                conn.execute(
                    """
                    INSERT INTO versions (
                        component_path, triad_type, asset_path, version, folder_rel, primary_file, created_at, comment, source_working_version
                    ) VALUES (?, 'repo', ?, ?, ?, ?, ?, ?, 0)
                    """,
                    (cpath, apath, rv.version, rv.folder_rel, rv.primary_file, rv.created_at, rv.comment),
                )

    def rebuild_all(self) -> int:
        """Deletes and reconstructs index.db from all component snapshots and disk folders."""
        if self.db_path.exists():
            try:
                self.db_path.unlink()
            except OSError:
                pass
        for suffix in ("-wal", "-shm"):
            wal = self.db_path.with_name(self.db_path.name + suffix)
            if wal.exists():
                try:
                    wal.unlink()
                except OSError:
                    pass

        components = discover_project_components(self.project_root)
        with contextlib.closing(self._connect()) as conn:
            with conn:
                self._init_schema(conn)
                for comp in components:
                    snap = load_component_snapshot(self.project_root, comp)
                    self._write_component(conn, snap)
        return len(components)

    def search_repo_assets(self, query: str = "", author: str = "", license_filter: str = "") -> list[dict[str, Any]]:
        """Queries the SQLite index for repo assets across all components, healing if DB is missing."""
        if not self.db_path.exists():
            self.rebuild_all()
        try:
            with contextlib.closing(self._connect()) as conn:
                with conn:
                    self._init_schema(conn)
                    sql = "SELECT * FROM repo_assets WHERE 1=1"
                    params: list[Any] = []
                    if query:
                        sql += " AND (name LIKE ? OR asset_path LIKE ? OR tags_json LIKE ? OR source LIKE ?)"
                        q = f"%{query}%"
                        params.extend([q, q, q, q])
                    if author:
                        sql += " AND author LIKE ?"
                        params.append(f"%{author}%")
                    if license_filter:
                        sql += " AND license LIKE ?"
                        params.append(f"%{license_filter}%")
                    sql += " ORDER BY component_path, asset_path"
                    rows = conn.execute(sql, params).fetchall()
                    return [dict(r) for r in rows]
        except sqlite3.DatabaseError:
            self.rebuild_all()
            return self.search_repo_assets(query=query, author=author, license_filter=license_filter)
