import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami

Item {
    id: root

    property var cfg_trayIndicators: []
    property int cfg_compactStyle: 0
    property int cfg_trayIconSize: 22

    implicitWidth: Kirigami.Units.gridUnit * 22
    implicitHeight: Kirigami.Units.gridUnit * 22

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

    QQC2.ScrollView {
        id: scroller
        anchors.fill: parent
        contentWidth: availableWidth
        clip: true

        Kirigami.FormLayout {
            width: scroller.availableWidth

            QQC2.Label {
                text: "Pick which (provider, window) meters to render as separate tray indicators."
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                opacity: 0.7
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            }

            QQC2.CheckBox {
                Kirigami.FormData.label: "Claude:"
                text: "5h session"
                checked: root._has("claude:primary")
                onToggled: root._toggle("claude:primary", checked)
            }
            QQC2.CheckBox {
                text: "7d weekly (all models)"
                checked: root._has("claude:secondary")
                onToggled: root._toggle("claude:secondary", checked)
            }
            QQC2.CheckBox {
                text: "7d weekly (Sonnet)"
                checked: root._has("claude:tertiary")
                onToggled: root._toggle("claude:tertiary", checked)
            }
            QQC2.CheckBox {
                text: "Claude Design"
                checked: root._has("claude:claude-design")
                onToggled: root._toggle("claude:claude-design", checked)
            }
            QQC2.CheckBox {
                text: "Daily Routines"
                checked: root._has("claude:claude-routines")
                onToggled: root._toggle("claude:claude-routines", checked)
            }
            QQC2.CheckBox {
                Kirigami.FormData.label: "Codex:"
                text: "5h session"
                checked: root._has("codex:primary")
                onToggled: root._toggle("codex:primary", checked)
            }
            QQC2.CheckBox {
                text: "7d weekly"
                checked: root._has("codex:secondary")
                onToggled: root._toggle("codex:secondary", checked)
            }
            QQC2.CheckBox {
                Kirigami.FormData.label: "z.ai:"
                text: "5h window"
                checked: root._has("zai:primary")
                onToggled: root._toggle("zai:primary", checked)
            }
            QQC2.CheckBox {
                text: "Monthly"
                checked: root._has("zai:secondary")
                onToggled: root._toggle("zai:secondary", checked)
            }
            QQC2.CheckBox {
                Kirigami.FormData.label: "OpenRouter:"
                text: "credit usage"
                checked: root._has("openrouter:primary")
                onToggled: root._toggle("openrouter:primary", checked)
            }
            QQC2.CheckBox {
                Kirigami.FormData.label: "Kilo:"
                text: "credit usage"
                checked: root._has("kilo:primary")
                onToggled: root._toggle("kilo:primary", checked)
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
    }
}
