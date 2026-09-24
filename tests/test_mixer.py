import pytest

from xr18_mcp import mixer as mixer_mod
from xr18_mcp import model
from xr18_mcp.mixer import Mixer, MixerUnavailable, _coerce, _dropped, _same
from xr18_mcp.osc_client import MixerInfo, OscClient
from xr18_mcp.safety import Change, NeedsConfirmation

FADER = model.FADER


# ------------------------------------------------------------- connection


async def test_connects_and_calls_first_connect_hook_once(mixer):
    calls = []

    async def hook(m):
        calls.append(m)

    mixer.on_first_connect = hook
    c1 = await mixer.ensure()
    c2 = await mixer.ensure()
    assert c1 is c2 and calls == [mixer]
    assert mixer.info.name == "FAKE-XR18"
    mixer.drop()
    await mixer.ensure()  # reconnect does not re-run the hook
    assert len(calls) == 1


async def test_falls_back_to_discovery(fake, tmp_path, monkeypatch):
    monkeypatch.setattr(mixer_mod, "discover", lambda: [MixerInfo("127.0.0.1", "found", "XR18", "1.25")])
    m = Mixer(tmp_path, host="192.0.2.1", port=fake.port, timeout=0.05)  # TEST-NET: nothing answers there
    await m.ensure()
    assert m.info.name == "FAKE-XR18"
    m.drop()


async def test_no_mixer_anywhere(tmp_path, monkeypatch):
    monkeypatch.setattr(mixer_mod, "discover", list)
    monkeypatch.setattr(mixer_mod.sys, "platform", "linux")
    m = Mixer(tmp_path, host="192.0.2.1", port=9, timeout=0.02)
    with pytest.raises(MixerUnavailable, match="at 192.0.2.1") as e:
        await m.ensure()
    assert "Local Network" not in str(e.value)
    monkeypatch.setattr(mixer_mod.sys, "platform", "darwin")
    m2 = Mixer(tmp_path, port=9, timeout=0.02)
    with pytest.raises(MixerUnavailable, match="Local Network"):
        await m2.ensure()


async def test_network_errors_count_as_not_found(tmp_path, monkeypatch):
    async def refuse(self):
        raise OSError(65, "No route to host")

    monkeypatch.setattr(OscClient, "open", refuse)
    monkeypatch.setattr(mixer_mod, "discover", list)
    with pytest.raises(MixerUnavailable):
        await Mixer(tmp_path, host="127.0.0.1", timeout=0.02).ensure()


async def test_mixer_going_away_drops_connection(mixer, fake):
    await mixer.ensure()
    fake.answer_xinfo = False
    fake.values.pop("/ch/01/mix/fader")
    with pytest.raises(MixerUnavailable, match="stopped answering"):
        await mixer.get_raw(["/ch/01/mix/fader"])
    assert mixer.client is None


async def test_invalid_address_keeps_connection(mixer):
    with pytest.raises(model.ParamError, match="not a valid XR18 address"):
        await mixer.get_raw(["/nope"])
    assert mixer.client is not None
    assert (await mixer.get_raw(["/nope", "/lr/mix/on"], strict=False)) == {"/nope": None, "/lr/mix/on": 1}


# ------------------------------------------------------------------ reads


async def test_read_nodes(mixer, fake):
    fake.values["/ch/01/config/name"] = "Kick"
    out = await mixer.read_nodes(["ch/01/config", "headamp/01", "ch/01/mix/fader", "no/such"])
    assert "no/such" not in out
    assert out["ch/01/config"]["name"] == "Kick"
    assert set(out["headamp/01"]) == {"gain", "phantom"} and out["headamp/01"]["phantom"] == "OFF"
    assert out["ch/01/mix/fader"] == {"0": "-oo"}  # leaf node without a layout: positional keys


async def test_read_nodes_all_silent_means_gone(mixer, fake):
    await mixer.ensure()
    fake.answer_xinfo = False
    with pytest.raises(MixerUnavailable):
        await mixer.read_nodes(["no/such/node"])


async def test_names_cache_and_resolve(mixer, fake):
    fake.values["/ch/07/config/name"] = "Bass"
    assert (await mixer.names())[model.Strip("ch", 7)] == "Bass"
    fake.values["/ch/07/config/name"] = "Renamed"
    assert (await mixer.names())[model.Strip("ch", 7)] == "Bass"  # cached for 2 s
    assert (await mixer.names(max_age=0))[model.Strip("ch", 7)] == "Renamed"
    assert await mixer.resolve("renamed") == model.Strip("ch", 7)
    assert await mixer.label(model.Strip("ch", 7)) == "ch7 (Renamed)"


async def test_headamp_follows_input_source(mixer, fake):
    fake.values["/ch/07/config/insrc"] = 11
    assert await mixer.headamp_for(model.Strip("ch", 7)) == 12
    fake.values["/ch/07/config/insrc"] = 17  # aux/USB, no mic preamp
    assert await mixer.headamp_for(model.Strip("ch", 7)) is None
    assert await mixer.headamp_for(model.Strip("bus", 1)) is None


# ----------------------------------------------------------------- writes


