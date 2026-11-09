# pylint: disable=protected-access,redefined-outer-name
"""The main menu, read from a real iTM's captures and from the fake.

Info, auto changeover, setback, overtime, emergency stop, schedules, history and
the hidden functions.
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from daikin_itm import ChangeoverReference, ItmCommandError, ProtocolError
from daikin_itm.menu import (
    MenuScreen,
    changeover_candidates_from_screen,
    changeover_groups_from_screen,
    changeover_members_from_screen,
    changeover_settings_from_screen,
    clock_from_screen,
    emergency_stop_from_screen,
    history_from_screen,
    indicators_from_screen,
    overtime_from_screen,
    parse_shown_date,
    region_from_screen,
    schedules_from_screen,
    setback_from_screen,
    version_from_screen,
)
from daikin_itm.protocol import Part, ScreenUpdate, parse_reply
from daikin_itm.state import UIState

from .fake_itm import FakeGroup, FakeITM
from .test_client import FakeClient

FIXTURE = Path(__file__).parent / "fixtures" / "menu_screens.json"


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM with one auto changeover group."""
    fake = FakeITM()
    fake.groups = [FakeGroup("Group 01", enabled=True, members=["1:1-04"])]
    return fake


def _replay(name: str, *, stop_before: list[int] | None = None) -> UIState:
    """Replay a captured session's replies, stopping before a given request."""
    s = UIState()
    for entry in json.loads(FIXTURE.read_text(encoding="utf-8"))[name]:
        if stop_before and entry["request"][:2] == stop_before:
            break
        s.apply(parse_reply(entry["reply"]).screens)
    return s


# -- captured screens ------------------------------------------------------------


def test_captured_indicators() -> None:
    """The main menu bar of a quiet iTM shows no indicators, only the clock."""
    ind = indicators_from_screen(_replay("menu"))
    assert ind.energy_saving is False
    assert ind.error is False
    assert ind.emergency_stop is False
    assert ind.clock_text == "Wed, 01/01\n11:00"


def test_captured_version() -> None:
    """The version screen gives the version and the build line."""
    v = version_from_screen(_replay("version", stop_before=[2600, 2]))
    assert v == {"version": "1.00.00", "build": "Jan 01 2025, 00:00:00"}
    assert version_from_screen(UIState()) == {"version": None, "build": None}


def test_captured_region() -> None:
    """The region screen gives language, formats and the temperature unit."""
    r = region_from_screen(_replay("region", stop_before=[2500, 19]))
    assert r == {
        "language": "English",
        "date_format": "%d/%m/%Y",
        "time_24h": True,
        "temperature_unit": "°C",
        "decimal_separator": ".",
    }
    assert not region_from_screen(UIState())


def test_captured_clock() -> None:
    """The time screen gives the iTM's clock and the DST setting."""
    c = clock_from_screen(_replay("time", stop_before=[2400, 19]), "%d/%m/%Y")
    assert c["clock"] == datetime(2025, 1, 1, 10, 15, 30)  # noqa: DTZ001
    assert c["clock_text"] == "01/01/2025 10:15:30"
    assert c["dst_enabled"] is False
    assert clock_from_screen(UIState(), None)["clock"] is None


def test_captured_changeover() -> None:
    """The changeover screens give the groups, members and settings."""
    groups = changeover_groups_from_screen(_replay("changeover", stop_before=[300, 2]))
    assert len(groups) == 1
    assert groups[0].name == "Group 01"
    assert groups[0].enabled is True
    assert groups[0].member_count == 1
    members = _replay("changeover", stop_before=[301, 13])
    assert changeover_members_from_screen(members) == ("1:1-07",)
    candidates = changeover_candidates_from_screen(members)
    assert len(candidates) == 11  # the 12 units minus the member
    assert ("1:1-00", "", "Able") in candidates
    assert all(name != "1:1-07" for name, _, _ in candidates)
    settings = changeover_settings_from_screen(
        _replay("changeover", stop_before=[302, 12])
    )
    assert settings == {"differential": 1, "reference": ChangeoverReference.AVERAGE}


def test_captured_setback() -> None:
    """The setback screens give the offsets and the recovery temperatures."""
    sb = setback_from_screen(_replay("setback_advanced", stop_before=[3001, 11]))
    assert (sb.high_up, sb.high_back, sb.low_up, sb.low_back) == (3, 4, 2, 1)
    assert (sb.recovery_cool, sb.recovery_heat) == (1, 4)
    options = (
        sb.high_up_options,
        sb.high_back_options,
        sb.low_up_options,
        sb.low_back_options,
        sb.recovery_cool_options,
        sb.recovery_heat_options,
    )
    assert all(options)
    assert sb.high_up in sb.high_up_options
    plain = setback_from_screen(_replay("setback", stop_before=[3000, 21]))
    assert plain.recovery_cool is None
    assert not plain.recovery_cool_options
    assert plain.high_up_options


