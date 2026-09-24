"""MCP tools for meters and gain staging, presets and mixer snapshots."""

import os
import time

from xr18_mcp import model, presets

SILENT = -128.0


def bank1(**levels):
    """A /meters/1 frame: 40 values, silent unless given by index (e.g. ch1=-3 -> index 0)."""
    vals = [SILENT] * 40
    for key, v in levels.items():
        vals[int(key[2:]) - 1] = v
    return vals


# ------------------------------------------------------------------ meters


async def test_read_meters_channels(rig):
    rig.set("/ch/01/config/name", "Kick")
    rig.fake.meters["/meters/1"] = bank1(ch1=-3.0, ch2=0.0, ch3=-20.0, ch4=-50.0, ch5=-100.0)
    out = await rig.call("read_meters", seconds=0.1)
    lines = out.splitlines()
    assert lines[0].startswith("Levels over 0.1s (5 frames)")
    assert "  ch1 (Kick): peak -3.0, avg -3.0 - hot" in lines
    assert "  ch2: peak +0.0, avg +0.0 - CLIPPING" in lines
    assert "  ch3: peak -20.0, avg -20.0 - ok" in lines
    assert "  ch4: peak -50.0, avg -50.0 - low" in lines
    assert lines[-1].startswith("  silent: ch5, ch6") and "monitor" in lines[-1]
    full = await rig.call("read_meters", seconds=0.1, include_silent=True)
    assert "  ch5: peak -100.0, avg -100.0 - no signal (noise floor)" in full
    assert "digital silence (no source)" in full and "silent:" not in full


async def test_read_meters_inputs_and_clamping(rig):
    rig.fake.meters["/meters/2"] = [-30.0] * 36
    out = await rig.call("read_meters", seconds=0.0, scope="inputs")
    assert out.startswith("Levels over 0.05s") and "  usb18: peak -30.0" in out and "  mic1:" in out


async def test_read_meters_without_data(rig):
    await rig.fails("read_meters", "no meter data received", seconds=0.1)


async def test_gain_check(rig):
    for ch, name in ((1, "Kick"), (2, "Snare"), (3, "Vox"), (4, "Click"), (7, "Crash")):
        rig.set(f"/ch/{ch:02d}/config/name", name)
    rig.set("/ch/04/preamp/rtnsw", 1)  # silent USB return
    rig.set("/ch/05/preamp/rtnsw", 1)  # USB return with signal
    rig.set("/ch/06/config/insrc", 17)  # not a mic preamp
    rig.set("/headamp/02/gain", model.KINDS["headamp/gain"].to_raw(20))
    rig.fake.meters["/meters/1"] = bank1(ch1=-30.0, ch2=-13.0, ch4=-128.0, ch5=-10.0, ch6=-15.0, ch7=0.0, ch8=-20.0)
    out = await rig.call("gain_check", seconds=0.1)
    lines = out.splitlines()
    assert lines[0] == "Gain check over 0.1s, target peak -12 dBFS:"
    assert "  ch1 (Kick): peak -30.0, gain +0.0 - suggest gain +0.0 -> +18.0 dB (+18.0) - big raise, do it in steps" in lines
    assert "  ch2 (Snare): peak -13.0, gain +20.0 - OK" in lines
    assert "  ch3 (Vox): NO SIGNAL (gain +0.0) - check cable, mic, phantom power, or that it is being played" in lines
    assert "  ch4 (Click): no signal from the USB return - is the computer playing into it?" in lines
    assert "  ch5: peak -10.0 - source is USB return; adjust the level on the computer" in lines
    assert "  ch6: peak -15.0 - not a mic preamp input, no gain suggestion" in lines
    assert "  ch7 (Crash): peak +0.0, gain +0.0 - suggest gain +0.0 -> -12.0 dB (-12.0) (CLIPPING!)" in lines
    assert "  ch8: peak -20.0, gain +0.0 - suggest gain +0.0 -> +8.0 dB (+8.0)" in lines
    assert not any(ln.startswith("  ch9") for ln in lines)  # unnamed and silent: skipped


async def test_gain_check_targets_and_no_data(rig):
    rig.fake.meters["/meters/1"] = bank1(ch9=-12.0)
    out = await rig.call("gain_check", targets=["ch9", "ch10"], seconds=0.1)
    assert out.splitlines()[1:] == ["  ch9: peak -12.0, gain +0.0 - OK", "  ch10: NO SIGNAL (gain +0.0) - check cable, mic, phantom power, or that it is being played"]
    rig.fake.meters["/meters/1"] = [-12.0] * 4  # a truncated frame only covers ch1-4
    assert await rig.call("gain_check", targets=["ch9"], seconds=0.1) == "Gain check over 0.1s, target peak -12 dBFS:"
    rig.fake.meters.clear()
    await rig.fails("gain_check", "no meter data received", seconds=0.1)
    await rig.fails("gain_check", "can't be used here", targets=["bus1"], seconds=0.1)


