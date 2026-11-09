# pylint: disable=too-many-lines
"""Demand (power limit) control and energy saving: readers and writes.

Session-level flows, each leaving every screen it opened with its Close or Cancel.
Writes save through the session's ``write`` / ``press_ok`` (a dry run plans the one
request that saves and cancels; ``read_only`` refuses before anything is sent) and
are confirmed by the client with a read in a fresh session (``confirm_*``). A part
the iTM reports disabled (``ENABLED 0``) is never clicked; a hidden one is.

The screens:

- demand control (3300, from the menu list's "power limit" button, part 109): the
  status (target, predicted, current and adjustment power, period, remaining time)
  and its graph; behind it the legend (3302), the target power (3312), the setup
  (3301: the control list, the forced level, the control status 3310, and per
  control its temperature, capacity or on/off setup 3307 / 3308 / 3309 with their
  groups 3311) and the graphs (3303) with their daily, monthly and yearly data
  (3304 / 3305 / 3306);
- energy saving suspend (1400, from the main menu bar's energy saving button, part
  17 next to the indicator 16);
- the service screens (after a service login): energy saving status (1508), the
  demand pulse setup (1512), the demand control interlock setup (1513) and their
  input port picker (1514). Their writes take the service password as an argument
  and need ``allow_service_change=True`` in the client.

As seen on a real iTM (read only): the menu's power limit button opened nothing
(no dealer option, no pulse input), so ``fetch_demand_control`` returns None there;
1508 opened with every text empty while energy saving was not active; the main menu
bar's energy saving indicator is hidden while energy saving is not active.

Assumed (from the web UI's script, not seen on a real iTM), and tested against the
simulated iTM only: 3301 has no OK, so picking the forced level saves it (a CONFIRM
is answered Yes); 3301's control list opens one of 3307-3309 with Modify, by the
control selected; the setups' group list selects a group (a CLICK with the row as
INT, like the emergency stop lists) and shows its values; the group button opens
3311 for the selected group; 3311's OK and 1514's OK are sent as writes (a dry run
cancels them) before the parent screen's OK saves; the energy saving button opens
1400 even while hidden, and 1400's button suspends energy saving while it is active
(the indicator shown) and resumes it while suspended; the service tab's power limit
button opens 1512 or 1513 by the energy save mode, and their port buttons open 1514.
Shown texts are kept; numbers are parsed from them where they are plain ones
(``ShownValue``). Graphs are returned as decoded, uninterpreted.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from ._bases import ClientCore
from ._screens import close as _close
from ._screens import enter_number as _number
from ._screens import graph as _graph
from ._screens import open_below as _open_below
from ._screens import press_ok as _ok
from ._screens import radio as _radio
from ._screens import row_named as _row
from ._util import T as _T
from ._util import field_names
from ._util import ignore as _ignore
from .energy_navigator import ShownValue
from .energy_navigator import shown_value as _shown
from .exceptions import ItmCommandError, ReadOnlyError
from .menu import MenuScreen
from .protocol import DataType, EventType, ScreenID
from .service import ENERGY_SAVING_STATUS as SERVICE_STATUS
from .service import (
    EST_CANCEL,
    EST_DISABLE,
    IL_CANCEL,
    IL_OK,
    IL_PORT_BUTTONS,
    IL_PORT_DELETES,
    INTERLOCK_SETUP,
    MB_DEMAND_SETUP,
    MB_ENERGY_SAVING_STATUS,
    PORT_PICKER,
    PP_CANCEL,
    PP_LIST,
    PP_OK,
    PS_CANCEL,
    PS_COEFFICIENT_BUTTON,
    PS_CUTOFF_BUTTON,
    PS_OK,
    PS_PORT_BUTTONS,
    PS_PORT_DELETES,
    PS_RETURN_BUTTON,
    PS_SYNC_EXTERNAL,
    PS_SYNC_PERIOD,
    PULSE_SETUP,
    DemandPulseSetup,
    DemandServiceSettings,
    EnergySavingStatus,
    energy_saving_status_from_screen,
    interlock_ports_from_screen,
    ports_from_screen,
    pulse_setup_from_screen,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from .protocol import Reply
    from .session import _Session
    from .state import UIState


# The menu list's power limit button and the main menu bar's energy saving button.
MB_DEMAND = 109
MM_ENERGY_SAVING_BUTTON = 17

# Demand control main (3300).
MAIN = 3300
MN_GRAPH, MN_LEGEND, MN_UNIT = 1, 2, 10
MN_STATE, MN_PERIOD, MN_TARGET, MN_PREDICTED, MN_SYNC = 5, 7, 9, 12, 15
MN_REMAIN_MIN, MN_REMAIN_SEC, MN_CURRENT, MN_ADJUST = 17, 19, 22, 25
MN_TARGET_BUTTON, MN_SETUP, MN_GRAPHS, MN_CANCEL = 27, 28, 29, 30
# Setup (3301); Status opens 3310.
SETUP = 3301
SU_LIST, SU_ENABLE, SU_DISABLE, SU_MODIFY, SU_STATUS = 1, 3, 4, 5, 6
SU_FORCED, SU_CANCEL = 7, 8
# Legend (3302).
LEGEND = 3302
LG_LABELS, LG_CANCEL = (1, 2, 3, 5), 4
# Graphs (3303): per tab (daily, monthly, yearly).
GRAPHS = 3303
GR_TABS, GR_CANCEL = 1, 11
GR_TAB_PARTS = (
    (2, (4, 6, 8), 9, 10, 3304),
    (12, (14, 16), 17, 18, 3305),
    (19, (21,), 22, 23, 3306),
)
# Daily / monthly / yearly data (3304-3306): the date shown, the list, Cancel.
DATA_DATE, DATA_LIST = 2, 3
DATA_CANCEL = {3304: 5, 3305: 4, 3306: 4}
# Level setups: temperature (3307), capacity (3308), on/off (3309); the group
# list and group button are the same on all three.
TEMPERATURE, CAPACITY, ON_OFF = 3307, 3308, 3309
LS_GROUPS, LS_GROUP_BUTTON = 2, 3
TS_UPPER_COOL, TS_UPPER_BUTTON, TS_LOWER_HEAT, TS_LOWER_BUTTON = 5, 7, 9, 11
# Control status (3310).
STATUS = 3310
CS_TABS, CS_CANCEL = 1, 7
CS_TAB_PARTS = ((3, 5, 6), (9, 11, 12), (14, 16, 17))
# Group (3311).
GROUP = 3311
GP_MEMBERS, GP_ADD, GP_REMOVE, GP_AVAILABLE = 2, 4, 5, 8
GP_CONDITION, GP_RESUME, GP_OK, GP_CANCEL = 10, 14, 11, 12
# Target power (3312).
TARGET = 3312
TP_TEXT, TP_BUTTON, TP_OK, TP_CANCEL = 2, 3, 4, 5
# Energy saving suspend (1400).
SUSPEND = 1400
SS_STATUS, SS_BUTTON, SS_CANCEL = 2, 3, 4


@dataclass(frozen=True)
class _Layout:
    """Where a level setup screen keeps its values."""

    kind: str
    start: int | None  # the start level combo
    levels: tuple[int, ...]  # the per-level combos, level 1 first
    ok: int
    cancel: int


LAYOUTS = {
    TEMPERATURE: _Layout("temperature", 13, tuple(range(24, 32)), 40, 41),
    CAPACITY: _Layout("capacity", None, tuple(range(13, 21)), 29, 30),
    ON_OFF: _Layout("on_off", 5, (), 7, 8),
}

# What fetch_demand_control reads besides the status (DemandControl fields).
PARTS = (
    "legend",
    "target_power",
    "setup",
    "levels",
    "groups",
    "control_status",
    "graphs",
)


# -- models --------------------------------------------------------------------------


@dataclass(frozen=True)
class DemandStatus:  # pylint: disable=too-many-instance-attributes
    """The demand control status (3300), texts as shown."""

    state: str | None
    period: str | None
    target: ShownValue | None  # target power
    predicted: ShownValue | None
    sync_method: str | None
    remaining: str | None  # "min:sec" as shown
    current: ShownValue | None
    adjustment: ShownValue | None
    power_unit: str | None  # the unit label after the target power, e.g. "kW"
    graph: tuple[Any, ...]  # as decoded, uninterpreted


@dataclass(frozen=True)
class DemandLegend:
    """The graph legend (3302): the label of each line, as shown."""

    target: str | None
    current: str | None
    predicted: str | None
    consumption: str | None


@dataclass(frozen=True)
class DemandControlRow:
    """A row of the setup's control list (3301)."""

    name: str  # the first cell
    cells: tuple[str, ...]
    enabled: bool | None  # the radios while the row is selected


