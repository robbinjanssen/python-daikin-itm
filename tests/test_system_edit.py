# pylint: disable=protected-access,redefined-outer-name
"""E-mail, confirm dialogs, web server port, remote management, own settings."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import ItmCommandError, ReadOnlyError
from daikin_itm._screens import open_screen
from daikin_itm.menu import MenuScreen
from daikin_itm.system import MB_EMAIL
from daikin_itm.system_edit import (
    EMAIL_LIST,
    EMAIL_OFF,
    EMAIL_ON,
    EmailServer,
    add_email_address,
    delete_email_address,
    edit_email_address,
    read_email_server,
    send_test_email,
    set_confirm_dialogs,
    set_email_enabled,
    set_email_server,
    set_remote_management,
    set_web_server_port,
)
from daikin_itm.users_edit import (
    leave_own_settings,
    open_own_settings,
    own_settings_from_screen,
    read_own_language,
    set_own_settings,
)

from .fake_system_edit import FakeSystemEditITM
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

ADDRESS = "user@example.com"


@pytest.fixture
def itm() -> FakeSystemEditITM:
    """Return a simulated iTM with the phase-2B screens."""
    return FakeSystemEditITM()


async def run(
    itm: FakeSystemEditITM,
    flow: Callable[..., Awaitable[Any]],
    *args: Any,
    client: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Run one flow in a fresh session, like the client's wrappers will."""
    c = FakeClient(itm, **(client or {}))
    async with c._session() as s:
        try:
            return await flow(s, *args, **kwargs)
        finally:
            await s.close_menu()


async def test_email_address_add_edit_delete(itm: FakeSystemEditITM) -> None:
    """Add an address reporting on one unit, change it, delete it."""
    unit = itm.units[0].name
    assert await run(itm, add_email_address, ADDRESS, (unit,)) is True
    assert itm.emails == {ADDRESS: [unit]}
    assert itm.email_active == {ADDRESS: False}  # activation is not touched
    with pytest.raises(ItmCommandError, match="exists already"):
        await run(itm, add_email_address, ADDRESS, (unit,))
    other = itm.units[1].name
    assert await run(itm, edit_email_address, ADDRESS, points=(other,)) is True
    assert itm.emails == {ADDRESS: [other]}
    new = "ops@example.com"
    assert await run(itm, edit_email_address, ADDRESS, new_address=new) is True
    assert itm.emails == {new: [other]}
    assert await run(itm, delete_email_address, new) is True
    assert not itm.emails
    assert itm.open_screens == set()


async def test_email_add_without_points(itm: FakeSystemEditITM) -> None:
    """The address goes straight into the list (as on a real iTM), without points."""
    assert await run(itm, add_email_address, ADDRESS, ()) is True
    assert itm.emails == {ADDRESS: []}
    with pytest.raises(ItmCommandError, match="exists already"):
        await run(itm, add_email_address, ADDRESS, ())


async def test_email_activation(itm: FakeSystemEditITM) -> None:
    """Activation is per address: select its row, then the radio applies at once."""
    with pytest.raises(ItmCommandError, match="not found"):
        await run(itm, set_email_enabled, ADDRESS, enabled=True)
    other = "ops@example.com"
    await run(itm, add_email_address, ADDRESS, (itm.units[0].name,))
    await run(itm, add_email_address, other, (itm.units[0].name,))
    assert await run(itm, set_email_enabled, ADDRESS, enabled=True) is True
    assert itm.email_active == {ADDRESS: True, other: False}
    assert await run(itm, set_email_enabled, ADDRESS, enabled=True) is True
    assert await run(itm, set_email_enabled, other, enabled=True) is True
    assert await run(itm, set_email_enabled, ADDRESS, enabled=False) is True
    assert itm.email_active == {ADDRESS: False, other: True}
    with pytest.raises(ItmCommandError, match="not found"):
        await run(itm, set_email_enabled, "nobody@example.com", enabled=True)
    c = FakeClient(itm, dry_run=True)
    async with c._session() as s:
        assert await set_email_enabled(s, other, enabled=False) is False
    assert any(f"off for {other}" in line for line in c.planned)
    assert itm.email_active == {ADDRESS: False, other: True}


