"""The placeholder engine used before a "Conecta tu IA" user adds a key."""

from __future__ import annotations

import asyncio

from openjarvis.engine.setup_pending import SETUP_MESSAGE, SetupPendingEngine


def test_explains_how_to_connect_and_stays_healthy():
    engine = SetupPendingEngine("claude-sonnet-5-5")
    out = engine.generate([], model="claude-sonnet-5-5")
    assert out["content"] == SETUP_MESSAGE and "Tu IA" in out["content"]
    assert out["usage"]["prompt_tokens"] == 0
    assert engine.health() and engine.list_models() == ["claude-sonnet-5-5"]

    async def collect():
        return [t async for t in engine.stream([], model="m")]

    assert asyncio.run(collect()) == [SETUP_MESSAGE]
