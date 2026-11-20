# pylint: disable=protected-access
"""Passwords and page tokens stay out of logs, plans, errors, reprs and traces."""

from __future__ import annotations

import hashlib
import logging
import secrets
from itertools import count
from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import EmailServer, ItmCommandError
from daikin_itm.protocol import NO_TOKEN
from daikin_itm.trace import TraceFile

from .fake_itm import FakeITM
from .fake_service_edit import FakeServiceEditITM
from .fake_system_edit import FakeSystemEditITM
from .fake_users import FakeUsersITM
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable
    from pathlib import Path

SECRET = "Zq8-leak-check"
LOGIN_DIGEST = hashlib.sha256(b"secret").hexdigest()


def _service(itm: FakeITM) -> None:
    itm.service_password = SECRET


def _nothing(_itm: FakeITM) -> None:
    pass


CASES: dict[
    str,
    tuple[
        type[FakeITM],
        Callable[[FakeITM], None],
        Callable[[FakeClient], Awaitable[Any]],
    ],
] = {
    "service login": (
        FakeITM,
        _service,
        lambda c: c.fetch_service_settings(SECRET),
    ),
    "wrong service password": (
        FakeITM,
        _nothing,
        lambda c: c.fetch_service_settings(SECRET),
    ),
    "new web user": (
        FakeITM,
        _nothing,
        lambda c: c.create_web_user("demouser", SECRET, zones=("Building",)),
    ),
    "web user password": (
        FakeITM,
        _nothing,
        lambda c: c.set_web_user("demouser", password=SECRET),
    ),
    "web user password too long": (
        FakeITM,
        _nothing,
        lambda c: c.set_web_user("demouser", password=SECRET * 2),
    ),
    "e-mail server": (
        FakeSystemEditITM,
        _nothing,
        lambda c: c.set_email_server(EmailServer(password=SECRET)),
    ),
    "own password": (
        FakeUsersITM,
        _nothing,
        lambda c: c.set_own_settings(password=SECRET, allow_password_change=True),
    ),
    "touch panel password": (
        FakeServiceEditITM,
        _nothing,
        lambda c: c.set_password_setting(
            "screen", enabled=True, password=SECRET, allow_password_change=True
        ),
    ),
}


def _record_tokens(c: FakeClient, trace: TraceFile) -> set[str]:
    """Trace the client's requests; return the set the reply tokens go into."""
    tokens: set[str] = set()
    plain_post = c._post

    async def recording(body: str) -> str:
        reply = await plain_post(body)
        tokens.add(reply[:32])
        return reply

    c._post = trace.wrap(recording)  # type: ignore[method-assign,assignment]
    return tokens


@pytest.mark.parametrize("dry_run", [False, True], ids=["sent", "dry-run"])
@pytest.mark.parametrize("case", list(CASES))
async def test_no_secret_leaks(
    case: str, *, dry_run: bool, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """No password, login hash or page token shows anywhere the caller can see."""
    caplog.set_level(logging.DEBUG)
    fake_type, prepare, call = CASES[case]
    itm = fake_type()
    if case.startswith("web user password"):
        await FakeClient(itm).create_web_user(
            "demouser", "0ther-pw", zones=("Building",)
        )
    prepare(itm)
    itm.tokens = (secrets.token_hex(16) for _ in count())
    c = FakeClient(itm, dry_run=dry_run)
    trace = TraceFile(tmp_path / "trace.jsonl")
    tokens = _record_tokens(c, trace)
    seen: list[str] = []
    try:
        await call(c)
    except ItmCommandError as err:
        seen += [str(err), repr(err), *getattr(err, "__notes__", ())]
    seen += [
        caplog.text,
        trace.path.read_text(encoding="utf-8"),
        *c.planned,
        repr(c),
        repr(c._store),
    ]
    assert dry_run or "plain http" in caplog.text
    text = "\n".join(seen).lower()
    tokens.discard(NO_TOKEN)
    assert tokens
    for secret in (SECRET, LOGIN_DIGEST, *tokens):
        assert secret.lower() not in text


def test_email_server_repr_hides_the_password() -> None:
    """An EmailServer's repr leaves the password out."""
    server = EmailServer(smtp_address="mail.example.com", password=SECRET)
    assert SECRET not in repr(server)
    assert "mail.example.com" in repr(server)


async def test_plain_http_warns_once_per_client(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A password typed over http warns once per client; https and the login don't."""
    itm = FakeITM()
    c = FakeClient(itm)
    assert c.plain_http
    await c.fetch_units()
    assert "plain http" not in caplog.text
    await c.fetch_service_settings("test-service")
    await c.fetch_service_settings("test-service")
    warnings = [r for r in caplog.records if "plain http" in r.getMessage()]
    assert len(warnings) == 1
    assert warnings[0].levelno == logging.WARNING
    assert "test-service" not in caplog.text
    caplog.clear()
    await FakeClient(itm).fetch_service_settings("test-service")
    assert "plain http" in caplog.text
    caplog.clear()
    secure = FakeClient(itm, host="https://itm.example")
    assert not secure.plain_http
    await secure.fetch_service_settings("test-service")
    assert "plain http" not in caplog.text
