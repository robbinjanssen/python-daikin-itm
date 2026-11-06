"""Zone structure and icons: add, delete and move zones, zone members, icons.

Session-level flows on the zone settings screens (1600 and the screens it opens)
and the management point settings (1700/1701). Each takes an open session, saves
through the session's ``write`` / ``press_ok`` (so a dry run plans instead of
sending) and leaves every screen it opened.

Renaming, moving or deleting a zone changes the zone paths below it; the root zone
can't be deleted, edited or moved (the iTM disables those buttons for it).
"""

from __future__ import annotations

import contextlib
import html
from typing import TYPE_CHECKING, Any

from ._bases import ClientCore
from ._screens import answer_name as _answer_name
from ._screens import click as _click
from ._screens import raw_rows as _rows
from ._screens import row_cells as _cells
from ._screens import select as _select
from ._util import ignore as _ignore
from .exceptions import ItmCommandError
from .menu import CLOSE_PART, MenuScreen
from .models import point_kind

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Iterable

    from .session import _Session
    from .state import UIState
    from .system import (
        ManagementPoint,
        ZoneAttributes,
        ZoneInfo,
    )


# Menu list buttons (screen 5).
MB_ZONES, MB_POINTS = 21, 23

# Zone settings (1600) and the screens it opens.
ZONES = MenuScreen.ZONES
MEMBERS, ICONS, MOVE = 1601, 1604, 1605
ZONE_SETTINGS = MenuScreen.ZONE_SETTINGS
POINTS, POINT_SETTINGS = MenuScreen.POINTS, MenuScreen.POINT_SETTINGS
ZN_LIST, ZN_ADD, ZN_DELETE, ZN_MEMBERS, ZN_SETTINGS, ZN_MOVE = 4, 6, 7, 8, 9, 10
MB_LIST, MB_ADD, MB_REMOVE, MB_AVAILABLE, MB_OK, MB_CANCEL = 2, 7, 8, 11, 12, 13
MB_UP, MB_DOWN, MB_JUMP = 3, 4, 5
# Member order (1602, opened by the member list's Jump); OK hands the order back
# to 1601.
ORDER, OR_LIST, OR_OK, OR_CANCEL = 1602, 1, 2, 3
MV_LIST, MV_OK, MV_CANCEL = 4, 5, 6
IC_LIST, IC_OK, IC_CANCEL = 1, 2, 3
ZA_ICON, ZA_ICON_BUTTON, ZA_OK = 16, 17, 18
PT_LIST, PT_CHANGE = 1, 2
PA_ICON, PA_ICON_BUTTON, PA_OK = 10, 11, 12
# Zone list columns; the root's level is "-".
ZC_NAME, ZC_LEVEL, ZC_ZONES, ZC_POINTS = 0, 1, 2, 3
# The zone error screen (1606, ScSysAreaErr).
ZONE_ERROR, ZE_TEXT, ZE_CLOSE = 1606, 1, 2


def zone_error_from_screen(state: UIState) -> str | None:
    """Return the text of the zone error screen (1606), or None when it isn't open.

    Nothing in script.js opens 1606 (ScSysAreaErr): the iTM opens it itself, so it
    is taken to be its answer to a zone operation that failed. Not seen on a real
    iTM yet.
    """
    if ZONE_ERROR not in state.screens:
        return None
    text = state.get(ZONE_ERROR, ZE_TEXT, "TEXT")
    return html.unescape(text).strip() if isinstance(text, str) else ""


async def _report_zone_error(s: _Session, cause: Exception | None = None) -> None:
    """Close the zone error screen if the iTM opened it, and raise its text."""
    text = zone_error_from_screen(s.state)
    if text is None:
        return
    await s.send(ZONE_ERROR, ZE_CLOSE)
    s.state.screens.pop(ZONE_ERROR, None)
    msg = f"the iTM reported a zone error: {text!r}"
    raise ItmCommandError(msg) from cause


