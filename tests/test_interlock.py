# pylint: disable=protected-access,redefined-outer-name
"""Interlock programs (900-906): list, enable, add, rename, copy, delete, setup."""

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
)
from daikin_itm import interlock as il
from daikin_itm.state import UIState

from .fake_dealer_functions import (
    FakeDealerITM,
    Interlock,
    default_action,
    default_condition,
)
from .test_client import FakeClient
from .test_dealer_functions import (
    _Broken,
    _Unchanged,
    cli,
    cli_itm,
    itm,
    run,
    writes,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["cli_itm", "itm"]


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
        (InterlockConditionSpec(a, multistate="State 2"), "has this disabled"),
        (InterlockConditionSpec("Nowhere", state="On"), "interlock input 'Nowhere'"),
    ):
        with pytest.raises(ItmCommandError, match=match):
            await c.set_interlock_setup("P", conditions=[spec])
    for action, match in (
        (InterlockActionSpec(x, power=PowerState.SETBACK_LOW), "has this disabled"),
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
            itm, il.set_interlock_setup, "P", conditions=tuple(conditions), client=ro
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
    with pytest.raises(ItmCommandError, match="has this disabled"):
        await c.set_interlock_setup(
            "P", conditions=[InterlockConditionSpec(a, state="On")]
        )
    with pytest.raises(ItmCommandError, match="has this disabled"):
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
    action = il.interlock_action_from_screen(state, ("Output", "X"))
    assert action.power is None
    assert action.lock_start_stop is RemoteLock.UNKNOWN
    assert (action.lock_mode, action.mode, action.setpoint) == (None, None, None)
    assert action.fan_speed == "auto"
    state.screens[903] = {(27, "TAB_NAME"): ["A", "B"]}
    condition = il.interlock_condition_from_screen(state, ("Input", "Y"))
    assert condition.tabs == ("A", "B")
    assert (condition.state, condition.comparison, condition.constant) == (None,) * 3
    assert default_condition()["state"] == 28
    assert default_action()["ticked"][2] == 0


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
