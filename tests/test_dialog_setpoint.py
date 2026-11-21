# pylint: disable=protected-access,redefined-outer-name
"""The setpoint through the unit's settings dialog: set_setpoint and apply."""

from __future__ import annotations

from typing import Any

import pytest

from daikin_itm import (
    ItmCommandError,
    NotConfirmed,
    NotOffered,
    ReadOnlyError,
    SetpointOutOfRange,
    UnitStopped,
)

from .fake_itm import FakeITM
from .test_client import FakeClient, confirmed

STOPPED = "1:1-09"
RUNNING = "1:1-04"


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM; 1:1-09 is stopped, its range 16-32."""
    return FakeITM()


def _sent(itm: FakeITM, screen: int, part: int) -> list[dict[str, Any]]:
    return [r for r in itm.requests if r["screen"] == screen and r["part"] == part]


async def test_stopped_unit_through_the_dialog(itm: FakeITM) -> None:
    """A stopped unit takes the setpoint from the dialog, stays stopped, keeps it."""
    c = FakeClient(itm)
    unit = await c.set_setpoint(STOPPED, 22, when_stopped="dialog")
    assert unit is not None
    assert (unit.setpoint, unit.running) == (22, False)
    assert (itm.units[2].setpoint, itm.units[2].running) == (22, False)
    answers = _sent(itm, 7, 32)
    assert [(r["dtype"], r["array"]) for r in answers] == [(16, [22, 0])]
    assert not _sent(itm, 4, 11)
    assert itm.dialog is None
    assert itm.session is None
    assert c.writes_sent == {STOPPED: 1}
    started = await c.apply(STOPPED, power=True)
    assert started is not None
    assert (started.setpoint, started.running) == (22, True)


async def test_stopped_unit_refused_by_default(itm: FakeITM) -> None:
    """Without when_stopped="dialog" a stopped unit's setpoint is refused."""
    with pytest.raises(UnitStopped):
        await FakeClient(itm).set_setpoint(STOPPED, 22, when_stopped="refuse")
    assert not _sent(itm, 7, 23)
    assert itm.units[2].setpoint == 18


async def test_running_unit_keeps_the_panel(itm: FakeITM) -> None:
    """A running unit takes the cheaper panel path, whatever when_stopped says."""
    unit = await FakeClient(itm).set_setpoint(RUNNING, 25, when_stopped="dialog")
    assert unit is not None
    assert unit.setpoint == 25
    assert _sent(itm, 4, 11)
    assert not _sent(itm, 4, 17)


async def test_out_of_the_dialogs_range(itm: FakeITM) -> None:
    """Outside the dialog's range: SetpointOutOfRange, nothing written."""
    c = FakeClient(itm)
    with pytest.raises(SetpointOutOfRange) as caught:
        await c.set_setpoint(STOPPED, 40, when_stopped="dialog")
    assert caught.value.allowed == tuple(range(16, 33))
    assert [r["event"] for r in _sent(itm, 7, 32)] == [2]
    assert not _sent(itm, 7, 5)
    assert itm.dialog is None
    assert itm.units[2].setpoint == 18
    assert c.writes_sent == {}


async def test_same_setpoint_writes_nothing(itm: FakeITM) -> None:
    """A stopped unit that shows the setpoint already gets no write."""
    c = FakeClient(itm)
    unit = await c.set_setpoint(STOPPED, 18, when_stopped="dialog")
    assert unit is not None
    assert unit.setpoint == 18
    assert not _sent(itm, 4, 17)
    assert c.writes_sent == {}


async def test_unknown_when_stopped(itm: FakeITM) -> None:
    """An unknown when_stopped is refused before anything is sent."""
    c = FakeClient(itm)
    with pytest.raises(ValueError, match="when_stopped"):
        await c.set_setpoint(STOPPED, 22, when_stopped="panel")
    with pytest.raises(ValueError, match="when_stopped"):
        await c.apply(STOPPED, setpoint=22, when_stopped="panel")
    assert not itm.requests
    with pytest.raises(ReadOnlyError):
        await FakeClient(itm, read_only=True).set_setpoint(
            STOPPED, 22, when_stopped="dialog"
        )
    assert not itm.requests


async def test_dry_run(itm: FakeITM) -> None:
    """A dry run enters the number, plans the OK and cancels the dialog."""
    c = FakeClient(itm, dry_run=True)
    assert await c.set_setpoint(STOPPED, 22, when_stopped="dialog") is None
    assert c.planned == ["save settings: screen 7 part 5 event 1"]
    assert itm.units[2].setpoint == 18
    assert itm.dialog is None


