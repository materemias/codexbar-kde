import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

ColumnLayout {
    id: agents
    spacing: Kirigami.Units.smallSpacing

    readonly property var snap: root.agentSnapshot
    readonly property var counts: snap && snap.counts ? snap.counts : ({})
    readonly property var list: snap && Array.isArray(snap.agents) ? snap.agents : []
    readonly property bool hasSomething: (counts.total || 0) > 0
    readonly property bool showPrompts: Plasmoid.configuration.showAgentPrompts === true

    // Cluster sessions by cwd. Groups are sorted alphabetically by folder name,
    // and rows within each group are newest first by stateChangedAt.
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
        for (var j = 0; j < arr.length; j++) {
            arr[j].sessions.sort(function(a, b) {
                var aTime = Number(a.stateChangedAt) || 0
                var bTime = Number(b.stateChangedAt) || 0
                if (aTime !== bTime) return bTime - aTime
                var aId = String(a.sessionId || "")
                var bId = String(b.sessionId || "")
                return aId < bId ? -1 : aId > bId ? 1 : 0
            })
        }

        arr.sort(function(a, b) {
            return a.folder.toLowerCase().localeCompare(b.folder.toLowerCase())
        })
        return arr
    }

    // Keep identity across poll-driven reordering. A vanished selection
    // clears instead of silently targeting the next session at its index.
    property string selectedKey: ""
    readonly property int selectedIndex: indexForKey(selectedKey)
    readonly property var flatAgents: {
        var out = []
        for (var i = 0; i < groups.length; i++) {
            for (var j = 0; j < groups[i].sessions.length; j++) {
                out.push(groups[i].sessions[j])
            }
        }
        return out
    }

    // Composite provider/session identity survives fresh JSON objects.
    property string peekKey: ""

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

        var fields = [a.windowTitle, a.lastPrompt, a.cwd, a.provider, a.model]
        for (var i = 0; i < fields.length; i++) {
            if (_fuzzyMatches(fields[i], q)) return true
        }

        var rec = a.recent || []
        for (var j = 0; j < rec.length; j++) {
            if ((rec[j].text || "").toLowerCase().indexOf(q) !== -1) return true
        }
        return false
    }

    // While a filter is active: up to two conversation lines of a record
    // that contain the query, as StyledText with the query highlighted.
    // The Agents and History rows both render these under the row.
    function filterSnippets(record) {
        var q = filterText.toLowerCase()
        if (!q || !record) return []
        var out = []
        var esc = function(s) {
            return s.replace(/&/g, "&amp;").replace(/</g, "&lt;")
                .replace(/>/g, "&gt;")
        }
        var hl = "<b><font color=\"#ffb300\">"
        var rec = record.recent || []
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

    function _providerName(provider) {
        var names = {
            claude: "Claude",
            codex: "Codex",
            opencode: "OpenCode",
            pi: "pi",
            omp: "omp"
        }
        return names[provider] || provider || "Agent"
    }

    function _modelName(model) {
        if (typeof model !== "string") return ""
        var value = model.trim()
        if (!value) return ""
        var slash = value.lastIndexOf("/")
        return slash >= 0 ? value.slice(slash + 1) : value
    }

    function agentKey(record) {
        if (!record || !record.provider || !record.sessionId) return ""
        return JSON.stringify([record.provider, record.sessionId])
    }

    function indexForKey(key) {
        if (!key) return -1
        for (var i = 0; i < flatAgents.length; i++) {
            if (agentKey(flatAgents[i]) === key) return i
        }
        return -1
    }

    function togglePeek() {
        if (indexForKey(selectedKey) < 0) return
        peekKey = peekKey === selectedKey ? "" : selectedKey
    }

    onFlatAgentsChanged: {
        if (selectedKey && indexForKey(selectedKey) < 0) selectedKey = ""
        if (peekKey && indexForKey(peekKey) < 0) peekKey = ""
    }


    function selectAt(index) {
        if (index < 0 || index >= flatAgents.length) return
        var followPeek = peekKey !== ""
        selectedKey = agentKey(flatAgents[index])
        if (followPeek) peekKey = selectedKey
    }
    function selectNext() {
        if (flatAgents.length === 0) return
        selectAt((selectedIndex + 1) % flatAgents.length)
    }
    function selectPrevious() {
        if (flatAgents.length === 0) return
        var n = flatAgents.length
        selectAt(((selectedIndex < 0 ? 0 : selectedIndex) - 1 + n) % n)
    }
    function activateSelected() {
        var index = indexForKey(selectedKey)
        if (index < 0) return
        var a = flatAgents[index]
        if (a && a.sessionId) {
            Qt.openUrlExternally("codexbar://focus/" + a.sessionId)
        }
    }

    function _ageLabel(ms) {
        return root.ageFrom(ms, root.nowMs)
    }

    // Header row: section label + count summary on the right.
    RowLayout {
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        PC3.Label {
            text: "ACTIVE AGENTS"
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
            text: agents.flatAgents.length
                + (agents.flatAgents.length === 1 ? " match" : " matches")
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
            readonly property string sessionKey: agents.agentKey(modelData)
            readonly property bool peekOpen: sessionKey !== ""
                && sessionKey === agents.peekKey

            readonly property var filterSnippets: agents.filterSnippets(modelData)

            // Row + optional peek panel stacked. The panel grows inside the
            // popup's ScrollView when open; only one row peeks at a time
            // (agents.peekKey), so height stays bounded.
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
                        // first five minutes after a session goes idle.
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
                        text: agents._modelName(rowItem.modelData.model)
                        visible: text.length > 0
                        color: Kirigami.Theme.linkColor
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        font.weight: Font.DemiBold
                        elide: Text.ElideMiddle
                        Layout.maximumWidth: Kirigami.Units.gridUnit * 9
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
                            agents.peekKey = rowItem.peekOpen ? "" : rowItem.sessionKey
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
                        spacing: 3

                        PC3.Label {
                            Layout.fillWidth: true
                            text: (rowItem.modelData.cwd || "")
                                + (rowItem.modelData.host ? "  ·  " + rowItem.modelData.host : "")
                            textFormat: Text.PlainText
                            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                            font.weight: Font.DemiBold
                            opacity: 0.75
                            elide: Text.ElideMiddle
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: 1
                            color: Qt.rgba(
                                Kirigami.Theme.textColor.r,
                                Kirigami.Theme.textColor.g,
                                Kirigami.Theme.textColor.b,
                                0.14
                            )
                        }

                        Repeater {
                            model: rowItem.modelData.recent || []
                            delegate: Rectangle {
                                id: turnCard
                                required property var modelData
                                readonly property bool toolTurn: modelData.kind === "tools"
                                readonly property bool userTurn: modelData.role === "user"
                                readonly property color accent: {
                                    if (toolTurn) return Kirigami.Theme.neutralTextColor
                                    if (userTurn) return Kirigami.Theme.highlightColor
                                    return Kirigami.Theme.linkColor
                                }

                                Layout.fillWidth: true
                                implicitHeight: turnRow.implicitHeight + 6
                                radius: 3
                                color: Qt.rgba(
                                    accent.r,
                                    accent.g,
                                    accent.b,
                                    0.14
                                )
                                border.color: Qt.rgba(
                                    accent.r,
                                    accent.g,
                                    accent.b,
                                    0.40
                                )
                                border.width: 1

                                Rectangle {
                                    anchors.left: parent.left
                                    anchors.top: parent.top
                                    anchors.bottom: parent.bottom
                                    anchors.leftMargin: 1
                                    anchors.topMargin: 1
                                    anchors.bottomMargin: 1
                                    width: 3
                                    color: turnCard.accent
                                }

                                RowLayout {
                                    id: turnRow
                                    anchors.fill: parent
                                    anchors.margins: 3
                                    anchors.leftMargin: 8
                                    spacing: Kirigami.Units.smallSpacing

                                    PC3.Label {
                                        text: turnCard.toolTurn
                                            ? "TOOLS"
                                            : (turnCard.userTurn ? "YOU" : "AI")
                                        textFormat: Text.PlainText
                                        color: turnCard.accent
                                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                                        font.weight: Font.DemiBold
                                        font.italic: turnCard.toolTurn
                                        Layout.preferredWidth: Kirigami.Units.gridUnit * 2
                                        Layout.minimumWidth: implicitWidth
                                        Layout.alignment: Qt.AlignTop
                                        horizontalAlignment: Text.AlignRight
                                    }

                                    PC3.Label {
                                        text: turnCard.modelData.text || ""
                                        textFormat: Text.PlainText
                                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                                        font.italic: turnCard.toolTurn
                                        color: Kirigami.Theme.textColor
                                        opacity: turnCard.toolTurn ? 0.7 : 1
                                        wrapMode: turnCard.toolTurn ? Text.NoWrap : Text.Wrap
                                        maximumLineCount: turnCard.toolTurn ? 1 : 4
                                        elide: Text.ElideRight
                                        lineHeight: 1.17
                                        lineHeightMode: Text.ProportionalHeight
                                        Layout.fillWidth: true
                                    }
                                }
                            }
                        }

                        PC3.Label {
                            visible: ((rowItem.modelData.recent || []).length === 0)
                            text: "no messages captured yet"
                            textFormat: Text.PlainText
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

            readonly property bool selected: sessionKey !== ""
                && sessionKey === agents.selectedKey

            // 1.0 at the moment a session goes idle, linearly down to 0 at
            // five minutes. Drives the "just finished" green background wash.
            readonly property real _idleFreshness: {
                if (rowItem.state !== "idle") return 0
                var since = rowItem.modelData.stateChangedAt || 0
                if (!since) return 0
                var age = root.nowMs - since
                return age >= 300000 ? 0 : (300000 - age) / 300000
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
            readonly property var desktopInfo: root.desktopInfoFor(modelData)
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
