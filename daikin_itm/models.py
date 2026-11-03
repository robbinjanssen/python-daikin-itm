"""Typed view of the units as the iTM shows them.

The operation panel (screen 4) and the list view (screen 2) give a unit's state, the
settings dialog (screen 7) and the Details screen (screen 9) its settings and identity.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .exceptions import ItmCommandError, ProtocolError
from .lang import (
    MODE_LABELS,
    analog_key,
    detail_key,
    lock_from_label,
    mode_from_label,
    on_off,
)
from .protocol import ScreenID

if TYPE_CHECKING:
    from .state import UIState

# Operation panel part IDs (ScMainOperation.initScreen in script.js).
P_NAME, P_STATE_ICON, P_FILTER_ICON, P_TEMP, P_MODE = 0, 1, 2, 4, 5
P_DETAILS_BUTTON, P_POWER, P_SETPOINT, P_FAN_ICON = 7, 9, 11, 14
P_SETTINGS_BUTTON, P_ERROR_CODE = 17, 19

# List view (screen 2, ScMainStdList): one PNTLIST part with every unit of the zone.
L_LIST, L_BTN_ICON = 8, 6
# Its columns, as the iTM lays them out (ScMainStdList.initScreen); the headers are
# in the iTM's language, so columns are addressed by index.
LIST_COLUMNS = 25
(
    LC_NAME,
    LC_STATUS,
    LC_CHANGEOVER,
    LC_MODE,
    LC_TEMP,
    LC_CO2,
    LC_SETPOINT,
    LC_COOL_LIMIT,
    LC_HEAT_LIMIT,
    LC_FILTER,
    LC_DUST,
    LC_FAN,
) = range(12)
LC_AIRFLOW = 12
LC_LOCK_START_STOP, LC_LOCK_MODE, LC_LOCK_SETPOINT = 19, 20, 21
LC_BLOCKED, LC_ERROR, LC_R32 = 22, 23, 24
ICON_ID_ZONE = 1000
ICON_ID_CHANGEOVER_OPTION = 4000  # "Omschakel optie" set on the unit

# Second value of the state icon (ICONID_MODE) as seen on the device.
ICON_STATE_ON, ICON_STATE_OFF, ICON_STATE_SETBACK = 0, 2, 11

# Operation mode labels are localised; every language's labels, see lang.py. Kept
# for compatibility, the lookup itself is normalize_mode().
MODES = {label: mode for table in MODE_LABELS.values() for label, mode in table.items()}

# Fan speed icons (bars / "A" in the web UI's own images); the settings dialog
# cycles through them in this order. The list view shows the same icons one lower.
FAN_ICONS = {3009: "low", 3011: "medium", 3013: "high", 3015: "auto"}
FAN_SPEEDS = tuple(FAN_ICONS.values())
FAN_ICONS_LIST = {icon - 1: speed for icon, speed in FAN_ICONS.items()}

# Settings dialog (screen 7, ScMainSetting in script.js).
D_OK, D_CANCEL = 5, 6
D_POWER_CHECK, D_POWER_START, D_POWER_STOP = 2, 3, 4
D_MODE_CHECK, D_MODE = 17, 18
D_FAN_CHECK, D_FAN_UP, D_FAN_DOWN, D_FAN_ICON = 28, 29, 30, 31
D_AIRFLOW_CHECK, D_AIRFLOW_UP, D_AIRFLOW_DOWN, D_AIRFLOW_ICON = 88, 89, 90, 91
D_FILTER_RESET, D_DUST_RESET, D_DUST_FORCED_RESET = 86, 142, 143
D_VENT_MODE_CHECK, D_VENT_MODE, D_VENT_AMOUNT_CHECK, D_VENT_AMOUNT = 33, 34, 35, 36
# Airflow direction, in the order the settings dialog's "down" button steps through
# them (it wraps around; "up" goes the other way), with the dialog's icons. The list
# view shows the small icon, one lower.
AIRFLOW_DIRECTIONS = (
    "position_0",  # horizontal
    "position_1",
    "position_2",
    "position_3",
    "position_4",  # down
    "swing",
)
AIRFLOW_ICONS = dict(
    zip((3101, 3103, 3105, 3107, 3109, 3111), AIRFLOW_DIRECTIONS, strict=True)
)
AIRFLOW_ICONS_LIST = {icon - 1: direction for icon, direction in AIRFLOW_ICONS.items()}
# Checkboxes that make a setting part of the change (the browser ticks them first).
D_LOCK_START_STOP_CHECK, D_LOCK_MODE_CHECK, D_LOCK_SETPOINT_CHECK = 7, 11, 14
D_OVERTIME_CHECK, D_LIMIT_CHECK = 26, 60
# The setpoint limits' "change" buttons, which open a number input dialog.
D_COOL_MAX_BUTTON, D_COOL_MIN_BUTTON = 67, 71
D_HEAT_MAX_BUTTON, D_HEAT_MIN_BUTTON = 77, 81
# Wall remote locks: three radio groups (allowed / stop only / forbidden).
D_LOCK_START_STOP = (8, 9, 10)
D_LOCK_MODE = (12, 13)  # allowed / forbidden
D_LOCK_SETPOINT = (15, 16)
# Overtime timer on/off (combo: AAN / UIT by index).
D_OVERTIME = 27
# Setpoint limits: enable combo (by index: on / off), max and min text.
D_COOL_LIMIT, D_COOL_MAX, D_COOL_MIN = 63, 65, 69
D_HEAT_LIMIT, D_HEAT_MAX, D_HEAT_MIN = 73, 75, 79
# The dialog's tabs (TAB_NAME, TAB_VISIBLE): standard, remote locks, air conditioning,
# ventilation, outputs (Dio, Ao, Mo), HW supply. A unit gets the tabs that apply.
D_TABS = 1
TAB_STANDARD, TAB_LOCKS, TAB_AIRCON, TAB_VENTILATION, TAB_OUTPUTS, TAB_HW_SUPPLY = (
    range(6)
)
# The R32 alarm stop checkbox (standard tab; read only, offered or not).
D_R32_ALARM_STOP = 144
# Leaving water setpoints (air conditioning tab) and the HW supply tab's settings:
# checkbox, then text and "change" button (number input) or combo, or start/stop.
D_LW_COOL_CHECK, D_LW_COOL_TEXT, D_LW_COOL_BUTTON = 111, 112, 114
D_LW_HEAT_CHECK, D_LW_HEAT_TEXT, D_LW_HEAT_BUTTON = 115, 116, 118
D_REHEAT_CHECK, D_REHEAT_START, D_REHEAT_STOP = 107, 108, 110
D_LOW_NOISE_CHECK, D_LOW_NOISE = 119, 120
D_STORAGE_CHECK, D_STORAGE_TEXT, D_STORAGE_BUTTON = 123, 124, 126
# Not in script.js's ScMainSetting but in its schedule event screen (ScSchEvent) with
# the same IDs; a real iTM's settings dialog sends them too (hidden there): the outdoor
# unit's low noise and capacity limit (COMBO_NUMS, %).
D_OUTDOOR_LOW_NOISE_CHECK, D_OUTDOOR_LOW_NOISE = 140, 141
D_CAPACITY_CHECK, D_CAPACITY = 147, 148
# The outputs tab: analog output value (Ao), the digital output's repeat mode (Dio:
# enable / disable and its interval in minutes, COMBO_NUMS), multistate output (Mo).
D_ANALOG_CHECK, D_ANALOG_TEXT, D_ANALOG_BUTTON = 37, 38, 40
D_REPEAT_CHECK, D_REPEAT_ON, D_REPEAT_OFF, D_REPEAT_INTERVAL = 94, 96, 97, 98
D_MULTISTATE_CHECK, D_MULTISTATE = 129, 130


@dataclass(frozen=True)
class DialogSetting:
    """An optional setting of the settings dialog, offered only by some units.

    kind says how it is set once its checkbox is ticked: "combo" (an option by label),
    "numbers" (a COMBO_NUMS option by value), "radios" (parts: on, off) or "number"
    (parts: text, "change" button that opens a number input).
    """

    key: str
    tab: int
    check: int
    kind: str
    parts: tuple[int, ...]


# Mapped from script.js (ScMainSetting / ScSchEvent); not seen offered on a real iTM
# yet (one showed all of them hidden or disabled), so not validated on a device.
DIALOG_SETTINGS = (
    DialogSetting("low_noise", TAB_HW_SUPPLY, D_LOW_NOISE_CHECK, "combo", (120,)),
    DialogSetting(
        "outdoor_low_noise",
        TAB_HW_SUPPLY,
        D_OUTDOOR_LOW_NOISE_CHECK,
        "combo",
        (D_OUTDOOR_LOW_NOISE,),
    ),
    DialogSetting("capacity", TAB_HW_SUPPLY, D_CAPACITY_CHECK, "numbers", (148,)),
    DialogSetting(
        "reheat",
        TAB_HW_SUPPLY,
        D_REHEAT_CHECK,
        "radios",
        (D_REHEAT_START, D_REHEAT_STOP),
    ),
    DialogSetting(
        "leaving_water_cool",
        TAB_AIRCON,
        D_LW_COOL_CHECK,
        "number",
        (D_LW_COOL_TEXT, D_LW_COOL_BUTTON),
    ),
    DialogSetting(
        "leaving_water_heat",
        TAB_AIRCON,
        D_LW_HEAT_CHECK,
        "number",
        (D_LW_HEAT_TEXT, D_LW_HEAT_BUTTON),
    ),
    DialogSetting(
        "storage_water",
        TAB_HW_SUPPLY,
        D_STORAGE_CHECK,
        "number",
        (D_STORAGE_TEXT, D_STORAGE_BUTTON),
    ),
    DialogSetting(
        "output_repeat",
        TAB_OUTPUTS,
        D_REPEAT_CHECK,
        "radios",
        (D_REPEAT_ON, D_REPEAT_OFF),
    ),
    DialogSetting(
        "output_repeat_interval",
        TAB_OUTPUTS,
        D_REPEAT_CHECK,
        "numbers",
        (D_REPEAT_INTERVAL,),
    ),
    DialogSetting(
        "analog_output",
        TAB_OUTPUTS,
        D_ANALOG_CHECK,
        "number",
        (D_ANALOG_TEXT, D_ANALOG_BUTTON),
    ),
    DialogSetting(
        "multistate_output", TAB_OUTPUTS, D_MULTISTATE_CHECK, "combo", (130,)
    ),
)
DIALOG_SETTING_KEYS = frozenset(d.key for d in DIALOG_SETTINGS)

# Details screen (screen 9, ScMainDetail): read-only text fields, closed with part 15.
DT_NAME, DT_ID, DT_TYPE, DT_PORT, DT_ADDRESS = 2, 4, 6, 8, 10
DT_INFO, DT_CHARS, DT_CLOSE = 12, 14, 15

_TEMP = re.compile(r"-?\d+(?:[.,]\d+)?")
# A setpoint limit as the list view shows it: "18.0°C-27.0°C" (the first number may
# be negative, the dash between the two is the separator).
_LIMIT = re.compile(r"(-?\d+(?:[.,]\d+)?)\s*(?:°?[CF]|℃|℉)?\s*-\s*(-?\d+(?:[.,]\d+)?)")
_STATUS_LINE = re.compile(r"^(.+?)\s*\[(.*)\]\s*$")
_ATTRIBUTE_LINE = re.compile(r"^([^:\[\]]+?)\s*:\s*(.*)$")

# Main menu bar (screen 0, ScMainMenu): the error indicator and its button, shown
# only while a unit has an error.
MM_ERROR_ICON, MM_ERROR_BUTTON = 18, 19
# Error information (screen 10, ScMainErrorInformation).
E_CLOSE, E_LIST, E_INQUIRY, E_ADDRESS = 1, 3, 4, 5
# The state of an analog point's icon when it is not in alarm (as seen on a device;
# the alarm state itself has not been observed yet).
ANALOG_STATE_NORMAL = 2


class PointKind(StrEnum):
    """What kind of management point an icon in the iTM stands for."""

    INDOOR = "indoor"  # an indoor unit: the operation panel has it all
    VENTILATION = "ventilation"  # a heat reclaim ventilation unit: on/off only
    ANALOG = "analog"  # an analog input, e.g. an internal alarm on a temperature
    OTHER = "other"  # an icon this client doesn't know


# Icon IDs of the icon view and list view, by kind (as seen on devices).
KIND_BY_ICON = {
    2114: PointKind.INDOOR,
    2102: PointKind.INDOOR,
    2100: PointKind.INDOOR,
    2201: PointKind.VENTILATION,
    2601: PointKind.ANALOG,
}


def point_kind(icon_id: int | None) -> PointKind:
    """Return the kind of point an icon stands for (OTHER when unknown)."""
    return KIND_BY_ICON.get(icon_id, PointKind.OTHER) if icon_id else PointKind.OTHER


class PowerState(StrEnum):
    """What the on/off control of a unit is set to."""

    ON = "on"
    OFF = "off"
    SETBACK_LOW = "setback_low"
    SETBACK_HIGH = "setback_high"
    SETBACK = "setback"  # low or high: the list view doesn't say which


class RemoteLock(StrEnum):
    """What the wall remote controller is allowed to do."""

    ALLOWED = "allowed"
    STOP_ONLY = "stop_only"
    FORBIDDEN = "forbidden"
    UNKNOWN = "unknown"  # a label in a language lang.py doesn't know yet


@dataclass(frozen=True)
class RemoteLocks:
    """The wall remote locks of a unit."""

    start_stop: RemoteLock
    mode: RemoteLock
    setpoint: RemoteLock


@dataclass(frozen=True)
class TempLimit:
    """A setpoint limit of a unit (cooling or heating)."""

    enabled: bool | None
    minimum: float | None
    maximum: float | None


@dataclass(frozen=True)
class UnitSettings:  # pylint: disable=too-many-instance-attributes
    """A unit's settings and status that only its dialogs show."""

    name: str
    thermo_on: bool | None  # the thermostat is calling for heating/cooling
    r32_alarm: bool | None
    remote_locks: RemoteLocks
    cool_limit: TempLimit
    heat_limit: TempLimit
    overtime: bool | None  # the overtime timer is on for the unit
    # Every "<key> [<value>]" line of the Details screen, as shown.
    flags: dict[str, str] = field(default_factory=dict)
    # None when the unit has no adjustable airflow direction.
    airflow_direction: str | None = None
    # The dialog offers these only while there is a filter sign / dust alarm.
    filter_sign_resettable: bool = False
    dust_alarm_resettable: bool = False
    # A ventilation unit's mode and air volume, as shown, and the options offered
    # (None and empty when the unit doesn't offer them).
    ventilation_mode: str | None = None
    ventilation_modes: tuple[str, ...] = ()
    ventilation_amount: str | None = None
    ventilation_amounts: tuple[str, ...] = ()
    # The dialog's tabs this unit gets (their names, as shown).
    tabs: tuple[str, ...] = ()
    # The dialog offers to stop the R32 alarm.
    r32_alarm_stoppable: bool = False
    # The settings in DIALOG_SETTINGS: None (and no options) when the unit doesn't
    # offer them. Water setpoints in the iTM's temperature unit.
    low_noise: str | None = None
    low_noise_options: tuple[str, ...] = ()
    outdoor_low_noise: str | None = None
    outdoor_low_noise_options: tuple[str, ...] = ()
    capacity: int | None = None  # the outdoor unit's capacity limit, %
    capacity_options: tuple[int, ...] = ()
    reheat: bool | None = None  # reheat on/off
    leaving_water_cool: float | None = None
    leaving_water_heat: float | None = None
    storage_water: float | None = None
    output_repeat: bool | None = None  # a digital output's repeat mode
    output_repeat_interval: int | None = None  # minutes
    output_repeat_interval_options: tuple[int, ...] = ()
    analog_output: float | None = None
    multistate_output: str | None = None
    multistate_output_options: tuple[str, ...] = ()


