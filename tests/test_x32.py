"""X32/M32 profile (experimental): the address map, and the MCP tools against a fake X32."""

import json
import struct
from pathlib import Path

import pytest

from xr18_mcp import meters, model, nodes
from xr18_mcp import mixer as mixer_mod
from xr18_mcp.mixer import Mixer
from xr18_mcp.model import X32, ParamError, Strip
from xr18_mcp.osc_client import MixerInfo
from xr18_mcp.x32 import source_name

from .fake_mixer import meter_blob_float


def S(kind: str, i: int = 0) -> Strip:
    return Strip(kind, i, "x32")


# A scene saved by X32-Edit (firmware 4.0). Scene files use the same text as /node replies.
SCENE = nodes.parse_lines((Path(__file__).parent / "data" / "x32_fw4.0.scn").read_text(encoding="utf-8"))


# ------------------------------------------------------------------ real console text


def test_every_node_matches_the_x32_edit_scene():
    missing = [p for p in X32.layouts if p not in SCENE]
    assert not missing, f"nodes we read that a real X32 does not have: {missing[:10]}"
    wrong = {p: (len(SCENE[p]), len(prm)) for p, prm in X32.layouts.items() if len(SCENE[p]) != len(prm)}
    assert not wrong, f"parameter count differs (console, ours): {dict(list(wrong.items())[:10])}"


def test_scene_values_parse_as_expected():
    def named(path):
        return nodes.named(path, SCENE[path], X32.layouts[path])

    assert named("ch/01/config") == {"name": "ABC", "icon": "1", "color": "YE", "source": "1"}
    assert named("ch/01/mix/01") == {"on": "ON", "level": "-oo", "pan": "+0", "type": "EQ->", "panFollow": "0"}
    assert named("ch/01/mix/04") == {"on": "ON", "level": "-5.5"}
    assert named("ch/01/dyn")["keysrc"] == "0" and named("ch/01/dyn")["auto"] == "OFF"
    assert named("mtx/01/dyn")["mix"] == "100" and named("main/st/mix") == {"on": "ON", "fader": "-oo", "pan": "+0"}
    assert named("headamp/000") == {"gain": "+0.0", "phantom": "ON"}
    assert named("auxin/07/config")["source"] == "39" and source_name(39) == "USB L"
    raw = {"/ch/01/config/source": 1, **{f"text:/config/routing/IN/{k}": v for k, v in named("config/routing/IN").items()}}
    assert X32.headamp_of(S("ch", 1), raw) == 0


# ------------------------------------------------------------------ profile


def test_for_model():
    assert model.for_model("X32C") is X32 and model.for_model(" m32r ") is X32
    assert model.for_model("XR18") is model.XAIR and model.for_model("MR18") is model.XAIR


def test_strips_and_layouts():
    expect = {
        S("ch", 32): ("ch32", "/ch/32", "Channel 32"),
        S("aux", 8): ("aux8", "/auxin/08", "Aux in 8"),
        S("fxrtn", 1): ("fxrtn1", "/fxrtn/01", "FX return 1"),
        S("bus", 16): ("bus16", "/bus/16", "Mix bus 16"),
        S("mtx", 6): ("mtx6", "/mtx/06", "Matrix 6"),
        S("lr"): ("lr", "/main/st", "Main LR"),
        S("mono"): ("mono", "/main/m", "Main mono (M/C)"),
        S("dca", 8): ("dca8", "/dca/8", "DCA 8"),
    }
    for s, (sid, base, title) in expect.items():
        assert (s.id, s.base, s.title) == (sid, base, title)
    assert len(X32.all_strips) == 32 + 8 + 8 + 16 + 6 + 1 + 1 + 8
    assert S("mtx", 1).eq_bands == 6 and S("dca", 1).eq_bands == 0
    assert X32.strip_of("/main/m/mix/fader") == S("mono")
    ch = dict(S("ch", 1).nodes())
    assert ch["ch/01/mix/01"] == ("on", "level", "pan", "type", "panFollow") and ch["ch/01/mix/16"] == ("on", "level")
    assert dict(S("mtx", 1).nodes())["mtx/01/preamp"] == ("invert",)
    assert dict(S("bus", 1).nodes())["bus/01/mix/06"] == ("on", "level")
    assert dict(S("dca", 1).nodes())["dca/1"] == ("on", "fader")
    with pytest.raises(KeyError):
        X32.layout("fxsend")


