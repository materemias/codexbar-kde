import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    id: form

    property string cfg_cliPath: "/usr/bin/codexbar"
    property int    cfg_refreshSeconds: 30

    QQC2.TextField {
        objectName: "cliPathField"
        Kirigami.FormData.label: "codexbar CLI:"
        text: cfg_cliPath
        onTextChanged: cfg_cliPath = text
        placeholderText: "/usr/bin/codexbar"
        Layout.fillWidth: true
    }

    RowLayout {
        Kirigami.FormData.label: "Refresh every:"
        QQC2.SpinBox {
            objectName: "usageRefreshSpinBox"
            from: 10
            to: 3600
            editable: true
            value: cfg_refreshSeconds
            onValueModified: cfg_refreshSeconds = value
        }
        QQC2.Label { text: "seconds" }
    }

    QQC2.Label {
        text: "Path to the codexbar CLI binary that the widget calls to fetch provider usage."
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
        opacity: 0.55
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
    }
}
