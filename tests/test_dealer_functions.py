# pylint: disable=protected-access,redefined-outer-name
"""Dealer-option functions: temperature limit, sliding temperature, HMO, interlock."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import (
    InterlockOutputSpec,
    ItmCommandError,
    ReadOnlyError,
    TemperatureLimitGroup,
)
from daikin_itm import cli as itm_cli
from daikin_itm import dealer_functions as df
from daikin_itm import interlock as il
from daikin_itm.state import UIState

from .fake_dealer_functions import (
    FakeDealerITM,
    Group,
    Interlock,
    _confirm,
)
from .test_cli import CliClient
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@pytest.fixture
def itm() -> FakeDealerITM:
    """Return a simulated iTM with the dealer screens empty (as seen)."""
    return FakeDealerITM()


async def run(
    itm: FakeDealerITM,
    flow: Callable[..., Awaitable[Any]],
    *args: Any,
    client: FakeClient | None = None,
    **kwargs: Any,
) -> Any:
    """Run one flow in a fresh session, like the client's wrappers do."""
    c = client or FakeClient(itm)
    async with c._session() as s:
        try:
            return await flow(s, *args, **kwargs)
        finally:
            await s.close_menu()


PERSISTING = {60, 61}  # the confirm and name dialog answers
MAIN_SCREENS = {400, 500, 900}  # where a name answer adds, renames or copies


def writes(itm: FakeDealerITM) -> list[tuple[int, int]]:
    """Return the requests that would change something (confirms, names)."""
    return [
        (r["screen"], r["part"])
        for r in itm.requests
        if r["event"] == 1
        and (r["part"] == 60 or (r["part"] == 61 and r["screen"] in MAIN_SCREENS))
    ]


# -- readers --------------------------------------------------------------------------


async def test_empty_screens_as_seen(itm: FakeDealerITM) -> None:
    """With the dealer options off: no groups or programs, every unit off."""
    c = FakeClient(itm)
    assert await c.fetch_temperature_limits(details=True) == []
    assert await c.fetch_sliding_temperatures(details=True) == []
    assert await c.fetch_interlocks() == []
    units = await c.fetch_heating_optimisation()
    assert [u.name for u in units] == itm.points
    assert not any(u.enabled for u in units)
    assert itm.open_screens == set()


def test_parsing() -> None:
    """Temperatures in either decimal style; the off label counts as off."""
    assert df.temperature("18,5 °C") == 18.5
    assert df.temperature("-3") == -3.0
    assert df.temperature("") is None
    state = UIState()
    state.screens[900] = {
        (1, "LIST_ROW"): {
            "rows": [
                {"text": ["Disable", "A"]},
                {"text": ["Enable", "B"]},
                {"text": ["x"]},
            ]
        },
        (4, "LABEL"): "Disable",
    }
    programs = il.interlocks_from_screen(state)
    assert [(p.name, p.enabled) for p in programs] == [("A", False), ("B", True)]


# -- temperature limit ----------------------------------------------------------------


async def test_temperature_limit_flow(itm: FakeDealerITM) -> None:
    """Create with members and limits, change, enable, delete; each read back."""
    c = FakeClient(itm)
    unit_a, unit_b, unit_c = itm.points[:3]
    group = await c.create_temperature_limit(
        "Annex", members=[unit_a, unit_b], lower=18, upper=26.5
    )
    assert group == TemperatureLimitGroup(
        name="Annex",
        enabled=False,
        lower=18.0,
        upper=26.5,
        members=(unit_a, unit_b),
        columns=("", "Annex", "18.0", "26.5"),
    )
    group = await c.set_temperature_limit_members("Annex", [unit_b, unit_c])
    assert group is not None
    assert group.members == (unit_b, unit_c)
    group = await c.set_temperature_limit("Annex", new_name="Wing", lower=17)
    assert group is not None
    assert (group.name, group.lower, group.upper) == ("Wing", 17.0, 26.5)
    group = await c.set_temperature_limit_enabled("Wing", enabled=True)
    assert group is not None
    assert group.enabled
    assert (await c.set_temperature_limit_enabled("Wing", enabled=True)) == group
    with pytest.raises(ItmCommandError, match="disable the temperature limit group"):
        await c.delete_temperature_limit("Wing")
    await c.set_temperature_limit_enabled("Wing", enabled=False)
    await c.delete_temperature_limit("Wing")
    assert itm.limits == []
    assert itm.open_screens == set()


