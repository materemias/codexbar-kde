import QtQuick
import QtQuick.Layouts
import org.kde.plasma.components as PC3
import org.kde.kirigami as Kirigami

ColumnLayout {
    id: section
    property var record: ({})
    spacing: 4

    readonly property string iconSource: record && record.id
        ? Qt.resolvedUrl("../icons/" + record.id + ".svg")
        : ""

    readonly property real labelColumnWidth: Kirigami.Units.gridUnit * 2.6

    // Active rate-window rows only. 0% windows are hidden — they're noise
    // in a status display. Applies uniformly to primary/secondary/tertiary
    // slots and extras (Sonnet, Designs, Daily Routines).
    readonly property var visibleRows: {
        if (!section.record || section.record.error) return []
        var pid = section.record.id
        var rows = []
        var slots = ["primary", "secondary", "tertiary"]
        for (var i = 0; i < slots.length; i++) {
            var w = section.record[slots[i]]
            if (!w || w.usedPercent === undefined || w.usedPercent === null) continue
            if ((w.usedPercent || 0) < 0.01) continue
            rows.push({ rec: w, slot: slots[i], providerId: pid, extraTitle: "" })
        }
        var extras = section.record.extraRateWindows || []
        for (var e = 0; e < extras.length; e++) {
            var extra = extras[e]
            if (!extra || !extra.window) continue
            if (extra.window.usedPercent === undefined || extra.window.usedPercent === null) continue
            if ((extra.window.usedPercent || 0) < 0.01) continue
            rows.push({
                rec: extra.window,
                slot: extra.id || "extra",
                providerId: pid,
                extraTitle: extra.title || ""
            })
        }
        return rows
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
            visible: text.length > 0
            opacity: 0.55
            font.pixelSize: Kirigami.Theme.smallFont.pixelSize
            verticalAlignment: Text.AlignVCenter
            Layout.alignment: Qt.AlignVCenter
            text: {
                var rec = section.record || {}
                if (rec.error) return ""
                if (rec.id === "codex" && rec.loginMethod) return "· " + rec.loginMethod
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
            readonly property var rec: modelData.rec
            readonly property real pct: Math.max(0, Math.min(100, rec.usedPercent || 0))
            readonly property color tint: root.colorFor(pct)
            readonly property string resetText: root.formatReset(rec)

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
                text: rowItem.resetText
                font.pixelSize: Kirigami.Theme.smallFont.pixelSize - 1
                opacity: 0.55
                horizontalAlignment: Text.AlignLeft
                elide: Text.ElideRight
            }
        }
    }

}
