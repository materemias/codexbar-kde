#!/usr/bin/env python3
"""Render actual popup QML with synthetic data; never load main.qml or live state.

Requires qml6 with QtTest and Plasma/Kirigami, and localedef with en_GB locale
sources. The compiled locale and appearance settings stay in the output directory.
Run: python3 tests/render_screenshots.py --output /tmp/codexbar-screenshots
The host extracts unchanged display helpers from main.qml, not its execution code.
Outputs usage.png, agents.png and history.png, plus fixture.json, geometry.json,
assessment.json and render.log. --width and --height allow narrow-layout captures.
Agents and History show conversation peeks; no real sessions are touched.
"""
import argparse
import configparser
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from datetime import datetime, timedelta, timezone

REPO = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 29, 14, 0, tzinfo=timezone.utc)
NOW_MS = int(NOW.timestamp() * 1000)


def window(percent, minutes, remaining_hours, recent=None):
    result = {"usedPercent": percent, "windowMinutes": minutes,
              "resetsAt": (NOW + timedelta(hours=remaining_hours)).isoformat()}
    if recent is not None:
        result["recent"] = {"hours": 1 if minutes == 300 else 24,
                            "consumedPercent": recent}
    return result


def fixture():
    providers = [
        {"id": "claude", "ok": True, "loginMethod": "Max", "primary": window(28, 300, 2, 9),
         "secondary": window(39, 10080, 80, 13),
         "extraRateWindows": [{"id": "claude-design", "title": "Design", "window": window(14, 10080, 80)}]},
        {"id": "codex", "ok": True, "accountEmail": "studio@example.com", "loginMethod": "Pro",
         "accountCount": 3, "primary": window(35, 300, 1.5, 4), "secondary": window(42, 10080, 72, 11)},
        {"id": "codex", "ok": True, "accountEmail": "lab@example.com", "loginMethod": "Plus",
         "accountCount": 3, "primary": window(68, 300, 2, 21), "secondary": window(76, 10080, 48, 18)},
        {"id": "codex", "ok": True, "accountEmail": "team@example.com", "loginMethod": "Plus",
         "accountCount": 3, "primary": window(18, 300, 3, 0), "secondary": window(30, 10080, 96, 2)},
        {"id": "zai", "ok": True, "primary": window(25, 300, 2), "secondary": window(46, 43200, 240)},
        {"id": "opencodego", "ok": True, "primary": window(19, 300, 2),
         "secondary": window(28, 10080, 72), "tertiary": window(32, 43200, 240)},
        {"id": "openrouter", "ok": True, "openRouterUsage": {"balance": 18.4}},
        {"id": "kilo", "ok": True, "balanceText": "$12.60 left",
         "primary": {"usedPercent": 37}},
        {"id": "typesafe", "ok": True, "balanceText": "Balance $8.50"},
    ]
    agents = []
    for i, (folder, title, provider, state, model) in enumerate([
        ("atlas", "Review cache invalidation", "codex", "working", "gpt-5.4"),
        ("atlas", "Polish settings layout", "claude", "idle", "claude-sonnet-4-6"),
        ("harbor", "Trace upload retries", "omp", "working", "gpt-5.4"),
        ("harbor", "Review query timeout", "codex", "blocked", "gpt-5.4"),
        ("harbor", "Check export permissions", "claude", "idle", "claude-sonnet-4-6"),
        ("beacon", "Fix runner shutdown", "omp", "working", "gpt-5.4"),
        ("beacon", "Review webhook errors", "claude", "blocked", "claude-sonnet-4-6"),
        ("beacon", "Improve log grouping", "codex", "working", "gpt-5.4"),
    ]):
        session_id = f"00000000-0000-4000-8000-{i + 1:012d}"
        agents.append({"provider": provider, "sessionId": session_id,
                       "cwd": f"/workspace/{folder}", "windowTitle": title, "state": state,
                       "model": model, "host": "kitty", "stateChangedAt": NOW_MS - (i + 10) * 60000,
                       "resumeCommand": f"cd -- /workspace/{folder} && {provider} --resume {session_id}",
                       "recent": [
                           {"role": "user", "text": "Check whether a failed cache refresh keeps the previous result."},
                           {"role": "assistant", "text": "The previous value stays available while refresh runs. I am checking the failure path."},
                           {"role": "assistant", "kind": "tools", "text": "Read cache handler · inspected refresh logic"},
                       ]})
    closed_id = "00000000-0000-4000-8000-000000000100"
    history = [{**agents[0], "sessionId": closed_id, "closedBy": "exit",
                "windowTitle": "Fix keyboard navigation", "lastSeenAt": NOW_MS - 3600000,
                "recent": [{"role": "user", "text": "Keep selection on the same session when the list changes."},
                           {"role": "assistant", "text": "Selection now follows session identity rather than the row index."}],
                "resumeCommand": f"codex resume {closed_id}"}]
    restart_id = "00000000-0000-4000-8000-000000000200"
    history.append({**agents[1], "sessionId": restart_id, "closedBy": "reboot",
                    "windowTitle": "Review settings", "desktop": "1",
                    "lastSeenAt": NOW_MS - 7200000, "lastPrompt": "Check spacing in the settings page.",
                    "resumeCommand": f"claude --resume {restart_id}", "recent": []})
    counts = {state: sum(agent["state"] == state for agent in agents)
              for state in ("working", "blocked", "idle", "untracked")}
    counts["total"] = len(agents)
    return {"snapshot": {"cliVersion": "0.68.0", "updatedAt": NOW.isoformat(), "providers": providers},
            "agentSnapshot": {"agents": agents, "history": history, "counts": counts,
                              "ternInstalled": True}}


