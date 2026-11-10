"""Mixer parameter map: value kinds, strips, and the Profile base class.

A Profile describes one mixer family's OSC address space (strips, node layouts,
sends, preamps, meters, snapshots). The data for each family lives in its own
module: xair.py (XR18, verified on hardware) and x32.py (X32/M32, experimental).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from functools import cached_property
from typing import TYPE_CHECKING, Any

from . import units

if TYPE_CHECKING:
    from .osc_client import OscClient


class ParamError(ValueError):
    """Invalid target, parameter or value supplied by the caller."""


# --------------------------------------------------------------------------- kinds


class Kind:
    unit = ""

    def from_raw(self, raw: Any) -> Any:
        return raw

    def to_raw(self, value: Any) -> Any:
        return value

    def fmt(self, value: Any) -> str:
        return str(value)

    def describe(self) -> str:
        return "raw value"


class Opaque(Kind):
    """Mixer-specific value we store and restore but don't interpret."""


class Fader(Kind):
    unit = "dB"

    def from_raw(self, raw: float) -> float:
        db = units.fader_to_db(raw)
        return db if db == units.NEG_INF else round(db, 1)

    def to_raw(self, value: Any) -> float:
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("-inf", "-oo", "off", "-infinity"):
                return 0.0
            value = float(v.replace("db", ""))
        value = float(value)
        if math.isnan(value):
            raise ParamError("level must be a number of dB or '-inf'")
        return round(units.db_to_fader(units.clamp(value, -90.0, 10.0)), 6) if value > -90 else 0.0

    def fmt(self, value: float) -> str:
        return units.fmt_db(value) + (" dB" if value != units.NEG_INF else "")

    def describe(self) -> str:
        return "dB from -inf to +10"


class Lin(Kind):
    def __init__(self, lo: float, hi: float, unit: str = "", nd: int = 1):
        self.lo, self.hi, self.unit, self.nd = lo, hi, unit, nd

    def from_raw(self, raw: float) -> float:
        return round(units.lin_to(self.lo, self.hi, raw), self.nd)

    def to_raw(self, value: Any) -> float:
        x = _num(value, self)
        if not self.lo <= x <= self.hi:
            raise ParamError(f"{x} out of range ({self.describe()})")
        return round(units.lin_from(self.lo, self.hi, x), 6)

    def fmt(self, value: float) -> str:
        return f"{value:g}{' ' + self.unit if self.unit else ''}"

    def describe(self) -> str:
        return f"{self.lo:g}..{self.hi:g}{' ' + self.unit if self.unit else ''}"


class Log(Lin):
    def from_raw(self, raw: float) -> float:
        return round(units.log_to(self.lo, self.hi, raw), self.nd)

    def to_raw(self, value: Any) -> float:
        x = units.parse_freq(value) if isinstance(value, str) else _num(value, self)
        lo, hi = sorted((self.lo, self.hi))
        if not lo <= x <= hi:
            raise ParamError(f"{x} out of range ({self.describe()})")
        return round(units.log_from(self.lo, self.hi, x), 6)

    def describe(self) -> str:
        lo, hi = sorted((self.lo, self.hi))
        return f"{lo:g}..{hi:g}{' ' + self.unit if self.unit else ''}"


class Bool(Kind):
    def from_raw(self, raw: int) -> bool:
        return bool(raw)

    def to_raw(self, value: Any) -> int:
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("on", "true", "yes", "1"):
                return 1
            if v in ("off", "false", "no", "0"):
                return 0
            raise ParamError(f"expected on/off, got {value!r}")
        return 1 if value else 0

    def fmt(self, value: bool) -> str:
        return "ON" if value else "OFF"

    def describe(self) -> str:
        return "on/off"


class Enum(Kind):
    def __init__(self, *labels: str):
        self.labels = labels

    def from_raw(self, raw: int) -> str:
        return self.labels[raw] if 0 <= raw < len(self.labels) else str(raw)

    def to_raw(self, value: Any) -> int:
        if isinstance(value, bool):
            raise ParamError(f"expected one of {self.describe()}")
        if isinstance(value, (int, float)) and float(value).is_integer() and 0 <= value < len(self.labels):
            return int(value)
        v = str(value).strip().lower()
        for i, label in enumerate(self.labels):
            if label.lower() == v:
                return i
        raise ParamError(f"{value!r} is not one of {self.describe()}")

    def describe(self) -> str:
        return "|".join(self.labels)


