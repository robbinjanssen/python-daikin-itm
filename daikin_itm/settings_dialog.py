"""The unit settings dialog (screen 7) and the Details screen: the session's work."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

from ._bases import SessionCore
from ._util import cleanup_after as _cleanup_after
from ._util import run_to_completion as _run_to_completion
from .exceptions import ItmCommandError, NotOffered, SetpointOutOfRange
from .models import (
    AIRFLOW_DIRECTIONS,
    AIRFLOW_ICONS,
    D_AIRFLOW_CHECK,
    D_AIRFLOW_DOWN,
    D_AIRFLOW_ICON,
    D_AIRFLOW_UP,
    D_CANCEL,
    D_COOL_LIMIT,
    D_COOL_MAX_BUTTON,
    D_COOL_MIN_BUTTON,
    D_DUST_FORCED_RESET,
    D_DUST_RESET,
    D_FAN_CHECK,
    D_FAN_ICON,
    D_FAN_UP,
    D_FILTER_RESET,
    D_HEAT_LIMIT,
    D_HEAT_MAX_BUTTON,
    D_HEAT_MIN_BUTTON,
    D_LIMIT_CHECK,
    D_LOCK_MODE,
    D_LOCK_MODE_CHECK,
    D_LOCK_SETPOINT,
    D_LOCK_SETPOINT_CHECK,
    D_LOCK_START_STOP,
    D_LOCK_START_STOP_CHECK,
    D_MODE,
    D_MODE_CHECK,
    D_OK,
    D_OVERTIME,
    D_OVERTIME_CHECK,
    D_POWER_CHECK,
    D_POWER_START,
    D_POWER_STOP,
    D_SETPOINT_BUTTON,
    D_SETPOINT_CHECK,
    D_VENT_AMOUNT,
    D_VENT_AMOUNT_CHECK,
    D_VENT_MODE,
    D_VENT_MODE_CHECK,
    DIALOG_SETTING_KEYS,
    DIALOG_SETTINGS,
    DT_CLOSE,
    FAN_ICONS,
    P_DETAILS_BUTTON,
    P_SETTINGS_BUTTON,
    RemoteLock,
    analog_details_from_screen,
    capabilities_from_dialog,
    details_from_screen,
    dialog_setting_choice,
    settings_from_dialog,
    status_from_details,
)
from .protocol import DataType, EventType, ScreenID

if TYPE_CHECKING:
    from collections.abc import Callable

    from .exceptions import ItmError
    from .models import Capabilities, DialogSetting, UnitSettings
    from .state import UIState


# Never skipped as already done: the dialog offers a reset only while it is due.
_RESETS = frozenset({"reset_filter_sign", "reset_dust_alarm", "force_dust_alarm_reset"})


class SettingsDialogMixin(SessionCore):
    """_Session's settings dialog and Details screen."""

    async def open_settings(self) -> None:
        """Open the selected unit's settings dialog."""
        reply = await self.send(ScreenID.OPERATION, P_SETTINGS_BUTTON)
        if not any(
            s.screen == ScreenID.SETTINGS and s.update == "OPEN" for s in reply.screens
        ):
            msg = f"settings dialog did not open: {reply.dialogs()}"
            raise ItmCommandError(msg)

    async def dialog_click(self, part: int, number: int | None = None) -> None:
        """Click a settings dialog control, optionally picking an option."""
        if number is None:
            reply = await self.send(ScreenID.SETTINGS, part)
        else:
            reply = await self.send(
                ScreenID.SETTINGS, part, EventType.CLICK, DataType.INT, number
            )
        if reply.dialogs():
            msg = f"iTM opened a dialog: {reply.dialogs()}"
            raise ItmCommandError(msg)

    async def close_settings(self, *, ok: bool) -> None:
        """Close the settings dialog with OK (and confirm) or Cancel."""
        if ok:
            self.require_writable()
            if self.client.dry_run:
                self.client.planned.append(
                    f"save settings: screen {ScreenID.SETTINGS} part {D_OK} event 1"
                )
                ok = False
        if ok:
            self.change_sent = True
        reply = await self.send(ScreenID.SETTINGS, D_OK if ok else D_CANCEL)
        # OK asks "Instelling uitvoeren?" (apply settings?). The browser answers "Ja"
        # with a CLICK on the dialog's ID (onClickDialog1) and "Nee" with CLICK2
        # (onClickDialog2).
        opened = [s for s in reply.screens if s.update == "DLG_OPEN"]
        confirms = [(s.screen, s.data[0]) for s in opened if s.data[1] == "CONFIRM"]
        others = [s.data for s in opened if s.data[1] != "CONFIRM"]
        if others:
            msg = f"iTM refused the settings: {others}"
            raise ItmCommandError(msg)
        if ok and confirms:
            screen, dialog_id = confirms[0]
            reply = await self.send(screen, dialog_id, EventType.CLICK)
            if reply.dialogs():
                msg = f"iTM refused the settings: {reply.dialogs()}"
                raise ItmCommandError(msg)
        if not self.settings_open or any(
            s.screen == ScreenID.SETTINGS and s.update == "CLOSE" for s in reply.screens
        ):
            return
        msg = "settings dialog did not close"
        raise ItmCommandError(msg)

    async def set_dialog_power(self, *, power: bool) -> None:
        """Tick power in the settings dialog and pick start or stop."""
        await self.dialog_click(D_POWER_CHECK, 1)
        await self.dialog_click(D_POWER_START if power else D_POWER_STOP)

    async def set_dialog_mode(self, caps: Capabilities, mode: str) -> None:
        """Tick the operation mode in the settings dialog and pick one."""
        if mode not in caps.mode_labels:
            msg = f"{caps.name}: mode {mode!r} not offered; available: {caps.modes}"
            raise NotOffered(msg, name=caps.name, value=mode, options=caps.modes)
        labels = self.state.get(ScreenID.SETTINGS, D_MODE, "COMBO_STRS")
        await self.dialog_click(D_MODE_CHECK, 1)
        await self.dialog_click(D_MODE, labels.index(caps.mode_labels[mode]))

    async def set_dialog_fan_speed(self, caps: Capabilities, fan_speed: str) -> None:
        """Tick the fan speed in the settings dialog and cycle to the wanted one."""
        if not caps.fan_speed:
            msg = f"{caps.name}: fan speed cannot be changed on this unit"
            raise NotOffered(msg, name=caps.name, value=fan_speed)
        await self.dialog_click(D_FAN_CHECK, 1)
        # The up button cycles low -> medium -> high -> auto -> low; the iTM reports
        # the new icon.
        for _ in range(len(FAN_ICONS) + 1):
            icon = self.state.get(ScreenID.SETTINGS, D_FAN_ICON, "ICONID")
            if FAN_ICONS.get(icon) == fan_speed:
                return
            await self.dialog_click(D_FAN_UP)
        msg = f"{caps.name}: could not select fan speed {fan_speed!r}"
        raise ItmCommandError(msg)

    async def answer_number(  # pylint: disable=too-many-locals
        self,
        button: int,
        value: float,
        what: str,
        screen: int = ScreenID.SETTINGS,
        *,
        out_of_range: Callable[[float, float], ItmError] | None = None,
    ) -> None:
        """Press a "change" button and enter a number in the dialog it opens.

        The dialog says which kind of number it takes (type) and its range; the
        browser sends the value as an int, or as [integer part, thousandths]. A
        value outside the range cancels the dialog and raises out_of_range(low,
        high), or an ItmCommandError.
        """
        reply = await self.send(screen, button)
        dialogs = [
            u for u in reply.screens if u.update == "DLG_OPEN" and u.data[1] == "NUMBER"
        ]
        if not dialogs:
            msg = f"{what}: no number input opened: {reply.dialogs()}"
            raise ItmCommandError(msg)
        d = dialogs[0]
        dialog_id, kind = d.data[0], d.data[2]
        low = d.data[6] + d.data[7] / 1000
        high = d.data[8] + d.data[9] / 1000
        if not low <= value <= high:
            await self.send(d.screen, dialog_id, EventType.CLICK2)
            if out_of_range is not None:
                raise out_of_range(low, high)
            msg = f"{what}: {value} outside the iTM's range {low}-{high}"
            raise ItmCommandError(msg)
        whole = int(value)
        thousandths = round((value - whole) * 1000)
        if kind == 0:
            reply = await self.send(
                d.screen, dialog_id, EventType.CLICK, DataType.INT, whole
            )
        elif kind in {1, 2}:
            data_type = DataType.FLOAT if kind == 1 else DataType.SETTEMP_C
            reply = await self.send(
                d.screen,
                dialog_id,
                EventType.CLICK,
                data_type,
                2,
                array=(whole, thousandths),
            )
        elif kind == 3:
            reply = await self.send(
                d.screen, dialog_id, EventType.CLICK, DataType.SETTEMP_F, round(value)
            )
        else:
            await self.send(d.screen, dialog_id, EventType.CLICK2)
            msg = f"{what}: number input of kind {kind} is not supported"
            raise ItmCommandError(msg)
        await self.wait_for_progress(reply)

    async def set_dialog_setpoint(self, unit: str, value: int) -> None:
        """Tick the setpoint in the settings dialog and enter it in its number input.

        The input's range (it follows the unit's setpoint limit) counts: a value
        outside it cancels the input and raises SetpointOutOfRange.
        """
        s = ScreenID.SETTINGS
        if not self.state.visible(s, D_SETPOINT_CHECK):
            msg = f"{unit}: the settings dialog offers no setpoint"
            raise NotOffered(msg, name=unit, value=value)
        await self.dialog_click(D_SETPOINT_CHECK, 1)
        if self.state.get(s, D_SETPOINT_BUTTON, "ENABLED") == 0:
            msg = f"{unit}: setpoint: the iTM keeps its control disabled"
            raise ItmCommandError(msg)

        def out_of_range(low: float, high: float) -> ItmError:
            allowed = tuple(range(math.ceil(low), math.floor(high) + 1))
            return SetpointOutOfRange(unit, value, allowed)

        await self.answer_number(
            D_SETPOINT_BUTTON, value, f"{unit}: setpoint", out_of_range=out_of_range
        )

    async def answer_text(self, screen: int, button: int, text: str, what: str) -> None:
        """Press a "change" button and enter text in the input dialog it opens.

        The dialog (CHARINPUT) says how long the text may be; a text outside that
        is refused before it is sent, and the dialog is cancelled.
        """
        reply = await self.send(screen, button)
        dialogs = [
            u
            for u in reply.screens
            if u.update == "DLG_OPEN" and u.data[1] == "CHARINPUT"
        ]
        if not dialogs:
            msg = f"{what}: no text input opened: {reply.dialogs()}"
            raise ItmCommandError(msg)
        d = dialogs[0]
        dialog_id, low, high = d.data[0], d.data[4], d.data[5]
        if not low <= len(text) <= high:
            await self.send(d.screen, dialog_id, EventType.CLICK2)
            msg = f"{what}: {text!r} must be {low}-{high} characters"
            raise ItmCommandError(msg)
        reply = await self.send(
            d.screen, dialog_id, EventType.CLICK, DataType.STRING, string=text
        )
        if reply.dialogs():
            msg = f"{what}: the iTM refused {text!r}: {reply.dialogs()}"
            raise ItmCommandError(msg)

    async def change_unit_settings(
        self,
        changes: dict[str, Any],
        name: str = "",
        skipped: set[str] | None = None,
    ) -> UnitSettings | None:
        """Change the selected unit's locks, overtime and limits, then OK.

        Only the settings the dialog doesn't show yet are changed (a reset always
        is). When it shows them all, the dialog is cancelled: nothing is written.
        The dialog is cancelled too when anything goes wrong.

        Args:
        ----
            changes: The settings to change, as set_unit_settings takes them.
            name: The unit's name, for the settings returned.
            skipped: Gets the settings left as they were, as the dialog showed
                them already.

        Returns:
        -------
            The settings the dialog showed (without the Details status) when it
            showed every change already, None when the changes were saved.

        """
        await self.open_settings()
        try:
            shown = settings_from_dialog(name, self.state)
            asked = {k: v for k, v in changes.items() if k not in _RESETS}
            todo = {
                k: v
                for k, v in changes.items()
                if k in _RESETS or k in settings_mismatch(shown, asked)
            }
            if skipped is not None:
                skipped.update(changes.keys() - todo.keys())
            if not todo:
                await self.close_settings(ok=False)
                return shown
            await self._change_in_dialog(todo)
        except BaseException as err:
            await self._cancel_after(err)
            raise
        return None

    async def _change_in_dialog(self, changes: dict[str, Any]) -> None:
        """Make change_unit_settings' changes in the open dialog, then OK."""
        # Refuse an optional setting the unit doesn't offer before any click.
        picks = {
            d.key: dialog_setting_choice(self.state, d, changes[d.key])
            for d in DIALOG_SETTINGS
            if changes.get(d.key) is not None
        }
        for key, check, radios in (
            ("start_stop_lock", D_LOCK_START_STOP_CHECK, D_LOCK_START_STOP),
            ("mode_lock", D_LOCK_MODE_CHECK, D_LOCK_MODE),
            ("setpoint_lock", D_LOCK_SETPOINT_CHECK, D_LOCK_SETPOINT),
        ):
            if (lock := changes.get(key)) is None:
                continue
            radio = _lock_radio(key, lock, radios)
            await self.dialog_click(check, 1)
            await self.dialog_click(radio)
        if (overtime := changes.get("overtime")) is not None:
            await self.dialog_click(D_OVERTIME_CHECK, 1)
            await self.dialog_click(D_OVERTIME, 0 if overtime else 1)
        limits = [
            ("cool_limit", D_COOL_LIMIT, D_COOL_MIN_BUTTON, D_COOL_MAX_BUTTON),
            ("heat_limit", D_HEAT_LIMIT, D_HEAT_MIN_BUTTON, D_HEAT_MAX_BUTTON),
        ]
        if any(changes.get(key) is not None for key, *_ in limits):
            await self.dialog_click(D_LIMIT_CHECK, 1)
        for key, combo, min_button, max_button in limits:
            if (limit := changes.get(key)) is None:
                continue
            await self.dialog_click(combo, 0 if limit.enabled else 1)
            if limit.enabled and limit.maximum is not None:
                await self.answer_number(max_button, limit.maximum, f"{key} max")
            if limit.enabled and limit.minimum is not None:
                await self.answer_number(min_button, limit.minimum, f"{key} min")
        await self._change_unit_extras(changes)
        for d in DIALOG_SETTINGS:
            if d.key in picks:
                await self.set_dialog_setting(d, picks[d.key])
        await self.close_settings(ok=True)

    async def _cancel_after(self, err: BaseException) -> None:
        """Cancel the dialog, if still open, while err goes on (see cleanup_after)."""
        if self.settings_open and self.logged_in:
            await _cleanup_after(
                err, lambda: _run_to_completion(self.close_settings(ok=False))
            )

    async def _change_unit_extras(self, changes: dict[str, Any]) -> None:
        """Airflow direction, ventilation mode/amount and the resets, if asked."""
        if (direction := changes.get("airflow_direction")) is not None:
            await self.set_dialog_airflow(direction)
        for key, part in (
            ("reset_filter_sign", D_FILTER_RESET),
            ("reset_dust_alarm", D_DUST_RESET),
            ("force_dust_alarm_reset", D_DUST_FORCED_RESET),
        ):
            if changes.get(key):
                await self._tick_reset(part, key)
        for key, check, combo in (
            ("ventilation_mode", D_VENT_MODE_CHECK, D_VENT_MODE),
            ("ventilation_amount", D_VENT_AMOUNT_CHECK, D_VENT_AMOUNT),
        ):
            if (value := changes.get(key)) is not None:
                await self.set_dialog_option(check, combo, value, key)

    async def _tick_reset(self, part: int, what: str) -> None:
        """Tick a reset the dialog offers (only while there is something to reset)."""
        if not self.state.visible(ScreenID.SETTINGS, part):
            msg = f"{what.replace('_', ' ')}: nothing to reset on this unit"
            raise ItmCommandError(msg)
        await self.dialog_click(part, 1)

    async def set_dialog_option(
        self, check: int, combo: int, value: str, what: str
    ) -> None:
        """Tick an optional dialog setting and pick an option by its label."""
        s = ScreenID.SETTINGS
        options = [str(o).strip() for o in self.state.get(s, combo, "COMBO_STRS") or []]
        disabled = self.state.get(s, check, "ENABLED") == 0
        if not self.state.visible(s, check) or disabled or not options:
            msg = f"{what.replace('_', ' ')}: not offered by this unit"
            raise NotOffered(msg, value=value)
        wanted = [o.casefold() for o in options]
        if value.strip().casefold() not in wanted:
            msg = f"{what.replace('_', ' ')}: {value!r} not offered: {options}"
            raise NotOffered(msg, value=value, options=options)
        await self.dialog_click(check, 1)
        await self.dialog_click(combo, wanted.index(value.strip().casefold()))

    async def set_dialog_setting(self, setting: DialogSetting, pick: Any) -> None:
        """Tick an optional setting (models.DIALOG_SETTINGS) and set it.

        pick comes from dialog_setting_choice. The setting's controls are enabled by
        ticking its checkbox; one still disabled is refused, never clicked.
        """
        what = setting.key.replace("_", " ")
        await self.dialog_click(setting.check, 1)
        control = {"radios": pick, "number": setting.parts[-1]}.get(
            setting.kind, setting.parts[0]
        )
        if self.state.get(ScreenID.SETTINGS, control, "ENABLED") == 0:
            msg = f"{what}: the iTM keeps its control disabled"
            raise ItmCommandError(msg)
        if setting.kind == "radios":
            await self.dialog_click(control)
        elif setting.kind == "number":
            await self.answer_number(control, pick, what)
        else:
            await self.dialog_click(control, pick)

    async def set_dialog_airflow(self, direction: str) -> None:
        """Tick airflow direction in the settings dialog and step to the wanted one.

        The down button steps position 0 -> 1 -> ... -> 4 -> swing -> 0, up the other
        way; take the shorter way and check the icon after each step.
        """
        s = ScreenID.SETTINGS
        if not self.state.visible(s, D_AIRFLOW_CHECK):
            msg = "this unit has no adjustable airflow direction"
            raise NotOffered(msg, value=direction)
        await self.dialog_click(D_AIRFLOW_CHECK, 1)
        current = AIRFLOW_ICONS.get(self.state.get(s, D_AIRFLOW_ICON, "ICONID"))
        if current is None:
            msg = "the iTM shows an unknown airflow direction"
            raise ItmCommandError(msg)
        n = len(AIRFLOW_DIRECTIONS)
        down = (
            AIRFLOW_DIRECTIONS.index(direction) - AIRFLOW_DIRECTIONS.index(current)
        ) % n
        button, steps = (
            (D_AIRFLOW_DOWN, down) if down <= n - down else (D_AIRFLOW_UP, n - down)
        )
        for _ in range(steps):
            if self.state.get(s, button, "ENABLED") == 0:
                msg = "the airflow direction buttons are disabled"
                raise ItmCommandError(msg)
            await self.dialog_click(button)
        shown = AIRFLOW_ICONS.get(self.state.get(s, D_AIRFLOW_ICON, "ICONID"))
        if shown != direction:
            msg = f"could not select airflow direction {direction!r} (shows {shown!r})"
            raise ItmCommandError(msg)

    async def change_settings(
        self,
        unit: str,
        *,
        power: bool | None,
        mode: str | None,
        fan_speed: str | None,
        setpoint: int | None = None,
    ) -> None:
        """Change the selected unit through its settings dialog.

        The dialog is cancelled when anything goes wrong.
        """
        await self.open_settings()
        try:
            caps = capabilities_from_dialog(unit, self.state)
            if power is not None:
                await self.set_dialog_power(power=power)
            if mode is not None:
                await self.set_dialog_mode(caps, mode)
            if fan_speed is not None:
                await self.set_dialog_fan_speed(caps, fan_speed)
            if setpoint is not None:
                await self.set_dialog_setpoint(unit, setpoint)
            await self.close_settings(ok=True)
        except BaseException as err:
            await self._cancel_after(err)
            raise

    async def _with_details(
        self, read: Callable[[UIState], dict[str, Any]]
    ) -> dict[str, Any]:
        """Open the selected point's Details screen, read it with read, close it."""
        reply = await self.send(ScreenID.OPERATION, P_DETAILS_BUTTON)
        if not any(
            s.screen == ScreenID.DETAILS and s.update == "OPEN" for s in reply.screens
        ):
            msg = f"details screen did not open: {reply.dialogs()}"
            raise ItmCommandError(msg)
        result = read(self.state)
        await self.send(ScreenID.DETAILS, DT_CLOSE)
        return result

    async def read_details(self) -> dict[str, Any]:
        """Read the selected unit's identity from its Details screen."""
        return await self._with_details(details_from_screen)

    async def read_detail_status(self) -> dict[str, Any]:
        """Read the selected unit's status lines from its Details screen."""
        return await self._with_details(status_from_details)

    async def read_analog_details(self) -> dict[str, Any]:
        """Read the selected analog point's attributes from its Details screen."""
        return await self._with_details(analog_details_from_screen)


