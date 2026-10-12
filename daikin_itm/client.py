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
import hashlib
import json
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Self, TypeVar

import aiohttp

from .exceptions import (
    ItmCommandError,
    ItmError,
    LoginRejected,
    ProtocolError,
    SessionEnded,
    SetpointOutOfRange,
    VersionMismatch,
    ZoneNotFound,
)
from .models import (
    D_CANCEL,
    D_FAN_CHECK,
    D_FAN_ICON,
    D_FAN_UP,
    D_MODE,
    D_MODE_CHECK,
    D_OK,
    D_POWER_CHECK,
    D_POWER_START,
    D_POWER_STOP,
    DT_CLOSE,
    FAN_ICONS,
    FAN_SPEEDS,
    P_DETAILS_BUTTON,
    P_NAME,
    P_SETPOINT,
    P_SETTINGS_BUTTON,
    Capabilities,
    Unit,
    capabilities_from_dialog,
    details_from_screen,
    unit_from_panel,
)
from .protocol import (
    MAX_REPLY_CHARS,
    NO_TOKEN,
    WEB_VERSION,
    DataType,
    EventType,
    ScreenID,
    encode_event,
    is_token,
    parse_reply,
)
from .state import UIState

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Coroutine

    from .protocol import Reply

__all__ = [
    "DEFAULT_ZONE_PATH",
    "FileSessionStore",
    "ItmClient",
    "ItmCommandError",
    "ItmError",
    "LoginRejected",
    "MemorySessionStore",
    "SessionEnded",
    "SessionStore",
    "VersionMismatch",
    "ZoneNotFound",
]

_LOGGER = logging.getLogger(__name__)

# Icon view (screen 1) part IDs (ScMainStdIcon.initScreen in script.js).
ICON_BTN_TOP, ICON_BTN_DOWN, ICON_BTN_UP, ICON_PANEL = 1, 2, 3, 6
ICON_ID_ZONE = 1000
LOGIN_BUTTON = 4

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


class _Session:
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
    ) -> Reply:
        """Send a GUI event and merge the reply into the UI state."""
        body = encode_event(
            self.token, screen, part, event, data_type, number, string=string
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
        """Go down into a zone path, starting at the top level."""
        # Every session starts at the top level of the icon view, so the path is
        # absolute.
        for zone in path:
            names = [i[0] for i in self.icons()]
            if zone not in names:
                raise ZoneNotFound(zone, names)
            await self.select_by_name(zone)
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

    # -- operation panel ----------------------------------------------------------
    async def choose(self, part: int, index: int) -> None:
        """Pick an option of an operation panel control."""
        reply = await self.send(
            ScreenID.OPERATION, part, EventType.CLICK, DataType.INT, index
        )
        if reply.dialogs():
            msg = f"iTM opened a dialog: {reply.dialogs()}"
            raise ItmCommandError(msg)

    async def commit(self, delay: float) -> None:
        """Commit operation panel changes like the browser does."""
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

    async def read_details(self) -> dict[str, Any]:
        """Read the selected unit's identity from its Details screen."""
        reply = await self.send(ScreenID.OPERATION, P_DETAILS_BUTTON)
        if not any(
            s.screen == ScreenID.DETAILS and s.update == "OPEN" for s in reply.screens
        ):
            msg = f"details screen did not open: {reply.dialogs()}"
            raise ItmCommandError(msg)
        details = details_from_screen(self.state)
        await self.send(ScreenID.DETAILS, DT_CLOSE)
        return details

    @property
    def settings_open(self) -> bool:
        """Whether the settings dialog is open."""
        return ScreenID.SETTINGS in self.state.screens


class ItmClient:
    """Client for the web interface of a Daikin intelligent Touch Manager."""

    def __init__(  # noqa: PLR0913  # pylint: disable=too-many-arguments
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

        """
        # The iTM's web UI is plain HTTP by default, which exposes the (replayable)
        # login hash on the network; use https:// where the iTM offers it.
        base = host.rstrip("/") if "://" in host else "http://" + host
        self._url = base + "/cmd/"
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
        # Updated automatically if the iTM reports another one.
        self.web_version = WEB_VERSION
        self._lock = asyncio.Lock()  # one session at a time, also within this process
        self._last_write: dict[str, float] = {}
        self._unit_locks: dict[str, asyncio.Lock] = {}
        # Unconfirmed changes (written with confirm=False) by unit: what was changed
        # and how to recognize it.
        self._unconfirmed: dict[str, tuple[str, Callable[[Unit], bool]]] = {}

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

    # -- public API ---------------------------------------------------------------
    async def fetch_units(self) -> dict[str, Unit]:
        """Read every unit in the configured zone, in one short session.

        Returns
        -------
            The units by name.

        """
        async with self._session() as s:
            await s.open_zone(self.zone_path)
            return await s.read_units()

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
        pending = self._unconfirmed.pop(unit, None)
        if pending is None:
            return
        what, check = pending
        try:
            await self._confirm(unit, what, check)
        except ItmCommandError as err:
            _LOGGER.warning("%s", err)

    async def _finish(
        self, unit: str, what: str, check: Callable[[Unit], bool], *, confirm: bool
    ) -> Unit | None:
        if confirm:
            return await self._confirm(unit, what, check)
        self._unconfirmed[unit] = (what, check)
        return None

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
                if power is not None and current.running == power:
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
                    (power is None or u.running == power)
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
