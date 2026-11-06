# ruff: noqa: SLF001
# pylint: disable=protected-access
"""PPD (power proportional distribution) and the iTM's file exports.

PPD: the PPD screen is a calculation, not a settings screen. It has no OK: the period
(or the monthly calculation day) is the input of "Execute", whose result is the use
per unit. Its advanced screen (which hours count as standby per weekday, exclusion
periods) does save, with OK.

Exports (the history as a ZIP archive, the energy navigator's data and its point
list): the output button starts a job behind a PROGRESS dialog. The browser asks for
progress (UPD_PROG) until the iTM answers with a DLG_DOWNLOAD update; its URI names
the file (its full path, e.g. ``/sdlfs/WebExport/History.zip``), which the browser
then GETs and saves. Afterwards it tells the iTM how the download ended
(FINISH_DOWNLOAD [job, reason]).

That GET carries no session token in the web UI: while the file exists, anyone who
can reach the iTM and knows its name can probably fetch it. These functions fetch it
at once and report the download finished, so the iTM can drop it.

Every function takes an open ``_Session``; the client's wrappers own the session.
A download needs a ``fetch`` callable that GETs a path on the iTM and returns the
body (the client provides one from its HTTP session).
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ._bases import ClientCore
from ._screens import click as _click
from ._screens import close as _close
from ._util import ignore as _ignore
from .exceptions import ItmCommandError
from .menu import MenuScreen
from .protocol import DataType, EventType
from .system import MB_ENERGY_EXPORT, MB_PPD, PpdSettings, ppd_from_screen

if TYPE_CHECKING:
    from datetime import date

    from .session import _Session
    from .state import UIState


# GETs a path on the iTM (e.g. "/sdlfs/WebExport/x.csv") and returns its body.
Fetch = Callable[[str], Awaitable[bytes]]

EXPORT_DIR = "/sdlfs/WebExport/"
EXPORT_NAME = re.compile(r"[A-Za-z0-9_\-][A-Za-z0-9_.\- ]{0,127}")
DOWNLOAD_SUCCEEDED, DOWNLOAD_CANCELLED, DOWNLOAD_FAILED = 0, 1, 2

# PPD (2900) and its advanced settings (2901).
PPD_SCREEN, PPD_ADVANCED = 2900, 2901
PD_PERIOD_RADIO, PD_MONTH_RADIO = 2, 10
PD_START_BUTTON, PD_END_BUTTON, PD_DAY_BUTTON = 6, 9, 14
PD_EXECUTE, PD_ADVANCED, PD_CLOSE = 15, 17, 22
PA_WEEKDAY_CHECKS = (2, 7, 12, 17, 22, 27, 32)  # Sunday .. Saturday
PA_EXCLUSIONS, PA_TAB, PA_OK, PA_CANCEL = 53, 1, 37, 38
# History (800): CSV opens the output screen (801).
MB_HISTORY = 53
HISTORY, HS_CSV, HS_CLOSE = 800, 13, 17
HISTORY_OUTPUT, HO_START, HO_END, HO_OUTPUT, HO_CANCEL = 801, 3, 19, 6, 7
# Energy navigator data output (3219): tab 0 energy data, tab 1 point list.
EX_TAB, EX_PERIOD, EX_SAVE_ENERGY = 1, 3, 4
EX_FROM_BUTTON, EX_TO_BUTTON, EX_SAVE_POINTS, EX_CLOSE = 17, 20, 21, 5


@dataclass(frozen=True)
class Download:
    """A file the iTM exported."""

    name: str  # as the iTM named it
    data: bytes


@dataclass(frozen=True)
class PpdWeekday:
    """A weekday's line of the PPD advanced settings, as shown."""

    enabled: bool | None
    option: str | None  # the combo's shown option
    time: str | None


@dataclass(frozen=True)
class PpdAdvanced:
    """The PPD advanced settings: per weekday (Sunday first) and exclusions."""

    weekdays: tuple[PpdWeekday, ...]
    exclusions: tuple[tuple[str, ...], ...]  # the exclusion list's rows, as shown


def export_path(uri: str) -> str:
    """Return the path to GET for a DLG_DOWNLOAD URI.

    The iTM names the file by its full path ("/sdlfs/WebExport/History.zip"); a bare
    "#name", as the browser script also handles, is a file in the export directory.
    The URI comes from a reply, so only a plain file name in that directory is
    accepted: never another host, directory or a hidden file.

    Raises
    ------
        ItmCommandError: The URI names anything else.

    """
    path = EXPORT_DIR + uri[1:] if uri.startswith("#") else uri
    name = path.removeprefix(EXPORT_DIR)
    if (
        not path.startswith(EXPORT_DIR)
        or EXPORT_NAME.fullmatch(name) is None
        or ".." in name
    ):
        msg = f"export: unexpected file location from the iTM: {uri!r}"
        raise ItmCommandError(msg)
    return path


