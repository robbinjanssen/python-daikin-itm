# pylint: disable=protected-access,redefined-outer-name
"""Zone structure and icons: add, delete, move, members, zone and point icons."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from daikin_itm import ItmCommandError, ReadOnlyError
from daikin_itm.zones_edit import (
    _icon_allowed,
    add_zone,
    delete_zone,
    move_zone,
    set_point_icon,
    set_zone_icon,
    set_zone_member_order,
    set_zone_members,
)

from .fake_zones import FakeZonesITM
from .test_client import FakeClient

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable


@pytest.fixture
def itm() -> FakeZonesITM:
    """Return a simulated iTM whose zone settings edit a zone tree."""
    return FakeZonesITM()


async def run(
    itm: FakeZonesITM,
    flow: Callable[..., Awaitable[bool]],
    *args: Any,
    client: FakeClient | None = None,
    **kwargs: Any,
) -> bool:
    """Run one flow in a fresh session, like the client's wrappers will."""
    c = client or FakeClient(itm)
    async with c._session() as s:
        try:
            return await flow(s, *args, **kwargs)
        finally:
            await s.close_menu()


def names(itm: FakeZonesITM) -> list[str]:
    """Return the zone names in list order."""
    return [z.name for z, _, _ in itm._z_flat()]


async def test_add_and_delete_zone(itm: FakeZonesITM) -> None:
    """Add a zone at the top and below another; delete it only while empty."""
    assert await run(itm, add_zone, "demo group") is True
    assert itm.zone("demo group") in itm.tree.children
    assert await run(itm, add_zone, "sub", parent="demo group") is True
    assert [z.name for z in itm.zone("demo group").children] == ["sub"]
    with pytest.raises(ItmCommandError, match="exists already"):
        await run(itm, add_zone, "demo group")
    with pytest.raises(ItmCommandError, match="1-12 characters"):
        await run(itm, add_zone, "x" * 13)
    with pytest.raises(ItmCommandError, match="not empty"):
        await run(itm, delete_zone, "demo group")
    assert await run(itm, delete_zone, "sub") is True
    assert await run(itm, delete_zone, "demo group") is True
    assert "demo group" not in names(itm)
    with pytest.raises(ItmCommandError, match="root zone"):
        await run(itm, delete_zone, "Top")
    with pytest.raises(ItmCommandError, match="not found once"):
        await run(itm, delete_zone, "Nowhere")
    assert itm.open_screens == set()


async def test_dry_run_and_read_only(itm: FakeZonesITM) -> None:
    """A dry run plans the saving request and changes nothing; read-only refuses."""
    dry = FakeClient(itm, dry_run=True)
    assert await run(itm, add_zone, "demo group", client=dry) is False
    assert dry.planned == [
        "add zone demo group: screen 1600 part 15 event 1 'demo group'"
    ]
    assert "demo group" not in names(itm)
    assert await run(itm, delete_zone, "Zone A", client=dry) is False
    assert "Zone A" in names(itm)
    with pytest.raises(ReadOnlyError):
        await run(itm, add_zone, "demo group", client=FakeClient(itm, read_only=True))


async def test_move_zone(itm: FakeZonesITM) -> None:
    """Move a zone below another; it can't go below itself."""
    assert await run(itm, move_zone, "Zone A", "Building") is True
    assert itm.zone("Zone A") in itm.zone("Building").children
    with pytest.raises(ItmCommandError, match="can't move below"):
        await run(itm, move_zone, "Building", "Zone A")
    assert await run(itm, move_zone, "Zone A", "Top") is True
    assert itm.zone("Zone A") in itm.tree.children
    assert itm.open_screens == set()


async def test_zone_members(itm: FakeZonesITM) -> None:
    """Add and remove management points by name; refuse unknown points."""
    first, second = itm.units[0].name, itm.units[1].name
    assert await run(itm, set_zone_members, "Zone A", (first, second)) is True
    assert itm.zone("Zone A").points == [first, second]
    assert await run(itm, set_zone_members, "Zone A", (second,)) is True
    assert itm.zone("Zone A").points == [second]
    with pytest.raises(ItmCommandError, match="not offered as a member"):
        await run(itm, set_zone_members, "Zone A", ("nope",))
    assert itm.zone("Zone A").points == [second]
    assert await run(itm, set_zone_members, "Zone A", ()) is True
    assert itm.zone("Zone A").points == []
    assert itm.open_screens == set()


async def test_zone_member_order(itm: FakeZonesITM) -> None:
    """Members move up one row at a time until in order; the rest keep theirs."""
    a, b, c = (u.name for u in itm.units[:3])
    assert await run(itm, set_zone_members, "Zone A", (a, b, c)) is True
    assert await run(itm, set_zone_member_order, "Zone A", (c, a)) is True
    assert itm.zone("Zone A").points == [c, a, b]
    with pytest.raises(ItmCommandError, match="not members"):
        await run(itm, set_zone_member_order, "Zone A", ("nope",))
    assert itm.zone("Zone A").points == [c, a, b]
    assert itm.open_screens == set()

    client = FakeClient(itm)
    assert await client.set_zone_member_order("Zone A", (b,)) == (b, c, a)
    client.dry_run = True
    assert await client.set_zone_member_order("Zone A", (a,)) is None
    assert itm.zone("Zone A").points == [b, c, a]