@contextlib.asynccontextmanager
async def _zone_errors(s: _Session) -> AsyncIterator[None]:
    """Turn a zone error screen (1606) opened by any request into ItmCommandError.

    It is closed before the flow leaves its other screens.
    """
    try:
        yield
    except ItmCommandError as err:
        await _report_zone_error(s, err)
        raise
    await _report_zone_error(s)


# For the zone flows outside this module (the zone settings in client.py).
zone_errors = _zone_errors


def _find_row(state: UIState, screen: int, part: int, column: int, name: str) -> int:
    """Return the row whose column shows name; raise when not there exactly once."""
    found = [
        i
        for i, row in enumerate(_rows(state, screen, part))
        if len(_cells(row)) > column and _cells(row)[column].strip() == name.strip()
    ]
    if len(found) != 1:
        msg = f"{name!r} not found once (found {len(found)})"
        raise ItmCommandError(msg)
    return found[0]


async def _leave(s: _Session, screen: int, cancel: int) -> None:
    """Leave a screen with its Cancel (a 'discard?' confirm is answered Yes)."""
    if screen not in s.state.screens:
        return
    reply = await s.send(screen, cancel)
    for dialog in reply.dialogs():
        if dialog[1] == "CONFIRM":
            with contextlib.suppress(ItmCommandError):
                await s.send(screen, dialog[0])
    s.state.screens.pop(screen, None)


async def _open_zones(s: _Session) -> None:
    if not await s.open_menu_screen(MB_ZONES, ZONES):
        msg = "the zone settings did not open"
        raise ItmCommandError(msg)


async def _select_zone(s: _Session, zone: str) -> list[str]:
    """Select a zone's row in the zone list; return its cells."""
    row = _find_row(s.state, ZONES, ZN_LIST, ZC_NAME, zone)
    await _select(s, ZONES, ZN_LIST, row)
    return [*_cells(_rows(s.state, ZONES, ZN_LIST)[row]), "", "", "", ""]


def zone_members_from_screen(state: UIState) -> tuple[str, ...]:
    """Return the names of the open member screen's members (1601)."""
    return tuple(
        _cells(r)[1].strip()
        for r in _rows(state, MEMBERS, MB_LIST)
        if len(_cells(r)) > 1
    )


async def add_zone(s: _Session, name: str, *, parent: str | None = None) -> bool:
    """Add a zone (below parent, or below the selected/top level) by its name.

    Returns
    -------
        True when sent, False in a dry run.

    """
    await _open_zones(s)
    try:
        async with _zone_errors(s):
            if any(
                _cells(r)[ZC_NAME].strip() == name.strip()
                for r in _rows(s.state, ZONES, ZN_LIST)
            ):
                msg = f"zone {name!r} exists already"
                raise ItmCommandError(msg)
            if parent is not None:
                await _select_zone(s, parent)
            reply = await _click(s, ZONES, ZN_ADD, "add zone")
            return await _answer_name(s, ZONES, reply, name, f"add zone {name}")
    finally:
        await s.close_screen(ZONES)


async def delete_zone(s: _Session, name: str) -> bool:
    """Delete an empty zone (no zones and no points in it); confirm.

    Returns
    -------
        True when sent, False in a dry run.

    """
    await _open_zones(s)
    try:
        async with _zone_errors(s):
            cells = await _select_zone(s, name)
            if cells[ZC_LEVEL].strip() == "-":
                msg = "the root zone can't be deleted"
                raise ItmCommandError(msg)
            if cells[ZC_ZONES].strip() not in {"", "0"} or cells[
                ZC_POINTS
            ].strip() not in {
                "",
                "0",
            }:
                msg = f"zone {name!r} is not empty: remove its zones and points first"
                raise ItmCommandError(msg)
            if s.state.get(ZONES, ZN_DELETE, "ENABLED") == 0:
                msg = f"the iTM does not allow deleting {name!r}"
                raise ItmCommandError(msg)
            reply = await s.write(f"delete zone {name}", ZONES, ZN_DELETE)
            if reply is None:
                return False
            await s._answer_confirm(ZONES, reply)  # noqa: SLF001  # pylint: disable=protected-access
            return True
    finally:
        await s.close_screen(ZONES)


