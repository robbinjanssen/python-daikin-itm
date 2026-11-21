"""The events a schedule program runs on a date, computed from the program as read."""

from __future__ import annotations

import html
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from daikin_itm import (
    CalendarRuleSpec,
    CalendarRuleValues,
    ScheduleEvent,
    ScheduleProgram,
    day_pattern,
    events_between,
    events_on,
    next_event,
    targets,
)
from daikin_itm.menu import (
    CalendarOptions,
    CalendarRule,
    MenuScreen,
    calendar_rule_values,
    program_from_editor,
)
from daikin_itm.protocol import Part, ScreenUpdate
from daikin_itm.state import UIState

from .fake_itm import FakeITM
from .fake_itm_base import ENGLISH_CALENDAR, OTHER_CALENDAR, CalendarWords
from .fake_schedule import FakeScheduleITM
from .test_client import FakeClient

MONDAY, TUESDAY, SUNDAY = date(2025, 1, 6), date(2025, 1, 7), date(2025, 1, 5)
TZ = timezone(timedelta(hours=1))


def _event(time: str, target: str, kind: str = "zone") -> ScheduleEvent:
    return ScheduleEvent(time=time, target=target, target_kind=kind, action="Start")


PROGRAM = ScheduleProgram(
    name="Program A",
    enabled=True,
    period=None,
    weekly_slots={1: (420, 1080), 2: (480,)},
    special_slots={"Ex1": (600,), "Ex2": (540,), "Ex3": (660,), "Ex4": (720,)},
    weekly={
        1: (
            _event("18:00", "Zone A"),
            _event("07:00", "Zone A"),
            _event("07:00", "1:1-01", "indoor"),
        ),
        2: (_event("08:00", "Zone B"),),
    },
    special={
        "Ex1": (_event("10:00", "Zone C"),),
        "Ex2": (_event("09:00", "Zone A"),),
        "Ex3": (_event("11:00", "Zone A"),),
        "Ex4": (_event("12:00", "Zone A"),),
    },
    special_names=("Ex1", "Ex2", "Ex3", "Ex4", "Ex5"),
)


def _times(events: tuple[ScheduleEvent, ...]) -> list[tuple[str, str]]:
    return [(e.time, e.target) for e in events]


def test_weekday_events_sorted_by_time() -> None:
    """A date without a rule runs its weekday's events, by time (ties as listed)."""
    assert day_pattern(PROGRAM, MONDAY) == 1
    assert _times(events_on(PROGRAM, MONDAY)) == [
        ("07:00", "Zone A"),
        ("07:00", "1:1-01"),
        ("18:00", "Zone A"),
    ]
    assert _times(events_on(PROGRAM, TUESDAY)) == [("08:00", "Zone B")]
    assert day_pattern(PROGRAM, SUNDAY) == 0
    assert not events_on(PROGRAM, SUNDAY)


def test_date_rule_replaces_the_weekday() -> None:
    """A calendar rule on a date runs its special day instead of the weekday."""
    program = replace(PROGRAM, calendar=(CalendarRule("Jan", "6", "Ex1"),))
    assert day_pattern(program, MONDAY) == "Ex1"
    assert _times(events_on(program, MONDAY)) == [("10:00", "Zone C")]
    assert day_pattern(program, date(2025, 1, 13)) == 1
    assert day_pattern(program, date(2026, 1, 6)) == "Ex1"
    every = replace(PROGRAM, calendar=(CalendarRule("Every", "6", "Ex2"),))
    assert day_pattern(every, date(2025, 2, 6)) == "Ex2"
    numbered = replace(PROGRAM, calendar=(CalendarRule("2", "6", "Ex2"),))
    assert day_pattern(numbered, date(2025, 2, 6)) == "Ex2"
    assert day_pattern(numbered, MONDAY) == 1


def test_special_day_without_events_runs_nothing() -> None:
    """A rule's special day with no events replaces the weekday with nothing."""
    program = replace(PROGRAM, calendar=(CalendarRule("Jan", "6", "Ex5"),))
    assert day_pattern(program, MONDAY) == "Ex5"
    assert not events_on(program, MONDAY)


