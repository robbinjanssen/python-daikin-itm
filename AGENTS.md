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
  __init__.py      public API (re-exports)
  client.py        ItmClient, the session stores, _Session (one logged-in conversation)
  exceptions.py    ItmError and subclasses
  models.py        Unit, Capabilities, FAN_SPEEDS, mode normalization, part IDs
  protocol.py      encoding and decoding of /cmd/ requests and replies
  state.py         UIState: the merged view of the screens built from delta replies
  cli.py           the itm command
  py.typed
tests/
  fake_itm.py                  simulated iTM used by the behaviour tests
  test_client.py, test_protocol.py, test_protocol_decoding.py, test_cli.py, test_security.py
  fixtures/                    replies captured from a real iTM
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

## Tests

- Behaviour tests run against `FakeITM` in `tests/fake_itm.py` (single session,
  delta replies, ignored setpoints on stopped units). Extend the simulator when
  the client starts relying on more of the iTM.
- Decoding tests use the replies captured from a real device in
  `tests/fixtures/`. Keep those files byte for byte as captured; the
  end-of-file, whitespace and codespell hooks skip them.
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
