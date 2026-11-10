"""MCP server exposing a Behringer XR18 (or, experimentally, an X32/M32) to an AI assistant."""

from __future__ import annotations

import functools
import logging
import os
import sys
import time
from pathlib import Path
from typing import Any, Literal

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

from . import meters as mtr
from . import model, presets, units
from .mixer import Mixer, MixerUnavailable
from .model import BOOL, COLORS, ParamError, Profile, Strip
from .safety import Change, Group, NeedsConfirmation, describe_raw


def _default_data_dir() -> Path:
    # AI apps start the server from arbitrary working directories, so don't use cwd.
    # On macOS, ~/Documents is privacy-protected (writes from AI apps trigger a permission prompt).
    docs = Path.home() / "Documents"
    base = docs if sys.platform == "win32" and docs.is_dir() else Path.home()
    return base / "XR18-MCP"


DATA_DIR = Path(os.environ.get("XR18_DATA_DIR") or _default_data_dir())
PRESETS_DIR = DATA_DIR / "presets"
AUTOSAVE = os.environ.get("XR18_AUTOSAVE", "1") != "0"
SESSION_PREFIX = "session-start-"
MIN_METER_SECONDS = 0.5
MIN_GAIN_CHECK_SECONDS = 2.0

INSTRUCTIONS = """\
Controls a Behringer XR18 digital mixer (X32/M32 consoles: experimental) used for band rehearsals
(live sound - changes are heard immediately).

- Start with board_overview to learn the model, channel names and levels. Refer to strips by name ("Guitar",
  "click") or id. XR18: ch1-16, aux, fxrtn1-4, bus1-6, fxsend1-4, lr (main), dca1-4.
  X32/M32: ch1-32, aux1-8, fxrtn1-8, bus1-16, mtx1-6, lr, mono, dca1-8.
- Levels are dB (faders and sends: -inf..+10). Monitor/in-ear mixes are sends from channels to buses,
  normally with tap PRE (pre-fader). "More X in the singer's ears" = raise the send from X to the singer's bus.
- Prefer small steps (2-3 dB) and relative changes. Use read_meters / gain_check before touching preamp gain.
- Guardrails: risky actions (phantom power, big level jumps, muting many channels, main LR above 0 dB,
  loading presets or snapshots, system settings) come back as "CONFIRMATION REQUIRED". Ask the user and only
  then repeat the call with confirm=true. Never pass confirm=true without the user's explicit OK.
- Every change is journaled: undo reverts the last change group. The board as it was when this session first
  connected is saved as preset session-start-<time>; load_preset can restore it.
- X AIR Edit, X32-Edit or other apps may change the board at the same time; re-read before reasoning about old values.
"""

mcp = MCPServer("xr18", instructions=INSTRUCTIONS)
mixer = Mixer(DATA_DIR, os.environ.get("XR18_IP") or None)

RO = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)
RW = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True, open_world_hint=False)


def tool(annotations: ToolAnnotations):
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            try:
                return await fn(*args, **kwargs)
            except NeedsConfirmation as e:
                raise ToolError(e.message) from None
            except (ParamError, MixerUnavailable) as e:
                raise ToolError(str(e)) from None
            except OSError as e:  # e.g. no permission to write presets, network unreachable
                raise ToolError(f"System error: {e}") from None

        mcp.add_tool(wrapper, annotations=annotations, structured_output=False)
        return fn

    return deco


def done(group: Group | None) -> str:
    if group is None or not group.changes:
        return "Nothing changed - the mixer already had those values."
    lines = [f"Done (change #{group.id}; undo reverts it):"]
    lines += ["  " + c.line() for c in group.changes]
    if not all(c.ok for c in group.changes):
        lines.append("Some values did not reach the mixer (network packets lost). Check them and try again.")
    return "\n".join(lines)


# ------------------------------------------------------------------ helpers


async def _profile() -> Profile:
    """Connect if needed and return the connected mixer's profile."""
    await mixer.ensure()
    return mixer.profile


async def _save_preset(name: str, notes: str) -> tuple[Path, int, int]:
    p = await _profile()
    raw = await mixer.get_raw(p.leaves, strict=False)
    values = {a: v for a, v in raw.items() if v is not None}
    info = mixer.info.__dict__ if mixer.info else None
    path = presets.write(PRESETS_DIR, name, notes, values, info, p)
    return path, len(values), len(raw) - len(values)


async def _on_first_connect(_m: Mixer) -> None:
    if not AUTOSAVE:
        return
    try:
        await _save_preset(SESSION_PREFIX + time.strftime("%Y%m%d-%H%M%S"), "Board state when the AI assistant first connected")
        presets.prune(PRESETS_DIR, SESSION_PREFIX, keep=10)
    except Exception:  # a failed backup must not block the tool call that triggered it
        logging.getLogger(__name__).warning("session-start autosave failed", exc_info=True)


mixer.on_first_connect = _on_first_connect


