# ruff: noqa: SLF001
# pylint: disable=protected-access
"""Writes for e-mail, confirm dialogs, web server, remote management.

Session-level functions: each takes an open ``_Session``. Every saving request goes
through ``s.write`` / ``s.press_ok``, so a dry run plans it instead of sending it.
Writes that can cut access (web server port, remote management) or that reach the
outside world (the test e-mail) refuse unless explicitly allowed. Passwords are never
logged and never put into a dry run's plan.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

from . import zones_edit
from ._bases import ClientCore, SessionCore
from ._util import ignore as _ignore
from .exceptions import ItmCommandError, SessionEnded
from .menu import (
    CLOSE_PART,
    DATE_FORMATS,
    MB_REGION,
    MB_TIME,
    R_12H,
    R_24H,
    R_CELSIUS,
    R_DATE_FORMAT,
    R_DECIMAL_COMMA,
    R_DECIMAL_POINT,
    R_FAHRENHEIT,
    MenuScreen,
)
from .protocol import DataType, EventType
from .system import (
    BUZZER_DURATIONS,
    EU_DST_END,
    EU_DST_START,
    HW_BACKLIGHT,
    HW_BUZZER_DURATION,
    HW_BUZZER_VOLUME,
    HW_KEY_VOLUME,
    HW_OK,
    LANGUAGE_RADIOS,
    LG_OK,
    MB_CONFIRM_DIALOGS,
    MB_EMAIL,
    MB_HARDWARE,
    MB_MAINTENANCE,
    MB_NETWORK,
    MB_POINTS,
    MB_REMOTE_MANAGEMENT,
    MB_SCREENSAVER,
    MB_ZONES,
    MT_ADD,
    MT_AVAILABLE,
    MT_IN_MAINTENANCE,
    MT_OK,
    MT_REMOVE,
    NW_BUTTONS,
    NW_OK,
    NW_TEXT_FIELDS,
    PA_INFO_BUTTON,
    PA_NAME_BUTTON,
    PA_OK,
    PT_CHANGE,
    PT_LIST,
    RG_LANGUAGE_BUTTON,
    RG_OK,
    SS_DELAY_BUTTON,
    SS_MODE,
    SS_OFF_ON_ERROR,
    SS_OK,
    SS_ON_ON_ERROR,
    TM_CHANGE,
    TM_DST_END,
    TM_DST_OFF,
    TM_DST_ON,
    TM_DST_START,
    TM_OK,
    ZA_INFO_BUTTON,
    ZA_NAME_BUTTON,
    ZA_OK,
    ZA_START_BUTTON,
    ZA_STOP_BUTTON,
    ZN_LIST,
    ZN_SETTINGS,
    ScreensaverMode,
    dst_from_screen,
    language_from_screen,
    row_index,
    zone_attributes_from_screen,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .menu import ItmInfo
    from .protocol import Reply
    from .session import _Session
    from .state import UIState
    from .system import (
        DstRule,
        DstSettings,
        EmailSettings,
        ZoneAttributes,
    )


# E-mail notification (1300), address (1301), test mail (1302), server (1303).
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


class SystemMixin(ClientCore):  # pylint: disable=too-many-public-methods
    """ItmClient's e-mail, confirm dialog, web server and remote management writes."""

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


