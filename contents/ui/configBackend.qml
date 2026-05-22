import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    id: form

    property string cfg_cliPath: "~/.local/bin/codexbar"
    property int    cfg_refreshSeconds: 30

    QQC2.TextField {
        Kirigami.FormData.label: "codexbar CLI:"
        text: cfg_cliPath
        onTextChanged: cfg_cliPath = text
        placeholderText: "~/.local/bin/codexbar"
        Layout.fillWidth: true
    }

    QQC2.ComboBox {
        Kirigami.FormData.label: "Refresh every:"
        model: [
            { text: "10 seconds",  value: 10 },
            { text: "30 seconds",  value: 30 },
            { text: "1 minute",    value: 60 },
            { text: "5 minutes",   value: 300 },
            { text: "15 minutes",  value: 900 }
        ]
        textRole: "text"
        currentIndex: {
            var v = cfg_refreshSeconds
            for (var i = 0; i < model.length; i++) if (model[i].value === v) return i
            return 1
        }
        onActivated: cfg_refreshSeconds = model[currentIndex].value
    }

    QQC2.Label {
        text: "Path to the codexbar CLI binary that the widget calls to fetch provider usage."
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
        opacity: 0.55
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
    }
}
