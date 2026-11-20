# pylint: disable=protected-access,redefined-outer-name
"""A failed clean-up never replaces a call's error or its cancellation."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiohttp
import pytest

from daikin_itm import (
    EventSpec,
    ItmClient,
    ItmCommandError,
    MemorySessionStore,
    TempLimit,
    WriteOutcomeUnknown,
)
from daikin_itm._util import cleanup
from daikin_itm.protocol import Reply, ScreenUpdate
from daikin_itm.session import _Session
from daikin_itm.zones_edit import (
    MOVE,
    MV_CANCEL,
    ZE_CLOSE,
    ZONE_ERROR,
    move_zone,
    set_zone_members,
)

from .fake_dealer_functions import FakeDealerITM, Group, Interlock
from .fake_demand import FakeDemandITM
from .fake_emergency import FakeEmergencyITM, Program
from .fake_energy_edit import FakeEnergyEditITM
from .fake_itm import FakeGroup, FakeITM
from .fake_schedule import FakeScheduleITM
from .fake_service_edit import FakeServiceEditITM
from .fake_system_edit import FakeSystemEditITM
from .fake_users import FakeUsersITM
from .fake_zones import FakeZonesITM
from .test_client import FakeClient, is_logout

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

EDITOR, CONFIRM_YES = 104, 25
DISCARD = 31
MENU_LIST, PASSWORD_ERROR = 5, 94


class StepError(Exception):
    """The error of a block or a step."""


def _write_error() -> StepError:
    return StepError("write")


def _step(done: list[str], name: str, error: BaseException | None = None) -> Any:
    async def run() -> None:
        done.append(name)
        if error is not None:
            raise error

    return run


# -- the helper ------------------------------------------------------------------------


async def test_steps_run_in_order_after_the_block() -> None:
    """Every step runs after a block that returned, in order."""
    done: list[str] = []
    async with cleanup(_step(done, "a"), _step(done, "b")):
        done.append("block")
    assert done == ["block", "a", "b"]


async def test_failed_step_after_success_raises() -> None:
    """After a block that returned, a failed step raises and ends the clean-up."""
    done: list[str] = []
    with pytest.raises(StepError, match="close"):
        async with cleanup(_step(done, "a", StepError("close")), _step(done, "b")):
            done.append("block")
    assert done == ["block", "a"]


async def test_error_goes_on_with_the_failed_step_as_note(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The block's error goes on; the step's error is logged and noted on it."""
    done: list[str] = []
    with (
        caplog.at_level(logging.WARNING),
        pytest.raises(StepError, match="write") as caught,
    ):
        async with cleanup(_step(done, "a", StepError("close")), _step(done, "b")):
            raise _write_error()
    assert done == ["a"]
    assert caught.value.__notes__ == ["clean-up failed as well: StepError('close')"]
    assert "clean-up failed while StepError('write') went on" in caplog.text


async def test_steps_run_after_an_error() -> None:
    """The steps run after a block that raised, and its error goes on unchanged."""
    done: list[str] = []
    with pytest.raises(StepError, match="write") as caught:
        async with cleanup(_step(done, "a"), _step(done, "b")):
            raise _write_error()
    assert done == ["a", "b"]
    assert not hasattr(caught.value, "__notes__")


async def test_cancellation_stays_a_cancellation() -> None:
    """A cancelled block stays cancelled when a step fails after it."""
    done: list[str] = []
    started = asyncio.Event()

    async def block() -> None:
        async with cleanup(_step(done, "a", StepError("close"))):
            started.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(block())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert done == ["a"]
    assert caught.value.__notes__ == ["clean-up failed as well: StepError('close')"]


async def test_cancelled_step_raises_the_cancellation() -> None:
    """A step cancelled after the block's error raises its cancellation."""
    done: list[str] = []
    with pytest.raises(asyncio.CancelledError):
        async with cleanup(_step(done, "a", asyncio.CancelledError())):
            raise _write_error()
    assert done == ["a"]