@dataclass(frozen=True)
class DemandSetup:
    """The demand control setup (3301)."""

    controls: tuple[DemandControlRow, ...]
    forced_level: str | None
    forced_level_options: tuple[str, ...]


@dataclass(frozen=True)
class DemandLevelSetup:  # pylint: disable=too-many-instance-attributes
    """A control's level setup (3307-3309) for one of its groups."""

    control: str  # the 3301 row
    kind: str  # "temperature", "capacity" or "on_off"
    group: str | None  # the group selected (None: the screen lists none)
    start_level: str | None
    start_level_options: tuple[str, ...]
    levels: tuple[str | None, ...]  # each level's shown option, level 1 first
    level_options: tuple[tuple[str, ...], ...]
    upper_cool: ShownValue | None = None  # temperature setup only
    lower_heat: ShownValue | None = None


@dataclass(frozen=True)
class DemandGroup:  # pylint: disable=too-many-instance-attributes
    """A demand group (3311), list rows reduced to their last cell (the name)."""

    control: str
    name: str
    members: tuple[str, ...]
    available: tuple[str, ...]
    condition: str | None
    condition_options: tuple[str, ...]
    resume: str | None
    resume_options: tuple[str, ...]


@dataclass(frozen=True)
class DemandStatusTab:
    """A tab of the control status (3310)."""

    name: str
    status: str | None
    level: str | None
    groups: tuple[tuple[str, ...], ...]  # list rows as shown


@dataclass(frozen=True)
class DemandData:
    """A daily, monthly or yearly data list (3304-3306)."""

    date: str | None
    rows: tuple[tuple[str, ...], ...]  # as shown


@dataclass(frozen=True)
class DemandGraph:
    """A graph tab (3303): the graph, the date picked in its combos, its data."""

    name: str
    graph: tuple[Any, ...]  # as decoded, uninterpreted
    selection: tuple[str | None, ...]  # the date combos' shown options
    data: DemandData | None  # None while the detail button is disabled


@dataclass(frozen=True)
class DemandControl:  # pylint: disable=too-many-instance-attributes
    """What fetch_demand_control read; None for what was not read or not there."""

    status: DemandStatus
    legend: DemandLegend | None = None
    target_power: ShownValue | None = None  # as 3312 shows it
    setup: DemandSetup | None = None
    levels: tuple[DemandLevelSetup, ...] | None = None
    groups: tuple[DemandGroup, ...] | None = None
    control_status: tuple[DemandStatusTab, ...] | None = None
    graphs: tuple[DemandGraph, ...] | None = None


@dataclass(frozen=True)
class EnergySaving:
    """Energy saving as the main menu bar, 1400 and (with a password) 1508 show it."""

    active: bool  # the main menu bar's indicator is shown
    suspend_status: str | None  # 1400's status text; None when 1400 didn't open
    suspend_enabled: bool | None  # 1400's button is enabled
    service: EnergySavingStatus | None = None  # 1508, after a service login


@dataclass(frozen=True)
class LevelChange:
    """What to change in a control's level setup; None keeps a value."""

    control: str  # the 3301 row (its first cell)
    group: str | None = None  # None: the group the screen shows first
    start_level: str | None = None  # an option as shown
    levels: Mapping[int, str] = field(default_factory=dict)  # level 1-8 -> option
    upper_cool: float | None = None  # temperature setup only
    lower_heat: float | None = None


@dataclass(frozen=True)
class GroupChange:
    """What to change in a demand group; None keeps a value."""

    control: str
    group: str
    members: tuple[str, ...] | None = None  # exactly these points
    condition: str | None = None  # an option as shown
    resume: str | None = None


@dataclass(frozen=True)
class PulseChange:
    """What to change in the demand pulse setup (1512); None keeps a value.

    A port "" clears it (its delete button); another value picks it in 1514.
    """

    input_port1: str | None = None
    input_port2: str | None = None
    pulse_coefficient: float | None = None
    sync_external: bool | None = None
    return_value: float | None = None
    cutoff_value: float | None = None


# -- helpers -------------------------------------------------------------------------


def _string(state: UIState, screen: int, part: int) -> str | None:
    for ptype in ("TEXT", "LABEL"):
        value = state.get(screen, part, ptype)
        if isinstance(value, str):
            return html.unescape(value).strip() or None
    return None


def _options(state: UIState, screen: int, part: int) -> tuple[str, ...]:
    return tuple(
        html.unescape(str(o)).strip()
        for o in state.get(screen, part, "COMBO_STRS") or ()
    )


def _option(state: UIState, screen: int, part: int) -> str | None:
    options = _options(state, screen, part)
    idx = state.get(screen, part, "SINGLE_SELIDX", 0)
    return options[idx] if isinstance(idx, int) and 0 <= idx < len(options) else None


def _rows(state: UIState, screen: int, part: int) -> tuple[tuple[str, ...], ...]:
    rows = (state.get(screen, part, "LIST_ROW") or {}).get("rows", [])
    return tuple(
        tuple(html.unescape(str(t)).strip() for t in r.get("text", [])) for r in rows
    )


def _names(state: UIState, screen: int, part: int, *, last: bool = True) -> list[str]:
    """Return a list's names: each row's last (or first) cell."""
    return [
        (row[-1] if last else row[0]) if row else ""
        for row in _rows(state, screen, part)
    ]


async def _dismiss(s: _Session, screen: int, reply: Reply) -> None:
    """Close the dialogs a reply opened (an INFO instead of a screen)."""
    for dialog in reply.dialogs():
        await s.send(screen, dialog[0])


