# ruff: noqa: SLF001
# pylint: disable=protected-access
"""Change what a schedule program does: its events, period, special days, calendar.

These functions drive a program's editor (screen 104) in an open session; the
client's write methods wrap them in a session and read the result back. They use
the session's own helpers (program selection, confirm answers), so they reach into
its private methods.

Everything is local to the editor until its OK, which the iTM confirms with
"apply settings?" (Yes). That OK is the one request that saves, and it goes
through the session's write(), so a dry run plans it instead and the editor is left
with Cancel: nothing changes. The editor is always left, saved or not.

The flows come from the web UI's script.js and captures of a real iTM: a new event
has no target and offers no settings until one is picked; a setting's controls are
enabled only once its checkbox is ticked; the event list has separator rows, so an
event is selected by its own row; a part the iTM shows as disabled is never clicked
(its web server hangs on that).
"""

from __future__ import annotations

import dataclasses
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from .exceptions import ItmCommandError
from .lang import mode_from_label
from .menu import (
    EV_EDIT,
    EV_LIST,
    EVT_FAN,
    EVT_FAN_ICON,
    EVT_LOCK_MODE,
    EVT_LOCK_SETPOINT,
    EVT_LOCK_START_STOP,
    EVT_MODE,
    EVT_MODE_COMBO,
    EVT_OVERTIME,
    EVT_OVERTIME_COMBO,
    EVT_POWER,
    EVT_SETPOINT,
    EVT_SETPOINT_FIXED,
    SCH_EDIT_BUTTON,
    SE_DAY_RADIOS,
    SE_DISABLED,
    SE_EDIT_DAY,
    SE_EDIT_SPECIAL,
    SE_OK,
    SE_PERIOD,
    SE_PERIOD_CHECK,
    SE_RENAME,
    SE_SPECIAL_RADIOS,
    SE_TAB,
    SE_TAB_CALENDAR,
    SE_TAB_PROPERTIES,
    SE_TAB_SPECIAL,
    SE_TAB_WEEKLY,
    CalendarRule,
    MenuScreen,
    ScheduleEvent,
    day_events_from_screen,
    program_from_editor,
    schedules_from_screen,
)
from .models import FAN_ICONS, FAN_SPEEDS, PowerState, RemoteLock
from .protocol import DataType, EventType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from .client import _Session
    from .menu import ScheduleProgram, ScheduleSummary
    from .protocol import Reply
    from .state import UIState

_T = TypeVar("_T")

KEPT_EVENT_SETTINGS = (
    "power",
    "mode",
    "setpoint",
    "lock_start_stop",
    "lock_mode",
    "lock_setpoint",
    "overtime",
    "fan_speed",
)

EDITOR = MenuScreen.SCHEDULE_EDITOR
EVENTS = MenuScreen.SCHEDULE_EVENTS
EVENT = MenuScreen.SCHEDULE_EVENT
PICKER = 110  # ScSchPntAreaSel: the point/zone picker of an event

# Event list (107).
EV_ADD, EV_DELETE, EV_OK, EV_CANCEL = 5, 7, 9, 10
# Event screen (109).
EVT_OK, EVT_TIME_BUTTON, EVT_TARGET_BUTTON, EVT_SETPOINT_BUTTON = 5, 44, 47, 23
EVT_FAN_UP = 29
# Picker (110): tab 0 is points, 1 zones.
PK_TAB, PK_POINTS, PK_ZONES, PK_OK, PK_CANCEL = 1, 2, 5, 3, 4
# Editor (104).
SE_SPECIAL_RENAME = 103
SE_CAL_LIST, SE_CAL_DELETE, SE_CAL_PATTERN = 26, 29, 32
SE_CAL_DATE_RADIO, SE_CAL_DATE_MONTH, SE_CAL_DATE_DAY = 33, 37, 38
SE_CAL_WEEK_RADIO, SE_CAL_WEEK_MONTH, SE_CAL_WEEK, SE_CAL_WEEKDAY = 39, 43, 44, 46
SE_CAL_ADD = 49

POWER_RADIOS = {
    PowerState.ON: 3,
    PowerState.OFF: 4,
    PowerState.SETBACK_HIGH: 54,
    PowerState.SETBACK_LOW: 55,
}
# Lock radios in the order allowed, stop only, forbidden (two-way locks skip one).
LOCK_ORDER = (RemoteLock.ALLOWED, RemoteLock.STOP_ONLY, RemoteLock.FORBIDDEN)
_TIME = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")
_SPECIAL = re.compile(r"^Ex([1-5])$")

# A day of a program: a weekday (0 = Sunday .. 6 = Saturday) or a special day
# ("Ex1" .. "Ex5", whatever it is named).
Day = int | str


@dataclass(frozen=True)
class EventSpec:  # pylint: disable=too-many-instance-attributes
    """An event to add, or what an event should become.

    A setting left None is not part of the event. ``mode`` is heat, cool, fan, auto
    or setpoint (a setpoint-only event); the iTM offers only what the target
    supports, and the write fails before saving when it doesn't.
    """

    time: str  # "HH:MM"
    target: str  # a management point (e.g. "1:1-09") or a zone name
    zone: bool = False
    power: PowerState | None = None  # ON, OFF, SETBACK_LOW or SETBACK_HIGH
    mode: str | None = None
    setpoint: float | None = None  # °C
    lock_start_stop: RemoteLock | None = None  # ALLOWED, STOP_ONLY or FORBIDDEN
    lock_mode: RemoteLock | None = None  # ALLOWED or FORBIDDEN
    lock_setpoint: RemoteLock | None = None  # ALLOWED or FORBIDDEN
    overtime: bool | None = None
    fan_speed: str | None = None  # low, medium, high or auto

    def __post_init__(self) -> None:
        """Refuse what the iTM can't take, before anything is sent."""
        if not _TIME.match(self.time):
            msg = f"time must be HH:MM (00:00-23:59), not {self.time!r}"
            raise ValueError(msg)
        if self.power is not None and self.power not in POWER_RADIOS:
            msg = f"power must be one of {list(POWER_RADIOS)}, not {self.power}"
            raise ValueError(msg)
        if self.fan_speed is not None and self.fan_speed not in FAN_SPEEDS:
            msg = f"fan speed must be one of {FAN_SPEEDS}, not {self.fan_speed!r}"
            raise ValueError(msg)
        for lock in (self.lock_start_stop, self.lock_mode, self.lock_setpoint):
            if lock is not None and lock not in LOCK_ORDER:
                msg = f"a lock must be one of {LOCK_ORDER}, not {lock}"
                raise ValueError(msg)
        if self.lock_mode is RemoteLock.STOP_ONLY:
            msg = "the mode lock is allowed or forbidden, not stop only"
            raise ValueError(msg)
        if self.lock_setpoint is RemoteLock.STOP_ONLY:
            msg = "the setpoint lock is allowed or forbidden, not stop only"
            raise ValueError(msg)


