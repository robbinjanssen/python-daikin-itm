# ruff: noqa: SLF001
# pylint: disable=protected-access
"""Writes for web users and the logged-in user's own settings.

The logged-in user's own settings are screen 1205 (password, language and region
settings); a normal web user's allowed screens are screen 1201. The own settings
take a fresh session, see ``open_own_settings``.
"""

from __future__ import annotations

import hashlib
import html
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ._bases import ClientCore, SessionCore
from ._screens import clean as _clean
from ._screens import close as _close
from ._screens import open_below as _open_below
from ._screens import open_screen as _open
from ._screens import radio as _radio
from ._util import ignore as _ignore
from .exceptions import ItmCommandError, LoginRejected, SessionEnded
from .menu import CLOSE_PART, DATE_FORMATS, MenuScreen
from .protocol import NO_TOKEN, DataType, EventType, ScreenID
from .system import (
    LANGUAGE_NAMES,
    LANGUAGE_RADIOS,
    LG_OK,
    MB_WEB_USERS,
    US_ADMIN,
    US_NAME_BUTTON,
    US_OK,
    US_PASSWORD_BUTTON,
    US_USER,
    US_ZONES_BUTTON,
    UZ_LIST,
    UZ_OK,
    WU_ADD,
    WU_CHANGE,
    WU_DELETE,
    WU_LIST,
    language_from_screen,
    row_index,
    web_users_from_screen,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from .protocol import Reply
    from .session import _Session
    from .state import UIState
    from .system import WebUser


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
# A normal web user's allowed screens (1201), from the user setup's (1204) button 16.
USER_SCREENS, US_SCREENS_BUTTON = 1201, 16
UA_ALLOWED, UA_ADD, UA_REMOVE, UA_AVAILABLE, UA_OK, UA_CANCEL = 2, 4, 5, 8, 9, 10


@dataclass(frozen=True)
class OwnSettings:
    """The logged-in user's own region settings (screen 1205)."""

    language: str | None  # a code: en, de, es, pt, ja, fr, it, nl, zh or ko
    date_format: str | None  # a strptime format, see menu.DATE_FORMATS
    time_24h: bool | None
    decimal_comma: bool | None
    icon_colour: int | None  # 1 or 2


@dataclass(frozen=True)
class WebUserScreens:
    """The screens a normal web user may open (1201), and the ones it may get."""

    allowed: tuple[str, ...]
    available: tuple[str, ...]


# The login screen's (1203) second button (part 5, "User Settings") logs in
# straight into 1205 with the same payload as Login. Seen on a real iTM: 1205 opens
# on that login, is read and left with Cancel, and its OK saves the icon colour and a
# new password (in the same PASSWORD inputs as 1204's). Assumed, not seen: that its
# language button (7) opens the language screen (2501).


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
    """Read the open own-settings screen (1205); the password shows as asterisks.

    The language is None when 1205 shows a name not in LANGUAGE_NAMES.
    """
    o = OWN_SCREEN
    language = LANGUAGE_NAMES.get(_clean(state.get(o, OWN_LANGUAGE, "TEXT") or ""))
    idx = state.get(o, OWN_DATE_FORMAT, "SINGLE_SELIDX")
    colour = _radio(state, o, OWN_COLOUR_1, OWN_COLOUR_2)
    return OwnSettings(
        language=language,
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


async def read_own_language(s: _Session) -> str | None:
    """Open the language screen from 1205, read the checked language and cancel."""
    g = MenuScreen.LANGUAGE
    await _open_below(s, OWN_SCREEN, OWN_LANGUAGE_BUTTON, g)
    try:
        return language_from_screen(s.state)
    finally:
        await _close(s, g, CLOSE_PART[g])


async def _pick_own_language(s: _Session, code: str) -> None:
    """Open the language screen from 1205, pick a language and press its OK."""
    g = MenuScreen.LANGUAGE
    await _open_below(s, OWN_SCREEN, OWN_LANGUAGE_BUTTON, g)
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


# Seen on a real iTM: the user setup's (1204) screens button (16) and zones button
# (12) are disabled for an administrator, so the screens apply to normal users only;
# 1201 reads for a normal user. Assumed: the lists' last column holds the screen's
# name, and 1201's OK only hands the list back to 1204, whose OK saves it.


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


class UsersMixin(ClientCore):
    """ItmClient's web user and own settings methods."""

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
            Language (a code), date format, 12/24 hours, decimal separator and
            icon colour.

        """
        async with self._own_session() as s:
            settings = own_settings_from_screen(s.state)
            if settings.language is None:
                settings = replace(settings, language=await read_own_language(s))
            return settings

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
        if language is not None and result.language != language:
            wrong["language"] = result.language
        if wrong:
            msg = f"the iTM did not take the own settings: {wrong}"
            raise ItmCommandError(msg)
        return result

    async def _logs_in_with(self, password: str) -> bool:
        """Switch to a password if a login with it works; else keep the current one."""
        current, self._password = self._password, password
        try:
            async with self._session():
                pass
        except (ItmCommandError, LoginRejected):
            self._password = current
            return False
        return True


class UsersSessionMixin(SessionCore):
    """_Session's web user screen writes."""

    async def _answer_password(
        self, reply: Reply, password: str, what: str = "password"
    ) -> None:
        """Enter the password in the dialog(s) a reply opened (new, then repeat).

        A dry run never types it: it cancels the input and plans ``***``.
        """
        for _ in range(2):
            dialogs = [
                u
                for u in reply.screens
                if u.update == "DLG_OPEN" and u.data[1] == "PASSWORD"
            ]
            if not dialogs:
                break
            d = dialogs[0]
            low, high = d.data[3], d.data[4]
            if not low <= len(password) <= high:
                await self.send(d.screen, d.data[0], EventType.CLICK2, DataType.INT, 0)
                msg = f"the password must be {low}-{high} characters"
                raise ItmCommandError(msg)
            if self._client.dry_run:
                await self.send(d.screen, d.data[0], EventType.CLICK2, DataType.INT, 0)
                self._client.planned.append(
                    f"{what} = ***: screen {d.screen} input {d.data[0]}"
                )
                return
            reply = await self.send(
                d.screen, d.data[0], EventType.CLICK, DataType.STRING, string=password
            )
        errors = [d for d in reply.dialogs() if d[1] in {"ERROR", "INFO"}]
        if errors:
            msg = f"the iTM refused the password: {errors}"
            raise ItmCommandError(msg)

    async def _pick_user_zones(self, zones: tuple[str, ...]) -> None:
        """Select the zones a web user manages in 1202 and press its OK."""
        z = MenuScreen.USER_ZONES
        await self.click(MenuScreen.USER_SETUP, US_ZONES_BUTTON, "user zones")
        if z not in self.state.screens:
            msg = "the user's zone list did not open"
            raise ItmCommandError(msg)
        rows = []
        for zone in zones:
            row = row_index(self.state, z, UZ_LIST, 0, zone)
            if row is None:
                await self.close_screen(z)
                msg = f"zone {zone!r} not offered for web users"
                raise ItmCommandError(msg)
            rows.append(row)
        await self.send(
            z, UZ_LIST, EventType.CLICK, DataType.INTARRAY, len(rows), array=tuple(rows)
        )
        reply = await self.send(z, UZ_OK)
        await self._answer_confirm(z, reply)
        self.state.screens.pop(z, None)

    async def save_web_user(
        self,
        name: str,
        *,
        new: bool,
        password: str | None,
        admin: bool | None,
        zones: tuple[str, ...] | None = None,
    ) -> None:
        """Add a web user, or change one: name, password, type and zones, then OK."""
        s, u = MenuScreen.WEB_USERS, MenuScreen.USER_SETUP
        await self._open_system_screen(MB_WEB_USERS, s)
        try:
            listed = row_index(self.state, s, WU_LIST, 0, name) is not None
            if new and listed:
                msg = f"web user {name!r} exists already"
                raise ItmCommandError(msg)
            if new:
                await self.click(s, WU_ADD, "add web user")
            else:
                await self._select_listed(s, WU_LIST, 0, name, "web user")
                await self.click(s, WU_CHANGE, "change web user")
            if u not in self.state.screens:
                msg = f"{name}: the user setup did not open"
                raise ItmCommandError(msg)
            if new:
                await self.answer_text(u, US_NAME_BUTTON, name, "user name")
            if password is not None:
                reply = await self.send(u, US_PASSWORD_BUTTON)
                await self._answer_password(
                    reply, password, f"web user {name} password"
                )
            if admin is not None:
                await self.send(u, US_ADMIN if admin else US_USER)
            if zones is not None:
                await self._pick_user_zones(zones)
            verb = "add" if new else "change"
            await self.press_ok(f"{verb} web user {name}", u, US_OK, CLOSE_PART[u])
        finally:
            await self.close_screen(u)
            await self.close_screen(s)

    async def delete_web_user(self, name: str) -> None:
        """Select a web user, press Delete and confirm."""
        s = MenuScreen.WEB_USERS
        await self._open_system_screen(MB_WEB_USERS, s)
        try:
            await self._select_listed(s, WU_LIST, 0, name, "web user")
            if self.state.get(s, WU_DELETE, "ENABLED") == 0:
                msg = f"the iTM does not allow deleting {name!r}"
                raise ItmCommandError(msg)
            reply = await self.write(f"delete web user {name}", s, WU_DELETE)
            if reply is not None:
                await self._answer_confirm(s, reply)
        finally:
            await self.close_screen(s)
