import time
from dataclasses import dataclass
from typing import Dict


@dataclass
class PairState:
    streak: int = 0
    last_sig: str | None = None
    last_notify_ts: float = 0.0


class LiveShiftRegistry:
    def __init__(self) -> None:
        self._shifts: Dict[int, dict] = {}
        self._pair_states: Dict[str, PairState] = {}

    @staticmethod
    def pair_key(shift_a: int, shift_b: int) -> str:
        left, right = sorted((int(shift_a), int(shift_b)))
        return f"{left}:{right}"

    def cleanup_stale(self, stale_timeout_sec: int, now_ts: float | None = None) -> None:
        now = now_ts or time.time()
        stale_ids = [
            shift_id
            for shift_id, data in self._shifts.items()
            if (now - float(data.get("last_seen_ts", 0.0))) > stale_timeout_sec
        ]
        for shift_id in stale_ids:
            self.remove_shift(shift_id)

    def upsert_shift(
        self,
        *,
        shift_id: int,
        staff_id: int | None,
        tg_user_id: int | None,
        point_id: int | None,
        bucket_key: str,
        now_ts: float | None = None,
    ) -> None:
        self._shifts[int(shift_id)] = {
            "staff_id": staff_id,
            "tg_user_id": tg_user_id,
            "point_id": point_id,
            "last_bucket_key": bucket_key,
            "same_gps_streak": 0,
            "last_seen_ts": now_ts or time.time(),
        }

    def remove_shift(self, shift_id: int) -> None:
        sid = int(shift_id)
        self._shifts.pop(sid, None)
        for key in [key for key in self._pair_states if str(sid) in key.split(":")]:
            self._pair_states.pop(key, None)

    def get_same_signature_shifts(self, shift_id: int, bucket_key: str) -> list[tuple[int, dict]]:
        sid = int(shift_id)
        return [
            (other_shift_id, data)
            for other_shift_id, data in self._shifts.items()
            if other_shift_id != sid and data.get("last_bucket_key") == bucket_key
        ]

    def touch_pair(self, shift_a: int, shift_b: int, sig: str, now_ts: float | None = None) -> tuple[str, int]:
        key = self.pair_key(shift_a, shift_b)
        state = self._pair_states.get(key) or PairState()
        if state.last_sig == sig:
            state.streak += 1
        else:
            state.streak = 1
            state.last_sig = sig
        self._pair_states[key] = state
        return key, state.streak

    def clear_shift_pairs_except(self, shift_id: int, keep_pair_keys: set[str]) -> None:
        sid = str(int(shift_id))
        for key, state in self._pair_states.items():
            if sid in key.split(":") and key not in keep_pair_keys:
                state.streak = 0
                state.last_sig = None

    def can_notify_pair(self, pair_key: str, cooldown_sec: int, now_ts: float | None = None) -> bool:
        state = self._pair_states.get(pair_key)
        if not state:
            return False
        now = now_ts or time.time()
        if (now - state.last_notify_ts) < cooldown_sec:
            return False
        state.last_notify_ts = now
        return True

    def get_shift(self, shift_id: int) -> dict | None:
        return self._shifts.get(int(shift_id))


LIVE_REGISTRY = LiveShiftRegistry()