# -- a schedule editor that doesn't close --------------------------------------------


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM whose schedule editor stays open when left."""
    fake = FakeITM()
    fake.editor_stays_open = True
    return fake


def _is_confirm(body: str) -> bool:
    """Tell whether a request body answers the editor's "apply settings?" with Yes."""
    return body[36:48] == f"{EDITOR:04x}{CONFIRM_YES:04x}0001"


async def test_cancelled_during_the_confirm(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C while the write is confirmed: cancelled, logged out, lock free."""
    c = FakeClient(itm)
    orig = c._post
    confirming = asyncio.Event()

    async def post(body: str) -> str:
        reply = await orig(body)
        if _is_confirm(body) and not confirming.is_set():
            confirming.set()
            await asyncio.sleep(0.05)
        return reply

    monkeypatch.setattr(c, "_post", post)
    task = asyncio.create_task(c.set_schedule_enabled("Program E", enabled=True))
    await confirming.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert "the schedule editor did not close" in caught.value.__notes__[0]
    assert itm.session is None
    assert not c._lock.locked()
    assert c._writing is None
    itm.editor_stays_open = False
    assert len(await c.fetch_schedules()) == len(itm.schedules)


async def test_error_during_the_confirm(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A lost confirm comes out as an unknown outcome; the editor is left as is."""
    c = FakeClient(itm)
    orig = c._post

    async def post(body: str) -> str:
        if _is_confirm(body):
            msg = "lost"
            raise aiohttp.ClientConnectionError(msg)
        return await orig(body)

    monkeypatch.setattr(c, "_post", post)
    with pytest.raises(aiohttp.ClientConnectionError) as caught:
        await c.set_schedule_enabled("Program E", enabled=True)
    assert isinstance(caught.value, WriteOutcomeUnknown)
    assert "lost" in str(caught.value.__cause__)
    assert "clean-up skipped" in caught.value.__notes__[0]
    assert itm.session is None
    assert not c._lock.locked()


async def test_success_and_failed_close(itm: FakeITM) -> None:
    """A call that went well raises the clean-up's error, as before."""
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="the schedule editor did not close"):
        await c.set_schedule_enabled("Always", enabled=True)
    assert itm.session is None
    assert not c._lock.locked()


