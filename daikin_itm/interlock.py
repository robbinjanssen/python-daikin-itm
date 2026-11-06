# ruff: noqa: SLF001
# pylint: disable=protected-access
"""Interlock programs (900-906): list, enable, add, rename, copy, delete, setup.

Seen on a real iTM with the dealer option switched off (read only): 900 lists
activation and program name, opens empty with Add enabled and Copy, Delete, Rename
and Edit disabled without a selection.

Assumed (not seen on a real iTM, only the web UI's part layout): the output list
(905) selects with a CLICK + INTARRAY; Add, Rename and Copy ask the name in a
CHARINPUT dialog; the input (902) and output (905) screens only return their choice
to the screen that opened them, and only 901's OK saves; an enabled program is
disabled before it is deleted or its setup changed (an enabled program's setup
offers a read-only mode, like the emergency stop's).

Also assumed, not seen on a real iTM (only script.js's part layout; the manual
doesn't describe these screens): the input setup's (902) condition button
(BTN_CONDITION_SETUP) opens the condition setup (903) of the input row selected
in its list. 903 has four tabs (on/off: two radios; operation mode: a combo;
analog value: a comparison radio pair, a constant or a management point, an
offset and a hysteresis; multistate value: a combo); the parts that don't apply to
the point are disabled. Its management point button opens the point picker (904):
one single-select list of management points (not the inputs or outputs), whose OK
returns the choice to 903. The output setup's (905) action button
(BTN_ACTION_SETUP) opens the action setup (906) of the output row selected in its
list; 906 has the part IDs of a schedule event's screen (109): a setting is part
of the action only when its checkbox is ticked. The OK of 903, 904 and 906 only
returns to the screen that opened them; only 901's OK saves. A radio is clicked
without data, a checkbox and a combo with CLICK + INT, and a tab is selected with
TAB_SELECT before its parts are used. A part is refused only when it is reported
disabled (ENABLED 0), never because it is hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ._bases import ClientCore
from ._screens import activation as _activation
from ._screens import answer_name as _answer_name
from ._screens import cells as _cells
from ._screens import checked_index as _checked_part
from ._screens import clean as _clean
from ._screens import click as _click
from ._screens import ensure_enabled as _enabled
from ._screens import leave as _leave
from ._screens import open_screen as _open
from ._screens import part_text as _text
from ._screens import press as _press
from ._screens import radio as _radio
from ._screens import return_choice as _return_choice
from ._screens import row_holding as _row_holding
from ._screens import row_named as _row
from ._screens import select as _select
from ._screens import ticked as _ticked
from ._util import check_wrong as _check_wrong
from ._util import ignore as _discard
from .dealer_functions import same_point, same_temperature, temperature
from .exceptions import ItmCommandError
from .lang import mode_from_label
from .menu import (
    EVT_FAN,
    EVT_FAN_ICON,
    EVT_LOCK_MODE,
    EVT_LOCK_SETPOINT,
    EVT_LOCK_START_STOP,
    EVT_MODE,
    EVT_MODE_COMBO,
    EVT_POWER,
    EVT_SETPOINT,
    EVT_SETPOINT_FIXED,
    EVT_SETPOINT_TEXT,
    MB_INTERLOCK,
    MenuScreen,
)
from .models import FAN_ICONS, PowerState, RemoteLock
from .protocol import DataType, EventType
from .schedule_edit import EVT_SETPOINT_BUTTON, LOCK_ORDER, POWER_RADIOS

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .session import _Session
    from .state import UIState


# Interlock (900): the program list.
I_LIST, I_ENABLE, I_DISABLE, I_CREATE, I_COPY = 1, 3, 4, 6, 7
I_DELETE, I_RENAME, I_EDIT, I_CLOSE = 8, 9, 10, 11
# A program's setup (901): inputs, and two output tabs.
INTERLOCK_SETUP, INTERLOCK_INPUTS, INTERLOCK_OUTPUTS = 901, 902, 905
P_INPUT_BUTTON, P_INPUTS, P_TAB, P_OK, P_CANCEL = 2, 3, 4, 11, 12
# Per output tab: condition combo, outputs button, outputs list, interval, its button.
P_OUTPUT_TABS = ((5, 6, 7, 9, 10), (14, 15, 16, 18, 19))
# The input setup (902) and output setup (905).
N_INPUTS, N_ADD, N_REMOVE, N_AVAILABLE, N_CONDITION, N_OK, N_CANCEL = (
    2,
    6,
    7,
    10,
    12,
    13,
    14,
)
O_EVENTS, O_ADD, O_REMOVE, O_AVAILABLE, O_OK, O_CANCEL = 2, 8, 9, 12, 13, 14
# The buttons opening the condition setup (902 -> 903) and action setup (905 -> 906).
N_CONDITION_BUTTON, O_ACTION_BUTTON = 4, 6
# Condition setup (903), management point picker (904), action setup (906).
INTERLOCK_CONDITION, INTERLOCK_POINTS, INTERLOCK_ACTION = 903, 904, 906
C_OK, C_CANCEL, C_TAB = 20, 21, 27
C_TAB_STATE, C_TAB_MODE, C_TAB_ANALOG, C_TAB_MULTISTATE = 0, 1, 2, 3
C_STATES = (28, 29)  # tab 1: the two on/off conditions
C_MODE = 32  # tab 2: the operation mode
C_VALUE, C_COMPARE = 2, (3, 4)  # tab 3: the input's value; ">" / "<" (as labelled)
C_CONSTANT_RADIO, C_CONSTANT, C_CONSTANT_BUTTON = 6, 7, 8
C_POINT_RADIO, C_POINT, C_POINT_BUTTON = 9, 10, 11
C_OFFSET, C_OFFSET_BUTTON, C_HYSTERESIS, C_HYSTERESIS_BUTTON = 14, 15, 17, 18
C_MULTISTATE = 35  # tab 4: the multistate value
MP_LIST, MP_OK, MP_CANCEL = 1, 2, 3
A_TAB, A_OK, A_CANCEL = 1, 5, 6
A_TAB_POWER, A_TAB_LOCKS, A_TAB_SETTINGS = 0, 1, 2
_TWO_WAY = (RemoteLock.ALLOWED, RemoteLock.FORBIDDEN)  # the mode and setpoint locks


@dataclass(frozen=True)
class InterlockProgram:
    """An interlock program and whether it is enabled."""

    name: str
    enabled: bool


@dataclass(frozen=True)
class InterlockCondition:  # pylint: disable=too-many-instance-attributes
    """The condition of one input point (903), as shown; None: not shown."""

    point: tuple[str, ...]  # the input's row in the input setup (902)
    tabs: tuple[str, ...] = ()
    state: str | None = None  # on/off tab: the checked condition's label
    state_options: tuple[str, ...] = ()
    mode: str | None = None  # operation mode tab, as shown
    mode_options: tuple[str, ...] = ()
    value: str | None = None  # analog tab: the input's value
    comparison: str | None = None  # analog tab: the checked comparison's label
    constant: str | None = None  # compared with a constant (when chosen)
    reference_point: str | None = None  # or with a management point (when chosen)
    reference_points: tuple[str, ...] = ()  # the points the picker (904) offers
    offset: str | None = None
    hysteresis: str | None = None
    multistate: str | None = None  # multistate tab, as shown
    multistate_options: tuple[str, ...] = ()


@dataclass(frozen=True)
class InterlockAction:  # pylint: disable=too-many-instance-attributes
    """What an output point is set to when the program fires (906).

    A setting is None when its checkbox isn't ticked (not part of the action).
    """

    point: tuple[str, ...]  # the output's row in the output setup (905)
    power: PowerState | None = None
    lock_start_stop: RemoteLock | None = None
    lock_mode: RemoteLock | None = None
    lock_setpoint: RemoteLock | None = None
    mode: str | None = None  # as shown
    setpoint: float | None = None  # °C
    fan_speed: str | None = None


@dataclass(frozen=True)
class InterlockOutput:
    """One output tab of an interlock program's setup (901)."""

    condition: str | None  # the input condition that drives these outputs, shown
    condition_options: tuple[str, ...]
    outputs: tuple[tuple[str, ...], ...]  # the rows as listed
    interval: str | None  # as shown
    events: tuple[tuple[str, ...], ...] = ()  # the output setup's rows (details)
    actions: tuple[InterlockAction, ...] = ()  # per output point (details)


