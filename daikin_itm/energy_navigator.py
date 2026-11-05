"""The energy navigator's screens, read only.

The budget overview (3200) with its three tabs (yearly budget, monthly budget,
compared with the previous fiscal year), the planned and the actual consumption
behind its planning settings (3201 -> 3202, 3203), and from the equipment management
screen (3210) the management rules (3213) and the sampling targets (3218).

Opt-in (named in ``only``; their paths come from the web UI's script and the
simulated iTM, not yet from a real one): the energy types and groups behind the
display settings (3200 -> 3204 -> 3205/3207, each one's settings in 3206/3208), the
equipment details (3210 -> 3211, only while its button is enabled) and each management
rule's editor (3213 -> 3214, after selecting the rule's row), each management rule's
time ranges per day (``rule_settings``: 3214 -> 3217, after selecting the day's radio
button, each range read by selecting its row) and, behind the service login, the
installer's settings (``installer``: the service tab's button 106 -> 3220, then 3221
with 3222, 3223 and 3224). 3220's data clear button (part 9) is never clicked.

Every screen is left with its Close or Cancel, never OK: these screens also create,
copy, delete and save, and none of that is sent. Tabs are switched and list rows
selected (display choices the web UI makes too); the energy type and unit combos are
not touched, so only the type the iTM shows is read. 3202 and 3203 open behind a
PROGRESS dialog, waited out like the history's. The writes are in ``energy_edit``.

Texts are kept as shown (``ShownValue.text``); numbers are parsed from them only
where the text is a plain number (``"0.0 "``, ``"12 %"``, ``"2026 : 1234.5(kWh)"``),
``None`` otherwise (``"--"``). The graphs and pattern bars are returned as decoded,
uninterpreted.

The screen and part IDs come from the web UI's script.js and a read-only look at a
real iTM without the energy navigator licence: its lists and graphs were empty, so
how rows and bars look with data is assumed from the web UI's script.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .exceptions import ItmCommandError
from .menu import MB_ENERGY_NAVIGATOR, MenuScreen
from .protocol import DataType, EventType
from .system import MB_ENERGY_EQUIPMENT

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable

    from .protocol import Reply
    from .session import _Session
    from .state import UIState

# Screens below the menu screens 3200 and 3210.
PLAN_SETTINGS, PLANNED, ACTUAL = 3201, 3202, 3203
DISPLAY_SETTINGS, TYPE_LIST, TYPE_SETTING = 3204, 3205, 3206
GROUP_LIST, GROUP_SETTING = 3207, 3208
DETAILS, RULES, RULE_EDITOR, SAMPLING_TARGETS = 3211, 3213, 3214, 3218
COPY_SELECT, CALENDAR_COPY, RULE_SETTING = 3215, 3216, 3217
INSTALLER, ROOM_TEMP, ANALOG_SELECT, COLLECT_START, CONVERSION = (
    3220,
    3221,
    3222,
    3223,
    3224,
)
# The service tab's energy navigator button (menu list, after a service login).
MB_ENERGY_INSTALLER = 106

# Budget overview (3200): the display mode button opens 3204, tab 1's planning
# settings button 3201.
BG_DISPLAY, BG_DISPLAY_MODE, BG_TABS, BG_CLOSE, BG_PLANNING = 2, 3, 4, 19, 18
BG_TAB_LABELS = ((5, 9, 10, 11), (20, 24, 25, 26), (34, 35, 36))
BG_TAB_GRAPH = (12, 27, 38)
BG_TAB_TYPE = (15, 30, 40)
BG_TAB_TYPE_LABEL = (14, 29, 39)
BG_TAB_UNIT = (17, 32, 42)
BG_TAB_UNIT_LABEL = (16, 31, 41)
# Display settings (3204): types -> 3205, groups -> 3207.
DS_TYPES, DS_GROUPS, DS_CLOSE = 2, 4, 5
# Energy type list (3205) and group list (3207) share their part IDs; Edit opens
# 3206 / 3208.
EL_LIST, EL_CREATE, EL_COPY, EL_DELETE = 2, 4, 5, 6
EL_RENAME, EL_EDIT, EL_CLOSE = 7, 8, 9
# Energy type settings (3206).
TS_UNIT, TS_TYPE, TS_CO2, TS_CO2_UNIT = 2, 4, 6, 7
TS_CONVERSION, TS_CONVERSION_UNIT, TS_OK, TS_CANCEL = 10, 11, 13, 14
# Energy group settings (3208).
GS_MEMBERS, GS_AVAILABLE, GS_TYPE, GS_OK, GS_CANCEL = 2, 8, 10, 11, 12
# Equipment details (3211).
DT_TABS, DT_DAY, DT_GRAPH, DT_LIST, DT_CLOSE = 1, 4, 7, 11, 9
# Rule editor (3214); the bars and radios start on Sunday, the special days'
# radio labels are their names.
RE_TABS, RE_CANCEL = 1, 30
RE_WEEKDAY_BARS = (14, 15, 16, 17, 18, 19, 20)
RE_SPECIAL_RADIOS = (33, 34, 35, 36, 37)
RE_SPECIAL_BARS = (43, 44, 45, 46, 47)
RE_CALENDAR = 62
# Rule editor (3214): Edit opens 3217 for the selected day.
RE_WEEKDAY_RADIOS = (2, 3, 4, 5, 6, 7, 8)
RE_EDIT_WEEKDAY, RE_EDIT_SPECIAL, RE_OK = 23, 50, 29
WEEKDAYS = (
    "sunday",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
)
# Calendar copy (3216); the target list takes several rows.
CC_FROM, CC_LIST, CC_OK, CC_CANCEL = 2, 4, 14, 15
# Rule settings (3217); Modify changes the selected row.
RS_LIST, RS_DELETE = 2, 3
RS_START, RS_START_BUTTON, RS_END, RS_END_BUTTON = 7, 8, 10, 11
RS_SETPOINT, RS_COOL, RS_HEAT = 12, 13, 14
RS_COOL_TEXT, RS_COOL_BUTTON, RS_HEAT_TEXT, RS_HEAT_BUTTON = 15, 16, 17, 18
RS_ADD, RS_MODIFY, RS_OK, RS_CANCEL = 19, 20, 21, 22
# Installer settings (3220): Change -> 3221, 3223, 3224. Part 9 clears the energy
# navigator's data: never clicked.
IN_ROOM_TEMP, IN_COLLECT, IN_CONVERSION, IN_CLOSE, IN_DATA_CLEAR = 4, 5, 6, 7, 9
# Room temperature (3221): the analog input's Change opens 3222; 4-8 are
# disabled until a row is selected.
RT_LIST, RT_OWN, RT_ANALOG, RT_ANALOG_TEXT = 2, 4, 5, 6
RT_ANALOG_BUTTON, RT_APPLY, RT_OK, RT_CANCEL = 7, 8, 9, 10
# Analog input selection (3222).
AS_LIST, AS_OK, AS_CANCEL = 2, 3, 4
# Data collection start month (3223).
CM_MONTH, CM_OK, CM_CANCEL = 2, 3, 4
# Energy conversion (3224): OK is disabled while nothing changed.
CV_FIELDS = {"name": (2, 3), "one": (6, 7), "thousand": (9, 10), "million": (12, 13)}
CV_OK, CV_CANCEL = 14, 15
# Planning settings (3201): planned -> 3202, actual -> 3203.
PS_PLANNED, PS_ACTUAL, PS_CLOSE = 2, 4, 5
# Planned consumption (3202), per tab a fiscal year; OK is 14.
PL_TYPE, PL_TABS, PL_CANCEL = 2, 3, 15
PL_TAB_PARTS = ((4, 11, 6, 7, 10, 13), (18, 25, 20, 21, 24, 27))
# Actual consumption (3203); OK is 15.
AC_TYPE, AC_TABS, AC_CANCEL = 2, 3, 16
AC_TAB_PARTS = ((5, 8, 9), (21, 24, 25))
# Equipment management (3210): sampling target -> 3218, rules -> 3213, Details
# -> 3211 (enabled only with data). Rules (3213): Edit -> 3214.
EQ_TARGET, EQ_RULES, EQ_CLOSE = 9, 10, 20
EQ_DETAILS = (16, 34)
RL_LIST, RL_EDIT, RL_CLOSE = 2, 8, 10
RL_CREATE, RL_COPY, RL_DELETE, RL_RENAME, RL_CALENDAR_COPY = 4, 5, 6, 7, 9
ST_LIST, ST_CANCEL = 2, 4

# What fetch_energy_navigator reads by default (validated on a real iTM without
# data), and every part it can read.
DEFAULT_PARTS = ("budget", "planned", "actual", "rules", "sampling_targets")
PARTS = (
    *DEFAULT_PARTS,
    "energy_types",
    "energy_groups",
    "details",
    "rule_editor",
    "rule_settings",
    "installer",
)

_NUMBER = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*(?:[^\d\s.,-][^\d]*)?")


# -- models ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ShownValue:
    """A text as the iTM shows it, and its number where the text is a plain one."""

    text: str
    value: float | None


@dataclass(frozen=True)
class BudgetTab:
    """A tab of the budget overview, as shown."""

    name: str
    labels: tuple[ShownValue, ...]  # the tab's labels: year, budget, totals
    energy_type: str | None  # the energy type combo's shown option
    unit: str | None  # the energy unit combo's shown option (e.g. energy or CO2)
    graph: tuple[Any, ...]  # the BARLINE_GRAPH as decoded (axes, then the bars)
    type_label: str | None = None  # the label before the energy type combo
    unit_label: str | None = None  # the label before the energy unit combo


@dataclass(frozen=True)
class EnergyBudget:
    """The budget overview (3200): what is displayed, and its tabs."""

    display: str | None
    tabs: tuple[BudgetTab, ...]


@dataclass(frozen=True)
class MonthValue:
    """A row of a month list: the month and its value, as shown."""

    month: str
    value: ShownValue


@dataclass(frozen=True)
class PlannedYear:
    """A fiscal year's planned consumption."""

    name: str  # the tab, e.g. "01/2025-12/2025"
    ratio: ShownValue | None  # relative to the previous year
    ratio_unit: str | None  # e.g. "%"
    expected: ShownValue | None  # the expected yearly consumption
    per_month: bool | None  # set per month (True) or for the year (False)
    months: tuple[MonthValue, ...]  # the target per month


