# pylint: disable=protected-access,redefined-outer-name
"""ItmClient.shared_session against the simulated iTM: one login for many reads."""

from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import inspect
import logging
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest

from daikin_itm import (
    EventSpec,
    ItmClient,
    ItmCommandError,
    LoginRejected,
    MemorySessionStore,
    PointKind,
    Points,
    ProtocolError,
    ReadOnlyError,
    RuleRange,
    SharedSessionError,
    Unit,
)
from daikin_itm.menu import MB_HISTORY, MenuScreen
from daikin_itm.protocol import ScreenID

from .fake_dealer_functions import FakeDealerITM, Group, Interlock
from .fake_demand import FakeDemandITM
from .fake_emergency import FakeEmergencyITM, Program
from .fake_energy_more import FakeEnergyMoreITM
from .fake_exports import FakeExportsITM
from .fake_itm import FakeGroup, FakeITM
from .fake_layout import FakeLayoutItem, FakeLayoutITM
from .fake_point_registry import FakeRegistryITM
from .fake_service_edit import FakeServiceEditITM
from .fake_system_edit import FakeSystemEditITM
from .fake_users import FakeUsersITM
from .fake_zones import FakeZonesITM
from .test_client import FakeClient, http_error, is_logout

if TYPE_CHECKING:
    from collections.abc import Callable

SERVICE = "test-service"


@dataclass(frozen=True)
class Read:
    """How to call a reader: on which simulated iTM, with which arguments."""

    make: Callable[[], FakeITM] = FakeITM
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)
    client: dict[str, Any] = field(default_factory=dict)
    # The read leaves the session where only a new login helps: the next read
    # in the block logs in again.
    ends_session: bool = False


def _with_groups() -> FakeITM:
    itm = FakeITM()
    itm.groups = [FakeGroup("Group A", members=["1:1-00"]), FakeGroup("Group B")]
    return itm


def _with_error() -> FakeITM:
    itm = FakeITM()
    itm.errors = [["1:1-04", "A3"]]
    return itm


def _with_layout() -> FakeLayoutITM:
    return FakeLayoutITM(
        [
            FakeLayoutItem(10, 20, icon_id=2114, mode=0, unit="Unit A"),
            FakeLayoutItem(300, 20, text="Floor 1"),
        ],
        title="Level 0",
    )


def _with_stop_program() -> FakeEmergencyITM:
    itm = FakeEmergencyITM()
    itm.programs.append(Program("Fire alarm", inputs=[itm.points[0]]))
    itm.start_stop("Fire alarm")
    return itm


def _with_dealer_functions() -> FakeDealerITM:
    itm = FakeDealerITM()
    itm.interlocks.append(
        Interlock("Program A", inputs=[itm.points[0]], outputs=[[itm.points[2]], []])
    )
    itm.slides.append(Group("Hall", members=[itm.points[0]]))
    itm.limits.append(Group("Offices", members=[itm.points[1]]))
    return itm


def _with_history_pages() -> FakeITM:
    itm = FakeITM()
    itm.history_page_size = 1
    return itm


