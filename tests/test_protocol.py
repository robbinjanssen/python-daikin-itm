"""Decoder/encoder checks against replies and requests captured from the real iTM."""

import json
from pathlib import Path
from typing import Any

from daikin_itm.models import unit_from_panel
from daikin_itm.protocol import DataType, EventType, encode_event, parse_reply
from daikin_itm.state import UIState

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_text(name: str) -> str:
    """Return the content of a fixture file."""
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str) -> Any:
    """Return the parsed content of a JSON fixture file."""
    return json.loads(fixture_text(name))


def test_login_reply_decodes_completely() -> None:
    """The login reply holds the full initial UI."""
    reply = parse_reply(fixture_text("login_reply.txt"))
    kinds = [(s.screen, s.update) for s in reply.screens]
    assert kinds == [
        (-1, "LOGIN"),
        (-1, "INIT"),
        (-1, "SYSSET_UPDATE"),
        (0, "OPEN"),
        (4, "OPEN"),
        (1, "OPEN"),
        (2, "OPEN"),
        (2, "HIDE"),
        (1203, "CLOSE"),
    ]
    icons = next(
        p.value
        for s in reply.screens
        if s.screen == 1
        for p in s.parts
        if p.type == "ICONPANEL"
    )
    assert [i[0] for i in icons["icons"]][:3] == ["Building", "Server room", "Offices"]


def test_truncated_tail_is_tolerated() -> None:
    """The iTM cuts off the last part count of some replies (a NUL instead).

    The browser treats the NaN as 0, so do we.
    """
    reply = parse_reply(fixture_text("login_reply.txt"))
    assert reply.screens[-1].parts == []


def test_browser_requests_encode_identically() -> None:
    """Requests are encoded exactly like the browser does."""
    for x in fixture_json("browser_session.json"):
        body = x["request"]
        if body is None:
            continue  # the login request is not kept in the fixtures
        token, rest = body[4:36], body[36:]
        fields = [int(rest[i : i + 4], 16) for i in range(0, 16, 4)]
        number = int(rest[16:24], 16)
        number = number - (1 << 32) if number >= 1 << 31 else number
        screen, part, event, dtype = [f - 0x10000 if f >= 0x8000 else f for f in fields]
        string = rest[32:]
        assert (
            encode_event(token, screen, part, event, dtype, number, string=string)
            == body
        )


def test_browser_setpoint_change_sequence() -> None:
    """The HAR shows: pick a setpoint on screen 4 part 11, then a TIMER commit."""
    reqs = [x["request"] for x in fixture_json("browser_session.json") if x["request"]]
    tails = [r[36:] for r in reqs]
    assert encode_event("", 4, 11, EventType.CLICK, DataType.INT, 1)[4:] in tails
    assert encode_event("", 4, -1, EventType.TIMER)[4:] in tails


def test_units_from_captured_session() -> None:
    """Every unit of a captured session is read correctly."""
    state = UIState()
    # The capture holds only the deltas after login; the login reply carries the
    # full panel.
    state.apply(parse_reply(fixture_text("login_reply.txt")).screens)
    units = {}
    for x in fixture_json("units_session.json"):
        state.apply(parse_reply(x["reply"]).screens)
        if x["tag"].startswith("unit "):
            u = unit_from_panel(state)
            units[u.name] = u
    assert len(units) == 16
    u = units["1:1-00"]
    assert (u.power, u.running, u.setpoint, u.room_temperature, u.mode) == (
        "Start",
        True,
        21,
        20.4,
        "heat",
    )
    assert units["1:1-02"].running is False
    assert units["1:1-02"].mode == "cool"
    assert units["1:1-10"].setback
    assert units["1:1-10"].power == "SB Laag"
    assert units["1:1-04"].setpoint_range == (20, 21, 22, 23, 24, 25)


def test_poll_reply_decodes() -> None:
    """A polling reply updates the clock and the icon states."""
    reply = parse_reply(fixture_text("poll_reply.txt"))
    parts = [(s.screen, p.part, p.type) for s in reply.screens for p in s.parts]
    assert parts == [(0, 6, "LABEL"), (1, 6, "ICONPANEL_STATE")]


def test_browser_replies_decode() -> None:
    """Every captured browser reply decodes."""
    for x in fixture_json("browser_session.json"):
        assert parse_reply(x["reply"]).token == x["reply"][:32]
