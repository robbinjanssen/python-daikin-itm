# pylint: disable=protected-access,redefined-outer-name
"""kept_session() against the simulated iTM: one login for a client's lifetime."""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from typing import Any

import aiohttp
import pytest

from daikin_itm import (
    ExperimentalError,
    ItmCommandError,
    PowerState,
    RemoteLock,
    SessionEnded,
    SessionInUse,
    SharedSessionError,
)
from daikin_itm.models import P_SETPOINT, unit_from_panel
from daikin_itm.protocol import ScreenID

from .fake_itm import FakeITM, FakeZone
from .test_client import FakeClient, confirmed, is_logout
from .test_experimental import GUARDED

IDLE_TIMEOUT = 270.0
POLLING = 4


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM whose sessions end when idle, by a clock of the test."""
    fake = FakeITM()
    now = [0.0]
    fake.clock = lambda: now[0]
    fake.idle_timeout = IDLE_TIMEOUT
    fake.now = now  # type: ignore[attr-defined]
    return fake


def _advance(itm: FakeITM, seconds: float) -> None:
    """Move the simulated iTM's clock on."""
    itm.now[0] += seconds  # type: ignore[attr-defined]


async def _polled(itm: FakeITM, after: int) -> None:
    """Wait until the iTM got a POLLING after the first `after` requests."""
    async with asyncio.timeout(2):
        while not any(r["event"] == POLLING for r in itm.requests[after:]):  # noqa: ASYNC110 - polls the simulator's request log
            await asyncio.sleep(0.005)


def _top_level(itm: FakeITM, count: int = 3) -> FakeITM:
    """Put the first units at the icon view's top level, without zones."""
    itm.root = FakeZone("top", itm.units[:count])
    return itm


async def test_keep_alive_keeps_the_login(itm: FakeITM) -> None:
    """Idle for far longer than the iTM allows, one login: the POLLINGs keep it."""
    c = FakeClient(itm)
    async with c.kept_session(keep_alive=0.01):
        assert len(await c.fetch_units()) == 3
        for _ in range(5):
            sent = len(itm.requests)
            _advance(itm, IDLE_TIMEOUT - 70)
            await _polled(itm, sent)
        assert len(await c.fetch_units()) == 3
        assert itm.session is not None
    assert itm.logins == 1
    assert itm.session is None


async def test_without_keep_alive_the_itm_ends_it(itm: FakeITM) -> None:
    """The iTM ends an idle session; the next call logs in again without an error."""
    c = FakeClient(itm)
    async with c.kept_session(keep_alive=None):
        await c.fetch_units()
        await asyncio.sleep(0.02)
        assert not any(r["event"] == POLLING for r in itm.requests)
        _advance(itm, IDLE_TIMEOUT + 30)
        assert len(await c.fetch_units()) == 3
        assert itm.logins == 2
        await c.fetch_units()
    assert itm.logins == 2
    assert itm.session is None


async def test_keep_alive_sees_the_session_ended(
    itm: FakeITM, caplog: pytest.LogCaptureFixture
) -> None:
    """A keep-alive POLLING the iTM answers with its end drops the session."""
    c = FakeClient(itm)
    with caplog.at_level(logging.INFO, logger="daikin_itm.client"):
        async with c.kept_session(keep_alive=0.01):
            await c.fetch_units()
            itm.session = None
            sent = len(itm.requests)
            await _polled(itm, sent)
            await asyncio.sleep(0.02)
            assert not any(is_logout_event(r) for r in itm.requests[sent:])
            assert len(await c.fetch_units()) == 3
    assert itm.logins == 2
    assert any("ended by the iTM" in r.getMessage() for r in caplog.records)


def is_logout_event(request: dict[str, object]) -> bool:
    """Tell whether a recorded request is a LOGOUT."""
    return request["event"] == 10


