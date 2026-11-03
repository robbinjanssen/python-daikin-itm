"""`itm own-settings` and `itm user screens`, end-to-end against the simulated iTM."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from daikin_itm import cli as itm_cli

from .fake_users import FakeUsersITM
from .test_cli import CliClient, run

if TYPE_CHECKING:
    from pathlib import Path

NEW_PASSWORD = "n3w-placeholder"


@pytest.fixture(name="itm")
def fixture_itm(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeUsersITM:
    """Route the CLI to a fresh simulator, with its cache in a temp directory."""
    fake = FakeUsersITM()
    monkeypatch.setattr(CliClient, "itm", fake, raising=False)
    monkeypatch.setattr(CliClient, "sigterm_at", None)
    monkeypatch.setattr(itm_cli, "ItmClient", CliClient)
    monkeypatch.setattr(itm_cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(itm_cli, "UNITS_CACHE", tmp_path / "cache" / "units.json")
    monkeypatch.setenv("ITM_USERNAME", "user")
    monkeypatch.setenv("ITM_PASSWORD", "secret")
    return fake


def test_own_settings_command(
    itm: FakeUsersITM,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Show, dry-run and change the own settings; the password never printed."""
    run("own-settings")
    out = capsys.readouterr().out
    assert "language English" in out
    assert "time 24h, decimal point, icon colour 1" in out
    monkeypatch.setenv("ITM_NEW_PASSWORD", NEW_PASSWORD)
    run("--dry-run", "own-settings", "--password", "--time", "12h")
    out = capsys.readouterr().out
    assert "dry run" in out
    assert "own password = ***" in out
    assert NEW_PASSWORD not in out
    assert itm.own_password_changes == 0
    run("own-settings", "--date-format", "ymd", "--decimal", "comma", "--time", "12h")
    out = capsys.readouterr().out
    assert "date %Y/%m/%d, time 12h, decimal comma" in out
    run("own-settings", "--language", "en", "--icon-colour", "2")
    assert "language English" in capsys.readouterr().out


def test_user_screens_command(
    itm: FakeUsersITM, capsys: pytest.CaptureFixture[str]
) -> None:
    """`itm user screens NAME [--screen S ...]`."""
    run("user", "screens", "viewer")
    out = capsys.readouterr().out
    assert "viewer: screens Screen A" in out
    assert "available: Screen B, Screen C" in out
    run("--dry-run", "user", "screens", "viewer", "--screen", "Screen B")
    assert "dry run" in capsys.readouterr().out
    assert itm.user_screens["viewer"] == ["Screen A"]
    run("user", "screens", "viewer", "--screen", "Screen B", "--screen", "Screen C")
    assert "viewer: screens Screen B, Screen C" in capsys.readouterr().out
