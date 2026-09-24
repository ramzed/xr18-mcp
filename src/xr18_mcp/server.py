"""MCP server exposing the Behringer XR18 to an AI assistant."""

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
from .model import ALL_STRIPS, BOOL, COLORS, KINDS, ParamError, Strip
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
Controls a Behringer XR18 digital mixer used for band rehearsals (live sound - changes are heard immediately).

- Start with board_overview to learn channel names and levels. Refer to strips by name ("Guitar", "click")
  or id: ch1-ch16, aux, fxrtn1-4, bus1-6, fxsend1-4, lr (main), dca1-4.
- Levels are dB (faders and sends: -inf..+10). Monitor/in-ear mixes are sends from channels to buses 1-6,
  normally with tap PRE (pre-fader). "More X in the singer's ears" = raise the send from X to the singer's bus.
- Prefer small steps (2-3 dB) and relative changes. Use read_meters / gain_check before touching preamp gain.
- Guardrails: risky actions (phantom power, big level jumps, muting many channels, main LR above 0 dB,
  loading presets or snapshots, system settings) come back as "CONFIRMATION REQUIRED". Ask the user and only
  then repeat the call with confirm=true. Never pass confirm=true without the user's explicit OK.
- Every change is journaled: undo reverts the last change group. The board as it was when this session first
  connected is saved as preset session-start-<time>; load_preset can restore it.
- X AIR Edit or other apps may change the board at the same time; re-read before reasoning about old values.
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


async def _save_preset(name: str, notes: str) -> tuple[Path, int, int]:
    raw = await mixer.get_raw(presets.ALL_LEAVES, strict=False)
    values = {a: v for a, v in raw.items() if v is not None}
    info = mixer.info.__dict__ if mixer.info else None
    path = presets.write(PRESETS_DIR, name, notes, values, info)
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
    lbl = model.label(s, names)
    k = key.strip().lower()
    if k == "mute":
        addr, _ = model.friendly_address(s, "on")
        return Change(addr, 0 if BOOL.to_raw(value) else 1, lbl)
    if k in ("gain", "phantom"):
        n = await mixer.headamp_for(s)
        if n is None:
            raise ParamError(f"{lbl} is not fed by a local mic preamp (gain/phantom exist only for inputs In01-In16)")
        addr, kind = f"/headamp/{n:02d}/{k}", KINDS[f"headamp/{k}"]
        lbl = f"{lbl} preamp {n}"
    else:
        addr, kind = model.friendly_address(s, k, dest)
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
    i = mixer.info
    snap = await c.get("/-snap/index")
    lim = mixer.limits
    undoable = len(mixer.journal.undoable())
    return "\n".join([
        f"Connected to {i.model} '{i.name}' at {i.ip}, firmware {i.firmware}." if i else "Connected.",
        f"Current snapshot index: {snap[0] if snap else '?'}",
        (
            f"Guardrails: confirm needed for level jumps > +{lim.max_raise_db:g} dB ending above {lim.loud_db:g} dB, "
            f"main LR above {lim.lr_max_db:+g} dB, preamp gain jumps > +{lim.max_gain_jump_db:g} dB, phantom power, "
            f"muting more than {lim.max_mutes} channels, presets/snapshots/system settings."
        ),
        f"Changes this session: {len(mixer.journal.groups)} ({undoable} undoable). Data folder: {DATA_DIR}",
    ])


def _src_label(s: Strip, cfg_text: dict[str, str], rtnsw: Any) -> str:
    if rtnsw:
        return cfg_text.get("rtnsrc", "USB") + " (USB)"
    return "Aux in" if s.kind == "aux" else cfg_text.get("insrc", "?")


