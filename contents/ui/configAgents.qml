import QtQuick
import QtQuick.Controls as QQC2
import QtQuick.Layouts
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasma5support as P5Support

Item {
    id: root

    property bool cfg_showAgents: true
    property bool cfg_agentBlockedBadge: true
    property bool cfg_showAgentPrompts: false
    property bool cfg_includeUntrackedAgents: true
    property int  cfg_agentsRefreshSeconds: 5
    property bool cfg_showAgentStateDots: true
    property int  cfg_agentStateDotsScale: 100
    property bool cfg_closePopupOnFocusLoss: true

    property string hookStatus: "checking…"
    readonly property string installScriptPath: {
        var url = Qt.resolvedUrl("../scripts/install_integration.py").toString()
        return url.replace(/^file:\/\//, "")
    }

    implicitWidth: Kirigami.Units.gridUnit * 22
    implicitHeight: Kirigami.Units.gridUnit * 22

    P5Support.DataSource {
        id: hookRunner
        engine: "executable"
        connectedSources: []
        onNewData: function(sourceName, data) {
            disconnectSource(sourceName)
            var stdout = (data["stdout"] || "").trim()
            var stderr = (data["stderr"] || "").trim()
            root.hookStatus = stdout || stderr || "(no output)"
        }
    }

    function _runHookCommand(arg) {
        var cmd = "python3 \"" + root.installScriptPath + "\""
        if (arg && arg.length > 0) cmd += " " + arg
        hookRunner.connectSource(cmd)
    }

    Component.onCompleted: _runHookCommand("--status")

    QQC2.ScrollView {
        id: scroller
        anchors.fill: parent
        contentWidth: availableWidth
        clip: true

        Kirigami.FormLayout {
            width: scroller.availableWidth

            Kirigami.Separator {
                Kirigami.FormData.label: "Popup"
                Kirigami.FormData.isSection: true
            }

            QQC2.CheckBox {
                text: "Show running/blocked agents section"
                checked: cfg_showAgents
                onToggled: cfg_showAgents = checked
            }

            QQC2.CheckBox {
                text: "Include untracked claude/codex processes"
                checked: cfg_includeUntrackedAgents
                onToggled: cfg_includeUntrackedAgents = checked
            }

            QQC2.CheckBox {
                text: "Show task title under each session row"
                checked: cfg_showAgentPrompts
                onToggled: cfg_showAgentPrompts = checked
            }

            QQC2.CheckBox {
                text: "Close popup when focus moves away (click-to-focus closes the popup)"
                checked: cfg_closePopupOnFocusLoss
                onToggled: cfg_closePopupOnFocusLoss = checked
            }

            QQC2.ComboBox {
                Kirigami.FormData.label: "Refresh agents every:"
                model: [
                    { text: "2 seconds",   value: 2 },
                    { text: "5 seconds",   value: 5 },
                    { text: "10 seconds",  value: 10 },
                    { text: "30 seconds",  value: 30 },
                    { text: "1 minute",    value: 60 }
                ]
                textRole: "text"
                currentIndex: {
                    var v = cfg_agentsRefreshSeconds
                    for (var i = 0; i < model.length; i++) if (model[i].value === v) return i
                    return 1
                }
                onActivated: cfg_agentsRefreshSeconds = model[currentIndex].value
            }

            Kirigami.Separator {
                Kirigami.FormData.label: "Tray indicator"
                Kirigami.FormData.isSection: true
            }

            QQC2.CheckBox {
                text: "Red badge when any agent is blocked"
                checked: cfg_agentBlockedBadge
                onToggled: cfg_agentBlockedBadge = checked
            }

            QQC2.CheckBox {
                text: "Stacked colored count dots (working/blocked/idle)"
                checked: cfg_showAgentStateDots
                onToggled: cfg_showAgentStateDots = checked
            }

            RowLayout {
                Kirigami.FormData.label: "Dot size:"
                spacing: Kirigami.Units.smallSpacing
                Layout.fillWidth: true
                enabled: cfg_showAgentStateDots

                QQC2.Slider {
                    from: 50
                    to: 200
                    stepSize: 5
                    snapMode: QQC2.Slider.SnapAlways
                    value: cfg_agentStateDotsScale
                    Layout.fillWidth: true
                    onMoved: cfg_agentStateDotsScale = Math.round(value)
                }
                QQC2.Label {
                    text: cfg_agentStateDotsScale + "%"
                    Layout.minimumWidth: Kirigami.Units.gridUnit * 3
                    horizontalAlignment: Text.AlignRight
                    opacity: 0.7
                }
            }

            Kirigami.Separator {
                Kirigami.FormData.label: "Integration"
                Kirigami.FormData.isSection: true
            }

            RowLayout {
                spacing: Kirigami.Units.smallSpacing
                Layout.fillWidth: true

                QQC2.Button {
                    text: "Install"
                    icon.name: "list-add"
                    onClicked: root._runHookCommand("")
                }
                QQC2.Button {
                    text: "Remove"
                    icon.name: "list-remove"
                    onClicked: root._runHookCommand("--uninstall")
                }
                QQC2.Button {
                    text: "Check"
                    icon.name: "view-refresh"
                    onClicked: root._runHookCommand("--status")
                }
                Item { Layout.fillWidth: true }
            }

            QQC2.Label {
                text: "Status: " + root.hookStatus
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                opacity: 0.7
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            }

            QQC2.Label {
                text: "Installs the QML XHR env scripts (so plasmashell can read the agent state file) and registers codexbar:// for click-to-focus. Also cleans up any legacy Claude Code hooks or systemd unit from earlier widget versions."
                wrapMode: Text.WordWrap
                Layout.fillWidth: true
                opacity: 0.55
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            }
        }
    }
}
