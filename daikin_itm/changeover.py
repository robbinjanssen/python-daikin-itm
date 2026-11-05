"""Auto changeover groups: the session's screen writes and ItmClient's methods."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .exceptions import ItmCommandError
from .menu import (
    CG_ADD,
    CG_OK,
    CG_REMOVE,
    CLOSE_PART,
    CO_ADD,
    CO_DELETE,
    CO_DISABLE,
    CO_ENABLE,
    CO_LIST,
    CO_MEMBERS_BUTTON,
    CO_SETTINGS_BUTTON,
    CS_DIFFERENTIAL,
    CS_OK,
    CS_REFERENCE,
    MB_CHANGEOVER,
    ChangeoverGroup,
    ChangeoverReference,
    MenuScreen,
    changeover_candidates_from_screen,
    changeover_groups_from_screen,
    changeover_members_from_screen,
)
from .protocol import DataType, EventType

if TYPE_CHECKING:
    from collections.abc import Callable
    from contextlib import AbstractAsyncContextManager

    from .protocol import Reply
    from .session import _Session
    from .state import UIState


class ChangeoverSessionMixin:
    """_Session's changeover screen writes."""

    if TYPE_CHECKING:
        # pylint: disable=unused-argument
        state: UIState

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

        async def open_menu_screen(self, button: int, screen: MenuScreen) -> bool:
            """Press a menu list button and tell whether its screen opened."""

        async def close_screen(self, screen: MenuScreen) -> None:
            """Close (or cancel) a menu screen, if it is open. Never OK."""

    async def _select_changeover_group(self, name: str) -> ChangeoverGroup:
        await self.open_menu_screen(MB_CHANGEOVER, MenuScreen.CHANGEOVER)
        groups = changeover_groups_from_screen(self.state)
        names = [g.name for g in groups]
        if names.count(name) != 1:
            msg = f"changeover group {name!r} not found once; available: {names}"
            raise ItmCommandError(msg)
        index = names.index(name)
        await self.send(
            MenuScreen.CHANGEOVER,
            CO_LIST,
            EventType.CLICK,
            DataType.INTARRAY,
            1,
            array=(index,),
        )
        return groups[index]

    async def set_changeover_enabled(self, name: str, *, enabled: bool) -> None:
        """Press Enable or Disable for a group."""
        group = await self._select_changeover_group(name)
        try:
            if group.enabled == enabled:
                return
            if enabled and not group.member_count:
                msg = f"{name}: a group without members can't be enabled"
                raise ItmCommandError(msg)
            label = f"{'enable' if enabled else 'disable'} changeover group {name}"
            reply = await self.write(
                label, MenuScreen.CHANGEOVER, CO_ENABLE if enabled else CO_DISABLE
            )
            if reply is not None and reply.dialogs():
                await self._answer_confirm(MenuScreen.CHANGEOVER, reply)
        finally:
            await self.close_screen(MenuScreen.CHANGEOVER)

    async def set_changeover_settings(
        self,
        name: str,
        *,
        differential: int | None,
        reference: ChangeoverReference | None,
    ) -> None:
        """Pick a group's differential and/or reference method and press OK."""
        await self._select_changeover_group(name)
        try:
            await self.send(MenuScreen.CHANGEOVER, CO_SETTINGS_BUTTON)
            s = MenuScreen.CHANGEOVER_SETTINGS
            if s not in self.state.screens:
                msg = f"{name}: the settings screen did not open"
                raise ItmCommandError(msg)
            if differential is not None:
                options = self.state.get(s, CS_DIFFERENTIAL, "COMBO_NUMS") or []
                if differential not in options:
                    msg = f"{name}: differential {differential} not offered: {options}"
                    raise ItmCommandError(msg)
                await self.send(
                    s,
                    CS_DIFFERENTIAL,
                    EventType.CLICK,
                    DataType.INT,
                    options.index(differential),
                )
            if reference is not None:
                radio = CS_REFERENCE[list(ChangeoverReference).index(reference)]
                await self.send(s, radio)
            await self.press_ok(
                f"save settings of changeover group {name}", s, CS_OK, CLOSE_PART[s]
            )
        finally:
            await self.close_screen(MenuScreen.CHANGEOVER_SETTINGS)
            await self.close_screen(MenuScreen.CHANGEOVER)

    async def create_changeover_group(self, name: str) -> None:
        """Press Add, enter the name, OK (the group starts empty, disabled)."""
        await self.open_menu_screen(MB_CHANGEOVER, MenuScreen.CHANGEOVER)
        try:
            names = [g.name for g in changeover_groups_from_screen(self.state)]
            if name in names:
                msg = f"changeover group {name!r} exists already"
                raise ItmCommandError(msg)
            reply = await self.send(MenuScreen.CHANGEOVER, CO_ADD)
            dialogs = [d for d in reply.dialogs() if d[1] == "CHARINPUT"]
            if not dialogs:
                msg = f"Add did not ask for a name: {reply.dialogs()}"
                raise ItmCommandError(msg)
            dialog_id = dialogs[0][0]
            sent = await self.write(
                f"create changeover group {name}",
                MenuScreen.CHANGEOVER,
                dialog_id,
                EventType.CLICK,
                DataType.STRING,
                string=name,
            )
            if sent is None:  # dry run: cancel the name dialog
                await self.send(MenuScreen.CHANGEOVER, dialog_id, EventType.CLICK2)
            elif sent.dialogs():
                await self._answer_confirm(MenuScreen.CHANGEOVER, sent)
        finally:
            await self.close_screen(MenuScreen.CHANGEOVER)

    async def set_changeover_members(self, name: str, members: tuple[str, ...]) -> None:
        """Make a group's members exactly these (added and removed via Registration)."""
        await self._select_changeover_group(name)
        s = MenuScreen.CHANGEOVER_GROUP
        try:
            await self.send(MenuScreen.CHANGEOVER, CO_MEMBERS_BUTTON)
            if s not in self.state.screens:
                msg = f"{name}: the members screen did not open"
                raise ItmCommandError(msg)
            wanted = list(dict.fromkeys(members))
            for member in list(changeover_members_from_screen(self.state)):
                if member not in wanted:
                    current = list(changeover_members_from_screen(self.state))
                    await self.send(
                        s,
                        2,
                        EventType.CLICK,
                        DataType.INTARRAY,
                        1,
                        array=(current.index(member),),
                    )
                    await self.send(s, CG_REMOVE)
            for member in wanted:
                if member in changeover_members_from_screen(self.state):
                    continue
                candidates = changeover_candidates_from_screen(self.state)
                names = [c[0] for c in candidates]
                if member not in names:
                    msg = f"{name}: {member!r} is not available; candidates: {names}"
                    raise ItmCommandError(msg)
                if candidates[names.index(member)][1]:
                    other = candidates[names.index(member)][1]
                    msg = f"{name}: {member!r} is in group {other!r}"
                    raise ItmCommandError(msg)
                await self.send(
                    s,
                    11,
                    EventType.CLICK,
                    DataType.INTARRAY,
                    1,
                    array=(names.index(member),),
                )
                await self.send(s, CG_ADD)
            if list(changeover_members_from_screen(self.state)) != wanted:
                got = changeover_members_from_screen(self.state)
                msg = f"{name}: members would be {got}, not {wanted}"
                raise ItmCommandError(msg)
            await self.press_ok(
                f"save members of changeover group {name}", s, CG_OK, CLOSE_PART[s]
            )
        finally:
            await self.close_screen(MenuScreen.CHANGEOVER_GROUP)
            await self.close_screen(MenuScreen.CHANGEOVER)

    async def delete_changeover_group(self, name: str) -> None:
        """Press Delete for a (disabled) group and confirm."""
        group = await self._select_changeover_group(name)
        try:
            if (
                group.enabled
                or self.state.get(MenuScreen.CHANGEOVER, CO_DELETE, "ENABLED") == 0
            ):
                msg = f"{name}: disable the group before deleting it"
                raise ItmCommandError(msg)
            reply = await self.write(
                f"delete changeover group {name}", MenuScreen.CHANGEOVER, CO_DELETE
            )
            if reply is not None:
                await self._answer_confirm(MenuScreen.CHANGEOVER, reply)
        finally:
            await self.close_screen(MenuScreen.CHANGEOVER)


