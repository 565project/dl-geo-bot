from dataclasses import dataclass, field
from typing import Optional

STATUS_IDLE = "IDLE"
STATUS_IN = "IN"
STATUS_OUT = "OUT"
STATUS_UNKNOWN = "UNKNOWN"

MODE_IDLE = "idle"
MODE_CHOOSE_POINT = "choose_point"
MODE_CHOOSE_ROLE = "choose_role"
MODE_AWAITING_LOCATION = "awaiting_location"
MODE_REPORT_ISSUE = "report_issue"


@dataclass
class ShiftSession:
    user_id: int
    chat_id: int
    active: bool = False

    mode: str = MODE_IDLE
    points_cache: list[dict] = field(default_factory=list)
    selected_point_index: Optional[int] = None
    selected_role: Optional[str] = None

    active_shift_id: Optional[int] = None
    active_point_id: Optional[int] = None
    active_point_name: Optional[str] = None
    active_point_lat: Optional[float] = None
    active_point_lon: Optional[float] = None
    active_point_radius: Optional[float] = None
    active_role: Optional[str] = None
    active_started_at: Optional[str] = None
    consecutive_out_count: int = 0
    last_out_warn_at: float = 0.0
    last_admin_alert_at: float = 0.0

    # legacy/runtime геополей для статуса и фоновых задач
    last_ping_ts: float = 0.0
    last_valid_ping_ts: float = 0.0
    out_streak: int = 0
    last_warn_ts: float = 0.0
    last_stale_notify_ts: float = 0.0
    last_distance_m: Optional[float] = None
    last_accuracy_m: Optional[float] = None
    last_status: str = STATUS_IDLE
    last_notified_status: str = STATUS_IDLE

    @property
    def awaiting_location(self) -> bool:
        return self.mode == MODE_AWAITING_LOCATION
