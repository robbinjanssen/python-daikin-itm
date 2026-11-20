[![GitHub Release][releases-shield]][releases]
[![Python Versions][python-versions-shield]][pypi]
![Project Stage][project-stage-shield]
![Project Maintenance][maintenance-shield]
[![License][license-shield]](LICENSE)

[![GitHub Activity][commits-shield]][commits-url]
[![PyPi Downloads][downloads-shield]][downloads-url]
[![GitHub Last Commit][last-commit-shield]][commits-url]
[![Stargazers][stars-shield]][stars-url]
[![Issues][issues-shield]][issues-url]

[![Code Coverage][codecov-shield]][codecov-url]

[![Build Status][build-shield]][build-url]

Asynchronous Python client for the Daikin intelligent Touch Manager (iTM).

## About

A Python package to control the indoor units connected to a Daikin
**intelligent Touch Manager (iTM)** locally. It speaks the protocol of the iTM's
built-in web UI, so it needs no cloud service and no extra hardware: only
network access to the iTM and a user account on it: an administrator account
for everything in the main menu (see
[Administrator or normal web user](#administrator-or-normal-web-user)).

Used by the Home Assistant integration
[home-assistant-daikin-itm](https://github.com/robbinjanssen/home-assistant-daikin-itm).

[FEATURES.md](FEATURES.md) lists every screen of the iTM's web UI and how far the
library covers it. The library was developed and validated on an iTM with firmware
Ver1.32.00; other firmware versions and iTM models may differ in details (see
[ROADMAP.md](ROADMAP.md) for how to help).

## Installation

```bash
pip install python-daikin-itm
```

## Changes

Changes in behaviour since the previous release:

- **A write never sends a value the unit shows already, and never loses
  one.** It leaves out what the unit shows (a zone write: the units that show
  it; a zone whose units lack different things is now written unit by unit),
  then reads what it left out in its login for `verify_timeout` (10 s) before
  it returns, with or without `confirm`, and writes it after all, once, when
  another change on its way moves it meanwhile. Such a call takes up to 10 s
  instead of returning at once. See
  [Writes that change nothing](#writes-that-change-nothing).

## Datasets

### Unit

This object represents the current state of an indoor unit, as shown on the
iTM's operation panel or in its list view. `fetch_units()` returns them by unit
name.

| Variable | Type | Description |
| :------- | :--- | :---------- |
| `name` | string | The name of the unit in the iTM, e.g. `1:1-04`. |
| `power` | string | The raw on/off option as the iTM shows it, e.g. `Start`, `Stop` or a setback option. `None` when unknown. |
| `running` | boolean | `True` when the unit is started; setback counts as not running. `None` when unknown. |
| `state_icon` | integer | The raw state icon value of the unit. |
| `setpoint` | integer | The current setpoint in °C. |
| `setpoint_range` | tuple | The setpoints the unit accepts, in °C. |
| `room_temperature` | float | The room temperature in °C, `None` when the unit doesn't report one. |
| `mode` | string | The normalized operation mode (see below), `None` when unknown. |
| `mode_label` | string | The operation mode as the iTM shows it (localized). |
| `fan_icon` | integer | The raw fan speed icon. |
| `fan_speed` | string | The fan speed (see below), `None` when unknown. |
| `error_code` | string | The malfunction code the iTM shows, `None` when there is none. |
| `filter_sign` | boolean | Whether the iTM shows the filter cleaning sign. |
| `power_state` | `PowerState` | What the on/off control is set to: `on`, `off`, `setback_low`, `setback_high`, or `setback` when the list view can't tell which. `None` when unknown. |
| `temperature_unit` | string | `°C` or `°F`, as the iTM shows temperatures. |
| `setback` | boolean | Property: whether the unit is in setback. |

The list view (`fetch_units(view="list")`) adds the fields below and leaves
`power` `None` and `setpoint_range` empty, as the list doesn't show those.

| Variable | Type | Description |
| :------- | :--- | :---------- |
| `cool_limit`, `heat_limit` | tuple | `(min, max)` of the setpoint limit as the list shows it, `None` when it shows none. The list can leave out a limit the settings dialog shows as enabled (seen: a heating limit shown as "---" while the dialog had it on); `fetch_unit_settings` is the complete view. |
| `remote_locks` | `RemoteLocks` | What the wall remote controller may do: `start_stop`, `mode` and `setpoint`, each a `RemoteLock` (`allowed`, `stop_only`, `forbidden`, or `unknown` for a label in a language the client doesn't know). |
| `r32_alarm` | boolean | The R32 refrigerant alarm, `None` when the unit has none. |
| `changeover_option` | boolean | Whether the "changeover option" is set on the unit (auto changeover may then drive it). |
| `co2` | float | The CO2 reading, `None` when the unit has no sensor. |
| `dust_alarm`, `blocked` | boolean | Whether the iTM shows the dust alarm / blocked indication. |

A `Unit` also carries its `kind` (`PointKind`): `indoor`, or `ventilation` for
a heat reclaim ventilation unit, which the iTM only lets you start and stop
(`mode`, `setpoint` and `room_temperature` are `None` then). Both
`set_power_state(name, PowerState.ON/OFF)` and `apply(name, power=...)` start
and stop one (both seen on a real iTM), so either fits an on/off fan entity.

### AnalogPoint

An analog input of the iTM, for example an internal alarm on a measured value.
`fetch_points()` returns these next to the units, `fetch_analog_values()` their
present values.

| Variable | Type | Description |
| :------- | :--- | :---------- |
| `name` | string | The name of the point in the iTM. |
| `kind` | `PointKind` | Always `analog`. |
| `state_icon` | integer | The raw state of its icon. |
| `alarm` | boolean | Whether the point is in alarm (`None` when unknown). The not-in-alarm state was seen on a device, the alarm state itself has not been yet. |
| `unit` | string | The unit of its value, e.g. `°C`. |
| `source_point` | string | The unit it watches. |
| `source_value` | string | Which of that unit's values, as the iTM shows it. |
| `upper_limit`, `lower_limit` | float | The limits that raise the alarm. |
| `description`, `zones` | string, tuple | As on the Details screen. |
| `attributes` | dict | Every `<key>:<value>` line of the Details screen, as shown, without the zone heading and paths (those are in `zones`). |
| `value` | float | The present value, `None` when the list shows none (`---`). |
| `point_id` | string | The iTM management point ID from the Details screen. |

The fields from `unit` to `attributes`, and `point_id`, come from the Details
screen, read with `fetch_points(details=True)`. Neither the operation panel nor
the Details screen shows the present value: only the list view of a zone
holding only analog inputs does (five columns: name, status, value, blocked,
error type; a value like `19.50°C`, two decimals with the unit), read with
`fetch_analog_values()`, which fills `name`, `value` and `unit`. Seen on a real
iTM (firmware Ver1.32.00), where it also kept a shared and a kept session: the
reads after it gave what they give alone, in the same login.

### ErrorInformation

What the iTM's error information screen lists while a unit has an error, read
with `fetch_error_information()`: `columns`, `errors` (one tuple of texts per
row, as listed), `inquiry` and `address`. This screen has not been seen with
content on a device yet, so the rows are passed through as listed.

### The main menu

The iTM's main menu holds its configuration. These are read-only views of it;
every method opens the screen, reads it and closes it with its Close or Cancel
button, so nothing changes. All of them are in `daikin_itm.menu` and exported
from the package.

| Object | Read with | Holds |
| :----- | :-------- | :---- |
| `ItmIndicators` | `fetch_indicators()`, `fetch_points()` (`Points.indicators`) | Whether energy saving, an error and the emergency stop are active, the clock as the main menu bar shows it, and whether the login showed the menu button (`menu_available`; False for a normal web user without allowed screens). The clock is left out of `==`, so two reads compare equal when only the clock moved on. |
| `ItmInfo` | `fetch_info()` | Firmware `version` and `build`, the region settings (`language`, `date_format`, `time_24h`, `temperature_unit`, `decimal_separator`), the iTM's `clock` and `dst_enabled`; `clock_offset` is how many seconds the iTM's clock is ahead of this machine's. The clock fields (`clock`, `clock_text`, `read_at`) are left out of `==`. |
| `ChangeoverGroup` | `fetch_changeover_groups(details=False)` | An auto changeover group: `name`, `enabled`, `member_count`; with details also `members`, `differential` (°C) and `reference` (`ChangeoverReference`: `fixed`, `operating`, `average`). |
| `SetbackSettings` | `fetch_setback()` | The setback offsets `high_up`, `high_back`, `low_up`, `low_back` and the recovery temperatures `recovery_cool`, `recovery_heat` (°C), with their `*_options`: the values the iTM offers (`()` when it didn't show the setting). |
| `int` | `fetch_overtime_duration()` | The overtime timer's duration in minutes. |
| `EmergencyStopProgram` | `fetch_emergency_stop()` | `name` and `enabled` per program. |
| `EmergencyStopSettings` | `fetch_emergency_stop_settings(name)` | A disarmed program's `inputs` and `outputs` (point names), `release_mode` and `stop` as shown, with their `*_options`. The iTM doesn't show an armed (or the built-in) program's settings. |
| `tuple[str, ...]` | `fetch_active_emergency_stops()` | The programs whose stop is active (from the release screen; empty while the main menu bar shows no stop). |
| `TemperatureLimitGroup` | `fetch_temperature_limits(details=False)` | A temperature limit group (dealer option, `daikin_itm.dealer_functions`): `name`, `enabled`, `lower` and `upper` (°C); with details also `members`. |
| `SlidingTemperatureGroup` | `fetch_sliding_temperatures(details=False)` | A sliding temperature group (dealer option): `name`, `enabled`, `outdoor_point`; with details also `members`, `outdoor_range` and `setpoint_range` (°C, from and to). `fetch_sliding_temperature_inputs(name)` lists the analog inputs it can follow. |
| `HeatingOptimisationUnit` | `fetch_heating_optimisation()` | Every unit and whether heating mode optimisation is on for it (dealer option). |
| `InterlockProgram`, `InterlockSetup` | `fetch_interlocks()`, `fetch_interlock_setup(name, details=False)` | Interlock programs (dealer option) and whether they are enabled; a program's input rows and its two output tabs (`InterlockOutput`: condition, outputs, interval; with details the output setup's rows and per output point its `InterlockAction`: on/off, locks, mode, setpoint, fan speed). With details also per input point its `InterlockCondition` (on/off state, operation mode, analog comparison with a constant or a management point and the points offered, offset, hysteresis, multistate value). The condition, point and action screens are driven as the web UI's layout suggests, not seen on a real iTM. |
| `ScheduleSummary` | `fetch_schedules()` | `name`, `enabled`, raw `state` and `period` per schedule program. |
| `ScheduleDay` | `fetch_schedule_day(name, days_ahead=0)` | What a program does on a day (three requests): its `events` as the day overview lists them (`ScheduleEvent`: `time`, `target`, `target_kind`, `action` as shown). |
| `ScheduleProgram` | `fetch_schedule(name, events=True, details=False)` | A program through its editor (left with Cancel): `enabled`, `period` (as shown) and `period_dates` (`((month, day), (month, day))`), the `calendar` rows as shown and as `calendar_values` (`CalendarRuleValues`: `month`, 0 for every month, `day` or `week` and `weekday`, and the `special` day, `"Ex1"`..`"Ex5"`; None for a row whose words are in neither the editor's option lists nor the English tables), the `weekly_slots` and `special_slots` (minutes of the day with an event, per weekday or special day) and, with events, every weekday's and special day's events (`ScheduleEvent`) as the editor lists them. `power`, `mode`, `setpoint` and the wall remote locks are parsed from the event's text through the language tables, the other `key[value]` parts land in `extra`. With `details=True` each event's own settings screen is opened as well (`fan_speed`, `ventilation_mode`, `air_volume`, `overtime`, relative setpoints): two requests per event, so off by default. |
| `History` | `fetch_history(day=None, include_management=False, pages=1)` | A page of the history: `entries` (`HistoryEntry`: `time`, `kind`, `number`, `content`, `name`, `source`, ...), the `day` and `page` as shown. `HistoryEntry.source_kind` says who made an entry: `"web"` for a `Web:<user>` source (the web UI; `source_user` names the user), `"itm"` for an empty source (seen for the iTM's own management entries), `None` for anything else, such as a schedule or the auto changeover; changes from the panel or a wall remote not seen yet. Logins and logouts (the "management" class, most of the history) are left out unless asked for. |
| `OptionalFunction` | `fetch_optional_functions()` | Experimental: whether the functions the menu hides (temperature limit, sliding temperature, heating optimisation, interlock) open, and what they list. |
| `SystemSettings` | `fetch_system(only=None)` | The system screens, most behind hidden menu buttons: zones, management points, maintenance, network, e-mail, web users, the touch panel's hardware and screensaver, confirm dialogs, remote management, PPD and the energy navigator. `only` names the fields to read. |
| `EnergyNavigator` | `fetch_energy_navigator(only=None, *, service_password=None)` | The energy navigator, read only (every screen left with Close or Cancel): the budget overview per tab (`BudgetTab`: labels, energy type, unit and the raw graph), the planned and the actual consumption per fiscal year of the energy type shown (`PlannedYear`, `ActualYear`: month rows, ratio, expected value, CO2 factor), the management rule names and the sampling targets. Shown texts are kept (`ShownValue.text`) and parsed to a number where they are one (`value`, None for "--"). `only` names the fields to read; these are the default. Read only when named: `energy_types` and `energy_groups` (`EnergyType`, `EnergyGroup`: each one's settings through its Edit screen, cancelled), `details` (`EquipmentDetails`, only while the iTM enables its Details button) and `rule_editor` (`RuleEditor`: each management rule's raw pattern bars, special days and calendar), `rule_settings` (`RuleDay`: each rule's weekdays and special days with their time ranges, `RuleRange`, read by selecting each row) and `installer` (`EnergyInstallerSettings`, with `service_password`: each point's room temperature source, the analog inputs, the month data collection starts and the energy conversion's texts; its data clear button is never pressed); their screen paths are tested against the simulated iTM only. |
| `ItmConfig` | `fetch_config()` | All of the above except the history and the hidden functions, in one session (about twenty requests; meant for an hourly read). With `system=True` also `fetch_system()`, or only the system screens named, e.g. `system=("hardware", "screensaver")`. |
| `DemandControl` | `fetch_demand_control(only=None)` | Demand (power limit) control, read only: the status (`DemandStatus`: target, predicted, current and adjustment power as `ShownValue`, period, remaining time, the raw graph), its `legend`, the `target_power` setup, the `setup` (controls, forced level), per control and group its temperature, capacity or on/off `levels` (`DemandLevelSetup`), the `groups` (`DemandGroup`: members, condition, resume), the `control_status` tabs and the `graphs` with their daily, monthly and yearly data. `None` when the menu's power limit button opens nothing (as on an iTM without the dealer option). Tested against the simulated iTM only. |
| `EnergySaving` | `fetch_energy_saving(password=None)` | Whether energy saving is active (the main menu bar's indicator), the suspend screen's status, and with the service password the energy saving status (`EnergySavingStatus`: start and remaining time, target, current, estimated and excess power, control status; empty while not active). |

#### A program's days, computed

These functions compute from a `ScheduleProgram` as `fetch_schedule()` read it (with
its events) and send no requests. They are in `daikin_itm.schedule_days` and exported
from the package. Tested against synthetic programs and the simulated iTM, and compared
with the iTM's own day overview on one real iTM (Dutch display language) with `itm
schedule NAME --on DATE --compare` (see the command line section).

| Function | Returns |
| :------- | :------ |
| `events_on(program, day)` | The events (`ScheduleEvent`) the program runs on a `date`, sorted by time: its weekday's, or the special day's a calendar rule names for that date. Empty outside the program's period. |
| `day_pattern(program, day)` | The pattern that date uses: the weekday (0 = Sunday), the special day (`"Ex1"`..`"Ex5"`) a calendar rule picks, or `None` outside the period. |
| `events_between(program, start, end)` | Every event the program runs from `start` (included) up to `end` (left out) as a list of `(datetime, ScheduleEvent)` in time order, with the period and special days as in `events_on`; empty when `end` is not after `start`. Pass both in the iTM's time zone, both naive or both aware (`ValueError` otherwise); the results get `start`'s tzinfo. Aware times are compared as moments. An event in a DST gap comes once, as `datetime` builds it (fold 0, the moment an hour after its wall-clock time), one in the overlap once, at its first occurrence; how the iTM runs those is unknown. |
| `next_event(program, after, *, days=366)` | The first event after a `datetime` (one at exactly that time doesn't count) as `(datetime, ScheduleEvent)`, searching `days` days ahead; `None` when there is none. Event times are the iTM's local clock, so pass `after` in the iTM's time zone: naive local time, or aware with a `ZoneInfo` zone. A UTC or fixed-offset `after` shifts the results by that offset (or by an hour across a DST change). The result gets `after`'s tzinfo. |
| `targets(program)` | The zones and units the program's events touch, each once, in the order they first appear (Sunday first, then the special days). |

The rules as implemented (`daikin_itm/schedule_days.py` lists which were observed on a
real iTM and which are assumed): a calendar rule's special day replaces the weekday's
events, also when it has none; when several rules name a date, a date rule wins over an
nth-weekday rule, and among rules of the same kind the one lowest in the calendar list
wins; "1st".."4th" is the nth occurrence of the weekday in the month (days 1-7, 8-14,
...); an enabled program runs nothing on a date outside its period, neither its
weekday's events nor a calendar rule's; the period includes its first and last day and
runs over the new year when its end comes first. Observed on that iTM.
Whether the program is enabled is left to the caller. The period and the calendar rules are taken from `period_dates` and
`calendar_values`, which `fetch_schedule()` reads by position in the editor's own combos
and option lists (month, week, weekday, special day), so they work in any display
language; seen on a real iTM in Dutch. A real iTM sends those option lists in the
same order and length in all ten display languages; reading a calendar row with them
in another language is tested against the simulated iTM only. A program without them (built by hand) falls back to the
English words of its text. A period or a calendar rule that can be read neither way,
or a program read without its events, raises `ValueError` instead of giving a wrong
day. An event time shown on a 12-hour display is read as "HH:MM".

#### Writing to the main menu

The menu reads above have write counterparts. Every one of them does its picks on
the screen, presses OK (answering the iTM's confirm dialog), and then reads the
result back, as the unit writes do: in the write's own login when that gets
back to where a login starts, otherwise after a new login (see
[Writes and logins](#writes-and-logins)); a mismatch raises `ItmCommandError`. With `ItmClient(..., dry_run=True)` (or `itm --dry-run`) a
write does everything except the request that makes the change: that request is
recorded in `client.planned` and the screen is cancelled, so nothing changes.
`read_only=True` refuses them all.

| Method | Changes |
| :----- | :------ |
| `set_changeover_enabled(name, enabled)` | Enables or disables an auto changeover group (a group without members can't be enabled). |
| `set_changeover_settings(name, *, differential=None, reference=None)` | A group's differential (°C, as the iTM offers) and/or reference method. |
| `create_changeover_group(name, *, members=(), differential=None, reference=None)` | Creates a group (disabled), then sets its members and settings, each step confirmed. |
| `set_changeover_members(name, members)` | Makes a group's members exactly these units; a unit in another group is refused. |
| `delete_changeover_group(name)` | Deletes a disabled group. |
| `set_setback(*, high_up=None, high_back=None, low_up=None, low_back=None, recovery_cool=None, recovery_heat=None)` | The setback offsets and recovery temperatures. |
| `set_overtime_duration(minutes)` | The overtime timer's duration (one the iTM offers). |
| `set_schedule_enabled(name, *, enabled)` | Enables or disables a schedule program (through its editor). |
| `create_schedule(name)` | Creates an empty, disabled schedule program. |
| `delete_schedule(name)` | Deletes a schedule program. |
| `add_schedule_event(program, day, spec)` | Adds an event (`EventSpec`: time, target point or zone, power/setback, mode, setpoint, locks, overtime, fan) to a weekday (0 = Sunday) or special day ("Ex1".."Ex5"). |
| `edit_schedule_event(program, day, at, target, spec, *, keep_unset=False)` | Replaces the event at `at` for `target` with `spec`; with `keep_unset=True` the settings `spec` leaves out keep their current values (the CLI's `edit-event` does this unless `--replace`). |
| `delete_schedule_event(program, day, at, target)` | Deletes that event. |
| `set_schedule_period(program, period)` | A program's period `((month, day), (month, day))`, or `None` for all year. |
| `rename_schedule_special_day(program, day, name)` | Renames a special day pattern. |
| `add_schedule_calendar_rule(program, rule)` / `delete_schedule_calendar_rule(program, rule)` | Calendar rules (`CalendarRuleSpec`: a date, or the nth weekday of a month) that select a special day pattern. |

The system screens have writes too, again with a dry run and a read-back:

| Method | Changes |
| :----- | :------ |
| `set_hardware(*, backlight=None, buzzer_volume=None, buzzer_minutes=None, buzzer_continuous=False, key_click_volume=None)` | The touch panel's backlight and buzzer. |
| `set_screensaver(*, mode=None, delay_minutes=None, off_on_error=None)` | The touch panel's screensaver. |
| `set_maintenance(point, *, on)` | Puts a point in maintenance, or takes it out. |
| `set_point_attributes(point, *, name=None, info=None)` | A point's description (renaming changes how it is found). |
| `fetch_zone_attributes(zone)` / `set_zone_attributes(zone, *, name=None, info=None, start_interval=None, stop_interval=None)` | A zone's settings screen. |
| `create_web_user(name, password, *, zones=(), admin=False)` / `set_web_user(name, *, password=None, admin=None, zones=None)` / `delete_web_user(name)` | Web access users; a new normal user needs at least one managed zone, an administrator takes none (it manages every zone, and the iTM disables the zone list for one). The account the client logs in with is refused; passwords are never logged. |
| `fetch_web_user_screens(name)` / `set_web_user_screens(name, screens)` | The screens a normal web user may open (`WebUserScreens`: allowed and available). Administrators are refused (the iTM disables the button for them). |
| `fetch_own_settings()` / `set_own_settings(*, date_format=None, time_24h=None, decimal_comma=None, icon_colour=None, language=None, password=None, allow_password_change=False)` | The logged-in user's own settings (`OwnSettings`), reached through the login screen's "user settings" button; the iTM ends the session when they apply. A new password needs `allow_password_change=True`; it is sent once, never logged or planned, and used for the read-back login. The language reads and sets as a code (`en`, `nl`, ...). All of it is validated on a real iTM. |
| `fetch_language()` / `set_language(code)` | The display language (en, de, es, pt, ja, fr, it, nl, zh, ko). It belongs to the logged-in web user: the iTM ends the session when it changes. |
| `fetch_dst()` / `set_dst(*, enabled=True, start=EU_DST_START, end=EU_DST_END)` | Summer time and its rule (`DstRule`). Set it before the clock. |
| `set_clock(when=None)` | The iTM's clock (default: this computer's local time). `when` is the iTM's local time without a time zone; one with a time zone raises `TypeError` before anything is sent. |
| `set_region(*, date_format=None, time_24h=None, temperature_unit=None, decimal_separator=None)` | How the iTM shows dates, times, temperatures and decimals (the library reads every format). |
| `create_zone(name, *, parent=None)`, `delete_zone(name)`, `move_zone(name, parent)`, `fetch_zone_members(zone)`, `set_zone_members(zone, members)`, `set_zone_member_order(zone, order, *, use_jump=False)`, `set_zone_icon(zone, icon)`, `set_point_icon(point, icon)` | The zone tree and icons (moving or renaming a zone changes its zone paths). |
| `add_email_address(address, points)`, `edit_email_address(...)`, `delete_email_address(address)`, `set_email_enabled(address, *, enabled)`, `fetch_email_server()`, `set_email_server(EmailServer(...))`, `send_test_email(address, *, allow_send=False)` | E-mail notification; activation is per address (`EmailSettings.addresses`) and an active address cannot be deleted. The sender must differ from the recipient, or the iTM may not send. The server password is never logged. |
| `set_confirm_dialogs(*, enabled)` | The iTM's "confirm settings" dialogs. |
| `fetch_password_settings()`, `set_password_setting(kind, *, enabled=None, password=None, allow_password_change=False)` | Whether the touch panel's administrator and screen password are enabled (`PasswordSettings`); enable, disable or change one (`kind` is `administrator` or `screen`). Refused unless `allow_password_change`; the password is sent once and never logged or planned. |
| `set_planned_consumption(*, year=None, energy_type=None, ratio=None, per_month=None, months=None)`, `set_actual_consumption(*, year=None, energy_type=None, co2_factor=None, months=None)` | The energy navigator's planned consumption (ratio to the previous year, yearly or per month, targets per month) and hand-entered actual consumption (values per month, CO2 factor) of a fiscal year, read back as `PlannedYear` / `ActualYear`. The expected yearly value has no input and is not written. Tested against the simulated iTM only. |
| `create_energy_type(name, *, group=False)`, `rename_energy_type(name, new_name, *, group=False)`, `delete_energy_type(name, *, group=False)` | The energy types (or with `group=True` the energy groups). Validated on a real iTM. |
| `create_energy_rule(name)`, `rename_energy_rule(name, new_name)`, `delete_energy_rule(name)`, `copy_energy_rule(name, new_name)`, `copy_energy_rule_calendar(name, targets)`, `set_energy_rule_day(rule, day, ranges)` | The equipment management's rules: the rule list, copying a rule under a new name, copying a rule's calendar to other rules, and one day's time ranges (`RuleRange`: start, end, cooling and heating setpoints), saved through the rule editor and read back as `RuleDay`. Validated on a real iTM. |
| `set_energy_room_temperature(password, point, *, analog_point=None, allow_service_change=False, allow_restart=False)`, `set_energy_collection_start(password, month, *, allow_service_change=False, allow_restart=False)`, `set_energy_conversion(password, change, *, allow_service_change=False, allow_restart=False)` | The energy navigator's installer settings, behind the service login: a point's room temperature source (the unit's return air temperature or an analog input), the month data collection starts and the energy conversion's name and unit texts (`ConversionType`). Saving restarts the iTM (it says so in its confirm dialog), so each also needs `allow_restart=True`; the read-back waits until the iTM is back. Refused unless `allow_service_change=True` (a dry run plans without both); the password is sent once, never stored or logged; the data clear button is never pressed. Tested against the simulated iTM only. |
| `set_emergency_stop_enabled(name, *, enabled, allow_safety_change=False)` | Arms or disarms an emergency stop program: refused unless `allow_safety_change` (disarming switches a safety function off; arming may start a stop at once when the input is in alarm). |
| `create_emergency_stop_program(name)`, `rename_emergency_stop_program(name, new_name)`, `copy_emergency_stop_program(name, new_name)`, `delete_emergency_stop_program(name)` | Emergency stop programs (adding needs the iTM's emergency stop dealer option; the built-in program can't be renamed, copied, deleted or edited; a program is deleted only while disarmed). |
| `set_emergency_stop_settings(name, *, inputs=None, outputs=None, release_mode=None, stop=None)` | A disarmed program's input and output points (exactly these), release mode and stop (options as shown). |
| `release_emergency_stop(name, *, allow_safety_change=False)` | Releases an active stop: refused unless `allow_safety_change` (it restarts equipment a safety input stopped). Nothing in the library starts one. |
| `create_temperature_limit(name, *, members=(), lower=None, upper=None)`, `set_temperature_limit(name, *, new_name=None, lower=None, upper=None)`, `set_temperature_limit_members(name, members)`, `set_temperature_limit_enabled(name, *, enabled)`, `delete_temperature_limit(name)` | Temperature limit groups (a group is deleted only while disabled). |
| `create_sliding_temperature(name, *, members=())`, `set_sliding_temperature(name, *, new_name=None, outdoor_point=None, outdoor_range=None, setpoint_range=None)`, `set_sliding_temperature_members(name, members)`, `set_sliding_temperature_enabled(name, *, enabled)`, `delete_sliding_temperature(name)` | Sliding temperature groups; the ranges are (from, to) in °C. |
| `set_heating_optimisation(units=None, *, enabled)` | Heating mode optimisation on or off for these units, or for all of them (the screen's all on / all off buttons) without units. |
| `create_interlock(name)`, `rename_interlock(name, new_name)`, `copy_interlock(name, new_name)`, `set_interlock_enabled(name, *, enabled)`, `delete_interlock(name)`, `set_interlock_setup(name, *, inputs=None, outputs=(), conditions=())` | Interlock programs; the setup of a disabled program: its input points, per input point an `InterlockConditionSpec` (state, mode, comparison, constant or management point, offset, hysteresis, multistate value) and per output tab an `InterlockOutputSpec` (condition, output points, interval, and per output point an `InterlockActionSpec`: power, mode, setpoint, locks). Not seen on a real iTM. |
| `set_demand_target(power)`, `set_demand_forced_level(level)`, `set_demand_level_setup(control, *, group=None, start_level=None, levels=None, upper_cool=None, lower_heat=None)`, `set_demand_group(control, group, *, members=None, condition=None, resume=None)` | Demand control's target power, forced level (saved when picked), a control's level setup per group and a group's members, condition and resume, each read back. Tested against the simulated iTM only. |
| `suspend_energy_saving(*, suspend=True)` | Suspends energy saving while it is active, or resumes it (`suspend=False`) while suspended; read back through the main menu bar's indicator. Tested against the simulated iTM only. |
| `set_web_server_port(port, *, allow_network_change=False)`, `set_remote_management(*, connect, allow_remote_change=False)` | Guarded like `set_network`: a dry run unless explicitly allowed. |
| `copy_schedule(...)`, `copy_schedule_day(...)`, `clear_schedule_day(...)`, `copy_schedule_event(...)`, `copy_schedule_calendar(...)` | Copies of programs, days, events and calendars, and clearing a day. |
| `fetch_ppd(*, start=None, end=None, calculation_day=None)`, `fetch_ppd_advanced()`, `set_ppd_weekday(...)` | Power proportional distribution (needs the iTM's PPD licence for results). |
| `export_history(start, end)`, `export_energy_data(period)`, `export_point_list(start, end)`, `export_setup()`, `run_backup()` | The iTM's exports, returned as `Download(name, data)`; a real iTM delivers the energy data and the point list as ZIP archives (validated). The history comes as a ZIP archive: Daikin's Windows CSV converter, the raw history (tab-separated text) and the iTM's setup data, including an authentication file, so treat it as sensitive. The iTM serves the file without a session token while it exists, so the library fetches it at once and tells the iTM it is done. `parse_history_export(data)` reads a history archive's records (`HistoryRecord`: time in UTC, event, source, point or user, value) in memory, opening only the history file and the converter settings; codes not seen yet are kept as numbers. `export_setup()` downloads the iTM's configuration (sensitive; validated on a real iTM). `run_backup()` runs the device's backup job (a write: refused by `read_only`, planned in a dry run) and returns the file if the iTM offers one; it follows the web UI's code and is tested against the simulated iTM only. |

The installer's service screens sit behind a service login (the menu's four hidden
corner buttons, then the service password, which is not a web user's). The library
reads them; the few writes are a dry run unless `allow_service_change=True`:

| Method | Reads |
| :----- | :---- |
| `fetch_service_settings(password)` | Time zone, changeover option per unit, contact, R32 alarm settings, central monitoring, the number of licence keys, dealer options and the energy save mode (`ServiceSettings`). The password is sent once, never stored or logged. |
| `fetch_point_registrations(password, *, details=False)` | The management point registration (`PointRegistration`: type, name, address and info as listed); with details each point's attributes screen (`PointAttributes`: its label and text fields), opened with Edit and left with Cancel. The list is left with Cancel, its discard confirmation answered with Yes; nothing is added, deleted, loaded, scanned, checked or saved. |
| `fetch_demand_service_settings(password)` | The power limit control setup the service tab opens (`DemandServiceSettings`: the demand pulse setup or the interlock's input ports) and the ports its port picker offers. |
| `set_demand_pulse_setup(password, *, input_port1=None, input_port2=None, pulse_coefficient=None, sync_external=None, return_value=None, cutoff_value=None, allow_service_change=False)`, `set_demand_interlock_ports(password, ports, *, allow_service_change=False)`, `disable_energy_saving(password, *, allow_service_change=False)` | Service writes: a dry run unless `allow_service_change=True`, refused by `read_only`, read back. A port `""` clears it. Tested against the simulated iTM only. |
| `fetch_leakage_check(password, *, details=False)`, `fetch_leakage_check_registration(password, outdoor)`, `fetch_leakage_check_schedule(password, outdoor)` | The leakage check's outdoor units (`LeakageOutdoorUnit`: address, name, type, indoor units, charge, schedule as listed); with details per outdoor unit its registered and unregistered indoor units (`LeakageRegistration`) and its four schedule programs (`LeakageCheckSchedule`, `LeakageCheckProgram`: activation, date and time as shown). Outdoor units are named by name or address. |
| `export_leakage_check_results(password)`, `fetch_leakage_check_results(password)` | The results file through the export download (`Download`), or parsed (`LeakageCheckResults`: per outdoor unit its check dates and leakage amounts; `last(name)`). |
| `set_leakage_check_indoor_units(password, outdoor, indoor_units, *, allow_service_change=False)`, `set_leakage_check_schedule(password, outdoor, program, *, activation=None, day=None, at=None, allow_service_change=False)`, `copy_leakage_check_schedule(password, outdoor, targets, *, allow_service_change=False)` | Writes: the indoor units of an outdoor unit's refrigerant system, one schedule program (1-4), the schedule copied to other outdoor units. Read back after a new login (the service login ends the session). |
| `start_leakage_check(password, outdoor, *, allow_leakage_check=False)`, `stop_leakage_check(password, outdoor, *, allow_leakage_check=False)` | Start or stop a leakage check by hand. A check runs about 3 hours, forces every unit of that refrigerant system to cool and puts them under maintenance (no control meanwhile): refused unless `allow_leakage_check`. |
| `fetch_layout_setup(password)`, `run_layout_setup(password, *, allow_service_change=False)` | Whether the service tab offers the layout setup (never pressed by the read); pressing it starts a job on the iTM (the screen has no parts). |
| `fetch_r32_output_ports(password)`, `set_r32_output_port(password, port, value, *, allow_service_change=False)` | The R32 alarm output ports and the ports the iTM offers (`R32OutputPorts`); set port 1 or 2 to an offered port, or clear it (`value=None`). The output test is never pressed. |

Two more unit writes go through the unit's own screens, with the same dry run and
read-back:

| Method | Changes |
| :----- | :------ |
| `set_unit_settings(unit, *, start_stop_lock=None, mode_lock=None, setpoint_lock=None, overtime=None, cool_limit=None, heat_limit=None, confirm=True)` | The wall remote locks (`RemoteLock`), the unit's overtime timer and its cooling/heating setpoint limits (`TempLimit`; min/max are entered in the iTM's number input). Read back with `fetch_unit_settings`, inside the unit's lock; with `confirm=False` it returns `None` and the next write to the unit waits for the change. Settings the dialog already shows are left alone and checked before it returns; when it shows them all nothing is written (see [Writes that change nothing](#writes-that-change-nothing)). |
| `set_power_state(unit, state, *, confirm=True)` | `PowerState.ON`, `OFF`, `SETBACK_LOW` or `SETBACK_HIGH` through the operation panel. |
| `set_unit_settings(unit, *, airflow_direction=…, ventilation_mode=…, ventilation_amount=…, reset_filter_sign=False, reset_dust_alarm=False, force_dust_alarm_reset=False)` | Also through the settings dialog: the airflow direction (`position_0`..`position_4`, `swing`, on units that offer it), a ventilation unit's mode and air volume (by the labels it lists), and resetting a filter sign or dust alarm (offered only while it shows). |
| `set_unit_settings(unit, *, low_noise=…, outdoor_low_noise=…, capacity=…, reheat=…, leaving_water_cool=…, leaving_water_heat=…, storage_water=…, output_repeat=…, output_repeat_interval=…, analog_output=…, multistate_output=…)` | The optional settings in `UnitSettings`: options by label or value, numbers through the iTM's number input. One the unit doesn't offer is refused before anything in the dialog is clicked (not validated on a real iTM yet). |
| `set_zone_power_state(zone, state)`, `set_zone_setpoint(zone, value)`, `apply_zone(zone, *, power=None, mode=None, fan_speed=None)`, `fetch_zone_units(zone)` | Operate every unit of a zone (below the zone path): through the zone's own panel and settings dialog when every indoor unit lacks the same, otherwise unit by unit with only what each lacks; confirmed on every indoor unit in it. They follow the unit write rules for each unit in the zone: its lock, the spacing and an earlier unconfirmed change. Units that show the change already are left out and checked (see [Writes that change nothing](#writes-that-change-nothing)). |

### UnitSettings

What only a unit's dialogs show, read with `fetch_unit_settings(name)`: the
thermostat status and R32 alarm from the Details screen, the wall remote locks,
setpoint limits and overtime timer from the settings dialog (cancelled
afterwards).

| Variable | Type | Description |
| :------- | :--- | :---------- |
| `name` | string | The name of the unit. |
| `thermo_on` | boolean | Whether the thermostat is calling for heating or cooling (`None` when not shown). |
| `r32_alarm` | boolean | The R32 refrigerant alarm (`None` when not shown). |
| `remote_locks` | `RemoteLocks` | The wall remote locks, see above. |
| `cool_limit`, `heat_limit` | `TempLimit` | The setpoint limits: `enabled`, `minimum`, `maximum`. |
| `overtime` | boolean | Whether the overtime timer is on for the unit. |
| `flags` | dict | Every `<key> [<value>]` status line of the Details screen, as shown. |
| `tabs` | tuple | The names of the settings dialog's tabs the unit gets. |
| `r32_alarm_stoppable` | boolean | Whether the dialog offers to stop the R32 alarm. |
| `low_noise`, `outdoor_low_noise`, `multistate_output` | string | The option shown, with its `…_options` (`None` and `()` when the unit doesn't offer it). |
| `capacity`, `output_repeat_interval` | int | The outdoor unit's capacity limit (%) and a digital output's repeat interval (minutes), with their `…_options`. |
| `reheat`, `output_repeat` | boolean | Reheat on/off and a digital output's repeat mode. |
| `leaving_water_cool`, `leaving_water_heat`, `storage_water`, `analog_output` | float | Water setpoints and an analog output's value. |

The settings from `tabs` on are offered only by some units (a HW supply unit,
a unit with outputs, an outdoor unit; see `DIALOG_SETTINGS`); a setting whose
controls the iTM hides or disables reads as `None`. Their part IDs come from the
web UI's script and have not been seen offered on a real iTM yet.

### Capabilities

This object represents the static facts about a unit: its identity (from the
iTM's Details screen) and what its settings dialog offers. Read it once with
`fetch_capabilities()`; it doesn't change while running.

| Variable | Type | Description |
| :------- | :--- | :---------- |
| `name` | string | The name of the unit in the iTM. |
| `mode_labels` | dict | Normalized mode to the label as the iTM shows it, in the dialog's order. |
| `fan_speed` | boolean | Whether the fan speed of this unit can be changed. |
| `point_id` | string | The iTM management point ID, e.g. `105`. |
| `address` | string | The bus address (`port:address`), e.g. `1:1-04`. Stable, survives renames. |
| `unit_type` | string | The unit type as the iTM shows it. |
| `description` | string | Free text set in the iTM for this unit. |
| `zones` | tuple | The zone paths the unit belongs to. |
| `unknown_labels` | tuple | Mode labels the iTM showed that the client doesn't know (see below): the unit's `modes` are incomplete then. |
| `modes` | tuple | Property: the normalized modes the unit offers. |
| `key` | string | Property: a stable identifier, the bus address when known, else the name. |

The fields from the Details screen (`point_id` to `zones`) are `None` (or empty)
when the iTM doesn't show them.

What has an identity that survives a rename (seen on a real iTM): a unit's bus
`address` and `point_id`, and an analog input's `point_id`. Schedule programs,
zones and auto changeover groups have none but their name and position: a new
program is added at the end of the list, a renamed one keeps its position, and
a deleted one moves the later ones up. Neither the version screen nor the
network screen shows a MAC address or serial number, so the iTM itself is
known by its host (or, for Home Assistant, the MAC address its DHCP discovery
gives).

`to_dict()` returns every field as plain JSON values (the tuples as lists), and
`Capabilities.from_dict(data)` reads that back, also when it was stored by
another version: keys it doesn't know are ignored and a field the data lacks
gets its default. Data of the wrong shape raises `ValueError`. So a caller can
keep the capabilities across restarts and read them again only when a unit is
unknown or renamed.

#### Operation modes

The iTM shows localized mode labels. The client normalizes the ones it knows to
`heat`, `cool`, `fan`, `dry` and `auto`; the original label stays available in
`mode_label` / `mode_labels`. Which modes a unit offers depends on the unit, see
`Capabilities.modes`.

Everything else the client reads is addressed by part ID, icon or index, so the
iTM's display language doesn't matter. The labels it does have to understand
(modes, wall remote locks, the Details status lines) are in `daikin_itm/lang.py`
for the iTM's ten languages, captured from a real iTM. A few labels no device showed
are inferred: the dry mode, an analog point's lower limit, the R32 alarm's "on"
value, German "an" and other spellings of the modes. A
label the client doesn't know ends up in `Capabilities.unknown_labels` (and the
`itm status` output), so it can be added.

#### Fan speeds

`FAN_SPEEDS` holds the fan speeds: `low`, `medium`, `high` and `auto`. Check
`Capabilities.fan_speed` to see whether a unit lets you change it.

## Usage

```python
import asyncio

from daikin_itm import ItmClient


async def main() -> None:
    """Show example on using the iTM client."""
    async with ItmClient(
        "192.168.1.10",
        "<your-username>",
        "<your-password>",
        zone_path=("Building", "Indoor units"),
    ) as client:
        units = await client.fetch_units()
        for unit in units.values():
            print(
                f"{unit.name}: {unit.mode} {unit.setpoint} °C, room {unit.room_temperature} °C"
            )

        capabilities = await client.fetch_capabilities()
        for name, caps in capabilities.items():
            print(
                f"{name} ({caps.key}): modes {caps.modes}, fan control: {caps.fan_speed}"
            )

        # Each write returns the unit as read back once the change shows.
        unit = await client.set_setpoint("1:1-04", 21)

        # Power, mode and fan speed can be combined in one write.
        unit = await client.apply("1:1-04", power=True, mode="heat", fan_speed="low")


if __name__ == "__main__":
    asyncio.run(main())
```

### Client

The host may be `"host"`, `"host:port"` or a full `http://` / `https://` URL.
The other constructor arguments are keyword-only:

| Argument | Default | Description |
| :------- | :------ | :---------- |
| `session` | `None` | An `aiohttp.ClientSession` to use; the client creates (and closes) its own otherwise. |
| `store` | `MemorySessionStore()` | Where to keep the page token, see [Sessions](#sessions). |
| `zone_path` | `()` | The path of zone names in the iTM's icon view to the zone that contains the indoor units, e.g. `("Building", "Indoor units")`. The default (`DEFAULT_ZONE_PATH`) is the top level; use `list_zones()` to find the path. |
| `commit_delay` | `3.0` | Seconds between a setpoint change and its commit, like the web UI. |
| `min_write_interval` | `5.0` | Minimum seconds between two writes to the same unit. |
| `confirm_timeout` | `30.0` | Seconds to wait for a read to show a change. |
| `confirm_interval` | `2.0` | Seconds between those reads. |
| `timeout` | `30.0` | Timeout of each HTTP request in seconds. |
| `read_only` | `False` | Refuse every write with `ReadOnlyError` before anything is sent, for exploring or monitoring an iTM without any risk of changing it. |
| `allow_experimental` | `False` | Allow the writes not validated on a real iTM yet, see [Experimental functions](#experimental-functions). |
| `other_client_timeout` | `60.0` | Seconds a call waits while another client in the same process uses the session of the same iTM and user, before it raises `SessionInUse`; `None` waits without limit. Counted from when the client began to wait, so its calls queued meanwhile fail together. See [Sessions](#sessions). |
| `confirm_in_session` | `True` | Check a write in the login that made it: the write and the reads that confirm it take one login instead of one each. `False` logs in for every one of those reads. See [Writes and logins](#writes-and-logins). |
| `on_write` | `None` | Called as `on_write(unit, what)` each time a unit or zone write sent its change (a zone write through the zone's panel once for every point of the zone, one written unit by unit once for each unit written; `what` is what was asked, such as `"setpoint 22"`), not for a write that found nothing to change or a dry run. An error it raises is logged. See [Write budget](#write-budget). |
| `max_writes_per_unit_per_hour` | `None` | Refuse a unit or zone write to a unit that had this many writes sent by this client in the last hour (`WriteBudgetExceeded`, nothing sent); `None`: no limit. See [Write budget](#write-budget). |
| `max_writes_per_unit_per_day` | `None` | The same for the last 24 hours. |
| `verify_timeout` | `10.0` | Seconds a write that left out what the unit showed already keeps reading it (at most `confirm_timeout`) before it returns, with or without `confirm`; a value that moves away meanwhile is written after all, once. See [Writes that change nothing](#writes-that-change-nothing). |

A few settings are attributes, which may be changed on a client at any time:

| Attribute | Default | Description |
| :-------- | :------ | :---------- |
| `request_timeout` | `timeout` | Seconds one request to the iTM may take. After a request that got no answer (a timeout or a lost connection) the call's clean-up (closing screens and dialogs) is skipped and only the logout is sent, once, so a hung iTM costs about one `request_timeout`; the page token stays in the `SessionStore` when that logout gets no answer either. |
| `logout_timeout` | `5.0` | Seconds that logout may take. |
| `progress_timeout` | `30.0` | Seconds a long list (the history, energy data) may take to load behind the iTM's progress dialog; the history's load is cancelled after it. |
| `progress_interval` | `0.5` | Seconds between the progress requests meanwhile. |
| `session_info` | | Read only: what the client knows about its login, for diagnostics, without a request: a `SessionInfo` with `logged_in`, `since`, `logins`, `last_poll`, `last_error` (and `last_error_at`), `kept`, `writing` and `unanswered`. Holds no token. |
| `writes_sent` | | Read only: the changes this client sent to each unit since it was created, by unit name (a read-only mapping). See [Write budget](#write-budget). |
| `on_write`, `max_writes_per_unit_per_hour`, `max_writes_per_unit_per_day` | constructor | As the constructor arguments. |

### Methods

| Method | Description |
| :----- | :---------- |
| `fetch_units(*, view="icons")` | Read every unit in the zone path, returns `dict[str, Unit]` by name. `view="icons"` reads each unit's operation panel (one request per unit); `view="list"` reads the zone's list view, one request for all units, with the extra fields described under [Unit](#unit) but without `power` and `setpoint_range`. A zone holding units of more than one kind (seen: indoor and ventilation units) gets only a short list from the iTM (name, status, type, error code), which raises `ProtocolError` with a hint to use `view="icons"`; a zone holding only analog inputs gets a list of five columns, which raises `ProtocolError` with a hint to use `fetch_analog_values()`. |
| `fetch_unit(name)` | Read a single unit (cheaper than `fetch_units()`). |
| `fetch_points(*, details=False, thermostat=False)` | Read every point in the zone path, whatever its kind: `Unit`s for indoor and ventilation units, [`AnalogPoint`](#analogpoint)s for analog inputs; with `details=True` also each analog point's Details screen. Returns `Points`: a `dict[str, Point]` as before, with `indicators` (the main menu bar's `ItmIndicators` as the walk left it, no request of its own) and `thermostat`. `thermostat` names the units to read the thermostat status of in the same walk, or `True` for the units `fetch_thermostat_status()` reads by default: each one's Details screen while it is selected anyway, two requests per unit instead of three; the result is in `Points.thermostat` (`dict[str, bool \| None]`, empty when not asked). A string, a zone or a name not in the zone raises `ItmCommandError`. `Points` compare their `thermostat` and `indicators` too when both sides are `Points`. The thermostat in the walk gave the same result alone and in a shared session with one login on a real iTM. |
| `fetch_analog_values()` | The present value of every analog input in the zone path, as `dict[str, AnalogPoint]` with `name`, `value` and `unit` (`state_icon` and `alarm` are `None`: use `fetch_points` for them, and merge by name). One request for the whole zone, read from its list view, so the zone must hold only analog inputs (else `ProtocolError`). Unlike the units' list view it keeps a shared or kept session: on one real iTM, the analog values, the points and the analog values again inside a shared and a kept session took one login and gave what they give alone. |
| `fetch_error_information()` | Read the [error information](#errorinformation) the iTM shows while a unit has an error, or `None` when it shows none. |
| `fetch_layout(*, names=False)` | Read the graphical layout view (`Layout`): its title, background image, linked screens and items (`LayoutItem`: position, icon ID, mode and size, or a text); empty when no layout is configured. The panel has no names: `names=True` selects each icon (one request each) and reads its name from the operation panel. The standard view is shown again afterwards. Read only. |
| `fetch_unit_settings(name)` | Read a unit's [settings](#unitsettings): wall remote locks, setpoint limits, overtime timer, thermostat status and R32 alarm. Four requests besides the navigation, so not for every poll. |
| `fetch_thermostat_status(names=None)` | Whether each unit's thermostat is calling for heating or cooling, as `dict[str, bool \| None]`: one session, each unit selected and its Details screen read (`None` for a unit that shows no thermostat status). When `names` is `None`, every point of the zone except its zones, ventilation units and analog inputs (so also an icon this client doesn't know); a zone name, or a string instead of a list, raises `ItmCommandError`, and an empty list returns `{}` without logging in. About three requests per unit (about 0.12 s per unit, measured with a scratch script on a real iTM); the caller decides which units are worth reading. Tested against the simulated iTM only. |
| `fetch_capabilities()` | Read every unit's identity and the modes and fan control it offers, returns `dict[str, Capabilities]`. This costs five requests per unit (select, Details open and close, settings open and cancel), so do it once rather than on every poll, and keep it across restarts with `Capabilities.to_dict()` and `from_dict()`. |
| `list_zones(path=())` | The names of the zones and units at a path, for discovery: `list_zones()` for the top level, `list_zones(("Building",))` for what's inside `Building`. |
| `set_setpoint(unit, value, *, confirm=True, when_stopped="refuse")` | Change a unit's setpoint, whole degrees. A running unit takes it from the operation panel; a value outside `setpoint_range` raises `SetpointOutOfRange`. The iTM ignores the panel's setpoint while the unit is stopped, so that is refused (`UnitStopped`) by default; with `when_stopped="dialog"` it goes through the unit's settings dialog instead, which the iTM keeps while the unit is stopped and uses when it starts. The dialog's number input has its own range, which follows the unit's setpoint limit (seen: the heating limit's own range while that limit is on, 16-32 with it off); a value outside it raises `SetpointOutOfRange` before anything is saved. Validated on a real iTM in heating and cooling (the panel showed the new setpoint after 3-4 s, running and stopped). |
| `apply(unit, *, power=None, mode=None, fan_speed=None, confirm=True, setpoint=None, when_stopped="refuse")` | Change power, mode, fan speed and/or setpoint in one write, through the settings dialog. The request is compared with the unit as read in the write session: only what differs is sent, and a unit that already is in the requested state is returned without writing anything. A setpoint for a unit that is stopped and stays stopped is refused (`UnitStopped`) unless `when_stopped="dialog"`; its range is the one the dialog's number input shows, after a mode change in the same call (seen on a real iTM: the range follows the new mode, and mode and setpoint both apply from one dialog), see `set_setpoint`. |
| `recent_writes(seconds=86400)` | The changes sent to each unit within the last `seconds` (at most a day), as `dict[str, int]`; what the write budgets compare with. No request. |
| `set_power(unit, on, *, confirm=True)` | Start or stop a unit. |
| `set_mode(unit, mode, *, confirm=True)` | Change the operation mode. |
| `set_fan_speed(unit, fan_speed, *, confirm=True)` | Change the fan speed. |
| `shared_session(max_seconds=30.0)` | An `async with` block in which the reads share one login instead of one each, see [Several reads in one login](#several-reads-in-one-login). Reads only; observed read only on one real iTM (see `ROADMAP.md` for what is still to check). |
| `kept_session(*, keep_alive=60.0, max_age=10800.0)` | **Experimental.** An `async with` block that keeps one login for every call inside it, for as long as it lasts (a polling client's lifetime), kept alive by a POLLING when idle; writes inside need `allow_experimental=True`. See [One login for a polling client](#one-login-for-a-polling-client-experimental). Tested against the simulated iTM only. |
| `release_saved_session()` | Log out a session a previous run left behind; returns `True` if there was one. Every call does this before logging in. A saved token the iTM answers with an HTTP error or an unreadable reply is dropped (with a warning) and left to the iTM's own timeout. |

By default, a write returns the `Unit` as read back once a read shows the
change (or raises `NotConfirmed`, an `ItmCommandError`, after `confirm_timeout`). With
`confirm=False` it returns `None` right after the write (unless it left out
something the unit showed already, see
[Writes that change nothing](#writes-that-change-nothing)); the next write to
the same unit first waits until that change shows, so it
can't overtake it, unless a read meanwhile showed it: `fetch_unit`, `fetch_units` (icon view),
`fetch_points`, `fetch_zone_units` and `fetch_unit_settings` drop such a pending
check when they show the change, in a login of their own or in a
`shared_session()` or `kept_session()` (never in a write's login, nor while the
panel shows a pick of the client). A read that checks a sent change and gets no
answer (a timeout, a lost connection, a reply that can't be read) hasn't seen it
yet: the next read tries again (in a new login when that one ended), up to
`confirm_timeout`; only a refused login (`LoginRejected`) raises at once. When
the last read got no answer, the `NotConfirmed` raised is of that error's type
as well (`TimeoutError`, the aiohttp error, `ProtocolError`), so code that
caught it before still does. The dropped checks and these retries are tested
against the simulated iTM only. A
write whose change was sent but whose reply was lost raises
`WriteOutcomeUnknown`: the iTM may or may not have made the change, and the
next write to the unit (or to the zone's units) first waits until it shows, as
after `confirm=False`; so does the next write after a call cancelled once its
change was sent. Read the unit before repeating such a write; a blind
retry may spend a unit write for nothing.

### Writes and logins

A change takes a few seconds to reach a unit, so a write reads the unit again
every `confirm_interval` seconds until the change shows. With
`confirm_in_session=True` (the default) the write and those reads use one login:
before each read the client sends a POLLING event, as the browser does, and
shows the unit's operation panel (or opens its settings dialog) again in that
login, then logs out once the change shows. A read in the login that made a
change needs care, as the iTM never echoes a value the client picked: a picked
value counts only once the iTM has sent it again. On the way back to where a
login starts, the client shows the zone's other points one by one until the iTM
has sent each picked value over, and a dialog or menu screen that was closed is
read only as the iTM sends it when it opens again. A name, a number or a time of
day typed in a dialog (a program, zone or user name, an e-mail address, a
setpoint limit, an event time) is saved with its screen's OK and keeps the
login; it is read back as the iTM sends it, never as typed. A pick no other point
sends over, a date, the clock or a password typed in a dialog, and the service
login can't be undone that way: they end the write's login, and the read-back
logs in again, as before.

| Write | Logins with `confirm_in_session` | Without |
| :---- | :------------------------------- | :------ |
| `apply`, `set_power`, `set_mode`, `set_fan_speed`, `set_setpoint`, `set_power_state`, `set_unit_settings` (locks, overtime, airflow, options, limits, numbers), `apply_zone` | 1 | 1, plus 1 per read until the change shows (3-5 with the default timing) |
| `set_zone_power_state`, `set_zone_setpoint` | 1, or 2 when no other point sends the zone's pick over | as above |
| Menu writes whose screens close again (for example setback, overtime, a schedule program on or off, hardware, screensaver, confirm dialogs) | 1 | 2 |
| Menu writes that type a name, a number or a time of day (names, event times and setpoints, e-mail addresses, limits) | 1 | 2 |
| Writes that type a date, the clock or a password (`set_clock`, web users with a password), service-login writes, writes that change the zone tree or a top-level icon | 2 | 2 |

Not changed, each with a login of its own for the read-back: `set_network` (the
address may change), `set_language` and `set_region` (the display settings may
only apply to the next login), `set_own_settings` (it logs in by itself), the
energy navigator's installer settings (the iTM restarts), and the writes without
a read-back (exports, backup, test e-mail, web server port, remote management,
layout setup, a schedule's calendar copy).

- **What was seen on a real iTM** (firmware Ver1.32.00, in a kept session): power,
  mode, fan speed and the setpoint lock written and read back in the same login
  matched a fresh read by another account every time (three rounds), with no
  extra login, and so did a fan speed written through a zone; a setpoint change
  matched in three rounds, on the unit and through a zone. When another account
  overruled a write within seconds, the read in the write's login showed the
  other account's value, never the client's own pick. Menu writes read back in
  the same login (a throwaway schedule program's period switched on and off,
  which unticks its checkbox, and the program enabled and disabled, three rounds)
  matched another account's fresh read every time, with one login per write; a
  special day renamed read back correctly too, in two logins while a typed name
  still ended the login. With a typed name or time of day keeping the login, a
  special day renamed and an event's time changed (three rounds each) took one
  login and matched another account's fresh read, and so did a setpoint limit
  switched off and back on (a typed number, 12 writes).
- The block holds the user's session for the whole write, its reads included
  (up to `confirm_timeout`). Other calls of the same client, from any task, use
  its login meanwhile and take turns (a coordinator's poll doesn't wait for the
  write); concurrent writes share it too, and the login ends when the last of
  them returns, also on an error or a cancellation. A `shared_session()` or
  `kept_session()` opened meanwhile, `fetch_own_settings()`, and other clients
  of the same user wait until it ends (`SessionInUse` after their
  `other_client_timeout`).
- Inside a [`kept_session()`](#one-login-for-a-polling-client-experimental) a
  write uses that login (with `allow_experimental=True`); inside a
  `shared_session()` it is refused, as before. `read_only`, the experimental
  guard and the other opt-ins still refuse before anything is sent, and a dry
  run takes one login.
- `confirm_timeout`, `confirm_interval`, the per-unit lock, `min_write_interval`
  and `confirm=False` work as before; a write with `confirm=False` is checked by
  the next write to the unit, in that write's login.

### Writes that change nothing

A write never sends a value the unit shows already. What the iTM forwards to a
unit for a value it already has, and whether the unit stores it again, can't be
seen from the web UI, and how many setting writes an indoor unit takes is not
known (see [Write budget](#write-budget)); so a value the unit has is left out,
which costs no unit write, no spacing (`min_write_interval`) and no count:

- `apply` and its shortcuts, `set_setpoint` and `set_power_state` compare with
  the unit's panel in the write's login and send only what differs (nothing
  when it all shows).
- `set_unit_settings` opens the settings dialog and changes only the settings
  it doesn't show yet; when it shows them all it cancels the dialog. A reset is
  always sent.
- `set_zone_power_state`, `set_zone_setpoint` and `apply_zone` read the zone's
  points (each one's operation panel, after waiting for a pending change to
  them). When every indoor unit shows the change (for a setpoint: every running
  one, and at least one runs), nothing is sent. When every indoor unit lacks
  the same, the zone's panel (or settings dialog) sends it in one write.
  Otherwise each indoor unit that lacks something gets only what it lacks,
  through its own panel or dialog inside the zone (a write and a
  `commit_delay` per unit), and the others are left out. A zone that also holds
  other points (ventilation units, analog inputs) is written through the
  zone's panel with everything asked, as before, whenever an indoor unit lacks
  something: those points aren't compared.

A value left out is never lost either. The iTM shows a change only once it
reached the unit, 3-8 s after it was sent, so another client's change sent
just before may not show yet (seen on a real iTM: a remote lock changed by one
account and asked back to its old value by another account a moment later was
skipped as unchanged, and the first change won). So every write that left
something out, with or without `confirm`, reads it again before it returns:
every `confirm_interval` for `verify_timeout` seconds (10 by default, at most
`confirm_timeout`, at least one read), in its own login and under the unit's
lock (a zone write: its members'). When a read shows a value it left out moved
away, it writes that value after all, once (only what it left out and what
differs then; counted, spaced and refused by a spent budget like any write),
and confirms the whole write, also with `confirm=False`: `NotConfirmed`, with
the last read in `last_seen`, when it still doesn't hold. It never writes a
third time, so two clients that keep asking for different values can't loop.
When no read of the check gets an answer it raises `NotConfirmed` with
`last_seen` `None`.

The cost is time: a call that left something out takes `verify_timeout` (about
five reads with the defaults, each a POLLING and the panel or the dialog) and
holds the unit's lock and the login meanwhile, also with `confirm=False`. A
write that changes everything it asks isn't held up. What a write returns:

| What happened | `confirm=True` | `confirm=False` |
| :------------ | :------------- | :-------------- |
| Everything sent | Confirmed, as before | `None` right away, as before |
| Nothing sent, nothing moved | What it read last, after `verify_timeout` | The same |
| Part sent, nothing moved | Confirmed, after `verify_timeout` | `None` after `verify_timeout`; the next write to the unit checks the part sent, as after any `confirm=False` write |
| A value left out moved | Written after all, once, and confirmed | The same |

The default covers the 8 s seen at most plus one read; a shorter
`verify_timeout` misses a change that arrives later. The check costs no unit
write and no extra login (with `confirm_in_session=False` each read logs in by
itself, as a check of a change sent does). A dry run checks nothing and doesn't
wait. Inside `kept_session()` the check uses the kept login. A call cancelled
during the check stops (`CancelledError`); a part it sent with `confirm=False`
is still checked by the next write to the unit.

These comparisons and the check are tested against the simulated iTM only; the
reads and writes they use are the validated ones, except a zone's unit written
through its own panel inside the zone.

### Write budget

Daikin limits the control commands to one indoor unit to 7,000 a year (the
functional specification of its DIII-NET Modbus adaptor, DTA116A51, which uses the
same bus as the iTM; its building-management interface guides repeat it). Why, and
what the iTM forwards to a unit (for instance a value the unit already has), is not
documented and can't be seen from the iTM's web UI. That is why writes leave out what a unit shows already
([Writes that change nothing](#writes-that-change-nothing)); the budgets below
are an optional guard against a loop in the caller. The client counts the
changes it sends: `writes_sent` (since the client was created) and
`recent_writes(seconds)` (the last day), by unit; a zone write through the
zone's panel counts once for every point of the zone, one written unit by unit
once for each unit written, a change whose reply was lost
(`WriteOutcomeUnknown`) counts, a write that changed nothing and a dry run
don't. `on_write(unit, what)` is called for each. With
`max_writes_per_unit_per_hour` or `max_writes_per_unit_per_day` a write to a unit
that already had that many raises `WriteBudgetExceeded` before it logs in (a
zone write: after reading its points, before its change, for the units it
writes), also when it would have changed nothing; a warning is logged when a
unit reaches 80% of a budget. The counts are kept in memory, per client: other
clients and other accounts writing to the same units are not counted. Both
budgets are off by default. Tested against the simulated iTM only.

### Sessions

The iTM allows one web session per user (another account can be logged in
alongside), so every call logs in, does its work and logs out. Use a dedicated
administrator account for the client, so it never collides with people on the
web UI. The page token is handed to a `SessionStore` after every reply,
so a session left behind by a crash can be released on the next run. A store is
any object with two coroutines:

```python
class SessionStore(Protocol):
    async def load(self) -> str | None: ...
    async def save(self, token: str | None) -> None: ...
```

- `MemorySessionStore()` (the default) keeps the token in memory.
- `FileSessionStore(path, host)` keeps it in an owner-only file and only hands
  it back for the host it belongs to (compared as the iTM it names: without
  case, default port and trailing slash), so a killed process can still release
  its session on the next run. A damaged file, or one that holds no page token,
  loads as no token. With `host=None` the token is bound to no host.

Whatever a store hands back that is not a page token is forgotten, never sent.

Several clients of the same iTM and user in one process (on one event loop) take
turns on the user's session, as the calls of one client do: a call waits until
the other client's call or `shared_session()` or `kept_session()` block is done. The host is compared
without case, default port and trailing slash (`"host"` and `"HTTP://Host:80/"`
are the same iTM). Clients of other users or other iTMs don't wait for each
other.

- The calls of one client wait for each other without limit, as before. A call
  that waits for another client raises `SessionInUse` (a `LoginRejected`)
  once the client has waited `other_client_timeout` seconds (60 by default)
  for the other client, before anything is sent, instead of waiting for ever:
  for example when a task inside one client's block waits for a call of
  another client of the same user. The limit counts from when the client began
  to wait, so the calls it queued meanwhile fail together, not one limit after
  the other.
- While a call or a logged-in block of this client or of another client holds
  the session, `release_saved_session()` releases nothing, from whichever task
  it is called: a saved token is that live session then.
- Across processes nothing changes: a token another process saved is taken for
  a session left behind and released, and a login while the other process is
  logged in is refused by the iTM (`LoginRejected`).

Tested against the simulated iTM only.

#### Several reads in one login

Inside `async with client.shared_session():` the reads use one login instead of
one each: fewer requests, and one login and logout in the iTM's history instead
of a pair for every read. Outside such a block nothing changes.

```python
from daikin_itm import PointKind, Unit

async with client.shared_session():
    points = await client.fetch_points()
    running = [
        name
        for name, point in points.items()
        if isinstance(point, Unit) and point.kind is PointKind.INDOOR and point.running
    ]
    thermostat = await client.fetch_thermostat_status(running)
```

Reading the thermostat status of a few units every poll costs less in the
units' own walk: `points = await client.fetch_points(thermostat=names)`, with
the status in `points.thermostat` and the indicators in `points.indicators`.

**Not validated on a real iTM yet**: it is tested against the simulated iTM
only. Until it is, compare what your reads return inside a block with what they
return alone before relying on it.

- The first read logs in, and leaving the block logs out, also on an error or a
  cancellation. The page token goes to the `SessionStore` as always, so a
  session left behind by a crash is released on the next login.
- After every read the session goes back to where a login starts (screens and
  menu closed, the top level of the icon view), and before the next one the
  client checks that it is there and asks the iTM for what changed meanwhile.
  When that fails, it logs out and the next read logs in again.
- A read that raises ends the session, as its state is unknown then; the next
  read in the block logs in again.
- Some reads end the session by what they do, so the read after them logs in
  again: the units' list view (`fetch_units(view="list")`; the analog list
  view of `fetch_analog_values()` keeps the session) and the layout view, the
  reads that take the service password, a read that enters a date or a number
  it doesn't save (the history of a given day, a PPD or export period, a PPD
  calculation day). Put such a read
  last, or outside the block.
- A day overview moved ahead (`fetch_schedule_day` with `days_ahead`) and a
  history that goes to an older page (`pages`) press the screen's Previous or
  Newer button once per step to get back to the first day or page. When that
  works the session stays; when the screen doesn't show the first one again, or
  the button fails, the session ends and the next read logs in again. One step
  back has been seen on a real iTM; longer runs only in the simulated iTM.
- **Reads only.** A write inside a block raises `SharedSessionError` before
  anything is sent, also as a dry run: a write takes a login of its own (see
  [Writes and logins](#writes-and-logins)). So does `fetch_own_settings()`,
  which needs a login of its own. A
  `read_only` client works as usual.
- `max_seconds` (30 by default) is how long one login may be used: a read that
  starts later logs a warning and logs in again. While a block is open, the
  same user can't log in elsewhere, so keep blocks short.
- The block belongs to the task that opened it and to the tasks started inside
  it (`asyncio.gather` inside a block works; the reads take turns). Calls from
  other tasks wait until the block ends, as they wait for a session otherwise;
  so never let the block's task wait for a read by a task that was started
  outside the block, as each would wait for the other. A block opened inside a
  block joins the outer one; its own `max_seconds` is ignored.
- While a block is logged in, `release_saved_session()` releases nothing, also
  when another task calls it: the saved token is the block's own session. A
  second client of the same iTM and user in the same process waits until the
  block ends (see [Sessions](#sessions)); a client in another process would
  still log the block's session out as one left behind, or be refused.

#### One login for a polling client (experimental)

`kept_session()` keeps one login for every call inside it, across calls and
across time: for a client such as Home Assistant that polls for as long as it
runs, so the iTM's history doesn't get a login and a logout for every poll.

```python
async with client.kept_session():
    while True:
        units = await client.fetch_units()
        ...
        await asyncio.sleep(60)
```

**Experimental: tested against the simulated iTM only.** What it builds on was
seen on one real iTM with a dedicated administrator account: an idle session
ended after between 4 and 5 minutes, a session polled every 30 s lasted an hour,
its unit reads matched a fresh read by another account, and a change made by
another account showed in its next read.

- Reads work as in a [shared session](#several-reads-in-one-login): after every
  call the session goes back to where a login starts, before the next one the
  client checks it is there with a POLLING, and a call that raises, or one of
  the reads that only a new login undoes (the units' list and the layout view,
  service password, a date entered in a dialog), ends the login; the next call
  logs in again.
- **Keep alive.** When no request was sent for `keep_alive` seconds (60 by
  default; the iTM ended an idle session after 3 to 5 minutes, sooner after
  activity than right after a login), a background
  task sends one POLLING, as the browser does. It takes its turn with the
  calls, stops when the block ends, and is cancelled safely. `keep_alive=None`
  sends none.
- **Recovery.** A session the iTM ended (`SessionEnded`, seen in a keep-alive
  POLLING or the one before a call) is replaced by a new login on the next
  call, without an error: callers only see one extra login. A POLLING that gets
  no answer (a connection error or timeout) keeps the session: a keep-alive
  just logs it, a call raises the error, and the next call polls with the same
  token again and goes on with it if the iTM still knows it. A call that loses
  its answer half way raises and ends the login; its token stays in the
  `SessionStore` when the logout gets no answer either, and is released before
  the next login. What a real iTM does after a network drop or its own restart
  has not been seen yet.
- `max_age` (3 hours by default) is how long one login may be used; the next
  call after that logs in again.
- **Writes** inside the block need `allow_experimental=True`; without it they
  raise `ExperimentalError` before anything is sent (a dry run is allowed).
  They take the same way as outside (the unit lock, `min_write_interval`,
  `confirm_timeout` and `confirm_interval`) and read their result back in this
  session, the way every write reads back in its own login (see
  [Writes and logins](#writes-and-logins)): only from what the iTM sent again,
  never from the client's own pick. The iTM sends a picked value over for
  another point only when that point's value differs from what it takes the
  client to show, which may be the pick or the value before it; a pick it
  doesn't send over that way (all other points show the same value) ends the
  login, and the read-back logs in again: such a write costs one extra login.
  The experimental part is writing in a login held for hours, seen on a real
  iTM for a few rounds only. `fetch_own_settings()` and `set_own_settings()`,
  which log in by themselves, stay refused (`SharedSessionError`).
- **One user, held for the whole block.** The session lock is held from the
  start of the block to its end: other clients of the same iTM and user in the
  process wait (`SessionInUse` after their `other_client_timeout`), and the
  same user can't log in on the web UI or from another process meanwhile. Give
  the client a dedicated administrator account.
- Unlike a shared session, the block belongs to the client: while it is open,
  every call of that client uses it, from whichever task (they take turns), so
  a coordinator's polls and a service call's write share the one login. A
  `shared_session()` or `kept_session()` opened meanwhile joins it, with its
  rules. Leaving the block, also on an error or a cancellation, stops the
  keep-alive and logs out.

### Administrator or normal web user

Use an administrator account for the client. A normal web user without allowed
screens couldn't open the main menu (seen on one real iTM), so every read
through it failed: the schedules,
the history, `fetch_info`, the system settings and the rest of
[the main menu](#the-main-menu). Seen on one real iTM, logged in as a normal
web user managing one zone, with no allowed screens:

| Reads | As that normal web user |
| :---- | :---------------------- |
| `fetch_units`, `fetch_points`, `fetch_capabilities`, `fetch_unit_settings`, `fetch_thermostat_status`, `list_zones`, `fetch_layout`, `fetch_indicators`, `fetch_energy_saving`, `fetch_own_settings` | work |
| `fetch_active_emergency_stops`, `fetch_error_information` | empty (nothing was active, so probably the same for an administrator) |
| Every read through the main menu: `fetch_info`, `fetch_config`, `fetch_system`, `fetch_schedules` (so `fetch_schedule` and `fetch_schedule_day`), `fetch_history`, `fetch_changeover_groups`, `fetch_setback`, `fetch_overtime_duration`, `fetch_emergency_stop`, `fetch_language`, `fetch_dst`, `fetch_email_server`, `fetch_optional_functions`, `fetch_energy_navigator`, `fetch_ppd`, `fetch_ppd_advanced`, `fetch_demand_control`, `fetch_interlocks`, `fetch_heating_optimisation`, `fetch_sliding_temperatures`, `fetch_temperature_limits` | `MenuNotAvailable`: the login hides the menu button, and the iTM answers it without opening the menu and without a dialog |

Such a user's login hides the main menu bar's menu button,
where an administrator's shows it. `fetch_indicators()` reports it as
`menu_available` (False when the button is hidden), and every read and write
through the main menu then raises `MenuNotAvailable` at once, without pressing
the button.

The contents of a normal user's managed zone show at the top level of the icon
view (seen for a user managing one zone): its units and points are there directly, and the zone itself is no path
(its name raises `ZoneNotFound`), so use `zone_path=()`. The built-in "All"
zone is not offered as a managed zone. A user managing two zones saw only the
first one's contents (the icon view's Top, Down and Up buttons disabled, no path
to the other zone), so through the web UI such a user reaches one zone only: give
each normal user one zone. Not known yet: whether allowing screens to a normal
user (`set_web_user_screens`) opens the menu for those screens. `itm check reads`
shows per read what an account may read.

### Experimental functions

Some writes are built from the web UI's code and Daikin's manual but have only
been tested against the simulated iTM, because they need a dealer option, a
licence or equipment that wasn't available for testing. `FEATURES.md` marks
them `F`. Among them: the dealer functions, demand control and energy saving,
emergency stop changes, energy navigator changes, the installer's service
writes (leakage check, layout setup, R32 ports, password setting), the backup
job, the test e-mail, web server port, remote management and network changes,
setting a web user's allowed screens, °F and
the decimal comma in `set_region`, and the
settings dialog's ventilation, reset, low noise, capacity, reheat, water and
output settings in `set_unit_settings`. Every write inside a
[`kept_session()`](#one-login-for-a-polling-client-experimental) block is
experimental too: a login held for hours with writes in it has been seen on a
real iTM for a few rounds only.

They refuse with `ExperimentalError` before anything is sent, unless the client
is created with `ItmClient(..., allow_experimental=True)` (`itm --experimental`
on the command line). A dry run (`dry_run=True`, `itm --dry-run`) is always
allowed, so you can see what would be sent without the opt-in. The other guards
still apply: `read_only` refuses first, and writes that need their own
allowance (`allow_network_change`, `allow_leakage_check`,
`allow_service_change`, `allow_safety_change`, `allow_send`, ...) need it as
well. Reads are never guarded.

### Exceptions

| Exception | Description |
| :-------- | :---------- |
| `ItmError` | Base class for everything this library raises. |
| `ProtocolError` | A reply could not be decoded (not an iTM reply, or an unknown layout). |
| `LoginRejected` | The iTM refused the login: wrong credentials, or this user is logged in elsewhere (the iTM allows one session per user). The iTM gives both the same reply ("Cannot log in", same timing; seen on a real iTM), so the two can't be told apart; neither locked the account, and the session already logged in survived. Catch `SessionInUse` before it, as it is a subclass. |
| `SessionInUse` | Another client in this process kept the same iTM user's session longer than `other_client_timeout` (subclass of `LoginRejected`); nothing was sent. |
| `SessionEnded` | The iTM ended the session (it answered with a LOGOUT update), e.g. because the token was stale. |
| `VersionMismatch` | The iTM expects another web UI version string (subclass of `SessionEnded`); carries it in `.version`. |
| `ItmCommandError` | The iTM answered a command with a dialog, the UI was not as expected, or a change was not confirmed in time. |
| `ReadOnlyError` | A write was asked of a client created with `read_only=True` (subclass of `ItmCommandError`); nothing was sent. |
| `SharedSessionError` | A write, or a call that logs in by itself, was asked inside a `shared_session()` block, or a call that logs in by itself inside a `kept_session()` block (subclass of `ItmCommandError`); nothing was sent. |
| `ExperimentalError` | A write not validated on a real iTM yet, or any write inside a `kept_session()` block, was asked without `allow_experimental=True`; nothing was sent. See [Experimental functions](#experimental-functions). |
| `UnitStopped` | A setpoint for a stopped unit or zone, which the iTM would ignore (subclass of `ItmCommandError`); `.name` names it. The change was not sent. |
| `NotOffered` | The unit or zone doesn't offer what was asked: a power state, an operation mode, fan speed control, a settings dialog option, a wall remote lock value, an airflow direction (subclass of `ItmCommandError`); `.value` is what was asked, `.options` what it offers (empty when it offers none), `.name` the unit or zone where known. Nothing was changed. |
| `SetpointOutOfRange` | The unit does not accept the setpoint (subclass of `NotOffered`). The allowed setpoints depend on the unit and its operation mode; the ones it accepts now are in `.allowed` (and `.options`). |
| `NotConfirmed` | A change was sent but the read-back doesn't show it (subclass of `ItmCommandError`), or a unit or zone write that found its change made already got no answer to its check: a unit or zone write after `confirm_timeout`, `set_unit_settings`, the setback, hardware and screensaver settings, and the menu writes that compare their read-back by name. `.last_seen` is the last read-back (a `Unit`, the zone's points by name, `UnitSettings` or the settings read; `None` when no read of a unit or zone write got an answer). When the last read of such a check got no answer, it is of that error's type as well (`TimeoutError`, the aiohttp error, `ProtocolError`), which is its `__cause__`, `.name` the unit, zone or setting. |
| `WriteBudgetExceeded` | A unit had as many writes as `max_writes_per_unit_per_hour` or `max_writes_per_unit_per_day` allows (subclass of `ItmCommandError`); `.name` is the unit, `.limit` the budget and `.seconds` its window (3600 or 86400). Nothing was sent. See [Write budget](#write-budget). |
| `WriteOutcomeUnknown` | The request that makes a change (the operation panel's commit, a settings dialog's or screen's OK or its Yes) was sent but its reply, or that of a later request of the call (its clean-up, the logout), was lost (subclass of `ItmCommandError`). The error is also of the transport error's own type (`TimeoutError`, the `aiohttp` error, `ProtocolError`), which is its `__cause__`, with its attributes (an HTTP error's `.status`), so an `except TimeoutError` still catches it. After a unit or zone write the next write to it first waits for the change to show. |
| `ZoneNotFound` | A zone of the zone path is not there (subclass of `ItmCommandError`); the names that are there are in `.available`. |
| `RequestValueError` | A value can't be put in a request: a number outside its field, a string longer than 65535 characters or with a control character or a character outside the Basic Multilingual Plane, a username over 9999 characters (subclass of `ItmError` and `ValueError`). The request that would carry it is not sent; a write may have logged in and opened its screen before it, and the call then ends that login as after any error. |
| `MenuNotAvailable` | The main menu is not available to this account: the login hid its button, or pressing it opened nothing (subclass of `ItmCommandError`): seen for a normal web user without allowed screens; an administrator account opens it. Raised by every read or write through the main menu. See [Administrator or normal web user](#administrator-or-normal-web-user). |

Network errors are raised as `aiohttp.ClientError` or `TimeoutError`; an HTTP
error status as `aiohttp.ClientResponseError`, any other status but 200 (such
as a redirect, which is never followed) as `ProtocolError`. `set_clock` raises
`TypeError` for a datetime with a time zone, before anything is sent. A
request a call didn't send because an earlier one of its login got no answer
raises `RequestSkipped` (a `TimeoutError`); it only shows up as a note on the
call's error ("clean-up skipped"). The login screen's first request is sent
once more when its connection broke (a closed keep-alive connection, a restart),
not after a timeout.

## Things to know about the iTM

- **One web session per user.** While a client is logged in, a second login
  with the same account (including in a browser) is refused; another account
  can log in and read meanwhile, and the first session stays alive (seen on a
  real iTM). How many
  different users can be logged in at once has not been tested. Give the client
  a dedicated administrator account: a normal web user without allowed screens
  couldn't open the main menu
  (see [Administrator or normal web user](#administrator-or-normal-web-user)). The library never keeps a session
  open: every call logs in, works and logs out, also on errors and
  cancellation; a write and the reads that check it share that login
  ([Writes and logins](#writes-and-logins)); only inside a [`shared_session()`](#several-reads-in-one-login)
  block the reads share one login, which ends with the block, and an
  experimental [`kept_session()`](#one-login-for-a-polling-client-experimental)
  keeps one login for as long as its block lasts. If a logout isn't
  confirmed, the latest page token is kept in the
  `SessionStore` so the next run can release the session. The CLI writes the
  token to a file after every reply, so a session left behind by a crash is
  released on the next run. The Home Assistant integration writes it to
  `.storage` only once a session has lasted 10 s, and then after every reply, to
  spare SD cards: a crash during a normal 1-2 s poll leaves the session to the
  iTM's own timeout.
- **Replies are deltas** against what the iTM believes the client shows, and it
  never echoes a value the client picked itself. Verify a change by what the
  iTM sends again, never by the client's own pick: the client checks every
  write in its own login with reads the iTM answers anew, or in fresh logins
  with `confirm_in_session=False`.
- **Setpoint changes are ignored while a unit is stopped** on the operation
  panel (the client refuses them); the settings dialog's setpoint is kept and
  used when the unit starts (`when_stopped="dialog"`). Setpoints are whole
  degrees (in every reading so far, panel and dialog), so a step of 1.
- **Changes take 3-8 s to reach a unit** over the Daikin bus. Every write waits
  until a read shows the new value (up to `confirm_timeout`), so the next
  change can't overtake it; a change sent while the previous one is still
  pending can get lost.
- Power, mode and fan speed go through the unit's settings dialog (tick the
  item, set it, OK, confirm "Instelling uitvoeren?" with *Ja*), so they can be
  combined in one write. The setpoint uses the operation panel's spinner plus a
  `TIMER` commit, or the settings dialog (`apply(setpoint=...)`, a stopped unit
  with `when_stopped="dialog"`). All are verified on the real device, the
  dialog's setpoint in heating and cooling.
- Daikin limits the control commands to one indoor unit to 7,000 a year (see
  [Write budget](#write-budget)); what the iTM forwards to a unit is not known. Don't
  automate setpoints in tight loops; the client spaces writes per unit
  (`min_write_interval`, 5 s), never writes a value a unit shows already
  ([Writes that change nothing](#writes-that-change-nothing)) and can cap writes
  per unit as a guard against a loop ([Write budget](#write-budget)).
- If the iTM's web UI version changes after a firmware update, the client adopts
  the version the iTM reports and logs a warning.

## Security notes

- **The iTM's login is weak by design.** Its web UI sends the username plus an
  unsalted SHA-256 of the password, without a server nonce, and over plain HTTP
  by default. Anyone who can see the traffic can replay it to log in. This
  client can't change the device's protocol, and it has to log in for every
  call, or once per `shared_session()` or `kept_session()` block (the iTM allows
  one session per user, and the library keeps one open only inside a block). Put the
  iTM and the machine running this client on an isolated network segment, use a
  dedicated iTM account for it, and use an `https://…` address if your iTM
  offers HTTPS (the host accepts a full URL).
- **Only the web login is hashed.** The passwords typed in the iTM go in clear
  over plain HTTP: the service password (every call that takes `password` for
  the service login, and `service_password`), a web user's new password
  (`create_web_user`, `set_web_user`), the client's own new password
  (`set_own_settings`), the touch panel passwords (`set_password_setting`) and
  the e-mail server's password (`set_email_server`). The first one a client
  sends over `http://` logs a warning; `ItmClient.plain_http` tells whether the
  client uses plain HTTP. Do such work over HTTPS where the iTM offers it, or
  on a network segment nobody else can see. No password, login hash or page
  token goes into the library's logs, plans, error messages or reprs, nor into
  a `--trace` file (tested for each of these passwords, normal and dry run,
  against the simulated iTM).
- Requests never follow an HTTP redirect, so no body or GET leaves the
  configured address. Numbers and strings that don't fit a request are refused
  (`RequestValueError`) rather than cut off.
- Replies are treated as untrusted: the decoder rejects anything that doesn't
  fit the reply (hostile element counts, reads past the end) and replies over
  1 MB.
- The CLI keeps the session token in `~/.cache/daikin-itm/` (owner-only, per
  host) and strips control characters from everything it prints. It writes a
  download owner-only, never over an existing file (unless `--force`) and never
  through a symbolic link (not even with `--force`); without `--out` it uses the
  iTM's file name only when that ends in `.csv`, `.zip` or `.txt`.

## CLI

The package installs an `itm` command for debugging. Credentials come from the
environment, or from `--creds` pointing at a JSON file with `username` and
`password` fields (a 1Password JSON export). Set the iTM's address with `--host`
or `ITM_HOST`.

```bash
export ITM_HOST=192.168.1.10
export ITM_USERNAME=... ITM_PASSWORD=...  # or: --creds creds.json
itm zones                 # top-level zones
itm zones Building        # zones/units inside "Building"
itm --zone "Building/Indoor units" status
                          # all units in that zone (default: the top level); descriptions are
                          # cached per host and zone in ~/.cache/daikin-itm/units.json (--refresh
                          # to re-read them; the file is replaced whole, never half written)
itm status --view list    # the zone's list view: one request for all units, with the
                          # setpoint limits, wall remote locks and alarms
itm status --thermostat   # also each indoor unit's thermostat status, read in the units' walk (two requests
                          # per unit, in one login with the units; analog inputs are left out, see points;
                          # with --view list three requests per unit)
itm caps                  # modes / fan control per unit
itm settings 1:1-04       # a unit's locks, setpoint limits, overtime timer and status
itm --zone "Building/Ventilation" points
                          # every point in a zone, whatever its kind (--details: what an
                          # analog point watches; --values: the analog inputs' values, in a
                          # zone of analog inputs only)
itm setpoint 1:1-09 21 --when-stopped dialog  # a stopped unit's setpoint, through its
                          # settings dialog
itm errors                # the error information the iTM shows, if any
itm info                  # firmware version, region settings and clock
itm config                # everything from the menu in one session
itm system --only network,hardware  # system settings; leave out --only for all
itm energy --only budget,rules  # the energy navigator, read only
itm --dry-run energy-edit planned --ratio 5 --month 01/2026=1000  # plan a change
itm --dry-run energy-edit rule-day "Rule A" --day monday --range 08:00-18:00/cool=26
itm changeover --details  # the auto changeover groups
itm setback / overtime / emgstop / schedules
itm schedule "Program B"   # a program in full (--no-events: patterns only)
itm schedule "Program B" --on 2025-01-06   # that date's events, computed
itm schedule "Program B" --on 2025-01-06 --compare   # also the iTM's day overview; exit 1 if they differ
itm schedule-day Always --days-ahead 1
itm history --day 2025-01-01 --pages 2
itm history --pages 5 --unit "Office"   # one unit: its name, address or description (one login; the
                                        # units cache is used when it knows the unit)
                          # who changed what, when (--management: logins too)
itm functions             # probe the hidden functions (experimental)
itm --dry-run changeover create "Group A" --add 1:1-09
                          # writes: --dry-run shows the request that would make the
                          # change instead of sending it
itm changeover create|enable|disable|set|members|delete NAME [--differential N]
                          [--reference fixed|operating|average] [--add UNIT] [--remove UNIT]
itm setback --high-up 3   # also --high-back, --low-up, --low-back, --recovery-cool/-heat
itm overtime 150          # 30 to 180 minutes
itm settings 1:1-09 --mode-lock forbidden --cool-limit 20-26 --overtime on
itm power 1:1-09 setback-low
itm settings 1:1-09 --airflow-direction swing  # also --ventilation-mode/-amount,
                          # --reset-filter-sign, --reset-dust-alarm
itm settings 1:1-09 --low-noise "Level 1" --capacity 70  # also --reheat on|off,
                          # --leaving-water-cool/-heat, --storage-water,
                          # --output-repeat on|off, --output-repeat-interval,
                          # --analog-output, --multistate
itm --zone Building control-zone "Indoor units" --power on --setpoint 21
itm schedule "Program E" --enable # or --disable
itm schedule "Program C" --create # or --delete
itm schedule-edit "Program B" add-event 2 08:30 1:1-04 --power off
                          # also edit-event, delete-event, period, rename-special,
                          # add-rule, delete-rule
itm hardware --key-click-volume 2   # itm screensaver --delay 10
itm maintenance 1:1-09 on
itm point 1:1-09 --info "AC 10"      # itm zone "Zone A" --info "..."
itm user add demouser --manage-zone "Zone A"   # password from ITM_NEW_PASSWORD or a prompt
itm user add demoadmin --type admin            # an administrator takes no --manage-zone
itm user screens viewer --screen "Screen A"   # a normal user's screens; without --screen: show
itm own-settings --time 24h --decimal comma   # also --date-format, --language, --icon-colour, --password --allow-password-change
itm language nl           # itm dst on; itm clock --set-now
itm layout [--names]      # the graphical layout view, read only
itm service               # installer settings, read only (ITM_SERVICE_PASSWORD)
itm service layout|r32-ports  # layout setup offered?, R32 output ports
itm service r32-port --port 1 --value NAME --allow-service-change
itm points-registry [--details]   # management point registration (ITM_SERVICE_PASSWORD)
itm leakcheck [details|results|export]   # leakage check (ITM_SERVICE_PASSWORD)
itm leakcheck start OUTDOOR --allow-leakage-check   # also stop, register, schedule, copy
itm password [enable|disable|change] [administrator|screen] --allow-password-change
                          # the new password from ITM_NEW_PASSWORD
itm demand                # demand (power limit) control; also target, forced-level, level,
                          # group, service, pulse, interlock (service: ITM_SERVICE_PASSWORD)
itm energy-saving suspend # also show, resume, disable --allow-service-change
itm region --time 12h      # also --date-format, --temperature, --decimal
itm zone-add "Zone A"    # also zone-delete, zone-move, zone-members, zone-icon, point-icon
itm email                 # the addresses and the server settings, in one login
itm email add ops@example.com --point 1:1-09   # also edit, delete
itm email on ops@example.com                   # or off: per address
itm confirm-dialogs off
itm emgstop settings "Fire alarm"   # also add, rename, copy, delete, active
itm emgstop disable "Fire alarm" --allow-safety-change  # or enable
itm emgstop set "Program 2" --input AC1 --output AC2 --stop "..."  # disarmed only
itm emgstop release "Fire alarm" --allow-safety-change  # an active stop
itm templimit --details  # also add, members, set, enable, disable, delete
itm templimit add "Group A" --add AC1 --lower 18 --upper 26
itm slidetemp set "Group B" --outdoor-point "Outdoor" --outdoor-range 0 30 --setpoint-range 21 25
itm heatopt on AC1 AC2  # or off; no units: all
itm interlock set "Program A" --input AC1 --output1 AC2 --condition1 "..."  # disabled only
itm schedule-edit "Program C" copy-day 1 2 3     # also copy-program, clear-day, ...
itm ppd --start 2024-11-20 --end 2024-12-19
itm export history --start 2024-12-20 --end 2025-01-01 --out history.zip
itm export history --start 2025-01-01 --end 2025-01-01 --parse   # print the records
itm export setup --out setup.bin   # the iTM's configuration (sensitive; files are saved owner-only)
itm export setup --out setup.bin --force   # replace an existing file (never through a link)
itm backup                       # run the device's backup job
itm zone-members "Zone A" --order AC2 AC1   # put these members first
itm --trace trace.jsonl status   # record requests (numbers only) and replies (no token; see below)
itm --read-only status    # --read-only refuses every write before it is sent
itm check shared-session  # read-only: each read alone and in a shared session, same or different
itm check reads           # read-only: what an account may read, per read
itm setpoint 1:1-04 21    # each write returns once the unit shows the change
itm power 1:1-09 on
itm mode 1:1-04 heat      # heat / cool / fan / dry / auto, as offered by the unit
itm fan 1:1-04 low        # low / medium / high / auto
```

A trace keeps each reply as the iTM sent it, apart from the token, so it includes
the installation's names and settings, and from screens such as the history also
usernames and IP addresses.

### Checking an iTM

`itm check` has two subcommands. **Both are read-only**: the client is built with
`read_only=True`, so nothing can be written, and each only reads.
`check shared-session` has run on one real iTM, the day overview and the paged history in a later run; `check reads` has run on one
real iTM as a normal web user. Running them on a real iTM is how the shared
session gets validated.

- `itm check shared-session` runs every read of the list below alone in its own
  session, then in one `shared_session()` block (allowed an hour, so a slow read
  doesn't make it log in again) after the read before it, and a second time in
  that block. It prints one line per read: `same` or `different` (all three
  results compared), the timings alone and in the block, and the logins each
  used. A read whose block logged in more than once, or whose read before it
  failed in the block, is `split` (not run in one session) instead: no
  validation either way. At the end come the totals and the number of logins the
  iTM saw. The exit code is 1 when a read differs, fails or is split, otherwise
  0.
- `itm check reads` runs every read once, each in a session of its own, and prints
  `ok`, `empty` (nothing returned) or the error class. It shows what an account
  may read, for example a user who is not an administrator (see
  [Administrator or normal web user](#administrator-or-normal-web-user)). The exit code is 0
  unless the login itself fails (a rejected login, or an iTM that can't be
  reached before any read worked).

The list is every public read that needs no arguments and no service password,
plus a schedule read per program (`fetch_schedule[1]`, ...), a day overview per program one day ahead (`fetch_schedule_day[1]`, ...), the history over two pages (`fetch_history[pages]`) and a settings read
per indoor unit (`fetch_unit_settings[1]`, ...), numbered because names are not
printed. The demand control and energy navigator reads are in it, the latter
twice (`[more]` opens the extra screens too). Left out of both are the reads that
press a menu button that starts something: `fetch_password_settings` (never read
on a real iTM yet) and `export_setup` (starts the export job).
Both also leave out `fetch_analog_values`, which reads only a zone holding
nothing but analog inputs. `shared-session`
also leaves out what can't run in a block: `fetch_layout` (ends the shared
session by design) and `fetch_own_settings` (logs in by itself); `reads`
includes them. `--list` prints the names, and what is left out, without
contacting the iTM; `--only NAME` and `--skip NAME` (repeatable) select reads by
name or by label such as `fetch_energy_navigator[more]`.

Values and error messages are not printed, as names of zones and units are part
of the installation and error messages may name them. `--show` prints both (for
`shared-session` only the values that differ), so don't share that output.
Fields that change by themselves are left out of the comparison and named on the
line when they differed: `read_at`, `clock`, `clock_text`, `room_temperature`,
`co2`, the demand control's countdown, measured and predicted power, power
adjustment and graphs, and the energy navigator's actual consumption. Other
readings can change between two reads (a thermostat turning on, the history
getting an entry); run the check again before taking such a `different` for a
fault.

## Protocol notes

`POST /cmd/`, body `snD=<hex>`: 32-char page token, then screen ID, part ID,
event type, data type (shorts), a number (int), an int array and a string.
Numbers are big-endian hex, strings are a length plus raw characters. Login:
screen 1203, part 4, string `%04d len(user) + user + sha256hex(password)`.
Replies: new token, then screen updates with part updates (labels, combobox
values, icon panels…). Screens used: 1 icon view (part 6 icon panel, 2 = go
down), 4 operation panel (9 on/off, 11 setpoint). A setpoint change is a
selection on screen 4 followed by a `TIMER` event on screen 4. Power/mode/fan
use the settings dialog (screen 7): tick 2/17/28, radio 3/4, mode combobox 18,
fan up 29 (icon 31: 3009 low, 3011 medium, 3013 high, 3015 auto), OK 5, then
CLICK on the confirm dialog's ID. See `daikin_itm/protocol.py`.

## Contributing

This is an active open-source project. We are always open to people who want to
use the code or contribute to it.

We've set up a separate document for our
[contribution guidelines](CONTRIBUTING.md). [ROADMAP.md](ROADMAP.md) lists what is
still to be added, what is left out on purpose and why, and how you can help with
equipment or licences the library couldn't be tested on.

Thank you for being involved! :heart_eyes:

## Setting up development environment

This Python project is fully managed using the [uv][uv] package manager.

You need at least:

- Python 3.11+
- [uv][uv-install]

Install all packages, including all development requirements:

```bash
uv sync
```

uv creates a virtual environment in `.venv` and installs all necessary
packages in it. Run commands in it with `uv run`.

Set up the pre-commit checks with [prek][prek]:

```bash
uv run prek install
```

*Now you're all set to get started!*

All changes are linted and tested with each commit. You can run all checks
and tests manually, using the following command:

```bash
uv run prek run --all-files
```

To run just the Python tests:

```bash
uv run pytest
```

No iTM is needed for the tests: `tests/fake_itm.py` simulates the behaviour that
matters (one session per user, one user simulated, delta replies, ignored setpoints on stopped units),
and `tests/fixtures/` holds iTM replies in the real protocol format, with
synthetic content.

## License

MIT License

Copyright (c) 2026 Robbin Janssen

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

<!-- MARKDOWN LINKS & IMAGES -->
[build-shield]: https://github.com/robbinjanssen/python-daikin-itm/actions/workflows/tests.yaml/badge.svg
[build-url]: https://github.com/robbinjanssen/python-daikin-itm/actions/workflows/tests.yaml
[commits-shield]: https://img.shields.io/github/commit-activity/y/robbinjanssen/python-daikin-itm.svg
[commits-url]: https://github.com/robbinjanssen/python-daikin-itm/commits/main
[codecov-shield]: https://codecov.io/gh/robbinjanssen/python-daikin-itm/branch/main/graph/badge.svg
[codecov-url]: https://codecov.io/gh/robbinjanssen/python-daikin-itm
[downloads-shield]: https://img.shields.io/pypi/dm/python-daikin-itm
[downloads-url]: https://pypistats.org/packages/python-daikin-itm
[issues-shield]: https://img.shields.io/github/issues/robbinjanssen/python-daikin-itm.svg
[issues-url]: https://github.com/robbinjanssen/python-daikin-itm/issues
[license-shield]: https://img.shields.io/github/license/robbinjanssen/python-daikin-itm.svg
[last-commit-shield]: https://img.shields.io/github/last-commit/robbinjanssen/python-daikin-itm.svg
[maintenance-shield]: https://img.shields.io/maintenance/yes/2026.svg
[project-stage-shield]: https://img.shields.io/badge/project%20stage-experimental-yellow.svg
[pypi]: https://pypi.org/project/python-daikin-itm/
[python-versions-shield]: https://img.shields.io/pypi/pyversions/python-daikin-itm
[releases-shield]: https://img.shields.io/github/release/robbinjanssen/python-daikin-itm.svg
[releases]: https://github.com/robbinjanssen/python-daikin-itm/releases
[stars-shield]: https://img.shields.io/github/stars/robbinjanssen/python-daikin-itm.svg
[stars-url]: https://github.com/robbinjanssen/python-daikin-itm/stargazers

[prek]: https://prek.j178.dev
[uv]: https://docs.astral.sh/uv/
[uv-install]: https://docs.astral.sh/uv/getting-started/installation/
