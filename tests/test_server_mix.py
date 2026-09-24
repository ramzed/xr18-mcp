"""MCP tools that change the board: mixing, setup, raw OSC, undo and history."""

import pytest

from xr18_mcp import model

FADER = model.FADER
GAIN = model.KINDS["headamp/gain"]


def db(raw):
    return FADER.from_raw(raw)


# ------------------------------------------------------------------ faders


async def test_set_fader_absolute_relative_and_inf(rig):
    out = await rig.call("set_fader", target="ch3", db=-20)
    assert out.splitlines() == ["Done (change #1; undo reverts it):", "  ch3 fader: -inf -> -20.0 dB"]
    assert "-20.0 dB -> -17.0 dB" in await rig.call("set_fader", target="3", delta_db=3)
    assert "-> -inf" in await rig.call("set_fader", target="ch3", db="-inf")
    assert db(rig.get("/ch/03/mix/fader")) == float("-inf")
    assert "/dca" not in await rig.call("set_fader", target="dca1", db=-5)
    assert db(rig.get("/dca/1/fader")) == pytest.approx(-5)


async def test_set_fader_errors_and_confirmation(rig):
    await rig.fails("set_fader", "exactly one", target="ch3")
    await rig.fails("set_fader", "exactly one", target="ch3", db=-10, delta_db=2)
    await rig.fails("set_fader", "is at -inf", target="ch3", delta_db=3)
    msg = await rig.fails("set_fader", "CONFIRMATION REQUIRED", target="ch3", db=0)
    assert "ch3 fader jumps up from -inf to 0.0 dB" in msg
    assert rig.get("/ch/03/mix/fader") == 0.0
    await rig.call("set_fader", target="ch3", db=0, confirm=True)
    assert db(rig.get("/ch/03/mix/fader")) == 0.0
    assert await rig.call("set_fader", target="ch3", db=0) == "Nothing changed - the mixer already had those values."


async def test_set_fader_reports_lost_packets(rig):
    rig.fake.drop_sets = {"/ch/03/mix/fader": 99}
    out = await rig.call("set_fader", target="ch3", db=-20)
    assert "NOT APPLIED" in out and "Some values did not reach the mixer" in out


async def test_set_mute(rig):
    out = await rig.call("set_mute", targets=["ch1", "ch2"])
    assert "  ch1: on -> MUTED" in out and "  ch2: on -> MUTED" in out
    assert "ch1: MUTED -> on" in await rig.call("set_mute", targets=["ch1"], muted=False)
    await rig.fails("set_mute", "mutes 5 channels", targets=["ch3", "ch4", "ch5", "ch6", "ch7"])
    await rig.fails("set_mute", "main LR", targets=["main"])


# ------------------------------------------------------------------- sends


async def test_set_send(rig):
    out = await rig.call("set_send", source="ch1", dest="bus2", db=-15, tap="PRE")
    assert "ch1 -> bus2 send.level: -inf -> -15.0 dB" in out and "send.tap: IN -> PRE" in out
    assert rig.get("/ch/01/mix/02/tap") == 3
    assert "-15.0 dB -> -12.0 dB" in await rig.call("set_send", source="ch1", dest="bus2", delta_db=3)
    await rig.call("set_send", source="aux", dest="fx1", db=-20)  # "fx1" means FX send here
    assert db(rig.get("/rtn/aux/mix/07/level")) == pytest.approx(-20)
    await rig.fails("set_send", "tap can only be set for bus sends", source="ch1", dest="fxsend2", tap="PRE")
    await rig.fails("set_send", "nothing to set", source="ch1", dest="bus2")
    await rig.fails("set_send", "can't be used here", source="bus1", dest="bus2", db=-10)
    await rig.fails("set_send", "can't be used here", source="ch1", dest="lr", db=-10)


# --------------------------------------------------------------- processing


async def test_set_eq(rig):
    out = await rig.call("set_eq", target="ch1", band=2, type="PEQ", freq_hz=2500, gain_db=-3, q=1.4)
    assert "eq.2.freq" in out and "eq.2.gain: 0 dB -> -3 dB" in out and "eq.2.q" in out
    assert "eq.on" in await rig.call("set_eq", target="ch1", on=True)
    assert "eq.6.gain" in await rig.call("set_eq", target="bus1", band=6, gain_db=2)
    await rig.fails("set_eq", "give band", target="ch1", gain_db=2)
    await rig.fails("set_eq", "nothing to set", target="ch1")
    await rig.fails("set_eq", "bands 1-4", target="ch1", band=5, gain_db=2)