def test_captured_overtime() -> None:
    """The overtime screen gives the duration in minutes."""
    assert overtime_from_screen(_replay("overtime", stop_before=[700, 4])) == 60
    assert overtime_from_screen(UIState()) is None


def test_captured_emergency_stop() -> None:
    """The emergency stop screen lists the programs."""
    programs = emergency_stop_from_screen(
        _replay("emergency_stop", stop_before=[200, 1])
    )
    assert [(p.name, p.enabled) for p in programs] == [("Prog.1", True)]


def test_emergency_stop_disabled_program() -> None:
    """A row showing the disable radio's label is a disabled program."""
    state = UIState()
    rows = [
        {"text": ["Enable", "Fire alarm"], "a": [-1, -1], "b": [-1, -1]},
        {"text": ["Disable", "Test"], "a": [-1, -1], "b": [-1, -1]},
    ]
    state.screens[200] = {
        (2, "LIST_ROW"): {"rows": rows, "selected": []},
        (4, "LABEL"): "Enable",
        (5, "LABEL"): "Disable",
    }
    programs = emergency_stop_from_screen(state)
    assert [(p.name, p.enabled) for p in programs] == [
        ("Fire alarm", True),
        ("Test", False),
    ]


def test_captured_schedules() -> None:
    """The schedule panel lists every program with its state and period."""
    programs = schedules_from_screen(_replay("schedule", stop_before=[100, 13]))
    assert len(programs) == 6
    by_name = {p.name: p for p in programs}
    assert by_name["Always"].enabled is True
    assert by_name["Program A"].enabled is False
    assert by_name["Program C"].period == ("15/03", "15/06")
    assert by_name["Program C"].state == 4
    assert by_name["Always"].period is None
    assert not schedules_from_screen(UIState())


def test_captured_history() -> None:
    """The history page decodes into entries; management rows are logins."""
    h = history_from_screen(_replay("history", stop_before=[800, 17]))
    assert h.page == "1-100/1500"
    assert h.day == "01/01/2025"
    assert len(h.columns) == 10
    assert len(h.entries) == 100
    assert h.entries[0].kind == "Control"
    assert h.entries[0].content.startswith("Logged out(")
    assert h.entries[0].name == "admin"


def test_history_layout_is_checked() -> None:
    """Another number of history columns is refused."""
    s = UIState()
    rows = {
        "rows": [{"text": ["a", "b"], "a": [-1, -1], "b": [-1, -1]}],
        "selected": [],
    }
    s.apply([ScreenUpdate(800, "OPEN", [-1], [Part(1, "LIST_ROW", rows)])])
    with pytest.raises(ProtocolError, match="2 columns"):
        history_from_screen(s)


def test_parse_shown_date() -> None:
    """Dates as the iTM shows them parse with its format, or any known one."""
    assert parse_shown_date("01/01/2025", "%d/%m/%Y") == date(2025, 1, 1)
    assert parse_shown_date("2025/01/01", None) == date(2025, 1, 1)
    assert parse_shown_date("nope", None) is None


# -- the fake iTM ------------------------------------------------------------------


async def test_fetch_info(itm: FakeITM) -> None:
    """Info reads version, region and clock, and closes the menu again."""
    info = await FakeClient(itm).fetch_info()
    assert info.version == "1.00.00"
    assert info.language == "English"
    assert info.temperature_unit == "°C"
    assert info.clock == datetime(2025, 1, 1, 10, 15, 30)  # noqa: DTZ001
    assert info.dst_enabled is False
    assert info.clock_offset is not None
    assert itm.menu_open is False
    assert itm.open_screens == set()
    assert itm.session is None


async def test_fetch_indicators(itm: FakeITM) -> None:
    """The indicators come from the main menu bar, without opening anything."""
    itm.emergency_stop_active = True
    ind = await FakeClient(itm).fetch_indicators()
    assert ind.emergency_stop is True
    assert ind.error is False
    assert len(itm.requests) == 3  # init, login, logout


async def test_fetch_changeover_groups(itm: FakeITM) -> None:
    """Groups with and without details; every screen closed again."""
    plain = await FakeClient(itm).fetch_changeover_groups()
    assert plain[0].name == "Group 01"
    assert plain[0].members == ()
    detailed = await FakeClient(itm).fetch_changeover_groups(details=True)
    assert detailed[0].members == ("1:1-04",)
    assert detailed[0].differential == 3
    assert detailed[0].reference is ChangeoverReference.AVERAGE
    assert itm.open_screens == set()
    itm.groups = []
    assert await FakeClient(itm).fetch_changeover_groups(details=True) == []