async def test_keep_alive_without_answer_keeps_the_token(itm: FakeITM) -> None:
    """A keep-alive POLLING that gets no answer: the next call goes on with it."""
    c = FakeClient(itm)
    async with c.kept_session(keep_alive=0.01):
        await c.fetch_units()
        itm.fail_next = aiohttp.ClientConnectionError("network down")
        await _wait(lambda: itm.fail_next is None)
        assert itm.session is not None
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 1


async def _wait(condition: object) -> None:
    """Wait until a condition holds."""
    assert callable(condition)
    async with asyncio.timeout(2):
        while not condition():  # noqa: ASYNC110 - polls a plain condition
            await asyncio.sleep(0.005)


async def test_keep_alive_drops_a_session_moved_elsewhere(itm: FakeITM) -> None:
    """A keep-alive POLLING showing the session away from its start logs it out."""
    c = FakeClient(itm)
    async with c.kept_session(keep_alive=0.01):
        await c.fetch_units()
        itm.root.children[0].name = "Renamed"
        sent = len(itm.requests)
        await _wait(lambda: any(is_logout_event(r) for r in itm.requests[sent:]))
        assert itm.session is None
        itm.root.children[0].name = "Building"
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 2


async def test_keep_alive_logout_without_answer(itm: FakeITM) -> None:
    """A logout of the keep-alive that gets no answer keeps the token for later."""
    c = FakeClient(itm)
    orig = c._post
    failed = asyncio.Event()

    async def post(body: str) -> str:
        if is_logout(body) and not failed.is_set():
            failed.set()
            raise aiohttp.ClientConnectionError
        return await orig(body)

    c._post = post  # type: ignore[method-assign]
    async with c.kept_session(keep_alive=0.01):
        await c.fetch_units()
        stale = itm.session
        itm.root.children[0].name = "Renamed"
        await failed.wait()
        await asyncio.sleep(0.02)
        assert await c._store.load() == stale
        itm.root.children[0].name = "Building"
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 2


async def test_no_answer_before_a_call_keeps_the_session(itm: FakeITM) -> None:
    """The POLLING before a call gets no answer: that call fails, nothing is lost."""
    c = FakeClient(itm)
    async with c.kept_session(keep_alive=None):
        await c.fetch_units()
        sent = len(itm.requests)
        itm.fail_next = aiohttp.ClientConnectionError("network down")
        with pytest.raises(aiohttp.ClientConnectionError):
            await c.fetch_units()
        assert len(itm.requests) == sent
        assert itm.session is not None
        assert len(await c.fetch_units()) == 3
        itm.fail_next = TimeoutError()
        with pytest.raises(TimeoutError):
            await c.fetch_units()
        itm.session = None
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 2


async def test_no_answer_in_a_call_logs_in_again(itm: FakeITM) -> None:
    """A call that loses its answer half way ends the login; the next logs in again."""
    c = FakeClient(itm)
    async with c.kept_session():
        await c.fetch_units()
        orig = c._post
        calls = [0]

        async def post(body: str) -> str:
            calls[0] += 1
            if calls[0] == 4:
                raise aiohttp.ClientConnectionError
            return await orig(body)

        c._post = post  # type: ignore[method-assign]
        with pytest.raises(aiohttp.ClientConnectionError):
            await c.fetch_units()
        assert itm.session is None
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 2


async def test_session_ended_in_a_call(itm: FakeITM) -> None:
    """The iTM ends the session during a call: it raises, the next logs in again."""
    c = FakeClient(itm)
    async with c.kept_session():
        await c.fetch_units()
        orig = c._post
        calls = [0]

        async def post(body: str) -> str:
            calls[0] += 1
            if calls[0] == 3:
                itm.session = None
            return await orig(body)

        c._post = post  # type: ignore[method-assign]
        with pytest.raises(SessionEnded):
            await c.fetch_units()
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 2