# Extract only named pure presentation functions; fail if the source contract moves.
HELPERS = """colorFor relativeMs _monthDay _absoluteTime _forecastTimeLeft
_forecastPercent codexForecastState formatCodexForecast formatCodexForecastIncident
formatCodexForecastAlert lastCodexIndex formatResetCredits formatReset
windowSpanMs compositeStats _compositeWindow codexCompositeRecord firstProviderIndex
windowLabel accountAvailabilityIndicator providerDisplayName agentStateColor cwdLabel ageFrom
historyLaunchAllowed launchHost canTeleport""".split()


def display_helpers():
    source = (REPO / "contents/ui/main.qml").read_text()
    functions = []
    for name in HELPERS:
        match = re.search(r"^    function " + re.escape(name)
                          + r"\([^\n]*?\{(?:[^\n]*\}$|\n.*?^    \})", source, re.M | re.S)
        if not match:
            raise RuntimeError(f"Display helper missing: {name}")
        functions.append(match.group(0))
    for name in ("forecastLikelyPercent", "_codexPlanWeight", "historyLaunchCooldownMs"):
        match = re.search(r"^    readonly property [^\n]+ " + re.escape(name) + r": [^\n]+", source, re.M)
        if not match:
            raise RuntimeError(f"Display constant missing: {name}")
        functions.append(match.group(0))
    return "\n".join(functions)


