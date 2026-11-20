# pylint: disable=redefined-outer-name
"""Points of other kinds (ventilation units, analog inputs) and error information."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import (
    AnalogPoint,
    ItmCommandError,
    ItmIndicators,
    PointKind,
    Points,
    PowerState,
    ProtocolError,
    Unit,
)
from daikin_itm.models import (
    L_LIST,
    analog_details_from_screen,
    analog_from_list,
    analog_from_panel,
    error_information_from_screen,
    point_kind,
    unit_from_panel,
    units_from_list,
)
from daikin_itm.protocol import Part, ScreenUpdate, parse_reply
from daikin_itm.state import UIState

from .fake_itm import FakeAnalog, FakeITM
from .test_client import FakeClient

if TYPE_CHECKING:
    import contextlib

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
    assert "Area" not in details["attributes"]
    assert details["point_id"] == "312"
    assert details["description"] is None


def _details(chars: str) -> UIState:
    s = UIState()
    parts = [Part(4, "TEXT", " 7 "), Part(14, "TEXT", chars)]
    s.apply([ScreenUpdate(9, "OPEN", [-1], parts)])
    return s


def test_analog_details_without_a_blank_line() -> None:
    """The zone heading and paths are no attributes, also without a blank line."""
    details = analog_details_from_screen(
        _details("Area:\nTop&gt;Zone 1\nTarget Point:1:1-00\nUnit Label:°C\n")
    )
    assert details["zones"] == ("Top>Zone 1",)
    assert details["attributes"] == {"Target Point": "1:1-00", "Unit Label": "°C"}
    assert details["point_id"] == "7"
    assert details["unit"] == "°C"


def test_analog_details_without_zones() -> None:
    """Attributes from the first line on, when there is no zone heading."""
    details = analog_details_from_screen(_details("Upper Limit:25.0\n"))
    assert not details["zones"]
    assert details["upper_limit"] == 25.0
    assert not analog_details_from_screen(_details(""))["zones"]


def test_analog_details_keys_next_to_the_zones() -> None:
    """A key and value right after the paths, or first, is an attribute, no zone."""
    details = analog_details_from_screen(
        _details("Area:\nTop&gt;Zone 1\nSensor Type:Internal\nUpper Limit:25.0\n")
    )
    assert details["zones"] == ("Top>Zone 1",)
    assert details["attributes"] == {"Sensor Type": "Internal", "Upper Limit": "25.0"}
    details = analog_details_from_screen(_details("Sensor Type:Internal\n"))
    assert not details["zones"]
    assert details["attributes"] == {"Sensor Type": "Internal"}
    heading = analog_details_from_screen(_details("Area\uff1a\nTop&gt;Zone 1\n"))
    assert heading["zones"] == ("Top>Zone 1",)


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
    assert point.point_id == "312"
    assert point.value is None

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


def _list(cols: list[str], rows: list[list[list[Any]]]) -> UIState:
    s = UIState()
    panel = {"cols": [[c, 100] for c in cols], "sort": [0, 0], "rows": rows}
    s.apply([ScreenUpdate(2, "SHOW", [], [Part(L_LIST, "PNTLIST", panel)])])
    return s


ANALOG_COLUMNS = ["Name", "Status", "Value", "Blocked", "Error Type"]


def _analog_row(name: str, value: str, icon: int = 2601) -> list[list[Any]]:
    return [[name, -1, -1], ["", icon, 2], [value, -1, -1], ["", -1, -1], ["", -1, -1]]


def test_analog_values_from_the_list() -> None:
    """The analog list's value column: number and unit, None for none."""
    state = _list(
        ANALOG_COLUMNS,
        [
            _analog_row("Input 1", "23.50°C"),
            _analog_row("Input 2", "---"),
            _analog_row("Input 3", ""),
            _analog_row("Input 4", "-1,5 %"),
            _analog_row("Input 5", "7"),
            _analog_row("Input 6", "1,234.5°C"),
        ],
    )
    points = analog_from_list(state)
    assert points["Input 1"] == AnalogPoint(
        name="Input 1", state_icon=None, alarm=None, unit="°C", value=23.5
    )
    assert (points["Input 2"].value, points["Input 2"].unit) == (None, None)
    assert points["Input 3"].value is None
    assert (points["Input 4"].value, points["Input 4"].unit) == (-1.5, "%")
    assert (points["Input 5"].value, points["Input 5"].unit) == (7.0, None)
    assert (points["Input 6"].value, points["Input 6"].unit) == (None, None)


