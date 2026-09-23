"""Full-board presets stored as JSON files, and board diffs."""

from __future__ import annotations

import json
import math
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import model
from .safety import describe_raw

ALL_LEAVES: list[str] = list(model.LEAF_KINDS)


def slug(name: str) -> str:
    s = re.sub(r"[^\w\-]+", "-", name.strip(), flags=re.UNICODE).strip("-")
    if not s:
        raise model.ParamError("preset name is empty")
    return s[:80]


@dataclass
class Preset:
    name: str
    path: Path
    saved_at: str
    notes: str
    values: dict[str, Any]


def clean(raw: dict[str, Any]) -> dict[str, Any]:
    return {a: (None if isinstance(v, float) and math.isnan(v) else v) for a, v in raw.items()}


def write(dir_: Path, name: str, notes: str, values: dict[str, Any], mixer_info: dict[str, str] | None) -> Path:
    dir_.mkdir(parents=True, exist_ok=True)
    path = dir_ / f"{slug(name)}.json"
    doc = {
        "name": name,
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "notes": notes,
        "mixer": mixer_info or {},
        "values": clean(values),
    }
    path.write_text(json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8")
    return path


def read(dir_: Path, name: str) -> Preset:
    path = dir_ / f"{slug(name)}.json"
    if not path.exists():
        known = ", ".join(p.stem for p in sorted(dir_.glob("*.json"))) or "none"
        raise model.ParamError(f"No preset {name!r}. Saved presets: {known}")
    doc = json.loads(path.read_text(encoding="utf-8"))
    return Preset(doc.get("name", path.stem), path, doc.get("saved_at", ""), doc.get("notes", ""), doc["values"])


def listing(dir_: Path) -> list[dict[str, str]]:
    out = []
    for p in sorted(dir_.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        out.append({"name": doc.get("name", p.stem), "file": p.name, "saved_at": doc.get("saved_at", ""), "notes": doc.get("notes", "")})
    return out


def prune(dir_: Path, prefix: str, keep: int) -> None:
    files = sorted(dir_.glob(f"{prefix}*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in files[keep:]:
        p.unlink(missing_ok=True)


def same(a: Any, b: Any) -> bool:
    if isinstance(a, float) and isinstance(b, float):
        if math.isnan(a) or math.isnan(b):
            return math.isnan(a) and math.isnan(b)
        return abs(a - b) < 1e-4
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return abs(float(a) - float(b)) < 1e-4
    return a == b


def differences(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    """Addresses whose value differs (skips values missing on either side)."""
    return [a for a in ALL_LEAVES if _known(before.get(a)) and _known(after.get(a)) and not same(before[a], after[a])]


def _known(v: Any) -> bool:
    return v is not None and not (isinstance(v, float) and math.isnan(v))


def param_label(address: str, names: dict[model.Strip, str]) -> str:
    s = model.strip_of(address)
    if s is None:
        return address
    rel = address[len(s.base) + 1 :] if address != s.base else ""
    return f"{model.label(s, names)} {rel}".strip()


def diff_lines(before: dict[str, Any], after: dict[str, Any], names: dict[model.Strip, str]) -> list[str]:
    return [
        f"{param_label(a, names)}: {describe_raw(a, before[a])} -> {describe_raw(a, after[a])}"
        for a in differences(before, after)
    ]
