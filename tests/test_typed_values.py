# pylint: disable=protected-access,redefined-outer-name
"""Values typed in input dialogs: which end the login, and their read-back."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import EventSpec, ItmCommandError, TempLimit
from daikin_itm.protocol import DIALOG, DataType, EventType, Reply, ScreenUpdate

from .fake_dealer_functions import FakeDealerITM, Group, Interlock
from .fake_demand import FakeDemandITM
from .fake_emergency import FakeEmergencyITM, Program
from .fake_energy_edit import FakeEnergyEditITM
from .fake_itm import FakeITM
from .fake_schedule import FakeScheduleITM
from .fake_system_edit import FakeSystemEditITM
from .fake_zones import FakeZonesITM
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from daikin_itm import ItmClient

SCREEN, DIALOG_ID = 104, 60
KEEPS = {"CHARINPUT", "NUMBER", "HOURMIN"}
TOKEN = "0" * 31 + "1"


def _session_with_dialog(kind: str) -> Any:
    s = FakeClient(FakeITM())._new_session()
    s._track(
        SCREEN,
        1,
        EventType.CLICK,
        DataType.NONE,
        Reply(TOKEN, [ScreenUpdate(SCREEN, "DLG_OPEN", [DIALOG_ID, kind])]),
    )
    return s


@pytest.mark.parametrize("kind", [*sorted(set(DIALOG.values())), "UNKNOWN"])
def test_answered_dialog_ends_the_login_by_type(kind: str) -> None:
    """A name, number or time of day keeps the login; any other value ends it."""
    s = _session_with_dialog(kind)
    s._track(SCREEN, DIALOG_ID, EventType.CLICK, DataType.STRING, Reply(TOKEN, []))
    assert not s._dialogs
    if kind in KEEPS:
        assert s._stuck is None
    else:
        assert s._stuck == f"a value was entered in a {kind} dialog"


@pytest.mark.parametrize("kind", ["DATE", "TIME", "PASSWORD"])
def test_cancelled_dialog_keeps_the_login(kind: str) -> None:
    """A dialog left with Cancel enters nothing."""
    s = _session_with_dialog(kind)
    s._track(SCREEN, DIALOG_ID, EventType.CLICK2, DataType.NONE, Reply(TOKEN, []))
    assert not s._dialogs
    assert s._stuck is None


async def test_typed_number_is_no_pick() -> None:
    """A number typed in a dialog never shows as a control's value."""
    s = _session_with_dialog("NUMBER")
    s.state.screens[SCREEN] = {(DIALOG_ID, "SINGLE_SELIDX"): 0}

    async def post(_: str) -> str:
        return FakeITM._reply(TOKEN, [])

    s._post = post
    await s.send(SCREEN, DIALOG_ID, EventType.CLICK, DataType.INT, 5)
    assert s.state.get(SCREEN, DIALOG_ID, "SINGLE_SELIDX") == 0
    assert not s.state.picked(SCREEN)


@dataclass
class Typed:
    """A write that types a value in a dialog, and the simulated iTM it needs."""

    make: Callable[[], FakeITM]
    call: Callable[[ItmClient, Any], Awaitable[object]]


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


TYPED: dict[str, Typed] = {
    "unit limit (NUMBER)": Typed(
        FakeITM,
        lambda c, _: c.set_unit_settings(
            "1:1-04", cool_limit=TempLimit(enabled=True, minimum=20, maximum=30)
        ),
    ),
    "special day name (CHARINPUT)": Typed(
        FakeScheduleITM,
        lambda c, _: c.rename_schedule_special_day("Always", "Ex2", "Holiday"),
    ),
    "event time (HOURMIN)": Typed(
        FakeScheduleITM,
        lambda c, _: c.edit_schedule_event(
            "Always",
            1,
            "06:00",
            "Indoor units",
            EventSpec("06:30", "Indoor units", zone=True),
        ),
    ),
    "program name (CHARINPUT)": Typed(
        FakeITM, lambda c, _: c.create_schedule("Program Z")
    ),
    "zone name (CHARINPUT)": Typed(
        FakeZonesITM, lambda c, _: c.create_zone("demo group")
    ),
    "point description (CHARINPUT)": Typed(
        FakeSystemEditITM,
        lambda c, itm: c.set_point_attributes(itm.units[0].name, info="Hall"),
    ),
    "interlock name (CHARINPUT)": Typed(
        _with_interlock, lambda c, _: c.rename_interlock("Program A", "Program B")
    ),
    "temperature limit (NUMBER)": Typed(
        _with_limits, lambda c, _: c.set_temperature_limit("Offices", lower=17)
    ),
    "event setpoint (NUMBER)": Typed(
        FakeScheduleITM,
        lambda c, _: c.add_schedule_event(
            "Always", 3, EventSpec("07:45", "1:1-09", setpoint=21)
        ),
    ),
    "program copy name (CHARINPUT)": Typed(
        FakeScheduleITM, lambda c, _: c.copy_schedule("Always", "Program Z")
    ),
    "e-mail address (CHARINPUT)": Typed(
        FakeSystemEditITM,
        lambda c, itm: c.add_email_address("ops@example.com", [itm.units[0].name]),
    ),
    "planned consumption (NUMBER)": Typed(
        FakeEnergyEditITM, lambda c, _: c.set_planned_consumption(ratio=7.5)
    ),
    "demand target (NUMBER)": Typed(
        FakeDemandITM, lambda c, _: c.set_demand_target(120.5)
    ),
    "stop program name (CHARINPUT)": Typed(
        _with_stop_program,
        lambda c, _: c.rename_emergency_stop_program("Fire alarm", "Smoke"),
    ),
}


@pytest.mark.parametrize("name", sorted(TYPED))
async def test_typed_value_reads_back_in_its_login(name: str) -> None:
    """The write and its read-back take one login."""
    typed = TYPED[name]
    itm = typed.make()
    itm.latency_logins = 0
    assert await typed.call(FakeClient(itm), itm) is not None
    assert itm.logins == 1
    assert itm.session is None


@pytest.mark.parametrize("name", sorted(TYPED))
async def test_typed_value_ignored_is_caught(name: str) -> None:
    """The read-back is what the iTM sends again, never the typed value."""
    typed = TYPED[name]
    itm = typed.make()
    itm.ignores_typed_values = True
    c = FakeClient(itm, confirm_timeout=0.05)
    with pytest.raises(ItmCommandError, match=r"did not|, not "):
        await typed.call(c, itm)
    assert itm.typed_values_ignored
    assert itm.logins == 1
    assert itm.session is None
