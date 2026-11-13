from datetime import datetime, timezone

from util import night_window_start, same_night


def test_same_night_across_midnight_and_restart():
    # 22:49 Sofia (20:49Z) and 07:47 Sofia next day (05:47Z) are the same trading night
    assert same_night("2026-11-11T20:49:31.577Z", "2026-11-12T05:47:36.358Z", "08:00")


def test_new_night_after_report_time():
    assert not same_night("2026-11-12T05:47:36.358Z", "2026-11-12T06:01:00.000Z", "08:00")


def test_window_start_is_report_time_sofia():
    w = night_window_start("2026-11-12T05:47:36.358Z", "08:00")
    assert w == datetime(2026, 11, 11, 6, 0, tzinfo=timezone.utc)
