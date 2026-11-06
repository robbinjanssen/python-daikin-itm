"""The ``itm`` energy navigator, PPD, export and backup sub-commands."""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from .cli_common import date_arg as _date_arg
from .cli_common import echo as _out
from .cli_common import print_plan as _print_plan
from .cli_common import print_system as _print_system
from .cli_common import save as _save
from .cli_common import shown as _shown
from .energy_navigator import ConversionType, EnergyNavigator, PlannedYear, RuleRange
from .history_export import HistoryExport, parse_history_export
from .system import SystemSettings

if TYPE_CHECKING:
    import argparse
    from collections.abc import Awaitable, Callable

    from .client import ItmClient


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
    allow = {
        "allow_service_change": args.allow_service_change,
        "allow_restart": args.allow_restart,
    }
    if action == "room-temp":
        if not args.point:
            sys.exit("energy-edit room-temp needs --point")
        return await client.set_energy_room_temperature(
            password,
            args.point,
            analog_point=args.analog,
            **allow,
        )
    if action == "collection-start":
        if not args.start_month:
            sys.exit("energy-edit collection-start needs --start-month")
        return await client.set_energy_collection_start(
            password, args.start_month, **allow
        )
    change = ConversionType(args.conversion_name, args.one, args.thousand, args.million)
    return await client.set_energy_conversion(password, change, **allow)


async def _energy_rule_edit(client: ItmClient, args: argparse.Namespace) -> Any:
    """Change a management rule (simulated iTM only)."""
    action, name = args.action, args.name
    if not name or (action in {"rule-rename", "rule-copy"} and not args.new_name):
        names = "a rule name and the new name" if args.name else "a rule name"
        sys.exit(f"energy-edit {action} needs {names}")
    if action == "rule-create":
        await client.create_energy_rule(name)
    elif action == "rule-rename":
        await client.rename_energy_rule(name, args.new_name)
    elif action == "rule-delete":
        await client.delete_energy_rule(name)
    elif action == "rule-copy":
        await client.copy_energy_rule(name, args.new_name)
    elif action == "calendar-copy":
        if not args.to:
            sys.exit("energy-edit calendar-copy needs --to")
        await client.copy_energy_rule_calendar(name, args.to)
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


def add_energy_commands(sub: Any) -> None:
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
            "create",
            "rename",
            "delete",
            *ENERGY_RULE_ACTIONS,
            *ENERGY_INSTALLER_ACTIONS,
        ],
    )
    ee.add_argument("--to", action="append", help="calendar-copy: a target rule")
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
    ee.add_argument(
        "--allow-restart",
        action="store_true",
        help="room-temp/collection-start/conversion: saving restarts the iTM",
    )
    ee.add_argument(
        "name", nargs="?", help="the energy type or group, or the rule (rule-*)"
    )
    ee.add_argument(
        "new_name", nargs="?", help="rename, rule-rename, rule-copy: the new name"
    )
    ee.add_argument("--group", action="store_true", help="an energy group instead")
    ee.add_argument("--year", help="planned/actual: the fiscal year's tab, as shown")
    ee.add_argument("--type", help="planned/actual: the energy type")
    ee.add_argument("--ratio", type=float, help="planned: ratio to last year (%%)")
    ee.add_argument("--per-month", choices=["yes", "no"], help="planned: per month")
    ee.add_argument("--co2", type=float, help="actual: the CO2 factor")
    ee.add_argument("--month", action="append", help="MONTH=VALUE, as listed")
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


ENERGY_COMMANDS: dict[str, Callable[[ItmClient, argparse.Namespace], Awaitable[None]]]
ENERGY_COMMANDS = {
    "ppd": _ppd,
    "energy": _energy,
    "energy-edit": _energy_edit,
    "export": _export,
    "backup": _backup,
}
