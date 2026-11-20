# pylint: disable=protected-access
"""_Session.step_back: undoing a reader's steps to another day or page."""

from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import ItmCommandError
from daikin_itm.menu import H_PAGE_NEWER, MenuScreen

from .fake_itm import FakeITM
from .test_client import FakeClient

if TYPE_CHECKING:
    from daikin_itm.session import _Session

WHY = "stayed on another page"


class Pager:
    """A screen showing a page, with a button back to the page before."""

    def __init__(self, page: int, *, step: int = 1, fail_at: int | None = None) -> None:
        """Start on a page; a press goes `step` pages back, or raises at `fail_at`."""
        self.page = page
        self.step = step
        self.fail_at = fail_at
        self.presses = 0

    async def press(self) -> None:
        """Press the way back once."""
        self.presses += 1
        if self.presses == self.fail_at:
            msg = "the button did not answer"
            raise ItmCommandError(msg)
        self.page -= self.step

    def shown(self) -> int:
        """Return the page shown."""
        return self.page


def _session(*, shared: bool) -> _Session:
    s = FakeClient(FakeITM())._new_session()
    s.shared = shared
    return s


async def test_not_shared_presses_nothing() -> None:
    """Outside a shared session the session ends without a press."""
    s = _session(shared=False)
    pager = Pager(3)
    await s.step_back(2, pager.press, pager.shown, 1, WHY)
    assert pager.presses == 0
    assert s._stuck == WHY


async def test_back_at_the_start_keeps_the_session() -> None:
    """Pressing back as often as stepped away, onto the first page: it stays."""
    s = _session(shared=True)
    pager = Pager(3)
    await s.step_back(2, pager.press, pager.shown, 1, WHY)
    assert pager.presses == 2
    assert pager.page == 1
    assert s._stuck is None


async def test_another_page_shown_ends_the_session() -> None:
    """A way back that lands elsewhere ends the session."""
    s = _session(shared=True)
    pager = Pager(3, step=0)
    await s.step_back(2, pager.press, pager.shown, 1, WHY)
    assert pager.presses == 2
    assert s._stuck == WHY


async def test_unknown_start_ends_the_session() -> None:
    """Without knowing what was shown first, the way back can't be checked."""
    s = _session(shared=True)
    pager = Pager(3)
    await s.step_back(2, pager.press, pager.shown, None, WHY)
    assert pager.presses == 2
    assert s._stuck == WHY


async def test_failing_press_ends_the_session_quietly(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An ItmError on the way back is logged, not raised, and stops the presses."""
    s = _session(shared=True)
    pager = Pager(3, fail_at=1)
    with caplog.at_level("DEBUG", logger="daikin_itm"):
        await s.step_back(2, pager.press, pager.shown, 1, WHY)
    assert pager.presses == 1
    assert s._stuck == WHY
    assert "the button did not answer" in caplog.text


@pytest.mark.parametrize("shared", [True, False])
async def test_no_steps_does_nothing(*, shared: bool) -> None:
    """A reader that stayed where it was presses nothing and keeps the session."""
    s = _session(shared=shared)
    pager = Pager(1)
    await s.step_back(0, pager.press, pager.shown, None, WHY)
    assert pager.presses == 0
    assert s._stuck is None


class StuckOverviewITM(FakeITM):
    """A simulated iTM whose day overview's Previous button changes nothing."""

    def _schedule_event(self, ev: dict[str, Any]) -> list[Any]:
        if ev["screen"] == MenuScreen.SCHEDULE_DAY and ev["part"] == 1:
            return [("UPDATE", MenuScreen.SCHEDULE_DAY, [], [])]
        return super()._schedule_event(ev)


async def test_day_overview_stepping_back_keeps_the_session() -> None:
    """Days ahead in the overview, then back to today: one login for the block."""
    itm = FakeITM()
    c = FakeClient(itm)
    alone = await c.fetch_schedule_day("Always", days_ahead=2)
    logins = itm.logins
    async with c.shared_session():
        assert await c.fetch_schedule_day("Always", days_ahead=2) == alone
        assert await c.fetch_schedule_day("Always") != alone
        assert len(await c.fetch_units()) == 3
    assert itm.logins - logins == 1


async def test_day_overview_that_does_not_go_back_ends_the_session() -> None:
    """When Previous doesn't bring today back, the next read logs in again."""
    itm = StuckOverviewITM()
    c = FakeClient(itm)
    alone = await c.fetch_schedule_day("Always", days_ahead=1)
    logins = itm.logins
    async with c.shared_session():
        assert await c.fetch_schedule_day("Always", days_ahead=1) == alone
        assert itm.session is None
        assert len(await c.fetch_units()) == 3
    assert itm.logins - logins == 2


async def test_history_of_another_day_ends_the_session() -> None:
    """The date dialog can't be undone: a day and pages still end the session."""
    itm = FakeITM()
    itm.history_page_size = 1
    c = FakeClient(itm)
    async with c.shared_session():
        pages = await c.fetch_history(day=date(2024, 12, 31), pages=2)
        assert pages
        assert itm.session is None
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 2


async def test_history_pages_keep_the_session() -> None:
    """Older pages, then back to the newest: one login for the block."""
    itm = FakeITM()
    itm.history_page_size = 1
    c = FakeClient(itm)
    async with c.shared_session():
        pages = await c.fetch_history(pages=3)
        assert len(pages) > 1
        assert itm.session is not None
        assert await c.fetch_history() == pages[:1]
        assert len(await c.fetch_units()) == 3
    assert itm.logins == 1


class StuckHistoryITM(FakeITM):
    """A simulated iTM whose history's Newer button changes nothing."""

    def _history_event(self, ev: dict[str, Any]) -> list[Any]:
        page = self.history_page
        updates = super()._history_event(ev)
        if ev["part"] == H_PAGE_NEWER:
            self.history_page = page
        return updates


async def test_history_that_does_not_go_back_ends_the_session() -> None:
    """When Newer doesn't bring the newest page back, the next read logs in again."""
    itm = StuckHistoryITM()
    itm.history_page_size = 1
    c = FakeClient(itm)
    alone = await c.fetch_history()
    logins = itm.logins
    async with c.shared_session():
        assert len(await c.fetch_history(pages=2)) == 2
        assert itm.session is None
        assert await c.fetch_history() == alone
    assert itm.logins - logins == 2
