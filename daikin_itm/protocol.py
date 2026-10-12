"""Wire format of the iTM web UI (POST /cmd/ with ``snD=<hex>``).

Reverse-engineered from the web UI's script.js (Ver1.32.00). Requests and replies are
hex strings: numbers are big-endian hex (short = 4 chars, int = 8 chars) and strings
are a short length followed by the raw characters. Every reply starts with a new
32-character page token.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from .exceptions import ItmError, ProtocolError

if TYPE_CHECKING:
    import builtins
    from collections.abc import Callable

__all__ = [
    "DIALOG",
    "MAX_REPLY_CHARS",
    "NO_TOKEN",
    "PART_UPDATE",
    "PART_UPDATE_CODE",
    "SCREEN_UPDATE",
    "SCREEN_UPDATE_CODE",
    "TOKEN_LEN",
    "WEB_VERSION",
    "DataType",
    "EventType",
    "ItmError",
    "Part",
    "ProtocolError",
    "Reader",
    "Reply",
    "ScreenID",
    "ScreenUpdate",
    "Writer",
    "encode_event",
    "is_token",
    "parse_reply",
]

WEB_VERSION = "Ver1.32.00"
TOKEN_LEN = 32
NO_TOKEN = "0" * TOKEN_LEN
# Real replies are a few KB (the settings dialog, the largest, ~30 KB).
MAX_REPLY_CHARS = 1_000_000


class EventType(IntEnum):
    """GUI event types the web UI sends."""

    INIT = 0
    CLICK = 1
    CLICK2 = 2
    TIMER = 3
    POLLING = 4
    UPD_PROG = 5
    CANCEL_PROG = 6
    LIST_COLUMN = 7
    TAB_SELECT = 8
    FINISH_DOWNLOAD = 9
    LOGOUT = 10


class DataType(IntEnum):
    """Type of the value sent along with an event."""

    NONE = 0
    INT = 1
    STRING = 2


class ScreenID(IntEnum):
    """Screens of the web UI this client uses."""

    MAINMENU = 0
    STDICON = 1
    STDLIST = 2
    STDLAYOUT = 3
    OPERATION = 4
    SETTINGS = 7  # "Gedetailleerde instellingen" dialog, from the operation panel
    DETAILS = 9  # "Gedetailleerde info": name, ID, type, port, address, description
    LOGIN = 1203


# Bits for the POLLING event, one per open screen (systemPolingPrivate in script.js).
POLL_MAINMENU, POLL_STDICON, POLL_STDLIST, POLL_STDLAYOUT, POLL_OPERATION = (
    1,
    2,
    4,
    8,
    16,
)

SCREEN_UPDATE = {
    0: "INIT",
    1: "OPEN",
    2: "CLOSE",
    3: "UPDATE",
    4: "SHOW",
    5: "HIDE",
    6: "DLG_OPEN",
    7: "DLG_CLOSE",
    8: "DLG_UPDATE",
    9: "RELOAD",
    10: "BALLOON_OPEN",
    11: "BALLOON_CLOSE",
    12: "TAB_SWITCH",
    13: "DLG_DOWNLOAD",
    14: "BUZZER_ON",
    15: "BUZZER_OFF",
    1000: "LOGIN",
    1001: "LOGOUT",
    1002: "SCREENSAVER_ON",
    1003: "SYSSET_UPDATE",
}
PART_UPDATE = {
    100: "VISIBLED",
    101: "ENABLED",
    200: "LABEL",
    201: "TEXT",
    300: "CHECK_STATE",
    400: "SINGLE_SELIDX",
    401: "MULTI_SELIDX",
    500: "COMBO_STRS",
    501: "COMBO_NUMS",
    600: "LIST_COLUMN",
    601: "LIST_ROW",
    602: "LIST_CELL",
    700: "TAB_NAME",
    701: "TAB_VISIBLE",
    1000: "ICONID",
    1001: "ICONID_MODE",
    1100: "CALENDAR",
    1200: "SCHEDULE_PANEL",
    1300: "TIMELINE_BAR",
    1400: "ICONPANEL",
    1401: "ICONPANEL_STATE",
    1402: "PNTLIST",
    1403: "PNTLIST_STATE",
    1404: "PNTLAYOUT",
    1405: "PNTLAYOUT_STATE",
    1500: "BARLINE_GRAPH",
    1501: "POINT_GRAPH",
    1502: "POINT_GRAPH_ID",
}
PART_UPDATE_CODE = {name: code for code, name in PART_UPDATE.items()}
SCREEN_UPDATE_CODE = {name: code for code, name in SCREEN_UPDATE.items()}
DIALOG = {
    1: "ERROR",
    2: "CONFIRM",
    3: "INFO",
    4: "CHARINPUT",
    5: "PASSWORD",
    6: "NUMBER",
    7: "IPADDR",
    8: "TIME",
    9: "DATE",
    10: "HOURMIN",
    11: "PROGRESS",
    12: "CALENDAR",
    13: "KEYBOARDSUPPORT",
    14: "NUMBER_EXTEND",
}


_HEX = re.compile(r"[0-9a-fA-F]+")
_TRUNCATED = re.compile(r"[0-9a-fA-F]*\0*")  # a cut-off number at the end of a reply


def is_token(text: str) -> bool:
    """Tell whether text is a page token.

    Args:
    ----
        text: The text to check.

    Returns:
    -------
        True when text is exactly 32 hex characters.

    """
    return len(text) == TOKEN_LEN and all(c in "0123456789abcdefABCDEF" for c in text)


@dataclass
class Part:
    """An update of one part (control) of a screen."""

    part: int
    type: str
    value: Any


@dataclass
class ScreenUpdate:
    """An update of one screen, with the parts that changed."""

    screen: int
    update: str
    data: list[Any] = field(default_factory=list)
    parts: list[Part] = field(default_factory=list)


@dataclass
class Reply:
    """A decoded reply: the new page token and the screen updates."""

    token: str
    screens: list[ScreenUpdate]

    def dialogs(self) -> list[list[Any]]:
        """Return the data of every dialog the reply opens."""
        return [s.data for s in self.screens if s.update == "DLG_OPEN"]


class Writer:
    """Builds a hex-encoded request body."""

    def __init__(self, token: str = "") -> None:
        """Start a request body.

        Args:
        ----
            token: The page token the body starts with.

        """
        self.buf = token

    def short(self, v: int) -> None:
        """Append a 16-bit number."""
        self.buf += f"{v & 0xFFFF:04x}"

    def int(self, v: int) -> None:
        """Append a 32-bit number."""
        self.buf += f"{v & 0xFFFFFFFF:08x}"

    def string(self, s: str) -> None:
        """Append a string, preceded by its length."""
        self.short(len(s))
        self.buf += s


class Reader:
    """Reads the hex-encoded reply.

    Every read must stay inside the reply: counts in a reply come from whatever
    answers at the iTM's address, so a read past the end is an error rather than a
    zero (which would let a tiny reply claiming a huge count spin a loop billions of
    times).
    """

    def __init__(self, buf: str) -> None:
        """Start reading a reply.

        Args:
        ----
            buf: The reply text.

        """
        self.buf, self.pos = buf, 0

    def _num(self, width: int) -> int:
        """Read a signed number of width hex characters."""
        chunk = self.buf[self.pos : self.pos + width]
        if len(chunk) == width and _HEX.fullmatch(chunk):
            self.pos += width
            v = int(chunk, 16)
            return v - (1 << (width * 4)) if v >= 1 << (width * 4 - 1) else v
        if (
            chunk
            and self.pos + len(chunk) == len(self.buf)
            and _TRUNCATED.fullmatch(chunk)
            and (len(chunk) < width or "\0" in chunk)
        ):
            # The iTM cuts off the final part count of some replies (e.g. "0000\0"
            # where 8 hex digits are due); the JS client reads that as NaN, which its
            # loops treat as 0. Tolerate exactly that: a short or malformed number
            # that is the very end of the reply. Nothing can be read after it.
            self.pos = len(self.buf)
            return 0
        if not chunk:
            msg = f"reply ends at offset {self.pos} but more data was expected"
            raise ProtocolError(msg)
        msg = f"invalid number {chunk!r} at offset {self.pos}"
        raise ProtocolError(msg)

    def short(self) -> int:
        """Read a 16-bit number."""
        return self._num(4)

    def int(self) -> int:
        """Read a 32-bit number."""
        return self._num(8)

    def count(self, *, wide: bool = True) -> builtins.int:
        """Read the number of elements that follow.

        Each element takes at least 4 characters, so a count the rest of the reply
        can't hold is rejected up front instead of failing after a long loop.

        Args:
        ----
            wide: Whether the count is a 32-bit (True) or 16-bit number.

        Returns:
        -------
            The number of elements.

        Raises:
        ------
            ProtocolError: The count does not fit the rest of the reply.

        """
        n = self._num(8 if wide else 4)
        if n < 0 or n * 4 > len(self.buf) - self.pos:
            msg = f"count {n} at offset {self.pos} does not fit the reply"
            raise ProtocolError(msg)
        return n

    def string(self) -> str:
        """Read a string, preceded by its length.

        Returns
        -------
            The string.

        Raises
        ------
            ProtocolError: The string does not fit the rest of the reply.

        """
        n = self.short()
        if n < 0 or self.pos + n > len(self.buf):
            msg = f"string of length {n} at offset {self.pos} does not fit the reply"
            raise ProtocolError(msg)
        s = self.buf[self.pos : self.pos + n]
        self.pos += n
        return s

    def raw(self, n: builtins.int) -> str:
        """Read n characters as they are."""
        s = self.buf[self.pos : self.pos + n]
        self.pos += n
        return s


def encode_event(  # noqa: PLR0913, PLR0917  # pylint: disable=too-many-arguments
    # The arguments mirror the fields of the wire format.
    token: str,
    screen: int = -1,
    part: int = -1,
    event: int = EventType.CLICK,
    data_type: int = DataType.NONE,
    number: int = -1,
    array: tuple[int, ...] = (),
    string: str = "",
) -> str:
    """Encode a GUI event into the ``snD=`` request body.

    Args:
    ----
        token: The current page token.
        screen: The screen the event is for.
        part: The part (control) of the screen the event is for.
        event: The event type.
        data_type: The type of the value sent along.
        number: The number sent along.
        array: Numbers sent along.
        string: The string sent along.

    Returns:
    -------
        The request body.

    """
    w = Writer(token)
    w.short(screen)
    w.short(part)
    w.short(event)
    w.short(data_type)
    w.int(number)
    w.short(len(array))
    for v in array:
        w.int(v)
    w.string(string)
    return "snD=" + w.buf


# -- decoding -------------------------------------------------------------------------
# Fixed layouts are written as one letter per field: N = int, H = short, S = string.
# Lowercase letters are read and dropped (fields the web UI skips as well).
_FIELD_READERS: dict[str, Callable[[Reader], Any]] = {
    "N": Reader.int,
    "H": Reader.short,
    "S": Reader.string,
}


def _fields(r: Reader, layout: str) -> list[Any]:
    """Read the fields of a fixed layout."""
    out = []
    for code in layout:
        value = _FIELD_READERS[code.upper()](r)
        if code.isupper():
            out.append(value)
    return out


def _repeat(r: Reader, layout: str) -> list[list[Any]]:
    """Read a counted list of fixed layouts."""
    return [_fields(r, layout) for _ in range(r.count())]


_DIALOG_LAYOUTS = {
    1: "SSS",  # ERROR
    2: "SSSS",  # CONFIRM
    3: "SSS",  # INFO
    4: "SSHHHHH" + "h" + "S" * 14 + "sss" + "S",  # CHARINPUT
    5: "SHHN" + "S" * 6 + "N",  # PASSWORD
    6: "NSNNNNNNHH" + "S" * 11,  # NUMBER
    7: "SS" + "S" * 4,  # IPADDR
    8: "NS" + "H" * 18 + "S" * 13,  # TIME
    9: "NS" + "H" * 9 + "h" + "S" * 13,  # DATE
    10: "NS" + "H" * 6 + "S" * 13,  # HOURMIN
    11: "SNNSS",  # PROGRESS
    14: "NSNNNNNNNHH" + "S" * 12,  # NUMBER_EXTEND
}


def _parse_calendar_dialog(r: Reader) -> list[Any]:
    """Read the CALENDAR dialog."""
    c, kind = r.count(wide=False), r.short()
    o = [c, kind, r.string(), *_fields(r, "HHHHN" + "S" * 9 + "H" * 4 * c)]
    if kind:
        o += _fields(r, "S" * 5)
        if kind == 2:
            o += _fields(r, "H" * 4)
    return o


def _parse_dialog(r: Reader, dtype: int) -> list[Any]:
    """Read the data of a dialog of type dtype."""
    if dtype == 12:
        return _parse_calendar_dialog(r)
    if dtype == 13:  # KEYBOARDSUPPORT: no payload
        return []
    layout = _DIALOG_LAYOUTS.get(dtype)
    if layout is None:
        msg = f"unknown dialog type {dtype} at offset {r.pos}"
        raise ProtocolError(msg)
    return _fields(r, layout)


def _read_vaxis(r: Reader, out: list[Any]) -> None:
    """Read a vertical graph axis."""
    e = r.int()
    out.append(e)
    if e == 1:
        n = r.count()
        out.append(n)
        for _ in range(n):
            out += [r.string(), r.int()]
        s = r.int()
        out.append(s)
        if s == 1:
            out.append(r.string())
        out += [r.int(), r.int()]


def _read_haxis(r: Reader, pt: int, out: list[Any]) -> None:
    """Read a horizontal graph axis."""
    e = r.int()
    out.append(e)
    if e == 1:
        out.append(pt)
        n = r.count()
        out.append(n)
        for _ in range(n):
            out += [r.string(), r.int()]
        s = r.int()
        out.append(s)
        if s == 1:
            out.append(r.string())
        n = r.count()
        out.append(n)
        out += [r.string() for _ in range(n)]


def _read_axes(r: Reader) -> list[Any]:
    """Read the axes every graph starts with."""
    o: list[Any] = []
    _, pt = r.short(), r.short()
    _read_vaxis(r, o)
    _read_vaxis(r, o)
    _read_haxis(r, pt, o)
    return o


def _parse_barline_graph(r: Reader) -> list[Any]:
    """Read a BARLINE_GRAPH part."""
    o = _read_axes(r)
    for _ in range(r.count()):
        o.append(r.int())
        n = r.count()
        o.append(n)
        for _ in range(n):
            a = r.int()
            o.append(a)
            if a == 1:
                o.append(r.int() / 10)
        o += [r.string(), r.string()]
    ct = r.int()
    o.append(ct)
    if ct == 1:
        o.append(r.int() / 10)
    return o


def _parse_point_graph(r: Reader) -> list[Any]:
    """Read a POINT_GRAPH part."""
    o = _read_axes(r)
    for _ in range(r.count()):
        o.append(r.int())
        n = r.count()
        o.append(n)
        for _ in range(n):
            o += [r.int(), r.int() / 100, r.int() / 100]
    o.append(r.int())
    return o


def _parse_tab_visible(r: Reader) -> list[Any]:
    """Read a TAB_VISIBLE part."""
    u, g = r.short(), r.count(wide=False)
    return [u, [r.int() for _ in range(g)]]


def _parse_list_row(r: Reader) -> dict[str, Any]:
    """Read a LIST_ROW part."""
    nrows, ncols = r.count(wide=False), r.count(wide=False)
    rows = []
    for _ in range(nrows):
        texts = _fields(r, "S" * ncols)
        a = _fields(r, "H" * ncols)
        b = _fields(r, "H" * ncols)
        rows.append({"text": texts, "a": a, "b": b})
    return {"rows": rows, "selected": [r.int() for _ in range(r.count())]}


def _parse_calendar(r: Reader) -> list[Any]:
    """Read a CALENDAR part."""
    it, ht = r.count(wide=False), r.short()
    o = _fields(r, "HHHHN" + "S" * 7 + "H" * 4 * it)
    if ht == 1:
        o += _fields(r, "HHHH")
    return [it, ht, o]


def _parse_schedule_panel(r: Reader) -> list[Any]:
    """Read a SCHEDULE_PANEL part."""
    o = _repeat(r, "NSSS")
    return [o, r.int()]


def _parse_iconpanel(r: Reader) -> dict[str, Any]:
    """Read an ICONPANEL part: the icons of the icon view."""
    icons = _repeat(r, "SHH")
    return {"icons": icons, "selected": r.int()}


def _parse_pntlist(r: Reader) -> dict[str, Any]:
    """Read a PNTLIST part."""
    ncols = r.count()
    cols = [_fields(r, "SN") for _ in range(ncols)]
    sort = _fields(r, "HH")
    rows = [[_fields(r, "SHH") for _ in range(ncols)] for _ in range(r.count())]
    return {"cols": cols, "sort": sort, "rows": rows, "selected": r.int()}


def _parse_pntlayout(r: Reader) -> dict[str, Any]:
    """Read a PNTLAYOUT part."""
    img = _fields(r, "SSHH")
    items = []
    for _ in range(r.count()):
        a, b, kind = r.short(), r.short(), r.int()
        rest = _fields(r, "HHN" if kind == 0 else "HHNNNNS")
        items.append([a, b, kind, *rest])
    return {"img": img, "items": items, "selected": r.int()}


def _parse_pntlayout_state(r: Reader) -> list[Any]:
    """Read a PNTLAYOUT_STATE part."""
    o = []
    for _ in range(r.count()):
        a, kind = r.short(), r.short()
        o.append([a, kind, r.int() if kind == 0 else r.string()])
    return o


_PART_READERS: dict[int, Callable[[Reader], Any]] = {
    100: Reader.int,  # VISIBLED
    101: Reader.int,  # ENABLED
    200: Reader.string,  # LABEL
    201: Reader.string,  # TEXT
    300: Reader.int,  # CHECK_STATE
    400: Reader.int,  # SINGLE_SELIDX
    401: lambda r: [r.int() for _ in range(r.count())],  # MULTI_SELIDX
    500: lambda r: [r.string() for _ in range(r.count())],  # COMBO_STRS
    501: lambda r: [r.int() for _ in range(r.count())],  # COMBO_NUMS
    600: lambda r: _repeat(r, "SHHHH"),  # LIST_COLUMN
    601: _parse_list_row,  # LIST_ROW
    602: lambda r: _repeat(r, "HHSHH"),  # LIST_CELL
    700: lambda r: [r.string() for _ in range(r.count())],  # TAB_NAME
    701: _parse_tab_visible,  # TAB_VISIBLE
    1000: Reader.int,  # ICONID
    1001: lambda r: _fields(r, "HH"),  # ICONID_MODE
    1100: _parse_calendar,  # CALENDAR
    1200: _parse_schedule_panel,  # SCHEDULE_PANEL
    1300: lambda r: _repeat(r, "HHHH"),  # TIMELINE_BAR
    1400: _parse_iconpanel,  # ICONPANEL
    1401: lambda r: _repeat(r, "HH"),  # ICONPANEL_STATE
    1402: _parse_pntlist,  # PNTLIST
    1403: lambda r: _repeat(r, "HHSHH"),  # PNTLIST_STATE
    1404: _parse_pntlayout,  # PNTLAYOUT
    1405: _parse_pntlayout_state,  # PNTLAYOUT_STATE
    1500: _parse_barline_graph,  # BARLINE_GRAPH
    1501: _parse_point_graph,  # POINT_GRAPH
    1502: Reader.int,  # POINT_GRAPH_ID
}


def _parse_part(r: Reader, ptype: int) -> Any:
    """Read the value of a part update of type ptype."""
    reader = _PART_READERS.get(ptype)
    if reader is None:
        msg = f"unknown part update type {ptype} at offset {r.pos}"
        raise ProtocolError(msg)
    return reader(r)


def _parse_dialog_open(r: Reader) -> list[Any]:
    """Read the data of DLG_OPEN."""
    dlg_id, dtype = r.short(), r.short()
    return [dlg_id, DIALOG.get(dtype, dtype), *_parse_dialog(r, dtype)]


def _parse_dialog_update(r: Reader) -> list[Any]:
    """Read the data of DLG_UPDATE."""
    data = _fields(r, "HH")
    if data[1] == 11:  # PROGRESS
        data.append(r.int())
    return data


def _parse_logout(r: Reader) -> list[Any]:
    """Read the data of LOGOUT: the reason, and its details."""
    reason = r.int()
    if reason == 1:
        return [reason, *_fields(r, "SS")]
    if reason == 2:  # another web UI version is expected; carries that version
        return [reason, r.string()]
    return [reason]


_SCREEN_DATA_READERS: dict[int, Callable[[Reader], list[Any]]] = {
    0: lambda r: [r.int()],  # INIT
    1: lambda r: [r.int()],  # OPEN
    6: _parse_dialog_open,  # DLG_OPEN
    7: lambda r: _fields(r, "HH"),  # DLG_CLOSE
    8: _parse_dialog_update,  # DLG_UPDATE
    10: lambda r: _fields(r, "NS"),  # BALLOON_OPEN
    13: lambda r: _fields(r, "HH" + "S" * 5),  # DLG_DOWNLOAD
    1000: lambda r: _fields(r, "NSS"),  # LOGIN
    1001: _parse_logout,  # LOGOUT
    1003: lambda r: [r.int()],  # SYSSET_UPDATE
}
# OPEN, CLOSE, UPDATE, SHOW, HIDE and RELOAD are followed by part updates.
_WITH_PARTS = frozenset({1, 2, 3, 4, 5, 9})
# Screen update types that carry nothing besides the screen ID (script.js reads
# nothing more for them).
_NO_PAYLOAD = frozenset(
    SCREEN_UPDATE_CODE[name]
    for name in (
        "BALLOON_CLOSE",
        "TAB_SWITCH",
        "BUZZER_ON",
        "BUZZER_OFF",
        "SCREENSAVER_ON",
    )
)


def parse_reply(text: str) -> Reply:
    """Decode a /cmd/ reply. Mirrors HttpHandler.onReply in script.js.

    Args:
    ----
        text: The reply as received.

    Returns:
    -------
        The decoded reply.

    Raises:
    ------
        ProtocolError: The reply could not be decoded.

    """
    r = Reader(text)
    token = r.raw(TOKEN_LEN)
    if not is_token(token):
        msg = f"not an iTM reply: {text[:80]!r}"
        raise ProtocolError(msg)
    screens = []
    for _ in range(r.count()):
        utype, screen_id = r.short(), r.short()
        data_reader = _SCREEN_DATA_READERS.get(utype)
        if data_reader is not None:
            data = data_reader(r)
        elif utype in _WITH_PARTS or utype in _NO_PAYLOAD:
            data = []
        else:
            # Its layout is unknown, so nothing after it can be read reliably.
            msg = f"unknown screen update type {utype} at offset {r.pos}"
            raise ProtocolError(msg)
        parts = []
        if utype in _WITH_PARTS:
            for _ in range(r.count()):
                part_id, ptype = r.short(), r.short()
                ptype_name = PART_UPDATE.get(ptype, str(ptype))
                parts.append(Part(part_id, ptype_name, _parse_part(r, ptype)))
        screens.append(ScreenUpdate(screen_id, SCREEN_UPDATE[utype], data, parts))
    if r.buf[r.pos :] not in {"", "\0"}:  # replies end with a NUL
        msg = f"unexpected data after the reply at offset {r.pos}"
        raise ProtocolError(msg)
    return Reply(token, screens)
