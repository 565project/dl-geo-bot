import unittest

from shiftbot.dead_soul_detector import DeadSoulDetector


class DeadSoulDetectorTests(unittest.TestCase):
    def build_detector(self, threshold: int = 5) -> DeadSoulDetector:
        return DeadSoulDetector(bucket_sec=10, window_sec=25, streak_threshold=threshold, alert_cooldown_sec=900)

    def test_alert_after_five_identical_pair_matches(self):
        detector = self.build_detector(threshold=5)

        # first ping only seeds the tracker
        self.assertEqual(detector.register_ping(shift_id=101, staff_id=1, point_id=29, coord_key="56.1,47.2"), [])

        alerts = []
        # next 5 pings for the pair with identical coordinates must trigger exactly one alert
        sequence = [(102, 3), (101, 1), (102, 3), (101, 1), (102, 3)]
        for idx, (shift_id, staff_id) in enumerate(sequence, 1):
            alerts = detector.register_ping(shift_id=shift_id, staff_id=staff_id, point_id=29, coord_key="56.1,47.2")
            if idx < 5:
                self.assertEqual(alerts, [])

        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0]["staff_a"], alerts[0]["staff_b"]), (1, 3))
        self.assertEqual(alerts[0]["shift_a"], 101)
        self.assertEqual(alerts[0]["shift_b"], 102)

    def test_exact_coordinate_match_only(self):
        detector = self.build_detector(threshold=5)

        detector.register_ping(shift_id=101, staff_id=1, point_id=29, coord_key="56.10000,47.20000")
        alerts = []
        for shift_id, staff_id in [(102, 3), (101, 1), (102, 3), (101, 1), (102, 3), (101, 1)]:
            coord = "56.1000,47.2" if staff_id == 3 else "56.10000,47.20000"
            alerts = detector.register_ping(shift_id=shift_id, staff_id=staff_id, point_id=29, coord_key=coord)

        self.assertEqual(alerts, [])

    def test_single_alert_until_shift_removed(self):
        detector = self.build_detector(threshold=3)

        alerts_seen = []
        for shift_id, staff_id in [(101, 1), (102, 3), (101, 1), (102, 3)]:
            alerts_seen.extend(detector.register_ping(shift_id=shift_id, staff_id=staff_id, point_id=29, coord_key="56.1,47.2"))
        self.assertEqual(len(alerts_seen), 1)

        for shift_id, staff_id in [(102, 3), (101, 1), (102, 3), (101, 1)]:
            alerts = detector.register_ping(shift_id=shift_id, staff_id=staff_id, point_id=29, coord_key="56.1,47.2")
        self.assertEqual(alerts, [])

        detector.remove_shift(101)
        alerts_seen = []
        for shift_id, staff_id in [(101, 1), (102, 3), (101, 1), (102, 3)]:
            alerts_seen.extend(detector.register_ping(shift_id=shift_id, staff_id=staff_id, point_id=29, coord_key="56.1,47.2"))
        self.assertEqual(len(alerts_seen), 1)


if __name__ == "__main__":
    unittest.main()
