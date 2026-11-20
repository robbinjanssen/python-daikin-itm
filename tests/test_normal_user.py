"""A normal web user without allowed screens: no main menu, its zone on top."""

from __future__ import annotations

from typing import Any

import pytest

from daikin_itm import ItmCommandError, MenuNotAvailable, ZoneNotFound

from .fake_itm import FakeITM, Update
from .test_client import FakeClient


@pytest.fixture(name="itm")
def fixture_itm() -> FakeITM:
    """Return a simulated iTM, logged in to as a normal web user."""
    return FakeITM(normal_user=True)


def client(fake: FakeITM) -> FakeClient:
    """Return a client for the normal user's top level."""
    return FakeClient(fake, zone_path=())


@pytest.mark.parametrize(
    "read",
    ["fetch_info", "fetch_schedules", "fetch_history", "fetch_system", "fetch_setback"],
)
async def test_menu_reads_raise_menu_not_available(itm: FakeITM, read: str) -> None:
    """A read through the main menu says the menu didn't open for this account."""
    with pytest.raises(MenuNotAvailable, match="not available to this account") as err:
        await getattr(client(itm), read)()
    assert isinstance(err.value, ItmCommandError)
    assert itm.session is None


def _menu_clicks(itm: FakeITM) -> list[dict[str, Any]]:
    return [r for r in itm.requests if (r["screen"], r["part"]) == (0, 0)]


async def test_a_hidden_menu_button_is_not_pressed(itm: FakeITM) -> None:
    """The login hid the menu button: the menu read raises before pressing it."""
    with pytest.raises(MenuNotAvailable, match="hid the menu button"):
        await client(itm).fetch_info()
    assert itm.logins == 1
    assert _menu_clicks(itm) == []


class ShownButtonITM(FakeITM):
    """A normal web user whose login leaves the menu button shown."""

    def _menu_bar(self) -> list[tuple[int, str, Any]]:
        return [b for b in super()._menu_bar() if b[0] != 0]


async def test_an_empty_menu_reply_still_raises() -> None:
    """With the button shown, the empty reply to it raises MenuNotAvailable."""
    itm = ShownButtonITM(normal_user=True)
    with pytest.raises(MenuNotAvailable) as err:
        await client(itm).fetch_info()
    assert "hidden" not in str(err.value)
    assert len(_menu_clicks(itm)) == 1


@pytest.mark.parametrize(("normal_user", "available"), [(True, False), (False, True)])
async def test_indicators_tell_whether_the_menu_is_available(
    *, normal_user: bool, available: bool
) -> None:
    """The indicators say whether the login showed the menu button."""
    itm = FakeITM(normal_user=normal_user)
    c = client(itm)
    assert (await c.fetch_indicators()).menu_available is available
    async with c.shared_session():
        assert (await c.fetch_indicators()).menu_available is available
        await c.fetch_units()
        assert (await c.fetch_indicators()).menu_available is available


async def test_units_and_status_still_work(itm: FakeITM) -> None:
    """The managed zone's units show at the top level and read as usual."""
    itm.units[0].thermo_on = True
    c = client(itm)
    assert await c.list_zones() == ["1:1-00", "1:1-04", "1:1-09"]
    units = await c.fetch_units()
    assert list(units) == ["1:1-00", "1:1-04", "1:1-09"]
    assert units["1:1-09"].running is False
    status = await c.fetch_thermostat_status()
    assert status["1:1-00"] is True
    indicators = await c.fetch_indicators()
    assert indicators.error is False


async def test_the_managed_zone_is_no_path(itm: FakeITM) -> None:
    """The managed zone itself can't be opened: its units are what is there."""
    with pytest.raises(ZoneNotFound) as err:
        await FakeClient(itm, zone_path=("Indoor units",)).fetch_units()
    assert err.value.available == ["1:1-00", "1:1-04", "1:1-09"]


async def test_an_administrator_opens_the_menu() -> None:
    """The default simulator is an administrator: the same read works."""
    info = await client(FakeITM()).fetch_info()
    assert info.language == "English"


class DialogITM(FakeITM):
    """A simulated iTM that answers the menu button with an error dialog."""

    def _menu(self, ev: dict[str, Any]) -> list[Update]:
        if ev["screen"] == 0 and ev["part"] == 0:
            return [("DLG_OPEN", 0, [6, 1, "Not now", "Error", "OK"], [])]
        return super()._menu(ev)


async def test_a_dialog_is_not_menu_not_available() -> None:
    """Only a reply without menu list and without dialog raises MenuNotAvailable."""
    with pytest.raises(ItmCommandError, match="menu list did not open") as err:
        await client(DialogITM()).fetch_info()
    assert not isinstance(err.value, MenuNotAvailable)
