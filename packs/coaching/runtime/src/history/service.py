"""Compose existing read-only storage, linkage and projection without analytics.

Inventory describes the filtered canonical activities. Diagnostics and unresolved
groups describe the complete database snapshot, so filters cannot hide quality
issues. No raw sources are read and no derived results are persisted.
"""

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from src.ingestion.activity_storage import load_activity_observations
from src.linkage.activity import LinkStatus, ObservationReference, link_activity_observations
from src.projection.activity import CanonicalActivity, ProjectionConflict, project_canonical_activities


class ActivityHistoryError(ValueError):
    """Fixed public diagnostic code; upstream exception text is never exposed."""


@dataclass(frozen=True)
class HistoryFilter:
    """Canonical start-time interval [start, end); exact sport/subtype strings.

    None means no constraint. All timestamps must be aware. Equal bounds yield
    an empty interval; reversed bounds are invalid. No fuzzy matching is used.
    """
    start: datetime | None = None
    end: datetime | None = None
    sport: str | None = None
    subtype: str | None = None


@dataclass(frozen=True)
class HistoryInventory:
    total_activities: int
    earliest_start: datetime | None
    latest_start: datetime | None
    counts_by_sport: tuple[tuple[str, int], ...]
    counts_by_subtype: tuple[tuple[str | None, int], ...]


@dataclass(frozen=True)
class HistoryDiagnostics:
    """Counts for the entire source snapshot, before canonical filtering."""
    source_observation_count: int
    strong_link_count: int
    authoritative_link_count: int
    linked_pair_count: int
    linkage_ambiguous_group_count: int
    ambiguous_group_count: int
    projection_conflict_count: int
    singleton_count: int


@dataclass(frozen=True)
class ActivityHistory:
    activities: tuple[CanonicalActivity, ...]
    ambiguous_groups: tuple[tuple[ObservationReference, ...], ...]
    conflicts: tuple[ProjectionConflict, ...]
    inventory: HistoryInventory
    diagnostics: HistoryDiagnostics
    unfiltered_activity_count: int
    linkage_version: str
    projection_version: str


def _validate_filter(filters: HistoryFilter) -> None:
    if not isinstance(filters, HistoryFilter):
        raise ActivityHistoryError('invalid_history_filter')
    for value in (filters.start, filters.end):
        if value is not None and (not isinstance(value, datetime) or value.utcoffset() is None):
            raise ActivityHistoryError('history_filter_requires_aware_datetime')
    if (filters.start is not None and filters.end is not None
            and filters.end.astimezone(timezone.utc) < filters.start.astimezone(timezone.utc)):
        raise ActivityHistoryError('invalid_history_date_range')
    for value in (filters.sport, filters.subtype):
        if value is not None and (not isinstance(value, str) or not value.strip()
                or value != value.strip() or any(ord(c) < 32 for c in value)):
            raise ActivityHistoryError('invalid_history_classification_filter')


def _start(activity: CanonicalActivity) -> datetime:
    value = activity.selected('start_time').value
    if not isinstance(value, datetime) or value.utcoffset() is None:
        raise ActivityHistoryError('invalid_history_projection')
    return value.astimezone(timezone.utc)


def read_activity_history(
    database_path: Path, filters: HistoryFilter | None = None,
) -> ActivityHistory:
    """Read a quiescent activity DB and return chronological canonical history.

    Ordering is ascending UTC start, then canonical ID. Retained canonical
    objects preserve contributors, external IDs and every field's provenance.
    Existing sidecars or WAL-mode headers cause a refusal before SQLite opens,
    avoiding journal recovery or shared-memory initialization. The caller must not run
    concurrent database writers during inspection.
    """
    filters = HistoryFilter() if filters is None else filters
    try:
        _validate_filter(filters)
    except ActivityHistoryError:
        raise
    except Exception:
        raise ActivityHistoryError('invalid_history_filter') from None
    if not isinstance(database_path, Path):
        raise ActivityHistoryError('invalid_history_database_path')
    try:
        resolved = database_path.resolve()
        if any(Path(str(resolved) + suffix).exists() for suffix in ('-wal', '-shm', '-journal')):
            raise ActivityHistoryError('history_database_has_sidecars')
        # SQLite's read/write format bytes identify WAL even after clean close
        # removes sidecars. This file-level safety check does not read SQL rows;
        # schema and observation validation remain exclusively in the loader.
        with resolved.open('rb') as source:
            header = source.read(20)
        if header[:16] == b'SQLite format 3\x00' and 2 in header[18:20]:
            raise ActivityHistoryError('history_database_uses_wal')
        observations = load_activity_observations(database_path)
    except ActivityHistoryError:
        raise
    except Exception:
        raise ActivityHistoryError('history_storage_failed') from None
    try:
        linkage = link_activity_observations(observations)
    except Exception:
        raise ActivityHistoryError('history_linkage_failed') from None
    try:
        projection = project_canonical_activities(observations, linkage)
        activities = tuple(sorted((a for a in projection.activities
            if (filters.start is None or _start(a) >= filters.start.astimezone(timezone.utc))
            and (filters.end is None or _start(a) < filters.end.astimezone(timezone.utc))
            and (filters.sport is None or a.selected('sport').value == filters.sport)
            and (filters.subtype is None or a.selected('subtype').value == filters.subtype)),
            key=lambda a: (_start(a), a.canonical_id)))
        sports = Counter(a.selected('sport').value for a in activities)
        subtypes = Counter(a.selected('subtype').value for a in activities)
        inventory = HistoryInventory(len(activities), _start(activities[0]) if activities else None,
            _start(activities[-1]) if activities else None, tuple(sorted(sports.items())),
            tuple(sorted(subtypes.items(), key=lambda item: (item[0] is not None, item[0] or ''))))
        strong = sum(link.status == LinkStatus.STRONG_CANDIDATE for link in linkage.links)
        authoritative = sum(link.status == LinkStatus.AUTHORITATIVE for link in linkage.links)
        diagnostics = HistoryDiagnostics(len(observations), strong, authoritative, strong + authoritative,
            len(linkage.ambiguous_groups), len(projection.ambiguous_groups), len(projection.conflicts),
            sum(len(a.contributors) == 1 for a in projection.activities))
        return ActivityHistory(activities, projection.ambiguous_groups, projection.conflicts,
            inventory, diagnostics, len(projection.activities), linkage.rule_version, projection.projection_version)
    except Exception:
        raise ActivityHistoryError('history_projection_failed') from None
