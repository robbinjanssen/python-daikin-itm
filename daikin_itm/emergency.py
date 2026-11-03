# ruff: noqa: SLF001
# pylint: disable=protected-access
"""Emergency stop programs: arm/disarm, add, copy, rename, delete, settings, release.

Session-level flows on the emergency stop screens: the program list (200), the
release screen (201), a program's settings (202) and its point picker (203). Each
takes an open ``_Session``, saves through the session's ``write`` / ``press_ok`` (so
a dry run plans instead of sending, and ``read_only`` refuses) and leaves every
screen it opened. Nothing here ever starts an emergency stop.

What the iTM does, as seen on a real one (read-only):

- Without the "emergency stop" dealer option there is one built-in program; the
  edit group (Add, Copy, Delete, Rename, Edit) is hidden.
- Selecting a program row (a CLICK with the row as INT) enables the Enable and
  Disable radios; for the built-in program Copy, Delete, Rename and
  Edit stay disabled. The library refuses those with a clear error and never
  clicks a disabled part.
- Add asks the new program's name in a CHARINPUT dialog (1-20 characters).
- Edit on an enabled program asks "open in read-only mode?" and, answered Yes,
  did not open the settings: a program's settings are only changed while it is
  disabled. The library disables nothing on its own; disarming is the caller's
  explicit step (``set_program_enabled(..., enabled=False)``).

Assumed (not seen on a real iTM): the radios apply at once (a confirm is answered
Yes), Copy and Rename ask the name in a CHARINPUT dialog like Add, the
lists of 201-203 select a row with a CLICK + INT like the program list and show
a point's (or program's) name in their last column, the settings point picker's OK
only returns the choice to 202, and the main menu bar's emergency stop indicator
(screen 0 part 20, shown only while a stop is active) opens the release screen.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .exceptions import ItmCommandError
from .menu import (
    MB_EMERGENCY_STOP,
    MM_EMERGENCY_STOP,
    EmergencyStopProgram,
    MenuScreen,
    emergency_stop_from_screen,
)
from .protocol import DataType, EventType, ScreenID

if TYPE_CHECKING:
    from .client import _Session
    from .protocol import Reply
    from .state import UIState

# The program list (200, ScEmgStopMain).
MAIN = MenuScreen.EMERGENCY_STOP
ES_CLOSE, ES_LIST, ES_ENABLE, ES_DISABLE, ES_EDIT_GROUP = 1, 2, 4, 5, 6
ES_CREATE, ES_COPY, ES_DELETE, ES_RENAME, ES_EDIT = 7, 8, 9, 10, 11
# The release screen (201, ScEmgStopRelease).
RELEASE = 201
RL_LIST, RL_RELEASE, RL_CANCEL = 1, 2, 3
# A program's settings (202, ScEmgStopSetting).
SETTINGS = 202
ST_IN_MODIFY, ST_IN_LIST, ST_RELEASE_MODE = 2, 3, 5
ST_OUT_MODIFY, ST_OUT_LIST, ST_STOP, ST_OK, ST_CANCEL = 7, 8, 10, 11, 12
# The point picker (203, ScEmgStopMngSelect).
PICKER = 203
PK_SELECTED, PK_ADD, PK_REMOVE, PK_AVAILABLE, PK_OK, PK_CANCEL = 2, 4, 5, 8, 9, 10

BUILT_IN = (
    "the built-in program can't be copied, deleted, renamed or edited "
    "(the iTM disables those buttons for it)"
)


@dataclass(frozen=True)
class EmergencyStopSettings:
    """An emergency stop program's settings (screen 202), as the iTM shows them."""

    name: str
    inputs: tuple[str, ...]  # the points whose signal starts the stop
    outputs: tuple[str, ...]  # the points the stop acts on
    release_mode: str | None  # the shown option
    release_mode_options: tuple[str, ...]
    stop: str | None  # what the stop does to the outputs: the shown option
    stop_options: tuple[str, ...]


# -- helpers --------------------------------------------------------------------------


