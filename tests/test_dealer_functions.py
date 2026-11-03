# pylint: disable=protected-access,redefined-outer-name
"""Dealer-option functions: temperature limit, sliding temperature, HMO, interlock."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import (
    InterlockActionSpec,
    InterlockConditionSpec,
    InterlockOutputSpec,
    ItmCommandError,
    PowerState,
    ReadOnlyError,
    RemoteLock,
    TemperatureLimitGroup,
)
from daikin_itm import cli as itm_cli
from daikin_itm import dealer_functions as df
from daikin_itm.state import UIState

from .fake_dealer_functions import (
    FakeDealerITM,
    Group,
    Interlock,
    _confirm,
    default_action,
    default_condition,
)
from .test_cli import CliClient
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@pytest.fixture
def itm() -> FakeDealerITM:
    """Return a simulated iTM with the dealer screens empty (as seen)."""
    return FakeDealerITM()


async def run(
    itm: FakeDealerITM,
    flow: Callable[..., Awaitable[Any]],
    *args: Any,
    client: FakeClient | None = None,
    **kwargs: Any,
) -> Any:
    """Run one flow in a fresh session, like the client's wrappers do."""
    c = client or FakeClient(itm)
    async with c._session() as s:
        try:
            return await flow(s, *args, **kwargs)
        finally:
            await s.close_menu()


PERSISTING = {60, 61}  # the confirm and name dialog answers
MAIN_SCREENS = {400, 500, 900}  # where a name answer adds, renames or copies


def writes(itm: FakeDealerITM) -> list[tuple[int, int]]:
    """Return the requests that would change something (confirms, names)."""
    return [
        (r["screen"], r["part"])
        for r in itm.requests
        if r["event"] == 1
        and (r["part"] == 60 or (r["part"] == 61 and r["screen"] in MAIN_SCREENS))
    ]


# -- readers --------------------------------------------------------------------------


async def test_empty_screens_as_seen(itm: FakeDealerITM) -> None:
    """With the dealer options off: no groups or programs, every unit off."""
    c = FakeClient(itm)
    assert await c.fetch_temperature_limits(details=True) == []
    assert await c.fetch_sliding_temperatures(details=True) == []
    assert await c.fetch_interlocks() == []
    units = await c.fetch_heating_optimisation()
    assert [u.name for u in units] == itm.points
    assert not any(u.enabled for u in units)
    assert itm.open_screens == set()


def test_parsing() -> None:
    """Temperatures in either decimal style; the off label counts as off."""
    assert df.temperature("18,5 °C") == 18.5
    assert df.temperature("-3") == -3.0
    assert df.temperature("") is None
    state = UIState()
    state.screens[900] = {
        (1, "LIST_ROW"): {
            "rows": [
                {"text": ["Disable", "A"]},
                {"text": ["Enable", "B"]},
                {"text": ["x"]},
            ]
        },
        (4, "LABEL"): "Disable",
    }
    programs = df.interlocks_from_screen(state)
    assert [(p.name, p.enabled) for p in programs] == [("A", False), ("B", True)]


# -- temperature limit ----------------------------------------------------------------


async def test_temperature_limit_flow(itm: FakeDealerITM) -> None:
    """Create with members and limits, change, enable, delete; each read back."""
    c = FakeClient(itm)
    unit_a, unit_b, unit_c = itm.points[:3]
    group = await c.create_temperature_limit(
        "Annex", members=[unit_a, unit_b], lower=18, upper=26.5
    )
    assert group == TemperatureLimitGroup(
        name="Annex",
        enabled=False,
        lower=18.0,
        upper=26.5,
        members=(unit_a, unit_b),
        columns=("", "Annex", "18.0", "26.5"),
    )
    group = await c.set_temperature_limit_members("Annex", [unit_b, unit_c])
    assert group is not None
    assert group.members == (unit_b, unit_c)
    group = await c.set_temperature_limit("Annex", new_name="Wing", lower=17)
    assert group is not None
    assert (group.name, group.lower, group.upper) == ("Wing", 17.0, 26.5)
    group = await c.set_temperature_limit_enabled("Wing", enabled=True)
    assert group is not None
    assert group.enabled
    assert (await c.set_temperature_limit_enabled("Wing", enabled=True)) == group
    with pytest.raises(ItmCommandError, match="disable the temperature limit group"):
        await c.delete_temperature_limit("Wing")
    await c.set_temperature_limit_enabled("Wing", enabled=False)
    await c.delete_temperature_limit("Wing")
    assert itm.limits == []
    assert itm.open_screens == set()


async def test_temperature_limit_refusals(itm: FakeDealerITM) -> None:
    """Unknown, duplicate, too long, not offered and out of range are refused."""
    itm.limits += [Group("A"), Group("B")]
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="exists already"):
        await c.create_temperature_limit("A")
    with pytest.raises(ItmCommandError, match="1-20 characters"):
        await c.create_temperature_limit("x" * 21)
    with pytest.raises(ItmCommandError, match="not found once"):
        await c.set_temperature_limit_enabled("C", enabled=True)
    with pytest.raises(ItmCommandError, match="not offered"):
        await c.set_temperature_limit_members("A", ["Nowhere"])
    with pytest.raises(ItmCommandError, match="exists already"):
        await c.set_temperature_limit("A", new_name="B")
    with pytest.raises(ItmCommandError, match="outside the iTM's range"):
        await c.set_temperature_limit("A", upper=99)
    assert itm.limit("A").upper == 30.0
    assert writes(itm) == []
    assert itm.open_screens == set()


