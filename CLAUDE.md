# CodexBar KDE

KDE Plasma 6 system-tray applet for AI provider usage and active coding-agent sessions. It ports the macOS CodexBar CLI data to Linux and adds a machine-wide agent view.

## Architecture

- `contents/ui/main.qml` owns polling and normalized snapshots. `contents/scripts/codexbar_fetch.py` is the single provider normalization path.
- `contents/scripts/codexbar_agents.py` scans `/proc`, reads transcripts, and writes `~/.codexbar/agents.json`.
- `contents/config/main.xml` is the sole configuration schema. Settings pages live in `contents/ui/config*.qml`.
- `contents/scripts/codexbar_focus.py` and `contents/scripts/install_integration.py` implement terminal focus and integration setup.
- `native/` exposes asynchronous `QProcess` commands to QML. Keep execution here;
  unique executable DataSource names make Plasma's dynamic metadata grow.

Extend these paths instead of adding another polling, normalization, or configuration path.

## Development

```sh
# Build the complete package, including its native QML plugin
cmake -S . -B build -DCODEXBAR_BUILD_TESTS=ON
cmake --build build
ctest --test-dir build --output-on-failure

# First install
kpackagetool6 -t Plasma/Applet -i build/package

# Upgrade after edits; stop Plasma before replacing its loaded native library
systemctl --user stop plasma-plasmashell.service
kpackagetool6 -t Plasma/Applet -u build/package
systemctl --user start plasma-plasmashell.service

# Clean reinstall after deleting packaged files
systemctl --user stop plasma-plasmashell.service
kpackagetool6 -t Plasma/Applet -r org.codexbar.plasmoid
kpackagetool6 -t Plasma/Applet -i build/package
systemctl --user start plasma-plasmashell.service
```

Confirm with `systemctl --user is-active plasma-plasmashell.service`.
`kquitapp6 plasmashell` returns before the process exits, so an immediate
`systemctl --user start` hits a still-active unit, does nothing, and the panel
stays down; `kstart plasmashell` from an agent's tool shell also left it down.
`systemctl --user stop` blocks until the unit is inactive. The unit takes its
locale from `systemctl --user show-environment`, which carries
`LC_TIME=hu_HU.UTF-8` for 24 hour formatting.

For changed QML, run `qmllint` on the staged files under `build/package`.
For changed Python, run `uv run python -m py_compile <files>`. Install the
built package and verify UI behavior in the actual panel. `plasmoidviewer`
is unreliable on Wayland and can exit on focus loss.

When completing a feature, update README.md in the same change.

## Provider contracts

- Claude and Codex use OAuth, with CLI fallback. z.ai, OpenRouter, and Kilo use automatic source selection.
- Claude `extraRateWindows` require OAuth.
- Codex emits one normalized record per account. Legacy tray keys use `codex:<window>`. Account-specific keys use `codex:<encoded-email>:<window>`.
- Codex Spark windows are intentionally filtered out.
- Provider order is Claude, Codex, z.ai, OpenCode Go, OpenRouter, Kilo, TypeSafe on every surface.
- OpenCode Go (`opencodego`) uses the `api` source with an `apiKey` in `~/.codexbar/config.json`; the CLI's auto pick is a `local` estimate that is far off the real quota, kept only as fallback. Its `tertiary` slot is the monthly window and is never hidden at 0%. OpenCode Zen (`opencode`) is web-only on macOS and unsupported here.
- OpenRouter shows balance in the header. It renders a usage bar only when `keyLimit > 0`.
- TypeSafe (`typesafe`, CLI 0.64.0+) is balance-only and off by default: no `primary`, header shows `Balance` parsed from `loginMethod`, no tray meter. Browser cookie import is macOS-only; Linux needs `cookieSource: "manual"` plus `cookieHeader` in `~/.codexbar/config.json`.

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
- `systemctl --user restart plasma-plasmashell` does not reload the applet reliably; use the stop, install, start sequence under Development.
