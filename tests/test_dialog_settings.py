# pylint: disable=protected-access,redefined-outer-name
"""The settings dialog's optional settings: HW supply, outputs, outdoor unit."""

from __future__ import annotations

from typing import Any

import pytest

from daikin_itm import ItmCommandError, ReadOnlyError
from daikin_itm.models import (
    DIALOG_SETTINGS,
    dialog_setting_choice,
    dialog_settings,
    settings_from_dialog,
)
from daikin_itm.protocol import Part, ScreenUpdate
from daikin_itm.state import UIState

from .fake_itm import FakeITM
from .test_client import FakeClient


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM with a HW supply unit and a unit with outputs."""
    fake = FakeITM()
    hw = fake.units[0]
    hw.low_noise, hw.outdoor_low_noise, hw.capacity = 1, 0, 0
    hw.reheat, hw.leaving_water, hw.storage_water = False, (7.0, 45.0), 50.0
    out = fake.units[1]
    out.output_repeat, out.analog_output, out.multistate = False, 0.0, 0
    return fake


def _dialog_clicks(itm: FakeITM) -> list[int]:
    return [r["part"] for r in itm.requests if r["screen"] == 7]


async def test_read_hw_supply_unit(itm: FakeITM) -> None:
    """A HW supply unit offers its tab's settings, with their options."""
    s = await FakeClient(itm).fetch_unit_settings(itm.units[0].name)
    assert s.tabs == (
        "Common",
        "R/C Prohibition",
        "A/C",
        "HW Supply",
    )
    assert (s.low_noise, s.low_noise_options[-1]) == ("Step 1", "Step 3")
    assert s.outdoor_low_noise == "Off"
    assert (s.capacity, s.capacity_options) == (100, (100, 70, 40, 0))
    assert s.reheat is False
    assert (s.leaving_water_cool, s.leaving_water_heat) == (7.0, 45.0)
    assert s.storage_water == 50.0
    assert s.output_repeat is s.analog_output is s.multistate_output is None
    assert s.output_repeat_interval_options == ()
    assert s.r32_alarm_stoppable is False


async def test_read_outputs_unit(itm: FakeITM) -> None:
    """A unit with outputs offers repeat mode, analog value and multistate."""
    s = await FakeClient(itm).fetch_unit_settings(itm.units[1].name)
    assert "Dio, Ao, Mo" in s.tabs
    assert "HW Supply" not in s.tabs
    assert (s.output_repeat, s.output_repeat_interval) == (False, 3)
    assert s.output_repeat_interval_options == tuple(range(1, 11))
    assert s.analog_output == 0.0
    assert (s.multistate_output, s.multistate_output_options[1]) == (
        "State 1",
        "State 2",
    )
    assert s.low_noise is s.capacity is s.leaving_water_cool is None


async def test_plain_unit_offers_none(itm: FakeITM) -> None:
    """Hidden or disabled settings read as None (the analog one is shown disabled)."""
    s = await FakeClient(itm).fetch_unit_settings(itm.units[2].name)
    for setting in DIALOG_SETTINGS:
        assert getattr(s, setting.key) is None
    assert "Dio, Ao, Mo" not in s.tabs


async def test_write_hw_supply_settings(itm: FakeITM) -> None:
    """Each setting is ticked, set, saved with OK and confirmed by a fresh read."""
    unit = itm.units[0]
    result = await FakeClient(itm).set_unit_settings(
        unit.name,
        low_noise="step 3",
        outdoor_low_noise="Step 1",
        capacity=40,
        reheat=True,
        leaving_water_cool=8.5,
        leaving_water_heat=50.0,
        storage_water=55.0,
    )
    assert result is not None
    assert (result.low_noise, result.outdoor_low_noise) == ("Step 3", "Step 1")
    assert (result.capacity, result.reheat) == (40, True)
    assert (result.leaving_water_cool, result.leaving_water_heat) == (8.5, 50.0)
    assert result.storage_water == 55.0
    assert (unit.low_noise, unit.capacity, unit.reheat) == (3, 2, True)
    assert unit.leaving_water == (8.5, 50.0)
    numbers = [r for r in itm.requests if r["screen"] == 7 and r["part"] == 70]
    assert [r["array"] for r in numbers] == [[8, 500], [50, 0], [55, 0]]
    assert {r["dtype"] for r in numbers} == {16}  # °C: [whole, thousandths]
    assert itm.dialog is None


async def test_write_outputs(itm: FakeITM) -> None:
    """Repeat mode with its interval, the analog value and the multistate output."""
    unit = itm.units[1]
    result = await FakeClient(itm).set_unit_settings(
        unit.name,
        output_repeat=True,
        output_repeat_interval=5,
        analog_output=12.5,
        multistate_output="State 3",
    )
    assert result is not None
    assert (result.output_repeat, result.output_repeat_interval) == (True, 5)
    assert (result.analog_output, result.multistate_output) == (12.5, "State 3")
    assert (unit.output_repeat, unit.output_interval, unit.multistate) == (True, 4, 2)
    numbers = [r for r in itm.requests if r["screen"] == 7 and r["part"] == 70]
    assert [(r["dtype"], r["array"]) for r in numbers] == [(4, [12, 500])]  # float
    # The repeat mode's checkbox is ticked (once per setting) before its controls.
    clicks = _dialog_clicks(itm)
    assert clicks.index(94) < clicks.index(96) < clicks.index(98)


