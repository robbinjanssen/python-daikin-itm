# pylint: disable=protected-access,redefined-outer-name
"""Schedule contents writes: events, period, special day names, calendar rules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import ItmCommandError, PowerState, ReadOnlyError, RemoteLock
from daikin_itm.menu import CalendarRule, MenuScreen
from daikin_itm.schedule_edit import (
    CalendarRuleSpec,
    EventSpec,
    add_calendar_rule,
    add_event,
    clear_day,
    copy_calendar,
    copy_day,
    copy_event,
    copy_program,
    delete_calendar_rule,
    delete_event,
    edit_event,
    rename_special_day,
    set_period,
    special_day_names,
)

from .fake_schedule import FakeScheduleITM
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

PROGRAM = "Always"  # weekly events Monday-Friday, Ex1 has one event


@pytest.fixture
def itm() -> FakeScheduleITM:
    """Return a simulated iTM whose schedule editor saves."""
    return FakeScheduleITM()


async def run(
    itm: FakeScheduleITM,
    flow: Callable[..., Awaitable[bool]],
    *args: Any,
    client: dict[str, Any] | None = None,
    **kwargs: Any,
) -> bool:
    """Run one edit flow in a fresh session, like the client's wrappers will."""
    c = FakeClient(itm, **(client or {}))
    async with c._session() as s:
        try:
            return await flow(s, *args, **kwargs)
        finally:
            await s.close_menu()


def weekly(itm: FakeScheduleITM, day: int) -> list[dict[str, Any]]:
    """Return the saved events of a weekday of the program."""
    return itm.schedule_events[PROGRAM].get(day, [])


# -- events ---------------------------------------------------------------------------


async def test_add_event_with_every_setting(itm: FakeScheduleITM) -> None:
    """Time, point target and all settings; read back through the event screen."""
    spec = EventSpec(
        time="07:30",
        target="1:1-09",
        power=PowerState.ON,
        mode="cool",
        setpoint=21.5,
        lock_start_stop=RemoteLock.STOP_ONLY,
        lock_mode=RemoteLock.FORBIDDEN,
        lock_setpoint=RemoteLock.ALLOWED,
        overtime=True,
        fan_speed="high",
    )
    assert await run(itm, add_event, PROGRAM, 3, spec) is True
    times = [e["time"] for e in weekly(itm, 3)]
    assert times == ["06:00", "07:30", "18:00"]
    assert itm.open_screens == set()
    program = await FakeClient(itm).fetch_schedule(PROGRAM, details=True)
    event = next(e for e in program.weekly[3] if e.time == "07:30")
    assert event.target == "1:1-09"
    assert event.power is PowerState.ON
    assert event.mode == "cool"
    assert event.setpoint == 21.5
    assert event.lock_start_stop is RemoteLock.STOP_ONLY
    assert event.lock_mode is RemoteLock.FORBIDDEN
    assert event.lock_setpoint is RemoteLock.ALLOWED
    assert event.overtime is True
    assert event.fan_speed == "high"


async def test_add_event_to_special_day_for_zone(itm: FakeScheduleITM) -> None:
    """A zone target on a special day; an empty day gets its first event."""
    spec = EventSpec(
        time="06:15", target="Indoor units", zone=True, power=PowerState.OFF
    )
    assert await run(itm, add_event, PROGRAM, "Ex2", spec) is True
    events = itm.special_events[PROGRAM]["Ex2"]
    assert events == [
        {"time": "06:15", "target": "Indoor units", "kind": "zone", "power": 1}
    ]
    spec = EventSpec(
        time="20:00", target="Indoor units", zone=True, power=PowerState.SETBACK_LOW
    )
    assert await run(itm, add_event, PROGRAM, 0, spec) is True
    assert weekly(itm, 0)[0]["power"] == 3  # setback low is the fourth radio


