# pylint: disable=protected-access,redefined-outer-name
"""Writes counted and budgeted, writes skipped that change nothing, checks that hold."""

from __future__ import annotations

import dataclasses
import functools
import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest

from daikin_itm import (
    ItmCommandError,
    LoginRejected,
    NotConfirmed,
    PowerState,
    RemoteLock,
    Unit,
    UnitSettings,
    UnitStopped,
    WriteBudgetExceeded,
    WriteOutcomeUnknown,
)
from daikin_itm.models import D_OK, D_OVERTIME_CHECK, P_SETPOINT, unit_from_panel
from daikin_itm.protocol import ScreenID

from .fake_itm import FakeITM
from .test_client import FakeClient, confirmed

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


TIMER = 3
POLLING = 4
ZONE = "Indoor units"


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM."""
    return FakeITM()


def _commits(itm: FakeITM) -> int:
    return sum(r["event"] == TIMER for r in itm.requests)


def _settings_oks(itm: FakeITM) -> int:
    return sum(
        r["screen"] == ScreenID.SETTINGS and r["part"] == D_OK for r in itm.requests
    )


def _zone_client(itm: FakeITM, **kw: Any) -> FakeClient:
    return FakeClient(itm, zone_path=("Building",), **kw)


# -- counting and the budget ----------------------------------------------------------


async def test_writes_sent_counts_changes_only(itm: FakeITM) -> None:
    """Only a change sent counts; a no-op and a dry run don't, nor space a write."""
    calls: list[tuple[str, str]] = []
    c = FakeClient(itm, on_write=lambda unit, what: calls.append((unit, what)))
    confirmed(await c.set_setpoint("1:1-04", 22))
    confirmed(await c.set_setpoint("1:1-04", 22))
    confirmed(await c.apply("1:1-00", mode="heat"))
    confirmed(await c.apply("1:1-00", mode="cool", fan_speed="high"))
    assert dict(c.writes_sent) == {"1:1-04": 1, "1:1-00": 1}
    assert calls == [("1:1-04", "setpoint 22"), ("1:1-00", "mode cool, fan speed high")]
    assert c.recent_writes() == {"1:1-04": 1, "1:1-00": 1}
    assert c.recent_writes(0) == {}
    with pytest.raises(TypeError):
        c.writes_sent["1:1-04"] = 0  # type: ignore[index]
    assert "1:1-09" not in c._last_write
    await c.set_power_state("1:1-09", PowerState.OFF)
    assert "1:1-09" not in c._last_write
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_setpoint("1:1-04", 24) is None
    assert not dry.writes_sent


async def test_zone_write_counts_every_member(itm: FakeITM) -> None:
    """A zone write through the zone's panel counts once for every point of it."""
    for u in itm.units:
        u.running = False
    calls: list[tuple[str, str]] = []
    c = _zone_client(itm, on_write=lambda unit, what: calls.append((unit, what)))
    await c.set_zone_power_state(ZONE, PowerState.ON)
    names = [u.name for u in itm.units]
    assert dict(c.writes_sent) == dict.fromkeys(names, 1)
    assert calls == [(n, "power on") for n in names]
    assert all(c._last_write[n] > 0 for n in names)


async def test_lost_reply_counts(itm: FakeITM) -> None:
    """A change whose reply was lost was sent: it counts."""
    c = FakeClient(itm)
    itm.lose_reply_next = TimeoutError()
    itm.fault_on = lambda ev: ev["event"] == TIMER
    with pytest.raises(WriteOutcomeUnknown):
        await c.set_setpoint("1:1-04", 22)
    assert dict(c.writes_sent) == {"1:1-04": 1}


async def test_daily_budget_refuses_before_anything_is_sent(itm: FakeITM) -> None:
    """A spent budget refuses the next write to the unit before its login."""
    c = FakeClient(itm, max_writes_per_unit_per_day=2)
    await c.set_setpoint("1:1-04", 22)
    await c.set_setpoint("1:1-04", 21)
    logins, sent = itm.logins, len(itm.requests)
    with pytest.raises(WriteBudgetExceeded) as caught:
        await c.set_setpoint("1:1-04", 24)
    assert isinstance(caught.value, ItmCommandError)
    assert (caught.value.name, caught.value.limit) == ("1:1-04", 2)
    assert caught.value.seconds == 86400
    assert (itm.logins, len(itm.requests)) == (logins, sent)
    assert itm.units[1].setpoint == 21
    await c.set_setpoint("1:1-00", 22)
    c._write_times["1:1-04"][0] -= 86400
    assert c.recent_writes() == {"1:1-04": 1, "1:1-00": 1}
    assert confirmed(await c.set_setpoint("1:1-04", 24)).setpoint == 24