class Int(Kind):
    def __init__(self, lo: int, hi: int):
        self.lo, self.hi = lo, hi

    def to_raw(self, value: Any) -> int:
        x = int(_num(value, self))
        if not self.lo <= x <= self.hi:
            raise ParamError(f"{x} out of range ({self.describe()})")
        return x

    def describe(self) -> str:
        return f"integer {self.lo}..{self.hi}"


class Str(Kind):
    def __init__(self, maxlen: int = 12):
        self.maxlen = maxlen

    def to_raw(self, value: Any) -> str:
        s = str(value)
        if len(s) > self.maxlen:
            raise ParamError(f"name {s!r} is longer than {self.maxlen} characters")
        return s

    def fmt(self, value: str) -> str:
        return f'"{value}"'

    def describe(self) -> str:
        return f"text up to {self.maxlen} characters"


def _num(value: Any, kind: Kind) -> float:
    try:
        return float(str(value).lower().replace("db", "").replace("hz", "").strip())
    except ValueError:
        raise ParamError(f"{value!r} is not a number ({kind.describe()})") from None


COLORS = (
    "off", "red", "green", "yellow", "blue", "magenta", "cyan", "white",
    "off_inv", "red_inv", "green_inv", "yellow_inv", "blue_inv", "magenta_inv", "cyan_inv", "white_inv",
)
EQ_TYPES = ("LCut", "LShv", "PEQ", "VEQ", "HShv", "HCut")
TAPS = ("IN", "PREEQ", "POSTEQ", "PRE", "POST", "GRP")
GATE_MODES = ("EXP2", "EXP3", "EXP4", "GATE", "DUCK")
RATIOS = ("1.1", "1.3", "1.5", "2.0", "2.5", "3.0", "4.0", "5.0", "7.0", "10", "20", "100")
FILTER_TYPES = ("LC6", "LC12", "HC6", "HC12", "1.0", "2.0", "3.0", "5.0", "10.0")

FADER = Fader()
BOOL = Bool()
OPAQUE = Opaque()
FREQ = Log(20, 20000, "Hz", 1)
TIME_HOLD = Log(0.02, 2000, "ms", 2)
TIME_RELEASE = Log(5, 4000, "ms", 0)
ATTACK = Lin(0, 120, "ms", 0)
PAN = Lin(-100, 100, "", 0)
BITMASK = Int(0, 15)

# Kinds by generic key: the leaf address relative to the strip base, with
# numeric path segments replaced by '#'. These are the X-Air kinds; other
# profiles extend them.
KINDS: dict[str, Kind] = {
    "config/name": Str(12),
    "config/color": Enum(*COLORS),
    "config/insrc": OPAQUE,
    "config/rtnsrc": OPAQUE,
    "preamp/rtntrim": Lin(-18, 18, "dB"),
    "preamp/rtnsw": BOOL,
    "preamp/invert": BOOL,
    "preamp/hpon": BOOL,
    "preamp/hpf": Log(20, 400, "Hz", 0),
    "gate/on": BOOL,
    "gate/mode": Enum(*GATE_MODES),
    "gate/thr": Lin(-80, 0, "dB"),
    "gate/range": Lin(3, 60, "dB"),
    "gate/attack": ATTACK,
    "gate/hold": TIME_HOLD,
    "gate/release": TIME_RELEASE,
    "gate/keysrc": OPAQUE,
    "gate/filter/on": BOOL,
    "gate/filter/type": Enum(*FILTER_TYPES),
    "gate/filter/f": FREQ,
    "dyn/on": BOOL,
    "dyn/mode": Enum("COMP", "EXP"),
    "dyn/det": Enum("PEAK", "RMS"),
    "dyn/env": Enum("LIN", "LOG"),
    "dyn/thr": Lin(-60, 0, "dB"),
    "dyn/ratio": Enum(*RATIOS),
    "dyn/knee": Lin(0, 5, "", 0),
    "dyn/mgain": Lin(0, 24, "dB"),
    "dyn/attack": ATTACK,
    "dyn/hold": TIME_HOLD,
    "dyn/release": TIME_RELEASE,
    "dyn/mix": Lin(0, 100, "%", 0),
    "dyn/keysrc": OPAQUE,
    "dyn/auto": BOOL,
    "dyn/filter/on": BOOL,
    "dyn/filter/type": Enum(*FILTER_TYPES),
    "dyn/filter/f": FREQ,
    "insert/on": BOOL,
    "insert/sel": OPAQUE,
    "eq/on": BOOL,
    "eq/mode": Enum("PEQ", "GEQ", "TEQ"),
    "eq/#/type": Enum(*EQ_TYPES),
    "eq/#/f": FREQ,
    "eq/#/g": Lin(-15, 15, "dB"),
    "eq/#/q": Log(10, 0.3, "", 1),
    "geq/*": Lin(-15, 15, "dB"),
    "mix/on": BOOL,
    "mix/fader": FADER,
    "mix/lr": BOOL,
    "mix/pan": PAN,
    "mix/#/level": FADER,
    "mix/#/grpon": BOOL,
    "mix/#/tap": Enum(*TAPS),
    "mix/#/pan": PAN,
    "grp/dca": BITMASK,
    "grp/mute": BITMASK,
    "automix/group": Enum("OFF", "X", "Y"),
    "automix/weight": Lin(-12, 12, "dB"),
    "on": BOOL,  # dca
    "fader": FADER,  # dca
    "headamp/gain": Lin(-12, 60, "dB"),
    "headamp/phantom": BOOL,
}

