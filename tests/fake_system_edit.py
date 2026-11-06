"""A simulated iTM that also edits e-mail and the other phase-2B settings.

Confirm dialogs, the web server port, remote management and the logged-in user's own
settings (screen 1205).

Strict like the real iTM: clicking a part it reports as disabled raises (the real
web server hangs then). Values are generic placeholders.
"""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING, Any

from daikin_itm.protocol import Reader

from .fake_itm import FakeITM, Update, _enabled, _rows

if TYPE_CHECKING:
    from collections.abc import Callable

EMAIL, ADDRESS, TRIAL, SERVER = 1300, 1301, 1302, 1303
CONFIRM, WEB_SERVER, REMOTE, OWN = 2100, 1801, 1100, 1205
CANCEL = {
    EMAIL: 12,
    ADDRESS: 18,
    TRIAL: 4,
    SERVER: 41,
    CONFIRM: 5,
    WEB_SERVER: 7,
    REMOTE: 7,
}
OK_CONFIRM = 30  # the "Execute setting?" dialog id
DATE_FORMATS = ["DD/MM/YYYY", "MM/DD/YYYY", "YYYY/MM/DD"]
LANGUAGE = 2501
LANGUAGE_NAMES = {2: "English", 3: "Deutsch", 9: "Nederlands"}  # by radio