async def test_email_radios_need_a_selection(itm: FakeSystemEditITM) -> None:
    """Like the real iTM: the radios are disabled until an address is selected."""
    await run(itm, add_email_address, ADDRESS, ())
    e = MenuScreen.EMAIL
    async with FakeClient(itm)._session() as s:
        await open_screen(s, MB_EMAIL, e)
        assert [s.state.get(e, p, "ENABLED") for p in (3, 4)] == [0, 0]
        await s._select_listed(e, EMAIL_LIST, 1, ADDRESS, "e-mail address")
        assert [s.state.get(e, p, "ENABLED") for p in (3, 4)] == [1, 1]
        assert s.state.get(e, EMAIL_OFF, "CHECK_STATE") == 1
        await s.close_screen(e)
    async with FakeClient(itm)._session() as s:
        await open_screen(s, MB_EMAIL, e)
        with pytest.raises(ItmCommandError, match="disabled"):
            await s.send(e, EMAIL_ON)


async def test_email_server(itm: FakeSystemEditITM) -> None:
    """Server settings; the password is sent, never planned in a dry run."""
    server = await run(itm, read_email_server)
    assert server["smtp_address"] == "smtp.example.com"
    assert server["smtp_port"] == 25
    assert "password" not in server
    change = EmailServer(
        smtp_address="mail.example.com",
        smtp_port=587,
        auth_method=2,
        password="placeholder",
    )
    assert await run(itm, set_email_server, change) is True
    assert itm.email_server[19] == "mail.example.com"
    assert itm.email_server[22] == "587"
    assert itm.email_server[26] == 2
    assert itm.email_password == "placeholder"
    c = FakeClient(itm, dry_run=True)
    async with c._session() as s:
        assert await set_email_server(s, EmailServer(password="other-secret")) is False
    assert all("other-secret" not in line for line in c.planned)
    assert any("***" in line for line in c.planned)
    assert itm.email_password == "placeholder"


async def test_test_email_is_guarded(itm: FakeSystemEditITM) -> None:
    """The test mail really sends: refused unless allowed."""
    await run(itm, add_email_address, ADDRESS, (itm.units[0].name,))
    with pytest.raises(ItmCommandError, match="allow_send"):
        await run(itm, send_test_email, ADDRESS)
    assert not itm.mails_sent
    assert await run(itm, send_test_email, ADDRESS, client={"dry_run": True}) is False
    assert not itm.mails_sent
    assert await run(itm, send_test_email, ADDRESS, allow_send=True) is True
    assert itm.mails_sent == [ADDRESS]


async def test_confirm_dialogs(itm: FakeSystemEditITM) -> None:
    """Confirm dialogs off and on again."""
    assert await run(itm, set_confirm_dialogs, enabled=False) is True
    assert itm.confirm_on is False
    assert await run(itm, set_confirm_dialogs, enabled=True) is True
    assert itm.confirm_on is True
    with pytest.raises(ReadOnlyError):
        await run(itm, set_confirm_dialogs, enabled=False, client={"read_only": True})


async def test_web_server_port_is_guarded(itm: FakeSystemEditITM) -> None:
    """A port change can cut access: refused unless allowed; a dry run enters none."""
    with pytest.raises(ItmCommandError, match="allow_network_change"):
        await run(itm, set_web_server_port, 8080)
    c = FakeClient(itm, dry_run=True)
    async with c._session() as s:
        assert await set_web_server_port(s, 8080) is False
    assert "web server port = 8080" in c.planned
    assert itm.web_port is None
    assert await run(itm, set_web_server_port, 8080, allow_network_change=True) is True
    assert itm.web_port == 8080
    assert await run(itm, set_web_server_port, None, allow_network_change=True) is True
    assert itm.web_port is None


async def test_remote_management_is_guarded(itm: FakeSystemEditITM) -> None:
    """Refused unless allowed; disabled connect/disconnect is refused before a click."""
    with pytest.raises(ItmCommandError, match="allow_remote_change"):
        await run(itm, set_remote_management, connect=True)
    with pytest.raises(ItmCommandError, match="disabled"):
        await run(itm, set_remote_management, connect=True, allow_remote_change=True)
    itm.remote_connectable = True
    result = await run(
        itm, set_remote_management, connect=True, allow_remote_change=True
    )
    assert result is True
    assert itm.remote_connected is True


