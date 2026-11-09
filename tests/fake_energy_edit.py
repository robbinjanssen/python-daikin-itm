# pylint: disable=protected-access
"""A simulated iTM whose energy navigator settings can be changed.

On top of FakeEnergyITM (which guards the reads): the planned (3202) and actual
(3203) consumption take an energy type pick, the yearly/monthly radios, NUMBER
inputs for the ratio, the CO2 factor and the selected month's value, and keep them
pending until OK (answered with a CONFIRM) saves them; Cancel discards them. The
energy type and group lists (3205, 3207) create and rename through a CHARINPUT dialog
and delete after a CONFIRM.

Buttons that need a selected row (a month's Change) stay disabled until one is
selected; a click on a disabled part fails the request, as in FakeEnergyITM.
"""

from __future__ import annotations

import copy
from typing import TYPE_CHECKING, Any

from .fake_energy import FakeEnergyITM, _rows

if TYPE_CHECKING:
    from collections.abc import Callable

    from .fake_itm import Update

NUMBER_ID, CONFIRM_ID, NAME_ID, DELETE_ID = 40, 41, 44, 46
RECALC_ID = 32
# Per tab of 3202: year radio, month radio, ratio text, ratio button, month list,
# month button; of 3203: month list, month button, CO2 text, CO2 button.
PLANNED_TABS = ((4, 11, 6, 8, 13, 12), (18, 25, 20, 22, 27, 26))
ACTUAL_TABS = ((5, 6, 8, 10), (21, 22, 24, 26))
MONTH_BUTTONS = {(3202, 13): 12, (3202, 27): 26, (3203, 5): 6, (3203, 21): 22}


def _number(value: list[int]) -> str:
    """Show a FLOAT answer ([whole, thousandths]) the way the iTM shows numbers."""
    return f"{value[0] + value[1] / 1000:.3f}".rstrip("0").rstrip(".") or "0"


