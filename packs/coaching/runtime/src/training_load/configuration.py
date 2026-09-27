"""Explicit read-only loading of private threshold timelines, outside calculation.

No auto-discovery, environment fallback, date inference, defaults, or writes.
Malformed/missing files fail closed with fixed codes, never file paths or values.
"""

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from .service import ThresholdConfiguration, ThresholdReference, TrainingLoadError, _validate_config


THRESHOLD_CONFIG_VERSION = 'training-thresholds-v1'
MAX_THRESHOLD_CONFIG_BYTES = 131072
_TIMESTAMP = re.compile(r'\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})', re.ASCII)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise TrainingLoadError('duplicate_threshold_config_key')
        result[key] = value
    return result


def _constant(_):
    raise TrainingLoadError('nonfinite_threshold_config_number')


def _timestamp(value):
    if not isinstance(value, str) or _TIMESTAMP.fullmatch(value) is None:
        raise TrainingLoadError('invalid_threshold_config_timestamp')
    # Unknown offset (-00:00) is not an established instant in this contract.
    if value.endswith('-00:00'):
        raise TrainingLoadError('invalid_threshold_config_timestamp')
    if not value.endswith('Z') and (int(value[-5:-3]) > 23 or int(value[-2:]) > 59):
        raise TrainingLoadError('invalid_threshold_config_timestamp')
    try:
        return datetime.fromisoformat(value).astimezone(timezone.utc)
    except ValueError:
        raise TrainingLoadError('invalid_threshold_config_timestamp') from None


def parse_threshold_configuration(text: str) -> ThresholdConfiguration:
    """Parse versioned UTF-8 JSON semantics; timestamps require explicit offsets.

    Overlapping valid entries remain explicit; calculation rejects ambiguous
    applicable thresholds. Empty timelines are allowed but never fill missing FTP.
    """
    try:
        if not isinstance(text, str) or len(text.encode('utf-8')) > MAX_THRESHOLD_CONFIG_BYTES:
            raise TrainingLoadError('invalid_threshold_config_size')
        value = json.loads(text, object_pairs_hook=_object, parse_constant=_constant)
        if (type(value) is not dict or set(value) != {'schema_version', 'thresholds'}
                or value['schema_version'] != THRESHOLD_CONFIG_VERSION
                or type(value['thresholds']) is not list):
            raise TrainingLoadError('invalid_threshold_config_schema')
        required = {'kind', 'value', 'unit', 'effective_from'}
        optional = {'effective_to', 'source', 'method'}
        thresholds = []
        for row in value['thresholds']:
            if type(row) is not dict or not required <= row.keys() or row.keys() - required - optional:
                raise TrainingLoadError('invalid_threshold_config_entry')
            thresholds.append(ThresholdReference(
                kind=row['kind'], value=row['value'], unit=row['unit'],
                effective_from=_timestamp(row['effective_from']),
                effective_to=_timestamp(row['effective_to']) if row.get('effective_to') is not None else None,
                source=row.get('source'), method=row.get('method')))
        result = ThresholdConfiguration(tuple(thresholds))
        _validate_config(result)
        return result
    except TrainingLoadError:
        raise
    except Exception:
        raise TrainingLoadError('invalid_threshold_config') from None


def load_threshold_configuration(path: Path) -> ThresholdConfiguration:
    """Read only the explicitly supplied path. Missing files are errors.

    Store real timelines under ignored config/*.local.* or data/. Loading a file
    does not certify its values or apply it automatically to any activity/history.
    """
    if not isinstance(path, Path):
        raise TrainingLoadError('invalid_threshold_config_path')
    try:
        with path.open('rb') as source:
            content = source.read(MAX_THRESHOLD_CONFIG_BYTES + 1)
    except OSError:
        raise TrainingLoadError('threshold_config_read_failed') from None
    if len(content) > MAX_THRESHOLD_CONFIG_BYTES:
        raise TrainingLoadError('invalid_threshold_config_size')
    try:
        text = content.decode('utf-8')
    except UnicodeError:
        raise TrainingLoadError('invalid_threshold_config_encoding') from None
    return parse_threshold_configuration(text)
