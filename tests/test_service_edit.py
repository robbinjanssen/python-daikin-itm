# pylint: disable=protected-access,redefined-outer-name
"""Leakage check, layout setup, R32 output ports and the password setting."""

from __future__ import annotations

import logging
from datetime import date
from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import (
    ItmCommandError,
    LeakageCheckResults,
    PasswordSettings,
    ReadOnlyError,
    service_edit,
)
from daikin_itm import cli as itm_cli
from daikin_itm.service_edit import (
    copy_schedule,
    export_results,
    layout_setup_job,
    parse_results,
    parse_time,
    port_shown,
    read_layout_setup,
    read_leakage_check,
    read_passwords,
    read_r32_ports,
    run_check,
    set_password,
    set_r32_port,
    set_registration,
    set_schedule_program,
)

from .fake_service_edit import FakeServiceEditITM
from .test_cli import CliClient
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

SERVICE = "test-service"
SECRET = "n3w-Secret"


@pytest.fixture
def itm() -> FakeServiceEditITM:
    """Return a simulated iTM with the service edit screens."""
    return FakeServiceEditITM()


def client(itm: FakeServiceEditITM, **kw: Any) -> FakeClient:
    """Return a client for the fake that fetches its exported files."""
    c = FakeClient(itm, **kw)
    c.progress_interval = 0

    async def get(path: str) -> bytes:
        return itm.files[path]

    c._get_bytes = get  # type: ignore[method-assign]
    return c


async def run(
    itm: FakeServiceEditITM,
    flow: Callable[..., Awaitable[Any]],
    *args: Any,
    c: FakeClient | None = None,
    **kwargs: Any,
) -> Any:
    """Run one flow in a fresh session, like the client's wrappers do."""
    c = c or client(itm)
    async with c._session() as s:
        try:
            return await flow(s, *args, **kwargs)
        finally:
            await s.close_menu()


def strings(itm: FakeServiceEditITM) -> list[str]:
    """Return every string the client sent."""
    return [r["string"] for r in itm.requests if r["string"]]


# -- leakage check --------------------------------------------------------------------


async def test_read_leakage_check(itm: FakeServiceEditITM) -> None:
    """The outdoor list by column; details open 3101 and 3102 and cancel them."""
    lc = await run(itm, read_leakage_check, SERVICE)
    assert [(u.address, u.name, u.indoor_units) for u in lc.outdoor_units] == [
        ("1-00", "OU-1", "1"),
        ("1-01", "OU-2", "0"),
    ]
    assert lc.registrations == {}
    lc = await run(itm, read_leakage_check, SERVICE, details=True)
    assert lc.registrations["OU-1"].registered == (itm.indoor[0],)
    assert itm.indoor[1] in lc.registrations["OU-1"].unregistered
    program = lc.schedules["OU-2"].programs[0]
    assert (program.activation, program.date, program.time) == (
        "Disable",
        "01/01/2026",
        "02:00",
    )
    assert lc.schedules["OU-2"].name == "OU-2"
    assert itm.open_screens == set()


async def test_buttons_disabled_without_selection(itm: FakeServiceEditITM) -> None:
    """With no outdoor unit the buttons stay disabled and are never clicked."""
    itm.outdoors = []
    lc = await run(itm, read_leakage_check, SERVICE, details=True)
    assert lc.outdoor_units == ()
    with pytest.raises(ItmCommandError, match="not found once"):
        await run(itm, run_check, SERVICE, "OU-1", start=True)
    with pytest.raises(ItmCommandError, match="no outdoor unit to export"):
        await run(itm, export_results, SERVICE, None)


async def test_registration(itm: FakeServiceEditITM) -> None:
    """Units are added and removed to match exactly, then OK + Yes."""
    wanted = (itm.indoor[1], f"A{itm.indoor[2]}")  # a name and an "address"
    assert await run(itm, set_registration, SERVICE, "1-00", wanted) is True
    assert itm.outdoors[0].registered == [itm.indoor[1], itm.indoor[2]]
    with pytest.raises(ItmCommandError, match="not found once"):
        await run(itm, set_registration, SERVICE, "OU-1", ("nope",))


