# pylint: disable=protected-access,redefined-outer-name
"""The energy navigator's readers, read only, against the simulated iTM."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import (
    EnergyGroup,
    EnergyNavigator,
    EnergyType,
    ItmCommandError,
    SamplingTarget,
    ShownValue,
)
from daikin_itm import cli as itm_cli
from daikin_itm import energy_navigator as en
from daikin_itm.energy_navigator import (
    parse_shown,
    read_actual,
    read_budget,
    read_planned,
    read_rules,
    read_sampling_targets,
    wanted_parts,
)
from daikin_itm.state import UIState

from .fake_energy import (
    CLOSE,
    DISCARD_ID,
    LISTS,
    OPENS,
    PROGRESS_ID,
    TABS,
    FakeEnergyITM,
)
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

ENERGY_SCREENS = set(CLOSE)
OK_PARTS = {(3202, 14), (3203, 15), (3218, 3), (3210, 17), (3210, 35), (3206, 13)}
OK_PARTS |= {(3208, 11), (3214, 29)}
# Create, copy, delete, rename on the type, group and rule lists: never in a read.
OK_PARTS |= {(screen, p) for screen in LISTS for p in (4, 5, 6, 7)}


@pytest.fixture
def itm() -> FakeEnergyITM:
    """Return a simulated iTM with energy data."""
    return FakeEnergyITM()


async def run(itm: FakeEnergyITM, flow: Callable[..., Awaitable[Any]]) -> Any:
    """Run one flow in a fresh session, like the client's wrappers."""
    c = FakeClient(itm)
    c.progress_interval = 0
    async with c._session() as s:
        try:
            return await flow(s)
        finally:
            await s.close_menu()


def assert_read_only(itm: FakeEnergyITM) -> None:
    """Only opening buttons, Close/Cancel, tabs and progress requests were sent."""
    assert itm.violations == []
    allowed = {*OPENS, *CLOSE.items(), (5, 100), (5, 102), (3214, DISCARD_ID)}
    allowed |= set(LISTS.items())  # row selections
    for r in itm.requests:
        if r["screen"] in ENERGY_SCREENS:
            key = (r["screen"], r["part"])
            assert key not in OK_PARTS
            if r["event"] == 1:
                assert key in allowed, r
            else:
                assert (r["event"], r["part"]) in {
                    (8, TABS.get(r["screen"])),
                    (5, PROGRESS_ID),
                }, r
    assert not itm.open_screens & ENERGY_SCREENS


def test_parse_shown() -> None:
    """Plain numbers are parsed, after a colon and before a unit; "--" is None."""
    assert parse_shown("0.0 ") == ShownValue("0.0 ", 0.0)
    assert parse_shown("12,5 %").value == 12.5
    assert parse_shown("2026 : 1234.5(kWh)").value == 1234.5
    assert parse_shown("-2026 : --()").value is None
    assert parse_shown("-- ").value is None
    assert parse_shown("Actual consumption").value is None
    assert parse_shown("A&amp;B").text == "A&B"


def test_readers_on_unopened_screens() -> None:
    """Nothing shown gives empty results, not errors."""
    state = UIState()
    assert not en.budget_from_screen(state).tabs
    assert not en.planned_from_screen(state).years
    assert en.actual_from_screen(state).energy_type is None
    assert not en.rules_from_screen(state)
    assert not en.sampling_targets_from_screen(state)
    # A selection index outside the options is not an option.
    state.screens[3200] = {(2, "COMBO_STRS"): ["A"], (2, "SINGLE_SELIDX"): 3}
    assert en.budget_from_screen(state).display is None
    # A tab without its parts: nothing shown, nothing guessed.
    state.screens[3202] = {(3, "TAB_NAME"): ["01/2025-12/2025"]}
    year = en.planned_from_screen(state).years[0]
    assert (year.ratio, year.ratio_unit, year.per_month) == (None, None, None)