# ----------------------------------------------------------------- presets


async def test_save_list_diff_presets(rig):
    assert await rig.call("list_presets") == "No presets saved yet."
    out = await rig.call("save_preset", name="song 1", notes="first song")
    assert out.startswith("Saved preset 'song 1' (2898 parameters) to ") and out.endswith("song-1.json")
    assert await rig.call("diff_preset", name="song 1") == "No differences between 'song 1' and current board."
    await rig.call("set_fader", target="ch3", db=-20)
    await rig.call("save_preset", name="song 2")
    listing = (await rig.call("list_presets")).splitlines()
    assert listing[1].startswith("song 1  (") and listing[1].endswith(" - first song")
    diff = await rig.call("diff_preset", name="song 1")
    assert diff.splitlines() == ["1 differences, 'song 1' -> current board:", "ch3 mix/fader: -inf -> -20.0 dB"]
    assert "'song 1' -> song 2" in await rig.call("diff_preset", name="song 1", other="song 2")
    await rig.call("set_fader", target="ch4", db=-20)
    capped = await rig.call("diff_preset", name="song 1", max_lines=1)
    assert capped.splitlines()[-1] == "... and 1 more"
    await rig.fails("diff_preset", "No preset 'song 9'", name="song 9")


async def test_save_preset_reports_silent_parameters(rig):
    del rig.fake.values["/fx/4/par/64"]
    out = await rig.call("save_preset", name="partial")
    assert "(2897 parameters)" in out and "1 parameters did not answer" in out


async def test_load_preset_confirm_backup_and_undo(rig):
    await rig.call("save_preset", name="clean")
    await rig.call("set_fader", target="ch3", db=-20)
    await rig.call("set_preamp", target="ch1", phantom=True, confirm=True)
    msg = await rig.fails("load_preset", "CONFIRMATION REQUIRED", name="clean")
    assert "2 parameters change" in msg and "phantom power changes" in msg
    out = await rig.call("load_preset", name="clean", confirm=True)
    assert out.startswith("Loaded preset 'clean': 2 parameters changed. Previous board saved as 'autosave-before-clean-")
    assert rig.get("/headamp/01/phantom") == 0 and rig.get("/ch/03/mix/fader") == 0.0
    assert list((rig.dir / "presets").glob("autosave-before-clean-*.json"))
    assert await rig.call("load_preset", name="clean") == "The board already matches preset 'clean'."
    await rig.call("undo")  # a preset load is one undo step
    assert rig.get("/headamp/01/phantom") == 1


async def test_load_preset_prunes_old_backups(rig):
    d = rig.dir / "presets"
    for i in range(25):
        presets.write(d, f"autosave-before-x-{i:02d}", "", {}, None)
        os.utime(d / f"autosave-before-x-{i:02d}.json", (time.time() - 1000 + i, time.time() - 1000 + i))
    await rig.call("save_preset", name="p")
    await rig.call("set_fader", target="ch3", db=-20)
    await rig.call("load_preset", name="p", confirm=True)
    assert len(list(d.glob("autosave-before-*.json"))) == 20


# --------------------------------------------------------------- snapshots


async def test_snapshots(rig):
    assert (await rig.call("snapshot_list")).endswith("All 64 snapshot slots are empty/unnamed.")
    assert await rig.call("snapshot_save", slot=5, name="Verse") == "Saved snapshot 5 'Verse'."
    assert (await rig.call("snapshot_list")).splitlines() == ["Current snapshot index: 5", "5: Verse"]
    await rig.fails("snapshot_save", "already holds snapshot 'Verse'", slot=5, name="Chorus")
    assert "'Chorus'" in await rig.call("snapshot_save", slot=5, name="Chorus", confirm=True)
    assert "reads empty" in await rig.call("snapshot_save", slot=6, name="")
    await rig.fails("snapshot_save", "slot must be 1-64", slot=65, name="x")
    await rig.fails("snapshot_save", "longer than 12", slot=7, name="A very long name")

    await rig.fails("snapshot_load", "slot must be 1-64", slot=0)
    await rig.fails("snapshot_load", "slot 9 is empty", slot=9)
    await rig.fails("snapshot_load", "whole board changes", slot=5)
    assert rig.fake.snap_loads == []
    out = await rig.call("snapshot_load", slot=5, confirm=True)
    assert out.startswith("Loaded snapshot 5 'Chorus'. Previous board saved as preset 'autosave-before-snapshot-5-")
    assert rig.fake.snap_loads == [5]
    assert list((rig.dir / "presets").glob("autosave-before-snapshot-5-*.json"))


async def test_snapshot_load_that_does_not_take(rig):
    rig.set("/-snap/03/name", "Bridge")
    rig.fake.drop_first = {"/-snap/load"}  # the load command is lost on the network
    out = await rig.call("snapshot_load", slot=3, confirm=True)
    assert "but the mixer reports snapshot 1 as current" in out
