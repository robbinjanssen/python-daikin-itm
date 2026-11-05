"""The management point data registration (service screen 2800, ScMngMain): read only.

The service tab's "Mgmt. Point Data Register" button (63, behind the service login)
opens the list of registered management points (part 1: type, name, address,
info). The screen edits the whole registration in memory: OK (12) checks and saves
it and restarts the iTM, Cancel (13) discards it after a CONFIRM ("discard
changes?"), answered Yes (nothing was changed). Its other buttons change the
registration or scan the bus and are never pressed here: Add (3), Delete (4),
Copy (6), Load (8, load a CSV file), A/C Auto Register (10), Check (11, which
restores the saved data on an error) and OK (12).

With ``details=True`` each point's attributes screen is read: its row is selected
(a CLICK with the row as INT), Edit (5, disabled until a row is selected)
opens the attributes screen for the point's type (2802-2814, 2824-2827 in the web
UI's script, each with its own Cancel part), and that screen is left with its
Cancel (a CONFIRM answered Yes). Only the label and text pairs are read (the text
fields with the label before them: port, type, management point ID, name, details
and so on); check boxes, radio buttons and combo boxes are not.

Seen on a real iTM (read only): 2800's parts as listed above, plus a "CSV" label (7)
and Save (9, save the CSV file); one row per management point, e.g. type
"Indoor unit", a name, address "1:1-00" and an empty info; Edit disabled until
a row is selected; Cancel asks a CONFIRM. Assumed (not seen on a real iTM):
the attributes screens and their Cancel, and that cancelling them leaves 2800 open
with the registration unchanged.
"""

from __future__ import annotations

import html
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, TypeVar

from .exceptions import ItmCommandError
from .menu import MenuScreen

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .protocol import Reply
    from .session import _Session
    from .state import UIState

_T = TypeVar("_T")

MENULIST = MenuScreen.MENULIST
MB_POINT_REGISTRY = 63  # service tab: Mgmt. Point Data Register
REGISTRY = 2800
RG_LIST, RG_EDIT, RG_CANCEL = 1, 5, 13
# Never pressed: Add, Delete, Copy, Load (CSV), A/C Auto Register, Check, OK.
RG_FORBIDDEN = frozenset({3, 4, 6, 8, 10, 11, 12})
# The attributes screen per point type: (type, Cancel part), from the web UI's script.
ATTRIBUTE_SCREENS: dict[int, tuple[str, int]] = {
    2802: ("indoor", 22),
    2803: ("outdoor", 18),
    2804: ("hrv_chiller", 22),
    2805: ("di", 20),
    2806: ("d3_di_dio", 22),
    2807: ("external_di", 23),
    2808: ("external_dio", 31),
    2809: ("external_ai", 20),
    2810: ("external_ao", 20),
    2811: ("pseudo_ai", 15),
    2812: ("pi", 20),
    2813: ("external_pi", 20),
    2814: ("pseudo_pi", 18),
    2824: ("hydrobox", 22),
    2825: ("inv_chiller", 22),
    2826: ("general_ahu", 23),
    2827: ("general_chiller", 23),
}


@dataclass(frozen=True)
class PointAttributes:
    """A management point's attributes screen, as shown (read only)."""

    screen: int  # 2802 indoor, 2803 outdoor, ...
    type: str  # from the screen: "indoor", "outdoor", ...
    fields: tuple[tuple[str, str], ...]  # (label, text) per text field, in part order


@dataclass(frozen=True)
class PointRegistration:
    """A registered management point, as the registration list shows it."""

    row: int  # the row in the list (what selecting it sends)
    type: str
    name: str
    address: str
    info: str
    columns: tuple[str, ...]  # every cell of the row
    attributes: PointAttributes | None = None  # only with details=True


def _clean(value: Any) -> str:
    return html.unescape(str(value)).strip()


def registrations_from_screen(state: UIState) -> tuple[PointRegistration, ...]:
    """Read the registration list (2800 part 1); rows without any text are skipped.

    Args:
    ----
        state: The session's merged screens.

    Returns:
    -------
        The registered points, by column index (the headers are localised).

    """
    panel = state.get(REGISTRY, RG_LIST, "LIST_ROW") or {}
    points = []
    for row, raw in enumerate(panel.get("rows", [])):
        cells = tuple(_clean(t) for t in raw.get("text", []))
        if not any(cells):
            continue
        kind, name, address, info = (*cells, "", "", "", "")[:4]
        points.append(PointRegistration(row, kind, name, address, info, cells))
    return tuple(points)


def attributes_from_screen(state: UIState, screen: int) -> PointAttributes:
    """Read an attributes screen's text fields, each with the label before it.

    Args:
    ----
        state: The session's merged screens.
        screen: The attributes screen (one of ATTRIBUTE_SCREENS).

    Returns:
    -------
        The screen's type and its (label, text) pairs.

    """
    parts = state.screens.get(screen, {})
    fields = []
    for part, ptype in sorted(parts):
        if ptype != "TEXT":
            continue
        label = parts.get((part - 1, "LABEL"))
        fields.append(
            (
                _clean(label) if isinstance(label, str) else "",
                _clean(parts[part, ptype]),
            )
        )
    return PointAttributes(screen, ATTRIBUTE_SCREENS[screen][0], tuple(fields))


