"""The ``itm check`` sub-commands: read-only checks of what an iTM account can read."""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import inspect
import operator
import sys
import time
from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass
from functools import partial
from typing import TYPE_CHECKING, Any, Final

import aiohttp

from .cli_common import echo as _out
from .client import ItmClient, Points
from .exceptions import LoginRejected
from .models import PointKind
from .protocol import EventType
from .trace import request_numbers

if TYPE_CHECKING:
    from collections.abc import Iterator

    from .menu import ScheduleSummary
    from .models import Unit

READ_PREFIXES: Final = ("fetch_",)
READ_NAMES: Final = frozenset({"list_zones"})

LEFT_OUT: Final = {
    "fetch_password_settings": "opens the password setting (menu button 33), "
    "never read on a real iTM yet",
}

EXCLUDED: Final = {
    "fetch_layout": "switches the main view: ends the shared session by design",
    "fetch_own_settings": "logs in by itself: refused inside a shared session",
}

ZONE_SPECIFIC: Final = {
    "fetch_analog_values": "reads only a zone that holds nothing but analog inputs",
}

VOLATILE: Final = {
    "read_at": "the moment of reading",
    "clock": "the iTM's clock",
    "clock_text": "the iTM's clock",
    "room_temperature": "room temperatures",
    "co2": "measured CO2",
    "DemandStatus.remaining": "the demand period's countdown",
    "DemandStatus.current": "measured power",
    "DemandStatus.predicted": "predicted power",
    "DemandStatus.adjustment": "power adjustment",
    "DemandStatus.graph": "the power graph",
    "DemandGraph.graph": "the power graphs",
    "DemandGraph.data": "measured power",
    "ActualYear.months": "actual consumption",
}

BLOCK_SECONDS = 3600.0

ENERGY_MORE: Final = (
    "energy_types",
    "energy_groups",
    "details",
    "rule_editor",
    "rule_settings",
)

SHOWN_VALUE_CHARS = 120
SHOWN_LINES = 40
MESSAGE_CHARS = 200
LABEL_WIDTH = 36
RESULT_WIDTH = 10
DAYS_AHEAD = 1
HISTORY_PAGES = 2

Call = Callable[[ItmClient], Awaitable[Any]]


@dataclass(frozen=True)
class Read:
    """One read to run: a client method, called without arguments or with fixed ones."""

    name: str
    call: Call
    variant: str = ""

    @property
    def label(self) -> str:
        """The name shown on the read's line."""
        return f"{self.name}[{self.variant}]" if self.variant else self.name


@dataclass(frozen=True)
class Derived:
    """Reads that depend on what another read finds, such as a program per name."""

    name: str
    source: str
    expand: Callable[[Any], list[Read]]
    what: str


@dataclass
class Outcome:
    """What one run of a read gave."""

    seconds: float
    value: Any = None
    error: str | None = None


def _points_with_thermostat(client: ItmClient) -> Awaitable[Any]:
    return client.fetch_points(thermostat=True)


def _schedule(name: str, client: ItmClient) -> Awaitable[Any]:
    return client.fetch_schedule(name)


def _schedule_day(name: str, client: ItmClient) -> Awaitable[Any]:
    return client.fetch_schedule_day(name, days_ahead=DAYS_AHEAD)


def _unit_settings(name: str, client: ItmClient) -> Awaitable[Any]:
    return client.fetch_unit_settings(name)


def _schedule_reads(programs: list[ScheduleSummary]) -> list[Read]:
    return [
        Read("fetch_schedule", partial(_schedule, p.name), str(i))
        for i, p in enumerate(programs, 1)
    ]


def _schedule_day_reads(programs: list[ScheduleSummary]) -> list[Read]:
    return [
        Read("fetch_schedule_day", partial(_schedule_day, p.name), str(i))
        for i, p in enumerate(programs, 1)
    ]


def _unit_settings_reads(units: dict[str, Unit]) -> list[Read]:
    names = [n for n, u in units.items() if u.kind is PointKind.INDOOR]
    return [
        Read("fetch_unit_settings", partial(_unit_settings, n), str(i))
        for i, n in enumerate(names, 1)
    ]