def test_analog_list_refusals() -> None:
    """Only a five-column list of analog inputs is read; units_from_list says so."""
    with pytest.raises(ProtocolError, match="not shown"):
        analog_from_list(UIState())
    mixed = _list(
        ANALOG_COLUMNS, [_analog_row("Input 1", "1"), _analog_row("AC 01", "", 2100)]
    )
    with pytest.raises(ProtocolError, match="5 columns"):
        analog_from_list(mixed)
    with pytest.raises(ProtocolError, match="5 columns"):
        analog_from_list(_list(ANALOG_COLUMNS, []))
    four = _list(ANALOG_COLUMNS[:4], [_analog_row("Input 1", "1")[:4]])
    with pytest.raises(ProtocolError, match="4 columns"):
        analog_from_list(four)
    analog = _list(ANALOG_COLUMNS, [_analog_row("Input 1", "1")])
    with pytest.raises(ProtocolError, match=r"fetch_analog_values\(\)") as caught:
        units_from_list(analog)
    assert 'view="icons"' not in str(caught.value)
    with pytest.raises(ProtocolError, match='view="icons"'):
        units_from_list(_list(ANALOG_COLUMNS[:4], [_analog_row("AC 01", "", 2100)]))
    with pytest.raises(ProtocolError, match="4 columns") as caught:
        units_from_list(four)
    assert "fetch_analog_values" not in str(caught.value)
    assert 'view="icons"' not in str(caught.value)


async def test_fetch_analog_values(itm: FakeITM) -> None:
    """The values from the analog zone's list view, in a session of its own."""
    itm.analog.append(FakeAnalog("Alarm 2", value=None))
    c = FakeClient(itm, zone_path=("Building", "Analog"))
    points = await c.fetch_analog_values()
    assert points["Alarm 1"].value == 21.25
    assert points["Alarm 1"].unit == "°C"
    assert points["Alarm 2"].value is None
    assert itm.session is None
    assert not itm.list_shown
    with pytest.raises(ProtocolError, match="analog inputs"):
        await FakeClient(itm).fetch_analog_values()
    assert itm.session is None


def _block(c: FakeClient, kind: str) -> contextlib.AbstractAsyncContextManager[None]:
    return c.shared_session() if kind == "shared" else c.kept_session(keep_alive=None)


@pytest.mark.parametrize("kind", ["shared", "kept"])
async def test_analog_values_keep_a_session(kind: str) -> None:
    """The analog list, the points and the list again: one login, as read alone."""
    zone = ("Building", "Analog")
    apart = FakeClient(FakeITM(), zone_path=zone)
    alone = [
        await apart.fetch_analog_values(),
        dict(await apart.fetch_points()),
        await apart.fetch_analog_values(),
    ]
    itm = FakeITM()
    c = FakeClient(itm, zone_path=zone)
    async with _block(c, kind):
        shared = [
            await c.fetch_analog_values(),
            dict(await c.fetch_points()),
            await c.fetch_analog_values(),
        ]
        itm.analog[0].value = 22.5
        changed = await c.fetch_analog_values()
    assert shared == alone
    assert changed["Alarm 1"].value == 22.5
    assert itm.logins == 1
    assert itm.session is None


async def test_units_list_after_analog_values_ends_the_session(itm: FakeITM) -> None:
    """Only the analog list keeps the session: the units' list still ends it."""
    c = FakeClient(itm, zone_path=("Building", "Analog"))
    async with c.shared_session():
        await c.fetch_analog_values()
        c.zone_path = FakeClient(itm).zone_path
        await c.fetch_units(view="list")
        await c.fetch_units()
        assert itm.logins == 2


async def test_fetch_points_without_details(itm: FakeITM) -> None:
    """Without details the analog point's Details screen is not opened."""
    c = FakeClient(itm, zone_path=("Building", "Analog"))
    point = (await c.fetch_points())["Alarm 1"]
    assert isinstance(point, AnalogPoint)
    assert point.source_point is None
    assert not any(r["screen"] == 4 and r["part"] == 7 for r in itm.requests)


def _details_opened(itm: FakeITM) -> int:
    return sum(r["screen"] == 4 and r["part"] == 7 for r in itm.requests)


def _icon_selects(itm: FakeITM) -> int:
    return sum(r["screen"] == 1 and r["part"] == 6 for r in itm.requests)


