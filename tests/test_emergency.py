# pylint: disable=protected-access,redefined-outer-name
"""Emergency stop programs: arm, add, copy, rename, delete, settings, release."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import EmergencyStopSettings, ItmCommandError, ReadOnlyError
from daikin_itm import cli as itm_cli
from daikin_itm.emergency import (
    copy_program,
    create_program,
    delete_program,
    read_active_stops,
    read_program_settings,
    read_programs,
    release,
    rename_program,
    set_program_enabled,
    set_program_settings,
)

from .fake_emergency import FakeEmergencyITM, Program
from .test_cli import CliClient
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@pytest.fixture
def itm() -> FakeEmergencyITM:
    """Return a simulated iTM with the dealer option and a second program."""
    fake = FakeEmergencyITM()
    fake.programs.append(Program("Fire alarm", inputs=[fake.points[0]]))
    return fake


async def run(
    itm: FakeEmergencyITM,
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


def writes(itm: FakeEmergencyITM) -> list[tuple[int, int]]:
    """Return the requests that would change something (OK, names, radios...)."""
    persisting = {(200, 4), (200, 5), (200, 12), (200, 13), (202, 30), (201, 4)}
    return [
        (r["screen"], r["part"])
        for r in itm.requests
        if (r["screen"], r["part"]) in persisting and r["event"] == 1
    ]


async def test_read_programs(itm: FakeEmergencyITM) -> None:
    """The radios tell whether each program is armed."""
    programs = await run(itm, read_programs)
    assert [(p.name, p.enabled) for p in programs] == [
        ("Program 1", True),
        ("Fire alarm", False),
    ]
    assert itm.open_screens == set()


async def test_enable_disable(itm: FakeEmergencyITM) -> None:
    """A radio arms or disarms; the state it already has sends nothing."""
    assert await run(itm, set_program_enabled, "Fire alarm", enabled=True) is True
    assert itm.program("Fire alarm").enabled
    before = len(writes(itm))
    assert await run(itm, set_program_enabled, "Fire alarm", enabled=True) is True
    assert len(writes(itm)) == before
    assert await run(itm, set_program_enabled, "Fire alarm", enabled=False) is True
    assert not itm.program("Fire alarm").enabled
    with pytest.raises(ItmCommandError, match="not found once"):
        await run(itm, set_program_enabled, "Nowhere", enabled=True)
    assert itm.open_screens == set()


async def test_create_rename_copy_delete(itm: FakeEmergencyITM) -> None:
    """Add, rename, copy and delete a program through its name dialogs."""
    assert await run(itm, create_program, "Program 2") is True
    assert itm.program("Program 2").enabled is False
    with pytest.raises(ItmCommandError, match="exists already"):
        await run(itm, create_program, "Program 2")
    with pytest.raises(ItmCommandError, match="1-20 characters"):
        await run(itm, create_program, "x" * 21)
    assert await run(itm, rename_program, "Program 2", "Program 3") is True
    assert [p.name for p in itm.programs][-1] == "Program 3"
    assert await run(itm, copy_program, "Fire alarm", "Fire copy") is True
    assert itm.program("Fire copy").inputs == itm.program("Fire alarm").inputs
    assert await run(itm, delete_program, "Program 3") is True
    assert "Program 3" not in [p.name for p in itm.programs]
    itm.program("Fire copy").enabled = True
    with pytest.raises(ItmCommandError, match="disable the program"):
        await run(itm, delete_program, "Fire copy")
    assert itm.open_screens == set()


async def test_built_in_program_refused(itm: FakeEmergencyITM) -> None:
    """The built-in program is never renamed, copied, deleted or edited."""
    for flow, args in (
        (rename_program, ("Program 1", "Other")),
        (copy_program, ("Program 1", "Other")),
        (delete_program, ("Program 1",)),
        (read_program_settings, ("Program 1",)),
        (set_program_settings, ("Program 1",)),
    ):
        with pytest.raises(ItmCommandError, match="built-in program"):
            await run(itm, flow, *args)
    assert [p.name for p in itm.programs] == ["Program 1", "Fire alarm"]
    assert itm.open_screens == set()


async def test_without_dealer_option() -> None:
    """Without the dealer option the edit group is hidden: no add, no edits."""
    itm = FakeEmergencyITM(dealer_option=False)
    with pytest.raises(ItmCommandError, match="dealer option"):
        await run(itm, create_program, "Program 2")
    with pytest.raises(ItmCommandError, match="built-in program"):
        await run(itm, rename_program, "Program 1", "Other")
    assert await run(itm, set_program_enabled, "Program 1", enabled=False) is True
    assert not itm.program("Program 1").enabled


async def test_settings(itm: FakeEmergencyITM) -> None:
    """Read and change a disarmed program's points, release mode and stop."""
    first, second, third = itm.points[:3]
    settings = await run(itm, read_program_settings, "Fire alarm")
    assert settings == EmergencyStopSettings(
        name="Fire alarm",
        inputs=(first,),
        outputs=(),
        release_mode="Auto",
        release_mode_options=("Auto", "Manual"),
        stop="Stop",
        stop_options=("Stop", "Stop and lock"),
    )
    assert (
        await run(
            itm,
            set_program_settings,
            "Fire alarm",
            inputs=(second,),
            outputs=(first, third),
            release_mode="manual",
            stop="Stop and lock",
        )
        is True
    )
    program = itm.program("Fire alarm")
    assert program.inputs == [second]
    assert program.outputs == [first, third]
    assert (program.release_mode, program.stop) == (1, 1)
    with pytest.raises(ItmCommandError, match="not offered"):
        await run(itm, set_program_settings, "Fire alarm", inputs=("nope",))
    with pytest.raises(ItmCommandError, match="not offered"):
        await run(itm, set_program_settings, "Fire alarm", stop="Explode")
    assert program.inputs == [second]
    assert itm.open_screens == set()