DERIVED: Final = (
    Derived(
        "fetch_schedule", "fetch_schedules", _schedule_reads, "one per schedule program"
    ),
    Derived(
        "fetch_schedule_day",
        "fetch_schedules",
        _schedule_day_reads,
        f"one per schedule program, days_ahead={DAYS_AHEAD}",
    ),
    Derived(
        "fetch_unit_settings",
        "fetch_units",
        _unit_settings_reads,
        "one per indoor unit",
    ),
)


def argument_free_reads() -> list[str]:
    """Return the names of the client's public reads that need no arguments.

    Only the names the allowlist takes (READ_PREFIXES, READ_NAMES): never an
    export, a backup or anything else that makes the iTM run a job.
    """
    names = []
    for name, method in inspect.getmembers(ItmClient, inspect.iscoroutinefunction):
        if not name.startswith(READ_PREFIXES) and name not in READ_NAMES:
            continue
        required = [
            p
            for p in inspect.signature(method).parameters.values()
            if p.name != "self" and p.default is p.empty
        ]
        if not required:
            names.append(name)
    return sorted(names)


def build_plan(left_out: Iterable[str]) -> list[Read | Derived]:
    """Return the reads to run, in order: each read, then what depends on it."""
    skipped = set(left_out)
    plan: list[Read | Derived] = []
    for name in argument_free_reads():
        if name in skipped:
            continue
        plan.append(Read(name, operator.methodcaller(name)))
        if name == "fetch_energy_navigator":
            plan.append(
                Read(
                    name,
                    lambda c: c.fetch_energy_navigator(ENERGY_MORE),
                    "more",
                )
            )
        if name == "fetch_points":
            plan.append(Read(name, _points_with_thermostat, "thermostat"))
        if name == "fetch_history":
            plan.append(
                Read(
                    name,
                    lambda c: c.fetch_history(pages=HISTORY_PAGES),
                    "pages",
                )
            )
        plan.extend(d for d in DERIVED if d.source == name)
    return plan


FIRST: Final = Read("fetch_info", operator.methodcaller("fetch_info"))
LEFT_OUT_BY_COMMAND: Final = {
    "reads": LEFT_OUT | ZONE_SPECIFIC,
    "shared-session": LEFT_OUT | EXCLUDED | ZONE_SPECIFIC,
}
PLANS: Final = {
    command: build_plan(left_out) for command, left_out in LEFT_OUT_BY_COMMAND.items()
}
SOURCES: Final = {d.source for d in DERIVED}


def _names(plan: list[Read | Derived]) -> set[str]:
    names = set()
    for item in plan:
        names.add(item.name)
        if isinstance(item, Read):
            names.add(item.label)
    return names


def list_reads(command: str) -> None:
    """Print the names of a check's reads, without contacting the iTM."""
    for item in PLANS[command]:
        if isinstance(item, Derived):
            _out(f"{item.name}  ({item.what}, found by {item.source})")
        else:
            _out(item.label)
    for name, reason in LEFT_OUT_BY_COMMAND[command].items():
        _out(f"not included: {name} ({reason})")


def _selected(name: str, label: str, args: argparse.Namespace) -> bool:
    if name in args.skip or label in args.skip:
        return False
    return not args.only or name in args.only or label in args.only


def _validate(args: argparse.Namespace) -> None:
    known = _names(PLANS[args.check_command])
    for given in (*args.only, *args.skip):
        if given not in known:
            sys.exit(f"unknown read {given!r}: --list shows the names")


def _text(err: BaseException, *, show: bool) -> str:
    if not show:
        return type(err).__name__
    message = " ".join(str(err).split())
    if len(message) > MESSAGE_CHARS:
        message = message[:MESSAGE_CHARS] + "..."
    return f"{type(err).__name__}: {message}" if message else type(err).__name__


def _leaf(value: Any) -> Any:
    if isinstance(value, bytes):
        return f"<{len(value)} bytes, sha256 {hashlib.sha256(value).hexdigest()}>"
    if isinstance(value, (set, frozenset)):
        return sorted(repr(v) for v in value)
    return value


