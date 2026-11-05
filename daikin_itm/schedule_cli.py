"""The ``itm`` schedule sub-commands: show, run on a day, edit."""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING, Any

from .cli_common import _out, _print_plan
from .menu import CalendarRule
from .models import PowerState, RemoteLock
from .schedule_edit import CalendarRuleSpec, EventSpec

if TYPE_CHECKING:
    import argparse
    from collections.abc import Awaitable, Callable

    from .client import ItmClient
    from .menu import ScheduleEvent, ScheduleSummary


def _print_schedules(programs: list[ScheduleSummary]) -> None:
    for p in programs:
        period = f"{p.period[0]}-{p.period[1]}" if p.period else "all year"
        _out(f"{p.name:24} {'enabled' if p.enabled else 'disabled':9} {period}")


async def _schedules(client: ItmClient, _args: argparse.Namespace) -> None:
    """Show the schedule programs."""
    _print_schedules(await client.fetch_schedules())


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


def add_schedule_commands(sub: Any) -> None:
    """Add `schedules`, `schedule`, `schedule-day` and `schedule-edit`."""
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
    _add_schedule_edit_command(sub)


SCHEDULE_COMMANDS: dict[str, Callable[[ItmClient, argparse.Namespace], Awaitable[None]]]
SCHEDULE_COMMANDS = {
    "schedules": _schedules,
    "schedule": _schedule,
    "schedule-day": _schedule_day,
    "schedule-edit": _schedule_edit,
}