async def test_dry_run_plans_and_cancels(itm: FakeITM) -> None:
    """A dry run makes the picks, plans the OK and cancels: nothing changes."""
    c = FakeClient(itm, dry_run=True)
    assert await c.set_unit_settings(itm.units[0].name, capacity=0) is None
    assert c.planned == ["save settings: screen 7 part 5 event 1"]
    assert itm.units[0].capacity == 0
    assert 6 in _dialog_clicks(itm)
    assert itm.dialog is None


async def test_read_only_refuses_before_sending(itm: FakeITM) -> None:
    """A read-only client sends nothing for a write."""
    c = FakeClient(itm, read_only=True)
    with pytest.raises(ReadOnlyError):
        await c.set_unit_settings(itm.units[0].name, reheat=True)
    assert itm.requests == []


@pytest.mark.parametrize(
    ("unit", "change", "match"),
    [
        (2, {"low_noise": "Off"}, "low noise: not offered by this unit"),
        (2, {"analog_output": 1.0}, "analog output: not offered by this unit"),
        (0, {"output_repeat": True}, "not offered by this unit"),
        (0, {"capacity": 50}, r"capacity: 50 not offered: \[100, 70, 40, 0\]"),
        (0, {"low_noise": "Turbo"}, "low noise: 'Turbo' not offered"),
    ],
)
async def test_refuses_what_the_unit_does_not_offer(
    itm: FakeITM, unit: int, change: dict[str, Any], match: str
) -> None:
    """Refused after opening the dialog, before any click in it; then cancelled."""
    with pytest.raises(ItmCommandError, match=match):
        await FakeClient(itm).set_unit_settings(
            itm.units[unit].name, overtime=True, **change
        )
    assert _dialog_clicks(itm) == [6]  # only Cancel
    assert itm.units[unit].overtime is False
    assert itm.dialog is None


async def test_number_outside_range_is_refused(itm: FakeITM) -> None:
    """The number input's range is checked; the dialog is cancelled."""
    with pytest.raises(ItmCommandError, match="outside the iTM's range"):
        await FakeClient(itm).set_unit_settings(
            itm.units[0].name, leaving_water_cool=30.0
        )
    assert itm.units[0].leaving_water == (7.0, 45.0)
    assert itm.dialog is None


async def test_control_still_disabled_is_not_clicked(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When ticking doesn't enable a control, it is refused, never clicked."""
    original = itm._extra_settings_event

    def no_enable(ev: dict[str, Any]) -> Any:
        updates = original(ev)
        if ev["part"] == 147 and updates:
            updates = [
                (k, s, d, [p for p in ps if p[1] != "ENABLED"])
                for k, s, d, ps in updates
            ]
        return updates

    monkeypatch.setattr(itm, "_extra_settings_event", no_enable)
    with pytest.raises(ItmCommandError, match="keeps its control disabled"):
        await FakeClient(itm).set_unit_settings(itm.units[0].name, capacity=70)
    assert 148 not in _dialog_clicks(itm)
    assert itm.units[0].capacity == 0


async def test_not_taken_is_reported(itm: FakeITM) -> None:
    """A change the read-back doesn't show is reported."""
    itm.latency_logins = 100  # the change never shows within the timeout
    c = FakeClient(itm, confirm_timeout=0)
    with pytest.raises(ItmCommandError, match="did not take"):
        await c.set_unit_settings(itm.units[0].name, low_noise="Step 2")


def _state(parts: list[tuple[int, str, Any]]) -> UIState:
    s = UIState()
    s.apply([ScreenUpdate(7, "OPEN", [0], [Part(p, t, v) for p, t, v in parts])])
    return s


def test_tabs_and_options_without_tab_visibility() -> None:
    """Without TAB_VISIBLE every tab counts as shown; options read as offered."""
    state = _state(
        [
            (1, "TAB_NAME", ["A&amp;B", "C"]),
            (119, "VISIBLED", 1),
            (120, "COMBO_STRS", [" Off ", "On"]),
            (120, "SINGLE_SELIDX", 7),
            (107, "LABEL", "reheat"),
            (144, "LABEL", "R32"),
        ]
    )
    s = settings_from_dialog("unit", state)
    assert s.tabs == ("A&B", "C")
    assert (s.low_noise, s.low_noise_options) == (None, ("Off", "On"))
    assert s.reheat is None  # offered, but neither radio checked
    assert s.r32_alarm_stoppable is True
    assert set(dialog_settings(state)) == {"low_noise", "low_noise_options", "reheat"}
    low_noise = next(d for d in DIALOG_SETTINGS if d.key == "low_noise")
    assert dialog_setting_choice(state, low_noise, "On") == 1