def ppd_advanced_from_screen(state: UIState) -> PpdAdvanced:
    """Read the PPD advanced screen (2901)."""
    s = PPD_ADVANCED
    weekdays = []
    for check in PA_WEEKDAY_CHECKS:
        options = state.get(s, check + 1, "COMBO_STRS") or []
        idx = state.get(s, check + 1, "SINGLE_SELIDX")
        tick = state.get(s, check, "CHECK_STATE")
        text = state.get(s, check + 2, "TEXT")
        weekdays.append(
            PpdWeekday(
                enabled=None if tick is None else tick == 1,
                option=str(options[idx])
                if idx is not None and idx < len(options)
                else None,
                time=str(text).strip() or None if text is not None else None,
            )
        )
    rows = (state.get(s, PA_EXCLUSIONS, "LIST_ROW") or {}).get("rows", [])
    exclusions = tuple(tuple(str(t) for t in r.get("text", [])) for r in rows)
    return PpdAdvanced(weekdays=tuple(weekdays), exclusions=exclusions)


# -- inputs ---------------------------------------------------------------------------


async def answer_date(
    s: _Session, screen: int, button: int, value: date, what: str
) -> None:
    """Press a date's "change" button and enter the date in the DATE dialog.

    The dialog says the allowed range ([year, month 0-11, day] from and to); a date
    outside it is refused before it is sent, and the dialog cancelled.
    """
    if s.state.get(screen, button, "ENABLED") == 0:
        msg = f"{what}: the iTM has this input disabled"
        raise ItmCommandError(msg)
    reply = await s.send(screen, button)
    dialogs = [
        u for u in reply.screens if u.update == "DLG_OPEN" and u.data[1] == "DATE"
    ]
    if not dialogs:
        msg = f"{what}: no date input opened: {reply.dialogs()}"
        raise ItmCommandError(msg)
    d = dialogs[0]
    wanted = (value.year, value.month - 1, value.day)
    low, high = tuple(d.data[7:10]), tuple(d.data[10:13])
    if not low <= wanted <= high:
        await s.send(d.screen, d.data[0], EventType.CLICK2)
        msg = f"{what}: {value} outside the iTM's range"
        raise ItmCommandError(msg)
    await s.send(d.screen, d.data[0], EventType.CLICK, DataType.DATE, 3, array=wanted)


# -- downloads ------------------------------------------------------------------------


async def _poll_for_download(s: _Session, *, attempts: int) -> str | None:
    """Poll until a reply carries the download dialog; return its URI."""
    for _ in range(attempts):
        reply = await s.poll()
        found = [u for u in reply.screens if u.update == "DLG_DOWNLOAD"]
        if found:
            return str(found[0].data[2])
        await asyncio.sleep(s._client.progress_interval)
    return None


