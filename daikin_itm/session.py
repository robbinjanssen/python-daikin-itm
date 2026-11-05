"""One logged-in session with the iTM: the screen work behind ItmClient."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
from dataclasses import replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any, TypeVar

import aiohttp

from . import zones_edit
from .changeover import ChangeoverSessionMixin
from .exceptions import (
    ItmCommandError,
    ItmError,
    LoginRejected,
    ProtocolError,
    ReadOnlyError,
    SessionEnded,
    VersionMismatch,
    ZoneNotFound,
)
from .lang import is_all_zone
from .menu import (
    CLOSE_PART,
    CO_LIST,
    CO_MEMBERS_BUTTON,
    CO_SETTINGS_BUTTON,
    DATE_FORMATS,
    EV_EDIT,
    EV_LIST,
    H_CLASS_MANAGEMENT,
    H_DATE,
    H_DAY_NEXT,
    H_DAY_PREVIOUS,
    H_PAGE_OLDER,
    H_REFRESH,
    MB_CHANGEOVER,
    MB_EMERGENCY_STOP,
    MB_HISTORY,
    MB_OVERTIME,
    MB_REGION,
    MB_SCHEDULE,
    MB_SETBACK,
    MB_TIME,
    MB_VERSION,
    MENU_CLOSE,
    MENU_OPEN,
    OPTIONAL_FUNCTIONS,
    OT_DURATION,
    OT_OK,
    R_12H,
    R_24H,
    R_CELSIUS,
    R_DATE_FORMAT,
    R_DECIMAL_COMMA,
    R_DECIMAL_POINT,
    R_FAHRENHEIT,
    SB_ADVANCED,
    SB_HIGH_BACK,
    SB_HIGH_UP,
    SB_LOW_BACK,
    SB_LOW_UP,
    SB_OK,
    SBA_COOL,
    SBA_HEAT,
    SBA_OK,
    SCH_ADD_BUTTON,
    SCH_DAY_BUTTON,
    SCH_DELETE_BUTTON,
    SCH_EDIT_BUTTON,
    SCH_PANEL,
    SD_NEXT,
    SE_DAY_RADIOS,
    SE_DISABLED,
    SE_EDIT_DAY,
    SE_EDIT_SPECIAL,
    SE_ENABLED,
    SE_OK,
    SE_RENAME,
    SE_SPECIAL_RADIOS,
    SE_TAB,
    SE_TAB_CALENDAR,
    SE_TAB_SPECIAL,
    SE_TAB_WEEKLY,
    ChangeoverGroup,
    EmergencyStopProgram,
    History,
    ItmIndicators,
    ItmInfo,
    MenuScreen,
    OptionalFunction,
    ScheduleDay,
    ScheduleEvent,
    ScheduleProgram,
    ScheduleSummary,
    SetbackSettings,
    changeover_candidates_from_screen,
    changeover_groups_from_screen,
    changeover_members_from_screen,
    changeover_settings_from_screen,
    clock_from_screen,
    day_events_from_screen,
    emergency_stop_from_screen,
    event_from_screen,
    history_from_screen,
    indicators_from_screen,
    overtime_from_screen,
    parse_shown_date,
    program_from_editor,
    region_from_screen,
    schedule_day_from_screen,
    schedules_from_screen,
    setback_from_screen,
    version_from_screen,
)
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
    D_VENT_AMOUNT,
    D_VENT_AMOUNT_CHECK,
    D_VENT_MODE,
    D_VENT_MODE_CHECK,
    DIALOG_SETTINGS,
    DT_CLOSE,
    E_CLOSE,
    FAN_ICONS,
    L_BTN_ICON,
    L_LIST,
    MM_ERROR_BUTTON,
    P_DETAILS_BUTTON,
    P_NAME,
    P_SETTINGS_BUTTON,
    Capabilities,
    DialogSetting,
    ErrorInformation,
    PointKind,
    RemoteLock,
    Unit,
    analog_details_from_screen,
    analog_from_panel,
    capabilities_from_dialog,
    details_from_screen,
    dialog_setting_choice,
    error_information_from_screen,
    point_kind,
    status_from_details,
    unit_from_panel,
    units_from_list,
)
from .protocol import (
    NO_TOKEN,
    POLL_MAINMENU,
    POLL_OPERATION,
    POLL_STDICON,
    POLL_STDLAYOUT,
    POLL_STDLIST,
    WEB_VERSION,
    DataType,
    EventType,
    ScreenID,
    encode_event,
    is_token,
    parse_reply,
)
from .schedule_edit import special_day_names
from .service import (
    MB_ACTIVATION,
    MB_CENTRAL_MONITOR,
    MB_CHANGEOVER_OPTION,
    MB_CONTACT,
    MB_DEALER_OPTIONS,
    MB_ENERGY_SAVE_MODE,
    MB_R32,
    MB_TIME_ZONE,
    SERVICE_CORNERS,
    ServiceSettings,
    activation_from_screen,
    central_monitor_from_screen,
    changeover_options_from_screen,
    contact_from_screen,
    dealer_options_from_screen,
    energy_save_mode_from_screen,
    r32_from_screen,
    time_zone_from_screen,
)
from .service import logged_in as service_logged_in
from .state import UIState
from .system import (
    BUZZER_DURATIONS,
    HW_BACKLIGHT,
    HW_BUZZER_DURATION,
    HW_BUZZER_VOLUME,
    HW_KEY_VOLUME,
    HW_OK,
    LANGUAGE_RADIOS,
    LG_OK,
    MB_CONFIRM_DIALOGS,
    MB_EMAIL,
    MB_ENERGY_EQUIPMENT,
    MB_ENERGY_EXPORT,
    MB_HARDWARE,
    MB_MAINTENANCE,
    MB_NETWORK,
    MB_POINTS,
    MB_PPD,
    MB_REMOTE_MANAGEMENT,
    MB_SCREENSAVER,
    MB_WEB_USERS,
    MB_ZONES,
    MT_ADD,
    MT_AVAILABLE,
    MT_IN_MAINTENANCE,
    MT_OK,
    MT_REMOVE,
    NW_BUTTONS,
    NW_OK,
    NW_TEXT_FIELDS,
    PA_INFO_BUTTON,
    PA_NAME_BUTTON,
    PA_OK,
    PT_CHANGE,
    PT_LIST,
    RG_LANGUAGE_BUTTON,
    RG_OK,
    SS_DELAY_BUTTON,
    SS_MODE,
    SS_OFF_ON_ERROR,
    SS_OK,
    SS_ON_ON_ERROR,
    TM_CHANGE,
    TM_DST_END,
    TM_DST_OFF,
    TM_DST_ON,
    TM_DST_START,
    TM_OK,
    US_ADMIN,
    US_NAME_BUTTON,
    US_OK,
    US_PASSWORD_BUTTON,
    US_USER,
    US_ZONES_BUTTON,
    UZ_LIST,
    UZ_OK,
    WU_ADD,
    WU_CHANGE,
    WU_DELETE,
    WU_LIST,
    ZA_INFO_BUTTON,
    ZA_NAME_BUTTON,
    ZA_OK,
    ZA_START_BUTTON,
    ZA_STOP_BUTTON,
    ZN_LIST,
    ZN_SETTINGS,
    DstRule,
    DstSettings,
    EnergyNavigatorInfo,
    ScreensaverMode,
    SystemSettings,
    ZoneAttributes,
    confirm_dialogs_from_screen,
    dst_from_screen,
    email_from_screen,
    energy_export_from_screen,
    energy_sampling_from_screen,
    hardware_from_screen,
    language_from_screen,
    maintenance_from_screen,
    network_from_screen,
    points_from_screen,
    ppd_from_screen,
    remote_management_from_screen,
    row_index,
    screensaver_from_screen,
    web_users_from_screen,
    zone_attributes_from_screen,
    zones_from_screen,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Coroutine

    from .client import ItmClient, Point, SessionStore
    from .protocol import Reply

_LOGGER = logging.getLogger(__name__)

# Icon view (screen 1) part IDs (ScMainStdIcon.initScreen in script.js).
ICON_BTN_TOP, ICON_BTN_DOWN, ICON_BTN_UP, ICON_BTN_LIST, ICON_PANEL = 1, 2, 3, 4, 6
ICON_ID_ZONE = 1000
LOGIN_BUTTON = 4


_T = TypeVar("_T")


async def _run_to_completion(coro: Coroutine[Any, Any, _T]) -> _T:
    """Run cleanup to the end even if the caller is cancelled meanwhile.

    Each request is bounded by the HTTP timeout, so this ends. The cancellation is
    passed on once the cleanup is done. A plain asyncio.shield would return at once
    on cancellation and leave the cleanup running unsupervised.

    Args:
    ----
        coro: The cleanup to run.

    Returns:
    -------
        What the cleanup returned.

    Raises:
    ------
        asyncio.CancelledError: The caller was cancelled while the cleanup ran.

    """
    task = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():  # the cleanup itself was cancelled, not just us
                raise
            cancelled = True
            continue
        except BaseException as e:
            if cancelled:
                raise asyncio.CancelledError from e
            raise
        if cancelled:
            raise asyncio.CancelledError
        return result


class _Session(ChangeoverSessionMixin):  # pylint: disable=too-many-public-methods
    """One logged-in conversation with the iTM. Only created by ItmClient."""

    def __init__(
        self,
        client: ItmClient,
        post: Callable[[str], Awaitable[str]],
        store: SessionStore,
    ) -> None:
        self._client = client
        self._post = post
        self._store = store
        self.token = NO_TOKEN
        self.state = UIState()
        self.logged_in = False

    async def send(  # noqa: PLR0913, PLR0917  # pylint: disable=too-many-arguments
        # The arguments mirror the fields of the wire format.
        self,
        screen: int = -1,
        part: int = -1,
        event: int = EventType.CLICK,
        data_type: int = DataType.NONE,
        number: int = -1,
        string: str = "",
        array: tuple[int, ...] = (),
    ) -> Reply:
        """Send a GUI event and merge the reply into the UI state.

        A click on a part the iTM reports disabled is refused: it hangs the iTM.
        The unit settings dialog keeps its own checks: it opens with its controls
        disabled, and ticking a setting's checkbox enables them.
        """
        if event == EventType.CLICK and screen != ScreenID.SETTINGS:
            self.refuse_disabled(screen, part, f"screen {screen} part {part}")
        body = encode_event(
            self.token, screen, part, event, data_type, number, array, string=string
        )
        request = asyncio.ensure_future(self._post(body))
        try:
            text = await asyncio.shield(request)
        except asyncio.CancelledError:
            # The iTM may already have processed this request and moved on to a new
            # token. Let it finish so the token stays known and the caller's cleanup
            # can still log out, then re-raise.
            with contextlib.suppress(Exception):
                await self._take_token(await request)
            raise
        await self._take_token(text)
        reply = parse_reply(text)
        if event == EventType.CLICK and data_type == DataType.INT:
            self.state.select_locally(screen, part, number)
        self.state.apply(reply.screens)
        for s in reply.screens:
            if s.update == "LOGOUT":
                self.logged_in = False
                await self._store.save(None)
                if s.data and s.data[0] == 2 and len(s.data) > 1:
                    raise VersionMismatch(s.data[1])
                msg = f"iTM ended the session: {s.data}"
                raise SessionEnded(msg)
        return reply

    def refuse_disabled(self, screen: int, part: int, what: str) -> None:
        """Raise for a part the iTM reports disabled; a hidden part is fine.

        Raises
        ------
            ItmCommandError: The part is disabled.

        """
        if self.state.get(screen, part, "ENABLED") == 0:
            msg = f"{what}: the iTM has this disabled"
            raise ItmCommandError(msg)

    async def _take_token(self, text: str) -> None:
        # Taken before parsing, so a parser problem can never lose the session.
        # Something that is not a token (an HTML error page, a truncated reply) must
        # not replace the last good one.
        if not is_token(text[:32]):
            msg = f"not an iTM reply: {text[:80]!r}"
            raise ProtocolError(msg)
        self.token = text[:32]
        if self.logged_in:
            await self._store.save(self.token)

    async def login(self, username: str, password: str) -> None:
        """Log in; raises LoginRejected when the iTM refuses."""
        version = self._client.web_version
        try:
            await self.send(
                event=EventType.INIT, data_type=DataType.STRING, string=version
            )
        except VersionMismatch as e:
            # After a firmware update the iTM wants its new version string; it tells
            # us which. The protocol may have changed too, so say so loudly, but
            # carry on.
            _LOGGER.warning(
                "iTM web UI version changed from %s to %s; "
                "this client was built against %s",
                version,
                e.version,
                WEB_VERSION,
            )
            self._client.web_version = e.version
            self.token = NO_TOKEN
            await self.send(
                event=EventType.INIT, data_type=DataType.STRING, string=e.version
            )
        self.token = NO_TOKEN
        digest = hashlib.sha256(password.encode()).hexdigest() if password else ""
        self.logged_in = True  # from here on every reply token is persisted
        reply = await self.send(
            ScreenID.LOGIN,
            LOGIN_BUTTON,
            EventType.CLICK,
            DataType.STRING,
            string=f"{len(username):04d}{username}{digest}",
        )
        if any(
            s.screen == ScreenID.LOGIN and s.update == "DLG_OPEN" for s in reply.screens
        ):
            self.logged_in = False
            await self._store.save(None)
            msg = "login rejected (wrong credentials, or another web session is active)"
            raise LoginRejected(msg)

    async def logout(self) -> None:
        """Log out, keeping the token unless the iTM confirms the logout."""
        if not self.logged_in:
            return
        self.logged_in = False
        try:
            await self.send(event=EventType.LOGOUT)
        except SessionEnded as err:
            # The iTM says the session is gone (e.g. it had already timed out).
            _LOGGER.debug("session already ended: %s", err)
        except (ItmError, aiohttp.ClientError, TimeoutError):
            # Not confirmed (no answer, an HTTP error, or a reply that can't be
            # read): the session may still be active, so keep the latest token for
            # the next login to release.
            await self._store.save(self.token)
            raise
        await self._store.save(None)

    # -- icon view navigation -----------------------------------------------------
    def icons(self) -> list[tuple[str, int, int]]:
        """Return (name, icon ID, state) of every icon in the current zone."""
        panel = self.state.get(ScreenID.STDICON, ICON_PANEL, "ICONPANEL")
        return [(i[0], i[1], i[2]) for i in panel["icons"]] if panel else []

    async def select_icon(self, index: int) -> None:
        """Select an icon of the current zone."""
        await self.send(
            ScreenID.STDICON, ICON_PANEL, EventType.CLICK, DataType.INT, index
        )

    async def select_by_name(self, name: str) -> None:
        """Select an icon by name and check the operation panel shows it."""
        names = [i[0] for i in self.icons()]
        if name not in names:
            msg = f"{name!r} not found here; available: {names}"
            raise ItmCommandError(msg)
        await self.select_icon(names.index(name))
        shown = self.state.get(ScreenID.OPERATION, P_NAME, "LABEL")
        if shown != name:
            msg = f"operation panel shows {shown!r} instead of {name!r}"
            raise ItmCommandError(msg)

    async def open_zone(self, path: tuple[str, ...]) -> None:
        """Go down into a zone path, starting at the top level.

        A step is a zone's name; the built-in "All" zone is found by its name in
        any display language (the iTM translates it); "#n" is the nth zone (from 0)
        at that level, which doesn't depend on names at all.
        """
        # Every session starts at the top level of the icon view, so the path is
        # absolute.
        for zone in path:
            await self.select_by_name(_zone_name(zone, self.icons()))
            await self.send(ScreenID.STDICON, ICON_BTN_DOWN)

    async def read_units(self) -> dict[str, Unit]:
        """Read every unit in the current zone."""
        units = {}
        for i, (name, icon_id, _) in enumerate(self.icons()):
            if icon_id == ICON_ID_ZONE:
                continue
            await self.select_icon(i)
            unit = unit_from_panel(self.state)
            if unit.name != name:
                msg = f"operation panel shows {unit.name!r} instead of {name!r}"
                raise ItmCommandError(msg)
            units[name] = unit
        return units

    # -- list view ----------------------------------------------------------------
    async def read_units_from_list(self) -> dict[str, Unit]:
        """Read every unit in the current zone from the list view, in one request.

        The list view is shown with the icon view's "List" button and the icon view
        shown again afterwards, so the session ends where it started.
        """
        await self.send(ScreenID.STDICON, ICON_BTN_LIST)
        try:
            return units_from_list(self.state)
        finally:
            await self.send(ScreenID.STDLIST, L_BTN_ICON)
            # The list stays in the merged state; a later read in this session
            # must not mistake it for a fresh one.
            self.state.screens.get(ScreenID.STDLIST, {}).pop((L_LIST, "PNTLIST"), None)

    # -- operation panel ----------------------------------------------------------
    def _writable(self) -> None:
        if self._client.read_only:
            msg = "the client is read-only"
            raise ReadOnlyError(msg)

    async def choose(self, part: int, index: int, *, confirm: bool = False) -> None:
        """Pick an option of an operation panel control.

        With confirm, a confirm dialog the pick opens is answered Yes: the iTM asks
        before it puts a unit in setback ("setback low?").
        """
        self._writable()
        if self._client.dry_run:
            # A panel pick is committed by the TIMER that follows; plan both.
            self._client.planned.append(
                f"pick option {index}: screen {ScreenID.OPERATION} part {part}"
            )
            return
        reply = await self.send(
            ScreenID.OPERATION, part, EventType.CLICK, DataType.INT, index
        )
        if confirm and all(d[1] == "CONFIRM" for d in reply.dialogs()):
            await self._answer_confirm(ScreenID.OPERATION, reply)
        elif reply.dialogs():
            msg = f"iTM opened a dialog: {reply.dialogs()}"
            raise ItmCommandError(msg)

    async def commit(self, delay: float) -> None:
        """Commit operation panel changes like the browser does."""
        self._writable()
        if self._client.dry_run:
            self._client.planned.append(
                f"commit: screen {ScreenID.OPERATION} event {EventType.TIMER}"
            )
            return
        # The browser waits SEND_TIMER_INTERVAL (3s) after the last change, then
        # sends a TIMER event.
        await asyncio.sleep(delay)
        await self.send(ScreenID.OPERATION, -1, EventType.TIMER)

    # -- settings dialog (screen 7): tick what to change, set it, press OK ---------
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
            self._writable()
            if self._client.dry_run:
                self._client.planned.append(
                    f"save settings: screen {ScreenID.SETTINGS} part {D_OK} event 1"
                )
                ok = False
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
            raise ItmCommandError(msg)
        labels = self.state.get(ScreenID.SETTINGS, D_MODE, "COMBO_STRS")
        await self.dialog_click(D_MODE_CHECK, 1)
        await self.dialog_click(D_MODE, labels.index(caps.mode_labels[mode]))

    async def set_dialog_fan_speed(self, caps: Capabilities, fan_speed: str) -> None:
        """Tick the fan speed in the settings dialog and cycle to the wanted one."""
        if not caps.fan_speed:
            msg = f"{caps.name}: fan speed cannot be changed on this unit"
            raise ItmCommandError(msg)
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
        self, button: int, value: float, what: str, screen: int = ScreenID.SETTINGS
    ) -> None:
        """Press a "change" button and enter a number in the dialog it opens.

        The dialog says which kind of number it takes (type) and its range; the
        browser sends the value as an int, or as [integer part, thousandths].
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
            msg = f"{what}: {value} outside the iTM's range {low}-{high}"
            raise ItmCommandError(msg)
        whole = int(value)
        thousandths = round((value - whole) * 1000)
        if kind == 0:
            await self.send(d.screen, dialog_id, EventType.CLICK, DataType.INT, whole)
        elif kind in {1, 2}:
            data_type = DataType.FLOAT if kind == 1 else DataType.SETTEMP_C
            await self.send(
                d.screen,
                dialog_id,
                EventType.CLICK,
                data_type,
                2,
                array=(whole, thousandths),
            )
        elif kind == 3:
            await self.send(
                d.screen, dialog_id, EventType.CLICK, DataType.SETTEMP_F, round(value)
            )
        else:
            await self.send(d.screen, dialog_id, EventType.CLICK2)
            msg = f"{what}: number input of kind {kind} is not supported"
            raise ItmCommandError(msg)

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

    async def change_unit_settings(self, changes: dict[str, Any]) -> None:
        """Change the selected unit's locks, overtime and limits, then OK.

        The dialog is cancelled when anything goes wrong.
        """
        await self.open_settings()
        try:
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
        except BaseException:
            if self.settings_open and self.logged_in:
                with contextlib.suppress(Exception):
                    await _run_to_completion(self.close_settings(ok=False))
            raise

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
            raise ItmCommandError(msg)
        wanted = [o.casefold() for o in options]
        if value.strip().casefold() not in wanted:
            msg = f"{what.replace('_', ' ')}: {value!r} not offered: {options}"
            raise ItmCommandError(msg)
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
            raise ItmCommandError(msg)
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
            await self.close_settings(ok=True)
        except BaseException:
            if self.settings_open and self.logged_in:
                with contextlib.suppress(Exception):
                    await _run_to_completion(self.close_settings(ok=False))
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

    # -- points of any kind -------------------------------------------------------
    async def read_points(self, *, details: bool = False) -> dict[str, Point]:
        """Read every point in the current zone, whatever its kind.

        Args:
        ----
            details: Also read each analog point's Details screen (one request
                more per analog point).

        Returns:
        -------
            The points by name: a Unit for indoor and ventilation units, an
            AnalogPoint for analog inputs.

        """
        points: dict[str, Point] = {}
        for i, (name, icon_id, _) in enumerate(self.icons()):
            if icon_id == ICON_ID_ZONE:
                continue
            await self.select_icon(i)
            point: Point
            if point_kind(icon_id) is PointKind.ANALOG:
                point = analog_from_panel(self.state)
                if details:
                    point = replace(point, **await self.read_analog_details())
            else:
                point = unit_from_panel(self.state)
            if point.name != name:
                msg = f"operation panel shows {point.name!r} instead of {name!r}"
                raise ItmCommandError(msg)
            points[name] = point
        return points

    # -- main menu ----------------------------------------------------------------
    def indicators(self) -> ItmIndicators:
        """Return the main menu bar's indicators and clock (open in every session)."""
        return indicators_from_screen(self.state)

    async def open_menu(self) -> None:
        """Open the menu list from the main menu bar."""
        if MenuScreen.MENULIST in self.state.screens:
            return
        reply = await self.send(ScreenID.MAINMENU, MENU_OPEN)
        if MenuScreen.MENULIST not in self.state.screens:
            msg = f"the menu list did not open: {reply.dialogs()}"
            raise ItmCommandError(msg)

    async def close_menu(self) -> None:
        """Close the menu list, if open."""
        if MenuScreen.MENULIST in self.state.screens:
            await self.send(ScreenID.MAINMENU, MENU_CLOSE)

    async def open_menu_screen(self, button: int, screen: MenuScreen) -> bool:
        """Press a menu list button and tell whether its screen opened.

        Args:
        ----
            button: The menu list part to click.
            screen: The screen that should open.

        Returns:
        -------
            Whether it did (a hidden, unavailable function doesn't react).

        Raises:
        ------
            ItmCommandError: The iTM answered with a dialog instead.

        """
        await self.open_menu()
        reply = await self.send(MenuScreen.MENULIST, button)
        if reply.dialogs() and screen not in self.state.screens:
            msg = f"menu screen {screen.name} did not open: {reply.dialogs()}"
            raise ItmCommandError(msg)
        return screen in self.state.screens

    async def close_screen(self, screen: MenuScreen) -> None:
        """Close (or cancel) a menu screen, if it is open. Never OK."""
        if screen in self.state.screens:
            await self.send(screen, CLOSE_PART[screen])
            self.state.screens.pop(screen, None)

    async def poll(self) -> Reply:
        """Send the POLLING event the browser sends while idle, for pending updates."""
        bits = {
            ScreenID.MAINMENU: POLL_MAINMENU,
            ScreenID.STDICON: POLL_STDICON,
            ScreenID.STDLIST: POLL_STDLIST,
            ScreenID.STDLAYOUT: POLL_STDLAYOUT,
            ScreenID.OPERATION: POLL_OPERATION,
            MenuScreen.MENULIST: 32,
        }
        mask = 0
        for screen in self.state.screens:
            mask |= bits.get(screen, 64)  # type: ignore[call-overload]
        return await self.send(-1, -1, EventType.POLLING, DataType.INT, mask)

    async def wait_for_progress(self, reply: Reply, *, attempts: int = 20) -> None:
        """Wait out a PROGRESS dialog a reply opened, like the browser does.

        The browser asks for progress (UPD_PROG with the job ID) until the iTM
        closes the dialog, then a POLLING event brings the data.

        Args:
        ----
            reply: The reply that may have opened the dialog.
            attempts: How many progress requests to make at most.

        Raises:
        ------
            ItmCommandError: The dialog did not close in time.

        """
        dialogs = [
            s
            for s in reply.screens
            if s.update == "DLG_OPEN" and s.data[1] == "PROGRESS"
        ]
        if not dialogs:
            return
        dialog = dialogs[0]
        screen, dialog_id, job_id = dialog.screen, dialog.data[0], dialog.data[3]
        for _ in range(attempts):
            progress = await self.send(
                screen, dialog_id, EventType.UPD_PROG, DataType.INT, job_id
            )
            if any(s.update == "DLG_CLOSE" for s in progress.screens):
                await self.poll()
                return
            await asyncio.sleep(self._client.progress_interval)
        msg = "the iTM did not finish loading in time"
        raise ItmCommandError(msg)

    async def read_info(self) -> ItmInfo:
        """Read the version, region and time screens (region is best effort)."""
        await self.open_menu_screen(MB_VERSION, MenuScreen.VERSION)
        version = version_from_screen(self.state)
        await self.close_screen(MenuScreen.VERSION)
        region: dict[str, Any] = {}
        # The region button is hidden in the menu; the iTM opens it anyway (seen on
        # a device), but don't count on it.
        if await self.open_menu_screen(MB_REGION, MenuScreen.REGION):
            region = region_from_screen(self.state)
            await self.close_screen(MenuScreen.REGION)
        await self.open_menu_screen(MB_TIME, MenuScreen.TIME)
        clock = clock_from_screen(self.state, region.get("date_format"))
        await self.close_screen(MenuScreen.TIME)
        return ItmInfo(
            version=version["version"],
            build=version["build"],
            language=region.get("language"),
            date_format=region.get("date_format"),
            time_24h=region.get("time_24h"),
            temperature_unit=region.get("temperature_unit"),
            decimal_separator=region.get("decimal_separator"),
            clock=clock["clock"],
            clock_text=clock["clock_text"],
            dst_enabled=clock["dst_enabled"],
            read_at=datetime.now(),  # noqa: DTZ005 - compared with the iTM's local clock
        )

    async def read_changeover_groups(
        self, *, details: bool = False
    ) -> list[ChangeoverGroup]:
        """Read the auto changeover groups; with details also members and settings."""
        await self.open_menu_screen(MB_CHANGEOVER, MenuScreen.CHANGEOVER)
        try:
            groups = changeover_groups_from_screen(self.state)
            if not details:
                return groups
            detailed = []
            for i, group in enumerate(groups):
                await self.send(
                    MenuScreen.CHANGEOVER,
                    CO_LIST,
                    EventType.CLICK,
                    DataType.INTARRAY,
                    1,
                    array=(i,),
                )
                await self.send(MenuScreen.CHANGEOVER, CO_MEMBERS_BUTTON)
                members = changeover_members_from_screen(self.state)
                await self.close_screen(MenuScreen.CHANGEOVER_GROUP)
                await self.send(MenuScreen.CHANGEOVER, CO_SETTINGS_BUTTON)
                settings = changeover_settings_from_screen(self.state)
                await self.close_screen(MenuScreen.CHANGEOVER_SETTINGS)
                detailed.append(replace(group, members=members, **settings))
            return detailed
        finally:
            await self.close_screen(MenuScreen.CHANGEOVER_SETTINGS)
            await self.close_screen(MenuScreen.CHANGEOVER_GROUP)
            await self.close_screen(MenuScreen.CHANGEOVER)

    async def read_changeover_candidates(self) -> list[tuple[str, str, str]]:
        """Read which points an auto changeover group could hold (needs a group)."""
        await self.open_menu_screen(MB_CHANGEOVER, MenuScreen.CHANGEOVER)
        try:
            if not changeover_groups_from_screen(self.state):
                return []
            await self.send(
                MenuScreen.CHANGEOVER,
                CO_LIST,
                EventType.CLICK,
                DataType.INTARRAY,
                1,
                array=(0,),
            )
            await self.send(MenuScreen.CHANGEOVER, CO_MEMBERS_BUTTON)
            return changeover_candidates_from_screen(self.state)
        finally:
            await self.close_screen(MenuScreen.CHANGEOVER_GROUP)
            await self.close_screen(MenuScreen.CHANGEOVER)

    async def read_setback(self) -> SetbackSettings:
        """Read the setback offsets and the recovery temperatures."""
        await self.open_menu_screen(MB_SETBACK, MenuScreen.SETBACK)
        try:
            await self.send(MenuScreen.SETBACK, SB_ADVANCED)
            settings = setback_from_screen(self.state)
            await self.close_screen(MenuScreen.SETBACK_ADVANCED)
            return settings
        finally:
            await self.close_screen(MenuScreen.SETBACK_ADVANCED)
            await self.close_screen(MenuScreen.SETBACK)

    async def read_overtime(self) -> int | None:
        """Read the overtime timer's duration in minutes."""
        await self.open_menu_screen(MB_OVERTIME, MenuScreen.OVERTIME)
        try:
            return overtime_from_screen(self.state)
        finally:
            await self.close_screen(MenuScreen.OVERTIME)

    async def read_emergency_stop(self) -> list[EmergencyStopProgram]:
        """Read the emergency stop programs."""
        await self.open_menu_screen(MB_EMERGENCY_STOP, MenuScreen.EMERGENCY_STOP)
        try:
            return emergency_stop_from_screen(self.state)
        finally:
            await self.close_screen(MenuScreen.EMERGENCY_STOP)

    async def read_schedules(self) -> list[ScheduleSummary]:
        """Read the schedule programs as the program list shows them."""
        await self.open_menu_screen(MB_SCHEDULE, MenuScreen.SCHEDULE)
        try:
            return schedules_from_screen(self.state)
        finally:
            await self.close_screen(MenuScreen.SCHEDULE)

    async def _read_screen(
        self, button: int, screen: MenuScreen, read: Callable[[UIState], _T]
    ) -> _T | None:
        """Open a menu screen, read it and close it; None when it doesn't open."""
        if not await self.open_menu_screen(button, screen):
            return None
        try:
            return read(self.state)
        finally:
            await self.close_screen(screen)

    async def read_system(self, only: frozenset[str] | None = None) -> SystemSettings:
        """Read the system screens (or only the named SystemSettings fields)."""

        def wanted(name: str) -> bool:
            return only is None or name in only

        values: dict[str, Any] = {}
        if wanted("zones") or wanted("zone_position"):
            zones = await self._read_screen(
                MB_ZONES, MenuScreen.ZONES, zones_from_screen
            )
            if zones is not None:
                values["zone_position"], values["zones"] = zones
        screens: tuple[tuple[str, int, MenuScreen, Callable[[UIState], Any]], ...] = (
            ("points", MB_POINTS, MenuScreen.POINTS, points_from_screen),
            (
                "maintenance",
                MB_MAINTENANCE,
                MenuScreen.MAINTENANCE,
                maintenance_from_screen,
            ),
            ("network", MB_NETWORK, MenuScreen.NETWORK, network_from_screen),
            ("email", MB_EMAIL, MenuScreen.EMAIL, email_from_screen),
            ("web_users", MB_WEB_USERS, MenuScreen.WEB_USERS, web_users_from_screen),
            ("hardware", MB_HARDWARE, MenuScreen.HARDWARE, hardware_from_screen),
            (
                "screensaver",
                MB_SCREENSAVER,
                MenuScreen.SCREENSAVER,
                screensaver_from_screen,
            ),
            (
                "confirm_dialogs",
                MB_CONFIRM_DIALOGS,
                MenuScreen.CONFIRM_DIALOGS,
                confirm_dialogs_from_screen,
            ),
            (
                "remote_management",
                MB_REMOTE_MANAGEMENT,
                MenuScreen.REMOTE_MANAGEMENT,
                remote_management_from_screen,
            ),
            ("ppd", MB_PPD, MenuScreen.PPD, ppd_from_screen),
        )
        for name, button, screen, read in screens:
            if wanted(name):
                values[name] = await self._read_screen(button, screen, read)
        if wanted("energy_navigator"):
            values["energy_navigator"] = await self._read_energy_navigator()
        return SystemSettings(**values)

    async def _read_energy_navigator(self) -> EnergyNavigatorInfo | None:
        """Read the energy navigator's equipment screen, then its export screen."""
        sampling = await self._read_screen(
            MB_ENERGY_EQUIPMENT,
            MenuScreen.ENERGY_EQUIPMENT,
            energy_sampling_from_screen,
        )
        periods = await self._read_screen(
            MB_ENERGY_EXPORT, MenuScreen.ENERGY_EXPORT, energy_export_from_screen
        )
        if sampling is None and periods is None:
            return None
        return EnergyNavigatorInfo(*(sampling or (None, None, None)), periods or ())

    # -- writes -------------------------------------------------------------------
    async def write(  # noqa: PLR0913, PLR0917  # pylint: disable=too-many-arguments
        self,
        label: str,
        screen: int,
        part: int,
        event: int = EventType.CLICK,
        data_type: int = DataType.NONE,
        number: int = -1,
        string: str = "",
    ) -> Reply | None:
        """Send the request that makes a change, unless this is a dry run.

        Args:
        ----
            label: What the request does, for the dry run's plan.
            screen: The screen of the control.
            part: The control.
            event: The event type.
            data_type: The type of the value sent along.
            number: The number sent along.
            string: The string sent along.

        Returns:
        -------
            The reply, or None in a dry run (the request is recorded instead).

        """
        self._writable()
        if event == EventType.CLICK and screen != ScreenID.SETTINGS:
            self.refuse_disabled(screen, part, label)
        if self._client.dry_run:
            self._client.planned.append(
                f"{label}: screen {screen} part {part} event {event}"
                + (f" {string!r}" if string else f" {number}" if number != -1 else "")
            )
            return None
        return await self.send(screen, part, event, data_type, number, string)

    async def press_ok(self, label: str, screen: int, ok: int, cancel: int) -> bool:
        """Press a screen's OK (answering its confirm dialog), or cancel in a dry run.

        Args:
        ----
            label: What the OK saves, for the dry run's plan.
            screen: The screen.
            ok: Its OK part.
            cancel: Its Cancel part.

        Returns:
        -------
            True when the change was sent, False in a dry run.

        Raises:
        ------
            ItmCommandError: The iTM refused (an error dialog) or the screen stayed.

        """
        reply = await self.write(label, screen, ok)
        if reply is None:
            await self.send(screen, cancel)
            self.state.screens.pop(screen, None)
            return False
        await self._answer_confirm(screen, reply)
        if screen in self.state.screens:
            msg = f"{label}: the screen did not close"
            raise ItmCommandError(msg)
        return True

    async def _answer_confirm(self, screen: int, reply: Reply) -> None:
        """Answer Yes to a confirm dialog; raise on an error dialog."""
        for dialog in reply.dialogs():
            if dialog[1] == "CONFIRM":
                reply = await self.send(screen, dialog[0])
                if reply.dialogs():
                    msg = f"iTM refused: {reply.dialogs()}"
                    raise ItmCommandError(msg)
            elif dialog[1] in {"ERROR", "INFO"}:
                msg = f"iTM refused: {dialog[2]!r}"
                raise ItmCommandError(msg)

    # -- setback and overtime writes ----------------------------------------------
    async def _pick_number(self, screen: int, part: int, value: int, what: str) -> None:
        """Pick a value in a numeric combo, refusing one the iTM doesn't offer."""
        options = self.state.get(screen, part, "COMBO_NUMS") or []
        if value not in options:
            msg = f"{what}: {value} not offered: {options}"
            raise ItmCommandError(msg)
        await self.send(
            screen, part, EventType.CLICK, DataType.INT, options.index(value)
        )

    async def set_setback(self, changes: dict[str, int]) -> None:
        """Pick new setback offsets and/or recovery temperatures and press OK."""
        await self.open_menu_screen(MB_SETBACK, MenuScreen.SETBACK)
        s, a = MenuScreen.SETBACK, MenuScreen.SETBACK_ADVANCED
        parts = {
            "high_up": SB_HIGH_UP,
            "high_back": SB_HIGH_BACK,
            "low_up": SB_LOW_UP,
            "low_back": SB_LOW_BACK,
        }
        advanced = {"recovery_cool": SBA_COOL, "recovery_heat": SBA_HEAT}
        try:
            if any(key in changes for key in advanced):
                await self.send(s, SB_ADVANCED)
                if a not in self.state.screens:
                    msg = "the advanced setback screen did not open"
                    raise ItmCommandError(msg)
                for key, part in advanced.items():
                    if key in changes:
                        await self._pick_number(a, part, changes[key], f"setback {key}")
                await self.press_ok("save setback recovery", a, SBA_OK, CLOSE_PART[a])
            for key, part in parts.items():
                if key in changes:
                    await self._pick_number(s, part, changes[key], f"setback {key}")
            await self.press_ok("save setback offsets", s, SB_OK, CLOSE_PART[s])
        finally:
            await self.close_screen(a)
            await self.close_screen(s)

    async def set_overtime(self, minutes: int) -> None:
        """Pick the overtime timer's duration and press OK."""
        await self.open_menu_screen(MB_OVERTIME, MenuScreen.OVERTIME)
        s = MenuScreen.OVERTIME
        try:
            options = self.state.get(s, OT_DURATION, "COMBO_STRS") or []
            values = [
                int(m.group()) if (m := re.search(r"\d+", o)) else -1 for o in options
            ]
            if minutes not in values:
                msg = f"overtime: {minutes} min not offered: {options}"
                raise ItmCommandError(msg)
            await self.send(
                s, OT_DURATION, EventType.CLICK, DataType.INT, values.index(minutes)
            )
            await self.press_ok(
                f"save overtime duration {minutes} min", s, OT_OK, CLOSE_PART[s]
            )
        finally:
            await self.close_screen(s)

    async def _pick_option(
        self, screen: int, part: int, index: int | None, what: str
    ) -> None:
        """Pick a combo option by index, refusing one the iTM doesn't offer."""
        options = self.state.get(screen, part, "COMBO_STRS") or []
        if index is None or not 0 <= index < len(options):
            msg = f"{what}: not offered: {options}"
            raise ItmCommandError(msg)
        await self.send(screen, part, EventType.CLICK, DataType.INT, index)

    def _option_index(self, screen: int, part: int, value: int) -> int | None:
        options = [
            str(o).strip() for o in self.state.get(screen, part, "COMBO_STRS") or []
        ]
        return options.index(str(value)) if str(value) in options else None

    async def set_hardware(self, changes: dict[str, Any]) -> None:
        """Pick backlight, buzzer and key click settings and press OK."""
        s = MenuScreen.HARDWARE
        if not await self.open_menu_screen(MB_HARDWARE, s):
            msg = "the hardware settings screen did not open"
            raise ItmCommandError(msg)
        try:
            for key, part in (
                ("backlight", HW_BACKLIGHT),
                ("buzzer_volume", HW_BUZZER_VOLUME),
                ("key_click_volume", HW_KEY_VOLUME),
            ):
                if key in changes:
                    index = self._option_index(s, part, changes[key])
                    await self._pick_option(s, part, index, f"{key} {changes[key]}")
            if "buzzer_minutes" in changes:
                minutes = changes["buzzer_minutes"]
                index = (
                    BUZZER_DURATIONS.index(minutes)
                    if minutes in BUZZER_DURATIONS
                    else None
                )
                await self._pick_option(
                    s, HW_BUZZER_DURATION, index, f"buzzer duration {minutes}"
                )
            await self.press_ok("save hardware settings", s, HW_OK, CLOSE_PART[s])
        finally:
            await self.close_screen(s)

    async def set_screensaver(self, changes: dict[str, Any]) -> None:
        """Pick the screensaver mode, delay and error behaviour and press OK."""
        s = MenuScreen.SCREENSAVER
        if not await self.open_menu_screen(MB_SCREENSAVER, s):
            msg = "the screensaver screen did not open"
            raise ItmCommandError(msg)
        try:
            if "mode" in changes:
                modes = list(ScreensaverMode)
                await self._pick_option(
                    s, SS_MODE, modes.index(changes["mode"]), f"mode {changes['mode']}"
                )
            if "delay_minutes" in changes:
                await self.answer_number(
                    SS_DELAY_BUTTON,
                    changes["delay_minutes"],
                    "screensaver delay",
                    screen=s,
                )
            if "off_on_error" in changes:
                await self.send(
                    s, SS_OFF_ON_ERROR if changes["off_on_error"] else SS_ON_ON_ERROR
                )
            await self.press_ok("save screensaver settings", s, SS_OK, CLOSE_PART[s])
        finally:
            await self.close_screen(s)

    async def select_row(self, screen: int, part: int, row: int) -> Reply:
        """Select a list row, as the browser does.

        A list sends its selection as an array of rows (the web UI's
        onSelectedMultiItem); the iTM ignores a plain number there.
        """
        return await self.send(
            screen, part, EventType.CLICK, DataType.INTARRAY, 1, array=(row,)
        )

    async def _select_listed(
        self, screen: int, part: int, column: int, name: str, what: str
    ) -> None:
        """Select the list row whose column shows name; raise when it isn't there."""
        row = row_index(self.state, screen, part, column, name)
        if row is None:
            msg = f"{what}: {name!r} not found"
            raise ItmCommandError(msg)
        await self.select_row(screen, part, row)

    async def click(self, screen: int, part: int, what: str) -> Reply:
        """Click a button, naming it when the iTM has it disabled."""
        self.refuse_disabled(screen, part, what)
        return await self.send(screen, part)

    async def _open_system_screen(self, button: int, screen: MenuScreen) -> None:
        if not await self.open_menu_screen(button, screen):
            msg = f"the {screen.name.lower()} screen did not open"
            raise ItmCommandError(msg)

    async def read_zone_attributes(self, zone: str) -> ZoneAttributes:
        """Open a zone's settings screen, read it and cancel."""
        s, a = MenuScreen.ZONES, MenuScreen.ZONE_SETTINGS
        await self._open_system_screen(MB_ZONES, s)
        try:
            await self._select_listed(s, ZN_LIST, 0, zone, "zone")
            await self.click(s, ZN_SETTINGS, "zone")
            return zone_attributes_from_screen(self.state)
        finally:
            await self.close_screen(a)
            await self.close_screen(s)

    async def read_dst(self) -> DstSettings:
        """Read the time screen's summer time switch and rule."""
        await self._open_system_screen(MB_TIME, MenuScreen.TIME)
        try:
            return dst_from_screen(self.state)
        finally:
            await self.close_screen(MenuScreen.TIME)

    async def _network_input(self, field: str, value: str) -> None:
        """Open a network field's input, check it, and enter value (or plan it)."""
        n = MenuScreen.NETWORK
        text = field in NW_TEXT_FIELDS
        kind = "CHARINPUT" if text else "IPADDR"
        reply = await self.click(n, NW_BUTTONS[field], f"network {field}")
        dialogs = [
            u for u in reply.screens if u.update == "DLG_OPEN" and u.data[1] == kind
        ]
        if not dialogs:
            msg = f"network {field}: no {kind} input opened: {reply.dialogs()}"
            raise ItmCommandError(msg)
        d = dialogs[0]
        if text and not d.data[4] <= len(value) <= d.data[5]:
            await self.send(d.screen, d.data[0], EventType.CLICK2)
            msg = f"network {field}: {value!r} must be {d.data[4]}-{d.data[5]} long"
            raise ItmCommandError(msg)
        data_type = DataType.STRING if text else DataType.IPADDRESS
        if self._client.dry_run:
            # Not even into the input: a dry run of a network change sends no value.
            await self.send(d.screen, d.data[0], EventType.CLICK2)
            self._client.planned.append(
                f"network {field} = {value}: screen {n} part {d.data[0]} "
                f"event {EventType.CLICK} type {data_type}"
            )
            return
        reply = await self.send(
            d.screen, d.data[0], EventType.CLICK, data_type, string=value
        )
        if reply.dialogs():
            msg = f"network {field}: the iTM refused {value!r}: {reply.dialogs()}"
            raise ItmCommandError(msg)

    async def set_network(self, changes: dict[str, str]) -> None:
        """Enter the network changes and press OK (in a dry run: plan, Cancel)."""
        n = MenuScreen.NETWORK
        await self._open_system_screen(MB_NETWORK, n)
        try:
            for field, value in changes.items():
                await self._network_input(field, value)
            await self.press_ok("save network settings", n, NW_OK, CLOSE_PART[n])
        finally:
            await self.close_screen(n)

    async def service_login(self, password: str) -> None:
        """Log in to service mode: tap the menu's four corners, enter the password."""
        await self.open_menu()
        reply = None
        for corner in SERVICE_CORNERS:
            reply = await self.send(MenuScreen.MENULIST, corner)
        dialogs = [
            u
            for u in (reply.screens if reply else [])
            if u.update == "DLG_OPEN" and u.data[1] == "PASSWORD"
        ]
        if not dialogs:
            msg = "the service login did not ask for a password"
            raise ItmCommandError(msg)
        d = dialogs[0]
        reply = await self.send(
            d.screen, d.data[0], EventType.CLICK, DataType.STRING, string=password
        )
        if reply.dialogs() or not service_logged_in(self.state):
            for dialog in reply.dialogs():
                with contextlib.suppress(Exception):
                    await self.send(d.screen, dialog[0])
            msg = "the iTM refused the service password"
            raise ItmCommandError(msg)

    async def read_service(self) -> ServiceSettings:
        """Read the service screens (after service_login), each left with Cancel."""
        values: dict[str, Any] = {}
        screens: tuple[
            tuple[int, MenuScreen, Callable[[UIState], dict[str, Any]]], ...
        ] = (
            (MB_TIME_ZONE, MenuScreen.TIME_ZONE, time_zone_from_screen),
            (
                MB_CHANGEOVER_OPTION,
                MenuScreen.CHANGEOVER_OPTION,
                changeover_options_from_screen,
            ),
            (MB_CONTACT, MenuScreen.CONTACT, contact_from_screen),
            (MB_R32, MenuScreen.R32, r32_from_screen),
            (
                MB_CENTRAL_MONITOR,
                MenuScreen.CENTRAL_MONITOR,
                central_monitor_from_screen,
            ),
            (MB_ACTIVATION, MenuScreen.ACTIVATION, activation_from_screen),
            (MB_DEALER_OPTIONS, MenuScreen.DEALER_OPTIONS, dealer_options_from_screen),
            (
                MB_ENERGY_SAVE_MODE,
                MenuScreen.ENERGY_SAVE_MODE,
                energy_save_mode_from_screen,
            ),
        )
        for button, screen, read in screens:
            result = await self._read_screen(button, screen, read)
            if result is not None:
                values.update(result)
        return ServiceSettings(**values)

    async def set_maintenance(self, point: str, *, on: bool) -> None:
        """Move a point into (or out of) the maintenance list and press OK."""
        s = MenuScreen.MAINTENANCE
        await self._open_system_screen(MB_MAINTENANCE, s)
        try:
            source, button = (
                (MT_AVAILABLE, MT_ADD) if on else (MT_IN_MAINTENANCE, MT_REMOVE)
            )
            target = MT_IN_MAINTENANCE if on else MT_AVAILABLE
            if row_index(self.state, s, target, 1, point) is not None:
                return
            await self._select_listed(s, source, 1, point, "maintenance")
            await self.click(s, button, "maintenance")
            verb = "into" if on else "out of"
            await self.press_ok(
                f"put {point} {verb} maintenance", s, MT_OK, CLOSE_PART[s]
            )
        finally:
            await self.close_screen(s)

    async def set_point_attributes(
        self, point: str, *, name: str | None, info: str | None
    ) -> None:
        """Change a management point's name and/or description, then OK."""
        s, a = MenuScreen.POINTS, MenuScreen.POINT_SETTINGS
        await self._open_system_screen(MB_POINTS, s)
        try:
            await self._select_listed(s, PT_LIST, 1, point, "point")
            await self.click(s, PT_CHANGE, "point")
            if a not in self.state.screens:
                msg = f"{point}: the point settings did not open"
                raise ItmCommandError(msg)
            if name is not None:
                await self.answer_text(a, PA_NAME_BUTTON, name, f"{point} name")
            if info is not None:
                await self.answer_text(a, PA_INFO_BUTTON, info, f"{point} description")
            await self.press_ok(f"save point {point}", a, PA_OK, CLOSE_PART[a])
        finally:
            await self.close_screen(a)
            await self.close_screen(s)

    async def set_zone_attributes(
        self,
        zone: str,
        *,
        name: str | None,
        info: str | None,
        start_interval: int | None,
        stop_interval: int | None,
    ) -> None:
        """Change a zone's name, description and/or intervals, then OK."""
        s, a = MenuScreen.ZONES, MenuScreen.ZONE_SETTINGS
        await self._open_system_screen(MB_ZONES, s)
        try:
            await self._select_listed(s, ZN_LIST, 0, zone, "zone")
            await self.click(s, ZN_SETTINGS, "zone")
            if a not in self.state.screens:
                msg = f"{zone}: the zone settings did not open"
                raise ItmCommandError(msg)
            if name is not None:
                await self.answer_text(a, ZA_NAME_BUTTON, name, f"{zone} name")
            if info is not None:
                await self.answer_text(a, ZA_INFO_BUTTON, info, f"{zone} description")
            for button, value, what in (
                (ZA_START_BUTTON, start_interval, "start interval"),
                (ZA_STOP_BUTTON, stop_interval, "stop interval"),
            ):
                if value is not None:
                    await self.answer_number(button, value, f"{zone} {what}", screen=a)
            async with zones_edit.zone_errors(self):
                await self.press_ok(f"save zone {zone}", a, ZA_OK, CLOSE_PART[a])
        finally:
            await self.close_screen(a)
            await self.close_screen(s)

    async def _answer_password(
        self, reply: Reply, password: str, what: str = "password"
    ) -> None:
        """Enter the password in the dialog(s) a reply opened (new, then repeat).

        A dry run never types it: it cancels the input and plans ``***``.
        """
        for _ in range(2):
            dialogs = [
                u
                for u in reply.screens
                if u.update == "DLG_OPEN" and u.data[1] == "PASSWORD"
            ]
            if not dialogs:
                break
            d = dialogs[0]
            low, high = d.data[3], d.data[4]
            if not low <= len(password) <= high:
                await self.send(d.screen, d.data[0], EventType.CLICK2, DataType.INT, 0)
                msg = f"the password must be {low}-{high} characters"
                raise ItmCommandError(msg)
            if self._client.dry_run:
                await self.send(d.screen, d.data[0], EventType.CLICK2, DataType.INT, 0)
                self._client.planned.append(
                    f"{what} = ***: screen {d.screen} input {d.data[0]}"
                )
                return
            reply = await self.send(
                d.screen, d.data[0], EventType.CLICK, DataType.STRING, string=password
            )
        errors = [d for d in reply.dialogs() if d[1] in {"ERROR", "INFO"}]
        if errors:
            msg = f"the iTM refused the password: {errors}"
            raise ItmCommandError(msg)

    async def _pick_user_zones(self, zones: tuple[str, ...]) -> None:
        """Select the zones a web user manages in 1202 and press its OK."""
        z = MenuScreen.USER_ZONES
        await self.click(MenuScreen.USER_SETUP, US_ZONES_BUTTON, "user zones")
        if z not in self.state.screens:
            msg = "the user's zone list did not open"
            raise ItmCommandError(msg)
        rows = []
        for zone in zones:
            row = row_index(self.state, z, UZ_LIST, 0, zone)
            if row is None:
                await self.close_screen(z)
                msg = f"zone {zone!r} not offered for web users"
                raise ItmCommandError(msg)
            rows.append(row)
        await self.send(
            z, UZ_LIST, EventType.CLICK, DataType.INTARRAY, len(rows), array=tuple(rows)
        )
        reply = await self.send(z, UZ_OK)
        await self._answer_confirm(z, reply)
        self.state.screens.pop(z, None)

    async def save_web_user(
        self,
        name: str,
        *,
        new: bool,
        password: str | None,
        admin: bool | None,
        zones: tuple[str, ...] | None = None,
    ) -> None:
        """Add a web user, or change one: name, password, type and zones, then OK."""
        s, u = MenuScreen.WEB_USERS, MenuScreen.USER_SETUP
        await self._open_system_screen(MB_WEB_USERS, s)
        try:
            listed = row_index(self.state, s, WU_LIST, 0, name) is not None
            if new and listed:
                msg = f"web user {name!r} exists already"
                raise ItmCommandError(msg)
            if new:
                await self.click(s, WU_ADD, "add web user")
            else:
                await self._select_listed(s, WU_LIST, 0, name, "web user")
                await self.click(s, WU_CHANGE, "change web user")
            if u not in self.state.screens:
                msg = f"{name}: the user setup did not open"
                raise ItmCommandError(msg)
            if new:
                await self.answer_text(u, US_NAME_BUTTON, name, "user name")
            if password is not None:
                reply = await self.send(u, US_PASSWORD_BUTTON)
                await self._answer_password(
                    reply, password, f"web user {name} password"
                )
            if admin is not None:
                await self.send(u, US_ADMIN if admin else US_USER)
            if zones is not None:
                await self._pick_user_zones(zones)
            verb = "add" if new else "change"
            await self.press_ok(f"{verb} web user {name}", u, US_OK, CLOSE_PART[u])
        finally:
            await self.close_screen(u)
            await self.close_screen(s)

    async def delete_web_user(self, name: str) -> None:
        """Select a web user, press Delete and confirm."""
        s = MenuScreen.WEB_USERS
        await self._open_system_screen(MB_WEB_USERS, s)
        try:
            await self._select_listed(s, WU_LIST, 0, name, "web user")
            if self.state.get(s, WU_DELETE, "ENABLED") == 0:
                msg = f"the iTM does not allow deleting {name!r}"
                raise ItmCommandError(msg)
            reply = await self.write(f"delete web user {name}", s, WU_DELETE)
            if reply is not None:
                await self._answer_confirm(s, reply)
        finally:
            await self.close_screen(s)

    async def read_language(self) -> str | None:
        """Open the language screen from the region screen and read the language."""
        r, g = MenuScreen.REGION, MenuScreen.LANGUAGE
        await self._open_system_screen(MB_REGION, r)
        try:
            await self.send(r, RG_LANGUAGE_BUTTON)
            return language_from_screen(self.state)
        finally:
            await self.close_screen(g)
            await self.close_screen(r)

    async def set_language(self, code: str) -> None:
        """Pick a display language: language screen OK, then region screen OK."""
        r, g = MenuScreen.REGION, MenuScreen.LANGUAGE
        await self._open_system_screen(MB_REGION, r)
        try:
            await self.send(r, RG_LANGUAGE_BUTTON)
            if g not in self.state.screens:
                msg = "the language screen did not open"
                raise ItmCommandError(msg)
            await self.send(g, LANGUAGE_RADIOS[code])
            await self.press_ok(f"pick language {code}", g, LG_OK, CLOSE_PART[g])
            await self.press_ok(f"save language {code}", r, RG_OK, CLOSE_PART[r])
        finally:
            await self.close_screen(g)
            await self.close_screen(r)

    async def _pick_dst_rule(self, parts: tuple[int, ...], rule: DstRule) -> None:
        """Pick a summer time rule's month, week, weekday and hour."""
        t = MenuScreen.TIME
        month, week, weekday, hour_part = parts
        hours = [_hour(o) for o in self.state.get(t, hour_part, "COMBO_STRS") or []]
        picks = (
            (month, rule.month - 1),
            (week, rule.week - 1),
            (weekday, rule.weekday),
            (hour_part, hours.index(rule.hour) if rule.hour in hours else None),
        )
        for part, index in picks:
            if self.state.get(t, part, "ENABLED") == 0:
                msg = "summer time: the rule is disabled on the iTM"
                raise ItmCommandError(msg)
            await self._pick_option(t, part, index, f"summer time {rule}")

    async def set_region(self, changes: dict[str, Any]) -> None:
        """Pick date format, 12/24 hours, °C/°F and decimal separator, then OK."""
        r = MenuScreen.REGION
        await self._open_system_screen(MB_REGION, r)
        try:
            if (fmt := changes.get("date_format")) is not None:
                await self._pick_option(
                    r, R_DATE_FORMAT, DATE_FORMATS.index(fmt), f"date format {fmt}"
                )
            for key, (first, second), values in (
                ("time_24h", (R_24H, R_12H), (True, False)),
                ("temperature_unit", (R_CELSIUS, R_FAHRENHEIT), ("°C", "°F")),
                ("decimal_separator", (R_DECIMAL_POINT, R_DECIMAL_COMMA), (".", ",")),
            ):
                if (value := changes.get(key)) is not None:
                    await self.send(r, first if value == values[0] else second)
            await self.press_ok("save region settings", r, RG_OK, CLOSE_PART[r])
        finally:
            await self.close_screen(r)

    async def set_dst(self, *, enabled: bool, start: DstRule, end: DstRule) -> None:
        """Switch summer time on (with its rule) or off, then OK."""
        t = MenuScreen.TIME
        await self._open_system_screen(MB_TIME, t)
        try:
            await self.send(t, TM_DST_ON if enabled else TM_DST_OFF)
            if enabled:
                await self._pick_dst_rule(TM_DST_START, start)
                await self._pick_dst_rule(TM_DST_END, end)
            await self.press_ok("save summer time", t, TM_OK, CLOSE_PART[t])
        finally:
            await self.close_screen(t)

    async def set_clock(self, when: datetime) -> None:
        """Enter a date and time in the clock's input, then OK."""
        t = MenuScreen.TIME
        await self._open_system_screen(MB_TIME, t)
        try:
            reply = await self.send(t, TM_CHANGE)
            dialogs = [
                u
                for u in reply.screens
                if u.update == "DLG_OPEN" and u.data[1] == "TIME"
            ]
            if not dialogs:
                msg = f"the clock input did not open: {reply.dialogs()}"
                raise ItmCommandError(msg)
            d = dialogs[0]
            value = (
                when.year,
                when.month - 1,
                when.day,
                when.hour,
                when.minute,
                when.second,
            )
            low, high = tuple(d.data[10:16]), tuple(d.data[16:22])
            if not low <= value <= high:
                await self.send(d.screen, d.data[0], EventType.CLICK2)
                msg = f"the clock: {when} outside the iTM's range"
                raise ItmCommandError(msg)
            await self.send(
                d.screen, d.data[0], EventType.CLICK, DataType.TIME, 6, array=value
            )
            await self.press_ok("save the clock", t, TM_OK, CLOSE_PART[t])
        finally:
            await self.close_screen(t)

    # -- schedules ----------------------------------------------------------------
    async def _select_program(self, name: str) -> None:
        await self.open_menu_screen(MB_SCHEDULE, MenuScreen.SCHEDULE)
        names = [p.name for p in schedules_from_screen(self.state)]
        if name not in names:
            msg = f"schedule program {name!r} not found; available: {names}"
            raise ItmCommandError(msg)
        await self.send(
            MenuScreen.SCHEDULE,
            SCH_PANEL,
            EventType.CLICK,
            DataType.INT,
            names.index(name),
        )

    async def read_schedule_day(self, name: str, *, days_ahead: int = 0) -> ScheduleDay:
        """Read what a program does on a day, from its day overview."""
        await self._select_program(name)
        try:
            await self.send(MenuScreen.SCHEDULE, SCH_DAY_BUTTON)
            if MenuScreen.SCHEDULE_DAY not in self.state.screens:
                msg = "the day overview did not open"
                raise ItmCommandError(msg)
            for _ in range(days_ahead):
                await self.send(MenuScreen.SCHEDULE_DAY, SD_NEXT)
            return schedule_day_from_screen(self.state)
        finally:
            await self.close_screen(MenuScreen.SCHEDULE_DAY)
            await self.close_screen(MenuScreen.SCHEDULE)

    async def _read_day_events(
        self, radio: int, edit_button: int, *, details: bool
    ) -> tuple[ScheduleEvent, ...]:
        """Open a weekday's or special day's event list and read it (and each event)."""
        await self.send(MenuScreen.SCHEDULE_EDITOR, radio)
        await self.send(MenuScreen.SCHEDULE_EDITOR, edit_button)
        if MenuScreen.SCHEDULE_EVENTS not in self.state.screens:
            msg = "the event list did not open"
            raise ItmCommandError(msg)
        try:
            events = day_events_from_screen(self.state)
            if not details:
                return tuple(events)
            detailed = []
            for event in events:
                # The list has separator rows too.
                await self.send(
                    MenuScreen.SCHEDULE_EVENTS,
                    EV_LIST,
                    EventType.CLICK,
                    DataType.INTARRAY,
                    1,
                    array=(event.row,),
                )
                # The iTM disables the buttons for a row that is no event; clicking
                # a disabled button hangs its web server, so never do that.
                if self.state.get(MenuScreen.SCHEDULE_EVENTS, EV_EDIT, "ENABLED") == 0:
                    msg = f"the iTM does not open the event at row {event.row}"
                    raise ItmCommandError(msg)
                await self.send(MenuScreen.SCHEDULE_EVENTS, EV_EDIT)
                if MenuScreen.SCHEDULE_EVENT in self.state.screens:
                    detailed.append(event_from_screen(self.state, event))
                    await self.close_screen(MenuScreen.SCHEDULE_EVENT)
                else:
                    detailed.append(event)
            return tuple(detailed)
        finally:
            await self.close_screen(MenuScreen.SCHEDULE_EVENT)
            await self.close_screen(MenuScreen.SCHEDULE_EVENTS)

    async def close_editor(self) -> None:
        """Leave the schedule editor without saving: Cancel, then Yes."""
        if MenuScreen.SCHEDULE_EDITOR not in self.state.screens:
            return
        reply = await self.send(
            MenuScreen.SCHEDULE_EDITOR, CLOSE_PART[MenuScreen.SCHEDULE_EDITOR]
        )
        for dialog in reply.dialogs():
            if dialog[1] == "CONFIRM":
                await self.send(MenuScreen.SCHEDULE_EDITOR, dialog[0])
        if MenuScreen.SCHEDULE_EDITOR in self.state.screens:
            msg = "the schedule editor did not close"
            raise ItmCommandError(msg)

    async def set_schedule_enabled(self, name: str, *, enabled: bool) -> None:
        """Open a program's editor, pick enabled or disabled, OK (and confirm)."""
        await self._select_program(name)
        try:
            await self.send(MenuScreen.SCHEDULE, SCH_EDIT_BUTTON)
            e = MenuScreen.SCHEDULE_EDITOR
            if e not in self.state.screens:
                msg = "the schedule editor did not open"
                raise ItmCommandError(msg)
            program = program_from_editor(self.state)
            if program.enabled == enabled:
                return
            await self.send(e, SE_ENABLED if enabled else SE_DISABLED)
            verb = "enable" if enabled else "disable"
            reply = await self.write(f"{verb} schedule {name}", e, SE_OK)
            if reply is not None:
                await self._answer_confirm(e, reply)
                if e in self.state.screens:
                    msg = f"{name}: the editor did not close after OK"
                    raise ItmCommandError(msg)
        finally:
            await self.close_editor()
            await self.close_screen(MenuScreen.SCHEDULE)

    async def create_schedule(self, name: str) -> None:
        """Press Add, rename the new program, OK (and confirm).

        The iTM opens the editor on a new, disabled program without events (with a
        default name); nothing exists until OK.
        """
        await self.open_menu_screen(MB_SCHEDULE, MenuScreen.SCHEDULE)
        e = MenuScreen.SCHEDULE_EDITOR
        try:
            names = [p.name for p in schedules_from_screen(self.state)]
            if name in names:
                msg = f"schedule program {name!r} exists already"
                raise ItmCommandError(msg)
            if self.state.get(MenuScreen.SCHEDULE, SCH_ADD_BUTTON, "ENABLED") == 0:
                msg = "the iTM does not allow another schedule program"
                raise ItmCommandError(msg)
            await self.send(MenuScreen.SCHEDULE, SCH_ADD_BUTTON)
            if e not in self.state.screens:
                msg = "the schedule editor did not open"
                raise ItmCommandError(msg)
            reply = await self.send(e, SE_RENAME)
            dialogs = [d for d in reply.dialogs() if d[1] == "CHARINPUT"]
            if not dialogs:
                msg = f"Change did not ask for a name: {reply.dialogs()}"
                raise ItmCommandError(msg)
            reply = await self.send(
                e, dialogs[0][0], EventType.CLICK, DataType.STRING, string=name
            )
            if reply.dialogs():  # a refused name; the read-back checks the rest
                msg = f"the iTM did not take the name {name!r}: {reply.dialogs()}"
                raise ItmCommandError(msg)
            saved = await self.write(f"create schedule {name}", e, SE_OK)
            if saved is not None:
                await self._answer_confirm(e, saved)
                if e in self.state.screens:
                    msg = f"{name}: the editor did not close after OK"
                    raise ItmCommandError(msg)
        finally:
            await self.close_editor()
            await self.close_screen(MenuScreen.SCHEDULE)

    async def delete_schedule(self, name: str) -> None:
        """Select a program, press Delete and confirm."""
        await self._select_program(name)
        try:
            # A disabled button hangs the iTM's web server when clicked.
            if self.state.get(MenuScreen.SCHEDULE, SCH_DELETE_BUTTON, "ENABLED") == 0:
                msg = f"the iTM does not allow deleting {name!r}"
                raise ItmCommandError(msg)
            reply = await self.write(
                f"delete schedule {name}", MenuScreen.SCHEDULE, SCH_DELETE_BUTTON
            )
            if reply is not None:
                await self._answer_confirm(MenuScreen.SCHEDULE, reply)
        finally:
            await self.close_screen(MenuScreen.SCHEDULE)

    async def read_schedule(
        self, name: str, *, events: bool = True, details: bool = False
    ) -> ScheduleProgram:
        """Read a program through its editor: properties, patterns and calendar.

        Everything is read and the editor left with Cancel; nothing is saved.
        With events, every weekday's and special day's event list is opened; with
        details, each event's own settings screen too (two requests per event).
        """
        await self._select_program(name)
        try:
            await self.send(MenuScreen.SCHEDULE, SCH_EDIT_BUTTON)
            if MenuScreen.SCHEDULE_EDITOR not in self.state.screens:
                msg = "the schedule editor did not open"
                raise ItmCommandError(msg)
            tab = MenuScreen.SCHEDULE_EDITOR, SE_TAB, EventType.TAB_SELECT, DataType.INT
            await self.send(*tab, SE_TAB_WEEKLY)
            program = program_from_editor(self.state)
            weekly: dict[int, tuple[ScheduleEvent, ...]] = {}
            for day, slots in program.weekly_slots.items():
                if slots and events:
                    weekly[day] = await self._read_day_events(
                        SE_DAY_RADIOS[day], SE_EDIT_DAY, details=details
                    )
            await self.send(*tab, SE_TAB_SPECIAL)
            program = program_from_editor(self.state)
            special: dict[str, tuple[ScheduleEvent, ...]] = {}
            for i, (label, slots) in enumerate(program.special_slots.items()):
                if slots and events:
                    special[label] = await self._read_day_events(
                        SE_SPECIAL_RADIOS[i], SE_EDIT_SPECIAL, details=details
                    )
            await self.send(*tab, SE_TAB_CALENDAR)
            program = program_from_editor(self.state)
            names = special_day_names(self.state)
            return replace(program, weekly=weekly, special=special, special_names=names)
        finally:
            await self.close_screen(MenuScreen.SCHEDULE_EVENT)
            await self.close_screen(MenuScreen.SCHEDULE_EVENTS)
            await self.close_editor()
            await self.close_screen(MenuScreen.SCHEDULE)

    async def read_history(
        self,
        *,
        day: date | None = None,
        include_management: bool = False,
        pages: int = 1,
        date_format: str | None = None,
    ) -> list[History]:
        """Read the history: today's newest page, or a day and more pages.

        The list loads behind a progress dialog; the day and class filters are
        display settings of the screen, not changes to the iTM.
        """
        reply = await self.send(MenuScreen.MENULIST, MB_HISTORY)
        if MenuScreen.HISTORY not in self.state.screens:
            await self.open_menu()
            reply = await self.send(MenuScreen.MENULIST, MB_HISTORY)
        try:
            await self.wait_for_progress(reply)
            # The class filters are display settings of the screen; set the
            # management one (logins) as asked, whatever it was.
            shown = self.state.get(
                MenuScreen.HISTORY, H_CLASS_MANAGEMENT, "CHECK_STATE"
            )
            wanted = 1 if include_management else 0
            if shown is not None and shown != wanted:
                await self.send(
                    MenuScreen.HISTORY,
                    H_CLASS_MANAGEMENT,
                    EventType.CLICK,
                    DataType.INT,
                    wanted,
                )
                await self.wait_for_progress(
                    await self.send(MenuScreen.HISTORY, H_REFRESH)
                )
            if day is not None:
                await self._history_go_to(day, date_format)
            result = [history_from_screen(self.state)]
            for _ in range(pages - 1):
                if self.state.get(MenuScreen.HISTORY, H_PAGE_OLDER, "ENABLED") != 1:
                    break
                await self.wait_for_progress(
                    await self.send(MenuScreen.HISTORY, H_PAGE_OLDER)
                )
                page = history_from_screen(self.state)
                if not page.entries or page.page == result[-1].page:
                    break
                result.append(page)
            return result
        finally:
            await self.close_screen(MenuScreen.HISTORY)

    async def _history_go_to(self, day: date, date_format: str | None) -> None:
        shown = parse_shown_date(
            self.state.get(MenuScreen.HISTORY, H_DATE, "TEXT"), date_format
        )
        if shown is None:
            msg = "the history's day could not be read"
            raise ItmCommandError(msg)
        if day == shown:
            return
        step = H_DAY_NEXT if day > shown else H_DAY_PREVIOUS
        for _ in range(abs((day - shown).days)):
            await self.send(MenuScreen.HISTORY, step)
        # The day buttons only change the date shown; Refresh loads that day's list.
        await self.wait_for_progress(await self.send(MenuScreen.HISTORY, H_REFRESH))

    async def read_optional_functions(self) -> dict[str, OptionalFunction]:
        """Probe the functions the menu hides (temperature limit, interlock, ...)."""
        out = {}
        for name, (button, screen, list_part) in OPTIONAL_FUNCTIONS.items():
            opened = await self.open_menu_screen(button, screen)
            rows: tuple[tuple[str, ...], ...] = ()
            if opened:
                raw = self.state.get(screen, list_part, "LIST_ROW") or {}
                rows = tuple(
                    tuple(str(t) for t in r["text"]) for r in raw.get("rows", [])
                )
                await self.close_screen(screen)
            out[name] = OptionalFunction(available=opened, rows=rows)
        return out

    # -- error information --------------------------------------------------------
    async def read_error_information(self) -> ErrorInformation | None:
        """Read the error information the main menu bar offers, if any.

        Returns
        -------
            The errors, or None when the iTM shows no error indicator.

        """
        if not self.state.visible(ScreenID.MAINMENU, MM_ERROR_BUTTON):
            return None
        reply = await self.send(ScreenID.MAINMENU, MM_ERROR_BUTTON)
        if not any(
            s.screen == ScreenID.ERROR_INFORMATION and s.update == "OPEN"
            for s in reply.screens
        ):
            msg = f"error information did not open: {reply.dialogs()}"
            raise ItmCommandError(msg)
        info = error_information_from_screen(self.state)
        await self.send(ScreenID.ERROR_INFORMATION, E_CLOSE)
        return info

    @property
    def settings_open(self) -> bool:
        """Whether the settings dialog is open."""
        return ScreenID.SETTINGS in self.state.screens