@dataclass(frozen=True)
class PlannedConsumption:
    """The planned consumption setup (3202) for the energy type shown."""

    energy_type: str | None
    years: tuple[PlannedYear, ...]


@dataclass(frozen=True)
class ActualYear:
    """A fiscal year's actual consumption."""

    name: str
    months: tuple[MonthValue, ...]
    co2_factor: ShownValue | None
    co2_unit: str | None  # as shown, e.g. "kg-CO2/kWh"


@dataclass(frozen=True)
class ActualConsumption:
    """The actual consumption screen (3203) for the energy type shown."""

    energy_type: str | None
    years: tuple[ActualYear, ...]


@dataclass(frozen=True)
class SamplingTarget:
    """A zone the equipment management can sample, as its list shows it."""

    level: int | None
    name: str
    selected: bool  # the row the list marks as chosen


@dataclass(frozen=True)
class EnergyType:
    """An energy type (3205) and its settings (3206); None where not shown.

    The settings stay None when the list's Edit button is disabled for it.
    """

    name: str
    unit: str | None = None  # the unit combo's shown option
    energy_type: str | None = None  # the energy type combo's shown option
    co2_factor: ShownValue | None = None
    co2_unit: str | None = None
    conversion: ShownValue | None = None
    conversion_unit: str | None = None


@dataclass(frozen=True)
class EnergyGroup:
    """An energy group (3207) and its settings (3208), list rows as shown.

    ``members`` and ``available`` stay None when the list's Edit button is disabled.
    """

    name: str
    energy_type: str | None = None
    members: tuple[tuple[str, ...], ...] | None = None
    available: tuple[tuple[str, ...], ...] | None = None


