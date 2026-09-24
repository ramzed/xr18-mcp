from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from xr18_mcp import server
from xr18_mcp.mixer import Mixer

from . import fake_mixer


@pytest.fixture
async def fake():
    proto, transport = await fake_mixer.start()
    yield proto
    transport.close()


@pytest.fixture
async def mixer(fake, tmp_path):
    m = Mixer(tmp_path, host="127.0.0.1", port=fake.port, timeout=0.05)
    yield m
    m.drop()


@dataclass
class Rig:
    fake: fake_mixer.FakeMixer
    mixer: Mixer
    dir: Path

    async def call(self, tool: str, **args) -> str:
        result = await server.mcp.call_tool(tool, args)
        return result.content[0].text

    async def fails(self, tool: str, match: str, **args) -> str:
        with pytest.raises(ToolError, match=match) as e:
            await server.mcp.call_tool(tool, args)
        return str(e.value)

    def set(self, address: str, value) -> None:
        self.fake.values[address] = value

    def get(self, address: str):
        return self.fake.values[address]


@pytest.fixture
async def rig(fake, tmp_path, monkeypatch):
    """The MCP server wired to a fake mixer and a temporary data folder."""
    m = Mixer(tmp_path, host="127.0.0.1", port=fake.port, timeout=0.05)
    m.on_first_connect = server._on_first_connect
    monkeypatch.setattr(server, "mixer", m)
    monkeypatch.setattr(server, "DATA_DIR", tmp_path)
    monkeypatch.setattr(server, "PRESETS_DIR", tmp_path / "presets")
    monkeypatch.setattr(server, "AUTOSAVE", False)
    monkeypatch.setattr(server, "MIN_METER_SECONDS", 0.05)
    monkeypatch.setattr(server, "MIN_GAIN_CHECK_SECONDS", 0.05)
    yield Rig(fake, m, tmp_path)
    m.drop()
