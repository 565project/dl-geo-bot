import time
from dataclasses import dataclass


@dataclass
class ActiveShiftState:
    staff_id: int
    point_id: int | None
    last_sig: str
    last_bucket: int
    last_seen_ts: float


@dataclass
class PairState:
    streak: int = 0
    last_bucket: int | None = None
    last_sig: str | None = None
    last_alert_ts: float = 0.0


class DeadSoulDetector:
    def __init__(
        self,
        *,
        bucket_sec: int,
        window_sec: int,
        streak_threshold: int,
        alert_cooldown_sec: int,
    ) -> None:
        self.bucket_sec = bucket_sec
        self.window_sec = window_sec
        self.streak_threshold = streak_threshold
        self.alert_cooldown_sec = alert_cooldown_sec
        self._active_shifts: dict[int, ActiveShiftState] = {}
        self._pair_states: dict[tuple[int, int], PairState] = {}

    @staticmethod
    def pair_key(staff_a: int, staff_b: int) -> tuple[int, int]:
        left, right = sorted((int(staff_a), int(staff_b)))
        return left, right

    def remove_shift(self, shift_id: int) -> None:
        state = self._active_shifts.pop(int(shift_id), None)
        if not state:
            return
        staff_id = int(state.staff_id)
        keys_to_drop = [key for key in self._pair_states if staff_id in key]
        for key in keys_to_drop:
            self._pair_states.pop(key, None)

    def register_ping(
        self,
        *,
        shift_id: int,
        staff_id: int,
        point_id: int | None,
        sig: str,
        now_ts: float | None = None,
    ) -> list[dict]:
        now = now_ts or time.time()
        bucket = int(now // self.bucket_sec)
        alerts: list[dict] = []

        shift_key = int(shift_id)
        current = ActiveShiftState(
            staff_id=int(staff_id),
            point_id=int(point_id) if point_id is not None else None,
            last_sig=sig,
            last_bucket=bucket,
            last_seen_ts=now,
        )

        for other_shift_id, other in self._active_shifts.items():
            if other_shift_id == shift_key:
                continue
            if current.point_id is None or other.point_id != current.point_id:
                continue

            pair_key = self.pair_key(current.staff_id, other.staff_id)
            pair_state = self._pair_states.get(pair_key) or PairState()

            is_fresh = (now - other.last_seen_ts) <= self.window_sec
            if is_fresh and other.last_sig == sig:
                if pair_state.last_bucket is not None and bucket == pair_state.last_bucket + 1 and pair_state.last_sig == sig:
                    pair_state.streak += 1
                else:
                    pair_state.streak = 1
                pair_state.last_bucket = bucket
                pair_state.last_sig = sig

                if pair_state.streak >= self.streak_threshold and (now - pair_state.last_alert_ts) >= self.alert_cooldown_sec:
                    pair_state.last_alert_ts = now
                    alerts.append(
                        {
                            "staff_a": pair_key[0],
                            "staff_b": pair_key[1],
                            "point_id": current.point_id,
                            "sig": sig,
                            "streak": pair_state.streak,
                        }
                    )
            elif pair_state.last_bucket is not None and bucket == pair_state.last_bucket + 1 and pair_state.last_sig != sig:
                pair_state.streak = 0
                pair_state.last_bucket = bucket
                pair_state.last_sig = sig

            self._pair_states[pair_key] = pair_state

        self._active_shifts[shift_key] = current
        return alerts