async def test_zone_member_order_with_jump(itm: FakeZonesITM) -> None:
    """Jump moves a member to its row in one step (1602); the default keeps up."""
    a, b, c = (u.name for u in itm.units[:3])
    assert await run(itm, set_zone_members, "Zone A", (a, b, c)) is True
    itm.jump_enabled = True
    assert await run(itm, set_zone_member_order, "Zone A", (c, b), use_jump=True)
    assert itm.zone("Zone A").points == [c, b, a]
    picks = [r["array"] for r in itm.requests if (r["screen"], r["part"]) == (1602, 1)]
    assert picks == [[0], [1]]
    ups = [r for r in itm.requests if (r["screen"], r["part"]) == (1601, 3)]
    assert ups == []
    assert itm.open_screens == set()

    client = FakeClient(itm)
    assert await client.set_zone_member_order("Zone A", (a,), use_jump=True) == (
        a,
        c,
        b,
    )
    client.dry_run = True
    assert await client.set_zone_member_order("Zone A", (b,), use_jump=True) is None
    assert itm.zone("Zone A").points == [a, c, b]
    assert itm.open_screens == set()


async def test_jump_refusals(itm: FakeZonesITM) -> None:
    """Jump disabled (as on a real iTM), 1602 not opening or misplacing: refused."""
    a, b, c = (u.name for u in itm.units[:3])
    assert await run(itm, set_zone_members, "Zone A", (a, b, c)) is True
    with pytest.raises(ItmCommandError, match="has the button disabled"):
        await run(itm, set_zone_member_order, "Zone A", (c,), use_jump=True)
    assert not [r for r in itm.requests if (r["screen"], r["part"]) == (1601, 5)]
    itm.jump_enabled = True
    original = itm._z_order_event

    def misplace(part: int) -> Any:
        if part == 2:
            itm.z_pending["sel1602_1"] += 1
        return original(part)

    itm._z_order_event = misplace  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="Jump did not put"):
        await run(itm, set_zone_member_order, "Zone A", (c,), use_jump=True)
    itm._z_order_event = original  # type: ignore[method-assign]
    original_event = itm._z_members_event

    def open_short(part: int, event: int) -> Any:
        updates = original_event(part, event)
        if part == 5:
            rows: dict[str, list[Any]] = {"rows": [], "selected": []}
            return [("OPEN", 1602, [-1], [(1, "LIST_ROW", rows)])]
        return updates

    itm._z_members_event = open_short  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="lists 0 rows"):
        await run(itm, set_zone_member_order, "Zone A", (c,), use_jump=True)

    def open_nothing(part: int, event: int) -> Any:
        updates = original_event(part, event)
        if part == 5:
            itm.open_screens.discard(1602)
            return [("UPDATE", 1601, [], [])]
        return updates

    itm._z_members_event = open_nothing  # type: ignore[method-assign]
    with pytest.raises(ItmCommandError, match="order screen did not open"):
        await run(itm, set_zone_member_order, "Zone A", (c,), use_jump=True)
    assert itm.zone("Zone A").points == [a, b, c]
    assert itm.open_screens == set()


async def test_zone_and_point_icons(itm: FakeZonesITM) -> None:
    """Zone icons from the zone list; point icons only within their family."""
    assert await run(itm, set_zone_icon, "Zone A", 1004) is True
    assert itm.zone("Zone A").icon == 1004
    with pytest.raises(ItmCommandError, match="not offered"):
        await run(itm, set_zone_icon, "Zone A", 2114)
    unit = itm.units[0].name
    assert await run(itm, set_point_icon, unit, 2102) is True
    assert itm.point_icons[unit] == 2102
    with pytest.raises(ItmCommandError, match="outside the point's family"):
        await run(itm, set_point_icon, unit, 2201)
    assert itm.point_icons[unit] == 2102
    assert itm.open_screens == set()


def test_icon_rules() -> None:
    """A point keeps its icon family and stays recognised by the library."""
    assert _icon_allowed(2114, 2100) is None
    assert _icon_allowed(None, 2100) is not None
    assert "family" in (_icon_allowed(2114, 2601) or "")
    assert "recognises" in (_icon_allowed(2114, 2107) or "")


# -- through the client and the CLI ---------------------------------------------------


async def test_client_zone_structure(itm: FakeZonesITM) -> None:
    """Create, fill, re-icon, move and delete a zone, each read back."""
    c = FakeClient(itm)
    zone = await c.create_zone("demo group")
    assert zone is not None
    assert zone.level == 1
    point = itm.units[0].name
    assert await c.set_zone_members("demo group", [point]) == (point,)
    assert await c.fetch_zone_members("demo group") == (point,)
    assert await c.set_zone_members("demo group", []) == ()
    attrs = await c.set_zone_icon("demo group", 1004)
    assert attrs is not None
    assert attrs.icon == 1004
    moved = await c.move_zone("demo group", "Zone A")
    assert moved is not None
    assert moved.level == 2
    await c.move_zone("demo group", "Top")
    await c.delete_zone("demo group")
    assert "demo group" not in [z.name for z in await c._zone_list()]
    icon = await c.set_point_icon(point, 2100)
    assert icon is not None
    assert icon.icon == 2100
    dry = FakeClient(itm, dry_run=True)
    assert await dry.create_zone("other") is None
    assert "other" not in [z.name for z in await c._zone_list()]