async def test_temperature_limit_refusals(itm: FakeDealerITM) -> None:
    """Unknown, duplicate, too long, not offered and out of range are refused."""
    itm.limits += [Group("A"), Group("B")]
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="exists already"):
        await c.create_temperature_limit("A")
    with pytest.raises(ItmCommandError, match="1-20 characters"):
        await c.create_temperature_limit("x" * 21)
    with pytest.raises(ItmCommandError, match="not found once"):
        await c.set_temperature_limit_enabled("C", enabled=True)
    with pytest.raises(ItmCommandError, match="not offered"):
        await c.set_temperature_limit_members("A", ["Nowhere"])
    with pytest.raises(ItmCommandError, match="exists already"):
        await c.set_temperature_limit("A", new_name="B")
    with pytest.raises(ItmCommandError, match="outside the iTM's range"):
        await c.set_temperature_limit("A", upper=99)
    assert itm.limit("A").upper == 30.0
    assert writes(itm) == []
    assert itm.open_screens == set()


# -- sliding temperature --------------------------------------------------------------


async def test_sliding_temperature_flow(itm: FakeDealerITM) -> None:
    """Create, pick the outdoor point and ranges, members, enable, delete."""
    c = FakeClient(itm)
    unit_a = itm.points[0]
    group = await c.create_sliding_temperature("Hall", members=[unit_a])
    assert group is not None
    assert group.members == (unit_a,)
    assert group.outdoor_point is None
    assert await c.fetch_sliding_temperature_inputs("Hall") == (
        "AI Outdoor sensor",
        "AI Return sensor",
    )
    group = await c.set_sliding_temperature(
        "Hall",
        new_name="Lobby",
        outdoor_point="Outdoor sensor",
        outdoor_range=(0, 30),
        setpoint_range=(21, 25.5),
    )
    assert group is not None
    assert group.name == "Lobby"
    assert group.outdoor_point == "Outdoor sensor"
    assert group.outdoor_range == (0.0, 30.0)
    assert group.setpoint_range == (21.0, 25.5)
    group = await c.set_sliding_temperature_members("Lobby", [])
    assert group is not None
    assert group.members == ()
    group = await c.set_sliding_temperature_enabled("Lobby", enabled=True)
    assert group is not None
    assert group.enabled
    shown = await c.fetch_sliding_temperatures()
    assert shown[0].columns[:3] == ("Enable", "Lobby", "Outdoor sensor")
    await c.set_sliding_temperature_enabled("Lobby", enabled=False)
    await c.delete_sliding_temperature("Lobby")
    assert itm.slides == []
    with pytest.raises(ItmCommandError, match="not found once"):
        await c.set_sliding_temperature("Lobby", outdoor_point="Nowhere")
    assert itm.open_screens == set()


async def test_sliding_point_not_offered(itm: FakeDealerITM) -> None:
    """An analog input the picker doesn't list is refused; nothing is saved."""
    itm.slides.append(Group("Hall"))
    with pytest.raises(ItmCommandError, match="analog input 'Nowhere' not found"):
        await FakeClient(itm).set_sliding_temperature("Hall", outdoor_point="Nowhere")
    assert writes(itm) == []
    assert itm.open_screens == set()


# -- heating mode optimisation --------------------------------------------------------