async def _change(
    s: Strip, key: str, value: Any = None, delta: float | None = None, dest: Strip | None = None
) -> Change:
    """Build a guarded change from a friendly parameter name."""
    names = await mixer.names()
    p = mixer.profile
    lbl = model.label(s, names)
    k = key.strip().lower()
    if k == "mute":
        addr, _ = p.friendly_address(s, "on")
        return Change(addr, 0 if BOOL.to_raw(value) else 1, lbl)
    if k in ("gain", "phantom"):
        n = await mixer.headamp_for(s)
        if n is None:
            raise ParamError(f"{lbl} is not fed by a local mic preamp (gain/phantom exist only for channels on mic inputs)")
        addr, kind = p.headamp_addr(n, k), p.kinds[f"headamp/{k}"]
        lbl = f"{lbl} {p.headamp_label(n)}"
    else:
        addr, kind = p.friendly_address(s, k, dest)
    if dest is not None:
        lbl = f"{lbl} -> {model.label(dest, names)}"
    if delta is not None:
        old_raw = (await mixer.get_raw([addr]))[addr]
        old = kind.from_raw(old_raw)
        if not isinstance(old, (int, float)) or isinstance(old, bool):
            raise ParamError(f"{key} is not numeric; give an absolute value")
        if old == units.NEG_INF:
            raise ParamError(f"{lbl} {key} is at -inf; give an absolute level (e.g. -20) instead of a relative change")
        value = old + delta
        if isinstance(kind, model.Lin):
            lo, hi = sorted((kind.lo, kind.hi))
            value = units.clamp(value, lo, hi)
    if value is None:
        raise ParamError(f"no value given for {key}")
    return Change(addr, kind.to_raw(value), f"{lbl} {k}")


def _level_args(db: float | str | None, delta_db: float | None) -> tuple[Any, float | None]:
    if (db is None) == (delta_db is None):
        raise ParamError("give exactly one of db (absolute) or delta_db (relative)")
    return db, delta_db


def _ratio(value: float | str) -> str:
    try:
        x = float(value)
    except ValueError:
        raise ParamError(f"ratio must be a number, one of {', '.join(model.RATIOS)}") from None
    return min(model.RATIOS, key=lambda r: abs(float(r) - x))


# ===================================================================== LOOK


@tool(RO)
async def mixer_status() -> str:
    """Connection info: model, name, IP, firmware, guardrail limits and change count."""
    c = await mixer.ensure()
    i, p = mixer.info, mixer.profile
    snap = await c.get(p.snap_index_addr)
    lim = mixer.limits
    undoable = len(mixer.journal.undoable())
    return "\n".join([
        (f"Connected to {i.model} '{i.name}' at {i.ip}, firmware {i.firmware}." if i else "Connected.") + _experimental(p),
        f"{p.snap_index_label}: {snap[0] if snap else '?'}",
        (
            f"Guardrails: confirm needed for level jumps > +{lim.max_raise_db:g} dB ending above {lim.loud_db:g} dB, "
            f"main LR above {lim.lr_max_db:+g} dB, preamp gain jumps > +{lim.max_gain_jump_db:g} dB, phantom power, "
            f"muting more than {lim.max_mutes} channels, presets/snapshots/system settings."
        ),
        f"Changes this session: {len(mixer.journal.groups)} ({undoable} undoable). Data folder: {DATA_DIR}",
    ])


def _experimental(p: Profile) -> str:
    return f" {p.family} support is EXPERIMENTAL (not yet verified on hardware) - double-check results." if p.experimental else ""


