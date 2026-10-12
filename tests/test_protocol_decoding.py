"""Decoder checks for every screen update, dialog and part layout of the iTM protocol.

Replies are built with the library's own Writer from a list of fields: ``h(v)`` is a
short, ``n(v)`` an int and a plain ``str`` a length-prefixed string.
"""

from __future__ import annotations

from typing import Any

import pytest

from daikin_itm import ProtocolError
from daikin_itm.models import (
    ICON_STATE_SETBACK,
    Capabilities,
    Unit,
    capabilities_from_dialog,
    details_from_screen,
    normalize_mode,
    unit_from_panel,
)
from daikin_itm.protocol import (
    PART_UPDATE_CODE,
    SCREEN_UPDATE_CODE,
    Part,
    Reader,
    Reply,
    ScreenUpdate,
    Writer,
    encode_event,
    is_token,
    parse_reply,
)
from daikin_itm.state import UIState

TOKEN = "0123456789abcdef" * 2
Field = tuple[str, int] | str


def h(value: int) -> Field:
    """Return a short field."""
    return ("h", value)


def n(value: int) -> Field:
    """Return an int field."""
    return ("n", value)


def encode(*fields: Field) -> str:
    """Encode fields the way the iTM does."""
    w = Writer()
    for f in fields:
        if isinstance(f, str):
            w.string(f)
        elif f[0] == "h":
            w.short(f[1])
        else:
            w.int(f[1])
    return w.buf


def screen(update: str | int, screen_id: int, *data: Field) -> list[Field]:
    """Return the fields of one screen update."""
    code = SCREEN_UPDATE_CODE[update] if isinstance(update, str) else update
    return [h(code), h(screen_id), *data]


def parse(*screens: list[Field]) -> Reply:
    """Build a reply holding the given screen updates and decode it."""
    body = "".join(encode(*s) for s in screens)
    return parse_reply(TOKEN + encode(n(len(screens))) + body)


def parse_one(update: str | int, *data: Field) -> ScreenUpdate:
    """Decode a reply with a single screen update for screen 4."""
    reply = parse(screen(update, 4, *data))
    assert reply.token == TOKEN
    assert len(reply.screens) == 1
    return reply.screens[0]


def part_value(ptype: str, *fields: Field) -> Any:
    """Decode a single part of the given type and return its value."""
    s = parse_one("UPDATE", n(1), h(5), h(PART_UPDATE_CODE[ptype]), *fields)
    assert len(s.parts) == 1
    assert s.parts[0].part == 5
    assert s.parts[0].type == ptype
    return s.parts[0].value


def layout(spec: str) -> tuple[list[Field], list[Any]]:
    """Build fields and the expected decoded values from a layout string.

    ``H``/``N``/``S`` are a short/int/string that is decoded, lowercase ones are read
    but not part of the result. Every value is unique, so misplaced reads show.
    """
    fields: list[Field] = []
    expected: list[Any] = []
    for i, kind in enumerate(spec.replace(" ", "")):
        value: Field
        decoded: Any
        if kind.upper() == "H":
            value, decoded = h(i), i
        elif kind.upper() == "N":
            value, decoded = n(-1000 - i), -1000 - i
        else:
            value = decoded = f"s{i}"
        fields.append(value)
        if kind.isupper():
            expected.append(decoded)
    return fields, expected


# -- screen updates --------------------------------------------------------------------


def test_init() -> None:
    """INIT carries one int and no parts."""
    s = parse_one("INIT", n(7))
    assert s == ScreenUpdate(4, "INIT", [7], [])


def test_open_with_parts() -> None:
    """OPEN carries an int followed by parts."""
    s = parse_one("OPEN", n(-1), n(2), h(0), h(200), "Naam", h(3), h(100), n(0))
    assert s.update == "OPEN"
    assert s.data == [-1]
    assert s.parts == [Part(0, "LABEL", "Naam"), Part(3, "VISIBLED", 0)]


