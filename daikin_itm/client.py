"""Async client for the Daikin iTM web interface.

The iTM allows a single web session, so this client never keeps one open: every
public method logs in, does its work and logs out (also on errors and cancellation).
The page token is handed to a SessionStore after every reply, so a session left
behind by a crash can be released on the next login instead of blocking everyone
until the iTM times it out.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import hashlib
import ipaddress
import json
import logging
import os
import re
import tempfile
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Self, TypeVar

import aiohttp

from . import (
    energy_edit,
    energy_navigator,
    system_edit,
    zones_edit,
)
from .dealer_functions import DealerFunctionsMixin
from .demand import DemandMixin
from .emergency import EmergencyMixin
from .exceptions import (
    ExperimentalError,
    ItmCommandError,
    ItmError,
    LoginRejected,
    ProtocolError,
    ReadOnlyError,
    SessionEnded,
    SetpointOutOfRange,
    VersionMismatch,
    ZoneNotFound,
)
from .exports import ExportsMixin
from .lang import is_all_zone
from .layout import LayoutMixin
from .menu import (
    CG_ADD,
    CG_OK,
    CG_REMOVE,
    CLOSE_PART,
    CO_ADD,
    CO_DELETE,
    CO_DISABLE,
    CO_ENABLE,
    CO_LIST,
    CO_MEMBERS_BUTTON,
    CO_SETTINGS_BUTTON,
    CS_DIFFERENTIAL,
    CS_OK,
    CS_REFERENCE,
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
    ChangeoverReference,
    EmergencyStopProgram,
    History,
    ItmConfig,
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
    DIALOG_SETTING_KEYS,
    DIALOG_SETTINGS,
    DT_CLOSE,
    E_CLOSE,
    FAN_ICONS,
    FAN_SPEEDS,
    L_BTN_ICON,
    L_LIST,
    MM_ERROR_BUTTON,
    P_DETAILS_BUTTON,
    P_NAME,
    P_POWER,
    P_SETPOINT,
    P_SETTINGS_BUTTON,
    AnalogPoint,
    Capabilities,
    DialogSetting,
    ErrorInformation,
    PointKind,
    PowerState,
    RemoteLock,
    TempLimit,
    Unit,
    UnitSettings,
    analog_details_from_screen,
    analog_from_panel,
    capabilities_from_dialog,
    details_from_screen,
    dialog_setting_choice,
    error_information_from_screen,
    point_kind,
    settings_from_dialog,
    status_from_details,
    unit_from_panel,
    units_from_list,
)
from .point_registry import PointRegistryMixin
from .protocol import (
    MAX_REPLY_CHARS,
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
from .schedule_edit import (
    ScheduleMixin,
    special_day_names,
)
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
from .service_edit import ServiceEditMixin
from .state import UIState
from .system import (
    BUZZER_DURATIONS,
    EU_DST_END,
    EU_DST_START,
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
    HardwareSettings,
    ManagementPoint,
    NetworkSettings,
    ScreensaverMode,
    ScreensaverSettings,
    SystemSettings,
    WebUser,
    ZoneAttributes,
    ZoneInfo,
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
from .system_edit import SystemMixin

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Awaitable,
        Callable,
        Coroutine,
        Iterable,
        Mapping,
    )

    from .energy_navigator import (
        ActualYear,
        ConversionType,
        EnergyInstallerSettings,
        EnergyNavigator,
        PlannedYear,
        RoomTemperaturePoint,
        RuleDay,
        RuleRange,
    )
    from .protocol import Reply
    from .system_edit import OwnSettings, WebUserScreens

# An export (history, energy data) is a CSV file; refuse anything far larger.
MAX_EXPORT_BYTES = 32 * 1024 * 1024

# What a zone holds: indoor and ventilation units, and analog inputs.
Point = Unit | AnalogPoint

__all__ = [
    "DEFAULT_ZONE_PATH",
    "FileSessionStore",
    "ItmClient",
    "ItmCommandError",
    "ItmError",
    "LoginRejected",
    "MemorySessionStore",
    "Point",
    "SessionEnded",
    "SessionStore",
    "VersionMismatch",
    "ZoneNotFound",
]

_LOGGER = logging.getLogger(__name__)

# Icon view (screen 1) part IDs (ScMainStdIcon.initScreen in script.js).
ICON_BTN_TOP, ICON_BTN_DOWN, ICON_BTN_UP, ICON_BTN_LIST, ICON_PANEL = 1, 2, 3, 4, 6
ICON_ID_ZONE = 1000
LOGIN_BUTTON = 4
# How fetch_units reads the units: one operation panel per unit ("icons"), or the
# list view that shows every unit of the zone at once ("list").
UNIT_VIEWS = ("icons", "list")

# The top level of the icon view. Most iTMs group the units in zones; pass the path
# of zone names to them, see list_zones().
DEFAULT_ZONE_PATH: tuple[str, ...] = ()

_T = TypeVar("_T")


class SessionStore(Protocol):
    """Keeps the page token of the logged-in session between replies and runs."""

    async def load(self) -> str | None:
        """Return the saved token, or None."""

    async def save(self, token: str | None) -> None:
        """Save the token, or forget it when None."""


class MemorySessionStore:
    """Keeps the token in memory."""

    def __init__(self) -> None:
        """Start without a token."""
        self.token: str | None = None

    async def load(self) -> str | None:
        """Return the saved token, or None."""
        return self.token

    async def save(self, token: str | None) -> None:
        """Save the token, or forget it when None."""
        self.token = token


class FileSessionStore:
    """Keeps the token on disk so a killed process can release its session later.

    The token is a bearer credential for the logged-in iTM session, so the file is
    owner-only (0600, in a directory created as 0700) and remembers which host it
    belongs to: a token is never sent to another host than the one that issued it.
    """

    def __init__(self, path: str, host: str | None = None) -> None:
        """Initialize the store.

        Args:
        ----
            path: The file to keep the token in.
            host: The iTM the token belongs to.

        """
        self.path = path
        self.host = host

    async def load(self) -> str | None:
        """Return the saved token, or None."""
        return await asyncio.to_thread(self._load)

    async def save(self, token: str | None) -> None:
        """Save the token, or remove the file when None."""
        await asyncio.to_thread(self._save, token)

    def _load(self) -> str | None:
        try:
            data = json.loads(Path(self.path).read_text(encoding="utf-8"))
        except (FileNotFoundError, ValueError):
            return None
        if self.host is not None and data.get("host") != self.host:
            return None
        token: str | None = data.get("token")
        return token

    def _save(self, token: str | None) -> None:
        path = Path(self.path)
        if token is None:
            path.unlink(missing_ok=True)
            return
        directory = path.absolute().parent
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=directory, prefix=".session-")  # created 0600
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(
                    {"host": self.host, "token": token, "saved_at": time.time()}, f
                )
            Path(tmp).replace(path)
        except BaseException:
            Path(tmp).unlink(missing_ok=True)
            raise


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


class _Session:  # pylint: disable=too-many-public-methods
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

    # -- changeover writes --------------------------------------------------------
    async def _select_changeover_group(self, name: str) -> ChangeoverGroup:
        await self.open_menu_screen(MB_CHANGEOVER, MenuScreen.CHANGEOVER)
        groups = changeover_groups_from_screen(self.state)
        names = [g.name for g in groups]
        if names.count(name) != 1:
            msg = f"changeover group {name!r} not found once; available: {names}"
            raise ItmCommandError(msg)
        index = names.index(name)
        await self.send(
            MenuScreen.CHANGEOVER,
            CO_LIST,
            EventType.CLICK,
            DataType.INTARRAY,
            1,
            array=(index,),
        )
        return groups[index]

    async def set_changeover_enabled(self, name: str, *, enabled: bool) -> None:
        """Press Enable or Disable for a group."""
        group = await self._select_changeover_group(name)
        try:
            if group.enabled == enabled:
                return
            if enabled and not group.member_count:
                msg = f"{name}: a group without members can't be enabled"
                raise ItmCommandError(msg)
            label = f"{'enable' if enabled else 'disable'} changeover group {name}"
            reply = await self.write(
                label, MenuScreen.CHANGEOVER, CO_ENABLE if enabled else CO_DISABLE
            )
            if reply is not None and reply.dialogs():
                await self._answer_confirm(MenuScreen.CHANGEOVER, reply)
        finally:
            await self.close_screen(MenuScreen.CHANGEOVER)

    async def set_changeover_settings(
        self,
        name: str,
        *,
        differential: int | None,
        reference: ChangeoverReference | None,
    ) -> None:
        """Pick a group's differential and/or reference method and press OK."""
        await self._select_changeover_group(name)
        try:
            await self.send(MenuScreen.CHANGEOVER, CO_SETTINGS_BUTTON)
            s = MenuScreen.CHANGEOVER_SETTINGS
            if s not in self.state.screens:
                msg = f"{name}: the settings screen did not open"
                raise ItmCommandError(msg)
            if differential is not None:
                options = self.state.get(s, CS_DIFFERENTIAL, "COMBO_NUMS") or []
                if differential not in options:
                    msg = f"{name}: differential {differential} not offered: {options}"
                    raise ItmCommandError(msg)
                await self.send(
                    s,
                    CS_DIFFERENTIAL,
                    EventType.CLICK,
                    DataType.INT,
                    options.index(differential),
                )
            if reference is not None:
                radio = CS_REFERENCE[list(ChangeoverReference).index(reference)]
                await self.send(s, radio)
            await self.press_ok(
                f"save settings of changeover group {name}", s, CS_OK, CLOSE_PART[s]
            )
        finally:
            await self.close_screen(MenuScreen.CHANGEOVER_SETTINGS)
            await self.close_screen(MenuScreen.CHANGEOVER)

    async def create_changeover_group(self, name: str) -> None:
        """Press Add, enter the name, OK (the group starts empty, disabled)."""
        await self.open_menu_screen(MB_CHANGEOVER, MenuScreen.CHANGEOVER)
        try:
            names = [g.name for g in changeover_groups_from_screen(self.state)]
            if name in names:
                msg = f"changeover group {name!r} exists already"
                raise ItmCommandError(msg)
            reply = await self.send(MenuScreen.CHANGEOVER, CO_ADD)
            dialogs = [d for d in reply.dialogs() if d[1] == "CHARINPUT"]
            if not dialogs:
                msg = f"Add did not ask for a name: {reply.dialogs()}"
                raise ItmCommandError(msg)
            dialog_id = dialogs[0][0]
            sent = await self.write(
                f"create changeover group {name}",
                MenuScreen.CHANGEOVER,
                dialog_id,
                EventType.CLICK,
                DataType.STRING,
                string=name,
            )
            if sent is None:  # dry run: cancel the name dialog
                await self.send(MenuScreen.CHANGEOVER, dialog_id, EventType.CLICK2)
            elif sent.dialogs():
                await self._answer_confirm(MenuScreen.CHANGEOVER, sent)
        finally:
            await self.close_screen(MenuScreen.CHANGEOVER)

    async def set_changeover_members(self, name: str, members: tuple[str, ...]) -> None:
        """Make a group's members exactly these (added and removed via Registration)."""
        await self._select_changeover_group(name)
        s = MenuScreen.CHANGEOVER_GROUP
        try:
            await self.send(MenuScreen.CHANGEOVER, CO_MEMBERS_BUTTON)
            if s not in self.state.screens:
                msg = f"{name}: the members screen did not open"
                raise ItmCommandError(msg)
            wanted = list(dict.fromkeys(members))
            for member in list(changeover_members_from_screen(self.state)):
                if member not in wanted:
                    current = list(changeover_members_from_screen(self.state))
                    await self.send(
                        s,
                        2,
                        EventType.CLICK,
                        DataType.INTARRAY,
                        1,
                        array=(current.index(member),),
                    )
                    await self.send(s, CG_REMOVE)
            for member in wanted:
                if member in changeover_members_from_screen(self.state):
                    continue
                candidates = changeover_candidates_from_screen(self.state)
                names = [c[0] for c in candidates]
                if member not in names:
                    msg = f"{name}: {member!r} is not available; candidates: {names}"
                    raise ItmCommandError(msg)
                if candidates[names.index(member)][1]:
                    other = candidates[names.index(member)][1]
                    msg = f"{name}: {member!r} is in group {other!r}"
                    raise ItmCommandError(msg)
                await self.send(
                    s,
                    11,
                    EventType.CLICK,
                    DataType.INTARRAY,
                    1,
                    array=(names.index(member),),
                )
                await self.send(s, CG_ADD)
            if list(changeover_members_from_screen(self.state)) != wanted:
                got = changeover_members_from_screen(self.state)
                msg = f"{name}: members would be {got}, not {wanted}"
                raise ItmCommandError(msg)
            await self.press_ok(
                f"save members of changeover group {name}", s, CG_OK, CLOSE_PART[s]
            )
        finally:
            await self.close_screen(MenuScreen.CHANGEOVER_GROUP)
            await self.close_screen(MenuScreen.CHANGEOVER)

    async def delete_changeover_group(self, name: str) -> None:
        """Press Delete for a (disabled) group and confirm."""
        group = await self._select_changeover_group(name)
        try:
            if (
                group.enabled
                or self.state.get(MenuScreen.CHANGEOVER, CO_DELETE, "ENABLED") == 0
            ):
                msg = f"{name}: disable the group before deleting it"
                raise ItmCommandError(msg)
            reply = await self.write(
                f"delete changeover group {name}", MenuScreen.CHANGEOVER, CO_DELETE
            )
            if reply is not None:
                await self._answer_confirm(MenuScreen.CHANGEOVER, reply)
        finally:
            await self.close_screen(MenuScreen.CHANGEOVER)

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


