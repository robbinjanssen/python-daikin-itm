# pylint: disable=redefined-outer-name
"""The system settings screens, read from a real iTM's captures and from the fake."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from daikin_itm import (
    DstRule,
    ItmCommandError,
    ReadOnlyError,
    ScreensaverMode,
    SystemSettings,
)
from daikin_itm.menu import MenuScreen, parse_clock, time_of_day
from daikin_itm.protocol import parse_reply
from daikin_itm.state import UIState
from daikin_itm.system import (
    confirm_dialogs_from_screen,
    email_from_screen,
    energy_export_from_screen,
    energy_sampling_from_screen,
    hardware_from_screen,
    maintenance_from_screen,
    network_from_screen,
    points_from_screen,
    ppd_from_screen,
    remote_management_from_screen,
    screensaver_from_screen,
    web_users_from_screen,
    zones_from_screen,
)

from .fake_itm import FakeITM
from .test_client import FakeClient

FIXTURE = Path(__file__).parent / "fixtures" / "system_screens.json"


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM."""
    return FakeITM()


def _captured(name: str) -> UIState:
    """Return the state after the captured reply that opened a screen."""
    s = UIState()
    for entry in json.loads(FIXTURE.read_text(encoding="utf-8"))[name]:
        s.apply(parse_reply(entry["reply"]).screens)
    return s


# -- captured screens ------------------------------------------------------------


def test_captured_zones_and_points() -> None:
    """The zone list and the management points of a captured iTM."""
    position, zones = zones_from_screen(_captured("zones"))
    assert position == "Top"
    assert zones[0].name == "Top"
    assert zones[0].level is None
    assert zones[0].zones == 6
    assert [z.name for z in zones if z.level == 2] == [
        "Indoor",
        "Ventilator",
        "Analog ",  # the trailing space is part of the name
    ]
    assert sum(z.points or 0 for z in zones if z.level == 2) == 16
    points = points_from_screen(_captured("points"))
    assert points[0].kind == "Indoor"
    assert points[0].name == "1:1-00"
    assert points[0].icon == 2114
    assert points[0].info == "AC 01"
    assert len(points) == 16


def test_captured_maintenance_network_users() -> None:
    """No point in maintenance; the network; one administrator, two users."""
    maintenance = maintenance_from_screen(_captured("maintenance"))
    assert maintenance
    assert not any(p.in_maintenance for p in maintenance)
    net = network_from_screen(_captured("network"))
    assert net.controller_name == "intelligent Touch Manager"
    assert net.hostname == "localhost"
    assert net.ip_address == "192.0.2.20"
    assert net.subnet_mask == "255.255.255.0"
    assert (net.dns_primary, net.dns_secondary) == ("9.9.9.9", "1.0.0.1")
    users = web_users_from_screen(_captured("web_users"))
    assert [u.name for u in users] == ["admin", "operator", "viewer"]
    assert [u.admin for u in users] == [True, False, False]
    assert users[0].kind == "Manager"


def test_captured_email_confirm_remote() -> None:
    """No e-mail addresses, confirm dialogs on, remote management disconnected."""
    email = email_from_screen(_captured("email"))
    assert not email.addresses
    assert confirm_dialogs_from_screen(_captured("confirm")) is True
    remote = remote_management_from_screen(_captured("remote"))
    assert remote.ip_address == "0.0.0.0"  # noqa: S104 - as shown
    assert remote.connected is False


def test_captured_touch_panel() -> None:
    """Backlight, buzzer, key click and screensaver of the touch panel."""
    hw = hardware_from_screen(_captured("hardware"))
    assert hw.backlight == 7
    assert hw.buzzer_volume == 2
    assert hw.buzzer_minutes == 5
    assert hw.buzzer_continuous is False
    assert hw.key_click_volume == 1
    ss = screensaver_from_screen(_captured("screensaver"))
    assert ss.mode is ScreensaverMode.BACKLIGHT_OFF
    assert ss.delay_minutes == 15
    assert ss.off_on_error is True