async def test_budget_with_data(itm: FakeEnergyITM) -> None:
    """Every tab: its labels as shown (numbers parsed), type, unit and graph."""
    budget = await run(itm, read_budget)
    assert budget.display == "Building"
    assert [t.name for t in budget.tabs] == [
        "Yearly budget",
        "Monthly budget",
        "Compare",
    ]
    yearly, monthly, compare = budget.tabs
    assert [v.text for v in yearly.labels] == ["2026", "Forecast : 900.0(kWh)"]
    assert yearly.labels[1].value == 900.0
    assert [v.text for v in monthly.labels] == ["01/2025"]
    assert compare.labels[1] == ShownValue("2026 : 1234.5(kWh)", 1234.5)
    assert compare.labels[2].value is None
    assert (yearly.energy_type, yearly.unit) == ("Power", "Energy")
    assert (yearly.type_label, yearly.unit_label) == ("Energy type", "Energy unit")
    assert yearly.graph[:3] == (1, 2, "0")
    assert 100.5 in yearly.graph
    tabs = [r["number"] for r in itm.requests if r["event"] == 8]
    assert tabs == [1, 2, 0]
    assert_read_only(itm)


def test_combo_without_selection_shows_its_first_option() -> None:
    """A real iTM sent the display combo's options without a selection index."""
    state = UIState()
    state.screens[3200] = {(2, "COMBO_STRS"): ["Building", "Indoor units"]}
    assert en.budget_from_screen(state).display == "Building"
    state.screens[3200][2, "SINGLE_SELIDX"] = -1
    assert en.budget_from_screen(state).display is None


async def test_budget_without_data() -> None:
    """Without the licence the totals show "--" and the graphs have no bars."""
    itm = FakeEnergyITM(data=False)
    budget = await run(itm, read_budget)
    compare = budget.tabs[2]
    assert compare.labels[1] == ShownValue("-2026 : --()", None)
    assert 100.5 not in compare.graph
    assert_read_only(itm)


async def test_planned_with_data(itm: FakeEnergyITM) -> None:
    """3202 behind PROGRESS: per fiscal year ratio, expected value, months."""
    planned = await run(itm, read_planned)
    assert planned.energy_type == "Power"
    this, last = planned.years
    assert this.name == "01/2025-12/2025"
    assert this.ratio == ShownValue("5", 5.0)
    assert this.ratio_unit == "%"
    assert this.expected == ShownValue("12000.0 ", 12000.0)
    assert this.per_month is True
    assert [(m.month, m.value.value) for m in this.months] == [
        ("01/2026", 1000.0),
        ("02/2026", None),
    ]
    assert last.expected is not None
    assert last.expected.value is None
    assert last.per_month is False
    assert last.months == ()
    # The progress dialog was waited out like the browser does.
    assert any(r["event"] == 5 and r["screen"] == 3202 for r in itm.requests)
    assert_read_only(itm)


async def test_planned_and_actual_without_data() -> None:
    """Empty month lists, "0" and "--" as shown."""
    itm = FakeEnergyITM(data=False)
    planned = await run(itm, read_planned)
    assert planned.years[0].months == ()
    assert planned.years[0].ratio == ShownValue("0", 0.0)
    actual = await run(itm, read_actual)
    assert [y.months for y in actual.years] == [(), ()]
    assert_read_only(itm)


async def test_actual_with_data(itm: FakeEnergyITM) -> None:
    """3203 behind PROGRESS: per fiscal year the months and the CO2 factor."""
    actual = await run(itm, read_actual)
    assert actual.energy_type == "Power"
    this, last = actual.years
    assert this.co2_factor == ShownValue("0.410", 0.41)
    assert this.co2_unit == "kg-CO2/kWh"
    assert this.months[0].month == "01/2026"
    assert last.co2_factor is not None
    assert last.co2_factor.value is None
    assert_read_only(itm)


async def test_rules_and_sampling_targets(itm: FakeEnergyITM) -> None:
    """3210 -> 3213 (names) and 3218 (levels, names), each left with Close/Cancel."""
    assert await run(itm, read_rules) == ("Rule A", "Rule B")
    assert await run(itm, read_sampling_targets) == (
        SamplingTarget(level=1, name="All", selected=True),
        SamplingTarget(level=2, name="Indoor units", selected=False),
    )
    assert_read_only(itm)
    empty = FakeEnergyITM(data=False)
    assert await run(empty, read_rules) == ()
    assert_read_only(empty)


