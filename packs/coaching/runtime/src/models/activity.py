from dataclasses import dataclass, field
from datetime import datetime

from src.models.provenance import SourceFile


@dataclass(frozen=True)
class ActivityMetrics:
    elapsed_time_s: float | None = None
    moving_time_s: float | None = None

    distance_m: float | None = None
    elevation_gain_m: float | None = None
    elevation_loss_m: float | None = None

    average_speed_mps: float | None = None
    max_speed_mps: float | None = None

    average_heart_rate_bpm: float | None = None
    max_heart_rate_bpm: float | None = None

    average_cadence_rpm: float | None = None
    max_cadence_rpm: float | None = None

    average_power_w: float | None = None
    max_power_w: float | None = None
    normalized_power_w: float | None = None

    work_kj: float | None = None
    calories_kcal: float | None = None

    # FIT session timer duration, excluding pauses; never elapsed/moving time.
    timer_time_s: float | None = None


@dataclass(frozen=True)
class ActivityRecord:
    record_id: str

    source_file: SourceFile
    source_activity_id: str | None
    source_record_index: int | None

    start_time: datetime
    end_time: datetime | None

    sport: str
    subtype: str | None
    source_activity_type: str | None

    name: str | None = None

    origin_platform: str | None = None
    device_manufacturer: str | None = None
    device_model: str | None = None

    is_indoor: bool | None = None
    is_manual: bool | None = None

    metrics: ActivityMetrics = field(
        default_factory=ActivityMetrics
    )
