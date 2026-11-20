"""``itm check``: the read-only checks, run against the simulated iTM."""

# pylint: disable=protected-access

from __future__ import annotations

import time
from dataclasses import dataclass, replace
from datetime import timedelta
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import aiohttp
import pytest

from daikin_itm import ItmClient, ItmCommandError, check_cli
from daikin_itm import cli as itm_cli
from daikin_itm import client as client_module
from daikin_itm.check_cli import (
    _MISSING,
    EXCLUDED,
    LEFT_OUT,
    PLANS,
    ZONE_SPECIFIC,
    Derived,
    Read,
    argument_free_reads,
    compare,
    is_empty,
)
from daikin_itm.demand import DemandControl, DemandStatus
from daikin_itm.exports import Download

from .fake_itm import FakeITM
from .test_cli import CliClient
from .test_shared_session import READS

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

HOST = "fake-host"
INFO_ONLY = ["--only", "fetch_info"]


class CheckClient(CliClient):
    """The CLI's test client, remembering how the command built it."""

    made: ClassVar[list[dict[str, Any]]] = []
    progress_interval = 0

    def __init__(self, host: str, username: str, password: str, **kw: Any) -> None:
        """Create the client and record its options."""
        super().__init__(host, username, password, **kw)
        self.made.append(kw)


