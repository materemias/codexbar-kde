import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

// Ended sessions: those cut off by a reboot, ordered by desktop, then the
// most recent exits of this boot, newest first. Rows have their own
// keyboard selection (Up/Down, Space peeks, Enter launches), separate from
// the live agent selection.
ColumnLayout {
    id: history
    spacing: Kirigami.Units.smallSpacing

    // AgentsSection owns the type-to-filter query and name helpers.
    required property var agentsView

    readonly property var records: {
        var s = root.agentSnapshot
        return s && Array.isArray(s.history) ? s.history : []
    }
    // Render models ignore the filter: typing toggles row visibility
    // instead of rebuilding every delegate on each keystroke. `rebooted`,
    // `exited` and `filtered` hold the matching rows for selection, counts
    // and bulk actions.
    readonly property var rebootedAll: {
        // Desktop number first, then "all", then unknown; newest first
        // within one desktop.
        function desktopRank(record) {
            var d = String(record.desktop || "")
            if (/^[0-9]+$/.test(d)) return Number(d)
            return d === "all" ? 1e9 : 2e9
        }
        var out = history.records.filter(function(r) { return r && r.closedBy === "reboot" })
        out.sort(function(a, b) {
            var aDesk = desktopRank(a)
            var bDesk = desktopRank(b)
            if (aDesk !== bDesk) return aDesk - bDesk
            return history._newestFirst(a, b)
        })
        return out
    }
    readonly property var exitedAll: {
        var out = history.records.filter(function(r) { return r && r.closedBy === "exit" })
        out.sort(history._newestFirst)
        return out
    }
    function _matching(list) {
        return list.filter(function(r) { return history.agentsView.recordMatches(r) })
    }
    readonly property var rebooted: _matching(rebootedAll)
    readonly property var exited: _matching(exitedAll)
    readonly property var filtered: rebooted.concat(exited)

    function _newestFirst(a, b) {
        var aTime = Number(a.lastSeenAt) || 0
        var bTime = Number(b.lastSeenAt) || 0
        if (aTime !== bTime) return bTime - aTime
        var aKey = String(a.provider || "") + "\n" + String(a.sessionId || "")
        var bKey = String(b.provider || "") + "\n" + String(b.sessionId || "")
        return aKey < bKey ? -1 : aKey > bKey ? 1 : 0
    }

    // Display order: restart rows, then recently closed rows.
    readonly property var flatRecords: history.rebooted.concat(history.exited)
    property string selectedKey: ""
    readonly property int selectedIndex: indexForKey(selectedKey)

    function indexForKey(key) {
        if (!key) return -1
        for (var i = 0; i < flatRecords.length; i++) {
            if (history.agentsView.agentKey(flatRecords[i]) === key) return i
        }
        return -1
    }
    function selectAt(index) {
        if (index < 0 || index >= flatRecords.length) return
        var followPeek = history.peekKey !== ""
        selectedKey = history.agentsView.agentKey(flatRecords[index])
        if (followPeek) history.peekKey = selectedKey
    }
    function selectNext() {
        if (flatRecords.length === 0) return
        selectAt((selectedIndex + 1) % flatRecords.length)
    }
    function selectPrevious() {
        if (flatRecords.length === 0) return
        var n = flatRecords.length
        selectAt(((selectedIndex < 0 ? 0 : selectedIndex) - 1 + n) % n)
    }
    function togglePeek() {
        var index = indexForKey(selectedKey)
        if (index < 0) return
        history.peekKey = history.peekKey === selectedKey ? "" : selectedKey
    }
    function activateSelected() {
        var index = indexForKey(selectedKey)
        if (index < 0) return
        var record = flatRecords[index]
        if (!root.launchHost(record.host) || !(record.resumeCommand || "")) {
            root.historyLaunchError = "This session cannot be launched: "
                + (record.resumeCommand ? "it did not run in kitty or Tern." : "its resume command is unknown.")
            return
        }
        root.launchHistory(record)
    }
    onFlatRecordsChanged: {
        if (selectedKey && indexForKey(selectedKey) < 0) selectedKey = ""
        if (peekKey && indexForKey(peekKey) < 0) peekKey = ""
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

    readonly property var restorable: history.rebooted.filter(function(r) {
        return root.launchHost(r.host) && (r.resumeCommand || "").length > 0
            && root.historyLaunchAllowed(history.agentsView.agentKey(r))
    })

    RowLayout {
        visible: history.rebooted.length > 0
        Layout.fillWidth: true
        spacing: Kirigami.Units.smallSpacing

        PC3.Label {
            text: "Interrupted by restart"
            font.weight: Font.Bold
            font.pixelSize: Kirigami.Theme.defaultFont.pixelSize * 1.02
            Layout.fillWidth: true
        }

        PC3.ToolButton {
            visible: history.restorable.length > 0
            text: "Restore all (" + history.restorable.length + ")"
            icon.name: "media-playback-start"
            display: QQC2.AbstractButton.TextBesideIcon
            onClicked: root.launchAllHistory(history.rebooted)
            PC3.ToolTip.visible: hovered
            PC3.ToolTip.text: "Resume every kitty and Tern session, desktop by desktop"
            PC3.ToolTip.delay: 400
        }

        PC3.ToolButton {
            text: "Dismiss all"
            icon.name: "edit-clear-history"
            display: QQC2.AbstractButton.TextBesideIcon
            onClicked: root.dismissHistory(history.rebooted)
        }
    }

    ColumnLayout {
        Layout.fillWidth: true
        spacing: 1

        Repeater {
            model: history.rebootedAll
            delegate: EndedRow {}
        }
    }

    PC3.Label {
        visible: history.exited.length > 0
        text: "Recently closed"
        font.weight: Font.Bold
        font.pixelSize: Kirigami.Theme.defaultFont.pixelSize * 1.02
        Layout.topMargin: history.rebooted.length > 0 ? Kirigami.Units.smallSpacing : 0
        Layout.fillWidth: true
    }

    // Ended rows mirror live agent rows: one line per session, with a peek
    // panel for its last turns and resume command.
    property string peekKey: ""

    ColumnLayout {
        Layout.fillWidth: true
        spacing: 1

        Repeater {
            model: history.exitedAll
            delegate: EndedRow {}
        }
    }

    component EndedRow: Item {
        id: closedRow
        required property var modelData
        visible: history.agentsView.recordMatches(modelData)
        Layout.fillWidth: true
        Layout.leftMargin: 6
        implicitHeight: closedCol.implicitHeight + 4

        readonly property string sessionKey: history.agentsView.agentKey(modelData)
        readonly property bool peekOpen: sessionKey !== "" && sessionKey === history.peekKey
        readonly property string command: modelData.resumeCommand || ""
        readonly property bool canLaunch: command.length > 0 && root.launchHost(modelData.host)
        readonly property bool launchAllowed: root.historyLaunchAllowed(sessionKey)
        readonly property bool selected: sessionKey !== "" && sessionKey === history.selectedKey
        readonly property color tint: Kirigami.Theme.disabledTextColor
        readonly property string taskLabel:
            modelData[history.agentsView.labelField(modelData)] || "agent"

        Rectangle {
            z: -1
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: closedLine.implicitHeight + 4
            radius: 4
            color: Kirigami.Theme.alternateBackgroundColor
            opacity: closedRow.selected ? 0.1
                : closedMouse.containsMouse || closedRow.peekOpen ? 0.06 : 0
            Behavior on opacity { NumberAnimation { duration: 120 } }
        }

        Rectangle {
            anchors.left: parent.left
            anchors.top: parent.top
            width: 3
            height: closedLine.implicitHeight + 4
            radius: 1
            color: Kirigami.Theme.highlightColor
            opacity: closedRow.selected ? 1 : 0
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
            onClicked: {
                history.selectedKey = closedRow.sessionKey
                history.peekKey = closedRow.peekOpen ? "" : closedRow.sessionKey
            }
        }

        ColumnLayout {
            id: closedCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            spacing: 0

            // Right-hand fields sit in fixed-width columns so they line up
            // across rows; the launch slot ends at the row's edge.
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
                    id: closedTitle
                    text: history.agentsView.highlighted(closedRow.taskLabel)
                    textFormat: history.agentsView.filterText ? Text.StyledText : Text.PlainText
                    elide: Text.ElideRight
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignVCenter
                }

                PC3.Label {
                    id: closedModel
                    text: history.agentsView.highlighted(
                        history.agentsView._modelName(closedRow.modelData.model))
                    textFormat: history.agentsView.filterText ? Text.StyledText : Text.PlainText
                    color: Kirigami.Theme.linkColor
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    font.weight: Font.DemiBold
                    elide: Text.ElideMiddle
                    horizontalAlignment: Text.AlignRight
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 5
                    Layout.alignment: Qt.AlignVCenter
                }

                PC3.Label {
                    text: root.cwdLabel(closedRow.modelData.cwd || "")
                    textFormat: Text.PlainText
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    opacity: 0.55
                    elide: Text.ElideMiddle
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 4
                    Layout.alignment: Qt.AlignVCenter
                }

                PC3.Label {
                    text: root.ageFrom(Number(closedRow.modelData.lastSeenAt) || 0, root.nowMs)
                    color: closedRow.tint
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    font.weight: Font.DemiBold
                    horizontalAlignment: Text.AlignRight
                    Layout.preferredWidth: Kirigami.Units.gridUnit * 2
                    Layout.alignment: Qt.AlignVCenter
                }

                // Desktop chip; its slot stays when the desktop is unknown.
                Item {
                    implicitWidth: Math.max(Kirigami.Units.gridUnit * 1.4, closedDesktop.width)
                    implicitHeight: closedDesktop.height
                    Layout.alignment: Qt.AlignVCenter

                    Rectangle {
                        id: closedDesktop
                        anchors.right: parent.right
                        visible: (closedRow.modelData.desktop || "").length > 0
                        width: closedDesktopLabel.implicitWidth + 8
                        height: closedDesktopLabel.implicitHeight + 3
                        radius: 3
                        color: "transparent"
                        border.width: 1
                        border.color: Qt.rgba(Kirigami.Theme.textColor.r,
                            Kirigami.Theme.textColor.g,
                            Kirigami.Theme.textColor.b, 0.3)

                        PC3.Label {
                            id: closedDesktopLabel
                            anchors.centerIn: parent
                            text: closedRow.modelData.desktop || ""
                            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                            font.weight: Font.DemiBold
                            opacity: 0.65
                        }
                    }
                }

                // Keeps its slot on rows that cannot launch so columns align.
                PC3.ToolButton {
                    id: launchButton
                    icon.name: "media-playback-start"
                    opacity: closedRow.canLaunch ? 1 : 0
                    enabled: closedRow.canLaunch && closedRow.launchAllowed
                    implicitWidth: Kirigami.Units.iconSizes.smallMedium + 6
                    implicitHeight: Kirigami.Units.iconSizes.smallMedium + 6
                    padding: 1
                    onClicked: root.launchHistory(closedRow.modelData)
                    PC3.ToolTip.visible: hovered && closedRow.canLaunch
                    PC3.ToolTip.text: !closedRow.launchAllowed ? "Launched"
                        : closedRow.modelData.host === "tern" ? "Resume in a new Tern tab"
                        : "Resume in kitty"
                            + (closedRow.modelData.desktop
                                ? " on desktop " + closedRow.modelData.desktop : "")
                    PC3.ToolTip.delay: 400
                }
            }

            SnippetPanel {
                snippets: history.agentsView.filterSnippets(closedRow.modelData,
                    history.agentsView.shownFields([
                        [history.agentsView.labelField(closedRow.modelData), closedTitle],
                        ["model", closedModel]]))
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

        // Peek and dismiss float left of the launch slot on hover, so they
        // take no column space.
        Rectangle {
            readonly property bool shown: closedMouse.containsMouse || closedRow.peekOpen
                || peekButton.hovered || dismissButton.hovered
            anchors.right: parent.right
            anchors.rightMargin: 4 + launchButton.width + Kirigami.Units.smallSpacing
            y: (closedLine.height - height) / 2
            width: hoverButtons.implicitWidth
            height: hoverButtons.implicitHeight
            radius: 4
            color: Kirigami.Theme.backgroundColor
            opacity: shown ? 1 : 0
            visible: opacity > 0
            Behavior on opacity { NumberAnimation { duration: 120 } }

            Row {
                id: hoverButtons

                PC3.ToolButton {
                    id: peekButton
                    icon.name: closedRow.peekOpen ? "arrow-up" : "arrow-down"
                    implicitWidth: Kirigami.Units.iconSizes.smallMedium + 6
                    implicitHeight: Kirigami.Units.iconSizes.smallMedium + 6
                    padding: 1
                    onClicked: history.peekKey = closedRow.peekOpen ? "" : closedRow.sessionKey
                    PC3.ToolTip.visible: hovered
                    PC3.ToolTip.text: "Peek at recent messages and the resume command"
                    PC3.ToolTip.delay: 400
                }

                PC3.ToolButton {
                    id: dismissButton
                    icon.name: "window-close"
                    implicitWidth: Kirigami.Units.iconSizes.smallMedium + 6
                    implicitHeight: Kirigami.Units.iconSizes.smallMedium + 6
                    padding: 1
                    onClicked: root.dismissHistory([closedRow.modelData])
                    PC3.ToolTip.visible: hovered
                    PC3.ToolTip.text: "Dismiss from History"
                    PC3.ToolTip.delay: 400
                }
            }
        }
    }

    // The conversation lines that matched the active filter, query
    // highlighted, as on Agents rows.
    component SnippetPanel: Rectangle {
        id: snippetPanel
        required property var snippets
        visible: snippets.length > 0
        Layout.fillWidth: true
        Layout.leftMargin: 18
        Layout.topMargin: 2
        implicitHeight: snippetCol.implicitHeight + 8
        radius: 4
        color: Kirigami.Theme.backgroundColor
        border.width: 1
        border.color: Qt.rgba(Kirigami.Theme.textColor.r,
            Kirigami.Theme.textColor.g,
            Kirigami.Theme.textColor.b, 0.14)

        ColumnLayout {
            id: snippetCol
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            anchors.margins: 4
            spacing: 1

            Repeater {
                model: snippetPanel.snippets
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

}
