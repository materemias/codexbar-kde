import QtQuick
import QtQuick.Layouts
import QtQuick.Window
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.plasma5support as P5Support

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
    onExpandedChanged: if (!Plasmoid.expanded) root.requestedTab = ""

    Connections {
        target: Plasmoid
        function onActivated() { root.requestedTab = "agents" }
    }

    property var snapshot: ({
        updatedAt: "",
        highestProvider: null,
        highestPercent: 0,
        providers: [],
        fatal: null
    })
    property var agentSnapshot: ({
        updatedAt: "",
        counts: { working: 0, blocked: 0, idle: 0, untracked: 0, total: 0 },
        agents: []
    })
    property bool loading: false
    property bool agentsLoading: false
    property string lastError: ""
    property string agentsError: ""

    readonly property string scriptPath: {
        var url = Qt.resolvedUrl("../scripts/codexbar_fetch.py").toString()
        return url.replace(/^file:\/\//, "")
    }
    readonly property string agentsScriptPath: {
        var url = Qt.resolvedUrl("../scripts/codexbar_agents.py").toString()
        return url.replace(/^file:\/\//, "")
    }
    // Path to the aggregate file the widget reads via XHR. Refreshed every
    // poll tick by aggregatorRunner. $HOME isn't directly available in QML,
    // so derive it from this file's install location.
    readonly property string agentsFileUrl: {
        var here = Qt.resolvedUrl("./").toString()
        var m = here.match(/^(file:\/\/\/home\/[^\/]+)\//)
        var homeUrl = m ? m[1] : "file:///root"
        return homeUrl + "/.codexbar/agents.json"
    }
    readonly property int agentsRefreshMs: Math.max(2, Plasmoid.configuration.agentsRefreshSeconds || 5) * 1000
    readonly property bool agentsEnabled: Plasmoid.configuration.showAgents !== false
    readonly property string cliPath: Plasmoid.configuration.cliPath || "~/.local/bin/codexbar"
    readonly property int refreshMs: Math.max(10, Plasmoid.configuration.refreshSeconds || 30) * 1000
    readonly property var enabledProviders: {
        // Display order: Claude → Codex → z.ai → OpenRouter → Kilo
        // (preserved in tray rings, popup sections, tooltip, settings).
        var ids = []
        if (Plasmoid.configuration.enableClaude)     ids.push("claude")
        if (Plasmoid.configuration.enableCodex)      ids.push("codex")
        if (Plasmoid.configuration.enableZai)        ids.push("zai")
        if (Plasmoid.configuration.enableOpenRouter) ids.push("openrouter")
        if (Plasmoid.configuration.enableKilo)       ids.push("kilo")
        return ids
    }

    preferredRepresentation: Plasmoid.formFactor === PlasmaCore.Types.Planar
        ? fullRepresentation
        : compactRepresentation
    compactRepresentation: CompactRepresentation { }
    fullRepresentation: FullRepresentation { }

    // The KDE System Tray container reads these instead of any ToolTipArea
    // inside the compact representation. Keep them in sync with the widget data.
    function _resetTimeLeft(rec) {
        if (!rec || !rec.resetsAt) return ""
        var diff = new Date(rec.resetsAt).getTime() - Date.now()
        if (diff <= 0) return "soon"
        return relativeMs(diff).replace(/^in /, "")
    }
    toolTipMainText: root.lastError ? "CodexBar — error" : "CodexBar"
    // RichText so the body can use an HTML table to right-align the percent
    // and time-left columns. PC3.Label inside Plasma's tooltip honours this.
    toolTipTextFormat: Text.RichText
    // Tooltip stays focused on the recurring usage windows users actually
    // watch: Claude 5h+7d, Codex 5h+7d, z.ai 5h+monthly. Extras (Sonnet,
    // Claude Design, Routines) and balance-only providers (OpenRouter, Kilo)
    // are intentionally omitted — they're available in the popup.
    readonly property var _tooltipSlots: ({
        claude: ["primary", "secondary"],
        codex:  ["primary", "secondary"],
        zai:    ["primary", "secondary"]
    })
    toolTipSubText: {
        if (root.lastError) return root.lastError
        var arr = root.snapshot.providers || []
        if (arr.length === 0) return root.loading ? "Loading…" : "No providers enabled"
        var labels = { codex: "Codex", claude: "Claude", zai: "z.ai" }
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
                var resetText = _resetTimeLeft(w)
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

    P5Support.DataSource {
        id: runner
        engine: "executable"
        connectedSources: []

        onNewData: function(sourceName, data) {
            disconnectSource(sourceName)
            root.loading = false
            var stdout = (data["stdout"] || "").trim()
            var stderr = (data["stderr"] || "").trim()
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

    // Fire-and-forget runner that re-builds ~/.codexbar/agents.json on each
    // poll tick. Hooks are no longer needed — the widget drives the refresh
    // itself by running the aggregator subprocess. Each invocation gets a
    // unique source name (`# t=…` is a shell comment) so the dataengine
    // treats every tick as a fresh command rather than caching the previous.
    P5Support.DataSource {
        id: aggregatorRunner
        engine: "executable"
        connectedSources: []

        onNewData: function(sourceName, data) {
            disconnectSource(sourceName)
            // Output ignored — the aggregator's side-effect is the file write.
        }
    }
    readonly property string aggregatorScriptPath: {
        var url = Qt.resolvedUrl("../scripts/codexbar_agents.py").toString()
        return url.replace(/^file:\/\//, "")
    }
    function runAggregator() {
        if (!root.agentsEnabled) return
        var cmd = "python3 \"" + root.aggregatorScriptPath + "\" --once # t=" + Date.now()
        aggregatorRunner.connectSource(cmd)
    }

    function refreshAgents() {
        if (!root.agentsEnabled) return
        if (root.agentsLoading) return
        root.agentsLoading = true
        // Requires QML_XHR_ALLOW_FILE_READ=1 in plasmashell's env — installed
        // by install_integration.py into ~/.config/plasma-workspace/env/.
        var xhr = new XMLHttpRequest()
        xhr.onreadystatechange = function() {
            if (xhr.readyState !== XMLHttpRequest.DONE) return
            root.agentsLoading = false
            if (xhr.status !== 0 && xhr.status !== 200) {
                if (xhr.status === 0 && (xhr.responseText || "").length === 0) {
                    root.agentsError = ""
                    return
                }
                root.agentsError = "file read failed (status " + xhr.status + ")"
                return
            }
            var text = (xhr.responseText || "").trim()
            if (text === "") {
                root.agentsError = ""
                root.agentSnapshot = {
                    updatedAt: "",
                    counts: { working: 0, blocked: 0, idle: 0, untracked: 0, total: 0 },
                    agents: []
                }
                return
            }
            try {
                var parsed = JSON.parse(text)
                // Drop untracked rows if the user opted out. Recompute the
                // total so chip counts and the "any agent?" check stay
                // consistent with what's displayed.
                if (Plasmoid.configuration.includeUntrackedAgents === false
                        && parsed.agents) {
                    parsed.agents = parsed.agents.filter(function(a) {
                        return a && a.state !== "untracked"
                    })
                    if (parsed.counts) {
                        parsed.counts.untracked = 0
                        parsed.counts.total = (parsed.counts.working || 0)
                            + (parsed.counts.blocked || 0)
                            + (parsed.counts.idle || 0)
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

    function refresh() {
        if (root.enabledProviders.length === 0) {
            root.snapshot = {
                updatedAt: new Date().toISOString(),
                highestProvider: null,
                highestPercent: 0,
                providers: [],
                fatal: null
            }
            return
        }
        if (root.loading) return
        root.loading = true
        var cmd = "python3 \"" + root.scriptPath + "\""
            + " --cli-path \"" + root.cliPath + "\""
            + " --providers " + root.enabledProviders.join(",")
        runner.connectSource(cmd)
    }

    Timer {
        id: poll
        interval: root.refreshMs
        running: true; repeat: true
        onTriggered: root.refresh()
    }
    onRefreshMsChanged: { poll.interval = root.refreshMs; poll.restart() }

    // Two timers — the aggregator runs slightly before the XHR read so the
    // file is fresh when we read it. Both use the same interval otherwise.
    Timer {
        id: agentsAggregator
        interval: root.agentsRefreshMs
        running: root.agentsEnabled
        repeat: true
        triggeredOnStart: true
        onTriggered: root.runAggregator()
    }
    Timer {
        id: agentsPoll
        // Lag the XHR by ~half the interval so the aggregator's most recent
        // write lands first. The reader is cheap; running both back-to-back
        // would just race the writer.
        interval: root.agentsRefreshMs
        running: root.agentsEnabled
        repeat: true
        onTriggered: root.refreshAgents()
    }
    onAgentsRefreshMsChanged: {
        agentsAggregator.interval = root.agentsRefreshMs
        agentsPoll.interval = root.agentsRefreshMs
        agentsAggregator.restart()
        agentsPoll.restart()
    }
    onAgentsEnabledChanged: {
        if (root.agentsEnabled) {
            agentsAggregator.start()
            agentsPoll.start()
            root.runAggregator()
            root.refreshAgents()
        } else {
            agentsAggregator.stop()
            agentsPoll.stop()
        }
    }

    Component.onCompleted: {
        // String → QKeySequence conversion only works in a JS assignment
        // (Plasma 6 / Qt 6 quirk), not a property binding. Super+A toggles
        // the popup via Plasma's default `activated` handler.
        Plasmoid.globalShortcut = "Meta+A"
        root.refresh()
        if (root.agentsEnabled) {
            root.runAggregator()
            root.refreshAgents()
        }
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

    function _absoluteTime(when) {
        var now = new Date()
        var sameDay = now.toDateString() === when.toDateString()
        var hhmm = pad2(when.getHours()) + ":" + pad2(when.getMinutes())
        if (sameDay) return hhmm
        var months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
                      "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        return months[when.getMonth()] + " " + when.getDate() + ", " + hhmm
    }

    // Returns "16:00 (2h 28m)" / "May 19, 21:56 (3d 8h)" / "" depending on data.
    function formatReset(rec) {
        if (!rec) return ""
        if (rec.resetsAt) {
            var when = new Date(rec.resetsAt)
            var diff = when.getTime() - Date.now()
            if (diff <= 0) return "soon"
            return _absoluteTime(when) + " (" + relativeMs(diff).replace(/^in /, "") + ")"
        }
        var desc = rec.resetDescription ? rec.resetDescription.toString().trim() : ""
        if (desc.length > 0) {
            desc = desc.replace(/^[Rr]esets\s+/, "")
            desc = desc.replace(/\s*\([^)]+\)\s*$/, "")
            return desc
        }
        return ""
    }

    function windowLabel(providerId, slot, rec, extraTitle) {
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
        return slot
    }

    function providerDisplayName(id) {
        var map = {
            codex: "OPENAI CODEX",
            claude: "CLAUDE CODE",
            zai: "Z.AI",
            openrouter: "OPENROUTER",
            kilo: "KILO"
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
    function ageFrom(ms) {
        if (!ms) return ""
        var diff = Math.max(0, Date.now() - ms)
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