def test_captured_ppd_and_energy_navigator() -> None:
    """The PPD period and the energy navigator's sampling and export periods."""
    ppd = ppd_from_screen(_captured("ppd"))
    assert ppd.time_limit is True
    assert (ppd.start, ppd.end) == ("02/12/2024", "01/01/2025")
    assert ppd.calculation_day == 5
    assert ppd.period is None
    assert not ppd.units
    assert energy_sampling_from_screen(_captured("energy_equipment")) == (
        "09/12/2024",
        "16/12/2024",
        "Top>All",
    )
    assert energy_export_from_screen(_captured("energy_export")) == (
        "01/2025-01/2025",
        "01/2024-12/2024",
    )


def test_captured_service_tab_is_locked() -> None:
    """The service tab answers "in use by another user" instead of opening."""
    state = _captured("service_locked")
    assert 1500 not in state.screens


def test_empty_screens_read_as_unknown() -> None:
    """A screen that sent nothing reads as None or empty, never raises."""
    empty = UIState()
    assert zones_from_screen(empty) == (None, ())
    assert hardware_from_screen(empty).backlight is None
    assert screensaver_from_screen(empty).mode is None
    assert confirm_dialogs_from_screen(empty) is None
    assert remote_management_from_screen(empty).connected is None
    assert ppd_from_screen(empty).time_limit is None


# -- through the client ------------------------------------------------------------


async def test_fetch_system(itm: FakeITM) -> None:
    """All system screens in one session, each closed again."""
    st = await FakeClient(itm).fetch_system()
    assert st.zone_position == "Top"
    assert st.zones is not None
    assert st.zones[1].points == len(itm.units)
    assert st.points is not None
    assert st.points[0].info == f"Unit {itm.units[0].name}"
    assert st.maintenance is not None
    assert [p.name for p in st.maintenance if p.in_maintenance] == [itm.units[0].name]
    assert st.network is not None
    assert st.network.dns_secondary is None
    assert st.email is not None
    assert st.email.addresses == ((True, "ops@example.com"),)
    assert st.web_users is not None
    assert [u.admin for u in st.web_users] == [True, False]
    assert st.hardware is not None
    assert st.hardware.buzzer_continuous is True
    assert st.hardware.buzzer_minutes is None
    assert st.screensaver is not None
    assert st.screensaver.mode is ScreensaverMode.BACKLIGHT_OFF
    assert st.confirm_dialogs is False
    assert st.remote_management is not None
    assert st.remote_management.connected is True
    assert st.ppd is not None
    assert st.ppd.time_limit is False
    assert st.ppd.units[0].consumption_kwh == 12.5
    assert st.ppd.units[0].standby_kwh == 0.3
    assert st.ppd.units[1].consumption_kwh is None
    assert st.energy_navigator is not None
    assert st.energy_navigator.sampling_target == "Top>All"
    assert itm.open_screens == set()


async def test_fetch_system_only_and_unavailable(itm: FakeITM) -> None:
    """Only the wanted screens are opened; one that doesn't open stays None."""
    itm.system_unavailable = {2900, 3210, 3219}
    c = FakeClient(itm)
    st = await c.fetch_system(["network", "ppd", "energy_navigator"])
    assert st.network is not None
    assert st.ppd is None
    assert st.energy_navigator is None
    assert st.zones is None
    assert st.hardware is None
    opened = {r["part"] for r in itm.requests if r["screen"] == 5}
    assert opened == {27, 57, 102, 104}
    itm.system_unavailable = {3219}
    st = await c.fetch_system(["energy_navigator"])
    assert st.energy_navigator is not None
    assert st.energy_navigator.export_periods == ()


async def test_fetch_config_with_system(itm: FakeITM) -> None:
    """fetch_config reads the system screens only when asked."""
    c = FakeClient(itm)
    assert (await c.fetch_config()).system is None
    config = await c.fetch_config(system=True)
    assert isinstance(config.system, SystemSettings)
    assert config.system.confirm_dialogs is False
    assert config.system.web_users is not None


