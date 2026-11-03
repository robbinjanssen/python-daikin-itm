"""The iTM's system settings: the screens on the menu's settings tabs.

Zones, management points, maintenance, network, e-mail, web users, the touch
panel's hardware and screensaver settings, confirm dialogs, remote management,
power proportional distribution (PPD) and the energy navigator. Most of their menu
buttons are hidden, but they open all the same. Everything here is read only.

The screen and part IDs come from the web UI's script.js and from captures of a real
iTM. Values are read by part ID and index; texts the iTM shows in its own language
(types, statuses) are passed through as shown.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from .lang import on_off, user_is_admin
from .menu import MenuScreen

if TYPE_CHECKING:
    from .state import UIState

# Menu list buttons (screen 5) of the system screens.
MB_ZONES, MB_POINTS, MB_MAINTENANCE, MB_NETWORK = 21, 23, 25, 27
MB_EMAIL, MB_WEB_USERS, MB_SCREENSAVER, MB_HARDWARE = 29, 31, 35, 37
MB_CONFIRM_DIALOGS, MB_REMOTE_MANAGEMENT, MB_PPD = 45, 49, 57
MB_ENERGY_EQUIPMENT, MB_ENERGY_EXPORT = 102, 104

# Parts per screen.
ZN_POSITION, ZN_LIST = 2, 4
PT_LIST = 1
MT_IN_MAINTENANCE, MT_AVAILABLE = 2, 8
NW_FIELDS = (8, 9, 10, 11, 12, 13, 14)  # name, hostname, IP, mask, gateway, DNS 1, 2
EM_LIST, EM_ENABLED, EM_DISABLED = 1, 3, 4
WU_LIST = 2
SS_MODE, SS_DELAY, SS_OFF_ON_ERROR, SS_ON_ON_ERROR = 2, 5, 8, 9
SS_DELAY_BUTTON, SS_OK = 6, 10
CD_ENABLED, CD_DISABLED = 2, 3
HW_BACKLIGHT, HW_BUZZER_VOLUME, HW_BUZZER_DURATION, HW_KEY_VOLUME = 3, 6, 8, 11
HW_OK = 12
RM_STATUS, RM_IP = 2, 4
PPD_TIME_LIMIT, PPD_START, PPD_END, PPD_DAY, PPD_PERIOD, PPD_LIST = 2, 5, 8, 13, 20, 21
EE_FROM, EE_TO, EE_TARGET = 2, 5, 8
EX_PERIODS = 3

# Write parts.
MT_ADD, MT_REMOVE, MT_OK = 4, 5, 9
PT_CHANGE = 2  # 1700: Change (after a row is selected) -> 1701
PA_NAME_BUTTON, PA_INFO_BUTTON, PA_OK = 5, 8, 12  # 1701
ZN_SETTINGS = 9  # 1600: Settings (after a row is selected) -> 1603
ZA_ICON = 16  # 1603: the zone's icon
ZA_NAME_BUTTON, ZA_INFO_BUTTON, ZA_START_BUTTON, ZA_STOP_BUTTON, ZA_OK = (
    3,
    6,
    10,
    14,
    18,
)
WU_ADD, WU_CHANGE, WU_DELETE = 4, 5, 6  # 1200
US_NAME_BUTTON, US_PASSWORD_BUTTON, US_ADMIN, US_USER, US_OK = 3, 6, 8, 9, 17  # 1204
US_ZONES_BUTTON = 12  # 1204 -> 1202, the zones a user manages
UZ_LIST, UZ_OK = 2, 3  # 1202
RG_LANGUAGE_BUTTON, RG_OK = 3, 18  # 2500 -> 2501
LG_OK = 12  # 2501
# 2501's language radios, by language code.
LANGUAGE_RADIOS = {
    "en": 2,
    "de": 3,
    "es": 4,
    "pt": 5,
    "ja": 6,
    "fr": 7,
    "it": 8,
    "nl": 9,
    "zh": 10,
    "ko": 11,
}
TM_CHANGE, TM_DST_ON, TM_DST_OFF, TM_OK = 3, 6, 7, 18  # 2400
# 1800: the Change button of each network field, in NetworkSettings order, and OK.
NW_BUTTONS = {
    "controller_name": 15,
    "hostname": 16,
    "ip_address": 17,
    "subnet_mask": 18,
    "gateway": 19,
    "dns_primary": 20,
    "dns_secondary": 21,
}
NW_TEXT_FIELDS = frozenset({"controller_name", "hostname"})  # the others: IP inputs
NW_OK = 23
TM_DST_START = (9, 10, 11, 12)  # month, week, weekday, time
TM_DST_END = (14, 15, 16, 17)

# Buzzer duration options in order; None is "continuous".
BUZZER_DURATIONS: tuple[int | None, ...] = (1, 3, 5, None)


class ScreensaverMode(StrEnum):
    """What the touch panel does after the screensaver delay (options in order)."""

    OFF = "off"
    BACKLIGHT_OFF = "backlight_off"
    SCREEN_1 = "screen_1"
    SCREEN_2 = "screen_2"
    SCREEN_3 = "screen_3"


# -- models ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ZoneInfo:
    """A zone as the zone settings list it."""

    name: str  # as configured, trailing spaces included
    level: int | None  # 1 for top-level zones; None for the root
    zones: int | None  # zones directly inside it
    points: int | None  # management points directly inside it


@dataclass(frozen=True)
class ManagementPoint:
    """A management point as the point settings list it."""

    kind: str  # the type as shown, e.g. "Indoor unit"
    name: str  # e.g. "1:1-00"
    icon: int | None
    info: str | None  # the description, e.g. "Storage"


@dataclass(frozen=True)
class MaintenancePoint:
    """A management point and whether it is marked as in maintenance."""

    kind: str
    name: str
    in_maintenance: bool | None


@dataclass(frozen=True)
class NetworkSettings:
    """The iTM's own network settings."""

    controller_name: str | None
    hostname: str | None
    ip_address: str | None
    subnet_mask: str | None
    gateway: str | None
    dns_primary: str | None
    dns_secondary: str | None


