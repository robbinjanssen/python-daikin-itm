# pylint: disable=protected-access,redefined-outer-name
"""confirm_in_session: a write and the reads that check it in one login."""

from __future__ import annotations

import asyncio
import contextvars
import inspect
from datetime import datetime
from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import (
    ExperimentalError,
    ItmClient,
    ItmCommandError,
    PowerState,
    ReadOnlyError,
    RemoteLock,
    SessionInUse,
    SetpointOutOfRange,
    SharedSessionError,
    TempLimit,
)

from .fake_itm import FakeITM, FakeZone
from .fake_users import FakeUsersITM
from .test_client import FakeClient, confirmed
from .test_experimental import GUARDED
from .test_kept_session import _wait
from .test_shared_session import WRITE_ARGUMENTS, _made_up, _writes

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from daikin_itm.models import Unit

POLLING = 4
SETPOINT_PICK = (4, 11)

UNIT_WRITES: dict[str, Callable[[ItmClient], Awaitable[object]]] = {
    "set_setpoint": lambda c: c.set_setpoint("1:1-04", 22),
    "set_power": lambda c: c.set_power("1:1-04", on=False),
    "set_mode": lambda c: c.set_mode("1:1-04", "heat"),
    "set_fan_speed": lambda c: c.set_fan_speed("1:1-04", "high"),
    "apply": lambda c: c.apply("1:1-04", mode="heat", fan_speed="low"),
    "set_power_state": lambda c: c.set_power_state("1:1-04", PowerState.OFF),
    "set_unit_settings": lambda c: c.set_unit_settings(
        "1:1-04", setpoint_lock=RemoteLock.FORBIDDEN
    ),
    "apply_zone": lambda c: c.apply_zone("Indoor units", fan_speed="high"),
}


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM whose changes reach a unit after three steps."""
    fake = FakeITM()
    fake.latency_logins = 3
    return fake


def _zone_client(itm: FakeITM, **kw: Any) -> FakeClient:
    return FakeClient(itm, zone_path=("Building",), **kw)


def _client(itm: FakeITM, name: str, **kw: Any) -> FakeClient:
    return _zone_client(itm, **kw) if "zone" in name else FakeClient(itm, **kw)


def test_on_by_default(itm: FakeITM) -> None:
    """The write's own login checks it unless the client is told otherwise."""
    assert FakeClient(itm).confirm_in_session
    assert not FakeClient(itm, confirm_in_session=False).confirm_in_session


@pytest.mark.parametrize("name", sorted(UNIT_WRITES))
async def test_one_login_per_write(itm: FakeITM, name: str) -> None:
    """A write and every check of it take one login, then it logs out."""
    await UNIT_WRITES[name](_client(itm, name))
    assert itm.logins == 1
    assert itm.session is None
    assert sum(r["event"] == POLLING for r in itm.requests) >= 2


@pytest.mark.parametrize("name", sorted(UNIT_WRITES))
async def test_a_login_per_check_without(itm: FakeITM, name: str) -> None:
    """With the option off every check logs in by itself."""
    await UNIT_WRITES[name](_client(itm, name, confirm_in_session=False))
    assert itm.logins >= 3
    assert itm.session is None


async def test_zone_writes_confirm_on_every_member(itm: FakeITM) -> None:
    """A zone's panel pick no point sends over ends the login once, not per check."""
    c = _zone_client(itm)
    units = await c.set_zone_power_state("Indoor units", PowerState.ON)
    assert units is not None
    assert all(u.running for u in units.values())
    assert itm.logins == 2
    assert await c.set_zone_setpoint("Indoor units", 22) is not None
    assert itm.logins == 4
    assert all(u.setpoint == 22 for u in itm.units)


async def test_menu_write_reads_back_in_its_login(itm: FakeITM) -> None:
    """A menu write whose screens all closed reads back in the same login."""
    c = FakeClient(itm)
    assert await c.set_setback(high_up=2) is not None
    assert itm.logins == 1
    assert itm.setback["high_up"] == 2
    off = FakeClient(itm, confirm_in_session=False)
    assert await off.set_setback(high_up=3) is not None
    assert itm.logins == 3


async def test_typed_number_reads_back_in_its_login(itm: FakeITM) -> None:
    """A number typed in a dialog is saved with OK: the check is in the same login."""
    c = FakeClient(itm)
    settings = await c.set_unit_settings(
        "1:1-04", cool_limit=TempLimit(enabled=True, minimum=20, maximum=30)
    )
    assert settings is not None
    assert settings.cool_limit.enabled
    assert itm.logins == 1
    assert itm.session is None


