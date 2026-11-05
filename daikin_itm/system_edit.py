# ruff: noqa: SLF001
# pylint: disable=protected-access
"""Writes for e-mail, confirm dialogs, web server, remote management, web users.

The logged-in user's own settings are screen 1205 (password, language and region
settings); a normal web user's allowed screens are screen 1201.

Session-level functions: each takes an open ``_Session`` (the user's own settings
take a fresh one, see ``open_own_settings``). Every saving request goes through
``s.write`` / ``s.press_ok``, so a dry run plans it instead of sending it. Writes that
can cut access (web server port, remote management) or that reach the outside world
(the test e-mail) refuse unless explicitly allowed. Passwords are never logged and
never put into a dry run's plan.
"""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, TypeVar

from .exceptions import ItmCommandError, LoginRejected, SessionEnded
from .menu import CLOSE_PART, DATE_FORMATS, MenuScreen
from .protocol import NO_TOKEN, DataType, EventType, ScreenID
from .system import (
    EU_DST_END,
    EU_DST_START,
    LANGUAGE_RADIOS,
    LG_OK,
    MB_CONFIRM_DIALOGS,
    MB_EMAIL,
    MB_NETWORK,
    MB_REMOTE_MANAGEMENT,
    MB_WEB_USERS,
    US_ADMIN,
    US_OK,
    WU_CHANGE,
    WU_LIST,
    web_users_from_screen,
)

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from contextlib import AbstractAsyncContextManager

    from .client import _Session
    from .menu import ItmInfo
    from .protocol import Reply
    from .state import UIState
    from .system import (
        DstRule,
        DstSettings,
        EmailSettings,
        SystemSettings,
        WebUser,
    )

_T = TypeVar("_T")

# E-mail notification (1300) and its address (1301), test mail (1302) and server
# (1303) screens.
EMAIL_LIST, EMAIL_ON, EMAIL_OFF = 1, 3, 4
EMAIL_ADD, EMAIL_DELETE, EMAIL_EDIT, EMAIL_SERVER, EMAIL_CLOSE = 6, 7, 8, 10, 12
ADDRESS_SCREEN, TRIAL_SCREEN, SERVER_SCREEN = 1301, 1302, 1303
AD_TAB, AD_SELECTED, AD_ADD, AD_REMOVE, AD_AVAILABLE = 1, 3, 5, 6, 9
AD_TEXT, AD_CHANGE, AD_TRIAL, AD_OK, AD_CANCEL = 11, 12, 13, 17, 18
TR_OK, TR_CANCEL = 3, 4
SV_TAB, SV_INTERVAL, SV_AUTH, SV_OK, SV_CANCEL = 1, 6, 26, 40, 41
# Server text fields: (shown text part, its change button, tab, kind).
SV_FIELDS: dict[str, tuple[int, int, int, str]] = {
    "site_name": (3, 4, 0, "text"),
    "from_address": (9, 10, 0, "text"),
    "smtp_address": (19, 20, 1, "text"),
    "smtp_port": (22, 23, 1, "number"),
    "pop_address": (29, 30, 1, "text"),
    "pop_port": (32, 33, 1, "number"),
    "user_id": (35, 36, 1, "text"),
}
SV_PASSWORD_BUTTON = 39
# Confirm dialogs (2100).
CD_ON, CD_OFF, CD_OK, CD_CANCEL = 2, 3, 4, 5
# Web server (1801, from the network screen's button 22).
WEB_SERVER_SCREEN, NW_WEB_SERVER = 1801, 22
WS_DEFAULT, WS_CUSTOM, WS_PORT, WS_CHANGE, WS_OK, WS_CANCEL = 2, 3, 4, 5, 6, 7
NW_CANCEL = 24
# Remote management (1100).
RM_CONNECT, RM_DISCONNECT = 5, 6
# The logged-in user's own settings (1205), reached from the login screen.
OWN_SCREEN, LOGIN_SETTINGS_BUTTON = 1205, 5
OWN_PASSWORD_BUTTON, OWN_LANGUAGE_BUTTON = 3, 7
OWN_LANGUAGE, OWN_DATE_FORMAT, OWN_24H, OWN_12H = 6, 9, 11, 12
OWN_POINT, OWN_COMMA, OWN_COLOUR_1, OWN_COLOUR_2, OWN_OK, OWN_CANCEL = (
    14,
    15,
    17,
    18,
    19,
    20,
)
# A normal web user's allowed screens (1201), from the user setup (1204) button 16.
USER_SCREENS, US_SCREENS_BUTTON = 1201, 16
UA_ALLOWED, UA_ADD, UA_REMOVE, UA_AVAILABLE, UA_OK, UA_CANCEL = 2, 4, 5, 8, 9, 10


@dataclass(frozen=True)
class EmailServer:
    """E-mail server settings to change; None leaves a field as it is."""

    site_name: str | None = None
    from_address: str | None = None
    smtp_address: str | None = None
    smtp_port: int | None = None
    pop_address: str | None = None
    pop_port: int | None = None
    user_id: str | None = None
    password: str | None = None  # sent once, never logged nor planned
    resend_interval: int | None = None  # option index, as the iTM lists them
    auth_method: int | None = None  # option index


@dataclass(frozen=True)
class OwnSettings:
    """The logged-in user's own region settings (screen 1205)."""

    language: str | None
    date_format: str | None  # a strptime format, see menu.DATE_FORMATS
    time_24h: bool | None
    decimal_comma: bool | None
    icon_colour: int | None  # 1 or 2


@dataclass(frozen=True)
class WebUserScreens:
    """The screens a normal web user may open (1201), and the ones it may get."""

    allowed: tuple[str, ...]
    available: tuple[str, ...]


async def _close(s: _Session, screen: int, cancel: int) -> None:
    """Leave a screen with its Cancel/Close, if it is still open."""
    if screen in s.state.screens:
        await s.send(screen, cancel)
        s.state.screens.pop(screen, None)


async def _open(s: _Session, button: int, screen: MenuScreen) -> None:
    if not await s.open_menu_screen(button, screen):
        msg = f"the {screen.name.lower()} screen did not open"
        raise ItmCommandError(msg)


def _radio(state: UIState, screen: int, on: int, off: int) -> bool | None:
    if state.get(screen, on, "CHECK_STATE") == 1:
        return True
    if state.get(screen, off, "CHECK_STATE") == 1:
        return False
    return None