async def test_fetch_config_with_some_system_screens(itm: FakeITM) -> None:
    """fetch_config reads only the system screens asked for, in one session."""
    c = FakeClient(itm)

    def logins() -> int:
        return sum(1 for r in itm.requests if r["screen"] == 1203 and r["event"] == 1)

    before = logins()
    config = await c.fetch_config(system=("hardware", "confirm_dialogs"))
    assert logins() == before + 1
    assert config.system is not None
    assert config.system.hardware is not None
    assert config.system.confirm_dialogs is False
    assert config.system.web_users is None
    assert config.system.network is None


@pytest.mark.parametrize(
    ("only", "message"),
    [
        ("hardware", "not the string 'hardware'"),
        (["hardware", "screen_saver"], r"unknown system settings \[.screen_saver.\]"),
    ],
)
async def test_system_names_are_checked(
    itm: FakeITM, only: str | list[str], message: str
) -> None:
    """A string or an unknown field name is refused before anything is read."""
    c = FakeClient(itm)
    before = len(itm.requests)
    with pytest.raises(ItmCommandError, match=message):
        await c.fetch_system(only)
    with pytest.raises(ItmCommandError, match=message):
        await c.fetch_config(system=only)
    assert len(itm.requests) == before


# -- writes --------------------------------------------------------------------------


async def test_set_hardware(itm: FakeITM) -> None:
    """Picks, OK and Yes; read back in a fresh session."""
    c = FakeClient(itm)
    result = await c.set_hardware(key_click_volume=2, buzzer_minutes=5, backlight=8)
    assert result is not None
    assert (result.key_click_volume, result.buzzer_minutes, result.backlight) == (
        2,
        5,
        8,
    )
    assert itm.hardware == {3: 7, 6: 1, 8: 2, 11: 2}
    result = await c.set_hardware(buzzer_continuous=True)
    assert result is not None
    assert result.buzzer_continuous is True
    with pytest.raises(ItmCommandError, match="not offered"):
        await c.set_hardware(buzzer_volume=9)
    with pytest.raises(ItmCommandError, match="nothing to change"):
        await c.set_hardware()
    assert itm.pending_system == {}
    assert itm.open_screens == set()
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_hardware(key_click_volume=3) is None
    assert dry.planned == ["save hardware settings: screen 2200 part 12 event 1"]
    assert itm.hardware[11] == 2
    with pytest.raises(ReadOnlyError):
        await FakeClient(itm, read_only=True).set_hardware(backlight=1)


async def test_set_screensaver(itm: FakeITM) -> None:
    """Mode pick, delay through the number input, the radio, OK and Yes."""
    c = FakeClient(itm)
    result = await c.set_screensaver(
        mode=ScreensaverMode.SCREEN_2, delay_minutes=31, off_on_error=False
    )
    assert result is not None
    assert result.mode is ScreensaverMode.SCREEN_2
    assert result.delay_minutes == 31
    assert result.off_on_error is False
    with pytest.raises(ItmCommandError, match="outside the iTM's range"):
        await c.set_screensaver(delay_minutes=99)
    assert itm.screensaver["delay"] == 31
    with pytest.raises(ItmCommandError, match="nothing to change"):
        await c.set_screensaver()
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_screensaver(delay_minutes=30) is None
    assert dry.planned == ["save screensaver settings: screen 2000 part 10 event 1"]
    assert itm.screensaver["delay"] == 31


async def test_set_maintenance(itm: FakeITM) -> None:
    """A unit into maintenance and out again; doing it twice changes nothing."""
    c = FakeClient(itm)
    unit = itm.units[2].name
    assert await c.set_maintenance(unit, on=True) is True
    assert unit in itm.in_maintenance
    before = len(itm.requests)
    assert await c.set_maintenance(unit, on=True) is True  # already: no OK sent
    assert not any(
        r["screen"] == 2700 and r["part"] == 9 for r in itm.requests[before:]
    )
    assert await c.set_maintenance(unit, on=False) is False
    assert unit not in itm.in_maintenance
    with pytest.raises(ItmCommandError, match="not found"):
        await c.set_maintenance("nope", on=True)
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_maintenance(unit, on=True) is None
    assert dry.planned == [f"put {unit} into maintenance: screen 2700 part 9 event 1"]
    assert unit not in itm.in_maintenance


