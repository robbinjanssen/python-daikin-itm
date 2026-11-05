"""What ItmClient and _Session provide to their mixins, for type checking."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterable
    from contextlib import AbstractAsyncContextManager

    from ._util import T as _T
    from .client import ItmClient
    from .menu import (
        ChangeoverGroup,
        EmergencyStopProgram,
        ItmInfo,
        MenuScreen,
        ScheduleProgram,
        ScheduleSummary,
    )
    from .protocol import DataType, EventType, Reply, ScreenID
    from .session import _Session
    from .state import UIState
    from .system import SystemSettings


class ClientCore:
    """The ItmClient members its feature mixins use."""

    if TYPE_CHECKING:
        # pylint: disable=unused-argument
        dry_run: bool
        read_only: bool
        planned: list[str]
        _username: str
        _password: str

        _session: Callable[[], AbstractAsyncContextManager[_Session]]
        _own_session: Callable[[], AbstractAsyncContextManager[_Session]]

        async def _in_session(
            self, call: Callable[[_Session], Awaitable[_T]]
        ) -> _T: ...

        async def _system_write(
            self, call: Callable[[_Session], Awaitable[None]]
        ) -> None: ...

        async def _get_bytes(self, path: str) -> bytes: ...

        def _writable(self) -> None: ...

        def _experimental(self, what: str) -> None: ...

        async def fetch_info(self) -> ItmInfo:
            """Read the version, region and time screens."""

        async def fetch_system(
            self, only: Iterable[str] | None = None
        ) -> SystemSettings:
            """Read the system settings."""

        async def fetch_language(self) -> str | None:
            """Read the display language as a code."""

        async def fetch_changeover_groups(
            self, *, details: bool = False
        ) -> list[ChangeoverGroup]:
            """Read the auto changeover groups."""

        async def fetch_emergency_stop(self) -> list[EmergencyStopProgram]:
            """Read the emergency stop programs."""

        async def fetch_schedule(
            self, name: str, *, events: bool = True, details: bool = False
        ) -> ScheduleProgram:
            """Read a schedule program in full."""

        async def fetch_schedules(self) -> list[ScheduleSummary]:
            """Read the schedule programs."""


class SessionCore:
    """The _Session members its screen mixins use."""

    if TYPE_CHECKING:
        # pylint: disable=unused-argument
        state: UIState
        logged_in: bool
        _client: ItmClient

        async def send(  # noqa: PLR0913, PLR0917  # pylint: disable=too-many-arguments
            self,
            screen: int = -1,
            part: int = -1,
            event: int = EventType.CLICK,
            data_type: int = DataType.NONE,
            number: int = -1,
            string: str = "",
            array: tuple[int, ...] = (),
        ) -> Reply:
            """Send a GUI event and merge the reply into the UI state."""

        async def write(  # noqa: PLR0913, PLR0917  # pylint: disable=too-many-arguments
            self,
            label: str,
            screen: int,
            part: int,
            event: int = EventType.CLICK,
            data_type: int = DataType.NONE,
            number: int = -1,
            string: str = "",
        ) -> Reply | None:
            """Send the request that makes a change, unless this is a dry run."""

        async def press_ok(self, label: str, screen: int, ok: int, cancel: int) -> bool:
            """Press a screen's OK, or cancel in a dry run."""

        async def _answer_confirm(self, screen: int, reply: Reply) -> None: ...

        def _writable(self) -> None: ...

        async def open_menu_screen(self, button: int, screen: MenuScreen) -> bool:
            """Press a menu list button and tell whether its screen opened."""

        async def close_screen(self, screen: MenuScreen) -> None:
            """Close (or cancel) a menu screen, if it is open. Never OK."""

        async def _open_system_screen(
            self, button: int, screen: MenuScreen
        ) -> None: ...

        async def _select_listed(
            self, screen: int, part: int, column: int, name: str, what: str
        ) -> None: ...

        async def click(self, screen: int, part: int, what: str) -> Reply:
            """Click a button, naming it when the iTM has it disabled."""

        async def answer_number(
            self, button: int, value: float, what: str, screen: int = ScreenID.SETTINGS
        ) -> None:
            """Press a "change" button and enter a number in the dialog it opens."""

        async def answer_text(
            self, screen: int, button: int, text: str, what: str
        ) -> None:
            """Press a "change" button and enter text in the input dialog it opens."""

        @property
        def settings_open(self) -> bool:
            """Whether the settings dialog is open."""
