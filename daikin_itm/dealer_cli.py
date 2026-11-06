"""The ``itm`` sub-commands for the dealer-option functions.

``itm templimit``, ``itm slidetemp``, ``itm heatopt`` and ``itm interlock``: show by
default, change with an action. Kept apart from ``cli.py``, which adds them with
``add_dealer_commands`` and runs them from ``DEALER_COMMANDS``.
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from .cli_common import echo as _out
from .cli_common import print_plan as _print_plan
from .interlock import (
    InterlockActionSpec,
    InterlockConditionSpec,
    InterlockOutputSpec,
)
from .models import PowerState, RemoteLock

if TYPE_CHECKING:
    import argparse
    from collections.abc import Awaitable, Callable

    from .client import ItmClient
    from .dealer_functions import SlidingTemperatureGroup, TemperatureLimitGroup
    from .interlock import InterlockAction, InterlockCondition, InterlockSetup

GROUP_ACTIONS = ["show", "enable", "disable", "add", "members", "set", "delete"]
CONDITION_KEYS = (
    "state",
    "mode",
    "compare",
    "constant",
    "point",
    "offset",
    "hysteresis",
    "multistate",
)
ACTION_KEYS = (
    "power",
    "mode",
    "setpoint",
    "lock-start-stop",
    "lock-mode",
    "lock-setpoint",
)


def _num(value: float | None) -> str:
    return "?" if value is None else f"{value:g}"


def _state(enabled: bool) -> str:  # noqa: FBT001 - a plain formatter
    return "enabled" if enabled else "disabled"


def _members(current: tuple[str, ...], args: argparse.Namespace) -> tuple[str, ...]:
    return tuple(m for m in current if m not in args.remove) + tuple(args.add)


def _print_limits(groups: list[TemperatureLimitGroup]) -> None:
    if not groups:
        _out("no temperature limit groups")
    for g in groups:
        members = f"  members {', '.join(g.members) or '-'}" if g.members else ""
        _out(
            f"{g.name:24} {_state(g.enabled):9} "
            f"{_num(g.lower)}-{_num(g.upper)} °C{members}"
        )


def _print_slides(groups: list[SlidingTemperatureGroup]) -> None:
    if not groups:
        _out("no sliding temperature groups")
    for g in groups:
        detail = ""
        if g.outdoor_range is not None and g.setpoint_range is not None:
            detail = (
                f"  outdoor {_num(g.outdoor_range[0])}-{_num(g.outdoor_range[1])} °C"
                f" -> setpoint {_num(g.setpoint_range[0])}-"
                f"{_num(g.setpoint_range[1])} °C, members "
                f"{', '.join(g.members) or '-'}"
            )
        _out(
            f"{g.name:24} {_state(g.enabled):9} point {g.outdoor_point or '-'}{detail}"
        )


async def _templimit(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the temperature limit groups."""
    if args.action == "show":
        _print_limits(await client.fetch_temperature_limits(details=args.details))
        return
    name = _needs_name(args)
    result: TemperatureLimitGroup | None = None
    if args.action in {"enable", "disable"}:
        result = await client.set_temperature_limit_enabled(
            name, enabled=args.action == "enable"
        )
    elif args.action == "add":
        result = await client.create_temperature_limit(
            name, members=args.add, lower=args.lower, upper=args.upper
        )
    elif args.action == "members":
        groups = await client.fetch_temperature_limits(details=True)
        current = next((g for g in groups if g.name == name), None)
        if current is None:
            sys.exit(f"no temperature limit group {name!r}")
        result = await client.set_temperature_limit_members(
            name, _members(current.members, args)
        )
    elif args.action == "set":
        result = await client.set_temperature_limit(
            name, new_name=args.new_name, lower=args.lower, upper=args.upper
        )
    else:
        await client.delete_temperature_limit(name)
        if not client.dry_run:
            _out(f"{name}: deleted")
    if not _print_plan(client) and result is not None:
        _print_limits([result])


