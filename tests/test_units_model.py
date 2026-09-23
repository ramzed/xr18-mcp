import math

import pytest

from xr18_mcp import model, nodes, units
from xr18_mcp.model import ParamError, Strip


# Raw values and the mixer's own /node readout, captured from a live XR18 (fw 1.25).
@pytest.mark.parametrize(
    "raw, db",
    [(0.7653958797454834, 0.6), (0.2971652150154114, -26.2), (0.567937433719635, -7.3), (0.7497556209564209, 0.0), (0.0, units.NEG_INF)],
)
def test_fader_curve_matches_mixer(raw, db):
    got = model.FADER.from_raw(raw)
    if db == units.NEG_INF:
        assert got == db
    else:
        assert got == pytest.approx(db, abs=0.05)


@pytest.mark.parametrize("db", [-80, -45.5, -30, -20, -10, -3, 0, 4.5, 10])
def test_fader_round_trip(db):
    assert units.fader_to_db(model.FADER.to_raw(db)) == pytest.approx(db, abs=1e-3)


def test_fader_accepts_inf_text():
    assert model.FADER.to_raw("-inf") == 0.0
    assert model.FADER.to_raw(-200) == 0.0


@pytest.mark.parametrize(
    "key, raw, human",
    [
        ("eq/#/q", 0.6338028311729431, 1.1),  # Q runs log 10 -> 0.3
        ("eq/#/q", 0.4647887349128723, 2.0),
        ("eq/#/f", 0.27000001072883606, 129.1),
        ("eq/#/g", 0.4833333194255829, -0.5),
        ("preamp/hpf", 0.3199999928474426, 52),
        ("gate/thr", 0.11874999850988388, -70.5),
        ("gate/hold", 0.8600000143051147, 399),
        ("gate/release", 0.7900000214576721, 983),
        ("dyn/knee", 0.20000000298023224, 1),
        ("headamp/gain", 0.1666666716337204, 0.0),
    ],
)
def test_kinds_match_mixer_readout(key, raw, human):
    assert model.KINDS[key].from_raw(raw) == pytest.approx(human, rel=0.01, abs=0.6)


def test_enum_orders_match_mixer():
    assert model.KINDS["gate/mode"].from_raw(3) == "GATE"
    assert model.KINDS["mix/#/tap"].from_raw(3) == "PRE"
    assert model.KINDS["mix/#/tap"].to_raw("pre") == 3
    assert model.KINDS["eq/#/type"].from_raw(4) == "HShv"
    assert model.KINDS["dyn/ratio"].from_raw(5) == "3.0"


def test_range_errors():
    with pytest.raises(ParamError):
        model.KINDS["eq/#/g"].to_raw(20)
    with pytest.raises(ParamError):
        model.KINDS["config/name"].to_raw("this name is far too long")
    with pytest.raises(ParamError):
        model.KINDS["mix/#/tap"].to_raw("sideways")


def test_parse_freq():
    assert units.parse_freq("1k98") == 1980
    assert units.parse_freq("19k32") == 19320
    assert units.parse_freq("10k5") == 10500
    assert units.parse_freq("129.1") == 129.1


def test_node_parsing_captured_lines():
    text = '/ch/01/config "Guitar" 2 In01 U01\n/ch/01/mix ON   -oo ON 0.0000\n/ch/02/config "" 0 In02 U02\n'
    lines = nodes.parse_lines(text)
    assert lines["ch/01/config"] == ["Guitar", "2", "In01", "U01"]
    assert lines["ch/02/config"][0] == ""
    named = nodes.named("ch/01/mix", lines["ch/01/mix"], nodes.layout_of("ch/01/mix"))
    assert named == {"on": "ON", "fader": "-oo", "lr": "ON", "pan": "0.0000"}


def test_leaf_map_covers_board():
    assert len(model.LEAF_KINDS) > 2800
    assert isinstance(model.kind_of("/ch/16/mix/03/level"), model.Fader)
    assert model.kind_of("/ch/01/mix/02/pan") is None  # even bus sends have no pan
    assert not any(a.startswith("/-prefs") for a in model.LEAF_KINDS)


NAMES = {Strip("ch", 1): "Guitar", Strip("ch", 16): "CLICK", Strip("ch", 3): "Vox Pavel", Strip("ch", 4): "Vox Anna", Strip("bus", 2): "Pavel IEM"}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("ch3", Strip("ch", 3)), ("3", Strip("ch", 3)), ("Channel 12", Strip("ch", 12)), ("bus 2", Strip("bus", 2)),
        ("main", Strip("lr")), ("aux", Strip("aux")), ("fx2", Strip("fxrtn", 2)), ("fxsend4", Strip("fxsend", 4)),
        ("dca1", Strip("dca", 1)), ("guitar", Strip("ch", 1)), ("click", Strip("ch", 16)), ("pavel iem", Strip("bus", 2)),
    ],
)
def test_resolve(text, expected):
    assert model.resolve(text, NAMES) == expected


def test_resolve_ambiguous_and_unknown():
    with pytest.raises(ParamError, match="ambiguous"):
        model.resolve("vox", NAMES)
    with pytest.raises(ParamError, match="No channel"):
        model.resolve("drums", NAMES)
    with pytest.raises(ParamError, match="does not exist"):
        model.resolve("ch17", NAMES)
    with pytest.raises(ParamError, match="can't be used"):
        model.resolve("lr", NAMES, allowed=("ch",))


def test_friendly_addresses():
    ch3 = Strip("ch", 3)
    assert model.friendly_address(ch3, "fader")[0] == "/ch/03/mix/fader"
    assert model.friendly_address(ch3, "eq.2.freq")[0] == "/ch/03/eq/2/f"
    assert model.friendly_address(ch3, "send.level", Strip("bus", 2))[0] == "/ch/03/mix/02/level"
    assert model.friendly_address(ch3, "send.level", Strip("fxsend", 1))[0] == "/ch/03/mix/07/level"
    assert model.friendly_address(Strip("dca", 2), "fader")[0] == "/dca/2/fader"
    with pytest.raises(ParamError):
        model.friendly_address(ch3, "eq.5.gain")
    with pytest.raises(ParamError):
        model.friendly_address(Strip("bus", 1), "gate.thr")
    with pytest.raises(ParamError):
        model.friendly_address(ch3, "send.pan", Strip("bus", 2))
    assert not math.isnan(model.friendly_address(ch3, "hpf")[1].to_raw(100))
