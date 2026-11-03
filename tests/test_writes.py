# pylint: disable=protected-access,redefined-outer-name
"""Writes through the main menu: auto changeover groups (with dry runs)."""

from __future__ import annotations

from typing import Any

import pytest

from daikin_itm import (
    ChangeoverReference,
    ItmCommandError,
    PowerState,
    ReadOnlyError,
    RemoteLock,
    TempLimit,
)

from .fake_itm import FakeGroup, FakeITM
from .test_client import FakeClient


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM with one disabled group with a member."""
    fake = FakeITM()
    fake.groups = [FakeGroup("Group 01", members=["1:1-04"])]
    return fake


def _confirmed(sent: list[dict[str, Any]], screen: int, part: int) -> bool:
    return any(r["screen"] == screen and r["part"] == part for r in sent)


async def test_enable_and_disable(itm: FakeITM) -> None:
    """Enable presses Enable, the result is read back in a fresh session."""
    c = FakeClient(itm)
    group = await c.set_changeover_enabled("Group 01", enabled=True)
    assert group is not None
    assert group.enabled is True
    assert group.members == ("1:1-04",)
    assert itm.groups[0].enabled is True
    assert itm.open_screens == set()
    assert itm.session is None
    group = await c.set_changeover_enabled("Group 01", enabled=False)
    assert group is not None
    assert group.enabled is False
    # Already in the wanted state: nothing pressed.
    before = len(itm.requests)
    await c.set_changeover_enabled("Group 01", enabled=False)
    assert not any(
        r["screen"] == 300 and r["part"] in {4, 5} for r in itm.requests[before:]
    )


async def test_enable_needs_members(itm: FakeITM) -> None:
    """A group without members can't be enabled."""
    itm.groups = [FakeGroup("empty")]
    with pytest.raises(ItmCommandError, match="without members"):
        await FakeClient(itm).set_changeover_enabled("empty", enabled=True)
    assert itm.groups[0].enabled is False


async def test_unknown_group(itm: FakeITM) -> None:
    """A group that is not there is refused with the known names."""
    with pytest.raises(ItmCommandError, match="available"):
        await FakeClient(itm).set_changeover_enabled("nope", enabled=True)
    assert itm.open_screens == set()


async def test_dry_run_enable(itm: FakeITM) -> None:
    """A dry run records the request instead of sending it."""
    c = FakeClient(itm, dry_run=True)
    assert await c.set_changeover_enabled("Group 01", enabled=True) is None
    assert c.planned == ["enable changeover group Group 01: screen 300 part 4 event 1"]
    assert itm.groups[0].enabled is False
    assert not _confirmed(itm.requests, 300, 4)
    assert itm.session is None


async def test_settings(itm: FakeITM) -> None:
    """Differential and reference are picked, OK confirmed, read back."""
    c = FakeClient(itm)
    group = await c.set_changeover_settings(
        "Group 01", differential=1, reference=ChangeoverReference.FIXED
    )
    assert group is not None
    assert group.differential == 1
    assert group.reference is ChangeoverReference.FIXED
    assert itm.groups[0].differential == 1
    assert itm.groups[0].reference == 0
    assert _confirmed(itm.requests, 302, 30)  # Yes on "Execute setting?"
    assert itm.open_screens == set()
    # Nothing asked: nothing written.
    before = len(itm.requests)
    await c.set_changeover_settings("Group 01")
    assert not _confirmed(itm.requests[before:], 302, 11)


async def test_settings_dry_run_cancels(itm: FakeITM) -> None:
    """A dry run makes the picks, plans the OK and cancels."""
    c = FakeClient(itm, dry_run=True)
    assert await c.set_changeover_settings("Group 01", differential=4) is None
    assert c.planned == [
        "save settings of changeover group Group 01: screen 302 part 11 event 1"
    ]
    assert _confirmed(itm.requests, 302, 5)  # the pick was made
    assert _confirmed(itm.requests, 302, 12)  # then cancelled
    assert not _confirmed(itm.requests, 302, 11)
    assert itm.groups[0].differential == 3


async def test_settings_refuses_unknown_differential(itm: FakeITM) -> None:
    """A differential the iTM doesn't offer is refused before OK."""
    with pytest.raises(ItmCommandError, match="not offered"):
        await FakeClient(itm).set_changeover_settings("Group 01", differential=9)
    assert itm.open_screens == set()
    assert itm.groups[0].differential == 3


