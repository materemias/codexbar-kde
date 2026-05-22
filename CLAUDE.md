# CodexBar KDE Plasmoid

System-tray plasmoid for KDE Plasma 6 with two surfaces:

1. **Usage** — AI coding-provider rate limits and reset countdowns (Claude, Codex, z.ai, OpenRouter, Kilo)
2. **Agent View** — real-time overview of every active coding-agent session on the machine, with one-click terminal focus

Linux port of macOS CodexBar (https://github.com/steipete/CodexBar).

## Stack

- Plasma 6 (KPackage `Plasma/Applet`)
- QML / Qt 6, `org.kde.plasma.plasmoid`, `Plasma5Support.DataSource` (executable engine)
- Python helper invoking `codexbar` CLI (Linux build from `steipete/CodexBar` releases)
- Python agent aggregator scanning `/proc` for Claude, Codex, OpenCode, pi/omp sessions
- Plasmoid ID: `org.codexbar.plasmoid`

## Layout

```
contents/
  config/main.xml              # KConfigXT schema
  config/config.qml             # Settings tab definitions (Backend / Providers / Tray / Agents)
  ui/main.qml                   # PlasmoidItem root, polling Timer, helpers
  ui/CompactRepresentation.qml  # Tray: rings, state dots, topic label
  ui/FullRepresentation.qml     # Popup: tab bar (Usage / Agents)
  ui/ProviderSection.qml        # Per-provider usage section
  ui/AgentsSection.qml          # Agent list with folder groups
  ui/configBackend.qml          # Settings → Backend
  ui/configProviders.qml        # Settings → Providers
  ui/configTray.qml             # Settings → Tray
  ui/configAgents.qml           # Settings → Agents (incl. integration install)
  scripts/codexbar_fetch.py     # Parallel CLI invocation, merges JSON
  scripts/codexbar_agents.py    # Agent state aggregator (/proc scanner)
  scripts/codexbar_focus.py     # Click-to-focus: KWin + Kitty activation
  scripts/install_integration.py # One-shot: env scripts + URL handler + cleanup
  icons/*.svg                   # Per-provider icons
```

## Dev workflow

```sh
# install (first time)
kpackagetool6 -t Plasma/Applet -i .

# upgrade after edits
kpackagetool6 -t Plasma/Applet -u .

# clean reinstall (when files are deleted, not just modified)
kpackagetool6 -t Plasma/Applet -r org.codexbar.plasmoid
kpackagetool6 -t Plasma/Applet -i .

# restart plasmashell (real one runs as transient app-plasmashell@*.service,
# `systemctl --user restart plasma-plasmashell` does NOT work)
plasmashell --replace
# or
kquitapp6 plasmashell && kstart plasmashell
```

Installed copy lives at `~/.local/share/plasma/plasmoids/org.codexbar.plasmoid/` — self-contained, independent of source path.

## Provider sources (Linux)

| Provider | `--source` |
|---|---|
| `claude` | `oauth` (fallback `cli` on HTTP 429) |
| `codex` | `oauth` (fallback `cli`) |
| `zai` | auto (= api) |
| `openrouter` | auto |
| `kilo` | auto |

`extraRateWindows` (Claude Design, Daily Routines) only present with `--source oauth`. Cookie/web providers are macOS-only (SweetCookieKit gated by `#if os(macOS)`).

## Agent discovery

The aggregator (`codexbar_agents.py`) scans `/proc` every tick:

- **Claude Code**: `pgrep claude` → reads `~/.claude/projects/*/sessions/*/transcript.jsonl` for window title + last prompt
- **Codex CLI**: `pgrep codex` → reads `~/.codex/sessions/*/transcript.jsonl`
- **OpenCode**: `pgrep opencode` → reads transcript JSONL
- **pi / omp**: `pgrep -x pi` → reads `~/.pi/agent/sessions/` or `~/.omp/agent/sessions/` JSONL rollouts

Sessions without a hook sentinel file appear as "untracked" — still visible with state and cwd, just no task title.

## Click-to-focus

Clicking an agent row opens `codexbar://focus/<sessionId>`, handled by `codexbar_focus.py`:
1. Walks `/proc` ancestors from the session PID to find the terminal emulator
2. For Kitty: uses the remote control socket to focus the right tab/window
3. Falls back to KWin scripting (`kwin-console`) to activate the window

The `install_integration.py` script registers the URL scheme handler.

## Credentials — `~/.codexbar/config.json` (REQUIRED for plasmoid)

Plasmashell runs in a separate environment from your interactive shell, so `KILO_API_KEY`/`ZAI_API_KEY` exported in `~/.zshrc` are **invisible** to the plasmoid subprocess. The CLI also reads `~/.codexbar/config.json` and injects each provider's `apiKey` as the appropriate env var before fetching. Use this file for tokens the plasmoid needs:

```json
{
  "version": 1,
  "providers": [
    {"id": "zai",  "enabled": true, "apiKey": "<from https://z.ai/manage-apikey/apikey>"},
    {"id": "kilo", "enabled": true, "apiKey": "<from app.kilo.ai or KILO_API_KEY env>"}
  ]
}
```

File must be `chmod 600`. Codex/Claude/OpenRouter don't need entries here — they use other auth paths (Codex/Claude OAuth tokens in `~/.codex/auth.json` / `~/.claude/.credentials.json`; OpenRouter via `OPENROUTER_API_KEY` env which the CLI reads).

**Quick proof config.json works**: `env -u ZAI_API_KEY -u KILO_API_KEY codexbar usage --provider zai,kilo --json`.

## Canonical provider order

**Claude → Codex → z.ai → OpenRouter → Kilo**. Applied in compact rep, popup, tooltip, settings — regardless of saved config order.

## Keyboard shortcuts

- `Super+A`: opens popup and switches to Agents tab
- `↑`/`↓`: navigate agent rows
- `Enter`: focus the terminal hosting the selected agent session
- `Esc`: close popup

## Config keys (`main.xml`)

`cliPath`, `refreshSeconds`, `enableClaude/Codex/Zai/OpenRouter/Kilo`, `compactStyle` (0=ring+percent, 1=ring, 2=percent), `trayIndicators` (StringList of `provider:window` combos), `trayIconSize` (14-48px), `showAgents`, `agentBlockedBadge`, `showAgentStateDots`, `agentStateDotsScale` (50-200%), `showAgentPrompts`, `includeUntrackedAgents`, `agentsRefreshSeconds` (2-120), `showAgentTopicInPanel`, `agentTopicMaxWidth` (80-800px), `closePopupOnFocusLoss`.

## Known constraints

- System tray clamps popup height (~280-460px). No API override exists in Plasma 6. Keep popup compact; one row per window.
- `plasmoidviewer` is unreliable on Wayland (exits on focus loss). Test in actual panel.
- Claude OAuth fetch can take ~16s — keep helper timeout ≥30s.

## OpenRouter UX

Show balance in header. Render usage bar only when `keyLimit > 0` (per-key allowance set); otherwise no bar.