def normalize_mode(label: str | None) -> str | None:
    """Map a localised operation mode label to heat/cool/fan/dry/auto.

    Args:
    ----
        label: The label as the iTM shows it.

    Returns:
    -------
        The normalized mode, or None when the label is unknown.

    """
    return mode_from_label(label)


def _temperature(text: str | None) -> float | None:
    """Read the number out of a temperature label like ``22.6°C``."""
    m = _TEMP.search(text or "")
    return float(m.group().replace(",", ".")) if m else None


def _temperature_unit(text: str | None) -> str:
    """Return °F when a temperature label says so, else °C."""
    return "°F" if text and ("°F" in text or "℉" in text) else "°C"


@dataclass(frozen=True)
class Capabilities:
    """Static facts about a unit.

    Its identity (Details screen) and what its settings dialog offers. Read once;
    none of this changes while running.
    """

    name: str
    # Normalized mode -> label as the iTM shows it, in dialog order.
    mode_labels: dict[str, str]
    fan_speed: bool
    point_id: str | None = None  # iTM management point ID, e.g. "105"
    # Bus port:address, e.g. "1:1-04"; stable, survives renames.
    address: str | None = None
    unit_type: str | None = None  # e.g. "Indoor unit", in the iTM's language
    description: str | None = None  # free text set in the iTM
    zones: tuple[str, ...] = field(default_factory=tuple)  # zone paths of the unit
    # Mode labels the iTM showed that lang.py doesn't know: the unit's modes are
    # incomplete then, which deserves a warning to the user.
    unknown_labels: tuple[str, ...] = field(default_factory=tuple)

    @property
    def modes(self) -> tuple[str, ...]:
        """The normalized modes the unit offers, in dialog order."""
        return tuple(self.mode_labels)

    @property
    def key(self) -> str:
        """Stable identifier: the bus address when known, else the name."""
        return self.address or self.name


