# ruff: noqa: SLF001
# pylint: disable=protected-access,too-many-lines
"""Dealer-option functions: temperature limit, sliding temperature, HMO.

Session-level flows on the screens of the functions the iTM only offers with a
dealer option: temperature limit groups (400-402), sliding temperature groups
(500-503) and heating mode optimisation per unit (600); interlock programs are in
``interlock``. Each takes an open ``_Session``, saves through the session's ``write``
/ ``press_ok`` (so a dry run plans instead of sending, and ``read_only`` refuses
before anything is sent) and leaves every screen it opened with its Close/Cancel.
``DealerFunctionsMixin`` gives ``ItmClient`` the public methods, each write
confirmed by a read-back in a fresh session.

Seen on a real iTM with the dealer options switched off (read only):

- The four menu buttons are hidden (VISIBLED 0) but open their screen.
- 400 (temperature limit) lists the columns activation, name, lower limit, upper
  limit; 500 (sliding temperature) activation, name, outdoor temperature point,
  then the limits and ranges. Both open with an empty list, Add enabled and
  the radios, Delete, Registration and Settings disabled until a row is
  selected.
- 600 (heating mode optimisation) lists every unit with the columns name and
  activation (empty while off); the radios are disabled until a row is selected,
  "All on" is enabled and "All off" disabled while all are off.

Assumed (not seen on a real iTM, only the web UI's part layout): a single-select
list selects a row with a CLICK + INT, a multi-select list (401/501 members) with
a CLICK + INTARRAY, as the browser does; a list row's activation text is the
enable radio's label when on; the radios, Delete and Add's name are confirmed
with a CONFIRM dialog that is answered Yes; Add and Rename ask the name in a
CHARINPUT dialog; Registration (401/501) lists the members with the name in the
first column; the limits and ranges are entered in NUMBER dialogs; the point
picker (503) only returns its choice to the screen that opened it, and only the
OK of 402 and 502 saves; an enabled group is disabled before it is deleted.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypeVar

from .exceptions import ItmCommandError
from .menu import (
    MB_HEATING_OPTIMISATION,
    MB_SLIDING_TEMP,
    MB_TEMP_LIMIT,
    MenuScreen,
)
from .protocol import DataType, EventType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from .protocol import Reply
    from .session import _Session
    from .state import UIState

_T = TypeVar("_T")

# Temperature limit (400) and sliding temperature (500): the group list.
G_CLOSE, G_LIST, G_ENABLE, G_DISABLE = 1, 3, 5, 6
G_CREATE, G_DELETE, G_MEMBERS, G_SETTINGS = 8, 9, 10, 11
# Their members screens (401, 501).
M_MEMBERS, M_ADD, M_REMOVE, M_AVAILABLE, M_OK, M_CANCEL = 2, 4, 5, 8, 9, 10
# A temperature limit group's settings (402).
TEMP_LIMIT_SETTINGS = 402
TA_NAME, TA_LOWER, TA_UPPER = 4, 5, 6
TA_NAME_BUTTON, TA_LOWER_BUTTON, TA_UPPER_BUTTON, TA_OK, TA_CANCEL = 9, 10, 11, 12, 13
# A sliding temperature group's settings (502) and its analog input picker (503).
SLIDING_SETTINGS, SLIDING_INPUTS = 502, 503
SA_NAME, SA_NAME_BUTTON, SA_POINT, SA_POINT_BUTTON = 2, 3, 5, 6
SA_OUTDOOR = ((8, 10), (12, 14))  # (text, change button): from, to
SA_SETPOINT = ((16, 18), (20, 22))
SA_OK, SA_CANCEL = 23, 24
AI_LIST, AI_OK, AI_CANCEL = 1, 2, 3
# Heating mode optimisation (600).
H_LIST, H_ENABLE, H_DISABLE, H_ALL_ON, H_ALL_OFF, H_CLOSE = 2, 4, 5, 6, 7, 8

TOLERANCE = 0.05  # °C, comparing a read-back temperature
_NUMBER = re.compile(r"-?\d+(?:[.,]\d+)?")


# -- models ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TemperatureLimitGroup:
    """A temperature limit group: keeps its members' setpoints within limits."""

    name: str
    enabled: bool
    lower: float | None  # °C, as listed
    upper: float | None  # °C, as listed
    members: tuple[str, ...] = ()  # with details only
    columns: tuple[str, ...] = ()  # the list row as shown


@dataclass(frozen=True)
class SlidingTemperatureGroup:
    """A sliding temperature group: its setpoint follows the outdoor temperature."""

    name: str
    enabled: bool
    outdoor_point: str | None  # the analog input giving the outdoor temperature
    columns: tuple[str, ...] = ()  # the list row as shown
    members: tuple[str, ...] = ()  # with details only
    outdoor_range: tuple[float | None, float | None] | None = None  # details: °C
    setpoint_range: tuple[float | None, float | None] | None = None  # details: °C


@dataclass(frozen=True)
class HeatingOptimisationUnit:
    """A unit on the heating mode optimisation screen and whether HMO is on."""

    name: str
    enabled: bool


@dataclass(frozen=True)
class _Groups:
    """The screens of a group function (temperature limit or sliding temperature)."""

    button: int
    main: MenuScreen
    members: int
    settings: int
    settings_cancel: int
    what: str


