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
network access to the iTM and a user account on it.

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
| `cool_limit`, `heat_limit` | tuple | `(min, max)` of the setpoint limit set on the unit, `None` when none is set. |
| `remote_locks` | `RemoteLocks` | What the wall remote controller may do: `start_stop`, `mode` and `setpoint`, each a `RemoteLock` (`allowed`, `stop_only`, `forbidden`, or `unknown` for a label in a language the client doesn't know). |
| `r32_alarm` | boolean | The R32 refrigerant alarm, `None` when the unit has none. |
| `changeover_option` | boolean | Whether the "changeover option" is set on the unit (auto changeover may then drive it). |
| `co2` | float | The CO2 reading, `None` when the unit has no sensor. |
| `dust_alarm`, `blocked` | boolean | Whether the iTM shows the dust alarm / blocked indication. |

A `Unit` also carries its `kind` (`PointKind`): `indoor`, or `ventilation` for
a heat reclaim ventilation unit, which the iTM only lets you start and stop
(`mode`, `setpoint` and `room_temperature` are `None` then).

### AnalogPoint

An analog input of the iTM, for example an internal alarm on a measured value.
`fetch_points()` returns these next to the units.

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
| `attributes` | dict | Every `<key>:<value>` line of the Details screen, as shown. |

The fields from `unit` on come from the Details screen, read with
`fetch_points(details=True)`.

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
| `ItmIndicators` | `fetch_indicators()` (or any session) | Whether energy saving, an error and the emergency stop are active, and the clock as the main menu bar shows it. |
| `ItmInfo` | `fetch_info()` | Firmware `version` and `build`, the region settings (`language`, `date_format`, `time_24h`, `temperature_unit`, `decimal_separator`), the iTM's `clock` and `dst_enabled`; `clock_offset` is how many seconds the iTM's clock is ahead of this machine's. |
| `ChangeoverGroup` | `fetch_changeover_groups(details=False)` | An auto changeover group: `name`, `enabled`, `member_count`; with details also `members`, `differential` (°C) and `reference` (`ChangeoverReference`: `fixed`, `operating`, `average`). |
| `SetbackSettings` | `fetch_setback()` | The setback offsets `high_up`, `high_back`, `low_up`, `low_back` and the recovery temperatures `recovery_cool`, `recovery_heat` (°C). |
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
| `ScheduleProgram` | `fetch_schedule(name, events=True, details=False)` | A program through its editor (left with Cancel): `enabled`, `period`, the `weekly_slots` and `special_slots` (minutes of the day with an event, per weekday or special day) and, with events, every weekday's and special day's events (`ScheduleEvent`) as the editor lists them. `power`, `mode`, `setpoint` and the wall remote locks are parsed from the event's text through the language tables, the other `key[value]` parts land in `extra`. With `details=True` each event's own settings screen is opened as well (`fan_speed`, `ventilation_mode`, `air_volume`, `overtime`, relative setpoints): two requests per event, so off by default. |
| `History` | `fetch_history(day=None, include_management=False, pages=1)` | A page of the history: `entries` (`HistoryEntry`: `time`, `kind`, `number`, `content`, `name`, `source`, ...), the `day` and `page` as shown. Logins and logouts (the "management" class, most of the history) are left out unless asked for. |
| `OptionalFunction` | `fetch_optional_functions()` | Experimental: whether the functions the menu hides (temperature limit, sliding temperature, heating optimisation, interlock) open, and what they list. |
| `SystemSettings` | `fetch_system(only=None)` | The system screens, most behind hidden menu buttons: zones, management points, maintenance, network, e-mail, web users, the touch panel's hardware and screensaver, confirm dialogs, remote management, PPD and the energy navigator. `only` names the fields to read. |
| `EnergyNavigator` | `fetch_energy_navigator(only=None, *, service_password=None)` | The energy navigator, read only (every screen left with Close or Cancel): the budget overview per tab (`BudgetTab`: labels, energy type, unit and the raw graph), the planned and the actual consumption per fiscal year of the energy type shown (`PlannedYear`, `ActualYear`: month rows, ratio, expected value, CO2 factor), the management rule names and the sampling targets. Shown texts are kept (`ShownValue.text`) and parsed to a number where they are one (`value`, None for "--"). `only` names the fields to read; these are the default. Read only when named: `energy_types` and `energy_groups` (`EnergyType`, `EnergyGroup`: each one's settings through its Edit screen, cancelled), `details` (`EquipmentDetails`, only while the iTM enables its Details button) and `rule_editor` (`RuleEditor`: each management rule's raw pattern bars, special days and calendar), `rule_settings` (`RuleDay`: each rule's weekdays and special days with their time ranges, `RuleRange`, read by selecting each row) and `installer` (`EnergyInstallerSettings`, with `service_password`: each point's room temperature source, the analog inputs, the month data collection starts and the energy conversion's texts; its data clear button is never pressed); their screen paths are tested against the simulated iTM only. |
| `ItmConfig` | `fetch_config()` | All of the above except the history and the hidden functions, in one session (about twenty requests; meant for an hourly read). With `system=True` also `fetch_system()`. |
| `DemandControl` | `fetch_demand_control(only=None)` | Demand (power limit) control, read only: the status (`DemandStatus`: target, predicted, current and adjustment power as `ShownValue`, period, remaining time, the raw graph), its `legend`, the `target_power` setup, the `setup` (controls, forced level), per control and group its temperature, capacity or on/off `levels` (`DemandLevelSetup`), the `groups` (`DemandGroup`: members, condition, resume), the `control_status` tabs and the `graphs` with their daily, monthly and yearly data. `None` when the menu's power limit button opens nothing (as on an iTM without the dealer option). Tested against the simulated iTM only. |
| `EnergySaving` | `fetch_energy_saving(password=None)` | Whether energy saving is active (the main menu bar's indicator), the suspend screen's status, and with the service password the energy saving status (`EnergySavingStatus`: start and remaining time, target, current, estimated and excess power, control status; empty while not active). |

#### Writing to the main menu

The menu reads above have write counterparts. Every one of them does its picks on
the screen, presses OK (answering the iTM's confirm dialog), and then reads the
result back in a fresh session, as the unit writes do; a mismatch raises
`ItmCommandError`. With `ItmClient(..., dry_run=True)` (or `itm --dry-run`) a
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

The system screens have writes too, again with a dry run and a fresh-session
read-back:

| Method | Changes |
| :----- | :------ |
| `set_hardware(*, backlight=None, buzzer_volume=None, buzzer_minutes=None, buzzer_continuous=False, key_click_volume=None)` | The touch panel's backlight and buzzer. |
| `set_screensaver(*, mode=None, delay_minutes=None, off_on_error=None)` | The touch panel's screensaver. |
| `set_maintenance(point, *, on)` | Puts a point in maintenance, or takes it out. |
| `set_point_attributes(point, *, name=None, info=None)` | A point's description (renaming changes how it is found). |
| `fetch_zone_attributes(zone)` / `set_zone_attributes(zone, *, name=None, info=None, start_interval=None, stop_interval=None)` | A zone's settings screen. |
| `create_web_user(name, password, *, zones, admin=False)` / `set_web_user(name, *, password=None, admin=None, zones=None)` / `delete_web_user(name)` | Web access users; a new user needs at least one managed zone. The account the client logs in with is refused; passwords are never logged. |
| `fetch_web_user_screens(name)` / `set_web_user_screens(name, screens)` | The screens a normal web user may open (`WebUserScreens`: allowed and available). Administrators are refused (the iTM disables the button for them). |
| `fetch_own_settings()` / `set_own_settings(*, date_format=None, time_24h=None, decimal_comma=None, icon_colour=None, language=None, password=None, allow_password_change=False)` | The logged-in user's own settings (`OwnSettings`), reached through the login screen's "user settings" button; the iTM ends the session when they apply. A new password needs `allow_password_change=True`; it is sent once, never logged or planned, and used for the read-back login. Reading, the icon colour and the password are validated on a real iTM; the language and the formats are tested against the simulated iTM only and need `allow_experimental=True`. |
| `fetch_language()` / `set_language(code)` | The display language (en, de, es, pt, ja, fr, it, nl, zh, ko). It belongs to the logged-in web user: the iTM ends the session when it changes. |
| `fetch_dst()` / `set_dst(*, enabled=True, start=EU_DST_START, end=EU_DST_END)` | Summer time and its rule (`DstRule`). Set it before the clock. |
| `set_clock(when=None)` | The iTM's clock (default: this computer's local time). |
| `set_region(*, date_format=None, time_24h=None, temperature_unit=None, decimal_separator=None)` | How the iTM shows dates, times, temperatures and decimals (the library reads every format). |
| `create_zone(name, *, parent=None)`, `delete_zone(name)`, `move_zone(name, parent)`, `fetch_zone_members(zone)`, `set_zone_members(zone, members)`, `set_zone_member_order(zone, order, *, use_jump=False)`, `set_zone_icon(zone, icon)`, `set_point_icon(point, icon)` | The zone tree and icons (moving or renaming a zone changes its zone paths). |
| `add_email_address(address, points)`, `edit_email_address(...)`, `delete_email_address(address)`, `set_email_enabled(*, enabled)`, `fetch_email_server()`, `set_email_server(EmailServer(...))`, `send_test_email(address, *, allow_send=False)` | E-mail notification; the server password is never logged. |
| `set_confirm_dialogs(*, enabled)` | The iTM's "confirm settings" dialogs. |
| `fetch_password_settings()`, `set_password_setting(kind, *, enabled=None, password=None, allow_password_change=False)` | Whether the touch panel's administrator and screen password are enabled (`PasswordSettings`); enable, disable or change one (`kind` is `administrator` or `screen`). Refused unless `allow_password_change`; the password is sent once and never logged or planned. |
| `set_planned_consumption(*, year=None, energy_type=None, ratio=None, per_month=None, months=None)`, `set_actual_consumption(*, year=None, energy_type=None, co2_factor=None, months=None)` | The energy navigator's planned consumption (ratio to the previous year, yearly or per month, targets per month) and hand-entered actual consumption (values per month, CO2 factor) of a fiscal year, read back as `PlannedYear` / `ActualYear`. The expected yearly value has no input and is not written. Tested against the simulated iTM only. |
| `set_energy_sampling(*, start=None, end=None, target=None)`, `create_energy_type(name, *, group=False)`, `rename_energy_type(name, new_name, *, group=False)`, `delete_energy_type(name, *, group=False)` | The equipment management's sampling period and target, and the energy types (or with `group=True` the energy groups). Creating, renaming and deleting energy types and groups are validated on a real iTM; the sampling is tested against the simulated iTM only. |
| `create_energy_rule(name)`, `rename_energy_rule(name, new_name)`, `delete_energy_rule(name)`, `copy_energy_rule(name, new_name)`, `copy_energy_rule_calendar(name, targets)`, `set_energy_rule_day(rule, day, ranges)` | The equipment management's rules: the rule list, copying a rule under a new name, copying a rule's calendar to other rules, and one day's time ranges (`RuleRange`: start, end, cooling and heating setpoints), saved through the rule editor and read back as `RuleDay`. All but the calendar copy are validated on a real iTM. |
| `set_energy_room_temperature(password, point, *, analog_point=None, allow_service_change=False)`, `set_energy_collection_start(password, month, *, allow_service_change=False)`, `set_energy_conversion(password, change, *, allow_service_change=False)` | The energy navigator's installer settings, behind the service login: a point's room temperature source (the unit's return air temperature or an analog input), the month data collection starts and the energy conversion's name and unit texts (`ConversionType`). Refused unless `allow_service_change=True` (a dry run plans without it); the password is sent once, never stored or logged; the data clear button is never pressed. Tested against the simulated iTM only. |
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
| `export_history(start, end)`, `export_energy_data(period)`, `export_point_list(start, end)`, `export_setup()`, `run_backup()` | The iTM's exports (the others as CSV), returned as `Download(name, data)`. The history comes as a ZIP archive: Daikin's Windows CSV converter, the raw history (tab-separated text) and the iTM's setup data, including an authentication file, so treat it as sensitive. The iTM serves the file without a session token while it exists, so the library fetches it at once and tells the iTM it is done. `parse_history_export(data)` reads a history archive's records (`HistoryRecord`: time in UTC, event, source, point or user, value) in memory, opening only the history file and the converter settings; codes not seen yet are kept as numbers. `export_setup()` downloads the iTM's configuration (sensitive; validated on a real iTM). `run_backup()` runs the device's backup job (a write: refused by `read_only`, planned in a dry run) and returns the file if the iTM offers one; it follows the web UI's code and is tested against the simulated iTM only. |

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
| `set_leakage_check_indoor_units(password, outdoor, indoor_units, *, allow_service_change=False)`, `set_leakage_check_schedule(password, outdoor, program, *, activation=None, day=None, at=None, allow_service_change=False)`, `copy_leakage_check_schedule(password, outdoor, targets, *, allow_service_change=False)` | Writes: the indoor units of an outdoor unit's refrigerant system, one schedule program (1-4), the schedule copied to other outdoor units. Read back in a fresh session. |
| `start_leakage_check(password, outdoor, *, allow_leakage_check=False)`, `stop_leakage_check(password, outdoor, *, allow_leakage_check=False)` | Start or stop a leakage check by hand. A check runs about 3 hours, forces every unit of that refrigerant system to cool and puts them under maintenance (no control meanwhile): refused unless `allow_leakage_check`. |
| `fetch_layout_setup(password)`, `run_layout_setup(password, *, allow_service_change=False)` | Whether the service tab offers the layout setup (never pressed by the read); pressing it starts a job on the iTM (the screen has no parts). |
| `fetch_r32_output_ports(password)`, `set_r32_output_port(password, port, value, *, allow_service_change=False)` | The R32 alarm output ports and the ports the iTM offers (`R32OutputPorts`); set port 1 or 2 to an offered port, or clear it (`value=None`). The output test is never pressed. |

Two more unit writes go through the unit's own screens, with the same dry run and
read-back:

| Method | Changes |
| :----- | :------ |
| `set_unit_settings(unit, *, start_stop_lock=None, mode_lock=None, setpoint_lock=None, overtime=None, cool_limit=None, heat_limit=None)` | The wall remote locks (`RemoteLock`), the unit's overtime timer and its cooling/heating setpoint limits (`TempLimit`; min/max are entered in the iTM's number input). Read back with `fetch_unit_settings`. |
| `set_power_state(unit, state, *, confirm=True)` | `PowerState.ON`, `OFF`, `SETBACK_LOW` or `SETBACK_HIGH` through the operation panel. |
| `set_unit_settings(unit, *, airflow_direction=…, ventilation_mode=…, ventilation_amount=…, reset_filter_sign=False, reset_dust_alarm=False, force_dust_alarm_reset=False)` | Also through the settings dialog: the airflow direction (`position_0`..`position_4`, `swing`, on units that offer it), a ventilation unit's mode and air volume (by the labels it lists), and resetting a filter sign or dust alarm (offered only while it shows). |
| `set_unit_settings(unit, *, low_noise=…, outdoor_low_noise=…, capacity=…, reheat=…, leaving_water_cool=…, leaving_water_heat=…, storage_water=…, output_repeat=…, output_repeat_interval=…, analog_output=…, multistate_output=…)` | The optional settings in `UnitSettings`: options by label or value, numbers through the iTM's number input. One the unit doesn't offer is refused before anything in the dialog is clicked (not validated on a real iTM yet). |
| `set_zone_power_state(zone, state)`, `set_zone_setpoint(zone, value)`, `apply_zone(zone, *, power=None, mode=None, fan_speed=None)`, `fetch_zone_units(zone)` | Operate every unit of a zone (below the zone path) at once, through the zone's own panel and settings dialog; confirmed on every indoor unit in it. They follow the unit write rules for each unit in the zone: its lock, the spacing and an earlier unconfirmed change. |

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

#### Operation modes

The iTM shows localized mode labels. The client normalizes the ones it knows to
`heat`, `cool`, `fan`, `dry` and `auto`; the original label stays available in
`mode_label` / `mode_labels`. Which modes a unit offers depends on the unit, see
`Capabilities.modes`.

Everything else the client reads is addressed by part ID, icon or index, so the
iTM's display language doesn't matter. The labels it does have to understand
(modes, wall remote locks, the Details status lines) are in `daikin_itm/lang.py`
for the iTM's ten languages; the Dutch and English entries were captured from
devices, the others are the usual Daikin terms until a capture confirms them. A
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
| `confirm_timeout` | `30.0` | Seconds to wait for a fresh read to show a change. |
| `confirm_interval` | `2.0` | Seconds between those reads. |
| `timeout` | `30.0` | Timeout of each HTTP request in seconds. |
| `read_only` | `False` | Refuse every write with `ReadOnlyError` before anything is sent, for exploring or monitoring an iTM without any risk of changing it. |
| `allow_experimental` | `False` | Allow the writes not validated on a real iTM yet, see [Experimental functions](#experimental-functions). |

### Methods

| Method | Description |
| :----- | :---------- |
| `fetch_units(*, view="icons")` | Read every unit in the zone path, returns `dict[str, Unit]` by name. `view="icons"` reads each unit's operation panel (one request per unit); `view="list"` reads the zone's list view, one request for all units, with the extra fields described under [Unit](#unit) but without `power` and `setpoint_range`. |
| `fetch_unit(name)` | Read a single unit (cheaper than `fetch_units()`). |
| `fetch_points(*, details=False)` | Read every point in the zone path, whatever its kind: `Unit`s for indoor and ventilation units, [`AnalogPoint`](#analogpoint)s for analog inputs; with `details=True` also each analog point's Details screen. Returns `dict[str, Point]`. |
| `fetch_error_information()` | Read the [error information](#errorinformation) the iTM shows while a unit has an error, or `None` when it shows none. |
| `fetch_layout(*, names=False)` | Read the graphical layout view (`Layout`): its title, background image, linked screens and items (`LayoutItem`: position, icon ID, mode and size, or a text); empty when no layout is configured. The panel has no names: `names=True` selects each icon (one request each) and reads its name from the operation panel. The standard view is shown again afterwards. Read only. |
| `fetch_unit_settings(name)` | Read a unit's [settings](#unitsettings): wall remote locks, setpoint limits, overtime timer, thermostat status and R32 alarm. Four requests besides the navigation, so not for every poll. |
| `fetch_capabilities()` | Read every unit's identity and the modes and fan control it offers, returns `dict[str, Capabilities]`. This costs four requests per unit, so do it once rather than on every poll. |
| `list_zones(path=())` | The names of the zones and units at a path, for discovery: `list_zones()` for the top level, `list_zones(("Building",))` for what's inside `Building`. |
| `set_setpoint(unit, value, *, confirm=True)` | Change a unit's setpoint. Refused while the unit is stopped (the iTM ignores it then) or when the value is outside `setpoint_range`. |
| `apply(unit, *, power=None, mode=None, fan_speed=None, confirm=True)` | Change power, mode and/or fan speed in one write. The request is compared with the unit as read in the write session: only what differs is sent, and a unit that already is in the requested state is returned without writing anything. |
| `set_power(unit, on, *, confirm=True)` | Start or stop a unit. |
| `set_mode(unit, mode, *, confirm=True)` | Change the operation mode. |
| `set_fan_speed(unit, fan_speed, *, confirm=True)` | Change the fan speed. |
| `release_saved_session()` | Log out a session a previous run left behind; returns `True` if there was one. Every call does this before logging in. A saved token the iTM answers with an HTTP error or an unreadable reply is dropped (with a warning) and left to the iTM's own timeout. |

By default, a write returns the `Unit` as read back once a fresh read shows the
change (or raises `ItmCommandError` after `confirm_timeout`). With
`confirm=False` it returns `None` right after the write; the next write to the
same unit first waits until that change shows, so it can't overtake it.

### Sessions

The iTM allows one web session at a time, so every call logs in, does its work
and logs out. The page token is handed to a `SessionStore` after every reply,
so a session left behind by a crash can be released on the next run. A store is
any object with two coroutines:

```python
class SessionStore(Protocol):
    async def load(self) -> str | None: ...
    async def save(self, token: str | None) -> None: ...
```

- `MemorySessionStore()` (the default) keeps the token in memory.
- `FileSessionStore(path, host)` keeps it in an owner-only file and only hands
  it back for the host it belongs to, so a killed process can still release its
  session on the next run.

### Experimental functions

Some writes are built from the web UI's code and Daikin's manual but have only
been tested against the simulated iTM, because they need a dealer option, a
licence or equipment that wasn't available for testing. `FEATURES.md` marks
them `F`. Among them: the dealer functions, demand control and energy saving,
emergency stop changes, energy navigator changes, the installer's service
writes (leakage check, layout setup, R32 ports, password setting), PPD settings,
the backup job, the test e-mail, web server port, remote management and network
changes, the own language and formats and setting a web user's allowed screens, Jump in
`set_zone_member_order`, °F and the decimal comma in `set_region`, and the
settings dialog's ventilation, reset, low noise, capacity, reheat, water and
output settings in `set_unit_settings`.

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
| `LoginRejected` | The iTM refused the login: wrong credentials, or another web session is active. |
| `SessionEnded` | The iTM ended the session (it answered with a LOGOUT update), e.g. because the token was stale. |
| `VersionMismatch` | The iTM expects another web UI version string (subclass of `SessionEnded`); carries it in `.version`. |
| `ItmCommandError` | The iTM answered a command with a dialog, the UI was not as expected, or a change was not confirmed in time. |
| `ReadOnlyError` | A write was asked of a client created with `read_only=True` (subclass of `ItmCommandError`); nothing was sent. |
| `ExperimentalError` | A write not validated on a real iTM yet was asked without `allow_experimental=True`; nothing was sent. See [Experimental functions](#experimental-functions). |
| `SetpointOutOfRange` | The unit does not accept the setpoint (subclass of `ItmCommandError`). The allowed setpoints depend on the unit and its operation mode; the ones it accepts now are in `.allowed`. |
| `ZoneNotFound` | A zone of the zone path is not there (subclass of `ItmCommandError`); the names that are there are in `.available`. |

Network errors are raised as `aiohttp.ClientError` or `TimeoutError`.

## Things to know about the iTM

- **One web session at a time.** While a client is logged in, every other login
  (including a browser) is refused. The library therefore never keeps a session
  open: every call logs in, works and logs out, also on errors and
  cancellation. If a logout isn't confirmed, the latest page token is kept in the
  `SessionStore` so the next run can release the session. The CLI writes the
  token to a file after every reply, so a session left behind by a crash is
  released on the next run. The Home Assistant integration writes it to
  `.storage` only once a session has lasted 10 s, and then after every reply, to
  spare SD cards: a crash during a normal 1-2 s poll leaves the session to the
  iTM's own timeout.
- **Replies are deltas** against what the iTM believes the client shows, and it
  never echoes a value the client picked itself. Verify a change in a *fresh*
  session (the client does this for every write).
- **Setpoint changes are ignored while a unit is stopped** (the client refuses
  them).
- **Changes take 3-8 s to reach a unit** over the Daikin bus. Every write waits
  until a fresh read shows the new value (up to `confirm_timeout`), so the next
  change can't overtake it; a change sent while the previous one is still
  pending can get lost.
- Power, mode and fan speed go through the unit's settings dialog (tick the
  item, set it, OK, confirm "Instelling uitvoeren?" with *Ja*), so they can be
  combined in one write. The setpoint uses the operation panel's spinner plus a
  `TIMER` commit. All four are verified on the real device.
- Indoor units store settings in memory with a limited number of writes
  (Daikin: max ~7000 changes per unit per year). Don't automate setpoints in
  tight loops; the client spaces writes per unit (`min_write_interval`, 5 s).
- If the iTM's web UI version changes after a firmware update, the client adopts
  the version the iTM reports and logs a warning.

## Security notes

- **The iTM's login is weak by design.** Its web UI sends the username plus an
  unsalted SHA-256 of the password, without a server nonce, and over plain HTTP
  by default. Anyone who can see the traffic can replay it to log in. This
  client can't change the device's protocol, and it has to log in for every
  call (the iTM allows only one session, so it can't stay logged in). Put the
  iTM and the machine running this client on an isolated network segment, use a
  dedicated iTM account for it, and use an `https://…` address if your iTM
  offers HTTPS (the host accepts a full URL).
- Replies are treated as untrusted: the decoder rejects anything that doesn't
  fit the reply (hostile element counts, reads past the end) and replies over
  1 MB.
- The CLI keeps the session token in `~/.cache/daikin-itm/` (owner-only, per
  host) and strips control characters from everything it prints.

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
                          # cached in ~/.cache/daikin-itm/units.json (--refresh to re-read them)
itm status --view list    # the zone's list view: one request for all units, with the
                          # setpoint limits, wall remote locks and alarms
itm caps                  # modes / fan control per unit
itm settings 1:1-04       # a unit's locks, setpoint limits, overtime timer and status
itm --zone "Building/Ventilation" points
                          # every point in a zone, whatever its kind (--details: what an
                          # analog point watches)
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
itm schedule-day Always --days-ahead 1
itm history --day 2025-01-01 --pages 2
itm history --pages 5 --unit "Office"   # one unit: its name, address or description
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
itm email add ops@example.com --point 1:1-09   # also edit, delete, on, off
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
itm backup                       # run the device's backup job
itm zone-members "Zone A" --order AC2 AC1   # put these members first
itm --trace trace.jsonl status   # record requests (numbers only) and replies (no token; see below)
itm --read-only status    # --read-only refuses every write before it is sent
itm setpoint 1:1-04 21    # each write returns once the unit shows the change
itm power 1:1-09 on
itm mode 1:1-04 heat      # heat / cool / fan / dry / auto, as offered by the unit
itm fan 1:1-04 low        # low / medium / high / auto
```

A trace keeps each reply as the iTM sent it, apart from the token, so it includes
the installation's names and settings, and from screens such as the history also
usernames and IP addresses.

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
matters (single session, delta replies, ignored setpoints on stopped units),
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
