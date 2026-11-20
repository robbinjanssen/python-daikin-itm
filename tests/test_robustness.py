# pylint: disable=protected-access,redefined-outer-name
"""Faults against the simulated iTM: lost replies, hung requests, restarts."""

from __future__ import annotations

import asyncio
import functools
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import aiohttp
import pytest

from daikin_itm import (
    FileSessionStore,
    ItmCommandError,
    ItmError,
    NotConfirmed,
    NotOffered,
    PowerState,
    RemoteLock,
    RequestSkipped,
    SessionEnded,
    SetpointOutOfRange,
    Unit,
    UnitSettings,
    UnitStopped,
    WriteOutcomeUnknown,
)
from daikin_itm import client as client_module
from daikin_itm._util import check_wrong
from daikin_itm.menu import MB_HISTORY, MenuScreen
from daikin_itm.models import D_OK
from daikin_itm.protocol import EventType, ScreenID
from daikin_itm.session import _Session

from .fake_itm import FakeITM, FakeZone
from .test_client import FakeClient, confirmed

if TYPE_CHECKING:
    from collections.abc import Callable

TIMER = 3
INIT = 0
LOGOUT = 10
UPD_PROG = 5
CANCEL_PROG = 6


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM."""
    return FakeITM()


def _from_on(
    match: Callable[[dict[str, Any]], bool],
) -> Callable[[dict[str, Any]], bool]:
    """Return a fault filter hitting the first matching request and all after it."""
    hit = False

    def fault_on(ev: dict[str, Any]) -> bool:
        nonlocal hit
        hit = hit or match(ev)
        return hit

    return fault_on


# -- the simulator's faults ------------------------------------------------------------


async def test_lost_reply_takes_effect(itm: FakeITM) -> None:
    """A lost reply: the request took effect, the client sees the error."""
    c = FakeClient(itm)
    itm.lose_reply_next = TimeoutError()
    itm.fault_on = lambda ev: ev["event"] == INIT
    with pytest.raises(TimeoutError):
        await c.fetch_units()
    assert [r["event"] for r in itm.lost_replies] == [INIT]
    assert itm.lose_reply_next is None
    assert len(await c.fetch_units()) == 3


async def test_hung_request_takes_no_effect(itm: FakeITM) -> None:
    """A hung request never gets an answer and changes nothing."""
    c = FakeClient(itm)
    c.request_timeout = 0.05
    itm.hang_next = 1
    itm.fault_on = lambda ev: ev["event"] == TIMER
    with pytest.raises(TimeoutError):
        await c.set_setpoint("1:1-04", 22)
    assert [r["event"] for r in itm.hung] == [TIMER]
    assert itm.units[1].setpoint == 23
    assert itm.hang_next == 0


async def test_restart_forgets_the_session(itm: FakeITM) -> None:
    """After a restart the iTM ends the session it knew; a new login works."""
    c = FakeClient(itm)
    async with c._session() as s:
        itm.restart()
        with pytest.raises(SessionEnded):
            await s.poll()
    assert itm.restarts == 1
    assert len(await c.fetch_units()) == 3


async def test_restart_during_a_kept_session(itm: FakeITM) -> None:
    """A kept session after a restart logs in again on the next call."""
    c = FakeClient(itm)
    async with c.kept_session(keep_alive=None):
        await c.fetch_units()
        itm.restart()
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 2
    assert itm.session is None


# -- R8: the INIT handshake ------------------------------------------------------------


async def test_init_sent_again_after_a_broken_connection(itm: FakeITM) -> None:
    """A connection the iTM closed costs no failed call: INIT is sent once more."""
    itm.fail_next = aiohttp.ServerDisconnectedError()
    assert len(await FakeClient(itm).fetch_units()) == 3
    assert itm.logins == 1


async def test_init_not_sent_a_third_time(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two broken connections in a row: the call fails."""
    handle = itm.handle
    failures = 0

    def flaky(body: str) -> str:
        nonlocal failures
        if failures < 2:
            failures += 1
            raise aiohttp.ServerDisconnectedError
        return handle(body)

    monkeypatch.setattr(itm, "handle", flaky)
    with pytest.raises(aiohttp.ServerDisconnectedError):
        await FakeClient(itm).fetch_units()
    assert failures == 2
    assert itm.logins == 0


async def test_init_timeout_not_retried(itm: FakeITM) -> None:
    """A timeout is not retried: a hung iTM costs one timeout."""
    c = FakeClient(itm)
    c.request_timeout = 0.05
    itm.hang_next = 2
    with pytest.raises(TimeoutError):
        await c.fetch_units()
    assert len(itm.hung) == 1
    assert itm.hang_next == 1


