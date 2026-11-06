"""The ``itm`` sub-commands for the system settings screens and the zone tree."""

from __future__ import annotations

import getpass
import os
import sys
from typing import TYPE_CHECKING, Any

from .cli_common import echo as _out
from .cli_common import onoff as _onoff
from .cli_common import print_plan as _print_plan
from .cli_common import print_system as _print_system
from .system import LANGUAGE_RADIOS, ScreensaverMode

if TYPE_CHECKING:
    import argparse
    from collections.abc import Awaitable, Callable

    from .client import ItmClient


async def _hardware(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the touch panel's backlight, buzzer and key click."""
    minutes = args.buzzer_minutes
    if any(
        v is not None
        for v in (args.backlight, args.buzzer_volume, minutes, args.key_click_volume)
    ):
        await client.set_hardware(
            backlight=args.backlight,
            buzzer_volume=args.buzzer_volume,
            buzzer_minutes=None if minutes in {None, "continuous"} else int(minutes),
            buzzer_continuous=minutes == "continuous",
            key_click_volume=args.key_click_volume,
        )
        if _print_plan(client):
            return
    _print_system(await client.fetch_system(["hardware"]))


async def _screensaver(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the touch panel's screensaver."""
    off_on_error = {"on": True, "off": False}.get(args.off_on_error or "")
    if args.mode or args.delay is not None or off_on_error is not None:
        await client.set_screensaver(
            mode=ScreensaverMode(args.mode) if args.mode else None,
            delay_minutes=args.delay,
            off_on_error=off_on_error,
        )
        if _print_plan(client):
            return
    _print_system(await client.fetch_system(["screensaver"]))


async def _maintenance(client: ItmClient, args: argparse.Namespace) -> None:
    """Put a point into maintenance or take it out."""
    state = await client.set_maintenance(args.point, on=args.state == "on")
    if not _print_plan(client):
        _out(f"{args.point}: maintenance {_onoff(state)}")


async def _point(client: ItmClient, args: argparse.Namespace) -> None:
    """Change a management point's description (or name)."""
    point = await client.set_point_attributes(
        args.point, name=args.name, info=args.info
    )
    if not _print_plan(client) and point is not None:
        _out(f"{point.name}: {point.info or ''}")


async def _zone(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change a zone's description, intervals (or name)."""
    changes = (args.name, args.info, args.start_interval, args.stop_interval)
    if all(v is None for v in changes):
        z = await client.fetch_zone_attributes(args.zone)
    else:
        result = await client.set_zone_attributes(
            args.zone,
            name=args.name,
            info=args.info,
            start_interval=args.start_interval,
            stop_interval=args.stop_interval,
        )
        if _print_plan(client) or result is None:
            return
        z = result
    _out(
        f"{z.name}: {z.info or ''} (start interval {z.start_interval}, "
        f"stop interval {z.stop_interval})"
    )


def _new_password() -> str:
    """Read a new password from ITM_NEW_PASSWORD or ask for it (never echoed)."""
    return os.environ.get("ITM_NEW_PASSWORD") or getpass.getpass("new password: ")


async def _user(client: ItmClient, args: argparse.Namespace) -> None:
    """Add, change or delete a web access user."""
    admin = {"admin": True, "user": False}.get(args.type or "")
    if args.action == "add":
        if not args.manage_zone:
            sys.exit("a new web user needs at least one --manage-zone")
        user = await client.create_web_user(
            args.name, _new_password(), zones=tuple(args.manage_zone), admin=bool(admin)
        )
    elif args.action == "set":
        password = _new_password() if args.password else None
        user = await client.set_web_user(
            args.name,
            password=password,
            admin=admin,
            zones=tuple(args.manage_zone) if args.manage_zone else None,
        )
    elif args.action == "screens":
        await _user_screens(client, args)
        return
    else:
        await client.delete_web_user(args.name)
        if not _print_plan(client):
            _out(f"{args.name}: deleted")
        return
    if not _print_plan(client) and user is not None:
        _out(f"{user.name}: {'admin' if user.admin else 'user'}")


async def _user_screens(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or set the screens a normal web user may open."""
    if args.screen:
        await client.set_web_user_screens(args.name, args.screen)
        if _print_plan(client):
            return
    screens = await client.fetch_web_user_screens(args.name)
    _out(f"{args.name}: screens {', '.join(screens.allowed) or 'none'}")
    _out(f"  available: {', '.join(screens.available) or 'none'}")


async def _own_settings(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the logged-in web user's own settings."""
    formats = {"dmy": "%d/%m/%Y", "mdy": "%m/%d/%Y", "ymd": "%Y/%m/%d"}
    changes: dict[str, Any] = {
        "date_format": formats.get(args.date_format or ""),
        "time_24h": {"24h": True, "12h": False}.get(args.time or ""),
        "decimal_comma": {"point": False, "comma": True}.get(args.decimal or ""),
        "icon_colour": args.icon_colour,
        "language": args.language,
        "password": _new_password() if args.password else None,
    }
    if any(v is not None for v in changes.values()):
        await client.set_own_settings(
            **changes, allow_password_change=args.allow_password_change
        )
        if _print_plan(client):
            return
    own = await client.fetch_own_settings()
    time = None if own.time_24h is None else ("24h" if own.time_24h else "12h")
    decimal = (
        None
        if own.decimal_comma is None
        else ("comma" if own.decimal_comma else "point")
    )
    _out(
        f"own settings: language {own.language}, date {own.date_format}, "
        f"time {time}, decimal {decimal}, icon colour {own.icon_colour}"
    )


async def _language(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or switch the iTM's display language."""
    if args.code:
        await client.set_language(args.code)
        if _print_plan(client):
            return
    _out(f"language: {await client.fetch_language()}")


async def _region(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the date, time, temperature and decimal formats."""
    formats = {"dmy": "%d/%m/%Y", "mdy": "%m/%d/%Y", "ymd": "%Y/%m/%d"}
    date_format = formats.get(args.date_format or "")
    time_24h = {"24h": True, "12h": False}.get(args.time or "")
    unit = {"c": "°C", "f": "°F"}.get(args.temperature or "")
    decimal = {"point": ".", "comma": ","}.get(args.decimal or "")
    if any(v is not None for v in (date_format, time_24h, unit, decimal)):
        await client.set_region(
            date_format=date_format,
            time_24h=time_24h,
            temperature_unit=unit,
            decimal_separator=decimal,
        )
        if _print_plan(client):
            return
    info = await client.fetch_info()
    clock = "24h" if info.time_24h else "12h"
    _out(
        f"region: dates {info.date_format}, {clock}, {info.temperature_unit}, "
        f"decimal {info.decimal_separator!r}"
    )


async def _dst(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or switch summer time (on: the EU rule)."""
    if args.state:
        await client.set_dst(enabled=args.state == "on")
        if _print_plan(client):
            return
    d = await client.fetch_dst()
    _out(f"summer time: {_onoff(d.enabled)}, from {d.start} to {d.end}")


async def _clock(client: ItmClient, args: argparse.Namespace) -> None:
    """Show the iTM's clock, or set it to this computer's time."""
    if args.set_now:
        await client.set_clock()
        if _print_plan(client):
            return
    info = await client.fetch_info()
    _out(f"clock: {info.clock} (off by {info.clock_offset})")


async def _network(client: ItmClient, args: argparse.Namespace) -> None:
    """Show the network settings, or plan (or, explicitly allowed, make) a change."""
    fields = {
        "controller_name": args.name,
        "hostname": args.hostname,
        "ip_address": args.ip,
        "subnet_mask": args.mask,
        "gateway": args.gateway,
        "dns_primary": args.dns1,
        "dns_secondary": args.dns2,
    }
    changes = {k: v for k, v in fields.items() if v is not None}
    if changes:
        await client.set_network(
            **changes, allow_network_change=args.allow_network_change
        )
        if _print_plan(client):
            return
    _print_system(await client.fetch_system(["network"]))


async def _zone_structure(client: ItmClient, args: argparse.Namespace) -> None:
    """Add, delete or move a zone, change its members or icon, or a point's icon."""
    command = args.command
    if command == "zone-add":
        await client.create_zone(args.name, parent=args.parent)
    elif command == "zone-delete":
        await client.delete_zone(args.name)
    elif command == "zone-move":
        await client.move_zone(args.name, args.parent)
    elif command == "zone-icon":
        await client.set_zone_icon(args.name, args.icon)
    elif command == "point-icon":
        await client.set_point_icon(args.name, args.icon)
    elif args.add or args.remove:
        current = await client.fetch_zone_members(args.name)
        wanted = [m for m in current if m not in (args.remove or [])]
        wanted += [m for m in args.add or [] if m not in wanted]
        await client.set_zone_members(args.name, wanted)
    if command == "zone-members" and args.order:
        await client.set_zone_member_order(args.name, args.order, use_jump=args.jump)
    if _print_plan(client):
        return
    if command == "zone-members":
        members = await client.fetch_zone_members(args.name)
        _out(f"{args.name}: {', '.join(members) or 'no members'}")
    elif command == "point-icon":
        _out(f"{args.name}: icon {args.icon}")
    else:
        _print_system(await client.fetch_system(["zones"]))


async def _email(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or change the e-mail notification: addresses, on/off."""
    action = args.action
    if action == "add":
        await client.add_email_address(args.address, args.point or [])
    elif action == "edit":
        await client.edit_email_address(
            args.address, new_address=args.new_address, points=args.point
        )
    elif action == "delete":
        await client.delete_email_address(args.address)
    elif action in {"on", "off"}:
        await client.set_email_enabled(enabled=action == "on")
    if _print_plan(client):
        return
    _print_system(await client.fetch_system(["email"]))
    if action == "show":
        server = await client.fetch_email_server()
        _out("e-mail server: " + ", ".join(f"{k} {v}" for k, v in server.items()))


async def _confirm_dialogs(client: ItmClient, args: argparse.Namespace) -> None:
    """Show or switch the iTM's "confirm settings" dialogs."""
    if args.state:
        await client.set_confirm_dialogs(enabled=args.state == "on")
        if _print_plan(client):
            return
    _print_system(await client.fetch_system(["confirm_dialogs"]))


async def _system(client: ItmClient, args: argparse.Namespace) -> None:
    """Show the system settings screens."""
    only = args.only.split(",") if args.only else None
    _print_system(await client.fetch_system(only))


def _add_system_commands(sub: Any) -> None:
    """Add the commands for the system settings screens."""
    sy = sub.add_parser(
        "system",
        help="system settings: zones, points, network, users, touch panel, PPD",
    )
    sy.add_argument("--only", help="comma-separated fields, e.g. network,hardware")
    hw = sub.add_parser(
        "hardware", help="show or set the touch panel's backlight and buzzer"
    )
    hw.add_argument("--backlight", type=int, help="1 to 8")
    hw.add_argument("--buzzer-volume", type=int, help="0 to 5")
    hw.add_argument("--buzzer-minutes", choices=["1", "3", "5", "continuous"])
    hw.add_argument("--key-click-volume", type=int, help="0 to 5")
    sv = sub.add_parser("screensaver", help="show or set the touch panel's screensaver")
    sv.add_argument("--mode", choices=[m.value for m in ScreensaverMode])
    sv.add_argument("--delay", type=int, help="minutes")
    sv.add_argument("--off-on-error", choices=["on", "off"])
    mt = sub.add_parser("maintenance", help="put a point into maintenance or out")
    mt.add_argument("point")
    mt.add_argument("state", choices=["on", "off"])
    pt = sub.add_parser("point", help="change a point's description (or name)")
    pt.add_argument("point")
    pt.add_argument("--info", help="the description, 0-50 characters")
    pt.add_argument("--name", help="a new name (changes how it is found!)")
    zn = sub.add_parser("zone", help="show or change a zone's settings")
    zn.add_argument("zone")
    zn.add_argument("--info")
    zn.add_argument("--name", help="a new name (changes every zone path!)")
    zn.add_argument("--start-interval", type=int)
    zn.add_argument("--stop-interval", type=int)
    us = sub.add_parser(
        "user",
        help="add, change or delete a web user (password from ITM_NEW_PASSWORD "
        "or a prompt)",
    )
    us.add_argument("action", choices=["add", "set", "delete", "screens"])
    us.add_argument("name")
    us.add_argument("--type", choices=["admin", "user"])
    us.add_argument("--password", action="store_true", help="set a new password")
    us.add_argument(
        "--manage-zone", action="append", help="a zone the user manages (repeatable)"
    )
    us.add_argument(
        "--screen",
        action="append",
        help="screens: a screen a normal user may open (repeatable; sets them all)",
    )
    _add_own_settings_command(sub)
    lg = sub.add_parser("language", help="show or switch the display language")
    lg.add_argument("code", nargs="?", choices=list(LANGUAGE_RADIOS))


def _add_region_commands(sub: Any) -> None:
    """Add the region, summer time and clock commands."""
    rg = sub.add_parser("region", help="show or change date/time/unit/decimal formats")
    rg.add_argument("--date-format", choices=["dmy", "mdy", "ymd"])
    rg.add_argument("--time", choices=["24h", "12h"])
    rg.add_argument("--temperature", choices=["c", "f"])
    rg.add_argument("--decimal", choices=["point", "comma"])
    ds = sub.add_parser("dst", help="show or switch summer time (EU rule)")
    ds.add_argument("state", nargs="?", choices=["on", "off"])
    nw = sub.add_parser(
        "network",
        help="show the network settings, or (with --dry-run) plan a change",
    )
    for opt in (
        "--name",
        "--hostname",
        "--ip",
        "--mask",
        "--gateway",
        "--dns1",
        "--dns2",
    ):
        nw.add_argument(opt)
    nw.add_argument(
        "--allow-network-change",
        action="store_true",
        help="really change it: a wrong value strands the iTM until someone is on site",
    )
    ck = sub.add_parser("clock", help="show the iTM's clock, or set it")
    ck.add_argument("--set-now", action="store_true", help="to this computer's time")


def _add_zone_commands(sub: Any) -> None:
    """Add the zone structure and icon commands."""
    za = sub.add_parser("zone-add", help="add an empty zone")
    za.add_argument("name")
    za.add_argument("--parent", help="the zone to add it below (default: top level)")
    zd = sub.add_parser("zone-delete", help="delete an empty zone")
    zd.add_argument("name")
    zm = sub.add_parser("zone-move", help="move a zone below another (paths change!)")
    zm.add_argument("name")
    zm.add_argument("parent", help="the new parent zone, or the root for the top")
    zb = sub.add_parser("zone-members", help="show or change a zone's points")
    zb.add_argument("name")
    zb.add_argument("--add", action="append", help="a point to add (repeatable)")
    zb.add_argument("--remove", action="append", help="a point to remove (repeatable)")
    zb.add_argument(
        "--order",
        nargs="+",
        metavar="POINT",
        help="put these members first, in this order",
    )
    zb.add_argument(
        "--jump", action="store_true", help="--order: move with Jump, not up"
    )
    zi = sub.add_parser("zone-icon", help="give a zone another icon (1000-1009)")
    zi.add_argument("name")
    zi.add_argument("icon", type=int)
    pi = sub.add_parser("point-icon", help="give a point another icon of its kind")
    pi.add_argument("name")
    pi.add_argument("icon", type=int)


def _add_own_settings_command(sub: Any) -> None:
    """Add `own-settings [--date-format ...] [--password]`."""
    ow = sub.add_parser(
        "own-settings",
        help="show or change the logged-in user's own settings (new password from "
        "ITM_NEW_PASSWORD or a prompt)",
    )
    ow.add_argument("--date-format", choices=["dmy", "mdy", "ymd"])
    ow.add_argument("--time", choices=["24h", "12h"])
    ow.add_argument("--decimal", choices=["point", "comma"])
    ow.add_argument("--icon-colour", type=int, choices=[1, 2])
    ow.add_argument("--language", choices=list(LANGUAGE_RADIOS))
    ow.add_argument("--password", action="store_true", help="set a new password")
    ow.add_argument(
        "--allow-password-change",
        action="store_true",
        help="--password: needed, a wrong password locks the client out",
    )


def _add_email_commands(sub: Any) -> None:
    """Add the e-mail notification and confirm dialog commands."""
    em = sub.add_parser("email", help="show or change the e-mail notification")
    em.add_argument(
        "action",
        nargs="?",
        default="show",
        choices=["show", "add", "edit", "delete", "on", "off"],
    )
    em.add_argument("address", nargs="?", help="the e-mail address (add/edit/delete)")
    em.add_argument("--point", action="append", help="a point it reports on")
    em.add_argument("--new-address", help="edit: the new address")
    cd = sub.add_parser("confirm-dialogs", help="show or switch the confirm dialogs")
    cd.add_argument("state", nargs="?", choices=["on", "off"])


def add_system_commands(sub: Any) -> None:
    """Add the system settings, region, zone and e-mail commands."""
    _add_system_commands(sub)
    _add_region_commands(sub)
    _add_zone_commands(sub)
    _add_email_commands(sub)


SYSTEM_COMMANDS: dict[str, Callable[[ItmClient, argparse.Namespace], Awaitable[None]]]
SYSTEM_COMMANDS = {
    "system": _system,
    "hardware": _hardware,
    "email": _email,
    "confirm-dialogs": _confirm_dialogs,
    "zone-add": _zone_structure,
    "zone-delete": _zone_structure,
    "zone-move": _zone_structure,
    "zone-members": _zone_structure,
    "zone-icon": _zone_structure,
    "point-icon": _zone_structure,
    "network": _network,
    "maintenance": _maintenance,
    "point": _point,
    "zone": _zone,
    "user": _user,
    "own-settings": _own_settings,
    "language": _language,
    "dst": _dst,
    "region": _region,
    "clock": _clock,
    "screensaver": _screensaver,
}
