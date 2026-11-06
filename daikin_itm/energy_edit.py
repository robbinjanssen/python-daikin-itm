# ruff: noqa: SLF001
# pylint: disable=protected-access
"""The energy navigator's settings: writes.

Session-level flows on the energy navigator's setting screens, each saving through
the session's ``write`` / ``press_ok`` (a dry run plans the one request that saves
and cancels; ``read_only`` refuses before anything is sent) and leaving every screen
it opened with its Close or Cancel:

- the planned consumption of a fiscal year (3200 -> 3201 -> 3202): yearly or per
  month, the ratio to the previous year and the target per month, then OK;
- the actual consumption entered by hand (3203): the value per month and the CO2
  factor, then OK;
- creating, renaming and deleting energy types (3205) and groups (3207), behind the
  budget overview's display settings (3200 -> 3204);
- creating, renaming, deleting and copying management rules (3213; Copy asks the
  new rule's name, a calendar copy goes through 3216) and a rule day's time ranges
  (3214 -> 3217:
  the day's rows are deleted and the wanted ranges added, 3217's OK hands them to
  the editor, whose OK saves the rule);
- behind the service login, the installer's settings (3220): a point's room
  temperature source (3221, with 3222 for the analog input), the month data
  collection starts (3223) and the energy conversion's texts (3224). 3220's data
  clear button (part 9) is never clicked.

The expected yearly consumption of 3202 has no input of its own (the web UI shows it
as text only), so it is not written. A button the iTM shows disabled (``ENABLED 0``)
is never clicked: the flow raises instead. Every write is confirmed by the client
with a read in a fresh session (``confirm_*``).

Validated on a real iTM: the energy types, groups and rules (create, rename,
delete, copy) and a rule day's ranges with their setpoints. The rest follows the web
UI's script and the part IDs it lists (FEATURES.md has the status per screen).
"""

from __future__ import annotations

import asyncio
import html
import itertools
import logging
import math
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import aiohttp

from . import energy_navigator as en
from ._bases import ClientCore
from ._screens import click as _click
from ._screens import close as _close
from ._screens import ensure_enabled as _ensure_enabled
from ._screens import enter_number as _number
from ._screens import open_below as _open_below
from ._screens import press_ok as _press_ok
from ._screens import select as _select
from ._util import ignore as _ignore
from .exceptions import ItmCommandError, ItmError, SessionEnded
from .protocol import DataType, EventType

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable, Mapping

    from .energy_navigator import (
        ActualYear,
        ConversionType,
        EnergyInstallerSettings,
        EnergyNavigator,
        PlannedYear,
        RoomTemperaturePoint,
        RuleDay,
        RuleRange,
    )
    from .protocol import Reply
    from .session import _Session
    from .state import UIState


# Planned consumption (3202).
PL_OK = 14
PL_TAB_INPUTS = ((8, 12), (22, 26))
# Actual consumption (3203).
AC_OK = 15
AC_TAB_INPUTS = ((6, 10), (22, 26))
_LOGGER = logging.getLogger(__name__)
RESTART_TIMEOUT, RESTART_POLL = 300, 10
# Energy type list (3205) and group list (3207): (3204 button, list screen).
LISTS = {False: (en.DS_TYPES, en.TYPE_LIST), True: (en.DS_GROUPS, en.GROUP_LIST)}


@dataclass(frozen=True)
class PlannedChange:
    """What to change in the planned consumption; None keeps a value."""

    year: str | None = None  # the fiscal year's tab name; None for the first tab
    energy_type: str | None = None  # None for the type the screen shows first
    ratio: float | None = None  # relative to the previous year, in %
    per_month: bool | None = None  # targets per month (True) or for the year
    months: Mapping[str, float] = field(default_factory=dict)  # month -> target


@dataclass(frozen=True)
class ActualChange:
    """What to change in the actual consumption; None keeps a value."""

    year: str | None = None
    energy_type: str | None = None
    co2_factor: float | None = None
    months: Mapping[str, float] = field(default_factory=dict)  # month -> value


# -- helpers ---------------------------------------------------------------------------


def _parts(screen: int) -> tuple[int, int]:
    """Return a plan screen's energy type combo and tab part."""
    if screen == en.PLANNED:
        return en.PL_TYPE, en.PL_TABS
    return en.AC_TYPE, en.AC_TABS


async def _pick_type(s: _Session, screen: int, energy_type: str | None) -> None:
    """Show another energy type (a display choice, like the browser's combo pick)."""
    combo = _parts(screen)[0]
    if energy_type is None or en.option(s.state, screen, combo) == energy_type:
        return
    options = [
        html.unescape(str(o)).strip()
        for o in s.state.get(screen, combo, "COMBO_STRS") or []
    ]
    if energy_type not in options:
        msg = f"energy type {energy_type!r} not offered: {options}"
        raise ItmCommandError(msg)
    _ensure_enabled(s, screen, combo, "energy type")
    index = options.index(energy_type)
    reply = await s.send(screen, combo, EventType.CLICK, DataType.INT, index)
    s.state.select_locally(screen, combo, index)
    await s.wait_for_progress(reply)


async def _show_tab(s: _Session, screen: int, year: str | None) -> int:
    """Show a fiscal year's tab (by its name; None for the first); return its index."""
    tabs = _parts(screen)[1]
    names = [n.strip() for n in en.tab_names(s.state, screen, tabs)]
    if year is None:
        index = 0
    elif year.strip() in names:
        index = names.index(year.strip())
    else:
        msg = f"fiscal year {year!r} not shown; the tabs are {names}"
        raise ItmCommandError(msg)
    reply = await s.send(screen, tabs, EventType.TAB_SELECT, DataType.INT, index)
    await s.wait_for_progress(reply)
    return index


