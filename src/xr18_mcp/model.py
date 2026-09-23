"""XR18 parameter map: strips, OSC node layout, value kinds and name resolution.

Node layouts (the parameter order inside each /node reply) were read from a
live XR18 running firmware 1.25.
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from . import units


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
# numeric path segments replaced by '#'.
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
DYN = ("on", "mode", "det", "env", "thr", "ratio", "knee", "mgain", "attack", "hold", "release", "mix", "keysrc", "auto")
DYN_LR = tuple(p for p in DYN if p != "keysrc")
FILTER = ("on", "type", "f")


def _sends() -> list[tuple[str, tuple[str, ...]]]:
    # Sends 01-06 go to buses (odd ones carry the stereo-pair pan), 07-10 to FX sends.
    out = []
    for i in range(1, 11):
        params = ("level", "grpon", "tap", "pan") if i <= 6 and i % 2 == 1 else ("level", "grpon", "tap")
        out.append((f"mix/{i:02d}", params))
    return out


STRIP_KINDS = ("ch", "aux", "fxrtn", "bus", "fxsend", "lr", "dca")
COUNTS = {"ch": 16, "aux": 1, "fxrtn": 4, "bus": 6, "fxsend": 4, "lr": 1, "dca": 4}
SEND_SOURCES = ("ch", "aux", "fxrtn")


def _layout(kind: str) -> list[tuple[str, tuple[str, ...]]]:
    if kind == "ch":
        return [
            ("config", ("name", "color", "insrc", "rtnsrc")),
            ("preamp", ("rtntrim", "rtnsw", "invert", "hpon", "hpf")),
            ("gate", ("on", "mode", "thr", "range", "attack", "hold", "release", "keysrc")),
            ("gate/filter", FILTER),
            ("dyn", DYN),
            ("dyn/filter", FILTER),
            ("insert", ("on", "sel")),
            ("eq", ("on",)),
            *[(f"eq/{b}", EQ_BAND) for b in range(1, 5)],
            ("mix", ("on", "fader", "lr", "pan")),
            *_sends(),
            ("grp", ("dca", "mute")),
            ("automix", ("group", "weight")),
        ]
    if kind in ("aux", "fxrtn"):
        return [
            ("config", ("name", "color", "rtnsrc")),
            ("preamp", ("rtntrim", "rtnsw")),
            ("eq", ("on",)),
            *[(f"eq/{b}", EQ_BAND) for b in range(1, 5)],
            ("mix", ("on", "fader", "lr", "pan")),
            *_sends(),
            ("grp", ("dca", "mute")),
        ]
    if kind == "bus":
        return [
            ("config", ("name", "color")),
            ("dyn", DYN),
            ("dyn/filter", FILTER),
            ("insert", ("on", "sel")),
            ("eq", ("on", "mode")),
            *[(f"eq/{b}", EQ_BAND) for b in range(1, 7)],
            ("geq", GEQ_BANDS),
            ("mix", ("on", "fader", "lr", "pan")),
            ("grp", ("dca", "mute")),
        ]
    if kind == "fxsend":
        return [("config", ("name", "color")), ("mix", ("on", "fader")), ("grp", ("dca", "mute"))]
    if kind == "lr":
        return [
            ("config", ("name", "color")),
            ("dyn", DYN_LR),
            ("dyn/filter", FILTER),
            ("insert", ("on", "sel")),
            ("eq", ("on", "mode")),
            *[(f"eq/{b}", EQ_BAND) for b in range(1, 7)],
            ("geq", GEQ_BANDS),
            ("mix", ("on", "fader", "pan")),
        ]
    if kind == "dca":
        return [("", ("on", "fader")), ("config", ("name", "color"))]
    raise KeyError(kind)


# ------------------------------------------------------------------------ strips


@dataclass(frozen=True)
class Strip:
    kind: str
    index: int = 0

    @property
    def id(self) -> str:
        return self.kind if self.kind in ("aux", "lr") else f"{self.kind}{self.index}"

    @property
    def base(self) -> str:
        return {
            "ch": f"/ch/{self.index:02d}",
            "aux": "/rtn/aux",
            "fxrtn": f"/rtn/{self.index}",
            "bus": f"/bus/{self.index}",
            "fxsend": f"/fxsend/{self.index}",
            "lr": "/lr",
            "dca": f"/dca/{self.index}",
        }[self.kind]

    @property
    def title(self) -> str:
        return {
            "ch": f"Channel {self.index}",
            "aux": "Aux in",
            "fxrtn": f"FX return {self.index}",
            "bus": f"Bus {self.index}",
            "fxsend": f"FX send {self.index}",
            "lr": "Main LR",
            "dca": f"DCA {self.index}",
        }[self.kind]

    def nodes(self) -> list[tuple[str, tuple[str, ...]]]:
        """(node path without leading slash, params in /node text order)."""
        out = []
        for suffix, params in _layout(self.kind):
            path = self.base[1:] + (f"/{suffix}" if suffix else "")
            out.append((path, params))
        return out

    def addr(self, suffix: str) -> str:
        return f"{self.base}/{suffix}" if suffix else self.base

    @property
    def sends_from(self) -> bool:
        return self.kind in SEND_SOURCES

    @property
    def eq_bands(self) -> int:
        return 6 if self.kind in ("bus", "lr") else 4 if self.kind in ("ch", "aux", "fxrtn") else 0


ALL_STRIPS: tuple[Strip, ...] = tuple(
    Strip(k, i) for k in STRIP_KINDS for i in ((0,) if k in ("aux", "lr") else range(1, COUNTS[k] + 1))
)
SEND_DESTS: tuple[Strip, ...] = tuple(s for s in ALL_STRIPS if s.kind in ("bus", "fxsend"))


def send_suffix(dest: Strip) -> str:
    if dest.kind == "bus":
        return f"mix/{dest.index:02d}"
    if dest.kind == "fxsend":
        return f"mix/{dest.index + 6:02d}"
    raise ParamError(f"{dest.id} is not a send destination (use bus1-6 or fxsend1-4)")


def headamp_path(n: int) -> str:
    return f"headamp/{n:02d}"


# Nodes outside the strips that belong in a full-board preset. /-prefs (network
# settings incl. Wi-Fi passwords), /-stat, /-snap and /-action are deliberately
# excluded.
GLOBAL_NODES: list[tuple[str, tuple[str, ...]]] = [
    *[(headamp_path(n), ("gain", "phantom")) for n in range(1, 17)],
    *[(headamp_path(n), ("gain",)) for n in range(17, 25)],
    *[(f"fx/{n}", ("type", "insert")) for n in range(1, 5)],
    *[(f"fx/{n}/par", tuple(f"{p:02d}" for p in range(1, 65))) for n in range(1, 5)],
    ("config/chlink", tuple(f"{i}-{i + 1}" for i in range(1, 16, 2))),
    ("config/buslink", ("1-2", "3-4", "5-6")),
    ("config/linkcfg", ("eq", "dyn", "fdrmute")),
    ("config/mute", ("1", "2", "3", "4")),
    ("config/amixenable", ("X", "Y")),
    ("config/amixlock", ("X", "Y")),
    ("config/solo", ("level", "source", "sourcetrim", "chmode", "busmode", "dim", "mono", "mute", "dimpfl")),
    *[(f"routing/main/{i:02d}", ("",)) for i in (1, 2)],
    *[(f"routing/aux/{i:02d}", ("src", "pos")) for i in range(1, 7)],
    *[(f"routing/p16/{i:02d}", ("src", "pos")) for i in range(1, 17)],
    *[(f"routing/usb/{i:02d}", ("src", "pos")) for i in range(1, 19)],
]

BLOCKED_PREFIXES = ("/-prefs",)
CONFIRM_PREFIXES = ("/-snap", "/-action", "/-usb", "/-show", "/config", "/routing", "/fx/")


def _generic_key(rel: str) -> str:
    return re.sub(r"(?<=/)\d+(?=/|$)", "#", rel)


def _kind_for(path: str, param: str) -> Kind:
    leaf = f"{path}/{param}" if param else path
    if path.startswith("headamp/"):
        return KINDS[f"headamp/{param}"]
    if path.endswith("/geq"):
        return KINDS["geq/*"]
    for s in ALL_STRIPS:
        base = s.base[1:]
        if leaf.startswith(base + "/"):
            rel = leaf[len(base) + 1 :]
            return KINDS.get(_generic_key(rel), OPAQUE)
    if path == "config/solo" and param == "level":
        return FADER
    if path.startswith("config/") and param != "":
        return BOOL if path.split("/")[1] in ("chlink", "buslink", "linkcfg", "mute", "amixenable", "amixlock") else OPAQUE
    return OPAQUE


def all_nodes() -> list[tuple[str, tuple[str, ...]]]:
    out = []
    for s in ALL_STRIPS:
        out.extend(s.nodes())
    out.extend(GLOBAL_NODES)
    return out


def leaf(path: str, param: str) -> str:
    return f"/{path}/{param}" if param else f"/{path}"


LEAF_KINDS: dict[str, Kind] = {leaf(p, prm): _kind_for(p, prm) for p, params in all_nodes() for prm in params}


def kind_of(address: str) -> Kind | None:
    return LEAF_KINDS.get(address)


def strip_of(address: str) -> Strip | None:
    for s in sorted(ALL_STRIPS, key=lambda s: -len(s.base)):
        if address == s.base or address.startswith(s.base + "/"):
            return s
    return None


# ----------------------------------------------------------------- name lookup

_ID_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"^(?:ch|chan|channel|in|input)?\s*0*(\d{1,2})$"), "ch"),
    (re.compile(r"^(?:aux|aux ?in|aux ?return|rtn ?aux|aux ?rtn)$"), "aux"),
    (re.compile(r"^(?:fx ?rtn|fx ?return|rtn|return)\s*(\d)$"), "fxrtn"),
    (re.compile(r"^(?:bus|mix|mon|monitor|aux ?bus)\s*(\d)$"), "bus"),
    (re.compile(r"^(?:fx ?send|fxs)\s*(\d)$"), "fxsend"),
    (re.compile(r"^(?:lr|main|main ?lr|master|stereo|mains)$"), "lr"),
    (re.compile(r"^dca\s*(\d)$"), "dca"),
]


def parse_strip_id(text: str, fx_means: str = "fxrtn") -> Strip | None:
    t = text.strip().lower().replace("_", " ").replace("-", " ")
    m = re.match(r"^fx\s*(\d)$", t)
    if m:
        return _checked(Strip(fx_means, int(m.group(1))))
    for pat, kind in _ID_PATTERNS:
        m = pat.match(t)
        if m:
            return _checked(Strip(kind, int(m.group(1)) if m.groups() else 0))
    return None


def _checked(s: Strip) -> Strip:
    if s.kind not in ("aux", "lr") and not 1 <= s.index <= COUNTS[s.kind]:
        raise ParamError(f"{s.kind}{s.index} does not exist on the XR18 ({s.kind}1-{COUNTS[s.kind]})")
    return s


def resolve(text: str, names: dict[Strip, str], allowed: Iterable[str] | None = None, fx_means: str = "fxrtn") -> Strip:
    """Resolve a user reference ('ch3', 'bus 2', 'Guitar', 'click') to a strip."""
    allowed_set = set(allowed) if allowed else set(STRIP_KINDS)
    s = parse_strip_id(text, fx_means)
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
        raise ParamError(f"No channel or bus called {text!r}. Use board_overview to see names, or ids like ch3, bus2, fxsend1, aux, lr, dca1.")
    if s.kind not in allowed_set:
        raise ParamError(f"{s.id} can't be used here (expected {', '.join(sorted(allowed_set))})")
    return s


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


def friendly_address(s: Strip, key: str, dest: Strip | None = None) -> tuple[str, Kind]:
    """Map a friendly parameter key to (address, kind). Headamp keys are handled by the caller."""
    k = key.strip().lower()
    parts = k.split(".")

    def need(*kinds: str) -> None:
        if s.kind not in kinds:
            raise ParamError(f"{s.id} has no {key!r} parameter")

    if k == "name":
        return s.addr("config/name"), KINDS["config/name"]
    if k == "color":
        return s.addr("config/color"), KINDS["config/color"]
    if k == "fader":
        return (s.addr("fader"), FADER) if s.kind == "dca" else (s.addr("mix/fader"), FADER)
    if k == "on":
        return (s.addr("on"), BOOL) if s.kind == "dca" else (s.addr("mix/on"), BOOL)
    if k == "lr":
        need("ch", "aux", "fxrtn", "bus")
        return s.addr("mix/lr"), BOOL
    if k == "pan":
        need("ch", "aux", "fxrtn", "bus", "lr")
        return s.addr("mix/pan"), PAN
    if k in ("hpf", "hpf.freq"):
        need("ch")
        return s.addr("preamp/hpf"), KINDS["preamp/hpf"]
    if k == "hpf.on":
        need("ch")
        return s.addr("preamp/hpon"), BOOL
    if k == "invert":
        need("ch")
        return s.addr("preamp/invert"), BOOL
    if k == "usb":
        need("ch", "aux")
        return s.addr("preamp/rtnsw"), BOOL
    if k == "eq.on":
        need("ch", "aux", "fxrtn", "bus", "lr")
        return s.addr("eq/on"), BOOL
    if parts[0] == "eq" and len(parts) == 3:
        band = int(parts[1]) if parts[1].isdigit() else 0
        if not 1 <= band <= s.eq_bands:
            raise ParamError(f"{s.id} has EQ bands 1-{s.eq_bands}")
        sub = EQ_KEYS.get(parts[2])
        if not sub:
            raise ParamError(f"EQ band parameter must be one of type|freq|gain|q, got {parts[2]!r}")
        return s.addr(f"eq/{band}/{sub}"), KINDS[f"eq/#/{sub}"]
    if parts[0] == "gate" and len(parts) == 2:
        need("ch")
        sub = GATE_KEYS.get(parts[1])
        if not sub:
            raise ParamError(f"gate parameter must be one of {'|'.join(GATE_KEYS)}")
        return s.addr(f"gate/{sub}"), KINDS[f"gate/{sub}"]
    if parts[0] in ("comp", "dyn") and len(parts) == 2:
        need("ch", "bus", "lr")
        sub = COMP_KEYS.get(parts[1])
        if not sub:
            raise ParamError(f"comp parameter must be one of {'|'.join(COMP_KEYS)}")
        return s.addr(f"dyn/{sub}"), KINDS[f"dyn/{sub}"]
    if parts[0] == "send" and len(parts) == 2:
        if not s.sends_from:
            raise ParamError(f"{s.id} has no sends (only channels, aux and FX returns send to buses)")
        if dest is None:
            raise ParamError("send needs a destination bus")
        sub = {"level": "level", "tap": "tap", "pan": "pan"}.get(parts[1])
        if not sub:
            raise ParamError("send parameter must be level|tap|pan")
        suffix = send_suffix(dest)
        if sub == "pan" and not (dest.kind == "bus" and dest.index % 2 == 1):
            raise ParamError("send pan exists only on odd buses (bus1, bus3, bus5) when linked as stereo pairs")
        return s.addr(f"{suffix}/{sub}"), KINDS[f"mix/#/{sub}"]
    raise ParamError(
        f"Unknown parameter {key!r}. Use one of: name, color, fader, on, lr, pan, hpf, hpf.on, invert, usb, eq.on, "
        "eq.<band>.type|freq|gain|q, gate.<param>, comp.<param>, send.level|tap|pan (with dest), gain, phantom"
    )
