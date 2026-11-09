"""Helpers shared by the ``itm`` sub-command modules."""

from __future__ import annotations

import os
import sys
import unicodedata
from datetime import date
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from .client import ItmClient
    from .energy_navigator import ShownValue
    from .system import SystemSettings


def safe(text: str) -> str:
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


def echo(text: str = "") -> None:
    """Print sanitized text."""
    print(safe(text))  # noqa: T201 - a command line tool prints


def print_plan(client: ItmClient) -> bool:
    """Print a dry run's plan; tell whether this was a dry run."""
    if not client.dry_run:
        return False
    echo("dry run, would send:")
    for line in client.planned:
        echo(f"  {line}")
    return True


def onoff(value: bool | None) -> str:  # noqa: FBT001 - a display helper
    """Return a flag as on, off or ?."""
    return "?" if value is None else "on" if value else "off"


def print_system(  # noqa: PLR0912  # pylint: disable=too-many-branches,too-many-locals
    st: SystemSettings,
) -> None:
    """Print the system settings, one section per screen that was read."""
    if st.zones is not None:
        echo(f"zones (shown from {st.zone_position}):")
        for z in st.zones:
            level = "-" if z.level is None else z.level
            echo(f"  {z.name!r:20} level {level}, {z.zones} zones, {z.points} points")
    if st.points is not None:
        echo("management points:")
        for p in st.points:
            echo(f"  {p.name:10} {p.kind:14} icon {p.icon}  {p.info or ''}")
    if st.maintenance is not None:
        busy = [m.name for m in st.maintenance if m.in_maintenance]
        echo(f"in maintenance: {', '.join(busy) or 'none'}")
    if (n := st.network) is not None:
        echo(
            f"network: {n.controller_name}, host {n.hostname}, {n.ip_address}/"
            f"{n.subnet_mask}, gateway {n.gateway}, DNS {n.dns_primary} "
            f"{n.dns_secondary}"
        )
    if (e := st.email) is not None:
        addresses = ", ".join(f"{a} ({onoff(on)})" for on, a in e.addresses)
        echo(f"e-mail: {addresses or 'no addresses'}")
    if st.web_users is not None:
        users = ", ".join(
            f"{u.name} ({'admin' if u.admin else u.kind})" for u in st.web_users
        )
        echo(f"web users: {users}")
    if (h := st.hardware) is not None:
        buzzer = "continuous" if h.buzzer_continuous else f"{h.buzzer_minutes} min"
        echo(
            f"touch panel: backlight {h.backlight}, buzzer volume {h.buzzer_volume} "
            f"for {buzzer}, key click volume {h.key_click_volume}"
        )
    if (v := st.screensaver) is not None:
        mode = v.mode.value if v.mode else "?"
        echo(
            f"screensaver: {mode} after {v.delay_minutes} min, "
            f"off on error {onoff(v.off_on_error)}"
        )
    if st.confirm_dialogs is not None:
        echo(f"confirm dialogs: {onoff(st.confirm_dialogs)}")
    if (r := st.remote_management) is not None:
        echo(
            f"remote management: {r.status} ({r.ip_address}), "
            f"connected {onoff(r.connected)}"
        )
    if (d := st.ppd) is not None:
        echo(
            f"PPD: {d.start} - {d.end}, time limit {onoff(d.time_limit)}, "
            f"calculation day {d.calculation_day}, {len(d.units)} units"
        )
        for u in d.units:
            echo(f"  {u.name:20} {u.consumption_kwh} kWh, standby {u.standby_kwh} kWh")
    if (g := st.energy_navigator) is not None:
        echo(
            f"energy navigator: sampling {g.sampling_from} - {g.sampling_to} "
            f"({g.sampling_target}), export periods {', '.join(g.export_periods)}"
        )


def date_arg(text: str | None) -> date | None:
    """Parse YYYY-MM-DD."""
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        sys.exit(f"a date is YYYY-MM-DD, not {text!r}")


def shown(value: ShownValue | None) -> str:
    """Return a shown value's text, trimmed ("" when not shown)."""
    return "" if value is None else value.text.strip()


def save(out: Path, data: bytes) -> None:
    """Write a download readable by its owner only (an archive can hold logins)."""
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(data)