async def test_heating_optimisation(itm: FakeDealerITM) -> None:
    """Per unit with the radios, all with All on / All off."""
    c = FakeClient(itm)
    unit_a, unit_b = itm.points[:2]
    result = await c.set_heating_optimisation([unit_a, unit_b], enabled=True)
    assert result is not None
    assert [u.name for u in result if u.enabled] == [unit_a, unit_b]
    before = len(writes(itm))
    await c.set_heating_optimisation([unit_a], enabled=True)
    assert len(writes(itm)) == before
    result = await c.set_heating_optimisation(enabled=True)
    assert result is not None
    assert all(u.enabled for u in result)
    await c.set_heating_optimisation(enabled=True)
    result = await c.set_heating_optimisation(enabled=False)
    assert result is not None
    assert not any(u.enabled for u in result)
    with pytest.raises(ItmCommandError, match="not found once"):
        await c.set_heating_optimisation(["Nowhere"], enabled=True)
    itm.hmo.clear()  # nothing listed: All off stays disabled
    with pytest.raises(ItmCommandError, match="has this disabled"):
        await run(itm, df.set_heating_optimisation_all, enabled=False)
    assert itm.open_screens == set()


# -- dry run, read only, disabled parts -----------------------------------------------


async def test_dry_run_sends_no_writes(itm: FakeDealerITM) -> None:
    """A dry run plans every write and changes nothing."""
    itm.limits.append(Group("A"))
    itm.slides.append(Group("S"))
    itm.interlocks.append(Interlock("P"))
    dry = FakeClient(itm, dry_run=True)
    unit = itm.points[0]
    assert await dry.create_temperature_limit("B") is None
    assert await dry.set_temperature_limit("A", new_name="C", lower=17) is None
    assert await dry.set_temperature_limit_members("A", [unit]) is None
    assert await dry.set_temperature_limit_enabled("A", enabled=True) is None
    await dry.delete_temperature_limit("A")
    assert await dry.create_sliding_temperature("T") is None
    assert (
        await dry.set_sliding_temperature(
            "S", outdoor_point="Outdoor sensor", setpoint_range=(20, 23)
        )
        is None
    )
    assert await dry.set_sliding_temperature_members("S", [unit]) is None
    assert await dry.set_sliding_temperature_enabled("S", enabled=True) is None
    await dry.delete_sliding_temperature("S")
    assert await dry.set_heating_optimisation([unit], enabled=True) is None
    assert await dry.set_heating_optimisation(enabled=True) is None
    assert await dry.create_interlock("Q") is None
    assert await dry.rename_interlock("P", "Q") is None
    assert await dry.copy_interlock("P", "Q") is None
    assert await dry.set_interlock_enabled("P", enabled=True) is None
    await dry.delete_interlock("P")
    assert (
        await dry.set_interlock_setup(
            "P", inputs=[unit], outputs=[InterlockOutputSpec(outputs=(unit,))]
        )
        is None
    )
    assert writes(itm) == []
    assert len(dry.planned) == 18
    assert dry.planned[0] == (
        "create temperature limit group B: screen 400 part 61 event 1 'B'"
    )
    assert [g.name for g in itm.limits] == ["A"]
    assert itm.limit("A").lower == 16.0
    assert itm.limit("A").members == []
    assert itm.slide("S").point == ""
    assert not any(itm.hmo.values())
    assert [(p.name, p.enabled, p.inputs) for p in itm.interlocks] == [("P", False, [])]
    assert itm.open_screens == set()


async def test_read_only_refuses(itm: FakeDealerITM) -> None:
    """A read-only client refuses every write before a request is sent."""
    itm.limits.append(Group("A"))
    ro = FakeClient(itm, read_only=True)
    for call in (
        lambda: ro.create_temperature_limit("B"),
        lambda: ro.set_temperature_limit("A", lower=17),
        lambda: ro.delete_sliding_temperature("A"),
        lambda: ro.set_heating_optimisation(enabled=True),
        lambda: ro.create_interlock("P"),
        lambda: ro.set_interlock_setup("P", inputs=[]),
    ):
        with pytest.raises(ReadOnlyError):
            await call()
    assert itm.requests == []
    for flow, args in (
        (df.create_group, (df.TEMP_LIMIT, "B")),
        (df.set_heating_optimisation, ((),)),
        (il.delete_interlock, ("P",)),
    ):
        with pytest.raises(ReadOnlyError):
            await run(
                itm,
                flow,
                *args,
                client=ro,
                **({"enabled": True} if flow is df.set_heating_optimisation else {}),
            )
    assert writes(itm) == []