class FakeSystemEditITM(FakeITM):  # pylint: disable=too-many-instance-attributes
    """FakeITM plus the e-mail, confirm, web server, remote and own settings writes."""

    def __init__(self) -> None:
        """Start with no e-mail address and confirm dialogs on."""
        super().__init__()
        self.emails: dict[str, list[str]] = {}
        self.email_active: dict[str, bool] = {}
        self.email_server: dict[int, Any] = {
            3: "Site",
            9: "itm@example.com",
            19: "smtp.example.com",
            22: "25",
            29: "",
            32: "110",
            35: "",
            6: 0,
            26: 0,
        }
        self.email_password = ""
        self.mails_sent: list[str] = []
        self.confirm_on = True
        self.web_port: int | None = None  # None: the default port
        self.remote_connectable = False
        self.remote_connected = False
        self.own = {"date": 0, "24h": True, "comma": False, "colour": 1}
        self.own_password_changes = 0
        self.own_ends_session = True  # leaving 1205 ends the session
        self.own_opens = True
        self.own_repeat_mismatch = False  # the repeated password is refused
        self.own_ignore_password = False  # a new password is dropped
        self.edit: dict[str, Any] = {}  # what the open screens are editing

    # -- login through "User Settings" (1203/5) opens 1205 ---------------------------
    def handle(self, body: str) -> str:
        """Answer a request; the login screen's second button opens 1205."""
        r = Reader(body[4 + 32 :])
        screen, part, event = r.short(), r.short(), r.short()
        reply = super().handle(body)
        if (
            screen == 1203
            and part == 5
            and event == 1
            and self.session is not None
            and self.own_opens
        ):
            self.open_screens.add(OWN)
            self.edit = {"own": dict(self.own)}
            return self._reply(
                self.session,
                [("OPEN", OWN, [-1], self._own_parts()), ("CLOSE", 1203, [], [])],
            )
        return reply

    # -- routing --------------------------------------------------------------------
    def _event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part = ev["screen"], ev["part"]
        if screen == LANGUAGE and "own_language" in self.edit:
            return self._own_language_event(part)
        if screen == 1800 and part == 22 and 1800 in self.open_screens:
            self.open_screens.add(WEB_SERVER)
            self.edit["port"] = self.web_port
            return [("OPEN", WEB_SERVER, [-1], self._web_parts())]
        if screen in {
            EMAIL,
            ADDRESS,
            TRIAL,
            SERVER,
            CONFIRM,
            WEB_SERVER,
            REMOTE,
            OWN,
        } and (screen in self.open_screens):
            return self._edit_event(ev)
        return super()._event(ev)

    def _system_parts(self, target: int) -> list[tuple[int, str, Any]]:
        if target == EMAIL:
            self.edit.pop("row", None)
            return self._email_parts()
        if target == CONFIRM:
            return [
                (2, "CHECK_STATE", int(self.confirm_on)),
                (3, "CHECK_STATE", int(not self.confirm_on)),
            ]
        if target == REMOTE:
            on = int(self.remote_connectable)
            return [
                (2, "TEXT", "Connected" if self.remote_connected else "Disconnected"),
                (4, "TEXT", "0.0.0.0"),  # noqa: S104 - as the iTM shows it
                (5, "ENABLED", on),
                (6, "ENABLED", on),
            ]
        return super()._system_parts(target)

    # -- parts ----------------------------------------------------------------------
    def _email_parts(self) -> list[tuple[int, str, Any]]:
        rows = [["Enable" if self.email_active.get(a) else " ", a] for a in self.emails]
        return [(1, "LIST_ROW", _rows(rows)), *self._email_selection_parts()]

    def _email_selection_parts(self) -> list[tuple[int, str, Any]]:
        row = self.edit.get("row")
        if row is None:
            return _enabled((3, 4, 7, 8), 0)
        active = self.email_active.get(list(self.emails)[row], False)
        return [
            *_enabled((3, 4, 8), 1),
            (7, "ENABLED", int(not active)),
            (3, "CHECK_STATE", int(active)),
            (4, "CHECK_STATE", int(not active)),
        ]

    def _address_parts(self) -> list[tuple[int, str, Any]]:
        e = self.edit
        selected = e["points"]
        available = [u.name for u in self.units if u.name not in selected]
        return [
            (3, "LIST_ROW", _rows([["Indoor", n] for n in selected])),
            (9, "LIST_ROW", _rows([["Indoor", n] for n in available])),
            (11, "TEXT", e["address"]),
            (5, "ENABLED", 0),
            (6, "ENABLED", 0),
        ]

    def _web_parts(self) -> list[tuple[int, str, Any]]:
        custom = self.edit.get("port") is not None
        return [
            (2, "CHECK_STATE", int(not custom)),
            (3, "CHECK_STATE", int(custom)),
            (4, "TEXT", str(self.edit.get("port") or 80)),
            (5, "ENABLED", int(custom)),
        ]

    def _own_parts(self) -> list[tuple[int, str, Any]]:
        o = self.edit["own"]
        return [
            (2, "TEXT", "***"),
            (6, "TEXT", LANGUAGE_NAMES.get(self.language_radio, "?")),
            (9, "COMBO_STRS", DATE_FORMATS),
            (9, "SINGLE_SELIDX", o["date"]),
            (11, "CHECK_STATE", int(o["24h"])),
            (12, "CHECK_STATE", int(not o["24h"])),
            (14, "CHECK_STATE", int(not o["comma"])),
            (15, "CHECK_STATE", int(o["comma"])),
            (17, "CHECK_STATE", int(o["colour"] == 1)),
            (18, "CHECK_STATE", int(o["colour"] == 2)),
        ]

    # -- events ---------------------------------------------------------------------
    def _disabled(self, screen: int, part: int) -> bool:
        return any(
            p == part and kind == "ENABLED" and value == 0
            for p, kind, value in self._current_parts(screen)
        )

    def _current_parts(self, screen: int) -> list[tuple[int, str, Any]]:
        if screen == EMAIL:
            return self._email_parts()
        if screen == ADDRESS:
            parts = self._address_parts()
            return [p for p in parts if p[0] not in self.edit.get("enabled", set())]
        if screen == WEB_SERVER:
            return self._web_parts()
        if screen == REMOTE:
            return self._system_parts(REMOTE)
        return []

    def _confirm(
        self, screen: int, apply: Callable[[], None], *, close: bool
    ) -> list[Update]:
        self.edit["confirm"] = (screen, apply, close)
        data = [OK_CONFIRM, 2, "Execute setting?", "Confirm", "Yes", "No"]
        return [("DLG_OPEN", screen, data, [])]

    def _edit_event(  # noqa: PLR0911, PLR0912  # pylint: disable=too-many-return-statements,too-many-branches
        self, ev: dict[str, Any]
    ) -> list[Update]:
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        if event == 1 and self._disabled(screen, part):
            msg = f"({screen}, {part}) clicked while disabled: the real iTM hangs here"
            raise RuntimeError(msg)
        if screen == OWN:
            return self._own_event(part, event, ev)
        if part == OK_CONFIRM and "confirm" in self.edit:
            target, apply, close = self.edit.pop("confirm")
            updates: list[Update] = [("DLG_CLOSE", target, [OK_CONFIRM, 2], [])]
            if event == 1:
                apply()
                if close:
                    self.open_screens.discard(target)
                    updates.append(("CLOSE", target, [], []))
            return updates
        if part == CANCEL.get(screen):
            self.open_screens.discard(screen)
            return [("CLOSE", screen, [], [])]
        if screen == EMAIL:
            return self._email_event(part, ev)
        if screen == ADDRESS:
            return self._address_event(part, ev)
        if screen == TRIAL and part == 3:
            address = self.edit["address"]
            return self._confirm(
                TRIAL, lambda: self.mails_sent.append(address), close=True
            )
        if screen == SERVER:
            return self._server_event(part, ev)
        if screen == CONFIRM:
            if part in {2, 3}:
                self.edit["confirm_on"] = part == 2
                return [("UPDATE", CONFIRM, [], [])]
            if part == 4:
                value = self.edit.get("confirm_on", self.confirm_on)
                return self._confirm(
                    CONFIRM, lambda: setattr(self, "confirm_on", value), close=True
                )
        if screen == WEB_SERVER:
            return self._web_event(part, ev)
        if screen == REMOTE and part in {5, 6}:
            self.remote_connected = part == 5
            return [("UPDATE", REMOTE, [], [])]
        return [("UPDATE", screen, [], [])]

    def _email_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, part: int, ev: dict[str, Any]
    ) -> list[Update]:
        if ev["dtype"] == 8:  # a row selection enables the radios, edit and delete
            self.edit["row"] = ev["array"][0]
            return [("UPDATE", EMAIL, [], self._email_selection_parts())]
        if part == 6:  # like the real iTM: the address first, then 1301
            self.edit["address_input"] = True
            data = [14, 4, "E-mail address", "", 1, 128, 0, 1, 0, "Words", "OK"]
            return [("DLG_OPEN", EMAIL, [*data, "Cancel", *[""] * 11, ""], [])]
        if part == 14 and self.edit.pop("address_input", False):
            close: list[Update] = [("DLG_CLOSE", EMAIL, [14, 4], [])]
            if ev["event"] != 1:
                return close
            # Like the real iTM: the address goes straight into the list.
            self.emails[ev["string"]] = []
            self.email_active[ev["string"]] = False
            return [*close, ("UPDATE", EMAIL, [], self._email_parts())]
        if part == 8:
            address = list(self.emails)[self.edit["row"]]
            self.edit.update(
                address=address, original=address, points=list(self.emails[address])
            )
            self.edit["enabled"] = set()
            self.open_screens.add(ADDRESS)
            return [("OPEN", ADDRESS, [-1], self._address_parts())]
        if part == 7:
            address = list(self.emails)[self.edit["row"]]

            def delete() -> None:
                del self.emails[address]
                self.email_active.pop(address, None)
                self.edit.pop("row", None)

            return self._confirm(EMAIL, delete, close=False)
        if part in {3, 4}:
            self.email_active[list(self.emails)[self.edit["row"]]] = part == 3
            return [("UPDATE", EMAIL, [], self._email_parts())]
        if part == 10:
            self.open_screens.add(SERVER)
            self.edit["server"] = dict(self.email_server)
            parts = [
                (p, "TEXT", v) for p, v in self.email_server.items() if p not in {6, 26}
            ]
            parts += [
                (6, "COMBO_STRS", ["1", "2", "4"]),
                (6, "SINGLE_SELIDX", self.email_server[6]),
                (26, "COMBO_STRS", ["None", "POP before SMTP", "SMTP-AUTH"]),
                (26, "SINGLE_SELIDX", self.email_server[26]),
                (38, "TEXT", "***"),
            ]
            return [("OPEN", SERVER, [-1], parts)]
        return [("UPDATE", EMAIL, [], [])]

    def _address_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, part: int, ev: dict[str, Any]
    ) -> list[Update]:
        e = self.edit
        if ev["dtype"] == 8:
            e["pick"] = (part, ev["array"][0])
            e["enabled"] = {5} if part == 9 else {6}
            return [("UPDATE", ADDRESS, [], [(5 if part == 9 else 6, "ENABLED", 1)])]
        if part in {5, 6}:
            _source, row = e.pop("pick")
            if part == 5:
                available = [u.name for u in self.units if u.name not in e["points"]]
                e["points"].append(available[row])
            else:
                del e["points"][row]
            e["enabled"] = set()
            return [("UPDATE", ADDRESS, [], self._address_parts())]
        if part == 12:
            e["input"] = 19
            data = [
                19,
                4,
                "E-mail",
                e["address"],
                1,
                60,
                0,
                0,
                1,
                "",
                "OK",
                "Cancel",
            ]
            return [("DLG_OPEN", ADDRESS, [*data, *[""] * 11, ""], [])]
        if part == 19 and e.get("input") == 19:
            e.pop("input")
            if ev["event"] == 1:
                e["address"] = ev["string"]
            return [("DLG_CLOSE", ADDRESS, [19, 4], [])]
        if part == 13:
            self.open_screens.add(TRIAL)
            return [("OPEN", TRIAL, [-1], [(2, "TEXT", e["address"])])]
        if part == 17:
            if not e["address"] or not e["points"]:
                error = [31, 1, "Setting incomplete", "Error", "OK"]
                return [("DLG_OPEN", ADDRESS, error, [])]

            def save() -> None:
                active = False
                if e.get("original"):
                    del self.emails[e["original"]]
                    active = self.email_active.pop(e["original"], False)
                self.emails[e["address"]] = list(e["points"])
                self.email_active[e["address"]] = active

            return self._confirm(ADDRESS, save, close=True)
        return [("UPDATE", ADDRESS, [], [])]

    def _server_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, part: int, ev: dict[str, Any]
    ) -> list[Update]:
        e = self.edit
        buttons = {4: 3, 10: 9, 20: 19, 30: 29, 36: 35}
        numbers = {23: 22, 33: 32}
        if part in buttons:
            e["input"] = (part, buttons[part])
            current = str(e["server"][buttons[part]])
            data = [
                part + 50,
                4,
                "Text",
                current,
                0,
                60,
                0,
                0,
                1,
                "",
                "OK",
                "Cancel",
            ]
            return [("DLG_OPEN", SERVER, [*data, *[""] * 11, ""], [])]
        if part in numbers:
            e["input"] = (part, numbers[part])
            data = [part + 50, 6, 0, "Port", 25, 0, 1, 0, 65535, 0, 2, 1, *["x"] * 11]
            return [("DLG_OPEN", SERVER, data, [])]
        if part == 39:
            e["input"] = (39, None)
            data = [
                89,
                5,
                "Password",
                0,
                32,
                4,
                "OK",
                "Cancel",
                "",
                "",
                "",
                "Error",
                0,
            ]
            return [("DLG_OPEN", SERVER, data, [])]
        if "input" in e and part == e["input"][0] + 50:
            button, field = e.pop("input")
            if ev["event"] == 1:
                if button == 39:
                    e["password"] = ev["string"]
                else:
                    e["server"][field] = (
                        ev["string"] if button in buttons else str(ev["number"])
                    )
            return [("DLG_CLOSE", SERVER, [part, 4], [])]
        if part in {6, 26}:
            e["server"][part] = ev["number"]
            return [("UPDATE", SERVER, [], [])]
        if part == 40:
            server, password = dict(e["server"]), e.get("password")

            def save() -> None:
                self.email_server = server
                if password is not None:
                    self.email_password = password

            return self._confirm(SERVER, save, close=True)
        return [("UPDATE", SERVER, [], [])]

    def _web_event(self, part: int, ev: dict[str, Any]) -> list[Update]:
        e = self.edit
        if part in {2, 3}:
            e["port"] = None if part == 2 else (e.get("port") or 8080)
            return [("UPDATE", WEB_SERVER, [], self._web_parts())]
        if part == 5:
            e["port_input"] = True
            data = [75, 6, 0, "Port", 80, 0, 1, 0, 65535, 0, 2, 1, *["x"] * 11]
            return [("DLG_OPEN", WEB_SERVER, data, [])]
        if part == 75 and e.pop("port_input", False):
            if ev["event"] == 1:
                e["port"] = ev["number"]
            return [("DLG_CLOSE", WEB_SERVER, [75, 6], [])]
        if part == 6:
            port = e.get("port")
            return self._confirm(
                WEB_SERVER, lambda: setattr(self, "web_port", port), close=True
            )
        return [("UPDATE", WEB_SERVER, [], [])]

    def _own_event(  # noqa: PLR0911, PLR0912  # pylint: disable=too-many-return-statements,too-many-branches
        self, part: int, event: int, ev: dict[str, Any]
    ) -> list[Update]:
        o = self.edit["own"]
        if part == 3:  # the password: a PASSWORD input, then its repeat
            self.edit["password_input"] = "new"
            data = [21, 5, "New password", 4, 15, 4]
            return [
                ("DLG_OPEN", OWN, [*data, "OK", "Cancel", "", "", "", "Error", 0], [])
            ]
        if part == 21 and "password_input" in self.edit:
            step = self.edit.pop("password_input")
            close: Update = ("DLG_CLOSE", OWN, [21, 5], [])
            if event != 1:
                return [close]
            if step == "new":
                o["password"] = ev["string"]
                self.edit["password_input"] = "repeat"
                data = [21, 5, "Repeat password", 4, 15, 4]
                data += ["OK", "Cancel", "", "", "", "Error", 0]
                return [close, ("DLG_OPEN", OWN, data, [])]
            if self.own_repeat_mismatch or ev["string"] != o["password"]:
                o.pop("password")
                return [close, ("DLG_OPEN", OWN, [22, 1, "Invalid", "Error", "OK"], [])]
            return [close]
        if part == 7:
            self.edit["own_language"] = o.get("language", self.language_radio)
            self.open_screens.add(LANGUAGE)
            radios = [
                (r, "CHECK_STATE", int(r == self.edit["own_language"]))
                for r in range(2, 12)
            ]
            return [("OPEN", LANGUAGE, [-1], radios)]
        if part == 9:
            o["date"] = ev["number"]
        elif part in {11, 12}:
            o["24h"] = part == 11
        elif part in {14, 15}:
            o["comma"] = part == 15
        elif part in {17, 18}:
            o["colour"] = 1 if part == 17 else 2
        elif part == 19:
            return self._confirm(OWN, lambda: None, close=False)
        elif part == OK_CONFIRM and "confirm" in self.edit:
            self.edit.pop("confirm")
            if event == 1:
                self._apply_own(o)
                return self._end_own_session()
            return [("DLG_CLOSE", OWN, [OK_CONFIRM, 2], [])]
        elif part == 20:
            return self._end_own_session()
        return [("UPDATE", OWN, [], [])]

    def _apply_own(self, o: dict[str, Any]) -> None:
        if "language" in o:
            self.language_radio = o.pop("language")
        password = o.pop("password", None)
        if password is not None and not self.own_ignore_password:
            self.digest = hashlib.sha256(password.encode()).hexdigest()
            self.own_password_changes += 1
        self.own = dict(o)

    def _own_language_event(self, part: int) -> list[Update]:
        """Answer the language screen opened from 1205: a radio, OK or Cancel."""
        if 2 <= part <= 11:
            self.edit["own_language"] = part
            return [("UPDATE", LANGUAGE, [], [])]
        if part in {12, 13}:
            picked = self.edit.pop("own_language")
            if part == 12:
                self.edit["own"]["language"] = picked
            self.open_screens.discard(LANGUAGE)
            return [("CLOSE", LANGUAGE, [], [])]
        return [("UPDATE", LANGUAGE, [], [])]

    def _end_own_session(self) -> list[Update]:
        """Leaving 1205 ends the session, like the real iTM (LOGOUT [0])."""
        self.open_screens.discard(OWN)
        if not self.own_ends_session:
            return [("CLOSE", OWN, [], [])]
        self.session = None
        return [("LOGOUT", -1, [0], [])]