async def download(  # pylint: disable=too-many-locals
    s: _Session,
    screen: int,
    button: int,
    fetch: Fetch,
    *,
    wait: float = 300.0,
) -> Download:
    """Press an output button, wait for the file, fetch it and report it done.

    The job can take minutes: a day of history sat at 0% for half a minute before
    it moved. The iTM is asked for progress until ``wait`` seconds have passed.

    Raises
    ------
        ItmCommandError: The iTM started no job, didn't finish it in time, or the
            file could not be fetched.

    """
    reply = await _click(s, screen, button, "export")
    # The history output asks first ("history output, is this correct?"): Yes.
    confirms = [
        u for u in reply.screens if u.update == "DLG_OPEN" and u.data[1] == "CONFIRM"
    ]
    if confirms:
        reply = await s.send(confirms[0].screen, confirms[0].data[0])
    progress = [
        u for u in reply.screens if u.update == "DLG_OPEN" and u.data[1] == "PROGRESS"
    ]
    if not progress:
        msg = f"export: the iTM started no job: {reply.dialogs()}"
        raise ItmCommandError(msg)
    dlg_screen, dlg_id, job = (
        progress[0].screen,
        progress[0].data[0],
        progress[0].data[3],
    )
    uri = None
    finished = False  # the progress dialog closed: the job ended by itself
    deadline = asyncio.get_running_loop().time() + wait
    while True:
        found = [u for u in reply.screens if u.update == "DLG_DOWNLOAD"]
        if found:
            uri = str(found[0].data[2])
            break
        if any(u.update == "DLG_CLOSE" and u.data[0] == dlg_id for u in reply.screens):
            # The progress dialog closed: like the history list, the result (the
            # download) comes with the next POLLING replies.
            uri = await _poll_for_download(s, attempts=5)
            finished = True
            break
        if asyncio.get_running_loop().time() >= deadline:
            break
        await asyncio.sleep(s._client.progress_interval)
        reply = await s.send(dlg_screen, dlg_id, EventType.UPD_PROG, DataType.INT, job)
    if uri is None:
        if not finished:
            await s.send(dlg_screen, dlg_id, EventType.CANCEL_PROG, DataType.INT, job)
            msg = f"export: the iTM did not finish within {wait:g} s (cancelled)"
            raise ItmCommandError(msg)
        msg = "export: the iTM produced no file"
        raise ItmCommandError(msg)
    reason = DOWNLOAD_FAILED
    try:
        path = export_path(uri)
        data = await fetch(path)
        reason = DOWNLOAD_SUCCEEDED
    except ItmCommandError:
        raise
    except Exception as err:
        msg = f"export: could not fetch {path}: {err}"
        raise ItmCommandError(msg) from err
    finally:
        await s.send(
            dlg_screen,
            dlg_id,
            EventType.FINISH_DOWNLOAD,
            DataType.INTARRAY,
            2,
            array=(job, reason),
        )
    return Download(name=path.removeprefix(EXPORT_DIR), data=data)


# -- PPD ------------------------------------------------------------------------------


async def read_ppd(
    s: _Session,
    *,
    start: date | None = None,
    end: date | None = None,
    calculation_day: int | None = None,
) -> PpdSettings:
    """Calculate PPD for a period (start/end) or by month (calculation day), read it.

    Without arguments, the period the iTM shows is calculated. The use per unit needs
    the PPD activation key on the iTM; without it the result is empty.
    """
    if calculation_day is not None and (start is not None or end is not None):
        msg = "PPD: give a period (start/end) or a calculation day, not both"
        raise ItmCommandError(msg)
    await s._open_system_screen(MB_PPD, MenuScreen.PPD)
    try:
        if start is not None or end is not None:
            await s.send(PPD_SCREEN, PD_PERIOD_RADIO)
            if start is not None:
                await answer_date(s, PPD_SCREEN, PD_START_BUTTON, start, "PPD start")
            if end is not None:
                await answer_date(s, PPD_SCREEN, PD_END_BUTTON, end, "PPD end")
        if calculation_day is not None:
            await s.send(PPD_SCREEN, PD_MONTH_RADIO)
            await s.answer_number(
                PD_DAY_BUTTON, calculation_day, "PPD calculation day", screen=PPD_SCREEN
            )
        reply = await _click(s, PPD_SCREEN, PD_EXECUTE, "PPD calculation")
        await s.wait_for_progress(reply)
        return ppd_from_screen(s.state)
    finally:
        await _close(s, PPD_SCREEN, PD_CLOSE)


async def _open_ppd_advanced(s: _Session) -> None:
    """Open the PPD advanced screen (the iTM first warns about exclusions)."""
    await s._open_system_screen(MB_PPD, MenuScreen.PPD)
    reply = await _click(s, PPD_SCREEN, PD_ADVANCED, "PPD advanced")
    # The warning ("exclusion periods are left out of the calculation; continue?")
    # only leads on to the screen; nothing is saved by answering it.
    await s._answer_confirm(PPD_SCREEN, reply)
    if PPD_ADVANCED not in s.state.screens:
        msg = "the PPD advanced settings did not open"
        raise ItmCommandError(msg)


async def read_ppd_advanced(s: _Session) -> PpdAdvanced:
    """Read the PPD advanced settings (both tabs), then cancel."""
    try:
        await _open_ppd_advanced(s)
        await s.send(PPD_ADVANCED, PA_TAB, EventType.TAB_SELECT, DataType.INT, 1)
        await s.send(PPD_ADVANCED, PA_TAB, EventType.TAB_SELECT, DataType.INT, 0)
        return ppd_advanced_from_screen(s.state)
    finally:
        await _close(s, PPD_ADVANCED, PA_CANCEL)
        await _close(s, PPD_SCREEN, PD_CLOSE)


