"""Compose History, Analytics and Training Load without changing their rules."""

from dataclasses import dataclass, field, replace
from datetime import timezone
from pathlib import Path

from src.analytics import ActivityAnalytics, summarize_activity_history
from src.history import ActivityHistory, HistoryFilter, read_activity_history
from src.training_load import (
    ActivityLoad, DurationReference, LoadCoverage, ThresholdConfiguration,
    calculate_activity_load, summarize_load_coverage,
)


REPORT_VERSION = 'personal-training-report-v1'


class TrainingReportError(ValueError):
    """Fixed stage code without private values or upstream exception messages."""


@dataclass(frozen=True)
class PersonalTrainingReport:
    """Private derived artifact. History retains all canonical field provenance.

    None selection means an externally supplied history of unspecified scope.
    A HistoryFilter records the exact selection used by the read-only reader.
    No wall clock is included, so the same snapshot/inputs yield the same report.
    """
    history: ActivityHistory = field(repr=False)
    analytics: ActivityAnalytics = field(repr=False)
    load_coverage: LoadCoverage
    power_loads: tuple[ActivityLoad, ...] = field(repr=False)
    selection: HistoryFilter | None = None
    report_version: str = REPORT_VERSION


def build_personal_training_report(
    history: ActivityHistory, *, aggregation_timezone: str = 'UTC',
    thresholds: ThresholdConfiguration = ThresholdConfiguration(),
    duration_references: tuple[DurationReference, ...] = (),
) -> PersonalTrainingReport:
    """Analyze exactly the supplied history. Missing load inputs still abstain.

    Pure composition; callers must establish any duration assertions themselves.
    HR and duration remain inventory-only; no mixed-method totals are created.
    """
    try:
        analytics = summarize_activity_history(history, aggregation_timezone=aggregation_timezone)
        ordered = replace(history, activities=tuple(sorted(history.activities,
            key=lambda a: (a.selected('start_time').value.astimezone(timezone.utc), a.canonical_id))))
    except Exception:
        raise TrainingReportError('report_analytics_failed') from None
    try:
        coverage = summarize_load_coverage(ordered, thresholds,
                                           duration_references=duration_references)
        durations = {d.canonical_id: d for d in duration_references}
        loads = tuple(calculate_activity_load(a, thresholds,
            duration_reference=durations.get(a.canonical_id)) for a in ordered.activities)
    except Exception:
        raise TrainingReportError('report_load_failed') from None
    return PersonalTrainingReport(ordered, analytics, coverage, loads)


def read_personal_training_report(
    database_path: Path, filters: HistoryFilter | None = None, *,
    aggregation_timezone: str = 'UTC',
    thresholds: ThresholdConfiguration = ThresholdConfiguration(),
    duration_references: tuple[DurationReference, ...] = (),
) -> PersonalTrainingReport:
    """Read a quiescent database through History; no writes or raw source reads."""
    selection = HistoryFilter() if filters is None else filters
    try:
        history = read_activity_history(database_path, selection)
    except Exception:
        raise TrainingReportError('report_history_failed') from None
    report = build_personal_training_report(history, aggregation_timezone=aggregation_timezone,
        thresholds=thresholds, duration_references=duration_references)
    return replace(report, selection=selection)