async def test_own_settings(itm: FakeSystemEditITM) -> None:
    """Log in through "User Settings", read and change 1205; it ends the session."""
    c = FakeClient(itm)
    s = c._new_session()
    await open_own_settings(s, "user", "secret")
    own = own_settings_from_screen(s.state)
    assert (own.date_format, own.time_24h, own.decimal_comma, own.icon_colour) == (
        "%d/%m/%Y",
        True,
        False,
        1,
    )
    assert own.language == "en"
    assert await read_own_language(s) == "en"
    assert await set_own_settings(s, time_24h=False, decimal_comma=True) is True
    assert itm.own["24h"] is False
    assert itm.own["comma"] is True
    assert itm.session is None
    s = c._new_session()
    await open_own_settings(s, "user", "secret")
    await leave_own_settings(s)
    assert itm.session is None
    s = c._new_session()
    await open_own_settings(s, "user", "secret")
    with pytest.raises(ItmCommandError, match="date format"):
        await set_own_settings(s, date_format="%Y")
    await leave_own_settings(s)
    with pytest.raises(ItmCommandError, match="did not open"):
        await open_own_settings(c._new_session(), "user", "wrong")


async def test_more_settings_and_refusals(itm: FakeSystemEditITM) -> None:
    """Text fields, combos, own icon colour and date format; refusals."""
    server = EmailServer(
        site_name="Office",
        from_address="itm@example.com",
        pop_address="pop.example.com",
        pop_port=995,
        user_id="mailer",
        resend_interval=2,
    )
    assert await run(itm, set_email_server, server) is True
    assert (itm.email_server[3], itm.email_server[32], itm.email_server[6]) == (
        "Office",
        "995",
        2,
    )
    with pytest.raises(ItmCommandError, match="not found"):
        await run(itm, edit_email_address, "nobody@example.com", points=())
    with pytest.raises(ItmCommandError, match="not found"):
        await run(itm, delete_email_address, "nobody@example.com")
    assert await run(itm, set_confirm_dialogs, enabled=True) is True
    assert await run(itm, set_web_server_port, None, client={"dry_run": True}) is False
    itm.remote_connectable = True
    assert (
        await run(itm, set_remote_management, connect=False, client={"dry_run": True})
        is False
    )
    c = FakeClient(itm)
    s = c._new_session()
    await open_own_settings(s, "user", "secret")
    with pytest.raises(ItmCommandError, match="icon colour"):
        await set_own_settings(s, icon_colour=3)
    assert await set_own_settings(s, date_format="%Y/%m/%d", icon_colour=2) is True
    assert (itm.own["date"], itm.own["colour"]) == (2, 2)


async def test_client_email_and_confirm(itm: FakeSystemEditITM) -> None:
    """E-mail addresses, activation, server and confirm dialogs, each read back."""
    c = FakeClient(itm)
    point = itm.units[0].name
    email = await c.add_email_address("ops@example.com", [point])
    assert email is not None
    assert [a for _, a in email.addresses] == ["ops@example.com"]
    email = await c.edit_email_address(
        "ops@example.com", new_address="it@example.com", points=[point]
    )
    assert email is not None
    assert [a for _, a in email.addresses] == ["it@example.com"]
    email = await c.set_email_enabled("it@example.com", enabled=True)
    assert email is not None
    assert email.addresses == ((True, "it@example.com"),)
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_email_enabled("it@example.com", enabled=False) is None
    assert itm.email_active == {"it@example.com": True}
    with pytest.raises(ItmCommandError, match="not found"):
        await c.set_email_enabled("nobody@example.com", enabled=True)
    server = await c.set_email_server(
        EmailServer(smtp_address="mail.example.com", smtp_port=587, password="x")
    )
    assert server is not None
    assert itm.email_password == "x"
    with pytest.raises(ItmCommandError, match="switch its e-mail off first"):
        await c.delete_email_address("it@example.com")
    await c.set_email_enabled("it@example.com", enabled=False)
    await c.delete_email_address("it@example.com")
    assert itm.emails == {}
    assert await c.set_confirm_dialogs(enabled=False) is False
    assert await c.set_confirm_dialogs(enabled=True) is True
    with pytest.raises(ItmCommandError, match="allow_send"):
        await c.send_test_email("it@example.com")
    with pytest.raises(ItmCommandError, match="allow_network_change"):
        await c.set_web_server_port(8080)
    with pytest.raises(ItmCommandError, match="allow_remote_change"):
        await c.set_remote_management(connect=True)
    assert await dry.add_email_address("x@example.com", [point]) is None
    assert itm.emails == {}
