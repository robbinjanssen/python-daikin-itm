# ruff: noqa: SLF001
# pylint: disable=protected-access
"""Screen helpers shared by the feature modules."""

from __future__ import annotations

import html
from typing import TYPE_CHECKING, Any

from .exceptions import ItmCommandError
from .protocol import DataType, EventType

if TYPE_CHECKING:
    from .menu import MenuScreen
    from .protocol import Reply
    from .session import _Session
    from .state import UIState


# -- readers --------------------------------------------------------------------------


def clean(value: Any) -> str:
    """Return a value as shown: HTML entities decoded, whitespace stripped."""
    return html.unescape(str(value)).strip()


def part_text(
    state: UIState, screen: int, part: int, ptype: str = "TEXT"
) -> str | None:
    """Return a part's text, cleaned; None when it has none."""
    value = state.get(screen, part, ptype)
    return (clean(value) or None) if isinstance(value, str) else None


def part_text_or_blank(state: UIState, screen: int, part: int) -> str:
    """Return a part's text, cleaned; an empty string when it has none."""
    value = state.get(screen, part, "TEXT")
    return html.unescape(value).strip() if isinstance(value, str) else ""


def raw_rows(state: UIState, screen: int, part: int) -> list[dict[str, Any]]:
    """Return a list's rows as the iTM sent them."""
    return list((state.get(screen, part, "LIST_ROW") or {}).get("rows", []))


def row_cells(row: dict[str, Any]) -> list[str]:
    """Return a list row's cells, HTML entities decoded (not stripped)."""
    return [html.unescape(str(t)) for t in row.get("text", [])]


def cells(state: UIState, screen: int, part: int) -> list[tuple[str, ...]]:
    """Return a list's rows as tuples of cleaned cells."""
    rows = (state.get(screen, part, "LIST_ROW") or {}).get("rows", [])
    return [tuple(clean(t) for t in r.get("text", [])) for r in rows]


def cell_lists(state: UIState, screen: int, part: int) -> list[list[str]]:
    """Return a list's rows as lists of cleaned cells."""
    rows = (state.get(screen, part, "LIST_ROW") or {}).get("rows", [])
    return [[html.unescape(str(t)).strip() for t in r.get("text", [])] for r in rows]


def radio(state: UIState, screen: int, on: int, off: int) -> bool | None:
    """Return which of two radios is checked: True for on, False for off, or None."""
    if state.get(screen, on, "CHECK_STATE") == 1:
        return True
    if state.get(screen, off, "CHECK_STATE") == 1:
        return False
    return None


def ticked(state: UIState, screen: int, part: int) -> bool:
    """Tell whether a checkbox or radio is checked."""
    return bool(state.get(screen, part, "CHECK_STATE") == 1)


def checkbox(state: UIState, screen: int, part: int) -> bool | None:
    """Tell whether a checkbox is ticked, or None while its screen isn't open.

    The iTM sends no check state for an unticked checkbox.
    """
    if screen not in state.screens:
        return None
    return bool(state.get(screen, part, "CHECK_STATE") == 1)


def checked_index(state: UIState, screen: int, parts: tuple[int, ...]) -> int | None:
    """Return the index of the checked radio among parts, or None."""
    for i, part in enumerate(parts):
        if state.get(screen, part, "CHECK_STATE") == 1:
            return i
    return None


def graph(state: UIState, screen: int, part: int) -> tuple[Any, ...]:
    """Return a graph part's values (a bar/line or a point graph)."""
    for ptype in ("BARLINE_GRAPH", "POINT_GRAPH"):
        if (value := state.get(screen, part, ptype)) is not None:
            return tuple(value)
    return ()


def activation(state: UIState, screen: int, disable: int, text: str) -> bool:
    """Tell whether a row's activation text means on.

    The text is the enable radio's label when on and empty (or the disable radio's
    label) when off, in any display language.
    """
    off = clean(state.get(screen, disable, "LABEL") or "")
    return bool(text) and not (off and text == off)


def row_named(names: list[str], name: str, what: str) -> int:
    """Return the row showing name; raise when it isn't there exactly once."""
    found = [i for i, n in enumerate(names) if n == name.strip()]
    if len(found) != 1:
        msg = f"{what} {name!r} not found once (found {len(found)}); listed: {names}"
        raise ItmCommandError(msg)
    return found[0]


def row_holding(rows: list[tuple[str, ...]], name: str, what: str) -> int:
    """Return the row with a cell showing name (the column layout is unknown)."""
    found = [i for i, r in enumerate(rows) if name.strip() in r]
    if len(found) != 1:
        listed = [" ".join(r) for r in rows]
        msg = f"{what} {name!r} not found once (found {len(found)}); listed: {listed}"
        raise ItmCommandError(msg)
    return found[0]