@tool(RO)
async def board_overview() -> str:
    """Whole-board summary: every input with source, preamp gain/phantom, HPF, fader, mute, LR assign and
    active sends; buses, FX sends and returns, main LR and DCAs. Start here."""
    await mixer.ensure()
    names = await mixer.names(max_age=0)
    inputs = [s for s in ALL_STRIPS if s.kind in ("ch", "aux", "fxrtn")]
    outs = [s for s in ALL_STRIPS if s.kind in ("bus", "fxsend", "lr", "dca")]
    addrs: list[str] = []
    for s in inputs:
        addrs += [s.addr(x) for x in ("mix/on", "mix/fader", "mix/lr", "mix/pan", "eq/on", "preamp/rtnsw")]
        addrs += [s.addr(f"mix/{i:02d}/level") for i in range(1, 11)] + [s.addr(f"mix/{i:02d}/tap") for i in range(1, 7)]
        if s.kind == "ch":
            addrs += [s.addr(x) for x in ("config/insrc", "preamp/hpon", "preamp/hpf", "preamp/invert", "gate/on", "dyn/on")]
    for s in outs:
        addrs += [s.addr(x) for x in (("on", "fader") if s.kind == "dca" else ("mix/on", "mix/fader"))]
        if s.kind == "bus":
            addrs += [s.addr("mix/lr")]
    addrs += [f"/headamp/{n:02d}/{p}" for n in range(1, 17) for p in ("gain", "phantom")]
    addrs += [f"/fx/{n}/type" for n in range(1, 5)] + [f"/config/mute/{n}" for n in range(1, 5)]
    raw = await mixer.get_raw(addrs, strict=False)
    text = await mixer.read_nodes([s.addr("config")[1:] for s in inputs] + [f"fx/{n}" for n in range(1, 5)])

    def v(a: str) -> Any:
        return raw.get(a)

    def db(a: str) -> str:
        r = v(a)
        return "?" if r is None else units.fmt_db(model.FADER.from_raw(r))

    i = mixer.info
    out = [f"{i.model} '{i.name}' at {i.ip} (fw {i.firmware})" if i else "XR18", ""]
    out.append("INPUTS  id: name | source | preamp | HPF | fader | state | sends")
    for s in inputs:
        cfg = text.get(s.addr("config")[1:], {})
        src = _src_label(s, cfg, v(s.addr("preamp/rtnsw"))) if s.kind != "fxrtn" else "FX"
        pre = ""
        if s.kind == "ch":
            m = cfg.get("insrc", "")
            if not v(s.addr("preamp/rtnsw")) and m.startswith("In"):
                n = int(m[2:])
                g, p = v(f"/headamp/{n:02d}/gain"), v(f"/headamp/{n:02d}/phantom")
                pre = f"gain {KINDS['headamp/gain'].from_raw(g):+.1f}{' 48V' if p else ''}" if g is not None else ""
            if v(s.addr("preamp/invert")):
                pre += " inv"
        hpf = ""
        if s.kind == "ch":
            hpf = f"HPF {model.KINDS['preamp/hpf'].from_raw(v(s.addr('preamp/hpf'))):.0f}Hz" if v(s.addr("preamp/hpon")) else "HPF off"
        state = "on" if v(s.addr("mix/on")) else "MUTED"
        state += "" if v(s.addr("mix/lr")) else " notLR"
        procs = [n for n, a in (("gate", "gate/on"), ("comp", "dyn/on")) if s.kind == "ch" and v(s.addr(a))]
        if procs:
            state += " +" + "+".join(procs)
        sends = []
        for d in model.SEND_DESTS:
            a = s.addr(model.send_suffix(d) + "/level")
            if v(a):
                tap = ""
                if d.kind == "bus":
                    t = v(s.addr(model.send_suffix(d) + "/tap"))
                    tap = f" {model.TAPS[t]}" if isinstance(t, int) and t < len(model.TAPS) else ""
                sends.append(f"{d.id} {db(a)}{tap}")
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
            extra = " | to LR" if v(s.addr("mix/lr")) else ""
        if s.kind == "fxsend":
            fx = text.get(f"fx/{s.index}", {})
            extra = f" | FX{s.index}: {fx.get('type', '?')}"
        out.append(f"  {s.id}: {names.get(s) or '-'} | {db(f_a)} | {'on' if v(on_a) else 'MUTED'}{extra}")
    groups = [str(n) for n in range(1, 5) if v(f"/config/mute/{n}")]
    out.append("")
    out.append(f"Mute groups engaged: {', '.join(groups) if groups else 'none'}")
    return "\n".join(out)


