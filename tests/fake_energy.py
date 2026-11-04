"""A simulated iTM with the energy navigator's screens.

The budget overview (menu button 100 -> 3200) with three tabs and a BARLINE_GRAPH
each, its planning settings (3201) and below them the planned (3202) and actual
(3203) consumption, which open behind a PROGRESS dialog and bring their month lists
with the POLLING after it; the equipment management (102 -> 3210) with its rule list
(3213) and sampling targets (3218). With ``data=False`` the lists and graphs are
empty and the totals show "--", as on an iTM without the licence.

It only allows what a read may send: the buttons that open these screens, their
Close or Cancel, tab switches and progress requests. Anything else (an OK, a save, a
create or delete button, a combo pick) or a click on a part shown ENABLED 0 is
recorded in ``violations`` and fails the request, the way a test should fail.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from . import fake_itm
from .fake_itm import FakeITM, Update

if TYPE_CHECKING:
    from daikin_itm.protocol import Writer

PROGRESS_ID, DISCARD_ID = 30, 31
ENERGY_BUTTONS = {100: 3200, 102: 3210}
CLOSE = {3200: 19, 3201: 5, 3202: 15, 3203: 16, 3210: 20, 3213: 10, 3218: 4}
CLOSE |= {3204: 5, 3205: 9, 3206: 14, 3207: 9, 3208: 12, 3211: 9, 3214: 30}
OPENS = {(3200, 18): 3201, (3201, 2): 3202, (3201, 4): 3203, (3210, 9): 3218}
OPENS |= {(3210, 10): 3213, (3200, 3): 3204, (3204, 2): 3205, (3204, 4): 3207}
OPENS |= {(3205, 8): 3206, (3207, 8): 3208, (3210, 16): 3211, (3213, 8): 3214}
TABS = {3200: 4, 3202: 3, 3203: 3, 3210: 11, 3211: 1, 3214: 1}
LOADING = {3202, 3203}  # behind a PROGRESS dialog; their lists come with POLLING
LISTS = {3205: 2, 3207: 2, 3213: 2}  # lists whose row selection enables buttons
ROW_BUTTONS = (5, 6, 7, 8)  # copy, delete, rename, edit: disabled until a row is
TYPES = ["Power", "Gas", "Water"]
YEARS = ["01/2025-12/2025", "01/2024-12/2024"]


def _columns(*names: str) -> list[list[Any]]:
    return [[name, 80, 0, 0, 0] for name in names]


def _rows(cells: list[list[str]], selected: list[int] | None = None) -> dict[str, Any]:
    n = len(cells[0]) if cells else 0
    rows = [{"text": c, "a": [-1] * n, "b": [-1] * n} for c in cells]
    return {"rows": rows, "selected": selected or []}


# -- graph encoders (registered with the base fake's) -----------------------------


def _axis_labels(w: Writer, labels: list[tuple[str, int]]) -> None:
    w.int(len(labels))
    for text, colour in labels:
        w.string(text)
        w.int(colour)


def _encode_axes(w: Writer, v: dict[str, Any]) -> None:
    w.short(0)  # graph type
    w.short(0)  # point type
    w.int(1)  # left axis: its labels, no title, two numbers
    _axis_labels(w, v["vaxis"])
    w.int(0)
    w.int(0)
    w.int(0)
    w.int(0)  # no right axis
    w.int(1)  # bottom axis: its labels, no title, no extra texts
    _axis_labels(w, v["haxis"])
    w.int(0)
    w.int(0)


def _encode_barline_graph(w: Writer, v: dict[str, Any]) -> None:
    _encode_axes(w, v)
    w.int(len(v["series"]))
    for colour, values in v["series"]:
        w.int(colour)
        w.int(len(values))
        for value in values:
            if value is None:
                w.int(0)
            else:
                w.int(1)
                w.int(round(value * 10))
        w.string("")
        w.string("")
    w.int(0)


def _encode_point_graph(w: Writer, v: dict[str, Any]) -> None:
    _encode_axes(w, v)
    w.int(len(v["series"]))
    for colour, points in v["series"]:
        w.int(colour)
        w.int(len(points))
        for kind, x, y in points:
            w.int(kind)
            w.int(round(x * 100))
            w.int(round(y * 100))
    w.int(0)


# The base fake encodes parts through this table; add the graphs it lacks.
fake_itm._ENCODERS.setdefault("BARLINE_GRAPH", _encode_barline_graph)  # pylint: disable=protected-access
fake_itm._ENCODERS.setdefault("POINT_GRAPH", _encode_point_graph)  # pylint: disable=protected-access


class FakeEnergyITM(FakeITM):  # pylint: disable=too-many-instance-attributes
    """A FakeITM with the energy navigator's screens, guarding against writes."""

    def __init__(self, *, data: bool = True) -> None:
        """Start with or without energy data, every screen available."""
        super().__init__()
        self.data = data
        self.violations: list[str] = []
        self.unavailable: set[int] = set()  # menu screens that don't open
        self.disabled: set[tuple[int, int]] = set()  # parts shown as ENABLED 0
        self.no_screen: set[int] = set()  # screens whose button opens nothing
        self.tab_shown: dict[int, int] = {}
        self.row_selected: dict[int, int] = {}
        self.locked: set[tuple[int, int]] = set()  # buttons disabled until a row
        # The lists' entries; a built-in type ("Power") can't be edited.
        self.energy_types: list[list[str]] = [["Power", "kWh"], ["Gas", "m3"]]
        self.energy_groups: list[list[str]] = [["Group A"], ["Group B"]]
        self.rules = [["Rule A"], ["Rule B"]] if data else []
        # Per (energy type, fiscal year tab) the planned and actual consumption.
        self.plans: dict[tuple[int, int], dict[str, Any]] = {}
        self.actuals: dict[tuple[int, int], dict[str, Any]] = {}
        self.type_shown: dict[int, int] = {}  # 3202/3203 -> energy type shown
        self.sampling = {"from": "20/12/2024", "to": "02/01/2025"}
        self.sampling["target"] = "Building&gt;All"
        self.loading: dict[int, int] = {}  # screen -> progress requests left
        self.loaded: set[int] = set()  # screens whose lists the next POLLING brings
        self.job = 0

    # -- routing ----------------------------------------------------------------------
    def _menu(self, ev: dict[str, Any]) -> list[Update]:
        screen, part = ev["screen"], ev["part"]
        if screen == 5 and self.menu_open and part in ENERGY_BUTTONS:
            target = ENERGY_BUTTONS[part]
            if target in self.unavailable:
                return []
            return self._open(target)
        if screen in CLOSE and screen in self.open_screens:
            return self._energy_event(ev)
        return super()._menu(ev)

    def _violation(self, ev: dict[str, Any], what: str) -> None:
        msg = f"{what}: screen {ev['screen']} part {ev['part']} event {ev['event']}"
        self.violations.append(msg)
        raise AssertionError(msg)

    def _energy_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update]:
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        if (screen, part) in self.disabled | self.locked:
            self._violation(ev, "clicked a part shown ENABLED 0 (hangs the iTM)")
        if event == 5 and part == PROGRESS_ID:  # UPD_PROG
            return self._progress(screen)
        if event == 8 and part == TABS.get(screen):  # TAB_SELECT
            self.tab_shown[screen] = ev["number"]
            return [("UPDATE", screen, [], [])]
        if event != 1:
            self._violation(ev, "not a read")
        if part == LISTS.get(screen) and ev["array"]:
            return self._select_row(screen, ev["array"][0])
        if screen == 3214 and part == DISCARD_ID:  # Yes to the discard question
            return [("DLG_CLOSE", 3214, [DISCARD_ID, 2], []), *self._close(3214)]
        if part == CLOSE[screen]:
            if screen == 3214:
                confirm = [DISCARD_ID, 2, "Confirm exit?", "", "Yes", "No"]
                return [("DLG_OPEN", 3214, confirm, [])]
            return self._close(screen)
        if (screen, part) in OPENS:
            target = OPENS[screen, part]
            return [] if target in self.no_screen else self._open(target)
        self._violation(ev, "a write or a display choice")
        return []  # pragma: no cover - _violation raises

    def _close(self, screen: int) -> list[Update]:
        self.open_screens.discard(screen)
        self.loading.pop(screen, None)
        self.loaded.discard(screen)
        self.row_selected.pop(screen, None)
        self.locked -= {(screen, p) for p in ROW_BUTTONS}
        return [("CLOSE", screen, [], [])]

    def _select_row(self, screen: int, row: int) -> list[Update]:
        """Select a list row: its buttons get enabled (not a built-in type's)."""
        self.row_selected[screen] = row
        builtin = screen == 3205 and row == 0
        self.locked -= {(screen, p) for p in ROW_BUTTONS}
        if builtin:
            self.locked |= {(screen, p) for p in ROW_BUTTONS}
        parts = [(p, "ENABLED", int(not builtin)) for p in ROW_BUTTONS]
        return [("UPDATE", screen, [], parts)]

    def _open(self, target: int) -> list[Update]:
        self.open_screens.add(target)
        if target in LISTS:
            self.locked |= {(target, p) for p in ROW_BUTTONS}
        locked = {(sc, p) for sc, p in self.disabled | self.locked if sc == target}
        disabled = [(p, "ENABLED", 0) for _, p in sorted(locked)]
        opened: list[Update] = [
            ("OPEN", target, [-1], [*self._parts(target), *disabled])
        ]
        if target in LOADING:
            self.job += 1
            self.loading[target] = 2
            data = [PROGRESS_ID, 11, "Now Loading.", self.job, 1, "Progress", "Cancel"]
            opened.append(("DLG_OPEN", target, data, []))
        return opened

    def _progress(self, screen: int) -> list[Update]:
        left = self.loading.get(screen, 0) - 1
        if left > 0:
            self.loading[screen] = left
            return [("DLG_UPDATE", screen, [PROGRESS_ID, 11, 50], [])]
        self.loading.pop(screen, None)
        self.loaded.add(screen)
        return [("DLG_CLOSE", screen, [PROGRESS_ID, 11], [])]

    def _polling(self, ev: dict[str, Any]) -> list[Update]:
        lists: list[Update] = [
            ("UPDATE", screen, [], self._lists(screen))
            for screen in sorted(self.loaded)
            if screen in self.open_screens
        ]
        self.loaded.clear()
        return [*lists, *super()._polling(ev)]

    # -- screens ----------------------------------------------------------------------
    def _graph(self) -> dict[str, Any]:
        series = [(255, [100.5, None, 80.0])] if self.data else []
        return {
            "vaxis": [("0", 3355443), ("10", 3355443)],
            "haxis": [("Jan", 3355443), ("Feb", 3355443), ("Mar", 3355443)],
            "series": series,
        }

    def _parts(self, target: int) -> list[tuple[int, str, Any]]:
        parts = {
            3200: self._budget_parts,
            3202: self._planned_parts,
            3203: self._actual_parts,
            3210: self._equipment_parts,
            3206: self._type_parts,
            3208: self._energy_group_parts,
            3211: self._details_parts,
            3214: self._rule_parts,
        }.get(target)
        if parts is not None:
            return parts()
        if target == 3201:
            return [(1, "LABEL", "Planned"), (3, "LABEL", "Actual")]
        if target == 3204:
            return [(1, "LABEL", "Energy types"), (3, "LABEL", "Energy groups")]
        return self._lists(target)

    def _type_parts(self) -> list[tuple[int, str, Any]]:
        name, unit = self.energy_types[self.row_selected[3205]]
        return [
            (0, "LABEL", name),
            (2, "COMBO_STRS", ["kWh", "m3", "MWh"]),
            (2, "SINGLE_SELIDX", ["kWh", "m3", "MWh"].index(unit)),
            (4, "COMBO_STRS", ["Electricity", "Gas", "Water", "Other"]),
            (4, "SINGLE_SELIDX", 1),
            (6, "TEXT", "1.950"),
            (7, "LABEL", f"kg-CO2/{unit}"),
            (10, "TEXT", "--"),
            (11, "LABEL", f"kWh/{unit}"),
        ]

    def _energy_group_parts(self) -> list[tuple[int, str, Any]]:
        mine = self.row_selected[3207] == 0
        return [
            (2, "LIST_COLUMN", _columns("Address", "Name")),
            (2, "LIST_ROW", _rows([["1-00", "Unit 1"]] if mine else [])),
            (8, "LIST_COLUMN", _columns("Address", "Name")),
            (8, "LIST_ROW", _rows([["1-01", "Unit 2"]])),
            (10, "COMBO_STRS", TYPES),
        ]

    def _details_parts(self) -> list[tuple[int, str, Any]]:
        return [
            (1, "TAB_NAME", ["Operation", "Occurrences"]),
            (4, "LABEL", "03/01/2025"),
            (7, "BARLINE_GRAPH", self._graph()),
            (11, "LIST_COLUMN", _columns("Name", "Days")),
            (11, "LIST_ROW", _rows([["Unit 1", "3"]])),
        ]

    def _rule_parts(self) -> list[tuple[int, str, Any]]:
        weekday = [[32, 0, -1, 2], [72, 1, -1, 2]]  # 08:00 on, 18:00 off
        parts: list[tuple[int, str, Any]] = [
            (1, "TAB_NAME", ["Weekly", "Special days", "Calendar"]),
            *[(14 + d, "TIMELINE_BAR", weekday if 0 < d < 6 else []) for d in range(7)],
            *[(33 + i, "LABEL", f"Holiday {i + 1}") for i in range(5)],
            (37, "VISIBLED", 0),
            *[(43 + i, "TIMELINE_BAR", []) for i in range(5)],
            (62, "LIST_COLUMN", _columns("Date", "Pattern")),
            (62, "LIST_ROW", _rows([["01/01", "Holiday 1"]])),
        ]
        return parts

    def _budget_parts(self) -> list[tuple[int, str, Any]]:
        total = "2026 : 1234.5(kWh)" if self.data else "-2026 : --()"
        parts: list[tuple[int, str, Any]] = [
            (0, "LABEL", "Energy navigator"),
            # Like a real iTM: the planning settings buttons are hidden, yet work.
            *[(part, "VISIBLED", 0) for part in (13, 18, 33, 43)],
            # Like a real iTM: the display combo comes without its selection.
            (2, "COMBO_STRS", ["Building", "Indoor units"]),
            (4, "TAB_NAME", ["Yearly budget", "Monthly budget", "Compare"]),
            (5, "LABEL", "2026"),
            (9, "LABEL", ""),
            (10, "LABEL", "Forecast : 900.0(kWh)" if self.data else "Forecast : --"),
            (20, "LABEL", "01/2025"),
            (34, "LABEL", "Actual consumption"),
            (35, "LABEL", total),
            (36, "LABEL", "-2025 : --()"),
        ]
        for graph, labels, kind, unit in (
            (12, (14, 16), 15, 17),
            (27, (29, 31), 30, 32),
            (38, (39, 41), 40, 42),
        ):
            parts += [
                (graph, "BARLINE_GRAPH", self._graph()),
                (labels[0], "LABEL", "Energy type"),
                (labels[1], "LABEL", "Energy unit"),
                (kind, "COMBO_STRS", TYPES),
                (kind, "SINGLE_SELIDX", 0),
                # The unit combo picks what is shown (energy, CO2), not "kWh".
                (unit, "COMBO_STRS", ["Energy", "CO2"]),
                (unit, "SINGLE_SELIDX", 0),
            ]
        return parts

    def plan(self, kind: int, tab: int) -> dict[str, Any]:
        """Return the planned consumption of an energy type's fiscal year."""
        first = kind == 0 and tab == 0
        months = (
            [["01/2026", "1000.0"], ["02/2026", "--"]] if first and self.data else []
        )
        return self.plans.setdefault(
            (kind, tab),
            {
                "per_month": first and self.data,
                "ratio": "5" if first and self.data else "0",
                "expected": "12000.0 " if tab == 0 else "-- ",
                "months": months,
            },
        )

    def actual(self, kind: int, tab: int) -> dict[str, Any]:
        """Return the actual consumption of an energy type's fiscal year."""
        first = kind == 0 and tab == 0
        months = (
            [["01/2026", "1000.0"], ["02/2026", "--"]] if first and self.data else []
        )
        return self.actuals.setdefault(
            (kind, tab), {"co2": "0.410" if tab == 0 else "--", "months": months}
        )

    def _planned_parts(self) -> list[tuple[int, str, Any]]:
        kind = self.type_shown.get(3202, 0)
        parts: list[tuple[int, str, Any]] = [
            (2, "COMBO_STRS", TYPES),
            (2, "SINGLE_SELIDX", kind),
            (3, "TAB_NAME", YEARS),
        ]
        for tab, (year, month, ratio, unit, expected, months) in enumerate(
            ((4, 11, 6, 7, 10, 13), (18, 25, 20, 21, 24, 27))
        ):
            plan = self.plan(kind, tab)
            parts += [
                (year, "CHECK_STATE", int(not plan["per_month"])),
                (month, "CHECK_STATE", int(plan["per_month"])),
                (ratio, "TEXT", plan["ratio"]),
                (unit, "LABEL", "%"),
                (expected, "TEXT", plan["expected"]),
                (months, "LIST_COLUMN", _columns("Month", "Target")),
            ]
        return parts

    def _actual_parts(self) -> list[tuple[int, str, Any]]:
        kind = self.type_shown.get(3203, 0)
        parts: list[tuple[int, str, Any]] = [
            (2, "COMBO_STRS", TYPES),
            (2, "SINGLE_SELIDX", kind),
            (3, "TAB_NAME", YEARS),
        ]
        for tab, (months, label, factor, unit) in enumerate(
            ((5, 7, 8, 9), (21, 23, 24, 25))
        ):
            parts += [
                (months, "LIST_COLUMN", _columns("Month", "Actual")),
                (label, "LABEL", "CO2 factor"),
                (factor, "TEXT", self.actual(kind, tab)["co2"]),
                (unit, "LABEL", "kg-CO2/kWh"),
            ]
        return parts

    def _equipment_parts(self) -> list[tuple[int, str, Any]]:
        rows = [["Rule A", "08:00", "5", "12.5"]] if self.data else []
        return [
            (2, "TEXT", self.sampling["from"]),
            (5, "TEXT", self.sampling["to"]),
            (8, "TEXT", self.sampling["target"]),
            (11, "TAB_NAME", ["On/Off", "Setpoint"]),
            (13, "COMBO_STRS", ["Rule A"] if self.data else []),
            (13, "SINGLE_SELIDX", 0),
            (16, "ENABLED", int(self.data)),
            (18, "POINT_GRAPH", {**self._graph(), "series": []}),
            (19, "LIST_COLUMN", _columns("Name", "Time", "Days", "Consumption")),
            (19, "LIST_ROW", _rows(rows)),
        ]

    def _lists(self, screen: int) -> list[tuple[int, str, Any]]:
        planned = [self.plan(self.type_shown.get(3202, 0), tab) for tab in (0, 1)]
        actual = [self.actual(self.type_shown.get(3203, 0), tab) for tab in (0, 1)]
        lists: dict[int, list[tuple[int, str, Any]]] = {
            3202: [
                (13, "LIST_ROW", _rows(planned[0]["months"])),
                (27, "LIST_ROW", _rows(planned[1]["months"])),
            ],
            3203: [
                (5, "LIST_ROW", _rows(actual[0]["months"])),
                (21, "LIST_ROW", _rows(actual[1]["months"])),
            ],
            3213: [
                (2, "LIST_COLUMN", _columns("Name")),
                (2, "LIST_ROW", _rows(self.rules)),
            ],
            3205: [
                (2, "LIST_COLUMN", _columns("Name", "Unit")),
                (2, "LIST_ROW", _rows(self.energy_types)),
            ],
            3207: [
                (2, "LIST_COLUMN", _columns("Name")),
                (2, "LIST_ROW", _rows(self.energy_groups)),
            ],
            3218: [
                (2, "LIST_COLUMN", _columns("Level", "Name")),
                (2, "LIST_ROW", _rows([["1", "All"], ["2", "Indoor units"]], [0])),
            ],
        }
        return lists[screen]
