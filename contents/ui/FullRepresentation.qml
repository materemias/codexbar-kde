import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

Item {
    id: full

    implicitWidth: Kirigami.Units.gridUnit * 24
    implicitHeight: 460

    Layout.preferredWidth: implicitWidth
    Layout.preferredHeight: implicitHeight
    Layout.minimumWidth: Kirigami.Units.gridUnit * 20
    Layout.minimumHeight: 280
    Layout.maximumHeight: Math.min(900, Screen.desktopAvailableHeight * 0.85)

    readonly property bool agentsTabVisible: Plasmoid.configuration.showAgents !== false
    readonly property int blockedCount: {
        var s = root.agentSnapshot
        if (!s || !s.counts) return 0
        return s.counts.blocked || 0
    }
    readonly property int agentTotalCount: {
        var s = root.agentSnapshot
        if (!s || !s.counts) return 0
        return (s.counts.total || 0) + (s.counts.untracked || 0)
    }

    // Header sits outside the tab area so it stays pinned.
    ColumnLayout {
        id: headerWrap
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.margins: Kirigami.Units.largeSpacing
        spacing: Kirigami.Units.smallSpacing

        RowLayout {
            Layout.fillWidth: true
            spacing: Kirigami.Units.smallSpacing

            PC3.Label {
                text: "CodexBar"
                font.pixelSize: Kirigami.Theme.defaultFont.pixelSize * 1.3
                font.weight: Font.Bold
                Layout.fillWidth: true
            }
            PC3.Label {
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                opacity: 0.7
                text: {
                    if (root.loading) return "refreshing…"
                    if (!root.snapshot.updatedAt) return ""
                    var t = new Date(root.snapshot.updatedAt)
                    return "updated " + t.toLocaleTimeString(Qt.locale(), Locale.ShortFormat)
                }
            }
            PC3.ToolButton {
                icon.name: "view-refresh"
                onClicked: {
                    root.refresh()
                    root.refreshAgents()
                }
                PC3.ToolTip.visible: hovered
                PC3.ToolTip.text: "Refresh now"
            }
        }

        QQC2.TabBar {
            id: tabBar
            Layout.fillWidth: true
            visible: full.agentsTabVisible
            currentIndex: root.requestedTab === "agents" ? 1
                : root.requestedTab === "usage" ? 0
                : (full.blockedCount > 0 ? 1 : 0)

            // A manual user click on a TabButton breaks the binding above.
            // Re-establish it explicitly whenever requestedTab changes (e.g.
            // when Super+A nudges us to Agents on an already-open popup).
            Connections {
                target: root
                function onRequestedTabChanged() {
                    if (root.requestedTab === "agents") tabBar.currentIndex = 1
                    else if (root.requestedTab === "usage") tabBar.currentIndex = 0
                }
            }

            QQC2.TabButton {
                text: "Usage"
                width: implicitWidth
            }
            QQC2.TabButton {
                text: full.agentTotalCount > 0
                    ? "Agents (" + full.agentTotalCount + ")"
                    : "Agents"
                width: implicitWidth
            }
        }

        Kirigami.Separator {
            Layout.fillWidth: true
            visible: !full.agentsTabVisible
        }

        PC3.Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            color: Kirigami.Theme.negativeTextColor
            visible: text.length > 0
            text: {
                if (root.snapshot.fatal) return root.snapshot.fatal.message
                if (root.lastError) return root.lastError
                return ""
            }
        }
    }

    // Tab content area.
    Item {
        id: tabHost
        anchors.top: headerWrap.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.leftMargin: Kirigami.Units.largeSpacing
        anchors.rightMargin: Kirigami.Units.largeSpacing
        anchors.bottomMargin: Kirigami.Units.largeSpacing
        anchors.topMargin: Kirigami.Units.smallSpacing

        readonly property int activeTab: full.agentsTabVisible ? tabBar.currentIndex : 0

        // --- Usage tab ---
        QQC2.ScrollView {
            anchors.fill: parent
            visible: tabHost.activeTab === 0
            contentWidth: availableWidth
            clip: true

            ColumnLayout {
                width: parent.width
                spacing: Kirigami.Units.smallSpacing

                PC3.Label {
                    Layout.fillWidth: true
                    wrapMode: Text.WordWrap
                    opacity: 0.7
                    visible: (root.snapshot.providers || []).length === 0
                        && !root.lastError && !root.snapshot.fatal
                    text: root.loading ? "Loading…" : "No providers enabled. Open Settings to enable some."
                }

                Repeater {
                    model: root.snapshot.providers || []
                    delegate: ColumnLayout {
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing
                        required property var modelData
                        required property int index

                        ProviderSection {
                            Layout.fillWidth: true
                            record: parent.modelData
                        }

                        Kirigami.Separator {
                            Layout.fillWidth: true
                            visible: parent.index < (root.snapshot.providers || []).length - 1
                            Layout.topMargin: Kirigami.Units.smallSpacing
                            Layout.bottomMargin: Kirigami.Units.smallSpacing / 2
                            opacity: 0.4
                        }
                    }
                }
            }
        }

        // --- Agents tab ---
        QQC2.ScrollView {
            anchors.fill: parent
            visible: tabHost.activeTab === 1 && full.agentsTabVisible
            contentWidth: availableWidth
            clip: true

            ColumnLayout {
                width: parent.width
                spacing: Kirigami.Units.smallSpacing

                AgentsSection {
                    id: agentsSection
                    Layout.fillWidth: true
                }
            }
        }
    }

    // Keyboard navigation: up/down moves the agent selection, Enter
    // activates the highlighted row. Works on any tab — pressing Enter
    // also switches to the Agents tab so the user sees what they activated.
    focus: true
    Keys.onPressed: function(event) {
        if (!agentsSection) return
        if (event.key === Qt.Key_Up) {
            agentsSection.selectPrevious()
            event.accepted = true
        } else if (event.key === Qt.Key_Down) {
            agentsSection.selectNext()
            event.accepted = true
        } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            root.requestedTab = "agents"
            agentsSection.activateSelected()
            event.accepted = true
        }
    }

    // Grab focus on a slight delay (the popup window isn't always settled
    // by the time expandedChanged fires) so arrow keys work immediately.
    Connections {
        target: root
        function onExpandedChanged() {
            if (root.expanded) {
                refocusTimer.restart()
                agentsSection.selectedIndex = 0
            }
        }
    }
    Timer {
        id: refocusTimer
        interval: 50
        onTriggered: full.forceActiveFocus()
    }
    Component.onCompleted: full.forceActiveFocus()
}