def _zone_name(step: str, icons: list[tuple[str, int, int]]) -> str:
    """Resolve a zone path step (a name, the "All" zone in any language, or #n)."""
    names = [i[0] for i in icons]
    zones = [i[0] for i in icons if i[1] == ICON_ID_ZONE]
    if (m := re.fullmatch(r"#(\d+)", step)) is not None:
        index = int(m.group(1))
        if index < len(zones):
            return zones[index]
        raise ZoneNotFound(step, names)
    if step in names:
        return step
    if is_all_zone(step):
        found = [z for z in zones if is_all_zone(z)]
        if len(found) == 1:
            return found[0]
    raise ZoneNotFound(step, names)


def _hour(option: str) -> int | None:
    m = re.match(r"\s*(\d+)", str(option))
    return int(m.group(1)) if m else None


def _lock_radio(key: str, lock: RemoteLock, radios: tuple[int, ...]) -> int:
    """Return the radio part for a lock value, refusing one the dialog lacks."""
    order = (
        (RemoteLock.ALLOWED, RemoteLock.STOP_ONLY, RemoteLock.FORBIDDEN)
        if len(radios) == 3
        else (RemoteLock.ALLOWED, RemoteLock.FORBIDDEN)
    )
    if lock not in order:
        msg = f"{key}: {lock} is not offered"
        raise ItmCommandError(msg)
    return radios[order.index(lock)]
