# pylint: disable=protected-access,redefined-outer-name
"""Unit settings (fetch_unit_settings), languages and the read-only mode."""

from __future__ import annotations

import pytest

from daikin_itm import PowerState, ReadOnlyError, RemoteLock
from daikin_itm.lang import lock_from_label, on_off
from daikin_itm.models import (
    MODES,
    Capabilities,
    capabilities_from_dialog,
    details_from_screen,
    normalize_mode,
    settings_from_dialog,
    status_from_details,
    unit_from_panel,
)
from daikin_itm.protocol import Part, ScreenUpdate
from daikin_itm.state import UIState

from .fake_itm import FakeITM
from .test_client import FakeClient


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM."""
    return FakeITM()


def _state(screen: int, parts: list[tuple[int, str, object]]) -> UIState:
    s = UIState()
    s.apply([ScreenUpdate(screen, "OPEN", [-1], [Part(p, t, v) for p, t, v in parts])])
    return s


@pytest.mark.parametrize(
    ("label", "mode"),
    [
        ("Heat", "heat"),
        ("KOELEN", "cool"),
        ("Heating", "heat"),
        ("Automatic", "auto"),
        ("Heizen", "heat"),
        ("Froid", "cool"),
        ("Raffreddamento", "cool"),
        ("Calefacción", "heat"),
        ("Ventilação", "fan"),
        ("暖房", "heat"),
        ("制冷", "cool"),
        ("송풍", "fan"),
        ("Mystery", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_mode(label: str | None, mode: str | None) -> None:
    """Mode labels of every iTM language map to the same modes."""
    assert normalize_mode(label) == mode


def test_modes_table_is_kept() -> None:
    """MODES still maps the Dutch and English labels (public, used by the CLI)."""
    assert MODES["verwarmen"] == "heat"
    assert MODES["cool"] == "cool"
    assert set(MODES.values()) == {"heat", "cool", "fan", "dry", "auto"}


def test_lock_and_on_off_labels() -> None:
    """Lock and on/off labels map by language, unknown ones to None."""
    assert lock_from_label("Prohibited") == "forbidden"
    assert lock_from_label("Stop only") == "stop_only"
    assert lock_from_label("Toegest.") == "allowed"
    assert lock_from_label("Gesperrt") is None
    assert on_off("On") is True
    assert on_off("Deactiv.") is False
    assert on_off("Off") is False
    assert on_off("?") is None


def test_unknown_mode_labels_are_reported() -> None:
    """A mode label the client doesn't know is kept, so the user can be warned."""
    s = _state(7, [(18, "COMBO_STRS", ["Ventilator", "Kühlen", "Chauffe-eau"])])
    caps = capabilities_from_dialog("a", s)
    assert caps.modes == ("fan", "cool")
    assert caps.unknown_labels == ("Chauffe-eau",)
    assert not Capabilities("a", {}, fan_speed=False).unknown_labels


def test_zones_without_heading() -> None:
    """A first line that is a path itself is a zone, in any language."""
    s = _state(9, [(14, "TEXT", "Floor 1&gt;Building\nFloor 1&gt;Wing A\n\nx [y]")])
    assert details_from_screen(s)["zones"] == ("Floor 1>Building", "Floor 1>Wing A")


def test_status_from_details() -> None:
    """The status lines give the thermostat state and the R32 alarm."""
    text = "Area:\nA&gt;B\n\nThermostat Status [ON]\nR32 Alarm [Disable]\nFoo [Bar]\n"
    status = status_from_details(_state(9, [(14, "TEXT", text)]))
    assert status["thermo_on"] is True
    assert status["r32_alarm"] is False
    assert status["flags"] == {
        "Thermostat Status": "ON",
        "R32 Alarm": "Disable",
        "Foo": "Bar",
    }
    assert status_from_details(_state(9, [])) == {
        "thermo_on": None,
        "r32_alarm": None,
        "flags": {},
    }
    unknown = status_from_details(_state(9, [(14, "TEXT", "Thermostat Status [?]")]))
    assert unknown["thermo_on"] is None


def test_settings_from_dialog() -> None:
    """Locks, limits and the overtime timer come from the dialog's controls."""
    parts: list[tuple[int, str, object]] = [
        (8, "CHECK_STATE", 0),
        (9, "CHECK_STATE", 1),
        (10, "CHECK_STATE", 0),
        (12, "CHECK_STATE", 0),
        (13, "CHECK_STATE", 1),
        (15, "CHECK_STATE", 1),
        (16, "CHECK_STATE", 0),
        (27, "SINGLE_SELIDX", 0),
        (63, "SINGLE_SELIDX", 0),
        (65, "TEXT", "24.0"),
        (69, "TEXT", "20.0"),
        (73, "SINGLE_SELIDX", 1),
        (75, "TEXT", "32.0"),
        (79, "TEXT", "16.0"),
    ]
    settings = settings_from_dialog("a", _state(7, parts), {"thermo_on": True})
    assert settings.remote_locks.start_stop is RemoteLock.STOP_ONLY
    assert settings.remote_locks.mode is RemoteLock.FORBIDDEN
    assert settings.remote_locks.setpoint is RemoteLock.ALLOWED
    assert settings.overtime is True
    assert settings.cool_limit.enabled is True
    assert (settings.cool_limit.minimum, settings.cool_limit.maximum) == (20.0, 24.0)
    assert settings.heat_limit.enabled is False
    assert settings.thermo_on is True
    assert settings.r32_alarm is None


