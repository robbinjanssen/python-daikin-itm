"""The ``itm`` command line tool, run end-to-end against the simulated iTM."""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import signal
import stat
from typing import TYPE_CHECKING, Any, ClassVar

import aiohttp
import pytest

from daikin_itm import Capabilities, ItmClient, ItmError
from daikin_itm import cli as itm_cli
from daikin_itm.cli_common import safe as _safe

from .export_archive import history_archive
from .fake_exports import FakeExportsITM
from .fake_itm import FakeGroup, FakeITM
from .fake_schedule import FakeScheduleITM
from .fake_system_edit import FakeSystemEditITM
from .fake_zones import FakeZonesITM

if TYPE_CHECKING:
    from pathlib import Path

HOST = "fake-host"


class CliClient(ItmClient):
    """ItmClient that talks to the FakeITM of the running test, without delays."""

    itm: ClassVar[FakeITM]
    sigterm_at: ClassVar[int | None] = None  # send SIGTERM on this request number

    def __init__(self, host: str, username: str, password: str, **kw: Any) -> None:
        """Create the client with all delays off."""
        super().__init__(
            host,
            username,
            password,
            commit_delay=0,
            min_write_interval=0,
            confirm_interval=0,
            confirm_timeout=0.5,
            **kw,
        )
        self.created_with = {"host": host, **kw}

    async def _post(self, body: str) -> str:
        """Answer from the simulator (or deliver a SIGTERM when asked to)."""
        await asyncio.sleep(0)
        reply = self.itm.handle(body)
        if len(self.itm.requests) == self.sigterm_at:
            os.kill(os.getpid(), signal.SIGTERM)
            await asyncio.sleep(5)  # cancelled by the signal handler
        return reply


