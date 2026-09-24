"""An in-process UDP stand-in for the XR18, so tests run without hardware.

It answers typed reads, stores typed writes, renders /node text like the real mixer,
streams meter blobs on /meters subscriptions, handles snapshot commands, and can
simulate lost packets.
"""

from __future__ import annotations

import asyncio
import re
import struct
from typing import Any

from pythonosc.osc_message import OscMessage

from xr18_mcp import model
from xr18_mcp.osc_client import build

FX_TYPES = ("HALL", "PLAT", "VRM", "DLY")


def default_values() -> dict[str, Any]:
    """A plausible idle board: inputs at -inf, outputs at 0 dB, nothing sent anywhere."""
    vals: dict[str, Any] = {}
    for addr, kind in model.LEAF_KINDS.items():
        if isinstance(kind, model.Str):
            vals[addr] = ""
        elif isinstance(kind, model.Fader):
            vals[addr] = 0.0
        elif isinstance(kind, model.Lin):
            vals[addr] = 0.5
        else:
            vals[addr] = 0
    for s in model.ALL_STRIPS:
        on = s.addr("on") if s.kind == "dca" else s.addr("mix/on")
        vals[on] = 1
        if s.kind in ("bus", "fxsend", "lr", "dca", "fxrtn"):
            vals[s.addr("fader") if s.kind == "dca" else s.addr("mix/fader")] = 0.75
    for i in range(1, 17):
        vals[f"/ch/{i:02d}/config/insrc"] = i - 1
        vals[f"/ch/{i:02d}/config/rtnsrc"] = i - 1
        vals[f"/ch/{i:02d}/mix/lr"] = 1
        vals[f"/headamp/{i:02d}/gain"] = model.KINDS["headamp/gain"].to_raw(0)
    vals["/-snap/name"] = ""
    vals["/-snap/index"] = 1
    for i in range(1, 65):
        vals[f"/-snap/{i:02d}/name"] = ""
    return vals


def text_value(addr: str, v: Any) -> str:
    """Render a raw value the way the XR18's /node text does (enough for the server's parsing)."""
    if isinstance(v, str):
        return f'"{v}"'
    if addr.endswith("/config/insrc"):
        return f"In{v + 1:02d}" if v < 16 else f"Aux{v}"
    if addr.endswith("/config/rtnsrc"):
        return f"U{v + 1:02d}"
    if re.fullmatch(r"/fx/\d/type", addr):
        return FX_TYPES[v % len(FX_TYPES)]
    kind = model.kind_of(addr)
    if isinstance(kind, model.Fader):
        db = kind.from_raw(v)
        return "-oo" if db == float("-inf") else f"{db:+.1f}"
    if isinstance(kind, model.Bool):
        return "ON" if v else "OFF"
    return str(v)


def meter_blob(levels_db: list[float]) -> bytes:
    return struct.pack("<i", len(levels_db)) + struct.pack(f"<{len(levels_db)}h", *(int(v * 256) for v in levels_db))


class FakeMixer(asyncio.DatagramProtocol):
    def __init__(
        self,
        values: dict[str, Any] | None = None,
        drop_first: set[str] | None = None,
        drop_sets: dict[str, int] | None = None,
        meters: dict[str, list[float]] | None = None,
        answer_xinfo: bool = True,
    ):
        self.values = values if values is not None else default_values()
        self.drop_first = set(drop_first or ())  # first read of these addresses is "lost"
        self.drop_sets = dict(drop_sets or {})  # address -> how many writes to "lose"
        self.meters = meters or {}  # bank -> dB values streamed on subscription
        self.meter_frames = 5
        self.answer_xinfo = answer_xinfo
        self.sets: list[tuple[str, Any]] = []
        self.subscriptions: list[str] = []
        self.snap_loads: list[int] = []
        self.hide_nodes: set[str] = set()  # /node paths that never answer
        self.transport: asyncio.DatagramTransport | None = None
        self.port = 0

    def connection_made(self, transport):  # type: ignore[override]
        self.transport = transport

    def reply(self, addr, address: str, *args: Any) -> None:
        self.transport.sendto(build(address, *args), addr)

    def datagram_received(self, data: bytes, addr) -> None:
        msg = OscMessage(data)
        a, p = msg.address, list(msg.params)
        if a in self.drop_first:
            self.drop_first.discard(a)
            return
        if a == "/xinfo":
            if self.answer_xinfo:
                self.reply(addr, "/xinfo", "127.0.0.1", "FAKE-XR18", "XR18", "1.25")
        elif a == "/node":
            self._node(addr, p[0])
        elif a == "/meters":
            self._subscribe(addr, p[0])
        elif a == "/-snap/save":
            self.values[f"/-snap/{p[0]:02d}/name"] = self.values.get("/-snap/name", "")
            self.values["/-snap/index"] = p[0]
        elif a == "/-snap/load":
            self.snap_loads.append(p[0])
            self.values["/-snap/index"] = p[0]
        elif p:
            if self.drop_sets.get(a):
                self.drop_sets[a] -= 1
                return
            self.values[a] = p[0]
            self.sets.append((a, p[0]))
        elif a in self.values:
            self.reply(addr, a, self.values[a])

    def _node(self, addr, path: str) -> None:
        if path in self.hide_nodes:
            return
        layout = dict(model.all_nodes()).get(path)
        if layout is not None:
            vals = " ".join(text_value(model.leaf(path, prm), self.values.get(model.leaf(path, prm), 0)) for prm in layout)
            self.reply(addr, "node", f"/{path} {vals}\n")
        elif "/" + path in self.values:
            self.reply(addr, "node", f"/{path} {text_value('/' + path, self.values['/' + path])}\n")

    def _subscribe(self, addr, bank: str) -> None:
        self.subscriptions.append(bank)
        levels = self.meters.get(bank)
        if not levels:
            return
        loop = asyncio.get_running_loop()
        for i in range(self.meter_frames):
            loop.call_later(0.005 * i, self.reply, addr, bank, meter_blob(levels))


async def start(**kw) -> tuple[FakeMixer, asyncio.DatagramTransport]:
    loop = asyncio.get_running_loop()
    transport, proto = await loop.create_datagram_endpoint(lambda: FakeMixer(**kw), local_addr=("127.0.0.1", 0))
    proto.port = transport.get_extra_info("sockname")[1]
    return proto, transport