@dataclass(frozen=True)
class CalendarRuleSpec:
    """A calendar rule to add: on which dates a special day pattern applies.

    Either a date (``day``) or a weekday of a week of the month (``week`` and
    ``weekday``). ``month`` None means every month.
    """

    pattern: int  # 1-5: Ex1 .. Ex5
    month: int | None = None  # 1-12
    day: int | None = None  # 1-31
    week: int | None = None  # 1-4, or 5 for the last
    weekday: int | None = None  # 0 = Sunday .. 6 = Saturday

    def __post_init__(self) -> None:
        """Refuse an incomplete or impossible rule, before anything is sent."""
        by_date = self.day is not None
        by_week = self.week is not None or self.weekday is not None
        if by_date == by_week or (by_week and None in (self.week, self.weekday)):
            msg = "a rule has either a day, or a week and a weekday"
            raise ValueError(msg)
        checks = (
            (1 <= self.pattern <= 5, "pattern 1-5"),
            (self.month is None or 1 <= self.month <= 12, "month 1-12"),
            (self.day is None or 1 <= self.day <= 31, "day 1-31"),
            (self.week is None or 1 <= self.week <= 5, "week 1-5"),
            (self.weekday is None or 0 <= self.weekday <= 6, "weekday 0-6"),
        )
        for ok, what in checks:
            if not ok:
                msg = f"calendar rule: {what} expected"
                raise ValueError(msg)


# A period: ((begin month, begin day), (end month, end day)); None is all year.
Period = tuple[tuple[int, int], tuple[int, int]]


# -- helpers --------------------------------------------------------------------------


def _usable(state: UIState, screen: int, part: int) -> bool:
    """Tell whether the iTM shows a part as usable (not disabled, not hidden)."""
    enabled = state.get(screen, part, "ENABLED")
    visible = state.get(screen, part, "VISIBLED")
    return bool(enabled != 0 and visible != 0)


async def _click(s: _Session, screen: int, part: int, what: str) -> Reply:
    """Click a part, refusing one the iTM shows as disabled (that hangs it)."""
    if not _usable(s.state, screen, part):
        msg = f"{what}: the iTM does not offer this here"
        raise ItmCommandError(msg)
    return await s.send(screen, part)


async def _pick(s: _Session, screen: int, part: int, index: int, what: str) -> None:
    """Pick a combo option by index (or set a checkbox: 1 ticked, 0 not)."""
    if not _usable(s.state, screen, part):
        msg = f"{what}: the iTM does not offer this here"
        raise ItmCommandError(msg)
    await s.send(screen, part, EventType.CLICK, DataType.INT, index)


async def _select_row(s: _Session, screen: int, part: int, row: int) -> None:
    await s.send(screen, part, EventType.CLICK, DataType.INTARRAY, 1, array=(row,))


async def _tab(s: _Session, tab: int) -> None:
    await s.send(EDITOR, SE_TAB, EventType.TAB_SELECT, DataType.INT, tab)


async def _confirm(s: _Session, screen: int, reply: Reply, what: str) -> None:
    """Answer a confirm with Yes; raise on an error or info dialog."""
    try:
        await s._answer_confirm(screen, reply)
    except ItmCommandError as err:
        msg = f"{what}: {err}"
        raise ItmCommandError(msg) from err


def _parse_day(day: Day) -> tuple[bool, int]:
    """Return (special, index) for a weekday 0-6 or "Ex1".."Ex5"."""
    if isinstance(day, int) and 0 <= day <= 6:
        return False, day
    if isinstance(day, str) and (m := _SPECIAL.match(day)):
        return True, int(m.group(1)) - 1
    msg = f"a day is a weekday 0-6 (0 = Sunday) or 'Ex1'-'Ex5', not {day!r}"
    raise ValueError(msg)


async def open_editor(s: _Session, program: str) -> None:
    """Select a program in the program list and open its editor."""
    s._writable()
    await s._select_program(program)
    await s.send(MenuScreen.SCHEDULE, SCH_EDIT_BUTTON)
    if EDITOR not in s.state.screens:
        msg = f"{program}: the schedule editor did not open"
        raise ItmCommandError(msg)


async def save_editor(s: _Session, label: str) -> bool:
    """Press the editor's OK and confirm; False in a dry run (nothing saved).

    Raises
    ------
        ItmCommandError: The iTM refused, or the editor stayed open.

    """
    reply = await s.write(label, EDITOR, SE_OK)
    if reply is None:
        return False
    await _confirm(s, EDITOR, reply, label)
    if EDITOR in s.state.screens:
        msg = f"{label}: the editor did not close after OK"
        raise ItmCommandError(msg)
    return True


async def leave_editor(s: _Session) -> None:
    """Back out of whatever is open below the editor, then leave it unsaved."""
    if PICKER in s.state.screens:
        await s.send(PICKER, PK_CANCEL)
        s.state.screens.pop(PICKER, None)
    await s.close_screen(EVENT)
    await s.close_screen(EVENTS)
    await s.close_editor()
    await s.close_screen(MenuScreen.SCHEDULE)


async def open_day(s: _Session, day: Day) -> None:
    """Open the event list of a weekday or special day in the open editor."""
    special, index = _parse_day(day)
    if special:
        await _tab(s, SE_TAB_SPECIAL)
        await s.send(EDITOR, SE_SPECIAL_RADIOS[index])
        await _click(s, EDITOR, SE_EDIT_SPECIAL, f"events of {day}")
    else:
        await _tab(s, SE_TAB_WEEKLY)
        await s.send(EDITOR, SE_DAY_RADIOS[index])
        await _click(s, EDITOR, SE_EDIT_DAY, f"events of day {day}")
    if EVENTS not in s.state.screens:
        msg = f"the event list of {day!r} did not open"
        raise ItmCommandError(msg)


