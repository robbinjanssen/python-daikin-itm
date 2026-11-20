# Feature coverage

Every screen of the iTM's web UI (the screen classes of its `script.js`, plus the
remote management screen it only shows on the device) and how far this library covers
it. Generated from the web UI's screen list; update it with each change. What is
planned, and what is left out on purpose and why, is in [ROADMAP.md](ROADMAP.md).

Status, for reading and for writing:

| Code | Meaning |
| :--- | :------ |
| V | in the library and validated on a real iTM |
| D | in the library; validated on a real iTM as a dry run only (writes that could strand the iTM) |
| F | in the library, tested against the simulated iTM only: experimental. Its writes refuse unless the client is created with `allow_experimental=True` (CLI: `--experimental`); a dry run always works, and reads need no opt-in |
| P | probed on a real iTM (it opens), not in the library yet |
| L | needs a dealer option, a paid licence or equipment that wasn't available for testing |
| X | out of scope on purpose (installer-only or destructive, or needs a person at the panel) |
| - | not covered yet |

Inside covered screens, these functions are built but only tested against the
simulated iTM, because they need equipment that wasn't available: in a unit's
settings dialog (screen 7) low noise (unit and outdoor unit), capacity limit, reheat,
leaving water and storage water setpoints, digital output repeat mode, analog and
multistate outputs; filter sign reset, dust alarm reset and ventilation mode/air
volume (the ventilation units' mode and air volume in schedule events are read
live); and the °F and decimal comma display settings (the date format and 12-hour
clock are validated live). Pre-cool/pre-heat, time and management point parts of
screen 7 belong to schedule events, not to a unit.

