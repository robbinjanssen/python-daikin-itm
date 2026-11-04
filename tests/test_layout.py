# pylint: disable=protected-access,redefined-outer-name
"""The graphical layout view (screen 3): read only."""

from __future__ import annotations

from typing import Any

import pytest

from daikin_itm import ItmCommandError, Layout, LayoutItem, PointKind
from daikin_itm import cli as itm_cli
from daikin_itm.layout import layout_from_screen
from daikin_itm.protocol import Part, ScreenUpdate
from daikin_itm.state import UIState

from .fake_layout import FakeLayoutItem, FakeLayoutITM
from .test_cli import CliClient
from .test_client import FakeClient


def filled() -> FakeLayoutITM:
    """Return a simulated iTM with a layout of two unit icons and a text."""
    itm = FakeLayoutITM(
        [
            FakeLayoutItem(10, 20, icon_id=2114, mode=0, unit="Unit A"),
            FakeLayoutItem(300, 20, text="Floor 1"),
            FakeLayoutItem(40, 200, icon_id=2201, mode=2, size=2, unit="Vent B"),
        ],
        title="Level 0",
        screens=("Floor 1", "Floor 2"),
    )
    itm.layout_state = [(0, 0, 3), (1, 1, "Floor 1 &amp; hall")]
    return itm


def toggles(itm: FakeLayoutITM) -> list[tuple[int, int]]:
    """Return the main menu bar clicks the client sent."""
    return [(r["screen"], r["part"]) for r in itm.requests if r["screen"] == 0]


async def test_empty_layout() -> None:
    """Without a layout: an empty layout, and the standard view shown again."""
    itm = FakeLayoutITM()
    layout = await FakeClient(itm).fetch_layout()
    assert layout.empty
    assert layout.items == ()
    assert layout.points == ()
    assert layout.screens == ()
    assert layout.title is None
    assert layout.image is None
    assert layout.back_enabled is False
    assert not itm.layout_shown
    assert toggles(itm) == [(0, 2), (0, 1)]
    assert itm.session is None


async def test_filled_layout() -> None:
    """Icons and texts with positions; PNTLAYOUT_STATE applied over the panel."""
    itm = filled()
    layout = await FakeClient(itm).fetch_layout()
    assert not layout.empty
    assert layout.title == "Level 0"
    assert layout.image == "floor.png"
    assert layout.screens == ("Floor 1", "Floor 2")
    first, text, vent = layout.items
    assert first == LayoutItem(
        0,
        10,
        20,
        icon=True,
        icon_id=2114,
        icon_mode=3,
        icon_size=1,
        kind=PointKind.INDOOR,
    )
    assert (text.icon, text.text, text.x, text.y) == (False, "Floor 1 & hall", 300, 20)
    assert text.kind is None
    assert (vent.icon_id, vent.icon_mode, vent.icon_size) == (2201, 2, 2)
    assert vent.kind == PointKind.VENTILATION
    assert layout.points == (first, vent)
    assert all(i.name is None for i in layout.items)
    assert not itm.layout_selected  # nothing selected without names
    assert not itm.layout_shown


async def test_layout_names() -> None:
    """With names, each icon is selected and the operation panel's name read."""
    itm = filled()
    layout = await FakeClient(itm).fetch_layout(names=True)
    assert [i.name for i in layout.items] == ["Unit A", None, "Vent B"]
    assert itm.layout_selected == [0, 2]  # the icons only, by item index
    assert not itm.layout_shown


async def test_layout_names_without_points() -> None:
    """Names on an empty layout select nothing."""
    itm = FakeLayoutITM()
    layout = await FakeClient(itm).fetch_layout(names=True)
    assert layout.empty
    assert not itm.layout_selected


async def test_layout_did_not_open() -> None:
    """A layout view that doesn't open raises, and nothing else is clicked."""
    itm = FakeLayoutITM()
    itm.layout_opens = False
    with pytest.raises(ItmCommandError, match="did not open"):
        await FakeClient(itm).fetch_layout()
    assert toggles(itm) == [(0, 2)]


async def test_standard_view_disabled() -> None:
    """A disabled standard screen button is never clicked: the read raises instead."""
    itm = FakeLayoutITM()
    itm.standard_enabled = 0
    with pytest.raises(ItmCommandError, match="disabled"):
        await FakeClient(itm).fetch_layout()
    assert toggles(itm) == [(0, 2)]
    assert itm.session is None  # the session still ended


def test_layout_state_edge_cases() -> None:
    """State updates out of range or of the other kind are ignored."""
    state = UIState()
    panel = {
        "img": ["", "", 0, 0],
        "items": [[1, 2, 0, 2114, 0, 1], [3, 4, 1, 120, 1, 0, 0, 0, 12, "T"]],
        "selected": -1,
    }
    updates: list[tuple[int, str, Any]] = [(1, "PNTLAYOUT", panel)]
    updates += [(0, "LABEL", "  ")]
    updates += [(1, "PNTLAYOUT_STATE", [[5, 0, 1], [0, 1, "x"], [1, 0, 7]])]
    state.apply([ScreenUpdate(3, "OPEN", [-1], [Part(p, t, v) for p, t, v in updates])])
    layout = layout_from_screen(state)
    assert layout == Layout(
        title=None,
        image=None,
        items=(
            LayoutItem(
                0,
                1,
                2,
                icon=True,
                icon_id=2114,
                icon_mode=0,
                icon_size=1,
                kind=PointKind.INDOOR,
            ),
            LayoutItem(1, 3, 4, icon=False, text="T"),
        ),
    )
    assert layout.back_enabled is None


@pytest.fixture
def cli_layout(monkeypatch: pytest.MonkeyPatch, tmp_path: Any) -> FakeLayoutITM:
    """Route the CLI to a simulator with a filled layout."""
    itm = filled()
    monkeypatch.setattr(CliClient, "itm", itm, raising=False)
    monkeypatch.setattr(CliClient, "sigterm_at", None, raising=False)
    monkeypatch.setattr(itm_cli, "ItmClient", CliClient)
    monkeypatch.setattr(itm_cli, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setenv("ITM_USERNAME", "user")
    monkeypatch.setenv("ITM_PASSWORD", "secret")
    return itm


def test_cli_layout(
    cli_layout: FakeLayoutITM,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`itm layout [--names]`: items with positions; an empty layout says so."""
    itm_cli.main(["--host", "fake-host", "layout"])
    out = capsys.readouterr().out
    assert "layout: Level 0 (floor.png)" in out
    assert "icon 2114 mode 3 size 1 (indoor)" in out
    assert "'Floor 1 & hall'" in out
    assert "linked screens: Floor 1, Floor 2" in out
    assert "Unit A" not in out
    itm_cli.main(["--host", "fake-host", "layout", "--names"])
    assert "(ventilation)  Vent B" in capsys.readouterr().out
    monkeypatch.setattr(CliClient, "itm", FakeLayoutITM(), raising=False)
    itm_cli.main(["--host", "fake-host", "layout"])
    assert capsys.readouterr().out == "layout: -\n  no layout configured\n"
    assert not cli_layout.layout_shown
