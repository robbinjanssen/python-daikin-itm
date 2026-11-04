"""Small CLI for debugging: ``itm status``, ``itm zones``, ``itm setpoint 1:1-04 21``.

Credentials come from ITM_USERNAME / ITM_PASSWORD, or --creds pointing at the JSON
written by ``op item get <id> --fields label=username,label=password --reveal
--format json``.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import os
import re
import signal
import sys
import unicodedata
from dataclasses import asdict
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp

from .client import DEFAULT_ZONE_PATH, UNIT_VIEWS, FileSessionStore, ItmClient
from .dealer_cli import DEALER_COMMANDS, add_dealer_commands
from .energy_navigator import (
    ConversionType,
    EnergyNavigator,
    PlannedYear,
    RuleRange,
    ShownValue,
)
from .exceptions import ItmError
from .history_export import HistoryExport, parse_history_export
from .layout_cli import LAYOUT_COMMANDS, add_layout_commands
from .menu import CalendarRule, ChangeoverReference
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
from .schedule_edit import CalendarRuleSpec, EventSpec
from .system import LANGUAGE_RADIOS, ScreensaverMode, SystemSettings
from .trace import TraceFile

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from .demand import DemandControl
    from .emergency import EmergencyStopSettings
    from .menu import (
        ChangeoverGroup,
        ItmInfo,
        ScheduleEvent,
        ScheduleSummary,
        SetbackSettings,
    )
    from .models import Unit, UnitSettings
    from .service_edit import LeakageCheckSchedule

CACHE_DIR = Path.home() / ".cache" / "daikin-itm"
# Descriptions etc., refreshed when unit names change.
UNITS_CACHE = CACHE_DIR / "units.json"


def _safe(text: str) -> str:
    """Make text from the iTM safe to print.

    Names, descriptions and errors come from whatever answers at the iTM's address;
    replace control and format characters so they can't drive the terminal (escape
    sequences, hidden or rewritten lines).

    Args:
    ----
        text: The text to sanitize.

    Returns:
    -------
        The text with every control and format character replaced by ``?``.

    """
    return "".join(
        "?" if unicodedata.category(ch).startswith("C") else ch for ch in text
    )


def _out(text: str = "") -> None:
    """Print sanitized text."""
    print(_safe(text))


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


def _print_schedules(programs: list[ScheduleSummary]) -> None:
    for p in programs:
        period = f"{p.period[0]}-{p.period[1]}" if p.period else "all year"
        _out(f"{p.name:24} {'enabled' if p.enabled else 'disabled':9} {period}")


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


def _print_plan(client: ItmClient) -> bool:
    """Print a dry run's plan; tell whether this was a dry run."""
    if not client.dry_run:
        return False
    _out("dry run, would send:")
    for line in client.planned:
        _out(f"  {line}")
    return True


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


async def _schedules(client: ItmClient, _args: argparse.Namespace) -> None:
    """Show the schedule programs."""
    _print_schedules(await client.fetch_schedules())


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


def _print_event(e: ScheduleEvent) -> None:
    extra = ""
    if e.details or e.power or e.mode or e.setpoint is not None or e.lock_mode:
        bits = [
            f"power {e.power.value}" if e.power else "",
            f"mode {e.mode or e.mode_label}" if e.mode or e.mode_label else "",
            f"setpoint {e.setpoint}" if e.setpoint is not None else "",
            f"setpoint {e.setpoint_shift}" if e.setpoint_shift else "",
            f"locks {e.lock_start_stop.value if e.lock_start_stop else '-'}/"
            f"{e.lock_mode.value if e.lock_mode else '-'}/"
            f"{e.lock_setpoint.value if e.lock_setpoint else '-'}"
            if e.lock_start_stop or e.lock_mode or e.lock_setpoint
            else "",
            f"fan {e.fan_speed}" if e.fan_speed else "",
            f"ventilation {e.ventilation_mode}" if e.ventilation_mode else "",
            f"air {e.air_volume}" if e.air_volume else "",
            f"overtime {'on' if e.overtime else 'off'}"
            if e.overtime is not None
            else "",
            *[
                f"{k} {v}"
                for k, v in e.extra.items()
                if v and v not in (e.ventilation_mode, e.air_volume)
            ],
        ]
        extra = "  [" + ", ".join(b for b in bits if b) + "]"
    _out(f"    {e.time} {e.target:20} {e.target_kind:12} {e.action}{extra}")


async def _schedule(client: ItmClient, args: argparse.Namespace) -> None:
    """Show a schedule program in full; create, delete, enable or disable it."""
    if args.create:
        created = await client.create_schedule(args.name)
        if not _print_plan(client) and created is not None:
            _print_schedules([created])
        return
    if args.delete:
        await client.delete_schedule(args.name)
        if not _print_plan(client):
            _out(f"{args.name}: deleted")
        return
    if args.enable or args.disable:
        summary = await client.set_schedule_enabled(args.name, enabled=args.enable)
        if not _print_plan(client) and summary is not None:
            _print_schedules([summary])
        return
    p = await client.fetch_schedule(
        args.name, events=not args.no_events, details=args.details
    )
    period = f"{p.period[0]} - {p.period[1]}" if p.period else "all year"
    _out(f"{p.name}: {'enabled' if p.enabled else 'disabled'}, {period}")
    names = [
        "Sunday",
        "Monday",
        "Tuesday",
        "Wednesday",
        "Thursday",
        "Friday",
        "Saturday",
    ]
    for day in range(7):
        events = p.weekly.get(day, ())
        slots = p.weekly_slots.get(day, ())
        if not events and not slots:
            continue
        _out(f"  {names[day]} ({len(slots)} time slots)")
        for e in events:
            _print_event(e)
    for label, events in p.special.items():
        _out(f"  {label}")
        for e in events:
            _print_event(e)
    for rule in p.calendar:
        _out(f"  calendar: {rule.month} {rule.day} -> {rule.pattern}")


async def _schedule_day(client: ItmClient, args: argparse.Namespace) -> None:
    """Show what a program does on a day."""
    d = await client.fetch_schedule_day(args.name, days_ahead=args.days_ahead)
    _out(f"{d.program}: {d.day}")
    for e in d.events:
        _print_event(e)


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


def _onoff(value: bool | None) -> str:  # noqa: FBT001 - a display helper
    return "?" if value is None else "on" if value else "off"


