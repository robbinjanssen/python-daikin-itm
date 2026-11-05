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
    print(_safe(text))  # noqa: T201 - a command line tool prints


def _print_plan(client: ItmClient) -> bool:
    """Print a dry run's plan; tell whether this was a dry run."""
    if not client.dry_run:
        return False
    _out("dry run, would send:")
    for line in client.planned:
        _out(f"  {line}")
    return True


def _onoff(value: bool | None) -> str:  # noqa: FBT001 - a display helper
    return "?" if value is None else "on" if value else "off"


def _print_system(  # noqa: PLR0912  # pylint: disable=too-many-branches,too-many-locals
    st: SystemSettings,
) -> None:
    """Print the system settings, one section per screen that was read."""
    if st.zones is not None:
        _out(f"zones (shown from {st.zone_position}):")
        for z in st.zones:
            level = "-" if z.level is None else z.level
            _out(f"  {z.name!r:20} level {level}, {z.zones} zones, {z.points} points")
    if st.points is not None:
        _out("management points:")
        for p in st.points:
            _out(f"  {p.name:10} {p.kind:14} icon {p.icon}  {p.info or ''}")
    if st.maintenance is not None:
        busy = [m.name for m in st.maintenance if m.in_maintenance]
        _out(f"in maintenance: {', '.join(busy) or 'none'}")
    if (n := st.network) is not None:
        _out(
            f"network: {n.controller_name}, host {n.hostname}, {n.ip_address}/"
            f"{n.subnet_mask}, gateway {n.gateway}, DNS {n.dns_primary} "
            f"{n.dns_secondary}"
        )
    if (e := st.email) is not None:
        addresses = ", ".join(a for _, a in e.addresses) or "no addresses"
        _out(f"e-mail: {_onoff(e.enabled)}, {addresses}")
    if st.web_users is not None:
        users = ", ".join(
            f"{u.name} ({'admin' if u.admin else u.kind})" for u in st.web_users
        )
        _out(f"web users: {users}")
    if (h := st.hardware) is not None:
        buzzer = "continuous" if h.buzzer_continuous else f"{h.buzzer_minutes} min"
        _out(
            f"touch panel: backlight {h.backlight}, buzzer volume {h.buzzer_volume} "
            f"for {buzzer}, key click volume {h.key_click_volume}"
        )
    if (v := st.screensaver) is not None:
        mode = v.mode.value if v.mode else "?"
        _out(
            f"screensaver: {mode} after {v.delay_minutes} min, "
            f"off on error {_onoff(v.off_on_error)}"
        )
    if st.confirm_dialogs is not None:
        _out(f"confirm dialogs: {_onoff(st.confirm_dialogs)}")
    if (r := st.remote_management) is not None:
        _out(
            f"remote management: {r.status} ({r.ip_address}), "
            f"connected {_onoff(r.connected)}"
        )
    if (d := st.ppd) is not None:
        _out(
            f"PPD: {d.start} - {d.end}, time limit {_onoff(d.time_limit)}, "
            f"calculation day {d.calculation_day}, {len(d.units)} units"
        )
        for u in d.units:
            _out(f"  {u.name:20} {u.consumption_kwh} kWh, standby {u.standby_kwh} kWh")
    if (g := st.energy_navigator) is not None:
        _out(
            f"energy navigator: sampling {g.sampling_from} - {g.sampling_to} "
            f"({g.sampling_target}), export periods {', '.join(g.export_periods)}"
        )


def _date_arg(text: str | None) -> date | None:
    """Parse YYYY-MM-DD."""
    if text is None:
        return None
    try:
        return date.fromisoformat(text)
    except ValueError:
        sys.exit(f"a date is YYYY-MM-DD, not {text!r}")


def _shown(value: ShownValue | None) -> str:
    """Return a shown value's text, trimmed ("" when not shown)."""
    return "" if value is None else value.text.strip()


def _save(out: Path, data: bytes) -> None:
    """Write a download readable by its owner only (an archive can hold logins)."""
    fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as f:
        os.fchmod(f.fileno(), 0o600)
        f.write(data)
