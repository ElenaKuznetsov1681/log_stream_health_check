import logging
import unittest

from log_stream_health_check import HealthStatus, StreamHealthMonitor


class _FakeClock:
    """Deterministic monotonic clock for tests.

    Using a fake clock is mandatory here: any test that compared against
    ``time.monotonic`` would be flaky on a slow CI runner. The clock also
    lets us drive a stream *into* staleness instantly instead of sleeping.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._t: float = start

    def __call__(self) -> float:
        return self._t

    def advance(self, seconds: float) -> None:
        self._t += seconds


def _emit(monitor: StreamHealthMonitor) -> None:
    """Push a minimal LogRecord through the monitor.

    Building a real ``LogRecord`` (rather than calling ``emit`` with a dummy)
    means the test exercises the exact code path a live logger would, including
    the attribute access ``logging`` performs around ``emit``.
    """
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="hello",
        args=None,
        exc_info=None,
    )
    monitor.handle(record)


class TestStreamHealthMonitorConstruction(unittest.TestCase):
    def test_default_interval_and_grace(self) -> None:
        clock = _FakeClock()
        monitor = StreamHealthMonitor(clock=clock)
        self.assertEqual(monitor.check().last_emit_at, None)

    def test_non_positive_interval_rejected(self) -> None:
        with self.assertRaises(ValueError):
            StreamHealthMonitor(interval_seconds=0, clock=_FakeClock())
        with self.assertRaises(ValueError):
            StreamHealthMonitor(interval_seconds=-1, clock=_FakeClock())

    def test_negative_grace_rejected(self) -> None:
        with self.assertRaises(ValueError):
            StreamHealthMonitor(grace_seconds=-0.01, clock=_FakeClock())

    def test_clock_must_not_be_none(self) -> None:
        with self.assertRaises(ValueError):
            StreamHealthMonitor(clock=None)  # type: ignore[arg-type]


class TestStreamHealthMonitorCheck(unittest.TestCase):
    def test_never_emitted_is_unhealthy_with_distinct_reason(self) -> None:
        monitor = StreamHealthMonitor(interval_seconds=5.0, clock=_FakeClock(10.0))
        status = monitor.check()
        self.assertFalse(status.healthy)
        self.assertIsNone(status.last_emit_at)
        self.assertEqual(status.stale_for_seconds, 0.0)
        self.assertIn("no log record has been emitted yet", status.reason)

    def test_healthy_within_interval(self) -> None:
        clock = _FakeClock(0.0)
        monitor = StreamHealthMonitor(interval_seconds=5.0, clock=clock)
        _emit(monitor)
        clock.advance(4.0)
        status = monitor.check()
        self.assertTrue(status.healthy)
        self.assertEqual(status.last_emit_at, 0.0)
        self.assertEqual(status.stale_for_seconds, 0.0)

    def test_healthy_at_exactly_deadline_boundary(self) -> None:
        clock = _FakeClock(0.0)
        monitor = StreamHealthMonitor(interval_seconds=5.0, clock=clock)
        _emit(monitor)
        clock.advance(5.0)
        status = monitor.check()
        self.assertTrue(status.healthy)
        self.assertEqual(status.last_emit_at, 0.0)
        # The deadline is last_emit + interval == 5.0; now == 5.0, so healthy.
        self.assertEqual(status.stale_for_seconds, 0.0)

    def test_healthy_within_grace_window(self) -> None:
        clock = _FakeClock(0.0)
        monitor = StreamHealthMonitor(
            interval_seconds=5.0, grace_seconds=2.0, clock=clock
        )
        _emit(monitor)
        clock.advance(6.0)
        status = monitor.check()
        self.assertTrue(status.healthy)
        self.assertEqual(status.stale_for_seconds, 0.0)

    def test_healthy_at_grace_boundary(self) -> None:
        clock = _FakeClock(0.0)
        monitor = StreamHealthMonitor(
            interval_seconds=5.0, grace_seconds=2.0, clock=clock
        )
        _emit(monitor)
        clock.advance(7.0)
        status = monitor.check()
        self.assertTrue(status.healthy)
        self.assertEqual(status.stale_for_seconds, 0.0)

    def test_stale_just_past_grace(self) -> None:
        clock = _FakeClock(0.0)
        monitor = StreamHealthMonitor(
            interval_seconds=5.0, grace_seconds=2.0, clock=clock
        )
        _emit(monitor)
        clock.advance(7.5)
        status = monitor.check()
        self.assertFalse(status.healthy)
        self.assertEqual(status.last_emit_at, 0.0)
        # overshoot = now - (last_emit + interval + grace) = 7.5 - 7.0 = 0.5
        self.assertEqual(status.stale_for_seconds, 0.5)

    def test_stale_for_seconds_grows_with_time(self) -> None:
        clock = _FakeClock(0.0)
        monitor = StreamHealthMonitor(interval_seconds=1.0, clock=clock)
        _emit(monitor)
        clock.advance(3.0)
        status = monitor.check()
        self.assertFalse(status.healthy)
        # deadline = 0 + 1 = 1; now = 3; overshoot = 2
        self.assertEqual(status.stale_for_seconds, 2.0)

    def test_emit_after_staleness_restores_health(self) -> None:
        clock = _FakeClock(0.0)
        monitor = StreamHealthMonitor(interval_seconds=1.0, clock=clock)
        _emit(monitor)
        clock.advance(5.0)
        self.assertFalse(monitor.check().healthy)
        _emit(monitor)
        # last_emit_at is now 5.0; interval 1.0; deadline 6.0
        clock.advance(0.5)
        status = monitor.check()
        self.assertTrue(status.healthy)
        self.assertEqual(status.last_emit_at, 5.0)

    def test_reset_clears_emission_state(self) -> None:
        clock = _FakeClock(0.0)
        monitor = StreamHealthMonitor(interval_seconds=5.0, clock=clock)
        _emit(monitor)
        self.assertTrue(monitor.check().healthy)
        monitor.reset()
        status = monitor.check()
        self.assertFalse(status.healthy)
        self.assertIsNone(status.last_emit_at)
        self.assertIn("no log record has been emitted yet", status.reason)

    def test_status_is_dataclass_with_expected_fields(self) -> None:
        monitor = StreamHealthMonitor(clock=_FakeClock(0.0))
        status = monitor.check()
        self.assertIsInstance(status, HealthStatus)
        # Ensure the four documented attributes exist and have sane types.
        self.assertIsInstance(status.healthy, bool)
        self.assertTrue(status.last_emit_at is None or isinstance(status.last_emit_at, float))
        self.assertIsInstance(status.stale_for_seconds, float)
        self.assertIsInstance(status.reason, str)


if __name__ == "__main__":
    unittest.main()