async def test_program_read_keeps_its_error(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A program read that fails in the editor keeps its error."""
    c = FakeClient(itm)

    def unreadable(*_: Any) -> None:
        msg = "unreadable editor"
        raise ItmCommandError(msg)

    monkeypatch.setattr("daikin_itm.schedule_edit.program_from_editor", unreadable)
    with pytest.raises(ItmCommandError, match="unreadable editor") as caught:
        await c.fetch_schedule("Always")
    assert "the schedule editor did not close" in caught.value.__notes__[0]
    assert itm.session is None


async def test_failed_logout_keeps_the_error(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A logout that fails after an error keeps the error and the token."""
    store = MemorySessionStore()
    c = FakeClient(itm, store=store)
    orig = c._post

    async def post(body: str) -> str:
        if is_logout(body):
            msg = "gone"
            raise aiohttp.ClientConnectionError(msg)
        return await orig(body)

    monkeypatch.setattr(c, "_post", post)
    with pytest.raises(ItmCommandError, match="no such unit") as caught:
        await c.fetch_unit("no such unit")
    assert "gone" in caught.value.__notes__[0]
    assert store.token == itm.session
    assert not c._lock.locked()


async def test_cancelled_call_with_a_failed_logout(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C during a read whose logout fails: still a cancellation, lock free."""
    store = MemorySessionStore()
    c = FakeClient(itm, store=store)
    orig = c._post
    reading = asyncio.Event()

    async def post(body: str) -> str:
        if is_logout(body):
            msg = "gone"
            raise aiohttp.ClientConnectionError(msg)
        reply = await orig(body)
        if itm.session is not None and not reading.is_set():
            reading.set()
            await asyncio.sleep(0.05)
        return reply

    monkeypatch.setattr(c, "_post", post)
    task = asyncio.create_task(c.fetch_units())
    await reading.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError) as caught:
        await task
    assert "gone" in caught.value.__notes__[0]
    assert store.token == itm.session
    assert not c._lock.locked()


# -- every group of writers ------------------------------------------------------------


class BrokenClient(FakeClient):
    """A client whose requests fail once the write did, except the logout."""

    broken = False

    async def _post(self, body: str) -> str:
        if self.broken and not is_logout(body):
            msg = "closing failed"
            raise aiohttp.ClientConnectionError(msg)
        return await super()._post(body)


@dataclass
class Writer:
    """A write of one group and the simulated iTM it needs."""

    make: Callable[[], FakeITM]
    call: Callable[[ItmClient, Any], Awaitable[object]]


def _with_changeover() -> FakeITM:
    itm = FakeITM()
    itm.groups = [FakeGroup("Group 01", members=["1:1-04"])]
    return itm


def _with_limits() -> FakeDealerITM:
    itm = FakeDealerITM()
    itm.limits.append(Group("Offices", members=[itm.points[1]]))
    return itm


def _with_interlock() -> FakeDealerITM:
    itm = FakeDealerITM()
    itm.interlocks.append(
        Interlock("Program A", inputs=[itm.points[0]], outputs=[[itm.points[2]], []])
    )
    return itm


def _with_stop_program() -> FakeEmergencyITM:
    itm = FakeEmergencyITM()
    itm.programs.append(Program("Fire alarm", inputs=[itm.points[0]]))
    return itm


def _with_demand() -> FakeDemandITM:
    itm = FakeDemandITM()
    itm.service_password = "service-secret"
    return itm


WRITERS: dict[str, Writer] = {
    "menu": Writer(FakeITM, lambda c, _: c.set_setback(high_up=2)),
    "schedule program": Writer(
        FakeITM, lambda c, _: c.set_schedule_enabled("Program E", enabled=True)
    ),
    "schedule editor": Writer(
        FakeScheduleITM,
        lambda c, _: c.add_schedule_event("Always", 3, EventSpec("07:45", "1:1-09")),
    ),
    "changeover": Writer(
        _with_changeover,
        lambda c, _: c.set_changeover_enabled("Group 01", enabled=True),
    ),
    "dealer functions": Writer(
        _with_limits,
        lambda c, _: c.set_temperature_limit_enabled("Offices", enabled=True),
    ),
    "interlock": Writer(
        _with_interlock, lambda c, _: c.set_interlock_enabled("Program A", enabled=True)
    ),
    "emergency": Writer(
        _with_stop_program,
        lambda c, _: c.set_emergency_stop_enabled(
            "Fire alarm", enabled=True, allow_safety_change=True
        ),
    ),
    "demand": Writer(_with_demand, lambda c, _: c.set_demand_forced_level("Level 2")),
    "energy": Writer(
        FakeEnergyEditITM, lambda c, _: c.set_planned_consumption(ratio=7.5)
    ),
    "service": Writer(
        FakeServiceEditITM,
        lambda c, itm: c.set_leakage_check_indoor_units(
            "test-service", "OU-2", (itm.indoor[1],), allow_service_change=True
        ),
    ),
    "system": Writer(
        FakeSystemEditITM,
        lambda c, itm: c.add_email_address("ops@example.com", [itm.units[0].name]),
    ),
    "users": Writer(FakeUsersITM, lambda c, _: c.set_web_user("viewer", admin=True)),
    "zones": Writer(FakeZonesITM, lambda c, _: c.create_zone("demo group")),
}


@pytest.mark.parametrize("name", sorted(WRITERS))
async def test_failed_write_keeps_its_error(
    name: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A write that fails keeps its error when closing its screens fails too."""
    writer = WRITERS[name]
    itm = writer.make()
    c = BrokenClient(itm)

    async def refused(_: _Session, label: str, *__: Any, **___: Any) -> None:
        c.broken = True
        msg = f"{label}: refused"
        raise ItmCommandError(msg)

    monkeypatch.setattr(_Session, "write", refused)
    with pytest.raises(ItmCommandError, match="refused") as caught:
        await writer.call(c, itm)
    assert "closing failed" in caught.value.__notes__[0]
    assert itm.session is None
    assert not c._lock.locked()


# -- clean-up after an error outside a cleanup block ---------------------------------


async def test_failed_settings_cancel_is_noted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A settings dialog that fails and then can't be cancelled keeps both errors."""
    itm = FakeITM()
    c = BrokenClient(itm)

    async def refused(*_: Any, **__: Any) -> None:
        c.broken = True
        msg = "limit refused"
        raise ItmCommandError(msg)

    monkeypatch.setattr(_Session, "answer_number", refused)
    with pytest.raises(ItmCommandError, match="limit refused") as caught:
        await c.set_unit_settings(
            "1:1-04", cool_limit=TempLimit(enabled=True, minimum=20, maximum=30)
        )
    assert "closing failed" in caught.value.__notes__[0]
    assert itm.session is None


async def test_failed_dialog_cancel_is_noted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A power, mode or fan change that fails keeps its error when Cancel fails."""
    itm = FakeITM()
    c = BrokenClient(itm)

    async def refused(*_: Any, **__: Any) -> None:
        c.broken = True
        msg = "power refused"
        raise ItmCommandError(msg)

    monkeypatch.setattr(_Session, "set_dialog_power", refused)
    unit = (await c.fetch_units())["1:1-04"]
    with pytest.raises(ItmCommandError, match="power refused") as caught:
        await c.apply("1:1-04", power=not unit.power)
    assert "closing failed" in caught.value.__notes__[0]
    assert itm.session is None


async def test_refused_service_password_keeps_its_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The refusal comes out when its error dialog can't be dismissed."""
    itm = FakeITM()
    c = FakeClient(itm)
    send = _Session.send

    async def sending(s: _Session, *a: Any, **kw: Any) -> Any:
        if a[:2] == (MENU_LIST, PASSWORD_ERROR):
            msg = "dismiss failed"
            raise ItmCommandError(msg)
        return await send(s, *a, **kw)

    monkeypatch.setattr(_Session, "send", sending)
    with pytest.raises(ItmCommandError, match="refused the service password") as caught:
        await c.fetch_service_settings("wrong")
    assert "dismiss failed" in caught.value.__notes__[0]
    assert itm.session is None


async def test_zone_error_kept_when_its_screen_does_not_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The iTM's zone error comes out, its screen's failed close as a note."""
    itm = FakeZonesITM()
    itm.zone_error = "Zone limit reached"
    c = FakeClient(itm)
    orig = c._post

    async def post(body: str) -> str:
        if body[36:44] == f"{ZONE_ERROR:04x}{ZE_CLOSE:04x}":
            msg = "lost"
            raise aiohttp.ClientConnectionError(msg)
        return await orig(body)

    monkeypatch.setattr(c, "_post", post)
    with pytest.raises(ItmCommandError, match="Zone limit reached") as caught:
        async with c._session() as s:
            await set_zone_members(s, "Zone A", (itm.units[0].name,))
    assert "lost" in caught.value.__notes__[0]
    assert itm.session is None


async def test_failed_discard_answer_is_noted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A "discard?" answer that fails while a zone move fails is noted, not lost."""
    itm = FakeZonesITM()
    c = FakeClient(itm)
    send = _Session.send
    discard = ScreenUpdate(
        MOVE, "DLG_OPEN", [DISCARD, "CONFIRM", "Discard?", "Confirm"]
    )

    async def sending(s: _Session, *a: Any, **kw: Any) -> Any:
        if a[:2] == (MOVE, DISCARD):
            msg = "discard refused"
            raise ItmCommandError(msg)
        reply = await send(s, *a, **kw)
        if a[:2] == (MOVE, MV_CANCEL):
            return Reply(reply.token, [*reply.screens, discard])
        return reply

    monkeypatch.setattr(_Session, "send", sending)
    with pytest.raises(ItmCommandError, match="can't move below") as caught:
        async with c._session() as s:
            await move_zone(s, "Zone A", "No such zone")
    assert "discard refused" in caught.value.__notes__[0]