@tool(RO)
async def board_overview() -> str:
    """Whole-board summary: every input with source, preamp gain/phantom, HPF, fader, mute, LR assign and
    active sends; buses, FX sends and returns, main LR and DCAs. Start here."""
    p = await _profile()
    names = await mixer.names(max_age=0)
    inputs = p.strips(*p.input_kinds)
    outs = p.strips(*p.output_kinds)
    bus_dests = [d for d in p.send_dests if d.kind == "bus"]
    send_on = "on" in p.send_subs
    addrs: list[str] = []
    for s in inputs:
        addrs += [s.addr(x) for x in ("mix/on", "mix/fader", p.lr_param, "mix/pan", "eq/on")]
        addrs += [s.addr(p.send_leaf(d, "level")) for d in p.send_dests]
        addrs += [s.addr(p.send_leaf(d, "tap")) for d in bus_dests]
        addrs += [s.addr(p.send_leaf(d, "on")) for d in p.send_dests] if send_on else []
        if s.kind == "ch":
            addrs += [s.addr(x) for x in ("preamp/hpon", "preamp/hpf", "preamp/invert", "gate/on", "dyn/on")]
    for s in outs:
        addrs += [s.addr(x) for x in (("on", "fader") if s.kind == "dca" else ("mix/on", "mix/fader"))]
        if s.kind == "bus":
            addrs += [s.addr(p.lr_param)]
    addrs += [f"/config/mute/{n}" for n in range(1, p.mute_groups + 1)]
    raw = await mixer.get_raw(addrs, strict=False)
    raw.update(await mixer.sources(inputs))
    heads = {s: p.headamp_of(s, raw) for s in inputs if not p.uses_usb(s, raw)}
    heads = {s: n for s, n in heads.items() if n is not None}
    raw.update(await mixer.get_raw([p.headamp_addr(n, x) for n in set(heads.values()) for x in ("gain", "phantom")], strict=False))
    fx_nodes = [f"fx/{n}" for n in range(1, p.fx_count + 1)]
    text = await mixer.read_nodes([s.addr("config")[1:] for s in inputs] + fx_nodes)

    def v(a: str) -> Any:
        return raw.get(a)

    def db(a: str) -> str:
        r = v(a)
        return "?" if r is None else units.fmt_db(model.FADER.from_raw(r))

    i = mixer.info
    out = [(f"{i.model} '{i.name}' at {i.ip} (fw {i.firmware})" if i else p.family) + _experimental(p), ""]
    out.append("INPUTS  id: name | source | preamp | HPF | fader | state | sends")
    for s in inputs:
        src = p.source_label(s, text.get(s.addr("config")[1:], {}), raw)
        pre = ""
        if s.kind == "ch":
            n = heads.get(s)
            g = v(p.headamp_addr(n, "gain")) if n is not None else None
            if g is not None:
                pre = f"gain {p.kinds['headamp/gain'].from_raw(g):+.1f}{' 48V' if v(p.headamp_addr(n, 'phantom')) else ''}"
            if v(s.addr("preamp/invert")):
                pre += " inv"
        hpf = ""
        if s.kind == "ch":
            hpf = f"HPF {p.kinds['preamp/hpf'].from_raw(v(s.addr('preamp/hpf'))):.0f}Hz" if v(s.addr("preamp/hpon")) else "HPF off"
        state = "on" if v(s.addr("mix/on")) else "MUTED"
        state += "" if v(s.addr(p.lr_param)) else " notLR"
        procs = [n for n, a in (("gate", "gate/on"), ("comp", "dyn/on")) if s.kind == "ch" and v(s.addr(a))]
        if procs:
            state += " +" + "+".join(procs)
        sends = []
        for d in p.send_dests:
            a = s.addr(p.send_leaf(d, "level"))
            if v(a):
                tap = ""
                if d.kind == "bus":
                    t = v(s.addr(p.send_leaf(d, "tap")))
                    tap = f" {model.TAPS[t]}" if isinstance(t, int) and t < len(model.TAPS) else ""
                off = " off" if send_on and v(s.addr(p.send_leaf(d, "on"))) == 0 else ""
                sends.append(f"{d.id} {db(a)}{tap}{off}")
        name = names.get(s) or "-"
        out.append(
            f"  {s.id}: {name} | {src} | {pre or '-'} | {hpf or '-'} | {db(s.addr('mix/fader'))} | {state} | "
            + (", ".join(sends) or "no sends")
        )
    out.append("")
    out.append("OUTPUTS")
    for s in outs:
        on_a, f_a = (s.addr("on"), s.addr("fader")) if s.kind == "dca" else (s.addr("mix/on"), s.addr("mix/fader"))
        extra = ""
        if s.kind == "bus":
            extra = " | to LR" if v(s.addr(p.lr_param)) else ""
        if s.kind == "fxsend":
            fx = text.get(f"fx/{s.index}", {})
            extra = f" | FX{s.index}: {fx.get('type', '?')}"
        out.append(f"  {s.id}: {names.get(s) or '-'} | {db(f_a)} | {'on' if v(on_a) else 'MUTED'}{extra}")
    if "fxsend" not in p.strip_kinds:
        out.append("FX slots: " + ", ".join(f"{n[3:]}: {text.get(n, {}).get('type', '?')}" for n in fx_nodes))
    groups = [str(n) for n in range(1, p.mute_groups + 1) if v(f"/config/mute/{n}")]
    out.append("")
    out.append(f"Mute groups engaged: {', '.join(groups) if groups else 'none'}")
    return "\n".join(out)


@tool(RO)
async def channel_detail(target: str) -> str:
    """Every setting of one strip (channel, aux, FX return, bus, FX send, LR or DCA) as shown by the mixer:
    config, preamp, gate, compressor, EQ bands, fader, sends, groups."""
    s = await mixer.resolve(target)
    names = await mixer.names()
    prof = mixer.profile
    paths = [p for p, _ in s.nodes()]
    headamp = await mixer.headamp_for(s)
    if headamp is not None:
        paths.append(prof.headamp_path(headamp))
    text = await mixer.read_nodes(paths)
    out = [f"{model.label(s, names)} - {s.title}"]
    for p in paths:
        vals = text.get(p)
        if vals is None:
            continue
        b = s.base[1:]
        rel = p[len(b) + 1 :] if p.startswith(b + "/") else ("" if p == b else p)
        if rel.startswith("mix/") and rel[4:].isdigit():
            rel = f"send -> {model.label(prof.send_dest(s, int(rel[4:])), names)}"
        if "color" in vals and vals["color"].isdigit():
            ci = int(vals["color"])
            vals = {**vals, "color": COLORS[ci] if ci < len(COLORS) else vals["color"]}
        body = " ".join(f"{k}={v.replace('-oo', '-inf')}" for k, v in vals.items())
        out.append(f"  {rel or 'main'}: {body}")
    return "\n".join(out)


@tool(RO)
async def find_channels(query: str = "") -> str:
    """Find strips whose name contains the query (case-insensitive). Empty query lists all named strips."""
    names = await mixer.names(max_age=0)
    q = query.strip().lower()
    hits = [f"{s.id}: {n}" for s, n in names.items() if n and q in n.lower()]
    return "\n".join(hits) if hits else f"No named strip matches {query!r}."


# ====================================================================== MIX

FaderDb = float | Literal["-inf"]


@tool(RW)
async def set_fader(target: str, db: FaderDb | None = None, delta_db: float | None = None, confirm: bool = False) -> str:
    """Set a fader (channel, aux, FX return, bus, FX send, main LR or DCA). Give db (absolute, -inf..+10)
    or delta_db (relative, e.g. -3)."""
    s = await mixer.resolve(target)
    db, delta = _level_args(db, delta_db)
    ch = await _change(s, "fader", db, delta)
    return done(await mixer.apply([ch], "set_fader", f"fader {s.id}", confirm))


