"""Public read-only activity history boundary for future application consumers."""

from .service import (
    ActivityHistory, ActivityHistoryError, HistoryDiagnostics, HistoryFilter,
    HistoryInventory, read_activity_history,
)
from src.projection.activity import CanonicalActivity, ProjectionConflict
from src.linkage.activity import ObservationReference

__all__ = [
    'ActivityHistory', 'ActivityHistoryError', 'HistoryDiagnostics', 'HistoryFilter',
    'HistoryInventory', 'read_activity_history', 'CanonicalActivity',
    'ProjectionConflict', 'ObservationReference',
]