class ChangeoverMixin:
    """ItmClient's auto changeover methods."""

    if TYPE_CHECKING:
        # pylint: disable=unused-argument
        dry_run: bool

        _session: Callable[[], AbstractAsyncContextManager[_Session]]

        def _writable(self) -> None: ...

        async def fetch_changeover_groups(
            self, *, details: bool = False
        ) -> list[ChangeoverGroup]:
            """Read the auto changeover groups."""

    async def _changeover_result(self, name: str) -> ChangeoverGroup | None:
        """Return the group as read in a fresh session, or None in a dry run."""
        if self.dry_run:
            return None
        for group in await self.fetch_changeover_groups(details=True):
            if group.name == name:
                return group
        return None

    async def set_changeover_enabled(
        self,
        name: str,
        enabled: bool,  # noqa: FBT001 - positional like the iTM's radios
    ) -> ChangeoverGroup | None:
        """Enable or disable an auto changeover group.

        Args:
        ----
            name: The group's name.
            enabled: Enable (True) or disable the group.

        Returns:
        -------
            The group as read back in a fresh session, or None in a dry run.

        Raises:
        ------
            ItmCommandError: The group is not there, or has no members to enable.

        """
        self._writable()
        async with self._session() as s:
            try:
                await s.set_changeover_enabled(name, enabled=enabled)
            finally:
                await s.close_menu()
        result = await self._changeover_result(name)
        if result is not None and result.enabled != enabled:
            verb = "enable" if enabled else "disable"
            msg = f"{name}: the iTM did not {verb} the group"
            raise ItmCommandError(msg)
        return result

    async def set_changeover_settings(
        self,
        name: str,
        *,
        differential: int | None = None,
        reference: ChangeoverReference | None = None,
    ) -> ChangeoverGroup | None:
        """Change a group's differential (°C) and/or reference method.

        Args:
        ----
            name: The group's name.
            differential: The new differential, one the iTM offers (0-7 °C).
            reference: The new reference method.

        Returns:
        -------
            The group as read back in a fresh session, or None in a dry run.

        """
        self._writable()
        if differential is None and reference is None:
            return await self._changeover_result(name)
        async with self._session() as s:
            try:
                await s.set_changeover_settings(
                    name, differential=differential, reference=reference
                )
            finally:
                await s.close_menu()
        result = await self._changeover_result(name)
        if result is not None and (
            (differential is not None and result.differential != differential)
            or (reference is not None and result.reference != reference)
        ):
            msg = f"{name}: the iTM did not take the settings: {result}"
            raise ItmCommandError(msg)
        return result

    async def create_changeover_group(
        self,
        name: str,
        *,
        members: tuple[str, ...] = (),
        differential: int | None = None,
        reference: ChangeoverReference | None = None,
    ) -> ChangeoverGroup | None:
        """Create an auto changeover group (disabled), with members and settings.

        Each step is its own session and confirmed before the next.

        Args:
        ----
            name: The new group's name.
            members: The units to put in it.
            differential: Its differential, when not the iTM's default.
            reference: Its reference method, when not the iTM's default.

        Returns:
        -------
            The group as read back, or None in a dry run.

        """
        self._writable()
        async with self._session() as s:
            try:
                await s.create_changeover_group(name)
            finally:
                await s.close_menu()
        if self.dry_run:
            return None
        if await self._changeover_result(name) is None:
            msg = f"{name}: the iTM did not create the group"
            raise ItmCommandError(msg)
        if members:
            await self.set_changeover_members(name, members)
        if differential is not None or reference is not None:
            await self.set_changeover_settings(
                name, differential=differential, reference=reference
            )
        return await self._changeover_result(name)

    async def set_changeover_members(
        self, name: str, members: tuple[str, ...]
    ) -> ChangeoverGroup | None:
        """Make a group's members exactly these units.

        Args:
        ----
            name: The group's name.
            members: The units; others are removed. A unit in another group is
                refused.

        Returns:
        -------
            The group as read back in a fresh session, or None in a dry run.

        """
        self._writable()
        async with self._session() as s:
            try:
                await s.set_changeover_members(name, tuple(members))
            finally:
                await s.close_menu()
        result = await self._changeover_result(name)
        if result is not None and list(result.members) != list(dict.fromkeys(members)):
            msg = f"{name}: the iTM did not take the members: {result.members}"
            raise ItmCommandError(msg)
        return result

    async def delete_changeover_group(self, name: str) -> None:
        """Delete a disabled auto changeover group.

        Args:
        ----
            name: The group's name.

        Raises:
        ------
            ItmCommandError: The group is enabled, not there, or still there after.

        """
        self._writable()
        async with self._session() as s:
            try:
                await s.delete_changeover_group(name)
            finally:
                await s.close_menu()
        if not self.dry_run and await self._changeover_result(name) is not None:
            msg = f"{name}: the iTM did not delete the group"
            raise ItmCommandError(msg)
