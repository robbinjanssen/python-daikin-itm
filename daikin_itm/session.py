"""One logged-in session with the iTM: the screen work behind ItmClient."""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import weakref
from dataclasses import replace
from datetime import date, datetime
from typing import TYPE_CHECKING, Any

import aiohttp

from ._screens import answer_date as _answer_date
from ._screens import raw_rows as _rows
from ._screens import row_cells as _cells
from ._screens import set_checkbox as _set_checkbox
from ._util import cleanup as _cleanup
from ._util import cleanup_after as _cleanup_after
from .changeover import ChangeoverSessionMixin
from .exceptions import (
    ExperimentalError,
    ItmCommandError,
    ItmError,
    LoginRejected,
    MenuNotAvailable,
    ProtocolError,
    ReadOnlyError,
    RequestSkipped,
    SessionEnded,
    SharedSessionError,
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
    H_DAY_CHANGE,
    H_PAGE_NEWER,
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
    D_CANCEL,
    DT_CLOSE,
    E_CLOSE,
    L_BTN_ICON,
    L_LIST,
    MM_ERROR_BUTTON,
    P_NAME,
    ErrorInformation,
    PointKind,
    Unit,
    analog_details_from_screen,
    analog_from_list,
    analog_from_panel,
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
    login_payload,
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
    from collections.abc import Awaitable, Callable, Collection

    from ._util import T as _T
    from .client import ItmClient, Point, SessionStore
    from .models import AnalogPoint
    from .protocol import Reply

_LOGGER = logging.getLogger(__name__)

# Icon view (screen 1) part IDs (ScMainStdIcon.initScreen in script.js). Part 0
# shows the path to the level shown; Top and Up are disabled at the top level.
ICON_PATH = 0
ICON_BTN_TOP, ICON_BTN_DOWN, ICON_BTN_UP, ICON_BTN_LIST, ICON_PANEL = 1, 2, 3, 4, 6
ICON_ID_ZONE = 1000
LOGIN_BUTTON = 4

_SAVED_WITH_OK = frozenset({"CHARINPUT", "NUMBER", "HOURMIN"})
# The clients that have warned about a password typed over plain http.
_WARNED_PLAIN_HTTP: weakref.WeakSet[ItmClient] = weakref.WeakSet()

# How a screen a call left open is closed on the way back to the start position:
# its Close or Cancel part, never OK.
_CLOSERS: dict[int, int] = {
    **{int(screen): part for screen, part in CLOSE_PART.items()},
    ScreenID.SETTINGS: D_CANCEL,
    ScreenID.DETAILS: DT_CLOSE,
    ScreenID.ERROR_INFORMATION: E_CLOSE,
}


def _analog_and_status(state: UIState) -> dict[str, Any]:
    """Read an analog point's attributes and its thermostat status at once."""
    return {
        "analog": analog_details_from_screen(state),
        "thermo_on": status_from_details(state)["thermo_on"],
    }


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
        self.restarting = False
        # A shared_session() block's session: reads only, see ItmClient._writable.
        self.shared = False
        # A kept_session() block's session: writes only with allow_experimental.
        self.kept = False
        # A write block's session (confirm_in_session): writes allowed.
        self.writing = False
        # The start position (the screens open and the icon view's level right
        # after the login), the dialogs open now, and why the session can't be
        # brought back there (None: it can).
        self._start: tuple[frozenset[int], tuple[Any, ...]] | None = None
        self._dialogs: dict[tuple[int, int], str] = {}
        self._stuck: str | None = None
        # The analog list view is being shown: its main view switch keeps the
        # session (seen on a real iTM), unlike the units' list and the layout.
        self._analog_list = False
        self._menu_available = True
        # A request got no answer (a timeout, a lost connection): the iTM may hang,
        # so only the logout is still sent (once, with the client's
        # logout_timeout); every other request raises RequestSkipped.
        self.unanswered = False
        # The request that makes the current call's change was sent (or was about
        # to be): a lost reply after it leaves the outcome unknown.
        self.change_sent = False

    @property
    def menu_available(self) -> bool:
        """Tell whether the login showed the main menu bar's menu button.

        A normal web user without allowed screens gets it hidden; True before a
        login.
        """
        return self._menu_available

    @property
    def client(self) -> ItmClient:
        """The client this session belongs to (its settings and plan)."""
        return self._client

    async def forget_login(self) -> None:
        """Mark the session logged out and drop its saved token."""
        self.logged_in = False
        await self._store.save(None)

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

        Each request is bounded by the client's request_timeout. After one got no
        answer only a logout is sent, bounded by logout_timeout.

        Raises
        ------
            RequestSkipped: An earlier request of this session got no answer.

        """
        if self.unanswered and event != EventType.LOGOUT:
            msg = "not sent: an earlier request of this login got no answer"
            raise RequestSkipped(msg)
        if event == EventType.CLICK and screen != ScreenID.SETTINGS:
            self.refuse_disabled(screen, part, f"screen {screen} part {part}")
        if (
            event == EventType.CLICK
            and data_type == DataType.STRING
            and self._dialogs.get((screen, part)) == "PASSWORD"
        ):
            self._warn_plain_http()
        body = encode_event(
            self.token, screen, part, event, data_type, number, array, string=string
        )
        text = await self._exchange(body)
        await self._take_token(text)
        reply = parse_reply(text)
        if (
            event == EventType.CLICK
            and data_type == DataType.INT
            and (screen, part) not in self._dialogs
        ):
            self.state.select_locally(screen, part, number)
        self.state.apply(reply.screens)
        self._track(screen, part, event, data_type, reply)
        for s in reply.screens:
            if s.update == "LOGOUT":
                self.logged_in = False
                await self._store.save(None)
                if s.data and s.data[0] == 2 and len(s.data) > 1:
                    raise VersionMismatch(s.data[1])
                msg = f"iTM ended the session: {s.data}"
                raise SessionEnded(msg)
        return reply

    async def _exchange(self, body: str) -> str:
        """Post a request and return the reply's text, keeping its token on cancel.

        Bounded by request_timeout, or by logout_timeout once a request got no
        answer.
        """
        seconds = self._client.request_timeout
        if self.unanswered:
            seconds = min(seconds, self._client.logout_timeout)
        request = asyncio.ensure_future(self._timed(body, seconds))
        try:
            return await asyncio.shield(request)
        except asyncio.CancelledError:
            # The iTM may already have processed this request and moved on to a new
            # token. Let it finish so the token stays known and the caller's cleanup
            # can still log out, then re-raise.
            try:
                await self._take_token(await request)
            except (aiohttp.ClientError, TimeoutError) as err:
                self._no_answer(err)
            except Exception:  # pylint: disable=broad-exception-caught
                _LOGGER.debug("reply after a cancellation not used", exc_info=True)
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            self._no_answer(err)
            raise

    async def _timed(self, body: str, seconds: float) -> str:
        """Post a request, bounded by seconds."""
        async with asyncio.timeout(seconds):
            return await self._post(body)

    def _no_answer(self, err: BaseException) -> None:
        """Record a request that got no answer; an HTTP error status is an answer."""
        if not isinstance(err, aiohttp.ClientResponseError):
            _LOGGER.debug("no answer from the iTM: %r", err)
            self.unanswered = True

    def _warn_plain_http(self) -> None:
        """Warn once per client that a typed password goes over plain http."""
        if self._client.plain_http and self._client not in _WARNED_PLAIN_HTTP:
            _WARNED_PLAIN_HTTP.add(self._client)
            _LOGGER.warning(
                "a password typed in the iTM (service, web user, touch panel or "
                "e-mail server password) goes in clear over plain http; use an "
                "https:// address where the iTM offers it"
            )

    def _track(
        self, screen: int, part: int, event: int, data_type: int, reply: Reply
    ) -> None:
        """Keep what the start position needs: open dialogs, what can't be undone.

        A value entered in a DATE, TIME or PASSWORD dialog, or one of a type not
        known to be saved with OK, may outlive its screen (the history opens on
        the date entered), and the iTM hides the list and layout views instead
        of closing them, so what it sends when one is shown again is unknown:
        after either, only a new login gives a start position to trust (bar the
        analog list view, which was seen to keep the session). A name,
        number or time of day entered (CHARINPUT, NUMBER, HOURMIN) is saved with
        its screen's OK and keeps the session.
        """
        answers = {EventType.CLICK, EventType.CLICK2, EventType.CANCEL_PROG}
        if (screen, part) in self._dialogs and event in answers:
            kind = self._dialogs.pop((screen, part))
            if (
                event == EventType.CLICK
                and data_type != DataType.NONE
                and kind not in _SAVED_WITH_OK
            ):
                self.no_way_back(f"a value was entered in a {kind} dialog")
        for s in reply.screens:
            if s.update == "DLG_OPEN":
                self._dialogs[s.screen, s.data[0]] = str(s.data[1])
            elif s.update == "DLG_CLOSE":
                self._dialogs.pop((s.screen, s.data[0]), None)
            elif s.update == "CLOSE":
                self._dialogs = {
                    d: kind for d, kind in self._dialogs.items() if d[0] != s.screen
                }
            elif s.update in {"SHOW", "HIDE"} and not self._analog_list:
                self.no_way_back("the main view was switched")

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
        await self.handshake()
        reply = await self.press_login(username, password, LOGIN_BUTTON)
        if any(
            s.screen == ScreenID.LOGIN and s.update == "DLG_OPEN" for s in reply.screens
        ):
            await self.forget_login()
            msg = (
                "login rejected (wrong credentials, "
                "or this user is logged in elsewhere)"
            )
            raise LoginRejected(msg)
        self._menu_available = self.state.visible(ScreenID.MAINMENU, MENU_OPEN)
        self._client._logged_in(self)  # noqa: SLF001  # pylint: disable=protected-access

    async def handshake(self) -> None:
        """Open the login screen, adopting a new web UI version after an update.

        The INIT is sent once more when its connection broke (a keep-alive
        connection the iTM closed, a restart): it changes nothing on the iTM. A
        timeout isn't retried, so a hung iTM costs one timeout.
        """
        version = self._client.web_version
        try:
            try:
                await self._init(version)
            except aiohttp.ClientConnectionError as err:
                if isinstance(err, TimeoutError):
                    raise
                _LOGGER.debug("the INIT got no answer (%r); sending it again", err)
                self.unanswered = False
                await self._init(version)
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
            await self._init(e.version)

    async def _init(self, version: str) -> None:
        """Send the INIT event that opens the login screen."""
        await self.send(event=EventType.INIT, data_type=DataType.STRING, string=version)

    async def press_login(self, username: str, password: str, button: int) -> Reply:
        """Press a login screen button with the credentials, as the browser does."""
        self.token = NO_TOKEN
        self.logged_in = True  # from here on every reply token is persisted
        return await self.send(
            ScreenID.LOGIN,
            button,
            EventType.CLICK,
            DataType.STRING,
            string=login_payload(username, password),
        )

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
            await self.select_by_name(zone_name(zone, self.icons()))
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
        return await self._read_list(units_from_list)

    async def read_analog_from_list(self) -> dict[str, AnalogPoint]:
        """Read the analog inputs' values from the current zone's list view.

        Unlike the units' list, showing this list and the icon view again keeps
        the session: on a real iTM the reads after it gave what they give alone.
        """
        self._analog_list = True
        try:
            return await self._read_list(analog_from_list)
        finally:
            self._analog_list = False

    async def _read_list(self, read: Callable[[UIState], _T]) -> _T:
        """Show the list view, read it with read, and show the icon view again."""
        await self.send(ScreenID.STDICON, ICON_BTN_LIST)
        async with _cleanup(self._show_icons):
            return read(self.state)

    async def _show_icons(self) -> None:
        """Go back from the list view to the icon view."""
        await self.send(ScreenID.STDLIST, L_BTN_ICON)
        # The list stays in the merged state; a later read in this session
        # must not mistake it for a fresh one.
        self.state.screens.get(ScreenID.STDLIST, {}).pop((L_LIST, "PNTLIST"), None)

    # -- start position -----------------------------------------------------------
    def _level(self) -> tuple[Any, ...]:
        """Return where the icon view is: its path and the icons shown there."""
        return (
            self.state.get(ScreenID.STDICON, ICON_PATH, "LABEL"),
            tuple(icon[:2] for icon in self.icons()),
        )

    def mark_start(self) -> None:
        """Remember the start position: what the session shows right after login."""
        self._start = (frozenset(self.state.screens), self._level())
        self._dialogs = {}
        self._stuck = None

    def no_way_back(self, why: str) -> None:
        """Record that only a new login gives the start position again.

        For what a reader changes that may outlive the screen it is on and that
        the next reader would take for the state after a login: a day or page
        moved to, service mode.
        """
        self._stuck = why

    async def step_back(
        self,
        steps: int,
        press: Callable[[], Awaitable[object]],
        shown: Callable[[], object],
        start: object,
        why: str,
    ) -> None:
        """Undo the steps a reader took to another day or page, in a shared session.

        The screen may show that day or page again when it is opened next, so the
        session only stays when pressing the way back as often brings back what
        the screen showed first, and that is known. Outside a shared session nothing
        is pressed.

        Args:
        ----
            steps: How often the reader stepped away.
            press: Presses the way back once.
            shown: Returns what the screen shows: a day, a page.
            start: What it showed before the reader stepped away.
            why: The reason the session ends when the way back fails.

        """
        if not steps:
            return
        if self.shared:
            try:
                for _ in range(steps):
                    await press()
            except ItmError as err:
                _LOGGER.debug("no way back: %s", err)
            else:
                if start is not None and shown() == start:
                    return
        self.no_way_back(why)

    def at_start(self) -> bool:
        """Tell whether the session is where a fresh login starts.

        The top level of the icon view, with no menu, screen or dialog open, and
        nothing done that only a new login undoes. The operation panel shows one
        of that level's icons then; the iTM names it again in answer to a
        POLLING event, so this also tells when the iTM is somewhere else. A
        screen still showing a value the client picked is no start either: the
        next reader would take the pick for what the iTM shows.
        """
        if not self.logged_in or self._stuck or self._dialogs:
            return False
        if any(self.state.picked(screen) for screen in self.state.screens):
            return False
        names = [icon[0] for icon in self.icons()]
        if names and self.state.get(ScreenID.OPERATION, P_NAME, "LABEL") not in names:
            return False
        return (frozenset(self.state.screens), self._level()) == self._start

    async def back_to_start(self) -> bool:
        """Close what a call left open and go back to the icon view's top level.

        Returns
        -------
            Whether the session is at its start position again. False when it
            can't be brought there: only a new login gives one then.

        """
        if self._start is None or self._stuck or self._dialogs:
            _LOGGER.debug(
                "no way back to the start position: %s",
                self._stuck or f"dialogs open {sorted(self._dialogs)}",
            )
            return False
        screens, level = self._start
        left = [s for s in self.state.screens if s not in screens]
        for screen in reversed(left):
            if screen == MenuScreen.MENULIST or screen not in self.state.screens:
                continue
            if screen not in _CLOSERS:
                return False
            await self.send(screen, _CLOSERS[screen])
            if screen in self.state.screens:
                return False
        await self.close_menu()
        await self._show_other_icon()
        if self._level() != level:
            await self.click(
                ScreenID.STDICON, ICON_BTN_TOP, "the icon view's top level"
            )
            await self._show_other_icon()
        return self.at_start()

    async def _show_other_icon(self) -> None:
        """Show other points on the operation panel until the iTM sent each pick over.

        The panel keeps a value the client picked until the iTM sends that part
        again, and the iTM never echoes a pick. Another point's panel sends it
        when that point's value differs from what the iTM takes the client to
        show, which is unknown (the pick or the value before it). So only a part
        the iTM sent counts: the other points are shown one by one until none
        holds a pick, and when one still does at_start ends the session.
        """
        shown = self.state.get(ScreenID.OPERATION, P_NAME, "LABEL")
        for i, icon in enumerate(self.icons()):
            if not self.state.picked(ScreenID.OPERATION):
                return
            if icon[0] != shown:
                await self.select_icon(i)

    # -- operation panel ----------------------------------------------------------
    def require_writable(self) -> None:
        if self._client.read_only:
            msg = "the client is read-only"
            raise ReadOnlyError(msg)
        if self.shared and not (self.kept or self.writing):
            msg = "a shared session only reads; write outside the block"
            raise SharedSessionError(msg)
        if self.kept and not (self._client.dry_run or self._client.allow_experimental):
            msg = "a write in a kept session is experimental; pass allow_experimental"
            raise ExperimentalError(msg)

    async def choose(self, part: int, index: int, *, confirm: bool = False) -> None:
        """Pick an option of an operation panel control.

        With confirm, a confirm dialog the pick opens is answered Yes: the iTM asks
        before it puts a unit in setback ("setback low?").
        """
        self.require_writable()
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
            await self.answer_confirm(ScreenID.OPERATION, reply)
        elif reply.dialogs():
            msg = f"iTM opened a dialog: {reply.dialogs()}"
            raise ItmCommandError(msg)

    async def commit(self, delay: float) -> None:
        """Commit operation panel changes like the browser does."""
        self.require_writable()
        if self._client.dry_run:
            self._client.planned.append(
                f"commit: screen {ScreenID.OPERATION} event {EventType.TIMER}"
            )
            return
        # The browser waits SEND_TIMER_INTERVAL (3s) after the last change, then
        # sends a TIMER event.
        await asyncio.sleep(delay)
        self.change_sent = True
        await self.send(ScreenID.OPERATION, -1, EventType.TIMER)

    # -- points of any kind -------------------------------------------------------
    async def read_points(
        self, *, details: bool = False, thermostat: Collection[str] = ()
    ) -> tuple[dict[str, Point], dict[str, bool | None]]:
        """Read every point in the current zone, whatever its kind.

        Args:
        ----
            details: Also read each analog point's Details screen (one request
                more per analog point).
            thermostat: The points to read the thermostat status of, from their
                Details screen while they are selected (two requests more each;
                an analog point read with details too shares its one).

        Returns:
        -------
            The points by name: a Unit for indoor and ventilation units, an
            AnalogPoint for analog inputs; and the thermostat status by name of
            the points in thermostat.

        """
        points: dict[str, Point] = {}
        status: dict[str, bool | None] = {}
        for i, (name, icon_id, _) in enumerate(self.icons()):
            if icon_id == ICON_ID_ZONE:
                continue
            await self.select_icon(i)
            point: Point
            if point_kind(icon_id) is PointKind.ANALOG:
                point = analog_from_panel(self.state)
                if details and name in thermostat:
                    both = await self._with_details(_analog_and_status)
                    point = replace(point, **both["analog"])
                    status[name] = both["thermo_on"]
                elif details:
                    point = replace(point, **await self.read_analog_details())
            else:
                point = unit_from_panel(self.state)
            if point.name != name:
                msg = f"operation panel shows {point.name!r} instead of {name!r}"
                raise ItmCommandError(msg)
            if name in thermostat and name not in status:
                status[name] = (await self.read_detail_status())["thermo_on"]
            points[name] = point
        return points, status

    # -- main menu ----------------------------------------------------------------
    def indicators(self) -> ItmIndicators:
        """Return the main menu bar's indicators and clock (open in every session)."""
        return indicators_from_screen(self.state, menu_available=self.menu_available)

    async def open_menu(self) -> None:
        """Open the menu list from the main menu bar.

        A menu button the login hid raises MenuNotAvailable before anything is
        sent, as does a reply without the menu list and without a dialog (both
        seen for a normal web user without allowed screens); a dialog raises
        ItmCommandError.
        """
        if MenuScreen.MENULIST in self.state.screens:
            return
        if not self.menu_available:
            raise MenuNotAvailable(button_hidden=True)
        reply = await self.send(ScreenID.MAINMENU, MENU_OPEN)
        if MenuScreen.MENULIST in self.state.screens:
            return
        if not reply.dialogs():
            raise MenuNotAvailable
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
        reply = await self.send(-1, -1, EventType.POLLING, DataType.INT, mask)
        self._client._polled()  # noqa: SLF001  # pylint: disable=protected-access
        return reply

    async def wait_for_progress(
        self, reply: Reply, *, seconds: float | None = None, cancel: bool = False
    ) -> None:
        """Wait out a PROGRESS dialog a reply opened, like the browser does.

        The browser asks for progress (UPD_PROG with the job ID) until the iTM
        closes the dialog, then a POLLING event brings the data. A progress
        request is not started once the time is up.

        Args:
        ----
            reply: The reply that may have opened the dialog.
            seconds: How long to wait at most; None: the client's progress_timeout.
            cancel: Cancel the job (CANCEL_PROG) when it doesn't end in time. Only
                for a read: a write's job (a number entered) is never cancelled.

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
        if seconds is None:
            seconds = self._client.progress_timeout
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            progress = await self.send(
                screen, dialog_id, EventType.UPD_PROG, DataType.INT, job_id
            )
            if any(s.update == "DLG_CLOSE" for s in progress.screens):
                await self.poll()
                return
            if asyncio.get_running_loop().time() >= deadline:
                break
            await asyncio.sleep(self._client.progress_interval)
        msg = f"the iTM did not finish loading in time ({seconds:g} s)"
        err = ItmCommandError(msg)
        if cancel:
            await _cleanup_after(
                err,
                lambda: self.send(
                    screen, dialog_id, EventType.CANCEL_PROG, DataType.INT, job_id
                ),
            )
        raise err

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
        async with _cleanup(
            lambda: self.close_screen(MenuScreen.CHANGEOVER_SETTINGS),
            lambda: self.close_screen(MenuScreen.CHANGEOVER_GROUP),
            lambda: self.close_screen(MenuScreen.CHANGEOVER),
        ):
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

    async def read_changeover_candidates(self) -> list[tuple[str, str, str]]:
        """Read which points an auto changeover group could hold (needs a group)."""
        await self.open_menu_screen(MB_CHANGEOVER, MenuScreen.CHANGEOVER)
        async with _cleanup(
            lambda: self.close_screen(MenuScreen.CHANGEOVER_GROUP),
            lambda: self.close_screen(MenuScreen.CHANGEOVER),
        ):
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

    async def read_setback(self) -> SetbackSettings:
        """Read the setback offsets and the recovery temperatures."""
        await self.open_menu_screen(MB_SETBACK, MenuScreen.SETBACK)
        async with _cleanup(
            lambda: self.close_screen(MenuScreen.SETBACK_ADVANCED),
            lambda: self.close_screen(MenuScreen.SETBACK),
        ):
            await self.send(MenuScreen.SETBACK, SB_ADVANCED)
            settings = setback_from_screen(self.state)
            await self.close_screen(MenuScreen.SETBACK_ADVANCED)
            return settings

    async def read_overtime(self) -> int | None:
        """Read the overtime timer's duration in minutes."""
        await self.open_menu_screen(MB_OVERTIME, MenuScreen.OVERTIME)
        async with _cleanup(lambda: self.close_screen(MenuScreen.OVERTIME)):
            return overtime_from_screen(self.state)

    async def read_emergency_stop(self) -> list[EmergencyStopProgram]:
        """Read the emergency stop programs."""
        await self.open_menu_screen(MB_EMERGENCY_STOP, MenuScreen.EMERGENCY_STOP)
        async with _cleanup(lambda: self.close_screen(MenuScreen.EMERGENCY_STOP)):
            return emergency_stop_from_screen(self.state)

    async def read_schedules(self) -> list[ScheduleSummary]:
        """Read the schedule programs as the program list shows them."""
        await self.open_menu_screen(MB_SCHEDULE, MenuScreen.SCHEDULE)
        async with _cleanup(lambda: self.close_screen(MenuScreen.SCHEDULE)):
            return schedules_from_screen(self.state)

    async def _read_screen(
        self, button: int, screen: MenuScreen, read: Callable[[UIState], _T]
    ) -> _T | None:
        """Open a menu screen, read it and close it; None when it doesn't open."""
        if not await self.open_menu_screen(button, screen):
            return None
        async with _cleanup(lambda: self.close_screen(screen)):
            return read(self.state)

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
        self.require_writable()
        if event == EventType.CLICK and screen != ScreenID.SETTINGS:
            self.refuse_disabled(screen, part, label)
        if self._client.dry_run:
            self._client.planned.append(
                f"{label}: screen {screen} part {part} event {event}"
                + (f" {string!r}" if string else f" {number}" if number != -1 else "")
            )
            return None
        self.change_sent = True
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
        await self.answer_confirm(screen, reply)
        if screen in self.state.screens:
            msg = f"{label}: the screen did not close"
            raise ItmCommandError(msg)
        return True

    async def answer_confirm(self, screen: int, reply: Reply) -> None:
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
        async with _cleanup(lambda: self.close_screen(a), lambda: self.close_screen(s)):
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

    async def set_overtime(self, minutes: int) -> None:
        """Pick the overtime timer's duration and press OK."""
        await self.open_menu_screen(MB_OVERTIME, MenuScreen.OVERTIME)
        s = MenuScreen.OVERTIME
        async with _cleanup(lambda: self.close_screen(s)):
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

    async def select_row(self, screen: int, part: int, row: int) -> Reply:
        """Select a list row, as the browser does.

        A list sends its selection as an array of rows (the web UI's
        onSelectedMultiItem); the iTM ignores a plain number there.
        """
        return await self.send(
            screen, part, EventType.CLICK, DataType.INTARRAY, 1, array=(row,)
        )

    async def select_listed(
        self, screen: int, part: int, column: int, name: str, what: str
    ) -> None:
        """Select the list row whose column shows name; raise when it isn't there."""
        row = row_index(self.state, screen, part, column, name)
        if row is None:
            listed = [
                c[column].strip()
                for c in map(_cells, _rows(self.state, screen, part))
                if len(c) > column
            ]
            msg = f"{what}: {name!r} not found; available: {listed}"
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
            msg = "the iTM refused the service password"
            refused = ItmCommandError(msg)
            for dialog in reply.dialogs():
                await _cleanup_after(
                    refused, functools.partial(self.send, d.screen, dialog[0])
                )
            raise refused
        self.no_way_back("service mode")

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
        async with _cleanup(lambda: self.close_screen(MenuScreen.HISTORY)):
            await self.wait_for_progress(reply, cancel=True)
            if await _set_checkbox(
                self,
                MenuScreen.HISTORY,
                H_CLASS_MANAGEMENT,
                on=include_management,
                what="management history",
            ):
                await self.wait_for_progress(
                    await self.send(MenuScreen.HISTORY, H_REFRESH), cancel=True
                )
            if day is not None:
                await self._history_go_to(day, date_format)
            result = [history_from_screen(self.state)]
            moved = 0
            for _ in range(pages - 1):
                if self.state.get(MenuScreen.HISTORY, H_PAGE_OLDER, "ENABLED") != 1:
                    break
                await self._history_page(H_PAGE_OLDER)
                moved += 1
                page = history_from_screen(self.state)
                if not page.entries or page.page == result[-1].page:
                    break
                result.append(page)
            await self.step_back(
                moved,
                lambda: self._history_page(H_PAGE_NEWER),
                lambda: history_from_screen(self.state).page,
                result[0].page,
                "the history stayed on an older page",
            )
            return result

    async def _history_page(self, button: int) -> None:
        await self.wait_for_progress(
            await self.send(MenuScreen.HISTORY, button), cancel=True
        )

    async def _history_go_to(self, day: date, date_format: str | None) -> None:
        shown = parse_shown_date(
            self.state.get(MenuScreen.HISTORY, H_DATE, "TEXT"), date_format
        )
        if shown is None:
            msg = "the history's day could not be read"
            raise ItmCommandError(msg)
        if day == shown:
            return
        await _answer_date(self, MenuScreen.HISTORY, H_DAY_CHANGE, day, "history day")
        # The date input only changes the date shown; Refresh loads that day's list.
        await self.wait_for_progress(
            await self.send(MenuScreen.HISTORY, H_REFRESH), cancel=True
        )

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


def zone_name(step: str, icons: list[tuple[str, int, int]]) -> str:
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
