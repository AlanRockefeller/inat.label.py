import unittest

from ratelimit import RateLimiter


class TestRateLimiterCleanup(unittest.TestCase):
    def setUp(self):
        self.now = 0.0
        self.limiter = RateLimiter(max_tracked=2, time_source=lambda: self.now)

    def test_sweep_uses_each_keys_own_window(self):
        self.assertEqual(self.limiter.check(("daily", "a"), 1, 86400), (True, 0))
        self.assertEqual(self.limiter.check(("minute", "b"), 1, 60), (True, 0))

        self.now = 61
        self.assertEqual(self.limiter.check(("new", "c"), 1, 60), (True, 0))

        self.assertIn(("daily", "a"), self.limiter._hits)
        self.assertNotIn(("minute", "b"), self.limiter._hits)
        self.assertEqual(
            self.limiter.check(("daily", "a"), 1, 86400), (False, 86340)
        )

    def test_capacity_rejects_new_keys_without_evicting_active_quotas(self):
        self.assertEqual(self.limiter.check(("daily", "a"), 1, 86400), (True, 0))
        self.assertEqual(self.limiter.check(("daily", "b"), 1, 86400), (True, 0))

        allowed, retry_after = self.limiter.check(("daily", "attacker"), 1, 86400)

        self.assertFalse(allowed)
        self.assertGreater(retry_after, 0)
        self.assertEqual(set(self.limiter._hits), {("daily", "a"), ("daily", "b")})
        self.assertEqual(
            self.limiter.check(("daily", "a"), 1, 86400), (False, 86401)
        )


if __name__ == "__main__":
    unittest.main()
