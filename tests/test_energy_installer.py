# pylint: disable=protected-access,redefined-outer-name
"""The energy navigator's installer settings, behind the service login."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import (
    ConversionType,
    EnergyInstallerSettings,
    ItmCommandError,
    ReadOnlyError,
    RoomTemperaturePoint,
)
from daikin_itm import cli as itm_cli
from daikin_itm import energy_edit as ee
from daikin_itm import energy_navigator as en
from daikin_itm.state import UIState

from .fake_energy_more import MONTHS, OWN, FakeEnergyMoreITM
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

PASSWORD = "test-service"  # the simulator's placeholder
SAVES = {(3221, 9), (3223, 3), (3224, 14)}


@pytest.fixture
def itm() -> FakeEnergyMoreITM:
    """Return a simulated iTM with the installer's energy navigator screens."""
    return FakeEnergyMoreITM()


def client(itm: FakeEnergyMoreITM, **kw: Any) -> FakeClient:
    """Return a client for the simulator, without progress delays."""
    c = FakeClient(itm, **kw)
    c.progress_interval = 0
    return c


def assert_closed(itm: FakeEnergyMoreITM) -> None:
    """Every screen left, no dialog open, nothing invalid sent, no data cleared."""
    assert itm.violations == []
    assert itm.edit_dialog is None
    assert not itm.data_cleared
    assert not {s for s in itm.open_screens if 3200 <= s < 3300}


def saves(itm: FakeEnergyMoreITM) -> list[tuple[int, int]]:
    """Return the saving requests sent."""
    return [
        (r["screen"], r["part"])
        for r in itm.requests
        if (r["screen"], r["part"]) in SAVES and r["event"] == 1
    ]


async def test_read_installer(itm: FakeEnergyMoreITM) -> None:
    """3220 -> 3221 (each row selected), 3222, 3223, 3224; every screen cancelled."""
    nav = await client(itm).fetch_energy_navigator(
        ["installer"], service_password=PASSWORD
    )
    settings = nav.installer
    assert isinstance(settings, EnergyInstallerSettings)
    assert settings.room_temperature == (
        RoomTemperaturePoint("AC1", OWN, own_sensor=True, analog_point=None),
        RoomTemperaturePoint("AC2", "AI 1", own_sensor=False, analog_point="AI 1"),
    )
    assert settings.analog_points == (("AI 1", "3-00"), ("AI 2", "3-01"))
    assert settings.collection_start == "Jan"
    assert settings.collection_months == tuple(MONTHS)
    assert settings.conversion == ConversionType("", "kWh", "MWh", "GWh")
    assert saves(itm) == []
    assert not [r for r in itm.requests if (r["screen"], r["part"]) == (3220, 9)]
    assert_closed(itm)


async def test_read_installer_without_analog_inputs(itm: FakeEnergyMoreITM) -> None:
    """3222's button disabled after the last row: 3222 is not opened."""
    itm.room_temp = {"AC1": "AI 2", "AC2": None}
    async with client(itm)._session() as s:
        settings = await ee.read_installer(s, PASSWORD)
    assert settings.analog_points is None
    assert settings.room_temperature is not None
    assert settings.room_temperature[0].analog_point == "AI 2"
    assert not [r for r in itm.requests if r["screen"] == 3222]
    assert_closed(itm)


async def test_read_installer_refusals(itm: FakeEnergyMoreITM) -> None:
    """No password: refused before a request; a wrong one; no service login."""
    c = client(itm)
    with pytest.raises(ItmCommandError, match="need service_password"):
        await c.fetch_energy_navigator(["installer"])
    assert itm.requests == []
    with pytest.raises(ItmCommandError, match="refused the service password"):
        await c.fetch_energy_navigator(["installer"], service_password="wrong")
    async with c._session() as s:
        with pytest.raises(ItmCommandError, match="need the service password"):
            await en.read_energy_navigator(s, frozenset({"installer"}))
        with pytest.raises(ItmCommandError, match="installer settings:"):
            await en.read_installer(s)
    itm.unavailable = {3220}
    nav = await c.fetch_energy_navigator(["installer"], service_password=PASSWORD)
    assert nav.installer is None
    async with c._session() as s:
        with pytest.raises(ItmCommandError, match="did not open"):
            await ee.read_installer(s, PASSWORD)
    assert_closed(itm)


async def test_fake_fails_on_data_clear(itm: FakeEnergyMoreITM) -> None:
    """The simulator fails the test as soon as 3220's data clear is clicked."""
    async with client(itm)._session() as s:
        await s.service_login(PASSWORD)
        assert await en.open_installer(s)
        with pytest.raises(AssertionError, match="data clear"):
            await s.send(3220, 9)
    assert itm.data_cleared
    assert itm.violations


