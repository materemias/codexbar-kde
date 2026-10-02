"""Run the actual applet root with isolated inputs and the native process plugin."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
import xml.etree.ElementTree as ET


ROOT = Path(__file__).resolve().parents[1]
QML = shutil.which("qml6") or shutil.which("qml") or "/usr/lib/qt6/bin/qml"
PLUGIN = ROOT / "build/package/contents/ui/process"


@unittest.skipUnless(Path(QML).is_file() and (PLUGIN / "libcodexbarprocess.so").is_file(),
                     "Build the native package and install the Qt 6 qml tool first")
class MainLifecycleTest(unittest.TestCase):
    def applet(self, scenario, helper_case=None):
        with tempfile.TemporaryDirectory(prefix="codexbar-main-") as directory:
            root = Path(directory)

            def put(name, text):
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text)

            (root / "ui").mkdir()
            for name in ("main.qml", "Command.js", "Usage.js", "Notifications.js"):
                shutil.copy2(ROOT / "contents/ui" / name,
                             root / "ui" / ("Main.qml" if name == "main.qml" else name))
            (root / "ui/process").symlink_to(PLUGIN, target_is_directory=True)
            for name in ("FullRepresentation", "CompactRepresentation"):
                put(f"ui/{name}.qml", "import QtQuick\nItem { property bool tooltipHovered: false }\n")
            # Replace desktop integration only; root bindings, callbacks, reducers,
            # command construction, and native subprocess execution remain real.
            module = "imports/org/kde/plasma/plasmoid/"
            put(module + "qmldir", "module org.kde.plasma.plasmoid\n"
                "singleton Plasmoid 1.0 Plasmoid.qml\nPlasmoidItem 1.0 PlasmoidItem.qml\n")
            configuration = []
            schema = ET.parse(ROOT / "contents/config/main.xml")
            for entry in schema.findall(".//{*}entry"):
                name, kind = entry.attrib["name"], entry.attrib["type"]
                default = entry.findtext("{*}default", "")
                if kind == "Bool":
                    value = name in ("enableCodex", "ompModeNotifications") or (
                        name == "usageNotifications" and bool((helper_case or {}).get("usageNotifications")))
                elif kind == "Int":
                    value = int(default or 0)
                elif kind == "StringList":
                    value = default.split(",") if default else []
                else:
                    value = "/backend-a" if name == "cliPath" else default
                configuration.append(f"property var {name}: {json.dumps(value)}")
            put(module + "Plasmoid.qml", "pragma Singleton\nimport QtQuick\nQtObject {\n"
                "signal activated()\nproperty int formFactor: 2\nproperty string globalShortcut: ''\n"
                "property QtObject configuration: QtObject {\n"
                + "\n".join(configuration) + "\n}\n}\n")
            put(module + "PlasmoidItem.qml", """import QtQuick