@tool(RW)
async def set_mute(targets: list[str], muted: bool = True, confirm: bool = False) -> str:
    """Mute (muted=true) or unmute (muted=false) one or more strips."""
    changes = [await _change(await mixer.resolve(t), "mute", muted) for t in targets]
    return done(await mixer.apply(changes, "set_mute", f"{'mute' if muted else 'unmute'} {', '.join(targets)}", confirm))


@tool(RW)
async def set_send(
    source: str,
    dest: str,
    db: FaderDb | None = None,
    delta_db: float | None = None,
    tap: Literal["IN", "PREEQ", "POSTEQ", "PRE", "POST", "GRP"] | None = None,
    confirm: bool = False,
) -> str:
    """Set the send level from a channel/aux/FX return to a bus (monitor mixes: bus1-6 on XR18, bus1-16 on X32)
    or FX send (fxsend1-4, XR18 only). Give db or delta_db, and optionally the tap point (PRE = pre-fader, usual
    for monitors; on X32 the tap is shared by a bus pair)."""
    p = await _profile()
    src = await mixer.resolve(source, p.send_sources)
    dst = await mixer.resolve(dest, p.send_dest_kinds, fx_means="fxsend")
    changes = []
    if db is not None or delta_db is not None:
        lvl, delta = _level_args(db, delta_db)
        changes.append(await _change(src, "send.level", lvl, delta, dst))
    if tap:
        if dst.kind != "bus":
            raise ParamError("tap can only be set for bus sends")
        changes.append(await _change(src, "send.tap", tap, dest=dst))
    if not changes:
        raise ParamError("nothing to set: give db, delta_db or tap")
    return done(await mixer.apply(changes, "set_send", f"send {src.id} -> {dst.id}", confirm))


@tool(RW)
async def set_eq(
    target: str,
    band: int | None = None,
    type: Literal["LCut", "LShv", "PEQ", "VEQ", "HShv", "HCut"] | None = None,
    freq_hz: float | None = None,
    gain_db: float | None = None,
    q: float | None = None,
    on: bool | None = None,
    confirm: bool = False,
) -> str:
    """Parametric EQ. Channels/aux/FX returns have bands 1-4, buses and LR 1-6. Gain -15..+15 dB,
    freq 20..20000 Hz, Q 0.3..10. on=true/false (without band) switches the whole EQ."""
    s = await mixer.resolve(target)
    changes = []
    if on is not None:
        changes.append(await _change(s, "eq.on", on))
    if band is not None:
        for key, val in (("type", type), ("freq", freq_hz), ("gain", gain_db), ("q", q)):
            if val is not None:
                changes.append(await _change(s, f"eq.{band}.{key}", val))
    elif any(x is not None for x in (type, freq_hz, gain_db, q)):
        raise ParamError("give band (1-4 for inputs, 1-6 for buses/LR) for band settings")
    if not changes:
        raise ParamError("nothing to set")
    return done(await mixer.apply(changes, "set_eq", f"eq {s.id}", confirm))


@tool(RW)
async def set_hpf(target: str, freq_hz: float | None = None, on: bool | None = None) -> str:
    """Channel high-pass (low-cut) filter, 20..400 Hz. Setting freq_hz also switches it on unless on=false."""
    s = await mixer.resolve(target, ("ch",))
    changes = []
    if freq_hz is not None:
        changes.append(await _change(s, "hpf", freq_hz))
        if on is None:
            on = True
    if on is not None:
        changes.append(await _change(s, "hpf.on", on))
    if not changes:
        raise ParamError("give freq_hz and/or on")
    return done(await mixer.apply(changes, "set_hpf", f"hpf {s.id}"))


@tool(RW)
async def set_gate(
    target: str,
    on: bool | None = None,
    mode: Literal["EXP2", "EXP3", "EXP4", "GATE", "DUCK"] | None = None,
    threshold_db: float | None = None,
    range_db: float | None = None,
    attack_ms: float | None = None,
    hold_ms: float | None = None,
    release_ms: float | None = None,
) -> str:
    """Channel noise gate / expander. threshold -80..0 dB, range 3..60 dB, attack 0..120 ms,
    hold 0.02..2000 ms, release 5..4000 ms."""
    s = await mixer.resolve(target, ("ch",))
    params = {"on": on, "mode": mode, "thr": threshold_db, "range": range_db, "attack": attack_ms, "hold": hold_ms, "release": release_ms}
    changes = [await _change(s, f"gate.{k}", v) for k, v in params.items() if v is not None]
    if not changes:
        raise ParamError("nothing to set")
    return done(await mixer.apply(changes, "set_gate", f"gate {s.id}"))


@tool(RW)
async def set_compressor(
    target: str,
    on: bool | None = None,
    threshold_db: float | None = None,
    ratio: float | None = None,
    knee: float | None = None,
    makeup_db: float | None = None,
    attack_ms: float | None = None,
    hold_ms: float | None = None,
    release_ms: float | None = None,
    mix_pct: float | None = None,
    auto: bool | None = None,
    mode: Literal["COMP", "EXP"] | None = None,
    detector: Literal["PEAK", "RMS"] | None = None,
    confirm: bool = False,
) -> str:
    """Compressor on a channel, bus or main LR (X32 also matrix and mono). threshold -60..0 dB, ratio 1.1..100 (snaps to
    1.1/1.3/1.5/2/2.5/3/4/5/7/10/20/100), knee 0..5, makeup 0..24 dB, attack 0..120 ms,
    release 5..4000 ms, mix 0..100 %."""
    s = await mixer.resolve(target, (await _profile()).comp_kinds)
    params: dict[str, Any] = {
        "on": on, "thr": threshold_db, "ratio": _ratio(ratio) if ratio is not None else None, "knee": knee,
        "gain": makeup_db, "attack": attack_ms, "hold": hold_ms, "release": release_ms, "mix": mix_pct,
        "auto": auto, "mode": mode, "det": detector,
    }
    changes = [await _change(s, f"comp.{k}", v) for k, v in params.items() if v is not None]
    if not changes:
        raise ParamError("nothing to set")
    return done(await mixer.apply(changes, "set_compressor", f"comp {s.id}", confirm))