def _cells(state: UIState, screen: int, part: int) -> list[list[str]]:
    rows = (state.get(screen, part, "LIST_ROW") or {}).get("rows", [])
    return [[html.unescape(str(t)).strip() for t in r.get("text", [])] for r in rows]


def _last_column(state: UIState, screen: int, part: int) -> list[str]:
    """Return each row's last cell: the name, in the lists of 201-203 (assumed)."""
    return [row[-1] if row else "" for row in _cells(state, screen, part)]


def _row(names: list[str], name: str, what: str) -> int:
    """Return the row showing name; raise when it isn't there exactly once."""
    found = [i for i, n in enumerate(names) if n == name.strip()]
    if len(found) != 1:
        msg = f"{what} {name!r} not found once (found {len(found)}); listed: {names}"
        raise ItmCommandError(msg)
    return found[0]


async def _select(s: _Session, screen: int, part: int, row: int) -> None:
    await s.send(screen, part, EventType.CLICK, DataType.INT, row)


async def _open(s: _Session) -> None:
    if not await s.open_menu_screen(MB_EMERGENCY_STOP, MAIN):
        msg = "the emergency stop screen did not open"
        raise ItmCommandError(msg)


def _names(state: UIState) -> list[str]:
    return [p.name.strip() for p in emergency_stop_from_screen(state)]


async def _select_program(s: _Session, name: str) -> None:
    """Select a program's row on the open program list (200)."""
    await _select(s, MAIN, ES_LIST, _row(_names(s.state), name, "emergency stop"))


def _radio_enabled(state: UIState) -> bool | None:
    """Return whether the selected program is armed, as its radios show."""
    if state.get(MAIN, ES_ENABLE, "CHECK_STATE") == 1:
        return True
    if state.get(MAIN, ES_DISABLE, "CHECK_STATE") == 1:
        return False
    return None


def _selected_enabled(state: UIState, name: str) -> bool:
    """Return whether the selected program is armed (radios, else the list)."""
    shown = _radio_enabled(state)
    if shown is not None:
        return shown
    listed = {p.name.strip(): p.enabled for p in emergency_stop_from_screen(state)}
    return listed.get(name.strip(), False)


def _allowed(s: _Session, part: int, name: str, verb: str) -> None:
    """Raise when the iTM disables a program button (the built-in program)."""
    if s.state.get(MAIN, part, "ENABLED") == 0:
        msg = f"{name}: the iTM does not allow {verb} this program; {BUILT_IN}"
        raise ItmCommandError(msg)


async def _answer_name(s: _Session, reply: Reply, name: str, label: str) -> bool:
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
        msg = f"an emergency stop program name is {low}-{high} characters: {name!r}"
        raise ItmCommandError(msg)
    sent = await s.write(
        label, d.screen, d.data[0], EventType.CLICK, DataType.STRING, string=name
    )
    if sent is None:  # dry run: cancel the name input
        await s.send(d.screen, d.data[0], EventType.CLICK2)
        return False
    await s._answer_confirm(MAIN, sent)
    return True


async def _leave(s: _Session, screen: int, cancel: int) -> None:
    """Leave a sub-screen with its Cancel, if it is still open."""
    if screen in s.state.screens:
        await s.send(screen, cancel)
        s.state.screens.pop(screen, None)


def _exists(s: _Session, name: str) -> None:
    if name.strip() in _names(s.state):
        msg = f"emergency stop program {name!r} exists already"
        raise ItmCommandError(msg)


# -- the program list (200) ------------------------------------------------------------


async def read_programs(s: _Session) -> list[EmergencyStopProgram]:
    """Read the program list, each program's activation as its radios show it.

    Selecting a row is harmless; the radios are what the iTM itself shows as the
    program's state, so they are preferred over the list's text column.

    Returns
    -------
        The programs, in list order.

    """
    await _open(s)
    try:
        out = []
        for i, program in enumerate(emergency_stop_from_screen(s.state)):
            await _select(s, MAIN, ES_LIST, i)
            shown = _radio_enabled(s.state)
            out.append(
                EmergencyStopProgram(
                    name=program.name.strip(),
                    enabled=program.enabled if shown is None else shown,
                )
            )
        return out
    finally:
        await s.close_screen(MAIN)