async def test_create_members_delete(itm: FakeITM) -> None:
    """A group's whole life: create, members, settings, enable, disable, delete."""
    c = FakeClient(itm)
    group = await c.create_changeover_group(
        "demo group", members=("1:1-00", "1:1-09"), differential=1
    )
    assert group is not None
    assert group.name == "demo group"
    assert group.enabled is False
    assert group.members == ("1:1-00", "1:1-09")
    assert group.differential == 1
    assert [g.name for g in itm.groups] == ["Group 01", "demo group"]

    group = await c.set_changeover_members("demo group", ("1:1-09",))
    assert group is not None
    assert group.members == ("1:1-09",)
    assert itm.groups[1].members == ["1:1-09"]

    with pytest.raises(ItmCommandError, match="exists already"):
        await c.create_changeover_group("demo group")
    with pytest.raises(ItmCommandError, match="is in group"):
        await c.set_changeover_members("demo group", ("1:1-04",))  # in Group 01
    with pytest.raises(ItmCommandError, match="not available"):
        await c.set_changeover_members("demo group", ("nope",))

    await c.set_changeover_enabled("demo group", enabled=True)
    with pytest.raises(ItmCommandError, match="disable the group"):
        await c.delete_changeover_group("demo group")
    await c.set_changeover_enabled("demo group", enabled=False)
    await c.delete_changeover_group("demo group")
    assert [g.name for g in itm.groups] == ["Group 01"]
    assert itm.open_screens == set()
    assert itm.session is None


async def test_create_dry_run(itm: FakeITM) -> None:
    """A dry run opens the name dialog, plans the name and cancels the dialog."""
    c = FakeClient(itm, dry_run=True)
    assert await c.create_changeover_group("demo group", members=("1:1-00",)) is None
    assert c.planned == [
        "create changeover group demo group: screen 300 part 11 event 1 'demo group'"
    ]
    assert any(
        r["screen"] == 300 and r["part"] == 11 and r["event"] == 2 for r in itm.requests
    )
    assert [g.name for g in itm.groups] == ["Group 01"]
    assert itm.naming is False


async def test_delete_dry_run(itm: FakeITM) -> None:
    """A dry run of a delete plans the press and sends nothing."""
    c = FakeClient(itm, dry_run=True)
    await c.delete_changeover_group("Group 01")
    assert c.planned == ["delete changeover group Group 01: screen 300 part 8 event 1"]
    assert len(itm.groups) == 1


async def test_writes_are_refused_read_only(itm: FakeITM) -> None:
    """Every changeover write is refused before anything is sent."""
    c = FakeClient(itm, read_only=True)
    for call in (
        c.set_changeover_enabled("Group 01", enabled=True),
        c.set_changeover_settings("Group 01", differential=1),
        c.create_changeover_group("x"),
        c.set_changeover_members("Group 01", ()),
        c.delete_changeover_group("Group 01"),
    ):
        with pytest.raises(ReadOnlyError):
            await call
    assert itm.requests == []


async def test_error_dialog_is_reported(itm: FakeITM) -> None:
    """An error dialog after a press is raised, not swallowed."""
    itm.groups = [FakeGroup("empty", members=[])]
    # The library refuses before pressing; make it press by lying about members.
    itm.groups[0].members = ["x"]
    c = FakeClient(itm)

    original = itm._changeover_event

    def refuse(ev: dict[str, Any]) -> list[Any] | None:
        if ev["part"] == 4:
            return [("DLG_OPEN", 300, [40, 1, "No members", "Error", "OK"], [])]
        return original(ev)

    itm._changeover_event = refuse  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="No members"):
        await c.set_changeover_enabled("empty", enabled=True)
    assert itm.session is None


# -- setback and overtime ------------------------------------------------------


async def test_set_setback_round_trip(itm: FakeITM) -> None:
    """Offsets and recovery temperatures are picked, saved and read back."""
    c = FakeClient(itm)
    result = await c.set_setback(high_up=4, low_back=1, recovery_heat=4)
    assert result is not None
    assert (result.high_up, result.high_back, result.low_up, result.low_back) == (
        4,
        5,
        1,
        1,
    )
    assert result.recovery_heat == 4
    assert itm.setback == {"high_up": 4, "high_back": 5, "low_up": 1, "low_back": 1}
    assert itm.recovery == {"cool": 3, "heat": 4}
    assert itm.open_screens == set()
    assert itm.session is None
    back = await c.set_setback(high_up=3, low_back=2, recovery_heat=1)
    assert back is not None
    assert (back.high_up, back.low_back, back.recovery_heat) == (3, 2, 1)
    assert (await c.set_setback()) == back  # nothing asked: just the read


