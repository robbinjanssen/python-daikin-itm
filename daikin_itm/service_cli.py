"""The ``itm`` service, demand and emergency stop sub-commands."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cli_common import _date_arg, _onoff, _out, _print_plan, _save, _shown

if TYPE_CHECKING:
    import argparse
    from collections.abc import Awaitable, Callable

    from .client import ItmClient
    from .demand import DemandControl
    from .emergency import EmergencyStopSettings
    from .service_edit import LeakageCheckSchedule


async def _emgstop(  # noqa: PLR0912  # pylint: disable=too-many-branches
    client: ItmClient, args: argparse.Namespace
) -> None:
    """Show or change the emergency stop programs; show or release active stops."""
    action, name, new_name = args.action, args.name, args.new_name
    if action == "show":
        for p in await client.fetch_emergency_stop():
            _out(f"{p.name:24} {'enabled' if p.enabled else 'disabled'}")
        return
    if action == "active":
        active = await client.fetch_active_emergency_stops()
        _out("active: " + (", ".join(active) if active else "none"))
        return
    if not name:
        sys.exit("the program's name is needed")
    if action in {"rename", "copy"} and not new_name:
        sys.exit("the new name is needed")
    if action == "settings":
        _print_emergency_settings(await client.fetch_emergency_stop_settings(name))
        return
    if action in {"enable", "disable"}:
        await client.set_emergency_stop_enabled(
            name,
            enabled=action == "enable",
            allow_safety_change=args.allow_safety_change,
        )
    elif action == "add":
        await client.create_emergency_stop_program(name)
    elif action == "rename":
        await client.rename_emergency_stop_program(name, new_name)
    elif action == "copy":
        await client.copy_emergency_stop_program(name, new_name)
    elif action == "delete":
        await client.delete_emergency_stop_program(name)
    elif action == "set":
        settings = await client.set_emergency_stop_settings(
            name,
            inputs=args.input,
            outputs=args.output,
            release_mode=args.release_mode,
            stop=args.stop,
        )
        if not _print_plan(client) and settings is not None:
            _print_emergency_settings(settings)
        return
    else:  # release
        await client.release_emergency_stop(
            name, allow_safety_change=args.allow_safety_change
        )
    if not _print_plan(client):
        _out(f"{name}: {action} done")


def _print_emergency_settings(settings: EmergencyStopSettings) -> None:
    """Print an emergency stop program's settings."""
    _out(f"{settings.name}:")
    _out(f"  inputs: {', '.join(settings.inputs) or 'none'}")
    _out(f"  outputs: {', '.join(settings.outputs) or 'none'}")
    _out(
        f"  release mode: {settings.release_mode} "
        f"(options: {', '.join(settings.release_mode_options)})"
    )
    _out(f"  stop: {settings.stop} (options: {', '.join(settings.stop_options)})")


async def _service(client: ItmClient, args: argparse.Namespace) -> None:
    """Show the installer's service settings (password from ITM_SERVICE_PASSWORD)."""
    if getattr(args, "action", "show") != "show":
        await _service_edit(client, args)
        return
    password = os.environ.get("ITM_SERVICE_PASSWORD")
    if not password:
        sys.exit("set ITM_SERVICE_PASSWORD to the iTM's service password")
    st = await client.fetch_service_settings(password)
    _out(f"time zone: {st.time_zone}")
    for o in st.changeover_options or ():
        _out(f"changeover option {o.address} {o.name}: {o.option}")
    contact = [line for line in st.contact or () if line]
    _out(f"contact: {' / '.join(contact) or 'none'}")
    ports = [port for port in st.r32_output_ports or () if port]
    _out(f"R32 alarm: output ports {ports or 'none'}, buzzer {_onoff(st.r32_buzzer)}")
    _out(
        f"central monitoring: warning as abnormal {_onoff(st.warning_as_abnormal)}, "
        f"dry mode {_onoff(st.dry_mode)}"
    )
    _out(f"activation keys: {st.activation_keys}")
    _out(f"dealer options on: {', '.join(st.dealer_options_enabled or ())}")
    _out(f"dealer options off: {', '.join(st.dealer_options_disabled or ())}")
    _out(f"energy save mode: {st.energy_save_mode}")


