import QtQuick
import QtQuick.Layouts
import QtQuick.Controls as QQC2
import org.kde.plasma.plasmoid
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

Item {
    id: full

    implicitWidth: Kirigami.Units.gridUnit * 24
    // Per-window rows are ~24px after the compact one-row layout, so 4 providers
    // with up to 3 windows each fits comfortably under the tray's clamp.
    implicitHeight: 460

    Layout.preferredWidth: implicitWidth
    Layout.preferredHeight: implicitHeight
    Layout.minimumWidth: Kirigami.Units.gridUnit * 20
    Layout.minimumHeight: 280
    Layout.maximumHeight: Math.min(900, Screen.desktopAvailableHeight * 0.85)

    // Header sits outside the scroll area so it stays pinned.
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
                onClicked: root.refresh()
                PC3.ToolTip.visible: hovered
                PC3.ToolTip.text: "Refresh now"
            }
        }

        Kirigami.Separator { Layout.fillWidth: true }

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

        PC3.Label {
            Layout.fillWidth: true
            wrapMode: Text.WordWrap
            opacity: 0.7
            visible: (root.snapshot.providers || []).length === 0
                && !root.lastError && !root.snapshot.fatal
            text: root.loading ? "Loading…" : "No providers enabled. Open Settings to enable some."
        }
    }

    QQC2.ScrollView {
        id: scrollContainer
        anchors.top: headerWrap.bottom
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        anchors.leftMargin: Kirigami.Units.largeSpacing
        anchors.rightMargin: Kirigami.Units.largeSpacing
        anchors.bottomMargin: Kirigami.Units.largeSpacing
        anchors.topMargin: Kirigami.Units.smallSpacing
        contentWidth: availableWidth
        clip: true

        // Force scroll to top whenever the popup re-opens or data refreshes,
        // so the first provider's header is never hidden behind a scroll position.
        Connections {
            target: root
            function onExpandedChanged() {
                if (root.expanded && scrollContainer.contentItem) {
                    scrollContainer.contentItem.contentY = 0
                }
            }
        }

        ColumnLayout {
            id: content
            width: scrollContainer.availableWidth
            spacing: Kirigami.Units.smallSpacing

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
}
