import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    id: form

    property bool cfg_enableClaude: true
    property bool cfg_enableCodex: true
    property bool cfg_showCodexResetForecast: true
    property bool cfg_showComposite5h: false
    property bool cfg_showComposite7d: true
    property bool cfg_enableZai: true
    property bool cfg_enableOpenCodeGo: true
    property bool cfg_enableOpenRouter: true
    property bool cfg_enableKilo: true
    property bool cfg_enableTypeSafe: false
    property bool cfg_usageNotifications: true
    property bool cfg_ompModeNotifications: true

    QQC2.CheckBox {
        Kirigami.FormData.label: "Notifications:"
        text: "Warn about usage limits"
        checked: cfg_usageNotifications
        onToggled: cfg_usageNotifications = checked
    }
    QQC2.Label {
        text: "Desktop alerts at 80% and 95%, or when the current pace projects exhaustion before reset. Requires notify-send (libnotify)."
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
        opacity: 0.55
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
    }
    QQC2.CheckBox {
        text: "Notify when omp mode changes"
        checked: cfg_ompModeNotifications
        onToggled: cfg_ompModeNotifications = checked
    }
    QQC2.Label {
        text: "Shows the previous and new mode when omp status is available. The first reading stays silent. Requires notify-send (libnotify)."
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
        opacity: 0.55
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
    }

    RowLayout {
        Kirigami.FormData.label: "Codex combined bars:"
        QQC2.CheckBox {
            text: "5h"
            checked: cfg_showComposite5h
            onToggled: cfg_showComposite5h = checked
        }
        QQC2.CheckBox {
            text: "7d"
            checked: cfg_showComposite7d
            onToggled: cfg_showComposite7d = checked
        }
    }
    QQC2.Label {
        text: "With two or more Codex accounts, a combined bar above the per-account sections pools their allowances, weighting Pro as 20× Plus."
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
        opacity: 0.55
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
    }
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
        Kirigami.FormData.label: "Codex forecast:"
        text: "Show reset forecast"
        checked: cfg_showCodexResetForecast
        onToggled: cfg_showCodexResetForecast = checked
    }
    QQC2.CheckBox {
        Kirigami.FormData.label: "z.ai:"
        text: "API (subscription)"
        checked: cfg_enableZai
        onToggled: cfg_enableZai = checked
    }
    QQC2.CheckBox {
        Kirigami.FormData.label: "OpenCode Go:"
        text: "Local usage history"
        checked: cfg_enableOpenCodeGo
        onToggled: cfg_enableOpenCodeGo = checked
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
    QQC2.CheckBox {
        Kirigami.FormData.label: "TypeSafe:"
        text: "Console cookie (manual)"
        checked: cfg_enableTypeSafe
        onToggled: cfg_enableTypeSafe = checked
    }

    QQC2.Label {
        text: "Disabled providers are skipped during the polling refresh and hidden from the popup, tray, and tooltip."
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
        opacity: 0.55
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
    }
}
