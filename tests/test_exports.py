# pylint: disable=protected-access,redefined-outer-name
"""PPD and the file exports (history CSV, energy navigator data) against the fake."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import ItmCommandError, ReadOnlyError, exports
from daikin_itm.exports import (
    DOWNLOAD_FAILED,
    DOWNLOAD_SUCCEEDED,
    backup,
    export_energy_data,
    export_history,
    export_path,
    export_point_list,
    export_setup,
    read_ppd,
    read_ppd_advanced,
    set_ppd_weekday,
)

from .fake_exports import FakeExportsITM
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@pytest.fixture
def itm() -> FakeExportsITM:
    """Return a simulated iTM with PPD and exports."""
    return FakeExportsITM()


async def run(
    itm: FakeExportsITM,
    flow: Callable[..., Awaitable[Any]],
    *args: Any,
    client: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """Run one flow in a fresh session, like the client's wrappers will."""
    c = FakeClient(itm, **(client or {}))
    c.progress_interval = 0
    async with c._session() as s:
        try:
            return await flow(s, *args, **kwargs)
        finally:
            await s.close_menu()


def fetcher(itm: FakeExportsITM) -> Callable[[str], Awaitable[bytes]]:
    """Return a fetch that GETs from the fake's export directory."""

    async def fetch(path: str) -> bytes:
        return itm.files[path]

    return fetch


def test_export_path() -> None:
    """A "#name" URI is a file in the export directory; others are paths."""
    assert export_path("#h.csv") == "/sdlfs/WebExport/h.csv"
    assert export_path("/other/x.csv") == "/other/x.csv"


async def test_read_ppd_for_a_period(itm: FakeExportsITM) -> None:
    """The period's dates through the date inputs, then Execute behind PROGRESS."""
    ppd = await run(itm, read_ppd, start=date(2024, 12, 2), end=date(2024, 12, 20))
    assert itm.dates[2900, "start"] == (2024, 11, 2)
    assert itm.dates[2900, "end"] == (2024, 11, 20)
    assert itm.ppd_calculated == [("period", (2024, 11, 2))]
    assert [u.name for u in ppd.units] == ["AC1", "AC2"]
    assert ppd.units[0].consumption_kwh == 12.5
    assert ppd.units[1].consumption_kwh is None
    assert 2900 not in itm.open_screens


async def test_read_ppd_by_month_and_refusals(itm: FakeExportsITM) -> None:
    """The monthly calculation day; a date outside the range; both kinds at once."""
    await run(itm, read_ppd, calculation_day=15)
    assert itm.ppd_day == 15
    assert itm.ppd_calculated[-1] == ("month", 15)
    with pytest.raises(ItmCommandError, match="outside the iTM's range"):
        await run(itm, read_ppd, start=date(2020, 1, 1))
    with pytest.raises(ItmCommandError, match="not both"):
        await run(itm, read_ppd, start=date(2026, 1, 1), calculation_day=3)
    assert await run(itm, read_ppd)  # the shown period
    assert 2900 not in itm.open_screens


async def test_ppd_advanced(itm: FakeExportsITM) -> None:
    """Read both tabs; change a weekday with OK + Yes; a dry run plans."""
    advanced = await run(itm, read_ppd_advanced)
    assert len(advanced.weekdays) == 7
    assert advanced.weekdays[1].enabled is False
    assert advanced.weekdays[1].option == "Operation"
    assert advanced.weekdays[1].time == "08:00"
    assert advanced.exclusions == (("01/01", "Holiday"),)
    assert await run(itm, set_ppd_weekday, 1, enabled=True, option="Standby") is True
    assert itm.weekdays[1] == {"on": True, "option": 1, "time": "08:00"}
    assert (
        await run(itm, set_ppd_weekday, 2, enabled=True, client={"dry_run": True})
        is False
    )
    assert itm.weekdays[2]["on"] is False
    with pytest.raises(ItmCommandError, match="not offered"):
        await run(itm, set_ppd_weekday, 3, option="Weekend")
    with pytest.raises(ItmCommandError, match="0 \\(Sunday\\)"):
        await run(itm, set_ppd_weekday, 7, enabled=True)
    assert itm.open_screens.isdisjoint({2900, 2901})


async def test_history_export(itm: FakeExportsITM) -> None:
    """800 -> CSV -> 801 dates -> Output; PROGRESS, DLG_DOWNLOAD, GET, FINISH."""
    result = await run(
        itm, export_history, date(2024, 12, 20), date(2025, 1, 1), fetcher(itm)
    )
    assert result.name == "history_0001.csv"
    assert result.data == b"history;data\n"
    assert itm.dates[801, "start"] == (2024, 11, 20)
    assert itm.finished == [(1, DOWNLOAD_SUCCEEDED)]
    assert itm.open_screens.isdisjoint({800, 801})