def details_from_screen(state: UIState) -> dict[str, Any]:
    """Read a unit's identity from the open Details screen.

    Args:
    ----
        state: The merged UI state with the Details screen open.

    Returns:
    -------
        Keyword arguments for Capabilities: point_id, address, unit_type,
        description and zones.

    """
    s = ScreenID.DETAILS

    def text(part: int) -> str | None:
        value = state.get(s, part, "TEXT")
        return html.unescape(value).strip() or None if value else None

    port, address = text(DT_PORT), text(DT_ADDRESS)
    return {
        "point_id": text(DT_ID),
        "address": f"{port}:{address}" if port and address else None,
        "unit_type": text(DT_TYPE),
        "description": text(DT_INFO),
        "zones": _zones_from_chars(text(DT_CHARS) or ""),
    }


def _zones_from_chars(chars: str) -> tuple[str, ...]:
    """Pick the zone paths out of the Details "characteristics" text.

    The text starts with a localised "Zone:" line, then one path per line
    (``Building>Floor>Zone``), then a blank line and status lines. The heading is
    skipped by position, not by its text, so the iTM's language doesn't matter; a
    first line that is a path itself (no heading) is kept.
    """
    lines = chars.splitlines()
    start = 0 if lines and ">" in lines[0] else 1
    zones = []
    for line in lines[start:]:
        if not line.strip():
            break
        zones.append(line.strip())
    return tuple(zones)