def _leaves(
    value: Any, path: str = "", volatile: str | None = None
) -> Iterator[tuple[str, Any, str | None]]:
    """Yield a result's values as (path, value, volatile field it sits in)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        for f in dataclasses.fields(value):
            yield from _leaves(
                getattr(value, f.name),
                f"{path}.{f.name}",
                volatile or _volatile(value, f.name),
            )
    elif isinstance(value, Mapping):
        for key, item in value.items():
            yield from _leaves(item, f"{path}[{key!r}]", volatile)
        if isinstance(value, Points):
            yield from _leaves(value.thermostat, f"{path}.thermostat", volatile)
            yield from _leaves(value.indicators, f"{path}.indicators", volatile)
    elif isinstance(value, (list, tuple)):
        for i, item in enumerate(value):
            yield from _leaves(item, f"{path}[{i}]", volatile)
    else:
        yield path or ".", _leaf(value), volatile


def _volatile(owner: Any, name: str) -> str | None:
    qualified = f"{type(owner).__name__}.{name}"
    return next((key for key in (qualified, name) if key in VOLATILE), None)


def _flat(value: Any) -> dict[str, tuple[Any, str | None]]:
    return {path: (leaf, vol) for path, leaf, vol in _leaves(value)}


_MISSING: Final = object()


def compare(
    values: list[Any],
) -> tuple[list[tuple[str, list[Any]]], set[str], list[bool]]:
    """Compare results, leaving out the fields that change by themselves.

    Returns
    -------
        The paths whose values differ, with the values; the volatile fields that
        differed (and so were left out); and for each result whether it differs
        from the first.

    """
    flats = [_flat(v) for v in values]
    differing: list[tuple[str, list[Any]]] = []
    ignored: set[str] = set()
    apart = [False] * len(values)
    paths = dict.fromkeys(p for flat in flats for p in flat)
    for path in paths:
        entries = [flat.get(path, (_MISSING, None)) for flat in flats]
        leaves = [e[0] for e in entries]
        if all(leaf == leaves[0] for leaf in leaves):
            continue
        reasons = {e[1] for e in entries if e[1]}
        if reasons:
            ignored |= reasons
            continue
        differing.append((path, leaves))
        for i, leaf in enumerate(leaves):
            apart[i] = apart[i] or leaf != leaves[0]
    return differing, ignored, apart


def is_empty(value: Any) -> bool:
    """Tell whether a read gave nothing: None, empty, or only empty parts."""
    if value is None:
        return True
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return all(is_empty(getattr(value, f.name)) for f in dataclasses.fields(value))
    if isinstance(value, (str, bytes, list, tuple, dict, set, frozenset)):
        return not value
    return False


def _shown(value: Any) -> str:
    text = "<missing>" if value is _MISSING else repr(value)
    if len(text) > SHOWN_VALUE_CHARS:
        text = text[:SHOWN_VALUE_CHARS] + "..."
    return text


class _Logins:  # pylint: disable=too-few-public-methods
    """Count the logins a client makes: the requests that start one."""

    def __init__(self, client: ItmClient) -> None:
        self.count = 0
        post = client._post  # noqa: SLF001  # pylint: disable=protected-access

        async def counting(body: str) -> str:
            header = request_numbers(body).get("request")
            if header and header[2] == EventType.INIT:
                self.count += 1
            return await post(body)

        client._post = counting  # type: ignore[method-assign]  # noqa: SLF001  # pylint: disable=protected-access


class _Check:
    """The state of one run: the client, the logins used and what was found."""

    def __init__(self, client: ItmClient, args: argparse.Namespace) -> None:
        self.client = client
        self.show = args.show
        self.args = args
        self.plan = PLANS[args.check_command]
        self.logins = _Logins(client)
        self.worked = False
        self.found: dict[str, Any] = {}

    async def attempt(self, read: Read) -> Outcome:
        """Run a read; a failure is the outcome, unless the login itself failed."""
        started = time.perf_counter()
        try:
            value = await read.call(self.client)
        except LoginRejected:
            raise
        except (aiohttp.ClientError, TimeoutError) as err:
            if not self.worked:
                raise
            return Outcome(
                time.perf_counter() - started, error=_text(err, show=self.show)
            )
        # pylint: disable-next=broad-exception-caught
        except Exception as err:  # noqa: BLE001 - any failure is reported on its line
            return Outcome(
                time.perf_counter() - started, error=_text(err, show=self.show)
            )
        self.worked = True
        if read.name in SOURCES and not read.variant:
            self.found[read.name] = value
        return Outcome(time.perf_counter() - started, value)

    async def expand(self, item: Read | Derived) -> list[Read] | str:
        """Return a plan item's reads; a message when the read they follow failed."""
        if isinstance(item, Read):
            return [item]
        if item.source not in self.found:
            source = next(
                r for r in self.plan if isinstance(r, Read) and r.name == item.source
            )
            outcome = await self.attempt(source)
            if outcome.error is not None:
                return f"{item.source} failed: {outcome.error}"
        return item.expand(self.found[item.source])

    def items(self) -> Iterator[Read | Derived]:
        """Yield the plan items that were asked for."""
        for item in self.plan:
            label = item.name if isinstance(item, Derived) else item.label
            if _selected(item.name, label, self.args):
                yield item

    def show_value(self, value: Any) -> None:
        """Print a read's values, for --show."""
        leaves = list(_leaves(value))
        for path, leaf, _ in leaves[:SHOWN_LINES]:
            _out(f"    {path} = {_shown(leaf)}")
        if len(leaves) > SHOWN_LINES:
            _out(f"    ... {len(leaves) - SHOWN_LINES} more")