@tool(RO)
async def channel_detail(target: str) -> str:
    """Every setting of one strip (channel, aux, FX return, bus, FX send, LR or DCA) as shown by the mixer:
    config, preamp, gate, compressor, EQ bands, fader, sends, groups."""
    s = await mixer.resolve(target)
    names = await mixer.names()
    paths = [p for p, _ in s.nodes()]
    headamp = await mixer.headamp_for(s)
    if headamp:
        paths.append(model.headamp_path(headamp))
    text = await mixer.read_nodes(paths)
    out = [f"{model.label(s, names)} - {s.title}"]
    for p in paths:
        vals = text.get(p)
        if vals is None:
            continue
        b = s.base[1:]
        rel = p[len(b) + 1 :] if p.startswith(b + "/") else ("" if p == b else p)
        if rel.startswith("mix/") and rel[4:].isdigit():
            n = int(rel[4:])
            dest = Strip("bus", n) if n <= 6 else Strip("fxsend", n - 6)
            rel = f"send -> {model.label(dest, names)}"
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
    """Set the send level from a channel/aux/FX return to a bus (bus1-6, monitor mixes) or FX send
    (fxsend1-4). Give db or delta_db, and optionally the tap point (PRE = pre-fader, usual for monitors)."""
    src = await mixer.resolve(source, model.SEND_SOURCES)
    dst = await mixer.resolve(dest, ("bus", "fxsend"), fx_means="fxsend")
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
    """Compressor on a channel, bus or main LR. threshold -60..0 dB, ratio 1.1..100 (snaps to
    1.1/1.3/1.5/2/2.5/3/4/5/7/10/20/100), knee 0..5, makeup 0..24 dB, attack 0..120 ms,
    release 5..4000 ms, mix 0..100 %."""
    s = await mixer.resolve(target, ("ch", "bus", "lr"))
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
        description="name, color, fader, mute, on, lr, pan, hpf, hpf.on, invert, usb, gain, phantom, eq.on, "
        "eq.<band>.type|freq|gain|q, gate.on|mode|thr|range|attack|hold|release, "
        "comp.on|thr|ratio|knee|gain|attack|hold|release|mix|auto, send.level|tap|pan (needs dest)"
    )
    value: str | float | bool | None = Field(None, description="absolute value (dB, Hz, on/off, label, text)")
    delta: float | None = Field(None, description="relative change instead of value (numeric params)")
    dest: str | None = Field(None, description="send destination bus1-6 / fxsend1-4 (for send.* params)")