def test_nth_and_last_weekday_rules() -> None:
    """A rule on the nth (or last) weekday of a month."""
    program = replace(
        PROGRAM,
        calendar=(
            CalendarRule("Every", "1st Tuesday", "Ex2"),
            CalendarRule("Mar", "Last Monday", "Ex1"),
            CalendarRule("Every", "2nd Sun", "Ex3"),
        ),
    )
    assert day_pattern(program, TUESDAY) == "Ex2"
    assert day_pattern(program, date(2025, 2, 4)) == "Ex2"
    assert day_pattern(program, date(2025, 1, 14)) == 2
    assert day_pattern(program, date(2025, 3, 31)) == "Ex1"
    assert day_pattern(program, date(2025, 3, 24)) == 1
    assert day_pattern(program, date(2025, 4, 28)) == 1
    assert day_pattern(program, date(2025, 1, 12)) == "Ex3"
    assert day_pattern(program, date(2025, 1, 19)) == 0


def test_renamed_special_day_in_a_rule() -> None:
    """A rule naming a renamed special day uses that pattern."""
    names = ("Holiday", "Ex2", "Ex3", "Ex4", "Ex5")
    program = replace(
        PROGRAM, special_names=names, calendar=(CalendarRule("Jan", "6", "Holiday"),)
    )
    assert day_pattern(program, MONDAY) == "Ex1"


def test_overlapping_rules_precedence() -> None:
    """A date beats a weekday; of the same kind the lowest in the calendar wins."""
    every_weekday = CalendarRule("Every", "1st Monday", "Ex4")
    every_date = CalendarRule("Every", "6", "Ex3")
    month_weekday = CalendarRule("Jan", "1st Monday", "Ex2")
    month_date = CalendarRule("Jan", "6", "Ex1")
    last_weekday = CalendarRule("Jan", "Last Monday", "Ex5")
    fourth_weekday = CalendarRule("Jan", "4th Monday", "Ex1")
    cases = (
        ((month_weekday, every_weekday), MONDAY, "Ex4"),
        ((every_weekday, month_weekday), MONDAY, "Ex2"),
        ((month_date, every_date), MONDAY, "Ex3"),
        ((every_date, month_date), MONDAY, "Ex1"),
        ((month_date, month_weekday), MONDAY, "Ex1"),
        ((month_weekday, month_date), MONDAY, "Ex1"),
        ((every_date, month_weekday), MONDAY, "Ex3"),
        ((fourth_weekday, last_weekday), date(2025, 1, 27), "Ex5"),
        ((last_weekday, fourth_weekday), date(2025, 1, 27), "Ex1"),
    )
    for rules, day, pattern in cases:
        assert day_pattern(replace(PROGRAM, calendar=rules), day) == pattern


def test_period_left_out() -> None:
    """The day overview's view: a date's pattern and events outside the period too."""
    program = replace(PROGRAM, period=("Feb/1", "Mar/31"))
    assert day_pattern(program, MONDAY) is None
    assert day_pattern(program, MONDAY, ignore_period=True) == day_pattern(
        PROGRAM, MONDAY
    )
    assert events_on(program, MONDAY, ignore_period=True) == events_on(PROGRAM, MONDAY)


def test_period() -> None:
    """Outside the period nothing runs; both ends are inside."""
    program = replace(PROGRAM, period=("Jun/1", "Aug/31"))
    assert not events_on(program, date(2025, 5, 26))
    assert day_pattern(program, date(2025, 5, 31)) is None
    assert day_pattern(program, date(2025, 6, 1)) == 0
    assert _times(events_on(program, date(2025, 6, 2)))[0] == ("07:00", "Zone A")
    assert day_pattern(program, date(2025, 8, 31)) == 0
    assert day_pattern(program, date(2025, 9, 1)) is None
    assert not events_on(program, date(2025, 9, 1))
    numbers = replace(PROGRAM, period=("6/1", "8/31"))
    assert day_pattern(numbers, date(2025, 9, 1)) is None
    assert day_pattern(numbers, date(2025, 8, 31)) == 0


