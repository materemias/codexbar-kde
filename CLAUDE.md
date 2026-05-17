# CodexBar KDE Plasmoid

System-tray plasmoid showing AI coding-provider usage limits and reset countdowns. Linux port of macOS CodexBar (https://github.com/steipete/CodexBar).

## Stack

- Plasma 6 (KPackage `Plasma/Applet`)
- QML / Qt 6, `org.kde.plasma.plasmoid`, `Plasma5Support.DataSource` (executable engine)
- Python helper invoking `codexbar` CLI (Linux build from `steipete/CodexBar` releases)
- Plasmoid ID: `org.codexbar.plasmoid`

## Layout

```
metadata.json
contents/
  config/main.xml          # KConfigXT schema
  config/config.qml
  ui/main.qml              # PlasmoidItem root, polling Timer
  ui/CompactRepresentation.qml
  ui/FullRepresentation.qml
  ui/ProviderSection.qml
  ui/configGeneral.qml
  scripts/codexbar_fetch.py  # parallel CLI invocation, merges JSON
  icons/*.svg                # per-provider icons
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

## Known constraints

- System tray clamps popup height (~280-460px). No API override exists in Plasma 6. Keep popup compact; one row per window.
- `plasmoidviewer` is unreliable on Wayland (exits on focus loss). Test in actual panel.
- Claude OAuth fetch can take ~16s — keep helper timeout ≥30s.

## OpenRouter UX

Show balance in header. Render usage bar only when `keyLimit > 0` (per-key allowance set); otherwise no bar.

## Config keys (`main.xml`)

`cliPath`, `refreshSeconds`, `enableClaude/Codex/Zai/OpenRouter/Kilo`, `compactStyle` (0=ring+percent, 1=ring, 2=percent), `trayIndicators` (StringList of `provider:window` combos), `trayIconSize` (14-48px).
