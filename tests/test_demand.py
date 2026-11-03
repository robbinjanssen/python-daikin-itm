# pylint: disable=protected-access,redefined-outer-name
"""Demand (power limit) control and energy saving, against the simulated iTM."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import (
    DemandControl,
    EnergySaving,
    ItmCommandError,
    ReadOnlyError,
    demand,
)
from daikin_itm import cli as itm_cli
from daikin_itm.demand import PulseChange, check_interlock_ports, check_pulse_setup
from daikin_itm.service import DemandPulseSetup

from .fake_demand import FakeDemandITM
from .test_cli import CliClient
from .test_client import FakeClient

if TYPE_CHECKING:
    from pathlib import Path

PASSWORD = "service-secret"


@pytest.fixture
def itm() -> FakeDemandITM:
    """Return a simulated iTM with demand control and its service password set."""
    fake = FakeDemandITM()
    fake.service_password = PASSWORD
    return fake


def saves(itm: FakeDemandITM) -> list[str]:
    """Return what the simulated iTM saved."""
    return list(itm.saved)


# -- reading ---------------------------------------------------------------------------


async def test_fetch_all(itm: FakeDemandITM) -> None:
    """Every part is read and every screen left again."""
    dc = await FakeClient(itm).fetch_demand_control()
    assert isinstance(dc, DemandControl)
    st = dc.status
    assert st.target is not None
    assert st.target.value == 100
    assert st.power_unit == "kW"
    assert st.remaining == "12:30"
    assert st.state == "Normal"
    assert st.graph
    assert dc.legend is not None
    assert dc.legend.consumption == "Consumption"
    assert dc.target_power is not None
    assert dc.target_power.value == 100
    assert dc.setup is not None
    assert [(c.name, c.enabled) for c in dc.setup.controls] == [
        ("Control A", True),
        ("Control B", True),
        ("Control C", False),
    ]
    assert dc.setup.forced_level == "None"
    assert dc.levels is not None
    kinds = [(lv.control, lv.kind, lv.group) for lv in dc.levels]
    assert kinds == [
        ("Control A", "temperature", "Group 1"),
        ("Control A", "temperature", "Group 2"),
        ("Control B", "capacity", "Group 3"),
        ("Control C", "on_off", None),
    ]
    first = dc.levels[0]
    assert first.start_level == "Level 1"
    assert first.levels == ("0",) * 8
    assert first.upper_cool is not None
    assert first.upper_cool.value == 28
    assert dc.levels[2].start_level is None
    assert dc.levels[2].levels[0] == "100%"
    assert dc.levels[3].levels == ()
    assert dc.groups is not None
    assert [(g.name, g.members) for g in dc.groups] == [
        ("Group 1", (itm.points[0],)),
        ("Group 2", ()),
        ("Group 3", (itm.points[1],)),
    ]
    assert dc.groups[0].condition == "Always"
    assert dc.control_status is not None
    assert [t.name for t in dc.control_status] == ["Tab 1", "Tab 2", "Tab 3"]
    assert dc.control_status[0].groups == (("Group 1", "Level 1"),)
    assert dc.graphs is not None
    assert [g.name for g in dc.graphs] == ["Daily", "Monthly", "Yearly"]
    assert dc.graphs[0].selection == ("2025", "01", "01")
    assert dc.graphs[2].data is not None
    assert dc.graphs[2].data.rows[0] == ("00:30", "12.5")
    assert itm.open_screens == set()
    assert itm.saved == []


async def test_fetch_only_and_unavailable(itm: FakeDemandITM) -> None:
    """Only the named parts; None where the button opens nothing or is disabled."""
    c = FakeClient(itm)
    dc = await c.fetch_demand_control(["legend"])
    assert dc is not None
    assert dc.legend is not None
    assert dc.setup is None
    with pytest.raises(ItmCommandError, match="unknown demand control parts"):
        await c.fetch_demand_control(["nope"])
    itm.available = False
    assert await c.fetch_demand_control() is None
    itm.available = True
    itm.disabled.add((5, 109))
    assert await c.fetch_demand_control() is None  # and not clicked (no hang)


async def test_disabled_detail_and_modify(itm: FakeDemandITM) -> None:
    """A disabled detail button gives no data; a disabled Modify is skipped."""
    itm.disabled.add((3303, 23))
    itm.controls[1].groups = []  # Modify stays disabled for a capacity without groups
    dc = await FakeClient(itm).fetch_demand_control(["graphs", "levels"])
    assert dc is not None
    assert dc.graphs is not None
    assert dc.graphs[2].data is None
    assert dc.levels is not None
    assert [lv.control for lv in dc.levels] == ["Control A", "Control A", "Control C"]


# -- demand control writes -------------------------------------------------------------


async def test_target(itm: FakeDemandITM) -> None:
    """The target power is entered, saved and read back; dry run and read only."""
    status = await FakeClient(itm).set_demand_target(120.5)
    assert status is not None
    assert status.target is not None
    assert status.target.value == 120.5
    assert itm.target == 120.5
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_demand_target(90) is None
    assert itm.target == 120.5
    assert any("save target power 90" in line for line in dry.planned)
    with pytest.raises(ReadOnlyError):
        await FakeClient(itm, read_only=True).set_demand_target(90)
    with pytest.raises(ItmCommandError, match="outside"):
        await FakeClient(itm).set_demand_target(100000)
    assert itm.open_screens == set()


async def test_disabled_part_refused(itm: FakeDemandITM) -> None:
    """A button the iTM reports disabled is never clicked."""
    itm.disabled.add((3312, 3))
    with pytest.raises(ItmCommandError, match="disabled"):
        await FakeClient(itm).set_demand_target(90)
    itm.disabled = {(3300, 27)}
    with pytest.raises(ItmCommandError, match="disabled"):
        await FakeClient(itm).set_demand_target(90)
    itm.available = False
    with pytest.raises(ItmCommandError, match="did not open"):
        await FakeClient(itm).set_demand_target(90)
    assert itm.target == 100


async def test_forced_level(itm: FakeDemandITM) -> None:
    """Picking the forced level saves it; the shown one sends nothing."""
    c = FakeClient(itm)
    assert await c.set_demand_forced_level("level 2") == "Level 2"
    assert itm.forced == 2
    itm.saved.clear()
    assert await c.set_demand_forced_level("Level 2") == "Level 2"
    assert itm.saved == []
    with pytest.raises(ItmCommandError, match="not offered"):
        await c.set_demand_forced_level("Level 9")
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_demand_forced_level("None") is None
    assert itm.forced == 2
    assert dry.planned == ["set forced level None: screen 3301 part 7 event 1 0"]


async def test_level_setups(itm: FakeDemandITM) -> None:
    """Temperature, capacity and on/off setups per group, read back."""
    c = FakeClient(itm)
    got = await c.set_demand_level_setup(
        "Control A",
        group="Group 2",
        start_level="Level 3",
        levels={1: "2", 8: "3"},
        upper_cool=27.5,
        lower_heat=19,
    )
    assert got is not None
    assert got.group == "Group 2"
    assert got.levels[0] == "2"
    g = itm.controls[0].groups[1]
    assert (g.start, g.levels[0], g.levels[7], g.upper, g.lower) == (2, 2, 3, 27.5, 19)
    cap = await c.set_demand_level_setup("Control B", levels={2: "60%"})
    assert cap is not None
    assert cap.levels[1] == "60%"
    onoff = await c.set_demand_level_setup("Control C")
    assert onoff is not None
    assert onoff.group is None
    for kwargs, match in (
        ({"upper_cool": 20.0}, "no temperature limits"),
        ({"start_level": "Level 1"}, "no start level"),
        ({"levels": {9: "60%"}}, "no level 9"),
        ({"levels": {1: "5%"}}, "not offered"),
        ({"group": "Group 9"}, "not found"),
    ):
        with pytest.raises(ItmCommandError, match=match):
            await c.set_demand_level_setup("Control B", **kwargs)
    with pytest.raises(ItmCommandError, match="not found"):
        await c.set_demand_level_setup("Control Z")
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_demand_level_setup("Control B", levels={1: "40%"}) is None
    assert itm.controls[1].groups[0].levels[0] == 0
    assert itm.open_screens == set()


async def test_group(itm: FakeDemandITM) -> None:
    """Members, condition and resume, read back; dry run changes nothing."""
    c = FakeClient(itm)
    names = itm.points
    got = await c.set_demand_group(
        "Control A",
        "Group 1",
        members=[names[1], names[2]],
        condition="Thermo on",
        resume="Manual",
    )
    assert got is not None
    assert set(got.members) == {names[1], names[2]}
    assert got.resume == "Manual"
    g = itm.controls[0].groups[0]
    assert (g.condition, g.resume) == (1, 1)
    with pytest.raises(ItmCommandError, match="is not offered"):
        await c.set_demand_group("Control A", "Group 1", members=["Nope"])
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_demand_group("Control A", "Group 1", members=[]) is None
    assert g.members == [names[1], names[2]]
    assert any("save demand group Group 1" in line for line in dry.planned)
    assert itm.open_screens == set()


class _Unchanged(FakeDemandITM):
    """An iTM that confirms every save but keeps nothing."""

    def _confirm(self, screen: int, apply: Any) -> Any:  # noqa: ARG002 - not applied
        def ignore(_: Any) -> Any:
            for s in (3312, 3307, 3308, 3309, 3311, 1512, 1513):
                if screen == s:
                    return self._close(s)
            return []

        return super()._confirm(screen, ignore)


async def test_read_back_failures() -> None:
    """A change the iTM did not keep raises after the fresh read."""
    itm = _Unchanged()
    itm.service_password = PASSWORD
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="target power"):
        await c.set_demand_target(90)
    with pytest.raises(ItmCommandError, match="forced level"):
        await c.set_demand_forced_level("Level 1")
    with pytest.raises(ItmCommandError, match="did not take the setup"):
        await c.set_demand_level_setup(
            "Control A", start_level="Level 2", levels={1: "1"}, upper_cool=25
        )
    with pytest.raises(ItmCommandError, match="did not take the group"):
        await c.set_demand_group(
            "Control A", "Group 1", members=[], condition="Thermo on"
        )
    with pytest.raises(ItmCommandError, match="did not suspend"):
        await c.suspend_energy_saving()
    with pytest.raises(ItmCommandError, match="did not disable"):
        await c.disable_energy_saving(PASSWORD, allow_service_change=True)
    with pytest.raises(ItmCommandError, match="pulse setup"):
        await c.set_demand_pulse_setup(
            PASSWORD,
            input_port1="Port 3",
            pulse_coefficient=2,
            sync_external=True,
            allow_service_change=True,
        )
    itm.demand_mode = 1513
    with pytest.raises(ItmCommandError, match="interlock ports"):
        await c.set_demand_interlock_ports(
            PASSWORD, {1: "Port 4"}, allow_service_change=True
        )


def test_checks_without_screen() -> None:
    """A read-back whose screen did not open raises."""
    with pytest.raises(ItmCommandError, match="did not open"):
        check_pulse_setup(None, PulseChange())
    with pytest.raises(ItmCommandError, match="did not open"):
        check_interlock_ports(None, {})
    setup = DemandPulseSetup((None, None), None, None, None, None)
    check_pulse_setup(setup, PulseChange())


# -- energy saving ---------------------------------------------------------------------


async def test_energy_saving(itm: FakeDemandITM) -> None:
    """Suspend and resume through 1400; the state shown sends nothing."""
    c = FakeClient(itm)
    es = await c.fetch_energy_saving()
    assert es == EnergySaving(
        active=True, suspend_status="Active", suspend_enabled=True
    )
    assert await c.suspend_energy_saving() is False
    assert itm.suspended
    itm.saved.clear()
    assert await c.suspend_energy_saving() is False  # already suspended
    assert itm.saved == []
    dry = FakeClient(itm, dry_run=True)
    assert await dry.suspend_energy_saving(suspend=False) is None
    assert itm.suspended
    assert dry.planned == ["resume energy saving: screen 1400 part 3 event 1"]
    assert await c.suspend_energy_saving(suspend=False) is True
    with pytest.raises(ReadOnlyError):
        await FakeClient(itm, read_only=True).suspend_energy_saving()
    assert itm.open_screens == set()


async def test_energy_saving_hidden_and_disabled(itm: FakeDemandITM) -> None:
    """The hidden button still opens 1400; a disabled one is not clicked."""
    itm.saving_configured = False
    itm.energy_saving = False
    c = FakeClient(itm)
    es = await c.fetch_energy_saving()
    assert (es.active, es.suspend_status, es.suspend_enabled) == (False, "Off", False)
    with pytest.raises(ItmCommandError, match="disabled"):
        await c.suspend_energy_saving(suspend=False)
    itm.disabled.add((0, 17))
    assert (await c.fetch_energy_saving()).suspend_status is None
    with pytest.raises(ItmCommandError, match="did not open"):
        await c.suspend_energy_saving(suspend=False)


async def test_energy_saving_service(itm: FakeDemandITM) -> None:
    """1508 after a service login; its disable needs allow_service_change."""
    c = FakeClient(itm)
    es = await c.fetch_energy_saving(PASSWORD)
    assert es.service is not None
    assert es.service.control_status == "Running"
    assert es.service.target is not None
    assert es.service.target.value == 100
    sent = len(itm.requests)
    with pytest.raises(ItmCommandError, match="allow_service_change"):
        await c.disable_energy_saving(PASSWORD)
    assert len(itm.requests) == sent  # refused before anything is sent
    dry = FakeClient(itm, dry_run=True)
    assert await dry.disable_energy_saving(PASSWORD) is None
    assert itm.saving_configured
    assert await c.disable_energy_saving(PASSWORD, allow_service_change=True) is False
    es = await c.fetch_energy_saving(PASSWORD)
    assert es.service is not None
    assert es.service.control_status is None  # empty while not active
    with pytest.raises(ReadOnlyError):
        await FakeClient(itm, read_only=True).disable_energy_saving(
            PASSWORD, allow_service_change=True
        )
    itm.disabled.add((5, 79))
    assert (await c.fetch_energy_saving(PASSWORD)).service is None
    with pytest.raises(ItmCommandError, match="did not open"):
        await c.disable_energy_saving(PASSWORD, allow_service_change=True)


async def test_service_tab_without_login(itm: FakeDemandITM) -> None:
    """Without the service login the service buttons answer a dialog: nothing read."""
    c = FakeClient(itm)

    async def read(s: Any) -> Any:
        return await demand.read_energy_saving_status(s)

    assert await c._in_session(read) is None


# -- the power limit control setup (service) -------------------------------------------


async def test_pulse_setup(itm: FakeDemandITM) -> None:
    """Read, change and read back the pulse setup; guarded and planned."""
    c = FakeClient(itm)
    st = await c.fetch_demand_service_settings(PASSWORD)
    assert st.pulse is not None
    assert st.pulse.input_ports == ("Port 1", None)
    assert st.pulse.sync_external is False
    assert st.interlock_ports is None
    assert st.ports == ("Port 1", "Port 2", "Port 3", "Port 4")
    with pytest.raises(ItmCommandError, match="allow_service_change"):
        await c.set_demand_pulse_setup(PASSWORD, pulse_coefficient=2)
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_demand_pulse_setup(PASSWORD, input_port2="Port 2") is None
    assert dry.planned == ["pick input port 2 Port 2: screen 1514 part 3 event 1"]
    assert await dry.set_demand_pulse_setup(PASSWORD, cutoff_value=12) is None
    assert itm.pulse["cutoff"] == 10
    got = await c.set_demand_pulse_setup(
        PASSWORD,
        input_port1="",
        input_port2="Port 4",
        pulse_coefficient=2.5,
        sync_external=True,
        return_value=6,
        cutoff_value=12,
        allow_service_change=True,
    )
    assert got is not None
    assert got.input_ports == (None, "Port 4")
    assert itm.pulse["coefficient"] == 2.5
    assert itm.pulse["sync_external"] is True
    with pytest.raises(ItmCommandError, match="interlock"):
        await c.set_demand_interlock_ports(
            PASSWORD, {1: "Port 1"}, allow_service_change=True
        )
    with pytest.raises(ItmCommandError, match="not found"):
        await c.set_demand_pulse_setup(
            PASSWORD, input_port1="Port 9", allow_service_change=True
        )
    assert itm.open_screens == set()


async def test_interlock_setup(itm: FakeDemandITM) -> None:
    """Interlock ports: picked, cleared, read back; guarded."""
    itm.demand_mode = 1513
    c = FakeClient(itm)
    st = await c.fetch_demand_service_settings(PASSWORD)
    assert st.interlock_ports == ("Port 2", None, None)
    assert st.pulse is None
    got = await c.set_demand_interlock_ports(
        PASSWORD, {1: "", 3: "Port 1"}, allow_service_change=True
    )
    assert got == (None, None, "Port 1")
    with pytest.raises(ItmCommandError, match="no interlock input port 4"):
        await c.set_demand_interlock_ports(
            PASSWORD, {4: "Port 1"}, allow_service_change=True
        )
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_demand_interlock_ports(PASSWORD, {2: "Port 3"}) is None
    assert await dry.set_demand_interlock_ports(PASSWORD, {2: ""}) is None
    assert itm.interlock == ["", "", "Port 1"]
    with pytest.raises(ItmCommandError, match="pulse setup"):
        await c.set_demand_pulse_setup(
            PASSWORD, pulse_coefficient=1, allow_service_change=True
        )
    itm.disabled.add((1513, 3))
    assert (await c.fetch_demand_service_settings(PASSWORD)).ports == ()
    itm.disabled.add((5, 113))
    assert await c.fetch_demand_service_settings(PASSWORD) == type(st)()


# -- the CLI ---------------------------------------------------------------------------


def cli(*args: str) -> None:
    """Run the CLI against the simulator of CliClient."""
    itm_cli.main(["--host", "fake-host", "--experimental", *args])


@pytest.fixture
def cli_itm(
    itm: FakeDemandITM, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> FakeDemandITM:
    """Route the CLI to the simulator."""
    monkeypatch.setattr(CliClient, "itm", itm, raising=False)
    monkeypatch.setattr(CliClient, "sigterm_at", None, raising=False)
    monkeypatch.setattr(itm_cli, "ItmClient", CliClient)
    monkeypatch.setattr(itm_cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("ITM_USERNAME", "user")
    monkeypatch.setenv("ITM_PASSWORD", "secret")
    monkeypatch.delenv("ITM_SERVICE_PASSWORD", raising=False)
    return itm


def test_cli_demand(
    cli_itm: FakeDemandITM,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every demand action, a dry run and the guards."""
    itm = cli_itm
    cli("demand")
    out = capsys.readouterr().out
    assert "target 100 kW" in out
    assert "control Control A: on" in out
    assert "Control A [temperature] group Group 1: start Level 1" in out
    assert "Control B group Group 3" in out
    assert "graph Daily: 2025/01/01, 2 rows" in out
    cli("demand", "target", "110")
    assert itm.target == 110
    cli("--dry-run", "demand", "forced-level", "Level 1")
    assert "dry run, would send:" in capsys.readouterr().out
    cli("demand", "forced-level", "Level 1")
    cli("demand", "level", "Control A", "--level", "1=3", "--upper-cool", "26")
    cli("demand", "group", "Control B", "Group 3", "--member", itm.points[2])
    assert itm.controls[1].groups[0].members == [itm.points[2]]
    for args, match in (
        (("demand", "target"), "one value"),
        (("demand", "group", "Control B"), "is needed"),
        (("demand", "level", "Control A", "--level", "x"), "N=VALUE"),
        (("demand", "service"), "ITM_SERVICE_PASSWORD"),
    ):
        with pytest.raises(SystemExit, match=match):
            cli(*args)
    monkeypatch.setenv("ITM_SERVICE_PASSWORD", PASSWORD)
    cli("demand", "service")
    assert "ports offered: Port 1, Port 2" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="allow_service_change"):
        cli("demand", "pulse", "--coefficient", "3")
    cli("demand", "pulse", "--coefficient", "3", "--sync", "external",
        "--allow-service-change")  # fmt: skip
    assert itm.pulse["coefficient"] == 3
    itm.demand_mode = 1513
    cli("demand", "interlock", "--port", "2=Port 3", "--allow-service-change")
    assert itm.interlock[1] == "Port 3"
    itm.available = False
    capsys.readouterr()
    cli("demand", "show", "--only", "legend")
    assert "not available" in capsys.readouterr().out


def test_cli_energy_saving(
    cli_itm: FakeDemandITM,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Show, suspend, resume and disable energy saving."""
    itm = cli_itm
    cli("energy-saving")
    out = capsys.readouterr().out
    assert "energy saving active: on" in out
    assert "status:" not in out
    cli("energy-saving", "suspend")
    assert "energy saving active: off" in capsys.readouterr().out
    cli("energy-saving", "resume")
    assert not itm.suspended
    monkeypatch.setenv("ITM_SERVICE_PASSWORD", PASSWORD)
    cli("energy-saving", "show")
    assert "control Running" in capsys.readouterr().out
    cli("--dry-run", "energy-saving", "disable")
    assert "disable energy saving" in capsys.readouterr().out
    cli("energy-saving", "disable", "--allow-service-change")
    assert not itm.saving_configured
