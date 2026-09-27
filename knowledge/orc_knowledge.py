"""Local, provider-neutral SQLite text collection. Python standard library only.

No PDF/EPUB parsing, embedding calls, model answer generation or web requests.
The Plan's older catalogue schema is supported for read-only inspect/search.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import re
import sqlite3
import sys
from pathlib import Path

SCHEMA_VERSION = "orc-text-1"
SOURCE_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,79}$")
# Exclude underscores: they are SQL LIKE wildcards but not useful search words.
WORD = re.compile(r"[^\W_]+", re.UNICODE)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _open_read(db_path: Path) -> sqlite3.Connection:
    path = Path(db_path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("database_must_be_file")
    db = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA query_only=ON")
    return db


def _tables(db: sqlite3.Connection) -> set[str]:
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(db: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in db.execute(f"PRAGMA table_info({table})")}


def _schema(db: sqlite3.Connection) -> str:
    tables = _tables(db)
    if not {"sources", "chunks"} <= tables:
        raise ValueError("unsupported_catalogue")
    columns = _columns(db, "chunks")
    if not {"chunk_id", "source_id", "text", "breadcrumb"} <= columns:
        raise ValueError("unsupported_catalogue")
    if "catalog_meta" in tables:
        row = db.execute("SELECT value FROM catalog_meta WHERE key='catalog_schema_version'").fetchone()
        if row:
            return "legacy-" + row[0]
    if "orc_meta" in tables:
        row = db.execute("SELECT value FROM orc_meta WHERE key='schema_version'").fetchone()
        if row and row[0] == SCHEMA_VERSION:
            return SCHEMA_VERSION
    raise ValueError("unsupported_catalogue")


def create(db_path: Path) -> None:
    path = Path(db_path)
    if path.exists():
        raise FileExistsError("database_exists")
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        db.executescript("""
            CREATE TABLE orc_meta(key TEXT PRIMARY KEY, value TEXT NOT NULL);
            INSERT INTO orc_meta VALUES('schema_version','orc-text-1');
            CREATE TABLE sources(
                source_id TEXT PRIMARY KEY, book_title TEXT NOT NULL,
                author TEXT NOT NULL, source_file_sha256 TEXT NOT NULL,
                source_format TEXT NOT NULL
            );
            CREATE TABLE chunks(
                chunk_id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL REFERENCES sources(source_id),
                ordinal INTEGER NOT NULL,
                breadcrumb TEXT NOT NULL, document_path TEXT NOT NULL,
                chapter_title TEXT, first_page_number INTEGER,
                last_page_number INTEGER, source_file_sha256 TEXT NOT NULL,
                body_sha256 TEXT NOT NULL, text TEXT NOT NULL,
                retrieval_eligible INTEGER NOT NULL CHECK(retrieval_eligible IN(0,1)),
                UNIQUE(source_id, ordinal)
            );
            CREATE INDEX chunks_source ON chunks(source_id);
        """)
        try:
            db.execute("CREATE VIRTUAL TABLE chunk_fts USING fts5(chunk_id UNINDEXED, breadcrumb, text)")
        except sqlite3.OperationalError as exc:
            if "fts5" not in str(exc).lower():
                raise


def _read_source(source: Path) -> tuple[bytes, str]:
    source = Path(source)
    if source.is_symlink() or not source.is_file():
        raise ValueError("source_must_be_regular_file")
    if source.suffix.lower() not in {".txt", ".md"}:
        raise ValueError("only_utf8_txt_or_md_supported")
    raw = source.read_bytes()
    if not raw or len(raw) > 20_000_000:
        raise ValueError("source_empty_or_too_large")
    return raw, raw.decode("utf-8-sig", errors="strict")


def _chunk_text(content: str, limit: int = 900) -> list[str]:
    paragraphs = [re.sub(r"\s+", " ", p).strip() for p in re.split(r"\n\s*\n", content)]
    paragraphs = [p for p in paragraphs if p]
    result: list[str] = []
    for paragraph in paragraphs:
        words = paragraph.split(" ")
        current: list[str] = []
        size = 0
        for word in words:
            if len(word) > limit:
                raise ValueError("single_word_exceeds_chunk_limit")
            if current and size + len(word) + 1 > limit:
                result.append(" ".join(current))
                current, size = [], 0
            current.append(word)
            size += len(word) + (1 if size else 0)
        if current:
            result.append(" ".join(current))
    if not result:
        raise ValueError("source_has_no_text")
    return result


def import_text(db_path: Path, source: Path, source_id: str, title: str,
                author: str = "", chapter: str = "", page: int | None = None) -> dict:
    if not SOURCE_ID.fullmatch(source_id):
        raise ValueError("invalid_source_id")
    if not title.strip() or page is not None and page < 1:
        raise ValueError("invalid_title_or_page")
    raw, content = _read_source(source)
    source_hash = digest(raw)
    pieces = _chunk_text(content)
    breadcrumb = " / ".join(part for part in (title, chapter) if part)
    chunk_rows = [
        (f"{source_id}:{index:05d}", source_id, index, breadcrumb, source.name,
         chapter or None, page, page, source_hash,
         digest(body.encode("utf-8")), body, 1)
        for index, body in enumerate(pieces, 1)
    ]
    db_file = Path(db_path).resolve(strict=True)
    if not db_file.is_file():
        raise ValueError("database_must_be_file")
    with closing(sqlite3.connect(db_file.as_uri() + "?mode=rw", uri=True)) as db, db:
        db.execute("PRAGMA foreign_keys=ON")
        if _schema(db) != SCHEMA_VERSION:
            raise ValueError("imports_require_orc_schema")
        existing = db.execute("SELECT source_file_sha256,book_title,author,source_format FROM sources WHERE source_id=?", (source_id,)).fetchone()
        if existing:
            stored_rows = db.execute(
                "SELECT chunk_id,source_id,ordinal,breadcrumb,document_path,chapter_title,"
                "first_page_number,last_page_number,source_file_sha256,body_sha256,text,"
                "retrieval_eligible FROM chunks WHERE source_id=? ORDER BY ordinal",
                (source_id,),
            ).fetchall()
            if (tuple(existing) == (source_hash, title, author, source.suffix.lower().lstrip("."))
                    and stored_rows == chunk_rows):
                return {"source_id": source_id, "source_sha256": source_hash, "chunks": len(chunk_rows), "status": "unchanged"}
            raise ValueError("source_id_conflict")
        db.execute("INSERT INTO sources VALUES(?,?,?,?,?)", (source_id, title, author, source_hash, source.suffix.lower().lstrip(".")))
        has_fts = "chunk_fts" in _tables(db)
        for row in chunk_rows:
            db.execute("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", row)
            if has_fts:
                db.execute("INSERT INTO chunk_fts(chunk_id,breadcrumb,text) VALUES(?,?,?)", (row[0], row[3], row[10]))
    return {"source_id": source_id, "source_sha256": source_hash, "chunks": len(pieces), "status": "imported"}


def inspect(db_path: Path) -> dict:
    with closing(_open_read(db_path)) as db:
        version = _schema(db)
        tables = _tables(db)
        columns = _columns(db, "chunks")
        result = {
            "schema": version,
            "sources": db.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "chunks": db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0],
            "fts_available": "chunk_fts" in tables,
            "text_characters": db.execute("SELECT COALESCE(SUM(LENGTH(text)),0) FROM chunks").fetchone()[0],
        }
        if "retrieval_eligible" in columns:
            result["eligible_chunks"] = db.execute("SELECT COUNT(*) FROM chunks WHERE retrieval_eligible=1").fetchone()[0]
        if "embeddings" in tables:
            result["embeddings"] = db.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0]
        return result


def search(db_path: Path, query: str, limit: int = 5) -> list[dict]:
    terms = [term.lower() for term in WORD.findall(query) if len(term) > 1][:12]
    if not 1 <= limit <= 50:
        raise ValueError("invalid_query_or_limit")
    with closing(_open_read(db_path)) as db:
        _schema(db)
        if not terms:
            return []
        cols = _columns(db, "chunks")
        eligible = " AND c.retrieval_eligible=1" if "retrieval_eligible" in cols else ""
        # Quote terms as FTS literals; no raw MATCH syntax from the caller.
        match = " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
        base = "SELECT c.*, s.book_title AS source_title, s.author AS source_author FROM chunks c JOIN sources s ON s.source_id=c.source_id"
        rows = []
        if "chunk_fts" in _tables(db):
            try:
                rows = db.execute(base + " JOIN chunk_fts f ON f.chunk_id=c.chunk_id WHERE chunk_fts MATCH ?" + eligible + " ORDER BY bm25(chunk_fts) LIMIT ?", (match, limit)).fetchall()
            except sqlite3.OperationalError:
                rows = []
        if not rows:
            condition = " OR ".join("LOWER(c.text) LIKE ? ESCAPE '!'" for _ in terms)
            escaped = ["%" + t.replace("!", "!!").replace("%", "!%").replace("_", "!_") + "%" for t in terms]
            rows = db.execute(base + " WHERE (" + condition + ")" + eligible + " ORDER BY c.source_id,c.chunk_id LIMIT ?", (*escaped, limit)).fetchall()
        results = []
        for row in rows:
            keys = row.keys()
            locator = {
                "source_id": row["source_id"], "chunk_id": row["chunk_id"],
                "title": row["source_title"], "author": row["source_author"],
                "breadcrumb": row["breadcrumb"],
                "document_path": row["document_path"] if "document_path" in keys else row["epub_path"] if "epub_path" in keys else None,
                "chapter": row["chapter_title"] if "chapter_title" in keys else None,
                "first_page": row["first_page_number"] if "first_page_number" in keys else None,
                "last_page": row["last_page_number"] if "last_page_number" in keys else None,
                "source_sha256": row["source_file_sha256"] if "source_file_sha256" in keys else None,
                "text": row["text"],
            }
            if "fitness_provenance" in _tables(db):
                provenance = db.execute("SELECT file_sha256,first_page_number,last_page_number FROM fitness_provenance WHERE chunk_id=?", (row["chunk_id"],)).fetchone()
                if provenance:
                    locator["source_sha256"], locator["first_page"], locator["last_page"] = provenance
            results.append(locator)
        return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("init", "inspect", "search", "import-text"):
        p = sub.add_parser(command)
        p.add_argument("--db", required=True, type=Path)
        if command == "search":
            p.add_argument("--query", required=True)
            p.add_argument("--limit", type=int, default=5)
        if command == "import-text":
            p.add_argument("--source", required=True, type=Path)
            p.add_argument("--source-id", required=True)
            p.add_argument("--title", required=True)
            p.add_argument("--author", default="")
            p.add_argument("--chapter", default="")
            p.add_argument("--page", type=int)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            create(args.db)
            result = inspect(args.db)
        elif args.command == "inspect":
            result = inspect(args.db)
        elif args.command == "search":
            result = search(args.db, args.query, args.limit)
        else:
            result = import_text(args.db, args.source, args.source_id, args.title, args.author, args.chapter, args.page)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, FileExistsError, FileNotFoundError, UnicodeError, sqlite3.Error) as exc:
        print(f"knowledge_error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