@dataclass(frozen=True)
class EquipmentDetails:
    """The equipment details (3211): the day shown, its graph and the occurrences."""

    day: str | None
    graph: tuple[Any, ...]  # as decoded, uninterpreted
    occurrences: tuple[tuple[str, ...], ...]  # list rows as shown


@dataclass(frozen=True)
class RuleEditor:
    """A management rule as its editor (3214) shows it.

    The pattern bars are returned as decoded (TIMELINE_BAR entries), uninterpreted.
    """

    name: str
    weekdays: tuple[tuple[Any, ...], ...]  # Sunday first
    special_days: tuple[tuple[str, tuple[Any, ...]], ...]  # (name, bar)
    calendar: tuple[tuple[str, ...], ...]  # list rows as shown


@dataclass(frozen=True)
class RuleRange:
    """A time range of a management rule's day (3217).

    ``cool`` and ``heat`` are the setpoints managed in the range (None: not managed);
    the range manages setpoints at all when either is set.
    """

    start: str  # "HH:MM"
    end: str  # "HH:MM"
    cool: float | None = None
    heat: float | None = None


@dataclass(frozen=True)
class RuleDay:
    """A management rule's day as its rule settings screen (3217) shows it."""

    rule: str
    day: str  # a weekday ("sunday" .. "saturday") or a special day's name
    rows: tuple[tuple[str, ...], ...]  # the time range list's rows, as shown
    ranges: tuple[RuleRange, ...]  # each row's values, read by selecting it


@dataclass(frozen=True)
class RoomTemperaturePoint:
    """A point's room temperature source for the energy navigator (3221)."""

    name: str
    setting: str  # the list's setting column, as shown
    own_sensor: bool | None  # the unit's return air temperature (True)
    analog_point: str | None  # the analog input's name, while one is used


@dataclass(frozen=True)
class ConversionType:
    """The energy conversion (3224): its name and unit texts; None keeps a value."""

    name: str | None = None
    one: str | None = None  # the unit for 1
    thousand: str | None = None  # the unit for 1000
    million: str | None = None  # the unit for 1000000


@dataclass(frozen=True)
class EnergyInstallerSettings:
    """The installer's energy navigator settings (3220 and the screens below it)."""

    room_temperature: tuple[RoomTemperaturePoint, ...] | None = None
    analog_points: tuple[tuple[str, ...], ...] | None = None  # 3222, when offered
    collection_start: str | None = None
    collection_months: tuple[str, ...] = ()
    conversion: ConversionType | None = None


@dataclass(frozen=True)
class EnergyNavigator:  # pylint: disable=too-many-instance-attributes
    """What fetch_energy_navigator read; None for what was not read or not there."""

    budget: EnergyBudget | None = None
    planned: PlannedConsumption | None = None
    actual: ActualConsumption | None = None
    rules: tuple[str, ...] | None = None
    sampling_targets: tuple[SamplingTarget, ...] | None = None
    energy_types: tuple[EnergyType, ...] | None = None
    energy_groups: tuple[EnergyGroup, ...] | None = None
    details: EquipmentDetails | None = None  # None too while Details is disabled
    rule_editor: tuple[RuleEditor, ...] | None = None
    rule_settings: tuple[RuleDay, ...] | None = None
    installer: EnergyInstallerSettings | None = None


# -- readers --------------------------------------------------------------------------


def parse_shown(text: str) -> ShownValue:
    """Keep a shown text and parse its number where it is a plain one.

    A text with a colon ("2026 : 1234.5(kWh)") is parsed after it; a unit after the
    number is ignored. "--" and other texts give None.
    """
    shown = html.unescape(text)
    t = shown.rsplit(":", 1)[-1].strip()
    m = _NUMBER.fullmatch(t)
    return ShownValue(
        text=shown, value=float(m.group(1).replace(",", ".")) if m else None
    )


def _string(state: UIState, screen: int, part: int, ptype: str) -> str | None:
    value = state.get(screen, part, ptype)
    if not isinstance(value, str):
        return None
    return html.unescape(value).strip() or None


def _shown(state: UIState, screen: int, part: int, ptype: str) -> ShownValue | None:
    value = state.get(screen, part, ptype)
    return parse_shown(value) if isinstance(value, str) else None


def option(state: UIState, screen: int, part: int) -> str | None:
    """Return a combo's shown option.

    A combo whose selection the iTM never sent shows its first option: replies are
    deltas, and a real iTM sent the budget's display combo without one.
    """
    options = state.get(screen, part, "COMBO_STRS") or []
    idx = state.get(screen, part, "SINGLE_SELIDX", 0)
    if not isinstance(idx, int) or not 0 <= idx < len(options):
        return None
    return html.unescape(str(options[idx])).strip()


def tab_names(state: UIState, screen: int, part: int) -> list[str]:
    """Return a tab part's tab names."""
    return [html.unescape(str(t)) for t in state.get(screen, part, "TAB_NAME") or []]


def list_rows(
    state: UIState, screen: int, part: int
) -> tuple[list[list[str]], list[int]]:
    """Return a list's rows (their texts) and the selected row indexes."""
    value = state.get(screen, part, "LIST_ROW") or {}
    rows = [
        [html.unescape(str(t)) for t in r.get("text", [])]
        for r in value.get("rows", [])
    ]
    return rows, list(value.get("selected", []))


def _months(state: UIState, screen: int, part: int) -> tuple[MonthValue, ...]:
    return tuple(
        MonthValue(month=cells[0].strip(), value=parse_shown(cells[1]))
        for cells in (
            [*row, "", ""] for row in list_rows(state, screen, part)[0] if row
        )
    )


def _checked(state: UIState, screen: int, on: int, off: int) -> bool | None:
    if state.get(screen, on, "CHECK_STATE") == 1:
        return True
    if state.get(screen, off, "CHECK_STATE") == 1:
        return False
    return None


