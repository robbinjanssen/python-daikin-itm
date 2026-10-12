"""Small CLI for debugging: ``itm status``, ``itm zones``, ``itm setpoint 1:1-04 21``.

Credentials come from ITM_USERNAME / ITM_PASSWORD, or --creds pointing at the JSON
written by ``op item get <id> --fields label=username,label=password --reveal
--format json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import sys
import unicodedata
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

import aiohttp

from .client import DEFAULT_ZONE_PATH, FileSessionStore, ItmClient
from .exceptions import ItmError
from .models import FAN_SPEEDS, MODES, Capabilities

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Sequence

    from .models import Unit

CACHE_DIR = Path.home() / ".cache" / "daikin-itm"
# Descriptions etc., refreshed when unit names change.
UNITS_CACHE = CACHE_DIR / "units.json"


def _safe(text: str) -> str:
    """Make text from the iTM safe to print.

    Names, descriptions and errors come from whatever answers at the iTM's address;
    replace control and format characters so they can't drive the terminal (escape
    sequences, hidden or rewritten lines).

    Args:
    ----
        text: The text to sanitize.

    Returns:
    -------
        The text with every control and format character replaced by ``?``.

    """
    return "".join(
        "?" if unicodedata.category(ch).startswith("C") else ch for ch in text
    )


def _out(text: str = "") -> None:
    """Print sanitized text."""
    print(_safe(text))


def _session_file(host: str) -> Path:
    """Return the default session file for a host."""
    safe_host = re.sub(r"[^A-Za-z0-9._-]", "_", host)
    return CACHE_DIR / f"session-{safe_host}.json"


def _credentials(path: str | None) -> tuple[str, str]:
    """Load the iTM credentials.

    Args:
    ----
        path: A JSON file with username/password fields, or None to use the
            ITM_USERNAME and ITM_PASSWORD environment variables.

    Returns:
    -------
        The username and password.

    """
    if path:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        fields = data if isinstance(data, list) else data.get("fields", [data])
        values = {f.get("label"): f.get("value") for f in fields}
        if not values.get("username") or not values.get("password"):
            sys.exit(f"{path} has no username and password fields")
        return values["username"], values["password"]
    try:
        return os.environ["ITM_USERNAME"], os.environ["ITM_PASSWORD"]
    except KeyError:
        sys.exit("set ITM_USERNAME and ITM_PASSWORD, or pass --creds")


def _load_caps(host: str) -> dict[str, Capabilities]:
    """Load the cached capabilities of a host's units."""
    try:
        data = json.loads(UNITS_CACHE.read_text(encoding="utf-8"))
        data = data.get(host, {}) if isinstance(data, dict) else {}
        return {
            name: Capabilities(**{**c, "zones": tuple(c.get("zones", ()))})
            for name, c in data.items()
        }
    except (FileNotFoundError, ValueError, TypeError):
        return {}