async def test_room_temperature(itm: FakeEnergyMoreITM) -> None:
    """Pick the analog input in 3222, apply to the row, OK; and back to own."""
    c = client(itm)
    point = await c.set_energy_room_temperature(
        PASSWORD, "AC1", analog_point="AI 2", allow_service_change=True
    )
    assert point == RoomTemperaturePoint(
        "AC1", "AI 2", own_sensor=False, analog_point="AI 2"
    )
    assert itm.room_temp["AC1"] == "AI 2"
    point = await c.set_energy_room_temperature(
        PASSWORD, "AC2", allow_service_change=True
    )
    assert point is not None
    assert point.own_sensor is True
    assert itm.room_temp == {"AC1": "AI 2", "AC2": None}
    assert itm.saved == ["room temperature", "room temperature"]
    assert_closed(itm)


async def test_collection_start_and_conversion(itm: FakeEnergyMoreITM) -> None:
    """The month combo and the conversion texts, each saved by OK and read back."""
    c = client(itm)
    month = await c.set_energy_collection_start(
        PASSWORD, "Apr", allow_service_change=True
    )
    assert month == "Apr"
    assert itm.collect_month == 3
    # The month shown already: nothing picked, OK all the same.
    assert await c.set_energy_collection_start(
        PASSWORD, "Apr", allow_service_change=True
    )
    shown = await c.set_energy_conversion(
        PASSWORD, ConversionType(name="Power", one="Wh"), allow_service_change=True
    )
    assert shown == ConversionType("Power", "Wh", "MWh", "GWh")
    assert itm.saved == ["collection start", "collection start", "conversion"]
    assert_closed(itm)


async def test_guard_dry_run_and_read_only(itm: FakeEnergyMoreITM) -> None:
    """Refused without allow_service_change; a dry run plans; read_only refuses."""
    c = client(itm)
    calls: list[Callable[[FakeClient], Awaitable[Any]]] = [
        lambda c: c.set_energy_room_temperature(PASSWORD, "AC1"),
        lambda c: c.set_energy_collection_start(PASSWORD, "Feb"),
        lambda c: c.set_energy_conversion(PASSWORD, ConversionType(one="Wh")),
    ]
    for call in calls:
        with pytest.raises(ItmCommandError, match="allow_service_change"):
            await call(c)
    assert itm.requests == []
    ro = client(itm, read_only=True)
    for call in calls:
        with pytest.raises(ReadOnlyError):
            await call(ro)
    assert itm.requests == []
    async with ro._session() as s:
        for flow in (
            ee.set_room_temperature(s, PASSWORD, "AC1", None),
            ee.set_collection_start(s, PASSWORD, "Feb"),
            ee.set_conversion(s, PASSWORD, ConversionType(one="Wh")),
        ):
            with pytest.raises(ReadOnlyError):
                await flow
    dry = client(itm, dry_run=True)
    for call in calls:
        assert await call(dry) is None
    assert [p.split(":")[0] for p in dry.planned] == [
        "room temperature of AC1",
        "collection start month Feb",
        "save energy conversion",
    ]
    assert itm.saved == []
    assert saves(itm) == []
    assert_closed(itm)


async def test_installer_write_refusals(itm: FakeEnergyMoreITM) -> None:
    """Unknown point, analog input or month; a disabled OK; 3220 not opening."""
    c = client(itm)

    async def write(call: Callable[[], Awaitable[Any]], match: str) -> None:
        with pytest.raises(ItmCommandError, match=match):
            await call()

    await write(
        lambda: c.set_energy_room_temperature(
            PASSWORD, "AC9", allow_service_change=True
        ),
        "'AC9' not listed once",
    )
    await write(
        lambda: c.set_energy_room_temperature(
            PASSWORD, "AC1", analog_point="AI 9", allow_service_change=True
        ),
        "'AI 9' not listed once",
    )
    await write(
        lambda: c.set_energy_collection_start(
            PASSWORD, "Month 13", allow_service_change=True
        ),
        "not offered",
    )
    # Nothing changed: 3224's OK stays disabled, so it is not clicked.
    await write(
        lambda: c.set_energy_conversion(
            PASSWORD, ConversionType(), allow_service_change=True
        ),
        "does not offer",
    )
    await write(
        lambda: c.set_energy_conversion(
            PASSWORD, ConversionType(one="x" * 17), allow_service_change=True
        ),
        "must be 1-16 characters",
    )
    itm.disabled = {(3223, 2)}
    await write(
        lambda: c.set_energy_collection_start(
            PASSWORD, "May", allow_service_change=True
        ),
        "does not offer",
    )
    itm.disabled = set()
    original = itm._analog_event
    itm._analog_event = lambda ev: (  # type: ignore[method-assign]
        [("UPDATE", 3222, [], [])] if ev["part"] == 3 else original(ev)
    )
    await write(
        lambda: c.set_energy_room_temperature(
            PASSWORD, "AC1", analog_point="AI 2", allow_service_change=True
        ),
        "selection did not close",
    )
    itm._analog_event = original  # type: ignore[method-assign]
    itm.unavailable = {3220}
    await write(
        lambda: c.set_energy_collection_start(
            PASSWORD, "May", allow_service_change=True
        ),
        "did not open",
    )
    assert itm.saved == []
    assert_closed(itm)


