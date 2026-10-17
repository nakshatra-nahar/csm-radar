from datetime import UTC, datetime, timedelta, timezone

import pytest

from radar import clock


def test_freeze_and_unfreeze():
    frozen = datetime(2026, 10, 17, 9, 30, tzinfo=timezone(timedelta(hours=-4)))
    clock.freeze(frozen)
    try:
        assert clock.now() == frozen
        assert clock.now().tzinfo is UTC
    finally:
        clock.unfreeze()
    assert abs(clock.now() - datetime.now(UTC)) < timedelta(seconds=5)
    assert clock.now().tzinfo is UTC


def test_freeze_rejects_naive_datetime():
    with pytest.raises(ValueError):
        clock.freeze(datetime(2026, 10, 17))
