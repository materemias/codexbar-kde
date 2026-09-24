import QtQuick
import QtQuick.Layouts
import QtQuick.Window
import QtCore
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import "process" as Process
import org.kde.taskmanager as TaskManager
import "Command.js" as Command

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
        recovery: []
    })
    property bool loading: false
    property bool agentsLoading: false
    property bool aggregatorLoading: false
    property var agentsRequest: null
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
    readonly property bool includeUntrackedAgents:
        Plasmoid.configuration.includeUntrackedAgents !== false
    onIncludeUntrackedAgentsChanged: root.refreshAgents()
    readonly property string cliPath: Command.cliPath(Plasmoid.configuration.cliPath)
    readonly property bool codexForecastEnabled:
        Plasmoid.configuration.enableCodex !== false
        && Plasmoid.configuration.showCodexResetForecast !== false
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
        if (root.lastError) return root.lastError
        var arr = root.snapshot.providers || []
        if (arr.length === 0) return root.loading ? "Loading…" : "No providers enabled"
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
                var account = String(rec.accountEmail)
                    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
                name += " · " + account
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
                var resetText = _resetTimeLeft(w, root.nowMs)
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

    Process.CommandRunner {
        id: runner

        onFinished: function(command, exitCode, standardOutput, standardError) {
            root.loading = false
            var stdout = standardOutput.trim()
            var stderr = standardError.trim()
            if (stdout === "") {
                root.lastError = stderr || "fetcher produced no output"
                return
            }
            try {
                var parsed = JSON.parse(stdout)
                root.snapshot = parsed
                root.lastError = parsed.fatal ? parsed.fatal.message : ""
            } catch (err) {
                root.lastError = "parse error: " + err.message
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

    // Plasma's task model supplies the live window PID and desktop roles.
    // Keep it at the root so desktop data remains available while another
    // popup tab is active and can be saved on every aggregator request.
    TaskManager.VirtualDesktopInfo { id: vdInfo }

    property var taskWindows: []

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
            readonly property string caption: model.display !== undefined
                ? String(model.display) : ""
            readonly property var info: ({
                pid: taskWin.pid,
                desktops: taskWin.desktops,
                all: taskWin.all,
                caption: taskWin.caption
            })
            Component.onCompleted: root.taskWindows = root.taskWindows.concat([taskWin])
            Component.onDestruction: root.taskWindows =
                root.taskWindows.filter(function(w) { return w !== taskWin })
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
                if (!Array.isArray(parsed.recovery)) parsed.recovery = []
                parsed.agents = Array.isArray(parsed.agents) ? parsed.agents : []
                parsed.agents = parsed.agents.filter(function(a) {
                    return a && (root.includeUntrackedAgents || a.state !== "untracked")
                })
                if (!root.includeUntrackedAgents) {
                    parsed.recovery = parsed.recovery.filter(function(r) {
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
            recovery: []
        }
    }

    function refresh() {
        if (root.enabledProviders.length === 0) {
            root.snapshot = {
                updatedAt: new Date().toISOString(),
                providers: [],
                fatal: null,
                forecast: null,
                codexRotation: null,
                cliVersion: null
            }
            return
        }
        if (root.loading) return
        root.loading = true
        var cmd = "python3 " + Command.shellQuote(root.scriptPath)
            + " --cli-path " + Command.shellQuote(root.cliPath)
            + " --providers " + root.enabledProviders.join(",")
        if (root.codexForecastEnabled) {
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
        root.refresh()
    }

    function colorFor(pct) {
        var p = Math.max(0, Math.min(100, pct || 0))
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

    function pad2(n) { return (n < 10 ? "0" : "") + n }
    function _time24(when) {
        return pad2(when.getHours()) + ":" + pad2(when.getMinutes())
    }

    function _monthDay(when) {
        var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        return months[when.getMonth()] + " " + when.getDate()
    }

    function _absoluteTime(when, nowMs) {
        var now = new Date(nowMs)
        var sameDay = now.toDateString() === when.toDateString()
        var hhmm = _time24(when)

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
            // Promised window, its deadline and the site's signal score. Model
            // confidence is about the cadence model, so it is left off here.
            var signal = forecast.signal
            var head = signal.windowLabel || "reset"
            var deadline = typeof signal.deadlineAt === "string"
                ? new Date(signal.deadlineAt) : null
            if (deadline && !isNaN(deadline.getTime())) {
                head += " (by " + _absoluteTime(deadline, now)
                    + ", " + _forecastTimeLeft(deadline, now) + ")"
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

    function firstCodexIndex() {
        var records = root.snapshot && Array.isArray(root.snapshot.providers)
            ? root.snapshot.providers : []
        for (var i = 0; i < records.length; i++) {
            if (records[i] && records[i].id === "codex") return i
        }
        return -1
    }

    function lastCodexIndex() {
        var records = root.snapshot && Array.isArray(root.snapshot.providers)
            ? root.snapshot.providers : []
        for (var i = records.length - 1; i >= 0; i--) {
            if (records[i] && records[i].id === "codex") return i
        }
        return -1
    }

    function _isoDate(when) {
        return when.getFullYear() + "-" + pad2(when.getMonth() + 1) + "-" + pad2(when.getDate())
    }

    // "2 saved resets · soonest expires in 16d 8h (2026-10-04)". Empty when
    // the account has no usable reset credit.
    function formatResetCredits(credits, now) {
        if (!credits || typeof credits !== "object") return ""
        var count = Number(credits.count)
        if (!isFinite(count) || count < 1) return ""
        var text = count + " saved reset" + (count === 1 ? "" : "s")
        if (typeof credits.soonestExpiresAt === "string") {
            var when = new Date(credits.soonestExpiresAt)
            if (!isNaN(when.getTime())) {
                var left = when.getTime() - now
                text += " · soonest expires " + (left <= 0 ? "now"
                    : "in " + _forecastTimeLeft(when, now))
                    + " (" + _isoDate(when) + ")"
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

    // Projected usage at reset if the current rate holds, or -1 when the
    // window cannot be projected: no resetsAt/windowMinutes, a reset that is
    // past or outside the declared window, or less than 3% elapsed (pure
    // noise). Same rule as the pace tick in ProviderSection.
    function projectedPercent(win, now) {
        if (!win || !win.resetsAt || !win.windowMinutes) return -1
        var windowMs = win.windowMinutes * 60000
        var remainingMs = new Date(win.resetsAt).getTime() - now
        if (isNaN(remainingMs) || remainingMs <= 0 || remainingMs > windowMs) return -1
        var pacePct = (1 - remainingMs / windowMs) * 100
        if (pacePct < 3) return -1
        return Math.min(999, (win.usedPercent || 0) * 100 / pacePct)
    }

    function windowLabel(providerId, slot, rec, extraTitle) {
        // Codex exposes this separate weekly GPT quota as "gpt-reserve".
        if (providerId === "codex"
                && (slot === "codex-base-model-inference"
                    || slot === "gpt-reserve"
                    || extraTitle === "gpt-reserve")) {
            return "Reserve 7d"
        }
        if (extraTitle && extraTitle.length > 0) return extraTitle
        if (slot === "claude-design") return "Design"
        if (slot === "claude-routines") return "Routines"
        if (providerId === "claude" && slot === "tertiary") return "Sonnet"
        if (providerId === "openrouter") return "Limit"
        if (providerId === "kilo") return "Credits"
        if (providerId === "zai" && slot === "secondary") return "Monthly"
        var mins = rec && rec.windowMinutes ? rec.windowMinutes : 0
        if (mins === 300) return "5h"
        if (mins === 1440) return "1d"
        if (mins === 10080) return "7d"
        if (mins === 43200) return "Monthly"
        return slot
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

    // Compact age string from a unix-ms timestamp. "2m", "15s", "1h 5m".
    function ageFrom(ms, now) {
        if (!ms) return ""
        var diff = Math.max(0, now - ms)
        var secs = Math.floor(diff / 1000)
        if (secs < 60) return secs + "s"
        var mins = Math.floor(secs / 60)
        if (mins < 60) return mins + "m"
        var hrs = Math.floor(mins / 60)
        if (hrs < 24) return hrs + "h " + (mins % 60) + "m"
        var days = Math.floor(hrs / 24)
        return days + "d " + (hrs % 24) + "h"
    }
}