async def test_max_age(itm: FakeITM, caplog: pytest.LogCaptureFixture) -> None:
    """A login older than max_age is replaced by a new one."""
    c = FakeClient(itm)
    with caplog.at_level(logging.INFO, logger="daikin_itm.client"):
        async with c.kept_session(max_age=3600):
            await c.fetch_units()
            await c.fetch_units()
            assert itm.logins == 1
            assert c._kept is not None
            c._kept.since -= 3601
            assert len(await c.fetch_units()) == 3
            await c.fetch_units()
    assert itm.logins == 2
    assert any(
        r.getMessage() == "kept session open for more than 3600 s; logging in again"
        and r.levelno == logging.INFO
        for r in caplog.records
    )


async def test_arguments_must_be_positive(itm: FakeITM) -> None:
    """A keep-alive interval or maximum age without time is refused."""
    c = FakeClient(itm)
    with pytest.raises(ValueError, match="keep_alive"):
        async with c.kept_session(keep_alive=0):
            pass
    with pytest.raises(ValueError, match="max_age"):
        async with c.kept_session(max_age=0):
            pass
    assert not c._lock.locked()


async def test_exit_logs_out_and_stops_the_keep_alive(itm: FakeITM) -> None:
    """Leaving the block logs out; no POLLING follows."""
    c = FakeClient(itm)
    async with c.kept_session(keep_alive=0.01):
        await c.fetch_units()
    assert itm.session is None
    sent = len(itm.requests)
    await asyncio.sleep(0.05)
    assert len(itm.requests) == sent


async def test_cancelled_block_logs_out(itm: FakeITM) -> None:
    """A cancelled holder logs out, also while a keep-alive POLLING is under way."""
    c = FakeClient(itm)
    orig = c._post
    polling = asyncio.Event()

    async def post(body: str) -> str:
        if body[36:48] == "ffffffff0004":
            polling.set()
            await asyncio.sleep(0.05)
        return await orig(body)

    reading = asyncio.Event()

    async def hold() -> None:
        async with c.kept_session(keep_alive=0.01):
            await c.fetch_units()
            reading.set()
            c._post = post  # type: ignore[method-assign]
            await asyncio.sleep(10)

    task = asyncio.create_task(hold())
    await reading.wait()
    await polling.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert itm.session is None
    sent = len(itm.requests)
    await asyncio.sleep(0.05)
    assert len(itm.requests) == sent
    assert not c._lock.locked()


async def test_other_client_waits(itm: FakeITM) -> None:
    """Another client of the same user waits for the whole block, within its limit."""
    a = FakeClient(itm)
    b = FakeClient(itm, other_client_timeout=0.05)
    async with a.kept_session():
        await a.fetch_units()
        sent = len(itm.requests)
        with pytest.raises(SessionInUse):
            await b.fetch_units()
        assert len(itm.requests) == sent
    assert len(await b.fetch_units()) == 3


async def test_changes_by_others_show_in_the_next_read(itm: FakeITM) -> None:
    """A change made elsewhere (another account, a wall remote) shows in the block."""
    c = FakeClient(itm)
    async with c.kept_session():
        assert (await c.fetch_units())["1:1-04"].setpoint == 23
        itm.units[1].setpoint = 25
        itm.units[0].running = False
        itm.bar_clock = "Tue, 31/12\n09:30"
        units = await c.fetch_units()
        assert units["1:1-04"].setpoint == 25
        assert not units["1:1-00"].running
        assert (await c.fetch_indicators()).clock_text == "Tue, 31/12\n09:30"
    assert itm.logins == 1


async def test_shared_session_inside_joins(itm: FakeITM) -> None:
    """A shared_session() inside a kept one joins it, with its rules."""
    c = FakeClient(itm)
    async with c.kept_session():
        async with c.shared_session():
            await c.fetch_units()
            assert confirmed(await c.set_setpoint("1:1-04", 22)).setpoint == 22
        async with c.kept_session():
            await c.fetch_units()
    assert itm.logins == 1