async def test_budget_refuses_before_the_settle(itm: FakeITM) -> None:
    """A spent budget refuses before the check of an earlier change logs in."""
    c = FakeClient(itm, max_writes_per_unit_per_day=1, confirm_in_session=False)
    assert await c.set_setpoint("1:1-04", 22, confirm=False) is None
    logins = itm.logins
    with pytest.raises(WriteBudgetExceeded):
        await c.set_setpoint("1:1-04", 21)
    assert itm.logins == logins
    assert "1:1-04" in c._unconfirmed


async def test_hourly_budget(itm: FakeITM) -> None:
    """The hourly budget counts the last hour only."""
    c = FakeClient(itm, max_writes_per_unit_per_hour=1)
    await c.set_setpoint("1:1-04", 22)
    with pytest.raises(WriteBudgetExceeded, match="last hour") as caught:
        await c.set_setpoint("1:1-04", 21)
    assert caught.value.seconds == 3600
    c._write_times["1:1-04"][0] -= 3601
    await c.set_setpoint("1:1-04", 21)
    assert c.writes_sent["1:1-04"] == 2
    dry = FakeClient(itm, max_writes_per_unit_per_hour=1, dry_run=True)
    dry._write_times["1:1-04"] = c._write_times["1:1-04"]
    assert await dry.set_setpoint("1:1-04", 22) is None


async def test_zone_budget(itm: FakeITM) -> None:
    """A zone write is refused before its change when a member's budget is spent."""
    c = _zone_client(itm, max_writes_per_unit_per_day=1)
    await c.set_zone_power_state(ZONE, PowerState.ON)
    commits = _commits(itm)
    with pytest.raises(WriteBudgetExceeded):
        await c.set_zone_power_state(ZONE, PowerState.OFF)
    assert _commits(itm) == commits
    assert all(u.running for u in itm.units)
    units = await c.set_zone_power_state(ZONE, PowerState.ON)
    assert units is not None
    assert all(u.running for u in units.values())


async def test_budget_warning_and_a_failing_hook(
    itm: FakeITM, caplog: pytest.LogCaptureFixture
) -> None:
    """80% of a budget is logged; an on_write that raises is logged, the write holds."""

    def hook(_unit: str, _what: str) -> None:
        msg = "hook"
        raise RuntimeError(msg)

    c = FakeClient(itm, max_writes_per_unit_per_hour=5, on_write=hook)
    with caplog.at_level(logging.WARNING, logger="daikin_itm.client"):
        for value in (22, 21, 22):
            assert confirmed(await c.set_setpoint("1:1-04", value)).setpoint == value
        assert "writes sent in the last hour" not in caplog.text
        await c.set_setpoint("1:1-04", 21)
    assert "1:1-04: 4 writes sent in the last hour, 5 allowed" in caplog.text
    assert "on_write failed for 1:1-04" in caplog.text


@pytest.mark.parametrize(
    "kw", [{"max_writes_per_unit_per_hour": 0}, {"max_writes_per_unit_per_day": -1}]
)
def test_budget_must_be_positive(itm: FakeITM, kw: dict[str, Any]) -> None:
    """A budget below one is refused."""
    with pytest.raises(ValueError, match="at least 1"):
        FakeClient(itm, **kw)


# -- set_unit_settings ---------------------------------------------------------------


async def test_unit_settings_already_shown_write_nothing(itm: FakeITM) -> None:
    """Settings the dialog already shows: Cancel, the status read, nothing waits."""
    itm.units[0].thermo_on = True
    c = FakeClient(itm)
    name = itm.units[0].name
    result = await c.set_unit_settings(
        name, overtime=False, start_stop_lock=RemoteLock.ALLOWED
    )
    assert isinstance(result, UnitSettings)
    assert result.overtime is False
    assert result.thermo_on is True
    assert result.flags
    assert _settings_oks(itm) == 0
    assert not c.writes_sent
    assert not c._unconfirmed
    assert name not in c._last_write