def budget_from_screen(state: UIState) -> EnergyBudget:
    """Read the budget overview (3200): per tab its labels, graph, type and unit."""
    s = MenuScreen.ENERGY_NAVIGATOR
    tabs = []
    for i, name in enumerate(tab_names(state, s, BG_TABS)[: len(BG_TAB_LABELS)]):
        labels = (_shown(state, s, p, "LABEL") for p in BG_TAB_LABELS[i])
        tabs.append(
            BudgetTab(
                name=name,
                labels=tuple(v for v in labels if v is not None and v.text.strip()),
                energy_type=option(state, s, BG_TAB_TYPE[i]),
                unit=option(state, s, BG_TAB_UNIT[i]),
                graph=tuple(state.get(s, BG_TAB_GRAPH[i], "BARLINE_GRAPH") or ()),
                type_label=_string(state, s, BG_TAB_TYPE_LABEL[i], "LABEL"),
                unit_label=_string(state, s, BG_TAB_UNIT_LABEL[i], "LABEL"),
            )
        )
    return EnergyBudget(display=option(state, s, BG_DISPLAY), tabs=tuple(tabs))


def planned_from_screen(state: UIState) -> PlannedConsumption:
    """Read the planned consumption setup (3202), every fiscal year tab."""
    years = []
    for name, (year, month, ratio, unit, expected, months) in zip(
        tab_names(state, PLANNED, PL_TABS), PL_TAB_PARTS, strict=False
    ):
        years.append(
            PlannedYear(
                name=name,
                ratio=_shown(state, PLANNED, ratio, "TEXT"),
                ratio_unit=_string(state, PLANNED, unit, "LABEL"),
                expected=_shown(state, PLANNED, expected, "TEXT"),
                per_month=_checked(state, PLANNED, month, year),
                months=_months(state, PLANNED, months),
            )
        )
    return PlannedConsumption(
        energy_type=option(state, PLANNED, PL_TYPE), years=tuple(years)
    )


def actual_from_screen(state: UIState) -> ActualConsumption:
    """Read the actual consumption screen (3203), every fiscal year tab."""
    years = [
        ActualYear(
            name=name,
            months=_months(state, ACTUAL, months),
            co2_factor=_shown(state, ACTUAL, factor, "TEXT"),
            co2_unit=_string(state, ACTUAL, unit, "LABEL"),
        )
        for name, (months, factor, unit) in zip(
            tab_names(state, ACTUAL, AC_TABS), AC_TAB_PARTS, strict=False
        )
    ]
    return ActualConsumption(
        energy_type=option(state, ACTUAL, AC_TYPE), years=tuple(years)
    )


def rules_from_screen(state: UIState) -> tuple[str, ...]:
    """Read the management rule list (3213): the names."""
    return tuple(row[0] for row in list_rows(state, RULES, RL_LIST)[0] if row)


def sampling_targets_from_screen(state: UIState) -> tuple[SamplingTarget, ...]:
    """Read the sampling target list (3218): level and name per row."""
    rows, selected = list_rows(state, SAMPLING_TARGETS, ST_LIST)
    targets = []
    for i, row in enumerate(rows):
        cells = [*row, "", ""]
        level = re.fullmatch(r"\s*(\d+)\s*", cells[0])
        targets.append(
            SamplingTarget(
                level=int(level.group(1)) if level else None,
                name=cells[1].strip(),
                selected=i in selected,
            )
        )
    return tuple(targets)


def _cells(state: UIState, screen: int, part: int) -> tuple[tuple[str, ...], ...]:
    return tuple(
        tuple(c.strip() for c in row) for row in list_rows(state, screen, part)[0]
    )


def list_names(state: UIState, screen: int) -> tuple[str, ...]:
    """Read the names (first column) of an energy type or group list (3205/3207)."""
    return tuple(row[0] for row in _cells(state, screen, EL_LIST) if row)


def energy_type_from_screen(state: UIState, name: str) -> EnergyType:
    """Read an energy type's settings screen (3206)."""
    s = TYPE_SETTING
    return EnergyType(
        name=name,
        unit=option(state, s, TS_UNIT),
        energy_type=option(state, s, TS_TYPE),
        co2_factor=_shown(state, s, TS_CO2, "TEXT"),
        co2_unit=_string(state, s, TS_CO2_UNIT, "LABEL"),
        conversion=_shown(state, s, TS_CONVERSION, "TEXT"),
        conversion_unit=_string(state, s, TS_CONVERSION_UNIT, "LABEL"),
    )


def energy_group_from_screen(state: UIState, name: str) -> EnergyGroup:
    """Read an energy group's settings screen (3208)."""
    s = GROUP_SETTING
    return EnergyGroup(
        name=name,
        energy_type=option(state, s, GS_TYPE),
        members=_cells(state, s, GS_MEMBERS),
        available=_cells(state, s, GS_AVAILABLE),
    )


def _graph(state: UIState, screen: int, part: int) -> tuple[Any, ...]:
    for ptype in ("BARLINE_GRAPH", "POINT_GRAPH"):
        if (value := state.get(screen, part, ptype)) is not None:
            return tuple(value)
    return ()


def details_from_screen(state: UIState) -> EquipmentDetails:
    """Read the equipment details (3211): the day shown, graph, occurrence list."""
    return EquipmentDetails(
        day=_string(state, DETAILS, DT_DAY, "LABEL"),
        graph=_graph(state, DETAILS, DT_GRAPH),
        occurrences=_cells(state, DETAILS, DT_LIST),
    )


def _bar(state: UIState, part: int) -> tuple[Any, ...]:
    return tuple(tuple(e) for e in state.get(RULE_EDITOR, part, "TIMELINE_BAR") or ())


def rule_editor_from_screen(state: UIState, name: str) -> RuleEditor:
    """Read a management rule's editor (3214): pattern bars, special days, calendar."""
    special = tuple(
        (
            _string(state, RULE_EDITOR, radio, "LABEL") or f"Ex{i + 1}",
            _bar(state, bar_part),
        )
        for i, (radio, bar_part) in enumerate(
            zip(RE_SPECIAL_RADIOS, RE_SPECIAL_BARS, strict=True)
        )
        if state.get(RULE_EDITOR, radio, "VISIBLED", 1) != 0
    )
    return RuleEditor(
        name=name,
        weekdays=tuple(_bar(state, part) for part in RE_WEEKDAY_BARS),
        special_days=special,
        calendar=_cells(state, RULE_EDITOR, RE_CALENDAR),
    )