@dataclass(frozen=True)
class InterlockSetup:
    """An interlock program's setup (901), as the iTM shows it."""

    name: str
    inputs: tuple[tuple[str, ...], ...]  # the rows as listed
    outputs: tuple[InterlockOutput, ...]  # the two output tabs
    input_conditions: tuple[str, ...] = ()  # the input setup's options (details)
    conditions: tuple[InterlockCondition, ...] = ()  # per input point (details)


@dataclass(frozen=True)
class InterlockConditionSpec:  # pylint: disable=too-many-instance-attributes
    """A change to the condition of one input point (903); None: unchanged.

    Labels and options are matched as the iTM shows them (any case); ``mode`` may
    also be a mode key (heat, cool, fan, auto, ...). Compare with either a constant
    or a management point (as the picker 904 lists it), not both.
    """

    point: str  # the input point, as listed in the input setup
    state: str | None = None
    mode: str | None = None
    comparison: str | None = None
    constant: float | None = None
    reference_point: str | None = None
    offset: float | None = None
    hysteresis: float | None = None
    multistate: str | None = None

    def __post_init__(self) -> None:
        """Refuse a constant and a management point together."""
        if self.constant is not None and self.reference_point is not None:
            msg = "compare with a constant or a management point, not both"
            raise ValueError(msg)

    @property
    def analog(self) -> bool:
        """Tell whether the spec changes the analog value tab."""
        return any(
            v is not None
            for v in (
                self.comparison,
                self.constant,
                self.reference_point,
                self.offset,
                self.hysteresis,
            )
        )


@dataclass(frozen=True)
class InterlockActionSpec:
    """A change to the action of one output point (906); None: unchanged.

    ``mode`` is an option as shown or a mode key; ``setpoint`` a fixed setpoint, °C.
    """

    point: str  # the output point, as listed in the output setup
    power: PowerState | None = None  # ON, OFF, SETBACK_HIGH or SETBACK_LOW
    mode: str | None = None
    setpoint: float | None = None
    lock_start_stop: RemoteLock | None = None  # ALLOWED, STOP_ONLY or FORBIDDEN
    lock_mode: RemoteLock | None = None  # ALLOWED or FORBIDDEN
    lock_setpoint: RemoteLock | None = None  # ALLOWED or FORBIDDEN

    def __post_init__(self) -> None:
        """Refuse what the action screen can't take, before anything is sent."""
        if self.power is not None and self.power not in POWER_RADIOS:
            msg = f"power must be one of {list(POWER_RADIOS)}, not {self.power}"
            raise ValueError(msg)
        for lock, choices in (
            (self.lock_start_stop, LOCK_ORDER),
            (self.lock_mode, _TWO_WAY),
            (self.lock_setpoint, _TWO_WAY),
        ):
            if lock is not None and lock not in choices:
                msg = f"a lock must be one of {list(choices)}, not {lock}"
                raise ValueError(msg)


@dataclass(frozen=True)
class InterlockOutputSpec:
    """A change to one output tab of an interlock program (None: unchanged)."""

    condition: str | None = None  # an option as shown
    outputs: tuple[str, ...] | None = None  # the output points, exactly
    interval: float | None = None  # as the iTM's number input takes it
    actions: tuple[InterlockActionSpec, ...] = ()


def interlocks_from_screen(state: UIState) -> list[InterlockProgram]:
    """Read the interlock screen's program list (900)."""
    s = MenuScreen.INTERLOCK
    return [
        InterlockProgram(name=row[1], enabled=_activation(state, s, I_DISABLE, row[0]))
        for row in _cells(state, s, I_LIST)
        if len(row) >= 2
    ]


