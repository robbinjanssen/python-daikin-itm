# pylint: disable=protected-access,redefined-outer-name
"""The list view: every unit of a zone in one request (fetch_units(view="list"))."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from daikin_itm import PowerState, ProtocolError, RemoteLock
from daikin_itm.models import L_LIST, unit_from_list_row, units_from_list
from daikin_itm.protocol import Part, ScreenUpdate, parse_reply
from daikin_itm.state import UIState

from .fake_itm import FakeITM, FakeUnit
from .test_client import FakeClient

FIXTURE = Path(__file__).parent / "fixtures" / "list_view_reply.txt"


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM."""
    return FakeITM()


@pytest.fixture
def state() -> UIState:
    """Return the UI state after the list view reply captured from a real iTM."""
    s = UIState()
    s.apply(parse_reply(FIXTURE.read_text(encoding="utf-8")).screens)
    return s


def test_units_from_captured_list(state: UIState) -> None:
    """The captured list view decodes into every unit of the zone."""
    units = units_from_list(state)
    assert len(units) == 12

    u = units["1:1-03"]
    assert u.power_state is PowerState.SETBACK
    assert u.running is False
    assert u.setback is True
    assert u.mode == "cool"
    assert u.mode_label == "Cool"
    assert u.setpoint == 25
    assert u.setpoint_range == ()
    assert u.room_temperature == 23.8
    assert u.temperature_unit == "°C"
    assert u.fan_speed == "medium"
    assert u.cool_limit == (23.0, 30.0)
    assert u.heat_limit is None
    assert u.remote_locks is not None
    assert u.remote_locks.start_stop is RemoteLock.FORBIDDEN
    assert u.remote_locks.mode is RemoteLock.ALLOWED
    assert u.changeover_option is True
    assert u.r32_alarm is None  # shown as "---"
    assert u.error_code is None
    assert u.power is None

    assert units["1:1-05"].power_state is PowerState.OFF
    assert units["1:1-05"].fan_speed == "medium"
    assert units["1:1-06"].heat_limit == (16.0, 21.0)
    assert units["1:1-04"].setpoint is None  # fan only shows "---"
    assert units["1:1-09"].power_state is PowerState.ON
    assert units["1:1-09"].running is True

    named = units["AC 11"]
    assert named.remote_locks is not None
    assert named.remote_locks.setpoint is RemoteLock.FORBIDDEN
    assert named.room_temperature == 21.2


def test_list_view_must_be_shown() -> None:
    """Without the list view there is nothing to read."""
    with pytest.raises(ProtocolError, match="not shown"):
        units_from_list(UIState())


def test_unknown_list_layout() -> None:
    """Another number of columns than this client knows is refused."""
    s = UIState()
    panel = {"cols": [["Name", 100]], "sort": [0, 0], "rows": [], "selected": 0}
    s.apply([ScreenUpdate(2, "SHOW", [], [Part(L_LIST, "PNTLIST", panel)])])
    with pytest.raises(ProtocolError, match="1 columns"):
        units_from_list(s)


def test_list_of_zones_only() -> None:
    """A zone holding only zones lists them in a short layout: no units, no error."""
    s = UIState()
    cols = [["Name", 100], ["Status", 100], ["Type", 100], ["Error Code", 100]]
    rows = [
        [[name, -1, -1], ["", 1000, 0], ["Zone", -1, -1], ["", -1, -1]]
        for name in ("Indoor units", "Ventilation")
    ]
    panel = {"cols": cols, "sort": [0, 0], "rows": rows, "selected": 0}
    s.apply([ScreenUpdate(2, "SHOW", [], [Part(L_LIST, "PNTLIST", panel)])])
    assert not units_from_list(s)
    # A short layout with anything but zones is still refused.
    rows[1][1] = ["", 2100, 0]
    s.apply([ScreenUpdate(2, "SHOW", [], [Part(L_LIST, "PNTLIST", panel)])])
    with pytest.raises(ProtocolError, match="4 columns"):
        units_from_list(s)


def _row(**cells: list[Any]) -> list[list[Any]]:
    row: list[list[Any]] = [["", -1, -1] for _ in range(25)]
    row[0] = ["unit", -1, -1]
    for col, cell in cells.items():
        row[int(col[1:])] = cell
    return row


