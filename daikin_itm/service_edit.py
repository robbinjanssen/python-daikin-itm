# ruff: noqa: SLF001
# pylint: disable=protected-access
"""Leakage check, layout setup, R32 output ports and the password setting.

Session-level flows on the installer's service screens (behind the service login,
see ``service.py``) and on the system tab's password setting:

- Leakage check (3100, service button 85): the outdoor unit list, per outdoor unit
  its registered indoor units (3101), its schedule of four programs (3102), the
  schedule copy to other outdoor units (3103), manual start and stop, and the
  results as the iTM's CSV file (through the export download flow of
  ``exports.py``). A leakage check runs for about 180 minutes: it forces every
  indoor unit of that refrigerant system to cool and puts the outdoor unit and
  those indoor units under Maintenance, so they can't be operated meanwhile.
- Layout setup (1510, service button 81): a screen without parts of its own (like
  the backup); pressing its button starts a job on the iTM, so it is a write.
- R32 output ports (1517, from the R32 settings 1516): the port picked for each of
  the two R32 alarm outputs.
- Password setting (1900, system tab button 33): whether the administrator and the
  screen password are enabled; enabling, disabling and changing them.

Every saving request goes through ``s.write`` / ``s.press_ok`` (so a dry run plans
it and ``read_only`` refuses it), every read leaves its screens with Close or
Cancel, and a part the iTM reports disabled (``ENABLED 0``) is never clicked; a
hidden part is (the iTM hides, it doesn't block). Passwords are sent once, only
into the iTM's PASSWORD input, and never logged or planned (a plan shows ``***``).

Seen on a real iTM (read only): 3100's list columns are address, name, type,
indoor units, charge and schedule, and its buttons are disabled until an outdoor
unit is selected; 1516 shows the ports as text with Change (opens 1517) and
Delete per port, and closes with part 11; 1517 lists ports as type, name, address
and its OK is disabled until a port is selected; 1510 has no parts.

Assumed (not seen on a real iTM): a list row is selected like in other screens (a
CLICK with the row as INT; the multi-select lists of 3101 and 3103 with an
INTARRAY); in 3101/3103 a unit is shown by its name in the second column (the
first when there is only one); OK on 3101, 3102, 3103 and 1517 asks a CONFIRM that
is answered Yes and saves; start, stop, Delete and the password radios apply at once
after a CONFIRM; enabling a password without one set opens the PASSWORD input; the
CSV file has four header lines (controller, date, version, title), a row with the
outdoor unit names (one per column pair), a row with the column labels and then per
row a check date and leakage amount per outdoor unit.
"""

from __future__ import annotations

import csv
import html
import io
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypeVar

from . import exports
from .exceptions import ItmCommandError, ReadOnlyError
from .menu import CLOSE_PART, MenuScreen
from .protocol import DataType, EventType
from .service import MB_R32

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from datetime import date

    from .client import _Session
    from .exports import Download
    from .protocol import Reply
    from .state import UIState

_T = TypeVar("_T")

MENULIST = MenuScreen.MENULIST
# Menu list buttons: layout setup and leakage check (service tab), password setting
# (system tab).
MB_LAYOUT, MB_LEAKAGE_CHECK, MB_PASSWORD = 81, 85, 33
# Leakage check outdoor units (3100, ScLeakageChkOutdoorSetup).
LEAKAGE = 3100
LK_LIST, LK_CLOSE, LK_REGISTER, LK_START, LK_STOP = 2, 3, 4, 6, 7
LK_SCHEDULE, LK_COPY, LK_CSV = 8, 9, 10
# Its indoor unit registration (3101, ScLeakageChkIndoorRegist).
REGISTRATION = 3101
RG_REGISTERED, RG_UNREGISTERED, RG_ADD, RG_REMOVE, RG_OK, RG_CANCEL = 2, 4, 6, 7, 9, 10
# Its schedule (3102, ScLeakageChkScheduleSetup): the name, then per program the
# activation combo, date text, date button, time text and time button.
SCHEDULE = 3102
SC_NAME, SC_OK, SC_CANCEL = 2, 27, 28
SC_PROGRAMS = tuple(tuple(4 + 6 * i + k for k in range(5)) for i in range(4))
REGISTER_SPEC = (LK_REGISTER, REGISTRATION, RG_CANCEL)
SCHEDULE_SPEC = (LK_SCHEDULE, SCHEDULE, SC_CANCEL)
# Its schedule copy (3103, ScLeakageChkCopy).
COPY = 3103
CP_TO, CP_ADD, CP_REMOVE, CP_AVAILABLE, CP_OK, CP_CANCEL = 4, 6, 7, 10, 11, 12
# Layout setup (1510, ScSrvLayoutSetup): no parts.
LAYOUT = 1510
# R32 settings (1516) and the output port setup (1517, ScSrvR32OutputPortSetup).
R32 = MenuScreen.R32
R32_TEXTS, R32_CHANGE, R32_DELETE = (2, 6), (3, 7), (4, 8)
PORTS = 1517
PT_LIST, PT_OK, PT_CANCEL = 2, 3, 4
# Password setting (1900, ScSysPasswordSetting): per password the enable and
# disable radios and the change button; Close.
PASSWORDS = 1900
PW_PARTS = {"administrator": (2, 3, 4), "screen": (6, 7, 8)}
PW_CLOSE = 9

_TIME = re.compile(r"^(\d{1,2}):(\d{2})$")


@dataclass(frozen=True)
class LeakageOutdoorUnit:
    """An outdoor unit on the leakage check screen (3100), as listed."""

    address: str
    name: str
    type: str
    indoor_units: str  # the number of registered indoor units, as shown
    charge: str  # the refrigerant charging status, as shown
    schedule: str
    columns: tuple[str, ...]  # every cell of the row


@dataclass(frozen=True)
class LeakageRegistration:
    """The indoor units registered with an outdoor unit (3101), and the others."""

    registered: tuple[str, ...]
    unregistered: tuple[str, ...]


@dataclass(frozen=True)
class LeakageCheckProgram:
    """One of the four programs of an outdoor unit's leakage check schedule."""

    number: int  # 1-4
    activation: str | None  # the combo's shown option
    activation_options: tuple[str, ...]
    date: str  # as shown (locale format)
    time: str  # as shown


@dataclass(frozen=True)
class LeakageCheckSchedule:
    """An outdoor unit's leakage check schedule (3102)."""

    name: str
    programs: tuple[LeakageCheckProgram, ...]


