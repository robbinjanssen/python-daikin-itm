"""Small CLI for debugging: ``itm status``, ``itm zones``, ``itm setpoint 1:1-04 21``.

Credentials come from ITM_USERNAME / ITM_PASSWORD, or --creds pointing at the JSON
written by ``op item get <id> --fields label=username,label=password --reveal
--format json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from .cli_common import _out, _print_plan, _print_system, _safe
from .client import DEFAULT_ZONE_PATH, UNIT_VIEWS, FileSessionStore, ItmClient
from .dealer_cli import DEALER_COMMANDS, add_dealer_commands
from .energy_cli import ENERGY_COMMANDS, add_energy_commands
from .exceptions import ItmError
from .layout_cli import LAYOUT_COMMANDS, add_layout_commands
from .menu import ChangeoverReference
from .models import (
    AIRFLOW_DIRECTIONS,
    DIALOG_SETTINGS,
    FAN_SPEEDS,
    MODES,
    AnalogPoint,
    Capabilities,
    PowerState,
    RemoteLock,
    TempLimit,
)
from .schedule_cli import SCHEDULE_COMMANDS, _print_schedules, add_schedule_commands
from .service_cli import SERVICE_COMMANDS, add_service_commands
from .system_cli import SYSTEM_COMMANDS, add_system_commands
from .trace import TraceFile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from .menu import ChangeoverGroup, ItmInfo, SetbackSettings
    from .models import Unit, UnitSettings


CACHE_DIR = Path.home() / ".cache" / "daikin-itm"


# Descriptions etc., refreshed when unit names change.
UNITS_CACHE = CACHE_DIR / "units.json"


def _session_file(host: str) -> Path:
    """Return the default session file for a host."""
    safe_host = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    return CACHE_DIR / f"session-{safe_host}.json"


def _credentials(path: str | None) -> tuple[str, str]:
    """Load the iTM credentials.

    Args:
    ----
        path: A JSON file with username/password fields, or None to use the
            ITM_USERNAME and ITM_PASSWORD environment variables.

    Returns:
    -------
        The username and password.

    """
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        fields = data if isinstance(data, list) else data.get("fields", [data])
        values = {f.get("label"): f.get("value") for f in fields}
        if not values.get("username") or not values.get("password"):
            sys.exit(f"{path} has no username and password fields")
        return values["username"], values["password"]
    try:
        return os.environ["ITM_USERNAME"], os.environ["ITM_PASSWORD"]
    except KeyError:
        sys.exit("set ITM_USERNAME and ITM_PASSWORD, or pass --creds")


def _load_caps(host: str) -> dict[str, Capabilities]:
    """Load the cached capabilities of a host's units."""
    try:
        data = json.loads(UNITS_CACHE.read_text(encoding="utf-8"))
        data = data.get(host, {}) if isinstance(data, dict) else {}
        return {
            name: Capabilities(
                **{
                    **c,
                    "zones": tuple(c.get("zones", ())),
                    "unknown_labels": tuple(c.get("unknown_labels", ())),
                }
            )
            for name, c in data.items()
        }
    except (FileNotFoundError, ValueError, TypeError):
        return {}


