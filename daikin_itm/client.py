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
import ipaddress
import json
import logging
import os
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Self

import aiohttp

from . import users_edit
from .changeover import ChangeoverMixin
from .dealer_functions import DealerFunctionsMixin
from .demand import DemandMixin
from .emergency import EmergencyMixin
from .energy_edit import EnergyMixin
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
from .interlock import InterlockMixin
from .layout import LayoutMixin
from .menu import (
    ChangeoverGroup,
    EmergencyStopProgram,
    History,
    ItmConfig,
    ItmIndicators,
    ItmInfo,
    OptionalFunction,
    ScheduleDay,
    ScheduleProgram,
    ScheduleSummary,
    SetbackSettings,
)
from .models import (
    AIRFLOW_DIRECTIONS,
    DIALOG_SETTING_KEYS,
    FAN_SPEEDS,
    P_POWER,
    P_SETPOINT,
    AnalogPoint,
    Capabilities,
    ErrorInformation,
    PointKind,
    PowerState,
    RemoteLock,
    TempLimit,
    Unit,
    UnitSettings,
    capabilities_from_dialog,
    settings_from_dialog,
    unit_from_panel,
)
from .point_registry import PointRegistryMixin
from .protocol import MAX_REPLY_CHARS, NO_TOKEN, WEB_VERSION, ScreenID
from .schedule_edit import ScheduleMixin
from .service_edit import ServiceEditMixin
from .session import _T, ICON_ID_ZONE, _Session, _zone_name
from .settings_dialog import _run_to_completion
from .system import (
    NW_TEXT_FIELDS,
    HardwareSettings,
    NetworkSettings,
    ScreensaverMode,
    ScreensaverSettings,
    SystemSettings,
)
from .system_edit import SystemMixin
from .users_edit import UsersMixin
from .zones_edit import ZonesMixin

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable
    from datetime import date

    from .service import ServiceSettings


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

# How fetch_units reads the units: one operation panel per unit ("icons"), or the
# list view that shows every unit of the zone at once ("list").
UNIT_VIEWS = ("icons", "list")

# The top level of the icon view. Most iTMs group the units in zones; pass the path
# of zone names to them, see list_zones().
DEFAULT_ZONE_PATH: tuple[str, ...] = ()


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


async def _ignore(call: Awaitable[Any]) -> None:
    """Await a session write and drop its "sent" flag (dry runs plan instead)."""
    await call


def _power_is(unit: Unit, *, on: bool) -> bool:
    """Tell whether a unit is started (on) or stopped; setback is neither."""
    setback = unit.power_state in {
        PowerState.SETBACK,
        PowerState.SETBACK_LOW,
        PowerState.SETBACK_HIGH,
    }
    return unit.running == on and not setback


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
    InterlockMixin,
    ServiceEditMixin,
    ExportsMixin,
    EmergencyMixin,
    SystemMixin,
    UsersMixin,
    ScheduleMixin,
    EnergyMixin,
    ZonesMixin,
    DemandMixin,
    LayoutMixin,
    PointRegistryMixin,
    ChangeoverMixin,
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
                await users_edit.open_own_settings(s, self._username, self._password)
                yield s
            finally:
                await _run_to_completion(users_edit.leave_own_settings(s))

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