@dataclass(frozen=True)
class LeakageCheck:
    """The leakage check screens: outdoor units, and with details their settings."""

    outdoor_units: tuple[LeakageOutdoorUnit, ...]
    registrations: dict[str, LeakageRegistration] = field(default_factory=dict)
    schedules: dict[str, LeakageCheckSchedule] = field(default_factory=dict)


@dataclass(frozen=True)
class LeakageCheckResult:
    """One check of an outdoor unit, from the results file."""

    outdoor: str
    checked: str  # the check date and time, as written
    amount: str  # the calculated leakage amount (only a guide), as written


@dataclass(frozen=True)
class LeakageCheckResults:
    """The leakage check results file (LeakageCheck.csv)."""

    controller: str
    created: str
    version: str
    results: tuple[LeakageCheckResult, ...]

    def last(self, outdoor: str) -> LeakageCheckResult | None:
        """Return an outdoor unit's last result in the file, if any."""
        found = [r for r in self.results if r.outdoor == outdoor.strip()]
        return found[-1] if found else None


@dataclass(frozen=True)
class LayoutSetup:
    """Whether the menu offers the layout setup (only with layout data)."""

    offered: bool | None  # None: the iTM didn't say
    enabled: bool | None


@dataclass(frozen=True)
class R32OutputPorts:
    """The R32 alarm output ports (1516) and the ports the iTM offers (1517)."""

    ports: tuple[str, ...]  # port 1 and 2 as shown ("" when unset)
    offered: tuple[str, ...]  # 1517's rows, their cells joined by spaces


@dataclass(frozen=True)
class PasswordSettings:
    """Whether the administrator and the screen password are enabled (1900)."""

    administrator: bool | None
    screen: bool | None


# -- helpers --------------------------------------------------------------------------


def _rows(state: UIState, screen: int, part: int) -> list[list[str]]:
    rows = (state.get(screen, part, "LIST_ROW") or {}).get("rows", [])
    return [[html.unescape(str(t)).strip() for t in r.get("text", [])] for r in rows]


def _text(state: UIState, screen: int, part: int) -> str:
    value = state.get(screen, part, "TEXT")
    return html.unescape(value).strip() if isinstance(value, str) else ""


def _name(row: list[str]) -> str:
    """Return a unit's name in a 3101/3103 row (second column, assumed)."""
    if len(row) > 1:
        return row[1]
    return row[0] if row else ""


def _matches(rows: list[list[str]], value: str) -> list[int]:
    """Return the rows showing value in any cell (a name or an address)."""
    return [i for i, row in enumerate(rows) if value.strip() in row]


def _one(rows: list[list[str]], value: str, what: str) -> int:
    found = _matches(rows, value)
    if len(found) != 1:
        listed = [_name(r) for r in rows]
        msg = f"{what} {value!r} not found once (found {len(found)}); listed: {listed}"
        raise ItmCommandError(msg)
    return found[0]


def _radio(state: UIState, screen: int, on: int, off: int) -> bool | None:
    if state.get(screen, on, "CHECK_STATE") == 1:
        return True
    if state.get(screen, off, "CHECK_STATE") == 1:
        return False
    return None


def _refuse_dialogs(reply: Reply, what: str) -> None:
    for dialog in reply.dialogs():
        if dialog[1] in {"ERROR", "INFO"}:
            msg = f"{what}: the iTM refused: {dialog[2]!r}"
            raise ItmCommandError(msg)


async def _click(s: _Session, screen: int, part: int, what: str) -> Reply:
    """Click a part, never one the iTM reports disabled (that hangs it)."""
    return await s.click(screen, part, what)


async def _write_enabled(
    s: _Session, label: str, screen: int, part: int
) -> Reply | None:
    """``s.write`` a button, never one the iTM reports disabled."""
    if s.state.get(screen, part, "ENABLED") == 0:
        msg = f"{label}: the iTM has this button disabled"
        raise ItmCommandError(msg)
    return await s.write(label, screen, part)


async def _leave(s: _Session, screen: int, cancel: int) -> None:
    """Leave a screen with its Close/Cancel, if it is still open."""
    if screen in s.state.screens:
        await s.send(screen, cancel)
        s.state.screens.pop(screen, None)


async def _open_menu_button(s: _Session, button: int, screen: int, what: str) -> None:
    """Press a menu list button (hidden ones too) and check its screen opened."""
    await s.open_menu()
    reply = await _click(s, MENULIST, button, what)
    _refuse_dialogs(reply, what)
    if screen not in s.state.screens:
        msg = f"{what}: the screen did not open: {reply.dialogs()}"
        raise ItmCommandError(msg)


async def _open_sub(s: _Session, screen: int, button: int, sub: int, what: str) -> None:
    reply = await _click(s, screen, button, what)
    _refuse_dialogs(reply, what)
    if sub not in s.state.screens:
        msg = f"{what}: the screen did not open: {reply.dialogs()}"
        raise ItmCommandError(msg)


async def _select(s: _Session, screen: int, part: int, row: int) -> None:
    await s.select_row(screen, part, row)


async def _select_rows(s: _Session, screen: int, part: int, rows: list[int]) -> None:
    await s.send(
        screen, part, EventType.CLICK, DataType.INTARRAY, len(rows), array=tuple(rows)
    )


# -- leakage check: the outdoor units (3100) -----------------------------------------


def outdoor_units_from_screen(state: UIState) -> tuple[LeakageOutdoorUnit, ...]:
    """Read the leakage check's outdoor unit list (3100)."""
    units = []
    for row in _rows(state, LEAKAGE, LK_LIST):
        cells = [*row, "", "", "", "", "", ""]
        units.append(
            LeakageOutdoorUnit(
                address=cells[0],
                name=cells[1],
                type=cells[2],
                indoor_units=cells[3],
                charge=cells[4],
                schedule=cells[5],
                columns=tuple(row),
            )
        )
    return tuple(units)


async def _open_leakage(s: _Session, password: str) -> None:
    await s.service_login(password)
    await _open_menu_button(s, MB_LEAKAGE_CHECK, LEAKAGE, "leakage check")


async def _select_outdoor(s: _Session, outdoor: str) -> None:
    """Select an outdoor unit (by name or address) on the open 3100."""
    rows = _rows(s.state, LEAKAGE, LK_LIST)
    await _select(s, LEAKAGE, LK_LIST, _one(rows, outdoor, "outdoor unit"))


