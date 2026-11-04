# pylint: disable=redefined-outer-name
"""The opt-in for writes not validated on a real iTM yet (allow_experimental)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import (
    ExperimentalError,
    ItmCommandError,
    ItmError,
    ReadOnlyError,
    RuleRange,
)
from daikin_itm import cli as itm_cli

from .fake_dealer_functions import FakeDealerITM
from .fake_demand import FakeDemandITM
from .fake_emergency import FakeEmergencyITM
from .fake_energy_more import FakeEnergyMoreITM
from .fake_itm import FakeITM
from .fake_service_edit import FakeServiceEditITM
from .fake_system_edit import FakeSystemEditITM
from .fake_users import FakeUsersITM
from .fake_zones import FakeZonesITM
from .test_cli import fixture_itm
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

__all__ = ["fixture_itm"]  # the simulator the CLI talks to (autouse)

SERVICE = "test-service"
REFUSED = "not validated on a real iTM yet; pass allow_experimental=True"

# Every experimental write, with arguments; each refuses before a request is sent.
GUARDED: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [
    ("set_network", (), {"dns_secondary": "10.0.0.9", "allow_network_change": True}),
    ("set_planned_consumption", (), {}),
    ("set_actual_consumption", (), {}),
    ("set_energy_sampling", (), {}),
    ("rename_energy_type", ("Gas", "Water"), {"group": True}),
    ("rename_energy_rule", ("Rule A", "Rule C"), {}),
    ("copy_energy_rule", ("Rule A", ("Rule B",)), {}),
    (
        "set_energy_rule_day",
        ("Rule A", "Monday", (RuleRange("09:00", "17:00", cool=24),)),
        {},
    ),
    ("set_energy_room_temperature", (SERVICE, "1:1-00"), {}),
    ("set_energy_collection_start", (SERVICE, "April"), {}),
    ("set_energy_conversion", (SERVICE, None), {}),
    ("set_ppd_weekday", (1,), {"enabled": True}),
    ("run_backup", (), {}),
    ("send_test_email", ("user@example.com",), {"allow_send": True}),
    ("set_web_server_port", (8080,), {"allow_network_change": True}),
    ("set_remote_management", (), {"connect": True, "allow_remote_change": True}),
    ("set_web_user_screens", ("viewer", ("Screen A",)), {}),
    ("set_own_settings", (), {"time_24h": False}),
    ("set_emergency_stop_enabled", ("Program 1",), {"enabled": False}),
    ("create_emergency_stop_program", ("Program 2",), {}),
    ("rename_emergency_stop_program", ("Program 1", "Program 2"), {}),
    ("copy_emergency_stop_program", ("Program 1", "Program 2"), {}),
    ("delete_emergency_stop_program", ("Program 1",), {}),
    ("set_emergency_stop_settings", ("Program 1",), {}),
    ("release_emergency_stop", ("Program 1",), {"allow_safety_change": True}),
    ("create_temperature_limit", ("Wing A",), {}),
    ("set_temperature_limit", ("Wing A",), {"lower": 18}),
    ("set_temperature_limit_members", ("Wing A", ()), {}),
    ("set_temperature_limit_enabled", ("Wing A",), {"enabled": True}),
    ("delete_temperature_limit", ("Wing A",), {}),
    ("create_sliding_temperature", ("Wing A",), {}),
    ("set_sliding_temperature", ("Wing A",), {}),
    ("set_sliding_temperature_members", ("Wing A", ()), {}),
    ("set_sliding_temperature_enabled", ("Wing A",), {"enabled": True}),
    ("delete_sliding_temperature", ("Wing A",), {}),
    ("set_heating_optimisation", (), {"enabled": True}),
    ("set_interlock_enabled", ("Program A",), {"enabled": True}),
    ("create_interlock", ("Program A",), {}),
    ("rename_interlock", ("Program A", "Program B"), {}),
    ("copy_interlock", ("Program A", "Program B"), {}),
    ("delete_interlock", ("Program A",), {}),
    ("set_interlock_setup", ("Program A",), {}),
    ("set_demand_target", (120.5,), {}),
    ("set_demand_forced_level", ("Level 1",), {}),
    ("set_demand_level_setup", ("Control 1",), {}),
    ("set_demand_group", ("Control 1", "Group 1"), {}),
    ("suspend_energy_saving", (), {}),
    ("disable_energy_saving", (SERVICE,), {"allow_service_change": True}),
    ("set_demand_pulse_setup", (SERVICE,), {"allow_service_change": True}),
    ("set_demand_interlock_ports", (SERVICE, {2: "Port 3"}), {}),
    ("set_leakage_check_indoor_units", (SERVICE, "OU-1", ()), {}),
    ("set_leakage_check_schedule", (SERVICE, "OU-1", 1), {}),
    ("copy_leakage_check_schedule", (SERVICE, "OU-1", ("OU-2",)), {}),
    ("start_leakage_check", (SERVICE, "OU-1"), {"allow_leakage_check": True}),
    ("stop_leakage_check", (SERVICE, "OU-1"), {"allow_leakage_check": True}),
    ("run_layout_setup", (SERVICE,), {"allow_service_change": True}),
    ("set_r32_output_port", (SERVICE, 1, "Alarm out A"), {}),
    ("set_password_setting", ("admin",), {}),
    ("set_unit_settings", ("1:1-00",), {"low_noise": "Step 2"}),
    ("set_region", (), {"temperature_unit": "°F"}),
    ("set_zone_member_order", ("Zone A", ("1:1-00",)), {"use_jump": True}),
]


@pytest.fixture
def hw_itm() -> FakeITM:
    """Return a simulated iTM whose first unit offers low noise."""
    fake = FakeITM()
    unit = fake.units[0]
    unit.low_noise, unit.outdoor_low_noise, unit.capacity = 0, 0, 0
    unit.reheat, unit.leaving_water, unit.storage_water = False, (7.0, 45.0), 50.0
    return fake


@pytest.mark.parametrize(("method", "args", "kwargs"), GUARDED)
async def test_refused_without_opt_in(
    method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    """Without allow_experimental an experimental write sends nothing."""
    itm = FakeITM()
    client = FakeClient(itm, allow_experimental=False)
    with pytest.raises(ExperimentalError, match=REFUSED):
        await getattr(client, method)(*args, **kwargs)
    assert not itm.requests
    with pytest.raises(ReadOnlyError):  # read only is checked first
        await getattr(FakeClient(itm, read_only=True), method)(*args, **kwargs)
    assert not itm.requests


def test_defaults_and_error_class() -> None:
    """The opt-in is off by default; the error is an ItmError, not a command error."""
    assert FakeClient(FakeITM(), allow_experimental=False).allow_experimental is False
    assert issubclass(ExperimentalError, ItmError)
    assert not issubclass(ExperimentalError, ItmCommandError)


async def _check_area(
    itm: FakeITM, flow: Callable[[FakeClient], Awaitable[Any]], **kw: Any
) -> None:
    """Refused without the opt-in; planned in a dry run; done with it."""
    with pytest.raises(ExperimentalError):
        await flow(FakeClient(itm, allow_experimental=False, **kw))
    assert not itm.requests
    dry = FakeClient(itm, allow_experimental=False, dry_run=True, **kw)
    dry.progress_interval = 0
    assert await flow(dry) is None
    assert dry.planned
    client = FakeClient(itm, allow_experimental=True, **kw)
    client.progress_interval = 0
    assert await flow(client) is not None


async def test_emergency_stop() -> None:
    """An emergency stop program write."""
    itm = FakeEmergencyITM()
    await _check_area(itm, lambda c: c.create_emergency_stop_program("Program 2"))
    assert itm.program("Program 2") is not None


async def test_dealer_functions() -> None:
    """A dealer function (interlock) write."""
    await _check_area(FakeDealerITM(), lambda c: c.create_interlock("Program A"))


async def test_demand() -> None:
    """A demand control write."""
    itm = FakeDemandITM()
    await _check_area(itm, lambda c: c.set_demand_target(120.5))
    assert itm.target == 120.5


async def test_energy_navigator() -> None:
    """An energy navigator write (it returns None, so checked on the simulator)."""
    itm = FakeEnergyMoreITM()
    guarded = FakeClient(itm, allow_experimental=False)
    with pytest.raises(ExperimentalError):
        await guarded.rename_energy_rule("Rule A", "Rule C")
    assert not itm.requests
    dry = FakeClient(itm, allow_experimental=False, dry_run=True)
    dry.progress_interval = 0
    await dry.rename_energy_rule("Rule A", "Rule C")
    assert dry.planned
    client = FakeClient(itm, allow_experimental=True)
    client.progress_interval = 0
    await client.rename_energy_rule("Rule A", "Rule C")
    assert "Rule C" in [r[0] for r in itm.rules]


async def test_test_email() -> None:
    """The test e-mail (it also needs allow_send)."""
    itm = FakeSystemEditITM()
    itm.emails["user@example.com"] = []
    client = FakeClient(itm, allow_experimental=False)
    with pytest.raises(ExperimentalError):
        await client.send_test_email("user@example.com", allow_send=True)
    assert not itm.requests
    dry = FakeClient(itm, allow_experimental=False, dry_run=True)
    await dry.send_test_email("user@example.com")
    assert dry.planned
    client.allow_experimental = True
    with pytest.raises(ItmCommandError, match="allow_send"):
        await client.send_test_email("user@example.com")
    await client.send_test_email("user@example.com", allow_send=True)


async def test_guards_add_up() -> None:
    """A leakage check start needs allow_experimental and allow_leakage_check."""
    itm = FakeServiceEditITM()
    client = FakeClient(itm, allow_experimental=False)
    with pytest.raises(ExperimentalError):
        await client.start_leakage_check(SERVICE, "OU-1", allow_leakage_check=True)
    client.allow_experimental = True
    with pytest.raises(ItmCommandError, match="allow_leakage_check"):
        await client.start_leakage_check(SERVICE, "OU-1")
    assert not itm.requests
    assert not itm.outdoors[0].running
    unit = await client.start_leakage_check(SERVICE, "OU-1", allow_leakage_check=True)
    assert unit is not None
    assert itm.outdoors[0].running
    dry = FakeClient(itm, allow_experimental=False, dry_run=True)
    assert await dry.stop_leakage_check(SERVICE, "1-00") is None


async def test_own_settings_per_argument() -> None:
    """The own icon colour needs no opt-in; the own language does."""
    itm = FakeUsersITM()
    client = FakeClient(itm, allow_experimental=False)
    assert (await client.set_own_settings(icon_colour=2)) is not None
    assert itm.own["colour"] == 2
    itm.requests.clear()
    with pytest.raises(ExperimentalError, match="own language: not validated"):
        await client.set_own_settings(language="de", icon_colour=1)
    assert not itm.requests


async def test_unit_settings_per_argument(hw_itm: FakeITM) -> None:
    """Low noise is experimental; the validated settings need no opt-in."""
    unit = hw_itm.units[0]
    client = FakeClient(hw_itm, allow_experimental=False)
    result = await client.set_unit_settings(unit.name, overtime=True)
    assert result is not None
    assert result.overtime is True
    hw_itm.requests.clear()
    with pytest.raises(ExperimentalError, match="set low_noise: not validated"):
        await client.set_unit_settings(unit.name, low_noise="Step 2", overtime=False)
    assert not hw_itm.requests
    assert unit.low_noise == 0
    dry = FakeClient(hw_itm, allow_experimental=False, dry_run=True)
    assert await dry.set_unit_settings(unit.name, low_noise="Step 2") is None
    assert dry.planned
    client.allow_experimental = True
    result = await client.set_unit_settings(unit.name, low_noise="Step 2")
    assert result is not None
    assert result.low_noise == "Step 2"


async def test_region_per_argument() -> None:
    """°F and the decimal comma are experimental; the date and time formats not."""
    itm = FakeITM()
    client = FakeClient(itm, allow_experimental=False)
    info = await client.set_region(date_format="%Y/%m/%d", time_24h=False)
    assert info is not None
    assert info.time_24h is False
    itm.requests.clear()
    with pytest.raises(ExperimentalError, match="temperature_unit and decimal"):
        await client.set_region(
            date_format="%d/%m/%Y", temperature_unit="°F", decimal_separator=","
        )
    assert not itm.requests
    dry = FakeClient(itm, allow_experimental=False, dry_run=True)
    assert await dry.set_region(temperature_unit="°F") is None
    client.allow_experimental = True
    info = await client.set_region(decimal_separator=",")
    assert info is not None
    assert info.decimal_separator == ","


async def test_zone_member_order_per_argument() -> None:
    """The up button is validated; Jump is experimental."""
    itm = FakeZonesITM()
    a, b, c = (u.name for u in itm.units[:3])
    client = FakeClient(itm, allow_experimental=False)
    assert await client.set_zone_members("Zone A", (a, b, c)) is not None
    assert await client.set_zone_member_order("Zone A", (c,)) == (c, a, b)
    itm.requests.clear()
    with pytest.raises(ExperimentalError, match="Jump"):
        await client.set_zone_member_order("Zone A", (a,), use_jump=True)
    assert not itm.requests
    itm.jump_enabled = True
    client.allow_experimental = True
    assert await client.set_zone_member_order("Zone A", (a,), use_jump=True) == (
        a,
        c,
        b,
    )


async def test_validated_write_needs_no_opt_in() -> None:
    """A validated write (unit power) works without allow_experimental."""
    itm = FakeITM()
    client = FakeClient(itm, allow_experimental=False)
    unit = await client.set_power("1:1-04", on=False)
    assert unit is not None
    assert unit.running is False


def test_cli_flag(itm: FakeITM, capsys: pytest.CaptureFixture[str]) -> None:
    """`--experimental` passes the opt-in; without it the CLI prints the refusal."""
    unit = itm.units[0]
    unit.low_noise, unit.capacity, unit.reheat = 0, 0, False
    unit.leaving_water = (7.0, 45.0)
    base = ["--host", "fake-host", "--zone", "Building/Indoor units"]
    command = ["settings", unit.name, "--low-noise", "Step 2"]
    with pytest.raises(SystemExit) as exc:
        itm_cli.main([*base, *command])
    assert str(exc.value) == (
        f"error: {unit.name}: set low_noise: {REFUSED} (or --experimental) to use it"
    )
    assert unit.low_noise == 0
    itm_cli.main([*base, "--dry-run", *command])
    assert "save settings" in capsys.readouterr().out
    assert unit.low_noise == 0
    itm_cli.main([*base, "--experimental", *command])
    assert "low noise: Step 2" in capsys.readouterr().out
    assert unit.low_noise == 2
