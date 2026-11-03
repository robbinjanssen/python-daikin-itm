"""The history export parser, on a made-up archive laid out like the iTM's."""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from typing import Any

import pytest

from daikin_itm import HistoryExportError, parse_history_export
from daikin_itm import history_export as hx

from .export_archive import ZERO, history_archive, record


def test_records_and_meanings() -> None:
    """Every field the history screen shows is read; unknown codes stay numbers."""
    export = parse_history_export(history_archive())
    records = export.records
    assert [r.number for r in records] == list(range(101, 110))

    login, logout, program, start, setpoint, clamp, fan, setback, unknown = records
    assert login.event == "login"
    assert login.kind == "management"
    assert login.name == "user"
    assert login.ip_address == "192.0.2.10"
    assert login.time == datetime(2025, 3, 3, 23, 15, 2, tzinfo=UTC)
    assert logout.event == "logout"
    assert program.event == "program_run"
    assert program.kind == "settings"
    assert program.source == "schedule"
    assert program.point_id is None

    assert (start.event, start.power, start.source) == ("power", "on", "schedule")
    assert start.point_id == 302
    assert (setpoint.setpoint, setpoint.source) == (21.5, "schedule")
    assert (clamp.setpoint, clamp.source) == (23.0, "temperature_limit")
    assert (fan.fan_speed, fan.source) == ("auto", None)
    assert setback.power == "setback_low"
    assert start.setpoint is None
    assert setpoint.power is None
    assert setpoint.fan_speed is None

    assert unknown.event is None
    assert unknown.event_code == 605030469
    assert len(unknown.fields) == 20


def test_converter_settings() -> None:
    """The converter settings give the export time, the days asked and the version."""
    export = parse_history_export(history_archive())
    assert export.utc_offset_minutes == -60
    assert export.first_day == datetime(2025, 3, 3, 23, 0, tzinfo=UTC)
    assert export.last_day == export.first_day
    assert export.exported is not None
    assert export.model == "intelligent Touch Manager"
    assert export.version == "Ver1.00.00"

    bare = parse_history_export(history_archive(config=False))
    assert len(bare.records) == 9
    assert bare.utc_offset_minutes is None
    assert bare.exported is None
    assert bare.model is None


def test_only_the_history_members_are_read(monkeypatch: pytest.MonkeyPatch) -> None:
    """The authentication file and the converter are never opened."""
    data = history_archive()
    opened: list[str] = []
    original = zipfile.ZipFile.open

    def spy(self: zipfile.ZipFile, name: Any, *args: Any, **kwargs: Any) -> Any:
        opened.append(getattr(name, "filename", name))
        return original(self, name, *args, **kwargs)

    monkeypatch.setattr(zipfile.ZipFile, "open", spy)
    parse_history_export(data)
    assert sorted(n.rsplit("/", 1)[-1] for n in opened) == [
        "conv.conf",
        "t_history.dat",
    ]


def test_bad_archives(monkeypatch: pytest.MonkeyPatch) -> None:
    """Anything that isn't a readable export raises HistoryExportError."""
    with pytest.raises(HistoryExportError, match="no ZIP"):
        parse_history_export(b"not a zip")

    empty = io.BytesIO()
    with zipfile.ZipFile(empty, "w") as archive:
        archive.writestr("History/readme.txt", "x")
    with pytest.raises(HistoryExportError, match=r"holds no t_history\.dat"):
        parse_history_export(empty.getvalue())

    with pytest.raises(HistoryExportError, match="fields"):
        parse_history_export(history_archive(["1\t2\t3"]))
    with pytest.raises(HistoryExportError, match="no valid time"):
        parse_history_export(history_archive([record(f1="yesterday")]))

    monkeypatch.setattr(hx, "MAX_MEMBER_BYTES", 10)
    with pytest.raises(HistoryExportError, match="too large"):
        parse_history_export(history_archive())


def test_bad_numbers_read_as_zero() -> None:
    """A field that should be a number but isn't reads as 0, as the converter does."""
    line = "\t".join(["x", "20250304061004", *ZERO[2:]])
    (only,) = parse_history_export(history_archive([line])).records
    assert only.number == 0