# Every public read of the client. A read that is missing here fails
# test_every_read_is_listed, so a new one can't go untested in a shared session.
READS: dict[str, tuple[Read, ...]] = {
    "export_energy_data": (Read(FakeExportsITM, ("01/2024-12/2024",)),),
    "export_history": (
        Read(
            FakeExportsITM,
            (date(2024, 12, 20), date(2025, 1, 1)),
            ends_session=True,
        ),
    ),
    "export_leakage_check_results": (
        Read(FakeServiceEditITM, (SERVICE,), ends_session=True),
    ),
    "export_point_list": (
        Read(
            FakeExportsITM,
            (date(2024, 11, 20), date(2024, 12, 19)),
            ends_session=True,
        ),
    ),
    "export_setup": (Read(FakeExportsITM),),
    "fetch_active_emergency_stops": (Read(_with_stop_program),),
    "fetch_capabilities": (Read(),),
    "fetch_changeover_groups": (
        Read(_with_groups),
        Read(_with_groups, kwargs={"details": True}),
    ),
    "fetch_config": (Read(), Read(kwargs={"system": True})),
    "fetch_demand_control": (Read(FakeDemandITM),),
    "fetch_demand_service_settings": (
        Read(FakeDemandITM, (SERVICE,), ends_session=True),
    ),
    "fetch_dst": (Read(),),
    "fetch_email_server": (Read(FakeSystemEditITM),),
    "fetch_emergency_stop": (Read(),),
    "fetch_emergency_stop_settings": (Read(_with_stop_program, ("Fire alarm",)),),
    "fetch_energy_navigator": (
        Read(FakeEnergyMoreITM),
        Read(
            FakeEnergyMoreITM,
            (["installer"],),
            {"service_password": SERVICE},
            ends_session=True,
        ),
    ),
    "fetch_energy_saving": (
        Read(FakeDemandITM),
        Read(FakeDemandITM, (SERVICE,), ends_session=True),
    ),
    "fetch_error_information": (Read(), Read(_with_error)),
    "fetch_heating_optimisation": (Read(_with_dealer_functions),),
    "fetch_history": (
        Read(),
        Read(kwargs={"include_management": True, "pages": 3}),
        Read(_with_history_pages, kwargs={"include_management": True, "pages": 3}),
        Read(kwargs={"day": date(2024, 12, 31)}, ends_session=True),
    ),
    "fetch_indicators": (Read(), Read(_with_error)),
    "fetch_info": (Read(),),
    "fetch_interlock_setup": (
        Read(_with_dealer_functions, ("Program A",), {"details": True}),
    ),
    "fetch_interlocks": (Read(_with_dealer_functions),),
    "fetch_language": (Read(),),
    "fetch_layout": (
        Read(FakeLayoutITM, ends_session=True),
        Read(_with_layout, kwargs={"names": True}, ends_session=True),
    ),
    "fetch_layout_setup": (Read(FakeServiceEditITM, (SERVICE,), ends_session=True),),
    "fetch_leakage_check": (
        Read(FakeServiceEditITM, (SERVICE,), {"details": True}, ends_session=True),
    ),
    "fetch_leakage_check_registration": (
        Read(FakeServiceEditITM, (SERVICE, "OU-1"), ends_session=True),
    ),
    "fetch_leakage_check_results": (
        Read(FakeServiceEditITM, (SERVICE,), ends_session=True),
    ),
    "fetch_leakage_check_schedule": (
        Read(FakeServiceEditITM, (SERVICE, "OU-1"), ends_session=True),
    ),
    "fetch_optional_functions": (Read(),),
    "fetch_overtime_duration": (Read(),),
    "fetch_password_settings": (Read(FakeServiceEditITM),),
    "fetch_point_registrations": (
        Read(FakeRegistryITM, (SERVICE,), {"details": True}, ends_session=True),
    ),
    "fetch_analog_values": (Read(client={"zone_path": ("Building", "Analog")}),),
    "fetch_points": (
        Read(),
        Read(
            kwargs={"details": True},
            client={"zone_path": ("Building", "Analog")},
        ),
        Read(client={"zone_path": ("Building", "Ventilation")}),
        Read(kwargs={"thermostat": True}),
        Read(kwargs={"thermostat": ["1:1-04"]}),
        Read(
            kwargs={"details": True, "thermostat": ["Alarm 1"]},
            client={"zone_path": ("Building", "Analog")},
        ),
    ),
    "fetch_ppd": (
        Read(FakeExportsITM),
        Read(
            FakeExportsITM,
            kwargs={"start": date(2024, 11, 20), "end": date(2024, 12, 19)},
            ends_session=True,
        ),
        Read(FakeExportsITM, kwargs={"calculation_day": 15}, ends_session=True),
    ),
    "fetch_ppd_advanced": (Read(FakeExportsITM),),
    "fetch_r32_output_ports": (
        Read(FakeServiceEditITM, (SERVICE,), ends_session=True),
    ),
    "fetch_schedule": (
        Read(args=("Always",)),
        Read(args=("Always",), kwargs={"details": True}),
    ),
    "fetch_schedule_day": (
        Read(args=("Always",)),
        Read(args=("Always",), kwargs={"days_ahead": 2}),
    ),
    "fetch_schedules": (Read(),),
    "fetch_service_settings": (Read(args=(SERVICE,), ends_session=True),),
    "fetch_setback": (Read(),),
    "fetch_sliding_temperature_inputs": (Read(_with_dealer_functions, ("Hall",)),),
    "fetch_sliding_temperatures": (
        Read(_with_dealer_functions, kwargs={"details": True}),
    ),
    "fetch_system": (Read(), Read(args=(["hardware", "network"],))),
    "fetch_temperature_limits": (
        Read(_with_dealer_functions, kwargs={"details": True}),
    ),
    "fetch_thermostat_status": (Read(), Read(args=(["1:1-04"],))),
    "fetch_unit": (Read(args=("1:1-04",)),),
    "fetch_unit_settings": (Read(args=("1:1-09",)),),
    "fetch_units": (Read(), Read(kwargs={"view": "list"}, ends_session=True)),
    "fetch_web_user_screens": (Read(FakeUsersITM, ("viewer",)),),
    "fetch_zone_attributes": (Read(args=("Building",)),),
    "fetch_zone_members": (Read(FakeZonesITM, ("Building",)),),
    "fetch_zone_units": (
        Read(args=("Indoor units",), client={"zone_path": ("Building",)}),
    ),
    "list_zones": (Read(), Read(args=(("Building",),))),
}

# Reads that log in by themselves: refused inside a block.
OWN_LOGIN: dict[str, Callable[[], FakeITM]] = {"fetch_own_settings": FakeUsersITM}

# The reads run before the one under test: through the zones to each unit's
# Details screen and settings dialog, and through the menu's screens.
BEFORE = ("fetch_capabilities", "fetch_config")