async def _service_edit(client: ItmClient, args: argparse.Namespace) -> None:
    """Layout setup and R32 output ports (`itm service ACTION`)."""
    password = _service_password()
    if args.action == "layout":
        setup = await client.fetch_layout_setup(password)
        offered = "?" if setup.offered is None else "yes" if setup.offered else "no"
        _out(f"layout setup: offered {offered}")
    elif args.action == "run-layout":
        await client.run_layout_setup(
            password, allow_service_change=args.allow_service_change
        )
        if not _print_plan(client):
            _out("layout setup: done")
    elif args.action == "r32-ports":
        ports = await client.fetch_r32_output_ports(password)
        for number, port in enumerate(ports.ports, 1):
            _out(f"R32 output port {number}: {port or 'none'}")
        _out(f"offered: {', '.join(ports.offered) or 'none'}")
    else:  # r32-port
        if args.value is None and not args.clear:
            sys.exit("r32-port needs --value or --clear")
        result = await client.set_r32_output_port(
            password,
            args.port,
            None if args.clear else args.value,
            allow_service_change=args.allow_service_change,
        )
        if not _print_plan(client) and result is not None:
            shown = result.ports[args.port - 1] or "none"
            _out(f"R32 output port {args.port}: {shown}")


async def _leakcheck_write(  # pylint: disable=too-many-return-statements
    client: ItmClient, args: argparse.Namespace, password: str
) -> None:
    """Run one of `itm leakcheck`'s writes."""
    outdoor = args.outdoor
    if not outdoor:
        sys.exit("name the outdoor unit (name or address)")
    if args.action in {"start", "stop"}:
        run = (
            client.start_leakage_check
            if args.action == "start"
            else client.stop_leakage_check
        )
        unit = await run(
            password, outdoor, allow_leakage_check=args.allow_leakage_check
        )
        if not _print_plan(client) and unit is not None:
            _out(f"{unit.name}: leakage check {args.action} sent")
        return
    if args.action == "register":
        reg = await client.set_leakage_check_indoor_units(
            password,
            outdoor,
            tuple(args.indoor or ()),
            allow_service_change=args.allow_service_change,
        )
        if not _print_plan(client) and reg is not None:
            _out(f"{outdoor}: registered {', '.join(reg.registered) or 'none'}")
        return
    if args.action == "copy":
        if not args.to:
            sys.exit("copy needs --to (repeat for more outdoor units)")
        await client.copy_leakage_check_schedule(
            password,
            outdoor,
            tuple(args.to),
            allow_service_change=args.allow_service_change,
        )
        if not _print_plan(client):
            _out(f"{outdoor}: schedule copied to {', '.join(args.to)}")
        return
    if args.program is None:
        sys.exit("schedule needs --program 1-4")
    schedule = await client.set_leakage_check_schedule(
        password,
        outdoor,
        args.program,
        activation=args.activation,
        day=_date_arg(args.date),
        at=args.time,
        allow_service_change=args.allow_service_change,
    )
    if not _print_plan(client) and schedule is not None:
        _print_leakage_schedule(schedule)


def _print_leakage_schedule(schedule: LeakageCheckSchedule) -> None:
    """Print an outdoor unit's leakage check schedule."""
    _out(f"  schedule of {schedule.name}:")
    for p in schedule.programs:
        _out(f"    program {p.number}: {p.activation} {p.date} {p.time}")