# -- sliding temperature --------------------------------------------------------------


async def test_sliding_temperature_flow(itm: FakeDealerITM) -> None:
    """Create, pick the outdoor point and ranges, members, enable, delete."""
    c = FakeClient(itm)
    unit_a = itm.points[0]
    group = await c.create_sliding_temperature("Hall", members=[unit_a])
    assert group is not None
    assert group.members == (unit_a,)
    assert group.outdoor_point is None
    assert await c.fetch_sliding_temperature_inputs("Hall") == (
        "AI Outdoor sensor",
        "AI Return sensor",
    )
    group = await c.set_sliding_temperature(
        "Hall",
        new_name="Lobby",
        outdoor_point="Outdoor sensor",
        outdoor_range=(0, 30),
        setpoint_range=(21, 25.5),
    )
    assert group is not None
    assert group.name == "Lobby"
    assert group.outdoor_point == "Outdoor sensor"
    assert group.outdoor_range == (0.0, 30.0)
    assert group.setpoint_range == (21.0, 25.5)
    group = await c.set_sliding_temperature_members("Lobby", [])
    assert group is not None
    assert group.members == ()
    group = await c.set_sliding_temperature_enabled("Lobby", enabled=True)
    assert group is not None
    assert group.enabled
    shown = await c.fetch_sliding_temperatures()
    assert shown[0].columns[:3] == ("Enable", "Lobby", "Outdoor sensor")
    await c.set_sliding_temperature_enabled("Lobby", enabled=False)
    await c.delete_sliding_temperature("Lobby")
    assert itm.slides == []
    with pytest.raises(ItmCommandError, match="not found once"):
        await c.set_sliding_temperature("Lobby", outdoor_point="Nowhere")
    assert itm.open_screens == set()


async def test_sliding_point_not_offered(itm: FakeDealerITM) -> None:
    """An analog input the picker doesn't list is refused; nothing is saved."""
    itm.slides.append(Group("Hall"))
    with pytest.raises(ItmCommandError, match="analog input 'Nowhere' not found"):
        await FakeClient(itm).set_sliding_temperature("Hall", outdoor_point="Nowhere")
    assert writes(itm) == []
    assert itm.open_screens == set()


# -- heating mode optimisation --------------------------------------------------------


async def test_heating_optimisation(itm: FakeDealerITM) -> None:
    """Per unit with the radios, all with All on / All off."""
    c = FakeClient(itm)
    unit_a, unit_b = itm.points[:2]
    result = await c.set_heating_optimisation([unit_a, unit_b], enabled=True)
    assert result is not None
    assert [u.name for u in result if u.enabled] == [unit_a, unit_b]
    before = len(writes(itm))
    await c.set_heating_optimisation([unit_a], enabled=True)
    assert len(writes(itm)) == before
    result = await c.set_heating_optimisation(enabled=True)
    assert result is not None
    assert all(u.enabled for u in result)
    await c.set_heating_optimisation(enabled=True)  # already: nothing sent
    result = await c.set_heating_optimisation(enabled=False)
    assert result is not None
    assert not any(u.enabled for u in result)
    with pytest.raises(ItmCommandError, match="not found once"):
        await c.set_heating_optimisation(["Nowhere"], enabled=True)
    itm.hmo.clear()  # nothing listed: All off stays disabled
    with pytest.raises(ItmCommandError, match="the button disabled"):
        await run(itm, df.set_heating_optimisation_all, enabled=False)
    assert itm.open_screens == set()


# -- interlock ------------------------------------------------------------------------


async def test_interlock_programs(itm: FakeDealerITM) -> None:
    """Add, rename, copy, enable, disable and delete, each read back."""
    c = FakeClient(itm)
    program = await c.create_interlock("Program A")
    assert program is not None
    assert not program.enabled
    renamed = await c.rename_interlock("Program A", "Program B")
    assert renamed is not None
    assert renamed.name == "Program B"
    copy = await c.copy_interlock("Program B", "Program C")
    assert copy is not None
    enabled = await c.set_interlock_enabled("Program C", enabled=True)
    assert enabled is not None
    assert enabled.enabled
    with pytest.raises(ItmCommandError, match="disable the interlock program"):
        await c.delete_interlock("Program C")
    with pytest.raises(ItmCommandError, match="exists already"):
        await c.rename_interlock("Program B", "Program C")
    await c.set_interlock_enabled("Program C", enabled=False)
    await c.delete_interlock("Program C")
    assert [(p.name, p.enabled) for p in await c.fetch_interlocks()] == [
        ("Program B", False)
    ]
    assert itm.open_screens == set()


