"""Read the iTM's history export: the ZIP archive `export_history` downloads.

The archive is made for Daikin's Windows converter (``CSVOutputTool.exe``) and also
holds the iTM's setup data, including an authentication file. This module reads only
two members, in memory: ``t_history.dat`` (the records, one per line, 20
tab-separated fields) and ``conv.conf`` (the converter's settings). Nothing else is
opened or extracted.

Field meanings come from comparing an export with the history screen of the same day.
Codes not seen yet are kept as numbers (``event``, ``source`` and friends are then
None), so nothing is guessed.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime

from .exceptions import ItmError

HISTORY_MEMBER = "t_history.dat"
CONFIG_MEMBER = "conv.conf"
MAX_MEMBER_BYTES = 64 * 1024 * 1024  # a member larger than this is refused
FIELDS = 20

# Event codes (field 6), as seen.
EVENTS = {
    605028363: "login",
    605028364: "logout",
    605030354: "program_run",  # a schedule program ran (its name in field 9)
    806356045: "power",
    806356047: "setpoint",
    806356048: "fan_speed",
}
# Sources (field 14): what made the change; 0 is none (the history shows no source).
SOURCES = {
    5: "schedule",
    11: "overtime_timer",
    14: "temperature_limit",
    16: "initialise",
}
# History classes (field 7), as the history screen's filters name them.
CLASSES = {0: "management", 2: "status", 4: "settings"}
# Values per event (field 10).
POWER_VALUES = {0: "off", 1: "on", 2: "setback_low"}
FAN_SPEED_VALUES = {1: "medium", 100: "auto"}


class HistoryExportError(ItmError):
    """The history export could not be read."""


@dataclass(frozen=True)
class HistoryRecord:  # pylint: disable=too-many-instance-attributes
    """One history record of an export."""

    number: int  # the iTM's sequence number
    time: datetime  # in UTC
    event_code: int
    class_code: int
    name: str  # the point's name, the program's name, or the user's name
    value: int  # raw; see setpoint, power and fan_speed
    source_code: int
    ip_address: str | None  # for logins and logouts
    point_id: int | None  # the iTM's point number, for points
    fields: tuple[str, ...]  # every field, as exported

    @property
    def event(self) -> str | None:
        """The event's name ("setpoint", "login", ...), or None when not known."""
        return EVENTS.get(self.event_code)

    @property
    def source(self) -> str | None:
        """What made the change ("schedule", ...), or None (none, or not known)."""
        return SOURCES.get(self.source_code)

    @property
    def kind(self) -> str | None:
        """The history class ("status", "management", "settings"), or None."""
        return CLASSES.get(self.class_code)

    @property
    def setpoint(self) -> float | None:
        """The new setpoint in °C, for a setpoint record."""
        return self.value / 10 if self.event == "setpoint" else None

    @property
    def power(self) -> str | None:
        """The new power state ("on", "off", "setback_low"), for a power record."""
        return POWER_VALUES.get(self.value) if self.event == "power" else None

    @property
    def fan_speed(self) -> str | None:
        """The new fan speed ("medium", "auto"), for a fan speed record."""
        return FAN_SPEED_VALUES.get(self.value) if self.event == "fan_speed" else None


@dataclass(frozen=True)
class HistoryExport:
    """An export's records and what its converter settings say about it."""

    records: tuple[HistoryRecord, ...]
    utc_offset_minutes: int | None  # as the converter settings give it, e.g. -60
    exported: datetime | None  # when the export was made, in UTC
    first_day: datetime | None  # the start of the first and last day asked for, UTC
    last_day: datetime | None
    model: str | None
    version: str | None


def _member(archive: zipfile.ZipFile, name: str) -> bytes | None:
    info = next(
        (i for i in archive.infolist() if i.filename.rsplit("/", 1)[-1] == name), None
    )
    if info is None:
        return None
    if info.file_size > MAX_MEMBER_BYTES:
        msg = f"{name} is too large ({info.file_size} bytes)"
        raise HistoryExportError(msg)
    with archive.open(info) as member:
        data = member.read(MAX_MEMBER_BYTES + 1)
    if len(data) > MAX_MEMBER_BYTES:
        msg = f"{name} is too large"
        raise HistoryExportError(msg)
    return data


def _int(text: str) -> int:
    try:
        return int(text)
    except ValueError:
        return 0


def _record(line: str) -> HistoryRecord:
    fields = line.split("\t")
    if len(fields) < FIELDS:
        msg = f"a history record has {len(fields)} fields, not {FIELDS}: {line[:80]!r}"
        raise HistoryExportError(msg)
    try:
        time = datetime.strptime(fields[1], "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError as err:
        msg = f"a history record has no valid time: {fields[1]!r}"
        raise HistoryExportError(msg) from err
    point_id = _int(fields[18])
    return HistoryRecord(
        number=_int(fields[0]),
        time=time,
        event_code=_int(fields[5]),
        class_code=_int(fields[6]),
        name=fields[8],
        value=_int(fields[9]),
        source_code=_int(fields[13]),
        ip_address=fields[12] or None,
        point_id=point_id or None,
        fields=tuple(fields),
    )


def _timestamp(text: str) -> datetime | None:
    value = _int(text)
    return datetime.fromtimestamp(value, tz=UTC) if value > 0 else None


def parse_history_export(data: bytes) -> HistoryExport:
    """Read the records of a history export (the ZIP archive).

    Args:
    ----
        data: The archive, as `export_history` returns it in `Download.data`.

    Returns:
    -------
        The records (in the order exported) and the converter settings.

    Raises:
    ------
        HistoryExportError: The data is no ZIP archive, has no history file, or a
            record can't be read.

    """
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as err:
        msg = "the history export is no ZIP archive"
        raise HistoryExportError(msg) from err
    with archive:
        history = _member(archive, HISTORY_MEMBER)
        config = _member(archive, CONFIG_MEMBER)
    if history is None:
        msg = f"the history export holds no {HISTORY_MEMBER}"
        raise HistoryExportError(msg)
    text = history.decode("utf-8", errors="replace")
    records = tuple(_record(line) for line in text.split("\n") if line.strip())
    lines = (config or b"").decode("utf-8", errors="replace").split("\n")
    lines += [""] * 9
    return HistoryExport(
        records=records,
        utc_offset_minutes=_int(lines[1]) if lines[1].strip() else None,
        exported=_timestamp(lines[4]),
        first_day=_timestamp(lines[5]),
        last_day=_timestamp(lines[6]),
        model=lines[7].strip() or None,
        version=lines[8].strip() or None,
    )