async def move_zone(s: _Session, name: str, parent: str) -> bool:
    """Move a zone below another zone (zone paths below it change).

    Returns
    -------
        True when sent, False in a dry run.

    """
    await _open_zones(s)
    try:
        async with _zone_errors(s):
            await _select_zone(s, name)
            await _click(s, ZONES, ZN_MOVE, "move zone")
            if MOVE not in s.state.screens:
                msg = "the move screen did not open"
                raise ItmCommandError(msg)
            try:
                row = _find_row(s.state, MOVE, MV_LIST, ZC_NAME, parent)
            except ItmCommandError as err:
                msg = f"{name!r} can't move below {parent!r}: {err}"
                raise ItmCommandError(msg) from err
            await _select(s, MOVE, MV_LIST, row)
            return await s.press_ok(
                f"move zone {name} to {parent}", MOVE, MV_OK, MV_CANCEL
            )
    finally:
        await _leave(s, MOVE, MV_CANCEL)
        await s.close_screen(ZONES)


async def read_zone_members(s: _Session, zone: str) -> tuple[str, ...]:
    """Open a zone's member screen, read its management points, and cancel.

    Returns
    -------
        The names of the points in the zone.

    """
    await _open_zones(s)
    try:
        await _select_zone(s, zone)
        await _click(s, ZONES, ZN_MEMBERS, "zone members")
        if MEMBERS not in s.state.screens:
            msg = "the member screen did not open"
            raise ItmCommandError(msg)
        return zone_members_from_screen(s.state)
    finally:
        await _leave(s, MEMBERS, MB_CANCEL)
        await s.close_screen(ZONES)


async def set_zone_members(s: _Session, zone: str, members: tuple[str, ...]) -> bool:
    """Make a zone's management points exactly these (by name), then OK.

    Returns
    -------
        True when sent, False in a dry run.

    """
    await _open_zones(s)
    try:
        async with _zone_errors(s):
            await _select_zone(s, zone)
            await _click(s, ZONES, ZN_MEMBERS, "zone members")
            if MEMBERS not in s.state.screens:
                msg = "the member screen did not open"
                raise ItmCommandError(msg)
            wanted = [m.strip() for m in members]
            for name in zone_members_from_screen(s.state):
                if name not in wanted:
                    await _select(
                        s,
                        MEMBERS,
                        MB_LIST,
                        _find_row(s.state, MEMBERS, MB_LIST, 1, name),
                    )
                    await _click(s, MEMBERS, MB_REMOVE, "remove member")
            for name in wanted:
                if name not in zone_members_from_screen(s.state):
                    try:
                        row = _find_row(s.state, MEMBERS, MB_AVAILABLE, 1, name)
                    except ItmCommandError as err:
                        msg = f"{name!r} is not offered as a member: {err}"
                        raise ItmCommandError(msg) from err
                    await _select(s, MEMBERS, MB_AVAILABLE, row)
                    await _click(s, MEMBERS, MB_ADD, "add member")
            if set(zone_members_from_screen(s.state)) != set(wanted):
                msg = f"{zone}: the members did not become {wanted}"
                raise ItmCommandError(msg)
            return await s.press_ok(
                f"save members of {zone}", MEMBERS, MB_OK, MB_CANCEL
            )
    finally:
        await _leave(s, MEMBERS, MB_CANCEL)
        await s.close_screen(ZONES)


