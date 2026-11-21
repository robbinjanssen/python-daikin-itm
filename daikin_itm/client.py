"""Async client for the Daikin iTM web interface.

The iTM allows one web session per user, so this client never keeps one open: every
public method logs in, does its work and logs out (also on errors and cancellation).
A write and its read-back share that login (``confirm_in_session``). Only inside
``shared_session()`` the reads share one login, which ends with the block;
``kept_session()`` keeps that login alive for a polling client's lifetime. The page
token is handed to a SessionStore after every reply, so a session left behind by a
crash can be released on the next login instead of blocking the user until the iTM
times it out.
"""

from __future__ import annotations

import asyncio
import collections
import contextlib
import contextvars
import dataclasses
import functools
import ipaddress
import json
import logging
import math
import os
import tempfile
import time
import types
import weakref
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol, Self, TypeAlias
from urllib.parse import urlsplit

import aiohttp

from . import users_edit
from ._util import cleanup as _cleanup
from ._util import field_names as _field_names
from ._util import one_login as _one_login
from ._util import run_to_completion as _run_to_completion
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
    NotConfirmed,
    NotOffered,
    ProtocolError,
    ReadOnlyError,
    RequestSkipped,
    SessionEnded,
    SessionInUse,
    SetpointOutOfRange,
    SharedSessionError,
    UnitStopped,
    VersionMismatch,
    WriteBudgetExceeded,
    WriteOutcomeUnknown,
    WritesPaused,
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
    point_kind,
    settings_from_dialog,
    unit_from_panel,
)
from .point_registry import PointRegistryMixin
from .protocol import MAX_REPLY_CHARS, NO_TOKEN, WEB_VERSION, ScreenID, is_token
from .schedule_edit import ScheduleMixin
from .service_edit import ServiceEditMixin
from .session import ICON_ID_ZONE, _Session
from .session import zone_name as _zone_name
from .settings_dialog import settings_mismatch as _settings_mismatch
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
    from collections.abc import AsyncIterator, Awaitable, Callable, Iterable, Mapping
    from datetime import date

    from ._util import T as _T
    from .service import ServiceSettings

    # How to check a write: the check left to the next write, a read of the
    # unit or zone, and whether a read shows the change.
    _Check: TypeAlias = tuple[
        Callable[[], Awaitable[_T]], Callable[[], Awaitable[_T]], Callable[[_T], bool]
    ]


# An export (history, energy data) is a CSV file; refuse anything far larger.
MAX_EXPORT_BYTES = 32 * 1024 * 1024
# A session file holds a token, a host and a time; refuse anything far larger.
_MAX_STORE_BYTES = 64 * 1024

# What a zone holds: indoor and ventilation units, and analog inputs.
Point = Unit | AnalogPoint


class Points(dict[str, Point]):
    """What fetch_points read: the points by name, a dict like before.

    Two attributes besides, which == compares too when both sides are Points:

    - ``thermostat``: the thermostat status by name of the units fetch_points
      was asked to read it for (True on, False off, None when the unit shows
      none); empty when it wasn't asked.
    - ``indicators``: the main menu bar's indicators and clock as the session
      showed them at the end of the walk (no request of its own); None for
      Points built without them.
    """

    thermostat: dict[str, bool | None]
    indicators: ItmIndicators | None
    __hash__ = None

    def __init__(
        self,
        points: Mapping[str, Point] | None = None,
        *,
        thermostat: Mapping[str, bool | None] | None = None,
        indicators: ItmIndicators | None = None,
    ) -> None:
        """Hold the points with what the same walk read besides."""
        super().__init__(points or {})
        self.thermostat = dict(thermostat or {})
        self.indicators = indicators

    def __eq__(self, other: object) -> bool:
        """Compare the points and, against other Points, the rest too."""
        same = super().__eq__(other)
        if same is NotImplemented or not same:
            return same
        if isinstance(other, Points):
            return (self.thermostat, self.indicators) == (
                other.thermostat,
                other.indicators,
            )
        return True

    def __ne__(self, other: object) -> bool:
        """Tell whether == doesn't hold."""
        return not self == other


__all__ = [
    "DEFAULT_ZONE_PATH",
    "FileSessionStore",
    "ItmClient",
    "ItmCommandError",
    "ItmError",
    "LoginRejected",
    "MemorySessionStore",
    "Point",
    "Points",
    "SessionEnded",
    "SessionInfo",
    "SessionStore",
    "VersionMismatch",
    "ZoneNotFound",
]

_LOGGER = logging.getLogger(__name__)

# How fetch_units reads the units: one operation panel per unit ("icons"), or the
# list view that shows every unit of the zone at once ("list").
UNIT_VIEWS = ("icons", "list")

WHEN_STOPPED = ("refuse", "dialog")

# The points fetch_thermostat_status leaves out by default: no thermostat to read.
_NO_THERMOSTAT = frozenset({PointKind.VENTILATION, PointKind.ANALOG})

_HOUR, _DAY = 3600.0, 86400.0


def _unit_names(names: Iterable[str] | None) -> list[str] | None:
    """Return the unit names asked for, each once, or None for the default.

    Raises
    ------
        ItmCommandError: names is a string.

    """
    if isinstance(names, str):
        msg = f"pass a list of unit names, not the string {names!r}"
        raise ItmCommandError(msg)
    return None if names is None else list(dict.fromkeys(names))


def _thermostat_units(s: _Session, wanted: list[str] | None) -> list[str]:
    """Return the points of the open zone to read the thermostat status of.

    Raises
    ------
        ItmCommandError: wanted names a zone.

    """
    icons = {name: icon_id for name, icon_id, _ in s.icons()}
    if wanted is None:
        return [
            name
            for name, icon_id in icons.items()
            if icon_id != ICON_ID_ZONE and point_kind(icon_id) not in _NO_THERMOSTAT
        ]
    if zones := [name for name in wanted if icons.get(name) == ICON_ID_ZONE]:
        msg = f"{zones} are zones, not units"
        raise ItmCommandError(msg)
    return wanted


# A read that checks a sent change and fails with one of these hasn't seen it yet.
_NOT_SEEN_YET = (aiohttp.ClientError, TimeoutError, ProtocolError)

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
    A file that is damaged, not this store's or holds no page token loads as no
    token.
    """

    def __init__(self, path: str, host: str | None = None) -> None:
        """Initialize the store.

        Args:
        ----
            path: The file to keep the token in.
            host: The iTM the token belongs to, as ItmClient takes it ("host",
                "host:port" or a URL; compared without case, default port and
                trailing slash). None binds the token to no host: it is handed
                to whichever iTM the client using this store talks to.

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
            with Path(self.path).open("rb") as f:
                raw = f.read(_MAX_STORE_BYTES + 1)
        except FileNotFoundError:
            return None
        saved = _saved_session(raw)
        if saved is None:
            return None
        host, token = saved
        if self.host is not None and (
            host is None or _store_host(host) != _store_host(self.host)
        ):
            return None
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
                host = None if self.host is None else _store_host(self.host)
                json.dump({"host": host, "token": token, "saved_at": time.time()}, f)
            Path(tmp).replace(path)
        except BaseException as err:
            try:
                Path(tmp).unlink(missing_ok=True)
            except OSError as failed:
                _LOGGER.warning("could not remove %s: %s", tmp, failed)
                err.add_note(f"clean-up failed as well: {failed!r}")
            raise


def _saved_session(raw: bytes) -> tuple[str | None, str] | None:
    """Return the host and token a session file holds; None for anything else."""
    if len(raw) > _MAX_STORE_BYTES:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, RecursionError):
        return None
    if not isinstance(data, dict):
        return None
    token, host = data.get("token"), data.get("host")
    if not isinstance(token, str) or not is_token(token):
        return None
    return (host if isinstance(host, str) else None), token


@dataclasses.dataclass(frozen=True)
class SessionInfo:  # pylint: disable=too-many-instance-attributes
    """What a client knows about its login, for diagnostics (ItmClient.session_info).

    Read from the client alone: nothing is sent. Holds no token and no secret.

    Attributes
    ----------
        logged_in: A login of this client is open now (a kept, shared or write
            block's, or a call's).
        since: When that login was made (UTC); None when there is none.
        logins: How many logins this client made.
        last_poll: When the last POLLING got its answer (UTC), the kept session's
            keep-alive included.
        last_error: The last error a call, keep-alive poll or background check
            of a write without confirm raised (its type and message), or None.
        last_error_at: When it was raised (UTC).
        kept: A kept_session() block is open.
        writing: A write block (confirm_in_session) is open.
        unanswered: The last request of the open login got no answer (the next
            call tries the kept session's token again, or logs in anew).
        writes_paused: The web UI versions (before, now) of a change that
            pauses writes (pause_writes_on_version_change) until
            confirm_web_version(); None when writes aren't paused.

    """

    logged_in: bool
    since: datetime | None
    logins: int
    last_poll: datetime | None
    last_error: str | None
    last_error_at: datetime | None
    kept: bool
    writing: bool
    unanswered: bool
    writes_paused: tuple[str, str] | None = None


class _SharedBlock:  # pylint: disable=too-few-public-methods,too-many-instance-attributes
    """A shared, kept or write block: its session, since when, use."""

    def __init__(self, client: ItmClient, max_seconds: float) -> None:
        self.client = client
        self.max_seconds = max_seconds
        self.session: _Session | None = None
        self.since = 0.0
        # One call at a time, also from tasks started inside the block.
        self.lock = asyncio.Lock()
        self.closed = False
        # A kept_session(): writes allowed, a POLLING when idle, recovery by the
        # same token.
        self.kept = False
        self.last_used = 0.0
        # The POLLING before the current call got no answer: keep the session.
        self.unanswered = False
        # A write block (confirm_in_session): open while writes run, writes
        # allowed, the session lock taken by its first call (held).
        self.writing = False
        self.writers = 0
        self.held: contextlib.AsyncExitStack | None = None


@dataclasses.dataclass(frozen=True)
class _Left:
    """What a write left out because the unit showed it, for ItmClient._unchanged.

    name: the unit or zone written; sent: the units the rest was sent to;
    watched: every unit the write is about; locks: the call's unit locks.
    """

    name: str
    sent: list[str]
    watched: list[str]
    locks: contextlib.AsyncExitStack


