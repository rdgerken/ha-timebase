"""Unit tests for statistics helpers (pure logic, no recorder required)."""

from datetime import datetime, timezone

from custom_components.timebase.statistics import (
    accumulate_counter,
    tag_to_statistic_id,
)


def _h(hour: int) -> datetime:
    return datetime(2026, 7, 22, hour, 0, tzinfo=timezone.utc)


def test_tag_to_statistic_id_slugifies():
    assert (
        tag_to_statistic_id("ha.sensor.Pool_Water")
        == "timebase:ha_sensor_pool_water"
    )
    assert tag_to_statistic_id("131-TT-001.PV") == "timebase:131_tt_001_pv"


def test_counter_baseline_then_monotonic():
    readings = {_h(13): 100.0, _h(14): 102.0, _h(15): 104.0}
    rows = accumulate_counter(readings, prev_state=None, prev_sum=0.0)
    assert [(r["state"], r["sum"]) for r in rows] == [
        (100.0, 0.0),  # first import establishes the baseline
        (102.0, 2.0),
        (104.0, 4.0),
    ]


def test_counter_meter_reset_keeps_sum_climbing():
    readings = {_h(16): 106.0, _h(17): 2.0, _h(18): 4.0}
    rows = accumulate_counter(readings, prev_state=104.0, prev_sum=4.0)
    assert [(r["state"], r["sum"]) for r in rows] == [
        (106.0, 6.0),
        (2.0, 8.0),  # reset: delta = full new reading, sum never dips
        (4.0, 10.0),
    ]


def test_counter_resumes_from_previous_import_without_double_count():
    # Second import starting exactly where the first ended.
    rows = accumulate_counter({_h(20): 8.0}, prev_state=6.0, prev_sum=12.0)
    assert rows == [{"start": _h(20), "state": 8.0, "sum": 14.0}]


def test_counter_rows_sorted_by_hour_regardless_of_dict_order():
    readings = {_h(15): 3.0, _h(13): 1.0, _h(14): 2.0}
    rows = accumulate_counter(readings, prev_state=None, prev_sum=0.0)
    assert [r["start"] for r in rows] == [_h(13), _h(14), _h(15)]
