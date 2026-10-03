import QtQuick
import QtQuick.Layouts
import QtQuick.Window
import QtCore
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import "process" as Process
import org.kde.taskmanager as TaskManager
import org.kde.kirigami as Kirigami
import "Command.js" as Command
import "Usage.js" as Usage
import "Notifications.js" as Notifications

PlasmoidItem {
    id: root

    // Auto-close on focus loss. Bound to the config flag (default on) so
    // users can pin the popup open by unchecking "Close popup when focus
    // moves away" in Settings.
    hideOnWindowDeactivate: Plasmoid.configuration.closePopupOnFocusLoss !== false

    // Super+A: Plasma's default toggleExpanded() runs first, then the
    // activated() signal fires. We just nudge the tab to Agents — the popup
    // is already open by that point.
    property string requestedTab: ""
    onExpandedChanged: {
        root.nowMs = Date.now()
        if (!root.expanded) root.requestedTab = ""
        // Tern pane focus is read per sweep; refresh it for the indicator.
        else root.runAggregator()
    }

    Connections {
        target: Plasmoid
        function onActivated() { root.requestedTab = "agents" }
    }

    property var snapshot: ({
        updatedAt: "",
        providers: [],
        fatal: null,
        forecast: null,
        codexRotation: null,
        cliVersion: null
    })
    property var agentSnapshot: ({
        updatedAt: "",
        counts: { working: 0, blocked: 0, idle: 0, untracked: 0, total: 0 },
        agents: [],
        history: []
    })
    property bool loading: false
    property bool agentsLoading: false
    property bool aggregatorLoading: false
    property var agentsRequest: null
    property string fetchKind: "startup-cache"
    readonly property bool cacheRestoring: root.fetchKind === "startup-cache"
    property int backendGeneration: 0
    property int fetchGeneration: 0
    property var usageNotificationState: ({})
    property var agentNotificationState: ({ enabled: false, sessions: {} })
    property var ompModeNotificationState: null
    property string notificationError: ""
    property var pendingNotifications: ({})
    property int notificationSequence: 0
    readonly property int notificationTimeoutMs: 10000
    property string lastError: ""
    property string agentsError: ""

    readonly property string scriptPath: Command.localPath(
        Qt.resolvedUrl("../scripts/codexbar_fetch.py"))
    // QtCore's QML StandardPaths returns a QUrl, not a filesystem string.
    // Keep its URL escaping intact for XHR.
    readonly property string agentsFileUrl: StandardPaths.writableLocation(
        StandardPaths.HomeLocation).toString().replace(/\/$/, "") + "/.codexbar/agents.json"
    readonly property int agentsRefreshMs: Math.max(2, Plasmoid.configuration.agentsRefreshSeconds || 5) * 1000
    readonly property bool agentsEnabled: Plasmoid.configuration.showAgents !== false
        || Plasmoid.configuration.showAgentStateDots !== false
        || Plasmoid.configuration.agentBlockedBadge === true
        || Plasmoid.configuration.showAgentTopicInPanel === true
        || root.agentNotificationsEnabled
    readonly property bool usageNotificationsEnabled:
        Plasmoid.configuration.usageNotifications !== false
    readonly property bool agentNotificationsEnabled:
        Plasmoid.configuration.agentNotifications === true
    readonly property bool ompModeNotificationsEnabled:
        Plasmoid.configuration.ompModeNotifications !== false
    onOmpModeNotificationsEnabledChanged: {
        root.ompModeNotificationState = Notifications.ompMode(
            root.ompModeNotificationState, null, false).state
    }
    onAgentNotificationsEnabledChanged: {
        // The next successful scan establishes a silent baseline after enabling.
        root.agentNotificationState = { enabled: false, sessions: {} }
    }
    readonly property bool includeUntrackedAgents:
        Plasmoid.configuration.includeUntrackedAgents !== false
    onIncludeUntrackedAgentsChanged: root.refreshAgents()
    readonly property string cliPath: Command.cliPath(Plasmoid.configuration.cliPath)
    onCliPathChanged: {
        // A generation also rejects an old request after A → B → A.
        root.backendGeneration++
        root.snapshot = {
            updatedAt: "", providers: [], fatal: null, forecast: null,
            codexRotation: null, cliVersion: null
        }
        root.usageNotificationState = {}
        root.ompModeNotificationState = null
        root.lastError = ""
        if (!root.cacheRestoring && !root.loading) Qt.callLater(root.refresh)
    }
    readonly property bool codexForecastEnabled:
        Plasmoid.configuration.enableCodex !== false
        && Plasmoid.configuration.showCodexResetForecast !== false
    readonly property bool showRecentUsage: Plasmoid.configuration.showRecentUsage !== false
    readonly property int refreshMs: Math.max(10, Plasmoid.configuration.refreshSeconds || 30) * 1000
    readonly property var enabledProviders: {
        // Display order: Claude → Codex → z.ai → OpenCode Go → OpenRouter → Kilo → TypeSafe
        // (preserved in tray rings, popup sections, tooltip, settings).
        var ids = []
        if (Plasmoid.configuration.enableClaude)     ids.push("claude")
        if (Plasmoid.configuration.enableCodex)      ids.push("codex")
        if (Plasmoid.configuration.enableZai)        ids.push("zai")
        if (Plasmoid.configuration.enableOpenCodeGo) ids.push("opencodego")
        if (Plasmoid.configuration.enableOpenRouter) ids.push("openrouter")
        if (Plasmoid.configuration.enableKilo)       ids.push("kilo")
        if (Plasmoid.configuration.enableTypeSafe)   ids.push("typesafe")
        return ids
    }
    onEnabledProvidersChanged: {
        if (root.enabledProviders.indexOf("codex") < 0) {
            root.snapshot = Object.assign({}, root.snapshot, { codexRotation: null })
            root.ompModeNotificationState = Notifications.ompMode(
                root.ompModeNotificationState, null, false).state
        }
    }

    preferredRepresentation: Plasmoid.formFactor === PlasmaCore.Types.Planar
        ? fullRepresentation
        : compactRepresentation
    compactRepresentation: CompactRepresentation { }
    fullRepresentation: FullRepresentation { }

    property real nowMs: Date.now()
    readonly property bool tooltipHovered: root.compactRepresentationItem
        ? root.compactRepresentationItem.tooltipHovered === true : false
    readonly property bool uiClockActive: root.expanded || root.tooltipHovered
        || (Plasmoid.formFactor === PlasmaCore.Types.Planar && root.visible)
    onTooltipHoveredChanged: if (root.tooltipHovered) root.nowMs = Date.now()
    onUiClockActiveChanged: if (root.uiClockActive) root.nowMs = Date.now()
    Timer {
        interval: 1000
        running: root.uiClockActive
        repeat: true
        onTriggered: root.nowMs = Date.now()
    }

    // The KDE System Tray container reads these instead of any ToolTipArea
    // inside the compact representation. Keep them in sync with the widget data.
    function _resetTimeLeft(rec, now) {
        if (!rec || !rec.resetsAt) return ""
        var diff = new Date(rec.resetsAt).getTime() - now
        if (diff <= 0) return "soon"
        return relativeMs(diff).replace(/^in /, "")
    }
    toolTipMainText: root.lastError ? "CodexBar — error" : "CodexBar"
    // RichText so the body can use an HTML table to right-align the percent
    // and time-left columns. PC3.Label inside Plasma's tooltip honours this.
    toolTipTextFormat: Text.RichText
    // Tooltip stays focused on the recurring usage windows users actually
    // watch: Claude 5h+7d, Codex 5h+7d, z.ai 5h+monthly, OpenCode Go 5h+7d.
    // Extras (Sonnet, Claude Design, Routines) and balance-only providers (OpenRouter, Kilo, TypeSafe)
    // are intentionally omitted — they're available in the popup.
    readonly property var _tooltipSlots: ({
        claude: ["primary", "secondary"],
        codex:  ["primary", "secondary"],
        zai:    ["primary", "secondary"],
        opencodego: ["primary", "secondary"]
    })
    toolTipSubText: {
        var arr = root.snapshot.providers || []
        if (arr.length === 0)
            return root.lastError || (root.loading ? "Loading…" : "No providers enabled")
        var labels = { codex: "Codex", claude: "Claude", zai: "z.ai", opencodego: "OpenCode Go" }
        // width="240" widens the tooltip a touch so the columns don't crowd.
        // Cellpadding gives horizontal breathing room between label / pct /
        // time-left without forcing a wider column with &nbsp;.
        var html = '<table width="240" cellpadding="2" cellspacing="0">'
        // Spacer row beneath the "CodexBar" title.
        html += '<tr><td colspan="3" style="font-size: 6px">&nbsp;</td></tr>'
        var anyData = false
        for (var i = 0; i < arr.length; i++) {
            var rec = arr[i]
            var allowedSlots = root._tooltipSlots[rec.id]
            if (!allowedSlots) continue
            var name = labels[rec.id] || rec.id
            if (rec.id === "codex" && rec.accountEmail) {
                var account = root.accountDisplayName(rec)
                    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
                name += " · " + account
            }
            if (rec.stale === true) {
                var cached = new Date(rec.cachedAt)
                name += " · last known"
                    + (isNaN(cached.getTime()) ? "" : " " + _absoluteTime(cached, root.nowMs))
            }
            if (!rec.ok) {
                if (anyData) {
                    html += '<tr><td colspan="3" style="font-size: 6px">&nbsp;</td></tr>'
                }
                html += '<tr><td colspan="3"><b>' + name + '</b>: error</td></tr>'
                anyData = true
                continue
            }
            var providerRows = ''
            for (var s = 0; s < allowedSlots.length; s++) {
                var w = rec[allowedSlots[s]]
                if (!w || w.usedPercent === undefined || w.usedPercent === null) continue
                var wl = windowLabel(rec.id, allowedSlots[s], w, "")
                var pctText = Math.round(w.usedPercent) + "%"
                var resetText = rec.stale === true ? "" : _resetTimeLeft(w, root.nowMs)
                providerRows += '<tr>'
                    + '<td>' + wl + '</td>'
                    + '<td align="right">' + pctText + '</td>'
                    + '<td align="right">' + resetText + '</td>'
                    + '</tr>'
            }
            if (providerRows.length > 0) {
                if (anyData) {
                    html += '<tr><td colspan="3" style="font-size: 6px">&nbsp;</td></tr>'
                }
                html += '<tr><td colspan="3"><b>' + name + '</b></td></tr>' + providerRows
                anyData = true
            }
        }
        if (anyData && root.snapshot.updatedAt) {
            // Spacer + footer "updated …" inside the same table so the column
            // grid (and thus right-alignment) stays consistent.
            var t = new Date(root.snapshot.updatedAt)
            html += '<tr><td colspan="3" style="font-size: 6px">&nbsp;</td></tr>'
                  + '<tr><td colspan="3">updated '
                  + t.toLocaleTimeString(Qt.locale(), Locale.ShortFormat)
                  + '</td></tr>'
        }
        html += '</table>'
        return anyData ? html : ""
    }

    function dispatchNotifications(events) {
        if (events.length === 0) return
        root.notificationError = ""
        for (var i = 0; i < events.length; i++) {
            var event = events[i]
            var cmd = "if ! command -v notify-send >/dev/null 2>&1; then "
                + "printf '%s\\n' 'notify-send is unavailable; install libnotify to enable desktop notifications.' >&2; "
                + "exit 127; fi; notify-send --app-name " + Command.shellQuote("CodexBar")
                + " --icon " + Command.shellQuote("dialog-information")
                // Plasma never auto-hides critical urgency, so every alert
                // stays normal and expires.
                + " --urgency normal --expire-time " + root.notificationTimeoutMs
                + " -- " + Command.shellQuote(event.title) + " " + Command.shellQuote(event.body)
                + " # " + (++root.notificationSequence)
            root.pendingNotifications[cmd] = {
                event: event, backendGeneration: root.backendGeneration
            }
            notificationRunner.run(cmd)
        }
    }

    Process.CommandRunner {
        id: notificationRunner
        onFinished: function(command, exitCode, standardOutput, standardError) {
            var pending = root.pendingNotifications[command]
            delete root.pendingNotifications[command]
            if (!pending) return
            var event = pending.event
            if (event.kind !== "agent"
                    && pending.backendGeneration !== root.backendGeneration) return
            if (event.kind === "usage") {
                root.usageNotificationState = Notifications.delivery(
                    root.usageNotificationState, event, exitCode === 0)
            }
            if (event.kind === "ompMode") {
                root.ompModeNotificationState = Notifications.ompModeDelivery(
                    root.ompModeNotificationState, event, exitCode === 0)
            }
            if (exitCode !== 0) {
                root.notificationError = "Desktop notification failed: "
                    + (standardError.trim() || "notify-send exited with " + exitCode)
                console.warn(root.notificationError)
            }
        }
    }

    Process.CommandRunner {
        id: runner

        onFinished: function(command, exitCode, standardOutput, standardError) {
            if (root.fetchGeneration !== root.backendGeneration) {
                root.loading = false
                root.fetchKind = ""
                Qt.callLater(root.refresh)
                return
            }
            var kind = root.fetchKind
            var stdout = standardOutput.trim()
            var stderr = standardError.trim()
            try {
                if (!stdout) throw new Error(stderr || "fetcher produced no output")
                var parsed = JSON.parse(stdout)
                if (!parsed || !Array.isArray(parsed.providers)
                        || parsed.providers.some(function(record) {
                            return !record || typeof record.id !== "string"
                                || typeof record.ok !== "boolean"
                        })) {
                    throw new Error("fetcher returned an invalid provider snapshot")
                }
                // Settings may have changed while the helper was running.
                parsed.providers = parsed.providers.filter(function(record) {
                    return root.enabledProviders.indexOf(record.id) >= 0
                })
                var error = parsed.fatal ? parsed.fatal.message || "fetch failed"
                    : exitCode !== 0 ? stderr || "fetch failed (exit " + exitCode + ")" : ""
                var fresh = kind === "fresh" && !error && parsed.cacheOnly !== true
                if (!fresh || root.enabledProviders.indexOf("codex") < 0) parsed.codexRotation = null
                // The helper owns credential and cache-age validation, including empty results.
                root.snapshot = parsed
                if (kind !== "recovery-cache") root.lastError = error
                var result = Notifications.usage(root.usageNotificationState,
                    parsed.providers, Date.now(), fresh && root.usageNotificationsEnabled)
                root.usageNotificationState = result.state
                root.dispatchNotifications(result.events)
                var modeResult = Notifications.ompMode(root.ompModeNotificationState,
                    parsed.codexRotation, fresh && root.ompModeNotificationsEnabled)
                root.ompModeNotificationState = modeResult.state
                root.dispatchNotifications(modeResult.events)
            } catch (err) {
                if (kind !== "recovery-cache") root.lastError = err.message
                root.snapshot = {
                    updatedAt: "", providers: [], fatal: null, forecast: null,
                    codexRotation: null, cliVersion: null
                }
                root.ompModeNotificationState = Notifications.ompMode(
                    root.ompModeNotificationState, null, false).state
                if (kind === "fresh" && root.enabledProviders.length > 0) {
                    // A transport failure cannot prove that in-memory credentials are still current.
                    root.fetchProviders("recovery-cache")
                    return
                }
                root.usageNotificationState = {}
            }
            root.loading = false
            root.fetchKind = ""
            if (kind === "startup-cache") {
                // Commit cached readings before starting any network command.
                Qt.callLater(root.refresh)
            }
        }
    }

    // Read the aggregate only after its writer exits successfully.
    Process.CommandRunner {
        id: aggregatorRunner

        onFinished: function(command, exitCode, standardOutput, standardError) {
            root.aggregatorLoading = false
            if (!root.agentsEnabled) return
            if (exitCode !== 0) {
                root.agentsError = standardError.trim()
                    || "agent scan failed (exit " + exitCode + ")"
                return
            }
            root.refreshAgents()
        }
    }
    readonly property string aggregatorScriptPath: Command.localPath(
        Qt.resolvedUrl("../scripts/codexbar_agents.py"))

    // History-record launches. Keys are agentKey() JSON; values are the
    // launch start time. A launched record leaves History once the
    // aggregator sees it live; the cooldown only re-enables a launch whose
    // agent never came up.
    readonly property string focusScriptPath: Command.localPath(
        Qt.resolvedUrl("../scripts/codexbar_focus.py"))
    readonly property int historyLaunchCooldownMs: 60000
    property var historyLaunches: ({})
    property string historyLaunchError: ""
    property var _launchCommands: ({})

    // Hosts codexbar_focus.py --launch reopens; mirrors LAUNCH_HOSTS.
    function launchHost(host) {
        return host === "kitty" || host === "tern"
    }

    function historyLaunchAllowed(key) {
        var started = root.historyLaunches[key]
        return !started || root.nowMs - started > root.historyLaunchCooldownMs
    }

    function launchHistory(record) {
        if (!record || !record.provider || !record.sessionId) return
        var key = JSON.stringify([record.provider, record.sessionId])
        if (!root.historyLaunchAllowed(key)) return
        var launches = Object.assign({}, root.historyLaunches)
        launches[key] = Date.now()
        root.historyLaunches = launches
        root.historyLaunchError = ""
        var cmd = "python3 " + Command.shellQuote(root.focusScriptPath)
            + " --launch " + Command.shellQuote(record.provider)
            + " " + Command.shellQuote(record.sessionId)
        root._launchCommands[cmd] = { keys: [key], batch: false }
        launchRunner.run(cmd)
    }

    // Restores the given launchable restart rows. The helper runs exactly
    // those, in desktop order, one at a time, so switches never interleave.
    function launchAllHistory(records) {
        var keys = []
        var pairs = []
        for (var i = 0; i < records.length; i++) {
            var r = records[i]
            if (!r || r.closedBy !== "reboot" || !root.launchHost(r.host) || !r.resumeCommand) continue
            var key = JSON.stringify([r.provider, r.sessionId])
            if (!root.historyLaunchAllowed(key)) continue
            keys.push(key)
            pairs.push([r.provider, r.sessionId])
        }
        if (keys.length === 0) return
        var launches = Object.assign({}, root.historyLaunches)
        for (var j = 0; j < keys.length; j++) launches[keys[j]] = Date.now()
        root.historyLaunches = launches
        root.historyLaunchError = ""
        var cmd = "python3 " + Command.shellQuote(root.focusScriptPath) + " --launch-all "
            + Command.shellQuote(encodeURIComponent(JSON.stringify(pairs)))
        root._launchCommands[cmd] = { keys: keys, batch: true }
        launchRunner.run(cmd)
    }

    Process.CommandRunner {
        id: launchRunner
        onFinished: function(command, exitCode, standardOutput, standardError) {
            var pending = root._launchCommands[command] || { keys: [], batch: false }
            delete root._launchCommands[command]
            // A batch reports which sessions started; the rest lose their
            // cooldown so they can be retried at once.
            var started = {}
            if (pending.batch) {
                try {
                    var done = JSON.parse(standardOutput.trim() || "[]")
                    for (var d = 0; d < done.length; d++) started[JSON.stringify(done[d])] = true
                } catch (err) {}
            } else if (exitCode === 0) {
                for (var s = 0; s < pending.keys.length; s++) started[pending.keys[s]] = true
            }
            var launches = Object.assign({}, root.historyLaunches)
            for (var i = 0; i < pending.keys.length; i++) {
                if (!started[pending.keys[i]]) delete launches[pending.keys[i]]
            }
            root.historyLaunches = launches
            root.historyLaunchError = standardError.trim()
                || (exitCode === 0 ? "" : "launch failed (exit " + exitCode + ")")
            if (Object.keys(started).length > 0) root.runAggregator()
        }
    }

    // Live kitty sessions moved to a new Tern tab. Keys are agentKey() JSON
    // of moves in flight; the row's button stays disabled until it ends.
    property var teleports: ({})
    property string teleportError: ""
    property var _teleportCommands: ({})

    function canTeleport(record) {
        return !!record && record.host === "kitty" && !!record.resumeCommand
            && root.agentSnapshot.ternInstalled === true
    }

    function teleportAgent(record) {
        if (!root.canTeleport(record)) return
        if (record.state !== "idle") {
            root.teleportError = "Teleport waits until the session is idle."
            return
        }
        var key = JSON.stringify([record.provider, record.sessionId])
        if (root.teleports[key]) return
        var moving = Object.assign({}, root.teleports)
        moving[key] = true
        root.teleports = moving
        root.teleportError = ""
        var cmd = "python3 " + Command.shellQuote(root.focusScriptPath)
            + " --teleport " + Command.shellQuote(record.provider)
            + " " + Command.shellQuote(record.sessionId)
        root._teleportCommands[cmd] = key
        teleportRunner.run(cmd)
    }

    Process.CommandRunner {
        id: teleportRunner
        onFinished: function(command, exitCode, standardOutput, standardError) {
            var moving = Object.assign({}, root.teleports)
            delete moving[root._teleportCommands[command]]
            delete root._teleportCommands[command]
            root.teleports = moving
            root.teleportError = exitCode === 0 ? ""
                : (standardError.trim() || "teleport failed (exit " + exitCode + ")")
            root.runAggregator()
        }
    }

    // Removes ended sessions from History. The aggregator owns agents.json,
    // so it performs the write under its lock; the rows hide immediately.
    function dismissHistory(records) {
        var pairs = []
        var hide = {}
        for (var i = 0; i < records.length; i++) {
            var r = records[i]
            if (!r || !r.provider || !r.sessionId) continue
            pairs.push([r.provider, r.sessionId])
            hide[JSON.stringify([r.provider, r.sessionId])] = true
        }
        if (pairs.length === 0) return
        var snap = Object.assign({}, root.agentSnapshot)
        snap.history = (snap.history || []).filter(function(r) {
            return !hide[JSON.stringify([r.provider, r.sessionId])]
        })
        root.agentSnapshot = snap
        dismissRunner.run("python3 " + Command.shellQuote(root.aggregatorScriptPath)
            + " --dismiss " + Command.shellQuote(encodeURIComponent(JSON.stringify(pairs))))
    }

    Process.CommandRunner {
        id: dismissRunner
        onFinished: function(command, exitCode, standardOutput, standardError) {
            if (exitCode !== 0) {
                root.historyLaunchError = standardError.trim()
                    || "dismiss failed (exit " + exitCode + ")"
            }
            root.runAggregator()
        }
    }

    // Plasma's task model supplies the live window PID and desktop roles.
    // Keep it at the root so desktop data remains available while another
    // popup tab is active and can be saved on every aggregator request.
    TaskManager.VirtualDesktopInfo { id: vdInfo }

    property var taskWindows: []
    // The last focused task window. Opening the popup focuses plasmashell,
    // which is not a task, so this keeps the window the user came from.
    property var activeTaskWindow: null
    readonly property string focusedAgentKey:
        root.focusedAgentKeyFor(root.activeTaskWindow, root.agentSnapshot)

    Repeater {
        model: TaskManager.TasksModel {
            id: tasksModel
            groupMode: TaskManager.TasksModel.GroupDisabled
        }
        delegate: Item {
            id: taskWin
            visible: false
            readonly property int pid: model.AppPid !== undefined ? model.AppPid : 0
            readonly property var desktops: {
                var outer = root._roleList(model.VirtualDesktops)
                var flat = []
                for (var k = 0; k < outer.length; k++) {
                    var inner = root._roleList(outer[k])
                    for (var m = 0; m < inner.length; m++) flat.push(inner[m])
                }
                return flat
            }
            readonly property bool all: model.IsOnAllVirtualDesktops === true
            readonly property bool active: model.IsActive === true
            onActiveChanged: if (active) root.activeTaskWindow = taskWin
            readonly property string caption: model.display !== undefined
                ? String(model.display) : ""
            readonly property var info: ({
                pid: taskWin.pid,
                desktops: taskWin.desktops,
                all: taskWin.all,
                caption: taskWin.caption
            })
            Component.onCompleted: {
                root.taskWindows = root.taskWindows.concat([taskWin])
                if (active) root.activeTaskWindow = taskWin
            }
            Component.onDestruction: {
                root.taskWindows = root.taskWindows.filter(function(w) { return w !== taskWin })
                if (root.activeTaskWindow === taskWin) root.activeTaskWindow = null
            }
        }
    }

    function _roleList(v) {
        if (v === undefined || v === null) return []
        if (Array.isArray(v)) return v
        if (typeof v === "object" && typeof v.length === "number") {
            var out = []
            for (var i = 0; i < v.length; i++) out.push(v[i])
            return out
        }
        return [v]
    }

    function _captionHint(record) {
        var parts = ((record && record.cwd) || "").split("/")
        var base = ""
        for (var i = parts.length - 1; i >= 0; i--) {
            if (parts[i]) { base = parts[i]; break }
        }
        var out = ""
        for (var j = 0; j < base.length; j++) {
            var c = base[j]
            var keep = (c >= "a" && c <= "z") || (c >= "A" && c <= "Z")
                || (c >= "0" && c <= "9") || c === "-" || c === "_"
                || c === "." || c === " "
            if (keep) out += c
        }
        return out.toLowerCase()
    }

    function _desktopIndexOf(id) {
        if (id === undefined || id === null) return -1
        var i = (vdInfo.desktopIds || []).indexOf(id)
        if (i >= 0) return i
        return (typeof id === "number" && id >= 1) ? id - 1 : -1
    }

    function desktopInfoFor(record) {
        var chain = (record && record.ancestorPids) || []
        if (chain.length === 0) return null
        var byPid = {}
        for (var i = 0; i < chain.length; i++) byPid[chain[i]] = true
        var hint = _captionHint(record)
        var fallback = null
        var onAll = null
        for (var j = 0; j < taskWindows.length; j++) {
            var w = taskWindows[j].info
            if (!w || !byPid[w.pid]) continue
            if (w.all) {
                if (!onAll) onAll = w
            } else if (!fallback) {
                fallback = w
            }
            if (hint && w.caption && !w.all
                    && w.caption.toLowerCase().indexOf(hint) >= 0) {
                fallback = w
                break
            }
        }
        var win = fallback || onAll
        if (!win) return null
        if (win.all) return { label: "all", onCurrent: true }
        var cur = _desktopIndexOf(vdInfo.currentDesktop)
        var idxs = []
        for (var d = 0; d < win.desktops.length; d++) {
            var di = _desktopIndexOf(win.desktops[d])
            if (di >= 0) idxs.push(di)
        }
        if (idxs.length === 0) return null
        return {
            label: String(idxs[0] + 1),
            onCurrent: idxs.indexOf(cur) >= 0
        }
    }

    // Whether a saved desktop label ("2", "all") names the current desktop.
    function isCurrentDesktop(label) {
        if (label === "all") return true
        return String(label || "") === String(_desktopIndexOf(vdInfo.currentDesktop) + 1)
    }

    // The live session shown in the focused window, as an AgentsSection
    // agentKey, or "" when the window shows no session or several could
    // match. In a Tern window only a visible pane of the session the
    // caption names counts (`ternSession`); elsewhere a window caption that
    // holds a session title (kitty tabs) or the folder name (VS Code
    // windows sharing one process) picks among the candidates.
    function focusedAgentKeyFor(win, snapshot) {
        var w = win ? win.info : null
        if (!w || !w.pid) return ""
        var candidates = ((snapshot && snapshot.agents) || []).filter(function(a) {
            return a && (a.ancestorPids || []).indexOf(w.pid) >= 0
        })
        var caption = (w.caption || "").toLowerCase()
        if (candidates.some(function(a) { return a.host === "tern" })) {
            candidates = candidates.filter(function(a) {
                return !!a.ternSession && a.ternSession.toLowerCase() === caption
            })
        }
        var sharedPid = taskWindows.filter(function(t) {
            return t.info && t.info.pid === w.pid
        }).length > 1
        if (candidates.length > 1 || sharedPid) {
            var titled = candidates.filter(function(a) {
                return a.windowTitle && caption.indexOf(a.windowTitle.toLowerCase()) >= 0
            })
            if (titled.length === 1) {
                candidates = titled
            } else {
                candidates = (titled.length > 1 ? titled : candidates).filter(function(a) {
                    var hint = root._captionHint(a)
                    return hint && caption.indexOf(hint) >= 0
                })
            }
        }
        if (candidates.length !== 1) return ""
        return JSON.stringify([candidates[0].provider, candidates[0].sessionId])
    }

    function desktopSnapshot() {
        var out = []
        var records = (root.agentSnapshot && root.agentSnapshot.agents) || []
        for (var i = 0; i < records.length; i++) {
            var record = records[i]
            if (!record || !record.provider || !record.sessionId) continue
            var desktop = root.desktopInfoFor(record)
            if (!desktop) continue
            out.push({
                provider: record.provider,
                sessionId: record.sessionId,
                desktop: desktop.label
            })
        }
        return out
    }
    function runAggregator() {
        if (!root.agentsEnabled || root.aggregatorLoading || root.agentsLoading) return
        root.aggregatorLoading = true
        var requestedAt = Date.now()
        var desktopMap = encodeURIComponent(JSON.stringify(root.desktopSnapshot()))
        var cmd = "python3 " + Command.shellQuote(root.aggregatorScriptPath) + " --once"
            + " --desktop-map " + Command.shellQuote(desktopMap)
            + " --requested-at " + requestedAt
            + " --history-limit " + Plasmoid.configuration.agentHistoryLimit
        aggregatorRunner.run(cmd)
    }

    function refreshAgents() {
        if (!root.agentsEnabled) return
        if (root.agentsLoading || root.aggregatorLoading) return
        root.agentsLoading = true
        // Requires QML_XHR_ALLOW_FILE_READ=1 in plasmashell's env — installed
        // by install_integration.py into ~/.config/plasma-workspace/env/.
        var xhr = new XMLHttpRequest()
        root.agentsRequest = xhr
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE) return
            if (root.agentsRequest !== xhr) return
            root.agentsRequest = null
            root.agentsLoading = false
            if (!root.agentsEnabled) return
            if (xhr.status !== 0 && xhr.status !== 200) {
                root.agentsError = "file read failed (status " + xhr.status + ")"
                return
            }
            var text = (xhr.responseText || "").trim()
            if (text === "") {
                root.agentsError = ""
                root.clearAgentSnapshot()
                return
            }
            try {
                var parsed = JSON.parse(text)
                if (!Array.isArray(parsed.history)) parsed.history = []
                var transition = Notifications.agents(root.agentNotificationState,
                    Array.isArray(parsed.agents) ? parsed.agents : [], root.agentNotificationsEnabled)
                root.agentNotificationState = transition.state
                root.dispatchNotifications(transition.events)
                parsed.agents = Array.isArray(parsed.agents) ? parsed.agents : []
                parsed.agents = parsed.agents.filter(function(a) {
                    return a && (root.includeUntrackedAgents || a.state !== "untracked")
                })
                if (!root.includeUntrackedAgents) {
                    parsed.history = parsed.history.filter(function(r) {
                        if (!r) return false
                        var sid = String(r.sessionId || "")
                        var prefix = "untracked-" + String(r.provider || "") + "-"
                        var suffix = sid.slice(prefix.length)
                        return sid.indexOf(prefix) !== 0 || !/^\d+$/.test(suffix)
                    })
                }
                parsed.counts = { working: 0, blocked: 0, idle: 0, untracked: 0,
                    total: parsed.agents.length }
                for (var i = 0; i < parsed.agents.length; i++) {
                    var state = parsed.agents[i].state
                    if (state === "working" || state === "blocked"
                            || state === "idle" || state === "untracked") {
                        parsed.counts[state]++
                    }
                }
                root.agentSnapshot = parsed
                root.agentsError = ""
            } catch (err) {
                root.agentsError = "parse error: " + err.message
            }
        }
        xhr.open("GET", root.agentsFileUrl)
        xhr.send()
    }

    function clearAgentSnapshot() {
        root.agentSnapshot = {
            updatedAt: "",
            counts: { working: 0, blocked: 0, idle: 0, untracked: 0, total: 0 },
            agents: [],
            history: []
        }
    }

    function refresh() {
        if (root.cacheRestoring || root.loading) return
        if (root.enabledProviders.length === 0) {
            root.snapshot = {
                updatedAt: new Date().toISOString(),
                providers: [],
                fatal: null,
                forecast: null,
                codexRotation: null,
                cliVersion: null
            }
            root.usageNotificationState = {}
            root.ompModeNotificationState = Notifications.ompMode(
                root.ompModeNotificationState, null, false).state
            return
        }
        root.fetchProviders("fresh")
    }

    function fetchProviders(kind) {
        root.loading = true
        root.fetchKind = kind
        root.fetchGeneration = root.backendGeneration
        var cmd = "python3 " + Command.shellQuote(root.scriptPath)
            + " --cli-path " + Command.shellQuote(root.cliPath)
            + " --providers " + Command.shellQuote(root.enabledProviders.join(","))
        if (kind !== "fresh") {
            cmd += " --cache-only"
        } else if (root.codexForecastEnabled) {
            cmd += " --forecast-url https://codex-reset.com/api/forecast"
        }
        runner.run(cmd)
    }

    Timer {
        id: poll
        interval: root.refreshMs
        running: true; repeat: true
        onTriggered: root.refresh()
    }
    onRefreshMsChanged: { poll.interval = root.refreshMs; poll.restart() }

    // One scan timer; completion triggers the file read.
    Timer {
        id: agentsAggregator
        interval: root.agentsRefreshMs
        running: root.agentsEnabled
        repeat: true
        triggeredOnStart: true
        onTriggered: root.runAggregator()
    }
    onAgentsEnabledChanged: {
        if (!root.agentsEnabled) {
            var xhr = root.agentsRequest
            root.agentsRequest = null
            root.agentsLoading = false
            if (xhr) xhr.abort()
            root.clearAgentSnapshot()
            root.agentsError = ""
        }
    }

    Component.onCompleted: {
        // String → QKeySequence conversion only works in a JS assignment
        // (Plasma 6 / Qt 6 quirk), not a property binding. Super+A toggles
        // the popup via Plasma's default `activated` handler.
        Plasmoid.globalShortcut = "Meta+A"
        root.fetchProviders("startup-cache")
    }

    // Bar/ring color. With a settled pace (elapsed share of the window, see
    // pacePercent) usage at or under pace is green; any usage above it leaves
    // green immediately and walks yellow → orange → red across the headroom
    // between the pace mark and 100%. Without a pace (no reset data, or the
    // first 3% of a window) fall back to fixed usage thresholds.
    function colorFor(pct, pacePct) {
        var p = Math.max(0, Math.min(100, pct || 0))
        if (pacePct !== undefined && pacePct >= 0) {
            if (p <= pacePct) return "#22c55e"
            var t = (p - pacePct) / (100 - pacePct)
            if (t >= 2 / 3) return "#ef4444"
            if (t >= 1 / 3) return "#f97316"
            return "#eab308"
        }
        if (p >= 90) return "#ef4444"
        if (p >= 70) return "#f97316"
        if (p >= 50) return "#eab308"
        return "#22c55e"
    }

    function relativeMs(ms) {
        if (ms <= 0) return "soon"
        var mins = Math.floor(ms / 60000)
        var hrs = Math.floor(mins / 60)
        if (hrs >= 24) {
            var days = Math.floor(hrs / 24)
            return "in " + days + "d " + (hrs % 24) + "h"
        }
        if (hrs > 0) return "in " + hrs + "h " + (mins % 60) + "m"
        return "in " + mins + "m"
    }

    function _monthDay(when) {
        var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        return months[when.getMonth()] + " " + when.getDate()
    }

    function _absoluteTime(when, nowMs) {
        var now = new Date(nowMs)
        var sameDay = now.toDateString() === when.toDateString()
        var hhmm = Qt.formatDateTime(when, "HH:mm")

        if (sameDay) return hhmm
        return _monthDay(when) + ", " + hhmm
    }

    function _forecastTimeLeft(when, now) {
        var diff = when.getTime() - now
        if (diff <= 0) return "soon"
        if (diff < 60 * 60 * 1000) {
            return Math.ceil(diff / (60 * 1000)) + "m"
        }
        if (diff < 24 * 60 * 60 * 1000) {
            return Math.ceil(diff / (60 * 60 * 1000)) + "h"
        }
        return relativeMs(diff).replace(/^in /, "").replace(/ 0h$/, "")
    }

    // Below this 48h chance the cadence-based ETA is noise, so the line says
    // "unknown" instead of inventing a timestamp.
    readonly property int forecastLikelyPercent: 50

    function _forecastPercent(value) {
        if (value === undefined || value === null) return NaN
        var p = Number(value)
        return isFinite(p) && p >= 0 && p <= 100 ? Math.round(p) : NaN
    }

    // Which branch the forecast card is in: "announced" (Tibo committed to a
    // window), "likely" (cadence model ≥ forecastLikelyPercent within 48h),
    // "unknown" (anything else), "" when there is no usable forecast.
    function codexForecastState(forecast) {
        if (!forecast || typeof forecast !== "object" || forecast.ok !== true) return ""
        if (forecast.signal && typeof forecast.signal === "object") return "announced"
        var p48 = _forecastPercent(forecast.prob48h)
        var expected = typeof forecast.expectedAt === "string"
            ? new Date(forecast.expectedAt) : null
        if (!isNaN(p48) && p48 >= root.forecastLikelyPercent
                && expected && !isNaN(expected.getTime())) return "likely"
        return "unknown"
    }

    // Detail text next to the state badge; the badge itself names the state.
    function formatCodexForecast(forecast, now) {
        var state = codexForecastState(forecast)
        if (state.length === 0) return ""

        var details = []
        if (state === "announced") {
            // The deadline in local time, its countdown and the site's signal
            // score. The site's own window label ("end of Friday") is in its
            // zone, so it is not shown. Model confidence is about the cadence
            // model, so it is left off here.
            var signal = forecast.signal
            var head = "reset"
            var deadline = typeof signal.deadlineAt === "string"
                ? new Date(signal.deadlineAt) : null
            if (deadline && !isNaN(deadline.getTime())) {
                head = "by " + _absoluteTime(deadline, now)
                    + " (" + _forecastTimeLeft(deadline, now) + ")"
            }
            var sp = _forecastPercent(signal.percent)
            if (!isNaN(sp)) head += " · " + sp + "% chance"
            details.push(head)
        } else if (state === "likely") {
            // The site's cadence model ignores announcements, so its 48h number
            // is only printed here, where it is the whole basis of the estimate.
            var expected = new Date(forecast.expectedAt)
            details.push("~ " + _absoluteTime(expected, now)
                + " (" + _forecastTimeLeft(expected, now) + ") · "
                + _forecastPercent(forecast.prob48h) + "% chance within 48h")
        } else {
            // No estimate worth printing; the elapsed wait against recent gaps
            // is the one time-dependent signal the site publishes.
            var line = "next reset unknown"
            var wait = forecast.wait && typeof forecast.wait === "object"
                ? forecast.wait : null
            var waited = wait ? Number(wait.days) : NaN
            if (isFinite(waited) && waited >= 0) {
                line += " · waited " + Math.round(waited) + "d"
                var share = Number(wait.shorterShare)
                if (isFinite(share) && share >= 0 && share <= 1) {
                    line += ", longer than " + Math.round(share * 100) + "% of recent gaps"
                }
            }
            details.push(line)
        }
        if (forecast.stale === true) details[0] += " (cached)"

        if (state !== "announced" && typeof forecast.confidence === "string"
                && forecast.confidence.trim().length > 0) {
            details.push(forecast.confidence.trim() + " confidence")
        }
        return details.join(" · ")
    }

    // Open Codex incident: compensation resets follow outages. Rendered in the
    // negative colour, so it stays separate from the announcement/hint line.
    function formatCodexForecastIncident(forecast) {
        if (!forecast || typeof forecast !== "object" || forecast.ok !== true) return ""
        var incident = forecast.incident
        if (!incident || typeof incident !== "object" || incident.open !== true) return ""
        var surfaces = Array.isArray(incident.surfaces) ? incident.surfaces : []
        return "Codex incident open"
            + (surfaces.length > 0 ? " (" + surfaces.join(", ") + ")" : "")
            + " — compensation reset possible"
    }

    // Why the next reset is coming: the alert that postdates the last recorded
    // reset, or, failing that, Tibo's latest soft hint.
    function formatCodexForecastAlert(forecast) {
        if (!forecast || typeof forecast !== "object" || forecast.ok !== true) return ""
        if (typeof forecast.alertSummary === "string" && forecast.alertSummary.trim().length > 0) {
            return forecast.alertSummary.trim()
        }
        if (forecast.hint && typeof forecast.hint === "object"
                && typeof forecast.hint.quote === "string") {
            var at = typeof forecast.hint.at === "string" ? new Date(forecast.hint.at) : null
            return "Hint" + (at && !isNaN(at.getTime()) ? " " + _monthDay(at) : "")
                + ": \u201c" + forecast.hint.quote.trim() + "\u201d"
        }
        return ""
    }

    // "2 saved resets · soonest expires in 16d 8h (2026-10-04)", or just
    // "2 saved resets" when brief. Empty when the account has no usable
    // reset credit.
    function formatResetCredits(credits, now, brief) {
        if (!credits || typeof credits !== "object") return ""
        var count = Number(credits.count)
        if (!isFinite(count) || count < 1) return ""
        var text = count + " saved reset" + (count === 1 ? "" : "s")
        if (brief) return text
        if (typeof credits.soonestExpiresAt === "string") {
            var when = new Date(credits.soonestExpiresAt)
            if (!isNaN(when.getTime())) {
                var left = when.getTime() - now
                text += " · soonest expires " + (left <= 0 ? "now"
                    : "in " + _forecastTimeLeft(when, now))
                    + " (" + Qt.formatDateTime(when, "yyyy-MM-dd") + ")"
            }
        }
        return text
    }

    // Returns "16:00 (2h 28m)" / "May 19, 21:56 (3d 8h)" / "" depending on data.
    function formatReset(rec, now) {
        if (!rec) return ""
        if (rec.resetsAt) {
            var when = new Date(rec.resetsAt)
            var diff = when.getTime() - now
            if (diff <= 0) return "soon"
            return _absoluteTime(when, now) + " (" + relativeMs(diff).replace(/^in /, "") + ")"
        }
        var desc = rec.resetDescription ? rec.resetDescription.toString().trim() : ""
        if (desc.length > 0) {
            desc = desc.replace(/^[Rr]esets\s+/, "")
            desc = desc.replace(/\s*\([^)]+\)\s*$/, "")
            return desc
        }
        return ""
    }

    // Length of a usage window in ms: the declared windowMinutes, or from
    // startsAt to resetsAt when an earlier weekly reset truncates it.
    function windowSpanMs(win) {
        return Usage.windowSpanMs(win)
    }

    // Elapsed share of a usage window (0–100), or -1 when the window has no
    // usable pace: no resetsAt/windowMinutes, a reset that is past or outside
    // the declared window, or less than 3% elapsed (pure noise). Same rule as
    // the pace tick in ProviderSection.
    function pacePercent(win, now) {
        return Usage.pacePercent(win, now)
    }

    // Projected usage at reset if the current rate holds, or -1 when the
    // window has no pace (see pacePercent).
    function projectedPercent(win, now) {
        return Usage.projectedPercent(win, now)
    }

    // Codex plan allowance relative to Plus. Pro carries 20× the Plus usage
    // limits; any other or missing plan counts as one Plus allowance.
    readonly property var _codexPlanWeight: ({ plus: 1, pro: 20 })

    // Pooled 5h/7d window stats across Codex accounts, recomputed per UI
    // clock tick. Each account contributes its plan weight in Plus-sized
    // allowances: the fill is the used share of the combined allowance and
    // the pace mark is the weighted mean elapsed share, i.e. where even
    // consumption on every account would sit now. An account whose window
    // already reset counts as 0% used, 0% elapsed. resetsAt is the soonest
    // upcoming reset, when allowance returns.
    function compositeStats(windows, windowMinutes, now) {
        var used = 0, elapsed = 0, total = 0, paced = true, soonest = NaN
        for (var i = 0; i < windows.length; i++) {
            var w = windows[i]
            var u = w.usedPercent
            var remainingMs = w.resetsAt ? new Date(w.resetsAt).getTime() - now : NaN
            var windowMs = w.startsAt ? windowSpanMs(w) : windowMinutes * 60000
            if (isNaN(remainingMs) || remainingMs > windowMs) {
                paced = false
            } else if (remainingMs <= 0) {
                u = 0
            } else {
                elapsed += w.weight * (1 - remainingMs / windowMs) * 100
                if (isNaN(soonest) || remainingMs < soonest) soonest = remainingMs
            }
            used += w.weight * u
            total += w.weight
        }
        return {
            usedPercent: used / total,
            resetsAt: isNaN(soonest) ? null : new Date(now + soonest).toISOString(),
            total: total,
            remaining: (total * 100 - used) / 100,
            pacePct: paced ? elapsed / total : -1
        }
    }

    // The Codex accounts' windows of one length with their plan weights, or
    // null when fewer than two report it. Clock-independent so the popup
    // keeps stable row delegates; ProviderSection applies compositeStats on
    // each tick.
    function _compositeWindow(records, windowMinutes) {
        var windows = []
        var used = 0, total = 0, consumed = 0, hours = 0
        for (var i = 0; i < records.length; i++) {
            var w = records[i].primary && records[i].primary.windowMinutes === windowMinutes
                ? records[i].primary
                : records[i].secondary && records[i].secondary.windowMinutes === windowMinutes
                    ? records[i].secondary : null
            if (!w || w.usedPercent === undefined || w.usedPercent === null) continue
            var weight = root._codexPlanWeight[String(records[i].loginMethod || "").toLowerCase()] || 1
            var u = Math.max(0, Math.min(100, w.usedPercent))
            windows.push({ usedPercent: u, resetsAt: w.resetsAt, startsAt: w.startsAt, weight: weight })
            used += weight * u
            total += weight
            // Pooled recent consumption in the same Plus-weighted units as
            // the fill; an account without history contributes nothing.
            if (w.recent) {
                consumed += weight * w.recent.consumedPercent
                hours = w.recent.hours
            }
        }
        if (windows.length < 2) return null
        return {
            usedPercent: used / total,
            windowMinutes: windowMinutes,
            compositeWindows: windows,
            recent: hours > 0 ? { hours: hours, consumedPercent: consumed / total } : null
        }
    }

    function codexCompositeRecord() {
        var show5h = Plasmoid.configuration.showComposite5h === true
        var show7d = Plasmoid.configuration.showComposite7d !== false
        if (!show5h && !show7d) return null
        var records = (root.snapshot.providers || []).filter(function(rec) {
            return rec && rec.id === "codex" && rec.ok
        })
        if (records.length < 2) return null
        if (records.some(function(record) { return record.stale === true })) return null
        var primary = show5h ? _compositeWindow(records, 300) : null
        var secondary = show7d ? _compositeWindow(records, 10080) : null
        if (!primary && !secondary) return null
        return {
            id: "codex",
            ok: true,
            composite: true,
            accountCount: records.length,
            primary: primary,
            secondary: secondary,
            extraRateWindows: [],
            error: null
        }
    }

    function windowLabel(providerId, slot, rec, extraTitle) {
        return Usage.windowLabel(providerId, slot, rec, extraTitle) || slot
    }

    function accountAvailabilityIndicator(rec) {
        var email = rec.accountEmail || ""
        if (rec.id !== "codex" || !email) return ""
        var rotation = root.snapshot.codexRotation
        var availability = rotation && rotation.accountAvailability
            ? rotation.accountAvailability[email] : ""
        if (availability === "open") return "🟢"
        if (availability === "blocked") return "🔒"
        return ""
    }

    function accountDisplayName(rec) {
        var indicator = accountAvailabilityIndicator(rec)
        return (indicator ? indicator + " " : "") + (rec.accountEmail || "")
    }

    function providerDisplayName(id) {
        var map = {
            codex: "OPENAI CODEX",
            claude: "CLAUDE CODE",
            zai: "Z.AI",
            opencodego: "OPENCODE GO",
            openrouter: "OPENROUTER",
            kilo: "KILO",
            typesafe: "TYPESAFE"
        }
        return map[id] || id.toUpperCase()
    }

    function agentStateColor(state) {
        if (state === "blocked") return "#ef4444"
        if (state === "working") return "#22c55e"
        if (state === "idle") return "#9ca3af"
        if (state === "untracked") return "#3b82f6"
        return "#9ca3af"
    }

    // Vendor tints shared by every tab: Anthropic orange, OpenAI violet
    // (orange/violet stays apart under red-green color blindness). A
    // model is tinted by its vendor, not its harness; others are neutral.
    function modelTint(model) {
        var m = String(model || "").trim().toLowerCase()
        m = m.slice(m.lastIndexOf("/") + 1)
        if (/^(claude|opus|sonnet|haiku)/.test(m)) return "#e08a5f"
        if (/^(gpt|codex|o\d)/.test(m)) return "#a78bfa"
        return Kirigami.Theme.textColor
    }

    // Pick the most "interesting" agent to feature in the panel:
    //   blocked  > working  > idle, then most-recently-changed first
    // Returns null when there's nothing worth surfacing.
    function featuredAgent() {
        var list = (root.agentSnapshot && root.agentSnapshot.agents) || []
        if (list.length === 0) return null
        var rank = { blocked: 0, working: 1, idle: 2 }
        var best = null
        for (var i = 0; i < list.length; i++) {
            var a = list[i]
            if (!a) continue
            var r = rank[a.state]
            if (r === undefined) continue
            if (!best) { best = a; continue }
            var bestR = rank[best.state]
            if (r < bestR) { best = a; continue }
            if (r === bestR &&
                (a.stateChangedAt || 0) > (best.stateChangedAt || 0)) {
                best = a
            }
        }
        // Don't feature an idle session unless nothing else is going on —
        // showing "idle: finished a thing" in the panel is noise.
        if (best && best.state === "idle") return null
        return best
    }

    // basename of a cwd, or "" if path is empty. Used to label sessions
    // compactly — "/home/me/code/foo" → "foo".
    function cwdLabel(path) {
        if (!path) return ""
        var p = path.toString()
        if (p.indexOf("/") < 0) return p
        var parts = p.split("/")
        for (var i = parts.length - 1; i >= 0; i--) {
            if (parts[i].length > 0) return parts[i]
        }
        return p
    }

    // Compact age string from a unix-ms timestamp, largest unit only:
    // "15s", "7m", "23h", "4d".
    function ageFrom(ms, now) {
        if (!ms) return ""
        var secs = Math.floor(Math.max(0, now - ms) / 1000)
        if (secs < 60) return secs + "s"
        var mins = Math.floor(secs / 60)
        if (mins < 60) return mins + "m"
        var hrs = Math.floor(mins / 60)
        if (hrs < 24) return hrs + "h"
        return Math.floor(hrs / 24) + "d"
    }
}
