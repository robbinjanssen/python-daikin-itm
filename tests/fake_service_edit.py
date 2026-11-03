"""A simulated iTM with the leakage check, layout setup, R32 ports and passwords.

Behind the service login: the leakage check's outdoor units (3100) with their
indoor unit registration (3101), schedule (3102) and schedule copy (3103), manual
start/stop and the results CSV (CONFIRM, PROGRESS, DLG_DOWNLOAD, FINISH_DOWNLOAD);
the layout setup button (81, a job behind a CONFIRM and a PROGRESS dialog); the R32
settings (1516) and their output port setup (1517). On the system tab: the
password setting (1900). Strict like the real iTM: clicking a part it reports
disabled raises (the real web server hangs then), and so does the R32 output test.
Every name and value is a generic placeholder.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from daikin_itm.protocol import PART_UPDATE_CODE, SCREEN_UPDATE_CODE, Writer

from .fake_itm import FakeITM, Update, _encode_data, _encode_part, _rows

LEAKAGE, REGISTRATION, SCHEDULE, COPY = 3100, 3101, 3102, 3103
LAYOUT, R32, PORTS, PASSWORDS = 1510, 1516, 1517, 1900
ACTIVATIONS = ["Disable", "Enable"]
DATE_RANGE = ((2010, 0, 1), (2036, 11, 31))
LK_BUTTONS = (4, 6, 7, 8, 9, 10, 27)
PROGRAM_PARTS = tuple(tuple(4 + 6 * i + k for k in range(5)) for i in range(4))
CONFIRM_ID, PROGRESS_ID, PASSWORD_ID, REPEAT_ID, ERROR_ID = 30, 31, 38, 39, 41
DATE_ID, HOURMIN_ID = 42, 43
PORT_ROWS = [["DO", "Alarm out A", "1-01"], ["DO", "Alarm out B", "1-02"]]


def _confirm(text: str = "Execute setting?") -> list[Any]:
    return [CONFIRM_ID, 2, text, "Confirm", "Yes", "No"]


def _password_dialog(dialog_id: int) -> list[Any]:
    return [
        dialog_id,
        5,
        "Password",
        4,
        15,
        0,
        "OK",
        "Cancel",
        "",
        "",
        "",
        "Error",
        0,
    ]


@dataclass
class Program:
    """A leakage check schedule program."""

    activation: int = 0
    day: tuple[int, int, int] = (2026, 0, 1)  # [year, month 0-11, day]
    at: tuple[int, int] = (2, 0)

    def texts(self) -> tuple[str, str]:
        """Return the date and time as the screen shows them."""
        y, m, d = self.day
        return f"{d:02d}/{m + 1:02d}/{y}", f"{self.at[0]:02d}:{self.at[1]:02d}"


@dataclass
class Outdoor:
    """An outdoor unit on the leakage check screen."""

    address: str
    name: str
    registered: list[str] = field(default_factory=list)
    programs: list[Program] = field(
        default_factory=lambda: [Program() for _ in range(4)]
    )
    running: bool = False
    charge: str = "Completed"


class FakeServiceEditITM(FakeITM):  # pylint: disable=too-many-instance-attributes
    """A FakeITM with the leakage check, layout setup, R32 ports and passwords."""

    def __init__(self) -> None:
        """Start with two outdoor units, no R32 port and no passwords enabled."""
        super().__init__()
        self.outdoors = [
            Outdoor("1-00", "OU-1", registered=[self.units[0].name]),
            Outdoor("1-01", "OU-2"),
        ]
        self.indoor = [u.name for u in self.units]
        self.layout_data = False
        self.layout_jobs = 0
        self.ports = ["", ""]
        self.pw_values: dict[str, str | None] = {"administrator": None, "screen": None}
        self.enabled = {"administrator": False, "screen": False}
        self.refuse_start = False
        self.lk_selected: int | None = None  # the selected outdoor unit (3100)
        self.sv_dialog: tuple[Any, ...] | None = None  # what the open dialog answers
        self.edit: dict[str, Any] = {}  # the open sub-screen's unsaved state
        self.files: dict[str, bytes] = {}
        self.finished: list[tuple[int, int]] = []
        self.job = 0

    # -- routing ----------------------------------------------------------------------
    def _service_event(self, ev: dict[str, Any]) -> list[Update] | None:
        part = ev["part"]
        if part == 81 and self.service_mode:
            self.open_screens.add(LAYOUT)
            self.sv_dialog = ("layout",)
            return [("OPEN", LAYOUT, [-1], []), ("DLG_OPEN", LAYOUT, _confirm(), [])]
        out = super()._service_event(ev)
        if part == 93 and self.service_mode and out is not None:
            out.append(("UPDATE", 5, [], [(81, "VISIBLED", int(self.layout_data))]))
        return out

    def _service_parts(self, target: int) -> list[tuple[int, str, Any]]:
        if target == LEAKAGE:
            self.lk_selected = None
            return self._leakage_parts()
        if target == R32:
            return self._r32_parts()
        return super()._service_parts(target)

    def _menu(self, ev: dict[str, Any]) -> list[Update]:
        screen, part = ev["screen"], ev["part"]
        if screen == 5 and part == 33 and self.menu_open:
            self.open_screens.add(PASSWORDS)
            return [("OPEN", PASSWORDS, [-1], self._password_parts())]
        handlers = {
            LEAKAGE: self._leakage_event,
            REGISTRATION: self._registration_event,
            SCHEDULE: self._schedule_event,
            COPY: self._copy_event,
            LAYOUT: self._layout_event,
            R32: self._r32_event,
            PORTS: self._ports_event,
            PASSWORDS: self._password_event,
        }
        if screen in handlers and screen in self.open_screens:
            return handlers[screen](ev)
        return super()._menu(ev)

    @staticmethod
    def _hang(screen: int, part: int) -> None:
        msg = f"{screen}/{part} clicked while disabled: the real iTM hangs"
        raise RuntimeError(msg)

    def _close(self, screen: int) -> list[Update]:
        self.open_screens.discard(screen)
        return [("CLOSE", screen, [], [])]

    def _answer(self, ev: dict[str, Any]) -> tuple[Any, ...] | None:
        """Take the open dialog's purpose when ev answers it."""
        if self.sv_dialog is None or ev["part"] not in {
            CONFIRM_ID,
            PASSWORD_ID,
            REPEAT_ID,
            DATE_ID,
            HOURMIN_ID,
        }:
            return None
        dialog, self.sv_dialog = self.sv_dialog, None
        return dialog

    # -- 3100 -------------------------------------------------------------------------
    def _outdoor_rows(self) -> dict[str, Any]:
        return _rows(
            [
                [
                    o.address,
                    o.name,
                    "VRV",
                    str(len(o.registered)),
                    o.charge,
                    "Yes" if any(p.activation for p in o.programs) else "",
                ]
                for o in self.outdoors
            ]
        )

    def _leakage_parts(self) -> list[tuple[int, str, Any]]:
        parts: list[tuple[int, str, Any]] = [(2, "LIST_ROW", self._outdoor_rows())]
        if self.lk_selected is None:
            return [*parts, *[(p, "ENABLED", 0) for p in LK_BUTTONS]]
        o = self.outdoors[self.lk_selected]
        return [
            *parts,
            *[(p, "ENABLED", 1) for p in LK_BUTTONS],
            (6, "ENABLED", int(not o.running)),
            (7, "ENABLED", int(o.running)),
        ]

    def _outdoor(self, part: int) -> Outdoor:
        if self.lk_selected is None:
            self._hang(LEAKAGE, part)
        assert self.lk_selected is not None
        return self.outdoors[self.lk_selected]

    def _leakage_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update]:
        part, event = ev["part"], ev["event"]
        if event == 5:  # UPD_PROG: the CSV is ready
            return self._download()
        if event == 9:  # FINISH_DOWNLOAD [job, reason]
            self.finished.append((ev["array"][0], ev["array"][1]))
            return [("DLG_CLOSE", LEAKAGE, [PROGRESS_ID, 11], [])]
        dialog = self._answer(ev)
        if dialog is not None:
            return self._leakage_answer(dialog, event)
        if part == 2 and ev["dtype"] == 1:
            self.lk_selected = ev["number"]
            return [("UPDATE", LEAKAGE, [], self._leakage_parts())]
        if part == 3:
            return self._close(LEAKAGE)
        if part in LK_BUTTONS and self.lk_selected is None:
            self._hang(LEAKAGE, part)
        o = self._outdoor(part)
        if part in {6, 7}:
            if (part == 6) == o.running:
                self._hang(LEAKAGE, part)
            self.sv_dialog = ("start" if part == 6 else "stop",)
            return [("DLG_OPEN", LEAKAGE, _confirm("Leakage check?"), [])]
        if part == 10:
            self.sv_dialog = ("csv",)
            return [("DLG_OPEN", LEAKAGE, _confirm("CSV output?"), [])]
        if part == 4:
            return self._open_registration(o)
        if part == 8:
            return self._open_schedule(o)
        if part == 9:
            return self._open_copy(o)
        return [("UPDATE", LEAKAGE, [], [])]

    def _leakage_answer(self, dialog: tuple[Any, ...], event: int) -> list[Update]:
        close: list[Update] = [("DLG_CLOSE", LEAKAGE, [CONFIRM_ID, 2], [])]
        if event != 1:
            return close
        if dialog[0] == "csv":
            self.job += 1
            progress = [PROGRESS_ID, 11, "Busy", self.job, 1, "Progress", "Cancel"]
            return [*close, ("DLG_OPEN", LEAKAGE, progress, [])]
        o = self._outdoor(CONFIRM_ID)
        if dialog[0] == "start" and self.refuse_start:
            error = [ERROR_ID, 1, "Status unsuitable", "Error", "OK"]
            return [*close, ("DLG_OPEN", LEAKAGE, error, [])]
        o.running = dialog[0] == "start"
        return [*close, ("UPDATE", LEAKAGE, [], self._leakage_parts())]

    def _download(self) -> list[Update]:
        lines = [
            "intelligent Touch Manager",
            "2024/12/20 PM 2:00",
            "Ver 1.00.00",
            "Leakage Check Result",
            ",".join(f"{o.name}," for o in self.outdoors),
            ",".join("Check Date,Leakage Amount" for _ in self.outdoors),
            "2026/05/10 AM1:00,0.0,2026/05/23 AM2:30,0.1",
            "2024/11/29 AM2:00,0.2,,",
        ]
        path = f"/sdlfs/WebExport/LeakageCheck_{self.job}.csv"
        self.files[path] = ("\r\n".join(lines) + "\r\n").encode()
        data = [PROGRESS_ID, 11, path, "", "Ready", "", ""]
        return [("DLG_DOWNLOAD", LEAKAGE, data, [])]

    # -- 3101 and 3103: two multi-select lists ----------------------------------------
    def _open_registration(self, o: Outdoor) -> list[Update]:
        self.edit = {"chosen": list(o.registered), "outdoor": o, "sel": {}}
        self.edit["all"] = list(self.indoor)
        self.open_screens.add(REGISTRATION)
        return [("OPEN", REGISTRATION, [-1], self._picker_parts(REGISTRATION))]

    def _open_copy(self, o: Outdoor) -> list[Update]:
        others = [x.name for x in self.outdoors if x is not o]
        self.edit = {"chosen": [], "outdoor": o, "sel": {}, "all": others}
        self.open_screens.add(COPY)
        parts = [(2, "TEXT", o.name), *self._picker_parts(COPY)]
        return [("OPEN", COPY, [-1], parts)]

    def _picker_parts(self, screen: int) -> list[tuple[int, str, Any]]:
        chosen_part, available_part = (2, 4) if screen == REGISTRATION else (4, 10)
        e = self.edit
        available = [n for n in e["all"] if n not in e["chosen"]]
        return [
            (chosen_part, "LIST_ROW", _rows([[f"A{n}", n] for n in e["chosen"]])),
            (available_part, "LIST_ROW", _rows([[f"A{n}", n] for n in available])),
        ]

    def _picker_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements,too-many-locals
        self, screen: int, ev: dict[str, Any]
    ) -> list[Update]:
        lists = {REGISTRATION: (2, 4, 6, 7, 9, 10), COPY: (4, 10, 6, 7, 11, 12)}
        chosen_part, available_part, add, remove, ok, cancel = lists[screen]
        part, e = ev["part"], self.edit
        dialog = self._answer(ev)
        if dialog is not None:
            close: list[Update] = [("DLG_CLOSE", screen, [CONFIRM_ID, 2], [])]
            if ev["event"] != 1:
                return close
            if screen == REGISTRATION:
                e["outdoor"].registered = list(e["chosen"])
            else:
                for name in e["chosen"]:
                    target = next(o for o in self.outdoors if o.name == name)
                    target.programs = [
                        Program(p.activation, p.day, p.at)
                        for p in e["outdoor"].programs
                    ]
            return [*close, *self._close(screen)]
        if part in {chosen_part, available_part} and ev["dtype"] == 8:
            e["sel"] = {part: list(ev["array"])}
            return [("UPDATE", screen, [], [])]
        if part in {add, remove}:
            source = available_part if part == add else chosen_part
            if source not in e["sel"]:
                self._hang(screen, part)
            rows = e["sel"].pop(source)
            if part == add:
                available = [n for n in e["all"] if n not in e["chosen"]]
                e["chosen"] += [available[r] for r in rows]
            else:
                e["chosen"] = [n for i, n in enumerate(e["chosen"]) if i not in rows]
            return [("UPDATE", screen, [], self._picker_parts(screen))]
        if part == ok:
            self.sv_dialog = ("save",)
            return [("DLG_OPEN", screen, _confirm(), [])]
        if part == cancel:
            return self._close(screen)
        return [("UPDATE", screen, [], [])]

    def _registration_event(self, ev: dict[str, Any]) -> list[Update]:
        return self._picker_event(REGISTRATION, ev)

    def _copy_event(self, ev: dict[str, Any]) -> list[Update]:
        return self._picker_event(COPY, ev)

    # -- 3102 -------------------------------------------------------------------------
    def _open_schedule(self, o: Outdoor) -> list[Update]:
        self.edit = {
            "outdoor": o,
            "programs": [Program(p.activation, p.day, p.at) for p in o.programs],
        }
        self.open_screens.add(SCHEDULE)
        return [("OPEN", SCHEDULE, [-1], self._schedule_parts())]

    def _schedule_parts(self) -> list[tuple[int, str, Any]]:
        parts: list[tuple[int, str, Any]] = [(2, "TEXT", self.edit["outdoor"].name)]
        for (combo, date_text, _, time_text, _), p in zip(
            PROGRAM_PARTS, self.edit["programs"], strict=True
        ):
            day, at = p.texts()
            parts += [
                (combo, "COMBO_STRS", ACTIVATIONS),
                (combo, "SINGLE_SELIDX", p.activation),
                (date_text, "TEXT", day),
                (time_text, "TEXT", at),
            ]
        return parts

    def _schedule_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update]:
        part, event, programs = ev["part"], ev["event"], self.edit["programs"]
        dialog = self._answer(ev)
        if dialog is not None:
            close: list[Update] = [("DLG_CLOSE", SCHEDULE, [part, 2], [])]
            if event != 1:
                return close
            if dialog[0] == "save":
                self.edit["outdoor"].programs = programs
                return [*close, *self._close(SCHEDULE)]
            program = programs[dialog[1]]
            if dialog[0] == "date":
                program.day = tuple(ev["array"][:3])
            else:
                program.at = (ev["array"][0], ev["array"][1])
            return [*close, ("UPDATE", SCHEDULE, [], self._schedule_parts())]
        for i, (combo, _, date_button, _, time_button) in enumerate(PROGRAM_PARTS):
            p = programs[i]
            if part == combo and ev["dtype"] == 1:
                p.activation = ev["number"]
                return [("UPDATE", SCHEDULE, [], [])]
            if part == date_button:
                self.sv_dialog = ("date", i)
                data = [DATE_ID, 9, 6, "Date", *p.day, *DATE_RANGE[0], *DATE_RANGE[1]]
                return [("DLG_OPEN", SCHEDULE, [*data, "OK", "Cancel"], [])]
            if part == time_button:
                self.sv_dialog = ("time", i)
                data = [HOURMIN_ID, 10, 4, "Time", *p.at, 0, 0, 23, 59]
                return [("DLG_OPEN", SCHEDULE, [*data, "OK", "Cancel"], [])]
        if part == 27:
            self.sv_dialog = ("save",)
            return [("DLG_OPEN", SCHEDULE, _confirm(), [])]
        if part == 28:
            return self._close(SCHEDULE)
        return [("UPDATE", SCHEDULE, [], [])]

    # -- 1510 -------------------------------------------------------------------------
    def _layout_event(self, ev: dict[str, Any]) -> list[Update]:
        if ev["event"] == 5:  # UPD_PROG: the job is done
            self.layout_jobs += 1
            return [("DLG_CLOSE", LAYOUT, [PROGRESS_ID, 11], []), *self._close(LAYOUT)]
        if self._answer(ev) is not None:
            close: list[Update] = [("DLG_CLOSE", LAYOUT, [CONFIRM_ID, 2], [])]
            if ev["event"] != 1:
                return [*close, *self._close(LAYOUT)]
            self.job += 1
            progress = [PROGRESS_ID, 11, "Busy", self.job, 1, "Progress", "Cancel"]
            return [*close, ("DLG_OPEN", LAYOUT, progress, [])]
        return [("UPDATE", LAYOUT, [], [])]

    # -- 1516 and 1517 ----------------------------------------------------------------
    def _r32_parts(self) -> list[tuple[int, str, Any]]:
        return [
            (2, "TEXT", self.ports[0]),
            (6, "TEXT", self.ports[1]),
            (4, "ENABLED", int(bool(self.ports[0]))),
            (8, "ENABLED", int(bool(self.ports[1]))),
            (9, "ENABLED", 0),
            (10, "ENABLED", 0),
            (15, "CHECK_STATE", 1),
        ]

    def _r32_event(self, ev: dict[str, Any]) -> list[Update]:
        part = ev["part"]
        dialog = self._answer(ev)
        if dialog is not None:
            close: list[Update] = [("DLG_CLOSE", R32, [CONFIRM_ID, 2], [])]
            if ev["event"] == 1:
                self.ports[dialog[1]] = ""
            return [*close, ("UPDATE", R32, [], self._r32_parts())]
        if part in {9, 10}:
            msg = "the R32 output test was pressed"
            raise RuntimeError(msg)
        if part in {4, 8}:
            port = 0 if part == 4 else 1
            if not self.ports[port]:
                self._hang(R32, part)
            self.sv_dialog = ("clear", port)
            return [("DLG_OPEN", R32, _confirm("Delete?"), [])]
        if part in {3, 7}:
            self.edit = {"port": 0 if part == 3 else 1, "sel": None}
            self.open_screens.add(PORTS)
            parts = [(2, "LIST_ROW", _rows(PORT_ROWS)), (3, "ENABLED", 0)]
            return [("OPEN", PORTS, [-1], parts)]
        if part == 11:
            return self._close(R32)
        return [("UPDATE", R32, [], [])]

    def _ports_event(self, ev: dict[str, Any]) -> list[Update]:
        part, e = ev["part"], self.edit
        dialog = self._answer(ev)
        if dialog is not None:
            close: list[Update] = [("DLG_CLOSE", PORTS, [CONFIRM_ID, 2], [])]
            if ev["event"] != 1:
                return close
            self.ports[e["port"]] = PORT_ROWS[e["sel"]][1]
            return [*close, *self._close(PORTS), ("UPDATE", R32, [], self._r32_parts())]
        if part == 2 and ev["dtype"] == 1:
            e["sel"] = ev["number"]
            return [("UPDATE", PORTS, [], [(3, "ENABLED", 1)])]
        if part == 3:
            if e["sel"] is None:
                self._hang(PORTS, part)
            self.sv_dialog = ("save",)
            return [("DLG_OPEN", PORTS, _confirm(), [])]
        if part == 4:
            return self._close(PORTS)
        return [("UPDATE", PORTS, [], [])]

    # -- 1900 -------------------------------------------------------------------------
    def _password_parts(self) -> list[tuple[int, str, Any]]:
        parts: list[tuple[int, str, Any]] = []
        for kind, (on, off, change) in (
            ("administrator", (2, 3, 4)),
            ("screen", (6, 7, 8)),
        ):
            enabled = self.enabled[kind]
            parts += [
                (on, "CHECK_STATE", int(enabled)),
                (off, "CHECK_STATE", int(not enabled)),
                (change, "ENABLED", int(enabled)),
            ]
        return parts

    def _password_event(self, ev: dict[str, Any]) -> list[Update]:
        part, event = ev["part"], ev["event"]
        kinds = {2: "administrator", 3: "administrator", 6: "screen", 7: "screen"}
        dialog = self._answer(ev)
        if dialog is not None:
            return self._password_answer(dialog, ev)
        if part in kinds:
            self.sv_dialog = ("switch", kinds[part], part in {2, 6})
            return [("DLG_OPEN", PASSWORDS, _confirm(), [])]
        if part in {4, 8}:
            kind = "administrator" if part == 4 else "screen"
            if not self.enabled[kind]:
                self._hang(PASSWORDS, part)
            self.sv_dialog = ("new", kind)
            return [("DLG_OPEN", PASSWORDS, _password_dialog(PASSWORD_ID), [])]
        if part == 9 and event == 1:
            return self._close(PASSWORDS)
        return [("UPDATE", PASSWORDS, [], [])]

    def _password_answer(
        self, dialog: tuple[Any, ...], ev: dict[str, Any]
    ) -> list[Update]:
        part = ev["part"]
        close: list[Update] = [("DLG_CLOSE", PASSWORDS, [part, 2], [])]
        if ev["event"] != 1:
            return close
        if dialog[0] == "switch":
            _, kind, on = dialog
            if on and self.pw_values[kind] is None:
                self.sv_dialog = ("new", kind, True)
                return [
                    *close,
                    ("DLG_OPEN", PASSWORDS, _password_dialog(PASSWORD_ID), []),
                ]
            self.enabled[kind] = on
            return [*close, ("UPDATE", PASSWORDS, [], self._password_parts())]
        if dialog[0] == "new":
            self.sv_dialog = ("repeat", dialog[1], ev["string"], *dialog[2:])
            return [*close, ("DLG_OPEN", PASSWORDS, _password_dialog(REPEAT_ID), [])]
        _, kind, first, *enable = dialog
        if ev["string"] != first:
            error = [ERROR_ID, 1, "Passwords differ", "Error", "OK"]
            return [*close, ("DLG_OPEN", PASSWORDS, error, [])]
        self.pw_values[kind] = first
        if enable:
            self.enabled[kind] = True
        return [*close, ("UPDATE", PASSWORDS, [], self._password_parts())]

    # -- encoding: DATE, HOURMIN and DLG_DOWNLOAD -------------------------------------
    @staticmethod
    def _reply(token: str | None, updates: list[Update]) -> str:
        w = Writer(token or "0" * 32)
        w.int(len(updates))
        for kind, screen, data, parts in updates:
            w.short(SCREEN_UPDATE_CODE[kind])
            w.short(screen)
            if kind == "DLG_OPEN" and data[1] in {9, 10}:  # DATE, HOURMIN
                w.short(data[0])
                w.short(data[1])
                w.int(data[2])
                w.string(data[3])
                count = 9 if data[1] == 9 else 6
                for n in data[4 : 4 + count]:
                    w.short(n)
                if data[1] == 9:
                    w.short(0)
                for text in [*data[4 + count :], *[""] * 13][:13]:
                    w.string(text)
            elif kind == "DLG_DOWNLOAD":  # HH + 5 S
                w.short(data[0])
                w.short(data[1])
                for text in data[2:7]:
                    w.string(text)
            else:
                _encode_data(w, kind, data)
            if kind in {"OPEN", "UPDATE", "CLOSE", "SHOW", "HIDE"}:
                w.int(len(parts))
                for part, ptype, value in parts:
                    w.short(part)
                    w.short(PART_UPDATE_CODE[ptype])
                    _encode_part(w, ptype, value)
        return w.buf + "\0"


__all__ = ["FakeServiceEditITM", "Outdoor", "Program"]
