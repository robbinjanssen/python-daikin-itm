# ruff: noqa: SLF001
# pylint: disable=protected-access,too-many-lines
"""Dealer-option functions: temperature limit, sliding temperature, HMO, interlock.

Session-level flows on the screens of the functions the iTM only offers with a
dealer option: temperature limit groups (400-402), sliding temperature groups
(500-503), heating mode optimisation per unit (600) and interlock programs
(900-906). Each takes an open ``_Session``, saves through the session's ``write``
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
- 900 (interlock) lists activation and program name, opens empty with Add
  enabled and Copy, Delete, Rename and Edit disabled without a
  selection.

Assumed (not seen on a real iTM, only the web UI's part layout): a single-select
list selects a row with a CLICK + INT, a multi-select list (401/501 members, 905)
with a CLICK + INTARRAY, as the browser does; a list row's activation text is the
enable radio's label when on; the radios, Delete and Add's name are confirmed
with a CONFIRM dialog that is answered Yes; Add, Rename and Copy ask
the name in a CHARINPUT dialog; Registration (401/501) lists the members with the
name in the first column; the limits and ranges are entered in NUMBER dialogs; the
point picker (503) and the interlock's input (902) and output (905) screens only
return their choice to the screen that opened them, and only the OK of 402, 502
and 901 saves; an enabled group or program is disabled before it is deleted or
its interlock setup changed (an enabled program's setup offers a read-only mode,
like the emergency stop's).

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

import html
import re
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypeVar

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
    MB_HEATING_OPTIMISATION,
    MB_INTERLOCK,
    MB_SLIDING_TEMP,
    MB_TEMP_LIMIT,
    MenuScreen,
)
from .models import FAN_ICONS, PowerState, RemoteLock
from .protocol import DataType, EventType
from .schedule_edit import EVT_SETPOINT_BUTTON, LOCK_ORDER, POWER_RADIOS

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from .client import _Session
    from .protocol import Reply
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
# The condition setup (903), the management point picker (904), the action setup
# (906).
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
class InterlockProgram:
    """An interlock program and whether it is enabled."""

    name: str
    enabled: bool


@dataclass(frozen=True)
class InterlockCondition:  # pylint: disable=too-many-instance-attributes
    """The condition of one input point (903), as shown; None: not shown."""

    point: tuple[str, ...]  # the input's row in the input setup (902)
    tabs: tuple[str, ...] = ()  # the condition tabs the iTM shows
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
    setpoint: float | None = None  # °C, a fixed setpoint
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
    actions: tuple[InterlockActionSpec, ...] = ()  # per output point


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


def interlocks_from_screen(state: UIState) -> list[InterlockProgram]:
    """Read the interlock screen's program list (900)."""
    s = MenuScreen.INTERLOCK
    return [
        InterlockProgram(name=row[1], enabled=_activation(state, s, I_DISABLE, row[0]))
        for row in _cells(state, s, I_LIST)
        if len(row) >= 2
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


def _checked_part(state: UIState, screen: int, parts: tuple[int, ...]) -> int | None:
    """Return the index of the checked radio among parts, or None."""
    for i, part in enumerate(parts):
        if state.get(screen, part, "CHECK_STATE") == 1:
            return i
    return None


def _ticked(state: UIState, screen: int, part: int) -> bool:
    return bool(state.get(screen, part, "CHECK_STATE") == 1)


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
    if s.state.get(screen, part, "ENABLED") == 0:
        msg = f"{what}: the iTM has the button disabled"
        raise ItmCommandError(msg)


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


async def _pick_rows(  # noqa: PLR0913  # pylint: disable=too-many-arguments,too-many-locals
    s: _Session,
    screen: int,
    lists: tuple[int, int],
    buttons: tuple[int, int],
    wanted: tuple[str, ...],
    *,
    what: str,
    multi: bool,
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
        multi: Whether the lists are multi-select (INTARRAY).

    """
    chosen, available = lists
    add, remove = buttons
    select = _select_multi if multi else _select
    names = list(dict.fromkeys(w.strip() for w in wanted))
    while True:
        rows = _cells(s.state, screen, chosen)
        extra = [i for i, r in enumerate(rows) if any(r) and not set(r) & set(names)]
        if not extra:
            break
        await select(s, screen, chosen, extra[0])
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
        await select(s, screen, available, row)
        _enabled(s, screen, add, f"add {what}")
        await s.send(screen, add)
    got = _cells(s.state, screen, chosen)
    missing = [n for n in names if not any(n in r for r in got)]
    if missing or len([r for r in got if any(r)]) != len(names):
        msg = f"{what}: the list would be {[' '.join(r) for r in got]}, not {names}"
        raise ItmCommandError(msg)


async def _click(s: _Session, screen: int, part: int, what: str) -> None:
    """Click a button or radio, refusing one the iTM reports disabled."""
    _enabled(s, screen, part, what)
    await s.send(screen, part)


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


# -- interlock (900-906) ---------------------------------------------------------------

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
        await _select_multi(s, INTERLOCK_OUTPUTS, O_EVENTS, row)
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
    await _select_multi(s, INTERLOCK_OUTPUTS, O_EVENTS, row)
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
                multi=True,
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
                    multi=False,
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

    # -- interlock ---------------------------------------------------------------------
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
            if spec.interval is not None and not _close(
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


def _same_point(shown: str | None, wanted: str) -> bool:
    """Tell whether the shown outdoor point is the picked one (as listed in 503)."""
    if not shown:
        return False
    return shown in wanted or wanted.strip() in shown


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
    return wanted is None or _close(temperature(shown), wanted)


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
            or _same_point(got.reference_point, spec.reference_point)
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
        and (spec.setpoint is None or _close(got.setpoint, spec.setpoint))
    )


__all__ = [
    "SLIDING",
    "TEMP_LIMIT",
    "DealerFunctionsMixin",
    "HeatingOptimisationUnit",
    "InterlockAction",
    "InterlockActionSpec",
    "InterlockCondition",
    "InterlockConditionSpec",
    "InterlockOutput",
    "InterlockOutputSpec",
    "InterlockProgram",
    "InterlockSetup",
    "SlidingTemperatureGroup",
    "TemperatureLimitGroup",
]