async def test_interlock_setup(itm: FakeDealerITM) -> None:
    """Inputs, outputs, conditions and intervals; an enabled program read only."""
    itm.interlocks.append(Interlock("Program A"))
    c = FakeClient(itm)
    a, b, x = itm.points[:3]
    setup = await c.set_interlock_setup(
        "Program A",
        inputs=[a],
        outputs=[
            None,
            InterlockOutputSpec(condition="error", outputs=(b, x), interval=15),
        ],
    )
    assert setup is not None
    assert setup.inputs == (("Input", a),)
    assert setup.outputs[0].outputs == ()
    assert setup.outputs[1].outputs == (("Output", b), ("Output", x))
    assert setup.outputs[1].condition == "Error"
    assert setup.outputs[1].interval == "15 min"
    setup = await c.set_interlock_setup(
        "Program A",
        inputs=[b],
        outputs=[InterlockOutputSpec(outputs=(a,)), InterlockOutputSpec(outputs=(x,))],
    )
    assert setup is not None
    assert itm.interlock("Program A").inputs == [b]
    assert itm.interlock("Program A").outputs == [[a], [x]]
    await c.set_interlock_enabled("Program A", enabled=True)
    detailed = await c.fetch_interlock_setup("Program A", details=True)
    assert detailed.input_conditions == ("At on", "At error")
    assert detailed.outputs[1].events == (("Output", x),)
    with pytest.raises(ItmCommandError, match="disable the interlock program"):
        await c.set_interlock_setup("Program A", inputs=[a])
    assert itm.open_screens == set()


async def test_interlock_setup_refusals(itm: FakeDealerITM) -> None:
    """Options and points the iTM doesn't offer, and too many tabs."""
    itm.interlocks.append(Interlock("Program A"))
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="is not offered"):
        await c.set_interlock_setup(
            "Program A", outputs=[InterlockOutputSpec(condition="Nooit")]
        )
    with pytest.raises(ItmCommandError, match="is not offered"):
        await c.set_interlock_setup("Program A", inputs=["Nowhere"])
    with pytest.raises(ItmCommandError, match="2 output tabs"):
        await c.set_interlock_setup("Program A", outputs=[None, None, None])
    assert writes(itm) == []
    assert itm.open_screens == set()


# -- dry run, read only, disabled parts -----------------------------------------------


async def test_dry_run_sends_no_writes(itm: FakeDealerITM) -> None:
    """A dry run plans every write and changes nothing."""
    itm.limits.append(Group("A"))
    itm.slides.append(Group("S"))
    itm.interlocks.append(Interlock("P"))
    dry = FakeClient(itm, dry_run=True)
    unit = itm.points[0]
    assert await dry.create_temperature_limit("B") is None
    assert await dry.set_temperature_limit("A", new_name="C", lower=17) is None
    assert await dry.set_temperature_limit_members("A", [unit]) is None
    assert await dry.set_temperature_limit_enabled("A", enabled=True) is None
    await dry.delete_temperature_limit("A")
    assert await dry.create_sliding_temperature("T") is None
    assert (
        await dry.set_sliding_temperature(
            "S", outdoor_point="Outdoor sensor", setpoint_range=(20, 23)
        )
        is None
    )
    assert await dry.set_sliding_temperature_members("S", [unit]) is None
    assert await dry.set_sliding_temperature_enabled("S", enabled=True) is None
    await dry.delete_sliding_temperature("S")
    assert await dry.set_heating_optimisation([unit], enabled=True) is None
    assert await dry.set_heating_optimisation(enabled=True) is None
    assert await dry.create_interlock("Q") is None
    assert await dry.rename_interlock("P", "Q") is None
    assert await dry.copy_interlock("P", "Q") is None
    assert await dry.set_interlock_enabled("P", enabled=True) is None
    await dry.delete_interlock("P")
    assert (
        await dry.set_interlock_setup(
            "P", inputs=[unit], outputs=[InterlockOutputSpec(outputs=(unit,))]
        )
        is None
    )
    assert writes(itm) == []
    assert len(dry.planned) == 18
    assert dry.planned[0] == (
        "create temperature limit group B: screen 400 part 61 event 1 'B'"
    )
    assert [g.name for g in itm.limits] == ["A"]
    assert itm.limit("A").lower == 16.0
    assert itm.limit("A").members == []
    assert itm.slide("S").point == ""
    assert not any(itm.hmo.values())
    assert [(p.name, p.enabled, p.inputs) for p in itm.interlocks] == [("P", False, [])]
    assert itm.open_screens == set()


async def test_read_only_refuses(itm: FakeDealerITM) -> None:
    """A read-only client refuses every write before a request is sent."""
    itm.limits.append(Group("A"))
    ro = FakeClient(itm, read_only=True)
    for call in (
        lambda: ro.create_temperature_limit("B"),
        lambda: ro.set_temperature_limit("A", lower=17),
        lambda: ro.delete_sliding_temperature("A"),
        lambda: ro.set_heating_optimisation(enabled=True),
        lambda: ro.create_interlock("P"),
        lambda: ro.set_interlock_setup("P", inputs=[]),
    ):
        with pytest.raises(ReadOnlyError):
            await call()
    assert itm.requests == []
    for flow, args in (
        (df.create_group, (df.TEMP_LIMIT, "B")),
        (df.set_heating_optimisation, ((),)),
        (df.delete_interlock, ("P",)),
    ):
        with pytest.raises(ReadOnlyError):
            await run(
                itm,
                flow,
                *args,
                client=ro,
                **({"enabled": True} if flow is df.set_heating_optimisation else {}),
            )
    assert writes(itm) == []


class _Disabled(FakeDealerITM):
    """An iTM that keeps some buttons disabled after a selection."""

    def _group_selection(self, screen: int) -> Any:
        return [*super()._group_selection(screen), *self._enable(screen, {9: False})]

    def _il_selection(self) -> Any:
        return [*super()._il_selection(), *self._enable(900, {10: False, 7: False})]