async def close_day(s: _Session) -> None:
    """Press the event list's OK (its changes stay in the editor)."""
    reply = await s.send(EVENTS, EV_OK)
    await _confirm(s, EVENTS, reply, "event list OK")
    if EVENTS in s.state.screens:
        msg = "the event list did not close after OK"
        raise ItmCommandError(msg)
    s.state.screens.pop(EVENT, None)


def find_event(state: UIState, time: str, target: str) -> ScheduleEvent | None:
    """Return the shown event list's event at time for target, if there is one."""
    for event in day_events_from_screen(state):
        if event.time == time and event.target == target.strip():
            return event
    return None


async def _select_event(s: _Session, time: str, target: str, button: int) -> None:
    """Select an event by its own row and check the button is enabled for it."""
    event = find_event(s.state, time, target)
    if event is None:
        shown = [(e.time, e.target) for e in day_events_from_screen(s.state)]
        msg = f"no event at {time} for {target!r}; the day has {shown}"
        raise ItmCommandError(msg)
    await _select_row(s, EVENTS, EV_LIST, event.row)
    if not _usable(s.state, EVENTS, button):
        msg = f"the iTM does not offer the event at {time} for {target!r}"
        raise ItmCommandError(msg)


# -- the event screen -----------------------------------------------------------------


async def set_event_time(s: _Session, time: str) -> None:
    """Enter the event's time through its hour:minute input."""
    m = _TIME.match(time)
    if m is None:
        msg = f"time must be HH:MM, not {time!r}"
        raise ValueError(msg)
    hour, minute = int(m.group(1)), int(m.group(2))
    reply = await _click(s, EVENT, EVT_TIME_BUTTON, "event time")
    dialogs = [
        u for u in reply.screens if u.update == "DLG_OPEN" and u.data[1] == "HOURMIN"
    ]
    if not dialogs:
        msg = f"event time: no time input opened: {reply.dialogs()}"
        raise ItmCommandError(msg)
    d = dialogs[0]
    low, high = (d.data[6], d.data[7]), (d.data[8], d.data[9])
    if not low <= (hour, minute) <= high:
        await s.send(d.screen, d.data[0], EventType.CLICK2)
        msg = f"event time: {time} outside the iTM's range {low}-{high}"
        raise ItmCommandError(msg)
    reply = await s.send(
        d.screen,
        d.data[0],
        EventType.CLICK,
        DataType.HOURMIN,
        2,
        array=(hour, minute),
    )
    if reply.dialogs():
        msg = f"event time: the iTM refused {time}: {reply.dialogs()}"
        raise ItmCommandError(msg)


def _row_of(state: UIState, part: int, name: str) -> int | None:
    """Return the picker row whose name (second column) is name."""
    rows = (state.get(PICKER, part, "LIST_ROW") or {}).get("rows", [])
    names = [str(r["text"][1]) if len(r["text"]) > 1 else "" for r in rows]
    if name in names:
        return names.index(name)
    stripped = [n.strip() for n in names]
    return stripped.index(name.strip()) if name.strip() in stripped else None


async def set_event_target(s: _Session, target: str, *, zone: bool) -> None:
    """Pick the event's target (a point or a zone) in the picker."""
    await _click(s, EVENT, EVT_TARGET_BUTTON, "event target")
    if PICKER not in s.state.screens:
        msg = "event target: the point/zone picker did not open"
        raise ItmCommandError(msg)
    part = PK_ZONES if zone else PK_POINTS
    await s.send(PICKER, PK_TAB, EventType.TAB_SELECT, DataType.INT, int(zone))
    row = _row_of(s.state, part, target)
    if row is None:
        kind = "zone" if zone else "point"
        msg = f"event target: no {kind} named {target!r}"
        raise ItmCommandError(msg)
    await _select_row(s, PICKER, part, row)
    reply = await s.send(PICKER, PK_OK)
    if reply.dialogs() or PICKER in s.state.screens:
        msg = f"event target: the iTM did not take {target!r}: {reply.dialogs()}"
        raise ItmCommandError(msg)


async def _tick(s: _Session, checkbox: int, *, on: bool, what: str) -> None:
    """Tick or untick a setting's checkbox, unless it already is."""
    ticked = s.state.get(EVENT, checkbox, "CHECK_STATE") == 1
    if ticked != on:
        await _pick(s, EVENT, checkbox, int(on), what)


def _mode_index(state: UIState, mode: str) -> int | None:
    options = state.get(EVENT, EVT_MODE_COMBO, "COMBO_STRS") or []
    for i, label in enumerate(options):
        found = mode_from_label(str(label)) or (
            "setpoint" if "setpoint" in str(label).lower() else None
        )
        if found == mode:
            return i
    return None


async def _set_lock(
    s: _Session,
    group: tuple[int, tuple[int, ...]],
    lock: RemoteLock,
    what: str,
) -> None:
    checkbox, radios = group
    await _tick(s, checkbox, on=True, what=what)
    order = (
        LOCK_ORDER if len(radios) == 3 else (RemoteLock.ALLOWED, RemoteLock.FORBIDDEN)
    )
    await _click(s, EVENT, radios[order.index(lock)], f"{what} {lock}")


async def _set_fan(s: _Session, speed: str) -> None:
    await _tick(s, EVT_FAN, on=True, what="fan speed")
    # The up button cycles low -> medium -> high -> auto; the iTM shows the icon.
    for _ in range(len(FAN_ICONS) + 1):
        if FAN_ICONS.get(s.state.get(EVENT, EVT_FAN_ICON, "ICONID")) == speed:
            return
        await _click(s, EVENT, EVT_FAN_UP, "fan speed")
    msg = f"fan speed: could not select {speed!r}"
    raise ItmCommandError(msg)