TEMP_LIMIT = _Groups(
    MB_TEMP_LIMIT,
    MenuScreen.TEMP_LIMIT,
    401,
    TEMP_LIMIT_SETTINGS,
    TA_CANCEL,
    "temperature limit group",
)
SLIDING = _Groups(
    MB_SLIDING_TEMP,
    MenuScreen.SLIDING_TEMP,
    501,
    SLIDING_SETTINGS,
    SA_CANCEL,
    "sliding temperature group",
)


# -- readers --------------------------------------------------------------------------


def _clean(value: Any) -> str:
    return html.unescape(str(value)).strip()


def _cells(state: UIState, screen: int, part: int) -> list[tuple[str, ...]]:
    rows = (state.get(screen, part, "LIST_ROW") or {}).get("rows", [])
    return [tuple(_clean(t) for t in r.get("text", [])) for r in rows]


def _text(state: UIState, screen: int, part: int) -> str | None:
    value = state.get(screen, part, "TEXT")
    return (_clean(value) or None) if isinstance(value, str) else None


def temperature(text: str | None) -> float | None:
    """Return the number in a shown temperature ("18.5", "18,5 °C"), or None."""
    m = _NUMBER.search(text or "")
    return float(m.group().replace(",", ".")) if m else None


def _activation(state: UIState, screen: int, disable: int, text: str) -> bool:
    """Tell whether a row's activation text means on.

    The text is the enable radio's label when on and empty (or the disable radio's
    label) when off, in any display language.
    """
    off = _clean(state.get(screen, disable, "LABEL") or "")
    return bool(text) and not (off and text == off)


def _cell(row: tuple[str, ...], index: int) -> str:
    return row[index] if len(row) > index else ""


def temperature_limits_from_screen(state: UIState) -> list[TemperatureLimitGroup]:
    """Read the temperature limit screen's group list (400)."""
    s = MenuScreen.TEMP_LIMIT
    return [
        TemperatureLimitGroup(
            name=_cell(row, 1),
            enabled=_activation(state, s, G_DISABLE, _cell(row, 0)),
            lower=temperature(_cell(row, 2)),
            upper=temperature(_cell(row, 3)),
            columns=row,
        )
        for row in _cells(state, s, G_LIST)
        if len(row) >= 2
    ]


def sliding_temperatures_from_screen(state: UIState) -> list[SlidingTemperatureGroup]:
    """Read the sliding temperature screen's group list (500)."""
    s = MenuScreen.SLIDING_TEMP
    return [
        SlidingTemperatureGroup(
            name=_cell(row, 1),
            enabled=_activation(state, s, G_DISABLE, _cell(row, 0)),
            outdoor_point=_cell(row, 2) or None,
            columns=row,
        )
        for row in _cells(state, s, G_LIST)
        if len(row) >= 2
    ]


def heating_optimisation_from_screen(state: UIState) -> list[HeatingOptimisationUnit]:
    """Read the heating mode optimisation screen's unit list (600)."""
    s = MenuScreen.HEATING_OPTIMISATION
    return [
        HeatingOptimisationUnit(
            name=row[0], enabled=_activation(state, s, H_DISABLE, _cell(row, 1))
        )
        for row in _cells(state, s, H_LIST)
        if row and row[0]
    ]


def members_from_screen(state: UIState, screen: int) -> tuple[str, ...]:
    """Read a group's members (401, 501): each row's first column."""
    return tuple(row[0] for row in _cells(state, screen, M_MEMBERS) if row and row[0])


def temperature_limit_settings_from_screen(state: UIState) -> dict[str, Any]:
    """Read a temperature limit group's settings screen (402)."""
    s = TEMP_LIMIT_SETTINGS
    return {
        "name": _text(state, s, TA_NAME),
        "lower": temperature(_text(state, s, TA_LOWER)),
        "upper": temperature(_text(state, s, TA_UPPER)),
    }


def sliding_settings_from_screen(state: UIState) -> dict[str, Any]:
    """Read a sliding temperature group's settings screen (502)."""
    s = SLIDING_SETTINGS

    def pair(
        parts: tuple[tuple[int, int], ...],
    ) -> tuple[float | None, float | None]:
        return (
            temperature(_text(state, s, parts[0][0])),
            temperature(_text(state, s, parts[1][0])),
        )

    return {
        "name": _text(state, s, SA_NAME),
        "outdoor_point": _text(state, s, SA_POINT),
        "outdoor_range": pair(SA_OUTDOOR),
        "setpoint_range": pair(SA_SETPOINT),
    }


# -- helpers --------------------------------------------------------------------------


def _row(rows: list[str], name: str, what: str) -> int:
    """Return the row showing name; raise when it isn't there exactly once."""
    found = [i for i, n in enumerate(rows) if n == name.strip()]
    if len(found) != 1:
        msg = f"{what} {name!r} not found once (found {len(found)}); listed: {rows}"
        raise ItmCommandError(msg)
    return found[0]


def _row_holding(rows: list[tuple[str, ...]], name: str, what: str) -> int:
    """Return the row with a cell showing name (the column layout is unknown)."""
    found = [i for i, r in enumerate(rows) if name.strip() in r]
    if len(found) != 1:
        listed = [" ".join(r) for r in rows]
        msg = f"{what} {name!r} not found once (found {len(found)}); listed: {listed}"
        raise ItmCommandError(msg)
    return found[0]