async def test_disabled_parts_are_never_clicked() -> None:
    """A button the iTM reports disabled raises before it is clicked."""
    itm = _Disabled()
    itm.limits.append(Group("A"))
    itm.interlocks.append(Interlock("P"))
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="the button disabled"):
        await c.delete_temperature_limit("A")
    with pytest.raises(ItmCommandError, match="the button disabled"):
        await c.fetch_interlock_setup("P")
    with pytest.raises(ItmCommandError, match="the button disabled"):
        await c.copy_interlock("P", "Q")
    assert itm.limits[0].name == "A"
    assert itm.open_screens == set()


class _Unchanged(FakeDealerITM):
    """An iTM that confirms everything but changes nothing."""

    def _event(self, ev: dict[str, Any]) -> Any:
        if ev["part"] in PERSISTING and ev["event"] == 1 and ev["screen"] >= 400:
            dialog = self.dl_dialog
            reply = super()._event({**ev, "event": 2})
            if dialog is not None and dialog[0] in {401, 402, 501, 502, 901}:
                reply = [*reply, *self._close(dialog[0])]
            return reply
        return super()._event(ev)


async def test_read_back_failures() -> None:
    """A change the iTM did not take raises after the fresh read."""
    itm = _Unchanged()
    itm.limits.append(Group("A"))
    itm.slides.append(Group("S"))
    itm.interlocks.append(Interlock("P"))
    c = FakeClient(itm)
    unit = itm.points[0]
    for call, match in (
        (lambda: c.create_temperature_limit("B"), "did not add"),
        (lambda: c.set_temperature_limit("A", lower=17), "did not take the lower"),
        (lambda: c.set_temperature_limit("A", new_name="C"), "is gone"),
        (lambda: c.set_temperature_limit_members("A", [unit]), "did not take"),
        (lambda: c.set_temperature_limit_enabled("A", enabled=True), "did not"),
        (lambda: c.delete_temperature_limit("A"), "did not delete"),
        (lambda: c.create_sliding_temperature("T"), "did not add"),
        (lambda: c.set_sliding_temperature("S", new_name="T"), "is gone"),
        (
            lambda: c.set_sliding_temperature(
                "S", outdoor_point="Return sensor", outdoor_range=(1, 2)
            ),
            "outdoor point, outdoor range",
        ),
        (lambda: c.set_sliding_temperature_members("S", [unit]), "did not take"),
        (lambda: c.set_sliding_temperature_enabled("S", enabled=True), "did not"),
        (lambda: c.delete_sliding_temperature("S"), "did not delete"),
        (lambda: c.set_heating_optimisation([unit], enabled=True), "did not take"),
        (lambda: c.create_interlock("Q"), "did not take"),
        (lambda: c.rename_interlock("P", "Q"), "did not take"),
        (lambda: c.set_interlock_enabled("P", enabled=True), "did not enable"),
        (lambda: c.delete_interlock("P"), "did not delete"),
        (
            lambda: c.set_interlock_setup(
                "P",
                inputs=[unit],
                outputs=[
                    InterlockOutputSpec(condition="Off", outputs=(unit,), interval=5)
                ],
            ),
            "inputs, outputs 1, condition 1, interval 1",
        ),
    ):
        with pytest.raises(ItmCommandError, match=match):
            await call()
    assert itm.open_screens == set()


class _Broken(FakeDealerITM):
    """An iTM that ignores some buttons, refuses others or shows no radio state."""

    def __init__(self) -> None:
        """Start with nothing broken."""
        super().__init__()
        self.ignored: set[tuple[int, int]] = set()
        self.refused: set[tuple[int, int]] = set()
        self.no_check_state = False
        self.cancel_asks: set[int] = set()

    def _event(self, ev: dict[str, Any]) -> Any:
        key = (ev["screen"], ev["part"])
        if key in self.ignored and ev["screen"] in self.open_screens:
            return [("UPDATE", ev["screen"], [], [])]
        if key in self.refused and ev["screen"] in self.open_screens:
            return [("DLG_OPEN", key[0], [70, 1, "Niet mogelijk", "Error", "OK"], [])]
        if (
            ev["part"] == 12
            and ev["screen"] in self.cancel_asks
            and self.dl_dialog is None
        ):
            return self._ask(_confirm(ev["screen"]), lambda _a: self._close(901))
        reply = super()._event(ev)
        if self.no_check_state:
            reply = [
                (u[0], u[1], u[2], [p for p in u[3] if p[1] != "CHECK_STATE"])
                for u in reply
            ]
        return reply