async def apply_event_settings(
    s: _Session, spec: EventSpec, *, replace: bool = False
) -> None:
    """Set the event screen's settings to spec (with replace, untick the others).

    Settings the spec doesn't cover (ventilation, analog, ...) are left as they are.
    """
    settings: tuple[tuple[int, object | None], ...] = (
        (EVT_POWER[0], spec.power),
        (EVT_LOCK_START_STOP[0], spec.lock_start_stop),
        (EVT_LOCK_MODE[0], spec.lock_mode),
        (EVT_LOCK_SETPOINT[0], spec.lock_setpoint),
        (EVT_MODE, spec.mode),
        (EVT_SETPOINT, spec.setpoint),
        (EVT_OVERTIME, spec.overtime),
        (EVT_FAN, spec.fan_speed),
    )
    if replace:
        for checkbox, value in settings:
            if value is None and s.state.get(EVENT, checkbox, "CHECK_STATE") == 1:
                await _pick(s, EVENT, checkbox, 0, "untick a setting")
    if spec.power is not None:
        await _tick(s, EVT_POWER[0], on=True, what="power")
        await _click(s, EVENT, POWER_RADIOS[spec.power], f"power {spec.power}")
    for group, lock, what in (
        (EVT_LOCK_START_STOP, spec.lock_start_stop, "start/stop lock"),
        (EVT_LOCK_MODE, spec.lock_mode, "mode lock"),
        (EVT_LOCK_SETPOINT, spec.lock_setpoint, "setpoint lock"),
    ):
        if lock is not None:
            await _set_lock(s, group, lock, what)
    if spec.mode is not None:
        await _tick(s, EVT_MODE, on=True, what="mode")
        index = _mode_index(s.state, spec.mode)
        if index is None:
            options = s.state.get(EVENT, EVT_MODE_COMBO, "COMBO_STRS")
            msg = f"mode {spec.mode!r} not offered: {options}"
            raise ItmCommandError(msg)
        await _pick(s, EVENT, EVT_MODE_COMBO, index, f"mode {spec.mode}")
    if spec.setpoint is not None:
        await _tick(s, EVT_SETPOINT, on=True, what="setpoint")
        await _click(s, EVENT, EVT_SETPOINT_FIXED, "setpoint")
        if not _usable(s.state, EVENT, EVT_SETPOINT_BUTTON):
            msg = "setpoint: the iTM does not offer this here"
            raise ItmCommandError(msg)
        await s.answer_number(
            EVT_SETPOINT_BUTTON, spec.setpoint, "event setpoint", screen=EVENT
        )
    if spec.overtime is not None:
        await _tick(s, EVT_OVERTIME, on=True, what="overtime")
        # The combo lists on, off.
        await _pick(s, EVENT, EVT_OVERTIME_COMBO, 0 if spec.overtime else 1, "overtime")
    if spec.fan_speed is not None:
        await _set_fan(s, spec.fan_speed)


async def event_ok(s: _Session, what: str) -> None:
    """Press the event screen's OK; it closes and the list shows the event."""
    reply = await s.send(EVENT, EVT_OK)
    await _confirm(s, EVENT, reply, what)
    if EVENT in s.state.screens:
        msg = f"{what}: the event screen did not close after OK"
        raise ItmCommandError(msg)


# -- the flows ------------------------------------------------------------------------


async def add_event(s: _Session, program: str, day: Day, spec: EventSpec) -> bool:
    """Add an event to a weekday or special day of a program and save it.

    Returns
    -------
        True when saved, False in a dry run.

    Raises
    ------
        ItmCommandError: The day has an event at that time for that target already,
            the target or a setting is not offered, or the iTM refused.

    """
    _parse_day(day)
    await open_editor(s, program)
    try:
        await open_day(s, day)
        if find_event(s.state, spec.time, spec.target) is not None:
            msg = f"{program} {day}: an event at {spec.time} for {spec.target!r} exists"
            raise ItmCommandError(msg)
        await _click(s, EVENTS, EV_ADD, "add event")
        if EVENT not in s.state.screens:
            msg = "add event: the event screen did not open"
            raise ItmCommandError(msg)
        await set_event_time(s, spec.time)
        await set_event_target(s, spec.target, zone=spec.zone)
        await apply_event_settings(s, spec)
        await event_ok(s, "add event")
        await close_day(s)
        return await save_editor(s, f"add event {spec.time} {spec.target} to {day}")
    finally:
        await leave_editor(s)


async def edit_event(  # noqa: PLR0913  # pylint: disable=too-many-arguments
    s: _Session,
    program: str,
    day: Day,
    *,
    time: str,
    target: str,
    spec: EventSpec,
) -> bool:
    """Replace an event: its time, target and the settings EventSpec covers.

    Returns
    -------
        True when saved, False in a dry run.

    """
    _parse_day(day)
    await open_editor(s, program)
    try:
        await open_day(s, day)
        await _select_event(s, time, target, EV_EDIT)
        await s.send(EVENTS, EV_EDIT)
        if EVENT not in s.state.screens:
            msg = "edit event: the event screen did not open"
            raise ItmCommandError(msg)
        if spec.time != time:
            await set_event_time(s, spec.time)
        if spec.target.strip() != target.strip():
            await set_event_target(s, spec.target, zone=spec.zone)
        await apply_event_settings(s, spec, replace=True)
        await event_ok(s, "edit event")
        await close_day(s)
        return await save_editor(s, f"edit event {time} {target} of {day}")
    finally:
        await leave_editor(s)


async def delete_event(
    s: _Session, program: str, day: Day, time: str, target: str
) -> bool:
    """Delete the event at time for target from a weekday or special day.

    Returns
    -------
        True when saved, False in a dry run.

    """
    _parse_day(day)
    await open_editor(s, program)
    try:
        await open_day(s, day)
        await _select_event(s, time, target, EV_DELETE)
        reply = await s.send(EVENTS, EV_DELETE)
        await _confirm(s, EVENTS, reply, "delete event")
        if find_event(s.state, time, target) is not None:
            msg = f"delete event: {time} {target!r} is still listed"
            raise ItmCommandError(msg)
        await close_day(s)
        return await save_editor(s, f"delete event {time} {target} of {day}")
    finally:
        await leave_editor(s)


def _option_index(state: UIState, part: int, value: int) -> int | None:
    """Return the index of a numeric option (COMBO_NUMS) or of option number value."""
    nums = state.get(EDITOR, part, "COMBO_NUMS")
    if nums:
        return list(nums).index(value) if value in nums else None
    options = state.get(EDITOR, part, "COMBO_STRS") or []
    return value - 1 if 1 <= value <= len(options) else None