# -- flows ----------------------------------------------------------------------------


async def click(s: _Session, screen: int, part: int, what: str) -> Reply:
    """Click a button, refusing one the iTM shows disabled (that hangs it).

    A hidden button works: the planning settings button of the budget screen is
    hidden on a real iTM and opens its screen all the same.
    """
    return await s.click(screen, part, what)


async def close(s: _Session, screen: int, part: int) -> None:
    """Leave a screen with its Close or Cancel part, if it is open. Never OK."""
    if screen in s.state.screens:
        await s.send(screen, part)
        s.state.screens.pop(screen, None)


async def open_from_menu(s: _Session, button: int, screen: MenuScreen) -> bool:
    """Press a menu list button, wait out a PROGRESS dialog, tell if it opened."""
    await s.open_menu()
    reply = await s.send(MenuScreen.MENULIST, button)
    await s.wait_for_progress(reply)
    return screen in s.state.screens


async def open_below(s: _Session, screen: int, button: int, target: int) -> None:
    """Open a screen from a button of another one (behind PROGRESS, if any)."""
    reply = await click(s, screen, button, f"screen {target}")
    await s.wait_for_progress(reply)
    if target not in s.state.screens:
        msg = f"screen {target} did not open: {reply.dialogs()}"
        raise ItmCommandError(msg)


async def _visit_tabs(s: _Session, screen: int, part: int) -> None:
    """Show every tab once (the iTM may send its parts when shown), then the first."""
    count = len(s.state.get(screen, part, "TAB_NAME") or [])
    if count < 2:
        return
    for index in [*range(1, count), 0]:
        reply = await s.send(screen, part, EventType.TAB_SELECT, DataType.INT, index)
        await s.wait_for_progress(reply)


async def read_budget(s: _Session) -> EnergyBudget | None:
    """Read the budget overview (3200), every tab; None when it does not open."""
    s3200 = MenuScreen.ENERGY_NAVIGATOR
    try:
        if not await open_from_menu(s, MB_ENERGY_NAVIGATOR, s3200):
            return None
        await _visit_tabs(s, s3200, BG_TABS)
        return budget_from_screen(s.state)
    finally:
        await close(s, s3200, BG_CLOSE)


async def open_plan_screen(s: _Session, button: int, screen: int, tabs: int) -> bool:
    """Open 3200 -> 3201 -> screen and show its tabs; False when 3200 doesn't open."""
    if not await open_from_menu(s, MB_ENERGY_NAVIGATOR, MenuScreen.ENERGY_NAVIGATOR):
        return False
    await open_below(s, MenuScreen.ENERGY_NAVIGATOR, BG_PLANNING, PLAN_SETTINGS)
    await open_below(s, PLAN_SETTINGS, button, screen)
    await _visit_tabs(s, screen, tabs)
    return True


async def leave_plan_screens(s: _Session, screen: int, cancel: int) -> None:
    """Cancel a plan screen (3202/3203), close 3201 and 3200."""
    await close(s, screen, cancel)
    await close(s, PLAN_SETTINGS, PS_CLOSE)
    await close(s, MenuScreen.ENERGY_NAVIGATOR, BG_CLOSE)


async def read_planned(s: _Session) -> PlannedConsumption | None:
    """Read the planned consumption (3200 -> 3201 -> 3202), then cancel.

    Returns
    -------
        The energy type shown and every fiscal year; None when 3200 doesn't open.

    Raises
    ------
        ItmCommandError: A screen below 3200 did not open, or its button is
            disabled.

    """
    try:
        if not await open_plan_screen(s, PS_PLANNED, PLANNED, PL_TABS):
            return None
        return planned_from_screen(s.state)
    finally:
        await leave_plan_screens(s, PLANNED, PL_CANCEL)


async def read_actual(s: _Session) -> ActualConsumption | None:
    """Read the actual consumption (3200 -> 3201 -> 3203), then cancel.

    Returns
    -------
        The energy type shown and every fiscal year; None when 3200 doesn't open.

    Raises
    ------
        ItmCommandError: A screen below 3200 did not open, or its button is
            disabled.

    """
    try:
        if not await open_plan_screen(s, PS_ACTUAL, ACTUAL, AC_TABS):
            return None
        return actual_from_screen(s.state)
    finally:
        await leave_plan_screens(s, ACTUAL, AC_CANCEL)


async def _read_equipment_list(s: _Session, button: int, screen: int) -> bool:
    """Open 3210 -> screen; False when 3210 doesn't open."""
    if not await open_from_menu(s, MB_ENERGY_EQUIPMENT, MenuScreen.ENERGY_EQUIPMENT):
        return False
    await open_below(s, MenuScreen.ENERGY_EQUIPMENT, button, screen)
    return True


async def read_rules(s: _Session) -> tuple[str, ...] | None:
    """Read the management rule names (3210 -> 3213), then close.

    Returns
    -------
        The names as listed; None when 3210 doesn't open.

    """
    try:
        if not await _read_equipment_list(s, EQ_RULES, RULES):
            return None
        return rules_from_screen(s.state)
    finally:
        await close(s, RULES, RL_CLOSE)
        await close(s, MenuScreen.ENERGY_EQUIPMENT, EQ_CLOSE)


async def read_sampling_targets(s: _Session) -> tuple[SamplingTarget, ...] | None:
    """Read the sampling targets (3210 -> 3218), then cancel.

    Returns
    -------
        The levels and names as listed; None when 3210 doesn't open.

    """
    try:
        if not await _read_equipment_list(s, EQ_TARGET, SAMPLING_TARGETS):
            return None
        return sampling_targets_from_screen(s.state)
    finally:
        await close(s, SAMPLING_TARGETS, ST_CANCEL)
        await close(s, MenuScreen.ENERGY_EQUIPMENT, EQ_CLOSE)