async def _leakcheck(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the leakage check (service password from the environment)."""
    password = _service_password()
    if args.action in {"show", "details"}:
        lc = await client.fetch_leakage_check(
            password, details=args.action == "details"
        )
        for u in lc.outdoor_units:
            _out(
                f"{u.address:8} {u.name:20} type {u.type}, indoor units "
                f"{u.indoor_units}, charge {u.charge}, schedule {u.schedule or '-'}"
            )
            key = u.name or u.address
            if key in lc.registrations:
                reg = lc.registrations[key].registered
                _out(f"  registered: {', '.join(reg) or 'none'}")
            if key in lc.schedules:
                _print_leakage_schedule(lc.schedules[key])
        if not lc.outdoor_units:
            _out("no outdoor units")
    elif args.action == "results":
        results = await client.fetch_leakage_check_results(password)
        for r in results.results:
            _out(f"{r.outdoor:20} {r.checked:20} {r.amount}")
    elif args.action == "export":
        download = await client.export_leakage_check_results(password)
        out = Path(args.out or download.name)
        await asyncio.to_thread(_save, out, download.data)
        _out(f"{download.name}: {len(download.data)} bytes written to {out}")
    else:
        await _leakcheck_write(client, args, password)


async def _password(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the administrator/screen password (new one: ITM_NEW_PASSWORD)."""
    if args.action == "show":
        st = await client.fetch_password_settings()
        _out(f"administrator password: {_onoff(st.administrator)}")
        _out(f"screen password: {_onoff(st.screen)}")
        return
    if not args.kind:
        sys.exit("which password: administrator or screen")
    new = os.environ.get("ITM_NEW_PASSWORD") or None
    if args.action == "change" and new is None:
        sys.exit("set ITM_NEW_PASSWORD to the new password")
    result = await client.set_password_setting(
        args.kind,
        enabled=None if args.action == "change" else args.action == "enable",
        password=None if args.action == "disable" else new,
        allow_password_change=args.allow_password_change,
    )
    if not _print_plan(client) and result is not None:
        _out(f"{args.kind} password: {_onoff(getattr(result, args.kind))}")


def _service_password() -> str:
    """Return the service password from ITM_SERVICE_PASSWORD, or exit."""
    password = os.environ.get("ITM_SERVICE_PASSWORD")
    if not password:
        sys.exit("set ITM_SERVICE_PASSWORD to the iTM's service password")
    return password


def _pairs(pairs: list[str] | None, what: str) -> dict[int, str]:
    """Parse N=VALUE arguments (a level or port number and an option)."""
    out = {}
    for pair in pairs or []:
        number, sep, value = pair.partition("=")
        if not sep or not number.strip().isdigit():
            sys.exit(f"{what}: expected N=VALUE, not {pair!r}")
        out[int(number)] = value
    return out


def _print_demand(dc: DemandControl) -> None:
    """Print what fetch_demand_control read."""
    st = dc.status
    unit = st.power_unit or ""
    _out(f"state: {st.state}, period: {st.period}, sync: {st.sync_method}")
    _out(
        f"target {_shown(st.target)} {unit}, predicted {_shown(st.predicted)}, "
        f"current {_shown(st.current)}, adjustment {_shown(st.adjustment)}, "
        f"remaining {st.remaining}"
    )
    if dc.setup is not None:
        for c in dc.setup.controls:
            _out(f"control {c.name}: {_onoff(c.enabled)} ({' | '.join(c.cells)})")
        _out(f"forced level: {dc.setup.forced_level}")
    for lv in dc.levels or ():
        levels = ", ".join(f"{i}={v}" for i, v in enumerate(lv.levels, 1))
        _out(
            f"{lv.control} [{lv.kind}] group {lv.group}: start {lv.start_level}"
            + (f", levels {levels}" if levels else "")
            + (
                f", cool <= {_shown(lv.upper_cool)}, heat >= {_shown(lv.lower_heat)}"
                if lv.kind == "temperature"
                else ""
            )
        )
    for g in dc.groups or ():
        _out(
            f"{g.control} group {g.name}: {', '.join(g.members) or 'no members'}; "
            f"condition {g.condition}, resume {g.resume}"
        )
    for tab in dc.control_status or ():
        _out(f"status {tab.name}: {tab.status}, level {tab.level}")
    for graph in dc.graphs or ():
        rows = len(graph.data.rows) if graph.data else 0
        _out(f"graph {graph.name}: {'/'.join(map(str, graph.selection))}, {rows} rows")


async def _demand(  # noqa: PLR0912  # pylint: disable=too-many-branches
    client: ItmClient, args: argparse.Namespace
) -> None:
    """Show or change demand (power limit) control; its service setup."""
    action, rest = args.action, args.values
    result: Any = None
    if action == "show":
        dc = await client.fetch_demand_control(
            args.only.split(",") if args.only else None
        )
        if dc is None:
            _out("demand control: not available (the menu's button opens nothing)")
        else:
            _print_demand(dc)
        return
    if action == "service":
        st = await client.fetch_demand_service_settings(_service_password())
        _out(f"pulse setup: {st.pulse}")
        _out(f"interlock ports: {st.interlock_ports}")
        _out(f"ports offered: {', '.join(st.ports) or 'none'}")
        return
    if action in {"target", "forced-level"} and len(rest) != 1:
        sys.exit(f"demand {action}: one value is needed")
    if action in {"level", "group"} and len(rest) != (1 if action == "level" else 2):
        sys.exit(f"demand {action}: the control (and for group the group) is needed")
    if action == "target":
        result = await client.set_demand_target(float(rest[0]))
    elif action == "forced-level":
        result = await client.set_demand_forced_level(rest[0])
    elif action == "level":
        result = await client.set_demand_level_setup(
            rest[0],
            group=args.group,
            start_level=args.start_level,
            levels=_pairs(args.level, "--level"),
            upper_cool=args.upper_cool,
            lower_heat=args.lower_heat,
        )
    elif action == "group":
        result = await client.set_demand_group(
            rest[0],
            rest[1],
            members=args.member,
            condition=args.condition,
            resume=args.resume,
        )
    elif action == "pulse":
        result = await client.set_demand_pulse_setup(
            _service_password(),
            input_port1=args.port1,
            input_port2=args.port2,
            pulse_coefficient=args.coefficient,
            sync_external=None if args.sync is None else args.sync == "external",
            return_value=args.return_value,
            cutoff_value=args.cutoff,
            allow_service_change=args.allow_service_change,
        )
    else:  # interlock
        result = await client.set_demand_interlock_ports(
            _service_password(),
            _pairs(args.port, "--port"),
            allow_service_change=args.allow_service_change,
        )
    if not _print_plan(client):
        _out(f"demand {action}: {result}")


async def _energy_saving(client: ItmClient, args: argparse.Namespace) -> None:
    """Show, suspend, resume or (service) disable energy saving."""
    if args.action == "show":
        es = await client.fetch_energy_saving(
            os.environ.get("ITM_SERVICE_PASSWORD") or None
        )
        _out(f"energy saving active: {_onoff(es.active)}")
        _out(
            f"suspend screen: {es.suspend_status} (button {_onoff(es.suspend_enabled)})"
        )
        if es.service is not None:
            sv = es.service
            _out(
                f"status: start {sv.start_time}, remaining {sv.remaining_time}, "
                f"target {_shown(sv.target)}, current {_shown(sv.current)}, "
                f"estimated {_shown(sv.estimated)}, excess {_shown(sv.excess)}, "
                f"control {sv.control_status}"
            )
        return
    if args.action == "disable":
        active = await client.disable_energy_saving(
            _service_password(), allow_service_change=args.allow_service_change
        )
    else:
        active = await client.suspend_energy_saving(suspend=args.action == "suspend")
    if not _print_plan(client):
        _out(f"energy saving active: {_onoff(active)}")


def _add_demand_commands(sub: Any) -> None:
    """Add `demand ACTION ...` and `energy-saving ACTION`."""
    dm = sub.add_parser(
        "demand", help="demand (power limit) control (tested on the simulator only)"
    )
    dm.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=[
            "show",
            "target",
            "forced-level",
            "level",
            "group",
            "service",
            "pulse",
            "interlock",
        ],
    )
    dm.add_argument(
        "values",
        nargs="*",
        help="target: POWER; forced-level: LEVEL; level: CONTROL; group: CONTROL GROUP",
    )
    dm.add_argument("--only", help="show: the parts to read, comma separated")
    dm.add_argument("--group", help="level: the group (default: the first)")
    dm.add_argument("--start-level", help="level: the start level, as shown")
    dm.add_argument("--level", action="append", help="level: N=OPTION (repeat)")
    dm.add_argument("--upper-cool", type=float, help="level: upper cooling limit")
    dm.add_argument("--lower-heat", type=float, help="level: lower heating limit")
    dm.add_argument("--member", action="append", help="group: a member (repeat)")
    dm.add_argument("--condition", help="group: the condition, as shown")
    dm.add_argument("--resume", help="group: the resume option, as shown")
    dm.add_argument("--port1", help="pulse: input port 1 ('' clears it)")
    dm.add_argument("--port2", help="pulse: input port 2 ('' clears it)")
    dm.add_argument("--coefficient", type=float, help="pulse: the pulse coefficient")
    dm.add_argument("--sync", choices=["external", "period"], help="pulse: sync method")
    dm.add_argument("--return-value", type=float, help="pulse: the return value")
    dm.add_argument("--cutoff", type=float, help="pulse: the cut-off value")
    dm.add_argument("--port", action="append", help="interlock: N=PORT (repeat)")
    dm.add_argument(
        "--allow-service-change",
        action="store_true",
        help="pulse/interlock: needed (installer settings; ITM_SERVICE_PASSWORD)",
    )
    es = sub.add_parser("energy-saving", help="show, suspend or resume energy saving")
    es.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=["show", "suspend", "resume", "disable"],
    )
    es.add_argument(
        "--allow-service-change",
        action="store_true",
        help="disable: needed (a service write; ITM_SERVICE_PASSWORD)",
    )