@dataclass(frozen=True)
class EmailSettings:
    """E-mail notification: whether it is on, and the addresses."""

    enabled: bool | None
    addresses: tuple[tuple[bool | None, str], ...]  # (active, address)


@dataclass(frozen=True)
class WebUser:
    """A web access user (never its password: the iTM doesn't show it)."""

    name: str
    kind: str  # as shown, e.g. "Administrator"
    admin: bool | None


@dataclass(frozen=True)
class HardwareSettings:
    """The touch panel's backlight and buzzer settings."""

    backlight: int | None  # level 1-8
    buzzer_volume: int | None  # 0-5
    buzzer_minutes: int | None  # how long the buzzer sounds; None: continuous
    buzzer_continuous: bool | None
    key_click_volume: int | None  # 0-5


@dataclass(frozen=True)
class ScreensaverSettings:
    """The touch panel's screensaver."""

    mode: ScreensaverMode | None
    delay_minutes: int | None
    off_on_error: bool | None  # the screensaver ends when an error occurs


@dataclass(frozen=True)
class RemoteManagement:
    """The remote management connection, as the iTM shows it."""

    status: str | None  # as shown
    ip_address: str | None
    connected: bool | None  # from the address: 0.0.0.0 means not connected


@dataclass(frozen=True)
class PpdUnit:
    """A unit's share of the power use in the PPD period."""

    name: str
    consumption_kwh: float | None
    standby_kwh: float | None


@dataclass(frozen=True)
class PpdSettings:
    """Power proportional distribution: the period and the use per unit."""

    time_limit: bool | None
    start: str | None  # as shown, in the iTM's date format
    end: str | None
    calculation_day: int | None
    period: str | None
    units: tuple[PpdUnit, ...]