# -- R6: a hung iTM --------------------------------------------------------------------


async def test_hung_itm_costs_one_timeout(itm: FakeITM) -> None:
    """After a request got no answer only the logout is sent, with a short timeout."""
    c = FakeClient(itm)
    c.request_timeout = 0.3
    c.logout_timeout = 0.02
    itm.hang_next = 1000
    itm.fault_on = _from_on(lambda ev: ev["screen"] == MenuScreen.MENULIST)
    loop = asyncio.get_running_loop()
    start = loop.time()
    with pytest.raises(TimeoutError) as caught:
        await c.fetch_setback()
    assert loop.time() - start < 0.5
    assert [r["event"] for r in itm.hung][1:] == [LOGOUT]
    assert any("clean-up skipped" in n for n in caught.value.__notes__)
    assert await c._store.load() == itm.session
    assert not c._lock.locked()
    itm.hang_next = 0
    assert len(await c.fetch_units()) == 3
    assert itm.logins == 2


async def test_hung_itm_in_a_write_block(itm: FakeITM) -> None:
    """A write whose checks hang: each logs out once, the lock is let go at the end."""
    c = FakeClient(itm, confirm_timeout=0.3)
    c.request_timeout = 0.1
    c.logout_timeout = 0.02
    itm.hang_next = 1000
    itm.fault_on = _from_on(lambda ev: ev["event"] == EventType.POLLING)
    with pytest.raises(NotConfirmed, match="no read succeeded") as caught:
        await c.set_setpoint("1:1-04", 22)
    assert isinstance(caught.value, TimeoutError)
    assert isinstance(caught.value.__cause__, TimeoutError)
    assert caught.value.last_seen is None
    hung = [r["event"] for r in itm.hung]
    assert hung[:2] == [EventType.POLLING, LOGOUT]
    assert set(hung[2:]) <= {LOGOUT}
    assert c._writing is None
    assert not c._lock.locked()


async def test_skipped_request_raises(itm: FakeITM) -> None:
    """A session whose request got no answer sends nothing but a logout."""
    c = FakeClient(itm)
    c.request_timeout = 0.05
    async with c._session() as s:
        itm.hang_next = 1
        with pytest.raises(TimeoutError):
            await s.poll()
        sent = len(itm.requests)
        with pytest.raises(RequestSkipped):
            await s.poll()
        assert len(itm.requests) == sent
    assert itm.requests[-1]["event"] == LOGOUT
    assert itm.session is None


async def test_http_error_status_is_an_answer(itm: FakeITM) -> None:
    """An HTTP error status is an answer: the clean-up is still sent."""
    c = FakeClient(itm)
    async with c._session() as s:
        itm.fail_next = aiohttp.ClientResponseError(MagicMock(), (), status=500)
        with pytest.raises(aiohttp.ClientResponseError):
            await s.poll()
        assert not s.unanswered
        await s.poll()


