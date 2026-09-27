"""No storage, clock, model calls, imputation, or cross-method score aggregation.

Power TSS: seconds / 3600 * (NP watts / FTP watts)**2 * 100.
See README.md for the local source anchors and duration semantics gate.
All numeric operations use decimal strings in a private 34-digit, half-even
context; final values are finite Python floats, without display rounding.
"""

from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext
from enum import StrEnum
import math

from src.analytics import summarize_activity_history
from src.history import ActivityHistory, CanonicalActivity
from src.projection.activity import CanonicalField, PROJECTION_VERSION


POWER_TSS_VERSION = 'power-tss-v1'
HR_VERSION = 'hr-unsupported-v1'
DURATION_VERSION = 'duration-inventory-v1'


class LoadMethod(StrEnum):
    POWER = 'power'
    HR = 'hr'
    DURATION = 'duration'


class LoadStatus(StrEnum):
    COMPUTED = 'computed'
    INSUFFICIENT_INPUTS = 'insufficient_inputs'
    INVALID_INPUTS = 'invalid_inputs'
    UNSUPPORTED_METHOD = 'unsupported_method'


class TrainingLoadError(ValueError):
    """Fixed code for malformed API/configuration inputs; no private values."""


@dataclass(frozen=True)
class ThresholdReference:
    kind: str
    value: float = field(repr=False)
    unit: str
    effective_from: datetime
    effective_to: datetime | None = None
    source: str | None = field(default=None, repr=False)
    method: str | None = None


@dataclass(frozen=True)
class ThresholdConfiguration:
    thresholds: tuple[ThresholdReference, ...] = ()


@dataclass(frozen=True)
class DurationReference:
    """Caller attests this canonical duration covers the NP measurement period.

    This is an explicit input assertion, not a conclusion inferred from equality
    of elapsed and moving times. It must be scoped to one canonical activity.
    """
    canonical_id: str = field(repr=False)
    field_name: str
    source: str = field(repr=False)


@dataclass(frozen=True)
class LoadInput:
    """Exact selected canonical field, retaining upstream provenance."""
    canonical_field: CanonicalField
    unit: str


@dataclass(frozen=True)
class ActivityLoad:
    canonical_id: str = field(repr=False)
    method: LoadMethod
    status: LoadStatus
    value: float | None
    unit: str | None
    activity_inputs: tuple[LoadInput, ...]
    thresholds: tuple[ThresholdReference, ...]
    formula_version: str
    reasons: tuple[str, ...]
    projection_version: str
    duration_reference: DurationReference | None = None


@dataclass(frozen=True)
class LoadCoverage:
    activity_count: int
    power_metrics_available: int
    normalized_power_available: int
    power_activity_inputs_available: int
    power_prerequisites_available: int
    missing_normalized_power: int
    missing_valid_ftp: int
    hr_data_available: int
    missing_hr_threshold_configuration: int
    unsupported_by_method: tuple[tuple[str, int], ...]
    scored_by_method: tuple[tuple[str, int], ...]
    abstention_reasons: tuple[tuple[str, str, int], ...]


def _text(value):
    return (isinstance(value, str) and bool(value.strip()) and value == value.strip()
            and not any(ord(c) < 32 for c in value))


def _utc(value):
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise TrainingLoadError('requires_aware_timestamp')
    return value.astimezone(timezone.utc)


def _numeric(value, *, positive=False):
    try:
        return (type(value) in (int, float) and math.isfinite(value)
                and (value > 0 if positive else value >= 0))
    except (OverflowError, ValueError):
        return False


def _validate_config(config):
    if not isinstance(config, ThresholdConfiguration) or type(config.thresholds) is not tuple:
        raise TrainingLoadError('invalid_threshold_configuration')
    units = {'ftp': 'W', 'threshold_hr': 'bpm', 'resting_hr': 'bpm', 'maximum_hr': 'bpm'}
    for threshold in config.thresholds:
        if (not isinstance(threshold, ThresholdReference) or threshold.kind not in units
                or threshold.unit != units[threshold.kind]
                or not _numeric(threshold.value, positive=True)):
            raise TrainingLoadError('invalid_threshold')
        start = _utc(threshold.effective_from)
        if threshold.effective_to is not None and _utc(threshold.effective_to) <= start:
            raise TrainingLoadError('invalid_threshold_period')
        if any(v is not None and not _text(v) for v in (threshold.source, threshold.method)):
            raise TrainingLoadError('invalid_threshold_metadata')


def _threshold(config, kind, start):
    matches = tuple(t for t in config.thresholds if t.kind == kind
                    and _utc(t.effective_from) <= start
                    and (t.effective_to is None or start < _utc(t.effective_to)))
    if len(matches) > 1:
        return None, 'ambiguous_' + kind
    return (matches[0], None) if matches else (None, 'missing_valid_' + kind)