@pytest.fixture(name="itm", autouse=True)
def fixture_itm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeITM:
    """Route the CLI to a fresh simulator, with its cache in a temp directory."""
    fake = FakeITM()
    monkeypatch.setattr(CliClient, "itm", fake, raising=False)
    monkeypatch.setattr(CliClient, "sigterm_at", None)
    monkeypatch.setattr(CheckClient, "made", [])
    monkeypatch.setattr(itm_cli, "ItmClient", CheckClient)
    monkeypatch.setattr(itm_cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(itm_cli, "UNITS_CACHE", tmp_path / "cache" / "units.json")
    monkeypatch.setenv("ITM_USERNAME", "user")
    monkeypatch.setenv("ITM_PASSWORD", "secret")
    return fake


def check(*args: str) -> None:
    """Run ``itm check`` against the simulator."""
    itm_cli.main(["--host", HOST, "--zone", "Building/Indoor units", "check", *args])


def lines(capsys: pytest.CaptureFixture[str]) -> dict[str, str]:
    """Return what was printed by the first word of each line."""
    out = capsys.readouterr().out.splitlines()
    return {line.split()[0]: line for line in out if line and line[0] != " "}


def vary_fetch_info(
    monkeypatch: pytest.MonkeyPatch, change: Callable[[Any, int], Any]
) -> None:
    """Make every fetch_info differ from the one before, as `change` says."""
    original = ItmClient.fetch_info
    calls: list[int] = []

    async def fetch_info(self: ItmClient) -> Any:
        calls.append(1)
        return change(await original(self), len(calls))

    monkeypatch.setattr(CheckClient, "fetch_info", fetch_info)


@pytest.mark.parametrize("command", ["shared-session", "reads"])
def test_list_names_the_reads_without_contacting_the_itm(
    command: str,
    itm: FakeITM,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--list prints the names and needs neither host nor credentials."""
    monkeypatch.delenv("ITM_USERNAME")
    monkeypatch.delenv("ITM_PASSWORD")
    monkeypatch.delenv("ITM_HOST", raising=False)
    itm_cli.main(["check", command, "--list"])
    out = capsys.readouterr().out.splitlines()
    names = {line.split()[0] for line in out}
    assert {
        "fetch_points",
        "fetch_units",
        "fetch_thermostat_status",
        "fetch_config",
        "fetch_info",
        "fetch_unit_settings",
        "fetch_schedules",
        "fetch_schedule",
        "fetch_schedule_day",
        "fetch_history[pages]",
        "fetch_points[thermostat]",
        "fetch_demand_control",
        "fetch_energy_navigator",
        "fetch_energy_navigator[more]",
    } <= names
    assert "fetch_schedule  (one per schedule program, found by fetch_schedules)" in out
    assert {"fetch_password_settings", "export_setup"} <= set(LEFT_OUT)
    for name in LEFT_OUT:
        assert name not in names
        assert any(line.startswith(f"not included: {name} ") for line in out)
    excluded = any(line.startswith("not included: fetch_layout") for line in out)
    assert excluded == (command == "shared-session")
    assert ("fetch_own_settings" in names) == (command == "reads")
    assert not itm.requests


@pytest.mark.parametrize("command", ["shared-session", "reads"])
def test_plan_covers_the_argument_free_reads(command: str) -> None:
    """Every read that needs no arguments is in the plan, bar the ones left out."""
    plan = PLANS[command]
    left_out = set(LEFT_OUT) | set(ZONE_SPECIFIC)
    if command == "shared-session":
        left_out |= set(EXCLUDED)
    planned = {item.name for item in plan if isinstance(item, Read)}
    assert planned == set(argument_free_reads()) - left_out
    assert left_out <= set(argument_free_reads())
    assert {item.source for item in plan if isinstance(item, Derived)} <= planned


def test_no_planned_read_ends_the_shared_session() -> None:
    """A read that ends the shared session by design is left out of the plan."""
    for item in PLANS["shared-session"]:
        if isinstance(item, Read) and item.name in READS:
            reads = [r for r in READS[item.name] if not r.args]
            plain = [r for r in reads if not r.kwargs] or reads
            assert not any(r.ends_session for r in plain), item.name
    assert all(READS[name][0].ends_session for name in EXCLUDED if name in READS)


def test_shared_session_is_the_same_everywhere_in_the_simulator(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reads give the same alone and in a block, and no value is printed."""
    check(
        "shared-session",
        "--only",
        "fetch_points",
        "--only",
        "fetch_units",
        "--only",
        "fetch_thermostat_status",
        "--only",
        "fetch_config",
        "--only",
        "fetch_info",
        "--only",
        "fetch_unit_settings",
        "--only",
        "fetch_schedules",
        "--only",
        "fetch_schedule",
    )
    out = capsys.readouterr().out
    assert "fetch_schedule[2]  " in out
    assert "fetch_unit_settings[1]" in out
    assert "fetch_points[thermostat]  " in out
    assert "13 reads: 13 same, 0 different, 0 failed, 0 not in one session;" in out
    assert "logins used: " in out
    assert "logins 1 alone, 1 in block" in out
    for name in ("Always", "Office", "Building", "AC 05"):
        assert name not in out
    assert "read_at ignored" in out
    assert CheckClient.made
    assert all(r["read_only"] for r in CheckClient.made)


def test_volatile_fields_are_ignored_and_named(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The clock changing between reads is not a difference, and the line says so."""
    vary_fetch_info(
        monkeypatch,
        lambda info, n: replace(
            info,
            clock=info.clock + timedelta(seconds=n),
            read_at=info.read_at + timedelta(seconds=n),
        ),
    )
    check("shared-session", *INFO_ONLY)
    line = lines(capsys)["fetch_info"]
    assert " same " in line
    assert line.endswith("clock, read_at ignored")


def test_a_difference_fails_and_shows_values_only_on_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A read that changes between sessions is reported; --show says how."""
    vary_fetch_info(monkeypatch, lambda info, n: replace(info, version=f"secret-{n}"))
    with pytest.raises(SystemExit) as stop:
        check("shared-session", *INFO_ONLY)
    assert stop.value.code == 1
    out = capsys.readouterr().out
    assert "different" in out
    assert "in a block differs from alone" in out
    assert "1 reads: 0 same, 1 different, 0 failed, 0 not in one session;" in out
    assert "secret" not in out

    vary_fetch_info(monkeypatch, lambda info, n: replace(info, version=f"secret-{n}"))
    with pytest.raises(SystemExit):
        check("shared-session", *INFO_ONLY, "--show")
    out = capsys.readouterr().out
    assert ".version: alone | in block | again: 'secret-" in out


@pytest.mark.parametrize(
    ("change", "said"),
    [
        (lambda n: "x" if n in {1, 4} else "y", "the first read in a block differs"),
        (lambda n: "x" if n in {1, 3} else "y", "the second read in a block differs"),
        (str, "in a block differs from alone"),
    ],
)
def test_the_line_tells_which_read_of_the_block_differs(
    change: Callable[[int], str],
    said: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Calls: alone, the read before it (fetch_info too), then the block's two."""
    vary_fetch_info(monkeypatch, lambda info, n: replace(info, version=change(n)))
    with pytest.raises(SystemExit):
        check("shared-session", *INFO_ONLY)
    assert said in capsys.readouterr().out


def test_a_failing_read_is_reported_and_the_run_goes_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An exception in one read is reported on its line and does not stop the rest."""

    async def broken(_self: ItmClient) -> Any:
        msg = "no way\nat all"
        raise ItmCommandError(msg)

    monkeypatch.setattr(CheckClient, "fetch_info", broken)
    with pytest.raises(SystemExit) as stop:
        check("shared-session", "--only", "fetch_info", "--only", "fetch_language")
    assert stop.value.code == 1
    out = lines(capsys)
    assert out["fetch_info"].split(None, 2)[1] == "error"
    assert out["fetch_info"].endswith(
        "alone, in block, in block again: ItmCommandError"
    )
    assert out["fetch_language"].split()[1] == "split"
    assert "2 reads: 0 same, 0 different, 1 failed, 1 not in one session" in out["2"]
    with pytest.raises(SystemExit):
        check("shared-session", "--only", "fetch_info", "--show")
    assert (
        "in block again: ItmCommandError: no way at all" in lines(capsys)["fetch_info"]
    )


@pytest.mark.parametrize(
    ("failing_call", "stage"), [(1, "alone"), (2, "in block"), (3, "in block again")]
)
def test_a_read_that_fails_in_one_stage_is_reported(
    failing_call: int,
    stage: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The line names the stage that failed."""
    original = ItmClient.fetch_language
    calls: list[int] = []

    async def fetch_language(self: ItmClient) -> Any:
        calls.append(1)
        if len(calls) == failing_call:
            msg = "gone"
            raise ItmCommandError(msg)
        return await original(self)

    monkeypatch.setattr(CheckClient, "fetch_language", fetch_language)
    with pytest.raises(SystemExit):
        check("shared-session", "--only", "fetch_language")
    assert f"{stage}: ItmCommandError" in capsys.readouterr().out


def test_skip_and_unknown_names(capsys: pytest.CaptureFixture[str]) -> None:
    """--skip leaves a read out; a name that is not a read is refused."""
    check(
        "reads",
        "--only",
        "fetch_info",
        "--only",
        "fetch_language",
        "--skip",
        "fetch_info",
    )
    assert list(lines(capsys)) == ["itm", "fetch_language", "1"]
    with pytest.raises(SystemExit, match="unknown read 'fetch_nothing'"):
        check("reads", "--skip", "fetch_nothing")


def test_a_variant_is_selected_by_its_label(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--only takes the label of one variant as well as the name of the read."""
    check("reads", "--only", "fetch_energy_navigator[more]")
    out = lines(capsys)
    assert "fetch_energy_navigator[more]" in out
    assert "fetch_energy_navigator" not in out


def test_a_derived_read_finds_its_names_without_its_source(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A read per schedule program works with the list of programs left out."""
    check("reads", "--only", "fetch_schedule", "--skip", "fetch_schedules")
    out = lines(capsys)
    assert {"fetch_schedule[1]", "fetch_schedule[2]", "fetch_schedule[3]"} <= set(out)
    assert "fetch_schedules" not in out

    async def fetch_schedules(_self: ItmClient) -> list[Any]:
        return []

    monkeypatch.setattr(CheckClient, "fetch_schedules", fetch_schedules)
    check("reads", "--only", "fetch_schedule")
    assert "none found" in lines(capsys)["fetch_schedule"]


def test_a_derived_read_whose_source_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the programs can't be listed the line says so."""

    async def fetch_schedules(_self: ItmClient) -> list[Any]:
        msg = "not allowed"
        raise ItmCommandError(msg)

    monkeypatch.setattr(CheckClient, "fetch_schedules", fetch_schedules)
    check("reads", "--only", "fetch_schedule")
    assert lines(capsys)["fetch_schedule"].endswith(
        "fetch_schedules failed: ItmCommandError"
    )
    with pytest.raises(SystemExit):
        check("shared-session", "--only", "fetch_schedule")
    assert "fetch_schedules failed" in lines(capsys)["fetch_schedule"]


def test_reads_say_ok_empty_or_the_error(capsys: pytest.CaptureFixture[str]) -> None:
    """Each read is ok, empty or the error it raised; the exit code stays zero."""
    check("reads")
    out = lines(capsys)
    assert out["fetch_info"].split() == ["fetch_info", "ok"]
    assert out["fetch_demand_control"].split() == ["fetch_demand_control", "empty"]
    assert out["fetch_own_settings"].startswith("fetch_own_settings ")
    assert out["fetch_layout"].startswith("fetch_layout ")
    assert not set(LEFT_OUT) & set(out)
    assert out["fetch_energy_navigator[more]"].split()[1:] == ["ItmCommandError"]
    assert "Office" not in "".join(out.values())
    assert CheckClient.made
    assert all(r["read_only"] for r in CheckClient.made)


def test_reads_show_values_on_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """--show prints what a read returned, a long result cut short."""
    check("reads", *INFO_ONLY, "--show")
    assert "    .language = 'English'" in capsys.readouterr().out
    monkeypatch.setattr(check_cli, "SHOWN_LINES", 3)
    check("reads", *INFO_ONLY, "--show")
    assert "    ... 8 more" in capsys.readouterr().out


def test_reads_stop_when_the_login_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected login is the one thing that gives a non-zero exit code."""
    monkeypatch.setenv("ITM_PASSWORD", "wrong")
    with pytest.raises(SystemExit, match=r"^error: login rejected"):
        check("reads")
    with pytest.raises(SystemExit, match=r"^error: login rejected"):
        check("shared-session")


async def _unreachable(_self: ItmClient) -> Any:
    msg = "connection refused"
    raise aiohttp.ClientConnectionError(msg)


def test_an_unreachable_itm_stops_the_run(monkeypatch: pytest.MonkeyPatch) -> None:
    """A connection failure before any read worked is no result for one read."""
    monkeypatch.setattr(CheckClient, "fetch_info", _unreachable)
    with pytest.raises(SystemExit, match="could not reach the iTM"):
        check("reads", *INFO_ONLY)


def test_a_connection_failure_after_a_read_worked_is_that_reads(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Once a read has worked a dropped connection is reported on its line."""
    monkeypatch.setattr(CheckClient, "fetch_language", _unreachable)
    check("reads", "--only", "fetch_info", "--only", "fetch_language")
    out = lines(capsys)
    assert out["fetch_info"].split()[1] == "ok"
    assert out["fetch_language"].split()[1:] == ["ClientConnectionError"]


def test_error_messages_only_with_show(capsys: pytest.CaptureFixture[str]) -> None:
    """An error's message can name zones and units: only --show prints it."""
    itm_cli.main(["--host", HOST, "--zone", "Building/Typo", "check", "reads"])
    out = capsys.readouterr().out
    assert "ZoneNotFound" in out
    assert "Typo" not in out
    assert "Building" not in out
    itm_cli.main(
        ["--host", HOST, "--zone", "Building/Typo", "check", "reads", "--show"]
    )
    assert "Typo" in capsys.readouterr().out


def test_reads_of_a_normal_user_name_the_menu(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """For a normal web user the menu reads fail with MenuNotAvailable, others work."""
    monkeypatch.setattr(CliClient, "itm", FakeITM(normal_user=True))
    itm_cli.main(["--host", HOST, "check", "reads"])
    out = lines(capsys)
    for name in ("fetch_info", "fetch_schedules", "fetch_history", "fetch_system"):
        assert out[name].split()[1:] == ["MenuNotAvailable"]
    assert "fetch_schedules failed: MenuNotAvailable" in out["fetch_schedule"]
    for name in ("fetch_units", "fetch_points", "fetch_thermostat_status"):
        assert out[name].split()[1:] == ["ok"]


def test_live_demand_values_are_ignored(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The demand countdown and measured power change between reads by themselves."""
    calls: list[int] = []

    async def fetch_demand_control(_self: ItmClient) -> DemandControl:
        calls.append(1)
        n = len(calls)
        status = DemandStatus(
            state="On",
            period="Period 1",
            target=None,
            predicted=None,
            sync_method=None,
            remaining=f"{n}:00",
            current=None,
            adjustment=None,
            power_unit="kW",
            graph=(n,),
        )
        return DemandControl(status=status)

    monkeypatch.setattr(CheckClient, "fetch_demand_control", fetch_demand_control)
    check("shared-session", "--only", "fetch_demand_control")
    line = lines(capsys)["fetch_demand_control"]
    assert " same " in line
    assert line.endswith("DemandStatus.graph, DemandStatus.remaining ignored")


def test_a_slow_read_stays_in_one_session(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A read slower than the library's default block limit keeps the one login."""
    clock = [0.0]
    monkeypatch.setattr(
        client_module,
        "time",
        SimpleNamespace(monotonic=lambda: clock[0], time=time.time),
    )
    original = ItmClient.fetch_language

    async def fetch_language(self: ItmClient) -> Any:
        result = await original(self)
        clock[0] += 35
        return result

    monkeypatch.setattr(CheckClient, "fetch_language", fetch_language)
    check("shared-session", "--only", "fetch_language")
    line = lines(capsys)["fetch_language"]
    assert " same " in line
    assert "logins 1 alone, 1 in block" in line


def test_a_read_not_run_in_one_session_is_flagged(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """When the read before it fails in the block, the line is no validation."""
    original = ItmClient.fetch_info

    async def fetch_info(self: ItmClient) -> Any:
        if self._shared() is not None:
            msg = "gone"
            raise ItmCommandError(msg)
        return await original(self)

    monkeypatch.setattr(CheckClient, "fetch_info", fetch_info)
    with pytest.raises(SystemExit) as stop:
        check("shared-session", "--only", "fetch_language")
    assert stop.value.code == 1
    out = lines(capsys)
    assert out["fetch_language"].split()[1] == "split"
    assert "the read before it failed in the block" in out["fetch_language"]
    assert "1 reads: 0 same, 0 different, 0 failed, 1 not in one session;" in out["1"]


def test_days_ahead_and_pages_stay_in_one_session(
    itm: FakeITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """The day overview a day ahead and older history pages step back in the block."""
    itm.history_page_size = 1
    check("shared-session", "--only", "fetch_schedule_day", "--only", "fetch_history")
    out = lines(capsys)
    for label in ("fetch_schedule_day[1]", "fetch_history", "fetch_history[pages]"):
        assert " same " in out[label], label
        assert "logins 1 alone, 1 in block" in out[label]
    assert "5 reads: 5 same, 0 different, 0 failed, 0 not in one session;" in out["5"]


def test_the_logins_are_counted(capsys: pytest.CaptureFixture[str]) -> None:
    """The total is the number of logins the iTM saw."""
    check("shared-session", *INFO_ONLY)
    out = capsys.readouterr().out
    assert "logins used: 2" in out
    assert "logins 1 alone, 1 in block" in out


@dataclass
class Nested:
    """A result with a volatile field in it."""

    items: list[Any]
    room_temperature: float | None = None


def test_compare_leaves_out_what_changes_by_itself() -> None:
    """A volatile field is left out wherever it sits; the others are compared."""
    a = {"u": Nested([1, 2], 20.0), "k": b"abc", "s": {1, 2}}
    b = {"u": Nested([1, 2], 20.5), "k": b"abc", "s": {2, 1}}
    differing, ignored, apart = compare([a, b])
    assert not differing
    assert ignored == {"room_temperature"}
    assert apart == [False, False]

    c = {"u": Nested([1, 3], 20.5), "k": b"abx", "s": {1}}
    differing, _, apart = compare([a, c])
    assert [p for p, _ in differing] == ["['u'].items[1]", "['k']", "['s']"]
    assert apart == [False, True]
    assert "sha256" in differing[1][1][0]

    differing, _, _ = compare([[1], [1, 2]])
    assert differing == [("[1]", [_MISSING, 2])]


def test_empty_means_nothing_was_returned() -> None:
    """None, empty collections and results made of nothing are empty."""
    assert is_empty(None)
    assert is_empty([])
    assert is_empty(Nested([], None))
    assert not is_empty(Nested([], 0.0))
    assert not is_empty(Download("a", b""))
    assert not is_empty(0)
