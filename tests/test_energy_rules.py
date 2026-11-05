# pylint: disable=protected-access,redefined-outer-name
"""The energy navigator's management rules: rule settings, copies, the rule list."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import ItmCommandError, ReadOnlyError, RuleDay, RuleRange
from daikin_itm import cli as itm_cli
from daikin_itm import energy_edit as ee
from daikin_itm import energy_navigator as en
from daikin_itm.state import UIState

from .fake_energy_more import FakeEnergyMoreITM
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

# Requests that save: never in a read, only once per write.
SAVES = {(3214, 29), (3215, 5), (3216, 14), (3213, 4), (3213, 6), (3213, 7)}


@pytest.fixture
def itm() -> FakeEnergyMoreITM:
    """Return a simulated iTM with two management rules."""
    return FakeEnergyMoreITM()


def client(itm: FakeEnergyMoreITM, **kw: Any) -> FakeClient:
    """Return a client for the simulator, without progress delays."""
    c = FakeClient(itm, **kw)
    c.progress_interval = 0
    return c


async def run(itm: FakeEnergyMoreITM, flow: Callable[..., Awaitable[Any]]) -> Any:
    """Run one flow in a fresh session."""
    async with client(itm)._session() as s:
        try:
            return await flow(s)
        finally:
            await s.close_menu()


def assert_closed(itm: FakeEnergyMoreITM) -> None:
    """Every energy screen was left again, no dialog open, nothing invalid sent."""
    assert itm.violations == []
    assert itm.edit_dialog is None
    assert not {s for s in itm.open_screens if 3200 <= s < 3300}


def saves(itm: FakeEnergyMoreITM) -> list[tuple[int, int]]:
    """Return the saving requests sent."""
    return [
        (r["screen"], r["part"])
        for r in itm.requests
        if (r["screen"], r["part"]) in SAVES and r["event"] == 1
    ]


async def test_read_rule_settings(itm: FakeEnergyMoreITM) -> None:
    """Every rule's weekdays and shown special days, each range by its row."""
    nav = await client(itm).fetch_energy_navigator(["rule_settings"])
    days = nav.rule_settings
    assert days is not None
    assert len(days) == 2 * (7 + 4)  # the fifth special day is hidden
    monday = next(d for d in days if (d.rule, d.day) == ("Rule A", "monday"))
    assert monday.ranges == (RuleRange("08:00", "18:00", 26.0, 20.0),)
    assert monday.rows == (("08:00-18:00", "26.0", "20.0"),)
    assert {d.day for d in days if d.rule == "Rule B"} >= {"sunday", "Holiday 4"}
    assert nav.budget is None
    assert saves(itm) == []
    assert_closed(itm)


async def test_read_rule_settings_skips_disabled(itm: FakeEnergyMoreITM) -> None:
    """A rule whose Edit stays disabled, or a day's Edit, is skipped."""
    original = itm._select_row

    def keep_disabled(screen: int, row: int) -> list[Any]:
        updates = original(screen, row)
        if screen == 3213 and row == 1:
            itm.locked.add((3213, 8))
            return [("UPDATE", 3213, [], [(8, "ENABLED", 0)])]
        return updates

    itm._select_row = keep_disabled  # type: ignore[method-assign]
    original_editor = itm._editor_event

    def no_special_edit(ev: dict[str, Any]) -> Any:
        updates = original_editor(ev)
        if ev["part"] in {33, 34, 35, 36, 37}:
            itm.locked.add((3214, 50))
            return [("UPDATE", 3214, [], [(50, "ENABLED", 0)])]
        return updates

    itm._editor_event = no_special_edit  # type: ignore[method-assign]
    days = await run(itm, en.read_rule_settings)
    assert [d.day for d in days] == list(en.WEEKDAYS)
    assert_closed(itm)
    itm.unavailable = {3210}
    assert await run(itm, en.read_rule_settings) is None


