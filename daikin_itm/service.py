"""The iTM's service settings: the installer screens behind the service login.

The menu's service tab is hidden until a service login: tapping the menu list's four
invisible corner buttons (top-right, bottom-left, top-left, bottom-right) opens a
password dialog for the service password (not the web user's). The caller supplies
it; the library never stores it. Everything here is read only: these are installer
settings (licences, dealer options, bus management, history deletion), so the
library opens each screen, reads it and leaves it with its Cancel or Close.

The energy saving and power limit (demand) control screens at the end of this module
are read here too; their writes (``daikin_itm.demand``) take the service password as
an argument and need ``allow_service_change=True``.
"""

from __future__ import annotations

import html
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .energy_navigator import ShownValue, parse_shown
from .menu import MenuScreen

if TYPE_CHECKING:
    from .state import UIState

# Menu list (screen 5): the corner buttons, in the order they are tapped, the
# password dialog they open, and the service login indicators (a red border and a
# label, shown while logged in).
SERVICE_CORNERS = (87, 88, 89, 90)
SERVICE_INDICATORS = (95, 96, 97, 98, 99)

# Service tab buttons (screen 5).
MB_TIME_ZONE, MB_CHANGEOVER_OPTION, MB_CONTACT, MB_R32 = 69, 77, 83, 117
MB_CENTRAL_MONITOR, MB_ACTIVATION, MB_DEALER_OPTIONS = 73, 65, 67
MB_ENERGY_SAVE_MODE = 111

TZ_LIST = 2
CO_OPTION_LIST = 1
CT_LINES = (2, 5, 8)
R32_PORTS = (2, 6)
R32_BUZZER_ON, R32_BUZZER_OFF = 14, 15
CM_WARNING_ON, CM_WARNING_OFF, CM_DRY_ON, CM_DRY_OFF = 2, 3, 5, 6
AC_KEYS = 2
DO_ENABLED, DO_DISABLED = 2, 4
ES_POWER_LIMIT_LOCK, ES_ENERGY_SAVING = 3, 4


@dataclass(frozen=True)
class ChangeoverOption:
    """A unit's changeover option, as the installer set it."""

    address: str  # the DIII address, e.g. "1:1-00"
    name: str
    option: str  # as shown, e.g. "Met" (with)
    kind: str  # as shown, e.g. "Indoor unit"


@dataclass(frozen=True)
class ServiceSettings:  # pylint: disable=too-many-instance-attributes
    """What the service screens show; None where a screen did not open."""

    time_zone: str | None = None  # as listed, e.g. "GMT+01:00:..."
    time_zones: tuple[str, ...] = ()  # every zone the iTM offers
    changeover_options: tuple[ChangeoverOption, ...] | None = None
    contact: tuple[str, ...] | None = None  # the contact lines (empty when unset)
    r32_output_ports: tuple[str, ...] | None = None
    r32_buzzer: bool | None = None
    warning_as_abnormal: bool | None = None  # central monitoring
    dry_mode: bool | None = None  # central monitoring
    activation_keys: int | None = None  # licence keys entered (the MAC isn't read)
    dealer_options_enabled: tuple[str, ...] | None = None  # as shown
    dealer_options_disabled: tuple[str, ...] | None = None
    energy_save_mode: str | None = None  # "power_limit_lock" or "energy_saving"


def _text(state: UIState, screen: int, part: int) -> str:
    value = state.get(screen, part, "TEXT")
    return html.unescape(value).strip() if isinstance(value, str) else ""


def _rows(state: UIState, screen: int, part: int) -> list[dict[str, Any]]:
    return list((state.get(screen, part, "LIST_ROW") or {}).get("rows", []))


def _cells(row: dict[str, Any]) -> list[str]:
    return [html.unescape(str(t)).strip() for t in row.get("text", [])]


def _radio(state: UIState, screen: int, on: int, off: int) -> bool | None:
    if state.get(screen, on, "CHECK_STATE") == 1:
        return True
    if state.get(screen, off, "CHECK_STATE") == 1:
        return False
    return None


def logged_in(state: UIState) -> bool:
    """Tell whether the menu list shows the service login indicators."""
    return all(
        state.get(MenuScreen.MENULIST, part, "VISIBLED") == 1
        for part in SERVICE_INDICATORS
    )


