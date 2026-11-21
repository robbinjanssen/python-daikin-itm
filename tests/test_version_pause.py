"""Writes paused after a web UI version change (pause_writes_on_version_change)."""

from __future__ import annotations

from typing import Any

import pytest

from daikin_itm import ItmCommandError, Unit, WritesPaused
from daikin_itm.protocol import WEB_VERSION, EventType

from .fake_demand import FakeDemandITM
from .fake_itm import FakeITM
from .test_client import FakeClient, is_logout
from .test_shared_session import _writes
from .test_write_session import _arguments

NEW = "Ver1.40.00"
UNIT = "1:1-04"


@pytest.fixture(name="itm")
def fixture_itm() -> FakeITM:
    """Return a simulated iTM that was updated to another web UI version."""
    fake = FakeITM()
    fake.web_version = NEW
    return fake


def commits(itm: FakeITM) -> int:
    """Count the operation panel commits sent."""
    return sum(r["event"] == EventType.TIMER for r in itm.requests)


async def test_off_by_default(itm: FakeITM) -> None:
    """Without the option a write goes on after the change, as before."""
    c = FakeClient(itm)
    assert c.pause_writes_on_version_change is False
    await c.set_setpoint(UNIT, 21)
    assert itm.units[1].setpoint == 21
    assert c.web_version == NEW
    assert c.session_info.writes_paused is None


async def test_paused_until_confirmed(itm: FakeITM) -> None:
    """After the change reads work, writes refuse before sending, until confirmed."""
    c = FakeClient(itm, pause_writes_on_version_change=True)
    assert len(await c.fetch_units()) == 3
    assert c.session_info.writes_paused == (WEB_VERSION, NEW)
    sent = len(itm.requests)
    with pytest.raises(WritesPaused) as exc:
        await c.set_setpoint(UNIT, 21)
    assert isinstance(exc.value, ItmCommandError)
    assert (exc.value.old_version, exc.value.new_version) == (WEB_VERSION, NEW)
    assert len(itm.requests) == sent
    assert itm.units[1].setpoint == 23
    assert (await c.fetch_units())[UNIT].setpoint == 23
    c.confirm_web_version()
    assert c.session_info.writes_paused is None
    await c.set_setpoint(UNIT, 21)
    assert itm.units[1].setpoint == 21
    c.confirm_web_version()
    assert c.session_info.writes_paused is None


async def test_the_write_that_finds_the_change_sends_no_change(itm: FakeITM) -> None:
    """A write whose own login finds the change stops before the change."""
    c = FakeClient(itm, pause_writes_on_version_change=True)
    with pytest.raises(WritesPaused):
        await c.set_setpoint(UNIT, 21)
    assert commits(itm) == 0
    assert itm.units[1].setpoint == 23
    assert itm.session is None


async def test_a_dry_run_is_paused_too(itm: FakeITM) -> None:
    """A dry run plans nothing while writes are paused."""
    dry = FakeClient(itm, dry_run=True, pause_writes_on_version_change=True)
    await dry.fetch_units()
    with pytest.raises(WritesPaused):
        await dry.set_setpoint(UNIT, 21)
    assert not dry.planned
    dry.confirm_web_version()
    assert await dry.set_setpoint(UNIT, 21) is None
    assert dry.planned


async def test_the_known_version_does_not_pause(itm: FakeITM) -> None:
    """A version the client was given before its first login is no change."""
    c = FakeClient(itm, pause_writes_on_version_change=True)
    c.web_version = NEW
    await c.set_setpoint(UNIT, 21)
    assert itm.units[1].setpoint == 21
    assert c.session_info.writes_paused is None


async def test_a_second_change_keeps_the_first_version(itm: FakeITM) -> None:
    """Two changes before a confirmation: from the first known version on."""
    c = FakeClient(itm, pause_writes_on_version_change=True)
    await c.fetch_units()
    itm.web_version = "Ver1.50.00"
    await c.fetch_units()
    assert c.session_info.writes_paused == (WEB_VERSION, "Ver1.50.00")


async def test_menu_writes_are_paused() -> None:
    """A demand control write refuses as well, before anything is sent."""
    demand = FakeDemandITM()
    demand.web_version = NEW
    c = FakeClient(demand, pause_writes_on_version_change=True)
    await c.fetch_units()
    sent = len(demand.requests)
    with pytest.raises(WritesPaused):
        await c.set_demand_target(120.5)
    assert len(demand.requests) == sent
    assert demand.target != 120.5


@pytest.mark.parametrize("dry_run", [False, True], ids=["sent", "dry-run"])
@pytest.mark.parametrize("name", _writes())
async def test_every_write_is_paused(itm: FakeITM, name: str, *, dry_run: bool) -> None:
    """Whatever isn't a read refuses while paused, before anything is sent."""
    c = FakeClient(itm, dry_run=dry_run, pause_writes_on_version_change=True)
    await c.fetch_indicators()
    sent = len(itm.requests)
    args, kwargs = _arguments(c, name)
    with pytest.raises(WritesPaused):
        await getattr(c, name)(*args, **kwargs)
    assert len(itm.requests) == sent
    assert not c.planned