def _save_caps(host: str, caps: dict[str, Capabilities]) -> None:
    """Cache the capabilities of a host's units."""
    try:
        data = json.loads(UNITS_CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        data = {}
    if not isinstance(data, dict):  # not ours: start over
        data = {}
    data[host] = {name: asdict(c) for name, c in caps.items()}
    UNITS_CACHE.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _print_units(units: dict[str, Unit], caps: dict[str, Capabilities]) -> None:
    """Print a table of units."""
    _out(
        f"{'unit':14} {'description':22} {'power':8} {'mode':10} {'fan':7} "
        f"{'setpoint':>8} {'range':>7} {'room':>6}  error"
    )
    for u in units.values():
        rng = (
            f"{u.setpoint_range[0]}-{u.setpoint_range[-1]}" if u.setpoint_range else "-"
        )
        temp = f"{u.room_temperature:.1f}" if u.room_temperature is not None else "-"
        desc = caps[u.name].description if u.name in caps else None
        # The list view has no on/off options, only the state.
        power = u.power or (u.power_state.value if u.power_state else "-")
        _out(
            f"{u.name:14} {desc or '-':22} {power:8} "
            f"{u.mode_label or '-':10} {u.fan_speed or '-':7} "
            f"{u.setpoint or '-':>8} {rng:>7} {temp:>6}  {u.error_code or ''}"
        )


def _print_change(unit: str, u: Unit | None) -> None:
    """Print a unit's state after a change."""
    if u is not None:
        _out(
            f"{unit}: power={u.power} mode={u.mode_label} fan={u.fan_speed} "
            f"setpoint={u.setpoint}"
        )


async def _status(client: ItmClient, args: argparse.Namespace) -> None:
    """Show all units."""
    units = await client.fetch_units(view=args.view)
    caps = {} if args.refresh else _load_caps(args.host)
    if set(units) - set(caps):  # first run, or a unit was added/renamed in the iTM
        caps = await client.fetch_capabilities()
        _save_caps(args.host, caps)
    _print_units(units, caps)
    for c in caps.values():
        if c.unknown_labels:
            _out(f"{c.name}: unknown mode labels {list(c.unknown_labels)}")


async def _points(client: ItmClient, args: argparse.Namespace) -> None:
    """Show every point in the zone, whatever its kind."""
    points = await client.fetch_points(details=args.details)
    for p in points.values():
        if isinstance(p, AnalogPoint):
            alarm = {True: "ALARM", False: "ok", None: "?"}[p.alarm]
            watch = (
                f" {p.source_point} {p.source_value or ''} "
                f"<= {p.upper_limit} {p.unit or ''}"
                if p.source_point
                else ""
            )
            _out(f"{p.name:14} {p.kind.value:12} {alarm:8}{watch}")
        else:
            power = p.power or (p.power_state.value if p.power_state else "-")
            _out(
                f"{p.name:14} {p.kind.value:12} {power:8} {p.mode_label or '-':10} "
                f"{p.fan_speed or '-':7} {p.setpoint or '-':>8}  {p.error_code or ''}"
            )


async def _errors(client: ItmClient, _args: argparse.Namespace) -> None:
    """Show the error information, if the iTM shows any."""
    info = await client.fetch_error_information()
    if info is None:
        _out("no error")
        return
    _out("  ".join(info.columns))
    for row in info.errors:
        _out("  ".join(row))
    if info.inquiry or info.address:
        _out(f"{info.inquiry or ''} {info.address or ''}".strip())


def _print_info(info: ItmInfo) -> None:
    offset = info.clock_offset
    off = f"{offset / 60:+.0f} min" if offset is not None else "?"
    _out(f"version {info.version or '?'} ({info.build or '?'})")
    _out(
        f"language {info.language or '?'}, {info.temperature_unit or '?'}, "
        f"decimal {info.decimal_separator or '?'}, "
        f"{'24h' if info.time_24h else '12h' if info.time_24h is not None else '?'}, "
        f"dates {info.date_format or '?'}"
    )
    dst = {True: "on", False: "off", None: "?"}[info.dst_enabled]
    _out(f"clock {info.clock_text or '?'} ({off} vs this machine), DST {dst}")


def _print_groups(groups: list[ChangeoverGroup]) -> None:
    if not groups:
        _out("no auto changeover groups")
    for g in groups:
        detail = ""
        if g.differential is not None or g.members:
            detail = (
                f"  differential {g.differential} °C, reference "
                f"{g.reference.value if g.reference else '?'}, "
                f"members {', '.join(g.members) or '-'}"
            )
        _out(
            f"{g.name:24} {'enabled' if g.enabled else 'disabled':9} "
            f"{g.member_count}{detail}"
        )


def _print_setback(sb: SetbackSettings) -> None:
    _out(f"setback high: +{sb.high_up} / -{sb.high_back} °C")
    _out(f"setback low:  +{sb.low_up} / -{sb.low_back} °C")
    _out(f"recovery: cool +{sb.recovery_cool} °C, heat -{sb.recovery_heat} °C")


async def _info(client: ItmClient, _args: argparse.Namespace) -> None:
    """Show the iTM's version, region and clock."""
    _print_info(await client.fetch_info())


async def _changeover(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the auto changeover groups."""
    action = args.action
    if action == "list":
        _print_groups(await client.fetch_changeover_groups(details=args.details))
        return
    if not args.name:
        sys.exit("the group's name is needed")
    reference = ChangeoverReference(args.reference) if args.reference else None
    result: ChangeoverGroup | None = None
    if action in {"enable", "disable"}:
        result = await client.set_changeover_enabled(
            args.name, enabled=action == "enable"
        )
    elif action == "set":
        result = await client.set_changeover_settings(
            args.name, differential=args.differential, reference=reference
        )
    elif action == "create":
        result = await client.create_changeover_group(
            args.name,
            members=tuple(args.add),
            differential=args.differential,
            reference=reference,
        )
    elif action == "members":
        current = next(
            (
                g
                for g in await client.fetch_changeover_groups(details=True)
                if g.name == args.name
            ),
            None,
        )
        if current is None:
            sys.exit(f"no changeover group {args.name!r}")
        wanted = [m for m in current.members if m not in args.remove] + list(args.add)
        result = await client.set_changeover_members(args.name, tuple(wanted))
    elif action == "delete":
        await client.delete_changeover_group(args.name)
        _out(f"{args.name}: deleted" if not client.dry_run else "")
    if not _print_plan(client) and result is not None:
        _print_groups([result])


async def _setback(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the setback settings."""
    changes = {
        k: getattr(args, k)
        for k in (
            "high_up",
            "high_back",
            "low_up",
            "low_back",
            "recovery_cool",
            "recovery_heat",
        )
        if getattr(args, k) is not None
    }
    if not changes:
        _print_setback(await client.fetch_setback())
        return
    result = await client.set_setback(**changes)
    if not _print_plan(client) and result is not None:
        _print_setback(result)


async def _overtime(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the overtime timer's duration."""
    if args.minutes is None:
        _out(f"overtime timer: {await client.fetch_overtime_duration()} min")
        return
    result = await client.set_overtime_duration(args.minutes)
    if not _print_plan(client):
        _out(f"overtime timer: {result} min")


async def _history_names(client: ItmClient, unit: str) -> set[str]:
    """Return the names the history may show a unit by.

    The history shows a unit by its name; ``unit`` may also be its address or the
    description set in the iTM (both read from the units' Details screens).
    """
    wanted = unit.strip()
    names = {wanted}
    key = wanted.casefold()
    found = False
    for name, caps in (await client.fetch_capabilities()).items():
        known = (name, caps.address or "", (caps.description or "").strip())
        if key in {k.casefold() for k in known}:
            names.add(name)
            found = True
    if not found:
        print(
            f"note: no unit in the zone path {'/'.join(client.zone_path) or '(top)'} "
            f"has the name, address or description {wanted!r}; showing entries "
            "named that only (use --zone to look elsewhere)",
            file=sys.stderr,
        )
    return names


async def _history(client: ItmClient, args: argparse.Namespace) -> None:
    """Show the history."""
    day = date.fromisoformat(args.day) if args.day else None
    names = await _history_names(client, args.unit) if args.unit else None
    pages = await client.fetch_history(
        day=day, include_management=args.management, pages=args.pages
    )
    for page in pages:
        _out(f"-- {page.day or '?'} {page.page or ''}")
        for e in page.entries:
            if names is not None and e.name.strip() not in names:
                continue
            _out(f"{e.time:6} {e.kind:12} {e.name:16} {e.content:36} {e.source}")


async def _functions(client: ItmClient, _args: argparse.Namespace) -> None:
    """Probe the functions the menu hides."""
    for name, f in (await client.fetch_optional_functions()).items():
        state = f"{len(f.rows)} entries" if f.available else "not available"
        _out(f"{name:24} {state}")


async def _config(client: ItmClient, args: argparse.Namespace) -> None:
    """Show everything fetch_config reads in one session."""
    c = await client.fetch_config(system=args.system)
    ind = c.indicators
    _out(
        f"indicators: energy saving {'on' if ind.energy_saving else 'off'}, "
        f"error {'on' if ind.error else 'off'}, "
        f"emergency stop {'on' if ind.emergency_stop else 'off'}"
    )
    _print_info(c.info)
    _out("schedules:")
    _print_schedules(list(c.schedules))
    _out("auto changeover:")
    _print_groups(list(c.changeover_groups))
    _print_setback(c.setback)
    _out(f"overtime timer: {c.overtime_minutes} min")
    for p in c.emergency_stop:
        _out(f"emergency stop {p.name}: {'enabled' if p.enabled else 'disabled'}")
    if c.system is not None:
        _print_system(c.system)


async def _control_zone(client: ItmClient, args: argparse.Namespace) -> None:
    """Operate every unit of a zone at once, then show them."""
    zone = args.zone_name
    if args.power in {"setback-low", "setback-high"}:
        await client.set_zone_power_state(
            zone, PowerState(args.power.replace("-", "_"))
        )
    elif args.power is not None or args.mode or args.fan:
        power = None if args.power is None else args.power == "on"
        await client.apply_zone(zone, power=power, mode=args.mode, fan_speed=args.fan)
    if args.setpoint is not None:
        await client.set_zone_setpoint(zone, args.setpoint)
    if _print_plan(client):
        return
    for unit in (await client.fetch_zone_units(zone)).values():
        _print_change(unit.name, unit)


def _limit_arg(text: str | None) -> TempLimit | None:
    """Parse "off" or "MIN-MAX" (°C) into a setpoint limit."""
    if text is None:
        return None
    if text == "off":
        return TempLimit(enabled=False, minimum=None, maximum=None)
    try:
        low, high = (float(x) for x in text.split("-", 1))
    except ValueError:
        sys.exit(f"a limit is 'off' or MIN-MAX, e.g. 18-26, not {text!r}")
    return TempLimit(enabled=True, minimum=low, maximum=high)


async def _settings(  # pylint: disable=too-many-locals
    client: ItmClient, args: argparse.Namespace
) -> None:
    """Show (or change) a unit's settings and status from its dialogs."""
    start_stop = RemoteLock(args.start_stop_lock) if args.start_stop_lock else None
    mode = RemoteLock(args.mode_lock) if args.mode_lock else None
    setpoint = RemoteLock(args.setpoint_lock) if args.setpoint_lock else None
    overtime = {"on": True, "off": False}.get(args.overtime or "")
    cool, heat = _limit_arg(args.cool_limit), _limit_arg(args.heat_limit)
    airflow = args.airflow_direction
    extras = {
        "ventilation_mode": args.ventilation_mode,
        "ventilation_amount": args.ventilation_amount,
        "reset_filter_sign": args.reset_filter_sign,
        "reset_dust_alarm": args.reset_dust_alarm,
        "force_dust_alarm_reset": args.force_dust_alarm_reset,
    }
    dialog = _dialog_setting_args(args)
    changes = (start_stop, mode, setpoint, overtime, cool, heat, airflow)
    if any(v is not None for v in (*changes, *dialog.values())) or any(extras.values()):
        result = await client.set_unit_settings(
            args.unit,
            start_stop_lock=start_stop,
            mode_lock=mode,
            setpoint_lock=setpoint,
            overtime=overtime,
            cool_limit=cool,
            heat_limit=heat,
            airflow_direction=airflow,
            **extras,
            **dialog,
        )
        if _print_plan(client) or result is None:
            return
    s = await client.fetch_unit_settings(args.unit)
    locks = s.remote_locks
    _out(f"{s.name}: thermostat {'on' if s.thermo_on else 'off'}")
    _out(f"  R32 alarm: {s.r32_alarm}")
    _out(
        f"  remote locks: start/stop {locks.start_stop.value}, "
        f"mode {locks.mode.value}, setpoint {locks.setpoint.value}"
    )
    for label, limit in (("cool", s.cool_limit), ("heat", s.heat_limit)):
        state = "on" if limit.enabled else "off"
        _out(f"  {label} limit: {state}, {limit.minimum}-{limit.maximum}")
    _out(f"  overtime timer: {'on' if s.overtime else 'off'}")
    if s.airflow_direction is not None:
        _out(f"  airflow direction: {s.airflow_direction}")
    if s.ventilation_modes:
        modes = ", ".join(s.ventilation_modes)
        _out(f"  ventilation mode: {s.ventilation_mode} ({modes})")
    if s.ventilation_amounts:
        amounts = ", ".join(s.ventilation_amounts)
        _out(f"  ventilation amount: {s.ventilation_amount} ({amounts})")
    if s.filter_sign_resettable:
        _out("  filter sign: shown (--reset-filter-sign resets it)")
    if s.dust_alarm_resettable:
        _out("  dust alarm: shown (--reset-dust-alarm resets it)")
    _print_dialog_settings(s)
    for key, value in s.flags.items():
        _out(f"  {key}: {value}")


def _dialog_setting_args(args: argparse.Namespace) -> dict[str, Any]:
    """Return the optional dialog settings (DIALOG_SETTINGS) asked for."""
    on_off_arg = {"on": True, "off": False}
    return {
        "low_noise": args.low_noise,
        "outdoor_low_noise": args.outdoor_low_noise,
        "capacity": args.capacity,
        "reheat": on_off_arg.get(args.reheat or ""),
        "leaving_water_cool": args.leaving_water_cool,
        "leaving_water_heat": args.leaving_water_heat,
        "storage_water": args.storage_water,
        "output_repeat": on_off_arg.get(args.output_repeat or ""),
        "output_repeat_interval": args.output_repeat_interval,
        "analog_output": args.analog_output,
        "multistate_output": args.multistate,
    }


def _print_dialog_settings(s: UnitSettings) -> None:
    """Print the optional dialog settings the unit offers, with their options."""
    if s.tabs:
        _out(f"  dialog tabs: {', '.join(s.tabs)}")
    if s.r32_alarm_stoppable:
        _out("  R32 alarm stop: offered")
    for setting in DIALOG_SETTINGS:
        value = getattr(s, setting.key)
        if value is None:
            continue
        if isinstance(value, bool):
            value = "on" if value else "off"
        options = getattr(s, f"{setting.key}_options", ())
        shown = f" ({', '.join(str(o) for o in options)})" if options else ""
        _out(f"  {setting.key.replace('_', ' ')}: {value}{shown}")


async def _zones(client: ItmClient, args: argparse.Namespace) -> None:
    """List the zones and units at a path."""
    path = tuple(args.path.split("/")) if args.path else ()
    for name in await client.list_zones(path):
        _out(name)


async def _setpoint(client: ItmClient, args: argparse.Namespace) -> None:
    """Change a unit's setpoint."""
    u = await client.set_setpoint(args.unit, args.value)
    if not _print_plan(client):
        _out(f"{args.unit}: setpoint now {u.setpoint if u else None}")


async def _caps(client: ItmClient, args: argparse.Namespace) -> None:
    """Show the modes and fan control each unit offers."""
    all_caps = await client.fetch_capabilities()
    _save_caps(args.host, all_caps)
    for name, caps in all_caps.items():
        _out(
            f"{name:14} {caps.address or '-':8} id={caps.point_id or '-':5} "
            f"modes={','.join(caps.modes) or '-':20} "
            f"fan={'yes' if caps.fan_speed else 'no':4} "
            f"{caps.description or ''}"
        )


async def _power(client: ItmClient, args: argparse.Namespace) -> None:
    """Start or stop a unit, or put it in setback."""
    if args.state in {"on", "off"}:
        result = await client.set_power(args.unit, args.state == "on")
    else:
        state = PowerState(args.state.replace("-", "_"))
        result = await client.set_power_state(args.unit, state)
    if not _print_plan(client):
        _print_change(args.unit, result)


async def _mode(client: ItmClient, args: argparse.Namespace) -> None:
    """Change a unit's operation mode."""
    result = await client.set_mode(args.unit, args.mode)
    if not _print_plan(client):
        _print_change(args.unit, result)


async def _fan(client: ItmClient, args: argparse.Namespace) -> None:
    """Change a unit's fan speed."""
    result = await client.set_fan_speed(args.unit, args.speed)
    if not _print_plan(client):
        _print_change(args.unit, result)


COMMANDS: dict[str, Callable[[ItmClient, argparse.Namespace], Awaitable[None]]] = {
    "status": _status,
    "zones": _zones,
    "setpoint": _setpoint,
    "caps": _caps,
    "settings": _settings,
    "points": _points,
    "errors": _errors,
    "info": _info,
    "config": _config,
    "control-zone": _control_zone,
    "changeover": _changeover,
    "setback": _setback,
    "overtime": _overtime,
    "history": _history,
    "functions": _functions,
    "power": _power,
    "mode": _mode,
    "fan": _fan,
    **DEALER_COMMANDS,
    **LAYOUT_COMMANDS,
    **SCHEDULE_COMMANDS,
    **SERVICE_COMMANDS,
    **SYSTEM_COMMANDS,
    **ENERGY_COMMANDS,
}


async def _run(args: argparse.Namespace) -> None:
    """Run the command given on the command line."""
    username, password = _credentials(args.creds)
    # Owner-only: it holds the session token while a command runs (and after a
    # failed logout).
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    CACHE_DIR.chmod(0o700)
    zone_path = tuple(args.zone.split("/")) if args.zone else DEFAULT_ZONE_PATH
    async with ItmClient(
        args.host,
        username,
        password,
        store=FileSessionStore(
            str(args.session_file or _session_file(args.host)), args.host
        ),
        zone_path=zone_path,
        read_only=args.read_only,
        dry_run=args.dry_run,
        allow_experimental=args.experimental,
    ) as client:
        if args.trace:
            trace = TraceFile(args.trace)
            # pylint: disable-next=protected-access
            client._post = trace.wrap(client._post)  # type: ignore[method-assign,assignment]  # noqa: SLF001
        await COMMANDS[args.command](client, args)


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    p = argparse.ArgumentParser(prog="itm", description=(__doc__ or "").splitlines()[0])
    p.add_argument(
        "--host",
        default=os.environ.get("ITM_HOST"),
        help="address of the iTM (default: $ITM_HOST)",
    )
    p.add_argument(
        "--creds", help="JSON file with username/password fields (1Password export)"
    )
    p.add_argument(
        "--trace",
        metavar="FILE",
        help="append every request (numbers only) and reply (token blanked) to FILE; "
        "replies include the installation's names, settings, usernames and IP "
        "addresses",
    )
    p.add_argument(
        "--zone",
        help="zone path to the units, e.g. 'Building/Indoor units' "
        "(default: the top level)",
    )
    p.add_argument(
        "--session-file",
        help=f"where to keep the session token (default: per host in {CACHE_DIR})",
    )
    p.add_argument(
        "--read-only",
        action="store_true",
        help="refuse every write before it is sent",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="do everything but the request that makes a change; print it instead",
    )
    p.add_argument(
        "--experimental",
        action="store_true",
        help="allow the writes not validated on a real iTM yet (tested against "
        "the simulated iTM only); a dry run needs no opt-in",
    )
    sub = p.add_subparsers(dest="command", required=True)
    st = sub.add_parser("status", help="show all units")
    st.add_argument(
        "--refresh",
        action="store_true",
        help="re-read descriptions instead of using the cache",
    )
    st.add_argument(
        "--view",
        choices=UNIT_VIEWS,
        default="icons",
        help="read each unit's panel, or the zone's list view in one request",
    )
    z = sub.add_parser("zones", help="list zones/units at a path")
    z.add_argument("path", nargs="?", help="e.g. 'Building'")
    s = sub.add_parser("setpoint", help="change a unit's setpoint")
    s.add_argument("unit")
    s.add_argument("value", type=int)
    sub.add_parser(
        "caps",
        help="modes and fan control each unit offers (opens each settings dialog)",
    )
    se = sub.add_parser(
        "settings", help="a unit's locks, setpoint limits and status (from its dialogs)"
    )
    se.add_argument("unit")
    se.add_argument("--start-stop-lock", choices=["allowed", "stop_only", "forbidden"])
    se.add_argument("--mode-lock", choices=["allowed", "forbidden"])
    se.add_argument("--setpoint-lock", choices=["allowed", "forbidden"])
    se.add_argument("--overtime", choices=["on", "off"])
    se.add_argument("--cool-limit", help="'off' or MIN-MAX in °C, e.g. 20-26")
    se.add_argument("--heat-limit", help="'off' or MIN-MAX in °C, e.g. 16-24")
    se.add_argument(
        "--airflow-direction",
        choices=list(AIRFLOW_DIRECTIONS),
        help="position_0 (horizontal) .. position_4 (down), or swing",
    )
    se.add_argument("--ventilation-mode", help="one of the modes the unit lists")
    se.add_argument("--ventilation-amount", help="one of the amounts the unit lists")
    se.add_argument("--reset-filter-sign", action="store_true")
    se.add_argument("--reset-dust-alarm", action="store_true")
    se.add_argument("--force-dust-alarm-reset", action="store_true")
    _add_dialog_setting_options(se)
    pt = sub.add_parser(
        "points", help="every point in the zone: units, ventilation, analog"
    )
    pt.add_argument(
        "--details", action="store_true", help="also what each analog point watches"
    )
    sub.add_parser("errors", help="the error information the iTM shows, if any")
    _add_menu_commands(sub)
    add_dealer_commands(sub)
    add_layout_commands(sub)
    add_schedule_commands(sub)
    add_service_commands(sub)
    add_system_commands(sub)
    add_energy_commands(sub)
    return p


def _add_dialog_setting_options(se: Any) -> None:
    """Add `itm settings` options for the dialog's optional settings."""
    se.add_argument("--low-noise", help="one of the low noise options the unit lists")
    se.add_argument(
        "--outdoor-low-noise", help="one of the outdoor unit's low noise options"
    )
    se.add_argument(
        "--capacity", type=int, help="outdoor unit capacity limit in %%, as listed"
    )
    se.add_argument("--reheat", choices=["on", "off"])
    se.add_argument("--leaving-water-cool", type=float, help="setpoint, e.g. 10.0")
    se.add_argument("--leaving-water-heat", type=float, help="setpoint, e.g. 45.0")
    se.add_argument("--storage-water", type=float, help="storage water setpoint")
    se.add_argument(
        "--output-repeat", choices=["on", "off"], help="digital output repeat mode"
    )
    se.add_argument(
        "--output-repeat-interval", type=int, help="repeat interval in minutes"
    )
    se.add_argument("--analog-output", type=float, help="analog output value")
    se.add_argument("--multistate", help="one of the multistate output's states")


def _add_menu_commands(sub: Any) -> None:
    """Add the commands that read the iTM's main menu."""
    sub.add_parser("info", help="firmware version, region settings and clock")
    cf = sub.add_parser("config", help="everything from the menu in one session")
    cf.add_argument("--system", action="store_true", help="the system screens too")
    co = sub.add_parser("changeover", help="the auto changeover groups")
    co.add_argument(
        "action",
        nargs="?",
        default="list",
        choices=["list", "enable", "disable", "set", "create", "members", "delete"],
    )
    co.add_argument("name", nargs="?")
    co.add_argument("--details", action="store_true", help="members and settings too")
    co.add_argument("--differential", type=int, help="°C, as the iTM offers")
    co.add_argument("--reference", choices=[r.value for r in ChangeoverReference])
    co.add_argument("--add", action="append", default=[], help="a unit to add")
    co.add_argument("--remove", action="append", default=[], help="a unit to remove")
    sb = sub.add_parser("setback", help="show or set the setback offsets (°C)")
    for key in (
        "high-up",
        "high-back",
        "low-up",
        "low-back",
        "recovery-cool",
        "recovery-heat",
    ):
        sb.add_argument(f"--{key}", type=int)
    overtime = sub.add_parser(
        "overtime", help="show or set the overtime timer's duration"
    )
    overtime.add_argument("minutes", nargs="?", type=int, help="30 to 180, step 30")
    hi = sub.add_parser("history", help="who changed what, when (newest first)")
    hi.add_argument("--day", help="YYYY-MM-DD (default: the iTM's current day)")
    hi.add_argument("--management", action="store_true", help="include logins/logouts")
    hi.add_argument("--pages", type=int, default=1, help="pages of 100 entries")
    hi.add_argument(
        "--unit",
        metavar="UNIT",
        help="only this unit's entries: its name, address or description (looked "
        "up in the zone path)",
    )
    sub.add_parser("functions", help="probe the hidden functions (experimental)")
    w = sub.add_parser("power", help="start or stop a unit")
    w.add_argument("unit")
    w.add_argument("state", choices=["on", "off", "setback-low", "setback-high"])
    m = sub.add_parser("mode", help="change a unit's operation mode")
    m.add_argument("unit")
    m.add_argument("mode", choices=sorted(set(MODES.values())))
    f = sub.add_parser("fan", help="change a unit's fan speed")
    f.add_argument("unit")
    f.add_argument("speed", choices=FAN_SPEEDS)
    cz = sub.add_parser(
        "control-zone", help="operate every unit of a zone (below --zone) at once"
    )
    cz.add_argument("zone_name", metavar="zone")
    cz.add_argument("--power", choices=["on", "off", "setback-low", "setback-high"])
    cz.add_argument("--setpoint", type=int)
    cz.add_argument("--mode", choices=["heat", "cool", "fan", "dry", "auto"])
    cz.add_argument("--fan", choices=["low", "medium", "high", "auto"])


def main(argv: Sequence[str] | None = None) -> None:
    """Run the ``itm`` command line tool.

    Args:
    ----
        argv: The arguments, defaults to the ones the process was started with.

    """
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("set the iTM's address with --host or ITM_HOST")

    async def runner() -> None:
        task = asyncio.current_task()
        if task is not None:
            # SIGTERM/SIGHUP cancel the task, so the client's finally-blocks still
            # log out.
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGHUP):
                loop.add_signal_handler(sig, task.cancel)
        await _run(args)

    try:
        asyncio.run(runner())
    except ItmError as e:
        sys.exit(_safe(f"error: {e}"))
    except (aiohttp.ClientError, TimeoutError) as e:
        reason = str(e) or type(e).__name__
        sys.exit(_safe(f"error: could not reach the iTM at {args.host}: {reason}"))
    except (KeyboardInterrupt, asyncio.CancelledError):
        sys.exit(130)


if __name__ == "__main__":
    main()