async def _plan_or_send_input(
    s: _Session, reply: Reply, kind: str, value: str, what: str
) -> None:
    """Answer an input dialog, or (dry run) cancel it and plan a masked value."""
    dialogs = [u for u in reply.screens if u.update == "DLG_OPEN" and u.data[1] == kind]
    if not dialogs:
        msg = f"{what}: no {kind} input opened: {reply.dialogs()}"
        raise ItmCommandError(msg)
    d = dialogs[0]
    if s._client.dry_run:
        cancel = (
            (EventType.CLICK2, DataType.INT, 0)
            if kind == "PASSWORD"
            else (
                EventType.CLICK2,
                DataType.NONE,
                -1,
            )
        )
        await s.send(d.screen, d.data[0], *cancel)
        s._client.planned.append(f"{what} = ***: screen {d.screen} input {d.data[0]}")
        return
    data_type = DataType.IPADDRESS if kind == "IPADDR" else DataType.STRING
    answer = await s.send(d.screen, d.data[0], EventType.CLICK, data_type, string=value)
    if any(x[1] in {"ERROR", "INFO"} for x in answer.dialogs()):
        msg = f"{what}: the iTM refused the value"
        raise ItmCommandError(msg)


def email_addresses(state: UIState) -> list[str]:
    """Return the addresses the open e-mail screen lists."""
    rows = (state.get(MenuScreen.EMAIL, EMAIL_LIST, "LIST_ROW") or {}).get("rows", [])
    return [
        html.unescape(str(r["text"][1])).strip() for r in rows if len(r["text"]) > 1
    ]


async def _set_points(s: _Session, points: tuple[str, ...]) -> None:
    """Make the address's points exactly these (tab 0 of the address screen)."""
    a = ADDRESS_SCREEN
    await s.send(a, AD_TAB, EventType.TAB_SELECT, DataType.INT, 0)
    for name in list(_names(s.state, AD_SELECTED)):
        if name not in points:
            await s._select_listed(a, AD_SELECTED, 1, name, "e-mail point")
            await s.click(a, AD_REMOVE, "remove e-mail point")
    for name in points:
        if name in _names(s.state, AD_SELECTED):
            continue
        await s._select_listed(a, AD_AVAILABLE, 1, name, "e-mail point")
        await s.click(a, AD_ADD, "add e-mail point")


def _names(state: UIState, part: int) -> list[str]:
    rows = (state.get(ADDRESS_SCREEN, part, "LIST_ROW") or {}).get("rows", [])
    return [
        html.unescape(str(r["text"][1])).strip() for r in rows if len(r["text"]) > 1
    ]


async def _fill_address(
    s: _Session, address: str | None, points: tuple[str, ...] | None
) -> None:
    a = ADDRESS_SCREEN
    if points is not None:
        await _set_points(s, points)
    if address is not None:
        await s.send(a, AD_TAB, EventType.TAB_SELECT, DataType.INT, 1)
        await s.answer_text(a, AD_CHANGE, address, "e-mail address")


async def add_email_address(s: _Session, address: str, points: tuple[str, ...]) -> bool:
    """Add an e-mail address that reports on these points; OK + Yes.

    Returns
    -------
        True when saved, False in a dry run.

    """
    await _open(s, MB_EMAIL, MenuScreen.EMAIL)
    try:
        if address in email_addresses(s.state):
            msg = f"e-mail address {address!r} exists already"
            raise ItmCommandError(msg)
        reply = await s.click(MenuScreen.EMAIL, EMAIL_ADD, "add e-mail address")
        # The iTM asks the address first (a text input on 1300), then opens 1301.
        inputs = [
            u
            for u in reply.screens
            if u.update == "DLG_OPEN" and u.data[1] == "CHARINPUT"
        ]
        if inputs:
            d = inputs[0]
            if not d.data[4] <= len(address) <= d.data[5]:
                await s.send(d.screen, d.data[0], EventType.CLICK2)
                msg = (
                    f"e-mail address: {address!r} must be {d.data[4]}-{d.data[5]} long"
                )
                raise ItmCommandError(msg)
            if s._client.dry_run:
                await s.send(d.screen, d.data[0], EventType.CLICK2)
                s._client.planned.append(
                    f"add e-mail address {address}: screen {d.screen} input {d.data[0]}"
                )
                return False
            answer = await s.send(
                d.screen, d.data[0], EventType.CLICK, DataType.STRING, string=address
            )
            if any(x[1] in {"ERROR", "INFO"} for x in answer.dialogs()):
                msg = f"e-mail address: the iTM refused {address!r}: {answer.dialogs()}"
                raise ItmCommandError(msg)
            if ADDRESS_SCREEN not in s.state.screens:
                # The address went straight into the list (as on the developing
                # iTM): set the points it reports on through Edit.
                if address not in email_addresses(s.state):
                    msg = f"e-mail address: the iTM did not add {address!r}"
                    raise ItmCommandError(msg)
                if not points:
                    return True
                row = email_addresses(s.state).index(address)
                await s.send(
                    MenuScreen.EMAIL,
                    EMAIL_LIST,
                    EventType.CLICK,
                    DataType.INTARRAY,
                    1,
                    array=(row,),
                )
                await s.click(MenuScreen.EMAIL, EMAIL_EDIT, "edit e-mail")
                if ADDRESS_SCREEN not in s.state.screens:
                    msg = "the e-mail address screen did not open"
                    raise ItmCommandError(msg)
            await _fill_address(s, None, points)
        else:
            if ADDRESS_SCREEN not in s.state.screens:
                msg = f"the e-mail address screen did not open: {reply.dialogs()}"
                raise ItmCommandError(msg)
            await _fill_address(s, address, points)
        return await s.press_ok(
            f"add e-mail address {address}", ADDRESS_SCREEN, AD_OK, AD_CANCEL
        )
    finally:
        await _close(s, ADDRESS_SCREEN, AD_CANCEL)
        await s.close_screen(MenuScreen.EMAIL)


async def edit_email_address(
    s: _Session,
    address: str,
    *,
    new_address: str | None = None,
    points: tuple[str, ...] | None = None,
) -> bool:
    """Change an e-mail address and/or the points it reports on; OK + Yes."""
    e = MenuScreen.EMAIL
    await _open(s, MB_EMAIL, e)
    try:
        await s._select_listed(e, EMAIL_LIST, 1, address, "e-mail address")
        await s.click(e, EMAIL_EDIT, "edit e-mail address")
        if ADDRESS_SCREEN not in s.state.screens:
            msg = "the e-mail address screen did not open"
            raise ItmCommandError(msg)
        await _fill_address(s, new_address, points)
        return await s.press_ok(
            f"change e-mail address {address}", ADDRESS_SCREEN, AD_OK, AD_CANCEL
        )
    finally:
        await _close(s, ADDRESS_SCREEN, AD_CANCEL)
        await s.close_screen(e)


async def delete_email_address(s: _Session, address: str) -> bool:
    """Select an e-mail address, press Delete and confirm."""
    e = MenuScreen.EMAIL
    await _open(s, MB_EMAIL, e)
    try:
        await s._select_listed(e, EMAIL_LIST, 1, address, "e-mail address")
        if s.state.get(e, EMAIL_DELETE, "ENABLED") == 0:
            msg = f"the iTM does not allow deleting {address!r}"
            raise ItmCommandError(msg)
        reply = await s.write(f"delete e-mail address {address}", e, EMAIL_DELETE)
        if reply is None:
            return False
        await s._answer_confirm(e, reply)
        return True
    finally:
        await s.close_screen(e)


