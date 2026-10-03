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
- Recent consumption (`window.recent`: `hours`, `consumedPercent`) is computed by the fetcher for `windowMinutes` 10080 (24h lookback) and 300 (1h). Its run-length `history` lives in `usage_cache.json` next to `providers`, keyed `<provider>:<casefolded email>:<slot or extra:id>`, written under the same lock. A usage drop or a reset time moving later starts a new cycle whose usage counts in full; a change across a gap over 15 minutes spanning the lookback start is left out. QML only displays it.

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
- Live records carry the latest `recap` and `recapStale` (turns followed it; QML prefixes `Earlier:`). omp: the newest `session_recaps` row in `~/.omp/agent/history.db` (read-only, one query per tick), stale when older than the last turn; `created_at` is whole seconds. Claude: the transcript's last `system`/`away_summary` record, minus its `(disable recaps in /config)` suffix, stale after any later user or assistant record. Codex has none. History rows keep a non-stale `recap` and turn `ts`; `_aggregate` backfills `lastState: idle` omp rows, and a row without turn `ts` drops its saved recap, recovers `ts` from the rollout, and is skipped if it still cannot be dated. QML shows it only under the keyboard-selected row (`selectedKey`) on both tabs.
- Session filtering matches one field at a time: a case-insensitive substring, else a subsequence whose every run of matched characters starts a word. Recent conversation text uses exact case-insensitive substring matching. Every match is visible, highlighted in a shown label or as a snippet line for an unshown field. Filtering toggles row `visible`; the Repeater models stay unfiltered so typing does not rebuild delegates.
- `codexbar://focus/<sessionId>` dispatches to `codexbar_focus.py`. It walks
  process ancestors, tries Kitty remote control or `tern focus`, then falls
  back to KWin activation. `install_integration.py` registers the handler.
- Tern runs panes under a `tern daemon` that outlives its windows, so a
  window need not be a pane's ancestor; `tern` host chains append every
  non-daemon `tern` pid. Focus reads `TERN_PANE` and `TERN_PANE_SOCKET` from
  the agent's environment. Launch runs `tern new tab`, then `tern focus` on
  the returned block, since command-line tabs open in the background; it
  opens a Tern window in a systemd scope first unless a non-daemon `tern`
  window runs and the daemon answers `tern ls`.
  `LAUNCH_HOSTS` and QML `launchHost` list the launchable hosts.
- The focused-session marker (QML `focusedAgentKey`) follows the last
  active task window, since the open popup is not a task, and draws a ring
  around the row's state dot. A Tern record carries `ternSession` (the
  session name, which is the window caption) when its pane is the focused
  block of the tab `tern ls` shows, or of a session's only tab; a Tern
  window marks only a record whose `ternSession` equals its caption. Other
  hosts pick one candidate by pid, then by `windowTitle` or cwd basename in
  the caption; ambiguity marks nothing.
- `agents.json` `history` holds ended sessions with `closedBy`: `reboot`
  rows (every session of the previous boot, kept until live again) and
  `exit` rows (same-boot exits, newest 100, untracked excluded). The History
  tab orders reboot rows by desktop and exit rows newest first.
  A pid still live under another session id changed identity and is not
  an exit. A relaunched session inherits its history row's desktop until the
  window lookup reports one. `omp --resume <id>` names its session before
  the process opens the rollout fd; the cwd slug fallback would pick another.
- Codex 0.15x loads threads in one `codex app-server` per `CODEX_HOME`
  that all TUIs share, and it keeps a closed thread loaded for about a
  minute. A TUI owns a loaded thread named by its `resume <id>` argv, or the
  only thread in its folder created after it started when no other fresh TUI
  runs there. Resume commands carry a non-default `CODEX_HOME`.
- pi overwrites its process title, so `--session <id>` is read from the
  parent shell's `-c` command (how Launch starts it). A fresh pi owns the
  only rollout in its slug whose header falls in its first 30 seconds.
- T3 Code runs Claude with stream-json flags and Codex as one
  `codex app-server` per thread, under its `t3code` Electron process.
  `_under_t3` makes these interactive despite headless or service argv, and
  keeps them out of the shared Codex rollout pool. The host is `t3code`.
  Focus opens `t3code://threads/<environment-id>/<thread>`, mapped from the
  session id through `provider_session_runtime.resume_cursor_json` in
  `~/.t3/userdata/state.sqlite`, then activates the window through KWin.
- Claude Code's agent view runs tasks as `kind: "bg"` sessions under
  `claude daemon`, with no terminal ancestor; they are not listed.
- `codexbar_focus.py --launch <provider> <sessionId>` resumes a kitty or
  Tern history record only when its saved resume command matches
  `_resume_command` and the session is not live. For kitty it runs kitty under
  `systemd-run --user --scope` so a plasmashell stop does not kill it. It
  switches to the saved desktop before starting kitty, and a self-unloading
  KWin script keeps the window there by pid and activates it.
  `--launch-all <uri-json [[provider, id]]>` runs `launch` sequentially over
  exactly those kitty and Tern reboot rows in desktop order and prints the pairs that
  launched; QML keeps the cooldown only for those. `codexbar_agents.py --dismiss <uri-json [[provider, id]]>`
  removes history rows under the aggregate writer lock; QML never edits
  `agents.json` itself.
- `codexbar_focus.py --teleport <provider> <sessionId>` moves an idle live
  kitty session to a new Tern tab. It re-reads the pid's session id and
  state, starts Tern first, sends the agent SIGHUP (SIGTERM after 3 s),
  hangs up its shell when that shell is kitty's direct child (closing the
  window), and resumes only after the agent exited, since a session file has
  one live writer. QML shows the button when `agents.json` `ternInstalled`
  is true and the row has a `resumeCommand`.

## Plasma constraints

- Plasma clamps popup height. Keep usage sections compact.
- Claude OAuth fetches can take about 16 seconds. Keep the provider helper timeout at least 30 seconds.
- `systemctl --user restart plasma-plasmashell` is fine for a plain restart, but an upgrade must stop Plasma before `kpackagetool6 -u` replaces the loaded native library; use the stop, install, start sequence under Development.