def _fields(activity):
    if (not isinstance(activity, CanonicalActivity) or not _text(activity.canonical_id)
            or activity.projection_version not in ('canonical-activity-v1', PROJECTION_VERSION)
            or activity.linkage_status not in ('unlinked', 'strong_candidate', 'authoritative')
            or type(activity.fields) is not tuple or not activity.contributors
            or type(activity.contributors) is not tuple
            or len(set(activity.contributors)) != len(activity.contributors)):
        raise TrainingLoadError('invalid_canonical_activity')
    fields = {}
    for item in activity.fields:
        if not isinstance(item, CanonicalField) or not _text(item.name) or item.name in fields:
            raise TrainingLoadError('invalid_canonical_fields')
        if (item.value is not None and item.selected_from not in activity.contributors
                or not _text(item.selection_rule)
                or type(item.corroborated_by) is not tuple
                or any(ref not in activity.contributors for ref in item.corroborated_by)):
            raise TrainingLoadError('invalid_canonical_provenance')
        fields[item.name] = item
    if 'start_time' not in fields:
        raise TrainingLoadError('missing_canonical_start')
    _utc(fields['start_time'].value)
    return fields


def _calculate(activity, config, method, duration):
    _validate_config(config)
    fields = _fields(activity)
    start = _utc(fields['start_time'].value)
    if duration is not None and (not isinstance(duration, DurationReference)
            or duration.canonical_id != activity.canonical_id
            or duration.field_name not in ('elapsed_time_s', 'moving_time_s', 'timer_time_s')
            or not _text(duration.source)):
        raise TrainingLoadError('invalid_duration_reference')
    inputs = [LoadInput(fields['start_time'], 'aware timestamp')]
    used = []
    reasons = []
    invalid = False

    def number(name, unit):
        nonlocal invalid
        item = fields.get(name)
        if item is not None:
            inputs.append(LoadInput(item, unit))
        value = item.value if item else None
        if value is None:
            reasons.append('missing_' + name)
        elif not _numeric(value):
            reasons.append('invalid_' + name)
            invalid = True
        return value

    versions = {LoadMethod.POWER: POWER_TSS_VERSION, LoadMethod.HR: HR_VERSION,
                LoadMethod.DURATION: DURATION_VERSION}

    def result(status, value=None):
        return ActivityLoad(activity.canonical_id, method, status, value,
            'TSS' if status == LoadStatus.COMPUTED else None, tuple(inputs), tuple(used),
            versions[method], tuple(reasons), activity.projection_version, duration)

    if method == LoadMethod.HR:
        for name in ('average_heart_rate_bpm', 'max_heart_rate_bpm'):
            number(name, 'bpm')
        for kind in ('threshold_hr', 'resting_hr', 'maximum_hr'):
            threshold, reason = _threshold(config, kind, start)
            if threshold is not None:
                used.append(threshold)
            elif reason.startswith('ambiguous_'):
                reasons.append(reason)
        reasons.append('unsupported_method')
        return result(LoadStatus.INVALID_INPUTS if invalid else LoadStatus.UNSUPPORTED_METHOD)
    if method == LoadMethod.DURATION:
        number('elapsed_time_s', 's')
        reasons.append('duration_only_no_load_formula')
        return result(LoadStatus.INVALID_INPUTS if invalid else LoadStatus.UNSUPPORTED_METHOD)

    sport = fields.get('sport')
    if sport is None or sport.value != 'cycling':
        reasons.append('unsupported_sport')
        return result(LoadStatus.UNSUPPORTED_METHOD)
    inputs.append(LoadInput(sport, 'classification'))
    np = number('normalized_power_w', 'W')
    # Default inventory inspects elapsed time, but cannot use it for scoring
    # until the caller establishes its match to the NP period explicitly.
    seconds = number(duration.field_name if duration else 'elapsed_time_s', 's')
    if duration is not None and duration.field_name == 'timer_time_s':
        timer_field = fields.get('timer_time_s')
        np_field = fields.get('normalized_power_w')
        if (timer_field is not None and np_field is not None and seconds is not None
                and np is not None and timer_field.selected_from != np_field.selected_from):
            reasons.append('duration_power_source_mismatch')
            invalid = True
        elapsed = fields.get('elapsed_time_s')
        if (elapsed is not None and _numeric(elapsed.value) and _numeric(seconds)
                and seconds > elapsed.value):
            reasons.append('timer_time_exceeds_elapsed_time')
            invalid = True
    if duration is None:
        reasons.append('unverified_duration_basis')
    ftp, reason = _threshold(config, 'ftp', start)
    if reason:
        reasons.append(reason)
        invalid |= reason == 'ambiguous_ftp'
    else:
        used.append(ftp)
    if reasons:
        return result(LoadStatus.INVALID_INPUTS if invalid else LoadStatus.INSUFFICIENT_INPUTS)
    with localcontext(Context(prec=34, rounding=ROUND_HALF_EVEN)):
        score = (Decimal(str(seconds)) / Decimal(3600)
                 * (Decimal(str(np)) / Decimal(str(ftp.value))) ** 2 * Decimal(100))
        value = float(score)
    if not math.isfinite(value) or (score != 0 and value == 0):
        reasons.append('unrepresentable_load')
        return result(LoadStatus.INVALID_INPUTS)
    return result(LoadStatus.COMPUTED, value)


