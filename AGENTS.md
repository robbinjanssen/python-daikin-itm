# AGENTS.md

Guidance for AI coding agents working on this repository. Human contributors:
see [CONTRIBUTING](CONTRIBUTING.md).

## Project

`python-daikin-itm` (package `daikin_itm`) is an asynchronous Python client
(aiohttp) for the Daikin intelligent Touch Manager (iTM). It controls the
indoor units locally by speaking the protocol of the iTM's built-in web UI; no
cloud. It also ships a small `itm` CLI for debugging.

The Home Assistant integration
[home-assistant-daikin-itm](https://github.com/robbinjanssen/home-assistant-daikin-itm)
(same owner) depends on the public API. Don't break any of these without a
`breaking-change` release:

- Everything exported from `daikin_itm/__init__.py`.
- `ItmClient`: its constructor signature, public attributes and methods, and
  `_post`, which tests patch to fake the iTM.
- `daikin_itm.protocol`: `PART_UPDATE_CODE`, `SCREEN_UPDATE_CODE`, `Reader`
  and `Writer`. `tests/fake_itm.py` and its `tests/fake_itm_*.py` modules use
  them, and the integration copies those files into its own tests.

```text
daikin_itm/
  __init__.py         public API (re-exports)
  client.py           ItmClient (with the feature modules' *Mixin classes), the session
                      stores, the session lock per iTM user, Points (fetch_points' result)
  session.py          _Session: one logged-in session, the screen work behind ItmClient
  settings_dialog.py  the unit settings dialog (screen 7), a _Session mixin
  exceptions.py       ItmError and subclasses
  _util.py            helpers shared by the modules (T, ignore, check_wrong,
                      one_login, cleanup, cleanup_after, run_to_completion)
  _screens.py         screen helpers shared by the feature modules
  _bases.py           ClientCore, SessionCore: the internals the mixins use (typing only)
  models.py           Unit, UnitSettings, Capabilities, part IDs, screen readers
  lang.py             the labels the iTM only sends as text, per display language
  menu.py             the main menu's screens: IDs, models and readers
  system.py           system settings screens: models and readers
  service.py          the installer's service screens (read only, service login)
  changeover.py       auto changeover group writes (session and client mixins)
  schedule_edit.py    schedule writes: events, period, special days, calendar, copies
  schedule_days.py    a program's events on a date or in a range of time, computed
                      from it (no requests)
  zones_edit.py       zone tree, members, member order, icons; the zone error screen
  system_edit.py      e-mail, confirm dialogs, ports, remote management, region, clock,
                      hardware (session and client mixins)
  users_edit.py       web users, a web user's allowed screens, own settings (session
                      and client mixins)
  emergency.py        emergency stop programs, settings, release (never a start)
  dealer_functions.py temperature limit, sliding temperature, heating mode
                      optimisation (dealer options); dealer_cli.py their and
                      interlock's CLI commands
  interlock.py        interlock programs: list, enable, add, rename, copy,
                      delete, setup (inputs, outputs, conditions, actions)
  demand.py           demand (power limit) control and energy saving
  layout.py           the graphical layout view; layout_cli.py its CLI command
  point_registry.py   the installer's management point registrations (read only)
  service_edit.py     leakage check, layout setup, R32 output ports, password setting
                      (each write guarded)
  energy_navigator.py energy navigator readers
  energy_edit.py      energy navigator writes
  exports.py          downloads: history, energy data, point list, setup export,
                      backup; PPD
  history_export.py   parse_history_export: the history archive's records, in memory
  trace.py            the CLI's --trace (request numbers, replies without token)
  protocol.py         encoding and decoding of /cmd/ requests and replies
  state.py            UIState: the merged view of the screens built from delta replies
  cli.py              the itm command: parsing, sessions, the unit commands
  cli_common.py       output and argument helpers shared by the CLI modules
  check_cli.py        itm check shared-session and itm check reads: read-only checks
                      (the list of reads: ItmClient's fetch_* and list_zones, an
                      allowlist found by introspection)
  schedule_cli.py     schedules; energy_cli.py energy navigator, PPD, exports;
  system_cli.py       system, users, zones, region and clock; service_cli.py the
                      installer's service commands, demand, emergency stop
  py.typed
tests/
  fake_itm.py                  simulated iTM used by the behaviour tests: request
                               handling, replies, FakeITM's assembly
  fake_itm_base.py             its shared data, dataclasses and state
  fake_itm_panel.py            icon/list views, operation panel, settings dialog
  fake_itm_menu.py             main menu bar, menu, changeover, setback, history
  fake_itm_system.py           system settings and service screens, SystemWrites
  fake_itm_schedule.py         schedule screens and event editor
  fake_*.py                    the simulator's extra screens, one file per area
  export_archive.py            a made-up history archive
  test_*.py                    behaviour, decoding, CLI and security tests
  fixtures/                    iTM replies in the protocol's format, with
                               synthetic content
```

## Commands

The project uses [uv](https://docs.astral.sh/uv/) and
[prek](https://github.com/j178/prek).

```sh
uv sync                        # create .venv with the dev tools
uv run prek install            # git hook running all checks on commit
uv run prek run --all-files    # see below
uv run pytest                  # tests only
```

`prek run --all-files` runs Ruff (`ALL` rules) and Ruff format, codespell, mypy
(strict), pylint, pytest with at least 90% coverage (covdefaults), a `uv lock`
check and yamllint. Run all hooks before committing. The hooks refuse commits
to `main`; work on a branch.

## How the iTM behaves

Most of the client's design follows from these quirks; keep them in mind when
changing it.

- **One web session per user** (a second login with the same account is
  refused; another account works alongside, seen on a real iTM; how many users
  at once is untested). A wrong password and an account in use get the same
  reply (DLG_OPEN ERROR "Cannot log in", same timing), with no lock-out after
  either, and the holder's session survives the second attempt (seen on a real
  iTM): both are `LoginRejected`, which can't tell them apart. Outside a
  shared session every public call (a write together with the reads that
  check it) logs in, works and logs out, also on
  errors and cancellation: `_run_to_completion` finishes the logout even when
  the caller is cancelled, and the session lock is held until it has. A logout
  that fails then doesn't replace the call's error or cancellation (`cleanup`,
  see Conventions).
  The page token is handed to the `SessionStore` after every reply, and only
  cleared on a logout the iTM confirms (or ends itself, `SessionEnded`), so a
  session left behind can be released (`release_saved_session`) on the next
  login. A saved token the iTM keeps refusing is dropped after one try.
  `_session_lock()` is the session lock: the client's own `_lock`, then the
  lock of its iTM and user (`_USER_LOCKS`, keyed by event loop, the base URL
  without case, default port and trailing slash (`_lock_origin`), and username,
  held weakly so an entry lives only while a call holds or waits for it). So
  clients of one user in a process take turns too. The public
  `release_saved_session` returns False while either lock is held and
  otherwise takes the session lock itself; the callers that hold it already use
  `_release_saved_session`. Only one task per client waits for the user's lock,
  so `other_client_timeout` (`SessionInUse`) only limits the wait for another
  client; it counts from when the client began that wait (`_waiting_since`),
  so the calls queued behind it fail together. Tested against the simulated
  iTM only.
- **A shared session** (`ItmClient.shared_session()`) lets the reads inside its
  block use one login. Not validated on a real iTM yet. How it works, for
  changes to it or to a reader:
  - `_session()` is the one place that hands out a session. Inside a block it
    hands out the block's (`_SharedBlock`), found through a context variable:
    the block belongs to the task that opened it and to the tasks started
    inside it, which take turns on the block's lock. Other tasks, and other
    clients of the same iTM and user, wait for the session lock, which the
    block holds until it has logged out (so a block whose task waits for a read
    by a task started outside it never ends; for another client's read, that
    read raises `SessionInUse` after its limit). A block inside a block joins
    it. `release_saved_session` releases nothing while the block is logged in,
    whichever task calls it: the block holds the session lock.
  - Every reader assumes a fresh login: the top level of the icon view, with no
    menu, screen or dialog open. `_Session.mark_start` records that start
    position after the login (the screens open, the icon view's path and
    icons). `back_to_start` returns to it after each call: it closes what is
    still open (`_CLOSERS`), the menu, and clicks the icon view's Top button,
    which is disabled at the top level and so never clicked there. `at_start`
    checks it before the next call, after a POLLING event: that brings the main
    menu bar (clock, indicators) up to date, shows whether the iTM still knows
    the session, and names the icon the operation panel shows, which must be
    one of the top level's.
  - The session is logged out, and the next call logs in again: when a call
    raised (its state is unknown; on cancellation the logout is completed
    first), when the way back fails, when the login is older than
    `max_seconds`, and after what only a new login undoes
    (`_Session.no_way_back`). That is: a main view switched (the iTM hides the
    list and layout views instead of closing them, and has part types for
    their changes, such as PNTLIST_STATE, that this client doesn't merge; not
    the analog list, `_analog_list`, which kept a shared and a kept session on
    a real iTM: the reads after it, the list again included, gave what they
    give alone), a value entered in a DATE, TIME or PASSWORD dialog or one of
    another type (the history's date, which it opens on again; the clock; a
    password), service mode. `_track` keeps each open dialog's type from its DLG_OPEN and
    decides when it is answered: a name, a number or a time of day (CHARINPUT,
    NUMBER, HOURMIN) is saved with its screen's OK and keeps the session (live
    validation pending). A reader that changes something the next reader would
    take for the state after a login must call `no_way_back`, also when it
    types one of those into a screen it doesn't save (the PPD calculation day).
  - A reader that steps to another day or page (the day overview's Next, the
    history's older pages) calls `_Session.step_back` instead: in a shared
    session it presses the way back as often, and the session stays only when
    the screen then shows what it showed first (a press that raises, or a start
    that couldn't be read, ends it). Outside a shared session it presses
    nothing. The screen may open on that day or page again next time, so a
    step that isn't undone ends the session.
  - A screen that was closed is taken to open complete and with its defaults
    again. Complete is what the readers that open a screen several times in one
    session rely on (a schedule's events, the capabilities), and so does a menu
    write read back in its own login (`confirm_in_session`): the iTM sends no
    check state for an unticked checkbox, so a reopen that came back incomplete
    would read a checkbox as unticked rather than as missing. Complete was seen
    on a real iTM for the reads `itm check shared-session` runs, and for a
    schedule program's period checkbox unticked and read back in the write's
    login (checked against another account's fresh read); the defaults are an
    assumption until validated on a real iTM.
  - Writes are refused with `SharedSessionError` by `_writable()`, which every
    public write calls before it sends or reads anything, and by the session's
    `require_writable()` as a backstop. `_own_session()` refuses as well: it is
    a login of its own (a write block of another task it waits for instead).
- **A kept session** (`ItmClient.kept_session()`; validated on one real iTM:
  2 hours of reads, 20 idle minutes with the keep-alive, a 60-75 s network
  drop, unit, zone and menu writes read back over 3 rounds; an iTM restart on
  the simulated iTM only) is a `_SharedBlock` with `kept` set, held for a
  polling client's lifetime: the same `_session()`, start position,
  `back_to_start`, `at_start` and `no_way_back` rules, and the session lock
  held for the whole block (use a dedicated administrator account). What differs:
  - Keep alive: a background task (`_keep_alive`) sends one POLLING when no
    request was sent for `keep_alive` seconds (`last_used`, set after each call
    and poll; the iTM ends an idle session after 4-5 minutes). It holds the
    block's lock while it polls, is cancelled and awaited (under
    `_run_to_completion`) before the block's logout, and never raises: a
    `SessionEnded` drops the session, no answer keeps it, a session no longer
    at its start is logged out.
  - Recovery: `_kept_up_to_date` replaces `_up_to_date` before each call. A
    POLLING that gets no answer (`aiohttp.ClientError`, `TimeoutError`) keeps the
    session (`block.unanswered`, so `_session()` doesn't log it out) and the
    call raises; the next call polls with the same token again. `SessionEnded`
    or another error: a new login, after `_release_saved_session`. A call that
    raises half way ends the login as in a shared session. `max_seconds` is the
    block's `max_age` (logged at INFO).
  - Writes are allowed, and their dry runs (a write not validated yet still
    needs `allow_experimental`, through `_experimental()`, a dry run of it
    too); `_own_session()` stays refused. They take the usual paths and read
    back in the kept session, never from a pick: `UIState` marks the values the
    client set itself (`select_locally`, `set_check`) until the iTM
    sends that part or closes its screen (`picked()`). Showing another point
    does not clear a mark: the iTM sends a part only when the new point's value
    differs from what it takes the client to show, and whether that is the pick
    or the value before it is unknown. `back_to_start` shows the other icons
    one by one, at the call's level and at the top, until no pick is left
    (`_show_other_icon`), and `at_start` is False while any open screen holds a
    pick, so such a session is logged out instead of read.
  - Ownership: the kept block is the client's (`ItmClient._kept`, checked first
    by `_shared()`), not the context's: calls from any task use it and take
    turns on its lock, so a poller and a service call share it instead of
    waiting for the block to end. The keep-alive task logs an unforeseen error
    and goes on; `_end_kept` logs out whatever the task did.
- **A write block** (`confirm_in_session`, on by default) runs a write and the
  reads that check it in one login. `@one_login` (`_util.py`) wraps every
  public write that reads back in `ItmClient._write_session()`: a `_SharedBlock`
  with `writing` set, belonging to the client (`ItmClient._writing`, after the
  kept block and the context's shared blocks in `_shared()`), with the same
  `_session()`, start position, `back_to_start`, `at_start`, `no_way_back` and
  pick rules as the kept session, and `_up_to_date` (not the kept recovery)
  before each call. Seen on a real iTM for unit writes, a zone write and the
  setpoint (in a kept session, which reads back the same way); a menu write's
  read-back in the same login only in the simulated iTM.
  - Opening it sends nothing and takes no lock: the first `_session()` in it
    takes the session lock (`held`), so `read_only`, `_experimental` and the
    other guards inside the write still refuse first, without waiting for
    another client. `_writable()` allows writes in it, the session's
    `require_writable()` too (`_Session.writing`).
  - Other calls of the client, from any task, use it and take turns on its
    lock; a write among them joins it (`writers`), and so does the background
    check of a write without confirm (`_watch`), and the block logs out and
    lets go of the session lock (`_leave_write`, `_end_write` under
    `_run_to_completion`) when the last write or check is done. A joining
    write never needs the session lock while holding a unit lock, so a zone
    write (session, then unit locks) and a unit write (unit lock, then
    session) can't deadlock. `shared_session()`,
    `kept_session()` and `_own_session()` don't join a write block
    (`_joinable()`): they wait for the session lock, so a shared session still
    refuses writes and a kept one keeps its own rules.
  - Inside a kept or shared block `_write_session()` does nothing: a write uses
    the kept login or is refused.
  - Left out on purpose (a login of their own for the read-back, as before):
    `set_network` (the address may change), `set_language` and `set_region` (a
    display setting may apply only to the next login), `set_own_settings`
    (`_own_session()`), the energy navigator's installer settings (restart),
    and the writes without a read-back. A new write that reads back gets
    `@one_login` unless it is one of those kinds.
- **Replies are deltas** against what the iTM believes the client shows, and it
  never echoes a value the client picked. `UIState` merges the replies and
  `UIState.select_locally` applies the client's own selections, like the
  browser does. A change is verified in the write's own session by reading what
  the iTM sends again (a panel value only once the iTM sent it over, a dialog or
  screen opened anew), never the client's pick; with `confirm_in_session=False`
  in fresh logins. A value typed in a dialog never enters `UIState`: `send`
  mirrors no answer to an open dialog as a pick, so a typed name or number is
  read back only as the iTM sends it.
- **Setpoints are ignored while a unit is stopped** on the operation panel, so
  `set_setpoint` refuses them by default. The settings dialog's setpoint (part
  20 checkbox, 21 text, 23 "change" button opening a NUMBER dialog of kind 2
  whose range follows the unit's setpoint limit) is kept while the unit is
  stopped and shown on the panel some 3-4 s later (seen on a real iTM in heating
  and cooling, running and stopped; with a mode picked in the same dialog its
  range follows the new mode): `set_setpoint(...,
  when_stopped="dialog")` and `apply(..., setpoint=...)` use it, checked by the
  panel's setpoint as any unit write. Setpoints are whole degrees in every
  reading so far, so the API stays int.
- **Changes take 3-8 s to reach a unit** over the Daikin bus. Each write is
  confirmed by reads every `confirm_interval` until `confirm_timeout`
  (`_confirm`, `_confirm_zone`, `fetch_unit_settings`), in the write's login. Writes to a
  unit are serialized with a per-unit lock and spaced by `min_write_interval`
  (indoor units may take a limited number of setting writes; how many, and what
  the iTM forwards to a unit, is unknown). With `confirm=False` a write that
  sends everything returns `None`, and the next write to that unit first waits
  for the change to show (`_settle`).
  - Never write a value the unit has (decided 2026-11-20, "better safe": the
    wear of an unchanged value is unknown). `apply` compares the request with
    the unit as read in the write session and only sends what differs.
    `set_unit_settings` (`change_unit_settings`) changes only the settings the
    open dialog doesn't show yet (`settings_mismatch`, a reset always) and
    cancels it when nothing is left; it confirms inside the unit lock. A zone
    write (`_zone_write`) reads its points after the unit locks and the
    settles; `needs(u)` says what an indoor unit lacks. Nothing lacks (`done`):
    nothing is sent. Every indoor unit lacks the same: the zone's panel sends
    it (`_write_zone`, `send(s, lacks)`). Otherwise (`apart`: the zone holds
    only indoor units, all read) each one that lacks something gets only that
    through its own panel inside the zone (`_write(unit, ..., zone=zone)`,
    `send(s, lacks)`); a zone holding other points goes through the zone's
    panel with everything asked (`send(s, None)`).
  - Never lose a value left out (seen on a real iTM before this check: another
    account's change made just before was skipped and won). Whatever a write
    left out, all of it (`_write_unit`'s `change` returned what the unit
    shows; `_zone_write` found every member done) or part of it (`change` sent
    the rest and `skipped` returns a test for what it left out: `apply`'s
    `left`, `change_unit_settings(skipped=...)`; a zone's members not
    written), is checked by `_unchanged` (`_Left`: the name, the units the
    rest was sent to, every unit of the write, the call's unit locks) in the
    write's login and unit locks: `_verify` reads (`_until`) for
    `verify_timeout` (at most `confirm_timeout`, at least one read) until what
    was left out moves away (`kept` fails); then `again(last)` sends only what
    was left out and differs now (`change` runs again with `left`; a zone
    write sends to the members left out that lack something), once, counted
    and budgeted as any write (no third write, so no loop). With confirm the
    call runs `_verify` itself and `later` confirms a write after all
    (`NotConfirmed`); without a move it returns what was read last when
    nothing was sent, otherwise `later()`. Without confirm the call returns at
    once (`shown`: what the write read, or `None` when the rest was sent, that
    part registered in `_unconfirmed` first, so an error or a cancellation
    keeps it) and `_watch` runs `_verify` in a background task
    (`ItmClient._watches`): it takes over the unit locks (`locks.pop_all()`)
    and, in a write block, counts as one more writer, so the block's login
    stays for its reads and is logged out when it is done (`_leave_write`); a
    write after all is left in `_unconfirmed` for every unit of the write,
    not confirmed. `_watching` releases the locks, then leaves the block; an
    error is logged and noted (`_note_error`), never raised. `close()`
    cancels the tasks and waits for their clean-up (`_end_watches`;
    `_stop_watches` under `_run_to_completion`, so a `close()` cancelled
    meanwhile still finishes it, after a `sleep(0)` so a task not started yet
    runs its clean-up); leaving `kept_session()` waits for them first (cancels
    them after an error or a cancellation in the block, or when its exit is
    cancelled), then closes the block. A write that sends
    everything isn't watched; a dry run returns at once. A unit write is a
    `change(s)` closure handed to `_write_unit` with its check (`_Check`: the
    later, a read, the test), a zone write `needs` and a `send(s, lacks)`
    closure handed to `_zone_write`; keep new ones on them.
  - `_last_write` and the counts are kept only for a change sent
    (`change_sent`): `_write(unit, later, what)` and `_write_zone(zone, members,
    later, what)` call `_wrote`, which feeds `writes_sent`, the last day's
    times (`_write_times`, `recent_writes`), the 80% warning and `on_write`.
    `_check_budget` raises `WriteBudgetExceeded` in a unit write's unit lock
    before it settles and logs in, again after the settle, before a write
    after all, and in `_zone_write` before the change, for the units it writes
    (never in a dry run). Per client, in memory; an optional guard against a
    loop in the caller, not a measure of the units' wear.
  - The checks (`_confirm`, `_confirm_zone`, `_confirm_settings`) read through
    `_until`: a transport error or `ProtocolError` is "not seen yet" and the
    next read tries again (a new login in a write block, whose login the error
    ended); `LoginRejected` and other errors raise at once; no read starts after
    the deadline, the first always does; `NotConfirmed` carries the last error
    as `__cause__` (`last_seen` None when no read got an answer), and after a
    last read without an answer is of that error's type too
    (`NotConfirmed.after`, like `WriteOutcomeUnknown.after`), since that error
    was raised before.
  - `_seen` drops an `_unconfirmed` entry when `fetch_unit`, `fetch_units`
    (icon view), `fetch_points`, `fetch_zone_units` or `fetch_unit_settings`
    shows its change, recognised by its partial (`_shows`: `_confirm`'s check,
    `_confirm_zone`'s per indoor unit, `_confirm_settings`' changes); never in
    a write block's session (`_Session.writing`) or while the panel or the
    settings dialog holds a pick. Keep a new unit read calling it, and a new
    pending check a partial of one of those three.
- **The list view shows every unit of a zone in one reply** (`PNTLIST`, 25
  columns, `fetch_units(view="list")`), the icon view one operation panel per
  unit. The list's columns are addressed by index (the headers are localised),
  so a list with another number of columns is refused rather than misread
  (except a zone holding only zones: its short list of zones gives no units). A
  zone holding units of more than one kind (seen: indoor and ventilation units) gets
  the same short list; it is refused with a hint to use the icon view. A zone
  holding only analog inputs gets a list of five columns (name, status, value,
  blocked, error type; seen on a real iTM): the only place their present value
  shows (`analog_from_list`, `fetch_analog_values`, which checks the column
  count and that every row is an analog icon). The
  list view is left again before logout. It doesn't show the on/off options nor
  the setpoint range. Showing the units' list ends a shared session
  (`no_way_back`, as the main view switched); the analog list keeps it (seen on
  one real iTM, which sent the list again each time it was shown).
  `_show_icons` drops the list from `UIState` all the same, so a list read
  never takes an old list for the one shown.
- **Only a few labels carry meaning as text**: operation modes, wall remote
  locks and the status lines on the Details screen. `lang.py` maps them for the
  iTM's ten display languages; everything else goes by part ID, icon ID or
  index. Unknown mode labels are reported in `Capabilities.unknown_labels`,
  never dropped silently. Don't add code that depends on label text without a
  table there.
- **A zone holds more than indoor units.** The icon ID says what a point is
  (`PointKind`, `KIND_BY_ICON`): indoor unit, heat reclaim ventilation unit
  (on/off only, no mode or setpoint) or analog input (a measured value with an
  alarm limit; only its Details screen says what it watches). `fetch_points`
  returns them by kind, `fetch_units` marks `Unit.kind`. The main menu bar's
  error button (screen 0 part 19) is only visible while a unit has an error and
  opens screen 10; that screen has not been seen with content yet.
- **The main menu** (`menu.py`) is opened from the main menu bar (screen 0 part
  0) and its buttons open screens whether or not their tab is active, and
  whether or not the button is visible (the iTM hides, it doesn't block). Every
  read closes its screen with the Close/Cancel part in `CLOSE_PART`, never OK.
  Long lists (the history) load behind a PROGRESS dialog: the browser sends
  UPD_PROG with the job ID until the dialog closes, then a POLLING event brings
  the data (`wait_for_progress`, for up to `ItmClient.progress_timeout`
  seconds). Only a read passes `cancel=True` (the history's readers): it sends
  CANCEL_PROG when the time is up, before raising; a write's progress (a number
  entered) is never cancelled. The CANCEL_PROG reply isn't recorded live. The history's day and class filters are
  display settings.
- **A normal web user without allowed screens** (seen once) gets no menu list,
  and its login reply hides the main menu bar's menu button (screen 0 part 0
  VISIBLED 0; an administrator's shows it). `_Session.login` records that
  (`menu_available`, reported in `ItmIndicators.menu_available`), `open_menu`
  then raises `MenuNotAvailable` without pressing the button, and still on an
  empty reply to it (the fallback); the contents of
  its managed zone are the icon view's top level (a user managing two zones saw
  only the first one's, with no way to the other)
  (`FakeITM(normal_user=True)`).
- **Schedules** are read through the program's editor (104): the pattern bars
  give the event times (96 slots of 15 min per day, Sunday first), each
  weekday's event list (107) the events, and an event's own screen (109) its
  settings. That screen has the same part IDs as a unit's settings dialog; a
  setting is part of the event only when its checkbox is checked. The event
  list has separator rows between times: select events by `ScheduleEvent.row`,
  never by their index. `fetch_schedule` parses the event list's text and opens
  event screens only with `details=True` (cost). Leaving the editor with Cancel
  asks for confirmation, answered with Yes (`close_editor`); nothing is saved.
  The day overview (102) is the cheap read. The editor's first reply holds the
  calendar tab's option lists (pattern 32, months 37 and 43, weeks 44, weekdays
  46); a calendar row's words are read by their position in them
  (`calendar_values`), the period by its combos' positions (`period_dates`), so
  `schedule_days` needs no words of the display language (simulated iTM only).
- **Writes through the menu** follow one pattern (`_Session.write` and
  `press_ok`): make the picks, send the one request that persists them (OK, or
  an immediate button such as Enable), answer the iTM's CONFIRM dialog with
  Yes, raise on an ERROR/INFO dialog, and read back: in the write's login when
  it gets back to the start position, otherwise (a date, the clock or a
  password typed in a dialog, service mode, a changed top level) after a new
  login. With
  `ItmClient.dry_run` that one request is recorded in `planned` instead of sent
  and the screen cancelled; keep every new write on that path so dry runs and
  `read_only` stay complete. In the unit's settings dialog a setting is only
  changed when its checkbox is ticked first; the limit values are entered in a
  NUMBER dialog whose `data[2]` says how to answer it (0 int, 1 float, 2 °C as
  [whole, thousandths]) and `data[6..9]` its range.
- **Never click a button the iTM reports disabled** (`ENABLED 0`). Its web
  server doesn't guard against it: the request never gets an answer, the TCP
  connection dies after some 20 s and the session is lost (seen on firmware
  Ver1.32.00, by clicking Edit with a separator row selected). `_Session.send`
  and `write` refuse such a click on every screen except the unit settings
  dialog, whose controls open disabled until their checkbox is ticked. Use
  `s.click(screen, part, what)` to name the part in the error.
- **`read_only=True`** makes every write raise `ReadOnlyError` before a request
  is sent: the public write methods check it (`_writable()`, first thing, also
  before a read the write needs), and so do the session's commit, panel picks
  and the settings dialog's OK. Keep that guard on any new write; a shared
  session refuses writes in the same place. It refuses everything that makes
  the iTM run a job or produce a file too: the exports call `self._job(...)`
  first (`exports.download` refuses as a backstop), the backup is a write. Give
  a new export or job the same guard; `itm check` takes only `fetch_*` and
  `list_zones` (`check_cli.READ_PREFIXES`, `READ_NAMES`), so never name one
  of those `fetch_*`.
- **Experimental writes** (not validated on a real iTM) call
  `self._experimental("what it does")` first: without `allow_experimental=True`
  they raise `ExperimentalError` before anything is sent (read only refuses
  first; a dry run needs the opt-in too, as it still sends the steps before the
  change). Arguments of a validated method that
  aren't validated are checked one by one (`_EXPERIMENTAL_UNIT_SETTINGS`,
  `set_region`, `set_zone_member_order`).
- **Faults** (tested against the simulated iTM only; `FakeITM.lose_reply_next`,
  `hang_next`, `fault_on`, `restart()`):
  - Every request in `_Session.send` is bounded by `ItmClient.request_timeout`
    (an `asyncio.timeout` around `_post`, so a patched `_post` gets it too). A
    request that times out or loses its connection (not an HTTP error status:
    that is an answer) sets `_Session.unanswered`: from then on `send` raises
    `RequestSkipped` (a `TimeoutError`, not an `ItmError`) for anything but the
    logout, which is tried once with `logout_timeout` and keeps the token when it
    gets no answer either. `cleanup_after` ends quietly on `RequestSkipped`
    (a note "clean-up skipped"), so a hung iTM costs about one timeout. The kept
    session's POLLING retries (`_kept_up_to_date`, `_keep_alive_poll`) reset the
    flag on purpose; `block.unanswered` stays what keeps a kept session after its
    POLLING got no answer.
  - `_Session.change_sent` is set just before the request that makes a change:
    the operation panel's TIMER (`commit`), the settings dialog's OK
    (`close_settings`) and every `write` (so `press_ok` and its Yes). `_session()`
    turns a transport error or `ProtocolError` after it into
    `WriteOutcomeUnknown.after(...)`: an `ItmCommandError` that is also of the
    cause's type (a class made per type), with the cause's attributes and notes;
    this covers the call's own clean-up and logout too, not a write block's
    logout at its end (that runs after the check).
    A refused connection (`ClientConnectorError`) or a `RequestSkipped` stays as
    it is, as nothing was sent. `_write` and `_write_zone` then put the write's
    check in `_unconfirmed` for the unit or every member, so the next write
    settles first, as after `confirm=False`; they
    do so too for a cancellation after `change_sent`. A new request that makes a
    change must go through `commit`, `close_settings(ok=True)` or `write`.
  - The INIT handshake is sent once more after a broken connection
    (`aiohttp.ClientConnectionError` that isn't a timeout): it changes nothing.
  - `ItmClient.session_info` (`SessionInfo`) is read from the client alone:
    `_logged_in` (from `_Session.login`), `_polled` (from `poll`) and
    `_note_error` (from `_session()` and the keep-alive) feed it. Never put a
    token in it.
- **Typed command errors**: `UnitStopped` (a setpoint for a stopped unit or
  zone), `NotOffered` (a power state, mode, fan speed, dialog option, lock value
  or airflow direction the unit lacks, with `.options`; `SetpointOutOfRange` is
  one), `NotConfirmed` (a read-back that doesn't show the change, with
  `.last_seen`: `_confirm`, `_confirm_zone`, `_confirm_settings`, `check_wrong`
  and the setback, hardware and screensaver writes), all `ItmCommandError`s with
  the messages they had. Raise them in new code for those cases.
- **Opt-ins** (`allow_service_change`, `allow_network_change`, `allow_restart`,
  `allow_leakage_check`, `allow_password_change`, `allow_safety_change`,
  `allow_send`, `allow_remote_change`) are checked in the public method before
  anything is sent, through `_require_opt_in` where it fits, and refuse a dry
  run as well. A new risky write gets the same.
- **Web UI version**: when the iTM reports another version string (after a
  firmware update), `handshake` adopts it and logs a warning. With
  `pause_writes_on_version_change` it also records the change
  (`_version_changed`: the first version known and the new one) and
  `_check_paused()` (in `_writable()`, `_demand_writable()` and the session's
  `require_writable()`) raises `WritesPaused` until `confirm_web_version()`
  or a login sees the first version again; `session_info.writes_paused` shows
  it. `_verify` checks it before a write after all too, so the background
  check of a write without confirm sends nothing while paused (the
  `WritesPaused` is logged and noted in `session_info`, as its other errors).
  Reads never check it.
- **Replies are untrusted.** `Reader` bounds-checks every read and count (also
  the 2-byte ones, `count(wide=False)`), unknown screen update and dialog types
  and data after the end of a reply are rejected, replies over 1 MB are
  refused, and the CLI sanitizes everything it prints.
  Keep it that way when adding decoding or output.
- **Requests stay in bounds.** `_post` and `_get_bytes` never follow a redirect
  and raise `ProtocolError` for any status but 200 (an error status still as
  `aiohttp.ClientResponseError`). `Writer` refuses a number outside its field
  (-32768..65535 for a short, -2^31..2^32-1 for an int) and a string over 65535
  characters with `RequestValueError` (an `ItmError` and a `ValueError`);
  `Writer(request=True)`, which `encode_event` uses, also refuses a control
  character or a character outside the BMP (the simulator's replies may hold
  them). `login_payload` refuses a username over 9999 characters.
  `FileSessionStore` loads only a dict whose token passes `is_token`, for its
  host as `_lock_origin` normalises it; `_release_saved_session` forgets
  anything else a store hands back.
- **Secrets**: only the web login is hashed; a password typed in a PASSWORD
  dialog goes in clear over http, and `_Session.send` logs a warning the first
  time a client does that over http (`ItmClient.plain_http`). Never put a
  password, the login hash or a page token in a log, plan, error message, repr
  or trace; a dataclass field holding a password gets `repr=False`.
  `tests/test_secrets.py` runs a call for each kind of password, normal and
  dry run, against a simulator that has its screen, checks all of those and
  that the call warned over http; add a new one there.

## Conventions

- Python 3.11+. mypy runs in strict mode; type everything.
- Docstrings with `Args:`, `Returns:` and `Raises:` sections, each followed by a
  dashed underline, as in the existing code.
- Exceptions live in `exceptions.py` and are re-exported from `__init__.py`.
- Assign exception messages to `msg` before raising (`raise ItmError(msg)`).
- Only add a targeted `# noqa: CODE - reason` where a rule doesn't fit; don't
  disable rules globally.
- Close screens and dialogs, and log out, through `cleanup` (`_util.py`), never
  in a `finally` block: `async with _cleanup(lambda: s.close_screen(X)):`. Its
  steps run in order after the block, the first that fails ends them. After a
  block that returned, a failed step raises as before; after a block that
  raised or was cancelled, that error or cancellation goes on unchanged and the
  step's error is logged and added to it as a note. A clean-up that only runs
  on the way out of an error (an `except` that cancels a dialog, dismisses an
  error dialog or closes a screen before raising) goes through `cleanup_after`,
  never `contextlib.suppress`. A `finally` stays only for what can't fail, such
  as releasing a lock or recording a time.
- A new write that hasn't been validated on a real iTM must call the experimental
  guard (`self._experimental(...)`); once it is validated live, remove the guard
  and mark it V in `FEATURES.md`.
- A result field that changes by itself, such as a clock, gets
  `field(compare=False)` so a caller's change detection (`==`) works, and its name
  goes in `check_cli.VOLATILE`. A new `Capabilities` field gets a default and goes
  in `to_dict` and `from_dict` (which ignores keys it doesn't know), as callers
  store them across versions.
- Keep `FEATURES.md` current: when a screen gets a reader or a write, or is
  validated on a real iTM, update its row. Keep `ROADMAP.md` in step: move an item
  out of it once it is done, and give a reason for anything left out on purpose.
- English is the default: code, comments, docs, the fake iTM, the fixtures and
  the tests use the iTM's English labels. Other display languages live only in
  `lang.py`, and `tests/test_lang.py` checks each of them.
- Never put values from a real installation in the repo (names, addresses,
  descriptions, network settings, credentials, readings, schedules, captured
  dates, or the structure of the installation): fixtures hold synthetic content,
  tests and docs generic names.

## Tests

- Behaviour tests run against `FakeITM` in `tests/fake_itm.py` (one session per user,
  delta replies, ignored setpoints on stopped units). Extend the simulator when
  the client starts relying on more of the iTM.
- `tests/test_kept_session.py` covers the kept session: keep-alive against the
  simulator's idle expiry (`FakeITM.idle_timeout` by `FakeITM.clock`), recovery,
  `max_age` (by moving the block's `since`, not by sleeping), cancellation,
  other clients, calls from other tasks, and writes; with
  `FakeITM.sticky_picks` or `FakeITM.forgets_picks` and
  `FakeUnit.ignores_writes` a pick the unit didn't take stays on the client's
  panel (with `forgets_picks` also when another point with the same value is
  shown), and the tests show the read-back doesn't take it for the result.
- `tests/test_write_session.py` covers the write block: logins per write with
  and without `confirm_in_session` (`FakeITM.latency_logins` counts logins and
  POLLING events), a unit or zone member that doesn't take a write (also with
  no other point to send the pick over), another account's change during the
  check, another client of the user waiting for the whole write, cancellation
  (during the check and while waiting for the session), calls from other
  tasks, and the guards (read only, experimental, the opt-ins). A test whose
  logins depend on another task joining the write holds the write's check
  until it has (`Gated`), never on timing.
- `tests/test_shared_session.py` runs every public read alone and inside a
  shared session and compares the results, and calls every other public
  coroutine inside one to see it refused. A new read needs its arguments in
  `READS` there (the test fails without), with `ends_session` when it calls
  `no_way_back`. `tests/test_step_back.py` covers `step_back`'s outcomes.
  A read's variant with arguments in `itm check` (`fetch_points[thermostat]`,
  `fetch_history[pages]`) is added in `check_cli.build_plan`; in `CHANGES` a key
  `name[variant]` runs `name`.
  `tests/test_check_cli.py` checks that `itm check
  shared-session`'s list of reads holds none that ends the session (those are
  left out in `EXCLUDED`, for that command only); `LEFT_OUT` holds the reads
  that press a menu button that starts something, and `ZONE_SPECIFIC` those
  that read only a zone of one kind (`fetch_analog_values`), left out of both commands;
  the list takes only the allowlisted names (no export, backup or other job).
- `tests/test_experimental.py` and `tests/test_dry_run_opt_ins.py` cover the
  opt-ins: every experimental write and every write behind an allowance refuses
  without it, normal and dry run, before a request is sent; add a new one to
  `GUARDED` or `OPT_INS` there. `tests/test_version_pause.py` covers
  `pause_writes_on_version_change`, the write after all of a write's check
  included.
- `tests/test_robustness.py` covers the faults: the simulator's fault modes, the
  INIT sent again, a hung iTM (one timeout, the clean-up skipped, the logout
  tried once), lost replies before and after a change (`WriteOutcomeUnknown`,
  the next write settling), the progress wait by time and its cancel from
  reads only, `session_info`, and the typed command errors. `FakeClient._post`
  goes through `FakeITM.answer`, which can hang.
- `tests/test_unchanged_writes.py` covers never writing an unchanged value and
  never losing one, for every unit and zone write, with and without confirm:
  another client's change on its way (`FakeITM.change_elsewhere`,
  `changed_elsewhere`) overruled by one write sent after it, the truly unchanged
  write (no write, one login, returns after the window), a change that still
  wins (`NotConfirmed` after exactly one write), a write that changes everything
  returning at once without confirm, the background check without confirm
  (returns at once, one write after all, nothing after the window, the next
  write to the unit waiting for it, `close()` (also cancelled), a kept session's
  exit and its error or cancellation, errors noted in `session_info` and not
  raised; `_checked` waits for it), partial skips (`apply` mode and setpoint,
  power and mode; `set_unit_settings` with one of two locks shown: the write
  after all sends only the part left out), zone writes per unit (only the units
  that lack it, each with only what it lacks, a race on a unit left out, the
  zone's panel when every unit lacks the same or the zone holds other points,
  the budget of a unit left out, a unit left out missing from a read of the
  check), a dry run, no answer, cancellation, the budget and a kept session.
- `tests/test_write_accounting.py` covers the write counts and budgets
  (`writes_sent`, `on_write`, the hour and day windows, a zone write per
  member), the writes that change nothing (`set_unit_settings`, the zone
  writes), the settings check inside the unit lock, checks that survive a lost
  reply, and the reads that drop a pending check (and those that must not).
- `tests/test_cleanup.py` covers `cleanup` and its users: a cancellation or an
  error during a write with a schedule editor that doesn't close
  (`FakeITM.editor_stays_open`), per group of writers a refused write whose
  screens can't be closed either, and the `cleanup_after` users (settings
  dialog Cancel, service password error, zone error screen, "discard?").
- `tests/test_typed_values.py` covers the input dialogs: per dialog type
  whether an answer ends the session, and per group of writers that type a
  name, number or time of day one login with `confirm_in_session`, and an error
  when the iTM ignores the typed value (`FakeITM.ignores_typed_values` takes
  such an answer as Cancel).
- Decoding tests use the replies in `tests/fixtures/`: the real protocol format
  with synthetic content. Every reply must still decode after an edit (each
  string carries its length); the end-of-file, whitespace and codespell hooks
  skip them.
- Add a test for every bug fix that fails without the fix. Keep coverage at 90%
  or more.

## Pull requests and releases

- Every pull request needs one of these labels: `breaking-change`, `bugfix`,
  `hotfix`, `documentation`, `enhancement`, `refactor`, `performance`,
  `new-feature`, `maintenance`, `ci`, `dependencies`, `skip-changelog`.
- Release Drafter drafts the release notes and the next version from the labels
  (`.github/release-drafter.yml`; `breaking-change` → major, `new-feature` →
  minor, otherwise patch).
- Publishing a GitHub release runs `.github/workflows/release.yaml`: it sets the
  version from the tag with `uv version`, builds the package and publishes it
  to PyPI with the `PYPI_TOKEN` secret. The version in `pyproject.toml` stays
  `0.0.0`; don't bump it.
- Renovate keeps dependencies up to date and automerges minor and patch
  updates.