@pytest.mark.parametrize("update", ["CLOSE", "UPDATE", "SHOW", "HIDE", "RELOAD"])
def test_updates_with_parts_only(update: str) -> None:
    """These updates carry no data, only parts."""
    s = parse_one(update, n(1), h(9), h(101), n(1))
    assert s.data == []
    assert s.parts == [Part(9, "ENABLED", 1)]


@pytest.mark.parametrize(
    "update",
    ["BALLOON_CLOSE", "TAB_SWITCH", "BUZZER_ON", "BUZZER_OFF", "SCREENSAVER_ON"],
)
def test_updates_without_data(update: str) -> None:
    """These updates carry neither data nor parts."""
    reply = parse(screen(update, 4), screen("INIT", 5, n(1)))
    assert reply.screens == [
        ScreenUpdate(4, update, [], []),
        ScreenUpdate(5, "INIT", [1], []),
    ]


def test_unknown_update_type() -> None:
    """An unknown update type can't be read past, so the reply is rejected."""
    with pytest.raises(ProtocolError, match="unknown screen update type 999"):
        parse(screen(999, 4), screen("INIT", 5, n(1)))


def test_unknown_dialog_type() -> None:
    """An unknown dialog type can't be read past, so the reply is rejected."""
    with pytest.raises(ProtocolError, match="unknown dialog type 99"):
        parse(screen("DLG_OPEN", 7, h(87), h(99)))


def test_dlg_close() -> None:
    """DLG_CLOSE carries the dialog id and type."""
    assert parse_one("DLG_CLOSE", h(87), h(2)).data == [87, 2]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ([h(3), h(2)], [3, 2]),
        ([h(3), h(11), n(42)], [3, 11, 42]),
    ],
)
def test_dlg_update(fields: list[Field], expected: list[int]) -> None:
    """DLG_UPDATE carries a progress value only for progress dialogs."""
    assert parse_one("DLG_UPDATE", *fields).data == expected


def test_balloon_open() -> None:
    """BALLOON_OPEN carries an int and a text."""
    assert parse_one("BALLOON_OPEN", n(5), "Let op").data == [5, "Let op"]


def test_dlg_download() -> None:
    """DLG_DOWNLOAD carries two shorts and five strings."""
    fields, expected = layout("HH SSSSS")
    assert parse_one("DLG_DOWNLOAD", *fields).data == expected


def test_login() -> None:
    """LOGIN carries an int and two strings."""
    assert parse_one("LOGIN", n(102), "Informatie", "").data == [102, "Informatie", ""]


@pytest.mark.parametrize(
    ("fields", "expected"),
    [
        ([n(1), "a", "b"], [1, "a", "b"]),
        ([n(2), "Ver1.40.00"], [2, "Ver1.40.00"]),
        ([n(3)], [3]),
    ],
)
def test_logout(fields: list[Field], expected: list[Any]) -> None:
    """LOGOUT carries a reason and reason-specific strings."""
    assert parse_one("LOGOUT", *fields).data == expected


def test_sysset_update() -> None:
    """SYSSET_UPDATE carries one int."""
    assert parse_one("SYSSET_UPDATE", n(9)).data == [9]


# -- dialogs ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dtype", "name", "spec"),
    [
        (1, "ERROR", "SSS"),
        (2, "CONFIRM", "SSSS"),
        (3, "INFO", "SSS"),
        (4, "CHARINPUT", "SS HHHHH h SSSSSSSSSSSSSS sss S"),
        (5, "PASSWORD", "S HH N SSSSSS N"),
        (6, "NUMBER", "N S NNNNNN HH SSSSSSSSSSS"),
        (7, "IPADDR", "SS SSSS"),
        (8, "TIME", "N S HHHHHHHHHHHHHHHHHH SSSSSSSSSSSSS"),
        (9, "DATE", "N S HHHHHHHHH h SSSSSSSSSSSSS"),
        (10, "HOURMIN", "N S HHHHHH SSSSSSSSSSSSS"),
        (11, "PROGRESS", "S NN SS"),
        (13, "KEYBOARDSUPPORT", ""),
        (14, "NUMBER_EXTEND", "N S NNNNNNN HH SSSSSSSSSSSS"),
    ],
)
def test_dialog_layouts(dtype: int, name: str | int, spec: str) -> None:
    """Every dialog type is decoded with its own layout."""
    fields, expected = layout(spec)
    reply = parse(
        screen("DLG_OPEN", 7, h(87), h(dtype), *fields), screen("INIT", 1, n(0))
    )
    assert reply.dialogs() == [[87, name, *expected]]
    assert reply.screens[1].data == [0]  # the dialog was read up to its end