def _reads() -> list[str]:
    """Return the client's public reads, by introspection."""
    return sorted(
        name
        for name, _ in inspect.getmembers(ItmClient, inspect.iscoroutinefunction)
        if name.startswith(("fetch_", "export_")) or name == "list_zones"
    )


def _client(read: Read, itm: FakeITM | None = None) -> FakeClient:
    """Return a client for a read's simulated iTM, fetching its exported files."""
    fake = itm or read.make()
    c = FakeClient(fake, **read.client)
    c.progress_interval = 0

    async def get(path: str) -> bytes:
        files: dict[str, bytes] = fake.files  # type: ignore[attr-defined]
        return files[path]

    c._get_bytes = get  # type: ignore[method-assign]
    return c


def _plain(value: Any) -> Any:
    """Return a result without the moment it was read, to compare two reads."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            f.name: _plain(getattr(value, f.name))
            for f in dataclasses.fields(value)
            if f.name != "read_at"
        }
    if isinstance(value, Points):
        return {
            "points": _plain(dict(value)),
            "thermostat": value.thermostat,
            "indicators": _plain(value.indicators),
        }
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(v) for v in value]
    return value


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM."""
    return FakeITM()


def test_every_read_is_listed() -> None:
    """A new public read needs its arguments here, or its place in OWN_LOGIN."""
    assert set(READS) | set(OWN_LOGIN) == set(_reads())
    assert not set(READS) & set(OWN_LOGIN)


@pytest.mark.parametrize(
    ("name", "read"),
    [
        pytest.param(name, read, id=f"{name}-{i}" if len(reads) > 1 else name)
        for name, reads in READS.items()
        for i, read in enumerate(reads)
    ],
)
async def test_read_is_the_same_inside_a_block(name: str, read: Read) -> None:
    """A read gives the same in a block, after other reads, as in its own session.

    Twice, as a second read of the same screens must be complete as well. The
    results are compared with two reads that each log in.
    """
    apart = _client(read)
    alone = [
        await getattr(apart, name)(*read.args, **read.kwargs),
        await getattr(apart, name)(*read.args, **read.kwargs),
    ]
    top = await apart.list_zones()
    fake = read.make()
    c = _client(read, fake)
    async with c.shared_session():
        for other in BEFORE:
            if other != name:
                await getattr(c, other)()
        assert fake.logins == 1
        shared = [
            await getattr(c, name)(*read.args, **read.kwargs),
            await getattr(c, name)(*read.args, **read.kwargs),
        ]
        assert await c.list_zones() == top
    assert _plain(shared) == _plain(alone)
    assert fake.logins == (3 if read.ends_session else 1)
    assert fake.session is None


@pytest.mark.parametrize("name", OWN_LOGIN)
async def test_read_with_its_own_login_is_refused(name: str) -> None:
    """A read that logs in by itself can't run in a block; nothing is sent."""
    fake = OWN_LOGIN[name]()
    c = FakeClient(fake)
    async with c.shared_session():
        await c.fetch_indicators()
        sent = len(fake.requests)
        with pytest.raises(SharedSessionError, match="login of their own"):
            await getattr(c, name)()
        assert len(fake.requests) == sent
        await c.fetch_indicators()
    assert fake.logins == 1
    assert await getattr(c, name)() is not None


async def test_block_is_one_login(itm: FakeITM) -> None:
    """Several reads in a block share one login, which ends with the block."""
    store = MemorySessionStore()
    c = FakeClient(itm, store=store)
    async with c.shared_session():
        assert itm.requests == []
        points = await c.fetch_points()
        assert store.token == itm.session
        running = [
            name
            for name, point in points.items()
            if isinstance(point, Unit)
            and point.kind is PointKind.INDOOR
            and point.running
        ]
        thermostat = await c.fetch_thermostat_status(running)
        info = await c.fetch_info()
        assert itm.session is not None
    assert thermostat == {"1:1-00": False, "1:1-04": False}
    assert info.version == "1.00.00"
    assert itm.logins == 1
    assert itm.session is None
    assert store.token is None
    assert sum(r["event"] == 10 for r in itm.requests) == 1


async def test_outside_a_block_every_call_logs_in(itm: FakeITM) -> None:
    """Without a block nothing changes: a login per call."""
    c = FakeClient(itm)
    async with c.shared_session():
        await c.fetch_units()
    await c.fetch_units()
    await c.fetch_info()
    assert itm.logins == 3


async def test_block_without_calls_sends_nothing(itm: FakeITM) -> None:
    """A block logs in with its first read only."""
    async with FakeClient(itm).shared_session():
        pass
    assert itm.requests == []


async def test_error_in_a_call_ends_the_session(itm: FakeITM) -> None:
    """After an error the session's state is unknown: the next read logs in."""
    c = FakeClient(itm)
    async with c.shared_session():
        await c.fetch_units()
        with pytest.raises(ItmCommandError, match="not found"):
            await c.fetch_unit("no such unit")
        assert itm.session is None
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 2
    assert itm.session is None


