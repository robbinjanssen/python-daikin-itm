"""A simulated iTM with the dealer-option function screens.

Temperature limit (400-402), sliding temperature (500-503), heating mode
optimisation (600) and interlock (900-906). The main screens open like a real
iTM's with the dealer options off: hidden menu buttons that still open their screen,
empty group and program lists with Add enabled, the radios and the other
edit buttons disabled until a row is selected, and every unit listed on 600. The
rest follows the library's documented assumptions. Strict like the real iTM:
clicking a part it reports disabled raises (the real web server hangs then).
Values are generic placeholders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from .fake_itm import FakeITM, Update, _rows

if TYPE_CHECKING:
    from collections.abc import Callable

Parts = list[tuple[int, str, Any]]

CONFIRM_ID, NAME_ID, NUMBER_ID = 60, 61, 62
ON, OFF = "Enable", "Disable"
CONDITIONS = ["On", "Off", "Error"]
INPUT_CONDITIONS = ["At on", "At error"]
MODES = ["Cool", "Heat", "Fan"]
MULTISTATES = ["State 1", "State 2"]
# The condition setup's (903) parts per tab, and the action setup's (906).
CONDITION_TABS = {28: 0, 29: 0, 32: 1, 35: 3} | dict.fromkeys(
    (3, 4, 6, 8, 9, 11, 15, 18), 2
)
ACTION_TABS = (
    dict.fromkeys((2, 3, 4, 54, 55), 0)
    | dict.fromkeys(range(7, 17), 1)
    | dict.fromkeys((17, 18, 19, 20, 23), 2)
)
ACTION_RADIOS = {3: 2, 4: 2, 54: 2, 55: 2, 8: 7, 9: 7, 10: 7, 12: 11, 13: 11}
ACTION_RADIOS |= {15: 14, 16: 14}  # radio -> its checkbox


def default_condition() -> dict[str, Any]:
    """Return a point's condition as a new program has it."""
    return {
        "state": 28,
        "mode": 0,
        "compare": 3,
        "ref": 6,
        "constant": 20.0,
        "point": "",
        "offset": 0.0,
        "hysteresis": 1.0,
        "multistate": 0,
    }


def default_action() -> dict[str, Any]:
    """Return a point's action as a new program has it: nothing ticked."""
    return {
        "ticked": dict.fromkeys((2, 7, 11, 14, 17, 19, 28), 0),
        "radios": {2: 3, 7: 8, 11: 12, 14: 15},
        "mode": 0,
        "setpoint": 22.0,
    }


@dataclass
class Group:
    """A temperature limit or sliding temperature group."""

    name: str
    enabled: bool = False
    members: list[str] = field(default_factory=list)
    lower: float = 16.0
    upper: float = 30.0
    point: str = ""
    outdoor: tuple[float, float] = (5.0, 25.0)
    setpoint: tuple[float, float] = (20.0, 24.0)


@dataclass
class Interlock:
    """An interlock program."""

    name: str
    enabled: bool = False
    inputs: list[str] = field(default_factory=list)
    outputs: list[list[str]] = field(default_factory=lambda: [[], []])
    conditions: list[int] = field(default_factory=lambda: [0, 0])
    intervals: list[int] = field(default_factory=lambda: [0, 0])
    condition_of: dict[str, dict[str, Any]] = field(default_factory=dict)
    action_of: dict[str, dict[str, Any]] = field(default_factory=dict)


N_SETUP, O_SETUP = 4, 6  # the condition (902) and action (905) setup buttons


def _t(value: float) -> str:
    return f"{value:.1f}"


def _copy(d: dict[str, Any]) -> dict[str, Any]:
    return {k: dict(v) if isinstance(v, dict) else v for k, v in d.items()}


def _confirm(screen: int) -> Update:
    return (
        "DLG_OPEN",
        screen,
        [CONFIRM_ID, 2, "Set?", "Confirm", "Yes", "No"],
        [],
    )


def _name_dialog(screen: int, default: str) -> Update:
    data = [NAME_ID, 4, "Name", default, 1, 20, 0, 0, 1]
    return (
        "DLG_OPEN",
        screen,
        [*data, "Words", "OK", "Cancel", *[""] * 11, ""],
        [],
    )


