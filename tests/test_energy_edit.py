# pylint: disable=protected-access,redefined-outer-name
"""The energy navigator's settings writes, against the simulated iTM."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import ItmCommandError, ReadOnlyError
from daikin_itm import cli as itm_cli
from daikin_itm import energy_edit as ee

from .fake_energy_edit import FakeEnergyEditITM
from .fake_energy_edit import _number as number_text
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path


@pytest.fixture
def itm() -> FakeEnergyEditITM:
    """Return a simulated iTM with energy data whose settings can change."""
    return FakeEnergyEditITM()


def client(itm: FakeEnergyEditITM, **kw: Any) -> FakeClient:
    """Return a client for the simulator, without progress delays."""
    c = FakeClient(itm, **kw)
    c.progress_interval = 0
    return c


async def run(itm: FakeEnergyEditITM, flow: Callable[..., Awaitable[Any]]) -> Any:
    """Run one flow in a fresh session."""
    async with client(itm)._session() as s:
        try:
            return await flow(s)
        finally:
            await s.close_menu()


def assert_closed(itm: FakeEnergyEditITM) -> None:
    """Every energy screen was left again, no dialog open, nothing invalid sent."""
    assert itm.violations == []
    assert itm.edit_dialog is None
    assert not {s for s in itm.open_screens if 3200 <= s < 3300}


async def test_planned_consumption(itm: FakeEnergyEditITM) -> None:
    """Ratio, per month and month targets, saved by OK and read back."""
    c = client(itm)
    year = await c.set_planned_consumption(
        ratio=7.5, per_month=True, months={"02/2026": 1250.5}
    )
    assert year is not None
    assert year.ratio is not None
    assert year.ratio.value == 7.5
    assert [(m.month, m.value.value) for m in year.months] == [
        ("01/2026", 1000.0),
        ("02/2026", 1250.5),
    ]
    assert itm.saved == ["planned"]
    assert itm.plans[0, 0]["per_month"] is True
    assert_closed(itm)


async def test_planned_other_type_and_year(itm: FakeEnergyEditITM) -> None:
    """Another energy type (picked in its combo) and the second fiscal year."""
    c = client(itm)
    year = await c.set_planned_consumption(
        year="01/2024-12/2024", energy_type="Gas", ratio=3, per_month=False
    )
    assert year is not None
    assert year.name == "01/2024-12/2024"
    assert itm.plans[1, 1]["ratio"] == "3"
    assert itm.plans[0, 0]["ratio"] == "5"
    assert_closed(itm)


async def test_actual_consumption(itm: FakeEnergyEditITM) -> None:
    """Month values and the CO2 factor, saved by OK and read back."""
    c = client(itm)
    year = await c.set_actual_consumption(co2_factor=0.39, months={"01/2026": 990})
    assert year is not None
    assert year.co2_factor is not None
    assert year.co2_factor.value == 0.39
    assert year.months[0].value.value == 990
    assert itm.saved == ["actual"]
    assert_closed(itm)


async def test_dry_run_plans_ok_and_cancels(itm: FakeEnergyEditITM) -> None:
    """A dry run enters the values, plans the OK, cancels: nothing saved."""
    c = client(itm, dry_run=True)
    assert await c.set_planned_consumption(ratio=9) is None
    assert await c.set_actual_consumption(co2_factor=1) is None
    assert itm.saved == []
    assert itm.plans[0, 0]["ratio"] == "5"
    assert [p.split(":")[0] for p in c.planned] == [
        "save planned consumption",
        "save actual consumption",
    ]
    assert_closed(itm)


async def test_read_only_sends_nothing(itm: FakeEnergyEditITM) -> None:
    """read_only refuses before a request is sent, and in the session flows too."""
    c = client(itm, read_only=True)
    calls: list[Callable[[], Awaitable[Any]]] = [
        lambda: c.set_planned_consumption(ratio=1),
        lambda: c.set_actual_consumption(co2_factor=1),
        lambda: c.create_energy_type("New"),
        lambda: c.rename_energy_type("Gas", "Oil"),
        lambda: c.delete_energy_type("Gas"),
    ]
    for call in calls:
        with pytest.raises(ReadOnlyError):
            await call()
    assert itm.requests == []
    async with c._session() as s:
        with pytest.raises(ReadOnlyError):
            await ee.set_planned(s, ee.PlannedChange(ratio=1))
    assert itm.saved == []


async def test_plan_refusals(itm: FakeEnergyEditITM) -> None:
    """Unknown type, year or month, and a disabled input: refused, screens closed."""
    c = client(itm)
    with pytest.raises(ItmCommandError, match="energy type 'Steam' not offered"):
        await c.set_planned_consumption(energy_type="Steam", ratio=1)
    with pytest.raises(ItmCommandError, match="fiscal year '2020' not shown"):
        await c.set_actual_consumption(year="2020", co2_factor=1)
    with pytest.raises(ItmCommandError, match="month '03/2026' not listed"):
        await c.set_actual_consumption(months={"03/2026": 1})
    itm.disabled = {(3202, 8)}
    with pytest.raises(ItmCommandError, match="ratio: the iTM has this disabled"):
        await c.set_planned_consumption(ratio=1)
    itm.disabled = {(3203, 15)}
    with pytest.raises(ItmCommandError, match="has this disabled"):
        await c.set_actual_consumption(co2_factor=1)
    itm.disabled = set()
    itm.unavailable = {3200}
    with pytest.raises(ItmCommandError, match="did not open"):
        await c.set_planned_consumption(ratio=1)
    assert itm.saved == []
    assert_closed(itm)


async def test_plan_read_back_mismatch(itm: FakeEnergyEditITM) -> None:
    """A value the iTM doesn't show afterwards fails the write's confirmation."""
    original = itm._save_plan

    def lose(screen: int) -> Any:
        updates = original(screen)
        itm.plans[0, 0]["ratio"] = "0"
        itm.actuals[0, 0]["co2"] = "--"
        itm.actuals[0, 0]["months"][0][1] = "--"
        return updates

    itm._save_plan = lose  # type: ignore[method-assign]
    c = client(itm)
    with pytest.raises(ItmCommandError, match="ratio shows"):
        await c.set_planned_consumption(ratio=4)
    with pytest.raises(ItmCommandError, match="CO2 factor shows"):
        await c.set_actual_consumption(co2_factor=0.5)
    with pytest.raises(ItmCommandError, match="01/2026: the iTM shows"):
        await c.set_actual_consumption(months={"01/2026": 5})

    def flip(screen: int) -> Any:
        updates = original(screen)
        itm.plans[0, 0]["per_month"] = not itm.plans[0, 0]["per_month"]
        return updates

    itm._save_plan = flip  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="per month is"):
        await c.set_planned_consumption(per_month=False)
    assert_closed(itm)


async def test_number_out_of_range(itm: FakeEnergyEditITM) -> None:
    """A number outside the dialog's range is refused and the dialog cancelled."""
    with pytest.raises(ItmCommandError, match="outside the iTM's range"):
        await client(itm).set_planned_consumption(ratio=-1)
    assert itm.saved == []
    assert_closed(itm)