def _combo(
    state: UIState, screen: int, part: int
) -> tuple[str | None, tuple[str, ...]]:
    options = tuple(_clean(o) for o in state.get(screen, part, "COMBO_STRS") or ())
    idx = state.get(screen, part, "SINGLE_SELIDX")
    shown = options[idx] if idx is not None and 0 <= idx < len(options) else None
    return shown, options


def interlock_setup_from_screen(state: UIState, name: str) -> InterlockSetup:
    """Read an open interlock program setup screen (901)."""
    s = INTERLOCK_SETUP
    outputs = []
    for condition, _button, rows, interval, _change in P_OUTPUT_TABS:
        shown, options = _combo(state, s, condition)
        outputs.append(
            InterlockOutput(
                condition=shown,
                condition_options=options,
                outputs=tuple(r for r in _cells(state, s, rows) if any(r)),
                interval=_text(state, s, interval),
            )
        )
    return InterlockSetup(
        name=name.strip(),
        inputs=tuple(r for r in _cells(state, s, P_INPUTS) if any(r)),
        outputs=tuple(outputs),
    )


def _label(state: UIState, screen: int, part: int) -> str:
    return _clean(state.get(screen, part, "LABEL") or "")


def _tabs_shown(state: UIState, screen: int, part: int) -> tuple[str, ...]:
    """Return the names of a tab control's tabs that aren't hidden."""
    names = [_clean(n) for n in state.get(screen, part, "TAB_NAME") or []]
    value = state.get(screen, part, "TAB_VISIBLE")
    flags = value[1] if isinstance(value, list) and len(value) > 1 else []
    return tuple(
        n for i, n in enumerate(names) if not (i < len(flags) and flags[i] == 0)
    )


def interlock_condition_from_screen(
    state: UIState, point: tuple[str, ...]
) -> InterlockCondition:
    """Read an open condition setup screen (903) of an input point."""
    c = INTERLOCK_CONDITION
    labels = tuple(_label(state, c, p) for p in C_STATES)
    checked = _checked_part(state, c, C_STATES)
    compare = _checked_part(state, c, C_COMPARE)
    mode, modes = _combo(state, c, C_MODE)
    multistate, multistates = _combo(state, c, C_MULTISTATE)
    return InterlockCondition(
        point=point,
        tabs=_tabs_shown(state, c, C_TAB),
        state=None if checked is None else labels[checked] or None,
        state_options=tuple(label for label in labels if label),
        mode=mode,
        mode_options=modes,
        value=_text(state, c, C_VALUE),
        comparison=None
        if compare is None
        else _label(state, c, C_COMPARE[compare]) or None,
        constant=_text(state, c, C_CONSTANT)
        if _ticked(state, c, C_CONSTANT_RADIO)
        else None,
        reference_point=_text(state, c, C_POINT)
        if _ticked(state, c, C_POINT_RADIO)
        else None,
        offset=_text(state, c, C_OFFSET),
        hysteresis=_text(state, c, C_HYSTERESIS),
        multistate=multistate,
        multistate_options=multistates,
    )


def _action_lock(
    state: UIState, group: tuple[int, tuple[int, ...]]
) -> RemoteLock | None:
    checkbox, radios = group
    if not _ticked(state, INTERLOCK_ACTION, checkbox):
        return None
    i = _checked_part(state, INTERLOCK_ACTION, radios)
    order = LOCK_ORDER if len(radios) == len(LOCK_ORDER) else _TWO_WAY
    return RemoteLock.UNKNOWN if i is None else order[i]


def interlock_action_from_screen(
    state: UIState, point: tuple[str, ...]
) -> InterlockAction:
    """Read an open action setup screen (906) of an output point."""
    a = INTERLOCK_ACTION
    power = None
    if _ticked(state, a, EVT_POWER[0]):
        i = _checked_part(state, a, EVT_POWER[1])
        by_radio = {part: p for p, part in POWER_RADIOS.items()}
        power = None if i is None else by_radio[EVT_POWER[1][i]]
    ticked = {p: _ticked(state, a, p) for p in (EVT_MODE, EVT_SETPOINT, EVT_FAN)}
    return InterlockAction(
        point=point,
        power=power,
        lock_start_stop=_action_lock(state, EVT_LOCK_START_STOP),
        lock_mode=_action_lock(state, EVT_LOCK_MODE),
        lock_setpoint=_action_lock(state, EVT_LOCK_SETPOINT),
        mode=_combo(state, a, EVT_MODE_COMBO)[0] if ticked[EVT_MODE] else None,
        setpoint=temperature(_text(state, a, EVT_SETPOINT_TEXT))
        if ticked[EVT_SETPOINT]
        else None,
        fan_speed=FAN_ICONS.get(state.get(a, EVT_FAN_ICON, "ICONID"))
        if ticked[EVT_FAN]
        else None,
    )


async def _pick_rows(  # noqa: PLR0913  # pylint: disable=too-many-arguments,too-many-locals
    s: _Session,
    screen: int,
    lists: tuple[int, int],
    buttons: tuple[int, int],
    wanted: tuple[str, ...],
    *,
    what: str,
) -> None:
    """Make a picker's chosen list hold exactly the wanted names.

    A row counts as a name when one of its cells shows it (the column layout of
    these lists has not been seen).

    Args:
    ----
        s: The open session.
        screen: The picker.
        lists: Its chosen and available list parts.
        buttons: Its add and remove buttons.
        wanted: The names, exactly.
        what: For errors.

    """
    chosen, available = lists
    add, remove = buttons
    names = list(dict.fromkeys(w.strip() for w in wanted))
    while True:
        rows = _cells(s.state, screen, chosen)
        extra = [i for i, r in enumerate(rows) if any(r) and not set(r) & set(names)]
        if not extra:
            break
        await _select(s, screen, chosen, extra[0])
        _enabled(s, screen, remove, f"remove {what}")
        await s.send(screen, remove)
        if len(_cells(s.state, screen, chosen)) == len(rows):
            msg = f"{what}: the iTM did not remove {' '.join(rows[extra[0]])!r}"
            raise ItmCommandError(msg)
    for name in names:
        if any(name in r for r in _cells(s.state, screen, chosen)):
            continue
        rows = _cells(s.state, screen, available)
        try:
            row = _row_holding(rows, name, what)
        except ItmCommandError as err:
            msg = f"{name!r} is not offered: {err}"
            raise ItmCommandError(msg) from err
        await _select(s, screen, available, row)
        _enabled(s, screen, add, f"add {what}")
        await s.send(screen, add)
    got = _cells(s.state, screen, chosen)
    missing = [n for n in names if not any(n in r for r in got)]
    if missing or len([r for r in got if any(r)]) != len(names):
        msg = f"{what}: the list would be {[' '.join(r) for r in got]}, not {names}"
        raise ItmCommandError(msg)


