"""Descriptive analytics only; no storage, filtering or physiological modeling.

Only selected canonical values contribute. Missing values are not imputed.
Coverage families mean at least one listed member is present; exact-field
coverage is also exposed so average/max availability remains distinguishable.
No per-activity averages are combined. Inventory describes analyzed activities;
global diagnostics and unfiltered count retain the underlying snapshot context.
"""

from collections import Counter, defaultdict
from dataclasses import dataclass, fields
from datetime import datetime, time, timedelta, timezone
import math
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from src.history import ActivityHistory, CanonicalActivity, HistoryDiagnostics, HistoryInventory


ANALYTICS_VERSION = 'activity-analytics-v1'
_ADDITIVE = ('elapsed_time_s', 'moving_time_s', 'distance_m', 'elevation_gain_m',
             'elevation_loss_m', 'work_kj', 'calories_kcal')
_NUMERIC = _ADDITIVE + (
    'average_speed_mps', 'max_speed_mps', 'average_heart_rate_bpm', 'max_heart_rate_bpm',
    'average_cadence_rpm', 'max_cadence_rpm', 'average_power_w', 'max_power_w',
    'normalized_power_w',
)
_DEVICE = ('device_manufacturer', 'device_model')
_FAMILIES = (
    ('heart_rate', ('average_heart_rate_bpm', 'max_heart_rate_bpm')),
    ('cadence', ('average_cadence_rpm', 'max_cadence_rpm')),
    ('power', ('average_power_w', 'max_power_w')),
    ('device_metadata', _DEVICE),
)


class ActivityAnalyticsError(ValueError):
    """Fixed diagnostic code without private canonical values or upstream text."""


@dataclass(frozen=True)
class MetricCoverage:
    metric: str
    available_count: int
    activity_count: int

    @property
    def fraction(self) -> float:
        """Empty history has zero coverage (not a claim of complete data)."""
        return self.available_count / self.activity_count if self.activity_count else 0.0


@dataclass(frozen=True)
class MetricTotal:
    value: float | None
    coverage: MetricCoverage


@dataclass(frozen=True)
class ActivityTotals:
    """Available-value sums in named units, each with its own coverage."""
    elapsed_time_s: MetricTotal
    moving_time_s: MetricTotal
    distance_m: MetricTotal
    elevation_gain_m: MetricTotal
    elevation_loss_m: MetricTotal
    work_kj: MetricTotal
    calories_kcal: MetricTotal


@dataclass(frozen=True)
class WeeklyActivitySummary:
    """Nonempty local week [week_start, week_end), Monday midnight boundaries."""
    week_start: datetime
    week_end: datetime
    activity_count: int
    active_days: int
    counts_by_sport: tuple[tuple[str, int], ...]
    counts_by_subtype: tuple[tuple[str | None, int], ...]
    totals: ActivityTotals
    coverage: tuple[MetricCoverage, ...]


@dataclass(frozen=True)
class ActivityAnalytics:
    activity_count: int
    active_days: int
    weeks_represented: int
    counts_by_sport: tuple[tuple[str, int], ...]
    counts_by_subtype: tuple[tuple[str | None, int], ...]
    totals: ActivityTotals
    coverage: tuple[MetricCoverage, ...]
    weeks: tuple[WeeklyActivitySummary, ...]
    history_inventory: HistoryInventory
    global_diagnostics: HistoryDiagnostics
    unfiltered_activity_count: int
    linkage_version: str
    projection_version: str
    aggregation_timezone: str
    analytics_version: str = ANALYTICS_VERSION


def _calendar_zone(key: str):
    if not isinstance(key, str) or not key or key != key.strip():
        raise ActivityAnalyticsError('invalid_analytics_timezone')
    try:
        return ZoneInfo(key)
    except ZoneInfoNotFoundError:
        # UTC needs no external rules. Non-UTC zones require local IANA data
        # discoverable by ZoneInfo (e.g. PYTHONTZPATH on Windows).
        if key == 'UTC':
            return timezone.utc
        raise ActivityAnalyticsError('analytics_timezone_unavailable') from None
    except (ValueError, TypeError):
        raise ActivityAnalyticsError('invalid_analytics_timezone') from None


def _text(value, *, optional=False):
    if optional and value is None:
        return
    if (not isinstance(value, str) or not value.strip() or value != value.strip()
            or any(ord(c) < 32 for c in value)):
        raise ActivityAnalyticsError('invalid_analytics_classification_or_metadata')


def _counts(rows, name):
    counts = Counter(row[name] for row in rows)
    return tuple(sorted(counts.items(), key=lambda item: (item[0] is not None, item[0] or '')))


def _aggregate(rows):
    count = len(rows)
    coverage = tuple(MetricCoverage(name, sum(row[name] is not None for row in rows), count)
                     for name in _NUMERIC + _DEVICE)
    coverage += tuple(MetricCoverage(name,
        sum(any(row[member] is not None for member in members) for row in rows), count)
        for name, members in _FAMILIES)
    by_name = {item.metric: item for item in coverage}
    totals = {}
    for name in _ADDITIVE:
        values = [row[name] for row in rows if row[name] is not None]
        try:
            total = math.fsum(values) if values else None
        except (OverflowError, ValueError):
            raise ActivityAnalyticsError('invalid_analytics_total') from None
        if total is not None and (not math.isfinite(total) or total < 0):
            raise ActivityAnalyticsError('invalid_analytics_total')
        totals[name] = MetricTotal(total, by_name[name])
    return ActivityTotals(**totals), coverage


