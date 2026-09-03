import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasma5support as P5Support

Item {
    id: root

    property var cfg_trayIndicators: []
    property int cfg_compactStyle: 0
    property int cfg_trayIconSize: 22
    property string cfg_cliPath: "~/.local/bin/codexbar"

    property var codexAccounts: []
    property bool codexLoading: true
    property string codexError: ""
    readonly property bool codexReady:
        !codexLoading && codexError.length === 0 && codexAccounts.length > 0
    readonly property string fetchScriptPath: {
        var url = Qt.resolvedUrl("../scripts/codexbar_fetch.py").toString()
        return url.replace(/^file:\/\//, "")
    }
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

    function _codexWindowLabel(id, fallback) {
        if (id === "codex-base-model-inference" || id === "gpt-reserve") {
            return "Reserve 7d"
        }
        return fallback || id
    }

    function _codexWindows(account) {
        var rows = []
        var slots = [
            { id: "primary", text: "5h session" },
            { id: "secondary", text: "7d weekly" },
            { id: "tertiary", text: "Additional limit" }
        ]
        for (var i = 0; i < slots.length; i++) {
            var window = account[slots[i].id]
            if (window && window.usedPercent !== undefined && window.usedPercent !== null) {
                rows.push(slots[i])
            }
        }
        var extras = account.extraRateWindows || []
        for (var e = 0; e < extras.length; e++) {
            if (!extras[e] || !extras[e].id || !extras[e].window) continue
            rows.push({ id: extras[e].id, text: _codexWindowLabel(extras[e].id, extras[e].title) })
        }
        return rows
    }

    function _codexKey(account, slot) {
        return "codex:" + encodeURIComponent(String(account.accountEmail)) + ":" + slot
    }

    function _codexChecked(account, slot) {
        return _has(_codexKey(account, slot)) || _has("codex:" + slot)
    }
    function _fallbackCodexWindows() {
        var rows = [
            { id: "primary", text: "5h session" },
            { id: "secondary", text: "7d weekly" }
        ]
        var seen = { primary: true, secondary: true }
        var configured = cfg_trayIndicators || []
        for (var i = 0; i < configured.length; i++) {
            var parts = String(configured[i]).split(":")
            if (parts[0] !== "codex" || parts.length < 2) continue
            var slot = parts.length > 2 ? parts.slice(2).join(":") : parts[1]
            if (!slot || seen[slot]) continue
            rows.push({
                id: slot,
                text: _codexWindowLabel(slot, slot)
            })
            seen[slot] = true
        }
        return rows
    }

    function _codexAllChecked(slot) {
        var configured = cfg_trayIndicators || []
        for (var i = 0; i < configured.length; i++) {
            var parts = String(configured[i]).split(":")
            if (parts[0] !== "codex") continue
            var configuredSlot = parts.length > 2
                ? parts.slice(2).join(":") : parts[1]
            if (configuredSlot === slot) return true
        }
        return false
    }

    function _toggleCodexAll(slot, on) {
        var current = cfg_trayIndicators ? cfg_trayIndicators.slice() : []
        for (var i = current.length - 1; i >= 0; i--) {
            var parts = String(current[i]).split(":")
            if (parts[0] !== "codex") continue
            var configuredSlot = parts.length > 2
                ? parts.slice(2).join(":") : parts[1]
            if (configuredSlot === slot) current.splice(i, 1)
        }
        if (on) current.push("codex:" + slot)
        cfg_trayIndicators = current
    }


    function _toggleCodex(account, slot, on) {
        var current = cfg_trayIndicators ? cfg_trayIndicators.slice() : []
        var legacy = "codex:" + slot
        var legacyIndex = current.indexOf(legacy)
        if (legacyIndex >= 0) {
            current.splice(legacyIndex, 1)
            for (var i = 0; i < codexAccounts.length; i++) {
                var windows = _codexWindows(codexAccounts[i])
                for (var w = 0; w < windows.length; w++) {
                    if (windows[w].id !== slot) continue
                    var migrated = _codexKey(codexAccounts[i], slot)
                    if (current.indexOf(migrated) < 0) current.push(migrated)
                    break
                }
            }
        }
        var key = _codexKey(account, slot)
        var index = current.indexOf(key)
        if (on && index < 0) current.push(key)
        if (!on && index >= 0) current.splice(index, 1)
        cfg_trayIndicators = current
    }

    P5Support.DataSource {
        id: codexRunner
        engine: "executable"
        connectedSources: []
        onNewData: function(sourceName, data) {
            disconnectSource(sourceName)
            root.codexLoading = false
            var stdout = (data["stdout"] || "").trim()
            if (!stdout) {
                root.codexAccounts = []
                root.codexError = (data["stderr"] || "No Codex account data").trim()
                return
            }
            try {
                var parsed = JSON.parse(stdout)
                if (parsed.fatal) {
                    root.codexAccounts = []
                    root.codexError = parsed.fatal.message || "Could not fetch Codex accounts"
                    return
                }
                var records = (parsed.providers || []).filter(function(record) {
                    return record && record.id === "codex"
                })
                root.codexAccounts = records.filter(function(record) {
                    return record.ok && record.accountEmail
                })
                var failed = records.filter(function(record) { return !record.ok })
                if (failed.length > 0) {
                    var error = failed[0].error || {}
                    root.codexError = error.message || "Some Codex accounts could not be loaded"
                } else {
                    root.codexError = root.codexAccounts.length > 0
                        ? "" : "No Codex accounts found"
                }
            } catch (err) {
                root.codexAccounts = []
                root.codexError = "Could not read Codex accounts: " + err.message
            }
        }
    }

    Component.onCompleted: {
        var cmd = "python3 \"" + root.fetchScriptPath + "\""
            + " --cli-path \"" + root.cfg_cliPath
            + "\" --providers codex --timeout 10"
        codexRunner.connectSource(cmd)
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
            ColumnLayout {
                Kirigami.FormData.label: "Codex:"
                visible: !root.codexReady
                spacing: 0

                Repeater {
                    model: root._fallbackCodexWindows()
                    delegate: QQC2.CheckBox {
                        id: fallbackWindow
                        required property var modelData
                        text: fallbackWindow.modelData.text
                        checked: root._codexAllChecked(fallbackWindow.modelData.id)
                        onToggled: root._toggleCodexAll(
                            fallbackWindow.modelData.id, checked)
                    }
                }
            }

            ColumnLayout {
                Kirigami.FormData.label: "Codex:"
                visible: root.codexReady
                spacing: Kirigami.Units.smallSpacing

                Repeater {
                    model: root.codexAccounts
                    delegate: ColumnLayout {
                        id: accountGroup
                        required property var modelData
                        spacing: 0

                        QQC2.Label {
                            text: accountGroup.modelData.accountEmail
                                + (accountGroup.modelData.loginMethod
                                   ? " · " + accountGroup.modelData.loginMethod : "")
                            font.weight: Font.DemiBold
                            opacity: 0.8
                        }

                        Repeater {
                            model: root._codexWindows(accountGroup.modelData)
                            delegate: QQC2.CheckBox {
                                id: accountWindow
                                required property var modelData
                                text: accountWindow.modelData.text
                                checked: root._codexChecked(
                                    accountGroup.modelData, accountWindow.modelData.id)
                                onToggled: root._toggleCodex(
                                    accountGroup.modelData, accountWindow.modelData.id, checked)
                            }
                        }
                    }
                }
            }

            QQC2.Label {
                Kirigami.FormData.label: "Codex status:"
                visible: root.codexLoading || root.codexError.length > 0
                text: root.codexLoading ? "Loading accounts…" : root.codexError
                color: root.codexError.length > 0
                    ? Kirigami.Theme.negativeTextColor : Kirigami.Theme.textColor
                opacity: root.codexError.length > 0 ? 1.0 : 0.6
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