Item {
    property bool hideOnWindowDeactivate: true
    property bool expanded: false
    property Component preferredRepresentation
    property Component fullRepresentation
    property Component compactRepresentation
    property Item compactRepresentationItem: null
    property string toolTipMainText
    property string toolTipSubText
    property int toolTipTextFormat
}
""")
            module = "imports/org/kde/plasma/core/"
            put(module + "qmldir", "module org.kde.plasma.core\nsingleton Types 1.0 Types.qml\n")
            put(module + "Types.qml", "pragma Singleton\nimport QtQuick\nQtObject { enum FormFactor { Planar } }\n")
            module = "imports/org/kde/taskmanager/"
            put(module + "qmldir", "module org.kde.taskmanager\n"
                "TasksModel 1.0 TasksModel.qml\nVirtualDesktopInfo 1.0 VirtualDesktopInfo.qml\n")
            put(module + "TasksModel.qml", "import QtQuick\nListModel {\n"
                "enum Mode { GroupDisabled }\nproperty int groupMode: 0\n}\n")
            put(module + "VirtualDesktopInfo.qml", "import QtQuick\nQtObject {\n"
                "property var desktopIds: []\nproperty var currentDesktop: 0\n}\n")
            put("scripts/scenario.json", json.dumps(helper_case))
            put("scripts/codexbar_fetch.py", '''import datetime, json, pathlib, sys, time
root = pathlib.Path(__file__).parent
args = sys.argv[1:]
backend = args[args.index("--cli-path") + 1]
cache = "--cache-only" in args
calls = root / "calls"
with calls.open("a") as output:
    output.write(json.dumps({"backend": backend, "cache": cache}) + "\\n")
history = [json.loads(line) for line in calls.read_text().splitlines()]
number = len(history)
normal = sum(call["backend"] == backend and not call["cache"] for call in history)
time.sleep(0.15)
test = json.loads((root / "scenario.json").read_text())
if test and test.get("failure") is not None and normal >= 2:
    if cache:
        if test.get("recoveryFails"):
            print("cache validation failed", file=sys.stderr)
            sys.exit(1)
        print(json.dumps({"providers": test.get("recoveryProviders", []),
            "cacheOnly": True, "fatal": None, "codexRotation": None}))
    elif test["failure"] == "fatal":
        print(json.dumps({"providers": [], "cacheOnly": False,
            "fatal": {"message": "credentials changed"}, "codexRotation": None}))
        sys.exit(1)
    else:
        print(test["failure"])
        print("transport exploded", file=sys.stderr)
        sys.exit(1)
    sys.exit(0)
if backend == "/missing":
    print("selected backend is unavailable", file=sys.stderr)
    sys.exit(1)
if not cache and normal == 6:
    print("temporary fetch failure", file=sys.stderr)
    sys.exit(1)
mode = {1: "FILL", 2: "TARGET", 4: "BURN", 5: "BURN", 7: "IDLE", 8: "IDLE"}.get(normal)
rotation = None if cache or mode is None else {
    "mode": mode, "operatingMode": "confirm" if normal == 8 else "auto",
    "stalled": normal == 5}
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
scope = "b" if test and test.get("switchScope") and normal >= 2 else "a"
percent = 80 if test and test.get("usageNotifications") else number
print(json.dumps({"updatedAt": now, "fatal": None, "cacheOnly": cache,
    "providers": [{"id": "codex", "ok": True, "sourceScope": scope * 64,
        "stale": cache, "primary": {"usedPercent": percent}, "updatedAt": now}],
    "codexRotation": rotation, "cliVersion": backend}))
''')
            put("bin/notify-send", "#!/usr/bin/env python3\n"
                "import json, pathlib, sys, time\n"
                "time.sleep(0.8)\n"
                "with pathlib.Path(__file__).with_name('notifications').open('a') as output:\n"
                "    output.write(json.dumps(sys.argv[1:]) + '\\n')\n"
                "config = json.loads((pathlib.Path(__file__).parent.parent / 'scripts/scenario.json').read_text()) or {}\n"
                "if config.get('notifyFails'):\n"
                "    print('synthetic service failure', file=sys.stderr)\n"
                "    sys.exit(1)\n")
            (root / "bin/notify-send").chmod(0o755)
            put("test.qml", '''import QtQuick
import org.kde.plasma.plasmoid
Item {
    id: fixture
    property int stage: 0
    property int ticks: 0
    property string oldCommand: ""
    function check(condition, message) {
        if (!condition) throw new Error(message)
    }
    Loader {
        id: applet
        source: "ui/Main.qml"
        onStatusChanged: if (status === Loader.Error) Qt.exit(1)
    }
    Timer {
        interval: 20; running: true; repeat: true
        onTriggered: {
            try {
                if (++fixture.ticks > 300) throw new Error("applet scenario timed out at stage " + fixture.stage)
                var app = applet.item
                if (!app) return
''' + scenario + '''
            } catch (error) {
                console.error("FAIL " + error.message)
                Qt.exit(1)
            }
        }
    }
}
''')
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                       QT_FORCE_STDERR_LOGGING="1", QT_LOGGING_TO_CONSOLE="1",
                       QML_IMPORT_PATH=str(root / "imports"),
                       PATH=str(root / "bin") + os.pathsep + os.environ["PATH"])
            result = subprocess.run([QML, str(root / "test.qml")], env=env,
                                    capture_output=True, text=True, timeout=15)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("PASS actual main", output)
            calls = [json.loads(line) for line in (root / "scripts/calls").read_text().splitlines()]
            notifications = root / "bin/notifications"
            return calls, ([json.loads(line) for line in notifications.read_text().splitlines()]
                           if notifications.exists() else [])

    def test_authoritative_empty_fatal_snapshot_does_not_resurrect_memory(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    check(app.snapshot.providers.length === 1, "initial scoped usage")
                    app.refresh()
                    fixture.stage = 1
                } else {
                    check(app.snapshot.providers.length === 0,
                        "an authoritative empty fatal snapshot must retire old credentials")
                    check(app.lastError === "credentials changed" && app.snapshot.codexRotation === null,
                        "fatal snapshot must preserve the current error and clear mode")
                    console.log("PASS actual main authoritative empty snapshot")
                    Qt.exit(0)
                }
''', {"failure": "fatal"})
        self.assertEqual([call["cache"] for call in calls], [True, False, False])
        self.assertEqual(notifications, [])

    def test_transport_failure_revalidates_cache_and_preserves_empty_marker(self):
        for failure in ("", "not json", '{"providers": [null]}'):
            with self.subTest(failure=failure):
                calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    app.refresh()
                    fixture.stage = 1
                } else {
                    check(app.snapshot.providers.length === 0,
                        "transport failure cannot trust old in-memory credentials")
                    check(app.snapshot.cacheOnly === true,
                        "empty validated cache result must retain its empty-state marker")
                    check(!!app.lastError && app.snapshot.codexRotation === null
                        && app.ompModeNotificationState.mode === null,
                        "cache recovery preserves fetch failure and clears mode continuity")
                    console.log("PASS actual main scoped empty recovery")
                    Qt.exit(0)
                }
''', {"failure": failure})
                self.assertEqual([call["cache"] for call in calls], [True, False, False, True])
                self.assertEqual(notifications, [])

    def test_recovery_cache_replaces_memory_with_current_scope_only(self):
        current = {"id": "codex", "ok": True, "stale": True, "sourceScope": "b" * 64,
                   "primary": {"usedPercent": 83}, "cachedAt": "2026-09-29T12:00:00Z"}
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    app.refresh()
                    fixture.stage = 1
                } else {
                    var record = app.snapshot.providers[0]
                    check(app.snapshot.providers.length === 1 && record.sourceScope === "b".repeat(64)
                        && record.primary.usedPercent === 83 && record.stale === true,
                        "only helper-confirmed current credentials may survive recovery")
                    check(app.lastError === "transport exploded"
                        && app.snapshot.codexRotation === null,
                        "cache success cannot hide the failed refresh")
                    console.log("PASS actual main scoped recovery")
                    Qt.exit(0)
                }
''', {"failure": "", "recoveryProviders": [current]})
        self.assertEqual([call["cache"] for call in calls], [True, False, False, True])
        self.assertEqual(notifications, [])

    def test_failed_cache_recovery_clears_unverified_usage_without_retry_loop(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    app.refresh()
                    fixture.stage = 1
                } else if (fixture.stage === 1) {
                    check(app.snapshot.providers.length === 0 && app.snapshot.codexRotation === null,
                        "failed validation must clear unverified usage and mode")
                    check(app.lastError === "transport exploded", "keep original refresh failure")
                    fixture.stage = 2
                } else if (++fixture.stage >= 12) {
                    check(!app.loading, "recovery must not trigger a fresh/recovery loop")
                    console.log("PASS actual main failed scoped recovery")
                    Qt.exit(0)
                }
''', {"failure": "", "recoveryFails": True})
        self.assertEqual([call["cache"] for call in calls], [True, False, False, True])
        self.assertEqual(notifications, [])

    def test_late_usage_delivery_cannot_settle_replacement_credential_scope(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    Plasmoid.configuration.ompModeNotifications = false
                    fixture.oldCommand = Object.keys(app.pendingNotifications)[0]
                    check(!!fixture.oldCommand, "first credential alert is in flight")
                    app.refresh()
                    fixture.stage = 1
                } else if (fixture.stage === 1) {
                    check(app.snapshot.providers[0].sourceScope === "b".repeat(64), "new credentials loaded")
                    if (app.pendingNotifications[fixture.oldCommand]) return
                    var states = Object.values(app.usageNotificationState)
                    check(states.length === 1 && states[0].pending.length === 1 && states[0].level === 0,
                        "old credential callback cannot acknowledge the replacement credential alert")
                    fixture.stage = 2
                } else if (Object.keys(app.pendingNotifications).length === 0) {
                    check(Object.values(app.usageNotificationState)[0].level === 2,
                        "current credential callback acknowledges only its own alert")
                    console.log("PASS actual main scopes usage acknowledgements")
                    Qt.exit(0)
                }
''', {"switchScope": True, "usageNotifications": True})
        self.assertEqual(len(calls), 3)
        self.assertEqual(len(notifications), 2)

    def test_successful_fetch_does_not_clear_notification_service_failure(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading || Object.keys(app.pendingNotifications).length) return
                if (fixture.stage === 0) {
                    app.refresh()
                    fixture.stage = 1
                } else if (fixture.stage === 1) {
                    check(app.notificationError.indexOf("synthetic service failure") >= 0,
                        "real notification subprocess failure is shown")
                    fixture.oldCommand = app.notificationError
                    app.refresh()
                    fixture.stage = 2
                } else {
                    check(app.lastError === "" && app.notificationError === fixture.oldCommand,
                        "successful usage refresh cannot clear an unrelated notification failure")
                    console.log("PASS actual main preserves notification service failures")
                    Qt.exit(0)
                }
''', {"notifyFails": True})
        self.assertEqual(len(calls), 4)
        self.assertEqual(len(notifications), 1)

    def test_backend_change_clears_old_usage_before_missing_backend_fails(self):
        calls, notifications = self.applet('''
                if (fixture.stage === 0 && !app.cacheRestoring && app.snapshot.providers.length) {
                    check(app.snapshot.providers[0].primary.usedPercent === 1,
                        "cache must be shown before the fresh fetch completes")
                    fixture.stage = 1
                } else if (fixture.stage === 1 && !app.loading) {
                    check(app.snapshot.codexRotation.mode === "FILL", "fresh initial status")
                    Plasmoid.configuration.cliPath = "/missing"
                    check(app.snapshot.providers.length === 0 && app.snapshot.codexRotation === null,
                        "changing backend must immediately discard previous usage and rotation")
                    fixture.stage = 2
                } else if (fixture.stage === 2 && !app.loading && app.lastError) {
                    check(app.snapshot.providers.length === 0 && app.snapshot.codexRotation === null,
                        "failed new backend must not retain another backend's usage")
                    console.log("PASS actual main backend change clears old readings")
                    Qt.exit(0)
                }
''')
        self.assertEqual(calls, [{"backend": "/backend-a", "cache": True},
                                 {"backend": "/backend-a", "cache": False},
                                 {"backend": "/missing", "cache": False},
                                 {"backend": "/missing", "cache": True}])
        self.assertEqual(notifications, [])

    def test_inflight_backend_round_trip_rejects_old_completion(self):
        calls, notifications = self.applet('''
                if (fixture.stage === 0 && !app.cacheRestoring && !app.loading) {
                    app.refresh()
                    check(app.loading, "start the old backend request")
                    Plasmoid.configuration.cliPath = "/missing"
                    Plasmoid.configuration.cliPath = "/backend-a"
                    fixture.stage = 1
                } else if (fixture.stage === 1) {
                    if (app.snapshot.providers.length) {
                        check(app.snapshot.providers[0].primary.usedPercent === 4,
                            "A to B to A must reject the original A completion")
                        check(!app.loading, "only the replacement request can publish usage")
                        fixture.stage = 2
                    }
                } else if (fixture.stage === 2 && Object.keys(app.pendingNotifications).length === 0) {
                    console.log("PASS actual main rejects stale inflight backend completion")
                    Qt.exit(0)
                }
''')
        self.assertEqual([call["backend"] for call in calls], ["/backend-a"] * 4)
        self.assertEqual(notifications, [])

    def test_mode_dispatch_uses_fresh_status_and_clears_gaps(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading
                        || Object.keys(app.pendingNotifications).length) return
                if (fixture.stage === 0) {
                    check(app.ompModeNotificationState.mode === "auto · FILL",
                        "first fresh status must establish the mode baseline")
                } else if (fixture.stage === 2) {
                    check(app.snapshot.codexRotation === null
                            && app.ompModeNotificationState.mode === null,
                        "missing status must break comparison continuity")
                } else if (fixture.stage === 5) {
                    check(app.lastError === "temporary fetch failure"
                            && app.snapshot.providers[0].stale === true
                            && app.snapshot.codexRotation === null
                            && app.ompModeNotificationState.mode === null,
                        "fetch failure must retain usage but not mode status or its baseline")
                } else if (fixture.stage === 7) {
                    console.log("PASS actual main sends only available mode transitions")
                    Qt.exit(0)
                    return
                }
                fixture.stage++
                app.refresh()
''')
        self.assertEqual([call["cache"] for call in calls],
                         [True] + [False] * 6 + [True, False, False])
        self.assertEqual([args[-1] for args in notifications], [
            "auto · FILL → auto · TARGET",
            "auto · BURN → auto · STALLED (BURN)",
            "auto · IDLE → confirm · IDLE",
        ])
        self.assertTrue(all(args[-2] == "CodexBar omp mode changed" for args in notifications))

    def test_disabling_codex_clears_status_and_reenabling_is_silent(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    Plasmoid.configuration.enableCodex = false
                    check(app.snapshot.codexRotation === null
                            && app.ompModeNotificationState.mode === null,
                        "disabling Codex must immediately clear status and comparison baseline")
                    app.refresh()
                    check(!app.loading && app.snapshot.providers.length === 0,
                        "empty provider selection must not launch a fetch")
                    Plasmoid.configuration.enableCodex = true
                    app.refresh()
                    fixture.stage = 1
                } else if (fixture.stage === 1) {
                    check(app.snapshot.codexRotation.mode === "TARGET"
                            && app.ompModeNotificationState.mode === "auto · TARGET"
                            && Object.keys(app.pendingNotifications).length === 0,
                        "reenabling Codex establishes a silent fresh baseline")
                    console.log("PASS actual main clears disabled-provider mode baseline")
                    Qt.exit(0)
                }
''')
        self.assertEqual(len(calls), 3)
        self.assertEqual(notifications, [])

    def test_old_delivery_cannot_settle_new_backend_mode_transition(self):
        calls, notifications = self.applet('''
                if (app.cacheRestoring || app.loading) return
                if (fixture.stage === 0) {
                    app.refresh()
                    fixture.stage = 1
                } else if (fixture.stage === 1) {
                    fixture.oldCommand = Object.keys(app.pendingNotifications)[0]
                    check(!!fixture.oldCommand, "old backend transition must be in flight")
                    Plasmoid.configuration.cliPath = "/backend-b"
                    fixture.stage = 2
                } else if (fixture.stage === 2) {
                    check(app.ompModeNotificationState.mode === "auto · FILL",
                        "new backend starts with its own silent baseline")
                    app.refresh()
                    fixture.stage = 3
                } else if (fixture.stage === 3) {
                    if (app.pendingNotifications[fixture.oldCommand]) return
                    check(app.ompModeNotificationState.pending
                            && app.ompModeNotificationState.transition !== null,
                        "late old-backend acknowledgement must not settle the new backend event")
                    fixture.stage = 4
                } else if (fixture.stage === 4 && Object.keys(app.pendingNotifications).length === 0) {
                    check(!app.ompModeNotificationState.pending
                            && app.ompModeNotificationState.transition === null,
                        "the new backend acknowledgement must settle its own event")
                    console.log("PASS actual main isolates late notification acknowledgements")
                    Qt.exit(0)
                }
''')
        self.assertEqual([call["backend"] for call in calls],
                         ["/backend-a"] * 3 + ["/backend-b"] * 2)
        self.assertEqual([args[-1] for args in notifications],
                         ["auto · FILL → auto · TARGET"] * 2)

    def test_focused_session_needs_one_candidate_in_the_focused_window(self):
        self.applet('''
                if (app.cacheRestoring || app.loading) return
                function win(pid, caption) { return { info: { pid: pid, caption: caption } } }
                function key(w, agents) {
                    return app.focusedAgentKeyFor(w, { agents: agents })
                }
                var code = [win(5, "a.cs - alpha - Visual Studio Code"), win(5, "b.js - beta - Visual Studio Code")]
                app.taskWindows = code.concat([win(7, "π > Fix login"), win(9, "work")])
                var alpha = { provider: "omp", sessionId: "a", cwd: "/x/alpha", ancestorPids: [11, 5] }
                check(key(code[0], [alpha]) === '["omp","a"]', "VS Code window of the session's folder")
                check(key(code[1], [alpha]) === "", "another window of the same VS Code process")
                var login = { provider: "omp", sessionId: "l", windowTitle: "Fix login", cwd: "/w", ancestorPids: [12, 7] }
                var other = { provider: "pi", sessionId: "o", windowTitle: "Docs", cwd: "/w", ancestorPids: [13, 7] }
                check(key(app.taskWindows[2], [other, login]) === '["omp","l"]', "kitty tab named by caption")
                var shown = { provider: "omp", sessionId: "s", host: "tern", cwd: "/w", ternSession: "work", ancestorPids: [14, 9] }
                var hidden = { provider: "omp", sessionId: "h", host: "tern", cwd: "/w", ancestorPids: [15, 9] }
                var elsewhere = { provider: "omp", sessionId: "e", host: "tern", cwd: "/w", ternSession: "Default", ancestorPids: [16, 9] }
                check(key(app.taskWindows[3], [hidden, elsewhere, shown]) === '["omp","s"]',
                    "Tern pane visible in the session the window shows")
                check(key(app.taskWindows[3], [elsewhere]) === "",
                    "a visible pane of a session the window does not show")
                check(key(app.taskWindows[3], [hidden]) === "", "a lone hidden Tern pane")
                check(key(null, [alpha]) === "", "no focused task window")
                console.log("PASS actual main focused session")
                Qt.exit(0)
''')


if __name__ == "__main__":
    unittest.main()