async def test_schedule_program(itm: FakeServiceEditITM) -> None:
    """Activation, date and time of one program, then OK + Yes."""
    sent = await run(
        itm,
        set_schedule_program,
        SERVICE,
        "OU-2",
        2,
        activation="enable",
        day=date(2027, 3, 4),
        at="23:15",
    )
    assert sent is True
    p = itm.outdoors[1].programs[1]
    assert (p.activation, p.day, p.at) == (1, (2027, 2, 4), (23, 15))
    for kwargs, match in (
        ({"activation": "sometimes"}, "not offered"),
        ({"day": date(2040, 1, 1)}, "outside"),
        ({"at": "25:00"}, "HH:MM"),
        ({}, "nothing to change"),
    ):
        with pytest.raises(ItmCommandError, match=match):
            await run(itm, set_schedule_program, SERVICE, "OU-2", 1, **kwargs)
    with pytest.raises(ItmCommandError, match="1-4"):
        await run(itm, set_schedule_program, SERVICE, "OU-2", 5, at="01:00")
    assert itm.open_screens == set()


async def test_hourmin_range(itm: FakeServiceEditITM) -> None:
    """A time outside the dialog's range is refused and the dialog cancelled."""
    c = client(itm)
    async with c._session() as s:
        await service_edit._open_leakage(s, SERVICE)
        await service_edit._select_outdoor(s, "OU-1")
        await service_edit._open_sub(s, 3100, 8, 3102, "schedule")
        assert 3102 in s.state.screens
        itm.edit["programs"][0].at = (1, 0)
        original = itm._schedule_event

        def narrow(ev: dict[str, Any]) -> Any:
            out = original(ev)
            for u in out:
                if u[0] == "DLG_OPEN" and u[2][1] == 10:
                    u[2][8:10] = [12, 0]
            return out

        itm._schedule_event = narrow  # type: ignore[method-assign]
        with pytest.raises(ItmCommandError, match="outside"):
            await service_edit.answer_hourmin(s, 3102, 8, "13:00", "time")
        with pytest.raises(ItmCommandError, match="no time input"):
            await service_edit.answer_hourmin(s, 3102, 2, "01:00", "time")
        await s.close_menu()


async def test_copy_schedule(itm: FakeServiceEditITM) -> None:
    """The source's programs are copied to the chosen outdoor units."""
    itm.outdoors[0].programs[0].activation = 1
    assert await run(itm, copy_schedule, SERVICE, "OU-1", ("OU-2",)) is True
    assert itm.outdoors[1].programs[0].activation == 1
    with pytest.raises(ItmCommandError, match="at least one"):
        await run(itm, copy_schedule, SERVICE, "OU-1", ())


async def test_start_stop(itm: FakeServiceEditITM) -> None:
    """Start, then stop; a disabled button is refused; an iTM refusal raises."""
    assert await run(itm, run_check, SERVICE, "OU-1", start=True) is True
    assert itm.outdoors[0].running
    with pytest.raises(ItmCommandError, match="disabled"):
        await run(itm, run_check, SERVICE, "OU-1", start=True)
    assert await run(itm, run_check, SERVICE, "OU-1", start=False) is True
    assert not itm.outdoors[0].running
    itm.refuse_start = True
    with pytest.raises(ItmCommandError, match="unsuitable"):
        await run(itm, run_check, SERVICE, "OU-1", start=True)
    assert itm.open_screens == set()


async def test_export_and_parse_results(itm: FakeServiceEditITM) -> None:
    """The CSV comes through the download flow and parses per outdoor unit."""
    c = client(itm)
    download = await c.export_leakage_check_results(SERVICE)
    assert download.name.startswith("LeakageCheck")
    assert itm.finished == [(1, 0)]
    results = parse_results(download.data)
    assert results.controller == "intelligent Touch Manager"
    assert results.version == "Ver 1.00.00"
    assert [(r.outdoor, r.amount) for r in results.results] == [
        ("OU-1", "0.0"),
        ("OU-1", "0.2"),
        ("OU-2", "0.1"),
    ]
    assert results.last("OU-1") is not None
    assert results.last("OU-1").amount == "0.2"  # type: ignore[union-attr]
    assert results.last("nope") is None
    parsed = await c.fetch_leakage_check_results(SERVICE)
    assert isinstance(parsed, LeakageCheckResults)