async def _months(
    s: _Session, screen: int, parts: tuple[int, int], months: Mapping[str, float]
) -> None:
    """Select each month's row in the list and enter its value."""
    list_part, button = parts
    for month, value in months.items():
        rows = [
            r[0].strip() if r else ""
            for r in en.list_rows(s.state, screen, list_part)[0]
        ]
        if rows.count(month.strip()) != 1:
            msg = f"month {month!r} not listed once; the months are {rows}"
            raise ItmCommandError(msg)
        await _select(s, screen, list_part, rows.index(month.strip()))
        await _number(s, screen, button, value, f"month {month}")


def _close_to(shown: en.ShownValue | None, wanted: float) -> bool:
    """Tell whether a shown number is the wanted one, as the iTM rounds it."""
    if shown is None or shown.value is None:
        return False
    return math.isclose(shown.value, wanted, rel_tol=1e-3, abs_tol=5e-4)


def _check_months(
    shown: tuple[en.MonthValue, ...], wanted: Mapping[str, float], what: str
) -> None:
    values = {m.month: m.value for m in shown}
    for month, value in wanted.items():
        if not _close_to(values.get(month.strip()), value):
            now = values.get(month.strip())
            msg = f"{what} {month}: the iTM shows {now}, not {value}"
            raise ItmCommandError(msg)


async def _open_plan(s: _Session, button: int, screen: int) -> None:
    if not await en.open_plan_screen(s, button, screen, _parts(screen)[1]):
        msg = "the energy navigator did not open"
        raise ItmCommandError(msg)


# -- planned and actual consumption ----------------------------------------------------


async def set_planned(s: _Session, change: PlannedChange) -> bool:
    """Change a fiscal year's planned consumption (3202), then OK.

    Returns
    -------
        True when sent, False in a dry run.

    Raises
    ------
        ItmCommandError: A screen did not open, a value is not offered, an input is
            disabled, or the iTM refused.

    """
    s._writable()
    try:
        await _open_plan(s, en.PS_PLANNED, en.PLANNED)
        await _pick_type(s, en.PLANNED, change.energy_type)
        tab = await _show_tab(s, en.PLANNED, change.year)
        year_radio, month_radio, *_, month_list = en.PL_TAB_PARTS[tab]
        ratio_button, month_button = PL_TAB_INPUTS[tab]
        if change.per_month is not None:
            radio = month_radio if change.per_month else year_radio
            await _click(s, en.PLANNED, radio, "yearly or monthly")
        if change.ratio is not None:
            await _number(s, en.PLANNED, ratio_button, change.ratio, "ratio")
        await _months(s, en.PLANNED, (month_list, month_button), change.months)
        return await _press_ok(
            s, "save planned consumption", en.PLANNED, PL_OK, en.PL_CANCEL
        )
    finally:
        await en.leave_plan_screens(s, en.PLANNED, en.PL_CANCEL)


async def set_actual(s: _Session, change: ActualChange) -> bool:
    """Change a fiscal year's actual consumption (3203), then OK.

    Returns
    -------
        True when sent, False in a dry run.

    Raises
    ------
        ItmCommandError: A screen did not open, a value is not offered, an input is
            disabled, or the iTM refused.

    """
    s._writable()
    try:
        await _open_plan(s, en.PS_ACTUAL, en.ACTUAL)
        await _pick_type(s, en.ACTUAL, change.energy_type)
        tab = await _show_tab(s, en.ACTUAL, change.year)
        month_list = en.AC_TAB_PARTS[tab][0]
        month_button, co2_button = AC_TAB_INPUTS[tab]
        await _months(s, en.ACTUAL, (month_list, month_button), change.months)
        if change.co2_factor is not None:
            await _number(s, en.ACTUAL, co2_button, change.co2_factor, "CO2 factor")
        return await _press_ok(
            s, "save actual consumption", en.ACTUAL, AC_OK, en.AC_CANCEL
        )
    finally:
        await en.leave_plan_screens(s, en.ACTUAL, en.AC_CANCEL)


async def _read_year(
    s: _Session, button: int, screen: int, energy_type: str | None, year: str | None
) -> int:
    """Open a plan screen showing a type and a fiscal year (for a read-back)."""
    await _open_plan(s, button, screen)
    await _pick_type(s, screen, energy_type)
    return await _show_tab(s, screen, year)


async def confirm_planned(s: _Session, change: PlannedChange) -> en.PlannedYear:
    """Read the changed fiscal year back (3202, cancelled) and compare.

    Returns
    -------
        The fiscal year as the iTM shows it now.

    Raises
    ------
        ItmCommandError: It doesn't show the change.

    """
    try:
        tab = await _read_year(
            s, en.PS_PLANNED, en.PLANNED, change.energy_type, change.year
        )
        year = en.planned_from_screen(s.state).years[tab]
    finally:
        await en.leave_plan_screens(s, en.PLANNED, en.PL_CANCEL)
    if change.per_month is not None and year.per_month != change.per_month:
        msg = f"{year.name}: per month is {year.per_month}, not {change.per_month}"
        raise ItmCommandError(msg)
    if change.ratio is not None and not _close_to(year.ratio, change.ratio):
        msg = f"{year.name}: the ratio shows {year.ratio}, not {change.ratio}"
        raise ItmCommandError(msg)
    _check_months(year.months, change.months, year.name)
    return year


async def confirm_actual(s: _Session, change: ActualChange) -> en.ActualYear:
    """Read the changed fiscal year back (3203, cancelled) and compare.

    Returns
    -------
        The fiscal year as the iTM shows it now.

    Raises
    ------
        ItmCommandError: It doesn't show the change.

    """
    try:
        tab = await _read_year(
            s, en.PS_ACTUAL, en.ACTUAL, change.energy_type, change.year
        )
        year = en.actual_from_screen(s.state).years[tab]
    finally:
        await en.leave_plan_screens(s, en.ACTUAL, en.AC_CANCEL)
    if change.co2_factor is not None and not _close_to(
        year.co2_factor, change.co2_factor
    ):
        msg = f"{year.name}: the CO2 factor shows {year.co2_factor}"
        raise ItmCommandError(msg)
    _check_months(year.months, change.months, year.name)
    return year


# -- energy types and groups -----------------------------------------------------------


