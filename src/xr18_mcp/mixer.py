"""High-level mixer facade: connection, name cache, guarded writes and undo."""

from __future__ import annotations

import asyncio
import math
import sys
import time
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import model, nodes
from .model import ALL_STRIPS, Strip
from .osc_client import XAIR_PORT, MixerInfo, OscClient, discover
from .safety import Applied, Change, Group, Journal, Limits, NeedsConfirmation, check


class MixerUnavailable(Exception):
    pass


class Mixer:
    def __init__(self, data_dir: Path, host: str | None = None, limits: Limits | None = None, port: int = XAIR_PORT):
        self.data_dir = data_dir
        self.host = host
        self.port = port
        self.limits = limits or Limits()
        self.client: OscClient | None = None
        self.info: MixerInfo | None = None
        self.journal = Journal(data_dir / "logs" / "changes.jsonl")
        self.on_first_connect: Callable[[Mixer], Awaitable[None]] | None = None
        self._connected_once = False
        self._names: dict[Strip, str] = {}
        self._names_at = 0.0
        self._lock = asyncio.Lock()
        self._write_lock = asyncio.Lock()

    # ------------------------------------------------------------- connection

    async def _try(self, host: str) -> bool:
        c = OscClient(host, self.port)
        try:
            await c.open()
            info = await c.info()
        except OSError:  # e.g. "no route to host" when macOS blocks local network access
            c.close()
            return False
        if info:
            self.client, self.info = c, info
            return True
        c.close()
        return False

    async def ensure(self) -> OscClient:
        first = False
        async with self._lock:
            if self.client is None:
                ok = bool(self.host) and await self._try(self.host)
                if not ok:
                    found = await asyncio.to_thread(discover)
                    for m in found:
                        if await self._try(m.ip):
                            ok = True
                            break
                if not ok:
                    where = f"at {self.host} " if self.host else ""
                    hint = (
                        " On macOS, also allow Local Network access for the app running the assistant "
                        "(System Settings > Privacy & Security > Local Network)."
                        if sys.platform == "darwin"
                        else ""
                    )
                    raise MixerUnavailable(
                        f"No XR18 answered {where}and none was found by network discovery. "
                        "Check the mixer is on and this computer is on the same network." + hint
                    )
                first = not self._connected_once
                self._connected_once = True
        if first and self.on_first_connect:
            await self.on_first_connect(self)
        assert self.client
        return self.client

    def drop(self) -> None:
        if self.client:
            self.client.close()
        self.client = None

    async def _fail(self, what: str) -> MixerUnavailable:
        self.drop()
        return MixerUnavailable(f"The mixer stopped answering ({what}). It will be re-discovered on the next call.")

    # ------------------------------------------------------------------ reads

    async def get_raw(self, addresses: list[str], strict: bool = True) -> dict[str, Any]:
        c = await self.ensure()
        res = await c.get_many(addresses)
        out = {a: (r[0] if r else None) for a, r in res.items()}
        missing = [a for a, v in out.items() if v is None]
        if missing and len(missing) == len(out) and await c.info() is None:
            raise await self._fail(f"no reply for {missing[0]}")
        if strict and missing:
            raise model.ParamError(
                f"The mixer does not answer for {', '.join(missing[:5])} - not a valid XR18 address?"
            )
        return out

    async def read_nodes(self, paths: list[str]) -> dict[str, dict[str, str]]:
        """Read /node text for each path; returns {path: {param: text value}}."""
        c = await self.ensure()
        res = await c.node_many(paths)
        if paths and all(v is None for v in res.values()):
            raise await self._fail("no /node reply")
        out: dict[str, dict[str, str]] = {}
        for text in res.values():
            if not text:
                continue
            for path, values in nodes.parse_lines(text).items():
                layout = nodes.layout_of(path)
                out[path] = nodes.named(path, values, layout) if layout else {str(i): v for i, v in enumerate(values)}
        return out

    async def names(self, max_age: float = 2.0) -> dict[Strip, str]:
        if time.monotonic() - self._names_at > max_age:
            raw = await self.get_raw([s.addr("config/name") for s in ALL_STRIPS], strict=False)
            self._names = {s: (raw.get(s.addr("config/name")) or "") for s in ALL_STRIPS}
            self._names_at = time.monotonic()
        return self._names

    async def resolve(self, text: str, allowed: tuple[str, ...] | None = None, fx_means: str = "fxrtn") -> Strip:
        return model.resolve(text, await self.names(), allowed, fx_means)

    async def label(self, s: Strip) -> str:
        return model.label(s, await self.names())

    async def headamp_for(self, s: Strip) -> int | None:
        """Mic preamp number feeding a channel (follows the channel's input source)."""
        if s.kind != "ch":
            return None
        raw = await self.get_raw([s.addr("config/insrc")])
        src = raw[s.addr("config/insrc")]
        return src + 1 if isinstance(src, int) and 0 <= src < 16 else None

    # ----------------------------------------------------------------- writes

    async def apply(self, changes: list[Change], tool: str, summary: str, confirm: bool = False) -> Group | None:
        """Guarded write: read old values, check guardrails, set, read back, journal."""
        if not changes:
            return None
        async with self._write_lock:
            addrs = [c.address for c in changes]
            old = await self.get_raw(addrs)
            for c in changes:
                c.new = _coerce(c.new, old[c.address])
            reasons = check([(c, old[c.address]) for c in changes], self.limits)
            todo = [c for c in changes if not _same(old[c.address], c.new)]
            if reasons and not confirm:
                preview = [Applied(c.address, c.label, old[c.address], c.new).line() for c in todo]
                raise NeedsConfirmation(reasons, preview[:40] + ([f"... and {len(preview) - 40} more"] if len(preview) > 40 else []))
            assert self.client
            await self.client.set_many([(c.address, c.new) for c in todo])
            new = await self.get_raw([c.address for c in todo]) if todo else {}
            applied = [Applied(c.address, c.label, old[c.address], new.get(c.address, c.new)) for c in todo]
            if any(c.address.endswith("/config/name") for c in todo):
                self._names_at = 0.0
            if not applied:
                return Group(0, time.time(), tool, summary + " (no change needed)", [])
            return self.journal.record(tool, summary, applied)

    async def undo(self, steps: int = 1) -> list[Group]:
        groups = list(reversed(self.journal.undoable()))[:steps]
        async with self._write_lock:
            for g in groups:
                await self.ensure()
                assert self.client
                await self.client.set_many([(c.address, c.old) for c in reversed(g.changes)])
                self.journal.mark_undone(g)
            self._names_at = 0.0
        return groups


def _coerce(new: Any, old: Any) -> Any:
    """Match the OSC type the mixer uses for this address (int vs float)."""
    if isinstance(old, float) and isinstance(new, int) and not isinstance(new, bool):
        return float(new)
    if isinstance(old, int) and isinstance(new, float) and new.is_integer():
        return int(new)
    if isinstance(new, bool):
        return int(new)
    return new


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) and math.isnan(b):
            return True
        return abs(a - b) < 1e-6
    return a == b