def test_parse_results_edge_cases() -> None:
    """Empty files, latin-1 bytes and missing label rows."""
    assert not parse_results(b"a\nb\nc\nd\n").results
    latin = "ctrl\nd\nv\nt\nOU-\xe9,\n2026/01/01,1.5\n".encode("latin-1")
    results = parse_results(latin)
    assert [(r.outdoor, r.checked, r.amount) for r in results.results] == [
        ("OU-\xe9", "2026/01/01", "1.5")
    ]
    assert not parse_results(b"c\nd\nv\nt\n,\n1,2\n").results


def test_parse_time() -> None:
    """HH:MM only."""
    assert parse_time("7:05") == (7, 5)
    for bad in ("24:00", "12:60", "noon"):
        with pytest.raises(ItmCommandError):
            parse_time(bad)


# -- layout, R32, passwords (session level) -------------------------------------------


async def test_layout(itm: FakeServiceEditITM) -> None:
    """The button's state is read without pressing; the job runs behind Yes."""
    assert (await run(itm, read_layout_setup, SERVICE)).offered is False
    itm.layout_data = True
    setup = await run(itm, read_layout_setup, SERVICE)
    assert (setup.offered, setup.enabled) == (True, None)
    assert not any(r["part"] == 81 for r in itm.requests)
    assert await run(itm, layout_setup_job, SERVICE) is True
    assert itm.layout_jobs == 1


async def test_r32_ports(itm: FakeServiceEditITM) -> None:
    """Read the ports and the offered ones; set by name, clear; tests never run."""
    ports = await run(itm, read_r32_ports, SERVICE)
    assert ports.ports == ("", "")
    assert ports.offered == ("DO Alarm out A 1-01", "DO Alarm out B 1-02")
    assert await run(itm, set_r32_port, SERVICE, 2, "Alarm out B") is True
    assert itm.ports == ["", "Alarm out B"]
    assert await run(itm, set_r32_port, SERVICE, 1, "DO Alarm out A 1-01") is True
    assert await run(itm, set_r32_port, SERVICE, 2, None) is True
    assert itm.ports == ["Alarm out A", ""]
    with pytest.raises(ItmCommandError, match="disabled"):
        await run(itm, set_r32_port, SERVICE, 2, None)
    with pytest.raises(ItmCommandError, match="not offered"):
        await run(itm, set_r32_port, SERVICE, 1, "DO")
    with pytest.raises(ItmCommandError, match="1 or 2"):
        await run(itm, set_r32_port, SERVICE, 3, None)
    assert itm.open_screens == set()
    assert port_shown("", None)
    assert port_shown("Alarm out A (1-01)", "Alarm out A")
    assert not port_shown("", "Alarm out A")


async def test_passwords(itm: FakeServiceEditITM) -> None:
    """Enable (the iTM asks the password twice), change, disable."""
    assert await run(itm, read_passwords) == PasswordSettings(
        administrator=False, screen=False
    )
    with pytest.raises(ItmCommandError, match="asks for one"):
        await run(itm, set_password, "administrator", enabled=True)
    assert not itm.enabled["administrator"]
    assert await run(itm, set_password, "administrator", enabled=True, password=SECRET)
    assert itm.pw_values["administrator"] == SECRET
    assert itm.enabled["administrator"]
    assert await run(itm, set_password, "administrator", password="0ther-pw")
    assert itm.pw_values["administrator"] == "0ther-pw"
    assert await run(itm, set_password, "administrator", enabled=False)
    assert not itm.enabled["administrator"]
    # Enabling again: a password is set, the radio only confirms.
    assert await run(itm, set_password, "administrator", enabled=True)
    assert itm.enabled["administrator"]
    with pytest.raises(ItmCommandError, match="4-15"):
        await run(itm, set_password, "administrator", password="abc")
    with pytest.raises(ItmCommandError, match="disabled"):
        await run(itm, set_password, "screen", password=SECRET)
    for kwargs, match in (
        ({}, "nothing to change"),
        ({"enabled": False, "password": SECRET}, "not changed while"),
    ):
        with pytest.raises(ItmCommandError, match=match):
            await run(itm, set_password, "screen", **kwargs)
    with pytest.raises(ItmCommandError, match="one of"):
        await run(itm, set_password, "root", enabled=True)
    assert itm.open_screens == set()