def status_from_details(state: UIState) -> dict[str, Any]:
    """Read the status lines of the open Details screen.

    Args:
    ----
        state: The merged UI state with the Details screen open.

    Returns:
    -------
        ``thermo_on`` and ``r32_alarm`` (None when the iTM doesn't show them, or
        shows them in a language lang.py doesn't know), and ``flags``: every
        ``<key> [<value>]`` line as shown.

    """
    chars = html.unescape(state.get(ScreenID.DETAILS, DT_CHARS, "TEXT") or "")
    flags: dict[str, str] = {}
    known: dict[str, bool | None] = {"thermo": None, "r32": None}
    for line in chars.splitlines():
        if (m := _STATUS_LINE.match(line.strip())) is None:
            continue
        key, value = m.group(1), m.group(2)
        flags[key] = value
        if (name := detail_key(key)) is not None:
            known[name] = on_off(value)
    return {"thermo_on": known["thermo"], "r32_alarm": known["r32"], "flags": flags}


def capabilities_from_dialog(
    name: str, state: UIState, details: dict[str, Any] | None = None
) -> Capabilities:
    """Read what a unit offers from its open settings dialog.

    Args:
    ----
        name: The unit's name.
        state: The merged UI state with the settings dialog open.
        details: The unit's identity, see details_from_screen.

    Returns:
    -------
        The unit's capabilities.

    """
    s = ScreenID.SETTINGS
    labels = state.get(s, D_MODE, "COMBO_STRS", []) or []
    modes = {}
    unknown = []
    for label in labels:
        if (mode := normalize_mode(label)) is None:
            unknown.append(label)
        elif mode not in modes:
            modes[mode] = label
    return Capabilities(
        name=name,
        mode_labels=modes,
        fan_speed=state.visible(s, D_FAN_CHECK)
        and state.get(s, D_FAN_ICON, "ICONID") in FAN_ICONS,
        unknown_labels=tuple(unknown),
        **(details or {}),
    )