async def set_period(  # pylint: disable=too-many-locals
    s: _Session, program: str, period: Period | None
) -> bool:
    """Give a program a period ((month, day), (month, day)) or none (all year).

    Returns
    -------
        True when saved, False in a dry run.

    """
    if period is not None:
        for month, day in period:
            if not (1 <= month <= 12 and 1 <= day <= 31):
                msg = f"a period date is (month 1-12, day 1-31), not {(month, day)}"
                raise ValueError(msg)
    await open_editor(s, program)
    try:
        await _tab(s, SE_TAB_PROPERTIES)
        on = period is not None
        if (s.state.get(EDITOR, SE_PERIOD_CHECK, "CHECK_STATE") == 1) != on:
            await _pick(s, EDITOR, SE_PERIOD_CHECK, int(on), "period")
        if period is not None:
            (m1, d1), (m2, d2) = period
            for part, value, what in zip(
                SE_PERIOD,
                (m1, d1, m2, d2),
                ("begin month", "begin day", "end month", "end day"),
                strict=True,
            ):
                index = _option_index(s.state, part, value)
                if index is None:
                    msg = f"period {what}: {value} not offered"
                    raise ItmCommandError(msg)
                await _pick(s, EDITOR, part, index, f"period {what}")
        label = "set period" if on else "remove period"
        return await save_editor(s, f"{label} of {program}")
    finally:
        await leave_editor(s)


def special_day_names(state: UIState) -> tuple[str, ...]:
    """Return the special day names (Ex1..Ex5 unless renamed) of the open editor."""
    return tuple(
        str(state.get(EDITOR, part, "LABEL") or f"Ex{i + 1}")
        for i, part in enumerate(SE_SPECIAL_RADIOS)
    )


async def rename_special_day(s: _Session, program: str, day: Day, name: str) -> bool:
    """Rename a special day pattern ("Ex1".."Ex5") of a program.

    Returns
    -------
        True when saved, False in a dry run.

    """
    special, index = _parse_day(day)
    if not special:
        msg = f"only special days have names, not weekday {day!r}"
        raise ValueError(msg)
    await open_editor(s, program)
    try:
        await _tab(s, SE_TAB_SPECIAL)
        await s.send(EDITOR, SE_SPECIAL_RADIOS[index])
        await s.answer_text(EDITOR, SE_SPECIAL_RENAME, name, f"name of {day}")
        return await save_editor(s, f"rename {day} of {program} to {name}")
    finally:
        await leave_editor(s)


def _calendar_rows(state: UIState) -> list[list[str]]:
    rows = (state.get(EDITOR, SE_CAL_LIST, "LIST_ROW") or {}).get("rows", [])
    return [[str(t).strip() for t in r["text"]] for r in rows]


async def add_calendar_rule(s: _Session, program: str, rule: CalendarRuleSpec) -> bool:
    """Add a calendar rule (which dates use a special day pattern) to a program.

    Returns
    -------
        True when saved, False in a dry run.

    """
    await open_editor(s, program)
    try:
        await _tab(s, SE_TAB_CALENDAR)
        before = len(_calendar_rows(s.state))
        await _pick(s, EDITOR, SE_CAL_PATTERN, rule.pattern - 1, "calendar pattern")
        month = 12 if rule.month is None else rule.month - 1  # 12: every month
        if rule.day is not None:
            await _click(s, EDITOR, SE_CAL_DATE_RADIO, "calendar date rule")
            await _pick(s, EDITOR, SE_CAL_DATE_MONTH, month, "calendar month")
            index = _option_index(s.state, SE_CAL_DATE_DAY, rule.day)
            if index is None:
                msg = f"calendar day {rule.day} not offered"
                raise ItmCommandError(msg)
            await _pick(s, EDITOR, SE_CAL_DATE_DAY, index, "calendar day")
        else:
            await _click(s, EDITOR, SE_CAL_WEEK_RADIO, "calendar weekday rule")
            await _pick(s, EDITOR, SE_CAL_WEEK_MONTH, month, "calendar month")
            await _pick(s, EDITOR, SE_CAL_WEEK, (rule.week or 1) - 1, "calendar week")
            await _pick(
                s, EDITOR, SE_CAL_WEEKDAY, rule.weekday or 0, "calendar weekday"
            )
        reply = await _click(s, EDITOR, SE_CAL_ADD, "add calendar rule")
        await _confirm(s, EDITOR, reply, "add calendar rule")
        if len(_calendar_rows(s.state)) != before + 1:
            msg = "add calendar rule: the iTM did not list it"
            raise ItmCommandError(msg)
        return await save_editor(s, f"add calendar rule to {program}")
    finally:
        await leave_editor(s)


async def delete_calendar_rule(s: _Session, program: str, rule: CalendarRule) -> bool:
    """Delete a calendar rule, as read with fetch_schedule (month, day, pattern).

    Returns
    -------
        True when saved, False in a dry run.

    """
    wanted = [rule.month.strip(), rule.day.strip(), rule.pattern.strip()]
    await open_editor(s, program)
    try:
        await _tab(s, SE_TAB_CALENDAR)
        rows = _calendar_rows(s.state)
        if wanted not in rows:
            msg = f"{program}: no calendar rule {wanted}; the calendar has {rows}"
            raise ItmCommandError(msg)
        await _select_row(s, EDITOR, SE_CAL_LIST, rows.index(wanted))
        reply = await _click(s, EDITOR, SE_CAL_DELETE, "delete calendar rule")
        await _confirm(s, EDITOR, reply, "delete calendar rule")
        if len(_calendar_rows(s.state)) != len(rows) - 1:
            msg = "delete calendar rule: the iTM still lists it"
            raise ItmCommandError(msg)
        return await save_editor(s, f"delete calendar rule of {program}")
    finally:
        await leave_editor(s)


# -- copies and clearing ------------------------------------------------------------

# Copies: program list (100), editor (104, Delete clears a day), event list (107).
SCH_COPY, SCH_CALENDAR_COPY = 9, 12
SE_COPY_DAY, SE_CLEAR_DAY, SE_COPY_SPECIAL, SE_CLEAR_SPECIAL = 71, 72, 100, 101
EV_COPY = 6
# Day copy (108) and calendar copy (106).
DAY_COPY, DC_TARGETS, DC_OK, DC_CANCEL = 108, 4, 5, 6
CALENDAR_COPY, CC_TARGETS, CC_OK, CC_CANCEL = 106, 4, 15, 16