async def select_row(s: _Session, screen: int, part: int, row: int) -> None:
    """Select a list row, as the browser does on a tap (a display choice)."""
    await s.send(screen, part, EventType.CLICK, DataType.INTARRAY, 1, array=(row,))


async def open_display_list(s: _Session, button: int, screen: int) -> bool:
    """Open 3200 -> 3204 -> an energy type or group list; False when 3200 doesn't."""
    if not await open_from_menu(s, MB_ENERGY_NAVIGATOR, MenuScreen.ENERGY_NAVIGATOR):
        return False
    await open_below(s, MenuScreen.ENERGY_NAVIGATOR, BG_DISPLAY_MODE, DISPLAY_SETTINGS)
    await open_below(s, DISPLAY_SETTINGS, button, screen)
    return True


async def leave_display_list(s: _Session, screen: int) -> None:
    """Close an energy type or group list, the display settings and 3200."""
    await close(s, screen, EL_CLOSE)
    await close(s, DISPLAY_SETTINGS, DS_CLOSE)
    await close(s, MenuScreen.ENERGY_NAVIGATOR, BG_CLOSE)


async def _read_list_entries(
    s: _Session,
    lists: tuple[int, int, int, int],
    read: Callable[[UIState, str], Any],
    plain: Callable[[str], Any],
) -> tuple[Any, ...] | None:
    """Read every entry of a type or group list through its Edit screen, cancelled.

    An entry whose Edit button stays disabled after selecting it gets plain(name).
    """
    button, screen, setting, cancel = lists
    try:
        if not await open_display_list(s, button, screen):
            return None
        entries = []
        for row, name in enumerate(list_names(s.state, screen)):
            await select_row(s, screen, EL_LIST, row)
            if s.state.get(screen, EL_EDIT, "ENABLED") == 0:
                entries.append(plain(name))
                continue
            await open_below(s, screen, EL_EDIT, setting)
            entries.append(read(s.state, name))
            await close(s, setting, cancel)
        return tuple(entries)
    finally:
        await close(s, setting, cancel)
        await leave_display_list(s, screen)


async def read_energy_types(s: _Session) -> tuple[EnergyType, ...] | None:
    """Read the energy types (3200 -> 3204 -> 3205) and each one's settings (3206).

    Returns
    -------
        Per type its settings, as shown; None when 3200 doesn't open.

    Raises
    ------
        ItmCommandError: A screen below 3200 did not open.

    """
    lists = (DS_TYPES, TYPE_LIST, TYPE_SETTING, TS_CANCEL)
    return await _read_list_entries(s, lists, energy_type_from_screen, EnergyType)


async def read_energy_groups(s: _Session) -> tuple[EnergyGroup, ...] | None:
    """Read the energy groups (3200 -> 3204 -> 3207) and each one's members (3208).

    Returns
    -------
        Per group its settings, as shown; None when 3200 doesn't open.

    Raises
    ------
        ItmCommandError: A screen below 3200 did not open.

    """
    lists = (DS_GROUPS, GROUP_LIST, GROUP_SETTING, GS_CANCEL)
    return await _read_list_entries(s, lists, energy_group_from_screen, EnergyGroup)


async def read_details(s: _Session) -> EquipmentDetails | None:
    """Read the equipment details (3210 -> 3211, both tabs), then close.

    Returns
    -------
        What 3211 shows; None when 3210 doesn't open or its Details button is
        disabled (as without data: it is never clicked then).

    """
    try:
        if not await open_from_menu(
            s, MB_ENERGY_EQUIPMENT, MenuScreen.ENERGY_EQUIPMENT
        ):
            return None
        if s.state.get(MenuScreen.ENERGY_EQUIPMENT, EQ_DETAILS[0], "ENABLED") == 0:
            return None
        await open_below(s, MenuScreen.ENERGY_EQUIPMENT, EQ_DETAILS[0], DETAILS)
        await _visit_tabs(s, DETAILS, DT_TABS)
        return details_from_screen(s.state)
    finally:
        await close(s, DETAILS, DT_CLOSE)
        await close(s, MenuScreen.ENERGY_EQUIPMENT, EQ_CLOSE)


async def leave_rule_editor(s: _Session) -> None:
    """Leave the rule editor with Cancel; a "discard?" confirm is answered Yes."""
    if RULE_EDITOR not in s.state.screens:
        return
    reply = await s.send(RULE_EDITOR, RE_CANCEL)
    for dialog in reply.dialogs():
        if dialog[1] == "CONFIRM":
            await s.send(RULE_EDITOR, dialog[0])
    s.state.screens.pop(RULE_EDITOR, None)


async def read_rule_editor(s: _Session) -> tuple[RuleEditor, ...] | None:
    """Read each management rule's editor (3210 -> 3213, select, Edit -> 3214).

    Only the rule list's Edit is pressed (never Create, Copy, Delete or Rename),
    after selecting the rule's row; a rule whose Edit stays disabled is skipped.
    Each editor is left with Cancel.

    Returns
    -------
        The rules as their editors show them; None when 3210 doesn't open.

    """
    try:
        if not await _read_equipment_list(s, EQ_RULES, RULES):
            return None
        rules = []
        for row, name in enumerate(rules_from_screen(s.state)):
            await select_row(s, RULES, RL_LIST, row)
            if s.state.get(RULES, RL_EDIT, "ENABLED") == 0:
                continue
            await open_below(s, RULES, RL_EDIT, RULE_EDITOR)
            await _visit_tabs(s, RULE_EDITOR, RE_TABS)
            rules.append(rule_editor_from_screen(s.state, name))
            await leave_rule_editor(s)
        return tuple(rules)
    finally:
        await leave_rule_editor(s)
        await close(s, RULES, RL_CLOSE)
        await close(s, MenuScreen.ENERGY_EQUIPMENT, EQ_CLOSE)


# -- rule settings (3217) -------------------------------------------------------------