async def _choose(s: _Session, screen: int, part: int, index: int, what: str) -> None:
    """Pick a combo option or set a checkbox (1 ticked), unless it is disabled."""
    _enabled(s, screen, part, what)
    await s.send(screen, part, EventType.CLICK, DataType.INT, index)


async def _tick(s: _Session, screen: int, part: int, what: str) -> None:
    """Tick a checkbox, unless it already is."""
    if not _ticked(s.state, screen, part):
        await _choose(s, screen, part, 1, what)


async def _tab_of(s: _Session, screen: int, part: int, tab: int) -> None:
    await s.send(screen, part, EventType.TAB_SELECT, DataType.INT, tab)


async def _enter(
    s: _Session, screen: int, button: int, value: float | None, what: str
) -> None:
    """Enter a number with a "change" button, unless value is None."""
    if value is None:
        return
    _enabled(s, screen, button, what)
    await s.answer_number(button, value, what, screen=screen)


async def _open_below(
    s: _Session, screen: int, button: int, sub: int, what: str
) -> None:
    """Open a sub-screen with a button of the screen above it."""
    _enabled(s, screen, button, what)
    await s.send(screen, button)
    if sub not in s.state.screens:
        msg = f"the {what} screen did not open"
        raise ItmCommandError(msg)


def _option(  # noqa: PLR0913  # pylint: disable=too-many-arguments
    state: UIState, screen: int, part: int, value: str, what: str, *, modes: bool
) -> int:
    """Return the index of a combo option shown as value (or, modes: its mode key)."""
    _shown, options = _combo(state, screen, part)
    wanted = value.strip().casefold()
    for i, option in enumerate(options):
        if option.casefold() == wanted or (modes and mode_from_label(option) == wanted):
            return i
    msg = f"{what}: {value!r} is not offered: {options}"
    raise ItmCommandError(msg)


def _labelled(
    state: UIState, screen: int, parts: tuple[int, ...], value: str, what: str
) -> int:
    """Return the radio among parts labelled value (any case)."""
    labels = [_label(state, screen, p) for p in parts]
    folded = [label.casefold() for label in labels]
    if value.strip().casefold() not in folded:
        msg = f"{what}: {value!r} is not offered: {labels}"
        raise ItmCommandError(msg)
    return parts[folded.index(value.strip().casefold())]


INTERLOCK = MenuScreen.INTERLOCK
IL_WHAT = "interlock program"


async def _open_interlock(s: _Session) -> None:
    await _open(s, MB_INTERLOCK, INTERLOCK, "interlock")


def _interlock_names(state: UIState) -> list[str]:
    return [p.name for p in interlocks_from_screen(state)]


async def _select_interlock(s: _Session, name: str) -> None:
    await _select(s, INTERLOCK, I_LIST, _row(_interlock_names(s.state), name, IL_WHAT))


def _interlock_enabled(s: _Session, name: str) -> bool:
    shown = _radio(s.state, INTERLOCK, I_ENABLE, I_DISABLE)
    if shown is not None:
        return shown
    listed = {p.name: p.enabled for p in interlocks_from_screen(s.state)}
    return listed.get(name.strip(), False)


def _interlock_new(s: _Session, name: str) -> None:
    if name.strip() in _interlock_names(s.state):
        msg = f"{IL_WHAT} {name!r} exists already"
        raise ItmCommandError(msg)


async def read_interlocks(s: _Session) -> list[InterlockProgram]:
    """Read the interlock programs, each one's activation as its radios show it.

    Returns
    -------
        The programs, in list order.

    """
    await _open_interlock(s)
    try:
        out = []
        for i, program in enumerate(interlocks_from_screen(s.state)):
            await _select(s, INTERLOCK, I_LIST, i)
            shown = _radio(s.state, INTERLOCK, I_ENABLE, I_DISABLE)
            out.append(program if shown is None else replace(program, enabled=shown))
        return out
    finally:
        await s.close_screen(INTERLOCK)


async def set_interlock_enabled(s: _Session, name: str, *, enabled: bool) -> bool:
    """Enable or disable an interlock program.

    Returns
    -------
        True when sent (or already so), False in a dry run.

    """
    s._writable()
    await _open_interlock(s)
    try:
        await _select_interlock(s, name)
        if _interlock_enabled(s, name) is enabled:
            return True
        verb = "enable" if enabled else "disable"
        part = I_ENABLE if enabled else I_DISABLE
        return await _press(s, f"{verb} {IL_WHAT} {name}", INTERLOCK, part)
    finally:
        await s.close_screen(INTERLOCK)


