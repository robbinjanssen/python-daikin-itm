# pylint: disable=protected-access,redefined-outer-name
"""Own settings (1205), a web user's allowed screens (1201), the zone error (1606)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

import pytest

from daikin_itm import ItmCommandError, LoginRejected, ReadOnlyError
from daikin_itm.users_edit import (
    WebUserScreens,
    leave_own_settings,
    open_own_settings,
    set_own_settings,
)
from daikin_itm.zones_edit import set_zone_members, zone_error_from_screen

from .fake_users import FakeUsersITM
from .fake_zones import FakeZonesITM
from .test_client import FakeClient

if TYPE_CHECKING:
    from .fake_itm import Update

NEW_PASSWORD = "n3w-placeholder"


@pytest.fixture
def itm() -> FakeUsersITM:
    """Return a simulated iTM with own settings and allowed screens."""
    return FakeUsersITM()


# -- own settings (1205) ------------------------------------------------------------


async def test_fetch_and_set_own_settings(itm: FakeUsersITM) -> None:
    """Read 1205; change the region picks, each read back in a fresh session."""
    c = FakeClient(itm)
    own = await c.fetch_own_settings()
    assert (own.language, own.time_24h, own.decimal_comma, own.icon_colour) == (
        "en",
        True,
        False,
        1,
    )
    assert itm.session is None  # leaving 1205 ended the session
    result = await c.set_own_settings(
        date_format="%Y/%m/%d", time_24h=False, decimal_comma=True, icon_colour=2
    )
    assert result is not None
    assert (result.date_format, result.time_24h, result.decimal_comma) == (
        "%Y/%m/%d",
        False,
        True,
    )
    assert result.icon_colour == 2
    assert itm.own == {"date": 2, "24h": False, "comma": True, "colour": 2}
    assert itm.session is None


async def test_own_language(itm: FakeUsersITM) -> None:
    """The language screen opened from 1205, also read back there as a code."""
    c = FakeClient(itm, allow_experimental=False)
    assert (await c.fetch_own_settings()).language == "en"
    result = await c.set_own_settings(language="nl", time_24h=False, decimal_comma=True)
    assert result is not None
    assert (result.language, result.time_24h, result.decimal_comma) == (
        "nl",
        False,
        True,
    )
    assert itm.language_radio == 9
    assert itm.session is None
    result = await c.set_own_settings(language="fr")
    assert result is not None
    assert result.language == "fr"


async def test_own_password_is_sent_never_planned_or_logged(
    itm: FakeUsersITM, caplog: pytest.LogCaptureFixture
) -> None:
    """The new password: entered (and repeated), then the client logs in with it."""
    caplog.set_level(logging.DEBUG)
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_own_settings(password=NEW_PASSWORD, time_24h=False) is None
    assert any("own password = ***" in line for line in dry.planned)
    assert any("save own settings" in line for line in dry.planned)
    assert all(NEW_PASSWORD not in line for line in dry.planned)
    assert itm.own_password_changes == 0
    assert itm.own["24h"] is True
    c = FakeClient(itm)
    assert (
        await c.set_own_settings(password=NEW_PASSWORD, allow_password_change=True)
        is not None
    )
    assert itm.own_password_changes == 1
    assert c._password == NEW_PASSWORD
    assert await c.fetch_language() == "en"  # a normal login with the new password
    assert NEW_PASSWORD not in caplog.text


async def test_own_settings_refusals(itm: FakeUsersITM) -> None:
    """Read-only, nothing to change, bad values, a refused password."""
    ro = FakeClient(itm, read_only=True)
    with pytest.raises(ReadOnlyError):
        await ro.set_own_settings(time_24h=False)
    assert not itm.requests
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="nothing to change"):
        await c.set_own_settings()
    with pytest.raises(ItmCommandError, match="date format"):
        await c.set_own_settings(date_format="%Y")
    with pytest.raises(ItmCommandError, match="icon colour"):
        await c.set_own_settings(icon_colour=3)
    with pytest.raises(ItmCommandError, match="unknown language"):
        await c.set_own_settings(language="xx")
    with pytest.raises(ItmCommandError, match="allow_password_change"):
        await c.set_own_settings(password=NEW_PASSWORD)
    with pytest.raises(ItmCommandError, match="4-15 characters"):
        await c.set_own_settings(password="abc", allow_password_change=True)
    assert itm.session is None
    assert itm.own_password_changes == 0


async def test_own_session_kept_by_the_itm(itm: FakeUsersITM) -> None:
    """When leaving 1205 doesn't end the session, the client logs out itself."""
    itm.own_ends_session = False
    c = FakeClient(itm)
    assert (await c.fetch_own_settings()).icon_colour == 1
    assert itm.session is None
    assert await c.set_own_settings(icon_colour=2) is not None
    assert itm.own["colour"] == 2
    assert itm.session is None


