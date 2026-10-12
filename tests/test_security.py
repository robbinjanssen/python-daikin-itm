# pylint: disable=protected-access
"""Regression tests for the security scan findings (F1-F5)."""

from __future__ import annotations

import stat
import time
from typing import TYPE_CHECKING

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from daikin_itm import FileSessionStore, ItmClient, ProtocolError
from daikin_itm.cli import _safe
from daikin_itm.protocol import parse_reply

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "0123456789abcdef0123456789abcdef"
# One UPDATE of screen 4 with one part update of part 0 (its type follows).
UPDATE_ONE_PART = TOKEN + "00000001" + "0003" + "0004" + "00000001" + "0000"


# -- F1/F2: counts in a reply can't drive unbounded loops ---------------------------
@pytest.mark.parametrize(
    "reply",
    [
        TOKEN + "7fffffff",  # 2^31 screens, nothing else
        # combo: an OPEN of screen 4 with one COMBO_STRS part
        TOKEN
        + "00000001"
        + "0001"
        + "0004"
        + "00000000"
        + "00000001"
        + "0000"
        + "01f4"
        + "7fffffff",
        UPDATE_ONE_PART + "0259" + "7fff" + "7fff",  # list rows
        UPDATE_ONE_PART + "00c8" + "7fff",  # string longer than reply
        TOKEN + "00000001" + "0003" + "0004" + "ffffffff",  # negative part count
    ],
    ids=["screens", "combo", "list-rows", "string", "negative-parts"],
)
def test_hostile_counts_fail_fast(reply: str) -> None:
    """Counts the reply can't hold are rejected before looping."""
    start = time.monotonic()
    with pytest.raises(ProtocolError):
        parse_reply(reply)
    assert time.monotonic() - start < 0.1


@pytest.mark.parametrize("count", ["ffff", "7fff"])
@pytest.mark.parametrize(
    "layout",
    [
        "0259{n}0001",  # LIST_ROW rows
        "02590001{n}",  # LIST_ROW columns
        "02bd0000{n}",  # TAB_VISIBLE
        "044c{n}0000",  # CALENDAR part
    ],
)
def test_short_counts_are_validated(layout: str, count: str) -> None:
    """2-byte counts are checked against the rest of the reply too."""
    with pytest.raises(ProtocolError, match="count"):
        parse_reply(UPDATE_ONE_PART + layout.format(n=count) + "00000000")


@pytest.mark.parametrize("count", ["ffff", "7fff"])
def test_calendar_dialog_count_is_validated(count: str) -> None:
    """The CALENDAR dialog's count is checked against the rest of the reply."""
    with pytest.raises(ProtocolError, match="count"):
        parse_reply(
            TOKEN + "00000001" + "0006" + "0004" + "0001" + "000c" + count + "0000"
        )


def test_unknown_update_types_are_rejected() -> None:
    """Updates of an unknown layout can't be read past."""
    with pytest.raises(ProtocolError, match="screen update type"):
        # 16 is not a known screen update
        parse_reply(TOKEN + "00000001" + "0010" + "0004")
    with pytest.raises(ProtocolError, match="dialog type"):
        # dialog type 99
        parse_reply(TOKEN + "00000001" + "0006" + "0004" + "0001" + "0063")


def test_known_updates_without_payload() -> None:
    """Updates that carry nothing besides the screen ID are accepted."""
    # BALLOON_CLOSE, TAB_SWITCH, BUZZER_ON, BUZZER_OFF, SCREENSAVER_ON, and a
    # KEYBOARDSUPPORT dialog.
    updates = [
        "000b0004",
        "000c0004",
        "000e0004",
        "000f0004",
        "03ea0004",
        "00060004" + "0001" + "000d",
    ]
    reply = parse_reply(TOKEN + f"{len(updates):08x}" + "".join(updates) + "\0")
    assert [s.update for s in reply.screens] == [
        "BALLOON_CLOSE",
        "TAB_SWITCH",
        "BUZZER_ON",
        "BUZZER_OFF",
        "SCREENSAVER_ON",
        "DLG_OPEN",
    ]
    assert reply.screens[-1].data == [1, "KEYBOARDSUPPORT"]