async def test_add_event_refused_before_saving(itm: FakeScheduleITM) -> None:
    """Refusals leave the program as it was and the editor closed."""
    before = [dict(e) for e in weekly(itm, 1)]
    taken = EventSpec(time="06:00", target="Indoor units", zone=True)
    with pytest.raises(ItmCommandError, match="exists"):
        await run(itm, add_event, PROGRAM, 1, taken)
    # A ventilation unit offers power and the start/stop lock only.
    vent = EventSpec(time="09:00", target="Vent 1", power=PowerState.ON, mode="cool")
    with pytest.raises(ItmCommandError, match="mode: the iTM does not offer"):
        await run(itm, add_event, PROGRAM, 1, vent)
    unknown = EventSpec(time="09:00", target="nope", power=PowerState.ON)
    with pytest.raises(ItmCommandError, match="no point named 'nope'"):
        await run(itm, add_event, PROGRAM, 1, unknown)
    zone = EventSpec(time="09:00", target="Nowhere", zone=True)
    with pytest.raises(ItmCommandError, match="no zone named 'Nowhere'"):
        await run(itm, add_event, PROGRAM, 1, zone)
    no_mode = EventSpec(time="09:00", target="1:1-09", mode="dry")
    with pytest.raises(ItmCommandError, match="mode 'dry' not offered"):
        await run(itm, add_event, PROGRAM, 1, no_mode)
    assert weekly(itm, 1) == before
    assert itm.open_screens == set()
    with pytest.raises(ValueError, match="a day is"):
        await run(itm, add_event, PROGRAM, 7, taken)


async def test_add_event_dry_run_and_read_only(itm: FakeScheduleITM) -> None:
    """A dry run plans the editor's OK and saves nothing; read-only sends nothing."""
    spec = EventSpec(time="09:00", target="1:1-09", power=PowerState.ON)
    c = FakeClient(itm, dry_run=True)
    async with c._session() as s:
        assert await add_event(s, PROGRAM, 1, spec) is False
        await s.close_menu()
    assert c.planned == [
        "add event 09:00 1:1-09 to 1: screen 104 part 20 event 1",
    ]
    assert [e["time"] for e in weekly(itm, 1)] == ["06:00", "18:00"]
    with pytest.raises(ReadOnlyError):
        await run(itm, add_event, PROGRAM, 1, spec, client={"read_only": True})


async def test_edit_event_replaces_its_settings(itm: FakeScheduleITM) -> None:
    """New time and settings; settings the spec doesn't cover stay."""
    spec = EventSpec(
        time="06:30",
        target="Indoor units",
        zone=True,
        power=PowerState.ON,
        setpoint=20.0,
    )
    done = await run(
        itm, edit_event, PROGRAM, 1, time="06:00", target="Indoor units", spec=spec
    )
    assert done is True
    first = weekly(itm, 1)[0]
    assert first["time"] == "06:30"
    assert first["setpoint"] == 20.0
    assert "mode_idx" not in first  # unticked: not part of the spec
    assert "locks" not in first
    assert first["vent"] == 0  # not covered by EventSpec: kept
    assert weekly(itm, 2)[0]["time"] == "06:00"
    retarget = EventSpec(time="18:00", target="1:1-00", power=PowerState.OFF)
    await run(
        itm, edit_event, PROGRAM, 1, time="18:00", target="Indoor units", spec=retarget
    )
    assert weekly(itm, 1)[1]["target"] == "1:1-00"
    with pytest.raises(ItmCommandError, match="no event at 05:00"):
        await run(
            itm, edit_event, PROGRAM, 1, time="05:00", target="Indoor units", spec=spec
        )


async def test_delete_event(itm: FakeScheduleITM) -> None:
    """Select the event's own row, Delete and Yes."""
    assert await run(itm, delete_event, PROGRAM, 2, "18:00", "Indoor units") is True
    assert [e["time"] for e in weekly(itm, 2)] == ["06:00"]
    assert await run(itm, delete_event, PROGRAM, "Ex1", "08:00", "1:1-00") is True
    assert itm.special_events[PROGRAM]["Ex1"] == []
    with pytest.raises(ItmCommandError, match="no event at 18:00"):
        await run(itm, delete_event, PROGRAM, 2, "18:00", "Indoor units")


async def test_cancelled_event_list_drops_its_changes(itm: FakeScheduleITM) -> None:
    """An error after a change inside the editor saves nothing of it."""
    c = FakeClient(itm)
    async with c._session() as s:
        await s.select_program(PROGRAM)
        await s.send(100, 11)
        await s.send(104, 1, 8, 1, 1)
        await s.send(104, 54)  # Wednesday
        await s.send(104, 73)
        await s.send(107, 4, 1, 8, 1, array=(0,))
        reply = await s.send(107, 7)
        await s.answer_confirm(107, reply)
        # Cancel: the deletion is dropped
        await s.close_screen(MenuScreen.SCHEDULE_EVENTS)
        await s.close_editor()
        await s.close_screen(MenuScreen.SCHEDULE)
        await s.close_menu()
    assert [e["time"] for e in weekly(itm, 3)] == ["06:00", "18:00"]