async def test_password_mismatch_and_short_enable(itm: FakeServiceEditITM) -> None:
    """The iTM's refusal and a too short password on enabling raise."""
    with pytest.raises(ItmCommandError, match="4-15"):
        await run(itm, set_password, "screen", enabled=True, password="ab")
    original = itm._password_answer

    def mismatch(dialog: tuple[Any, ...], ev: dict[str, Any]) -> Any:
        if dialog[0] == "repeat":
            ev = {**ev, "string": "different"}
        return original(dialog, ev)

    itm._password_answer = mismatch  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="refused"):
        await run(itm, set_password, "screen", enabled=True, password=SECRET)


# -- dry run, read only, guards -------------------------------------------------------


async def test_dry_run_plans_and_sends_nothing(itm: FakeServiceEditITM) -> None:
    """Every write in a dry run plans its request; nothing changes; no password."""
    dry = client(itm, dry_run=True)
    assert await dry.set_leakage_check_indoor_units(SERVICE, "OU-1", ()) is None
    assert await dry.set_leakage_check_schedule(SERVICE, "OU-1", 1, at="03:00") is None
    await dry.copy_leakage_check_schedule(SERVICE, "OU-1", ("OU-2",))
    assert await dry.start_leakage_check(SERVICE, "OU-1") is None
    assert await dry.run_layout_setup(SERVICE) is None
    assert await dry.set_r32_output_port(SERVICE, 1, "Alarm out A") is None
    assert (
        await dry.set_password_setting("administrator", enabled=True, password=SECRET)
        is None
    )
    itm.enabled["screen"] = True
    assert await dry.set_password_setting("screen", password=SECRET) is None
    with pytest.raises(ItmCommandError, match="4-15"):
        await dry.set_password_setting("screen", password="ab")
    assert itm.outdoors[0].registered == [itm.indoor[0]]
    assert itm.outdoors[0].programs[0].at == (2, 0)
    assert not itm.outdoors[0].running
    assert itm.layout_jobs == 0
    assert itm.ports == ["", ""]
    assert itm.pw_values == {"administrator": None, "screen": None}
    planned = "\n".join(dry.planned)
    assert SECRET not in planned
    assert SECRET not in strings(itm)
    assert "administrator password = ***" in planned
    assert "screen password = ***" in planned
    for label in (
        "register indoor units with OU-1",
        "save the leakage check schedule of OU-1",
        "copy the leakage check schedule of OU-1",
        "start the leakage check of OU-1",
        "run the layout setup",
        "set R32 output port 1",
        "enable the administrator password",
    ):
        assert label in planned
    assert itm.open_screens == set()


async def test_read_only_refuses(itm: FakeServiceEditITM) -> None:
    """A read-only client refuses before anything is sent, also in a dry run."""
    for kw in ({"read_only": True}, {"read_only": True, "dry_run": True}):
        ro = client(itm, **kw)
        calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = [
            ("set_leakage_check_indoor_units", (SERVICE, "OU-1", ()), {}),
            ("set_leakage_check_schedule", (SERVICE, "OU-1", 1), {"at": "1:00"}),
            ("copy_leakage_check_schedule", (SERVICE, "OU-1", ("OU-2",)), {}),
            ("start_leakage_check", (SERVICE, "OU-1"), {}),
            ("stop_leakage_check", (SERVICE, "OU-1"), {}),
            ("run_layout_setup", (SERVICE,), {}),
            ("set_r32_output_port", (SERVICE, 1, None), {}),
            ("set_password_setting", ("screen",), {"enabled": True}),
        ]
        for name, args, kwargs in calls:
            with pytest.raises(ReadOnlyError):
                await getattr(ro, name)(*args, **kwargs)
    assert itm.requests == []
    ro = client(itm, read_only=True)
    for flow, args in (
        (set_registration, (SERVICE, "OU-1", ())),
        (set_schedule_program, (SERVICE, "OU-1", 1)),
        (copy_schedule, (SERVICE, "OU-1", ("OU-2",))),
        (layout_setup_job, (SERVICE,)),
        (set_r32_port, (SERVICE, 1, None)),
    ):
        with pytest.raises(ReadOnlyError):
            await run(itm, flow, *args, c=ro)
    with pytest.raises(ReadOnlyError):
        await run(itm, run_check, SERVICE, "OU-1", start=True, c=ro)
    with pytest.raises(ReadOnlyError):
        await run(itm, set_password, "screen", enabled=True, c=ro)