def _list_names(state: UIState, screen: int, part: int) -> list[str]:
    rows = (state.get(screen, part, "LIST_ROW") or {}).get("rows", [])
    return [str((r.get("text") or [""])[0]).strip() for r in rows]


async def _select_rows(s: _Session, screen: int, part: int, rows: list[int]) -> None:
    await s.send(
        screen, part, EventType.CLICK, DataType.INTARRAY, len(rows), array=tuple(rows)
    )


async def copy_program(
    s: _Session, program: str, name: str, *, keep_enabled: bool = False
) -> bool:
    """Copy a program under a new name and save the copy.

    Copy opens the editor on an unsaved copy (with a default name); it is renamed
    and saved. The copy is saved disabled unless keep_enabled (a copy of an enabled
    program would otherwise start switching units at once).

    Returns
    -------
        True when saved, False in a dry run.

    """
    await s._select_program(program)
    names = [p.name for p in schedules_from_screen(s.state)]
    if name in names:
        await s.close_screen(MenuScreen.SCHEDULE)
        msg = f"schedule program {name!r} exists already"
        raise ItmCommandError(msg)
    try:
        await _click(s, MenuScreen.SCHEDULE, SCH_COPY, "copy program")
        if EDITOR not in s.state.screens:
            msg = f"{program}: the copy did not open in the editor"
            raise ItmCommandError(msg)
        await s.answer_text(EDITOR, SE_RENAME, name, "program name")
        if not keep_enabled and program_from_editor(s.state).enabled:
            await _click(s, EDITOR, SE_DISABLED, "disable the copy")
        return await save_editor(s, f"copy program {program} to {name}")
    finally:
        await leave_editor(s)


async def copy_day(
    s: _Session, program: str, day: Day, targets: tuple[Day, ...]
) -> bool:
    """Copy a weekday's (or special day's) events to other days of the same kind.

    Returns
    -------
        True when saved, False in a dry run.

    """
    special, index = _parse_day(day)
    parsed = [_parse_day(t) for t in targets]
    if not targets or any(kind != special for kind, _ in parsed):
        msg = "copy a day to one or more days of the same kind (weekdays or Ex1-Ex5)"
        raise ItmCommandError(msg)
    await open_editor(s, program)
    try:
        if special:
            await _tab(s, SE_TAB_SPECIAL)
            await s.send(EDITOR, SE_SPECIAL_RADIOS[index])
            await _click(s, EDITOR, SE_COPY_SPECIAL, f"copy {day}")
        else:
            await _tab(s, SE_TAB_WEEKLY)
            await s.send(EDITOR, SE_DAY_RADIOS[index])
            await _click(s, EDITOR, SE_COPY_DAY, f"copy day {day}")
        if DAY_COPY not in s.state.screens:
            msg = f"the copy screen for {day!r} did not open"
            raise ItmCommandError(msg)
        # The list holds the other days of that kind, in order, without the source.
        others = [i for i in range(5 if special else 7) if i != index]
        rows = sorted(others.index(i) for _, i in parsed if i in others)
        if len(rows) != len(targets):
            await s.send(DAY_COPY, DC_CANCEL)
            msg = f"copy {day}: a day can't be copied onto itself"
            raise ItmCommandError(msg)
        await _select_rows(s, DAY_COPY, DC_TARGETS, rows)
        reply = await _click(s, DAY_COPY, DC_OK, "copy day OK")
        await _confirm(s, DAY_COPY, reply, "copy day")
        s.state.screens.pop(DAY_COPY, None)
        return await save_editor(s, f"copy {day} of {program} to {list(targets)}")
    finally:
        if DAY_COPY in s.state.screens:
            await s.send(DAY_COPY, DC_CANCEL)
            s.state.screens.pop(DAY_COPY, None)
        await leave_editor(s)


async def clear_day(s: _Session, program: str, day: Day) -> bool:
    """Delete every event of a weekday or special day (its pattern's Delete).

    Returns
    -------
        True when saved, False in a dry run.

    """
    special, index = _parse_day(day)
    await open_editor(s, program)
    try:
        if special:
            await _tab(s, SE_TAB_SPECIAL)
            await s.send(EDITOR, SE_SPECIAL_RADIOS[index])
            reply = await _click(s, EDITOR, SE_CLEAR_SPECIAL, f"clear {day}")
        else:
            await _tab(s, SE_TAB_WEEKLY)
            await s.send(EDITOR, SE_DAY_RADIOS[index])
            reply = await _click(s, EDITOR, SE_CLEAR_DAY, f"clear day {day}")
        await _confirm(s, EDITOR, reply, f"clear {day}")
        return await save_editor(s, f"clear {day} of {program}")
    finally:
        await leave_editor(s)


async def copy_event(  # noqa: PLR0913  # pylint: disable=too-many-arguments
    s: _Session,
    program: str,
    day: Day,
    *,
    time: str,
    target: str,
    new_time: str,
    new_target: str | None = None,
    zone: bool = False,
) -> bool:
    """Copy an event of a day to another time (and target), keeping its settings.

    Copy opens the event screen on a copy; the time (and target) are changed and
    the copy added to the list.

    Returns
    -------
        True when saved, False in a dry run.

    """
    if not _TIME.match(new_time):
        msg = f"time must be HH:MM (00:00-23:59), not {new_time!r}"
        raise ValueError(msg)
    _parse_day(day)
    await open_editor(s, program)
    try:
        await open_day(s, day)
        await _select_event(s, time, target, EV_COPY)
        await s.send(EVENTS, EV_COPY)
        if EVENT not in s.state.screens:
            msg = "copy event: the iTM did not open the copy"
            raise ItmCommandError(msg)
        await set_event_time(s, new_time)
        if new_target is not None:
            await set_event_target(s, new_target, zone=zone)
        await event_ok(s, f"copy of {time} {target}")
        if find_event(s.state, new_time, new_target or target) is None:
            msg = f"copy event: {new_time} is not listed"
            raise ItmCommandError(msg)
        await close_day(s)
        return await save_editor(s, f"copy event {time} {target} of {day}")
    finally:
        await leave_editor(s)