async def _select(s: _Session, screen: int, part: int, row: int) -> None:
    """Select a list row."""
    await s.select_row(screen, part, row)


async def _select_multi(s: _Session, screen: int, part: int, row: int) -> None:
    """Select one row of a multi-select list (CLICK + INTARRAY, as the browser)."""
    await s.send(screen, part, EventType.CLICK, DataType.INTARRAY, 1, array=(row,))


def _enabled(s: _Session, screen: int, part: int, what: str) -> None:
    """Raise when the iTM reports a part disabled (clicking it hangs the iTM)."""
    s.refuse_disabled(screen, part, what)


def _radio(state: UIState, screen: int, enable: int, disable: int) -> bool | None:
    """Return the selected row's activation as its radios show it."""
    if state.get(screen, enable, "CHECK_STATE") == 1:
        return True
    if state.get(screen, disable, "CHECK_STATE") == 1:
        return False
    return None


async def _open(s: _Session, button: int, screen: MenuScreen, what: str) -> None:
    if not await s.open_menu_screen(button, screen):
        msg = f"the {what} screen did not open"
        raise ItmCommandError(msg)


async def _leave(s: _Session, screen: int, cancel: int) -> None:
    """Leave a sub-screen with its Cancel, if open; a "discard?" question gets Yes."""
    if screen not in s.state.screens:
        return
    reply = await s.send(screen, cancel)
    for dialog in reply.dialogs():
        if dialog[1] == "CONFIRM":
            await s.send(screen, dialog[0])
    s.state.screens.pop(screen, None)


async def _answer_name(
    s: _Session, screen: int, reply: Reply, name: str, label: str
) -> bool:
    """Answer the CHARINPUT dialog a button opened, through ``s.write``.

    Returns
    -------
        True when sent, False in a dry run (the dialog is cancelled).

    """
    dialogs = [u for u in reply.screens if u.update == "DLG_OPEN"]
    if not dialogs or dialogs[0].data[1] != "CHARINPUT":
        msg = f"{label}: no name input opened: {reply.dialogs()}"
        raise ItmCommandError(msg)
    d = dialogs[0]
    low, high = d.data[4], d.data[5]
    if not low <= len(name) <= high:
        await s.send(d.screen, d.data[0], EventType.CLICK2)
        msg = f"{label}: a name is {low}-{high} characters: {name!r}"
        raise ItmCommandError(msg)
    sent = await s.write(
        label, d.screen, d.data[0], EventType.CLICK, DataType.STRING, string=name
    )
    if sent is None:
        await s.send(d.screen, d.data[0], EventType.CLICK2)
        return False
    await s._answer_confirm(screen, sent)
    return True


async def _press(s: _Session, label: str, screen: int, part: int) -> bool:
    """Press an immediate button (radio, delete) through ``s.write``; answer Yes.

    Returns
    -------
        True when sent, False in a dry run.

    """
    _enabled(s, screen, part, label)
    reply = await s.write(label, screen, part)
    if reply is None:
        return False
    await s._answer_confirm(screen, reply)
    return True


async def _return_choice(s: _Session, screen: int, ok: int, what: str) -> None:
    """Press a sub-screen's OK that only returns its choice (assumed)."""
    reply = await s.send(screen, ok)
    await s._answer_confirm(screen, reply)
    if screen in s.state.screens:
        msg = f"{what}: the screen did not close"
        raise ItmCommandError(msg)


# -- groups: temperature limit (400-402) and sliding temperature (500-503) ------------


def _group_names(state: UIState, g: _Groups) -> list[str]:
    rows = _cells(state, g.main, G_LIST)
    return [_cell(r, 1) for r in rows]


def _new_name_free(s: _Session, g: _Groups, name: str, new_name: str | None) -> None:
    """Raise when a group is to get the name of another group."""
    if new_name is None or new_name.strip() == name.strip():
        return
    if new_name.strip() in _group_names(s.state, g):
        msg = f"{g.what} {new_name!r} exists already"
        raise ItmCommandError(msg)


async def _select_group(s: _Session, g: _Groups, name: str) -> None:
    await _select(s, g.main, G_LIST, _row(_group_names(s.state, g), name, g.what))


def _group_enabled(s: _Session, g: _Groups, name: str) -> bool:
    """Return whether the selected group is enabled (radios, else the list)."""
    shown = _radio(s.state, g.main, G_ENABLE, G_DISABLE)
    if shown is not None:
        return shown
    rows = _cells(s.state, g.main, G_LIST)
    return any(
        _cell(r, 1) == name.strip()
        and _activation(s.state, g.main, G_DISABLE, _cell(r, 0))
        for r in rows
    )


async def _open_sub(s: _Session, g: _Groups, button: int, screen: int) -> None:
    _enabled(s, g.main, button, g.what)
    await s.send(g.main, button)
    if screen not in s.state.screens:
        msg = f"{g.what}: screen {screen} did not open"
        raise ItmCommandError(msg)