async def test_set_point_attributes(itm: FakeITM) -> None:
    """A point's description through the text input; too long is refused."""
    c = FakeClient(itm)
    point = await c.set_point_attributes("1:1-09", info="AC 10 (test)")
    assert point is not None
    assert point.info == "AC 10 (test)"
    assert itm.point_info["1:1-09"] == "AC 10 (test)"
    with pytest.raises(ItmCommandError, match="0-50 characters"):
        await c.set_point_attributes("1:1-09", info="x" * 51)
    with pytest.raises(ItmCommandError, match="nothing to change"):
        await c.set_point_attributes("1:1-09")
    renamed = await c.set_point_attributes("1:1-09", name="AC 10")
    assert renamed is not None
    assert renamed.name == "AC 10"
    assert "1:1-09" not in itm.point_info


async def test_zone_attributes(itm: FakeITM) -> None:
    """Read a zone's settings, change its description and intervals."""
    c = FakeClient(itm)
    z = await c.fetch_zone_attributes("Building")
    assert (z.name, z.info, z.start_interval, z.stop_interval) == (
        "Building",
        None,
        0,
        0,
    )
    z2 = await c.set_zone_attributes(
        "Building", info="Main", start_interval=5, stop_interval=0
    )
    assert z2 is not None
    assert (z2.info, z2.start_interval, z2.stop_interval) == ("Main", 5, 0)
    with pytest.raises(ItmCommandError, match="outside the iTM's range"):
        await c.set_zone_attributes("Building", start_interval=181)
    with pytest.raises(ItmCommandError, match="nothing to change"):
        await c.set_zone_attributes("Building")
    z3 = await c.set_zone_attributes("Building", info="")
    assert z3 is not None
    assert z3.info is None


async def test_web_users(itm: FakeITM) -> None:
    """Add, change and delete a user; never the client's own account."""
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="Managed area"):
        async with c._session() as session:  # pylint: disable=protected-access
            await session.save_web_user(
                "nozone", new=True, password="s3cret!", admin=False
            )
    with pytest.raises(ItmCommandError, match="at least one zone"):
        await c.create_web_user("demouser", "s3cret!", zones=())
    with pytest.raises(ItmCommandError, match="not offered"):
        await c.create_web_user("demouser", "s3cret!", zones=("Nowhere",))
    user = await c.create_web_user(
        "demouser", "s3cret!", zones=("Building",), admin=False
    )
    assert user is not None
    assert (user.name, user.admin) == ("demouser", False)
    assert itm.passwords["demouser"] == "s3cret!"
    with pytest.raises(ItmCommandError, match="exists already"):
        await c.create_web_user("demouser", "s3cret!", zones=("Building",))
    with pytest.raises(ItmCommandError, match="4-15 characters"):
        await c.create_web_user("other", "abc", zones=("Building",))
    changed = await c.set_web_user("demouser", admin=True, password="n3wpass")
    assert changed is not None
    assert changed.admin is True
    assert itm.passwords["demouser"] == "n3wpass"
    with pytest.raises(ItmCommandError, match="nothing to change"):
        await c.set_web_user("demouser")
    await c.delete_web_user("demouser")
    assert [u[0] for u in itm.web_users] == ["admin", "viewer"]
    for call in (
        c.create_web_user("user", "s3cret!", zones=("Building",)),
        c.set_web_user("user", admin=True),
        c.delete_web_user("user"),
    ):
        with pytest.raises(ItmCommandError, match="logs in with"):
            await call
    dry = FakeClient(itm, dry_run=True)
    await dry.delete_web_user("viewer")
    assert dry.planned == ["delete web user viewer: screen 1200 part 6 event 1"]
    assert len(itm.web_users) == 2


async def test_web_user_dry_run_never_types_the_password(itm: FakeITM) -> None:
    """A dry run cancels the password input and plans it as ***."""
    dry = FakeClient(itm, dry_run=True)
    sent = len(itm.requests)
    assert await dry.create_web_user("demouser", "s3cret!", zones=("Building",)) is None
    assert await dry.set_web_user("viewer", password="n3wpass") is None
    assert "web user demouser password = ***" in " ".join(dry.planned)
    assert "web user viewer password = ***" in " ".join(dry.planned)
    typed = [
        r for r in itm.requests[sent:] if r.get("string") in {"s3cret!", "n3wpass"}
    ]
    assert typed == []
    assert "demouser" not in itm.passwords
    with pytest.raises(ItmCommandError, match="4-15 characters"):
        await dry.create_web_user("other", "abc", zones=("Building",))