@pytest.fixture(name="itm", autouse=True)
def fixture_itm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeITM:
    """Route the CLI to a fresh simulator, with its cache in a temp directory."""
    fake = FakeITM()
    monkeypatch.setattr(CliClient, "itm", fake, raising=False)
    monkeypatch.setattr(CliClient, "sigterm_at", None)
    monkeypatch.setattr(itm_cli, "ItmClient", CliClient)
    monkeypatch.setattr(itm_cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(itm_cli, "UNITS_CACHE", tmp_path / "cache" / "units.json")
    monkeypatch.setenv("ITM_USERNAME", "user")
    monkeypatch.setenv("ITM_PASSWORD", "secret")
    return fake


def run(*args: str) -> None:
    """Run the CLI against the simulator, for the units in Building/Indoor units.

    With --experimental: these tests cover the writes; test_experimental.py the
    opt-in.
    """
    zone = [] if "--zone" in args else ["--zone", "Building/Indoor units"]
    itm_cli.main(["--host", HOST, "--experimental", *zone, *args])


def detail_requests(itm: FakeITM) -> int:
    """Count how often the details screen was opened (to read capabilities)."""
    return sum(1 for r in itm.requests if r["screen"] == 4 and r["part"] == 7)


def test_status_reads_and_caches_capabilities(
    itm: FakeITM, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The first status reads descriptions and caches them, the next one reuses them."""
    run("status")
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == [
        "unit",
        "description",
        "power",
        "mode",
        "fan",
        "setpoint",
        "range",
        "room",
        "error",
    ]
    assert out[1].split() == [
        "1:1-00",
        "Office",
        "Start",
        "Heat",
        "medium",
        "21",
        "19-24",
        "21.0",
    ]
    assert out[3].split()[:3] == ["1:1-09", "-", "Stop"]
    assert detail_requests(itm) == 3
    cache = json.loads((tmp_path / "cache" / "units.json").read_text(encoding="utf-8"))
    assert cache[HOST]["1:1-04"]["description"] == "AC 05"
    assert stat.S_IMODE((tmp_path / "cache").stat().st_mode) == 0o700

    run("status")
    assert "AC 05" in capsys.readouterr().out
    assert detail_requests(itm) == 3  # from the cache

    run("status", "--refresh")
    assert "Office" in capsys.readouterr().out
    assert detail_requests(itm) == 6


def test_status_refreshes_cache_for_new_units(
    itm: FakeITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """A unit missing from the cache (added or renamed) triggers a refresh."""
    run("status")
    itm.units[0].name = "1:1-01"
    run("status")
    assert "1:1-01" in capsys.readouterr().out
    assert detail_requests(itm) == 6


@pytest.mark.parametrize(
    "args",
    [
        ("setpoint", "1:1-04", "21"),
        ("mode", "1:1-04", "heat"),
        ("fan", "1:1-04", "high"),
        ("power", "1:1-04", "off"),
    ],
)
def test_unit_dry_runs_print_the_plan(
    args: tuple[str, ...], itm: FakeITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run of a unit change prints what would be sent, and changes nothing."""
    before = (itm.units[1].setpoint, itm.units[1].mode, itm.units[1].running)
    run("--dry-run", *args)
    out = capsys.readouterr().out
    assert out.startswith("dry run, would send:")
    assert len(out.splitlines()) > 1
    assert (itm.units[1].setpoint, itm.units[1].mode, itm.units[1].running) == before


@pytest.mark.parametrize(
    ("args", "expected"),
    [((), ["Building"]), (("Building",), ["Indoor units", "Ventilation", "Analog"])],
)
def test_zones(
    args: tuple[str, ...], expected: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """Zones lists the names at a path."""
    run("zones", *args)
    assert capsys.readouterr().out.splitlines() == expected


def test_setpoint(itm: FakeITM, capsys: pytest.CaptureFixture[str]) -> None:
    """Setpoint changes the unit and prints the value read back."""
    run("setpoint", "1:1-04", "22")
    assert capsys.readouterr().out == "1:1-04: setpoint now 22\n"
    assert itm.units[1].setpoint == 22


def test_caps(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Caps lists what each unit offers and refreshes the cache."""
    run("caps")
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == [
        "1:1-00",
        "1:1-00",
        "id=401",
        "modes=fan,cool,heat",
        "fan=yes",
        "Office",
    ]
    assert out[2].split() == [
        "1:1-09",
        "1:1-09",
        "id=210",
        "modes=fan,cool,heat,dry,auto",
        "fan=no",
    ]
    assert HOST in json.loads((tmp_path / "cache" / "units.json").read_text("utf-8"))


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ("power", "1:1-00", "off"),
            "1:1-00: power=Stop mode=Heat fan=medium setpoint=21",
        ),
        (
            ("power", "1:1-09", "on"),
            "1:1-09: power=Start mode=Cool fan=medium setpoint=18",
        ),
        (
            ("mode", "1:1-00", "cool"),
            "1:1-00: power=Start mode=Cool fan=medium setpoint=21",
        ),
        (
            ("fan", "1:1-04", "high"),
            "1:1-04: power=Start mode=Cool fan=high setpoint=23",
        ),
    ],
)
def test_changes(
    args: tuple[str, ...], expected: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """Power, mode and fan print the unit as read back after the change."""
    run(*args)
    assert capsys.readouterr().out == expected + "\n"


def test_zone_and_session_file_options(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--zone picks the units' zone and --session-file where the token is kept."""
    session_file = tmp_path / "session.json"
    itm_cli.main(
        [
            "--host",
            HOST,
            "--zone",
            "Building/Ventilation",
            "--session-file",
            str(session_file),
            "status",
        ]
    )
    out = capsys.readouterr().out.splitlines()
    assert len(out) == 2  # the header and the zone's one ventilation unit
    assert out[1].split()[:5] == ["Vent", "1", "Fresh", "air", "Start"]
    assert not session_file.exists()


def test_client_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    """The client gets the host, the zone path and a per-host session file."""
    created: list[CliClient] = []

    async def capture(client: ItmClient, _args: Any) -> None:
        assert isinstance(client, CliClient)
        created.append(client)

    monkeypatch.setitem(itm_cli.COMMANDS, "zones", capture)
    run("zones")
    kw = created[0].created_with
    assert kw["host"] == HOST
    assert kw["zone_path"] == ("Building", "Indoor units")
    assert kw["store"].path == str(itm_cli.CACHE_DIR / f"session-{HOST}.json")
    assert kw["store"].host == HOST


# -- errors ----------------------------------------------------------------------------


def test_itm_error_is_reported(itm: FakeITM) -> None:
    """Library errors end the program with their message."""
    with pytest.raises(SystemExit, match=r"^error: .*stopped") as exc:
        run("setpoint", "1:1-09", "17")
    assert isinstance(exc.value.code, str)
    assert itm.session is None


def test_error_message_is_sanitized(itm: FakeITM) -> None:
    """Control characters in an error can't reach the terminal."""
    itm.fail_next = ItmError("bad\x1b[2Jthing\nhere")
    with pytest.raises(SystemExit) as exc:
        run("status")
    assert exc.value.code == "error: bad?[2Jthing?here"


@pytest.mark.parametrize(
    ("error", "reason"),
    [
        (aiohttp.ClientConnectionError("connection refused"), "connection refused"),
        (TimeoutError(), "TimeoutError"),
    ],
)
def test_unreachable(itm: FakeITM, error: Exception, reason: str) -> None:
    """Network errors say the iTM could not be reached, and why."""
    itm.fail_next = error
    with pytest.raises(SystemExit) as exc:
        run("status")
    assert exc.value.code == f"error: could not reach the iTM at {HOST}: {reason}"


@pytest.mark.parametrize("error", [KeyboardInterrupt(), asyncio.CancelledError()])
def test_interrupted(itm: FakeITM, error: BaseException) -> None:
    """An interrupt exits with 130."""
    itm.fail_next = error  # type: ignore[assignment]
    with pytest.raises(SystemExit) as exc:
        run("status")
    assert exc.value.code == 130


def test_sigterm_logs_out(itm: FakeITM, monkeypatch: pytest.MonkeyPatch) -> None:
    """SIGTERM cancels the command, and the session is still released."""
    monkeypatch.setattr(CliClient, "sigterm_at", 4)  # after login, while working
    with pytest.raises(SystemExit) as exc:
        run("status")
    assert exc.value.code == 130
    assert itm.requests[-1]["event"] == 10  # logout
    assert itm.session is None


# -- credentials -----------------------------------------------------------------------


def test_missing_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without credentials the program explains what to set."""
    monkeypatch.delenv("ITM_PASSWORD")
    with pytest.raises(SystemExit, match="set ITM_USERNAME and ITM_PASSWORD"):
        run("zones")


def test_wrong_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected login is reported as an error."""
    monkeypatch.setenv("ITM_PASSWORD", "wrong")
    with pytest.raises(SystemExit, match=r"^error: login rejected"):
        run("zones")


FIELDS = [
    {"id": "username", "label": "username", "value": "user"},
    {"id": "password", "label": "password", "value": "secret"},
]


@pytest.mark.parametrize("data", [FIELDS, {"fields": FIELDS}])
def test_creds_file(
    data: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """--creds reads both 1Password export formats, ahead of the environment."""
    monkeypatch.setenv("ITM_PASSWORD", "wrong")
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps(data), encoding="utf-8")
    itm_cli.main(["--host", HOST, "--creds", str(creds), "zones"])
    assert capsys.readouterr().out == "Building\n"


def test_credentials_from_env() -> None:
    """Without a file the credentials come from the environment."""
    assert itm_cli._credentials(None) == ("user", "secret")


def test_credentials_single_field(tmp_path: Path) -> None:
    """A single exported field is read as a field list of one."""
    creds = tmp_path / "creds.json"
    creds.write_text(json.dumps({"label": "username", "value": "u"}), encoding="utf-8")
    with pytest.raises(SystemExit, match="no username and password"):
        itm_cli._credentials(str(creds))


def test_unusable_cache_is_ignored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A cache file that is valid JSON but not an object is ignored and replaced."""
    cache = tmp_path / "units.json"
    cache.write_text("[]", encoding="utf-8")
    monkeypatch.setattr(itm_cli, "UNITS_CACHE", cache)
    assert itm_cli._load_caps(HOST) == {}
    itm_cli._save_caps(HOST, {})
    assert json.loads(cache.read_text(encoding="utf-8")) == {HOST: {}}


# -- helpers ---------------------------------------------------------------------------


def test_load_caps(tmp_path: Path) -> None:
    """The cache gives the capabilities of one host's units, zones as a tuple."""
    assert itm_cli._load_caps(HOST) == {}
    caps = {
        "a": Capabilities("a", {"cool": "Cool"}, fan_speed=True, zones=("x", "y")),
    }
    (tmp_path / "cache").mkdir()
    itm_cli._save_caps(HOST, caps)
    itm_cli._save_caps("other", {})
    assert itm_cli._load_caps(HOST) == caps
    assert itm_cli._load_caps("other") == {}
    assert itm_cli._load_caps("unknown") == {}


@pytest.mark.parametrize(
    "content",
    ["{not json", json.dumps({HOST: {"a": {"unexpected": 1}}})],
)
def test_load_caps_bad_cache(tmp_path: Path, content: str) -> None:
    """A corrupt or outdated cache is ignored."""
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "units.json").write_text(content, encoding="utf-8")
    assert itm_cli._load_caps(HOST) == {}


def test_save_caps_replaces_corrupt_cache(tmp_path: Path) -> None:
    """Saving over a corrupt cache starts a new one."""
    (tmp_path / "cache").mkdir()
    (tmp_path / "cache" / "units.json").write_text("{not json", encoding="utf-8")
    itm_cli._save_caps(HOST, {})
    data = (tmp_path / "cache" / "units.json").read_text(encoding="utf-8")
    assert json.loads(data) == {HOST: {}}


@pytest.mark.parametrize(
    ("host", "name"),
    [
        ("10.0.0.1", "session-10.0.0.1.json"),
        ("https://itm.local:8443/", "session-https___itm.local_8443_.json"),
        ("../../etc/passwd", "session-.._.._etc_passwd.json"),
    ],
)
def test_session_file(host: str, name: str) -> None:
    """The session file stays inside the cache directory whatever the host is."""
    path = itm_cli._session_file(host)
    assert path == itm_cli.CACHE_DIR / name
    assert path.parent == itm_cli.CACHE_DIR


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Office 21.0°C", "Office 21.0°C"),
        ("a\x1b[31mb", "a?[31mb"),
        ("x\u202ey\r\n", "x?y??"),
        ("\x00\x7f\x9b", "???"),
    ],
)
def test_safe(text: str, expected: str) -> None:
    """Control and format characters are replaced."""
    assert _safe(text) == expected


def test_print_change_without_unit(capsys: pytest.CaptureFixture[str]) -> None:
    """Nothing is printed when no unit was read back."""
    itm_cli._print_change("1:1-00", None)
    assert not capsys.readouterr().out


def test_host_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --host or ITM_HOST the CLI says what is missing."""
    monkeypatch.delenv("ITM_HOST", raising=False)
    with pytest.raises(SystemExit):
        itm_cli.main(["zones"])


def test_status_list_view(itm: FakeITM, capsys: pytest.CaptureFixture[str]) -> None:
    """Status can read the zone's list view instead of every panel."""
    run("status", "--view", "list")
    out = capsys.readouterr().out.splitlines()
    assert out[1].split()[:4] == ["1:1-00", "Office", "on", "Heat"]
    assert out[3].split()[:3] == ["1:1-09", "-", "off"]
    assert any(r["screen"] == 1 and r["part"] == 4 for r in itm.requests)  # "List"
    assert itm.list_shown is False


def test_status_warns_about_unknown_mode_labels(
    itm: FakeITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """A mode label the client doesn't know is printed with the status."""
    itm.units[0].modes = ("Fan", "Cool", "Heat", "Chauffe-eau")
    run("status")
    assert "1:1-00: unknown mode labels ['Chauffe-eau']" in capsys.readouterr().out


def test_settings(itm: FakeITM, capsys: pytest.CaptureFixture[str]) -> None:
    """Settings prints a unit's locks, limits and status."""
    itm.units[0].lock_mode = 1
    itm.units[0].cool_limit = (True, 20.0, 24.0)
    run("settings", "1:1-00")
    out = capsys.readouterr().out
    assert "1:1-00: thermostat off" in out
    assert "remote locks: start/stop allowed, mode forbidden, setpoint allowed" in out
    assert "cool limit: on, 20.0-24.0" in out
    assert "heat limit: off, 16.0-32.0" in out
    assert "overtime timer: off" in out
    assert "Thermostat Status: OFF" in out
    assert itm.dialog is None


def test_read_only_option(itm: FakeITM) -> None:
    """--read-only refuses writes before anything is sent."""
    with pytest.raises(SystemExit, match="read-only"):
        run("--read-only", "setpoint", "1:1-04", "21")
    assert itm.requests == []
    assert itm.units[1].setpoint == 23


def test_load_caps_with_unknown_labels(tmp_path: Path) -> None:
    """Cached capabilities keep the unknown labels as a tuple."""
    cache = tmp_path / "units.json"
    cache.write_text(
        json.dumps(
            {
                HOST: {
                    "a": {
                        "name": "a",
                        "mode_labels": {},
                        "fan_speed": False,
                        "unknown_labels": ["Chauffe-eau"],
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(itm_cli, "UNITS_CACHE", cache)
        assert itm_cli._load_caps(HOST)["a"].unknown_labels == ("Chauffe-eau",)


def test_points(itm: FakeITM, capsys: pytest.CaptureFixture[str]) -> None:
    """Points lists units, ventilation units and analog points with their kind."""
    itm.analog[0].alarm = True
    run("--zone", "Building/Analog", "points", "--details")
    out = capsys.readouterr().out
    assert "Alarm 1        analog       ALARM    1:1-00 Suction Temp. <= 30.0 °C" in out
    run("--zone", "Building/Ventilation", "points")
    assert capsys.readouterr().out.split()[:4] == ["Vent", "1", "ventilation", "Start"]
    run("points")
    assert capsys.readouterr().out.splitlines()[0].split()[:3] == [
        "1:1-00",
        "indoor",
        "Start",
    ]


def test_errors(itm: FakeITM, capsys: pytest.CaptureFixture[str]) -> None:
    """Errors shows the error information, or that there is none."""
    run("errors")
    assert capsys.readouterr().out.strip() == "no error"
    itm.errors = [["1:1-04", "U4"]]
    run("errors")
    out = capsys.readouterr().out.splitlines()
    assert out[0].split() == ["Name", "Code"]
    assert out[1].split() == ["1:1-04", "U4"]
    assert out[2] == "Contact 0800-1234"


def test_menu_commands(itm: FakeITM, capsys: pytest.CaptureFixture[str]) -> None:
    """The menu read commands print what the iTM shows."""
    itm.groups = [FakeGroup("Group 01", enabled=True, members=["1:1-04"])]
    run("info")
    out = capsys.readouterr().out
    assert "version 1.00.00 (Jan 01 2025, 00:00:00)" in out
    assert "language English, °C, decimal ., 24h, dates %d/%m/%Y" in out
    assert "clock 01/01/2025 10:15:30" in out
    assert "DST off" in out
    run("changeover", "--details")
    out = capsys.readouterr().out
    assert (
        "Group 01                 enabled   1  differential 3 °C, "
        "reference average, members 1:1-04" in out
    )
    run("setback")
    assert "setback high: +3 / -5 °C" in capsys.readouterr().out
    run("overtime")
    assert capsys.readouterr().out.strip() == "overtime timer: 60 min"
    run("emgstop")
    assert capsys.readouterr().out.split() == ["Prog.1", "enabled"]
    run("schedules")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].split() == ["Always", "enabled", "all", "year"]
    assert lines[1].split() == ["Program", "G", "disabled", "01/06-31/08"]
    run("history", "--day", "2025-01-01")
    lines = capsys.readouterr().out.splitlines()
    assert lines[0].startswith("-- 01/01/2025")
    assert "Auto Change" in lines[1]
    for unit in ("AC 05", "ac 05", "1:1-04"):
        run("history", "--day", "2025-01-01", "--unit", unit)
        captured = capsys.readouterr()
        lines = captured.out.splitlines()
        assert len(lines) == 3
        assert all("1:1-04" in line for line in lines[1:])
        assert "note:" not in captured.err
    run("history", "--day", "2025-01-01", "--unit", "Office")
    assert len(capsys.readouterr().out.splitlines()) == 1
    run("history", "--day", "2025-01-01", "--unit", "Nowhere")
    captured = capsys.readouterr()
    assert len(captured.out.splitlines()) == 1
    assert "use --zone" in captured.err
    run("functions")
    assert "interlock                0 entries" in capsys.readouterr().out
    run("config")
    out = capsys.readouterr().out
    assert "indicators: energy saving off, error off, emergency stop off" in out
    assert "emergency stop Prog.1: enabled" in out
    itm.groups = []
    run("changeover")
    assert capsys.readouterr().out.strip() == "no auto changeover groups"


def test_schedule_commands(itm: FakeITM, capsys: pytest.CaptureFixture[str]) -> None:
    """The schedule commands print a program and a day."""
    run("schedule", "Always")
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Always: enabled, all year"
    assert "  Monday (2 time slots)" in out
    assert "[power on]" in out  # parsed from the text; no event screen opened
    assert not any(r["screen"] == 109 for r in itm.requests)
    run("schedule", "Always", "--details")
    out = capsys.readouterr().out
    assert "06:00 Indoor units         zone         Start  [power on, mode cool" in out
    assert "locks forbidden/forbidden/allowed" in out
    assert "  Ex1" in out
    assert "calendar: Jan 1 -> Ex1" in out
    assert any(r["screen"] == 109 for r in itm.requests)
    run("schedule", "Always", "--no-events")
    assert "06:00" not in capsys.readouterr().out
    run("schedule-day", "Always", "--days-ahead", "1")
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Always: 02/01(Thu)"
    assert "18:00 Indoor units         zone         Stop" in out
    assert itm.open_screens == set()


def test_setback_and_overtime_writes(
    itm: FakeITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """The setback and overtime commands change values, with dry runs."""
    run("--dry-run", "overtime", "150")
    out = capsys.readouterr().out
    assert "dry run, would send:" in out
    assert "save overtime duration 150 min" in out
    assert itm.overtime_idx == 1
    run("overtime", "150")
    assert capsys.readouterr().out.strip() == "overtime timer: 150 min"
    assert itm.overtime_idx == 4
    run("setback", "--high-up", "3", "--recovery-cool", "1")
    out = capsys.readouterr().out
    assert "setback high: +3 / -5 °C" in out
    assert "recovery: cool +1 °C" in out
    itm.groups = [FakeGroup("Group 01", members=["1:1-04"])]
    run("--dry-run", "changeover", "enable", "Group 01")
    assert "would send" in capsys.readouterr().out
    assert itm.groups[0].enabled is False
    run("changeover", "enable", "Group 01")
    assert "Group 01                 enabled" in capsys.readouterr().out


def test_unit_and_schedule_writes(
    itm: FakeITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """Settings, setback power states and schedule on/off from the command line."""
    run("--dry-run", "settings", "1:1-09", "--overtime", "on")
    assert "save settings" in capsys.readouterr().out
    assert itm.units[2].overtime is False
    run("settings", "1:1-09", "--mode-lock", "forbidden", "--cool-limit", "20-26")
    out = capsys.readouterr().out
    assert "mode forbidden" in out
    assert "cool limit: on, 20.0-26.0" in out
    run("settings", "1:1-09", "--mode-lock", "allowed", "--cool-limit", "off")
    assert "cool limit: off" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="MIN-MAX"):
        run("settings", "1:1-09", "--cool-limit", "warm")
    run("power", "1:1-00", "setback-high")
    assert "power=SB High" in capsys.readouterr().out
    run("power", "1:1-00", "on")
    assert "power=Start" in capsys.readouterr().out
    run("schedule", "Program E", "--enable")
    assert capsys.readouterr().out.split()[:3] == ["Program", "E", "enabled"]
    run("--dry-run", "schedule", "Program E", "--disable")
    assert "disable schedule Program E" in capsys.readouterr().out
    run("--dry-run", "schedule", "demo group", "--create")
    assert "create schedule demo group" in capsys.readouterr().out
    run("schedule", "demo group", "--create")
    assert capsys.readouterr().out.split()[:3] == ["demo", "group", "disabled"]
    run("--dry-run", "schedule", "demo group", "--delete")
    assert "delete schedule demo group" in capsys.readouterr().out
    run("schedule", "demo group", "--delete")
    assert capsys.readouterr().out == "demo group: deleted\n"
    assert "demo group" not in [p[1] for p in itm.schedules]


def test_system_command(capsys: pytest.CaptureFixture[str]) -> None:
    """`itm system` prints a line or section per screen."""
    run("system")
    out = capsys.readouterr().out
    assert "zones (shown from Top):" in out
    assert "network: intelligent Touch Manager, host localhost" in out
    assert "e-mail: on, ops@example.com" in out
    assert "web users: admin (admin), viewer (User)" in out
    assert "touch panel: backlight 7, buzzer volume 1 for continuous" in out
    assert "screensaver: backlight_off after 10 min, off on error off" in out
    assert "confirm dialogs: off" in out
    assert "remote management: Connect (198.51.100.7), connected on" in out
    assert "PPD: 20/11/2024 - 01/01/2025, time limit off" in out
    assert "energy navigator: sampling 20/12/2024 - 02/01/2025 (Top>All)" in out
    run("system", "--only", "confirm_dialogs")
    assert capsys.readouterr().out == "confirm dialogs: off\n"
    run("config", "--system")
    assert "confirm dialogs: off" in capsys.readouterr().out
    run("hardware", "--key-click-volume", "1", "--buzzer-minutes", "continuous")
    assert "key click volume 1" in capsys.readouterr().out
    run("--dry-run", "hardware", "--backlight", "2")
    assert "save hardware settings" in capsys.readouterr().out
    run("hardware")
    assert "backlight 7" in capsys.readouterr().out
    run("screensaver", "--mode", "screen_1", "--delay", "10", "--off-on-error", "off")
    assert "screensaver: screen_1 after 10 min, off on error off" in (
        capsys.readouterr().out
    )
    run("--dry-run", "screensaver", "--delay", "5")
    assert "save screensaver settings" in capsys.readouterr().out


def test_system_write_commands(
    itm: FakeITM, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Maintenance, point, zone, user, language, summer time and clock commands."""
    unit = itm.units[2].name
    run("maintenance", unit, "on")
    assert capsys.readouterr().out == f"{unit}: maintenance on\n"
    run("point", "1:1-09", "--info", "AC 10 test")
    assert capsys.readouterr().out == "1:1-09: AC 10 test\n"
    run("zone", "Building")
    assert "Building:  (start interval 0" in capsys.readouterr().out
    run("zone", "Building", "--info", "Main", "--start-interval", "3")
    assert "Building: Main (start interval 3" in capsys.readouterr().out
    monkeypatch.setenv("ITM_NEW_PASSWORD", "s3cret!")
    run("user", "add", "demouser", "--manage-zone", "Building")
    assert capsys.readouterr().out == "demouser: user\n"
    run("user", "set", "demouser", "--type", "admin", "--password")
    assert capsys.readouterr().out == "demouser: admin\n"
    run("--dry-run", "user", "delete", "demouser")
    assert "delete web user demouser" in capsys.readouterr().out
    run("user", "delete", "demouser")
    assert capsys.readouterr().out == "demouser: deleted\n"
    run("language")
    assert capsys.readouterr().out == "language: en\n"
    run("--dry-run", "language", "en")
    assert "pick language en" in capsys.readouterr().out
    run("dst", "on")
    assert "summer time: on" in capsys.readouterr().out
    run("dst")
    assert "hour=3" in capsys.readouterr().out
    run("--dry-run", "clock", "--set-now")
    assert "save the clock" in capsys.readouterr().out
    run("clock")
    assert capsys.readouterr().out.startswith("clock: 2025-01-01 10:15:30")


def test_schedule_edit_command(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`itm schedule-edit NAME ACTION ...` on a fake whose editor saves."""
    fake = FakeScheduleITM()
    monkeypatch.setattr(CliClient, "itm", fake, raising=False)
    edit = ("schedule-edit", "Always")
    run(
        "schedule-edit", "Always", "add-event", "3", "07:00", "1:1-09", "--power", "off"
    )
    assert "07:00 1:1-09" in capsys.readouterr().out
    run("--dry-run", *edit, "delete-event", "3", "07:00", "1:1-09")
    assert "dry run, would send" in capsys.readouterr().out
    run(
        "schedule-edit", "Always", "edit-event", "3", "07:00", "1:1-09",
        "--new-time", "07:15", "--power", "off", "--start-stop-lock", "allowed",
    )  # fmt: skip
    assert "07:15 1:1-09" in capsys.readouterr().out
    run("schedule-edit", *edit[1:], "edit-event", "3", "07:15", "1:1-09")
    assert "Stop" in capsys.readouterr().out
    run("schedule-edit", *edit[1:], "edit-event", "3", "07:15", "1:1-09", "--replace")
    assert "Stop" not in capsys.readouterr().out
    run("schedule-edit", "Always", "delete-event", "3", "07:15", "1:1-09")
    assert capsys.readouterr().out == "07:15 1:1-09: deleted\n"
    run("schedule-edit", "Always", "period", "06-15:08-31")
    assert "Always: " in capsys.readouterr().out
    run("schedule-edit", "Always", "period", "off")
    assert "all year" in capsys.readouterr().out
    run("schedule-edit", "Always", "rename-special", "Ex2", "Feest")
    assert "Feest" in capsys.readouterr().out
    run("schedule-edit", "Always", "add-rule", "2", "--date", "12-25")
    out = capsys.readouterr().out
    rule = [line for line in out.splitlines() if "calendar:" in line][-1]
    month, day, _, pattern = rule.split(":", 1)[1].split()
    run("schedule-edit", "Always", "delete-rule", month, day, pattern)
    assert rule not in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run("schedule-edit", "Always", "period", "May")
    with pytest.raises(SystemExit):
        run("schedule-edit", "Always", "add-event", "3", "7:00", "1:1-09")


def test_network_command(itm: FakeITM, capsys: pytest.CaptureFixture[str]) -> None:
    """`itm network` shows the settings; a change is refused unless allowed."""
    run("network")
    assert "network: intelligent Touch Manager, host localhost" in (
        capsys.readouterr().out
    )
    run("--dry-run", "network", "--dns2", "8.8.4.4")
    assert "network dns_secondary = 8.8.4.4" in capsys.readouterr().out
    with pytest.raises(SystemExit):
        run("network", "--dns2", "8.8.4.4")
    assert itm.network[6] == ""
    run("network", "--dns2", "8.8.4.4", "--allow-network-change")
    assert "DNS 9.9.9.9 8.8.4.4" in capsys.readouterr().out


def test_service_command(
    itm: FakeITM, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`itm service` reads the service screens; the password comes from the env."""
    monkeypatch.delenv("ITM_SERVICE_PASSWORD", raising=False)
    with pytest.raises(SystemExit):
        run("service")
    monkeypatch.setenv("ITM_SERVICE_PASSWORD", itm.service_password)
    run("service")
    out = capsys.readouterr().out
    assert "time zone: GMT+01:00:Zone B" in out
    assert "R32 alarm: output ports ['DO-1'], buzzer off" in out
    assert "central monitoring: warning as abnormal on, dry mode off" in out
    assert "dealer options off: Emergency Stop, E-mail Error Report" in out


def test_airflow_direction_command(
    itm: FakeITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """`itm settings UNIT --airflow-direction` steps the direction and shows it."""
    itm.units[0].airflow = 3103
    unit = itm.units[0].name
    run("settings", unit, "--airflow-direction", "position_3")
    assert "airflow direction: position_3" in capsys.readouterr().out


def test_control_zone_command(itm: FakeITM, capsys: pytest.CaptureFixture[str]) -> None:
    """`itm --zone Building control-zone "Indoor units" ...` operates them all."""
    run("--zone", "Building", "control-zone", "Indoor units", "--power", "on")
    run("--zone", "Building", "control-zone", "Indoor units", "--setpoint", "22")
    out = capsys.readouterr().out
    assert all(u.running and u.setpoint == 22 for u in itm.units)
    assert itm.units[0].name in out
    run("--zone", "Building", "control-zone", "Indoor units", "--power", "setback-low")
    assert all(u.setback == 1 for u in itm.units)


def test_reset_and_ventilation_options(
    itm: FakeITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """The reset and ventilation options of `itm settings`."""
    unit = itm.units[0]
    unit.filter_sign = True
    run("settings", unit.name)
    assert "filter sign: shown" in capsys.readouterr().out
    run("settings", unit.name, "--reset-filter-sign")
    assert "filter sign" not in capsys.readouterr().out
    vent = itm.ventilation[0]
    vent.vent_modes = ("Automatic", "Bypass")
    run(
        "--zone",
        "Building/Ventilation",
        "settings",
        vent.name,
        "--ventilation-mode",
        "Bypass",
    )
    assert "ventilation mode: Bypass (Automatic, Bypass)" in capsys.readouterr().out


def test_region_command(itm: FakeITM, capsys: pytest.CaptureFixture[str]) -> None:
    """`itm region` shows the formats and changes them."""
    run("region", "--time", "12h", "--decimal", "comma", "--date-format", "ymd")
    assert "region: dates %Y/%m/%d, 12h" in capsys.readouterr().out
    assert itm.decimal_comma is True
    run("--dry-run", "region", "--temperature", "f")
    assert "save region settings" in capsys.readouterr().out


def test_zone_structure_commands(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """zone-add, zone-members, zone-icon, zone-move, zone-delete and point-icon."""
    fake = FakeZonesITM()
    monkeypatch.setattr(CliClient, "itm", fake, raising=False)
    point = fake.units[0].name
    run("zone-add", "demo group")
    assert "'demo group'" in capsys.readouterr().out
    run("zone-members", "demo group", "--add", point)
    assert capsys.readouterr().out == f"demo group: {point}\n"
    second = fake.units[1].name
    run("zone-members", "demo group", "--add", second)
    capsys.readouterr()
    run("zone-members", "demo group", "--order", second, point)
    assert capsys.readouterr().out == f"demo group: {second}, {point}\n"
    run("zone-members", "demo group", "--remove", point, "--remove", second)
    assert capsys.readouterr().out == "demo group: no members\n"
    run("zone-icon", "demo group", "1004")
    run("zone-move", "demo group", "Zone A")
    run("zone-move", "demo group", "Top")
    run("--dry-run", "zone-delete", "demo group")
    assert "delete" in capsys.readouterr().out
    run("zone-delete", "demo group")
    assert "demo group" not in capsys.readouterr().out
    run("point-icon", point, "2100")
    assert capsys.readouterr().out == f"{point}: icon 2100\n"


def test_email_and_confirm_commands(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`itm email` and `itm confirm-dialogs`."""
    fake = FakeSystemEditITM()
    monkeypatch.setattr(CliClient, "itm", fake, raising=False)
    point = fake.units[0].name
    run("email", "add", "ops@example.com", "--point", point)
    assert "ops@example.com" in capsys.readouterr().out
    run("email")
    assert "e-mail server:" in capsys.readouterr().out
    run("email", "delete", "ops@example.com")
    assert "no addresses" in capsys.readouterr().out
    run("confirm-dialogs", "off")
    assert "confirm dialogs: off" in capsys.readouterr().out
    run("--dry-run", "confirm-dialogs", "on")
    assert "dry run" in capsys.readouterr().out


def test_schedule_copy_commands(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The copy actions of `itm schedule-edit`."""
    fake = FakeScheduleITM()
    monkeypatch.setattr(CliClient, "itm", fake, raising=False)
    run("schedule-edit", "Always", "copy-program", "demo copy")
    assert "copy-program done" in capsys.readouterr().out
    run("schedule-edit", "demo copy", "copy-day", "1", "2", "3")
    run("schedule-edit", "demo copy", "clear-day", "3")
    run(
        "schedule-edit",
        "demo copy",
        "copy-event",
        "1",
        "06:00",
        "Indoor units",
        "06:30",
    )
    run("--dry-run", "schedule-edit", "demo copy", "copy-calendar", "Always")
    assert "dry run" in capsys.readouterr().out


def test_ppd_and_export_commands(
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """`itm ppd` and `itm export history` (written to a local file)."""
    fake = FakeExportsITM()
    monkeypatch.setattr(CliClient, "itm", fake, raising=False)

    async def get(_self: Any, path: str) -> bytes:
        return fake.files[path]

    monkeypatch.setattr(CliClient, "_get_bytes", get)
    run("ppd", "--start", "2024-11-20", "--end", "2024-12-19")
    assert "PPD:" in capsys.readouterr().out
    out = tmp_path / "history.csv"
    run(
        "export",
        "history",
        "--start",
        "2024-12-20",
        "--end",
        "2025-01-01",
        "--out",
        str(out),
    )
    assert out.read_bytes().startswith(b"history")
    assert out.stat().st_mode & 0o777 == 0o600
    setup = tmp_path / "setup.bin"
    run("export", "setup", "--out", str(setup))
    assert setup.read_bytes().startswith(b"setup")
    run("--dry-run", "backup")
    assert "backup" in capsys.readouterr().out
    saved = tmp_path / "backup.bin"
    run("backup", "--out", str(saved))
    assert saved.read_bytes().startswith(b"backup")
    fake.backup_on_device = True
    run("backup")
    assert "the iTM keeps it" in capsys.readouterr().out
    archive = history_archive()

    async def get_archive(_self: Any, _path: str) -> bytes:
        return archive

    monkeypatch.setattr(CliClient, "_get_bytes", get_archive)
    run("export", "history", "--start", "2024-12-20", "--end", "2025-01-01", "--parse")
    printed = capsys.readouterr().out
    assert "AC1" in printed
    assert "setpoint" in printed
    assert "21.5" in printed
    assert "temperature_limit" in printed
    assert "event 605030469" in printed
    assert "192.0.2.10" in printed
    with pytest.raises(SystemExit):
        run("export", "energy")
    with pytest.raises(SystemExit):
        run("export", "history", "--start", "2025-13-01", "--end", "2025-01-01")


def test_trace_holds_no_credentials(tmp_path: Path) -> None:
    """`itm --trace FILE` writes the session without the login's name or hash."""
    path = tmp_path / "cli.jsonl"
    run("--trace", str(path), "status")
    text = path.read_text()
    entries = [json.loads(line) for line in text.splitlines()]
    assert any(e["request"] and e["request"][0] == 1203 for e in entries)
    assert all(e.get("reply", "0" * 32).startswith("0" * 32) for e in entries)
    assert hashlib.sha256(b"secret").hexdigest() not in text.lower()
