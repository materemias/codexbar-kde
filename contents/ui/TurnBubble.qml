import QtQuick
import QtQuick.Layouts
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

// One conversation turn in a peek, laid out like a chat: your turns in a
// tinted bubble on the right, the agent's as plain text, tool runs as one
// muted line. Agents and History peeks share it; place it in a ColumnLayout.
Rectangle {
    id: bubble

    required property var modelData
    property int maxLines: 4
    readonly property bool toolTurn: modelData.kind === "tools"
    readonly property bool userTurn: modelData.role === "user"
    readonly property int padX: userTurn ? 8 : 0
    readonly property int padY: userTurn ? 4 : 0
    readonly property real columnWidth: parent ? parent.width : 0

    Layout.fillWidth: !userTurn
    Layout.alignment: userTurn ? Qt.AlignRight : Qt.AlignLeft
    Layout.preferredWidth: userTurn ? turnText.implicitWidth + 2 * padX : -1
    Layout.maximumWidth: userTurn ? columnWidth * 0.85 : columnWidth
    implicitHeight: turnText.implicitHeight + 2 * padY
    radius: 8
    color: userTurn
        ? Qt.rgba(Kirigami.Theme.highlightColor.r, Kirigami.Theme.highlightColor.g,
            Kirigami.Theme.highlightColor.b, 0.22)
        : "transparent"

    PC3.Label {
        id: turnText
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.leftMargin: bubble.padX
        anchors.rightMargin: bubble.padX
        anchors.topMargin: bubble.padY
        text: (bubble.toolTurn ? "Tools: " : "") + (bubble.modelData.text || "")
        textFormat: Text.PlainText
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
        font.italic: bubble.toolTurn
        opacity: bubble.toolTurn ? 0.45 : 0.92
        wrapMode: bubble.toolTurn ? Text.NoWrap : Text.Wrap
        maximumLineCount: bubble.toolTurn ? 1 : bubble.maxLines
        elide: Text.ElideRight
        lineHeight: 1.15
        lineHeightMode: Text.ProportionalHeight
    }
}