def _checked(state: UIState, parts: tuple[int, ...]) -> int | None:
    """Return the index of the checked radio button, or None when none is."""
    s = ScreenID.SETTINGS
    for i, part in enumerate(parts):
        if state.get(s, part, "CHECK_STATE") == 1:
            return i
    return None


def _lock_from_radios(
    state: UIState, parts: tuple[int, ...], values: tuple[RemoteLock, ...]
) -> RemoteLock:
    i = _checked(state, parts)
    return values[i] if i is not None and i < len(values) else RemoteLock.UNKNOWN


def _limit_from_dialog(
    state: UIState, enable: int, maximum: int, minimum: int
) -> TempLimit:
    s = ScreenID.SETTINGS
    idx = state.get(s, enable, "SINGLE_SELIDX")
    return TempLimit(
        enabled=None if idx is None else idx == 0,
        minimum=_temperature(state.get(s, minimum, "TEXT")),
        maximum=_temperature(state.get(s, maximum, "TEXT")),
    )


def settings_from_dialog(
    name: str, state: UIState, status: dict[str, Any] | None = None
) -> UnitSettings:
    """Read a unit's settings from its open settings dialog.

    Args:
    ----
        name: The unit's name.
        state: The merged UI state with the settings dialog open.
        status: The unit's Details status, see status_from_details.

    Returns:
    -------
        The unit's settings.

    """
    s = ScreenID.SETTINGS
    status = status or {}
    three = (RemoteLock.ALLOWED, RemoteLock.STOP_ONLY, RemoteLock.FORBIDDEN)
    two = (RemoteLock.ALLOWED, RemoteLock.FORBIDDEN)
    overtime_idx = state.get(s, D_OVERTIME, "SINGLE_SELIDX")
    return UnitSettings(
        name=name,
        thermo_on=status.get("thermo_on"),
        r32_alarm=status.get("r32_alarm"),
        remote_locks=RemoteLocks(
            start_stop=_lock_from_radios(state, D_LOCK_START_STOP, three),
            mode=_lock_from_radios(state, D_LOCK_MODE, two),
            setpoint=_lock_from_radios(state, D_LOCK_SETPOINT, two),
        ),
        cool_limit=_limit_from_dialog(state, D_COOL_LIMIT, D_COOL_MAX, D_COOL_MIN),
        heat_limit=_limit_from_dialog(state, D_HEAT_LIMIT, D_HEAT_MAX, D_HEAT_MIN),
        overtime=None if overtime_idx is None else overtime_idx == 0,
        flags=dict(status.get("flags", {})),
        airflow_direction=AIRFLOW_ICONS.get(state.get(s, D_AIRFLOW_ICON, "ICONID"))
        if state.visible(s, D_AIRFLOW_CHECK)
        else None,
        filter_sign_resettable=_offered(state, D_FILTER_RESET),
        dust_alarm_resettable=_offered(state, D_DUST_RESET),
        **_combo_setting(state, D_VENT_MODE_CHECK, D_VENT_MODE, "ventilation_mode"),
        **_combo_setting(
            state, D_VENT_AMOUNT_CHECK, D_VENT_AMOUNT, "ventilation_amount"
        ),
        tabs=_visible_tabs(state),
        r32_alarm_stoppable=_usable(state, D_R32_ALARM_STOP)
        and _tab_visible(state, TAB_STANDARD),
        **dialog_settings(state),
    )


def _usable(state: UIState, part: int) -> bool:
    """Tell whether a part is offered and not disabled (clicking that hangs the iTM)."""
    return _offered(state, part) and state.get(ScreenID.SETTINGS, part, "ENABLED") != 0


def _tab_visible(state: UIState, tab: int) -> bool:
    """Tell whether the dialog shows a tab (all are, when it doesn't say)."""
    value = state.get(ScreenID.SETTINGS, D_TABS, "TAB_VISIBLE")
    flags = value[1] if isinstance(value, list) and len(value) > 1 else []
    return not 0 <= tab < len(flags) or flags[tab] != 0


def _visible_tabs(state: UIState) -> tuple[str, ...]:
    """Return the names of the dialog's tabs the unit gets."""
    names = state.get(ScreenID.SETTINGS, D_TABS, "TAB_NAME") or []
    return tuple(
        " ".join(html.unescape(str(n)).split())
        for i, n in enumerate(names)
        if _tab_visible(state, i)
    )


def setting_offered(state: UIState, setting: DialogSetting) -> bool:
    """Tell whether the open settings dialog offers an optional setting.

    Args:
    ----
        state: The merged UI state with the settings dialog open.
        setting: One of DIALOG_SETTINGS.

    Returns:
    -------
        True when its checkbox is shown, enabled and on a tab the unit gets.

    """
    return _usable(state, setting.check) and _tab_visible(state, setting.tab)


def _options(state: UIState, setting: DialogSetting) -> tuple[Any, ...]:
    s = ScreenID.SETTINGS
    if setting.kind == "numbers":
        return tuple(state.get(s, setting.parts[0], "COMBO_NUMS") or [])
    return tuple(
        str(o).strip() for o in state.get(s, setting.parts[0], "COMBO_STRS") or []
    )


def _setting_value(state: UIState, setting: DialogSetting) -> Any:
    """Read the value an optional setting shows."""
    s = ScreenID.SETTINGS
    if setting.kind == "radios":
        i = _checked(state, setting.parts)
        return None if i is None else i == 0
    if setting.kind == "number":
        return _temperature(state.get(s, setting.parts[0], "TEXT"))
    options = _options(state, setting)
    idx = state.get(s, setting.parts[0], "SINGLE_SELIDX")
    return options[idx] if isinstance(idx, int) and 0 <= idx < len(options) else None