GEQ_BANDS = (
    "20", "25", "31.5", "40", "50", "63", "80", "100", "125", "160", "200", "250", "315", "400", "500",
    "630", "800", "1k", "1k25", "1k6", "2k", "2k5", "3k15", "4k", "5k", "6k3", "8k", "10k", "12k5", "16k", "20k",
)

# ----------------------------------------------------------------- node layouts

EQ_BAND = ("type", "f", "g", "q")
FILTER = ("on", "type", "f")

Layout = list[tuple[str, tuple[str, ...]]]
MeterLayout = list[tuple[str, "Strip | None", list[int]]]


def _generic_key(rel: str) -> str:
    return re.sub(r"(?<=/)\d+(?=/|$)", "#", rel)


def leaf(path: str, param: str) -> str:
    return f"/{path}/{param}" if param else f"/{path}"


# ------------------------------------------------------------------------ strips

PROFILES: dict[str, Profile] = {}


@dataclass(frozen=True)
class Strip:
    kind: str
    index: int = 0
    profile: str = "xair"

    @property
    def p(self) -> Profile:
        return PROFILES[self.profile]

    @cached_property
    def id(self) -> str:
        return self.kind if self.kind in self.p.singles else f"{self.kind}{self.index}"

    @cached_property
    def base(self) -> str:
        return self.p.base(self)

    @property
    def title(self) -> str:
        return self.p.titles[self.kind].format(i=self.index)

    def nodes(self) -> Layout:
        """(node path without leading slash, params in /node text order)."""
        out = []
        for suffix, params in self.p.layout(self.kind):
            path = self.base[1:] + (f"/{suffix}" if suffix else "")
            out.append((path, params))
        return out

    def addr(self, suffix: str) -> str:
        return f"{self.base}/{suffix}" if suffix else self.base

    @property
    def sends_from(self) -> bool:
        return self.kind in self.p.send_sources

    @property
    def eq_bands(self) -> int:
        return self.p.eq_bands.get(self.kind, 0)


def label(s: Strip, names: dict[Strip, str]) -> str:
    n = names.get(s)
    return f"{s.id} ({n})" if n else s.id


# ------------------------------------------------------------ friendly params

COMP_KEYS = {
    "on": "on", "mode": "mode", "det": "det", "env": "env", "thr": "thr", "threshold": "thr", "ratio": "ratio",
    "knee": "knee", "gain": "mgain", "makeup": "mgain", "attack": "attack", "hold": "hold", "release": "release",
    "mix": "mix", "auto": "auto",
}
GATE_KEYS = {
    "on": "on", "mode": "mode", "thr": "thr", "threshold": "thr", "range": "range", "attack": "attack",
    "hold": "hold", "release": "release",
}
EQ_KEYS = {"type": "type", "freq": "f", "f": "f", "gain": "g", "g": "g", "q": "q"}


# ----------------------------------------------------------------------- profile