class SystemSessionMixin(SessionCore):  # pylint: disable=too-many-public-methods
    """_Session's system screen reads and writes."""

    async def _pick_option(
        self, screen: int, part: int, index: int | None, what: str
    ) -> None:
        """Pick a combo option by index, refusing one the iTM doesn't offer."""
        options = self.state.get(screen, part, "COMBO_STRS") or []
        if index is None or not 0 <= index < len(options):
            msg = f"{what}: not offered: {options}"
            raise ItmCommandError(msg)
        await self.send(screen, part, EventType.CLICK, DataType.INT, index)

    def _option_index(self, screen: int, part: int, value: int) -> int | None:
        options = [
            str(o).strip() for o in self.state.get(screen, part, "COMBO_STRS") or []
        ]
        return options.index(str(value)) if str(value) in options else None

    async def set_hardware(self, changes: dict[str, Any]) -> None:
        """Pick backlight, buzzer and key click settings and press OK."""
        s = MenuScreen.HARDWARE
        if not await self.open_menu_screen(MB_HARDWARE, s):
            msg = "the hardware settings screen did not open"
            raise ItmCommandError(msg)
        try:
            for key, part in (
                ("backlight", HW_BACKLIGHT),
                ("buzzer_volume", HW_BUZZER_VOLUME),
                ("key_click_volume", HW_KEY_VOLUME),
            ):
                if key in changes:
                    index = self._option_index(s, part, changes[key])
                    await self._pick_option(s, part, index, f"{key} {changes[key]}")
            if "buzzer_minutes" in changes:
                minutes = changes["buzzer_minutes"]
                index = (
                    BUZZER_DURATIONS.index(minutes)
                    if minutes in BUZZER_DURATIONS
                    else None
                )
                await self._pick_option(
                    s, HW_BUZZER_DURATION, index, f"buzzer duration {minutes}"
                )
            await self.press_ok("save hardware settings", s, HW_OK, CLOSE_PART[s])
        finally:
            await self.close_screen(s)

    async def set_screensaver(self, changes: dict[str, Any]) -> None:
        """Pick the screensaver mode, delay and error behaviour and press OK."""
        s = MenuScreen.SCREENSAVER
        if not await self.open_menu_screen(MB_SCREENSAVER, s):
            msg = "the screensaver screen did not open"
            raise ItmCommandError(msg)
        try:
            if "mode" in changes:
                modes = list(ScreensaverMode)
                await self._pick_option(
                    s, SS_MODE, modes.index(changes["mode"]), f"mode {changes['mode']}"
                )
            if "delay_minutes" in changes:
                await self.answer_number(
                    SS_DELAY_BUTTON,
                    changes["delay_minutes"],
                    "screensaver delay",
                    screen=s,
                )
            if "off_on_error" in changes:
                await self.send(
                    s, SS_OFF_ON_ERROR if changes["off_on_error"] else SS_ON_ON_ERROR
                )
            await self.press_ok("save screensaver settings", s, SS_OK, CLOSE_PART[s])
        finally:
            await self.close_screen(s)

    async def _open_system_screen(self, button: int, screen: MenuScreen) -> None:
        if not await self.open_menu_screen(button, screen):
            msg = f"the {screen.name.lower()} screen did not open"
            raise ItmCommandError(msg)

    async def read_zone_attributes(self, zone: str) -> ZoneAttributes:
        """Open a zone's settings screen, read it and cancel."""
        s, a = MenuScreen.ZONES, MenuScreen.ZONE_SETTINGS
        await self._open_system_screen(MB_ZONES, s)
        try:
            await self._select_listed(s, ZN_LIST, 0, zone, "zone")
            await self.click(s, ZN_SETTINGS, "zone")
            return zone_attributes_from_screen(self.state)
        finally:
            await self.close_screen(a)
            await self.close_screen(s)

    async def read_dst(self) -> DstSettings:
        """Read the time screen's summer time switch and rule."""
        await self._open_system_screen(MB_TIME, MenuScreen.TIME)
        try:
            return dst_from_screen(self.state)
        finally:
            await self.close_screen(MenuScreen.TIME)

    async def _network_input(self, field: str, value: str) -> None:
        """Open a network field's input, check it, and enter value (or plan it)."""
        n = MenuScreen.NETWORK
        text = field in NW_TEXT_FIELDS
        kind = "CHARINPUT" if text else "IPADDR"
        reply = await self.click(n, NW_BUTTONS[field], f"network {field}")
        dialogs = [
            u for u in reply.screens if u.update == "DLG_OPEN" and u.data[1] == kind
        ]
        if not dialogs:
            msg = f"network {field}: no {kind} input opened: {reply.dialogs()}"
            raise ItmCommandError(msg)
        d = dialogs[0]
        if text and not d.data[4] <= len(value) <= d.data[5]:
            await self.send(d.screen, d.data[0], EventType.CLICK2)
            msg = f"network {field}: {value!r} must be {d.data[4]}-{d.data[5]} long"
            raise ItmCommandError(msg)
        data_type = DataType.STRING if text else DataType.IPADDRESS
        if self._client.dry_run:
            # Not even into the input: a dry run of a network change sends no value.
            await self.send(d.screen, d.data[0], EventType.CLICK2)
            self._client.planned.append(
                f"network {field} = {value}: screen {n} part {d.data[0]} "
                f"event {EventType.CLICK} type {data_type}"
            )
            return
        reply = await self.send(
            d.screen, d.data[0], EventType.CLICK, data_type, string=value
        )
        if reply.dialogs():
            msg = f"network {field}: the iTM refused {value!r}: {reply.dialogs()}"
            raise ItmCommandError(msg)

    async def set_network(self, changes: dict[str, str]) -> None:
        """Enter the network changes and press OK (in a dry run: plan, Cancel)."""
        n = MenuScreen.NETWORK
        await self._open_system_screen(MB_NETWORK, n)
        try:
            for field, value in changes.items():
                await self._network_input(field, value)
            await self.press_ok("save network settings", n, NW_OK, CLOSE_PART[n])
        finally:
            await self.close_screen(n)

    async def set_maintenance(self, point: str, *, on: bool) -> None:
        """Move a point into (or out of) the maintenance list and press OK."""
        s = MenuScreen.MAINTENANCE
        await self._open_system_screen(MB_MAINTENANCE, s)
        try:
            source, button = (
                (MT_AVAILABLE, MT_ADD) if on else (MT_IN_MAINTENANCE, MT_REMOVE)
            )
            target = MT_IN_MAINTENANCE if on else MT_AVAILABLE
            if row_index(self.state, s, target, 1, point) is not None:
                return
            await self._select_listed(s, source, 1, point, "maintenance")
            await self.click(s, button, "maintenance")
            verb = "into" if on else "out of"
            await self.press_ok(
                f"put {point} {verb} maintenance", s, MT_OK, CLOSE_PART[s]
            )
        finally:
            await self.close_screen(s)

    async def set_point_attributes(
        self, point: str, *, name: str | None, info: str | None
    ) -> None:
        """Change a management point's name and/or description, then OK."""
        s, a = MenuScreen.POINTS, MenuScreen.POINT_SETTINGS
        await self._open_system_screen(MB_POINTS, s)
        try:
            await self._select_listed(s, PT_LIST, 1, point, "point")
            await self.click(s, PT_CHANGE, "point")
            if a not in self.state.screens:
                msg = f"{point}: the point settings did not open"
                raise ItmCommandError(msg)
            if name is not None:
                await self.answer_text(a, PA_NAME_BUTTON, name, f"{point} name")
            if info is not None:
                await self.answer_text(a, PA_INFO_BUTTON, info, f"{point} description")
            await self.press_ok(f"save point {point}", a, PA_OK, CLOSE_PART[a])
        finally:
            await self.close_screen(a)
            await self.close_screen(s)

    async def set_zone_attributes(
        self,
        zone: str,
        *,
        name: str | None,
        info: str | None,
        start_interval: int | None,
        stop_interval: int | None,
    ) -> None:
        """Change a zone's name, description and/or intervals, then OK."""
        s, a = MenuScreen.ZONES, MenuScreen.ZONE_SETTINGS
        await self._open_system_screen(MB_ZONES, s)
        try:
            await self._select_listed(s, ZN_LIST, 0, zone, "zone")
            await self.click(s, ZN_SETTINGS, "zone")
            if a not in self.state.screens:
                msg = f"{zone}: the zone settings did not open"
                raise ItmCommandError(msg)
            if name is not None:
                await self.answer_text(a, ZA_NAME_BUTTON, name, f"{zone} name")
            if info is not None:
                await self.answer_text(a, ZA_INFO_BUTTON, info, f"{zone} description")
            for button, value, what in (
                (ZA_START_BUTTON, start_interval, "start interval"),
                (ZA_STOP_BUTTON, stop_interval, "stop interval"),
            ):
                if value is not None:
                    await self.answer_number(button, value, f"{zone} {what}", screen=a)
            async with zones_edit.zone_errors(cast("_Session", self)):
                await self.press_ok(f"save zone {zone}", a, ZA_OK, CLOSE_PART[a])
        finally:
            await self.close_screen(a)
            await self.close_screen(s)

    async def read_language(self) -> str | None:
        """Open the language screen from the region screen and read the language."""
        r, g = MenuScreen.REGION, MenuScreen.LANGUAGE
        await self._open_system_screen(MB_REGION, r)
        try:
            await self.send(r, RG_LANGUAGE_BUTTON)
            return language_from_screen(self.state)
        finally:
            await self.close_screen(g)
            await self.close_screen(r)

    async def set_language(self, code: str) -> None:
        """Pick a display language: language screen OK, then region screen OK."""
        r, g = MenuScreen.REGION, MenuScreen.LANGUAGE
        await self._open_system_screen(MB_REGION, r)
        try:
            await self.send(r, RG_LANGUAGE_BUTTON)
            if g not in self.state.screens:
                msg = "the language screen did not open"
                raise ItmCommandError(msg)
            await self.send(g, LANGUAGE_RADIOS[code])
            await self.press_ok(f"pick language {code}", g, LG_OK, CLOSE_PART[g])
            await self.press_ok(f"save language {code}", r, RG_OK, CLOSE_PART[r])
        finally:
            await self.close_screen(g)
            await self.close_screen(r)

    async def _pick_dst_rule(self, parts: tuple[int, ...], rule: DstRule) -> None:
        """Pick a summer time rule's month, week, weekday and hour."""
        t = MenuScreen.TIME
        month, week, weekday, hour_part = parts
        hours = [_hour(o) for o in self.state.get(t, hour_part, "COMBO_STRS") or []]
        picks = (
            (month, rule.month - 1),
            (week, rule.week - 1),
            (weekday, rule.weekday),
            (hour_part, hours.index(rule.hour) if rule.hour in hours else None),
        )
        for part, index in picks:
            if self.state.get(t, part, "ENABLED") == 0:
                msg = "summer time: the rule is disabled on the iTM"
                raise ItmCommandError(msg)
            await self._pick_option(t, part, index, f"summer time {rule}")

    async def set_region(self, changes: dict[str, Any]) -> None:
        """Pick date format, 12/24 hours, °C/°F and decimal separator, then OK."""
        r = MenuScreen.REGION
        await self._open_system_screen(MB_REGION, r)
        try:
            if (fmt := changes.get("date_format")) is not None:
                await self._pick_option(
                    r, R_DATE_FORMAT, DATE_FORMATS.index(fmt), f"date format {fmt}"
                )
            for key, (first, second), values in (
                ("time_24h", (R_24H, R_12H), (True, False)),
                ("temperature_unit", (R_CELSIUS, R_FAHRENHEIT), ("°C", "°F")),
                ("decimal_separator", (R_DECIMAL_POINT, R_DECIMAL_COMMA), (".", ",")),
            ):
                if (value := changes.get(key)) is not None:
                    await self.send(r, first if value == values[0] else second)
            await self.press_ok("save region settings", r, RG_OK, CLOSE_PART[r])
        finally:
            await self.close_screen(r)

    async def set_dst(self, *, enabled: bool, start: DstRule, end: DstRule) -> None:
        """Switch summer time on (with its rule) or off, then OK."""
        t = MenuScreen.TIME
        await self._open_system_screen(MB_TIME, t)
        try:
            await self.send(t, TM_DST_ON if enabled else TM_DST_OFF)
            if enabled:
                await self._pick_dst_rule(TM_DST_START, start)
                await self._pick_dst_rule(TM_DST_END, end)
            await self.press_ok("save summer time", t, TM_OK, CLOSE_PART[t])
        finally:
            await self.close_screen(t)

    async def set_clock(self, when: datetime) -> None:
        """Enter a date and time in the clock's input, then OK."""
        t = MenuScreen.TIME
        await self._open_system_screen(MB_TIME, t)
        try:
            reply = await self.send(t, TM_CHANGE)
            dialogs = [
                u
                for u in reply.screens
                if u.update == "DLG_OPEN" and u.data[1] == "TIME"
            ]
            if not dialogs:
                msg = f"the clock input did not open: {reply.dialogs()}"
                raise ItmCommandError(msg)
            d = dialogs[0]
            value = (
                when.year,
                when.month - 1,
                when.day,
                when.hour,
                when.minute,
                when.second,
            )
            low, high = tuple(d.data[10:16]), tuple(d.data[16:22])
            if not low <= value <= high:
                await self.send(d.screen, d.data[0], EventType.CLICK2)
                msg = f"the clock: {when} outside the iTM's range"
                raise ItmCommandError(msg)
            await self.send(
                d.screen, d.data[0], EventType.CLICK, DataType.TIME, 6, array=value
            )
            await self.press_ok("save the clock", t, TM_OK, CLOSE_PART[t])
        finally:
            await self.close_screen(t)


def _hour(option: str) -> int | None:
    m = re.match(r"\s*(\d+)", str(option))
    return int(m.group(1)) if m else None