def _checked_on(state: UIState, screen: int, part: int) -> bool:
    return bool(state.get(screen, part, "CHECK_STATE") == 1)


def rule_range_from_screen(state: UIState) -> RuleRange:
    """Read the rule settings' edit group (3217): the selected row's values."""
    s = RULE_SETTING

    def setpoint(check: int, text: int) -> float | None:
        if not (_checked_on(state, s, RS_SETPOINT) and _checked_on(state, s, check)):
            return None
        shown = _shown(state, s, text, "TEXT")
        return None if shown is None else shown.value

    return RuleRange(
        start=_string(state, s, RS_START, "TEXT") or "",
        end=_string(state, s, RS_END, "TEXT") or "",
        cool=setpoint(RS_COOL, RS_COOL_TEXT),
        heat=setpoint(RS_HEAT, RS_HEAT_TEXT),
    )


def special_days(state: UIState) -> dict[str, int]:
    """Return the rule editor's visible special days: name -> radio button part."""
    days = {}
    for i, radio in enumerate(RE_SPECIAL_RADIOS):
        if state.get(RULE_EDITOR, radio, "VISIBLED", 1) == 0:
            continue
        days[_string(state, RULE_EDITOR, radio, "LABEL") or f"Ex{i + 1}"] = radio
    return days


def day_parts(state: UIState, day: str) -> tuple[int, int, int]:
    """Return a day's rule editor tab, radio button and Edit button.

    Raises
    ------
        ItmCommandError: The day is neither a weekday nor a special day shown.

    """
    key = day.strip().lower()
    if key in WEEKDAYS:
        return 0, RE_WEEKDAY_RADIOS[WEEKDAYS.index(key)], RE_EDIT_WEEKDAY
    special = special_days(state)
    if day.strip() in special:
        return 1, special[day.strip()], RE_EDIT_SPECIAL
    msg = f"day {day!r} is not a weekday {WEEKDAYS} nor a special day {[*special]}"
    raise ItmCommandError(msg)


async def open_rule_day(s: _Session, day: str) -> bool:
    """In the rule editor (3214), pick a day's radio button and open 3217 (Edit).

    Returns
    -------
        False when the day's Edit stays disabled (it is not clicked then).

    """
    tab, radio, edit = day_parts(s.state, day)
    reply = await s.send(RULE_EDITOR, RE_TABS, EventType.TAB_SELECT, DataType.INT, tab)
    await s.wait_for_progress(reply)
    await click(s, RULE_EDITOR, radio, f"day {day}")
    if s.state.get(RULE_EDITOR, edit, "ENABLED") == 0:
        return False
    await open_below(s, RULE_EDITOR, edit, RULE_SETTING)
    return True


async def read_rule_ranges(s: _Session) -> tuple[RuleRange, ...]:
    """Select each row of the open rule settings (3217) and read its values."""
    ranges = []
    for row in range(len(list_rows(s.state, RULE_SETTING, RS_LIST)[0])):
        await select_row(s, RULE_SETTING, RS_LIST, row)
        ranges.append(rule_range_from_screen(s.state))
    return tuple(ranges)


async def open_rule(s: _Session, rule: str) -> None:
    """Open 3210 -> 3213, select a rule's row and open its editor (3214).

    Raises
    ------
        ItmCommandError: A screen did not open, the rule is not listed once, or its
            Edit is disabled.

    """
    await open_rules(s)
    names = list(rules_from_screen(s.state))
    if names.count(rule.strip()) != 1:
        msg = f"rule {rule!r} not listed once; the rules are {names}"
        raise ItmCommandError(msg)
    await select_row(s, RULES, RL_LIST, names.index(rule.strip()))
    await open_below(s, RULES, RL_EDIT, RULE_EDITOR)


async def open_rules(s: _Session) -> None:
    """Open 3210 -> 3213.

    Raises
    ------
        ItmCommandError: A screen did not open.

    """
    if not await _read_equipment_list(s, EQ_RULES, RULES):
        msg = "the equipment management did not open"
        raise ItmCommandError(msg)


async def leave_rules(s: _Session) -> None:
    """Cancel 3217 and the rule editor, close 3213 and 3210 (whichever are open)."""
    await close(s, RULE_SETTING, RS_CANCEL)
    await leave_rule_editor(s)
    await close(s, RULES, RL_CLOSE)
    await close(s, MenuScreen.ENERGY_EQUIPMENT, EQ_CLOSE)


async def read_rule_day(s: _Session, rule: str, day: str) -> RuleDay:
    """Read one day of a management rule (3214 -> 3217), every screen cancelled.

    Raises
    ------
        ItmCommandError: A screen did not open, or the day's Edit is disabled.

    """
    try:
        await open_rule(s, rule)
        if not await open_rule_day(s, day):
            msg = f"rule {rule}: the iTM does not offer editing {day}"
            raise ItmCommandError(msg)
        rows = _cells(s.state, RULE_SETTING, RS_LIST)
        return RuleDay(rule, day, rows, await read_rule_ranges(s))
    finally:
        await leave_rules(s)


async def read_rule_settings(s: _Session) -> tuple[RuleDay, ...] | None:
    """Read every rule's days: each weekday and each special day shown (3217).

    A rule or a day whose Edit stays disabled is skipped.

    Returns
    -------
        The days read; None when 3210 doesn't open.

    """
    try:
        if not await _read_equipment_list(s, EQ_RULES, RULES):
            return None
        days = []
        for row, name in enumerate(rules_from_screen(s.state)):
            await select_row(s, RULES, RL_LIST, row)
            if s.state.get(RULES, RL_EDIT, "ENABLED") == 0:
                continue
            await open_below(s, RULES, RL_EDIT, RULE_EDITOR)
            for day in (*WEEKDAYS, *special_days(s.state)):
                if not await open_rule_day(s, day):
                    continue
                rows = _cells(s.state, RULE_SETTING, RS_LIST)
                days.append(RuleDay(name, day, rows, await read_rule_ranges(s)))
                await close(s, RULE_SETTING, RS_CANCEL)
            await leave_rule_editor(s)
        return tuple(days)
    finally:
        await leave_rules(s)