def _save_caps(host: str, caps: dict[str, Capabilities]) -> None:
    """Cache the capabilities of a host's units."""
    try:
        data = json.loads(UNITS_CACHE.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        data = {}
    if not isinstance(data, dict):  # not ours: start over
        data = {}
    data[host] = {name: asdict(c) for name, c in caps.items()}
    UNITS_CACHE.write_text(
        json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def _print_units(units: dict[str, Unit], caps: dict[str, Capabilities]) -> None:
    """Print a table of units."""
    _out(
        f"{'unit':14} {'description':22} {'power':8} {'mode':10} {'fan':7} "
        f"{'setpoint':>8} {'range':>7} {'room':>6}  error"
    )
    for u in units.values():
        rng = (
            f"{u.setpoint_range[0]}-{u.setpoint_range[-1]}" if u.setpoint_range else "-"
        )
        temp = f"{u.room_temperature:.1f}" if u.room_temperature is not None else "-"
        desc = caps[u.name].description if u.name in caps else None
        _out(
            f"{u.name:14} {desc or '-':22} {u.power or '-':8} "
            f"{u.mode_label or '-':10} {u.fan_speed or '-':7} "
            f"{u.setpoint or '-':>8} {rng:>7} {temp:>6}  {u.error_code or ''}"
        )


def _print_change(unit: str, u: Unit | None) -> None:
    """Print a unit's state after a change."""
    if u is not None:
        _out(
            f"{unit}: power={u.power} mode={u.mode_label} fan={u.fan_speed} "
            f"setpoint={u.setpoint}"
        )


async def _status(client: ItmClient, args: argparse.Namespace) -> None:
    """Show all units."""
    units = await client.fetch_units()
    caps = {} if args.refresh else _load_caps(args.host)
    if set(units) - set(caps):  # first run, or a unit was added/renamed in the iTM
        caps = await client.fetch_capabilities()
        _save_caps(args.host, caps)
    _print_units(units, caps)


async def _zones(client: ItmClient, args: argparse.Namespace) -> None:
    """List the zones and units at a path."""
    path = tuple(args.path.split("/")) if args.path else ()
    for name in await client.list_zones(path):
        _out(name)


async def _setpoint(client: ItmClient, args: argparse.Namespace) -> None:
    """Change a unit's setpoint."""
    # Returns the unit as read back once the change shows; refuses stopped units.
    u = await client.set_setpoint(args.unit, args.value)
    _out(f"{args.unit}: setpoint now {u.setpoint if u else None}")


async def _caps(client: ItmClient, args: argparse.Namespace) -> None:
    """Show the modes and fan control each unit offers."""
    all_caps = await client.fetch_capabilities()
    _save_caps(args.host, all_caps)
    for name, caps in all_caps.items():
        _out(
            f"{name:14} {caps.address or '-':8} id={caps.point_id or '-':5} "
            f"modes={','.join(caps.modes) or '-':20} "
            f"fan={'yes' if caps.fan_speed else 'no':4} "
            f"{caps.description or ''}"
        )


async def _power(client: ItmClient, args: argparse.Namespace) -> None:
    """Start or stop a unit."""
    _print_change(args.unit, await client.set_power(args.unit, args.state == "on"))


async def _mode(client: ItmClient, args: argparse.Namespace) -> None:
    """Change a unit's operation mode."""
    _print_change(args.unit, await client.set_mode(args.unit, args.mode))


async def _fan(client: ItmClient, args: argparse.Namespace) -> None:
    """Change a unit's fan speed."""
    _print_change(args.unit, await client.set_fan_speed(args.unit, args.speed))


COMMANDS: dict[str, Callable[[ItmClient, argparse.Namespace], Awaitable[None]]] = {
    "status": _status,
    "zones": _zones,
    "setpoint": _setpoint,
    "caps": _caps,
    "power": _power,
    "mode": _mode,
    "fan": _fan,
}


async def _run(args: argparse.Namespace) -> None:
    """Run the command given on the command line."""
    username, password = _credentials(args.creds)
    # Owner-only: it holds the session token while a command runs (and after a
    # failed logout).
    CACHE_DIR.mkdir(mode=0o700, parents=True, exist_ok=True)
    CACHE_DIR.chmod(0o700)
    zone_path = tuple(args.zone.split("/")) if args.zone else DEFAULT_ZONE_PATH
    async with ItmClient(
        args.host,
        username,
        password,
        store=FileSessionStore(
            str(args.session_file or _session_file(args.host)), args.host
        ),
        zone_path=zone_path,
    ) as client:
        await COMMANDS[args.command](client, args)


def _parser() -> argparse.ArgumentParser:
    """Build the argument parser."""
    p = argparse.ArgumentParser(prog="itm", description=(__doc__ or "").splitlines()[0])
    p.add_argument(
        "--host",
        default=os.environ.get("ITM_HOST"),
        help="address of the iTM (default: $ITM_HOST)",
    )
    p.add_argument(
        "--creds", help="JSON file with username/password fields (1Password export)"
    )
    p.add_argument(
        "--zone",
        help="zone path to the units, e.g. 'Building/Indoor units' "
        "(default: the top level)",
    )
    p.add_argument(
        "--session-file",
        help=f"where to keep the session token (default: per host in {CACHE_DIR})",
    )
    sub = p.add_subparsers(dest="command", required=True)
    st = sub.add_parser("status", help="show all units")
    st.add_argument(
        "--refresh",
        action="store_true",
        help="re-read descriptions instead of using the cache",
    )
    z = sub.add_parser("zones", help="list zones/units at a path")
    z.add_argument("path", nargs="?", help="e.g. 'Building'")
    s = sub.add_parser("setpoint", help="change a unit's setpoint")
    s.add_argument("unit")
    s.add_argument("value", type=int)
    sub.add_parser(
        "caps",
        help="modes and fan control each unit offers (opens each settings dialog)",
    )
    w = sub.add_parser("power", help="start or stop a unit")
    w.add_argument("unit")
    w.add_argument("state", choices=["on", "off"])
    m = sub.add_parser("mode", help="change a unit's operation mode")
    m.add_argument("unit")
    m.add_argument("mode", choices=sorted(set(MODES.values())))
    f = sub.add_parser("fan", help="change a unit's fan speed")
    f.add_argument("unit")
    f.add_argument("speed", choices=FAN_SPEEDS)
    return p


def main(argv: Sequence[str] | None = None) -> None:
    """Run the ``itm`` command line tool.

    Args:
    ----
        argv: The arguments, defaults to the ones the process was started with.

    """
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.host:
        parser.error("set the iTM's address with --host or ITM_HOST")

    async def runner() -> None:
        task = asyncio.current_task()
        if task is not None:
            # SIGTERM/SIGHUP cancel the task, so the client's finally-blocks still
            # log out.
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGTERM, signal.SIGHUP):
                loop.add_signal_handler(sig, task.cancel)
        await _run(args)

    try:
        asyncio.run(runner())
    except ItmError as e:
        sys.exit(_safe(f"error: {e}"))
    except (aiohttp.ClientError, TimeoutError) as e:
        reason = str(e) or type(e).__name__
        sys.exit(_safe(f"error: could not reach the iTM at {args.host}: {reason}"))
    except (KeyboardInterrupt, asyncio.CancelledError):
        sys.exit(130)


if __name__ == "__main__":
    main()