async def set_email_enabled(s: _Session, *, enabled: bool) -> bool:
    """Switch e-mail notification on or off (the radio applies at once: no OK)."""
    e = MenuScreen.EMAIL
    await _open(s, MB_EMAIL, e)
    try:
        if _radio(s.state, e, EMAIL_ON, EMAIL_OFF) is enabled:
            return True
        part = EMAIL_ON if enabled else EMAIL_OFF
        if s.state.get(e, part, "ENABLED") == 0:
            msg = "e-mail activation is disabled (it needs an address first)"
            raise ItmCommandError(msg)
        verb = "on" if enabled else "off"
        reply = await s.write(f"e-mail notification {verb}", e, part)
        if reply is None:
            return False
        await s._answer_confirm(e, reply)
        return True
    finally:
        await s.close_screen(e)


def email_server_from_screen(state: UIState) -> dict[str, Any]:
    """Read the open server screen (never the password: it shows as asterisks)."""
    out: dict[str, Any] = {}
    for name, (text, _button, _tab, kind) in SV_FIELDS.items():
        value = state.get(SERVER_SCREEN, text, "TEXT")
        value = html.unescape(value).strip() if isinstance(value, str) else None
        out[name] = (
            int(value) if kind == "number" and value and value.isdigit() else value
        )
    for name, part in (("resend_interval", SV_INTERVAL), ("auth_method", SV_AUTH)):
        out[name] = state.get(SERVER_SCREEN, part, "SINGLE_SELIDX")
        out[f"{name}_options"] = tuple(
            state.get(SERVER_SCREEN, part, "COMBO_STRS") or ()
        )
    return out


async def read_email_server(s: _Session) -> dict[str, Any]:
    """Open the e-mail server screen, read it and cancel."""
    e = MenuScreen.EMAIL
    await _open(s, MB_EMAIL, e)
    try:
        await s.click(e, EMAIL_SERVER, "e-mail server")
        for tab in (0, 1):
            await s.send(SERVER_SCREEN, SV_TAB, EventType.TAB_SELECT, DataType.INT, tab)
        return email_server_from_screen(s.state)
    finally:
        await _close(s, SERVER_SCREEN, SV_CANCEL)
        await s.close_screen(e)


async def set_email_server(s: _Session, server: EmailServer) -> bool:
    """Change the e-mail server settings; OK + Yes. The password is never planned."""
    e, v = MenuScreen.EMAIL, SERVER_SCREEN
    await _open(s, MB_EMAIL, e)
    try:
        await s.click(e, EMAIL_SERVER, "e-mail server")
        if v not in s.state.screens:
            msg = "the e-mail server screen did not open"
            raise ItmCommandError(msg)
        for name, (_text, button, tab, kind) in SV_FIELDS.items():
            value = getattr(server, name)
            if value is None:
                continue
            await s.send(v, SV_TAB, EventType.TAB_SELECT, DataType.INT, tab)
            if kind == "number":
                await s.answer_number(button, value, f"e-mail {name}", screen=v)
            else:
                await s.answer_text(v, button, value, f"e-mail {name}")
        for name, part, tab in (
            ("resend_interval", SV_INTERVAL, 0),
            ("auth_method", SV_AUTH, 1),
        ):
            if (index := getattr(server, name)) is not None:
                await s.send(v, SV_TAB, EventType.TAB_SELECT, DataType.INT, tab)
                await s._pick_option(v, part, index, f"e-mail {name}")
        if server.password is not None:
            await s.send(v, SV_TAB, EventType.TAB_SELECT, DataType.INT, 1)
            reply = await s.click(v, SV_PASSWORD_BUTTON, "e-mail password")
            await _plan_or_send_input(
                s, reply, "PASSWORD", server.password, "e-mail password"
            )
        return await s.press_ok("save e-mail server settings", v, SV_OK, SV_CANCEL)
    finally:
        await _close(s, v, SV_CANCEL)
        await s.close_screen(e)


async def send_test_email(
    s: _Session, address: str, *, allow_send: bool = False
) -> bool:
    """Send the iTM's test mail to an address (it really sends one).

    Refused unless ``allow_send`` is True; a dry run plans it instead.
    """
    if not allow_send and not s._client.dry_run:
        msg = "the test e-mail really sends a mail: pass allow_send=True"
        raise ItmCommandError(msg)
    e = MenuScreen.EMAIL
    await _open(s, MB_EMAIL, e)
    try:
        await s._select_listed(e, EMAIL_LIST, 1, address, "e-mail address")
        await s.click(e, EMAIL_EDIT, "edit e-mail address")
        await s.send(ADDRESS_SCREEN, AD_TAB, EventType.TAB_SELECT, DataType.INT, 1)
        await s.click(ADDRESS_SCREEN, AD_TRIAL, "test e-mail")
        if TRIAL_SCREEN not in s.state.screens:
            msg = "the test e-mail screen did not open"
            raise ItmCommandError(msg)
        return await s.press_ok(
            f"send a test e-mail to {address}", TRIAL_SCREEN, TR_OK, TR_CANCEL
        )
    finally:
        await _close(s, TRIAL_SCREEN, TR_CANCEL)
        await _close(s, ADDRESS_SCREEN, AD_CANCEL)
        await s.close_screen(e)


async def set_confirm_dialogs(s: _Session, *, enabled: bool) -> bool:
    """Switch the iTM's "apply settings?" confirm dialogs on or off; OK."""
    c = MenuScreen.CONFIRM_DIALOGS
    await _open(s, MB_CONFIRM_DIALOGS, c)
    try:
        if _radio(s.state, c, CD_ON, CD_OFF) is enabled:
            return True
        await s.send(c, CD_ON if enabled else CD_OFF)
        verb = "on" if enabled else "off"
        return await s.press_ok(f"confirm dialogs {verb}", c, CD_OK, CD_CANCEL)
    finally:
        await s.close_screen(c)


async def set_web_server_port(
    s: _Session, port: int | None, *, allow_network_change: bool = False
) -> bool:
    """Set the web server's port (None: the default); OK + Yes on both screens.

    A wrong port takes the web UI, and this library, off the network: refused
    unless ``allow_network_change`` (a dry run is always allowed).
    """
    if not allow_network_change and not s._client.dry_run:
        msg = "a web server port change can cut access; pass allow_network_change=True"
        raise ItmCommandError(msg)
    n, w = MenuScreen.NETWORK, WEB_SERVER_SCREEN
    await _open(s, MB_NETWORK, n)
    try:
        await s.click(n, NW_WEB_SERVER, "web server settings")
        if w not in s.state.screens:
            msg = "the web server screen did not open"
            raise ItmCommandError(msg)
        await s.send(w, WS_DEFAULT if port is None else WS_CUSTOM)
        if port is not None:
            if s._client.dry_run:
                s._client.planned.append(f"web server port = {port}")
            else:
                await s.answer_number(WS_CHANGE, port, "web server port", screen=w)
        if not await s.press_ok("save web server port", w, WS_OK, WS_CANCEL):
            return False
        return await s.press_ok("save network settings", n, 23, NW_CANCEL)
    finally:
        await _close(s, w, WS_CANCEL)
        await s.close_screen(n)