async def _visit_tabs(s: _Session, screen: int, part: int) -> int:
    """Show every tab once (the iTM may send a tab's parts when shown); count them."""
    count = len(s.state.get(screen, part, "TAB_NAME") or [])
    for index in range(1, count):
        await s.send(screen, part, EventType.TAB_SELECT, DataType.INT, index)
    if count > 1:
        await s.send(screen, part, EventType.TAB_SELECT, DataType.INT, 0)
    return count


def _tab_names(state: UIState, screen: int, part: int, count: int) -> list[str]:
    names = [html.unescape(str(t)) for t in state.get(screen, part, "TAB_NAME") or []]
    return [names[i] if i < len(names) else f"Tab {i + 1}" for i in range(count)]


def _option_index(s: _Session, screen: int, part: int, value: str, what: str) -> int:
    options = [o.casefold() for o in _options(s.state, screen, part)]
    if value.strip().casefold() not in options:
        msg = (
            f"{what} {value!r} is not offered: {list(_options(s.state, screen, part))}"
        )
        raise ItmCommandError(msg)
    return options.index(value.strip().casefold())


async def _pick(s: _Session, screen: int, part: int, value: str, what: str) -> None:
    """Pick a combo option by its text (an edit the screen's OK saves)."""
    index = _option_index(s, screen, part, value, what)
    if s.state.get(screen, part, "SINGLE_SELIDX") == index:
        return
    s.refuse_disabled(screen, part, what)
    await s.pick_option(screen, part, index, what)


def _close_to(shown: ShownValue | None, wanted: float) -> bool:
    return (
        shown is not None
        and shown.value is not None
        and math.isclose(shown.value, wanted, abs_tol=0.0005)
    )


# -- demand control: readers ---------------------------------------------------------


def status_from_screen(state: UIState) -> DemandStatus:
    """Read the demand control main screen (3300)."""
    minutes = _string(state, MAIN, MN_REMAIN_MIN)
    seconds = _string(state, MAIN, MN_REMAIN_SEC)
    remaining = (
        None
        if minutes is None and seconds is None
        else f"{minutes or ''}:{seconds or ''}"
    )
    return DemandStatus(
        state=_string(state, MAIN, MN_STATE),
        period=_string(state, MAIN, MN_PERIOD),
        target=_shown(state, MAIN, MN_TARGET),
        predicted=_shown(state, MAIN, MN_PREDICTED),
        sync_method=_string(state, MAIN, MN_SYNC),
        remaining=remaining,
        current=_shown(state, MAIN, MN_CURRENT),
        adjustment=_shown(state, MAIN, MN_ADJUST),
        power_unit=_string(state, MAIN, MN_UNIT),
        graph=_graph(state, MAIN, MN_GRAPH),
    )


def legend_from_screen(state: UIState) -> DemandLegend:
    """Read the graph legend (3302)."""
    target, current, predicted, consumption = (
        _string(state, LEGEND, p) for p in LG_LABELS
    )
    return DemandLegend(target, current, predicted, consumption)


def level_setup_from_screen(
    state: UIState, screen: int, control: str, group: str | None
) -> DemandLevelSetup:
    """Read an open level setup screen (3307-3309) for the group selected."""
    layout = LAYOUTS[screen]
    start = layout.start
    temperature = screen == TEMPERATURE
    return DemandLevelSetup(
        control=control,
        kind=layout.kind,
        group=group,
        start_level=None if start is None else _option(state, screen, start),
        start_level_options=() if start is None else _options(state, screen, start),
        levels=tuple(_option(state, screen, p) for p in layout.levels),
        level_options=tuple(_options(state, screen, p) for p in layout.levels),
        upper_cool=_shown(state, screen, TS_UPPER_COOL) if temperature else None,
        lower_heat=_shown(state, screen, TS_LOWER_HEAT) if temperature else None,
    )


def group_from_screen(state: UIState, control: str, name: str) -> DemandGroup:
    """Read an open group screen (3311)."""
    return DemandGroup(
        control=control,
        name=name,
        members=tuple(n for n in _names(state, GROUP, GP_MEMBERS) if n),
        available=tuple(n for n in _names(state, GROUP, GP_AVAILABLE) if n),
        condition=_option(state, GROUP, GP_CONDITION),
        condition_options=_options(state, GROUP, GP_CONDITION),
        resume=_option(state, GROUP, GP_RESUME),
        resume_options=_options(state, GROUP, GP_RESUME),
    )


async def open_main(s: _Session) -> bool:
    """Open demand control (3300) from the menu list; False when it doesn't open.

    A real iTM without the dealer option and pulse input opened nothing. A disabled
    button is not clicked; a dialog instead of the screen is closed.
    """
    await s.open_menu()
    if s.state.get(MenuScreen.MENULIST, MB_DEMAND, "ENABLED") == 0:
        return False
    reply = await s.send(MenuScreen.MENULIST, MB_DEMAND)
    await s.wait_for_progress(reply)
    if MAIN not in s.state.screens:
        await _dismiss(s, MenuScreen.MENULIST, reply)
        return False
    return True


async def _require_main(s: _Session) -> None:
    if not await open_main(s):
        msg = "demand control did not open (the iTM may not offer it)"
        raise ItmCommandError(msg)


async def _leave_all(s: _Session) -> None:
    """Leave every demand control screen that is still open, innermost first."""
    for screen, cancel in (
        (GROUP, GP_CANCEL),
        *((screen, layout.cancel) for screen, layout in LAYOUTS.items()),
        (STATUS, CS_CANCEL),
        *((screen, cancel) for screen, cancel in DATA_CANCEL.items()),
        (GRAPHS, GR_CANCEL),
        (LEGEND, LG_CANCEL),
        (TARGET, TP_CANCEL),
        (SETUP, SU_CANCEL),
        (MAIN, MN_CANCEL),
    ):
        await _close(s, screen, cancel)


async def read_legend(s: _Session) -> DemandLegend:
    """Read the legend (3300 -> 3302), then cancel it."""
    await _open_below(s, MAIN, MN_LEGEND, LEGEND)
    try:
        return legend_from_screen(s.state)
    finally:
        await _close(s, LEGEND, LG_CANCEL)


async def read_target_power(s: _Session) -> ShownValue | None:
    """Read the target power setup (3300 -> 3312), then cancel it."""
    await _open_below(s, MAIN, MN_TARGET_BUTTON, TARGET)
    try:
        return _shown(s.state, TARGET, TP_TEXT)
    finally:
        await _close(s, TARGET, TP_CANCEL)


async def read_setup(s: _Session) -> DemandSetup:
    """Read the setup (3300 -> 3301): each control's radios, the forced level."""
    await _open_below(s, MAIN, MN_SETUP, SETUP)
    try:
        controls = []
        for i, cells in enumerate(_rows(s.state, SETUP, SU_LIST)):
            await s.select_row(SETUP, SU_LIST, i)
            controls.append(
                DemandControlRow(
                    name=cells[0] if cells else "",
                    cells=cells,
                    enabled=_radio(s.state, SETUP, SU_ENABLE, SU_DISABLE),
                )
            )
        return DemandSetup(
            controls=tuple(controls),
            forced_level=_option(s.state, SETUP, SU_FORCED),
            forced_level_options=_options(s.state, SETUP, SU_FORCED),
        )
    finally:
        await _close(s, SETUP, SU_CANCEL)


