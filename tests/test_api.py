"""Unit tests for the Timebase API client helpers (no historian required)."""

from datetime import datetime, timezone

from custom_components.timebase.api import TimebaseClient, iso_z, unit_from_meta


def test_iso_z_formats_utc_with_z_suffix():
    when = datetime(2026, 7, 22, 21, 12, 14, 326000, tzinfo=timezone.utc)
    assert iso_z(when) == "2026-07-22T21:12:14.326Z"


def test_unit_from_meta_single_entry_enum_is_a_unit():
    assert unit_from_meta({"u": {"1": "kWh"}}) == "kWh"


def test_unit_from_meta_multi_entry_enum_is_labels_not_unit():
    assert unit_from_meta({"u": {"0": "Off", "1": "On"}}) is None


def test_unit_from_meta_plain_string_and_missing():
    assert unit_from_meta({"u": "°F"}) == "°F"
    assert unit_from_meta({}) is None
    assert unit_from_meta({"u": {}}) is None


def test_normalize_read_live_tl_wrapper():
    """The live 1.3.x shape: {"s","e","tl":[{"t":meta,"d":[tvq]}]}."""
    data = {
        "s": "2026-07-22T21:00:00Z",
        "e": "2026-07-22T22:00:00Z",
        "tl": [
            {
                "t": {"n": "ha.sensor.temp"},
                "d": [{"t": "2026-07-22T21:10:00Z", "v": 72.5, "q": 192}],
            },
            {
                # empty tag: placeholder TVQ with no "v" — must be preserved
                # as-is (callers filter on the "v" key)
                "t": {"n": "ha.sensor.empty", "u": {"1": "°F"}},
                "d": [{"t": "0001-01-01T04:57:00Z", "q": 0}],
            },
        ],
    }
    result = TimebaseClient._normalize_read(data)
    assert result["ha.sensor.temp"] == [
        {"t": "2026-07-22T21:10:00Z", "v": 72.5, "q": 192}
    ]
    assert result["ha.sensor.empty"] == [{"t": "0001-01-01T04:57:00Z", "q": 0}]


def test_normalize_read_legacy_single_object():
    data = {
        "t": {"n": "tag.one"},
        "s": "x",
        "e": "y",
        "d": [{"t": "2026-07-22T21:10:00Z", "v": 1.0, "q": 192}],
    }
    result = TimebaseClient._normalize_read(data)
    assert list(result) == ["tag.one"]
    assert result["tag.one"][0]["v"] == 1.0


def test_normalize_read_list_form_and_garbage():
    data = [
        {"t": {"n": "a"}, "d": [{"t": "t1", "v": 2, "q": 192}]},
        "not-a-dict",
        {"no": "name"},
    ]
    result = TimebaseClient._normalize_read(data)
    assert list(result) == ["a"]
