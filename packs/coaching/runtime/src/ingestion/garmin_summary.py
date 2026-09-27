"""Parse the inspected Garmin summarized-activities export format.

Conversions were checked against one independently displayed activity, not a
universal Garmin schema. Timestamp millisecond precision was not independently
verified. Raw files are read only; no normalized data is persisted.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.ingestion.activity import (
    ActivityValidationError,
    generate_activity_record_id,
    validate_activity_record,
)
from src.models.activity import ActivityMetrics, ActivityRecord
from src.models.provenance import SourceFile
from src.utils.checksums import sha256_file


class GarminSummaryError(ValueError):
    """Document-level failure, with no source values in the message."""


@dataclass(frozen=True)
class RejectedSummaryRecord:
    source_record_index: int
    field: str
    code: str


@dataclass(frozen=True)
class GarminSummaryResult:
    records: tuple[ActivityRecord, ...]
    rejected: tuple[RejectedSummaryRecord, ...]
    warnings: dict[str, int]

    @property
    def total_records(self) -> int:
        return len(self.records) + len(self.rejected)


class _FieldError(ValueError):
    def __init__(self, field: str, code: str):
        self.field = field
        self.code = code
        super().__init__(code)


_METRICS = (
    ("elapsedDuration", "elapsed_time_s", 0.001),
    ("movingDuration", "moving_time_s", 0.001),
    ("distance", "distance_m", 0.01),
    ("elevationGain", "elevation_gain_m", 0.01),
    ("elevationLoss", "elevation_loss_m", 0.01),
    ("avgSpeed", "average_speed_mps", 10.0),
    ("maxSpeed", "max_speed_mps", 10.0),
    ("avgHr", "average_heart_rate_bpm", 1.0),
    ("maxHr", "max_heart_rate_bpm", 1.0),
    ("avgBikeCadence", "average_cadence_rpm", 1.0),
    ("maxBikeCadence", "max_cadence_rpm", 1.0),
    ("avgPower", "average_power_w", 1.0),
    ("maxPower", "max_power_w", 1.0),
    ("normPower", "normalized_power_w", 1.0),
)
_CLASSIFICATIONS = {
    "road_biking": ("cycling", "road_biking"),
    "mountain_biking": ("cycling", "mountain_biking"),
    "e_bike_fitness": ("cycling", "e_bike_fitness"),
    "cycling": ("cycling", None),
    "strength_training": ("strength_training", None),
}


def _number(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _FieldError(field, "expected_number")
    try:
        result = float(value)
    except OverflowError:
        raise _FieldError(field, "nonfinite_number") from None
    if not math.isfinite(result):
        raise _FieldError(field, "nonfinite_number")
    return result


def _optional_text(row: dict, field: str) -> str | None:
    value = row.get(field)
    if value is not None and not isinstance(value, str):
        raise _FieldError(field, "expected_string")
    return value


def _parse_record(row: object, index: int, source: SourceFile) -> ActivityRecord:
    if not isinstance(row, dict):
        raise _FieldError("record", "expected_object")
    activity_id = row.get("activityId")
    if (
        isinstance(activity_id, bool)
        or not isinstance(activity_id, int)
        or activity_id <= 0
    ):
        raise _FieldError("activityId", "expected_positive_integer")
    activity_type = row.get("activityType")
    if not isinstance(activity_type, str) or not activity_type.strip():
        raise _FieldError("activityType", "expected_nonempty_string")

    milliseconds = _number(row.get("startTimeGmt"), "startTimeGmt")
    try:
        start_time = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(
            milliseconds=milliseconds
        )
    except OverflowError:
        raise _FieldError("startTimeGmt", "timestamp_out_of_range") from None

    metrics = {}
    for source_field, target_field, scale in _METRICS:
        value = row.get(source_field)
        if value is None:
            metrics[target_field] = None
            continue
        number = _number(value, source_field)
        if number < 0:
            raise _FieldError(source_field, "negative_metric")
        converted = number * scale
        if not math.isfinite(converted):
            raise _FieldError(source_field, "conversion_overflow")
        metrics[target_field] = converted

    sport, subtype = _CLASSIFICATIONS.get(activity_type, ("unknown", None))
    source_activity_id = str(activity_id)
    record = ActivityRecord(
        record_id=generate_activity_record_id(
            source_file=source,
            source_activity_id=source_activity_id,
            source_record_index=index,
            start_time=start_time,
        ),
        source_file=source,
        source_activity_id=source_activity_id,
        source_record_index=index,
        start_time=start_time,
        end_time=None,
        sport=sport,
        subtype=subtype,
        source_activity_type=activity_type,
        name=_optional_text(row, "name"),
        device_manufacturer=_optional_text(row, "manufacturer"),
        metrics=ActivityMetrics(**metrics),
    )
    try:
        return validate_activity_record(record)
    except ActivityValidationError:
        raise _FieldError("record", "activity_validation_failed") from None


def _unique_keys(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise GarminSummaryError("duplicate_json_key")
        result[key] = value
    return result


def parse_garmin_summary(path: Path) -> GarminSummaryResult:
    """Read an export and return validated records and privacy-safe diagnostics.

    Required record fields are positive integer activityId, nonblank string
    activityType, and finite numeric startTimeGmt in representable Unix ms.
    Each rejection reports its first invalid mapped field. Unmapped fields are
    ignored. Unknown types on accepted records increment unknown_activity_type.
    Indices span all wrapper arrays, including rejected entries.

    duration is Garmin Total Time (timer duration), which the current model
    cannot represent. It never substitutes for elapsedDuration or movingDuration.
    End time, origin, model, indoor/manual flags, calories, and work stay None.

    Raises GarminSummaryError for invalid JSON, duplicate keys, invalid wrapper
    structure, or content changes during reading. Filesystem errors propagate.
    """
    checksum = sha256_file(path)
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != checksum:
        raise GarminSummaryError("source_changed_during_read")
    try:
        document = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=_unique_keys)
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        if isinstance(error, GarminSummaryError):
            raise
        raise GarminSummaryError("invalid_json_document") from None
    if not isinstance(document, list):
        raise GarminSummaryError("expected_wrapper_list")
    rows = []
    for wrapper_index, wrapper in enumerate(document):
        if not isinstance(wrapper, dict) or not isinstance(
            wrapper.get("summarizedActivitiesExport"), list
        ):
            raise GarminSummaryError(
                f"wrapper_{wrapper_index}: expected_summarized_activities_array"
            )
        rows.extend(wrapper["summarizedActivitiesExport"])

    source = SourceFile(
        source="garmin",
        original_path=path,
        discovered_at=datetime.now(timezone.utc),
        checksum_sha256=checksum,
    )
    records = []
    rejected = []
    warnings = {}
    for index, row in enumerate(rows):
        try:
            record = _parse_record(row, index, source)
        except _FieldError as error:
            rejected.append(RejectedSummaryRecord(index, error.field, error.code))
            continue
        records.append(record)
        if record.sport == "unknown":
            warnings["unknown_activity_type"] = (
                warnings.get("unknown_activity_type", 0) + 1
            )
    return GarminSummaryResult(tuple(records), tuple(rejected), warnings)