async def _open_control(s: _Session, control: str) -> int:
    """Modify the control selected on the setup (3301); return the screen opened."""
    reply = await s.click(SETUP, SU_MODIFY, f"modify {control}")
    for screen in LAYOUTS:
        if screen in s.state.screens:
            return screen
    msg = f"{control}: no level setup opened: {reply.dialogs()}"
    raise ItmCommandError(msg)


async def _walk(
    s: _Session, visit: Callable[[int, str, str | None], Awaitable[None]]
) -> None:
    """Call visit(screen, control, group) for every control's every group."""
    await _open_below(s, MAIN, MN_SETUP, SETUP)
    try:
        for row, control in enumerate(_names(s.state, SETUP, SU_LIST, last=False)):
            await s.select_row(SETUP, SU_LIST, row)
            if s.state.get(SETUP, SU_MODIFY, "ENABLED") == 0:
                continue  # never click a disabled Modify
            screen = await _open_control(s, control)
            try:
                groups = _names(s.state, screen, LS_GROUPS)
                if not groups:
                    await visit(screen, control, None)
                for i, group in enumerate(groups):
                    await s.select_row(screen, LS_GROUPS, i)
                    await visit(screen, control, group)
            finally:
                await _close(s, screen, LAYOUTS[screen].cancel)
    finally:
        await _close(s, SETUP, SU_CANCEL)


async def read_levels(s: _Session) -> tuple[DemandLevelSetup, ...]:
    """Read every control's level setup per group (3301 -> 3307-3309), cancelled."""
    out: list[DemandLevelSetup] = []

    async def visit(screen: int, control: str, group: str | None) -> None:
        out.append(level_setup_from_screen(s.state, screen, control, group))

    await _walk(s, visit)
    return tuple(out)


async def _read_group(
    s: _Session, screen: int, control: str, group: str
) -> DemandGroup:
    await _open_below(s, screen, LS_GROUP_BUTTON, GROUP)
    try:
        return group_from_screen(s.state, control, group)
    finally:
        await _close(s, GROUP, GP_CANCEL)


async def read_groups(s: _Session) -> tuple[DemandGroup, ...]:
    """Read every control's groups through the group screen (3311), cancelled."""
    out: list[DemandGroup] = []

    async def visit(screen: int, control: str, group: str | None) -> None:
        if group is not None and s.state.get(screen, LS_GROUP_BUTTON, "ENABLED") != 0:
            out.append(await _read_group(s, screen, control, group))

    await _walk(s, visit)
    return tuple(out)


async def read_control_status(s: _Session) -> tuple[DemandStatusTab, ...]:
    """Read the control status (3301 -> 3310), every tab, then cancel."""
    await _open_below(s, MAIN, MN_SETUP, SETUP)
    try:
        await _open_below(s, SETUP, SU_STATUS, STATUS)
        count = await _visit_tabs(s, STATUS, CS_TABS) or 1
        names = _tab_names(s.state, STATUS, CS_TABS, count)
        return tuple(
            DemandStatusTab(
                name=name,
                status=_string(s.state, STATUS, status),
                level=_string(s.state, STATUS, level),
                groups=_rows(s.state, STATUS, groups),
            )
            for name, (status, level, groups) in zip(
                names, CS_TAB_PARTS[:count], strict=False
            )
        )
    finally:
        await _close(s, STATUS, CS_CANCEL)
        await _close(s, SETUP, SU_CANCEL)


async def _read_data(s: _Session, button: int, screen: int) -> DemandData | None:
    if s.state.get(GRAPHS, button, "ENABLED") == 0:
        return None
    await _open_below(s, GRAPHS, button, screen)
    try:
        return DemandData(
            date=_string(s.state, screen, DATA_DATE),
            rows=_rows(s.state, screen, DATA_LIST),
        )
    finally:
        await _close(s, screen, DATA_CANCEL[screen])


async def read_graphs(s: _Session) -> tuple[DemandGraph, ...]:
    """Read the graphs (3300 -> 3303), each tab's graph and its data (3304-3306)."""
    await _open_below(s, MAIN, MN_GRAPHS, GRAPHS)
    try:
        count = len(s.state.get(GRAPHS, GR_TABS, "TAB_NAME") or []) or 1
        names = _tab_names(s.state, GRAPHS, GR_TABS, count)
        out = []
        for index, (name, parts) in enumerate(
            zip(names, GR_TAB_PARTS[:count], strict=False)
        ):
            graph, combos, _update, detail, screen = parts
            if index:
                await s.send(GRAPHS, GR_TABS, EventType.TAB_SELECT, DataType.INT, index)
            out.append(
                DemandGraph(
                    name=name,
                    graph=_graph(s.state, GRAPHS, graph),
                    selection=tuple(_option(s.state, GRAPHS, c) for c in combos),
                    data=await _read_data(s, detail, screen),
                )
            )
        return tuple(out)
    finally:
        for screen, cancel in DATA_CANCEL.items():
            await _close(s, screen, cancel)
        await _close(s, GRAPHS, GR_CANCEL)


def wanted_parts(only: Iterable[str] | None) -> frozenset[str]:
    """Check the names of the parts to read (DemandControl fields); None: all.

    Raises
    ------
        ItmCommandError: Only is a string, or a name is not one of them.

    """
    names = field_names(only, PARTS, "demand control parts")
    return frozenset(PARTS) if names is None else names


async def read_demand_control(
    s: _Session, only: frozenset[str] = frozenset(PARTS)
) -> DemandControl | None:
    """Read demand control's status and the named parts, each screen cancelled.

    Returns
    -------
        What was read; None when the menu's power limit button opens nothing.

    """
    readers: dict[str, Callable[[_Session], Awaitable[Any]]] = {
        "legend": read_legend,
        "target_power": read_target_power,
        "setup": read_setup,
        "levels": read_levels,
        "groups": read_groups,
        "control_status": read_control_status,
        "graphs": read_graphs,
    }
    try:
        if not await open_main(s):
            return None
        status = status_from_screen(s.state)
        values = {name: await readers[name](s) for name in PARTS if name in only}
        return DemandControl(status=status, **values)
    finally:
        await _leave_all(s)


# -- demand control: writes ----------------------------------------------------------


async def set_target(s: _Session, power: float) -> bool:
    """Enter the target power (3300 -> 3312) and OK.

    Returns
    -------
        True when saved, False in a dry run.

    """
    try:
        await _require_main(s)
        await _open_below(s, MAIN, MN_TARGET_BUTTON, TARGET)
        await _number(s, TARGET, TP_BUTTON, power, "target power")
        return await _ok(s, f"save target power {power}", TARGET, TP_OK, TP_CANCEL)
    finally:
        await _leave_all(s)


async def confirm_target(s: _Session, power: float) -> DemandStatus:
    """Read the status back and check its target power.

    Returns
    -------
        The status as the iTM shows it now.

    Raises
    ------
        ItmCommandError: It doesn't show the target power.

    """
    try:
        await _require_main(s)
        status = status_from_screen(s.state)
    finally:
        await _leave_all(s)
    if not _close_to(status.target, power):
        msg = f"the iTM did not take the target power: {status.target}, not {power}"
        raise ItmCommandError(msg)
    return status