async def _read_group_details(s: _Session, g: _Groups, row: int) -> dict[str, Any]:
    """Select a group and read its members and settings screens."""
    await _select(s, g.main, G_LIST, row)
    await _open_sub(s, g, G_MEMBERS, g.members)
    members = members_from_screen(s.state, g.members)
    await _leave(s, g.members, M_CANCEL)
    await _open_sub(s, g, G_SETTINGS, g.settings)
    if g is TEMP_LIMIT:
        settings = temperature_limit_settings_from_screen(s.state)
    else:
        settings = sliding_settings_from_screen(s.state)
    await _leave(s, g.settings, g.settings_cancel)
    settings.pop("name")
    return {"members": members, **settings}


async def _read_groups(s: _Session, g: _Groups, *, details: bool) -> list[Any]:
    await _open(s, g.button, g.main, g.what)
    try:
        if g is TEMP_LIMIT:
            groups: list[Any] = temperature_limits_from_screen(s.state)
        else:
            groups = sliding_temperatures_from_screen(s.state)
        if not details:
            return groups
        out = []
        for i, group in enumerate(groups):
            found = await _read_group_details(s, g, i)
            if g is TEMP_LIMIT:
                found["lower"] = (
                    found["lower"] if found["lower"] is not None else group.lower
                )
                found["upper"] = (
                    found["upper"] if found["upper"] is not None else group.upper
                )
            else:
                point = found.pop("outdoor_point") or group.outdoor_point
                found["outdoor_point"] = point
            out.append(replace(group, **found))
        return out
    finally:
        await _leave(s, g.settings, g.settings_cancel)
        await _leave(s, g.members, M_CANCEL)
        await s.close_screen(g.main)


async def read_temperature_limits(
    s: _Session, *, details: bool = False
) -> list[TemperatureLimitGroup]:
    """Read the temperature limit groups; with details their members and settings.

    Returns
    -------
        The groups, in list order.

    """
    return await _read_groups(s, TEMP_LIMIT, details=details)


async def read_sliding_temperatures(
    s: _Session, *, details: bool = False
) -> list[SlidingTemperatureGroup]:
    """Read the sliding temperature groups; with details members and settings.

    Returns
    -------
        The groups, in list order.

    """
    return await _read_groups(s, SLIDING, details=details)


async def create_group(s: _Session, g: _Groups, name: str) -> bool:
    """Add a group and give it its name; it starts empty and disabled.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    await _open(s, g.button, g.main, g.what)
    try:
        if name.strip() in _group_names(s.state, g):
            msg = f"{g.what} {name!r} exists already"
            raise ItmCommandError(msg)
        _enabled(s, g.main, G_CREATE, f"add {g.what}")
        reply = await s.send(g.main, G_CREATE)
        return await _answer_name(s, g.main, reply, name, f"create {g.what} {name}")
    finally:
        await s.close_screen(g.main)


async def delete_group(s: _Session, g: _Groups, name: str) -> bool:
    """Delete a disabled group and confirm.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    await _open(s, g.button, g.main, g.what)
    try:
        await _select_group(s, g, name)
        if _group_enabled(s, g, name):
            msg = f"{name}: disable the {g.what} before deleting it"
            raise ItmCommandError(msg)
        return await _press(s, f"delete {g.what} {name}", g.main, G_DELETE)
    finally:
        await s.close_screen(g.main)


async def set_group_enabled(
    s: _Session, g: _Groups, name: str, *, enabled: bool
) -> bool:
    """Enable or disable a group.

    Returns
    -------
        True when sent (or already so), False in a dry run.

    """
    s._writable()
    await _open(s, g.button, g.main, g.what)
    try:
        await _select_group(s, g, name)
        if _group_enabled(s, g, name) is enabled:
            return True
        verb = "enable" if enabled else "disable"
        part = G_ENABLE if enabled else G_DISABLE
        return await _press(s, f"{verb} {g.what} {name}", g.main, part)
    finally:
        await s.close_screen(g.main)


async def set_group_members(
    s: _Session, g: _Groups, name: str, members: tuple[str, ...]
) -> bool:
    """Make a group's members exactly these (Registration: add, remove, OK).

    Returns
    -------
        True when saved, False in a dry run.

    """
    s._writable()
    await _open(s, g.button, g.main, g.what)
    try:
        await _select_group(s, g, name)
        await _open_sub(s, g, G_MEMBERS, g.members)
        wanted = list(dict.fromkeys(m.strip() for m in members))
        for member in members_from_screen(s.state, g.members):
            if member not in wanted:
                current = list(members_from_screen(s.state, g.members))
                await _select_multi(
                    s, g.members, M_MEMBERS, _row(current, member, "member")
                )
                _enabled(s, g.members, M_REMOVE, f"remove from {g.what}")
                await s.send(g.members, M_REMOVE)
        for member in wanted:
            if member in members_from_screen(s.state, g.members):
                continue
            available = [_cell(r, 0) for r in _cells(s.state, g.members, M_AVAILABLE)]
            try:
                row = _row(available, member, "unit")
            except ItmCommandError as err:
                msg = f"{name}: {member!r} is not offered: {err}"
                raise ItmCommandError(msg) from err
            await _select_multi(s, g.members, M_AVAILABLE, row)
            _enabled(s, g.members, M_ADD, f"add to {g.what}")
            await s.send(g.members, M_ADD)
        got = list(members_from_screen(s.state, g.members))
        if got != wanted:
            msg = f"{name}: the members would be {got}, not {wanted}"
            raise ItmCommandError(msg)
        return await s.press_ok(
            f"save members of {g.what} {name}", g.members, M_OK, M_CANCEL
        )
    finally:
        await _leave(s, g.members, M_CANCEL)
        await s.close_screen(g.main)


