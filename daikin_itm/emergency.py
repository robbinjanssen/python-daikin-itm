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

from ._bases import ClientCore
from ._screens import answer_name as _answer_name
from ._screens import cell_lists as _cells
from ._screens import close as _leave
from ._screens import row_named as _row
from ._util import ignore as _ignore
from .exceptions import ItmCommandError
from .menu import (
    MB_EMERGENCY_STOP,
    MM_EMERGENCY_STOP,
    EmergencyStopProgram,
    MenuScreen,
    emergency_stop_from_screen,
)
from .protocol import ScreenID

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .session import _Session
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


def _last_column(state: UIState, screen: int, part: int) -> list[str]:
    """Return each row's last cell: the name, in the lists of 201-203 (assumed)."""
    return [row[-1] if row else "" for row in _cells(state, screen, part)]


async def _open(s: _Session) -> None:
    if not await s.open_menu_screen(MB_EMERGENCY_STOP, MAIN):
        msg = "the emergency stop screen did not open"
        raise ItmCommandError(msg)


def _names(state: UIState) -> list[str]:
    return [p.name.strip() for p in emergency_stop_from_screen(state)]


async def _select_program(s: _Session, name: str) -> None:
    """Select a program's row on the open program list (200)."""
    await s.select_row(MAIN, ES_LIST, _row(_names(s.state), name, "emergency stop"))


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
            await s.select_row(MAIN, ES_LIST, i)
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
        await s.answer_confirm(MAIN, reply)
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
        reply = await s.click(MAIN, ES_CREATE, "add emergency stop program")
        return await _answer_name(
            s, MAIN, reply, name, f"create emergency stop program {name}"
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
        reply = await s.click(MAIN, ES_RENAME, "rename emergency stop program")
        return await _answer_name(
            s,
            MAIN,
            reply,
            new_name,
            f"rename emergency stop program {name} to {new_name}",
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
        reply = await s.click(MAIN, ES_COPY, "copy emergency stop program")
        return await _answer_name(
            s,
            MAIN,
            reply,
            new_name,
            f"copy emergency stop program {name} to {new_name}",
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
        await s.answer_confirm(MAIN, reply)
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
    reply = await s.click(MAIN, ES_EDIT, "edit emergency stop program")
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
    await s.click(SETTINGS, button, "emergency stop points")
    if PICKER not in s.state.screens:
        msg = "the emergency stop point picker did not open"
        raise ItmCommandError(msg)
    wanted = list(dict.fromkeys(p.strip() for p in points))
    for point in _last_column(s.state, PICKER, PK_SELECTED):
        if point and point not in wanted:
            names = _last_column(s.state, PICKER, PK_SELECTED)
            await s.select_row(PICKER, PK_SELECTED, _row(names, point, "point"))
            await s.click(PICKER, PK_REMOVE, "remove emergency stop point")
    for point in wanted:
        if point in _last_column(s.state, PICKER, PK_SELECTED):
            continue
        names = _last_column(s.state, PICKER, PK_AVAILABLE)
        try:
            row = _row(names, point, "point")
        except ItmCommandError as err:
            msg = f"{point!r} is not offered: {err}"
            raise ItmCommandError(msg) from err
        await s.select_row(PICKER, PK_AVAILABLE, row)
        await s.click(PICKER, PK_ADD, "add emergency stop point")
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
                await s.pick_option(SETTINGS, part, index, what)
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
        await s.select_row(RELEASE, RL_LIST, _row(names, name, "active emergency stop"))
        if s.state.get(RELEASE, RL_RELEASE, "ENABLED") == 0:
            msg = f"{name}: the iTM has the release button disabled"
            raise ItmCommandError(msg)
        reply = await s.write(f"release emergency stop {name}", RELEASE, RL_RELEASE)
        if reply is None:
            return False
        await s.answer_confirm(RELEASE, reply)
        return True
    finally:
        await _leave(s, RELEASE, RL_CANCEL)


class EmergencyMixin(ClientCore):
    """ItmClient's emergency stop methods."""

    async def _emergency_program(self, name: str) -> EmergencyStopProgram | None:
        """Return a program as read in a fresh session (armed as its radios show)."""
        programs = await self._in_session(read_programs)
        return next((p for p in programs if p.name == name.strip()), None)

    async def _emergency_names(self) -> list[str]:
        return [p.name.strip() for p in await self.fetch_emergency_stop()]

    async def fetch_emergency_stop_settings(self, name: str) -> EmergencyStopSettings:
        """Read an emergency stop program's points, release mode and stop.

        The iTM only shows a disarmed program's settings; an armed (or the
        built-in) program's raise ItmCommandError.

        Args:
        ----
            name: The program.

        Returns:
        -------
            The settings as the iTM shows them.

        """
        return await self._in_session(lambda s: read_program_settings(s, name))

    async def set_emergency_stop_enabled(
        self, name: str, *, enabled: bool, allow_safety_change: bool = False
    ) -> EmergencyStopProgram | None:
        """Arm or disarm an emergency stop program.

        Disarming switches a safety function off; arming may start a stop at once
        when the program's input is already in alarm. Refused unless
        ``allow_safety_change`` (a dry run is always allowed).

        Args:
        ----
            name: The program.
            enabled: Arm (True) or disarm.
            allow_safety_change: Really change it.

        Returns:
        -------
            The program as read back in a fresh session, or None in a dry run.

        Raises:
        ------
            ItmCommandError: Not allowed, or the iTM did not take the change.

        """
        self._experimental("enable or disable an emergency stop program")
        if not allow_safety_change and not self.dry_run:
            msg = (
                "arming or disarming an emergency stop program changes a safety "
                "function: pass allow_safety_change=True"
            )
            raise ItmCommandError(msg)
        await self._system_write(
            lambda s: _ignore(set_program_enabled(s, name, enabled=enabled))
        )
        if self.dry_run:
            return None
        program = await self._emergency_program(name)
        if program is None or program.enabled != enabled:
            msg = f"{name}: the iTM did not {'enable' if enabled else 'disable'} it"
            raise ItmCommandError(msg)
        return program

    async def create_emergency_stop_program(
        self, name: str
    ) -> EmergencyStopProgram | None:
        """Add an emergency stop program (needs the emergency stop dealer option).

        Args:
        ----
            name: Its name (1-20 characters on the iTM this was built on).

        Returns:
        -------
            The program as the list shows it, or None in a dry run.

        """
        self._experimental("create an emergency stop program")
        await self._system_write(lambda s: _ignore(create_program(s, name)))
        if self.dry_run:
            return None
        program = await self._emergency_program(name)
        if program is None:
            msg = f"{name}: the iTM did not add the emergency stop program"
            raise ItmCommandError(msg)
        return program

    async def rename_emergency_stop_program(
        self, name: str, new_name: str
    ) -> EmergencyStopProgram | None:
        """Rename an emergency stop program (not the built-in one).

        Args:
        ----
            name: The program.
            new_name: Its new name.

        Returns:
        -------
            The program as the list shows it, or None in a dry run.

        """
        self._experimental("rename an emergency stop program")
        await self._system_write(lambda s: _ignore(rename_program(s, name, new_name)))
        if self.dry_run:
            return None
        program = await self._emergency_program(new_name)
        if program is None or name.strip() in await self._emergency_names():
            msg = f"{name}: the iTM did not rename it to {new_name!r}"
            raise ItmCommandError(msg)
        return program

    async def copy_emergency_stop_program(
        self, name: str, new_name: str
    ) -> EmergencyStopProgram | None:
        """Copy an emergency stop program under a new name (not the built-in one).

        Args:
        ----
            name: The program to copy.
            new_name: The copy's name.

        Returns:
        -------
            The copy as the list shows it, or None in a dry run.

        """
        self._experimental("copy an emergency stop program")
        await self._system_write(lambda s: _ignore(copy_program(s, name, new_name)))
        if self.dry_run:
            return None
        program = await self._emergency_program(new_name)
        if program is None:
            msg = f"{name}: the iTM did not make the copy {new_name!r}"
            raise ItmCommandError(msg)
        return program

    async def delete_emergency_stop_program(self, name: str) -> None:
        """Delete a disarmed emergency stop program (not the built-in one).

        Args:
        ----
            name: The program.

        """
        self._experimental("delete an emergency stop program")
        await self._system_write(lambda s: _ignore(delete_program(s, name)))
        if not self.dry_run and name.strip() in await self._emergency_names():
            msg = f"{name}: the iTM did not delete the emergency stop program"
            raise ItmCommandError(msg)

    async def set_emergency_stop_settings(  # pylint: disable=too-many-arguments
        self,
        name: str,
        *,
        inputs: Iterable[str] | None = None,
        outputs: Iterable[str] | None = None,
        release_mode: str | None = None,
        stop: str | None = None,
    ) -> EmergencyStopSettings | None:
        """Change a disarmed program's input/output points, release mode or stop.

        Args:
        ----
            name: The program (disarmed: the iTM only edits those).
            inputs: The input points, exactly (None: unchanged).
            outputs: The output points, exactly (None: unchanged).
            release_mode: The release mode, an option as the iTM shows it.
            stop: What the stop does, an option as the iTM shows it.

        Returns:
        -------
            The settings as read back in a fresh session, or None in a dry run.

        """
        self._experimental("change an emergency stop program's settings")
        ins = None if inputs is None else tuple(inputs)
        outs = None if outputs is None else tuple(outputs)
        await self._system_write(
            lambda s: _ignore(
                set_program_settings(
                    s,
                    name,
                    inputs=ins,
                    outputs=outs,
                    release_mode=release_mode,
                    stop=stop,
                )
            )
        )
        if self.dry_run:
            return None
        got = await self.fetch_emergency_stop_settings(name)
        wrong = [
            what
            for what, want, have in (
                ("inputs", ins, got.inputs),
                ("outputs", outs, got.outputs),
            )
            if want is not None and set(want) != set(have)
        ] + [
            what
            for what, want, have in (
                ("release mode", release_mode, got.release_mode),
                ("stop", stop, got.stop),
            )
            if want is not None and want.strip().casefold() != (have or "").casefold()
        ]
        if wrong:
            msg = f"{name}: the iTM did not take the {', '.join(wrong)}"
            raise ItmCommandError(msg)
        return got

    async def fetch_active_emergency_stops(self) -> tuple[str, ...]:
        """Read the programs whose emergency stop is active (none: empty).

        Returns
        -------
            Their names, from the release screen.

        """
        return await self._in_session(read_active_stops)

    async def release_emergency_stop(
        self, name: str, *, allow_safety_change: bool = False
    ) -> None:
        """Release an active emergency stop (this library never starts one).

        Releasing restarts equipment a safety input (e.g. a fire alarm) stopped, so
        it is refused unless ``allow_safety_change`` (a dry run is always allowed).

        Args:
        ----
            name: The program whose stop is active.
            allow_safety_change: Really release it.

        Raises:
        ------
            ItmCommandError: Not allowed, or the iTM did not release the stop.

        """
        self._experimental("release an emergency stop")
        if not allow_safety_change and not self.dry_run:
            msg = (
                "releasing an emergency stop restarts equipment a safety input "
                "stopped: pass allow_safety_change=True"
            )
            raise ItmCommandError(msg)
        await self._system_write(lambda s: _ignore(release(s, name)))
        if self.dry_run:
            return
        if name.strip() in await self.fetch_active_emergency_stops():
            msg = f"{name}: the iTM did not release the emergency stop"
            raise ItmCommandError(msg)
