"""A simulated iTM that also edits its zone structure and icons.

Zone settings (1600) with add/delete/members/settings/move, the member screen
(1601) with its Jump to the member order screen (1602: it lists the other members
and the selected one lands after the picked row's member), the icon
list (1604, for zone and point icons) and the move screen (1605),
plus the point icon on the point settings screen (1701). Strict like the real iTM:
clicking a part it reports disabled raises.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fake_itm import FakeITM, FakeUnit, Update, _enabled

ZONE_ICONS = list(range(1000, 1010))
POINT_ICONS = [2100, 2102, 2114, 2201, 2601, 2704]
CONFIRM = 30


@dataclass
class Zone:
    """A zone of the zone tree: sub-zones and management points (by name)."""

    name: str
    children: list[Zone] = field(default_factory=list)
    points: list[str] = field(default_factory=list)
    icon: int = 1000


def _text_rows(
    cells: list[list[str]], icons: list[int] | None = None
) -> dict[str, Any]:
    return {
        "rows": [
            {
                "text": c,
                "a": [icons[i] if icons else -1, *[-1] * (len(c) - 1)],
                "b": [-1] * len(c),
            }
            for i, c in enumerate(cells)
        ],
        "selected": [],
    }


class FakeZonesITM(FakeITM):  # pylint: disable=too-many-instance-attributes
    """A FakeITM whose zone settings screens edit a zone tree."""

    def __init__(self) -> None:
        """Start with a root holding "Building" (every unit) and an empty "Zone A"."""
        super().__init__()
        units = [u.name for u in self.units]
        self.tree = Zone("Top", [Zone("Building", points=units), Zone("Zone A")])
        self.point_icons = dict.fromkeys(units, 2114)
        self.z_selected: int | None = None
        self.z_dialog: str | None = None
        self.z_pending: dict[str, Any] = {}
        # Set: the next confirmed member change opens the zone error screen (1606).
        self.zone_error: str | None = None
        self.jump_enabled = True

    # -- the tree -------------------------------------------------------------
    def _z_flat(self) -> list[tuple[Zone, int, Zone | None]]:
        """Return (zone, level, parent) in list order: depth first from the root."""
        out: list[tuple[Zone, int, Zone | None]] = []

        def walk(z: Zone, level: int, parent: Zone | None) -> None:
            out.append((z, level, parent))
            for child in z.children:
                walk(child, level + 1, z)

        walk(self.tree, 0, None)
        return out

    def zone(self, name: str) -> Zone:
        """Return a zone by name (test helper)."""
        return next(z for z, _, _ in self._z_flat() if z.name == name)

    def _z_rows(self, exclude: Zone | None = None) -> list[list[str]]:
        rows = []
        for z, level, _ in self._z_flat():
            if exclude is not None and (z is exclude or self._z_below(z, exclude)):
                continue
            rows.append(
                [
                    z.name,
                    "-" if level == 0 else str(level),
                    str(len(z.children)),
                    str(len(z.points)),
                ]
            )
        return rows

    def _z_below(self, z: Zone, ancestor: Zone) -> bool:
        return any(z is d for d, _, _ in self._z_flat_from(ancestor))

    @staticmethod
    def _z_flat_from(z: Zone) -> list[tuple[Zone, int, Zone | None]]:
        out: list[tuple[Zone, int, Zone | None]] = []

        def walk(node: Zone) -> None:
            for child in node.children:
                out.append((child, 0, node))
                walk(child)

        walk(z)
        return out

    def _z_selected_zone(self) -> tuple[Zone, Zone | None]:
        if self.z_selected is None:
            msg = "a zone button clicked without a selection: the real iTM hangs"
            raise RuntimeError(msg)
        zone, _, parent = self._z_flat()[self.z_selected]
        return zone, parent

    def _z_list_update(self) -> list[tuple[int, str, Any]]:
        return [
            (4, "LIST_ROW", _text_rows(self._z_rows())),
            *_enabled((7, 9, 10), 0),
        ]

    # -- screens --------------------------------------------------------------
    def _system_parts(self, target: int) -> list[tuple[int, str, Any]]:
        if target == 1600:
            self.z_selected = None
            return [(2, "TEXT", self.tree.name), *self._z_list_update()]
        if target == 1700:
            names = list(self.point_icons)
            cells = [["Indoor", n, "", self.point_info.get(n, "")] for n in names]
            rows = _text_rows(cells)
            for row, name in zip(rows["rows"], names, strict=True):
                row["a"] = [-1, -1, self.point_icons[name], -1]
            return [(1, "LIST_ROW", rows), (2, "ENABLED", 0)]
        return super()._system_parts(target)

    def _event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part = ev["screen"], ev["part"]
        mine = screen in {1601, 1602, 1603, 1604, 1605, 1606} or (
            screen == 1600 and part != 14
        )
        mine = mine or (screen == 1700 and part == 2) or (screen == 1701 and part == 11)
        if screen in self.open_screens and mine:
            return self._zones_event(ev)
        if (
            screen == 1701
            and part == CONFIRM
            and ev["event"] == 1
            and "icon" in self.z_pending
        ):
            self.point_icons[self.pending_system["point"]] = self.z_pending.pop("icon")
        return super()._event(ev)

    def _zones_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update]:
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        if ev["dtype"] == 8:  # a list selection
            return self._z_select_row(screen, part, ev["array"][0])
        if screen == 1606 and part == 2:
            self.open_screens.discard(1606)
            return [("CLOSE", 1606, [], [])]
        if screen == 1600:
            return self._z_list_event(part, event, ev)
        if screen == 1601:
            return self._z_members_event(part, event)
        if screen == 1602:
            return self._z_order_event(part)
        if screen in {1603, 1701} and part in {17, 11}:
            return self._z_open_icons(screen)
        if screen == 1603:
            return self._z_settings_event(part, event)
        if screen == 1604:
            return self._z_icons_event(part)
        if screen == 1605:
            return self._z_move_event(part, event)
        if screen == 1700:  # Change: open 1701 with the point's icon
            names = list(self.point_icons)
            if 1700 not in self.system_selected:
                msg = "1700/2 clicked while disabled: the real iTM hangs"
                raise RuntimeError(msg)
            name = names[self.system_selected[1700]]
            self.pending_system["point"] = name
            self.open_screens.add(1701)
            parts = [
                (2, "TEXT", "Indoor"),
                (4, "TEXT", name),
                (7, "TEXT", self.point_info.get(name, "")),
                (10, "ICONID", self.point_icons[name]),
            ]
            return [("OPEN", 1701, [-1], parts)]
        return [("UPDATE", screen, [], [])]

    def _z_select_row(self, screen: int, part: int, row: int) -> list[Update]:
        if screen == 1600:
            self.z_selected = row
            enabled = 0 if row == 0 else 1  # the root can't be deleted/edited/moved
            parts = _enabled((7, 9, 10), enabled)
            return [("UPDATE", 1600, [], parts)]
        if screen == 1700:
            self.system_selected[1700] = row
            return [("UPDATE", 1700, [], [(2, "ENABLED", 1)])]
        self.z_pending[f"sel{screen}_{part}"] = row
        if (screen, part) == (1601, 2):
            return [("UPDATE", 1601, [], self._z_order_enabled())]
        return [("UPDATE", screen, [], [])]

    def _z_list_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, part: int, event: int, ev: dict[str, Any]
    ) -> list[Update]:
        if part == 6:
            self.z_dialog = "add"
            data = [15, 4, "Name", "", 1, 12, 0, 0, 1, "Words", "OK", "Cancel"]
            return [("DLG_OPEN", 1600, [*data, *[""] * 11, ""], [])]
        if part == 15 and self.z_dialog == "add":
            self.z_dialog = None
            if event == 1:
                under = self.tree
                if self.z_selected is not None:
                    under = self._z_flat()[self.z_selected][0]
                under.children.append(Zone(ev["string"]))
            return [
                ("DLG_CLOSE", 1600, [15, 4], []),
                ("UPDATE", 1600, [], self._z_list_update()),
            ]
        if part in {7, 8, 9, 10} and (
            self.z_selected is None or (part != 8 and self.z_selected == 0)
        ):
            msg = f"1600/{part} clicked while disabled: the real iTM hangs"
            raise RuntimeError(msg)
        if part == 7:
            self.z_dialog = "delete"
            confirm = [16, 2, "Delete?", "Confirm", "Yes", "No"]
            return [("DLG_OPEN", 1600, confirm, [])]
        if part == 16 and self.z_dialog == "delete":
            self.z_dialog = None
            zone, parent = self._z_selected_zone()
            close: Update = ("DLG_CLOSE", 1600, [16, 2], [])
            if event != 1:
                return [close]
            if zone.children or zone.points:
                return [
                    close,
                    ("DLG_OPEN", 1600, [17, 1, "Zone not empty", "Error", "OK"], []),
                ]
            assert parent is not None
            parent.children.remove(zone)
            self.z_selected = None
            return [close, ("UPDATE", 1600, [], self._z_list_update())]
        zone, _ = self._z_selected_zone()
        if part == 8:
            self.z_pending = {"zone": zone, "members": list(zone.points)}
            self.open_screens.add(1601)
            return [("OPEN", 1601, [-1], self._z_member_parts())]
        if part == 9:
            self.z_pending = {"zone": zone, "for": "zone"}
            self.open_screens.add(1603)
            parts = [(2, "TEXT", zone.name), (16, "ICONID", zone.icon)]
            return [("OPEN", 1603, [-1], parts)]
        if part == 10:
            self.z_pending = {"zone": zone, "targets": self._z_rows(exclude=zone)}
            self.open_screens.add(1605)
            rows = _text_rows(self.z_pending["targets"])
            return [
                (
                    "OPEN",
                    1605,
                    [-1],
                    [(2, "TEXT", self.tree.name), (4, "LIST_ROW", rows)],
                )
            ]
        return [("UPDATE", 1600, [], [])]

    def _z_member_parts(self) -> list[tuple[int, str, Any]]:
        members = self.z_pending["members"]
        available = [n for n in self.point_icons if n not in members]
        self.z_pending["available"] = available
        return [
            (2, "LIST_ROW", _text_rows([["Indoor", n] for n in members])),
            (11, "LIST_ROW", _text_rows([["Indoor", n] for n in available])),
            *self._z_order_enabled(),
        ]

    def _z_order_enabled(self) -> list[tuple[int, str, Any]]:
        """Up and down as the real iTM: off at the ends and without a selection."""
        p = self.z_pending
        row, last = p.get("sel1601_2"), len(p["members"]) - 1
        up = row is not None and 0 < row <= last
        down = row is not None and 0 <= row < last
        jump = row is not None and self.jump_enabled
        return [
            (3, "ENABLED", int(up)),
            (4, "ENABLED", int(down)),
            (5, "ENABLED", int(jump)),
        ]

    def _z_members_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, part: int, event: int
    ) -> list[Update]:
        p = self.z_pending
        if part in {3, 4, 5}:
            if {q: v for q, _, v in self._z_order_enabled()}.get(part) != 1:
                msg = f"1601/{part} clicked while disabled: the real iTM hangs"
                raise RuntimeError(msg)
            row = p["sel1601_2"]
            if part == 5:
                p.pop("sel1602_1", None)
                self.open_screens.add(1602)
                others = [n for i, n in enumerate(p["members"]) if i != row]
                rows = _text_rows([["Indoor", n] for n in others])
                return [("OPEN", 1602, [-1], [(1, "LIST_ROW", rows)])]
            other = row - 1 if part == 3 else row + 1
            members = p["members"]
            members[row], members[other] = members[other], members[row]
            p["sel1601_2"] = other
            return [("UPDATE", 1601, [], self._z_member_parts())]
        if part == 7:
            p["members"].append(p["available"][p.pop("sel1601_11")])
            return [("UPDATE", 1601, [], self._z_member_parts())]
        if part == 8:
            p["members"].pop(p.pop("sel1601_2"))
            return [("UPDATE", 1601, [], self._z_member_parts())]
        if part == 12:
            self.z_dialog = "members"
            return [
                (
                    "DLG_OPEN",
                    1601,
                    [CONFIRM, 2, "Execute setting?", "Confirm", "Yes", "No"],
                    [],
                )
            ]
        if part == CONFIRM and self.z_dialog == "members":
            self.z_dialog = None
            if event == 1 and self.zone_error is not None:
                self.open_screens.add(1606)
                text = self.zone_error
                self.zone_error = None
                return [
                    ("DLG_CLOSE", 1601, [CONFIRM, 2], []),
                    ("OPEN", 1606, [-1], [(1, "TEXT", text)]),
                ]
            if event == 1:
                p["zone"].points = list(p["members"])
                self.open_screens.discard(1601)
                return [("DLG_CLOSE", 1601, [CONFIRM, 2], []), ("CLOSE", 1601, [], [])]
            return [("DLG_CLOSE", 1601, [CONFIRM, 2], [])]
        if part == 13:
            self.open_screens.discard(1601)
            return [("CLOSE", 1601, [], [])]
        return [("UPDATE", 1601, [], [])]

    def _z_order_event(self, part: int) -> list[Update]:
        """1602: OK puts the selected member after the picked row's member."""
        p = self.z_pending
        self.open_screens.discard(1602)
        if part == 2:
            members, target = p["members"], p.pop("sel1602_1")
            members.insert(target + 1, members.pop(p["sel1601_2"]))
            p["sel1601_2"] = target + 1
            return [
                ("CLOSE", 1602, [], []),
                ("UPDATE", 1601, [], self._z_member_parts()),
            ]
        return [("CLOSE", 1602, [], [])]

    def _z_open_icons(self, screen: int) -> list[Update]:
        if screen == 1603:
            icons, current = ZONE_ICONS, self.z_pending["zone"].icon
        else:
            icons = POINT_ICONS
            current = self.point_icons[self.pending_system["point"]]
        self.z_pending["icon_for"] = screen
        self.z_pending["icons"] = icons
        rows = _text_rows([["", f"Icon {i}", str(i % 1000)] for i in icons], icons)
        rows["selected"] = [icons.index(current)] if current in icons else []
        self.open_screens.add(1604)
        return [("OPEN", 1604, [-1], [(1, "LIST_ROW", rows)])]

    def _z_icons_event(self, part: int) -> list[Update]:
        p = self.z_pending
        self.open_screens.discard(1604)
        if part == 2:
            icon = p["icons"][p.pop("sel1604_1")]
            p["icon"] = icon
            parent = p["icon_for"]
            icon_part = 16 if parent == 1603 else 10
            return [
                ("CLOSE", 1604, [], []),
                ("UPDATE", parent, [], [(icon_part, "ICONID", icon)]),
            ]
        return [("CLOSE", 1604, [], [])]

    def _z_settings_event(self, part: int, event: int) -> list[Update]:
        if part == 18:
            self.z_dialog = "zone settings"
            return [
                (
                    "DLG_OPEN",
                    1603,
                    [CONFIRM, 2, "Execute setting?", "Confirm", "Yes", "No"],
                    [],
                )
            ]
        if part == CONFIRM and self.z_dialog == "zone settings":
            self.z_dialog = None
            if event == 1 and self.zone_error is not None:
                self.open_screens.add(1606)
                text, self.zone_error = self.zone_error, None
                return [
                    ("DLG_CLOSE", 1603, [CONFIRM, 2], []),
                    ("OPEN", 1606, [-1], [(1, "TEXT", text)]),
                ]
            if event == 1:
                if "icon" in self.z_pending:
                    self.z_pending["zone"].icon = self.z_pending.pop("icon")
                self.open_screens.discard(1603)
                return [("DLG_CLOSE", 1603, [CONFIRM, 2], []), ("CLOSE", 1603, [], [])]
            return [("DLG_CLOSE", 1603, [CONFIRM, 2], [])]
        if part == 19:
            self.open_screens.discard(1603)
            return [("CLOSE", 1603, [], [])]
        return [("UPDATE", 1603, [], [])]

    def _z_move_event(self, part: int, event: int) -> list[Update]:
        p = self.z_pending
        if part == 5:
            self.z_dialog = "move"
            return [
                (
                    "DLG_OPEN",
                    1605,
                    [CONFIRM, 2, "Execute setting?", "Confirm", "Yes", "No"],
                    [],
                )
            ]
        if part == CONFIRM and self.z_dialog == "move":
            self.z_dialog = None
            if event != 1:
                return [("DLG_CLOSE", 1605, [CONFIRM, 2], [])]
            target_name = p["targets"][p.pop("sel1605_4")][0]
            zone = p["zone"]
            for z, _, parent in self._z_flat():
                if z is zone and parent is not None:
                    parent.children.remove(zone)
            self.zone(target_name).children.append(zone)
            self.open_screens.discard(1605)
            return [
                ("DLG_CLOSE", 1605, [CONFIRM, 2], []),
                ("CLOSE", 1605, [], []),
                ("UPDATE", 1600, [], self._z_list_update()),
            ]
        if part == 6:
            self.open_screens.discard(1605)
            return [("CLOSE", 1605, [], [])]
        return [("UPDATE", 1605, [], [])]


__all__ = ["FakeUnit", "FakeZonesITM", "Zone"]