async def test_entered_clock_reads_back_in_a_new_login(itm: FakeITM) -> None:
    """A time entered in the clock's TIME dialog ends the write's login."""
    when = datetime(2025, 1, 2, 21, 5, 30)  # noqa: DTZ001 - the iTM's local time
    info = await FakeClient(itm).set_clock(when)
    assert info is not None
    assert info.clock == when
    assert itm.logins == 2
    assert itm.session is None


async def test_unit_not_taking_the_write_is_caught(itm: FakeITM) -> None:
    """The check reads what the iTM sends, not the pick on the client's panel."""
    itm.latency_logins = 0
    itm.sticky_picks = True
    itm.units[1].ignores_writes = True
    c = FakeClient(itm, confirm_timeout=0.05)
    with pytest.raises(ItmCommandError, match="setpoint=23"):
        await c.set_setpoint("1:1-04", 22)
    assert itm.units[1].setpoint == 23
    assert itm.session is None


async def test_forgotten_pick_is_caught(itm: FakeITM) -> None:
    """A pick the iTM doesn't send for a point with that value is no read-back."""
    itm.latency_logins = 0
    itm.root = FakeZone("top", itm.units[:3])
    itm.forgets_picks = True
    itm.units[0].ignores_writes = True
    for u in itm.units[1:3]:
        u.setpoint = itm.units[0].setpoint
    c = FakeClient(itm, zone_path=(), confirm_timeout=0.05)
    with pytest.raises(ItmCommandError, match="setpoint=21"):
        await c.set_setpoint("1:1-00", 22)
    assert itm.units[0].setpoint == 21
    assert itm.session is None


def _pick_mode(itm: FakeITM, mode: str) -> None:
    itm.latency_logins = 0
    itm.sticky_picks = mode == "sticky"
    itm.forgets_picks = mode == "forgets"


@pytest.mark.parametrize("mode", ["plain", "sticky", "forgets"])
async def test_unit_alone_at_its_level(itm: FakeITM, mode: str) -> None:
    """With no other point to send a pick over, the check logs in anew."""
    _pick_mode(itm, mode)
    itm.root = FakeZone("top", itm.units[:1])
    c = FakeClient(itm, zone_path=(), confirm_timeout=0.05)
    assert confirmed(await c.set_setpoint("1:1-00", 22)).setpoint == 22
    assert itm.logins == 2
    itm.units[0].ignores_writes = True
    with pytest.raises(ItmCommandError, match="setpoint=22"):
        await c.set_setpoint("1:1-00", 24)
    assert itm.units[0].setpoint == 22
    assert itm.session is None


@pytest.mark.parametrize("mode", ["plain", "sticky", "forgets"])
@pytest.mark.parametrize("write", ["setpoint", "power"])
async def test_zone_member_not_taking_the_write_is_caught(
    itm: FakeITM, mode: str, write: str
) -> None:
    """A zone write checks every member as the iTM sends it, not the zone's pick."""
    _pick_mode(itm, mode)
    for u in itm.units:
        u.running = True
    itm.units[2].ignores_writes = True
    c = _zone_client(itm, confirm_timeout=0.05)
    zone_write = (
        c.set_zone_setpoint("Indoor units", 22)
        if write == "setpoint"
        else c.set_zone_power_state("Indoor units", PowerState.OFF)
    )
    with pytest.raises(
        ItmCommandError, match=rf"not confirmed on \['{itm.units[2].name}'\]"
    ):
        await zone_write
    assert itm.units[2].running
    assert itm.session is None
    assert not c._lock.locked()


async def test_other_client_waits_for_the_whole_check(itm: FakeITM) -> None:
    """The session lock is held from the write until its last check has logged out."""
    c = FakeClient(itm, host="itm.test", confirm_interval=0.01)
    other = FakeClient(itm, host="itm.test", other_client_timeout=0.02)
    write = asyncio.create_task(c.set_setpoint("1:1-04", 22))
    await _wait(lambda: c._writing is not None and c._writing.session is not None)
    with pytest.raises(SessionInUse):
        await other.fetch_units()
    assert confirmed(await write).setpoint == 22
    assert itm.logins == 1
    assert len(await other.fetch_units()) == 3
    assert itm.logins == 2


