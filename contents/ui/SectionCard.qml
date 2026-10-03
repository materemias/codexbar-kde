import QtQuick
import QtQuick.Layouts
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

// Rounded card with an optional small uppercase header. Agents folders,
// History sections and Usage providers share it. Children go into the body
// column; `headerData` items sit at the header's right end.
Rectangle {
    id: card

    property string title: ""
    property int contentMargins: 4
    property alias bodySpacing: body.spacing
    property alias headerData: trailing.data
    default property alias contentData: body.data

    Layout.fillWidth: true
    // With a parent column's smallSpacing, cards sit 10 px apart.
    Layout.topMargin: 6
    implicitHeight: column.implicitHeight + contentMargins + 6
    radius: 6
    color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g,
        Kirigami.Theme.textColor.b, 0.035)
    border.width: 1
    border.color: Qt.rgba(Kirigami.Theme.textColor.r, Kirigami.Theme.textColor.g,
        Kirigami.Theme.textColor.b, 0.08)

    ColumnLayout {
        id: column
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.margins: card.contentMargins
        anchors.topMargin: 6
        spacing: 0

        RowLayout {
            id: header
            visible: card.title.length > 0
            Layout.fillWidth: true
            Layout.leftMargin: 6
            Layout.bottomMargin: 3
            spacing: Kirigami.Units.smallSpacing

            PC3.Label {
                text: card.title
                textFormat: Text.PlainText
                font.capitalization: Font.AllUppercase
                font.weight: Font.DemiBold
                font.letterSpacing: 0.8
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize * 0.92
                opacity: 0.55
                elide: Text.ElideRight
                Layout.fillWidth: true
                Layout.alignment: Qt.AlignVCenter
            }

            RowLayout {
                id: trailing
                spacing: 0
                Layout.alignment: Qt.AlignVCenter
            }
        }

        ColumnLayout {
            id: body
            Layout.fillWidth: true
            spacing: 1
        }
    }
}