def test_period_over_new_year() -> None:
    """A period whose end comes before its begin runs over the new year."""
    program = replace(PROGRAM, period=("Nov/1", "Feb/28"))
    assert day_pattern(program, MONDAY) == 1
    assert day_pattern(program, date(2025, 11, 1)) == 6
    assert day_pattern(program, date(2025, 3, 3)) is None
    assert day_pattern(program, date(2025, 10, 31)) is None


def test_period_wins_over_rules() -> None:
    """A calendar rule outside the period runs nothing."""
    program = replace(
        PROGRAM, period=("Jun/1", "Aug/31"), calendar=(CalendarRule("Jan", "6", "Ex1"),)
    )
    assert not events_on(program, MONDAY)


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"calendar": (CalendarRule("Janvier", "6", "Ex1"),)}, "calendar rule"),
        ({"calendar": (CalendarRule("13", "6", "Ex1"),)}, "calendar rule"),
        ({"calendar": (CalendarRule("Jan", "6", "Ex9"),)}, "calendar rule"),
        ({"calendar": (CalendarRule("Jan", "Monday", "Ex1"),)}, "calendar rule"),
        ({"calendar": (CalendarRule("Jan", "1st Lundi", "Ex1"),)}, "calendar rule"),
        ({"period": ("May", "Aug/31")}, "period date"),
        ({"period": ("Every/1", "Aug/31")}, "period date"),
        ({"weekly": {1: (_event("7h", "Zone A"),)}}, "event time"),
        ({"weekly": {}}, "without its events"),
        ({"special": {}, "calendar": (CalendarRule("Jan", "6", "Ex1"),)}, "without"),
    ],
)
def test_unreadable_programs_are_refused(change: dict[str, object], match: str) -> None:
    """What can't be read raises, rather than giving a wrong day."""
    with pytest.raises(ValueError, match=match):
        events_on(replace(PROGRAM, **change), MONDAY)  # type: ignore[arg-type]


def test_days_without_slots_need_no_events() -> None:
    """A program read without events still answers for a day without slots."""
    assert not events_on(replace(PROGRAM, weekly={}), SUNDAY)


def test_targets() -> None:
    """Every zone and unit the events touch, once, weekdays first."""
    assert targets(PROGRAM) == ("Zone A", "1:1-01", "Zone B", "Zone C")
    assert not targets(replace(PROGRAM, weekly={}, special={}))


def test_next_event() -> None:
    """The next event after a moment, over day boundaries and empty days."""
    assert next_event(PROGRAM, datetime(2025, 1, 6, 17, 0, tzinfo=TZ)) == (
        datetime(2025, 1, 6, 18, 0, tzinfo=TZ),
        _event("18:00", "Zone A"),
    )
    found = next_event(PROGRAM, datetime(2025, 1, 6, 18, 0, tzinfo=TZ))
    assert found is not None
    assert found[0] == datetime(2025, 1, 7, 8, 0, tzinfo=TZ)
    found = next_event(PROGRAM, datetime(2025, 1, 7, 9, 0, tzinfo=TZ))
    assert found is not None
    assert found[0] == datetime(2025, 1, 13, 7, 0, tzinfo=TZ)
    assert found[1].target == "Zone A"
    assert next_event(PROGRAM, datetime(2025, 1, 7, 9, 0, tzinfo=TZ), days=5) is None
    aware = next_event(PROGRAM, datetime(2025, 1, 6, 6, 0, tzinfo=UTC))
    assert aware is not None
    assert aware[0] == datetime(2025, 1, 6, 7, 0, tzinfo=UTC)
    empty = replace(PROGRAM, weekly={}, weekly_slots={})
    assert next_event(empty, datetime(2025, 1, 6, 9, 0, tzinfo=TZ)) is None


