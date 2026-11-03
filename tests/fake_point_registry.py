"""A simulated iTM with the management point data registration (service 2800).

Behind the service login, the service tab's button 63 opens 2800 with one row per
management point. Selecting a row enables Edit (5), which opens the point's
attributes screen (2802 for an indoor unit, 2803 for an outdoor unit); its Cancel
asks a CONFIRM, answered Yes to close it. Cancel (13) on 2800 asks "discard
changes?", answered Yes to close it. Strict like the real iTM: a click on a part it
reports disabled raises (the real web server hangs then), and so does any button
the client must never press (add, delete, copy, CSV load/save, auto register,
check, OK) and an OK on an attributes screen. Every name and value is a generic
placeholder.
"""

from __future__ import annotations

from typing import Any

from .fake_itm import FakeITM, Update, _rows

REGISTRY = 2800
CONFIRM_ID = 30
FORBIDDEN = {3, 4, 6, 8, 9, 10, 11, 12}
# Attributes screens: (Cancel, OK) parts and the (label part, label, text) fields.
ATTRIBUTES = {
    2802: (22, 21),
    2803: (18, 17),
}


def _confirm(text: str) -> list[Any]:
    return [CONFIRM_ID, 2, text, "Confirm", "Yes", "No"]


class FakeRegistryITM(FakeITM):  # pylint: disable=too-many-instance-attributes
    """A FakeITM with the management point registration list and its edit screens."""

    def __init__(self) -> None:
        """Register the fake's units as indoor units and one outdoor unit."""
        super().__init__()
        self.closing = False  # the second closing question is open
        self.points = [
            ["Indoor unit", u.name, f"1:1-{i:02d}", ""]
            for i, u in enumerate(self.units)
        ]
        self.points.append(["Outdoor unit", "OU-1", "1-00", "Roof"])
        self.registry_opens = True
        self.edit_screen = 2802  # 0: Edit opens an unknown screen (2899)
        self.cancel_answer = "CONFIRM"  # or "ERROR", "STAY" (Cancel on 2800)
        self.rg_selected: int | None = None
        self.pending: int | None = None  # the screen whose CONFIRM is open
        self.opened: list[int] = []

    def _service_event(self, ev: dict[str, Any]) -> list[Update] | None:
        if ev["part"] == 63 and self.service_mode:
            if not self.registry_opens:
                return [("UPDATE", 5, [], [])]
            self.open_screens.add(REGISTRY)
            self.rg_selected = None
            self.opened.append(REGISTRY)
            return [("OPEN", REGISTRY, [-1], self._registry_parts())]
        return super()._service_event(ev)

    def _registry_parts(self) -> list[tuple[int, str, Any]]:
        columns = [[name, 100, 0, 0, 0] for name in ("Type", "Name", "Address", "Info")]
        rows = _rows([*self.points, ["", "", "", ""]])  # an empty row at the end
        return [
            (0, "LABEL", "Mgmt. Point Data"),
            (1, "LIST_COLUMN", columns),
            (1, "LIST_ROW", rows),
            (2, "LABEL", "Edit"),
            (3, "ENABLED", 1),
            (4, "ENABLED", 0),
            (5, "ENABLED", 0),
            (6, "ENABLED", 0),
            (7, "LABEL", "CSV"),
            (13, "LABEL", "Cancel"),
        ]

    def _menu(self, ev: dict[str, Any]) -> list[Update]:
        screen = ev["screen"]
        if screen == REGISTRY and screen in self.open_screens:
            return self._registry_event(ev)
        if screen in self.open_screens and (screen in ATTRIBUTES or screen == 2899):
            return self._attributes_event(ev)
        return super()._menu(ev)

    @staticmethod
    def _refuse(screen: int, part: int, why: str) -> None:
        msg = f"{screen}/{part} clicked: {why}"
        raise RuntimeError(msg)

    def _registry_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update]:
        part = ev["part"]
        if part in FORBIDDEN:
            self._refuse(REGISTRY, part, "never pressed by a read")
        if part == 1 and ev["dtype"] == 1:
            self.rg_selected = ev["number"]
            return [("UPDATE", REGISTRY, [], [(5, "ENABLED", 1)])]
        if part == 5:
            if self.rg_selected is None:
                self._refuse(REGISTRY, part, "disabled: the real iTM hangs")
            return self._open_attributes()
        if part == 13:
            if self.cancel_answer == "STAY":  # e.g. a PROGRESS dialog, no close
                return [
                    ("DLG_OPEN", REGISTRY, [42, 11, "Please wait", 0, 0, "", ""], [])
                ]
            if self.cancel_answer == "ERROR":
                return [("DLG_OPEN", REGISTRY, [41, 1, "Error", "Error", "OK"], [])]
            self.pending = REGISTRY
            return [("DLG_OPEN", REGISTRY, _confirm("Discard changes?"), [])]
        if part == CONFIRM_ID and self.pending == REGISTRY:
            # Like the real iTM: a second question follows, with its own id.
            self.pending = None
            self.closing = True
            second = [CONFIRM_ID + 1, 2, "Confirm exit?", "Confirm"]
            return [
                ("DLG_CLOSE", REGISTRY, [CONFIRM_ID, -1], []),
                ("DLG_OPEN", REGISTRY, [*second, "Yes", "No"], []),
            ]
        if part == CONFIRM_ID + 1 and self.closing:
            self.closing = False
            self.open_screens.discard(REGISTRY)
            return [
                ("DLG_CLOSE", REGISTRY, [CONFIRM_ID + 1, -1], []),
                ("CLOSE", REGISTRY, [], []),
            ]
        self._refuse(REGISTRY, part, "unexpected")
        return []

    def _open_attributes(self) -> list[Update]:
        assert self.rg_selected is not None
        kind, name, address, info = self.points[self.rg_selected]
        if not self.edit_screen:
            self.open_screens.add(2899)
            return [("OPEN", 2899, [-1], [])]
        screen = 2803 if kind == "Outdoor unit" else self.edit_screen
        self.open_screens.add(screen)
        self.opened.append(screen)
        parts: list[tuple[int, str, Any]]
        if screen == 2803:
            parts = [(2, "LABEL", "Port No."), (3, "TEXT", "1"), (5, "LABEL", "Type")]
            parts += [(6, "TEXT", kind), (9, "LABEL", "Name"), (10, "TEXT", name)]
            parts += [(12, "LABEL", "Details"), (13, "TEXT", info)]
        else:
            parts = [
                (2, "LABEL", "Port No."),
                (3, "TEXT", "1"),
                (5, "LABEL", "Address"),
            ]
            parts += [(6, "COMBO_STRS", [address]), (9, "LABEL", "Type")]
            parts += [(10, "TEXT", kind), (13, "LABEL", "Name"), (14, "TEXT", name)]
            parts += [(17, "TEXT", info)]  # a text without a label before it
        return [("OPEN", screen, [-1], [(0, "LABEL", "Attributes"), *parts])]

    def _attributes_event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part = ev["screen"], ev["part"]
        cancel, _ok = ATTRIBUTES.get(screen, (-1, -1))
        if part == cancel:
            self.pending = screen
            return [("DLG_OPEN", screen, _confirm("Discard changes?"), [])]
        if part == CONFIRM_ID and self.pending == screen:
            self.pending = None
            self.open_screens.discard(screen)
            return [("CLOSE", screen, [], [])]
        self._refuse(screen, part, "never pressed by a read")
        return []
