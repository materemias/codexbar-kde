"""Exercise integration-button bursts through the real QML and process plugin."""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
QML = shutil.which("qml6") or shutil.which("qml") or "/usr/lib/qt6/bin/qml"
PLUGIN = ROOT / "build/package/contents/ui/process"


@unittest.skipUnless(Path(QML).is_file() and (PLUGIN / "libcodexbarprocess.so").is_file(),
                     "Build the native package and install the Qt 6 qml tool first")
class IntegrationActionsTest(unittest.TestCase):
    def test_bursts_serialize_but_later_click_runs(self):
        with tempfile.TemporaryDirectory(prefix="codexbar-actions-") as directory:
            root = Path(directory)
            (root / "ui").mkdir()
            (root / "scripts").mkdir()
            for name in ("configAgents.qml", "Command.js"):
                shutil.copy2(ROOT / "contents/ui" / name, root / "ui" / name)
            (root / "ui/process").symlink_to(PLUGIN, target_is_directory=True)
            # Replace only the external installer: never mutate real desktop settings.
            (root / "scripts/install_integration.py").write_text(
                "import pathlib,time\n"
                "p=pathlib.Path(__file__).with_name('calls')\n"
                "with p.open('a') as f: f.write('call\\n')\n"
                "time.sleep(0.2)\n"
                "print(len(p.read_text().splitlines()))\n"
            )
            (root / "test.qml").write_text('''import QtQuick
Item {
    id: root
    property int stage: 0
    Loader {
        id: page
        source: "ui/configAgents.qml"
        onLoaded: { item._runHookCommand("--status"); item._runHookCommand("--status") }
    }
    Timer {
        interval: 50; running: true; repeat: true
        onTriggered: {
            if (!page.item || page.item.hookLoading
                    || page.item.hookStatus === "checking…" || page.item.hookStatus === "") return
            var expected = root.stage + 1
            if (!page.item || page.item.hookStatus !== String(expected)) {
                console.error("FAIL integration commands overlapped; expected " + expected
                    + " invocation(s), got " + (page.item ? page.item.hookStatus : "no page"))
                Qt.exit(1); return
            }
            if (root.stage === 1) {
                console.log("PASS integration bursts serialize; subsequent click runs fresh")
                Qt.exit(0); return
            }
            root.stage = 1
            page.item.hookStatus = ""
            page.item._runHookCommand("--status")
            page.item._runHookCommand("--status")
        }
    }
}
''')
            env = dict(os.environ, QT_QPA_PLATFORM="offscreen",
                       QT_FORCE_STDERR_LOGGING="1", QT_LOGGING_TO_CONSOLE="1")
            result = subprocess.run([QML, str(root / "test.qml")], env=env,
                                    capture_output=True, text=True, timeout=15)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, output)
            self.assertIn("PASS integration bursts serialize", output)
            self.assertEqual((root / "scripts/calls").read_text().splitlines(),
                             ["call", "call"])


if __name__ == "__main__":
    unittest.main()
