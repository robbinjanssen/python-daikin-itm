"""Client-side view of the iTM screens.

Replies only carry parts that differ from what the iTM believes the client is
showing, so the client has to keep the merged view itself, and has to apply its own
selections locally like the browser does: the iTM never echoes back a value the
client picked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .protocol import ScreenUpdate


class UIState:
    """The merged parts of every open screen."""

    def __init__(self) -> None:
        """Start without any open screens."""
        self.screens: dict[int, dict[tuple[int, str], Any]] = {}
        # The values the client set itself (screen, part, type), until the iTM
        # sends that part or closes its screen.
        self._local: set[tuple[int, int, str]] = set()

    def apply(self, updates: list[ScreenUpdate]) -> None:
        """Merge the screen updates of a reply.

        Args:
        ----
            updates: The screen updates, in reply order.

        """
        for s in updates:
            if s.update == "CLOSE":
                self.screens.pop(s.screen, None)
                self._local = {k for k in self._local if k[0] != s.screen}
            elif s.parts or s.update in {"OPEN", "SHOW", "UPDATE", "RELOAD"}:
                parts = self.screens.setdefault(s.screen, {})
                for p in s.parts:
                    parts[p.part, p.type] = p.value
                    self._local.discard((s.screen, p.part, p.type))

    def select_locally(self, screen: int, part: int, index: int) -> None:
        """Mirror a combobox/spinbox selection like the browser updates its control.

        Args:
        ----
            screen: The screen of the control.
            part: The part ID of the control.
            index: The selected index.

        """
        parts = self.screens.get(screen)
        if parts is not None and (part, "SINGLE_SELIDX") in parts:
            parts[part, "SINGLE_SELIDX"] = index
            self._local.add((screen, part, "SINGLE_SELIDX"))

    def set_check(self, screen: int, part: int, *, on: bool) -> None:
        """Keep a checkbox's state after ticking it: the iTM doesn't echo it."""
        parts = self.screens.get(screen)
        if parts is not None:
            parts[part, "CHECK_STATE"] = int(on)
            self._local.add((screen, part, "CHECK_STATE"))

    def picked(self, screen: int) -> set[tuple[int, str]]:
        """Return the parts of an open screen that show the client's own pick.

        The iTM never echoes a pick, so such a value only says what the client
        asked for, not what the iTM took, until the iTM sends that part again.

        Args:
        ----
            screen: The screen.

        Returns:
        -------
            The (part, type) keys still holding a pick.

        """
        parts = self.screens.get(screen, {})
        return {
            (part, ptype)
            for s, part, ptype in self._local
            if s == screen and (part, ptype) in parts
        }

    def get(self, screen: int, part: int, ptype: str, default: Any = None) -> Any:
        """Return the last known value of a part.

        Args:
        ----
            screen: The screen of the part.
            part: The part ID.
            ptype: The part update type, e.g. "LABEL".
            default: What to return when the value is unknown.

        Returns:
        -------
            The value, or default.

        """
        return self.screens.get(screen, {}).get((part, ptype), default)

    def visible(self, screen: int, part: int) -> bool:
        """Tell whether a part is visible (parts are visible unless hidden)."""
        return bool(self.get(screen, part, "VISIBLED", 1) != 0)