async def test_fetch_points_reads_the_thermostat_in_the_walk(itm: FakeITM) -> None:
    """The thermostat status comes from the walk's own selection of each unit."""
    itm.units[1].thermo_on = True
    c = FakeClient(itm)
    async with c.shared_session():
        units = await c.fetch_units()
        apart = await c.fetch_thermostat_status()
    selects = _icon_selects(itm)
    itm.requests.clear()
    async with c.shared_session():
        points = await c.fetch_points(thermostat=True)
    assert (
        points.thermostat
        == apart
        == {
            "1:1-00": False,
            "1:1-04": True,
            "1:1-09": False,
        }
    )
    assert dict(points) == units
    assert _icon_selects(itm) <= selects - len(units)
    assert _details_opened(itm) == len(units)
    assert itm.logins == 2
    assert itm.session is None


async def test_fetch_points_thermostat_by_name(itm: FakeITM) -> None:
    """Only the named units, each once; none when not asked."""
    itm.units[2].thermo_on = True
    c = FakeClient(itm)
    name = itm.units[2].name
    points = await c.fetch_points(thermostat=iter([name, name]))
    assert points.thermostat == {name: True}
    assert _details_opened(itm) == 1
    assert not (await c.fetch_points()).thermostat
    assert not (await c.fetch_points(thermostat=[])).thermostat
    assert _details_opened(itm) == 1


@pytest.mark.parametrize(
    ("zone", "names", "match"),
    [
        (("Building", "Indoor units"), "1:1-04", "not the string '1:1-04'"),
        (("Building",), ["Indoor units"], r"\['Indoor units'\] are zones"),
        (("Building", "Indoor units"), ["1:1-04", "nope"], r"\['nope'\] not found"),
    ],
)
async def test_fetch_points_thermostat_refused(
    itm: FakeITM, zone: tuple[str, ...], names: object, match: str
) -> None:
    """A string, a zone or a missing name: refused before any Details screen."""
    c = FakeClient(itm, zone_path=zone)
    with pytest.raises(ItmCommandError, match=match):
        await c.fetch_points(thermostat=names)  # type: ignore[arg-type]
    assert not _details_opened(itm)
    assert itm.session is None


async def test_fetch_points_thermostat_of_other_points(itm: FakeITM) -> None:
    """True skips analog inputs; a named one with details opens Details once."""
    itm.analog[0].alarm = True
    c = FakeClient(itm, zone_path=("Building", "Analog"))
    assert not (await c.fetch_points(thermostat=True)).thermostat
    assert not _details_opened(itm)
    points = await c.fetch_points(details=True, thermostat=["Alarm 1"])
    assert points.thermostat == {"Alarm 1": None}
    assert _details_opened(itm) == 1
    assert points == await c.fetch_points(details=True, thermostat=["Alarm 1"])
    point = points["Alarm 1"]
    assert isinstance(point, AnalogPoint)
    assert point.source_point == "1:1-00"
    vent = FakeClient(itm, zone_path=("Building", "Ventilation"))
    assert (await vent.fetch_points(thermostat=["Vent 1"])).thermostat == {
        "Vent 1": None
    }


async def test_fetch_points_carries_the_indicators(itm: FakeITM) -> None:
    """The main menu bar as the walk left it, without a request of its own."""
    itm.errors = [["1:1-04", "A3"]]
    c = FakeClient(itm)
    points = await c.fetch_points()
    assert points.indicators == await c.fetch_indicators()
    assert points.indicators is not None
    assert points.indicators.error is True
    assert not any(r["screen"] == 0 for r in itm.requests)


def test_points_compare_what_they_carry() -> None:
    """Points compare their extras with other Points, only the items with a dict."""
    unit = unit_from_panel(UIState())
    shown = ItmIndicators(
        energy_saving=False, error=False, emergency_stop=False, clock_text=None
    )
    points = Points({"a": unit}, thermostat={"a": True}, indicators=shown)
    assert points == {"a": unit}
    assert points == Points({"a": unit}, thermostat={"a": True}, indicators=shown)
    assert points != Points({"a": unit}, thermostat={"a": False}, indicators=shown)
    assert points != Points({"a": unit}, thermostat={"a": True})
    assert points != Points()
    assert points != "a"
    assert Points().indicators is None
    with pytest.raises(TypeError):
        hash(points)


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