async def test_cancelled_while_waiting_for_the_session(itm: FakeITM) -> None:
    """A write cancelled before it got the session leaves no block or lock behind."""
    holder = FakeClient(itm, host="itm.test")
    c = FakeClient(itm, host="itm.test", other_client_timeout=None)
    async with holder.shared_session():
        await holder.fetch_units()
        task = asyncio.create_task(c.set_setpoint("1:1-04", 22))
        await _wait(lambda: c._writing is not None and c._lock.locked())
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    assert c._writing is None
    assert not c._lock.locked()
    assert len(await c.fetch_units()) == 3
    assert itm.logins == 2


OPT_IN_WRITES: dict[str, Callable[[ItmClient], Awaitable[object]]] = {
    "start_leakage_check": lambda c: c.start_leakage_check("secret", "Outdoor 1"),
    "set_leakage_check_indoor_units": lambda c: c.set_leakage_check_indoor_units(
        "secret", "Outdoor 1", ("1:1-04",)
    ),
    "set_password_setting": lambda c: c.set_password_setting("screen", enabled=False),
}


@pytest.mark.parametrize("name", sorted(OPT_IN_WRITES))
async def test_opt_in_refuses_before_anything_is_sent(itm: FakeITM, name: str) -> None:
    """A write that needs its own opt-in refuses unsent, the write block too."""
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="pass allow_"):
        await OPT_IN_WRITES[name](c)
    assert not itm.requests
    assert c._writing is None
    assert not c._lock.locked()


async def test_refused_write_sends_no_pick(itm: FakeITM) -> None:
    """A setpoint out of the unit's range is refused before it is picked."""
    c = FakeClient(itm)
    with pytest.raises(SetpointOutOfRange):
        await c.set_setpoint("1:1-04", 99)
    assert not any((r["screen"], r["part"]) == SETPOINT_PICK for r in itm.requests)
    assert itm.logins == 1
    assert itm.session is None
    assert confirmed(await c.set_setpoint("1:1-04", 22)).setpoint == 22


async def test_change_by_another_client_shows(itm: FakeITM) -> None:
    """Another account overruling the write shows in the check, never the pick."""
    itm.latency_logins = 0

    class Overruled(FakeClient):
        """Another account sets the unit's setpoint before each check."""

        async def fetch_unit(self, name: str) -> Unit:
            itm.units[1].setpoint = 25
            return await super().fetch_unit(name)

    c = Overruled(itm, confirm_timeout=0.05)
    with pytest.raises(ItmCommandError, match="setpoint=25"):
        await c.set_setpoint("1:1-04", 22)
    assert itm.logins == 1