async def set_remote_management(
    s: _Session, *, connect: bool, allow_remote_change: bool = False
) -> bool:
    """Connect or disconnect remote management (refused unless explicitly allowed)."""
    if not allow_remote_change and not s._client.dry_run:
        msg = "remote management changes access: pass allow_remote_change=True"
        raise ItmCommandError(msg)
    r = MenuScreen.REMOTE_MANAGEMENT
    await _open(s, MB_REMOTE_MANAGEMENT, r)
    try:
        part = RM_CONNECT if connect else RM_DISCONNECT
        if s.state.get(r, part, "ENABLED") == 0:
            msg = "the iTM has remote management connect/disconnect disabled"
            raise ItmCommandError(msg)
        reply = await s.write(
            "remote management " + ("connect" if connect else "disconnect"), r, part
        )
        if reply is None:
            return False
        await s._answer_confirm(r, reply)
        return True
    finally:
        await s.close_screen(r)


# Seen on a real iTM: the login screen (1203) has a second button next to "Login"
# (part 5, "User Settings"); pressed without credentials it answers "Cannot log
# in". Everything else here follows the web UI's script.js and is tested
# against the simulated iTM only: ScMainLogin.onClick sends the same payload (name
# length, name, SHA-256 of the password) for both its buttons, so part 5 logs in
# straight into ScWebUserSetup (1205). Seen on a real iTM: 1205 opens on that login,
# is read and left with Cancel, and its OK saves the icon colour and a new password
# (entered in the same PASSWORD inputs as the user setup's, 1204). Assumed, not
# seen: that its language button (7) opens the language screen (2501).


async def open_own_settings(s: _Session, username: str, password: str) -> None:
    """Log in through the login screen's "User Settings": it opens 1205.

    ``s`` is a fresh session that is not logged in. The credentials are sent like
    the browser sends them (the password only as its SHA-256 digest).

    When the login succeeds but 1205 does not open, the session stays marked as
    logged in, so ``leave_own_settings`` logs it out instead of leaving it taken.

    Raises
    ------
        ItmCommandError: 1205 did not open (wrong credentials, or another session).

    """
    await s.send(
        event=EventType.INIT,
        data_type=DataType.STRING,
        string=s._client.web_version,
    )
    s.token = NO_TOKEN
    digest = hashlib.sha256(password.encode()).hexdigest() if password else ""
    s.logged_in = True
    reply = await s.send(
        ScreenID.LOGIN,
        LOGIN_SETTINGS_BUTTON,
        EventType.CLICK,
        DataType.STRING,
        string=f"{len(username):04d}{username}{digest}",
    )
    if OWN_SCREEN not in s.state.screens:
        dialogs = reply.dialogs()
        if any(
            u.screen == ScreenID.LOGIN and u.update == "DLG_OPEN" for u in reply.screens
        ):
            s.logged_in = False
            await s._store.save(None)
            msg = (
                f"the user's own settings did not open (wrong credentials?): {dialogs}"
            )
        else:
            msg = f"logged in, but the user's own settings did not open: {dialogs}"
        raise ItmCommandError(msg)


def own_settings_from_screen(state: UIState) -> OwnSettings:
    """Read the open own-settings screen (1205); the password shows as asterisks."""
    o = OWN_SCREEN
    language = state.get(o, OWN_LANGUAGE, "TEXT")
    idx = state.get(o, OWN_DATE_FORMAT, "SINGLE_SELIDX")
    colour = _radio(state, o, OWN_COLOUR_1, OWN_COLOUR_2)
    return OwnSettings(
        language=html.unescape(language).strip() if isinstance(language, str) else None,
        date_format=DATE_FORMATS[idx]
        if idx is not None and 0 <= idx < len(DATE_FORMATS)
        else None,
        time_24h=_radio(state, o, OWN_24H, OWN_12H),
        decimal_comma=None
        if (point := _radio(state, o, OWN_POINT, OWN_COMMA)) is None
        else not point,
        icon_colour=None if colour is None else (1 if colour else 2),
    )


async def leave_own_settings(s: _Session) -> None:
    """Leave 1205 with Cancel and make sure the session is over.

    The iTM is expected to end the session then; when it doesn't, log out.
    """
    if not s.logged_in:
        return
    if OWN_SCREEN in s.state.screens:
        try:
            await s.send(OWN_SCREEN, OWN_CANCEL)
        except SessionEnded:
            return
        s.state.screens.pop(OWN_SCREEN, None)
    await s.logout()


async def _enter_own_password(s: _Session, password: str) -> None:
    """Press 1205's password button and enter the new password (dry run: cancel).

    The password goes only into the iTM's PASSWORD input (and its repeat input, when
    the iTM asks for it); it is never logged, and a dry run plans it as ``***``.
    """
    reply = await s.click(OWN_SCREEN, OWN_PASSWORD_BUTTON, "own password")
    dialogs = [
        u for u in reply.screens if u.update == "DLG_OPEN" and u.data[1] == "PASSWORD"
    ]
    if not dialogs:
        msg = f"own password: no password input opened: {reply.dialogs()}"
        raise ItmCommandError(msg)
    d = dialogs[0]
    low, high = d.data[3], d.data[4]
    if not low <= len(password) <= high:
        await s.send(d.screen, d.data[0], EventType.CLICK2, DataType.INT, 0)
        msg = f"the password must be {low}-{high} characters"
        raise ItmCommandError(msg)
    if s._client.dry_run:
        await s.send(d.screen, d.data[0], EventType.CLICK2, DataType.INT, 0)
        s._client.planned.append(
            f"own password = ***: screen {d.screen} input {d.data[0]}"
        )
        return
    await s._answer_password(reply, password)


async def _pick_own_language(s: _Session, code: str) -> None:
    """Open the language screen from 1205, pick a language and press its OK."""
    g = MenuScreen.LANGUAGE
    await s.click(OWN_SCREEN, OWN_LANGUAGE_BUTTON, "own language")
    if g not in s.state.screens:
        msg = "the language screen did not open"
        raise ItmCommandError(msg)
    try:
        await s.send(g, LANGUAGE_RADIOS[code])
        await s.press_ok(f"pick own language {code}", g, LG_OK, CLOSE_PART[g])
    finally:
        if s.logged_in:
            await _close(s, g, CLOSE_PART[g])