async def set_program_enabled(s: _Session, name: str, *, enabled: bool) -> bool:
    """Arm (Enable) or disarm (Disable) a program.

    Disarming switches a safety function off, and arming may start a stop at once
    when its input is already in alarm: the client guards this with
    ``allow_safety_change``.

    Returns
    -------
        True when sent (or already so), False in a dry run.

    """
    await _open(s)
    try:
        await _select_program(s, name)
        if _radio_enabled(s.state) is enabled:
            return True
        part = ES_ENABLE if enabled else ES_DISABLE
        if s.state.get(MAIN, part, "ENABLED") == 0:
            msg = f"{name}: the iTM has (de)activation disabled"
            raise ItmCommandError(msg)
        verb = "enable" if enabled else "disable"
        reply = await s.write(f"{verb} emergency stop program {name}", MAIN, part)
        if reply is None:
            return False
        await s._answer_confirm(MAIN, reply)
        return True
    finally:
        await s.close_screen(MAIN)


async def create_program(s: _Session, name: str) -> bool:
    """Add a program and give it its name.

    Refused while the iTM hides the edit buttons (no emergency stop dealer option).

    Returns
    -------
        True when sent, False in a dry run.

    """
    await _open(s)
    try:
        _exists(s, name)
        if not s.state.visible(MAIN, ES_EDIT_GROUP) or not s.state.visible(
            MAIN, ES_CREATE
        ):
            msg = (
                "the iTM hides the emergency stop program editing "
                "(it needs the emergency stop dealer option)"
            )
            raise ItmCommandError(msg)
        reply = await s._click_enabled(MAIN, ES_CREATE, "add emergency stop program")
        return await _answer_name(
            s, reply, name, f"create emergency stop program {name}"
        )
    finally:
        await s.close_screen(MAIN)


async def rename_program(s: _Session, name: str, new_name: str) -> bool:
    """Rename a program; not the built-in one.

    Returns
    -------
        True when sent, False in a dry run.

    """
    await _open(s)
    try:
        _exists(s, new_name)
        await _select_program(s, name)
        _allowed(s, ES_RENAME, name, "renaming")
        reply = await s._click_enabled(MAIN, ES_RENAME, "rename emergency stop program")
        return await _answer_name(
            s, reply, new_name, f"rename emergency stop program {name} to {new_name}"
        )
    finally:
        await s.close_screen(MAIN)


async def copy_program(s: _Session, name: str, new_name: str) -> bool:
    """Copy a program under a new name; not the built-in one.

    Returns
    -------
        True when sent, False in a dry run.

    """
    await _open(s)
    try:
        _exists(s, new_name)
        await _select_program(s, name)
        _allowed(s, ES_COPY, name, "copying")
        reply = await s._click_enabled(MAIN, ES_COPY, "copy emergency stop program")
        return await _answer_name(
            s, reply, new_name, f"copy emergency stop program {name} to {new_name}"
        )
    finally:
        await s.close_screen(MAIN)


async def delete_program(s: _Session, name: str) -> bool:
    """Delete a disarmed program and confirm; not the built-in one.

    Returns
    -------
        True when sent, False in a dry run.

    """
    await _open(s)
    try:
        await _select_program(s, name)
        _allowed(s, ES_DELETE, name, "deleting")
        if _selected_enabled(s.state, name):
            msg = f"{name}: disable the program before deleting it"
            raise ItmCommandError(msg)
        reply = await s.write(f"delete emergency stop program {name}", MAIN, ES_DELETE)
        if reply is None:
            return False
        await s._answer_confirm(MAIN, reply)
        return True
    finally:
        await s.close_screen(MAIN)


# -- a program's settings (202, 203) ---------------------------------------------------