async def test_cancelled_while_checking_logs_out(itm: FakeITM) -> None:
    """A write cancelled during its check still logs out and frees the session."""
    itm.latency_logins = 10_000
    c = FakeClient(itm, confirm_timeout=10, confirm_interval=0.01)
    task = asyncio.create_task(c.set_setpoint("1:1-04", 22))
    await _wait(lambda: sum(r["event"] == POLLING for r in itm.requests) >= 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert itm.session is None
    assert not c._lock.locked()
    assert c._writing is None
    assert len(await c.fetch_units()) == 3
    assert itm.logins == 2


async def test_unconfirmed_write_settles_in_the_next_ones_login(itm: FakeITM) -> None:
    """confirm=False returns at once; the next write to the unit waits for it."""
    c = FakeClient(itm)
    assert await c.set_setpoint("1:1-04", 22, confirm=False) is None
    assert itm.logins == 1
    assert itm.session is None
    u = confirmed(await c.set_fan_speed("1:1-04", "high"))
    assert (u.setpoint, u.fan_speed) == (22, "high")
    assert itm.logins == 2


async def test_other_tasks_join_the_block(itm: FakeITM) -> None:
    """A read and a write from other tasks use the write's login meanwhile."""
    c = FakeClient(itm, confirm_interval=0.01)
    outside = contextvars.Context()
    write = asyncio.create_task(c.set_setpoint("1:1-04", 22))
    await _wait(lambda: c._writing is not None and c._writing.session is not None)
    read = asyncio.create_task(c.fetch_units(), context=outside)
    other = asyncio.create_task(c.set_setpoint("1:1-00", 22), context=outside)
    await asyncio.gather(write, read, other)
    assert len(read.result()) == 3
    assert itm.logins == 1
    assert itm.session is None
    assert c._writing is None


async def test_own_settings_wait_for_the_write() -> None:
    """The user's own settings log in by themselves: after the write, not refused."""
    itm = FakeUsersITM()
    itm.latency_logins = 3
    c = FakeClient(itm, confirm_interval=0.01)
    write = asyncio.create_task(c.set_setpoint("1:1-04", 22))
    await _wait(lambda: c._writing is not None and c._writing.session is not None)
    own = asyncio.create_task(c.fetch_own_settings(), context=contextvars.Context())
    await asyncio.gather(write, own)
    assert own.result() is not None
    assert itm.logins == 2


async def test_shared_session_waits_for_the_write(itm: FakeITM) -> None:
    """A shared_session() of another task is no write block: it waits, and refuses."""
    c = FakeClient(itm, confirm_interval=0.01)
    write = asyncio.create_task(c.set_setpoint("1:1-04", 22))
    await _wait(lambda: c._writing is not None and c._writing.session is not None)

    async def poll() -> None:
        async with c.shared_session():
            assert c._writing is None
            await c.fetch_units()
            with pytest.raises(SharedSessionError):
                await c.set_setpoint("1:1-04", 21)

    await asyncio.gather(
        write, asyncio.create_task(poll(), context=contextvars.Context())
    )
    assert itm.logins == 2


async def test_kept_session_of_another_task_waits(itm: FakeITM) -> None:
    """A kept_session() opened meanwhile doesn't join the write: it follows it."""
    c = FakeClient(itm, confirm_interval=0.01)
    write = asyncio.create_task(c.set_setpoint("1:1-04", 22))
    await _wait(lambda: c._writing is not None and c._writing.session is not None)

    async def poll() -> None:
        async with c.kept_session(keep_alive=None):
            assert c._writing is None
            assert c._kept is not None
            await c.fetch_units()

    await asyncio.gather(
        write, asyncio.create_task(poll(), context=contextvars.Context())
    )
    assert itm.logins == 2


async def test_refusal_doesnt_wait_for_another_client(itm: FakeITM) -> None:
    """Opening the write block takes no lock: a refusal comes at once."""
    holder = FakeClient(itm, host="itm.test")
    refused = FakeClient(
        itm, host="itm.test", read_only=True, other_client_timeout=0.05
    )
    async with holder.shared_session():
        await holder.fetch_units()
        with pytest.raises(ReadOnlyError):
            await refused.set_setpoint("1:1-04", 22)
    assert itm.logins == 1


async def test_kept_session_writes_unchanged(itm: FakeITM) -> None:
    """Inside kept_session() a write uses the kept login and its opt-in."""
    c = FakeClient(itm, allow_experimental=False)
    async with c.kept_session():
        with pytest.raises(ExperimentalError, match="kept session"):
            await c.set_setpoint("1:1-04", 22)
        assert c._writing is None
    allowed = FakeClient(itm)
    async with allowed.kept_session():
        assert confirmed(await allowed.set_setpoint("1:1-04", 22)).setpoint == 22
        assert allowed._writing is None
    assert itm.logins == 1


def _arguments(c: ItmClient, name: str) -> tuple[tuple[Any, ...], dict[str, Any]]:
    if name in WRITE_ARGUMENTS:
        return WRITE_ARGUMENTS[name]
    required = [
        p
        for p in inspect.signature(getattr(c, name)).parameters.values()
        if p.default is p.empty
    ]
    args = tuple(_made_up(p) for p in required if p.kind is not p.KEYWORD_ONLY)
    kwargs = {p.name: _made_up(p) for p in required if p.kind is p.KEYWORD_ONLY}
    return args, kwargs


@pytest.mark.parametrize("name", _writes())
async def test_read_only_refuses_before_anything_is_sent(
    itm: FakeITM, name: str
) -> None:
    """A read-only client refuses every write unsent, the write block too."""
    c = FakeClient(itm, read_only=True)
    args, kwargs = _arguments(c, name)
    with pytest.raises((ReadOnlyError, ItmCommandError)):
        await getattr(c, name)(*args, **kwargs)
    assert not itm.requests
    assert c._writing is None
    assert not c._lock.locked()


@pytest.mark.parametrize(("method", "args", "kwargs"), GUARDED)
async def test_experimental_refuses_before_anything_is_sent(
    itm: FakeITM, method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    """Without allow_experimental an experimental write is refused unsent."""
    c = FakeClient(itm, allow_experimental=False, confirm_in_session=True)
    with pytest.raises(ExperimentalError):
        await getattr(c, method)(*args, **kwargs)
    assert not itm.requests
    assert c._writing is None
    assert not c._lock.locked()


async def test_dry_run_takes_one_login(itm: FakeITM) -> None:
    """A dry run plans the write in one login and changes nothing."""
    c = FakeClient(itm, dry_run=True)
    assert await c.set_setpoint("1:1-04", 22) is None
    assert await c.set_setback(high_up=2) is None
    assert c.planned
    assert itm.logins == 2
    assert itm.units[1].setpoint == 23