class _UserLock:  # pylint: disable=too-few-public-methods
    """The session lock of one iTM user, shared by the clients on one event loop."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()


# The session locks by event loop, iTM and user. An entry lives as long as a call
# holds or waits for it.
_USER_LOCKS: weakref.WeakValueDictionary[
    tuple[asyncio.AbstractEventLoop, str, str], _UserLock
] = weakref.WeakValueDictionary()


def _check_status(resp: aiohttp.ClientResponse) -> None:
    """Raise for any HTTP status but 200; a redirect is never followed.

    Raises
    ------
        aiohttp.ClientResponseError: An error status (400 or more).
        ProtocolError: Any other status but 200, such as a redirect.

    """
    resp.raise_for_status()
    if resp.status != HTTPStatus.OK:
        msg = f"unexpected HTTP status {resp.status} from the iTM"
        raise ProtocolError(msg)


def _lock_origin(base: str) -> str:
    """Return the iTM a base URL names, as the key of its session lock."""
    try:
        url = urlsplit(base)
        port = url.port
    except ValueError:
        return base.lower()
    scheme = url.scheme.lower()
    if port == {"http": 80, "https": 443}.get(scheme):
        port = None
    origin = f"{scheme}://{url.hostname or ''}"
    if port is not None:
        origin += f":{port}"
    return origin + url.path.rstrip("/")


def _base_url(host: str) -> str:
    """Return the base URL of an iTM given as ItmClient takes it."""
    return host.rstrip("/") if "://" in host else "http://" + host


def _store_host(host: str) -> str:
    """Return the iTM a host names, as FileSessionStore compares it."""
    return _lock_origin(_base_url(host))


# The shared_session() blocks open in the current context. A task gets a copy of
# the context of the task that starts it, so a block belongs to the task that
# opened it and to the tasks started inside it.
_SHARED: contextvars.ContextVar[tuple[_SharedBlock, ...]] = contextvars.ContextVar(
    "daikin_itm_shared_sessions", default=()
)


async def _up_to_date(s: _Session) -> bool:
    """Bring a session at its start position up to date; False when it can't be used.

    The iTM only sends what changed, and for the screens that stay open (the main
    menu bar's clock and indicators) it does so in answer to the POLLING event,
    as for the browser. The answer also shows whether the iTM still knows the
    session.
    """
    if not s.at_start():
        return False
    try:
        await s.poll()
    except (ItmError, aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("shared session lost: %s", err)
        return False
    return s.at_start()


async def _kept_up_to_date(s: _Session) -> bool:
    """Bring a kept session up to date like _up_to_date; raise when it got no answer.

    A connection error or timeout leaves the session as it was, at its start
    position: the call raises, and the next one polls with the same token again.
    A session the iTM ended (or any other answer that can't be used) gives False:
    a new login.

    Raises
    ------
        aiohttp.ClientError: The POLLING got no answer.
        TimeoutError: The POLLING got no answer in time.

    """
    if not s.at_start():
        return False
    # A POLLING that got no answer before is tried again on purpose.
    s.unanswered = False
    try:
        await s.poll()
    except ItmError as err:
        _LOGGER.info("kept session lost: %s", err)
        return False
    return s.at_start()


async def _back_to_start(s: _Session) -> bool:
    """Bring a session back to its start position after a call; False: it can't be."""
    try:
        return await s.back_to_start()
    except (ItmError, aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("shared session: no way back to the start position (%s)", err)
        return False


def _system_fields(only: Iterable[str] | None) -> frozenset[str] | None:
    """Check the SystemSettings fields to read; None: all."""
    known = [f.name for f in dataclasses.fields(SystemSettings)]
    return _field_names(only, known, "system settings")


def _asked(
    *,
    power: bool | None,
    mode: str | None,
    fan_speed: str | None,
    setpoint: int | None = None,
) -> str:
    """Describe what an apply() asks, for on_write: "power on, mode heat"."""
    parts = [] if power is None else [f"power {'on' if power else 'off'}"]
    if mode is not None:
        parts.append(f"mode {mode}")
    if fan_speed is not None:
        parts.append(f"fan speed {fan_speed}")
    if setpoint is not None:
        parts.append(f"setpoint {setpoint}")
    return ", ".join(parts)


def _when_stopped(when_stopped: str) -> None:
    """Refuse an unknown when_stopped before anything is sent.

    Raises
    ------
        ValueError: when_stopped is not one of WHEN_STOPPED.

    """
    if when_stopped not in WHEN_STOPPED:
        msg = f"unknown when_stopped {when_stopped!r}, expected one of {WHEN_STOPPED}"
        raise ValueError(msg)


def _not_confirmed(
    msg: str, name: str, last_seen: object, error: Exception | None
) -> NotConfirmed:
    """Return NotConfirmed; after a read without an answer, of its error's type too."""
    if error is None:
        return NotConfirmed(msg, name=name, last_seen=last_seen)
    return NotConfirmed.after(msg, error, name=name, last_seen=last_seen)


def _every(check: Callable[[Unit], bool]) -> Callable[[list[Unit]], bool]:
    """Return a test that every unit of a list passes check."""
    return lambda units: all(check(u) for u in units)


def _power_is(unit: Unit, *, on: bool) -> bool:
    """Tell whether a unit is started (on) or stopped; setback is neither."""
    setback = unit.power_state in {
        PowerState.SETBACK,
        PowerState.SETBACK_LOW,
        PowerState.SETBACK_HIGH,
    }
    return unit.running == on and not setback


def _matches(
    u: Unit,
    *,
    power: bool | None = None,
    mode: str | None = None,
    fan_speed: str | None = None,
    setpoint: int | None = None,
) -> bool:
    """Tell whether a unit shows what an apply() asks."""
    return (
        (power is None or _power_is(u, on=power))
        and (mode is None or u.mode == mode)
        and (fan_speed is None or u.fan_speed == fan_speed)
        and (setpoint is None or u.setpoint == setpoint)
    )


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

    def __init__(  # noqa: PLR0913, PLR0915  # pylint: disable=too-many-arguments,too-many-locals,too-many-statements
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
        other_client_timeout: float | None = 60.0,
        confirm_in_session: bool = True,
        on_write: Callable[[str, str], None] | None = None,
        max_writes_per_unit_per_hour: int | None = None,
        max_writes_per_unit_per_day: int | None = None,
        pause_writes_on_version_change: bool = False,
        verify_timeout: float = 10.0,
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
                ExperimentalError before anything is sent, in a dry run too.
            other_client_timeout: Seconds a call waits while another client in this
                process uses the session of the same iTM and user, before it raises
                SessionInUse; None waits without limit. Calls of this client wait
                for each other without limit; the seconds count from when this
                client began to wait for the other one, so the calls it queued
                meanwhile fail together.
            confirm_in_session: Check a write in the login that made it: the write
                and the reads that confirm it take one login, and those reads
                only count what the iTM sends again, never the client's own
                pick. False: each of those reads logs in by itself (three to
                five logins for a unit write).
            on_write: Called with the unit's name and what was asked each time a
                unit or zone write sent its change (a zone write through the
                zone's panel: once per point of the zone; unit by unit: once per
                unit written), not for a write that found nothing to change or
                a dry run. An error it raises is logged.
            max_writes_per_unit_per_hour: Refuse a unit or zone write
                (WriteBudgetExceeded, nothing sent) to a unit that had this many
                writes sent by this client in the last hour; None: no limit.
            max_writes_per_unit_per_day: The same for the last 24 hours.
            pause_writes_on_version_change: When the iTM reports another web UI
                version than web_version (after a firmware update), refuse every
                write (WritesPaused, before its change is sent; a dry run too)
                until confirm_web_version() is called or the iTM reports the
                version known before again. Reads keep working. The write
                after all of a write's check (verify_timeout) is paused too:
                with confirm it raises, in the background check without
                confirm nothing is sent and the error is noted in
                session_info. False: the client adopts the new version and
                goes on.
            verify_timeout: Seconds a unit or zone write keeps reading the
                values it left out because the unit showed them already (every
                confirm_interval, at most confirm_timeout, at least one read):
                another change on its way may still move them. When one moves
                away, it is written after all, once. With confirm the call
                reads them before it returns and confirms a write after all
                (NotConfirmed when it still doesn't hold). Without, it returns
                at once and a background task reads them, in the write's login
                and holding the unit's lock (the next write to the unit waits
                for it); a write after all is checked by the next write to the
                unit, and an error is logged and noted in session_info, never
                raised. close() cancels the task, leaving kept_session() waits
                for it. A write that changes everything it was asked isn't
                held up by it.

        Raises:
        ------
            ValueError: other_client_timeout is not positive, or a write budget
                is below 1.

        """
        if other_client_timeout is not None and other_client_timeout <= 0:
            msg = f"other_client_timeout must be positive, not {other_client_timeout}"
            raise ValueError(msg)
        for name, limit in (
            ("max_writes_per_unit_per_hour", max_writes_per_unit_per_hour),
            ("max_writes_per_unit_per_day", max_writes_per_unit_per_day),
        ):
            if limit is not None and limit < 1:
                msg = f"{name} must be at least 1, not {limit}"
                raise ValueError(msg)
        # The iTM's web UI is plain HTTP by default, which exposes the (replayable)
        # login hash on the network; use https:// where the iTM offers it.
        base = _base_url(host)
        self._url = base + "/cmd/"
        self._base = base
        self._origin = _lock_origin(base)
        self._username, self._password = username, password
        self._http = session
        self._owns_http = session is None
        self._store: SessionStore = store or MemorySessionStore()
        self.zone_path = tuple(zone_path)
        self.commit_delay, self.min_write_interval = commit_delay, min_write_interval
        # Changes take a few seconds to reach the unit over the Daikin bus; a write is
        # only done once a read of what the iTM sends shows it. Sending the next
        # change before that can make the iTM drop one of them.
        self.confirm_timeout = confirm_timeout
        self.confirm_interval = confirm_interval
        self.verify_timeout = verify_timeout
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self.read_only = read_only
        # With dry_run, a write does everything up to the request that would make
        # the change, records that request in `planned` instead of sending it, and
        # cancels. Nothing on the iTM changes; the write methods return None.
        self.dry_run = dry_run
        # Writes not validated on a real iTM yet refuse unless this is set, in a
        # dry run too; see _experimental.
        self.allow_experimental = allow_experimental
        self.other_client_timeout = other_client_timeout
        self.confirm_in_session = confirm_in_session
        self.on_write = on_write
        self.max_writes_per_unit_per_hour = max_writes_per_unit_per_hour
        self.max_writes_per_unit_per_day = max_writes_per_unit_per_day
        self.planned: list[str] = []
        # Seconds between progress requests while the iTM loads a long list, and
        # how long it may take.
        self.progress_interval = 0.5
        self.progress_timeout = 30.0
        # Seconds one request may take (aiohttp's and the call's own bound).
        self.request_timeout = timeout
        # After a request got no answer only the logout is sent, bounded by this.
        self.logout_timeout = 5.0
        # Updated automatically if the iTM reports another one.
        self.web_version = WEB_VERSION
        self.pause_writes_on_version_change = pause_writes_on_version_change
        # The versions (before, now) of a change that pauses writes.
        self._version_change: tuple[str, str] | None = None
        # One session at a time for this client; _session_lock adds the user's.
        self._lock = asyncio.Lock()
        # Since when the calls of this client wait for another client.
        self._waiting_since: float | None = None
        # The open kept_session() block: every call of this client uses it.
        self._kept: _SharedBlock | None = None
        # The open write block (confirm_in_session): the calls of this client use
        # it unless a kept or shared block of theirs comes first.
        self._writing: _SharedBlock | None = None
        self._last_write: dict[str, float] = {}
        self._unit_locks: dict[str, asyncio.Lock] = {}
        # Unconfirmed changes (written with confirm=False) by unit: what was changed
        # and how to recognize it.
        self._unconfirmed: dict[str, Callable[[], Awaitable[object]]] = {}
        # The changes sent to each unit: in all, and when (the last day's).
        self._writes_sent: dict[str, int] = {}
        self._write_times: dict[str, collections.deque[float]] = {}
        # For session_info: the session of the last login and what happened.
        self._live: _Session | None = None
        self._since: datetime | None = None
        self._logins = 0
        self._last_poll: datetime | None = None
        self._last_error: tuple[str, datetime] | None = None
        # The background checks of what a write without confirm left out.
        self._watches: set[asyncio.Task[None]] = set()

    @property
    def session_info(self) -> SessionInfo:
        """Tell what the client knows about its login, for diagnostics.

        Nothing is sent; see SessionInfo.
        """
        live = self._live
        logged_in = live is not None and live.logged_in
        kept = self._kept is not None and not self._kept.closed
        writing = self._writing is not None and not self._writing.closed
        unanswered = (live is not None and logged_in and live.unanswered) or (
            self._kept is not None and self._kept.unanswered
        )
        error, error_at = self._last_error or (None, None)
        return SessionInfo(
            logged_in=logged_in,
            since=self._since if logged_in else None,
            logins=self._logins,
            last_poll=self._last_poll,
            last_error=error,
            last_error_at=error_at,
            kept=kept,
            writing=writing,
            unanswered=unanswered,
            writes_paused=self._version_change,
        )

    def confirm_web_version(self) -> None:
        """Resume writes after a web UI version change paused them.

        Call it once the iTM's new version (WritesPaused, session_info) is known
        to work with this library. Does nothing when writes aren't paused.
        """
        if self._version_change is not None:
            _LOGGER.info(
                "writes resume with iTM web UI version %r", self._version_change[1]
            )
        self._version_change = None

    def _version_changed(self, old: str, new: str) -> None:
        if not self.pause_writes_on_version_change:
            return
        before = old if self._version_change is None else self._version_change[0]
        if new == before:
            _LOGGER.warning("back to web UI version %r: writes resume", new)
            self._version_change = None
            return
        self._version_change = (before, new)
        _LOGGER.warning("writes are paused until confirm_web_version()")

    def _check_paused(self) -> None:
        if self._version_change is not None:
            raise WritesPaused(*self._version_change)

    @property
    def writes_sent(self) -> Mapping[str, int]:
        """Count the changes this client sent to each unit, by unit name (read only).

        A unit or zone write counts when its change was sent (a zone write
        through the zone's panel once for every point of the zone, unit by unit
        once for each unit written); a write that found nothing to change, a
        refused one and a dry run don't. A change whose reply was lost
        (WriteOutcomeUnknown) counts. Kept in memory for the client's lifetime.
        """
        return types.MappingProxyType(self._writes_sent)

    def recent_writes(self, seconds: float = _DAY) -> dict[str, int]:
        """Count the changes sent to each unit within the last seconds (at most a day).

        Counted like writes_sent; this is what the write budgets compare with.

        Args:
        ----
            seconds: The window, up to 86400 (a day); older writes are forgotten.

        Returns:
        -------
            The number of writes by unit name, for the units written to within it.

        """
        now = time.monotonic()
        counts = {
            unit: self._count_writes(unit, seconds, now) for unit in self._write_times
        }
        return {unit: n for unit, n in counts.items() if n}

    def _count_writes(self, unit: str, seconds: float, now: float) -> int:
        """Count a unit's writes in the last seconds; forget those over a day old."""
        times = self._write_times.get(unit)
        if not times:
            return 0
        while times and times[0] <= now - _DAY:
            times.popleft()
        return sum(1 for t in times if t > now - seconds)

    def _budgets(self) -> list[tuple[str, float, int]]:
        """Return the write budgets set: (the window's name, its seconds, the limit)."""
        return [
            (label, seconds, limit)
            for label, seconds, limit in (
                ("hour", _HOUR, self.max_writes_per_unit_per_hour),
                ("day", _DAY, self.max_writes_per_unit_per_day),
            )
            if limit is not None
        ]

    def _check_budget(self, units: Iterable[str]) -> None:
        """Refuse a write to units whose write budget is spent, before anything is sent.

        Raises
        ------
            WriteBudgetExceeded: A unit had as many writes as a budget allows.

        """
        if self.dry_run:
            return
        now = time.monotonic()
        for unit in units:
            for label, seconds, limit in self._budgets():
                if (count := self._count_writes(unit, seconds, now)) >= limit:
                    msg = (
                        f"{unit}: {count} writes in the last {label}, the budget "
                        f"allows {limit}; nothing was sent"
                    )
                    raise WriteBudgetExceeded(
                        msg, name=unit, limit=limit, seconds=seconds
                    )

    def _wrote(self, units: Iterable[str], what: str) -> None:
        """Count a change sent to units, warn at 80% of a budget, call on_write."""
        now = time.monotonic()
        for unit in units:
            self._writes_sent[unit] = self._writes_sent.get(unit, 0) + 1
            self._write_times.setdefault(unit, collections.deque()).append(now)
            self._count_writes(unit, _DAY, now)
            for label, seconds, limit in self._budgets():
                count = self._count_writes(unit, seconds, now)
                if count == math.ceil(limit * 0.8):
                    _LOGGER.warning(
                        "%s: %d writes sent in the last %s, %d allowed",
                        unit,
                        count,
                        label,
                        limit,
                    )
            if self.on_write is not None:
                try:
                    self.on_write(unit, what)
                except Exception:  # pylint: disable=broad-exception-caught
                    _LOGGER.warning("on_write failed for %s", unit, exc_info=True)

    def _logged_in(self, s: _Session) -> None:
        """Note a login, for session_info."""
        self._live = s
        self._since = datetime.now(UTC)
        self._logins += 1

    def _polled(self) -> None:
        """Note a POLLING that got its answer, for session_info."""
        self._last_poll = datetime.now(UTC)

    def _note_error(self, err: BaseException) -> None:
        """Note the error a call raised, for session_info."""
        text = f"{type(err).__name__}: {err}"
        self._last_error = (text[:300], datetime.now(UTC))

    @property
    def plain_http(self) -> bool:
        """Tell whether the client talks to the iTM over plain http.

        The web login sends only a hash of the password, but the passwords typed
        in the iTM go in clear over http: the service password, a web user's new
        password, the touch panel passwords and the e-mail server's password.
        The first one a client sends over http logs a warning.
        """
        return urlsplit(self._base).scheme.lower() == "http"

    async def close(self) -> None:
        """Close the client: stop its background checks, close its aiohttp session.

        A check of what a write without confirm left out that still runs is
        cancelled; its login is logged out before this returns. The aiohttp
        session is closed only if the client created it.
        """
        await self._end_watches(cancel=True)
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
            timeout=aiohttp.ClientTimeout(total=self.request_timeout),
            headers={"Content-Type": "text/plain"},
            allow_redirects=False,
        ) as resp:
            _check_status(resp)
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
        async with self._http.get(
            self._base + path, timeout=self._timeout, allow_redirects=False
        ) as resp:
            _check_status(resp)
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

        Nothing is released while a call or a logged-in shared_session() block of
        this client, or another client in this process, uses the session of the
        same iTM and user (from whichever task it is called): the saved token is
        that live session then.

        Returns
        -------
            True if there was one.

        Raises
        ------
            SessionInUse: Another client took the session just before, and kept
                it longer than other_client_timeout.

        """
        block = self._shared()
        if block is not None:
            async with block.lock:
                if block.session is not None:
                    return False
                return await self._release_saved_session()
        user = _USER_LOCKS.get(self._user_key())
        if self._lock.locked() or (user is not None and user.lock.locked()):
            return False
        async with self._session_lock():
            return await self._release_saved_session()

    async def _release_saved_session(self) -> bool:
        """Log out a saved session; the caller holds the session lock."""
        token = await self._store.load()
        if not token or token == NO_TOKEN:
            return False
        if not is_token(token):
            _LOGGER.warning("the session store held no page token; forgetting it")
            await self._store.save(None)
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

    def _user_key(self) -> tuple[asyncio.AbstractEventLoop, str, str]:
        return asyncio.get_running_loop(), self._origin, self._username

    @asynccontextmanager
    async def _session_lock(self) -> AsyncIterator[None]:
        """Hold the session: this client's lock, then the one of its iTM and user.

        The second is shared by every client of the same iTM and user on this
        event loop, so they take turns as the calls of one client do. Only one
        task per client waits for it, so the limit only counts the wait for
        another client. It counts from when this client began to wait for it:
        the calls queued behind that wait fail together.

        Raises
        ------
            SessionInUse: Another client kept the session longer than
                other_client_timeout.

        """
        queued = self._lock.locked()
        async with self._lock:
            if not queued:
                self._waiting_since = None
            key = self._user_key()
            user = _USER_LOCKS.get(key)
            if user is None:
                user = _USER_LOCKS[key] = _UserLock()
            if self._waiting_since is None:
                self._waiting_since = asyncio.get_running_loop().time()
            deadline = None
            if self.other_client_timeout is not None:
                deadline = self._waiting_since + self.other_client_timeout
            try:
                async with asyncio.timeout_at(deadline):
                    await user.lock.acquire()
            except TimeoutError:
                msg = (
                    "this user's session is in use by another client "
                    f"(still after {self.other_client_timeout} s)"
                )
                raise SessionInUse(msg) from None
            self._waiting_since = None
            try:
                yield
            finally:
                user.lock.release()

    def _shared(self) -> _SharedBlock | None:
        """Return the block a call uses: kept, this context's shared, then write."""
        if self._kept is not None and not self._kept.closed:
            return self._kept
        block = next(
            (b for b in _SHARED.get() if b.client is self and not b.closed), None
        )
        if block is None and self._writing is not None and not self._writing.closed:
            block = self._writing
        return block

    def _joinable(self) -> _SharedBlock | None:
        """Return the open kept or shared block a new block joins; not a write block."""
        block = self._shared()
        return None if block is None or block.writing else block

    @asynccontextmanager
    async def shared_session(self, max_seconds: float = 30.0) -> AsyncIterator[None]:
        """Let every read inside the block use one login instead of one each.

        The login is made by the first read and ends with the block (also on
        errors and cancellation). Between two reads the session goes back to
        where a login starts, and a read that raises ends it: the next read logs
        in again. So does a read after one that switched the main view (the
        units' list and the layout view), entered a value in a dialog, logged in
        to service mode, or moved a day overview or the history to another day or
        page.
        Writes are refused inside the block (SharedSessionError), and so are the
        calls that log in by themselves (the user's own settings).

        The block belongs to the task that opened it and to the tasks started
        inside it, which take turns; calls from other tasks wait until it ends,
        as they wait for a session otherwise, and so do the calls of other
        clients of the same iTM and user in this process (other_client_timeout).
        A write of this client still being checked (confirm_in_session) is
        waited for as well. A block opened inside a block joins it, with the
        outer block's rules (so inside a kept_session() it is one). Not
        validated on a real iTM yet.

        Args:
        ----
            max_seconds: How long one login may be used. A read that starts later
                logs a warning and logs in again: meanwhile the same user can't
                log in elsewhere.

        Raises:
        ------
            ValueError: max_seconds is not positive.

        """
        if max_seconds <= 0:
            msg = f"max_seconds must be positive, not {max_seconds}"
            raise ValueError(msg)
        if self._joinable() is not None:
            yield
            return
        async with self._session_lock():
            block = _SharedBlock(self, max_seconds)
            _SHARED.set((*_SHARED.get(), block))

            async def end() -> None:
                _SHARED.set(tuple(b for b in _SHARED.get() if b is not block))
                block.closed = True
                await _run_to_completion(self._end_shared(block))

            async with _cleanup(end):
                yield

    @asynccontextmanager
    async def kept_session(
        self, *, keep_alive: float | None = 60.0, max_age: float = 3 * 3600.0
    ) -> AsyncIterator[None]:
        """Keep one login for every call inside the block, for as long as it lasts.

        For a client that polls for its whole lifetime, such as Home Assistant:
        every call reuses one login, across calls and across time, instead of a
        login and logout (and two history entries) per poll. Reads work as in
        shared_session(): between two calls the session goes back to where a
        login starts, and a call that raises, or a read that only a new login
        undoes (the units' list and the layout view, a value entered in a
        dialog, service mode), ends the login; the next call logs in again.

        The iTM ends a session after 4 to 5 idle minutes. When no request was
        sent for keep_alive seconds, a background task sends one POLLING, as
        the browser does. A session the iTM ended (SessionEnded) is replaced by
        a new login on the next call, so callers see one extra login. A POLLING
        that gets no answer (a connection error or timeout) keeps the session:
        the next call polls with the same token again and goes on with it if the
        iTM still knows it, otherwise it logs in again, after releasing the old
        token (as release_saved_session does). A login older than max_age is
        replaced as well.

        Writes are allowed inside, and their dry runs; a write not validated on a
        real iTM yet still needs allow_experimental, as outside the block (in a
        dry run too), and the calls that log in by themselves (the user's own
        settings) stay refused (SharedSessionError).
        A write takes the same way as outside the block (per-unit lock,
        min_write_interval, confirm_timeout and confirm_interval) and reads its
        result back in this session, as a write with confirm_in_session does in
        its own: from screens the iTM sends again (the dialog opened anew, a
        panel value the iTM sent again while other icons were shown), never from
        the client's own picks, which the iTM doesn't echo; a pick it doesn't
        send again ends the login instead, so such a write costs one extra login.

        Validated on one real iTM: reads every minute for two hours matched
        another account's fresh reads; 20 idle minutes with the keep-alive kept
        one login; a network drop of 60 to 75 seconds didn't end the session; and
        unit, zone and menu writes, also writes another account overruled, read
        back in the kept session matched fresh reads. A restart of the iTM is
        tested against the simulated iTM only (it recovers as after the idle
        timeout, which was seen on the real one).

        Unlike shared_session(), the block belongs to the client: while it is
        open, every call of this client uses it, from whichever task (they take
        turns), and a shared_session() or kept_session() opened meanwhile joins
        it. The session lock is held for the whole block: other clients of this
        iTM and user in the process wait (SessionInUse after their
        other_client_timeout), and the user can't log in elsewhere. Use a
        dedicated administrator account for it. Leaving the block first waits
        for the background check of a write without confirm still under way
        (see verify_timeout; after an error or a cancellation in the block it
        is cancelled instead), then stops the keep-alive task and logs out,
        also on errors and cancellation.

        Args:
        ----
            keep_alive: Seconds without a request after which the background
                task sends a POLLING; None sends none.
            max_age: Seconds one login may be used; the next call after that
                logs in again.

        Raises:
        ------
            ValueError: keep_alive or max_age is not positive.

        """
        if keep_alive is not None and keep_alive <= 0:
            msg = f"keep_alive must be positive, not {keep_alive}"
            raise ValueError(msg)
        if max_age <= 0:
            msg = f"max_age must be positive, not {max_age}"
            raise ValueError(msg)
        if self._joinable() is not None:
            yield
            return
        async with self._session_lock():
            block = self._kept = _SharedBlock(self, max_age)
            block.kept = True
            keeper = None
            if keep_alive is not None:
                keeper = asyncio.create_task(self._keep_alive(block, keep_alive))
            failed = True

            async def close() -> None:
                block.closed = True
                self._kept = None
                await _run_to_completion(self._end_kept(block, keeper))

            async def end() -> None:
                async with _cleanup(close):
                    await self._end_watches(cancel=failed)

            async with _cleanup(end):
                yield
                failed = False

    async def _end_kept(
        self, block: _SharedBlock, keeper: asyncio.Task[None] | None
    ) -> None:
        """Stop a kept block's background task, then log its session out."""
        async with _cleanup(lambda: self._end_shared(block)):
            if keeper is not None:
                keeper.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await keeper

    async def _keep_alive(self, block: _SharedBlock, keep_alive: float) -> None:
        """Send a POLLING whenever a kept block's session was idle for keep_alive."""
        while not block.closed:
            wait = block.last_used + keep_alive - time.monotonic()
            if wait > 0:
                await asyncio.sleep(wait)
                continue
            async with block.lock:
                idle = time.monotonic() - block.last_used >= keep_alive
                if not block.closed and block.session is not None and idle:
                    try:
                        await self._keep_alive_poll(block, block.session)
                    except Exception:  # pylint: disable=broad-exception-caught
                        _LOGGER.warning("kept session: keep-alive poll", exc_info=True)
                block.last_used = time.monotonic()

    async def _keep_alive_poll(self, block: _SharedBlock, s: _Session) -> None:
        """Poll a kept block's idle session; drop it when the iTM ended it."""
        s.unanswered = False
        try:
            await s.poll()
        except SessionEnded as err:
            _LOGGER.info("kept session ended by the iTM: %s", err)
            self._note_error(err)
            block.session = None
            return
        except (ItmError, aiohttp.ClientError, TimeoutError) as err:
            # The session may well be alive: the next call polls it again first.
            _LOGGER.info("kept session: no answer to the keep-alive poll (%s)", err)
            self._note_error(err)
            block.unanswered = s.unanswered
            return
        _LOGGER.debug("kept session: keep-alive poll")
        if not s.at_start():
            try:
                await _run_to_completion(self._drop_shared(block))
            except (ItmError, aiohttp.ClientError, TimeoutError) as err:
                _LOGGER.info("kept session: logout not confirmed (%s)", err)

    async def _end_shared(self, block: _SharedBlock) -> None:
        """Log a block's session out, once a call still using it is done."""
        async with block.lock:
            await self._drop_shared(block)

    async def _shared_login(self, block: _SharedBlock) -> _Session:
        """Return a block's session at the start position, logging in when needed."""
        s = block.session
        if s is not None:
            if time.monotonic() - block.since > block.max_seconds:
                _LOGGER.log(
                    logging.INFO if block.kept else logging.WARNING,
                    "%s session open for more than %s s; logging in again",
                    "kept" if block.kept else "shared",
                    block.max_seconds,
                )
            elif block.kept:
                try:
                    if await _kept_up_to_date(s):
                        return s
                except (aiohttp.ClientError, TimeoutError):
                    block.unanswered = True
                    raise
            elif await _up_to_date(s):
                return s
            try:
                await _run_to_completion(self._drop_shared(block))
            except (aiohttp.ClientResponseError, ProtocolError) as e:
                # As for a session left behind: the iTM answered without confirming,
                # so give up on it rather than fail every read of the block.
                _LOGGER.warning(
                    "could not log out the shared session (%s); "
                    "waiting for the iTM to time it out",
                    e,
                )
                await self._store.save(None)
        if block.writing and block.held is None:
            held = contextlib.AsyncExitStack()
            await held.enter_async_context(self._session_lock())
            block.held = held
        await self._release_saved_session()
        s = block.session = self._new_session()
        s.shared = True
        s.kept = block.kept
        s.writing = block.writing
        if block.kept:
            _LOGGER.info("kept session: logging in")
        await s.login(self._username, self._password)
        s.mark_start()
        block.since = block.last_used = time.monotonic()
        return s

    @staticmethod
    async def _drop_shared(block: _SharedBlock) -> None:
        """Log a block's session out, if it has one; its next call logs in again."""
        s, block.session = block.session, None
        if s is not None:
            await s.logout()

    @asynccontextmanager
    async def _session(self) -> AsyncIterator[_Session]:
        """Hand out the session a call uses: a block's, or a login of its own.

        A transport error (or a reply that can't be read) after the request that
        makes the call's change was sent, also one of the clean-up or the logout,
        leaves the outcome unknown: it is raised as WriteOutcomeUnknown, of the
        transport error's type as well.

        Raises
        ------
            WriteOutcomeUnknown: The reply to a change, or a later one, was lost.

        """
        s: _Session | None = None
        try:
            async with self._call_session() as s:  # pylint: disable=contextmanager-generator-missing-cleanup
                s.change_sent = False
                yield s
        except (aiohttp.ClientError, TimeoutError, ProtocolError) as err:
            if (
                s is None
                or not s.change_sent
                or isinstance(
                    err,
                    (WriteOutcomeUnknown, aiohttp.ClientConnectorError, RequestSkipped),
                )
            ):
                self._note_error(err)
                raise
            msg = (
                f"the change was sent but a reply was lost ({err!r}): "
                "whether the iTM made it is unknown"
            )
            unknown = WriteOutcomeUnknown.after(msg, err)
            self._note_error(unknown)
            raise unknown from err
        except Exception as err:
            self._note_error(err)
            raise

    @asynccontextmanager
    async def _call_session(self) -> AsyncIterator[_Session]:
        """Return the block's session at its start position, or log in for the call."""
        block = self._shared()
        if block is not None:
            async with block.lock:
                if not block.closed:
                    # The block's session, brought back to the start position
                    # after the call. It is logged out instead when the call
                    # raised (its state is unknown then, also when cancelled) or
                    # when it can't be brought back: the next call logs in again.
                    keep = False
                    block.unanswered = False

                    async def end() -> None:
                        block.last_used = time.monotonic()
                        if not keep and not block.unanswered:
                            await _run_to_completion(self._drop_shared(block))

                    async with _cleanup(end):
                        s = await self._shared_login(block)
                        yield s
                        keep = await _back_to_start(s)
                    return
        async with self._session_lock():
            await self._release_saved_session()
            s = self._new_session()
            # A cancelled caller (e.g. HA shutting down) still frees the single
            # session, and the lock is held until it has, so the next session
            # can't start while this logout is still on its way.
            async with _cleanup(lambda: _run_to_completion(s.logout())):
                await s.login(self._username, self._password)
                yield s

    @asynccontextmanager
    async def _write_session(self) -> AsyncIterator[None]:
        """Run a write and the reads that check it in one login (confirm_in_session).

        A write block is a shared block that allows writes. Its first call takes
        the session lock and logs in; the reads that check the write come back to
        that login, through _session() like the reads of a kept session: at the
        start position, after a POLLING, reading only what the iTM sent (the
        panel shown again, a dialog or menu screen opened anew), never a pick of
        the client, which the iTM doesn't echo. A call that raised, a pick no
        other point sent over, or what only a new login undoes (a value entered
        in a dialog, service mode) ends the login, and the next read logs in
        again as it would without the block. Opening it sends nothing, so the
        write's own guards still refuse before any request.

        The block belongs to the client: other calls of the client join it (and
        take turns), a write among them too, and it ends, with a logout, when the
        last write in it returns (also on errors and cancellation), or when the
        background check a write without confirm left in it is done. A kept or
        shared block of the call comes first: inside kept_session() a write uses
        that, and inside shared_session() it is refused as before. With
        confirm_in_session off this does nothing.
        """
        block = self._shared()
        if block is not None and not block.writing:
            yield
            return
        if block is None:
            if not self.confirm_in_session:
                yield
                return
            block = self._writing = _SharedBlock(self, math.inf)
            block.writing = True
        block.writers += 1
        async with _cleanup(functools.partial(self._leave_write, block)):
            yield

    async def _leave_write(self, block: _SharedBlock) -> None:
        """Leave a write block; the last write (or check) in it logs it out."""
        block.writers -= 1
        if not block.writers:
            block.closed = True
            if self._writing is block:
                self._writing = None
            await _run_to_completion(self._end_write(block))

    async def _end_write(self, block: _SharedBlock) -> None:
        """Log a write block's session out, then let go of the session lock."""
        try:
            await self._end_shared(block)
        finally:
            if block.held is not None:
                await block.held.aclose()

    @asynccontextmanager
    async def _own_session(self) -> AsyncIterator[_Session]:
        """Log in through the login screen's "User Settings" into 1205 (own settings).

        Leaving 1205 ends the session (or, if the iTM keeps it, a logout does).
        A write block of another task is waited for, as for any other login.
        """
        if self._joinable() is not None:
            msg = (
                "the user's own settings need a login of their own; "
                "open them outside the shared session"
            )
            raise SharedSessionError(msg)
        async with self._session_lock():
            await self._release_saved_session()
            s = self._new_session()
            async with _cleanup(
                lambda: _run_to_completion(users_edit.leave_own_settings(s))
            ):
                await users_edit.open_own_settings(s, self._username, self._password)
                yield s

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
            units = await s.read_units()
            self._seen(s, units.values())
            return units

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
            async with _cleanup(lambda: s.close_settings(ok=False)):
                settings = settings_from_dialog(name, s.state, status)
                self._seen(s, [settings])
                return settings

    async def fetch_thermostat_status(
        self, names: Iterable[str] | None = None
    ) -> dict[str, bool | None]:
        """Read whether each unit's thermostat is calling for heating or cooling.

        One session: each unit is selected and its Details screen read, about three
        requests per unit (about 0.12 s per unit, measured with a scratch script on
        a real iTM). The status can't be derived from the room temperature and the
        setpoint. The caller decides which units are worth reading, e.g. only
        those running; an empty selection reads nothing and doesn't log in.

        Args:
        ----
            names: The units to read. When None, every point of the zone except
                its zones, ventilation units and analog inputs (so also an icon
                this client doesn't know).

        Returns:
        -------
            The status by unit name: True while the thermostat is on, False when
            it is off, None when the unit shows no thermostat status (a
            ventilation unit, an analog input).

        Raises:
        ------
            ItmCommandError: names is a string, or names a zone or a unit that is
                not in the zone.

        """
        wanted = _unit_names(names)
        if wanted == []:
            return {}
        async with self._session() as s:
            await s.open_zone(self.zone_path)
            wanted = _thermostat_units(s, wanted)
            status: dict[str, bool | None] = {}
            for name in wanted:
                await s.select_by_name(name)
                status[name] = (await s.read_detail_status())["thermo_on"]
            return status

    def _writable(self) -> None:
        if self.read_only:
            msg = "the client is read-only"
            raise ReadOnlyError(msg)
        self._check_paused()
        block = self._shared()
        if block is None or block.writing or block.kept:
            return
        msg = (
            "a shared session only reads (a change is verified in a fresh "
            "session); write outside the block"
        )
        raise SharedSessionError(msg)

    def _require_opt_in(self, *, allowed: bool, flag: str, why: str) -> None:
        """Refuse a risky write unless its opt-in is passed, a dry run too.

        Raises
        ------
            ReadOnlyError: The client is read-only.
            ItmCommandError: The opt-in is off.

        """
        self._writable()
        if not allowed:
            msg = f"{why}; pass {flag}=True"
            raise ItmCommandError(msg)

    def _job(self, what: str) -> None:
        """Refuse a call that makes the iTM run a job or produce a file, when read only.

        Called at the top of every export, before anything is sent; the backup
        is a write.

        Args:
        ----
            what: What the call makes the iTM do, e.g. "export the history".

        Raises:
        ------
            ReadOnlyError: The client is read-only.

        """
        if self.read_only:
            msg = f"{what}: the iTM runs a job for it; the client is read-only"
            raise ReadOnlyError(msg)

    def _experimental(self, what: str) -> None:
        """Refuse a write not validated on a real iTM yet, unless allowed.

        Called at the top of every such write, before anything is sent. A
        read-only client refuses first (ReadOnlyError). A dry run needs the
        opt-in too: it still sends the steps before the change (row selections,
        sub-screens, number entries), which are not validated either.

        Args:
        ----
            what: What the write does, e.g. "start a leakage check".

        Raises:
        ------
            ReadOnlyError: The client is read-only.
            ExperimentalError: allow_experimental is off.

        """
        self._writable()
        if self.allow_experimental:
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
            unit = unit_from_panel(s.state)
            self._seen(s, [unit])
            return unit

    async def fetch_points(
        self, *, details: bool = False, thermostat: bool | Iterable[str] = False
    ) -> Points:
        """Read every point in the configured zone, whatever its kind.

        Like fetch_units, but analog inputs come back as AnalogPoint instead of a
        Unit with nothing in it. Indoor and ventilation units are Units; see
        Unit.kind. The result also carries the main menu bar's indicators (no
        request of its own) and, when asked, the thermostat status read in the
        same walk: each unit's Details screen while it is selected anyway, two
        requests per unit instead of fetch_thermostat_status's three.

        Args:
        ----
            details: Also read each analog point's Details screen (what it watches
                and its limits; one request more per analog point).
            thermostat: The units to read the thermostat status of, by name; True
                for every point fetch_thermostat_status reads by default (all but
                ventilation units and analog inputs). In Points.thermostat.

        Returns:
        -------
            The points by name, with Points.thermostat and Points.indicators.

        Raises:
        ------
            ItmCommandError: thermostat is a string, or names a zone or a point
                that is not in the zone.

        """
        wanted = (
            None
            if thermostat is True
            else _unit_names(() if thermostat is False else thermostat)
        )
        async with self._session() as s:
            await s.open_zone(self.zone_path)
            names = _thermostat_units(s, wanted)
            if missing := sorted(set(names) - {icon[0] for icon in s.icons()}):
                msg = f"{missing} not found in the zone"
                raise ItmCommandError(msg)
            points, status = await s.read_points(
                details=details, thermostat=frozenset(names)
            )
            self._seen(s, points.values())
            return Points(points, thermostat=status, indicators=s.indicators())

    async def fetch_analog_values(self) -> dict[str, AnalogPoint]:
        """Read the present value of every analog input in the configured zone.

        Neither the operation panel nor the Details screen shows it: only the list
        view of a zone holding only analog inputs does, so the configured zone
        must be one. One request for the whole zone; unlike the units' list it
        keeps a shared or kept session (seen on one real iTM). Merge it with
        fetch_points by name.

        Returns
        -------
            The analog inputs by name with value and unit (as the list shows
            it); state_icon and alarm are None, the Details fields empty.

        Raises
        ------
            ProtocolError: The zone's list is not that of analog inputs only.

        """
        async with self._session() as s:
            await s.open_zone(self.zone_path)
            return await s.read_analog_from_list()

    async def fetch_indicators(self) -> ItmIndicators:
        """Read the main menu bar's indicators and clock, in a session of its own.

        Returns
        -------
            Whether energy saving, an error and the emergency stop are active,
            and whether the login showed the menu button (``menu_available``:
            False for a normal web user without allowed screens).

        """
        async with self._session() as s:
            return s.indicators()

    async def fetch_info(self) -> ItmInfo:
        """Read the iTM's firmware version, regional settings and clock.

        Returns
        -------
            The info; ``clock_offset`` tells how far the iTM's clock is off.

        """
        async with self._session() as s, _cleanup(s.close_menu):
            return await s.read_info()

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
        async with self._session() as s, _cleanup(s.close_menu):
            return await s.read_changeover_groups(details=details)

    async def fetch_setback(self) -> SetbackSettings:
        """Read the setback offsets and recovery temperatures."""
        async with self._session() as s, _cleanup(s.close_menu):
            return await s.read_setback()

    async def fetch_overtime_duration(self) -> int | None:
        """Read the overtime timer's duration in minutes (None when not shown)."""
        async with self._session() as s, _cleanup(s.close_menu):
            return await s.read_overtime()

    async def fetch_emergency_stop(self) -> list[EmergencyStopProgram]:
        """Read the emergency stop programs and whether they are armed."""
        async with self._session() as s, _cleanup(s.close_menu):
            return await s.read_emergency_stop()

    async def fetch_schedules(self) -> list[ScheduleSummary]:
        """Read the schedule programs: name, enabled and period."""
        async with self._session() as s, _cleanup(s.close_menu):
            return await s.read_schedules()

    @_one_login
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
            The settings as read back, or None in a dry run.

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
        async with self._session() as s, _cleanup(s.close_menu):
            await s.set_setback(changes)
        if self.dry_run:
            return None
        result = await self.fetch_setback()
        wrong = {
            k: getattr(result, k) for k, v in changes.items() if getattr(result, k) != v
        }
        if wrong:
            msg = f"setback: the iTM did not take {wrong}"
            raise NotConfirmed(msg, name="setback", last_seen=result)
        return result

    @_one_login
    async def set_overtime_duration(self, minutes: int) -> int | None:
        """Change the overtime timer's duration.

        Args:
        ----
            minutes: One of the durations the iTM offers (30 to 180, step 30).

        Returns:
        -------
            The duration as read back, or None in a dry run.

        """
        self._writable()
        async with self._session() as s, _cleanup(s.close_menu):
            await s.set_overtime(minutes)
        if self.dry_run:
            return None
        result = await self.fetch_overtime_duration()
        if result != minutes:
            msg = f"overtime: the iTM shows {result} min, not {minutes}"
            raise ItmCommandError(msg)
        return result

    @_one_login
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
            The program as read back, or None in a dry run.

        """
        self._writable()
        async with self._session() as s, _cleanup(s.close_menu):
            await s.set_schedule_enabled(name, enabled=enabled)
        if self.dry_run:
            return None
        program = next(
            (p for p in await self.fetch_schedules() if p.name == name), None
        )
        if program is None or program.enabled != enabled:
            msg = f"{name}: the iTM did not {'enable' if enabled else 'disable'} it"
            raise ItmCommandError(msg)
        return program

    @_one_login
    async def create_schedule(self, name: str) -> ScheduleSummary | None:
        """Create an empty, disabled schedule program.

        Args:
        ----
            name: The new program's name.

        Returns:
        -------
            The program as read back, or None in a dry run.

        """
        self._writable()
        async with self._session() as s, _cleanup(s.close_menu):
            await s.create_schedule(name)
        if self.dry_run:
            return None
        program = next(
            (p for p in await self.fetch_schedules() if p.name == name), None
        )
        if program is None:
            msg = f"{name}: the iTM did not create the program"
            raise ItmCommandError(msg)
        return program

    @_one_login
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
        async with self._session() as s, _cleanup(s.close_menu):
            await s.delete_schedule(name)
        if not self.dry_run and any(
            p.name == name for p in await self.fetch_schedules()
        ):
            msg = f"{name}: the iTM did not delete the program"
            raise ItmCommandError(msg)

    @_one_login
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
        confirm: bool = True,
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
            confirm: Wait until the change shows on the unit. Without, the next
                write to the same unit first waits until this change shows.

        The last eleven are offered only by some units (see DIALOG_SETTINGS); one the
        unit doesn't offer is refused before anything in the dialog is clicked. A
        setting the dialog already shows is left as it is (a reset never is) and
        read until verify_timeout has passed (without confirm in the
        background): when another change on its way moves it away, it is
        written after all, once (see ItmClient).

        Returns:
        -------
            The unit's settings as read back; as read last when nothing differed
            (as the write read them without confirm or in a dry run). None in a
            dry run (that changes something), when not confirmed and something
            was sent, or when nothing was asked.

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

        left: set[str] = set()

        async def change(s: _Session) -> UnitSettings | None:
            todo = {k: changes[k] for k in left} if left else changes
            left.clear()
            shown = await s.change_unit_settings(todo, unit, skipped=left)
            if shown is None or not self.dry_run:
                return shown
            status = await s.read_detail_status()
            return dataclasses.replace(
                shown,
                thermo_on=status["thermo_on"],
                r32_alarm=status["r32_alarm"],
                flags=dict(status["flags"]),
            )

        def kept() -> Callable[[UnitSettings], bool] | None:
            shown = {k: changes[k] for k in left}
            return (lambda r: not _settings_mismatch(r, shown)) if shown else None

        return await self._write_unit(
            unit,
            "settings " + ", ".join(changes),
            change,
            (
                functools.partial(self._confirm_settings, unit, changes),
                functools.partial(self.fetch_unit_settings, unit),
                lambda r: not _settings_mismatch(r, changes),
            ),
            confirm=confirm,
            skipped=kept,
        )

    async def _confirm_settings(
        self, unit: str, changes: dict[str, Any]
    ) -> UnitSettings:
        """Read a unit's settings until a change shows.

        Like the other unit writes, the change takes a few seconds to reach the
        unit over the Daikin bus.
        """
        checked = await self._until(
            functools.partial(self.fetch_unit_settings, unit),
            lambda r: not _settings_mismatch(r, changes),
        )
        result, error = checked
        if result is None:
            msg = f"{unit}: the settings could not be read back: {error!r}"
            raise _not_confirmed(msg, unit, None, error) from error
        if wrong := _settings_mismatch(result, changes):
            msg = f"{unit}: the iTM did not take {wrong}"
            raise _not_confirmed(msg, unit, result, error) from error
        return result

    @_one_login
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
            The unit as read back, also when it was in that state already
            (nothing is written then, unless it moves away within
            verify_timeout), or None when not confirmed or in a dry run.

        Raises:
        ------
            ItmCommandError: The unit doesn't offer that state.

        """
        self._writable()
        what = f"power {state}"

        def check(u: Unit) -> bool:
            return u.power_state == state

        async def change(s: _Session) -> Unit | None:
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
                raise NotOffered(msg, name=unit, value=state, options=order)
            if current.power_state == state:
                return current
            await s.choose(P_POWER, order.index(state), confirm=True)
            await s.commit(self.commit_delay)
            return None

        return await self._write_unit(
            unit, what, change, self._unit_check(unit, what, check), confirm=confirm
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
        async with self._session() as s, _cleanup(s.close_menu):
            return await s.read_schedule(name, events=events, details=details)

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
        async with self._session() as s, _cleanup(s.close_menu):
            return await s.read_schedule_day(name, days_ahead=days_ahead)

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
        async with self._session() as s, _cleanup(s.close_menu):
            await s.open_menu()
            return await s.read_history(
                day=day, include_management=include_management, pages=pages
            )

    async def fetch_optional_functions(self) -> dict[str, OptionalFunction]:
        """Probe the functions the menu hides and list what they hold (experimental).

        Returns
        -------
            By function name: whether its screen opened and the rows it listed.

        """
        async with self._session() as s, _cleanup(s.close_menu):
            return await s.read_optional_functions()

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

        Raises:
        ------
            ItmCommandError: Only is a string, or names an unknown field.

        """
        wanted = _system_fields(only)
        async with self._session() as s, _cleanup(s.close_menu):
            return await s.read_system(wanted)

    @_one_login
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
            The settings as read back, or None in a dry run.

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
        async with self._session() as s, _cleanup(s.close_menu):
            await s.set_hardware(changes)
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
            raise NotConfirmed(msg, name="hardware", last_seen=result)
        return result

    @_one_login
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
            The settings as read back, or None in a dry run.

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
        async with self._session() as s, _cleanup(s.close_menu):
            await s.set_screensaver(changes)
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
            raise NotConfirmed(msg, name="screensaver", last_seen=result)
        return result

    async def _in_session(self, call: Callable[[_Session], Awaitable[_T]]) -> _T:
        """Run one session call and close the menu after it."""
        async with self._session() as s, _cleanup(s.close_menu):
            return await call(s)

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
        True, it is refused, a dry run too; a dry run opens each input to check it
        and cancels it, without entering anything.

        Args:
        ----
            controller_name: The controller's name.
            hostname: Its host name.
            ip_address: Its IPv4 address.
            subnet_mask: The subnet mask.
            gateway: The default gateway.
            dns_primary: The primary DNS server.
            dns_secondary: The secondary DNS server.
            allow_network_change: Really change it; needed for a dry run too.

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
        if not allow_network_change:
            msg = "a network change can strand the iTM; pass allow_network_change=True"
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

    async def fetch_config(self, *, system: bool | Iterable[str] = False) -> ItmConfig:
        """Read everything configuration-like in one session.

        The indicators and clock, version and region, the schedule programs, the
        auto changeover groups (without details), setback, overtime and the
        emergency stop programs; with system, the system settings of fetch_system
        too. About twenty requests (fifty with all system screens); meant for an
        hourly read, not every poll.

        Args:
        ----
            system: Also read the system settings screens: True for all of them,
                or the SystemSettings fields to read (as fetch_system's only).

        Returns:
        -------
            The configuration.

        Raises:
        ------
            ItmCommandError: System is a string, or names an unknown field.

        """
        wanted = None if isinstance(system, bool) else _system_fields(system)
        async with self._session() as s, _cleanup(s.close_menu):
            indicators = s.indicators()
            info = await s.read_info()
            schedules = tuple(await s.read_schedules())
            groups = tuple(await s.read_changeover_groups())
            setback = await s.read_setback()
            overtime = await s.read_overtime()
            emergency_programs = tuple(await s.read_emergency_stop())
            if system is True:
                settings = await s.read_system()
            elif wanted:
                settings = await s.read_system(wanted)
            else:
                settings = None
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

    async def _until(
        self,
        read: Callable[[], Awaitable[_T]],
        done: Callable[[_T], bool],
        seconds: float | None = None,
    ) -> tuple[_T | None, Exception | None]:
        """Read every confirm_interval until done, for up to seconds.

        That is confirm_timeout unless given.

        A read that fails on its way (a transport error, a reply that can't be
        read) hasn't seen the change yet: the next one tries again, also in a
        write block, whose login that error ended. The first read is always made,
        a later one only while time is left. A read under way isn't cut at the
        deadline (that would end a kept login): a hung iTM ends it after one
        request_timeout.

        Returns
        -------
            The last value read (None: no read succeeded) and the last error of
            a read since. The caller checks done on the value again.

        """
        deadline = time.monotonic() + (
            self.confirm_timeout if seconds is None else seconds
        )
        seen: _T | None = None
        error: Exception | None = None
        first = True
        while True:
            await asyncio.sleep(self.confirm_interval)
            if not first and time.monotonic() >= deadline:
                return seen, error
            first = False
            try:
                value = await read()
            except _NOT_SEEN_YET as err:
                _LOGGER.debug("a check of a change got no answer: %r", err)
                error = err
                continue
            seen, error = value, None
            if done(value):
                return value, None

    async def _confirm(
        self, unit: str, what: str, check: Callable[[Unit], bool]
    ) -> Unit:
        """Read a unit until a change shows."""
        checked = await self._until(functools.partial(self.fetch_unit, unit), check)
        u, error = checked
        if u is not None and check(u):
            return u
        seen = (
            f"no read succeeded: {error!r}"
            if u is None
            else f"last seen: power={u.power} mode={u.mode} fan={u.fan_speed} "
            f"setpoint={u.setpoint}"
        )
        msg = f"{unit}: {what} not confirmed within {self.confirm_timeout}s ({seen})"
        raise _not_confirmed(msg, unit, u, error) from error

    def _seen(self, s: _Session, points: Iterable[object]) -> None:
        """Drop the unchecked changes a read shows made, so no write waits for them.

        Only a read of what the iTM sent counts: not one in a write block (a
        session that made a write), nor one while the panel or the settings
        dialog holds a pick of the client. In a kept session the read comes after
        the way back to the start, which shows each unit again until no pick is
        left. A zone write's change is dropped per indoor unit that shows it.
        """
        if not self._unconfirmed or s.writing:
            return
        if s.state.picked(ScreenID.OPERATION) or s.state.picked(ScreenID.SETTINGS):
            return
        for point in points:
            name = getattr(point, "name", None)
            if not isinstance(name, str):
                continue
            later = self._unconfirmed.get(name)
            if later is not None and self._shows(later, point):
                del self._unconfirmed[name]

    def _shows(self, later: Callable[[], Awaitable[object]], point: object) -> bool:
        """Tell whether a point read shows the change the check later waits for."""
        if (
            not isinstance(later, functools.partial)
            or getattr(later.func, "__self__", None) is not self
        ):
            return False
        name = getattr(later.func, "__name__", "")
        if name == "_confirm":
            return isinstance(point, Unit) and bool(later.args[2](point))
        if name == "_confirm_zone":
            return (
                isinstance(point, Unit)
                and point.kind is PointKind.INDOOR
                and bool(later.args[2](point))
            )
        if name == "_confirm_settings":
            return isinstance(point, UnitSettings) and not _settings_mismatch(
                point, later.args[1]
            )
        return False

    async def _settle(self, unit: str) -> None:
        """Wait until an earlier unconfirmed change to this unit shows.

        So the next change can't overtake it. Callers hold _unit_lock(unit). A change
        that never shows is logged, not raised: it belongs to an earlier call that
        has already returned.
        """
        confirm = self._unconfirmed.pop(unit, None)
        if confirm is None:
            return
        self._forget(confirm)
        try:
            await confirm()
        except ItmCommandError as err:
            _LOGGER.warning("%s", err)

    def _forget(self, later: Callable[[], Awaitable[object]]) -> None:
        """Drop a check from _unconfirmed, for every unit it was left for."""
        for unit in [k for k, v in self._unconfirmed.items() if v is later]:
            del self._unconfirmed[unit]

    async def _write_unit(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        unit: str,
        what: str,
        change: Callable[[_Session], Awaitable[_T | None]],
        check: _Check[_T],
        *,
        confirm: bool,
        skipped: Callable[[], Callable[[_T], bool] | None] | None = None,
    ) -> _T | None:
        """Make a write to a unit under its lock, and check it.

        change gets the session with the unit selected and sends the change, or
        returns what the unit shows when that already holds (sending nothing).
        A change sent is checked by check's later (left to the next write
        without confirm). A change found made already, or the parts of it that
        skipped says change left out, are checked by _unchanged. change runs
        again for a write after all and then sends only what it left out.
        """
        later, _, holds = check
        self._check_budget([unit])
        async with contextlib.AsyncExitStack() as locks:
            await locks.enter_async_context(self._unit_lock(unit))
            self._check_budget([unit])
            await self._settle(unit)
            self._check_budget([unit])
            async with self._write(unit, later, what) as s:
                shown = await change(s)
            if shown is not None:
                kept: Callable[[_T], bool] | None = holds
            else:
                kept = skipped() if skipped else None
            if kept is None:
                if self.dry_run:
                    return None
                if not confirm:
                    self._unconfirmed[unit] = later
                    return None
                return await later()

            async def again(_last: _T) -> None:
                self._check_budget([unit])
                async with self._write(unit, later, what) as s:
                    await change(s)

            return await self._unchanged(
                _Left(unit, [unit], [unit], locks),
                check,
                kept,
                again,
                shown=shown,
                confirm=confirm,
            )

    async def _unchanged(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        left: _Left,
        check: _Check[_T],
        kept: Callable[[_T], bool],
        again: Callable[[_T], Awaitable[None]],
        *,
        shown: _T | None,
        confirm: bool,
    ) -> _T | None:
        """Check what a write left out because the unit showed it; send it after all.

        With confirm the call checks it (_verify) before it returns. Without,
        it returns at once and a background task (_watch) checks it, holding
        the unit locks (left.locks: the call's, handed over) and the write
        block's login until it is done. shown: what the unit showed when
        nothing was sent; None when the rest was sent, for left.sent, whose
        check is left to the next write to them without confirm (registered
        first, so an error or a cancellation keeps it).

        Returns
        -------
            With confirm what _verify returns. Without: shown (None when the
            rest was sent).

        """
        if self.dry_run:
            return shown
        later = check[0]
        if shown is None and not confirm:
            self._unconfirmed.update(dict.fromkeys(left.sent, later))
        verify = functools.partial(
            self._verify, left, check, kept, again, shown=shown, confirm=confirm
        )
        if confirm:
            return await verify()
        self._watch(left.name, verify, left.locks.pop_all())
        return shown

    async def _verify(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        left: _Left,
        check: _Check[_T],
        kept: Callable[[_T], bool],
        again: Callable[[_T], Awaitable[None]],
        *,
        shown: _T | None,
        confirm: bool,
    ) -> _T | None:
        """Read what a write left out for verify_timeout; send it after all if it moves.

        The iTM shows another client's change only once it reached the unit
        (3-8 s), so what was left out is read every confirm_interval for
        verify_timeout (at most confirm_timeout, at least one read), in the
        write's login and unit locks. When it moves away (kept fails), again
        sends it after all, once (counted and budgeted as any write); with
        confirm, later then confirms the whole write; without, that check is
        left to the next write to left.watched.

        Returns
        -------
            The value read last when nothing was sent and nothing moved; the
            write as confirmed; None without confirm when something was sent.

        Raises
        ------
            NotConfirmed: No read got an answer, or the write after all or
                what was sent doesn't show.
            WriteBudgetExceeded: The write after all is over the budget.
            WritesPaused: Writes were paused (a web UI version change) before
                the write after all; nothing is sent.

        """
        later = check[0]

        def moved(value: Any) -> bool:
            return not kept(value)

        checked: tuple[_T | None, Exception | None] = await self._until(
            check[1], moved, min(self.verify_timeout, self.confirm_timeout)
        )
        last, error = checked
        if last is None:
            msg = f"{left.name}: could not check what it showed already: {error!r}"
            raise _not_confirmed(msg, left.name, None, error) from error
        if kept(last):
            if shown is not None:
                return last
            return await later() if confirm else None
        _LOGGER.info(
            "%s: another change came in after the read; writing after all", left.name
        )
        self._check_paused()
        await again(last)
        if not confirm:
            self._unconfirmed.update(dict.fromkeys(left.watched, later))
            return None
        try:
            result = await later()
        except ItmCommandError:
            self._forget(later)
            raise
        self._forget(later)
        return result

    def _watch(
        self,
        name: str,
        run: Callable[[], Awaitable[object]],
        held: contextlib.AsyncExitStack,
    ) -> None:
        """Run a check in the background, holding the unit locks and the write block.

        A write block the call runs in stays open (as one more writer) until
        the check is done, so the check reads in its login and the block logs
        out only then.
        """
        block = self._shared()
        if block is not None and block.writing:
            block.writers += 1
        else:
            block = None
        task = asyncio.create_task(self._watching(name, run, held, block))
        self._watches.add(task)
        task.add_done_callback(self._watches.discard)

    async def _watching(
        self,
        name: str,
        run: Callable[[], Awaitable[object]],
        held: contextlib.AsyncExitStack,
        block: _SharedBlock | None,
    ) -> None:
        """Run a background check; release its unit locks, then leave its write block.

        An error is logged and noted for session_info, never raised: no call
        waits for it. What it sent stays in _unconfirmed for the next write.
        """
        steps = [] if block is None else [functools.partial(self._leave_write, block)]
        try:
            async with _cleanup(*steps), held:
                await run()
        except Exception as err:  # pylint: disable=broad-exception-caught
            _LOGGER.warning(
                "%s: the check of what was left out failed: %s",
                name,
                err,
                exc_info=True,
            )
            self._note_error(err)

    async def _end_watches(self, *, cancel: bool) -> None:
        """Wait until the background checks are done; with cancel, cancel them first.

        Cancelled meanwhile, it still cancels them and waits until they have
        released their locks and logged out, then raises the cancellation.
        """
        while self._watches:
            tasks = set(self._watches)
            if cancel:
                await _run_to_completion(self._stop_watches(tasks))
                continue
            try:
                await asyncio.wait(tasks)
            except asyncio.CancelledError:
                await _run_to_completion(self._stop_watches(tasks))
                raise

    @staticmethod
    async def _stop_watches(tasks: set[asyncio.Task[None]]) -> None:
        """Cancel background checks and wait until they are done."""
        # A task cancelled before its first step would skip its clean-up.
        await asyncio.sleep(0)
        for task in tasks:
            task.cancel()
        await asyncio.wait(tasks)

    # -- whole zones ------------------------------------------------------------------
    @asynccontextmanager
    async def _write_zone(
        self,
        zone: str,
        members: list[str],
        later: Callable[[], Awaitable[object]],
        what: str,
    ) -> AsyncIterator[_Session]:
        """Open the zone path and select a zone's icon: its panel operates them all.

        A change whose outcome is unknown (WriteOutcomeUnknown, or a cancellation
        after it was sent) is checked by later before the next write to a member.
        A change sent counts as a write to every member (what: for on_write).
        """
        sent: _Session | None = None
        try:
            async with self._session() as s:  # pylint: disable=contextmanager-generator-missing-cleanup
                sent = s
                await s.open_zone(self.zone_path)
                name = _zone_name(zone, s.icons())
                if not any(i[0] == name and i[1] == ICON_ID_ZONE for i in s.icons()):
                    msg = f"{zone!r} is not a zone"
                    raise ItmCommandError(msg)
                await s.select_by_name(name)
                yield s
        except WriteOutcomeUnknown as err:
            self._outcome_unknown(err, members, later)
            raise
        except asyncio.CancelledError:
            if sent is not None and sent.change_sent:
                for member in members:
                    self._unconfirmed[member] = later
            raise
        finally:
            if sent is not None and sent.change_sent:
                now = time.monotonic()
                for member in members:
                    self._last_write[member] = now
                self._wrote(members, what)

    def _outcome_unknown(
        self,
        err: WriteOutcomeUnknown,
        units: list[str],
        later: Callable[[], Awaitable[object]],
    ) -> None:
        """Let the next write to these units wait for a change of unknown outcome."""
        for unit in units:
            self._unconfirmed[unit] = later
        err.add_note(
            f"the next write to {', '.join(units)} first waits until the change shows"
        )

    async def _zone_members(
        self, zone: str, *, read: bool = False
    ) -> tuple[list[str], dict[str, Unit]]:
        """Return the points directly in a zone below the zone path.

        With read, also read them (the icon view: each point's operation panel).
        """
        async with self._session() as s:
            await s.open_zone(self.zone_path)
            name = _zone_name(zone, s.icons())
            if not any(i[0] == name and i[1] == ICON_ID_ZONE for i in s.icons()):
                msg = f"{zone!r} is not a zone"
                raise ItmCommandError(msg)
            await s.open_zone((name,))
            members = [i[0] for i in s.icons() if i[1] != ICON_ID_ZONE]
            units = await s.read_units() if read else {}
            self._seen(s, units.values())
            return members, units

    async def _zone_write(  # noqa: PLR0913  # pylint: disable=too-many-arguments,too-many-locals
        self,
        zone: str,
        what: str,
        needs: Callable[[Unit], dict[str, Any]],
        send: Callable[[_Session, dict[str, Any] | None], Awaitable[None]],
        *,
        confirm: bool,
        done: Callable[[list[Unit]], bool] | None = None,
        checked: str | None = None,
    ) -> dict[str, Unit] | None:
        """Write to the points of a zone, with the per-unit write rules.

        needs tells what an indoor unit lacks of the write (nothing: it passes).
        Holds each member's unit lock (in name order, so two zone writes can't
        deadlock), lets an earlier unconfirmed change to a member show first,
        then reads the members. When done (by default: no indoor unit lacks
        anything) holds already, nothing is sent and _unchanged checks it.
        Otherwise the zone's panel sends it when every indoor unit lacks the
        same, or when the zone holds other points or wasn't read whole (send
        gets the zone selected and what they lack, None for all that was
        asked); else each indoor unit that lacks something gets only that,
        through its own panel, and _unchanged checks the ones left out. Writes
        are budgeted and spaced like a unit write, and confirmed on every
        indoor unit (checked names it in the error), or left to the next write
        to each point written without confirm.
        """

        def check(u: Unit) -> bool:
            return not needs(u)

        later = functools.partial(self._confirm_zone, zone, checked or what, check)
        read = functools.partial(self.fetch_zone_units, zone)
        test = done or _every(check)

        def holds(units: dict[str, Unit]) -> bool:
            indoor = [u for u in units.values() if u.kind is PointKind.INDOOR]
            return bool(indoor) and test(indoor)

        def lacking(
            units: dict[str, Unit], names: list[str]
        ) -> dict[str, dict[str, Any]]:
            return {n: f for n in names if n in units and (f := needs(units[n]))}

        members, _ = await self._zone_members(zone)
        async with contextlib.AsyncExitStack() as stack:
            for member in sorted(set(members)):
                await stack.enter_async_context(self._unit_lock(member))
            for member in members:
                await self._settle(member)
            _, units = await self._zone_members(zone, read=True)
            indoor = [n for n, u in units.items() if u.kind is PointKind.INDOOR]
            apart = set(units) == set(members) and len(indoor) == len(units)

            async def write(todo: dict[str, dict[str, Any]]) -> list[str]:
                fields = {k: v for f in todo.values() for k, v in f.items()}
                if (
                    apart
                    and todo
                    and (
                        set(todo) != set(indoor)
                        or any(f != fields for f in todo.values())
                    )
                ):
                    self._check_budget(todo)
                    for name, lacks in todo.items():
                        async with self._write(name, later, what, zone=zone) as s:
                            await send(s, lacks)
                    return list(todo)
                self._check_budget(members)
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
                async with self._write_zone(zone, members, later, what) as s:
                    await send(s, (fields or None) if apart else None)
                return members

            async def verify(
                left: list[str], written: list[str], shown: dict[str, Unit] | None
            ) -> dict[str, Unit] | None:
                def kept(found: dict[str, Unit]) -> bool:
                    if shown is not None:
                        return holds(found)
                    return all(check(found[n]) for n in left if n in found)

                async def again(last: dict[str, Unit]) -> None:
                    await write(lacking(last, left))

                return await self._unchanged(
                    _Left(zone, written, members, stack),
                    (later, read, holds),
                    kept,
                    again,
                    shown=shown,
                    confirm=confirm,
                )

            start = time.monotonic()
            try:
                if set(units) == set(members) and holds(units):
                    return await verify(indoor, [], units)
                written = await write(lacking(units, indoor))
                if left := [n for n in indoor if n not in written]:
                    return await verify(left, written, None)
                if self.dry_run:
                    return None
                if not confirm:
                    for member in members:
                        self._unconfirmed[member] = later
                    return None
                return await later()
            finally:
                end = time.monotonic()
                for member in members:
                    if self._last_write.get(member, -math.inf) >= start:
                        self._last_write[member] = end

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
            units = await s.read_units()
            self._seen(s, units.values())
            return units

    async def _confirm_zone(
        self, zone: str, what: str, check: Callable[[Unit], bool]
    ) -> dict[str, Unit]:
        """Wait until every indoor unit in the zone shows a change."""

        def done(units: dict[str, Unit]) -> bool:
            indoor = [u for u in units.values() if u.kind is PointKind.INDOOR]
            return bool(indoor) and all(check(u) for u in indoor)

        checked = await self._until(
            functools.partial(self.fetch_zone_units, zone), done
        )
        units, error = checked
        if units is not None and done(units):
            return units
        if units is None:
            msg = f"{zone}: {what} not confirmed: no read succeeded: {error!r}"
            raise _not_confirmed(msg, zone, None, error) from error
        pending = [
            u.name
            for u in units.values()
            if u.kind is PointKind.INDOOR and not check(u)
        ]
        msg = f"{zone}: {what} not confirmed on {pending}"
        raise _not_confirmed(msg, zone, units, error) from error

    @_one_login
    async def set_zone_power_state(
        self, zone: str, state: PowerState, *, confirm: bool = True
    ) -> dict[str, Unit] | None:
        """Put every unit of a zone on, off, or in setback low/high.

        Through the zone's panel when every indoor unit lacks it, otherwise
        through the panel of each one that does; the ones that show it already
        are left out and checked (see verify_timeout).

        Args:
        ----
            zone: The zone's name, below the client's zone path.
            state: on / off / setback_low / setback_high.
            confirm: Wait until every indoor unit in the zone shows it.

        Returns:
        -------
            The zone's points as read back, also when every indoor unit
            already showed the state (nothing is written then, unless one moves
            away within verify_timeout). None when not confirmed or in a dry
            run.

        """
        self._writable()
        what = f"power {state}"

        def needs(u: Unit) -> dict[str, Any]:
            return {} if u.power_state == state else {"state": state}

        async def send(s: _Session, _lacks: dict[str, Any] | None) -> None:
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
                raise NotOffered(msg, name=zone, value=state, options=order)
            await s.choose(P_POWER, order.index(state), confirm=True)
            await s.commit(self.commit_delay)

        return await self._zone_write(zone, what, needs, send, confirm=confirm)

    @_one_login
    async def set_zone_setpoint(
        self, zone: str, value: int, *, confirm: bool = True
    ) -> dict[str, Unit] | None:
        """Give every running unit of a zone the same setpoint.

        Through the zone's panel when every indoor unit runs with another
        setpoint, otherwise through the panel of each running one that does;
        the ones that have it are left out and checked (see verify_timeout).

        Args:
        ----
            zone: The zone's name, below the client's zone path.
            value: The setpoint, one the zone's panel offers.
            confirm: Wait until every running indoor unit in the zone shows it.

        Returns:
        -------
            The zone's points as read back, also when every running indoor
            unit (at least one) already had the setpoint (nothing is written
            then, unless one moves away within verify_timeout). None when not
            confirmed or in a dry run.

        """
        self._writable()
        what = f"setpoint {value}"

        def needs(u: Unit) -> dict[str, Any]:
            return {} if not u.running or u.setpoint == value else {"setpoint": value}

        def done(indoor: list[Unit]) -> bool:
            return any(u.running for u in indoor) and not any(map(needs, indoor))

        async def send(s: _Session, _lacks: dict[str, Any] | None) -> None:
            current = unit_from_panel(s.state)
            if not current.running:
                raise UnitStopped(zone)
            nums = list(current.setpoint_range)
            if value not in nums:
                raise SetpointOutOfRange(zone, value, current.setpoint_range)
            await s.choose(P_SETPOINT, nums.index(value))
            await s.commit(self.commit_delay)

        return await self._zone_write(
            zone, what, needs, send, confirm=confirm, done=done
        )

    @_one_login
    async def apply_zone(
        self,
        zone: str,
        *,
        power: bool | None = None,
        mode: str | None = None,
        fan_speed: str | None = None,
        confirm: bool = True,
    ) -> dict[str, Unit] | None:
        """Change power, mode and/or fan speed of every unit in a zone.

        Through the zone's settings dialog when every indoor unit lacks the
        same, otherwise through the dialog of each unit that lacks something,
        with only what it lacks, like apply() does for one unit; what a unit
        shows already is left out and checked (see verify_timeout).

        Args:
        ----
            zone: The zone's name, below the client's zone path.
            power: Start (True) or stop (False) them.
            mode: heat / cool / fan / dry / auto, as the zone's dialog offers.
            fan_speed: low / medium / high / auto.
            confirm: Wait until every indoor unit in the zone shows the change.

        Returns:
        -------
            The zone's points as read back, also when every indoor unit
            already showed the change (nothing is written then, unless one
            moves away within verify_timeout). None when not confirmed, in a
            dry run, or when nothing was asked.

        """
        if power is None and mode is None and fan_speed is None:
            return None
        self._writable()
        if fan_speed is not None and fan_speed not in FAN_SPEEDS:
            msg = (
                f"{zone}: unknown fan speed {fan_speed!r}, expected one of {FAN_SPEEDS}"
            )
            raise ItmCommandError(msg)

        asked: dict[str, Any] = {"power": power, "mode": mode, "fan_speed": fan_speed}

        def needs(u: Unit) -> dict[str, Any]:
            lacks: dict[str, Any] = {}
            if power is not None and not _power_is(u, on=power):
                lacks["power"] = power
            if mode is not None and u.mode != mode:
                lacks["mode"] = mode
            if fan_speed is not None and u.fan_speed not in {None, fan_speed}:
                lacks["fan_speed"] = fan_speed
            return lacks

        async def send(s: _Session, lacks: dict[str, Any] | None) -> None:
            to = asked if lacks is None else lacks
            await s.change_settings(
                zone,
                power=to.get("power"),
                mode=to.get("mode"),
                fan_speed=to.get("fan_speed"),
            )

        what = _asked(power=power, mode=mode, fan_speed=fan_speed)
        return await self._zone_write(
            zone, what, needs, send, confirm=confirm, checked="change"
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

    @_one_login
    async def set_setpoint(
        self,
        unit: str,
        value: int,
        *,
        confirm: bool = True,
        when_stopped: str = "refuse",
    ) -> Unit | None:
        """Change a unit's setpoint.

        A running unit takes it from the operation panel. The iTM ignores the
        panel's setpoint while the unit is stopped, so by default that is refused;
        with when_stopped="dialog" it goes through the unit's settings dialog
        instead, which the iTM keeps for when the unit starts (seen on a real iTM
        in heating and cooling).

        Args:
        ----
            unit: The unit's name.
            value: The new setpoint: one of the unit's setpoint_range, or for a
                stopped unit within the settings dialog's range (the unit's
                setpoint limit).
            confirm: Wait until the change shows on the unit. Without, the next
                write to the same unit first waits until this change shows.
            when_stopped: "refuse" (UnitStopped) or "dialog", see above.

        Returns:
        -------
            The unit as read back once the change shows, also when it showed
            already (nothing is written then, unless it moves away within
            verify_timeout), or None when not confirmed.

        Raises:
        ------
            ItmCommandError: The unit is stopped (UnitStopped), the value is out of
                range (SetpointOutOfRange), or the change did not show in time.
            ValueError: An unknown when_stopped.

        """
        self._writable()
        _when_stopped(when_stopped)
        what = f"setpoint {value}"

        def check(u: Unit) -> bool:
            return u.setpoint == value

        async def change(s: _Session) -> Unit | None:
            current = unit_from_panel(s.state)
            if current.running:
                nums = list(current.setpoint_range)
                if value not in nums:
                    raise SetpointOutOfRange(unit, value, current.setpoint_range)
                if current.setpoint == value:
                    return current
                await s.choose(P_SETPOINT, nums.index(value))
                await s.commit(self.commit_delay)
            elif when_stopped == "refuse":
                raise UnitStopped(unit)
            elif current.setpoint == value:
                return current
            else:
                await s.change_settings(
                    unit, power=None, mode=None, fan_speed=None, setpoint=value
                )
            return None

        return await self._write_unit(
            unit, what, change, self._unit_check(unit, what, check), confirm=confirm
        )

    async def fetch_capabilities(self) -> dict[str, Capabilities]:
        """Read every unit's identity and which modes and fan control it offers.

        The identity comes from the Details screen, the rest from the settings dialog
        (cancelled). This costs five requests per unit (select, Details open and
        close, settings open and cancel), so do it once rather than on every poll;
        Capabilities.to_dict and from_dict keep it across restarts.

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

    @_one_login
    async def apply(  # noqa: PLR0913  # pylint: disable=too-many-arguments
        self,
        unit: str,
        *,
        power: bool | None = None,
        mode: str | None = None,
        fan_speed: str | None = None,
        confirm: bool = True,
        setpoint: int | None = None,
        when_stopped: str = "refuse",
    ) -> Unit | None:
        """Change power, mode, fan speed and/or setpoint of a unit in one go.

        This goes through the unit's settings dialog, one write for all of them.

        Args:
        ----
            unit: The unit's name.
            power: Start (True) or stop (False) the unit.
            mode: heat / cool / fan / dry / auto, as offered by the unit (see
                fetch_capabilities).
            fan_speed: low / medium / high / auto.
            confirm: Wait until the change shows on the unit. Without, the next
                write to the same unit first waits until this change shows.
            setpoint: The new setpoint, within the range of the settings
                dialog's number input (the unit's setpoint limit), as it shows
                after a mode change in the same call.
            when_stopped: For a setpoint while the unit is stopped and stays
                stopped: "refuse" (UnitStopped) or "dialog" (write it; the iTM
                keeps it for when the unit starts), as in set_setpoint.

        Returns:
        -------
            The unit as read back once the change shows, also when it showed
            already (nothing is written then, unless it moves away within
            verify_timeout), or None when not confirmed or nothing was asked to
            change.

        Raises:
        ------
            ItmCommandError: The unit does not offer the change (NotOffered,
                SetpointOutOfRange), is stopped (UnitStopped), or the change did
                not show in time.
            ValueError: An unknown when_stopped.

        """
        if power is None and mode is None and fan_speed is None and setpoint is None:
            return None
        self._writable()
        _when_stopped(when_stopped)
        if fan_speed is not None and fan_speed not in FAN_SPEEDS:
            msg = (
                f"{unit}: unknown fan speed {fan_speed!r}, expected one of {FAN_SPEEDS}"
            )
            raise ItmCommandError(msg)

        asked: dict[str, Any] = {
            "power": power,
            "mode": mode,
            "fan_speed": fan_speed,
            "setpoint": setpoint,
        }
        left: dict[str, Any] = {}

        async def change(s: _Session) -> Unit | None:
            # Compare with the unit as read in this session, not with what the
            # caller last saw: only send what differs, and nothing at all (no
            # write to the unit) if it already is in the requested state. A
            # write after all sends only from what the first one left out.
            want = dict(left) or asked
            current = unit_from_panel(s.state)
            runs = bool(current.running) if power is None else power
            if setpoint is not None and not runs and when_stopped == "refuse":
                raise UnitStopped(unit)
            on = want.get("power")
            to_power = None if on is None or _power_is(current, on=on) else on
            to_mode = None if current.mode == want.get("mode") else want.get("mode")
            fan = want.get("fan_speed")
            to_fan = None if current.fan_speed == fan else fan
            temp = want.get("setpoint")
            to_setpoint = None if current.setpoint == temp else temp
            to = (to_power, to_mode, to_fan, to_setpoint)
            if to == (None, None, None, None):
                return current
            left.clear()
            left.update(
                (k, v)
                for (k, v), sent in zip(asked.items(), to, strict=True)
                if v is not None and sent is None
            )
            await s.change_settings(
                unit,
                power=to_power,
                mode=to_mode,
                fan_speed=to_fan,
                setpoint=to_setpoint,
            )
            return None

        def kept() -> Callable[[Unit], bool] | None:
            return functools.partial(_matches, **left) if left else None

        what = _asked(power=power, mode=mode, fan_speed=fan_speed, setpoint=setpoint)
        check = functools.partial(_matches, **asked)
        return await self._write_unit(
            unit,
            what,
            change,
            self._unit_check(unit, "change", check),
            confirm=confirm,
            skipped=kept,
        )

    def _unit_check(
        self, unit: str, what: str, check: Callable[[Unit], bool]
    ) -> _Check[Unit]:
        """Return how to check a write to a unit's power, mode, fan or setpoint."""
        return (
            functools.partial(self._confirm, unit, what, check),
            functools.partial(self.fetch_unit, unit),
            check,
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
    async def _write(
        self,
        unit: str,
        later: Callable[[], Awaitable[object]],
        what: str,
        zone: str | None = None,
    ) -> AsyncIterator[_Session]:
        """Select a unit for a write; later checks a change of unknown outcome.

        The unit is found below the zone path, or in zone there. A change of
        unknown outcome is one whose reply was lost (WriteOutcomeUnknown) or
        whose call was cancelled after it was sent. Callers hold _unit_lock(unit).
        Space out writes per unit: the indoor units may store settings in memory
        with a limited number of writes. A change sent is counted (what: for on_write);
        callers check the write budget before they settle.
        """
        wait = (
            self._last_write.get(unit, 0) + self.min_write_interval - time.monotonic()
        )
        if wait > 0:
            await asyncio.sleep(wait)
        sent: _Session | None = None
        try:
            async with self._session() as s:  # pylint: disable=contextmanager-generator-missing-cleanup
                sent = s
                path = self.zone_path if zone is None else (*self.zone_path, zone)
                await s.open_zone(path)
                await s.select_by_name(unit)
                yield s
        except WriteOutcomeUnknown as err:
            self._outcome_unknown(err, [unit], later)
            raise
        except asyncio.CancelledError:
            if sent is not None and sent.change_sent:
                self._unconfirmed[unit] = later
            raise
        finally:
            if sent is not None and sent.change_sent:
                self._last_write[unit] = time.monotonic()
                self._wrote([unit], what)