def _stray_screens(state: UIState) -> list[int]:
    """Return the registration's sub-screens still open (none after a clean read)."""
    return [n for n in state.screens if REGISTRY < n < REGISTRY + 100]


async def _click(s: _Session, screen: int, part: int, what: str) -> Reply:
    """Click a part: never a forbidden one, nor one the iTM reports disabled."""
    if screen == REGISTRY and part in RG_FORBIDDEN:
        msg = f"{what}: part {part} of the registration screen is never pressed"
        raise ItmCommandError(msg)
    return await s.click(screen, part, what)


MAX_CONFIRMS = 3  # questions answered when leaving a screen


async def _cancel(s: _Session, screen: int, cancel: int, what: str) -> None:
    """Leave a screen with its Cancel, answering its CONFIRMs with Yes.

    A real iTM asks twice when leaving the registration list: "close after
    discarding the changes?" and then "confirm closing?".
    """
    reply = await _click(s, screen, cancel, what)
    for _ in range(MAX_CONFIRMS):
        dialogs = reply.dialogs()
        errors = [d for d in dialogs if d[1] in {"ERROR", "INFO"}]
        if errors:
            msg = f"{what}: the iTM answered {errors[0][2]!r}"
            raise ItmCommandError(msg)
        confirms = [d for d in dialogs if d[1] == "CONFIRM"]
        if not confirms:
            break
        reply = await s.send(screen, confirms[0][0])
    if screen in s.state.screens:
        msg = f"{what}: the screen did not close"
        raise ItmCommandError(msg)


async def _read_attributes(s: _Session, point: PointRegistration) -> PointAttributes:
    """Select a point, open its attributes with Edit, read them, Cancel."""
    what = f"read the attributes of {point.name!r}"
    await s.select_row(REGISTRY, RG_LIST, point.row)
    before = set(s.state.screens)
    reply = await _click(s, REGISTRY, RG_EDIT, what)
    opened = [u.screen for u in reply.screens if u.update == "OPEN"]
    screen = next((n for n in opened if n in ATTRIBUTE_SCREENS), None)
    if screen is None:
        unknown = sorted(set(s.state.screens) - before)
        msg = (
            f"{what}: no known attributes screen opened ({unknown or reply.dialogs()})"
        )
        raise ItmCommandError(msg)
    try:
        return attributes_from_screen(s.state, screen)
    finally:
        await _cancel(s, screen, ATTRIBUTE_SCREENS[screen][1], what)


async def read_registrations(
    s: _Session, password: str, *, details: bool = False
) -> tuple[PointRegistration, ...]:
    """Log in to service mode, read the registration list and leave it (Cancel).

    Args:
    ----
        s: The logged-in session.
        password: The service password (sent once, never stored).
        details: Also read each point's attributes screen.

    Returns:
    -------
        The registered management points.

    Raises:
    ------
        ItmCommandError: The screen did not open or could not be left cleanly.

    """
    await s.service_login(password)
    await s.open_menu()
    reply = await _click(s, MENULIST, MB_POINT_REGISTRY, "open the point registration")
    if REGISTRY not in s.state.screens:
        msg = f"the point registration did not open: {reply.dialogs()}"
        raise ItmCommandError(msg)
    try:
        points = registrations_from_screen(s.state)
        if details:
            points = tuple(
                [replace(p, attributes=await _read_attributes(s, p)) for p in points]
            )
        return points
    finally:
        # Not while another screen is still open on top of it (an unknown
        # attributes screen): the session's end discards that instead.
        if REGISTRY in s.state.screens and not _stray_screens(s.state):
            await _cancel(s, REGISTRY, RG_CANCEL, "leave the point registration")


class PointRegistryMixin:  # pylint: disable=too-few-public-methods
    """ItmClient's management point registration reader."""

    if TYPE_CHECKING:
        # pylint: disable=unused-argument
        async def _in_session(
            self, call: Callable[[_Session], Awaitable[_T]]
        ) -> _T: ...

    async def fetch_point_registrations(
        self, password: str, *, details: bool = False
    ) -> tuple[PointRegistration, ...]:
        """Read the installer's management point registration list. Read only.

        Opens the service tab's "Mgmt. Point Data Register" (2800) after a service
        login and leaves it with Cancel, answering its "discard changes?" with
        Yes (nothing was changed). Its add, delete, copy, CSV load, auto register,
        check and OK buttons are never pressed. The service password is the
        installer's; it is sent once and never stored or logged.

        Args:
        ----
            password: The service password.
            details: Also open each point's attributes screen (Edit, then
                Cancel) and read its text fields; one screen per point.

        Returns:
        -------
            The registered management points: type, name, address and info as
            listed.

        """
        return await self._in_session(
            lambda s: read_registrations(s, password, details=details)
        )