async def test_apply_with_setpoint(itm: FakeITM) -> None:
    """A setpoint goes with the mode in one apply, one dialog write."""
    calls: list[tuple[str, str]] = []
    c = FakeClient(itm, on_write=lambda unit, what: calls.append((unit, what)))
    unit = await c.apply(RUNNING, mode="heat", setpoint=22)
    assert unit is not None
    assert (unit.mode, unit.setpoint) == ("heat", 22)
    assert len(_sent(itm, 4, 17)) == 1
    assert calls == [(RUNNING, "mode heat, setpoint 22")]
    assert c.recent_writes() == {RUNNING: 1}
    assert await c.apply(RUNNING, setpoint=22) is not None
    assert c.writes_sent == {RUNNING: 1}


async def test_apply_setpoint_while_stopped(itm: FakeITM) -> None:
    """A setpoint for a unit that stays stopped is refused, unless asked."""
    c = FakeClient(itm)
    with pytest.raises(UnitStopped):
        await c.apply(STOPPED, setpoint=20)
    with pytest.raises(UnitStopped):
        await c.apply(RUNNING, power=False, setpoint=20)
    assert not _sent(itm, 4, 17)
    unit = await c.apply(STOPPED, setpoint=20, when_stopped="dialog")
    assert unit is not None
    assert (unit.setpoint, unit.running) == (20, False)
    unit = await c.apply(STOPPED, power=True, setpoint=21)
    assert unit is not None
    assert (unit.setpoint, unit.running) == (21, True)


async def test_not_offered(itm: FakeITM) -> None:
    """A dialog without a setpoint (a ventilation unit) is refused."""
    c = FakeClient(itm, zone_path=("Building", "Ventilation"))
    with pytest.raises(NotOffered, match="offers no setpoint"):
        await c.apply("Vent 1", setpoint=21, when_stopped="dialog")
    assert itm.dialog is None


async def test_control_kept_disabled(itm: FakeITM) -> None:
    """A "change" button the iTM keeps disabled is never clicked."""
    real = itm._setpoint_event

    def tick_only(ev: dict[str, Any]) -> list[Any]:
        if ev["part"] == 19:
            return [("UPDATE", 7, [], [(19, "CHECK_STATE", 1)])]
        return real(ev)

    itm._setpoint_event = tick_only  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="keeps its control disabled"):
        await FakeClient(itm).set_setpoint(STOPPED, 22, when_stopped="dialog")
    assert not _sent(itm, 7, 23)
    assert itm.dialog is None


async def test_read_back_in_the_writes_login(itm: FakeITM) -> None:
    """The check reads the panel in the write's own login, after the bus delay."""
    itm.latency_logins = 3
    c = FakeClient(itm)
    assert (
        confirmed(await c.set_setpoint(STOPPED, 22, when_stopped="dialog")).setpoint
        == 22
    )
    assert (
        confirmed(await c.apply(STOPPED, setpoint=23, when_stopped="dialog")).setpoint
        == 23
    )
    assert itm.logins == 2
    assert itm.session is None


async def test_read_back_in_a_kept_session(itm: FakeITM) -> None:
    """Inside kept_session() the dialog write and its check use the kept login."""
    itm.latency_logins = 3
    c = FakeClient(itm)
    async with c.kept_session():
        unit = await c.set_setpoint(STOPPED, 22, when_stopped="dialog")
        assert confirmed(unit).setpoint == 22
        assert confirmed(await c.fetch_unit(STOPPED)).setpoint == 22
    assert itm.logins == 1
    assert itm.session is None


async def test_typed_value_is_no_read_back(itm: FakeITM) -> None:
    """A unit that doesn't take the dialog's setpoint is not confirmed."""
    itm.units[2].ignores_writes = True
    c = FakeClient(itm, confirm_timeout=0.05)
    with pytest.raises(NotConfirmed, match="setpoint=18"):
        await c.set_setpoint(STOPPED, 22, when_stopped="dialog")
    assert itm.units[2].setpoint == 18
    assert itm.session is None


async def test_ticks_the_visible_setpoint_checkbox(itm: FakeITM) -> None:
    """The dialog's setpoint checkbox is part 19; part 20 next to it stays hidden."""
    c = FakeClient(itm)
    assert (
        confirmed(await c.set_setpoint(STOPPED, 22, when_stopped="dialog")).setpoint
        == 22
    )
    assert _sent(itm, 7, 19)
    assert not _sent(itm, 7, 20)
