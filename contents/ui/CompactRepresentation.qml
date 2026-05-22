import QtQuick
import QtQuick.Layouts
import org.kde.plasma.plasmoid
import org.kde.plasma.core as PlasmaCore
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

Item {
    id: compact

    readonly property int style: Plasmoid.configuration.compactStyle || 0
    readonly property var configured: Plasmoid.configuration.trayIndicators || []

    // Provider-grouped indicator records:
    //   [{ providerId, icon, windows: [{ slot, pct, hasData, ok }] }, ...]
    // Grouping is in first-appearance order from the configured list, so the
    // user's ordering ("codex:primary, claude:primary, codex:secondary") still
    // produces a clean group-by-provider layout.
    function _iconFor(pid) {
        return Qt.resolvedUrl("../icons/" + pid + ".svg")
    }

    // Canonical provider display order, applied regardless of how the user's
    // config list happens to be sorted — keeps tray ordering consistent with
    // the popup, tooltip, and settings layout.
    readonly property var _canonicalOrder: ["claude", "codex", "zai", "openrouter", "kilo"]

    readonly property var groups: {
        var keys = compact.configured || []
        if (keys.length === 0) return []
        var providers = root.snapshot.providers || []
        var byId = {}
        for (var i = 0; i < providers.length; i++) byId[providers[i].id] = providers[i]

        var groupByPid = {}
        for (var k = 0; k < keys.length; k++) {
            var parts = keys[k].split(":")
            var pid = parts[0]
            var slot = parts[1] || "primary"
            if (groupByPid[pid] === undefined) {
                groupByPid[pid] = { providerId: pid, icon: compact._iconFor(pid), windows: [] }
            }
            var rec = byId[pid]
            var win = { slot: slot, pct: 0, hasData: false, ok: false }
            if (rec) {
                win.ok = !!rec.ok
                if (rec.ok) {
                    var w = rec[slot]
                    // Standard primary/secondary/tertiary live on the record;
                    // anything else (claude-design, claude-routines, …) is
                    // resolved against extraRateWindows by id.
                    if (!w && rec.extraRateWindows) {
                        for (var xi = 0; xi < rec.extraRateWindows.length; xi++) {
                            if (rec.extraRateWindows[xi].id === slot) {
                                w = rec.extraRateWindows[xi].window
                                break
                            }
                        }
                    }
                    if (w && w.usedPercent !== undefined && w.usedPercent !== null) {
                        win.pct = Math.max(0, Math.min(100, w.usedPercent))
                        win.hasData = true
                    }
                }
            }
            groupByPid[pid].windows.push(win)
        }

        var out = []
        var seen = {}
        for (var c = 0; c < compact._canonicalOrder.length; c++) {
            var canonId = compact._canonicalOrder[c]
            if (groupByPid[canonId]) {
                out.push(groupByPid[canonId])
                seen[canonId] = true
            }
        }
        // Any providers not in the canonical list go at the end.
        for (var pid2 in groupByPid) {
            if (!seen[pid2]) out.push(groupByPid[pid2])
        }
        return out
    }

    // Configurable target size from settings; clamped to what the panel
    // actually offers so we never overflow a thin panel. Use the limiting
    // (smaller, non-zero) dimension so horizontal panels constrain by height
    // and vertical panels constrain by width.
    readonly property int _configuredSize: Plasmoid.configuration.trayIconSize || 22
    readonly property int _availSize: {
        var h = compact.height
        var w = compact.width
        if (h > 0 && w > 0) return Math.min(h, w)
        return Math.max(h, w)
    }
    readonly property int ringSize: {
        if (_availSize <= 0) return _configuredSize
        return Math.max(14, Math.min(_availSize - 2, _configuredSize))
    }
    readonly property int iconSize: ringSize

    // True only when there's room for a horizontal panel + a topic label —
    // vertical/tray panels stay terse.
    readonly property bool _isHorizontalPanel: Plasmoid.formFactor === PlasmaCore.Types.Horizontal
    readonly property bool topicVisible: _isHorizontalPanel
        && Plasmoid.configuration.showAgentTopicInPanel !== false
        && featuredTopic.length > 0
    readonly property string featuredTopic: {
        var f = root.featuredAgent ? root.featuredAgent() : null
        if (!f) return ""
        return f.windowTitle || f.lastPrompt || ""
    }
    readonly property string featuredState: {
        var f = root.featuredAgent ? root.featuredAgent() : null
        return f ? f.state : ""
    }

    // Per-state agent counts for the inline status dots.
    readonly property var _agentCounts: (root.agentSnapshot && root.agentSnapshot.counts) || ({})
    readonly property int workingCount: _agentCounts.working || 0
    readonly property int idleCount: _agentCounts.idle || 0
    readonly property bool stateDotsVisible: Plasmoid.configuration.showAgentStateDots !== false
        && (workingCount + blockedAgentCount + idleCount) > 0

    Layout.fillWidth: false
    Layout.minimumWidth: row.implicitWidth + Kirigami.Units.smallSpacing
    Layout.preferredWidth: row.implicitWidth + Kirigami.Units.smallSpacing

    MouseArea {
        anchors.fill: parent
        acceptedButtons: Qt.LeftButton | Qt.MiddleButton
        onClicked: function(mouse) {
            if (mouse.button === Qt.MiddleButton) {
                root.refresh()
            } else {
                root.expanded = !root.expanded
            }
        }
    }

    // Blocked-agent indicator: small red dot in the top-right corner of the
    // tray icon group whenever an agent is waiting on user input. Gated by
    // `agentBlockedBadge` so users who don't want it can switch off.
    readonly property int blockedAgentCount: {
        var s = root.agentSnapshot
        if (!s || !s.counts) return 0
        return s.counts.blocked || 0
    }

    Rectangle {
        id: blockedBadge
        visible: Plasmoid.configuration.agentBlockedBadge !== false
            && compact.blockedAgentCount > 0
        anchors.top: parent.top
        anchors.right: parent.right
        anchors.topMargin: -1
        anchors.rightMargin: -1
        width: Math.max(8, Math.floor(compact.ringSize / 3))
        height: width
        radius: width / 2
        color: "#ef4444"
        border.width: 1
        border.color: Kirigami.Theme.backgroundColor
        z: 10

        // Number-in-dot — only when the dot is large enough to read it.
        PC3.Label {
            visible: parent.width >= 12
            anchors.centerIn: parent
            text: compact.blockedAgentCount.toString()
            color: "white"
            font.pixelSize: Math.max(7, Math.floor(parent.width * 0.7))
            font.weight: Font.Bold
        }
    }

    RowLayout {
        id: row
        anchors.centerIn: parent
        spacing: Kirigami.Units.largeSpacing

        // Fallback when no indicators configured.
        Kirigami.Icon {
            visible: compact.groups.length === 0
            source: "applications-development-symbolic"
            implicitWidth: compact.iconSize
            implicitHeight: compact.iconSize
            isMask: true
        }

        Repeater {
            model: compact.groups
            delegate: RowLayout {
                required property var modelData
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Icon {
                    source: modelData.icon
                    implicitWidth: compact.iconSize
                    implicitHeight: compact.iconSize
                    smooth: true
                    Layout.alignment: Qt.AlignVCenter
                }

                Repeater {
                    id: _windowsRepeater
                    model: modelData.windows
                    delegate: Item {
                        id: indicatorRoot
                        required property var modelData

                        readonly property real pct: indicatorRoot.modelData.pct
                        readonly property bool active: indicatorRoot.modelData.ok && indicatorRoot.modelData.hasData
                        readonly property color tint: indicatorRoot.active
                            ? root.colorFor(pct)
                            : Kirigami.Theme.disabledTextColor

                        implicitWidth: ring.visible
                            ? compact.ringSize + (percentOutside.visible ? percentOutside.implicitWidth + 2 : 0)
                            : percentOutside.implicitWidth
                        implicitHeight: compact.ringSize
                        Layout.alignment: Qt.AlignVCenter

                        Item {
                            id: ring
                            visible: compact.style !== 2
                            anchors.left: parent.left
                            anchors.verticalCenter: parent.verticalCenter
                            width: compact.ringSize
                            height: compact.ringSize

                            Canvas {
                                id: ringCanvas
                                anchors.fill: parent
                                property real pct: indicatorRoot.pct
                                property color color: indicatorRoot.tint
                                property bool active: indicatorRoot.active
                                onPctChanged: requestPaint()
                                onColorChanged: requestPaint()
                                onActiveChanged: requestPaint()

                                onPaint: {
                                    var ctx = getContext("2d")
                                    ctx.clearRect(0, 0, width, height)
                                    var cx = width / 2
                                    var cy = height / 2
                                    var lw = Math.max(2, Math.floor(width / 8))
                                    var r = Math.min(cx, cy) - lw / 2 - 1
                                    ctx.lineCap = "round"

                                    // background track
                                    ctx.lineWidth = lw
                                    ctx.strokeStyle = Qt.rgba(1, 1, 1, 0.18)
                                    ctx.beginPath()
                                    ctx.arc(cx, cy, r, 0, Math.PI * 2)
                                    ctx.stroke()
                                    if (!active) return
                                    // usage arc
                                    ctx.strokeStyle = color
                                    ctx.beginPath()
                                    var start = -Math.PI / 2
                                    var end = start + (Math.PI * 2 * pct / 100)
                                    ctx.arc(cx, cy, r, start, end, false)
                                    ctx.stroke()
                                }
                            }

                            // Inside-ring number (style 0 = ring + number inside).
                            PC3.Label {
                                visible: compact.style === 0
                                anchors.centerIn: parent
                                text: indicatorRoot.active ? Math.round(indicatorRoot.pct).toString() : "—"
                                color: indicatorRoot.tint
                                font.pixelSize: Math.max(8, Math.floor(compact.ringSize * 0.45))
                                font.weight: Font.DemiBold
                            }
                        }

                        // Style 2 (percent only, no ring).
                        PC3.Label {
                            id: percentOutside
                            visible: compact.style === 2
                            anchors.verticalCenter: parent.verticalCenter
                            anchors.left: ring.visible ? ring.right : parent.left
                            anchors.leftMargin: ring.visible ? 2 : 0
                            text: indicatorRoot.active ? Math.round(indicatorRoot.pct) + "%" : "—"
                            color: indicatorRoot.tint
                            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        }
                    }
                }
            }
        }

        // Vertical stack of small colored dots with their count next to them.
        // 3 rows fit inside a single ring-height slot in a horizontal panel.
        ColumnLayout {
            id: dotsStack
            visible: compact.stateDotsVisible
            spacing: 0
            Layout.alignment: Qt.AlignVCenter
            Layout.leftMargin: Kirigami.Units.smallSpacing

            Repeater {
                model: [
                    { stateKey: "working", count: compact.workingCount },
                    { stateKey: "blocked", count: compact.blockedAgentCount },
                    { stateKey: "idle",    count: compact.idleCount }
                ]
                delegate: RowLayout {
                    required property var modelData
                    spacing: 4
                    Layout.alignment: Qt.AlignVCenter

                    Rectangle {
                        readonly property real _scale: (Plasmoid.configuration.agentStateDotsScale || 100) / 100.0
                        readonly property int dotSize: Math.max(4, Math.floor(compact.ringSize * 0.38 * _scale))
                        width: dotSize; height: dotSize; radius: dotSize / 2
                        color: root.agentStateColor(modelData.stateKey)
                        Layout.alignment: Qt.AlignVCenter
                    }
                    PC3.Label {
                        readonly property real _scale: (Plasmoid.configuration.agentStateDotsScale || 100) / 100.0
                        text: modelData.count
                        color: root.agentStateColor(modelData.stateKey)
                        font.pixelSize: Math.max(6, Math.floor(compact.ringSize * 0.42 * _scale))
                        font.weight: Font.DemiBold
                        Layout.alignment: Qt.AlignVCenter
                    }
                }
            }
        }

        // Featured-agent topic. Asterisk + dim grey text, capped width.
        // Shown only on horizontal panels (vertical/tray is too narrow).
        RowLayout {
            visible: compact.topicVisible
            spacing: 4
            Layout.alignment: Qt.AlignVCenter

            PC3.Label {
                text: "✳"
                color: root.agentStateColor(compact.featuredState)
                font.pixelSize: Math.max(10, Math.floor(compact.ringSize * 0.55))
                font.weight: Font.Bold
                Layout.alignment: Qt.AlignVCenter
            }

            PC3.Label {
                text: compact.featuredTopic
                color: "#9ca3af"
                font.pixelSize: Math.max(9, Math.floor(compact.ringSize * 0.45))
                font.italic: true
                elide: Text.ElideRight
                Layout.maximumWidth: Plasmoid.configuration.agentTopicMaxWidth || 260
                Layout.alignment: Qt.AlignVCenter
            }
        }
    }
}
