import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

// Ended sessions: those cut off by a reboot, ordered by desktop, then the
// most recent exits of this boot, newest first. Rows never join the live
// agent selection, peek, or focus behavior.
ColumnLayout {
    id: history
    spacing: Kirigami.Units.smallSpacing

    // AgentsSection owns the type-to-filter query and name helpers.
    required property var agentsView

    readonly property var records: {
        var s = root.agentSnapshot
        return s && Array.isArray(s.history) ? s.history : []
    }
    readonly property var filtered: {
        var out = []
        for (var i = 0; i < history.records.length; i++) {
            var record = history.records[i]
            if (record && history.agentsView._matchesFilter(record)) out.push(record)
        }
        return out
    }
    readonly property var rebooted: {
        // Desktop number first, then "all", then unknown; newest first
        // within one desktop.
        function desktopRank(record) {
            var d = String(record.desktop || "")
            if (/^[0-9]+$/.test(d)) return Number(d)
            return d === "all" ? 1e9 : 2e9
        }
        var out = history.filtered.filter(function(r) { return r.closedBy === "reboot" })
        out.sort(function(a, b) {
            var aDesk = desktopRank(a)
            var bDesk = desktopRank(b)
            if (aDesk !== bDesk) return aDesk - bDesk
            return history._newestFirst(a, b)
        })
        return out
    }
    readonly property var exited: {
        var out = history.filtered.filter(function(r) { return r.closedBy === "exit" })
        out.sort(history._newestFirst)
        return out
    }

    function _newestFirst(a, b) {
        var aTime = Number(a.lastSeenAt) || 0
        var bTime = Number(b.lastSeenAt) || 0
        if (aTime !== bTime) return bTime - aTime
        var aKey = String(a.provider || "") + "\n" + String(a.sessionId || "")
        var bKey = String(b.provider || "") + "\n" + String(b.sessionId || "")
        return aKey < bKey ? -1 : aKey > bKey ? 1 : 0
    }

    PC3.Label {
        visible: history.records.length === 0
        text: "No ended sessions yet. Sessions that exit or are cut off by a restart appear here."
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        opacity: 0.65
        Layout.fillWidth: true
    }

    PC3.Label {
        visible: history.agentsView.filterText.length > 0
        text: "filter: " + history.agentsView.filterText + " · "
            + history.filtered.length
            + (history.filtered.length === 1 ? " match" : " matches")
            + " · Esc clears"
        textFormat: Text.PlainText
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
        opacity: 0.85
        Layout.fillWidth: true
    }

    PC3.Label {
        visible: history.records.length > 0 && history.filtered.length === 0
        text: "No history records match this filter."
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
        opacity: 0.65
        Layout.fillWidth: true
    }

    PC3.Label {
        visible: root.historyLaunchError.length > 0
        text: root.historyLaunchError
        textFormat: Text.PlainText
        wrapMode: Text.WordWrap
        color: Kirigami.Theme.negativeTextColor
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
        Layout.fillWidth: true
    }

    PC3.Label {
        visible: history.rebooted.length > 0
        text: "Interrupted by restart"
        font.weight: Font.Bold
        font.pixelSize: Kirigami.Theme.defaultFont.pixelSize * 1.02
        Layout.fillWidth: true
    }

    Repeater {
        model: history.rebooted
        delegate: HistoryRow {}
    }

    PC3.Label {
        visible: history.exited.length > 0
        text: "Recently closed"
        font.weight: Font.Bold
        font.pixelSize: Kirigami.Theme.defaultFont.pixelSize * 1.02
        Layout.topMargin: history.rebooted.length > 0 ? Kirigami.Units.smallSpacing : 0
        Layout.fillWidth: true
    }

    // Recently closed rows mirror live agent rows: one line per session,
    // with a peek panel for its last turns and resume command.
    property string peekKey: ""
    onExitedChanged: {
        for (var i = 0; i < history.exited.length; i++) {
            if (history.agentsView.agentKey(history.exited[i]) === history.peekKey) return
        }
        history.peekKey = ""
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: 1

        Repeater {
            model: history.exited
            delegate: ClosedRow {}
        }
    }

    component ClosedRow: Item {
        id: closedRow
        required property var modelData
        Layout.fillWidth: true
        Layout.leftMargin: 6
        implicitHeight: closedCol.implicitHeight + 4

        readonly property string sessionKey: history.agentsView.agentKey(modelData)
        readonly property bool peekOpen: sessionKey !== "" && sessionKey === history.peekKey
        readonly property string command: modelData.resumeCommand || ""
        readonly property bool canLaunch: command.length > 0 && modelData.host === "kitty"
        readonly property bool launchAllowed: root.historyLaunchAllowed(sessionKey)
        readonly property color tint: Kirigami.Theme.disabledTextColor
        readonly property string taskLabel: {
            if (modelData.windowTitle) return modelData.windowTitle
            if (history.agentsView.showPrompts && modelData.lastPrompt) return modelData.lastPrompt
            return modelData.provider || "agent"
        }

        Rectangle {
            z: -1
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: closedLine.implicitHeight + 4
            radius: 4
            color: Kirigami.Theme.alternateBackgroundColor
            opacity: closedMouse.containsMouse || closedRow.peekOpen ? 0.06 : 0
            Behavior on opacity { NumberAnimation { duration: 120 } }
        }

        MouseArea {
            id: closedMouse
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: closedLine.implicitHeight + 6
            hoverEnabled: true
            cursorShape: Qt.PointingHandCursor
            onClicked: history.peekKey = closedRow.peekOpen ? "" : closedRow.sessionKey
        }

        ColumnLayout {
            id: closedCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            spacing: 0

            RowLayout {
                id: closedLine
                Layout.fillWidth: true
                Layout.leftMargin: 4
                Layout.rightMargin: 4
                spacing: Kirigami.Units.smallSpacing

                Rectangle {
                    width: 10; height: 10; radius: 5
                    color: "transparent"
                    border.width: 2
                    border.color: closedRow.tint
                    Layout.alignment: Qt.AlignVCenter
                }

                Kirigami.Icon {
                    source: closedRow.modelData.provider
                        ? Qt.resolvedUrl("../icons/" + closedRow.modelData.provider + ".svg")
                        : ""
                    implicitWidth: Kirigami.Units.iconSizes.small
                    implicitHeight: Kirigami.Units.iconSizes.small
                    smooth: true
                    visible: source.toString().length > 0
                    Layout.alignment: Qt.AlignVCenter
                }

                PC3.Label {
                    text: closedRow.taskLabel
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                }

                PC3.Label {
                    text: history.agentsView._modelName(closedRow.modelData.model)
                    visible: text.length > 0
                    color: Kirigami.Theme.linkColor
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    font.weight: Font.DemiBold
                    elide: Text.ElideMiddle
                    Layout.maximumWidth: Kirigami.Units.gridUnit * 9
                    Layout.alignment: Qt.AlignVCenter
                }

                PC3.Label {
                    text: root.cwdLabel(closedRow.modelData.cwd || "")
                    visible: text.length > 0
                    textFormat: Text.PlainText
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    opacity: 0.55
                    elide: Text.ElideMiddle
                    Layout.maximumWidth: Kirigami.Units.gridUnit * 7
                    Layout.alignment: Qt.AlignVCenter
                }

                PC3.Label {
                    text: "closed " + root.ageFrom(Number(closedRow.modelData.lastSeenAt) || 0, root.nowMs)
                    color: closedRow.tint
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    font.weight: Font.DemiBold
                    Layout.alignment: Qt.AlignVCenter
                }

                Rectangle {
                    visible: (closedRow.modelData.desktop || "").length > 0
                    width: closedDesktopLabel.implicitWidth + 8
                    height: closedDesktopLabel.implicitHeight + 3
                    radius: 3
                    color: "transparent"
                    border.width: 1
                    border.color: Qt.rgba(Kirigami.Theme.textColor.r,
                        Kirigami.Theme.textColor.g,
                        Kirigami.Theme.textColor.b, 0.3)
                    Layout.alignment: Qt.AlignVCenter

                    PC3.Label {
                        id: closedDesktopLabel
                        anchors.centerIn: parent
                        text: closedRow.modelData.desktop || ""
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        font.weight: Font.DemiBold
                        opacity: 0.65
                    }
                }

                PC3.ToolButton {
                    icon.name: closedRow.peekOpen ? "arrow-up" : "arrow-down"
                    opacity: closedMouse.containsMouse || closedRow.peekOpen || hovered ? 1 : 0
                    implicitWidth: Kirigami.Units.iconSizes.smallMedium + 6
                    implicitHeight: Kirigami.Units.iconSizes.smallMedium + 6
                    padding: 1
                    onClicked: history.peekKey = closedRow.peekOpen ? "" : closedRow.sessionKey
                    PC3.ToolTip.visible: hovered
                    PC3.ToolTip.text: "Peek at recent messages and the resume command"
                    PC3.ToolTip.delay: 400
                }

                // Keeps its slot on rows that cannot launch so columns align.
                PC3.ToolButton {
                    icon.name: "media-playback-start"
                    opacity: closedRow.canLaunch ? 1 : 0
                    enabled: closedRow.canLaunch && closedRow.launchAllowed
                    implicitWidth: Kirigami.Units.iconSizes.smallMedium + 6
                    implicitHeight: Kirigami.Units.iconSizes.smallMedium + 6
                    padding: 1
                    onClicked: root.launchHistory(closedRow.modelData)
                    PC3.ToolTip.visible: hovered && closedRow.canLaunch
                    PC3.ToolTip.text: closedRow.launchAllowed
                        ? "Resume in kitty"
                            + (closedRow.modelData.desktop
                                ? " on desktop " + closedRow.modelData.desktop : "")
                        : "Launched"
                    PC3.ToolTip.delay: 400
                }
            }

            Rectangle {
                visible: closedRow.peekOpen
                Layout.fillWidth: true
                Layout.leftMargin: 18
                Layout.topMargin: 2
                Layout.bottomMargin: 4
                implicitHeight: closedPeek.implicitHeight + 10
                radius: 4
                color: Kirigami.Theme.backgroundColor
                border.width: 1
                border.color: Qt.rgba(Kirigami.Theme.textColor.r,
                    Kirigami.Theme.textColor.g,
                    Kirigami.Theme.textColor.b, 0.14)

                ColumnLayout {
                    id: closedPeek
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.top: parent.top
                    anchors.margins: 5
                    spacing: 3

                    PC3.Label {
                        Layout.fillWidth: true
                        text: (closedRow.modelData.cwd || "cwd unknown")
                            + (closedRow.modelData.host ? "  ·  " + closedRow.modelData.host : "")
                        textFormat: Text.PlainText
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        font.weight: Font.DemiBold
                        opacity: 0.75
                        elide: Text.ElideMiddle
                    }

                    RowLayout {
                        visible: closedRow.command.length > 0
                        Layout.fillWidth: true
                        spacing: Kirigami.Units.smallSpacing

                        TextEdit {
                            id: closedCommand
                            text: closedRow.command
                            textFormat: TextEdit.PlainText
                            readOnly: true
                            selectByMouse: true
                            wrapMode: TextEdit.NoWrap
                            clip: true
                            color: Kirigami.Theme.textColor
                            selectionColor: Kirigami.Theme.highlightColor
                            selectedTextColor: Kirigami.Theme.highlightedTextColor
                            font.family: "monospace"
                            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                            Layout.fillWidth: true
                        }

                        PC3.ToolButton {
                            icon.name: "edit-copy"
                            implicitWidth: Kirigami.Units.iconSizes.smallMedium + 6
                            implicitHeight: Kirigami.Units.iconSizes.smallMedium + 6
                            padding: 1
                            onClicked: {
                                closedCommand.selectAll()
                                closedCommand.copy()
                            }
                            PC3.ToolTip.visible: hovered
                            PC3.ToolTip.text: "Copy resume command"
                        }
                    }

                    PC3.Label {
                        visible: closedRow.command.length === 0
                        text: "Resume command unavailable for this session."
                        textFormat: Text.PlainText
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        opacity: 0.65
                    }

                    Repeater {
                        model: closedRow.modelData.recent || []
                        delegate: RowLayout {
                            id: closedTurn
                            required property var modelData
                            readonly property bool toolTurn: modelData.kind === "tools"
                            readonly property bool userTurn: modelData.role === "user"
                            readonly property color accent: toolTurn
                                ? Kirigami.Theme.neutralTextColor
                                : userTurn ? Kirigami.Theme.highlightColor : Kirigami.Theme.linkColor
                            Layout.fillWidth: true
                            spacing: Kirigami.Units.smallSpacing

                            PC3.Label {
                                text: closedTurn.toolTurn ? "TOOLS"
                                    : closedTurn.userTurn ? "YOU" : "AI"
                                color: closedTurn.accent
                                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                                font.weight: Font.DemiBold
                                font.italic: closedTurn.toolTurn
                                horizontalAlignment: Text.AlignRight
                                Layout.preferredWidth: Kirigami.Units.gridUnit * 2
                                Layout.alignment: Qt.AlignTop
                            }

                            PC3.Label {
                                text: closedTurn.modelData.text || ""
                                textFormat: Text.PlainText
                                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                                font.italic: closedTurn.toolTurn
                                opacity: closedTurn.toolTurn ? 0.7 : 1
                                wrapMode: closedTurn.toolTurn ? Text.NoWrap : Text.Wrap
                                maximumLineCount: closedTurn.toolTurn ? 1 : 3
                                elide: Text.ElideRight
                                Layout.fillWidth: true
                            }
                        }
                    }

                    PC3.Label {
                        visible: (closedRow.modelData.recent || []).length === 0
                        text: "no messages captured"
                        textFormat: Text.PlainText
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        opacity: 0.7
                    }
                }
            }
        }
    }

    component HistoryRow: Rectangle {
        id: historyRow
        required property var modelData
        Layout.fillWidth: true
        implicitHeight: historyCol.implicitHeight + 10
        radius: 4
        color: Kirigami.Theme.alternateBackgroundColor
        border.width: 1
        border.color: Qt.rgba(
            Kirigami.Theme.textColor.r,
            Kirigami.Theme.textColor.g,
            Kirigami.Theme.textColor.b,
            0.14
        )

        readonly property string lastSeenText: {
            var value = Number(modelData.lastSeenAt) || 0
            if (!value) return "last seen unknown"
            return "last seen " + new Date(value).toLocaleString(
                Qt.locale(), Locale.ShortFormat)
        }

        ColumnLayout {
            id: historyCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 5
            spacing: 3

            RowLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing

                Kirigami.Icon {
                    source: historyRow.modelData.provider
                        ? Qt.resolvedUrl("../icons/"
                            + historyRow.modelData.provider + ".svg")
                        : ""
                    implicitWidth: Kirigami.Units.iconSizes.small
                    implicitHeight: Kirigami.Units.iconSizes.small
                    smooth: true
                    visible: source.toString().length > 0
                    Layout.alignment: Qt.AlignVCenter
                }

                PC3.Label {
                    text: history.agentsView._providerName(historyRow.modelData.provider)
                    font.weight: Font.DemiBold
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    Layout.alignment: Qt.AlignVCenter
                }

                PC3.Label {
                    text: history.agentsView._modelName(historyRow.modelData.model)
                    visible: text.length > 0
                    color: Kirigami.Theme.linkColor
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    font.weight: Font.DemiBold
                    elide: Text.ElideMiddle
                    Layout.maximumWidth: Kirigami.Units.gridUnit * 9
                    Layout.alignment: Qt.AlignVCenter
                }

                PC3.Label {
                    text: historyRow.modelData.windowTitle
                        || historyRow.modelData.sessionId
                        || "session"
                    textFormat: Text.PlainText
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                }

                PC3.Label {
                    text: historyRow.lastSeenText
                    textFormat: Text.PlainText
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    opacity: 0.6
                    Layout.alignment: Qt.AlignVCenter
                }
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing

                PC3.Label {
                    text: historyRow.modelData.cwd || "cwd unknown"
                    textFormat: Text.PlainText
                    elide: Text.ElideMiddle
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    opacity: 0.75
                    Layout.fillWidth: true
                }

                PC3.Label {
                    text: historyRow.modelData.host
                        ? "host " + historyRow.modelData.host
                        : "host unknown"
                    textFormat: Text.PlainText
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    opacity: 0.65
                }

                Rectangle {
                    width: historyDesktopLabel.implicitWidth + 8
                    height: historyDesktopLabel.implicitHeight + 3
                    radius: 3
                    color: "transparent"
                    border.width: 1
                    border.color: Qt.rgba(
                        Kirigami.Theme.textColor.r,
                        Kirigami.Theme.textColor.g,
                        Kirigami.Theme.textColor.b,
                        0.3
                    )
                    Layout.alignment: Qt.AlignVCenter

                    PC3.Label {
                        id: historyDesktopLabel
                        anchors.centerIn: parent
                        text: historyRow.modelData.desktop
                            ? "desktop " + historyRow.modelData.desktop
                            : "desktop unknown"
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                        font.weight: Font.DemiBold
                        opacity: 0.65
                    }
                }
            }

            PC3.Label {
                visible: text.length > 0
                text: historyRow.modelData.lastPrompt || ""
                textFormat: Text.PlainText
                wrapMode: Text.WordWrap
                maximumLineCount: 2
                elide: Text.ElideRight
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                opacity: 0.8
                Layout.fillWidth: true
            }

            RowLayout {
                visible: (historyRow.modelData.resumeCommand || "").length > 0
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing

                Rectangle {
                    Layout.fillWidth: true
                    Layout.preferredHeight: resumeText.contentHeight + 8
                    radius: 3
                    color: Kirigami.Theme.backgroundColor
                    border.width: 1
                    border.color: Qt.rgba(
                        Kirigami.Theme.textColor.r,
                        Kirigami.Theme.textColor.g,
                        Kirigami.Theme.textColor.b,
                        0.2
                    )

                    TextEdit {
                        id: resumeText
                        anchors.fill: parent
                        anchors.margins: 4
                        text: historyRow.modelData.resumeCommand || ""
                        textFormat: TextEdit.PlainText
                        readOnly: true
                        selectByMouse: true
                        wrapMode: TextEdit.NoWrap
                        clip: true
                        color: Kirigami.Theme.textColor
                        selectionColor: Kirigami.Theme.highlightColor
                        selectedTextColor: Kirigami.Theme.highlightedTextColor
                        font.family: "monospace"
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    }
                }

                PC3.ToolButton {
                    text: "Copy resume command"
                    icon.name: "edit-copy"
                    display: QQC2.AbstractButton.TextBesideIcon
                    onClicked: {
                        resumeText.selectAll()
                        resumeText.copy()
                    }
                }

                // Only kitty rows launch; others keep the copy action.
                PC3.ToolButton {
                    readonly property bool allowed: root.historyLaunchAllowed(
                        history.agentsView.agentKey(historyRow.modelData))
                    visible: historyRow.modelData.host === "kitty"
                    enabled: allowed
                    text: allowed ? "Launch" : "Launched"
                    icon.name: "media-playback-start"
                    display: QQC2.AbstractButton.TextBesideIcon
                    onClicked: root.launchHistory(historyRow.modelData)
                }
            }

            PC3.Label {
                visible: (historyRow.modelData.resumeCommand || "").length === 0
                text: "Resume command unavailable for this record."
                textFormat: Text.PlainText
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                opacity: 0.65
                Layout.fillWidth: true
            }
        }
    }
}
