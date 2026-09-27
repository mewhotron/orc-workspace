"""Pure, per-method load calculation over canonical activities."""

from .service import (
    POWER_TSS_VERSION, HR_VERSION, DURATION_VERSION, ActivityLoad,
    DurationReference, LoadCoverage, LoadInput, LoadMethod, LoadStatus,
    ThresholdConfiguration, ThresholdReference, TrainingLoadError,
    calculate_activity_load, summarize_load_coverage,
)
from .configuration import (
    THRESHOLD_CONFIG_VERSION, load_threshold_configuration, parse_threshold_configuration,
)

__all__ = [
    'POWER_TSS_VERSION', 'HR_VERSION', 'DURATION_VERSION', 'ActivityLoad',
    'DurationReference', 'LoadCoverage', 'LoadInput', 'LoadMethod', 'LoadStatus',
    'ThresholdConfiguration', 'ThresholdReference', 'TrainingLoadError',
    'calculate_activity_load', 'summarize_load_coverage',
    'THRESHOLD_CONFIG_VERSION', 'load_threshold_configuration', 'parse_threshold_configuration',
]