async def test_unavailable_screens_read_none(itm: FakeEnergyITM) -> None:
    """A menu screen that doesn't open gives None, without errors."""
    itm.unavailable = {3200, 3210}
    for flow in (read_budget, read_planned, read_actual, read_rules):
        assert await run(itm, flow) is None
    assert await run(itm, read_sampling_targets) is None
    assert_read_only(itm)


async def test_disabled_button_is_never_clicked(itm: FakeEnergyITM) -> None:
    """A button shown ENABLED 0 is refused before it is sent; screens are closed."""
    itm.disabled = {(3200, 18)}
    with pytest.raises(ItmCommandError, match="has this disabled"):
        await run(itm, read_planned)
    assert_read_only(itm)


async def test_single_tab_is_not_switched(itm: FakeEnergyITM) -> None:
    """With one tab, no tab is selected."""
    original = itm._planned_parts

    def one_year() -> list[tuple[int, str, Any]]:
        parts = original()
        return [
            (3, "TAB_NAME", ["01/2025-12/2025"]) if p[1] == "TAB_NAME" else p
            for p in parts
        ]

    itm._planned_parts = one_year  # type: ignore[method-assign]
    planned = await run(itm, read_planned)
    assert [y.name for y in planned.years] == ["01/2025-12/2025"]
    assert not [r for r in itm.requests if r["event"] == 8 and r["screen"] == 3202]
    assert_read_only(itm)


async def test_screen_that_does_not_open(itm: FakeEnergyITM) -> None:
    """A button that opens nothing raises; what did open is closed again."""
    itm.no_screen = {3203}
    with pytest.raises(ItmCommandError, match="3203 did not open"):
        await run(itm, read_actual)
    assert_read_only(itm)


async def test_fake_fails_on_a_write(itm: FakeEnergyITM) -> None:
    """The simulator itself refuses an OK, so a flow that sent one would fail."""

    async def press_ok(s: Any) -> None:
        await read_budget(s)
        await s.open_menu()
        await s.send(5, 100)
        await s.send(3200, 18)
        await s.send(3201, 2)
        await s.send(3202, 14)

    with pytest.raises(AssertionError, match="a write"):
        await run(itm, press_ok)
    assert itm.violations


async def test_disabled_click_is_refused(itm: FakeEnergyITM) -> None:
    """A click on a part shown as ENABLED 0 never reaches the iTM."""
    itm.disabled = {(3210, 16)}

    async def details(s: Any) -> None:
        await s.open_menu()
        await s.send(5, 102)
        await s.send(3210, 16)

    with pytest.raises(ItmCommandError, match=r"screen 3210 part 16: .* disabled"):
        await run(itm, details)


async def test_disabled_write_is_refused_in_a_dry_run(itm: FakeEnergyITM) -> None:
    """A dry run doesn't plan a click on a disabled part either."""
    itm.disabled = {(3210, 16)}
    dry = FakeClient(itm, dry_run=True)

    async def details(s: Any) -> None:
        await s.open_menu()
        await s.send(5, 102)
        await s.write("details", 3210, 16)

    async with dry._session() as s:
        try:
            with pytest.raises(ItmCommandError, match="details: the iTM has this"):
                await details(s)
        finally:
            await s.close_menu()
    assert not dry.planned


async def test_fake_fails_on_a_combo_pick(itm: FakeEnergyITM) -> None:
    """A combo pick (another energy type) is a display choice the reads don't make."""

    async def pick(s: Any) -> None:
        await s.open_menu()
        await s.send(5, 100)
        await s.send(3200, 15, 1, 1, 1)

    with pytest.raises(AssertionError, match="display choice"):
        await run(itm, pick)


async def test_fake_fails_on_a_list_sort(itm: FakeEnergyITM) -> None:
    """Any other event (a list column sort) is not a read either."""

    async def sort(s: Any) -> None:
        await s.open_menu()
        await s.send(5, 102)
        await s.send(3210, 19, 7, 1, 0)

    with pytest.raises(AssertionError, match="not a read"):
        await run(itm, sort)


