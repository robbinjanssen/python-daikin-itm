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
  and `Writer`. `tests/fake_itm.py` uses them, and the integration copies that
  file into its own tests.

```text
daikin_itm/
  __init__.py         public API (re-exports)
  client.py           ItmClient (with the feature modules' *Mixin classes), the session
                      stores
  session.py          _Session: one logged-in session, the screen work behind ItmClient
  settings_dialog.py  the unit settings dialog (screen 7), a _Session mixin
  exceptions.py       ItmError and subclasses
  models.py           Unit, UnitSettings, Capabilities, part IDs, screen readers
  lang.py             the labels the iTM only sends as text, per display language
  menu.py             the main menu's screens: IDs, models and readers
  system.py           system settings screens: models and readers
  service.py          the installer's service screens (read only, service login)
  changeover.py       auto changeover group writes (session and client mixins)
  schedule_edit.py    schedule writes: events, period, special days, calendar, copies
  zones_edit.py       zone tree, members, member order, icons; the zone error screen
  system_edit.py      e-mail, confirm dialogs, ports, remote management, own settings,
                      users, region, clock, hardware (session and client mixins),
                      a web user's allowed screens
  emergency.py        emergency stop programs, settings, release (never a start)
  dealer_functions.py temperature limit, sliding temperature, heating mode optimisation,
                      interlock (dealer options); dealer_cli.py their CLI commands
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
  schedule_cli.py     schedules; energy_cli.py energy navigator, PPD, exports;
  system_cli.py       system, users, zones, region and clock; service_cli.py the
                      installer's service commands, demand, emergency stop
  py.typed
tests/
  fake_itm.py                  simulated iTM used by the behaviour tests
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

- **One web session at a time.** Every public call logs in, works and logs out,
  also on errors and cancellation: `_run_to_completion` finishes the logout
  even when the caller is cancelled, and the session lock is held until it has.
  The page token is handed to the `SessionStore` after every reply, and only
  cleared on a logout the iTM confirms (or ends itself, `SessionEnded`), so a
  session left behind can be released (`release_saved_session`) on the next
  login. A saved token the iTM keeps refusing is dropped after one try.
- **Replies are deltas** against what the iTM believes the client shows, and it
  never echoes a value the client picked. `UIState` merges the replies and
  `UIState.select_locally` applies the client's own selections, like the
  browser does. Verify a change in a fresh session, never in the one that made
  it.
- **Setpoints are ignored while a unit is stopped**, so `set_setpoint` refuses
  them.
- **Changes take 3-8 s to reach a unit** over the Daikin bus. Each write is
  confirmed by fresh reads (`confirm_timeout`, `confirm_interval`). Writes to a
  unit are serialized with a per-unit lock and spaced by `min_write_interval`
  (indoor units have a limited number of setting writes). With `confirm=False`
  a write returns `None`, and the next write to that unit first waits for the
  change to show (`_settle`). `apply` compares the request with the unit as
  read in the write session and only sends what differs; when nothing differs
  it writes nothing and records no unconfirmed change.
- **The list view shows every unit of a zone in one reply** (`PNTLIST`, 25
  columns, `fetch_units(view="list")`), the icon view one operation panel per
  unit. The list's columns are addressed by index (the headers are localised),
  so a list with another number of columns is refused rather than misread
  (except a zone holding only zones: its short list of zones gives no units). The
  list view is left again before logout. It doesn't show the on/off options nor
  the setpoint range.
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
  the data (`wait_for_progress`). The history's day and class filters are
  display settings.
- **Schedules** are read through the program's editor (104): the pattern bars
  give the event times (96 slots of 15 min per day, Sunday first), each
  weekday's event list (107) the events, and an event's own screen (109) its
  settings. That screen has the same part IDs as a unit's settings dialog; a
  setting is part of the event only when its checkbox is checked. The event
  list has separator rows between times: select events by `ScheduleEvent.row`,
  never by their index. `fetch_schedule` parses the event list's text and opens
  event screens only with `details=True` (cost). Leaving the editor with Cancel
  asks for confirmation, answered with Yes (`close_editor`); nothing is saved.
  The day overview (102) is the cheap read.
- **Writes through the menu** follow one pattern (`_Session.write` and
  `press_ok`): make the picks, send the one request that persists them (OK, or
  an immediate button such as Enable), answer the iTM's CONFIRM dialog with
  Yes, raise on an ERROR/INFO dialog, and verify in a fresh session. With
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
  is sent: the public write methods check it, and so do the session's commit,
  panel picks and the settings dialog's OK. Keep that guard on any new write.
- **Experimental writes** (not validated on a real iTM) call
  `self._experimental("what it does")` first: without `allow_experimental=True`
  they raise `ExperimentalError` before anything is sent (read only refuses
  first; a dry run is always allowed). Arguments of a validated method that
  aren't validated are checked one by one (`_EXPERIMENTAL_UNIT_SETTINGS`,
  `set_region`, `set_zone_member_order`).
- **Web UI version**: when the iTM reports another version string (after a
  firmware update), the client adopts it and logs a warning.
- **Replies are untrusted.** `Reader` bounds-checks every read and count (also
  the 2-byte ones, `count(wide=False)`), unknown screen update and dialog types
  and data after the end of a reply are rejected, replies over 1 MB are
  refused, and the CLI sanitizes everything it prints.
  Keep it that way when adding decoding or output.

## Conventions

- Python 3.11+. mypy runs in strict mode; type everything.
- Docstrings with `Args:`, `Returns:` and `Raises:` sections, each followed by a
  dashed underline, as in the existing code.
- Exceptions live in `exceptions.py` and are re-exported from `__init__.py`.
- Assign exception messages to `msg` before raising (`raise ItmError(msg)`).
- Only add a targeted `# noqa: CODE - reason` where a rule doesn't fit; don't
  disable rules globally.
- A new write that hasn't been validated on a real iTM must call the experimental
  guard (`self._experimental(...)`); once it is validated live, remove the guard
  and mark it V in `FEATURES.md`.
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

- Behaviour tests run against `FakeITM` in `tests/fake_itm.py` (single session,
  delta replies, ignored setpoints on stopped units). Extend the simulator when
  the client starts relying on more of the iTM.
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
