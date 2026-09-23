"""Conversions between the XR18's normalized OSC values (0.0-1.0) and human units.

Curves are taken from xair-api-python (MIT) and checked against the mixer's own
/node text readout. Two fixes relative to that library: EQ Q runs log 10 -> 0.3
(inverted), and gate mode order is EXP2, EXP3, EXP4, GATE, DUCK.
"""

from __future__ import annotations

import math
import re

NEG_INF = float("-inf")


def lin_to(lo: float, hi: float, v: float) -> float:
    return lo + (hi - lo) * v


def lin_from(lo: float, hi: float, x: float) -> float:
    return (x - lo) / (hi - lo)


def log_to(lo: float, hi: float, v: float) -> float:
    return lo * math.exp(math.log(hi / lo) * v)


def log_from(lo: float, hi: float, x: float) -> float:
    return math.log(x / lo) / math.log(hi / lo)


def fader_to_db(v: float) -> float:
    """Normalized fader/send level -> dB (4-segment X-Air curve)."""
    if v >= 1:
        return 10.0
    if v >= 0.5:
        return v * 40 - 30
    if v >= 0.25:
        return v * 80 - 50
    if v >= 0.0625:
        return v * 160 - 70
    if v > 0:
        return v * 480 - 90
    return NEG_INF


def db_to_fader(db: float) -> float:
    if db >= 10:
        return 1.0
    if db >= -10:
        return (db + 30) / 40
    if db >= -30:
        return (db + 50) / 80
    if db >= -60:
        return (db + 70) / 160
    if db > -90:
        return (db + 90) / 480
    return 0.0


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


_FREQ_RE = re.compile(r"^(\d+)k(\d*)$")


def parse_freq(text: str) -> float:
    """Parse mixer frequency text: '129.1', '1k98', '19k32', '10k5'."""
    m = _FREQ_RE.match(text)
    if m:
        whole, frac = m.groups()
        return float(f"{whole}.{frac or 0}") * 1000
    return float(text)


def parse_db(text: str) -> float:
    return NEG_INF if text in ("-oo", "-inf") else float(text)


def fmt_db(db: float) -> str:
    if db == NEG_INF:
        return "-inf"
    r = round(db, 1)
    return "0.0" if r == 0 else f"{r:+.1f}"


def fmt_freq(hz: float) -> str:
    return f"{hz / 1000:.2f}k" if hz >= 1000 else f"{hz:.0f}"