async def test_unit_settings_send_only_what_differs(itm: FakeITM) -> None:
    """A setting already shown isn't ticked; the others are saved."""
    c = FakeClient(itm)
    name = itm.units[0].name
    result = await c.set_unit_settings(
        name, overtime=False, mode_lock=RemoteLock.FORBIDDEN
    )
    assert result is not None
    assert result.remote_locks.mode is RemoteLock.FORBIDDEN
    assert _settings_oks(itm) == 1
    assert not any(
        r["screen"] == ScreenID.SETTINGS and r["part"] == D_OVERTIME_CHECK
        for r in itm.requests
    )


async def test_reset_is_never_skipped(itm: FakeITM) -> None:
    """A reset the dialog doesn't offer is still refused, not taken as done."""
    with pytest.raises(ItmCommandError, match="nothing to reset"):
        await FakeClient(itm).set_unit_settings(
            itm.units[0].name, reset_filter_sign=True
        )


async def test_unit_settings_check_holds_the_unit_lock(itm: FakeITM) -> None:
    """The settings check runs inside the unit lock, as for the other unit writes."""
    c = FakeClient(itm)
    name = itm.units[0].name
    held: list[bool] = []
    check = c._confirm_settings

    async def spy(unit: str, changes: dict[str, Any], **kw: Any) -> UnitSettings:
        held.append(c._unit_lock(unit).locked())
        return await check(unit, changes, **kw)

    c._confirm_settings = spy  # type: ignore[method-assign]
    assert await c.set_unit_settings(name, overtime=True) is not None
    assert held == [True]


async def test_unit_settings_without_confirm(itm: FakeITM) -> None:
    """confirm=False returns at once; the next write to the unit settles first."""
    c = FakeClient(itm)
    name = itm.units[0].name
    assert await c.set_unit_settings(name, overtime=True, confirm=False) is None
    assert name in c._unconfirmed
    assert itm.units[0].overtime is True
    assert await c.set_unit_settings(name, mode_lock=RemoteLock.FORBIDDEN)
    assert not c._unconfirmed


# -- zone writes that already match --------------------------------------------------


async def test_zone_write_that_already_matches_writes_nothing(itm: FakeITM) -> None:
    """Every indoor unit already shows it: the members, no commit, nothing counted.

    Without confirm the call checks that it still shows it and leaves nothing.
    """
    for u in itm.units:
        u.running = True
    c = _zone_client(itm, verify_timeout=0.05)
    units = await c.set_zone_power_state(ZONE, PowerState.ON, confirm=False)
    assert units is not None
    assert set(units) == {u.name for u in itm.units}
    applied = await c.apply_zone(ZONE, power=True, confirm=False)
    assert applied is not None
    for u in itm.units:
        u.setpoint = 22
    assert await c.set_zone_setpoint(ZONE, 22, confirm=False) is not None
    assert _commits(itm) == 0
    assert _settings_oks(itm) == 0
    assert not c.writes_sent
    assert not c._unconfirmed
    assert not c._last_write


async def test_zone_write_that_matches_in_part_writes_all(itm: FakeITM) -> None:
    """One member differs: the zone is written as before."""
    c = _zone_client(itm)
    assert await c.set_zone_setpoint(ZONE, 21) is not None
    assert _commits(itm) == 1
    assert all(u.setpoint == 21 for u in itm.units if u.running)
    for u in itm.units:
        u.running = False
    with pytest.raises(UnitStopped):
        await c.set_zone_setpoint(ZONE, 21)


# -- checks that survive a transient error -------------------------------------------


@pytest.mark.parametrize("in_session", [True, False])
async def test_check_survives_a_lost_reply(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch, *, in_session: bool
) -> None:
    """A check whose reply is lost tries again: the write is confirmed."""
    itm.latency_logins = 2
    c = FakeClient(itm, confirm_in_session=in_session, confirm_timeout=5)
    fetch = c.fetch_unit
    first = True

    async def lossy(name: str) -> Unit:
        nonlocal first
        if first:
            first = False
            itm.lose_reply_next = TimeoutError()
            itm.fault_on = lambda ev: ev["screen"] == ScreenID.STDICON
        return await fetch(name)

    monkeypatch.setattr(c, "fetch_unit", lossy)
    assert confirmed(await c.set_setpoint("1:1-04", 22)).setpoint == 22
    assert len(itm.lost_replies) == 1
    assert itm.session is None


