"""The ``itm layout`` and ``itm points-registry`` sub-commands (read only).

Kept apart from ``cli.py``, which adds them with ``add_layout_commands`` and runs
them from ``LAYOUT_COMMANDS``.
"""

from __future__ import annotations

import os
import sys
from typing import TYPE_CHECKING, Any

from .cli_common import echo as _out

if TYPE_CHECKING:
    import argparse
    from collections.abc import Awaitable, Callable

    from .client import ItmClient
    from .layout import Layout, LayoutItem


def _item_line(item: LayoutItem) -> str:
    where = f"{item.x:>5},{item.y:<5}"
    if not item.icon:
        return f"  text  {where} {item.text!r}"
    kind = item.kind.value if item.kind is not None else "?"
    name = f"  {item.name}" if item.name else ""
    return (
        f"  icon  {where} icon {item.icon_id} mode {item.icon_mode} "
        f"size {item.icon_size} ({kind}){name}"
    )


def _print_layout(layout: Layout) -> None:
    _out(
        f"layout: {layout.title or '-'}"
        + (f" ({layout.image})" if layout.image else "")
    )
    if layout.empty:
        _out("  no layout configured")
    for item in layout.items:
        _out(_item_line(item))
    if layout.screens:
        _out(f"linked screens: {', '.join(layout.screens)}")


async def _layout(client: ItmClient, args: argparse.Namespace) -> None:
    """Show the graphical layout view."""
    _print_layout(await client.fetch_layout(names=args.names))


async def _points_registry(client: ItmClient, args: argparse.Namespace) -> None:
    """Show the management point registration (service; ITM_SERVICE_PASSWORD)."""
    password = os.environ.get("ITM_SERVICE_PASSWORD")
    if not password:
        sys.exit("set ITM_SERVICE_PASSWORD to the iTM's service password")
    points = await client.fetch_point_registrations(password, details=args.details)
    if not points:
        _out("no management points registered")
    for p in points:
        _out(f"{p.type:16} {p.name:20} {p.address:10} {p.info}".rstrip())
        if p.attributes is not None:
            for label, text in p.attributes.fields:
                _out(f"    {label or '?'}: {text}")


def add_layout_commands(sub: Any) -> None:
    """Add `layout` and `points-registry` to the parser."""
    ly = sub.add_parser("layout", help="the graphical layout view (read only)")
    ly.add_argument(
        "--names",
        action="store_true",
        help="select each icon to read its name (one request per icon)",
    )
    pr = sub.add_parser(
        "points-registry",
        help="the installer's management point registration (read only; "
        "ITM_SERVICE_PASSWORD)",
    )
    pr.add_argument(
        "--details",
        action="store_true",
        help="also read each point's attributes screen (Edit, then Cancel)",
    )


LAYOUT_COMMANDS: dict[str, Callable[[ItmClient, argparse.Namespace], Awaitable[None]]]
LAYOUT_COMMANDS = {
    "layout": _layout,
    "points-registry": _points_registry,
}