def test_next_event_follows_the_calendar_and_period() -> None:
    """A special day and the period decide the next event too."""
    program = replace(
        PROGRAM,
        period=("Jan/7", "Dec/31"),
        calendar=(CalendarRule("Jan", "13", "Ex1"),),
    )
    found = next_event(program, datetime(2025, 1, 6, 6, 0, tzinfo=TZ))
    assert found is not None
    assert found[0] == datetime(2025, 1, 7, 8, 0, tzinfo=TZ)
    found = next_event(program, datetime(2025, 1, 7, 9, 0, tzinfo=TZ))
    assert found is not None
    assert found[0] == datetime(2025, 1, 13, 10, 0, tzinfo=TZ)
    assert found[1].target == "Zone C"


AMSTERDAM = ZoneInfo("Europe/Amsterdam")


def _at(found: list[tuple[datetime, ScheduleEvent]]) -> list[tuple[str, str]]:
    return [(when.strftime("%m-%d %H:%M"), e.target) for when, e in found]


def test_events_between() -> None:
    """Start counts, end doesn't; over days, naive or aware, in time order."""
    start, end = (
        datetime(2025, 1, 6, 7, 0, tzinfo=TZ),
        datetime(2025, 1, 7, 8, 0, tzinfo=TZ),
    )
    found = events_between(PROGRAM, start, end)
    assert _at(found) == [
        ("01-06 07:00", "Zone A"),
        ("01-06 07:00", "1:1-01"),
        ("01-06 18:00", "Zone A"),
    ]
    assert all(when.tzinfo is TZ for when, _ in found)
    assert found[2][1] == _event("18:00", "Zone A")
    naive = events_between(
        PROGRAM, start.replace(tzinfo=None), end.replace(tzinfo=None, minute=1)
    )
    assert _at(naive) == [*_at(found), ("01-07 08:00", "Zone B")]
    assert all(when.tzinfo is None for when, _ in naive)
    assert _at(events_between(PROGRAM, start, end.astimezone(UTC))) == _at(found)
    assert not events_between(PROGRAM, end, start)
    assert not events_between(PROGRAM, start, start)


def test_events_between_naive_and_aware_mixed() -> None:
    """A range with a time zone on one end only is refused."""
    with pytest.raises(ValueError, match="both be naive or both have a time zone"):
        events_between(PROGRAM, datetime(2025, 1, 6), datetime(2025, 1, 7, tzinfo=TZ))  # noqa: DTZ001


def test_events_between_follows_the_period_and_special_days() -> None:
    """Outside the period nothing runs; a special day replaces its weekday."""
    program = replace(
        PROGRAM,
        period=("Jan/7", "Dec/31"),
        calendar=(CalendarRule("Jan", "13", "Ex1"),),
    )
    found = events_between(
        program, datetime(2025, 1, 6, tzinfo=TZ), datetime(2025, 1, 15, tzinfo=TZ)
    )
    assert _at(found) == [
        ("01-07 08:00", "Zone B"),
        ("01-13 10:00", "Zone C"),
        ("01-14 08:00", "Zone B"),
    ]


DST_PROGRAM = replace(
    PROGRAM,
    weekly_slots={0: (150, 195)},
    weekly={0: (_event("03:15", "Zone B"), _event("02:30", "Zone A"))},
)


def test_events_between_in_the_dst_gap() -> None:
    """An event in the hour the clock skips comes once, at fold 0's moment."""
    day = datetime(2025, 3, 30, tzinfo=AMSTERDAM)
    found = events_between(DST_PROGRAM, day, day + timedelta(days=1))
    assert [e.target for _, e in found] == ["Zone B", "Zone A"]
    gap = found[1][0]
    assert (gap.hour, gap.minute, gap.fold) == (2, 30, 0)
    assert gap.astimezone(UTC) == datetime(2025, 3, 30, 1, 30, tzinfo=UTC)
    assert found[0][0].astimezone(UTC) == datetime(2025, 3, 30, 1, 15, tzinfo=UTC)