async def test_settings_of_an_armed_program(itm: FakeEmergencyITM) -> None:
    """An armed program's settings are refused (write) or not shown (read)."""
    itm.program("Fire alarm").enabled = True
    with pytest.raises(ItmCommandError, match="disable it first"):
        await run(itm, set_program_settings, "Fire alarm", stop="Stop")
    with pytest.raises(ItmCommandError, match="did not open"):
        await run(itm, read_program_settings, "Fire alarm")
    assert itm.open_screens == set()


async def test_active_and_release(itm: FakeEmergencyITM) -> None:
    """No stop: nothing is clicked; an active stop is listed and released."""
    assert await run(itm, read_active_stops) == ()
    with pytest.raises(ItmCommandError, match="no emergency stop is active"):
        await run(itm, release, "Fire alarm")
    itm.start_stop("Fire alarm")
    assert await run(itm, read_active_stops) == ("Fire alarm",)
    with pytest.raises(ItmCommandError, match="not found once"):
        await run(itm, release, "Program 1")
    assert await run(itm, release, "Fire alarm") is True
    assert itm.active == []
    assert not itm.emergency_stop_active
    assert itm.open_screens == set()


async def test_dry_run_sends_no_writes(itm: FakeEmergencyITM) -> None:
    """A dry run plans every write and changes nothing."""
    dry = FakeClient(itm, dry_run=True)
    itm.start_stop("Fire alarm")
    for flow, args, kwargs in (
        (set_program_enabled, ("Fire alarm",), {"enabled": True}),
        (create_program, ("Program 2",), {}),
        (rename_program, ("Fire alarm", "Other"), {}),
        (copy_program, ("Fire alarm", "Other"), {}),
        (delete_program, ("Fire alarm",), {}),
        (set_program_settings, ("Fire alarm",), {"stop": "Stop and lock"}),
        (release, ("Fire alarm",), {}),
    ):
        assert await run(itm, flow, *args, client=dry, **kwargs) is False
    assert writes(itm) == []
    assert len(dry.planned) == 7
    assert dry.planned[1] == (
        "create emergency stop program Program 2: screen 200 part 12 event 1 "
        "'Program 2'"
    )
    assert [p.name for p in itm.programs] == ["Program 1", "Fire alarm"]
    assert itm.program("Fire alarm").stop == 0
    assert itm.active == ["Fire alarm"]
    assert itm.open_screens == set()