async def test_login_error_in_a_block(itm: FakeITM) -> None:
    """A refused login fails that read; the next one tries again."""
    c = FakeClient(itm)
    itm.session = "f" * 32
    async with c.shared_session():
        with pytest.raises(LoginRejected):
            await c.fetch_units()
        itm.session = None
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 1
    assert itm.session is None


async def test_write_is_refused_inside_a_block(itm: FakeITM) -> None:
    """A write raises before anything is sent, also as a dry run."""
    c = FakeClient(itm)
    async with c.shared_session():
        await c.fetch_units()
        sent = len(itm.requests)
        with pytest.raises(SharedSessionError, match="only reads"):
            await c.set_setpoint("1:1-04", 21)
        with pytest.raises(SharedSessionError):
            await c.apply("1:1-04", power=False)
        with pytest.raises(SharedSessionError):
            await c.set_setback(high_up=2)
        assert len(itm.requests) == sent
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 1
    assert itm.units[1].setpoint == 23
    dry = FakeClient(itm, dry_run=True)
    async with dry.shared_session():
        with pytest.raises(SharedSessionError):
            await dry.set_setpoint("1:1-04", 21)
    assert not dry.planned
    assert itm.logins == 1
    await c.set_setpoint("1:1-04", 21)
    assert itm.units[1].setpoint == 21


# Writes that look at their arguments before they refuse: what to call them with.
# The others refuse whatever they are given.
WRITE_ARGUMENTS: dict[str, tuple[tuple[Any, ...], dict[str, Any]]] = {
    "apply": (("1:1-04",), {"power": False}),
    "apply_zone": (("Indoor units",), {"power": False}),
    "create_web_user": (("demouser", "s3cret!"), {"zones": ("Building",)}),
    "edit_schedule_event": (
        ("Always", 1, "06:00", "Indoor units", EventSpec("06:30", "Indoor units")),
        {"keep_unset": True},
    ),
    "set_energy_rule_day": (("Rule 1", "Mon", [RuleRange("08:00", "09:00")]), {}),
    "set_hardware": ((), {"backlight": 3}),
    "set_language": (("nl",), {}),
    "set_point_attributes": (("1:1-04",), {"info": "Hall"}),
    "set_region": ((), {"time_24h": False}),
    "set_screensaver": ((), {"delay_minutes": 5}),
    "set_unit_settings": (("1:1-04",), {"overtime": True}),
    "set_web_user": (("viewer",), {"admin": True}),
    "set_zone_attributes": (("Building",), {"info": "Hall"}),
}


def _made_up(parameter: inspect.Parameter) -> Any:
    """Return an argument for a parameter without a default."""
    kind = str(parameter.annotation)
    for marker, value in (
        ("bool", True),
        ("int", 1),
        ("float", 1.0),
        ("date", date(2025, 1, 1)),
        ("Iterable", ["x"]),
        ("list", ["x"]),
        ("tuple", ("x",)),
        ("dict", {}),
    ):
        if marker in kind:
            return value
    return "x"


def _writes() -> list[str]:
    """Return the client's public coroutines that are no reads."""
    skip = {*_reads(), "close", "release_saved_session"}
    return sorted(
        name
        for name, _ in inspect.getmembers(ItmClient, inspect.iscoroutinefunction)
        if not name.startswith("_") and name not in skip
    )


@pytest.mark.parametrize("name", _writes())
async def test_every_write_is_refused_inside_a_block(itm: FakeITM, name: str) -> None:
    """Whatever isn't a read is refused in a block, before anything is sent."""
    c = FakeClient(itm)
    if name in WRITE_ARGUMENTS:
        args, kwargs = WRITE_ARGUMENTS[name]
    else:
        required = [
            p
            for p in inspect.signature(getattr(c, name)).parameters.values()
            if p.default is p.empty
        ]
        args = tuple(_made_up(p) for p in required if p.kind is not p.KEYWORD_ONLY)
        kwargs = {p.name: _made_up(p) for p in required if p.kind is p.KEYWORD_ONLY}
    async with c.shared_session():
        await c.fetch_indicators()
        sent = len(itm.requests)
        with pytest.raises(SharedSessionError):
            await getattr(c, name)(*args, **kwargs)
        assert len(itm.requests) == sent
    assert itm.logins == 1


async def test_read_only_client_in_a_block(itm: FakeITM) -> None:
    """A read-only client reads in a block; its writes stay ReadOnlyError."""
    c = FakeClient(itm, read_only=True)
    async with c.shared_session():
        assert len(await c.fetch_units()) == 3
        with pytest.raises(ReadOnlyError):
            await c.set_setpoint("1:1-04", 21)
        assert (await c.fetch_setback()).high_up == 3
    assert itm.logins == 1


async def test_session_of_a_block_refuses_a_write(itm: FakeITM) -> None:
    """The session itself refuses what would change the iTM, as a last line."""
    c = FakeClient(itm)

    async def unguarded_write() -> None:
        async with c._session() as s:
            await s.open_zone(c.zone_path)
            await s.select_by_name("1:1-04")
            await s.choose(11, 0)

    async with c.shared_session():
        with pytest.raises(SharedSessionError, match="only reads"):
            await unguarded_write()
        assert itm.session is None
    assert not any(r["screen"] == 4 and r["part"] == 11 for r in itm.requests)
    await unguarded_write()
    assert any(r["screen"] == 4 and r["part"] == 11 for r in itm.requests)