async def _jump(s: _Session, zone: str, name: str, position: int) -> None:
    """Move a member to a row with Jump: select it, Jump (1602), pick the row, OK.

    Assumption (script.js only lays 1602 out, the iTM decides): 1602 lists the
    members in their order and the selected member takes the picked row's place, so
    it lands before that row's member when it moves up and after it when it moves
    down. 1601's list is checked afterwards; anything else is refused (nothing is
    saved before 1601's OK).
    """
    row = _find_row(s.state, MEMBERS, MB_LIST, 1, name)
    await _select(s, MEMBERS, MB_LIST, row)
    await _click(s, MEMBERS, MB_JUMP, "jump member")
    if ORDER not in s.state.screens:
        msg = "the member order screen did not open"
        raise ItmCommandError(msg)
    try:
        count = len(_rows(s.state, ORDER, OR_LIST))
        if count != len(zone_members_from_screen(s.state)):
            msg = f"{zone}: the order screen lists {count} rows, not the members"
            raise ItmCommandError(msg)
        await _select(s, ORDER, OR_LIST, position)
        await _click(s, ORDER, OR_OK, "jump member to")
    finally:
        await _leave(s, ORDER, OR_CANCEL)
    if _find_row(s.state, MEMBERS, MB_LIST, 1, name) != position:
        msg = f"{zone}: Jump did not put {name!r} at row {position + 1}"
        raise ItmCommandError(msg)


async def _move_up(s: _Session, zone: str, name: str, position: int) -> None:
    """Move a member up one row at a time (the up button) to a row."""
    row = _find_row(s.state, MEMBERS, MB_LIST, 1, name)
    while row > position:
        await _select(s, MEMBERS, MB_LIST, row)
        await _click(s, MEMBERS, MB_UP, "move member up")
        moved = _find_row(s.state, MEMBERS, MB_LIST, 1, name)
        if moved != row - 1:
            msg = f"{zone}: {name!r} did not move up"
            raise ItmCommandError(msg)
        row = moved


async def set_zone_member_order(
    s: _Session, zone: str, order: tuple[str, ...], *, use_jump: bool = False
) -> bool:
    """Put a zone's members in this order, then OK.

    By default with the member list's up button, one row at a time; with use_jump
    each member out of place is moved to its row at once with Jump (1602, see
    ``_jump`` for the assumption it rests on). Members not named keep their
    relative order after the named ones.

    Returns
    -------
        True when sent, False in a dry run.

    """
    await _open_zones(s)
    try:
        async with _zone_errors(s):
            await _select_zone(s, zone)
            await _click(s, ZONES, ZN_MEMBERS, "zone members")
            if MEMBERS not in s.state.screens:
                msg = "the member screen did not open"
                raise ItmCommandError(msg)
            wanted = [m.strip() for m in order]
            current = zone_members_from_screen(s.state)
            missing = [m for m in wanted if m not in current]
            if missing:
                msg = f"{zone}: not members: {missing}"
                raise ItmCommandError(msg)
            for position, name in enumerate(wanted):
                if not use_jump:
                    await _move_up(s, zone, name, position)
                elif _find_row(s.state, MEMBERS, MB_LIST, 1, name) != position:
                    await _jump(s, zone, name, position)
            if list(zone_members_from_screen(s.state)[: len(wanted)]) != wanted:
                msg = f"{zone}: the order did not become {wanted}"
                raise ItmCommandError(msg)
            what = f"save member order of {zone}"
            return await s.press_ok(what, MEMBERS, MB_OK, MB_CANCEL)
    finally:
        await _leave(s, ORDER, OR_CANCEL)
        await _leave(s, MEMBERS, MB_CANCEL)
        await s.close_screen(ZONES)


async def _pick_icon(s: _Session, icon: int) -> None:
    """Pick an icon in the open icon list (1604) and press its OK."""
    rows = _rows(s.state, ICONS, IC_LIST)
    found = [i for i, r in enumerate(rows) if (r.get("a") or [-1])[0] == icon]
    if not found:
        offered = [(r.get("a") or [-1])[0] for r in rows]
        await _leave(s, ICONS, IC_CANCEL)
        msg = f"icon {icon} is not offered: {offered}"
        raise ItmCommandError(msg)
    await _select(s, ICONS, IC_LIST, found[0])
    await s.send(ICONS, IC_OK)  # only returns the choice to the settings screen
    s.state.screens.pop(ICONS, None)