def time_zone_from_screen(state: UIState) -> dict[str, Any]:
    """Read the time zone screen: the selected zone and every zone offered."""
    s = MenuScreen.TIME_ZONE
    panel = state.get(s, TZ_LIST, "LIST_ROW") or {}
    zones = tuple(_cells(r)[0] for r in panel.get("rows", []) if _cells(r))
    selected = panel.get("selected") or []
    zone = zones[selected[0]] if selected and selected[0] < len(zones) else None
    return {"time_zone": zone, "time_zones": zones}


def changeover_options_from_screen(state: UIState) -> dict[str, Any]:
    """Read the changeover option screen: each unit and its option.

    The iTM lists every DIII-NET address; the ones without a device are left out.
    """
    options = []
    for row in _rows(state, MenuScreen.CHANGEOVER_OPTION, CO_OPTION_LIST):
        cells = [*_cells(row), "", "", "", "", "", "", ""]
        if not cells[1] and not cells[3]:
            continue  # a free address
        options.append(
            ChangeoverOption(
                address=cells[0], name=cells[1], option=cells[3], kind=cells[6]
            )
        )
    return {"changeover_options": tuple(options)}


def contact_from_screen(state: UIState) -> dict[str, Any]:
    """Read the contact screen's three lines."""
    return {"contact": tuple(_text(state, MenuScreen.CONTACT, p) for p in CT_LINES)}


def r32_from_screen(state: UIState) -> dict[str, Any]:
    """Read the R32 alarm settings: output ports and the built-in buzzer."""
    s = MenuScreen.R32
    return {
        "r32_output_ports": tuple(_text(state, s, p) for p in R32_PORTS),
        "r32_buzzer": _radio(state, s, R32_BUZZER_ON, R32_BUZZER_OFF),
    }


def central_monitor_from_screen(state: UIState) -> dict[str, Any]:
    """Read the central monitoring settings."""
    s = MenuScreen.CENTRAL_MONITOR
    return {
        "warning_as_abnormal": _radio(state, s, CM_WARNING_ON, CM_WARNING_OFF),
        "dry_mode": _radio(state, s, CM_DRY_ON, CM_DRY_OFF),
    }


def activation_from_screen(state: UIState) -> dict[str, Any]:
    """Count the licence keys entered (the device's MAC address is not read)."""
    return {"activation_keys": len(_rows(state, MenuScreen.ACTIVATION, AC_KEYS))}


def dealer_options_from_screen(state: UIState) -> dict[str, Any]:
    """Read the dealer options: which are enabled and which are not."""
    s = MenuScreen.DEALER_OPTIONS
    return {
        "dealer_options_enabled": tuple(
            _cells(r)[0] for r in _rows(state, s, DO_ENABLED)
        ),
        "dealer_options_disabled": tuple(
            _cells(r)[0] for r in _rows(state, s, DO_DISABLED)
        ),
    }


def energy_save_mode_from_screen(state: UIState) -> dict[str, Any]:
    """Read the energy save mode: power limit lock or energy saving."""
    on = _radio(
        state, MenuScreen.ENERGY_SAVE_MODE, ES_ENERGY_SAVING, ES_POWER_LIMIT_LOCK
    )
    mode = None if on is None else ("energy_saving" if on else "power_limit_lock")
    return {"energy_save_mode": mode}


# -- energy saving and demand (power limit) control: the service screens -------------
# 1508's layout was seen on a real iTM (read only; every text empty while energy
# saving is not active). The rest is tested against the simulated iTM only: the real
# iTM's power limit control needs a dealer option and a pulse input it did not have.
# Screen and part IDs come from the web UI's script; which of 1512 / 1513 the service
# tab's "power limit control setup" button opens (by the energy save mode) and that
# 1514 picks a port for them are assumptions.