class FakeEnergyEditITM(FakeEnergyITM):
    """A FakeEnergyITM whose energy navigator settings can be changed."""

    def __init__(self, *, data: bool = True) -> None:
        """Start like FakeEnergyITM, nothing pending."""
        super().__init__(data=data)
        self.plan_pending: dict[int, Any] = {}  # per tab: the unsaved values
        self.edit_dialog: tuple[Any, ...] | None = None  # what the open dialog answers
        self.list_row: dict[tuple[int, int], int] = {}
        self.saved: list[str] = []  # what OK or an input saved, in order
        self.recalculating = False
        self.offer_settings = False  # open 3206/3208 after a create's name

    # -- routing ----------------------------------------------------------------------
    def _energy_event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part = ev["screen"], ev["part"]
        if (screen, part) in self.disabled | self.locked:
            self._violation(ev, "clicked a part shown ENABLED 0 (hangs the iTM)")
        if self.edit_dialog is not None and part == self.edit_dialog[0]:
            return self._answer(screen, ev)
        handler = {
            3202: self._planned_event,
            3203: self._actual_event,
            3205: self._list_event,
            3207: self._list_event,
            3206: self._settings_event,
            3208: self._settings_event,
        }.get(screen)
        updates = handler(ev) if handler is not None else None
        return super()._energy_event(ev) if updates is None else updates

    def _open(self, target: int) -> list[Update]:
        if target in {3202, 3203}:
            self._start_pending(target)
            self.locked |= {
                (target, b) for (sc, _), b in MONTH_BUTTONS.items() if sc == target
            }
        return super()._open(target)

    def _close(self, screen: int) -> list[Update]:
        if screen in {3202, 3203}:
            self.plan_pending = {}
            self.locked -= {
                (screen, b) for (sc, _), b in MONTH_BUTTONS.items() if sc == screen
            }
        return super()._close(screen)

    # -- dialogs ----------------------------------------------------------------------
    def _number_dialog(self, screen: int, *what: Any) -> list[Update]:
        self.edit_dialog = (NUMBER_ID, "number", *what)
        data = [NUMBER_ID, 6, 1, "Value", 0, 0, 0, 0, 99999, 0, 2, 1, *["x"] * 11]
        return [("DLG_OPEN", screen, data, [])]

    def _confirm_dialog(self, screen: int, *what: Any) -> list[Update]:
        self.edit_dialog = (CONFIRM_ID, "confirm", *what)
        data = [CONFIRM_ID, 2, "Execute setting?", "Confirm", "Yes", "No"]
        return [("DLG_OPEN", screen, data, [])]

    def _name_dialog(self, screen: int, *what: Any) -> list[Update]:
        self.edit_dialog = (NAME_ID, "name", *what)
        data = [NAME_ID, 4, "Name", "", 1, 16, 0, 0, 1, "Words", "OK", "Cancel"]
        return [("DLG_OPEN", screen, [*data, *[""] * 11, ""], [])]

    def _answer(self, screen: int, ev: dict[str, Any]) -> list[Update]:
        assert self.edit_dialog is not None
        dialog_id, kind, *what = self.edit_dialog
        self.edit_dialog = None
        types = {"number": 6, "confirm": 2, "name": 4}
        close: list[Update] = [("DLG_CLOSE", screen, [dialog_id, types[kind]], [])]
        if ev["event"] != 1:
            return close
        if kind == "number":
            return close + self._entered(screen, what, _number(ev["array"]))
        if kind == "name":
            return close + self._named(screen, what, ev["string"])
        action: Callable[[], list[Update]] = what[0]  # a confirm: its action
        return close + action()

    # -- planned and actual consumption -----------------------------------------------
    def _start_pending(self, screen: int) -> None:
        kind = self.type_shown.get(screen, 0)
        values = self.plan if screen == 3202 else self.actual
        self.plan_pending = {tab: copy.deepcopy(values(kind, tab)) for tab in (0, 1)}

    def _plan_update(self, screen: int) -> list[Update]:
        parts = self._planned_parts() if screen == 3202 else self._actual_parts()
        return [("UPDATE", screen, [], [*parts, *self._lists(screen)])]

    def _pick_type(self, screen: int, index: int) -> list[Update]:
        self.type_shown[screen] = index
        self._start_pending(screen)
        return self._plan_update(screen)

    def _select_month(self, screen: int, part: int, row: int) -> list[Update]:
        self.list_row[screen, part] = row
        button = MONTH_BUTTONS[screen, part]
        self.locked.discard((screen, button))
        return [("UPDATE", screen, [], [(button, "ENABLED", 1)])]

    def _entered(self, screen: int, what: list[Any], text: str) -> list[Update]:
        tab, key = what[0], what[1]
        if key == "month":
            self.plan_pending[tab]["months"][what[2]][1] = text
            return [("UPDATE", screen, [], [])]
        self.plan_pending[tab][key] = text
        updates: list[Update] = [("UPDATE", screen, [], [(what[2], "TEXT", text)])]
        if screen == 3202 and key == "ratio":
            self.recalculating = True
            data = [RECALC_ID, 11, "Now Loading.", 1, 1, "Progress", "Cancel"]
            updates.append(("DLG_OPEN", 3202, data, []))
        return updates

    def _save_plan(self, screen: int) -> list[Update]:
        values = self.plans if screen == 3202 else self.actuals
        for tab, value in self.plan_pending.items():
            values[self.type_shown.get(screen, 0), tab] = value
        self.saved.append("planned" if screen == 3202 else "actual")
        return self._close(screen)

    def _planned_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update] | None:
        part, event = ev["part"], ev["event"]
        if part == RECALC_ID and event == 5:
            self.recalculating = False
            return [("DLG_CLOSE", 3202, [RECALC_ID, 11], [])]
        if event != 1:
            return None
        if part == 2:
            return self._pick_type(3202, ev["number"])
        if part == 14:
            if self.recalculating:
                self._violation(ev, "OK while the iTM still recalculates")
            return self._confirm_dialog(3202, lambda: self._save_plan(3202))
        for tab, (year, month, text, ratio, months, month_button) in enumerate(
            PLANNED_TABS
        ):
            if part in {year, month}:
                self.plan_pending[tab]["per_month"] = part == month
                checks = [(month, "CHECK_STATE", int(part == month))]
                return [
                    (
                        "UPDATE",
                        3202,
                        [],
                        [*checks, (year, "CHECK_STATE", int(part == year))],
                    )
                ]
            if part == ratio:
                return self._number_dialog(3202, tab, "ratio", text)
            if part == months and ev["array"]:
                return self._select_month(3202, months, ev["array"][0])
            if part == month_button:
                return self._number_dialog(
                    3202, tab, "month", self.list_row[3202, months]
                )
        return None

    def _actual_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update] | None:
        part, event = ev["part"], ev["event"]
        if event != 1:
            return None
        if part == 2:
            return self._pick_type(3203, ev["number"])
        if part == 15:
            return self._confirm_dialog(3203, lambda: self._save_plan(3203))
        for tab, (months, month_button, text, co2) in enumerate(ACTUAL_TABS):
            if part == months and ev["array"]:
                return self._select_month(3203, months, ev["array"][0])
            if part == month_button:
                return self._number_dialog(
                    3203, tab, "month", self.list_row[3203, months]
                )
            if part == co2:
                return self._number_dialog(3203, tab, "co2", text)
        return None

    # -- energy types and groups ------------------------------------------------------
    def _entries(self, screen: int) -> list[list[str]]:
        return self.energy_types if screen == 3205 else self.energy_groups

    def _list_update(self, screen: int) -> list[Update]:
        return [("UPDATE", screen, [], [(2, "LIST_ROW", _rows(self._entries(screen)))])]

    def _list_event(self, ev: dict[str, Any]) -> list[Update] | None:
        screen, part = ev["screen"], ev["part"]
        if ev["event"] != 1:
            return None
        if part == 4:
            return self._name_dialog(screen, "create")
        if part == 7:
            return self._name_dialog(screen, "rename", self.row_selected[screen])
        if part == 6:
            row = self.row_selected[screen]

            def delete() -> list[Update]:
                del self._entries(screen)[row]
                self.saved.append(f"delete {screen}")
                return self._list_update(screen)

            return self._confirm_dialog(screen, delete)
        return None

    def _named(self, screen: int, what: list[Any], name: str) -> list[Update]:
        entries = self._entries(screen)
        if what[0] == "rename":
            entries[what[1]][0] = name
            self.saved.append(f"rename {screen}")
            return self._list_update(screen)
        entries.append([name, "kWh"] if screen == 3205 else [name])
        self.saved.append(f"create {screen}")
        if self.offer_settings:
            self.row_selected[screen] = len(entries) - 1
            return [*self._list_update(screen), *self._open(screen + 1)]
        return self._list_update(screen)

    def _settings_event(self, ev: dict[str, Any]) -> list[Update] | None:
        ok = {3206: 13, 3208: 11}[ev["screen"]]
        if ev["part"] == ok and ev["event"] == 1:
            self.saved.append(f"settings {ev['screen']}")
            return self._close(ev["screen"])
        return None