async def test_installer_read_back_mismatch(itm: FakeEnergyMoreITM) -> None:
    """A setting the iTM doesn't show afterwards fails the confirmation."""
    c = client(itm)
    itm._confirm_dialog = lambda screen, *_: itm._close(  # type: ignore[method-assign]
        screen
    )
    with pytest.raises(ItmCommandError, match="AC1: the iTM shows"):
        await c.set_energy_room_temperature(
            PASSWORD, "AC1", analog_point="AI 2", allow_service_change=True
        )
    with pytest.raises(ItmCommandError, match="collection start: the iTM shows"):
        await c.set_energy_collection_start(PASSWORD, "Jun", allow_service_change=True)
    with pytest.raises(ItmCommandError, match="conversion one: the iTM shows"):
        await c.set_energy_conversion(
            PASSWORD, ConversionType(one="Wh"), allow_service_change=True
        )
    assert_closed(itm)


def test_installer_readers_on_unopened_screens() -> None:
    """Nothing shown: empty values, no errors."""
    state = UIState()
    assert en.conversion_from_screen(state) == ConversionType("", "", "", "")
    assert en.collection_from_screen(state) == (None, ())
    point = en.room_point_from_screen(state, [])
    assert point == RoomTemperaturePoint("", "", None, None)


def test_cli_installer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`itm energy --only installer` and the installer actions of `energy-edit`."""
    fake = FakeEnergyMoreITM()

    class Client(FakeClient):
        """The CLI's client, on this test's simulator."""

        def __init__(self, _host: str, _user: str, _password: str, **kw: Any) -> None:
            kw.pop("store", None)
            super().__init__(fake, **kw)
            self.progress_interval = 0

    monkeypatch.setattr(itm_cli, "ItmClient", Client)
    monkeypatch.setattr(itm_cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("ITM_USERNAME", "user")
    monkeypatch.setenv("ITM_PASSWORD", "secret")
    monkeypatch.delenv("ITM_SERVICE_PASSWORD", raising=False)

    def main(*args: str) -> str:
        itm_cli.main(["--host", "fake-host", "--experimental", *args])
        return capsys.readouterr().out

    with pytest.raises(SystemExit, match="ITM_SERVICE_PASSWORD"):
        main("energy", "--only", "installer")
    with pytest.raises(SystemExit, match="ITM_SERVICE_PASSWORD"):
        main("energy-edit", "conversion", "--one", "Wh")
    monkeypatch.setenv("ITM_SERVICE_PASSWORD", PASSWORD)
    out = main("--read-only", "energy", "--only", "installer")
    assert "room temperature AC2: AI 1 AI 1" in out
    assert "analog inputs: AI 1 3-00; AI 2 3-01" in out
    assert "collection starts in Jan" in out
    assert "conversion '': kWh / MWh / GWh" in out
    out = main("--dry-run", "energy-edit", "conversion", "--one", "Wh")
    assert "save energy conversion" in out
    with pytest.raises(SystemExit, match="allow_service_change"):
        main("energy-edit", "collection-start", "--start-month", "Feb")
    allow = "--allow-service-change"
    main("energy-edit", "collection-start", "--start-month", "Feb", allow)
    assert fake.collect_month == 1
    out = main("energy-edit", "room-temp", "--point", "AC1", "--analog", "AI 2", allow)
    assert "room-temp: done, the iTM shows" in out
    main("energy-edit", "conversion", "--conversion-name", "Power", allow)
    assert fake.conversion[2] == "Power"
    for args, error in (
        (["room-temp"], "needs --point"),
        (["collection-start"], "needs --start-month"),
    ):
        with pytest.raises(SystemExit, match=error):
            main("energy-edit", *args)
    assert not fake.violations
    assert not fake.data_cleared
