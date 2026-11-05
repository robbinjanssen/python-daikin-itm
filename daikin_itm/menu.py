"""The iTM's main menu: the screens behind "Menulijst" and what they show.

Screen and part IDs come from the web UI's own script.js (ScSysMainMenu and the
screen classes named below). Everything is read by part ID, index or icon; the few
texts that are used (version, clock) have a fixed shape in every language.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, Any

from .exceptions import ProtocolError
from .lang import (
    event_lock_key,
    lock_from_label,
    mode_from_label,
    power_from_label,
)
from .models import FAN_ICONS, ICON_ID_ZONE, PowerState, RemoteLock, point_kind
from .protocol import ScreenID

if TYPE_CHECKING:
    from .state import UIState
    from .system import SystemSettings

# Main menu bar (screen 0, ScMainMenu): the menu list button and its close button,
# the clock label and the indicators (shown while active).
MENU_OPEN, MENU_CLOSE, MM_CLOCK = 0, 25, 6
MM_ENERGY_SAVING, MM_ERROR, MM_EMERGENCY_STOP = 16, 18, 20


class MenuScreen(IntEnum):
    """Screens opened from the menu list (ScreenID.SCR_ID_* in script.js)."""

    MENULIST = 5  # ScSysMainMenu
    SCHEDULE = 100  # ScSchMain
    SCHEDULE_DAY = 102  # ScSchConfirm: what a program does on a day
    SCHEDULE_EDITOR = 104  # ScSchEdit: a program's properties, patterns and calendar
    SCHEDULE_EVENTS = 107  # ScSchEventList: the events of a weekday or special day
    SCHEDULE_EVENT = 109  # ScSchEvent: one event's settings
    EMERGENCY_STOP = 200  # ScEmgStopMain
    CHANGEOVER = 300  # ScAutoChangeOverSetting
    CHANGEOVER_GROUP = 301  # ScAutoChangeOverGroup: the members
    CHANGEOVER_SETTINGS = 302  # ScAutoChangeOverAttribute
    TEMP_LIMIT = 400  # ScTempLimitSetting
    SLIDING_TEMP = 500  # ScSlideTempSetting
    HEATING_OPTIMISATION = 600  # ScHMO
    OVERTIME = 700  # ScTimerExtension
    HISTORY = 800  # ScHistoryMain
    INTERLOCK = 900  # ScInterlockMain
    TIME = 2400  # ScSysTimeSetting
    REGION = 2500  # ScSysLocalSetting (behind the hidden "language" button)
    VERSION = 2600  # ScSysVersionInfo
    SETBACK = 3000  # ScSetbackMain
    SETBACK_ADVANCED = 3001  # ScSetbackAdvanced
    ENERGY_NAVIGATOR = 3200  # ScEnergyNaviPlanManage
    # System settings, read by system.py (most of their buttons are hidden).
    REMOTE_MANAGEMENT = 1100  # Afstandbeheer: not in script.js's screen list
    LANGUAGE = 2501  # ScSysLanguageSetting (from the region screen)
    ZONE_SETTINGS = 1603  # ScSysAreaAttributeSetup
    POINT_SETTINGS = 1701  # ScSysMngAttributeSetup
    USER_SETUP = 1204  # ScSysWebAccessUserSetup
    USER_ZONES = 1202  # ScSysWebAccessManageArea
    WEB_USERS = 1200  # ScSysWebAccessUser
    EMAIL = 1300  # ScEmlSetting
    ZONES = 1600  # ScSysAreaSetup
    POINTS = 1700  # ScSysMngSetup
    NETWORK = 1800  # ScSysNetworkSetting
    SCREENSAVER = 2000  # ScSysScreensaverSetting
    CONFIRM_DIALOGS = 2100  # ScSysCheckSetting
    HARDWARE = 2200  # ScSysHardSetting
    MAINTENANCE = 2700  # ScSysInspection
    PPD = 2900  # ScPPDSetup: power proportional distribution
    ENERGY_EQUIPMENT = 3210  # ScEnergyNaviEquipmentManage
    ENERGY_EXPORT = 3219  # ScEnergyNaviDataOutput
    # Service screens (service.py), behind the service login.
    TIME_ZONE = 1500  # ScSysTimezoneSetting
    ACTIVATION = 1501  # ScSrvActivation
    DEALER_OPTIONS = 1502  # ScSrvOptionSetup
    CHANGEOVER_OPTION = 1505  # ScSrvChangeOverOption
    CENTRAL_MONITOR = 1507  # ScSysCentralCtrlSetting
    CONTACT = 1509  # ScSrvContactSetup
    ENERGY_SAVE_MODE = 1511  # ScSrvEneSaveModeSelect
    R32 = 1516  # ScSrvR32Setting


# Menu list buttons (screen 5) and the screen each opens, with its Close/Cancel.
# Buttons work whether or not their tab is the active one.
MB_SCHEDULE, MB_INTERLOCK, MB_EMERGENCY_STOP, MB_CHANGEOVER = 3, 5, 7, 9
MB_TEMP_LIMIT, MB_SLIDING_TEMP, MB_HEATING_OPTIMISATION = 11, 13, 15
MB_OVERTIME, MB_SETBACK, MB_TIME, MB_REGION, MB_VERSION = 17, 19, 41, 43, 51
MB_HISTORY, MB_ENERGY_NAVIGATOR = 53, 100

# Close (or Cancel) part of each menu screen; never its OK.
CLOSE_PART = {
    MenuScreen.SCHEDULE: 13,
    MenuScreen.SCHEDULE_DAY: 5,
    MenuScreen.SCHEDULE_EDITOR: 21,  # asks "confirm closing?": answer Yes
    MenuScreen.SCHEDULE_EVENTS: 10,
    MenuScreen.SCHEDULE_EVENT: 6,
    MenuScreen.EMERGENCY_STOP: 1,
    MenuScreen.CHANGEOVER: 1,
    MenuScreen.CHANGEOVER_GROUP: 13,
    MenuScreen.CHANGEOVER_SETTINGS: 12,
    MenuScreen.TEMP_LIMIT: 1,
    MenuScreen.SLIDING_TEMP: 1,
    MenuScreen.HEATING_OPTIMISATION: 8,
    MenuScreen.OVERTIME: 4,
    MenuScreen.HISTORY: 17,
    MenuScreen.INTERLOCK: 11,
    MenuScreen.TIME: 19,
    MenuScreen.REGION: 19,
    MenuScreen.VERSION: 2,
    MenuScreen.SETBACK: 21,
    MenuScreen.SETBACK_ADVANCED: 11,
    MenuScreen.ENERGY_NAVIGATOR: 19,
    MenuScreen.REMOTE_MANAGEMENT: 7,
    MenuScreen.LANGUAGE: 13,
    MenuScreen.ZONE_SETTINGS: 19,
    MenuScreen.POINT_SETTINGS: 13,
    MenuScreen.USER_SETUP: 18,
    MenuScreen.USER_ZONES: 4,
    MenuScreen.WEB_USERS: 7,
    MenuScreen.EMAIL: 12,
    MenuScreen.ZONES: 14,
    MenuScreen.POINTS: 3,
    MenuScreen.NETWORK: 24,  # Cancel
    MenuScreen.SCREENSAVER: 11,  # Cancel
    MenuScreen.CONFIRM_DIALOGS: 5,  # Cancel
    MenuScreen.HARDWARE: 13,  # Cancel
    MenuScreen.MAINTENANCE: 10,  # Cancel
    MenuScreen.PPD: 22,
    MenuScreen.ENERGY_EQUIPMENT: 20,
    MenuScreen.ENERGY_EXPORT: 5,
    MenuScreen.TIME_ZONE: 4,  # Cancel
    MenuScreen.ACTIVATION: 6,  # Cancel
    MenuScreen.DEALER_OPTIONS: 10,  # Cancel
    MenuScreen.CHANGEOVER_OPTION: 5,
    MenuScreen.CENTRAL_MONITOR: 8,  # Cancel
    MenuScreen.CONTACT: 11,  # Cancel
    MenuScreen.ENERGY_SAVE_MODE: 6,  # Cancel
    MenuScreen.R32: 11,
}

# Auto changeover (300): the group list and its buttons.
CO_LIST, CO_ENABLE, CO_DISABLE, CO_ADD, CO_DELETE = 2, 4, 5, 7, 8
CO_MEMBERS_BUTTON, CO_SETTINGS_BUTTON = 9, 10
# Its members screen (301) and settings screen (302).
CG_MEMBERS, CG_AVAILABLE, CG_ADD, CG_REMOVE, CG_OK = 2, 11, 7, 8, 12
CS_NAME, CS_DIFFERENTIAL, CS_REFERENCE, CS_OK = 2, 5, (8, 9, 10), 11

# Setback (3000) and its advanced settings (3001).
SB_HIGH_UP, SB_HIGH_BACK, SB_LOW_UP, SB_LOW_BACK, SB_ADVANCED = 4, 8, 13, 17, 19
SB_OK, SBA_COOL, SBA_HEAT, SBA_OK = 20, 4, 8, 10

# Overtime timer (700): the duration combo.
OT_DURATION, OT_OK = 2, 3

# Emergency stop (200): the program list.
ES_LIST, ES_ENABLE, ES_DISABLE = 2, 4, 5

# Schedules (100).
SCH_PANEL, SCH_DAY_BUTTON, SCH_EDIT_BUTTON = 4, 8, 11
SCH_ADD_BUTTON, SCH_DELETE_BUTTON = 1, 10
# Day overview (102).
SD_PREVIOUS, SD_DAY_LABEL, SD_NEXT, SD_LIST = 1, 2, 3, 4
SD_SEPARATOR_ICON = 4016  # rows that only separate the times
# Editor (104).
SE_TAB, SE_NAME, SE_ENABLED, SE_DISABLED, SE_PERIOD_CHECK = 1, 3, 7, 8, 10
SE_OK = 20
SE_RENAME = 4  # Change: asks the name in a text input dialog
SE_PERIOD = (16, 17, 18, 19)  # month and day combos of the begin and the end
SE_DAY_RADIOS = tuple(range(52, 59))  # Sunday .. Saturday
SE_WEEKLY_BARS = tuple(range(64, 71))
SE_EDIT_DAY = 73
SE_SPECIAL_RADIOS = tuple(range(85, 90))  # Ex1 .. Ex5
SE_SPECIAL_BARS = tuple(range(95, 100))
SE_EDIT_SPECIAL = 102
SE_CALENDAR_LIST = 26
SE_TAB_PROPERTIES, SE_TAB_WEEKLY, SE_TAB_SPECIAL, SE_TAB_CALENDAR = 0, 1, 2, 3
SLOT_MINUTES = 15  # a pattern bar has 96 slots
# Day events (107) and one event's settings (109).
EV_PREVIOUS, EV_DAY_LABEL, EV_NEXT, EV_LIST, EV_EDIT = 1, 2, 3, 4, 8
EVT_TIME, EVT_TARGET = 43, 46
# The settings an event can carry: (its checkbox, its value controls). A setting
# is part of the event only when the checkbox is checked.
EVT_POWER = (2, (3, 4, 54, 55))  # start, stop, setback high, setback low
EVT_LOCK_START_STOP = (7, (8, 9, 10))
EVT_LOCK_MODE = (11, (12, 13))
EVT_LOCK_SETPOINT = (14, (15, 16))
EVT_MODE, EVT_MODE_COMBO = 17, 18
EVT_SETPOINT, EVT_SETPOINT_FIXED, EVT_SETPOINT_TEXT = 19, 20, 21
EVT_SETPOINT_SHIFT, EVT_SETPOINT_SHIFT_COMBO = 24, 25
EVT_OVERTIME, EVT_OVERTIME_COMBO = 26, 27
EVT_FAN, EVT_FAN_ICON = 28, 31
EVT_VENT_MODE, EVT_VENT_MODE_COMBO = 33, 34
EVT_AIR_VOLUME, EVT_AIR_VOLUME_COMBO = 35, 36

# History (800).
H_LIST, H_PAGE_LABEL, H_DATE = 1, 14, 8
H_CLASS_ERROR, H_CLASS_MANAGEMENT, H_CLASS_SETTINGS, H_CLASS_STATUS = 3, 4, 5, 6
H_DAY_PREVIOUS, H_DAY_NEXT, H_REFRESH = 9, 10, 12
H_PAGE_OLDER, H_PAGE_NEWER = 15, 16
HISTORY_COLUMNS = 10

# Time (2400), region (2500) and version (2600).
T_DATETIME, T_DST_ENABLED, T_DST_DISABLED, T_OK = 2, 6, 7, 18
R_LANGUAGE, R_DATE_FORMAT, R_24H, R_12H, R_CELSIUS, R_FAHRENHEIT = 2, 5, 7, 8, 10, 11
R_DECIMAL_POINT, R_DECIMAL_COMMA, R_OK = 13, 14, 18
V_TEXT = 1

# Hidden, usually unconfigured functions: their menu button, screen and main list.
OPTIONAL_FUNCTIONS: dict[str, tuple[int, MenuScreen, int]] = {
    "temperature_limit": (MB_TEMP_LIMIT, MenuScreen.TEMP_LIMIT, 3),
    "sliding_temperature": (MB_SLIDING_TEMP, MenuScreen.SLIDING_TEMP, 3),
    "heating_optimisation": (
        MB_HEATING_OPTIMISATION,
        MenuScreen.HEATING_OPTIMISATION,
        2,
    ),
    "interlock": (MB_INTERLOCK, MenuScreen.INTERLOCK, 1),
}

DATE_FORMATS = ("%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d")
_VERSION = re.compile(r"\bVer\s*([\d.]+)")
_ACTION_PAIR = re.compile(r"^(.*?)\s*\[(.*)\]$")
_ACTION_TEMP = re.compile(r"^(-?\d+(?:[.,]\d+)?)\s*(?:°[CF]|℃|℉)$")


@dataclass(frozen=True)
class ItmIndicators:
    """The indicators of the main menu bar (shown while active) and its clock."""

    energy_saving: bool
    error: bool
    emergency_stop: bool
    clock_text: str | None  # e.g. "Wo, 01/01\n09:30", as shown


@dataclass(frozen=True)
class ItmInfo:
    """Firmware, regional settings and clock of the iTM."""

    version: str | None  # e.g. "1.32.00"
    build: str | None  # the build date line, as shown
    language: str | None  # as shown, e.g. "Nederlands" (None when not readable)
    date_format: str | None  # a strptime format, see DATE_FORMATS
    time_24h: bool | None
    temperature_unit: str | None  # "°C" or "°F"
    decimal_separator: str | None  # "." or ","
    clock: datetime | None  # the iTM's clock, as read
    clock_text: str | None
    dst_enabled: bool | None
    read_at: datetime | None = None  # local time when the clock was read

    @property
    def clock_offset(self) -> float | None:
        """Seconds the iTM's clock is ahead of the local clock (negative: behind)."""
        if self.clock is None or self.read_at is None:
            return None
        return (self.clock - self.read_at).total_seconds()