async def test_reads_are_not_paused(itm: FakeITM) -> None:
    """Reads of every kind go on while writes are paused."""
    c = FakeClient(itm, pause_writes_on_version_change=True)
    await c.fetch_indicators()
    assert c.session_info.writes_paused == (WEB_VERSION, NEW)
    assert (await c.fetch_unit(UNIT)).setpoint == 23
    assert UNIT in await c.fetch_points()
    assert (await c.fetch_unit_settings(UNIT)).overtime is not None
    assert await c.fetch_overtime_duration()
    async with c.shared_session():
        assert UNIT in await c.fetch_units()


async def test_paused_in_a_kept_session_and_after_it(itm: FakeITM) -> None:
    """A kept session's writes are paused too, also after the block, until confirmed."""
    c = FakeClient(itm, allow_experimental=False, pause_writes_on_version_change=True)
    async with c.kept_session(keep_alive=None):
        assert UNIT in await c.fetch_units()
        with pytest.raises(WritesPaused):
            await c.set_setpoint(UNIT, 21)
        assert commits(itm) == 0
        assert (await c.fetch_unit(UNIT)).setpoint == 23
    with pytest.raises(WritesPaused):
        await c.set_setpoint(UNIT, 21)
    c.confirm_web_version()
    async with c.kept_session(keep_alive=None):
        await c.set_setpoint(UNIT, 21)
    assert itm.units[1].setpoint == 21


async def test_paused_after_a_refused_write_block(itm: FakeITM) -> None:
    """The write block a refused write opened ends; the next write is paused too."""
    c = FakeClient(itm, pause_writes_on_version_change=True)
    with pytest.raises(WritesPaused):
        await c.set_setpoint(UNIT, 21)
    assert not c.session_info.writing
    assert not c.session_info.logged_in
    with pytest.raises(WritesPaused):
        await c.set_zone_setpoint("Indoor units", 21)
    assert commits(itm) == 0


async def test_back_to_the_known_version_resumes(itm: FakeITM) -> None:
    """The iTM reporting the version known before again is no change any more."""
    c = FakeClient(itm, pause_writes_on_version_change=True)
    await c.fetch_units()
    itm.web_version = WEB_VERSION
    await c.fetch_units()
    assert c.session_info.writes_paused is None
    assert c.web_version == WEB_VERSION
    await c.set_setpoint(UNIT, 21)
    assert itm.units[1].setpoint == 21


class PausedWhileChecking(FakeClient):
    """A read of the check finds writes paused, as after another login saw a change.

    bodies: the requests sent; read: how many were sent by the last read's end.
    """

    def __init__(self, itm: FakeITM, **kw: Any) -> None:
        """Create the client; the check reads every millisecond for a second."""
        super().__init__(
            itm,
            pause_writes_on_version_change=True,
            confirm_interval=0.001,
            verify_timeout=1,
            **kw,
        )
        self.bodies: list[str] = []
        self.read = 0

    async def _post(self, body: str) -> str:
        self.bodies.append(body)
        return await super()._post(body)

    async def fetch_unit(self, name: str) -> Unit:
        """Pause writes, then read the unit."""
        self._version_changed(WEB_VERSION, NEW)
        unit = await super().fetch_unit(name)
        self.read = len(self.bodies)
        return unit

    async def checked(self) -> None:
        """Wait until the background checks are done."""
        await self._end_watches(cancel=False)

    def sent_after_the_read(self) -> list[str]:
        """Return what was sent after the last read, the logout left out."""
        return [b for b in self.bodies[self.read :] if not is_logout(b)]


def _heating() -> FakeITM:
    """Return a simulated iTM whose first unit heats and gets cooling from elsewhere."""
    fake = FakeITM()
    fake.units[0].running, fake.units[0].mode = True, "Heat"
    fake.change_elsewhere(fake.units[0], 4, mode="Cool")
    return fake


async def test_the_check_writes_nothing_after_all_while_paused(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The background check of a write without confirm sends nothing; it is noted."""
    itm = _heating()
    c = PausedWhileChecking(itm)
    assert await c.set_mode("1:1-00", "heat", confirm=False) is not None
    await c.checked()
    assert commits(itm) == 0
    assert itm.units[0].mode == "Cool"
    assert not c.writes_sent
    error = c.session_info.last_error
    assert error is not None
    assert error.startswith("WritesPaused")
    assert any("left out failed" in r.getMessage() for r in caplog.records)
    assert not c.sent_after_the_read()
    assert itm.session is None


async def test_the_write_after_all_with_confirm_is_paused() -> None:
    """With confirm the write after all raises WritesPaused, nothing sent."""
    itm = _heating()
    c = PausedWhileChecking(itm)
    with pytest.raises(WritesPaused):
        await c.set_mode("1:1-00", "heat")
    assert not c.sent_after_the_read()
    assert commits(itm) == 0
    assert itm.units[0].mode == "Cool"
    assert itm.session is None
