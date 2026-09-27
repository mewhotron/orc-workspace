"""Run from the project folder: python -m media_lab --help."""

import argparse
import json
import math
import os
import sqlite3
import sys
from pathlib import Path

from . import __version__
from .catalogue import Catalogue
from .errors import MediaLabError
from .identity import source_path
from .ingest import ingest, verify
from .probe import FFprobe, PROJECT_ROOT


def positive_seconds(value: str) -> float:
    seconds = float(value)
    if not math.isfinite(seconds) or seconds <= 0:
        raise argparse.ArgumentTypeError("Choose a positive, finite timeout.")
    return seconds


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Media Lab: local catalogue; original files stay unchanged.")
    result.add_argument("--version", action="version", version=__version__)
    result.add_argument("--db", type=Path, default=Path(os.environ.get("MEDIA_LAB_DB", PROJECT_ROOT / "data/catalogue.sqlite3")))
    result.add_argument("--ffprobe", help="FFprobe executable path (optional)")
    result.add_argument("--probe-timeout", type=positive_seconds, default=120)
    commands = result.add_subparsers(dest="command", required=True)
    commands.add_parser("doctor", help="Check Python, SQLite and FFprobe")
    commands.add_parser("init", help="Create the private local catalogue")
    add = commands.add_parser("ingest", help="Catalogue one or more existing video files in place")
    add.add_argument("files", nargs="+", type=Path)
    add.add_argument("--rehash", action="store_true", help="Read all source bytes even when cached")
    listing = commands.add_parser("list", help="Search the catalogue")
    listing.add_argument("--search", default="")
    check = commands.add_parser("verify", help="Rehash sources and compare to their recorded identity")
    check.add_argument("files", nargs="+", type=Path)
    return result


def emit(value: dict | list):
    print(json.dumps(value, ensure_ascii=True, indent=2))


def validate_database(path: Path, files: list[Path]):
    # Do not let a custom --db open a supplied original as a writable SQLite file.
    db = source_path(path)
    if db.suffix.lower() not in (".sqlite3", ".sqlite", ".db"):
        raise MediaLabError("invalid_database_path", "Use a separate .sqlite3, .sqlite or .db file for the catalogue.")
    raw = source_path(PROJECT_ROOT / "media/raw")
    if db.is_relative_to(raw):
        raise MediaLabError("invalid_database_path", "The catalogue cannot be stored inside media/raw.")
    for source in files:
        resolved = source_path(source)
        if resolved == db or (db.exists() and resolved.exists() and os.path.samefile(db, resolved)):
            raise MediaLabError("invalid_database_path", "The database and original source must be different files.")


def main(argv=None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "doctor":
            probe = FFprobe(args.ffprobe, args.probe_timeout)
            emit({"media_lab": __version__, "python": sys.version.split()[0],
                  "sqlite": sqlite3.sqlite_version, "ffprobe": probe.executable,
                  "ffprobe_version": probe.version,
                  "scope": "Ingestion only; codec decoding, GPU, proxies and rendering are not verified."})
            return 0
        files = getattr(args, "files", [])
        validate_database(args.db, files)
        if args.command in ("list", "verify") and not args.db.is_file():
            raise MediaLabError("missing_catalogue", "No catalogue exists yet; run init or ingest first.")
        with Catalogue(args.db) as catalogue:
            if args.command == "init":
                emit({"outcome": "catalogue_ready", "path": str(catalogue.path)})
                return 0
            if args.command == "list":
                emit(catalogue.search(args.search))
                return 0
            results = []
            failures = 0
            for source in files:
                try:
                    if args.command == "ingest":
                        results.append(ingest(catalogue, source, rehash=args.rehash,
                                              ffprobe=args.ffprobe, probe_timeout=args.probe_timeout))
                    else:
                        results.append(verify(catalogue, source))
                except MediaLabError as exc:
                    failures += 1
                    results.append({"path": str(source), "outcome": "failed", "error": exc.code, "message": str(exc)})
            emit({"results": results, "failures": failures})
            return 1 if failures else 0
    except MediaLabError as exc:
        emit({"outcome": "failed", "error": exc.code, "message": str(exc)})
        return 1
    except (OSError, sqlite3.Error) as exc:
        emit({"outcome": "failed", "error": "storage_error", "message": str(exc)})
        return 1
    except KeyboardInterrupt:
        emit({"outcome": "interrupted", "message": "Import stopped. Completed files remain catalogued; retry unfinished files."})
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