class _Disabled(FakeDealerITM):
    """An iTM that keeps some buttons disabled after a selection."""

    def _group_selection(self, screen: int) -> Any:
        return [*super()._group_selection(screen), *self._enable(screen, {9: False})]

    def _il_selection(self) -> Any:
        return [*super()._il_selection(), *self._enable(900, {10: False, 7: False})]


async def test_disabled_parts_are_never_clicked() -> None:
    """A button the iTM reports disabled raises before it is clicked."""
    itm = _Disabled()
    itm.limits.append(Group("A"))
    itm.interlocks.append(Interlock("P"))
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="has this disabled"):
        await c.delete_temperature_limit("A")
    with pytest.raises(ItmCommandError, match="has this disabled"):
        await c.fetch_interlock_setup("P")
    with pytest.raises(ItmCommandError, match="has this disabled"):
        await c.copy_interlock("P", "Q")
    assert itm.limits[0].name == "A"
    assert itm.open_screens == set()


class _Unchanged(FakeDealerITM):
    """An iTM that confirms everything but changes nothing."""

    def _event(self, ev: dict[str, Any]) -> Any:
        if ev["part"] in PERSISTING and ev["event"] == 1 and ev["screen"] >= 400:
            dialog = self.dl_dialog
            reply = super()._event({**ev, "event": 2})
            if dialog is not None and dialog[0] in {401, 402, 501, 502, 901}:
                reply = [*reply, *self._close(dialog[0])]
            return reply
        return super()._event(ev)


async def test_read_back_failures() -> None:
    """A change the iTM did not take raises after the fresh read."""
    itm = _Unchanged()
    itm.limits.append(Group("A"))
    itm.slides.append(Group("S"))
    itm.interlocks.append(Interlock("P"))
    c = FakeClient(itm)
    unit = itm.points[0]
    for call, match in (
        (lambda: c.create_temperature_limit("B"), "did not add"),
        (lambda: c.set_temperature_limit("A", lower=17), "did not take the lower"),
        (lambda: c.set_temperature_limit("A", new_name="C"), "is gone"),
        (lambda: c.set_temperature_limit_members("A", [unit]), "did not take"),
        (lambda: c.set_temperature_limit_enabled("A", enabled=True), "did not"),
        (lambda: c.delete_temperature_limit("A"), "did not delete"),
        (lambda: c.create_sliding_temperature("T"), "did not add"),
        (lambda: c.set_sliding_temperature("S", new_name="T"), "is gone"),
        (
            lambda: c.set_sliding_temperature(
                "S", outdoor_point="Return sensor", outdoor_range=(1, 2)
            ),
            "outdoor point, outdoor range",
        ),
        (lambda: c.set_sliding_temperature_members("S", [unit]), "did not take"),
        (lambda: c.set_sliding_temperature_enabled("S", enabled=True), "did not"),
        (lambda: c.delete_sliding_temperature("S"), "did not delete"),
        (lambda: c.set_heating_optimisation([unit], enabled=True), "did not take"),
        (lambda: c.create_interlock("Q"), "did not take"),
        (lambda: c.rename_interlock("P", "Q"), "did not take"),
        (lambda: c.set_interlock_enabled("P", enabled=True), "did not enable"),
        (lambda: c.delete_interlock("P"), "did not delete"),
        (
            lambda: c.set_interlock_setup(
                "P",
                inputs=[unit],
                outputs=[
                    InterlockOutputSpec(condition="Off", outputs=(unit,), interval=5)
                ],
            ),
            "inputs, outputs 1, condition 1, interval 1",
        ),
    ):
        with pytest.raises(ItmCommandError, match=match):
            await call()
    assert itm.open_screens == set()


