# pylint: disable=redefined-outer-name
"""Points of other kinds (ventilation units, analog inputs) and error information."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from daikin_itm import AnalogPoint, PointKind, PowerState, Unit
from daikin_itm.models import (
    analog_details_from_screen,
    analog_from_panel,
    error_information_from_screen,
    point_kind,
    unit_from_panel,
)
from daikin_itm.protocol import Part, ScreenUpdate, parse_reply
from daikin_itm.state import UIState

from .fake_itm import FakeITM
from .test_client import FakeClient

FIXTURE = Path(__file__).parent / "fixtures" / "points_session.json"


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM."""
    return FakeITM()


def _session_state(prefix: str, until: str) -> UIState:
    """Replay the captured replies of a session up to (and including) a tag."""
    s = UIState()
    for entry in json.loads(FIXTURE.read_text(encoding="utf-8")):
        tag = entry["tag"]
        if not tag.startswith(prefix):
            continue
        s.apply(parse_reply(entry["reply"]).screens)
        if tag == f"{prefix}: {until}":
            break
    return s


@pytest.mark.parametrize(
    ("icon", "kind"),
    [
        (2114, PointKind.INDOOR),
        (2102, PointKind.INDOOR),
        (2201, PointKind.VENTILATION),
        (2601, PointKind.ANALOG),
        (4242, PointKind.OTHER),
        (None, PointKind.OTHER),
    ],
)
def test_point_kind(icon: int | None, kind: PointKind) -> None:
    """The icon says what kind of point it is."""
    assert point_kind(icon) is kind


def test_captured_ventilation_unit() -> None:
    """A heat reclaim ventilation unit reads as a Unit with on/off only."""
    # The third icon selection is the unit itself (after two zone selections).
    state = UIState()
    tags = []
    for entry in json.loads(FIXTURE.read_text(encoding="utf-8")):
        if entry["tag"].startswith("ventilation"):
            state.apply(parse_reply(entry["reply"]).screens)
            tags.append(entry["tag"])
            if tags.count("ventilation: select icon") == 3:
                break
    unit = unit_from_panel(state)
    assert unit.name == "Vent 1"
    assert unit.kind is PointKind.VENTILATION
    assert unit.power_state is PowerState.ON
    assert unit.running is True
    assert unit.mode is None
    assert unit.setpoint is None
    assert not unit.setpoint_range
    assert unit.room_temperature is None
    assert unit.fan_speed is None  # a fixed icon, not a speed


def test_captured_analog_point() -> None:
    """An analog input's Details say what it watches and its limit."""
    state = _session_state("analog", "open details")
    point = analog_from_panel(state)
    assert point.name == "Input 1"
    assert point.alarm is False
    details = analog_details_from_screen(state)
    assert details["source_point"] == "1:1-05"
    assert details["source_value"] == "Suction Temp."
    assert details["upper_limit"] == 30.0
    assert details["unit"] == "°C"
    assert details["zones"] == ("Top>All>Analog",)  # the iTM's trailing space
    assert details["attributes"]["Upper Limit"] == "30.0"
    assert details["description"] is None


def test_analog_without_state() -> None:
    """Without a state icon the alarm is unknown."""
    point = analog_from_panel(UIState())
    assert point.alarm is None
    assert point.kind is PointKind.ANALOG


async def test_fetch_points(itm: FakeITM) -> None:
    """Every kind of point in a zone, analog details on request."""
    itm.analog[0].alarm = True
    c = FakeClient(itm, zone_path=("Building", "Analog"))
    points = await c.fetch_points(details=True)
    point = points["Alarm 1"]
    assert isinstance(point, AnalogPoint)
    assert point.alarm is True
    assert point.source_point == "1:1-00"
    assert point.upper_limit == 30.0
    assert point.unit == "°C"
    assert point.zones == ("Floor 1>Building>Analog",)

    c = FakeClient(itm, zone_path=("Building", "Ventilation"))
    unit = (await c.fetch_points())["Vent 1"]
    assert isinstance(unit, Unit)
    assert unit.kind is PointKind.VENTILATION
    assert unit.running is True
    assert unit.mode is None

    c = FakeClient(itm, zone_path=("Building", "Indoor units"))
    units = await c.fetch_points()
    assert all(
        isinstance(u, Unit) and u.kind is PointKind.INDOOR for u in units.values()
    )
    assert itm.session is None


async def test_fetch_points_without_details(itm: FakeITM) -> None:
    """Without details the analog point's Details screen is not opened."""
    c = FakeClient(itm, zone_path=("Building", "Analog"))
    point = (await c.fetch_points())["Alarm 1"]
    assert isinstance(point, AnalogPoint)
    assert point.source_point is None
    assert not any(r["screen"] == 4 and r["part"] == 7 for r in itm.requests)


async def test_fetch_units_marks_the_kind(itm: FakeITM) -> None:
    """fetch_units on a ventilation zone gives Units of that kind."""
    c = FakeClient(itm, zone_path=("Building", "Ventilation"))
    assert (await c.fetch_units())["Vent 1"].kind is PointKind.VENTILATION


async def test_no_error_information(itm: FakeITM) -> None:
    """Without an error indicator there is nothing to read."""
    assert await FakeClient(itm).fetch_error_information() is None
    assert not any(r["screen"] == 0 and r["part"] == 19 for r in itm.requests)


async def test_error_information(itm: FakeITM) -> None:
    """The error information is opened from the indicator, read and closed."""
    itm.errors = [["1:1-04", "U4"]]
    info = await FakeClient(itm).fetch_error_information()
    assert info is not None
    assert info.columns == ("Name", "Code")
    assert info.errors == (("1:1-04", "U4"),)
    assert info.inquiry == "Contact"
    assert info.address == "0800-1234"
    assert itm.error_shown is False
    assert itm.session is None


def test_error_information_from_empty_screen() -> None:
    """An error screen without a list gives empty information."""
    s = UIState()
    s.apply([ScreenUpdate(10, "OPEN", [-1], [Part(4, "LABEL", " ")])])
    info = error_information_from_screen(s)
    assert not info.columns
    assert not info.errors
    assert info.inquiry is None
