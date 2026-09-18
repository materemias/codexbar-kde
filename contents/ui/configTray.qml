import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasma5support as P5Support
import "Command.js" as Command

Item {
    id: root

    property var cfg_trayIndicators: []
    property var cfg_trayOverPaceOnly: []
    property int cfg_compactStyle: 0
    property int cfg_trayIconSize: 22
    property string cfg_cliPath: "/usr/bin/codexbar"

    property var codexAccounts: []
    property bool codexLoading: true
    property string codexError: ""
    property bool initialized: false
    property string activeCommand: ""
    property string activeCliPath: ""
    readonly property string cliPath: Command.cliPath(cfg_cliPath)
    onCliPathChanged: {
        if (!initialized) return
        codexAccounts = []
        codexError = ""
        codexLoading = true
        loadCodexAccounts()
    }
    readonly property bool codexReady:
        !codexLoading && codexError.length === 0 && codexAccounts.length > 0
    readonly property string fetchScriptPath: Command.localPath(
        Qt.resolvedUrl("../scripts/codexbar_fetch.py"))
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
    function _overPaceOnly(key) {
        return cfg_trayOverPaceOnly && cfg_trayOverPaceOnly.indexOf(key) >= 0
    }
    function _toggleOverPaceOnly(key, on) {
        var current = cfg_trayOverPaceOnly ? cfg_trayOverPaceOnly.slice() : []
        var idx = current.indexOf(key)
        if (on && idx < 0) current.push(key)
        if (!on && idx >= 0) current.splice(idx, 1)
        cfg_trayOverPaceOnly = current
    }

    // One tray meter: the main on/off box plus, while on, a secondary box
    // that hides the meter until its projected usage at reset passes 100%.
    component MeterCheck: RowLayout {
        id: meter
        property string key
        property string text
        property bool isOn: root._has(key)
        property var setOn: function (on) { root._toggle(meter.key, on) }
        spacing: Kirigami.Units.smallSpacing

        QQC2.CheckBox {
            text: meter.text
            checked: meter.isOn
            onToggled: meter.setOn(checked)
        }
        QQC2.CheckBox {
            visible: meter.isOn
            text: "only if proj > 100%"
            opacity: 0.7
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            checked: root._overPaceOnly(meter.key)
            onToggled: root._toggleOverPaceOnly(meter.key, checked)
            QQC2.ToolTip.text: "Hide this meter from the tray unless the current rate projects above 100% at reset"
            QQC2.ToolTip.visible: hovered
            QQC2.ToolTip.delay: Kirigami.Units.toolTipDelay
        }
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
            if (sourceName !== root.activeCommand) return
            root.activeCommand = ""
            if (root.activeCliPath !== root.cliPath) {
                root.loadCodexAccounts()
                return
            }
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

    function loadCodexAccounts() {
        if (root.activeCommand) return
        root.codexLoading = true
        root.activeCliPath = root.cliPath
        root.activeCommand = "python3 " + Command.shellQuote(root.fetchScriptPath)
            + " --cli-path " + Command.shellQuote(root.activeCliPath)
            + " --providers codex --timeout 30 # t=" + Date.now()
        codexRunner.connectSource(root.activeCommand)
    }

    Component.onCompleted: {
        root.initialized = true
        root.loadCodexAccounts()
    }

    QQC2.ScrollView {
        id: scroller
        anchors.fill: parent
        contentWidth: availableWidth
        clip: true

        Kirigami.FormLayout {
            width: scroller.availableWidth

            QQC2.Label {
                text: "Pick which (provider, window) meters to render as separate tray indicators. "
                    + "\"only if proj > 100%\" keeps a meter hidden while it is on pace to last until reset."
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                opacity: 0.7
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            }

            MeterCheck {
                Kirigami.FormData.label: "Claude:"
                key: "claude:primary"
                text: "5h session"
            }
            MeterCheck { key: "claude:secondary"; text: "7d weekly (all models)" }
            MeterCheck { key: "claude:tertiary"; text: "7d weekly (Sonnet)" }
            MeterCheck { key: "claude:claude-design"; text: "Claude Design" }
            MeterCheck { key: "claude:claude-routines"; text: "Daily Routines" }
            ColumnLayout {
                Kirigami.FormData.label: "Codex:"
                visible: !root.codexReady
                spacing: 0

                Repeater {
                    model: root._fallbackCodexWindows()
                    delegate: MeterCheck {
                        id: fallbackWindow
                        required property var modelData
                        key: "codex:" + fallbackWindow.modelData.id
                        text: fallbackWindow.modelData.text
                        isOn: root._codexAllChecked(fallbackWindow.modelData.id)
                        setOn: function (on) {
                            root._toggleCodexAll(fallbackWindow.modelData.id, on)
                        }
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
                            delegate: MeterCheck {
                                id: accountWindow
                                required property var modelData
                                key: root._codexKey(accountGroup.modelData, accountWindow.modelData.id)
                                text: accountWindow.modelData.text
                                isOn: root._codexChecked(
                                    accountGroup.modelData, accountWindow.modelData.id)
                                setOn: function (on) {
                                    root._toggleCodex(
                                        accountGroup.modelData, accountWindow.modelData.id, on)
                                }
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
            MeterCheck {
                Kirigami.FormData.label: "z.ai:"
                key: "zai:primary"
                text: "5h window"
            }
            MeterCheck { key: "zai:secondary"; text: "Monthly" }
            MeterCheck {
                Kirigami.FormData.label: "OpenCode Go:"
                key: "opencodego:primary"
                text: "5h window"
            }
            MeterCheck { key: "opencodego:secondary"; text: "7d weekly" }
            MeterCheck { key: "opencodego:tertiary"; text: "Monthly" }
            MeterCheck {
                Kirigami.FormData.label: "OpenRouter:"
                key: "openrouter:primary"
                text: "credit usage"
            }
            MeterCheck {
                Kirigami.FormData.label: "Kilo:"
                key: "kilo:primary"
                text: "credit usage"
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