async def test_screens_that_do_not_react() -> None:
    """A screen or sub-screen that doesn't open, a refusal: errors, all left."""
    itm = _Broken()
    itm.limits.append(Group("A"))
    itm.slides.append(Group("S"))
    itm.interlocks.append(Interlock("P"))
    c = FakeClient(itm)
    unit = itm.points[0]
    itm.optional_available[600] = False
    with pytest.raises(ItmCommandError, match="screen did not open"):
        await c.fetch_heating_optimisation()
    itm.ignored = {(400, 10), (400, 8), (502, 6), (901, 2), (500, 11)}
    with pytest.raises(ItmCommandError, match="screen 401 did not open"):
        await c.set_temperature_limit_members("A", [unit])
    with pytest.raises(ItmCommandError, match="no name input opened"):
        await c.create_temperature_limit("B")
    with pytest.raises(ItmCommandError, match="screen 502 did not open"):
        await c.fetch_sliding_temperature_inputs("S")
    with pytest.raises(ItmCommandError, match="interlock input screen did not open"):
        await c.set_interlock_setup("P", inputs=[unit])
    itm.ignored = {(900, 10)}
    with pytest.raises(ItmCommandError, match="setup screen did not open"):
        await c.fetch_interlock_setup("P")
    itm.ignored = {(902, 13)}
    with pytest.raises(ItmCommandError, match="the screen did not close"):
        await c.set_interlock_setup("P", inputs=[unit])
    itm.ignored = {(902, 6)}
    with pytest.raises(ItmCommandError, match="the list would be"):
        await c.set_interlock_setup("P", inputs=[unit])
    itm.interlock("P").inputs = [unit]
    itm.ignored = {(902, 7)}
    with pytest.raises(ItmCommandError, match="did not remove"):
        await c.set_interlock_setup("P", inputs=[])
    itm.ignored = {(401, 4)}
    with pytest.raises(ItmCommandError, match="the members would be"):
        await c.set_temperature_limit_members("A", [unit])
    itm.ignored = set()
    itm.refused = {(900, 10)}
    with pytest.raises(ItmCommandError, match="refused to open the setup"):
        await c.fetch_interlock_setup("P")
    assert writes(itm) == []
    assert itm.open_screens == set()


async def test_without_radio_state() -> None:
    """Without the radios' state the list's activation text decides."""
    itm = _Broken()
    itm.no_check_state = True
    itm.limits.append(Group("A", enabled=True))
    itm.interlocks.append(Interlock("P", enabled=True))
    itm.hmo[itm.points[0]] = True
    itm.cancel_asks = {901}
    c = FakeClient(itm)
    assert [p.enabled for p in await c.fetch_interlocks()] == [True]
    assert await run(itm, df.set_interlock_enabled, "P", enabled=True) is True
    assert await run(itm, df.set_group_enabled, df.TEMP_LIMIT, "A", enabled=True)
    assert await run(itm, df.set_heating_optimisation, (itm.points[0],), enabled=True)
    setup = await c.fetch_interlock_setup("P")  # its Cancel asks: answered Yes
    assert setup.inputs == ()
    assert itm.interlock("P").enabled  # the confirms opened and discarded only
    assert itm.limit("A").enabled
    assert itm.open_screens == set()


async def test_interval_only(itm: FakeDealerITM) -> None:
    """An output tab's interval alone."""
    itm.interlocks.append(Interlock("P"))
    setup = await FakeClient(itm).set_interlock_setup(
        "P", outputs=[InterlockOutputSpec(interval=30)]
    )
    assert setup is not None
    assert setup.outputs[0].interval == "30 min"


# -- interlock conditions (903), management points (904), actions (906) -----------


async def test_interlock_conditions_and_actions(itm: FakeDealerITM) -> None:
    """Inputs with their conditions, outputs with their actions; read back."""
    itm.interlocks.append(Interlock("Program A"))
    c = FakeClient(itm)
    a, b, x = itm.points[:3]
    setup = await c.set_interlock_setup(
        "Program A",
        inputs=[a, b],
        conditions=[
            InterlockConditionSpec(a, state="error"),
            InterlockConditionSpec(
                b,
                mode="heat",
                comparison="<",
                reference_point="Return sensor",
                offset=1.5,
                hysteresis=0.5,  # its button is hidden, not disabled
            ),
        ],
        outputs=[
            None,
            InterlockOutputSpec(
                outputs=(x,),
                actions=(
                    InterlockActionSpec(
                        x,
                        power=PowerState.SETBACK_HIGH,  # hidden, not disabled
                        mode="Cool",
                        setpoint=23.5,
                        lock_start_stop=RemoteLock.STOP_ONLY,
                        lock_mode=RemoteLock.FORBIDDEN,
                    ),
                ),
            ),
        ],
    )
    assert setup is not None
    first, second = setup.conditions
    assert (first.point, first.state, first.state_options) == (
        ("Input", a),
        "Error",
        ("On", "Error"),
    )
    assert first.tabs == ("On/Off", "Operation Mode", "Analog")
    assert (first.constant, first.reference_point, first.reference_points) == (
        "20.0",
        None,
        (),
    )
    assert (second.mode, second.comparison, second.value) == ("Heat", "<", b)
    assert (second.constant, second.reference_point) == (None, "Return sensor")
    assert second.reference_points == (
        "Analog Outdoor sensor",
        "Analog Return sensor",
    )
    assert (second.offset, second.hysteresis) == ("1.5", "0.5")
    assert second.multistate == "State 1"
    (action,) = setup.outputs[1].actions
    assert action.point == ("Output", x)
    assert action.power is PowerState.SETBACK_HIGH
    assert (action.mode, action.setpoint, action.fan_speed) == ("Cool", 23.5, None)
    assert (action.lock_start_stop, action.lock_mode, action.lock_setpoint) == (
        RemoteLock.STOP_ONLY,
        RemoteLock.FORBIDDEN,
        None,
    )
    assert setup.outputs[0].actions == ()
    program = itm.interlock("Program A")
    assert program.condition_of[b]["point"] == "Return sensor"
    # Change only a condition and an action: the inputs and outputs stay.
    setup = await c.set_interlock_setup(
        "Program A",
        conditions=[InterlockConditionSpec(b, constant=25, comparison=">")],
        outputs=[
            None,
            InterlockOutputSpec(
                actions=(InterlockActionSpec(x, power=PowerState.OFF),)
            ),
        ],
    )
    assert setup is not None
    assert (setup.conditions[1].constant, setup.conditions[1].reference_point) == (
        "25.0",
        None,
    )
    assert setup.conditions[1].comparison == ">"
    assert setup.outputs[1].actions[0].power is PowerState.OFF
    assert setup.outputs[1].actions[0].mode == "Cool"
    assert itm.interlock("Program A").inputs == [a, b]
    assert writes(itm) == [(901, 60), (901, 60)]
    assert itm.open_screens == set()