# -- actions --------------------------------------------------------------------------


def ensure_enabled(s: _Session, screen: int, part: int, what: str) -> None:
    """Raise when the iTM reports a part disabled (clicking it hangs the iTM)."""
    s.refuse_disabled(screen, part, what)


async def click(s: _Session, screen: int, part: int, what: str) -> Reply:
    """Click a part, never one the iTM reports disabled; a hidden one works."""
    return await s.click(screen, part, what)


async def select(s: _Session, screen: int, part: int, row: int) -> None:
    """Select a list row, as the browser does (CLICK + INTARRAY)."""
    await s.select_row(screen, part, row)


async def select_rows(s: _Session, screen: int, part: int, rows: list[int]) -> None:
    """Select rows of a multi-select list."""
    await s.send(
        screen, part, EventType.CLICK, DataType.INTARRAY, len(rows), array=tuple(rows)
    )


async def close(s: _Session, screen: int, part: int) -> None:
    """Leave a screen with its Close or Cancel part, if it is open. Never OK."""
    if screen in s.state.screens:
        await s.send(screen, part)
        s.state.screens.pop(screen, None)


async def leave(s: _Session, screen: int, cancel: int) -> None:
    """Leave a sub-screen with its Cancel, if open; a "discard?" question gets Yes."""
    if screen not in s.state.screens:
        return
    reply = await s.send(screen, cancel)
    for dialog in reply.dialogs():
        if dialog[1] == "CONFIRM":
            await s.send(screen, dialog[0])
    s.state.screens.pop(screen, None)


async def open_screen(
    s: _Session, button: int, screen: MenuScreen, what: str | None = None
) -> None:
    """Open a menu screen; raise when it doesn't open (what names it, else its ID)."""
    if not await s.open_menu_screen(button, screen):
        shown = screen.name.lower() if what is None else what
        msg = f"the {shown} screen did not open"
        raise ItmCommandError(msg)


async def open_below(s: _Session, screen: int, button: int, target: int) -> None:
    """Open a screen from a button of another one (behind PROGRESS, if any)."""
    reply = await click(s, screen, button, f"screen {target}")
    await s.wait_for_progress(reply)
    if target not in s.state.screens:
        msg = f"screen {target} did not open: {reply.dialogs()}"
        raise ItmCommandError(msg)


async def enter_number(
    s: _Session, screen: int, button: int, value: float, what: str
) -> None:
    """Enter a number through its Change button (never one shown disabled)."""
    ensure_enabled(s, screen, button, what)
    await s.answer_number(button, value, what, screen=screen)


async def press_ok(s: _Session, label: str, screen: int, ok: int, cancel: int) -> bool:
    """Press a screen's OK through ``s.press_ok``, never one shown disabled."""
    ensure_enabled(s, screen, ok, label)
    return await s.press_ok(label, screen, ok, cancel)


async def press(s: _Session, label: str, screen: int, part: int) -> bool:
    """Press an immediate button (radio, delete) through ``s.write``; answer Yes.

    Returns
    -------
        True when sent, False in a dry run.

    """
    ensure_enabled(s, screen, part, label)
    reply = await s.write(label, screen, part)
    if reply is None:
        return False
    await s._answer_confirm(screen, reply)
    return True


async def return_choice(s: _Session, screen: int, ok: int, what: str) -> None:
    """Press a sub-screen's OK that only returns its choice (assumed)."""
    reply = await s.send(screen, ok)
    await s._answer_confirm(screen, reply)
    if screen in s.state.screens:
        msg = f"{what}: the screen did not close"
        raise ItmCommandError(msg)


async def answer_name(
    s: _Session, screen: int, reply: Reply, name: str, label: str
) -> bool:
    """Answer the CHARINPUT dialog a button opened, through ``s.write``.

    Returns
    -------
        True when sent, False in a dry run (the dialog is cancelled).

    """
    dialogs = [u for u in reply.screens if u.update == "DLG_OPEN"]
    if not dialogs or dialogs[0].data[1] != "CHARINPUT":
        msg = f"{label}: no name input opened: {reply.dialogs()}"
        raise ItmCommandError(msg)
    d = dialogs[0]
    low, high = d.data[4], d.data[5]
    if not low <= len(name) <= high:
        await s.send(d.screen, d.data[0], EventType.CLICK2)
        msg = f"{label}: a name is {low}-{high} characters: {name!r}"
        raise ItmCommandError(msg)
    sent = await s.write(
        label, d.screen, d.data[0], EventType.CLICK, DataType.STRING, string=name
    )
    if sent is None:
        await s.send(d.screen, d.data[0], EventType.CLICK2)
        return False
    await s._answer_confirm(screen, sent)
    return True