def _line(label: str, result: str, rest: str = "") -> None:
    _out(f"{label:<{LABEL_WIDTH}} {result:<{RESULT_WIDTH}} {rest}".rstrip())


async def _reads(client: ItmClient, args: argparse.Namespace) -> None:
    """Run every read once in its own session; show what the account may read."""
    _validate(args)
    check = _Check(client, args)
    ok = empty = failed = 0
    _out("itm check reads: read only, each read in a session of its own")
    for item in check.items():
        reads = await check.expand(item)
        if isinstance(reads, str):
            failed += 1
            _line(item.name, "error", reads)
            continue
        if not reads:
            _line(item.name, "none found")
            continue
        for read in reads:
            outcome = await check.attempt(read)
            if outcome.error is not None:
                failed += 1
                _line(read.label, outcome.error)
                continue
            if is_empty(outcome.value):
                empty += 1
                _line(read.label, "empty")
            else:
                ok += 1
                _line(read.label, "ok")
                if args.show:
                    check.show_value(outcome.value)
    _out(
        f"{ok + empty + failed} reads: {ok} ok, {empty} empty, {failed} failed; "
        f"logins used: {check.logins.count}"
    )


def _stages(alone: Outcome, first: Outcome, second: Outcome) -> list[str]:
    """Return what failed, by message: "alone, in block: the message"."""
    failed: dict[str, list[str]] = {}
    for name, outcome in (
        ("alone", alone),
        ("in block", first),
        ("in block again", second),
    ):
        if outcome.error is not None:
            failed.setdefault(outcome.error, []).append(name)
    return [f"{', '.join(names)}: {error}" for error, names in failed.items()]


@dataclass
class Totals:
    """What a shared-session run counted."""

    same: int = 0
    different: int = 0
    failed: int = 0
    split: int = 0
    alone_seconds: float = 0.0
    block_seconds: float = 0.0


def _print_differences(differing: list[tuple[str, list[Any]]]) -> None:
    for path, leaves in differing[:SHOWN_LINES]:
        shown = " | ".join(_shown(v) for v in leaves)
        _out(f"    {path}: alone | in block | again: {shown}")
    if len(differing) > SHOWN_LINES:
        _out(f"    ... {len(differing) - SHOWN_LINES} more")


def _compared(
    check: _Check, label: str, outcomes: list[Outcome], detail: str, totals: Totals
) -> None:
    """Compare a read's three results and print its line: same or different."""
    differing, ignored, apart = compare([o.value for o in outcomes])
    if ignored:
        detail += f", {', '.join(sorted(ignored))} ignored"
    if not differing:
        totals.same += 1
        _line(label, "same", detail)
        return
    totals.different += 1
    if apart[1] and apart[2]:
        where = "in a block differs from alone"
    elif apart[1]:
        where = "the first read in a block differs from alone"
    else:
        where = "the second read in a block differs from the first"
    _line(label, "different", f"{where}; {detail}")
    if check.show:
        _print_differences(differing)