async def _open_list(s: _Session, *, group: bool) -> int:
    button, screen = LISTS[group]
    if not await en.open_display_list(s, button, screen):
        msg = "the energy navigator did not open"
        raise ItmCommandError(msg)
    return screen


async def _select_entry(s: _Session, screen: int, name: str) -> None:
    names = list(en.list_names(s.state, screen))
    if names.count(name.strip()) != 1:
        msg = f"{name!r} not listed once; listed: {names}"
        raise ItmCommandError(msg)
    await _select(s, screen, en.EL_LIST, names.index(name.strip()))


async def _answer_name(
    s: _Session, reply: Reply, screen: int, label: str, name: str
) -> bool:
    """Enter a name in the CHARINPUT dialog a button opened; False in a dry run."""
    dialogs = [
        u for u in reply.screens if u.update == "DLG_OPEN" and u.data[1] == "CHARINPUT"
    ]
    if not dialogs:
        msg = f"{label}: no name input opened: {reply.dialogs()}"
        raise ItmCommandError(msg)
    d = dialogs[0]
    low, high = d.data[4], d.data[5]
    if not low <= len(name) <= high:
        await s.send(d.screen, d.data[0], EventType.CLICK2)
        msg = f"{label}: a name is {low}-{high} characters: {name!r}"
        raise ItmCommandError(msg)
    sent = await s.write(
        label, d.screen, d.data[0], EventType.CLICK, DataType.STRING, string=name
    )
    if sent is None:  # dry run: cancel the name input
        await s.send(d.screen, d.data[0], EventType.CLICK2)
        return False
    await s._answer_confirm(screen, sent)
    return True


def _kind(*, group: bool) -> str:
    return "energy group" if group else "energy type"


async def create_entry(s: _Session, name: str, *, group: bool = False) -> bool:
    """Create an energy type (3205) or group (3207): Create, enter the name.

    Should the iTM open the new entry's settings (3206/3208) after the name, they are
    saved as it proposes them (OK).

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    screen = await _open_list(s, group=group)
    label = f"create {_kind(group=group)} {name}"
    setting, ok, cancel = (
        (en.GROUP_SETTING, en.GS_OK, en.GS_CANCEL)
        if group
        else (en.TYPE_SETTING, en.TS_OK, en.TS_CANCEL)
    )
    try:
        if name.strip() in en.list_names(s.state, screen):
            msg = f"{_kind(group=group)} {name!r} exists already"
            raise ItmCommandError(msg)
        reply = await _click(s, screen, en.EL_CREATE, label)
        sent = await _answer_name(s, reply, screen, label, name)
        if sent and setting in s.state.screens:
            await _press_ok(s, f"{label}: settings", setting, ok, cancel)
        return sent
    finally:
        await _close(s, setting, cancel)
        await en.leave_display_list(s, screen)


async def rename_entry(
    s: _Session, name: str, new_name: str, *, group: bool = False
) -> bool:
    """Rename an energy type or group: select it, Rename, enter the new name.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    screen = await _open_list(s, group=group)
    label = f"rename {_kind(group=group)} {name} to {new_name}"
    try:
        if new_name.strip() in en.list_names(s.state, screen):
            msg = f"{_kind(group=group)} {new_name!r} exists already"
            raise ItmCommandError(msg)
        await _select_entry(s, screen, name)
        reply = await _click(s, screen, en.EL_RENAME, label)
        return await _answer_name(s, reply, screen, label, new_name)
    finally:
        await en.leave_display_list(s, screen)


async def delete_entry(s: _Session, name: str, *, group: bool = False) -> bool:
    """Delete an energy type or group: select it, Delete, confirm.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    screen = await _open_list(s, group=group)
    label = f"delete {_kind(group=group)} {name}"
    try:
        await _select_entry(s, screen, name)
        _ensure_enabled(s, screen, en.EL_DELETE, label)
        reply = await s.write(label, screen, en.EL_DELETE)
        if reply is None:
            return False
        await s._answer_confirm(screen, reply)
        return True
    finally:
        await en.leave_display_list(s, screen)


async def read_names(s: _Session, *, group: bool = False) -> tuple[str, ...]:
    """Read the energy type or group names (3205/3207), then close (a read-back)."""
    screen = await _open_list(s, group=group)
    try:
        return en.list_names(s.state, screen)
    finally:
        await en.leave_display_list(s, screen)


# -- management rules (3213 -> 3214 -> 3216 / 3217) -----------------------------------

_HHMM = re.compile(r"^\s*(\d{1,2}):(\d{2})\s*$", re.ASCII)
DAY_END, RANGE_GAP = 24 * 60, 15


async def _rule_list(s: _Session) -> list[str]:
    await en.open_rules(s)
    return list(en.rules_from_screen(s.state))


async def _select_rule(s: _Session, names: list[str], name: str) -> None:
    if names.count(name.strip()) != 1:
        msg = f"rule {name!r} not listed once; the rules are {names}"
        raise ItmCommandError(msg)
    await _select(s, en.RULES, en.RL_LIST, names.index(name.strip()))


async def create_rule(s: _Session, name: str) -> bool:
    """Create a management rule (3213): Create, enter the name.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    label = f"create rule {name}"
    try:
        if name.strip() in await _rule_list(s):
            msg = f"rule {name!r} exists already"
            raise ItmCommandError(msg)
        reply = await _click(s, en.RULES, en.RL_CREATE, label)
        return await _answer_name(s, reply, en.RULES, label, name)
    finally:
        await en.leave_rules(s)


async def rename_rule(s: _Session, name: str, new_name: str) -> bool:
    """Rename a management rule: select it, Rename, enter the new name.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    label = f"rename rule {name} to {new_name}"
    try:
        names = await _rule_list(s)
        if new_name.strip() in names:
            msg = f"rule {new_name!r} exists already"
            raise ItmCommandError(msg)
        await _select_rule(s, names, name)
        reply = await _click(s, en.RULES, en.RL_RENAME, label)
        return await _answer_name(s, reply, en.RULES, label, new_name)
    finally:
        await en.leave_rules(s)


async def delete_rule(s: _Session, name: str) -> bool:
    """Delete a management rule: select it, Delete, confirm.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    label = f"delete rule {name}"
    try:
        await _select_rule(s, await _rule_list(s), name)
        _ensure_enabled(s, en.RULES, en.RL_DELETE, label)
        reply = await s.write(label, en.RULES, en.RL_DELETE)
        if reply is None:
            return False
        await s._answer_confirm(en.RULES, reply)
        return True
    finally:
        await en.leave_rules(s)


