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
        return s.counts.total || 0
    }
    readonly property int recoveryCount: {
        var s = root.agentSnapshot
        return s && Array.isArray(s.recovery) ? s.recovery.length : 0
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
                Layout.alignment: Qt.AlignBaseline
            }
            PC3.Label {
                // codexbar CLI version, not the applet package version.
                text: {
                    var v = root.snapshot ? root.snapshot.cliVersion : null
                    return typeof v === "string" && v.length > 0 ? "v" + v : ""
                }
                visible: text.length > 0
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                opacity: 0.55
                Layout.alignment: Qt.AlignBaseline
            }
            Item {
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
                objectName: "refreshNowButton"
                icon.name: "view-refresh"
                onClicked: {
                    root.refresh()
                    root.runAggregator()
                }
                PC3.ToolTip.visible: hovered
                PC3.ToolTip.text: "Refresh now (R)"
            }
        }

        QQC2.TabBar {
            id: tabBar
            Layout.fillWidth: true
            visible: full.agentsTabVisible
            currentIndex: root.requestedTab === "agents" ? 1
                : root.requestedTab === "usage" ? 0
                : (full.blockedCount > 0 || full.recoveryCount > 0 ? 1 : 0)

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
                text: {
                    if (full.recoveryCount === 0) {
                        return full.agentTotalCount > 0
                            ? "Agents (" + full.agentTotalCount + ")"
                            : "Agents"
                    }
                    if (full.agentTotalCount === 0) {
                        return "Agents (" + full.recoveryCount + " restore)"
                    }
                    return "Agents (" + full.agentTotalCount + " live, "
                        + full.recoveryCount + " restore)"
                }
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

                        readonly property var providers: root.snapshot.providers || []
                        readonly property bool isCodex: modelData.id === "codex"
                        readonly property bool codexFirst: isCodex
                            && index === root.firstProviderIndex("codex")
                        readonly property bool nextIsCodex: isCodex && index + 1 < providers.length
                            && providers[index + 1] && providers[index + 1].id === "codex"

                        // Codex group header: one provider title for all
                        // accounts, with the omp rotation mode as a chip whose
                        // tooltip carries the rule description.
                        RowLayout {
                            id: codexHeader
                            readonly property var rotationState: root.snapshot.codexRotation
                            readonly property bool stalled: !!rotationState && rotationState.stalled === true
                            readonly property color stateColor: stalled
                                ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.disabledTextColor
                            visible: parent.codexFirst
                            Layout.fillWidth: true
                            Layout.minimumHeight: Kirigami.Units.iconSizes.smallMedium
                            spacing: Kirigami.Units.smallSpacing

                            Kirigami.Icon {
                                source: Qt.resolvedUrl("../icons/codex.svg")
                                implicitWidth: Kirigami.Units.iconSizes.smallMedium
                                implicitHeight: Kirigami.Units.iconSizes.smallMedium
                                Layout.alignment: Qt.AlignVCenter
                                smooth: true
                            }
                            PC3.Label {
                                text: root.providerDisplayName("codex")
                                font.weight: Font.Bold
                                font.pixelSize: Kirigami.Theme.defaultFont.pixelSize * 1.02
                                font.letterSpacing: 0.4
                                Layout.alignment: Qt.AlignVCenter
                            }
                            Item { Layout.fillWidth: true }

                            RowLayout {
                                visible: !!codexHeader.rotationState
                                spacing: Kirigami.Units.smallSpacing
                                Layout.alignment: Qt.AlignVCenter

                                PC3.Label {
                                    text: codexHeader.rotationState
                                        ? "omp · " + codexHeader.rotationState.operatingMode : ""
                                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                                    opacity: 0.6
                                }
                                Rectangle {
                                    implicitWidth: rotationModeLabel.implicitWidth + Kirigami.Units.smallSpacing * 2
                                    implicitHeight: rotationModeLabel.implicitHeight + 2
                                    radius: height / 2
                                    color: Qt.rgba(codexHeader.stateColor.r, codexHeader.stateColor.g,
                                        codexHeader.stateColor.b, 0.18)
                                    border.width: 1
                                    border.color: codexHeader.stateColor

                                    PC3.Label {
                                        id: rotationModeLabel
                                        anchors.centerIn: parent
                                        text: codexHeader.stalled ? "STALLED"
                                            : codexHeader.rotationState ? codexHeader.rotationState.mode : ""
                                        color: codexHeader.stateColor
                                        font.weight: Font.DemiBold
                                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize - 2
                                    }
                                }

                                HoverHandler { id: rotationHover }
                                PC3.ToolTip {
                                    visible: rotationHover.hovered && !!codexHeader.rotationState
                                    delay: 350
                                    text: !codexHeader.rotationState ? ""
                                        : "omp Codex rotation (" + codexHeader.rotationState.mode + "): "
                                          + codexHeader.rotationState.description
                                          + (codexHeader.stalled ? "\nSTALLED: all accounts closed" : "")
                                }
                            }
                        }

                        ProviderSection {
                            id: compositeSection
                            readonly property var compositeRecord: parent.codexFirst
                                ? root.codexCompositeRecord() : null
                            Layout.fillWidth: true
                            visible: !!compositeRecord
                            record: compositeRecord || ({})
                            subheading: true
                        }

                        Kirigami.Separator {
                            Layout.fillWidth: true
                            visible: compositeSection.visible
                            Layout.topMargin: Kirigami.Units.smallSpacing / 2
                            opacity: 0.2
                        }

                        ProviderSection {
                            Layout.fillWidth: true
                            record: parent.modelData
                            subheading: parent.isCodex
                            forecast: showForecast ? root.snapshot.forecast : null
                            showForecast: root.codexForecastEnabled
                                && parent.isCodex
                                && index === root.lastCodexIndex()
                        }

                        // Accounts inside the Codex group get a faint divider;
                        // provider boundaries keep the stronger one.
                        Kirigami.Separator {
                            Layout.fillWidth: true
                            visible: parent.index < parent.providers.length - 1
                            Layout.topMargin: parent.nextIsCodex
                                ? Kirigami.Units.smallSpacing / 2 : Kirigami.Units.smallSpacing
                            Layout.bottomMargin: parent.nextIsCodex ? 0 : Kirigami.Units.smallSpacing / 2
                            opacity: parent.nextIsCodex ? 0.2 : 0.4
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
                    objectName: "agentsSection"
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
        } else if (event.key === Qt.Key_Space
            && agentsSection.filterText.length === 0) {
            // Space with no query in progress = peek toggle. Once the user
            // is typing a filter, space is just a space.
            root.requestedTab = "agents"
            agentsSection.togglePeek()
            event.accepted = true
        } else if (event.key === Qt.Key_Return || event.key === Qt.Key_Enter) {
            root.requestedTab = "agents"
            agentsSection.activateSelected()
            event.accepted = true
        } else if (event.key === Qt.Key_Escape) {
            if (agentsSection.filterText.length > 0) {
                agentsSection.filterText = ""
                event.accepted = true
            }
        } else if (event.key === Qt.Key_Right
            && tabBar.currentIndex < tabBar.count - 1) {
            tabBar.currentIndex++
            event.accepted = true
        } else if (event.key === Qt.Key_Left
            && tabBar.currentIndex > 0) {
            tabBar.currentIndex--
            event.accepted = true
        } else if (event.key === Qt.Key_R
            && tabHost.activeTab === 0) {
            // R on the Usage tab = manual refresh, same as the header
            // button. On the Agents tab R stays a filter character.
            root.refresh()
            root.runAggregator()
            event.accepted = true
        } else if (event.key === Qt.Key_Backspace) {
            agentsSection.filterText =
                agentsSection.filterText.slice(0, -1)
            event.accepted = true
        } else if (event.text.length > 0
            && !/[\u0000-\u001f\u007f]/.test(event.text)
            && (event.modifiers & ~Qt.ShiftModifier) === Qt.NoModifier) {
            // Type-to-filter: any printable key starts/extends the query.
            root.requestedTab = "agents"
            agentsSection.filterText += event.text
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
                if (agentsSection.selectedIndex < 0) agentsSection.selectAt(0)
            } else {
                agentsSection.filterText = ""
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