async def read_outdoor_units(
    s: _Session, password: str
) -> tuple[LeakageOutdoorUnit, ...]:
    """Log in to service mode and read the outdoor unit list; Close.

    Returns
    -------
        The outdoor units, in list order.

    """
    try:
        await _open_leakage(s, password)
        return outdoor_units_from_screen(s.state)
    finally:
        await _leave(s, LEAKAGE, LK_CLOSE)


def registration_from_screen(state: UIState) -> LeakageRegistration:
    """Read the open indoor unit registration (3101)."""
    return LeakageRegistration(
        registered=tuple(_name(r) for r in _rows(state, REGISTRATION, RG_REGISTERED)),
        unregistered=tuple(
            _name(r) for r in _rows(state, REGISTRATION, RG_UNREGISTERED)
        ),
    )


def schedule_from_screen(state: UIState) -> LeakageCheckSchedule:
    """Read the open schedule screen (3102)."""
    programs = []
    for number, (combo, date_text, _, time_text, _) in enumerate(SC_PROGRAMS, 1):
        options = tuple(
            html.unescape(str(o)).strip()
            for o in state.get(SCHEDULE, combo, "COMBO_STRS") or ()
        )
        idx = state.get(SCHEDULE, combo, "SINGLE_SELIDX")
        shown = options[idx] if idx is not None and 0 <= idx < len(options) else None
        programs.append(
            LeakageCheckProgram(
                number=number,
                activation=shown,
                activation_options=options,
                date=_text(state, SCHEDULE, date_text),
                time=_text(state, SCHEDULE, time_text),
            )
        )
    return LeakageCheckSchedule(
        name=_text(state, SCHEDULE, SC_NAME), programs=tuple(programs)
    )


async def _read_sub(
    s: _Session,
    spec: tuple[int, int, int],
    read: Callable[[UIState], _T],
    what: str,
) -> _T:
    """Open a 3100 sub-screen for the selected unit, read it and cancel."""
    button, sub, cancel = spec
    try:
        await _open_sub(s, LEAKAGE, button, sub, what)
        return read(s.state)
    finally:
        await _leave(s, sub, cancel)


async def read_leakage_check(
    s: _Session, password: str, *, details: bool = False
) -> LeakageCheck:
    """Read the outdoor units and, with details, each one's registration and schedule.

    Selecting an outdoor unit is harmless; each sub-screen is left with Cancel.

    Returns
    -------
        What the screens show.

    """
    try:
        await _open_leakage(s, password)
        units = outdoor_units_from_screen(s.state)
        registrations: dict[str, LeakageRegistration] = {}
        schedules: dict[str, LeakageCheckSchedule] = {}
        if details:
            for i, unit in enumerate(units):
                await _select(s, LEAKAGE, LK_LIST, i)
                key = unit.name or unit.address
                registrations[key] = await _read_sub(
                    s,
                    REGISTER_SPEC,
                    registration_from_screen,
                    f"{key}: indoor unit registration",
                )
                schedules[key] = await _read_sub(
                    s,
                    SCHEDULE_SPEC,
                    schedule_from_screen,
                    f"{key}: leakage check schedule",
                )
        return LeakageCheck(units, registrations, schedules)
    finally:
        await _leave(s, REGISTRATION, RG_CANCEL)
        await _leave(s, SCHEDULE, SC_CANCEL)
        await _leave(s, LEAKAGE, LK_CLOSE)


async def read_registration(
    s: _Session, password: str, outdoor: str
) -> LeakageRegistration:
    """Read one outdoor unit's indoor unit registration; Cancel.

    Returns
    -------
        The registered and the unregistered indoor units.

    """
    try:
        await _open_leakage(s, password)
        await _select_outdoor(s, outdoor)
        return await _read_sub(
            s,
            REGISTER_SPEC,
            registration_from_screen,
            f"{outdoor}: indoor unit registration",
        )
    finally:
        await _leave(s, LEAKAGE, LK_CLOSE)


async def read_schedule(
    s: _Session, password: str, outdoor: str
) -> LeakageCheckSchedule:
    """Read one outdoor unit's leakage check schedule; Cancel.

    Returns
    -------
        Its four programs.

    """
    try:
        await _open_leakage(s, password)
        await _select_outdoor(s, outdoor)
        return await _read_sub(
            s,
            SCHEDULE_SPEC,
            schedule_from_screen,
            f"{outdoor}: leakage check schedule",
        )
    finally:
        await _leave(s, LEAKAGE, LK_CLOSE)


async def _pick_units(  # pylint: disable=too-many-locals
    s: _Session,
    screen: int,
    lists: tuple[int, int, int, int],
    wanted: tuple[str, ...],
    what: str,
) -> None:
    """Make a multi-select picker's chosen list exactly ``wanted``.

    ``lists`` is (chosen list, available list, add button, remove button).
    """
    chosen_part, available_part, add, remove = lists
    chosen, available = (
        _rows(s.state, screen, chosen_part),
        _rows(s.state, screen, available_part),
    )
    names = {
        _name((chosen + available)[_one(chosen + available, w, what)]) for w in wanted
    }
    adding = [i for i, row in enumerate(available) if _name(row) in names]
    if adding:
        await _select_rows(s, screen, available_part, adding)
        await _click(s, screen, add, f"{what}: add")
    chosen = _rows(s.state, screen, chosen_part)
    removing = [i for i, row in enumerate(chosen) if _name(row) not in names]
    if removing:
        await _select_rows(s, screen, chosen_part, removing)
        await _click(s, screen, remove, f"{what}: remove")
    got = {_name(r) for r in _rows(s.state, screen, chosen_part)}
    if got != names:
        msg = f"{what}: the list would be {sorted(got)}, not {sorted(names)}"
        raise ItmCommandError(msg)


async def set_registration(
    s: _Session, password: str, outdoor: str, indoor_units: tuple[str, ...]
) -> bool:
    """Register exactly these indoor units with an outdoor unit; OK.

    Returns
    -------
        True when saved, False in a dry run.

    """
    s._writable()
    try:
        await _open_leakage(s, password)
        await _select_outdoor(s, outdoor)
        await _open_sub(
            s,
            LEAKAGE,
            LK_REGISTER,
            REGISTRATION,
            f"{outdoor}: indoor unit registration",
        )
        await _pick_units(
            s,
            REGISTRATION,
            (RG_REGISTERED, RG_UNREGISTERED, RG_ADD, RG_REMOVE),
            indoor_units,
            "indoor unit",
        )
        return await s.press_ok(
            f"register indoor units with {outdoor}", REGISTRATION, RG_OK, RG_CANCEL
        )
    finally:
        await _leave(s, REGISTRATION, RG_CANCEL)
        await _leave(s, LEAKAGE, LK_CLOSE)


