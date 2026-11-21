"""Which events a schedule program runs on a date, from the program as read.

Pure functions over a ``ScheduleProgram`` read with ``fetch_schedule`` (with its
events): no iTM requests. A date outside the program's period has no events. A date a
calendar rule names runs that rule's special day pattern instead of its weekday's
events; when several rules name it, a date rule wins over an nth-weekday rule, and
among rules of the same kind the one lowest in the calendar list wins, whether it is
for one month or for every month. Whether the program is enabled is left to the
caller.

The period and the calendar rules are read from ``ScheduleProgram.period_dates`` and
``ScheduleProgram.calendar_values``, which ``fetch_schedule`` fills by position in the
editor's own combos and option lists, so they don't depend on the display language.
A program without them (built by hand, or a rule whose words were in no option list)
falls back to the text with the English month, week and weekday words, and raises
ValueError when those don't fit either.

Observed on one real iTM (Dutch display language), by comparing with its day
overview for a throwaway program:

- a calendar row is [month, day, pattern], the day a number or "<week word> <weekday
  word>" in the words of the editor's option lists;
- the ranking of rules above (the iTM refuses a second rule identical in month and
  day, so only a rule for one month and one for every month, or "4th" and "Last",
  can name the same date in the same way);
- a special day replaces the weekday also when it has no events;
- "4th" and "Last" both name a date that is the fourth and the last of its weekday;
- the day overview shows a disabled program too, ignores the period (see
  ``ignore_period``), and reaches seven days ahead but not fourteen;
- a renamed special day shows its new name in the calendar rows and the pattern
  option list (Ex1..Ex5 there is read too, from the simulated iTM), and the iTM
  refuses a name another special day has (its name or its Ex key), so a name points
  to one special day;
- "1st".."4th" is the nth occurrence (days 1-7, 8-14, ...) and not a row of the
  month's calendar: a first Sunday in the calendar's second row used the "1st" rule;
- an enabled program doesn't run its weekday events on a date outside its period,
  and runs them on a date inside it, its first and last day included, also when the
  period runs over the new year (its end before its begin); a calendar rule naming a
  date outside the period doesn't run its special day either (the history shows each
  run, also of an event that changes nothing);
- the editor's month, week and weekday option lists keep the iTM's order in every
  display language (Jan..Dec then every month, 1st..4th then Last, Sunday first):
  seen in all ten.

Assumed, not observed:

- a day number a month lacks (31 in a 30-day month, 29 February) never matches;
- event times are the iTM's local wall-clock time; how the iTM runs an event in a
  DST gap or overlap is unknown (``events_between`` gives such an event once, as
  datetime builds it with fold 0).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from typing import TYPE_CHECKING

from .lang import calendar_month
from .menu import calendar_rule_values

if TYPE_CHECKING:
    from .menu import CalendarRule, ScheduleEvent, ScheduleProgram

_LAST_WEEK = 5
_WEEK = timedelta(days=7)


@dataclass(frozen=True)
class _Rule:
    month: int  # 1-12, 0 for every month
    day: int | None  # a date rule
    week: int | None  # or the nth (5: last) weekday of the month
    weekday: int | None
    pattern: str  # "Ex1".."Ex5"
    order: int

    def matches(self, day: date) -> bool:
        """Tell whether the rule names a date."""
        if self.month and self.month != day.month:
            return False
        if self.day is not None:
            return self.day == day.day
        if self.weekday != _weekday(day):
            return False
        if self.week == _LAST_WEEK:
            return (day + _WEEK).month != day.month
        return (day.day - 1) // 7 + 1 == self.week

    def rank(self) -> tuple[bool, int]:
        """Sort key: a date first, then the rule lowest in the calendar."""
        return self.day is None, -self.order


@dataclass(frozen=True)
class _Calendar:
    period: tuple[tuple[int, int], tuple[int, int]] | None
    rules: tuple[_Rule, ...]


def _weekday(day: date) -> int:
    return (day.weekday() + 1) % 7


def _month(text: str) -> int | None:
    text = text.strip()
    if text.isdigit():
        return int(text) if 1 <= int(text) <= 12 else None
    return calendar_month(text)


def _period_date(text: str) -> tuple[int, int]:
    month_text, _, day_text = text.partition("/")
    month = _month(month_text)
    if not month or not day_text.strip().isdigit():
        msg = (
            f"unreadable period date {text!r}: the words of its display language "
            "are not known"
        )
        raise ValueError(msg)
    return month, int(day_text)


def _rule(program: ScheduleProgram, rule: CalendarRule, order: int) -> _Rule:
    values = None
    if len(program.calendar_values) == len(program.calendar):
        values = program.calendar_values[order]
    if values is None:
        values = calendar_rule_values(rule, program.special_names)
    if values is None:
        msg = (
            f"{program.name}: unreadable calendar rule {rule}: the words of its "
            "display language are not known"
        )
        raise ValueError(msg)
    return _Rule(
        values.month, values.day, values.week, values.weekday, values.special, order
    )


def _calendar(program: ScheduleProgram) -> _Calendar:
    period = program.period_dates
    if period is None and program.period is not None:
        period = _period_date(program.period[0]), _period_date(program.period[1])
    rules = tuple(_rule(program, r, i) for i, r in enumerate(program.calendar))
    return _Calendar(period, rules)


def _day_pattern(
    calendar: _Calendar, day: date, *, ignore_period: bool = False
) -> int | str | None:
    if calendar.period is not None and not ignore_period:
        begin, end = calendar.period
        at = (day.month, day.day)
        inside = begin <= at <= end if begin <= end else at >= begin or at <= end
        if not inside:
            return None
    rules = sorted((r for r in calendar.rules if r.matches(day)), key=_Rule.rank)
    return rules[0].pattern if rules else _weekday(day)


def _time(text: str) -> time:
    hours, _, minutes = text.partition(":")
    if not (hours.isdigit() and minutes.isdigit()):
        msg = f"unreadable event time {text!r}"
        raise ValueError(msg)
    return time(int(hours), int(minutes))


def _events(
    program: ScheduleProgram, pattern: int | str | None
) -> tuple[ScheduleEvent, ...]:
    if pattern is None:
        return ()
    if isinstance(pattern, int):
        slots = program.weekly_slots.get(pattern, ())
        events = program.weekly.get(pattern)
    else:
        slots = program.special_slots.get(pattern, ())
        events = program.special.get(pattern)
    if events is None:
        if slots:
            msg = f"{program.name}: read without its events (fetch_schedule events)"
            raise ValueError(msg)
        return ()
    return tuple(sorted(events, key=lambda e: _time(e.time)))


def day_pattern(
    program: ScheduleProgram, day: date, *, ignore_period: bool = False
) -> int | str | None:
    """Return the pattern a program runs on a date.

    Args:
    ----
        program: The program, as fetch_schedule read it.
        day: The date.
        ignore_period: Leave the program's period out, as the iTM's day overview
            does: it shows a date's pattern outside the period too.

    Returns:
    -------
        The weekday (0 = Sunday) or, when a calendar rule names the date, the special
        day ("Ex1".."Ex5"); None outside the program's period.

    Raises:
    ------
        ValueError: The period or a calendar rule can't be read: neither its
            values nor its English words.

    """
    return _day_pattern(_calendar(program), day, ignore_period=ignore_period)


def events_on(
    program: ScheduleProgram, day: date, *, ignore_period: bool = False
) -> tuple[ScheduleEvent, ...]:
    """Return the events a program runs on a date, sorted by time.

    Args:
    ----
        program: The program, as fetch_schedule read it (with its events).
        day: The date.
        ignore_period: Leave the program's period out, as the iTM's day overview
            does: it shows a date's events outside the period too.

    Returns:
    -------
        The events of the weekday or special day the date uses; empty outside the
        program's period.

    Raises:
    ------
        ValueError: The program was read without its events, or its period, a
            calendar rule or an event time can't be read.

    """
    pattern = _day_pattern(_calendar(program), day, ignore_period=ignore_period)
    return _events(program, pattern)


def targets(program: ScheduleProgram) -> tuple[str, ...]:
    """Return the zones and units a program's events touch, each once.

    In the order they first appear: the weekdays from Sunday, then the special days,
    each day's events by time.

    Args:
    ----
        program: The program, as fetch_schedule read it (with its events).

    Returns:
    -------
        The targets as the events name them.

    Raises:
    ------
        ValueError: An event time can't be read.

    """
    days = [program.weekly[d] for d in sorted(program.weekly)]
    days += [program.special[label] for label in sorted(program.special)]
    return tuple(
        dict.fromkeys(
            e.target
            for events in days
            for e in sorted(events, key=lambda e: _time(e.time))
        )
    )


def next_event(
    program: ScheduleProgram, after: datetime, *, days: int = 366
) -> tuple[datetime, ScheduleEvent] | None:
    """Return the first event a program runs after a moment, and when.

    Event times are the iTM's local wall-clock time, so ``after`` must be in the
    iTM's time zone: a naive local time, or an aware one with a ZoneInfo zone
    (``datetime.now(ZoneInfo("Europe/Amsterdam"))``). An aware time in UTC or with
    a fixed offset (``datetime.now().astimezone()``) gives event times shifted by
    that offset, or by an hour across a DST change. A time in a DST gap or overlap
    is returned as datetime builds it (fold 0).

    Args:
    ----
        program: The program, as fetch_schedule read it (with its events).
        after: The moment, in the iTM's local time (see above); an event at exactly
            this time doesn't count. Its tzinfo is given to the result.
        days: How many days after ``after``'s date to search.

    Returns:
    -------
        The event's date and time and the event, or None when there is none
        within ``days``.

    Raises:
    ------
        ValueError: The program was read without its events, or its period, a
            calendar rule or an event time can't be read.

    """
    calendar = _calendar(program)
    for offset in range(days + 1):
        day = after.date() + timedelta(days=offset)
        for event in _events(program, _day_pattern(calendar, day)):
            at = datetime.combine(day, _time(event.time), tzinfo=after.tzinfo)
            if at > after:
                return at, event
    return None


def events_between(
    program: ScheduleProgram, start: datetime, end: datetime
) -> list[tuple[datetime, ScheduleEvent]]:
    """Return the events a program runs from start up to end, and when.

    The program's period and special days count, as in events_on. Event times are
    the iTM's local wall-clock time, so pass start and end in the iTM's time zone,
    as for next_event: both naive local times, or both aware (a ZoneInfo zone for
    the iTM's). The results get start's tzinfo. Aware times are compared as
    moments, so a range across a DST change holds the right events. An event in
    a DST gap (02:30 on the day the clock jumps from 02:00 to 03:00) is given once,
    as datetime builds it (fold 0: the moment of 03:30); one in the overlap (02:30
    on the day it falls back) once too, at its first occurrence (fold 0). How the
    iTM itself runs those is unknown.

    Args:
    ----
        program: The program, as fetch_schedule read it (with its events).
        start: The first moment that counts (an event at exactly start does).
        end: The moment after the last that counts (an event at exactly end
            doesn't).

    Returns:
    -------
        The events by time, each with its date and time; empty when end is not
        after start.

    Raises:
    ------
        ValueError: One of start and end has a time zone and the other doesn't,
            or the program was read without its events, or its period, a
            calendar rule or an event time can't be read.

    """
    if (start.tzinfo is None) != (end.tzinfo is None):
        msg = "start and end must both be naive or both have a time zone"
        raise ValueError(msg)
    first, last = _moment(start), _moment(end)
    if last <= first:
        return []
    last_day = (end if end.tzinfo is None else end.astimezone(start.tzinfo)).date()
    calendar = _calendar(program)
    found: list[tuple[datetime, ScheduleEvent]] = []
    day = start.date()
    while day <= last_day:
        for event in _events(program, _day_pattern(calendar, day)):
            at = datetime.combine(day, _time(event.time), tzinfo=start.tzinfo)
            if first <= _moment(at) < last:
                found.append((at, event))
        day += timedelta(days=1)
    return sorted(found, key=lambda item: _moment(item[0]))


def _moment(when: datetime) -> datetime:
    """Return an aware time in UTC, so a DST change or fold compares right."""
    return when if when.tzinfo is None else when.astimezone(UTC)
