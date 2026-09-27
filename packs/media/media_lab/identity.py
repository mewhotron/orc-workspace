"""Read-only source identity. Stat caching is not a fresh integrity check."""

import hashlib
import json
import os
import stat
from pathlib import Path

from .errors import MediaLabError


def source_path(path: str | Path) -> Path:
    return Path(os.path.normcase(str(Path(path).expanduser().resolve())))


def fingerprint(path: Path) -> str:
    try:
        info = path.stat()
    except FileNotFoundError as exc:
        raise MediaLabError("missing_source", f"Source does not exist: {path}") from exc
    except OSError as exc:
        raise MediaLabError("unreadable_source", f"Cannot inspect source: {path}") from exc
    if not stat.S_ISREG(info.st_mode):
        raise MediaLabError("unsupported_source", f"Choose a regular file: {path}")
    return json.dumps(
        [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns]
    )


def require_unchanged(path: Path, expected: str) -> None:
    if fingerprint(path) != expected:
        raise MediaLabError(
            "source_changed", "The source changed while being read. Finish copying it and retry."
        )


def hash_source(path: Path, expected: str) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise MediaLabError("unreadable_source", f"Cannot read source: {path}") from exc
    require_unchanged(path, expected)
    return digest.hexdigest()
