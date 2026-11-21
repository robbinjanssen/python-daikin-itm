# pylint: disable=protected-access,redefined-outer-name
"""Clients of one iTM and user in a process take turns on its single session."""

from __future__ import annotations

import asyncio
import contextlib
import logging

import pytest

from daikin_itm import ItmClient, LoginRejected, MemorySessionStore, SessionInUse
from daikin_itm.client import _USER_LOCKS, _lock_origin

from .fake_itm import FakeITM
from .test_client import FakeClient

HOST = "itm.example"


@pytest.fixture
def itm() -> FakeITM:
    """Return a simulated iTM."""
    return FakeITM()


async def test_second_client_waits_for_the_block(itm: FakeITM) -> None:
    """A second client's read waits for the block, which keeps its one login."""
    store = MemorySessionStore()
    a, b = FakeClient(itm, store=store), FakeClient(itm, store=store)
    async with a.shared_session():
        await a.fetch_units()
        other = asyncio.create_task(b.fetch_units())
        await asyncio.sleep(0.05)
        assert not other.done()
        assert len(await a.fetch_units()) == 3
        assert itm.logins == 1
        assert store.token == itm.session
    assert len(await other) == 3
    assert itm.logins == 2
    assert itm.session is None
    assert store.token is None


async def test_calls_of_two_clients_at_once(
    itm: FakeITM, caplog: pytest.LogCaptureFixture
) -> None:
    """Calls of two clients sharing a store take turns: none logs the other out."""
    store = MemorySessionStore()
    a, b = FakeClient(itm, store=store), FakeClient(itm, store=store)
    with caplog.at_level(logging.INFO, logger="daikin_itm.client"):
        results = await asyncio.gather(
            a.fetch_units(), b.fetch_units(), a.fetch_info(), b.list_zones()
        )
    assert len(results[0]) == len(results[1]) == 3
    assert results[3] == ["Building"]
    assert itm.logins == 4
    assert itm.session is None
    assert store.token is None
    assert not caplog.records


@pytest.mark.parametrize(
    ("first", "second"),
    [
        (HOST, f"http://{HOST}/"),
        (HOST, "ITM.example"),
        (HOST, f"{HOST}/"),
        (HOST, f"http://{HOST}:80"),
        (f"http://{HOST}", f"HTTP://{HOST}"),
        (f"https://{HOST}", f"https://{HOST}:443/"),
    ],
)
async def test_host_is_normalised(itm: FakeITM, first: str, second: str) -> None:
    """The same iTM written two ways is one lock."""
    a = FakeClient(itm, host=first)
    b = FakeClient(itm, host=second, other_client_timeout=0.05)
    async with a.shared_session():
        await a.fetch_units()
        with pytest.raises(SessionInUse):
            await b.fetch_units()


def test_lock_origin() -> None:
    """The lock's key drops case, default ports and slashes; odd ports stay as given."""
    assert _lock_origin("HTTPS://ITM.example:443/sub/") == "https://itm.example/sub"
    assert _lock_origin("http://itm.example:8080") == "http://itm.example:8080"
    assert _lock_origin("http://ITM.example:port") == "http://itm.example:port"


async def test_waiting_for_another_client_has_a_limit(itm: FakeITM) -> None:
    """A wait that outlasts the limit raises before anything is sent."""
    a = FakeClient(itm)
    b = FakeClient(itm, other_client_timeout=0.05)
    async with a.shared_session():
        await a.fetch_units()
        sent = len(itm.requests)
        with pytest.raises(SessionInUse, match="in use by another client") as err:
            await b.fetch_units()
        assert isinstance(err.value, LoginRejected)
        assert len(itm.requests) == sent
        assert not b._lock.locked()
        assert len(await a.fetch_units()) == 3
    assert itm.logins == 1
    assert len(await b.fetch_units()) == 3


async def test_queued_calls_share_the_limit(itm: FakeITM) -> None:
    """Calls queued while another client holds the session fail together."""
    loop = asyncio.get_running_loop()
    a = FakeClient(itm)
    b = FakeClient(itm, other_client_timeout=0.2)
    async with a.shared_session():
        await a.fetch_units()
        started = loop.time()
        results = await asyncio.gather(
            *(b.fetch_units() for _ in range(4)), return_exceptions=True
        )
        waited = loop.time() - started
        assert all(isinstance(r, SessionInUse) for r in results)
        assert waited < 0.35
        later = loop.time()
        with pytest.raises(SessionInUse):
            await b.fetch_units()
        assert loop.time() - later >= 0.2
    assert len(await b.fetch_units()) == 3