async def set_own_settings(  # noqa: PLR0913  # pylint: disable=too-many-arguments
    s: _Session,
    *,
    date_format: str | None = None,
    time_24h: bool | None = None,
    decimal_comma: bool | None = None,
    icon_colour: int | None = None,
    language: str | None = None,
    password: str | None = None,
) -> bool:
    """Change the open own-settings screen (1205) and press OK.

    The picks are made first, then the password and the language, then OK; the
    iTM ends the session once a language change or the settings apply.

    Args:
    ----
        s: A session opened with ``open_own_settings``.
        date_format: One of DATE_FORMATS.
        time_24h: 24-hour (True) or 12-hour time.
        decimal_comma: Decimal comma (True) or point.
        icon_colour: 1 or 2.
        language: A language code (en, de, es, pt, ja, fr, it, nl, zh, ko).
        password: A new password for the logged-in user; never logged or planned.

    Returns:
    -------
        True when saved, False in a dry run.

    Raises:
    ------
        ReadOnlyError: The client is read-only.
        ItmCommandError: A value the iTM doesn't offer, or the iTM refused.

    """
    s._writable()
    o = OWN_SCREEN
    if date_format is not None and date_format not in DATE_FORMATS:
        msg = f"date format must be one of {DATE_FORMATS}"
        raise ItmCommandError(msg)
    if icon_colour is not None and icon_colour not in {1, 2}:
        msg = "icon colour is 1 or 2"
        raise ItmCommandError(msg)
    if language is not None and language not in LANGUAGE_RADIOS:
        msg = f"unknown language {language!r}; expected one of {list(LANGUAGE_RADIOS)}"
        raise ItmCommandError(msg)
    if date_format is not None:
        await s._pick_option(
            o, OWN_DATE_FORMAT, DATE_FORMATS.index(date_format), "date format"
        )
    if time_24h is not None:
        await s.send(o, OWN_24H if time_24h else OWN_12H)
    if decimal_comma is not None:
        await s.send(o, OWN_COMMA if decimal_comma else OWN_POINT)
    if icon_colour is not None:
        await s.send(o, OWN_COLOUR_1 if icon_colour == 1 else OWN_COLOUR_2)
    if password is not None:
        await _enter_own_password(s, password)
    try:
        if language is not None:
            await _pick_own_language(s, language)
        saved = await s.press_ok("save own settings", o, OWN_OK, OWN_CANCEL)
    except SessionEnded:
        # The iTM ends the session once the settings apply (or, in a dry run, when
        # 1205 is cancelled).
        return not s._client.dry_run
    await s.logout()  # the iTM kept the session: end it, like the browser's logout
    return saved


# From script.js (ScSysWebAccessRegistScreen) and the screen inventory; tested against
# the simulated iTM only. Seen on a real iTM: the user setup's (1204) screens button
# (16) and its zones button (12) are disabled for an administrator, so the screens
# apply to normal users only. Assumed: the lists' last column holds the screen's name,
# and 1201's OK only hands the list back to 1204, whose OK saves it.


def _screen_rows(state: UIState, part: int) -> list[str]:
    rows = (state.get(USER_SCREENS, part, "LIST_ROW") or {}).get("rows", [])
    names = []
    for row in rows:
        cells = [html.unescape(str(t)).strip() for t in row.get("text", [])]
        names.append(next((c for c in reversed(cells) if c), ""))
    return names


def web_user_screens_from_screen(state: UIState) -> WebUserScreens:
    """Read the open allowed-screens screen (1201): allowed and available screens."""
    return WebUserScreens(
        allowed=tuple(_screen_rows(state, UA_ALLOWED)),
        available=tuple(_screen_rows(state, UA_AVAILABLE)),
    )


async def _open_user_screens(s: _Session, name: str) -> None:
    """Open a normal web user's setup (1204) and its allowed screens (1201).

    An administrator is refused before its (disabled) screens button is clicked.
    """
    w, u = MenuScreen.WEB_USERS, MenuScreen.USER_SETUP
    await _open(s, MB_WEB_USERS, w)
    user = next((x for x in web_users_from_screen(s.state) if x.name == name), None)
    if user is None:
        msg = f"web user: {name!r} not found"
        raise ItmCommandError(msg)
    admin_msg = f"{name} is an administrator: allowed screens apply to normal users"
    if user.admin:
        raise ItmCommandError(admin_msg)
    await s._select_listed(w, WU_LIST, 0, name, "web user")
    await s.click(w, WU_CHANGE, "change web user")
    if u not in s.state.screens:
        msg = f"{name}: the user setup did not open"
        raise ItmCommandError(msg)
    if (
        s.state.get(u, US_ADMIN, "CHECK_STATE") == 1
        or s.state.get(u, US_SCREENS_BUTTON, "ENABLED") == 0
    ):
        raise ItmCommandError(admin_msg)
    await s.send(u, US_SCREENS_BUTTON)
    if USER_SCREENS not in s.state.screens:
        msg = f"{name}: the allowed screens did not open"
        raise ItmCommandError(msg)


async def _leave_user_screens(s: _Session) -> None:
    await _close(s, USER_SCREENS, UA_CANCEL)
    await s.close_screen(MenuScreen.USER_SETUP)
    await s.close_screen(MenuScreen.WEB_USERS)


async def read_web_user_screens(s: _Session, name: str) -> WebUserScreens:
    """Open a normal web user's allowed screens, read them and cancel."""
    try:
        await _open_user_screens(s, name)
        return web_user_screens_from_screen(s.state)
    finally:
        await _leave_user_screens(s)


async def _move_screen(s: _Session, source: int, button: int, screen: str) -> None:
    """Select a screen in one list and move it to the other (add or remove)."""
    rows = _screen_rows(s.state, source)
    if rows.count(screen) != 1:
        msg = f"screen {screen!r} not found once: {rows}"
        raise ItmCommandError(msg)
    await s.send(
        USER_SCREENS,
        source,
        EventType.CLICK,
        DataType.INTARRAY,
        1,
        array=(rows.index(screen),),
    )
    await s.click(USER_SCREENS, button, f"move screen {screen!r}")