def test_leaf_kinds():
    assert X32.kind_of("/ch/01/grp/dca").describe() == "integer 0..255"
    assert isinstance(X32.kind_of("/main/st/mix/fader"), model.Fader)
    assert X32.kind_of("/config/chlink/31-32") is model.BOOL
    assert X32.kind_of("/config/routing/IN/1-8") is model.OPAQUE
    assert X32.kind_of("/headamp/127/phantom") is model.BOOL
    assert X32.kind_of("/ch/01/mix/02/type") is None  # the tap lives on the odd send of a pair
    assert not any(a.startswith(("/-prefs", "/-show", "/-action")) for a in X32.leaves)
    assert X32.main_fader_addrs == ("/main/st/mix/fader", "/main/m/mix/fader")


def test_sends():
    ch3, fa = S("ch", 3), X32.friendly_address
    assert fa(ch3, "send.level", S("bus", 12))[0] == "/ch/03/mix/12/level"
    assert fa(ch3, "send.tap", S("bus", 12))[0] == "/ch/03/mix/11/type"
    assert fa(ch3, "send.tap", S("bus", 11))[0] == "/ch/03/mix/11/type"
    assert fa(ch3, "send.on", S("bus", 2))[0] == "/ch/03/mix/02/on"
    assert fa(ch3, "send.pan", S("bus", 3))[0] == "/ch/03/mix/03/pan"
    with pytest.raises(ParamError, match="odd buses"):
        fa(ch3, "send.pan", S("bus", 4))
    with pytest.raises(ParamError, match=r"level\|tap\|pan\|on"):
        fa(ch3, "send.mute", S("bus", 4))
    with pytest.raises(ParamError, match="bus1-16"):
        X32.send_suffix(S("lr"))
    assert X32.send_suffix(S("mtx", 2)) == "mix/02"
    assert X32.send_dest(ch3, 5) == S("bus", 5) and X32.send_dest(S("bus", 1), 5) == S("mtx", 5)


def test_friendly_params():
    fa = X32.friendly_address
    assert fa(S("ch", 1), "lr")[0] == "/ch/01/mix/st"
    assert fa(S("aux", 2), "trim")[0] == "/auxin/02/preamp/trim"
    assert fa(S("mono"), "comp.thr")[0] == "/main/m/dyn/thr"
    assert fa(S("mtx", 1), "eq.6.gain")[0] == "/mtx/01/eq/6/g"
    with pytest.raises(ParamError, match="has no 'usb'"):
        fa(S("ch", 1), "usb")
    with pytest.raises(ParamError, match="trim, eq.on") as e:
        fa(S("ch", 1), "bogus")
    assert "usb" not in str(e.value)
    with pytest.raises(ParamError, match="usb, eq.on"):
        model.XAIR.friendly_address(Strip("ch", 1), "bogus")


def test_resolve_ids():
    def r(text, **kw):
        return X32.resolve(text, {}, **kw)

    assert r("ch32") == S("ch", 32) and r("aux 3") == S("aux", 3) and r("mtx2") == S("mtx", 2)
    assert r("matrix 6") == S("mtx", 6) and r("mono") == S("mono") and r("main") == S("lr")
    assert r("bus 16") == S("bus", 16) and r("fx return 8") == S("fxrtn", 8)
    assert r("fx2", fx_means="fxsend") == S("fxrtn", 2)  # the X32 has no FX send strips
    with pytest.raises(ParamError, match="aux0 does not exist on the X32"):
        r("aux")
    with pytest.raises(ParamError, match=r"ch33 does not exist on the X32 \(ch1-32\)"):
        r("ch33")
    with pytest.raises(ParamError, match="mtx1, lr, mono"):
        r("nobody")


@pytest.mark.parametrize(
    "src, routing, expected",
    [
        (1, {"1-8": "AN1-8"}, 0),  # In01 <- local input 1
        (12, {"9-16": "AN9-16"}, 11),  # In12 <- local input 12
        (9, {"9-16": "AN1-8"}, 0),  # block 9-16 patched to local inputs 1-8
        (3, {"1-8": "A1-8"}, 34),  # AES50 A 3
        (17, {"17-24": "B1-8"}, 80),  # AES50 B 1
        (25, {"25-32": "B41-48"}, 120),  # AES50 B 41
        (5, {"1-8": "CARD1-8"}, None),
        (5, {}, None),  # routing unknown
        (33, {"1-8": "AN1-8"}, None),  # Aux 1 is not a channel input
        ("x", {}, None),
    ],
)
def test_headamp_of(src, routing, expected):
    ch = S("ch", 1)
    raw = {"/ch/01/config/source": src, **{f"text:/config/routing/IN/{b}": v for b, v in routing.items()}}
    assert X32.headamp_of(ch, raw) == expected