async def test_fetch_energy_navigator(itm: FakeEnergyITM) -> None:
    """Everything in one session, or only the named parts."""
    c = FakeClient(itm)
    c.progress_interval = 0
    nav = await c.fetch_energy_navigator()
    assert isinstance(nav, EnergyNavigator)
    assert nav.budget is not None
    assert nav.planned is not None
    assert nav.actual is not None
    assert nav.rules == ("Rule A", "Rule B")
    assert nav.sampling_targets is not None
    assert sum(1 for r in itm.requests if r["event"] == 10) == 1  # one logout
    assert_read_only(itm)
    only = await c.fetch_energy_navigator(["rules"])
    assert only == EnergyNavigator(rules=("Rule A", "Rule B"))
    only = await c.fetch_energy_navigator(["actual"])
    assert only.actual is not None
    assert only.rules is None
    assert nav.energy_types is None  # not a default part
    more = await c.fetch_energy_navigator(
        ["energy_types", "energy_groups", "details", "rule_editor"]
    )
    assert more.energy_types is not None
    assert more.energy_groups is not None
    assert more.details is not None
    assert more.rule_editor is not None
    assert more.budget is None
    assert_read_only(itm)
    with pytest.raises(ItmCommandError, match="unknown"):
        await c.fetch_energy_navigator(["bogus"])


async def test_energy_types(itm: FakeEnergyITM) -> None:
    """3200 -> 3204 -> 3205: each type through Edit (3206), cancelled."""
    types = await run(itm, en.read_energy_types)
    assert [t.name for t in types] == ["Power", "Gas"]
    # The built-in type's Edit stays disabled after selecting it: not clicked.
    assert types[0] == EnergyType("Power")
    gas = types[1]
    assert (gas.unit, gas.energy_type) == ("m3", "Gas")
    assert gas.co2_factor == ShownValue("1.950", 1.95)
    assert gas.co2_unit == "kg-CO2/m3"
    assert gas.conversion is not None
    assert gas.conversion.value is None
    assert gas.conversion_unit == "kWh/m3"
    assert_read_only(itm)


async def test_energy_groups(itm: FakeEnergyITM) -> None:
    """3200 -> 3204 -> 3207: each group's members and available points (3208)."""
    groups = await run(itm, en.read_energy_groups)
    assert groups == (
        EnergyGroup("Group A", "Power", (("1-00", "Unit 1"),), (("1-01", "Unit 2"),)),
        EnergyGroup("Group B", "Power", (), (("1-01", "Unit 2"),)),
    )
    assert_read_only(itm)


async def test_display_lists_unavailable(itm: FakeEnergyITM) -> None:
    """3200 not opening reads None; 3204 not opening raises, all closed again."""
    itm.unavailable = {3200}
    assert await run(itm, en.read_energy_types) is None
    itm.unavailable = set()
    itm.no_screen = {3204}
    with pytest.raises(ItmCommandError, match="3204 did not open"):
        await run(itm, en.read_energy_groups)
    assert_read_only(itm)


async def test_details_only_when_enabled(itm: FakeEnergyITM) -> None:
    """3211 opens from Details with data; without, Details is never clicked."""
    details = await run(itm, en.read_details)
    assert details == en.EquipmentDetails(
        day="03/01/2025", graph=details.graph, occurrences=(("Unit 1", "3"),)
    )
    assert 100.5 in details.graph
    assert [r["number"] for r in itm.requests if r["event"] == 8] == [1, 0]
    assert_read_only(itm)
    empty = FakeEnergyITM(data=False)
    assert await run(empty, en.read_details) is None
    assert not [r for r in empty.requests if (r["screen"], r["part"]) == (3210, 16)]
    assert_read_only(empty)
    itm.unavailable = {3210}
    assert await run(itm, en.read_details) is None


