"""A small SQLite catalogue; source bytes are never stored here."""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import __version__
from .errors import MediaLabError

APPLICATION_ID = 0x4D4C4142  # MLAB

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    media_id TEXT PRIMARY KEY,
    checksum TEXT NOT NULL UNIQUE,
    size_bytes INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    probe_json TEXT NOT NULL,
    probe_version TEXT NOT NULL,
    processing_version TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS locations (
    path TEXT PRIMARY KEY,
    media_id TEXT NOT NULL REFERENCES media(media_id),
    fingerprint TEXT NOT NULL,
    verified_at TEXT NOT NULL,
    status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS import_events (
    event_id INTEGER PRIMARY KEY,
    path TEXT NOT NULL,
    media_id TEXT REFERENCES media(media_id),
    outcome TEXT NOT NULL,
    detail TEXT,
    occurred_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Catalogue:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        try:
            self.connection.execute("PRAGMA foreign_keys = ON")
            version = self.connection.execute("PRAGMA user_version").fetchone()[0]
            if version not in (0, 1):
                raise MediaLabError("catalogue_version", "This catalogue needs a different Media Lab version.")
            app_id = self.connection.execute("PRAGMA application_id").fetchone()[0]
            tables = self.connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app_id != APPLICATION_ID and (app_id != 0 or tables or version != 0):
                raise MediaLabError("unrelated_database", "This file is not a Media Lab catalogue. Choose a new database path.")
            if version == 0:
                # Explicit transaction also makes initial schema creation restart-safe.
                self.connection.executescript(
                    "BEGIN IMMEDIATE;\n" + SCHEMA
                    + f"\nPRAGMA application_id={APPLICATION_ID};\nPRAGMA user_version=1;\nCOMMIT;"
                )
        except BaseException:
            self.connection.close()
            raise

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def location(self, path: Path):
        return self.connection.execute("SELECT * FROM locations WHERE path = ?", (str(path),)).fetchone()

    def media(self, checksum: str):
        return self.connection.execute("SELECT * FROM media WHERE checksum = ?", (checksum,)).fetchone()

    def event(self, path: Path, media_id: str | None, outcome: str, detail: str | None = None):
        self.connection.execute(
            "INSERT INTO import_events(path, media_id, outcome, detail, occurred_at) VALUES (?, ?, ?, ?, ?)",
            (str(path), media_id, outcome, detail, now()),
        )

    def record(self, path: Path, checksum: str, signature: str, metadata: dict | None,
               raw: dict | None, probe_version: str | None, outcome: str) -> str:
        media_id = "sha256:" + checksum
        with self.connection:
            if metadata is not None:
                self.connection.execute(
                    "INSERT OR IGNORE INTO media VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (media_id, checksum, json.loads(signature)[2], json.dumps(metadata),
                     json.dumps(raw), probe_version, __version__, now()),
                )
            self.connection.execute(
                "INSERT INTO locations VALUES (?, ?, ?, ?, ?) ON CONFLICT(path) DO UPDATE SET "
                "media_id=excluded.media_id, fingerprint=excluded.fingerprint, "
                "verified_at=excluded.verified_at, status=excluded.status",
                (str(path), media_id, signature, now(), "verified"),
            )
            self.event(path, media_id, outcome)
        return media_id

    def failed(self, path: Path, error: MediaLabError):
        with self.connection:
            previous = self.location(path)
            # A failed read must not leave the old location looking freshly verified.
            self.connection.execute("UPDATE locations SET status = ? WHERE path = ?", (error.code, str(path)))
            self.event(path, previous["media_id"] if previous else None, error.code, str(error))

    def search(self, query: str = "") -> list[dict]:
        # Literal substring search: SQL LIKE wildcard characters have no special meaning.
        rows = self.connection.execute(
            "SELECT m.* FROM media m WHERE instr(lower(m.media_id), lower(?)) > 0 OR EXISTS "
            "(SELECT 1 FROM locations l WHERE l.media_id=m.media_id AND instr(lower(l.path), lower(?)) > 0) "
            "ORDER BY m.created_at, m.media_id", (query, query),
        ).fetchall()
        records = []
        for row in rows:
            locations = self.connection.execute(
                "SELECT path, verified_at, status FROM locations WHERE media_id = ? ORDER BY path",
                (row["media_id"],),
            ).fetchall()
            records.append({
                "media_id": row["media_id"], "sha256": row["checksum"],
                "size_bytes": row["size_bytes"], "metadata": json.loads(row["metadata_json"]),
                "locations": [dict(location) for location in locations],
                "probe_version": row["probe_version"], "processing_version": row["processing_version"],
                "catalogued_at": row["created_at"],
            })
        return records