async def test_own_settings_not_opened_logs_out(itm: FakeUsersITM) -> None:
    """A login that works but doesn't open 1205 is logged out, never left taken."""
    itm.own_opens = False
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="logged in, but"):
        await c.fetch_own_settings()
    assert itm.session is None
    itm.own_opens = True
    assert (await c.fetch_own_settings()).icon_colour == 1


async def test_own_settings_session_level(itm: FakeUsersITM) -> None:
    """Session-level: wrong repeat refused; read-only; dry run of a language."""
    c = FakeClient(itm)
    s = c._new_session()
    await open_own_settings(s, "user", "secret")
    itm.own_repeat_mismatch = True
    with pytest.raises(ItmCommandError, match="refused the password"):
        await set_own_settings(s, password=NEW_PASSWORD)
    await leave_own_settings(s)
    await leave_own_settings(s)  # already over: nothing is sent
    assert itm.session is None
    ro = FakeClient(itm, read_only=True)
    s = ro._new_session()
    await open_own_settings(s, "user", "secret")
    with pytest.raises(ReadOnlyError):
        await set_own_settings(s, icon_colour=2)
    await leave_own_settings(s)
    dry = FakeClient(itm, dry_run=True)
    s = dry._new_session()
    await open_own_settings(s, "user", "secret")
    assert await set_own_settings(s, language="de") is False
    assert itm.language_radio == 2
    assert any("pick own language de" in line for line in dry.planned)
    assert itm.session is None


async def test_own_read_back_fails(itm: FakeUsersITM) -> None:
    """A read-back that can't log in keeps the old password and says so."""
    c = FakeClient(itm)
    itm.own_ignore_password = True  # the iTM drops the new password
    with pytest.raises(ItmCommandError, match="could not log in again"):
        await c.set_own_settings(password=NEW_PASSWORD, allow_password_change=True)
    assert c._password == "secret"


async def test_own_read_back_fails_after_the_change(
    itm: FakeUsersITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The iTM took the new password but the read-back can't log in: keep it."""
    c = FakeClient(itm)

    async def busy() -> None:
        msg = "another web session is active"
        raise LoginRejected(msg)

    monkeypatch.setattr(c, "fetch_own_settings", busy)
    with pytest.raises(ItmCommandError, match="keeps the new password"):
        await c.set_own_settings(password=NEW_PASSWORD, allow_password_change=True)
    assert itm.own_password_changes == 1
    assert c._password == NEW_PASSWORD


# -- a web user's allowed screens (1201) ----------------------------------------------


async def test_web_user_screens(itm: FakeUsersITM) -> None:
    """Read and set a normal user's screens; each change read back."""
    c = FakeClient(itm)
    assert await c.fetch_web_user_screens("viewer") == WebUserScreens(
        allowed=("Screen A",), available=("Screen B", "Screen C")
    )
    result = await c.set_web_user_screens("viewer", ["Screen B", "Screen C"])
    assert result is not None
    assert set(result.allowed) == {"Screen B", "Screen C"}
    assert itm.user_screens["viewer"] == ["Screen B", "Screen C"]
    assert itm.open_screens == set()
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_web_user_screens("viewer", ["Screen A"]) is None
    assert any("allowed screens of web user viewer" in p for p in dry.planned)
    assert itm.user_screens["viewer"] == ["Screen B", "Screen C"]
    with pytest.raises(ReadOnlyError):
        await FakeClient(itm, read_only=True).set_web_user_screens("viewer", [])
    assert itm.open_screens == set()


async def test_web_user_screens_refusals(itm: FakeUsersITM) -> None:
    """Administrators (by list or by 1204), unknown users and screens, own account."""
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="administrator"):
        await c.fetch_web_user_screens("admin")
    itm.web_users.append(["guest", "Onbekend"])  # a type label the library can't read
    itm.screens_disabled_for.add("guest")
    with pytest.raises(ItmCommandError, match="administrator"):
        await c.set_web_user_screens("guest", ["Screen A"])
    with pytest.raises(ItmCommandError, match="not found"):
        await c.fetch_web_user_screens("nobody")
    with pytest.raises(ItmCommandError, match="not offered"):
        await c.set_web_user_screens("viewer", ["Screen X"])
    with pytest.raises(ItmCommandError, match="account this client logs in with"):
        await c.set_web_user_screens("user", ["Screen A"])
    assert itm.user_screens["viewer"] == ["Screen A"]
    assert itm.open_screens == set()