async def test_cancelled_call_logs_out(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cancelling inside a block still completes the logout."""
    c = FakeClient(itm)
    started = asyncio.Event()
    orig = c._post

    async def slow_post(body: str) -> str:
        if itm.session and len(itm.requests) > 12 and not started.is_set():
            started.set()
            await asyncio.sleep(0.2)
        return await orig(body)

    monkeypatch.setattr(c, "_post", slow_post)

    async def poll() -> None:
        async with c.shared_session():
            await c.fetch_units()
            await c.fetch_units()

    task = asyncio.create_task(poll())
    await started.wait()
    assert itm.session is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert itm.session is None
    assert itm.logins == 1
    assert len(await c.fetch_units()) == 3


async def test_cancelled_between_calls_logs_out(itm: FakeITM) -> None:
    """A block cancelled while it waits between two reads logs out too."""
    c = FakeClient(itm)
    reading = asyncio.Event()

    async def poll() -> None:
        async with c.shared_session():
            await c.fetch_units()
            reading.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(poll())
    await reading.wait()
    assert itm.session is not None
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert itm.session is None


async def test_time_limit(itm: FakeITM, caplog: pytest.LogCaptureFixture) -> None:
    """A login used longer than max_seconds is replaced, with a warning."""
    c = FakeClient(itm)
    with caplog.at_level(logging.WARNING, logger="daikin_itm.client"):
        async with c.shared_session(max_seconds=0.05):
            await c.fetch_units()
            await c.fetch_units()
            assert itm.logins == 1
            assert not caplog.records
            await asyncio.sleep(0.06)
            assert len(await c.fetch_units()) == 3
            await c.fetch_units()
    assert itm.logins == 2
    assert itm.session is None
    assert [r.getMessage() for r in caplog.records] == [
        "shared session open for more than 0.05 s; logging in again"
    ]


async def test_max_seconds_must_be_positive(itm: FakeITM) -> None:
    """A block without time is refused."""
    c = FakeClient(itm)
    with pytest.raises(ValueError, match="max_seconds"):
        async with c.shared_session(max_seconds=0):
            pass
    assert not c._lock.locked()


async def test_other_task_waits_for_the_block(itm: FakeITM) -> None:
    """A call from another task waits until the block ends, then logs in itself."""
    c = FakeClient(itm)
    inside = asyncio.Event()
    leave = asyncio.Event()

    async def poll() -> None:
        async with c.shared_session():
            await c.fetch_units()
            inside.set()
            await leave.wait()
            await c.fetch_units()

    block = asyncio.create_task(poll())
    await inside.wait()
    other = asyncio.create_task(c.fetch_unit("1:1-04"))
    write = asyncio.create_task(c.set_setpoint("1:1-04", 21))
    await asyncio.sleep(0.05)
    assert not other.done()
    assert not write.done()
    assert itm.logins == 1
    leave.set()
    await block
    assert (await other).setpoint == 23
    assert (await write) is not None
    assert itm.units[1].setpoint == 21
    assert itm.session is None


async def test_tasks_started_inside_the_block_share_it(itm: FakeITM) -> None:
    """Reads gathered inside a block take turns on its session."""
    c = FakeClient(itm)
    async with c.shared_session():
        units, info, zones = await asyncio.gather(
            c.fetch_units(), c.fetch_info(), c.list_zones()
        )
        assert await asyncio.wait_for(c.fetch_units(), 5) == units
        with pytest.raises(SharedSessionError):
            await asyncio.gather(c.set_setpoint("1:1-04", 21))
    assert len(units) == 3
    assert info.version == "1.00.00"
    assert zones == ["Building"]
    assert itm.logins == 1
    assert itm.session is None


async def test_task_outliving_the_block_logs_in_itself(itm: FakeITM) -> None:
    """A task started in a block that reads after it has ended: its own login."""
    c = FakeClient(itm)
    go = asyncio.Event()

    async def later() -> int:
        await go.wait()
        return len(await c.fetch_units())

    async with c.shared_session():
        await c.fetch_units()
        task = asyncio.create_task(later())
    assert itm.session is None
    go.set()
    assert await task == 3
    assert itm.logins == 2
    assert itm.session is None


async def test_block_ends_after_a_call_still_running(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Leaving the block waits for a read a task started inside it.

    A read that was still waiting for its turn then logs in by itself.
    """
    c = FakeClient(itm)
    logged_in = asyncio.Event()
    orig = c._post

    async def post(body: str) -> str:
        if itm.session:
            logged_in.set()
        return await orig(body)

    monkeypatch.setattr(c, "_post", post)
    async with c.shared_session():
        running = asyncio.create_task(c.fetch_units())
        waiting = asyncio.create_task(c.fetch_info())
        await logged_in.wait()
    assert running.done()
    assert itm.logins == 1
    assert len(await running) == 3
    assert (await waiting).version == "1.00.00"
    assert itm.logins == 2
    assert itm.session is None


async def test_nested_block_joins_the_outer_one(itm: FakeITM) -> None:
    """A block inside a block is the same block; the outer one logs out."""
    c = FakeClient(itm)
    async with c.shared_session():
        await c.fetch_units()
        async with c.shared_session(max_seconds=0.001):
            await asyncio.sleep(0.01)
            await c.fetch_info()
        assert itm.session is not None
        await c.fetch_units()
    assert itm.logins == 1
    assert itm.session is None


async def test_blocks_of_two_clients(itm: FakeITM) -> None:
    """Each client has its own block; one inside the other doesn't mix them."""
    other = FakeITM()
    a, b = FakeClient(itm), FakeClient(other)
    async with a.shared_session(), b.shared_session():
        await a.fetch_units()
        await b.fetch_units()
        await a.fetch_info()
        await b.fetch_info()
    assert (itm.logins, other.logins) == (1, 1)
    assert itm.session is None
    assert other.session is None


async def test_session_left_by_a_crash_is_released(itm: FakeITM) -> None:
    """The block's token is in the store: another run's login releases its session.

    The block then finds its session gone and logs in again.
    """
    store = MemorySessionStore()
    c = FakeClient(itm, store=store)
    async with c.shared_session():
        await c.fetch_units()
        assert store.token == itm.session
        other_run = FakeClient(itm, store=store).fetch_units()
        assert len(await asyncio.to_thread(asyncio.run, other_run)) == 3
        assert itm.session is None
        assert len(await c.fetch_units()) == 3
        assert store.token == itm.session
    assert itm.logins == 3
    assert store.token is None


async def test_session_ended_by_the_itm_between_calls(itm: FakeITM) -> None:
    """A session the iTM timed out is replaced before the next read."""
    c = FakeClient(itm)
    async with c.shared_session():
        await c.fetch_units()
        itm.session = None
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 2
    assert itm.session is None


async def test_release_saved_session_inside_a_block(itm: FakeITM) -> None:
    """The saved token is the block's own session: there is nothing to release."""
    c = FakeClient(itm)
    async with c.shared_session():
        await c.fetch_units()
        sent = len(itm.requests)
        assert await c.release_saved_session() is False
        assert len(itm.requests) == sent
        await c.fetch_units()
    assert itm.logins == 1


async def test_unconfirmed_logout_of_a_block_keeps_the_token(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A logout that fails at the end of a block leaves the token to release."""
    store = MemorySessionStore()
    c = FakeClient(itm, store=store)
    orig = c._post
    fail = True

    async def post(body: str) -> str:
        if is_logout(body) and fail:
            msg = "gone"
            raise aiohttp.ClientConnectionError(msg)
        return await orig(body)

    monkeypatch.setattr(c, "_post", post)
    with pytest.raises(aiohttp.ClientConnectionError):
        async with c.shared_session():
            await c.fetch_units()
    assert itm.session is not None
    assert store.token == itm.session
    assert not c._lock.locked()
    fail = False
    async with c.shared_session():
        assert len(await c.fetch_units()) == 3
    assert itm.session is None
    assert store.token is None


async def test_unit_changes_show_in_the_next_read(itm: FakeITM) -> None:
    """A second read of the same units in a block shows what changed since."""
    c = FakeClient(itm)
    async with c.shared_session():
        before = await c.fetch_units()
        itm.units[1].setpoint = 25
        itm.units[1].temp = 26.5
        itm.units[2].running = True
        itm.units[0].thermo_on = True
        after = await c.fetch_units()
        status = await c.fetch_thermostat_status()
        listed = await c.fetch_units(view="list")
    assert before["1:1-04"].setpoint == 23
    assert after["1:1-04"].setpoint == 25
    assert after["1:1-04"].room_temperature == 26.5
    assert after["1:1-09"].running is True
    assert status == {"1:1-00": True, "1:1-04": False, "1:1-09": False}
    assert listed["1:1-04"].setpoint == 25
    assert after == await FakeClient(itm).fetch_units()
    assert itm.logins == 2


async def test_menu_changes_show_in_the_next_read(itm: FakeITM) -> None:
    """A screen opened again in a block shows the iTM's current settings."""
    c = FakeClient(itm)
    async with c.shared_session():
        assert (await c.fetch_setback()).high_up == 3
        assert (await c.fetch_overtime_duration()) == 60
        assert [p.name for p in await c.fetch_schedules()] == [
            "Always",
            "Program G",
            "Program E",
        ]
        itm.setback["high_up"] = 5
        itm.overtime_idx = 3
        itm.schedules[0][1] = "Weekdays"
        assert (await c.fetch_setback()).high_up == 5
        assert (await c.fetch_overtime_duration()) == 120
        assert [p.name for p in await c.fetch_schedules()] == [
            "Weekdays",
            "Program G",
            "Program E",
        ]
    assert itm.logins == 1


async def test_indicators_are_current_in_a_block(itm: FakeITM) -> None:
    """The main menu bar is brought up to date before each read."""
    c = FakeClient(itm)
    async with c.shared_session():
        assert (await c.fetch_indicators()).error is False
        assert await c.fetch_error_information() is None
        itm.errors = [["1:1-04", "A3"]]
        itm.energy_saving = True
        indicators = await c.fetch_indicators()
        errors = await c.fetch_error_information()
        itm.errors = []
        assert await c.fetch_error_information() is None
    assert indicators.error is True
    assert indicators.energy_saving is True
    assert errors is not None
    assert errors.errors == (("1:1-04", "A3"),)
    assert itm.logins == 1


async def test_list_view_is_read_in_a_new_session(itm: FakeITM) -> None:
    """The list view shown twice in one session wouldn't send its list again."""
    c = FakeClient(itm)
    alone = await c.fetch_units(view="list")
    async with c._session() as s:
        await s.open_zone(c.zone_path)
        assert await s.read_units_from_list() == alone
        with pytest.raises(ProtocolError, match="not shown"):
            await s.read_units_from_list()
    logins = itm.logins
    async with c.shared_session():
        assert await c.fetch_units(view="list") == alone
        assert await c.fetch_units(view="list") == alone
    assert itm.logins - logins == 2


async def test_screen_left_open_is_closed(itm: FakeITM) -> None:
    """What a call leaves open is closed before the next one, in the same login."""
    c = FakeClient(itm)
    async with c.shared_session():
        async with c._session() as s:
            await s.open_zone(c.zone_path)
            await s.select_by_name("1:1-04")
            await s.send(ScreenID.OPERATION, 7)
            await s.open_menu_screen(3, 100)  # type: ignore[arg-type]
            assert not s.at_start()
        assert s.at_start()
        assert itm.open_screens == set()
        assert itm.menu_open is False
        assert len(itm.path) == 1
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 1


async def test_what_cannot_be_closed_ends_the_session(itm: FakeITM) -> None:
    """A dialog left open, a screen without a known Close or one that stays."""
    c = FakeClient(itm)
    async with c.shared_session():
        async with c._session() as s:
            await s.open_menu_screen(MB_HISTORY, MenuScreen.HISTORY)
            assert not s.at_start()
        assert itm.session is None
        async with c._session() as s:
            s.state.screens[4242] = {}
        assert itm.session is None
        async with c._session() as s:
            s.state.screens[MenuScreen.SETBACK] = {}
        assert itm.session is None
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 4


async def test_session_is_checked_before_each_call(itm: FakeITM) -> None:
    """A session that isn't at its start position when a call starts is replaced."""
    c = FakeClient(itm)
    async with c.shared_session():
        async with c._session() as s:
            pass
        assert s.at_start()
        s.state.screens[4242] = {}
        assert len(await c.fetch_units()) == 3
        assert itm.logins == 2
        # The iTM is somewhere else than the client believes: its answer to the
        # POLLING event names an icon that isn't at the top level.
        itm.path.append(itm.root.children[0])
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 3
    assert itm.session is None


async def test_failing_way_back_ends_the_session(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When going back to the start fails, the read still counts; a new login."""
    c = FakeClient(itm)
    orig = c._post
    failures = [aiohttp.ClientConnectionError("gone")]

    async def post(body: str) -> str:
        if body[36:48] == "000100010001" and failures:  # the icon view's Top
            raise failures.pop()
        return await orig(body)

    def no_top(_ev: dict[str, Any]) -> list[Any]:
        return []

    monkeypatch.setattr(c, "_post", post)
    async with c.shared_session():
        assert len(await c.fetch_units()) == 3
        assert itm.session is None
        monkeypatch.setattr(itm, "_zone_top", no_top)
        assert len(await c.fetch_units()) == 3
        assert itm.session is None
        monkeypatch.undo()
        assert len(await c.fetch_units()) == 3
        assert itm.session is not None
    assert itm.logins == 3


async def test_error_and_failing_logout_in_a_block(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A logout that fails after an error keeps the token and the error.

    The next read frees the session.
    """
    store = MemorySessionStore()
    c = FakeClient(itm, store=store)
    orig = c._post
    failures = [aiohttp.ClientConnectionError("gone")]

    async def post(body: str) -> str:
        if is_logout(body) and failures:
            raise failures.pop()
        return await orig(body)

    monkeypatch.setattr(c, "_post", post)
    async with c.shared_session():
        with pytest.raises(ItmCommandError, match="no such unit") as caught:
            await c.fetch_unit("no such unit")
        assert "gone" in " ".join(caught.value.__notes__)
        assert store.token == itm.session
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 2
    assert itm.session is None
    assert store.token is None


async def test_top_button_is_never_clicked_at_the_top(itm: FakeITM) -> None:
    """At the top level the Top button is disabled: clicking it hangs the iTM."""
    c = FakeClient(itm)
    async with c.shared_session():
        await c.list_zones()
        await c.fetch_indicators()
        await c.fetch_setback()
        async with c._session() as s:
            with pytest.raises(ItmCommandError, match="disabled"):
                await s.click(ScreenID.STDICON, 1, "top")
    assert not any(r["screen"] == 1 and r["part"] == 1 for r in itm.requests)


def _warmer(itm: FakeITM) -> None:
    itm.units[1].temp = 26.5
    itm.units[1].setpoint = 25
    itm.units[2].running = True


def _points_changed(itm: FakeITM) -> None:
    _warmer(itm)
    itm.ventilation[0].running = False
    itm.analog[0].alarm = True


def _thermostats(itm: FakeITM) -> None:
    itm.units[0].thermo_on = True
    itm.units[2].thermo_on = True


def _unit_settings(itm: FakeITM) -> None:
    itm.units[2].lock_mode = 2
    itm.units[2].thermo_on = True


def _bar(itm: FakeITM) -> None:
    itm.errors = [["1:1-04", "A3"]]
    itm.energy_saving = True
    itm.bar_clock = "Tue, 31/12\n09:05"


def _errors(itm: FakeITM) -> None:
    itm.errors = [["1:1-04", "A3"]]


def _setback(itm: FakeITM) -> None:
    itm.setback["high_up"] = 5


def _overtime(itm: FakeITM) -> None:
    itm.overtime_idx = 3


def _renamed(itm: FakeITM) -> None:
    itm.schedules[0][1] = "Weekdays"


def _new_event(itm: FakeITM) -> None:
    event = {"time": "12:00", "target": "1:1-04", "kind": "unit", "power": 1}
    itm.schedule_events["Always"][0] = [event]
    itm.special_events["Always"]["Ex1"] = []


# Reads with a change on the simulated iTM that their result shows.
CHANGES: dict[str, tuple[Read, Callable[[FakeITM], None]]] = {
    "fetch_units": (Read(), _warmer),
    "fetch_unit": (Read(args=("1:1-04",)), _warmer),
    "fetch_points": (Read(), _points_changed),
    "fetch_points[thermostat]": (Read(kwargs={"thermostat": True}), _thermostats),
    "fetch_thermostat_status": (Read(), _thermostats),
    "fetch_unit_settings": (Read(args=("1:1-09",)), _unit_settings),
    "fetch_indicators": (Read(), _bar),
    "fetch_error_information": (Read(), _errors),
    "fetch_setback": (Read(), _setback),
    "fetch_overtime_duration": (Read(), _overtime),
    "fetch_schedules": (Read(), _renamed),
    "fetch_schedule": (Read(args=("Always",), kwargs={"details": True}), _new_event),
}


@pytest.mark.parametrize("name", CHANGES)
async def test_read_after_a_change_is_current(name: str) -> None:
    """A read in a block shows what changed on the iTM since its last read.

    The second read is compared with a read in a session of its own, after the
    change: not with the reads before it, which a block returning what it saw
    earlier would match as well.
    """
    read, change = CHANGES[name]
    method = name.partition("[")[0]
    fake = read.make()
    c = _client(read, fake)
    async with c.shared_session():
        for other in BEFORE:
            await getattr(c, other)()
        before = await getattr(c, method)(*read.args, **read.kwargs)
        change(fake)
        shared = await getattr(c, method)(*read.args, **read.kwargs)
    fresh = await getattr(_client(read, fake), method)(*read.args, **read.kwargs)
    assert _plain(shared) != _plain(before)
    assert _plain(shared) == _plain(fresh)
    assert fake.logins == 2


async def test_failing_check_and_logout_in_a_block(
    itm: FakeITM, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A session that can't be checked nor logged out is given up, not the read."""
    store = MemorySessionStore()
    c = FakeClient(itm, store=store)
    orig = c._post
    polls: list[aiohttp.ClientResponseError] = []
    logouts: list[aiohttp.ClientResponseError] = []

    async def post(body: str) -> str:
        if body[44:48] == "0004" and polls:  # POLLING
            raise polls.pop()
        reply = await orig(body)
        if is_logout(body) and logouts:
            raise logouts.pop()
        return reply

    monkeypatch.setattr(c, "_post", post)
    async with c.shared_session():
        await c.fetch_units()
        polls.append(http_error())
        logouts.append(http_error())
        with caplog.at_level(logging.WARNING):
            assert len(await c.fetch_units()) == 3
        assert store.token == itm.session
    assert "could not log out the shared session" in caplog.text
    assert not polls
    assert not logouts
    assert itm.logins == 2
    assert itm.session is None
    assert store.token is None


async def test_release_from_another_task_keeps_the_block(itm: FakeITM) -> None:
    """A task outside the block can't release the block's session as left behind."""
    store = MemorySessionStore()
    c = FakeClient(itm, store=store)
    async with c.shared_session():
        await c.fetch_units()
        sent = len(itm.requests)
        task = asyncio.get_running_loop().create_task(
            c.release_saved_session(), context=contextvars.Context()
        )
        assert await task is False
        assert len(itm.requests) == sent
        await c.fetch_units()
    assert itm.logins == 1
    store.token = "f" * 32
    assert await c.release_saved_session() is True
