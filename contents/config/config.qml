import QtQuick
import org.kde.plasma.configuration

ConfigModel {
    ConfigCategory {
        name: "Backend"
        icon: "applications-system"
        source: "configBackend.qml"
    }
    ConfigCategory {
        name: "Providers"
        icon: "network-server"
        source: "configProviders.qml"
    }
    ConfigCategory {
        name: "Tray"
        icon: "preferences-desktop-icons"
        source: "configTray.qml"
    }
    ConfigCategory {
        name: "Agents"
        icon: "system-run"
        source: "configAgents.qml"
    }
}