@pytest.mark.parametrize(
    ("kind", "tail"),
    [
        (0, ""),
        (1, "SSSSS"),
        (2, "SSSSS HHHH"),
    ],
)
def test_calendar_dialog(kind: int, tail: str) -> None:
    """The calendar dialog has 4 shorts per entry and kind-specific extras."""
    fields, expected = layout(f"S HHHH N SSSSSSSSS HHHHHHHH {tail}")
    reply = parse(
        screen("DLG_OPEN", 7, h(1), h(12), h(2), h(kind), *fields),
        screen("INIT", 1, n(0)),
    )
    assert reply.dialogs() == [[1, "CALENDAR", 2, kind, *expected]]
    assert reply.screens[1].data == [0]


# -- parts -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "ptype",
    ["VISIBLED", "ENABLED", "CHECK_STATE", "SINGLE_SELIDX", "ICONID", "POINT_GRAPH_ID"],
)
def test_int_parts(ptype: str) -> None:
    """Parts holding a single (signed) int."""
    assert part_value(ptype, n(-1)) == -1


def test_iconid_mode() -> None:
    """ICONID_MODE holds two shorts."""
    assert part_value("ICONID_MODE", h(2114), h(11)) == [2114, 11]


@pytest.mark.parametrize("ptype", ["LABEL", "TEXT"])
def test_text_parts(ptype: str) -> None:
    """LABEL and TEXT hold a string."""
    assert part_value(ptype, "21.0°C") == "21.0°C"


@pytest.mark.parametrize("ptype", ["COMBO_STRS", "TAB_NAME"])
def test_string_list_parts(ptype: str) -> None:
    """COMBO_STRS and TAB_NAME hold a counted list of strings."""
    assert part_value(ptype, n(2), "Start", "Stop") == ["Start", "Stop"]


@pytest.mark.parametrize("ptype", ["MULTI_SELIDX", "COMBO_NUMS"])
def test_int_list_parts(ptype: str) -> None:
    """MULTI_SELIDX and COMBO_NUMS hold a counted list of ints."""
    assert part_value(ptype, n(3), n(19), n(20), n(-1)) == [19, 20, -1]


def test_tab_visible() -> None:
    """TAB_VISIBLE holds a short and a short-counted list of ints."""
    assert part_value("TAB_VISIBLE", h(1), h(2), n(0), n(1)) == [1, [0, 1]]


def test_list_column() -> None:
    """LIST_COLUMN holds a string and four shorts per column."""
    value = part_value("LIST_COLUMN", n(1), "Naam", h(1), h(2), h(3), h(4))
    assert value == [["Naam", 1, 2, 3, 4]]


def test_list_row() -> None:
    """LIST_ROW holds texts and two short arrays per row, then the selection."""
    value = part_value(
        "LIST_ROW",
        *(h(1), h(2)),
        *("a", "b", h(1), h(2), h(3), h(4)),
        *(n(1), n(0)),
    )
    assert value == {
        "rows": [{"text": ["a", "b"], "a": [1, 2], "b": [3, 4]}],
        "selected": [0],
    }


def test_list_cell() -> None:
    """LIST_CELL holds two shorts, a string and two shorts per cell."""
    value = part_value("LIST_CELL", n(1), h(1), h(2), "x", h(3), h(4))
    assert value == [[1, 2, "x", 3, 4]]