class ChangeoverReference(StrEnum):
    """Whose temperature an auto changeover group decides on."""

    FIXED = "fixed"  # one fixed unit
    OPERATING = "operating"  # the units that are running
    AVERAGE = "average"  # the group's average


@dataclass(frozen=True)
class ChangeoverGroup:
    """An auto changeover group: the iTM switches its members between heat and cool."""

    name: str
    enabled: bool
    member_count: int | None
    members: tuple[str, ...] = ()  # with details only
    differential: int | None = None  # °C, with details only
    reference: ChangeoverReference | None = None  # with details only


@dataclass(frozen=True)
class SetbackSettings:
    """The setback offsets (°C) for setback high and low, and the recovery."""

    high_up: int | None
    high_back: int | None
    low_up: int | None
    low_back: int | None
    recovery_cool: int | None = None
    recovery_heat: int | None = None


@dataclass(frozen=True)
class EmergencyStopProgram:
    """An emergency stop program and whether it is armed."""

    name: str
    enabled: bool


@dataclass(frozen=True)
class ScheduleSummary:
    """A schedule program as the program list shows it."""

    name: str
    enabled: bool
    state: int  # raw: 0 active, 3 inactive, 4 inactive with a period
    period: tuple[str, str] | None  # ("01/06", "31/08"), as shown