def settings_mismatch(result: UnitSettings, changes: dict[str, Any]) -> dict[str, Any]:
    """Return the asked changes the settings don't show, with what they show.

    Args:
    ----
        result: The settings as read (back).
        changes: The settings asked for, as set_unit_settings takes them.

    Returns:
    -------
        The asked settings that differ, each with the value shown.

    """
    shown = {
        "start_stop_lock": result.remote_locks.start_stop,
        "mode_lock": result.remote_locks.mode,
        "setpoint_lock": result.remote_locks.setpoint,
        "overtime": result.overtime,
        "airflow_direction": result.airflow_direction,
        "ventilation_mode": result.ventilation_mode,
        "ventilation_amount": result.ventilation_amount,
        # A reset shows as the dialog no longer offering it.
        "reset_filter_sign": not result.filter_sign_resettable,
        "reset_dust_alarm": not result.dust_alarm_resettable,
        "force_dust_alarm_reset": not result.dust_alarm_resettable,
    }
    wrong: dict[str, Any] = {}
    for key, value in changes.items():
        have = shown.get(key)
        if key in {"ventilation_mode", "ventilation_amount"}:
            if str(have or "").strip().casefold() != str(value).strip().casefold():
                wrong[key] = have
        elif key in shown and have != value:
            wrong[key] = have
        elif key in {"cool_limit", "heat_limit"}:
            got = getattr(result, key)
            if got.enabled != value.enabled or (
                value.enabled
                and any(
                    want is not None and have != want
                    for want, have in (
                        (value.minimum, got.minimum),
                        (value.maximum, got.maximum),
                    )
                )
            ):
                wrong[key] = got
        elif key in DIALOG_SETTING_KEYS:
            have = getattr(result, key)
            same = (
                str(have or "").strip().casefold() == str(value).strip().casefold()
                if isinstance(value, str)
                else have == value
            )
            if not same:
                wrong[key] = have
    return wrong


def _lock_radio(key: str, lock: RemoteLock, radios: tuple[int, ...]) -> int:
    """Return the radio part for a lock value, refusing one the dialog lacks."""
    order = (
        (RemoteLock.ALLOWED, RemoteLock.STOP_ONLY, RemoteLock.FORBIDDEN)
        if len(radios) == 3
        else (RemoteLock.ALLOWED, RemoteLock.FORBIDDEN)
    )
    if lock not in order:
        msg = f"{key}: {lock} is not offered"
        raise NotOffered(msg, value=lock, options=order)
    return radios[order.index(lock)]
