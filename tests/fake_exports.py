"""A simulated iTM that also calculates PPD and exports files.

PPD (2900) with its date and day inputs and Execute behind a PROGRESS dialog, its
advanced screen (2901) behind a warning, saved with OK + Yes; the history's CSV
output (800 -> 801) and the energy navigator's data output (3219). An export runs
like on the real iTM: a PROGRESS dialog, UPD_PROG until a DLG_DOWNLOAD update with a
"#<name>" URI, the file under /sdlfs/WebExport/, and FINISH_DOWNLOAD [job, reason].
"""

from __future__ import annotations

from typing import Any

from daikin_itm.protocol import PART_UPDATE_CODE, SCREEN_UPDATE_CODE, Writer

from .fake_itm import FakeITM, Update, _enabled, _encode_data, _encode_part

DATE_RANGE = ((2023, 11, 21), (2025, 0, 1))  # [year, month 0-11, day] from, to
PERIODS = ["01/2025-01/2025", "01/2024-12/2024"]
WEEKDAY_CHECKS = (2, 7, 12, 17, 22, 27, 32)
ADVANCED_OPTIONS = ["Operation", "Standby"]
PROGRESS_ID, WARN_ID, SAVE_ID, NUMBER_ID = 19, 31, 40, 25
DATE_IDS = {
    (2900, 6): (23, "start"),
    (2900, 9): (24, "end"),
    (801, 3): (26, "start"),
    (801, 19): (27, "end"),
    (3219, 17): (28, "start"),
    (3219, 20): (29, "end"),
}
EXPORTS = {(801, 6): "history", (3219, 4): "energy", (3219, 21): "points"}
JOB_BUTTONS = {47: (4100, "backup"), 59: (4000, "setup")}  # menu list buttons


def _date_text(value: tuple[int, int, int]) -> str:
    year, month0, day = value
    return f"{day:02d}/{month0 + 1:02d}/{year}"


