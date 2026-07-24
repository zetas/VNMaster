from datetime import datetime, timezone

from freezegun import freeze_time

from vnmaster.clock import Clock, SystemClock


def test_system_clock_returns_int_epoch() -> None:
    c = SystemClock()
    t = c.now_epoch()
    assert isinstance(t, int)
    assert t > 1_700_000_000


def test_system_clock_now_datetime_aware() -> None:
    c = SystemClock()
    dt = c.now()
    assert dt.tzinfo is not None


@freeze_time("2026-05-19T14:00:00+00:00")
def test_clock_is_freezable() -> None:
    c = SystemClock()
    assert c.now_epoch() == int(datetime(2026, 5, 19, 14, 0, tzinfo=timezone.utc).timestamp())


def test_clock_is_a_protocol() -> None:
    class FakeClock:
        def now(self) -> datetime:
            return datetime(2026, 1, 1, tzinfo=timezone.utc)

        def now_epoch(self) -> int:
            return 1735689600

    f: Clock = FakeClock()
    assert f.now_epoch() == 1735689600