@tool(RW)
async def apply_changes(changes: list[ChangeSpec], summary: str = "", confirm: bool = False) -> str:
    """Apply several changes at once as ONE undo step, e.g. rebalancing a monitor mix:
    [{target:'click', param:'send.level', dest:'bus2', delta:3}, {target:'bass', param:'send.level', dest:'bus2', delta:-2}]."""
    built = []
    for c in changes:
        s = await mixer.resolve(c.target)
        dst = await mixer.resolve(c.dest, ("bus", "fxsend"), fx_means="fxsend") if c.dest else None
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
    usb: bool | None = Field(None, description="take the signal from the USB return instead of the local input")
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
        src = await mixer.resolve(src_name, model.SEND_SOURCES)
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
    bank, layout = mtr.BANKS[scope]
    frames = await c.sample_meters(bank, seconds)
    if not frames:
        raise MixerUnavailable("no meter data received")
    names = await mixer.names()
    levels = mtr.analyse(frames, layout)
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
    names = await mixer.names()
    chans = [await mixer.resolve(t, ("ch",)) for t in targets] if targets else [s for s in ALL_STRIPS if s.kind == "ch"]
    frames = await c.sample_meters("/meters/1", seconds)
    if not frames:
        raise MixerUnavailable("no meter data received")
    levels = {lv.strip: lv for lv in mtr.analyse(frames, mtr.BANKS["channels"][1]) if lv.strip}
    raw = await mixer.get_raw([s.addr(x) for s in chans for x in ("config/insrc", "preamp/rtnsw")] +
                              [f"/headamp/{n:02d}/gain" for n in range(1, 17)], strict=False)
    gain_kind = KINDS["headamp/gain"]
    lines = [f"Gain check over {seconds:g}s, target peak {target_peak_db:+.0f} dBFS:"]
    for s in chans:
        lv = levels.get(s)
        lbl = model.label(s, names)
        if lv is None:
            continue
        if not targets and lv.peak < mtr.NO_SIGNAL_DB and not names.get(s):
            continue
        src = raw.get(s.addr("config/insrc"))
        if raw.get(s.addr("preamp/rtnsw")):
            if lv.peak < mtr.NO_SIGNAL_DB:
                lines.append(f"  {lbl}: no signal from the USB return - is the computer playing into it?")
            else:
                lines.append(f"  {lbl}: peak {lv.peak:+.1f} - source is USB return; adjust the level on the computer")
            continue
        if not (isinstance(src, int) and 0 <= src < 16):
            lines.append(f"  {lbl}: peak {lv.peak:+.1f} - not a mic preamp input, no gain suggestion")
            continue
        g = gain_kind.from_raw(raw.get(f"/headamp/{src + 1:02d}/gain") or 0.0)
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
    a = presets.read(PRESETS_DIR, name)
    if other:
        b_values, b_name = presets.read(PRESETS_DIR, other).values, other
    else:
        b_values, b_name = await mixer.get_raw(presets.ALL_LEAVES, strict=False), "current board"
    lines = presets.diff_lines(a.values, b_values, await mixer.names())
    if not lines:
        return f"No differences between '{a.name}' and {b_name}."
    head = f"{len(lines)} differences, '{a.name}' -> {b_name}:"
    more = [f"... and {len(lines) - max_lines} more"] if len(lines) > max_lines else []
    return "\n".join([head, *lines[:max_lines], *more])


@tool(DESTRUCTIVE)
async def load_preset(name: str, confirm: bool = False) -> str:
    """Restore a saved preset to the mixer (only differing parameters are written). Always needs confirm;
    the current board is auto-saved first and the load is one undo step."""
    p = presets.read(PRESETS_DIR, name)
    current = await mixer.get_raw(presets.ALL_LEAVES, strict=False)
    diffs = presets.differences(current, p.values)
    if not diffs:
        return f"The board already matches preset '{p.name}'."
    names = await mixer.names()
    if not confirm:
        lines = presets.diff_lines(current, p.values, names)
        phantom = [ln for ln in lines if "/phantom" in ln or "phantom" in ln]
        reasons = [f"loads preset '{p.name}' (saved {p.saved_at}): {len(diffs)} parameters change"]
        if phantom:
            reasons.append("phantom power changes: " + "; ".join(phantom))
        raise NeedsConfirmation(reasons, lines[:40] + ([f"... and {len(lines) - 40} more"] if len(lines) > 40 else []))
    backup = f"autosave-before-{presets.slug(p.name)}-{time.strftime('%Y%m%d-%H%M%S')}"
    await _save_preset(backup, f"Automatic backup before loading '{p.name}'")
    presets.prune(PRESETS_DIR, "autosave-before-", keep=20)
    changes = [Change(a, p.values[a], presets.param_label(a, names)) for a in diffs]
    g = await mixer.apply(changes, "load_preset", f"load preset {p.name}", confirm=True)
    return f"Loaded preset '{p.name}': {len(g.changes) if g else 0} parameters changed. Previous board saved as '{backup}'."