class _Broken(FakeDealerITM):
    """An iTM that ignores some buttons, refuses others or shows no radio state."""

    def __init__(self) -> None:
        """Start with nothing broken."""
        super().__init__()
        self.ignored: set[tuple[int, int]] = set()
        self.refused: set[tuple[int, int]] = set()
        self.no_check_state = False
        self.cancel_asks: set[int] = set()

    def _event(self, ev: dict[str, Any]) -> Any:
        key = (ev["screen"], ev["part"])
        if key in self.ignored and ev["screen"] in self.open_screens:
            return [("UPDATE", ev["screen"], [], [])]
        if key in self.refused and ev["screen"] in self.open_screens:
            return [("DLG_OPEN", key[0], [70, 1, "Niet mogelijk", "Error", "OK"], [])]
        if (
            ev["part"] == 12
            and ev["screen"] in self.cancel_asks
            and self.dl_dialog is None
        ):
            return self._ask(_confirm(ev["screen"]), lambda _a: self._close(901))
        reply = super()._event(ev)
        if self.no_check_state:
            reply = [
                (u[0], u[1], u[2], [p for p in u[3] if p[1] != "CHECK_STATE"])
                for u in reply
            ]
        return reply


async def test_screens_that_do_not_react() -> None:
    """A screen or sub-screen that doesn't open, a refusal: errors, all left."""
    itm = _Broken()
    itm.limits.append(Group("A"))
    itm.slides.append(Group("S"))
    itm.interlocks.append(Interlock("P"))
    c = FakeClient(itm)
    unit = itm.points[0]
    itm.optional_available[600] = False
    with pytest.raises(ItmCommandError, match="screen did not open"):
        await c.fetch_heating_optimisation()
    itm.ignored = {(400, 10), (400, 8), (502, 6), (901, 2), (500, 11)}
    with pytest.raises(ItmCommandError, match="screen 401 did not open"):
        await c.set_temperature_limit_members("A", [unit])
    with pytest.raises(ItmCommandError, match="no name input opened"):
        await c.create_temperature_limit("B")
    with pytest.raises(ItmCommandError, match="screen 502 did not open"):
        await c.fetch_sliding_temperature_inputs("S")
    with pytest.raises(ItmCommandError, match="interlock input screen did not open"):
        await c.set_interlock_setup("P", inputs=[unit])
    itm.ignored = {(900, 10)}
    with pytest.raises(ItmCommandError, match="setup screen did not open"):
        await c.fetch_interlock_setup("P")
    itm.ignored = {(902, 13)}
    with pytest.raises(ItmCommandError, match="the screen did not close"):
        await c.set_interlock_setup("P", inputs=[unit])
    itm.ignored = {(902, 6)}
    with pytest.raises(ItmCommandError, match="the list would be"):
        await c.set_interlock_setup("P", inputs=[unit])
    itm.interlock("P").inputs = [unit]
    itm.ignored = {(902, 7)}
    with pytest.raises(ItmCommandError, match="did not remove"):
        await c.set_interlock_setup("P", inputs=[])
    itm.ignored = {(401, 4)}
    with pytest.raises(ItmCommandError, match="the members would be"):
        await c.set_temperature_limit_members("A", [unit])
    itm.ignored = set()
    itm.refused = {(900, 10)}
    with pytest.raises(ItmCommandError, match="refused to open the setup"):
        await c.fetch_interlock_setup("P")
    assert writes(itm) == []
    assert itm.open_screens == set()


async def test_without_radio_state() -> None:
    """Without the radios' state the list's activation text decides."""
    itm = _Broken()
    itm.no_check_state = True
    itm.limits.append(Group("A", enabled=True))
    itm.interlocks.append(Interlock("P", enabled=True))
    itm.hmo[itm.points[0]] = True
    itm.cancel_asks = {901}
    c = FakeClient(itm)
    assert [p.enabled for p in await c.fetch_interlocks()] == [True]
    assert await run(itm, il.set_interlock_enabled, "P", enabled=True) is True
    assert await run(itm, df.set_group_enabled, df.TEMP_LIMIT, "A", enabled=True)
    assert await run(itm, df.set_heating_optimisation, (itm.points[0],), enabled=True)
    setup = await c.fetch_interlock_setup("P")  # its Cancel asks: answered Yes
    assert setup.inputs == ()
    assert itm.interlock("P").enabled  # the confirms opened and discarded only
    assert itm.limit("A").enabled
    assert itm.open_screens == set()


