"""A dry run of a guarded write needs the same opt-in as the write itself."""

from __future__ import annotations

from typing import Any

import pytest

from daikin_itm import ItmCommandError

from .fake_itm import FakeITM
from .test_client import FakeClient

SERVICE = "test-service"

# Every write behind an opt-in of its own: arguments, the other opt-ins, its own.
OPT_INS: list[tuple[str, tuple[Any, ...], dict[str, Any], str]] = [
    ("set_network", (), {"dns_secondary": "10.0.0.9"}, "allow_network_change"),
    ("set_web_server_port", (8080,), {}, "allow_network_change"),
    ("set_remote_management", (), {"connect": True}, "allow_remote_change"),
    ("send_test_email", ("user@example.com",), {}, "allow_send"),
    (
        "set_emergency_stop_enabled",
        ("Program 1",),
        {"enabled": False},
        "allow_safety_change",
    ),
    ("release_emergency_stop", ("Program 1",), {}, "allow_safety_change"),
    (
        "set_energy_room_temperature",
        (SERVICE, "1:1-00"),
        {"allow_restart": True},
        "allow_service_change",
    ),
    (
        "set_energy_collection_start",
        (SERVICE, "April"),
        {"allow_service_change": True},
        "allow_restart",
    ),
    ("disable_energy_saving", (SERVICE,), {}, "allow_service_change"),
    ("set_demand_pulse_setup", (SERVICE,), {}, "allow_service_change"),
    (
        "set_demand_interlock_ports",
        (SERVICE, {2: "Port 3"}),
        {},
        "allow_service_change",
    ),
    (
        "set_leakage_check_indoor_units",
        (SERVICE, "OU-1", ()),
        {},
        "allow_service_change",
    ),
    ("set_leakage_check_schedule", (SERVICE, "OU-1", 1), {}, "allow_service_change"),
    (
        "copy_leakage_check_schedule",
        (SERVICE, "OU-1", ("OU-2",)),
        {},
        "allow_service_change",
    ),
    ("start_leakage_check", (SERVICE, "OU-1"), {}, "allow_leakage_check"),
    ("stop_leakage_check", (SERVICE, "OU-1"), {}, "allow_leakage_check"),
    ("run_layout_setup", (SERVICE,), {}, "allow_service_change"),
    ("set_r32_output_port", (SERVICE, 1, "Alarm out A"), {}, "allow_service_change"),
    ("set_password_setting", ("administrator",), {}, "allow_password_change"),
    ("set_own_settings", (), {"password": "new-secret"}, "allow_password_change"),
]


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize(("method", "args", "kwargs", "flag"), OPT_INS)
async def test_refused_before_anything_is_sent(
    method: str,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
    flag: str,
    *,
    dry_run: bool,
) -> None:
    """Without its opt-in a guarded write sends nothing, also in a dry run."""
    itm = FakeITM()
    client = FakeClient(itm, dry_run=dry_run)
    with pytest.raises(ItmCommandError, match=flag):
        await getattr(client, method)(*args, **kwargs)
    assert not itm.requests
    assert not client.planned