def _number_dialog(screen: int, kind: int, low: int, high: int) -> Update:
    data = [NUMBER_ID, 6, kind, "Value", 0, 0, low, 0, high, 0, 2, 1]
    return ("DLG_OPEN", screen, [*data, *["x"] * 11], [])


def _value(ev: dict[str, Any]) -> float:
    if ev["dtype"] == 1:
        return float(ev["number"])
    return float(ev["array"][0] + ev["array"][1] / 1000)


class FakeDealerITM(FakeITM):  # pylint: disable=too-many-instance-attributes
    """A FakeITM whose dealer-option screens edit groups, units and programs."""

    def __init__(self) -> None:
        """Start empty (as with the dealer options off); HMO off for every unit."""
        super().__init__()
        self.points = [u.name for u in self.units]
        self.analog_inputs = ["Outdoor sensor", "Return sensor"]
        self.limits: list[Group] = []
        self.slides: list[Group] = []
        self.hmo = dict.fromkeys(self.points, False)
        self.interlocks: list[Interlock] = []
        self.dl_selected: dict[int, int | None] = {}
        self.dl_disabled: set[tuple[int, int]] = set()
        self.dl_dialog: tuple[int, Callable[[dict[str, Any]], list[Update]]] | None
        self.dl_dialog = None
        self.dl_edit: dict[str, Any] = {}
        self.read_only_setup = False

    # -- test helpers ---------------------------------------------------------
    def limit(self, name: str) -> Group:
        """Return a temperature limit group by name."""
        return next(g for g in self.limits if g.name == name)

    def slide(self, name: str) -> Group:
        """Return a sliding temperature group by name."""
        return next(g for g in self.slides if g.name == name)

    def interlock(self, name: str) -> Interlock:
        """Return an interlock program by name."""
        return next(p for p in self.interlocks if p.name == name)

    # -- routing --------------------------------------------------------------
    def _optional_parts(self, target: int) -> list[tuple[int, str, Any]]:
        self.dl_selected[target] = None
        return {
            400: lambda: self._group_main(400),
            500: lambda: self._group_main(500),
            600: self._hmo_parts,
            900: self._il_main,
        }[target]()

    def _event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        if screen == 0 and part == 0 and event == 1:
            hidden = [(p, "VISIBLED", 0) for p in (5, 11, 13, 15)]
            return [*super()._event(ev), ("UPDATE", 5, [], hidden)]
        if screen not in self.open_screens or screen < 400 or screen > 906:
            return super()._event(ev)
        if self.dl_dialog is not None and part in {CONFIRM_ID, NAME_ID, NUMBER_ID}:
            dialog_screen, on_ok = self.dl_dialog
            self.dl_dialog = None
            close: Update = ("DLG_CLOSE", dialog_screen, [part, 2], [])
            return [close, *(on_ok(ev) if event == 1 else [])]
        if (screen, part) in self.dl_disabled and event == 1:
            msg = f"{screen}/{part} clicked while disabled: the real iTM hangs"
            raise RuntimeError(msg)
        handler = {
            400: self._group_event,
            500: self._group_event,
            401: self._members_event,
            501: self._members_event,
            402: self._settings_event,
            502: self._settings_event,
            503: self._inputs_event,
            600: self._hmo_event,
            900: self._il_event,
            901: self._setup_event,
            902: self._picker_event,
            905: self._picker_event,
            903: self._condition_event,
            904: self._point_picker_event,
            906: self._action_event,
        }.get(screen)
        if handler is None:
            return super()._event(ev)
        return handler(ev)

    def _ask(
        self, update: Update, on_ok: Callable[[dict[str, Any]], list[Update]]
    ) -> list[Update]:
        self.dl_dialog = (update[1], on_ok)
        return [update]

    def _close(self, screen: int) -> list[Update]:
        self.open_screens.discard(screen)
        self.dl_disabled = {k for k in self.dl_disabled if k[0] != screen}
        return [("CLOSE", screen, [], [])]

    def _enable(self, screen: int, parts: dict[int, bool]) -> Parts:
        for part, on in parts.items():
            (self.dl_disabled.discard if on else self.dl_disabled.add)((screen, part))
        return [(p, "ENABLED", 1 if on else 0) for p, on in parts.items()]

    # -- temperature limit and sliding temperature groups (400-402, 500-503) -----
    def _groups(self, screen: int) -> list[Group]:
        return self.limits if screen // 100 == 4 else self.slides

    def _group_rows(self, screen: int) -> dict[str, Any]:
        if screen == 400:
            return _rows(
                [
                    [ON if g.enabled else "", g.name, _t(g.lower), _t(g.upper)]
                    for g in self.limits
                ]
            )
        return _rows(
            [
                [
                    ON if g.enabled else "",
                    g.name,
                    g.point,
                    f"{_t(g.setpoint[0])}-{_t(g.setpoint[1])}",
                    f"{_t(g.outdoor[0])}-{_t(g.outdoor[1])}",
                ]
                for g in self.slides
            ]
        )

    def _group_main(self, screen: int) -> Parts:
        headers = ["Activation", "Name", "Lower Limit", "Upper Limit"]
        return [
            (3, "LIST_COLUMN", [[h, 100, 0, 0, 0] for h in headers]),
            (3, "LIST_ROW", self._group_rows(screen)),
            (5, "LABEL", ON),
            (6, "LABEL", OFF),
            (6, "CHECK_STATE", 1),
            *self._enable(screen, {5: False, 6: False, 8: True}),
            *self._enable(screen, {9: False, 10: False, 11: False}),
        ]

    def _group_selection(self, screen: int) -> Parts:
        row = self.dl_selected.get(screen)
        if row is None:
            return self._group_main(screen)
        g = self._groups(screen)[row]
        return [
            (3, "LIST_ROW", self._group_rows(screen)),
            (5, "CHECK_STATE", 1 if g.enabled else 0),
            (6, "CHECK_STATE", 0 if g.enabled else 1),
            *self._enable(screen, dict.fromkeys((5, 6, 9, 10, 11), True)),
        ]

    def _group_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update]:
        screen, part = ev["screen"], ev["part"]
        groups = self._groups(screen)
        if part == 3 and ev["dtype"] == 1:
            self.dl_selected[screen] = ev["number"]
            return [("UPDATE", screen, [], self._group_selection(screen))]
        if part == 1:
            return self._close(screen)
        if part == 8:

            def add(answer: dict[str, Any]) -> list[Update]:
                groups.append(Group(answer["string"]))
                return [("UPDATE", screen, [], self._group_selection(screen))]

            return self._ask(_name_dialog(screen, "Group 01"), add)
        row = self.dl_selected[screen]
        assert row is not None
        group = groups[row]
        if part in {5, 6}:

            def toggle(_answer: dict[str, Any]) -> list[Update]:
                group.enabled = part == 5
                return [("UPDATE", screen, [], self._group_selection(screen))]

            return self._ask(_confirm(screen), toggle)
        if part == 9:

            def delete(_answer: dict[str, Any]) -> list[Update]:
                groups.remove(group)
                self.dl_selected[screen] = None
                return [("UPDATE", screen, [], self._group_main(screen))]

            return self._ask(_confirm(screen), delete)
        if part == 10:
            self.dl_edit = {"group": group, "members": list(group.members)}
            self.open_screens.add(screen + 1)
            return [("OPEN", screen + 1, [-1], self._members_parts(screen + 1))]
        self.dl_edit = {"group": group, "name": group.name}
        self.open_screens.add(screen + 2)
        return [("OPEN", screen + 2, [-1], self._settings_parts(screen + 2))]

    def _free_points(self) -> list[str]:
        return [p for p in self.points if p not in self.dl_edit["members"]]

    def _members_parts(self, screen: int) -> Parts:
        return [
            (
                2,
                "LIST_ROW",
                _rows([[m, "Indoor"] for m in self.dl_edit["members"]]),
            ),
            (8, "LIST_ROW", _rows([[p, "Indoor"] for p in self._free_points()])),
            *self._enable(screen, {4: False, 5: False}),
        ]

    def _members_event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part, e = ev["screen"], ev["part"], self.dl_edit
        if part in {2, 8} and ev["dtype"] == 8:
            e["sel"] = (part, ev["array"][0])
            button = 5 if part == 2 else 4
            return [("UPDATE", screen, [], self._enable(screen, {button: True}))]
        if part in {4, 5}:
            source, row = e.pop("sel")
            if part == 4:
                assert source == 8
                e["members"].append(self._free_points()[row])
            else:
                assert source == 2
                e["members"].pop(row)
            return [("UPDATE", screen, [], self._members_parts(screen))]
        if part == 9:

            def save(_answer: dict[str, Any]) -> list[Update]:
                e["group"].members = list(e["members"])
                return self._close(screen)

            return self._ask(_confirm(screen), save)
        if part == 10:
            return self._close(screen)
        return [("UPDATE", screen, [], [])]

    def _settings_parts(self, screen: int) -> Parts:
        g: Group = self.dl_edit["group"]
        if screen == 402:
            return [
                (4, "TEXT", g.name),
                (5, "TEXT", _t(g.lower)),
                (6, "TEXT", _t(g.upper)),
            ]
        return [
            (2, "TEXT", g.name),
            (5, "TEXT", g.point),
            (8, "TEXT", _t(g.outdoor[0])),
            (12, "TEXT", _t(g.outdoor[1])),
            (16, "TEXT", _t(g.setpoint[0])),
            (20, "TEXT", _t(g.setpoint[1])),
        ]

    def _settings_event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part, e = ev["screen"], ev["part"], self.dl_edit
        name_button, ok, cancel = (9, 12, 13) if screen == 402 else (3, 23, 24)
        numbers = (
            {10: "lower", 11: "upper"}
            if screen == 402
            else {10: "out0", 14: "out1", 18: "sp0", 22: "sp1"}
        )
        if part == name_button:

            def rename(answer: dict[str, Any]) -> list[Update]:
                e["name"] = answer["string"]
                return []

            return self._ask(_name_dialog(screen, e["name"]), rename)
        if part in numbers:

            def number(answer: dict[str, Any]) -> list[Update]:
                e[numbers[part]] = _value(answer)
                return []

            return self._ask(_number_dialog(screen, 2, 0, 40), number)
        if part == 6 and screen == 502:
            self.open_screens.add(503)
            rows = _rows([["AI", n] for n in self.analog_inputs])
            return [("OPEN", 503, [-1], [(1, "LIST_ROW", rows)])]
        if part == ok:
            return self._ask(_confirm(screen), lambda _a: self._save_group(screen))
        if part == cancel:
            return self._close(screen)
        return [("UPDATE", screen, [], [])]

    def _save_group(self, screen: int) -> list[Update]:
        e = self.dl_edit
        g: Group = e["group"]
        g.name = e["name"]
        g.lower = e.get("lower", g.lower)
        g.upper = e.get("upper", g.upper)
        g.point = e.get("point", g.point)
        g.outdoor = (e.get("out0", g.outdoor[0]), e.get("out1", g.outdoor[1]))
        g.setpoint = (e.get("sp0", g.setpoint[0]), e.get("sp1", g.setpoint[1]))
        return self._close(screen)

    def _inputs_event(self, ev: dict[str, Any]) -> list[Update]:
        part = ev["part"]
        if part == 1 and ev["dtype"] == 1:
            self.dl_edit["ai"] = ev["number"]
            return [("UPDATE", 503, [], [])]
        if part == 2:
            self.dl_edit["point"] = self.analog_inputs[self.dl_edit.pop("ai")]
            text = (5, "TEXT", self.dl_edit["point"])
            return [*self._close(503), ("UPDATE", 502, [], [text])]
        return self._close(503)

    # -- heating mode optimisation (600) ----------------------------------------
    def _hmo_parts(self) -> Parts:
        row = self.dl_selected.get(600)
        rows = _rows([[n, ON if on else ""] for n, on in self.hmo.items()])
        parts: Parts = [
            (2, "LIST_COLUMN", [["Name", 100, 0, 0, 0], ["Activation", 80, 0, 0, 0]]),
            (2, "LIST_ROW", rows),
            (4, "LABEL", ON),
            (5, "LABEL", OFF),
            *self._enable(600, {6: True, 7: any(self.hmo.values())}),
        ]
        if row is None:
            return [
                *parts,
                (5, "CHECK_STATE", 1),
                *self._enable(600, {4: False, 5: False}),
            ]
        on = list(self.hmo.values())[row]
        return [
            *parts,
            (4, "CHECK_STATE", 1 if on else 0),
            (5, "CHECK_STATE", 0 if on else 1),
            *self._enable(600, {4: True, 5: True}),
        ]

    def _hmo_event(self, ev: dict[str, Any]) -> list[Update]:
        part = ev["part"]
        if part == 2 and ev["dtype"] == 1:
            self.dl_selected[600] = ev["number"]
            return [("UPDATE", 600, [], self._hmo_parts())]
        if part == 8:
            return self._close(600)
        if part in {4, 5, 6, 7}:

            def apply(_answer: dict[str, Any]) -> list[Update]:
                row = self.dl_selected.get(600)
                names = list(self.hmo) if part in {6, 7} else [list(self.hmo)[row or 0]]
                for name in names:
                    self.hmo[name] = part in {4, 6}
                return [("UPDATE", 600, [], self._hmo_parts())]

            return self._ask(_confirm(600), apply)
        return [("UPDATE", 600, [], [])]

    # -- interlock (900-902, 905) -------------------------------------------------
    def _il_rows(self) -> dict[str, Any]:
        return _rows([[ON if p.enabled else "", p.name] for p in self.interlocks])

    def _il_main(self) -> Parts:
        columns = [["Activation", 80, 0, 0, 0], ["Program Name", 200, 0, 0, 0]]
        return [
            (1, "LIST_COLUMN", columns),
            (1, "LIST_ROW", self._il_rows()),
            (3, "LABEL", ON),
            (4, "LABEL", OFF),
            (4, "CHECK_STATE", 1),
            *self._enable(900, {3: False, 4: False, 6: True}),
            *self._enable(900, dict.fromkeys((7, 8, 9, 10), False)),
        ]

    def _il_selection(self) -> Parts:
        row = self.dl_selected.get(900)
        if row is None:
            return self._il_main()
        p = self.interlocks[row]
        return [
            (1, "LIST_ROW", self._il_rows()),
            (3, "CHECK_STATE", 1 if p.enabled else 0),
            (4, "CHECK_STATE", 0 if p.enabled else 1),
            *self._enable(900, dict.fromkeys((3, 4, 7, 8, 9, 10), True)),
        ]

    def _il_update(self) -> list[Update]:
        return [("UPDATE", 900, [], self._il_selection())]

    def _il_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update]:
        part = ev["part"]
        if part == 1 and ev["dtype"] == 1:
            self.dl_selected[900] = ev["number"]
            return [("UPDATE", 900, [], self._il_selection())]
        if part == 11:
            return self._close(900)
        if part == 6:

            def add(answer: dict[str, Any]) -> list[Update]:
                self.interlocks.append(Interlock(answer["string"]))
                return self._il_update()

            return self._ask(_name_dialog(900, "Program 01"), add)
        row = self.dl_selected[900]
        assert row is not None
        program = self.interlocks[row]
        if part in {3, 4}:

            def toggle(_answer: dict[str, Any]) -> list[Update]:
                program.enabled = part == 3
                return self._il_update()

            return self._ask(_confirm(900), toggle)
        if part == 8:

            def delete(_answer: dict[str, Any]) -> list[Update]:
                self.interlocks.remove(program)
                self.dl_selected[900] = None
                return [("UPDATE", 900, [], self._il_main())]

            return self._ask(_confirm(900), delete)
        if part in {7, 9}:

            def name(answer: dict[str, Any]) -> list[Update]:
                if part == 9:
                    program.name = answer["string"]
                else:
                    self.interlocks.append(
                        Interlock(
                            answer["string"],
                            inputs=list(program.inputs),
                            outputs=[list(o) for o in program.outputs],
                        )
                    )
                return self._il_update()

            return self._ask(_name_dialog(900, program.name), name)
        if program.enabled:  # Edit: only a read-only mode is offered
            return self._ask(
                _confirm(900), lambda _a: self._open_setup(program, read_only=True)
            )
        return self._open_setup(program, read_only=False)

    def _open_setup(self, program: Interlock, *, read_only: bool) -> list[Update]:
        self.dl_edit = {
            "program": program,
            "inputs": list(program.inputs),
            "outputs": [list(o) for o in program.outputs],
            "conditions": list(program.conditions),
            "intervals": list(program.intervals),
            "condition_of": {k: _copy(v) for k, v in program.condition_of.items()},
            "action_of": {k: _copy(v) for k, v in program.action_of.items()},
            "tab": 0,
        }
        self.read_only_setup = read_only
        self.open_screens.add(901)
        parts = [*self._setup_parts(), *self._enable(901, {11: not read_only})]
        return [("OPEN", 901, [-1], parts)]

    def _setup_parts(self) -> Parts:
        e = self.dl_edit
        parts: Parts = [(3, "LIST_ROW", _rows([["Input", n] for n in e["inputs"]]))]
        for tab, (combo, rows, interval) in enumerate(((5, 7, 9), (14, 16, 18))):
            parts += [
                (combo, "COMBO_STRS", CONDITIONS),
                (combo, "SINGLE_SELIDX", e["conditions"][tab]),
                (rows, "LIST_ROW", _rows([["Output", n] for n in e["outputs"][tab]])),
                (interval, "TEXT", f"{e['intervals'][tab]} min"),
            ]
        return parts

    def _setup_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update]:
        part, e = ev["part"], self.dl_edit
        tabs = {5: 0, 6: 0, 10: 0, 14: 1, 15: 1, 19: 1}
        if part == 4 and ev["event"] == 8:
            e["tab"] = ev["number"]
            return [("UPDATE", 901, [], [])]
        if part in tabs and tabs[part] != e["tab"]:
            msg = f"901/{part} clicked on another tab"
            raise RuntimeError(msg)
        if part in {5, 14} and ev["dtype"] == 1:
            e["conditions"][tabs[part]] = ev["number"]
            return [("UPDATE", 901, [], [])]
        if part == 2:
            return self._open_picker(902, e["inputs"], "Input", self.points)
        if part in {6, 15}:
            e["picking"] = tabs[part]
            chosen = e["outputs"][tabs[part]]
            return self._open_picker(905, chosen, "Output", self.points)
        if part in {10, 19}:

            def number(answer: dict[str, Any]) -> list[Update]:
                e["intervals"][tabs[part]] = int(_value(answer))
                return []

            return self._ask(_number_dialog(901, 0, 0, 60), number)
        if part == 11:
            return self._ask(_confirm(901), lambda _a: self._save_setup())
        if part == 12:
            return self._close(901)
        return [("UPDATE", 901, [], [])]

    def _save_setup(self) -> list[Update]:
        e = self.dl_edit
        program: Interlock = e["program"]
        program.inputs = list(e["inputs"])
        program.outputs = [list(o) for o in e["outputs"]]
        program.conditions = list(e["conditions"])
        program.intervals = list(e["intervals"])
        program.condition_of = e["condition_of"]
        program.action_of = e["action_of"]
        return self._close(901)

    def _picker_lists(self, screen: int) -> tuple[int, int, int, int]:
        """Return the chosen list, available list, add and remove parts."""
        return (2, 10, 6, 7) if screen == 902 else (2, 12, 8, 9)

    def _open_picker(
        self, screen: int, chosen: list[str], kind: str, points: list[str]
    ) -> list[Update]:
        self.dl_edit["picked"] = list(chosen)
        self.dl_edit["kind"] = kind
        self.dl_edit["points"] = points
        self.open_screens.add(screen)
        parts = self._picker_parts(screen)
        if screen == 902:
            parts += [(12, "COMBO_STRS", INPUT_CONDITIONS), (12, "SINGLE_SELIDX", 0)]
        return [("OPEN", screen, [-1], parts)]

    def _picker_parts(self, screen: int) -> Parts:
        e = self.dl_edit
        chosen, available, add, remove = self._picker_lists(screen)
        free = [p for p in e["points"] if p not in e["picked"]]
        setup = N_SETUP if screen == 902 else O_SETUP
        return [
            (chosen, "LIST_ROW", _rows([[e["kind"], n] for n in e["picked"]])),
            (available, "LIST_ROW", _rows([[e["kind"], n] for n in free])),
            *self._enable(screen, {add: False, remove: False, setup: False}),
        ]

    def _picker_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update]:
        screen, part, e = ev["screen"], ev["part"], self.dl_edit
        chosen, available, add, remove = self._picker_lists(screen)
        setup = N_SETUP if screen == 902 else O_SETUP
        multi = screen == 905
        if part in {chosen, available} and ev["dtype"] == (8 if multi else 1):
            row = ev["array"][0] if multi else ev["number"]
            e["sel"] = (part, row)
            buttons = {remove: True, setup: True} if part == chosen else {add: True}
            return [("UPDATE", screen, [], self._enable(screen, buttons))]
        if part == setup:
            source, row = e["sel"]
            assert source == chosen
            if screen == 902:
                return self._open_condition(e["picked"][row])
            return self._open_action(e["picked"][row])
        if part in {add, remove}:
            source, row = e.pop("sel")
            if part == add:
                assert source == available
                free = [p for p in e["points"] if p not in e["picked"]]
                e["picked"].append(free[row])
            else:
                assert source == chosen
                e["picked"].pop(row)
            return [("UPDATE", screen, [], self._picker_parts(screen))]
        if part == 13:
            if screen == 902:
                e["inputs"] = list(e["picked"])
            else:
                e["outputs"][e["picking"]] = list(e["picked"])
            return [*self._close(screen), ("UPDATE", 901, [], self._setup_parts())]
        if part == 14:
            return self._close(screen)
        return [("UPDATE", screen, [], [])]

    # -- interlock conditions (903), management points (904), actions (906) -------
    def _open_condition(self, point: str) -> list[Update]:
        e = self.dl_edit
        e["cond_point"] = point
        e["cond"] = _copy(e["condition_of"].get(point, default_condition()))
        e["ctab"] = 0
        self.open_screens.add(903)
        return [("OPEN", 903, [-1], self._condition_parts(point))]

    def _condition_parts(self, point: str) -> Parts:
        c = self.dl_edit["cond"]
        names = ["On/Off", "Operation Mode", "Analog", "Multistate"]
        return [
            (27, "TAB_NAME", names),
            (27, "TAB_VISIBLE", [0, [1, 1, 1, 0]]),
            (28, "LABEL", "On"),
            (29, "LABEL", "Error"),
            *[(p, "CHECK_STATE", int(c["state"] == p)) for p in (28, 29)],
            (32, "COMBO_STRS", MODES),
            (32, "SINGLE_SELIDX", c["mode"]),
            (2, "TEXT", point),
            (3, "LABEL", "&gt;"),
            (4, "LABEL", "&lt;"),
            *[(p, "CHECK_STATE", int(c["compare"] == p)) for p in (3, 4)],
            *[(p, "CHECK_STATE", int(c["ref"] == p)) for p in (6, 9)],
            (7, "TEXT", _t(c["constant"])),
            (10, "TEXT", c["point"]),
            (14, "TEXT", _t(c["offset"])),
            (17, "TEXT", _t(c["hysteresis"])),
            (18, "VISIBLED", 0),  # hidden but usable: never refused for that
            (35, "COMBO_STRS", MULTISTATES),
            (35, "SINGLE_SELIDX", c["multistate"]),
            *self._enable(903, {35: False, 11: True}),
        ]

    def _condition_event(self, ev: dict[str, Any]) -> list[Update]:
        part, e = ev["part"], self.dl_edit
        c = e["cond"]
        if part == 27 and ev["event"] == 8:
            e["ctab"] = ev["number"]
            return [("UPDATE", 903, [], [])]
        if part in CONDITION_TABS and CONDITION_TABS[part] != e["ctab"]:
            msg = f"903/{part} clicked on another tab"
            raise RuntimeError(msg)
        if part in {28, 29}:
            c["state"] = part
        elif part in {3, 4}:
            c["compare"] = part
        elif part in {6, 9}:
            c["ref"] = part
        elif part in {32, 35} and ev["dtype"] == 1:
            c["mode" if part == 32 else "multistate"] = ev["number"]
        elif part in {8, 15, 18}:
            key = {8: "constant", 15: "offset", 18: "hysteresis"}[part]

            def number(answer: dict[str, Any]) -> list[Update]:
                c[key] = _value(answer)
                return []

            return self._ask(_number_dialog(903, 1, -50, 100), number)
        elif part == 11:
            self.open_screens.add(904)
            rows = _rows([["Analog", n] for n in self.analog_inputs])
            return [("OPEN", 904, [-1], [(1, "LIST_ROW", rows)])]
        elif part == 20:
            e["condition_of"][e["cond_point"]] = c
            return self._close(903)
        elif part == 21:
            return self._close(903)
        return [("UPDATE", 903, [], [])]

    def _point_picker_event(self, ev: dict[str, Any]) -> list[Update]:
        part, e = ev["part"], self.dl_edit
        if part == 1 and ev["dtype"] == 1:
            e["mp"] = ev["number"]
            return [("UPDATE", 904, [], [])]
        if part == 2:
            e["cond"]["point"] = self.analog_inputs[e.pop("mp")]
            text = (10, "TEXT", e["cond"]["point"])
            return [*self._close(904), ("UPDATE", 903, [], [text])]
        return self._close(904)

    def _open_action(self, point: str) -> list[Update]:
        e = self.dl_edit
        e["act_point"] = point
        e["act"] = _copy(e["action_of"].get(point, default_action()))
        e["atab"] = 0
        self.open_screens.add(906)
        return [("OPEN", 906, [-1], self._action_parts())]

    def _action_parts(self) -> Parts:
        a = self.dl_edit["act"]
        radios = [
            (radio, "CHECK_STATE", int(a["radios"][box] == radio))
            for radio, box in ACTION_RADIOS.items()
        ]
        names = ["On/Off", "Lock", "Attributes", "Fan Speed", "Analog"]
        return [
            (1, "TAB_NAME", [*names, "Low Noise"]),
            *[(p, "CHECK_STATE", v) for p, v in a["ticked"].items()],
            *radios,
            (18, "COMBO_STRS", MODES),
            (18, "SINGLE_SELIDX", a["mode"]),
            (20, "CHECK_STATE", 1),
            (21, "TEXT", _t(a["setpoint"])),
            (31, "ICONID", 3011),
            (54, "VISIBLED", 0),  # hidden but usable
            *self._enable(906, {55: False}),
        ]

    def _action_event(self, ev: dict[str, Any]) -> list[Update]:
        part, e = ev["part"], self.dl_edit
        a = e["act"]
        if part == 1 and ev["event"] == 8:
            e["atab"] = ev["number"]
            return [("UPDATE", 906, [], [])]
        if part in ACTION_TABS and ACTION_TABS[part] != e["atab"]:
            msg = f"906/{part} clicked on another tab"
            raise RuntimeError(msg)
        if part in a["ticked"] and ev["dtype"] == 1:
            a["ticked"][part] = ev["number"]
        elif part in ACTION_RADIOS:
            a["radios"][ACTION_RADIOS[part]] = part
        elif part == 18 and ev["dtype"] == 1:
            a["mode"] = ev["number"]
        elif part == 23:

            def number(answer: dict[str, Any]) -> list[Update]:
                a["setpoint"] = _value(answer)
                return []

            return self._ask(_number_dialog(906, 2, 16, 32), number)
        elif part in {5, 6}:
            if part == 5:
                e["action_of"][e["act_point"]] = a
            return self._close(906)
        return [("UPDATE", 906, [], [])]


__all__ = ["FakeDealerITM", "Group", "Interlock"]
