"""Meter decoding and level analysis.

X-Air meter blobs: little-endian int32 count, then count x int16 in 1/256 dBFS.
/meters/1 (40 values): ch1-16, aux L/R, fxrtn1-4 L/R, bus1-6, fxsend1-4, LR L/R, monitor L/R.
/meters/2 (36 values): mic inputs 1-16, aux in L/R, USB 1-18.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass

from .model import Strip

SILENCE = -128.0
CLIP_DB = -0.5
HOT_DB = -6.0
LOW_DB = -40.0
NO_SIGNAL_DB = -80.0


def decode(blob: bytes) -> list[float]:
    if len(blob) < 4:
        return []
    (count,) = struct.unpack_from("<i", blob, 0)
    count = min(count, (len(blob) - 4) // 2)
    return [v / 256.0 for v in struct.unpack_from(f"<{count}h", blob, 4)]


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


BANKS = {"channels": ("/meters/1", _bank1_layout()), "inputs": ("/meters/2", _bank2_layout())}


@dataclass
class Level:
    id: str
    strip: Strip | None
    peak: float
    avg: float

    @property
    def status(self) -> str:
        if self.peak <= SILENCE + 0.5:
            return "digital silence (no source)"
        if self.peak < NO_SIGNAL_DB:
            return "no signal (noise floor)"
        if self.peak >= CLIP_DB:
            return "CLIPPING"
        if self.peak > HOT_DB:
            return "hot"
        if self.peak < LOW_DB:
            return "low"
        return "ok"


def analyse(frames: list[bytes], layout: list[tuple[str, Strip | None, list[int]]]) -> list[Level]:
    decoded = [d for d in (decode(f) for f in frames) if d]
    out = []
    for ident, strip, idxs in layout:
        vals = [max(fr[i] for i in idxs) for fr in decoded if max(idxs) < len(fr)]
        if not vals:
            continue
        peak = max(vals)
        mean_amp = sum(10 ** (v / 20) for v in vals) / len(vals)
        avg = 20 * math.log10(mean_amp) if mean_amp > 0 else SILENCE
        out.append(Level(ident, strip, round(peak, 1), round(avg, 1)))
    return out
