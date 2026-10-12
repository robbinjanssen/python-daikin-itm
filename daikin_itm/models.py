"""Typed view of an indoor unit as shown on the iTM operation panel (screen 4)."""

from __future__ import annotations

import html
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .protocol import ScreenID

if TYPE_CHECKING:
    from .state import UIState

# Operation panel part IDs (ScMainOperation.initScreen in script.js).
P_NAME, P_STATE_ICON, P_FILTER_ICON, P_TEMP, P_MODE = 0, 1, 2, 4, 5
P_DETAILS_BUTTON, P_POWER, P_SETPOINT, P_FAN_ICON = 7, 9, 11, 14
P_SETTINGS_BUTTON, P_ERROR_CODE = 17, 19

# Second value of the state icon (ICONID_MODE) as seen on the device.
ICON_STATE_ON, ICON_STATE_OFF, ICON_STATE_SETBACK = 0, 2, 11

# Operation mode labels are localised; map the ones we know, keep the raw label too.
MODES = {
    "verwarmen": "heat",
    "heat": "heat",
    "heating": "heat",
    "koelen": "cool",
    "cool": "cool",
    "cooling": "cool",
    "ventilator": "fan",
    "ventilatie": "fan",
    "fan": "fan",
    "drogen": "dry",
    "dry": "dry",
    "auto": "auto",
    "automatisch": "auto",
}

# Fan speed icons (bars / "A" in the web UI's own images); the settings dialog
# cycles through them in this order.
FAN_ICONS = {3009: "low", 3011: "medium", 3013: "high", 3015: "auto"}
FAN_SPEEDS = tuple(FAN_ICONS.values())

# Settings dialog (screen 7, ScMainSetting in script.js).
D_OK, D_CANCEL = 5, 6
D_POWER_CHECK, D_POWER_START, D_POWER_STOP = 2, 3, 4
D_MODE_CHECK, D_MODE = 17, 18
D_FAN_CHECK, D_FAN_UP, D_FAN_DOWN, D_FAN_ICON = 28, 29, 30, 31

# Details screen (screen 9, ScMainDetail): read-only text fields, closed with part 15.
DT_NAME, DT_ID, DT_TYPE, DT_PORT, DT_ADDRESS = 2, 4, 6, 8, 10
DT_INFO, DT_CHARS, DT_CLOSE = 12, 14, 15

_TEMP = re.compile(r"-?\d+(?:[.,]\d+)?")


def normalize_mode(label: str | None) -> str | None:
    """Map a localised operation mode label to heat/cool/fan/dry/auto.

    Args:
    ----
        label: The label as the iTM shows it.

    Returns:
    -------
        The normalized mode, or None when the label is unknown.

    """
    return MODES.get(label.strip().lower()) if label else None


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
    chars = text(DT_CHARS) or ""
    zones = []
    if chars.startswith("Zone:"):
        for line in chars.splitlines()[1:]:
            if not line.strip():
                break
            zones.append(line.strip())
    return {
        "point_id": text(DT_ID),
        "address": f"{port}:{address}" if port and address else None,
        "unit_type": text(DT_TYPE),
        "description": text(DT_INFO),
        "zones": tuple(zones),
    }


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
    for label in labels:
        if (mode := normalize_mode(label)) and mode not in modes:
            modes[mode] = label
    return Capabilities(
        name=name,
        mode_labels=modes,
        fan_speed=state.visible(s, D_FAN_CHECK)
        and state.get(s, D_FAN_ICON, "ICONID") in FAN_ICONS,
        **(details or {}),
    )


@dataclass(frozen=True)
class Unit:
    """The state of an indoor unit as shown on the operation panel."""

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

    @property
    def setback(self) -> bool:
        """Whether the unit is in setback (the state icon shows SB)."""
        return self.state_icon == ICON_STATE_SETBACK


def _selected(options: Any, index: Any) -> Any:
    """Return options[index], or None when the index is missing or out of range."""
    return options[index] if index is not None and 0 <= index < len(options) else None


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

    temp = None
    if state.visible(s, P_TEMP):
        m = _TEMP.search(get(s, P_TEMP, "LABEL") or "")
        temp = float(m.group().replace(",", ".")) if m else None

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
        room_temperature=temp,
        mode=normalize_mode(mode_label),
        mode_label=mode_label,
        fan_icon=fan_icon,
        fan_speed=FAN_ICONS.get(fan_icon),
        error_code=error or None,
        filter_sign=state.visible(s, P_FILTER_ICON),
    )
