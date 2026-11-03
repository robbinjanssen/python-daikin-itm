# pylint: disable=protected-access,redefined-outer-name
"""The management point data registration (service screen 2800): read only."""

from __future__ import annotations

from typing import Any

import pytest

from daikin_itm import ItmCommandError, PointAttributes
from daikin_itm import cli as itm_cli
from daikin_itm.point_registry import (
    RG_FORBIDDEN,
    _click,
    registrations_from_screen,
)

from .fake_point_registry import FORBIDDEN, FakeRegistryITM
from .test_cli import CliClient
from .test_client import FakeClient

SERVICE = "test-service"


@pytest.fixture
def itm() -> FakeRegistryITM:
    """Return a simulated iTM with the point registration screens."""
    return FakeRegistryITM()


def pressed(itm: FakeRegistryITM, screen: int) -> list[int]:
    """Return the parts the client clicked on a screen."""
    return [r["part"] for r in itm.requests if r["screen"] == screen]


async def test_registrations(itm: FakeRegistryITM) -> None:
    """The list by column; left with Cancel, both closing questions answered Yes."""
    points = await FakeClient(itm).fetch_point_registrations(SERVICE)
    names = [u.name for u in itm.units]
    assert [p.name for p in points] == [*names, "OU-1"]
    first, last = points[0], points[-1]
    assert (first.type, first.address, first.info) == ("Indoor unit", "1:1-00", "")
    assert first.row == 0
    assert first.columns == ("Indoor unit", names[0], "1:1-00", "")
    assert (last.type, last.address, last.info) == ("Outdoor unit", "1-00", "Roof")
    assert all(p.attributes is None for p in points)
    assert pressed(itm, 2800) == [13, 30, 31]  # Cancel, then Yes twice
    assert not set(pressed(itm, 2800)) & FORBIDDEN
    assert 2800 not in itm.open_screens
    assert itm.opened == [2800]
    assert itm.session is None


async def test_registration_details(itm: FakeRegistryITM) -> None:
    """Each point: select, Edit, read the text fields, Cancel + Yes."""
    points = await FakeClient(itm).fetch_point_registrations(SERVICE, details=True)
    indoor, outdoor = points[0].attributes, points[-1].attributes
    assert indoor == PointAttributes(
        2802,
        "indoor",
        (
            ("Port No.", "1"),
            ("Type", "Indoor unit"),
            ("Name", itm.units[0].name),
            ("", ""),
        ),
    )
    assert outdoor is not None
    assert outdoor.type == "outdoor"
    assert ("Details", "Roof") in outdoor.fields
    assert itm.opened == [2800, *[2802] * len(itm.units), 2803]
    assert not set(pressed(itm, 2800)) & FORBIDDEN
    assert pressed(itm, 2802) == [22, 30] * len(itm.units)  # Cancel, Yes
    assert pressed(itm, 2803) == [18, 30]
    assert itm.open_screens == set()


async def test_unknown_attributes_screen(itm: FakeRegistryITM) -> None:
    """Edit opening an unknown screen raises; 2800 is left to the session end."""
    itm.edit_screen = 0
    with pytest.raises(ItmCommandError, match="no known attributes screen"):
        await FakeClient(itm).fetch_point_registrations(SERVICE, details=True)
    assert 13 not in pressed(itm, 2800)
    assert pressed(itm, 2899) == []
    assert itm.session is None


async def test_registry_did_not_open(itm: FakeRegistryITM) -> None:
    """A registration screen that doesn't open raises."""
    itm.registry_opens = False
    with pytest.raises(ItmCommandError, match="did not open"):
        await FakeClient(itm).fetch_point_registrations(SERVICE)
    assert pressed(itm, 2800) == []


async def test_cancel_refused(itm: FakeRegistryITM) -> None:
    """An error dialog in answer to Cancel raises."""
    itm.cancel_answer = "ERROR"
    with pytest.raises(ItmCommandError, match="answered 'Error'"):
        await FakeClient(itm).fetch_point_registrations(SERVICE)