async def create_interlock(s: _Session, name: str) -> bool:
    """Add an interlock program and give it its name.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    await _open_interlock(s)
    try:
        _interlock_new(s, name)
        _enabled(s, INTERLOCK, I_CREATE, f"add {IL_WHAT}")
        reply = await s.send(INTERLOCK, I_CREATE)
        return await _answer_name(s, INTERLOCK, reply, name, f"create {IL_WHAT} {name}")
    finally:
        await s.close_screen(INTERLOCK)


async def _named_copy(s: _Session, name: str, new_name: str, *, copy: bool) -> bool:
    s._writable()
    await _open_interlock(s)
    try:
        _interlock_new(s, new_name)
        await _select_interlock(s, name)
        verb = "copy" if copy else "rename"
        part = I_COPY if copy else I_RENAME
        _enabled(s, INTERLOCK, part, f"{verb} {IL_WHAT} {name}")
        reply = await s.send(INTERLOCK, part)
        label = f"{verb} {IL_WHAT} {name} to {new_name}"
        return await _answer_name(s, INTERLOCK, reply, new_name, label)
    finally:
        await s.close_screen(INTERLOCK)


async def rename_interlock(s: _Session, name: str, new_name: str) -> bool:
    """Rename an interlock program.

    Returns
    -------
        True when sent, False in a dry run.

    """
    return await _named_copy(s, name, new_name, copy=False)


async def copy_interlock(s: _Session, name: str, new_name: str) -> bool:
    """Copy an interlock program under a new name.

    Returns
    -------
        True when sent, False in a dry run.

    """
    return await _named_copy(s, name, new_name, copy=True)


async def delete_interlock(s: _Session, name: str) -> bool:
    """Delete a disabled interlock program and confirm.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    await _open_interlock(s)
    try:
        await _select_interlock(s, name)
        if _interlock_enabled(s, name):
            msg = f"{name}: disable the {IL_WHAT} before deleting it"
            raise ItmCommandError(msg)
        return await _press(s, f"delete {IL_WHAT} {name}", INTERLOCK, I_DELETE)
    finally:
        await s.close_screen(INTERLOCK)


async def _open_setup(s: _Session, name: str, *, reading: bool) -> None:
    """Select a program and open its setup (901) with Edit.

    An enabled program's setup is assumed to open only in a read-only mode, like
    the emergency stop's: a write refuses it, a read answers the question Yes.
    """
    await _select_interlock(s, name)
    if not reading and _interlock_enabled(s, name):
        msg = f"{name}: disable the {IL_WHAT} before changing its setup"
        raise ItmCommandError(msg)
    _enabled(s, INTERLOCK, I_EDIT, f"edit {IL_WHAT} {name}")
    reply = await s.send(INTERLOCK, I_EDIT)
    for dialog in reply.dialogs():
        if dialog[1] == "CONFIRM":
            await s.send(INTERLOCK, dialog[0])
        elif dialog[1] in {"ERROR", "INFO"}:
            msg = f"{name}: the iTM refused to open the setup: {dialog[2]!r}"
            raise ItmCommandError(msg)
    if INTERLOCK_SETUP not in s.state.screens:
        msg = f"{name}: the interlock setup screen did not open"
        raise ItmCommandError(msg)


async def _tab(s: _Session, tab: int) -> None:
    await s.send(INTERLOCK_SETUP, P_TAB, EventType.TAB_SELECT, DataType.INT, tab)


async def _open_from_setup(s: _Session, button: int, screen: int, what: str) -> None:
    _enabled(s, INTERLOCK_SETUP, button, what)
    await s.send(INTERLOCK_SETUP, button)
    if screen not in s.state.screens:
        msg = f"the {what} screen did not open"
        raise ItmCommandError(msg)


async def read_interlock_setup(
    s: _Session, name: str, *, details: bool = False
) -> InterlockSetup:
    """Open a program's setup (901), read it and cancel.

    With details the input setup (902, for its condition options) and each output
    tab's output setup (905, for its rows) are opened and cancelled too.

    Returns
    -------
        The setup as shown.

    """
    await _open_interlock(s)
    try:
        await _open_setup(s, name, reading=True)
        setup = interlock_setup_from_screen(s.state, name)
        if not details:
            return setup
        await _open_from_setup(s, P_INPUT_BUTTON, INTERLOCK_INPUTS, "interlock input")
        _shown, conditions = _combo(s.state, INTERLOCK_INPUTS, N_CONDITION)
        found = await _read_conditions(s)
        await _leave(s, INTERLOCK_INPUTS, N_CANCEL)
        outputs = []
        for tab, output in enumerate(setup.outputs):
            await _tab(s, tab)
            button = P_OUTPUT_TABS[tab][1]
            await _open_from_setup(s, button, INTERLOCK_OUTPUTS, "interlock output")
            events = tuple(
                r for r in _cells(s.state, INTERLOCK_OUTPUTS, O_EVENTS) if any(r)
            )
            actions = await _read_actions(s)
            await _leave(s, INTERLOCK_OUTPUTS, O_CANCEL)
            outputs.append(replace(output, events=events, actions=actions))
        return replace(
            setup,
            outputs=tuple(outputs),
            input_conditions=conditions,
            conditions=found,
        )
    finally:
        await _leave_interlock(s)


async def _leave_interlock(s: _Session) -> None:
    """Cancel every interlock sub-screen still open, deepest first; close 900."""
    await _leave(s, INTERLOCK_POINTS, MP_CANCEL)
    await _leave(s, INTERLOCK_CONDITION, C_CANCEL)
    await _leave(s, INTERLOCK_ACTION, A_CANCEL)
    await _leave(s, INTERLOCK_OUTPUTS, O_CANCEL)
    await _leave(s, INTERLOCK_INPUTS, N_CANCEL)
    await _leave(s, INTERLOCK_SETUP, P_CANCEL)
    await s.close_screen(INTERLOCK)


def _usable_button(s: _Session, screen: int, part: int) -> bool:
    return bool(s.state.get(screen, part, "ENABLED") != 0)


async def _read_conditions(s: _Session) -> tuple[InterlockCondition, ...]:
    """Open the condition setup (903) of each input row, read it and cancel.

    An input whose condition button stays disabled after its row is selected is
    left out (the button is never clicked then). With a management point compared,
    the point picker (904) is opened for the points it offers, and cancelled.
    """
    c, found = INTERLOCK_CONDITION, []
    for row, cells in enumerate(_cells(s.state, INTERLOCK_INPUTS, N_INPUTS)):
        if not any(cells):
            continue
        await _select(s, INTERLOCK_INPUTS, N_INPUTS, row)
        if not _usable_button(s, INTERLOCK_INPUTS, N_CONDITION_BUTTON):
            continue
        what = "interlock condition"
        await _open_below(s, INTERLOCK_INPUTS, N_CONDITION_BUTTON, c, what)
        condition = interlock_condition_from_screen(s.state, cells)
        if _ticked(s.state, c, C_POINT_RADIO) and _usable_button(s, c, C_POINT_BUTTON):
            await _tab_of(s, c, C_TAB, C_TAB_ANALOG)
            await _open_below(
                s, c, C_POINT_BUTTON, INTERLOCK_POINTS, "management point"
            )
            offered = tuple(
                " ".join(x for x in r if x)
                for r in _cells(s.state, INTERLOCK_POINTS, MP_LIST)
                if any(r)
            )
            await _leave(s, INTERLOCK_POINTS, MP_CANCEL)
            condition = replace(condition, reference_points=offered)
        await _leave(s, c, C_CANCEL)
        found.append(condition)
    return tuple(found)