async def test_cancelled_request_without_answer(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled call whose request got no answer logs out with a short timeout."""
    c = FakeClient(itm)
    c.request_timeout = 0.1
    c.logout_timeout = 0.02
    seen: list[bool] = []
    original = _Session.logout

    async def logout(self: _Session) -> None:
        seen.append(self.unanswered)
        await original(self)

    monkeypatch.setattr(_Session, "logout", logout)
    itm.hang_next = 1000
    itm.fault_on = _from_on(lambda ev: ev["screen"] == ScreenID.STDICON)
    task = asyncio.create_task(c.fetch_units())
    async with asyncio.timeout(1):
        while not itm.hung:  # noqa: ASYNC110 - polls the simulator's request log
            await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert seen == [True]
    assert not c._lock.locked()


# -- R5: a write whose reply was lost --------------------------------------------------


@pytest.mark.parametrize(
    "error", [TimeoutError(), aiohttp.ServerDisconnectedError()], ids=type
)
async def test_lost_commit_is_an_unknown_outcome(
    itm: FakeITM, error: Exception
) -> None:
    """The commit's reply is lost: the outcome is unknown, the next write settles."""
    c = FakeClient(itm)
    itm.lose_reply_next = error
    itm.fault_on = lambda ev: ev["event"] == TIMER
    with pytest.raises(WriteOutcomeUnknown) as caught:
        await c.set_setpoint("1:1-04", 22)
    assert isinstance(caught.value, type(error))
    assert isinstance(caught.value, ItmCommandError)
    assert caught.value.__cause__ is error
    assert "unknown" in str(caught.value)
    assert any("1:1-04" in n for n in caught.value.__notes__)
    assert itm.units[1].setpoint == 22
    assert "1:1-04" in c._unconfirmed
    assert itm.session is None
    settled: list[str] = []
    settle = c._settle

    async def spy(unit: str) -> None:
        settled.append(unit)
        if unit in c._unconfirmed:
            settled.append("checked")
        await settle(unit)

    c._settle = spy  # type: ignore[method-assign]
    assert confirmed(await c.set_setpoint("1:1-04", 21)).setpoint == 21
    assert settled == ["1:1-04", "checked"]
    assert not c._unconfirmed


async def test_lost_settings_ok_is_an_unknown_outcome(itm: FakeITM) -> None:
    """The settings dialog's OK lost its reply: unknown, checked before the next one."""
    c = FakeClient(itm)
    itm.lose_reply_next = aiohttp.ServerDisconnectedError()
    itm.fault_on = lambda ev: ev["screen"] == ScreenID.SETTINGS and ev["part"] == D_OK
    with pytest.raises(aiohttp.ServerDisconnectedError) as caught:
        await c.apply("1:1-04", mode="heat")
    assert isinstance(caught.value, WriteOutcomeUnknown)
    assert "1:1-04" in c._unconfirmed


async def test_lost_unit_settings_ok(itm: FakeITM) -> None:
    """set_unit_settings with a lost Yes: its check runs before the next write."""
    c = FakeClient(itm)
    name = itm.units[0].name
    after_ok = False

    def the_yes(ev: dict[str, Any]) -> bool:
        nonlocal after_ok
        hit = after_ok
        after_ok = ev["screen"] == ScreenID.SETTINGS and ev["part"] == D_OK
        return hit

    itm.lose_reply_next = TimeoutError()
    itm.fault_on = the_yes
    with pytest.raises(WriteOutcomeUnknown):
        await c.set_unit_settings(name, overtime=True)
    later = c._unconfirmed[name]
    assert isinstance(later, functools.partial)
    assert getattr(later.func, "__name__", None) == "_confirm_settings"
    assert isinstance(await later(), UnitSettings)


async def test_lost_zone_commit(itm: FakeITM) -> None:
    """A zone write with a lost commit: every member waits for it."""
    for u in itm.units:
        u.running = True
    c = FakeClient(itm, zone_path=("Building",))
    itm.lose_reply_next = TimeoutError()
    itm.fault_on = lambda ev: ev["event"] == TIMER
    with pytest.raises(WriteOutcomeUnknown):
        await c.set_zone_setpoint("Indoor units", 22)
    checks = {c._unconfirmed[u.name] for u in itm.units[:3]}
    assert len(checks) == 1


async def test_lost_menu_write(itm: FakeITM, monkeypatch: pytest.MonkeyPatch) -> None:
    """A menu write whose OK lost its reply reports an unknown outcome."""
    original = _Session.write

    async def write(self: _Session, *args: Any, **kwargs: Any) -> Any:
        itm.lose_reply_next = TimeoutError()
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(_Session, "write", write)
    with pytest.raises(WriteOutcomeUnknown) as caught:
        await FakeClient(itm).set_overtime_duration(60)
    assert isinstance(caught.value, TimeoutError)


async def test_lost_reply_before_the_change(itm: FakeITM) -> None:
    """A reply lost before the change: the error as it was, nothing to check."""
    c = FakeClient(itm)
    itm.lose_reply_next = TimeoutError()
    itm.fault_on = lambda ev: ev["screen"] == ScreenID.STDICON
    with pytest.raises(TimeoutError) as caught:
        await c.set_setpoint("1:1-04", 22)
    assert not isinstance(caught.value, WriteOutcomeUnknown)
    assert not c._unconfirmed
    assert itm.units[1].setpoint == 23


async def test_change_never_sent(itm: FakeITM, monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection that couldn't be made: the change wasn't sent, nothing unknown."""
    c = FakeClient(itm)
    post = c._post
    refused = aiohttp.ClientConnectorError(MagicMock(), OSError(61, "refused"))

    async def refuse_commit(body: str) -> str:
        if FakeITM._parse(body)["event"] == TIMER:
            raise refused
        return await post(body)

    monkeypatch.setattr(c, "_post", refuse_commit)
    with pytest.raises(aiohttp.ClientConnectorError) as caught:
        await c.set_setpoint("1:1-04", 22)
    assert caught.value is refused
    assert not c._unconfirmed


async def test_lost_logout_after_a_change(itm: FakeITM) -> None:
    """The logout after a sent change lost its reply: unknown, settled next time."""
    c = FakeClient(itm, confirm_in_session=False)
    itm.lose_reply_next = TimeoutError()
    itm.fault_on = lambda ev: ev["event"] == LOGOUT
    with pytest.raises(WriteOutcomeUnknown) as caught:
        await c.set_setpoint("1:1-04", 22, confirm=False)
    assert isinstance(caught.value, TimeoutError)
    assert [r["event"] for r in itm.lost_replies] == [LOGOUT]
    assert "1:1-04" in c._unconfirmed
    assert str(c.session_info.last_error).startswith("WriteOutcomeUnknown")


async def test_cancelled_after_the_change(itm: FakeITM) -> None:
    """A call cancelled once its change was sent: the next write settles first."""
    c = FakeClient(itm)
    c.request_timeout = 0.1
    c.logout_timeout = 0.02
    itm.hang_next = 1
    itm.fault_on = lambda ev: ev["event"] == TIMER
    task = asyncio.create_task(c.set_setpoint("1:1-04", 22))
    async with asyncio.timeout(1):
        while not itm.hung:  # noqa: ASYNC110 - polls the simulator's request log
            await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert "1:1-04" in c._unconfirmed
    assert not c._lock.locked()


async def test_cancelled_zone_write_after_the_change(itm: FakeITM) -> None:
    """A zone write cancelled once its change was sent: every member settles first."""
    for u in itm.units:
        u.running = True
    c = FakeClient(itm, zone_path=("Building",))
    c.request_timeout = 0.1
    c.logout_timeout = 0.02
    itm.hang_next = 1
    itm.fault_on = lambda ev: ev["event"] == TIMER
    task = asyncio.create_task(c.set_zone_setpoint("Indoor units", 22))
    async with asyncio.timeout(1):
        while not itm.hung:  # noqa: ASYNC110 - polls the simulator's request log
            await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert {u.name for u in itm.units[:3]} <= c._unconfirmed.keys()


async def test_cancelled_before_the_change(itm: FakeITM) -> None:
    """A call cancelled before its change was sent leaves nothing to settle."""
    c = FakeClient(itm)
    c.request_timeout = 0.1
    c.logout_timeout = 0.02
    itm.hang_next = 1
    itm.fault_on = lambda ev: ev["screen"] == ScreenID.STDICON
    task = asyncio.create_task(c.set_setpoint("1:1-04", 22))
    async with asyncio.timeout(1):
        while not itm.hung:  # noqa: ASYNC110 - polls the simulator's request log
            await asyncio.sleep(0.005)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not c._unconfirmed


def test_outcome_unknown_keeps_the_cause_attributes() -> None:
    """An HTTP error's status and an OSError's errno stay readable."""
    status = WriteOutcomeUnknown.after(
        "lost", aiohttp.ClientResponseError(MagicMock(), (), status=500)
    )
    assert isinstance(status, aiohttp.ClientResponseError)
    assert status.status == 500
    assert repr(status)
    reset = WriteOutcomeUnknown.after("lost", aiohttp.ClientOSError(104, "reset"))
    assert isinstance(reset, OSError)
    assert reset.errno == 104
    assert str(reset) == "lost"


def test_outcome_unknown_keeps_the_type_and_notes() -> None:
    """The error is of the cause's type too, once per type, with its notes."""
    cause = ItmError("x")
    cause.add_note("a note")
    err = WriteOutcomeUnknown.after("lost", cause)
    assert isinstance(err, ItmError)
    assert err.__notes__ == ["a note"]
    again = WriteOutcomeUnknown.after("lost", err)
    assert type(again) is type(err)
    assert type(WriteOutcomeUnknown.after("x", TimeoutError())) is type(
        WriteOutcomeUnknown.after("y", TimeoutError())
    )
    assert str(WriteOutcomeUnknown()) == ""


# -- R10: the progress wait ------------------------------------------------------------


async def test_progress_waits_by_time(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A list that takes more progress requests than the old limit still loads."""
    handle = itm.handle
    slow = 25

    def slow_load(body: str) -> str:
        nonlocal slow
        if FakeITM._parse(body)["event"] == UPD_PROG and slow:
            slow -= 1
            itm.history_loading = True
        return handle(body)

    monkeypatch.setattr(itm, "handle", slow_load)
    c = FakeClient(itm)
    c.progress_interval = 0
    assert (await c.fetch_history())[0].entries
    assert sum(r["event"] == UPD_PROG for r in itm.requests) > 20
    assert itm.history_cancelled == 0


async def test_history_load_cancelled_after_its_time(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A history that never loads: cancelled (a read), then an error."""
    handle = itm.handle

    def never_done(body: str) -> str:
        reply = handle(body)
        itm.history_loading = True
        return reply

    monkeypatch.setattr(itm, "handle", never_done)
    c = FakeClient(itm)
    c.progress_interval = 0.01
    c.progress_timeout = 0.05
    with pytest.raises(ItmCommandError, match=r"loading in time \(0.05 s\)"):
        await c.fetch_history()
    assert itm.history_cancelled == 1
    assert itm.session is None


async def test_progress_of_a_write_is_never_cancelled(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without cancel (a write's progress) no CANCEL_PROG is sent."""
    handle = itm.handle

    def never_done(body: str) -> str:
        reply = handle(body)
        itm.history_loading = True
        return reply

    c = FakeClient(itm)
    c.progress_interval = 0
    async with c._session() as s:
        await s.open_menu()
        monkeypatch.setattr(itm, "handle", never_done)
        reply = await s.send(MenuScreen.MENULIST, MB_HISTORY)
        with pytest.raises(ItmCommandError, match="loading"):
            await s.wait_for_progress(reply, seconds=0.02)
    assert not any(r["event"] == CANCEL_PROG for r in itm.requests)
    assert itm.history_cancelled == 0


# -- R3: the session file --------------------------------------------------------------


def test_failed_temp_file_removal_keeps_the_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A temp file that can't be removed doesn't replace the save's error."""
    store = FileSessionStore(str(tmp_path / "session.json"))

    def broken_dump(*_args: object, **_kwargs: object) -> None:
        msg = "disk full"
        raise ValueError(msg)

    def no_unlink(_self: Path, *, missing_ok: bool = False) -> None:
        del missing_ok
        msg = "busy"
        raise OSError(msg)

    monkeypatch.setattr(client_module, "json", MagicMock(dump=broken_dump))
    monkeypatch.setattr(Path, "unlink", no_unlink)
    with pytest.raises(ValueError, match="disk full") as caught:
        store._save("0" * 32)
    assert "busy" in caught.value.__notes__[0]


# -- HA gap 2: session_info ------------------------------------------------------------


async def test_session_info(itm: FakeITM, monkeypatch: pytest.MonkeyPatch) -> None:
    """What the client knows about its login, read without a request."""
    c = FakeClient(itm)
    info = c.session_info
    assert not info.logged_in
    assert (info.since, info.logins, info.last_poll, info.last_error) == (
        None,
        0,
        None,
        None,
    )
    async with c.kept_session(keep_alive=None):
        await c.fetch_units()
        await c.fetch_units()
        info = c.session_info
        assert info.logged_in
        assert info.kept
        assert not info.writing
        assert info.since is not None
        assert info.last_poll is not None
        assert info.logins == 1
        sent = len(itm.requests)
        assert c.session_info == info
        assert len(itm.requests) == sent
    info = c.session_info
    assert not info.logged_in
    assert not info.kept
    assert info.since is None

    seen = []
    commit = _Session.commit

    async def spy(self: _Session, delay: float) -> None:
        seen.append(c.session_info)
        await commit(self, delay)

    monkeypatch.setattr(_Session, "commit", spy)
    await c.set_setpoint("1:1-04", 22)
    assert seen[0].writing
    assert seen[0].logged_in
    assert c.session_info.logins == 2

    with pytest.raises(UnitStopped):
        await c.set_setpoint("1:1-09", 17)
    info = c.session_info
    assert info.last_error is not None
    assert info.last_error.startswith("UnitStopped: 1:1-09 is stopped")
    assert info.last_error_at is not None


async def test_session_info_unanswered(itm: FakeITM) -> None:
    """A kept session whose POLLING got no answer says so until it is answered."""
    c = FakeClient(itm)
    async with c.kept_session(keep_alive=None):
        await c.fetch_units()
        itm.fail_next = aiohttp.ClientConnectionError("network down")
        with pytest.raises(aiohttp.ClientConnectionError):
            await c.fetch_units()
        assert c.session_info.unanswered
        assert c.session_info.logged_in
        await c.fetch_units()
        assert not c.session_info.unanswered
    assert itm.logins == 1


async def test_keep_alive_errors_are_noted(itm: FakeITM) -> None:
    """A keep-alive poll that got no answer, or a session the iTM ended, is noted."""
    c = FakeClient(itm)
    async with c.kept_session(keep_alive=None):
        await c.fetch_units()
        block = c._kept
        assert block is not None
        s = block.session
        assert s is not None
        itm.fail_next = aiohttp.ClientConnectionError("network down")
        await c._keep_alive_poll(block, s)
        assert c.session_info.unanswered
        assert "network down" in str(c.session_info.last_error)
        itm.restart()
        await c._keep_alive_poll(block, s)
        assert str(c.session_info.last_error).startswith("SessionEnded")
        assert block.session is None


# -- HA gap 5: typed command errors ----------------------------------------------------


async def test_unit_stopped(itm: FakeITM) -> None:
    """A setpoint for a stopped unit or zone: UnitStopped, the message as before."""
    with pytest.raises(UnitStopped, match="1:1-09 is stopped") as caught:
        await FakeClient(itm).set_setpoint("1:1-09", 17)
    assert caught.value.name == "1:1-09"
    for u in itm.units:
        u.running = False
    c = FakeClient(itm, zone_path=("Building",))
    with pytest.raises(UnitStopped) as caught:
        await c.set_zone_setpoint("Indoor units", 22)
    assert caught.value.name == "Indoor units"


async def test_not_offered(itm: FakeITM) -> None:
    """What a unit doesn't offer: NotOffered with the options it does offer."""
    c = FakeClient(itm)
    with pytest.raises(NotOffered, match="not offered") as caught:
        await c.set_power_state("1:1-04", PowerState.SETBACK)
    assert caught.value.value is PowerState.SETBACK
    assert PowerState.SETBACK_LOW in caught.value.options
    with pytest.raises(NotOffered) as caught:
        await c.apply("1:1-04", mode="dry")
    assert caught.value.name == "1:1-04"
    assert caught.value.options == ("fan", "cool", "heat")
    with pytest.raises(NotOffered) as caught:
        await c.set_unit_settings(itm.units[0].name, low_noise="Turbo")
    assert caught.value.value == "Turbo"
    with pytest.raises(SetpointOutOfRange) as caught_range:
        await c.set_setpoint("1:1-04", 40)
    assert isinstance(caught_range.value, NotOffered)
    assert caught_range.value.options == caught_range.value.allowed
    zone = FakeClient(itm, zone_path=("Building",))
    with pytest.raises(NotOffered) as caught:
        await zone.set_zone_power_state("Indoor units", PowerState.SETBACK)
    assert caught.value.name == "Indoor units"


async def test_not_offered_lock(itm: FakeITM) -> None:
    """A wall remote lock value the dialog lacks: NotOffered with its values."""
    with pytest.raises(NotOffered) as caught:
        await FakeClient(itm).set_unit_settings(
            itm.units[0].name, mode_lock=RemoteLock.STOP_ONLY
        )
    assert caught.value.value is RemoteLock.STOP_ONLY
    assert caught.value.options


async def test_not_confirmed(itm: FakeITM) -> None:
    """A change that doesn't show: NotConfirmed with the last read-back."""
    itm.latency_logins = 10_000
    c = FakeClient(itm, confirm_timeout=0)
    with pytest.raises(NotConfirmed, match="not confirmed") as caught:
        await c.set_mode("1:1-04", "heat")
    assert isinstance(caught.value.last_seen, Unit)
    assert caught.value.name == "1:1-04"
    with pytest.raises(NotConfirmed, match="did not take") as caught:
        await c.set_unit_settings(itm.units[0].name, overtime=True)
    assert isinstance(caught.value.last_seen, UnitSettings)
    itm.root = FakeZone("top", [FakeZone("Indoor units", itm.units[:3])])
    zone = FakeClient(itm, zone_path=(), confirm_timeout=0)
    with pytest.raises(NotConfirmed) as caught:
        await zone.set_zone_power_state("Indoor units", PowerState.OFF)
    assert isinstance(caught.value.last_seen, dict)


def test_check_wrong_is_not_confirmed() -> None:
    """A menu write's read-back that shows other values: NotConfirmed."""
    with pytest.raises(NotConfirmed, match="did not take the name") as caught:
        check_wrong("program", ["name"], {"name": "old"})
    assert caught.value.last_seen == {"name": "old"}
    check_wrong("program", [], None)
