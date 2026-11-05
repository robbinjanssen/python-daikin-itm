# pylint: disable=protected-access
"""A simulated iTM with the energy navigator's rule and installer screens.

On top of FakeEnergyEditITM: the management rule list (3213) creates, renames and
deletes rules, copies one under a name it asks for, and opens the calendar copy
(3216); the
rule editor (3214) takes a day's radio button, whose Edit (disabled until one is
picked) opens that day's time ranges (3217). 3217 takes HOURMIN and NUMBER inputs,
checkboxes, Add, Modify and Delete; its OK hands the ranges to the editor, whose OK
(after a CONFIRM) saves the rule.

Behind the service login, the service tab's button 106 opens the installer's
settings (3220, as seen on a real iTM): room temperature (3221, its radio buttons
and buttons disabled until a row is selected) with the analog input selection
(3222), the collection start month (3223) and the energy conversion (3224, OK
disabled while nothing changed). Clicking 3220's data clear button (part 9) fails
the test, whatever else happens.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from daikin_itm.protocol import PART_UPDATE_CODE, SCREEN_UPDATE_CODE, Writer

from .fake_energy import _columns, _rows
from .fake_energy_edit import CONFIRM_ID, FakeEnergyEditITM
from .fake_itm import _encode_data, _encode_part

if TYPE_CHECKING:
    from collections.abc import Callable

    from .fake_itm import Update

HOURMIN_ID, TEMP_ID = 47, 48
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
WEEKDAY_RADIOS = (2, 3, 4, 5, 6, 7, 8)
SPECIAL_RADIOS = (33, 34, 35, 36, 37)
# Screens this fake adds, with their Close or Cancel part.
MORE_CLOSE = {3216: 15, 3217: 22, 3220: 7, 3221: 10, 3222: 4, 3223: 4}
MORE_CLOSE |= {3224: 15}
OWN = "Suction Temp."
ANALOG = ["AI 1", "AI 2"]
CONVERSION_PARTS = {3: 2, 7: 6, 10: 9, 13: 12}  # button -> text


def _slot(time: str) -> int:
    hour, minute = time.split(":")
    return int(hour) * 4 + int(minute) // 15


def _temp(value: list[int]) -> float:
    return value[0] + value[1] / 1000


class FakeEnergyMoreITM(FakeEnergyEditITM):  # pylint: disable=too-many-instance-attributes
    """A FakeEnergyEditITM with the rule settings and the installer's screens."""

    def __init__(self, *, data: bool = True) -> None:
        """Start with two rules (Rule A with a weekday range) and generic points."""
        super().__init__(data=data)
        # Per rule: per day key (weekday 0-6, special "s0"-"s4") its time ranges.
        self.rule_days: dict[str, dict[str, list[dict[str, Any]]]] = {
            name[0]: {} for name in self.rules
        }
        if data:
            self.rule_days["Rule A"]["1"] = [
                {"start": "08:00", "end": "18:00", "cool": 26.0, "heat": 20.0}
            ]
        self.calendars: dict[str, list[list[str]]] = {
            name[0]: [] for name in self.rules
        }
        if data:
            self.calendars["Rule A"] = [["01/01", "Holiday 1"]]
        self.rule_pending: dict[str, list[dict[str, Any]]] = {}
        self.day: str | None = None
        self.rs_rows: list[dict[str, Any]] = []
        self.rs_fields: dict[str, Any] = {}
        self.rs_selected: int | None = None
        self.copy_picks: list[int] = []
        # The installer's settings.
        self.room_temp: dict[str, str | None] = {"AC1": None, "AC2": "AI 1"}
        self.rt_pending: dict[str, str | None] = {}
        self.rt_row: int | None = None
        self.rt_field: dict[str, Any] = {}
        self.analog_pick: int | None = None
        self.collect_month = 0
        self.collect_pending = 0
        self.conversion = {2: "", 6: "kWh", 9: "MWh", 12: "GWh"}
        self.conversion_pending: dict[int, str] = {}
        self.data_cleared = False

    # -- encoding: HOURMIN and NUMBER dialogs ---------------------------------------
    @staticmethod
    def _reply(token: str | None, updates: list[Update]) -> str:
        w = Writer(token or "0" * 32)
        w.int(len(updates))
        for kind, screen, data, parts in updates:
            w.short(SCREEN_UPDATE_CODE[kind])
            w.short(screen)
            if kind == "DLG_OPEN" and data[1] == 10:  # HOURMIN: N S + 6 H + 13 S
                w.short(data[0])
                w.short(data[1])
                w.int(data[2])
                w.string(data[3])
                for n in data[4:10]:
                    w.short(n)
                for text in [*data[10:], *[""] * 13][:13]:
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

    # -- routing --------------------------------------------------------------------
    def _menu(self, ev: dict[str, Any]) -> list[Update]:
        screen, part = ev["screen"], ev["part"]
        if screen == 5 and self.menu_open and part == 106:
            if not self.service_mode:
                info = [92, 3, "Function used by another user.", "Info", "OK"]
                return [("DLG_OPEN", 5, info, [])]
            return [] if 3220 in self.unavailable else self._open(3220)
        if screen in MORE_CLOSE and screen in self.open_screens:
            return self._energy_event(ev)
        return super()._menu(ev)

    def _energy_event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part = ev["screen"], ev["part"]
        if (screen, part) == (3220, 9):
            self.data_cleared = True
            self._violation(ev, "pressed the energy navigator's data clear button")
        if (screen, part) in self.disabled | self.locked:
            self._violation(ev, "clicked a part shown ENABLED 0 (hangs the iTM)")
        if self.edit_dialog is not None and part == self.edit_dialog[0]:
            return self._answer(screen, ev)
        handler = {
            3213: self._rule_list_event,
            3214: self._editor_event,
            3216: self._copy_event,
            3217: self._ranges_event,
            3220: self._installer_event,
            3221: self._room_event,
            3222: self._analog_event,
            3223: self._collect_event,
            3224: self._conversion_event,
        }.get(screen)
        updates = handler(ev) if handler is not None else None
        if updates is not None:
            return updates
        if screen in MORE_CLOSE:
            if part == MORE_CLOSE[screen] and ev["event"] == 1:
                return self._close(screen)
            self._violation(ev, "not offered by this screen")
        return super()._energy_event(ev)

    def _answer(self, screen: int, ev: dict[str, Any]) -> list[Update]:
        assert self.edit_dialog is not None
        dialog_id, kind, *what = self.edit_dialog
        if kind not in {"hourmin", "temp"}:
            return super()._answer(screen, ev)
        self.edit_dialog = None
        close: list[Update] = [
            ("DLG_CLOSE", screen, [dialog_id, 10 if kind == "hourmin" else 6], [])
        ]
        if ev["event"] != 1:
            return close
        if kind == "hourmin":
            hour, minute = ev["array"]
            self.rs_fields[what[0]] = f"{hour:02d}:{minute:02d}"
        else:
            self.rs_fields[what[0]] = _temp(ev["array"])
        return [*close, ("UPDATE", 3217, [], self._field_parts())]

    def _open(self, target: int) -> list[Update]:
        if target == 3214:
            rule = self.rules[self.row_selected[3213]][0]
            self.rule_pending = copy.deepcopy(self.rule_days.get(rule, {}))
            self.day = None
            self.locked |= {(3214, 23), (3214, 50)}
        if target == 3217:
            self.rs_rows = copy.deepcopy(self.rule_pending.get(self.day or "", []))
            self.rs_selected = None
            self.rs_fields = {"start": "00:00", "end": "24:00", "setpoint": False}
            self.rs_fields |= {"cool_on": False, "heat_on": False}
            self.rs_fields |= {"cool": 26.0, "heat": 20.0}
            self.locked |= {(3217, 3), (3217, 20)}
        if target == 3216:
            self.copy_picks = []
            self.locked.add((3216, 14))
        if target == 3221:
            self.rt_pending = dict(self.room_temp)
            self.rt_row = None
            self.locked |= {(3221, p) for p in (4, 5, 6, 7, 8)}
        if target == 3222:
            self.analog_pick = None
        if target == 3223:
            self.collect_pending = self.collect_month
        if target == 3224:
            self.conversion_pending = {}
            self.locked.add((3224, 14))
        return super()._open(target)

    def _close(self, screen: int) -> list[Update]:
        self.locked -= {(sc, p) for sc, p in set(self.locked) if sc == screen}
        return super()._close(screen)

    def _parts(self, target: int) -> list[tuple[int, str, Any]]:
        parts: Callable[[], list[tuple[int, str, Any]]] | None = {
            3214: self._rule_parts,
            3216: self._copy_parts,
            3217: self._ranges_parts,
            3220: self._installer_parts,
            3221: self._room_parts,
            3222: self._analog_parts,
            3223: self._collect_parts,
            3224: self._conversion_parts,
        }.get(target)
        return parts() if parts is not None else super()._parts(target)

    # -- rule list (3213) -----------------------------------------------------------
    def _entries(self, screen: int) -> list[list[str]]:
        return self.rules if screen == 3213 else super()._entries(screen)

    def _rule_list_event(self, ev: dict[str, Any]) -> list[Update] | None:
        part = ev["part"]
        if ev["event"] != 1 or part in {2, 8, 10}:
            return None
        if part in {4, 6, 7}:
            return self._list_event(ev)
        if part == 5:
            return self._name_dialog(3213, "copy", self.row_selected[3213])
        if part == 9:
            self.row_selected.setdefault(3213, -1)
            if self.row_selected[3213] < 0:
                self._violation(ev, "a copy without a selected rule")
            return self._open(3216)
        return None

    def _named(self, screen: int, what: list[Any], name: str) -> list[Update]:
        if screen == 3224:
            return self._named_conversion(what[1], name)
        if screen == 3213 and what[0] == "copy":
            source = self.rules[what[1]][0]
            self.rule_days[name] = copy.deepcopy(self.rule_days.get(source, {}))
            self.calendars[name] = list(self.calendars.get(source, []))
            self.rules.append([name])
            self.saved.append("copy 3213")
            return self._list_update(screen)
        if screen == 3213:
            if what[0] == "rename":
                old = self.rules[what[1]][0]
                self.rule_days[name] = self.rule_days.pop(old, {})
                self.calendars[name] = self.calendars.pop(old, [])
            else:
                self.rule_days[name] = {}
                self.calendars[name] = []
        return super()._named(screen, what, name)

    # -- rule editor (3214) ---------------------------------------------------------
    def _rule_parts(self) -> list[tuple[int, str, Any]]:
        rule = self.rules[self.row_selected[3213]][0]

        def pattern(key: str) -> list[list[int]]:
            entries = []
            for r in self.rule_pending.get(key, []):
                entries += [[_slot(r["start"]), 0, -1, 2], [_slot(r["end"]), 1, -1, 2]]
            return entries

        return [
            (1, "TAB_NAME", ["Weekly", "Special days", "Calendar"]),
            *[(14 + d, "TIMELINE_BAR", pattern(str(d))) for d in range(7)],
            *[(33 + i, "LABEL", f"Holiday {i + 1}") for i in range(5)],
            (37, "VISIBLED", 0),
            *[(43 + i, "TIMELINE_BAR", pattern(f"s{i}")) for i in range(5)],
            (62, "LIST_COLUMN", _columns("Date", "Pattern")),
            (62, "LIST_ROW", _rows(self.calendars.get(rule, []))),
        ]

    def _editor_event(self, ev: dict[str, Any]) -> list[Update] | None:
        part, event = ev["part"], ev["event"]
        if event != 1:
            return None
        radios = {**{p: str(i) for i, p in enumerate(WEEKDAY_RADIOS)}}
        radios |= {p: f"s{i}" for i, p in enumerate(SPECIAL_RADIOS)}
        if part in radios:
            self.day = radios[part]
            edit = 50 if part in SPECIAL_RADIOS else 23
            self.locked.discard((3214, edit))
            checks = [(p, "CHECK_STATE", int(p == part)) for p in radios]
            return [("UPDATE", 3214, [], [*checks, (edit, "ENABLED", 1)])]
        if part in {23, 50}:
            return self._open(3217)
        if part == 29:
            return self._confirm_dialog(3214, self._save_rule)
        return None

    def _save_rule(self) -> list[Update]:
        rule = self.rules[self.row_selected[3213]][0]
        self.rule_days[rule] = copy.deepcopy(self.rule_pending)
        self.saved.append(f"rule {rule}")
        return self._close(3214)

    # -- calendar copy (3216) -------------------------------------------------------
    def _copy_targets(self) -> list[list[str]]:
        source = self.rules[self.row_selected[3213]][0]
        return [r for r in self.rules if r[0] != source]

    def _copy_parts(self) -> list[tuple[int, str, Any]]:
        source = self.rules[self.row_selected[3213]][0]
        return [
            (2, "TEXT", source),
            (4, "LIST_COLUMN", _columns("Name")),
            (4, "LIST_ROW", _rows(self._copy_targets())),
            *[(9 + i, "LABEL", f"Holiday {i + 1}") for i in range(5)],
        ]

    def _copy_event(self, ev: dict[str, Any]) -> list[Update] | None:
        screen, part = ev["screen"], ev["part"]
        ok = 14
        if part == 4 and ev["event"] == 1 and ev["array"]:
            self.copy_picks = list(ev["array"])
            self.locked.discard((screen, ok))
            return [("UPDATE", screen, [], [(ok, "ENABLED", 1)])]
        if part == ok and ev["event"] == 1:

            def copy_now() -> list[Update]:
                source = self.rules[self.row_selected[3213]][0]
                targets = self._copy_targets()
                for row in self.copy_picks:
                    name = targets[row][0]
                    self.calendars[name] = copy.deepcopy(self.calendars[source])
                self.saved.append(f"copy {screen}")
                return self._close(screen)

            return self._confirm_dialog(screen, copy_now)
        return None

    # -- rule settings (3217) -------------------------------------------------------
    @staticmethod
    def _row_text(r: dict[str, Any]) -> list[str]:
        cool = "--" if r.get("cool") is None else f"{r['cool']:.1f}"
        heat = "--" if r.get("heat") is None else f"{r['heat']:.1f}"
        return [f"{r['start']}-{r['end']}", cool, heat]

    def _field_parts(self) -> list[tuple[int, str, Any]]:
        f = self.rs_fields
        usable = {
            13: f["setpoint"],
            14: f["setpoint"],
            15: f["setpoint"] and f["cool_on"],
            16: f["setpoint"] and f["cool_on"],
            17: f["setpoint"] and f["heat_on"],
            18: f["setpoint"] and f["heat_on"],
        }
        for part, on in usable.items():
            (self.locked.discard if on else self.locked.add)((3217, part))
        return [
            *[(part, "ENABLED", int(on)) for part, on in usable.items()],
            (7, "TEXT", f["start"]),
            (10, "TEXT", f["end"]),
            (12, "CHECK_STATE", int(f["setpoint"])),
            (13, "CHECK_STATE", int(f["cool_on"])),
            (14, "CHECK_STATE", int(f["heat_on"])),
            (15, "TEXT", f"{f['cool']:.1f}"),
            (17, "TEXT", f"{f['heat']:.1f}"),
        ]

    def _ranges_parts(self) -> list[tuple[int, str, Any]]:
        return [
            (2, "LIST_COLUMN", _columns("Time", "Cool", "Heat")),
            (2, "LIST_ROW", _rows([self._row_text(r) for r in self.rs_rows])),
            *self._field_parts(),
            (30, "LABEL", "°C"),
            (31, "LABEL", "°C"),
        ]

    def _range_from_fields(self) -> dict[str, Any]:
        f = self.rs_fields
        on = f["setpoint"]
        return {
            "start": f["start"],
            "end": f["end"],
            "cool": f["cool"] if on and f["cool_on"] else None,
            "heat": f["heat"] if on and f["heat_on"] else None,
        }

    def _time_limits(self, part: int) -> tuple[tuple[int, int], tuple[int, int]]:
        """Return the range the real iTM gives the start (8) or end (11) input."""
        hour, minute = map(
            int, self.rs_fields["start" if part == 11 else "end"].split(":")
        )
        value = hour * 60 + minute
        if part == 11:
            return divmod(value + 15, 60), (24, 0)
        return (0, 0), divmod((value or 24 * 60) - 15, 60)

    def _ranges_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update] | None:
        part = ev["part"]
        if ev["event"] != 1:
            return None
        if part == 2 and ev["array"]:
            self.rs_selected = row = ev["array"][0]
            r = self.rs_rows[row]
            self.rs_fields |= {"start": r["start"], "end": r["end"]}
            self.rs_fields |= {
                "setpoint": r["cool"] is not None or r["heat"] is not None,
                "cool_on": r["cool"] is not None,
                "heat_on": r["heat"] is not None,
                "cool": r["cool"] if r["cool"] is not None else 26.0,
                "heat": r["heat"] if r["heat"] is not None else 20.0,
            }
            self.locked -= {(3217, 3), (3217, 20)}
            enable = [(3, "ENABLED", 1), (20, "ENABLED", 1)]
            return [("UPDATE", 3217, [], [*self._field_parts(), *enable])]
        if part in {8, 11}:
            self.edit_dialog = (HOURMIN_ID, "hourmin", "start" if part == 8 else "end")
            first, last = self._time_limits(part)
            data = [HOURMIN_ID, 10, 4, "Time", 0, 0, *first, *last, "OK", "Cancel"]
            return [("DLG_OPEN", 3217, data, [])]
        if part in {12, 13, 14}:
            if ev["dtype"] != 1:
                return [("UPDATE", 3217, [], [])]
            key = {12: "setpoint", 13: "cool_on", 14: "heat_on"}[part]
            self.rs_fields[key] = ev["number"] == 1
            enabled = [p for p in self._field_parts() if p[1] == "ENABLED"]
            return [("UPDATE", 3217, [], enabled)]
        if part in {16, 18}:
            key, low, high = ("cool", 16, 32) if part == 16 else ("heat", 10, 30)
            self.edit_dialog = (TEMP_ID, "temp", key)
            data = [TEMP_ID, 6, 2, "Value", 0, 0, low, 0, high, 0, 2, 1, *["x"] * 11]
            return [("DLG_OPEN", 3217, data, [])]
        if part in {19, 20, 3}:
            if part == 19:
                self.rs_rows.append(self._range_from_fields())
            elif part == 20:
                assert self.rs_selected is not None
                self.rs_rows[self.rs_selected] = self._range_from_fields()
            else:
                assert self.rs_selected is not None
                del self.rs_rows[self.rs_selected]
                self.rs_selected = None
                self.locked |= {(3217, 3), (3217, 20)}
            return [("UPDATE", 3217, [], self._ranges_parts())]
        if part == 21:
            self.rule_pending[self.day or ""] = self.rs_rows
            updates = self._close(3217)
            return [*updates, ("UPDATE", 3214, [], self._rule_parts())]
        return None

    # -- the installer's settings (3220-3224) ---------------------------------------
    @staticmethod
    def _installer_parts() -> list[tuple[int, str, Any]]:
        return [
            (1, "LABEL", "Room temperature settings"),
            (2, "LABEL", "Data collection start month"),
            (3, "LABEL", "Energy conversion"),
            (8, "LABEL", "Clear data"),
        ]

    def _installer_event(self, ev: dict[str, Any]) -> list[Update] | None:
        targets = {4: 3221, 5: 3223, 6: 3224}
        if ev["event"] == 1 and ev["part"] in targets:
            return self._open(targets[ev["part"]])
        return None

    def _room_rows(self) -> list[list[str]]:
        return [[name, src or OWN] for name, src in self.rt_pending.items()]

    def _room_parts(self) -> list[tuple[int, str, Any]]:
        parts: list[tuple[int, str, Any]] = [
            (2, "LIST_COLUMN", _columns("Mgmt. Point Name", "Attributes")),
            (2, "LIST_ROW", _rows(self._room_rows())),
            (4, "LABEL", OWN),
            (5, "LABEL", "Analog input"),
            (4, "CHECK_STATE", 1),
            (5, "CHECK_STATE", 0),
            (6, "TEXT", ""),
        ]
        if self.rt_row is not None:
            analog = self.rt_field.get("analog")
            parts += [
                (4, "CHECK_STATE", int(analog is None)),
                (5, "CHECK_STATE", int(analog is not None)),
                (6, "TEXT", analog or ""),
                *[(p, "ENABLED", 1) for p in (4, 5, 6, 8)],
                (7, "ENABLED", int(analog is not None)),
            ]
        return parts

    def _sync_room_locks(self) -> None:
        self.locked -= {(3221, p) for p in (4, 5, 6, 7, 8)}
        if self.rt_field.get("analog") is None:
            self.locked.add((3221, 7))

    def _room_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update] | None:
        part = ev["part"]
        if ev["event"] != 1:
            return None
        names = list(self.rt_pending)
        if part == 2 and ev["array"]:
            self.rt_row = ev["array"][0]
            self.rt_field = {"analog": self.rt_pending[names[self.rt_row]]}
            self._sync_room_locks()
            return [("UPDATE", 3221, [], self._room_parts())]
        if part in {4, 5}:
            analog = None if part == 4 else self.rt_field.get("analog") or ""
            self.rt_field["analog"] = analog
            self._sync_room_locks()
            return [("UPDATE", 3221, [], self._room_parts())]
        if part == 7:
            return self._open(3222)
        if part == 8:
            assert self.rt_row is not None
            self.rt_pending[names[self.rt_row]] = self.rt_field["analog"] or None
            return [("UPDATE", 3221, [], self._room_parts())]
        if part == 9:

            def save() -> list[Update]:
                self.room_temp = dict(self.rt_pending)
                self.saved.append("room temperature")
                return self._close(3221)

            return self._confirm_dialog(3221, save)
        return None

    @staticmethod
    def _analog_parts() -> list[tuple[int, str, Any]]:
        return [
            (2, "LIST_COLUMN", _columns("Name", "Address")),
            (2, "LIST_ROW", _rows([[n, f"3-0{i}"] for i, n in enumerate(ANALOG)])),
        ]

    def _analog_event(self, ev: dict[str, Any]) -> list[Update] | None:
        if ev["event"] != 1:
            return None
        if ev["part"] == 2 and ev["array"]:
            self.analog_pick = ev["array"][0]
            return [("UPDATE", 3222, [], [])]
        if ev["part"] == 3 and self.analog_pick is not None:
            self.rt_field["analog"] = ANALOG[self.analog_pick]
            self._sync_room_locks()
            return [*self._close(3222), ("UPDATE", 3221, [], self._room_parts())]
        return None

    def _collect_parts(self) -> list[tuple[int, str, Any]]:
        return [(2, "COMBO_STRS", MONTHS), (2, "SINGLE_SELIDX", self.collect_month)]

    def _collect_event(self, ev: dict[str, Any]) -> list[Update] | None:
        if ev["event"] != 1:
            return None
        if ev["part"] == 2:
            self.collect_pending = ev["number"]
            return [("UPDATE", 3223, [], [])]
        if ev["part"] == 3:

            def save() -> list[Update]:
                self.collect_month = self.collect_pending
                self.saved.append("collection start")
                return self._close(3223)

            return self._confirm_dialog(3223, save)
        return None

    def _conversion_parts(self) -> list[tuple[int, str, Any]]:
        return [
            *[(text, "TEXT", value) for text, value in self.conversion.items()],
            (5, "LABEL", "(1)"),
            (8, "LABEL", "(1) x 1000"),
            (11, "LABEL", "(1) x 1000000"),
        ]

    def _conversion_event(self, ev: dict[str, Any]) -> list[Update] | None:
        if ev["event"] != 1:
            return None
        part = ev["part"]
        if part in CONVERSION_PARTS:
            return self._name_dialog(3224, "conversion", CONVERSION_PARTS[part])
        if part == 14:

            def save() -> list[Update]:
                self.conversion |= self.conversion_pending
                self.saved.append("conversion")
                return self._close(3224)

            return self._confirm_dialog(3224, save)
        return None

    def _named_conversion(self, text: int, value: str) -> list[Update]:
        self.conversion_pending[text] = value
        self.locked.discard((3224, 14))
        return [("UPDATE", 3224, [], [(text, "TEXT", value), (14, "ENABLED", 1)])]


__all__ = ["CONFIRM_ID", "FakeEnergyMoreITM"]