async def _read_actions(s: _Session) -> tuple[InterlockAction, ...]:
    """Open the action setup (906) of each output row, read it and cancel.

    An output whose action button stays disabled after its row is selected is
    left out.
    """
    found = []
    for row, cells in enumerate(_cells(s.state, INTERLOCK_OUTPUTS, O_EVENTS)):
        if not any(cells):
            continue
        await _select(s, INTERLOCK_OUTPUTS, O_EVENTS, row)
        if not _usable_button(s, INTERLOCK_OUTPUTS, O_ACTION_BUTTON):
            continue
        await _open_below(
            s, INTERLOCK_OUTPUTS, O_ACTION_BUTTON, INTERLOCK_ACTION, "interlock action"
        )
        found.append(interlock_action_from_screen(s.state, cells))
        await _leave(s, INTERLOCK_ACTION, A_CANCEL)
    return tuple(found)


async def _set_analog(s: _Session, spec: InterlockConditionSpec, what: str) -> None:
    """Set the analog value tab of the open condition setup (903)."""
    c = INTERLOCK_CONDITION
    await _tab_of(s, c, C_TAB, C_TAB_ANALOG)
    if spec.comparison is not None:
        part = _labelled(s.state, c, C_COMPARE, spec.comparison, f"{what} comparison")
        await _click(s, c, part, f"{what} comparison")
    if spec.constant is not None:
        await _click(s, c, C_CONSTANT_RADIO, f"{what} constant")
        await _enter(s, c, C_CONSTANT_BUTTON, spec.constant, f"{what} constant")
    if spec.reference_point is not None:
        point = f"{what} management point"
        await _click(s, c, C_POINT_RADIO, point)
        await _open_below(s, c, C_POINT_BUTTON, INTERLOCK_POINTS, point)
        rows = _cells(s.state, INTERLOCK_POINTS, MP_LIST)
        row = _row_holding(rows, spec.reference_point, "management point")
        await _select(s, INTERLOCK_POINTS, MP_LIST, row)
        await _return_choice(s, INTERLOCK_POINTS, MP_OK, point)
    await _enter(s, c, C_OFFSET_BUTTON, spec.offset, f"{what} offset")
    await _enter(s, c, C_HYSTERESIS_BUTTON, spec.hysteresis, f"{what} hysteresis")


async def _set_condition(s: _Session, spec: InterlockConditionSpec) -> None:
    """Select an input in the open input setup (902) and set its condition (903)."""
    what = f"condition of {spec.point}"
    rows = _cells(s.state, INTERLOCK_INPUTS, N_INPUTS)
    row = _row_holding(rows, spec.point, "interlock input")
    await _select(s, INTERLOCK_INPUTS, N_INPUTS, row)
    c = INTERLOCK_CONDITION
    await _open_below(s, INTERLOCK_INPUTS, N_CONDITION_BUTTON, c, what)
    if spec.state is not None:
        await _tab_of(s, c, C_TAB, C_TAB_STATE)
        part = _labelled(s.state, c, C_STATES, spec.state, f"{what} state")
        await _click(s, c, part, f"{what} state")
    for value, tab, combo, label, modes in (
        (spec.mode, C_TAB_MODE, C_MODE, "mode", True),
        (spec.multistate, C_TAB_MULTISTATE, C_MULTISTATE, "multistate", False),
    ):
        if value is None:
            continue
        await _tab_of(s, c, C_TAB, tab)
        index = _option(s.state, c, combo, value, f"{what} {label}", modes=modes)
        await _choose(s, c, combo, index, f"{what} {label}")
    if spec.analog:
        await _set_analog(s, spec, what)
    await _return_choice(s, c, C_OK, what)


async def _set_action(s: _Session, spec: InterlockActionSpec) -> None:
    """Select an output in the open output setup (905) and set its action (906)."""
    what = f"action of {spec.point}"
    rows = _cells(s.state, INTERLOCK_OUTPUTS, O_EVENTS)
    row = _row_holding(rows, spec.point, "interlock output")
    await _select(s, INTERLOCK_OUTPUTS, O_EVENTS, row)
    a = INTERLOCK_ACTION
    await _open_below(s, INTERLOCK_OUTPUTS, O_ACTION_BUTTON, a, what)
    if spec.power is not None:
        await _tab_of(s, a, A_TAB, A_TAB_POWER)
        await _tick(s, a, EVT_POWER[0], f"{what} power")
        await _click(s, a, POWER_RADIOS[spec.power], f"{what} power {spec.power}")
    locks = (
        (EVT_LOCK_START_STOP, spec.lock_start_stop, "start/stop lock"),
        (EVT_LOCK_MODE, spec.lock_mode, "mode lock"),
        (EVT_LOCK_SETPOINT, spec.lock_setpoint, "setpoint lock"),
    )
    if any(lock is not None for _group, lock, _what in locks):
        await _tab_of(s, a, A_TAB, A_TAB_LOCKS)
    for (checkbox, radios), lock, label in locks:
        if lock is None:
            continue
        await _tick(s, a, checkbox, f"{what} {label}")
        order = LOCK_ORDER if len(radios) == len(LOCK_ORDER) else _TWO_WAY
        await _click(s, a, radios[order.index(lock)], f"{what} {label} {lock}")
    if spec.mode is not None or spec.setpoint is not None:
        await _tab_of(s, a, A_TAB, A_TAB_SETTINGS)
    if spec.mode is not None:
        await _tick(s, a, EVT_MODE, f"{what} mode")
        index = _option(
            s.state, a, EVT_MODE_COMBO, spec.mode, f"{what} mode", modes=True
        )
        await _choose(s, a, EVT_MODE_COMBO, index, f"{what} mode")
    if spec.setpoint is not None:
        await _tick(s, a, EVT_SETPOINT, f"{what} setpoint")
        await _click(s, a, EVT_SETPOINT_FIXED, f"{what} setpoint")
        await _enter(s, a, EVT_SETPOINT_BUTTON, spec.setpoint, f"{what} setpoint")
    await _return_choice(s, a, A_OK, what)