def test_events_between_in_the_dst_overlap() -> None:
    """An event in the hour that runs twice comes once, at its first occurrence."""
    day = datetime(2025, 10, 26, tzinfo=AMSTERDAM)
    found = events_between(DST_PROGRAM, day, day + timedelta(days=1))
    assert [e.target for _, e in found] == ["Zone A", "Zone B"]
    first = found[0][0]
    assert first.fold == 0
    assert first.astimezone(UTC) == datetime(2025, 10, 26, 0, 30, tzinfo=UTC)
    second_pass = datetime(2025, 10, 26, 2, 15, fold=1, tzinfo=AMSTERDAM)
    later = events_between(DST_PROGRAM, second_pass, day + timedelta(days=1))
    assert [e.target for _, e in later] == ["Zone B"]
    utc = events_between(
        DST_PROGRAM,
        datetime(2025, 10, 26, 0, 0, tzinfo=UTC),
        datetime(2025, 10, 26, 1, 0, tzinfo=UTC),
    )
    assert not utc


async def test_simulated_program() -> None:
    """A program read from the simulated iTM: its calendar rule and weekdays."""
    itm = FakeITM()
    program = await FakeClient(itm).fetch_schedule("Always")
    assert program.period is None
    assert day_pattern(program, date(2025, 1, 1)) == "Ex1"
    assert _times(events_on(program, date(2025, 1, 1))) == [("08:00", "1:1-00")]
    assert _times(events_on(program, date(2025, 1, 2))) == [
        ("06:00", "Indoor units"),
        ("18:00", "Indoor units"),
    ]
    assert not events_on(program, date(2025, 1, 4))
    assert targets(program) == ("Indoor units", "1:1-00")


async def test_simulated_program_with_a_period() -> None:
    """A program with a period, read from the simulated iTM."""
    itm = FakeScheduleITM()
    itm.schedule_events["Program G"] = {
        1: [{"time": "07:30", "target": "Indoor units", "kind": "zone", "power": 0}]
    }
    program = await FakeClient(itm).fetch_schedule("Program G")
    assert program.period == ("Jun/1", "Aug/31")
    assert not events_on(program, date(2025, 5, 26))
    assert _times(events_on(program, date(2025, 6, 2))) == [("07:30", "Indoor units")]
    found = next_event(program, datetime(2025, 5, 20, 12, 0, tzinfo=TZ))
    assert found is not None
    assert found[0] == datetime(2025, 6, 2, 7, 30, tzinfo=TZ)


async def test_simulated_special_day_after_an_empty_one() -> None:
    """Ex2's events are read from Ex2 when Ex1 has none (its bar comes empty)."""
    itm = FakeITM()
    itm.special_events["Always"] = {
        "Ex2": [{"time": "09:00", "target": "1:1-04", "kind": "indoor", "power": 0}]
    }
    itm.calendar_rows = [["Jan", "1", "Ex2"]]
    program = await FakeClient(itm).fetch_schedule("Always")
    assert program.special_slots["Ex1"] == ()
    assert program.special_slots["Ex2"] == (540,)
    assert set(program.special) == {"Ex2"}
    assert _times(events_on(program, date(2025, 1, 1))) == [("09:00", "1:1-04")]


def test_next_event_in_the_itms_time_zone() -> None:
    """With the iTM's own zone, an event keeps its local time across a DST change."""
    zone = ZoneInfo("Europe/Amsterdam")
    found = next_event(PROGRAM, datetime(2025, 3, 29, 12, 0, tzinfo=zone))
    assert found is not None
    assert found[0] == datetime(2025, 3, 31, 7, 0, tzinfo=zone)
    assert found[0].utcoffset() == timedelta(hours=2)