# -- the installer's settings (3220-3224, behind the service login) -------------------


async def open_installer(s: _Session) -> bool:
    """Press the service tab's energy navigator button; tell whether 3220 opened.

    Raises
    ------
        ItmCommandError: The iTM answered with a dialog (no service login).

    """
    await s.open_menu()
    reply = await s.send(MenuScreen.MENULIST, MB_ENERGY_INSTALLER)
    if INSTALLER not in s.state.screens and reply.dialogs():
        msg = f"the energy navigator's installer settings: {reply.dialogs()}"
        raise ItmCommandError(msg)
    return INSTALLER in s.state.screens


async def leave_installer(s: _Session) -> None:
    """Cancel the installer screens and close 3220. Never its data clear button."""
    await close(s, ANALOG_SELECT, AS_CANCEL)
    await close(s, ROOM_TEMP, RT_CANCEL)
    await close(s, COLLECT_START, CM_CANCEL)
    await close(s, CONVERSION, CV_CANCEL)
    await close(s, INSTALLER, IN_CLOSE)


def room_point_from_screen(state: UIState, cells: list[str]) -> RoomTemperaturePoint:
    """Read a 3221 row and the radio buttons and text its selection shows."""
    cells = [*cells, "", ""]
    own = _checked(state, ROOM_TEMP, RT_OWN, RT_ANALOG)
    return RoomTemperaturePoint(
        name=cells[0].strip(),
        setting=cells[1].strip(),
        own_sensor=own,
        analog_point=(
            None
            if own is not False
            else _string(state, ROOM_TEMP, RT_ANALOG_TEXT, "TEXT")
        ),
    )


async def read_room_points(s: _Session) -> tuple[RoomTemperaturePoint, ...]:
    """Select each row of the open 3221 and read its room temperature source."""
    points = []
    for row, cells in enumerate(list_rows(s.state, ROOM_TEMP, RT_LIST)[0]):
        await select_row(s, ROOM_TEMP, RT_LIST, row)
        points.append(room_point_from_screen(s.state, cells))
    return tuple(points)


def conversion_from_screen(state: UIState) -> ConversionType:
    """Read the energy conversion screen (3224): name and unit texts."""
    values = {
        key: html.unescape(str(state.get(CONVERSION, text, "TEXT") or "")).strip()
        for key, (text, _) in CV_FIELDS.items()
    }
    return ConversionType(**values)


def collection_from_screen(state: UIState) -> tuple[str | None, tuple[str, ...]]:
    """Read the collection start month (3223): the month shown and those offered."""
    options = state.get(COLLECT_START, CM_MONTH, "COMBO_STRS") or []
    months = tuple(html.unescape(str(o)).strip() for o in options)
    return option(state, COLLECT_START, CM_MONTH), months


async def read_installer(s: _Session) -> EnergyInstallerSettings | None:
    """Read the installer's settings (after a service login), each screen cancelled.

    3222 (the analog inputs) is read only while 3221's button for it is enabled
    after the rows were selected. 3220's data clear button is never clicked.

    Returns
    -------
        The settings; None when 3220 doesn't open.

    """
    try:
        if not await open_installer(s):
            return None
        await open_below(s, INSTALLER, IN_ROOM_TEMP, ROOM_TEMP)
        points = await read_room_points(s)
        analog = None
        if points and s.state.get(ROOM_TEMP, RT_ANALOG_BUTTON, "ENABLED") != 0:
            await open_below(s, ROOM_TEMP, RT_ANALOG_BUTTON, ANALOG_SELECT)
            analog = _cells(s.state, ANALOG_SELECT, AS_LIST)
            await close(s, ANALOG_SELECT, AS_CANCEL)
        await close(s, ROOM_TEMP, RT_CANCEL)
        await open_below(s, INSTALLER, IN_COLLECT, COLLECT_START)
        month, months = collection_from_screen(s.state)
        await close(s, COLLECT_START, CM_CANCEL)
        await open_below(s, INSTALLER, IN_CONVERSION, CONVERSION)
        conversion = conversion_from_screen(s.state)
        return EnergyInstallerSettings(points, analog, month, months, conversion)
    finally:
        await leave_installer(s)


def wanted_parts(only: Iterable[str] | None) -> frozenset[str]:
    """Check the names of the parts to read (EnergyNavigator fields).

    None means the default parts (DEFAULT_PARTS); the others are read when named.

    Raises
    ------
        ItmCommandError: A name is not one of them.

    """
    if only is None:
        return frozenset(DEFAULT_PARTS)
    names = frozenset(only)
    if unknown := names - frozenset(PARTS):
        msg = f"unknown energy navigator parts {sorted(unknown)}; known: {PARTS}"
        raise ItmCommandError(msg)
    return names


async def read_energy_navigator(
    s: _Session,
    only: frozenset[str] = frozenset(DEFAULT_PARTS),
    service_password: str | None = None,
) -> EnergyNavigator:
    """Read the named parts of the energy navigator, each from its own screens.

    The installer's settings are read last, after a service login with
    service_password (sent once, never stored).

    Raises
    ------
        ItmCommandError: The installer's settings are named without a password.

    """

    async def installer(s: _Session) -> EnergyInstallerSettings | None:
        if service_password is None:
            msg = "the installer's settings need the service password"
            raise ItmCommandError(msg)
        await s.service_login(service_password)
        return await read_installer(s)

    readers: dict[str, Callable[[_Session], Awaitable[Any]]] = {
        "budget": read_budget,
        "planned": read_planned,
        "actual": read_actual,
        "rules": read_rules,
        "sampling_targets": read_sampling_targets,
        "energy_types": read_energy_types,
        "energy_groups": read_energy_groups,
        "details": read_details,
        "rule_editor": read_rule_editor,
        "rule_settings": read_rule_settings,
        "installer": installer,
    }
    values = {name: await readers[name](s) for name in PARTS if name in only}
    return EnergyNavigator(**values)
