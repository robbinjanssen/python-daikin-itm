"""The graphical layout view (screen 3, ScMainLayout): read only.

The main menu bar's "Grafisch scherm" (screen 0 part 2) shows the layout view in
place of the icon or list view, and its "Standaard scherm" (part 1) shows the
standard view again; that is how the browser switches between them (the two are
toggle buttons on the main menu bar). The layout view's parts: 0 the title, 1 the
layout panel (PNTLAYOUT and PNTLAYOUT_STATE), 2 the list of linked layout screens,
3 Back (to the parent layout screen), 4 the legend, 5-9 the operation window
labels and buttons.

A layout panel item is an icon (a management point, by its icon ID, mode and size)
or a text (its sub information: a text with its own width, alignment and colours).
PNTLAYOUT_STATE updates an icon's mode or a text's sub information by the item's
index. The panel carries no names: with ``names=True`` each icon is selected (a
CLICK with the item's index, like the browser) and the operation panel (screen 4)
read for its name, like the icon view does.

Seen on a real iTM (read only): the main menu bar's part 2 is hidden (VISIBLED 0)
but opens the layout view; without a configured layout it shows an empty screen
switch list, Back disabled (ENABLED 0), the labels "Operation Window" and "Unlock",
and no layout panel. Assumed (not seen on a real iTM): a configured layout sends
its panel as PNTLAYOUT on opening, and selecting an icon fills the operation panel.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypeVar

from .exceptions import ItmCommandError
from .models import P_NAME, PointKind, point_kind
from .protocol import DataType, EventType, ScreenID

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .client import _Session
    from .state import UIState

_T = TypeVar("_T")

MAINMENU, LAYOUT_VIEW = ScreenID.MAINMENU, ScreenID.STDLAYOUT
# Main menu bar (screen 0): the standard view and layout view toggle buttons.
MM_STANDARD_VIEW, MM_LAYOUT_VIEW = 1, 2
# Layout view (screen 3).
LY_TITLE, LY_PANEL, LY_SCREENS, LY_BACK = 0, 1, 2, 3
ITEM_ICON = 0  # a PNTLAYOUT item of kind 0 is an icon, any other kind a text


@dataclass(frozen=True)
class LayoutItem:  # pylint: disable=too-many-instance-attributes
    """An item of the layout panel, as PNTLAYOUT and PNTLAYOUT_STATE give it."""

    index: int  # the item's position in the panel (what selecting it sends)
    x: int
    y: int
    icon: bool  # an icon (a management point), else a text
    icon_id: int | None = None
    icon_mode: int | None = None  # the icon's state, as the icon view's
    icon_size: int | None = None
    text: str | None = None  # a text item's sub information
    kind: PointKind | None = None  # what the icon stands for (icons only)
    name: str | None = None  # the operation panel's name (only with names=True)


@dataclass(frozen=True)
class Layout:
    """The layout view: its title, background image, items and linked screens."""

    title: str | None = None
    image: str | None = None  # the background image's name, as sent
    items: tuple[LayoutItem, ...] = ()
    screens: tuple[str, ...] = ()  # the screen switch list's entries
    back_enabled: bool | None = None  # Back: only within a linked screen

    @property
    def points(self) -> tuple[LayoutItem, ...]:
        """Return the icon items: the management points on the layout."""
        return tuple(i for i in self.items if i.icon)

    @property
    def empty(self) -> bool:
        """Tell whether the layout shows nothing (no layout configured)."""
        return not self.items


def _clean(value: Any) -> str:
    return html.unescape(str(value)).strip()


def _item(index: int, raw: list[Any]) -> LayoutItem:
    """Turn one decoded PNTLAYOUT item into a LayoutItem."""
    x, y, kind = raw[0], raw[1], raw[2]
    if kind == ITEM_ICON:
        icon_id, mode, size = raw[3], raw[4], raw[5]
        return LayoutItem(
            index,
            x,
            y,
            icon=True,
            icon_id=icon_id,
            icon_mode=mode,
            icon_size=size,
            kind=point_kind(icon_id),
        )
    return LayoutItem(index, x, y, icon=False, text=_clean(raw[-1]))


def layout_from_screen(state: UIState) -> Layout:
    """Read the layout view (screen 3) from the merged state.

    Args:
    ----
        state: The session's merged screens.

    Returns:
    -------
        The layout; without a layout panel an empty one.

    """
    s = LAYOUT_VIEW
    panel = state.get(s, LY_PANEL, "PNTLAYOUT") or {}
    items = [_item(i, list(raw)) for i, raw in enumerate(panel.get("items", []))]
    for index, kind, value in state.get(s, LY_PANEL, "PNTLAYOUT_STATE") or []:
        if not 0 <= index < len(items):
            continue
        old = items[index]
        if kind == ITEM_ICON and old.icon:
            items[index] = replace(old, icon_mode=value)
        elif kind != ITEM_ICON and not old.icon:
            items[index] = replace(old, text=_clean(value))
    img = panel.get("img") or []
    image = _clean(img[0]) if img and img[0] else None
    title = state.get(s, LY_TITLE, "LABEL")
    rows = (state.get(s, LY_SCREENS, "LIST_ROW") or {}).get("rows", [])
    screens = tuple(
        cells[0] for row in rows if (cells := [_clean(t) for t in row["text"]])
    )
    back = state.get(s, LY_BACK, "ENABLED")
    return Layout(
        title=(_clean(title) or None) if isinstance(title, str) else None,
        image=image,
        items=tuple(items),
        screens=tuple(name for name in screens if name),
        back_enabled=None if back is None else back != 0,
    )


async def _click(s: _Session, screen: int, part: int, what: str) -> None:
    """Click a part, never one the iTM reports disabled (that hangs it)."""
    await s.click(screen, part, what)


async def leave_layout(s: _Session) -> None:
    """Show the standard view again with the main menu bar's toggle (part 1)."""
    if LAYOUT_VIEW not in s.state.screens:
        return
    await _click(s, MAINMENU, MM_STANDARD_VIEW, "show the standard view")
    # The layout view stays in the merged state; a later read in this session
    # must not mistake it for a fresh one.
    s.state.screens.pop(LAYOUT_VIEW, None)