def _calendar_itm(words: CalendarWords) -> FakeScheduleITM:
    """Return a simulated iTM showing Program G's calendar in a display language."""
    months, every = words.months, words.every
    weeks, weekdays = words.weeks, words.weekdays
    itm = FakeScheduleITM()
    itm.calendar_words = words
    itm.calendar_rows = [
        [months[0], "6", "Ex1"],
        [every, f"{weeks[4]} {weekdays[0]}", "Ex2"],
        [months[5], f"{weeks[1]} {weekdays[2]}", "Holiday"],
    ]
    itm.special_names["Program G"] = ["Ex1", "Ex2", "Holiday", "Ex4", "Ex5"]
    itm.schedule_events["Program G"] = {
        d: [{"time": "07:30", "target": "Indoor units", "kind": "zone", "power": 0}]
        for d in range(1, 6)
    }
    itm.special_events["Program G"] = {
        f"Ex{i}": [
            {"time": f"0{i}:00", "target": "Indoor units", "kind": "zone", "power": 1}
        ]
        for i in range(1, 4)
    }
    return itm


VALUES = (
    CalendarRuleValues(month=1, day=6, week=None, weekday=None, special="Ex1"),
    CalendarRuleValues(month=0, day=None, week=5, weekday=0, special="Ex2"),
    CalendarRuleValues(month=6, day=None, week=2, weekday=2, special="Ex3"),
)


@pytest.mark.parametrize("words", [ENGLISH_CALENDAR, OTHER_CALENDAR])
async def test_simulated_calendar_as_values(words: CalendarWords) -> None:
    """The period and calendar rules are read by position, in any display language."""
    program = await FakeClient(_calendar_itm(words)).fetch_schedule("Program G")
    assert program.period == (f"{words.months[5]}/1", f"{words.months[7]}/31")
    assert program.period_dates == ((6, 1), (8, 31))
    assert program.calendar[1].month == words.every
    assert program.calendar_values == VALUES


async def test_events_on_without_english_words() -> None:
    """Every date runs the same events whichever language the editor showed."""
    english = await FakeClient(_calendar_itm(ENGLISH_CALENDAR)).fetch_schedule(
        "Program G"
    )
    other = await FakeClient(_calendar_itm(OTHER_CALENDAR)).fetch_schedule("Program G")
    days = [date(2025, 1, 1) + timedelta(days=n) for n in range(365)]
    assert [day_pattern(other, d) for d in days] == [
        day_pattern(english, d) for d in days
    ]
    assert [events_on(other, d) for d in days] == [events_on(english, d) for d in days]
    assert day_pattern(other, date(2025, 6, 10)) == "Ex3"
    assert day_pattern(other, date(2025, 6, 29)) == "Ex2"
    assert day_pattern(other, date(2025, 6, 6)) == 5
    assert day_pattern(other, date(2025, 5, 6)) is None
    after = datetime(2025, 6, 9, 12, 0, tzinfo=TZ)
    assert next_event(other, after) == next_event(english, after)
    with pytest.raises(ValueError, match="display language"):
        events_on(replace(other, period_dates=None), date(2025, 6, 10))
    with pytest.raises(ValueError, match="display language"):
        events_on(replace(other, calendar_values=()), date(2025, 6, 10))


async def test_unknown_words_leave_a_rule_unread() -> None:
    """A row in no option list reads as None; only computing a day raises."""
    itm = _calendar_itm(OTHER_CALENDAR)
    itm.calendar_rows.append(["Sigma", "6", "Ex1"])
    program = await FakeClient(itm).fetch_schedule("Program G")
    assert program.calendar_values == (*VALUES, None)
    with pytest.raises(ValueError, match="display language are not known"):
        events_on(program, date(2025, 6, 10))


async def test_renamed_special_day_with_markup_characters() -> None:
    """A special day named with & and < is matched although the iTM escapes it."""
    itm = _calendar_itm(ENGLISH_CALENDAR)
    itm.special_names["Program G"][2] = "R&D <1>"
    itm.calendar_rows[2][2] = "R&D <1>"
    program = await FakeClient(itm).fetch_schedule("Program G")
    assert program.special_names[2] == "R&D <1>"
    assert program.calendar[2].pattern == "R&D <1>"
    assert program.calendar_values == VALUES
    assert day_pattern(program, date(2025, 6, 10)) == "Ex3"
    assert day_pattern(replace(program, calendar_values=()), date(2025, 6, 10)) == (
        "Ex3"
    )