def _add_service_edit_commands(sub: Any) -> None:
    """Add `service [ACTION]`, `leakcheck ACTION [OUTDOOR]` and `password`."""
    sv = sub.add_parser(
        "service",
        help="the installer's service settings (ITM_SERVICE_PASSWORD); layout setup "
        "and R32 output ports",
    )
    sv.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=["show", "layout", "run-layout", "r32-ports", "r32-port"],
    )
    sv.add_argument("--port", type=int, choices=[1, 2], default=1, help="r32-port")
    sv.add_argument("--value", help="r32-port: the port, as r32-ports offers it")
    sv.add_argument("--clear", action="store_true", help="r32-port: clear the port")
    sv.add_argument(
        "--allow-service-change",
        action="store_true",
        help="needed for run-layout and r32-port (unless --dry-run)",
    )
    lc = sub.add_parser(
        "leakcheck",
        help="the leakage check (service; ITM_SERVICE_PASSWORD): outdoor units, "
        "registration, schedule, results, start/stop",
    )
    lc.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=[
            "show",
            "details",
            "results",
            "export",
            "register",
            "schedule",
            "copy",
            "start",
            "stop",
        ],
    )
    lc.add_argument("outdoor", nargs="?", help="the outdoor unit (name or address)")
    lc.add_argument("--indoor", action="append", help="register: an indoor unit")
    lc.add_argument("--program", type=int, help="schedule: the program, 1-4")
    lc.add_argument("--activation", help="schedule: as the program's combo shows it")
    lc.add_argument("--date", help="schedule: the start date, YYYY-MM-DD")
    lc.add_argument("--time", help="schedule: the start time, HH:MM")
    lc.add_argument("--to", action="append", help="copy: an outdoor unit (repeat)")
    lc.add_argument("--out", help="export: the file to write")
    lc.add_argument(
        "--allow-leakage-check",
        action="store_true",
        help="start/stop: needed; a check cools the whole refrigerant system for "
        "about 3 hours under maintenance",
    )
    lc.add_argument(
        "--allow-service-change",
        action="store_true",
        help="register/schedule/copy: needed (unless --dry-run)",
    )
    pw = sub.add_parser(
        "password",
        help="show or change the administrator/screen password "
        "(the new one from ITM_NEW_PASSWORD)",
    )
    pw.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=["show", "enable", "disable", "change"],
    )
    pw.add_argument("kind", nargs="?", choices=["administrator", "screen"])
    pw.add_argument(
        "--allow-password-change",
        action="store_true",
        help="needed (unless --dry-run): a mistake can lock the touch panel",
    )


