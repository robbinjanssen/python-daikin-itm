"""A simulated iTM that also edits a normal web user's allowed screens (1201).

The user setup (1204) gets the screens button (16) and its count (14); like the real
iTM, the screens and zones buttons are disabled for an administrator, and clicking a
disabled part raises (the real web server hangs then). 1201's OK hands the list back
to 1204; 1204's OK (and Yes) saves it. Screen names are generic placeholders.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from .fake_system_edit import FakeSystemEditITM

if TYPE_CHECKING:
    from .fake_itm import Update

USER_SETUP, SCREENS = 1204, 1201
ALL_SCREENS = ["Screen A", "Screen B", "Screen C"]


def _screen_rows(names: list[str]) -> dict[str, Any]:
    """Two columns, a number and the name, like a list with a header."""
    return {
        "rows": [
            {"text": [str(i + 1), n], "a": [-1, -1], "b": [-1, -1]}
            for i, n in enumerate(names)
        ],
        "selected": [],
    }


class FakeUsersITM(FakeSystemEditITM):
    """FakeSystemEditITM plus the allowed screens of normal web users."""

    def __init__(self) -> None:
        """Start with "viewer" allowed one screen."""
        super().__init__()
        self.user_screens: dict[str, list[str]] = {"viewer": ["Screen A"]}
        self.screens_disabled_for: set[str] = set()  # 1204/16 disabled anyway
        self.us_edit: dict[str, Any] = {}

    def _user(self) -> tuple[str, bool]:
        pending = self.pending_system
        return pending["user"], bool(pending["admin"])

    def _screens_enabled(self) -> bool:
        name, admin = self._user()
        return not admin and name not in self.screens_disabled_for

    def _event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part = ev["screen"], ev["part"]
        if screen == SCREENS and SCREENS in self.open_screens:
            return self._screens_event(ev)
        if screen == USER_SETUP and USER_SETUP in self.open_screens:
            if part in {12, 16} and not self._screens_enabled():
                msg = f"1204/{part} clicked while disabled: the real iTM hangs"
                raise RuntimeError(msg)
            if part == 16:
                return self._open_screens()
            if part == 18:
                self.us_edit = {}
        confirmed = (
            screen == USER_SETUP
            and part == 30
            and ev["event"] == 1
            and "allowed" in self.us_edit
        )
        user = self._user()[0] if confirmed else None
        updates = super()._event(ev)
        if user is not None and USER_SETUP not in self.open_screens:
            self.user_screens[user] = self.us_edit.pop("allowed")
        for u in updates:
            if u[0] == "OPEN" and u[1] == USER_SETUP:
                self.us_edit = {}
                name, _admin = self._user()
                on = int(self._screens_enabled())
                u[3].extend(
                    [
                        (12, "ENABLED", on),
                        (14, "TEXT", str(len(self.user_screens.get(name, [])))),
                        (16, "ENABLED", on),
                    ]
                )
        return updates

    def _screen_parts(self) -> list[tuple[int, str, Any]]:
        allowed = self.us_edit["list"]
        available = [n for n in ALL_SCREENS if n not in allowed]
        return [
            (2, "LIST_ROW", _screen_rows(allowed)),
            (8, "LIST_ROW", _screen_rows(available)),
            (4, "ENABLED", int(self.us_edit.get("pick", (0,))[0] == 8)),
            (5, "ENABLED", int(self.us_edit.get("pick", (0,))[0] == 2)),
        ]

    def _open_screens(self) -> list[Update]:
        name, _admin = self._user()
        self.us_edit["list"] = list(self.user_screens.get(name, []))
        self.open_screens.add(SCREENS)
        return [("OPEN", SCREENS, [-1], self._screen_parts())]

    def _screens_event(self, ev: dict[str, Any]) -> list[Update]:
        e, part = self.us_edit, ev["part"]
        if ev["dtype"] == 8:
            e["pick"] = (part, ev["array"][0])
            return [("UPDATE", SCREENS, [], self._screen_parts())]
        if part in {4, 5}:
            source = e.pop("pick", (0, 0))
            if source[0] != (8 if part == 4 else 2):
                msg = f"1201/{part} clicked while disabled: the real iTM hangs"
                raise RuntimeError(msg)
            if part == 4:
                available = [n for n in ALL_SCREENS if n not in e["list"]]
                e["list"].append(available[source[1]])
            else:
                del e["list"][source[1]]
            return [("UPDATE", SCREENS, [], self._screen_parts())]
        if part in {9, 10}:
            if part == 9:
                e["allowed"] = list(e["list"])
            self.open_screens.discard(SCREENS)
            return [("CLOSE", SCREENS, [], [])]
        return [("UPDATE", SCREENS, [], [])]