class _NamingITM(FakeScheduleITM):
    """Shows renamed special days by name in the calendar rows or the pattern list."""

    def __init__(self, *, rows: bool, options: bool) -> None:
        super().__init__()
        self.named_rows, self.named_options = rows, options

    def _shown_rows(self) -> list[list[str]]:
        names = self._names()
        return [
            [*r[:2], names[int(r[2][2:]) - 1] if self.named_rows else r[2]]
            for r in self.calendar_rows
        ]

    def _calendar_list(self) -> tuple[int, str, Any]:
        rows = [
            {"text": [html.escape(c) for c in r], "a": [-1] * 3, "b": [-1] * 3}
            for r in self._shown_rows()
        ]
        return (26, "LIST_ROW", {"rows": rows, "selected": []})

    def _editor_parts(self) -> list[tuple[int, str, Any]]:
        parts = [
            p
            for p in super()._editor_parts()
            if p[:2] not in {(26, "LIST_ROW"), (32, "COMBO_STRS")}
        ]
        patterns = (
            self._names() if self.named_options else [f"Ex{i}" for i in range(1, 6)]
        )
        return [*parts, self._calendar_list(), (32, "COMBO_STRS", list(patterns))]


@pytest.mark.parametrize(
    ("rows", "options"), [(False, False), (True, False), (True, True), (False, True)]
)
async def test_special_day_renamed_through_the_client(
    *, rows: bool, options: bool
) -> None:
    """Rules added before and after a rename keep mapping to the renamed special day.

    The calendar rows and the pattern list may show the new name or Ex1..Ex5; each
    combination is simulated.
    """
    itm = _NamingITM(rows=rows, options=options)
    itm.calendar_rows = []
    itm.schedule_events["Program G"] = {
        d: [{"time": "07:30", "target": "Indoor units", "kind": "zone", "power": 0}]
        for d in range(7)
    }
    itm.special_events["Program G"] = {
        "Ex3": [{"time": "09:00", "target": "1:1-04", "kind": "indoor", "power": 1}]
    }
    client = FakeClient(itm)
    await client.add_schedule_calendar_rule(
        "Program G", CalendarRuleSpec(pattern=3, month=6, day=10)
    )
    renamed = await client.rename_schedule_special_day("Program G", "Ex3", "Holiday")
    assert renamed is not None
    assert renamed.special_names[2] == "Holiday"
    await client.add_schedule_calendar_rule(
        "Program G", CalendarRuleSpec(pattern=3, month=7, week=1, weekday=1)
    )
    program = await client.fetch_schedule("Program G")
    shown = "Holiday" if rows else "Ex3"
    assert [r.pattern for r in program.calendar] == [shown, shown]
    assert [v.special if v else None for v in program.calendar_values] == [
        "Ex3",
        "Ex3",
    ]
    for program_read in (program, replace(program, calendar_values=())):
        for day in (date(2025, 6, 10), date(2025, 7, 7)):
            assert day_pattern(program_read, day) == "Ex3"
            assert _times(events_on(program_read, day)) == [("09:00", "1:1-04")]
        assert day_pattern(program_read, date(2025, 7, 14)) == 1


def test_given_values_win_over_the_text() -> None:
    """A program with values uses them; without, or out of step, its text."""
    rule = CalendarRule("?", "?", "?")
    program = replace(PROGRAM, calendar=(rule,), calendar_values=(VALUES[0],))
    assert day_pattern(program, MONDAY) == "Ex1"
    stale = replace(program, calendar=(rule, CalendarRule("Jan", "6", "Ex2")))
    with pytest.raises(ValueError, match="calendar rule"):
        day_pattern(stale, MONDAY)
    dated = replace(PROGRAM, period=("?", "?"), period_dates=((1, 7), (12, 31)))
    assert day_pattern(dated, MONDAY) is None
    assert day_pattern(dated, TUESDAY) == 2