# Energy saving status (1508), power limit control setup (1512 or 1513).
MB_ENERGY_SAVING_STATUS, MB_DEMAND_SETUP = 79, 113
# Energy saving status (1508): "disable energy saving" is a write.
ENERGY_SAVING_STATUS = 1508
EST_TEXTS = (3, 5, 7, 9, 11, 13, 15)
EST_DISABLE, EST_CANCEL = 16, 17
# Demand pulse setup (1512).
PULSE_SETUP = 1512
PS_PORTS, PS_PORT_BUTTONS, PS_PORT_DELETES = (3, 12), (4, 13), (29, 30)
PS_COEFFICIENT, PS_COEFFICIENT_BUTTON = 6, 7
PS_SYNC_EXTERNAL, PS_SYNC_PERIOD = 9, 10
PS_RETURN, PS_RETURN_BUTTON, PS_CUTOFF, PS_CUTOFF_BUTTON = 16, 18, 20, 22
PS_OK, PS_CANCEL = 23, 24
# Demand control interlock setup (1513): three input ports.
INTERLOCK_SETUP = 1513
IL_PORTS, IL_PORT_BUTTONS, IL_PORT_DELETES = (2, 5, 8), (3, 6, 9), (13, 14, 15)
IL_OK, IL_CANCEL = 10, 11
# Demand input port setup (1514).
PORT_PICKER = 1514
PP_LIST, PP_OK, PP_CANCEL = 2, 3, 4


@dataclass(frozen=True)
class EnergySavingStatus:
    """The energy saving status (1508), texts as shown; None where empty."""

    start_time: str | None
    remaining_time: str | None
    target: ShownValue | None  # target power
    current: ShownValue | None
    estimated: ShownValue | None
    excess: ShownValue | None
    control_status: str | None


@dataclass(frozen=True)
class DemandPulseSetup:
    """The demand pulse setup (1512): the power meter's pulse input and limits."""

    input_ports: tuple[str | None, str | None]  # input port 1 and 2, as shown
    pulse_coefficient: ShownValue | None
    sync_external: bool | None  # synchronise on an external signal (else period)
    return_value: ShownValue | None
    cutoff_value: ShownValue | None


@dataclass(frozen=True)
class DemandServiceSettings:
    """The power limit control setup the service tab opens; None where not open."""

    pulse: DemandPulseSetup | None = None
    interlock_ports: tuple[str | None, ...] | None = None  # 1513's three ports
    ports: tuple[str, ...] = ()  # the ports the port picker (1514) offers


def _shown_text(state: UIState, screen: int, part: int) -> str | None:
    value = state.get(screen, part, "TEXT")
    if not isinstance(value, str):
        return None
    return html.unescape(value).strip() or None


def _shown_value(state: UIState, screen: int, part: int) -> ShownValue | None:
    value = state.get(screen, part, "TEXT")
    return parse_shown(value) if isinstance(value, str) and value.strip() else None


def energy_saving_status_from_screen(state: UIState) -> EnergySavingStatus:
    """Read the energy saving status screen (1508)."""
    s = ENERGY_SAVING_STATUS
    start, remaining, target, current, estimated, excess, control = EST_TEXTS
    return EnergySavingStatus(
        start_time=_shown_text(state, s, start),
        remaining_time=_shown_text(state, s, remaining),
        target=_shown_value(state, s, target),
        current=_shown_value(state, s, current),
        estimated=_shown_value(state, s, estimated),
        excess=_shown_value(state, s, excess),
        control_status=_shown_text(state, s, control),
    )


def pulse_setup_from_screen(state: UIState) -> DemandPulseSetup:
    """Read the demand pulse setup screen (1512)."""
    s = PULSE_SETUP
    port1, port2 = (_shown_text(state, s, p) for p in PS_PORTS)
    return DemandPulseSetup(
        input_ports=(port1, port2),
        pulse_coefficient=_shown_value(state, s, PS_COEFFICIENT),
        sync_external=_radio(state, s, PS_SYNC_EXTERNAL, PS_SYNC_PERIOD),
        return_value=_shown_value(state, s, PS_RETURN),
        cutoff_value=_shown_value(state, s, PS_CUTOFF),
    )


def interlock_ports_from_screen(state: UIState) -> tuple[str | None, ...]:
    """Read the demand control interlock setup's input ports (1513)."""
    return tuple(_shown_text(state, INTERLOCK_SETUP, p) for p in IL_PORTS)


def ports_from_screen(state: UIState) -> tuple[str, ...]:
    """Read the ports the port picker (1514) offers: each row's last cell."""
    return tuple(
        cells[-1] for cells in map(_cells, _rows(state, PORT_PICKER, PP_LIST)) if cells
    )