def dialog_settings(state: UIState) -> dict[str, Any]:
    """Read the optional settings (DIALOG_SETTINGS) the unit's dialog offers.

    Args:
    ----
        state: The merged UI state with the settings dialog open.

    Returns:
    -------
        The UnitSettings fields of the offered settings, with their options.

    """
    values: dict[str, Any] = {}
    for setting in DIALOG_SETTINGS:
        if not setting_offered(state, setting):
            continue
        values[setting.key] = _setting_value(state, setting)
        if setting.kind in {"combo", "numbers"}:
            values[f"{setting.key}_options"] = _options(state, setting)
    return values


def dialog_setting_choice(state: UIState, setting: DialogSetting, value: Any) -> Any:
    """Check a wanted value against what the dialog offers, and return the pick.

    Args:
    ----
        state: The merged UI state with the settings dialog open.
        setting: One of DIALOG_SETTINGS.
        value: The wanted value: an option's label (combo), one of the numbers
            (numbers), on/off (radios) or a number (number).

    Returns:
    -------
        The option's index (combo, numbers), the radio part to click (radios), or the
        number (number; its range is checked by the iTM's number input).

    Raises:
    ------
        ItmCommandError: The unit doesn't offer the setting or the value.

    """
    what = setting.key.replace("_", " ")
    if not setting_offered(state, setting):
        msg = f"{what}: not offered by this unit"
        raise ItmCommandError(msg)
    if setting.kind == "radios":
        return setting.parts[0] if value else setting.parts[1]
    if setting.kind == "number":
        return float(value)
    options = _options(state, setting)
    if setting.kind == "numbers":
        wanted: list[Any] = list(options)
        key: Any = value
    else:
        wanted = [str(o).casefold() for o in options]
        key = str(value).strip().casefold()
    if key not in wanted:
        msg = f"{what}: {value!r} not offered: {list(options)}"
        raise ItmCommandError(msg)
    return wanted.index(key)


def _offered(state: UIState, part: int) -> bool:
    """Tell whether the settings dialog offers a part (it hides what doesn't apply)."""
    s = ScreenID.SETTINGS
    return state.get(s, part, "VISIBLED") != 0 and any(
        key[0] == part for key in state.screens.get(s, {})
    )


def _combo_setting(state: UIState, check: int, combo: int, name: str) -> dict[str, Any]:
    """Read an optional combo setting of the dialog: its shown option and options."""
    s = ScreenID.SETTINGS
    if not _offered(state, check):
        return {}
    options = tuple(str(o).strip() for o in state.get(s, combo, "COMBO_STRS") or [])
    idx = state.get(s, combo, "SINGLE_SELIDX")
    shown = options[idx] if idx is not None and 0 <= idx < len(options) else None
    return {name: shown, f"{name}s": options}


@dataclass(frozen=True)
class Unit:  # pylint: disable=too-many-instance-attributes
    """The state of an indoor unit as shown on the operation panel or list view."""

    name: str
    power: str | None  # raw on/off option, e.g. "Start", "Stop", "SB Laag" (setback)
    running: bool | None  # True when "Start" (first option); setback is not running
    state_icon: int | None
    setpoint: int | None
    setpoint_range: tuple[int, ...]
    room_temperature: float | None
    mode: str | None  # heat / cool / fan / dry / auto, None when unknown
    mode_label: str | None
    fan_icon: int | None
    fan_speed: str | None  # low / medium / high / auto
    error_code: str | None
    filter_sign: bool
    # What the on/off control is set to; None when it can't be told.
    power_state: PowerState | None = None
    temperature_unit: str = "°C"  # as the iTM shows temperatures
    # Only the list view shows the rest (fetch_units(view="list")).
    cool_limit: tuple[float, float] | None = None  # (min, max) when a limit is set
    heat_limit: tuple[float, float] | None = None
    remote_locks: RemoteLocks | None = None
    r32_alarm: bool | None = None
    changeover_option: bool | None = None  # the "changeover option" is set
    co2: float | None = None
    dust_alarm: bool | None = None
    blocked: bool | None = None
    # From the icon: a ventilation unit has no mode nor setpoint, only on/off.
    kind: PointKind = PointKind.INDOOR
    airflow_direction: str | None = None  # see AIRFLOW_DIRECTIONS (list view)

    @property
    def setback(self) -> bool:
        """Whether the unit is in setback (the state icon shows SB)."""
        return self.state_icon == ICON_STATE_SETBACK


def _selected(options: Any, index: Any) -> Any:
    """Return options[index], or None when the index is missing or out of range."""
    return options[index] if index is not None and 0 <= index < len(options) else None


# The on/off options by their count: Start/Stop, or Start/SB low/SB high/Stop.
_POWER_STATES = {
    2: (PowerState.ON, PowerState.OFF),
    4: (PowerState.ON, PowerState.SETBACK_LOW, PowerState.SETBACK_HIGH, PowerState.OFF),
}
_ICON_POWER_STATES = {
    ICON_STATE_ON: PowerState.ON,
    ICON_STATE_OFF: PowerState.OFF,
    ICON_STATE_SETBACK: PowerState.SETBACK,
}