def _print_system(  # noqa: PLR0912  # pylint: disable=too-many-branches,too-many-locals
    st: SystemSettings,
) -> None:
    """Print the system settings, one section per screen that was read."""
    if st.zones is not None:
        _out(f"zones (shown from {st.zone_position}):")
        for z in st.zones:
            level = "-" if z.level is None else z.level
            _out(f"  {z.name!r:20} level {level}, {z.zones} zones, {z.points} points")
    if st.points is not None:
        _out("management points:")
        for p in st.points:
            _out(f"  {p.name:10} {p.kind:14} icon {p.icon}  {p.info or ''}")
    if st.maintenance is not None:
        busy = [m.name for m in st.maintenance if m.in_maintenance]
        _out(f"in maintenance: {', '.join(busy) or 'none'}")
    if (n := st.network) is not None:
        _out(
            f"network: {n.controller_name}, host {n.hostname}, {n.ip_address}/"
            f"{n.subnet_mask}, gateway {n.gateway}, DNS {n.dns_primary} "
            f"{n.dns_secondary}"
        )
    if (e := st.email) is not None:
        addresses = ", ".join(a for _, a in e.addresses) or "no addresses"
        _out(f"e-mail: {_onoff(e.enabled)}, {addresses}")
    if st.web_users is not None:
        users = ", ".join(
            f"{u.name} ({'admin' if u.admin else u.kind})" for u in st.web_users
        )
        _out(f"web users: {users}")
    if (h := st.hardware) is not None:
        buzzer = "continuous" if h.buzzer_continuous else f"{h.buzzer_minutes} min"
        _out(
            f"touch panel: backlight {h.backlight}, buzzer volume {h.buzzer_volume} "
            f"for {buzzer}, key click volume {h.key_click_volume}"
        )
    if (v := st.screensaver) is not None:
        mode = v.mode.value if v.mode else "?"
        _out(
            f"screensaver: {mode} after {v.delay_minutes} min, "
            f"off on error {_onoff(v.off_on_error)}"
        )
    if st.confirm_dialogs is not None:
        _out(f"confirm dialogs: {_onoff(st.confirm_dialogs)}")
    if (r := st.remote_management) is not None:
        _out(
            f"remote management: {r.status} ({r.ip_address}), "
            f"connected {_onoff(r.connected)}"
        )
    if (d := st.ppd) is not None:
        _out(
            f"PPD: {d.start} - {d.end}, time limit {_onoff(d.time_limit)}, "
            f"calculation day {d.calculation_day}, {len(d.units)} units"
        )
        for u in d.units:
            _out(f"  {u.name:20} {u.consumption_kwh} kWh, standby {u.standby_kwh} kWh")
    if (g := st.energy_navigator) is not None:
        _out(
            f"energy navigator: sampling {g.sampling_from} - {g.sampling_to} "
            f"({g.sampling_target}), export periods {', '.join(g.export_periods)}"
        )