async def copy_calendar(s: _Session, program: str, targets: tuple[str, ...]) -> bool:
    """Copy a program's calendar to other programs, named explicitly.

    This changes the target programs at once (not through an editor): the targets
    must be given; there is no "all".

    Returns
    -------
        True when copied, False in a dry run.

    """
    if not targets:
        msg = "copy a calendar only to explicitly named programs"
        raise ItmCommandError(msg)
    if program in targets:
        msg = f"{program}: a calendar can't be copied onto its own program"
        raise ItmCommandError(msg)
    await s._select_program(program)
    try:
        await _click(s, MenuScreen.SCHEDULE, SCH_CALENDAR_COPY, "calendar copy")
        if CALENDAR_COPY not in s.state.screens:
            msg = "the calendar copy screen did not open"
            raise ItmCommandError(msg)
        names = _list_names(s.state, CALENDAR_COPY, CC_TARGETS)
        missing = [t for t in targets if t not in names]
        if missing:
            msg = f"calendar copy: {missing} not offered; programs: {names}"
            raise ItmCommandError(msg)
        await _select_rows(
            s, CALENDAR_COPY, CC_TARGETS, sorted(names.index(t) for t in targets)
        )
        return await s.press_ok(
            f"copy the calendar of {program} to {list(targets)}",
            CALENDAR_COPY,
            CC_OK,
            CC_CANCEL,
        )
    finally:
        if CALENDAR_COPY in s.state.screens:
            await s.send(CALENDAR_COPY, CC_CANCEL)
            s.state.screens.pop(CALENDAR_COPY, None)
        await s.close_screen(MenuScreen.SCHEDULE)


async def _ignore(call: Awaitable[Any]) -> None:
    await call


