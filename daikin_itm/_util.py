"""Helpers shared by the library's modules."""

from __future__ import annotations

import asyncio
import functools
import logging
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Concatenate, ParamSpec, Protocol, TypeVar

from .exceptions import ItmCommandError, NotConfirmed, RequestSkipped

if TYPE_CHECKING:
    from collections.abc import (
        AsyncIterator,
        Awaitable,
        Callable,
        Coroutine,
        Iterable,
    )
    from contextlib import AbstractAsyncContextManager

T = TypeVar("T")
_P = ParamSpec("_P")
_LOGGER = logging.getLogger(__name__)


class _WritesInOneLogin(Protocol):  # pylint: disable=too-few-public-methods
    def _write_session(self) -> AbstractAsyncContextManager[None]: ...


_C = TypeVar("_C", bound=_WritesInOneLogin)


def one_login(
    method: Callable[Concatenate[_C, _P], Awaitable[T]],
) -> Callable[Concatenate[_C, _P], Coroutine[Any, Any, T]]:
    """Run a public write and its read-back in one login (confirm_in_session).

    The write's guards (read only, experimental, a shared session) still run
    first inside the method: opening the block sends nothing.

    Args:
    ----
        method: The write.

    Returns:
    -------
        The write, run inside ItmClient._write_session().

    """

    @functools.wraps(method)
    async def run(self: _C, /, *args: _P.args, **kwargs: _P.kwargs) -> T:
        async with self._write_session():  # pylint: disable=protected-access
            return await method(self, *args, **kwargs)

    return run


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


@asynccontextmanager
async def cleanup(*steps: Callable[[], Awaitable[object]]) -> AsyncIterator[None]:
    """Run clean-up steps (close a screen or dialog, log out) after the block.

    The steps run in order, also when the block raised or was cancelled; the first
    one that fails ends the clean-up. After a block that returned, a failed step
    raises its error. After a block that raised, the block's error (or
    cancellation) goes on unchanged: the step's error is logged and added to it as
    a note. A step that is itself cancelled raises the cancellation.

    Args:
    ----
        steps: The clean-up, each a call that returns what to await.

    Raises:
    ------
        Exception: The block returned and a step failed: that step's error.

    """
    try:
        yield
    except BaseException as err:
        await cleanup_after(err, *steps)
        raise
    for step in steps:
        await step()


async def cleanup_after(
    err: BaseException, *steps: Callable[[], Awaitable[object]]
) -> None:
    """Run clean-up steps while err goes on; a failed step never replaces it.

    The steps run in order; the first one that fails ends them, is logged and is
    added to err as a note. A step that wasn't sent because the iTM didn't answer
    before (RequestSkipped) ends them quietly. A step that is itself cancelled
    raises the cancellation. The caller raises err afterwards.

    Args:
    ----
        err: The error or cancellation on its way out.
        steps: The clean-up, each a call that returns what to await.

    """
    try:
        for step in steps:
            await step()
    except RequestSkipped:
        _LOGGER.debug("clean-up skipped while %r went on", err)
        err.add_note("clean-up skipped: the iTM didn't answer")
    except Exception as failed:  # pylint: disable=broad-exception-caught
        _LOGGER.warning(
            "clean-up failed while %r went on: %s", err, failed, exc_info=failed
        )
        err.add_note(f"clean-up failed as well: {failed!r}")


def check_wrong(name: str, wrong: list[str], result: object) -> None:
    """Raise when a read-back shows values the iTM did not take.

    Raises
    ------
        NotConfirmed: Wrong names values the read-back (result) doesn't show.

    """
    if wrong:
        msg = f"{name}: the iTM did not take the {', '.join(wrong)}: {result}"
        raise NotConfirmed(msg, name=name, last_seen=result)


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
