import QtQuick
import QtQuick.Layouts
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

ColumnLayout {
    id: section
    property var record: ({})
    property var forecast: null
    property bool showForecast: false
    spacing: 4

    readonly property string iconSource: record && record.id
        ? Qt.resolvedUrl("../icons/" + record.id + ".svg")
        : ""

    readonly property real labelColumnWidth: Kirigami.Units.gridUnit * 2.6

    // A single-account status display hides unused *extra* windows (Fable
    // only, Design, Routines …) to keep the popup within Plasma's clamped
    // height. The core 5h/7d windows always render: a freshly reset quota at
    // 0% must not look like a quota that does not exist. With multiple Codex
    // accounts, keep 0% extras too so an unused account does not look empty.
    readonly property var visibleRows: {
        if (!section.record || section.record.error) return []
        var pid = section.record.id
        var rows = []
        var hideZero = (section.record.accountCount || 1) < 2
        var slots = ["primary", "secondary", "tertiary"]
        for (var i = 0; i < slots.length; i++) {
            var w = section.record[slots[i]]
            if (!w || w.usedPercent === undefined || w.usedPercent === null) continue
            // Claude's tertiary (Sonnet) is noise at 0%; OpenCode Go's is its monthly window.
            if (hideZero && slots[i] === "tertiary" && pid !== "opencodego"
                    && (w.usedPercent || 0) < 0.01) continue
            rows.push({ rec: w, slot: slots[i], providerId: pid, extraTitle: "" })
        }
        var extras = section.record.extraRateWindows || []
        for (var e = 0; e < extras.length; e++) {
            var extra = extras[e]
            if (!extra || !extra.window) continue
            if (extra.window.usedPercent === undefined || extra.window.usedPercent === null) continue
            if (hideZero && (extra.window.usedPercent || 0) < 0.01) continue
            rows.push({
                rec: extra.window,
                slot: extra.id || "extra",
                providerId: pid,
                extraTitle: extra.title || ""
            })
        }
        return rows
    }

    // Row whose reset line carries the Codex/Claude "saved resets" suffix: the
    // weekly (7d) core window, else the last visible row; -1 when none.
    readonly property int creditsRowIndex: {
        var rows = section.visibleRows
        for (var i = 0; i < rows.length; i++) {
            var slot = rows[i].slot
            if ((slot === "primary" || slot === "secondary" || slot === "tertiary")
                    && rows[i].rec.windowMinutes === 10080) return i
        }
        return rows.length - 1
    }

    // Header: icon + UPPERCASE name + plan/subtitle + right-side badge.
    // Explicit Layout.minimumHeight on the row prevents the section header
    // from collapsing if any inner Label transiently has empty text.
    RowLayout {
        Layout.fillWidth: true
        Layout.minimumHeight: Kirigami.Units.iconSizes.smallMedium
        spacing: Kirigami.Units.smallSpacing

        Kirigami.Icon {
            source: section.iconSource
            implicitWidth: Kirigami.Units.iconSizes.smallMedium
            implicitHeight: Kirigami.Units.iconSizes.smallMedium
            Layout.alignment: Qt.AlignVCenter
            smooth: true
            visible: section.iconSource.length > 0
        }

        PC3.Label {
            text: root.providerDisplayName(section.record.id || "")
            font.weight: Font.Bold
            font.pixelSize: Kirigami.Theme.defaultFont.pixelSize * 1.02
            font.letterSpacing: 0.4
            verticalAlignment: Text.AlignVCenter
            Layout.alignment: Qt.AlignVCenter
        }

        PC3.Label {
            text: section.record.error ? "" : root.accountAvailabilityIndicator(section.record)
            visible: text.length > 0
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            verticalAlignment: Text.AlignVCenter
            Layout.alignment: Qt.AlignVCenter
        }

        PC3.Label {
            visible: text.length > 0
            opacity: 0.55
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            verticalAlignment: Text.AlignVCenter
            Layout.alignment: Qt.AlignVCenter
            text: {
                var rec = section.record || {}
                if (rec.error) return ""
                if (rec.id === "codex") {
                    var parts = []
                    if (rec.accountEmail) parts.push(rec.accountEmail)
                    if (rec.loginMethod) parts.push(rec.loginMethod)
                    return parts.length > 0 ? "· " + parts.join(" · ") : ""
                }
                if (rec.id === "claude" && rec.loginMethod) return "· " + rec.loginMethod
                if (rec.accountEmail) return "· " + rec.accountEmail
                return ""
            }
        }

        Item { Layout.fillWidth: true }

        PC3.Label {
            visible: text.length > 0
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            opacity: 0.75
            verticalAlignment: Text.AlignVCenter
            Layout.alignment: Qt.AlignVCenter
            text: {
                var rec = section.record || {}
                if (rec.error) return ""
                if (rec.balanceText) return rec.balanceText
                if (rec.id === "openrouter" && rec.openRouterUsage) {
                    var or_ = rec.openRouterUsage
                    if (or_.balance !== undefined) return "$" + or_.balance.toFixed(2) + " left"
                }
                return ""
            }
        }
    }

    PC3.Label {
        visible: section.record && section.record.error
        Layout.fillWidth: true
        wrapMode: Text.WordWrap
        color: Kirigami.Theme.negativeTextColor
        font.pixelSize: Kirigami.Theme.smallFont.pixelSize
        text: section.record && section.record.error ? section.record.error.message : ""
    }

    // Per-window rows.
    Repeater {
        model: section.visibleRows

        // Two-line row: bar + percent on top, optional reset text underneath
        // (indented to the bar column). Keeping reset off the bar row means
        // every bar takes the same available width, so percentages line up
        // vertically across windows.
        delegate: ColumnLayout {
            id: rowItem
            Layout.fillWidth: true
            spacing: 1
            opacity: rowItem.pct < 1 ? 0.45 : 1.0
            required property var modelData
            required property int index
            readonly property var rec: modelData.rec
            readonly property real pct: Math.max(0, Math.min(100, rec.usedPercent || 0))
            readonly property color tint: root.colorFor(pct, paceSettled ? pacePct : -1)
            readonly property string resetText: root.formatReset(rec, root.nowMs)
            // Codex and Claude "saved reset" credits ride on the weekly row's
            // reset line (they restore the 7d + 5h windows), falling back to
            // the last row.
            readonly property string creditsSuffix: {
                if (!section.record) return ""
                if (section.record.id !== "codex" && section.record.id !== "claude") return ""
                if (index !== section.creditsRowIndex) return ""
                var txt = root.formatResetCredits(section.record.resetCredits, root.nowMs)
                return txt.length > 0 ? " · " + txt : ""
            }

            // Window pace: elapsed share of this usage window, assuming even
            // consumption. Needs resetsAt + windowMinutes; balance-only rows
            // (OpenRouter, Kilo) carry neither and render without a tick. A
            // reset that is past or outside the declared window is treated as
            // broken data — no tick rather than a confident fake position.
            readonly property real paceWindowMs: (rec.windowMinutes || 0) * 60000
            readonly property real paceRemainingMs: rec.resetsAt
                ? new Date(rec.resetsAt).getTime() - root.nowMs : NaN
            readonly property bool paceValid: !isNaN(paceRemainingMs)
                && paceWindowMs > 0 && paceRemainingMs > 0
                && paceRemainingMs <= paceWindowMs
            readonly property real pacePct: paceValid
                ? (1 - paceRemainingMs / paceWindowMs) * 100 : -1
            readonly property real paceElapsedMs: paceValid
                ? paceWindowMs - paceRemainingMs : 0
            // Status and projection need a settled window: in the first
            // minutes elapsed is dominated by noise, so the tick stays
            // neutral and no "proj" number is emitted yet.
            readonly property bool paceSettled: pacePct >= 3
            readonly property bool overPace: paceSettled && pct > pacePct
            readonly property real projectedPct: paceSettled
                ? Math.min(999, pct * 100 / pacePct) : 0
            readonly property string projectionSuffix: !paceSettled ? ""
                : " · proj " + Math.round(projectedPct) + "%"
            readonly property string paceTip: {
                if (!paceValid) return ""
                var wm = rec.windowMinutes || 0
                var windowTxt = wm === 300 ? "5h" : wm === 1440 ? "1d"
                    : wm === 10080 ? "7d"
                    : root.relativeMs(paceWindowMs).replace(/^in /, "")
                if (!paceSettled) {
                    return "Window just started — pace projection shows once "
                        + "it passes 3% elapsed"
                }
                var elapsedTxt = paceElapsedMs >= 60000
                    ? root.relativeMs(paceElapsedMs).replace(/^in /, "") : "<1m"
                var line = elapsedTxt + " elapsed of " + windowTxt
                    + " (" + Math.round(pacePct) + "%)"
                if (overPace) {
                    var msToFull = pct > 0
                        ? paceElapsedMs * (100 - pct) / pct : 0
                    line += " — over pace: projected "
                        + Math.round(projectedPct) + "% at reset, hits 100% in ~"
                        + root.relativeMs(Math.round(msToFull)).replace(/^in /, "")
                } else {
                    line += " — on pace: projected "
                        + Math.round(projectedPct) + "% at reset"
                }
                return line
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing

                PC3.Label {
                    text: root.windowLabel(rowItem.modelData.providerId,
                                           rowItem.modelData.slot, rowItem.rec,
                                           rowItem.modelData.extraTitle)
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize
                    Layout.minimumWidth: section.labelColumnWidth
                    opacity: 0.85
                }

                Item {
                    Layout.fillWidth: true
                    implicitHeight: 8
                    Rectangle {
                        anchors.fill: parent
                        radius: 4
                        color: Qt.rgba(1, 1, 1, 0.10)
                    }
                    Rectangle {
                        radius: 4
                        color: rowItem.tint
                        height: parent.height
                        width: parent.width * (rowItem.pct / 100)
                        Behavior on width { NumberAnimation { duration: 300; easing.type: Easing.OutCubic } }
                    }
                    // Pace tick: where even consumption would sit at this
                    // point of the window. Tick ahead of the fill = budget
                    // lasts until reset; red tick behind the fill's end =
                    // current rate runs out before the window closes.
                    Rectangle {
                        visible: rowItem.paceValid
                        x: Math.max(0, Math.min(parent.width - width,
                            parent.width * rowItem.pacePct / 100 - width / 2))
                        anchors.verticalCenter: parent.verticalCenter
                        width: 2
                        height: parent.height + 4
                        radius: 1
                        color: rowItem.overPace ? Kirigami.Theme.negativeTextColor
                                                : Kirigami.Theme.textColor
                        opacity: rowItem.overPace ? 0.95 : 0.65
                        Behavior on x { NumberAnimation { duration: 300; easing.type: Easing.OutCubic } }
                    }
                    MouseArea {
                        anchors.fill: parent
                        enabled: rowItem.paceValid
                        hoverEnabled: enabled
                        PC3.ToolTip.visible: enabled && containsMouse
                        PC3.ToolTip.delay: 350
                        PC3.ToolTip.text: rowItem.paceTip
                    }
                }

                PC3.Label {
                    text: Math.round(rowItem.pct) + "%"
                    color: rowItem.tint
                    font.weight: Font.DemiBold
                    Layout.minimumWidth: Kirigami.Units.gridUnit * 2.4
                    horizontalAlignment: Text.AlignRight
                }
            }

            PC3.Label {
                visible: text.length > 0
                Layout.fillWidth: true
                Layout.leftMargin: section.labelColumnWidth + Kirigami.Units.smallSpacing
                text: rowItem.resetText + rowItem.projectionSuffix + rowItem.creditsSuffix
                opacity: 0.55
                horizontalAlignment: Text.AlignLeft
                elide: Text.ElideRight
            }
        }
    }

    Rectangle {
        id: forecastCard
        readonly property string forecastState: root.codexForecastState(section.forecast)
        readonly property color stateColor: forecastState === "announced"
            ? Kirigami.Theme.positiveTextColor
            : forecastState === "likely" ? Kirigami.Theme.neutralTextColor
                                          : Kirigami.Theme.disabledTextColor
        readonly property string forecastText: root.formatCodexForecast(section.forecast, root.nowMs)
        readonly property string incidentText: root.formatCodexForecastIncident(section.forecast)
        readonly property string alertText: root.formatCodexForecastAlert(section.forecast)
        visible: section.showForecast && forecastText.length > 0
        Layout.fillWidth: true
        Layout.topMargin: Kirigami.Units.smallSpacing / 2
        implicitHeight: forecastContent.implicitHeight
            + Kirigami.Units.smallSpacing * 2
        radius: 5
        color: Kirigami.Theme.alternateBackgroundColor
        border.width: 1
        // The border carries the state colour so the card reads at a glance;
        // unknown stays neutral so it does not compete with the usage bars.
        border.color: Qt.rgba(stateColor.r, stateColor.g, stateColor.b,
                              forecastState === "unknown" ? 0.3 : 0.6)

        ColumnLayout {
            id: forecastContent
            anchors.fill: parent
            anchors.margins: Kirigami.Units.smallSpacing
            spacing: 1

            PC3.Label {
                text: "Codex reset forecast"
                font.weight: Font.DemiBold
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            }

            RowLayout {
                Layout.fillWidth: true
                spacing: Kirigami.Units.smallSpacing

                Rectangle {
                    id: statePill
                    Layout.alignment: Qt.AlignTop
                    Layout.topMargin: 1
                    implicitWidth: stateLabel.implicitWidth + Kirigami.Units.smallSpacing * 2
                    implicitHeight: stateLabel.implicitHeight + 2
                    radius: height / 2
                    color: Qt.rgba(forecastCard.stateColor.r, forecastCard.stateColor.g,
                                   forecastCard.stateColor.b, 0.18)
                    border.width: 1
                    border.color: forecastCard.stateColor

                    PC3.Label {
                        id: stateLabel
                        anchors.centerIn: parent
                        text: forecastCard.forecastState.toUpperCase()
                        color: forecastCard.stateColor
                        font.weight: Font.DemiBold
                        font.pixelSize: Kirigami.Theme.smallFont.pixelSize - 2
                    }
                }

                PC3.Label {
                    Layout.fillWidth: true
                    text: forecastCard.forecastText
                    wrapMode: Text.WordWrap
                    font.pixelSize: Kirigami.Theme.smallFont.pixelSize - 1
                    opacity: 0.72
                }
            }
            PC3.Label {
                Layout.fillWidth: true
                text: forecastCard.incidentText
                visible: text.length > 0
                wrapMode: Text.WordWrap
                color: Kirigami.Theme.negativeTextColor
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize - 1
            }
            PC3.Label {
                Layout.fillWidth: true
                text: forecastCard.alertText
                visible: text.length > 0
                wrapMode: Text.WordWrap
                maximumLineCount: 3
                elide: Text.ElideRight
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize - 1
                opacity: 0.6
            }
        }
    }
}