async def set_web_user_screens(
    s: _Session, name: str, screens: tuple[str, ...]
) -> bool:
    """Make a normal web user's allowed screens exactly these; OK on 1201 and 1204.

    Returns
    -------
        True when saved, False in a dry run.

    """
    s._writable()
    try:
        await _open_user_screens(s, name)
        wanted = [x.strip() for x in screens]
        current = web_user_screens_from_screen(s.state)
        offered = set(current.allowed) | set(current.available)
        if missing := [x for x in wanted if x not in offered]:
            msg = f"{name}: screens not offered: {missing}"
            raise ItmCommandError(msg)
        for screen in current.allowed:
            if screen not in wanted:
                await _move_screen(s, UA_ALLOWED, UA_REMOVE, screen)
        for screen in wanted:
            if screen not in _screen_rows(s.state, UA_ALLOWED):
                await _move_screen(s, UA_AVAILABLE, UA_ADD, screen)
        if set(_screen_rows(s.state, UA_ALLOWED)) != set(wanted):
            msg = f"{name}: the allowed screens did not become {wanted}"
            raise ItmCommandError(msg)
        label = f"allowed screens of web user {name}"
        if not await s.press_ok(label, USER_SCREENS, UA_OK, UA_CANCEL):
            return False
        u = MenuScreen.USER_SETUP
        return await s.press_ok(f"save {label}", u, US_OK, CLOSE_PART[u])
    finally:
        await _leave_user_screens(s)


async def _ignore(call: Awaitable[Any]) -> None:
    await call