async def _slidetemp(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the sliding temperature groups."""
    if args.action == "show":
        _print_slides(await client.fetch_sliding_temperatures(details=args.details))
        return
    name = _needs_name(args)
    result: SlidingTemperatureGroup | None = None
    if args.action == "inputs":
        for row in await client.fetch_sliding_temperature_inputs(name):
            _out(row)
        return
    if args.action in {"enable", "disable"}:
        result = await client.set_sliding_temperature_enabled(
            name, enabled=args.action == "enable"
        )
    elif args.action == "add":
        result = await client.create_sliding_temperature(name, members=args.add)
    elif args.action == "members":
        groups = await client.fetch_sliding_temperatures(details=True)
        current = next((g for g in groups if g.name == name), None)
        if current is None:
            sys.exit(f"no sliding temperature group {name!r}")
        result = await client.set_sliding_temperature_members(
            name, _members(current.members, args)
        )
    elif args.action == "set":
        result = await client.set_sliding_temperature(
            name,
            new_name=args.new_name,
            outdoor_point=args.outdoor_point,
            outdoor_range=tuple(args.outdoor_range) if args.outdoor_range else None,
            setpoint_range=tuple(args.setpoint_range) if args.setpoint_range else None,
        )
    else:
        await client.delete_sliding_temperature(name)
        if not client.dry_run:
            _out(f"{name}: deleted")
    if not _print_plan(client) and result is not None:
        _print_slides([result])


async def _heatopt(client: ItmClient, args: argparse.Namespace) -> None:
    """Show heating mode optimisation per unit, or switch it on or off."""
    if args.action == "show":
        units = await client.fetch_heating_optimisation()
    else:
        result = await client.set_heating_optimisation(
            args.units or None, enabled=args.action == "on"
        )
        if _print_plan(client) or result is None:
            return
        units = result
    for u in units:
        _out(f"{u.name:24} {'on' if u.enabled else 'off'}")


def _shown(pairs: tuple[tuple[str, object], ...]) -> str:
    return ", ".join(f"{k} {v}" for k, v in pairs if v is not None) or "nothing set"


def _print_condition(c: InterlockCondition) -> None:
    _out(
        f"  condition of {' '.join(c.point)}: "
        + _shown(
            (
                ("state", c.state),
                ("mode", c.mode),
                ("value", c.value),
                ("compare", c.comparison),
                ("constant", c.constant),
                ("point", c.reference_point),
                ("offset", c.offset),
                ("hysteresis", c.hysteresis),
                ("multistate", c.multistate),
            )
        )
    )
    if c.reference_points:
        _out(f"    points offered: {', '.join(c.reference_points)}")


def _print_action(a: InterlockAction) -> None:
    _out(
        f"    action of {' '.join(a.point)}: "
        + _shown(
            (
                ("power", a.power),
                ("mode", a.mode),
                ("setpoint", None if a.setpoint is None else _num(a.setpoint)),
                ("fan", a.fan_speed),
                ("lock start/stop", a.lock_start_stop),
                ("lock mode", a.lock_mode),
                ("lock setpoint", a.lock_setpoint),
            )
        )
    )


def _print_setup(setup: InterlockSetup) -> None:
    _out(f"{setup.name}:")
    _out("  inputs: " + ("; ".join(" ".join(r) for r in setup.inputs) or "none"))
    if setup.input_conditions:
        _out(f"  input conditions: {', '.join(setup.input_conditions)}")
    for c in setup.conditions:
        _print_condition(c)
    for i, o in enumerate(setup.outputs, 1):
        _out(
            f"  output {i}: condition {o.condition} "
            f"(options: {', '.join(o.condition_options)}), interval {o.interval}"
        )
        _out("    outputs: " + ("; ".join(" ".join(r) for r in o.outputs) or "none"))
        if o.events:
            _out("    events: " + "; ".join(" ".join(r) for r in o.events))
        for a in o.actions:
            _print_action(a)


def _pairs(values: list[str], keys: tuple[str, ...], what: str) -> dict[str, str]:
    """Parse KEY=VALUE arguments; exit on an unknown key."""
    out = {}
    for value in values:
        key, sep, text = value.partition("=")
        if not sep or key not in keys:
            sys.exit(f"{what}: KEY=VALUE with KEY one of {', '.join(keys)}: {value!r}")
        out[key] = text
    return out


def _number(text: str | None, what: str) -> float | None:
    if text is None:
        return None
    try:
        return float(text.replace(",", "."))
    except ValueError:
        sys.exit(f"{what}: a number is needed, not {text!r}")


def _condition_specs(args: argparse.Namespace) -> tuple[InterlockConditionSpec, ...]:
    specs = []
    for point, *values in args.input_condition or []:
        d = _pairs(values, CONDITION_KEYS, f"--input-condition {point}")
        try:
            specs.append(
                InterlockConditionSpec(
                    point=point,
                    state=d.get("state"),
                    mode=d.get("mode"),
                    comparison=d.get("compare"),
                    constant=_number(d.get("constant"), "constant"),
                    reference_point=d.get("point"),
                    offset=_number(d.get("offset"), "offset"),
                    hysteresis=_number(d.get("hysteresis"), "hysteresis"),
                    multistate=d.get("multistate"),
                )
            )
        except ValueError as err:
            sys.exit(f"--input-condition {point}: {err}")
    return tuple(specs)


def _action_specs(given: list[list[str]] | None) -> tuple[InterlockActionSpec, ...]:
    specs = []
    for point, *values in given or []:
        what = f"action of {point}"
        d = _pairs(values, ACTION_KEYS, what)
        try:
            locks = {
                key: None if d.get(key) is None else RemoteLock(d[key])
                for key in ("lock-start-stop", "lock-mode", "lock-setpoint")
            }
            specs.append(
                InterlockActionSpec(
                    point=point,
                    power=None if d.get("power") is None else PowerState(d["power"]),
                    mode=d.get("mode"),
                    setpoint=_number(d.get("setpoint"), "setpoint"),
                    lock_start_stop=locks["lock-start-stop"],
                    lock_mode=locks["lock-mode"],
                    lock_setpoint=locks["lock-setpoint"],
                )
            )
        except ValueError as err:
            sys.exit(f"{what}: {err}")
    return tuple(specs)


def _output_specs(args: argparse.Namespace) -> tuple[InterlockOutputSpec | None, ...]:
    specs: list[InterlockOutputSpec | None] = []
    for i in (1, 2):
        outputs = getattr(args, f"output{i}")
        condition = getattr(args, f"condition{i}")
        interval = getattr(args, f"interval{i}")
        actions = _action_specs(getattr(args, f"action{i}"))
        if outputs is None and condition is None and interval is None and not actions:
            specs.append(None)
        else:
            specs.append(
                InterlockOutputSpec(
                    condition=condition,
                    outputs=None if outputs is None else tuple(outputs),
                    interval=interval,
                    actions=actions,
                )
            )
    return tuple(specs)


async def _interlock(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the interlock programs."""
    action = args.action
    if action == "show":
        programs = await client.fetch_interlocks()
        if not programs:
            _out("no interlock programs")
        for p in programs:
            _out(f"{p.name:24} {_state(p.enabled)}")
        return
    name = _needs_name(args)
    if action in {"rename", "copy"} and not args.new_name:
        sys.exit("the new name is needed")
    if action == "setup":
        _print_setup(await client.fetch_interlock_setup(name, details=args.details))
        return
    if action == "set":
        setup = await client.set_interlock_setup(
            name,
            inputs=args.input,
            outputs=_output_specs(args),
            conditions=_condition_specs(args),
        )
        if not _print_plan(client) and setup is not None:
            _print_setup(setup)
        return
    calls: dict[str, Callable[[], Awaitable[Any]]] = {
        "enable": lambda: client.set_interlock_enabled(name, enabled=True),
        "disable": lambda: client.set_interlock_enabled(name, enabled=False),
        "add": lambda: client.create_interlock(name),
        "rename": lambda: client.rename_interlock(name, args.new_name),
        "copy": lambda: client.copy_interlock(name, args.new_name),
        "delete": lambda: client.delete_interlock(name),
    }
    await calls[action]()
    if not _print_plan(client):
        _out(f"{name}: {action} done")


def _needs_name(args: argparse.Namespace) -> str:
    if not args.name:
        sys.exit("the name is needed")
    return str(args.name)


def _group_parser(sub: Any, command: str, help_text: str, actions: list[str]) -> Any:
    p = sub.add_parser(command, help=help_text)
    p.add_argument("action", nargs="?", default="show", choices=actions)
    p.add_argument("name", nargs="?", help="the group")
    p.add_argument("--details", action="store_true", help="members and settings too")
    p.add_argument("--add", action="append", default=[], help="a unit to add")
    p.add_argument("--remove", action="append", default=[], help="a unit to remove")
    p.add_argument("--new-name", help="set: rename the group")
    return p


def add_dealer_commands(sub: Any) -> None:
    """Add `templimit`, `slidetemp`, `heatopt` and `interlock` to the parser."""
    tl = _group_parser(
        sub, "templimit", "the temperature limit groups (dealer option)", GROUP_ACTIONS
    )
    tl.add_argument("--lower", type=float, help="add/set: the lower limit, °C")
    tl.add_argument("--upper", type=float, help="add/set: the upper limit, °C")
    st = _group_parser(
        sub,
        "slidetemp",
        "the sliding temperature groups (dealer option)",
        [*GROUP_ACTIONS, "inputs"],
    )
    st.add_argument("--outdoor-point", help="set: the analog input, as `inputs` lists")
    for key in ("outdoor-range", "setpoint-range"):
        st.add_argument(
            f"--{key}", nargs=2, type=float, metavar=("FROM", "TO"), help="set: °C"
        )
    ho = sub.add_parser(
        "heatopt", help="heating mode optimisation per unit (dealer option)"
    )
    ho.add_argument("action", nargs="?", default="show", choices=["show", "on", "off"])
    ho.add_argument("units", nargs="*", help="on/off: the units (default: all)")
    il = sub.add_parser("interlock", help="the interlock programs (dealer option)")
    il.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=[
            "show",
            "setup",
            "enable",
            "disable",
            "add",
            "rename",
            "copy",
            "delete",
            "set",
        ],
    )
    il.add_argument("name", nargs="?", help="the program")
    il.add_argument("new_name", nargs="?", help="rename/copy: the new name")
    il.add_argument("--details", action="store_true", help="setup: sub-screens too")
    il.add_argument("--input", action="append", help="set: an input point (repeat)")
    il.add_argument(
        "--input-condition",
        nargs="+",
        action="append",
        metavar=("POINT", "KEY=VALUE"),
        help=f"set: an input's condition; KEY one of {', '.join(CONDITION_KEYS)}",
    )
    for i in (1, 2):
        il.add_argument(
            f"--action{i}",
            nargs="+",
            action="append",
            metavar=("POINT", "KEY=VALUE"),
            help=f"set: a tab {i} output's action; KEY one of {', '.join(ACTION_KEYS)}"
            " (power on/off/setback_high, locks allowed/stop_only/forbidden)",
        )
        il.add_argument(
            f"--output{i}", action="append", help=f"set: a tab {i} output (repeat)"
        )
        il.add_argument(f"--condition{i}", help=f"set: tab {i}'s condition, as shown")
        il.add_argument(f"--interval{i}", type=float, help=f"set: tab {i}'s interval")


DEALER_COMMANDS: dict[str, Callable[[ItmClient, argparse.Namespace], Awaitable[None]]]
DEALER_COMMANDS = {
    "templimit": _templimit,
    "slidetemp": _slidetemp,
    "heatopt": _heatopt,
    "interlock": _interlock,
}