@dataclass(frozen=True)
class ScheduleEvent:  # pylint: disable=too-many-instance-attributes
    """What a schedule program does to a zone or unit at a time.

    ``action`` is the overview text as the iTM shows it (localised). The typed
    fields are parsed from it (power, mode, setpoint and the locks are recognised
    through the language tables; the other ``key[value]`` pairs land in ``extra``),
    or read from the event's settings screen when ``details`` is True. They are
    None when the setting is not part of the event.
    """

    time: str  # "HH:MM"
    target: str  # a zone or a unit
    target_kind: str  # "zone", "indoor", "ventilation", "analog" or "other"
    action: str
    row: int = -1  # its row in the iTM's list (separator rows count), to select it
    details: bool = False
    power: PowerState | None = None
    mode: str | None = None  # heat / cool / fan / dry / auto / setpoint
    mode_label: str | None = None
    setpoint: float | None = None
    setpoint_shift: str | None = None  # a relative setpoint, as shown
    lock_start_stop: RemoteLock | None = None
    lock_mode: RemoteLock | None = None
    lock_setpoint: RemoteLock | None = None
    overtime: bool | None = None
    fan_speed: str | None = None
    ventilation_mode: str | None = None  # as shown
    air_volume: str | None = None  # as shown
    # The other "key[value]" parts of the action text, e.g. the ventilation mode,
    # by their key as shown.
    extra: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CalendarRule:
    """A row of a program's calendar: which pattern a date uses."""

    month: str
    day: str
    pattern: str


