"""The ``itm`` command line tool, run end-to-end against the simulated iTM."""

# pylint: disable=protected-access

from __future__ import annotations

import asyncio
import json
import os
import signal
import stat
from typing import TYPE_CHECKING, Any, ClassVar

import aiohttp
import pytest

from daikin_itm import Capabilities, ItmClient, ItmError
from daikin_itm import cli as itm_cli

from .fake_itm import FakeITM

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
    """Run the CLI against the simulator, for the units in Building/Indoor units."""
    zone = [] if "--zone" in args else ["--zone", "Building/Indoor units"]
    itm_cli.main(["--host", HOST, *zone, *args])


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
        "Verwarmen",
        "medium",
        "21",
        "19-24",
        "21.0",
    ]
    assert out[3].split()[:3] == ["1:1-09", "-", "Stop"]
    assert detail_requests(itm) == 3
    cache = json.loads((tmp_path / "cache" / "units.json").read_text(encoding="utf-8"))
    assert cache[HOST]["1:1-04"]["description"] == "Meeting room"
    assert stat.S_IMODE((tmp_path / "cache").stat().st_mode) == 0o700

    run("status")
    assert "Meeting room" in capsys.readouterr().out
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
    ("args", "expected"),
    [((), ["Building"]), (("Building",), ["Indoor units", "Ventilation"])],
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
        "id=101",
        "modes=fan,cool,heat",
        "fan=yes",
        "Office",
    ]
    assert out[2].split() == [
        "1:1-09",
        "1:1-09",
        "id=110",
        "modes=fan,cool,heat,dry,auto",
        "fan=no",
    ]
    assert HOST in json.loads((tmp_path / "cache" / "units.json").read_text("utf-8"))


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        (
            ("power", "1:1-00", "off"),
            "1:1-00: power=Stop mode=Verwarmen fan=medium setpoint=21",
        ),
        (
            ("power", "1:1-09", "on"),
            "1:1-09: power=Start mode=Koelen fan=medium setpoint=16",
        ),
        (
            ("mode", "1:1-00", "cool"),
            "1:1-00: power=Start mode=Koelen fan=medium setpoint=21",
        ),
        (
            ("fan", "1:1-04", "high"),
            "1:1-04: power=Start mode=Koelen fan=high setpoint=20",
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
    assert len(capsys.readouterr().out.splitlines()) == 1  # only the header
    assert not session_file.exists()  # removed again after the logout


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
    assert itm_cli._load_caps(HOST) == {}  # no cache yet
    caps = {
        "a": Capabilities("a", {"cool": "Koelen"}, fan_speed=True, zones=("x", "y")),
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
    assert itm_cli._safe(text) == expected


def test_print_change_without_unit(capsys: pytest.CaptureFixture[str]) -> None:
    """Nothing is printed when no unit was read back."""
    itm_cli._print_change("1:1-00", None)
    assert not capsys.readouterr().out


def test_host_is_required(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without --host or ITM_HOST the CLI says what is missing."""
    monkeypatch.delenv("ITM_HOST", raising=False)
    with pytest.raises(SystemExit):
        itm_cli.main(["zones"])
