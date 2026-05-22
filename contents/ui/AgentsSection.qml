import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

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
    onFlatAgentsChanged: {
        if (flatAgents.length === 0) selectedIndex = -1
        else if (selectedIndex < 0 || selectedIndex >= flatAgents.length) selectedIndex = 0
    }

    function selectNext() {
        if (flatAgents.length === 0) return
        selectedIndex = (Math.max(0, selectedIndex) + 1) % flatAgents.length
    }
    function selectPrevious() {
        if (flatAgents.length === 0) return
        var n = flatAgents.length
        selectedIndex = ((selectedIndex < 0 ? 0 : selectedIndex) - 1 + n) % n
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

            implicitHeight: rowContent.implicitHeight + 4
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
                anchors.fill: parent
                radius: 4
                color: root.agentStateColor("working")
                opacity: rowItem._idleFreshness * 0.18
            }

            Rectangle {
                anchors.fill: parent
                radius: 4
                color: Kirigami.Theme.hoverColor
                opacity: rowItem.selected ? 0.45
                    : rowMouse.containsMouse ? 0.25 : 0
                Behavior on opacity { NumberAnimation { duration: 120 } }
            }

            readonly property string state: modelData.state || "idle"
            readonly property color tint: root.agentStateColor(state)
            // Prefer the agent-generated session title. Only fall through
            // to the raw user prompt when the user has opted into showing
            // it (otherwise the row just shows the provider name).
            readonly property string taskLabel: {
                if (modelData.windowTitle) return modelData.windowTitle
                if (agents.showPrompts && modelData.lastPrompt) return modelData.lastPrompt
                return modelData.provider || "agent"
            }

            // "Just finished" highlight: italicize the row while the session
            // has been idle for under a minute. Touches nowTick so the
            // binding re-evaluates each second and flips back to normal.
            readonly property bool recentlyIdle: {
                var _ = agents.nowTick
                if (rowItem.state !== "idle") return false
                var since = rowItem.modelData.stateChangedAt || 0
                if (!since) return false
                return (Date.now() - since) < 60000
            }

            RowLayout {
                id: rowContent
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                anchors.leftMargin: 4
                anchors.rightMargin: 4
                spacing: Kirigami.Units.smallSpacing

                Rectangle {
                    width: 10; height: 10; radius: 5
                    color: rowItem.tint
                    Layout.alignment: Qt.AlignVCenter

                    // Gently pulse the state dot for the first minute after
                    // a session goes idle, so freshly-finished agents catch
                    // the eye. `alwaysRunToEnd` ensures the animation lands
                    // back at full opacity when the minute elapses.
                    SequentialAnimation on opacity {
                        running: rowItem.recentlyIdle
                        loops: Animation.Infinite
                        alwaysRunToEnd: true
                        NumberAnimation { to: 0.35; duration: 700; easing.type: Easing.InOutSine }
                        NumberAnimation { to: 1.0;  duration: 700; easing.type: Easing.InOutSine }
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
            }

            MouseArea {
                id: rowMouse
                anchors.fill: parent
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
                + "\nclick to focus terminal"
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
