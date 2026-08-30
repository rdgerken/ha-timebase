"""Exporter behavior tests: ordering, quality mapping, attributes, buffering."""

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from homeassistant.config_entries import SOURCE_REAUTH
from homeassistant.core import Event, State
from homeassistant.helpers.entityfilter import generate_filter
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.timebase.api import TimebaseAuthError
from custom_components.timebase.const import DOMAIN
from custom_components.timebase.exporter import TimebaseExporter, _coerce_numeric

UTC = timezone.utc


def _event(entity_id, old, new):
    return Event(
        "state_changed",
        {"entity_id": entity_id, "old_state": old, "new_state": new},
    )


def _state(entity_id, value, attrs=None, ts=None):
    return State(
        entity_id,
        value,
        attrs or {},
        last_updated=ts or datetime(2026, 7, 22, 21, 0, tzinfo=UTC),
    )


@pytest.fixture
def exporter(hass):
    return TimebaseExporter(
        hass,
        AsyncMock(),
        "TestDS",
        "ha",
        generate_filter([], [], [], []),  # allow everything
        export_attributes=["brightness"],
        entry_id="test_entry",
    )


def test_coerce_numeric():
    assert _coerce_numeric(True) == 1.0
    assert _coerce_numeric(False) == 0.0
    assert _coerce_numeric(42) == 42.0
    assert _coerce_numeric("3.14") == 3.14
    assert _coerce_numeric("on") is None
    assert _coerce_numeric([1, 2]) is None
    assert _coerce_numeric(None) is None


def test_numeric_state_buffered_with_event_timestamp(hass, exporter):
    ts = datetime(2026, 7, 22, 21, 5, 30, tzinfo=UTC)
    exporter._handle_event(
        _event("sensor.temp", None, _state("sensor.temp", "72.5", {"unit_of_measurement": "°F"}, ts))
    )
    tvqs = exporter._buffer["ha.sensor.temp"]
    assert tvqs == [{"t": "2026-07-22T21:05:30.000Z", "v": 72.5, "q": 192}]
    assert exporter._tag_meta["ha.sensor.temp"]["u"] == {"1": "°F"}


def test_binary_states_map_to_numeric(hass, exporter):
    exporter._handle_event(
        _event("switch.pump", None, _state("switch.pump", "on"))
    )
    assert exporter._buffer["ha.switch.pump"][0]["v"] == 1.0


def test_unavailable_holds_last_value_with_bad_quality(hass, exporter):
    good = _state("sensor.temp", "70")
    exporter._handle_event(_event("sensor.temp", None, good))
    bad_ts = datetime(2026, 7, 22, 21, 30, tzinfo=UTC)
    exporter._handle_event(
        _event("sensor.temp", good, _state("sensor.temp", "unavailable", ts=bad_ts))
    )
    tvqs = exporter._buffer["ha.sensor.temp"]
    assert tvqs[-1] == {"t": "2026-07-22T21:30:00.000Z", "v": 70.0, "q": 24}


def test_unavailable_without_prior_value_is_skipped(hass, exporter):
    exporter._handle_event(
        _event("sensor.x", None, _state("sensor.x", "unavailable"))
    )
    assert not exporter._buffer


def test_attribute_only_change_exports_attribute_not_state(hass, exporter):
    old = _state("light.desk", "on", {"brightness": 100})
    new = _state(
        "light.desk", "on", {"brightness": 200},
        ts=datetime(2026, 7, 22, 21, 10, tzinfo=UTC),
    )
    exporter._handle_event(_event("light.desk", None, old))
    state_points = len(exporter._buffer["ha.light.desk"])
    exporter._handle_event(_event("light.desk", old, new))
    # no new state point (state unchanged), one new attribute point
    assert len(exporter._buffer["ha.light.desk"]) == state_points
    assert exporter._buffer["ha.light.desk.brightness"][-1]["v"] == 200.0


def test_buffer_preserves_per_tag_chronological_order(hass, exporter):
    prev = None
    for minute in (1, 2, 3):
        st = _state(
            "sensor.seq", str(minute),
            ts=datetime(2026, 7, 22, 21, minute, tzinfo=UTC),
        )
        exporter._handle_event(_event("sensor.seq", prev, st))
        prev = st
    times = [p["t"] for p in exporter._buffer["ha.sensor.seq"]]
    assert times == sorted(times)


async def test_flush_failure_requeues_in_front_then_succeeds(hass, exporter):
    exporter._handle_event(
        _event("sensor.a", None, _state("sensor.a", "1"))
    )
    exporter._client.async_write.side_effect = Exception("boom")
    await exporter.async_flush()
    assert exporter._buffered_count == 1  # requeued
    assert exporter.last_error == "boom"

    exporter._client.async_write.side_effect = None
    await exporter.async_flush()
    assert exporter._buffered_count == 0
    assert exporter.samples_sent == 1
    assert exporter.last_error is None


def test_overflow_drops_globally_oldest_across_tags(hass, exporter, monkeypatch):
    """Overflow loss spreads by sample age, not by tag registration order."""
    monkeypatch.setattr(
        "custom_components.timebase.exporter.MAX_BUFFERED_TVQS", 4
    )
    prev = {}
    for minute, entity in enumerate(
        ("sensor.a", "sensor.b", "sensor.a", "sensor.b", "sensor.a", "sensor.b"),
        start=1,
    ):
        st = _state(
            entity, str(minute), ts=datetime(2026, 7, 22, 21, minute, tzinfo=UTC)
        )
        exporter._handle_event(_event(entity, prev.get(entity), st))
        prev[entity] = st

    assert exporter.samples_dropped == 2
    # The two globally oldest samples (a@21:01, b@21:02) are gone; the
    # first-registered tag was NOT drained to protect later ones.
    assert [p["v"] for p in exporter._buffer["ha.sensor.a"]] == [3.0, 5.0]
    assert [p["v"] for p in exporter._buffer["ha.sensor.b"]] == [4.0, 6.0]


def test_bound_enforced_when_state_not_convertible(hass, exporter, monkeypatch):
    """Attribute samples queued before a skipped state must still hit the cap."""
    monkeypatch.setattr(
        "custom_components.timebase.exporter.MAX_BUFFERED_TVQS", 1
    )
    old = _state("light.desk", "fading", {"brightness": 100})
    new = _state(
        "light.desk", "shimmering", {"brightness": 200},
        ts=datetime(2026, 7, 22, 21, 10, tzinfo=UTC),
    )
    exporter._handle_event(_event("light.desk", None, old))
    exporter._handle_event(_event("light.desk", old, new))
    # non-numeric states are skipped (export_string_states off), but the two
    # attribute samples count — the cap of 1 must have dropped one of them
    assert exporter._buffered_count == 1


async def test_flush_auth_failure_starts_reauth(
    recorder_mock, enable_custom_integrations, hass
):
    """A rotated Pulse secret during flush must surface a reauth prompt."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"host": "127.0.0.1", "port": 4512, "dataset": "DS"},
        unique_id="127.0.0.1:4512:DS",
    )
    entry.add_to_hass(hass)
    exporter = TimebaseExporter(
        hass,
        AsyncMock(),
        "DS",
        "ha",
        generate_filter([], [], [], []),
        entry_id=entry.entry_id,
    )
    exporter._handle_event(_event("sensor.a", None, _state("sensor.a", "1")))
    exporter._client.async_write.side_effect = TimebaseAuthError(401, "revoked")
    await exporter.async_flush()
    await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert any(f["context"]["source"] == SOURCE_REAUTH for f in flows)
    assert exporter._buffered_count == 1  # sample kept for post-reauth recovery
