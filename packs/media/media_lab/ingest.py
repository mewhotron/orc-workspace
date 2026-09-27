"""Transactional registration of existing files. No source copying or writing."""

from pathlib import Path

from .catalogue import Catalogue, now
from .errors import MediaLabError
from .identity import fingerprint, hash_source, require_unchanged, source_path
from .probe import FFprobe


def ingest(catalogue: Catalogue, source: str | Path, *, rehash: bool = False,
           probe=None, ffprobe: str | None = None, probe_timeout: float = 120) -> dict:
    path = source_path(source)
    try:
        signature = fingerprint(path)
        previous = catalogue.location(path)
        if previous and previous["fingerprint"] == signature and previous["status"] == "verified" and not rehash:
            with catalogue.connection:
                catalogue.event(path, previous["media_id"], "already_known_cached")
            return {"outcome": "already_known_cached", "path": str(path),
                    "media_id": previous["media_id"], "identity_check": "stat_cache"}
        checksum = hash_source(path, signature)
        known = catalogue.media(checksum)
        metadata = raw = version = None
        if not known:
            inspector = probe if probe is not None else FFprobe(ffprobe, probe_timeout)
            metadata, raw = inspector.inspect(path)
            version = inspector.version
        require_unchanged(path, signature)
        media_id = "sha256:" + checksum
        if previous and previous["media_id"] != media_id:
            outcome = "changed"
        elif previous:
            outcome = "already_known_verified"
        elif known:
            # A new path with identical bytes may be a rename or a second copy.
            outcome = "known_content_new_path"
        else:
            outcome = "new"
        catalogue.record(path, checksum, signature, metadata, raw, version, outcome)
        return {"outcome": outcome, "path": str(path), "media_id": media_id,
                "identity_check": "sha256"}
    except MediaLabError as exc:
        catalogue.failed(path, exc)
        raise


def verify(catalogue: Catalogue, source: str | Path) -> dict:
    """Always read and hash all bytes against a registered identity."""
    path = source_path(source)
    previous = catalogue.location(path)
    if not previous:
        raise MediaLabError("unknown_source", "This path is not catalogued; ingest it first.")
    try:
        signature = fingerprint(path)
        checksum = hash_source(path, signature)
        if "sha256:" + checksum != previous["media_id"]:
            raise MediaLabError("checksum_mismatch", "Source contents differ from the recorded identity. No media record was replaced.")
        with catalogue.connection:
            catalogue.connection.execute(
                "UPDATE locations SET fingerprint=?, verified_at=?, status='verified' WHERE path=?",
                (signature, now(), str(path)),
            )
            catalogue.event(path, previous["media_id"], "checksum_verified")
        return {"outcome": "checksum_verified", "path": str(path), "media_id": previous["media_id"],
                "identity_check": "sha256", "decode_integrity": "not_tested"}
    except MediaLabError as exc:
        catalogue.failed(path, exc)
        raise