@tool(RO)
async def snapshot_list() -> str:
    """List the mixer's internal snapshots (slots 1-64) that have a name."""
    raw = await mixer.get_raw([f"/-snap/{i:02d}/name" for i in range(1, 65)] + ["/-snap/index"], strict=False)
    used = [f"{i}: {raw[f'/-snap/{i:02d}/name']}" for i in range(1, 65) if raw.get(f"/-snap/{i:02d}/name")]
    return f"Current snapshot index: {raw.get('/-snap/index')}\n" + ("\n".join(used) if used else "All 64 snapshot slots are empty/unnamed.")


@tool(DESTRUCTIVE)
async def snapshot_save(slot: int, name: str, confirm: bool = False) -> str:
    """Save the current board into the mixer's internal snapshot slot 1-64 with a name (max 12 chars).
    Overwriting a named slot needs confirm."""
    if not 1 <= slot <= 64:
        raise ParamError("slot must be 1-64")
    model.KINDS["config/name"].to_raw(name)
    c = await mixer.ensure()
    addr = f"/-snap/{slot:02d}/name"
    old = (await mixer.get_raw([addr]))[addr]
    if old and not confirm:
        raise NeedsConfirmation([f"slot {slot} already holds snapshot '{old}' - it will be overwritten"])
    c.send("/-snap/name", name)
    c.send("/-snap/save", slot)
    new = (await mixer.get_raw([addr]))[addr]
    return f"Saved snapshot {slot} '{new}'." if new else f"Sent save to slot {slot}, but the slot name reads empty - check in X AIR Edit."


@tool(DESTRUCTIVE)
async def snapshot_load(slot: int, confirm: bool = False) -> str:
    """Load one of the mixer's internal snapshots (changes the whole board). Always needs confirm; the current
    board is auto-saved as a preset first so it can be restored with load_preset."""
    if not 1 <= slot <= 64:
        raise ParamError("slot must be 1-64")
    c = await mixer.ensure()
    addr = f"/-snap/{slot:02d}/name"
    nm = (await mixer.get_raw([addr]))[addr]
    if not nm:
        raise ParamError(f"snapshot slot {slot} is empty")
    if not confirm:
        raise NeedsConfirmation([f"loads mixer snapshot {slot} '{nm}' - the whole board changes"])
    backup = f"autosave-before-snapshot-{slot}-{time.strftime('%Y%m%d-%H%M%S')}"
    await _save_preset(backup, f"Automatic backup before loading snapshot {slot} '{nm}'")
    c.send("/-snap/load", slot)
    index = (await mixer.get_raw(["/-snap/index"]))["/-snap/index"]
    if index != slot:
        return (f"Sent the load for snapshot {slot} '{nm}', but the mixer reports snapshot {index} as current - "
                f"check in X AIR Edit. Previous board saved as preset '{backup}'.")
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
        out += [f"  {c.label}: back to {describe_raw(c.address, c.old)}" for c in g.changes]
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
    if any(address.startswith(p) for p in model.BLOCKED_PREFIXES):
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
        raise ParamError(f"The mixer does not answer for {address}; it is probably not a valid XR18 address.")
    return f"{address} raw={r[0] if r else '?'!r} text={t.strip() if t else '?'}"


@tool(RW)
async def osc_set(address: str, value: str | float | int, confirm: bool = False) -> str:
    """Write a RAW OSC value (normalized 0..1 floats for levels/frequencies, ints for switches/enums, text for
    names). Prefer the dedicated tools. Guardrails still apply; system paths need confirm."""
    _check_blocked(address)
    names = await mixer.names()
    lbl = presets.param_label(address, names)
    return done(await mixer.apply([Change(address, value, lbl)], "osc_set", f"osc {address}", confirm))


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
