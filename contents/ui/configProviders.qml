import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    id: form

    property bool cfg_enableClaude: true
    property bool cfg_enableCodex: true
    property bool cfg_enableZai: true
    property bool cfg_enableOpenRouter: true
    property bool cfg_enableKilo: true

    QQC2.CheckBox {
        Kirigami.FormData.label: "Claude:"
        text: "CLI"
        checked: cfg_enableClaude
        onToggled: cfg_enableClaude = checked
    }
    QQC2.CheckBox {
        Kirigami.FormData.label: "Codex:"
        text: "OAuth / CLI"
        checked: cfg_enableCodex
        onToggled: cfg_enableCodex = checked
    }
    QQC2.CheckBox {
        Kirigami.FormData.label: "z.ai:"
        text: "API (subscription)"
        checked: cfg_enableZai
        onToggled: cfg_enableZai = checked
    }
    QQC2.CheckBox {
        Kirigami.FormData.label: "OpenRouter:"
        text: "API"
        checked: cfg_enableOpenRouter
        onToggled: cfg_enableOpenRouter = checked
    }
    QQC2.CheckBox {
        Kirigami.FormData.label: "Kilo:"
        text: "API"
        checked: cfg_enableKilo
        onToggled: cfg_enableKilo = checked
    }

    QQC2.Label {
        text: "Disabled providers are skipped during the polling refresh and hidden from the popup, tray, and tooltip."
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
        opacity: 0.55
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
    }
}