@dataclass(frozen=True)
class ScheduleProgram:
    """A schedule program as its editor shows it."""

    name: str
    enabled: bool
    period: tuple[str, str] | None  # (begin, end) as "MM/DD" of the combos, or None
    # Minutes of the day at which the weekly pattern has an event, per weekday
    # (0 = Sunday), from the pattern bars.
    weekly_slots: dict[int, tuple[int, ...]] = field(default_factory=dict)
    special_slots: dict[str, tuple[int, ...]] = field(default_factory=dict)
    weekly: dict[int, tuple[ScheduleEvent, ...]] = field(default_factory=dict)
    special: dict[str, tuple[ScheduleEvent, ...]] = field(default_factory=dict)
    calendar: tuple[CalendarRule, ...] = ()
    special_names: tuple[str, ...] = ()  # Ex1..Ex5 as named (read_schedule)


@dataclass(frozen=True)
class ScheduleDay:
    """What a program does on one day, from its day overview."""

    program: str
    day: str  # as shown, e.g. "01/01(Wo)"
    events: tuple[ScheduleEvent, ...]


@dataclass(frozen=True)
class HistoryEntry:
    """One row of the history: who changed what, when."""

    time: str
    kind: str  # error / management / settings / status, as the iTM shows it
    number: str
    content: str
    name: str
    source: str  # e.g. the schedule, "Web:<user>", the auto changeover
    port: str
    address: str
    point_id: str
    code: str


@dataclass(frozen=True)
class History:
    """A page of the history, with where it sits."""

    day: str | None  # as shown
    entries: tuple[HistoryEntry, ...]
    columns: tuple[str, ...]
    page: str | None  # e.g. "1-100/1250", as shown


@dataclass(frozen=True)
class OptionalFunction:
    """A function the menu hides: whether its screen opens, and what it lists."""

    available: bool
    rows: tuple[tuple[str, ...], ...] = ()


@dataclass(frozen=True)
class ItmConfig:
    """Everything fetch_config reads in one session."""

    indicators: ItmIndicators
    info: ItmInfo
    schedules: tuple[ScheduleSummary, ...]
    changeover_groups: tuple[ChangeoverGroup, ...]
    setback: SetbackSettings
    overtime_minutes: int | None
    emergency_stop: tuple[EmergencyStopProgram, ...]
    extra: dict[str, Any] = field(default_factory=dict)
    system: SystemSettings | None = None  # with fetch_config(system=True)