async def set_forced_level(s: _Session, level: str) -> bool:
    """Pick the forced level on the setup (3301); the pick saves (no OK there).

    Returns
    -------
        True when saved (or already so), False in a dry run.

    """
    try:
        await _require_main(s)
        await _open_below(s, MAIN, MN_SETUP, SETUP)
        index = _option_index(s, SETUP, SU_FORCED, level, "forced level")
        if s.state.get(SETUP, SU_FORCED, "SINGLE_SELIDX") == index:
            return True
        s.refuse_disabled(SETUP, SU_FORCED, "forced level")
        reply = await s.write(
            f"set forced level {level}",
            SETUP,
            SU_FORCED,
            EventType.CLICK,
            DataType.INT,
            index,
        )
        if reply is None:
            return False
        await s.answer_confirm(SETUP, reply)
        return True
    finally:
        await _leave_all(s)


async def read_forced_level(s: _Session) -> str | None:
    """Read the setup's forced level (for a read-back).

    Returns
    -------
        The forced level's shown option.

    """
    try:
        await _require_main(s)
        await _open_below(s, MAIN, MN_SETUP, SETUP)
        return _option(s.state, SETUP, SU_FORCED)
    finally:
        await _leave_all(s)


async def _open_setup_for(s: _Session, control: str, group: str | None) -> int:
    """Open a control's level setup with a group selected; return its screen."""
    await _require_main(s)
    await _open_below(s, MAIN, MN_SETUP, SETUP)
    row = _row(_names(s.state, SETUP, SU_LIST, last=False), control, "control")
    await s.select_row(SETUP, SU_LIST, row)
    screen = await _open_control(s, control)
    if group is not None:
        names = _names(s.state, screen, LS_GROUPS)
        await s.select_row(screen, LS_GROUPS, _row(names, group, "group"))
    return screen


def _selected_group(s: _Session, screen: int, group: str | None) -> str | None:
    if group is not None:
        return group.strip()
    names = _names(s.state, screen, LS_GROUPS)
    return names[0] if names else None


async def set_level_setup(s: _Session, change: LevelChange) -> bool:
    """Change a control's level setup for a group (3307-3309) and OK.

    Returns
    -------
        True when saved, False in a dry run.

    Raises
    ------
        ItmCommandError: A value this kind of setup doesn't have, or not offered.

    """
    try:
        screen = await _open_setup_for(s, change.control, change.group)
        layout = LAYOUTS[screen]
        if screen != TEMPERATURE and (
            change.upper_cool is not None or change.lower_heat is not None
        ):
            msg = f"{change.control}: a {layout.kind} setup has no temperature limits"
            raise ItmCommandError(msg)
        if change.start_level is not None:
            if layout.start is None:
                msg = f"{change.control}: a {layout.kind} setup has no start level"
                raise ItmCommandError(msg)
            await _pick(s, screen, layout.start, change.start_level, "start level")
        for level, value in sorted(change.levels.items()):
            if not 1 <= level <= len(layout.levels):
                msg = f"{change.control}: no level {level} in a {layout.kind} setup"
                raise ItmCommandError(msg)
            await _pick(s, screen, layout.levels[level - 1], value, f"level {level}")
        if change.upper_cool is not None:
            await _number(
                s, screen, TS_UPPER_BUTTON, change.upper_cool, "upper cooling limit"
            )
        if change.lower_heat is not None:
            await _number(
                s, screen, TS_LOWER_BUTTON, change.lower_heat, "lower heating limit"
            )
        return await _ok(
            s,
            f"save {layout.kind} setup of {change.control}",
            screen,
            layout.ok,
            layout.cancel,
        )
    finally:
        await _leave_all(s)


async def confirm_level_setup(s: _Session, change: LevelChange) -> DemandLevelSetup:
    """Read a level setup back (cancelled) and compare.

    Returns
    -------
        The setup as the iTM shows it now.

    Raises
    ------
        ItmCommandError: It doesn't show the change.

    """
    try:
        screen = await _open_setup_for(s, change.control, change.group)
        group = _selected_group(s, screen, change.group)
        got = level_setup_from_screen(s.state, screen, change.control, group)
    finally:
        await _leave_all(s)
    wrong = []
    if change.start_level is not None and (got.start_level or "").casefold() != (
        change.start_level.strip().casefold()
    ):
        wrong.append(f"start level {got.start_level}")
    for level, value in change.levels.items():
        shown = got.levels[level - 1]
        if (shown or "").casefold() != value.strip().casefold():
            wrong.append(f"level {level} {shown}")
    for name, shown_value, wanted in (
        ("upper cooling limit", got.upper_cool, change.upper_cool),
        ("lower heating limit", got.lower_heat, change.lower_heat),
    ):
        if wanted is not None and not _close_to(shown_value, wanted):
            wrong.append(f"{name} {shown_value}")
    if wrong:
        msg = f"{change.control}: the iTM did not take the setup: {wrong}"
        raise ItmCommandError(msg)
    return got


async def _pick_members(s: _Session, members: tuple[str, ...]) -> None:
    """Make the group's members exactly these (remove, then add, by name)."""
    wanted = list(dict.fromkeys(m.strip() for m in members))
    for member in _names(s.state, GROUP, GP_MEMBERS):
        if member and member not in wanted:
            names = _names(s.state, GROUP, GP_MEMBERS)
            await s.select_row(GROUP, GP_MEMBERS, _row(names, member, "member"))
            await s.click(GROUP, GP_REMOVE, "remove demand group member")
    for member in wanted:
        if member in _names(s.state, GROUP, GP_MEMBERS):
            continue
        names = _names(s.state, GROUP, GP_AVAILABLE)
        try:
            row = _row(names, member, "point")
        except ItmCommandError as err:
            msg = f"{member!r} is not offered: {err}"
            raise ItmCommandError(msg) from err
        await s.select_row(GROUP, GP_AVAILABLE, row)
        await s.click(GROUP, GP_ADD, "add demand group member")
    got = [n for n in _names(s.state, GROUP, GP_MEMBERS) if n]
    if set(got) != set(wanted):
        msg = f"the members would be {got}, not {wanted}"
        raise ItmCommandError(msg)


async def set_group(s: _Session, change: GroupChange) -> bool:
    """Change a demand group (3311): members, condition, resume; OK, then the setup's.

    3311's OK is sent as a write (a dry run cancels 3311) whether it saves or only
    returns to the setup screen, whose OK is then pressed too.

    Returns
    -------
        True when saved, False in a dry run.

    """
    try:
        screen = await _open_setup_for(s, change.control, change.group)
        await _open_below(s, screen, LS_GROUP_BUTTON, GROUP)
        if change.members is not None:
            await _pick_members(s, change.members)
        if change.condition is not None:
            await _pick(s, GROUP, GP_CONDITION, change.condition, "condition")
        if change.resume is not None:
            await _pick(s, GROUP, GP_RESUME, change.resume, "resume")
        label = f"save demand group {change.group} of {change.control}"
        s.refuse_disabled(GROUP, GP_OK, label)
        reply = await s.write(label, GROUP, GP_OK)
        if reply is None:
            return False
        await s.answer_confirm(GROUP, reply)
        s.state.screens.pop(GROUP, None)
        layout = LAYOUTS[screen]
        return await _ok(s, label, screen, layout.ok, layout.cancel)
    finally:
        await _leave_all(s)


