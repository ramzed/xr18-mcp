"""X32 / M32 profile - EXPERIMENTAL.

Built from the community "Unofficial X32/M32 OSC Remote Protocol" documentation.
Node layouts are checked against a scene file saved by X32-Edit for firmware 4.0
(tests/data/x32_fw4.0.scn uses the same text format as /node replies). Meters,
scene commands and discovery are cross-checked against the protocol document
v4.06 and two open-source X32 clients used on real consoles (Bitfocus Companion's
X32 module, x32-osc); see docs/x32-sources.md. Not yet run on a live console.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from . import meters
from .model import (
    BOOL,
    EQ_BAND,
    FADER,
    FILTER,
    KINDS,
    OPAQUE,
    TAPS,
    Enum,
    Int,
    Layout,
    Lin,
    ParamError,
    Profile,
    Strip,
)

if TYPE_CHECKING:
    from .osc_client import OscClient

PORT = 10023

DYN = (
    "on", "mode", "det", "env", "thr", "ratio", "knee", "mgain", "attack", "hold", "release", "pos", "keysrc",
    "mix", "auto",
)
DYN_MAIN = tuple(p for p in DYN if p != "keysrc")
MIX_IN = ("on", "fader", "st", "pan", "mono", "mlevel")

# /config/routing/IN/<block>: which 8 physical inputs feed channel inputs In01-08, In09-16, ...
# Read as /node text ("AN1-8", "A9-16", "B33-40", "CARD1-8") so the enum order doesn't matter.
ROUTING_TEXT = re.compile(r"^(AN|A|B)(\d+)-\d+$")
ROUTING_BLOCKS = ("1-8", "9-16", "17-24", "25-32")
AES50_BLOCKS = ("1-8", "9-16", "17-24", "25-32", "33-40", "41-48")


def _sends(n: int) -> Layout:
    # Odd sends carry pan, the tap point ("type") for the bus pair and panFollow; even sends only on/level.
    odd = ("on", "level", "pan", "type", "panFollow")
    return [(f"mix/{i:02d}", odd if i % 2 else ("on", "level")) for i in range(1, n + 1)]


def _eq(bands: int) -> Layout:
    return [("eq", ("on",)), *[(f"eq/{b}", EQ_BAND) for b in range(1, bands + 1)]]


def _dyn(params: tuple[str, ...]) -> Layout:
    return [("dyn", params), ("dyn/filter", FILTER), ("insert", ("on", "pos", "sel"))]


def _channels_layout() -> list[tuple[str, Strip | None, list[int]]]:
    out: list[tuple[str, Strip | None, list[int]]] = []
    for kind, n, first in (("ch", 32, 0), ("aux", 8, 32), ("fxrtn", 8, 40), ("bus", 16, 48), ("mtx", 6, 64)):
        out += [(f"{kind}{i}", Strip(kind, i, "x32"), [first + i - 1]) for i in range(1, n + 1)]
    return out


def source_name(v: Any) -> str:
    """Channel input source (/ch/NN/config/source) as shown on the console."""
    if not isinstance(v, int):
        return "?"
    if v == 0:
        return "OFF"
    if v <= 32:
        return f"In{v:02d}"
    if v <= 38:
        return f"Aux{v - 32}"
    if v <= 40:
        return "USB " + "LR"[v - 39]
    if v <= 48:
        return f"FX{(v - 41) // 2 + 1}{'LR'[(v - 41) % 2]}"
    return f"Bus{v - 48:02d}"


class X32Profile(Profile):
    name = "x32"
    family = "X32"
    port = PORT
    experimental = True
    strip_kinds = ("ch", "aux", "fxrtn", "bus", "mtx", "lr", "mono", "dca")
    counts = {"ch": 32, "aux": 8, "fxrtn": 8, "bus": 16, "mtx": 6, "lr": 1, "mono": 1, "dca": 8}
    singles = ("lr", "mono")
    titles = {
        "ch": "Channel {i}", "aux": "Aux in {i}", "fxrtn": "FX return {i}", "bus": "Mix bus {i}",
        "mtx": "Matrix {i}", "lr": "Main LR", "mono": "Main mono (M/C)", "dca": "DCA {i}",
    }
    output_kinds = ("bus", "mtx", "lr", "mono", "dca")
    send_dest_kinds = ("bus",)
    send_subs = ("level", "tap", "pan", "on")
    comp_kinds = ("ch", "bus", "mtx", "lr", "mono")
    lr_param = "mix/st"
    trim_kinds = ("ch", "aux")
    eq_bands = {"ch": 4, "aux": 4, "fxrtn": 4, "bus": 6, "mtx": 6, "lr": 6, "mono": 6}
    kinds = {
        **KINDS,
        "config/icon": OPAQUE,
        "config/source": OPAQUE,
        "delay/on": BOOL,
        "preamp/trim": Lin(-18, 18, "dB"),
        "preamp/hpslope": Enum("12", "18", "24"),
        "dyn/pos": Enum("PRE", "POST"),
        "insert/pos": Enum("PRE", "POST"),
        "mix/st": BOOL,
        "mix/mono": BOOL,
        "mix/mlevel": FADER,
        "mix/#/on": BOOL,
        "mix/#/type": Enum(*TAPS),
        "mix/#/panFollow": OPAQUE,
        "grp/dca": Int(0, 255),
        "grp/mute": Int(0, 63),
    }
    config_bool_nodes = ("chlink", "auxlink", "fxlink", "buslink", "mtxlink", "mute", "linkcfg")
    confirm_prefixes = (
        "/-show", "/-action", "/-usb", "/-snap", "/config", "/outputs", "/fx/", "/save", "/load", "/copy", "/delete",
    )
    fx_count = 8
    mute_groups = 6
    snap_slots = range(100)
    snap_index_addr = "/-show/prepos/current"  # the highlighted cue/scene/snippet, per show-control mode
    snap_index_label = "Highlighted scene/cue/snippet"
    snap_index_verifies = False
    snap_load_note = (
        " If the console has Config > Confirm Pop-Ups > Scene Load switched on, the load waits for"
        " someone to confirm it on the console screen."
    )
    meter_extra = (0, 0, 1)  # as x32-osc sends: two unused request ints, then time factor 1 (50 ms)
    meter_banks = {"channels": ("/meters/0", _channels_layout())}
    id_patterns = [
        (re.compile(r"^(?:ch|chan|channel|in|input)?\s*0*(\d{1,2})$"), "ch"),
        (re.compile(r"^(?:aux ?bus|mix ?bus|bus|mix|mon|monitor)\s*(\d{1,2})$"), "bus"),
        (re.compile(r"^(?:aux|aux ?in|aux ?return)\s*(\d{1,2})?$"), "aux"),
        (re.compile(r"^(?:fx ?rtn|fx ?return|rtn|return)\s*(\d{1,2})$"), "fxrtn"),
        (re.compile(r"^(?:mtx|matrix)\s*(\d{1,2})$"), "mtx"),
        (re.compile(r"^(?:lr|st|main|main ?lr|main ?st|master|stereo|mains)$"), "lr"),
        (re.compile(r"^(?:mono|main ?m|main ?mono|m ?c|center|centre)$"), "mono"),
        (re.compile(r"^dca\s*(\d{1,2})$"), "dca"),
    ]
    id_hint = "ch3, aux1, fxrtn2, bus2, mtx1, lr, mono, dca1"

    def base(self, s: Strip) -> str:
        return {
            "ch": f"/ch/{s.index:02d}",
            "aux": f"/auxin/{s.index:02d}",
            "fxrtn": f"/fxrtn/{s.index:02d}",
            "bus": f"/bus/{s.index:02d}",
            "mtx": f"/mtx/{s.index:02d}",
            "lr": "/main/st",
            "mono": "/main/m",
            "dca": f"/dca/{s.index}",
        }[s.kind]

    def layout(self, kind: str) -> Layout:
        cfg = ("config", ("name", "icon", "color"))
        if kind == "ch":
            return [
                ("config", ("name", "icon", "color", "source")),
                ("delay", ("on", "time")),
                ("preamp", ("trim", "invert", "hpon", "hpslope", "hpf")),
                ("gate", ("on", "mode", "thr", "range", "attack", "hold", "release", "keysrc")),
                ("gate/filter", FILTER),
                *_dyn(DYN),
                *_eq(4),
                ("mix", MIX_IN),
                *_sends(16),
                ("grp", ("dca", "mute")),
                ("automix", ("group", "weight")),
            ]
        if kind == "aux":
            return [
                ("config", ("name", "icon", "color", "source")),
                ("preamp", ("trim", "invert")),
                *_eq(4), ("mix", MIX_IN), *_sends(16), ("grp", ("dca", "mute")),
            ]
        if kind == "fxrtn":
            return [cfg, *_eq(4), ("mix", MIX_IN), *_sends(16), ("grp", ("dca", "mute"))]
        if kind == "bus":
            return [cfg, *_dyn(DYN), *_eq(6), ("mix", MIX_IN), *_sends(6), ("grp", ("dca", "mute"))]
        grp = ("grp", ("dca", "mute"))
        if kind == "mtx":
            return [cfg, ("preamp", ("invert",)), *_dyn(DYN_MAIN), *_eq(6), ("mix", ("on", "fader")), grp]
        if kind == "lr":
            return [cfg, *_dyn(DYN_MAIN), *_eq(6), ("mix", ("on", "fader", "pan")), *_sends(6), grp]
        if kind == "mono":
            return [cfg, *_dyn(DYN_MAIN), *_eq(6), ("mix", ("on", "fader")), *_sends(6), grp]
        if kind == "dca":
            return [("", ("on", "fader")), cfg]
        raise KeyError(kind)

    def global_nodes(self) -> Layout:
        # /-prefs (network settings), /-stat, /-show and /-action are deliberately excluded.
        def pairs(n: int) -> tuple[str, ...]:
            return tuple(f"{i}-{i + 1}" for i in range(1, n, 2))

        return [
            *[(self.headamp_path(n), ("gain", "phantom")) for n in range(128)],
            *[(f"fx/{n}", ("type",)) for n in range(1, 9)],
            *[(f"fx/{n}/source", ("l", "r")) for n in range(1, 5)],
            *[(f"fx/{n}/par", tuple(f"{p:02d}" for p in range(1, 65))) for n in range(1, 9)],
            ("config/chlink", pairs(32)),
            ("config/auxlink", pairs(8)),
            ("config/fxlink", pairs(8)),
            ("config/buslink", pairs(16)),
            ("config/mtxlink", pairs(6)),
            ("config/mute", tuple(str(i) for i in range(1, 7))),
            ("config/linkcfg", ("hadly", "eq", "dyn", "fdrmute")),
            ("config/routing/IN", (*ROUTING_BLOCKS, "AUX")),
            ("config/routing/AES50A", AES50_BLOCKS),
            ("config/routing/AES50B", AES50_BLOCKS),
            ("config/routing/CARD", ROUTING_BLOCKS),
        ]

    # --- sends: channels feed mix buses 1-16; buses and mains feed matrices 1-6

    def send_suffix(self, dest: Strip) -> str:
        if dest.kind in ("bus", "mtx"):
            return f"mix/{dest.index:02d}"
        raise ParamError(f"{dest.id} is not a send destination (use bus1-16)")

    def send_dest(self, s: Strip, n: int) -> Strip:
        return Strip("mtx" if s.kind in ("bus", "lr", "mono") else "bus", n, self.name)

    def send_leaf(self, dest: Strip, sub: str) -> str:
        if sub == "tap":
            odd = dest.index - (dest.index + 1) % 2
            return f"mix/{odd:02d}/type"  # one tap point per bus pair, stored on the odd send
        return super().send_leaf(dest, sub)

    # --- preamps: /headamp/000-031 local inputs, 032-079 AES50 A, 080-127 AES50 B

    def headamp_path(self, n: int) -> str:
        return f"headamp/{n:03d}"

    def headamp_label(self, n: int) -> str:
        if n < 32:
            return f"local preamp {n + 1}"
        return f"AES50-{'A' if n < 80 else 'B'} preamp {(n - 32) % 48 + 1}"

    source_nodes = ("config/routing/IN",)

    def source_reads(self, s: Strip) -> list[str]:
        return [s.addr("config/source")] if s.kind in ("ch", "aux") else []

    def headamp_of(self, s: Strip, raw: dict[str, Any]) -> int | None:
        if s.kind != "ch":
            return None
        src = raw.get(s.addr("config/source"))
        if not (isinstance(src, int) and 1 <= src <= 32):
            return None
        block, offset = divmod(src - 1, 8)
        m = ROUTING_TEXT.match(str(raw.get(f"text:/config/routing/IN/{ROUTING_BLOCKS[block]}", "")))
        if not m:
            return None  # card or unknown
        return {"AN": 0, "A": 32, "B": 80}[m.group(1)] + int(m.group(2)) - 1 + offset

    def source_label(self, s: Strip, cfg_text: dict[str, str], raw: dict[str, Any]) -> str:
        if s.kind == "fxrtn":
            return "FX"
        return source_name(raw.get(s.addr("config/source")))

    def decode_meters(self, blob: bytes) -> list[float]:
        return meters.decode_float(blob)

    # --- scenes

    def snap_name_addr(self, slot: int) -> str:
        return f"/-show/showfile/scene/{slot:03d}/name"

    def snap_used_addr(self, slot: int) -> str | None:
        return f"/-show/showfile/scene/{slot:03d}/hasdata"  # scenes can hold data without a name

    def snap_save(self, c: OscClient, slot: int, name: str) -> None:
        c.send("/save", "scene", slot, name, "")

    def snap_load(self, c: OscClient, slot: int) -> None:
        c.send("/-action/goscene", slot)


X32 = X32Profile()