@dataclass(frozen=True)
class EnergyNavigatorInfo:
    """The energy navigator's sampling period and target, and its export periods."""

    sampling_from: str | None
    sampling_to: str | None
    sampling_target: str | None  # zone path as shown, e.g. "Top>All"
    export_periods: tuple[str, ...]


@dataclass(frozen=True)
class ZoneAttributes:
    """A zone's settings screen: name, description and intervals."""

    name: str | None
    info: str | None
    start_interval: int | None
    stop_interval: int | None
    icon: int | None = None  # the zone's icon ID (1000-1009)


@dataclass(frozen=True)
class DstRule:
    """When summer time starts or ends: the nth weekday of a month, at an hour."""

    month: int  # 1-12
    week: int  # 1-4, or 5 for the last
    weekday: int  # 0 Sunday .. 6 Saturday
    hour: int  # local time


# The EU rule: last Sunday of March 02:00 -> last Sunday of October 03:00.
EU_DST_START = DstRule(month=3, week=5, weekday=0, hour=2)
EU_DST_END = DstRule(month=10, week=5, weekday=0, hour=3)


@dataclass(frozen=True)
class DstSettings:
    """Whether the iTM switches to summer time, and when."""

    enabled: bool | None
    start: DstRule | None
    end: DstRule | None


@dataclass(frozen=True)
class SystemSettings:
    """Everything fetch_system reads; None where a screen did not open."""

    zones: tuple[ZoneInfo, ...] | None = None
    zone_position: str | None = None
    points: tuple[ManagementPoint, ...] | None = None
    maintenance: tuple[MaintenancePoint, ...] | None = None
    network: NetworkSettings | None = None
    email: EmailSettings | None = None
    web_users: tuple[WebUser, ...] | None = None
    hardware: HardwareSettings | None = None
    screensaver: ScreensaverSettings | None = None
    confirm_dialogs: bool | None = None
    remote_management: RemoteManagement | None = None
    ppd: PpdSettings | None = None
    energy_navigator: EnergyNavigatorInfo | None = None


# -- readers --------------------------------------------------------------------------


def _text(state: UIState, screen: int, part: int) -> str | None:
    value = state.get(screen, part, "TEXT")
    if not isinstance(value, str):
        return None
    return html.unescape(html.unescape(value)).strip() or None


def _rows(state: UIState, screen: int, part: int) -> list[dict[str, Any]]:
    return list((state.get(screen, part, "LIST_ROW") or {}).get("rows", []))


def _cells(row: dict[str, Any]) -> list[str]:
    return [html.unescape(str(t)) for t in row.get("text", [])]


def _int(text: str | None) -> int | None:
    m = re.search(r"-?\d+", text or "")
    return int(m.group()) if m else None


def _float(text: str | None) -> float | None:
    try:
        return float((text or "").replace(",", "."))
    except ValueError:
        return None


def _option(state: UIState, screen: int, part: int) -> tuple[int, str] | None:
    """Return the selected index of a combo and its option text."""
    options = state.get(screen, part, "COMBO_STRS") or []
    idx = state.get(screen, part, "SINGLE_SELIDX")
    if idx is None or not 0 <= idx < len(options):
        return None
    return idx, str(options[idx])


def _radio(state: UIState, screen: int, on: int, off: int) -> bool | None:
    if state.get(screen, on, "CHECK_STATE") == 1:
        return True
    if state.get(screen, off, "CHECK_STATE") == 1:
        return False
    return None


def zones_from_screen(state: UIState) -> tuple[str | None, tuple[ZoneInfo, ...]]:
    """Read the zone settings: the shown position and the zone list."""
    s = MenuScreen.ZONES
    zones = []
    for row in _rows(state, s, ZN_LIST):
        cells = [*_cells(row), "", "", "", ""]
        zones.append(
            ZoneInfo(
                name=cells[0],
                level=_int(cells[1]),
                zones=_int(cells[2]),
                points=_int(cells[3]),
            )
        )
    return _text(state, s, ZN_POSITION), tuple(zones)