async def test_cancel_stays_open(itm: FakeRegistryITM) -> None:
    """A registration screen that stays open after Cancel raises."""
    itm.cancel_answer = "STAY"
    with pytest.raises(ItmCommandError, match="did not close"):
        await FakeClient(itm).fetch_point_registrations(SERVICE)
    assert pressed(itm, 2800) == [13]


async def test_wrong_service_password(itm: FakeRegistryITM) -> None:
    """A wrong service password never opens the screen."""
    with pytest.raises(ItmCommandError, match="refused the service password"):
        await FakeClient(itm).fetch_point_registrations("wrong")
    assert itm.opened == []


async def test_never_clicks_forbidden_or_disabled(itm: FakeRegistryITM) -> None:
    """The click guard refuses the write buttons and disabled parts before sending."""
    c = FakeClient(itm)
    async with c._session() as s:
        await s.service_login(SERVICE)
        await s.open_menu()
        await _click(s, 5, 63, "open")
        sent = len(itm.requests)
        for part in sorted(RG_FORBIDDEN):
            with pytest.raises(ItmCommandError, match="never pressed"):
                await _click(s, 2800, part, "press")
        with pytest.raises(ItmCommandError, match="disabled"):
            await _click(s, 2800, 5, "edit")  # no row selected
        assert len(itm.requests) == sent
        assert registrations_from_screen(s.state)[0].row == 0
        await _click(s, 2800, 13, "cancel")
        await s.send(2800, 30)
        await s.close_menu()
    assert FORBIDDEN >= RG_FORBIDDEN


def test_fake_refuses_forbidden(itm: FakeRegistryITM) -> None:
    """The simulator itself fails on a forbidden or disabled button."""
    itm.open_screens.add(2800)
    for part in (*sorted(FORBIDDEN), 5, 7):
        with pytest.raises(RuntimeError):
            itm._registry_event({"screen": 2800, "part": part, "dtype": 0})
    itm.open_screens.add(2802)
    with pytest.raises(RuntimeError):
        itm._attributes_event({"screen": 2802, "part": 21, "dtype": 0})


# -- CLI ------------------------------------------------------------------------------


@pytest.fixture
def cli_itm(
    itm: FakeRegistryITM, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> FakeRegistryITM:
    """Route the CLI to this simulator, with the service password set."""
    monkeypatch.setattr(CliClient, "itm", itm, raising=False)
    monkeypatch.setattr(CliClient, "sigterm_at", None, raising=False)
    monkeypatch.setattr(itm_cli, "ItmClient", CliClient)
    monkeypatch.setattr(itm_cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("ITM_USERNAME", "user")
    monkeypatch.setenv("ITM_PASSWORD", "secret")
    monkeypatch.setenv("ITM_SERVICE_PASSWORD", SERVICE)
    return itm


def test_cli_points_registry(
    cli_itm: FakeRegistryITM,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`itm points-registry [--details]`; the password only from the environment."""
    itm_cli.main(["--host", "fake-host", "points-registry"])
    out = capsys.readouterr().out
    assert "Outdoor unit" in out
    assert "OU-1" in out
    assert "Roof" in out
    assert SERVICE not in out
    itm_cli.main(["--host", "fake-host", "points-registry", "--details"])
    out = capsys.readouterr().out
    assert "    Port No.: 1" in out
    assert "    ?: " in out
    cli_itm.points = []
    itm_cli.main(["--host", "fake-host", "points-registry"])
    assert capsys.readouterr().out == "no management points registered\n"
    monkeypatch.delenv("ITM_SERVICE_PASSWORD")
    with pytest.raises(SystemExit, match="ITM_SERVICE_PASSWORD"):
        itm_cli.main(["--host", "fake-host", "points-registry"])
