"""Exceptions raised by the Daikin iTM client."""

from __future__ import annotations

from typing import TYPE_CHECKING, TypeVar, cast

if TYPE_CHECKING:
    from collections.abc import Sequence


class ItmError(Exception):
    """Base error for everything this library raises."""


class ProtocolError(ItmError):
    """A reply could not be decoded (not an iTM reply, or an unknown layout)."""


class RequestValueError(ItmError, ValueError):
    """A value can't be put in a request; raised before the request is sent.

    A number out of the range of its field, or a string too long, with a control
    character or with a character outside the Basic Multilingual Plane.
    """


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

    Wrong credentials, or this user is logged in elsewhere (the iTM allows one
    session per user).
    """


class SessionInUse(LoginRejected):
    """Another client in this process kept the same user's session for too long.

    The clients of one iTM and user in a process take turns on its single session.
    Raised before anything is sent, once a call has waited ``other_client_timeout``
    seconds for another client to be done.
    """


class ExperimentalError(ItmError):
    """A write not validated on a real iTM yet was asked without the opt-in.

    Raised before anything is sent, unless the client was created with
    ``allow_experimental=True`` (or the write is a dry run).
    """


class HistoryExportError(ItmError):
    """The history export could not be read."""


class ItmCommandError(ItmError):
    """The iTM answered a command with a dialog, or the UI was not as expected."""


class ReadOnlyError(ItmCommandError):
    """A write was asked of a client created with ``read_only=True``.

    Raised before anything is sent, so the iTM is not touched.
    """


class SharedSessionError(ItmCommandError):
    """A call can't run inside a ``shared_session()`` block.

    A write (also as a dry run), or a call that logs in by itself. Raised before
    anything is sent; do it outside the block.
    """


class MenuNotAvailable(ItmCommandError):  # noqa: N818 - public API name
    """The main menu did not open for this account.

    The login hid the main menu bar's menu button (raised then without pressing
    it), or the iTM answered the button without opening the menu list and without
    a dialog. Seen for a normal web user without allowed screens; an
    administrator account opens it. Every read and write through the main menu
    (schedules, history, info, system settings...) raises it then.
    """

    def __init__(self, *, button_hidden: bool = False) -> None:
        """Initialize the error.

        Args:
        ----
            button_hidden: The login hid the menu button, so it wasn't pressed.

        """
        why = (
            "the login hid the menu button, so it wasn't pressed"
            if button_hidden
            else "no menu list, no dialog"
        )
        msg = (
            f"the main menu is not available to this account ({why}); seen for a "
            "normal web user without allowed screens"
        )
        super().__init__(msg)


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


class UnitStopped(ItmCommandError):  # noqa: N818 - public API name
    """The unit (or every unit of a zone) is stopped; the iTM ignores the change then.

    Raised for a setpoint before the change is sent: the iTM accepts it and drops it.
    """

    def __init__(self, name: str) -> None:
        """Initialize the error.

        Args:
        ----
            name: The unit or zone.

        """
        msg = f"{name} is stopped; the iTM ignores setpoint changes then"
        super().__init__(msg)
        self.name = name


class NotOffered(ItmCommandError):  # noqa: N818 - public API name
    """The unit or zone doesn't offer what was asked; carries what it offers.

    Raised before the change is sent. The options are as the iTM showed them while
    writing (labels, states or numbers); empty when it offers none at all.
    """

    def __init__(
        self,
        msg: str,
        *,
        name: str | None = None,
        value: object = None,
        options: Sequence[object] = (),
    ) -> None:
        """Initialize the error.

        Args:
        ----
            msg: The message.
            name: The unit or zone, when the error is about one.
            value: What was asked.
            options: What is offered instead.

        """
        super().__init__(msg)
        self.name = name
        self.value = value
        self.options = tuple(options)


class NotConfirmed(ItmCommandError):  # noqa: N818 - public API name
    """A change was sent but the read-back doesn't show it; carries what it shows.

    The iTM may still apply it later (a unit takes a few seconds), or it ignored it.
    last_seen is the last read-back: a Unit, the zone's points by name, the unit's
    UnitSettings, or the settings a menu write read back.
    """

    def __init__(self, msg: str, *, name: str | None = None, last_seen: object) -> None:
        """Initialize the error.

        Args:
        ----
            msg: The message.
            name: The unit, zone or setting the change was for.
            last_seen: What the last read-back showed.

        """
        super().__init__(msg)
        self.name = name
        self.last_seen = last_seen

    def __str__(self) -> str:
        """Return the message, whatever a transport error's type makes of it."""
        return str(self.args[0]) if self.args else ""

    @classmethod
    def after(
        cls, msg: str, cause: BaseException, *, name: str | None, last_seen: object
    ) -> NotConfirmed:
        """Return the error for a check whose last read got no answer.

        Before a check survived a transport error, that error was raised: this
        one is of its type as well, so code that caught it still does.

        Args:
        ----
            msg: The message.
            cause: The last read's transport error.
            name: The unit or zone the change was for.
            last_seen: What the last read that got an answer showed, or None.

        Returns:
        -------
            The error, an instance of a subclass of this class and cause's type.

        """
        err = _combined(cls, cause, msg)
        err.name = name
        err.last_seen = last_seen
        return err


