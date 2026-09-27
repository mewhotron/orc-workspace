"""Import recognized activity FIT members from a ZIP, without extraction.

Malformed FITs and normalizer rejections are member diagnostics; accepted
activities are committed together. Unsafe/unreadable ZIPs fail before storage.
Duplicate names are excluded in their entirety, never selected by name lookup.
"""

from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import io
from pathlib import Path
import stat
import zipfile
import zlib

from src.ingestion.activity_storage import (
    DEFAULT_ACTIVITY_DATABASE, ActivityImportReport, ActivityStorageError,
    ObservationBatch, _validate_archive, store_activity_observations,
)
from src.ingestion.garmin_fit import GarminFitError, decode_fit_bytes
from src.ingestion.garmin_fit_activity import normalize_fit_activity
from src.models.provenance import SourceFile
from src.utils.checksums import sha256_file


class GarminFitImportError(ValueError):
    """Fixed privacy-safe archive import diagnostic."""


# Bound in-memory reads and decompression before touching the database.
MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_TOTAL_FIT_BYTES = 1024 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 10000


def parse_garmin_fit_archive(archive_path: Path) -> ObservationBatch:
    """Return an archive batch; only the existing normalizer admits activities.

    Diagnostics identify members by central-directory ordinal, never filename.
    All accepted members share the archive's discovery timestamp. Each member
    retains its own byte checksum and source_record_index (currently zero).
    """
    try:
        if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
            raise GarminFitImportError("fit_archive_size_limit")
        with archive_path.open('rb') as stream:
            content = stream.read(MAX_ARCHIVE_BYTES + 1)
        if len(content) > MAX_ARCHIVE_BYTES:
            raise GarminFitImportError("fit_archive_size_limit")
        checksum = hashlib.sha256(content).hexdigest()
        if checksum != sha256_file(archive_path):
            raise GarminFitImportError("fit_archive_changed_during_read")
        source = SourceFile('garmin', archive_path, datetime.now(timezone.utc), checksum)
        members, rejected = [], []
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_ARCHIVE_ENTRIES:
                raise GarminFitImportError("fit_archive_entry_limit")
            total = 0
            for entry in entries:
                # orig_filename preserves NULs that ZipInfo.filename truncates.
                name = entry.orig_filename
                try:
                    _validate_archive(name[:-1] if entry.is_dir() else name, checksum)
                except ActivityStorageError:
                    raise GarminFitImportError("unsafe_fit_archive_member") from None
                if entry.flag_bits & 1 or stat.S_ISLNK(entry.external_attr >> 16):
                    raise GarminFitImportError("unsupported_fit_archive_member")
                if name.lower().endswith('.fit') and not entry.is_dir():
                    total += entry.file_size
                    if entry.file_size > MAX_MEMBER_BYTES or total > MAX_TOTAL_FIT_BYTES:
                        raise GarminFitImportError("fit_archive_member_size_limit")
            names = Counter(entry.filename for entry in entries)
            for ordinal, entry in enumerate(entries):
                if entry.is_dir() or not entry.filename.lower().endswith('.fit'):
                    continue
                if names[entry.filename] != 1:
                    rejected.append({'member_index': ordinal, 'code': 'duplicate_archive_member'})
                    continue
                # ZipInfo selects the exact entry; no extraction or filename lookup.
                with archive.open(entry) as stream:
                    raw = stream.read(MAX_MEMBER_BYTES + 1)
                if len(raw) > MAX_MEMBER_BYTES:
                    raise GarminFitImportError("fit_archive_member_size_limit")
                try:
                    decoded = decode_fit_bytes(raw, source_path=archive_path,
                        archive_member=entry.filename, archive_checksum_sha256=checksum)
                except GarminFitError:
                    rejected.append({'member_index': ordinal, 'code': 'fit_decode_failed'})
                    continue
                decoded = replace(decoded, provenance=replace(decoded.provenance,
                                  decoded_at=source.discovered_at))
                result = normalize_fit_activity(decoded)
                if result.rejected:
                    rejected.extend(dict(asdict(item), member_index=ordinal) for item in result.rejected)
                    continue
                if result.records:
                    members.append(ObservationBatch(result.records,
                        result.records[0].source_file, result.warnings))
        if checksum != sha256_file(archive_path):
            raise GarminFitImportError("fit_archive_changed_during_read")
        return ObservationBatch((), source, {}, tuple(rejected), tuple(members))
    except GarminFitImportError:
        raise
    except (OSError, ValueError, RuntimeError, NotImplementedError, EOFError,
            zipfile.BadZipFile, zlib.error):
        raise GarminFitImportError("fit_archive_read_failed") from None


def import_garmin_fit_activities(
    archive_path: Path, database_path: Path = DEFAULT_ACTIVITY_DATABASE,
) -> ActivityImportReport:
    """Parse fully, then commit the accepted archive operation atomically."""
    return store_activity_observations(database_path, parse_garmin_fit_archive(archive_path))