async def test_interlock_condition_refusals(itm: FakeDealerITM) -> None:
    """Labels, options and points not offered; disabled parts; nothing saved."""
    a, x = itm.points[0], itm.points[2]
    itm.interlocks.append(Interlock("P", inputs=[a], outputs=[[x], []]))
    c = FakeClient(itm)
    for spec, match in (
        (InterlockConditionSpec(a, state="Nooit"), "'Nooit' is not offered"),
        (InterlockConditionSpec(a, mode="dry"), "'dry' is not offered"),
        (InterlockConditionSpec(a, comparison="="), "is not offered"),
        (InterlockConditionSpec(a, reference_point="Nowhere"), "not found once"),
        (InterlockConditionSpec(a, multistate="State 2"), "the button disabled"),
        (InterlockConditionSpec("Nowhere", state="On"), "interlock input 'Nowhere'"),
    ):
        with pytest.raises(ItmCommandError, match=match):
            await c.set_interlock_setup("P", conditions=[spec])
    for action, match in (
        (InterlockActionSpec(x, power=PowerState.SETBACK_LOW), "the button disabled"),
        (InterlockActionSpec(x, mode="Dry"), "is not offered"),
        (InterlockActionSpec("Nowhere", power=PowerState.ON), "interlock output"),
    ):
        with pytest.raises(ItmCommandError, match=match):
            await c.set_interlock_setup(
                "P", outputs=[InterlockOutputSpec(actions=(action,))]
            )
    with pytest.raises(ValueError, match="not both"):
        InterlockConditionSpec(a, constant=1, reference_point="x")
    with pytest.raises(ValueError, match="power must be one of"):
        InterlockActionSpec(x, power=PowerState.SETBACK)
    with pytest.raises(ValueError, match="a lock must be one of"):
        InterlockActionSpec(x, lock_mode=RemoteLock.STOP_ONLY)
    assert InterlockConditionSpec(a).analog is False
    assert writes(itm) == []
    assert itm.interlock("P").condition_of == {}
    assert itm.open_screens == set()


async def test_interlock_condition_dry_run_and_read_only(itm: FakeDealerITM) -> None:
    """A dry run plans only the setup's OK; read only refuses before sending."""
    a, x = itm.points[0], itm.points[2]
    itm.interlocks.append(Interlock("P", inputs=[a], outputs=[[x], []]))
    conditions = [InterlockConditionSpec(a, state="Error", constant=30)]
    outputs = [
        InterlockOutputSpec(actions=(InterlockActionSpec(x, setpoint=21),)),
    ]
    dry = FakeClient(itm, dry_run=True)
    assert (
        await dry.set_interlock_setup("P", conditions=conditions, outputs=outputs)
        is None
    )
    assert dry.planned == ["save interlock program P: screen 901 part 11 event 1"]
    ro = FakeClient(itm, read_only=True)
    sent = len(itm.requests)
    with pytest.raises(ReadOnlyError):
        await ro.set_interlock_setup("P", conditions=conditions, outputs=outputs)
    assert len(itm.requests) == sent
    with pytest.raises(ReadOnlyError):  # the session flow refuses before its clicks
        await run(
            itm, df.set_interlock_setup, "P", conditions=tuple(conditions), client=ro
        )
    assert not any(900 <= r["screen"] <= 906 for r in itm.requests[sent:])
    assert writes(itm) == []
    assert itm.interlock("P").condition_of == {}
    assert itm.interlock("P").action_of == {}
    assert itm.open_screens == set()


class _NoSetupButtons(FakeDealerITM):
    """An iTM that keeps the condition and action buttons disabled."""

    def _picker_event(self, ev: dict[str, Any]) -> Any:
        reply = super()._picker_event(ev)
        screen = ev["screen"]
        if screen in self.open_screens:
            part = 4 if screen == 902 else 6
            reply = [
                *reply,
                ("UPDATE", screen, [], self._enable(screen, {part: False})),
            ]
        return reply