async def test_set_rule_day(itm: FakeEnergyMoreITM) -> None:
    """The day's rows are replaced, 3217's OK, the editor's OK saves; read back."""
    c = client(itm)
    wanted = [RuleRange("07:00", "12:00", cool=25.5), RuleRange("13:00", "17:30")]
    day = await c.set_energy_rule_day("Rule A", "monday", wanted)
    assert isinstance(day, RuleDay)
    assert day.ranges == tuple(wanted)
    assert itm.rule_days["Rule A"]["1"] == [
        {"start": "07:00", "end": "12:00", "cool": 25.5, "heat": None},
        {"start": "13:00", "end": "17:30", "cool": None, "heat": None},
    ]
    special = await c.set_energy_rule_day(
        "Rule B", "Holiday 2", [RuleRange("09:00", "10:00", heat=21)]
    )
    assert special is not None
    assert special.ranges == (RuleRange("09:00", "10:00", None, 21.0),)
    assert itm.rule_days["Rule B"]["s1"][0]["heat"] == 21.0
    assert itm.saved == ["rule Rule A", "rule Rule B"]
    assert saves(itm) == [(3214, 29), (3214, 29)]
    assert_closed(itm)


async def test_rule_day_dry_run_and_read_only(itm: FakeEnergyMoreITM) -> None:
    """A dry run plans the editor's OK and discards; read_only sends nothing."""
    before = dict(itm.rule_days["Rule A"])
    dry = client(itm, dry_run=True)
    assert await dry.set_energy_rule_day("Rule A", "monday", []) is None
    assert [p.split(":")[0] for p in dry.planned] == ["save rule Rule A (monday)"]
    assert itm.rule_days["Rule A"] == before
    assert saves(itm) == []
    assert_closed(itm)
    itm.requests.clear()
    ro = client(itm, read_only=True)
    calls: list[Callable[[], Awaitable[Any]]] = [
        lambda: ro.set_energy_rule_day("Rule A", "monday", []),
        lambda: ro.create_energy_rule("Rule C"),
        lambda: ro.rename_energy_rule("Rule A", "Rule C"),
        lambda: ro.delete_energy_rule("Rule A"),
        lambda: ro.copy_energy_rule("Rule A", "Rule C"),
        lambda: ro.copy_energy_rule_calendar("Rule A", ["Rule B"]),
    ]
    for call in calls:
        with pytest.raises(ReadOnlyError):
            await call()
    assert itm.requests == []
    async with ro._session() as s:
        for flow in (
            ee.set_rule_day(s, "Rule A", "monday", ()),
            ee.create_rule(s, "Rule C"),
            ee.rename_rule(s, "Rule A", "Rule C"),
            ee.delete_rule(s, "Rule A"),
            ee.copy_rule(s, "Rule A", "Rule C"),
            ee.copy_rule_calendar(s, "Rule A", ("Rule B",)),
        ):
            with pytest.raises(ReadOnlyError):
                await flow
    assert itm.saved == []


async def test_rule_day_refusals(itm: FakeEnergyMoreITM) -> None:
    """Bad days, times and values, and disabled buttons: refused, nothing saved."""
    c = client(itm)
    with pytest.raises(ItmCommandError, match="'someday' is not a weekday"):
        await c.set_energy_rule_day("Rule A", "someday", [])
    with pytest.raises(ItmCommandError, match="rule 'Rule Z' not listed"):
        await c.set_energy_rule_day("Rule Z", "monday", [])
    with pytest.raises(ItmCommandError, match="a time is HH:MM"):
        await c.set_energy_rule_day("Rule A", "monday", [RuleRange("8h", "9:00")])
    with pytest.raises(ItmCommandError, match="outside the iTM's range"):
        await c.set_energy_rule_day("Rule A", "monday", [RuleRange("24:30", "25:00")])
    with pytest.raises(ItmCommandError, match="cool setpoint: 40"):
        await c.set_energy_rule_day(
            "Rule A", "monday", [RuleRange("08:00", "09:00", cool=40)]
        )
    itm.disabled = {(3217, 8)}
    with pytest.raises(ItmCommandError, match="start: the iTM has this disabled"):
        await c.set_energy_rule_day("Rule A", "monday", [RuleRange("8:00", "9:00")])
    itm.disabled = {(3214, 29)}
    with pytest.raises(ItmCommandError, match="has this disabled"):
        await c.set_energy_rule_day("Rule A", "monday", [])
    itm.disabled = set()
    original = itm._editor_event

    def edit_stays_disabled(ev: dict[str, Any]) -> Any:
        updates = original(ev)
        if ev["part"] in en.RE_WEEKDAY_RADIOS:
            itm.locked.add((3214, 23))
            return [("UPDATE", 3214, [], [(23, "ENABLED", 0)])]
        return updates

    itm._editor_event = edit_stays_disabled  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="does not offer editing monday"):
        await c.set_energy_rule_day("Rule A", "monday", [])
    with pytest.raises(ItmCommandError, match="does not offer editing monday"):
        await run(itm, lambda s: en.read_rule_day(s, "Rule A", "monday"))
    assert itm.saved == []
    assert_closed(itm)