class Profile:
    """One mixer family's OSC map. Subclasses supply the data; the logic lives here."""

    name = ""
    family = ""  # shown to the user, e.g. "XR18"
    port = 0
    experimental = False
    strip_kinds: tuple[str, ...] = ()
    counts: dict[str, int] = {}
    singles: tuple[str, ...] = ()  # kinds with one strip whose id has no number ("lr")
    titles: dict[str, str] = {}
    input_kinds: tuple[str, ...] = ("ch", "aux", "fxrtn")
    output_kinds: tuple[str, ...] = ()
    send_sources: tuple[str, ...] = ("ch", "aux", "fxrtn")
    send_dest_kinds: tuple[str, ...] = ("bus",)
    send_subs: tuple[str, ...] = ("level", "tap", "pan")
    comp_kinds: tuple[str, ...] = ("ch", "bus", "lr")
    lr_kinds: tuple[str, ...] = ("ch", "aux", "fxrtn", "bus")
    pan_kinds: tuple[str, ...] = ("ch", "aux", "fxrtn", "bus", "lr")
    usb_kinds: tuple[str, ...] = ()
    trim_kinds: tuple[str, ...] = ()
    eq_bands: dict[str, int] = {}
    lr_param = "mix/lr"  # per-strip "assigned to main" switch
    kinds: dict[str, Kind] = KINDS
    config_bool_nodes: tuple[str, ...] = ()
    blocked_prefixes: tuple[str, ...] = ("/-prefs",)
    confirm_prefixes: tuple[str, ...] = ()
    fx_count = 4
    mute_groups = 4
    snap_slots = range(1, 65)
    snap_index_label = "Current snapshot index"
    snap_index_verifies = True  # the index reliably reports the snapshot just loaded
    snap_load_note = ""
    meter_banks: dict[str, tuple[str, MeterLayout]] = {}
    meter_extra: tuple[Any, ...] = ()  # extra /meters request arguments after the bank name
    id_patterns: list[tuple[re.Pattern[str], str]] = []
    id_hint = ""

    def __init__(self) -> None:
        PROFILES[self.name] = self

    # --- data hooks (overridden per family)

    def base(self, s: Strip) -> str:
        raise NotImplementedError

    def layout(self, kind: str) -> Layout:
        raise NotImplementedError

    def global_nodes(self) -> Layout:
        """Nodes outside the strips that belong in a full-board preset."""
        raise NotImplementedError

    def send_suffix(self, dest: Strip) -> str:
        raise NotImplementedError

    def send_dest(self, s: Strip, n: int) -> Strip:
        """The strip that send node mix/<n> of strip s feeds."""
        raise NotImplementedError

    def send_leaf(self, dest: Strip, sub: str) -> str:
        """Leaf (relative to the source strip) for a send parameter: level, tap, pan, on."""
        if sub not in self.send_subs:
            raise ParamError(f"send parameter must be {'|'.join(self.send_subs)}")
        suffix = self.send_suffix(dest)
        if sub == "pan" and not (dest.kind == "bus" and dest.index % 2 == 1):
            raise ParamError("send pan exists only on odd buses (bus1, bus3, bus5) when linked as stereo pairs")
        return f"{suffix}/{sub}"

    def headamp_path(self, n: int) -> str:
        raise NotImplementedError

    def headamp_label(self, n: int) -> str:
        return f"preamp {n}"

    def headamp_addr(self, n: int, param: str) -> str:
        return f"/{self.headamp_path(n)}/{param}"

    # Nodes read as /node text for headamp_of; their values appear in raw as "text:/<node>/<param>".
    source_nodes: tuple[str, ...] = ()

    def source_reads(self, s: Strip) -> list[str]:
        """Addresses headamp_of / uses_usb / source_label need for strip s."""
        raise NotImplementedError

    def headamp_of(self, s: Strip, raw: dict[str, Any]) -> int | None:
        """Mic preamp feeding channel s, from Mixer.sources() values (None: not a local preamp)."""
        raise NotImplementedError

    def uses_usb(self, s: Strip, raw: dict[str, Any]) -> bool:
        return False

    def source_label(self, s: Strip, cfg_text: dict[str, str], raw: dict[str, Any]) -> str:
        raise NotImplementedError

    def decode_meters(self, blob: bytes) -> list[float]:
        raise NotImplementedError

    def snap_name_addr(self, slot: int) -> str:
        raise NotImplementedError

    snap_index_addr = ""

    def snap_used_addr(self, slot: int) -> str | None:
        """Address that reads non-zero when a slot holds data (None: a slot is in use when it has a name)."""
        return None

    def snap_save(self, c: OscClient, slot: int, name: str) -> None:
        raise NotImplementedError

    def snap_load(self, c: OscClient, slot: int) -> None:
        raise NotImplementedError

    # --- derived maps

    @cached_property
    def all_strips(self) -> tuple[Strip, ...]:
        return tuple(
            Strip(k, i, self.name)
            for k in self.strip_kinds
            for i in ((0,) if k in self.singles else range(1, self.counts[k] + 1))
        )

    def strips(self, *kinds: str) -> list[Strip]:
        return [s for s in self.all_strips if s.kind in kinds]

    @cached_property
    def send_dests(self) -> tuple[Strip, ...]:
        return tuple(self.strips(*self.send_dest_kinds))

    @cached_property
    def main_strips(self) -> tuple[Strip, ...]:
        return tuple(s for s in self.all_strips if s.kind in ("lr", "mono"))

    def all_nodes(self) -> Layout:
        out = []
        for s in self.all_strips:
            out.extend(s.nodes())
        out.extend(self.global_nodes())
        return out

    @cached_property
    def layouts(self) -> dict[str, tuple[str, ...]]:
        return dict(self.all_nodes())

    @cached_property
    def _by_base(self) -> list[Strip]:
        return sorted(self.all_strips, key=lambda s: -len(s.base))

    def _kind_for(self, path: str, param: str) -> Kind:
        lf = leaf(path, param)
        if path.startswith("headamp/"):
            return self.kinds[f"headamp/{param}"]
        if path.endswith("/geq"):
            return self.kinds["geq/*"]
        s = self.strip_of(lf)
        if s is not None:
            return self.kinds.get(_generic_key(lf[len(s.base) + 1 :]), OPAQUE)
        if path == "config/solo" and param == "level":
            return FADER
        if path.startswith("config/") and param != "":
            return BOOL if path.split("/")[1] in self.config_bool_nodes else OPAQUE
        return OPAQUE

    @cached_property
    def leaf_kinds(self) -> dict[str, Kind]:
        return {leaf(p, prm): self._kind_for(p, prm) for p, params in self.all_nodes() for prm in params}

    @property
    def leaves(self) -> list[str]:
        return list(self.leaf_kinds)

    def kind_of(self, address: str) -> Kind | None:
        return self.leaf_kinds.get(address)

    def strip_of(self, address: str) -> Strip | None:
        for s in self._by_base:
            if address == s.base or address.startswith(s.base + "/"):
                return s
        return None

    @cached_property
    def main_fader_addrs(self) -> tuple[str, ...]:
        return tuple(s.addr("mix/fader") for s in self.main_strips)

    @cached_property
    def main_on_addrs(self) -> tuple[str, ...]:
        return tuple(s.addr("mix/on") for s in self.main_strips)

    def is_mute_switch(self, address: str) -> bool:
        return address.endswith("/mix/on") or bool(re.fullmatch(r"/dca/\d+/on", address))

    # --- names

    def parse_strip_id(self, text: str, fx_means: str = "fxrtn") -> Strip | None:
        t = text.strip().lower().replace("_", " ").replace("-", " ")
        m = re.match(r"^fx\s*(\d{1,2})$", t)
        if m:
            return self._checked(fx_means if fx_means in self.counts else "fxrtn", int(m.group(1)))
        for pat, kind in self.id_patterns:
            m = pat.match(t)
            if m:
                return self._checked(kind, int(m.group(1)) if m.groups() and m.group(1) else 0)
        return None

    def _checked(self, kind: str, index: int) -> Strip:
        n = self.counts[kind]
        if kind in self.singles:
            return Strip(kind, 0, self.name)
        if not 1 <= index <= n:
            raise ParamError(f"{kind}{index} does not exist on the {self.family} ({kind}1-{n})")
        return Strip(kind, index, self.name)

    def resolve(
        self, text: str, names: dict[Strip, str], allowed: Iterable[str] | None = None, fx_means: str = "fxrtn"
    ) -> Strip:
        """Resolve a user reference ('ch3', 'bus 2', 'Guitar', 'click') to a strip."""
        allowed_set = set(allowed) if allowed else set(self.strip_kinds)
        s = self.parse_strip_id(text, fx_means)
        if s is None:
            q = text.strip().lower()
            pool = {st: n for st, n in names.items() if n and st.kind in allowed_set}
            for test in (
                lambda n: n.lower() == q,
                lambda n: n.lower().startswith(q),
                lambda n: q in n.lower(),
            ):
                hits = [st for st, n in pool.items() if test(n)]
                if len(hits) == 1:
                    s = hits[0]
                    break
                if len(hits) > 1:
                    opts = ", ".join(f"{h.id} ({pool[h]})" for h in hits)
                    raise ParamError(f"{text!r} is ambiguous: {opts}")
        if s is None:
            raise ParamError(f"No channel or bus called {text!r}. Use board_overview to see names, or ids like {self.id_hint}.")
        if s.kind not in allowed_set:
            raise ParamError(f"{s.id} can't be used here (expected {', '.join(sorted(allowed_set))})")
        return s

    # --- friendly parameters

    def friendly_address(self, s: Strip, key: str, dest: Strip | None = None) -> tuple[str, Kind]:
        """Map a friendly parameter key to (address, kind). Headamp keys are handled by the caller."""
        k = key.strip().lower()
        parts = k.split(".")

        def need(*kinds: str) -> None:
            if s.kind not in kinds:
                raise ParamError(f"{s.id} has no {key!r} parameter")

        def at(rel: str) -> tuple[str, Kind]:
            return s.addr(rel), self.kinds[_generic_key(rel)]

        if k in ("name", "color"):
            return at(f"config/{k}")
        if k == "fader":
            return (s.addr("fader"), FADER) if s.kind == "dca" else (s.addr("mix/fader"), FADER)
        if k == "on":
            return (s.addr("on"), BOOL) if s.kind == "dca" else (s.addr("mix/on"), BOOL)
        if k == "lr":
            need(*self.lr_kinds)
            return at(self.lr_param)
        if k == "pan":
            need(*self.pan_kinds)
            return at("mix/pan")
        if k in ("hpf", "hpf.freq"):
            need("ch")
            return at("preamp/hpf")
        if k == "hpf.on":
            need("ch")
            return at("preamp/hpon")
        if k == "invert":
            need("ch")
            return at("preamp/invert")
        if k == "usb":
            need(*self.usb_kinds)
            return at("preamp/rtnsw")
        if k == "trim":
            need(*self.trim_kinds)
            return at("preamp/trim")
        if k == "eq.on":
            if not s.eq_bands:
                raise ParamError(f"{s.id} has no {key!r} parameter")
            return at("eq/on")
        if parts[0] == "eq" and len(parts) == 3:
            band = int(parts[1]) if parts[1].isdigit() else 0
            if not 1 <= band <= s.eq_bands:
                raise ParamError(f"{s.id} has EQ bands 1-{s.eq_bands}")
            sub = EQ_KEYS.get(parts[2])
            if not sub:
                raise ParamError(f"EQ band parameter must be one of type|freq|gain|q, got {parts[2]!r}")
            return at(f"eq/{band}/{sub}")
        if parts[0] == "gate" and len(parts) == 2:
            need("ch")
            sub = GATE_KEYS.get(parts[1])
            if not sub:
                raise ParamError(f"gate parameter must be one of {'|'.join(GATE_KEYS)}")
            return at(f"gate/{sub}")
        if parts[0] in ("comp", "dyn") and len(parts) == 2:
            need(*self.comp_kinds)
            sub = COMP_KEYS.get(parts[1])
            if not sub:
                raise ParamError(f"comp parameter must be one of {'|'.join(COMP_KEYS)}")
            return at(f"dyn/{sub}")
        if parts[0] == "send" and len(parts) == 2:
            if not s.sends_from:
                raise ParamError(f"{s.id} has no sends (only channels, aux and FX returns send to buses)")
            if dest is None:
                raise ParamError("send needs a destination bus")
            return at(self.send_leaf(dest, parts[1]))
        raise ParamError(
            f"Unknown parameter {key!r}. Use one of: name, color, fader, on, lr, pan, hpf, hpf.on, invert, "
            + ("usb, " if self.usb_kinds else "")
            + ("trim, " if self.trim_kinds else "")
            + "eq.on, eq.<band>.type|freq|gain|q, gate.<param>, comp.<param>, "
            f"send.{'|'.join(self.send_subs)} (with dest), gain, phantom"
        )


def for_model(model_name: str) -> Profile:
    """Pick the profile for the model string a mixer reports in /xinfo."""
    m = model_name.strip().upper()
    return X32 if m.startswith(("X32", "M32")) else XAIR


# Profile modules import this one, so they are loaded last.
from .x32 import X32
from .xair import XAIR
