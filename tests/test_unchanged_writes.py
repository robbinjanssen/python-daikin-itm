# pylint: disable=protected-access,redefined-outer-name
"""A write that finds its change made already: checked a while, sent after all."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

import aiohttp
import pytest

from daikin_itm import (
    NotConfirmed,
    PowerState,
    RemoteLock,
    Unit,
    UnitSettings,
    WriteBudgetExceeded,
)
from daikin_itm.models import (
    D_FAN_CHECK,
    D_LOCK_SETPOINT_CHECK,
    D_LOCK_START_STOP_CHECK,
    D_MODE_CHECK,
    D_OK,
    D_POWER_CHECK,
    D_SETPOINT_CHECK,
)
from daikin_itm.protocol import ScreenID

from .fake_itm import FakeITM, FakeUnit
from .fake_itm_base import FakeZone
from .test_client import FakeClient
from .test_kept_session import _wait

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from daikin_itm import ItmClient

TIMER = 3
POLLING = 4
UNIT = "1:1-00"
ZONE = "Indoor units"
TICKS = {
    D_POWER_CHECK,
    D_MODE_CHECK,
    D_FAN_CHECK,
    D_SETPOINT_CHECK,
    D_LOCK_START_STOP_CHECK,
    D_LOCK_SETPOINT_CHECK,
}


class Write(Protocol):
    """A write, with or without its confirm."""

    def __call__(self, c: ItmClient, *, confirm: bool) -> Awaitable[object]:
        """Start the write."""


@dataclass(frozen=True)
class Case:
    """A write whose change the unit shows already, and another client's change."""

    call: Write
    elsewhere: dict[str, Any]
    ours: Callable[[FakeUnit], bool]
    zone: bool = False


CASES = {
    "set_unit_settings": Case(
        lambda c, *, confirm: c.set_unit_settings(
            UNIT, setpoint_lock=RemoteLock.ALLOWED, confirm=confirm
        ),
        {"lock_setpoint": 2},
        lambda u: u.lock_setpoint == 0,
    ),
    "apply": Case(
        lambda c, *, confirm: c.apply(
            UNIT, mode="heat", fan_speed="medium", confirm=confirm
        ),
        {"mode": "Cool"},
        lambda u: u.mode == "Heat",
    ),
    "set_mode": Case(
        lambda c, *, confirm: c.set_mode(UNIT, "heat", confirm=confirm),
        {"mode": "Cool"},
        lambda u: u.mode == "Heat",
    ),
    "set_power": Case(
        lambda c, *, confirm: c.set_power(UNIT, on=True, confirm=confirm),
        {"running": False},
        lambda u: u.running,
    ),
    "set_fan_speed": Case(
        lambda c, *, confirm: c.set_fan_speed(UNIT, "medium", confirm=confirm),
        {"fan_icon": 3013},
        lambda u: u.fan_icon == 3011,
    ),
    "set_setpoint": Case(
        lambda c, *, confirm: c.set_setpoint(UNIT, 21, confirm=confirm),
        {"setpoint": 23},
        lambda u: u.setpoint == 21,
    ),
    "set_power_state": Case(
        lambda c, *, confirm: c.set_power_state(UNIT, PowerState.ON, confirm=confirm),
        {"running": False},
        lambda u: u.running,
    ),
    "set_zone_power_state": Case(
        lambda c, *, confirm: c.set_zone_power_state(
            ZONE, PowerState.ON, confirm=confirm
        ),
        {"running": False},
        lambda u: u.running,
        zone=True,
    ),
    "apply_zone": Case(
        lambda c, *, confirm: c.apply_zone(ZONE, mode="heat", confirm=confirm),
        {"mode": "Cool"},
        lambda u: u.mode == "Heat",
        zone=True,
    ),
    "set_zone_setpoint": Case(
        lambda c, *, confirm: c.set_zone_setpoint(ZONE, 21, confirm=confirm),
        {"setpoint": 23},
        lambda u: u.setpoint == 21,
        zone=True,
    ),
}