def test_row_edge_cases() -> None:
    """Placeholders, unknown labels and negative limits."""
    u = unit_from_list_row(
        _row(
            c1=["", 2114, 0],
            c3=["Mystery", -1, -1],
            c4=["70.2°F", -1, -1],
            c5=["812", -1, -1],
            c6=["---", -1, -1],
            c8=["-30.0°C-19.0°C", -1, -1],
            c9=["", 4050, -1],
            c10=["Alarm", -1, -1],
            c11=["", 3008, -1],
            c19=["Stop Only", -1, -1],
            c20=["Gesperrt", -1, -1],
            c22=["Yes", -1, -1],
            c23=["U4", -1, -1],
            c24=["Actief", -1, -1],
        )
    )
    assert u.power_state is PowerState.ON
    assert u.mode is None
    assert u.mode_label == "Mystery"
    assert u.room_temperature == 70.2
    assert u.temperature_unit == "°F"
    assert u.co2 == 812
    assert u.setpoint is None
    assert u.heat_limit == (-30.0, 19.0)
    assert u.filter_sign is True
    assert u.dust_alarm is True
    assert u.fan_speed == "low"
    assert u.remote_locks is not None
    assert u.remote_locks.start_stop is RemoteLock.STOP_ONLY
    assert u.remote_locks.mode is RemoteLock.UNKNOWN
    assert u.blocked is True
    assert u.error_code == "U4"
    assert u.r32_alarm is True


def test_row_without_state() -> None:
    """A row without a state icon has no power state."""
    u = unit_from_list_row(_row(c1=["", -1, -1], c2=["", -1, -1]))
    assert u.power_state is None
    assert u.running is None
    assert u.changeover_option is None
    assert u.filter_sign is False


def test_zones_are_skipped() -> None:
    """Zones in the list (icon 1000) are not units."""
    s = UIState()
    cols = [[f"c{i}", 100] for i in range(25)]
    rows = [_row(c1=["", 1000, 0]), _row(c1=["", 2114, 2])]
    panel = {"cols": cols, "sort": [0, 0], "rows": rows, "selected": 0}
    s.apply([ScreenUpdate(2, "SHOW", [], [Part(L_LIST, "PNTLIST", panel)])])
    assert list(units_from_list(s)) == ["unit"]


async def test_fetch_units_from_list(itm: FakeITM) -> None:
    """Reading through the list view gives every unit, then shows the icons again."""
    itm.units[0].cool_limit = (True, 20.0, 24.0)
    itm.units[0].lock_mode = 2
    units = await FakeClient(itm).fetch_units(view="list")
    assert list(units) == ["1:1-00", "1:1-04", "1:1-09"]
    assert units["1:1-00"].setpoint == 21
    assert units["1:1-00"].cool_limit == (20.0, 24.0)
    assert units["1:1-00"].remote_locks is not None
    assert units["1:1-00"].remote_locks.mode is RemoteLock.FORBIDDEN
    assert units["1:1-04"].mode == "cool"
    assert units["1:1-09"].running is False
    assert units["1:1-09"].setpoint is None  # stopped: the list shows "---"
    assert itm.list_shown is False
    assert itm.session is None
    # Only one request reads the units, instead of one per unit.
    selects = [r for r in itm.requests if r["screen"] == 1 and r["part"] == 6]
    assert len(selects) == 2  # the zone path, not the units


async def test_fetch_units_unknown_view(itm: FakeITM) -> None:
    """An unknown view is refused before anything is sent."""
    with pytest.raises(ValueError, match="unknown view"):
        await FakeClient(itm).fetch_units(view="table")
    assert itm.requests == []


async def test_list_then_panel_in_one_session(itm: FakeITM) -> None:
    """After the list view, the operation panel still reads normally."""
    itm.units.append(FakeUnit("1:1-10", setpoint=22))
    c = FakeClient(itm)
    async with c._session() as s:
        await s.open_zone(c.zone_path)
        listed = await s.read_units_from_list()
        panel = await s.read_units()
    assert set(listed) == set(panel) == {"1:1-00", "1:1-04", "1:1-09", "1:1-10"}
    assert panel["1:1-10"].setpoint == 22
    # A second list read in the same session reads the list again, not a stale one.
    async with c._session() as s:
        await s.open_zone(c.zone_path)
        await s.read_units_from_list()
        assert s.state.get(2, L_LIST, "PNTLIST") is None