`events_on()`, `events_between()`, `day_pattern()`, `next_event()` and `targets()`
compute a schedule program's days from the program as screen 104 reads it, without
requests (`events_between()` against synthetic programs only, its DST handling a
choice of the library: how the iTM runs an event in a DST gap or overlap is
unknown). They are tested against synthetic programs and the simulated iTM, and
compared with the iTM's day overview (screen 102) on one real iTM with `itm
schedule NAME --on DATE --compare` and a throwaway program; ROADMAP.md lists what
is still open. The period and calendar rules are read by position in the editor's
combos and option lists (`period_dates`, `calendar_values`), so they don't depend on the
display language. A real iTM sends those option lists when the editor opens, in the
order and length assumed (twelve months and "every month", four weeks and "last",
Sunday to Saturday), in each of its ten display languages; reading a calendar row
with them is seen in Dutch, in other languages tested against the simulated iTM
only.

`shared_session()`, which lets several reads use one login, reads the screens below
as the single reads do, and goes back to the top level with the icon view's Top
button. On one real iTM every read in the check gave the same result inside a block as
alone (read only, `itm check shared-session`); `ROADMAP.md` lists what is still to check.
Clients of the same iTM and user in one process taking turns on its session
(`other_client_timeout`, `SessionInUse`) are tested against the simulated iTM
only as well. So is `kept_session()` (experimental): one login kept for a polling
client's lifetime, with a keep-alive POLLING, a new login after the iTM ended the
session, and writes (with `allow_experimental`) read back in that session from
the dialog opened anew or a panel value only once the iTM sent it again (else a
new login).
A write and the reads that check it share one login (`confirm_in_session`, on
by default), read back the same way; seen on one real iTM (in a kept session)
for power, mode, fan speed, the setpoint lock, a zone's fan speed and the
setpoint. A menu write read back in the same login is tested against the
simulated iTM only (a menu write's V below was seen with a read-back after a new
login), and so is a typed name, number or time of day keeping the write's login.

How the client meets faults is tested against the simulated iTM only (its lost
reply, hung request and restart modes): after a request that got no answer only
the logout is sent (`request_timeout`, `logout_timeout`); a change whose reply
was lost raises `WriteOutcomeUnknown` and the next write to the unit waits for
it; the login screen's first request is sent once more after a broken
connection; a progress dialog is waited out by time (`progress_timeout`), and
the history's load is cancelled after it (CANCEL_PROG, its reply not recorded
on a real iTM yet). A read that checks a sent change and gets no answer is tried
again until `confirm_timeout`.

The unit and zone writes on screens 4 and 7 never send a value the iTM
already shows (`set_unit_settings` changes only the settings the dialog
doesn't show yet; a zone write reads its points first and, when its units lack
different things, writes them one by one with only what each lacks), then read
what they left out for `verify_timeout`, with or without confirm, and write it
after all, once, when another change on its way moves it (on a real iTM such a
skipped write lost to another account's change made just before; the check is
not validated there yet), count the changes they send per unit
(`writes_sent`, `on_write`) and can be capped per unit
(`max_writes_per_unit_per_hour`, `max_writes_per_unit_per_day`). That logic
is tested against the simulated iTM; the screens' reads and writes it uses are
the validated ones.

172 screens, each counted by its best status (read or write). Covered: 138 (validated on a real iTM: 94, dry run only: 0, simulated iTM only: 44); probed only: 0; needs a licence or another iTM: 0; out of scope: 33; not covered: 1.

| Screen | Class | Read | Write | Notes |
| ---: | :---- | :--: | :---: | :---- |
| 1 | `ScMainStdIcon` | V | V | icon view: units, zones, selection; the Top button, which only `shared_session()` clicks, was seen to return to the top level on one real iTM; a normal web user's top level holds its managed zone's contents (seen for a user managing one zone) |
| 2 | `ScMainStdList` | V | - | list view (fetch_units(view='list')); a zone holding only zones gives no units; a zone holding only analog inputs gives their present values (fetch_analog_values, five columns, seen on a real iTM; it keeps a shared or kept session, seen on one real iTM) |
| 3 | `ScMainLayout` | V | - | graphical layout view (fetch_layout; validated without a layout: empty until an installer sets one up) |
| 4 | `ScMainOperation` | V | V | operation panel: power, setback, setpoint, fan; of a unit or a whole zone |
| 5 | `ScSysMainMenu` | V | V | menu list; service login (corner buttons); doesn't open for a normal web user without allowed screens (`MenuNotAvailable`, seen on one real iTM); that user's login hides the main menu bar's menu button (`ItmIndicators.menu_available`), so the client doesn't press it |
| 6 | `ScMainLegend` | X | X | icon legend (static help, no data) |
| 7 | `ScMainSetting` | V | V | unit/zone settings: mode, fan, locks, overtime, limits, airflow direction, the setpoint (`set_setpoint(when_stopped="dialog")` for a stopped unit, `apply(setpoint=...)`; validated in heating and cooling, running and stopped, and with a mode in the same dialog) (V); ventilation mode/amount, filter sign and dust alarm reset (F); separate cool/heat setpoints, low noise, outputs (-) |
| 9 | `ScMainDetail` | V | - | details: thermostat, R32, analog attributes and ID (`fetch_thermostat_status` reads the thermostat for many units in one session, tested against the simulated iTM only; `fetch_points(thermostat=...)` in the units' own walk, the same alone and in a shared session on a real iTM) |
| 10 | `ScMainErrorInformation` | F | - | error information (never seen with content) |
| 100 | `ScSchMain` | V | V | programs: list, create, delete, enable, copy |
| 102 | `ScSchConfirm` | V | - | day overview (moved one day ahead with `days_ahead`, then back, in a shared session: seen on a real iTM; also onto and past a day without events; more steps in a shared session: simulated iTM only) |
| 103 | `ScSchLegend` | X | X | schedule legend (static help, no data) |
| 104 | `ScSchEdit` | V | V | program editor: patterns, period, special days, calendar rules, day clear |
| 106 | `ScSchCalCopy` | - | V | calendar copy (to programs named explicitly) |
| 107 | `ScSchEventList` | V | V | event list: add, edit, delete, event copy |
| 108 | `ScSchCopy` | - | V | copy a day's events |
| 109 | `ScSchEvent` | V | V | event settings |
| 110 | `ScSchPntAreaSel` | V | V | event target picker (single selection) |
| 200 | `ScEmgStopMain` | V | F | emergency stop programs: list, enabled state (V); enable/disable (guarded), add, rename, copy, delete (F: hidden without the dealer option) |
| 201 | `ScEmgStopRelease` | F | F | emergency stop release: active stops, release (only reachable while a stop is active) |
| 202 | `ScEmgStopSetting` | F | F | emergency stop program settings: inputs, outputs, release mode, stop (needs the dealer option) |
| 203 | `ScEmgStopMngSelect` | F | F | emergency stop point selection |
| 300 | `ScAutoChangeOverSetting` | V | V | auto changeover groups |
| 301 | `ScAutoChangeOverGroup` | V | V | changeover group members |
| 302 | `ScAutoChangeOverAttribute` | V | V | changeover group settings |
| 400 | `ScTempLimitSetting` | V | F | temperature limit groups (read validated on an empty list, without the dealer option) |
| 401 | `ScTempLimitGroup` | F | F | temperature limit members |
| 402 | `ScTempLimitAttribute` | F | F | temperature limit settings |
| 500 | `ScSlideTempSetting` | V | F | sliding temperature groups (read validated on an empty list, without the dealer option) |
| 501 | `ScSlideTempGroup` | F | F | sliding temperature members |
| 502 | `ScSlideTempAttribute` | F | F | sliding temperature settings |
| 503 | `ScSlideTempAi` | F | F | sliding temperature analog input |
| 600 | `ScHMO` | V | F | heating mode optimisation per unit, all on/off (read validated without the dealer option) |
| 700 | `ScTimerExtension` | V | V | overtime timer duration |
| 800 | `ScHistoryMain` | V | - | history (`HistoryEntry.source_kind`: "web" for `Web:<user>`, "itm" for an empty source, seen on a real iTM; with its progress dialog; paged back one page with `pages`, then forward, in a shared session: seen on a real iTM; more pages: simulated iTM only) |
| 801 | `ScHistoryOutput` | - | V | history export (a ZIP download) |
| 900 | `ScInterlockMain` | V | F | interlock programs (read validated on an empty list, without the dealer option) |
| 901 | `ScInterlockProgramSetup` | F | F | interlock program setup |
| 902 | `ScInterlockProgramInputSetup` | F | F | interlock input setup |
| 903 | `ScInterlockConditionSetup` | F | F | interlock condition setup |
| 904 | `ScInterlockMngSelect` | F | F | interlock management point selection (an analog condition's reference point) |
| 905 | `ScInterlockProgramOutputSetup` | F | F | interlock output setup |
| 906 | `ScInterlockActionSetup` | F | F | interlock action setup |
| 1000 | `ScBootSequence` | X | X | boot sequence |
| 1100 | `Remote management` | V | F | remote management: status (V); connect/disconnect guarded (F) |
| 1200 | `ScSysWebAccessUser` | V | V | web users: add, change, delete |
| 1201 | `ScSysWebAccessRegistScreen` | V | F | a normal web user's allowed screens (refused for administrators: the button is disabled for them; read validated on an iTM without graphical screens, which offers none to allow) |
| 1202 | `ScSysWebAccessManageArea` | - | V | a web user's managed zones |
| 1203 | `ScMainLogin` | V | - | login screen |
| 1204 | `ScSysWebAccessUserSetup` | - | V | web user setup: name, password, type |
| 1205 | `ScWebUserSetup` | V | V | the logged-in user's own settings, opened through the login screen's user settings button: read, icon colour, password, date format, language and time/decimal formats (V) |
| 1300 | `ScEmlSetting` | V | V | e-mail notification: addresses, activation per address |
| 1301 | `ScEmlAddrEdit` | V | V | e-mail address edit |
| 1302 | `ScEmlTrial` | - | F | test e-mail (guarded: allow_send); accepted live, delivery unconfirmed |
| 1303 | `ScEmlServerSetting` | V | V | e-mail server settings (password never logged) |
| 1400 | `ScSrvEnergySavingSuspend` | F | F | energy saving suspend / resume |
| 1500 | `ScSysTimezoneSetting` | V | - | service: time zone |
| 1501 | `ScSrvActivation` | V | X | service: activation (licence keys counted; MAC not read) |
| 1502 | `ScSrvOptionSetup` | V | X | service: dealer options |
| 1503 | `ScSrvHistoryMng` | - | X | service: history management (delete only) |
| 1504 | `ScSrvD3Engineering` | - | X | service: DIII-NET management |
| 1505 | `ScSrvChangeOverOption` | V | - | service: changeover option per unit |
| 1506 | `ScSrvACAutoRegister` | - | X | service: A/C auto registration (bus scan) |
| 1507 | `ScSysCentralCtrlSetting` | V | - | service: central monitoring |
| 1508 | `ScSrvEnergySaving` | V | F | service: energy saving status (read V; disable guarded, F) |
| 1509 | `ScSrvContactSetup` | V | - | service: contact information |
| 1510 | `ScSrvLayoutSetup` | - | F | service: layout setup (a job without screen parts; guarded) |
| 1511 | `ScSrvEneSaveModeSelect` | V | - | service: energy save mode |
| 1512 | `ScSrvDemandPulseSetup` | F | F | service: demand pulse setup (guarded) |
| 1513 | `ScSrvDemandCtrlInterlockSetup` | F | F | service: demand control interlock setup (guarded) |
| 1514 | `ScSrvDemandInputPortSetup` | F | F | service: demand input port setup (guarded) |
| 1515 | `ScSrvServicePassword` | - | X | service: service password |
| 1516 | `ScSrvR32Setting` | V | F | service: R32 alarm settings (read V; output ports guarded) |
| 1517 | `ScSrvR32OutputPortSetup` | V | F | service: R32 output port setup (read V; guarded) |
| 1600 | `ScSysAreaSetup` | V | V | zones: list, add, delete |
| 1601 | `ScSysAreaMember` | V | V | zone members |
| 1602 | `ScSysAreaOrderSetup` | P | V | zone member order: up button and Jump (Jump lists the other members; the member lands after the picked one) |
| 1603 | `ScSysAreaAttributeSetup` | V | V | zone attributes: name, description, intervals, icon |
| 1604 | `ScSysAreaIconSetup` | - | V | icon list for zones and points |
| 1605 | `ScSysAreaMove` | - | V | move a zone |
| 1606 | `ScSysAreaErr` | F | - | zone error: its text is reported as an error by the zone writes |
| 1700 | `ScSysMngSetup` | V | - | management points |
| 1701 | `ScSysMngAttributeSetup` | - | V | point attributes: name, description, icon |
| 1702 | `ScSysMngIconSetup` | - | V | point icon |
| 1800 | `ScSysNetworkSetting` | V | D | network settings (writes guarded; validated as a live dry run) |
| 1801 | `ScSysWebserverSetting` | - | F | web server port (guarded: allow_network_change) |
| 1802 | `ScSysWebIFServerSetting` | - | F | web interface server port (guarded) |
| 1900 | `ScSysPasswordSetting` | F | F | password setting: administrator and screen password (guarded) |
| 2000 | `ScSysScreensaverSetting` | V | V | touch panel screensaver |
| 2100 | `ScSysCheckSetting` | V | V | confirm dialogs on/off |
| 2200 | `ScSysHardSetting` | V | V | touch panel backlight, buzzer, key click |
| 2300 | `ScSysCalibration` | X | X | touch panel calibration (needs a finger on the panel) |
| 2400 | `ScSysTimeSetting` | V | V | clock and summer time (a clock with a time zone refused) |
| 2500 | `ScSysLocalSetting` | V | V | region: language, date format, 12/24h (V); °C/°F, decimal separator (F) |
| 2501 | `ScSysLanguageSetting` | V | V | display language |
| 2600 | `ScSysVersionInfo` | V | - | version information |
| 2700 | `ScSysInspection` | V | V | maintenance flags |
| 2800 | `ScMngMain` | V | X | service: management point data registration list (read only, validated; writes out of scope) |
| 2801 | `ScMngTypeSelection` | - | X | service: management point data registration (installer) |
| 2802 | `ScMngSettingIndoor` | V | X | service: an indoor unit's registration (read only with details, validated; writes out of scope) |
| 2803 | `ScMngSettingOutdoor` | - | X | service: management point data registration (installer) |
| 2804 | `ScMngSettingHrvChiller` | V | X | service: a ventilation unit's registration (read only with details, validated; writes out of scope) |
| 2805 | `ScMngSettingDi` | - | X | service: management point data registration (installer) |
| 2806 | `ScMngSettingD3DiDio` | - | X | service: management point data registration (installer) |
| 2807 | `ScMngSettingExternalDi` | - | X | service: management point data registration (installer) |
| 2808 | `ScMngSettingExternalDio` | - | X | service: management point data registration (installer) |
| 2809 | `ScMngSettingExternalAi` | - | X | service: management point data registration (installer) |
| 2810 | `ScMngSettingExternalAo` | - | X | service: management point data registration (installer) |
| 2811 | `ScMngSettingPseudoAi` | V | X | service: an internal analog input's registration (read only with details, validated; writes out of scope) |
| 2812 | `ScMngSettingPi` | - | X | service: management point data registration (installer) |
| 2813 | `ScMngSettingExternalPi` | - | X | service: management point data registration (installer) |
| 2814 | `ScMngSettingPseudoPi` | - | X | service: management point data registration (installer) |
| 2815 | `ScMngPointSelection` | - | X | service: management point data registration (installer) |
| 2816 | `ScMngCheck` | - | X | service: management point data registration (installer) |
| 2817 | `ScMngMerge` | - | X | service: management point data registration (installer) |
| 2818 | `ScMngACAutoSearch` | - | X | service: management point data registration (installer) |
| 2819 | `ScMngTypeSelection2` | - | X | service: management point data registration (installer) |
| 2820 | `ScMngError` | - | X | service: management point data registration (installer) |
| 2821 | `ScMngTypeSelectionSub` | - | X | service: management point data registration (installer) |
| 2824 | `ScMngSettingHydrobox` | - | X | service: management point data registration (installer) |
| 2825 | `ScMngSettingInvChiller` | - | X | service: management point data registration (installer) |
| 2826 | `ScMngSettingGeneralAHU` | - | X | service: management point data registration (installer) |
| 2827 | `ScMngSettingGeneralChiller` | - | X | service: management point data registration (installer) |
| 2900 | `ScPPDSetup` | V | - | PPD: period and use per unit (calculation: F) |
| 2901 | `ScPPDAdvancedSetup` | V | V | PPD advanced setup (weekday on/off validated; an unticked weekday sends no check state) |
| 3000 | `ScSetbackMain` | V | V | setback offsets |
| 3001 | `ScSetbackAdvanced` | V | V | setback recovery temperatures |
| 3100 | `ScLeakageChkOutdoorSetup` | V | F | service: leakage check outdoor units (read V, on an empty list); start/stop guarded (allow_leakage_check) |
| 3101 | `ScLeakageChkIndoorRegist` | F | F | leakage check: indoor units per outdoor unit |
| 3102 | `ScLeakageChkScheduleSetup` | F | F | leakage check: schedule |
| 3103 | `ScLeakageChkCopy` | F | F | leakage check: schedule copy |
| 3200 | `ScEnergyNaviPlanManage` | V | - | energy navigator: budget overview per tab (fetch_energy_navigator; validated on empty screens, without a licence) |
| 3201 | `ScEnergyNaviPlanSetting` | V | - | energy navigator: planning settings (the way to 3202/3203) |
| 3202 | `ScEnergyNaviEnergyPlanSetting` | V | F | energy navigator: planned consumption per fiscal year (read V; ratio, disabled while per month, and per-month targets F) |
| 3203 | `ScEnergyNaviEnergyConsumeSetting` | V | F | energy navigator: actual consumption per fiscal year, CO2 factor (read V; monthly values, CO2 factor F: empty without energy data) |
| 3204 | `ScEnergyNaviDisplaySetting` | V | - | energy navigator: display settings (the way to energy types and groups) |
| 3205 | `ScEnergyNaviEnergyTypeList` | V | V | energy navigator: energy types: create, rename, delete |
| 3206 | `ScEnergyNaviEnergyTypeSetting` | V | - | energy navigator: an energy type's settings |
| 3207 | `ScEnergyNaviEnergyGroupList` | V | V | energy navigator: energy groups: create, rename, delete |
| 3208 | `ScEnergyNaviEnergyGroupSetting` | V | - | energy navigator: an energy group's settings |
| 3209 | `ScEnergyNaviLegendPlanManage` | X | X | energy navigator legend (static help, no data) |
| 3210 | `ScEnergyNaviEquipmentManage` | V | - | energy navigator: equipment management; the sampling period and target are a view filter (no save) |
| 3211 | `ScEnergyNaviDetailInfo` | F | - | energy navigator: equipment details (only offered with data) |
| 3212 | `ScEnergyNaviLegendDriveInfo` | X | X | energy navigator legend (static help, no data) |
| 3213 | `ScEnergyNaviRuleList` | V | V | energy navigator: management rule list: create, rename, delete, copy under a new name |
| 3214 | `ScEnergyNaviRuleEdit` | V | - | energy navigator: rule editor (read) |
| 3215 | `ScEnergyNaviCopySelect` | - | - | energy navigator: rule copy screen (not opened by the rule list's Copy, which asks a new rule's name) |
| 3216 | `ScEnergyNaviCalenderCopy` | V | V | energy navigator: calendar copy (validated with empty calendars) |
| 3217 | `ScEnergyNaviRuleSetting` | V | V | energy navigator: rule settings: time ranges per day and their cool/heat setpoints |
| 3218 | `ScEnergyNaviExtractSetting` | V | - | energy navigator: sampling targets (a view filter, no save) |
| 3219 | `ScEnergyNaviDataOutput` | V | V | energy navigator: data output periods and exports (energy data, point list) |
| 3220 | `ScEnergyNaviTrialSetting` | V | F | energy navigator installer settings (read V; the data clear button is never pressed) |
| 3221 | `ScEnergyNaviRoomTempSetting` | V | F | energy navigator: room temperature per point (read V; guarded; saving restarts the iTM) |
| 3222 | `ScEnergyNaviMngSelect` | F | F | energy navigator: analog point selection |
| 3223 | `ScEnergyNaviCollectStartSetting` | V | F | energy navigator: data collection start month (read V; guarded; saving restarts the iTM) |
| 3224 | `ScEnergyNaviExchangeTypeSetting` | V | F | energy navigator: energy conversion (read V; guarded; saving restarts the iTM) |
| 3300 | `ScDemandControlMain` | F | - | demand control (needs the dealer option and a pulse input) |
| 3301 | `ScDemandSetup` | F | F | demand control (needs the dealer option and a pulse input) |
| 3302 | `ScDemandLegend` | F | - | demand control (needs the dealer option and a pulse input) |
| 3303 | `ScDemandGraph` | F | - | demand control (needs the dealer option and a pulse input) |
| 3304 | `ScDemandDataDaily` | F | - | demand control (needs the dealer option and a pulse input) |
| 3305 | `ScDemandDataMonthly` | F | - | demand control (needs the dealer option and a pulse input) |
| 3306 | `ScDemandDataYearly` | F | - | demand control (needs the dealer option and a pulse input) |
| 3307 | `ScDemandTempSetup` | F | F | demand control (needs the dealer option and a pulse input) |
| 3308 | `ScDemandCapacitySetup` | F | F | demand control (needs the dealer option and a pulse input) |
| 3309 | `ScDemandOnOffSetup` | F | F | demand control (needs the dealer option and a pulse input) |
| 3310 | `ScDemandControlStatus` | F | - | demand control (needs the dealer option and a pulse input) |
| 3311 | `ScDemandGroup` | F | F | demand control (needs the dealer option and a pulse input) |
| 3312 | `ScDemandTargetPowerSetup` | F | F | demand control (needs the dealer option and a pulse input) |
| 4000 | `ScSetupExport` | - | V | setup export (export_setup; a download, sensitive) |
| 4100 | `ScSysBackup` | - | F | backup (run_backup; a job on the device, treated as a write) |
| 20000 | `ScSample` | X | X | sample screen (unused) |
