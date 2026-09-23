"""A tiny in-process UDP stand-in for the XR18, for tests without hardware."""

from __future__ import annotations

import asyncio
from typing import Any

from pythonosc.osc_message import OscMessage

from xr18_mcp import model
from xr18_mcp.osc_client import build


def default_values() -> dict[str, Any]:
    vals: dict[str, Any] = {}
    for addr, kind in model.LEAF_KINDS.items():
        if isinstance(kind, model.Str):
            vals[addr] = ""
        elif isinstance(kind, (model.Fader, model.Lin)):
            vals[addr] = 0.5
        else:
            vals[addr] = 0
    for i in range(1, 17):
        vals[f"/ch/{i:02d}/config/insrc"] = i - 1
        vals[f"/ch/{i:02d}/mix/fader"] = 0.0
        vals[f"/ch/{i:02d}/mix/on"] = 1
    vals["/lr/mix/fader"] = 0.75
    vals["/lr/mix/on"] = 1
    vals["/-snap/index"] = 1
    return vals


class FakeMixer(asyncio.DatagramProtocol):
    def __init__(self, values: dict[str, Any] | None = None, drop_first: set[str] | None = None):
        self.values = values if values is not None else default_values()
        self.drop_first = set(drop_first or ())
        self.sets: list[tuple[str, Any]] = []
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport):  # type: ignore[override]
        self.transport = transport

    def datagram_received(self, data: bytes, addr) -> None:
        msg = OscMessage(data)
        a, p = msg.address, list(msg.params)
        if a in self.drop_first:  # simulate a lost packet
            self.drop_first.discard(a)
            return
        if a == "/xinfo":
            self.transport.sendto(build("/xinfo", "127.0.0.1", "FAKE-XR18", "XR18", "1.25"), addr)
        elif a == "/node":
            path = "/" + p[0]
            layout = dict(model.all_nodes()).get(p[0])
            if layout:
                vals = " ".join(str(self.values.get(model.leaf(p[0], prm), "?")) for prm in layout)
                self.transport.sendto(build("node", f"{path} {vals}\n"), addr)
        elif p:
            self.values[a] = p[0]
            self.sets.append((a, p[0]))
        elif a in self.values:
            self.transport.sendto(build(a, self.values[a]), addr)


async def start(**kw) -> tuple[FakeMixer, int, asyncio.DatagramTransport]:
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(lambda: FakeMixer(**kw), local_addr=("127.0.0.1", 0))
    port = transport.get_extra_info("sockname")[1]
    return proto, port, transport
