# pylint: disable=protected-access,redefined-outer-name
"""Schedule programs: the day overview, the editor, event lists and event settings."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from daikin_itm import ItmCommandError, PowerState, RemoteLock
from daikin_itm.menu import (
    ScheduleEvent,
    day_events_from_screen,
    event_from_action,
    event_from_screen,
    program_from_editor,
    schedule_day_from_screen,
)
from daikin_itm.protocol import parse_reply
from daikin_itm.state import UIState

from .fake_itm import FakeITM
from .test_client import FakeClient

FIXTURE = Path(__file__).parent / "fixtures" / "schedule_session.json"


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM."""
    return FakeITM()


def _replay(name: str, *, stop_before: list[int], nth: int = 1) -> UIState:
    """Replay a captured session, stopping before the nth given request."""
    s = UIState()
    seen = 0
    for entry in json.loads(FIXTURE.read_text(encoding="utf-8"))[name]:
        if entry["request"][:2] == stop_before:
            seen += 1
            if seen == nth:
                break
        s.apply(parse_reply(entry["reply"]).screens)
    return s


def test_captured_day_overview() -> None:
    """The day overview lists a program's events for the shown day."""
    day = schedule_day_from_screen(_replay("overview", stop_before=[102, 3]))
    assert day.program == "Always"
    assert day.day == "01/01(Wed)"
    assert [e.time for e in day.events] == ["07:30", "16:45"]
    assert day.events[0].target == "Zone 1"
    assert day.events[0].target_kind == "zone"
    assert day.events[0].action.startswith("Start, ")
    assert day.events[0].details is False
    assert day.events[0].power is PowerState.ON
    assert day.events[0].lock_start_stop is None  # not in the text
    assert day.events[0].lock_mode is RemoteLock.FORBIDDEN
    assert day.events[0].lock_setpoint is None
    assert day.events[0].mode == "cool"
    assert day.events[0].setpoint == 22.0
    assert day.events[0].extra == {"Low": ""}
    assert day.events[1].power is PowerState.OFF


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("Stop", {"power": PowerState.OFF}),
        ("Setback High, 17.5°C", {"power": PowerState.SETBACK_HIGH, "setpoint": 17.5}),
        ("Setback Low, 18.0°C", {"power": PowerState.SETBACK_LOW, "setpoint": 18.0}),
        ("18.5°C", {"power": None, "setpoint": 18.5}),
        (
            (
                "Start, R/C Start/Stop[Prohibited], "
                "R/C Op. Mode[Permitted], "
                "R/C Setpoint[Prohibited], "
                "Automatic, High, 23.5°C, Ventilation Mode[Automatic], "
                "Ventilation Amount[Auto (normal)]"
            ),
            {
                "power": PowerState.ON,
                "lock_start_stop": RemoteLock.FORBIDDEN,
                "lock_mode": RemoteLock.ALLOWED,
                "lock_setpoint": RemoteLock.FORBIDDEN,
                "mode": "auto",
                "setpoint": 23.5,
                "extra": {
                    "High": "",
                    "Ventilation Mode": "Automatic",
                    "Ventilation Amount": "Auto (normal)",
                },
            },
        ),
        (
            (
                "Start, Automatic, Low, 24.5°C, "
                "Setpoint Restriction(Cool)[Enable(24.0°C-30.0°C)]"
            ),
            {"mode": "auto", "setpoint": 24.5, "lock_start_stop": None},
        ),
    ],
)
def test_event_from_action(action: str, expected: dict[str, object]) -> None:
    """The overview text parses into typed fields through the language tables."""
    event = event_from_action(ScheduleEvent("06:00", "x", "zone", action))
    for key, value in expected.items():
        assert getattr(event, key) == value, key


def test_captured_editor() -> None:
    """The editor's properties and pattern bars."""
    program = program_from_editor(_replay("program", stop_before=[104, 73]))
    assert program.name == "Program B"
    assert program.enabled is True
    assert program.period is None
    assert set(program.weekly_slots) == set(range(7))
    assert program.weekly_slots[1] == (405, 480, 495, 1050, 1065)  # Monday from 06:45
    assert 390 in program.weekly_slots[2]  # Tuesday 06:30
    assert program.weekly_slots[0] == ()  # Sunday: no events
    assert program.weekly_slots[6] == (540, 570)  # Saturday 09:00 and 09:30
    assert not program.special_slots
    assert not program.calendar


def test_captured_day_events() -> None:
    """The editor's event list: times without a colon continue the hour before."""
    events = day_events_from_screen(_replay("program", stop_before=[107, 4]))
    assert [(e.time, e.target) for e in events] == [
        ("06:30", "Zone 3"),
        ("08:00", "Zone 1"),
        ("08:15", "1:1-05"),
        ("17:30", "Zone 3"),
        ("17:45", "Zone 1"),
    ]
    assert events[2].target_kind == "indoor"
    assert events[0].target_kind == "zone"
    # The rows in the iTM's list, separators included: that is what gets selected.
    assert [e.row for e in events] == [0, 2, 3, 5, 6]