async def test_energy_types_and_groups(itm: FakeEnergyEditITM) -> None:
    """Create, rename and delete, each confirmed by the list read back."""
    c = client(itm)
    await c.create_energy_type("Oil")
    await c.rename_energy_type("Oil", "Heating oil")
    await c.delete_energy_type("Heating oil")
    assert [t[0] for t in itm.energy_types] == ["Power", "Gas"]
    await c.create_energy_type("Group C", group=True)
    await c.rename_energy_type("Group A", "Group Z", group=True)
    await c.delete_energy_type("Group B", group=True)
    assert [g[0] for g in itm.energy_groups] == ["Group Z", "Group C"]
    assert itm.saved == [
        "create 3205",
        "rename 3205",
        "delete 3205",
        "create 3207",
        "rename 3207",
        "delete 3207",
    ]
    assert_closed(itm)


async def test_create_saves_offered_settings(itm: FakeEnergyEditITM) -> None:
    """Should the iTM open the new type's settings after the name, OK saves them."""
    itm.offer_settings = True
    await client(itm).create_energy_type("Oil")
    assert itm.saved == ["create 3205", "settings 3206"]
    assert_closed(itm)


async def test_type_dry_run_and_refusals(itm: FakeEnergyEditITM) -> None:
    """Dry runs cancel the name input / plan the delete; refusals send nothing."""
    dry = client(itm, dry_run=True)
    await dry.create_energy_type("Oil")
    await dry.rename_energy_type("Gas", "Oil")
    await dry.delete_energy_type("Gas")
    assert [p.split(":")[0] for p in dry.planned] == [
        "create energy type Oil",
        "rename energy type Gas to Oil",
        "delete energy type Gas",
    ]
    c = client(itm)
    with pytest.raises(ItmCommandError, match="exists already"):
        await c.create_energy_type("Gas")
    with pytest.raises(ItmCommandError, match="exists already"):
        await c.rename_energy_type("Gas", "Power")
    with pytest.raises(ItmCommandError, match="'Coal' not listed once"):
        await c.delete_energy_type("Coal")
    # The built-in type's buttons stay disabled after selecting it.
    with pytest.raises(ItmCommandError, match="has this disabled"):
        await c.delete_energy_type("Power")
    with pytest.raises(ItmCommandError, match="has this disabled"):
        await c.rename_energy_type("Power", "Electricity")
    with pytest.raises(ItmCommandError, match="1-16 characters"):
        await c.create_energy_type("x" * 17)
    itm.unavailable = {3200}
    with pytest.raises(ItmCommandError, match="did not open"):
        await c.create_energy_type("Oil")
    assert itm.saved == []
    assert [t[0] for t in itm.energy_types] == ["Power", "Gas"]
    assert_closed(itm)