async def confirm_group(s: _Session, change: GroupChange) -> DemandGroup:
    """Read a group back (3311, cancelled) and compare.

    Returns
    -------
        The group as the iTM shows it now.

    Raises
    ------
        ItmCommandError: It doesn't show the change.

    """
    try:
        screen = await _open_setup_for(s, change.control, change.group)
        got = await _read_group(s, screen, change.control, change.group.strip())
    finally:
        await _leave_all(s)
    wrong = []
    if change.members is not None and set(got.members) != {
        m.strip() for m in change.members
    }:
        wrong.append(f"members {list(got.members)}")
    for name, shown, wanted in (
        ("condition", got.condition, change.condition),
        ("resume", got.resume, change.resume),
    ):
        if wanted is not None and (shown or "").casefold() != wanted.casefold():
            wrong.append(f"{name} {shown}")
    if wrong:
        msg = f"{change.group}: the iTM did not take the group: {wrong}"
        raise ItmCommandError(msg)
    return got


# -- energy saving suspend (1400) ----------------------------------------------------


async def _open_suspend(s: _Session) -> bool:
    """Open 1400 with the main menu bar's energy saving button (also while hidden).

    Returns
    -------
        Whether it opened; False (nothing clicked) when the button is disabled.

    """
    if s.state.get(ScreenID.MAINMENU, MM_ENERGY_SAVING_BUTTON, "ENABLED") == 0:
        return False
    reply = await s.send(ScreenID.MAINMENU, MM_ENERGY_SAVING_BUTTON)
    if SUSPEND not in s.state.screens:
        await _dismiss(s, ScreenID.MAINMENU, reply)
        return False
    return True


async def read_energy_saving(s: _Session) -> EnergySaving:
    """Read the energy saving indicator and the suspend screen (1400), cancelled.

    Returns
    -------
        What they show.

    """
    active = s.indicators().energy_saving
    try:
        if not await _open_suspend(s):
            return EnergySaving(active, None, None)
        return EnergySaving(
            active=active,
            suspend_status=_string(s.state, SUSPEND, SS_STATUS),
            suspend_enabled=s.state.get(SUSPEND, SS_BUTTON, "ENABLED") != 0,
        )
    finally:
        await _close(s, SUSPEND, SS_CANCEL)


async def set_suspended(s: _Session, *, suspended: bool) -> bool:
    """Suspend energy saving (while active) or resume it (while suspended).

    Returns
    -------
        True when sent (or already so), False in a dry run.

    """
    if s.indicators().energy_saving is not suspended:
        return True
    verb = "suspend" if suspended else "resume"
    try:
        if not await _open_suspend(s):
            msg = "the energy saving suspend screen did not open"
            raise ItmCommandError(msg)
        s.refuse_disabled(SUSPEND, SS_BUTTON, f"{verb} energy saving")
        reply = await s.write(f"{verb} energy saving", SUSPEND, SS_BUTTON)
        if reply is None:
            return False
        await s.answer_confirm(SUSPEND, reply)
        return True
    finally:
        await _close(s, SUSPEND, SS_CANCEL)


# -- service screens (after a service login) -----------------------------------------


async def _open_service(
    s: _Session, button: int, screens: tuple[int, ...]
) -> int | None:
    """Press a service tab button; return the screen that opened, if any."""
    await s.open_menu()
    if s.state.get(MenuScreen.MENULIST, button, "ENABLED") == 0:
        return None
    reply = await s.send(MenuScreen.MENULIST, button)
    for screen in screens:
        if screen in s.state.screens:
            return screen
    await _dismiss(s, MenuScreen.MENULIST, reply)
    return None


async def read_energy_saving_status(s: _Session) -> EnergySavingStatus | None:
    """Read the energy saving status (1508), then close it.

    Returns
    -------
        What it shows; None when it doesn't open.

    """
    try:
        if not await _open_service(s, MB_ENERGY_SAVING_STATUS, (SERVICE_STATUS,)):
            return None
        return energy_saving_status_from_screen(s.state)
    finally:
        await _close(s, SERVICE_STATUS, EST_CANCEL)


async def disable_energy_saving(s: _Session) -> bool:
    """Press 1508's "disable energy saving" (a service write) and confirm.

    Returns
    -------
        True when sent, False in a dry run.

    """
    try:
        if not await _open_service(s, MB_ENERGY_SAVING_STATUS, (SERVICE_STATUS,)):
            msg = "the energy saving status screen did not open"
            raise ItmCommandError(msg)
        s.refuse_disabled(SERVICE_STATUS, EST_DISABLE, "disable energy saving")
        reply = await s.write("disable energy saving", SERVICE_STATUS, EST_DISABLE)
        if reply is None:
            return False
        await s.answer_confirm(SERVICE_STATUS, reply)
        return True
    finally:
        await _close(s, SERVICE_STATUS, EST_CANCEL)


async def _leave_service(s: _Session) -> None:
    await _close(s, PORT_PICKER, PP_CANCEL)
    await _close(s, PULSE_SETUP, PS_CANCEL)
    await _close(s, INTERLOCK_SETUP, IL_CANCEL)


async def _open_demand_setup(s: _Session) -> int | None:
    return await _open_service(s, MB_DEMAND_SETUP, (PULSE_SETUP, INTERLOCK_SETUP))


async def read_demand_service(s: _Session) -> DemandServiceSettings:
    """Read the power limit control setup (1512 or 1513) and the ports 1514 offers.

    Returns
    -------
        What the screen that opened shows; empty when none did.

    """
    try:
        screen = await _open_demand_setup(s)
        if screen is None:
            return DemandServiceSettings()
        buttons = PS_PORT_BUTTONS if screen == PULSE_SETUP else IL_PORT_BUTTONS
        pulse = pulse_setup_from_screen(s.state) if screen == PULSE_SETUP else None
        interlock = (
            interlock_ports_from_screen(s.state) if screen == INTERLOCK_SETUP else None
        )
        ports: tuple[str, ...] = ()
        if s.state.get(screen, buttons[0], "ENABLED") != 0:
            await _open_below(s, screen, buttons[0], PORT_PICKER)
            ports = ports_from_screen(s.state)
            await _close(s, PORT_PICKER, PP_CANCEL)
        return DemandServiceSettings(pulse, interlock, ports)
    finally:
        await _leave_service(s)


async def _set_port(  # noqa: PLR0913  # pylint: disable=too-many-arguments
    s: _Session, screen: int, *, button: int, delete: int, port: str, what: str
) -> bool:
    """Clear a port ("") or pick it in 1514, both sent as writes.

    A dry run plans them (1514 is cancelled), so the rest of the screen is still
    checked and planned.

    Returns
    -------
        False in a dry run, else True.

    """
    if not port:
        return await s.write(f"clear {what}", screen, delete) is not None
    await _open_below(s, screen, button, PORT_PICKER)
    names = ports_from_screen(s.state)
    await s.select_row(PORT_PICKER, PP_LIST, _row(list(names), port, "port"))
    s.refuse_disabled(PORT_PICKER, PP_OK, what)
    reply = await s.write(f"pick {what} {port}", PORT_PICKER, PP_OK)
    if reply is None:
        await _close(s, PORT_PICKER, PP_CANCEL)
        return False
    await s.answer_confirm(PORT_PICKER, reply)
    s.state.screens.pop(PORT_PICKER, None)
    return True