# -- period, names, calendar ----------------------------------------------------------


async def test_set_and_remove_period(itm: FakeScheduleITM) -> None:
    """Tick the period, pick its months and days; untick it again."""
    assert await run(itm, set_period, PROGRAM, ((6, 1), (8, 31))) is True
    program = next(p for p in itm.schedules if p[1] == PROGRAM)
    assert program[2:] == ["01/06", "31/08"]
    read = await FakeClient(itm).fetch_schedule(PROGRAM, events=False)
    assert read.period == ("Jun/1", "Aug/31")
    assert await run(itm, set_period, PROGRAM, None) is True
    assert program[2:] == ["", ""]
    with pytest.raises(ValueError, match="period date"):
        await run(itm, set_period, PROGRAM, ((13, 1), (1, 1)))


async def test_rename_special_day(itm: FakeScheduleITM) -> None:
    """The name input takes 1-15 characters; only special days have names."""
    assert await run(itm, rename_special_day, PROGRAM, "Ex2", "Holiday") is True
    assert itm.special_names[PROGRAM][1] == "Holiday"
    c = FakeClient(itm)
    async with c._session() as s:
        await s.select_program(PROGRAM)
        await s.send(100, 11)
        assert special_day_names(s.state)[:2] == ("Ex1", "Holiday")
        await s.close_editor()
        await s.close_menu()
    with pytest.raises(ItmCommandError, match="1-15 characters"):
        await run(itm, rename_special_day, PROGRAM, "Ex3", "x" * 16)
    assert itm.special_names[PROGRAM][2] == "Ex3"
    with pytest.raises(ValueError, match="only special days"):
        await run(itm, rename_special_day, PROGRAM, 1, "Monday")


async def test_calendar_rules(itm: FakeScheduleITM) -> None:
    """Add a date rule and a weekday rule; delete a rule as read back."""
    christmas = CalendarRuleSpec(pattern=2, month=12, day=25)
    assert await run(itm, add_calendar_rule, PROGRAM, christmas) is True
    last_sunday = CalendarRuleSpec(pattern=3, week=5, weekday=0)
    assert await run(itm, add_calendar_rule, PROGRAM, last_sunday) is True
    assert itm.calendar_rows == [
        ["Jan", "1", "Ex1"],
        ["Dec", "25", "Ex2"],
        ["Every", "Last Sunday", "Ex3"],
    ]
    read = await FakeClient(itm).fetch_schedule(PROGRAM, events=False)
    assert read.calendar[1] == CalendarRule("Dec", "25", "Ex2")
    assert await run(itm, delete_calendar_rule, PROGRAM, read.calendar[0]) is True
    assert itm.calendar_rows[0] == ["Dec", "25", "Ex2"]
    with pytest.raises(ItmCommandError, match="no calendar rule"):
        await run(itm, delete_calendar_rule, PROGRAM, CalendarRule("Feb", "2", "Ex1"))


def test_specs_refuse_what_the_itm_cannot_take() -> None:
    """EventSpec and CalendarRuleSpec check their values before anything is sent."""
    bad_events: list[dict[str, Any]] = [
        {"time": "24:00"},
        {"time": "7:30"},
        {"time": "07:30", "power": PowerState.SETBACK},
        {"time": "07:30", "fan_speed": "turbo"},
        {"time": "07:30", "lock_mode": RemoteLock.STOP_ONLY},
        {"time": "07:30", "lock_setpoint": RemoteLock.STOP_ONLY},
        {"time": "07:30", "lock_start_stop": RemoteLock.UNKNOWN},
    ]
    for values in bad_events:
        with pytest.raises(ValueError):  # noqa: PT011 - each message differs
            EventSpec(target="1:1-09", **values)
    bad_rules: list[dict[str, Any]] = [
        {"pattern": 1},
        {"pattern": 1, "day": 1, "week": 1, "weekday": 0},
        {"pattern": 1, "week": 1},
        {"pattern": 6, "day": 1},
        {"pattern": 1, "month": 13, "day": 1},
        {"pattern": 1, "day": 32},
        {"pattern": 1, "week": 6, "weekday": 0},
        {"pattern": 1, "week": 1, "weekday": 7},
    ]
    for values in bad_rules:
        with pytest.raises(ValueError):  # noqa: PT011 - each message differs
            CalendarRuleSpec(**values)