def points_from_screen(state: UIState) -> tuple[ManagementPoint, ...]:
    """Read the management point settings list."""
    points = []
    for row in _rows(state, MenuScreen.POINTS, PT_LIST):
        cells = [*_cells(row), "", "", "", ""]
        icons = [*row.get("a", []), -1, -1, -1]
        points.append(
            ManagementPoint(
                kind=cells[0],
                name=cells[1],
                icon=icons[2] if icons[2] != -1 else None,
                info=cells[3] or None,
            )
        )
    return tuple(points)


def maintenance_from_screen(state: UIState) -> tuple[MaintenancePoint, ...]:
    """Read the maintenance screen: the points in maintenance, then the others."""
    s = MenuScreen.MAINTENANCE
    points = []
    for part, default in ((MT_IN_MAINTENANCE, True), (MT_AVAILABLE, None)):
        for row in _rows(state, s, part):
            cells = [*_cells(row), "", "", ""]
            flag = on_off(cells[2]) if cells[2] else None
            points.append(
                MaintenancePoint(
                    kind=cells[0],
                    name=cells[1],
                    in_maintenance=default if flag is None else flag,
                )
            )
    return tuple(points)


def network_from_screen(state: UIState) -> NetworkSettings:
    """Read the network settings."""
    values = [_text(state, MenuScreen.NETWORK, p) for p in NW_FIELDS]
    return NetworkSettings(*values)


def email_from_screen(state: UIState) -> EmailSettings:
    """Read the e-mail settings: on or off, and the addresses."""
    s = MenuScreen.EMAIL
    addresses = []
    for row in _rows(state, s, EM_LIST):
        cells = [*_cells(row), "", ""]
        addresses.append((on_off(cells[0]) if cells[0] else None, cells[1]))
    return EmailSettings(
        enabled=_radio(state, s, EM_ENABLED, EM_DISABLED), addresses=tuple(addresses)
    )


def web_users_from_screen(state: UIState) -> tuple[WebUser, ...]:
    """Read the web access users: name and type."""
    users = []
    for row in _rows(state, MenuScreen.WEB_USERS, WU_LIST):
        cells = [*_cells(row), "", ""]
        users.append(
            WebUser(name=cells[0], kind=cells[1], admin=user_is_admin(cells[1]))
        )
    return tuple(users)


def hardware_from_screen(state: UIState) -> HardwareSettings:
    """Read the touch panel's backlight and buzzer settings."""
    s = MenuScreen.HARDWARE
    backlight = _option(state, s, HW_BACKLIGHT)
    volume = _option(state, s, HW_BUZZER_VOLUME)
    duration = _option(state, s, HW_BUZZER_DURATION)
    keys = _option(state, s, HW_KEY_VOLUME)
    minutes = continuous = None
    if duration is not None and duration[0] < len(BUZZER_DURATIONS):
        minutes = BUZZER_DURATIONS[duration[0]]
        continuous = minutes is None
    return HardwareSettings(
        backlight=_int(backlight[1]) if backlight else None,
        buzzer_volume=_int(volume[1]) if volume else None,
        buzzer_minutes=minutes,
        buzzer_continuous=continuous,
        key_click_volume=_int(keys[1]) if keys else None,
    )


def screensaver_from_screen(state: UIState) -> ScreensaverSettings:
    """Read the touch panel's screensaver settings."""
    s = MenuScreen.SCREENSAVER
    mode = _option(state, s, SS_MODE)
    modes = list(ScreensaverMode)
    return ScreensaverSettings(
        mode=modes[mode[0]] if mode and mode[0] < len(modes) else None,
        delay_minutes=_int(_text(state, s, SS_DELAY)),
        off_on_error=_radio(state, s, SS_OFF_ON_ERROR, SS_ON_ON_ERROR),
    )