def unit_from_panel(state: UIState) -> Unit:
    """Build a Unit from the merged operation panel.

    Args:
    ----
        state: The merged UI state with the unit selected.

    Returns:
    -------
        The unit.

    """
    s = ScreenID.OPERATION
    get = state.get

    options = get(s, P_POWER, "COMBO_STRS", []) or []
    power_idx = get(s, P_POWER, "SINGLE_SELIDX")
    nums = tuple(get(s, P_SETPOINT, "COMBO_NUMS", []) or [])

    temp_label = get(s, P_TEMP, "LABEL") if state.visible(s, P_TEMP) else None
    mode_label = get(s, P_MODE, "LABEL") if state.visible(s, P_MODE) else None
    icon = get(s, P_STATE_ICON, "ICONID_MODE")
    error = get(s, P_ERROR_CODE, "LABEL") if state.visible(s, P_ERROR_CODE) else None
    fan_icon = get(s, P_FAN_ICON, "ICONID")

    return Unit(
        name=get(s, P_NAME, "LABEL"),
        power=_selected(options, power_idx),
        running=None if power_idx is None else power_idx == 0,
        state_icon=icon[1] if icon else None,
        setpoint=_selected(nums, get(s, P_SETPOINT, "SINGLE_SELIDX")),
        setpoint_range=nums,
        room_temperature=_temperature(temp_label),
        mode=normalize_mode(mode_label),
        mode_label=mode_label,
        fan_icon=fan_icon,
        fan_speed=FAN_ICONS.get(fan_icon),
        error_code=error or None,
        filter_sign=state.visible(s, P_FILTER_ICON),
        power_state=_selected(_POWER_STATES.get(len(options), ()), power_idx),
        temperature_unit=_temperature_unit(temp_label),
        kind=point_kind(icon[0]) if icon else PointKind.INDOOR,
    )


def _cell_text(cell: list[Any]) -> str | None:
    """Return a list view cell's text, None when empty or a placeholder (``---``)."""
    text = html.unescape(str(cell[0])).strip()
    return text if text and text != "---" else None


def _cell_icon(cell: list[Any]) -> int | None:
    """Return a list view cell's icon, None when it has none."""
    return cell[1] if cell[1] not in (None, -1) else None


def _limit_from_cell(cell: list[Any]) -> tuple[float, float] | None:
    """Read a setpoint limit cell like ``18.0°C-27.0°C``."""
    m = _LIMIT.search(_cell_text(cell) or "")
    if m is None:
        return None
    return (float(m.group(1).replace(",", ".")), float(m.group(2).replace(",", ".")))


def _lock_from_cell(cell: list[Any]) -> RemoteLock:
    """Read a lock cell: empty means allowed, else a localised label."""
    text = _cell_text(cell)
    if text is None:
        return RemoteLock.ALLOWED
    value = lock_from_label(text)
    return RemoteLock(value) if value else RemoteLock.UNKNOWN


def unit_from_list_row(row: list[list[Any]]) -> Unit:
    """Build a Unit from one row of the list view.

    Args:
    ----
        row: The row's cells, ``[text, icon, state]`` each.

    Returns:
    -------
        The unit. The list view doesn't show the on/off options nor the setpoint
        range, so ``power`` is None and ``setpoint_range`` empty.

    """
    status = row[LC_STATUS]
    state = status[2] if status[2] not in (None, -1) else None
    power_state = _ICON_POWER_STATES.get(state) if state is not None else None
    status_icon = _cell_icon(status)
    mode_label = _cell_text(row[LC_MODE])
    temp_label = _cell_text(row[LC_TEMP])
    fan_icon = _cell_icon(row[LC_FAN])
    setpoint = _temperature(_cell_text(row[LC_SETPOINT]))
    r32 = _cell_text(row[LC_R32])
    return Unit(
        name=html.unescape(str(row[LC_NAME][0])),
        power=None,
        running=None if power_state is None else power_state == PowerState.ON,
        state_icon=state,
        setpoint=int(setpoint) if setpoint is not None else None,
        setpoint_range=(),
        room_temperature=_temperature(temp_label),
        mode=normalize_mode(mode_label),
        mode_label=mode_label,
        fan_icon=fan_icon,
        fan_speed=FAN_ICONS_LIST.get(fan_icon) if fan_icon is not None else None,
        error_code=_cell_text(row[LC_ERROR]),
        filter_sign=_cell_icon(row[LC_FILTER]) is not None
        or _cell_text(row[LC_FILTER]) is not None,
        power_state=power_state,
        temperature_unit=_temperature_unit(temp_label),
        cool_limit=_limit_from_cell(row[LC_COOL_LIMIT]),
        heat_limit=_limit_from_cell(row[LC_HEAT_LIMIT]),
        remote_locks=RemoteLocks(
            start_stop=_lock_from_cell(row[LC_LOCK_START_STOP]),
            mode=_lock_from_cell(row[LC_LOCK_MODE]),
            setpoint=_lock_from_cell(row[LC_LOCK_SETPOINT]),
        ),
        r32_alarm=None if r32 is None else on_off(r32),
        changeover_option=_cell_icon(row[LC_CHANGEOVER]) == ICON_ID_CHANGEOVER_OPTION
        if _cell_icon(row[LC_CHANGEOVER]) is not None
        else None,
        co2=_temperature(_cell_text(row[LC_CO2])),
        dust_alarm=_cell_icon(row[LC_DUST]) is not None
        or _cell_text(row[LC_DUST]) is not None,
        blocked=_cell_text(row[LC_BLOCKED]) is not None,
        kind=point_kind(status_icon) if status_icon else PointKind.INDOOR,
        airflow_direction=AIRFLOW_ICONS_LIST.get(_cell_icon(row[LC_AIRFLOW]) or 0),
    )