async def test_energy_exports(itm: FakeExportsITM) -> None:
    """The energy data of a listed period and the point list of a date range."""
    data = await run(itm, export_energy_data, "01/2024-12/2024", fetcher(itm))
    assert data.data == b"energy;data\n"
    assert itm.period == 1
    points = await run(
        itm, export_point_list, date(2024, 11, 20), date(2024, 12, 19), fetcher(itm)
    )
    assert points.name.startswith("points_")
    assert itm.dates[3219, "end"] == (2024, 11, 19)
    assert [reason for _, reason in itm.finished] == [DOWNLOAD_SUCCEEDED] * 2
    with pytest.raises(ItmCommandError, match="not offered"):
        await run(itm, export_energy_data, "1999", fetcher(itm))
    assert 3219 not in itm.open_screens


async def test_failed_downloads(itm: FakeExportsITM) -> None:
    """A fetch that fails is reported as failed; a job without a file is cancelled."""

    async def broken(_path: str) -> bytes:
        msg = "connection reset"
        raise OSError(msg)

    with pytest.raises(ItmCommandError, match="could not fetch"):
        await run(itm, export_energy_data, "01/2025-01/2025", broken)
    assert itm.finished == [(1, DOWNLOAD_FAILED)]
    itm.no_file = True
    with pytest.raises(ItmCommandError, match="produced no file"):
        await run(itm, export_energy_data, "01/2025-01/2025", fetcher(itm))
    assert 3219 not in itm.open_screens


async def test_slow_and_stuck_jobs(
    itm: FakeExportsITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A slow job is waited for by time, not a number of polls; a stuck one ends."""
    itm.slow_polls = 150
    result = await run(itm, export_energy_data, "01/2025-01/2025", fetcher(itm))
    assert result.data
    defaults = exports.download.__kwdefaults__
    assert defaults is not None
    monkeypatch.setitem(defaults, "wait", 0.0)
    with pytest.raises(ItmCommandError, match="did not finish"):
        await run(itm, export_energy_data, "01/2025-01/2025", fetcher(itm))
    assert 3219 not in itm.open_screens


async def test_refused_before_sending(itm: FakeExportsITM) -> None:
    """Disabled inputs and buttons are refused; a button that starts no job too."""
    itm.disabled_parts = {(3219, 17), (2900, 15)}
    with pytest.raises(ItmCommandError, match="input disabled"):
        await run(
            itm, export_point_list, date(2024, 11, 20), date(2024, 11, 21), fetcher(itm)
        )
    with pytest.raises(ItmCommandError, match="does not offer"):
        await run(itm, read_ppd)
    itm.disabled_parts = set()
    itm.no_job = True
    with pytest.raises(ItmCommandError, match="started no job"):
        await run(itm, export_energy_data, "01/2025-01/2025", fetcher(itm))
    assert itm.open_screens.isdisjoint({2900, 3219})


async def test_client_ppd_and_exports(itm: FakeExportsITM) -> None:
    """PPD and the exports through ItmClient, the file fetched with its GET."""
    c = FakeClient(itm)

    async def get(path: str) -> bytes:
        return itm.files[path]

    c._get_bytes = get  # type: ignore[method-assign]  # pylint: disable=protected-access
    ppd = await c.fetch_ppd(start=date(2024, 11, 20), end=date(2024, 12, 19))
    assert ppd.units
    advanced = await c.fetch_ppd_advanced()
    assert len(advanced.weekdays) == 7
    download = await c.export_history(date(2024, 12, 20), date(2025, 1, 1))
    assert download.data.startswith(b"history")
    assert download.name.endswith(".csv")
    dry = FakeClient(itm, dry_run=True)
    assert await dry.set_ppd_weekday(1, enabled=False) is None


async def test_setup_export_and_backup(itm: FakeExportsITM) -> None:
    """Both start from a menu button: confirm, progress, then the download."""
    setup = await run(itm, export_setup, fetcher(itm))
    assert setup.name.startswith("setup_")
    assert setup.data.startswith(b"setup")
    saved = await run(itm, backup, fetcher(itm))
    assert saved is not None
    assert saved.name.startswith("backup_")
    assert {4000, 4100}.isdisjoint(itm.open_screens)

    itm.backup_on_device = True
    assert await run(itm, backup, fetcher(itm)) is None  # kept on the iTM
    assert 4100 not in itm.open_screens


async def test_backup_is_a_write(itm: FakeExportsITM) -> None:
    """A read-only client refuses the backup; a dry run plans it; no job starts."""
    client = FakeClient(itm, read_only=True)
    with pytest.raises(ReadOnlyError):
        await client.run_backup()
    client = FakeClient(itm, dry_run=True)
    assert await client.run_backup() is None
    assert any("backup" in p for p in client.planned)
    assert itm.job == 0

    async def get(path: str) -> bytes:
        return itm.files[path]

    client = FakeClient(itm)
    client._get_bytes = get  # type: ignore[method-assign]
    assert (await client.export_setup()).data.startswith(b"setup")
    result = await client.run_backup()
    assert result is not None
    assert result.data.startswith(b"backup")