async def test_disabled_setup_buttons() -> None:
    """A disabled condition/action button: left out of a read, refused for a write."""
    itm = _NoSetupButtons()
    a, x = itm.points[0], itm.points[2]
    itm.interlocks.append(Interlock("P", inputs=[a], outputs=[[x], []]))
    c = FakeClient(itm)
    setup = await c.fetch_interlock_setup("P", details=True)
    assert setup.conditions == ()
    assert setup.outputs[0].events == (("Output", x),)
    assert setup.outputs[0].actions == ()
    with pytest.raises(ItmCommandError, match="the button disabled"):
        await c.set_interlock_setup(
            "P", conditions=[InterlockConditionSpec(a, state="On")]
        )
    with pytest.raises(ItmCommandError, match="the button disabled"):
        await c.set_interlock_setup(
            "P",
            outputs=[
                InterlockOutputSpec(actions=(InterlockActionSpec(x, setpoint=20),))
            ],
        )
    assert writes(itm) == []
    assert itm.open_screens == set()


async def test_condition_and_action_read_back_failures() -> None:
    """Conditions and actions the iTM did not keep raise after the fresh read."""
    itm = _Unchanged()
    a, x = itm.points[0], itm.points[2]
    itm.interlocks.append(Interlock("P", inputs=[a], outputs=[[x], []]))
    with pytest.raises(ItmCommandError, match=f"condition of {a}, action of {x}"):
        await FakeClient(itm).set_interlock_setup(
            "P",
            conditions=[InterlockConditionSpec(a, state="Error", hysteresis=2)],
            outputs=[
                InterlockOutputSpec(
                    actions=(
                        InterlockActionSpec(x, lock_setpoint=RemoteLock.FORBIDDEN),
                    )
                )
            ],
        )
    assert itm.open_screens == set()


async def test_condition_screens_that_do_not_open() -> None:
    """903, 904 and 906 that don't open: errors, every screen left."""
    itm = _Broken()
    a, x = itm.points[0], itm.points[2]
    itm.interlocks.append(Interlock("P", inputs=[a], outputs=[[x], []]))
    c = FakeClient(itm)
    calls: tuple[tuple[tuple[int, int], Callable[[], Awaitable[Any]]], ...] = (
        ((902, 4), lambda: c.fetch_interlock_setup("P", details=True)),
        (
            (903, 11),
            lambda: c.set_interlock_setup(
                "P",
                conditions=[InterlockConditionSpec(a, reference_point="Return sensor")],
            ),
        ),
        (
            (905, 6),
            lambda: c.set_interlock_setup(
                "P",
                outputs=[
                    InterlockOutputSpec(actions=(InterlockActionSpec(x, setpoint=20),))
                ],
            ),
        ),
        (
            (904, 2),
            lambda: c.set_interlock_setup(
                "P",
                conditions=[InterlockConditionSpec(a, reference_point="Return sensor")],
            ),
        ),
    )
    for ignored, call in calls:
        itm.ignored = {ignored}
        with pytest.raises(ItmCommandError, match=r"did not open|did not close"):
            await call()
    assert writes(itm) == []
    assert itm.open_screens == set()


def test_condition_and_action_parsing() -> None:
    """Unchecked radios read as unknown; unticked settings as not part of it."""
    state = UIState()
    state.screens[906] = {
        (2, "CHECK_STATE"): 1,
        (7, "CHECK_STATE"): 1,
        (28, "CHECK_STATE"): 1,
        (31, "ICONID"): 3015,
    }
    action = df.interlock_action_from_screen(state, ("Output", "X"))
    assert action.power is None
    assert action.lock_start_stop is RemoteLock.UNKNOWN
    assert (action.lock_mode, action.mode, action.setpoint) == (None, None, None)
    assert action.fan_speed == "auto"
    state.screens[903] = {(27, "TAB_NAME"): ["A", "B"]}
    condition = df.interlock_condition_from_screen(state, ("Input", "Y"))
    assert condition.tabs == ("A", "B")
    assert (condition.state, condition.comparison, condition.constant) == (None,) * 3
    assert default_condition()["state"] == 28
    assert default_action()["ticked"][2] == 0


# -- the CLI --------------------------------------------------------------------------


def cli(*args: str) -> None:
    """Run the CLI against the simulator of CliClient."""
    itm_cli.main(["--host", "fake-host", "--experimental", *args])