class ScheduleMixin:
    """ItmClient's schedule program and event writes."""

    if TYPE_CHECKING:
        # pylint: disable=unused-argument
        dry_run: bool

        async def _in_session(
            self, call: Callable[[_Session], Awaitable[_T]]
        ) -> _T: ...

        def _writable(self) -> None: ...

        async def fetch_schedule(
            self, name: str, *, events: bool = True, details: bool = False
        ) -> ScheduleProgram:
            """Read a schedule program in full."""

        async def fetch_schedules(self) -> list[ScheduleSummary]:
            """Read the schedule programs."""

    async def _schedule_write(
        self, call: Callable[[_Session], Awaitable[bool]]
    ) -> None:
        self._writable()
        await self._in_session(call)

    async def _schedule_event(
        self, program: str, day: Day, at: str, target: str
    ) -> ScheduleEvent | None:
        """Return an event of a program's day as read in a fresh session."""
        p = await self.fetch_schedule(program, details=True)
        if isinstance(day, int):
            events = p.weekly.get(day, ())
        else:
            index = int(day[2:]) - 1
            labels = list(p.special_slots)
            events = p.special.get(labels[index], ()) if index < len(labels) else ()
        return next(
            (e for e in events if e.time == at and e.target == target.strip()), None
        )

    async def add_schedule_event(
        self, program: str, day: Day, spec: EventSpec
    ) -> ScheduleEvent | None:
        """Add an event to a weekday (0 = Sunday) or special day ("Ex1".."Ex5").

        Args:
        ----
            program: The program's name.
            day: The weekday or special day.
            spec: The event: time, target and settings.

        Returns:
        -------
            The event as read back in a fresh session, or None in a dry run.

        """
        await self._schedule_write(lambda s: add_event(s, program, day, spec))
        if self.dry_run:
            return None
        event = await self._schedule_event(program, day, spec.time, spec.target)
        if event is None:
            msg = f"{program}: the iTM did not add the event {spec.time} {spec.target}"
            raise ItmCommandError(msg)
        return event

    async def edit_schedule_event(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        program: str,
        day: Day,
        at: str,
        target: str,
        spec: EventSpec,
        *,
        keep_unset: bool = False,
    ) -> ScheduleEvent | None:
        """Replace an event (found by its time and target) with spec.

        Args:
        ----
            program: The program's name.
            day: The weekday or special day.
            at: The event's current time ("HH:MM").
            target: Its current target.
            spec: What it should become (time, target and settings).
            keep_unset: Keep the event's current value of every setting spec
                leaves None (read first), instead of leaving it out.

        Returns:
        -------
            The event as read back in a fresh session, or None in a dry run.

        Raises:
        ------
            ItmCommandError: No such event; with keep_unset, an event holding
                settings this library can't write back.

        """
        if keep_unset:
            spec = await self._keep_event_settings(program, day, at, target, spec)
        await self._schedule_write(
            lambda s: edit_event(s, program, day, time=at, target=target, spec=spec)
        )
        if self.dry_run:
            return None
        event = await self._schedule_event(program, day, spec.time, spec.target)
        if event is None:
            msg = f"{program}: the iTM did not take the change to {at} {target}"
            raise ItmCommandError(msg)
        return event

    async def _keep_event_settings(  # pylint: disable=too-many-arguments
        self, program: str, day: Day, at: str, target: str, spec: EventSpec
    ) -> EventSpec:
        """Fill the settings spec leaves None from the event as it is now."""
        current = await self._schedule_event(program, day, at, target)
        if current is None:
            msg = f"{program}: no event at {at} for {target}"
            raise ItmCommandError(msg)
        if (
            current.setpoint_shift
            or current.ventilation_mode
            or current.air_volume
            or current.extra
        ):
            msg = (
                f"{program}: the event at {at} for {target} has settings this "
                "library can't write back; give the whole event instead"
            )
            raise ItmCommandError(msg)
        kept = {
            name: getattr(current, name)
            for name in KEPT_EVENT_SETTINGS
            if getattr(spec, name) is None
        }
        return dataclasses.replace(spec, **kept)

    async def delete_schedule_event(
        self, program: str, day: Day, at: str, target: str
    ) -> None:
        """Delete an event (found by its time and target) from a program's day.

        Args:
        ----
            program: The program's name.
            day: The weekday or special day.
            at: The event's time ("HH:MM").
            target: Its target.

        """
        await self._schedule_write(lambda s: delete_event(s, program, day, at, target))
        if not self.dry_run and await self._schedule_event(program, day, at, target):
            msg = f"{program}: the iTM did not delete {at} {target}"
            raise ItmCommandError(msg)

    async def copy_schedule(
        self, program: str, name: str, *, keep_enabled: bool = False
    ) -> ScheduleSummary | None:
        """Copy a program under a new name (disabled unless keep_enabled).

        Args:
        ----
            program: The program to copy.
            name: The copy's name.
            keep_enabled: Keep the source's activation (default: disabled).

        Returns:
        -------
            The copy as the program list shows it, or None in a dry run.

        """
        await self._schedule_write(
            lambda s: copy_program(s, program, name, keep_enabled=keep_enabled)
        )
        if self.dry_run:
            return None
        copy = next((p for p in await self.fetch_schedules() if p.name == name), None)
        if copy is None:
            msg = f"{program}: the iTM did not make the copy {name!r}"
            raise ItmCommandError(msg)
        return copy

    async def copy_schedule_day(
        self, program: str, day: Day, targets: Iterable[Day]
    ) -> ScheduleProgram | None:
        """Copy a day's events to other days of the same kind (weekdays or Ex).

        Args:
        ----
            program: The program.
            day: The day to copy.
            targets: The days to copy it to.

        Returns:
        -------
            The program as read back (with events), or None in a dry run.

        """
        days = tuple(targets)
        await self._schedule_write(lambda s: copy_day(s, program, day, days))
        return None if self.dry_run else await self.fetch_schedule(program)

    async def clear_schedule_day(
        self, program: str, day: Day
    ) -> ScheduleProgram | None:
        """Delete every event of a day.

        Args:
        ----
            program: The program.
            day: The weekday (0 = Sunday) or special day ("Ex1".."Ex5").

        Returns:
        -------
            The program as read back (with events), or None in a dry run.

        """
        await self._schedule_write(lambda s: clear_day(s, program, day))
        if self.dry_run:
            return None
        result = await self.fetch_schedule(program)
        if isinstance(day, int) and result.weekly.get(day):
            msg = f"{program}: day {day} still has events"
            raise ItmCommandError(msg)
        return result

    async def copy_schedule_event(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        program: str,
        day: Day,
        at: str,
        target: str,
        new_time: str,
        *,
        new_target: str | None = None,
        zone: bool = False,
    ) -> ScheduleEvent | None:
        """Copy an event to another time (and target), keeping its settings.

        Args:
        ----
            program: The program.
            day: The weekday or special day.
            at: The event's time.
            target: The event's target.
            new_time: The copy's time.
            new_target: The copy's target (default: the same).
            zone: The new target is a zone.

        Returns:
        -------
            The copy as read back, or None in a dry run.

        """
        await self._schedule_write(
            lambda s: copy_event(
                s,
                program,
                day,
                time=at,
                target=target,
                new_time=new_time,
                new_target=new_target,
                zone=zone,
            )
        )
        if self.dry_run:
            return None
        copy = await self._schedule_event(program, day, new_time, new_target or target)
        if copy is None:
            msg = f"{program}: the iTM did not copy the event to {new_time}"
            raise ItmCommandError(msg)
        return copy

    async def copy_schedule_calendar(
        self, program: str, targets: Iterable[str]
    ) -> None:
        """Copy a program's calendar to other programs (overwriting theirs).

        Args:
        ----
            program: The program to copy from.
            targets: The programs to copy to, named explicitly.

        """
        names = tuple(targets)
        await self._schedule_write(lambda s: copy_calendar(s, program, names))

    async def set_schedule_period(
        self, program: str, period: Period | None
    ) -> ScheduleProgram | None:
        """Give a program a period ((month, day), (month, day)), or none (all year).

        Args:
        ----
            program: The program's name.
            period: Begin and end, or None for all year.

        Returns:
        -------
            The program as read back (without events), or None in a dry run.

        """
        await self._schedule_write(lambda s: set_period(s, program, period))
        if self.dry_run:
            return None
        result = await self.fetch_schedule(program, events=False)
        if (result.period is None) != (period is None):
            msg = (
                f"{program}: the iTM did not take the period {period}: {result.period}"
            )
            raise ItmCommandError(msg)
        return result

    async def rename_schedule_special_day(
        self, program: str, day: str, name: str
    ) -> ScheduleProgram | None:
        """Rename a special day pattern ("Ex1".."Ex5") of a program.

        Args:
        ----
            program: The program's name.
            day: "Ex1".."Ex5".
            name: The new name (1-15 characters).

        Returns:
        -------
            The program as read back (without events), or None in a dry run.

        """
        await self._schedule_write(lambda s: rename_special_day(s, program, day, name))
        if self.dry_run:
            return None
        result = await self.fetch_schedule(program, events=False)
        index = int(day[2:]) - 1
        if index >= len(result.special_names) or result.special_names[index] != name:
            msg = f"{program}: the iTM did not rename {day}: {result.special_names}"
            raise ItmCommandError(msg)
        return result

    async def add_schedule_calendar_rule(
        self, program: str, rule: CalendarRuleSpec
    ) -> ScheduleProgram | None:
        """Add a calendar rule: on which dates a special day pattern applies.

        Args:
        ----
            program: The program's name.
            rule: The rule.

        Returns:
        -------
            The program as read back (without events), or None in a dry run.

        """
        before = len((await self.fetch_schedule(program, events=False)).calendar)
        await self._schedule_write(lambda s: add_calendar_rule(s, program, rule))
        if self.dry_run:
            return None
        result = await self.fetch_schedule(program, events=False)
        if len(result.calendar) != before + 1:
            msg = f"{program}: the iTM did not add the rule {rule}"
            raise ItmCommandError(msg)
        return result

    async def delete_schedule_calendar_rule(
        self, program: str, rule: CalendarRule
    ) -> ScheduleProgram | None:
        """Delete a calendar rule, as fetch_schedule read it.

        Args:
        ----
            program: The program's name.
            rule: The rule (month, day and pattern as shown).

        Returns:
        -------
            The program as read back (without events), or None in a dry run.

        """
        await self._schedule_write(lambda s: delete_calendar_rule(s, program, rule))
        if self.dry_run:
            return None
        result = await self.fetch_schedule(program, events=False)
        if rule in result.calendar:
            msg = f"{program}: the iTM did not delete the rule {rule}"
            raise ItmCommandError(msg)
        return result
