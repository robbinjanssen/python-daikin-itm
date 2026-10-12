# pylint: disable=protected-access,redefined-outer-name
"""Client behaviour against the simulated iTM (tests/fake_itm.py)."""

from __future__ import annotations

import asyncio
from typing import Any

import aiohttp
import pytest
from multidict import CIMultiDict, CIMultiDictProxy
from yarl import URL

from daikin_itm import (
    DEFAULT_ZONE_PATH,
    ItmClient,
    ItmCommandError,
    ItmError,
    LoginRejected,
    MemorySessionStore,
    ProtocolError,
    SetpointOutOfRange,
    Unit,
    ZoneNotFound,
)
from daikin_itm.models import unit_from_panel
from daikin_itm.protocol import parse_reply

from .fake_itm import FakeITM


class FakeClient(ItmClient):
    """An ItmClient talking to a FakeITM, without the real-world delays."""

    def __init__(self, itm: FakeITM, **kw: Any) -> None:
        """Create a client for the simulated iTM."""
        kw.setdefault("commit_delay", 0)
        kw.setdefault("min_write_interval", 0)
        kw.setdefault("confirm_interval", 0)
        kw.setdefault("confirm_timeout", 0.5)
        kw.setdefault("zone_path", ("Building", "Indoor units"))
        super().__init__("fake", "user", "secret", **kw)
        self.itm = itm

    async def _post(self, body: str) -> str:
        await asyncio.sleep(0)
        return self.itm.handle(body)


def http_error(status: int = 500) -> aiohttp.ClientResponseError:
    """Return the error aiohttp raises for an HTTP error status."""
    url = URL("http://fake/cmd/")
    info = aiohttp.RequestInfo(url, "POST", CIMultiDictProxy(CIMultiDict()), url)
    return aiohttp.ClientResponseError(info, (), status=status)


def is_logout(body: str) -> bool:
    """Tell whether a request body is a LOGOUT event."""
    return body[36:52] == "ffffffff000a0000"


def confirmed(unit: Unit | None) -> Unit:
    """Return the unit a confirmed write returned."""
    assert unit is not None
    return unit


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM."""
    return FakeITM()


async def test_fetch_units(itm: FakeITM) -> None:
    """Read all units in one session."""
    units = await FakeClient(itm).fetch_units()
    assert list(units) == ["1:1-00", "1:1-04", "1:1-09"]
    assert units["1:1-04"].setpoint == 20
    assert units["1:1-04"].mode == "cool"
    assert units["1:1-09"].running is False
    assert itm.session is None  # logged out again


async def test_fetch_unit(itm: FakeITM) -> None:
    """Read a single unit."""
    unit = await FakeClient(itm).fetch_unit("1:1-04")
    assert unit.name == "1:1-04"
    assert unit.setpoint == 20
    assert itm.session is None


async def test_list_zones(itm: FakeITM) -> None:
    """List the zones and units at a path."""
    client = FakeClient(itm)
    assert await client.list_zones() == ["Building"]
    assert await client.list_zones(("Building",)) == ["Indoor units", "Ventilation"]


async def test_set_setpoint_running_unit(itm: FakeITM) -> None:
    """Change the setpoint of a running unit."""
    c = FakeClient(itm)
    await c.set_setpoint("1:1-04", 21)
    assert itm.units[1].setpoint == 21
    assert (await c.fetch_units())["1:1-04"].setpoint == 21
    assert itm.session is None


async def test_set_setpoint_unchanged(itm: FakeITM) -> None:
    """Setting the current setpoint again writes nothing."""
    unit = confirmed(await FakeClient(itm).set_setpoint("1:1-04", 20))
    assert unit.setpoint == 20
    assert not any(r["event"] == 3 for r in itm.requests)  # no commit


async def test_setpoint_refused_while_stopped(itm: FakeITM) -> None:
    """The iTM ignores setpoints of stopped units, so the client refuses them."""
    with pytest.raises(ItmCommandError, match="stopped"):
        await FakeClient(itm).set_setpoint("1:1-09", 17)
    assert itm.units[2].setpoint == 16
    assert itm.session is None


async def test_own_selection_is_tracked_within_a_session(itm: FakeITM) -> None:
    """The iTM never echoes a value the client picked; the client must track it."""
    c = FakeClient(itm)
    async with c._session() as s:
        await s.open_zone(c.zone_path)
        await s.select_by_name("1:1-04")
        await s.choose(11, 1)
        await s.commit(0)
        await s.select_by_name("1:1-00")
        await s.select_by_name("1:1-04")
        assert unit_from_panel(s.state).setpoint == 21


async def test_setpoint_out_of_range(itm: FakeITM) -> None:
    """A setpoint outside the unit's range is refused, with the range it accepts."""
    with pytest.raises(SetpointOutOfRange) as err:
        await FakeClient(itm).set_setpoint("1:1-04", 30)
    assert err.value.allowed == (20, 21, 22, 23, 24, 25)
    assert (err.value.unit, err.value.value) == ("1:1-04", 30)
    assert isinstance(err.value, ItmCommandError)
    assert itm.session is None