class FakeExportsITM(FakeITM):  # pylint: disable=too-many-instance-attributes
    """A FakeITM with PPD, its advanced settings and file exports."""

    def __init__(self) -> None:
        """Start like FakeITM, with a PPD period and no export running."""
        super().__init__()
        self.ppd_mode = "period"
        self.dates: dict[tuple[int, str], tuple[int, int, int]] = {
            (screen, key): DATE_RANGE[0] if key == "start" else DATE_RANGE[1]
            for (screen, _), (_, key) in DATE_IDS.items()
        }
        self.ppd_day = 20
        self.ppd_rows = [["AC1", "12.5", "0.3"], ["AC2", "--", ""]]
        self.ppd_calculated: list[tuple[str, Any]] = []
        self.weekdays: list[dict[str, Any]] = [
            {"on": False, "option": 0, "time": "08:00"} for _ in range(7)
        ]
        self.pending_weekdays: dict[int, dict[str, Any]] = {}
        self.dialog_for: dict[int, Any] = {}  # dialog id -> what it answers
        self.period = 0
        self.job = 0
        self.export: dict[str, Any] | None = None
        self.files: dict[str, bytes] = {}
        self.confirming_export: str | None = None
        self.finished: list[tuple[int, int]] = []
        self.download_uri: str | None = None
        self.no_file = False  # an export that ends without a file
        self.backup_on_device = False  # a backup that keeps its file on the iTM
        self.slow_polls = 2  # progress requests before the job ends
        self.no_job = False  # an output button that starts nothing
        self.disabled_parts: set[tuple[int, int]] = set()  # shown as ENABLED 0

    # -- screens ----------------------------------------------------------------------
    def _system_parts(self, target: int) -> list[tuple[int, str, Any]]:
        disabled = [(p, "ENABLED", 0) for sc, p in self.disabled_parts if sc == target]
        return [*self._screen_parts(target), *disabled]

    def _screen_parts(self, target: int) -> list[tuple[int, str, Any]]:
        if target == 2900:
            month = self.ppd_mode == "month"
            return [
                (2, "CHECK_STATE", int(not month)),
                (10, "CHECK_STATE", int(month)),
                (5, "TEXT", _date_text(self.dates[2900, "start"])),
                (8, "TEXT", _date_text(self.dates[2900, "end"])),
                (13, "TEXT", str(self.ppd_day)),
                (14, "ENABLED", int(month)),
                (20, "TEXT", ""),
                (21, "LIST_ROW", {"rows": [], "selected": []}),
            ]
        if target == 3219:
            return [
                (3, "COMBO_STRS", PERIODS),
                (3, "SINGLE_SELIDX", self.period),
                (16, "TEXT", _date_text(self.dates[3219, "start"])),
                (19, "TEXT", _date_text(self.dates[3219, "end"])),
            ]
        return super()._system_parts(target)

    def _menu(self, ev: dict[str, Any]) -> list[Update]:
        screen, part = ev["screen"], ev["part"]
        if screen == 5 and part in JOB_BUTTONS:
            # Backup and setup export: an empty screen and a confirm, then the job.
            target, kind = JOB_BUTTONS[part]
            self.open_screens.add(target)
            self.confirming_export = kind
            confirm = [8, 2, f"{kind}\nIs this correct?", "", "Yes", "No"]
            return [("OPEN", target, [-1], []), ("DLG_OPEN", target, confirm, [])]
        if screen == 800 and part == 13 and 800 in self.open_screens:
            self.open_screens.add(801)
            parts = [
                (2, "TEXT", _date_text(self.dates[801, "start"])),
                (18, "TEXT", _date_text(self.dates[801, "end"])),
            ]
            return [("OPEN", 801, [-1], parts)]
        if (
            screen in {2900, 2901, 801, 3219, 4000, 4100}
            and screen in self.open_screens
        ):
            return self._export_event(ev)
        return super()._menu(ev)

    def _export_event(  # noqa: PLR0911  # pylint: disable=too-many-return-statements,too-many-branches
        self, ev: dict[str, Any]
    ) -> list[Update]:
        screen, part, event = ev["screen"], ev["part"], ev["event"]
        if event == 5:  # UPD_PROG
            return self._progress(screen)
        if event == 9:  # FINISH_DOWNLOAD [job, reason]
            return self._finish(screen, ev["array"])
        if event == 8:  # a tab
            return [("UPDATE", screen, [], [])]
        if part in self.dialog_for:
            return self._answer(screen, part, ev)
        if (screen, part) in DATE_IDS:
            return self._date_dialog(screen, part)
        if part == 8 and screen in {801, 4000, 4100} and self.confirming_export:
            kind, self.confirming_export = self.confirming_export, None
            close: list[Update] = [("DLG_CLOSE", screen, [8, 2], [])]
            return close + self._start_export(screen, kind) if event == 1 else close
        if (screen, part) in EXPORTS:
            if screen == 801:  # the history output asks first, like the real iTM
                self.confirming_export = EXPORTS[screen, part]
                confirm = [8, 2, "History Output\nIs this correct?", "", "Yes", "No"]
                return [("DLG_OPEN", screen, confirm, [])]
            return self._start_export(screen, EXPORTS[screen, part])
        if screen == 2900:
            return self._ppd_event(part)
        if screen == 2901:
            return self._advanced_event(part, ev)
        if screen == 3219 and part == 3:
            self.period = ev["number"]
            return [("UPDATE", 3219, [], [])]
        if (screen, part) in {(801, 7), (3219, 5), (2901, 38)}:
            self.pending_weekdays = {}
            return self._close(screen)
        return [("UPDATE", screen, [], [])]

    def _close(self, screen: int) -> list[Update]:
        self.open_screens.discard(screen)
        return [("CLOSE", screen, [], [])]

    # -- inputs -----------------------------------------------------------------------
    def _date_dialog(self, screen: int, part: int) -> list[Update]:
        dialog_id, key = DATE_IDS[screen, part]
        self.dialog_for[dialog_id] = ("date", screen, key)
        current = self.dates[screen, key]
        data = [dialog_id, 9, 6, key, *current, *DATE_RANGE[0], *DATE_RANGE[1]]
        data += ["OK", "Cancel", *[""] * 11]
        return [("DLG_OPEN", screen, data, [])]

    def _answer(self, screen: int, part: int, ev: dict[str, Any]) -> list[Update]:
        kind, *what = self.dialog_for.pop(part)
        close: list[Update] = [("DLG_CLOSE", screen, [part, 9], [])]
        if ev["event"] != 1:
            return close
        if kind == "date":
            self.dates[what[0], what[1]] = (
                ev["array"][0],
                ev["array"][1],
                ev["array"][2],
            )
        elif kind == "day":
            self.ppd_day = ev["number"]
        elif kind == "warning":
            self.open_screens.add(2901)
            return [*close, ("OPEN", 2901, [-1], self._advanced_parts())]
        elif kind == "save":
            for weekday, change in self.pending_weekdays.items():
                self.weekdays[weekday].update(change)
            self.pending_weekdays = {}
            return [*close, *self._close(2901)]
        return close

    # -- PPD --------------------------------------------------------------------------
    def _ppd_event(self, part: int) -> list[Update]:
        if part in {2, 10}:
            self.ppd_mode = "period" if part == 2 else "month"
            month = int(part == 10)
            parts = [
                (2, "CHECK_STATE", 1 - month),
                (10, "CHECK_STATE", month),
                (14, "ENABLED", month),
            ]
            return [("UPDATE", 2900, [], parts)]
        if part == 14:
            if self.ppd_mode != "month":
                msg = "the day input clicked while disabled: the real iTM hangs here"
                raise RuntimeError(msg)
            self.dialog_for[NUMBER_ID] = ("day",)
            data = [NUMBER_ID, 6, 0, "Day", self.ppd_day, 0, 1, 0, 31, 0, 4, 0]
            return [("DLG_OPEN", 2900, [*data, *["x"] * 11], [])]
        if part == 15:
            self.job += 1
            self.export = {"screen": 2900, "kind": "ppd", "polls": 0}
            return [("DLG_OPEN", 2900, self._progress_data(), [])]
        if part == 17:
            self.dialog_for[WARN_ID] = ("warning",)
            data = [WARN_ID, 2, "Exclusion period?", "Confirm", "Yes", "No"]
            return [("DLG_OPEN", 2900, data, [])]
        if part == 22:
            return self._close(2900)
        return [("UPDATE", 2900, [], [])]

    def _advanced_parts(self) -> list[tuple[int, str, Any]]:
        parts: list[tuple[int, str, Any]] = []
        for check, day in zip(WEEKDAY_CHECKS, self.weekdays, strict=True):
            parts += [
                *([(check, "CHECK_STATE", 1)] if day["on"] else []),
                *_enabled((check + 1, check + 3, check + 4), day["on"]),
                (check + 1, "COMBO_STRS", ADVANCED_OPTIONS),
                (check + 1, "SINGLE_SELIDX", day["option"]),
                (check + 2, "TEXT", day["time"]),
            ]
        rows = [{"text": ["01/01", "Holiday"], "a": [-1, -1], "b": [-1, -1]}]
        parts.append((53, "LIST_ROW", {"rows": rows, "selected": []}))
        return parts

    def _advanced_event(self, part: int, ev: dict[str, Any]) -> list[Update]:
        if part in WEEKDAY_CHECKS:
            change = self.pending_weekdays.setdefault(WEEKDAY_CHECKS.index(part), {})
            change["on"] = ev["number"] == 1
            return [
                (
                    "UPDATE",
                    2901,
                    [],
                    _enabled((part + 1, part + 3, part + 4), change["on"]),
                )
            ]
        if part - 1 in WEEKDAY_CHECKS:
            change = self.pending_weekdays.setdefault(
                WEEKDAY_CHECKS.index(part - 1), {}
            )
            change["option"] = ev["number"]
        elif part == 37:
            self.dialog_for[SAVE_ID] = ("save",)
            data = [SAVE_ID, 2, "Execute setting?", "Confirm", "Yes", "No"]
            return [("DLG_OPEN", 2901, data, [])]
        return [("UPDATE", 2901, [], [])]

    # -- jobs and downloads -----------------------------------------------------------
    def _progress_data(self) -> list[Any]:
        return [PROGRESS_ID, 11, "Busy", self.job, 1, "Progress", "Cancel"]

    def _start_export(self, screen: int, kind: str) -> list[Update]:
        if self.no_job:
            return [("UPDATE", screen, [], [])]
        self.job += 1
        self.export = {"screen": screen, "kind": kind, "polls": 0}
        return [("DLG_OPEN", screen, self._progress_data(), [])]

    def _progress(self, screen: int) -> list[Update]:
        export = self.export
        if export is None or export["screen"] != screen:
            return [("DLG_CLOSE", screen, [PROGRESS_ID, 11], [])]
        export["polls"] += 1
        if export["polls"] < self.slow_polls:
            return [("DLG_UPDATE", screen, [PROGRESS_ID, 11, 50], [])]
        if export["kind"] == "ppd":
            self.export = None
            label = self.ppd_mode
            value = self.ppd_day if label == "month" else self.dates[2900, "start"]
            self.ppd_calculated.append((label, value))
            rows = [{"text": r, "a": [-1] * 3, "b": [-1] * 3} for r in self.ppd_rows]
            parts = [
                (20, "TEXT", "berekend"),
                (21, "LIST_ROW", {"rows": rows, "selected": []}),
            ]
            return [
                ("DLG_CLOSE", screen, [PROGRESS_ID, 11], []),
                ("UPDATE", 2900, [], parts),
            ]
        if self.no_file or (export["kind"] == "backup" and self.backup_on_device):
            self.export = None
            closed: list[Update] = [("DLG_CLOSE", screen, [PROGRESS_ID, 11], [])]
            if screen in {4000, 4100}:
                self.open_screens.discard(screen)
                closed.append(("CLOSE", screen, [], []))
            return closed
        name = f"{export['kind']}_{self.job:04d}.csv"
        self.files[f"/sdlfs/WebExport/{name}"] = f"{export['kind']};data\n".encode()
        # Like the real iTM: the full path (the "#name" form is tested on its own).
        uri = self.download_uri or f"/sdlfs/WebExport/{name}"
        data = [PROGRESS_ID, 11, uri, "", "Ready", "", ""]
        return [("DLG_DOWNLOAD", screen, data, [])]

    def _finish(self, screen: int, array: list[int]) -> list[Update]:
        self.finished.append((array[0], array[1]))
        self.export = None
        closed: list[Update] = [("DLG_CLOSE", screen, [PROGRESS_ID, 11], [])]
        if screen in {4000, 4100}:  # the job's empty screen closes with it
            self.open_screens.discard(screen)
            closed.append(("CLOSE", screen, [], []))
        return closed

    # -- encoding: DLG_DOWNLOAD -------------------------------------------------------
    @staticmethod
    def _reply(token: str | None, updates: list[Update]) -> str:
        w = Writer(token or "0" * 32)
        w.int(len(updates))
        for kind, screen, data, parts in updates:
            w.short(SCREEN_UPDATE_CODE[kind])
            w.short(screen)
            if kind == "DLG_DOWNLOAD":  # HH + 5 S
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