async def test_guards(itm: FakeServiceEditITM) -> None:
    """Each kind of write needs its own allowance (outside a dry run)."""
    c = client(itm)
    for call, flag in (
        (lambda: c.start_leakage_check(SERVICE, "OU-1"), "allow_leakage_check"),
        (lambda: c.stop_leakage_check(SERVICE, "OU-1"), "allow_leakage_check"),
        (
            lambda: c.set_leakage_check_indoor_units(SERVICE, "OU-1", ()),
            "allow_service_change",
        ),
        (
            lambda: c.set_leakage_check_schedule(SERVICE, "OU-1", 1, at="1:00"),
            "allow_service_change",
        ),
        (
            lambda: c.copy_leakage_check_schedule(SERVICE, "OU-1", ("OU-2",)),
            "allow_service_change",
        ),
        (lambda: c.run_layout_setup(SERVICE), "allow_service_change"),
        (lambda: c.set_r32_output_port(SERVICE, 1, None), "allow_service_change"),
        (
            lambda: c.set_password_setting("screen", password=SECRET),
            "allow_password_change",
        ),
    ):
        with pytest.raises(ItmCommandError, match=flag):
            await call()
    assert itm.requests == []


# -- through the client ---------------------------------------------------------------


async def test_client_flows(
    itm: FakeServiceEditITM, caplog: pytest.LogCaptureFixture
) -> None:
    """Every write is read back in a fresh session; passwords never logged."""
    caplog.set_level(logging.DEBUG)
    c = client(itm)
    lc = await c.fetch_leakage_check(SERVICE, details=True)
    assert len(lc.outdoor_units) == 2
    reg = await c.set_leakage_check_indoor_units(
        SERVICE, "OU-2", (itm.indoor[1],), allow_service_change=True
    )
    assert reg is not None
    assert reg.registered == (itm.indoor[1],)
    schedule = await c.set_leakage_check_schedule(
        SERVICE,
        "OU-1",
        1,
        activation="Enable",
        day=date(2027, 1, 2),
        at="04:30",
        allow_service_change=True,
    )
    assert schedule is not None
    assert schedule.programs[0].activation == "Enable"
    await c.copy_leakage_check_schedule(
        SERVICE, "OU-1", ("OU-2",), allow_service_change=True
    )
    unit = await c.start_leakage_check(SERVICE, "OU-1", allow_leakage_check=True)
    assert unit is not None
    assert unit.name == "OU-1"
    assert itm.outdoors[0].running
    await c.stop_leakage_check(SERVICE, "1-00", allow_leakage_check=True)
    assert not itm.outdoors[0].running
    assert (await c.fetch_layout_setup(SERVICE)).offered is False
    assert await c.run_layout_setup(SERVICE, allow_service_change=True) is True
    ports = await c.set_r32_output_port(
        SERVICE, 1, "Alarm out A", allow_service_change=True
    )
    assert ports is not None
    assert ports.ports[0] == "Alarm out A"
    assert (await c.fetch_r32_output_ports(SERVICE)).ports[0] == "Alarm out A"
    got = await c.set_password_setting(
        "screen", enabled=True, password=SECRET, allow_password_change=True
    )
    assert got == PasswordSettings(administrator=False, screen=True)
    assert await c.fetch_password_settings() == got
    assert itm.pw_values["screen"] == SECRET
    assert SECRET not in caplog.text
    assert SERVICE not in caplog.text
    assert SECRET not in "\n".join(c.planned)


