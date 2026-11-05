"""One logged-in session with the iTM: the screen work behind ItmClient."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
import re
from dataclasses import replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

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
    SB_ADVANCED,
    SB_HIGH_BACK,
    SB_HIGH_UP,
    SB_LOW_BACK,
    SB_LOW_UP,
    SB_OK,
    SBA_COOL,
    SBA_HEAT,
    SBA_OK,
    ChangeoverGroup,
    EmergencyStopProgram,
    History,
    ItmIndicators,
    ItmInfo,
    MenuScreen,
    OptionalFunction,
    ScheduleSummary,
    SetbackSettings,
    changeover_candidates_from_screen,
    changeover_groups_from_screen,
    changeover_members_from_screen,
    changeover_settings_from_screen,
    clock_from_screen,
    emergency_stop_from_screen,
    history_from_screen,
    indicators_from_screen,
    overtime_from_screen,
    parse_shown_date,
    region_from_screen,
    schedules_from_screen,
    setback_from_screen,
    version_from_screen,
)
from .models import (
    E_CLOSE,
    L_BTN_ICON,
    L_LIST,
    MM_ERROR_BUTTON,
    P_NAME,
    ErrorInformation,
    PointKind,
    Unit,
    analog_from_panel,
    error_information_from_screen,
    point_kind,
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
from .schedule_edit import ScheduleSessionMixin
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
from .settings_dialog import SettingsDialogMixin
from .state import UIState
from .system import (
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
    EnergyNavigatorInfo,
    SystemSettings,
    confirm_dialogs_from_screen,
    email_from_screen,
    energy_export_from_screen,
    energy_sampling_from_screen,
    hardware_from_screen,
    maintenance_from_screen,
    network_from_screen,
    points_from_screen,
    ppd_from_screen,
    remote_management_from_screen,
    row_index,
    screensaver_from_screen,
    web_users_from_screen,
    zones_from_screen,
)
from .system_edit import SystemSessionMixin
from .users_edit import UsersSessionMixin

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from ._util import T as _T
    from .client import ItmClient, Point, SessionStore
    from .protocol import Reply

_LOGGER = logging.getLogger(__name__)

# Icon view (screen 1) part IDs (ScMainStdIcon.initScreen in script.js).
ICON_BTN_TOP, ICON_BTN_DOWN, ICON_BTN_UP, ICON_BTN_LIST, ICON_PANEL = 1, 2, 3, 4, 6
ICON_ID_ZONE = 1000
LOGIN_BUTTON = 4


class _Session(
    ChangeoverSessionMixin,
    SettingsDialogMixin,
    SystemSessionMixin,
    UsersSessionMixin,
    ScheduleSessionMixin,
):  # pylint: disable=too-many-public-methods
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

    # -- setback, overtime, service and history -----------------------------------
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