async def _number(  # noqa: PLR0913  # pylint: disable=too-many-arguments
    s: _Session, screen: int, button: int, value: float | None, what: str, *, g: _Groups
) -> None:
    if value is None:
        return
    _enabled(s, screen, button, f"{g.what} {what}")
    await s.answer_number(button, value, f"{g.what} {what}", screen=screen)


async def set_temperature_limit_settings(
    s: _Session,
    name: str,
    *,
    new_name: str | None = None,
    lower: float | None = None,
    upper: float | None = None,
) -> bool:
    """Change a temperature limit group's name and/or limits (402), then OK.

    Args:
    ----
        s: The open session.
        name: The group.
        new_name: Its new name (None: unchanged).
        lower: The lower limit, °C (None: unchanged).
        upper: The upper limit, °C (None: unchanged).

    Returns:
    -------
        True when saved, False in a dry run.

    """
    s._writable()
    g = TEMP_LIMIT
    await _open(s, g.button, g.main, g.what)
    try:
        _new_name_free(s, g, name, new_name)
        await _select_group(s, g, name)
        await _open_sub(s, g, G_SETTINGS, g.settings)
        a = g.settings
        if new_name is not None:
            _enabled(s, a, TA_NAME_BUTTON, f"{g.what} name")
            await s.answer_text(a, TA_NAME_BUTTON, new_name, f"{g.what} name")
        await _number(s, a, TA_LOWER_BUTTON, lower, "lower limit", g=g)
        await _number(s, a, TA_UPPER_BUTTON, upper, "upper limit", g=g)
        return await s.press_ok(f"save {g.what} {name}", a, TA_OK, TA_CANCEL)
    finally:
        await _leave(s, g.settings, g.settings_cancel)
        await s.close_screen(g.main)


async def _open_inputs(s: _Session) -> None:
    _enabled(s, SLIDING_SETTINGS, SA_POINT_BUTTON, "outdoor temperature point")
    await s.send(SLIDING_SETTINGS, SA_POINT_BUTTON)
    if SLIDING_INPUTS not in s.state.screens:
        msg = "the outdoor temperature point picker did not open"
        raise ItmCommandError(msg)


async def read_sliding_inputs(s: _Session, name: str) -> tuple[str, ...]:
    """Read the analog inputs a sliding temperature group could follow (503).

    Returns
    -------
        Each offered row, its cells joined by spaces.

    """
    g = SLIDING
    await _open(s, g.button, g.main, g.what)
    try:
        await _select_group(s, g, name)
        await _open_sub(s, g, G_SETTINGS, g.settings)
        await _open_inputs(s)
        return tuple(
            " ".join(c for c in r if c)
            for r in _cells(s.state, SLIDING_INPUTS, AI_LIST)
            if any(r)
        )
    finally:
        await _leave(s, SLIDING_INPUTS, AI_CANCEL)
        await _leave(s, g.settings, g.settings_cancel)
        await s.close_screen(g.main)


async def set_sliding_settings(  # noqa: PLR0913  # pylint: disable=too-many-arguments
    s: _Session,
    name: str,
    *,
    new_name: str | None = None,
    outdoor_point: str | None = None,
    outdoor_range: tuple[float, float] | None = None,
    setpoint_range: tuple[float, float] | None = None,
) -> bool:
    """Change a sliding temperature group's settings (502), then OK.

    Args:
    ----
        s: The open session.
        name: The group.
        new_name: Its new name (None: unchanged).
        outdoor_point: The analog input giving the outdoor temperature, as the
            picker (503) lists it (None: unchanged).
        outdoor_range: The outdoor temperatures (from, to) over which the setpoint
            slides, °C (None: unchanged).
        setpoint_range: The setpoints (from, to) that go with them, °C.

    Returns:
    -------
        True when saved, False in a dry run.

    """
    s._writable()
    g = SLIDING
    await _open(s, g.button, g.main, g.what)
    try:
        _new_name_free(s, g, name, new_name)
        await _select_group(s, g, name)
        await _open_sub(s, g, G_SETTINGS, g.settings)
        a = g.settings
        if new_name is not None:
            _enabled(s, a, SA_NAME_BUTTON, f"{g.what} name")
            await s.answer_text(a, SA_NAME_BUTTON, new_name, f"{g.what} name")
        if outdoor_point is not None:
            await _open_inputs(s)
            rows = _cells(s.state, SLIDING_INPUTS, AI_LIST)
            row = _row_holding(rows, outdoor_point, "analog input")
            await _select(s, SLIDING_INPUTS, AI_LIST, row)
            await _return_choice(s, SLIDING_INPUTS, AI_OK, "outdoor temperature point")
        for values, parts, what in (
            (outdoor_range, SA_OUTDOOR, "outdoor temperature"),
            (setpoint_range, SA_SETPOINT, "setpoint"),
        ):
            if values is not None:
                await _number(s, a, parts[0][1], values[0], f"{what} from", g=g)
                await _number(s, a, parts[1][1], values[1], f"{what} to", g=g)
        return await s.press_ok(f"save {g.what} {name}", a, SA_OK, SA_CANCEL)
    finally:
        await _leave(s, SLIDING_INPUTS, AI_CANCEL)
        await _leave(s, g.settings, g.settings_cancel)
        await s.close_screen(g.main)