def test_settings_from_empty_dialog() -> None:
    """Missing controls (another unit type) give unknown values, not errors."""
    settings = settings_from_dialog("a", _state(7, []))
    assert settings.remote_locks.start_stop is RemoteLock.UNKNOWN
    assert settings.overtime is None
    assert settings.cool_limit.enabled is None
    assert settings.cool_limit.minimum is None
    assert not settings.flags


async def test_fetch_unit_settings(itm: FakeITM) -> None:
    """The settings are read from Details and the (cancelled) settings dialog."""
    u = itm.units[1]
    u.lock_start_stop, u.lock_mode, u.lock_setpoint = 2, 1, 0
    u.heat_limit = (True, 18.0, 25.0)
    u.overtime = True
    u.thermo_on = True
    settings = await FakeClient(itm).fetch_unit_settings("1:1-04")
    assert settings.name == "1:1-04"
    assert settings.thermo_on is True
    assert settings.r32_alarm is False
    assert settings.remote_locks.start_stop is RemoteLock.FORBIDDEN
    assert settings.remote_locks.mode is RemoteLock.FORBIDDEN
    assert settings.remote_locks.setpoint is RemoteLock.ALLOWED
    assert settings.heat_limit.enabled is True
    assert settings.heat_limit.maximum == 25.0
    assert settings.cool_limit.enabled is False
    assert settings.overtime is True
    assert settings.flags["Thermostat Status"] == "ON"
    assert itm.dialog is None
    assert itm.session is None


def test_power_state_from_panel() -> None:
    """The on/off options map to a power state by their count and index."""
    four = ["Start", "SB Low", "SB High", "Stop"]
    for idx, expected in enumerate(
        (PowerState.ON, PowerState.SETBACK_LOW, PowerState.SETBACK_HIGH, PowerState.OFF)
    ):
        s = _state(4, [(9, "COMBO_STRS", four), (9, "SINGLE_SELIDX", idx)])
        assert unit_from_panel(s).power_state is expected
    s = _state(4, [(9, "COMBO_STRS", ["Start", "Stop"]), (9, "SINGLE_SELIDX", 1)])
    assert unit_from_panel(s).power_state is PowerState.OFF
    s = _state(4, [(9, "COMBO_STRS", ["A", "B", "C"]), (9, "SINGLE_SELIDX", 1)])
    assert unit_from_panel(s).power_state is None


def test_temperature_unit_from_panel() -> None:
    """A Fahrenheit iTM is recognised from the label."""
    s = _state(4, [(4, "LABEL", "70.2°F"), (4, "VISIBLED", 1)])
    unit = unit_from_panel(s)
    assert unit.room_temperature == 70.2
    assert unit.temperature_unit == "°F"
    assert unit_from_panel(_state(4, [])).temperature_unit == "°C"


async def test_read_only_refuses_writes_before_sending(itm: FakeITM) -> None:
    """A read-only client never sends a write, not even a request."""
    c = FakeClient(itm, read_only=True)
    with pytest.raises(ReadOnlyError):
        await c.set_setpoint("1:1-04", 21)
    with pytest.raises(ReadOnlyError):
        await c.apply("1:1-04", power=False)
    with pytest.raises(ReadOnlyError):
        await c.set_mode("1:1-04", "heat")
    assert itm.requests == []
    assert await c.apply("1:1-04") is None  # nothing asked: nothing refused


async def test_read_only_still_reads(itm: FakeITM) -> None:
    """Reads work as usual in read-only mode."""
    c = FakeClient(itm, read_only=True)
    assert (await c.fetch_units())["1:1-04"].setpoint == 23
    assert (await c.fetch_units(view="list"))["1:1-04"].setpoint == 23
    assert (await c.fetch_unit_settings("1:1-04")).overtime is False
    assert "1:1-04" in await c.fetch_capabilities()
    assert itm.units[1].setpoint == 23


async def test_read_only_guards_the_session_too(itm: FakeITM) -> None:
    """The session refuses the commit, the panel picks and the dialog's OK."""
    c = FakeClient(itm, read_only=True)
    async with c._session() as s:
        await s.open_zone(c.zone_path)
        await s.select_by_name("1:1-04")
        with pytest.raises(ReadOnlyError):
            await s.choose(11, 1)
        with pytest.raises(ReadOnlyError):
            await s.commit(0)
        await s.open_settings()
        with pytest.raises(ReadOnlyError):
            await s.close_settings(ok=True)
        await s.close_settings(ok=False)
    assert not any(r["event"] == 3 for r in itm.requests)
    assert not any(r["screen"] == 7 and r["part"] == 5 for r in itm.requests)
    assert itm.session is None
