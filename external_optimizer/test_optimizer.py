import datetime
import sys
import types
import unittest

sys.modules.setdefault("requests", types.SimpleNamespace())

from gen24_optimizer import build_hours, dp_optimal


class OptimizerTests(unittest.TestCase):
    def test_exact_24_hour_window_has_24_buckets(self):
        start = datetime.datetime(2026, 9, 7, 7, 0, tzinfo=datetime.timezone.utc)
        end = start + datetime.timedelta(hours=24)
        points = [((start + datetime.timedelta(hours=i)).isoformat(), 1000.0) for i in range(25)]
        pv = [(t, 0.0) for t, _ in points]
        prices = [(t, 1.0) for t, _ in points]
        hours = build_hours(points, pv, prices, prices, start, end)
        self.assertEqual(24, len(hours))

    def test_interval_crossing_hour_boundary_is_split(self):
        z = datetime.timezone.utc
        start = datetime.datetime(2026, 9, 7, 7, 30, tzinfo=z)
        end = datetime.datetime(2026, 9, 7, 9, 30, tzinfo=z)
        times = [start.isoformat(), end.isoformat()]
        load = [(t, 2000.0) for t in times]
        pv = [(t, 500.0) for t in times]
        prices = [(t, 1.5) for t in times]
        hours = build_hours(load, pv, prices, prices, start, end)
        self.assertEqual(3, len(hours))
        self.assertTrue(all(abs(v["net"] - 1.5) < 1e-9 for v in hours.values()))

    def test_dp_remains_a_lower_bound_for_idle(self):
        z = datetime.timezone.utc
        hours = {datetime.datetime(2026, 9, 7, h, tzinfo=z):
                 {"net": 1.0, "buy": 2.0, "sell": 1.0} for h in range(4)}
        optimum, _ = dp_optimal(hours, 10.0, 0.9, 5.0, 5.0, 2.0, k=201)
        self.assertLessEqual(optimum, 8.0 + 1e-6)


if __name__ == "__main__":
    unittest.main()