@tool(RW)
async def set_preamp(
    target: str,
    gain_db: float | None = None,
    delta_gain_db: float | None = None,
    phantom: bool | None = None,
    invert: bool | None = None,
    confirm: bool = False,
) -> str:
    """Mic preamp of a channel: gain -12..+60 dB (absolute or delta), 48 V phantom power, polarity invert.
    Phantom changes always need confirmation."""
    s = await mixer.resolve(target, ("ch",))
    changes = []
    if gain_db is not None or delta_gain_db is not None:
        g, d = _level_args(gain_db, delta_gain_db)
        changes.append(await _change(s, "gain", g, d))
    if phantom is not None:
        changes.append(await _change(s, "phantom", phantom))
    if invert is not None:
        changes.append(await _change(s, "invert", invert))
    if not changes:
        raise ParamError("nothing to set")
    return done(await mixer.apply(changes, "set_preamp", f"preamp {s.id}", confirm))


class ChangeSpec(BaseModel):
    target: str = Field(description="strip name or id, e.g. 'Guitar', 'ch3', 'bus2', 'lr'")
    param: str = Field(
        description="name, color, fader, mute, on, lr, pan, hpf, hpf.on, invert, usb (XR18), trim (X32), gain, phantom, eq.on, "
        "eq.<band>.type|freq|gain|q, gate.on|mode|thr|range|attack|hold|release, "
        "comp.on|thr|ratio|knee|gain|attack|hold|release|mix|auto, send.level|tap|pan|on (needs dest; on: X32)"
    )
    value: str | float | bool | None = Field(None, description="absolute value (dB, Hz, on/off, label, text)")
    delta: float | None = Field(None, description="relative change instead of value (numeric params)")
    dest: str | None = Field(None, description="send destination bus / fxsend (for send.* params)")


@tool(RW)
async def apply_changes(changes: list[ChangeSpec], summary: str = "", confirm: bool = False) -> str:
    """Apply several changes at once as ONE undo step, e.g. rebalancing a monitor mix:
    [{target:'click', param:'send.level', dest:'bus2', delta:3}, {target:'bass', param:'send.level', dest:'bus2', delta:-2}]."""
    built = []
    p = await _profile()
    for c in changes:
        s = await mixer.resolve(c.target)
        dst = await mixer.resolve(c.dest, p.send_dest_kinds, fx_means="fxsend") if c.dest else None
        if c.param.lower().startswith("comp.ratio") and c.value is not None:
            c.value = _ratio(c.value)  # type: ignore[arg-type]
        built.append(await _change(s, c.param, c.value, c.delta, dst))
    return done(await mixer.apply(built, "apply_changes", summary or f"{len(built)} changes", confirm))


# ==================================================================== SETUP


class ChannelSetup(BaseModel):
    channel: str = Field(description="channel id or current name, e.g. 'ch3' or '3'")
    name: str | None = Field(None, description="new name, max 12 characters")
    color: str | None = Field(None, description="|".join(COLORS))
    hpf_hz: float | Literal["off"] | None = Field(None, description="high-pass frequency 20..400 Hz, or 'off'")
    gain_db: float | None = Field(None, description="mic preamp gain -12..+60 dB")
    phantom: bool | None = Field(None, description="48 V phantom power (needs confirm)")
    usb: bool | None = Field(None, description="XR18: take the signal from the USB return instead of the local input")
    lr: bool | None = Field(None, description="assign to main LR")
    fader_db: float | None = None
    pan: float | None = Field(None, description="-100 (left) .. 100 (right)")


@tool(RW)
async def setup_channels(channels: list[ChannelSetup], confirm: bool = False) -> str:
    """Set up many input channels from an input list / stage plot in one go (one undo step):
    names, colours, HPF, preamp gain, phantom, USB source, LR assign, fader, pan."""
    built: list[Change] = []
    for c in channels:
        s = await mixer.resolve(c.channel, ("ch", "aux", "fxrtn"))
        if c.name is not None:
            built.append(await _change(s, "name", c.name))
        if c.color is not None:
            built.append(await _change(s, "color", c.color))
        if c.hpf_hz is not None:
            if c.hpf_hz == "off":
                built.append(await _change(s, "hpf.on", False))
            else:
                built += [await _change(s, "hpf", c.hpf_hz), await _change(s, "hpf.on", True)]
        if c.usb is not None:
            built.append(await _change(s, "usb", c.usb))
        if c.gain_db is not None:
            built.append(await _change(s, "gain", c.gain_db))
        if c.phantom is not None:
            built.append(await _change(s, "phantom", c.phantom))
        if c.lr is not None:
            built.append(await _change(s, "lr", c.lr))
        if c.fader_db is not None:
            built.append(await _change(s, "fader", c.fader_db))
        if c.pan is not None:
            built.append(await _change(s, "pan", c.pan))
    return done(await mixer.apply(built, "setup_channels", f"setup {len(channels)} channels", confirm))