async def test_rule_day_not_taking_effect(itm: FakeEnergyMoreITM) -> None:
    """Rows that don't go, a 3217 or editor that stays, a read-back mismatch."""
    c = client(itm)
    original = itm._ranges_event

    def stuck(ev: dict[str, Any]) -> Any:
        if ev["part"] in {3, 21} and ev["event"] == 1:
            return [("UPDATE", 3217, [], [])]
        return original(ev)

    itm._ranges_event = stuck  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="was not deleted"):
        await c.set_energy_rule_day("Rule A", "monday", [])
    with pytest.raises(ItmCommandError, match="rule settings did not close"):
        await c.set_energy_rule_day("Rule A", "tuesday", [])
    itm._ranges_event = original  # type: ignore[method-assign]
    itm._save_rule = list  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="rule editor did not close"):
        await c.set_energy_rule_day("Rule A", "tuesday", [])
    itm._save_rule = lambda: itm._close(3214)  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="the iTM shows"):
        await c.set_energy_rule_day("Rule A", "monday", [])
    assert_closed(itm)


def test_same_ranges() -> None:
    """Times compare as times, setpoints as numbers, None only with None."""
    a = RuleRange("8:00", "9:00", 25.0)
    assert ee.same_ranges((a,), (RuleRange("08:00", "09:00", 25),))
    assert not ee.same_ranges((a,), (RuleRange("08:00", "09:15", 25),))
    assert not ee.same_ranges((a,), (RuleRange("08:00", "09:00"),))
    assert not ee.same_ranges((a,), ())


def test_rule_range_readers_on_unopened_screens() -> None:
    """Nothing shown: empty values; an unchecked setpoint gives None."""
    state = UIState()
    assert en.rule_range_from_screen(state) == RuleRange("", "")
    assert en.special_days(state) == {f"Ex{i}": 32 + i for i in range(1, 6)}
    state.screens[3217] = {(12, "CHECK_STATE"): 1, (13, "CHECK_STATE"): 1}
    assert en.rule_range_from_screen(state).cool is None  # no text shown


async def test_rule_list_writes(itm: FakeEnergyMoreITM) -> None:
    """Create, rename and delete rules, each confirmed by the rule list."""
    c = client(itm)
    await c.create_energy_rule("Rule C")
    await c.rename_energy_rule("Rule C", "Rule D")
    await c.delete_energy_rule("Rule D")
    assert [r[0] for r in itm.rules] == ["Rule A", "Rule B"]
    assert itm.saved == ["create 3213", "rename 3213", "delete 3213"]
    dry = client(itm, dry_run=True)
    await dry.create_energy_rule("Rule C")
    await dry.rename_energy_rule("Rule A", "Rule C")
    await dry.delete_energy_rule("Rule A")
    assert [p.split(":")[0] for p in dry.planned] == [
        "create rule Rule C",
        "rename rule Rule A to Rule C",
        "delete rule Rule A",
    ]
    with pytest.raises(ItmCommandError, match="exists already"):
        await c.create_energy_rule("Rule A")
    with pytest.raises(ItmCommandError, match="exists already"):
        await c.rename_energy_rule("Rule A", "Rule B")
    with pytest.raises(ItmCommandError, match="'Rule Z' not listed once"):
        await c.delete_energy_rule("Rule Z")
    assert len(itm.saved) == 3
    assert_closed(itm)


