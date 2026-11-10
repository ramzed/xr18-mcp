# X32 / M32 support: where each detail comes from

The X32 profile (`src/xr18_mcp/x32.py`) has not yet run on a live console. This file lists the sources each part
was checked against, so you can see what is confirmed and what is still an assumption.

## Sources

| | Source | Why it counts |
|---|---|---|
| **Scene** | `tests/data/x32_fw4.0.scn`, saved by X32-Edit for firmware 4.0 | Scene files use the same text as `/node` replies. `test_every_node_matches_the_x32_edit_scene` checks every node the server reads against it. |
| **Doc** | Patrick-Gilles Maillot, *Unofficial X32/M32 OSC Remote Protocol*, v4.06-09 (March 2022) | The reference everyone uses. A copy ships with the Companion module as `docs/X32-OSC.pdf`. |
| **Companion** | [bitfocus/companion-module-behringer-x32](https://github.com/bitfocus/companion-module-behringer-x32) | Stream Deck control used on many real consoles. |
| **x32-osc** | [markschwartzkopf/x32-osc](https://github.com/markschwartzkopf/x32-osc) | A separate Node client with its own meter decoding. |

## What each detail rests on

| Detail | What we do | Confirmed by |
|---|---|---|
| Strip and parameter node layouts | layouts in `x32.py` | Scene (exact field counts for all nodes we read) |
| Input routing | read `/config/routing/IN` as text (`AN1-8`, `A9-16`, `B1-8`) | Scene |
| Port and discovery | `/xinfo` to UDP 10023, broadcast to `255.255.255.255`; reply is ip, name, model, firmware | Companion (`device-detector.ts`, `variables/main.ts`), x32-osc, Doc |
| Meter bank | `/meters/0`: 32 ch, 8 aux, 8 FX returns, 16 buses, 6 matrices (70 values) | Doc |
| Meter data | blob = int32 LE count + float32 LE linear values (1.0 = 0 dBFS, up to 8.0) | Doc, x32-osc (`readFloatLE` after the 4-byte count) |
| Meter request | `/meters ,siii /meters/0 0 0 1`, renewed before the 10 s timeout | x32-osc (same argument shape); Doc for the 10 s timeout |
| Scene save | `/save ,siss scene <0-99> <name> <note>` | Doc, Companion (`go-command.ts`) |
| Scene load | `/-action/goscene ,i <0-99>` | Doc, Companion |
| Scene in use | `/-show/showfile/scene/NNN/hasdata` (0/1) or a name | Doc |

## Known caveats

- `/-show/prepos/current` is the *highlighted* cue, scene or snippet for the GO button, depending on the console's
  show-control mode. It does not reliably show which scene is loaded, so `snapshot_load` doesn't use it to confirm
  a load on the X32.
- When **Config > Confirm Pop-Ups > Scene Load** is on, a scene recall waits for someone to confirm it on the
  console. That setting is in `/-prefs`, which the server never touches, so `snapshot_load` says so in its reply.
- `/meters/0` has no main LR or mono values, so `read_meters` doesn't show the mains on the X32. Main levels are in
  `/meters/2`.
- The X32 has no Wi-Fi. The computer needs Ethernet to the console or to the same router.