class SystemMixin:  # pylint: disable=too-many-public-methods
    """ItmClient's e-mail, confirm dialog, web server and remote management writes."""

    if TYPE_CHECKING:
        # pylint: disable=unused-argument
        dry_run: bool

        async def _in_session(
            self, call: Callable[[_Session], Awaitable[_T]]
        ) -> _T: ...

        async def _system_write(
            self, call: Callable[[_Session], Awaitable[None]]
        ) -> None: ...

        def _writable(self) -> None: ...

        def _experimental(self, what: str) -> None: ...

        _username: str
        _password: str

        _own_session: Callable[[], AbstractAsyncContextManager[_Session]]

        async def fetch_info(self) -> ItmInfo:
            """Read the version, region and time screens."""

        async def fetch_system(
            self, only: Iterable[str] | None = None
        ) -> SystemSettings:
            """Read the system settings."""

    async def _email(self) -> EmailSettings:
        email = (await self.fetch_system(["email"])).email
        if email is None:
            msg = "the e-mail settings screen did not open"
            raise ItmCommandError(msg)
        return email

    async def add_email_address(
        self, address: str, points: Iterable[str]
    ) -> EmailSettings | None:
        """Add an address that gets the iTM's error mails for these points.

        Args:
        ----
            address: The e-mail address.
            points: The management points it reports on.

        Returns:
        -------
            The e-mail settings as read back, or None in a dry run.

        """
        chosen = tuple(points)
        await self._system_write(
            lambda s: _ignore(add_email_address(s, address, chosen))
        )
        if self.dry_run:
            return None
        email = await self._email()
        if address not in [a for _, a in email.addresses]:
            msg = f"{address}: the iTM did not add the address"
            raise ItmCommandError(msg)
        return email

    async def edit_email_address(
        self,
        address: str,
        *,
        new_address: str | None = None,
        points: Iterable[str] | None = None,
    ) -> EmailSettings | None:
        """Change an e-mail address and/or the points it reports on.

        Args:
        ----
            address: The address as listed.
            new_address: Its new address.
            points: The points it should report on (replacing the current ones).

        Returns:
        -------
            The e-mail settings as read back, or None in a dry run.

        """
        chosen = None if points is None else tuple(points)
        await self._system_write(
            lambda s: _ignore(
                edit_email_address(s, address, new_address=new_address, points=chosen)
            )
        )
        if self.dry_run:
            return None
        email = await self._email()
        if (new_address or address) not in [a for _, a in email.addresses]:
            msg = f"{address}: the iTM did not take the change"
            raise ItmCommandError(msg)
        return email

    async def delete_email_address(self, address: str) -> None:
        """Delete an e-mail address.

        Args:
        ----
            address: The address as listed.

        """
        await self._system_write(lambda s: _ignore(delete_email_address(s, address)))
        if not self.dry_run and address in [
            a for _, a in (await self._email()).addresses
        ]:
            msg = f"{address}: the iTM did not delete the address"
            raise ItmCommandError(msg)

    async def set_email_enabled(self, *, enabled: bool) -> EmailSettings | None:
        """Switch the e-mail notification on or off.

        Args:
        ----
            enabled: On (True) or off.

        Returns:
        -------
            The e-mail settings as read back, or None in a dry run.

        """
        await self._system_write(
            lambda s: _ignore(set_email_enabled(s, enabled=enabled))
        )
        if self.dry_run:
            return None
        email = await self._email()
        if email.enabled is not enabled:
            msg = f"the iTM did not switch e-mail {'on' if enabled else 'off'}"
            raise ItmCommandError(msg)
        return email

    async def fetch_email_server(self) -> dict[str, Any]:
        """Read the e-mail server settings (never the password).

        Returns
        -------
            The settings by name, as the server screen shows them.

        """
        return await self._in_session(read_email_server)

    async def set_email_server(self, server: EmailServer) -> dict[str, Any] | None:
        """Change the e-mail server settings; the password is never logged.

        Args:
        ----
            server: The fields to change (None leaves a field as it is).

        Returns:
        -------
            The server settings as read back, or None in a dry run.

        """
        await self._system_write(lambda s: _ignore(set_email_server(s, server)))
        if self.dry_run:
            return None
        result = await self.fetch_email_server()
        wanted = {
            k: v
            for k, v in vars(server).items()
            if v is not None and k != "password" and k in result
        }
        wrong = {k: result[k] for k, v in wanted.items() if result[k] != v}
        if wrong:
            msg = f"the iTM did not take the e-mail server settings: {wrong}"
            raise ItmCommandError(msg)
        return result

    async def send_test_email(self, address: str, *, allow_send: bool = False) -> None:
        """Let the iTM send its test mail to an address (it really sends one).

        Args:
        ----
            address: The address as listed.
            allow_send: Really send it; otherwise only a dry run is allowed.

        """
        self._experimental("send a test e-mail")
        self._writable()
        await self._in_session(
            lambda s: _ignore(send_test_email(s, address, allow_send=allow_send))
        )

    async def set_confirm_dialogs(self, *, enabled: bool) -> bool | None:
        """Switch the iTM's "confirm settings" dialogs on or off.

        Args:
        ----
            enabled: On (True) or off.

        Returns:
        -------
            The setting as read back, or None in a dry run.

        """
        await self._system_write(
            lambda s: _ignore(set_confirm_dialogs(s, enabled=enabled))
        )
        if self.dry_run:
            return None
        shown = (await self.fetch_system(["confirm_dialogs"])).confirm_dialogs
        if shown is not enabled:
            msg = f"the iTM did not switch confirm dialogs {'on' if enabled else 'off'}"
            raise ItmCommandError(msg)
        return shown

    async def set_web_server_port(
        self, port: int | None, *, allow_network_change: bool = False
    ) -> None:
        """Set the web server's port (None: the default). Can cut off access.

        Args:
        ----
            port: The port, or None for the default.
            allow_network_change: Really change it; otherwise only a dry run.

        """
        self._experimental("change the web server port")
        await self._system_write(
            lambda s: _ignore(
                set_web_server_port(s, port, allow_network_change=allow_network_change)
            )
        )

    async def set_remote_management(
        self, *, connect: bool, allow_remote_change: bool = False
    ) -> None:
        """Connect or disconnect the iTM's remote management.

        Args:
        ----
            connect: Connect (True) or disconnect.
            allow_remote_change: Really do it; otherwise only a dry run.

        """
        self._experimental("change the remote management connection")
        await self._system_write(
            lambda s: _ignore(
                set_remote_management(
                    s, connect=connect, allow_remote_change=allow_remote_change
                )
            )
        )

    def _not_own_account(self, name: str) -> None:
        if name == self._username:
            msg = "refusing to change the account this client logs in with"
            raise ItmCommandError(msg)

    async def _web_user(self, name: str) -> WebUser | None:
        users = (await self.fetch_system(["web_users"])).web_users or ()
        return next((u for u in users if u.name == name), None)

    async def create_web_user(
        self,
        name: str,
        password: str,
        *,
        zones: tuple[str, ...],
        admin: bool = False,
    ) -> WebUser | None:
        """Add a web access user.

        Args:
        ----
            name: The user name (1-15 characters).
            password: Its password (4-15 characters); never logged.
            zones: The zones the user manages (the iTM wants at least one).
            admin: Administrator (True) or user.

        Returns:
        -------
            The user as read back, or None in a dry run.

        """
        self._not_own_account(name)
        if not zones:
            msg = "a web user needs at least one zone"
            raise ItmCommandError(msg)
        await self._system_write(
            lambda s: s.save_web_user(
                name, new=True, password=password, admin=admin, zones=tuple(zones)
            )
        )
        if self.dry_run:
            return None
        user = await self._web_user(name)
        if user is None or user.admin is not admin:
            msg = f"{name}: the iTM did not add the user: {user}"
            raise ItmCommandError(msg)
        return user

    async def set_web_user(
        self,
        name: str,
        *,
        password: str | None = None,
        admin: bool | None = None,
        zones: tuple[str, ...] | None = None,
    ) -> WebUser | None:
        """Change a web user's password and/or type (never the client's own).

        Args:
        ----
            name: The user.
            password: A new password (4-15 characters); never logged.
            admin: Administrator (True) or user.
            zones: The zones the user manages, replacing the current ones.

        Returns:
        -------
            The user as read back, or None in a dry run.

        """
        self._not_own_account(name)
        if password is None and admin is None and not zones:
            msg = "nothing to change"
            raise ItmCommandError(msg)
        await self._system_write(
            lambda s: s.save_web_user(
                name, new=False, password=password, admin=admin, zones=zones or None
            )
        )
        if self.dry_run:
            return None
        user = await self._web_user(name)
        if user is None or (admin is not None and user.admin is not admin):
            msg = f"{name}: the iTM did not take the change: {user}"
            raise ItmCommandError(msg)
        return user

    async def delete_web_user(self, name: str) -> None:
        """Delete a web user (never the client's own).

        Args:
        ----
            name: The user.

        """
        self._not_own_account(name)
        await self._system_write(lambda s: s.delete_web_user(name))
        if not self.dry_run and await self._web_user(name) is not None:
            msg = f"{name}: the iTM did not delete the user"
            raise ItmCommandError(msg)

    async def fetch_web_user_screens(self, name: str) -> WebUserScreens:
        """Read the screens a normal web user may open (an administrator is refused).

        Args:
        ----
            name: The user.

        Returns:
        -------
            The allowed screens and the ones it could get.

        """
        return await self._in_session(lambda s: read_web_user_screens(s, name))

    async def set_web_user_screens(
        self, name: str, screens: Iterable[str]
    ) -> WebUserScreens | None:
        """Make the screens a normal web user may open exactly these.

        Args:
        ----
            name: The user (not an administrator, not the client's own account).
            screens: The screen names, as the iTM lists them.

        Returns:
        -------
            The screens as read back, or None in a dry run.

        """
        self._experimental("change a web user's allowed screens")
        self._not_own_account(name)
        wanted = tuple(screens)
        await self._system_write(
            lambda s: _ignore(set_web_user_screens(s, name, wanted))
        )
        if self.dry_run:
            return None
        result = await self.fetch_web_user_screens(name)
        if set(result.allowed) != {x.strip() for x in wanted}:
            msg = f"{name}: the iTM did not take the screens {wanted}: {result}"
            raise ItmCommandError(msg)
        return result

    async def fetch_own_settings(self) -> OwnSettings:
        """Read the logged-in web user's own settings (screen 1205).

        Returns
        -------
            Language (as shown), date format, 12/24 hours, decimal separator and
            icon colour.

        """
        async with self._own_session() as s:
            return own_settings_from_screen(s.state)

    async def set_own_settings(  # noqa: PLR0913  # pylint: disable=too-many-arguments,too-many-locals
        self,
        *,
        date_format: str | None = None,
        time_24h: bool | None = None,
        decimal_comma: bool | None = None,
        icon_colour: int | None = None,
        language: str | None = None,
        password: str | None = None,
        allow_password_change: bool = False,
    ) -> OwnSettings | None:
        """Change the logged-in web user's own settings (screen 1205).

        The iTM ends the session when they apply. A new password is the one this
        client logs in with from then on; it is sent once and never logged. It
        needs ``allow_password_change`` (a dry run is always allowed). When the
        read-back fails, the client goes back to the old password only if that
        one still logs in.

        Args:
        ----
            date_format: One of DATE_FORMATS.
            time_24h: 24-hour (True) or 12-hour time.
            decimal_comma: Decimal comma (True) or point.
            icon_colour: 1 or 2.
            language: en, de, es, pt, ja, fr, it, nl, zh or ko.
            password: A new password for the client's own account.
            allow_password_change: Really change the password.

        Returns:
        -------
            The settings as read back (logging in with the new password), or None
            in a dry run.

        Raises:
        ------
            ItmCommandError: Not allowed, nothing to change, or the read-back failed.

        """
        for name, value in (
            ("language", language),
            ("date format", date_format),
            ("time format", time_24h),
            ("decimal sign", decimal_comma),
        ):
            if value is not None:
                self._experimental(f"change the own {name}")
        self._writable()
        if password is not None and not allow_password_change and not self.dry_run:
            msg = (
                "changing the client's own password can lock it out: "
                "pass allow_password_change=True"
            )
            raise ItmCommandError(msg)
        changes: dict[str, Any] = {
            "date_format": date_format,
            "time_24h": time_24h,
            "decimal_comma": decimal_comma,
            "icon_colour": icon_colour,
        }
        if all(v is None for v in (*changes.values(), language, password)):
            msg = "nothing to change"
            raise ItmCommandError(msg)
        async with self._own_session() as s:
            await set_own_settings(
                s,
                date_format=date_format,
                time_24h=time_24h,
                decimal_comma=decimal_comma,
                icon_colour=icon_colour,
                language=language,
                password=password,
            )
        if self.dry_run:
            return None
        old_password = self._password
        if password is not None:
            self._password = password
        try:
            result = await self.fetch_own_settings()
            code = await self.fetch_language() if language is not None else None
        except (ItmCommandError, LoginRejected) as err:
            msg = "could not log in again after changing the own settings"
            if password is not None:
                if await self._logs_in_with(old_password):
                    msg += "; the iTM kept the old password, which the client uses"
                else:
                    self._password = password
                    msg += "; the client keeps the new password"
            raise ItmCommandError(msg) from err
        wrong = {
            k: getattr(result, k)
            for k, v in changes.items()
            if v is not None and getattr(result, k) != v
        }
        if language is not None and code != language:
            wrong["language"] = code
        if wrong:
            msg = f"the iTM did not take the own settings: {wrong}"
            raise ItmCommandError(msg)
        return result

    async def _logs_in_with(self, password: str) -> bool:
        """Switch to a password if a login with it works; else keep the current one."""
        current, self._password = self._password, password
        try:
            await self.fetch_language()
        except (ItmCommandError, LoginRejected):
            self._password = current
            return False
        return True

    async def fetch_language(self) -> str | None:
        """Read the display language as a code (en, de, es, pt, ja, fr, it, nl, zh, ko).

        Returns
        -------
            The code, or None when not readable.

        """
        return await self._in_session(lambda s: s.read_language())

    async def set_language(self, code: str) -> str | None:
        """Switch the iTM's display language (all labels it sends change).

        The language is a setting of the web user the client logs in with; the iTM
        ends that session when it changes, so the result is read in a new one.

        Args:
        ----
            code: en, de, es, pt, ja, fr, it, nl, zh or ko.

        Returns:
        -------
            The language as read back, or None in a dry run.

        """
        if code not in LANGUAGE_RADIOS:
            msg = f"unknown language {code!r}; expected one of {list(LANGUAGE_RADIOS)}"
            raise ItmCommandError(msg)
        try:
            await self._system_write(lambda s: s.set_language(code))
        except SessionEnded:
            # The language belongs to the logged-in web user: once it changes, the
            # iTM ends that user's session ("web user deleted or changed").
            if self.dry_run:
                raise
        if self.dry_run:
            return None
        result = await self.fetch_language()
        if result != code:
            msg = f"the iTM did not switch to {code}: {result}"
            raise ItmCommandError(msg)
        return result

    async def set_region(
        self,
        *,
        date_format: str | None = None,
        time_24h: bool | None = None,
        temperature_unit: str | None = None,
        decimal_separator: str | None = None,
    ) -> ItmInfo | None:
        """Change how the iTM shows dates, times, temperatures and decimals.

        Like the language, these belong to the logged-in web user; the library
        reads every format, but a switch to °F also changes the setpoints a unit
        offers. Read back in a fresh session.

        Args:
        ----
            date_format: One of DATE_FORMATS ("%d/%m/%Y", "%m/%d/%Y", "%Y/%m/%d").
            time_24h: 24-hour (True) or 12-hour time.
            temperature_unit: "°C" or "°F".
            decimal_separator: "." or ",".

        Returns:
        -------
            The info as read back, or None in a dry run.

        """
        changes: dict[str, Any] = {
            k: v
            for k, v in (
                ("date_format", date_format),
                ("time_24h", time_24h),
                ("temperature_unit", temperature_unit),
                ("decimal_separator", decimal_separator),
            )
            if v is not None
        }
        if not changes:
            msg = "nothing to change"
            raise ItmCommandError(msg)
        allowed = {
            "date_format": DATE_FORMATS,
            "temperature_unit": ("°C", "°F"),
            "decimal_separator": (".", ","),
        }
        for key, options in allowed.items():
            if key in changes and changes[key] not in options:
                msg = f"{key}: {changes[key]!r}, expected one of {options}"
                raise ItmCommandError(msg)
        experimental = [
            k for k in ("temperature_unit", "decimal_separator") if k in changes
        ]
        if experimental:
            self._experimental(f"set the region's {' and '.join(experimental)}")
        try:
            await self._system_write(lambda s: s.set_region(changes))
        except SessionEnded:
            if self.dry_run:  # the iTM may end the user's session, like the language
                raise
        if self.dry_run:
            return None
        info = await self.fetch_info()
        wrong = {
            k: getattr(info, k) for k, v in changes.items() if getattr(info, k) != v
        }
        if wrong:
            msg = f"the iTM did not take the region settings {changes}: {wrong}"
            raise ItmCommandError(msg)
        return info

    async def fetch_dst(self) -> DstSettings:
        """Read whether the iTM switches to summer time, and its rule.

        Returns
        -------
            The settings.

        """
        return await self._in_session(lambda s: s.read_dst())

    async def set_dst(
        self,
        *,
        enabled: bool = True,
        start: DstRule = EU_DST_START,
        end: DstRule = EU_DST_END,
    ) -> DstSettings | None:
        """Switch summer time on (default: the EU rule) or off.

        Set this before the clock: switching it may move the iTM's clock by an hour.

        Args:
        ----
            enabled: Summer time on or off.
            start: When it starts (ignored when off).
            end: When it ends (ignored when off).

        Returns:
        -------
            The settings as read back, or None in a dry run.

        """
        await self._system_write(
            lambda s: s.set_dst(enabled=enabled, start=start, end=end)
        )
        if self.dry_run:
            return None
        result = await self.fetch_dst()
        ok = result.enabled is enabled and (
            not enabled or (result.start == start and result.end == end)
        )
        if not ok:
            msg = f"the iTM did not take the summer time settings: {result}"
            raise ItmCommandError(msg)
        return result

    async def set_clock(
        self, when: datetime | None = None, *, tolerance: float = 120
    ) -> ItmInfo | None:
        """Set the iTM's clock (default: this computer's local time, now).

        Args:
        ----
            when: The local date and time to set.
            tolerance: How far (seconds) the read-back may differ.

        Returns:
        -------
            The info as read back, or None in a dry run.

        """
        target = when or datetime.now()  # noqa: DTZ005 - the iTM keeps local time
        await self._system_write(lambda s: s.set_clock(target))
        if self.dry_run:
            return None
        info = await self.fetch_info()
        if info.clock is None:
            msg = "the iTM's clock could not be read back"
            raise ItmCommandError(msg)
        expected = target if when is not None else datetime.now()  # noqa: DTZ005
        if abs((info.clock - expected).total_seconds()) > tolerance:
            msg = f"the iTM's clock reads {info.clock}, expected about {expected}"
            raise ItmCommandError(msg)
        return info