async def test_rule_list_writes_not_taking_effect(itm: FakeEnergyMoreITM) -> None:
    """A create, rename or delete the rule list doesn't show fails."""
    c = client(itm)
    itm._named = lambda *_: []  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="did not create"):
        await c.create_energy_rule("Rule C")
    with pytest.raises(ItmCommandError, match="did not rename"):
        await c.rename_energy_rule("Rule A", "Rule C")
    original = itm._confirm_dialog
    itm._confirm_dialog = lambda screen, *_: original(  # type: ignore[method-assign]
        screen, list
    )
    with pytest.raises(ItmCommandError, match="did not delete"):
        await c.delete_energy_rule("Rule A")
    itm.unavailable = {3210}
    with pytest.raises(ItmCommandError, match="did not open"):
        await c.create_energy_rule("Rule C")
    assert await client(itm)._rule_names() == ()
    assert_closed(itm)


async def test_copy_rule_under_a_new_name(itm: FakeEnergyMoreITM) -> None:
    """Copy asks the new rule's name; the copy holds the source's patterns."""
    c = client(itm)
    await c.copy_energy_rule("Rule A", "Rule C")
    assert itm.rule_days["Rule C"] == itm.rule_days["Rule A"]
    assert itm.saved == ["copy 3213"]
    with pytest.raises(ItmCommandError, match="exists already"):
        await c.copy_energy_rule("Rule A", "Rule B")
    dry = client(itm, dry_run=True)
    await dry.copy_energy_rule("Rule A", "Rule D")
    assert "Rule D" not in itm.rule_days
    assert_closed(itm)


async def test_copy_calendar(itm: FakeEnergyMoreITM) -> None:
    """3216 copies the calendar, read back in the editor."""
    c = client(itm)
    await c.copy_energy_rule_calendar("Rule A", ["Rule B"])
    assert itm.calendars["Rule B"] == [["01/01", "Holiday 1"]]
    assert itm.saved == ["copy 3216"]
    dry = client(itm, dry_run=True)
    await dry.copy_energy_rule_calendar("Rule B", ["Rule A"])
    assert [p.split(":")[0] for p in dry.planned] == [
        "copy calendar of rule Rule B to Rule A"
    ]
    assert len(itm.saved) == 1
    assert_closed(itm)


async def test_copy_refusals(itm: FakeEnergyMoreITM) -> None:
    """No target, the source itself, an unknown target, another source shown."""
    c = client(itm)
    with pytest.raises(ItmCommandError, match="name other rules"):
        await c.copy_energy_rule_calendar("Rule A", [])
    with pytest.raises(ItmCommandError, match="name other rules"):
        await c.copy_energy_rule_calendar("Rule A", ["Rule A"])
    with pytest.raises(ItmCommandError, match="not every target is listed once"):
        await c.copy_energy_rule_calendar("Rule A", ["Rule Z"])
    original = itm._copy_parts
    itm._copy_parts = lambda target: [  # type: ignore[method-assign]
        *original(target),
        (2, "TEXT", "Rule Z"),
    ]
    with pytest.raises(ItmCommandError, match="copies from 'Rule Z'"):
        await c.copy_energy_rule_calendar("Rule A", ["Rule B"])
    itm._copy_parts = original  # type: ignore[method-assign]
    itm._confirm_dialog = lambda screen, *_: [  # type: ignore[method-assign]
        ("DLG_OPEN", screen, [60, 1, "Error", "Error", "OK"], [])
    ]
    with pytest.raises(ItmCommandError, match="iTM refused"):
        await c.copy_energy_rule_calendar("Rule A", ["Rule B"])
    assert itm.saved == []
    itm.violations.clear()
    itm.edit_dialog = None
    assert_closed(itm)