def _add_emgstop_command(sub: Any) -> None:
    """Add `emgstop [ACTION] [NAME] [NEW_NAME]`."""
    es = sub.add_parser(
        "emgstop", help="show or change the emergency stop programs, release a stop"
    )
    es.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=[
            "show",
            "settings",
            "enable",
            "disable",
            "add",
            "rename",
            "copy",
            "delete",
            "set",
            "active",
            "release",
        ],
    )
    es.add_argument("name", nargs="?", help="the program")
    es.add_argument("new_name", nargs="?", help="rename/copy: the new name")
    es.add_argument(
        "--allow-safety-change",
        action="store_true",
        help="enable/disable/release: needed, they change a safety function",
    )
    es.add_argument("--input", action="append", help="set: an input point (repeat)")
    es.add_argument("--output", action="append", help="set: an output point (repeat)")
    es.add_argument("--release-mode", help="set: a release mode, as the iTM shows it")
    es.add_argument("--stop", help="set: what the stop does, as the iTM shows it")


def add_service_commands(sub: Any) -> None:
    """Add the emergency stop, demand and service commands."""
    _add_emgstop_command(sub)
    _add_demand_commands(sub)
    _add_service_edit_commands(sub)


SERVICE_COMMANDS: dict[str, Callable[[ItmClient, argparse.Namespace], Awaitable[None]]]
SERVICE_COMMANDS = {
    "service": _service,
    "leakcheck": _leakcheck,
    "password": _password,
    "demand": _demand,
    "energy-saving": _energy_saving,
    "emgstop": _emgstop,
}
