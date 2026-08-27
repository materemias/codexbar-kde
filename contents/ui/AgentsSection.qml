import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami
import org.kde.taskmanager as TaskManager

ColumnLayout {
    id: agents
    spacing: Kirigami.Units.smallSpacing

    readonly property var snap: root.agentSnapshot
    readonly property var counts: snap && snap.counts ? snap.counts : ({})
    readonly property var list: snap && snap.agents ? snap.agents : []
    readonly property bool hasSomething: (counts.total || 0) > 0
    readonly property bool showPrompts: Plasmoid.configuration.showAgentPrompts === true

    // Cluster sessions by cwd. Groups are sorted alphabetically by folder
    // name so positions stay stable as states change. Within a group,
    // sessions stay in the order the aggregator emitted (blocked first).
    readonly property var groups: {
        var byFolder = {}
        var order = []
        var src = agents.list
        for (var i = 0; i < src.length; i++) {
            var a = src[i]
            if (!a) continue
            if (!agents._matchesFilter(a)) continue
            var folder = root.cwdLabel(a.cwd || "") || (a.provider || "agent")
            var g = byFolder[folder]
            if (!g) {
                g = { folder: folder, sessions: [] }
                byFolder[folder] = g
                order.push(folder)
            }
            g.sessions.push(a)
        }
        var arr = order.map(function(f) { return byFolder[f] })
        arr.sort(function(a, b) {
            return a.folder.toLowerCase().localeCompare(b.folder.toLowerCase())
        })
        return arr
    }

    // Keyboard navigation. `selectedIndex` is the row in `flatAgents` (the
    // groups expanded in display order) that's highlighted; -1 = nothing.
    // Reset to 0 whenever the popup opens or the list changes.
    property int selectedIndex: -1
    readonly property var flatAgents: {
        var out = []
        for (var i = 0; i < groups.length; i++) {
            for (var j = 0; j < groups[i].sessions.length; j++) {
                out.push(groups[i].sessions[j])
            }
        }
        return out
    }

    // Peek panel: sessionId of the one row whose recent messages are
    // expanded inline. Compared by string, same reason as selectedIndex —
    // the aggregate objects are replaced on every poll tick.
    property string peekSid: ""

    // Type-to-filter: printable keys typed while the popup has focus
    // accumulate here (FullRepresentation forwards them). Empty = no filter.
    property string filterText: ""

    // Fuzzy matching stays within one session field. Recent turns use exact
    // substring matching so unrelated prose cannot satisfy a short query.
    function _fuzzyMatches(text, q) {
        var hay = (text || "").toLowerCase()
        var j = 0
        for (var i = 0; i < hay.length && j < q.length; i++) {
            if (hay[i] === q[j]) j++
        }
        return j === q.length
    }

    function _matchesFilter(a) {
        var q = filterText.toLowerCase()
        if (!q) return true

        var fields = [a.windowTitle, a.lastPrompt, a.cwd, a.provider]
        for (var i = 0; i < fields.length; i++) {
            if (_fuzzyMatches(fields[i], q)) return true
        }

        var rec = a.recent || []
        for (var j = 0; j < rec.length; j++) {
            if ((rec[j].text || "").toLowerCase().indexOf(q) !== -1) return true
        }
        return false
    }

    function togglePeek() {
        if (selectedIndex < 0 || selectedIndex >= flatAgents.length) return
        var a = flatAgents[selectedIndex]
        if (!a || !a.sessionId) return
        peekSid = peekSid === a.sessionId ? "" : a.sessionId
    }

    onFlatAgentsChanged: {
        if (flatAgents.length === 0) selectedIndex = -1
        else if (selectedIndex < 0 || selectedIndex >= flatAgents.length) selectedIndex = 0
        if (peekSid !== "") {
            var still = false
            for (var k = 0; k < flatAgents.length; k++) {
                if (flatAgents[k].sessionId === peekSid) { still = true; break }
            }
            if (!still) peekSid = ""
        }
    }


    function selectAt(index) {
        if (flatAgents.length === 0) return
        var followPeek = peekSid !== ""
        selectedIndex = index
        if (followPeek) peekSid = flatAgents[index].sessionId || ""
    }
    function selectNext() {
        if (flatAgents.length === 0) return
        selectAt((Math.max(0, selectedIndex) + 1) % flatAgents.length)
    }
    function selectPrevious() {
        if (flatAgents.length === 0) return
        var n = flatAgents.length
        selectAt(((selectedIndex < 0 ? 0 : selectedIndex) - 1 + n) % n)
    }
    function activateSelected() {
        if (selectedIndex < 0 || selectedIndex >= flatAgents.length) return
        var a = flatAgents[selectedIndex]
        if (a && a.sessionId) {
            Qt.openUrlExternally("codexbar://focus/" + a.sessionId)
        }
    }

    // Ticking value so age labels ("2m", "15s") refresh while popup is open
    // without re-polling the aggregator. Updated by an internal Timer below.
    property int nowTick: 0

    Timer {
        interval: 1000
        running: root.expanded && agents.hasSomething
        repeat: true
        onTriggered: agents.nowTick = Date.now()
    }

    function _ageLabel(ms) {
        // touch nowTick so the binding re-evaluates each second
        var _ = agents.nowTick
        return root.ageFrom(ms)
    }

    // --- Virtual desktop lookup ------------------------------------------
    // Which desktop a session's terminal window lives on. Plasma's
    // taskmanager model is the only QML source for window pids and
    // desktops; its roles carry no role ids we could pass to data(), so
    // an invisible Repeater harvests them through per-delegate `model.*`
    // bindings. Every window delegate registers itself in `taskWindows`;
    // role updates recreate its `info` object, and the chip bindings that
    // read `info` re-resolve. Lives here rather than main.qml so the model
    // only exists while the Agents tab does.
    TaskManager.VirtualDesktopInfo { id: vdInfo }

    property var taskWindows: []

    Repeater {
        // Grouping collapses same-app windows into one row with a single
        // pid; we need every window with its own pid to match sessions.
        model: TaskManager.TasksModel {
            id: tasksModel
            groupMode: TaskManager.TasksModel.GroupDisabled
        }
        // Repeater demands Item delegates; this one stays invisible and
        // zero-size, it only exists to bind the roles we need.
        delegate: Item {
            id: taskWin
            visible: false
            readonly property int pid: model.AppPid !== undefined ? model.AppPid : 0
            // Plural: a window may live on several desktops at once, and
            // the role arrives as a nested list.
            readonly property var desktops: {
                var outer = agents._roleList(model.VirtualDesktops)
                var flat = []
                for (var k = 0; k < outer.length; k++) {
                    var inner = agents._roleList(outer[k])
                    for (var m = 0; m < inner.length; m++) flat.push(inner[m])
                }
                return flat
            }
            readonly property bool all: model.IsOnAllVirtualDesktops === true
            readonly property string caption: model.display !== undefined ? String(model.display) : ""
            readonly property var info: ({
                pid: taskWin.pid,
                desktops: taskWin.desktops,
                all: taskWin.all,
                caption: taskWin.caption
            })
            Component.onCompleted: agents.taskWindows = agents.taskWindows.concat([taskWin])
            Component.onDestruction: agents.taskWindows =
                agents.taskWindows.filter(function(w) { return w !== taskWin })
        }
    }

    // Task roles arrive as QVariant lists that aren't always real JS
    // arrays; anything object-shaped with a numeric length counts.
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

    // Cwd basename as a window-caption hint, mirroring
    // codexbar_focus._caption_hint: last path segment, keeping alnum
    // plus "-", "_", "." and space, compared case-insensitively.
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

    // Desktop ids are uuid strings on Wayland and 1-based numbers on X11;
    // normalize either to a position in vdInfo.desktopIds.
    function _desktopIndexOf(id) {
        if (id === undefined || id === null) return -1
        var i = (vdInfo.desktopIds || []).indexOf(id)
        if (i >= 0) return i
        return (typeof id === "number" && id >= 1) ? id - 1 : -1
    }

    // Resolve the desktop chip for a session: the first task window whose
    // pid appears in the session's ancestor chain. Multi-window hosts (one
    // kitty or VS Code pid behind many windows) prefer the window whose
    // caption contains the cwd basename, same disambiguation as
    // click-to-focus. A window on all desktops shows "all" and counts as
    // being on the current one. Returns null when nothing resolved.
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

    // Header row: section label + count summary on the right.
    RowLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        PC3.Label {
            text: "AGENTS"
            font.weight: Font.Bold
            font.pixelSize: Kirigami.Theme.defaultFont.pixelSize * 1.02
            font.letterSpacing: 0.4
            Layout.alignment: Qt.AlignVCenter
        }

        Item { Layout.fillWidth: true }

        // Count chips. Render only non-zero states so the row stays compact
        // when most agents are idle.
        Repeater {
            model: [
                { key: "blocked",   label: "blocked",   color: "#ef4444" },
                { key: "working",   label: "working",   color: "#22c55e" },
                { key: "idle",      label: "idle",      color: "#9ca3af" },
                { key: "untracked", label: "untracked", color: "#3b82f6" }
            ]
            delegate: RowLayout {
                required property var modelData
                readonly property int v: agents.counts[modelData.key] || 0
                visible: v > 0
                spacing: 3
                Layout.alignment: Qt.AlignVCenter

                Rectangle {
                    width: 8; height: 8; radius: 4
                    color: modelData.color
                    Layout.alignment: Qt.AlignVCenter
                }
                PC3.Label {
                    text: v + " " + modelData.label
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    opacity: 0.85
                    Layout.alignment: Qt.AlignVCenter
                    rightPadding: Kirigami.Units.smallSpacing
                }
            }
        }

        PC3.Label {
            visible: !agents.hasSomething
            text: "none"
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            opacity: 0.55
        }
    }

    // Active filter query + match count. Only shown while typing.
    RowLayout {
        visible: agents.filterText.length > 0
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        PC3.Label {
            text: "filter: " + agents.filterText
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            opacity: 0.85
            Layout.fillWidth: true
        }
        PC3.Label {
            text: agents.flatAgents.length + (agents.flatAgents.length === 1 ? " match" : " matches")
                + " · Esc clears"
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            opacity: 0.6
        }
    }

    PC3.Label {
        visible: root.agentsError && root.agentsError.length > 0
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
        color: Kirigami.Theme.negativeTextColor
        text: root.agentsError
    }

    // Folder-grouped session rows. Each group: a small bold folder header
    // followed by indented per-session rows.
    Repeater {
        model: agents.groups
        delegate: ColumnLayout {
            id: groupItem
            Layout.fillWidth: true
            spacing: 1
            required property var modelData

            PC3.Label {
                text: groupItem.modelData.folder
                font.weight: Font.Bold
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                opacity: 0.8
                Layout.fillWidth: true
                Layout.topMargin: 4
                Layout.bottomMargin: 1
            }

            Repeater {
                model: groupItem.modelData.sessions
                delegate: sessionRow
            }
        }
    }

    // Per-session row delegate — single line. Folder name is already shown
    // as the group header, so the row's primary label is the task title.
    Component {
        id: sessionRow
        Item {
            id: rowItem
            Layout.fillWidth: true
            Layout.leftMargin: 6
            required property var modelData

            implicitHeight: rowCol.implicitHeight + 4
            readonly property bool peekOpen: agents.peekSid !== ""
                && modelData && modelData.sessionId === agents.peekSid

            // While a filter is active: up to two conversation lines that
            // contain the query, as StyledText with the query highlighted.
            readonly property var filterSnippets: {
                var q = agents.filterText.toLowerCase()
                if (!q || !modelData) return []
                var out = []
                var esc = function(s) {
                    return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
                        .replace(/>/g, "&gt;")
                }
                var hl = "<b><font color=\"#ffb300\">"
                var rec = modelData.recent || []
                for (var i = 0; i < rec.length && out.length < 2; i++) {
                    var t = (rec[i].text || "").replace(/\s+/g, " ")
                    var idx = t.toLowerCase().indexOf(q)
                    if (idx === -1) continue
                    var start = Math.max(0, idx - 60)
                    var end = Math.min(t.length, idx + q.length + 100)
                    out.push((rec[i].role === "user" ? "> " : "· ")
                        + (start > 0 ? "… " : "")
                        + esc(t.slice(start, idx))
                        + hl + esc(t.slice(idx, idx + q.length)) + "</font></b>"
                        + esc(t.slice(idx + q.length, end))
                        + (end < t.length ? " …" : ""))
                }
                return out
            }

            // Row + optional peek panel stacked. The panel grows inside the
            // popup's ScrollView when open; only one row peeks at a time
            // (agents.peekSid), so height stays bounded.
            ColumnLayout {
                id: rowCol
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                spacing: 0

                RowLayout {
                    id: rowContent
                    Layout.fillWidth: true
                    Layout.leftMargin: 4
                    Layout.rightMargin: 4
                    spacing: Kirigami.Units.smallSpacing

                    Rectangle {
                        property real pulse: 0
                        width: 10; height: 10; radius: 5
                        color: rowItem.recentlyIdle
                            ? Kirigami.Theme.positiveTextColor : rowItem.tint
                        opacity: rowItem.recentlyIdle ? 1 - pulse * 0.65 : 1
                        scale: rowItem.recentlyIdle ? 1 + pulse * 0.35 : 1
                        Layout.alignment: Qt.AlignVCenter

                        // Pulse the state dot's brightness and size for the
                        // first minute after a session goes idle.
                        SequentialAnimation on pulse {
                            running: rowItem.recentlyIdle
                            loops: Animation.Infinite
                            alwaysRunToEnd: true
                            NumberAnimation { to: 1; duration: 700; easing.type: Easing.InOutSine }
                            NumberAnimation { to: 0; duration: 700; easing.type: Easing.InOutSine }
                        }
                    }

                    Kirigami.Icon {
                        source: rowItem.modelData.provider
                            ? Qt.resolvedUrl("../icons/" + rowItem.modelData.provider + ".svg")
                            : ""
                        implicitWidth: Kirigami.Units.iconSizes.small
                        implicitHeight: Kirigami.Units.iconSizes.small
                        smooth: true
                        visible: source.toString().length > 0
                        Layout.alignment: Qt.AlignVCenter
                    }

                    PC3.Label {
                        text: rowItem.taskLabel
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                        Layout.alignment: Qt.AlignVCenter
                    }

                    PC3.Label {
                        text: rowItem.modelData.host || ""
                        visible: text.length > 0
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        opacity: 0.55
                        Layout.alignment: Qt.AlignVCenter
                    }

                    PC3.Label {
                        text: rowItem.state + " " + agents._ageLabel(rowItem.modelData.stateChangedAt)
                        color: rowItem.tint
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        font.weight: Font.DemiBold
                        Layout.alignment: Qt.AlignVCenter
                    }

                    // Desktop chip: the virtual desktop hosting this
                    // session's terminal window. Highlighted when that
                    // desktop (or "all" desktops) is the current one;
                    // absent when no window resolved. Visibility never
                    // depends on hover, so the row cannot jump.
                    Rectangle {
                        visible: rowItem.desktopInfo !== null
                        width: desktopChipLabel.implicitWidth + 8
                        height: desktopChipLabel.implicitHeight + 3
                        radius: 3
                        color: rowItem.desktopInfo && rowItem.desktopInfo.onCurrent
                            ? Kirigami.Theme.highlightColor : "transparent"
                        border.width: 1
                        border.color: rowItem.desktopInfo && rowItem.desktopInfo.onCurrent
                            ? Kirigami.Theme.highlightColor
                            : Qt.rgba(Kirigami.Theme.textColor.r,
                                Kirigami.Theme.textColor.g,
                                Kirigami.Theme.textColor.b, 0.3)
                        Layout.alignment: Qt.AlignVCenter

                        PC3.Label {
                            id: desktopChipLabel
                            anchors.centerIn: parent
                            text: rowItem.desktopInfo ? rowItem.desktopInfo.label : ""
                            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                            font.weight: Font.DemiBold
                            color: rowItem.desktopInfo && rowItem.desktopInfo.onCurrent
                                ? Kirigami.Theme.highlightedTextColor
                                : Kirigami.Theme.textColor
                            opacity: rowItem.desktopInfo && rowItem.desktopInfo.onCurrent
                                ? 1 : 0.65
                        }
                    }

                    PC3.ToolButton {
                        // Keep the button's slot in the layout when its icon is hidden.
                        visible: true
                        opacity: rowMouse.containsMouse || rowItem.peekOpen ? 1 : 0
                        enabled: rowMouse.containsMouse || rowItem.peekOpen
                        icon.name: rowItem.peekOpen ? "arrow-up" : "arrow-down"
                        Layout.preferredWidth: Kirigami.Units.iconSizes.smallMedium + 6
                        Layout.preferredHeight: Kirigami.Units.iconSizes.smallMedium + 6
                        implicitWidth: Kirigami.Units.iconSizes.smallMedium + 6
                        implicitHeight: Kirigami.Units.iconSizes.smallMedium + 6
                        padding: 1
                        onClicked: {
                            agents.peekSid = rowItem.peekOpen
                                ? "" : (rowItem.modelData.sessionId || "")
                        }
                        PC3.ToolTip.visible: hovered
                        PC3.ToolTip.text: "Peek at recent messages (or press Space)"
                        PC3.ToolTip.delay: 400
                    }
                }

                // Filter-hit snippet panel: the conversation lines that
                // matched the active filter, query highlighted.
                Rectangle {
                    visible: agents.filterText.length > 0
                        && rowItem.filterSnippets.length > 0
                    Layout.fillWidth: true
                    Layout.leftMargin: 18
                    Layout.topMargin: 2
                    implicitHeight: snipCol.implicitHeight + 8
                    radius: 4
                    color: Kirigami.Theme.backgroundColor
                    border.color: Qt.rgba(
                        Kirigami.Theme.textColor.r,
                        Kirigami.Theme.textColor.g,
                        Kirigami.Theme.textColor.b,
                        0.14
                    )
                    border.width: 1

                    ColumnLayout {
                        id: snipCol
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 4
                        spacing: 1

                        Repeater {
                            model: rowItem.filterSnippets
                            delegate: PC3.Label {
                                required property string modelData
                                Layout.fillWidth: true
                                text: modelData
                                textFormat: Text.StyledText
                                wrapMode: Text.WordWrap
                                maximumLineCount: 2
                                elide: Text.ElideRight
                                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                                opacity: 0.85
                            }
                        }
                    }
                }

                // Inline peek panel: the last few turns of the session,
                // straight from its transcript via the aggregator's
                // `recent` field.
                Rectangle {
                    visible: rowItem.peekOpen
                    Layout.fillWidth: true
                    Layout.leftMargin: 18
                    Layout.topMargin: 2
                    Layout.bottomMargin: 4
                    implicitHeight: peekCol.implicitHeight + 10
                    radius: 4
                    color: Kirigami.Theme.backgroundColor
                    border.color: Qt.rgba(
                        Kirigami.Theme.textColor.r,
                        Kirigami.Theme.textColor.g,
                        Kirigami.Theme.textColor.b,
                        0.14
                    )
                    border.width: 1

                    ColumnLayout {
                        id: peekCol
                        anchors.left: parent.left
                        anchors.right: parent.right
                        anchors.top: parent.top
                        anchors.margins: 5
                        spacing: 1

                        PC3.Label {
                            Layout.fillWidth: true
                            text: (rowItem.modelData.cwd || "")
                                + (rowItem.modelData.host ? "  ·  " + rowItem.modelData.host : "")
                            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                            opacity: 0.85
                            elide: Text.ElideMiddle
                        }

                        Repeater {
                            model: rowItem.modelData.recent || []
                            delegate: RowLayout {
                                Layout.fillWidth: true
                                spacing: Kirigami.Units.smallSpacing
                                required property var modelData

                                PC3.Label {
                                    text: modelData.role === "user" ? "you" : "ai"
                                    color: modelData.role === "user"
                                        ? Kirigami.Theme.highlightColor
                                        : Kirigami.Theme.textColor
                                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                                    font.weight: Font.DemiBold
                                    Layout.preferredWidth: 22
                                    Layout.alignment: Qt.AlignTop
                                    horizontalAlignment: Text.AlignRight
                                }

                                PC3.Label {
                                    text: modelData.kind === "tools"
                                        ? "→ " + (modelData.text || "")
                                        : (modelData.text || "")
                                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                                    font.italic: modelData.kind === "tools"
                                    color: Kirigami.Theme.textColor
                                    opacity: modelData.kind === "tools" ? 0.75 : 1
                                    wrapMode: modelData.kind === "tools" ? Text.NoWrap : Text.Wrap
                                    maximumLineCount: modelData.kind === "tools" ? 1 : 4
                                    elide: Text.ElideRight
                                    Layout.fillWidth: true
                                }
                            }
                        }

                        PC3.Label {
                            visible: ((rowItem.modelData.recent || []).length === 0)
                            text: "no messages captured yet"
                            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                            opacity: 0.7
                        }
                    }

                    // Swallow clicks so clicking inside the panel doesn't
                    // focus the terminal — reading shouldn't teleport.
                    MouseArea {
                        anchors.fill: parent
                        hoverEnabled: true
                    }
                }
            }

            // Compare by sessionId (stable string), NOT by object reference.
            // The aggregate JSON re-parses every 5s, replacing all agent
            // objects — so `flatAgents[selectedIndex] === modelData` silently
            // always returns false after the first poll tick.
            readonly property string _curSid: agents.flatAgents
                && agents.selectedIndex >= 0
                && agents.selectedIndex < agents.flatAgents.length
                    ? (agents.flatAgents[agents.selectedIndex].sessionId || "")
                    : ""
            readonly property bool selected: _curSid !== ""
                && modelData && modelData.sessionId === _curSid

            // 1.0 at the moment a session goes idle, linearly down to 0 at
            // 60s. Drives the "just finished" green background wash.
            readonly property real _idleFreshness: {
                var _ = agents.nowTick
                if (rowItem.state !== "idle") return 0
                var since = rowItem.modelData.stateChangedAt || 0
                if (!since) return 0
                var age = Date.now() - since
                return age >= 60000 ? 0 : (60000 - age) / 60000
            }

            // "Just finished" green wash. Sits below the selection/hover
            // highlight so both can apply at once.
            Rectangle {
                z: -1
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                height: rowContent.implicitHeight + 4
                radius: 4
                color: root.agentStateColor("working")
                opacity: rowItem._idleFreshness * 0.08
            }

            Rectangle {
                z: -1
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                height: rowContent.implicitHeight + 4
                radius: 4
                color: Kirigami.Theme.alternateBackgroundColor
                opacity: rowItem.selected ? 0.1
                    : rowMouse.containsMouse ? 0.04 : 0
                Behavior on opacity { NumberAnimation { duration: 120 } }
            }

            Rectangle {
                anchors.left: parent.left
                anchors.top: parent.top
                width: 3
                height: rowContent.implicitHeight + 4
                radius: 1
                color: rowItem.tint
                opacity: rowItem.selected ? 1
                    : rowMouse.containsMouse ? 0.7 : 0
                Behavior on opacity { NumberAnimation { duration: 120 } }
            }

            readonly property string state: modelData.state || "idle"
            readonly property color tint: root.agentStateColor(state)
            // Desktop badge data for this row, or null when the host
            // window didn't resolve against Plasma's task list.
            readonly property var desktopInfo: agents.desktopInfoFor(modelData)
            // Prefer the agent-generated session title. Only fall through
            // to the raw user prompt when the user has opted into showing
            // it (otherwise the row just shows the provider name).
            readonly property string taskLabel: {
                if (modelData.windowTitle) return modelData.windowTitle
                if (agents.showPrompts && modelData.lastPrompt) return modelData.lastPrompt
                return modelData.provider || "agent"
            }

            // "Just finished" highlight: true while the existing freshness
            // value remains above zero.
            readonly property bool recentlyIdle: _idleFreshness > 0


            MouseArea {
                id: rowMouse
                // Only the single-line row is clickable-to-focus; the peek
                // panel below swallows its own clicks.
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                height: rowContent.implicitHeight + 6
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                acceptedButtons: Qt.LeftButton
                onClicked: {
                    if (!rowItem.modelData.sessionId) return
                    Qt.openUrlExternally(
                        "codexbar://focus/" + rowItem.modelData.sessionId
                    )
                }
            }

            PC3.ToolTip.visible: rowMouse.containsMouse
                && (rowItem.modelData.cwd || "").length > 0
            PC3.ToolTip.text: (rowItem.modelData.cwd || "")
                + (rowItem.modelData.host ? "  (" + rowItem.modelData.host + ")" : "")
                + "\nclick to focus · peek button or Space for recent messages"
            PC3.ToolTip.delay: 600
        }
    }

    // Untracked summary row — shown only when untracked count > 0 since these
    // sessions have no per-record info.
    RowLayout {
        visible: (agents.counts.untracked || 0) > 0
        Layout.fillWidth: true
        Layout.topMargin: 2
        spacing: Kirigami.Units.smallSpacing

        Rectangle {
            width: 10; height: 10; radius: 5
            color: root.agentStateColor("untracked")
            Layout.alignment: Qt.AlignVCenter
        }
        PC3.Label {
            text: (agents.counts.untracked || 0) + " more running (no hook sentinel)"
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            opacity: 0.7
            Layout.fillWidth: true
        }
    }
}