def _summarize(history, zone_key, zone):
    if not isinstance(history, ActivityHistory) or not isinstance(history.activities, tuple):
        raise ActivityAnalyticsError('invalid_analytics_history')
    rows = []
    ids, contributors = set(), set()
    unresolved = {ref for group in history.ambiguous_groups for ref in group}
    unresolved.update(ref for conflict in history.conflicts for ref in conflict.observations)
    for activity in history.activities:
        if not isinstance(activity, CanonicalActivity):
            raise ActivityAnalyticsError('invalid_analytics_activity')
        if (not isinstance(activity.canonical_id, str) or not activity.canonical_id
                or activity.canonical_id in ids or not activity.contributors
                or activity.linkage_status not in ('unlinked', 'strong_candidate', 'authoritative')
                or activity.projection_version != history.projection_version):
            raise ActivityAnalyticsError('invalid_analytics_canonical_identity')
        ids.add(activity.canonical_id)
        refs = set(activity.contributors)
        if len(refs) != len(activity.contributors) or refs & (contributors | unresolved):
            raise ActivityAnalyticsError('invalid_analytics_contributors')
        contributors.update(refs)
        row = {item.name: item.value for item in activity.fields}
        if len(row) != len(activity.fields):
            raise ActivityAnalyticsError('duplicate_analytics_field')
        # Projection v1 supplies every field, with None for unavailable values.
        if not set(_NUMERIC + _DEVICE + ('start_time', 'sport', 'subtype')) <= row.keys():
            raise ActivityAnalyticsError('missing_analytics_canonical_field')
        start = row['start_time']
        if not isinstance(start, datetime) or start.utcoffset() is None:
            raise ActivityAnalyticsError('analytics_requires_aware_start')
        row['_utc'] = start.astimezone(timezone.utc)
        row['_day'] = start.astimezone(zone).date()
        row['_id'] = activity.canonical_id
        _text(row['sport'])
        _text(row['subtype'], optional=True)
        for name in _DEVICE:
            _text(row[name], optional=True)
        for name in _NUMERIC:
            value = row[name]
            if value is not None and (type(value) not in (int, float)
                    or not math.isfinite(value) or value < 0):
                raise ActivityAnalyticsError('invalid_analytics_metric')
        rows.append(row)
    rows.sort(key=lambda row: (row['_utc'], row['_id']))
    inventory = HistoryInventory(len(rows), rows[0]['_utc'] if rows else None,
        rows[-1]['_utc'] if rows else None, _counts(rows, 'sport'), _counts(rows, 'subtype'))
    if history.inventory != inventory:
        raise ActivityAnalyticsError('inconsistent_analytics_inventory')
    diag = history.diagnostics
    if not isinstance(diag, HistoryDiagnostics) or any(
            type(getattr(diag, f.name)) is not int or getattr(diag, f.name) < 0 for f in fields(diag)):
        raise ActivityAnalyticsError('invalid_analytics_diagnostics')
    if (type(history.unfiltered_activity_count) is not int
            or not len(rows) <= history.unfiltered_activity_count <= diag.source_observation_count
            or diag.singleton_count > history.unfiltered_activity_count
            or diag.ambiguous_group_count != len(history.ambiguous_groups)
            or diag.projection_conflict_count != len(history.conflicts)
            or len(contributors) > diag.source_observation_count):
        raise ActivityAnalyticsError('inconsistent_analytics_diagnostics')
    for version in (history.linkage_version, history.projection_version):
        _text(version)
    groups = defaultdict(list)
    for row in rows:
        day = row['_day']
        groups[day - timedelta(days=day.weekday())].append(row)
    weeks = []
    for monday, members in sorted(groups.items()):
        totals, coverage = _aggregate(members)
        weeks.append(WeeklyActivitySummary(
            datetime.combine(monday, time.min, zone),
            datetime.combine(monday + timedelta(days=7), time.min, zone),
            len(members), len({row['_day'] for row in members}),
            _counts(members, 'sport'), _counts(members, 'subtype'), totals, coverage))
    totals, coverage = _aggregate(rows)
    active_days = len({row['_day'] for row in rows})
    if sum(week.activity_count for week in weeks) != len(rows) or active_days > len(rows):
        raise ActivityAnalyticsError('inconsistent_analytics_calendar')
    for name in _ADDITIVE:
        overall = getattr(totals, name)
        weekly = [getattr(week.totals, name) for week in weeks]
        if sum(item.coverage.available_count for item in weekly) != overall.coverage.available_count:
            raise ActivityAnalyticsError('inconsistent_analytics_coverage')
        if overall.value is not None and not math.isclose(
                math.fsum(item.value for item in weekly if item.value is not None),
                overall.value, rel_tol=1e-12, abs_tol=1e-9):
            raise ActivityAnalyticsError('inconsistent_analytics_totals')
    return ActivityAnalytics(len(rows), active_days, len(weeks), inventory.counts_by_sport,
        inventory.counts_by_subtype, totals, coverage, tuple(weeks), history.inventory,
        diag, history.unfiltered_activity_count, history.linkage_version,
        history.projection_version, zone_key)


def summarize_activity_history(
    history: ActivityHistory, *, aggregation_timezone: str = 'UTC',
) -> ActivityAnalytics:
    """Analyze exactly the supplied history; use HistoryFilter before this call.

    Calendar dates use activity starts, not end times or duration splitting.
    Week end is exclusive. IANA rules come from ZoneInfo's configured database;
    UTC also works without installed timezone data. Invalid or unavailable zones
    fail with fixed codes. No input objects are modified or persisted.
    """
    try:
        zone = _calendar_zone(aggregation_timezone)
        return _summarize(history, aggregation_timezone, zone)
    except ActivityAnalyticsError:
        raise
    except Exception:
        raise ActivityAnalyticsError('invalid_analytics_input') from None