def parse_time(text: str) -> tuple[int, int]:
    """Parse "HH:MM".

    Returns
    -------
        (hour, minute).

    Raises
    ------
        ItmCommandError: Not a time of day.

    """
    m = _TIME.match(text.strip())
    if m is None or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        msg = f"a time is HH:MM, not {text!r}"
        raise ItmCommandError(msg)
    return int(m.group(1)), int(m.group(2))


async def answer_hourmin(
    s: _Session, screen: int, button: int, at: str, what: str
) -> None:
    """Press a time's "change" button and enter it in the HOURMIN dialog.

    The dialog says its range; a time outside it is refused before it is sent.
    """
    hour, minute = parse_time(at)
    reply = await _click(s, screen, button, what)
    dialogs = [
        u for u in reply.screens if u.update == "DLG_OPEN" and u.data[1] == "HOURMIN"
    ]
    if not dialogs:
        msg = f"{what}: no time input opened: {reply.dialogs()}"
        raise ItmCommandError(msg)
    d = dialogs[0]
    low, high = (d.data[6], d.data[7]), (d.data[8], d.data[9])
    if not low <= (hour, minute) <= high:
        await s.send(d.screen, d.data[0], EventType.CLICK2)
        msg = f"{what}: {at} outside the iTM's range {low}-{high}"
        raise ItmCommandError(msg)
    reply = await s.send(
        d.screen, d.data[0], EventType.CLICK, DataType.HOURMIN, 2, array=(hour, minute)
    )
    _refuse_dialogs(reply, what)


async def set_schedule_program(  # noqa: PLR0913  # pylint: disable=too-many-arguments
    s: _Session,
    password: str,
    outdoor: str,
    program: int,
    *,
    activation: str | None = None,
    day: date | None = None,
    at: str | None = None,
) -> bool:
    """Change one program (1-4) of an outdoor unit's schedule; OK.

    Args:
    ----
        s: The open session.
        password: The service password.
        outdoor: The outdoor unit (name or address).
        program: The program, 1-4.
        activation: The activation, an option as the combo shows it.
        day: The start date (the iTM allows 2010-01-01 to 2036-12-31).
        at: The start time, "HH:MM".

    Returns:
    -------
        True when saved, False in a dry run.

    """
    s._writable()
    if not 1 <= program <= len(SC_PROGRAMS):
        msg = f"a leakage check program is 1-{len(SC_PROGRAMS)}, not {program}"
        raise ItmCommandError(msg)
    if activation is None and day is None and at is None:
        msg = "nothing to change"
        raise ItmCommandError(msg)
    if at is not None:
        parse_time(at)
    combo, _, date_button, _, time_button = SC_PROGRAMS[program - 1]
    try:
        await _open_leakage(s, password)
        await _select_outdoor(s, outdoor)
        await _open_sub(
            s, LEAKAGE, LK_SCHEDULE, SCHEDULE, f"{outdoor}: leakage check schedule"
        )
        what = f"{outdoor} program {program}"
        if activation is not None:
            options = [
                html.unescape(str(o)).strip().casefold()
                for o in s.state.get(SCHEDULE, combo, "COMBO_STRS") or ()
            ]
            wanted = activation.strip().casefold()
            index = options.index(wanted) if wanted in options else None
            await s._pick_option(SCHEDULE, combo, index, f"{what} {activation!r}")
        if day is not None:
            await exports.answer_date(s, SCHEDULE, date_button, day, f"{what} date")
        if at is not None:
            await answer_hourmin(s, SCHEDULE, time_button, at, f"{what} time")
        return await s.press_ok(
            f"save the leakage check schedule of {outdoor}", SCHEDULE, SC_OK, SC_CANCEL
        )
    finally:
        await _leave(s, SCHEDULE, SC_CANCEL)
        await _leave(s, LEAKAGE, LK_CLOSE)


async def copy_schedule(
    s: _Session, password: str, outdoor: str, targets: tuple[str, ...]
) -> bool:
    """Copy an outdoor unit's schedule to other outdoor units (3103); OK.

    Returns
    -------
        True when saved, False in a dry run.

    """
    s._writable()
    if not targets:
        msg = "name at least one outdoor unit to copy to"
        raise ItmCommandError(msg)
    try:
        await _open_leakage(s, password)
        await _select_outdoor(s, outdoor)
        await _open_sub(s, LEAKAGE, LK_COPY, COPY, f"{outdoor}: schedule copy")
        await _pick_units(
            s, COPY, (CP_TO, CP_AVAILABLE, CP_ADD, CP_REMOVE), targets, "outdoor unit"
        )
        return await s.press_ok(
            f"copy the leakage check schedule of {outdoor}", COPY, CP_OK, CP_CANCEL
        )
    finally:
        await _leave(s, COPY, CP_CANCEL)
        await _leave(s, LEAKAGE, LK_CLOSE)