async def test_writes_in_the_block(itm: FakeITM) -> None:
    """Unit writes run and confirm in the one login, without allow_experimental."""
    c = FakeClient(itm, allow_experimental=False)
    async with c.kept_session():
        await c.fetch_units()
        assert confirmed(await c.set_setpoint("1:1-04", 22)).setpoint == 22
        u = confirmed(await c.apply("1:1-04", mode="heat", fan_speed="high"))
        assert (u.mode, u.fan_speed) == ("heat", "high")
        u = confirmed(await c.set_power_state("1:1-04", PowerState.OFF))
        assert not u.running
        settings = await c.set_unit_settings(
            "1:1-04", setpoint_lock=RemoteLock.FORBIDDEN
        )
        assert settings is not None
        assert settings.remote_locks.setpoint == RemoteLock.FORBIDDEN
        assert await c.set_setback(high_up=2) is not None
        assert itm.session is not None
    assert itm.logins == 1
    assert itm.units[1].setpoint == 22
    assert itm.setback["high_up"] == 2


async def test_zone_write_in_the_block(itm: FakeITM) -> None:
    """A zone write confirms on every member; an unsent pick costs a login."""
    c = FakeClient(itm, zone_path=("Building",))
    async with c.kept_session():
        units = await c.set_zone_power_state("Indoor units", PowerState.ON)
        assert units is not None
        assert all(u.running for u in units.values())
        assert itm.logins == 2
        assert await c.set_zone_setpoint("Indoor units", 22) is not None
    assert itm.logins == 3
    assert all(u.running and u.setpoint == 22 for u in itm.units)


async def test_writes_keep_their_spacing(itm: FakeITM) -> None:
    """Two writes to one unit in the block are still min_write_interval apart."""
    c = FakeClient(itm, min_write_interval=0.1)
    async with c.kept_session():
        started = time.monotonic()
        await c.set_setpoint("1:1-04", 21)
        await c.set_setpoint("1:1-04", 22)
        assert time.monotonic() - started >= 0.1
    assert itm.units[1].setpoint == 22


async def test_session_of_the_block_writes_without_the_opt_in(itm: FakeITM) -> None:
    """The kept session itself sends a pick without allow_experimental."""
    c = FakeClient(itm, allow_experimental=False)
    async with c.kept_session(), c._session() as s:
        await s.open_zone(c.zone_path)
        await s.select_by_name("1:1-04")
        await s.choose(P_SETPOINT, 0)
    assert any(r["screen"] == 4 and r["part"] == 11 for r in itm.requests)


async def test_dry_run_in_the_block_without_the_opt_in(itm: FakeITM) -> None:
    """A dry run of a validated write inside needs no allow_experimental either."""
    dry = FakeClient(itm, allow_experimental=False, dry_run=True)
    async with dry.kept_session():
        assert await dry.set_setpoint("1:1-04", 21) is None
    assert dry.planned
    assert itm.units[1].setpoint == 23


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(("method", "args", "kwargs"), GUARDED)
async def test_experimental_write_needs_the_opt_in(
    itm: FakeITM,
    method: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    *,
    dry_run: bool,
) -> None:
    """A write not validated yet is still refused inside without the opt-in, unsent."""
    c = FakeClient(itm, allow_experimental=False, dry_run=dry_run)
    async with c.kept_session():
        await c.fetch_indicators()
        sent = len(itm.requests)
        with pytest.raises(ExperimentalError):
            await getattr(c, method)(*args, **kwargs)
        assert len(itm.requests) == sent
    assert itm.logins == 1


async def test_own_settings_stay_refused(itm: FakeITM) -> None:
    """The user's own settings log in by themselves: refused, also when allowed."""
    c = FakeClient(itm)
    async with c.kept_session():
        with pytest.raises(SharedSessionError):
            await c.set_own_settings(time_24h=False)
        with pytest.raises(SharedSessionError):
            await c.fetch_own_settings()
    assert itm.logins == 0