async def test_client_read_back_failures(itm: FakeServiceEditITM) -> None:
    """A change the iTM doesn't show afterwards raises."""
    c = client(itm)
    original = itm._picker_event

    def ignore_ok(screen: int, ev: dict[str, Any]) -> Any:
        if itm.sv_dialog is not None and ev["part"] == 30:
            itm.sv_dialog = None
            return [("DLG_CLOSE", screen, [30, 2], []), *itm._close(screen)]
        return original(screen, ev)

    itm._picker_event = ignore_ok  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="did not register"):
        await c.set_leakage_check_indoor_units(
            SERVICE, "OU-1", (itm.indoor[1],), allow_service_change=True
        )
    itm.outdoors[0].programs[0].activation = 1
    with pytest.raises(ItmCommandError, match="not copied"):
        await c.copy_leakage_check_schedule(
            SERVICE, "OU-1", ("OU-2",), allow_service_change=True
        )
    itm.ports = ["", ""]
    ports_event = itm._ports_event

    def keep(ev: dict[str, Any]) -> Any:
        out = ports_event(ev)
        itm.ports = ["", ""]
        return out

    itm._ports_event = keep  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="R32 output port 1"):
        await c.set_r32_output_port(
            SERVICE, 1, "Alarm out A", allow_service_change=True
        )
    schedule_event = itm._schedule_event

    def no_activation(ev: dict[str, Any]) -> Any:
        if ev["dtype"] == 1:
            return [("UPDATE", 3102, [], [])]
        return schedule_event(ev)

    itm._schedule_event = no_activation  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="program 1"):
        await c.set_leakage_check_schedule(
            SERVICE, "OU-2", 1, activation="Enable", allow_service_change=True
        )
    password_parts = itm._password_parts

    def stuck() -> Any:
        itm.enabled["administrator"] = False
        return password_parts()

    itm._password_parts = stuck  # type: ignore[method-assign]
    itm.pw_values["administrator"] = SECRET
    with pytest.raises(ItmCommandError, match="not enabled"):
        await c.set_password_setting(
            "administrator", enabled=True, allow_password_change=True
        )


async def test_start_read_back_unlisted(itm: FakeServiceEditITM) -> None:
    """An outdoor unit that disappears after the start raises."""
    c = client(itm)
    answer = itm._leakage_answer

    def vanish(dialog: tuple[Any, ...], event: int) -> Any:
        out = answer(dialog, event)
        itm.outdoors.pop(0)
        itm.lk_selected = None
        return out

    itm._leakage_answer = vanish  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="no longer listed"):
        await c.start_leakage_check(SERVICE, "OU-1", allow_leakage_check=True)


async def test_screens_that_do_not_open(itm: FakeServiceEditITM) -> None:
    """A button the iTM answers with INFO, or that opens nothing, raises."""
    c = client(itm)
    async with c._session() as s:
        with pytest.raises(ItmCommandError, match="refused"):
            await service_edit._open_menu_button(s, 85, 3100, "leakage check")
        with pytest.raises(ItmCommandError, match="did not open"):
            await service_edit._open_menu_button(s, 1, 3100, "nothing")
        await service_edit._open_leakage(s, SERVICE)
        await service_edit._select_outdoor(s, "OU-1")
        with pytest.raises(ItmCommandError, match="did not open"):
            await service_edit._open_sub(s, 3100, 27, 3104, "type")
        s.state.screens[1900] = {}
        with pytest.raises(ItmCommandError, match="no password input"):
            await service_edit._change_password(s, "screen", SECRET)
        await s.close_menu()


# -- CLI ------------------------------------------------------------------------------


def cli(*args: str) -> None:
    """Run the CLI against the simulator of CliClient."""
    itm_cli.main(["--host", "fake-host", "--experimental", *args])