async def set_zone_icon(s: _Session, zone: str, icon: int) -> bool:
    """Give a zone another icon (1000-1009 on the iTM this was built on), then OK.

    Returns
    -------
        True when sent, False in a dry run.

    """
    await _open_zones(s)
    try:
        async with _zone_errors(s):
            await _select_zone(s, zone)
            await _click(s, ZONES, ZN_SETTINGS, "zone settings")
            if ZONE_SETTINGS not in s.state.screens:
                msg = "the zone settings screen did not open"
                raise ItmCommandError(msg)
            await _click(s, ZONE_SETTINGS, ZA_ICON_BUTTON, "zone icon")
            await _pick_icon(s, icon)
            return await s.press_ok(
                f"save icon {icon} of zone {zone}",
                ZONE_SETTINGS,
                ZA_OK,
                CLOSE_PART[ZONE_SETTINGS],
            )
    finally:
        await _leave(s, ICONS, IC_CANCEL)
        await s.close_screen(ZONE_SETTINGS)
        await s.close_screen(ZONES)


def _icon_allowed(current: int | None, icon: int) -> str | None:
    """Return why a point may not get icon, or None when it may."""
    if current is None:
        return "the point's current icon is unknown"
    if current // 100 != icon // 100:
        return f"icon {icon} is outside the point's family ({current // 100}xx)"
    if point_kind(current) != point_kind(icon):
        # The library recognises points by icon: a point would turn into "other".
        return f"icon {icon} is not one the library recognises like {current}"
    return None


async def set_point_icon(s: _Session, point: str, icon: int) -> bool:
    """Give a management point another icon of its own family, then OK.

    Returns
    -------
        True when sent, False in a dry run.

    """
    if not await s.open_menu_screen(MB_POINTS, POINTS):
        msg = "the management point settings did not open"
        raise ItmCommandError(msg)
    try:
        async with _zone_errors(s):
            await _select(
                s, POINTS, PT_LIST, _find_row(s.state, POINTS, PT_LIST, 1, point)
            )
            await _click(s, POINTS, PT_CHANGE, "point settings")
            if POINT_SETTINGS not in s.state.screens:
                msg = "the point settings screen did not open"
                raise ItmCommandError(msg)
            current = s.state.get(POINT_SETTINGS, PA_ICON, "ICONID")
            if (why := _icon_allowed(current, icon)) is not None:
                msg = f"{point}: {why}"
                raise ItmCommandError(msg)
            await _click(s, POINT_SETTINGS, PA_ICON_BUTTON, "point icon")
            await _pick_icon(s, icon)
            return await s.press_ok(
                f"save icon {icon} of point {point}",
                POINT_SETTINGS,
                PA_OK,
                CLOSE_PART[POINT_SETTINGS],
            )
    finally:
        await _leave(s, ICONS, IC_CANCEL)
        await s.close_screen(POINT_SETTINGS)
        await s.close_screen(POINTS)


def _shown(value: Any, key: str) -> Any:
    """Return a read-back value for comparing: no description reads as ""."""
    return "" if key == "info" and value is None else value