async def _set_output(s: _Session, tab: int, spec: InterlockOutputSpec) -> None:
    condition, button, _rows, _interval, change = P_OUTPUT_TABS[tab]
    await _tab(s, tab)
    what = f"interlock output {tab + 1}"
    if spec.condition is not None:
        _shown, options = _combo(s.state, INTERLOCK_SETUP, condition)
        folded = [o.casefold() for o in options]
        if spec.condition.strip().casefold() not in folded:
            msg = f"{what}: condition {spec.condition!r} is not offered: {options}"
            raise ItmCommandError(msg)
        index = folded.index(spec.condition.strip().casefold())
        await s._pick_option(INTERLOCK_SETUP, condition, index, f"{what} condition")
    if spec.outputs is not None or spec.actions:
        await _open_from_setup(s, button, INTERLOCK_OUTPUTS, what)
        if spec.outputs is not None:
            await _pick_rows(
                s,
                INTERLOCK_OUTPUTS,
                (O_EVENTS, O_AVAILABLE),
                (O_ADD, O_REMOVE),
                spec.outputs,
                what=what,
            )
        for action in spec.actions:
            await _set_action(s, action)
        await _return_choice(s, INTERLOCK_OUTPUTS, O_OK, what)
    if spec.interval is not None:
        _enabled(s, INTERLOCK_SETUP, change, f"{what} interval")
        await s.answer_number(
            change, spec.interval, f"{what} interval", screen=INTERLOCK_SETUP
        )


async def set_interlock_setup(
    s: _Session,
    name: str,
    *,
    inputs: tuple[str, ...] | None = None,
    outputs: tuple[InterlockOutputSpec | None, ...] = (),
    conditions: tuple[InterlockConditionSpec, ...] = (),
) -> bool:
    """Change a disabled program's input points and output tabs (901), then OK.

    Args:
    ----
        s: The open session.
        name: The program.
        inputs: The input points, exactly (None: unchanged).
        outputs: Per output tab (1, 2) the change, or None to leave the tab.
        conditions: Per input point the change of its condition (903), after the
            input points are picked.

    Returns:
    -------
        True when saved, False in a dry run.

    """
    s._writable()
    if len(outputs) > len(P_OUTPUT_TABS):
        msg = f"an interlock program has {len(P_OUTPUT_TABS)} output tabs"
        raise ItmCommandError(msg)
    await _open_interlock(s)
    try:
        await _open_setup(s, name, reading=False)
        if inputs is not None or conditions:
            what = "interlock input"
            await _open_from_setup(s, P_INPUT_BUTTON, INTERLOCK_INPUTS, what)
            if inputs is not None:
                await _pick_rows(
                    s,
                    INTERLOCK_INPUTS,
                    (N_INPUTS, N_AVAILABLE),
                    (N_ADD, N_REMOVE),
                    inputs,
                    what=what,
                )
            for condition in conditions:
                await _set_condition(s, condition)
            await _return_choice(s, INTERLOCK_INPUTS, N_OK, what)
        for tab, spec in enumerate(outputs):
            if spec is not None:
                await _set_output(s, tab, spec)
        return await s.press_ok(
            f"save {IL_WHAT} {name}", INTERLOCK_SETUP, P_OK, P_CANCEL
        )
    finally:
        await _leave_interlock(s)


