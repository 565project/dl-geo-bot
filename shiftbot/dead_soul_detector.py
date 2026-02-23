from dataclasses import dataclass, field


@dataclass
class PairState:
    streak: int = 0
    alert_sent: bool = False


@dataclass
class PointTracker:
    last_coord: dict[int, str] = field(default_factory=dict)
    last_shift_id: dict[int, int] = field(default_factory=dict)
    pairs: dict[tuple[int, int], PairState] = field(default_factory=dict)


class DeadSoulDetector:
    def __init__(
        self,
        *,
        bucket_sec: int,
        window_sec: int,
        streak_threshold: int,
        alert_cooldown_sec: int,
    ) -> None:
        # keep constructor args for backward compatibility with app config
        self.streak_threshold = max(int(streak_threshold or 5), 1)
        self._point_trackers: dict[int, PointTracker] = {}
        self._shift_to_staff: dict[int, int] = {}

    @staticmethod
    def pair_key(staff_a: int, staff_b: int) -> tuple[int, int]:
        left, right = sorted((int(staff_a), int(staff_b)))
        return left, right

    def remove_shift(self, shift_id: int) -> None:
        shift_key = int(shift_id)
        staff_id = self._shift_to_staff.pop(shift_key, None)
        if staff_id is None:
            return

        for point_id in list(self._point_trackers.keys()):
            tracker = self._point_trackers[point_id]
            tracker.last_coord.pop(staff_id, None)
            tracker.last_shift_id.pop(staff_id, None)

            keys_to_drop = [pair_key for pair_key in tracker.pairs if staff_id in pair_key]
            for pair_key in keys_to_drop:
                tracker.pairs.pop(pair_key, None)

            if not tracker.last_coord and not tracker.pairs:
                self._point_trackers.pop(point_id, None)

    def register_ping(
        self,
        *,
        shift_id: int,
        staff_id: int,
        point_id: int | None,
        coord_key: str,
        now_ts: float | None = None,
    ) -> list[dict]:
        del now_ts  # intentionally unused in exact-coordinate tracker

        if point_id is None:
            return []

        shift_key = int(shift_id)
        staff_key = int(staff_id)
        point_key = int(point_id)

        previous_staff = self._shift_to_staff.get(shift_key)
        if previous_staff is not None and previous_staff != staff_key:
            self.remove_shift(shift_key)
        self._shift_to_staff[shift_key] = staff_key

        tracker = self._point_trackers.setdefault(point_key, PointTracker())

        # ensure staff has no stale data in other points
        for other_point_id, other_tracker in list(self._point_trackers.items()):
            if other_point_id == point_key:
                continue
            if staff_key in other_tracker.last_coord:
                other_tracker.last_coord.pop(staff_key, None)
                other_tracker.last_shift_id.pop(staff_key, None)
                keys_to_drop = [pair_key for pair_key in other_tracker.pairs if staff_key in pair_key]
                for pair_key in keys_to_drop:
                    other_tracker.pairs.pop(pair_key, None)
                if not other_tracker.last_coord and not other_tracker.pairs:
                    self._point_trackers.pop(other_point_id, None)

        for other_staff_id, other_coord in list(tracker.last_coord.items()):
            if other_staff_id == staff_key:
                continue
            pair_key = self.pair_key(staff_key, other_staff_id)
            pair_state = tracker.pairs.setdefault(pair_key, PairState())
            if coord_key == other_coord:
                pair_state.streak += 1
            else:
                pair_state.streak = 0

        pairs_to_alert: list[dict] = []
        for (staff_a, staff_b), pair_state in tracker.pairs.items():
            if pair_state.streak >= self.streak_threshold and not pair_state.alert_sent:
                pairs_to_alert.append(
                    {
                        "staff_a": staff_a,
                        "staff_b": staff_b,
                        "shift_a": tracker.last_shift_id.get(staff_a),
                        "shift_b": tracker.last_shift_id.get(staff_b),
                        "point_id": point_key,
                        "streak": pair_state.streak,
                        "coord": coord_key,
                    }
                )

        for alert in pairs_to_alert:
            pair_key = self.pair_key(alert["staff_a"], alert["staff_b"])
            pair_state = tracker.pairs.get(pair_key)
            if pair_state:
                pair_state.alert_sent = True

        tracker.last_coord[staff_key] = coord_key
        tracker.last_shift_id[staff_key] = shift_key
        return pairs_to_alert