@pytest.fixture
def cli_itm(
    itm: FakeDealerITM, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> FakeDealerITM:
    """Route the CLI to the simulator."""
    monkeypatch.setattr(CliClient, "itm", itm, raising=False)
    monkeypatch.setattr(CliClient, "sigterm_at", None, raising=False)
    monkeypatch.setattr(itm_cli, "ItmClient", CliClient)
    monkeypatch.setattr(itm_cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("ITM_USERNAME", "user")
    monkeypatch.setenv("ITM_PASSWORD", "secret")
    return itm


def test_cli_show_empty(
    cli_itm: FakeDealerITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each sub-command shows by default."""
    assert cli_itm.limits == []
    cli("templimit")
    cli("slidetemp")
    cli("interlock")
    assert capsys.readouterr().out.splitlines() == [
        "no temperature limit groups",
        "no sliding temperature groups",
        "no interlock programs",
    ]


def test_cli_templimit(
    cli_itm: FakeDealerITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """Add, members, set, enable, show, disable, delete (also dry)."""
    a, b = cli_itm.points[:2]
    cli("templimit", "add", "Annex", "--add", a, "--lower", "18", "--upper", "26")
    assert "Annex" in capsys.readouterr().out
    cli("templimit", "members", "Annex", "--add", b, "--remove", a)
    cli("templimit", "set", "Annex", "--upper", "25")
    cli("templimit", "enable", "Annex")
    cli("templimit", "show", "--details")
    last = capsys.readouterr().out.splitlines()[-1].split()
    assert last == ["Annex", "enabled", "18-25", "°C", "members", b]
    cli("templimit", "disable", "Annex")
    cli("--dry-run", "templimit", "delete", "Annex")
    assert "delete temperature limit group Annex" in capsys.readouterr().out
    cli("templimit", "delete", "Annex")
    assert capsys.readouterr().out.splitlines()[-1] == "Annex: deleted"
    with pytest.raises(SystemExit, match="no temperature limit group"):
        cli("templimit", "members", "Annex", "--add", a)
    with pytest.raises(SystemExit, match="the name is needed"):
        cli("templimit", "delete")


def test_cli_slidetemp(
    cli_itm: FakeDealerITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """Add, inputs, set, members, enable, disable, delete."""
    a = cli_itm.points[0]
    cli("slidetemp", "add", "Hall", "--add", a)
    cli("slidetemp", "inputs", "Hall")
    assert "AI Outdoor sensor" in capsys.readouterr().out
    cli(
        "slidetemp",
        "set",
        "Hall",
        "--outdoor-point",
        "Outdoor sensor",
        "--outdoor-range",
        "0",
        "30",
        "--setpoint-range",
        "21",
        "25",
    )
    assert "outdoor 0-30 °C -> setpoint 21-25 °C" in capsys.readouterr().out
    cli("slidetemp", "members", "Hall", "--remove", a)
    cli("slidetemp", "enable", "Hall")
    cli("slidetemp", "disable", "Hall")
    cli("slidetemp", "delete", "Hall")
    assert cli_itm.slides == []
    with pytest.raises(SystemExit, match="no sliding temperature group"):
        cli("slidetemp", "members", "Hall")


def test_cli_heatopt(
    cli_itm: FakeDealerITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """On for a unit, a dry run for all, show."""
    a, b = cli_itm.points[:2]
    cli("heatopt", "on", a)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [a, "on"]
    assert lines[1].split() == [b, "off"]
    cli("--dry-run", "heatopt", "off")
    assert "heating mode optimisation off for all units" in capsys.readouterr().out
    cli("heatopt")
    assert capsys.readouterr().out.splitlines()[0].split() == [a, "on"]


def test_cli_interlock(
    cli_itm: FakeDealerITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """Add, copy, rename, delete, set, enable, setup, show; refusals."""
    a, b = cli_itm.points[:2]
    cli("interlock", "add", "Program A")
    cli("interlock", "copy", "Program A", "Program B")
    cli("interlock", "rename", "Program B", "Program C")
    cli("interlock", "delete", "Program C")
    assert capsys.readouterr().out.splitlines()[-1] == "Program C: delete done"
    cli(
        "interlock",
        "set",
        "Program A",
        "--input",
        a,
        "--output2",
        b,
        "--condition2",
        "Off",
        "--interval2",
        "10",
    )
    out = capsys.readouterr().out
    assert f"inputs: Input {a}" in out
    assert f"outputs: Output {b}" in out
    cli("interlock", "enable", "Program A")
    cli("interlock", "setup", "Program A", "--details")
    out = capsys.readouterr().out
    assert "input conditions: At on, At error" in out
    assert f"events: Output {b}" in out
    cli("interlock")
    assert capsys.readouterr().out.split() == ["Program", "A", "enabled"]
    with pytest.raises(SystemExit, match="error: the client is read-only"):
        cli("--read-only", "interlock", "add", "Program D")
    with pytest.raises(SystemExit, match="the name is needed"):
        cli("interlock", "delete")
    with pytest.raises(SystemExit, match="the new name is needed"):
        cli("interlock", "rename", "Program A")


def test_cli_interlock_conditions_and_actions(
    cli_itm: FakeDealerITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """Conditions and actions through `interlock set`; shown by `setup --details`."""
    a, x = cli_itm.points[0], cli_itm.points[2]
    cli_itm.interlocks.append(Interlock("P", inputs=[a], outputs=[[x], []]))
    cli(
        "interlock",
        "set",
        "P",
        "--input-condition",
        a,
        "state=Error",
        "point=Outdoor sensor",
        "offset=0,5",
        "--action1",
        x,
        "power=on",
        "setpoint=22",
        "lock-mode=forbidden",
    )
    out = capsys.readouterr().out
    assert f"condition of Input {a}: state Error, mode Cool, value {a}" in out
    assert "point Outdoor sensor, offset 0.5" in out
    assert "points offered: Analog Outdoor sensor, Analog Return sensor" in out
    assert f"action of Output {x}: power on, setpoint 22, lock mode forbidden" in out
    cli("--dry-run", "interlock", "set", "P", "--action1", x, "mode=heat")
    assert "save interlock program P" in capsys.readouterr().out
    for args, match in (
        (["--input-condition", a, "colour=red"], "KEY=VALUE with KEY one of"),
        (["--input-condition", a, "constant=warm"], "a number is needed"),
        (["--input-condition", a, "constant=1", "point=x"], "not both"),
        (["--action1", x, "power=maybe"], "action of"),
    ):
        with pytest.raises(SystemExit, match=match):
            cli("interlock", "set", "P", *args)
    cli_itm.interlocks.append(Interlock("Q"))
    cli("interlock", "setup", "Q", "--details")
    assert "condition of" not in capsys.readouterr().out
