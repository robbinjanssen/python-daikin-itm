# pylint: disable=too-many-lines
"""A simulated iTM with demand (power limit) control and energy saving.

Demand control (menu list part 109 -> 3300) with its legend (3302), target power
(3312), setup (3301: controls, forced level, status 3310), the level setups
(3307-3309) with their groups (3311) and the graphs (3303 -> 3304-3306); energy
saving suspend (main menu bar part 17 -> 1400, also while the indicator 16 is
hidden); and the service screens 1508, 1512 / 1513 (by ``demand_mode``) and their
port picker 1514. Values are generic placeholders.

Strict like the real iTM: a click on a part it reports disabled (``ENABLED 0``)
raises (the real web server hangs then). Saving screens answer OK with a CONFIRM;
Yes saves, Cancel discards. The forced level saves when picked (after a CONFIRM).
With ``available=False`` the power limit button opens nothing, as on a real iTM
without the dealer option.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import fake_energy
from .fake_itm import FakeITM, _rows

if TYPE_CHECKING:
    from collections.abc import Callable

    from .fake_itm import Update

# Importing fake_energy registers the graph encoders with the base fake.
GRAPH_ENCODERS = fake_energy.__name__
NUMBER_ID, CONFIRM_ID = 60, 61
CONFIRM = [CONFIRM_ID, 2, "Execute setting?", "Confirm", "Yes", "No"]
FORCED = ["None", "Level 1", "Level 2", "Level 3"]
START_LEVELS = [f"Level {n}" for n in range(1, 9)]
SHIFTS = ["0", "1", "2", "3"]
CAPACITIES = ["100%", "80%", "60%", "40%"]
CONDITIONS = ["Always", "Thermo on"]
RESUMES = ["Auto", "Manual"]
PORTS = ["Port 1", "Port 2", "Port 3", "Port 4"]
# Level setup screens: start combo, level combos, OK, Cancel.
LAYOUTS: dict[int, tuple[int | None, tuple[int, ...], int, int]] = {
    3307: (13, tuple(range(24, 32)), 40, 41),
    3308: (None, tuple(range(13, 21)), 29, 30),
    3309: (5, (), 7, 8),
}
DATA_SCREENS = {10: 3304, 18: 3305, 23: 3306}
DATA_CANCEL = {3304: 5, 3305: 4, 3306: 4}
SCREENS = {
    3300,
    3301,
    3302,
    3303,
    3304,
    3305,
    3306,
    3307,
    3308,
    3309,
    3310,
    3311,
    3312,
    1400,
    1508,
    1512,
    1513,
    1514,
}


@dataclass
class Group:
    """A demand group and its level setup."""

    name: str
    members: list[str] = field(default_factory=list)
    condition: int = 0
    resume: int = 0
    start: int = 0
    levels: list[int] = field(default_factory=lambda: [0] * 8)
    upper: float = 28.0
    lower: float = 18.0


@dataclass
class Control:
    """A row of the setup's control list."""

    name: str
    screen: int  # 3307, 3308 or 3309
    enabled: bool = True
    groups: list[Group] = field(default_factory=list)


def _num(value: float) -> str:
    return f"{value:g}"


def _graph() -> dict[str, Any]:
    return {
        "vaxis": [("0", 0), ("100", 0)],
        "haxis": [("0:00", 0), ("0:30", 0)],
        "series": [(1, [10.0, 20.0, None])],
    }