async def copy_rule(s: _Session, name: str, new_name: str) -> bool:
    """Copy a management rule under a new name: select it, Copy, enter the name.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    label = f"copy rule {name} to {new_name}"
    try:
        names = await _rule_list(s)
        if new_name.strip() in names:
            msg = f"rule {new_name!r} exists already"
            raise ItmCommandError(msg)
        await _select_rule(s, names, name)
        reply = await _click(s, en.RULES, en.RL_COPY, label)
        return await _answer_name(s, reply, en.RULES, label, new_name)
    finally:
        await en.leave_rules(s)


async def copy_rule_calendar(s: _Session, name: str, targets: tuple[str, ...]) -> bool:
    """Copy a rule's calendar to other rules (3216).

    The source is selected in the rule list, Calendar copy opens the copy screen,
    whose source text must show the rule; the targets are selected in its list (all
    at once) and OK copies.

    Returns
    -------
        True when sent, False in a dry run.

    Raises
    ------
        ItmCommandError: No target, a target not listed once or the source itself,
            a disabled button, or the iTM refused.

    """
    s._writable()
    screen = en.CALENDAR_COPY
    label = f"copy calendar of rule {name} to {', '.join(targets)}"
    wanted = [t.strip() for t in targets]
    if not wanted or name.strip() in wanted:
        msg = f"{label}: name other rules to copy to"
        raise ItmCommandError(msg)
    try:
        await _select_rule(s, await _rule_list(s), name)
        reply = await _click(s, en.RULES, en.RL_CALENDAR_COPY, label)
        prompts = [
            u
            for u in reply.screens
            if u.update == "DLG_OPEN" and u.data[1] == "CHARINPUT"
        ]
        if prompts:
            await s.send(prompts[0].screen, prompts[0].data[0], EventType.CLICK2)
            msg = f"{label}: the iTM asks for a name instead of opening {screen}"
            raise ItmCommandError(msg)
        await s.wait_for_progress(reply)
        if screen not in s.state.screens:
            msg = f"{label}: screen {screen} did not open: {reply.dialogs()}"
            raise ItmCommandError(msg)
        shown = html.unescape(
            str(s.state.get(screen, en.CC_FROM, "TEXT") or "")
        ).strip()
        if shown and shown != name.strip():
            msg = f"{label}: the copy screen copies from {shown!r}"
            raise ItmCommandError(msg)
        rows = [r[0] if r else "" for r in en.list_cells(s.state, screen, en.CC_LIST)]
        if any(rows.count(t) != 1 for t in wanted):
            msg = f"{label}: not every target is listed once; listed: {rows}"
            raise ItmCommandError(msg)
        picks = tuple(rows.index(t) for t in wanted)
        await s.send(
            screen,
            en.CC_LIST,
            EventType.CLICK,
            DataType.INTARRAY,
            len(picks),
            array=picks,
        )
        return await _press_ok(s, label, screen, en.CC_OK, en.CC_CANCEL)
    finally:
        await _close(s, screen, en.CC_CANCEL)
        await en.leave_rules(s)


def _hhmm(value: str) -> tuple[int, int]:
    m = _HHMM.match(value)
    if m is None:
        msg = f"a time is HH:MM, not {value!r}"
        raise ItmCommandError(msg)
    return int(m.group(1)), int(m.group(2))


async def _enter_time(s: _Session, button: int, value: str, what: str) -> None:
    """Enter a time of day in the HOURMIN dialog a 3217 button opens."""
    wanted = _hhmm(value)
    _ensure_enabled(s, en.RULE_SETTING, button, what)
    reply = await s.send(en.RULE_SETTING, button)
    dialogs = [
        u for u in reply.screens if u.update == "DLG_OPEN" and u.data[1] == "HOURMIN"
    ]
    if not dialogs:
        msg = f"{what}: no time input opened: {reply.dialogs()}"
        raise ItmCommandError(msg)
    d = dialogs[0]
    low, high = (d.data[6], d.data[7]), (d.data[8], d.data[9])
    if not low <= wanted <= high:
        await s.send(d.screen, d.data[0], EventType.CLICK2)
        msg = f"{what}: {value} outside the iTM's range {low}-{high}"
        raise ItmCommandError(msg)
    reply = await s.send(
        d.screen, d.data[0], EventType.CLICK, DataType.HOURMIN, 2, array=wanted
    )
    if reply.dialogs():
        msg = f"{what}: the iTM refused {value}: {reply.dialogs()}"
        raise ItmCommandError(msg)


async def _check(s: _Session, screen: int, part: int, *, on: bool, what: str) -> None:
    """Tick or untick a checkbox, only when it differs.

    A checkbox takes its new state as a number; the iTM ignores a plain click and
    doesn't report the new state back, so it is kept here.
    """
    if (s.state.get(screen, part, "CHECK_STATE") == 1) != on:
        s.refuse_disabled(screen, part, what)
        await s.send(screen, part, EventType.CLICK, DataType.INT, int(on))


def _minute_of_day(text: str, *, end: bool = False) -> int | None:
    m = _HHMM.match(text)
    if m is None:
        return None
    minute = int(m.group(1)) * 60 + int(m.group(2))
    return DAY_END if end and minute == 0 else minute


def _refuse_overlaps(ranges: tuple[en.RuleRange, ...]) -> None:
    spans = sorted(
        (_minute_of_day(r.start), _minute_of_day(r.end, end=True), r) for r in ranges
    )
    for (_, end, first), (start, _, second) in itertools.pairwise(spans):
        if start is not None and end is not None and start < end:
            msg = (
                f"time ranges {first.start}-{first.end} and {second.start}-"
                f"{second.end} overlap"
            )
            raise ItmCommandError(msg)


def _time_order(state: UIState, r: en.RuleRange) -> list[tuple[int, str, str]]:
    """Return the start and end inputs in an order the iTM accepts."""
    rs = en.RULE_SETTING
    shown_start = _minute_of_day(str(state.get(rs, en.RS_START, "TEXT") or ""))
    shown_end = _minute_of_day(str(state.get(rs, en.RS_END, "TEXT") or ""), end=True)
    start, end = _minute_of_day(r.start), _minute_of_day(r.end, end=True)
    start_time = (en.RS_START_BUTTON, r.start, "start")
    end_time = (en.RS_END_BUTTON, r.end, "end")
    if (
        shown_start is None
        or shown_end is None
        or start is None
        or end is None
        or start <= shown_end - RANGE_GAP
    ):
        return [start_time, end_time]
    if end >= shown_start + RANGE_GAP:
        return [end_time, start_time]
    return [(en.RS_END_BUTTON, "24:00", "end"), start_time, end_time]


async def _enter_range(s: _Session, r: en.RuleRange) -> None:
    """Fill 3217's edit group with a time range.

    The iTM keeps the start at least 15 minutes before the end shown, so the times
    go in an order it accepts; when neither order fits, the end is first moved to
    the end of the day.
    """
    rs = en.RULE_SETTING
    for button, hhmm, label in _time_order(s.state, r):
        await _enter_time(s, button, hhmm, label)
    managed = r.cool is not None or r.heat is not None
    await _check(s, rs, en.RS_SETPOINT, on=managed, what="setpoint")
    if not managed:
        return
    for value, check, button, what in (
        (r.cool, en.RS_COOL, en.RS_COOL_BUTTON, "cool"),
        (r.heat, en.RS_HEAT, en.RS_HEAT_BUTTON, "heat"),
    ):
        await _check(s, rs, check, on=value is not None, what=what)
        if value is not None:
            await _number(s, rs, button, value, f"{what} setpoint")


async def set_rule_day(
    s: _Session, rule: str, day: str, ranges: tuple[en.RuleRange, ...]
) -> bool:
    """Make a rule's day hold exactly these time ranges, then save the rule.

    The day's rows are deleted one by one (row 0 selected, Delete) and the ranges
    added (the edit group filled, Add); 3217's OK hands them to the rule editor and
    the editor's OK saves the rule (the one request a dry run plans instead).

    Returns
    -------
        True when sent, False in a dry run.

    Raises
    ------
        ItmCommandError: A screen did not open, a button is disabled, a value is
            outside the iTM's range, or the iTM refused.

    """
    s._writable()
    for r in ranges:
        _hhmm(r.start)
        _hhmm(r.end)
    rs = en.RULE_SETTING
    label = f"save rule {rule} ({day})"
    try:
        await en.open_rule(s, rule)
        if not await en.open_rule_day(s, day):
            msg = f"rule {rule}: the iTM does not offer editing {day}"
            raise ItmCommandError(msg)
        while rows := en.list_rows(s.state, rs, en.RS_LIST)[0]:
            await _select(s, rs, en.RS_LIST, 0)
            reply = await _click(s, rs, en.RS_DELETE, "delete time range")
            await s._answer_confirm(rs, reply)
            if len(en.list_rows(s.state, rs, en.RS_LIST)[0]) >= len(rows):
                msg = f"{label}: a time range was not deleted"
                raise ItmCommandError(msg)
        for r in ranges:
            await _enter_range(s, r)
            reply = await _click(s, rs, en.RS_ADD, f"add {r.start}-{r.end}")
            await s._answer_confirm(rs, reply)
        await s._answer_confirm(rs, await _click(s, rs, en.RS_OK, label))
        if rs in s.state.screens:
            msg = f"{label}: the rule settings did not close"
            raise ItmCommandError(msg)
        _ensure_enabled(s, en.RULE_EDITOR, en.RE_OK, label)
        saved = await s.write(label, en.RULE_EDITOR, en.RE_OK)
        if saved is None:
            return False
        await s._answer_confirm(en.RULE_EDITOR, saved)
        if en.RULE_EDITOR in s.state.screens:
            msg = f"{label}: the rule editor did not close"
            raise ItmCommandError(msg)
        return True
    finally:
        await en.leave_rules(s)


def same_ranges(
    shown: tuple[en.RuleRange, ...], wanted: tuple[en.RuleRange, ...]
) -> bool:
    """Tell whether the ranges read back are the wanted ones (times and setpoints)."""

    def same(a: en.RuleRange, b: en.RuleRange) -> bool:
        if _hhmm(a.start) != _hhmm(b.start) or _hhmm(a.end) != _hhmm(b.end):
            return False
        return all(
            (x is None) == (y is None) and (x is None or math.isclose(x, y or 0))
            for x, y in ((a.cool, b.cool), (a.heat, b.heat))
        )

    return len(shown) == len(wanted) and all(map(same, shown, wanted))


# -- the installer's settings (3220 -> 3221 / 3222 / 3223 / 3224) ---------------------


def check_service_change(
    *, dry_run: bool, allow_service_change: bool, allow_restart: bool
) -> None:
    """Refuse an installer setting change unless allowed (or a dry run).

    Saving one restarts the iTM (it says so in its confirm dialog, seen on a real
    iTM), so it needs allow_restart as well.

    Raises
    ------
        ItmCommandError: Not allowed.

    """
    if dry_run:
        return
    if not allow_service_change:
        msg = (
            "the energy navigator's installer settings change how it collects "
            "data; use a dry run, or pass allow_service_change=True"
        )
        raise ItmCommandError(msg)
    if not allow_restart:
        msg = (
            "saving the energy navigator's installer settings restarts the iTM; "
            "use a dry run, or pass allow_restart=True"
        )
        raise ItmCommandError(msg)


async def _open_installer(s: _Session, password: str, button: int, screen: int) -> None:
    await s.service_login(password)
    if not await en.open_installer(s):
        msg = "the energy navigator's installer settings did not open"
        raise ItmCommandError(msg)
    await _open_below(s, en.INSTALLER, button, screen)


def _row_named(state: UIState, screen: int, part: int, name: str) -> int:
    rows = [r[0] if r else "" for r in en.list_cells(state, screen, part)]
    if rows.count(name.strip()) != 1:
        msg = f"{name!r} not listed once; listed: {rows}"
        raise ItmCommandError(msg)
    return rows.index(name.strip())


async def set_room_temperature(
    s: _Session, password: str, point: str, analog_point: str | None
) -> bool:
    """Set the room temperature a point reports to the energy navigator (3221).

    The point's row is selected, then the return air temperature's radio button or
    (analog_point named) the analog input's, whose input is picked in 3222 (OK
    there hands it back); Change applies it to the row and OK saves. A dry run
    plans the input's OK and the Change instead of sending them.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    rt = en.ROOM_TEMP
    label = f"room temperature of {point}"
    try:
        await _open_installer(s, password, en.IN_ROOM_TEMP, rt)
        await _select(s, rt, en.RT_LIST, _row_named(s.state, rt, en.RT_LIST, point))
        radio = en.RT_OWN if analog_point is None else en.RT_ANALOG
        await _click(s, rt, radio, label)
        if analog_point is not None:
            await _open_below(s, rt, en.RT_ANALOG_BUTTON, en.ANALOG_SELECT)
            row = _row_named(s.state, en.ANALOG_SELECT, en.AS_LIST, analog_point)
            await _select(s, en.ANALOG_SELECT, en.AS_LIST, row)
            reply = await s.write(
                f"{label}: analog input {analog_point}", en.ANALOG_SELECT, en.AS_OK
            )
            if reply is None:
                await _close(s, en.ANALOG_SELECT, en.AS_CANCEL)
            else:
                await s._answer_confirm(en.ANALOG_SELECT, reply)
            if en.ANALOG_SELECT in s.state.screens:
                msg = f"{label}: the analog input selection did not close"
                raise ItmCommandError(msg)
        reply = await s.write(f"apply {label}", rt, en.RT_APPLY)
        if reply is not None:
            await s._answer_confirm(rt, reply)
        return await _press_ok(s, label, rt, en.RT_OK, en.RT_CANCEL)
    finally:
        await en.leave_installer(s)


