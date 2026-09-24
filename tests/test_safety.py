import json

import pytest

from xr18_mcp import model
from xr18_mcp.safety import Applied, Change, Journal, Limits, NeedsConfirmation, _json_default, check, describe_raw

FADER = model.FADER
LIM = Limits(max_raise_db=6, loud_db=-10, lr_max_db=0, max_gain_jump_db=10, max_mutes=2)


def reasons(addr, old, new):
    return check([(Change(addr, new, addr), old)], LIM)


def test_level_rules():
    assert reasons("/ch/01/mix/fader", FADER.to_raw(-10), FADER.to_raw(-5)) == []  # +5 dB is fine
    assert reasons("/ch/01/mix/fader", 0.0, FADER.to_raw(-20)) == []  # up from -inf into a quiet level
    assert reasons("/ch/01/mix/fader", FADER.to_raw(-20), FADER.to_raw(0))  # big jump into loud
    assert reasons("/ch/01/mix/03/level", FADER.to_raw(-30), FADER.to_raw(-5))  # sends too
    assert reasons("/config/solo/level", FADER.to_raw(-30), FADER.to_raw(0))  # headphone level
    assert reasons("/ch/01/mix/fader", FADER.to_raw(0), FADER.to_raw(-40)) == []  # lowering is always fine
    lr = reasons("/lr/mix/fader", FADER.to_raw(-2), FADER.to_raw(3))
    assert any("main LR" in r for r in lr)
    assert reasons("/lr/mix/fader", FADER.to_raw(5), FADER.to_raw(4)) == []  # lowering LR, even above 0


def test_preamp_and_phantom_rules():
    assert any("phantom power ON" in r for r in reasons("/headamp/01/phantom", 0, 1))
    assert any("phantom power OFF" in r for r in reasons("/headamp/01/phantom", 1, 0))
    assert reasons("/headamp/01/gain", 0.2, 0.4)  # +14.4 dB
    assert reasons("/headamp/01/gain", 0.2, 0.25) == []  # +3.6 dB
    assert reasons("/headamp/01/gain", 0.4, 0.2) == []


def test_mute_and_system_rules():
    assert any("main LR output" in r for r in reasons("/lr/mix/on", 1, 0))
    assert reasons("/ch/01/mix/on", 1, 0) == []
    assert reasons("/ch/01/mix/on", 0, 1) == []  # unmuting
    mutes = [(Change(f"/ch/0{i}/mix/on", 0, f"ch{i}"), 1) for i in range(1, 4)]
    assert any("mutes 3 channels" in r for r in check(mutes, LIM))
    dcas = [(Change(f"/dca/{i}/on", 0, f"dca{i}"), 1) for i in range(1, 4)]
    assert any("mutes 3" in r for r in check(dcas, LIM))
    assert reasons("/routing/p16/01/src", 0, 3)
    assert reasons("/-snap/load", 0, 3)
    assert reasons("/ch/01/eq/1/g", 0.5, 0.9) == []  # ordinary settings pass
    assert reasons("/ch/01/mix/fader", 0.5, 0.5) == []  # unchanged values are skipped


def test_blocked_prefix():
    with pytest.raises(model.ParamError, match="blocked"):
        reasons("/-prefs/ap", "x", "y")


def test_limits_from_environment(monkeypatch):
    monkeypatch.setenv("XR18_MAX_RAISE_DB", "3")
    monkeypatch.setenv("XR18_MAX_MUTES", "8")
    monkeypatch.setenv("XR18_LOUD_DB", "not a number")
    lim = Limits()
    assert lim.max_raise_db == 3 and lim.max_mutes == 8 and lim.loud_db == -10.0


def test_needs_confirmation_message():
    e = NeedsConfirmation(["too loud"], ["ch1 fader: -20 -> 0"])
    assert "CONFIRMATION REQUIRED" in e.message
    assert "- too loud" in e.message and "Planned changes:" in e.message and "confirm=true" in str(e)
    assert "Planned" not in NeedsConfirmation(["x"]).message


def test_describe_raw():
    assert describe_raw("/ch/01/mix/fader", None) == "?"
    assert describe_raw("/ch/01/mix/on", 1) == "on"
    assert describe_raw("/ch/01/mix/on", 0) == "MUTED"
    assert describe_raw("/dca/1/on", 0) == "MUTED"
    assert describe_raw("/ch/01/mix/fader", 0.75) == "0.0 dB"
    assert describe_raw("/ch/01/config/insrc", 3) == "3"  # opaque
    assert describe_raw("/nope", "x") == "'x'"  # unknown address
    assert describe_raw("/ch/01/mix/03/tap", "bad") == "'bad'"  # conversion failure falls back to repr


def test_applied_line_flags_dropped_writes():
    ok = Applied("/ch/01/mix/fader", "ch1 fader", 0.0, 0.5)
    assert ok.line() == "ch1 fader: -inf -> -10.0 dB"
    lost = Applied("/ch/01/mix/fader", "ch1 fader", 0.0, 0.0, ok=False)
    assert "NOT APPLIED" in lost.line()


def test_journal_records_and_undo_marks(tmp_path):
    j = Journal(tmp_path / "logs" / "changes.jsonl")
    g1 = j.record("t", "first", [Applied("/a", "a", 0, 1)])
    g2 = j.record("t", "second", [Applied("/b", "b", b"\x01", 2)])
    assert (g1.id, g2.id) == (1, 2)
    assert j.undoable() == [g1, g2]
    j.mark_undone(g2)
    assert j.undoable() == [g1]
    lines = [json.loads(x) for x in (tmp_path / "logs" / "changes.jsonl").read_text().splitlines()]
    assert [x["event"] for x in lines] == ["change", "change", "undo"]
    assert lines[1]["changes"][0]["old"] == "01"  # bytes are hex-encoded
    assert lines[0]["changes"][0]["ok"] is True


def test_journal_ignores_unwritable_log(tmp_path):
    blocker = tmp_path / "logs"
    blocker.write_text("a file where the folder should be")
    j = Journal(blocker / "changes.jsonl")
    g = j.record("t", "x", [])
    assert g.id == 1  # recording still works in memory


def test_json_default():
    assert _json_default(b"\xff") == "ff"
    assert _json_default(object()).startswith("<object")