class FakeDemandITM(FakeITM):  # pylint: disable=too-many-instance-attributes,too-many-public-methods
    """A FakeITM with demand control, energy saving and their service screens."""

    def __init__(self, *, available: bool = True, demand_mode: int = 1512) -> None:
        """Start with two controls, a target of 100 kW and energy saving active."""
        super().__init__()
        self.available = available
        self.demand_mode = demand_mode  # the screen the service button opens
        self.disabled: set[tuple[int, int]] = set()  # forced ENABLED 0 (test helper)
        self.enabled: dict[tuple[int, int], int] = {}  # as last reported
        names = [u.name for u in self.units]
        self.controls = [
            Control(
                "Control A",
                3307,
                groups=[Group("Group 1", [names[0]]), Group("Group 2")],
            ),
            Control("Control B", 3308, groups=[Group("Group 3", [names[1]])]),
            Control("Control C", 3309, enabled=False),
        ]
        self.points = names
        self.target = 100.0
        self.forced = 0
        self.saving_configured = True
        self.suspended = False
        self.energy_saving = True
        self.pulse: dict[str, Any] = {
            "ports": ["Port 1", ""],
            "coefficient": 1.0,
            "sync_external": False,
            "return": 5.0,
            "cutoff": 10.0,
        }
        self.interlock = ["Port 2", "", ""]
        self.saved: list[str] = []
        self.dm_selected: dict[tuple[int, int], int] = {}
        self.dm_pending: dict[str, Any] = {}
        self.dm_dialog: tuple[str, int, Callable[[Any], list[Update]]] | None = None
        self.picker_for: tuple[int, int] | None = None

    # -- helpers ----------------------------------------------------------------------
    def _menu_bar(self) -> list[tuple[int, str, Any]]:
        return self._parts(0, super()._menu_bar())

    def _sync_indicator(self) -> list[Update]:
        self.energy_saving = self.saving_configured and not self.suspended
        return [("UPDATE", 0, [], [(16, "VISIBLED", 1 if self.energy_saving else 0)])]

    def _parts(
        self, screen: int, parts: list[tuple[int, str, Any]]
    ) -> list[tuple[int, str, Any]]:
        """Record what ENABLED the client is told; add the forced disabled parts."""
        parts = parts + [(p, "ENABLED", 0) for s, p in self.disabled if s == screen]
        for part, ptype, value in parts:
            if ptype == "ENABLED":
                self.enabled[screen, part] = value
        return parts

    def _open(self, screen: int, parts: list[tuple[int, str, Any]]) -> list[Update]:
        self.open_screens.add(screen)
        return [("OPEN", screen, [-1], self._parts(screen, parts))]

    def _update(self, screen: int, parts: list[tuple[int, str, Any]]) -> list[Update]:
        return [("UPDATE", screen, [], self._parts(screen, parts))]

    def _close(self, screen: int) -> list[Update]:
        self.open_screens.discard(screen)
        return [("CLOSE", screen, [], [])]

    def _hang_if_disabled(self, screen: int, part: int) -> None:
        if (screen, part) in self.disabled or self.enabled.get((screen, part)) == 0:
            msg = f"{screen}/{part} clicked while disabled: the real iTM hangs"
            raise RuntimeError(msg)

    def _confirm(
        self, screen: int, apply: Callable[[Any], list[Update]]
    ) -> list[Update]:
        self.dm_dialog = ("confirm", screen, apply)
        return [("DLG_OPEN", screen, CONFIRM, [])]

    def _number(
        self, screen: int, apply: Callable[[Any], list[Update]]
    ) -> list[Update]:
        self.dm_dialog = ("number", screen, apply)
        data = [NUMBER_ID, 6, 1, "Value", 0, 0, 0, 0, 9999, 0, 2, 1, *["x"] * 11]
        return [("DLG_OPEN", screen, data, [])]

    def _dialog_answer(self, ev: dict[str, Any]) -> list[Update]:
        assert self.dm_dialog is not None
        kind, screen, apply = self.dm_dialog
        self.dm_dialog = None
        part = NUMBER_ID if kind == "number" else CONFIRM_ID
        close: list[Update] = [("DLG_CLOSE", screen, [part, 2], [])]
        if ev["event"] != 1:
            return close
        value = ev["array"][0] + ev["array"][1] / 1000 if kind == "number" else None
        return close + apply(value)

    # -- routing ----------------------------------------------------------------------
    def _event(self, ev: dict[str, Any]) -> list[Update]:  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        if self.dm_dialog is not None and part in {NUMBER_ID, CONFIRM_ID}:
            return self._dialog_answer(ev)
        if event == 1 and ev["dtype"] == 0 and (screen in SCREENS or screen in {0, 5}):
            self._hang_if_disabled(screen, part)
        if screen == 0 and part == 0:
            return super()._event(ev) + self._update(5, [])
        if screen == 0 and part == 17 and event == 1:
            return self._open_suspend()
        if screen == 5 and self.menu_open and part == 109:
            return self._open_main() if self.available else []
        if screen == 5 and self.menu_open and part in {79, 113}:
            if not self.service_mode:
                info = [92, 3, "Function used by another user.", "Info", "Close"]
                return [("DLG_OPEN", 5, info, [])]
            return self._open_status() if part == 79 else self._open_demand_setup()
        if screen in SCREENS and screen in self.open_screens:
            handler = getattr(self, f"_on_{screen}")
            result: list[Update] = handler(ev)
            return result
        return super()._event(ev)

    # -- demand control main (3300), legend (3302), target power (3312) ---------------
    def _open_main(self) -> list[Update]:
        texts = {5: "Normal", 7: "30 min", 12: "80", 15: "Period", 17: "12"}
        texts |= {19: "30", 22: "60", 25: "-20"}
        parts: list[tuple[int, str, Any]] = [(p, "TEXT", t) for p, t in texts.items()]
        parts += [
            (9, "TEXT", _num(self.target)),
            (10, "LABEL", "kW"),
            (1, "BARLINE_GRAPH", _graph()),
        ]
        return self._open(3300, parts)

    def _on_3300(self, ev: dict[str, Any]) -> list[Update]:
        part = ev["part"]
        if part == 2:
            labels = [(1, "LABEL", "Target"), (2, "LABEL", "Current")]
            labels += [(3, "LABEL", "Predicted"), (5, "LABEL", "Consumption")]
            return self._open(3302, labels)
        if part == 27:
            self.dm_pending = {}
            return self._open(3312, [(2, "TEXT", _num(self.target))])
        if part == 28:
            self.dm_selected.pop((3301, 1), None)
            return self._open(3301, self._setup_parts())
        if part == 29:
            return self._open(3303, self._graph_parts())
        if part == 30:
            return self._close(3300)
        return []

    def _on_3302(self, ev: dict[str, Any]) -> list[Update]:
        return self._close(3302) if ev["part"] == 4 else []

    def _on_3312(self, ev: dict[str, Any]) -> list[Update]:
        part = ev["part"]
        if part == 3:

            def enter(value: Any) -> list[Update]:
                self.dm_pending["target"] = value
                return self._update(3312, [(2, "TEXT", _num(value))])

            return self._number(3312, enter)
        if part == 4:

            def save(_: Any) -> list[Update]:
                self.target = self.dm_pending.get("target", self.target)
                self.saved.append("target")
                return self._close(3312) + self._update(
                    3300, [(9, "TEXT", _num(self.target))]
                )

            return self._confirm(3312, save)
        if part == 5:
            return self._close(3312)
        return []

    # -- setup (3301), control status (3310) ------------------------------------------
    def _setup_parts(self) -> list[tuple[int, str, Any]]:
        kinds = {3307: "Temperature", 3308: "Capacity", 3309: "On/Off"}
        rows = [[c.name, kinds[c.screen]] for c in self.controls]
        return [
            (1, "LIST_ROW", _rows(rows)),
            (3, "CHECK_STATE", 0),
            (4, "CHECK_STATE", 0),
            (5, "ENABLED", 0),
            (6, "ENABLED", 1),
            (7, "COMBO_STRS", FORCED),
            (7, "SINGLE_SELIDX", self.forced),
        ]

    def _control(self) -> Control:
        return self.controls[self.dm_selected[3301, 1]]

    def _on_3301(self, ev: dict[str, Any]) -> list[Update]:  # pylint: disable=too-many-return-statements
        part = ev["part"]
        if part == 1 and ev["dtype"] == 1:
            self.dm_selected[3301, 1] = ev["number"]
            c = self._control()
            return self._update(
                3301,
                [
                    (3, "CHECK_STATE", 1 if c.enabled else 0),
                    (4, "CHECK_STATE", 0 if c.enabled else 1),
                    (5, "ENABLED", 1 if c.groups or c.screen == 3309 else 0),
                ],
            )
        if part == 5:
            return self._open_level_setup()
        if part == 6:
            return self._open(3310, self._status_parts())
        if part == 7 and ev["dtype"] == 1:
            index = ev["number"]

            def save(_: Any) -> list[Update]:
                self.forced = index
                self.saved.append("forced")
                return []

            return self._confirm(3301, save)
        if part == 8:
            return self._close(3301)
        return []

    def _status_parts(self) -> list[tuple[int, str, Any]]:
        tabs = ["Tab 1", "Tab 2", "Tab 3"]
        parts: list[tuple[int, str, Any]] = [(1, "TAB_NAME", tabs)]
        for status, level, groups in ((3, 5, 6), (9, 11, 12), (14, 16, 17)):
            parts += [
                (status, "TEXT", "Running"),
                (level, "TEXT", FORCED[self.forced]),
                (groups, "LIST_ROW", _rows([["Group 1", "Level 1"]])),
            ]
        return parts

    def _on_3310(self, ev: dict[str, Any]) -> list[Update]:
        return self._close(3310) if ev["part"] == 7 else []

    # -- level setups (3307-3309) and groups (3311) -----------------------------------
    def _group(self) -> Group:
        c = self._control()
        return c.groups[self.dm_selected.get((c.screen, 2), 0)]

    def _level_values(self, screen: int) -> list[tuple[int, str, Any]]:
        start, levels, _, _ = LAYOUTS[screen]
        c = self._control()
        if not c.groups:
            return []
        g = self._group()
        options = SHIFTS if screen == 3307 else CAPACITIES
        parts: list[tuple[int, str, Any]] = []
        if start is not None:
            parts += [
                (start, "COMBO_STRS", START_LEVELS),
                (start, "SINGLE_SELIDX", g.start),
            ]
        for i, p in enumerate(levels):
            parts += [(p, "COMBO_STRS", options), (p, "SINGLE_SELIDX", g.levels[i])]
        if screen == 3307:
            parts += [(5, "TEXT", _num(g.upper)), (9, "TEXT", _num(g.lower))]
        return parts

    def _open_level_setup(self) -> list[Update]:
        c = self._control()
        self.dm_selected[c.screen, 2] = 0
        self.dm_pending = {}
        rows = {**_rows([[g.name] for g in c.groups]), "selected": [0]}
        parts = [(2, "LIST_ROW", rows), (3, "ENABLED", 1 if c.groups else 0)]
        return self._open(c.screen, parts + self._level_values(c.screen))

    def _on_level(self, ev: dict[str, Any]) -> list[Update]:  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        screen, part = ev["screen"], ev["part"]
        start, levels, ok, cancel = LAYOUTS[screen]
        if part == 2 and ev["dtype"] == 1:
            self.dm_selected[screen, 2] = ev["number"]
            self.dm_pending = {}
            return self._update(screen, self._level_values(screen))
        if ev["dtype"] == 1 and (part == start or part in levels):
            self.dm_pending[part] = ev["number"]
            return []
        if screen == 3307 and part in {7, 11}:
            key = "upper" if part == 7 else "lower"

            def enter(value: Any) -> list[Update]:
                self.dm_pending[key] = value
                return self._update(3307, [(part - 2, "TEXT", _num(value))])

            return self._number(3307, enter)
        if part == 3:
            return self._open_group()
        if part == ok:

            def save(_: Any) -> list[Update]:
                if not self._control().groups:
                    return self._close(screen)
                g = self._group()
                for key, value in self.dm_pending.items():
                    if key == start:
                        g.start = value
                    elif key in levels:
                        g.levels[levels.index(key)] = value
                    else:
                        setattr(g, key, value)
                self.saved.append(f"setup {g.name}")
                return self._close(screen)

            return self._confirm(screen, save)
        if part == cancel:
            return self._close(screen)
        return []

    _on_3307 = _on_level
    _on_3308 = _on_level
    _on_3309 = _on_level

    def _dm_group_parts(self) -> list[tuple[int, str, Any]]:
        members = self.dm_pending["members"]
        available = [p for p in self.points if p not in members]
        return [
            (2, "LIST_ROW", _rows([["Indoor", m] for m in members])),
            (8, "LIST_ROW", _rows([["Indoor", p] for p in available])),
            (4, "ENABLED", 0),
            (5, "ENABLED", 0),
        ]

    def _open_group(self) -> list[Update]:
        g = self._group()
        self.dm_pending = {"members": list(g.members)}
        self.dm_selected.pop((3311, 2), None)
        self.dm_selected.pop((3311, 8), None)
        parts = [
            *self._dm_group_parts(),
            (10, "COMBO_STRS", CONDITIONS),
            (10, "SINGLE_SELIDX", g.condition),
            (14, "COMBO_STRS", RESUMES),
            (14, "SINGLE_SELIDX", g.resume),
        ]
        return self._open(3311, parts)

    def _on_3311(self, ev: dict[str, Any]) -> list[Update]:  # pylint: disable=too-many-return-statements
        part, p = ev["part"], self.dm_pending
        if part in {2, 8} and ev["dtype"] == 1:
            self.dm_selected[3311, part] = ev["number"]
            return self._update(3311, [(5 if part == 2 else 4, "ENABLED", 1)])
        if part in {4, 5}:
            members = p["members"]
            if part == 4:
                available = [x for x in self.points if x not in members]
                members.append(available[self.dm_selected.pop((3311, 8))])
            else:
                members.pop(self.dm_selected.pop((3311, 2)))
            return self._update(3311, self._dm_group_parts())
        if part in {10, 14} and ev["dtype"] == 1:
            p["condition" if part == 10 else "resume"] = ev["number"]
            return []
        if part == 11:

            def save(_: Any) -> list[Update]:
                g = self._group()
                g.members = list(p["members"])
                g.condition = p.get("condition", g.condition)
                g.resume = p.get("resume", g.resume)
                self.saved.append(f"group {g.name}")
                self.dm_pending = {}
                return self._close(3311)

            return self._confirm(3311, save)
        if part == 12:
            return self._close(3311)
        return []

    # -- graphs (3303) and their data (3304-3306) -------------------------------------
    def _graph_parts(self) -> list[tuple[int, str, Any]]:
        parts: list[tuple[int, str, Any]] = [
            (1, "TAB_NAME", ["Daily", "Monthly", "Yearly"])
        ]
        parts.extend((graph, "BARLINE_GRAPH", _graph()) for graph in (2, 12, 19))
        for combo, options in ((4, ["2025"]), (6, ["01"]), (8, ["01"])):
            parts += [(combo, "COMBO_STRS", options), (combo, "SINGLE_SELIDX", 0)]
        for combo, options in ((14, ["2025"]), (16, ["01"]), (21, ["2025"])):
            parts += [(combo, "COMBO_STRS", options), (combo, "SINGLE_SELIDX", 0)]
        return parts

    def _on_3303(self, ev: dict[str, Any]) -> list[Update]:
        part = ev["part"]
        if part in DATA_SCREENS:
            screen = DATA_SCREENS[part]
            rows = _rows([["00:30", "12.5"], ["01:00", "13.0"]])
            return self._open(
                screen, [(2, "TEXT", "01/01/2025"), (3, "LIST_ROW", rows)]
            )
        if part == 11:
            return self._close(3303)
        return []

    def _on_data(self, ev: dict[str, Any]) -> list[Update]:
        screen = ev["screen"]
        return self._close(screen) if ev["part"] == DATA_CANCEL[screen] else []

    _on_3304 = _on_data
    _on_3305 = _on_data
    _on_3306 = _on_data

    # -- energy saving suspend (1400) and status (1508) -------------------------------
    def _suspend_text(self) -> str:
        if not self.saving_configured:
            return "Off"
        return "Suspended" if self.suspended else "Active"

    def _open_suspend(self) -> list[Update]:
        parts = [
            (2, "TEXT", self._suspend_text()),
            (3, "ENABLED", 1 if self.saving_configured else 0),
        ]
        return self._open(1400, parts)

    def _on_1400(self, ev: dict[str, Any]) -> list[Update]:
        if ev["part"] == 3:

            def toggle(_: Any) -> list[Update]:
                self.suspended = not self.suspended
                self.saved.append("suspend" if self.suspended else "resume")
                text = [(2, "TEXT", self._suspend_text())]
                return self._update(1400, text) + self._sync_indicator()

            return self._confirm(1400, toggle)
        if ev["part"] == 4:
            return self._close(1400)
        return []

    def _open_status(self) -> list[Update]:
        texts: list[str] = ["", "", "", "", "", "", ""]
        if self.energy_saving:
            texts = ["10:00", "20 min", "100", "60", "80", "0", "Running"]
        parts = [
            (p, "TEXT", t) for p, t in zip((3, 5, 7, 9, 11, 13, 15), texts, strict=True)
        ]
        return self._open(1508, parts)

    def _on_1508(self, ev: dict[str, Any]) -> list[Update]:
        if ev["part"] == 16:

            def disable(_: Any) -> list[Update]:
                self.saving_configured = False
                self.saved.append("disable energy saving")
                return self._sync_indicator()

            return self._confirm(1508, disable)
        if ev["part"] == 17:
            return self._close(1508)
        return []

    # -- the power limit control setup (1512, 1513) and its port picker (1514) --------
    def _pulse_parts(self) -> list[tuple[int, str, Any]]:
        p = {**self.pulse, **self.dm_pending}
        ports = p["ports"]
        return [
            (3, "TEXT", ports[0]),
            (12, "TEXT", ports[1]),
            (6, "TEXT", _num(p["coefficient"])),
            (9, "CHECK_STATE", 1 if p["sync_external"] else 0),
            (10, "CHECK_STATE", 0 if p["sync_external"] else 1),
            (16, "TEXT", _num(p["return"])),
            (20, "TEXT", _num(p["cutoff"])),
        ]

    def _interlock_parts(self) -> list[tuple[int, str, Any]]:
        ports = self.dm_pending.get("ports", self.interlock)
        return [(p, "TEXT", t) for p, t in zip((2, 5, 8), ports, strict=True)]

    def _open_demand_setup(self) -> list[Update]:
        if self.demand_mode == 1512:
            self.dm_pending = {"ports": list(self.pulse["ports"])}
            return self._open(1512, self._pulse_parts())
        self.dm_pending = {"ports": list(self.interlock)}
        return self._open(1513, self._interlock_parts())

    def _open_picker(self, screen: int, index: int) -> list[Update]:
        self.picker_for = (screen, index)
        self.dm_selected.pop((1514, 2), None)
        rows = _rows([["DI", port] for port in PORTS])
        return self._open(1514, [(2, "LIST_ROW", rows), (3, "ENABLED", 0)])

    def _on_1512(self, ev: dict[str, Any]) -> list[Update]:  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        part, p = ev["part"], self.dm_pending
        if part in {4, 13}:
            return self._open_picker(1512, 0 if part == 4 else 1)
        if part in {29, 30}:
            p["ports"][0 if part == 29 else 1] = ""
            return self._update(1512, self._pulse_parts())
        if part in {7, 18, 22}:
            key = {7: "coefficient", 18: "return", 22: "cutoff"}[part]

            def enter(value: Any) -> list[Update]:
                p[key] = value
                return self._update(1512, self._pulse_parts())

            return self._number(1512, enter)
        if part in {9, 10}:
            p["sync_external"] = part == 9
            return self._update(1512, self._pulse_parts())
        if part == 23:

            def save(_: Any) -> list[Update]:
                self.pulse.update(p)
                self.saved.append("pulse")
                return self._close(1512)

            return self._confirm(1512, save)
        if part == 24:
            return self._close(1512)
        return []

    def _on_1513(self, ev: dict[str, Any]) -> list[Update]:
        part, ports = ev["part"], self.dm_pending["ports"]
        if part in {3, 6, 9}:
            return self._open_picker(1513, (3, 6, 9).index(part))
        if part in {13, 14, 15}:
            ports[part - 13] = ""
            return self._update(1513, self._interlock_parts())
        if part == 10:

            def save(_: Any) -> list[Update]:
                self.interlock = list(ports)
                self.saved.append("interlock")
                return self._close(1513)

            return self._confirm(1513, save)
        if part == 11:
            return self._close(1513)
        return []

    def _on_1514(self, ev: dict[str, Any]) -> list[Update]:
        part = ev["part"]
        if part == 2 and ev["dtype"] == 1:
            self.dm_selected[1514, 2] = ev["number"]
            return self._update(1514, [(3, "ENABLED", 1)])
        if part == 3 and self.picker_for is not None:
            screen, index = self.picker_for
            self.dm_pending["ports"][index] = PORTS[self.dm_selected[1514, 2]]
            parts = self._pulse_parts() if screen == 1512 else self._interlock_parts()
            return self._close(1514) + self._update(screen, parts)
        if part == 4:
            return self._close(1514)
        return []


__all__ = ["Control", "FakeDemandITM", "Group"]
