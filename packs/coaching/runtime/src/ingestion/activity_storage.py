"""Transactional storage of source observations, never canonical workouts.

The stable payload includes all normalized fields and stable source identity.
Path, discovery time, and archive metadata live in the import row and preserve
the original ActivityRecord. Rediscovery is logged but cannot change an existing
observation or its first-import provenance. No uniqueness is imposed on workout
IDs, parser IDs, times, or metrics.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.ingestion.activity import validate_activity_record
from src.ingestion.garmin_summary import GarminSummaryResult, parse_garmin_summary
from src.models.activity import ActivityMetrics, ActivityRecord
from src.models.provenance import SourceFile
from src.utils.checksums import sha256_file


DEFAULT_ACTIVITY_DATABASE = Path("local/activities.sqlite3")
_APPLICATION_ID = 0x41544356
_SCHEMA_VERSION = 3


class ActivityStorageError(ValueError):
    """Storage failure with no private payload values in the message."""


class ObservationConflictError(ActivityStorageError):
    """An export/index identity already exists with a different payload."""


@dataclass(frozen=True)
class ObservationBatch:
    """One source object's accepted records and parser diagnostics.

    Accepted/rejected counts are derived from the tuples to avoid count drift.
    Archive location belongs to receipt provenance, never observation identity.
    An archive batch has no direct records and groups member batches in one
    transaction. Its receipt links to per-member provenance receipts.
    """
    records: tuple[ActivityRecord, ...]
    source_file: SourceFile
    warnings: dict[str, int]
    rejection_diagnostics: tuple[dict, ...] = ()
    member_batches: tuple[ObservationBatch, ...] = ()


@dataclass(frozen=True)
class ActivityImportReport:
    parser_accepted: int
    parser_rejected: int
    inserted: int
    already_existing: int
    final_observation_count: int
    source_sha256: str
    warnings: dict[str, int]
    rejection_diagnostics: tuple[dict, ...]


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=True, allow_nan=False)


def canonical_observation_payload(record: ActivityRecord) -> str:
    """Deterministic stable content; volatile source provenance is stored separately."""
    value = asdict(record)
    # Additive payload extension: absence means unavailable. Preserve the exact
    # legacy serialization/checksum when no timer is supplied; never rewrite old
    # observations or drop unknown/missing legacy fields during validation.
    if value['metrics']['timer_time_s'] is None:
        del value['metrics']['timer_time_s']
    value["source_file"] = {
        "source": record.source_file.source,
        "checksum_sha256": record.source_file.checksum_sha256,
    }
    value["start_time"] = record.start_time.isoformat(timespec="microseconds")
    value["end_time"] = (
        record.end_time.isoformat(timespec="microseconds") if record.end_time else None
    )
    return _json(value)


def _checksum(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _create_schema(db: sqlite3.Connection) -> None:
    application_id = db.execute("PRAGMA application_id").fetchone()[0]
    version = db.execute("PRAGMA user_version").fetchone()[0]
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    if (tables or application_id or version) and (
        application_id != _APPLICATION_ID or version not in (1, 2, _SCHEMA_VERSION)
    ):
        raise ActivityStorageError("not_a_supported_activity_database")
    if tables or application_id or version:
        _validate_schema(db)
        if version == 1:
            _migrate_v1_to_v2(db)
        if version < 3:
            _migrate_v2_to_v3(db)
        return
    # Individual statements keep schema creation inside the explicit transaction.
    db.execute("""CREATE TABLE IF NOT EXISTS activity_exports (
        source_export_sha256 TEXT PRIMARY KEY NOT NULL,
        source TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS activity_imports (
        import_id INTEGER PRIMARY KEY,
        source_export_sha256 TEXT NOT NULL REFERENCES activity_exports,
        source_path TEXT NOT NULL,
        source_path_absolute TEXT NOT NULL,
        discovered_at TEXT NOT NULL,
        imported_at TEXT NOT NULL,
        parser_accepted INTEGER NOT NULL,
        parser_rejected INTEGER NOT NULL,
        warnings_json TEXT NOT NULL,
        rejections_json TEXT NOT NULL,
        archive_member TEXT,
        archive_checksum_sha256 TEXT,
        UNIQUE(import_id, source_export_sha256)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS activity_observations (
        source_export_sha256 TEXT NOT NULL REFERENCES activity_exports,
        source_record_index INTEGER NOT NULL CHECK(source_record_index >= 0),
        record_id TEXT NOT NULL,
        source_activity_id TEXT,
        import_id INTEGER NOT NULL,
        payload_json TEXT NOT NULL,
        payload_sha256 TEXT NOT NULL,
        PRIMARY KEY(source_export_sha256, source_record_index),
        FOREIGN KEY(import_id, source_export_sha256)
            REFERENCES activity_imports(import_id, source_export_sha256)
    )""")
    _migrate_v2_to_v3(db)
    db.execute(f"PRAGMA application_id = {_APPLICATION_ID}")
    db.execute(f"PRAGMA user_version = {_SCHEMA_VERSION}")


def store_garmin_summary(
    database_path: Path, result: GarminSummaryResult, source_file: SourceFile,
) -> ActivityImportReport:
    """Compatibility adapter from summary parsing to generic storage."""
    if source_file.source != "garmin":
        raise ActivityStorageError("invalid_source_identity")
    return store_activity_observations(database_path, ObservationBatch(
        result.records, source_file, result.warnings,
        tuple(asdict(item) for item in result.rejected),
    ))


def _migrate_v1_to_v2(db: sqlite3.Connection) -> None:
    """DDL and version change share the caller's explicit write transaction."""
    db.execute("ALTER TABLE activity_imports ADD COLUMN archive_member TEXT")
    db.execute("ALTER TABLE activity_imports ADD COLUMN archive_checksum_sha256 TEXT")
    db.execute("PRAGMA user_version = 2")


def _migrate_v2_to_v3(db: sqlite3.Connection) -> None:
    """Add operation/member receipt links without changing existing rows."""
    db.execute("""CREATE TABLE activity_import_members (
        archive_import_id INTEGER NOT NULL REFERENCES activity_imports(import_id),
        member_import_id INTEGER PRIMARY KEY REFERENCES activity_imports(import_id),
        CHECK(archive_import_id != member_import_id)
    )""")
    db.execute("PRAGMA user_version = 3")


def _valid_checksum(value: object) -> bool:
    return (isinstance(value, str) and len(value) == 64
            and all(c in "0123456789abcdef" for c in value))


def _validate_archive(member: object, checksum: object) -> None:
    if member is None and checksum is None:
        return
    if (not _valid_checksum(checksum) or not isinstance(member, str)
            or not member or "\\" in member or ":" in member
            or any(ord(c) < 32 for c in member)
            or any(part in ("", ".", "..") for part in member.split("/"))):
        raise ActivityStorageError("invalid_archive_provenance")


def _validate_schema(db: sqlite3.Connection) -> int:
    if db.execute("PRAGMA application_id").fetchone()[0] != _APPLICATION_ID:
        raise ActivityStorageError("invalid_activity_database_application_id")
    version = db.execute("PRAGMA user_version").fetchone()[0]
    if version not in (1, 2, _SCHEMA_VERSION):
        raise ActivityStorageError("unsupported_activity_database_schema_version")
    required = {
        "activity_exports": {"source_export_sha256", "source"},
        "activity_imports": {"import_id", "source_export_sha256", "source_path",
            "source_path_absolute", "discovered_at", "imported_at", "parser_accepted",
            "parser_rejected", "warnings_json", "rejections_json"},
        "activity_observations": {"source_export_sha256", "source_record_index",
            "record_id", "source_activity_id", "import_id", "payload_json", "payload_sha256"},
    }
    if version >= 3:
        required["activity_import_members"] = {"archive_import_id", "member_import_id"}
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not required.keys() <= tables:
        raise ActivityStorageError("missing_activity_database_tables")
    if version >= 2:
        required["activity_imports"].update(("archive_member", "archive_checksum_sha256"))
    for table, columns in required.items():
        if not columns <= {r[1] for r in db.execute(f"PRAGMA table_info({table})")}:
            raise ActivityStorageError("missing_activity_database_columns")
    if db.execute("PRAGMA foreign_key_check").fetchone() is not None:
        raise ActivityStorageError("activity_database_foreign_key_violation")
    if version >= 3 and db.execute("""SELECT 1 FROM activity_import_members link
        JOIN activity_imports parent ON parent.import_id=link.archive_import_id
        JOIN activity_imports member ON member.import_id=link.member_import_id
        WHERE member.archive_member IS NULL
           OR parent.archive_member IS NOT NULL
           OR parent.archive_checksum_sha256 IS NOT NULL
           OR member.archive_checksum_sha256 IS NOT parent.source_export_sha256
           OR member.source_path IS NOT parent.source_path
           OR member.discovered_at IS NOT parent.discovered_at
        LIMIT 1""").fetchone() is not None:
        raise ActivityStorageError("invalid_archive_receipt_link")
    return version


def _prepare_batch(database_path: Path, batch: ObservationBatch) -> list:
    source_file = batch.source_file
    checksum = source_file.checksum_sha256
    if (not isinstance(source_file.source, str) or not source_file.source.strip()
            or not _valid_checksum(checksum)):
        raise ActivityStorageError("invalid_source_identity")
    if (not isinstance(source_file.discovered_at, datetime)
            or source_file.discovered_at.utcoffset() is None):
        raise ActivityStorageError("naive_discovery_timestamp")
    if (not isinstance(source_file.original_path, Path)
            or str(source_file.original_path) == "." or "\x00" in str(source_file.original_path)):
        raise ActivityStorageError("invalid_source_path")
    _validate_archive(source_file.archive_member, source_file.archive_checksum_sha256)
    if (database_path.resolve() == source_file.original_path.resolve()
            or (database_path.exists() and source_file.original_path.exists()
                and database_path.samefile(source_file.original_path))):
        raise ActivityStorageError("database_is_source_file")
    prepared = []
    indices = set()
    for record in batch.records:
        validate_activity_record(record)
        index = record.source_record_index
        if record.source_file != source_file or index is None or index in indices:
            raise ActivityStorageError("inconsistent_record_provenance_or_index")
        if record.source_activity_id is not None and not isinstance(record.source_activity_id, str):
            raise ActivityStorageError("source_activity_id_must_be_text")
        indices.add(index)
        payload = canonical_observation_payload(record)
        prepared.append((record, payload, _checksum(payload)))
    return prepared


def _store_batch(db, batch, prepared, accepted_count, warnings, diagnostics):
    """Write a source receipt and its observations using the caller's transaction."""
    source_file = batch.source_file
    checksum = source_file.checksum_sha256
    db.execute("""INSERT INTO activity_exports VALUES (?, ?)
        ON CONFLICT(source_export_sha256) DO NOTHING""", (checksum, source_file.source))
    if db.execute("SELECT source FROM activity_exports WHERE source_export_sha256=?",
                  (checksum,)).fetchone()[0] != source_file.source:
        raise ObservationConflictError("source_export_conflict")
    receipt = db.execute("""INSERT INTO activity_imports (
        source_export_sha256, source_path, source_path_absolute, discovered_at, imported_at,
        parser_accepted, parser_rejected, warnings_json, rejections_json,
        archive_member, archive_checksum_sha256
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", (
        checksum, str(source_file.original_path), str(source_file.original_path.resolve()),
        source_file.discovered_at.isoformat(),
        datetime.now(timezone.utc).isoformat(), accepted_count, len(diagnostics),
        _json(warnings), _json(diagnostics),
        source_file.archive_member, source_file.archive_checksum_sha256,
    )).lastrowid
    inserted = existing = 0
    for record, payload, payload_checksum in prepared:
        identity = (checksum, record.source_record_index)
        stored = db.execute("""SELECT record_id, source_activity_id, payload_json,
            payload_sha256 FROM activity_observations
            WHERE source_export_sha256=? AND source_record_index=?""", identity).fetchone()
        content = (record.record_id, record.source_activity_id, payload, payload_checksum)
        if stored is not None:
            if stored != content:
                raise ObservationConflictError(
                    f"observation_payload_conflict: index={record.source_record_index}"
                )
            existing += 1
            continue
        db.execute("""INSERT INTO activity_observations VALUES (?, ?, ?, ?, ?, ?, ?)""",
                   (*identity, record.record_id, record.source_activity_id,
                    receipt, payload, payload_checksum))
        inserted += 1
    return receipt, inserted, existing


def store_activity_observations(
    database_path: Path, batch: ObservationBatch,
) -> ActivityImportReport:
    """Persist one source or an archive operation in a single transaction.

    Archive operations have a root receipt with aggregate diagnostics and linked
    member provenance receipts. Observation identity always uses member bytes.
    Repeats retain first-import provenance. Any conflict rolls back every receipt,
    observation, and migration in this operation.
    """
    if batch.member_batches and (batch.records or batch.source_file.archive_member is not None):
        raise ActivityStorageError("invalid_archive_batch")
    prepared = _prepare_batch(database_path, batch)
    children = []
    for member in batch.member_batches:
        source = member.source_file
        if (member.member_batches or source.archive_member is None
                or source.source != batch.source_file.source
                or source.original_path != batch.source_file.original_path
                or source.archive_checksum_sha256 != batch.source_file.checksum_sha256
                or source.discovered_at != batch.source_file.discovered_at):
            raise ActivityStorageError("inconsistent_archive_batch_provenance")
        children.append((member, _prepare_batch(database_path, member)))
    accepted = len(batch.records) + sum(len(m.records) for m, _ in children)
    warnings = dict(batch.warnings)
    diagnostics = list(batch.rejection_diagnostics)
    for member, _ in children:
        diagnostics.extend(member.rejection_diagnostics)
        for key, count in member.warnings.items():
            warnings[key] = warnings.get(key, 0) + count
    diagnostics = tuple(diagnostics)
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(database_path, isolation_level=None)
    except (OSError, sqlite3.Error):
        raise ActivityStorageError("activity_database_write_failed") from None
    try:
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("BEGIN IMMEDIATE")
        _create_schema(db)
        receipt, inserted, existing = _store_batch(
            db, batch, prepared, accepted, warnings, diagnostics)
        for member, member_prepared in children:
            member_receipt, added, repeated = _store_batch(
                db, member, member_prepared, len(member.records),
                member.warnings, member.rejection_diagnostics)
            db.execute("INSERT INTO activity_import_members VALUES (?, ?)",
                       (receipt, member_receipt))
            inserted += added
            existing += repeated
        final_count = db.execute("SELECT COUNT(*) FROM activity_observations").fetchone()[0]
        db.execute("COMMIT")
    except BaseException as error:
        if db.in_transaction:
            db.execute("ROLLBACK")
        if isinstance(error, (sqlite3.Error, OSError)):
            raise ActivityStorageError("activity_database_write_failed") from None
        raise
    finally:
        db.close()
    return ActivityImportReport(accepted, len(diagnostics), inserted, existing,
                                final_count, batch.source_file.checksum_sha256,
                                warnings, diagnostics)


def import_garmin_summary(
    source_path: Path, database_path: Path = DEFAULT_ACTIVITY_DATABASE,
) -> ActivityImportReport:
    """Parse completely before opening the database; never alter raw source bytes."""
    checksum = sha256_file(source_path)
    discovered_at = datetime.now(timezone.utc)
    result = parse_garmin_summary(source_path)
    if checksum != sha256_file(source_path):
        raise ActivityStorageError("source_changed_during_import")
    source = (result.records[0].source_file if result.records else
              SourceFile("garmin", source_path, discovered_at, checksum))
    if source.checksum_sha256 != checksum:
        raise ActivityStorageError("source_changed_during_import")
    return store_garmin_summary(database_path, result, source)


def load_activity_observations(database_path: Path) -> tuple[ActivityRecord, ...]:
    """Read one consistent snapshot, rejecting corrupt identity or provenance."""
    db = None
    try:
        db = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True,
                             isolation_level=None)
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("BEGIN")
        version = _validate_schema(db)
        archive_columns = ("i.archive_member, i.archive_checksum_sha256"
                           if version >= 2 else "NULL, NULL")
        rows = db.execute(f"""SELECT o.source_export_sha256, o.source_record_index,
            o.record_id, o.source_activity_id, o.payload_json, o.payload_sha256,
            e.source, i.source_path, i.discovered_at, {archive_columns}
            FROM activity_observations o LEFT JOIN activity_imports i
            ON o.import_id=i.import_id AND o.source_export_sha256=i.source_export_sha256
            LEFT JOIN activity_exports e ON o.source_export_sha256=e.source_export_sha256
            ORDER BY o.source_export_sha256, o.source_record_index""").fetchall()
    except (OSError, sqlite3.Error, ValueError) as error:
        if isinstance(error, ActivityStorageError):
            raise
        raise ActivityStorageError("activity_database_read_failed") from None
    finally:
        if db is not None:
            db.close()
    records = []
    for (export_checksum, index, record_id, activity_id, payload, checksum,
         source, source_path, discovered_at, archive_member, archive_checksum) in rows:
        try:
            if not isinstance(payload, str) or _checksum(payload) != checksum:
                raise ObservationConflictError("stored_payload_checksum_mismatch")
            value = json.loads(payload)
            comparisons = (
                (value["source_file"]["source"], source, "source"),
                (value["source_file"]["checksum_sha256"], export_checksum, "source_checksum"),
                (value["source_record_index"], index, "source_record_index"),
                (value["record_id"], record_id, "record_id"),
                (value["source_activity_id"], activity_id, "source_activity_id"),
            )
            for actual, expected, field in comparisons:
                if actual != expected:
                    raise ObservationConflictError(f"stored_payload_{field}_mismatch")
            if not isinstance(source_path, str) or not source_path or "\x00" in source_path:
                raise ActivityStorageError("invalid_stored_source_path")
            discovery_time = datetime.fromisoformat(discovered_at)
            if discovery_time.utcoffset() is None:
                raise ActivityStorageError("invalid_stored_discovery_timestamp")
            _validate_archive(archive_member, archive_checksum)
            value["source_file"] = SourceFile(
                **value["source_file"], original_path=Path(source_path),
                discovered_at=discovery_time, archive_member=archive_member,
                archive_checksum_sha256=archive_checksum,
            )
            value["start_time"] = datetime.fromisoformat(value["start_time"])
            value["end_time"] = (
                datetime.fromisoformat(value["end_time"]) if value["end_time"] is not None else None
            )
            value["metrics"] = ActivityMetrics(**value["metrics"])
            record = validate_activity_record(ActivityRecord(**value))
            for field in ("source_activity_id", "subtype", "source_activity_type", "name",
                          "origin_platform", "device_manufacturer", "device_model"):
                if getattr(record, field) is not None and not isinstance(getattr(record, field), str):
                    raise ActivityStorageError("invalid_stored_activity_field")
            for field in ("is_indoor", "is_manual"):
                if getattr(record, field) is not None and not isinstance(getattr(record, field), bool):
                    raise ActivityStorageError("invalid_stored_activity_field")
            if canonical_observation_payload(record) != payload:
                raise ObservationConflictError("stored_payload_not_canonical")
            records.append(record)
        except (ValueError, TypeError, KeyError, AttributeError, OverflowError, RecursionError) as error:
            if isinstance(error, ActivityStorageError):
                raise
            raise ActivityStorageError("invalid_stored_activity_payload") from None
    return tuple(records)