def _text(state: UIState, screen: int, part: int, ptype: str = "TEXT") -> str | None:
    value = state.get(screen, part, ptype)
    return html.unescape(value).strip() or None if isinstance(value, str) else None


def _rows(state: UIState, screen: int, part: int) -> list[list[str]]:
    rows = (state.get(screen, part, "LIST_ROW") or {}).get("rows", [])
    return [[html.unescape(str(t)) for t in r["text"]] for r in rows]


def _combo_value(state: UIState, screen: int, part: int) -> int | None:
    nums = state.get(screen, part, "COMBO_NUMS") or []
    idx = state.get(screen, part, "SINGLE_SELIDX")
    return nums[idx] if idx is not None and 0 <= idx < len(nums) else None


def _checked(state: UIState, screen: int, parts: tuple[int, ...]) -> int | None:
    for i, part in enumerate(parts):
        if state.get(screen, part, "CHECK_STATE") == 1:
            return i
    return None


def indicators_from_screen(state: UIState) -> ItmIndicators:
    """Read the main menu bar's indicators and clock (open in every session)."""
    s = ScreenID.MAINMENU
    return ItmIndicators(
        energy_saving=state.visible(s, MM_ENERGY_SAVING)
        and state.get(s, MM_ENERGY_SAVING, "VISIBLED") is not None,
        error=state.visible(s, MM_ERROR)
        and state.get(s, MM_ERROR, "VISIBLED") is not None,
        emergency_stop=state.visible(s, MM_EMERGENCY_STOP)
        and state.get(s, MM_EMERGENCY_STOP, "VISIBLED") is not None,
        clock_text=_text(state, s, MM_CLOCK, "LABEL"),
    )


def version_from_screen(state: UIState) -> dict[str, str | None]:
    """Read the version screen: the version number and the build line."""
    text = _text(state, MenuScreen.VERSION, V_TEXT) or ""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    version = None
    build = None
    for i, line in enumerate(lines):
        if (m := _VERSION.search(line)) is not None:
            version = m.group(1)
            build = lines[i + 1] if i + 1 < len(lines) else None
            break
    return {"version": version, "build": build}


def region_from_screen(state: UIState) -> dict[str, Any]:
    """Read the region screen: language, date/time format, unit, decimal separator."""
    s = MenuScreen.REGION
    if s not in state.screens:
        return {}
    fmt_idx = state.get(s, R_DATE_FORMAT, "SINGLE_SELIDX")
    unit = _checked(state, s, (R_CELSIUS, R_FAHRENHEIT))
    decimal = _checked(state, s, (R_DECIMAL_POINT, R_DECIMAL_COMMA))
    hours = _checked(state, s, (R_24H, R_12H))
    return {
        "language": _text(state, s, R_LANGUAGE),
        "date_format": DATE_FORMATS[fmt_idx]
        if fmt_idx is not None and 0 <= fmt_idx < len(DATE_FORMATS)
        else None,
        "time_24h": None if hours is None else hours == 0,
        "temperature_unit": None if unit is None else ("°C", "°F")[unit],
        "decimal_separator": None if decimal is None else (".", ",")[decimal],
    }


_CLOCK = re.compile(
    r"^(?P<date>\S+)\s+(?P<h>\d{1,2}):(?P<m>\d{2})(?::(?P<s>\d{2}))?\s*(?P<ampm>\S+)?$"
)
_TIME_OF_DAY = re.compile(r"^(?P<h>\d{1,2}):(?P<m>\d{2})\s*(?P<ampm>\S+)?$")
# The iTM writes AM/PM as these in every language (its time input lists them so).
_PM = ("pm", "p.m.", "午後", "下午")
_AM = ("am", "a.m.", "午前", "上午")


def _hour_24(hour: int, marker: str | None) -> int | None:
    """Turn a 12-hour clock hour into 24 hours; None for an unknown marker."""
    if not marker:
        return hour
    m = marker.strip().lower()
    if m in _PM:
        return hour % 12 + 12
    if m in _AM:
        return hour % 12
    return None


def parse_clock(text: str | None, date_format: str | None) -> datetime | None:
    """Parse the iTM's date and time as shown, in 24-hour or 12-hour format."""
    m = _CLOCK.match((text or "").strip())
    if m is None:
        return None
    hour = _hour_24(int(m.group("h")), m.group("ampm"))
    if hour is None:
        return None
    clock = f"{hour:02d}:{m.group('m')}:{m.group('s') or '00'}"
    for fmt in ([date_format] if date_format else []) + list(DATE_FORMATS):
        try:
            return datetime.strptime(f"{m.group('date')} {clock}", f"{fmt} %H:%M:%S")  # noqa: DTZ007 - the iTM's own clock, no zone
        except ValueError:
            continue
    return None


def time_of_day(text: str) -> str:
    """Normalise a time of day as shown ("7:05 PM", "19:05") to "HH:MM"."""
    m = _TIME_OF_DAY.match(text.strip())
    if m is None:
        return text.strip()
    hour = _hour_24(int(m.group("h")), m.group("ampm"))
    return text.strip() if hour is None else f"{hour:02d}:{m.group('m')}"


