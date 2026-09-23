import pytest

from xr18_mcp import model
from xr18_mcp.mixer import Mixer
from xr18_mcp.osc_client import OscClient
from xr18_mcp.safety import Change, Limits, NeedsConfirmation, check

from . import fake_mixer

FADER = model.FADER


@pytest.fixture
async def rig(tmp_path):
    fake, port, transport = await fake_mixer.start()
    m = Mixer(tmp_path, host="127.0.0.1", port=port)
    yield m, fake
    m.drop()
    transport.close()


async def test_replies_are_matched_and_lost_packets_retried():
    fake, port, transport = await fake_mixer.start(drop_first={"/ch/01/mix/fader"})
    fake.values["/ch/01/mix/fader"] = 0.25
    fake.values["/ch/02/mix/fader"] = 0.5
    c = OscClient("127.0.0.1", port, timeout=0.1)
    await c.open()
    res = await c.get_many(["/ch/01/mix/fader", "/ch/02/mix/fader", "/nope"])
    assert res["/ch/01/mix/fader"] == [0.25]  # first request dropped, retry answered
    assert res["/ch/02/mix/fader"] == [0.5]
    assert res["/nope"] is None
    assert (await c.node("ch/01/config")).startswith("/ch/01/config")
    c.close()
    transport.close()


async def test_apply_reads_back_and_journals(rig):
    m, fake = rig
    g = await m.apply([Change("/ch/03/mix/fader", FADER.to_raw(-20), "ch3 fader")], "t", "fader")
    assert fake.values["/ch/03/mix/fader"] == pytest.approx(FADER.to_raw(-20))
    assert g and g.changes[0].old == 0.0
    assert "-20.0 dB" in g.changes[0].line()
    assert (m.data_dir / "logs" / "changes.jsonl").exists()


async def test_guardrail_blocks_until_confirmed(rig):
    m, fake = rig
    change = Change("/ch/03/mix/fader", FADER.to_raw(0), "ch3 fader")
    with pytest.raises(NeedsConfirmation) as e:
        await m.apply([change], "t", "loud")
    assert "jumps up" in e.value.message
    assert fake.sets == []  # nothing written
    await m.apply([Change("/ch/03/mix/fader", FADER.to_raw(0), "ch3 fader")], "t", "loud", confirm=True)
    assert fake.values["/ch/03/mix/fader"] == pytest.approx(0.75)


async def test_undo_restores_exact_values(rig):
    m, fake = rig
    before = dict(fake.values)
    await m.apply([Change("/ch/05/config/name", "Kick", "n"), Change("/ch/05/mix/fader", FADER.to_raw(-30), "f")], "t", "a")
    await m.apply([Change("/ch/05/mix/fader", FADER.to_raw(-25), "f")], "t", "b")
    groups = await m.undo(5)
    assert [g.summary for g in groups] == ["b", "a"]
    assert await m.get_raw(["/ch/05/config/name", "/ch/05/mix/fader"]) == {
        "/ch/05/config/name": before["/ch/05/config/name"],
        "/ch/05/mix/fader": before["/ch/05/mix/fader"],
    }
    assert await m.undo() == []


async def test_names_and_headamp_mapping(rig):
    m, fake = rig
    fake.values["/ch/07/config/name"] = "Bass"
    fake.values["/ch/07/config/insrc"] = 11  # patched to input 12
    s = await m.resolve("bass")
    assert s == model.Strip("ch", 7)
    assert await m.headamp_for(s) == 12


def test_check_rules():
    lim = Limits(max_raise_db=6, loud_db=-10, lr_max_db=0, max_gain_jump_db=10, max_mutes=2)

    def reasons(addr, old, new):
        return check([(Change(addr, new, addr), old)], lim)

    assert reasons("/ch/01/mix/fader", FADER.to_raw(-10), FADER.to_raw(-5)) == []  # +5 dB is fine
    assert reasons("/ch/01/mix/fader", 0.0, FADER.to_raw(-20)) == []  # up from -inf into a quiet level
    assert reasons("/ch/01/mix/fader", FADER.to_raw(-20), FADER.to_raw(0))  # big jump into loud
    assert reasons("/lr/mix/fader", FADER.to_raw(-2), FADER.to_raw(3))  # LR above 0 dB
    assert reasons("/ch/01/mix/fader", FADER.to_raw(0), FADER.to_raw(-40)) == []  # lowering is always fine
    assert reasons("/headamp/01/phantom", 0, 1)
    assert reasons("/headamp/01/gain", 0.2, 0.4)  # +14.4 dB
    assert reasons("/lr/mix/on", 1, 0)
    assert reasons("/routing/p16/01/src", 0, 3)
    mutes = [(Change(f"/ch/0{i}/mix/on", 0, f"ch{i}"), 1) for i in range(1, 4)]
    assert any("mutes 3 channels" in r for r in check(mutes, lim))
    with pytest.raises(model.ParamError):
        reasons("/-prefs/ap", "x", "y")