async def test_setpoint_range_is_checked_as_read_while_writing(itm: FakeITM) -> None:
    """The range read in the write session counts, not an earlier read."""
    client = FakeClient(itm)
    assert (await client.fetch_units())["1:1-04"].setpoint_range[0] == 20
    itm.units[1].setpoint_range = (21, 22, 23, 24)  # e.g. after a mode change
    itm.units[1].setpoint = 22
    with pytest.raises(SetpointOutOfRange) as err:
        await client.set_setpoint("1:1-04", 20)
    assert err.value.allowed == (21, 22, 23, 24)


async def test_unknown_unit(itm: FakeITM) -> None:
    """A unit that is not in the zone is an error."""
    with pytest.raises(ItmCommandError):
        await FakeClient(itm).set_setpoint("nope", 21)
    assert itm.session is None


async def test_power(itm: FakeITM) -> None:
    """Start and stop a unit."""
    c = FakeClient(itm)
    await c.set_power("1:1-09", on=True)
    assert itm.units[2].running is True
    await c.set_power("1:1-09", on=False)
    assert itm.units[2].running is False


async def test_apply_nothing(itm: FakeITM) -> None:
    """Applying no changes does not log in."""
    assert await FakeClient(itm).apply("1:1-04") is None
    assert itm.requests == []


async def test_apply_unknown_fan_speed(itm: FakeITM) -> None:
    """An unknown fan speed is refused before logging in."""
    with pytest.raises(ItmCommandError, match="unknown fan speed"):
        await FakeClient(itm).set_fan_speed("1:1-04", "turbo")
    assert itm.requests == []


async def test_login_rejected_when_other_session_active(itm: FakeITM) -> None:
    """The iTM allows one web session at a time."""
    itm.session = "f" * 32  # someone is logged in through the browser
    with pytest.raises(LoginRejected):
        await FakeClient(itm).fetch_units()


async def test_wrong_password() -> None:
    """Wrong credentials are rejected."""
    itm = FakeITM(password="other")
    with pytest.raises(LoginRejected):
        await FakeClient(itm).fetch_units()
    assert itm.session is None


async def test_logout_after_error_mid_session(itm: FakeITM) -> None:
    """An error during a session still logs out."""
    c = FakeClient(itm, zone_path=("Building", "does-not-exist"))
    with pytest.raises(ItmCommandError):
        await c.fetch_units()
    assert itm.session is None