# -- through the client's wrappers ----------------------------------------------------


async def test_client_event_wrappers(itm: FakeScheduleITM) -> None:
    """Add, edit and delete through ItmClient, each read back in a fresh session."""
    c = FakeClient(itm)
    spec = EventSpec(time="07:00", target="1:1-09", power=PowerState.OFF)
    event = await c.add_schedule_event(PROGRAM, 3, spec)
    assert event is not None
    assert (event.time, event.target, event.power) == (
        "07:00",
        "1:1-09",
        PowerState.OFF,
    )
    changed = await c.edit_schedule_event(
        PROGRAM, 3, "07:00", "1:1-09", EventSpec(time="07:15", target="1:1-09")
    )
    assert changed is not None
    assert changed.time == "07:15"
    await c.delete_schedule_event(PROGRAM, 3, "07:15", "1:1-09")
    assert not any(e["time"] == "07:15" for e in weekly(itm, 3))
    dry = FakeClient(itm, dry_run=True)
    assert await dry.add_schedule_event(PROGRAM, 3, spec) is None
    assert len(dry.planned) == 1
    with pytest.raises(ReadOnlyError):
        await FakeClient(itm, read_only=True).add_schedule_event(PROGRAM, 3, spec)


async def test_edit_event_keeps_unset_settings(itm: FakeScheduleITM) -> None:
    """keep_unset keeps what the spec leaves out; without it, it is cleared."""
    c = FakeClient(itm)
    spec = EventSpec(
        time="07:00", target="1:1-09", power=PowerState.ON, mode="heat", setpoint=21
    )
    await c.add_schedule_event(PROGRAM, 3, spec)
    kept = await c.edit_schedule_event(
        PROGRAM,
        3,
        "07:00",
        "1:1-09",
        EventSpec(time="07:30", target="1:1-09", setpoint=20),
        keep_unset=True,
    )
    assert kept is not None
    assert (kept.time, kept.power, kept.mode, kept.setpoint) == (
        "07:30",
        PowerState.ON,
        "heat",
        20,
    )
    replaced = await c.edit_schedule_event(
        PROGRAM,
        3,
        "07:30",
        "1:1-09",
        EventSpec(time="07:30", target="1:1-09", setpoint=19),
    )
    assert replaced is not None
    assert (replaced.power, replaced.mode, replaced.setpoint) == (None, None, 19)
    with pytest.raises(ItmCommandError, match="no event at 09:00"):
        await c.edit_schedule_event(
            PROGRAM,
            3,
            "09:00",
            "1:1-09",
            EventSpec(time="09:00", target="1:1-09"),
            keep_unset=True,
        )
    await c.delete_schedule_event(PROGRAM, 3, "07:30", "1:1-09")


async def test_client_program_wrappers(itm: FakeScheduleITM) -> None:
    """Period, special day name and calendar rules through ItmClient."""
    c = FakeClient(itm)
    program = await c.set_schedule_period(PROGRAM, ((5, 1), (9, 30)))
    assert program is not None
    assert program.period is not None
    program = await c.set_schedule_period(PROGRAM, None)
    assert program is not None
    assert program.period is None
    program = await c.rename_schedule_special_day(PROGRAM, "Ex2", "Feest")
    assert program is not None
    assert program.special_names[1] == "Feest"
    before = program.calendar
    program = await c.add_schedule_calendar_rule(
        PROGRAM, CalendarRuleSpec(pattern=2, month=12, day=25)
    )
    assert program is not None
    assert len(program.calendar) == len(before) + 1
    added = next(r for r in program.calendar if r not in before)
    program = await c.delete_schedule_calendar_rule(PROGRAM, added)
    assert program is not None
    assert program.calendar == before


# -- copies and clearing ------------------------------------------------------------