@pytest.mark.parametrize(("ht", "tail"), [(0, ""), (1, "HHHH")])
def test_calendar_part(ht: int, tail: str) -> None:
    """CALENDAR holds 4 shorts per item and 4 more shorts when ht is 1."""
    fields, expected = layout(f"HHHH N SSSSSSS HHHH {tail}")
    assert part_value("CALENDAR", h(1), h(ht), *fields) == [1, ht, expected]


def test_schedule_panel() -> None:
    """SCHEDULE_PANEL holds an int and three strings per entry, then an int."""
    value = part_value("SCHEDULE_PANEL", n(1), n(5), "a", "b", "c", n(9))
    assert value == [[[5, "a", "b", "c"]], 9]


def test_timeline_bar() -> None:
    """TIMELINE_BAR holds four shorts per bar."""
    assert part_value("TIMELINE_BAR", n(1), h(1), h(2), h(3), h(4)) == [[1, 2, 3, 4]]


def test_iconpanel() -> None:
    """ICONPANEL holds a name and two shorts per icon, then the selection."""
    value = part_value("ICONPANEL", n(1), "Building", h(1000), h(0), n(0))
    assert value == {"icons": [["Building", 1000, 0]], "selected": 0}


def test_iconpanel_state() -> None:
    """ICONPANEL_STATE holds two shorts per icon."""
    assert part_value("ICONPANEL_STATE", n(2), h(1), h(2), h(3), h(4)) == [
        [1, 2],
        [3, 4],
    ]


def test_pntlist() -> None:
    """PNTLIST holds columns, the sort order, rows of cells and the selection."""
    value = part_value(
        "PNTLIST",
        *(n(2), "Naam", n(1), "Status", n(2)),
        *(h(0), h(1)),
        *(n(1), "a", h(1), h(2), "b", h(3), h(4)),
        n(-1),
    )
    assert value == {
        "cols": [["Naam", 1], ["Status", 2]],
        "sort": [0, 1],
        "rows": [[["a", 1, 2], ["b", 3, 4]]],
        "selected": -1,
    }


def test_pntlist_state() -> None:
    """PNTLIST_STATE holds two shorts, a string and two shorts per entry."""
    value = part_value("PNTLIST_STATE", n(1), h(1), h(2), "x", h(3), h(4))
    assert value == [[1, 2, "x", 3, 4]]


def test_pntlayout() -> None:
    """PNTLAYOUT items have a longer layout when their kind is not 0."""
    value = part_value(
        "PNTLAYOUT",
        *("bg.png", "fg.png", h(640), h(480)),
        n(2),
        *(h(1), h(2), n(0), h(3), h(4), n(5)),
        *(h(6), h(7), n(1), h(8), h(9), n(10), n(11), n(12), n(13), "label"),
        n(1),
    )
    assert value == {
        "img": ["bg.png", "fg.png", 640, 480],
        "items": [[1, 2, 0, 3, 4, 5], [6, 7, 1, 8, 9, 10, 11, 12, 13, "label"]],
        "selected": 1,
    }


def test_pntlayout_state() -> None:
    """PNTLAYOUT_STATE holds an int for kind 0 and a string otherwise."""
    value = part_value("PNTLAYOUT_STATE", n(2), h(1), h(0), n(7), h(2), h(1), "x")
    assert value == [[1, 0, 7], [2, 1, "x"]]


AXES_FULL: list[Field] = [
    # vertical axis 1: enabled, one label, with a unit
    *(n(1), n(1), "y", n(5), n(1), "°C", n(0), n(100)),
    # vertical axis 2: disabled
    n(0),
    # horizontal axis: enabled, one label, with a unit, two names
    *(n(1), n(1), "x", n(3), n(1), "h", n(2), "a", "b"),
]
AXES_FULL_DECODED = [
    1,
    1,
    "y",
    5,
    1,
    "°C",
    0,
    100,
    0,
    1,
    7,
    1,
    "x",
    3,
    1,
    "h",
    2,
    "a",
    "b",
]
AXES_BARE: list[Field] = [
    *(n(1), n(0), n(0), n(0), n(10)),  # enabled, no labels, no unit
    n(0),
    *(n(1), n(0), n(0), n(0)),  # enabled, no labels, no unit, no names
]
AXES_BARE_DECODED = [1, 0, 0, 0, 10, 0, 1, 7, 0, 0, 0]


