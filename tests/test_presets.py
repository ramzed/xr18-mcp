import json
import math
import os
import time

import pytest

from xr18_mcp import model, presets
from xr18_mcp.model import Strip


def test_slug():
    assert presets.slug("Song 3 / final!") == "Song-3-final"
    assert presets.slug("  Пісня 1 ") == "Пісня-1"  # unicode names are kept
    assert len(presets.slug("x" * 200)) == 80
    with pytest.raises(model.ParamError):
        presets.slug(" !!! ")


def test_write_read_round_trip(tmp_path):
    values = {"/ch/01/mix/fader": 0.5, "/fx/1/par/64": float("nan"), "/ch/01/config/name": "Kick"}
    path = presets.write(tmp_path, "Song 1", "notes", values, {"ip": "1.2.3.4"})
    assert path.name == "Song-1.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["values"]["/fx/1/par/64"] is None  # NaN is not valid JSON; stored as null
    p = presets.read(tmp_path, "Song 1")
    assert (p.name, p.notes, p.values["/ch/01/config/name"]) == ("Song 1", "notes", "Kick")
    assert p.saved_at


def test_read_missing_lists_known(tmp_path):
    presets.write(tmp_path, "a", "", {}, None)
    with pytest.raises(model.ParamError, match="Saved presets: a"):
        presets.read(tmp_path, "b")
    with pytest.raises(model.ParamError, match="none"):
        presets.read(tmp_path / "empty", "b")


def test_listing_newest_first_and_skips_broken(tmp_path):
    presets.write(tmp_path, "old", "", {}, None)
    os.utime(tmp_path / "old.json", (time.time() - 100, time.time() - 100))
    presets.write(tmp_path, "new", "with notes", {}, None)
    (tmp_path / "broken.json").write_text("{not json", encoding="utf-8")
    items = presets.listing(tmp_path)
    assert [i["name"] for i in items] == ["new", "old"]
    assert items[0]["notes"] == "with notes"


def test_prune_keeps_newest(tmp_path):
    for i in range(5):
        presets.write(tmp_path, f"session-start-{i}", "", {}, None)
        os.utime(tmp_path / f"session-start-{i}.json", (1000 + i, 1000 + i))
    presets.write(tmp_path, "keep-me", "", {}, None)
    presets.prune(tmp_path, "session-start-", keep=2)
    left = sorted(p.stem for p in tmp_path.glob("*.json"))
    assert left == ["keep-me", "session-start-3", "session-start-4"]


def test_same():
    assert presets.same(0.5, 0.50001)
    assert not presets.same(0.5, 0.6)
    assert presets.same(float("nan"), float("nan"))
    assert not presets.same(float("nan"), 0.5)
    assert presets.same(1, 1.0)
    assert presets.same("a", "a") and not presets.same("a", "b")
    assert not presets.same(True, 2)


def test_differences_skip_unknown_values():
    a = {"/ch/01/mix/fader": 0.5, "/ch/02/mix/fader": 0.5, "/fx/1/par/64": math.nan, "/ch/03/mix/fader": None}
    b = {"/ch/01/mix/fader": 0.6, "/ch/02/mix/fader": 0.5, "/fx/1/par/64": 0.1, "/ch/03/mix/fader": 0.2}
    assert presets.differences(a, b) == ["/ch/01/mix/fader"]


def test_labels_and_diff_lines():
    names = {Strip("ch", 1): "Kick"}
    assert presets.param_label("/ch/01/mix/fader", names) == "ch1 (Kick) mix/fader"
    assert presets.param_label("/dca/2", names) == "dca2"
    assert presets.param_label("/headamp/01/gain", names) == "/headamp/01/gain"
    lines = presets.diff_lines({"/ch/01/mix/fader": 0.0}, {"/ch/01/mix/fader": 0.75}, names)
    assert lines == ["ch1 (Kick) mix/fader: -inf -> 0.0 dB"]
