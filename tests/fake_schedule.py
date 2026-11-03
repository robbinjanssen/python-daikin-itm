# pylint: disable=too-many-instance-attributes
"""A simulated iTM that also edits schedule programs.

Extends FakeITM with the screens behind a program's editor that change it: new and
changed events (event screen 109 with the time input and the point/zone picker 110),
deleting events, the period, special day names and calendar rules. Like the real iTM,
every change stays in the editor until its OK and Yes; Cancel and Yes discard it.
Shapes follow the captures of a real iTM.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

from daikin_itm.protocol import PART_UPDATE_CODE, SCREEN_UPDATE_CODE, Writer

from .fake_itm import (
    EVENT_MODES,
    FakeAnalog,
    FakeITM,
    FakeVentilation,
    FakeZone,
    Update,
    _encode_data,
    _encode_part,
)

MONTHS = [
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
]
WEEKS = ["1st", "2nd", "3rd", "4th", "Last"]
WEEKDAYS = [
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
]
FAN_CYCLE = [3009, 3011, 3013, 3015]  # low, medium, high, auto

# Event screen 109: each setting's checkbox and the controls it enables.
SETTINGS = {
    2: (3, 4, 54, 55),  # power: start, stop, setback high, setback low
    7: (8, 9, 10),  # start/stop lock
    11: (12, 13),  # mode lock
    14: (15, 16),  # setpoint lock
    17: (18,),  # mode
    19: (20, 23),  # setpoint: fixed value and its "change" button
    26: (27,),  # overtime
    28: (29, 30),  # fan speed up/down
}
LOCK_KEYS = {7: 0, 11: 1, 14: 2}
HOURMIN_ID, NUMBER_ID, ERROR_ID, DELETE_ID, RENAME_ID = 56, 70, 57, 20, 115
CLEAR_ID, CALENDAR_COPY_ID = 116, 117


EVERY_MONTH = "Every"  # codespell:ignore alle - the iTM's "every month"


class FakeScheduleITM(FakeITM):  # pylint: disable=too-many-public-methods
    """A FakeITM whose schedule editor saves events, periods, names and rules."""

    def __init__(self) -> None:
        """Start like FakeITM, with special day names and no open event."""
        super().__init__()
        self.special_names: dict[str, list[str]] = {}
        self.event: dict[str, Any] | None = None  # the event screen's values
        self.event_index: int | None = None  # the edited event, None for a new one
        self.picker: dict[str, Any] | None = None
        self.period: list[int] | None = None  # [on, month, day, month, day] indexes
        self.calendar: dict[str, Any] = {}
        self.backup: dict[str, Any] | None = None  # the program as it was saved
        self.day_backup: list[dict[str, Any]] | None = None
        self.deleting_event = False
        self.renaming_special = False
        self.disabled: set[tuple[int, int]] = set()  # parts shown as ENABLED 0
        self.copy_source: str | None = None  # the program Copy copies
        self.copying_day = False
        self.copy_rows: list[int] = []
        self.clearing_day = False
        self.calendar_copies: list[tuple[str, list[str]]] = []
        self.calendar_targets: list[str] = []
        self.confirming_calendar = False

    # -- strictness -----------------------------------------------------------------
    def _enabled(
        self, screen: int, parts: dict[int, bool]
    ) -> list[tuple[int, str, Any]]:
        """Return ENABLED updates for parts, and remember the disabled ones."""
        out: list[tuple[int, str, Any]] = []
        for part, on in parts.items():
            if on:
                self.disabled.discard((screen, part))
            else:
                self.disabled.add((screen, part))
            out.append((part, "ENABLED", int(on)))
        return out

    def _menu(self, ev: dict[str, Any]) -> list[Update]:
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        if event == 1 and (screen, part) in self.disabled:
            msg = f"({screen}, {part}) clicked while disabled: the real iTM hangs here"
            raise RuntimeError(msg)
        if screen == 110 and 110 in self.open_screens:
            return self._picker_event(ev)
        if screen in {106, 108} and screen in self.open_screens:
            return self._copy_screen_event(ev)
        if screen == 107 and part == 10 and 107 in self.open_screens:
            if self.day_backup is not None:  # Cancel drops the list's changes
                self._day_list()[:] = self.day_backup
            self.day_backup = None
        if screen == 109 and part == 6:
            self.event = None
        return super()._menu(ev)

    # -- program state ----------------------------------------------------------------
    def _name(self) -> str:
        return str(self._program()[1])

    def _day_list(self) -> list[dict[str, Any]]:
        """Return the shown day's event list, stored so additions persist."""
        if self.editor_list == "weekly":
            days = self.schedule_events.setdefault(self._name(), {})
            return days.setdefault(self.editor_day, [])
        specials = self.special_events.setdefault(self._name(), {})
        return specials.setdefault(f"Ex{self.editor_special + 1}", [])

    def _names(self) -> list[str]:
        return self.special_names.setdefault(
            self._name(), [f"Ex{i + 1}" for i in range(5)]
        )

    def _snapshot(self) -> dict[str, Any]:
        name = self._name()
        return copy.deepcopy(
            {
                "weekly": self.schedule_events.get(name, {}),
                "special": self.special_events.get(name, {}),
                "names": self._names(),
                "calendar": self.calendar_rows,
            }
        )

    def _restore(self) -> None:
        if self.backup is None:
            return
        name = self._name()
        self.schedule_events[name] = self.backup["weekly"]
        self.special_events[name] = self.backup["special"]
        self.special_names[name] = self.backup["names"]
        self.calendar_rows = self.backup["calendar"]
        self.backup = None

    def _period_from_program(self) -> list[int]:
        _state, _name, start, end = self._program()
        if not start:
            return [0, 4, 0, 8, 29]
        d1, m1 = (int(x) for x in str(start).split("/"))
        d2, m2 = (int(x) for x in str(end).split("/"))
        return [1, m1 - 1, d1 - 1, m2 - 1, d2 - 1]

    def _editor_parts(self) -> list[tuple[int, str, Any]]:
        """Return FakeITM's editor parts plus the period, names and calendar."""
        parts = super()._editor_parts()
        if self.period is None:
            self.period = self._period_from_program()
        on = self.period[0] == 1
        parts += [(10, "CHECK_STATE", int(on))]
        for part, value in zip((16, 17, 18, 19), self.period[1:], strict=True):
            parts.append((part, "SINGLE_SELIDX", value))
        parts += self._enabled(104, dict.fromkeys((16, 17, 18, 19), on))
        parts += [(85 + i, "LABEL", n) for i, n in enumerate(self._names())]
        self.calendar = {"pattern": 0, "daily": True, "values": {}}
        parts += [
            (32, "COMBO_STRS", [f"Ex{i + 1}" for i in range(5)]),
            (32, "SINGLE_SELIDX", 0),
            (33, "CHECK_STATE", 1),
            (39, "CHECK_STATE", 0),
            (37, "COMBO_STRS", [*MONTHS, EVERY_MONTH]),
            (37, "SINGLE_SELIDX", 0),
            (38, "COMBO_NUMS", list(range(1, 32))),
            (38, "SINGLE_SELIDX", 0),
            (43, "COMBO_STRS", [*MONTHS, EVERY_MONTH]),
            (44, "COMBO_STRS", WEEKS),
            (46, "COMBO_STRS", WEEKDAYS),
        ]
        parts += self._enabled(
            104, {43: False, 44: False, 46: False, 27: False, 28: False, 29: False}
        )
        return parts

    def _calendar_list(self) -> tuple[int, str, Any]:
        rows = [{"text": r, "a": [-1] * 3, "b": [-1] * 3} for r in self.calendar_rows]
        return (26, "LIST_ROW", {"rows": rows, "selected": []})

    # -- copies and clearing ----------------------------------------------------------
    def _copy_event(self, ev: dict[str, Any]) -> list[Update] | None:
        """Route program copy, day copy, clear a day, event copy and calendar copy."""
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        if event != 1 and part not in {CLEAR_ID, 25}:
            return None
        handlers: dict[
            tuple[int, int], Callable[[dict[str, Any]], list[Update] | None]
        ] = {
            (100, 9): self._program_copy,
            (104, 25): self._save_copy,
            (104, 71): self._open_day_copy,
            (104, 100): self._open_day_copy,
            (104, 72): self._clear_day,
            (104, 101): self._clear_day,
            (104, CLEAR_ID): self._clear_day,
            (107, 6): self._event_copy,
            (100, 12): self._open_calendar_copy,
        }
        handler = handlers.get((screen, part))
        return handler(ev) if handler else None

    def _program_copy(self, ev: dict[str, Any]) -> list[Update]:
        source = self._program()
        self.copy_source = str(source[1])
        opened = self._schedule_create_delete({**ev, "part": 1}) or []
        on = source[0] == 0
        checks = [(7, "CHECK_STATE", int(on)), (8, "CHECK_STATE", int(not on))]
        return [*opened, ("UPDATE", 104, [], checks)]

    def _save_copy(self, ev: dict[str, Any]) -> list[Update] | None:
        if not (
            self.editor_saving
            and ev["event"] == 1
            and self.new_program is not None
            and self.copy_source is not None
        ):
            return None
        name, source = self.new_program, self.copy_source
        saved = super()._schedule_event(ev)
        copy_of = next(p for p in self.schedules if p[1] == source)
        enabled = self.pending_program_enabled
        if enabled is None:
            enabled = copy_of[0] == 0
        self.schedules[-1][0] = 0 if enabled else 3
        self.schedule_events[name] = copy.deepcopy(self.schedule_events.get(source, {}))
        self.special_events[name] = copy.deepcopy(self.special_events.get(source, {}))
        self.copy_source, self.pending_program_enabled = None, None
        return saved

    def _open_day_copy(self, ev: dict[str, Any]) -> list[Update]:
        self.open_screens.add(108)
        if ev["part"] == 71:
            names = [WEEKDAYS[i] for i in range(7) if i != self.editor_day]
        else:
            specials = enumerate(self._names())
            names = [n for i, n in specials if i != self.editor_special]
        rows = [{"text": [n], "a": [-1], "b": [-1]} for n in names]
        return [("OPEN", 108, [-1], [(4, "LIST_ROW", {"rows": rows, "selected": []})])]

    def _clear_day(self, ev: dict[str, Any]) -> list[Update] | None:
        if ev["part"] == CLEAR_ID:
            if not self.clearing_day:
                return None
            self.clearing_day = False
            if ev["event"] == 1:
                self._day_list().clear()
            return [("DLG_CLOSE", 104, [CLEAR_ID, 2], [])]
        if not self._day_list():
            msg = "clear clicked on an empty day: the real iTM hangs here"
            raise RuntimeError(msg)
        self.clearing_day = True
        confirm = [CLEAR_ID, 2, "Delete?", "Confirm", "Yes", "No"]
        return [("DLG_OPEN", 104, confirm, [])]

    def _event_copy(self, _ev: dict[str, Any]) -> list[Update]:
        if self.selected_event is None:
            msg = "Copy clicked while disabled: the real iTM hangs here"
            raise RuntimeError(msg)
        self.event = copy.deepcopy(self._day_list()[self.selected_event])
        self.event_index = None  # added as a new event on OK
        self.open_screens.add(109)
        return [("OPEN", 109, [-1], self._event_screen())]

    def _open_calendar_copy(self, _ev: dict[str, Any]) -> list[Update]:
        source = self._name()
        self.calendar_targets = [str(p[1]) for p in self.schedules if p[1] != source]
        self.open_screens.add(106)
        rows = [{"text": [n], "a": [-1], "b": [-1]} for n in self.calendar_targets]
        return [("OPEN", 106, [-1], [(4, "LIST_ROW", {"rows": rows, "selected": []})])]

    def _copy_screen_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update]:
        """Handle the day copy (108) and calendar copy (106) screens."""
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        if part == 4 and event == 1:
            self.copy_rows = list(ev["array"])
            return [("UPDATE", screen, [], [])]
        close: list[Update] = [("CLOSE", screen, [], [])]
        if screen == 108 and part == 5:
            others = list(range(7 if self.editor_list == "weekly" else 5))
            source = (
                self.editor_day if self.editor_list == "weekly" else self.editor_special
            )
            others.remove(source)
            events = copy.deepcopy(self._day_list())
            for row in self.copy_rows:
                if self.editor_list == "weekly":
                    days = self.schedule_events.setdefault(self._name(), {})
                    days[others[row]] = copy.deepcopy(events)
                else:
                    specials = self.special_events.setdefault(self._name(), {})
                    specials[f"Ex{others[row] + 1}"] = copy.deepcopy(events)
            self.open_screens.discard(108)
            return close
        if screen == 106 and part == 15 and event == 1:
            self.confirming_calendar = True
            confirm = [CALENDAR_COPY_ID, 2, "Copy?", "Confirm", "Yes", "No"]
            return [("DLG_OPEN", 106, confirm, [])]
        if screen == 106 and part == CALENDAR_COPY_ID and self.confirming_calendar:
            self.confirming_calendar = False
            if event != 1:
                return [("DLG_CLOSE", 106, [CALENDAR_COPY_ID, 2], [])]
            targets = [self.calendar_targets[r] for r in self.copy_rows]
            self.calendar_copies.append((self._name(), targets))
            self.open_screens.discard(106)
            return [("DLG_CLOSE", 106, [CALENDAR_COPY_ID, 2], []), *close]
        if part in {6, 16}:  # Cancel
            self.open_screens.discard(screen)
            return close
        return [("UPDATE", screen, [], [])]

    # -- the editor and its event lists ---------------------------------------------
    def _schedule_event(  # noqa: PLR0911, PLR0912  # pylint: disable=too-many-return-statements,too-many-branches
        self, ev: dict[str, Any]
    ) -> list[Update]:
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        copied = self._copy_event(ev)
        if copied is not None:
            return copied
        if screen == 100 and part == 11:
            self.period = None
            self.disabled = {k for k in self.disabled if k[0] not in {104, 107, 109}}
            opened = super()._schedule_event(ev)
            self.backup = self._snapshot()
            return opened
        if screen == 104:
            handled = self._editor_event(ev)
            if handled is not None:
                return handled
        if screen == 104 and part in {73, 102}:
            listed = super()._schedule_event(ev)
            self.day_backup = copy.deepcopy(self._day_list())
            return listed
        if screen == 107 and part == 5:
            self.event, self.event_index = {"time": "00:00", "target": ""}, None
            self.open_screens.add(109)
            return [("OPEN", 109, [-1], self._event_screen())]
        if screen == 107 and part == 8:
            if self.selected_event is None:
                msg = "Edit clicked while disabled: the real iTM hangs here"
                raise RuntimeError(msg)
            self.event_index = self.selected_event
            self.event = copy.deepcopy(self._day_list()[self.selected_event])
            self.open_screens.add(109)
            return [("OPEN", 109, [-1], self._event_screen())]
        if screen == 107 and part == 7:
            if self.selected_event is None:
                msg = "Delete clicked while disabled: the real iTM hangs here"
                raise RuntimeError(msg)
            self.deleting_event = True
            confirm = [DELETE_ID, 2, "Delete?", "Confirm", "Yes", "No"]
            return [("DLG_OPEN", 107, confirm, [])]
        if screen == 107 and part == DELETE_ID and self.deleting_event:
            self.deleting_event = False
            updates: list[Update] = [("DLG_CLOSE", 107, [DELETE_ID, 2], [])]
            if event == 1 and self.selected_event is not None:
                del self._day_list()[self.selected_event]
                self.selected_event = None
                rows = (4, "LIST_ROW", self._event_rows(self._day_list()))
                parts = [rows, *[(p, "ENABLED", 0) for p in (6, 7, 8)]]
                updates.append(("UPDATE", 107, [], parts))
            return updates
        if screen == 107 and part == 9:
            self.open_screens.discard(107)
            self.day_backup = None
            return [("CLOSE", 107, [], [])]
        if screen == 109:
            return self._event_screen_event(ev)
        return super()._schedule_event(ev)

    def _editor_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update] | None:
        """Handle the editor's save, discard, period, names and calendar."""
        part, event, number = ev["part"], ev["event"], ev["number"]
        if (
            part == 25
            and self.editor_saving
            and event == 1
            and self.new_program is None
        ):
            self.editor_saving = False
            self._save_program()
            self.open_screens.discard(104)
            return [("DLG_CLOSE", 104, [25, 2], []), ("CLOSE", 104, [], [])]
        if part == 24 and self.editor_confirming and event == 1:
            self._restore()
            return None  # FakeITM closes the editor
        if part == 10 and event == 1 and self.period is not None:
            self.period[0] = number
            return [
                (
                    "UPDATE",
                    104,
                    [],
                    self._enabled(104, dict.fromkeys((16, 17, 18, 19), number == 1)),
                )
            ]
        if part in {16, 17, 18, 19} and event == 1 and self.period is not None:
            self.period[part - 15] = number
            return [("UPDATE", 104, [], [])]
        if part == 103 and event == 1:
            self.renaming_special = True
            current = self._names()[self.editor_special]
            data = [RENAME_ID, 4, "Special day", current, 1, 15, 0, 0, 1]
            data += ["WordListEN.txt", "OK", "Cancel", *[""] * 12, ""]
            return [("DLG_OPEN", 104, data, [])]
        if part == RENAME_ID and self.renaming_special:
            self.renaming_special = False
            updates: list[Update] = [("DLG_CLOSE", 104, [RENAME_ID, 4], [])]
            if event == 1:
                self._names()[self.editor_special] = ev["string"]
                label = (85 + self.editor_special, "LABEL", ev["string"])
                updates.append(("UPDATE", 104, [], [label]))
            return updates
        return self._calendar_event(ev)

    def _calendar_event(self, ev: dict[str, Any]) -> list[Update] | None:
        part, event, number = ev["part"], ev["event"], ev["number"]
        cal = self.calendar
        if part in {33, 39} and event == 1:
            cal["daily"] = part == 33
            daily = cal["daily"]
            states = [
                (33, "CHECK_STATE", int(daily)),
                (39, "CHECK_STATE", int(not daily)),
            ]
            enabled = {
                37: daily,
                38: daily,
                43: not daily,
                44: not daily,
                46: not daily,
            }
            return [("UPDATE", 104, [], states + self._enabled(104, enabled))]
        if part in {32, 37, 38, 43, 44, 46} and event == 1:
            cal["values"][part] = number
            if part == 32:
                cal["pattern"] = number
            return [("UPDATE", 104, [], [])]
        if part == 49 and event == 1:
            values = cal["values"]
            if cal["daily"]:
                month = [*MONTHS, EVERY_MONTH][values.get(37, 0)]
                day = str(values.get(38, 0) + 1)
            else:
                month = [*MONTHS, EVERY_MONTH][values.get(43, 0)]
                day = f"{WEEKS[values.get(44, 0)]} {WEEKDAYS[values.get(46, 0)]}"
            self.calendar_rows.append([month, day, f"Ex{cal['pattern'] + 1}"])
            return [("UPDATE", 104, [], [self._calendar_list()])]
        if part == 26 and event == 1:
            row = ev["array"][0] if ev["array"] else -1
            cal["selected"] = row if 0 <= row < len(self.calendar_rows) else None
            on = cal["selected"] is not None
            return [
                ("UPDATE", 104, [], self._enabled(104, dict.fromkeys((27, 28, 29), on)))
            ]
        if part == 29 and event == 1 and cal.get("selected") is not None:
            del self.calendar_rows[cal["selected"]]
            cal["selected"] = None
            parts = [
                self._calendar_list(),
                *self._enabled(104, dict.fromkeys((27, 28, 29), False)),
            ]
            return [("UPDATE", 104, [], parts)]
        return None

    def _save_program(self) -> None:
        """Keep the editor's changes: the period and the enabled state."""
        program = self._program()
        if self.period is not None:
            on, m1, d1, m2, d2 = self.period
            program[2] = f"{d1 + 1:02d}/{m1 + 1:02d}" if on else ""
            program[3] = f"{d2 + 1:02d}/{m2 + 1:02d}" if on else ""
            if program[0] in {3, 4}:
                program[0] = 4 if on else 3
        if self.pending_program_enabled is not None:
            program[0] = 0 if self.pending_program_enabled else (4 if program[2] else 3)
            self.pending_program_enabled = None
        self.backup = None

    # -- the event screen (109) -------------------------------------------------------
    def _offers(self) -> set[int]:
        """Return the setting checkboxes the event's target offers."""
        kind = (self.event or {}).get("kind")
        if not (self.event or {}).get("target"):
            return set()
        if kind == "ventilation":
            return {2, 7}
        return set(SETTINGS)

    def _ticks(self) -> set[int]:
        e = self.event or {}
        ticks = set()
        for check, key in ((2, "power"), (17, "mode_idx"), (19, "setpoint")):
            if key in e:
                ticks.add(check)
        locks = e.get("locks", (None, None, None))
        ticks |= {c for c, i in LOCK_KEYS.items() if locks[i] is not None}
        if "overtime" in e:
            ticks.add(26)
        if "fan" in e:
            ticks.add(28)
        return ticks

    def _event_screen(self) -> list[tuple[int, str, Any]]:
        """Return every part of the event screen for the current values."""
        e = self.event or {}
        offers, ticks = self._offers(), self._ticks()
        parts: list[tuple[int, str, Any]] = [
            (43, "TEXT", e.get("time", "00:00")),
            (46, "TEXT", e.get("target", "")),
            (18, "COMBO_STRS", EVENT_MODES if offers else []),
            (18, "SINGLE_SELIDX", e.get("mode_idx", 0)),
            (21, "TEXT", f"{e.get('setpoint', 25.0):.1f}"),
            (27, "COMBO_STRS", ["On", "Off"] if offers else []),
            (27, "SINGLE_SELIDX", e.get("overtime", 1)),
            (31, "ICONID", e.get("fan", 3007)),
        ]
        enabled: dict[int, bool] = {}
        for check, controls in SETTINGS.items():
            parts.append((check, "CHECK_STATE", int(check in ticks)))
            parts.append((check, "VISIBLED", int(check in offers)))
            enabled[check] = check in offers
            for c in controls:
                enabled[c] = check in offers and check in ticks
        power = e.get("power")
        parts += [
            (p, "CHECK_STATE", int(i == power)) for i, p in enumerate(SETTINGS[2])
        ]
        locks = e.get("locks", (None, None, None))
        for check, i in LOCK_KEYS.items():
            parts += [
                (p, "CHECK_STATE", int(j == locks[i]))
                for j, p in enumerate(SETTINGS[check])
            ]
        parts.append((20, "CHECK_STATE", 1))
        return parts + self._enabled(109, enabled)

    def _event_screen_event(  # noqa: PLR0911, PLR0912  # pylint: disable=too-many-return-statements,too-many-branches
        self, ev: dict[str, Any]
    ) -> list[Update]:
        part, event, number = ev["part"], ev["event"], ev["number"]
        e = self.event
        if e is None:
            return []
        if part == 44:
            data = [HOURMIN_ID, 10, 4, "Time", 0, 0, 0, 0, 23, 59, "OK", "Cancel"]
            return [("DLG_OPEN", 109, [*data, *["x"] * 11], [])]
        if part == HOURMIN_ID:
            updates: list[Update] = [("DLG_CLOSE", 109, [HOURMIN_ID, 10], [])]
            if event == 1:
                hour, minute = ev["array"]
                e["time"] = f"{hour:02d}:{minute:02d}"
                updates.append(("UPDATE", 109, [], [(43, "TEXT", e["time"])]))
            return updates
        if part == 47:
            self.picker = {"tab": 0, "selected": None}
            self.open_screens.add(110)
            return [("OPEN", 110, [0], self._picker_parts())]
        if part == 23:
            data = [NUMBER_ID, 6, 2, "Setpoint", 25, 0, 16, 0, 32, 0, 2, 1, *["x"] * 11]
            return [("DLG_OPEN", 109, data, [])]
        if part == NUMBER_ID:
            if event == 1:
                e["setpoint"] = ev["array"][0] + ev["array"][1] / 1000
            return [("DLG_CLOSE", 109, [NUMBER_ID, 6], [])]
        if part in SETTINGS and event == 1:
            self._tick(part, on=number == 1)
            return [("UPDATE", 109, [], self._event_screen())]
        if part in SETTINGS[2]:
            e["power"] = SETTINGS[2].index(part)
        elif any(part in SETTINGS[c] for c in LOCK_KEYS):
            check = next(c for c in LOCK_KEYS if part in SETTINGS[c])
            locks = list(e.get("locks", (None, None, None)))
            locks[LOCK_KEYS[check]] = SETTINGS[check].index(part)
            e["locks"] = tuple(locks)
        elif part == 18:
            e["mode_idx"] = number
        elif part == 27:
            e["overtime"] = number
        elif part == 29:
            e["fan"] = FAN_CYCLE[(FAN_CYCLE.index(e.get("fan", 3015)) + 1) % 4]
        elif part == 5:
            return self._event_ok()
        else:
            return []
        return [("UPDATE", 109, [], self._event_screen())]

    def _tick(self, check: int, *, on: bool) -> None:
        e = self.event or {}
        defaults: dict[int, tuple[str, Any]] = {
            2: ("power", 0),
            17: ("mode_idx", 0),
            19: ("setpoint", 25.0),
            26: ("overtime", 1),
            28: ("fan", 3009),
        }
        if check in LOCK_KEYS:
            locks = list(e.get("locks", (None, None, None)))
            locks[LOCK_KEYS[check]] = 0 if on else None
            e["locks"] = tuple(locks)
            if all(lock is None for lock in locks):
                e.pop("locks")
        elif on:
            key, value = defaults[check]
            e.setdefault(key, value)
        else:
            e.pop(defaults[check][0], None)

    def _event_ok(self) -> list[Update]:
        e = self.event or {}
        if not e.get("target"):
            error = [ERROR_ID, 1, "No management point set.", "Error", "OK"]
            return [("DLG_OPEN", 109, error, [])]
        events = self._day_list()
        if self.event_index is None:
            events.append(e)
        else:
            events[self.event_index] = e
        events.sort(key=lambda x: x["time"])
        self.event, self.event_index, self.selected_event = None, None, None
        self.open_screens.discard(109)
        rows = (4, "LIST_ROW", self._event_rows(events))
        return [("CLOSE", 109, [], []), ("UPDATE", 107, [], [rows])]

    # -- the point/zone picker (110) --------------------------------------------------
    def _targets(self) -> tuple[list[tuple[str, str]], list[tuple[int, str]]]:
        """Return the picker's points (kind, name) and zones (level, name)."""
        points: list[tuple[str, str]] = [("Indoor", u.name) for u in self.units]
        points += [("Ventilator", v.name) for v in self.ventilation]
        zones: list[tuple[int, str]] = []

        def walk(zone: FakeZone, level: int) -> None:
            for child in zone.children:
                if isinstance(child, FakeZone):
                    zones.append((level, child.name))
                    walk(child, level + 1)

        walk(self.root, 1)
        return points, zones

    def _picker_parts(self) -> list[tuple[int, str, Any]]:
        points, zones = self._targets()

        def rows(cells: list[list[str]]) -> dict[str, Any]:
            return {
                "rows": [{"text": c, "a": [-1, -1], "b": [-1, -1]} for c in cells],
                "selected": [],
            }

        return [
            (1, "TAB_NAME", ["Mgmt. Point", "Zone"]),
            (2, "LIST_ROW", rows([[k, n] for k, n in points])),
            (5, "LIST_ROW", rows([[str(lv), n] for lv, n in zones])),
        ]

    def _picker_event(self, ev: dict[str, Any]) -> list[Update]:
        part, event = ev["part"], ev["event"]
        picker = self.picker or {}
        if part == 1 and event == 8:
            picker["tab"] = ev["number"]
            return [("UPDATE", 110, [], [])]
        if part in {2, 5} and event == 1:
            picker["selected"] = (part, ev["array"][0]) if ev["array"] else None
            return [("UPDATE", 110, [], [])]
        if part == 4:
            self.open_screens.discard(110)
            return [("CLOSE", 110, [], [])]
        if part == 3 and event == 1:
            if picker.get("selected") is None or self.event is None:
                error = [ERROR_ID, 1, "Nothing selected.", "Error", "OK"]
                return [("DLG_OPEN", 110, error, [])]
            list_part, row = picker["selected"]
            points, zones = self._targets()
            if list_part == 2:
                kind_label, name = points[row]
                kind = "ventilation" if kind_label == "Ventilator" else "unit"
            else:
                name, kind = zones[row][1], "zone"
            self.event["target"], self.event["kind"] = name, kind
            self.open_screens.discard(110)
            return [("CLOSE", 110, [], []), ("UPDATE", 109, [], self._event_screen())]
        return []

    # -- encoding: the HOURMIN dialog has shorts the generic encoder can't write ------
    @staticmethod
    def _reply(token: str | None, updates: list[Update]) -> str:
        w = Writer(token or "0" * 32)
        w.int(len(updates))
        for kind, screen, data, parts in updates:
            w.short(SCREEN_UPDATE_CODE[kind])
            w.short(screen)
            if kind == "DLG_OPEN" and data[1] == 10:  # HOURMIN: NS + 6 H + 13 S
                w.short(data[0])
                w.short(data[1])
                w.int(data[2])
                w.string(data[3])
                for n in data[4:10]:
                    w.short(n)
                for text in data[10:23]:
                    w.string(text)
            else:
                _encode_data(w, kind, data)
            if kind in {"OPEN", "UPDATE", "CLOSE", "SHOW", "HIDE"}:
                w.int(len(parts))
                for part, ptype, value in parts:
                    w.short(part)
                    w.short(PART_UPDATE_CODE[ptype])
                    _encode_part(w, ptype, value)
        return w.buf + "\0"


__all__ = ["FakeAnalog", "FakeScheduleITM", "FakeVentilation"]
