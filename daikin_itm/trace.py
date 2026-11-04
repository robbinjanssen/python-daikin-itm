"""Record what a client sends to and receives from the iTM, for debugging.

Each request is written as its numbers only (screen, part, event, data type, number
and an integer array); text it carries (such as the login's username and password
hash) is never written. Each reply is written whole, with its page token blanked. A
reply still holds the installation's names, descriptions and settings, and from
some screens (such as the history) usernames and IP addresses.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .exceptions import ProtocolError
from .protocol import Reader

TOKEN_CHARS = 32
PREFIX = "snD="

Post = Callable[[str], Awaitable[str]]


def request_numbers(body: str) -> dict[str, Any]:
    """Return a request's numbers: [screen, part, event, data type, number], array.

    Args:
    ----
        body: The request body as posted ("snD=" + page token + the event in hex).

    Returns:
    -------
        {"request": [...], "array": [...]}, or {"request": None} when the body
        doesn't decode.

    """
    data = body.removeprefix(PREFIX)[TOKEN_CHARS:]
    try:
        r = Reader(data)
        header = [r.short(), r.short(), r.short(), r.short(), r.int()]
        array = [r.int() for _ in range(r.short())]
    except ProtocolError:
        return {"request": None}
    return {"request": header, "array": array}


class TraceFile:
    """Append one JSON line per request to a file only the owner can read."""

    def __init__(self, path: str | Path) -> None:
        """Open (create) the trace file.

        Args:
        ----
            path: Where to write; appended to when it exists.

        """
        self.path = Path(path)
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        os.close(fd)

    def write(self, entry: dict[str, Any]) -> None:
        """Append one entry."""
        with self.path.open("a", encoding="utf-8") as out:
            out.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def wrap(self, post: Post) -> Post:
        """Return a post function that records each request and reply.

        Args:
        ----
            post: The client's `_post`.

        Returns:
        -------
            A function with the same signature that records, then returns.

        """

        async def traced(body: str) -> str:
            entry: dict[str, Any] = {
                "time": datetime.now(UTC).isoformat(timespec="milliseconds"),
                **request_numbers(body),
            }
            started = time.monotonic()
            try:
                reply = await post(body)
            except Exception as err:
                entry["ms"] = round((time.monotonic() - started) * 1000)
                entry["error"] = f"{type(err).__name__}: {err}"
                self.write(entry)
                raise
            entry["ms"] = round((time.monotonic() - started) * 1000)
            entry["reply"] = "0" * TOKEN_CHARS + reply[TOKEN_CHARS:]
            self.write(entry)
            return reply

        return traced
