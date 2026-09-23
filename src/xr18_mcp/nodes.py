"""Parse the XR18's /node text replies.

A /node request returns one line per node, e.g.
    /ch/01/config "Guitar" 2 In01 U01
    /ch/01/mix ON   -oo ON 0.0000
Values are already in human units as shown by X AIR Edit.
"""

from __future__ import annotations

import re

from . import model

_TOKEN = re.compile(r'"([^"]*)"|(\S+)')


def tokenize(line: str) -> list[str]:
    return [m.group(1) if m.group(1) is not None else m.group(2) for m in _TOKEN.finditer(line)]


def parse_lines(text: str) -> dict[str, list[str]]:
    """'/ch/01/mix ON -oo ...' lines -> {'ch/01/mix': ['ON', '-oo', ...]}."""
    out: dict[str, list[str]] = {}
    for line in text.splitlines():
        toks = tokenize(line)
        if toks and toks[0].startswith("/"):
            out[toks[0][1:]] = toks[1:]
    return out


def named(path: str, values: list[str], params: tuple[str, ...]) -> dict[str, str]:
    """Zip node values with parameter names (tolerates missing trailing values)."""
    return {p: v for p, v in zip(params, values)}


def layout_of(path: str) -> tuple[str, ...] | None:
    return _LAYOUT.get(path)


_LAYOUT: dict[str, tuple[str, ...]] = {p: params for p, params in model.all_nodes()}