def test_preamp_helpers():
    assert X32.headamp_of(S("aux", 1), {}) is None and X32.source_reads(S("fxrtn", 1)) == []
    assert X32.headamp_addr(5, "gain") == "/headamp/005/gain"
    assert [X32.headamp_label(n) for n in (0, 32, 79, 127)] == [
        "local preamp 1", "AES50-A preamp 1", "AES50-A preamp 48", "AES50-B preamp 48",
    ]
    assert not X32.uses_usb(S("ch", 1), {})


def test_source_names():
    got = [source_name(v) for v in (0, 1, 32, 33, 38, 39, 40, 41, 48, 49, 64, None)]
    assert got == ["OFF", "In01", "In32", "Aux1", "Aux6", "USB L", "USB R", "FX1L", "FX4R", "Bus01", "Bus16", "?"]
    assert X32.source_label(S("fxrtn", 1), {}, {}) == "FX"
    assert X32.source_label(S("aux", 7), {}, {"/auxin/07/config/source": 39}) == "USB L"


def test_decode_float():
    assert meters.decode_float(meter_blob_float([-12.0, 0.0, -128.0])) == pytest.approx([-12.0, 0.0, -128.0], abs=0.01)
    assert meters.decode_float(b"\x01") == []
    assert meters.decode_float(struct.pack("<i", 10) + struct.pack("<f", 1.0)) == [0.0]


# ------------------------------------------------------------------ connection


async def test_port_probing_finds_x32(fake32, tmp_path, monkeypatch):
    monkeypatch.setattr(mixer_mod, "XAIR_PORT", 9)  # nothing answers there
    monkeypatch.setattr(mixer_mod, "X32_PORT", fake32.port)
    m = Mixer(tmp_path, host="127.0.0.1", timeout=0.05)
    await m.ensure()
    assert m.profile is X32 and m.info.port == fake32.port
    m.drop()


async def test_discovery_uses_reported_port(fake32, tmp_path, monkeypatch):
    found = [MixerInfo("127.0.0.1", "found", "X32", "4.06", port=fake32.port)]
    monkeypatch.setattr(mixer_mod, "discover", lambda: found)
    m = Mixer(tmp_path, timeout=0.05)
    await m.ensure()
    assert m.profile is X32
    m.drop()


# ------------------------------------------------------------------ tools


async def test_status_and_overview(rig32):
    status = await rig32.call("mixer_status")
    assert "Connected to X32C 'FAKE-X32'" in status and "EXPERIMENTAL" in status
    assert "Highlighted scene/cue/snippet: 0" in status
    rig32.set("/ch/01/config/name", "Kick")
    rig32.set("/headamp/000/phantom", 1)
    rig32.set("/ch/01/mix/02/level", 0.5)
    rig32.set("/ch/01/mix/01/type", 3)
    rig32.set("/ch/01/mix/03/level", 0.5)
    rig32.set("/ch/01/mix/03/on", 0)
    rig32.set("/ch/02/config/source", 33)
    rig32.set("/ch/02/mix/st", 0)
    rig32.set("/ch/09/preamp/invert", 1)
    rig32.set("/config/routing/IN/9-16", 4)
    rig32.set("/bus/03/mix/st", 1)
    rig32.set("/config/mute/6", 1)
    out = await rig32.call("board_overview")
    lines = out.splitlines()
    assert lines[0].startswith("X32C 'FAKE-X32' at 127.0.0.1 (fw 4.06) X32 support is EXPERIMENTAL")
    assert "  ch1: Kick | In01 | gain +0.0 48V | HPF off | -inf | on | bus2 -10.0 PRE, bus3 -10.0 IN off" in lines
    assert "  ch2: - | Aux1 | - | HPF off | -inf | on notLR | no sends" in lines
    assert "  ch9: - | In09 | gain +0.0 inv | HPF off | -inf | on | no sends" in lines
    assert "  aux1: - | Aux1 | - | - | -inf | on notLR | no sends" in lines
    assert "  aux8: - | USB R | - | - | -inf | on notLR | no sends" in lines
    assert "  bus3: - | 0.0 | on | to LR" in lines and "  mtx6: - | 0.0 | on" in lines
    assert "  mono: - | 0.0 | on" in lines
    assert "FX slots: 1: HALL, 2: HALL, 3: HALL, 4: HALL, 5: HALL, 6: HALL, 7: HALL, 8: HALL" in lines
    assert lines[-1] == "Mute groups engaged: 6"


async def test_channel_detail(rig32):
    out = await rig32.call("channel_detail", target="ch1")
    assert "send -> bus2:" in out and "headamp/000: gain=" in out
    assert "send -> mtx1:" in await rig32.call("channel_detail", target="bus1")


