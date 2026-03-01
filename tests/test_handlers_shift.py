import unittest

from shiftbot.handlers_shift import extract_dl_number, sort_points_by_dl_number


class HandlersShiftTests(unittest.TestCase):
    def test_extract_dl_number(self):
        self.assertEqual(extract_dl_number({"short_name": "ДЛ 5"}), 5)
        self.assertEqual(extract_dl_number({"short_name": "дл10"}), 10)
        self.assertEqual(extract_dl_number({"short_name": "Точка 1"}), None)

    def test_sort_points_by_dl_number(self):
        points = [
            {"short_name": "ДЛ 10", "address": "A"},
            {"short_name": "ДЛ 2", "address": "B"},
            {"short_name": "ДЛ 1", "address": "C"},
            {"short_name": "Склад", "address": "D"},
        ]

        sorted_points = sort_points_by_dl_number(points)
        self.assertEqual([p["short_name"] for p in sorted_points], ["ДЛ 1", "ДЛ 2", "ДЛ 10", "Склад"])


if __name__ == "__main__":
    unittest.main()
