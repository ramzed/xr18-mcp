"""Asyncio OSC/UDP client for the XR18 with request/response matching.

Unlike a "send, sleep, read the last message" approach, every reply is matched
to its request by address (or, for /node, by the path echoed in the text), so
reads are never stale, and many reads can be pipelined.
"""

from __future__ import annotations

import asyncio
import socket
import time
from dataclasses import dataclass
from typing import Any

from pythonosc.osc_message import OscMessage
from pythonosc.osc_message_builder import OscMessageBuilder

XAIR_PORT = 10024


def build(address: str, *args: Any) -> bytes:
    b = OscMessageBuilder(address)
    for a in args:
        b.add_arg(a)
    return b.build().dgram


@dataclass
class MixerInfo:
    ip: str
    name: str
    model: str
    firmware: str


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, client: OscClient):
        self.client = client

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.client._on_datagram(data)

    def error_received(self, exc: Exception) -> None:  # e.g. ICMP port unreachable
        pass


class OscClient:
    def __init__(self, host: str, port: int = XAIR_PORT, timeout: float = 0.3, retries: int = 2, window: int = 16):
        self.host, self.port = host, port
        self.timeout, self.retries, self.window = timeout, retries, window
        self._transport: asyncio.DatagramTransport | None = None
        self._pending: dict[str, list[asyncio.Future[list[Any]]]] = {}
        self._meter_queues: list[asyncio.Queue[tuple[str, list[Any]]]] = []

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        self._transport, _ = await loop.create_datagram_endpoint(
            lambda: _Proto(self), remote_addr=(self.host, self.port)
        )

    def close(self) -> None:
        if self._transport:
            self._transport.close()
            self._transport = None

    @property
    def is_open(self) -> bool:
        return self._transport is not None

    # ------------------------------------------------------------- receive

    def _on_datagram(self, data: bytes) -> None:
        try:
            msg = OscMessage(data)
        except Exception:
            return
        address, params = msg.address, list(msg.params)
        if address.startswith("/meters/"):
            for q in self._meter_queues:
                q.put_nowait((address, params))
            return
        key = address
        if address in ("node", "/node") and params and isinstance(params[0], str):
            first = params[0].split(None, 1)[0] if params[0].strip() else ""
            key = "node:" + first.lstrip("/")
        for fut in self._pending.pop(key, []):
            if not fut.done():
                fut.set_result(params)

    # ------------------------------------------------------------- requests

    async def _request(self, key: str, dgram: bytes) -> list[Any] | None:
        assert self._transport, "client not open"
        loop = asyncio.get_running_loop()
        for _ in range(self.retries + 1):
            fut: asyncio.Future[list[Any]] = loop.create_future()
            self._pending.setdefault(key, []).append(fut)
            self._transport.sendto(dgram)
            try:
                return await asyncio.wait_for(fut, self.timeout)
            except asyncio.TimeoutError:
                waiters = self._pending.get(key)
                if waiters and fut in waiters:
                    waiters.remove(fut)
                    if not waiters:
                        del self._pending[key]
        return None

    async def get(self, address: str) -> list[Any] | None:
        return await self._request(address, build(address))

    async def node(self, path: str) -> str | None:
        path = path.lstrip("/")
        r = await self._request("node:" + path, build("/node", path))
        return r[0] if r else None

    async def _many(self, keys: list[str], fn) -> dict[str, Any]:
        sem = asyncio.Semaphore(self.window)

        async def one(k: str) -> tuple[str, Any]:
            async with sem:
                return k, await fn(k)

        return dict(await asyncio.gather(*(one(k) for k in dict.fromkeys(keys))))

    async def get_many(self, addresses: list[str]) -> dict[str, list[Any] | None]:
        return await self._many(addresses, self.get)

    async def node_many(self, paths: list[str]) -> dict[str, str | None]:
        return await self._many([p.lstrip("/") for p in paths], self.node)

    def send(self, address: str, *args: Any) -> None:
        assert self._transport, "client not open"
        self._transport.sendto(build(address, *args))

    async def set_many(self, pairs: list[tuple[str, Any]]) -> None:
        for i, (address, value) in enumerate(pairs):
            self.send(address, value)
            if i % self.window == self.window - 1:
                await asyncio.sleep(0.01)  # don't flood the mixer's UDP buffer
        await asyncio.sleep(0.02)

    async def info(self) -> MixerInfo | None:
        r = await self.get("/xinfo")
        if not r or len(r) < 4:
            return None
        return MixerInfo(*[str(x) for x in r[:4]])

    # --------------------------------------------------------------- meters

    async def sample_meters(self, bank: str, seconds: float, *extra: Any) -> list[bytes]:
        """Subscribe to a meter bank and collect raw blobs for `seconds`."""
        loop = asyncio.get_running_loop()
        q: asyncio.Queue[tuple[str, list[Any]]] = asyncio.Queue()
        self._meter_queues.append(q)
        frames: list[bytes] = []
        try:
            end = loop.time() + seconds
            renew = 0.0
            while (now := loop.time()) < end:
                if now >= renew:
                    self.send("/meters", bank, *extra)
                    renew = now + 5.0  # subscriptions last ~10 s
                try:
                    address, params = await asyncio.wait_for(q.get(), timeout=max(0.01, min(0.5, end - now)))
                except asyncio.TimeoutError:
                    continue
                if address == bank and params and isinstance(params[0], (bytes, bytearray)):
                    frames.append(bytes(params[0]))
        finally:
            self._meter_queues.remove(q)
        return frames


AP_MODE_IP = "192.168.1.1"  # the mixer's own address when computers join its Wi-Fi access point


def _subnet_broadcast() -> str | None:
    """x.y.z.255 for the interface that routes outward (no packets are sent)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # TEST-NET address; connect() only picks a route
            ip = s.getsockname()[0]
    except OSError:
        return None
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip.rsplit(".", 1)[0] + ".255"


def discover(timeout: float = 1.5) -> list[MixerInfo]:
    """Find X-Air mixers on the LAN via /xinfo (blocking).

    Tries the limited broadcast, the local /24 broadcast and the mixer's access-point address;
    the limited broadcast alone can fail on macOS or on networks without a default route.
    """
    found: dict[str, MixerInfo] = {}
    targets = dict.fromkeys(t for t in ("255.255.255.255", _subnet_broadcast(), AP_MODE_IP) if t)
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.2)
        for target in targets:
            try:
                s.sendto(build("/xinfo"), (target, XAIR_PORT))
            except OSError:
                continue
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, (ip, _) = s.recvfrom(4096)
            except (TimeoutError, OSError):
                continue
            try:
                msg = OscMessage(data)
            except Exception:
                continue
            if msg.address == "/xinfo" and len(msg.params) >= 4:
                found[ip] = MixerInfo(ip, *[str(x) for x in list(msg.params)[1:4]])
    return list(found.values())
