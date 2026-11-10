# How to install XR18 MCP

XR18 MCP lets an AI assistant such as Claude run your **Behringer XR18** mixer while you rehearse. You type what
you want, for example *"more vocal in the drummer's in-ears"* or *"check the input levels"*, and it makes the
change on the mixer. It also works with **Behringer X32 / Midas M32** consoles, but that support is still
experimental (see [Connecting to the mixer](#connecting-to-the-mixer)).

Installing takes about 10 minutes. You copy a few commands into a window, edit one settings file, and restart an
app. You don't need to know how to program. Do every step in order.

**What you need**

- A Mac or a Windows computer.
- The **Claude** desktop app, signed in. Download it from [claude.ai/download](https://claude.ai/download).
- An internet connection for the installation. At rehearsal you only need the mixer's network.

> **Copying commands:** everything in a grey box is a command or text to copy. Select the whole box, copy it
> (⌘C on a Mac, Ctrl+C on Windows) and paste it where the step says.

Go to [Mac](#mac) or [Windows](#windows).

---

## Mac

### Step 1. Open Terminal

Terminal is a built-in app where you type commands. Press **⌘ Space**, type `Terminal` and press **Return**. A
window with text and a blinking cursor opens. Keep it open until Step 3.

To run a command: paste it into the Terminal window and press **Return**. Wait until the cursor is back on a new
empty line before you paste the next one.

### Step 2. Install uv

uv is a free helper that downloads and installs the mixer program for you. Paste this and press **Return**:

```
curl -LsSf https://astral.sh/uv/install.sh | sh
```

When it finishes, **quit Terminal (⌘Q) and open it again**, as in Step 1. Terminal only picks up the new helper
after a restart.

### Step 3. Install the mixer program

Paste this and press **Return**:

```
uv tool install https://github.com/ramzed/xr18-mcp/archive/refs/heads/main.zip
```

It takes a minute and ends with `Installed 1 executable: xr18-mcp`.

Now paste this and press **Return**:

```
which xr18-mcp
```

It prints one line, for example `/Users/anna/.local/bin/xr18-mcp`. That is where the program lives on your Mac.
**Copy that line**, you need it in the next step.

> If it says `xr18-mcp not found`, quit Terminal, open it again and repeat the `which` command.

### Step 4. Tell Claude about the program

1. Open the Claude app.
2. In the menu bar at the top of the screen, click **Claude**, then **Settings…**
3. Click **Developer** on the left, then **Edit Config**. A Finder window opens with a file called
   `claude_desktop_config.json` selected.
4. Right-click that file and choose **Open With → TextEdit**.
5. Look at what is in the file and follow **A** or **B**:

**A. The file is empty, or contains only `{}`.** Delete everything in it and paste this:

```json
{
  "mcpServers": {
    "xr18": {
      "command": "PASTE-THE-LINE-HERE"
    }
  }
}
```

**B. The file already has text in it.** Copy the file's contents, paste them into a chat with Claude and write:
*"Add an MCP server called xr18 with the command PASTE-THE-LINE-HERE to this config and give me the whole file
back."* Replace the whole file with the answer. This keeps your other settings safe.

6. Replace `PASTE-THE-LINE-HERE` with the line you copied in Step 3. Keep the quote marks around it. It should look
   like `"command": "/Users/anna/.local/bin/xr18-mcp"`.
7. Save the file (⌘S) and close TextEdit.
8. **Quit Claude completely (⌘Q)** and open it again. Closing the window is not enough.

> Paste the text, don't type it. When you type, TextEdit may turn straight quotes `"` into curly ones `“ ”`, and
> then Claude can't read the file.

### Step 5. Allow access to the local network

macOS asks before an app talks to devices on your network. The first time Claude connects to the mixer, a message
appears: *"Allow Claude to find devices on local networks?"* Click **Allow**.

If you clicked **Don't Allow** by mistake: open **System Settings → Privacy & Security → Local Network** and switch
**Claude** on.

Now go to [Connecting to the mixer](#connecting-to-the-mixer).

---

## Windows

### Step 1. Open PowerShell

PowerShell is a built-in app where you type commands. Click **Start**, type `PowerShell` and click **Windows
PowerShell**. A blue or black window with a blinking cursor opens. Keep it open until Step 3.

To run a command: paste it into the window (right-click or Ctrl+V) and press **Enter**. Wait until you see a new
line ending in `>` before you paste the next one.

### Step 2. Install uv

uv is a free helper that downloads and installs the mixer program for you. Paste this and press **Enter**:

```
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

When it finishes, **close PowerShell and open it again**, as in Step 1. PowerShell only picks up the new helper
after a restart.

### Step 3. Install the mixer program

Paste this and press **Enter**:

```
uv tool install https://github.com/ramzed/xr18-mcp/archive/refs/heads/main.zip
```

It takes a minute and ends with `Installed 1 executable: xr18-mcp.exe`.

Now paste this and press **Enter**:

```
(Get-Command xr18-mcp).Source.Replace('\', '/')
```

It prints one line, for example `C:/Users/Anna/.local/bin/xr18-mcp.exe`. That is where the program lives on your
computer. **Copy that line**: select it with the mouse and press Ctrl+C. You need it in the next step.

> If it shows a red error, close PowerShell, open it again and repeat the command.

### Step 4. Tell Claude about the program

1. Open the Claude app.
2. Click the **☰** menu at the top left, then **File → Settings**.
3. Click **Developer** on the left, then **Edit Config**. A folder opens with a file called
   `claude_desktop_config.json` selected.
4. Right-click that file, choose **Open with** and pick **Notepad**.
5. Look at what is in the file and follow **A** or **B**:

**A. The file is empty, or contains only `{}`.** Delete everything in it and paste this:

```json
{
  "mcpServers": {
    "xr18": {
      "command": "PASTE-THE-LINE-HERE"
    }
  }
}
```

**B. The file already has text in it.** Copy the file's contents, paste them into a chat with Claude and write:
*"Add an MCP server called xr18 with the command PASTE-THE-LINE-HERE to this config and give me the whole file
back."* Replace the whole file with the answer. This keeps your other settings safe.

6. Replace `PASTE-THE-LINE-HERE` with the line you copied in Step 3. Keep the quote marks around it. It should look
   like `"command": "C:/Users/Anna/.local/bin/xr18-mcp.exe"`. Use forward slashes `/`, as the command printed them.
7. Save the file (Ctrl+S) and close Notepad.
8. **Quit Claude completely** and open it again: right-click the Claude icon in the taskbar's notification area
   (next to the clock) and choose **Quit**. Closing the window is not enough.

### Step 5. Allow network access

The first time Claude connects to the mixer, Windows may ask whether to allow network access. Allow it for
**private networks**.

Now go to [Connecting to the mixer](#connecting-to-the-mixer).

---

## Connecting to the mixer

The computer has to be on the same network as the mixer. The program finds the mixer by itself.

**Behringer XR18.** Two options:
- Connect the computer to the mixer's own Wi-Fi (its name usually starts with `XR18-`).
- Or connect the computer to the same router or Wi-Fi as the mixer.

**Behringer X32 / Midas M32 (experimental).** The console has no Wi-Fi of its own. It connects by cable to a
router. Connect your computer to that router, by cable or its Wi-Fi. If the venue already uses an iPad or phone
app to control the console, join the same Wi-Fi as that tablet. If you're not sure, ask the venue's sound tech.
On a venue's console, ask before changing anything: start by only looking (*"What's on the board?"*).

**Quick check:** if **X AIR Edit** (XR18) or **X32-Edit** (X32) on the same computer can see the mixer, Claude can
too.

## Try it

Open Claude and start a new chat. The first time, Claude may ask for permission to use the **xr18** tools. Allow
it. Then try:

- *"What's on the board?"*
- *"Name channels 1-6: kick, snare, bass, guitar, vox 1, vox 2."*
- *"Everyone play loud for 10 seconds, then check the gains."*
- *"More vocal in bus 2."* Then: *"Undo that."*
- *"Save the board as rehearsal-1."* Later: *"What changed since rehearsal-1?"*

You can check the setup in Claude: **Settings → Developer** should list **xr18** as **running**.

## Safety

- **Risky moves need your OK.** Before it switches phantom power, makes a big jump in level, raises the main
  output above 0 dB, mutes many channels or loads a saved setup, Claude has to ask you. Only say yes when you mean
  it.
- **Everything can be undone.** Just say *"undo"*.
- **Automatic backup.** Each time Claude connects, it saves the mixer settings as they were. To get them back, say
  *"Restore the board as it was at the start of the session."*
- Backups and saved setups are stored in the **XR18-MCP** folder: in your home folder on a Mac (in Finder:
  **Go → Home**), in **Documents** on Windows.

## If something doesn't work

**Claude says the mixer was not found.**
- Check that the computer is on the mixer's network (see [Connecting to the mixer](#connecting-to-the-mixer)).
- On a Mac, check the Local Network permission (Mac Step 5).
- Tell the program the mixer's address. X AIR Edit or X32-Edit shows it, for example `192.168.1.1`. In the config
  file from Step 4, add an `env` line, so the `xr18` part looks like this (with your own command line):

  ```json
  "xr18": {
    "command": "/Users/anna/.local/bin/xr18-mcp",
    "env": { "XR18_IP": "192.168.1.1" }
  }
  ```

  This works for an X32 too. Save the file and restart Claude.

**Claude shows an error about its settings or the config file, or xr18 is missing from Settings → Developer.**
The config file probably has a typo: a missing comma, quote or bracket. Open it as in Step 4, copy everything, paste
it into a Claude chat and ask *"Fix this so it's valid JSON"*. Replace the file with the answer and restart Claude.

**Terminal or PowerShell says `uv` or `xr18-mcp` is not found.** Close the window, open it again and repeat the
command. If that doesn't help, repeat Step 2.

## Updating

Open Terminal (Mac) or PowerShell (Windows), paste this and press Enter/Return:

```
uv tool install --reinstall https://github.com/ramzed/xr18-mcp/archive/refs/heads/main.zip
```

Then quit and reopen Claude. You don't need to touch the config file again.

## Removing

```
uv tool uninstall xr18-mcp
```

Then delete the `xr18` part from the config file (Step 4).

---

## Other AI apps

The program works with any app that supports MCP. Do Steps 1-3 (and Step 5 on a Mac) as above, then:

- **Claude Code** (a command-line version of Claude): run
  `claude mcp add xr18 --scope user -- PASTE-THE-LINE-HERE`, using the line from Step 3. On a Mac, the Local
  Network permission goes to your terminal app (Terminal, iTerm or VS Code) instead of Claude.
- **Antigravity**: in the Agent panel, open **⋯ → MCP Servers → Manage MCP Servers → View raw config**, add the same
  `"xr18"` part as in Step 4, save, and click **Refresh**.

If someone sent you a file ending in `.whl` instead, install it in Step 3 with
`uv tool install ~/Downloads/<file name>.whl` (Mac) or `uv tool install $HOME\Downloads\<file name>.whl` (Windows).
