"""A made-up history export, laid out like the iTM's (no real installation's data)."""

from __future__ import annotations

import io
import zipfile

ZERO = ["0"] * 20


def record(**fields: str) -> str:
    """One t_history.dat line; keyword names are field numbers like f5="806356047"."""
    values = list(ZERO)
    for key, value in fields.items():
        values[int(key[1:])] = value
    return "\t".join(values)


RECORDS = [
    # login and logout of a web user
    record(
        f0="101",
        f1="20250303231502",
        f5="605028363",
        f7="102",
        f8="user",
        f12="192.0.2.10",
    ),
    record(
        f0="102",
        f1="20250303231503",
        f5="605028364",
        f7="102",
        f8="user",
        f12="192.0.2.10",
    ),
    # a schedule program ran
    record(
        f0="103", f1="20250304061000", f5="605030354", f6="4", f8="Program B", f13="5"
    ),
    # AC1: started by the schedule, setpoint 21.5, then clamped to 23.0
    record(
        f0="104",
        f1="20250304061004",
        f5="806356045",
        f6="2",
        f7="302",
        f8="AC1",
        f9="1",
        f11="101",
        f13="5",
        f15="remote",
        f17="1",
        f18="302",
    ),
    record(
        f0="105",
        f1="20250304061004",
        f5="806356047",
        f6="2",
        f7="302",
        f8="AC1",
        f9="215",
        f11="101",
        f13="5",
        f15="remote",
        f17="1",
        f18="302",
    ),
    record(
        f0="106",
        f1="20250304061005",
        f5="806356047",
        f6="2",
        f7="302",
        f8="AC1",
        f9="230",
        f11="101",
        f13="14",
        f15="remote",
        f17="1",
        f18="302",
    ),
    # AC2: fan speed to auto, without a source; then setback low by the schedule
    record(
        f0="107",
        f1="20250304073118",
        f5="806356048",
        f6="2",
        f7="108",
        f8="AC2",
        f9="100",
        f11="101",
        f17="7",
        f18="108",
    ),
    record(
        f0="108",
        f1="20250304170000",
        f5="806356045",
        f6="2",
        f7="108",
        f8="AC2",
        f9="2",
        f11="101",
        f13="5",
        f15="remote",
        f17="7",
        f18="108",
    ),
    # an event code not known yet
    record(
        f0="109",
        f1="20250304170001",
        f5="605030469",
        f6="2",
        f7="108",
        f8="AC2",
        f11="101",
        f13="5",
        f17="7",
        f18="108",
    ),
]

CONFIG = (
    "1\n-60\n3 5 0 2\n10 5 0 3\n1741252327\n1741042800\n1741042800\n"
    "intelligent Touch Manager\nVer1.00.00\n102\n8 0 0 0 0 0 1\n0\nt_history.dat\n"
)


def history_archive(records: list[str] | None = None, *, config: bool = True) -> bytes:
    """Build the archive: the two members the parser reads, plus decoys."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("History/CSVOutputTool.exe", b"MZ placeholder")
        archive.writestr("History/DBdata/sflfs/FLASH/iTMAuth.dat.gz", b"secret")
        archive.writestr(
            "History/DBdata/t_history.dat", "\n".join(records or RECORDS) + "\n"
        )
        if config:
            archive.writestr("History/DBdata/conv.conf", CONFIG)
    return buffer.getvalue()
