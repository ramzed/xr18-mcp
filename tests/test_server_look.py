"""MCP tools that read the board, plus server wiring (registration, startup, errors)."""

import logging

import pytest

import xr18_mcp
from xr18_mcp import presets, server


async def test_all_tools_registered_with_annotations():
    tools = {t.name: t for t in await server.mcp.list_tools()}
    assert len(tools) == 28
    read_only = {n for n, t in tools.items() if t.annotations.read_only_hint}
    assert read_only == {
        "mixer_status", "board_overview", "channel_detail", "find_channels", "read_meters", "gain_check",
        "list_presets", "diff_preset", "snapshot_list", "change_history", "osc_get",
    }
    destructive = {n for n, t in tools.items() if t.annotations.destructive_hint}
    assert destructive == {"load_preset", "snapshot_save", "snapshot_load"}
    assert "confirm" in tools["set_fader"].input_schema["properties"]


async def test_mixer_status(rig):
    out = await rig.call("mixer_status")
    assert "Connected to XR18 'FAKE-XR18' at 127.0.0.1, firmware 1.25." in out
    assert "Current snapshot index: 1" in out
    assert "phantom power" in out and "Changes this session: 0 (0 undoable)" in out


async def test_board_overview(rig):
    rig.set("/ch/01/config/name", "Kick")
    rig.set("/headamp/01/phantom", 1)
    rig.set("/ch/01/preamp/invert", 1)
    rig.set("/ch/01/preamp/hpon", 1)
    rig.set("/ch/01/preamp/hpf", 0.32)
    rig.set("/ch/01/gate/on", 1)
    rig.set("/ch/01/dyn/on", 1)
    rig.set("/ch/01/mix/fader", 0.75)
    rig.set("/ch/01/mix/02/level", 0.5)
    rig.set("/ch/01/mix/02/tap", 3)
    rig.set("/ch/01/mix/07/level", 0.5)
    rig.set("/ch/02/mix/on", 0)
    rig.set("/ch/02/mix/lr", 0)
    rig.set("/ch/03/config/insrc", 17)  # not a mic preamp
    rig.set("/ch/16/config/name", "Click")
    rig.set("/ch/16/preamp/rtnsw", 1)
    rig.set("/rtn/aux/preamp/rtnsw", 1)
    rig.set("/bus/1/config/name", "Bass IEM")
    rig.set("/bus/1/mix/lr", 1)
    rig.set("/dca/1/on", 0)
    rig.set("/config/mute/2", 1)
    out = await rig.call("board_overview")
    lines = out.splitlines()
    assert lines[0] == "XR18 'FAKE-XR18' at 127.0.0.1 (fw 1.25)"
    assert "  ch1: Kick | In01 | gain +0.0 48V inv | HPF 52Hz | 0.0 | on +gate+comp | bus2 -10.0 PRE, fxsend1 -10.0" in lines
    assert "  ch2: - | In02 | gain +0.0 | HPF off | -inf | MUTED notLR | no sends" in lines
    assert "  ch3: - | Aux17 | - | HPF off | -inf | on | no sends" in lines
    assert "  ch16: Click | U16 (USB) | - | HPF off | -inf | on | no sends" in lines
    assert "  aux: - | U01 (USB) | - | - | -inf | on notLR | no sends" in lines
    assert "  fxrtn1: - | FX | - | - | 0.0 | on notLR | no sends" in lines
    assert "  bus1: Bass IEM | 0.0 | on | to LR" in lines
    assert "  bus2: - | 0.0 | on" in lines
    assert "  fxsend1: - | 0.0 | on | FX1: HALL" in lines
    assert "  dca1: - | 0.0 | MUTED" in lines
    assert lines[-1] == "Mute groups engaged: 2"


async def test_board_overview_aux_without_usb_and_no_mute_groups(rig):
    out = await rig.call("board_overview")
    assert "  aux: - | Aux in |" in out
    assert out.endswith("Mute groups engaged: none")


async def test_channel_detail(rig):
    rig.set("/ch/01/config/name", "Kick")
    rig.set("/ch/01/config/color", 2)
    rig.set("/ch/01/mix/02/level", 0.5)
    rig.fake.hide_nodes.add("ch/01/automix")
    out = await rig.call("channel_detail", target="kick")
    lines = out.splitlines()
    assert lines[0] == "ch1 (Kick) - Channel 1"
    assert '  config: name=Kick color=green insrc=In01 rtnsrc=U01' in lines
    assert any(ln.startswith("  send -> bus2: level=-10.0") for ln in lines)
    assert any(ln.startswith("  send -> fxsend4: level=-inf") for ln in lines)
    assert any(ln.startswith("  headamp/01: gain=") for ln in lines)
    assert not any("automix" in ln for ln in lines)  # a node that did not answer is skipped


async def test_channel_detail_other_strips(rig):
    rig.set("/bus/1/config/color", 99)
    bus = await rig.call("channel_detail", target="bus1")
    assert "  config: name= color=99" in bus
    dca = await rig.call("channel_detail", target="dca2")
    assert dca.splitlines()[:2] == ["dca2 - DCA 2", "  main: on=ON fader=+0.0"]
    await rig.fails("channel_detail", "No channel or bus called 'drums'", target="drums")


async def test_find_channels(rig):
    rig.set("/ch/03/config/name", "Vox 1")
    rig.set("/ch/04/config/name", "Vox 2")
    assert await rig.call("find_channels", query="vox") == "ch3: Vox 1\nch4: Vox 2"
    assert await rig.call("find_channels") == "ch3: Vox 1\nch4: Vox 2"
    assert await rig.call("find_channels", query="drums") == "No named strip matches 'drums'."


async def test_first_connection_saves_session_backup(rig, monkeypatch):
    monkeypatch.setattr(server, "AUTOSAVE", True)
    await rig.call("mixer_status")
    files = list((rig.dir / "presets").glob("session-start-*.json"))
    assert len(files) == 1
    assert presets.read(rig.dir / "presets", files[0].stem).values["/lr/mix/fader"] == 0.75


async def test_failed_session_backup_does_not_block_tools(rig, monkeypatch, caplog):
    async def broken(*a):
        raise RuntimeError("disk full")

    monkeypatch.setattr(server, "AUTOSAVE", True)
    monkeypatch.setattr(server, "_save_preset", broken)
    with caplog.at_level(logging.WARNING):
        assert "Connected" in await rig.call("mixer_status")
    assert "session-start autosave failed" in caplog.text


async def test_system_errors_become_tool_errors(rig, monkeypatch):
    def denied(*a, **k):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(presets, "write", denied)
    await rig.fails("save_preset", "System error: .*Permission denied", name="x")


async def test_no_mixer_is_a_tool_error(rig, monkeypatch):
    from xr18_mcp import mixer as mixer_mod

    monkeypatch.setattr(mixer_mod, "discover", list)
    rig.fake.answer_xinfo = False
    await rig.fails("mixer_status", "No XR18 answered")


def test_entry_points(monkeypatch):
    calls = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: calls.append("run"))
    server.main()
    xr18_mcp.main()
    assert calls == ["run", "run"]


@pytest.mark.parametrize("value, expected", [(3.6, "4.0"), (1, "1.1"), ("2.4", "2.5"), (500, "100")])
def test_ratio_snapping(value, expected):
    assert server._ratio(value) == expected
