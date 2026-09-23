# XR18 MCP

An MCP server that lets Claude work as a sound tech on a **Behringer XR18** during rehearsals.
It connects over OSC/UDP and can run alongside X AIR Edit, which shows every change live.

## Install

**To use it** (any Mac or PC on the mixer's network): install [uv](https://docs.astral.sh/uv/), then run

```
uv tool install git+https://github.com/ramzed/xr18-mcp
```

and register the `xr18-mcp` command in your AI app. [INSTALL.md](INSTALL.md) has step-by-step instructions
for Claude Desktop, Claude Code and Antigravity. To update later, run `uv tool upgrade xr18-mcp`.

**To work on it:** clone the repo and open the folder in Claude Code. `.mcp.json` runs the server from the
checkout with `uv run xr18-mcp` and keeps presets and logs in the repo folder (automatic backups are
gitignored). Approve the `xr18` server and check it with `/mcp`.

The mixer is found automatically with an `/xinfo` search. Set `XR18_IP` only if that fails.

## Things to ask

- "What's on the board?" / "Why is there no sound from channel 5?"
- "More click and less bass in bus 2." / "Guitar up 3 dB in the mains."
- "New band tomorrow. Inputs: 1 kick, 2 snare, 3 bass DI, 4 guitar, 5-6 vox. Set it up."
- "Build Anna's in-ear mix on bus 3: her vocal at 0, guitar at -6, click at -3."
- "Everyone play loud for 10 seconds, then check the gains."
- "Save this as song-3." / "What changed since the start of rehearsal?" / "Undo that."

## Tools

| Group | Tools |
|---|---|
| Look | `mixer_status`, `board_overview`, `channel_detail`, `find_channels` |
| Mix | `set_fader`, `set_mute`, `set_send`, `set_eq`, `set_hpf`, `set_gate`, `set_compressor`, `set_preamp`, `apply_changes` (several edits as one undo step) |
| Setup | `setup_channels` (input list: name, colour, HPF, gain, phantom, USB, LR, fader, pan), `setup_monitor_mix` |
| Listen | `read_meters` (peak/avg dBFS, clip/hot/low/silent flags), `gain_check` (preamp gain suggestions) |
| Remember | `save_preset`, `list_presets`, `diff_preset`, `load_preset`, `snapshot_list`, `snapshot_save`, `snapshot_load`, `undo`, `change_history` |
| Raw | `osc_get`, `osc_set` |

Strips can be named by their mixer name ("Guitar", "click") or by id: `ch1-16`, `aux`, `fxrtn1-4`, `bus1-6`,
`fxsend1-4`, `lr`, `dca1-4`.

## Safety

- **Needs confirmation.** The tool refuses these moves and explains why; Claude has to ask you and then retry with
  `confirm=true`:
  - phantom power changes
  - raising a level by more than 6 dB when it ends above −10 dB
  - main LR above 0 dB
  - a preamp gain jump of more than 10 dB
  - muting more than 4 channels, or muting LR
  - loading presets or snapshots
  - system, routing and FX settings
- **Undo.** Every change is read back from the mixer and written to `logs/changes.jsonl`. `undo` restores the exact
  previous values.
- **Automatic backups.** When the server first connects, it saves the whole board as `presets/session-start-<time>.json`
  (the last 10 are kept). Loading a preset or snapshot first saves `presets/autosave-before-…`.
- **Blocked.** `/-prefs` is never read or written. The mixer returns its network settings, **including Wi-Fi
  passwords**, to anyone on the LAN who asks over OSC.

Limits can be changed with environment variables in the app's MCP config: `XR18_MAX_RAISE_DB`, `XR18_LOUD_DB`, `XR18_LR_MAX_DB`,
`XR18_MAX_GAIN_JUMP_DB`, `XR18_MAX_MUTES`. Other settings: `XR18_DATA_DIR` sets where presets and logs go (default:
`Documents\XR18-MCP` on Windows, `~/XR18-MCP` on macOS; this repo's `.mcp.json` points it at the checkout), and `XR18_AUTOSAVE=0` turns off the session-start backup.

## Development

```
uv run pytest          # unit tests and fake-mixer tests, no hardware needed
```

Layout: `osc_client.py` (UDP, matches each reply to its request, pipelined reads, meters) · `model.py` (address
map, value conversions, name lookup) · `mixer.py` (connection, guarded writes, undo) · `safety.py` · `meters.py` ·
`presets.py` · `server.py` (MCP tools).

### XR18 OSC notes (checked on firmware 1.25)

- `/node ,s "ch/01/eq"` returns that section as human-readable text in one packet (at most about 528 bytes, so request small sections).
  Setting values as text through `/` does **not** work on the XR18; only typed values do.
- A full board is 2,898 parameters. With 16 requests in flight it reads in about 1.6 s.
- EQ Q is log from 10 down to 0.3 (raw 0 = Q 10). Gate mode order is EXP2, EXP3, EXP4, GATE, DUCK. Send tap
  order is IN, PREEQ, POSTEQ, PRE, POST, GRP.
- Snapshots: to save, set `/-snap/name ,s` then send `/-snap/save ,i N`. Load and delete are `/-snap/load ,i N` and `/-snap/delete ,i N`.
- Meters: `/meters ,s "/meters/1"` streams for about 10 s. Each blob is an int32 count followed by int16 values in 1/256 dBFS (little-endian).

Fader, frequency and dynamics curves come from
[xair-api-python](https://github.com/onyx-and-iris/xair-api-python) (MIT). Its EQ Q curve and gate-mode order
are corrected here.