# -- heating mode optimisation (600) --------------------------------------------------

HMO = MenuScreen.HEATING_OPTIMISATION
HMO_WHAT = "heating mode optimisation"


async def read_heating_optimisation(s: _Session) -> list[HeatingOptimisationUnit]:
    """Read every unit and whether heating mode optimisation is on (the list).

    Returns
    -------
        The units, in list order.

    """
    await _open(s, MB_HEATING_OPTIMISATION, HMO, HMO_WHAT)
    try:
        return heating_optimisation_from_screen(s.state)
    finally:
        await s.close_screen(HMO)


async def set_heating_optimisation(
    s: _Session, names: tuple[str, ...], *, enabled: bool
) -> bool:
    """Switch heating mode optimisation on or off for these units (the radios).

    Returns
    -------
        True when sent (or already so), False in a dry run.

    """
    s._writable()
    await _open(s, MB_HEATING_OPTIMISATION, HMO, HMO_WHAT)
    try:
        sent = True
        for name in dict.fromkeys(n.strip() for n in names):
            units = [u.name for u in heating_optimisation_from_screen(s.state)]
            await _select(s, HMO, H_LIST, _row(units, name, "unit"))
            shown = _radio(s.state, HMO, H_ENABLE, H_DISABLE)
            if shown is None:
                listed = heating_optimisation_from_screen(s.state)
                shown = next(u.enabled for u in listed if u.name == name)
            if shown is enabled:
                continue
            verb = "on" if enabled else "off"
            part = H_ENABLE if enabled else H_DISABLE
            sent = await _press(s, f"{HMO_WHAT} {verb} for {name}", HMO, part) and sent
        return sent
    finally:
        await s.close_screen(HMO)


async def set_heating_optimisation_all(s: _Session, *, enabled: bool) -> bool:
    """Switch heating mode optimisation on ("All on") or off for every unit.

    Returns
    -------
        True when sent (or already so), False in a dry run.

    """
    s._writable()
    await _open(s, MB_HEATING_OPTIMISATION, HMO, HMO_WHAT)
    try:
        units = heating_optimisation_from_screen(s.state)
        if units and all(u.enabled is enabled for u in units):
            return True
        part = H_ALL_ON if enabled else H_ALL_OFF
        label = f"{HMO_WHAT} {'on' if enabled else 'off'} for all units"
        return await _press(s, label, HMO, part)
    finally:
        await s.close_screen(HMO)


# -- the client's public methods ------------------------------------------------------


def _close(a: float | None, b: float | None) -> bool:
    return a is not None and b is not None and abs(a - b) <= TOLERANCE


def _check_wrong(name: str, wrong: list[str], result: object) -> None:
    if wrong:
        msg = f"{name}: the iTM did not take the {', '.join(wrong)}: {result}"
        raise ItmCommandError(msg)


async def _discard(call: Awaitable[Any]) -> None:
    """Await a session write and drop its "sent" flag (dry runs plan instead)."""
    await call


