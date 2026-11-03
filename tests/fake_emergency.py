"""A simulated iTM that also manages its emergency stop programs.

The program list (200), the release screen (201, opened by the main menu bar's
emergency stop indicator while a stop is active), a program's settings (202) and
its point picker (203). Without the dealer option only the built-in program exists
and the edit group is hidden. Strict like the real iTM: clicking a part it reports
disabled raises (the real web server hangs then). Values are generic placeholders.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .fake_itm import FakeITM, Update, _rows

MAIN, RELEASE, SETTINGS, PICKER = 200, 201, 202, 203
NAME_DIALOG, DELETE_DIALOG, OK_CONFIRM = 12, 13, 30
RELEASE_MODES = ["Auto", "Manual"]
STOP_TYPES = ["Stop", "Stop and lock"]
EDIT_PARTS = (8, 9, 10, 11)  # copy, delete, rename, edit
CONFIRM = [2, "Execute setting?", "Confirm", "Yes", "No"]


@dataclass
class Program:
    """An emergency stop program."""

    name: str
    enabled: bool = False
    built_in: bool = False
    inputs: list[str] = field(default_factory=list)
    outputs: list[str] = field(default_factory=list)
    release_mode: int = 0
    stop: int = 0


def _name_dialog(default: str) -> list[Any]:
    data = [NAME_DIALOG, 4, "Program Name", default, 1, 20, 0, 0, 1]
    return [*data, "Words", "OK", "Cancel", *[""] * 11, ""]


class FakeEmergencyITM(FakeITM):  # pylint: disable=too-many-instance-attributes
    """A FakeITM whose emergency stop screens edit and release programs."""

    def __init__(self, *, dealer_option: bool = True) -> None:
        """Start with the built-in program, armed, and no active stop."""
        super().__init__()
        self.dealer_option = dealer_option
        self.programs = [Program("Program 1", enabled=True, built_in=True)]
        self.points = [u.name for u in self.units]
        self.active: list[str] = []
        self.es_selected: int | None = None
        self.es_dialog: str | None = None
        self.es_edit: dict[str, Any] = {}

    def program(self, name: str) -> Program:
        """Return a program by name (test helper)."""
        return next(p for p in self.programs if p.name == name)

    def start_stop(self, name: str) -> None:
        """Make a program's stop active (as its input would; test helper)."""
        self.active.append(name)
        self.emergency_stop_active = True

    # -- routing --------------------------------------------------------------
    def _open_menu_screen(self, target: int) -> list[Update]:
        if target == MAIN:
            self.es_selected = None
            self.es_dialog = None
            return [("OPEN", MAIN, [-1], self._main_parts())]
        return super()._open_menu_screen(target)

    def _event(self, ev: dict[str, Any]) -> list[Update]:
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        if screen == 0 and part == 20 and event == 1:
            return self._open_release()
        if screen in {MAIN, RELEASE, SETTINGS, PICKER} and screen in self.open_screens:
            handler = {
                MAIN: self._main_event,
                RELEASE: self._release_event,
                SETTINGS: self._settings_event,
                PICKER: self._picker_event,
            }[screen]
            return handler(ev)
        return super()._event(ev)

    @staticmethod
    def _hang(screen: int, part: int) -> None:
        msg = f"{screen}/{part} clicked while disabled: the real iTM hangs"
        raise RuntimeError(msg)

    # -- the program list (200) -----------------------------------------------
    def _list(self) -> dict[str, Any]:
        return _rows([["Enable" if p.enabled else "", p.name] for p in self.programs])

    def _main_parts(self) -> list[tuple[int, str, Any]]:
        shown = 1 if self.dealer_option else 0
        return [
            (2, "LIST_ROW", self._list()),
            (4, "ENABLED", 0),
            (5, "ENABLED", 0),
            (4, "CHECK_STATE", 0),
            (5, "CHECK_STATE", 0),
            *[(p, "VISIBLED", shown) for p in range(6, 12)],
            (7, "ENABLED", 1),
            *[(p, "ENABLED", 0) for p in EDIT_PARTS],
        ]

    def _selection_parts(self) -> list[tuple[int, str, Any]]:
        if self.es_selected is None:
            return self._main_parts()
        p = self.programs[self.es_selected]
        editable = 1 if self.dealer_option and not p.built_in else 0
        return [
            (2, "LIST_ROW", self._list()),
            (4, "ENABLED", 1),
            (5, "ENABLED", 1),
            (4, "CHECK_STATE", 1 if p.enabled else 0),
            (5, "CHECK_STATE", 0 if p.enabled else 1),
            *[(part, "ENABLED", editable) for part in EDIT_PARTS],
        ]

    def _selected(self, part: int) -> Program:
        if self.es_selected is None:
            self._hang(MAIN, part)
        assert self.es_selected is not None
        return self.programs[self.es_selected]

    def _main_event(  # noqa: PLR0911, PLR0912  # pylint: disable=too-many-return-statements,too-many-branches
        self, ev: dict[str, Any]
    ) -> list[Update]:
        part, event = ev["part"], ev["event"]
        if part == 2 and ev["dtype"] == 1:  # a row, as an INT
            self.es_selected = ev["number"]
            return [("UPDATE", MAIN, [], self._selection_parts())]
        if part == 1:
            self.open_screens.discard(MAIN)
            return [("CLOSE", MAIN, [], [])]
        if part in {4, 5}:
            program = self._selected(part)
            self.es_dialog = "enable" if part == 4 else "disable"
            self.es_edit = {"program": program}
            return [("DLG_OPEN", MAIN, [OK_CONFIRM, *CONFIRM], [])]
        if part == OK_CONFIRM and self.es_dialog in {"enable", "disable"}:
            if event == 1:
                self.es_edit["program"].enabled = self.es_dialog == "enable"
            self.es_dialog = None
            return [
                ("DLG_CLOSE", MAIN, [OK_CONFIRM, 2], []),
                ("UPDATE", MAIN, [], self._selection_parts()),
            ]
        if part == 7:
            self.es_dialog = "add"
            return [("DLG_OPEN", MAIN, _name_dialog("New Program 01"), [])]
        if part in EDIT_PARTS:
            program = self._selected(part)
            if not self.dealer_option or program.built_in:
                self._hang(MAIN, part)
            if part in {8, 10}:
                self.es_dialog = "copy" if part == 8 else "rename"
                return [("DLG_OPEN", MAIN, _name_dialog(program.name), [])]
            if part == 9:
                self.es_dialog = "delete"
                data = [DELETE_DIALOG, 2, "Delete?", "Confirm", "Yes", "No"]
                return [("DLG_OPEN", MAIN, data, [])]
            if program.enabled:  # Edit: only a read-only mode is offered
                self.es_dialog = "read-only"
                data = [DELETE_DIALOG, 2, "Read only?", "Confirm", "Yes", "No"]
                return [("DLG_OPEN", MAIN, data, [])]
            return self._open_settings(program)
        if part == NAME_DIALOG and self.es_dialog in {"add", "copy", "rename"}:
            return self._name_answer(ev)
        if part == DELETE_DIALOG and self.es_dialog in {"delete", "read-only"}:
            close: Update = ("DLG_CLOSE", MAIN, [DELETE_DIALOG, 2], [])
            if event == 1 and self.es_dialog == "delete":
                self.programs.remove(self._selected(part))
                self.es_selected = None
                self.es_dialog = None
                return [close, ("UPDATE", MAIN, [], self._main_parts())]
            self.es_dialog = None
            return [close]
        return [("UPDATE", MAIN, [], [])]

    def _name_answer(self, ev: dict[str, Any]) -> list[Update]:
        dialog, self.es_dialog = self.es_dialog, None
        close: Update = ("DLG_CLOSE", MAIN, [NAME_DIALOG, 4], [])
        if ev["event"] != 1:
            return [close]
        name = ev["string"]
        if any(p.name == name for p in self.programs):
            return [
                close,
                ("DLG_OPEN", MAIN, [14, 1, "Name exists", "Error", "OK"], []),
            ]
        if dialog == "add":
            self.programs.append(Program(name))
        elif dialog == "rename":
            self._selected(NAME_DIALOG).name = name
        else:
            source = self._selected(NAME_DIALOG)
            self.programs.append(
                Program(
                    name,
                    inputs=list(source.inputs),
                    outputs=list(source.outputs),
                    release_mode=source.release_mode,
                    stop=source.stop,
                )
            )
        return [close, ("UPDATE", MAIN, [], self._selection_parts())]

    # -- the settings (202) and the point picker (203) --------------------------
    def _open_settings(self, program: Program) -> list[Update]:
        self.es_edit = {
            "program": program,
            "inputs": list(program.inputs),
            "outputs": list(program.outputs),
            "release_mode": program.release_mode,
            "stop": program.stop,
        }
        self.open_screens.add(SETTINGS)
        return [("OPEN", SETTINGS, [-1], self._settings_parts())]

    def _settings_parts(self) -> list[tuple[int, str, Any]]:
        e = self.es_edit
        return [
            (3, "LIST_ROW", _rows([["Indoor", n] for n in e["inputs"]])),
            (8, "LIST_ROW", _rows([["Indoor", n] for n in e["outputs"]])),
            (5, "COMBO_STRS", RELEASE_MODES),
            (5, "SINGLE_SELIDX", e["release_mode"]),
            (10, "COMBO_STRS", STOP_TYPES),
            (10, "SINGLE_SELIDX", e["stop"]),
        ]

    def _settings_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements
        self, ev: dict[str, Any]
    ) -> list[Update]:
        part, event, e = ev["part"], ev["event"], self.es_edit
        if part in {5, 10} and ev["dtype"] == 1:
            e["release_mode" if part == 5 else "stop"] = ev["number"]
            return [("UPDATE", SETTINGS, [], [])]
        if part in {2, 7}:
            e["picking"] = "inputs" if part == 2 else "outputs"
            e["picked"] = list(e[e["picking"]])
            e["sel"] = {}
            self.open_screens.add(PICKER)
            return [("OPEN", PICKER, [-1], self._picker_parts())]
        if part == 11:
            self.es_dialog = "save"
            return [("DLG_OPEN", SETTINGS, [OK_CONFIRM, *CONFIRM], [])]
        if part == OK_CONFIRM and self.es_dialog == "save":
            self.es_dialog = None
            if event != 1:
                return [("DLG_CLOSE", SETTINGS, [OK_CONFIRM, 2], [])]
            program = e["program"]
            for key in ("inputs", "outputs", "release_mode", "stop"):
                setattr(program, key, e[key])
            self.open_screens.discard(SETTINGS)
            return [
                ("DLG_CLOSE", SETTINGS, [OK_CONFIRM, 2], []),
                ("CLOSE", SETTINGS, [], []),
            ]
        if part == 12:
            self.open_screens.discard(SETTINGS)
            return [("CLOSE", SETTINGS, [], [])]
        return [("UPDATE", SETTINGS, [], [])]

    def _es_available(self) -> list[str]:
        return [n for n in self.points if n not in self.es_edit["picked"]]

    def _picker_parts(self) -> list[tuple[int, str, Any]]:
        picked = self.es_edit["picked"]
        return [
            (2, "LIST_ROW", _rows([["Indoor", n] for n in picked])),
            (8, "LIST_ROW", _rows([["Indoor", n] for n in self._es_available()])),
            (4, "ENABLED", 0),
            (5, "ENABLED", 0),
        ]

    def _picker_event(self, ev: dict[str, Any]) -> list[Update]:
        part, e = ev["part"], self.es_edit
        if part in {2, 8} and ev["dtype"] == 1:
            e["sel"] = {part: ev["number"]}
            button = 5 if part == 2 else 4
            return [("UPDATE", PICKER, [], [(button, "ENABLED", 1)])]
        if part in {4, 5}:
            source = 8 if part == 4 else 2
            if source not in e["sel"]:
                self._hang(PICKER, part)
            row = e["sel"].pop(source)
            if part == 4:
                e["picked"].append(self._es_available()[row])
            else:
                e["picked"].pop(row)
            return [("UPDATE", PICKER, [], self._picker_parts())]
        if part in {9, 10}:
            self.open_screens.discard(PICKER)
            updates: list[Update] = [("CLOSE", PICKER, [], [])]
            if part == 9:
                e[e["picking"]] = list(e["picked"])
                updates.append(("UPDATE", SETTINGS, [], self._settings_parts()))
            return updates
        return [("UPDATE", PICKER, [], [])]

    # -- the release screen (201) -----------------------------------------------
    def _open_release(self) -> list[Update]:
        if not self.active:
            msg = "the hidden emergency stop indicator was clicked"
            raise RuntimeError(msg)
        self.es_selected = None
        self.open_screens.add(RELEASE)
        parts = [(1, "LIST_ROW", _rows([[n] for n in self.active])), (2, "ENABLED", 0)]
        return [("OPEN", RELEASE, [-1], parts)]

    def _release_event(self, ev: dict[str, Any]) -> list[Update]:
        part, event = ev["part"], ev["event"]
        if part == 1 and ev["dtype"] == 1:
            self.es_selected = ev["number"]
            return [("UPDATE", RELEASE, [], [(2, "ENABLED", 1)])]
        if part == 2:
            if self.es_selected is None:
                self._hang(RELEASE, part)
            self.es_dialog = "release"
            return [
                (
                    "DLG_OPEN",
                    RELEASE,
                    [4, 2, "Release?", "Confirm", "Yes", "No"],
                    [],
                )
            ]
        if part == 4 and self.es_dialog == "release":
            self.es_dialog = None
            close: Update = ("DLG_CLOSE", RELEASE, [4, 2], [])
            if event != 1 or self.es_selected is None:
                return [close]
            self.active.pop(self.es_selected)
            self.es_selected = None
            self.emergency_stop_active = bool(self.active)
            rows = _rows([[n] for n in self.active])
            return [
                close,
                ("UPDATE", RELEASE, [], [(1, "LIST_ROW", rows), (2, "ENABLED", 0)]),
                ("UPDATE", 0, [], [(20, "VISIBLED", 1 if self.active else 0)]),
            ]
        if part == 3:
            self.open_screens.discard(RELEASE)
            return [("CLOSE", RELEASE, [], [])]
        return [("UPDATE", RELEASE, [], [])]


__all__ = ["FakeEmergencyITM", "Program"]