@tool(RW)
async def setup_monitor_mix(
    bus: str,
    sends: dict[str, float],
    name: str | None = None,
    color: str | None = None,
    tap: Literal["IN", "PREEQ", "POSTEQ", "PRE", "POST"] = "PRE",
    bus_fader_db: float | None = None,
    confirm: bool = False,
) -> str:
    """Build a monitor / in-ear mix on a bus in one step: sends = {source: dB} (e.g. {'vocal': 0, 'guitar': -6,
    'click': -3}); sources not listed are left unchanged. Sets the tap point (default PRE-fader) for the listed
    sources, and optionally the bus name, colour and master fader."""
    b = await mixer.resolve(bus, ("bus",))
    built: list[Change] = []
    if name is not None:
        built.append(await _change(b, "name", name))
    if color is not None:
        built.append(await _change(b, "color", color))
    for src_name, level in sends.items():
        src = await mixer.resolve(src_name, mixer.profile.send_sources)
        built.append(await _change(src, "send.level", level, dest=b))
        built.append(await _change(src, "send.tap", tap, dest=b))
    if bus_fader_db is not None:
        built.append(await _change(b, "fader", bus_fader_db))
    return done(await mixer.apply(built, "setup_monitor_mix", f"monitor mix {b.id}", confirm))


# =================================================================== LISTEN


@tool(RO)
async def read_meters(
    seconds: float = 3.0, scope: Literal["channels", "inputs"] = "channels", include_silent: bool = False
) -> str:
    """Measure live levels for a few seconds (peak and average dBFS, 0 = clipping). scope 'channels' =
    channel strips, FX returns, buses, main LR; 'inputs' = raw mic/aux/USB inputs. Flags clipping, hot,
    low and silent signals."""
    seconds = units.clamp(seconds, MIN_METER_SECONDS, 30)
    c = await mixer.ensure()
    p = mixer.profile
    if scope not in p.meter_banks:
        raise ParamError(f"scope {scope!r} is not available on the {p.family} yet; use 'channels'")
    bank, layout = p.meter_banks[scope]
    frames = await c.sample_meters(bank, seconds, *p.meter_extra)
    if not frames:
        raise MixerUnavailable("no meter data received")
    names = await mixer.names()
    levels = mtr.analyse(frames, layout, p.decode_meters)
    lines = [f"Levels over {seconds:g}s ({len(frames)} frames), dBFS:"]
    quiet = []
    for lv in levels:
        name = f"{lv.id} ({names[lv.strip]})" if lv.strip and names.get(lv.strip) else lv.id
        if lv.peak < mtr.NO_SIGNAL_DB and not include_silent:
            quiet.append(lv.id)
            continue
        lines.append(f"  {name}: peak {lv.peak:+.1f}, avg {lv.avg:+.1f} - {lv.status}")
    if quiet:
        lines.append(f"  silent: {', '.join(quiet)}")
    return "\n".join(lines)


@tool(RO)
async def gain_check(targets: list[str] | None = None, seconds: float = 8.0, target_peak_db: float = -12.0) -> str:
    """Gain-staging helper. Ask the musicians to play their LOUDEST part first, then run this. Measures
    channel peaks and suggests preamp gain changes to reach target_peak_db (default -12 dBFS). Suggests
    only - apply with set_preamp."""
    seconds = units.clamp(seconds, MIN_GAIN_CHECK_SECONDS, 30)
    c = await mixer.ensure()
    p = mixer.profile
    names = await mixer.names()
    chans = [await mixer.resolve(t, ("ch",)) for t in targets] if targets else p.strips("ch")
    bank, layout = p.meter_banks["channels"]
    frames = await c.sample_meters(bank, seconds, *p.meter_extra)
    if not frames:
        raise MixerUnavailable("no meter data received")
    levels = {lv.strip: lv for lv in mtr.analyse(frames, layout, p.decode_meters) if lv.strip}
    raw = await mixer.sources(chans)
    heads = {s: p.headamp_of(s, raw) for s in chans}
    raw.update(await mixer.get_raw([p.headamp_addr(n, "gain") for n in set(heads.values()) if n is not None], strict=False))
    gain_kind = p.kinds["headamp/gain"]
    lines = [f"Gain check over {seconds:g}s, target peak {target_peak_db:+.0f} dBFS:"]
    for s in chans:
        lv = levels.get(s)
        lbl = model.label(s, names)
        if lv is None:
            continue
        if not targets and lv.peak < mtr.NO_SIGNAL_DB and not names.get(s):
            continue
        if p.uses_usb(s, raw):
            if lv.peak < mtr.NO_SIGNAL_DB:
                lines.append(f"  {lbl}: no signal from the USB return - is the computer playing into it?")
            else:
                lines.append(f"  {lbl}: peak {lv.peak:+.1f} - source is USB return; adjust the level on the computer")
            continue
        n = heads[s]
        if n is None:
            lines.append(f"  {lbl}: peak {lv.peak:+.1f} - not a mic preamp input, no gain suggestion")
            continue
        g = gain_kind.from_raw(raw.get(p.headamp_addr(n, "gain")) or 0.0)
        if lv.peak < mtr.NO_SIGNAL_DB:
            lines.append(f"  {lbl}: NO SIGNAL (gain {g:+.1f}) - check cable, mic, phantom power, or that it is being played")
            continue
        delta = target_peak_db - lv.peak
        new = units.clamp(g + delta, -12, 60)
        if abs(delta) < 3:
            verdict = "OK"
        else:
            verdict = f"suggest gain {g:+.1f} -> {new:+.1f} dB ({delta:+.1f})"
            if delta > mixer.limits.max_gain_jump_db:
                verdict += " - big raise, do it in steps"
        lines.append(f"  {lbl}: peak {lv.peak:+.1f}, gain {g:+.1f} - {verdict}{' (CLIPPING!)' if lv.peak >= mtr.CLIP_DB else ''}")
    return "\n".join(lines)