async def test_type_writes_not_taking_effect(itm: FakeEnergyEditITM) -> None:
    """A create, rename or delete the list doesn't show fails the confirmation."""
    itm._named = lambda *_: []  # type: ignore[method-assign]
    c = client(itm)
    with pytest.raises(ItmCommandError, match="did not create"):
        await c.create_energy_type("Oil")
    with pytest.raises(ItmCommandError, match="did not rename"):
        await c.rename_energy_type("Gas", "Oil")
    itm.edit_dialog = None
    original = itm._confirm_dialog
    itm._confirm_dialog = lambda screen, *_: original(  # type: ignore[method-assign]
        screen, list
    )
    with pytest.raises(ItmCommandError, match="did not delete"):
        await c.delete_energy_type("Gas")
    assert_closed(itm)


async def test_create_without_a_name_input(itm: FakeEnergyEditITM) -> None:
    """A Create that opens no name input is refused."""
    itm._name_dialog = lambda *_: []  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="no name input opened"):
        await client(itm).create_energy_type("Oil")
    assert_closed(itm)


def test_number_text() -> None:
    """The simulator shows answers like the iTM: no trailing zeros."""
    assert number_text([7, 500]) == "7.5"
    assert number_text([0, 0]) == "0"


def test_cli_energy_edit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`itm energy-edit`: each action, a dry run, and the argument checks."""
    fake = FakeEnergyEditITM()

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

    def main(*args: str) -> str:
        itm_cli.main(["--host", "fake-host", "--experimental", *args])
        return capsys.readouterr().out

    out = main("--dry-run", "energy-edit", "planned", "--ratio", "6")
    assert "dry run, would send:" in out
    assert "save planned consumption" in out
    out = main(
        "energy-edit", "planned", "--ratio", "6", "--per-month", "yes",
        "--month", "01/2026=900",
    )  # fmt: skip
    assert "planned: done, the iTM shows" in out
    assert fake.plans[0, 0]["ratio"] == "6"
    main("energy-edit", "actual", "--co2", "0.5", "--type", "Power")
    assert fake.actuals[0, 0]["co2"] == "0.5"
    main("energy-edit", "create", "Oil")
    main("energy-edit", "rename", "Oil", "Fuel")
    out = main("energy-edit", "delete", "Fuel")
    assert out == "delete: done\n"
    main("energy-edit", "create", "Group C", "--group")
    assert fake.energy_groups[-1] == ["Group C"]
    for args, error in (
        (["rename", "Gas"], "needs a name and a new name"),
        (["delete"], "needs a name"),
        (["actual", "--month", "January"], "--month is MONTH=VALUE"),
    ):
        with pytest.raises(SystemExit, match=error):
            main("energy-edit", *args)
    assert not fake.violations