class ZonesMixin(ClientCore):
    """ItmClient's zone structure, icon and point attribute writes."""

    async def _zone_list(self) -> tuple[ZoneInfo, ...]:
        return (await self.fetch_system(["zones"])).zones or ()

    async def create_zone(
        self, name: str, *, parent: str | None = None
    ) -> ZoneInfo | None:
        """Add a zone (empty), at the top level or below a parent zone.

        Args:
        ----
            name: The new zone's name (1-12 characters).
            parent: The zone to add it below; None for the top level.

        Returns:
        -------
            The zone as the zone list shows it, or None in a dry run.

        """
        await self._system_write(lambda s: _ignore(add_zone(s, name, parent=parent)))
        if self.dry_run:
            return None
        zone = next((z for z in await self._zone_list() if z.name == name), None)
        if zone is None:
            msg = f"{name}: the iTM did not add the zone"
            raise ItmCommandError(msg)
        return zone

    async def delete_zone(self, name: str) -> None:
        """Delete an empty zone (one without zones or points in it).

        Args:
        ----
            name: The zone.

        """
        await self._system_write(lambda s: _ignore(delete_zone(s, name)))
        if not self.dry_run and any(z.name == name for z in await self._zone_list()):
            msg = f"{name}: the iTM did not delete the zone"
            raise ItmCommandError(msg)

    async def move_zone(self, name: str, parent: str) -> ZoneInfo | None:
        """Move a zone below another zone (or to the top). Its zone paths change.

        Args:
        ----
            name: The zone to move.
            parent: Where to: a zone, or the root's name for the top level.

        Returns:
        -------
            The zone as the zone list shows it, or None in a dry run.

        """
        await self._system_write(lambda s: _ignore(move_zone(s, name, parent)))
        if self.dry_run:
            return None
        zones = await self._zone_list()
        zone = next((z for z in zones if z.name == name), None)
        target = next((z for z in zones if z.name == parent), None)
        want = None if target is None else (target.level or 0) + 1
        if zone is None or (want is not None and zone.level != want):
            msg = f"{name}: the iTM did not move the zone below {parent}"
            raise ItmCommandError(msg)
        return zone

    async def fetch_zone_members(self, zone: str) -> tuple[str, ...]:
        """Read the management points in a zone, from its member screen.

        Args:
        ----
            zone: The zone.

        Returns:
        -------
            The points' names.

        """
        return await self._in_session(lambda s: read_zone_members(s, zone))

    async def set_zone_members(
        self, zone: str, members: Iterable[str]
    ) -> tuple[str, ...] | None:
        """Make a zone's management points exactly these.

        Args:
        ----
            zone: The zone.
            members: The points' names; the others are taken out of the zone.

        Returns:
        -------
            The members as read back, or None in a dry run.

        """
        wanted = tuple(m.strip() for m in members)
        await self._system_write(lambda s: _ignore(set_zone_members(s, zone, wanted)))
        if self.dry_run:
            return None
        result = await self.fetch_zone_members(zone)
        if set(result) != set(wanted):
            msg = f"{zone}: the members are {result}, not {wanted}"
            raise ItmCommandError(msg)
        return result

    async def set_zone_member_order(
        self, zone: str, order: Iterable[str], *, use_jump: bool = False
    ) -> tuple[str, ...] | None:
        """Put a zone's members in this order (the order the iTM lists them in).

        Args:
        ----
            zone: The zone.
            order: Member names, first to last; members not named follow them, in
                their current order.
            use_jump: Move each member to its row with the list's Jump (one
                step, 1602) instead of the up button (tested against the
                simulated iTM only).

        Returns:
        -------
            The members as read back, or None in a dry run.

        """
        if use_jump:
            self._experimental("reorder zone members with Jump")
        wanted = tuple(m.strip() for m in order)
        await self._system_write(
            lambda s: _ignore(set_zone_member_order(s, zone, wanted, use_jump=use_jump))
        )
        if self.dry_run:
            return None
        result = await self.fetch_zone_members(zone)
        if result[: len(wanted)] != wanted:
            msg = f"{zone}: the members are in the order {result}, not {wanted}"
            raise ItmCommandError(msg)
        return result

    async def set_zone_icon(self, zone: str, icon: int) -> ZoneAttributes | None:
        """Give a zone another icon (1000-1009).

        Args:
        ----
            zone: The zone.
            icon: The icon ID.

        Returns:
        -------
            The zone's attributes as read back, or None in a dry run.

        """
        await self._system_write(lambda s: _ignore(set_zone_icon(s, zone, icon)))
        if self.dry_run:
            return None
        result = await self.fetch_zone_attributes(zone)
        if result.icon != icon:
            msg = f"{zone}: the icon is {result.icon}, not {icon}"
            raise ItmCommandError(msg)
        return result

    async def set_point_icon(self, point: str, icon: int) -> ManagementPoint | None:
        """Give a management point another icon of its own kind.

        Args:
        ----
            point: The point's name.
            icon: The icon ID (one of the same family, e.g. 21xx for indoor units).

        Returns:
        -------
            The point as read back, or None in a dry run.

        """
        await self._system_write(lambda s: _ignore(set_point_icon(s, point, icon)))
        if self.dry_run:
            return None
        points = (await self.fetch_system(["points"])).points or ()
        result = next((p for p in points if p.name == point), None)
        if result is None or result.icon != icon:
            msg = f"{point}: the icon did not become {icon}: {result}"
            raise ItmCommandError(msg)
        return result

    async def set_maintenance(self, point: str, *, on: bool) -> bool | None:
        """Mark a management point as in maintenance, or end it.

        Args:
        ----
            point: The point's name, e.g. "1:1-09".
            on: Put it in maintenance (True) or take it out.

        Returns:
        -------
            Whether it is in maintenance as read back, or None in a dry run.

        """
        await self._system_write(lambda s: s.set_maintenance(point, on=on))
        if self.dry_run:
            return None
        points = (await self.fetch_system(["maintenance"])).maintenance or ()
        state = next((p.in_maintenance for p in points if p.name == point), None)
        if state is not on:
            msg = f"{point}: the iTM did not take maintenance {on}"
            raise ItmCommandError(msg)
        return state

    async def set_point_attributes(
        self, point: str, *, name: str | None = None, info: str | None = None
    ) -> ManagementPoint | None:
        """Change a management point's description (or name).

        Renaming a point changes how the library and Home Assistant find it.

        Args:
        ----
            point: The point's current name.
            name: Its new name (1-12 characters).
            info: Its new description (0-50 characters), what the CLI shows as name.

        Returns:
        -------
            The point as read back, or None in a dry run.

        """
        if name is None and info is None:
            msg = "nothing to change"
            raise ItmCommandError(msg)
        await self._system_write(
            lambda s: s.set_point_attributes(point, name=name, info=info)
        )
        if self.dry_run:
            return None
        wanted = name or point
        points = (await self.fetch_system(["points"])).points or ()
        result = next((p for p in points if p.name == wanted), None)
        if result is None or (info is not None and (result.info or "") != info):
            msg = f"{point}: the iTM did not take the change: {result}"
            raise ItmCommandError(msg)
        return result

    async def fetch_zone_attributes(self, zone: str) -> ZoneAttributes:
        """Read a zone's name, description and intervals from its settings screen.

        Args:
        ----
            zone: The zone's name as the zone list shows it.

        Returns:
        -------
            The attributes.

        """
        return await self._in_session(lambda s: s.read_zone_attributes(zone))

    async def set_zone_attributes(
        self,
        zone: str,
        *,
        name: str | None = None,
        info: str | None = None,
        start_interval: int | None = None,
        stop_interval: int | None = None,
    ) -> ZoneAttributes | None:
        """Change a zone's description, intervals (or name).

        Renaming a zone changes every zone path that names it.

        Args:
        ----
            zone: The zone's current name.
            name: Its new name (1-12 characters).
            info: Its new description (0-50 characters).
            start_interval: 0-180, as the iTM's input allows.
            stop_interval: 0-180.

        Returns:
        -------
            The zone's attributes as read back, or None in a dry run.

        """
        changes = {
            "name": name,
            "info": info,
            "start_interval": start_interval,
            "stop_interval": stop_interval,
        }
        if all(v is None for v in changes.values()):
            msg = "nothing to change"
            raise ItmCommandError(msg)
        await self._system_write(
            lambda s: s.set_zone_attributes(
                zone,
                name=name,
                info=info,
                start_interval=start_interval,
                stop_interval=stop_interval,
            )
        )
        if self.dry_run:
            return None
        result = await self.fetch_zone_attributes(name or zone)
        wrong = {
            k: v
            for k, v in changes.items()
            if v is not None and _shown(getattr(result, k), k) != v
        }
        if wrong:
            msg = f"{zone}: the iTM did not take {wrong}: {result}"
            raise ItmCommandError(msg)
        return result