async def test_language(itm: FakeITM) -> None:
    """Read and switch the display language; only the region screen's OK applies."""
    c = FakeClient(itm)
    assert await c.fetch_language() == "en"
    assert await c.set_language("nl") == "nl"
    assert itm.language_radio == 9
    assert await c.set_language("en") == "en"
    with pytest.raises(ItmCommandError, match="unknown language"):
        await c.set_language("xx")
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_language("de") is None
    assert dry.planned == [
        "pick language de: screen 2501 part 12 event 1",
        "save language de: screen 2500 part 18 event 1",
    ]
    assert itm.language_radio == 2


async def test_dst_and_clock(itm: FakeITM) -> None:
    """Summer time on with the EU rule, then the clock; read back each time."""
    c = FakeClient(itm)
    before = await c.fetch_dst()
    assert before.enabled is False
    assert before.start == DstRule(month=4, week=1, weekday=0, hour=2)
    assert before.end == DstRule(month=9, week=2, weekday=0, hour=3)
    after = await c.set_dst()
    assert after is not None
    assert after.enabled is True
    assert after.end == DstRule(month=10, week=5, weekday=0, hour=3)
    with pytest.raises(ItmCommandError, match="not offered"):
        await c.set_dst(start=DstRule(month=3, week=5, weekday=0, hour=7))
    off = await c.set_dst(enabled=False)
    assert off is not None
    assert off.enabled is False
    when = datetime(2025, 1, 2, 21, 5, 30)  # noqa: DTZ001 - the iTM's local time
    info = await c.set_clock(when)
    assert info is not None
    assert info.clock == when
    assert itm.clock_text == "02/01/2025 21:05:30"
    with pytest.raises(ItmCommandError, match="outside the iTM's range"):
        await c.set_clock(datetime(2040, 1, 1))  # noqa: DTZ001
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_clock(when) is None
    assert await dry.set_dst() is None
    assert dry.planned == [
        "save the clock: screen 2400 part 18 event 1",
        "save summer time: screen 2400 part 18 event 1",
    ]
    with pytest.raises(ReadOnlyError):
        await FakeClient(itm, read_only=True).set_clock(when)


async def test_network_dry_run_sends_no_value(itm: FakeITM) -> None:
    """A network dry run opens and cancels each input; nothing is entered or saved."""
    dry = FakeClient(itm, dry_run=True)
    before = list(itm.network)
    assert (
        await dry.set_network(
            ip_address="192.0.2.21", dns_secondary="8.8.4.4", hostname="itm"
        )
        is None
    )
    assert dry.planned == [
        "network hostname = itm: screen 1800 part 26 event 1 type 2",
        "network ip_address = 192.0.2.21: screen 1800 part 27 event 1 type 512",
        "network dns_secondary = 8.8.4.4: screen 1800 part 27 event 1 type 512",
        "save network settings: screen 1800 part 23 event 1",
    ]
    assert itm.network == before
    answered = [
        r for r in itm.requests if r["screen"] == 1800 and r["part"] in {26, 27}
    ]
    assert answered
    assert all(r["event"] == 2 for r in answered)  # only Cancel
    assert itm.open_screens == set()


async def test_network_write_needs_explicit_permission(itm: FakeITM) -> None:
    """Without allow_network_change only a dry run runs; with it, the change saves."""
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="strand the iTM"):
        await c.set_network(dns_secondary="8.8.4.4")
    assert not any(r["screen"] == 1800 for r in itm.requests)
    with pytest.raises(ItmCommandError, match="not an IPv4 address"):
        await c.set_network(gateway="192.0.2", allow_network_change=True)
    with pytest.raises(ItmCommandError, match="nothing to change"):
        await c.set_network(allow_network_change=True)
    result = await c.set_network(
        dns_secondary="8.8.4.4", controller_name="iTM", allow_network_change=True
    )
    assert result is not None
    assert result.dns_secondary == "8.8.4.4"
    assert result.controller_name == "iTM"
    with pytest.raises(ItmCommandError, match="must be 1-30 long"):
        await c.set_network(hostname="x" * 31, allow_network_change=True)


