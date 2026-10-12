"""Exceptions raised by the Daikin iTM client."""

from __future__ import annotations


class ItmError(Exception):
    """Base error for everything this library raises."""


class ProtocolError(ItmError):
    """A reply could not be decoded (not an iTM reply, or an unknown layout)."""


class SessionEnded(ItmError):  # noqa: N818 - public API name
    """The iTM ended the session (it answered with a LOGOUT update).

    For example because the token was stale.
    """


class VersionMismatch(SessionEnded):
    """The iTM expects another web UI version string; carries the one it reported."""

    def __init__(self, version: str) -> None:
        """Initialize the error.

        Args:
        ----
            version: The web UI version string the iTM reported.

        """
        super().__init__(f"iTM web UI version is {version}")
        self.version = version


class LoginRejected(ItmError):  # noqa: N818 - public API name
    """The iTM refused the login.

    Wrong credentials, or another web session is active (the iTM allows one).
    """


class ItmCommandError(ItmError):
    """The iTM answered a command with a dialog, or the UI was not as expected."""


class ZoneNotFound(ItmCommandError):  # noqa: N818 - public API name
    """A zone of the zone path is not there; carries the names that are."""

    def __init__(self, zone: str, available: list[str]) -> None:
        """Initialize the error.

        Args:
        ----
            zone: The zone that was not found.
            available: The names of the zones and units that are there.

        """
        super().__init__(f"{zone!r} not found here; available: {available}")
        self.zone = zone
        self.available = available


class SetpointOutOfRange(ItmCommandError):  # noqa: N818 - public API name
    """The unit does not accept the setpoint; carries the setpoints it does accept.

    The allowed setpoints depend on the unit and its operation mode, so they may have
    changed since the unit was last read.
    """

    def __init__(self, unit: str, value: int, allowed: tuple[int, ...]) -> None:
        """Initialize the error.

        Args:
        ----
            unit: The name of the unit.
            value: The setpoint that was asked for.
            allowed: The setpoints the unit accepts now, as read while writing.

        """
        msg = f"{unit}: setpoint {value} outside allowed range {list(allowed)}"
        super().__init__(msg)
        self.unit = unit
        self.value = value
        self.allowed = allowed