def settings_from_screen(state: UIState, name: str) -> EmergencyStopSettings:
    """Read the open settings screen (202)."""

    def combo(part: int) -> tuple[str | None, tuple[str, ...]]:
        options = tuple(
            html.unescape(str(o)).strip()
            for o in state.get(SETTINGS, part, "COMBO_STRS") or ()
        )
        idx = state.get(SETTINGS, part, "SINGLE_SELIDX")
        shown = options[idx] if idx is not None and 0 <= idx < len(options) else None
        return shown, options

    release_mode, release_options = combo(ST_RELEASE_MODE)
    stop, stop_options = combo(ST_STOP)
    return EmergencyStopSettings(
        name=name.strip(),
        inputs=tuple(n for n in _last_column(state, SETTINGS, ST_IN_LIST) if n),
        outputs=tuple(n for n in _last_column(state, SETTINGS, ST_OUT_LIST) if n),
        release_mode=release_mode,
        release_mode_options=release_options,
        stop=stop,
        stop_options=stop_options,
    )


async def _open_settings(s: _Session, name: str, *, reading: bool) -> None:
    """Select a program and open its settings (202) with Edit.

    An armed program's settings don't open (the iTM offers a read-only mode that
    did not show them): a write refuses it before clicking; a read answers the
    read-only question and raises when 202 still doesn't open.
    """
    await _select_program(s, name)
    _allowed(s, ES_EDIT, name, "editing")
    if not reading and _selected_enabled(s.state, name):
        msg = (
            f"{name}: the iTM only changes a disabled program's settings; "
            "disable it first (that needs allow_safety_change)"
        )
        raise ItmCommandError(msg)
    reply = await s._click_enabled(MAIN, ES_EDIT, "edit emergency stop program")
    for dialog in reply.dialogs():
        if dialog[1] == "CONFIRM":  # "open in read-only mode?"
            await s.send(MAIN, dialog[0])
        elif dialog[1] in {"ERROR", "INFO"}:
            msg = f"{name}: the iTM refused to open the settings: {dialog[2]!r}"
            raise ItmCommandError(msg)
    if SETTINGS not in s.state.screens:
        msg = (
            f"{name}: the settings screen did not open (the iTM does not show an "
            "armed program's settings)"
        )
        raise ItmCommandError(msg)


async def read_program_settings(s: _Session, name: str) -> EmergencyStopSettings:
    """Open a program's settings, read them and cancel.

    Returns
    -------
        The settings as shown.

    """
    await _open(s)
    try:
        await _open_settings(s, name, reading=True)
        return settings_from_screen(s.state, name)
    finally:
        await _leave(s, SETTINGS, ST_CANCEL)
        await s.close_screen(MAIN)


async def _pick_points(s: _Session, button: int, points: tuple[str, ...]) -> None:
    """Make a 202 point list exactly these, through the picker (203), then its OK."""
    await s._click_enabled(SETTINGS, button, "emergency stop points")
    if PICKER not in s.state.screens:
        msg = "the emergency stop point picker did not open"
        raise ItmCommandError(msg)
    wanted = list(dict.fromkeys(p.strip() for p in points))
    for point in _last_column(s.state, PICKER, PK_SELECTED):
        if point and point not in wanted:
            names = _last_column(s.state, PICKER, PK_SELECTED)
            await _select(s, PICKER, PK_SELECTED, _row(names, point, "point"))
            await s._click_enabled(PICKER, PK_REMOVE, "remove emergency stop point")
    for point in wanted:
        if point in _last_column(s.state, PICKER, PK_SELECTED):
            continue
        names = _last_column(s.state, PICKER, PK_AVAILABLE)
        try:
            row = _row(names, point, "point")
        except ItmCommandError as err:
            msg = f"{point!r} is not offered: {err}"
            raise ItmCommandError(msg) from err
        await _select(s, PICKER, PK_AVAILABLE, row)
        await s._click_enabled(PICKER, PK_ADD, "add emergency stop point")
    got = [n for n in _last_column(s.state, PICKER, PK_SELECTED) if n]
    if set(got) != set(wanted):
        msg = f"the points would be {got}, not {wanted}"
        raise ItmCommandError(msg)
    await s.send(PICKER, PK_OK)  # only returns the choice to 202
    s.state.screens.pop(PICKER, None)