async def _ignore(call: Awaitable[Any]) -> None:
    """Await a session write and drop its "sent" flag (dry runs plan instead)."""
    await call


def _shown(value: Any, key: str) -> Any:
    """Return a read-back value for comparing: no description reads as ""."""
    return "" if key == "info" and value is None else value


def _hour(option: str) -> int | None:
    m = re.match(r"\s*(\d+)", str(option))
    return int(m.group(1)) if m else None


def _power_is(unit: Unit, *, on: bool) -> bool:
    """Tell whether a unit is started (on) or stopped; setback is neither."""
    setback = unit.power_state in {
        PowerState.SETBACK,
        PowerState.SETBACK_LOW,
        PowerState.SETBACK_HIGH,
    }
    return unit.running == on and not setback


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


def _settings_mismatch(result: UnitSettings, changes: dict[str, Any]) -> dict[str, Any]:
    """Return the asked changes the read-back settings don't show."""
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


# set_unit_settings arguments not validated on a real iTM yet (the developing iTM's
# units don't offer them); they need allow_experimental.
_EXPERIMENTAL_UNIT_SETTINGS = frozenset(
    {
        "ventilation_mode",
        "ventilation_amount",
        "reset_filter_sign",
        "reset_dust_alarm",
        "force_dust_alarm_reset",
        "low_noise",
        "outdoor_low_noise",
        "capacity",
        "reheat",
        "leaving_water_cool",
        "leaving_water_heat",
        "storage_water",
        "output_repeat",
        "output_repeat_interval",
        "analog_output",
        "multistate_output",
    }
)