# ================================================================= REMEMBER


@tool(RW)
async def save_preset(name: str, notes: str = "") -> str:
    """Save the whole board (all channel, bus, FX, routing and preamp settings) to presets/<name>.json.
    Use names like 'song-3' or 'band-X-2026-09'. Overwrites a preset with the same name."""
    path, n, missing = await _save_preset(name, notes)
    return f"Saved preset '{name}' ({n} parameters) to {path}" + (f"; {missing} parameters did not answer" if missing else "")


@tool(RO)
async def list_presets() -> str:
    """List saved board presets (newest first)."""
    items = presets.listing(PRESETS_DIR)
    if not items:
        return "No presets saved yet."
    return "\n".join(f"{p['name']}  ({p['saved_at']}){' - ' + p['notes'] if p['notes'] else ''}" for p in items)


@tool(RO)
async def diff_preset(name: str, other: str | None = None, max_lines: int = 80) -> str:
    """Show what differs between a saved preset and the current board (or another preset given as other).
    Answers 'what changed since ...'."""
    p = await _profile()
    a = presets.read(PRESETS_DIR, name)
    a.check_profile(p)
    if other:
        b = presets.read(PRESETS_DIR, other)
        b.check_profile(p)
        b_values, b_name = b.values, other
    else:
        b_values, b_name = await mixer.get_raw(p.leaves, strict=False), "current board"
    lines = presets.diff_lines(a.values, b_values, await mixer.names(), p)
    if not lines:
        return f"No differences between '{a.name}' and {b_name}."
    head = f"{len(lines)} differences, '{a.name}' -> {b_name}:"
    more = [f"... and {len(lines) - max_lines} more"] if len(lines) > max_lines else []
    return "\n".join([head, *lines[:max_lines], *more])


@tool(DESTRUCTIVE)
async def load_preset(name: str, confirm: bool = False) -> str:
    """Restore a saved preset to the mixer (only differing parameters are written). Always needs confirm;
    the current board is auto-saved first and the load is one undo step."""
    prof = await _profile()
    p = presets.read(PRESETS_DIR, name)
    p.check_profile(prof)
    current = await mixer.get_raw(prof.leaves, strict=False)
    diffs = presets.differences(current, p.values, prof)
    if not diffs:
        return f"The board already matches preset '{p.name}'."
    names = await mixer.names()
    if not confirm:
        lines = presets.diff_lines(current, p.values, names, prof)
        phantom = [ln for ln in lines if "/phantom" in ln or "phantom" in ln]
        reasons = [f"loads preset '{p.name}' (saved {p.saved_at}): {len(diffs)} parameters change"]
        if phantom:
            reasons.append("phantom power changes: " + "; ".join(phantom))
        raise NeedsConfirmation(reasons, lines[:40] + ([f"... and {len(lines) - 40} more"] if len(lines) > 40 else []))
    backup = f"autosave-before-{presets.slug(p.name)}-{time.strftime('%Y%m%d-%H%M%S')}"
    await _save_preset(backup, f"Automatic backup before loading '{p.name}'")
    presets.prune(PRESETS_DIR, "autosave-before-", keep=20)
    changes = [Change(a, p.values[a], presets.param_label(a, names, prof)) for a in diffs]
    g = await mixer.apply(changes, "load_preset", f"load preset {p.name}", confirm=True)
    return f"Loaded preset '{p.name}': {len(g.changes) if g else 0} parameters changed. Previous board saved as '{backup}'."


def _slot(p: Profile, slot: int) -> None:
    if slot not in p.snap_slots:
        raise ParamError(f"slot must be {p.snap_slots[0]}-{p.snap_slots[-1]}")


def _slot_used(p: Profile, slot: int, raw: dict[str, Any]) -> bool:
    used = p.snap_used_addr(slot)
    return bool(raw.get(p.snap_name_addr(slot))) or bool(used and raw.get(used))


async def _read_slot(p: Profile, slot: int) -> tuple[str, bool]:
    """(name, holds data) of a snapshot slot."""
    addrs = [a for a in (p.snap_name_addr(slot), p.snap_used_addr(slot)) if a]
    raw = await mixer.get_raw(addrs, strict=False)
    return raw.get(p.snap_name_addr(slot)) or "", _slot_used(p, slot, raw)


@tool(RO)
async def snapshot_list() -> str:
    """List the mixer's internal snapshots (XR18 slots 1-64, X32 scenes 0-99) that have a name."""
    p = await _profile()
    slots = p.snap_slots
    used_addrs = [a for a in (p.snap_used_addr(i) for i in slots) if a]
    raw = await mixer.get_raw([p.snap_name_addr(i) for i in slots] + used_addrs + [p.snap_index_addr], strict=False)
    used = [f"{i}: {raw.get(p.snap_name_addr(i)) or '(no name)'}" for i in slots if _slot_used(p, i, raw)]
    head = f"{p.snap_index_label}: {raw.get(p.snap_index_addr)}\n"
    return head + ("\n".join(used) if used else f"All {len(slots)} snapshot slots are empty/unnamed.")