HOST = r'''
import QtQuick
import QtQuick.Window
import QtTest
import org.kde.kirigami as Kirigami
import org.kde.plasma.plasmoid
import "contents/ui" as Product
import "contents/ui/Usage.js" as Usage

Window {
    id: window
    width: @WIDTH@
    height: @HEIGHT@
    visible: true
    color: Kirigami.Theme.backgroundColor
    Rectangle {
        id: root
        anchors.fill: parent
        color: Kirigami.Theme.backgroundColor
        property var snapshot: (@SNAPSHOT@)
        property var agentSnapshot: (@AGENTS@)
        property double nowMs: @NOW@
        property string requestedTab: "usage"
        property bool expanded: true
        property bool loading: false
        property bool codexForecastEnabled: false
        property bool showRecentUsage: true
        property string lastError: ""
        property string agentsError: ""
        property string notificationError: ""
        property string historyLaunchError: ""
        function desktopInfoFor(record) { return null }
        property var historyLaunches: ({})
        property var teleports: ({})
        property string teleportError: ""
        function teleportAgent(record) { throw new Error("Preview must never move a session") }
        function launchHistory(record) { throw new Error("Preview must never launch a session") }
        function launchAllHistory(records) { throw new Error("Preview must never launch sessions") }
        function dismissHistory(records) { throw new Error("Preview must never mutate history") }
        function refresh() { throw new Error("Preview must never fetch live usage") }
        function runAggregator() { throw new Error("Preview must never scan live sessions") }
        @HELPERS@
        Product.FullRepresentation { id: popup; anchors.fill: parent }
    }
    TestCase {
        id: keyboard
        name: "IsolatedPreviewKeyboard"
        when: false
        // No test runner: keyClick sends events only to this offscreen window.
    }
    function tabGeometry() {
        var result = []
        function inside(icon, target) {
            var point = icon.mapToItem(target, 0, 0)
            return point.x >= -0.01 && point.y >= -0.01
                && point.x + icon.width <= target.width + 0.01
                && point.y + icon.height <= target.height + 0.01
        }
        function visit(item) {
            if (item.iconName !== undefined && item.contentItem) {
                var icon = item.contentItem.children[0]
                result.push({name: item.iconName, checked: item.checked,
                    width: icon.width, height: icon.height,
                    expected: Kirigami.Units.iconSizes.small,
                    contentWidth: item.contentItem.width, contentHeight: item.contentItem.height,
                    buttonWidth: item.width, buttonHeight: item.height,
                    insideContent: inside(icon, item.contentItem), insideButton: inside(icon, item)})
            }
            var children = item.children || []
            for (var i = 0; i < children.length; ++i) visit(children[i])
        }
        visit(popup)
        return result
    }
    property int step: 0
    property var scenes: [
        {name: "usage", keys: []},
        {name: "agents", keys: [Qt.Key_Right, Qt.Key_Down, Qt.Key_Space]},
        {name: "history", keys: [Qt.Key_Right, Qt.Key_Up, Qt.Key_Space]}
    ]
    Timer {
        // Allow the offscreen component layout to settle before each still.
        id: advance
        interval: 800
        running: true
        onTriggered: {
            popup.forceActiveFocus()
            var scene = window.scenes[window.step]
            for (var i = 0; i < scene.keys.length; ++i) keyboard.keyClick(scene.keys[i])
            capture.restart()
        }
    }
    Timer {
        id: capture
        interval: 700
        onTriggered: root.grabToImage(function(result) {
            var scene = window.scenes[window.step]
            if (!result.saveToFile(scene.name + ".png")) throw new Error("Could not save " + scene.name)
            console.log("CAPTURE " + scene.name)
            console.log("GEOMETRY " + JSON.stringify({scene: scene.name, tabs: window.tabGeometry()}))
            window.step++
            if (window.step === window.scenes.length) Qt.quit()
            else advance.restart()
        }, Qt.size(window.width, window.height))
    }
}
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/codexbar-screenshots"))
    parser.add_argument("--width", type=int, default=864)
    parser.add_argument("--height", type=int, default=895)
    args = parser.parse_args()
    if not 360 <= args.width <= 1200 or not 280 <= args.height <= 1200:
        parser.error("Size must be 360..1200 wide and 280..1200 high")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    locale_dir = output / "locales"
    locale_dir.mkdir(exist_ok=True)
    subprocess.run(["localedef", "--no-archive", "-i", "en_GB", "-f", "UTF-8",
                    str(locale_dir / "en_GB.UTF-8")], check=True)
    for directory in ("config", "cache", "runtime", "home", "data"):
        (output / directory).mkdir(exist_ok=True, mode=0o700)
    # The Plasma theme palette differs from the application color scheme.
    # Copy appearance only; never use the live config directory in the Qt host.
    data_home = Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share")))
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))
    plasma = configparser.ConfigParser(interpolation=None)
    plasma.read(config_home / "plasmarc")
    theme_name = plasma.get("Theme", "name", fallback="Nordic")
    if Path(theme_name).name != theme_name:
        raise ValueError("Plasma theme must be a theme name, not a path")
    theme_candidates = [data_home / "plasma/desktoptheme" / theme_name,
                        Path("/usr/share/plasma/desktoptheme") / theme_name]
    theme = next((path for path in theme_candidates if path.is_dir()), None)
    candidates = ([theme / "colors"] if theme else []) + [
        data_home / "color-schemes/Nordic.colors", Path("/usr/share/color-schemes/BreezeDark.colors")]
    scheme = next(path for path in candidates if path.is_file())
    palette = configparser.ConfigParser(interpolation=None)
    palette.optionxform = str
    palette.read(scheme)
    settings = configparser.ConfigParser(interpolation=None, strict=False)
    settings.optionxform = str
    settings.read(config_home / "kdeglobals")
    if not palette.has_section("General"):
        palette.add_section("General")
    for key in ("font", "smallestReadableFont"):
        if settings.has_option("General", key):
            palette.set("General", key, settings.get("General", key))
    palette["Icons"] = {"Theme": settings.get("Icons", "Theme", fallback="breeze-dark")}
    for path in (output / "config/kdeglobals", output / "theme.colors"):
        with path.open("w") as stream:
            palette.write(stream, space_around_delimiters=False)
    if theme:
        shutil.copytree(theme, output / "data/plasma/desktoptheme" / theme_name, dirs_exist_ok=True)
        (output / "config/plasmarc").write_text(f"[Theme]\nname={theme_name}\n")
    # Byte-for-byte real UI/dependencies. main.qml is deliberately not instantiated.
    shutil.copytree(REPO / "contents/ui", output / "contents/ui", dirs_exist_ok=True)
    shutil.copytree(REPO / "contents/icons", output / "contents/icons", dirs_exist_ok=True)
    module = output / "imports/org/kde/plasma/plasmoid"
    module.mkdir(parents=True, exist_ok=True)
    (module / "qmldir").write_text("module org.kde.plasma.plasmoid\nsingleton Plasmoid 1.0 Plasmoid.qml\n")
    (module / "Plasmoid.qml").write_text('pragma Singleton\nimport QtQuick\nQtObject {\n'
        ' property var configuration: ({showAgents: true, showAgentPrompts: true, showComposite5h: false, showComposite7d: true})\n}\n')
    data = fixture()
    (output / "fixture.json").write_text(json.dumps(data, indent=2) + "\n")
    host = HOST.replace("@WIDTH@", str(args.width)).replace("@NOW@", str(NOW_MS))
    host = host.replace("@HEIGHT@", str(args.height))
    host = host.replace("@SNAPSHOT@", json.dumps(data["snapshot"]))
    host = host.replace("@AGENTS@", json.dumps(data["agentSnapshot"]))
    host = host.replace("@HELPERS@", display_helpers())
    (output / "host.qml").write_text(host)
    environment = dict(os.environ, HOME=str(output / "home"), XDG_CONFIG_HOME=str(output / "config"),
                       XDG_CACHE_HOME=str(output / "cache"), XDG_DATA_HOME=str(output / "data"),
                       XDG_RUNTIME_DIR=str(output / "runtime"), XDG_DATA_DIRS="/usr/local/share:/usr/share",
                       QT_QPA_PLATFORM="offscreen", QT_QUICK_BACKEND="software", QT_SCALE_FACTOR="1",
                       QT_QUICK_CONTROLS_STYLE="org.kde.desktop", QT_QPA_PLATFORMTHEME="kde",
                       QT_STYLE_OVERRIDE="breeze", QML_IMPORT_PATH=str(output / "imports"),
                       QML2_IMPORT_PATH=str(output / "imports"), KDE_COLOR_SCHEME_PATH=str(output / "theme.colors"),
                       QT_LOGGING_RULES="*.debug=false;qml.debug=true;qml.warning=true;qt.qml.warning=true",
                       QT_FORCE_STDERR_LOGGING="1",
                       LANG="en_GB.UTF-8", LC_ALL="en_GB.UTF-8", LOCPATH=str(locale_dir),
                       DBUS_SESSION_BUS_ADDRESS="unix:path=/nonexistent-codexbar-preview-bus", TZ="UTC")
    process = subprocess.run(["qml6", "--apptype", "widget", str(output / "host.qml")], cwd=output,
                             env=environment, capture_output=True, text=True, timeout=45)
    log = process.stdout + process.stderr
    (output / "render.log").write_text(log)
    print("\n".join(line for line in log.splitlines() if "GEOMETRY " not in line))
    process.check_returncode()
    captures = re.findall(r"CAPTURE ([\w-]+)", log)
    if captures != ["usage", "agents", "history"] or re.search(r"(?:ReferenceError|TypeError|Error:|Unable to assign)", log):
        raise RuntimeError("Incomplete or invalid render; see render.log")
    geometry = [json.loads(line) for line in re.findall(r"GEOMETRY (.+)", log)]
    (output / "geometry.json").write_text(json.dumps(geometry, indent=2) + "\n")
    if len(geometry) != len(captures) or any(len(scene["tabs"]) != 3 for scene in geometry):
        raise RuntimeError("Tab geometry capture missing")
    for scene in geometry:
        for tab in scene["tabs"]:
            if (abs(tab["width"] - tab["height"]) > 0.01
                    or tab["width"] < tab["expected"] - 0.01
                    or not tab["insideContent"] or not tab["insideButton"]):
                raise RuntimeError(f"Tab icon shrunk or clipped: {scene['scene']} {tab}")
    for name in {tab["name"] for scene in geometry for tab in scene["tabs"]}:
        states = {tab["checked"] for scene in geometry for tab in scene["tabs"] if tab["name"] == name}
        if states != {False, True}:
            raise RuntimeError(f"Selected and unselected geometry not exercised: {name}")
    sources = {str(path.relative_to(output)): hashlib.sha256(path.read_bytes()).hexdigest()
               for path in sorted((output / "contents/ui").glob("*")) if path.is_file()}
    report = {"synthetic_only": True, "theme": f"{theme_name}/{scheme.name}",
              "logical_size": [args.width, args.height],
              "screenshots": [name + ".png" for name in captures], "source_sha256": sources,
              "display_helpers_from_main": HELPERS,
              "limitations": ["Offscreen Qt, not Plasma compositor chrome or live panel integration",
                              "Synthetic fixtures; no fetching, polling, process scanning, focus, or launch actions",
                              "Desktop badges omitted: no window manager connection",
                              "Keyboard events target the isolated Qt window only"]}
    (output / "assessment.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({key: report[key] for key in ("theme", "logical_size", "screenshots")}, indent=2))


if __name__ == "__main__":
    main()