@pytest.mark.parametrize(
    ("rule", "options", "expected"),
    [
        (CalendarRule("Jan", "6", "Ex1"), None, VALUES[0]),
        (CalendarRule("Every", "Last Sun", "Ex2"), None, VALUES[1]),
        (CalendarRule("Omega", "Pos Z Dia A", "Ex2"), None, None),
        (CalendarRule("Omega", "Dia A Pos Z", "Ex2"), OTHER_CALENDAR, VALUES[1]),
        (CalendarRule("Zeta", "pos 2  dia c", "Ex3"), OTHER_CALENDAR, VALUES[2]),
        (CalendarRule("Jan", "6", "Ex1"), OTHER_CALENDAR, VALUES[0]),
        (CalendarRule("6", "2nd Tuesday", "Ex3"), OTHER_CALENDAR, VALUES[2]),
        (CalendarRule("13", "6", "Ex1"), OTHER_CALENDAR, None),
        (CalendarRule("Alpha", "6", "Holiday"), OTHER_CALENDAR, VALUES[0]),
        (CalendarRule("Alpha", "6", "Ex9"), OTHER_CALENDAR, None),
    ],
)
def test_calendar_rule_values(
    rule: CalendarRule, options: CalendarWords | None, expected: object
) -> None:
    """By position in the option lists, else by the English words."""
    lists = None
    if options is not None:
        lists = CalendarOptions(
            months=tuple(options.month_options),
            weeks=options.weeks,
            weekdays=options.weekdays,
            patterns=("Holiday", "Ex2", "Ex3", "Ex4", "Ex5"),
        )
    assert calendar_rule_values(rule, ("Ex1", "Ex2", "Ex3"), lists) == expected


ENGLISH, OTHER = list(ENGLISH_CALENDAR.months), list(OTHER_CALENDAR.months)
NUMBERS = [str(n) for n in range(1, 7)]


def _editor(*parts: tuple[int, str, object]) -> UIState:
    state = UIState()
    update = ScreenUpdate(
        MenuScreen.SCHEDULE_EDITOR,
        "OPEN",
        [0],
        [Part(10, "CHECK_STATE", 1), *(Part(*p) for p in parts)],
    )
    state.apply([update])
    return state


@pytest.mark.parametrize(
    ("parts", "expected"),
    [
        (
            [(16, "COMBO_STRS", OTHER), (17, "COMBO_NUMS", [1])],
            ((6, 1), (1, 1)),
        ),
        ([(16, "COMBO_STRS", ENGLISH[:6]), (17, "COMBO_NUMS", [1])], ((6, 1), (1, 1))),
        ([(16, "COMBO_STRS", NUMBERS), (17, "COMBO_STRS", ["1"])], ((6, 1), (1, 1))),
        ([(16, "COMBO_STRS", OTHER[:6]), (17, "COMBO_NUMS", [1])], None),
        ([(16, "COMBO_STRS", ENGLISH[:1]), (17, "COMBO_NUMS", [1])], None),
        ([(16, "COMBO_STRS", ENGLISH[:6]), (17, "COMBO_STRS", ["x"])], None),
    ],
)
def test_period_dates_fallbacks(
    parts: list[tuple[int, str, object]],
    expected: tuple[tuple[int, int], tuple[int, int]] | None,
) -> None:
    """The period's numbers come from the combos' values or positions, else None."""
    begin = [(16, "SINGLE_SELIDX", 5), (17, "SINGLE_SELIDX", 0)]
    end = [(18, "COMBO_STRS", ["Jan"]), (18, "SINGLE_SELIDX", 0)]
    end += [(19, "COMBO_NUMS", [1]), (19, "SINGLE_SELIDX", 0)]
    program = program_from_editor(_editor(*parts, *begin, *end))
    assert program.period is not None
    assert program.period_dates == expected