def test_barline_graph_all_branches() -> None:
    """BARLINE_GRAPH with axes, a value series and a current value."""
    value = part_value(
        "BARLINE_GRAPH",
        *(h(0), h(7), *AXES_FULL),
        *(n(1), n(9), n(2), n(1), n(215), n(0), "naam", "kleur"),
        *(n(1), n(205)),
    )
    assert value == [
        *AXES_FULL_DECODED,
        *(9, 2, 1, 21.5, 0, "naam", "kleur"),
        *(1, 20.5),
    ]


def test_barline_graph_bare() -> None:
    """BARLINE_GRAPH without units, series or current value."""
    value = part_value("BARLINE_GRAPH", h(0), h(7), *AXES_BARE, n(0), n(0))
    assert value == [*AXES_BARE_DECODED, 0]


def test_point_graph() -> None:
    """POINT_GRAPH holds axes, then series of points scaled by 100."""
    value = part_value(
        "POINT_GRAPH",
        *(h(0), h(7), *AXES_FULL),
        *(n(1), n(4), n(1), n(3), n(150), n(-250)),
        n(8),
    )
    assert value == [*AXES_FULL_DECODED, 4, 1, 3, 1.5, -2.5, 8]


def test_point_graph_disabled_axes() -> None:
    """POINT_GRAPH with every axis disabled."""
    value = part_value("POINT_GRAPH", h(0), h(7), n(0), n(0), n(0), n(0), n(3))
    assert value == [0, 0, 0, 3]


def test_unknown_part_type() -> None:
    """An unknown part type can't be skipped, so the reply is rejected."""
    with pytest.raises(ProtocolError, match="unknown part update type 4242"):
        parse_one("UPDATE", n(1), h(5), h(4242), n(0))


# -- reader, writer and helpers --------------------------------------------------------


@pytest.mark.parametrize("tail", ["0000\0", "00", "0000000\0", "\0"])
def test_truncated_final_count(tail: str) -> None:
    """A cut-off number at the very end of a reply reads as 0."""
    text = TOKEN + encode(n(1), *screen("UPDATE", 4)) + tail
    assert parse_reply(text).screens == [ScreenUpdate(4, "UPDATE", [], [])]


def test_truncated_number_ends_the_reply() -> None:
    """Nothing can be read after a truncated number."""
    r = Reader("12")
    assert r.short() == 0
    assert r.pos == 2
    with pytest.raises(ProtocolError, match="ends at offset 2"):
        r.short()


@pytest.mark.parametrize("text", ["zzzz", "00g00000", "zz", "12\0x"])
def test_invalid_number(text: str) -> None:
    """Non-hex characters are an error unless they are a truncated tail."""
    with pytest.raises(ProtocolError, match="invalid number"):
        Reader(text).short()


def test_read_past_end() -> None:
    """Reading where the reply has ended is an error."""
    with pytest.raises(ProtocolError, match="ends at offset 0"):
        Reader("").int()


@pytest.mark.parametrize(
    ("text", "wide"),
    [("00000002" + "0000", True), ("ffffffff", True), ("0002", False)],
)
def test_count_that_does_not_fit(text: str, *, wide: bool) -> None:
    """A count the rest of the reply can't hold is rejected."""
    with pytest.raises(ProtocolError, match="does not fit"):
        Reader(text).count(wide=wide)


def test_short_count() -> None:
    """A short count is read from 4 characters."""
    r = Reader("0001abcd")
    assert r.count(wide=False) == 1
    assert r.pos == 4


@pytest.mark.parametrize("text", ["0005abc", "ffff"])
def test_string_that_does_not_fit(text: str) -> None:
    """A string longer than the rest of the reply (or negative) is rejected."""
    with pytest.raises(ProtocolError, match="does not fit"):
        Reader(text).string()