class ItmClient(  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    DealerFunctionsMixin,
    ServiceEditMixin,
    ExportsMixin,
    EmergencyMixin,
    SystemMixin,
    ScheduleMixin,
    DemandMixin,
    LayoutMixin,
    PointRegistryMixin,
):
    """Client for the web interface of a Daikin intelligent Touch Manager."""

    def __init__(  # noqa: PLR0913  # pylint: disable=too-many-arguments,too-many-locals
        # Keyword-only tuning arguments, part of the public API.
        self,
        host: str,
        username: str,
        password: str,
        *,
        session: aiohttp.ClientSession | None = None,
        store: SessionStore | None = None,
        zone_path: tuple[str, ...] = DEFAULT_ZONE_PATH,
        commit_delay: float = 3.0,
        min_write_interval: float = 5.0,
        confirm_timeout: float = 30.0,
        confirm_interval: float = 2.0,
        timeout: float = 30.0,
        read_only: bool = False,
        dry_run: bool = False,
        allow_experimental: bool = False,
    ) -> None:
        """Initialize the client.

        Args:
        ----
            host: "host", "host:port", or a full "http(s)://host" URL.
            username: The iTM user.
            password: The iTM user's password.
            session: The aiohttp session to use; one is created when omitted.
            store: Where to keep the session token; in memory when omitted.
            zone_path: The zones to go down into to find the indoor units.
            commit_delay: Seconds to wait before committing a setpoint change.
            min_write_interval: Minimum seconds between two writes to a unit.
            confirm_timeout: Seconds to wait for a change to show on the unit.
            confirm_interval: Seconds between the reads that confirm a change.
            timeout: Timeout of a single request, in seconds.
            read_only: Refuse every write (ReadOnlyError) before anything is sent,
                for exploring or monitoring an iTM without any risk of changing it.
            dry_run: Let writes do their picks but record the request that would
                make the change in `planned` instead of sending it, then cancel.
            allow_experimental: Allow the writes not validated on a real iTM yet
                (tested against the simulated iTM only); without it they raise
                ExperimentalError before anything is sent, except in a dry run.

        """
        # The iTM's web UI is plain HTTP by default, which exposes the (replayable)
        # login hash on the network; use https:// where the iTM offers it.
        base = host.rstrip("/") if "://" in host else "http://" + host
        self._url = base + "/cmd/"
        self._base = base
        self._username, self._password = username, password
        self._http = session
        self._owns_http = session is None
        self._store: SessionStore = store or MemorySessionStore()
        self.zone_path = tuple(zone_path)
        self.commit_delay = commit_delay
        self.min_write_interval = min_write_interval
        # Changes take a few seconds to reach the unit over the Daikin bus; a write is
        # only done once a fresh read shows it. Sending the next change before that
        # can make the iTM drop one of them.
        self.confirm_timeout = confirm_timeout
        self.confirm_interval = confirm_interval
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self.read_only = read_only
        # With dry_run, a write does everything up to the request that would make
        # the change, records that request in `planned` instead of sending it, and
        # cancels. Nothing on the iTM changes; the write methods return None.
        self.dry_run = dry_run
        # Writes not validated on a real iTM yet refuse unless this is set (a dry
        # run is always allowed); see _experimental.
        self.allow_experimental = allow_experimental
        self.planned: list[str] = []
        # Seconds between progress requests while the iTM loads a long list.
        self.progress_interval = 0.5
        # Updated automatically if the iTM reports another one.
        self.web_version = WEB_VERSION
        self._lock = asyncio.Lock()  # one session at a time, also within this process
        self._last_write: dict[str, float] = {}
        self._unit_locks: dict[str, asyncio.Lock] = {}
        # Unconfirmed changes (written with confirm=False) by unit: what was changed
        # and how to recognize it.
        self._unconfirmed: dict[str, Callable[[], Awaitable[object]]] = {}

    async def close(self) -> None:
        """Close the aiohttp session if the client created it."""
        if self._owns_http and self._http is not None:
            await self._http.close()
            self._http = None

    async def __aenter__(self) -> Self:
        """Async enter.

        Returns
        -------
            The ItmClient object.

        """
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        """Async exit: close the client."""
        await self.close()

    async def _post(self, body: str) -> str:
        if self._http is None:
            self._http = aiohttp.ClientSession()
        async with self._http.post(
            self._url,
            data=body.encode(),
            timeout=self._timeout,
            headers={"Content-Type": "text/plain"},
        ) as resp:
            resp.raise_for_status()
            # Replies are a few KB; refuse anything huge rather than buffering
            # whatever the other end sends.
            data = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                data += chunk
                if len(data) > MAX_REPLY_CHARS:
                    msg = f"reply larger than {MAX_REPLY_CHARS} bytes"
                    raise ProtocolError(msg)
            return data.decode("utf-8", errors="replace")

    async def _get_bytes(self, path: str) -> bytes:
        """GET a file the iTM exported (under /sdlfs/WebExport/), capped in size."""
        if self._http is None:
            self._http = aiohttp.ClientSession()
        async with self._http.get(self._base + path, timeout=self._timeout) as resp:
            resp.raise_for_status()
            data = bytearray()
            async for chunk in resp.content.iter_chunked(65536):
                data += chunk
                if len(data) > MAX_EXPORT_BYTES:
                    msg = f"export larger than {MAX_EXPORT_BYTES} bytes"
                    raise ProtocolError(msg)
            return bytes(data)

    async def _request(self, body: str) -> str:
        # Looks _post up on every request, so a replaced _post (tests) is used.
        return await self._post(body)

    def _new_session(self) -> _Session:
        return _Session(self, self._request, self._store)

    async def release_saved_session(self) -> bool:
        """Log out a session a previous run left behind.

        Returns
        -------
            True if there was one.

        """
        token = await self._store.load()
        if not token or token == NO_TOKEN:
            return False
        s = self._new_session()
        s.token, s.logged_in = token, True
        _LOGGER.info("releasing session left behind by a previous run")
        try:
            await s.logout()
        except (aiohttp.ClientResponseError, ProtocolError) as e:
            # The iTM answered but didn't confirm. Retrying a token it keeps refusing
            # would fail every later session, so give up on it; the iTM ends the
            # session itself once it times out.
            _LOGGER.warning(
                "could not release the previous session (%s); "
                "waiting for the iTM to time it out",
                e,
            )
            await self._store.save(None)
        return True

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[_Session]:
        async with self._lock:
            await self.release_saved_session()
            s = self._new_session()
            try:
                await s.login(self._username, self._password)
                yield s
            finally:
                # A cancelled caller (e.g. HA shutting down) still frees the single
                # session, and the lock is held until it has, so the next session
                # can't start while this logout is still on its way.
                await _run_to_completion(s.logout())

    @asynccontextmanager
    async def _own_session(self) -> AsyncIterator[_Session]:
        """Log in through the login screen's "User Settings" into 1205 (own settings).

        Leaving 1205 ends the session (or, if the iTM keeps it, a logout does).
        """
        async with self._lock:
            await self.release_saved_session()
            s = self._new_session()
            try:
                await system_edit.open_own_settings(s, self._username, self._password)
                yield s
            finally:
                await _run_to_completion(system_edit.leave_own_settings(s))

    # -- public API ---------------------------------------------------------------
    async def fetch_units(self, *, view: str = "icons") -> dict[str, Unit]:
        """Read every unit in the configured zone, in one short session.

        Args:
        ----
            view: "icons" reads each unit's operation panel (one request per unit)
                and gives the full state. "list" reads the zone's list view instead:
                one request for every unit, with the setpoint limits, wall remote
                locks, error code and R32 alarm as well, but without the on/off
                options and the setpoint range (see Unit).

        Returns:
        -------
            The units by name.

        Raises:
        ------
            ValueError: An unknown view.

        """
        if view not in UNIT_VIEWS:
            msg = f"unknown view {view!r}, expected one of {UNIT_VIEWS}"
            raise ValueError(msg)
        async with self._session() as s:
            await s.open_zone(self.zone_path)
            if view == "list":
                return await s.read_units_from_list()
            return await s.read_units()

    async def fetch_unit_settings(self, name: str) -> UnitSettings:
        """Read a unit's settings and status that only its dialogs show.

        The thermostat status and R32 alarm come from the Details screen, the wall
        remote locks, setpoint limits and overtime timer from the settings dialog
        (cancelled). Four requests besides the navigation, so not for every poll.

        Args:
        ----
            name: The unit's name.

        Returns:
        -------
            The unit's settings.

        """
        async with self._session() as s:
            await s.open_zone(self.zone_path)
            await s.select_by_name(name)
            status = await s.read_detail_status()
            await s.open_settings()
            try:
                return settings_from_dialog(name, s.state, status)
            finally:
                await s.close_settings(ok=False)

    def _writable(self) -> None:
        if self.read_only:
            msg = "the client is read-only"
            raise ReadOnlyError(msg)

    def _experimental(self, what: str) -> None:
        """Refuse a write not validated on a real iTM yet, unless allowed.

        Called at the top of every such write, before anything is sent. A
        read-only client refuses first (ReadOnlyError); a dry run is always
        allowed, so its plan can be seen without the opt-in.

        Args:
        ----
            what: What the write does, e.g. "start a leakage check".

        Raises:
        ------
            ReadOnlyError: The client is read-only.
            ExperimentalError: allow_experimental is off and this is no dry run.

        """
        self._writable()
        if self.dry_run or self.allow_experimental:
            return
        msg = (
            f"{what}: not validated on a real iTM yet; pass allow_experimental=True "
            "(or --experimental) to use it"
        )
        raise ExperimentalError(msg)

    async def fetch_unit(self, name: str) -> Unit:
        """Read a single unit (cheaper than fetch_units).

        Args:
        ----
            name: The unit's name.

        Returns:
        -------
            The unit.

        """
        async with self._session() as s:
            await s.open_zone(self.zone_path)
            await s.select_by_name(name)
            return unit_from_panel(s.state)

    async def fetch_points(self, *, details: bool = False) -> dict[str, Point]:
        """Read every point in the configured zone, whatever its kind.

        Like fetch_units, but analog inputs come back as AnalogPoint instead of a
        Unit with nothing in it. Indoor and ventilation units are Units; see
        Unit.kind.

        Args:
        ----
            details: Also read each analog point's Details screen (what it watches
                and its limits; one request more per analog point).

        Returns:
        -------
            The points by name.

        """
        async with self._session() as s:
            await s.open_zone(self.zone_path)
            return await s.read_points(details=details)

    async def fetch_indicators(self) -> ItmIndicators:
        """Read the main menu bar's indicators and clock, in a session of its own.

        Returns
        -------
            Whether energy saving, an error and the emergency stop are active.

        """
        async with self._session() as s:
            return s.indicators()

    async def fetch_info(self) -> ItmInfo:
        """Read the iTM's firmware version, regional settings and clock.

        Returns
        -------
            The info; ``clock_offset`` tells how far the iTM's clock is off.

        """
        async with self._session() as s:
            try:
                return await s.read_info()
            finally:
                await s.close_menu()

    async def fetch_changeover_groups(
        self, *, details: bool = False
    ) -> list[ChangeoverGroup]:
        """Read the auto changeover groups.

        Args:
        ----
            details: Also read each group's members, differential and reference
                method (four requests more per group).

        Returns:
        -------
            The groups, in the iTM's order.

        """
        async with self._session() as s:
            try:
                return await s.read_changeover_groups(details=details)
            finally:
                await s.close_menu()

    async def fetch_setback(self) -> SetbackSettings:
        """Read the setback offsets and recovery temperatures."""
        async with self._session() as s:
            try:
                return await s.read_setback()
            finally:
                await s.close_menu()

    async def fetch_overtime_duration(self) -> int | None:
        """Read the overtime timer's duration in minutes (None when not shown)."""
        async with self._session() as s:
            try:
                return await s.read_overtime()
            finally:
                await s.close_menu()

    async def fetch_emergency_stop(self) -> list[EmergencyStopProgram]:
        """Read the emergency stop programs and whether they are armed."""
        async with self._session() as s:
            try:
                return await s.read_emergency_stop()
            finally:
                await s.close_menu()

    async def fetch_schedules(self) -> list[ScheduleSummary]:
        """Read the schedule programs: name, enabled and period."""
        async with self._session() as s:
            try:
                return await s.read_schedules()
            finally:
                await s.close_menu()

    # -- changeover writes --------------------------------------------------------
    async def _changeover_result(self, name: str) -> ChangeoverGroup | None:
        """Return the group as read in a fresh session, or None in a dry run."""
        if self.dry_run:
            return None
        for group in await self.fetch_changeover_groups(details=True):
            if group.name == name:
                return group
        return None

    async def set_changeover_enabled(
        self,
        name: str,
        enabled: bool,  # noqa: FBT001 - positional like the iTM's radios
    ) -> ChangeoverGroup | None:
        """Enable or disable an auto changeover group.

        Args:
        ----
            name: The group's name.
            enabled: Enable (True) or disable the group.

        Returns:
        -------
            The group as read back in a fresh session, or None in a dry run.

        Raises:
        ------
            ItmCommandError: The group is not there, or has no members to enable.

        """
        self._writable()
        async with self._session() as s:
            try:
                await s.set_changeover_enabled(name, enabled=enabled)
            finally:
                await s.close_menu()
        result = await self._changeover_result(name)
        if result is not None and result.enabled != enabled:
            verb = "enable" if enabled else "disable"
            msg = f"{name}: the iTM did not {verb} the group"
            raise ItmCommandError(msg)
        return result

    async def set_changeover_settings(
        self,
        name: str,
        *,
        differential: int | None = None,
        reference: ChangeoverReference | None = None,
    ) -> ChangeoverGroup | None:
        """Change a group's differential (°C) and/or reference method.

        Args:
        ----
            name: The group's name.
            differential: The new differential, one the iTM offers (0-7 °C).
            reference: The new reference method.

        Returns:
        -------
            The group as read back in a fresh session, or None in a dry run.

        """
        self._writable()
        if differential is None and reference is None:
            return await self._changeover_result(name)
        async with self._session() as s:
            try:
                await s.set_changeover_settings(
                    name, differential=differential, reference=reference
                )
            finally:
                await s.close_menu()
        result = await self._changeover_result(name)
        if result is not None and (
            (differential is not None and result.differential != differential)
            or (reference is not None and result.reference != reference)
        ):
            msg = f"{name}: the iTM did not take the settings: {result}"
            raise ItmCommandError(msg)
        return result

    async def create_changeover_group(
        self,
        name: str,
        *,
        members: tuple[str, ...] = (),
        differential: int | None = None,
        reference: ChangeoverReference | None = None,
    ) -> ChangeoverGroup | None:
        """Create an auto changeover group (disabled), with members and settings.

        Each step is its own session and confirmed before the next.

        Args:
        ----
            name: The new group's name.
            members: The units to put in it.
            differential: Its differential, when not the iTM's default.
            reference: Its reference method, when not the iTM's default.

        Returns:
        -------
            The group as read back, or None in a dry run.

        """
        self._writable()
        async with self._session() as s:
            try:
                await s.create_changeover_group(name)
            finally:
                await s.close_menu()
        if self.dry_run:
            return None
        if await self._changeover_result(name) is None:
            msg = f"{name}: the iTM did not create the group"
            raise ItmCommandError(msg)
        if members:
            await self.set_changeover_members(name, members)
        if differential is not None or reference is not None:
            await self.set_changeover_settings(
                name, differential=differential, reference=reference
            )
        return await self._changeover_result(name)

    async def set_changeover_members(
        self, name: str, members: tuple[str, ...]
    ) -> ChangeoverGroup | None:
        """Make a group's members exactly these units.

        Args:
        ----
            name: The group's name.
            members: The units; others are removed. A unit in another group is
                refused.

        Returns:
        -------
            The group as read back in a fresh session, or None in a dry run.

        """
        self._writable()
        async with self._session() as s:
            try:
                await s.set_changeover_members(name, tuple(members))
            finally:
                await s.close_menu()
        result = await self._changeover_result(name)
        if result is not None and list(result.members) != list(dict.fromkeys(members)):
            msg = f"{name}: the iTM did not take the members: {result.members}"
            raise ItmCommandError(msg)
        return result

    async def delete_changeover_group(self, name: str) -> None:
        """Delete a disabled auto changeover group.

        Args:
        ----
            name: The group's name.

        Raises:
        ------
            ItmCommandError: The group is enabled, not there, or still there after.

        """
        self._writable()
        async with self._session() as s:
            try:
                await s.delete_changeover_group(name)
            finally:
                await s.close_menu()
        if not self.dry_run and await self._changeover_result(name) is not None:
            msg = f"{name}: the iTM did not delete the group"
            raise ItmCommandError(msg)

    async def set_setback(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        *,
        high_up: int | None = None,
        high_back: int | None = None,
        low_up: int | None = None,
        low_back: int | None = None,
        recovery_cool: int | None = None,
        recovery_heat: int | None = None,
    ) -> SetbackSettings | None:
        """Change the setback offsets and/or recovery temperatures (°C).

        Args:
        ----
            high_up: Setback high, the relative setpoint shift.
            high_back: Setback high, the relative setback setpoint.
            low_up: Setback low, the relative setpoint shift.
            low_back: Setback low, the relative setback setpoint.
            recovery_cool: The recovery temperature for cooling.
            recovery_heat: The recovery temperature for heating.

        Returns:
        -------
            The settings as read back in a fresh session, or None in a dry run.

        """
        self._writable()
        changes = {
            k: v
            for k, v in {
                "high_up": high_up,
                "high_back": high_back,
                "low_up": low_up,
                "low_back": low_back,
                "recovery_cool": recovery_cool,
                "recovery_heat": recovery_heat,
            }.items()
            if v is not None
        }
        if not changes:
            return None if self.dry_run else await self.fetch_setback()
        async with self._session() as s:
            try:
                await s.set_setback(changes)
            finally:
                await s.close_menu()
        if self.dry_run:
            return None
        result = await self.fetch_setback()
        wrong = {
            k: getattr(result, k) for k, v in changes.items() if getattr(result, k) != v
        }
        if wrong:
            msg = f"setback: the iTM did not take {wrong}"
            raise ItmCommandError(msg)
        return result

    async def set_overtime_duration(self, minutes: int) -> int | None:
        """Change the overtime timer's duration.

        Args:
        ----
            minutes: One of the durations the iTM offers (30 to 180, step 30).

        Returns:
        -------
            The duration as read back in a fresh session, or None in a dry run.

        """
        self._writable()
        async with self._session() as s:
            try:
                await s.set_overtime(minutes)
            finally:
                await s.close_menu()
        if self.dry_run:
            return None
        result = await self.fetch_overtime_duration()
        if result != minutes:
            msg = f"overtime: the iTM shows {result} min, not {minutes}"
            raise ItmCommandError(msg)
        return result

    async def set_schedule_enabled(
        self, name: str, *, enabled: bool
    ) -> ScheduleSummary | None:
        """Enable or disable a schedule program.

        Args:
        ----
            name: The program's name.
            enabled: Enable or disable it.

        Returns:
        -------
            The program as read back in a fresh session, or None in a dry run.

        """
        self._writable()
        async with self._session() as s:
            try:
                await s.set_schedule_enabled(name, enabled=enabled)
            finally:
                await s.close_menu()
        if self.dry_run:
            return None
        program = next(
            (p for p in await self.fetch_schedules() if p.name == name), None
        )
        if program is None or program.enabled != enabled:
            msg = f"{name}: the iTM did not {'enable' if enabled else 'disable'} it"
            raise ItmCommandError(msg)
        return program

    async def create_schedule(self, name: str) -> ScheduleSummary | None:
        """Create an empty, disabled schedule program.

        Args:
        ----
            name: The new program's name.

        Returns:
        -------
            The program as read back in a fresh session, or None in a dry run.

        """
        self._writable()
        async with self._session() as s:
            try:
                await s.create_schedule(name)
            finally:
                await s.close_menu()
        if self.dry_run:
            return None
        program = next(
            (p for p in await self.fetch_schedules() if p.name == name), None
        )
        if program is None:
            msg = f"{name}: the iTM did not create the program"
            raise ItmCommandError(msg)
        return program

    async def delete_schedule(self, name: str) -> None:
        """Delete a schedule program.

        Args:
        ----
            name: The program's name.

        Raises:
        ------
            ItmCommandError: The program is not there, or still there after.

        """
        self._writable()
        async with self._session() as s:
            try:
                await s.delete_schedule(name)
            finally:
                await s.close_menu()
        if not self.dry_run and any(
            p.name == name for p in await self.fetch_schedules()
        ):
            msg = f"{name}: the iTM did not delete the program"
            raise ItmCommandError(msg)

    async def set_unit_settings(  # noqa: PLR0913  # pylint: disable=too-many-arguments,too-many-locals
        self,
        unit: str,
        *,
        start_stop_lock: RemoteLock | None = None,
        mode_lock: RemoteLock | None = None,
        setpoint_lock: RemoteLock | None = None,
        overtime: bool | None = None,
        cool_limit: TempLimit | None = None,
        heat_limit: TempLimit | None = None,
        airflow_direction: str | None = None,
        ventilation_mode: str | None = None,
        ventilation_amount: str | None = None,
        reset_filter_sign: bool = False,
        reset_dust_alarm: bool = False,
        force_dust_alarm_reset: bool = False,
        low_noise: str | None = None,
        outdoor_low_noise: str | None = None,
        capacity: int | None = None,
        reheat: bool | None = None,
        leaving_water_cool: float | None = None,
        leaving_water_heat: float | None = None,
        storage_water: float | None = None,
        output_repeat: bool | None = None,
        output_repeat_interval: int | None = None,
        analog_output: float | None = None,
        multistate_output: str | None = None,
    ) -> UnitSettings | None:
        """Change a unit's remote locks, overtime timer, limits and airflow direction.

        Through the unit's settings dialog, like apply(); the result is read back
        with fetch_unit_settings.

        Args:
        ----
            unit: The unit's name.
            start_stop_lock: allowed / stop_only / forbidden.
            mode_lock: allowed / forbidden.
            setpoint_lock: allowed / forbidden.
            overtime: Switch the unit's overtime timer on or off.
            cool_limit: Enable a cooling setpoint limit (with min/max) or disable it.
            heat_limit: Enable a heating setpoint limit (with min/max) or disable it.
            airflow_direction: position_0 (horizontal) .. position_4 (down), or
                swing; only on units that offer it (see AIRFLOW_DIRECTIONS).
            ventilation_mode: A ventilation unit's mode, as one of the labels in
                UnitSettings.ventilation_modes.
            ventilation_amount: Its air volume, one of ventilation_amounts.
            reset_filter_sign: Reset the filter sign (offered while it shows).
            reset_dust_alarm: Reset the dust alarm (offered while it shows).
            force_dust_alarm_reset: Force the dust alarm reset.
            low_noise: Low noise, one of UnitSettings.low_noise_options.
            outdoor_low_noise: The outdoor unit's low noise, one of its options.
            capacity: The outdoor unit's capacity limit in %, one of
                capacity_options.
            reheat: Reheat on or off.
            leaving_water_cool: Leaving water setpoint for cooling.
            leaving_water_heat: Leaving water setpoint for heating.
            storage_water: Storage water setpoint.
            output_repeat: A digital output's repeat mode on or off.
            output_repeat_interval: Its interval in minutes, one of
                output_repeat_interval_options.
            analog_output: An analog output's value.
            multistate_output: A multistate output's state, one of its options.

        The last eleven are offered only by some units (see DIALOG_SETTINGS); one the
        unit doesn't offer is refused before anything in the dialog is clicked.

        Returns:
        -------
            The unit's settings as read back, or None in a dry run or when nothing
            was asked.

        """
        changes: dict[str, Any] = {
            "start_stop_lock": start_stop_lock,
            "mode_lock": mode_lock,
            "setpoint_lock": setpoint_lock,
            "overtime": overtime,
            "cool_limit": cool_limit,
            "heat_limit": heat_limit,
            "airflow_direction": airflow_direction,
            "ventilation_mode": ventilation_mode,
            "ventilation_amount": ventilation_amount,
            "reset_filter_sign": reset_filter_sign or None,
            "reset_dust_alarm": reset_dust_alarm or None,
            "force_dust_alarm_reset": force_dust_alarm_reset or None,
            "low_noise": low_noise,
            "outdoor_low_noise": outdoor_low_noise,
            "capacity": capacity,
            "reheat": reheat,
            "leaving_water_cool": leaving_water_cool,
            "leaving_water_heat": leaving_water_heat,
            "storage_water": storage_water,
            "output_repeat": output_repeat,
            "output_repeat_interval": output_repeat_interval,
            "analog_output": analog_output,
            "multistate_output": multistate_output,
        }
        changes = {k: v for k, v in changes.items() if v is not None}
        if (
            airflow_direction is not None
            and airflow_direction not in AIRFLOW_DIRECTIONS
        ):
            msg = f"airflow direction must be one of {AIRFLOW_DIRECTIONS}"
            raise ItmCommandError(msg)
        if not changes:
            return None
        self._writable()
        experimental = sorted(changes.keys() & _EXPERIMENTAL_UNIT_SETTINGS)
        if experimental:
            self._experimental(f"{unit}: set {', '.join(experimental)}")
        async with self._unit_lock(unit):
            await self._settle(unit)
            async with self._write(unit) as s:
                await s.change_unit_settings(changes)
        if self.dry_run:
            return None
        # Like the other unit writes, the change takes a few seconds to reach the
        # unit over the Daikin bus: read again until it shows.
        deadline = time.monotonic() + self.confirm_timeout
        while True:
            await asyncio.sleep(self.confirm_interval)
            result = await self.fetch_unit_settings(unit)
            wrong = _settings_mismatch(result, changes)
            if not wrong:
                return result
            if time.monotonic() >= deadline:
                msg = f"{unit}: the iTM did not take {wrong}"
                raise ItmCommandError(msg)

    async def set_power_state(
        self, unit: str, state: PowerState, *, confirm: bool = True
    ) -> Unit | None:
        """Put a unit on, off, or in setback low/high, through the operation panel.

        Args:
        ----
            unit: The unit's name.
            state: on / off / setback_low / setback_high.
            confirm: Wait until the change shows on the unit.

        Returns:
        -------
            The unit as read back, or None when not confirmed or in a dry run.

        Raises:
        ------
            ItmCommandError: The unit doesn't offer that state.

        """
        self._writable()
        async with self._unit_lock(unit):
            await self._settle(unit)
            async with self._write(unit) as s:
                current = unit_from_panel(s.state)
                options = s.state.get(ScreenID.OPERATION, P_POWER, "COMBO_STRS") or []
                order = {
                    2: (PowerState.ON, PowerState.OFF),
                    4: (
                        PowerState.ON,
                        PowerState.SETBACK_LOW,
                        PowerState.SETBACK_HIGH,
                        PowerState.OFF,
                    ),
                }.get(len(options), ())
                if state not in order:
                    msg = f"{unit}: {state} not offered (options {options})"
                    raise ItmCommandError(msg)
                if current.power_state == state:
                    return current
                await s.choose(P_POWER, order.index(state), confirm=True)
                await s.commit(self.commit_delay)
            return await self._finish(
                unit,
                f"power {state}",
                lambda u: u.power_state == state,
                confirm=confirm,
            )

    async def fetch_schedule(
        self, name: str, *, events: bool = True, details: bool = False
    ) -> ScheduleProgram:
        """Read a schedule program through its editor.

        Properties, weekly and special day patterns, the calendar and, with events,
        every weekday's and special day's events as the editor lists them, their
        settings parsed from the text. The editor is left with Cancel; nothing is
        saved.

        Args:
        ----
            name: The program's name, see fetch_schedules.
            events: Read the event lists (two requests per day with events).
            details: Also open each event's own settings screen (two requests per
                event, so many for a program with many events), for the
                settings the text doesn't carry: fan speed, overtime, relative
                setpoints.

        Returns:
        -------
            The program.

        """
        async with self._session() as s:
            try:
                return await s.read_schedule(name, events=events, details=details)
            finally:
                await s.close_menu()

    async def fetch_schedule_day(
        self, name: str, *, days_ahead: int = 0
    ) -> ScheduleDay:
        """Read what a program does on a day (today, or days ahead): three requests.

        Args:
        ----
            name: The program's name.
            days_ahead: 0 for the iTM's current day, 1 for tomorrow, and so on.

        Returns:
        -------
            The day's events, as the overview lists them.

        """
        async with self._session() as s:
            try:
                return await s.read_schedule_day(name, days_ahead=days_ahead)
            finally:
                await s.close_menu()

    async def fetch_history(
        self,
        *,
        day: date | None = None,
        include_management: bool = False,
        pages: int = 1,
    ) -> list[History]:
        """Read the iTM's history (who changed what, when).

        Args:
        ----
            day: The day to read; the iTM's current day when omitted.
            include_management: Also list logins and logouts (the "management"
                class), which are most of the history.
            pages: How many pages of 100 entries to read, newest first.

        Returns:
        -------
            The pages read.

        """
        async with self._session() as s:
            try:
                await s.open_menu()
                return await s.read_history(
                    day=day, include_management=include_management, pages=pages
                )
            finally:
                await s.close_menu()

    async def fetch_optional_functions(self) -> dict[str, OptionalFunction]:
        """Probe the functions the menu hides and list what they hold (experimental).

        Returns
        -------
            By function name: whether its screen opened and the rows it listed.

        """
        async with self._session() as s:
            try:
                return await s.read_optional_functions()
            finally:
                await s.close_menu()

    async def fetch_system(self, only: Iterable[str] | None = None) -> SystemSettings:
        """Read the system settings screens, in one session.

        Zones, management points, maintenance, network, e-mail, web users, the
        touch panel's hardware and screensaver, confirm dialogs, remote management,
        PPD and the energy navigator. About thirty requests.

        Args:
        ----
            only: The SystemSettings fields to read; the others stay None.

        Returns:
        -------
            The settings; None for each screen that did not open.

        """
        wanted = None if only is None else frozenset(only)
        async with self._session() as s:
            try:
                return await s.read_system(wanted)
            finally:
                await s.close_menu()

    async def set_hardware(
        self,
        *,
        backlight: int | None = None,
        buzzer_volume: int | None = None,
        buzzer_minutes: int | None = None,
        buzzer_continuous: bool = False,
        key_click_volume: int | None = None,
    ) -> HardwareSettings | None:
        """Change the touch panel's backlight, buzzer and key click settings.

        Args:
        ----
            backlight: Level 1-8.
            buzzer_volume: 0-5.
            buzzer_minutes: How long the buzzer sounds: 1, 3 or 5.
            buzzer_continuous: Let the buzzer sound until it is stopped.
            key_click_volume: 0-5.

        Returns:
        -------
            The settings as read back in a fresh session, or None in a dry run.

        """
        changes: dict[str, Any] = {
            k: v
            for k, v in (
                ("backlight", backlight),
                ("buzzer_volume", buzzer_volume),
                ("buzzer_minutes", buzzer_minutes),
                ("key_click_volume", key_click_volume),
            )
            if v is not None
        }
        if buzzer_continuous:
            changes["buzzer_minutes"] = None
        if not changes:
            msg = "nothing to change"
            raise ItmCommandError(msg)
        self._writable()
        async with self._session() as s:
            try:
                await s.set_hardware(changes)
            finally:
                await s.close_menu()
        if self.dry_run:
            return None
        result = (await self.fetch_system(["hardware"])).hardware
        wrong = {
            k: v
            for k, v in changes.items()
            if result is None or getattr(result, k) != v
        }
        if wrong:
            msg = f"the iTM did not take {wrong}: {result}"
            raise ItmCommandError(msg)
        return result

    async def set_screensaver(
        self,
        *,
        mode: ScreensaverMode | None = None,
        delay_minutes: int | None = None,
        off_on_error: bool | None = None,
    ) -> ScreensaverSettings | None:
        """Change the touch panel's screensaver.

        Args:
        ----
            mode: What the panel does after the delay.
            delay_minutes: The delay, within the range the iTM's input allows.
            off_on_error: End the screensaver when an error occurs.

        Returns:
        -------
            The settings as read back in a fresh session, or None in a dry run.

        """
        changes: dict[str, Any] = {
            k: v
            for k, v in (
                ("mode", mode),
                ("delay_minutes", delay_minutes),
                ("off_on_error", off_on_error),
            )
            if v is not None
        }
        if not changes:
            msg = "nothing to change"
            raise ItmCommandError(msg)
        self._writable()
        async with self._session() as s:
            try:
                await s.set_screensaver(changes)
            finally:
                await s.close_menu()
        if self.dry_run:
            return None
        result = (await self.fetch_system(["screensaver"])).screensaver
        wrong = {
            k: v
            for k, v in changes.items()
            if result is None or getattr(result, k) != v
        }
        if wrong:
            msg = f"the iTM did not take {wrong}: {result}"
            raise ItmCommandError(msg)
        return result

    async def _in_session(self, call: Callable[[_Session], Awaitable[_T]]) -> _T:
        """Run one session call and close the menu after it."""
        async with self._session() as s:
            try:
                return await call(s)
            finally:
                await s.close_menu()

    async def _system_write(self, call: Callable[[_Session], Awaitable[None]]) -> None:
        self._writable()
        await self._in_session(call)

    async def set_network(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        *,
        controller_name: str | None = None,
        hostname: str | None = None,
        ip_address: str | None = None,
        subnet_mask: str | None = None,
        gateway: str | None = None,
        dns_primary: str | None = None,
        dns_secondary: str | None = None,
        allow_network_change: bool = False,
    ) -> NetworkSettings | None:
        """Change the iTM's own network settings.

        A wrong address, mask or gateway takes the iTM off the network, and only
        someone at the device can put it back. So unless allow_network_change is
        True, only a dry run is allowed; a dry run opens each input to check it and
        cancels it, without entering anything.

        Args:
        ----
            controller_name: The controller's name.
            hostname: Its host name.
            ip_address: Its IPv4 address.
            subnet_mask: The subnet mask.
            gateway: The default gateway.
            dns_primary: The primary DNS server.
            dns_secondary: The secondary DNS server.
            allow_network_change: Really change it (not just a dry run).

        Returns:
        -------
            The settings as read back (the iTM may be unreachable after a change of
            address), or None in a dry run.

        """
        self._experimental("change the network settings")
        changes = {
            k: v
            for k, v in (
                ("controller_name", controller_name),
                ("hostname", hostname),
                ("ip_address", ip_address),
                ("subnet_mask", subnet_mask),
                ("gateway", gateway),
                ("dns_primary", dns_primary),
                ("dns_secondary", dns_secondary),
            )
            if v is not None
        }
        if not changes:
            msg = "nothing to change"
            raise ItmCommandError(msg)
        for key, value in changes.items():
            if key not in NW_TEXT_FIELDS:
                try:
                    ipaddress.IPv4Address(value)
                except ValueError as err:
                    msg = f"{key}: {value!r} is not an IPv4 address"
                    raise ItmCommandError(msg) from err
        if not self.dry_run and not allow_network_change:
            msg = (
                "a network change can strand the iTM; use a dry run, or pass "
                "allow_network_change=True"
            )
            raise ItmCommandError(msg)
        await self._system_write(lambda s: s.set_network(changes))
        if self.dry_run:
            return None
        return (await self.fetch_system(["network"])).network

    async def fetch_service_settings(self, password: str) -> ServiceSettings:
        """Read the installer's service screens, after a service login.

        Read only: time zone, changeover option per unit, contact, R32 alarm
        settings, central monitoring, the number of licence keys, dealer options and
        the energy save mode. The service password is the installer's (not a web
        user's); it is sent once and never stored or logged.

        Args:
        ----
            password: The service password.

        Returns:
        -------
            The settings; None for each screen that did not open.

        """

        async def read(s: _Session) -> ServiceSettings:
            await s.service_login(password)
            return await s.read_service()

        return await self._in_session(read)

    # -- energy navigator -----------------------------------------------------------
    async def fetch_energy_navigator(
        self, only: Iterable[str] | None = None, *, service_password: str | None = None
    ) -> EnergyNavigator:
        """Read the energy navigator's screens, in one session. Read only.

        The budget overview (every tab), the planned and the actual consumption of
        the energy type shown, the management rule names and the sampling targets.
        Every screen is left with Close or Cancel; nothing is saved.

        Args:
        ----
            only: The EnergyNavigator fields to read; the others stay None. None
                reads the ones above; energy_types, energy_groups, details and
                rule_editor, rule_settings and installer (tested against the
                simulated iTM only) when named.
            service_password: The service password, for installer (behind the
                service login); sent once, never stored or logged.

        Returns:
        -------
            What was read; None for each part whose menu screen did not open.

        """
        wanted = energy_navigator.wanted_parts(only)
        if "installer" in wanted and service_password is None:
            msg = "the installer's settings need service_password"
            raise ItmCommandError(msg)
        return await self._in_session(
            lambda s: energy_navigator.read_energy_navigator(
                s, wanted, service_password
            )
        )

    # -- energy navigator writes -----------------------------------------------------
    async def set_planned_consumption(  # pylint: disable=too-many-arguments
        self,
        *,
        year: str | None = None,
        energy_type: str | None = None,
        ratio: float | None = None,
        per_month: bool | None = None,
        months: Mapping[str, float] | None = None,
    ) -> PlannedYear | None:
        """Change a fiscal year's planned consumption (3202) and OK.

        Args:
        ----
            year: The fiscal year's tab, as shown; None for the first.
            energy_type: The energy type; None for the one the screen shows first.
            ratio: The ratio to the previous year (%).
            per_month: Targets per month (True) or for the whole year (False).
            months: Targets per month, by the month as listed.

        Returns:
        -------
            The fiscal year as read back in a fresh session; None in a dry run.

        """
        self._experimental("set the energy navigator's planned consumption")
        change = energy_edit.PlannedChange(
            year, energy_type, ratio, per_month, dict(months or {})
        )
        await self._system_write(lambda s: _ignore(energy_edit.set_planned(s, change)))
        if self.dry_run:
            return None
        return await self._in_session(lambda s: energy_edit.confirm_planned(s, change))

    async def set_actual_consumption(
        self,
        *,
        year: str | None = None,
        energy_type: str | None = None,
        co2_factor: float | None = None,
        months: Mapping[str, float] | None = None,
    ) -> ActualYear | None:
        """Enter a fiscal year's actual consumption (3203) and OK.

        Args:
        ----
            year: The fiscal year's tab, as shown; None for the first.
            energy_type: The energy type; None for the one the screen shows first.
            co2_factor: The CO2 factor.
            months: Values per month, by the month as listed.

        Returns:
        -------
            The fiscal year as read back in a fresh session; None in a dry run.

        """
        self._experimental("set the energy navigator's actual consumption")
        change = energy_edit.ActualChange(
            year, energy_type, co2_factor, dict(months or {})
        )
        await self._system_write(lambda s: _ignore(energy_edit.set_actual(s, change)))
        if self.dry_run:
            return None
        return await self._in_session(lambda s: energy_edit.confirm_actual(s, change))

    async def set_energy_sampling(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        target: str | None = None,
    ) -> tuple[str | None, str | None, str | None] | None:
        """Change the equipment management's sampling period and target (3210).

        Args:
        ----
            start: The first day sampled.
            end: The last day sampled.
            target: The sampling target, by its name in the target list (3218).

        Returns:
        -------
            From, to and target as read back; None in a dry run.

        """
        self._experimental("set the energy navigator's sampling")
        await self._system_write(
            lambda s: _ignore(
                energy_edit.set_sampling(s, start=start, end=end, target=target)
            )
        )
        if self.dry_run:
            return None
        return await self._in_session(
            lambda s: energy_edit.confirm_sampling(
                s, start=start, end=end, target=target
            )
        )

    async def _energy_names(self, *, group: bool) -> tuple[str, ...]:
        return await self._in_session(lambda s: energy_edit.read_names(s, group=group))

    async def create_energy_type(self, name: str, *, group: bool = False) -> None:
        """Create an energy type (3205), or with group=True an energy group (3207).

        Args:
        ----
            name: The new name.
            group: Work on the energy groups instead.

        """
        await self._system_write(
            lambda s: _ignore(energy_edit.create_entry(s, name, group=group))
        )
        if not self.dry_run and name not in await self._energy_names(group=group):
            msg = f"{name}: the iTM did not create it"
            raise ItmCommandError(msg)

    async def rename_energy_type(
        self, name: str, new_name: str, *, group: bool = False
    ) -> None:
        """Rename an energy type, or with group=True an energy group.

        Args:
        ----
            name: The current name.
            new_name: The new name.
            group: Work on the energy groups instead.

        """
        if group:
            self._experimental("rename an energy group")
        await self._system_write(
            lambda s: _ignore(energy_edit.rename_entry(s, name, new_name, group=group))
        )
        if self.dry_run:
            return
        names = await self._energy_names(group=group)
        if new_name not in names or name in names:
            msg = f"{name}: the iTM did not rename it to {new_name}"
            raise ItmCommandError(msg)

    async def delete_energy_type(self, name: str, *, group: bool = False) -> None:
        """Delete an energy type, or with group=True an energy group.

        Args:
        ----
            name: The name.
            group: Work on the energy groups instead.

        """
        await self._system_write(
            lambda s: _ignore(energy_edit.delete_entry(s, name, group=group))
        )
        if not self.dry_run and name in await self._energy_names(group=group):
            msg = f"{name}: the iTM did not delete it"
            raise ItmCommandError(msg)

    # -- energy navigator rules and installer settings ------------------------------
    async def _rule_names(self) -> tuple[str, ...]:
        return await self._in_session(energy_navigator.read_rules) or ()

    async def create_energy_rule(self, name: str) -> None:
        """Create a management rule (3213), confirmed by the rule list."""
        await self._system_write(lambda s: _ignore(energy_edit.create_rule(s, name)))
        if not self.dry_run and name.strip() not in await self._rule_names():
            msg = f"rule {name}: the iTM did not create it"
            raise ItmCommandError(msg)

    async def rename_energy_rule(self, name: str, new_name: str) -> None:
        """Rename a management rule, confirmed by the rule list."""
        self._experimental("rename an energy navigator rule")
        await self._system_write(
            lambda s: _ignore(energy_edit.rename_rule(s, name, new_name))
        )
        if self.dry_run:
            return
        names = await self._rule_names()
        if new_name.strip() not in names or name.strip() in names:
            msg = f"rule {name}: the iTM did not rename it to {new_name}"
            raise ItmCommandError(msg)

    async def delete_energy_rule(self, name: str) -> None:
        """Delete a management rule, confirmed by the rule list."""
        await self._system_write(lambda s: _ignore(energy_edit.delete_rule(s, name)))
        if not self.dry_run and name.strip() in await self._rule_names():
            msg = f"rule {name}: the iTM did not delete it"
            raise ItmCommandError(msg)

    async def copy_energy_rule(
        self, name: str, targets: Iterable[str], *, calendar: bool = False
    ) -> None:
        """Copy a management rule (3215), or its calendar (3216), to other rules.

        Args:
        ----
            name: The rule to copy.
            targets: The rules to copy to, as listed.
            calendar: Copy only the calendar.

        Raises:
        ------
            ItmCommandError: A target's editor doesn't show the copy afterwards.

        """
        self._experimental("copy an energy navigator rule")
        wanted = tuple(t.strip() for t in targets)
        await self._system_write(
            lambda s: _ignore(energy_edit.copy_rule(s, name, wanted, calendar=calendar))
        )
        if self.dry_run:
            return
        rules = {
            r.name: r
            for r in await self._in_session(energy_navigator.read_rule_editor) or ()
        }
        source = rules.get(name.strip())
        for target in wanted:
            got = rules.get(target)
            same = (
                source is not None
                and got is not None
                and (
                    got.calendar == source.calendar
                    if calendar
                    else (got.weekdays, got.special_days)
                    == (source.weekdays, source.special_days)
                )
            )
            if not same:
                msg = f"rule {target}: the iTM does not show the copy of {name}"
                raise ItmCommandError(msg)

    async def set_energy_rule_day(
        self, rule: str, day: str, ranges: Iterable[RuleRange]
    ) -> RuleDay | None:
        """Make a management rule's day hold exactly these time ranges (3217).

        Args:
        ----
            rule: The rule, as listed.
            day: A weekday ("sunday" .. "saturday") or a special day's name.
            ranges: The time ranges, with the setpoints they manage.

        Returns:
        -------
            The day as read back in a fresh session; None in a dry run.

        """
        wanted = tuple(ranges)
        if any(r.cool is not None or r.heat is not None for r in wanted):
            self._experimental("set an energy navigator rule's setpoints")
        await self._system_write(
            lambda s: _ignore(energy_edit.set_rule_day(s, rule, day, wanted))
        )
        if self.dry_run:
            return None
        shown = await self._in_session(
            lambda s: energy_navigator.read_rule_day(s, rule, day)
        )
        if not energy_edit.same_ranges(shown.ranges, wanted):
            msg = f"rule {rule} {day}: the iTM shows {shown.ranges}"
            raise ItmCommandError(msg)
        return shown

    async def _installer_write(
        self,
        password: str,
        call: Callable[[_Session], Awaitable[bool]],
        *,
        allow_service_change: bool,
    ) -> EnergyInstallerSettings | None:
        self._writable()
        energy_edit.check_service_change(
            dry_run=self.dry_run, allow_service_change=allow_service_change
        )
        await self._in_session(lambda s: _ignore(call(s)))
        if self.dry_run:
            return None
        return await self._in_session(lambda s: energy_edit.read_installer(s, password))

    async def set_energy_room_temperature(
        self,
        password: str,
        point: str,
        *,
        analog_point: str | None = None,
        allow_service_change: bool = False,
    ) -> RoomTemperaturePoint | None:
        """Set a point's room temperature source for the energy navigator (3221).

        Behind the service login; refused unless allow_service_change (or a dry
        run). The password is sent once, never stored or logged.

        Args:
        ----
            password: The service password.
            point: The point, as listed.
            analog_point: An analog input to take it from; None for the unit's
                return air temperature.
            allow_service_change: Really change the installer setting.

        Returns:
        -------
            The point as read back; None in a dry run.

        """
        self._experimental("set the energy navigator's room temperature point")
        settings = await self._installer_write(
            password,
            lambda s: energy_edit.set_room_temperature(
                s, password, point, analog_point
            ),
            allow_service_change=allow_service_change,
        )
        if settings is None:
            return None
        found = [p for p in settings.room_temperature or () if p.name == point.strip()]
        analog = None if analog_point is None else analog_point.strip()
        if (
            not found
            or found[0].own_sensor is not (analog is None)
            or found[0].analog_point != analog
        ):
            msg = f"{point}: the iTM shows {found}"
            raise ItmCommandError(msg)
        return found[0]

    async def set_energy_collection_start(
        self, password: str, month: str, *, allow_service_change: bool = False
    ) -> str | None:
        """Set the month the energy navigator starts collecting data (3223).

        Behind the service login; refused unless allow_service_change (or a dry
        run).

        Args:
        ----
            password: The service password.
            month: The month, as the iTM lists it.
            allow_service_change: Really change the installer setting.

        Returns:
        -------
            The month as read back; None in a dry run.

        """
        self._experimental("set the energy navigator's collection start")
        settings = await self._installer_write(
            password,
            lambda s: energy_edit.set_collection_start(s, password, month),
            allow_service_change=allow_service_change,
        )
        if settings is None:
            return None
        if settings.collection_start != month.strip():
            msg = f"collection start: the iTM shows {settings.collection_start}"
            raise ItmCommandError(msg)
        return settings.collection_start

    async def set_energy_conversion(
        self,
        password: str,
        change: ConversionType,
        *,
        allow_service_change: bool = False,
    ) -> ConversionType | None:
        """Change the energy conversion's name and unit texts (3224).

        Behind the service login; refused unless allow_service_change (or a dry
        run).

        Args:
        ----
            password: The service password.
            change: The texts to set; None keeps one.
            allow_service_change: Really change the installer setting.

        Returns:
        -------
            The conversion as read back; None in a dry run.

        """
        self._experimental("set the energy navigator's energy conversion")
        settings = await self._installer_write(
            password,
            lambda s: energy_edit.set_conversion(s, password, change),
            allow_service_change=allow_service_change,
        )
        if settings is None:
            return None
        shown = settings.conversion or energy_navigator.ConversionType()
        for key in ("name", "one", "thousand", "million"):
            value = getattr(change, key)
            if value is not None and getattr(shown, key) != value.strip():
                msg = f"conversion {key}: the iTM shows {getattr(shown, key)!r}"
                raise ItmCommandError(msg)
        return shown

    # -- zone structure and icons -----------------------------------------------------
    async def _zone_list(self) -> tuple[ZoneInfo, ...]:
        return (await self.fetch_system(["zones"])).zones or ()

    async def create_zone(
        self, name: str, *, parent: str | None = None
    ) -> ZoneInfo | None:
        """Add a zone (empty), at the top level or below a parent zone.

        Args:
        ----
            name: The new zone's name (1-12 characters).
            parent: The zone to add it below; None for the top level.

        Returns:
        -------
            The zone as the zone list shows it, or None in a dry run.

        """
        await self._system_write(
            lambda s: _ignore(zones_edit.add_zone(s, name, parent=parent))
        )
        if self.dry_run:
            return None
        zone = next((z for z in await self._zone_list() if z.name == name), None)
        if zone is None:
            msg = f"{name}: the iTM did not add the zone"
            raise ItmCommandError(msg)
        return zone

    async def delete_zone(self, name: str) -> None:
        """Delete an empty zone (one without zones or points in it).

        Args:
        ----
            name: The zone.

        """
        await self._system_write(lambda s: _ignore(zones_edit.delete_zone(s, name)))
        if not self.dry_run and any(z.name == name for z in await self._zone_list()):
            msg = f"{name}: the iTM did not delete the zone"
            raise ItmCommandError(msg)

    async def move_zone(self, name: str, parent: str) -> ZoneInfo | None:
        """Move a zone below another zone (or to the top). Its zone paths change.

        Args:
        ----
            name: The zone to move.
            parent: Where to: a zone, or the root's name for the top level.

        Returns:
        -------
            The zone as the zone list shows it, or None in a dry run.

        """
        await self._system_write(
            lambda s: _ignore(zones_edit.move_zone(s, name, parent))
        )
        if self.dry_run:
            return None
        zones = await self._zone_list()
        zone = next((z for z in zones if z.name == name), None)
        target = next((z for z in zones if z.name == parent), None)
        want = None if target is None else (target.level or 0) + 1
        if zone is None or (want is not None and zone.level != want):
            msg = f"{name}: the iTM did not move the zone below {parent}"
            raise ItmCommandError(msg)
        return zone

    async def fetch_zone_members(self, zone: str) -> tuple[str, ...]:
        """Read the management points in a zone, from its member screen.

        Args:
        ----
            zone: The zone.

        Returns:
        -------
            The points' names.

        """
        return await self._in_session(lambda s: zones_edit.read_zone_members(s, zone))

    async def set_zone_members(
        self, zone: str, members: Iterable[str]
    ) -> tuple[str, ...] | None:
        """Make a zone's management points exactly these.

        Args:
        ----
            zone: The zone.
            members: The points' names; the others are taken out of the zone.

        Returns:
        -------
            The members as read back, or None in a dry run.

        """
        wanted = tuple(m.strip() for m in members)
        await self._system_write(
            lambda s: _ignore(zones_edit.set_zone_members(s, zone, wanted))
        )
        if self.dry_run:
            return None
        result = await self.fetch_zone_members(zone)
        if set(result) != set(wanted):
            msg = f"{zone}: the members are {result}, not {wanted}"
            raise ItmCommandError(msg)
        return result

    async def set_zone_member_order(
        self, zone: str, order: Iterable[str], *, use_jump: bool = False
    ) -> tuple[str, ...] | None:
        """Put a zone's members in this order (the order the iTM lists them in).

        Args:
        ----
            zone: The zone.
            order: Member names, first to last; members not named follow them, in
                their current order.
            use_jump: Move each member to its row with the list's Jump (one
                step, 1602) instead of the up button (tested against the
                simulated iTM only).

        Returns:
        -------
            The members as read back, or None in a dry run.

        """
        if use_jump:
            self._experimental("reorder zone members with Jump")
        wanted = tuple(m.strip() for m in order)
        await self._system_write(
            lambda s: _ignore(
                zones_edit.set_zone_member_order(s, zone, wanted, use_jump=use_jump)
            )
        )
        if self.dry_run:
            return None
        result = await self.fetch_zone_members(zone)
        if result[: len(wanted)] != wanted:
            msg = f"{zone}: the members are in the order {result}, not {wanted}"
            raise ItmCommandError(msg)
        return result

    async def set_zone_icon(self, zone: str, icon: int) -> ZoneAttributes | None:
        """Give a zone another icon (1000-1009).

        Args:
        ----
            zone: The zone.
            icon: The icon ID.

        Returns:
        -------
            The zone's attributes as read back, or None in a dry run.

        """
        await self._system_write(
            lambda s: _ignore(zones_edit.set_zone_icon(s, zone, icon))
        )
        if self.dry_run:
            return None
        result = await self.fetch_zone_attributes(zone)
        if result.icon != icon:
            msg = f"{zone}: the icon is {result.icon}, not {icon}"
            raise ItmCommandError(msg)
        return result

    async def set_point_icon(self, point: str, icon: int) -> ManagementPoint | None:
        """Give a management point another icon of its own kind.

        Args:
        ----
            point: The point's name.
            icon: The icon ID (one of the same family, e.g. 21xx for indoor units).

        Returns:
        -------
            The point as read back, or None in a dry run.

        """
        await self._system_write(
            lambda s: _ignore(zones_edit.set_point_icon(s, point, icon))
        )
        if self.dry_run:
            return None
        points = (await self.fetch_system(["points"])).points or ()
        result = next((p for p in points if p.name == point), None)
        if result is None or result.icon != icon:
            msg = f"{point}: the icon did not become {icon}: {result}"
            raise ItmCommandError(msg)
        return result

    async def set_maintenance(self, point: str, *, on: bool) -> bool | None:
        """Mark a management point as in maintenance, or end it.

        Args:
        ----
            point: The point's name, e.g. "1:1-09".
            on: Put it in maintenance (True) or take it out.

        Returns:
        -------
            Whether it is in maintenance as read back, or None in a dry run.

        """
        await self._system_write(lambda s: s.set_maintenance(point, on=on))
        if self.dry_run:
            return None
        points = (await self.fetch_system(["maintenance"])).maintenance or ()
        state = next((p.in_maintenance for p in points if p.name == point), None)
        if state is not on:
            msg = f"{point}: the iTM did not take maintenance {on}"
            raise ItmCommandError(msg)
        return state

    async def set_point_attributes(
        self, point: str, *, name: str | None = None, info: str | None = None
    ) -> ManagementPoint | None:
        """Change a management point's description (or name).

        Renaming a point changes how the library and Home Assistant find it.

        Args:
        ----
            point: The point's current name.
            name: Its new name (1-12 characters).
            info: Its new description (0-50 characters), what the CLI shows as name.

        Returns:
        -------
            The point as read back, or None in a dry run.

        """
        if name is None and info is None:
            msg = "nothing to change"
            raise ItmCommandError(msg)
        await self._system_write(
            lambda s: s.set_point_attributes(point, name=name, info=info)
        )
        if self.dry_run:
            return None
        wanted = name or point
        points = (await self.fetch_system(["points"])).points or ()
        result = next((p for p in points if p.name == wanted), None)
        if result is None or (info is not None and (result.info or "") != info):
            msg = f"{point}: the iTM did not take the change: {result}"
            raise ItmCommandError(msg)
        return result

    async def fetch_zone_attributes(self, zone: str) -> ZoneAttributes:
        """Read a zone's name, description and intervals from its settings screen.

        Args:
        ----
            zone: The zone's name as the zone list shows it.

        Returns:
        -------
            The attributes.

        """
        return await self._in_session(lambda s: s.read_zone_attributes(zone))

    async def set_zone_attributes(
        self,
        zone: str,
        *,
        name: str | None = None,
        info: str | None = None,
        start_interval: int | None = None,
        stop_interval: int | None = None,
    ) -> ZoneAttributes | None:
        """Change a zone's description, intervals (or name).

        Renaming a zone changes every zone path that names it.

        Args:
        ----
            zone: The zone's current name.
            name: Its new name (1-12 characters).
            info: Its new description (0-50 characters).
            start_interval: 0-180, as the iTM's input allows.
            stop_interval: 0-180.

        Returns:
        -------
            The zone's attributes as read back, or None in a dry run.

        """
        changes = {
            "name": name,
            "info": info,
            "start_interval": start_interval,
            "stop_interval": stop_interval,
        }
        if all(v is None for v in changes.values()):
            msg = "nothing to change"
            raise ItmCommandError(msg)
        await self._system_write(
            lambda s: s.set_zone_attributes(
                zone,
                name=name,
                info=info,
                start_interval=start_interval,
                stop_interval=stop_interval,
            )
        )
        if self.dry_run:
            return None
        result = await self.fetch_zone_attributes(name or zone)
        wrong = {
            k: v
            for k, v in changes.items()
            if v is not None and _shown(getattr(result, k), k) != v
        }
        if wrong:
            msg = f"{zone}: the iTM did not take {wrong}: {result}"
            raise ItmCommandError(msg)
        return result

    def _not_own_account(self, name: str) -> None:
        if name == self._username:
            msg = "refusing to change the account this client logs in with"
            raise ItmCommandError(msg)

    async def _web_user(self, name: str) -> WebUser | None:
        users = (await self.fetch_system(["web_users"])).web_users or ()
        return next((u for u in users if u.name == name), None)

    async def create_web_user(
        self,
        name: str,
        password: str,
        *,
        zones: tuple[str, ...],
        admin: bool = False,
    ) -> WebUser | None:
        """Add a web access user.

        Args:
        ----
            name: The user name (1-15 characters).
            password: Its password (4-15 characters); never logged.
            zones: The zones the user manages (the iTM wants at least one).
            admin: Administrator (True) or user.

        Returns:
        -------
            The user as read back, or None in a dry run.

        """
        self._not_own_account(name)
        if not zones:
            msg = "a web user needs at least one zone"
            raise ItmCommandError(msg)
        await self._system_write(
            lambda s: s.save_web_user(
                name, new=True, password=password, admin=admin, zones=tuple(zones)
            )
        )
        if self.dry_run:
            return None
        user = await self._web_user(name)
        if user is None or user.admin is not admin:
            msg = f"{name}: the iTM did not add the user: {user}"
            raise ItmCommandError(msg)
        return user

    async def set_web_user(
        self,
        name: str,
        *,
        password: str | None = None,
        admin: bool | None = None,
        zones: tuple[str, ...] | None = None,
    ) -> WebUser | None:
        """Change a web user's password and/or type (never the client's own).

        Args:
        ----
            name: The user.
            password: A new password (4-15 characters); never logged.
            admin: Administrator (True) or user.
            zones: The zones the user manages, replacing the current ones.

        Returns:
        -------
            The user as read back, or None in a dry run.

        """
        self._not_own_account(name)
        if password is None and admin is None and not zones:
            msg = "nothing to change"
            raise ItmCommandError(msg)
        await self._system_write(
            lambda s: s.save_web_user(
                name, new=False, password=password, admin=admin, zones=zones or None
            )
        )
        if self.dry_run:
            return None
        user = await self._web_user(name)
        if user is None or (admin is not None and user.admin is not admin):
            msg = f"{name}: the iTM did not take the change: {user}"
            raise ItmCommandError(msg)
        return user

    async def delete_web_user(self, name: str) -> None:
        """Delete a web user (never the client's own).

        Args:
        ----
            name: The user.

        """
        self._not_own_account(name)
        await self._system_write(lambda s: s.delete_web_user(name))
        if not self.dry_run and await self._web_user(name) is not None:
            msg = f"{name}: the iTM did not delete the user"
            raise ItmCommandError(msg)

    async def fetch_web_user_screens(self, name: str) -> WebUserScreens:
        """Read the screens a normal web user may open (an administrator is refused).

        Args:
        ----
            name: The user.

        Returns:
        -------
            The allowed screens and the ones it could get.

        """
        return await self._in_session(
            lambda s: system_edit.read_web_user_screens(s, name)
        )

    async def set_web_user_screens(
        self, name: str, screens: Iterable[str]
    ) -> WebUserScreens | None:
        """Make the screens a normal web user may open exactly these.

        Args:
        ----
            name: The user (not an administrator, not the client's own account).
            screens: The screen names, as the iTM lists them.

        Returns:
        -------
            The screens as read back, or None in a dry run.

        """
        self._experimental("change a web user's allowed screens")
        self._not_own_account(name)
        wanted = tuple(screens)
        await self._system_write(
            lambda s: _ignore(system_edit.set_web_user_screens(s, name, wanted))
        )
        if self.dry_run:
            return None
        result = await self.fetch_web_user_screens(name)
        if set(result.allowed) != {x.strip() for x in wanted}:
            msg = f"{name}: the iTM did not take the screens {wanted}: {result}"
            raise ItmCommandError(msg)
        return result

    async def fetch_own_settings(self) -> OwnSettings:
        """Read the logged-in web user's own settings (screen 1205).

        Returns
        -------
            Language (as shown), date format, 12/24 hours, decimal separator and
            icon colour.

        """
        async with self._own_session() as s:
            return system_edit.own_settings_from_screen(s.state)

    async def set_own_settings(  # noqa: PLR0913  # pylint: disable=too-many-arguments,too-many-locals
        self,
        *,
        date_format: str | None = None,
        time_24h: bool | None = None,
        decimal_comma: bool | None = None,
        icon_colour: int | None = None,
        language: str | None = None,
        password: str | None = None,
        allow_password_change: bool = False,
    ) -> OwnSettings | None:
        """Change the logged-in web user's own settings (screen 1205).

        The iTM ends the session when they apply. A new password is the one this
        client logs in with from then on; it is sent once and never logged. It
        needs ``allow_password_change`` (a dry run is always allowed). When the
        read-back fails, the client goes back to the old password only if that
        one still logs in.

        Args:
        ----
            date_format: One of DATE_FORMATS.
            time_24h: 24-hour (True) or 12-hour time.
            decimal_comma: Decimal comma (True) or point.
            icon_colour: 1 or 2.
            language: en, de, es, pt, ja, fr, it, nl, zh or ko.
            password: A new password for the client's own account.
            allow_password_change: Really change the password.

        Returns:
        -------
            The settings as read back (logging in with the new password), or None
            in a dry run.

        Raises:
        ------
            ItmCommandError: Not allowed, nothing to change, or the read-back failed.

        """
        for name, value in (
            ("language", language),
            ("date format", date_format),
            ("time format", time_24h),
            ("decimal sign", decimal_comma),
        ):
            if value is not None:
                self._experimental(f"change the own {name}")
        self._writable()
        if password is not None and not allow_password_change and not self.dry_run:
            msg = (
                "changing the client's own password can lock it out: "
                "pass allow_password_change=True"
            )
            raise ItmCommandError(msg)
        changes: dict[str, Any] = {
            "date_format": date_format,
            "time_24h": time_24h,
            "decimal_comma": decimal_comma,
            "icon_colour": icon_colour,
        }
        if all(v is None for v in (*changes.values(), language, password)):
            msg = "nothing to change"
            raise ItmCommandError(msg)
        async with self._own_session() as s:
            await system_edit.set_own_settings(
                s,
                date_format=date_format,
                time_24h=time_24h,
                decimal_comma=decimal_comma,
                icon_colour=icon_colour,
                language=language,
                password=password,
            )
        if self.dry_run:
            return None
        old_password = self._password
        if password is not None:
            self._password = password
        try:
            result = await self.fetch_own_settings()
            code = await self.fetch_language() if language is not None else None
        except (ItmCommandError, LoginRejected) as err:
            msg = "could not log in again after changing the own settings"
            if password is not None:
                if await self._logs_in_with(old_password):
                    msg += "; the iTM kept the old password, which the client uses"
                else:
                    self._password = password
                    msg += "; the client keeps the new password"
            raise ItmCommandError(msg) from err
        wrong = {
            k: getattr(result, k)
            for k, v in changes.items()
            if v is not None and getattr(result, k) != v
        }
        if language is not None and code != language:
            wrong["language"] = code
        if wrong:
            msg = f"the iTM did not take the own settings: {wrong}"
            raise ItmCommandError(msg)
        return result

    async def _logs_in_with(self, password: str) -> bool:
        """Switch to a password if a login with it works; else keep the current one."""
        current, self._password = self._password, password
        try:
            await self.fetch_language()
        except (ItmCommandError, LoginRejected):
            self._password = current
            return False
        return True

    async def fetch_language(self) -> str | None:
        """Read the display language as a code (en, de, es, pt, ja, fr, it, nl, zh, ko).

        Returns
        -------
            The code, or None when not readable.

        """
        return await self._in_session(lambda s: s.read_language())

    async def set_language(self, code: str) -> str | None:
        """Switch the iTM's display language (all labels it sends change).

        The language is a setting of the web user the client logs in with; the iTM
        ends that session when it changes, so the result is read in a new one.

        Args:
        ----
            code: en, de, es, pt, ja, fr, it, nl, zh or ko.

        Returns:
        -------
            The language as read back, or None in a dry run.

        """
        if code not in LANGUAGE_RADIOS:
            msg = f"unknown language {code!r}; expected one of {list(LANGUAGE_RADIOS)}"
            raise ItmCommandError(msg)
        try:
            await self._system_write(lambda s: s.set_language(code))
        except SessionEnded:
            # The language belongs to the logged-in web user: once it changes, the
            # iTM ends that user's session ("web user deleted or changed").
            if self.dry_run:
                raise
        if self.dry_run:
            return None
        result = await self.fetch_language()
        if result != code:
            msg = f"the iTM did not switch to {code}: {result}"
            raise ItmCommandError(msg)
        return result

    async def set_region(
        self,
        *,
        date_format: str | None = None,
        time_24h: bool | None = None,
        temperature_unit: str | None = None,
        decimal_separator: str | None = None,
    ) -> ItmInfo | None:
        """Change how the iTM shows dates, times, temperatures and decimals.

        Like the language, these belong to the logged-in web user; the library
        reads every format, but a switch to °F also changes the setpoints a unit
        offers. Read back in a fresh session.

        Args:
        ----
            date_format: One of DATE_FORMATS ("%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d").
            time_24h: 24-hour (True) or 12-hour time.
            temperature_unit: "°C" or "°F".
            decimal_separator: "." or ",".

        Returns:
        -------
            The info as read back, or None in a dry run.

        """
        changes: dict[str, Any] = {
            k: v
            for k, v in (
                ("date_format", date_format),
                ("time_24h", time_24h),
                ("temperature_unit", temperature_unit),
                ("decimal_separator", decimal_separator),
            )
            if v is not None
        }
        if not changes:
            msg = "nothing to change"
            raise ItmCommandError(msg)
        allowed = {
            "date_format": DATE_FORMATS,
            "temperature_unit": ("°C", "°F"),
            "decimal_separator": (".", ","),
        }
        for key, options in allowed.items():
            if key in changes and changes[key] not in options:
                msg = f"{key}: {changes[key]!r}, expected one of {options}"
                raise ItmCommandError(msg)
        experimental = [
            k for k in ("temperature_unit", "decimal_separator") if k in changes
        ]
        if experimental:
            self._experimental(f"set the region's {' and '.join(experimental)}")
        try:
            await self._system_write(lambda s: s.set_region(changes))
        except SessionEnded:
            if self.dry_run:  # the iTM may end the user's session, like the language
                raise
        if self.dry_run:
            return None
        info = await self.fetch_info()
        wrong = {
            k: getattr(info, k) for k, v in changes.items() if getattr(info, k) != v
        }
        if wrong:
            msg = f"the iTM did not take the region settings {changes}: {wrong}"
            raise ItmCommandError(msg)
        return info

    async def fetch_dst(self) -> DstSettings:
        """Read whether the iTM switches to summer time, and its rule.

        Returns
        -------
            The settings.

        """
        return await self._in_session(lambda s: s.read_dst())

    async def set_dst(
        self,
        *,
        enabled: bool = True,
        start: DstRule = EU_DST_START,
        end: DstRule = EU_DST_END,
    ) -> DstSettings | None:
        """Switch summer time on (default: the EU rule) or off.

        Set this before the clock: switching it may move the iTM's clock by an hour.

        Args:
        ----
            enabled: Summer time on or off.
            start: When it starts (ignored when off).
            end: When it ends (ignored when off).

        Returns:
        -------
            The settings as read back, or None in a dry run.

        """
        await self._system_write(
            lambda s: s.set_dst(enabled=enabled, start=start, end=end)
        )
        if self.dry_run:
            return None
        result = await self.fetch_dst()
        ok = result.enabled is enabled and (
            not enabled or (result.start == start and result.end == end)
        )
        if not ok:
            msg = f"the iTM did not take the summer time settings: {result}"
            raise ItmCommandError(msg)
        return result

    async def set_clock(
        self, when: datetime | None = None, *, tolerance: float = 120
    ) -> ItmInfo | None:
        """Set the iTM's clock (default: this computer's local time, now).

        Args:
        ----
            when: The local date and time to set.
            tolerance: How far (seconds) the read-back may differ.

        Returns:
        -------
            The info as read back, or None in a dry run.

        """
        target = when or datetime.now()  # noqa: DTZ005 - the iTM keeps local time
        await self._system_write(lambda s: s.set_clock(target))
        if self.dry_run:
            return None
        info = await self.fetch_info()
        if info.clock is None:
            msg = "the iTM's clock could not be read back"
            raise ItmCommandError(msg)
        expected = target if when is not None else datetime.now()  # noqa: DTZ005
        if abs((info.clock - expected).total_seconds()) > tolerance:
            msg = f"the iTM's clock reads {info.clock}, expected about {expected}"
            raise ItmCommandError(msg)
        return info

    async def fetch_config(self, *, system: bool = False) -> ItmConfig:
        """Read everything configuration-like in one session.

        The indicators and clock, version and region, the schedule programs, the
        auto changeover groups (without details), setback, overtime and the
        emergency stop programs; with system, the system settings of fetch_system
        too. About twenty requests (fifty with system); meant for an hourly read,
        not every poll.

        Args:
        ----
            system: Also read the system settings screens.

        Returns:
        -------
            The configuration.

        """
        async with self._session() as s:
            try:
                indicators = s.indicators()
                info = await s.read_info()
                schedules = tuple(await s.read_schedules())
                groups = tuple(await s.read_changeover_groups())
                setback = await s.read_setback()
                overtime = await s.read_overtime()
                emergency_programs = tuple(await s.read_emergency_stop())
                settings = await s.read_system() if system else None
            finally:
                await s.close_menu()
        return ItmConfig(
            indicators=indicators,
            info=info,
            schedules=schedules,
            changeover_groups=groups,
            setback=setback,
            overtime_minutes=overtime,
            emergency_stop=emergency_programs,
            system=settings,
        )

    async def fetch_error_information(self) -> ErrorInformation | None:
        """Read the error information the iTM shows while a unit has an error.

        The main menu bar shows an error indicator then; this opens it, reads the
        list and closes it. Not seen on a device yet, so the rows are passed
        through as listed.

        Returns
        -------
            The errors, or None when the iTM shows no error.

        """
        async with self._session() as s:
            return await s.read_error_information()

    async def _confirm(
        self, unit: str, what: str, check: Callable[[Unit], bool]
    ) -> Unit:
        deadline = time.monotonic() + self.confirm_timeout
        while True:
            await asyncio.sleep(self.confirm_interval)
            u = await self.fetch_unit(unit)
            if check(u):
                return u
            if time.monotonic() >= deadline:
                msg = (
                    f"{unit}: {what} not confirmed within {self.confirm_timeout}s "
                    f"(last seen: power={u.power} mode={u.mode} fan={u.fan_speed} "
                    f"setpoint={u.setpoint})"
                )
                raise ItmCommandError(msg)

    async def _settle(self, unit: str) -> None:
        """Wait until an earlier unconfirmed change to this unit shows.

        So the next change can't overtake it. Callers hold _unit_lock(unit). A change
        that never shows is logged, not raised: it belongs to an earlier call that
        has already returned.
        """
        confirm = self._unconfirmed.pop(unit, None)
        if confirm is None:
            return
        for other in [k for k, v in self._unconfirmed.items() if v is confirm]:
            del self._unconfirmed[other]
        try:
            await confirm()
        except ItmCommandError as err:
            _LOGGER.warning("%s", err)

    async def _finish(
        self, unit: str, what: str, check: Callable[[Unit], bool], *, confirm: bool
    ) -> Unit | None:
        if self.dry_run:
            return None
        if confirm:
            return await self._confirm(unit, what, check)
        self._unconfirmed[unit] = functools.partial(self._confirm, unit, what, check)
        return None

    # -- whole zones ------------------------------------------------------------------
    @asynccontextmanager
    async def _write_zone(self, zone: str) -> AsyncIterator[_Session]:
        """Open the zone path and select a zone's icon: its panel operates them all."""
        async with self._session() as s:
            await s.open_zone(self.zone_path)
            name = _zone_name(zone, s.icons())
            if not any(i[0] == name and i[1] == ICON_ID_ZONE for i in s.icons()):
                msg = f"{zone!r} is not a zone"
                raise ItmCommandError(msg)
            await s.select_by_name(name)
            yield s

    async def _zone_members(self, zone: str) -> list[str]:
        """Return the points directly in a zone below the zone path."""
        async with self._session() as s:
            await s.open_zone(self.zone_path)
            name = _zone_name(zone, s.icons())
            if not any(i[0] == name and i[1] == ICON_ID_ZONE for i in s.icons()):
                msg = f"{zone!r} is not a zone"
                raise ItmCommandError(msg)
            await s.open_zone((name,))
            return [i[0] for i in s.icons() if i[1] != ICON_ID_ZONE]

    @asynccontextmanager
    async def _zone_writes(self, zone: str) -> AsyncIterator[list[str]]:
        """Apply the per-unit write rules to every point of a zone, for a zone write.

        Holds each member's unit lock (in name order, so two zone writes can't
        deadlock), lets an earlier unconfirmed change to a member show first, and
        spaces the write like a unit write.
        """
        members = await self._zone_members(zone)
        async with contextlib.AsyncExitStack() as stack:
            for member in sorted(set(members)):
                await stack.enter_async_context(self._unit_lock(member))
            for member in members:
                await self._settle(member)
            now = time.monotonic()
            wait = max(
                (
                    self._last_write.get(m, 0) + self.min_write_interval - now
                    for m in members
                ),
                default=0,
            )
            if wait > 0:
                await asyncio.sleep(wait)
            try:
                yield members
            finally:
                done = time.monotonic()
                for member in members:
                    self._last_write[member] = done

    async def _finish_zone(  # pylint: disable=too-many-arguments
        self,
        zone: str,
        members: list[str],
        what: str,
        check: Callable[[Unit], bool],
        *,
        confirm: bool,
    ) -> dict[str, Unit] | None:
        """Confirm a zone write, or leave it to the next write to each member."""
        if self.dry_run:
            return None
        if confirm:
            return await self._confirm_zone(zone, what, check)
        later = functools.partial(self._confirm_zone, zone, what, check)
        for member in members:
            self._unconfirmed[member] = later
        return None

    async def fetch_zone_units(self, zone: str) -> dict[str, Unit]:
        """Read every point directly in a zone below the zone path.

        Args:
        ----
            zone: The zone's name (or "#n", or the "All" zone in any language).

        Returns:
        -------
            The units and other points, by name.

        """
        async with self._session() as s:
            await s.open_zone((*self.zone_path, zone))
            return await s.read_units()

    async def _confirm_zone(
        self, zone: str, what: str, check: Callable[[Unit], bool]
    ) -> dict[str, Unit]:
        """Wait until every indoor unit in the zone shows a change."""
        deadline = time.monotonic() + self.confirm_timeout
        while True:
            await asyncio.sleep(self.confirm_interval)
            units = await self.fetch_zone_units(zone)
            indoor = [u for u in units.values() if u.kind is PointKind.INDOOR]
            if indoor and all(check(u) for u in indoor):
                return units
            if time.monotonic() >= deadline:
                pending = [u.name for u in indoor if not check(u)]
                msg = f"{zone}: {what} not confirmed on {pending}"
                raise ItmCommandError(msg)

    async def set_zone_power_state(
        self, zone: str, state: PowerState, *, confirm: bool = True
    ) -> dict[str, Unit] | None:
        """Put every unit of a zone on, off, or in setback low/high at once.

        Args:
        ----
            zone: The zone's name, below the client's zone path.
            state: on / off / setback_low / setback_high.
            confirm: Wait until every indoor unit in the zone shows it.

        Returns:
        -------
            The zone's points as read back, or None when not confirmed or in a dry
            run.

        """
        self._writable()
        async with self._zone_writes(zone) as members:
            return await self._zone_power_state(zone, members, state, confirm=confirm)

    async def _zone_power_state(
        self, zone: str, members: list[str], state: PowerState, *, confirm: bool
    ) -> dict[str, Unit] | None:
        async with self._write_zone(zone) as s:
            options = s.state.get(ScreenID.OPERATION, P_POWER, "COMBO_STRS") or []
            order = {
                2: (PowerState.ON, PowerState.OFF),
                4: (
                    PowerState.ON,
                    PowerState.SETBACK_LOW,
                    PowerState.SETBACK_HIGH,
                    PowerState.OFF,
                ),
            }.get(len(options), ())
            if state not in order:
                msg = f"{zone}: {state} not offered (options {options})"
                raise ItmCommandError(msg)
            await s.choose(P_POWER, order.index(state), confirm=True)
            await s.commit(self.commit_delay)
        return await self._finish_zone(
            zone,
            members,
            f"power {state}",
            lambda u: u.power_state == state,
            confirm=confirm,
        )

    async def set_zone_setpoint(
        self, zone: str, value: int, *, confirm: bool = True
    ) -> dict[str, Unit] | None:
        """Give every running unit of a zone the same setpoint.

        Args:
        ----
            zone: The zone's name, below the client's zone path.
            value: The setpoint, one the zone's panel offers.
            confirm: Wait until every running indoor unit in the zone shows it.

        Returns:
        -------
            The zone's points as read back, or None when not confirmed or in a dry
            run.

        """
        self._writable()
        async with self._zone_writes(zone) as members:
            async with self._write_zone(zone) as s:
                current = unit_from_panel(s.state)
                if not current.running:
                    msg = f"{zone} is stopped; the iTM ignores setpoint changes then"
                    raise ItmCommandError(msg)
                nums = list(current.setpoint_range)
                if value not in nums:
                    raise SetpointOutOfRange(zone, value, current.setpoint_range)
                await s.choose(P_SETPOINT, nums.index(value))
                await s.commit(self.commit_delay)
            return await self._finish_zone(
                zone,
                members,
                f"setpoint {value}",
                lambda u: not u.running or u.setpoint == value,
                confirm=confirm,
            )

    async def apply_zone(
        self,
        zone: str,
        *,
        power: bool | None = None,
        mode: str | None = None,
        fan_speed: str | None = None,
        confirm: bool = True,
    ) -> dict[str, Unit] | None:
        """Change power, mode and/or fan speed of every unit in a zone at once.

        Through the zone's settings dialog, like apply() does for one unit.

        Args:
        ----
            zone: The zone's name, below the client's zone path.
            power: Start (True) or stop (False) them.
            mode: heat / cool / fan / dry / auto, as the zone's dialog offers.
            fan_speed: low / medium / high / auto.
            confirm: Wait until every indoor unit in the zone shows the change.

        Returns:
        -------
            The zone's points as read back, or None when not confirmed or in a dry
            run.

        """
        if power is None and mode is None and fan_speed is None:
            return None
        self._writable()
        if fan_speed is not None and fan_speed not in FAN_SPEEDS:
            msg = (
                f"{zone}: unknown fan speed {fan_speed!r}, expected one of {FAN_SPEEDS}"
            )
            raise ItmCommandError(msg)
        async with self._zone_writes(zone) as members:
            async with self._write_zone(zone) as s:
                await s.change_settings(
                    zone, power=power, mode=mode, fan_speed=fan_speed
                )
            return await self._finish_zone(
                zone,
                members,
                "change",
                lambda u: (
                    (power is None or _power_is(u, on=power))
                    and (mode is None or u.mode == mode)
                    and (fan_speed is None or u.fan_speed in {None, fan_speed})
                ),
                confirm=confirm,
            )

    async def list_zones(self, path: tuple[str, ...] = ()) -> list[str]:
        """List the zones and units at a path (for discovery and configuration).

        Args:
        ----
            path: The zone path, empty for the top level.

        Returns:
        -------
            The names of the zones and units there.

        """
        async with self._session() as s:
            await s.open_zone(path)
            return [i[0] for i in s.icons()]

    async def set_setpoint(
        self, unit: str, value: int, *, confirm: bool = True
    ) -> Unit | None:
        """Change a unit's setpoint.

        The iTM ignores this while the unit is stopped, so that is refused.

        Args:
        ----
            unit: The unit's name.
            value: The new setpoint, one of the unit's setpoint_range.
            confirm: Wait until the change shows on the unit. Without, the next
                write to the same unit first waits until this change shows.

        Returns:
        -------
            The unit as read back once the change shows, or None when not confirmed.

        Raises:
        ------
            ItmCommandError: The unit is stopped, the value is out of range, or the
                change did not show in time.

        """
        self._writable()
        async with self._unit_lock(unit):
            await self._settle(unit)
            async with self._write(unit) as s:
                current = unit_from_panel(s.state)
                if not current.running:
                    msg = f"{unit} is stopped; the iTM ignores setpoint changes then"
                    raise ItmCommandError(msg)
                nums = list(current.setpoint_range)
                if value not in nums:
                    raise SetpointOutOfRange(unit, value, current.setpoint_range)
                if current.setpoint == value:
                    return current
                await s.choose(P_SETPOINT, nums.index(value))
                await s.commit(self.commit_delay)
            return await self._finish(
                unit,
                f"setpoint {value}",
                lambda u: u.setpoint == value,
                confirm=confirm,
            )

    async def fetch_capabilities(self) -> dict[str, Capabilities]:
        """Read every unit's identity and which modes and fan control it offers.

        The identity comes from the Details screen, the rest from the settings dialog
        (cancelled). This costs four requests per unit, so do it once rather than on
        every poll.

        Returns
        -------
            The capabilities by unit name.

        """
        async with self._session() as s:
            await s.open_zone(self.zone_path)
            caps = {}
            for i, (name, icon_id, _) in enumerate(s.icons()):
                if icon_id == ICON_ID_ZONE:
                    continue
                await s.select_icon(i)
                details = await s.read_details()
                await s.open_settings()
                caps[name] = capabilities_from_dialog(name, s.state, details)
                await s.close_settings(ok=False)
            return caps

    async def apply(
        self,
        unit: str,
        *,
        power: bool | None = None,
        mode: str | None = None,
        fan_speed: str | None = None,
        confirm: bool = True,
    ) -> Unit | None:
        """Change power, mode and/or fan speed of a unit in one go.

        This goes through the unit's settings dialog.

        Args:
        ----
            unit: The unit's name.
            power: Start (True) or stop (False) the unit.
            mode: heat / cool / fan / dry / auto, as offered by the unit (see
                fetch_capabilities).
            fan_speed: low / medium / high / auto.
            confirm: Wait until the change shows on the unit. Without, the next
                write to the same unit first waits until this change shows.

        Returns:
        -------
            The unit as read back once the change shows, or None when not confirmed
            or nothing was asked to change.

        Raises:
        ------
            ItmCommandError: The unit does not offer the change, or the change did
                not show in time.

        """
        if power is None and mode is None and fan_speed is None:
            return None
        self._writable()
        if fan_speed is not None and fan_speed not in FAN_SPEEDS:
            msg = (
                f"{unit}: unknown fan speed {fan_speed!r}, expected one of {FAN_SPEEDS}"
            )
            raise ItmCommandError(msg)
        async with self._unit_lock(unit):
            await self._settle(unit)
            async with self._write(unit) as s:
                # Compare with the unit as read in this session, not with what the
                # caller last saw: only send what differs, and nothing at all (no
                # write to the unit) if it already is in the requested state.
                current = unit_from_panel(s.state)
                if power is not None and _power_is(current, on=power):
                    power = None
                if mode is not None and current.mode == mode:
                    mode = None
                if fan_speed is not None and current.fan_speed == fan_speed:
                    fan_speed = None
                if power is None and mode is None and fan_speed is None:
                    return current
                await s.change_settings(
                    unit, power=power, mode=mode, fan_speed=fan_speed
                )

            return await self._finish(
                unit,
                "change",
                lambda u: (
                    (power is None or _power_is(u, on=power))
                    and (mode is None or u.mode == mode)
                    and (fan_speed is None or u.fan_speed == fan_speed)
                ),
                confirm=confirm,
            )

    async def set_power(
        self,
        unit: str,
        on: bool,  # noqa: FBT001 - public API, positional like the iTM's own switch
        *,
        confirm: bool = True,
    ) -> Unit | None:
        """Start or stop a unit, see apply."""
        return await self.apply(unit, power=on, confirm=confirm)

    async def set_mode(
        self, unit: str, mode: str, *, confirm: bool = True
    ) -> Unit | None:
        """Change a unit's operation mode, see apply."""
        return await self.apply(unit, mode=mode, confirm=confirm)

    async def set_fan_speed(
        self, unit: str, fan_speed: str, *, confirm: bool = True
    ) -> Unit | None:
        """Change a unit's fan speed, see apply."""
        return await self.apply(unit, fan_speed=fan_speed, confirm=confirm)

    def _unit_lock(self, unit: str) -> asyncio.Lock:
        """Return the lock held for a whole write to a unit, incl. confirmation.

        So a second write to the same unit can't be sent while the first change is
        still on its way to the unit (the iTM may drop one of them).
        """
        return self._unit_locks.setdefault(unit, asyncio.Lock())

    @asynccontextmanager
    async def _write(self, unit: str) -> AsyncIterator[_Session]:
        # Callers hold _unit_lock(unit). Space out writes per unit: the indoor units
        # store settings in memory with a limited number of writes.
        wait = (
            self._last_write.get(unit, 0) + self.min_write_interval - time.monotonic()
        )
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            async with self._session() as s:
                await s.open_zone(self.zone_path)
                await s.select_by_name(unit)
                yield s
        finally:
            self._last_write[unit] = time.monotonic()