async def test_no_limit(itm: FakeITM) -> None:
    """Without a limit a second client waits as long as the first one needs."""
    a = FakeClient(itm)
    b = FakeClient(itm, other_client_timeout=None)
    async with a.shared_session():
        await a.fetch_units()
        other = asyncio.create_task(b.fetch_units())
        await asyncio.sleep(0.05)
        assert not other.done()
    assert len(await other) == 3


async def test_queued_calls_of_one_client_have_no_limit(
    itm: FakeITM, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The calls of one client wait for each other longer than the limit."""
    c = FakeClient(itm, other_client_timeout=0.01)
    orig = c._post

    async def slow(body: str) -> str:
        await asyncio.sleep(0.01)
        return await orig(body)

    monkeypatch.setattr(c, "_post", slow)
    results = await asyncio.gather(c.fetch_units(), c.fetch_units(), c.fetch_units())
    assert [len(r) for r in results] == [3, 3, 3]
    assert itm.logins == 3


async def test_other_hosts_and_users_do_not_wait(itm: FakeITM) -> None:
    """Another iTM, or another user of the same iTM, has a session of its own."""
    other_user = FakeITM(username="other")
    a = FakeClient(itm, host=HOST)
    clients = [
        FakeClient(FakeITM(), other_client_timeout=0.05),
        FakeClient(FakeITM(), host="itm2.example", other_client_timeout=0.05),
        FakeClient(other_user, host=HOST, other_client_timeout=0.05),
    ]
    async with a.shared_session():
        await a.fetch_units()
        for c in clients:
            assert len(await c.fetch_units()) == 3
        assert itm.logins == 1
    assert other_user.logins == 1


async def test_release_while_another_client_holds_the_session(itm: FakeITM) -> None:
    """The saved token is another client's live session: nothing is released."""
    store = MemorySessionStore()
    a, b = FakeClient(itm, store=store), FakeClient(itm, store=store)
    async with a.shared_session():
        await a.fetch_units()
        sent = len(itm.requests)
        assert await b.release_saved_session() is False
        assert len(itm.requests) == sent
        assert len(await a.fetch_units()) == 3
    assert itm.logins == 1
    store.token = "f" * 32
    assert await b.release_saved_session() is True


class SlowStore(MemorySessionStore):
    """A store whose first load takes a moment, as one on disk can."""

    def __init__(self) -> None:
        """Start with no token."""
        super().__init__()
        self.loads = 0
        self.saved = asyncio.Event()

    async def save(self, token: str | None) -> None:
        """Save the token, and tell when there is one."""
        await super().save(token)
        if token is not None:
            self.saved.set()

    async def load(self) -> str | None:
        """Return the token; the first time once one is saved, or after a moment."""
        self.loads += 1
        if self.loads == 1:
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(0.05):
                    await self.saved.wait()
        return await super().load()


async def test_release_racing_another_clients_login(itm: FakeITM) -> None:
    """A release that started first doesn't log out the login made meanwhile."""
    store = SlowStore()
    a, b = FakeClient(itm, store=store), FakeClient(itm, store=store)
    release = asyncio.create_task(b.release_saved_session())
    await asyncio.sleep(0)
    assert len(await a.fetch_units()) == 3
    assert await release is False
    assert itm.logins == 1
    assert itm.session is None


async def test_release_from_another_task_during_a_call(itm: FakeITM) -> None:
    """A call in flight is this client's live session: nothing is released."""
    store = SlowStore()
    c = FakeClient(itm, store=store)
    call = asyncio.create_task(c.fetch_units())
    await store.saved.wait()
    sent = len(itm.requests)
    assert await c.release_saved_session() is False
    assert len(itm.requests) == sent
    assert len(await call) == 3


async def test_locks_are_not_kept(itm: FakeITM) -> None:
    """A lock lives as long as a call holds or waits for it."""
    loop = asyncio.get_running_loop()
    a, b = FakeClient(itm), FakeClient(itm)
    await asyncio.gather(a.fetch_units(), b.fetch_units())
    async with a.shared_session():
        await a.fetch_units()
        assert [k for k in _USER_LOCKS if k[0] is loop]
    assert not [k for k in _USER_LOCKS if k[0] is loop]


def test_limit_must_be_positive() -> None:
    """A limit of zero or less is refused."""
    with pytest.raises(ValueError, match="other_client_timeout"):
        ItmClient(HOST, "user", "secret", other_client_timeout=0)