class InterlockMixin(ClientCore):
    """``ItmClient`` methods for the interlock programs (mixed in).

    Every write runs in its own session and is confirmed by a read in a fresh
    one; with ``dry_run`` it returns None and only plans, with ``read_only`` it
    raises ReadOnlyError before anything is sent.
    """

    async def fetch_interlocks(self) -> list[InterlockProgram]:
        """Read the interlock programs and whether they are enabled.

        Returns
        -------
            The programs; empty when there are none.

        """
        return await self._in_session(read_interlocks)

    async def fetch_interlock_setup(
        self, name: str, *, details: bool = False
    ) -> InterlockSetup:
        """Read an interlock program's inputs and output tabs.

        Args:
        ----
            name: The program.
            details: Also open the input and output setup screens.

        Returns:
        -------
            The setup as shown.

        """
        return await self._in_session(
            lambda s: read_interlock_setup(s, name, details=details)
        )

    async def _interlock(self, name: str) -> InterlockProgram | None:
        programs = await self.fetch_interlocks()
        return next((p for p in programs if p.name == name.strip()), None)

    async def _interlock_present(
        self, name: str, *, gone: str | None = None
    ) -> InterlockProgram:
        program = await self._interlock(name)
        if program is None or (
            gone is not None and await self._interlock(gone) is not None
        ):
            msg = f"{name}: the iTM did not take the change of the {IL_WHAT}"
            raise ItmCommandError(msg)
        return program

    async def set_interlock_enabled(
        self, name: str, *, enabled: bool
    ) -> InterlockProgram | None:
        """Enable or disable an interlock program.

        Returns
        -------
            The program as read back, or None in a dry run.

        """
        self._experimental("enable or disable an interlock program")
        await self._system_write(
            lambda s: _discard(set_interlock_enabled(s, name, enabled=enabled))
        )
        if self.dry_run:
            return None
        program = await self._interlock_present(name)
        if program.enabled != enabled:
            msg = f"{name}: the iTM did not {'enable' if enabled else 'disable'} it"
            raise ItmCommandError(msg)
        return program

    async def create_interlock(self, name: str) -> InterlockProgram | None:
        """Add an interlock program (disabled, empty).

        Returns
        -------
            The program as read back, or None in a dry run.

        """
        self._experimental("create an interlock program")
        await self._system_write(lambda s: _discard(create_interlock(s, name)))
        return None if self.dry_run else await self._interlock_present(name)

    async def rename_interlock(
        self, name: str, new_name: str
    ) -> InterlockProgram | None:
        """Rename an interlock program.

        Returns
        -------
            The program as read back, or None in a dry run.

        """
        self._experimental("rename an interlock program")
        await self._system_write(
            lambda s: _discard(rename_interlock(s, name, new_name))
        )
        if self.dry_run:
            return None
        return await self._interlock_present(new_name, gone=name)

    async def copy_interlock(self, name: str, new_name: str) -> InterlockProgram | None:
        """Copy an interlock program under a new name.

        Returns
        -------
            The copy as read back, or None in a dry run.

        """
        self._experimental("copy an interlock program")
        await self._system_write(lambda s: _discard(copy_interlock(s, name, new_name)))
        return None if self.dry_run else await self._interlock_present(new_name)

    async def delete_interlock(self, name: str) -> None:
        """Delete a disabled interlock program."""
        self._experimental("delete an interlock program")
        await self._system_write(lambda s: _discard(delete_interlock(s, name)))
        if not self.dry_run and await self._interlock(name) is not None:
            msg = f"{name}: the iTM did not delete the {IL_WHAT}"
            raise ItmCommandError(msg)

    async def set_interlock_setup(
        self,
        name: str,
        *,
        inputs: Iterable[str] | None = None,
        outputs: Iterable[InterlockOutputSpec | None] = (),
        conditions: Iterable[InterlockConditionSpec] = (),
    ) -> InterlockSetup | None:
        """Change a disabled interlock program's inputs, conditions and output tabs.

        Args:
        ----
            name: The program (disabled).
            inputs: The input points, exactly (None: unchanged).
            outputs: Per output tab (1, 2) an ``InterlockOutputSpec`` or None; its
                ``actions`` set the action of output points (906).
            conditions: Per input point an ``InterlockConditionSpec`` (903).

        Returns:
        -------
            The setup as read back (with details when conditions or actions were
            set), or None in a dry run.

        """
        self._experimental("change an interlock program's setup")
        ins = None if inputs is None else tuple(inputs)
        specs = tuple(outputs)
        conds = tuple(conditions)
        await self._system_write(
            lambda s: _discard(
                set_interlock_setup(
                    s, name, inputs=ins, outputs=specs, conditions=conds
                )
            )
        )
        if self.dry_run:
            return None
        details = bool(conds) or any(spec and spec.actions for spec in specs)
        result = await self.fetch_interlock_setup(name, details=details)
        wrong = [
            f"condition of {c.point}"
            for c in conds
            if not any(
                _condition_is(got, c)
                for got in result.conditions
                if c.point.strip() in got.point
            )
        ]
        if ins is not None and not _rows_are(result.inputs, ins):
            wrong.append("inputs")
        for tab, spec in enumerate(specs):
            if spec is None:
                continue
            got = result.outputs[tab]
            if spec.outputs is not None and not _rows_are(got.outputs, spec.outputs):
                wrong.append(f"outputs {tab + 1}")
            if spec.condition is not None and (
                (got.condition or "").casefold() != spec.condition.strip().casefold()
            ):
                wrong.append(f"condition {tab + 1}")
            if spec.interval is not None and not same_temperature(
                temperature(got.interval), spec.interval
            ):
                wrong.append(f"interval {tab + 1}")
            wrong += [
                f"action of {a.point}"
                for a in spec.actions
                if not any(
                    _action_is(g, a) for g in got.actions if a.point.strip() in g.point
                )
            ]
        _check_wrong(name, wrong, result)
        return result


def _rows_are(rows: tuple[tuple[str, ...], ...], names: tuple[str, ...]) -> bool:
    """Tell whether the rows show exactly these names (one each, any column)."""
    wanted = {n.strip() for n in names}
    return len(rows) == len(wanted) and all(any(n in r for r in rows) for n in wanted)


def _shown_as(shown: str | None, wanted: str | None, *, modes: bool = False) -> bool:
    """Tell whether a shown label is the wanted one (None wanted: anything)."""
    if wanted is None:
        return True
    want = wanted.strip().casefold()
    return shown is not None and (
        shown.casefold() == want or (modes and mode_from_label(shown) == want)
    )


def _number_is(shown: str | None, wanted: float | None) -> bool:
    return wanted is None or same_temperature(temperature(shown), wanted)


def _condition_is(got: InterlockCondition, spec: InterlockConditionSpec) -> bool:
    """Tell whether a condition as read back is what the spec asked for."""
    return (
        _shown_as(got.state, spec.state)
        and _shown_as(got.mode, spec.mode, modes=True)
        and _shown_as(got.comparison, spec.comparison)
        and _shown_as(got.multistate, spec.multistate)
        and _number_is(got.constant, spec.constant)
        and (
            spec.reference_point is None
            or same_point(got.reference_point, spec.reference_point)
        )
        and _number_is(got.offset, spec.offset)
        and _number_is(got.hysteresis, spec.hysteresis)
    )


def _action_is(got: InterlockAction, spec: InterlockActionSpec) -> bool:
    """Tell whether an action as read back is what the spec asked for."""
    pairs = (
        (spec.power, got.power),
        (spec.lock_start_stop, got.lock_start_stop),
        (spec.lock_mode, got.lock_mode),
        (spec.lock_setpoint, got.lock_setpoint),
    )
    return (
        all(want is None or want == have for want, have in pairs)
        and _shown_as(got.mode, spec.mode, modes=True)
        and (spec.setpoint is None or same_temperature(got.setpoint, spec.setpoint))
    )


__all__ = [
    "InterlockAction",
    "InterlockActionSpec",
    "InterlockCondition",
    "InterlockConditionSpec",
    "InterlockMixin",
    "InterlockOutput",
    "InterlockOutputSpec",
    "InterlockProgram",
    "InterlockSetup",
]