def test_captured_event_settings() -> None:
    """An event's settings screen gives its typed settings."""
    state = _replay("program", stop_before=[109, 6])
    base = day_events_from_screen(state)[0]
    event = event_from_screen(state, base)
    assert event.details is True
    assert event.time == "06:30"
    assert event.target == "Zone 3"
    assert event.power is PowerState.ON
    assert event.lock_start_stop is RemoteLock.FORBIDDEN
    assert event.lock_mode is RemoteLock.FORBIDDEN
    assert event.lock_setpoint is RemoteLock.FORBIDDEN
    assert event.mode == "heat"
    assert event.mode_label == "Heat"
    assert event.setpoint == 21.0
    assert event.setpoint_shift is None
    assert event.overtime is None  # not part of the event
    assert event.fan_speed is None
    assert event.action.startswith("Start, ")


def test_event_without_settings() -> None:
    """A settings screen with nothing checked adds no settings."""
    assert not day_events_from_screen(UIState())
    event = event_from_screen(UIState(), ScheduleEvent("06:00", "x", "zone", "Start"))
    assert event.details is True
    assert event.power is None
    assert event.mode is None


async def test_fetch_schedule(itm: FakeITM) -> None:
    """A whole program through its editor, left with Cancel and Yes."""
    program = await FakeClient(itm).fetch_schedule("Always", details=True)
    assert program.name == "Always"
    assert program.enabled is True
    assert program.period is None
    assert program.weekly_slots[1] == (6 * 60, 18 * 60)
    assert program.weekly_slots[0] == ()
    assert set(program.weekly) == {1, 2, 3, 4, 5}
    morning, evening = program.weekly[1]
    assert morning.time == "06:00"
    assert morning.target == "Indoor units"
    assert morning.target_kind == "zone"
    assert morning.power is PowerState.ON
    assert morning.mode == "cool"
    assert morning.setpoint == 22.0
    assert morning.lock_start_stop is RemoteLock.FORBIDDEN
    assert morning.lock_mode is RemoteLock.FORBIDDEN
    assert morning.lock_setpoint is RemoteLock.ALLOWED
    assert morning.ventilation_mode == "Automatic"
    assert evening.power is PowerState.OFF
    assert evening.mode is None
    assert program.special["Ex1"][0].time == "08:00"
    assert program.special["Ex1"][0].target_kind == "indoor"
    assert program.calendar[0].pattern == "Ex1"
    assert itm.open_screens == set()
    assert itm.editor_confirming is False
    assert itm.session is None
    # Events were selected by their row in the list, not by their index.
    selections = [
        r["array"] for r in itm.requests if r["screen"] == 107 and r["part"] == 4
    ]
    assert selections[:2] == [[0], [2]]


async def test_disabled_edit_button_is_not_clicked(itm: FakeITM) -> None:
    """When the iTM disables Edit for a row, the library refuses to click it."""
    original = itm._schedule_event

    def disabled(ev: dict[str, Any]) -> list[Any]:
        updates = original(ev)
        if ev["screen"] == 107 and ev["part"] == 4:
            itm.selected_event = None
            return [("UPDATE", 107, [], [(p, "ENABLED", 0) for p in (6, 7, 8)])]
        return updates

    itm._schedule_event = disabled  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="does not open"):
        await FakeClient(itm).fetch_schedule("Always", details=True)
    assert not any(r["screen"] == 107 and r["part"] == 8 for r in itm.requests)
    assert itm.session is None


async def test_fetch_schedule_without_details(itm: FakeITM) -> None:
    """By default the events come from the lists only: no event screen is opened."""
    program = await FakeClient(itm).fetch_schedule("Always")
    morning = program.weekly[1][0]
    assert morning.details is False
    assert morning.power is PowerState.ON
    assert morning.mode is None  # the fake's text says only "Start"
    assert not any(r["screen"] == 109 for r in itm.requests)
    assert itm.open_screens == set()


async def test_fetch_schedule_with_period(itm: FakeITM) -> None:
    """A program with a period reads it from the combos."""
    itm.schedules[0] = [4, "Always", "01/06", "31/08"]
    program = await FakeClient(itm).fetch_schedule("Always", events=False)
    assert program.enabled is False
    assert program.period == ("May/1", "Sep/30")
    assert not program.weekly  # the lists are not opened either
    assert program.weekly_slots[1] == (6 * 60, 18 * 60)
    assert not any(r["screen"] in {107, 109} for r in itm.requests)


async def test_fetch_schedule_unknown(itm: FakeITM) -> None:
    """An unknown program is refused with the known names."""
    with pytest.raises(ItmCommandError, match="available"):
        await FakeClient(itm).fetch_schedule("Nope")
    assert itm.open_screens == set()


async def test_fetch_schedule_read_only(itm: FakeITM) -> None:
    """Reading a program is fine in read-only mode (nothing is written)."""
    program = await FakeClient(itm, read_only=True).fetch_schedule(
        "Always", details=True
    )
    assert program.weekly[2][0].setpoint == 22.0


async def test_fetch_schedule_day(itm: FakeITM) -> None:
    """The day overview for today and days ahead."""
    c = FakeClient(itm)
    today = await c.fetch_schedule_day("Always")
    assert today.program == "Always"
    assert today.day == "01/01(Wed)"
    assert [e.time for e in today.events] == ["06:00", "18:00"]
    sunday = await c.fetch_schedule_day("Always", days_ahead=4)
    assert sunday.day == "05/01(Sun)"
    assert sunday.events == ()
    assert itm.open_screens == set()