async def _require_screen(s: _Session, wanted: int, what: str) -> None:
    screen = await _open_demand_setup(s)
    if screen != wanted:
        msg = f"the service tab did not open the {what} (the energy save mode decides)"
        raise ItmCommandError(msg)


async def set_pulse_setup(s: _Session, change: PulseChange) -> bool:
    """Change the demand pulse setup (1512) and OK.

    Returns
    -------
        True when saved, False in a dry run.

    """
    try:
        await _require_screen(s, PULSE_SETUP, "demand pulse setup")
        for index, port in enumerate((change.input_port1, change.input_port2)):
            if port is not None:
                await _set_port(
                    s,
                    PULSE_SETUP,
                    button=PS_PORT_BUTTONS[index],
                    delete=PS_PORT_DELETES[index],
                    port=port.strip(),
                    what=f"input port {index + 1}",
                )
        for value, button, what in (
            (change.pulse_coefficient, PS_COEFFICIENT_BUTTON, "pulse coefficient"),
            (change.return_value, PS_RETURN_BUTTON, "return value"),
            (change.cutoff_value, PS_CUTOFF_BUTTON, "cut-off value"),
        ):
            if value is not None:
                await _number(s, PULSE_SETUP, button, value, what)
        if change.sync_external is not None:
            radio = PS_SYNC_EXTERNAL if change.sync_external else PS_SYNC_PERIOD
            await s.click(PULSE_SETUP, radio, "synchronisation method")
        return await _ok(s, "save demand pulse setup", PULSE_SETUP, PS_OK, PS_CANCEL)
    finally:
        await _leave_service(s)


def check_pulse_setup(got: DemandPulseSetup | None, change: PulseChange) -> None:
    """Compare a pulse setup read back with the change.

    Raises
    ------
        ItmCommandError: It doesn't show the change.

    """
    if got is None:
        msg = "the demand pulse setup did not open for the read-back"
        raise ItmCommandError(msg)
    wrong = []
    for index, port in enumerate((change.input_port1, change.input_port2)):
        if port is not None and (got.input_ports[index] or "") != port.strip():
            wrong.append(f"input port {index + 1} {got.input_ports[index]}")
    for name, shown, wanted in (
        ("pulse coefficient", got.pulse_coefficient, change.pulse_coefficient),
        ("return value", got.return_value, change.return_value),
        ("cut-off value", got.cutoff_value, change.cutoff_value),
    ):
        if wanted is not None and not _close_to(shown, wanted):
            wrong.append(f"{name} {shown}")
    if change.sync_external is not None and got.sync_external != change.sync_external:
        wrong.append(f"external sync {got.sync_external}")
    if wrong:
        msg = f"the iTM did not take the demand pulse setup: {wrong}"
        raise ItmCommandError(msg)


async def set_interlock_ports(s: _Session, ports: Mapping[int, str]) -> bool:
    """Change the demand control interlock's input ports (1513) and OK.

    Args:
    ----
        s: The open session.
        ports: Port 1-3 -> the port to pick, or "" to clear it.

    Returns:
    -------
        True when saved, False in a dry run.

    """
    try:
        await _require_screen(s, INTERLOCK_SETUP, "demand control interlock setup")
        for number, port in sorted(ports.items()):
            if not 1 <= number <= len(IL_PORT_BUTTONS):
                msg = f"there is no interlock input port {number}"
                raise ItmCommandError(msg)
            await _set_port(
                s,
                INTERLOCK_SETUP,
                button=IL_PORT_BUTTONS[number - 1],
                delete=IL_PORT_DELETES[number - 1],
                port=port.strip(),
                what=f"interlock input port {number}",
            )
        return await _ok(
            s, "save demand control interlock", INTERLOCK_SETUP, IL_OK, IL_CANCEL
        )
    finally:
        await _leave_service(s)


def check_interlock_ports(
    got: tuple[str | None, ...] | None, ports: Mapping[int, str]
) -> None:
    """Compare interlock ports read back with the change.

    Raises
    ------
        ItmCommandError: They don't show the change.

    """
    if got is None:
        msg = "the demand control interlock setup did not open for the read-back"
        raise ItmCommandError(msg)
    wrong = {n: got[n - 1] for n, p in ports.items() if (got[n - 1] or "") != p.strip()}
    if wrong:
        msg = f"the iTM did not take the interlock ports: {wrong}"
        raise ItmCommandError(msg)


# -- the client's methods ------------------------------------------------------------


