"""The trace: request numbers and token-less replies, never a request's text."""

from __future__ import annotations

import json
import stat
from typing import TYPE_CHECKING

import pytest

from daikin_itm import ItmError
from daikin_itm.protocol import DataType, EventType, encode_event
from daikin_itm.trace import TraceFile, request_numbers

if TYPE_CHECKING:
    from pathlib import Path

TOKEN = "A" * 32


def test_request_numbers_leave_out_text() -> None:
    """Only the header and the array are read; a string payload is not."""
    body = encode_event(
        TOKEN,
        1203,
        4,
        EventType.CLICK,
        DataType.STRING,
        2,
        array=(7, 8),
        string="user secret-hash",
    )
    assert request_numbers(body) == {
        "request": [1203, 4, EventType.CLICK, DataType.STRING, 2],
        "array": [7, 8],
    }
    assert request_numbers("snD=" + TOKEN + "zz") == {"request": None}


async def test_trace_file_records_and_passes_on(tmp_path: Path) -> None:
    """Each call is one JSON line; the reply's token is blanked, errors re-raised."""
    path = tmp_path / "trace.jsonl"
    trace = TraceFile(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    path.chmod(0o644)
    TraceFile(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600

    async def post(_body: str) -> str:
        return "B" * 32 + "0001"

    traced = trace.wrap(post)
    assert await traced(encode_event(TOKEN, 5, 53)) == "B" * 32 + "0001"

    async def broken(_body: str) -> str:
        msg = "boom"
        raise ItmError(msg)

    with pytest.raises(ItmError, match="boom"):
        await trace.wrap(broken)(encode_event(TOKEN, 0, 25))

    first, second = (json.loads(line) for line in path.read_text().splitlines())
    assert first["request"][:2] == [5, 53]
    assert first["reply"] == "0" * 32 + "0001"
    assert "B" * 32 not in path.read_text()
    assert second["request"][:2] == [0, 25]
    assert second["error"] == "ItmError: boom"
    assert "reply" not in second
