# XR18 MCP: installation on macOS

This lets an AI assistant (Claude, Gemini in Antigravity, and others) run a **Behringer XR18** for you. You can ask
it to set up channels, balance monitor mixes, check input levels, and save or restore the whole board. It works
alongside X AIR Edit, which shows every change live.

**You need**
- A Mac on the same network as the mixer: either the mixer's own Wi-Fi, or the same router.
- An AI app that supports MCP: Claude Desktop, Claude Code, or Antigravity.

## 1. Install uv

uv is a small tool that installs the server and the Python it needs. Open **Terminal** and run:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

If you use Homebrew, `brew install uv` works too. Close Terminal and open it again.

## 2. Install the server

```
uv tool install git+https://github.com/ramzed/xr18-mcp
```

This needs git. If macOS offers to install the "command line developer tools", accept, then run the command again.
If you were sent a `.whl` file instead, install that: `uv tool install ~/Downloads/xr18_mcp-0.1.1-py3-none-any.whl`.

Then run `which xr18-mcp`. It prints the full path to the program, for example
`/Users/alex/.local/bin/xr18-mcp`. Copy it; you need it in the next step.

## 3. Add it to your AI app

**Claude Desktop:** in the menu bar, go to **Claude**, then **Settings…**, then **Developer**, then **Edit Config**. Add
this to the file, using the path you copied (config files don't understand `~`):

```json
{
  "mcpServers": {
    "xr18": {
      "command": "/Users/alex/.local/bin/xr18-mcp"
    }
  }
}
```

If the file already has an `mcpServers` section, add only the `"xr18": { ... }` part inside it. Save the file,
quit Claude completely (⌘Q) and open it again.

**Claude Code:** run this in Terminal:

```
claude mcp add xr18 --scope user -- ~/.local/bin/xr18-mcp
```

**Antigravity:** in the Agent panel, open **⋯**, then **MCP Servers**, then **Manage MCP Servers**, then **View raw config**.
Add the same `"xr18"` entry as for Claude Desktop, save, and click **Refresh**.

## 4. Allow local network access

macOS asks whether an app may talk to devices on your network. The first time the assistant connects to the mixer,
you'll see a prompt like *"Allow Claude to find devices on local networks?"*. Click **Allow**.

If you missed it or clicked Don't Allow, go to System Settings, then **Privacy & Security**, then **Local Network**, and
switch on the app you use: Claude, Antigravity, or for Claude Code, your terminal app (Terminal, iTerm or VS Code).

## 5. Try it

Turn on the mixer, connect the Mac to its network, and ask:

- "What's on the board?"
- "Name channels 1-6: kick, snare, bass, guitar, vox 1, vox 2."
- "Everyone play loud for 10 seconds, then check the gains."
- "More vocal in bus 2." Then: "Undo that."
- "Save the board as rehearsal-1." Later: "What changed since rehearsal-1?"

## Safety

- **Risky moves need your OK.** The assistant has to ask you before it switches phantom power, makes a big
  jump in level, raises the main output above 0 dB, mutes many channels, or loads a preset or snapshot. Only say
  yes when you mean it.
- **Every change can be undone.** Just say "undo".
- **Automatic backup.** Each session saves the board as it was when the assistant connected. Backups, presets and
  the change log are kept in the **XR18-MCP** folder in your home folder (`~/XR18-MCP`).

## If the mixer is not found

- Check that X AIR Edit on the same Mac can see the mixer.
- Check the Local Network permission (step 4).
- Tell the server the mixer's IP address (X AIR Edit shows it). In the app config, add an `env` section next to
  `command`:

  ```json
  "xr18": {
    "command": "/Users/alex/.local/bin/xr18-mcp",
    "env": { "XR18_IP": "192.168.1.1" }
  }
  ```

  When your Mac is connected to the mixer's own Wi-Fi, its address is usually `192.168.1.1`.

## Update or remove

- Update to the latest version: `uv tool upgrade xr18-mcp`. Then restart your AI app.
- If you installed from a `.whl` file, update with `uv tool install --force ~/Downloads/<new-file>.whl`.
- Remove: `uv tool uninstall xr18-mcp`

## Windows

The steps are the same with these differences:
- Install uv in PowerShell:
  `powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"`
- The program is `C:/Users/NAME/.local/bin/xr18-mcp.exe`.
- There's no Local Network step. If Windows asks about network access, allow it on private networks.
- Presets go to `Documents\XR18-MCP`.