async def test_send_preamp_and_guardrails(rig32):
    out = await rig32.call("set_send", source="ch1", dest="bus2", db=-10, tap="PRE")
    assert rig32.get("/ch/01/mix/01/type") == 3 and rig32.get("/ch/01/mix/02/level") == pytest.approx(0.5, abs=0.01)
    assert "ch1 -> bus2 send.tap" in out
    out = await rig32.call("set_preamp", target="ch1", gain_db=8)
    assert "local preamp 1" in out and rig32.get("/headamp/000/gain") == pytest.approx(20 / 72, abs=0.001)
    await rig32.fails("set_preamp", "phantom power ON", target="ch1", phantom=True)
    await rig32.fails("set_fader", "main LR goes above", target="lr", db=3)
    await rig32.fails("set_mute", "mutes the main LR output", targets=["mono"])
    await rig32.fails("setup_channels", "has no 'usb'", channels=[{"channel": "ch1", "usb": True}])
    out = await rig32.call(
        "apply_changes", changes=[{"target": "ch4", "param": "send.on", "dest": "bus5", "value": "off"}]
    )
    assert rig32.get("/ch/04/mix/05/on") == 0
    undo = await rig32.call("undo")
    assert "back to ON" in undo and rig32.get("/ch/04/mix/05/on") == 1
    await rig32.fails("osc_get", "valid X32 address", address="/nope")


async def test_meters_and_gain_check(rig32):
    levels = [-128.0] * 70
    levels[0], levels[1], levels[63] = -30.0, -12.0, -6.0
    rig32.fake.meters = {"/meters/0": levels}
    out = await rig32.call("read_meters", seconds=0.1)
    assert "ch1: peak -30.0" in out and "bus16: peak -6.0" in out
    assert rig32.fake.meter_args[0] == ("/meters/0", 0, 0, 1)
    await rig32.fails("read_meters", "not available on the X32", scope="inputs")
    rig32.set("/ch/02/config/source", 39)
    out = await rig32.call("gain_check", targets=["ch1", "ch2"], seconds=0.1)
    assert "ch1: peak -30.0, gain +0.0 - suggest gain +0.0 -> +18.0 dB" in out
    assert "ch2: peak -12.0 - not a mic preamp input" in out


async def test_scenes(rig32):
    assert (await rig32.call("snapshot_list")).endswith("All 100 snapshot slots are empty/unnamed.")
    assert await rig32.call("snapshot_save", slot=0, name="Song1") == "Saved snapshot 0 'Song1'."
    assert rig32.fake.meter_args[-1] == ("scene", 0, "Song1", "")  # /save ,siss scene <n> <name> <note>
    rig32.set("/-show/showfile/scene/007/hasdata", 1)  # a scene saved on the console without a name
    listing = await rig32.call("snapshot_list")
    assert "0: Song1" in listing and "7: (no name)" in listing
    await rig32.fails("snapshot_save", "slot 7 already holds snapshot '\\(no name\\)'", slot=7, name="x")
    await rig32.fails("snapshot_save", "slot must be 0-99", slot=100, name="x")
    await rig32.fails("snapshot_load", "slot 3 is empty", slot=3, confirm=True)
    out = await rig32.call("snapshot_load", slot=7, confirm=True)
    assert out.startswith("Sent the load for snapshot 7 '(no name)'") and "Confirm Pop-Ups" in out
    assert await rig32.mixer.get_raw(["/-show/prepos/current"]) == {"/-show/prepos/current": 7}  # load has arrived
    assert rig32.fake.snap_loads == [7]


async def test_presets_are_tied_to_the_mixer_type(rig32):
    await rig32.call("save_preset", name="x32-a")
    doc = json.loads((rig32.dir / "presets" / "x32-a.json").read_text(encoding="utf-8"))
    assert doc["profile"] == "x32" and "/main/st/mix/fader" in doc["values"]
    rig32.set("/main/st/mix/fader", 0.5)
    assert "main/st" not in await rig32.call("diff_preset", name="x32-a", other="x32-a")
    assert "lr mix/fader: 0.0 dB -> -10.0 dB" in await rig32.call("diff_preset", name="x32-a")
    out = await rig32.call("load_preset", name="x32-a", confirm=True)
    assert "Loaded preset 'x32-a'" in out and rig32.get("/main/st/mix/fader") == 0.75
    xr = {"name": "xr", "saved_at": "", "notes": "", "values": {"/lr/mix/fader": 0.5}}  # no profile key: an XR18 preset
    (rig32.dir / "presets" / "xr.json").write_text(json.dumps(xr), encoding="utf-8")
    await rig32.fails("load_preset", "different mixer type", name="xr")
    await rig32.fails("diff_preset", "different mixer type", name="x32-a", other="xr")
