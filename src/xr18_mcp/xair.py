"""X-Air profile (XR18): the OSC map of the mixer this project was built on.

Node layouts (the parameter order inside each /node reply) were read from a
live XR18 running firmware 1.25.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from . import meters
from .model import EQ_BAND, FILTER, GEQ_BANDS, Layout, ParamError, Profile, Strip

if TYPE_CHECKING:
    from .osc_client import OscClient

PORT = 10024

DYN = ("on", "mode", "det", "env", "thr", "ratio", "knee", "mgain", "attack", "hold", "release", "mix", "keysrc", "auto")
DYN_LR = tuple(p for p in DYN if p != "keysrc")


def _sends() -> Layout:
    # Sends 01-06 go to buses (odd ones carry the stereo-pair pan), 07-10 to FX sends.
    out = []
    for i in range(1, 11):
        params = ("level", "grpon", "tap", "pan") if i <= 6 and i % 2 == 1 else ("level", "grpon", "tap")
        out.append((f"mix/{i:02d}", params))
    return out


def _bank1_layout() -> list[tuple[str, Strip | None, list[int]]]:
    out: list[tuple[str, Strip | None, list[int]]] = [(f"ch{i}", Strip("ch", i), [i - 1]) for i in range(1, 17)]
    out.append(("aux", Strip("aux"), [16, 17]))
    out += [(f"fxrtn{i}", Strip("fxrtn", i), [16 + 2 * i, 17 + 2 * i]) for i in range(1, 5)]
    out += [(f"bus{i}", Strip("bus", i), [25 + i]) for i in range(1, 7)]
    out += [(f"fxsend{i}", Strip("fxsend", i), [31 + i]) for i in range(1, 5)]
    out.append(("lr", Strip("lr"), [36, 37]))
    out.append(("monitor", None, [38, 39]))
    return out


def _bank2_layout() -> list[tuple[str, Strip | None, list[int]]]:
    out: list[tuple[str, Strip | None, list[int]]] = [(f"mic{i}", None, [i - 1]) for i in range(1, 17)]
    out.append(("aux-in", None, [16, 17]))
    out += [(f"usb{i}", None, [17 + i]) for i in range(1, 19)]
    return out


class XAirProfile(Profile):
    name = "xair"
    family = "XR18"
    port = PORT
    strip_kinds = ("ch", "aux", "fxrtn", "bus", "fxsend", "lr", "dca")
    counts = {"ch": 16, "aux": 1, "fxrtn": 4, "bus": 6, "fxsend": 4, "lr": 1, "dca": 4}
    singles = ("aux", "lr")
    titles = {
        "ch": "Channel {i}", "aux": "Aux in", "fxrtn": "FX return {i}", "bus": "Bus {i}",
        "fxsend": "FX send {i}", "lr": "Main LR", "dca": "DCA {i}",
    }
    output_kinds = ("bus", "fxsend", "lr", "dca")
    send_dest_kinds = ("bus", "fxsend")
    usb_kinds = ("ch", "aux")
    eq_bands = {"ch": 4, "aux": 4, "fxrtn": 4, "bus": 6, "lr": 6}
    config_bool_nodes = ("chlink", "buslink", "linkcfg", "mute", "amixenable", "amixlock")
    confirm_prefixes = ("/-snap", "/-action", "/-usb", "/-show", "/config", "/routing", "/fx/")
    fx_count = 4
    mute_groups = 4
    snap_slots = range(1, 65)
    snap_index_addr = "/-snap/index"
    meter_banks = {"channels": ("/meters/1", _bank1_layout()), "inputs": ("/meters/2", _bank2_layout())}
    id_patterns = [
        (re.compile(r"^(?:ch|chan|channel|in|input)?\s*0*(\d{1,2})$"), "ch"),
        (re.compile(r"^(?:aux|aux ?in|aux ?return|rtn ?aux|aux ?rtn)$"), "aux"),
        (re.compile(r"^(?:fx ?rtn|fx ?return|rtn|return)\s*(\d{1,2})$"), "fxrtn"),
        (re.compile(r"^(?:bus|mix|mon|monitor|aux ?bus)\s*(\d{1,2})$"), "bus"),
        (re.compile(r"^(?:fx ?send|fxs)\s*(\d{1,2})$"), "fxsend"),
        (re.compile(r"^(?:lr|main|main ?lr|master|stereo|mains)$"), "lr"),
        (re.compile(r"^dca\s*(\d{1,2})$"), "dca"),
    ]
    id_hint = "ch3, bus2, fxsend1, aux, lr, dca1"

    def base(self, s: Strip) -> str:
        return {
            "ch": f"/ch/{s.index:02d}",
            "aux": "/rtn/aux",
            "fxrtn": f"/rtn/{s.index}",
            "bus": f"/bus/{s.index}",
            "fxsend": f"/fxsend/{s.index}",
            "lr": "/lr",
            "dca": f"/dca/{s.index}",
        }[s.kind]

    def layout(self, kind: str) -> Layout:
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

    def global_nodes(self) -> Layout:
        # Nodes outside the strips that belong in a full-board preset. /-prefs (network
        # settings incl. Wi-Fi passwords), /-stat, /-snap and /-action are deliberately excluded.
        return [
            *[(self.headamp_path(n), ("gain", "phantom")) for n in range(1, 17)],
            *[(self.headamp_path(n), ("gain",)) for n in range(17, 25)],
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

    def send_suffix(self, dest: Strip) -> str:
        if dest.kind == "bus":
            return f"mix/{dest.index:02d}"
        if dest.kind == "fxsend":
            return f"mix/{dest.index + 6:02d}"
        raise ParamError(f"{dest.id} is not a send destination (use bus1-6 or fxsend1-4)")

    def send_dest(self, s: Strip, n: int) -> Strip:
        return Strip("bus", n) if n <= 6 else Strip("fxsend", n - 6)

    # --- preamps: channel inputs In01-In16 are the 16 local mic preamps

    def headamp_path(self, n: int) -> str:
        return f"headamp/{n:02d}"

    def source_reads(self, s: Strip) -> list[str]:
        return [s.addr("config/insrc"), s.addr("preamp/rtnsw")]

    def headamp_of(self, s: Strip, raw: dict[str, Any]) -> int | None:
        if s.kind != "ch":
            return None
        src = raw.get(s.addr("config/insrc"))
        return src + 1 if isinstance(src, int) and 0 <= src < 16 else None

    def uses_usb(self, s: Strip, raw: dict[str, Any]) -> bool:
        return s.kind in self.usb_kinds and bool(raw.get(s.addr("preamp/rtnsw")))

    def source_label(self, s: Strip, cfg_text: dict[str, str], raw: dict[str, Any]) -> str:
        if s.kind == "fxrtn":
            return "FX"
        if self.uses_usb(s, raw):
            return cfg_text.get("rtnsrc", "USB") + " (USB)"
        return "Aux in" if s.kind == "aux" else cfg_text.get("insrc", "?")

    def decode_meters(self, blob: bytes) -> list[float]:
        return meters.decode(blob)

    # --- snapshots

    def snap_name_addr(self, slot: int) -> str:
        return f"/-snap/{slot:02d}/name"

    def snap_save(self, c: OscClient, slot: int, name: str) -> None:
        c.send("/-snap/name", name)
        c.send("/-snap/save", slot)

    def snap_load(self, c: OscClient, slot: int) -> None:
        c.send("/-snap/load", slot)


XAIR = XAirProfile()
