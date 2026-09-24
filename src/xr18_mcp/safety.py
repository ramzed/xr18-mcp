"""Guardrails for live-board changes, plus the change journal used for undo."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import model, units


class NeedsConfirmation(Exception):
    def __init__(self, reasons: list[str], preview: list[str] | None = None):
        self.reasons = reasons
        self.preview = preview or []
        super().__init__(self.message)

    @property
    def message(self) -> str:
        lines = ["CONFIRMATION REQUIRED - nothing was changed."]
        lines += [f"- {r}" for r in self.reasons]
        if self.preview:
            lines.append("Planned changes:")
            lines += [f"  {p}" for p in self.preview]
        lines.append("Ask the user. If they agree, call the same tool again with confirm=true.")
        return "\n".join(lines)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Limits:
    max_raise_db: float = field(default_factory=lambda: _env_float("XR18_MAX_RAISE_DB", 6.0))
    loud_db: float = field(default_factory=lambda: _env_float("XR18_LOUD_DB", -10.0))
    lr_max_db: float = field(default_factory=lambda: _env_float("XR18_LR_MAX_DB", 0.0))
    max_gain_jump_db: float = field(default_factory=lambda: _env_float("XR18_MAX_GAIN_JUMP_DB", 10.0))
    max_mutes: int = field(default_factory=lambda: int(_env_float("XR18_MAX_MUTES", 4)))


@dataclass
class Change:
    address: str
    new: Any
    label: str


@dataclass
class Applied:
    address: str
    label: str
    old: Any
    new: Any
    ok: bool = True  # False when the read-back shows the write never landed

    def line(self) -> str:
        text = f"{self.label}: {describe_raw(self.address, self.old)} -> {describe_raw(self.address, self.new)}"
        return text if self.ok else text + "  [NOT APPLIED - the mixer still reports the old value]"


def describe_raw(address: str, raw: Any) -> str:
    kind = model.kind_of(address)
    if raw is None:
        return "?"
    if address.endswith("/mix/on") or re.fullmatch(r"/dca/\d/on", address):
        return "on" if raw else "MUTED"
    if kind is None or isinstance(kind, model.Opaque):
        return repr(raw)
    try:
        return kind.fmt(kind.from_raw(raw))
    except Exception:
        return repr(raw)


def _is_level(address: str) -> bool:
    return isinstance(model.kind_of(address), model.Fader)


def check(pairs: list[tuple[Change, Any]], limits: Limits) -> list[str]:
    """Return the reasons a batch of changes needs explicit confirmation."""
    reasons: list[str] = []
    mutes = []
    for ch, old in pairs:
        a = ch.address
        if any(a.startswith(p) for p in model.BLOCKED_PREFIXES):
            raise model.ParamError(f"{a} is blocked (network settings and credentials are never touched)")
        if old == ch.new:
            continue
        if _is_level(a) and isinstance(old, float) and isinstance(ch.new, float):
            o, n = units.fader_to_db(old), units.fader_to_db(ch.new)
            if n > o + limits.max_raise_db and n > limits.loud_db:
                reasons.append(f"{ch.label} jumps up from {units.fmt_db(o)} to {units.fmt_db(n)} dB")
            if a == "/lr/mix/fader" and n > limits.lr_max_db and n > o:
                reasons.append(f"main LR goes above {units.fmt_db(limits.lr_max_db)} dB (to {units.fmt_db(n)} dB)")
        elif a.endswith("/phantom"):
            reasons.append(f"{ch.label}: phantom power {'ON' if ch.new else 'OFF'} (48 V can damage ribbon mics and causes loud pops)")
        elif a.startswith("/headamp/") and a.endswith("/gain") and isinstance(old, float):
            kind = model.KINDS["headamp/gain"]
            o, n = kind.from_raw(old), kind.from_raw(ch.new)
            if n - o > limits.max_gain_jump_db:
                reasons.append(f"{ch.label} jumps from {o:+.1f} to {n:+.1f} dB")
        elif (a.endswith("/mix/on") or re.fullmatch(r"/dca/\d/on", a)) and old == 1 and ch.new == 0:
            mutes.append(ch.label)
            if a == "/lr/mix/on":
                reasons.append("mutes the main LR output")
        elif any(a.startswith(p) for p in model.CONFIRM_PREFIXES):
            reasons.append(f"changes system setting {a}")
    if len(mutes) > limits.max_mutes:
        reasons.append(f"mutes {len(mutes)} channels at once ({', '.join(mutes)})")
    return reasons


# -------------------------------------------------------------------- journal


@dataclass
class Group:
    id: int
    ts: float
    tool: str
    summary: str
    changes: list[Applied]
    undone: bool = False


class Journal:
    def __init__(self, path: Path):
        self.path = path
        self.groups: list[Group] = []
        self._next = 1

    def record(self, tool: str, summary: str, changes: list[Applied]) -> Group:
        g = Group(self._next, time.time(), tool, summary, changes)
        self._next += 1
        self.groups.append(g)
        self._write({"event": "change", **self._as_dict(g)})
        return g

    def undoable(self) -> list[Group]:
        return [g for g in self.groups if not g.undone]

    def mark_undone(self, g: Group) -> None:
        g.undone = True
        self._write({"event": "undo", "id": g.id, "ts": time.time()})

    def _as_dict(self, g: Group) -> dict[str, Any]:
        return {
            "id": g.id,
            "ts": g.ts,
            "tool": g.tool,
            "summary": g.summary,
            "changes": [
                {"address": c.address, "label": c.label, "old": c.old, "new": c.new, "ok": c.ok} for c in g.changes
            ],
        }

    def _write(self, rec: dict[str, Any]) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, default=_json_default) + "\n")
        except OSError:
            pass


def _json_default(o: Any) -> Any:
    if isinstance(o, (bytes, bytearray)):
        return o.hex()
    return str(o)