def test_data_after_the_reply_is_rejected() -> None:
    """Only a trailing NUL may follow the last screen update."""
    valid = TOKEN + "00000001" + "0003" + "0004" + "00000000"
    assert parse_reply(valid).screens[0].update == "UPDATE"
    assert parse_reply(valid + "\0").screens[0].update == "UPDATE"
    with pytest.raises(ProtocolError, match="after the reply"):
        parse_reply(valid + "0003000400000000")


def test_reading_past_the_end_is_an_error() -> None:
    """A reply that stops early is an error, not a zero."""
    # One screen update announced, but the reply stops right after the count.
    with pytest.raises(ProtocolError):
        parse_reply(TOKEN + "00000001")


def test_python_int_leniency_is_not_accepted() -> None:
    """Numbers must be plain hex digits."""
    with pytest.raises(ProtocolError):
        # int("0x000001", 16) would happily accept this
        parse_reply(TOKEN + "0x000001")


async def test_oversized_reply_is_refused() -> None:
    """Replies over 1 MB are refused while reading."""

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(text=TOKEN + "0" * 2_000_000)

    app = web.Application()
    app.router.add_post("/cmd/", handler)
    async with TestServer(app) as server:
        client = ItmClient(f"{server.host}:{server.port}", "u", "p")
        try:
            with pytest.raises(ProtocolError, match="larger"):
                await client._post("snD=")
        finally:
            await client.close()


async def test_reply_is_read() -> None:
    """A normal reply is returned as text."""

    async def handler(_request: web.Request) -> web.Response:
        return web.Response(text=TOKEN + "00000000")

    app = web.Application()
    app.router.add_post("/cmd/", handler)
    async with TestServer(app) as server:
        client = ItmClient(f"{server.host}:{server.port}", "u", "p")
        try:
            assert await client._post("snD=") == TOKEN + "00000000"
        finally:
            await client.close()


# -- F3: an https:// iTM address is used as given -----------------------------------
def test_https_address_is_kept() -> None:
    """A full URL is kept, a bare host gets http://."""
    assert ItmClient("https://itm.example", "u", "p")._url == "https://itm.example/cmd/"
    assert ItmClient("10.0.0.5:8080", "u", "p")._url == "http://10.0.0.5:8080/cmd/"


# -- F4: the session token file is private and bound to its host --------------------
async def test_session_file_is_owner_only(tmp_path: Path) -> None:
    """The token file is 0600 in a 0700 directory and bound to its host."""
    path = tmp_path / "state" / "session.json"
    store = FileSessionStore(str(path), "10.0.0.5")
    await store.save("a" * 32)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert await store.load() == "a" * 32
    # a token is never handed to another host
    assert await FileSessionStore(str(path), "10.0.0.6").load() is None
    await store.save(None)
    assert not path.exists()
    assert not list(path.parent.iterdir())


async def test_session_file_missing_or_corrupt(tmp_path: Path) -> None:
    """A missing or unreadable token file means no token."""
    path = tmp_path / "session.json"
    assert await FileSessionStore(str(path)).load() is None
    path.write_text("{not json", encoding="utf-8")
    assert await FileSessionStore(str(path)).load() is None
    await FileSessionStore(str(path)).save(None)
    await FileSessionStore(str(path)).save(None)  # removing twice is fine
    assert not path.exists()


async def test_session_file_write_failure_leaves_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write removes its temporary file."""

    def fail(*_args: object, **_kwargs: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr("daikin_itm.client.json.dump", fail)
    with pytest.raises(OSError, match="disk full"):
        await FileSessionStore(str(tmp_path / "session.json")).save("a" * 32)
    assert not list(tmp_path.iterdir())  # noqa: ASYNC240 - a test assertion


# -- F5: device strings can't drive the terminal ------------------------------------
def test_cli_output_is_sanitized() -> None:
    """Control and format characters are replaced."""
    assert _safe("Lobby\x1b[2K\x1b]0;pwned\x07") == "Lobby?[2K?]0;pwned?"
    assert _safe("Room1\u202eevil") == "Room1?evil"  # right-to-left override
    assert _safe("Meeting room 21.5°C") == "Meeting room 21.5°C"