def _locks(result: object) -> RemoteLock:
    assert isinstance(result, UnitSettings)
    return result.remote_locks.setpoint


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM whose units all run in heating at 21 degrees."""
    fake = FakeITM()
    fake.latency_logins = 3
    for u in fake.units:
        u.running, u.mode, u.setpoint, u.fan_icon = True, "Heat", 21, 3011
    return fake


def _client(itm: FakeITM, case: Case, **kw: Any) -> FakeClient:
    kw.setdefault("confirm_interval", 0.001)
    kw.setdefault("confirm_timeout", 2)
    kw.setdefault("verify_timeout", 1)
    if case.zone:
        kw["zone_path"] = ("Building",)
    return FakeClient(itm, **kw)


def _is_write(r: dict[str, Any]) -> bool:
    return bool(
        r["event"] == TIMER or (r["screen"] == ScreenID.SETTINGS and r["part"] == D_OK)
    )


def _writes(itm: FakeITM) -> int:
    return sum(_is_write(r) for r in itm.requests)


def _first_write(itm: FakeITM) -> int:
    return next(i for i, r in enumerate(itm.requests) if _is_write(r))


def _pollings(itm: FakeITM) -> int:
    return sum(r["event"] == POLLING for r in itm.requests)


def _write_indexes(itm: FakeITM) -> list[int]:
    return [i for i, r in enumerate(itm.requests) if _is_write(r)]


def _ticks(itm: FakeITM, start: int, end: int) -> set[int]:
    return {
        r["part"]
        for r in itm.requests[start:end]
        if r["screen"] == ScreenID.SETTINGS and r["part"] in TICKS
    }


@pytest.mark.parametrize("confirm", [True, False])
@pytest.mark.parametrize("name", sorted(CASES))
async def test_a_change_coming_in_is_overruled(
    itm: FakeITM, name: str, *, confirm: bool
) -> None:
    """Another client's change shows after the write found its own value: sent anyway.

    The write's own read still showed the value asked for, so nothing was sent;
    the check then sees the other change arrive and sends the write after all,
    once, to the unit that moved, and returns it confirmed, in the same login,
    with or without confirm.
    """
    case = CASES[name]
    c = _client(itm, case)
    target = itm.units[0]
    itm.change_elsewhere(target, 4, **case.elsewhere)
    result = await case.call(c, confirm=confirm)
    assert result is not None
    assert all(case.ours(u) for u in itm.units)
    assert _writes(itm) == 1
    assert itm.changed_elsewhere
    assert _first_write(itm) > itm.changed_elsewhere[0]
    assert dict(c.writes_sent) == {UNIT: 1}
    assert not c._unconfirmed
    if name == "set_unit_settings":
        assert _locks(result) == RemoteLock.ALLOWED
    elif case.zone:
        assert isinstance(result, dict)
        assert set(result) >= {u.name for u in itm.units}
    else:
        assert isinstance(result, Unit)


@pytest.mark.parametrize("confirm", [True, False])
@pytest.mark.parametrize("name", sorted(CASES))
async def test_truly_unchanged_writes_nothing(
    itm: FakeITM, name: str, *, confirm: bool
) -> None:
    """Nothing comes in: no write, the value read last after the window, one login."""
    case = CASES[name]
    c = _client(itm, case, verify_timeout=0.1)
    start = time.monotonic()
    result = await case.call(c, confirm=confirm)
    assert time.monotonic() - start >= 0.1
    assert result is not None
    if name == "set_unit_settings":
        assert _locks(result) == RemoteLock.ALLOWED
    assert _writes(itm) == 0
    assert not c.writes_sent
    assert not c._last_write
    assert not c._unconfirmed
    assert itm.logins == 1
    assert _pollings(itm) >= 2


@pytest.mark.parametrize("confirm", [True, False])
@pytest.mark.parametrize("name", sorted(CASES))
async def test_still_overruled_after_the_write(
    itm: FakeITM, name: str, *, confirm: bool
) -> None:
    """The other change keeps winning: NotConfirmed after exactly one write."""
    case = CASES[name]
    c = _client(itm, case, confirm_timeout=0.3, verify_timeout=0.3)
    target = itm.units[0]
    itm.change_elsewhere(target, 4, **case.elsewhere)
    target.ignores_writes = True
    with pytest.raises(NotConfirmed) as err:
        await case.call(c, confirm=confirm)
    assert err.value.last_seen is not None
    assert not case.ours(target)
    assert _writes(itm) == 1
    assert dict(c.writes_sent) == {UNIT: 1}
    assert not c._unconfirmed


CHANGES: dict[str, Write] = {
    "apply": lambda c, *, confirm: c.apply(
        UNIT, mode="cool", setpoint=22, confirm=confirm
    ),
    "set_unit_settings": lambda c, *, confirm: c.set_unit_settings(
        UNIT, setpoint_lock=RemoteLock.FORBIDDEN, confirm=confirm
    ),
    "set_zone_setpoint": lambda c, *, confirm: c.set_zone_setpoint(
        ZONE, 22, confirm=confirm
    ),
}


@pytest.mark.parametrize("name", sorted(CHANGES))
async def test_a_change_without_confirm_returns_at_once(
    itm: FakeITM, name: str
) -> None:
    """Everything asked differs: sent, not watched, the check left to the next write."""
    c = _client(itm, CASES["apply_zone"], verify_timeout=30, confirm_timeout=30)
    if not name.startswith("set_zone"):
        c.zone_path = ("Building", ZONE)
    start = time.monotonic()
    assert await CHANGES[name](c, confirm=False) is None
    assert time.monotonic() - start < 5
    assert _writes(itm) == 1
    members = {u.name for u in itm.units} if name.startswith("set_zone") else {UNIT}
    assert set(c._unconfirmed) == members


async def test_dry_run_reads_nothing_more(itm: FakeITM) -> None:
    """A dry run returns what the write's read showed and leaves nothing behind."""
    c = _client(itm, CASES["set_mode"], dry_run=True, verify_timeout=30)
    assert await c.set_mode(UNIT, "heat", confirm=False) is not None
    itm.units[0].mode = "Cool"
    assert await c.apply(UNIT, mode="heat", setpoint=21, confirm=False) is None
    assert _pollings(itm) == 0
    assert not c._unconfirmed
    assert len(c.planned) == 1


