# CodexBar KDE

KDE Plasma 6 system-tray applet for AI provider usage and active coding-agent sessions. It ports the macOS CodexBar CLI data to Linux and adds a machine-wide agent view.

## Architecture

- `contents/ui/main.qml` owns polling and normalized snapshots. `contents/scripts/codexbar_fetch.py` is the single provider normalization path.
- `contents/scripts/codexbar_agents.py` scans `/proc`, reads transcripts, and writes `~/.codexbar/agents.json`.
- `contents/config/main.xml` is the sole configuration schema. Settings pages live in `contents/ui/config*.qml`.
- `contents/scripts/codexbar_focus.py` and `contents/scripts/install_integration.py` implement terminal focus and integration setup.

Extend these paths instead of adding another polling, normalization, or configuration path.

## Development

```sh
# First install
kpackagetool6 -t Plasma/Applet -i .

# Upgrade after edits
kpackagetool6 -t Plasma/Applet -u .

# Clean reinstall after deleting packaged files
kpackagetool6 -t Plasma/Applet -r org.codexbar.plasmoid
kpackagetool6 -t Plasma/Applet -i .

# Reload the real panel from the user's session shell
kquitapp6 plasmashell && kstart plasmashell
```

Manual `plasmashell` restarts inherit the locale of the launching shell. Run
the restart from the user's session shell. For Hungarian 24 hour formatting,
use:

```sh
kquitapp6 plasmashell \
  && env LANG=hu_HU.utf8 LC_ALL=hu_HU.utf8 LC_TIME=hu_HU.utf8 \
       kstart plasmashell
```

For changed QML, run `qmllint`. For changed Python, run
`uv run python -m py_compile <files>`. Install the package and verify UI
behavior in the actual panel. `plasmoidviewer` is unreliable on Wayland and can
exit on focus loss.

When completing a feature, update README.md in the same change.

## Provider contracts

- Claude and Codex use OAuth, with CLI fallback. z.ai, OpenRouter, and Kilo use automatic source selection.
- Claude `extraRateWindows` require OAuth.
- Codex emits one normalized record per account. Legacy tray keys use `codex:<window>`. Account-specific keys use `codex:<encoded-email>:<window>`.
- Codex Spark windows are intentionally filtered out.
- Provider order is Claude, Codex, z.ai, OpenRouter, Kilo on every surface.
- OpenRouter shows balance in the header. It renders a usage bar only when `keyLimit > 0`.

Plasmashell does not inherit API keys from shell startup files.
`~/.codexbar/config.json` must be mode `0600`; the CLI reads it for provider
`apiKey` values and additional Codex profile homes. Default Codex and Claude
authentication remains in `~/.codex/auth.json` and
`~/.claude/.credentials.json`. See README.md section "Configure provider
credentials" for setup.

## Agent contracts

- The aggregator scans `/proc` every tick. Processes without a hook sentinel remain visible as `untracked`.
- Each session record carries up to eight recent user or assistant turns, capped at 320 characters each. Consecutive tool-only turns collapse into one summary.
- omp advisor sidecars named `__advisor.*.jsonl` are never session rollouts.
- Session filtering uses fuzzy subsequence matching within one field. Recent conversation text uses exact case-insensitive substring matching.
- `codexbar://focus/<sessionId>` dispatches to `codexbar_focus.py`. It walks
  process ancestors, tries Kitty remote control, then falls back to KWin
  activation. `install_integration.py` registers the handler.

## Plasma constraints

- Plasma clamps popup height. Keep usage sections compact.
- Claude OAuth fetches can take about 16 seconds. Keep the provider helper timeout at least 30 seconds.
- `plasmashell` runs as a transient service. `systemctl --user restart plasma-plasmashell` does not reload it reliably.