async def test_set_hpf(rig):
    out = await rig.call("set_hpf", target="ch1", freq_hz=100)
    assert "ch1 hpf: 89 Hz -> " in out and "hpf.on: OFF -> ON" in out
    assert "hpf.on: ON -> OFF" in await rig.call("set_hpf", target="ch1", on=False)
    out = await rig.call("set_hpf", target="ch1", freq_hz=120, on=False)
    assert "hpf.on" not in out  # already off, only the frequency changes
    await rig.fails("set_hpf", "give freq_hz", target="ch1")
    await rig.fails("set_hpf", "can't be used here", target="bus1", freq_hz=100)


async def test_set_gate_and_compressor(rig):
    out = await rig.call("set_gate", target="ch1", on=True, mode="GATE", threshold_db=-50, range_db=30,
                         attack_ms=5, hold_ms=100, release_ms=300)
    assert "gate.mode: EXP2 -> GATE" in out and "gate.thr" in out and "gate.release" in out
    await rig.fails("set_gate", "nothing to set", target="ch1")
    out = await rig.call("set_compressor", target="bus1", on=True, threshold_db=-20, ratio=3.6, knee=2, makeup_db=3,
                         attack_ms=10, hold_ms=10, release_ms=150, mix_pct=80, auto=True, mode="COMP", detector="RMS")
    assert "comp.ratio: 1.1 -> 4.0" in out and "comp.det: PEAK -> RMS" in out
    await rig.fails("set_compressor", "nothing to set", target="lr")
    await rig.fails("set_compressor", "can't be used here", target="aux", on=True)


async def test_set_preamp(rig):
    out = await rig.call("set_preamp", target="ch1", gain_db=10, invert=True)
    assert "ch1 preamp 1 gain: 0 dB -> 10 dB" in out and "invert: OFF -> ON" in out
    assert "10 dB -> 15 dB" in await rig.call("set_preamp", target="ch1", delta_gain_db=5)
    await rig.fails("set_preamp", "preamp gain|jumps from", target="ch1", gain_db=40)
    msg = await rig.fails("set_preamp", "phantom power ON", target="ch1", phantom=True)
    assert rig.get("/headamp/01/phantom") == 0 and "confirm=true" in msg
    await rig.call("set_preamp", target="ch1", phantom=True, confirm=True)
    assert rig.get("/headamp/01/phantom") == 1
    rig.set("/ch/05/config/insrc", 11)  # ch5 patched to input 12
    await rig.call("set_preamp", target="ch5", gain_db=6)
    assert GAIN.from_raw(rig.get("/headamp/12/gain")) == pytest.approx(6)
    rig.set("/ch/06/config/insrc", 17)
    await rig.fails("set_preamp", "not fed by a local mic preamp", target="ch6", gain_db=6)
    await rig.fails("set_preamp", "nothing to set", target="ch1")


# ------------------------------------------------------------------- batches


async def test_apply_changes_is_one_undo_step(rig):
    rig.set("/ch/02/mix/02/level", FADER.to_raw(-10))
    out = await rig.call("apply_changes", summary="rebalance", changes=[
        {"target": "ch1", "param": "send.level", "dest": "bus2", "value": -10},
        {"target": "ch2", "param": "send.level", "dest": "bus2", "delta": -3},
        {"target": "ch1", "param": "comp.ratio", "value": "3.6"},
        {"target": "ch1", "param": "mute", "value": True},
        {"target": "ch1", "param": "name", "value": "Kick"},
    ])
    assert out.startswith("Done (change #1") and out.count("\n  ") == 5
    assert rig.get("/ch/01/dyn/ratio") == 6 and rig.get("/ch/01/config/name") == "Kick"
    assert "Reverted change #1 (rebalance)" in await rig.call("undo")
    assert rig.get("/ch/01/config/name") == ""
    assert "rebalance (undone)" in await rig.call("change_history")