def calculate_activity_load(activity: CanonicalActivity,
        thresholds: ThresholdConfiguration = ThresholdConfiguration(),
        method: LoadMethod | str = LoadMethod.POWER, *,
        duration_reference: DurationReference | None = None) -> ActivityLoad:
    """Compute one method. Malformed structure/config raises fixed-code errors.

    Missing/invalid metrics and overlapping applicable FTP periods abstain in
    the immutable result. Unknown method names are rejected, never defaulted.
    """
    try:
        return _calculate(activity, thresholds, LoadMethod(method), duration_reference)
    except TrainingLoadError:
        raise
    except Exception:
        raise TrainingLoadError('invalid_load_input') from None


def summarize_load_coverage(history: ActivityHistory,
        thresholds: ThresholdConfiguration = ThresholdConfiguration(), *,
        duration_references: tuple[DurationReference, ...] = ()) -> LoadCoverage:
    """Inventory only the supplied history; counts never sum load scores.

    Power activity inputs means cycling + valid NP + available elapsed time
    (or explicitly selected duration). Full prerequisites also require unique
    effective FTP and declared duration basis. HR configuration coverage means
    unique threshold_hr only, not sufficient inputs for an established formula.
    Reasons are multi-label: their counts need not sum to activity count.
    """
    try:
        _validate_config(thresholds)
        summarize_activity_history(history)  # Existing canonical history validation.
        if type(duration_references) is not tuple or any(
                not isinstance(d, DurationReference) for d in duration_references):
            raise TrainingLoadError('invalid_duration_references')
        durations = {d.canonical_id: d for d in duration_references}
        ids = {a.canonical_id for a in history.activities}
        if len(durations) != len(duration_references) or not durations.keys() <= ids:
            raise TrainingLoadError('invalid_duration_references')
        counts, reasons = Counter(), Counter()
        scored = Counter({method.value: 0 for method in LoadMethod})
        unsupported = Counter({method.value: 0 for method in LoadMethod})
        for activity in history.activities:
            fields = _fields(activity)
            value = lambda name: fields[name].value if name in fields else None
            start = _utc(value('start_time'))
            duration = durations.get(activity.canonical_id)
            has_np = _numeric(value('normalized_power_w'))
            counts['np'] += has_np
            counts['power'] += any(value(n) is not None for n in ('average_power_w', 'max_power_w'))
            counts['inputs'] += (has_np and value('sport') == 'cycling'
                and _numeric(value(duration.field_name if duration else 'elapsed_time_s')))
            counts['missing_ftp'] += _threshold(thresholds, 'ftp', start)[0] is None
            counts['hr'] += any(value(n) is not None for n in ('average_heart_rate_bpm', 'max_heart_rate_bpm'))
            counts['missing_hr'] += _threshold(thresholds, 'threshold_hr', start)[0] is None
            for method in LoadMethod:
                load = calculate_activity_load(activity, thresholds, method, duration_reference=duration)
                if method == LoadMethod.POWER:
                    counts['prerequisites'] += (load.status == LoadStatus.COMPUTED
                        or load.reasons == ('unrepresentable_load',))
                scored[method.value] += load.status == LoadStatus.COMPUTED
                unsupported[method.value] += load.status == LoadStatus.UNSUPPORTED_METHOD
                for reason in load.reasons:
                    reasons[method.value, reason] += 1
        return LoadCoverage(len(ids), counts['power'], counts['np'], counts['inputs'],
            counts['prerequisites'], len(ids) - counts['np'], counts['missing_ftp'], counts['hr'],
            counts['missing_hr'], tuple(sorted(unsupported.items())),
            tuple(sorted(scored.items())), tuple((m, r, n) for (m, r), n in sorted(reasons.items())))
    except TrainingLoadError:
        raise
    except Exception:
        raise TrainingLoadError('invalid_load_history') from None