async def test_apply_reads_back_and_journals(mixer, fake):
    g = await mixer.apply([Change("/ch/03/mix/fader", FADER.to_raw(-20), "ch3 fader")], "t", "fader")
    assert fake.values["/ch/03/mix/fader"] == pytest.approx(FADER.to_raw(-20))
    assert g.id == 1 and g.changes[0].old == 0.0 and g.changes[0].ok
    assert "-20.0 dB" in g.changes[0].line()
    assert (mixer.data_dir / "logs" / "changes.jsonl").exists()


async def test_apply_edge_cases(mixer, fake):
    assert await mixer.apply([], "t", "nothing") is None
    g = await mixer.apply([Change("/ch/03/mix/fader", 0.0, "x")], "t", "same value")
    assert g.id == 0 and not g.changes and "no change needed" in g.summary
    # duplicate addresses: last value wins, written once
    g = await mixer.apply([Change("/ch/04/mix/fader", 0.3, "a"), Change("/ch/04/mix/fader", 0.4, "b")], "t", "dup")
    assert len(g.changes) == 1 and fake.values["/ch/04/mix/fader"] == pytest.approx(0.4)
    # ints sent to float addresses are coerced to the mixer's type
    await mixer.apply([Change("/ch/05/mix/fader", 1, "int")], "t", "coerce", confirm=True)
    assert isinstance(fake.values["/ch/05/mix/fader"], float)


async def test_guardrail_blocks_until_confirmed(mixer, fake):
    change = Change("/ch/03/mix/fader", FADER.to_raw(0), "ch3 fader")
    with pytest.raises(NeedsConfirmation) as e:
        await mixer.apply([change], "t", "loud")
    assert "jumps up" in e.value.message and "Planned changes" in e.value.message
    assert fake.sets == []  # nothing written
    await mixer.apply([Change("/ch/03/mix/fader", FADER.to_raw(0), "ch3 fader")], "t", "loud", confirm=True)
    assert fake.values["/ch/03/mix/fader"] == pytest.approx(0.75)


async def test_confirmation_preview_is_capped(mixer):
    changes = [Change("/lr/mix/on", 0, "lr")] + [Change(f"/ch/{i:02d}/mix/fader", 0.3, f"ch{i}") for i in range(1, 17)]
    changes += [Change(f"/ch/{i:02d}/mix/0{b}/level", 0.3, "s") for i in range(1, 17) for b in range(1, 3)]
    with pytest.raises(NeedsConfirmation) as e:
        await mixer.apply(changes, "t", "big")
    assert "... and 9 more" in e.value.message


async def test_dropped_writes_are_resent(mixer, fake):
    fake.drop_sets = {"/ch/03/mix/fader": 2}  # the first two writes are lost
    g = await mixer.apply([Change("/ch/03/mix/fader", 0.5, "ch3")], "t", "lossy")
    assert g.changes[0].ok and fake.values["/ch/03/mix/fader"] == pytest.approx(0.5)


async def test_writes_that_never_land_are_flagged(mixer, fake):
    fake.drop_sets = {"/ch/03/mix/fader": 99}
    g = await mixer.apply([Change("/ch/03/mix/fader", 0.5, "ch3")], "t", "dead")
    assert not g.changes[0].ok
    assert "NOT APPLIED" in g.changes[0].line()


async def test_rename_refreshes_name_cache(mixer):
    await mixer.names()
    await mixer.apply([Change("/ch/09/config/name", "Snare", "n")], "t", "rename")
    assert await mixer.resolve("snare") == model.Strip("ch", 9)


async def test_undo_restores_exact_values(mixer, fake):
    before = dict(fake.values)
    await mixer.apply([Change("/ch/05/config/name", "Kick", "n"), Change("/ch/05/mix/fader", FADER.to_raw(-30), "f")], "t", "a")
    await mixer.apply([Change("/ch/05/mix/fader", FADER.to_raw(-25), "f")], "t", "b")
    groups, failed = await mixer.undo(5)
    assert [g.summary for g in groups] == ["b", "a"] and failed == []
    assert fake.values["/ch/05/config/name"] == before["/ch/05/config/name"]
    assert fake.values["/ch/05/mix/fader"] == before["/ch/05/mix/fader"]
    assert await mixer.undo() == ([], [])


async def test_undo_reports_lost_writes(mixer, fake):
    await mixer.apply([Change("/ch/06/mix/fader", 0.4, "ch6 fader")], "t", "a")
    fake.drop_sets = {"/ch/06/mix/fader": 99}
    groups, failed = await mixer.undo()
    assert len(groups) == 1 and failed == ["ch6 fader"]


def test_helpers():
    assert _coerce(1, 0.5) == 1.0 and isinstance(_coerce(1, 0.5), float)
    assert _coerce(2.0, 1) == 2 and isinstance(_coerce(2.0, 1), int)
    assert _coerce(True, "x") == 1
    assert _coerce("a", 1) == "a"
    assert _same(0.5, 0.5 + 1e-9) and _same(float("nan"), float("nan")) and not _same(0.5, 0.6)
    assert _same("a", "a")
    assert _dropped(None, 1, 0)
    assert not _dropped(0.505, 0.5, 0.0)  # quantized by the mixer, but landed
    assert _dropped(0.0, 0.5, 0.0)  # still the old value
    assert not _dropped(0.3, 0.5, 0.0)  # something else changed it: not a lost packet
    assert _dropped("", "Kick", "") and not _dropped("Kick", "Kick", "")