async def test_fetch_setback_overtime_emergency(itm: FakeITM) -> None:
    """Setback, overtime and emergency stop reads."""
    c = FakeClient(itm)
    sb = await c.fetch_setback()
    assert (sb.high_up, sb.low_back, sb.recovery_heat) == (3, 2, 1)
    assert await c.fetch_overtime_duration() == 60
    assert [(p.name, p.enabled) for p in await c.fetch_emergency_stop()] == [
        ("Prog.1", True)
    ]
    assert [p.name for p in await c.fetch_schedules()] == [
        "Always",
        "Program G",
        "Program E",
    ]
    assert itm.open_screens == set()


async def test_fetch_history(itm: FakeITM) -> None:
    """The history loads behind a progress dialog; logins are left out by default."""
    pages = await FakeClient(itm).fetch_history()
    assert len(pages) == 1
    assert [e.source for e in pages[0].entries] == ["Auto Change", "Schedule"]
    assert pages[0].entries[0].name == "1:1-04"
    assert pages[0].day == "01/01/2025"
    assert itm.open_screens == set()
    assert itm.session is None

    with_logins = await FakeClient(itm).fetch_history(include_management=True)
    assert len(with_logins[0].entries) == 3

    other_day = await FakeClient(itm).fetch_history(day=date(2024, 12, 31))
    assert other_day[0].day == "31/12/2024"
    assert other_day[0].entries == ()
    # The list follows the day only after a Refresh, as on the real iTM.
    assert itm.history_loaded_day == "31/12/2024"

    itm.requests.clear()
    two_years_back = await FakeClient(itm).fetch_history(day=date(2023, 1, 1))
    assert two_years_back[0].day == "01/01/2023"
    assert len(itm.requests) < 20


async def test_fetch_history_pages(itm: FakeITM) -> None:
    """Older pages come from "<", and only while the iTM enables it."""
    itm.history_page_size = 1
    pages = await FakeClient(itm).fetch_history(pages=5)
    assert [p.page for p in pages] == ["1-1/2", "2-2/2"]
    itm.history_page_size = 100
    everything = (await FakeClient(itm).fetch_history())[0].entries
    assert pages[0].entries + pages[1].entries == everything


async def test_fetch_history_single_page(itm: FakeITM) -> None:
    """With one page, no page button is pressed: both are disabled."""
    pages = await FakeClient(itm).fetch_history(pages=3)
    assert len(pages) == 1
    sent = {(r["screen"], r["part"]) for r in itm.requests}
    assert not {(800, 15), (800, 16)} & sent


async def test_history_progress_times_out(itm: FakeITM) -> None:
    """A list that never finishes loading is an error, not a hang."""
    itm.history_rows = []

    original = itm.handle

    def never_done(body: str) -> str:
        reply = original(body)
        itm.history_loading = True  # the iTM keeps loading
        return reply

    itm.handle = never_done  # type: ignore[method-assign]
    c = FakeClient(itm)
    c.progress_interval = 0
    with pytest.raises(ItmCommandError, match="loading"):
        await c.fetch_history()
    assert itm.session is None


async def test_fetch_optional_functions(itm: FakeITM) -> None:
    """Hidden functions are probed: available ones list their rows."""
    itm.optional_available[900] = False
    functions = await FakeClient(itm).fetch_optional_functions()
    assert functions["temperature_limit"].available is True
    assert functions["temperature_limit"].rows == ()
    assert functions["heating_optimisation"].rows == (("", "1:1-00"),)
    assert functions["interlock"].available is False
    assert itm.open_screens == set()


async def test_fetch_config(itm: FakeITM) -> None:
    """Everything in one session."""
    itm.energy_saving = True
    config = await FakeClient(itm).fetch_config()
    assert config.indicators.energy_saving is True
    assert config.info.version == "1.00.00"
    assert [p.name for p in config.schedules] == ["Always", "Program G", "Program E"]
    assert config.changeover_groups[0].name == "Group 01"
    assert config.setback.high_up == 3
    assert config.overtime_minutes == 60
    assert config.emergency_stop[0].enabled is True
    logins = [r for r in itm.requests if r["screen"] == 1203]
    assert len(logins) == 1
    assert itm.session is None


async def test_menu_screen_refused(itm: FakeITM) -> None:
    """A menu screen that does not open is reported."""
    c = FakeClient(itm)
    async with c._session() as s:
        assert await s.open_menu_screen(5, MenuScreen.INTERLOCK) is True
        await s.close_screen(MenuScreen.INTERLOCK)
        itm.optional_available[900] = False
        assert await s.open_menu_screen(5, MenuScreen.INTERLOCK) is False
        await s.close_menu()
    assert itm.menu_open is False