async def test_copy_not_taking_effect(itm: FakeEnergyMoreITM) -> None:
    """A target whose editor doesn't show the copy fails the confirmation."""
    c = client(itm)
    itm.rule_days["Rule B"] = {"3": [{"start": "01:00", "end": "02:00"}]}
    itm.rule_days["Rule B"]["3"][0] |= {"cool": None, "heat": None}
    itm._confirm_dialog = lambda screen, *_: itm._close(  # type: ignore[method-assign]
        screen
    )
    with pytest.raises(ItmCommandError, match="does not show the copy"):
        await c.copy_energy_rule_calendar("Rule A", ["Rule B"])
    itm._named = lambda screen, *_: itm._list_update(screen)  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="did not copy it"):
        await c.copy_energy_rule("Rule A", "Rule E")
    assert_closed(itm)


def test_cli_rules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`itm energy --only rule_settings` and the rule actions of `energy-edit`."""
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

    def main(*args: str) -> str:
        itm_cli.main(["--host", "fake-host", "--experimental", *args])
        return capsys.readouterr().out

    out = main("--read-only", "energy", "--only", "rule_settings")
    assert "rule Rule A monday: 08:00-18:00/cool=26/heat=20" in out
    assert "rule Rule B sunday: (none)" in out
    out = main(
        "energy-edit", "rule-day", "Rule A", "--day", "friday",
        "--range", "07:00-09:00/heat=19.5", "--range", "10:00-11:00",
    )  # fmt: skip
    assert "rule-day: done, the iTM shows" in out
    assert fake.rule_days["Rule A"]["5"][0]["heat"] == 19.5
    main("energy-edit", "rule-create", "Rule C")
    main("energy-edit", "rule-rename", "Rule C", "Rule D")
    main("energy-edit", "calendar-copy", "Rule A", "--to", "Rule D")
    main("energy-edit", "rule-copy", "Rule A", "Rule E")
    assert fake.rule_days["Rule E"] == fake.rule_days["Rule A"]
    main("energy-edit", "rule-delete", "Rule E")
    out = main("energy-edit", "rule-delete", "Rule D")
    assert out == "rule-delete: done\n"
    out = main("--dry-run", "energy-edit", "rule-delete", "Rule B")
    assert "delete rule Rule B" in out
    for args, error in (
        (["rule-rename", "Rule A"], "needs a rule name"),
        (["rule-copy", "Rule A"], "needs a rule name"),
        (["calendar-copy", "Rule A"], "needs --to"),
        (["rule-day", "Rule A"], "needs --day"),
        (["rule-day", "Rule A", "--day", "monday", "--range", "1-2/x=1"], "--range"),
        (["rule-day", "Rule A", "--day", "monday", "--range", "1-2/cool=x"], "--range"),
    ):
        with pytest.raises(SystemExit, match=error):
            main("energy-edit", *args)
    assert not fake.violations


async def test_time_input_refusals(itm: FakeEnergyMoreITM) -> None:
    """A button opening no time input, or a time the iTM refuses: raised."""
    c = client(itm)
    original = itm._ranges_event
    itm._ranges_event = lambda ev: (  # type: ignore[method-assign]
        [("UPDATE", 3217, [], [])] if ev["part"] == 8 else original(ev)
    )
    with pytest.raises(ItmCommandError, match="no time input opened"):
        await c.set_energy_rule_day("Rule A", "monday", [RuleRange("8:00", "9:00")])
    itm._ranges_event = original  # type: ignore[method-assign]
    answer = itm._answer

    def refuse(screen: int, ev: dict[str, Any]) -> Any:
        updates = answer(screen, ev)
        if screen == 3217:
            return [*updates, ("DLG_OPEN", 3217, [60, 1, "Error", "Error", "OK"], [])]
        return updates

    itm._answer = refuse  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="the iTM refused 8:00"):
        await c.set_energy_rule_day("Rule A", "monday", [RuleRange("8:00", "9:00")])
    assert itm.saved == []
    assert itm.violations == []