def clock_from_screen(state: UIState, date_format: str | None) -> dict[str, Any]:
    """Read the time screen: the iTM's clock and whether DST is enabled."""
    s = MenuScreen.TIME
    text = _text(state, s, T_DATETIME)
    clock = parse_clock(text, date_format)
    dst = _checked(state, s, (T_DST_ENABLED, T_DST_DISABLED))
    return {
        "clock": clock,
        "clock_text": text,
        "dst_enabled": None if dst is None else dst == 0,
    }


def changeover_groups_from_screen(state: UIState) -> list[ChangeoverGroup]:
    """Read the auto changeover screen's group list."""
    groups = []
    for row in _rows(state, MenuScreen.CHANGEOVER, CO_LIST):
        enabled, name, count = [*row, "", "", ""][:3]
        groups.append(
            ChangeoverGroup(
                name=name,
                enabled=bool(enabled.strip()),
                member_count=int(count) if count.strip().isdigit() else None,
            )
        )
    return groups


def changeover_members_from_screen(state: UIState) -> tuple[str, ...]:
    """Read a group's members from its members screen."""
    return tuple(
        row[0] for row in _rows(state, MenuScreen.CHANGEOVER_GROUP, CG_MEMBERS)
    )


def changeover_candidates_from_screen(state: UIState) -> list[tuple[str, str, str]]:
    """Read the points a group could hold: (name, current group, changeover option)."""
    rows = _rows(state, MenuScreen.CHANGEOVER_GROUP, CG_AVAILABLE)
    return [(r[0], r[1], r[2]) for r in rows if len(r) >= 3]


def changeover_settings_from_screen(state: UIState) -> dict[str, Any]:
    """Read a group's settings screen: differential and reference method."""
    s = MenuScreen.CHANGEOVER_SETTINGS
    ref = _checked(state, s, CS_REFERENCE)
    return {
        "differential": _combo_value(state, s, CS_DIFFERENTIAL),
        "reference": None if ref is None else list(ChangeoverReference)[ref],
    }


def setback_from_screen(state: UIState) -> SetbackSettings:
    """Read the setback screen (and the advanced screen when it is open)."""
    s, a = MenuScreen.SETBACK, MenuScreen.SETBACK_ADVANCED
    return SetbackSettings(
        high_up=_combo_value(state, s, SB_HIGH_UP),
        high_back=_combo_value(state, s, SB_HIGH_BACK),
        low_up=_combo_value(state, s, SB_LOW_UP),
        low_back=_combo_value(state, s, SB_LOW_BACK),
        recovery_cool=_combo_value(state, a, SBA_COOL),
        recovery_heat=_combo_value(state, a, SBA_HEAT),
    )


def overtime_from_screen(state: UIState) -> int | None:
    """Read the overtime timer's duration in minutes."""
    s = MenuScreen.OVERTIME
    options = state.get(s, OT_DURATION, "COMBO_STRS") or []
    idx = state.get(s, OT_DURATION, "SINGLE_SELIDX")
    if idx is None or not 0 <= idx < len(options):
        return None
    m = re.search(r"\d+", options[idx])
    return int(m.group()) if m else (idx + 1) * 30


def emergency_stop_from_screen(state: UIState) -> list[EmergencyStopProgram]:
    """Read the emergency stop screen's program list.

    A row's activation text is the label of the screen's enable or disable radio
    ("Inschakelen" / "Uitschakelen" in Dutch), so it is compared with those labels,
    in any display language. Without the labels, any text counts as enabled.
    """
    s = MenuScreen.EMERGENCY_STOP
    disabled = (_text(state, s, ES_DISABLE, "LABEL") or "").strip()
    programs = []
    for row in _rows(state, s, ES_LIST):
        if len(row) < 2:
            continue
        text = row[0].strip()
        enabled = bool(text) and not (disabled and text == disabled)
        programs.append(EmergencyStopProgram(name=row[1], enabled=enabled))
    return programs


def schedules_from_screen(state: UIState) -> list[ScheduleSummary]:
    """Read the schedule screen's program panel."""
    panel = state.get(MenuScreen.SCHEDULE, SCH_PANEL, "SCHEDULE_PANEL")
    if not panel:
        return []
    out = []
    for raw_state, name, start, end in panel[0]:
        out.append(
            ScheduleSummary(
                name=html.unescape(name),
                enabled=raw_state not in (3, 4),
                state=raw_state,
                period=(start, end) if start or end else None,
            )
        )
    return out


def _event_rows(state: UIState, screen: int, part: int) -> list[ScheduleEvent]:
    """Read an event list (day overview or day events): time, target, action."""
    raw = (state.get(screen, part, "LIST_ROW") or {}).get("rows", [])
    events = []
    last_time = ""
    for index, row in enumerate(raw):
        texts = [html.unescape(str(t)) for t in row["text"]]
        if len(texts) < 3 or row["a"][0] == SD_SEPARATOR_ICON or not texts[0].strip():
            continue
        time_text = time_of_day(texts[0])
        if ":" not in time_text:  # "05": the same hour as the row before
            time_text = (
                f"{last_time.split(':', maxsplit=1)[0]}:{time_text}"
                if last_time
                else time_text
            )
        last_time = time_text
        icon = row["a"][1]
        kind = "zone" if icon == ICON_ID_ZONE else point_kind(icon).value
        events.append(
            ScheduleEvent(
                time=time_text,
                target=texts[1].strip(),
                target_kind=kind,
                action=texts[2],
                row=index,
            )
        )
    return [event_from_action(e) for e in events]


