"""Live sensor entity behavior."""

from homeassistant.components.sensor import SensorStateClass

from custom_components.timebase.sensor import TimebaseTagSensor


class _FakeCoordinator:
    """Duck-typed stand-in — CoordinatorEntity only stores it at init."""

    dataset = "DS"

    def __init__(self, data):
        self.data = data
        self.units = {}


def test_state_class_follows_value_type():
    """String-valued tags (allowed by the API contract) must not claim
    MEASUREMENT, or HA logs a non-numeric-state warning on every update."""
    sensor = TimebaseTagSensor(
        _FakeCoordinator({"pump.status": {"v": "RUNNING", "q": 192}}),
        "entry1",
        "pump.status",
    )
    assert sensor.state_class is None

    sensor = TimebaseTagSensor(
        _FakeCoordinator({"plant.temp": {"v": 21.5, "q": 192}}),
        "entry1",
        "plant.temp",
    )
    assert sensor.state_class is SensorStateClass.MEASUREMENT
