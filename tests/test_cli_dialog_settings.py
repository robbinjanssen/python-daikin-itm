# pylint: disable=redefined-outer-name
"""`itm settings` with the settings dialog's optional settings."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .test_cli import fixture_itm, run

if TYPE_CHECKING:
    import pytest

    from .fake_itm import FakeITM

__all__ = ["fixture_itm"]  # the simulator the CLI talks to (autouse)


def test_show_and_change_hw_supply(
    itm: FakeITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """Offered settings are listed with their options, and changed by option."""
    unit = itm.units[0]
    unit.low_noise, unit.capacity, unit.reheat = 0, 0, False
    unit.leaving_water, unit.r32_alarm_stop = (7.0, 45.0), True
    run("settings", unit.name)
    out = capsys.readouterr().out
    assert "R32 alarm stop: offered" in out
    assert "dialog tabs: Common, R/C Prohibition, A/C, HW" in out
    assert "low noise: Off (Off, Step 1, Step 2, Step 3)" in out
    assert "capacity: 100 (100, 70, 40, 0)" in out
    assert "reheat: off" in out
    assert "leaving water cool: 7.0" in out
    assert "storage water" not in out
    run(
        "settings",
        unit.name,
        "--low-noise",
        "Step 2",
        "--capacity",
        "0",
        "--reheat",
        "on",
        "--leaving-water-heat",
        "40",
    )
    out = capsys.readouterr().out
    assert "low noise: Step 2" in out
    assert "capacity: 0 (" in out
    assert "reheat: on" in out
    assert "leaving water heat: 40.0" in out
    assert (unit.low_noise, unit.capacity, unit.reheat) == (2, 3, True)


def test_change_outputs_dry_run(
    itm: FakeITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run plans the change; the outputs stay as they are."""
    unit = itm.units[1]
    unit.output_repeat, unit.analog_output, unit.multistate = False, 1.0, 0
    run(
        "--dry-run",
        "settings",
        unit.name,
        "--output-repeat",
        "on",
        "--output-repeat-interval",
        "1",
        "--analog-output",
        "2.5",
        "--multistate",
        "State 2",
    )
    assert "save settings" in capsys.readouterr().out
    assert (unit.output_repeat, unit.analog_output, unit.multistate) == (
        False,
        1.0,
        0,
    )