async def test_a_pick_is_echoed_in_its_own_session(itm: FakeITM) -> None:
    """Why the read-back needs care: the panel keeps a pick the unit didn't take."""
    _top_level(itm)
    itm.sticky_picks = True
    itm.units[0].ignores_writes = True
    c = FakeClient(itm, zone_path=())
    async with c._session() as s:
        await s.select_by_name("1:1-00")
        await s.choose(P_SETPOINT, 3)
        await s.commit(0)
        await s.select_by_name("1:1-00")
        assert unit_from_panel(s.state).setpoint == 22
        assert s.state.picked(ScreenID.OPERATION) == {(P_SETPOINT, "SINGLE_SELIDX")}
    assert itm.units[0].setpoint == 21


async def test_read_back_is_the_itm_not_the_pick(itm: FakeITM) -> None:
    """A write the unit doesn't take is not confirmed by its own pick."""
    _top_level(itm)
    itm.sticky_picks = True
    itm.units[0].ignores_writes = True
    c = FakeClient(itm, zone_path=(), confirm_timeout=0.05)
    async with c.kept_session():
        with pytest.raises(ItmCommandError, match="setpoint=21"):
            await c.set_setpoint("1:1-00", 22)
        assert itm.session is not None
        assert confirmed(await c.set_setpoint("1:1-04", 24)).setpoint == 24
    assert itm.logins == 1
    assert itm.units[0].setpoint == 21


async def test_pick_not_sent_again_ends_the_login(itm: FakeITM) -> None:
    """A pick the iTM doesn't overwrite ends the login: the read-back logs in anew."""
    _top_level(itm, count=1)
    itm.sticky_picks = True
    itm.units[0].ignores_writes = True
    c = FakeClient(itm, zone_path=(), confirm_timeout=0.05)
    async with c.kept_session():
        with pytest.raises(ItmCommandError, match="setpoint=21"):
            await c.set_setpoint("1:1-00", 22)
        assert itm.logins == 2
        itm.units[0].ignores_writes = False
        assert confirmed(await c.set_setpoint("1:1-00", 22)).setpoint == 22
    assert itm.logins == 3


async def test_pick_another_point_doesnt_overwrite(itm: FakeITM) -> None:
    """A pick the iTM doesn't send for the next point either is no read-back."""
    _top_level(itm)
    itm.forgets_picks = True
    itm.units[0].ignores_writes = True
    for u in itm.units[1:3]:
        u.setpoint = itm.units[0].setpoint
    c = FakeClient(itm, zone_path=(), confirm_timeout=0.05)
    async with c.kept_session():
        with pytest.raises(ItmCommandError, match="setpoint=21"):
            await c.set_setpoint("1:1-00", 22)
        assert (await c.fetch_units())["1:1-04"].setpoint == 21
    assert itm.units[0].setpoint == 21


async def test_calls_from_any_task_use_the_block(itm: FakeITM) -> None:
    """A call from a task started outside the block uses its login, no wait."""
    c = FakeClient(itm)
    async with c.kept_session():
        await c.fetch_units()
        outside = contextvars.Context()
        read = asyncio.create_task(c.fetch_units(), context=outside)
        own = asyncio.create_task(c.fetch_own_settings(), context=outside)
        done, _ = await asyncio.wait({read, own}, timeout=2)
        assert done == {read, own}
        assert len(read.result()) == 3
        assert isinstance(own.exception(), SharedSessionError)
    assert itm.logins == 1
    assert len(await c.fetch_units()) == 3
    assert itm.logins == 2


async def test_keep_alive_survives_an_unexpected_error(itm: FakeITM) -> None:
    """A keep-alive poll that raises something unforeseen: it goes on, exit logs out."""
    c = FakeClient(itm)
    orig = c._keep_alive_poll
    calls = [0]

    async def poll(*args: object) -> None:
        calls[0] += 1
        if calls[0] == 1:
            msg = "disk full"
            raise OSError(msg)
        await orig(*args)  # type: ignore[arg-type]

    c._keep_alive_poll = poll  # type: ignore[method-assign, assignment]
    async with c.kept_session(keep_alive=0.01):
        await c.fetch_units()
        await _wait(lambda: calls[0] >= 3)
        assert len(await c.fetch_units()) == 3
    assert itm.session is None
    assert itm.logins == 1