class SetpointOutOfRange(NotOffered):
    """The unit does not accept the setpoint; carries the setpoints it does accept.

    The allowed setpoints depend on the unit and its operation mode, so they may have
    changed since the unit was last read. A NotOffered (options: the allowed ones).
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
        super().__init__(msg, name=unit, value=value, options=allowed)
        self.unit = unit
        self.allowed = allowed


class WriteBudgetExceeded(ItmCommandError):  # noqa: N818 - public API name
    """A unit had as many writes as its budget allows; nothing was sent.

    Raised before a unit or zone write logs in (a zone write: after reading its
    members), when the client was created with max_writes_per_unit_per_hour or
    max_writes_per_unit_per_day and the unit already had that many writes sent
    within the hour or day: a guard against a loop in the caller (how many
    setting writes an indoor unit takes is not known).
    """

    def __init__(self, msg: str, *, name: str, limit: int, seconds: float) -> None:
        """Initialize the error.

        Args:
        ----
            msg: The message.
            name: The unit whose budget is spent.
            limit: The number of writes the budget allows.
            seconds: The window the budget counts over (3600 or 86400).

        """
        super().__init__(msg)
        self.name = name
        self.limit = limit
        self.seconds = seconds


class WriteOutcomeUnknown(ItmCommandError):  # noqa: N818 - public API name
    """A change was sent but its reply was lost: whether the iTM made it is unknown.

    The request was sent (the OK, its Yes or the operation panel's commit), but its
    reply, or that of a later request of the call (the clean-up, the logout), was
    lost (a timeout, a dropped connection) or could not be read. The
    error raised is also of the transport error's own type (TimeoutError, the
    aiohttp error, ProtocolError), which is its __cause__, so code that caught
    that before still does. After a unit or zone write the next write to the
    unit first waits for the change to show, as after confirm=False; read the
    unit before repeating the write, which may spend a unit write for nothing.
    """

    def __str__(self) -> str:
        """Return the message, whatever the transport error's type makes of it."""
        return str(self.args[0]) if self.args else ""

    @classmethod
    def after(cls, msg: str, cause: BaseException) -> WriteOutcomeUnknown:
        """Return the error for a lost reply, of the cause's type as well.

        Args:
        ----
            msg: The message.
            cause: The transport error.

        Returns:
        -------
            The error, an instance of a subclass of this class and cause's type,
            with the cause's attributes (an HTTP error's status, an OSError's
            errno) and notes.

        """
        return _combined(cls, cause, msg)


_E = TypeVar("_E", bound=ItmCommandError)

# An error class combined with each transport error type seen.
_COMBINED_TYPES: dict[tuple[type[BaseException], type[BaseException]], type] = {}


def _combined(base: type[_E], cause: BaseException, msg: str) -> _E:
    """Return an error of base and cause's type, with cause's attributes and notes."""
    kind: type[_E]
    if isinstance(cause, base):
        kind = type(cause)
    else:
        key = (base, type(cause))
        if key not in _COMBINED_TYPES:
            _COMBINED_TYPES[key] = type(
                base.__name__, (base, type(cause)), {"__module__": __name__}
            )
        kind = cast("type[_E]", _COMBINED_TYPES[key])
    err = kind.__new__(kind, msg)
    Exception.__init__(err, msg)
    err.__dict__.update(vars(cause))
    if hasattr(cause, "__notes__"):
        err.__notes__ = list(cause.__notes__)
    if isinstance(cause, OSError) and isinstance(err, OSError):
        err.errno, err.strerror = cause.errno, cause.strerror
    return err


class RequestSkipped(TimeoutError):  # noqa: N818 - public API name
    """A request was not sent: an earlier one of the same login got no answer.

    After a request that timed out or lost its connection the iTM may hang (it does
    after a click on a disabled control), so the clean-up of that call (closing
    screens and dialogs) is skipped and only the logout is tried, once, with
    ItmClient.logout_timeout. A TimeoutError, like the error that started it, not
    an ItmError: the iTM didn't answer.
    """