@pytest.fixture
def cli_itm(
    itm: FakeServiceEditITM, monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> FakeServiceEditITM:
    """Route the CLI to this simulator, with the service password set."""
    monkeypatch.setattr(CliClient, "itm", itm, raising=False)
    monkeypatch.setattr(CliClient, "sigterm_at", None, raising=False)
    monkeypatch.setattr(CliClient, "progress_interval", 0, raising=False)
    monkeypatch.setattr(itm_cli, "ItmClient", CliClient)
    monkeypatch.setattr(itm_cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("ITM_USERNAME", "user")
    monkeypatch.setenv("ITM_PASSWORD", "secret")

    async def get(_self: CliClient, path: str) -> bytes:
        return itm.files[path]

    monkeypatch.setattr(CliClient, "_get_bytes", get)
    monkeypatch.setenv("ITM_SERVICE_PASSWORD", SERVICE)
    return itm


def test_cli_leakcheck(
    cli_itm: FakeServiceEditITM,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`itm leakcheck`: show, details, results, export, writes behind flags."""
    itm = cli_itm
    cli("leakcheck")
    out = capsys.readouterr().out
    assert "OU-1" in out
    assert "1-01" in out
    cli("leakcheck", "details")
    assert "registered" in capsys.readouterr().out
    cli("leakcheck", "results")
    assert "0.2" in capsys.readouterr().out
    target = tmp_path / "lc.csv"
    cli("leakcheck", "export", "--out", str(target))
    assert target.read_bytes().startswith(b"intelligent")
    with pytest.raises(SystemExit, match="allow_leakage_check"):
        cli("leakcheck", "start", "OU-1")
    cli("--dry-run", "leakcheck", "start", "OU-1")
    assert "would send" in capsys.readouterr().out
    cli("leakcheck", "start", "OU-1", "--allow-leakage-check")
    assert itm.outdoors[0].running
    cli("leakcheck", "stop", "OU-1", "--allow-leakage-check")
    assert not itm.outdoors[0].running
    cli(
        "leakcheck",
        "register",
        "OU-2",
        "--indoor",
        itm.indoor[1],
        "--allow-service-change",
    )
    assert itm.outdoors[1].registered == [itm.indoor[1]]
    cli(
        "leakcheck",
        "schedule",
        "OU-2",
        "--program",
        "1",
        "--activation",
        "Enable",
        "--date",
        "2027-01-02",
        "--time",
        "05:00",
        "--allow-service-change",
    )
    assert itm.outdoors[1].programs[0].at == (5, 0)
    assert "Enable" in capsys.readouterr().out
    cli("leakcheck", "copy", "OU-2", "--to", "OU-1", "--allow-service-change")
    assert itm.outdoors[0].programs[0].at == (5, 0)
    with pytest.raises(SystemExit, match="outdoor unit"):
        cli("leakcheck", "start")
    with pytest.raises(SystemExit, match="--program"):
        cli("leakcheck", "schedule", "OU-1")
    with pytest.raises(SystemExit, match="--to"):
        cli("leakcheck", "copy", "OU-1")
    monkeypatch.delenv("ITM_SERVICE_PASSWORD")
    with pytest.raises(SystemExit, match="ITM_SERVICE_PASSWORD"):
        cli("leakcheck")


def test_cli_service_extensions(
    cli_itm: FakeServiceEditITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """`itm service layout|run-layout|r32-ports|r32-port`."""
    itm = cli_itm
    cli("service", "layout")
    assert "layout setup: offered no" in capsys.readouterr().out
    cli("service", "run-layout", "--allow-service-change")
    assert itm.layout_jobs == 1
    cli("service", "r32-ports")
    assert "Alarm out B" in capsys.readouterr().out
    cli("service", "r32-port", "--port", "2", "--value", "Alarm out B",
        "--allow-service-change")  # fmt: skip
    assert itm.ports[1] == "Alarm out B"
    cli("service", "r32-port", "--port", "2", "--clear", "--allow-service-change")
    assert itm.ports[1] == ""
    with pytest.raises(SystemExit, match="--value or --clear"):
        cli("service", "r32-port", "--port", "1")
    cli("--dry-run", "service", "run-layout")
    assert "would send" in capsys.readouterr().out
    cli("service")
    assert "time zone" in capsys.readouterr().out


def test_cli_password(
    cli_itm: FakeServiceEditITM,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`itm password`: the new password only from the environment, never printed."""
    itm = cli_itm
    cli("password")
    assert "administrator password: off" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="ITM_NEW_PASSWORD"):
        cli("password", "change", "screen", "--allow-password-change")
    with pytest.raises(SystemExit, match="which password"):
        cli("password", "enable")
    monkeypatch.setenv("ITM_NEW_PASSWORD", SECRET)
    cli("--dry-run", "password", "enable", "screen")
    out = capsys.readouterr().out
    assert "***" in out
    assert SECRET not in out
    with pytest.raises(SystemExit, match="allow_password_change"):
        cli("password", "enable", "screen")
    cli("password", "enable", "screen", "--allow-password-change")
    out = capsys.readouterr().out
    assert SECRET not in out
    assert "screen password: on" in out
    assert itm.pw_values["screen"] == SECRET
    monkeypatch.setenv("ITM_NEW_PASSWORD", "4n0ther")
    cli("password", "change", "screen", "--allow-password-change")
    assert itm.pw_values["screen"] == "4n0ther"
    monkeypatch.delenv("ITM_NEW_PASSWORD")
    cli("password", "disable", "screen", "--allow-password-change")
    assert not itm.enabled["screen"]
