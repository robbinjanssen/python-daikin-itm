"""Helpers shared by the library's modules."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Awaitable

T = TypeVar("T")


async def ignore(call: Awaitable[Any]) -> None:
    """Await a call and drop its result."""
    await call