async def test_set_setback_dry_run_and_refusals(itm: FakeITM) -> None:
    """Dry runs plan the OKs; impossible values are refused before OK."""
    c = FakeClient(itm, dry_run=True)
    assert await c.set_setback(high_up=5, recovery_cool=2) is None
    assert c.planned == [
        "save setback recovery: screen 3001 part 10 event 1",
        "save setback offsets: screen 3000 part 20 event 1",
    ]
    assert itm.setback["high_up"] == 3
    assert itm.recovery["cool"] == 3
    assert itm.open_screens == set()
    with pytest.raises(ItmCommandError, match="not offered"):
        await FakeClient(itm).set_setback(high_up=9)
    assert itm.setback["high_up"] == 3
    with pytest.raises(ReadOnlyError):
        await FakeClient(itm, read_only=True).set_setback(high_up=3)


async def test_set_overtime_duration(itm: FakeITM) -> None:
    """The overtime duration is picked by minutes, saved and read back."""
    c = FakeClient(itm)
    assert await c.set_overtime_duration(150) == 150
    assert itm.overtime_idx == 4
    assert await c.set_overtime_duration(120) == 120
    with pytest.raises(ItmCommandError, match="not offered"):
        await c.set_overtime_duration(45)
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_overtime_duration(30) is None
    assert dry.planned == ["save overtime duration 30 min: screen 700 part 3 event 1"]
    assert itm.overtime_idx == 3
    assert itm.open_screens == set()


# -- unit settings, setback command, schedule on/off --------------------------


async def test_set_unit_settings(itm: FakeITM) -> None:
    """Locks, overtime and limits go through the settings dialog and read back."""
    c = FakeClient(itm)
    result = await c.set_unit_settings(
        "1:1-09",
        start_stop_lock=RemoteLock.STOP_ONLY,
        mode_lock=RemoteLock.FORBIDDEN,
        overtime=True,
        cool_limit=TempLimit(enabled=True, minimum=20.0, maximum=26.5),
    )
    assert result is not None
    assert result.remote_locks.start_stop is RemoteLock.STOP_ONLY
    assert result.remote_locks.mode is RemoteLock.FORBIDDEN
    assert result.remote_locks.setpoint is RemoteLock.ALLOWED
    assert result.overtime is True
    assert result.cool_limit == TempLimit(enabled=True, minimum=20.0, maximum=26.5)
    assert result.heat_limit.enabled is False
    u = itm.units[2]
    assert (u.lock_start_stop, u.lock_mode, u.overtime) == (1, 2, True)
    # The number input was answered as a °C temperature: [whole, thousandths].
    numbers = [r for r in itm.requests if r["screen"] == 7 and r["part"] == 70]
    assert [r["array"] for r in numbers] == [[26, 500], [20, 0]]
    assert [r["dtype"] for r in numbers] == [16, 16]
    assert itm.dialog is None
    # And back.
    result = await c.set_unit_settings(
        "1:1-09",
        start_stop_lock=RemoteLock.ALLOWED,
        mode_lock=RemoteLock.ALLOWED,
        overtime=False,
        cool_limit=TempLimit(enabled=False, minimum=None, maximum=None),
    )
    assert result is not None
    assert result.cool_limit.enabled is False
    assert (u.lock_start_stop, u.lock_mode, u.overtime) == (0, 0, False)
    assert await c.set_unit_settings("1:1-09") is None  # nothing asked


async def test_set_unit_settings_refusals(itm: FakeITM) -> None:
    """Impossible values are refused and the dialog cancelled."""
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="not offered"):
        await c.set_unit_settings("1:1-09", mode_lock=RemoteLock.STOP_ONLY)
    with pytest.raises(ItmCommandError, match="outside the iTM's range"):
        await c.set_unit_settings(
            "1:1-09", heat_limit=TempLimit(enabled=True, minimum=10.0, maximum=30.0)
        )
    assert itm.dialog is None
    assert itm.units[2].heat_limit == (False, 16.0, 32.0)
    with pytest.raises(ReadOnlyError):
        await FakeClient(itm, read_only=True).set_unit_settings("1:1-09", overtime=True)


async def test_set_unit_settings_dry_run(itm: FakeITM) -> None:
    """A dry run makes the picks, plans the OK and cancels the dialog."""
    c = FakeClient(itm, dry_run=True)
    assert await c.set_unit_settings("1:1-09", overtime=True) is None
    assert c.planned == ["save settings: screen 7 part 5 event 1"]
    assert itm.units[2].overtime is False
    assert itm.dialog is None


