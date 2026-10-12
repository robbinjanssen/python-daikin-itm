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

## Installation

```bash
pip install python-daikin-itm
```

## Datasets

### Unit

This object represents the current state of an indoor unit, as shown on the
iTM's operation panel. `fetch_units()` returns them by unit name.

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
| `setback` | boolean | Property: whether the unit is in setback. |

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
| `modes` | tuple | Property: the normalized modes the unit offers. |
| `key` | string | Property: a stable identifier, the bus address when known, else the name. |

The fields from the Details screen (`point_id` to `zones`) are `None` (or empty)
when the iTM doesn't show them.

#### Operation modes

The iTM shows localized mode labels. The client normalizes the ones it knows to
`heat`, `cool`, `fan`, `dry` and `auto`; the original label stays available in
`mode_label` / `mode_labels`. Which modes a unit offers depends on the unit, see
`Capabilities.modes`.

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
            print(f"{unit.name}: {unit.mode} {unit.setpoint} °C, room {unit.room_temperature} °C")

        capabilities = await client.fetch_capabilities()
        for name, caps in capabilities.items():
            print(f"{name} ({caps.key}): modes {caps.modes}, fan control: {caps.fan_speed}")

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

### Methods

| Method | Description |
| :----- | :---------- |
| `fetch_units()` | Read every unit in the zone path, returns `dict[str, Unit]` by name. |
| `fetch_unit(name)` | Read a single unit (cheaper than `fetch_units()`). |
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

### Exceptions

| Exception | Description |
| :-------- | :---------- |
| `ItmError` | Base class for everything this library raises. |
| `ProtocolError` | A reply could not be decoded (not an iTM reply, or an unknown layout). |
| `LoginRejected` | The iTM refused the login: wrong credentials, or another web session is active. |
| `SessionEnded` | The iTM ended the session (it answered with a LOGOUT update), e.g. because the token was stale. |
| `VersionMismatch` | The iTM expects another web UI version string (subclass of `SessionEnded`); carries it in `.version`. |
| `ItmCommandError` | The iTM answered a command with a dialog, the UI was not as expected, or a change was not confirmed in time. |
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
itm caps                  # modes / fan control per unit
itm setpoint 1:1-04 21    # each write returns once the unit shows the change
itm power 1:1-09 on
itm mode 1:1-04 heat      # heat / cool / fan / dry / auto, as offered by the unit
itm fan 1:1-04 low        # low / medium / high / auto
```

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
[contribution guidelines](CONTRIBUTING.md).

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
and `tests/fixtures/` holds replies captured from a real device.

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
