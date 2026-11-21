"""A small simulated iTM that behaves like the real one where it matters.

- one web session; logins are rejected while it is active (the real iTM allows one
  per user, the simulator has one user)
- replies only contain operation-panel, icon view and main menu bar parts that
  differ from what the client is believed to show, and a client's own selection
  is never echoed back; the main menu bar's changes come with a POLLING event
- the icon view's Top and Up buttons are disabled at the top level, Down unless
  a zone is selected; the list view opens hidden with the login and sends its
  list again only when it differs from the one it sent before in the session
- setpoint changes are applied on the TIMER commit, and ignored while a unit is
  stopped; with ``latency_logins`` a change reaches the unit only after that many
  further logins or POLLING events; ``change_elsewhere`` sends another client's
  change that way
- the session ends after ``idle_timeout`` seconds without a request (by ``clock``);
  any request, a POLLING too, keeps it alive
- with ``sticky_picks`` a pick a unit didn't take stays on the client's panel
  until another point is shown; with ``forgets_picks`` the iTM takes the value it
  sent last for what the client shows, so such a pick also stays when another
  point with the unit's value is shown; ``FakeUnit.ignores_writes`` makes a unit
  ignore changes
- with ``editor_stays_open`` a schedule program's editor stays open when it is
  left with Cancel and Yes
- with ``ignores_typed_values`` a name, number or time of day typed in an input
  dialog (CHARINPUT, NUMBER, HOURMIN) is taken as Cancel: the dialog closes and
  nothing changes
- with ``normal_user=True`` the account is a normal web user without allowed
  screens: the login hides the main menu bar's menu button, the menu doesn't
  open, and the managed zone's units are the top level
- faults, all off by default: ``fail_next`` fails the next request before the
  iTM sees it (a refused connection); ``lose_reply_next`` lets the next request
  matching ``fault_on`` take effect and then loses its reply (the error given);
  ``hang_next`` makes that many requests matching ``fault_on`` never get an
  answer, and not take effect (through ``answer``, which waits); ``restart()``
  forgets the sessions, as a restarted iTM does
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

from daikin_itm.protocol import (
    _DIALOG_LAYOUTS,
    PART_UPDATE_CODE,
    SCREEN_UPDATE_CODE,
    Reader,
    Writer,
)

from .fake_itm_base import (
    CAPACITY_OPTIONS,
    DST_HOURS,
    EVENT_MODES,
    EXTRA_COMBOS,
    EXTRA_CONTROLS,
    EXTRA_NUMBERS,
    EXTRA_OWNER,
    EXTRA_RADIOS,
    HARDWARE_OPTIONS,
    HISTORY_COLUMNS,
    HISTORY_ROWS,
    LIST_HEADERS,
    LOCK_LABELS,
    LOW_NOISE_OPTIONS,
    MENU_CLOSE_PARTS,
    MENU_SCREENS,
    MONTHS,
    MULTISTATE_OPTIONS,
    OPTIONAL_BUTTONS,
    POWER_OPTIONS,
    ROW_LISTS,
    SCREENSAVER_MODES,
    SERVICE_BUTTONS,
    SETTINGS_TABS,
    SYSTEM_BUTTONS,
    SYSTEM_WRITE_SCREENS,
    WEEKDAYS,
    WEEKS,
    FakeAnalog,
    FakeGroup,
    FakeUnit,
    FakeVentilation,
    FakeZone,
    Update,
    _enabled,
    _rows,
)
from .fake_itm_menu import MenuMixin
from .fake_itm_panel import PanelMixin
from .fake_itm_system import SystemWrites

if TYPE_CHECKING:
    from collections.abc import Callable

__all__ = [
    "CAPACITY_OPTIONS",
    "DST_HOURS",
    "EVENT_MODES",
    "EXTRA_COMBOS",
    "EXTRA_CONTROLS",
    "EXTRA_NUMBERS",
    "EXTRA_OWNER",
    "EXTRA_RADIOS",
    "HARDWARE_OPTIONS",
    "HISTORY_COLUMNS",
    "HISTORY_ROWS",
    "LIST_HEADERS",
    "LOCK_LABELS",
    "LOW_NOISE_OPTIONS",
    "MENU_CLOSE_PARTS",
    "MENU_SCREENS",
    "MONTHS",
    "MULTISTATE_OPTIONS",
    "OPTIONAL_BUTTONS",
    "POWER_OPTIONS",
    "ROW_LISTS",
    "SCREENSAVER_MODES",
    "SERVICE_BUTTONS",
    "SETTINGS_TABS",
    "SYSTEM_BUTTONS",
    "SYSTEM_WRITE_SCREENS",
    "WEEKDAYS",
    "WEEKS",
    "_ENCODERS",
    "FakeAnalog",
    "FakeGroup",
    "FakeITM",
    "FakeUnit",
    "FakeVentilation",
    "FakeZone",
    "SystemWrites",
    "Update",
    "_enabled",
    "_encode_data",
    "_encode_part",
    "_rows",
]

_TYPED_INPUTS = frozenset({4, 6, 10})  # CHARINPUT, NUMBER, HOURMIN


class FakeITM(PanelMixin, MenuMixin):  # pylint: disable=too-many-instance-attributes
    """The simulated iTM; feed it request bodies with handle()."""

    # -- faults -----------------------------------------------------------------------
    async def answer(self, body: str) -> str:
        """Answer a request body like handle, or never (``hang_next``).

        A hung request is recorded in ``hung`` and doesn't take effect; the
        caller's timeout ends the wait.
        """
        await asyncio.sleep(0)
        if self.hang_next and self.fault_on(self._parse(body)):
            self.hang_next -= 1
            self.hung.append(self._parse(body))
            await asyncio.Event().wait()
        return self.handle(body)

    def restart(self) -> None:
        """Restart: every session is forgotten; units and settings stay."""
        self.session = None
        self.restarts += 1

    # -- request handling ---------------------------------------------------------
    def handle(self, body: str) -> str:
        """Answer a request body (``snD=...``) like the iTM would."""
        if (error := self.fail_next) is not None:
            self.fail_next = None
            raise error  # pylint: disable=raising-bad-type  # inferred from __init__
        reply = self._handle(body)
        lost = self.lose_reply_next
        if lost is not None and self.fault_on(self.requests[-1]):
            self.lose_reply_next = None
            self.lost_replies.append(self.requests[-1])
            raise lost  # pylint: disable=raising-bad-type  # inferred from __init__
        return reply

    @staticmethod
    def _parse(body: str) -> dict[str, Any]:
        """Return the fields of a request body."""
        r = Reader(body[4:])
        token = r.raw(32)
        ev: dict[str, Any] = {
            "screen": r.short(),
            "part": r.short(),
            "event": r.short(),
            "dtype": r.short(),
            "number": r.int(),
        }
        n = r.short()
        ev["array"] = [r.int() for _ in range(n)]
        ev["string"] = r.string()
        ev["token"] = token
        return ev

    def _handle(self, body: str) -> str:
        """Answer a request body; the faults are left to handle and answer."""
        ev = self._parse(body)
        token = ev["token"]
        self.requests.append(ev)
        now = self.clock()
        if (
            self.session is not None
            and self.idle_timeout is not None
            and now - self.last_request > self.idle_timeout
        ):
            self.session = None

        if ev["event"] == 0:  # INIT
            if ev["string"] != self.web_version:
                return self._reply(None, [("LOGOUT", -1, [2, self.web_version], [])])
            return self._reply(None, [("OPEN", 1203, [-1], [(0, "LABEL", "User")])])
        if ev["screen"] == 1203 and ev["event"] == 1:
            return self._login(ev["string"])
        if self.session is None or token != self.session:
            return self._reply(None, [("LOGOUT", -1, [3], [])])
        self.last_request = now
        if ev["event"] == 10:  # logout
            self.session = None
            return self._reply(None, [])
        if ev["event"] == 4:  # POLLING
            self._tick_pending()
        if (
            self.ignores_typed_values
            and ev["event"] == 1
            and self.input_dialogs.get((ev["screen"], ev["part"])) in _TYPED_INPUTS
        ):
            ev = {**ev, "event": 2}
            self.typed_values_ignored += 1
        updates = self._list_or_event(ev)
        self._track_dialogs(updates)
        return self._reply(self.session, updates)

    def _track_dialogs(self, updates: list[Update]) -> None:
        for kind, screen, data, _ in updates:
            if kind == "DLG_OPEN":
                self.input_dialogs[screen, data[0]] = data[1]
            elif kind == "DLG_CLOSE":
                self.input_dialogs.pop((screen, data[0]), None)
            elif kind == "CLOSE":
                self.input_dialogs = {
                    d: t for d, t in self.input_dialogs.items() if d[0] != screen
                }

    def _list_or_event(self, ev: dict[str, Any]) -> list[Update]:
        if ev["event"] == 1 and (ev["screen"], ev["part"]) in ROW_LISTS:
            # A list takes its selection as INTARRAY (the web UI's
            # onSelectedMultiItem); the real iTM ignores a plain INT there.
            if ev["dtype"] != 8 or not ev["array"]:
                return []
            ev = {**ev, "dtype": 1, "number": ev["array"][0]}
        return self._event(ev)

    def change_elsewhere(self, unit: FakeUnit, steps: int, **values: Any) -> None:
        """Let another client's change reach a unit after steps logins or POLLINGs.

        changed_elsewhere gets the number of requests seen when it does.
        """

        def change() -> None:
            for key, value in values.items():
                setattr(unit, key, value)
            self.changed_elsewhere.append(len(self.requests))

        self._pending.append([steps, change, unit])

    def _tick_pending(self) -> None:
        """Let the changes still on their way to a unit come one step closer."""
        for p in self._pending:
            p[0] -= 1
            if p[0] <= 0:
                p[1]()
        self._pending = [p for p in self._pending if p[0] > 0]

    def _login(self, s: str) -> str:
        n = int(s[:4])
        user, digest = s[4 : 4 + n], s[4 + n :]
        if self.session is not None or user != self.username or digest != self.digest:
            return self._reply(
                None,
                [("DLG_OPEN", 1203, [6, 1, "Cannot log in", "Error", "OK"], [])],
            )
        self.session = next(self.tokens)
        self.input_dialogs = {}
        self.last_request = self.clock()
        self.logins += 1
        self._tick_pending()
        self.path = [self.root]
        self.selected = 0
        self.shown = {}
        self.panel_sent = {}
        self.panel_item = None
        self.icon_shown = {}
        self.bar_shown = {}
        self.list_sent = None
        # The day a screen was moved to lasts as long as the session.
        self.overview_offset = 0
        self.history_page = 0
        self.history_day = self.history_loaded_day = "01/01/2025"
        self.pending_setpoint = None
        self.pending_power = None
        self.confirming_setback = None
        self.service_mode = False
        self.corners = []
        return self._reply(
            self.session,
            [
                ("LOGIN", -1, [102, "Information", ""], []),
                ("OPEN", 0, [-1], self._bar_delta()),
                ("OPEN", 4, [-1], self._panel_delta()),
                ("OPEN", 1, [-1], self._icon_delta()),
                ("OPEN", 2, [-1], [(6, "LABEL", "Icon")]),
                ("HIDE", 2, [], []),
                ("CLOSE", 1203, [], []),
            ],
        )

    def _event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        routes: list[tuple[bool, Callable[[dict[str, Any]], list[Update]]]] = [
            (screen == 1 and part == 6 and event == 1, self._select_icon),
            (screen == 1 and part == 2 and event == 1, self._zone_down),
            (screen == 1 and part == 1 and event == 1, self._zone_top),
            (screen == 1 and part == 4 and event == 1, self._list_view),
            (screen == 2 and part == 6 and event == 1, self._icon_view),
            (screen == 0 and part == 19 and event == 1, self._error_information),
            (screen == 10 and part == 1 and event == 1, self._close_errors),
            (event == 4, self._polling),
            (screen in {0, 5} or screen in self.open_screens, self._menu),
            (screen == 4 and event == 1 and part in {9, 11}, self._choose),
            (
                screen == 4 and part == 24 and self.confirming_setback is not None,
                self._confirm_setback,
            ),
            (screen == 4 and part == 17 and event == 1, self._open_dialog),
            (screen == 4 and part == 7 and event == 1, self._details),
            (screen == 9 and part == 15, lambda _: [("CLOSE", 9, [], [])]),
            (screen == 7, self._dialog_event),
            (screen == 4 and event == 3, self._commit),
        ]
        for matches, handler in routes:
            if matches:
                return handler(ev)
        return []

    def _polling(self, ev: dict[str, Any]) -> list[Update]:
        """Name the selected icon on the operation panel again, like the real iTM."""
        changed = [p for p in self._panel_delta() if p[:2] != (0, "LABEL")]
        panel = [(0, "LABEL", self.shown[0, "LABEL"]), *changed]
        return [("UPDATE", 4, [], panel), *super()._polling(ev)]

    # -- encoding -------------------------------------------------------------------
    @staticmethod
    def _reply(token: str | None, updates: list[Update]) -> str:
        w = Writer(token or "0" * 32)
        w.int(len(updates))
        for kind, screen, data, parts in updates:
            w.short(SCREEN_UPDATE_CODE[kind])
            w.short(screen)
            _encode_data(w, kind, data)
            if kind in {"OPEN", "UPDATE", "CLOSE", "SHOW", "HIDE"}:
                w.int(len(parts))
                for part, ptype, value in parts:
                    w.short(part)
                    w.short(PART_UPDATE_CODE[ptype])
                    _encode_part(w, ptype, value)
        return w.buf + "\0"


def _encode_data(  # noqa: PLR0912, PLR0915  # pylint: disable=too-many-branches,too-many-statements
    w: Writer, kind: str, data: list[Any]
) -> None:
    if kind == "OPEN":
        w.int(data[0])
    elif kind == "LOGIN":
        w.int(data[0])
        w.string(data[1])
        w.string(data[2])
    elif kind == "LOGOUT":
        w.int(data[0])
        if data[0] == 2:
            w.string(data[1])
    elif kind == "DLG_CLOSE":
        w.short(data[0])
        w.short(data[1])
    elif kind == "DLG_OPEN" and data[1] == 9:  # DATE: N S + 9 H + h + 13 S
        w.short(data[0])
        w.short(data[1])
        w.int(data[2])
        w.string(data[3])
        for n in data[4:13]:
            w.short(n)
        w.short(0)
        for text in [*data[13:], *[""] * 13][:13]:
            w.string(text)
    elif kind == "DLG_OPEN" and data[1] == 4:  # CHARINPUT, see _DIALOG_LAYOUTS
        w.short(data[0])
        w.short(data[1])
        w.string(data[2])
        w.string(data[3])
        for n in data[4:9]:
            w.short(n)
        w.short(0)
        for text in data[9:23]:
            w.string(text)
        w.string("")
        w.string("")
        w.string("")
        w.string(data[23])
    elif kind == "DLG_OPEN" and data[1] in {5, 8}:  # PASSWORD, TIME: by layout
        w.short(data[0])
        w.short(data[1])
        values = iter(data[2:])
        for code in _DIALOG_LAYOUTS[data[1]]:
            value = next(values) if code.isupper() else None
            if code.upper() == "S":
                w.string(value or "")
            elif code.upper() == "H":
                w.short(value or 0)
            else:
                w.int(value or 0)
    elif kind == "DLG_OPEN" and data[1] == 6:  # NUMBER: NSNNNNNNHH + 11 S
        w.short(data[0])
        w.short(data[1])
        w.int(data[2])
        w.string(data[3])
        for n in data[4:10]:
            w.int(n)
        w.short(data[10])
        w.short(data[11])
        for text in data[12:23]:
            w.string(text)
    elif kind == "DLG_OPEN":
        w.short(data[0])
        w.short(data[1])
        for v in data[2:]:
            if isinstance(v, int):
                w.int(v)
            else:
                w.string(v)
    elif kind == "DLG_UPDATE":
        w.short(data[0])
        w.short(data[1])
        if data[1] == 11:
            w.int(data[2])


def _encode_part(w: Writer, ptype: str, v: Any) -> None:
    if ptype in {"LABEL", "TEXT"}:
        w.string(v)
    elif ptype in {"VISIBLED", "ENABLED", "SINGLE_SELIDX", "ICONID", "CHECK_STATE"}:
        w.int(v)
    elif ptype == "ICONID_MODE":
        w.short(v[0])
        w.short(v[1])
    elif ptype == "COMBO_STRS":
        w.int(len(v))
        for s in v:
            w.string(s)
    elif ptype == "COMBO_NUMS":
        w.int(len(v))
        for n in v:
            w.int(n)
    elif ptype in _ENCODERS:
        _ENCODERS[ptype](w, v)
    else:
        raise NotImplementedError(ptype)


def _encode_iconpanel(w: Writer, v: dict[str, Any]) -> None:
    w.int(len(v["icons"]))
    for name, a, b in v["icons"]:
        w.string(name)
        w.short(a)
        w.short(b)
    w.int(v["selected"])


def _encode_list_column(w: Writer, v: list[list[Any]]) -> None:
    w.int(len(v))
    for name, *shorts in v:
        w.string(name)
        for n in shorts:
            w.short(n)


def _encode_list_row(w: Writer, v: dict[str, Any]) -> None:
    rows = v["rows"]
    ncols = v.get("ncols", len(rows[0]["text"]) if rows else 0)
    w.short(len(rows))
    w.short(ncols)
    for row in rows:
        for text in row["text"]:
            w.string(text)
        for n in row["a"]:
            w.short(n)
        for n in row["b"]:
            w.short(n)
    w.int(len(v["selected"]))
    for n in v["selected"]:
        w.int(n)


def _encode_pntlist(w: Writer, v: dict[str, Any]) -> None:
    w.int(len(v["cols"]))
    for name, width in v["cols"]:
        w.string(name)
        w.int(width)
    w.short(v["sort"][0])
    w.short(v["sort"][1])
    w.int(len(v["rows"]))
    for row in v["rows"]:
        for text, a, b in row:
            w.string(text)
            w.short(a)
            w.short(b)
    w.int(v["selected"])


def _encode_tab_name(w: Writer, v: list[str]) -> None:
    w.int(len(v))
    for name in v:
        w.string(name)


def _encode_tab_visible(w: Writer, v: list[Any]) -> None:
    w.short(v[0])
    w.short(len(v[1]))
    for n in v[1]:
        w.int(n)


def _encode_schedule_panel(w: Writer, v: list[Any]) -> None:
    w.int(len(v[0]))
    for state, name, start, end in v[0]:
        w.int(state)
        w.string(name)
        w.string(start)
        w.string(end)
    w.int(v[1])


def _encode_timeline_bar(w: Writer, v: list[list[int]]) -> None:
    w.int(len(v))
    for entry in v:
        for n in entry:
            w.short(n)


def _encode_list_cell(w: Writer, v: list[list[Any]]) -> None:
    w.int(len(v))
    for row, col, text, a, b in v:
        w.short(row)
        w.short(col)
        w.string(text)
        w.short(a)
        w.short(b)


_ENCODERS: dict[str, Callable[[Writer, Any], None]] = {
    "LIST_CELL": _encode_list_cell,
    "TIMELINE_BAR": _encode_timeline_bar,
    "TAB_NAME": _encode_tab_name,
    "TAB_VISIBLE": _encode_tab_visible,
    "SCHEDULE_PANEL": _encode_schedule_panel,
    "ICONPANEL": _encode_iconpanel,
    "PNTLIST": _encode_pntlist,
    "LIST_COLUMN": _encode_list_column,
    "LIST_ROW": _encode_list_row,
}
