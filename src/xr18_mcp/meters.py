"""Meter decoding and level analysis.

X-Air meter blobs: little-endian int32 count, then count x int16 in 1/256 dBFS.
X32 meter blobs: little-endian int32 count, then count x float32 linear (1.0 = 0 dBFS).
Bank layouts (which value belongs to which strip) live in each mixer profile.
"""

from __future__ import annotations

import math
import struct
from collections.abc import Callable
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


def decode_float(blob: bytes) -> list[float]:
    if len(blob) < 4:
        return []
    (count,) = struct.unpack_from("<i", blob, 0)
    count = min(count, (len(blob) - 4) // 4)
    return [20 * math.log10(v) if v > 10 ** (SILENCE / 20) else SILENCE for v in struct.unpack_from(f"<{count}f", blob, 4)]


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


def analyse(
    frames: list[bytes],
    layout: list[tuple[str, Strip | None, list[int]]],
    decoder: Callable[[bytes], list[float]] = decode,
) -> list[Level]:
    decoded = [d for d in (decoder(f) for f in frames) if d]
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
