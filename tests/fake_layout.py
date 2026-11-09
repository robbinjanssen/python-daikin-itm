"""A simulated iTM with the graphical layout view (screen 3).

The main menu bar's "Grafisch scherm" (part 2, hidden like on the real iTM) shows
the layout view, "Standaard scherm" (part 1) the standard view again. Without a
layout the view shows what a real iTM without one showed: an empty screen switch
list, Terug disabled, the operation window labels and no layout panel. Strict: any
other click on the layout view, Terug while disabled, or a main menu bar toggle the
fake reports disabled raises. Every name and value is a generic placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from .fake_itm import _ENCODERS, FakeITM, Update, _rows

if TYPE_CHECKING:
    from daikin_itm.protocol import Writer

LAYOUT = 3


@dataclass
class FakeLayoutItem:  # pylint: disable=too-many-instance-attributes
    """An item of the layout panel: an icon (with the unit it stands for) or a text."""

    x: int
    y: int
    icon_id: int = 0  # 0: a text item
    mode: int = 0
    size: int = 1
    text: str = ""
    unit: str = ""  # the name the operation panel shows when the icon is selected


def _encode_pntlayout(w: Writer, v: dict[str, Any]) -> None:
    image, extra, scope_x, scope_y = v["img"]
    w.string(image)
    w.string(extra)
    w.short(scope_x)
    w.short(scope_y)
    w.int(len(v["items"]))
    for item in v["items"]:
        w.short(item.x)
        w.short(item.y)
        if item.icon_id:
            w.int(0)
            w.short(item.icon_id)
            w.short(item.mode)
            w.int(item.size)
        else:
            w.int(1)
            for n in (120, 1):  # width, alignment
                w.short(n)
            for n in (0, 0xFFFFFF, 0, 12):  # background, colours, font size
                w.int(n)
            w.string(item.text)
    w.int(v["selected"])


def _encode_pntlayout_state(w: Writer, v: list[tuple[int, int, Any]]) -> None:
    w.int(len(v))
    for index, kind, value in v:
        w.short(index)
        w.short(kind)
        if kind == 0:
            w.int(value)
        else:
            w.string(value)


_ENCODERS.setdefault("PNTLAYOUT", _encode_pntlayout)
_ENCODERS.setdefault("PNTLAYOUT_STATE", _encode_pntlayout_state)


class FakeLayoutITM(FakeITM):
    """A FakeITM with the layout view; empty unless items are given."""

    def __init__(
        self,
        items: list[FakeLayoutItem] | None = None,
        *,
        title: str = "",
        screens: tuple[str, ...] = (),
    ) -> None:
        """Start with the layout's items (none: no layout configured)."""
        super().__init__()
        self.layout_items = items or []
        self.layout_title = title
        self.layout_screens = screens
        self.layout_state: list[tuple[int, int, Any]] = []  # sent along on opening
        self.layout_opens = True  # False: part 2 doesn't react
        self.standard_enabled = 1  # the main menu bar's "Standaard scherm"
        self.layout_shown = False
        self.layout_selected: list[int] = []

    def _menu_bar(self) -> list[tuple[int, str, Any]]:
        return [
            *super()._menu_bar(),
            (1, "ENABLED", self.standard_enabled),
            (2, "VISIBLED", 0),
        ]

    def _event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        if screen == 0 and part == 2 and event == 1:
            return self._show_layout()
        if screen == 0 and part == 1 and event == 1:
            if not self.standard_enabled:
                msg = "0/1 clicked while disabled: the real iTM hangs"
                raise RuntimeError(msg)
            return self._show_standard()
        if screen == LAYOUT:
            return self._layout_event(ev)
        return super()._event(ev)

    def _show_layout(self) -> list[Update]:
        if not self.layout_opens:
            return [("UPDATE", 0, [], [])]
        self.layout_shown = True
        parts: list[tuple[int, str, Any]] = [
            (0, "LABEL", self.layout_title),
            (2, "LIST_COLUMN", [["", 122, 0, 0, 0]]),
            (2, "LIST_ROW", _rows([[name] for name in self.layout_screens])),
            (3, "LABEL", "Back"),
            (3, "ENABLED", 0),
            (5, "LABEL", "Operation Window"),
            (6, "LABEL", "Unlock"),
        ]
        if self.layout_items:
            panel = {
                "img": ["floor.png", "", 0, 0],
                "items": self.layout_items,
                "selected": -1,
            }
            parts.append((1, "PNTLAYOUT", panel))
        if self.layout_state:
            parts.append((1, "PNTLAYOUT_STATE", self.layout_state))
        return [
            ("OPEN", LAYOUT, [-1], parts),
            ("UPDATE", 0, [], []),
            ("HIDE", 1, [], []),
            ("HIDE", 4, [], []),
        ]

    def _show_standard(self) -> list[Update]:
        if not self.layout_shown:
            return [("UPDATE", 0, [], [])]
        self.layout_shown = False
        return [("HIDE", LAYOUT, [], []), ("SHOW", 1, [], []), ("SHOW", 4, [], [])]

    def _layout_event(self, ev: dict[str, Any]) -> list[Update]:
        part = ev["part"]
        if not self.layout_shown:
            msg = f"3/{part} clicked while the layout view is not shown"
            raise RuntimeError(msg)
        if part == 1 and ev["dtype"] == 1:
            index = ev["number"]
            self.layout_selected.append(index)
            item = self.layout_items[index]
            return [("UPDATE", 4, [], [(0, "LABEL", item.unit)])]
        msg = f"3/{part} clicked: the client never presses it"
        raise RuntimeError(msg)
