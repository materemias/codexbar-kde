from __future__ import annotations

import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SCRIPTS = Path(__file__).resolve().parents[1] / "contents" / "scripts"
sys.path.insert(0, str(SCRIPTS))
import codexbar_focus as focus  # noqa: E402


def entry(**changes: object) -> dict:
    record = {
        "provider": "omp",
        "sessionId": "sid-1",
        "cwd": "/work/project",
        "host": "kitty",
        "desktop": "3",
        "closedBy": "exit",
        "resumeCommand": "cd -- /work/project && omp --resume sid-1",
    }
    record.update(changes)
    return record


class T3LinkTests(unittest.TestCase):
    def test_link_maps_claude_and_codex_sessions_to_t3_threads(self) -> None:
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "environment-id").write_text("env-1\n")
            with sqlite3.connect(root / "state.sqlite") as db:
                db.execute("CREATE TABLE provider_session_runtime"
                           " (thread_id TEXT, last_seen_at TEXT, resume_cursor_json TEXT)")
                db.executemany("INSERT INTO provider_session_runtime VALUES (?, ?, ?)", [
                    ("t-claude", "2", json.dumps({"threadId": "t-claude", "resume": "claude-sid"})),
                    ("t-codex", "1", json.dumps({"threadId": "codex-sid"})),
                ])
            with mock.patch.object(focus, "T3_DIR", root):
                self.assertEqual(focus._t3_thread_link("claude-sid"), "t3code://threads/env-1/t-claude")
                self.assertEqual(focus._t3_thread_link("codex-sid"), "t3code://threads/env-1/t-codex")
                self.assertEqual(focus._t3_thread_link("other"), "")
            with mock.patch.object(focus, "T3_DIR", root / "missing"):
                self.assertEqual(focus._t3_thread_link("claude-sid"), "")


class LaunchTests(unittest.TestCase):
    def launch(self, payload: dict, desktops: int = 6):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            path.write_text(json.dumps(payload))
            popen = mock.MagicMock()
            popen.return_value.pid = 4242
            stderr = io.StringIO()
            with (
                mock.patch.object(focus, "AGGREGATE_PATH", path),
                mock.patch.object(focus.subprocess, "Popen", popen),
                mock.patch.object(focus, "_desktop_count", return_value=desktops),
                mock.patch.object(focus, "_kwin_place", return_value=True) as place,
                mock.patch.object(focus, "_set_current_desktop") as switch,
                mock.patch.object(focus.sys, "stderr", stderr),
            ):
                code = focus.launch("omp", "sid-1")
        return code, popen, place, switch, stderr.getvalue()

    def test_verified_kitty_record_switches_desktop_then_launches_in_scope(self) -> None:
        code, popen, place, switch, _ = self.launch({"agents": [], "history": [entry()]})
        self.assertEqual(code, 0)
        argv = popen.call_args.args[0]
        self.assertEqual(argv[:4], ["systemd-run", "--user", "--scope", "--quiet"])
        kitty = argv.index("kitty")
        self.assertEqual(argv[kitty + 1:kitty + 3], ["--directory", "/work/project"])
        self.assertIn("omp --resume sid-1", argv[-1])
        switch.assert_called_once_with(3)
        place.assert_called_once_with(4242, "3")

    def test_refusals_never_spawn_or_switch(self) -> None:
        cases = {
            5: {"agents": [{"provider": "omp", "sessionId": "sid-1"}],
                "history": [entry()]},
            2: {"agents": [], "history": []},
            6: {"agents": [], "history": [entry(resumeCommand="rm -rf /")]},
            7: {"agents": [], "history": [entry(host="konsole")]},
        }
        for expected, payload in cases.items():
            with self.subTest(expected=expected):
                code, popen, place, switch, _ = self.launch(payload)
                self.assertEqual(code, expected)
                popen.assert_not_called()
                place.assert_not_called()
                switch.assert_not_called()

    def test_missing_desktop_opens_on_current_and_reports_it(self) -> None:
        code, popen, place, switch, err = self.launch(
            {"agents": [], "history": [entry(desktop="9")]}, desktops=6
        )
        self.assertEqual(code, 0)
        popen.assert_called_once()
        place.assert_not_called()
        switch.assert_not_called()
        self.assertIn("desktop 9 no longer exists", err)

    def test_launch_all_restores_reboot_rows_in_desktop_order(self) -> None:
        def row(sid: str, **changes: object) -> dict:
            record = entry(sessionId=sid, closedBy="reboot",
                           resumeCommand=f"cd -- /work/project && omp --resume {sid}")
            record.update(changes)
            return record
        payload = {
            "agents": [{"provider": "omp", "sessionId": "live"}],
            "history": [
                row("unknown", desktop=""), row("d5", desktop="5"),
                row("d2", desktop="2"), row("live", desktop="1"),
                row("konsole", desktop="1", host="konsole"),
                row("no-cmd", desktop="1", resumeCommand=""),
                entry(sessionId="exit-row", desktop="1", closedBy="exit",
                      resumeCommand="cd -- /work/project && omp --resume exit-row"),
                row("hidden", desktop="1"),
            ],
        }
        requested = {("omp", sid) for sid in
                     ("unknown", "d5", "d2", "live", "konsole", "no-cmd", "exit-row")}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            path.write_text(json.dumps(payload))
            stdout = io.StringIO()
            with (
                mock.patch.object(focus, "AGGREGATE_PATH", path),
                mock.patch.object(focus, "launch",
                                  side_effect=lambda _p, sid: 8 if sid == "d5" else 0) as launch,
                mock.patch.object(focus.sys, "stdout", stdout),
            ):
                code = focus.launch_all(requested)
        self.assertEqual(code, 0)
        # Unrequested rows never launch, and a failed launch is not reported.
        self.assertEqual([c.args[1] for c in launch.call_args_list],
                         ["d2", "d5", "unknown"])
        self.assertEqual(json.loads(stdout.getvalue()),
                         [["omp", "d2"], ["omp", "unknown"]])


if __name__ == "__main__":
    unittest.main()