# -- the CLI --------------------------------------------------------------------------


def cli(*args: str) -> None:
    """Run the CLI against the simulator of CliClient."""
    itm_cli.main(["--host", "fake-host", "--experimental", *args])


@pytest.fixture
def cli_itm(
    itm: FakeDealerITM, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> FakeDealerITM:
    """Route the CLI to the simulator."""
    monkeypatch.setattr(CliClient, "itm", itm, raising=False)
    monkeypatch.setattr(CliClient, "sigterm_at", None, raising=False)
    monkeypatch.setattr(itm_cli, "ItmClient", CliClient)
    monkeypatch.setattr(itm_cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("ITM_USERNAME", "user")
    monkeypatch.setenv("ITM_PASSWORD", "secret")
    return itm


def test_cli_show_empty(
    cli_itm: FakeDealerITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """Each sub-command shows by default."""
    assert cli_itm.limits == []
    cli("templimit")
    cli("slidetemp")
    cli("interlock")
    assert capsys.readouterr().out.splitlines() == [
        "no temperature limit groups",
        "no sliding temperature groups",
        "no interlock programs",
    ]


def test_cli_templimit(
    cli_itm: FakeDealerITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """Add, members, set, enable, show, disable, delete (also dry)."""
    a, b = cli_itm.points[:2]
    cli("templimit", "add", "Annex", "--add", a, "--lower", "18", "--upper", "26")
    assert "Annex" in capsys.readouterr().out
    cli("templimit", "members", "Annex", "--add", b, "--remove", a)
    cli("templimit", "set", "Annex", "--upper", "25")
    cli("templimit", "enable", "Annex")
    cli("templimit", "show", "--details")
    last = capsys.readouterr().out.splitlines()[-1].split()
    assert last == ["Annex", "enabled", "18-25", "°C", "members", b]
    cli("templimit", "disable", "Annex")
    cli("--dry-run", "templimit", "delete", "Annex")
    assert "delete temperature limit group Annex" in capsys.readouterr().out
    cli("templimit", "delete", "Annex")
    assert capsys.readouterr().out.splitlines()[-1] == "Annex: deleted"
    with pytest.raises(SystemExit, match="no temperature limit group"):
        cli("templimit", "members", "Annex", "--add", a)
    with pytest.raises(SystemExit, match="the name is needed"):
        cli("templimit", "delete")


def test_cli_slidetemp(
    cli_itm: FakeDealerITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """Add, inputs, set, members, enable, disable, delete."""
    a = cli_itm.points[0]
    cli("slidetemp", "add", "Hall", "--add", a)
    cli("slidetemp", "inputs", "Hall")
    assert "AI Outdoor sensor" in capsys.readouterr().out
    cli(
        "slidetemp",
        "set",
        "Hall",
        "--outdoor-point",
        "Outdoor sensor",
        "--outdoor-range",
        "0",
        "30",
        "--setpoint-range",
        "21",
        "25",
    )
    assert "outdoor 0-30 °C -> setpoint 21-25 °C" in capsys.readouterr().out
    cli("slidetemp", "members", "Hall", "--remove", a)
    cli("slidetemp", "enable", "Hall")
    cli("slidetemp", "disable", "Hall")
    cli("slidetemp", "delete", "Hall")
    assert cli_itm.slides == []
    with pytest.raises(SystemExit, match="no sliding temperature group"):
        cli("slidetemp", "members", "Hall")


def test_cli_heatopt(
    cli_itm: FakeDealerITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """On for a unit, a dry run for all, show."""
    a, b = cli_itm.points[:2]
    cli("heatopt", "on", a)
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == [a, "on"]
    assert lines[1].split() == [b, "off"]
    cli("--dry-run", "heatopt", "off")
    assert "heating mode optimisation off for all units" in capsys.readouterr().out
    cli("heatopt")
    assert capsys.readouterr().out.splitlines()[0].split() == [a, "on"]
