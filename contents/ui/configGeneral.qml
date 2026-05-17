import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Kirigami.FormLayout {
    property string cfg_cliPath: "~/.local/bin/codexbar"
    property int    cfg_refreshSeconds: 30
    property bool   cfg_enableCodex: true
    property bool   cfg_enableClaude: true
    property bool   cfg_enableOpenRouter: true
    property bool   cfg_enableKilo: true
    property bool   cfg_enableZai: true
    property int    cfg_compactStyle: 0
    property var    cfg_trayIndicators: []
    property int    cfg_trayIconSize: 22

    Kirigami.Separator {
        Kirigami.FormData.label: "Backend"
        Kirigami.FormData.isSection: true
    }

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

    Kirigami.Separator {
        Kirigami.FormData.label: "Providers"
        Kirigami.FormData.isSection: true
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

    Kirigami.Separator {
        Kirigami.FormData.label: "Tray indicators"
        Kirigami.FormData.isSection: true
    }

    QQC2.Label {
        text: "Pick which (provider, window) meters to render as separate tray indicators."
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
        opacity: 0.7
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
    }

    function _has(key) {
        return cfg_trayIndicators && cfg_trayIndicators.indexOf(key) >= 0
    }
    function _toggle(key, on) {
        var current = cfg_trayIndicators ? cfg_trayIndicators.slice() : []
        var idx = current.indexOf(key)
        if (on && idx < 0) current.push(key)
        if (!on && idx >= 0) current.splice(idx, 1)
        cfg_trayIndicators = current
    }

    QQC2.CheckBox {
        Kirigami.FormData.label: "Claude:"
        text: "5h session"
        checked: _has("claude:primary")
        onToggled: _toggle("claude:primary", checked)
    }
    QQC2.CheckBox {
        text: "7d weekly (all models)"
        checked: _has("claude:secondary")
        onToggled: _toggle("claude:secondary", checked)
    }
    QQC2.CheckBox {
        text: "7d weekly (Sonnet)"
        checked: _has("claude:tertiary")
        onToggled: _toggle("claude:tertiary", checked)
    }
    QQC2.CheckBox {
        text: "Claude Design"
        checked: _has("claude:claude-design")
        onToggled: _toggle("claude:claude-design", checked)
    }
    QQC2.CheckBox {
        text: "Daily Routines"
        checked: _has("claude:claude-routines")
        onToggled: _toggle("claude:claude-routines", checked)
    }
    QQC2.CheckBox {
        Kirigami.FormData.label: "Codex:"
        text: "5h session"
        checked: _has("codex:primary")
        onToggled: _toggle("codex:primary", checked)
    }
    QQC2.CheckBox {
        text: "7d weekly"
        checked: _has("codex:secondary")
        onToggled: _toggle("codex:secondary", checked)
    }
    QQC2.CheckBox {
        Kirigami.FormData.label: "z.ai:"
        text: "5h window"
        checked: _has("zai:primary")
        onToggled: _toggle("zai:primary", checked)
    }
    QQC2.CheckBox {
        text: "Monthly"
        checked: _has("zai:secondary")
        onToggled: _toggle("zai:secondary", checked)
    }
    QQC2.CheckBox {
        Kirigami.FormData.label: "OpenRouter:"
        text: "credit usage"
        checked: _has("openrouter:primary")
        onToggled: _toggle("openrouter:primary", checked)
    }
    QQC2.CheckBox {
        Kirigami.FormData.label: "Kilo:"
        text: "credit usage"
        checked: _has("kilo:primary")
        onToggled: _toggle("kilo:primary", checked)
    }

    QQC2.ComboBox {
        Kirigami.FormData.label: "Indicator style:"
        model: [
            { text: "Ring + percent", value: 0 },
            { text: "Ring only",      value: 1 },
            { text: "Percent only",   value: 2 }
        ]
        textRole: "text"
        currentIndex: {
            var v = cfg_compactStyle
            for (var i = 0; i < model.length; i++) if (model[i].value === v) return i
            return 0
        }
        onActivated: cfg_compactStyle = model[currentIndex].value
    }

    RowLayout {
        Kirigami.FormData.label: "Icon + ring size:"
        spacing: Kirigami.Units.smallSpacing
        Layout.fillWidth: true

        QQC2.Slider {
            id: sizeSlider
            from: 14
            to: 48
            stepSize: 2
            snapMode: QQC2.Slider.SnapAlways
            value: cfg_trayIconSize
            Layout.fillWidth: true
            onMoved: cfg_trayIconSize = Math.round(value)
        }
        QQC2.Label {
            text: cfg_trayIconSize + " px"
            Layout.minimumWidth: Kirigami.Units.gridUnit * 3
            horizontalAlignment: Text.AlignRight
            opacity: 0.7
        }
    }

    QQC2.Label {
        text: "Capped automatically by your panel thickness."
        wrapMode: Text.WordWrap
        Layout.fillWidth: true
        opacity: 0.55
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
    }
}
