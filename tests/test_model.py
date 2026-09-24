import math

import pytest

from xr18_mcp import model, units
from xr18_mcp.model import ParamError, Strip

# ------------------------------------------------------------------ kinds


# Raw values and the mixer's own /node readout, captured from a live XR18 (firmware 1.25).
@pytest.mark.parametrize(
    "raw, db",
    [(0.7653958797454834, 0.6), (0.2971652150154114, -26.2), (0.567937433719635, -7.3), (0.7497556209564209, 0.0)],
)
def test_fader_matches_mixer_readout(raw, db):
    assert model.FADER.from_raw(raw) == pytest.approx(db, abs=0.05)


def test_fader_kind():
    f = model.FADER
    assert f.from_raw(0.0) == units.NEG_INF
    for text in ("-inf", "-oo", "off", "-Infinity"):
        assert f.to_raw(text) == 0.0
    assert f.to_raw("-10dB") == pytest.approx(0.5)
    assert f.to_raw(-200) == 0.0
    assert f.to_raw(20) == 1.0
    with pytest.raises(ParamError):
        f.to_raw(float("nan"))
    assert f.fmt(-3.0) == "-3.0 dB"
    assert f.fmt(units.NEG_INF) == "-inf"
    assert "dB" in f.describe()


@pytest.mark.parametrize(
    "key, raw, human",
    [
        ("eq/#/q", 0.6338028311729431, 1.1),  # Q is log 10 -> 0.3
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


def test_lin_kind():
    k = model.Lin(-15, 15, "dB")
    assert k.to_raw("3dB") == pytest.approx(0.6)
    assert k.fmt(3.0) == "3 dB"
    assert k.describe() == "-15..15 dB"
    assert model.Lin(0, 5).fmt(2.0) == "2"
    with pytest.raises(ParamError, match="out of range"):
        k.to_raw(16)
    with pytest.raises(ParamError, match="not a number"):
        k.to_raw("loud")


def test_log_kind():
    k = model.KINDS["eq/#/f"]
    assert k.to_raw("1k") == pytest.approx(units.log_from(20, 20000, 1000), abs=1e-6)
    assert k.to_raw(1000) == k.to_raw("1k")
    assert k.describe() == "20..20000 Hz"
    q = model.KINDS["eq/#/q"]
    assert q.describe() == "0.3..10"
    assert q.to_raw(10) == 0.0
    with pytest.raises(ParamError, match="out of range"):
        q.to_raw(20)


def test_bool_kind():
    b = model.BOOL
    assert b.from_raw(1) is True
    for on in ("on", "TRUE", "yes", "1", True, 1):
        assert b.to_raw(on) == 1
    for off in ("off", "false", "no", "0", False, 0):
        assert b.to_raw(off) == 0
    with pytest.raises(ParamError):
        b.to_raw("maybe")
    assert b.fmt(True) == "ON" and b.fmt(False) == "OFF"
    assert b.describe() == "on/off"


def test_enum_kind():
    tap = model.KINDS["mix/#/tap"]
    assert tap.from_raw(3) == "PRE"
    assert tap.from_raw(99) == "99"
    assert tap.to_raw("pre") == 3
    assert tap.to_raw(4) == 4
    assert tap.to_raw(4.0) == 4
    with pytest.raises(ParamError):
        tap.to_raw(True)
    with pytest.raises(ParamError):
        tap.to_raw("sideways")
    assert "PRE" in tap.describe()


def test_enum_orders_match_mixer():
    assert model.KINDS["gate/mode"].from_raw(3) == "GATE"
    assert model.KINDS["eq/#/type"].from_raw(4) == "HShv"
    assert model.KINDS["dyn/ratio"].from_raw(5) == "3.0"


def test_int_and_str_kinds():
    assert model.BITMASK.to_raw("5") == 5
    with pytest.raises(ParamError):
        model.BITMASK.to_raw(16)
    assert model.BITMASK.describe() == "integer 0..15"
    name = model.KINDS["config/name"]
    assert name.to_raw("Kick") == "Kick"
    assert name.fmt("Kick") == '"Kick"'
    assert "12" in name.describe()
    with pytest.raises(ParamError, match="longer"):
        name.to_raw("this name is far too long")


def test_base_and_opaque_kinds():
    k = model.Kind()
    assert k.from_raw(3) == 3 and k.to_raw(3) == 3 and k.fmt(3) == "3" and k.describe() == "raw value"
    assert model.OPAQUE.to_raw(7) == 7


# ----------------------------------------------------------------- layout


def test_strip_ids_bases_titles():
    expect = {
        Strip("ch", 3): ("ch3", "/ch/03", "Channel 3"),
        Strip("aux"): ("aux", "/rtn/aux", "Aux in"),
        Strip("fxrtn", 2): ("fxrtn2", "/rtn/2", "FX return 2"),
        Strip("bus", 6): ("bus6", "/bus/6", "Bus 6"),
        Strip("fxsend", 1): ("fxsend1", "/fxsend/1", "FX send 1"),
        Strip("lr"): ("lr", "/lr", "Main LR"),
        Strip("dca", 4): ("dca4", "/dca/4", "DCA 4"),
    }
    for s, (sid, base, title) in expect.items():
        assert (s.id, s.base, s.title) == (sid, base, title)
    assert len(model.ALL_STRIPS) == 16 + 1 + 4 + 6 + 4 + 1 + 4
    assert Strip("ch", 1).eq_bands == 4 and Strip("lr").eq_bands == 6 and Strip("dca", 1).eq_bands == 0
    assert Strip("aux").sends_from and not Strip("bus", 1).sends_from
    assert Strip("dca", 1).addr("") == "/dca/1"


def test_node_layouts():
    ch = dict(Strip("ch", 1).nodes())
    assert ch["ch/01/mix/01"] == ("level", "grpon", "tap", "pan")
    assert ch["ch/01/mix/07"] == ("level", "grpon", "tap")
    assert "keysrc" not in dict(Strip("lr").nodes())["lr/dyn"]
    assert dict(Strip("dca", 2).nodes())["dca/2"] == ("on", "fader")
    assert dict(Strip("fxsend", 1).nodes())["fxsend/1/mix"] == ("on", "fader")
    with pytest.raises(KeyError):
        model._layout("nope")


def test_send_suffix():
    assert model.send_suffix(Strip("bus", 2)) == "mix/02"
    assert model.send_suffix(Strip("fxsend", 1)) == "mix/07"
    with pytest.raises(ParamError):
        model.send_suffix(Strip("lr"))


def test_leaf_map():
    assert len(model.LEAF_KINDS) == 2898
    assert not any(a.startswith(("/-prefs", "/-snap", "/-action", "/-stat")) for a in model.LEAF_KINDS)
    kind = model.kind_of
    assert isinstance(kind("/ch/16/mix/03/level"), model.Fader)
    assert kind("/ch/01/mix/02/pan") is None  # even bus sends have no pan
    assert isinstance(kind("/headamp/01/gain"), model.Lin)
    assert kind("/headamp/17/gain") is model.KINDS["headamp/gain"]
    assert kind("/bus/1/geq/1k") is model.KINDS["geq/*"]
    assert kind("/config/solo/level") is model.FADER
    assert kind("/config/chlink/1-2") is model.BOOL
    assert kind("/config/solo/source") is model.OPAQUE
    assert kind("/routing/main/01") is model.OPAQUE
    assert kind("/fx/1/par/01") is model.OPAQUE
    assert kind("/dca/1/on") is model.BOOL


def test_strip_of():
    assert model.strip_of("/ch/03/mix/fader") == Strip("ch", 3)
    assert model.strip_of("/rtn/aux/mix/on") == Strip("aux")
    assert model.strip_of("/dca/2") == Strip("dca", 2)
    assert model.strip_of("/headamp/01/gain") is None


# ------------------------------------------------------------ name lookup

NAMES = {
    Strip("ch", 1): "Guitar", Strip("ch", 16): "CLICK", Strip("ch", 3): "Vox Pavel", Strip("ch", 4): "Vox Anna",
    Strip("bus", 2): "Pavel IEM", Strip("ch", 5): "Bus",
}


@pytest.mark.parametrize(
    "text, expected",
    [
        ("ch3", Strip("ch", 3)), ("3", Strip("ch", 3)), ("Channel 12", Strip("ch", 12)), ("input_07", Strip("ch", 7)),
        ("bus 2", Strip("bus", 2)), ("mon-4", Strip("bus", 4)), ("main", Strip("lr")), ("master", Strip("lr")),
        ("aux", Strip("aux")), ("aux in", Strip("aux")), ("fx2", Strip("fxrtn", 2)), ("fx return 3", Strip("fxrtn", 3)),
        ("fxsend4", Strip("fxsend", 4)), ("fx send 2", Strip("fxsend", 2)), ("dca1", Strip("dca", 1)),
        ("guitar", Strip("ch", 1)), ("click", Strip("ch", 16)), ("pavel iem", Strip("bus", 2)), ("anna", Strip("ch", 4)),
        ("gui", Strip("ch", 1)),
    ],
)
def test_resolve(text, expected):
    assert model.resolve(text, NAMES) == expected


def test_resolve_fx_meaning_and_errors():
    assert model.resolve("fx1", NAMES, fx_means="fxsend") == Strip("fxsend", 1)
    with pytest.raises(ParamError, match="ambiguous"):
        model.resolve("vox", NAMES)
    with pytest.raises(ParamError, match="No channel"):
        model.resolve("drums", NAMES)
    with pytest.raises(ParamError, match="does not exist"):
        model.resolve("ch17", NAMES)
    with pytest.raises(ParamError, match="does not exist"):
        model.resolve("bus7", NAMES)
    with pytest.raises(ParamError, match="can't be used"):
        model.resolve("lr", NAMES, allowed=("ch",))
    # names of disallowed kinds are ignored
    with pytest.raises(ParamError, match="No channel"):
        model.resolve("pavel iem", NAMES, allowed=("ch",))


def test_label():
    assert model.label(Strip("ch", 1), NAMES) == "ch1 (Guitar)"
    assert model.label(Strip("ch", 2), NAMES) == "ch2"


# ------------------------------------------------------- friendly params

CH3 = Strip("ch", 3)


@pytest.mark.parametrize(
    "strip, key, address",
    [
        (CH3, "name", "/ch/03/config/name"), (CH3, "color", "/ch/03/config/color"), (CH3, "fader", "/ch/03/mix/fader"),
        (Strip("dca", 2), "fader", "/dca/2/fader"), (CH3, "on", "/ch/03/mix/on"), (Strip("dca", 2), "on", "/dca/2/on"),
        (CH3, "lr", "/ch/03/mix/lr"), (Strip("lr"), "pan", "/lr/mix/pan"), (CH3, "hpf", "/ch/03/preamp/hpf"),
        (CH3, "hpf.freq", "/ch/03/preamp/hpf"), (CH3, "hpf.on", "/ch/03/preamp/hpon"), (CH3, "invert", "/ch/03/preamp/invert"),
        (Strip("aux"), "usb", "/rtn/aux/preamp/rtnsw"), (Strip("bus", 1), "eq.on", "/bus/1/eq/on"),
        (CH3, "eq.2.freq", "/ch/03/eq/2/f"), (Strip("lr"), "eq.6.q", "/lr/eq/6/q"), (CH3, "gate.threshold", "/ch/03/gate/thr"),
        (Strip("bus", 1), "comp.makeup", "/bus/1/dyn/mgain"), (CH3, "dyn.ratio", "/ch/03/dyn/ratio"),
        (CH3, " Fader ", "/ch/03/mix/fader"),
    ],
)
def test_friendly_addresses(strip, key, address):
    assert model.friendly_address(strip, key)[0] == address


def test_friendly_send_addresses():
    assert model.friendly_address(CH3, "send.level", Strip("bus", 2))[0] == "/ch/03/mix/02/level"
    assert model.friendly_address(CH3, "send.tap", Strip("bus", 2))[0] == "/ch/03/mix/02/tap"
    assert model.friendly_address(CH3, "send.pan", Strip("bus", 3))[0] == "/ch/03/mix/03/pan"
    assert model.friendly_address(Strip("fxrtn", 1), "send.level", Strip("fxsend", 1))[0] == "/rtn/1/mix/07/level"


@pytest.mark.parametrize(
    "strip, key, dest, match",
    [
        (Strip("fxsend", 1), "lr", None, "has no 'lr'"),
        (Strip("fxsend", 1), "pan", None, "has no 'pan'"),
        (Strip("bus", 1), "hpf", None, "has no"),
        (Strip("dca", 1), "eq.on", None, "has no"),
        (CH3, "eq.5.gain", None, "bands 1-4"),
        (CH3, "eq.x.gain", None, "bands 1-4"),
        (CH3, "eq.1.slope", None, "type\\|freq"),
        (Strip("bus", 1), "gate.thr", None, "has no"),
        (CH3, "gate.color", None, "gate parameter"),
        (Strip("aux"), "comp.thr", None, "has no"),
        (CH3, "comp.color", None, "comp parameter"),
        (Strip("bus", 1), "send.level", Strip("bus", 2), "no sends"),
        (CH3, "send.level", None, "destination"),
        (CH3, "send.mute", Strip("bus", 2), "level\\|tap\\|pan"),
        (CH3, "send.pan", Strip("bus", 2), "odd buses"),
        (CH3, "send.pan", Strip("fxsend", 1), "odd buses"),
        (CH3, "sparkle", None, "Unknown parameter"),
    ],
)
def test_friendly_errors(strip, key, dest, match):
    with pytest.raises(ParamError, match=match):
        model.friendly_address(strip, key, dest)


def test_hpf_conversion_is_finite():
    assert not math.isnan(model.friendly_address(CH3, "hpf")[1].to_raw(100))