class DemandMixin(ClientCore):
    """ItmClient's demand (power limit) control and energy saving methods.

    Every write runs in its own session and is confirmed by a read in a fresh one;
    with ``dry_run`` it returns None and only plans, with ``read_only`` it raises
    ReadOnlyError before anything is sent. Tested against the simulated iTM only.
    """

    def _demand_writable(self) -> None:
        if self.read_only:
            msg = "the client is read-only"
            raise ReadOnlyError(msg)

    def _demand_service_guard(self, *, allowed: bool) -> None:
        """Refuse a service write when read only, or unless allowed (or a dry run)."""
        self._demand_writable()
        if not self.dry_run and not allowed:
            msg = (
                "this changes the installer's service settings; use a dry run, or "
                "pass allow_service_change=True"
            )
            raise ItmCommandError(msg)

    async def _demand_write(self, call: Callable[[_Session], Awaitable[Any]]) -> None:
        self._demand_writable()
        await self._in_session(lambda s: _ignore(call(s)))

    async def _service_session(
        self, password: str, call: Callable[[_Session], Awaitable[_T]]
    ) -> _T:
        async def run(s: _Session) -> _T:
            await s.service_login(password)
            return await call(s)

        return await self._in_session(run)

    # -- demand control ---------------------------------------------------------------
    async def fetch_demand_control(
        self, only: Iterable[str] | None = None
    ) -> DemandControl | None:
        """Read demand (power limit) control, in one session. Read only.

        Args:
        ----
            only: The DemandControl fields to read besides the status; None: all.

        Returns:
        -------
            What was read; None when the menu's power limit button opens nothing
            (as on an iTM without the dealer option and a pulse input).

        """
        wanted = wanted_parts(only)
        return await self._in_session(lambda s: read_demand_control(s, wanted))

    async def set_demand_target(self, power: float) -> DemandStatus | None:
        """Change the target power (3312) and OK.

        Args:
        ----
            power: The target power, in the unit the iTM shows (DemandStatus).

        Returns:
        -------
            The status as read back in a fresh session; None in a dry run.

        """
        self._experimental("set the demand control target")
        await self._demand_write(lambda s: set_target(s, power))
        if self.dry_run:
            return None
        return await self._in_session(lambda s: confirm_target(s, power))

    async def set_demand_forced_level(self, level: str) -> str | None:
        """Pick the setup's forced level (3301); the pick saves.

        Args:
        ----
            level: An option as the iTM shows it.

        Returns:
        -------
            The forced level as read back; None in a dry run.

        Raises:
        ------
            ItmCommandError: Not offered, or the iTM did not take it.

        """
        self._experimental("set the demand control forced level")
        await self._demand_write(lambda s: set_forced_level(s, level))
        if self.dry_run:
            return None
        got = await self._in_session(read_forced_level)
        if (got or "").casefold() != level.strip().casefold():
            msg = f"the iTM did not take the forced level: {got}, not {level}"
            raise ItmCommandError(msg)
        return got

    async def set_demand_level_setup(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        control: str,
        *,
        group: str | None = None,
        start_level: str | None = None,
        levels: Mapping[int, str] | None = None,
        upper_cool: float | None = None,
        lower_heat: float | None = None,
    ) -> DemandLevelSetup | None:
        """Change a control's temperature, capacity or on/off setup for a group.

        Args:
        ----
            control: The control, as the setup's list shows it (first column).
            group: The group; None for the one the screen shows first.
            start_level: The start level, an option as shown.
            levels: Level (1-8) -> an option as shown (shift or capacity).
            upper_cool: The upper cooling limit (temperature setup only).
            lower_heat: The lower heating limit (temperature setup only).

        Returns:
        -------
            The setup as read back; None in a dry run.

        """
        self._experimental("change a demand control level setup")
        change = LevelChange(
            control, group, start_level, dict(levels or {}), upper_cool, lower_heat
        )
        await self._demand_write(lambda s: set_level_setup(s, change))
        if self.dry_run:
            return None
        return await self._in_session(lambda s: confirm_level_setup(s, change))

    async def set_demand_group(
        self,
        control: str,
        group: str,
        *,
        members: Iterable[str] | None = None,
        condition: str | None = None,
        resume: str | None = None,
    ) -> DemandGroup | None:
        """Change a demand group's members, condition and resume (3311).

        Args:
        ----
            control: The control, as the setup's list shows it.
            group: The group, as the setup's group list shows it.
            members: Exactly these points (None: unchanged).
            condition: An option as shown.
            resume: An option as shown.

        Returns:
        -------
            The group as read back; None in a dry run.

        """
        self._experimental("change a demand control group")
        change = GroupChange(
            control,
            group,
            None if members is None else tuple(members),
            condition,
            resume,
        )
        await self._demand_write(lambda s: set_group(s, change))
        if self.dry_run:
            return None
        return await self._in_session(lambda s: confirm_group(s, change))

    # -- energy saving ----------------------------------------------------------------
    async def fetch_energy_saving(self, password: str | None = None) -> EnergySaving:
        """Read energy saving: indicator, suspend screen and status (1508). Read only.

        The status (1508) is a service screen: it is read only with the password.

        Args:
        ----
            password: The service password, to read 1508 too; sent once, never
                stored or logged.

        Returns:
        -------
            What the screens show.

        """

        async def read(s: _Session) -> EnergySaving:
            saving = await read_energy_saving(s)
            if password is None:
                return saving
            await s.service_login(password)
            return replace(saving, service=await read_energy_saving_status(s))

        return await self._in_session(read)

    async def suspend_energy_saving(self, *, suspend: bool = True) -> bool | None:
        """Suspend energy saving (1400), or resume it with ``suspend=False``.

        Args:
        ----
            suspend: Suspend (True) or resume (False).

        Returns:
        -------
            Whether energy saving is active as read back; None in a dry run.

        Raises:
        ------
            ItmCommandError: The iTM did not take it.

        """
        self._experimental("suspend or resume energy saving")
        await self._demand_write(lambda s: set_suspended(s, suspended=suspend))
        if self.dry_run:
            return None
        active = (await self.fetch_energy_saving()).active
        if active is suspend:
            msg = f"the iTM did not {'suspend' if suspend else 'resume'} energy saving"
            raise ItmCommandError(msg)
        return active

    async def disable_energy_saving(
        self, password: str, *, allow_service_change: bool = False
    ) -> bool | None:
        """Press the energy saving status's "disable energy saving" (1508).

        Args:
        ----
            password: The service password.
            allow_service_change: Needed unless this is a dry run.

        Returns:
        -------
            Whether energy saving is active as read back; None in a dry run.

        Raises:
        ------
            ItmCommandError: Not allowed, or the iTM did not take it.

        """
        self._experimental("disable energy saving")
        self._demand_service_guard(allowed=allow_service_change)
        await self._service_session(
            password, lambda s: _ignore(disable_energy_saving(s))
        )
        if self.dry_run:
            return None
        active = (await self.fetch_energy_saving()).active
        if active:
            msg = "the iTM did not disable energy saving"
            raise ItmCommandError(msg)
        return active

    # -- the power limit control setup (service) --------------------------------------
    async def fetch_demand_service_settings(
        self, password: str
    ) -> DemandServiceSettings:
        """Read the service tab's power limit control setup (1512 or 1513). Read only.

        Args:
        ----
            password: The service password; sent once, never stored or logged.

        Returns:
        -------
            The setup the energy save mode opens, and the ports 1514 offers.

        """
        return await self._service_session(password, read_demand_service)

    async def set_demand_pulse_setup(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        password: str,
        *,
        input_port1: str | None = None,
        input_port2: str | None = None,
        pulse_coefficient: float | None = None,
        sync_external: bool | None = None,
        return_value: float | None = None,
        cutoff_value: float | None = None,
        allow_service_change: bool = False,
    ) -> DemandPulseSetup | None:
        """Change the demand pulse setup (1512) and OK.

        Args:
        ----
            password: The service password.
            input_port1: A port as 1514 lists it; "" clears it.
            input_port2: A port as 1514 lists it; "" clears it.
            pulse_coefficient: The pulse coefficient.
            sync_external: Synchronise on an external signal (else the period).
            return_value: The return value.
            cutoff_value: The cut-off value.
            allow_service_change: Needed unless this is a dry run.

        Returns:
        -------
            The setup as read back; None in a dry run.

        """
        self._experimental("change the demand pulse setup")
        self._demand_service_guard(allowed=allow_service_change)
        change = PulseChange(
            input_port1,
            input_port2,
            pulse_coefficient,
            sync_external,
            return_value,
            cutoff_value,
        )
        await self._service_session(
            password, lambda s: _ignore(set_pulse_setup(s, change))
        )
        if self.dry_run:
            return None
        got = (await self.fetch_demand_service_settings(password)).pulse
        check_pulse_setup(got, change)
        return got

    async def set_demand_interlock_ports(
        self,
        password: str,
        ports: Mapping[int, str],
        *,
        allow_service_change: bool = False,
    ) -> tuple[str | None, ...] | None:
        """Change the demand control interlock's input ports (1513) and OK.

        Args:
        ----
            password: The service password.
            ports: Port number (1-3) -> a port as 1514 lists it, or "" to clear it.
            allow_service_change: Needed unless this is a dry run.

        Returns:
        -------
            The ports as read back; None in a dry run.

        """
        self._experimental("change the demand control interlock ports")
        self._demand_service_guard(allowed=allow_service_change)
        wanted = dict(ports)
        await self._service_session(
            password, lambda s: _ignore(set_interlock_ports(s, wanted))
        )
        if self.dry_run:
            return None
        got = (await self.fetch_demand_service_settings(password)).interlock_ports
        check_interlock_ports(got, wanted)
        return got