async def test_copy_program(itm: FakeScheduleITM) -> None:
    """Copy + rename + OK; the copy of an enabled program is saved disabled."""
    assert await run(itm, copy_program, PROGRAM, "demo copy") is True
    copied = next(p for p in itm.schedules if p[1] == "demo copy")
    assert copied[0] == 3  # disabled
    assert itm.schedule_events["demo copy"] == itm.schedule_events[PROGRAM]
    assert await run(itm, copy_program, PROGRAM, "demo copy 2", keep_enabled=True)
    assert next(p for p in itm.schedules if p[1] == "demo copy 2")[0] == 0
    with pytest.raises(ItmCommandError, match="exists already"):
        await run(itm, copy_program, PROGRAM, "demo copy")
    count = len(itm.schedules)
    assert (
        await run(itm, copy_program, PROGRAM, "lib dry", client={"dry_run": True})
        is False
    )
    assert len(itm.schedules) == count


async def test_copy_day(itm: FakeScheduleITM) -> None:
    """A weekday's events copied to two other weekdays, saved with the editor."""
    monday = weekly(itm, 1)
    assert monday
    assert await run(itm, copy_day, PROGRAM, 1, (0, 6)) is True
    assert weekly(itm, 0) == monday
    assert weekly(itm, 6) == monday
    with pytest.raises(ItmCommandError, match="same kind"):
        await run(itm, copy_day, PROGRAM, 1, ("Ex1",))
    with pytest.raises(ItmCommandError, match="onto itself"):
        await run(itm, copy_day, PROGRAM, 1, (1,))
    with pytest.raises(ItmCommandError, match="same kind"):
        await run(itm, copy_day, PROGRAM, 1, ())


async def test_clear_day(itm: FakeScheduleITM) -> None:
    """Delete on a day's pattern, confirmed, then saved."""
    assert weekly(itm, 2)
    assert await run(itm, clear_day, PROGRAM, 2) is True
    assert not weekly(itm, 2)
    assert weekly(itm, 3)


async def test_copy_event(itm: FakeScheduleITM) -> None:
    """An event copied to another time keeps its settings."""
    original = dict(weekly(itm, 1)[0])
    assert (
        await run(
            itm,
            copy_event,
            PROGRAM,
            1,
            time=original["time"],
            target=original["target"],
            new_time="07:30",
        )
        is True
    )
    copied = next(e for e in weekly(itm, 1) if e["time"] == "07:30")
    assert copied["target"] == original["target"]
    assert copied.get("setpoint") == original.get("setpoint")
    with pytest.raises(ValueError, match="HH:MM"):
        await run(
            itm, copy_event, PROGRAM, 1, time="06:00", target="x", new_time="7:30"
        )


async def test_copy_calendar(itm: FakeScheduleITM) -> None:
    """Only to explicitly named other programs; confirmed; dry run plans."""
    others = [str(p[1]) for p in itm.schedules if p[1] != PROGRAM]
    assert await run(itm, copy_calendar, PROGRAM, (others[0],)) is True
    assert itm.calendar_copies == [(PROGRAM, [others[0]])]
    for targets, match in (
        ((), "explicitly named"),
        ((PROGRAM,), "own program"),
        (("Nowhere",), "not offered"),
    ):
        with pytest.raises(ItmCommandError, match=match):
            await run(itm, copy_calendar, PROGRAM, targets)
    dry = await run(
        itm, copy_calendar, PROGRAM, tuple(others), client={"dry_run": True}
    )
    assert dry is False
    assert len(itm.calendar_copies) == 1
    assert 106 not in itm.open_screens


async def test_client_copy_wrappers(itm: FakeScheduleITM) -> None:
    """Program, day and event copies and clearing a day through ItmClient."""
    c = FakeClient(itm)
    copy = await c.copy_schedule(PROGRAM, "demo copy")
    assert copy is not None
    assert copy.enabled is False
    program = await c.copy_schedule_day("demo copy", 1, [2])
    assert program is not None
    assert program.weekly.get(2)
    program = await c.clear_schedule_day("demo copy", 2)
    assert program is not None
    assert not program.weekly.get(2)
    first = program.weekly[1][0]
    event = await c.copy_schedule_event(
        "demo copy", 1, first.time, first.target, "23:45"
    )
    assert event is not None
    assert event.time == "23:45"
    await c.copy_schedule_calendar(PROGRAM, ["demo copy"])
    dry = FakeClient(itm, dry_run=True)
    assert await dry.copy_schedule(PROGRAM, "other") is None