def event_from_action(event: ScheduleEvent) -> ScheduleEvent:
    """Parse an event's overview text into its typed fields, where known.

    The text lists the settings comma-separated: the on/off option, ``key[value]``
    pairs for the locks and other options, the mode (and reference), the setpoint.
    Power, mode, setpoint and the lock values are recognised through the language
    tables; the rest is kept in ``extra`` by its key.
    """
    values: dict[str, Any] = {}
    unnamed: list[RemoteLock] = []  # lock values whose key the tables don't know
    extra: dict[str, str] = {}
    for raw in event.action.split(","):
        token = raw.strip()
        if not token:
            continue
        if (m := _ACTION_PAIR.match(token)) is not None:
            key, value = m.group(1).strip(), m.group(2).strip()
            if (lock := lock_from_label(value)) is not None:
                if (name := event_lock_key(key)) is not None:
                    values[name] = RemoteLock(lock)
                else:
                    unnamed.append(RemoteLock(lock))
            else:
                extra[key] = value
            continue
        if (m := _ACTION_TEMP.match(token)) is not None:
            values["setpoint"] = float(m.group(1).replace(",", "."))
            continue
        if (power := power_from_label(token)) is not None and "power" not in values:
            values["power"] = PowerState(power)
            continue
        if (mode := mode_from_label(token)) is not None and "mode" not in values:
            values["mode"], values["mode_label"] = mode, token
            continue
        if "setpoint" in token.lower() and "mode" not in values:
            values["mode"], values["mode_label"] = "setpoint", token
            continue
        extra.setdefault(token, "")
    # A lock whose key is unknown: fall back to the order the iTM lists them in
    # (start/stop, mode, setpoint), skipping the locks already named.
    free = [
        n for n in ("lock_start_stop", "lock_mode", "lock_setpoint") if n not in values
    ]
    values.update(dict(zip(free, unnamed, strict=False)))
    return replace(event, extra=extra, **values)


def schedule_day_from_screen(state: UIState) -> ScheduleDay:
    """Read the day overview: the program's events on the shown day."""
    s = MenuScreen.SCHEDULE_DAY
    title = _text(state, s, 0, "LABEL") or ""
    return ScheduleDay(
        program=title.split(":", 1)[1].strip() if ":" in title else title,
        day=_text(state, s, SD_DAY_LABEL, "LABEL") or "",
        events=tuple(_event_rows(state, s, SD_LIST)),
    )


def day_events_from_screen(state: UIState) -> list[ScheduleEvent]:
    """Read the editor's event list of a weekday or special day."""
    return _event_rows(state, MenuScreen.SCHEDULE_EVENTS, EV_LIST)


def _included(state: UIState, checkbox: int) -> bool:
    return bool(state.get(MenuScreen.SCHEDULE_EVENT, checkbox, "CHECK_STATE") == 1)


def _lock(state: UIState, group: tuple[int, tuple[int, ...]]) -> RemoteLock | None:
    checkbox, radios = group
    if not _included(state, checkbox):
        return None
    i = _checked(state, MenuScreen.SCHEDULE_EVENT, radios)
    values: tuple[RemoteLock, ...] = (
        RemoteLock.ALLOWED,
        RemoteLock.STOP_ONLY,
        RemoteLock.FORBIDDEN,
    )
    if len(radios) == 2:
        values = (RemoteLock.ALLOWED, RemoteLock.FORBIDDEN)
    return values[i] if i is not None and i < len(values) else RemoteLock.UNKNOWN


def _combo_text(state: UIState, part: int) -> str | None:
    s = MenuScreen.SCHEDULE_EVENT
    options = state.get(s, part, "COMBO_STRS") or []
    idx = state.get(s, part, "SINGLE_SELIDX")
    return options[idx] if idx is not None and 0 <= idx < len(options) else None


