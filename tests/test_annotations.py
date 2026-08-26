"""Every tool declares itself read-only, and that claim is checked.

This server is unusual in the annotation sweep for being uniformly read-only:
all thirteen tools answer a question about Umphrey's McGee shows and none of
them writes anything, anywhere.

That is worth SAYING rather than leaving to be inferred. An unannotated
read-only server and an unannotated server full of delete tools look identical
in the manifest, so a client that wants to be careful has to be careful about
everything, which in practice means being careful about nothing.
"""

from __future__ import annotations

import pytest

from tests.test_tools import FakeVaultReader, _build, _vault_settings


@pytest.fixture
async def tools(stub_settings):
    """The live manifest, not the source. What a client would receive."""
    mcp = _build(_vault_settings(stub_settings), vault_reader=FakeVaultReader())
    return {tool.name: tool for tool in await mcp.list_tools()}


async def test_every_tool_is_annotated(tools):
    assert [name for name, t in tools.items() if t.annotations is None] == []


async def test_every_tool_is_read_only(tools):
    """The whole surface. If a write tool is ever added it fails here first.

    That is the point: the failure is a prompt to classify the new tool
    deliberately, not an obstacle to adding one.
    """
    writes = [n for n, t in tools.items() if t.annotations.readOnlyHint is not True]
    assert writes == []


async def test_nothing_claims_to_be_destructive(tools):
    destructive = [n for n, t in tools.items() if t.annotations.destructiveHint]
    assert destructive == []


async def test_every_tool_declares_an_open_world(tools):
    """Reads reach the vault or the ATU API, so an answer can change between calls.

    Idempotent in the sense that matters here -- calling twice causes no extra
    effect -- but not closed-world, and the two are different claims.
    """
    closed = [n for n, t in tools.items() if t.annotations.openWorldHint is not True]
    assert closed == []


async def test_the_expected_thirteen_are_present(tools):
    """Guards the guard: a fixture that built an empty server would pass everything above."""
    assert len(tools) == 13
    assert "recent_shows" in tools
    assert "jam_chart" in tools