async def _shared_read(
    check: _Check, read: Read, previous: Read | None, totals: Totals
) -> bool:
    """Run a read alone and twice in a block, print its line; True when it worked."""
    before = check.logins.count
    alone = await check.attempt(read)
    after_alone = check.logins.count
    async with check.client.shared_session(max_seconds=BLOCK_SECONDS):
        before_it = await check.attempt(previous or FIRST)
        first = await check.attempt(read)
        second = await check.attempt(read)
    if stages := _stages(alone, first, second):
        totals.failed += 1
        _line(read.label, "error", "; ".join(stages))
        return False
    in_block = check.logins.count - after_alone
    detail = (
        f"alone {alone.seconds:.2f} s, in block {first.seconds:.2f} s / "
        f"{second.seconds:.2f} s, logins {after_alone - before} alone, "
        f"{in_block} in block"
    )
    if before_it.error is not None or in_block != 1:
        totals.split += 1
        why = (
            "the read before it failed in the block"
            if before_it.error is not None
            else "the block logged in again"
        )
        _line(read.label, "split", f"not run in one session: {why}; {detail}")
        return True
    totals.alone_seconds += alone.seconds
    totals.block_seconds += first.seconds
    _compared(check, read.label, [alone, first, second], detail, totals)
    return True


async def _shared_session(client: ItmClient, args: argparse.Namespace) -> None:
    """Run each read alone and in a shared session; exit 1 on a difference."""
    _validate(args)
    check = _Check(client, args)
    _out("itm check shared-session: read only")
    _out(
        "per read: alone, in a block after the read before it, in the block again; "
        "values and error messages are left out unless --show"
    )
    _out(
        "ignored when they differ (they change by themselves): "
        + ", ".join(sorted({f"{n} ({why})" for n, why in VOLATILE.items()}))
    )
    started = time.perf_counter()
    previous: Read | None = None
    totals = Totals()
    for item in check.items():
        reads = await check.expand(item)
        if isinstance(reads, str):
            totals.failed += 1
            _line(item.name, "error", reads)
            continue
        if not reads:
            _line(item.name, "none found")
        for read in reads:
            if await _shared_read(check, read, previous, totals):
                previous = read
    count = totals.same + totals.different + totals.failed + totals.split
    _out(
        f"{count} reads: {totals.same} same, {totals.different} different, "
        f"{totals.failed} failed, {totals.split} not in one session; alone "
        f"{totals.alone_seconds:.1f} s, in block {totals.block_seconds:.1f} s "
        f"(of {time.perf_counter() - started:.1f} s in all); "
        f"logins used: {check.logins.count}"
    )
    if totals.different or totals.failed or totals.split:
        sys.exit(1)


async def _check(client: ItmClient, args: argparse.Namespace) -> None:
    """Run the check named on the command line."""
    if args.check_command == "reads":
        await _reads(client, args)
    else:
        await _shared_session(client, args)


def add_check_commands(sub: Any) -> None:
    """Add `check shared-session` and `check reads`."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--only",
        action="append",
        default=[],
        metavar="NAME",
        help="only this read (repeatable); --list shows the names",
    )
    common.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="NAME",
        help="leave this read out (repeatable)",
    )
    common.add_argument(
        "--list",
        action="store_true",
        help="print the names of the reads and stop, without contacting the iTM",
    )
    common.add_argument(
        "--show",
        action="store_true",
        help="print values and error messages (names of zones and units, "
        "settings): not for sharing",
    )
    check = sub.add_parser(
        "check", help="read-only checks of what the library reads from an iTM"
    )
    which = check.add_subparsers(dest="check_command", required=True)
    which.add_parser(
        "shared-session",
        parents=[common],
        help="read everything alone and in a shared session; say what differs "
        "(read only; exits 1 on a difference, a failed read or a read not run "
        "in one session)",
    )
    which.add_parser(
        "reads",
        parents=[common],
        help="read everything once, each in its own session; say what the account "
        "may read (read only; exits 0 unless the login fails)",
    )


CHECK_COMMANDS: dict[str, Callable[[ItmClient, argparse.Namespace], Awaitable[None]]]
CHECK_COMMANDS = {"check": _check}