async def test_set_power_state(itm: FakeITM) -> None:
    """Setback low/high and back, through the operation panel."""
    c = FakeClient(itm)
    unit = await c.set_power_state("1:1-00", PowerState.SETBACK_LOW)
    assert unit is not None
    assert unit.power_state is PowerState.SETBACK_LOW
    assert unit.setback is True
    assert itm.units[0].setback == 1
    unit = await c.set_power_state("1:1-00", PowerState.ON)
    assert unit is not None
    assert unit.running is True
    with pytest.raises(ItmCommandError, match="not offered"):
        await c.set_power_state("1:1-00", PowerState.SETBACK)
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_power_state("1:1-00", PowerState.OFF) is None
    assert dry.planned == [
        "pick option 3: screen 4 part 9",
        "commit: screen 4 event 3",
    ]
    assert itm.units[0].running is True


async def test_set_schedule_enabled(itm: FakeITM) -> None:
    """A program is enabled/disabled in its editor, OK confirmed, read back."""
    c = FakeClient(itm)
    program = await c.set_schedule_enabled("Program E", enabled=True)
    assert program is not None
    assert program.enabled is True
    assert itm.schedules[2][0] == 0
    program = await c.set_schedule_enabled("Program E", enabled=False)
    assert program is not None
    assert program.enabled is False
    assert itm.open_screens == set()
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_schedule_enabled("Program E", enabled=True) is None
    assert dry.planned == ["enable schedule Program E: screen 104 part 20 event 1"]
    assert itm.schedules[2][0] == 3
    assert itm.open_screens == set()
    # Already as asked: no OK at all.
    before = len(itm.requests)
    await c.set_schedule_enabled("Always", enabled=True)
    assert not any(
        r["screen"] == 104 and r["part"] == 20 for r in itm.requests[before:]
    )


async def test_set_unit_settings_waits_for_the_unit(itm: FakeITM) -> None:
    """The settings reach the unit a few reads later; the write waits for them."""
    itm.latency_logins = 2
    result = await FakeClient(itm).set_unit_settings("1:1-09", overtime=True)
    assert result is not None
    assert result.overtime is True


async def test_set_unit_settings_gives_up(itm: FakeITM) -> None:
    """A change that never shows is reported after confirm_timeout."""
    itm.latency_logins = 100
    c = FakeClient(itm, confirm_timeout=0.05)
    with pytest.raises(ItmCommandError, match="did not take"):
        await c.set_unit_settings("1:1-09", overtime=True)


async def test_set_power_off_ends_setback(itm: FakeITM) -> None:
    """A unit in setback is not running, but set_power(False) still stops it."""
    c = FakeClient(itm)
    await c.set_power_state("1:1-00", PowerState.SETBACK_HIGH)
    assert itm.units[0].setback == 2
    unit = await c.set_power("1:1-00", on=False)
    assert unit is not None
    assert unit.power_state is PowerState.OFF
    assert itm.units[0].setback == 0


async def test_create_and_delete_schedule(itm: FakeITM) -> None:
    """Add, rename, OK and Yes; then Delete and Yes."""
    c = FakeClient(itm)
    before = [p[1] for p in itm.schedules]
    dry = FakeClient(itm, dry_run=True)
    assert await dry.create_schedule("demo group") is None
    assert dry.planned == ["create schedule demo group: screen 104 part 20 event 1"]
    assert [p[1] for p in itm.schedules] == before
    assert 104 not in itm.open_screens
    created = await c.create_schedule("demo group")
    assert created is not None
    assert created.name == "demo group"
    assert created.enabled is False
    with pytest.raises(ItmCommandError, match="exists already"):
        await c.create_schedule("demo group")
    summary = await c.set_schedule_enabled("demo group", enabled=True)
    assert summary is not None
    assert summary.enabled is True
    await dry.delete_schedule("demo group")
    assert "demo group" in [p[1] for p in itm.schedules]
    await c.delete_schedule("demo group")
    assert [p[1] for p in itm.schedules] == before
    with pytest.raises(ItmCommandError, match="not found"):
        await c.delete_schedule("demo group")