def test_raw() -> None:
    """raw() returns characters as they are."""
    r = Reader("abcdef")
    assert r.raw(4) == "abcd"
    assert r.pos == 4


@pytest.mark.parametrize("value", [-32768, -1, 0, 1, 32767])
def test_short_round_trip(value: int) -> None:
    """Shorts, negative ones included, survive Writer -> Reader."""
    w = Writer()
    w.short(value)
    assert len(w.buf) == 4
    assert Reader(w.buf).short() == value


@pytest.mark.parametrize("value", [-(2**31), -1, 0, 1, 2**31 - 1])
def test_int_round_trip(value: int) -> None:
    """Ints, negative ones included, survive Writer -> Reader."""
    w = Writer()
    w.int(value)
    assert len(w.buf) == 8
    assert Reader(w.buf).int() == value


def test_writer_encoding() -> None:
    """Numbers are big-endian hex, strings length-prefixed."""
    w = Writer("tok")
    w.short(-1)
    w.int(255)
    w.string("ab")
    assert w.buf == "tokffff000000ff0002ab"


def test_not_a_reply() -> None:
    """Text that doesn't start with a token is rejected."""
    with pytest.raises(ProtocolError, match="not an iTM reply"):
        parse_reply("<html>error</html>" + "0" * 40)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("0" * 32, True),
        ("0123456789abcdefABCDEF0123456789", True),
        ("0" * 31, False),
        ("0" * 33, False),
        ("g" + "0" * 31, False),
    ],
)
def test_is_token(text: str, *, expected: bool) -> None:
    """A token is 32 hex characters."""
    assert is_token(text) is expected


def test_reply_dialogs() -> None:
    """dialogs() returns the data of every opened dialog."""
    reply = parse(
        screen("DLG_OPEN", 7, h(1), h(1), "a", "b", "c"),
        screen("INIT", 1, n(0)),
        screen("DLG_OPEN", 7, h(2), h(3), "d", "e", "f"),
    )
    assert reply.dialogs() == [[1, "ERROR", "a", "b", "c"], [2, "INFO", "d", "e", "f"]]


def test_encode_event_with_array() -> None:
    """Events encode every field, including an array of ints."""
    body = encode_event(TOKEN, 4, 11, 1, 1, -1, array=(3, -2), string="xy")
    assert body.startswith("snD=")
    r = Reader(body[4:])
    assert r.raw(32) == TOKEN
    assert [r.short(), r.short(), r.short(), r.short(), r.int()] == [4, 11, 1, 1, -1]
    assert r.short() == 2
    assert [r.int(), r.int()] == [3, -2]
    assert r.string() == "xy"
    assert r.pos == len(body) - 4


# -- UI state --------------------------------------------------------------------------


def test_state_merges_and_closes() -> None:
    """Parts are merged per screen and CLOSE forgets the screen."""
    state = UIState()
    state.apply(
        [
            ScreenUpdate(
                4, "OPEN", [-1], [Part(0, "LABEL", "a"), Part(9, "VISIBLED", 0)]
            ),
            ScreenUpdate(7, "UPDATE"),
            ScreenUpdate(8, "HIDE"),
        ]
    )
    state.apply([ScreenUpdate(4, "HIDE", [], [Part(0, "LABEL", "b")])])
    assert state.get(4, 0, "LABEL") == "b"
    assert state.visible(4, 9) is False
    assert state.visible(4, 0) is True
    assert 7 in state.screens
    assert 8 not in state.screens
    state.apply([ScreenUpdate(4, "CLOSE")])
    assert state.get(4, 0, "LABEL", "gone") == "gone"


def test_select_locally() -> None:
    """Only an existing selection of an open screen is changed locally."""
    state = UIState()
    state.apply([ScreenUpdate(4, "OPEN", [-1], [Part(11, "SINGLE_SELIDX", 0)])])
    state.select_locally(4, 11, 3)
    state.select_locally(4, 12, 3)
    state.select_locally(5, 11, 3)
    assert state.get(4, 11, "SINGLE_SELIDX") == 3
    assert (12, "SINGLE_SELIDX") not in state.screens[4]
    assert 5 not in state.screens