async def set_ppd_weekday(
    s: _Session, weekday: int, *, enabled: bool | None = None, option: str | None = None
) -> bool:
    """Change a weekday's line of the PPD advanced settings, then OK.

    Returns
    -------
        True when saved, False in a dry run.

    """
    if not 0 <= weekday <= 6:
        msg = "a weekday is 0 (Sunday) .. 6 (Saturday)"
        raise ItmCommandError(msg)
    check = PA_WEEKDAY_CHECKS[weekday]
    try:
        await _open_ppd_advanced(s)
        if enabled is not None:
            await s.send(
                PPD_ADVANCED, check, EventType.CLICK, DataType.INT, int(enabled)
            )
        if option is not None:
            options = [
                str(o).strip()
                for o in s.state.get(PPD_ADVANCED, check + 1, "COMBO_STRS") or []
            ]
            if option.strip() not in options:
                msg = f"PPD weekday {weekday}: {option!r} not offered: {options}"
                raise ItmCommandError(msg)
            await s.send(
                PPD_ADVANCED,
                check + 1,
                EventType.CLICK,
                DataType.INT,
                options.index(option.strip()),
            )
        return await s.press_ok(
            f"save PPD advanced settings (weekday {weekday})",
            PPD_ADVANCED,
            PA_OK,
            PA_CANCEL,
        )
    finally:
        await _close(s, PPD_ADVANCED, PA_CANCEL)
        await _close(s, PPD_SCREEN, PD_CLOSE)


# -- history and energy navigator exports ---------------------------------------------


async def export_history(s: _Session, start: date, end: date, fetch: Fetch) -> Download:
    """Export the history of a period and fetch it.

    The iTM serves a ZIP archive: Daikin's Windows CSV converter, the raw history
    (tab-separated text), the converter's settings and the iTM's setup data,
    including an authentication file. Treat the archive as sensitive.
    """
    await s.open_menu()
    reply = await s.send(MenuScreen.MENULIST, MB_HISTORY)
    await s.wait_for_progress(reply)
    if HISTORY not in s.state.screens:
        msg = "the history did not open"
        raise ItmCommandError(msg)
    try:
        await _click(s, HISTORY, HS_CSV, "history CSV")
        if HISTORY_OUTPUT not in s.state.screens:
            msg = "the history output screen did not open"
            raise ItmCommandError(msg)
        await answer_date(s, HISTORY_OUTPUT, HO_START, start, "history start")
        await answer_date(s, HISTORY_OUTPUT, HO_END, end, "history end")
        return await download(s, HISTORY_OUTPUT, HO_OUTPUT, fetch)
    finally:
        await _close(s, HISTORY_OUTPUT, HO_CANCEL)
        await _close(s, HISTORY, HS_CLOSE)


async def export_energy_data(s: _Session, period: str, fetch: Fetch) -> Download:
    """Export the energy navigator's data of a period (as its combo lists it)."""
    await s._open_system_screen(MB_ENERGY_EXPORT, MenuScreen.ENERGY_EXPORT)
    x = MenuScreen.ENERGY_EXPORT
    try:
        options = [
            str(o).strip() for o in s.state.get(x, EX_PERIOD, "COMBO_STRS") or []
        ]
        if period.strip() not in options:
            msg = f"energy data: period {period!r} not offered: {options}"
            raise ItmCommandError(msg)
        await s.send(x, EX_PERIOD, EventType.CLICK, DataType.INT, options.index(period))
        return await download(s, x, EX_SAVE_ENERGY, fetch)
    finally:
        await _close(s, x, EX_CLOSE)


async def export_point_list(
    s: _Session, start: date, end: date, fetch: Fetch
) -> Download:
    """Export the energy navigator's management point list of a period."""
    await s._open_system_screen(MB_ENERGY_EXPORT, MenuScreen.ENERGY_EXPORT)
    x = MenuScreen.ENERGY_EXPORT
    try:
        await s.send(x, EX_TAB, EventType.TAB_SELECT, DataType.INT, 1)
        await answer_date(s, x, EX_FROM_BUTTON, start, "point list from")
        await answer_date(s, x, EX_TO_BUTTON, end, "point list to")
        return await download(s, x, EX_SAVE_POINTS, fetch)
    finally:
        await _close(s, x, EX_CLOSE)


# -- backup and setup export ----------------------------------------------------------
# Menu list buttons that start a job at once (no screen of their own: 4000 and 4100
# have no parts; the iTM drives them with a CONFIRM, a PROGRESS and the download).
MB_BACKUP, MB_SETUP_EXPORT = 47, 59
BACKUP, SETUP_EXPORT = 4100, 4000