def units_from_list(state: UIState) -> dict[str, Unit]:
    """Build every unit of the zone from the open list view.

    Args:
    ----
        state: The merged UI state with the list view shown.

    Returns:
    -------
        The units by name, zones left out (none for a zone holding only zones).

    Raises:
    ------
        ProtocolError: The list view is not shown, or has another layout than
            the one this client knows.

    """
    panel = state.get(ScreenID.STDLIST, L_LIST, "PNTLIST")
    if not panel:
        msg = "the list view is not shown"
        raise ProtocolError(msg)
    if len(panel["cols"]) != LIST_COLUMNS:
        # A zone that holds only zones gets a short list (name, status, type, error
        # code) of its zones: like the icon view, it has no units.
        rows = panel["rows"]
        if rows and all(_cell_icon(row[LC_STATUS]) == ICON_ID_ZONE for row in rows):
            return {}
        headers = [c[0] for c in panel["cols"]]
        msg = f"unexpected list view layout: {len(headers)} columns {headers}"
        raise ProtocolError(msg)
    units = {}
    for row in panel["rows"]:
        if _cell_icon(row[LC_STATUS]) == ICON_ID_ZONE:
            continue
        unit = unit_from_list_row(row)
        units[unit.name] = unit
    return units


@dataclass(frozen=True)
class AnalogPoint:
    """An analog input of the iTM, e.g. an internal alarm on a unit's temperature.

    The operation panel only shows its state icon; what it watches and its limits
    are on its Details screen (``fetch_points(details=True)``).
    """

    name: str
    state_icon: int | None
    alarm: bool | None  # None when the state is unknown
    kind: PointKind = PointKind.ANALOG
    unit: str | None = None  # e.g. "°C"
    source_point: str | None = None  # the unit it watches
    source_value: str | None = None  # which of its values, as the iTM shows it
    upper_limit: float | None = None
    lower_limit: float | None = None
    description: str | None = None
    zones: tuple[str, ...] = field(default_factory=tuple)
    # Every "<key>:<value>" line of the Details screen, as shown.
    attributes: dict[str, str] = field(default_factory=dict)


def analog_from_panel(state: UIState) -> AnalogPoint:
    """Build an AnalogPoint from the merged operation panel.

    Args:
    ----
        state: The merged UI state with the point selected.

    Returns:
    -------
        The point, with only what the panel shows.

    """
    s = ScreenID.OPERATION
    icon = state.get(s, P_STATE_ICON, "ICONID_MODE")
    icon_state = icon[1] if icon else None
    return AnalogPoint(
        name=state.get(s, P_NAME, "LABEL"),
        state_icon=icon_state,
        alarm=None if icon_state is None else icon_state != ANALOG_STATE_NORMAL,
    )


def analog_details_from_screen(state: UIState) -> dict[str, Any]:
    """Read an analog point's attributes from the open Details screen.

    Args:
    ----
        state: The merged UI state with the Details screen open.

    Returns:
    -------
        Keyword arguments for AnalogPoint: description, zones, attributes, and the
        attributes the client knows by field (unit, source_point, ...).

    """
    text = state.get(ScreenID.DETAILS, DT_INFO, "TEXT")
    chars = html.unescape(state.get(ScreenID.DETAILS, DT_CHARS, "TEXT") or "")
    out: dict[str, Any] = {
        "description": html.unescape(text).strip() or None if text else None,
        "zones": _zones_from_chars(chars),
        "attributes": {},
    }
    for line in chars.splitlines():
        m = _ATTRIBUTE_LINE.match(line.strip())
        if m is None or ">" in m.group(2):
            continue
        key, value = m.group(1), m.group(2)
        out["attributes"][key] = value
        if (name := analog_key(key)) in ("upper_limit", "lower_limit"):
            out[name] = _temperature(value)
        elif name is not None:
            out[name] = value
    return out


@dataclass(frozen=True)
class ErrorInformation:
    """What the iTM's error information screen shows while a unit has an error.

    Not yet seen on a device: the rows are passed through as the iTM lists them.
    """

    columns: tuple[str, ...]
    errors: tuple[tuple[str, ...], ...]
    inquiry: str | None  # the "contact" text and address the iTM shows
    address: str | None


def error_information_from_screen(state: UIState) -> ErrorInformation:
    """Read the open error information screen.

    Args:
    ----
        state: The merged UI state with the error information screen open.

    Returns:
    -------
        The errors as listed.

    """
    s = ScreenID.ERROR_INFORMATION
    columns = state.get(s, E_LIST, "LIST_COLUMN") or []
    rows = (state.get(s, E_LIST, "LIST_ROW") or {}).get("rows", [])
    inquiry = state.get(s, E_INQUIRY, "LABEL")
    address = state.get(s, E_ADDRESS, "TEXT")
    return ErrorInformation(
        columns=tuple(html.unescape(str(c[0])) for c in columns),
        errors=tuple(tuple(html.unescape(str(t)) for t in r["text"]) for r in rows),
        inquiry=html.unescape(inquiry).strip() or None if inquiry else None,
        address=html.unescape(address).strip() or None if address else None,
    )