async def test_no_answer_while_checking(itm: FakeITM) -> None:
    """No read of the check gets an answer: NotConfirmed, nothing written."""

    class Unanswered(FakeClient):
        """Every read of the unit after the write's own gets no answer."""

        async def fetch_unit(self, name: str) -> Unit:
            raise aiohttp.ClientConnectionError(name)

    c = Unanswered(itm, confirm_interval=0.001, verify_timeout=0.05)
    with pytest.raises(NotConfirmed) as err:
        await c.set_mode(UNIT, "heat", confirm=False)
    assert err.value.last_seen is None
    assert isinstance(err.value.__cause__, aiohttp.ClientConnectionError)
    assert _writes(itm) == 0


async def test_window_at_most_confirm_timeout(itm: FakeITM) -> None:
    """verify_timeout is cut to confirm_timeout."""
    c = _client(itm, CASES["set_mode"], confirm_timeout=0.1, verify_timeout=60)
    start = time.monotonic()
    assert await c.set_mode(UNIT, "heat") is not None
    assert 0.1 <= time.monotonic() - start < 5


@pytest.mark.parametrize("confirm", [True, False])
async def test_cancelled_while_checking(itm: FakeITM, *, confirm: bool) -> None:
    """A cancellation during the check goes on; nothing is left behind."""
    c = _client(itm, CASES["set_unit_settings"], confirm_timeout=30, verify_timeout=30)
    task = asyncio.create_task(
        c.set_unit_settings(UNIT, setpoint_lock=RemoteLock.ALLOWED, confirm=confirm)
    )
    await _wait(lambda: _pollings(itm) >= 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert itm.session is None
    assert not c._unconfirmed
    assert not c._unit_lock(UNIT).locked()
    assert _writes(itm) == 0


async def test_zone_write_after_all_needs_budget(itm: FakeITM) -> None:
    """A write sent after all counts against the budget like any other."""
    case = CASES["set_zone_setpoint"]
    c = _client(itm, case, max_writes_per_unit_per_hour=1)
    assert await c.set_zone_setpoint(ZONE, 22) is not None
    assert _writes(itm) == 1
    itm.change_elsewhere(itm.units[0], 4, setpoint=23)
    with pytest.raises(WriteBudgetExceeded):
        await c.set_zone_setpoint(ZONE, 22, confirm=False)
    assert _writes(itm) == 1


@pytest.mark.parametrize("confirm", [True, False])
async def test_in_a_kept_session(itm: FakeITM, *, confirm: bool) -> None:
    """The check and the write after all use the kept login."""
    c = _client(itm, CASES["set_unit_settings"])
    itm.change_elsewhere(itm.units[0], 4, lock_setpoint=2)
    async with c.kept_session(keep_alive=None):
        result = await c.set_unit_settings(
            UNIT, setpoint_lock=RemoteLock.ALLOWED, confirm=confirm
        )
    assert result is not None
    assert result.remote_locks.setpoint == RemoteLock.ALLOWED
    assert itm.units[0].lock_setpoint == 0
    assert itm.changed_elsewhere
    assert _first_write(itm) > itm.changed_elsewhere[0]
    assert itm.logins == 1


# -- partial skips -------------------------------------------------------------------


@dataclass(frozen=True)
class Partial:
    """A write of which the unit shows one part already, and another client's change."""

    setup: dict[str, Any]
    call: Write
    elsewhere: dict[str, Any]
    ours: Callable[[FakeUnit], bool]
    left_out: int


PARTIALS = {
    "apply mode and setpoint": Partial(
        {"mode": "Cool"},
        lambda c, *, confirm: c.apply(UNIT, mode="heat", setpoint=21, confirm=confirm),
        {"setpoint": 23},
        lambda u: u.mode == "Heat" and u.setpoint == 21,
        D_SETPOINT_CHECK,
    ),
    "apply power and mode": Partial(
        {"running": False},
        lambda c, *, confirm: c.apply(UNIT, power=True, mode="heat", confirm=confirm),
        {"mode": "Cool"},
        lambda u: u.running and u.mode == "Heat",
        D_MODE_CHECK,
    ),
    "set_unit_settings two locks": Partial(
        {},
        lambda c, *, confirm: c.set_unit_settings(
            UNIT,
            start_stop_lock=RemoteLock.FORBIDDEN,
            setpoint_lock=RemoteLock.ALLOWED,
            confirm=confirm,
        ),
        {"lock_setpoint": 2},
        lambda u: u.lock_start_stop == 2 and u.lock_setpoint == 0,
        D_LOCK_SETPOINT_CHECK,
    ),
}


def _partial(itm: FakeITM, case: Partial) -> FakeUnit:
    target = itm.units[0]
    for key, value in case.setup.items():
        setattr(target, key, value)
    return target


@pytest.mark.parametrize("confirm", [True, False])
@pytest.mark.parametrize("name", sorted(PARTIALS))
async def test_partial_skip_is_overruled(
    itm: FakeITM, name: str, *, confirm: bool
) -> None:
    """The part shown already moves away after the write: written after all, once.

    The write after all sends only that part.
    """
    case = PARTIALS[name]
    c = _client(itm, CASES["set_mode"])
    target = _partial(itm, case)
    itm.change_elsewhere(target, 4, **case.elsewhere)
    assert await case.call(c, confirm=confirm) is not None
    assert itm.changed_elsewhere
    writes = _write_indexes(itm)
    assert len(writes) == 2
    assert writes[1] > itm.changed_elsewhere[0]
    assert _ticks(itm, writes[0], writes[1]) == {case.left_out}
    assert case.ours(target)
    assert c.writes_sent[UNIT] == 2
    assert not c._unconfirmed


@pytest.mark.parametrize("confirm", [True, False])
@pytest.mark.parametrize("name", sorted(PARTIALS))
async def test_partial_skip_still_overruled(
    itm: FakeITM, name: str, *, confirm: bool
) -> None:
    """The other change keeps winning: NotConfirmed after one write after all."""
    case = PARTIALS[name]
    c = _client(itm, CASES["set_mode"], confirm_timeout=0.3, verify_timeout=0.3)
    target = _partial(itm, case)
    itm.change_elsewhere(target, 2, **case.elsewhere)
    target.ignores_writes = True
    with pytest.raises(NotConfirmed):
        await case.call(c, confirm=confirm)
    assert _writes(itm) == 2
    assert _first_write(itm) < itm.changed_elsewhere[0]
    assert not c._unconfirmed


@pytest.mark.parametrize("name", sorted(PARTIALS))
async def test_partial_skip_nothing_comes_in(itm: FakeITM, name: str) -> None:
    """Nothing moves: one write, watched until the window ends, then confirmed."""
    case = PARTIALS[name]
    c = _client(itm, CASES["set_mode"], verify_timeout=0.2)
    target = _partial(itm, case)
    start = time.monotonic()
    assert await case.call(c, confirm=True) is not None
    assert time.monotonic() - start >= 0.2
    assert _writes(itm) == 1
    assert case.ours(target)


@pytest.mark.parametrize("name", sorted(PARTIALS))
async def test_partial_skip_without_confirm_waits_the_window(
    itm: FakeITM, name: str
) -> None:
    """confirm=False: the window is waited; what was sent is left to the next write."""
    case = PARTIALS[name]
    c = _client(itm, CASES["set_mode"], verify_timeout=0.2)
    target = _partial(itm, case)
    start = time.monotonic()
    assert await case.call(c, confirm=False) is None
    assert time.monotonic() - start >= 0.2
    assert _writes(itm) == 1
    assert set(c._unconfirmed) == {UNIT}
    await c.set_unit_settings(UNIT, mode_lock=RemoteLock.FORBIDDEN)
    assert not c._unconfirmed
    assert case.ours(target)


async def test_partial_cancelled_without_confirm_keeps_the_sent_check(
    itm: FakeITM,
) -> None:
    """Cancelled in the window: the part sent stays for the next write to check."""
    c = _client(itm, CASES["set_mode"], confirm_timeout=30, verify_timeout=30)
    _partial(itm, PARTIALS["apply mode and setpoint"])
    task = asyncio.create_task(c.apply(UNIT, mode="heat", setpoint=21, confirm=False))
    await _wait(lambda: _writes(itm) == 1 and UNIT in c._unconfirmed)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert set(c._unconfirmed) == {UNIT}
    assert not c._unit_lock(UNIT).locked()


async def test_full_write_is_not_watched(itm: FakeITM) -> None:
    """Every part sent: one write, confirmed, no verify window."""
    c = _client(itm, CASES["set_mode"], verify_timeout=30, confirm_timeout=30)
    start = time.monotonic()
    result = await c.apply(UNIT, mode="cool", setpoint=22)
    assert time.monotonic() - start < 5
    assert isinstance(result, Unit)
    assert _writes(itm) == 1


# -- zones: written per member -------------------------------------------------------


def _zone_client(itm: FakeITM, **kw: Any) -> FakeClient:
    return _client(itm, CASES["apply_zone"], **kw)


@pytest.mark.parametrize("confirm", [True, False])
async def test_zone_writes_only_the_members_that_differ(
    itm: FakeITM, *, confirm: bool
) -> None:
    """One member has another setpoint: only it is written, the others are checked."""
    other = itm.units[1]
    other.setpoint = 23
    c = _zone_client(itm, verify_timeout=0.2)
    start = time.monotonic()
    result = await c.set_zone_setpoint(ZONE, 21, confirm=confirm)
    assert time.monotonic() - start >= 0.2
    assert _writes(itm) == 1
    assert dict(c.writes_sent) == {other.name: 1}
    assert all(u.setpoint == 21 for u in itm.units)
    if confirm:
        assert result is not None
        assert not c._unconfirmed
    else:
        assert result is None
        assert set(c._unconfirmed) == {other.name}


@pytest.mark.parametrize("confirm", [True, False])
async def test_zone_partial_skip_is_overruled(itm: FakeITM, *, confirm: bool) -> None:
    """A member left out moves away after the write: it is written after all, once."""
    first, other = itm.units[0], itm.units[1]
    other.setpoint = 23
    c = _zone_client(itm)
    itm.change_elsewhere(first, 6, setpoint=24)
    assert await c.set_zone_setpoint(ZONE, 21, confirm=confirm) is not None
    writes = _write_indexes(itm)
    assert len(writes) == 2
    assert writes[0] < itm.changed_elsewhere[0] < writes[1]
    assert dict(c.writes_sent) == {first.name: 1, other.name: 1}
    assert all(u.setpoint == 21 for u in itm.units)
    assert not c._unconfirmed


@pytest.mark.parametrize("confirm", [True, False])
async def test_zone_partial_skip_still_overruled(
    itm: FakeITM, *, confirm: bool
) -> None:
    """The other change keeps winning on a member left out: NotConfirmed, no loop."""
    first, other = itm.units[0], itm.units[1]
    other.setpoint = 23
    c = _zone_client(itm, confirm_timeout=0.3, verify_timeout=0.3)
    itm.change_elsewhere(first, 6, setpoint=24)
    first.ignores_writes = True
    with pytest.raises(NotConfirmed):
        await c.set_zone_setpoint(ZONE, 21, confirm=confirm)
    assert _writes(itm) == 2
    assert first.setpoint == 24


@pytest.mark.parametrize("differs", [False, True])
async def test_zone_member_missing_from_a_read_is_not_written(
    itm: FakeITM, *, differs: bool
) -> None:
    """A check's read that misses a member left out writes nothing more to the zone."""
    reads = 0

    class Missing(FakeClient):
        """The check's first read of the zone misses its first member."""

        async def fetch_zone_units(self, zone: str) -> dict[str, Unit]:
            nonlocal reads
            reads += 1
            units = await super().fetch_zone_units(zone)
            if reads == 1:
                del units[itm.units[0].name]
            return units

    if differs:
        itm.units[1].mode = "Cool"
    c = Missing(
        itm,
        confirm_interval=0.001,
        verify_timeout=0.1,
        zone_path=("Building",),
    )
    assert await c.apply_zone(ZONE, mode="heat") is not None
    assert reads > 1
    assert _writes(itm) == int(differs)
    assert dict(c.writes_sent) == ({itm.units[1].name: 1} if differs else {})
    assert all(u.mode == "Heat" for u in itm.units)


async def test_zone_members_get_only_what_they_lack(itm: FakeITM) -> None:
    """apply_zone: each member's dialog ticks only what that member lacks."""
    itm.units[0].mode = "Cool"
    itm.units[1].fan_icon = 3013
    c = _zone_client(itm, verify_timeout=0.05)
    assert await c.apply_zone(ZONE, mode="heat", fan_speed="medium") is not None
    writes = _write_indexes(itm)
    assert len(writes) == 2
    assert _ticks(itm, 0, writes[0]) == {D_MODE_CHECK}
    assert _ticks(itm, writes[0], writes[1]) == {D_FAN_CHECK}
    assert dict(c.writes_sent) == {itm.units[0].name: 1, itm.units[1].name: 1}
    assert all(u.mode == "Heat" and u.fan_icon == 3011 for u in itm.units)


async def test_zone_lacking_the_same_is_written_whole(itm: FakeITM) -> None:
    """Every member lacks the same: one write through the zone's panel."""
    c = _zone_client(itm)
    assert await c.apply_zone(ZONE, power=True, mode="cool") is not None
    writes = _write_indexes(itm)
    assert len(writes) == 1
    assert _ticks(itm, 0, writes[0]) == {D_MODE_CHECK}
    assert dict(c.writes_sent) == dict.fromkeys((u.name for u in itm.units), 1)


async def test_zone_with_other_points_is_written_whole(itm: FakeITM) -> None:
    """A zone holding more than indoor units goes through its panel, as asked."""
    building = itm.root.children[0]
    building.children.append(FakeZone("Mixed", [*itm.units[:2], *itm.ventilation]))
    itm.units[1].running = False
    c = _zone_client(itm)
    assert await c.set_zone_power_state("Mixed", PowerState.ON) is not None
    assert _writes(itm) == 1
    names = [itm.units[0].name, itm.units[1].name, itm.ventilation[0].name]
    assert dict(c.writes_sent) == dict.fromkeys(names, 1)


async def test_zone_budget_counts_only_members_written(itm: FakeITM) -> None:
    """A member left out spends none of its budget."""
    itm.units[1].setpoint = 23
    c = _zone_client(itm, max_writes_per_unit_per_hour=1, verify_timeout=0.05)
    assert await c.set_zone_setpoint(ZONE, 21) is not None
    assert c.recent_writes() == {itm.units[1].name: 1}
    c.zone_path = ("Building", ZONE)
    assert await c.set_setpoint(UNIT, 22) is not None


@pytest.mark.parametrize("name", sorted(PARTIALS))
async def test_write_after_all_sends_only_what_was_left_out(
    itm: FakeITM, name: str
) -> None:
    """The part sent is still on its way when the other change lands: not resent."""
    case = PARTIALS[name]
    itm.latency_logins = 8
    c = _client(itm, CASES["set_mode"])
    target = _partial(itm, case)
    itm.change_elsewhere(target, 2, **case.elsewhere)
    assert await case.call(c, confirm=True) is not None
    writes = _write_indexes(itm)
    assert len(writes) == 2
    assert writes[0] < itm.changed_elsewhere[0] < writes[1]
    assert _ticks(itm, writes[0], writes[1]) == {case.left_out}
    assert case.ours(target)


async def test_zone_write_after_all_writes_only_units_left_out(itm: FakeITM) -> None:
    """A unit written is still on its way when one left out moves: not rewritten."""
    first, other = itm.units[0], itm.units[1]
    other.setpoint = 23
    itm.latency_logins = 8
    c = _zone_client(itm)
    itm.change_elsewhere(first, 5, setpoint=24)
    assert await c.set_zone_setpoint(ZONE, 21) is not None
    writes = _write_indexes(itm)
    assert len(writes) == 2
    assert writes[0] < itm.changed_elsewhere[0] < writes[1]
    assert dict(c.writes_sent) == {first.name: 1, other.name: 1}
    assert all(u.setpoint == 21 for u in itm.units)
