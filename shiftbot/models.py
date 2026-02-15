from dataclasses import dataclass
from typing import Optional

STATUS_IDLE = "IDLE"
STATUS_IN = "IN"
STATUS_OUT = "OUT"
STATUS_UNKNOWN = "UNKNOWN"


@dataclass
class ShiftSession:
    user_id: int
    chat_id: int
    active: bool = False

    last_ping_ts: float = 0.0
    last_valid_ping_ts: float = 0.0

    out_streak: int = 0
    last_warn_ts: float = 0.0

    last_stale_notify_ts: float = 0.0

    last_distance_m: Optional[float] = None
    last_accuracy_m: Optional[float] = None
    last_status: str = STATUS_IDLE
    last_notified_status: str = STATUS_IDLE