async def test_settle_survives_a_lost_reply(itm: FakeITM) -> None:
    """The next write's settle survives a lost reply and still waits for the change."""
    itm.latency_logins = 2
    c = FakeClient(itm, confirm_timeout=5)
    assert await c.set_setpoint("1:1-04", 22, confirm=False) is None
    itm.lose_reply_next = TimeoutError()
    itm.fault_on = lambda ev: ev["screen"] == ScreenID.STDICON
    assert confirmed(await c.set_setpoint("1:1-04", 21)).setpoint == 21
    assert len(itm.lost_replies) == 1
    assert itm.overlaps == 0


async def test_check_raises_at_once_on_a_refused_login(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A refused login is no transient error: the check raises it at once."""
    c = FakeClient(itm, confirm_in_session=False, confirm_timeout=5)
    errors: list[Exception] = [TimeoutError(), LoginRejected("refused")]
    reads = 0

    async def fetch_unit(_name: str) -> Unit:
        nonlocal reads
        reads += 1
        raise errors.pop(0)

    monkeypatch.setattr(c, "fetch_unit", fetch_unit)
    with pytest.raises(LoginRejected):
        await c.set_setpoint("1:1-04", 22)
    assert reads == 2


async def test_checks_give_up_at_their_deadline(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Checks that never get an answer: NotConfirmed with the last error, no state."""
    c = _zone_client(itm, confirm_in_session=False, confirm_timeout=0.05)

    async def no_answer(*_args: Any) -> Any:
        raise TimeoutError

    for read in ("fetch_unit", "fetch_zone_units", "fetch_unit_settings"):
        monkeypatch.setattr(c, read, no_answer)
    c.zone_path = ("Building", ZONE)
    with pytest.raises(NotConfirmed, match="no read succeeded") as caught:
        await c.set_setpoint("1:1-04", 22)
    assert isinstance(caught.value, TimeoutError)
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert caught.value.last_seen is None
    assert caught.value.name == "1:1-04"
    assert str(caught.value).startswith("1:1-04: setpoint 22 not confirmed")
    with pytest.raises(TimeoutError, match="could not be read back") as settings:
        await c.set_unit_settings("1:1-04", overtime=True)
    assert isinstance(settings.value, NotConfirmed)
    c.zone_path = ("Building",)
    for u in itm.units:
        u.running = True
    with pytest.raises(TimeoutError, match="no read succeeded") as zone:
        await c.set_zone_power_state(ZONE, PowerState.OFF)
    assert isinstance(zone.value, NotConfirmed)
    assert len({type(zone.value), type(caught.value)}) == 1


async def test_check_with_a_last_read_lost_keeps_what_it_saw(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An answer, then none until the deadline: the state seen, the error's type."""
    c = FakeClient(itm, confirm_in_session=False, confirm_timeout=0.05)
    fetch = c.fetch_unit
    reads = 0

    async def then_lost(name: str) -> Unit:
        nonlocal reads
        reads += 1
        if reads > 1:
            raise aiohttp.ServerDisconnectedError
        unit = await fetch(name)
        return dataclasses.replace(unit, setpoint=23)

    monkeypatch.setattr(c, "fetch_unit", then_lost)
    with pytest.raises(aiohttp.ServerDisconnectedError) as caught:
        await c.set_setpoint("1:1-04", 22)
    assert isinstance(caught.value, NotConfirmed)
    assert isinstance(caught.value.last_seen, Unit)
    assert caught.value.last_seen.setpoint == 23
    assert "last seen: power=" in str(caught.value)


async def test_old_write_times_are_forgotten(itm: FakeITM) -> None:
    """Without a budget the times of writes over a day old are not kept."""
    c = FakeClient(itm)
    await c.set_setpoint("1:1-04", 22)
    c._write_times["1:1-04"][0] -= 86400
    await c.set_setpoint("1:1-04", 21)
    assert len(c._write_times["1:1-04"]) == 1
    assert c.writes_sent["1:1-04"] == 2


# -- a later read clears an unchecked change -----------------------------------------


async def _zone_read(c: FakeClient) -> object:
    c.zone_path = ("Building",)
    return await c.fetch_zone_units(ZONE)


READS: dict[str, Callable[[FakeClient], Awaitable[object]]] = {
    "fetch_unit": lambda c: c.fetch_unit("1:1-04"),
    "fetch_units": lambda c: c.fetch_units(),
    "fetch_points": lambda c: c.fetch_points(),
    "fetch_zone_units": _zone_read,
}


@pytest.mark.parametrize("read", READS.values(), ids=READS.keys())
async def test_read_clears_an_unchecked_change(
    itm: FakeITM, read: Callable[[FakeClient], Awaitable[object]]
) -> None:
    """A read that shows the change drops it: the next write doesn't wait for it."""
    c = FakeClient(itm)
    assert await c.set_setpoint("1:1-04", 22, confirm=False) is None
    assert "1:1-04" in c._unconfirmed
    await read(c)
    assert not c._unconfirmed


async def test_read_before_the_change_shows_keeps_it(itm: FakeITM) -> None:
    """A read that doesn't show the change yet keeps it for the next write."""
    itm.latency_logins = 3
    c = FakeClient(itm)
    assert await c.set_setpoint("1:1-04", 22, confirm=False) is None
    assert (await c.fetch_unit("1:1-04")).setpoint == 23
    assert "1:1-04" in c._unconfirmed


async def test_read_in_a_write_block_clears_nothing(itm: FakeITM) -> None:
    """A read that comes from a write's login doesn't drop an unchecked change."""
    c = FakeClient(itm)
    assert await c.set_setpoint("1:1-04", 22, confirm=False) is None
    async with c._write_session():
        assert (await c.fetch_unit("1:1-04")).setpoint == 22
    assert "1:1-04" in c._unconfirmed


async def test_read_with_a_pick_clears_nothing(itm: FakeITM) -> None:
    """A session whose panel holds a pick of the client doesn't drop a change."""
    c = FakeClient(itm)
    assert await c.set_setpoint("1:1-04", 22, confirm=False) is None
    async with c._session() as s:
        await s.open_zone(c.zone_path)
        await s.select_by_name("1:1-04")
        unit = unit_from_panel(s.state)
        assert unit.setpoint == 22
        s.state.select_locally(ScreenID.OPERATION, P_SETPOINT, 3)
        c._seen(s, [unit])
        assert "1:1-04" in c._unconfirmed
        s.state.screens[ScreenID.OPERATION].clear()
        c._seen(s, [unit, object()])
    assert not c._unconfirmed


async def test_kept_session_read_clears_an_unchecked_change(itm: FakeITM) -> None:
    """In a kept session a read after the way back drops a change it shows."""
    c = FakeClient(itm)
    async with c.kept_session():
        assert await c.set_setpoint("1:1-04", 22, confirm=False) is None
        assert "1:1-04" in c._unconfirmed
        assert (await c.fetch_unit("1:1-04")).setpoint == 22
        assert not c._unconfirmed
    assert itm.logins == 1


async def test_zone_change_cleared_per_indoor_unit(itm: FakeITM) -> None:
    """A zone change is dropped for each indoor unit a read shows it on."""
    for u in itm.units:
        u.running = True
    c = _zone_client(itm)
    assert await c.set_zone_power_state(ZONE, PowerState.OFF, confirm=False) is None
    assert set(c._unconfirmed) == {u.name for u in itm.units}
    itm.units[0].running = True
    await c.fetch_zone_units(ZONE)
    assert set(c._unconfirmed) == {itm.units[0].name}


async def test_settings_change_cleared_by_a_settings_read(itm: FakeITM) -> None:
    """An unchecked settings change is dropped by a settings read that shows it."""
    c = FakeClient(itm)
    name = itm.units[0].name
    assert await c.set_unit_settings(name, overtime=True, confirm=False) is None
    await c.fetch_unit(name)
    assert name in c._unconfirmed
    assert (await c.fetch_unit_settings(name)).overtime is True
    assert not c._unconfirmed


async def test_other_checks_are_never_cleared(itm: FakeITM) -> None:
    """Only the client's own unit and zone checks are recognised."""

    async def later() -> None:
        return None

    c = FakeClient(itm)
    c._unconfirmed["1:1-04"] = later
    c._unconfirmed["1:1-00"] = functools.partial(c.fetch_unit, "1:1-00")
    await c.fetch_units()
    assert set(c._unconfirmed) == {"1:1-04", "1:1-00"}