class DealerFunctionsMixin:  # pylint: disable=too-many-public-methods
    """``ItmClient`` methods for the dealer-option functions (mixed in).

    Every write runs in its own session and is confirmed by a read in a fresh
    one; with ``dry_run`` it returns None and only plans, with ``read_only`` it
    raises ReadOnlyError before anything is sent.
    """

    if TYPE_CHECKING:
        dry_run: bool

        async def _in_session(self, call: Callable[[_Session], Awaitable[_T]]) -> _T:
            """Run one session call (ItmClient)."""

        async def _system_write(
            self, call: Callable[[_Session], Awaitable[None]]
        ) -> None:
            """Run one write session call (ItmClient)."""

        def _experimental(self, what: str) -> None:
            """Refuse a write not validated on a real iTM yet (ItmClient)."""

    # -- temperature limit -------------------------------------------------------------
    async def fetch_temperature_limits(
        self, *, details: bool = False
    ) -> list[TemperatureLimitGroup]:
        """Read the temperature limit groups (a dealer option).

        Args:
        ----
            details: Also open each group's members and settings screens.

        Returns:
        -------
            The groups; empty when there are none (the screen opens without the
            dealer option too).

        """
        return await self._in_session(
            lambda s: read_temperature_limits(s, details=details)
        )

    async def _temperature_limit(self, name: str) -> TemperatureLimitGroup | None:
        groups = await self.fetch_temperature_limits(details=True)
        return next((g for g in groups if g.name == name.strip()), None)

    async def create_temperature_limit(
        self,
        name: str,
        *,
        members: Iterable[str] = (),
        lower: float | None = None,
        upper: float | None = None,
    ) -> TemperatureLimitGroup | None:
        """Create a temperature limit group (disabled), then its members and limits.

        Args:
        ----
            name: Its name.
            members: Its units.
            lower: The lower limit, °C.
            upper: The upper limit, °C.

        Returns:
        -------
            The group as read back, or None in a dry run.

        """
        self._experimental("create a temperature limit group")
        await self._system_write(lambda s: _discard(create_group(s, TEMP_LIMIT, name)))
        if self.dry_run:
            return None
        if await self._temperature_limit(name) is None:
            msg = f"{name}: the iTM did not add the temperature limit group"
            raise ItmCommandError(msg)
        wanted = tuple(members)
        if wanted:
            await self.set_temperature_limit_members(name, wanted)
        if lower is not None or upper is not None:
            await self.set_temperature_limit(name, lower=lower, upper=upper)
        return await self._temperature_limit(name)

    async def set_temperature_limit(
        self,
        name: str,
        *,
        new_name: str | None = None,
        lower: float | None = None,
        upper: float | None = None,
    ) -> TemperatureLimitGroup | None:
        """Change a temperature limit group's name and/or limits.

        Args:
        ----
            name: The group.
            new_name: Its new name.
            lower: The lower limit, °C (within the range the iTM's input offers).
            upper: The upper limit, °C.

        Returns:
        -------
            The group as read back, or None in a dry run.

        """
        self._experimental("change a temperature limit group")
        await self._system_write(
            lambda s: _discard(
                set_temperature_limit_settings(
                    s, name, new_name=new_name, lower=lower, upper=upper
                )
            )
        )
        if self.dry_run:
            return None
        result = await self._temperature_limit(new_name or name)
        if result is None:
            msg = f"{name}: the temperature limit group is gone after the change"
            raise ItmCommandError(msg)
        wrong = [
            what
            for what, want, have in (
                ("lower", lower, result.lower),
                ("upper", upper, result.upper),
            )
            if want is not None and not _close(want, have)
        ]
        _check_wrong(name, wrong, result)
        return result

    async def set_temperature_limit_members(
        self, name: str, members: Iterable[str]
    ) -> TemperatureLimitGroup | None:
        """Make a temperature limit group's members exactly these units.

        Returns
        -------
            The group as read back, or None in a dry run.

        """
        self._experimental("change a temperature limit group's members")
        wanted = tuple(members)
        await self._system_write(
            lambda s: _discard(set_group_members(s, TEMP_LIMIT, name, wanted))
        )
        if self.dry_run:
            return None
        result = await self._temperature_limit(name)
        if result is None or set(result.members) != {m.strip() for m in wanted}:
            msg = f"{name}: the iTM did not take the members {list(wanted)}: {result}"
            raise ItmCommandError(msg)
        return result

    async def set_temperature_limit_enabled(
        self, name: str, *, enabled: bool
    ) -> TemperatureLimitGroup | None:
        """Enable or disable a temperature limit group.

        Returns
        -------
            The group as read back, or None in a dry run.

        """
        self._experimental("enable or disable a temperature limit group")
        await self._system_write(
            lambda s: _discard(set_group_enabled(s, TEMP_LIMIT, name, enabled=enabled))
        )
        if self.dry_run:
            return None
        result = await self._temperature_limit(name)
        if result is None or result.enabled != enabled:
            msg = f"{name}: the iTM did not {'enable' if enabled else 'disable'} it"
            raise ItmCommandError(msg)
        return result

    async def delete_temperature_limit(self, name: str) -> None:
        """Delete a disabled temperature limit group."""
        self._experimental("delete a temperature limit group")
        await self._system_write(lambda s: _discard(delete_group(s, TEMP_LIMIT, name)))
        if not self.dry_run:
            names = [g.name for g in await self.fetch_temperature_limits()]
            if name.strip() in names:
                msg = f"{name}: the iTM did not delete the temperature limit group"
                raise ItmCommandError(msg)

    # -- sliding temperature -----------------------------------------------------------
    async def fetch_sliding_temperatures(
        self, *, details: bool = False
    ) -> list[SlidingTemperatureGroup]:
        """Read the sliding temperature groups (a dealer option).

        Args:
        ----
            details: Also open each group's members and settings screens.

        Returns:
        -------
            The groups; empty when there are none.

        """
        return await self._in_session(
            lambda s: read_sliding_temperatures(s, details=details)
        )

    async def fetch_sliding_temperature_inputs(self, name: str) -> tuple[str, ...]:
        """Read the analog inputs a sliding temperature group could follow.

        Returns
        -------
            The rows of the outdoor temperature point picker, as shown.

        """
        return await self._in_session(lambda s: read_sliding_inputs(s, name))

    async def _sliding(self, name: str) -> SlidingTemperatureGroup | None:
        groups = await self.fetch_sliding_temperatures(details=True)
        return next((g for g in groups if g.name == name.strip()), None)

    async def create_sliding_temperature(
        self, name: str, *, members: Iterable[str] = ()
    ) -> SlidingTemperatureGroup | None:
        """Create a sliding temperature group (disabled), then its members.

        Returns
        -------
            The group as read back, or None in a dry run.

        """
        self._experimental("create a sliding temperature group")
        await self._system_write(lambda s: _discard(create_group(s, SLIDING, name)))
        if self.dry_run:
            return None
        if await self._sliding(name) is None:
            msg = f"{name}: the iTM did not add the sliding temperature group"
            raise ItmCommandError(msg)
        wanted = tuple(members)
        if wanted:
            await self.set_sliding_temperature_members(name, wanted)
        return await self._sliding(name)

    async def set_sliding_temperature(  # pylint: disable=too-many-arguments
        self,
        name: str,
        *,
        new_name: str | None = None,
        outdoor_point: str | None = None,
        outdoor_range: tuple[float, float] | None = None,
        setpoint_range: tuple[float, float] | None = None,
    ) -> SlidingTemperatureGroup | None:
        """Change a sliding temperature group's name, outdoor point and ranges.

        Args:
        ----
            name: The group.
            new_name: Its new name.
            outdoor_point: The analog input for the outdoor temperature, as
                ``fetch_sliding_temperature_inputs`` lists it.
            outdoor_range: Outdoor temperatures (from, to), °C.
            setpoint_range: The setpoints (from, to) going with them, °C.

        Returns:
        -------
            The group as read back, or None in a dry run.

        """
        self._experimental("change a sliding temperature group")
        await self._system_write(
            lambda s: _discard(
                set_sliding_settings(
                    s,
                    name,
                    new_name=new_name,
                    outdoor_point=outdoor_point,
                    outdoor_range=outdoor_range,
                    setpoint_range=setpoint_range,
                )
            )
        )
        if self.dry_run:
            return None
        result = await self._sliding(new_name or name)
        if result is None:
            msg = f"{name}: the sliding temperature group is gone after the change"
            raise ItmCommandError(msg)
        wrong = []
        if outdoor_point is not None and not _same_point(
            result.outdoor_point, outdoor_point
        ):
            wrong.append("outdoor point")
        for what, want, have in (
            ("outdoor range", outdoor_range, result.outdoor_range),
            ("setpoint range", setpoint_range, result.setpoint_range),
        ):
            if want is not None and not (
                have is not None and all(map(_close, want, have))
            ):
                wrong.append(what)
        _check_wrong(name, wrong, result)
        return result

    async def set_sliding_temperature_members(
        self, name: str, members: Iterable[str]
    ) -> SlidingTemperatureGroup | None:
        """Make a sliding temperature group's members exactly these units.

        Returns
        -------
            The group as read back, or None in a dry run.

        """
        self._experimental("change a sliding temperature group's members")
        wanted = tuple(members)
        await self._system_write(
            lambda s: _discard(set_group_members(s, SLIDING, name, wanted))
        )
        if self.dry_run:
            return None
        result = await self._sliding(name)
        if result is None or set(result.members) != {m.strip() for m in wanted}:
            msg = f"{name}: the iTM did not take the members {list(wanted)}: {result}"
            raise ItmCommandError(msg)
        return result

    async def set_sliding_temperature_enabled(
        self, name: str, *, enabled: bool
    ) -> SlidingTemperatureGroup | None:
        """Enable or disable a sliding temperature group.

        Returns
        -------
            The group as read back, or None in a dry run.

        """
        self._experimental("enable or disable a sliding temperature group")
        await self._system_write(
            lambda s: _discard(set_group_enabled(s, SLIDING, name, enabled=enabled))
        )
        if self.dry_run:
            return None
        result = await self._sliding(name)
        if result is None or result.enabled != enabled:
            msg = f"{name}: the iTM did not {'enable' if enabled else 'disable'} it"
            raise ItmCommandError(msg)
        return result

    async def delete_sliding_temperature(self, name: str) -> None:
        """Delete a disabled sliding temperature group."""
        self._experimental("delete a sliding temperature group")
        await self._system_write(lambda s: _discard(delete_group(s, SLIDING, name)))
        if not self.dry_run:
            names = [g.name for g in await self.fetch_sliding_temperatures()]
            if name.strip() in names:
                msg = f"{name}: the iTM did not delete the sliding temperature group"
                raise ItmCommandError(msg)

    # -- heating mode optimisation -----------------------------------------------------
    async def fetch_heating_optimisation(self) -> list[HeatingOptimisationUnit]:
        """Read whether heating mode optimisation is on, per unit.

        Returns
        -------
            Every unit the screen lists.

        """
        return await self._in_session(read_heating_optimisation)

    async def set_heating_optimisation(
        self, units: Iterable[str] | None = None, *, enabled: bool
    ) -> list[HeatingOptimisationUnit] | None:
        """Switch heating mode optimisation on or off.

        Args:
        ----
            units: The units; None for all of them ("All on" / "All off").
            enabled: On (True) or off.

        Returns:
        -------
            Every unit as read back, or None in a dry run.

        """
        self._experimental("change the heating mode optimisation")
        names = None if units is None else tuple(u.strip() for u in units)
        if names is None:
            await self._system_write(
                lambda s: _discard(set_heating_optimisation_all(s, enabled=enabled))
            )
        else:
            await self._system_write(
                lambda s: _discard(set_heating_optimisation(s, names, enabled=enabled))
            )
        if self.dry_run:
            return None
        result = await self.fetch_heating_optimisation()
        wrong = [
            u.name
            for u in result
            if (names is None or u.name in names) and u.enabled != enabled
        ]
        _check_wrong(
            "heating mode optimisation", [f"change of {wrong}"] if wrong else [], result
        )
        return result


def _same_point(shown: str | None, wanted: str) -> bool:
    """Tell whether the shown outdoor point is the picked one (as listed in 503)."""
    if not shown:
        return False
    return shown in wanted or wanted.strip() in shown


__all__ = [
    "SLIDING",
    "TEMP_LIMIT",
    "DealerFunctionsMixin",
    "HeatingOptimisationUnit",
    "SlidingTemperatureGroup",
    "TemperatureLimitGroup",
]