# -- models ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        (" Koelen ", "cool"),
        ("HEAT", "heat"),
        ("Drogen", "dry"),
        ("Turbo", None),
        ("", None),
    ],
)
def test_normalize_mode(label: str, expected: str | None) -> None:
    """Mode labels are mapped case-insensitively; unknown ones give None."""
    assert normalize_mode(label) == expected
    assert normalize_mode(None) is None


def _details(texts: dict[int, str]) -> dict[str, Any]:
    """Run details_from_screen on a details screen with the given texts."""
    state = UIState()
    parts = [Part(p, "TEXT", t) for p, t in texts.items()]
    state.apply([ScreenUpdate(9, "OPEN", [-1], parts)])
    return details_from_screen(state)


def test_details_from_screen() -> None:
    """The details screen gives the address, description and zones."""
    details = _details(
        {
            4: "105",
            6: "Indoor unit",
            8: "1",
            10: "1-04",
            12: " Up &amp; down ",
            14: "Zone:\nFloor 1&gt;Building\n Offices \n\nThermostaat\n",
        }
    )
    assert details == {
        "point_id": "105",
        "address": "1:1-04",
        "unit_type": "Indoor unit",
        "description": "Up & down",
        "zones": ("Floor 1>Building", "Offices"),
    }


def test_details_from_screen_empty() -> None:
    """Missing or blank fields give None, text without zones gives none."""
    details = _details({4: "", 10: "1-04", 12: "   ", 14: "Thermostaat"})
    assert details == {
        "point_id": None,
        "address": None,
        "unit_type": None,
        "description": None,
        "zones": (),
    }


def test_unit_setback_and_capabilities_key() -> None:
    """Setback follows the state icon; the capability key prefers the address."""
    unit = Unit(
        name="1:1-10",
        power="SB Laag",
        running=False,
        state_icon=ICON_STATE_SETBACK,
        setpoint=None,
        setpoint_range=(),
        room_temperature=None,
        mode=None,
        mode_label=None,
        fan_icon=None,
        fan_speed=None,
        error_code=None,
        filter_sign=False,
    )
    assert unit.setback is True
    caps = Capabilities("a", {"cool": "Koelen"}, fan_speed=False)
    assert caps.key == "a"
    assert caps.modes == ("cool",)
    assert Capabilities("a", {}, fan_speed=False, address="1:1-04").key == "1:1-04"


def test_details_zones_until_end() -> None:
    """Zones run to the end of the text when no blank line follows."""
    assert _details({14: "Zone:\nA\nB"})["zones"] == ("A", "B")


def test_capabilities_from_dialog() -> None:
    """Unknown and duplicate mode labels are skipped; fan needs a known icon."""
    state = UIState()
    parts = [
        Part(18, "COMBO_STRS", ["Koelen", "Turbo", "Cool", "Verwarmen"]),
        Part(28, "VISIBLED", 1),
        Part(31, "ICONID", 1234),
    ]
    state.apply([ScreenUpdate(7, "OPEN", [0], parts)])
    caps = capabilities_from_dialog("1:1-00", state)
    assert caps.mode_labels == {"cool": "Koelen", "heat": "Verwarmen"}
    assert caps.fan_speed is False


def test_unit_with_hidden_temperature() -> None:
    """A hidden temperature label gives no room temperature."""
    state = UIState()
    parts = [
        Part(0, "LABEL", "1:1-00"),
        Part(4, "LABEL", "21,5°C"),
        Part(4, "VISIBLED", 0),
    ]
    state.apply([ScreenUpdate(4, "OPEN", [-1], parts)])
    unit = unit_from_panel(state)
    assert unit.name == "1:1-00"
    assert unit.room_temperature is None
    assert unit.running is None
    state.apply([ScreenUpdate(4, "UPDATE", [], [Part(4, "VISIBLED", 1)])])
    assert unit_from_panel(state).room_temperature == 21.5