async def run_check(s: _Session, password: str, outdoor: str, *, start: bool) -> bool:
    """Start or stop an outdoor unit's leakage check (button, then Yes).

    Starting forces every unit of that refrigerant system to cool for about 180
    minutes under Maintenance; the client guards it with ``allow_leakage_check``.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    verb = "start" if start else "stop"
    try:
        await _open_leakage(s, password)
        await _select_outdoor(s, outdoor)
        reply = await _write_enabled(
            s,
            f"{verb} the leakage check of {outdoor}",
            LEAKAGE,
            LK_START if start else LK_STOP,
        )
        if reply is None:
            return False
        await s._answer_confirm(LEAKAGE, reply)
        return True
    finally:
        await _leave(s, LEAKAGE, LK_CLOSE)


async def export_results(s: _Session, password: str, fetch: exports.Fetch) -> Download:
    """Export the leakage check results (every outdoor unit) and fetch the file.

    CSV-Uitv. is disabled until an outdoor unit is selected: the first one is
    selected first (harmless; the file holds every unit's results).

    Returns
    -------
        The file's name and contents.

    """
    try:
        await _open_leakage(s, password)
        if s.state.get(LEAKAGE, LK_CSV, "ENABLED") == 0 and _rows(
            s.state, LEAKAGE, LK_LIST
        ):
            await _select(s, LEAKAGE, LK_LIST, 0)
        if s.state.get(LEAKAGE, LK_CSV, "ENABLED") == 0:
            msg = "leakage check results: the iTM has no outdoor unit to export"
            raise ItmCommandError(msg)
        return await exports.download(s, LEAKAGE, LK_CSV, fetch)
    finally:
        await _leave(s, LEAKAGE, LK_CLOSE)


def parse_results(data: bytes) -> LeakageCheckResults:
    """Parse the leakage check results file (see the module's assumptions).

    Returns
    -------
        The header and each outdoor unit's results, per unit in file order.

    """
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        text = data.decode("latin-1")
    rows = [[c.strip() for c in r] for r in csv.reader(io.StringIO(text))]
    header = [r[0] if r else "" for r in rows[:4]] + ["", "", "", ""]
    body = [r for r in rows[4:] if any(r)]
    if not body:
        return LeakageCheckResults(header[0], header[1], header[2], ())
    names_row, data_rows = [*body[0], ""], body[1:]
    if data_rows and not any(ch.isdigit() for ch in "".join(data_rows[0])):
        data_rows = data_rows[1:]  # the column labels
    results = []
    for col in range(0, len(names_row) - 1, 2):
        name = names_row[col] or names_row[col + 1]
        if not name:
            continue
        for row in data_rows:
            cells = [*row, "", ""]
            if len(cells) > col + 1 and (cells[col] or cells[col + 1]):
                results.append(LeakageCheckResult(name, cells[col], cells[col + 1]))
    return LeakageCheckResults(header[0], header[1], header[2], tuple(results))


# -- layout setup (1510) --------------------------------------------------------------


async def read_layout_setup(s: _Session, password: str) -> LayoutSetup:
    """Tell whether the service tab offers the layout setup; nothing is pressed.

    Returns
    -------
        The button's visibility and enabled state, as the iTM reports them.

    """
    await s.service_login(password)
    visible = s.state.get(MENULIST, MB_LAYOUT, "VISIBLED")
    enabled = s.state.get(MENULIST, MB_LAYOUT, "ENABLED")
    return LayoutSetup(
        offered=None if visible is None else visible != 0,
        enabled=None if enabled is None else enabled != 0,
    )


async def layout_setup_job(s: _Session, password: str) -> bool:
    """Press the layout setup button: a job on the iTM, like the backup.

    A CONFIRM is answered Yes and a PROGRESS dialog waited out. 1510 has no parts,
    so it has no Close either: a screen it leaves open ends with the session.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    await s.service_login(password)
    reply = await _write_enabled(s, "run the layout setup", MENULIST, MB_LAYOUT)
    if reply is None:
        return False
    for u in reply.screens:
        if u.update == "DLG_OPEN" and u.data[1] == "CONFIRM":
            reply = await s.send(u.screen, u.data[0])
            break
    _refuse_dialogs(reply, "layout setup")
    await s.wait_for_progress(reply)
    return True


# -- R32 output ports (1516, 1517) ----------------------------------------------------


def _port_rows(state: UIState) -> list[list[str]]:
    return _rows(state, PORTS, PT_LIST)


def _port_text(row: list[str]) -> str:
    return " ".join(c for c in row if c)


async def _open_r32(s: _Session, password: str) -> None:
    await s.service_login(password)
    await _open_menu_button(s, MB_R32, R32, "R32 settings")


async def read_r32_ports(s: _Session, password: str) -> R32OutputPorts:
    """Read the R32 output ports and the ports 1517 offers; Cancel, Close.

    Returns
    -------
        Ports 1 and 2 as shown and the offered ports.

    """
    try:
        await _open_r32(s, password)
        ports = tuple(_text(s.state, R32, p) for p in R32_TEXTS)
        offered: tuple[str, ...] = ()
        if s.state.get(R32, R32_CHANGE[0], "ENABLED") != 0:
            await _open_sub(s, R32, R32_CHANGE[0], PORTS, "R32 output port setup")
            offered = tuple(_port_text(r) for r in _port_rows(s.state))
        return R32OutputPorts(ports=ports, offered=offered)
    finally:
        await _leave(s, PORTS, PT_CANCEL)
        await _leave(s, R32, CLOSE_PART[R32])


async def set_r32_port(
    s: _Session, password: str, port: int, value: str | None
) -> bool:
    """Pick an R32 alarm output port (1517, OK), or clear it (Delete, Yes).

    The output test run and its release are never pressed.

    Args:
    ----
        s: The open session.
        password: The service password.
        port: 1 or 2.
        value: A port as 1517 lists it (a cell, e.g. its name or address, or the
            row's cells joined by spaces); None clears the port.

    Returns:
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    if port not in {1, 2}:
        msg = f"an R32 output port is 1 or 2, not {port}"
        raise ItmCommandError(msg)
    try:
        await _open_r32(s, password)
        if value is None:
            reply = await _write_enabled(
                s, f"clear R32 output port {port}", R32, R32_DELETE[port - 1]
            )
            if reply is None:
                return False
            await s._answer_confirm(R32, reply)
            return True
        await _open_sub(s, R32, R32_CHANGE[port - 1], PORTS, "R32 output port setup")
        rows = _port_rows(s.state)
        found = _matches(rows, value) or [
            i for i, r in enumerate(rows) if _port_text(r) == value.strip()
        ]
        if len(found) != 1:
            offered = [_port_text(r) for r in rows]
            msg = f"R32 output port {value!r} not offered once; offered: {offered}"
            raise ItmCommandError(msg)
        await _select(s, PORTS, PT_LIST, found[0])
        if s.state.get(PORTS, PT_OK, "ENABLED") == 0:
            msg = "R32 output port setup: the iTM has OK disabled"
            raise ItmCommandError(msg)
        return await s.press_ok(
            f"set R32 output port {port} to {value}", PORTS, PT_OK, PT_CANCEL
        )
    finally:
        await _leave(s, PORTS, PT_CANCEL)
        await _leave(s, R32, CLOSE_PART[R32])


def port_shown(shown: str, value: str | None) -> bool:
    """Tell whether 1516 shows the port that was set (or none, when cleared)."""
    if value is None:
        return not shown.strip()
    return bool(shown.strip()) and (
        shown.strip() == value.strip() or value.strip() in shown
    )


# -- password setting (1900) ----------------------------------------------------------


def passwords_from_screen(state: UIState) -> PasswordSettings:
    """Read the open password setting (1900)."""
    admin_on, admin_off, _ = PW_PARTS["administrator"]
    screen_on, screen_off, _ = PW_PARTS["screen"]
    return PasswordSettings(
        administrator=_radio(state, PASSWORDS, admin_on, admin_off),
        screen=_radio(state, PASSWORDS, screen_on, screen_off),
    )


async def read_passwords(s: _Session) -> PasswordSettings:
    """Open the password setting, read it and close it.

    Returns
    -------
        Whether each password is enabled.

    """
    try:
        await _open_menu_button(s, MB_PASSWORD, PASSWORDS, "password setting")
        return passwords_from_screen(s.state)
    finally:
        await _leave(s, PASSWORDS, PW_CLOSE)


def _password_dialog(reply: Reply) -> Any:
    found = [
        u for u in reply.screens if u.update == "DLG_OPEN" and u.data[1] == "PASSWORD"
    ]
    return found[0] if found else None


async def _cancel_password(s: _Session, d: Any) -> None:
    await s.send(d.screen, d.data[0], EventType.CLICK2, DataType.INT, 0)


async def _enter_password(s: _Session, reply: Reply, password: str, kind: str) -> None:
    """Answer the PASSWORD input(s) a reply opened; check the length first."""
    d = _password_dialog(reply)
    low, high = d.data[3], d.data[4]
    if not low <= len(password) <= high:
        await _cancel_password(s, d)
        msg = f"the {kind} password must be {low}-{high} characters"
        raise ItmCommandError(msg)
    await s._answer_password(reply, password)


async def _switch_password(
    s: _Session, kind: str, *, enabled: bool, password: str | None
) -> bool:
    """Click a password's enable/disable radio and answer what follows.

    Returns
    -------
        Whether the password was entered on the way (enabling may ask for it).

    """
    on, off, _ = PW_PARTS[kind]
    verb = "enable" if enabled else "disable"
    reply = await _write_enabled(
        s, f"{verb} the {kind} password", PASSWORDS, on if enabled else off
    )
    if reply is None:
        return False
    for dialog in reply.dialogs():
        if dialog[1] == "CONFIRM":
            reply = await s.send(PASSWORDS, dialog[0])
            break
    d = _password_dialog(reply)
    if d is not None:
        if password is None:
            await _cancel_password(s, d)
            msg = f"enabling the {kind} password: the iTM asks for one; pass it"
            raise ItmCommandError(msg)
        await _enter_password(s, reply, password, kind)
        return True
    _refuse_dialogs(reply, f"{verb} the {kind} password")
    return False


async def _change_password(s: _Session, kind: str, password: str) -> None:
    """Press a password's change button and enter it (dry run: plan ``***``)."""
    _, _, change = PW_PARTS[kind]
    disabled = s.state.get(PASSWORDS, change, "ENABLED") == 0
    if s._client.dry_run and disabled:
        # Not offered before the planned enable has happened: plan it only.
        s._client.planned.append(
            f"{kind} password = ***: screen {PASSWORDS} button {change}"
        )
        return
    reply = await _click(s, PASSWORDS, change, f"{kind} password")
    d = _password_dialog(reply)
    if d is None:
        _refuse_dialogs(reply, f"{kind} password")
        msg = f"{kind} password: no password input opened: {reply.dialogs()}"
        raise ItmCommandError(msg)
    if s._client.dry_run:
        low, high = d.data[3], d.data[4]
        await _cancel_password(s, d)
        if not low <= len(password) <= high:
            msg = f"the {kind} password must be {low}-{high} characters"
            raise ItmCommandError(msg)
        s._client.planned.append(
            f"{kind} password = ***: screen {d.screen} input {d.data[0]}"
        )
        return
    await _enter_password(s, reply, password, kind)


async def set_password(
    s: _Session,
    kind: str,
    *,
    enabled: bool | None = None,
    password: str | None = None,
) -> bool:
    """Enable or disable a password and/or change it, on the password setting.

    Args:
    ----
        s: The open session.
        kind: "administrator" or "screen".
        enabled: Enable (True) or disable (False); None leaves it.
        password: A new password; sent once, never logged or planned.

    Returns:
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    if kind not in PW_PARTS:
        msg = f"a password is one of {list(PW_PARTS)}, not {kind!r}"
        raise ItmCommandError(msg)
    if enabled is None and password is None:
        msg = "nothing to change"
        raise ItmCommandError(msg)
    if enabled is False and password is not None:
        msg = "a password is not changed while disabling it"
        raise ItmCommandError(msg)
    try:
        await _open_menu_button(s, MB_PASSWORD, PASSWORDS, "password setting")
        current = getattr(passwords_from_screen(s.state), kind)
        entered = False
        if enabled is not None and current is not enabled:
            entered = await _switch_password(
                s, kind, enabled=enabled, password=password
            )
        if password is not None and not entered:
            await _change_password(s, kind, password)
        return not s._client.dry_run
    finally:
        await _leave(s, PASSWORDS, PW_CLOSE)


# -- the client's methods -------------------------------------------------------------


class ServiceEditMixin:
    """ItmClient's leakage check, layout setup, R32 port and password methods."""

    if TYPE_CHECKING:
        # pylint: disable=unused-argument
        dry_run: bool
        read_only: bool
        planned: list[str]

        async def _in_session(
            self, call: Callable[[_Session], Awaitable[_T]]
        ) -> _T: ...

        async def _get_bytes(self, path: str) -> bytes: ...

        def _experimental(self, what: str) -> None: ...

    def _service_write_allowed(self, *, allowed: bool, flag: str, why: str) -> None:
        """Refuse a write on a read-only client, or without its explicit allowance."""
        if self.read_only:
            msg = "the client is read-only"
            raise ReadOnlyError(msg)
        if not self.dry_run and not allowed:
            msg = f"{why}; use a dry run, or pass {flag}=True"
            raise ItmCommandError(msg)

    def _service_guard(self, *, allowed: bool) -> None:
        self._service_write_allowed(
            allowed=allowed,
            flag="allow_service_change",
            why="this changes the installer's service settings",
        )

    # -- leakage check ------------------------------------------------------------
    async def fetch_leakage_check(
        self, password: str, *, details: bool = False
    ) -> LeakageCheck:
        """Read the leakage check's outdoor units, after a service login. Read only.

        Args:
        ----
            password: The service password (the installer's); sent once, never
                stored or logged.
            details: Also open each outdoor unit's indoor unit registration and
                schedule (each left with Cancel).

        Returns:
        -------
            The outdoor units, and with details their registrations and schedules
            by name (the address when the name is empty).

        """
        return await self._in_session(
            lambda s: read_leakage_check(s, password, details=details)
        )

    async def fetch_leakage_check_registration(
        self, password: str, outdoor: str
    ) -> LeakageRegistration:
        """Read the indoor units registered with an outdoor unit. Read only.

        Args:
        ----
            password: The service password.
            outdoor: The outdoor unit, by name or address.

        Returns:
        -------
            The registered and the unregistered indoor units.

        """
        return await self._in_session(lambda s: read_registration(s, password, outdoor))

    async def fetch_leakage_check_schedule(
        self, password: str, outdoor: str
    ) -> LeakageCheckSchedule:
        """Read an outdoor unit's leakage check schedule. Read only.

        Args:
        ----
            password: The service password.
            outdoor: The outdoor unit, by name or address.

        Returns:
        -------
            Its four programs.

        """
        return await self._in_session(lambda s: read_schedule(s, password, outdoor))

    async def export_leakage_check_results(self, password: str) -> Download:
        """Export the leakage check results (LeakageCheck.csv) and fetch the file.

        Args:
        ----
            password: The service password.

        Returns:
        -------
            The file's name and contents.

        """
        return await self._in_session(
            lambda s: export_results(s, password, self._get_bytes)
        )

    async def fetch_leakage_check_results(self, password: str) -> LeakageCheckResults:
        """Export the leakage check results and parse them.

        Args:
        ----
            password: The service password.

        Returns:
        -------
            Every outdoor unit's results (``last(name)`` gives a unit's last one).

        """
        return parse_results((await self.export_leakage_check_results(password)).data)

    async def set_leakage_check_indoor_units(
        self,
        password: str,
        outdoor: str,
        indoor_units: tuple[str, ...],
        *,
        allow_service_change: bool = False,
    ) -> LeakageRegistration | None:
        """Register exactly these indoor units with an outdoor unit.

        They are the units a leakage check of that outdoor unit puts under
        Maintenance (its refrigerant system).

        Args:
        ----
            password: The service password.
            outdoor: The outdoor unit, by name or address.
            indoor_units: The indoor units, by name or address.
            allow_service_change: Needed unless this is a dry run.

        Returns:
        -------
            The registration as read back, or None in a dry run.

        Raises:
        ------
            ItmCommandError: Not allowed, a unit not offered, or the read-back differs.

        """
        self._experimental("change a leakage check registration")
        self._service_guard(allowed=allow_service_change)
        await self._in_session(
            lambda s: set_registration(s, password, outdoor, tuple(indoor_units))
        )
        if self.dry_run:
            return None
        got = await self.fetch_leakage_check_registration(password, outdoor)
        wanted = {u.strip() for u in indoor_units}
        missing = [u for u in wanted if u not in got.registered]
        if missing or len(got.registered) != len(wanted):
            msg = f"{outdoor}: the iTM did not register {sorted(wanted)}: {got}"
            raise ItmCommandError(msg)
        return got

    async def set_leakage_check_schedule(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        password: str,
        outdoor: str,
        program: int,
        *,
        activation: str | None = None,
        day: date | None = None,
        at: str | None = None,
        allow_service_change: bool = False,
    ) -> LeakageCheckSchedule | None:
        """Change one program of an outdoor unit's leakage check schedule.

        An active program starts a leakage check at its date and time: the whole
        refrigerant system then cools for about 180 minutes under Maintenance.

        Args:
        ----
            password: The service password.
            outdoor: The outdoor unit, by name or address.
            program: 1-4.
            activation: The activation, as the program's combo shows it.
            day: The start date.
            at: The start time, "HH:MM".
            allow_service_change: Needed unless this is a dry run.

        Returns:
        -------
            The schedule as read back, or None in a dry run.

        Raises:
        ------
            ItmCommandError: Not allowed, a value not offered, or the read-back
                differs.

        """
        self._experimental("change a leakage check schedule")
        self._service_guard(allowed=allow_service_change)
        await self._in_session(
            lambda s: set_schedule_program(
                s, password, outdoor, program, activation=activation, day=day, at=at
            )
        )
        if self.dry_run:
            return None
        got = await self.fetch_leakage_check_schedule(password, outdoor)
        shown = got.programs[program - 1]
        if activation is not None and (shown.activation or "").casefold() != (
            activation.strip().casefold()
        ):
            msg = f"{outdoor} program {program}: the iTM shows {shown}"
            raise ItmCommandError(msg)
        return got

    async def copy_leakage_check_schedule(
        self,
        password: str,
        outdoor: str,
        targets: tuple[str, ...],
        *,
        allow_service_change: bool = False,
    ) -> None:
        """Copy an outdoor unit's leakage check schedule to other outdoor units.

        Args:
        ----
            password: The service password.
            outdoor: The source outdoor unit, by name or address.
            targets: The outdoor units to overwrite.
            allow_service_change: Needed unless this is a dry run.

        Raises:
        ------
            ItmCommandError: Not allowed, a unit not offered, or a target's
                schedule differs afterwards.

        """
        self._experimental("copy a leakage check schedule")
        self._service_guard(allowed=allow_service_change)
        await self._in_session(
            lambda s: copy_schedule(s, password, outdoor, tuple(targets))
        )
        if self.dry_run:
            return
        source = await self.fetch_leakage_check_schedule(password, outdoor)
        for target in targets:
            got = await self.fetch_leakage_check_schedule(password, target)
            if got.programs != source.programs:
                msg = f"{target}: the schedule was not copied from {outdoor}"
                raise ItmCommandError(msg)

    async def _leakage_check_run(
        self, password: str, outdoor: str, *, start: bool, allowed: bool
    ) -> LeakageOutdoorUnit | None:
        self._service_write_allowed(
            allowed=allowed,
            flag="allow_leakage_check",
            why=(
                "a leakage check cools every unit of the refrigerant system for "
                "about 3 hours under Maintenance"
            ),
        )
        await self._in_session(lambda s: run_check(s, password, outdoor, start=start))
        if self.dry_run:
            return None
        units = (await self.fetch_leakage_check(password)).outdoor_units
        unit = next((u for u in units if outdoor.strip() in u.columns), None)
        if unit is None:
            msg = f"{outdoor}: the outdoor unit is no longer listed"
            raise ItmCommandError(msg)
        return unit

    async def start_leakage_check(
        self, password: str, outdoor: str, *, allow_leakage_check: bool = False
    ) -> LeakageOutdoorUnit | None:
        """Start an outdoor unit's leakage check by hand.

        It takes about 180 minutes and forces every indoor unit of that refrigerant
        system to cool; the outdoor unit and those indoor units are under
        Maintenance (no control) until it ends or is stopped. The iTM refuses it
        while a unit is in error, maintenance or emergency stop, or the refrigerant
        charging was not done.

        Args:
        ----
            password: The service password.
            outdoor: The outdoor unit, by name or address.
            allow_leakage_check: Needed unless this is a dry run.

        Returns:
        -------
            The outdoor unit as listed afterwards, or None in a dry run.

        """
        self._experimental("start a leakage check")
        return await self._leakage_check_run(
            password, outdoor, start=True, allowed=allow_leakage_check
        )

    async def stop_leakage_check(
        self, password: str, outdoor: str, *, allow_leakage_check: bool = False
    ) -> LeakageOutdoorUnit | None:
        """Stop an outdoor unit's running leakage check by hand.

        Stopping releases the Maintenance of its refrigerant system; the check's
        result is lost. Not possible while the outdoor unit has a communication
        error.

        Args:
        ----
            password: The service password.
            outdoor: The outdoor unit, by name or address.
            allow_leakage_check: Needed unless this is a dry run.

        Returns:
        -------
            The outdoor unit as listed afterwards, or None in a dry run.

        """
        self._experimental("stop a leakage check")
        return await self._leakage_check_run(
            password, outdoor, start=False, allowed=allow_leakage_check
        )

    # -- layout setup -------------------------------------------------------------
    async def fetch_layout_setup(self, password: str) -> LayoutSetup:
        """Tell whether the service tab offers the layout setup. Read only.

        Args:
        ----
            password: The service password.

        Returns:
        -------
            The button's state; it is never pressed.

        """
        return await self._in_session(lambda s: read_layout_setup(s, password))

    async def run_layout_setup(
        self, password: str, *, allow_service_change: bool = False
    ) -> bool | None:
        """Press the layout setup button: a job on the iTM (it has no screen).

        Args:
        ----
            password: The service password.
            allow_service_change: Needed unless this is a dry run.

        Returns:
        -------
            True when the job ran, None in a dry run. The iTM shows no result to
            read back.

        """
        self._experimental("run the layout setup")
        self._service_guard(allowed=allow_service_change)
        sent = await self._in_session(lambda s: layout_setup_job(s, password))
        return sent or None

    # -- R32 output ports ---------------------------------------------------------
    async def fetch_r32_output_ports(self, password: str) -> R32OutputPorts:
        """Read the R32 alarm output ports and the ports offered. Read only.

        Args:
        ----
            password: The service password.

        Returns:
        -------
            Ports 1 and 2 as shown, and the offered ports (1517, cancelled).

        """
        return await self._in_session(lambda s: read_r32_ports(s, password))

    async def set_r32_output_port(
        self,
        password: str,
        port: int,
        value: str | None,
        *,
        allow_service_change: bool = False,
    ) -> R32OutputPorts | None:
        """Set (or clear) an R32 alarm output port.

        The output test run is never pressed.

        Args:
        ----
            password: The service password.
            port: 1 or 2.
            value: The port as 1517 lists it (name, address or the whole row);
                None clears it.
            allow_service_change: Needed unless this is a dry run.

        Returns:
        -------
            The ports as read back, or None in a dry run.

        Raises:
        ------
            ItmCommandError: Not allowed, not offered, or the read-back differs.

        """
        self._experimental("change an R32 output port")
        self._service_guard(allowed=allow_service_change)
        await self._in_session(lambda s: set_r32_port(s, password, port, value))
        if self.dry_run:
            return None
        got = await self.fetch_r32_output_ports(password)
        if port in {1, 2} and not port_shown(got.ports[port - 1], value):
            msg = f"R32 output port {port}: the iTM shows {got.ports[port - 1]!r}"
            raise ItmCommandError(msg)
        return got

    # -- password setting ---------------------------------------------------------
    async def fetch_password_settings(self) -> PasswordSettings:
        """Read whether the administrator and the screen password are enabled.

        Returns
        -------
            The two radios' states (passwords themselves are never shown).

        """
        return await self._in_session(read_passwords)

    async def set_password_setting(
        self,
        kind: str,
        *,
        enabled: bool | None = None,
        password: str | None = None,
        allow_password_change: bool = False,
    ) -> PasswordSettings | None:
        """Enable or disable the administrator or screen password, or change it.

        These passwords lock the iTM's own touch panel; a mistake can lock people
        out of it. The password is sent once, only into the iTM's password input;
        it is never logged, and a dry run plans it as ``***``.

        Args:
        ----
            kind: "administrator" or "screen".
            enabled: Enable (True) or disable (False); None leaves it.
            password: A new password (enabling one without a password set asks
                for it).
            allow_password_change: Needed unless this is a dry run.

        Returns:
        -------
            The settings as read back, or None in a dry run.

        Raises:
        ------
            ItmCommandError: Not allowed, refused, or the read-back differs.

        """
        self._experimental("change a password setting")
        self._service_write_allowed(
            allowed=allow_password_change,
            flag="allow_password_change",
            why="a password change can lock people out of the iTM's touch panel",
        )
        await self._in_session(
            lambda s: set_password(s, kind, enabled=enabled, password=password)
        )
        if self.dry_run:
            return None
        got = await self.fetch_password_settings()
        if enabled is not None and getattr(got, kind) is not enabled:
            msg = f"the {kind} password is not {'en' if enabled else 'dis'}abled"
            raise ItmCommandError(msg)
        return got


__all__ = [
    "LayoutSetup",
    "LeakageCheck",
    "LeakageCheckProgram",
    "LeakageCheckResult",
    "LeakageCheckResults",
    "LeakageCheckSchedule",
    "LeakageOutdoorUnit",
    "LeakageRegistration",
    "PasswordSettings",
    "R32OutputPorts",
    "ServiceEditMixin",
    "parse_results",
]
