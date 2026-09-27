"""Normalize a single-session FIT activity without storage or workout linking.

Each FIT member is its own source object (member checksum, record index zero).
Content-identical copies share a record ID under the existing identity contract;
their archive/member provenance still distinguishes the physical copies.
Multi-session activities are explicitly unsupported rather than flattened.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

from src.ingestion.activity import (
    ActivityValidationError, generate_activity_record_id, validate_activity_record,
)
from src.ingestion.garmin_fit import (
    FitDecodeResult, GarminFitError, decode_fit_bytes, decode_fit_file,
)
from src.models.activity import ActivityMetrics, ActivityRecord
from src.models.provenance import SourceFile


@dataclass(frozen=True)
class RejectedFitActivity:
    source_record_index: int
    field: str
    code: str


@dataclass(frozen=True)
class FitActivityResult:
    records: tuple[ActivityRecord, ...] = field(repr=False)
    rejected: tuple[RejectedFitActivity, ...]
    warnings: dict[str, int]


class _Reject(ValueError):
    def __init__(self, field_name, code):
        self.field_name, self.code = field_name, code
        super().__init__(code)


_METRICS = {
    'total_elapsed_time': 'elapsed_time_s', 'total_moving_time': 'moving_time_s',
    'total_timer_time': 'timer_time_s',
    'total_distance': 'distance_m', 'total_ascent': 'elevation_gain_m',
    'total_descent': 'elevation_loss_m', 'avg_heart_rate': 'average_heart_rate_bpm',
    'max_heart_rate': 'max_heart_rate_bpm', 'avg_power': 'average_power_w',
    'max_power': 'max_power_w', 'normalized_power': 'normalized_power_w',
    'total_calories': 'calories_kcal',
}
_CYCLING = {
    'generic': None, 'road': 'road_biking', 'mountain': 'mountain_biking',
    'commuting': 'commuting', 'e_bike_fitness': 'e_bike_fitness',
}


def _number(row, key):
    value = row.get(key)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise _Reject(key, 'expected_number')
    try:
        value = float(value)
    except OverflowError:
        raise _Reject(key, 'nonfinite_metric') from None
    if not math.isfinite(value) or value < 0:
        raise _Reject(key, 'invalid_metric')
    return value


def _time(value, key, required=False):
    if value is None and not required:
        return None
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise _Reject(key, 'expected_aware_datetime')
    return value.astimezone(timezone.utc)


def _checksum(value):
    return (isinstance(value, str) and len(value) == 64
            and all(c in '0123456789abcdef' for c in value))


def _source(decoded):
    p = decoded.provenance
    if not isinstance(p.original_path, Path) or not _checksum(p.checksum_sha256):
        raise _Reject('provenance', 'missing_or_invalid_source')
    discovery = _time(p.decoded_at, 'discovered_at', required=True)
    if (p.archive_member is None) != (p.archive_checksum_sha256 is None):
        raise _Reject('provenance', 'incomplete_archive_provenance')
    if p.archive_member is not None:
        member = p.archive_member
        if (not isinstance(member, str) or not member or '\\' in member
                or '\x00' in member or ':' in member
                or PurePosixPath(member).is_absolute() or '..' in member.split('/')
                or not _checksum(p.archive_checksum_sha256)):
            raise _Reject('provenance', 'invalid_archive_provenance')
    return SourceFile('garmin', p.original_path, discovery, p.checksum_sha256,
                      p.archive_member, p.archive_checksum_sha256)


def _messages(decoded, name):
    rows = decoded.messages.get(name, [])
    if not isinstance(rows, (tuple, list)) or not all(isinstance(r, dict) for r in rows):
        raise _Reject(name, 'invalid_message_collection')
    return rows


def _normalize(decoded, warnings):
    source = _source(decoded)
    ids = _messages(decoded, 'file_id_mesgs')
    if len(ids) != 1 or ids[0].get('type') != 'activity':
        raise _Reject('file_id', 'expected_one_activity_file_id')
    sessions = _messages(decoded, 'session_mesgs')
    if len(sessions) != 1:
        raise _Reject('session', 'expected_one_session')
    activities = _messages(decoded, 'activity_mesgs')
    if len(activities) > 1:
        raise _Reject('activity', 'multiple_activity_messages')
    if activities and activities[0].get('num_sessions', 1) != 1:
        raise _Reject('activity', 'inconsistent_session_count')
    session, file_id = sessions[0], ids[0]
    start = _time(session.get('start_time'), 'start_time', required=True)
    # Session timestamp is the session endpoint. File creation time and the
    # activity save timestamp are not substitutes for either endpoint.
    end = _time(session.get('timestamp'), 'timestamp')
    if end is not None and end < start:
        raise _Reject('timestamp', 'end_before_start')
    raw_sport, raw_sub = session.get('sport'), session.get('sub_sport')
    for key, value in [('sport', raw_sport), ('sub_sport', raw_sub)]:
        if value is not None and (isinstance(value, bool) or not isinstance(value, (str, int))):
            raise _Reject(key, 'invalid_classification')
    sport, subtype = 'unknown', None
    if raw_sport == 'cycling':
        sport = 'cycling'
        subtype = _CYCLING.get(raw_sub)
        if raw_sub not in _CYCLING:
            warnings['unknown_sport_mapping'] = 1
    elif raw_sport in ('fitness_equipment', 'training') and raw_sub == 'strength_training':
        sport = 'strength_training'
    else:
        if raw_sport in ('running', 'walking', 'swimming'):
            sport = raw_sport
        warnings['unknown_sport_mapping'] = 1
    metrics = {target: _number(session, key) for key, target in _METRICS.items()}
    for prefix, target in [('avg', 'average_speed_mps'), ('max', 'max_speed_mps')]:
        enhanced = _number(session, 'enhanced_' + prefix + '_speed')
        metrics[target] = enhanced if enhanced is not None else _number(session, prefix + '_speed')
    # The SDK exposes fractional cadence separately, already in rpm.
    for prefix, target in [('avg', 'average_cadence_rpm'), ('max', 'max_cadence_rpm')]:
        whole = _number(session, prefix + '_cadence')
        fraction = _number(session, prefix + '_fractional_cadence')
        metrics[target] = whole + (fraction or 0) if whole is not None else None
        if fraction is not None and whole is None:
            warnings['fractional_cadence_without_base'] = 1
    work = _number(session, 'total_work')
    metrics['work_kj'] = work / 1000 if work is not None else None
    # Only the session duration matches the scope of session metrics. Activity
    # summary timer time is not a substitute, even in a single-session file.
    if session.get('total_timer_time') is None and any(
            a.get('total_timer_time') is not None for a in activities):
        warnings['activity_timer_time_not_used'] = 1
    maker = file_id.get('manufacturer')
    if maker is not None and not isinstance(maker, str):
        maker = None
        warnings['unknown_device_manufacturer'] = 1
    model = file_id.get('garmin_product') if maker == 'garmin' else file_id.get('product')
    if model is not None and not isinstance(model, str):
        model = None
        warnings['unknown_device_product'] = 1
    if model in ('connect', 'training_center'):
        maker = model = None
        warnings['non_device_file_creator'] = 1
    for name, values in [('unknown_fit_messages', decoded.unknown_message_numbers),
                         ('unknown_fit_fields', decoded.unknown_fields),
                         ('unmapped_developer_fields', decoded.developer_field_definitions)]:
        if values:
            warnings[name] = 1
    record = ActivityRecord(
        record_id=generate_activity_record_id(source_file=source, source_activity_id=None,
                                             source_record_index=0, start_time=start),
        source_file=source, source_activity_id=None, source_record_index=0,
        start_time=start, end_time=end, sport=sport, subtype=subtype,
        source_activity_type=json.dumps({'sport': raw_sport, 'sub_sport': raw_sub},
                                        sort_keys=True, separators=(',', ':')),
        device_manufacturer=maker, device_model=model, metrics=ActivityMetrics(**metrics),
        # The export location does not establish origin platform. A profile name
        # is not an activity title; missing GPS does not establish indoor/manual.
    )
    return validate_activity_record(record)


def normalize_fit_activity(decoded: FitDecodeResult) -> FitActivityResult:
    """Normalize one successfully decoded source; return only safe diagnostics.

    Input without a real source path is rejected because generic SourceFile
    requires filesystem provenance. No path or upstream activity ID is invented.
    """
    warnings = {}
    try:
        record = _normalize(decoded, warnings)
    except _Reject as error:
        return FitActivityResult((), (RejectedFitActivity(0, error.field_name, error.code),), warnings)
    except (ActivityValidationError, AttributeError, TypeError, ValueError, OverflowError):
        return FitActivityResult((), (RejectedFitActivity(0, 'record', 'invalid_activity'),), warnings)
    return FitActivityResult((record,), (), warnings)


def parse_fit_activity(source: bytes | Path, **provenance) -> FitActivityResult:
    """Decode using the SDK boundary, then normalize; never persist output.

    Bytes accept the same provenance keywords as decode_fit_bytes. A Path is
    read directly and does not accept provenance overrides.
    """
    try:
        if isinstance(source, Path):
            if provenance:
                return FitActivityResult((), (RejectedFitActivity(0, 'provenance', 'unexpected_override'),), {})
            decoded = decode_fit_file(source)
        else:
            decoded = decode_fit_bytes(source, **provenance)
    except GarminFitError:
        return FitActivityResult((), (RejectedFitActivity(0, 'source', 'fit_decode_failed'),), {})
    return normalize_fit_activity(decoded)