def confirm_dialogs_from_screen(state: UIState) -> bool | None:
    """Read whether the iTM asks to confirm settings ("apply settings?")."""
    return _radio(state, MenuScreen.CONFIRM_DIALOGS, CD_ENABLED, CD_DISABLED)


def remote_management_from_screen(state: UIState) -> RemoteManagement:
    """Read the remote management status and address."""
    s = MenuScreen.REMOTE_MANAGEMENT
    ip = _text(state, s, RM_IP)
    return RemoteManagement(
        status=_text(state, s, RM_STATUS),
        ip_address=ip,
        connected=None if ip is None else ip != "0.0.0.0",  # noqa: S104 - shown, not bound
    )


def ppd_from_screen(state: UIState) -> PpdSettings:
    """Read the PPD settings and the use per unit."""
    s = MenuScreen.PPD
    units = []
    for row in _rows(state, s, PPD_LIST):
        cells = [*_cells(row), "", "", ""]
        units.append(
            PpdUnit(
                name=cells[0],
                consumption_kwh=_float(cells[1]),
                standby_kwh=_float(cells[2]),
            )
        )
    limit = state.get(s, PPD_TIME_LIMIT, "CHECK_STATE")
    return PpdSettings(
        time_limit=None if limit is None else limit == 1,
        start=_text(state, s, PPD_START),
        end=_text(state, s, PPD_END),
        calculation_day=_int(_text(state, s, PPD_DAY)),
        period=_text(state, s, PPD_PERIOD),
        units=tuple(units),
    )


def energy_sampling_from_screen(
    state: UIState,
) -> tuple[str | None, str | None, str | None]:
    """Read the equipment management screen: sampling from, to and target."""
    e = MenuScreen.ENERGY_EQUIPMENT
    return _text(state, e, EE_FROM), _text(state, e, EE_TO), _text(state, e, EE_TARGET)


def energy_export_from_screen(state: UIState) -> tuple[str, ...]:
    """Read the data export screen: the periods it offers."""
    periods = state.get(MenuScreen.ENERGY_EXPORT, EX_PERIODS, "COMBO_STRS") or []
    return tuple(str(p) for p in periods)


def _dst_rule(state: UIState, parts: tuple[int, int, int, int]) -> DstRule | None:
    s = MenuScreen.TIME
    picks = [_option(state, s, p) for p in parts]
    if any(p is None for p in picks):
        return None
    month, week, weekday, time = (p for p in picks if p is not None)
    hour = _int(time[1])
    if hour is None:
        return None
    return DstRule(month=month[0] + 1, week=week[0] + 1, weekday=weekday[0], hour=hour)


def dst_from_screen(state: UIState) -> DstSettings:
    """Read the time screen's summer time switch and rule."""
    return DstSettings(
        enabled=_radio(state, MenuScreen.TIME, TM_DST_ON, TM_DST_OFF),
        start=_dst_rule(state, TM_DST_START),
        end=_dst_rule(state, TM_DST_END),
    )


def language_from_screen(state: UIState) -> str | None:
    """Read the language screen (2501): the code of the checked language."""
    for code, radio in LANGUAGE_RADIOS.items():
        if state.get(MenuScreen.LANGUAGE, radio, "CHECK_STATE") == 1:
            return code
    return None


def row_index(
    state: UIState, screen: int, part: int, column: int, text: str
) -> int | None:
    """Return the list row whose column shows text (compared without outer spaces)."""
    for i, row in enumerate(_rows(state, screen, part)):
        cells = _cells(row)
        if len(cells) > column and cells[column].strip() == text.strip():
            return i
    return None


def zone_attributes_from_screen(state: UIState) -> ZoneAttributes:
    """Read a zone's settings screen (1603)."""
    z = MenuScreen.ZONE_SETTINGS
    return ZoneAttributes(
        name=_text(state, z, 2),
        info=_text(state, z, 5),
        start_interval=_int(_text(state, z, 8)),
        stop_interval=_int(_text(state, z, 12)),
        icon=state.get(z, ZA_ICON, "ICONID"),
    )