@tool(DESTRUCTIVE)
async def snapshot_save(slot: int, name: str, confirm: bool = False) -> str:
    """Save the current board into one of the mixer's internal snapshot slots (XR18 1-64, X32 scenes 0-99)
    with a name (max 12 chars). Overwriting a named slot needs confirm."""
    p = await _profile()
    _slot(p, slot)
    p.kinds["config/name"].to_raw(name)
    c = await mixer.ensure()
    addr = p.snap_name_addr(slot)
    old, used = await _read_slot(p, slot)
    if used and not confirm:
        raise NeedsConfirmation([f"slot {slot} already holds snapshot '{old or '(no name)'}' - it will be overwritten"])
    p.snap_save(c, slot, name)
    new = (await mixer.get_raw([addr]))[addr]
    return f"Saved snapshot {slot} '{new}'." if new else f"Sent save to slot {slot}, but the slot name reads empty - check on the mixer."


@tool(DESTRUCTIVE)
async def snapshot_load(slot: int, confirm: bool = False) -> str:
    """Load one of the mixer's internal snapshots (changes the whole board). Always needs confirm; the current
    board is auto-saved as a preset first so it can be restored with load_preset."""
    p = await _profile()
    _slot(p, slot)
    c = await mixer.ensure()
    nm, used = await _read_slot(p, slot)
    if not used:
        raise ParamError(f"snapshot slot {slot} is empty")
    nm = nm or "(no name)"
    if not confirm:
        raise NeedsConfirmation([f"loads mixer snapshot {slot} '{nm}' - the whole board changes"])
    backup = f"autosave-before-snapshot-{slot}-{time.strftime('%Y%m%d-%H%M%S')}"
    await _save_preset(backup, f"Automatic backup before loading snapshot {slot} '{nm}'")
    p.snap_load(c, slot)
    if not p.snap_index_verifies:
        return (f"Sent the load for snapshot {slot} '{nm}'. This mixer doesn't report which snapshot is loaded, so check"
                f" the console.{p.snap_load_note} Previous board saved as preset '{backup}'.")
    index = (await mixer.get_raw([p.snap_index_addr]))[p.snap_index_addr]
    if index != slot:
        return (f"Sent the load for snapshot {slot} '{nm}', but the mixer reports snapshot {index} as current - "
                f"check on the mixer. Previous board saved as preset '{backup}'.")
    return f"Loaded snapshot {slot} '{nm}'. Previous board saved as preset '{backup}'."


@tool(RW)
async def undo(steps: int = 1) -> str:
    """Revert the last change group(s) made through this assistant (restores the exact previous values)."""
    groups, failed = await mixer.undo(max(1, steps))
    if not groups:
        return "Nothing to undo."
    out = []
    for g in groups:
        out.append(f"Reverted change #{g.id} ({g.summary}):")
        out += [f"  {c.label}: back to {describe_raw(c.address, c.old, c.profile)}" for c in g.changes]
    if failed:
        out.append(f"These did not reach the mixer (network packets lost), check them: {', '.join(failed)}")
    return "\n".join(out)


@tool(RO)
async def change_history(limit: int = 10) -> str:
    """Changes made through this assistant in this session, newest first."""
    groups = list(reversed(mixer.journal.groups))[:limit]
    if not groups:
        return "No changes yet in this session."
    out = []
    for g in groups:
        out.append(f"#{g.id} {time.strftime('%H:%M:%S', time.localtime(g.ts))} {g.summary}{' (undone)' if g.undone else ''}")
        out += [f"    {c.line()}" for c in g.changes[:8]]
        if len(g.changes) > 8:
            out.append(f"    ... and {len(g.changes) - 8} more")
    return "\n".join(out)


# ====================================================================== RAW


def _check_blocked(address: str) -> None:
    if not address.startswith("/"):
        raise ParamError("OSC addresses start with '/', e.g. /ch/01/mix/fader")
    if any(address.startswith(p) for p in mixer.profile.blocked_prefixes):
        raise ParamError(f"{address} is blocked: it holds network settings and Wi-Fi passwords")


@tool(RO)
async def osc_get(address: str) -> str:
    """Read any OSC address (e.g. /ch/01/eq/2/f) - returns the raw value and the mixer's own text readout.
    Escape hatch for settings the other tools don't cover."""
    _check_blocked(address)
    c = await mixer.ensure()
    r = await c.get(address)
    t = await c.node(address[1:])
    if r is None and t is None:
        raise ParamError(f"The mixer does not answer for {address}; it is probably not a valid {mixer.profile.family} address.")
    return f"{address} raw={r[0] if r else '?'!r} text={t.strip() if t else '?'}"


@tool(RW)
async def osc_set(address: str, value: str | float | int, confirm: bool = False) -> str:
    """Write a RAW OSC value (normalized 0..1 floats for levels/frequencies, ints for switches/enums, text for
    names). Prefer the dedicated tools. Guardrails still apply; system paths need confirm."""
    _check_blocked(address)
    names = await mixer.names()
    lbl = presets.param_label(address, names, mixer.profile)
    return done(await mixer.apply([Change(address, value, lbl)], "osc_set", f"osc {address}", confirm))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