async def test_service_settings(itm: FakeITM) -> None:
    """Corner login with the service password, then every service screen, read only."""
    c = FakeClient(itm)
    st = await c.fetch_service_settings("test-service")
    assert st.time_zone == "GMT+01:00:Zone B"
    assert len(st.time_zones) == 3
    assert st.changeover_options is not None
    assert st.changeover_options[0].option == "Able"
    assert len(st.changeover_options) == len(itm.units)  # free addresses left out
    assert st.changeover_options[0].address == itm.units[0].name
    assert st.contact == ("Installer", "", "")
    assert st.r32_output_ports == ("", "DO-1")
    assert st.r32_buzzer is False
    assert st.warning_as_abnormal is True
    assert st.dry_mode is False
    assert st.activation_keys == 1
    assert st.dealer_options_enabled == ("Initialising",)
    assert "Emergency Stop" in (st.dealer_options_disabled or ())
    assert st.energy_save_mode == "energy_saving"
    assert itm.open_screens == set()
    oks = {(1500, 3), (1501, 5), (1502, 9), (1507, 7), (1509, 10), (1511, 5)}
    assert not any((r["screen"], r["part"]) in oks for r in itm.requests)


async def test_service_login_refused(itm: FakeITM) -> None:
    """A wrong service password is refused; without a login the tab answers INFO."""
    c = FakeClient(itm)
    with pytest.raises(ItmCommandError, match="refused the service password"):
        await c.fetch_service_settings("wrong")
    assert itm.service_mode is False
    async with c._session() as s:  # pylint: disable=protected-access
        await s.open_menu()
        with pytest.raises(ItmCommandError, match="used by another user"):
            await s.open_menu_screen(69, MenuScreen.TIME_ZONE)


async def test_region_formats(itm: FakeITM) -> None:
    """Date format, 12/24 hours, °C/°F and the decimal separator; read back."""
    c = FakeClient(itm)
    info = await c.set_region(
        date_format="%Y/%m/%d", time_24h=False, decimal_separator=","
    )
    assert info is not None
    assert (info.date_format, info.time_24h, info.decimal_separator) == (
        "%Y/%m/%d",
        False,
        ",",
    )
    assert (itm.date_format, itm.time_24h, itm.decimal_comma) == (2, False, True)
    info = await c.set_region(
        date_format="%d/%m/%Y", time_24h=True, decimal_separator="."
    )
    assert info is not None
    assert info.time_24h is True
    with pytest.raises(ItmCommandError, match="expected one of"):
        await c.set_region(temperature_unit="K")
    with pytest.raises(ItmCommandError, match="nothing to change"):
        await c.set_region()
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_region(temperature_unit="°F") is None
    assert dry.planned == ["save region settings: screen 2500 part 18 event 1"]
    assert itm.fahrenheit is False


def test_clock_and_times_in_12_hour_format() -> None:
    """12-hour clocks and event times read as 24 hours."""
    assert parse_clock("02/01/2025 09:15:04 PM", "%d/%m/%Y") == datetime(  # noqa: DTZ001
        2025, 1, 2, 21, 15, 4
    )
    assert parse_clock("01/15/2025 12:05:00 AM", None) == datetime(  # noqa: DTZ001
        2025, 1, 15, 0, 5, 0
    )
    assert parse_clock("2025/01/02 21:15:04", "%Y/%m/%d") == datetime(  # noqa: DTZ001
        2025, 1, 2, 21, 15, 4
    )
    assert parse_clock("garbage", None) is None
    assert parse_clock("02/01/2025 9:15 XX", None) is None
    assert time_of_day("7:05 PM") == "19:05"
    assert time_of_day("12:30 AM") == "00:30"
    assert time_of_day("06:00") == "06:00"
    assert time_of_day("05") == "05"