async def test_apply_changes_errors(rig):
    await rig.fails("apply_changes", "ratio must be a number", changes=[{"target": "ch1", "param": "comp.ratio", "value": "hard"}])
    await rig.fails("apply_changes", "not numeric", changes=[{"target": "ch1", "param": "name", "delta": 1}])
    await rig.fails("apply_changes", "no value given", changes=[{"target": "ch1", "param": "fader"}])
    await rig.fails("apply_changes", "Unknown parameter", changes=[{"target": "ch1", "param": "sparkle", "value": 1}])


async def test_setup_channels(rig):
    channels = [
        {"channel": "ch3", "name": "Snare", "color": "red", "hpf_hz": 100, "gain_db": 8, "usb": False, "lr": True,
         "fader_db": -15, "pan": -20},
        {"channel": "4", "hpf_hz": "off", "phantom": True},
    ]
    await rig.fails("setup_channels", "phantom power ON", channels=channels)
    assert rig.get("/ch/03/config/name") == ""  # nothing written without confirmation
    out = await rig.call("setup_channels", channels=channels, confirm=True)
    assert "ch3 name" in out and "ch3 color: off -> red" in out and "ch4 preamp 4 phantom: OFF -> ON" in out
    assert rig.get("/ch/03/config/name") == "Snare" and rig.get("/ch/03/preamp/hpon") == 1
    assert model.PAN.from_raw(rig.get("/ch/03/mix/pan")) == -20
    await rig.fails("setup_channels", "longer than 12", channels=[{"channel": "ch3", "name": "A very long name"}])


async def test_setup_monitor_mix(rig):
    rig.set("/ch/09/config/name", "Drums")
    sends = {"drums": 0, "ch16": -6}
    await rig.fails("setup_monitor_mix", "jumps up", bus="bus3", sends=sends)
    out = await rig.call("setup_monitor_mix", bus="bus3", sends=sends, name="Drum IEM", color="blue",
                         bus_fader_db=-5, confirm=True)
    assert "bus3 name" in out and "ch9 (Drums) -> bus3" in out
    assert rig.get("/ch/09/mix/03/tap") == 3 and rig.get("/bus/3/config/name") == "Drum IEM"
    assert db(rig.get("/bus/3/mix/fader")) == pytest.approx(-5)
    await rig.fails("setup_monitor_mix", "can't be used here", bus="fxsend1", sends={})


# ---------------------------------------------------------------------- raw


async def test_osc_get(rig):
    assert await rig.call("osc_get", address="/ch/01/mix/fader") == "/ch/01/mix/fader raw=0.0 text=/ch/01/mix/fader -oo"
    assert "raw=1" in await rig.call("osc_get", address="/-snap/index")
    await rig.fails("osc_get", "blocked", address="/-prefs/ap")
    await rig.fails("osc_get", "start with '/'", address="ch/01/mix/fader")
    await rig.fails("osc_get", "does not answer", address="/nope")


async def test_osc_set(rig):
    out = await rig.call("osc_set", address="/ch/01/eq/1/g", value=0.6)
    assert "ch1 eq/1/g: 0 dB -> 3 dB" in out
    await rig.fails("osc_set", "system setting", address="/routing/p16/01/src", value=3)
    await rig.call("osc_set", address="/routing/p16/01/src", value=3, confirm=True)
    assert rig.get("/routing/p16/01/src") == 3
    await rig.fails("osc_set", "blocked", address="/-prefs/ap", value="x")
    await rig.fails("osc_set", "jumps up", address="/ch/01/mix/fader", value=0.9)


# ------------------------------------------------------------ undo/history


async def test_undo_and_history(rig):
    assert await rig.call("undo") == "Nothing to undo."
    assert await rig.call("change_history") == "No changes yet in this session."
    await rig.call("set_fader", target="ch3", db=-20)
    await rig.call("setup_channels", channels=[{"channel": str(i), "name": f"In {i}"} for i in range(1, 11)])
    hist = await rig.call("change_history")
    assert hist.splitlines()[0].endswith("setup 10 channels")
    assert "    ... and 2 more" in hist and "fader ch3" in hist
    assert "Reverted change #2" in await rig.call("undo")
    assert "(undone)" in await rig.call("change_history", limit=1)
    rig.fake.drop_sets = {"/ch/03/mix/fader": 99}
    out = await rig.call("undo")
    assert "Reverted change #1" in out and "did not reach the mixer" in out and "ch3 fader" in out