async def test_read_only_refuses(itm: FakeEmergencyITM) -> None:
    """A read-only client refuses every write before it is sent."""
    ro = FakeClient(itm, read_only=True)
    with pytest.raises(ReadOnlyError):
        await run(itm, create_program, "Program 2", client=ro)
    with pytest.raises(ReadOnlyError):
        await ro.delete_emergency_stop_program("Fire alarm")
    with pytest.raises(ReadOnlyError):
        await ro.set_emergency_stop_enabled(
            "Fire alarm", enabled=True, allow_safety_change=True
        )
    with pytest.raises(ReadOnlyError):
        await ro.release_emergency_stop("Fire alarm")
    assert writes(itm) == []


# -- through the client ---------------------------------------------------------------


async def test_client_guard(itm: FakeEmergencyITM) -> None:
    """Arming or disarming needs allow_safety_change; a dry run doesn't."""
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="allow_safety_change"):
        await c.set_emergency_stop_enabled("Program 1", enabled=False)
    assert itm.program("Program 1").enabled
    assert itm.requests == []
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_emergency_stop_enabled("Program 1", enabled=False) is None
    assert itm.program("Program 1").enabled
    program = await c.set_emergency_stop_enabled(
        "Fire alarm", enabled=True, allow_safety_change=True
    )
    assert program is not None
    assert program.enabled


async def test_client_programs(itm: FakeEmergencyITM) -> None:
    """Create, rename, copy, configure and delete, each read back."""
    c = FakeClient(itm)
    created = await c.create_emergency_stop_program("Program 2")
    assert created is not None
    assert not created.enabled
    renamed = await c.rename_emergency_stop_program("Program 2", "Program 3")
    assert renamed is not None
    assert renamed.name == "Program 3"
    copy = await c.copy_emergency_stop_program("Program 3", "Program 4")
    assert copy is not None
    settings = await c.set_emergency_stop_settings(
        "Program 4",
        inputs=[itm.points[0]],
        outputs=itm.points[1:3],
        stop="Stop and lock",
    )
    assert settings is not None
    assert settings.inputs == (itm.points[0],)
    assert settings.stop == "Stop and lock"
    assert (await c.fetch_emergency_stop_settings("Program 4")) == settings
    await c.delete_emergency_stop_program("Program 4")
    await c.delete_emergency_stop_program("Program 3")
    assert [p.name for p in await c.fetch_emergency_stop()] == [
        "Program 1",
        "Fire alarm",
    ]
    dry = FakeClient(itm, dry_run=True)
    assert await dry.create_emergency_stop_program("Other") is None
    assert await dry.rename_emergency_stop_program("Fire alarm", "Other") is None
    assert await dry.copy_emergency_stop_program("Fire alarm", "Other") is None
    assert await dry.set_emergency_stop_settings("Fire alarm", stop="Stop") is None
    await dry.delete_emergency_stop_program("Fire alarm")
    assert len(itm.programs) == 2


async def test_client_release(itm: FakeEmergencyITM) -> None:
    """Active stops are read and released; never started."""
    c = FakeClient(itm)
    assert await c.fetch_active_emergency_stops() == ()
    itm.start_stop("Fire alarm")
    assert await c.fetch_active_emergency_stops() == ("Fire alarm",)
    with pytest.raises(ItmCommandError, match="allow_safety_change"):
        await c.release_emergency_stop("Fire alarm")
    assert await c.fetch_active_emergency_stops() == ("Fire alarm",)
    await c.release_emergency_stop("Fire alarm", allow_safety_change=True)
    assert await c.fetch_active_emergency_stops() == ()


class _Unchanged(FakeEmergencyITM):
    """An iTM that confirms everything but changes nothing."""

    def _main_event(self, ev: dict[str, Any]) -> Any:
        if ev["part"] in {12, 13, 30} and ev["event"] == 1:
            ev = {**ev, "event": 2}
        return super()._main_event(ev)

    def _settings_event(self, ev: dict[str, Any]) -> Any:
        if ev["part"] == 30 and ev["event"] == 1:
            self.open_screens.discard(202)
            return [("DLG_CLOSE", 202, [30, 2], []), ("CLOSE", 202, [], [])]
        return super()._settings_event(ev)

    def _release_event(self, ev: dict[str, Any]) -> Any:
        if ev["part"] == 4 and ev["event"] == 1:
            ev = {**ev, "event": 2}
        return super()._release_event(ev)