async def test_rule_editor(itm: FakeEnergyITM) -> None:
    """Each rule: select its row, Edit (3214), read every tab, Cancel and Yes."""
    rules = await run(itm, en.read_rule_editor)
    assert [r.name for r in rules] == ["Rule A", "Rule B"]
    rule = rules[0]
    assert rule.weekdays[0] == ()
    assert rule.weekdays[1] == ((32, 0, -1, 2), (72, 1, -1, 2))
    # A hidden special day is left out.
    assert [name for name, _ in rule.special_days] == [
        f"Holiday {i}" for i in range(1, 5)
    ]
    assert rule.calendar == (("01/01", "Holiday 1"),)
    selects = [
        r["array"] for r in itm.requests if (r["screen"], r["part"]) == (3213, 2)
    ]
    assert selects == [[0], [1]]
    assert_read_only(itm)
    empty = FakeEnergyITM(data=False)
    assert await run(empty, en.read_rule_editor) == ()
    empty.unavailable = {3210}
    assert await run(empty, en.read_rule_editor) is None


async def test_rule_editor_skips_a_disabled_edit(itm: FakeEnergyITM) -> None:
    """A rule whose Edit stays disabled after selecting it is not opened."""
    original = itm._select_row

    def keep_disabled(screen: int, row: int) -> list[Any]:
        updates = original(screen, row)
        if screen == 3213 and row == 1:
            itm.locked.add((3213, 8))
            return [("UPDATE", 3213, [], [(8, "ENABLED", 0)])]
        return updates

    itm._select_row = keep_disabled  # type: ignore[method-assign]
    rules = await run(itm, en.read_rule_editor)
    assert [r.name for r in rules] == ["Rule A"]
    assert_read_only(itm)


def test_new_readers_on_unopened_screens() -> None:
    """Nothing shown: empty values, no errors."""
    state = UIState()
    assert en.details_from_screen(state) == en.EquipmentDetails(None, (), ())
    rule = en.rule_editor_from_screen(state, "R")
    assert rule.weekdays == ((),) * 7
    assert [name for name, _ in rule.special_days] == [f"Ex{i}" for i in range(1, 6)]
    assert not en.list_names(state, 3205)
    state.screens[3211] = {(7, "POINT_GRAPH"): [1, 2]}
    assert en.details_from_screen(state).graph == (1, 2)


def test_wanted_parts() -> None:
    """None means the default parts; the others only when named."""
    assert "budget" in wanted_parts(None)
    assert "energy_types" not in wanted_parts(None)
    assert wanted_parts(["rule_editor"]) == frozenset({"rule_editor"})
    assert wanted_parts(["actual"]) == frozenset({"actual"})
    with pytest.raises(ItmCommandError, match="not the string 'budget'"):
        wanted_parts("budget")
    with pytest.raises(ItmCommandError, match="unknown energy navigator parts"):
        wanted_parts(["budgte"])


def test_cli_energy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`itm energy` prints what was read; --only narrows it."""
    fake = FakeEnergyITM()

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
    itm_cli.main(["--host", "fake-host", "--read-only", "energy"])
    out = capsys.readouterr().out
    assert "budget (display Building)" in out
    # The unit combo's option is printed after its own label, not as "[unit]".
    assert "Energy type Power, Energy unit Energy;" in out
    assert "[Energy]" not in out
    assert "Yearly budget" in out
    assert "2026 : 1234.5(kWh)" in out
    assert "01/2025-12/2025" in out
    assert "Rule A" in out
    assert "Indoor units" in out
    assert_read_only(fake)
    itm_cli.main(["--host", "fake-host", "energy", "--only", "rules"])
    out = capsys.readouterr().out
    assert "Rule B" in out
    assert "Yearly budget" not in out
    only = "energy_types,energy_groups,details,rule_editor"
    itm_cli.main(["--host", "fake-host", "--read-only", "energy", "--only", only])
    out = capsys.readouterr().out
    assert "energy type Gas: unit m3, type Gas, CO2 1.950 kg-CO2/m3" in out
    assert "energy group Group A (Power): 1-00 Unit 1" in out
    assert "energy group Group B (Power): (none)" in out
    assert "equipment details (03/01/2025):" in out
    assert "rule Rule A: 5 weekday patterns" in out
    assert "01/01 | Holiday 1" in out
    assert_read_only(fake)
