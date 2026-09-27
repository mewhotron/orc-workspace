from __future__ import annotations

import hashlib
import math
from dataclasses import fields
from datetime import datetime, timezone
from numbers import Real

from src.models.activity import ActivityMetrics, ActivityRecord
from src.models.provenance import SourceFile


class ActivityValidationError(ValueError):
    """Raised when a normalized activity violates a structural invariant."""


def _validate_aware_datetime(
    value: datetime,
    *,
    field_name: str,
) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ActivityValidationError(
            f"{field_name} must be timezone-aware."
        )


def _validate_source_record_index(
    source_record_index: int | None,
) -> None:
    if source_record_index is None:
        return

    if (
        isinstance(source_record_index, bool)
        or not isinstance(source_record_index, int)
        or source_record_index < 0
    ):
        raise ActivityValidationError(
            "source_record_index must be a non-negative integer or None."
        )


def validate_activity_metrics(
    metrics: ActivityMetrics,
) -> ActivityMetrics:
    for metric_field in fields(metrics):
        value = getattr(metrics, metric_field.name)

        if value is None:
            continue

        if isinstance(value, bool) or not isinstance(value, Real):
            raise ActivityValidationError(
                f"{metric_field.name} must be numeric or None."
            )

        if not math.isfinite(float(value)):
            raise ActivityValidationError(
                f"{metric_field.name} must be finite."
            )

        if value < 0:
            raise ActivityValidationError(
                f"{metric_field.name} must not be negative."
            )

    if (metrics.timer_time_s is not None and metrics.elapsed_time_s is not None
            and metrics.timer_time_s > metrics.elapsed_time_s):
        raise ActivityValidationError('timer_time_exceeds_elapsed_time')

    return metrics


def generate_activity_record_id(
    *,
    source_file: SourceFile,
    source_activity_id: str | None,
    source_record_index: int | None,
    start_time: datetime,
) -> str:
    """Build a deterministic ID for one source observation.

    The local file path and discovery time are intentionally excluded.
    Identical source content parsed into the same source record therefore
    receives the same ID even if the raw file is moved or rediscovered.
    """

    _validate_aware_datetime(
        start_time,
        field_name="start_time",
    )
    _validate_source_record_index(source_record_index)

    normalized_start = (
        start_time
        .astimezone(timezone.utc)
        .isoformat(timespec="microseconds")
    )

    identity_parts = (
        source_file.source.strip().casefold(),
        source_file.checksum_sha256.strip().casefold(),
        (source_activity_id or "").strip(),
        (
            str(source_record_index)
            if source_record_index is not None
            else ""
        ),
        normalized_start,
    )

    identity_text = "\x1f".join(identity_parts)
    digest = hashlib.sha256(
        identity_text.encode("utf-8")
    ).hexdigest()

    return f"activity-{digest}"


def validate_activity_record(
    record: ActivityRecord,
) -> ActivityRecord:
    if not record.record_id.strip():
        raise ActivityValidationError(
            "record_id must be non-empty."
        )

    if not record.source_file.source.strip():
        raise ActivityValidationError(
            "source_file.source must be non-empty."
        )

    if not record.source_file.checksum_sha256.strip():
        raise ActivityValidationError(
            "source_file.checksum_sha256 must be non-empty."
        )

    _validate_source_record_index(
        record.source_record_index
    )

    _validate_aware_datetime(
        record.start_time,
        field_name="start_time",
    )

    if record.end_time is not None:
        _validate_aware_datetime(
            record.end_time,
            field_name="end_time",
        )

        if record.end_time < record.start_time:
            raise ActivityValidationError(
                "end_time must not be before start_time."
            )

    if not record.sport.strip():
        raise ActivityValidationError(
            "sport must be non-empty."
        )

    validate_activity_metrics(record.metrics)

    return record
