"""Statistics tests: pure helpers, plus the real recorder insert path."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import get_last_statistics
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_wait_recording_done,
)

from custom_components.timebase.api import iso_z
from custom_components.timebase.statistics import (
    TimebaseStatisticsImporter,
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


def test_metadata_keys_are_statisticsmeta_columns():
    """Recorder does StatisticsMeta(**meta) with NO key filtering.

    Any metadata key that is not a column on the installed schema raises
    TypeError in the recorder thread — so every key we emit must exist on
    the running HA's model. On pre-2025.11 schemas this asserts unit_class
    is NOT sent; on newer ones, that everything we send is constructible.
    """
    from homeassistant.components.recorder.db_schema import StatisticsMeta

    importer = TimebaseStatisticsImporter(None, None, "DS", ["x"], [])
    for is_counter in (False, True):
        meta = importer._metadata("x", is_counter=is_counter)
        for key in meta:
            assert hasattr(StatisticsMeta, key), (
                f"metadata key {key!r} is not a StatisticsMeta column on "
                "this HA version — recorder would raise TypeError"
            )


async def test_import_lands_in_recorder(recorder_mock, hass):
    """End-to-end through async_add_external_statistics into the recorder.

    Exercises the real StatisticsMeta.from_meta path, so metadata the
    installed schema rejects fails HERE instead of on users' installs.
    """
    hour = (dt_util.utcnow() - timedelta(hours=2)).replace(
        minute=0, second=0, microsecond=0
    )
    client = MagicMock()
    client.async_get_tags = AsyncMock(
        return_value=[{"n": "plant.temp", "u": {"1": "°C"}}]
    )
    client.async_read = AsyncMock(
        return_value={
            "plant.temp": [
                {"t": iso_z(hour + timedelta(minutes=5)), "v": 20.0, "q": 192},
                {"t": iso_z(hour + timedelta(minutes=35)), "v": 22.0, "q": 192},
            ]
        }
    )
    importer = TimebaseStatisticsImporter(hass, client, "DS", ["plant.temp"], [])
    await importer.async_import()
    await async_wait_recording_done(hass)

    assert importer.last_error is None
    stats = await get_instance(hass).async_add_executor_job(
        get_last_statistics, hass, 1, "timebase:plant_temp", False, {"mean"}
    )
    rows = stats.get("timebase:plant_temp")
    assert rows, "statistics row never landed — metadata rejected by recorder"
    assert rows[0]["mean"] == 21.0