async def test_logout_when_cancelled(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cancelled call still logs out."""
    c = FakeClient(itm)
    started = asyncio.Event()
    orig = c._post

    async def slow_post(body: str) -> str:
        # A request in flight while logged in: cancelled before the iTM answered.
        if itm.session and not started.is_set():
            started.set()
            await asyncio.sleep(0.2)
        return await orig(body)

    monkeypatch.setattr(c, "_post", slow_post)
    task = asyncio.create_task(c.fetch_units())
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert itm.session is None


async def test_network_error_keeps_token_and_next_run_releases_it(
    itm: FakeITM,
) -> None:
    """A logout that never reaches the iTM keeps the token for the next run."""
    store = MemorySessionStore()
    c = FakeClient(itm, store=store)
    async with c._session() as s:
        await s.open_zone(c.zone_path)
        # The logout never reaches the iTM.
        itm.fail_next = aiohttp.ClientConnectionError("gone")
        with pytest.raises(aiohttp.ClientConnectionError):
            await s.logout()
    assert itm.session is not None
    assert store.token == itm.session
    # A new client (e.g. after a restart) frees the stale session before logging in.
    units = await FakeClient(itm, store=store).fetch_units()
    assert len(units) == 3
    assert itm.session is None
    assert store.token is None


async def test_release_saved_session_without_token(itm: FakeITM) -> None:
    """Without a saved token there is nothing to release."""
    assert await FakeClient(itm).release_saved_session() is False
    assert itm.requests == []


async def test_token_saved_after_every_reply(itm: FakeITM) -> None:
    """The store always holds the current token while logged in."""
    store = MemorySessionStore()
    c = FakeClient(itm, store=store)
    async with c._session():
        assert store.token == itm.session
    assert store.token is None


async def test_session_ended_by_the_itm(itm: FakeITM) -> None:
    """A LOGOUT update in a reply ends the session."""
    c = FakeClient(itm)
    async with c._session() as s:
        itm.session = "e" * 32  # e.g. the iTM timed the session out
        with pytest.raises(ItmError, match="ended the session"):
            await s.open_zone(c.zone_path)
        assert not s.logged_in


async def test_fetch_units_fan_speed(itm: FakeITM) -> None:
    """The fan speed is read from the fan icon."""
    units = await FakeClient(itm).fetch_units()
    assert units["1:1-04"].fan_speed == "medium"


async def test_capabilities(itm: FakeITM) -> None:
    """Read the modes and fan control every unit offers."""
    caps = await FakeClient(itm).fetch_capabilities()
    assert caps["1:1-04"].modes == ("fan", "cool", "heat")
    assert caps["1:1-04"].fan_speed
    assert caps["1:1-09"].modes == ("fan", "cool", "heat", "dry", "auto")
    assert not caps["1:1-09"].fan_speed
    assert itm.dialog is None  # every dialog cancelled
    assert itm.session is None  # and logged out


async def test_set_mode(itm: FakeITM) -> None:
    """Change the operation mode."""
    c = FakeClient(itm)
    await c.set_mode("1:1-04", "heat")
    assert itm.units[1].mode == "Verwarmen"
    assert (await c.fetch_units())["1:1-04"].mode == "heat"


async def test_set_mode_not_offered(itm: FakeITM) -> None:
    """A mode the unit does not offer is refused and the dialog cancelled."""
    with pytest.raises(ItmCommandError):
        await FakeClient(itm).set_mode("1:1-04", "dry")
    assert itm.units[1].mode == "Koelen"
    assert itm.dialog is None
    assert itm.session is None


async def test_set_fan_speed(itm: FakeITM) -> None:
    """Cycle through every fan speed."""
    c = FakeClient(itm)
    for speed in ("high", "auto", "low", "medium"):
        await c.set_fan_speed("1:1-04", speed)
        assert (await c.fetch_units())["1:1-04"].fan_speed == speed


async def test_set_fan_speed_unsupported(itm: FakeITM) -> None:
    """A unit without fan control refuses fan speed changes."""
    with pytest.raises(ItmCommandError):
        await FakeClient(itm).set_fan_speed("1:1-09", "low")
    assert itm.dialog is None
    assert itm.session is None


async def test_apply_combined(itm: FakeITM) -> None:
    """Power and mode change in one write."""
    c = FakeClient(itm)
    await c.apply("1:1-09", power=True, mode="heat")
    u = itm.units[2]
    assert u.running
    assert u.mode == "Verwarmen"


async def test_write_waits_until_change_reaches_unit(itm: FakeITM) -> None:
    """A write returns once a fresh read shows the change."""
    itm.latency_logins = 3  # the change shows up only on the 3rd read after the write
    c = FakeClient(itm)
    await c.set_fan_speed("1:1-04", "low")
    assert itm.units[1].fan_icon == 3009
    await c.set_setpoint("1:1-04", 22)
    assert itm.units[1].setpoint == 22


async def test_write_not_confirmed(itm: FakeITM) -> None:
    """A change that never shows is an error."""
    itm.latency_logins = 10_000
    with pytest.raises(ItmCommandError, match="not confirmed"):
        await FakeClient(itm, confirm_timeout=0).set_mode("1:1-04", "heat")
    assert itm.session is None


async def test_capabilities_include_identity(itm: FakeITM) -> None:
    """Capabilities carry the unit's identity from the Details screen."""
    caps = await FakeClient(itm).fetch_capabilities()
    c = caps["1:1-04"]
    assert (c.point_id, c.address, c.unit_type, c.description) == (
        "105",
        "1:1-04",
        "Indoor unit",
        "Meeting room",
    )
    assert c.zones == ("Floor 1>Building>Indoor units",)
    assert c.key == "1:1-04"


async def test_web_version_follows_the_device(
    itm: FakeITM, caplog: pytest.LogCaptureFixture
) -> None:
    """The client adopts the web UI version the iTM reports."""
    itm.web_version = "Ver1.40.00"  # e.g. after a firmware update
    c = FakeClient(itm)
    assert len(await c.fetch_units()) == 3
    assert c.web_version == "Ver1.40.00"
    assert "version changed" in caplog.text
    assert itm.session is None


async def test_writes_to_one_unit_never_overlap(itm: FakeITM) -> None:
    """Two simultaneous writes must not send the 2nd while the 1st is pending."""
    itm.latency_logins = 2
    c = FakeClient(itm)
    await asyncio.gather(c.set_fan_speed("1:1-04", "low"), c.set_setpoint("1:1-04", 23))
    assert itm.overlaps == 0
    assert itm.units[1].fan_icon == 3009
    assert itm.units[1].setpoint == 23


async def test_writes_are_spaced_per_unit(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A second write to a unit waits for min_write_interval."""
    c = FakeClient(itm, min_write_interval=60)
    await c.set_setpoint("1:1-04", 22)
    waits: list[float] = []
    sleep = asyncio.sleep

    async def fake_sleep(delay: float, result: Any = None) -> Any:
        waits.append(delay)
        return await sleep(0, result)

    monkeypatch.setattr(asyncio, "sleep", fake_sleep)
    await c.set_setpoint("1:1-04", 23)
    assert any(w > 50 for w in waits)


async def test_writes_return_the_confirmed_unit(itm: FakeITM) -> None:
    """Confirmed writes return the unit as read back."""
    c = FakeClient(itm)
    assert confirmed(await c.set_setpoint("1:1-04", 22)).setpoint == 22
    assert confirmed(await c.set_mode("1:1-04", "heat")).mode == "heat"


async def test_stale_saved_token_answered_with_http_error_is_dropped(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale token the iTM answers with an HTTP error must not block sessions."""
    store = MemorySessionStore()
    store.token = "a" * 32
    c = FakeClient(itm, store=store)
    orig = c._post

    async def post(body: str) -> str:
        if body[4:36] == "a" * 32:
            raise http_error()
        return await orig(body)

    monkeypatch.setattr(c, "_post", post)
    assert len(await c.fetch_units()) == 3
    assert store.token is None


async def test_garbage_reply_is_a_protocol_error_and_keeps_the_token(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reply that is not from the iTM never replaces the last good token."""
    assert issubclass(ProtocolError, ItmError)
    store = MemorySessionStore()
    c = FakeClient(itm, store=store)

    async def garbage(_body: str) -> str:
        return "<html>500 Internal Server Error</html>"

    async with c._session() as s:
        good = s.token
        with monkeypatch.context() as patch:
            patch.setattr(c, "_post", garbage)
            with pytest.raises(ProtocolError):
                await s.send(1, 6, 1, 1, 0)
        assert s.token == good
        assert store.token == good


def test_garbage_in_the_middle_of_a_reply_is_rejected() -> None:
    """Invalid hex inside a reply is a protocol error."""
    with pytest.raises(ProtocolError):
        parse_reply("0" * 32 + "00000001" + "zzzz" + "0003" + "00000000")


async def test_unconfirmed_write_returns_right_away(itm: FakeITM) -> None:
    """With confirm=False a write returns None right after sending it."""
    itm.latency_logins = 3
    c = FakeClient(itm)
    assert await c.set_fan_speed("1:1-04", "low", confirm=False) is None
    assert itm.units[1].fan_icon == 3011  # not on the unit yet
    assert itm.session is None


async def test_next_write_waits_for_an_unconfirmed_change(itm: FakeITM) -> None:
    """A stopped unit started without confirmation takes a setpoint once it runs."""
    itm.latency_logins = 3
    c = FakeClient(itm)
    await c.set_power("1:1-09", on=True, confirm=False)
    await c.set_setpoint("1:1-09", 20)
    assert itm.overlaps == 0
    assert itm.units[2].running
    assert itm.units[2].setpoint == 20


async def test_unconfirmed_change_that_never_shows_is_logged(
    itm: FakeITM, caplog: pytest.LogCaptureFixture
) -> None:
    """An unconfirmed change that never shows is logged by the next write."""
    itm.latency_logins = 10_000
    c = FakeClient(itm, confirm_timeout=0)
    await c.set_mode("1:1-04", "heat", confirm=False)
    itm.latency_logins = 0
    await c.set_fan_speed("1:1-04", "low")
    assert "not confirmed" in caplog.text
    assert itm.units[1].fan_icon == 3009


async def test_unknown_zone(itm: FakeITM) -> None:
    """A zone that is not there names the ones that are."""
    with pytest.raises(ZoneNotFound) as err:
        await FakeClient(itm, zone_path=("Building", "Nope")).fetch_units()
    assert err.value.available == ["Indoor units", "Ventilation"]
    assert itm.session is None


async def test_context_manager_closes_own_http_session() -> None:
    """The client closes the aiohttp session it created itself."""
    async with ItmClient("10.0.0.5", "u", "p") as client:
        client._http = aiohttp.ClientSession()
    assert client._http is None


async def test_context_manager_keeps_given_http_session() -> None:
    """A session passed in is left open for its owner."""
    async with aiohttp.ClientSession() as http:
        async with ItmClient("10.0.0.5", "u", "p", session=http):
            pass
        assert not http.closed


UNREADABLE = "f" * 32 + "zz"


@pytest.mark.parametrize(
    "failure",
    [http_error(), "<html>500 Internal Server Error</html>", UNREADABLE],
    ids=["http-500", "html-page", "unreadable-reply"],
)
async def test_unconfirmed_logout_keeps_the_token_for_the_next_run(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch, failure: Exception | str
) -> None:
    """A logout the iTM doesn't confirm may leave the session: keep the token."""
    store = MemorySessionStore()
    c = FakeClient(itm, store=store)
    orig = c._post

    async def post(body: str) -> str:
        if is_logout(body):
            if isinstance(failure, Exception):
                raise failure
            return failure
        return await orig(body)

    monkeypatch.setattr(c, "_post", post)
    with pytest.raises((aiohttp.ClientResponseError, ProtocolError)):
        await c.fetch_units()
    assert itm.session is not None
    # An unreadable reply still carries the latest token.
    expected = "f" * 32 if failure == UNREADABLE else itm.session
    assert store.token == expected
    itm.session = expected  # the iTM moved on to the token it sent along
    # The next client (e.g. after a restart) releases the session with that token
    # and carries on.
    units = await FakeClient(itm, store=store).fetch_units()
    assert len(units) == 3
    assert itm.session is None
    assert store.token is None


async def test_cancel_during_logout_holds_the_lock_until_logged_out(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The next session waits until a cancelled caller's logout has finished."""
    c = FakeClient(itm)
    orig = c._post
    in_logout = asyncio.Event()
    order = []

    async def post(body: str) -> str:
        if is_logout(body) and not in_logout.is_set():  # the first LOGOUT is slow
            in_logout.set()
            await asyncio.sleep(0.1)
            order.append("first logout")
        elif body[44:48] == "0000":  # INIT, the start of a new session
            order.append("init")
        return await orig(body)

    monkeypatch.setattr(c, "_post", post)
    first = asyncio.create_task(c.fetch_units())
    await in_logout.wait()
    first.cancel()
    # Starts right away, while the logout is still on its way.
    second = asyncio.create_task(c.fetch_units())
    with pytest.raises(asyncio.CancelledError):
        await first
    assert len(await second) == 3
    assert order == ["init", "first logout", "init"]
    assert itm.session is None
    assert itm.requests[-1]["event"] == 10


async def test_apply_compares_with_the_unit_as_read_in_the_write_session(
    itm: FakeITM,
) -> None:
    """Turning on in heat must start a unit that was stopped since the last look."""
    c = FakeClient(itm)
    # Stopped at the wall controller; the caller still thinks it runs in heat.
    itm.units[0].running = False
    u = confirmed(await c.apply("1:1-00", power=True, mode="heat"))
    assert itm.units[0].running
    assert u.running
    assert u.mode == "heat"


async def test_apply_without_changes_does_not_write(itm: FakeITM) -> None:
    """A request the unit already matches opens no settings dialog."""
    c = FakeClient(itm)
    u = confirmed(await c.apply("1:1-04", power=True, mode="cool", fan_speed="medium"))
    assert (u.running, u.mode, u.fan_speed) == (True, "cool", "medium")
    assert not any(
        r["screen"] == 7 or (r["screen"] == 4 and r["part"] == 17) for r in itm.requests
    )
    assert itm.session is None
    await c.set_power("1:1-09", on=False)  # already stopped
    assert not any(r["screen"] == 7 for r in itm.requests)


async def test_apply_without_changes_records_no_unconfirmed_change(
    itm: FakeITM,
) -> None:
    """An unconfirmed write that writes nothing leaves nothing to wait for."""
    c = FakeClient(itm)
    assert await c.set_mode("1:1-04", "cool", confirm=False) is not None
    assert not c._unconfirmed


async def test_apply_sends_only_what_differs(itm: FakeITM) -> None:
    """Only the settings that differ from the unit are ticked."""
    c = FakeClient(itm)
    # Already running: only the mode is set.
    await c.apply("1:1-04", power=True, mode="heat")
    ticks = [
        r["part"] for r in itm.requests if r["screen"] == 7 and r["part"] in {2, 17, 28}
    ]
    assert ticks == [17]
    assert itm.units[1].mode == "Verwarmen"


async def test_default_zone_path_is_the_top_level(itm: FakeITM) -> None:
    """Without a zone path the units at the top level are read, zones skipped."""
    assert not DEFAULT_ZONE_PATH
    client = FakeClient(itm, zone_path=DEFAULT_ZONE_PATH)
    assert await client.fetch_units() == {}  # only a zone at the top level
    itm.root.children = [*itm.units, *itm.root.children]
    assert set(await client.fetch_units()) == {"1:1-00", "1:1-04", "1:1-09"}
