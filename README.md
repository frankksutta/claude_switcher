# Claude Code · Account Switcher

A small Windows desktop tool for running **multiple Claude Code accounts at the
same time**. Each launch opens its own PowerShell shell with its own isolated
environment, so sessions on different accounts (or backends) run side by side
without interfering. It also bundles a few conveniences: a one-click **Claude
Cowork** (desktop app) launcher, a **disk-usage monitor** with safe cleanup, and
a **"reduce confirmations"** helper for Claude Code's permission prompts.

It's a single-file Tkinter app (`claude_switcher.pyw`) — no install, no
dependencies beyond a standard Python.

![profiles → pick one → launch](#) <!-- add a screenshot if you like -->

## Features

### Account / profile switching
Pick a profile and launch — the new shell points the Claude CLI at that
profile's own config directory via `CLAUDE_CONFIG_DIR`, so each account stays
isolated. Run the switcher again to open another account in parallel.

- **Team/subscription profiles** strip all cloud-backend environment variables
  (`CLAUDE_CODE_USE_BEDROCK`, `ANTHROPIC_MODEL`, `ANTHROPIC_API_KEY`, …) so the
  session is a clean seat login — otherwise a globally-set backend variable leaks
  in and the team backend rejects it.
- **A Bedrock profile** sets `CLAUDE_CODE_USE_BEDROCK=1` to use AWS Bedrock
  instead of a subscription login.

Profiles are **configuration, not code** — see [Configuring your accounts](#configuring-your-accounts).

### Project directory + recent folders
Browse to a project folder and the launched shell starts there. The most recent
folders appear as **quick-select buttons**, and the button matching the current
directory is highlighted. Leave the directory empty to start in the default
location.

### Extra launch options
- **Extra PowerShell** — also open a second plain PowerShell window in the
  project dir (for git/scripts next to your Claude session).
- **Open File Explorer** — also open a File Explorer window in the project dir.
- **Full-card click** — click anywhere on a profile card to select it.

### Claude Cowork (desktop app)
One button launches the **Claude Desktop** app straight into a new Cowork
session (via the `claude://cowork/new` deep link), or — if the app is already
running — just brings its window to the front. Cowork uses the desktop app's own
login; the CLI profiles above don't apply to it.

### Disk-usage monitor + safe cleanup
A bar shows how much disk the Claude Desktop app is using (green under the limit,
red over it). Most of the footprint is the Claude Code VM image. When you're over
the limit, a **Clean up excess** button lights up and reclaims space by deleting
the VM's live disk (`rootfs.vhdx`, several GB) plus the browser caches.

This is the **same rotation the app performs itself** — the VM is rebuilt from a
kept compressed copy on the next launch (so that first launch is slower), and
your session data is left untouched. Claude Desktop must be fully quit first; the
cleanup refuses to run while it's open. The green limit defaults to 8 GB and can
be changed via `"disk_limit_gb"` in the prefs file.

### Reduce confirmations
The **🛡 Reduce confirmations…** button opens a window that lowers Claude Code's
permission prompts, either for the selected project
(`.claude/settings.local.json`) or globally (`~/.claude/settings.json`):

- **Auto-accept edits** lets Claude edit files in the project without asking (it
  still asks for shell commands and out-of-project writes).
- **Skip all confirmations** silences everything — use only in trusted/throwaway
  setups.

It only ever changes `permissions.defaultMode` and preserves your other settings.

### Other
- Help dialog (the **?** button) explaining every control.
- Window preferences and recent folders persist between runs.

## Requirements

- **Windows** (the tool launches PowerShell, uses Windows-specific app paths, and
  the Cowork deep link targets the Windows Claude Desktop app).
- **Python 3** with Tkinter (bundled with the standard python.org installer).
- **[Claude Code](https://www.anthropic.com/claude-code) CLI** on `PATH` (`claude`).
- **Claude Desktop** app — only needed for the Cowork button and the disk monitor.

## Running it

Double-click `claude_switcher.pyw`, or from a terminal:

```powershell
python claude_switcher.pyw
```

Pick a profile, optionally set a project directory, then **Launch in PowerShell**.
Run the switcher again to open another account alongside the first.

## Configuring your accounts

Account profiles live in `claude_switcher_profiles.json` (next to the app). This
file is **gitignored** because it holds your real account details. On first run —
or if the file is missing or invalid — the app writes a generic default you can
edit.

To set up your own accounts, copy the shipped example and edit it:

```powershell
copy claude_switcher_profiles.example.json claude_switcher_profiles.json
```

Each profile is an object:

```json
{
  "label": "My account (Team)",
  "tag": "TEAM-1",
  "color": "#00c8ff",
  "config_dir": "~/.claude-team1",
  "mode": "claude",
  "env_set": {},
  "env_remove": ["CLAUDE_CODE_USE_BEDROCK", "ANTHROPIC_MODEL", "ANTHROPIC_API_KEY"]
}
```

| Field | Meaning |
|-------|---------|
| `label` | Shown on the profile card |
| `tag` | Short badge on the card |
| `color` | Accent color (hex) |
| `config_dir` | The account's `CLAUDE_CONFIG_DIR` (`~` is expanded) |
| `mode` | `"claude"` (subscription/team) or `"bedrock"` (AWS Bedrock) |
| `env_set` | Environment variables to set for this launch |
| `env_remove` | Environment variables to strip for this launch |

First-time setup of a new seat: launch that profile once and run
`claude auth login` in the shell it opens — the login is saved under that
profile's `config_dir`.

## Development

The app is a single Tkinter file. Per the project's testing approach, the pure
logic (settings engine, profile loader, disk math) is GUI-free and the window
tests instantiate/destroy a single shared root — no display server gymnastics
needed.

```powershell
python -m pytest test_claude_switcher.py
```

Files:

- `claude_switcher.pyw` — the app.
- `test_claude_switcher.py` — headless tests.
- `claude_switcher_profiles.example.json` — template for your accounts.
- `claude_switcher_profiles.json` — your real accounts (gitignored, generated).
- `claude_switcher_prefs.json` — window/recent-dir state (gitignored, generated).