async def test_airflow_direction(itm: FakeITM) -> None:
    """Step to a direction the shorter way, read back; refused where not offered."""
    unit = itm.units[0]
    unit.airflow = 3103  # position 1
    c = FakeClient(itm)
    settings = await c.fetch_unit_settings(unit.name)
    assert settings.airflow_direction == "position_1"
    result = await c.set_unit_settings(unit.name, airflow_direction="swing")
    assert result is not None
    assert result.airflow_direction == "swing"
    steps = [
        r["part"] for r in itm.requests if r["screen"] == 7 and r["part"] in {89, 90}
    ]
    assert steps == [89, 89]  # position 1 -> 0 -> swing, going up
    result = await c.set_unit_settings(unit.name, airflow_direction="position_4")
    assert result is not None
    assert result.airflow_direction == "position_4"
    units = await c.fetch_units(view="list")
    assert units[unit.name].airflow_direction == "position_4"
    assert units[itm.units[1].name].airflow_direction is None
    with pytest.raises(ItmCommandError, match="no adjustable airflow"):
        await c.set_unit_settings(itm.units[1].name, airflow_direction="swing")
    with pytest.raises(ItmCommandError, match="must be one of"):
        await c.set_unit_settings(unit.name, airflow_direction="sideways")


async def test_zone_operation(itm: FakeITM) -> None:
    """Power, setpoint and dialog settings for every unit of a zone at once."""
    c = FakeClient(itm, zone_path=("Building",))
    zone = "Indoor units"
    units = await c.set_zone_power_state(zone, PowerState.SETBACK_HIGH)
    assert units is not None
    assert all(u.setback == 2 for u in itm.units)
    units = await c.apply_zone(zone, power=True, fan_speed="low")
    assert units is not None
    assert all(u.running and u.fan_icon == 3009 for u in itm.units)
    units = await c.set_zone_setpoint(zone, 23)
    assert units is not None
    assert all(u.setpoint == 23 for u in itm.units)
    await c.apply_zone(zone, power=False)
    assert not any(u.running for u in itm.units)
    with pytest.raises(ItmCommandError, match="stopped"):
        await c.set_zone_setpoint(zone, 22)
    with pytest.raises(ItmCommandError, match="is not a zone"):
        await FakeClient(
            itm, zone_path=("Building", "Indoor units")
        ).set_zone_power_state(itm.units[0].name, PowerState.ON)
    dry = FakeClient(itm, zone_path=("Building",), dry_run=True)
    assert await dry.set_zone_power_state(zone, PowerState.ON) is None
    assert not any(u.running for u in itm.units)
    assert await c.apply_zone(zone) is None


async def test_filter_sign_and_dust_alarm_reset(itm: FakeITM) -> None:
    """Resets are offered only while there is something to reset."""
    unit = itm.units[0]
    unit.filter_sign = unit.dust_alarm = True
    c = FakeClient(itm)
    before = await c.fetch_unit_settings(unit.name)
    assert before.filter_sign_resettable is True
    assert before.dust_alarm_resettable is True
    units = await c.fetch_units(view="list")
    assert units[unit.name].filter_sign is True
    assert units[unit.name].dust_alarm is True
    after = await c.set_unit_settings(
        unit.name, reset_filter_sign=True, reset_dust_alarm=True
    )
    assert after is not None
    assert after.filter_sign_resettable is False
    assert (unit.filter_sign, unit.dust_alarm) == (False, False)
    with pytest.raises(ItmCommandError, match="nothing to reset"):
        await c.set_unit_settings(unit.name, reset_filter_sign=True)
    unit.dust_alarm = True
    after = await c.set_unit_settings(unit.name, force_dust_alarm_reset=True)
    assert after is not None
    assert unit.dust_alarm is False


async def test_ventilation_mode_and_amount(itm: FakeITM) -> None:
    """A ventilation unit's mode and air volume, picked by their labels."""
    vent = itm.ventilation[0]
    vent.vent_modes = ("Automatic", "ERVentilation", "Bypass")
    vent.vent_amounts = ("Auto (normal)", "Low (normal)", "High (normal)")
    c = FakeClient(itm, zone_path=("Building", "Ventilation"))
    settings = await c.fetch_unit_settings(vent.name)
    assert settings.ventilation_mode == "Automatic"
    assert settings.ventilation_modes == vent.vent_modes
    result = await c.set_unit_settings(
        vent.name, ventilation_mode="bypass", ventilation_amount="High (normal)"
    )
    assert result is not None
    assert (result.ventilation_mode, result.ventilation_amount) == (
        "Bypass",
        "High (normal)",
    )
    assert (vent.vent_mode, vent.vent_amount) == (2, 2)
    with pytest.raises(ItmCommandError, match="not offered"):
        await c.set_unit_settings(vent.name, ventilation_mode="Turbo")
    plain = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="not offered by this unit"):
        await plain.set_unit_settings(itm.units[0].name, ventilation_mode="Bypass")