def _option(state: UIState, part: int, value: str, what: str) -> int:
    options = [
        html.unescape(str(o)).strip().casefold()
        for o in state.get(SETTINGS, part, "COMBO_STRS") or ()
    ]
    if value.strip().casefold() not in options:
        msg = f"{what} {value!r} is not offered: {options}"
        raise ItmCommandError(msg)
    return options.index(value.strip().casefold())


async def set_program_settings(  # noqa: PLR0913  # pylint: disable=too-many-arguments
    s: _Session,
    name: str,
    *,
    inputs: tuple[str, ...] | None = None,
    outputs: tuple[str, ...] | None = None,
    release_mode: str | None = None,
    stop: str | None = None,
) -> bool:
    """Change a disarmed program's points, release mode and/or stop; OK + Yes.

    Args:
    ----
        s: The open session.
        name: The program.
        inputs: The input points, exactly (None: unchanged).
        outputs: The output points, exactly (None: unchanged).
        release_mode: The release mode, an option as shown (None: unchanged).
        stop: What the stop does, an option as shown (None: unchanged).

    Returns:
    -------
        True when saved, False in a dry run.

    """
    await _open(s)
    try:
        await _open_settings(s, name, reading=False)
        if inputs is not None:
            await _pick_points(s, ST_IN_MODIFY, inputs)
        if outputs is not None:
            await _pick_points(s, ST_OUT_MODIFY, outputs)
        for part, value, what in (
            (ST_RELEASE_MODE, release_mode, "release mode"),
            (ST_STOP, stop, "stop"),
        ):
            if value is not None:
                index = _option(s.state, part, value, what)
                await s._pick_option(SETTINGS, part, index, what)
        return await s.press_ok(
            f"save emergency stop program {name}", SETTINGS, ST_OK, ST_CANCEL
        )
    finally:
        await _leave(s, PICKER, PK_CANCEL)
        await _leave(s, SETTINGS, ST_CANCEL)
        await s.close_screen(MAIN)


# -- active stops and their release (201) ----------------------------------------------


async def _open_release(s: _Session) -> bool:
    """Open the release screen from the main menu bar's indicator (assumed).

    Returns
    -------
        False when no stop is active (the indicator is hidden; it isn't clicked).

    """
    if not s.indicators().emergency_stop:
        return False
    reply = await s.send(ScreenID.MAINMENU, MM_EMERGENCY_STOP)
    if RELEASE not in s.state.screens:
        msg = f"the emergency stop release screen did not open: {reply.dialogs()}"
        raise ItmCommandError(msg)
    return True


async def read_active_stops(s: _Session) -> tuple[str, ...]:
    """Read the programs whose stop is active (the release screen's list).

    Returns
    -------
        Their names; empty when no stop is active.

    """
    try:
        if not await _open_release(s):
            return ()
        return tuple(n for n in _last_column(s.state, RELEASE, RL_LIST) if n)
    finally:
        await _leave(s, RELEASE, RL_CANCEL)


async def release(s: _Session, name: str) -> bool:
    """Release an active stop (select it, Vrijgeven, confirm).

    Returns
    -------
        True when sent, False in a dry run.

    """
    try:
        if not await _open_release(s):
            msg = "no emergency stop is active"
            raise ItmCommandError(msg)
        names = _last_column(s.state, RELEASE, RL_LIST)
        await _select(s, RELEASE, RL_LIST, _row(names, name, "active emergency stop"))
        if s.state.get(RELEASE, RL_RELEASE, "ENABLED") == 0:
            msg = f"{name}: the iTM has the release button disabled"
            raise ItmCommandError(msg)
        reply = await s.write(f"release emergency stop {name}", RELEASE, RL_RELEASE)
        if reply is None:
            return False
        await s._answer_confirm(RELEASE, reply)
        return True
    finally:
        await _leave(s, RELEASE, RL_CANCEL)