async def read_layout(s: _Session, *, names: bool = False) -> Layout:
    """Show the layout view, read it and show the standard view again.

    Args:
    ----
        s: The logged-in session.
        names: Select each icon and read its name from the operation panel.

    Returns:
    -------
        The layout (empty when none is configured).

    Raises:
    ------
        ItmCommandError: The layout view did not open, or an icon could not be
            read.

    """
    await _click(s, MAINMENU, MM_LAYOUT_VIEW, "show the layout view")
    try:
        if LAYOUT_VIEW not in s.state.screens:
            msg = "the layout view did not open"
            raise ItmCommandError(msg)
        layout = layout_from_screen(s.state)
        if names and layout.points:
            named = {}
            for item in layout.points:
                await s.send(
                    LAYOUT_VIEW, LY_PANEL, EventType.CLICK, DataType.INT, item.index
                )
                name = s.state.get(ScreenID.OPERATION, P_NAME, "LABEL")
                named[item.index] = _clean(name) if isinstance(name, str) else None
            layout = replace(
                layout,
                items=tuple(
                    replace(i, name=named[i.index]) if i.icon else i
                    for i in layout.items
                ),
            )
        return layout
    finally:
        await leave_layout(s)


class LayoutMixin:  # pylint: disable=too-few-public-methods
    """ItmClient's layout view reader."""

    if TYPE_CHECKING:  # what ItmClient provides
        # pylint: disable=unused-argument
        async def _in_session(
            self, call: Callable[[_Session], Awaitable[_T]]
        ) -> _T: ...

    async def fetch_layout(self, *, names: bool = False) -> Layout:
        """Read the graphical layout view (screen 3) in one session. Read only.

        The layout view is shown with the main menu bar's "Grafisch scherm" and the
        standard view shown again afterwards, so the session ends where it started.

        Args:
        ----
            names: Also select each icon (one request each) and read its name
                from the operation panel; the layout panel itself has no names.

        Returns:
        -------
            The layout: its items (icons with their icon ID and mode, texts) and
            positions; empty when no layout is configured.

        """
        return await self._in_session(lambda s: read_layout(s, names=names))