def event_from_screen(state: UIState, event: ScheduleEvent) -> ScheduleEvent:
    """Fill an event's typed settings from its open settings screen."""
    s = MenuScreen.SCHEDULE_EVENT
    values: dict[str, Any] = {"details": True}
    if _included(state, EVT_POWER[0]):
        i = _checked(state, s, EVT_POWER[1])
        states = (
            PowerState.ON,
            PowerState.OFF,
            PowerState.SETBACK_HIGH,
            PowerState.SETBACK_LOW,
        )
        values["power"] = states[i] if i is not None else None
    values["lock_start_stop"] = _lock(state, EVT_LOCK_START_STOP)
    values["lock_mode"] = _lock(state, EVT_LOCK_MODE)
    values["lock_setpoint"] = _lock(state, EVT_LOCK_SETPOINT)
    if _included(state, EVT_MODE):
        label = _combo_text(state, EVT_MODE_COMBO)
        values["mode_label"] = label
        mode = mode_from_label(label)
        # The event's mode list has one more option than a unit's: a setpoint-only
        # event (shown as "Setpoint" in every language seen so far).
        values["mode"] = mode or (
            "setpoint" if label and "setpoint" in label.lower() else None
        )
    if _included(state, EVT_SETPOINT):
        if state.get(s, EVT_SETPOINT_SHIFT, "CHECK_STATE") == 1:
            values["setpoint_shift"] = _combo_text(state, EVT_SETPOINT_SHIFT_COMBO)
        else:
            text = _text(state, s, EVT_SETPOINT_TEXT)
            try:
                values["setpoint"] = float(text.replace(",", ".")) if text else None
            except ValueError:
                values["setpoint"] = None
    if _included(state, EVT_OVERTIME):
        idx = state.get(s, EVT_OVERTIME_COMBO, "SINGLE_SELIDX")
        values["overtime"] = None if idx is None else idx == 0
    if _included(state, EVT_FAN):
        values["fan_speed"] = FAN_ICONS.get(state.get(s, EVT_FAN_ICON, "ICONID"))
    if _included(state, EVT_VENT_MODE):
        values["ventilation_mode"] = _combo_text(state, EVT_VENT_MODE_COMBO)
    if _included(state, EVT_AIR_VOLUME):
        values["air_volume"] = _combo_text(state, EVT_AIR_VOLUME_COMBO)
    time_text = _text(state, s, EVT_TIME)
    target = _text(state, s, EVT_TARGET)
    if time_text:
        values["time"] = time_text
    if target:
        values["target"] = target
    return replace(event, **values)


def _slots_from_bar(entries: Any) -> tuple[int, ...]:
    """Return the minutes of the day at which a pattern bar has an event."""
    if not entries:
        return ()
    return tuple(
        entry[0] * SLOT_MINUTES
        for entry in entries
        if len(entry) >= 3 and (entry[1] != -1 or entry[2] != -1)
    )


def _period_from_editor(state: UIState) -> tuple[str, str] | None:
    """Read the editor's period combos: ("<month>/<day>", "<month>/<day>")."""
    s = MenuScreen.SCHEDULE_EDITOR
    if state.get(s, SE_PERIOD_CHECK, "CHECK_STATE") != 1:
        return None
    values = [state.get(s, p, "SINGLE_SELIDX") for p in SE_PERIOD]
    if any(v is None for v in values):
        return None

    def option(part: int, idx: int) -> str:
        options = (
            state.get(s, part, "COMBO_STRS") or state.get(s, part, "COMBO_NUMS") or []
        )
        return str(options[idx]) if 0 <= idx < len(options) else str(idx + 1)

    begin = f"{option(SE_PERIOD[0], values[0])}/{option(SE_PERIOD[1], values[1])}"
    end = f"{option(SE_PERIOD[2], values[2])}/{option(SE_PERIOD[3], values[3])}"
    return (begin, end)


def program_from_editor(state: UIState) -> ScheduleProgram:
    """Read the editor's properties tab and whatever pattern bars it has shown."""
    s = MenuScreen.SCHEDULE_EDITOR
    enabled = _checked(state, s, (SE_ENABLED, SE_DISABLED))
    period = _period_from_editor(state)
    weekly_slots = {
        day: _slots_from_bar(state.get(s, part, "TIMELINE_BAR"))
        for day, part in enumerate(SE_WEEKLY_BARS)
        if state.get(s, part, "TIMELINE_BAR") is not None
    }
    special_slots = {
        f"Ex{i + 1}": _slots_from_bar(state.get(s, part, "TIMELINE_BAR"))
        for i, part in enumerate(SE_SPECIAL_BARS)
        if state.get(s, part, "TIMELINE_BAR")
    }
    calendar = tuple(
        CalendarRule(*row[:3])
        for row in _rows(state, s, SE_CALENDAR_LIST)
        if len(row) >= 3
    )
    return ScheduleProgram(
        name=_text(state, s, SE_NAME) or "",
        enabled=enabled == 0 if enabled is not None else False,
        period=period,
        weekly_slots=weekly_slots,
        special_slots=special_slots,
        calendar=calendar,
    )


def history_from_screen(state: UIState) -> History:
    """Read the history screen's current page."""
    s = MenuScreen.HISTORY
    columns = tuple(
        html.unescape(str(c[0])) for c in (state.get(s, H_LIST, "LIST_COLUMN") or [])
    )
    rows = _rows(state, s, H_LIST)
    if rows and len(rows[0]) != HISTORY_COLUMNS:
        msg = f"unexpected history layout: {len(rows[0])} columns {columns}"
        raise ProtocolError(msg)
    entries = tuple(HistoryEntry(*row) for row in rows)
    return History(
        day=_text(state, s, H_DATE),
        entries=entries,
        columns=columns,
        page=_text(state, s, H_PAGE_LABEL, "LABEL"),
    )


def parse_shown_date(text: str | None, date_format: str | None) -> date | None:
    """Parse a date as the iTM shows it (e.g. the history's day)."""
    for fmt in ([date_format] if date_format else []) + list(DATE_FORMATS):
        try:
            return datetime.strptime(text or "", fmt).date()  # noqa: DTZ007 - a date only
        except ValueError:
            continue
    return None