async def set_collection_start(s: _Session, password: str, month: str) -> bool:
    """Set the month the energy navigator starts collecting data (3223), OK.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    cs = en.COLLECT_START
    try:
        await _open_installer(s, password, en.IN_COLLECT, cs)
        _, months = en.collection_from_screen(s.state)
        if months.count(month.strip()) != 1:
            msg = f"month {month!r} not offered: {list(months)}"
            raise ItmCommandError(msg)
        index = months.index(month.strip())
        if en.option(s.state, cs, en.CM_MONTH) != month.strip():
            _ensure_enabled(s, cs, en.CM_MONTH, "collection start month")
            await s.send(cs, en.CM_MONTH, EventType.CLICK, DataType.INT, index)
            s.state.select_locally(cs, en.CM_MONTH, index)
        return await _press_ok(
            s, f"collection start month {month}", cs, en.CM_OK, en.CM_CANCEL
        )
    finally:
        await en.leave_installer(s)


async def set_conversion(s: _Session, password: str, change: en.ConversionType) -> bool:
    """Change the energy conversion's name and unit texts (3224), then OK.

    Returns
    -------
        True when sent, False in a dry run.

    """
    s._writable()
    cv = en.CONVERSION
    try:
        await _open_installer(s, password, en.IN_CONVERSION, cv)
        for key, (_, button) in en.CV_FIELDS.items():
            value = getattr(change, key)
            if value is not None:
                _ensure_enabled(s, cv, button, f"conversion {key}")
                await s.answer_text(cv, button, value, f"conversion {key}")
        return await _press_ok(s, "save energy conversion", cv, en.CV_OK, en.CV_CANCEL)
    finally:
        await en.leave_installer(s)


async def read_installer(s: _Session, password: str) -> en.EnergyInstallerSettings:
    """Read the installer's settings back (a service login first).

    Raises
    ------
        ItmCommandError: 3220 did not open.

    """
    await s.service_login(password)
    settings = await en.read_installer(s)
    if settings is None:
        msg = "the energy navigator's installer settings did not open"
        raise ItmCommandError(msg)
    return settings


class EnergyMixin(ClientCore):
    """ItmClient's energy navigator methods."""

    async def fetch_energy_navigator(
        self, only: Iterable[str] | None = None, *, service_password: str | None = None
    ) -> EnergyNavigator:
        """Read the energy navigator's screens, in one session. Read only.

        The budget overview (every tab), the planned and the actual consumption of
        the energy type shown, the management rule names and the sampling targets.
        Every screen is left with Close or Cancel; nothing is saved.

        Args:
        ----
            only: The EnergyNavigator fields to read; the others stay None. None
                reads the ones above; energy_types, energy_groups, details and
                rule_editor, rule_settings and installer (tested against the
                simulated iTM only) when named.
            service_password: The service password, for installer (behind the
                service login); sent once, never stored or logged.

        Returns:
        -------
            What was read; None for each part whose menu screen did not open.

        """
        wanted = en.wanted_parts(only)
        if "installer" in wanted and service_password is None:
            msg = "the installer's settings need service_password"
            raise ItmCommandError(msg)
        return await self._in_session(
            lambda s: en.read_energy_navigator(s, wanted, service_password)
        )

    async def set_planned_consumption(  # pylint: disable=too-many-arguments
        self,
        *,
        year: str | None = None,
        energy_type: str | None = None,
        ratio: float | None = None,
        per_month: bool | None = None,
        months: Mapping[str, float] | None = None,
    ) -> PlannedYear | None:
        """Change a fiscal year's planned consumption (3202) and OK.

        Args:
        ----
            year: The fiscal year's tab, as shown; None for the first.
            energy_type: The energy type; None for the one the screen shows first.
            ratio: The ratio to the previous year (%).
            per_month: Targets per month (True) or for the whole year (False).
            months: Targets per month, by the month as listed.

        Returns:
        -------
            The fiscal year as read back in a fresh session; None in a dry run.

        """
        self._experimental("set the energy navigator's planned consumption")
        change = PlannedChange(year, energy_type, ratio, per_month, dict(months or {}))
        await self._system_write(lambda s: _ignore(set_planned(s, change)))
        if self.dry_run:
            return None
        return await self._in_session(lambda s: confirm_planned(s, change))

    async def set_actual_consumption(
        self,
        *,
        year: str | None = None,
        energy_type: str | None = None,
        co2_factor: float | None = None,
        months: Mapping[str, float] | None = None,
    ) -> ActualYear | None:
        """Enter a fiscal year's actual consumption (3203) and OK.

        Args:
        ----
            year: The fiscal year's tab, as shown; None for the first.
            energy_type: The energy type; None for the one the screen shows first.
            co2_factor: The CO2 factor.
            months: Values per month, by the month as listed.

        Returns:
        -------
            The fiscal year as read back in a fresh session; None in a dry run.

        """
        self._experimental("set the energy navigator's actual consumption")
        change = ActualChange(year, energy_type, co2_factor, dict(months or {}))
        await self._system_write(lambda s: _ignore(set_actual(s, change)))
        if self.dry_run:
            return None
        return await self._in_session(lambda s: confirm_actual(s, change))

    async def _energy_names(self, *, group: bool) -> tuple[str, ...]:
        return await self._in_session(lambda s: read_names(s, group=group))

    async def create_energy_type(self, name: str, *, group: bool = False) -> None:
        """Create an energy type (3205), or with group=True an energy group (3207).

        Args:
        ----
            name: The new name.
            group: Work on the energy groups instead.

        """
        await self._system_write(lambda s: _ignore(create_entry(s, name, group=group)))
        if not self.dry_run and name not in await self._energy_names(group=group):
            msg = f"{name}: the iTM did not create it"
            raise ItmCommandError(msg)

    async def rename_energy_type(
        self, name: str, new_name: str, *, group: bool = False
    ) -> None:
        """Rename an energy type, or with group=True an energy group.

        Args:
        ----
            name: The current name.
            new_name: The new name.
            group: Work on the energy groups instead.

        """
        await self._system_write(
            lambda s: _ignore(rename_entry(s, name, new_name, group=group))
        )
        if self.dry_run:
            return
        names = await self._energy_names(group=group)
        if new_name not in names or name in names:
            msg = f"{name}: the iTM did not rename it to {new_name}"
            raise ItmCommandError(msg)

    async def delete_energy_type(self, name: str, *, group: bool = False) -> None:
        """Delete an energy type, or with group=True an energy group.

        Args:
        ----
            name: The name.
            group: Work on the energy groups instead.

        """
        await self._system_write(lambda s: _ignore(delete_entry(s, name, group=group)))
        if not self.dry_run and name in await self._energy_names(group=group):
            msg = f"{name}: the iTM did not delete it"
            raise ItmCommandError(msg)

    async def _rule_names(self) -> tuple[str, ...]:
        return await self._in_session(en.read_rules) or ()

    async def create_energy_rule(self, name: str) -> None:
        """Create a management rule (3213), confirmed by the rule list."""
        await self._system_write(lambda s: _ignore(create_rule(s, name)))
        if not self.dry_run and name.strip() not in await self._rule_names():
            msg = f"rule {name}: the iTM did not create it"
            raise ItmCommandError(msg)

    async def rename_energy_rule(self, name: str, new_name: str) -> None:
        """Rename a management rule, confirmed by the rule list."""
        await self._system_write(lambda s: _ignore(rename_rule(s, name, new_name)))
        if self.dry_run:
            return
        names = await self._rule_names()
        if new_name.strip() not in names or name.strip() in names:
            msg = f"rule {name}: the iTM did not rename it to {new_name}"
            raise ItmCommandError(msg)

    async def delete_energy_rule(self, name: str) -> None:
        """Delete a management rule, confirmed by the rule list."""
        await self._system_write(lambda s: _ignore(delete_rule(s, name)))
        if not self.dry_run and name.strip() in await self._rule_names():
            msg = f"rule {name}: the iTM did not delete it"
            raise ItmCommandError(msg)

    async def copy_energy_rule(self, name: str, new_name: str) -> None:
        """Copy a management rule under a new name, confirmed by the rule editor."""
        await self._system_write(lambda s: _ignore(copy_rule(s, name, new_name)))
        if self.dry_run:
            return
        rules = {r.name: r for r in await self._in_session(en.read_rule_editor) or ()}
        source, copied = rules.get(name.strip()), rules.get(new_name.strip())
        if (
            source is None
            or copied is None
            or (copied.weekdays, copied.special_days)
            != (source.weekdays, source.special_days)
        ):
            msg = f"rule {name}: the iTM did not copy it to {new_name}"
            raise ItmCommandError(msg)

    async def copy_energy_rule_calendar(
        self, name: str, targets: Iterable[str]
    ) -> None:
        """Copy a management rule's calendar to other rules (3216).

        Args:
        ----
            name: The rule to copy from.
            targets: The rules to copy to, as listed.

        Raises:
        ------
            ItmCommandError: A target's editor doesn't show the copy afterwards.

        """
        self._experimental("copy an energy navigator rule's calendar")
        wanted = tuple(t.strip() for t in targets)
        await self._system_write(lambda s: _ignore(copy_rule_calendar(s, name, wanted)))
        if self.dry_run:
            return
        rules = {r.name: r for r in await self._in_session(en.read_rule_editor) or ()}
        source = rules.get(name.strip())
        for target in wanted:
            got = rules.get(target)
            if source is None or got is None or got.calendar != source.calendar:
                msg = f"rule {target}: the iTM does not show the copy of {name}"
                raise ItmCommandError(msg)

    async def set_energy_rule_day(
        self, rule: str, day: str, ranges: Iterable[RuleRange]
    ) -> RuleDay | None:
        """Make a management rule's day hold exactly these time ranges (3217).

        Args:
        ----
            rule: The rule, as listed.
            day: A weekday ("sunday" .. "saturday") or a special day's name.
            ranges: The time ranges, with the setpoints they manage.

        Returns:
        -------
            The day as read back in a fresh session; None in a dry run.

        Raises:
        ------
            ItmCommandError: Ranges overlap (the iTM refuses those), or the iTM
                doesn't show the ranges afterwards.

        """
        wanted = tuple(ranges)
        _refuse_overlaps(wanted)
        await self._system_write(lambda s: _ignore(set_rule_day(s, rule, day, wanted)))
        if self.dry_run:
            return None
        shown = await self._in_session(lambda s: en.read_rule_day(s, rule, day))
        if not same_ranges(shown.ranges, wanted):
            msg = f"rule {rule} {day}: the iTM shows {shown.ranges}"
            raise ItmCommandError(msg)
        return shown

    async def _installer_write(
        self,
        password: str,
        call: Callable[[_Session], Awaitable[bool]],
        *,
        allow_service_change: bool,
        allow_restart: bool,
    ) -> EnergyInstallerSettings | None:
        self._writable()
        check_service_change(
            dry_run=self.dry_run,
            allow_service_change=allow_service_change,
            allow_restart=allow_restart,
        )
        try:
            await self._in_session(lambda s: _ignore(call(s)))
        except (SessionEnded, TimeoutError, aiohttp.ClientError) as err:
            if self.dry_run:
                raise
            _LOGGER.info("the iTM restarts after saving: %s", err)
        if self.dry_run:
            return None
        await self._wait_until_back()
        return await self._in_session(lambda s: read_installer(s, password))

    async def _wait_until_back(self) -> None:
        """Wait until the iTM answers a login again after its restart."""
        deadline = time.monotonic() + RESTART_TIMEOUT
        while True:
            await asyncio.sleep(RESTART_POLL)
            try:
                await self.fetch_info()
            except (ItmError, TimeoutError, aiohttp.ClientError) as err:
                if time.monotonic() >= deadline:
                    msg = f"the iTM did not come back after its restart: {err}"
                    raise ItmCommandError(msg) from err
            else:
                return

    async def set_energy_room_temperature(
        self,
        password: str,
        point: str,
        *,
        analog_point: str | None = None,
        allow_service_change: bool = False,
        allow_restart: bool = False,
    ) -> RoomTemperaturePoint | None:
        """Set a point's room temperature source for the energy navigator (3221).

        Behind the service login; refused unless allow_service_change and
        allow_restart (or a dry run): saving restarts the iTM, and the read-back
        waits until it is back. The password is sent once, never stored or logged.

        Args:
        ----
            password: The service password.
            point: The point, as listed.
            analog_point: An analog input to take it from; None for the unit's
                return air temperature.
            allow_service_change: Really change the installer setting.
            allow_restart: Accept that saving restarts the iTM.

        Returns:
        -------
            The point as read back; None in a dry run.

        """
        self._experimental("set the energy navigator's room temperature point")
        settings = await self._installer_write(
            password,
            lambda s: set_room_temperature(s, password, point, analog_point),
            allow_service_change=allow_service_change,
            allow_restart=allow_restart,
        )
        if settings is None:
            return None
        found = [p for p in settings.room_temperature or () if p.name == point.strip()]
        analog = None if analog_point is None else analog_point.strip()
        if (
            not found
            or found[0].own_sensor is not (analog is None)
            or found[0].analog_point != analog
        ):
            msg = f"{point}: the iTM shows {found}"
            raise ItmCommandError(msg)
        return found[0]

    async def set_energy_collection_start(
        self,
        password: str,
        month: str,
        *,
        allow_service_change: bool = False,
        allow_restart: bool = False,
    ) -> str | None:
        """Set the month the energy navigator starts collecting data (3223).

        Behind the service login; refused unless allow_service_change and
        allow_restart (or a dry run): saving restarts the iTM, and the read-back
        waits until it is back.

        Args:
        ----
            password: The service password.
            month: The month, as the iTM lists it.
            allow_service_change: Really change the installer setting.
            allow_restart: Accept that saving restarts the iTM.

        Returns:
        -------
            The month as read back; None in a dry run.

        """
        self._experimental("set the energy navigator's collection start")
        settings = await self._installer_write(
            password,
            lambda s: set_collection_start(s, password, month),
            allow_service_change=allow_service_change,
            allow_restart=allow_restart,
        )
        if settings is None:
            return None
        if settings.collection_start != month.strip():
            msg = f"collection start: the iTM shows {settings.collection_start}"
            raise ItmCommandError(msg)
        return settings.collection_start

    async def set_energy_conversion(
        self,
        password: str,
        change: ConversionType,
        *,
        allow_service_change: bool = False,
        allow_restart: bool = False,
    ) -> ConversionType | None:
        """Change the energy conversion's name and unit texts (3224).

        Behind the service login; refused unless allow_service_change and
        allow_restart (or a dry run): saving restarts the iTM, and the read-back
        waits until it is back.

        Args:
        ----
            password: The service password.
            change: The texts to set; None keeps one.
            allow_service_change: Really change the installer setting.
            allow_restart: Accept that saving restarts the iTM.

        Returns:
        -------
            The conversion as read back; None in a dry run.

        """
        self._experimental("set the energy navigator's energy conversion")
        settings = await self._installer_write(
            password,
            lambda s: set_conversion(s, password, change),
            allow_service_change=allow_service_change,
            allow_restart=allow_restart,
        )
        if settings is None:
            return None
        shown = settings.conversion or en.ConversionType()
        for key in ("name", "one", "thousand", "million"):
            value = getattr(change, key)
            if value is not None and getattr(shown, key) != value.strip():
                msg = f"conversion {key}: the iTM shows {getattr(shown, key)!r}"
                raise ItmCommandError(msg)
        return shown