async def test_web_user_screens_not_taken(
    itm: FakeUsersITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A change the iTM doesn't keep is reported."""
    c = FakeClient(itm)
    keep = dict(itm.user_screens)

    def drop(*_args: object) -> None:
        itm.user_screens.clear()
        itm.user_screens.update(keep)

    original = itm._event

    def event(ev: dict[str, object]) -> list[Update]:
        updates = original(ev)
        if ev["screen"] == 1204 and ev["part"] == 30:
            drop()
        return updates

    monkeypatch.setattr(itm, "_event", event)
    with pytest.raises(ItmCommandError, match="did not take the screens"):
        await c.set_web_user_screens("viewer", ["Screen B"])


# -- the zone error screen (1606) -----------------------------------------------------


async def test_zone_error_is_reported() -> None:
    """A zone error screen opened by the iTM is closed and raised with its text."""
    itm = FakeZonesITM()
    itm.zone_error = "Zone limit reached"
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="Zone limit reached"):
        async with c._session() as s:
            await set_zone_members(s, "Zone A", (itm.units[0].name,))
    assert itm.open_screens == set()
    assert itm.zone("Zone A").points == []
    async with c._session() as s:
        assert zone_error_from_screen(s.state) is None
        assert await set_zone_members(s, "Zone A", (itm.units[0].name,)) is True
    assert itm.zone("Zone A").points == [itm.units[0].name]


async def test_own_settings_not_taken(
    itm: FakeUsersITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Settings the iTM doesn't keep are reported after the read-back."""
    monkeypatch.setattr(itm, "_apply_own", lambda _o: None)
    with pytest.raises(ItmCommandError, match="did not take the own settings"):
        await FakeClient(itm).set_own_settings(language="en", icon_colour=2)


async def test_own_buttons_without_answer(
    itm: FakeUsersITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A password or language button that opens nothing is reported, not waited on."""
    original = itm._own_event

    def own_event(part: int, event: int, ev: dict[str, object]) -> list[Update]:
        if part in {3, 7}:
            return [("UPDATE", 1205, [], [])]
        return original(part, event, ev)

    monkeypatch.setattr(itm, "_own_event", own_event)
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="no password input"):
        await c.set_own_settings(password=NEW_PASSWORD, allow_password_change=True)
    with pytest.raises(ItmCommandError, match="screen 2501 did not open"):
        await c.set_own_settings(language="en")
    assert itm.session is None


async def test_web_user_screens_odd_answers(
    itm: FakeUsersITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A screen listed twice, an ignored move, a screens button without answer."""
    c = FakeClient(itm)
    itm.user_screens["viewer"] = ["Screen A", "Screen A"]
    with pytest.raises(ItmCommandError, match="not found once"):
        await c.set_web_user_screens("viewer", ["Screen B"])
    itm.user_screens["viewer"] = []
    original = itm._screens_event

    def screens_event(ev: dict[str, object]) -> list[Update]:
        if ev["part"] == 4:  # add: ignored
            itm.us_edit.pop("pick", None)
            return [("UPDATE", 1201, [], itm._screen_parts())]
        return original(ev)

    monkeypatch.setattr(itm, "_screens_event", screens_event)
    with pytest.raises(ItmCommandError, match="did not become"):
        await c.set_web_user_screens("viewer", ["Screen B"])
    monkeypatch.setattr(itm, "_open_screens", lambda: [("UPDATE", 1204, [], [])])
    with pytest.raises(ItmCommandError, match="allowed screens did not open"):
        await c.fetch_web_user_screens("viewer")
    assert itm.open_screens == set()


async def test_zone_error_on_zone_settings() -> None:
    """The zone settings' OK reports a zone error too, and leaves no screen open."""
    itm = FakeZonesITM()
    itm.zone_error = "Zone limit reached"
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="Zone limit reached"):
        async with c._session() as s:
            await s.set_zone_attributes(
                "Zone A", name=None, info=None, start_interval=None, stop_interval=None
            )
    assert itm.open_screens == set()