async def test_client_read_back_failures() -> None:
    """A change the iTM did not take raises after the fresh read."""
    itm = _Unchanged()
    itm.programs.append(Program("Fire alarm"))
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="did not enable"):
        await c.set_emergency_stop_enabled(
            "Fire alarm", enabled=True, allow_safety_change=True
        )
    with pytest.raises(ItmCommandError, match="did not add"):
        await c.create_emergency_stop_program("Program 2")
    with pytest.raises(ItmCommandError, match="did not rename"):
        await c.rename_emergency_stop_program("Fire alarm", "Other")
    with pytest.raises(ItmCommandError, match="did not make the copy"):
        await c.copy_emergency_stop_program("Fire alarm", "Other")
    with pytest.raises(ItmCommandError, match="did not delete"):
        await c.delete_emergency_stop_program("Fire alarm")
    with pytest.raises(ItmCommandError, match="did not take the stop"):
        await c.set_emergency_stop_settings("Fire alarm", stop="Stop and lock")
    itm.start_stop("Fire alarm")
    with pytest.raises(ItmCommandError, match="did not release"):
        await c.release_emergency_stop("Fire alarm", allow_safety_change=True)


# -- the CLI --------------------------------------------------------------------------


def cli(*args: str) -> None:
    """Run the CLI against the simulator of CliClient."""
    itm_cli.main(["--host", "fake-host", "--experimental", *args])


def test_cli(
    itm: FakeEmergencyITM,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Any,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Every emgstop action, the guard and a dry run."""
    monkeypatch.setattr(CliClient, "itm", itm, raising=False)
    monkeypatch.setattr(CliClient, "sigterm_at", None, raising=False)
    monkeypatch.setattr(itm_cli, "ItmClient", CliClient)
    monkeypatch.setattr(itm_cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("ITM_USERNAME", "user")
    monkeypatch.setenv("ITM_PASSWORD", "secret")
    cli("emgstop")
    assert capsys.readouterr().out.split() == [
        "Program",
        "1",
        "enabled",
        "Fire",
        "alarm",
        "disabled",
    ]
    cli("emgstop", "settings", "Fire alarm")
    out = capsys.readouterr().out
    assert f"inputs: {itm.points[0]}" in out
    assert "release mode: Auto (options: Auto, Manual)" in out
    with pytest.raises(SystemExit, match="allow_safety_change"):
        cli("emgstop", "enable", "Fire alarm")
    cli("emgstop", "enable", "Fire alarm", "--allow-safety-change")
    assert "Fire alarm: enable done" in capsys.readouterr().out
    cli("emgstop", "disable", "Fire alarm", "--allow-safety-change")
    cli("emgstop", "add", "Program 2")
    cli("emgstop", "rename", "Program 2", "Program 3")
    cli("emgstop", "copy", "Program 3", "Program 4")
    cli("emgstop", "delete", "Program 4")
    capsys.readouterr()
    cli("emgstop", "set", "Program 3", "--output", itm.points[1], "--stop", "Stop")
    assert f"outputs: {itm.points[1]}" in capsys.readouterr().out
    assert [p.name for p in itm.programs] == ["Program 1", "Fire alarm", "Program 3"]
    cli("emgstop", "active")
    assert capsys.readouterr().out.strip() == "active: none"
    itm.start_stop("Fire alarm")
    cli("emgstop", "active")
    assert capsys.readouterr().out.strip() == "active: Fire alarm"
    cli("--dry-run", "emgstop", "release", "Fire alarm")
    out = capsys.readouterr().out
    assert "dry run, would send:" in out
    assert "release emergency stop Fire alarm" in out
    assert itm.active == ["Fire alarm"]
    cli("--dry-run", "emgstop", "set", "Program 3", "--stop", "Stop and lock")
    assert "save emergency stop program Program 3" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="allow_safety_change"):
        cli("emgstop", "release", "Fire alarm")
    assert itm.active == ["Fire alarm"]
    cli("emgstop", "release", "Fire alarm", "--allow-safety-change")
    assert itm.active == []
    with pytest.raises(SystemExit, match="error: the client is read-only"):
        cli("--read-only", "emgstop", "add", "Program 5")
    with pytest.raises(SystemExit, match="name is needed"):
        cli("emgstop", "delete")
    with pytest.raises(SystemExit, match="new name is needed"):
        cli("emgstop", "rename", "Program 3")