async def _hardware(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the touch panel's backlight, buzzer and key click."""
    minutes = args.buzzer_minutes
    if any(
        v is not None
        for v in (args.backlight, args.buzzer_volume, minutes, args.key_click_volume)
    ):
        await client.set_hardware(
            backlight=args.backlight,
            buzzer_volume=args.buzzer_volume,
            buzzer_minutes=None if minutes in {None, "continuous"} else int(minutes),
            buzzer_continuous=minutes == "continuous",
            key_click_volume=args.key_click_volume,
        )
        if _print_plan(client):
            return
    _print_system(await client.fetch_system(["hardware"]))


async def _screensaver(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the touch panel's screensaver."""
    off_on_error = {"on": True, "off": False}.get(args.off_on_error or "")
    if args.mode or args.delay is not None or off_on_error is not None:
        await client.set_screensaver(
            mode=ScreensaverMode(args.mode) if args.mode else None,
            delay_minutes=args.delay,
            off_on_error=off_on_error,
        )
        if _print_plan(client):
            return
    _print_system(await client.fetch_system(["screensaver"]))


async def _maintenance(client: ItmClient, args: argparse.Namespace) -> None:
    """Put a point into maintenance or take it out."""
    state = await client.set_maintenance(args.point, on=args.state == "on")
    if not _print_plan(client):
        _out(f"{args.point}: maintenance {_onoff(state)}")


async def _point(client: ItmClient, args: argparse.Namespace) -> None:
    """Change a management point's description (or name)."""
    point = await client.set_point_attributes(
        args.point, name=args.name, info=args.info
    )
    if not _print_plan(client) and point is not None:
        _out(f"{point.name}: {point.info or ''}")


async def _zone(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change a zone's description, intervals (or name)."""
    changes = (args.name, args.info, args.start_interval, args.stop_interval)
    if all(v is None for v in changes):
        z = await client.fetch_zone_attributes(args.zone)
    else:
        result = await client.set_zone_attributes(
            args.zone,
            name=args.name,
            info=args.info,
            start_interval=args.start_interval,
            stop_interval=args.stop_interval,
        )
        if _print_plan(client) or result is None:
            return
        z = result
    _out(
        f"{z.name}: {z.info or ''} (start interval {z.start_interval}, "
        f"stop interval {z.stop_interval})"
    )


def _new_password() -> str:
    """Read a new password from ITM_NEW_PASSWORD or ask for it (never echoed)."""
    return os.environ.get("ITM_NEW_PASSWORD") or getpass.getpass("new password: ")


async def _user(client: ItmClient, args: argparse.Namespace) -> None:
    """Add, change or delete a web access user."""
    admin = {"admin": True, "user": False}.get(args.type or "")
    if args.action == "add":
        if not args.manage_zone:
            sys.exit("a new web user needs at least one --manage-zone")
        user = await client.create_web_user(
            args.name, _new_password(), zones=tuple(args.manage_zone), admin=bool(admin)
        )
    elif args.action == "set":
        password = _new_password() if args.password else None
        user = await client.set_web_user(
            args.name,
            password=password,
            admin=admin,
            zones=tuple(args.manage_zone) if args.manage_zone else None,
        )
    elif args.action == "screens":
        await _user_screens(client, args)
        return
    else:
        await client.delete_web_user(args.name)
        if not _print_plan(client):
            _out(f"{args.name}: deleted")
        return
    if not _print_plan(client) and user is not None:
        _out(f"{user.name}: {'admin' if user.admin else 'user'}")


async def _user_screens(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or set the screens a normal web user may open."""
    if args.screen:
        await client.set_web_user_screens(args.name, args.screen)
        if _print_plan(client):
            return
    screens = await client.fetch_web_user_screens(args.name)
    _out(f"{args.name}: screens {', '.join(screens.allowed) or 'none'}")
    _out(f"  available: {', '.join(screens.available) or 'none'}")


async def _own_settings(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the logged-in web user's own settings."""
    formats = {"dmy": "%d/%m/%Y", "mdy": "%m/%d/%Y", "ymd": "%Y/%m/%d"}
    changes: dict[str, Any] = {
        "date_format": formats.get(args.date_format or ""),
        "time_24h": {"24h": True, "12h": False}.get(args.time or ""),
        "decimal_comma": {"point": False, "comma": True}.get(args.decimal or ""),
        "icon_colour": args.icon_colour,
        "language": args.language,
        "password": _new_password() if args.password else None,
    }
    if any(v is not None for v in changes.values()):
        await client.set_own_settings(
            **changes, allow_password_change=args.allow_password_change
        )
        if _print_plan(client):
            return
    own = await client.fetch_own_settings()
    time = None if own.time_24h is None else ("24h" if own.time_24h else "12h")
    decimal = (
        None
        if own.decimal_comma is None
        else ("comma" if own.decimal_comma else "point")
    )
    _out(
        f"own settings: language {own.language}, date {own.date_format}, "
        f"time {time}, decimal {decimal}, icon colour {own.icon_colour}"
    )


async def _language(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or switch the iTM's display language."""
    if args.code:
        await client.set_language(args.code)
        if _print_plan(client):
            return
    _out(f"language: {await client.fetch_language()}")


async def _region(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the date, time, temperature and decimal formats."""
    formats = {"dmy": "%d/%m/%Y", "mdy": "%m/%d/%Y", "ymd": "%Y/%m/%d"}
    date_format = formats.get(args.date_format or "")
    time_24h = {"24h": True, "12h": False}.get(args.time or "")
    unit = {"c": "°C", "f": "°F"}.get(args.temperature or "")
    decimal = {"point": ".", "comma": ","}.get(args.decimal or "")
    if any(v is not None for v in (date_format, time_24h, unit, decimal)):
        await client.set_region(
            date_format=date_format,
            time_24h=time_24h,
            temperature_unit=unit,
            decimal_separator=decimal,
        )
        if _print_plan(client):
            return
    info = await client.fetch_info()
    clock = "24h" if info.time_24h else "12h"
    _out(
        f"region: dates {info.date_format}, {clock}, {info.temperature_unit}, "
        f"decimal {info.decimal_separator!r}"
    )


async def _dst(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or switch summer time (on: the EU rule)."""
    if args.state:
        await client.set_dst(enabled=args.state == "on")
        if _print_plan(client):
            return
    d = await client.fetch_dst()
    _out(f"summer time: {_onoff(d.enabled)}, from {d.start} to {d.end}")


async def _clock(client: ItmClient, args: argparse.Namespace) -> None:
    """Show the iTM's clock, or set it to this computer's time."""
    if args.set_now:
        await client.set_clock()
        if _print_plan(client):
            return
    info = await client.fetch_info()
    _out(f"clock: {info.clock} (off by {info.clock_offset})")


def _day_arg(text: str) -> int | str:
    """Parse a weekday 0-6 (0 = Sunday) or a special day Ex1-Ex5."""
    return int(text) if text.isdigit() else text


def _event_spec(args: argparse.Namespace, time: str, target: str) -> EventSpec:
    """Build an EventSpec from the event options."""
    locks = {
        k: RemoteLock(v) if v else None
        for k, v in (
            ("lock_start_stop", args.start_stop_lock),
            ("lock_mode", args.mode_lock),
            ("lock_setpoint", args.setpoint_lock),
        )
    }
    try:
        return EventSpec(
            time=time,
            target=target,
            zone=args.zone_target,
            power=PowerState(args.power.replace("-", "_")) if args.power else None,
            mode=args.mode,
            setpoint=args.setpoint,
            overtime={"on": True, "off": False}.get(args.overtime or ""),
            fan_speed=args.fan,
            **locks,
        )
    except ValueError as err:
        sys.exit(str(err))


def _calendar_rule(args: argparse.Namespace) -> CalendarRuleSpec:
    """Build a CalendarRuleSpec from --date M-D or --weekday MONTH|all:WEEK:WEEKDAY."""
    try:
        if args.date:
            month, day = (int(x) for x in args.date.split("-"))
            return CalendarRuleSpec(pattern=args.pattern, month=month, day=day)
        month_text, week, weekday = args.weekday.split(":")
        return CalendarRuleSpec(
            pattern=args.pattern,
            month=None if month_text == "all" else int(month_text),
            week=int(week),
            weekday=int(weekday),
        )
    except ValueError as err:
        sys.exit(f"a rule is --date M-D or --weekday MONTH|all:WEEK:WEEKDAY ({err})")


COPY_ACTIONS = {"copy-program", "copy-day", "clear-day", "copy-event", "copy-calendar"}


async def _schedule_copy(client: ItmClient, args: argparse.Namespace) -> None:
    """Copy a program, a day, an event or the calendar, or clear a day."""
    name, action = args.name, args.action
    if action == "copy-program":
        await client.copy_schedule(name, args.new_name)
    elif action == "copy-day":
        targets = [_day_arg(t) for t in args.targets]
        await client.copy_schedule_day(name, _day_arg(args.day), targets)
    elif action == "clear-day":
        await client.clear_schedule_day(name, _day_arg(args.day))
    elif action == "copy-event":
        await client.copy_schedule_event(
            name,
            _day_arg(args.day),
            args.time,
            args.target,
            args.new_time,
            new_target=args.new_target,
        )
    else:
        await client.copy_schedule_calendar(name, args.targets)
    if not _print_plan(client):
        _out(f"{name}: {action} done")


async def _schedule_edit(  # noqa: PLR0912  # pylint: disable=too-many-branches
    client: ItmClient, args: argparse.Namespace
) -> None:
    """Change a schedule program's events, period, special days or calendar."""
    name, action = args.name, args.action
    if action in COPY_ACTIONS:
        await _schedule_copy(client, args)
        return
    if action in {"add-event", "edit-event", "delete-event"}:
        day = _day_arg(args.day)
        if action == "add-event":
            spec = _event_spec(args, args.time, args.target)
            event = await client.add_schedule_event(name, day, spec)
        elif action == "edit-event":
            spec = _event_spec(
                args, args.new_time or args.time, args.new_target or args.target
            )
            event = await client.edit_schedule_event(
                name, day, args.time, args.target, spec, keep_unset=not args.replace
            )
        else:
            await client.delete_schedule_event(name, day, args.time, args.target)
            event = None
        if not _print_plan(client):
            if event is not None:
                _print_event(event)
            else:
                _out(f"{args.time} {args.target}: deleted")
        return
    if action == "period":
        period = None
        if args.period != "off":
            try:
                begin, end = (
                    tuple(int(x) for x in part.split("-"))
                    for part in args.period.split(":")
                )
            except ValueError:
                sys.exit("a period is MM-DD:MM-DD or off")
            period = ((begin[0], begin[1]), (end[0], end[1]))
        result = await client.set_schedule_period(name, period)
    elif action == "rename-special":
        result = await client.rename_schedule_special_day(name, args.day, args.new_name)
    elif action == "add-rule":
        result = await client.add_schedule_calendar_rule(name, _calendar_rule(args))
    else:
        rule = CalendarRule(month=args.month, day=args.rule_day, pattern=args.pattern)
        result = await client.delete_schedule_calendar_rule(name, rule)
    if not _print_plan(client) and result is not None:
        period_text = (
            f"{result.period[0]} - {result.period[1]}" if result.period else "all year"
        )
        names = ", ".join(result.special_names)
        _out(f"{result.name}: {period_text}; special days {names}")
        for r in result.calendar:
            _out(f"  calendar: {r.month} {r.day} -> {r.pattern}")


async def _network(client: ItmClient, args: argparse.Namespace) -> None:
    """Show the network settings, or plan (or, explicitly allowed, make) a change."""
    fields = {
        "controller_name": args.name,
        "hostname": args.hostname,
        "ip_address": args.ip,
        "subnet_mask": args.mask,
        "gateway": args.gateway,
        "dns_primary": args.dns1,
        "dns_secondary": args.dns2,
    }
    changes = {k: v for k, v in fields.items() if v is not None}
    if changes:
        await client.set_network(
            **changes, allow_network_change=args.allow_network_change
        )
        if _print_plan(client):
            return
    _print_system(await client.fetch_system(["network"]))


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


async def _zone_structure(client: ItmClient, args: argparse.Namespace) -> None:
    """Add, delete or move a zone, change its members or icon, or a point's icon."""
    command = args.command
    if command == "zone-add":
        await client.create_zone(args.name, parent=args.parent)
    elif command == "zone-delete":
        await client.delete_zone(args.name)
    elif command == "zone-move":
        await client.move_zone(args.name, args.parent)
    elif command == "zone-icon":
        await client.set_zone_icon(args.name, args.icon)
    elif command == "point-icon":
        await client.set_point_icon(args.name, args.icon)
    elif args.add or args.remove:
        current = await client.fetch_zone_members(args.name)
        wanted = [m for m in current if m not in (args.remove or [])]
        wanted += [m for m in args.add or [] if m not in wanted]
        await client.set_zone_members(args.name, wanted)
    if command == "zone-members" and args.order:
        await client.set_zone_member_order(args.name, args.order, use_jump=args.jump)
    if _print_plan(client):
        return
    if command == "zone-members":
        members = await client.fetch_zone_members(args.name)
        _out(f"{args.name}: {', '.join(members) or 'no members'}")
    elif command == "point-icon":
        _out(f"{args.name}: icon {args.icon}")
    else:
        _print_system(await client.fetch_system(["zones"]))


async def _email(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the e-mail notification: addresses, on/off."""
    action = args.action
    if action == "add":
        await client.add_email_address(args.address, args.point or [])
    elif action == "edit":
        await client.edit_email_address(
            args.address, new_address=args.new_address, points=args.point
        )
    elif action == "delete":
        await client.delete_email_address(args.address)
    elif action in {"on", "off"}:
        await client.set_email_enabled(enabled=action == "on")
    if _print_plan(client):
        return
    _print_system(await client.fetch_system(["email"]))
    if action == "show":
        server = await client.fetch_email_server()
        _out("e-mail server: " + ", ".join(f"{k} {v}" for k, v in server.items()))


async def _confirm_dialogs(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or switch the iTM's "confirm settings" dialogs."""
    if args.state:
        await client.set_confirm_dialogs(enabled=args.state == "on")
        if _print_plan(client):
            return
    _print_system(await client.fetch_system(["confirm_dialogs"]))


def _date_arg(text: str | None) -> date | None:
    """Parse YYYY-MM-DD."""
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        sys.exit(f"a date is YYYY-MM-DD, not {text!r}")


def _shown(value: ShownValue | None) -> str:
    """Return a shown value's text, trimmed ("" when not shown)."""
    return "" if value is None else value.text.strip()


def _print_energy(nav: EnergyNavigator) -> None:
    """Print what fetch_energy_navigator read, as the iTM shows it."""
    if (b := nav.budget) is not None:
        _out(f"budget (display {b.display}):")
        for tab in b.tabs:
            labels = " | ".join(v.text.strip() for v in tab.labels)
            _out(
                f"  {tab.name}: {tab.type_label or 'energy type'} {tab.energy_type}, "
                f"{tab.unit_label or 'energy unit'} {tab.unit}; {labels}"
            )
    for title, plan in (("planned", nav.planned), ("actual", nav.actual)):
        if plan is None:
            continue
        _out(f"{title} consumption ({plan.energy_type}):")
        for year in plan.years:
            if isinstance(year, PlannedYear):
                monthly = {True: "per month", False: "per year", None: "?"}
                _out(
                    f"  {year.name}: expected {_shown(year.expected)}, ratio "
                    f"{_shown(year.ratio)} {year.ratio_unit or ''}, "
                    f"{monthly[year.per_month]}"
                )
            else:
                _out(
                    f"  {year.name}: CO2 factor {_shown(year.co2_factor)} "
                    f"{year.co2_unit or ''}"
                )
            for m in year.months:
                _out(f"    {m.month:12} {m.value.text.strip()}")
    if nav.rules is not None:
        _out(f"rules: {', '.join(nav.rules) or '(none)'}")
    if nav.sampling_targets is not None:
        _out("sampling targets:")
        for t in nav.sampling_targets:
            mark = "*" if t.selected else " "
            _out(f"  {mark} {t.level} {t.name}")
    _print_energy_more(nav)


def _print_energy_more(nav: EnergyNavigator) -> None:
    """Print the opt-in parts: energy types and groups, details, rule editors."""
    for t in nav.energy_types or ():
        _out(
            f"energy type {t.name}: unit {t.unit}, type {t.energy_type}, CO2 "
            f"{_shown(t.co2_factor)} {t.co2_unit or ''}, conversion "
            f"{_shown(t.conversion)} {t.conversion_unit or ''}"
        )
    for g in nav.energy_groups or ():
        members = "; ".join(" ".join(m) for m in g.members or ())
        _out(f"energy group {g.name} ({g.energy_type}): {members or '(none)'}")
    if (d := nav.details) is not None:
        _out(f"equipment details ({d.day}):")
        for row in d.occurrences:
            _out(f"  {' | '.join(row)}")
    for r in nav.rule_editor or ():
        days = sum(1 for pattern in r.weekdays if pattern)
        special = ", ".join(name for name, _ in r.special_days)
        _out(f"rule {r.name}: {days} weekday patterns, special days {special}")
        for row in r.calendar:
            _out(f"  {' | '.join(row)}")
    for day in nav.rule_settings or ():
        ranges = ", ".join(_rule_range(r) for r in day.ranges)
        _out(f"rule {day.rule} {day.day}: {ranges or '(none)'}")
    if (i := nav.installer) is not None:
        _out("energy navigator installer settings:")
        for p in i.room_temperature or ():
            _out(f"  room temperature {p.name}: {p.setting} {p.analog_point or ''}")
        analog = "; ".join(" ".join(row) for row in i.analog_points or ())
        _out(f"  analog inputs: {analog or '(not offered)'}")
        _out(f"  collection starts in {i.collection_start}")
        c = i.conversion or ConversionType()
        _out(f"  conversion {c.name!r}: {c.one} / {c.thousand} / {c.million}")


def _rule_range(r: RuleRange) -> str:
    """Return a rule range as the --range option takes it."""
    text = f"{r.start}-{r.end}"
    return text + "".join(
        f"/{key}={value:g}"
        for key, value in (("cool", r.cool), ("heat", r.heat))
        if value is not None
    )


def _parse_range(text: str) -> RuleRange:
    """Parse --range START-END[/cool=N][/heat=N]."""
    times, *options = text.split("/")
    start, _, end = times.partition("-")
    values: dict[str, float] = {}
    for option in options:
        key, _, value = option.partition("=")
        try:
            values[key.strip()] = float(value)
        except ValueError:
            values["bad"] = 0
        if set(values) - {"cool", "heat"}:
            sys.exit(f"--range is START-END[/cool=N][/heat=N], not {text!r}")
    return RuleRange(start.strip(), end.strip(), values.get("cool"), values.get("heat"))


async def _energy(client: ItmClient, args: argparse.Namespace) -> None:
    """Show the energy navigator's screens (read only)."""
    only = args.only.split(",") if args.only else None
    password = None
    if only and "installer" in only:
        password = os.environ.get("ITM_SERVICE_PASSWORD")
        if not password:
            sys.exit("set ITM_SERVICE_PASSWORD to read the installer settings")
    _print_energy(await client.fetch_energy_navigator(only, service_password=password))


def _month_values(pairs: list[str] | None) -> dict[str, float]:
    """Parse --month MONTH=VALUE options."""
    values = {}
    for pair in pairs or []:
        month, _, value = pair.rpartition("=")
        try:
            values[month.strip()] = float(value)
        except ValueError:
            sys.exit(
                f"--month is MONTH=VALUE (as the iTM lists the month), not {pair!r}"
            )
    return values


async def _energy_edit(client: ItmClient, args: argparse.Namespace) -> None:
    """Change an energy navigator setting (tested against the simulated iTM only)."""
    if args.action in ENERGY_RULE_ACTIONS + ENERGY_INSTALLER_ACTIONS:
        await _energy_edit_more(client, args)
        return
    action, months = args.action, _month_values(args.month)
    per_month = None if args.per_month is None else args.per_month == "yes"
    result: Any = None
    if action == "planned":
        result = await client.set_planned_consumption(
            year=args.year,
            energy_type=args.type,
            ratio=args.ratio,
            per_month=per_month,
            months=months,
        )
    elif action == "actual":
        result = await client.set_actual_consumption(
            year=args.year, energy_type=args.type, co2_factor=args.co2, months=months
        )
    elif action == "sampling":
        result = await client.set_energy_sampling(
            start=_date_arg(args.start), end=_date_arg(args.end), target=args.target
        )
    elif not args.name or (action == "rename" and not args.new_name):
        sys.exit(
            f"energy-edit {action} needs a name"
            + " and a new name" * (action == "rename")
        )
    elif action == "create":
        await client.create_energy_type(args.name, group=args.group)
    elif action == "rename":
        await client.rename_energy_type(args.name, args.new_name, group=args.group)
    else:
        await client.delete_energy_type(args.name, group=args.group)
    if not _print_plan(client):
        _out(f"{action}: done" + (f", the iTM shows {result}" if result else ""))


ENERGY_RULE_ACTIONS = [
    "rule-create",
    "rule-rename",
    "rule-delete",
    "rule-copy",
    "calendar-copy",
    "rule-day",
]
ENERGY_INSTALLER_ACTIONS = ["room-temp", "collection-start", "conversion"]


async def _energy_installer_edit(client: ItmClient, args: argparse.Namespace) -> Any:
    """Change an energy navigator installer setting (simulated iTM only)."""
    action = args.action
    password = os.environ.get("ITM_SERVICE_PASSWORD")
    if not password:
        sys.exit(f"energy-edit {action} needs ITM_SERVICE_PASSWORD")
    allow = args.allow_service_change
    if action == "room-temp":
        if not args.point:
            sys.exit("energy-edit room-temp needs --point")
        return await client.set_energy_room_temperature(
            password,
            args.point,
            analog_point=args.analog,
            allow_service_change=allow,
        )
    if action == "collection-start":
        if not args.start_month:
            sys.exit("energy-edit collection-start needs --start-month")
        return await client.set_energy_collection_start(
            password, args.start_month, allow_service_change=allow
        )
    change = ConversionType(args.conversion_name, args.one, args.thousand, args.million)
    return await client.set_energy_conversion(
        password, change, allow_service_change=allow
    )


async def _energy_rule_edit(client: ItmClient, args: argparse.Namespace) -> Any:
    """Change a management rule (simulated iTM only)."""
    action, name = args.action, args.name
    if not name or (action == "rule-rename" and not args.new_name):
        sys.exit(f"energy-edit {action} needs a rule name")
    if action == "rule-create":
        await client.create_energy_rule(name)
    elif action == "rule-rename":
        await client.rename_energy_rule(name, args.new_name)
    elif action == "rule-delete":
        await client.delete_energy_rule(name)
    elif action in {"rule-copy", "calendar-copy"}:
        if not args.to:
            sys.exit(f"energy-edit {action} needs --to")
        await client.copy_energy_rule(name, args.to, calendar=action == "calendar-copy")
    else:
        if not args.day:
            sys.exit("energy-edit rule-day needs --day")
        ranges = [_parse_range(r) for r in args.range or []]
        return await client.set_energy_rule_day(name, args.day, ranges)
    return None


async def _energy_edit_more(client: ItmClient, args: argparse.Namespace) -> None:
    """Change a management rule or an installer setting (simulated iTM only)."""
    if args.action in ENERGY_INSTALLER_ACTIONS:
        result = await _energy_installer_edit(client, args)
    else:
        result = await _energy_rule_edit(client, args)
    if not _print_plan(client):
        _out(f"{args.action}: done" + (f", the iTM shows {result}" if result else ""))


async def _ppd(client: ItmClient, args: argparse.Namespace) -> None:
    """Calculate power proportional distribution for a period or monthly day."""
    d = await client.fetch_ppd(
        start=_date_arg(args.start), end=_date_arg(args.end), calculation_day=args.day
    )
    _print_system(SystemSettings(ppd=d))


async def _export(client: ItmClient, args: argparse.Namespace) -> None:
    """Export the history, energy data or point list to a local file."""
    if args.kind == "setup":
        download = await client.export_setup()
    elif args.kind == "energy":
        if not args.period:
            sys.exit("an energy export needs --period, as the iTM lists it")
        download = await client.export_energy_data(args.period)
    else:
        start, end = _date_arg(args.start), _date_arg(args.end)
        if start is None or end is None:
            sys.exit("this export needs --start and --end")
        if args.kind == "history":
            download = await client.export_history(start, end)
            if args.parse:
                _print_history_export(parse_history_export(download.data))
                return
        else:
            download = await client.export_point_list(start, end)
    out = Path(args.out or download.name)
    await asyncio.to_thread(_save, out, download.data)
    _out(f"{download.name}: {len(download.data)} bytes written to {out}")


def _save(out: Path, data: bytes) -> None:
    """Write a download readable by its owner only (an archive can hold logins)."""
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(data)


def _print_history_export(export: HistoryExport) -> None:
    """Print a history export's records (times in UTC), oldest first."""
    for r in export.records:
        what = r.event or f"event {r.event_code}"
        value = r.setpoint if r.setpoint is not None else (r.power or r.fan_speed)
        if value is None and r.value:
            value = r.value
        by = r.source or (f"source {r.source_code}" if r.source_code else "")
        extra = r.ip_address or ""
        _out(
            f"{r.time:%Y-%m-%d %H:%M:%S}Z  {r.kind or r.class_code!s:10} "
            f"{r.name:16} {what:12} {value if value is not None else '':8} "
            f"{by} {extra}".rstrip()
        )


async def _backup(client: ItmClient, args: argparse.Namespace) -> None:
    """Run the iTM's backup; write the file when the iTM offers one."""
    download = await client.run_backup()
    if _print_plan(client):
        return
    if download is None:
        _out("backup done; the iTM keeps it (no file offered)")
        return
    out = Path(args.out or download.name)
    await asyncio.to_thread(_save, out, download.data)
    _out(f"{download.name}: {len(download.data)} bytes written to {out}")


async def _system(client: ItmClient, args: argparse.Namespace) -> None:
    """Show the system settings screens."""
    only = args.only.split(",") if args.only else None
    _print_system(await client.fetch_system(only))


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
    "system": _system,
    "hardware": _hardware,
    "ppd": _ppd,
    "energy": _energy,
    "energy-edit": _energy_edit,
    "export": _export,
    "email": _email,
    "confirm-dialogs": _confirm_dialogs,
    "zone-add": _zone_structure,
    "zone-delete": _zone_structure,
    "zone-move": _zone_structure,
    "zone-members": _zone_structure,
    "zone-icon": _zone_structure,
    "point-icon": _zone_structure,
    "control-zone": _control_zone,
    "service": _service,
    "leakcheck": _leakcheck,
    "password": _password,
    "demand": _demand,
    "energy-saving": _energy_saving,
    "network": _network,
    "schedule-edit": _schedule_edit,
    "maintenance": _maintenance,
    "point": _point,
    "zone": _zone,
    "user": _user,
    "own-settings": _own_settings,
    "language": _language,
    "dst": _dst,
    "region": _region,
    "clock": _clock,
    "screensaver": _screensaver,
    "changeover": _changeover,
    "setback": _setback,
    "overtime": _overtime,
    "emgstop": _emgstop,
    "backup": _backup,
    "schedules": _schedules,
    "schedule": _schedule,
    "schedule-day": _schedule_day,
    "history": _history,
    "functions": _functions,
    "power": _power,
    "mode": _mode,
    "fan": _fan,
    **DEALER_COMMANDS,
    **LAYOUT_COMMANDS,
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
    _add_system_commands(sub)
    _add_region_commands(sub)
    _add_zone_commands(sub)
    _add_email_commands(sub)
    _add_export_commands(sub)
    _add_schedule_edit_command(sub)
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
    _add_emgstop_command(sub)
    add_dealer_commands(sub)
    add_layout_commands(sub)
    _add_demand_commands(sub)
    sub.add_parser("schedules", help="the schedule programs")
    sc = sub.add_parser("schedule", help="a schedule program in full (via its editor)")
    sc.add_argument("name")
    sc.add_argument("--no-events", action="store_true", help="patterns only")
    on_off = sc.add_mutually_exclusive_group()
    on_off.add_argument("--enable", action="store_true", help="enable the program")
    on_off.add_argument("--disable", action="store_true", help="disable the program")
    on_off.add_argument(
        "--create", action="store_true", help="create it (empty and disabled)"
    )
    on_off.add_argument("--delete", action="store_true", help="delete the program")
    sc.add_argument(
        "--details",
        action="store_true",
        help="also open each event's settings screen (two requests per event)",
    )
    sd = sub.add_parser("schedule-day", help="what a program does on a day")
    sd.add_argument("name")
    sd.add_argument("--days-ahead", type=int, default=0)
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


def _add_system_commands(sub: Any) -> None:
    """Add the commands for the system settings screens."""
    sy = sub.add_parser(
        "system",
        help="system settings: zones, points, network, users, touch panel, PPD",
    )
    sy.add_argument("--only", help="comma-separated fields, e.g. network,hardware")
    hw = sub.add_parser(
        "hardware", help="show or set the touch panel's backlight and buzzer"
    )
    hw.add_argument("--backlight", type=int, help="1 to 8")
    hw.add_argument("--buzzer-volume", type=int, help="0 to 5")
    hw.add_argument("--buzzer-minutes", choices=["1", "3", "5", "continuous"])
    hw.add_argument("--key-click-volume", type=int, help="0 to 5")
    sv = sub.add_parser("screensaver", help="show or set the touch panel's screensaver")
    sv.add_argument("--mode", choices=[m.value for m in ScreensaverMode])
    sv.add_argument("--delay", type=int, help="minutes")
    sv.add_argument("--off-on-error", choices=["on", "off"])
    mt = sub.add_parser("maintenance", help="put a point into maintenance or out")
    mt.add_argument("point")
    mt.add_argument("state", choices=["on", "off"])
    pt = sub.add_parser("point", help="change a point's description (or name)")
    pt.add_argument("point")
    pt.add_argument("--info", help="the description, 0-50 characters")
    pt.add_argument("--name", help="a new name (changes how it is found!)")
    zn = sub.add_parser("zone", help="show or change a zone's settings")
    zn.add_argument("zone")
    zn.add_argument("--info")
    zn.add_argument("--name", help="a new name (changes every zone path!)")
    zn.add_argument("--start-interval", type=int)
    zn.add_argument("--stop-interval", type=int)
    us = sub.add_parser(
        "user",
        help="add, change or delete a web user (password from ITM_NEW_PASSWORD "
        "or a prompt)",
    )
    us.add_argument("action", choices=["add", "set", "delete", "screens"])
    us.add_argument("name")
    us.add_argument("--type", choices=["admin", "user"])
    us.add_argument("--password", action="store_true", help="set a new password")
    us.add_argument(
        "--manage-zone", action="append", help="a zone the user manages (repeatable)"
    )
    us.add_argument(
        "--screen",
        action="append",
        help="screens: a screen a normal user may open (repeatable; sets them all)",
    )
    _add_own_settings_command(sub)
    lg = sub.add_parser("language", help="show or switch the display language")
    lg.add_argument("code", nargs="?", choices=list(LANGUAGE_RADIOS))


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


def _add_schedule_edit_command(sub: Any) -> None:  # pylint: disable=too-many-locals
    """Add `schedule-edit NAME ACTION ...` for a program's contents."""
    se = sub.add_parser("schedule-edit", help="change a schedule program's contents")
    se.add_argument("name", help="the program")
    actions = se.add_subparsers(dest="action", required=True)

    def event_options(a: Any) -> None:
        a.add_argument("--zone-target", action="store_true", help="TARGET is a zone")
        a.add_argument("--power", choices=["on", "off", "setback-low", "setback-high"])
        a.add_argument("--mode", choices=["heat", "cool", "fan", "auto", "setpoint"])
        a.add_argument("--setpoint", type=float)
        a.add_argument(
            "--start-stop-lock", choices=["allowed", "stop_only", "forbidden"]
        )
        a.add_argument("--mode-lock", choices=["allowed", "forbidden"])
        a.add_argument("--setpoint-lock", choices=["allowed", "forbidden"])
        a.add_argument("--overtime", choices=["on", "off"])
        a.add_argument("--fan", choices=["low", "medium", "high", "auto"])

    for action, text in (
        ("add-event", "add an event"),
        (
            "edit-event",
            (
                "change an event (found by DAY TIME TARGET); settings left out are "
                "kept, or cleared with --replace"
            ),
        ),
        ("delete-event", "delete an event"),
    ):
        a = actions.add_parser(action, help=text)
        a.add_argument("day", help="0-6 (0 = Sunday) or Ex1-Ex5")
        a.add_argument("time", help="HH:MM")
        a.add_argument("target", help="a point (1:1-09) or, with --zone-target, a zone")
        if action != "delete-event":
            event_options(a)
        if action == "edit-event":
            a.add_argument("--new-time")
            a.add_argument("--new-target")
            a.add_argument(
                "--replace",
                action="store_true",
                help="clear the settings left out instead of keeping them",
            )
    pe = actions.add_parser("period", help="the program's period, or all year")
    pe.add_argument("period", help="MM-DD:MM-DD or off")
    rn = actions.add_parser("rename-special", help="rename a special day")
    rn.add_argument("day", help="Ex1-Ex5")
    rn.add_argument("new_name")
    ar = actions.add_parser("add-rule", help="add a calendar rule")
    ar.add_argument("pattern", type=int, help="1-5: Ex1-Ex5")
    when = ar.add_mutually_exclusive_group(required=True)
    when.add_argument("--date", help="M-D")
    when.add_argument("--weekday", help="MONTH|all:WEEK(1-5, 5 = last):WEEKDAY(0-6)")
    cp = actions.add_parser("copy-program", help="copy the program (disabled)")
    cp.add_argument("new_name")
    cy = actions.add_parser("copy-day", help="copy a day's events to other days")
    cy.add_argument("day")
    cy.add_argument("targets", nargs="+")
    cl = actions.add_parser("clear-day", help="delete every event of a day")
    cl.add_argument("day")
    ce = actions.add_parser("copy-event", help="copy an event to another time")
    for arg in ("day", "time", "target", "new_time"):
        ce.add_argument(arg)
    ce.add_argument("--new-target")
    cc = actions.add_parser("copy-calendar", help="copy the calendar to programs")
    cc.add_argument("targets", nargs="+")
    dr = actions.add_parser("delete-rule", help="delete a calendar rule, as shown")
    dr.add_argument("month")
    dr.add_argument("rule_day", metavar="day")
    dr.add_argument("pattern")


def _add_region_commands(sub: Any) -> None:
    """Add the region, summer time and clock commands."""
    rg = sub.add_parser("region", help="show or change date/time/unit/decimal formats")
    rg.add_argument("--date-format", choices=["dmy", "mdy", "ymd"])
    rg.add_argument("--time", choices=["24h", "12h"])
    rg.add_argument("--temperature", choices=["c", "f"])
    rg.add_argument("--decimal", choices=["point", "comma"])
    ds = sub.add_parser("dst", help="show or switch summer time (EU rule)")
    ds.add_argument("state", nargs="?", choices=["on", "off"])
    cz = sub.add_parser(
        "control-zone", help="operate every unit of a zone (below --zone) at once"
    )
    cz.add_argument("zone_name", metavar="zone")
    cz.add_argument("--power", choices=["on", "off", "setback-low", "setback-high"])
    cz.add_argument("--setpoint", type=int)
    cz.add_argument("--mode", choices=["heat", "cool", "fan", "dry", "auto"])
    cz.add_argument("--fan", choices=["low", "medium", "high", "auto"])
    _add_service_edit_commands(sub)
    nw = sub.add_parser(
        "network",
        help="show the network settings, or (with --dry-run) plan a change",
    )
    for opt in (
        "--name",
        "--hostname",
        "--ip",
        "--mask",
        "--gateway",
        "--dns1",
        "--dns2",
    ):
        nw.add_argument(opt)
    nw.add_argument(
        "--allow-network-change",
        action="store_true",
        help="really change it: a wrong value strands the iTM until someone is on site",
    )
    ck = sub.add_parser("clock", help="show the iTM's clock, or set it")
    ck.add_argument("--set-now", action="store_true", help="to this computer's time")


def _add_zone_commands(sub: Any) -> None:
    """Add the zone structure and icon commands."""
    za = sub.add_parser("zone-add", help="add an empty zone")
    za.add_argument("name")
    za.add_argument("--parent", help="the zone to add it below (default: top level)")
    zd = sub.add_parser("zone-delete", help="delete an empty zone")
    zd.add_argument("name")
    zm = sub.add_parser("zone-move", help="move a zone below another (paths change!)")
    zm.add_argument("name")
    zm.add_argument("parent", help="the new parent zone, or the root for the top")
    zb = sub.add_parser("zone-members", help="show or change a zone's points")
    zb.add_argument("name")
    zb.add_argument("--add", action="append", help="a point to add (repeatable)")
    zb.add_argument("--remove", action="append", help="a point to remove (repeatable)")
    zb.add_argument(
        "--order",
        nargs="+",
        metavar="POINT",
        help="put these members first, in this order",
    )
    zb.add_argument(
        "--jump", action="store_true", help="--order: move with Jump, not up"
    )
    zi = sub.add_parser("zone-icon", help="give a zone another icon (1000-1009)")
    zi.add_argument("name")
    zi.add_argument("icon", type=int)
    pi = sub.add_parser("point-icon", help="give a point another icon of its kind")
    pi.add_argument("name")
    pi.add_argument("icon", type=int)


def _add_own_settings_command(sub: Any) -> None:
    """Add `own-settings [--date-format ...] [--password]`."""
    ow = sub.add_parser(
        "own-settings",
        help="show or change the logged-in user's own settings (new password from "
        "ITM_NEW_PASSWORD or a prompt)",
    )
    ow.add_argument("--date-format", choices=["dmy", "mdy", "ymd"])
    ow.add_argument("--time", choices=["24h", "12h"])
    ow.add_argument("--decimal", choices=["point", "comma"])
    ow.add_argument("--icon-colour", type=int, choices=[1, 2])
    ow.add_argument("--language", choices=list(LANGUAGE_RADIOS))
    ow.add_argument("--password", action="store_true", help="set a new password")
    ow.add_argument(
        "--allow-password-change",
        action="store_true",
        help="--password: needed, a wrong password locks the client out",
    )


def _add_email_commands(sub: Any) -> None:
    """Add the e-mail notification and confirm dialog commands."""
    em = sub.add_parser("email", help="show or change the e-mail notification")
    em.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=["show", "add", "edit", "delete", "on", "off"],
    )
    em.add_argument("address", nargs="?", help="the e-mail address (add/edit/delete)")
    em.add_argument("--point", action="append", help="a point it reports on")
    em.add_argument("--new-address", help="edit: the new address")
    cd = sub.add_parser("confirm-dialogs", help="show or switch the confirm dialogs")
    cd.add_argument("state", nargs="?", choices=["on", "off"])


def _add_export_commands(sub: Any) -> None:
    """Add the PPD and export commands."""
    en = sub.add_parser("energy", help="the energy navigator's screens (read only)")
    en.add_argument(
        "--only",
        help="comma-separated: budget,planned,actual,rules,sampling_targets (the "
        "default), energy_types,energy_groups,details,rule_editor,rule_settings,"
        "installer (ITM_SERVICE_PASSWORD)",
    )
    ee = sub.add_parser(
        "energy-edit",
        help="change energy navigator settings (tested on a simulated iTM only)",
    )
    ee.add_argument(
        "action",
        choices=[
            "planned",
            "actual",
            "sampling",
            "create",
            "rename",
            "delete",
            *ENERGY_RULE_ACTIONS,
            *ENERGY_INSTALLER_ACTIONS,
        ],
    )
    ee.add_argument("--to", action="append", help="rule/calendar-copy: a target rule")
    ee.add_argument("--day", help="rule-day: a weekday or a special day's name")
    ee.add_argument("--range", action="append", help="START-END[/cool=N][/heat=N]")
    ee.add_argument("--point", help="room-temp: the point, as listed")
    ee.add_argument("--analog", help="room-temp: the analog input (else the unit's)")
    ee.add_argument("--start-month", help="collection-start: the month, as listed")
    ee.add_argument("--conversion-name", help="conversion: its name")
    ee.add_argument("--one", help="conversion: the unit for 1")
    ee.add_argument("--thousand", help="conversion: the unit for 1000")
    ee.add_argument("--million", help="conversion: the unit for 1000000")
    ee.add_argument(
        "--allow-service-change",
        action="store_true",
        help="room-temp/collection-start/conversion: really change it",
    )
    ee.add_argument("name", nargs="?", help="create/rename/delete: the energy type")
    ee.add_argument("new_name", nargs="?", help="rename: the new name")
    ee.add_argument("--group", action="store_true", help="an energy group instead")
    ee.add_argument("--year", help="planned/actual: the fiscal year's tab, as shown")
    ee.add_argument("--type", help="planned/actual: the energy type")
    ee.add_argument("--ratio", type=float, help="planned: ratio to last year (%%)")
    ee.add_argument("--per-month", choices=["yes", "no"], help="planned: per month")
    ee.add_argument("--co2", type=float, help="actual: the CO2 factor")
    ee.add_argument("--month", action="append", help="MONTH=VALUE, as listed")
    ee.add_argument("--start", help="sampling: YYYY-MM-DD")
    ee.add_argument("--end", help="sampling: YYYY-MM-DD")
    ee.add_argument("--target", help="sampling: the target, as listed")
    pp = sub.add_parser("ppd", help="power proportional distribution for a period")
    pp.add_argument("--start", help="YYYY-MM-DD")
    pp.add_argument("--end", help="YYYY-MM-DD")
    pp.add_argument("--day", type=int, help="or: the monthly calculation day")
    bk = sub.add_parser("backup", help="run the iTM's backup (a job on the device)")
    bk.add_argument("--out", help="the file to write, if the iTM offers one")
    ex = sub.add_parser(
        "export", help="export history, energy data, the point list or the setup"
    )
    ex.add_argument("kind", choices=["history", "energy", "points", "setup"])
    ex.add_argument("--start", help="YYYY-MM-DD")
    ex.add_argument("--end", help="YYYY-MM-DD")
    ex.add_argument("--period", help="energy: a period as the iTM lists it")
    ex.add_argument("--out", help="the file to write (default: the iTM's file name)")
    ex.add_argument(
        "--parse",
        action="store_true",
        help="history: print the records instead of writing the archive",
    )


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
