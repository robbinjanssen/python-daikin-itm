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

    def apply(self, updates: list[ScreenUpdate]) -> None:
        """Merge the screen updates of a reply.

        Args:
        ----
            updates: The screen updates, in reply order.

        """
        for s in updates:
            if s.update == "CLOSE":
                self.screens.pop(s.screen, None)
            elif s.parts or s.update in {"OPEN", "SHOW", "UPDATE", "RELOAD"}:
                parts = self.screens.setdefault(s.screen, {})
                for p in s.parts:
                    parts[p.part, p.type] = p.value

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
