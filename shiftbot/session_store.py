from typing import Dict

from shiftbot.models import MODE_IDLE, STATUS_IDLE, ShiftSession


class SessionStore:
    def __init__(self) -> None:
        self._sessions: Dict[int, ShiftSession] = {}

    def get_or_create(self, user_id: int, chat_id: int) -> ShiftSession:
        session = self._sessions.get(user_id)
        if not session:
            session = ShiftSession(user_id=user_id, chat_id=chat_id)
            self._sessions[user_id] = session
        else:
            session.chat_id = chat_id
        return session

    def reset_flow(self, session: ShiftSession) -> None:
        session.mode = MODE_IDLE
        session.points_cache = []
        session.selected_point_index = None
        session.selected_point_id = None
        session.selected_point_name = None
        session.selected_point_address = None
        session.selected_point_lat = None
        session.selected_point_lon = None
        session.selected_point_radius = None
        session.selected_role = None
        session.gate_attempt = 0
        session.gate_last_reason = None

    def patch(self, session: ShiftSession, **changes) -> None:
        for key, value in changes.items():
            setattr(session, key, value)

    def clear_shift_state(self, session: ShiftSession) -> None:
        session.active = False
        session.active_shift_id = None
        session.active_point_id = None
        session.active_point_name = None
        session.active_point_lat = None
        session.active_point_lon = None
        session.active_point_radius = None
        session.active_role = None
        session.active_started_at = None
        session.consecutive_out_count = 0
        session.last_out_warn_at = 0.0
        session.last_admin_alert_at = 0.0
        session.last_out_violation_notified_round = 0
        session.last_unknown_warn_ts = 0.0
        session.last_status = STATUS_IDLE
        session.last_notified_status = STATUS_IDLE
        session.last_ping_ts = 0.0
        session.last_notify_ts = 0.0
        session.last_valid_ping_ts = 0.0
        session.last_lat = None
        session.last_lon = None
        session.last_acc = None
        session.last_dist_m = None
        session.same_gps_signature = None
        session.last_distance_m = None
        session.last_accuracy_m = None
        session.out_streak = 0
        session.last_bucket_key = None
        session.same_bucket_hits = 0
        session.last_warn_ts = 0.0
        session.last_stale_notify_ts = 0.0
        session.gate_attempt = 0
        session.gate_last_reason = None

    def values(self):
        return self._sessions.values()

    def is_empty(self) -> bool:
        return not self._sessions
