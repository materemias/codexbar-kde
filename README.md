# CodexBar for KDE Plasma 6

System-tray plasmoid showing AI coding-provider usage limits and reset
countdowns at a glance. Linux port of the macOS
[CodexBar](https://github.com/steipete/CodexBar) menu-bar app.

![CodexBar popup and tray indicators](docs/screenshot.png)

## Supported providers

| Provider       | Auth                                  | What it shows                                                     |
| -------------- | ------------------------------------- | ----------------------------------------------------------------- |
| **Claude**     | OAuth (`~/.claude/.credentials.json`) | 5h / 7d windows, plus Claude Design and Daily Routines quotas     |
| **Codex**      | OAuth (`~/.codex/auth.json`)          | 5h / weekly windows with reset countdowns                         |
| **z.ai**       | API key (`ZAI_API_KEY`)               | 5h and monthly windows                                            |
| **OpenRouter** | API key (`OPENROUTER_API_KEY`)        | Remaining balance; per-key allowance bar when a `keyLimit` is set |
| **Kilo**       | API key (`KILO_API_KEY`)              | Remaining credits balance                                         |

Each enabled provider gets its own section in the popup with a per-window
progress bar, percent used, and reset time. The compact tray view renders a
ring per provider/window you choose to expose.

## Requirements

- KDE Plasma **6**
- The [`codexbar`](https://github.com/steipete/CodexBar) CLI installed and on
  your `PATH` (defaults to `~/.local/bin/codexbar`)
- Python 3 (already present on every Plasma 6 system)
- `kpackagetool6` (ships with Plasma 6)

## Install

```sh
git clone https://github.com/materemias/codexbar-kde
cd codexbar-kde
kpackagetool6 -t Plasma/Applet -i .
```

Then in Plasma:

1. Right-click the panel → **Enter Edit Mode** → **Add Widgets…**
2. Search for **CodexBar** and drag it onto the panel.

The installed copy lives at `~/.local/share/plasma/plasmoids/org.codexbar.plasmoid/` —
it's a self-contained snapshot, so the source directory can live anywhere.

## Configure provider credentials

Plasmashell runs in its own environment, so API keys exported in `~/.zshrc` or
`~/.bashrc` are **invisible** to the plasmoid. The `codexbar` CLI reads
`~/.codexbar/config.json` and injects each provider's `apiKey` as the
appropriate env var before fetching — use this file for tokens the plasmoid
needs to see:

```json
{
  "version": 1,
  "providers": [
    {"id": "zai",  "enabled": true, "apiKey": "<from https://z.ai/manage-apikey/apikey>"},
    {"id": "kilo", "enabled": true, "apiKey": "<from app.kilo.ai>"}
  ]
}
```

Set permissions: `chmod 600 ~/.codexbar/config.json`.

Codex, Claude, and OpenRouter don't need entries here — they use their own
auth paths (`~/.codex/auth.json`, `~/.claude/.credentials.json`, and
`OPENROUTER_API_KEY` respectively).

## Update

```sh
cd /path/to/codexbar-kde
git pull
kpackagetool6 -t Plasma/Applet -u .
```

If files were deleted between versions, do a clean reinstall:

```sh
kpackagetool6 -t Plasma/Applet -r org.codexbar.plasmoid
kpackagetool6 -t Plasma/Applet -i .
```

## Uninstall

```sh
kpackagetool6 -t Plasma/Applet -r org.codexbar.plasmoid
```

## Restarting plasmashell

If the widget doesn't pick up changes (new env vars, deleted files, icon
caches), restart plasmashell:

```sh
plasmashell --replace
# or
kquitapp6 plasmashell && kstart plasmashell
```

## Configuration

Right-click the widget → **Configure CodexBar** to set:

- Polling interval (default 30s)
- Path to the `codexbar` CLI binary
- Which providers to query
- Tray indicators (which provider+window combos to show as rings)
- Tray icon size and compact style (ring+percent / ring only / percent only)

## License

MIT — see [`LICENSE`](LICENSE).

Third-party provider logos under `contents/icons/` are trademarks of their
respective owners, used solely for visual identification — see
[`NOTICE`](NOTICE) for attribution.