async def export_setup(s: _Session, fetch: Fetch) -> Download:
    """Export the iTM's setup (its configuration) and fetch the file.

    Not seen on a real iTM: the flow follows the web UI's code, where the screen is
    an empty shell and the job runs like the history export (confirm, progress,
    download). The file holds the installation's configuration: treat it as
    sensitive.
    """
    await s.open_menu()
    try:
        return await download(s, MenuScreen.MENULIST, MB_SETUP_EXPORT, fetch)
    finally:
        await s.close_menu()


async def backup(s: _Session, fetch: Fetch) -> Download | None:
    """Run the iTM's backup; fetch the file when the iTM offers one.

    Returns
    -------
        The backup file, or None when the iTM keeps the backup itself (its progress
        dialog closes without a download). Not seen on a real iTM; see
        `export_setup`.

    """
    await s.open_menu()
    try:
        return await download(s, MenuScreen.MENULIST, MB_BACKUP, fetch)
    except ItmCommandError as err:
        if "produced no file" in str(err):
            return None
        raise
    finally:
        await s.close_menu()


class ExportsMixin(ClientCore):
    """ItmClient's PPD and export methods."""

    async def fetch_ppd(
        self,
        *,
        start: date | None = None,
        end: date | None = None,
        calculation_day: int | None = None,
    ) -> PpdSettings:
        """Calculate power proportional distribution for a period (or a monthly day).

        Without the iTM's PPD licence the result lists no units.

        Args:
        ----
            start: The period's first day.
            end: The period's last day.
            calculation_day: Or the day of the month a monthly period starts.

        Returns:
        -------
            The PPD settings with the use per unit.

        """
        return await self._in_session(
            lambda s: read_ppd(s, start=start, end=end, calculation_day=calculation_day)
        )

    async def fetch_ppd_advanced(self) -> PpdAdvanced:
        """Read the PPD advanced settings (per weekday, and exclusions).

        Returns
        -------
            The settings.

        """
        return await self._in_session(read_ppd_advanced)

    async def set_ppd_weekday(
        self, weekday: int, *, enabled: bool | None = None, option: str | None = None
    ) -> PpdAdvanced | None:
        """Change a weekday of the PPD advanced settings.

        Args:
        ----
            weekday: 0 = Sunday .. 6 = Saturday.
            enabled: Count the weekday (True) or not.
            option: One of the options the screen lists for it.

        Returns:
        -------
            The settings as read back, or None in a dry run.

        """
        self._experimental("change the PPD settings")
        await self._system_write(
            lambda s: _ignore(
                set_ppd_weekday(s, weekday, enabled=enabled, option=option)
            )
        )
        return None if self.dry_run else await self.fetch_ppd_advanced()

    async def export_history(self, start: date, end: date) -> Download:
        """Export the history of a period as the iTM's file (CSV).

        The iTM serves the file without a session token while it exists; the
        library fetches it at once and tells the iTM it is done.

        Args:
        ----
            start: The first day.
            end: The last day.

        Returns:
        -------
            The file's name and contents.

        """
        return await self._in_session(
            lambda s: export_history(s, start, end, self._get_bytes)
        )

    async def export_energy_data(self, period: str) -> Download:
        """Export the energy navigator's data for a period it lists.

        Args:
        ----
            period: As the export screen lists it, e.g. "01/2025-01/2025".

        Returns:
        -------
            The file's name and contents.

        """
        return await self._in_session(
            lambda s: export_energy_data(s, period, self._get_bytes)
        )

    async def export_point_list(self, start: date, end: date) -> Download:
        """Export the energy navigator's point list for a period.

        Args:
        ----
            start: The first day.
            end: The last day.

        Returns:
        -------
            The file's name and contents.

        """
        return await self._in_session(
            lambda s: export_point_list(s, start, end, self._get_bytes)
        )

    async def export_setup(self) -> Download:
        """Export the iTM's setup (its configuration) and fetch the file.

        The file holds the installation's configuration: treat it as sensitive.

        Returns
        -------
            The file's name and contents.

        """
        return await self._in_session(lambda s: export_setup(s, self._get_bytes))

    async def run_backup(self) -> Download | None:
        """Run the iTM's backup (a job on the device, so a write).

        Returns
        -------
            The backup file when the iTM offers it as a download, None when it keeps
            the backup itself or in a dry run.

        """
        self._experimental("run a backup")
        self._writable()
        if self.dry_run:
            self.planned.append(
                f"run the backup: screen {MenuScreen.MENULIST} part {MB_BACKUP}"
            )
            return None
        return await self._in_session(lambda s: backup(s, self._get_bytes))
