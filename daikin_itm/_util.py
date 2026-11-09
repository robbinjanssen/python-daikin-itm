"""Helpers shared by the library's modules."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, TypeVar

from .exceptions import ItmCommandError

if TYPE_CHECKING:
    from collections.abc import Awaitable, Coroutine, Iterable

T = TypeVar("T")


async def ignore(call: Awaitable[Any]) -> None:
    """Await a call and drop its result."""
    await call


async def run_to_completion(coro: Coroutine[Any, Any, T]) -> T:
    """Run cleanup to the end even if the caller is cancelled meanwhile.

    Each request is bounded by the HTTP timeout, so this ends. The cancellation is
    passed on once the cleanup is done. A plain asyncio.shield would return at once
    on cancellation and leave the cleanup running unsupervised.

    Args:
    ----
        coro: The cleanup to run.

    Returns:
    -------
        What the cleanup returned.

    Raises:
    ------
        asyncio.CancelledError: The caller was cancelled while the cleanup ran.

    """
    task = asyncio.ensure_future(coro)
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():  # the cleanup itself was cancelled, not just us
                raise
            cancelled = True
            continue
        except BaseException as e:
            if cancelled:
                raise asyncio.CancelledError from e
            raise
        if cancelled:
            raise asyncio.CancelledError
        return result


def check_wrong(name: str, wrong: list[str], result: object) -> None:
    """Raise when a read-back shows values the iTM did not take."""
    if wrong:
        msg = f"{name}: the iTM did not take the {', '.join(wrong)}: {result}"
        raise ItmCommandError(msg)


def field_names(
    only: Iterable[str] | None, known: Iterable[str], what: str
) -> frozenset[str] | None:
    """Check the names of the fields to read; None means the caller's default.

    Raises
    ------
        ItmCommandError: Only is a string, or a name is not one of the known ones.

    """
    if only is None:
        return None
    known = tuple(known)
    if isinstance(only, str):
        msg = f"{what}: pass a list of names, not the string {only!r}"
        raise ItmCommandError(msg)
    names = frozenset(only)
    if unknown := names - frozenset(known):
        msg = f"unknown {what} {sorted(unknown)}; known: {known}"
        raise ItmCommandError(msg)
    return names
