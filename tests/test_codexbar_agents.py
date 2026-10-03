from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import shlex
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
from datetime import datetime, timezone
from unittest import mock
from urllib.parse import quote


SCRIPT_PATH = (
    Path(__file__).resolve().parents[1]
    / "contents"
    / "scripts"
    / "codexbar_agents.py"
)
SPEC = importlib.util.spec_from_file_location("codexbar_agents", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
agents = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(agents)


HISTORY_FIELDS = {
    "provider",
    "sessionId",
    "cwd",
    "windowTitle",
    "lastPrompt",
    "host",
    "desktop",
    "lastState",
    "lastSeenAt",
    "model",
    "resumeCommand",
    "codexHome",
    "closedBy",
    "recent",
    "recap",
}


def active(provider: str, session_id: str, **changes: object) -> dict:
    record = {
        "provider": provider,
        "sessionId": session_id,
        "cwd": "/work/project",
        "windowTitle": "Project agent",
        "lastPrompt": "Fix the state",
        "host": "kitty",
        "desktop": "4",
        "model": "provider/model",
        "state": "idle",
        "updatedAt": 100,
        "startedAt": 50,
        "stateChangedAt": 75,
        "identityExact": True,
        "pid": 123,
        "ancestorPids": [123, 100],
        "recent": [{"role": "user", "text": "private live data"}],
    }
    record.update(changes)
    return record


def history(provider: str, session_id: str, **changes: object) -> dict:
    record = {
        "provider": provider,
        "sessionId": session_id,
        "cwd": "/work/project",
        "windowTitle": "Project agent",
        "lastPrompt": "Fix the state",
        "host": "kitty",
        "desktop": "4",
        "model": "provider/model",
        "lastState": "idle",
        "lastSeenAt": 100,
        "resumeCommand": f"{provider} resume {session_id}",
        "closedBy": "reboot",
    }
    record.update(changes)
    return record


class ModelExtractionTests(unittest.TestCase):
    def test_model_text_is_bounded_and_whitespace_normalized(self) -> None:
        self.assertEqual(agents._model_text("  provider/model\n"), "provider/model")
        self.assertEqual(agents._model_text("x" * 200), "x" * 160)
        self.assertEqual(agents._model_text({"model": "bad"}), "")

    def test_claude_uses_latest_assistant_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            rows = [
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "anthropic/claude-sonnet-4",
                        "content": "first",
                    },
                },
                {
                    "type": "assistant",
                    "message": {
                        "role": "assistant",
                        "model": "anthropic/claude-opus-5",
                        "content": "latest",
                    },
                },
            ]
            path.write_text("\n".join(
                json.dumps(row, separators=(",", ":")) for row in rows
            ) + "\n")

            model = agents._transcript_info(str(path), "claude")["model"]

        self.assertEqual(model, "anthropic/claude-opus-5")

    def test_codex_reads_model_from_turn_context(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "codex.jsonl"
            path.write_text("\n".join([
                json.dumps({
                    "type": "session_meta",
                    "payload": {"id": "codex-id", "cwd": "/work"},
                }),
                json.dumps({
                    "type": "turn_context",
                    "payload": {"model": "openai-codex/gpt-5.6-sol"},
                }),
            ]) + "\n")
            with mock.patch.object(
                agents, "_open_jsonl_under", return_value=(str(path), True)
            ):
                info = agents._codex_info(42)

        self.assertEqual(info["model"], "openai-codex/gpt-5.6-sol")

    def test_pi_uses_latest_model_change_or_assistant_model(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.jsonl"
            path.write_text("\n".join([
                json.dumps({
                    "type": "session", "id": "pi-id", "cwd": "/work"
                }),
                json.dumps({
                    "type": "model_change", "model": "zai/glm-5.3-flash"
                }),
                json.dumps({
                    "type": "message",
                    "message": {
                        "role": "assistant",
                        "model": "anthropic/claude-opus-5",
                        "content": "done",
                    },
                }),
            ]) + "\n")
            with mock.patch.object(
                agents, "_open_jsonl_under", return_value=(str(path), True)
            ):
                info = agents._pi_info(42)

        self.assertEqual(info["model"], "anthropic/claude-opus-5")

    def test_opencode_reads_latest_assistant_model_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            db_path = home / ".local" / "share" / "opencode" / "opencode.db"
            db_path.parent.mkdir(parents=True)
            con = sqlite3.connect(db_path)
            try:
                con.executescript("""
                    CREATE TABLE session (
                        id TEXT, directory TEXT, title TEXT, time_updated INTEGER
                    );
                    CREATE TABLE message (
                        id TEXT, session_id TEXT, time_created INTEGER, data TEXT
                    );
                    CREATE TABLE part (
                        message_id TEXT, time_created INTEGER, data TEXT
                    );
                """)
                con.execute(
                    "INSERT INTO session VALUES (?, ?, ?, ?)",
                    ("session-id", "/work", "OpenCode", 1),
                )
                con.execute(
                    "INSERT INTO message VALUES (?, ?, ?, ?)",
                    (
                        "message-id",
                        "session-id",
                        1,
                        json.dumps({
                            "role": "assistant",
                            "model_id": "openai/gpt-5.6",
                        }),
                    ),
                )
                con.commit()
            finally:
                con.close()

            with (
                mock.patch.object(agents.Path, "home", return_value=home),
                mock.patch.object(agents, "_cwd_of", return_value="/work"),
            ):
                info = agents._opencode_info(42)

        self.assertEqual(info["model"], "openai/gpt-5.6")


class SnapshotMergeTests(unittest.TestCase):
    def test_boot_change_recovers_exact_omp_and_live_return_removes_it(self) -> None:
        old = active(
            "omp",
            "session id's",
            cwd="/work/O'Brien project",
            desktop="4",
        )
        previous = {"bootId": "boot-a", "agents": [old], "history": []}

        snapshot = agents._merge_snapshot([], previous, "boot-b", 200)

        self.assertEqual(
            set(snapshot),
            {"bootId", "updatedAt", "counts", "agents", "history", "ternInstalled"},
        )
        self.assertEqual(snapshot["bootId"], "boot-b")
        self.assertEqual(snapshot["agents"], [])
        self.assertEqual(len(snapshot["history"]), 1)
        restored = snapshot["history"][0]
        self.assertEqual(set(restored), HISTORY_FIELDS)
        self.assertEqual(restored["closedBy"], "reboot")
        self.assertEqual(restored["cwd"], "/work/O'Brien project")
        self.assertEqual(restored["desktop"], "4")
        self.assertEqual(restored["lastSeenAt"], 100)
        self.assertEqual(
            restored["resumeCommand"],
            f"{shlex.join(['cd', '--', old['cwd']])} && "
            f"{shlex.join(['omp', '--resume', old['sessionId']])}",
        )
        self.assertTrue(
            {"pid", "ancestorPids", "identityExact"}
            .isdisjoint(restored)
        )

        live = active("omp", old["sessionId"], updatedAt=250)
        resumed = agents._merge_snapshot([live], snapshot, "boot-b", 250)
        self.assertEqual(resumed["history"], [])
        self.assertEqual(len(resumed["agents"]), 1)

    def test_history_keeps_bounded_peek_turns(self) -> None:
        turns = [
            {"role": "user", "kind": "text", "text": f"turn {n}", "ts": n}
            for n in range(10)
        ]
        turns[-1] = {"role": "assistant", "kind": "text", "text": "x" * 999, "ts": 9}
        turns[-2] = {"role": "system", "kind": "text", "text": "dropped", "ts": 8}
        previous = {
            "bootId": "boot-a",
            "agents": [active("omp", "peek", recent=turns)],
            "history": [],
        }
        row = agents._merge_snapshot([], previous, "boot-a", 200)["history"][0]
        self.assertEqual(
            [turn["text"] for turn in row["recent"]],
            [f"turn {n}" for n in range(2, 8)] + ["x" * 320],
        )
        self.assertEqual(set(row["recent"][0]), {"role", "kind", "text", "ts"})

    def test_unresolved_reboot_history_survives_reboots_and_unions_new_active(self) -> None:
        first = agents._merge_snapshot(
            [],
            {"bootId": "boot-a", "agents": [active("omp", "old")], "history": []},
            "boot-b",
            200,
        )
        second = agents._merge_snapshot([], first, "boot-c", 300)
        self.assertEqual(
            [(row["provider"], row["sessionId"]) for row in second["history"]],
            [("omp", "old")],
        )

        with_new_live = agents._merge_snapshot(
            [active("claude", "new", desktop="2")], second, "boot-c", 350
        )
        later = agents._merge_snapshot([], with_new_live, "boot-d", 400)
        self.assertEqual(
            {(row["provider"], row["sessionId"]) for row in later["history"]},
            {("omp", "old"), ("claude", "new")},
        )

    def test_just_ended_active_record_wins_history_duplicate(self) -> None:
        previous = {
            "bootId": "boot-a",
            "agents": [
                active(
                    "omp", "same", cwd="/new cwd", desktop="5", updatedAt=200
                )
            ],
            "history": [
                history(
                    "omp", "same", cwd="/old cwd", desktop="2", lastSeenAt=100
                )
            ],
        }
        snapshot = agents._merge_snapshot([], previous, "boot-b", 300)
        self.assertEqual(len(snapshot["history"]), 1)
        self.assertEqual(snapshot["history"][0]["cwd"], "/new cwd")
        self.assertEqual(snapshot["history"][0]["desktop"], "5")
        self.assertEqual(snapshot["history"][0]["lastSeenAt"], 200)

    def test_same_boot_exit_enters_history_newest_first_and_capped(self) -> None:
        limit = 3
        ended = [
            active("omp", f"exit-{n}", updatedAt=1000 + n)
            for n in range(limit + 3)
        ]
        previous = {
            "bootId": "boot-a",
            "agents": [
                *ended,
                active("omp", "untracked-omp-7", state="untracked"),
            ],
            "history": [
                history("omp", f"rebooted-{n}", lastSeenAt=1) for n in range(limit + 1)
            ],
        }
        snapshot = agents._merge_snapshot([], previous, "boot-a", 5000, limit)
        rows = [(row["sessionId"], row["closedBy"]) for row in snapshot["history"]]
        newest = limit + 2
        self.assertEqual(
            rows,
            [(f"rebooted-{n}", "reboot") for n in range(limit + 1)]
            + [(f"exit-{n}", "exit")
               for n in range(newest, newest - limit, -1)],
        )

    def test_identity_change_of_a_live_process_is_not_an_exit(self) -> None:
        previous = {
            "bootId": "boot-a",
            "agents": [active("omp", "startup-guess", pid=77, identityExact=False)],
            "history": [],
        }
        snapshot = agents._merge_snapshot(
            [active("omp", "resumed", pid=77)], previous, "boot-a", 200
        )
        self.assertEqual(snapshot["history"], [])

    def test_relaunched_session_keeps_history_desktop_through_a_short_run(self) -> None:
        previous = {
            "bootId": "boot-a",
            "agents": [],
            "history": [history("omp", "again", closedBy="exit", desktop="3")],
        }
        live = active("omp", "again", pid=88)
        live.pop("desktop")
        running = agents._merge_snapshot([live], previous, "boot-a", 200)
        self.assertEqual(running["history"], [])
        self.assertEqual(running["agents"][0]["desktop"], "3")

        closed = agents._merge_snapshot([], running, "boot-a", 300)
        self.assertEqual(
            [(row["sessionId"], row["desktop"]) for row in closed["history"]],
            [("again", "3")],
        )

    def test_cross_provider_session_ids_remain_distinct(self) -> None:
        previous = {
            "bootId": "boot-a",
            "agents": [active("claude", "shared"), active("omp", "shared")],
            "history": [],
        }
        snapshot = agents._merge_snapshot([], previous, "boot-b", 200)
        self.assertEqual(
            {(row["provider"], row["sessionId"]) for row in snapshot["history"]},
            {("claude", "shared"), ("omp", "shared")},
        )

    def test_legacy_payload_records_boot_without_inventing_history(self) -> None:
        previous = {"agents": [active("omp", "legacy")], "history": []}
        snapshot = agents._merge_snapshot([], previous, "boot-a", 200)
        self.assertEqual(snapshot["bootId"], "boot-a")
        self.assertEqual(snapshot["history"], [])

    def test_unreadable_boot_id_keeps_saved_boot_and_only_existing_history(self) -> None:
        existing = history("omp", "unresolved")
        previous = {
            "bootId": "known-boot",
            "agents": [active("claude", "active-before-unknown")],
            "history": [existing],
        }
        snapshot = agents._merge_snapshot([], previous, "", 200)
        self.assertEqual(snapshot["bootId"], "known-boot")
        self.assertEqual(
            [(row["provider"], row["sessionId"]) for row in snapshot["history"]],
            [("omp", "unresolved")],
        )

    def test_malformed_previous_entries_are_ignored(self) -> None:
        previous = {
            "bootId": "boot-a",
            "agents": [
                None,
                {},
                {"provider": "omp", "sessionId": ""},
                {"provider": 3, "sessionId": "bad"},
                active("omp", "valid"),
            ],
            "history": [None, {"provider": "omp", "sessionId": []}],
        }
        snapshot = agents._merge_snapshot([], previous, "boot-b", 200)
        self.assertEqual(
            [(row["provider"], row["sessionId"]) for row in snapshot["history"]],
            [("omp", "valid")],
        )

    def test_same_boot_carries_timers_and_desktop_but_new_boot_does_not(self) -> None:
        old = active(
            "omp", "carry", desktop="6", startedAt=10, stateChangedAt=20
        )
        current = active(
            "omp", "carry", desktop=None, model="", startedAt=0, stateChangedAt=100
        )
        current.pop("desktop")
        previous = {"bootId": "boot-a", "agents": [old], "history": []}

        same = agents._merge_snapshot([current], previous, "boot-a", 200)
        self.assertEqual(same["agents"][0]["desktop"], "6")
        self.assertEqual(same["agents"][0]["startedAt"], 10)
        self.assertEqual(same["agents"][0]["stateChangedAt"], 20)
        self.assertEqual(same["agents"][0]["model"], "provider/model")

        changed = agents._merge_snapshot([current], previous, "boot-b", 200)
        self.assertNotIn("desktop", changed["agents"][0])
        self.assertEqual(changed["agents"][0]["startedAt"], 0)
        self.assertEqual(changed["agents"][0]["stateChangedAt"], 100)


class BoundaryParsingTests(unittest.TestCase):
    def test_load_payload_normalizes_top_level_and_record_collections(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            path.write_text("[]")
            self.assertEqual(
                agents._load_payload(path), {"agents": [], "history": []}
            )

            path.write_text(json.dumps({
                "bootId": "boot-a",
                "agents": [None, {"provider": "omp", "sessionId": "ok"}, "bad"],
                "history": "not-a-list",
            }))
            payload = agents._load_payload(path)
            self.assertEqual(payload["agents"], [{"provider": "omp", "sessionId": "ok"}])
            self.assertEqual(payload["history"], [])

            path.write_text("{not json")
            self.assertEqual(
                agents._load_payload(path), {"agents": [], "history": []}
            )

    def test_dismiss_removes_only_named_history_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            live = active("omp", "keep-live")
            agents._write_aggregate({
                "bootId": "boot-a", "agents": [live],
                "history": [history("omp", "a"), history("claude", "a"),
                            history("omp", "b", closedBy="exit")],
            }, path)
            removed = agents._dismiss_history(
                agents._parse_session_keys(quote(json.dumps([["omp", "a"], ["omp", "b"]]))),
                path,
            )
            payload = agents._load_payload(path)
        self.assertEqual(removed, 2)
        self.assertEqual(
            [(r["provider"], r["sessionId"]) for r in payload["history"]],
            [("claude", "a")],
        )
        self.assertEqual(payload["agents"], [live])

    def test_dismiss_argument_rejects_malformed_pairs(self) -> None:
        for value in (None, "", "%7Bnot", quote(json.dumps({"omp": "a"}))):
            self.assertEqual(agents._parse_session_keys(value), set())
        self.assertEqual(
            agents._parse_session_keys(quote(json.dumps(
                [["omp", "a"], ["unknown", "b"], ["omp", ""], ["omp"], "x"]
            ))),
            {("omp", "a")},
        )

    def test_boot_id_reader_returns_empty_for_missing_and_empty_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "boot_id"
            with mock.patch.object(agents, "BOOT_ID_PATH", path):
                self.assertEqual(agents._read_boot_id(), "")
                path.write_text("   \n")
                self.assertEqual(agents._read_boot_id(), "")
                path.write_text("boot-id\n")
                self.assertEqual(agents._read_boot_id(), "boot-id")

    def test_percent_encoded_desktop_map_uses_composite_identity(self) -> None:
        encoded = quote(json.dumps([
            {"provider": "omp", "sessionId": "same", "desktop": "4"},
            {"provider": "claude", "sessionId": "same", "desktop": "all"},
        ]), safe="")
        parsed = agents._parse_desktop_map(encoded)
        self.assertEqual(parsed, {
            ("omp", "same"): "4",
            ("claude", "same"): "all",
        })

        mapped = agents._apply_desktop_map(
            [active("omp", "same", desktop=None), active("claude", "same", desktop=None)],
            parsed,
        )
        self.assertEqual(
            {(row["provider"], row["desktop"]) for row in mapped},
            {("omp", "4"), ("claude", "all")},
        )

    def test_invalid_desktop_map_data_is_ignored(self) -> None:
        self.assertEqual(agents._parse_desktop_map(None), {})
        self.assertEqual(agents._parse_desktop_map("%5Bbad"), {})
        self.assertEqual(agents._parse_desktop_map(quote(json.dumps({}), safe="")), {})
        self.assertEqual(agents._parse_desktop_map("x" * (64 * 1024 + 1)), {})

        mixed = quote(json.dumps([
            None,
            {"provider": "other", "sessionId": "sid", "desktop": "1"},
            {"provider": [], "sessionId": "sid", "desktop": "1"},
            {"provider": "omp", "sessionId": "", "desktop": "1"},
            {"provider": "omp", "sessionId": "x" * 257, "desktop": "1"},
            {"provider": "omp", "sessionId": "zero", "desktop": "0"},
            {"provider": "omp", "sessionId": "negative", "desktop": "-1"},
            {"provider": "omp", "sessionId": "word", "desktop": "desk"},
            {"provider": "omp", "sessionId": "huge", "desktop": "1" * 5000},
            {"provider": "omp", "sessionId": "valid", "desktop": "2"},
        ]), safe="")
        self.assertEqual(agents._parse_desktop_map(mixed), {("omp", "valid"): "2"})

    def test_requested_at_accepts_only_nonnegative_decimal(self) -> None:
        self.assertEqual(agents._parse_requested_at("123"), 123)
        for value in (None, "", "-1", "1.0", " 1", "abc", "1" * 21):
            self.assertIsNone(agents._parse_requested_at(value))


class ResumeAndIdentityTests(unittest.TestCase):
    def test_resume_commands_quote_every_word(self) -> None:
        cwd = "/work/O'Brien project"
        session_id = "session id's"
        expected_args = {
            "claude": ["claude", "--resume", session_id],
            "codex": ["codex", "resume", session_id],
            "opencode": ["opencode", "--session", session_id],
            "pi": ["pi", "--session", session_id],
            "omp": ["omp", "--resume", session_id],
        }
        for provider, command in expected_args.items():
            with self.subTest(provider=provider):
                self.assertEqual(
                    agents._resume_command(provider, session_id, cwd, True),
                    f"{shlex.join(['cd', '--', cwd])} && {shlex.join(command)}",
                )

    def test_heuristic_untracked_and_invalid_identities_have_no_command(self) -> None:
        self.assertEqual(agents._resume_command("omp", "heuristic", "/tmp", False), "")
        self.assertEqual(
            agents._resume_command("omp", "untracked-omp-42", "/tmp", False), ""
        )
        self.assertEqual(agents._resume_command("unknown", "sid", "/tmp", True), "")
        self.assertEqual(agents._resume_command("omp", "", "/tmp", True), "")
        self.assertEqual(agents._resume_command("omp", "x" * 257, "/tmp", True), "")
        self.assertEqual(agents._resume_command("omp", "bad\0id", "/tmp", True), "")

        snapshot = agents._merge_snapshot(
            [
                active("opencode", "heuristic", identityExact=False),
                active(
                    "omp",
                    "untracked-omp-42",
                    identityExact=False,
                    cwd="/tmp",
                ),
            ],
            {"bootId": "boot-a", "agents": [], "history": []},
            "boot-a",
            100,
        )
        self.assertEqual(
            [row["resumeCommand"] for row in snapshot["agents"]], ["", ""]
        )

    def test_saved_history_command_must_match_canonical_command(self) -> None:
        valid = history(
            "omp",
            "safe-session",
            cwd="/work/project",
            resumeCommand="cd -- /work/project && omp --resume safe-session",
        )
        tampered = history(
            "omp",
            "tampered-session",
            cwd="/work/project",
            resumeCommand="rm -rf /",
        )
        snapshot = agents._merge_snapshot(
            [],
            {
                "bootId": "boot-a",
                "agents": [],
                "history": [valid, tampered],
            },
            "boot-a",
            100,
        )
        commands = {
            row["sessionId"]: row["resumeCommand"]
            for row in snapshot["history"]
        }
        self.assertEqual(
            commands["safe-session"],
            "cd -- /work/project && omp --resume safe-session",
        )
        self.assertEqual(commands["tampered-session"], "")

    def test_open_rollout_reports_direct_and_sidecar_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory) / ".omp" / "agent" / "sessions" / "project"
            base.mkdir(parents=True)
            rollout = base / "session.jsonl"
            rollout.write_text("{}\n")

            with (
                mock.patch.object(agents.os, "listdir", return_value=["3"]),
                mock.patch.object(agents.os, "readlink", return_value=str(rollout)),
            ):
                self.assertEqual(
                    agents._open_jsonl_under(42, "/.omp/agent/sessions/"),
                    (str(rollout), True),
                )

            sidecar_dir = base / "sidecar-owner"
            sidecar_dir.mkdir()
            owner = base / "sidecar-owner.jsonl"
            owner.write_text("{}\n")
            sidecar = sidecar_dir / "__advisor.test.jsonl"
            sidecar.write_text("{}\n")
            with (
                mock.patch.object(agents.os, "listdir", return_value=["4"]),
                mock.patch.object(agents.os, "readlink", return_value=str(sidecar)),
            ):
                self.assertEqual(
                    agents._open_jsonl_under(42, "/.omp/agent/sessions/"),
                    (str(owner), False),
                )

    def test_provider_extractors_set_identity_exact_only_from_direct_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            claude_state = home / ".claude" / "sessions"
            claude_state.mkdir(parents=True)
            (claude_state / "42.json").write_text(json.dumps({
                "sessionId": "claude-id", "cwd": "/work", "status": "idle"
            }))
            with mock.patch.object(agents.Path, "home", return_value=home):
                self.assertTrue(agents._claude_info(42)["identityExact"])
                self.assertFalse(agents._opencode_info(42)["identityExact"])

            codex_rollout = home / "codex.jsonl"
            codex_rollout.write_text(json.dumps({
                "type": "session_meta",
                "payload": {"id": "codex-id", "cwd": "/work"},
            }) + "\n")
            with mock.patch.object(
                agents, "_open_jsonl_under", return_value=(str(codex_rollout), True)
            ):
                self.assertTrue(agents._codex_info(42)["identityExact"])
            with mock.patch.object(
                agents, "_open_jsonl_under", return_value=(str(codex_rollout), False)
            ):
                self.assertFalse(agents._codex_info(42)["identityExact"])

            pi_rollout = home / "pi.jsonl"
            pi_rollout.write_text(json.dumps({
                "type": "session", "id": "pi-id", "cwd": "/work"
            }) + "\n")
            with mock.patch.object(
                agents, "_open_jsonl_under", return_value=(str(pi_rollout), True)
            ):
                self.assertTrue(agents._pi_info(42)["identityExact"])
            with (
                mock.patch.object(agents, "_open_jsonl_under", return_value=("", False)),
                mock.patch.object(agents, "_session_dir_of", return_value=""),
                mock.patch.object(agents, "_find_pi_rollout", return_value=str(pi_rollout)),
                mock.patch.object(agents, "_cwd_of", return_value="/work"),
            ):
                self.assertFalse(agents._pi_info(42)["identityExact"])

    def test_resuming_omp_is_named_by_argv_before_it_opens_its_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            slug = home / ".omp" / "agent" / "sessions" / "-work"
            slug.mkdir(parents=True)
            resumed = slug / "2026-09-28T10-00-00-000Z_01a0-resumed.jsonl"
            resumed.write_text(json.dumps({"type": "session", "id": "01a0-resumed", "cwd": "/work"}) + "\n")
            other = slug / "2026-09-28T11-00-00-000Z_01a0-other.jsonl"
            other.write_text(json.dumps({"type": "session", "id": "01a0-other", "cwd": "/work"}) + "\n")
            with (
                mock.patch.object(agents.Path, "home", return_value=home),
                mock.patch.object(agents, "_open_jsonl_under", return_value=("", False)),
                mock.patch.object(agents, "_argv_of", return_value=["omp", "--resume", "01a0-resumed"]),
                mock.patch.object(agents, "_find_pi_rollout", return_value=str(other)),
                mock.patch.object(agents, "_cwd_of", return_value="/work"),
            ):
                info = agents._pi_info(42)
        self.assertEqual(info["sessionId"], "01a0-resumed")
        self.assertTrue(info["identityExact"])

    def test_codex_tuis_match_threads_loaded_by_shared_app_server(self) -> None:
        start = 1_790_000_000_000
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory) / "codex-work"
            day = home / "sessions" / "2026" / "09" / "28"
            day.mkdir(parents=True)

            def thread(sid: str, cwd: str, created: int) -> str:
                path = day / f"rollout-x-{sid}.jsonl"
                iso = datetime.fromtimestamp(created / 1000, timezone.utc).isoformat()
                path.write_text(json.dumps({"type": "session_meta", "payload": {
                    "id": sid, "cwd": cwd, "timestamp": iso}}) + "\n")
                return str(path)

            pool = {
                thread("mine", "/work", start + 5000),
                # Closed a minute ago but still loaded by the server.
                thread("stale", "/work", start - 60_000),
                thread("elsewhere", "/other", start + 1000),
            }
            (home / "session_index.jsonl").write_text(
                json.dumps({"id": "mine", "thread_name": "First"}) + "\n"
                + json.dumps({"id": "other", "thread_name": "Nope"}) + "\n"
                + json.dumps({"id": "mine", "thread_name": "Renamed"}) + "\n"
            )
            argv = {42: ["codex"], 43: ["codex"], 44: ["codex", "resume", "stale"]}
            tuis = [42, 44]

            def run(pid: int) -> dict:
                with (
                    mock.patch.object(agents, "_environ_value", return_value=str(home)),
                    mock.patch.object(agents, "_pgrep", side_effect=lambda _n, services=False:
                                      [77] if services else list(tuis)),
                    mock.patch.object(agents, "_held_rollouts", side_effect=lambda server, _n:
                                      set(pool) if server == 77 else set()),
                    mock.patch.object(agents, "_open_jsonl_under", return_value=("", False)),
                    mock.patch.object(agents, "_argv_of", side_effect=lambda p: argv[p]),
                    mock.patch.object(agents, "_cwd_of", return_value="/work"),
                    mock.patch.object(agents, "_start_ms", return_value=start),
                    mock.patch.object(agents, "_transcript_info", side_effect=lambda path, _p: {
                        "sessionId": agents._codex_meta(path)[0], "cwd": "/work",
                        "windowTitle": "", "lastPrompt": "", "state": "idle", "model": ""}),
                ):
                    return agents._codex_info(pid)

            fresh = run(42)
            resumed = run(44)
            tuis.append(43)
            crowded = run(42)
        self.assertEqual((fresh["sessionId"], fresh["identityExact"]), ("mine", True))
        self.assertEqual(fresh["windowTitle"], "Renamed")
        self.assertEqual((resumed["sessionId"], resumed["identityExact"]), ("stale", True))
        # A second fresh TUI in the same folder could own that thread too.
        self.assertEqual((crowded["sessionId"], crowded["identityExact"]), ("mine", False))
        self.assertEqual(
            agents._resume_command("codex", "mine", "/work", True, fresh["codexHome"]),
            f"cd -- /work && CODEX_HOME={shlex.quote(str(home))} codex resume mine",
        )
        # The home only ever prefixes Codex, and a malformed one is dropped.
        self.assertEqual(agents._resume_command("claude", "s", "", True, str(home)),
                         "claude --resume s")
        self.assertEqual(agents._resume_command("codex", "s", "", True, "rel/home"),
                         "codex resume s")

    def test_pi_launched_by_a_shell_is_named_by_that_command(self) -> None:
        script = "cd -- /work && pi --session 01a0-launched; exec /usr/bin/zsh -i"
        with mock.patch.object(agents, "_argv_of", return_value=["/usr/bin/zsh", "-ic", script]):
            self.assertEqual(agents._shell_resume_arg(42, "pi", "--session"), "01a0-launched")
            self.assertEqual(agents._shell_resume_arg(42, "omp", "--session"), "")
        with mock.patch.object(agents, "_argv_of", return_value=["zsh", "-i"]):
            self.assertEqual(agents._shell_resume_arg(42, "pi", "--session"), "")
        self.assertEqual(agents._flag_value(["codex", "resume", "--last"], "resume"), "")

    def test_fresh_pi_session_is_exact_only_when_alone_in_its_start_window(self) -> None:
        start = 1_790_000_000_000

        def header(sid: str, at_ms: int) -> str:
            iso = datetime.fromtimestamp(at_ms / 1000, timezone.utc).isoformat()
            return json.dumps({"type": "session", "id": sid, "timestamp": iso, "cwd": "/work"}) + "\n"

        with tempfile.TemporaryDirectory() as directory:
            slug = Path(directory)
            mine = slug / "a_mine.jsonl"
            mine.write_text(header("mine", start + 1100))
            old = slug / "b_old.jsonl"
            old.write_text(header("old", start - 3_600_000))
            with mock.patch.object(agents, "_start_ms", return_value=start):
                self.assertTrue(agents._created_by(42, str(mine)))
                self.assertFalse(agents._created_by(42, str(old)))
                # A second run started in the same folder at the same time
                # makes the pick ambiguous.
                (slug / "c_twin.jsonl").write_text(header("twin", start + 4000))
                self.assertFalse(agents._created_by(42, str(mine)))

    def test_pi_resumed_with_session_flag_is_named_by_argv(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            slug = home / ".pi" / "agent" / "sessions" / "--work--"
            slug.mkdir(parents=True)
            resumed = slug / "2026-09-28T10-00-00-000Z_01a0-resumed.jsonl"
            resumed.write_text(json.dumps({"type": "session", "id": "01a0-resumed", "cwd": "/work"}) + "\n")
            with (
                mock.patch.object(agents.Path, "home", return_value=home),
                mock.patch.object(agents, "_open_jsonl_under", return_value=("", False)),
                mock.patch.object(agents, "_argv_of", return_value=["pi", "--session", "01a0-resumed"]),
                mock.patch.object(agents, "_cwd_of", return_value="/work"),
            ):
                info = agents._INFO_FN["pi"](42)
        self.assertEqual(info["sessionId"], "01a0-resumed")
        self.assertTrue(info["identityExact"])

    def test_live_dedup_identity_remains_session_id_only(self) -> None:
        info = lambda _pid: {
            "sessionId": "shared",
            "cwd": "/work",
            "state": "idle",
            "identityExact": True,
        }
        with (
            mock.patch.object(agents, "_INFO_FN", {"claude": info, "omp": info}),
            mock.patch.object(agents, "_pgrep", return_value=[42]),
            mock.patch.object(
                agents, "_parent_walk_for_host", return_value=("kitty", [42, 10])
            ),
        ):
            records = agents._build_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["provider"], "claude")
        self.assertEqual(records[0]["sessionId"], "shared")
        self.assertTrue(records[0]["identityExact"])
        self.assertEqual(records[0]["resumeCommand"], "cd -- /work && claude --resume shared")


class T3CodeTests(unittest.TestCase):
    # T3 Code runs Claude as stream-json and Codex as one app-server per
    # thread, directly under its Electron main process.
    COMM = {10: "claude", 11: "claude", 12: "codex", 5: "t3code", 4: "t3code", 3: "systemd"}
    PPID = {10: 5, 11: 3, 12: 5, 5: 4, 4: 3, 3: 1}
    ARGV = {
        10: ["claude", "--output-format", "stream-json", "--add-dir", "/home/u/code/proj"],
        11: ["claude", "-p", "hi"],
        12: ["codex", "app-server"],
        5: ["/opt/t3code-bin/t3code", "--require", "x"],
        4: ["/opt/t3code-bin/t3code"],
        3: ["/usr/lib/systemd/systemd", "--user"],
    }

    def _patch(self):
        return (
            mock.patch.object(agents, "_comm_of", side_effect=lambda p: self.COMM.get(p, "")),
            mock.patch.object(agents, "_ppid_of", side_effect=lambda p: self.PPID.get(p, 0)),
            mock.patch.object(agents, "_argv_of", side_effect=lambda p: self.ARGV.get(p, [])),
        )

    def test_pgrep_keeps_t3_sessions_interactive(self) -> None:
        out = subprocess.CompletedProcess([], 0, stdout="10\n11\n12\n")
        a, b, c = self._patch()
        with a, b, c, mock.patch.object(agents.subprocess, "run", return_value=out):
            self.assertEqual(agents._pgrep("claude"), [10, 12])
            self.assertEqual(agents._pgrep("claude", services=True), [11])

    def test_host_walk_stops_at_t3_not_code_in_args(self) -> None:
        a, b, c = self._patch()
        with a, b, c, mock.patch.object(agents, "t3_window_pids", return_value=[4, 9]):
            # Window 9 is another T3 window; this session sits under window 4.
            self.assertEqual(agents._parent_walk_for_host(10), ("t3code", [10, 5, 4]))

    def test_service_managed_server_hosts_sessions(self) -> None:
        # T3 Code 0.0.44 runs its server as a systemd user service: a Node
        # single-executable named node-MainThread, away from the window.
        comm = {20: "claude", 21: "codex", 7: "node-MainThread", 6: "node-MainThread",
                3: "systemd", 4: "t3code"}
        ppid = {20: 7, 21: 7, 7: 6, 6: 3, 4: 3, 3: 1}
        argv = {
            20: ["claude", "--output-format", "stream-json"],
            21: ["codex", "app-server"],
            7: ["/home/u/.t3/runtime/versions/0.0.44/t3", "serve"],
            6: ["/home/u/.t3/runtime/versions/0.0.44/t3", "__service-launcher"],
            3: ["/usr/lib/systemd/systemd", "--user"],
            4: ["/opt/t3code-bin/t3code"],
        }

        def readlink(path: str) -> str:
            pid = int(path.split("/")[2])
            if pid not in argv:
                raise OSError
            # The running server's version was deleted by a self-update.
            return argv[pid][0] + (" (deleted)" if pid == 7 else "")

        out = subprocess.CompletedProcess([], 0, stdout="20\n21\n")
        with mock.patch.object(agents, "_comm_of", side_effect=lambda p: comm.get(p, "")), \
                mock.patch.object(agents, "_ppid_of", side_effect=lambda p: ppid.get(p, 0)), \
                mock.patch.object(agents, "_argv_of", side_effect=lambda p: argv.get(p, [])), \
                mock.patch.object(agents.os, "readlink", side_effect=readlink), \
                mock.patch.object(agents, "t3_window_pids", return_value=[4]), \
                mock.patch.object(agents.subprocess, "run", return_value=out):
            self.assertEqual(agents._pgrep("claude"), [20, 21])
            self.assertEqual(agents._parent_walk_for_host(20), ("t3code", [20, 7, 4]))


class OmpRecapTests(unittest.TestCase):
    def test_latest_recap_is_skipped_or_flagged_when_a_later_turn_followed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            db = Path(directory) / "history.db"
            con = sqlite3.connect(db)
            con.execute("CREATE TABLE session_recaps (id INTEGER PRIMARY KEY,"
                        " session_id TEXT, cwd TEXT, recap TEXT, created_at INTEGER)")
            con.executemany(
                "INSERT INTO session_recaps (session_id, cwd, recap, created_at)"
                " VALUES (?, '/w', ?, ?)",
                [("a", "old", 100), ("a", "first", 200), ("a", "Done:\n  next   step", 200),
                 ("b", "stale", 100), ("c", "working", 100)],
            )
            con.commit()
            con.close()
            def records():
                return [
                    {"provider": "omp", "sessionId": "a", "recent": [{"ts": 200_900}]},
                    {"provider": "omp", "sessionId": "b", "recent": [{"ts": 150_000}]},
                ]
            ended, live = records(), records()
            agents._attach_omp_recaps(ended, db)
            agents._attach_omp_recaps(live, db, keep_stale=True)
        self.assertEqual(ended[0]["recap"], "Done: next step")
        self.assertNotIn("recapStale", ended[0])
        self.assertNotIn("recap", ended[1])
        self.assertEqual((live[1]["recap"], live[1]["recapStale"]), ("stale", True))

    def test_history_keeps_only_a_recap_of_the_final_exchange(self) -> None:
        fresh = active("omp", "fresh", state="idle", recap="Done.")
        stale = active("omp", "stale", state="idle", recap="Earlier.", recapStale=True)
        running = agents._merge_snapshot([fresh, stale], {"bootId": "boot-a"}, "boot-a", 100)
        rows = agents._merge_snapshot([], running, "boot-a", 200)["history"]
        self.assertEqual({row["sessionId"]: row["recap"] for row in rows},
                         {"fresh": "Done.", "stale": ""})

    def test_claude_away_summary_turns_stale_after_the_next_turn(self) -> None:
        summary = {"type": "system", "subtype": "away_summary",
                   "content": "Done.\nNext: ship. (disable recaps in /config)\n"}
        aside = [{"type": "assistant", "isSidechain": True,
                  "message": {"role": "assistant", "content": "subagent"}},
                 {"type": "user", "message": {"role": "user",
                                              "content": "<command-name>/model</command-name>"}}]
        turn = {"type": "user", "message": {"role": "user", "content": "go on"}}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "claude.jsonl"
            path.write_text("".join(json.dumps(r) + "\n" for r in [summary, *aside]))
            info = agents._transcript_info(str(path), "claude")
            self.assertEqual((info["recap"], info["recapStale"]), ("Done. Next: ship.", False))
            path.write_text("".join(json.dumps(r) + "\n" for r in [summary, *aside, turn]))
            info = agents._transcript_info(str(path), "claude")
            self.assertEqual((info["recap"], info["recapStale"]), ("Done. Next: ship.", True))

    def test_legacy_history_row_rejects_recap_older_than_its_rollout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / ".omp" / "agent").mkdir(parents=True)
            con = sqlite3.connect(home / ".omp" / "agent" / "history.db")
            con.execute("CREATE TABLE session_recaps (id INTEGER PRIMARY KEY,"
                        " session_id TEXT, cwd TEXT, recap TEXT, created_at INTEGER)")
            con.execute("INSERT INTO session_recaps (session_id, cwd, recap, created_at)"
                        " VALUES ('old', '/w', 'earlier exchange', 100)")
            con.commit()
            con.close()
            slug = home / ".omp" / "agent" / "sessions" / "-w"
            slug.mkdir(parents=True)
            (slug / "2026_old.jsonl").write_text(json.dumps({
                "type": "message", "timestamp": 500_000,
                "message": {"role": "assistant", "content": "later", "stopReason": "stop"},
            }) + "\n")
            saved = home / "agents.json"
            saved.write_text(json.dumps({"bootId": "boot-a", "agents": [], "history": [
                history("omp", "old", lastState="idle", closedBy="exit",
                        recap="earlier exchange",
                        recent=[{"role": "assistant", "kind": "text", "text": "later"}]),
            ]}))
            with (mock.patch.object(agents.Path, "home", return_value=home),
                  mock.patch.object(agents, "_read_boot_id", return_value="boot-a"),
                  mock.patch.object(agents, "_build_records", return_value=[])):
                agents._locked_sweep(aggregate_path=saved)
            row = json.loads(saved.read_text())["history"][0]
        self.assertEqual(row["recap"], "")
        self.assertEqual(row["recent"][0]["ts"], 500_000_000)


class TernHostTests(unittest.TestCase):
    # Tern's session daemon outlived the window that started it and was
    # reparented to systemd; a later window (pid 7) shows its panes.
    COMM = {10: "omp", 9: "zsh", 8: "tern", 3: "systemd"}
    PPID = {10: 9, 9: 8, 8: 3, 3: 1}

    def test_daemon_pane_is_a_tern_session_whose_chain_reaches_the_window(self) -> None:
        with (
            mock.patch.object(agents, "_comm_of", side_effect=lambda p: self.COMM.get(p, "")),
            mock.patch.object(agents, "_ppid_of", side_effect=lambda p: self.PPID.get(p, 0)),
            mock.patch.object(agents, "_argv_of", return_value=[]),
            mock.patch.object(agents, "tern_window_pids", return_value=[7]),
        ):
            self.assertEqual(agents._parent_walk_for_host(10), ("tern", [10, 9, 8, 7]))

    def test_visible_pane_is_focused_block_of_shown_or_only_tab(self) -> None:
        def tab(shown, *blocks):
            return {"shown": shown, "blocks": [
                {"id": block, "focused": focused} for block, focused in blocks
            ]}
        listing = {"sessions": [
            # The window shows "work" tab 2; its unfocused split and the
            # hidden tab's focused block are not visible.
            {"name": "work", "tabs": [tab(False, (1, True)), tab(True, (2, False), (3, True))]},
            # A hidden session's only tab is what a window switching to it shows.
            {"name": "solo", "tabs": [tab(False, (4, True))]},
            # A hidden session with several tabs has no known visible tab.
            {"name": "many", "tabs": [tab(False, (5, True)), tab(False, (6, True))]},
        ]}
        out = subprocess.CompletedProcess([], 0, stdout=json.dumps(listing))
        with mock.patch.object(agents.subprocess, "run", return_value=out):
            self.assertEqual(agents._tern_visible_panes(""), {3: "work", 4: "solo"})


class PersistenceTests(unittest.TestCase):
    def test_older_requested_at_is_skipped_only_for_same_boot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            aggregate_path = Path(directory) / "agents.json"
            saved = agents._merge_snapshot([], {}, "boot-current", 200)
            agents._write_aggregate(saved, aggregate_path)
            before = aggregate_path.read_bytes()

            with (
                mock.patch.object(agents, "_read_boot_id", return_value="boot-current"),
                mock.patch.object(
                    agents,
                    "_build_records",
                    side_effect=AssertionError("stale request must skip the scan"),
                ),
            ):
                payload, written = agents._locked_sweep(
                    {}, 100, aggregate_path=aggregate_path
                )
            self.assertFalse(written)
            self.assertEqual(payload["updatedAt"], 200)
            self.assertEqual(aggregate_path.read_bytes(), before)

            older_boot_path = Path(directory) / "older" / "agents.json"
            agents._write_aggregate(
                agents._merge_snapshot(
                    [],
                    {"bootId": "boot-old", "agents": [], "history": []},
                    "boot-old",
                    200,
                ),
                older_boot_path,
            )
            with (
                mock.patch.object(agents, "_read_boot_id", return_value="boot-current"),
                mock.patch.object(agents, "_build_records", return_value=[]),
            ):
                payload, written = agents._locked_sweep(
                    {}, 100, aggregate_path=older_boot_path
                )
            self.assertTrue(written)
            self.assertEqual(payload["bootId"], "boot-current")

    def test_future_saved_timestamp_does_not_freeze_same_boot_sweeps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            agents._write_aggregate(
                agents._merge_snapshot([], {}, "boot-current", 10_000),
                path,
            )
            with (
                mock.patch.object(agents, "_read_boot_id", return_value="boot-current"),
                mock.patch.object(agents, "_build_records", return_value=[]),
                mock.patch.object(agents.time, "time", return_value=1.0),
            ):
                payload, written = agents._locked_sweep(
                    {}, 100, aggregate_path=path
                )

            self.assertTrue(written)
            self.assertEqual(payload["updatedAt"], 1_000)

    def test_boot_change_ignores_desktop_map_until_same_boot_sweep(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            agents._write_aggregate(
                {
                    "bootId": "boot-old",
                    "updatedAt": 100,
                    "counts": {},
                    "agents": [],
                    "history": [],
                },
                path,
            )
            desktop_map = {("omp", "live"): "4"}
            live = active("omp", "live", desktop=None)
            live.pop("desktop")

            with (
                mock.patch.object(agents, "_read_boot_id", return_value="boot-current"),
                mock.patch.object(agents, "_build_records", return_value=[live]),
            ):
                first, written = agents._locked_sweep(
                    desktop_map, aggregate_path=path
                )
                second, _ = agents._locked_sweep(
                    desktop_map, aggregate_path=path
                )

            self.assertTrue(written)
            self.assertNotIn("desktop", first["agents"][0])
            self.assertEqual(second["agents"][0]["desktop"], "4")

    def test_atomic_write_is_mode_0600_valid_and_leaves_no_temporary(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state" / "agents.json"
            payload = agents._merge_snapshot([], {}, "boot-a", 123)
            agents._write_aggregate(payload, path)

            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(json.loads(path.read_text()), payload)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])

    def test_failed_write_before_replace_preserves_previous_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            agents._write_aggregate({"version": "old"}, path)
            before = path.read_bytes()

            with mock.patch.object(
                agents.json, "dump", side_effect=OSError("synthetic write failure")
            ):
                with self.assertRaisesRegex(OSError, "synthetic write failure"):
                    agents._write_aggregate({"version": "new"}, path)

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(list(path.parent.glob(f".{path.name}.*.tmp")), [])



class IncrementalParserTests(unittest.TestCase):
    def setUp(self):
        agents._TRANSCRIPT_CACHE.clear()

    def rows(self, provider):
        if provider == "claude":
            return [
                {"type": "ai-title", "aiTitle": "old title"},
                {"type": "user", "message": {"content": "real prompt"}},
                {"type": "assistant", "message": {"model": "old-model", "content": "answer"}},
            ]
        if provider == "codex":
            return [
                {"type": "session_meta", "payload": {"id": "sid", "cwd": "/old", "model": "old-model"}},
                {"type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "real prompt"}]}},
            ]
        return [
            {"type": "session", "id": "sid", "cwd": "/old", "title": "old title"},
            {"type": "model_change", "modelId": "old-model"},
            {"type": "message", "message": {"role": "user", "content": "real prompt"}},
        ]

    def encode(self, rows):
        return "".join(json.dumps(row) + "\n" for row in rows)

    def assert_cold_equal(self, path, provider):
        warm = agents._transcript_info(str(path), provider)
        cache = dict(agents._TRANSCRIPT_CACHE)
        agents._TRANSCRIPT_CACHE.clear()
        cold = agents._transcript_info(str(path), provider)
        self.assertEqual(warm, cold)
        agents._TRANSCRIPT_CACHE.clear()
        agents._TRANSCRIPT_CACHE.update(cache)
        return warm

    def test_append_rewrite_truncate_partial_and_rotation(self):
        for provider in ("claude", "codex", "pi"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "session.jsonl"
                original = self.encode(self.rows(provider))
                path.write_text(original)
                first = self.assert_cold_equal(path, provider)
                self.assertEqual(first["model"], "old-model")
                with path.open("a") as output:
                    output.write(self.encode([[], 42, {"type": "response_item", "payload": []}]))
                self.assert_cold_equal(path, provider)
                partial = json.dumps(self.rows(provider)[-1])
                with path.open("a") as output:
                    output.write(partial[:10])
                before = self.assert_cold_equal(path, provider)
                with path.open("a") as output:
                    output.write(partial[10:])
                self.assertEqual(self.assert_cold_equal(path, provider), before)
                with path.open("a") as output:
                    output.write("\n")
                self.assert_cold_equal(path, provider)
                path.write_text(original.replace("old-model", "new-model"))
                self.assertEqual(self.assert_cold_equal(path, provider)["model"], "new-model")
                path.write_text("{}\n")
                self.assertEqual(self.assert_cold_equal(path, provider)["model"], "")
                replacement = path.with_suffix(".replacement")
                replacement.write_text(original)
                replacement.replace(path)
                self.assertEqual(self.assert_cold_equal(path, provider)["model"], "old-model")

    def test_tools_are_bounded_online_and_noise_preserves_last_real_prompt(self):
        state = agents._parser_state()
        for row in self.rows("claude"):
            agents._parse_record("claude", state, row)
        for index in range(5000):
            agents._parse_record("claude", state, {
                "type": "assistant", "message": {"content": [{"type": "tool_use", "name": "bash"}]},
                "timestamp": index + 1,
            })
        agents._parse_record("claude", state, {"type": "user", "message": {"content": "<system-reminder> noise"}})
        self.assertEqual(state["last_real"], "real prompt")
        self.assertEqual(len(state["peek"]), 3)
        self.assertEqual(len(state["peek"][-1]["names"]), 10)
        self.assertTrue(agents._peek_finalize(state["peek"])[-1]["text"].endswith("+4990 more"))
        self.assertLess(len(json.dumps(state)), 3000)
        for index in range(20):
            agents._peek_add(state["peek"], "user", str(index), 1)
        self.assertEqual(len(state["peek"]), 8)

    def test_collapsed_tool_preview_respects_display_limit(self):
        peek = []
        for _ in range(11):
            agents._peek_add(peek, "assistant", "x" * 320, 1, "tools")
        recent = agents._peek_finalize(peek)
        self.assertEqual(len(recent), 1)
        self.assertEqual(len(recent[0]["text"]), 320)

    def test_malformed_fields_do_not_hide_healthy_rows(self):
        for provider in ("claude", "codex", "pi"):
            with self.subTest(provider=provider), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "session.jsonl"
                bad = [[], None, 5, "scalar", {"type": "ai-title", "aiTitle": []},
                       {"type": "session_meta", "payload": 4},
                       {"type": "session", "id": [], "cwd": {}, "title": 7},
                       {"type": "assistant", "message": {"content": [{"type": "text", "text": {}}, {"name": []}]}}]
                path.write_text(self.encode(bad + self.rows(provider)))
                info = agents._transcript_info(str(path), provider)
                self.assertEqual(info["lastPrompt"], "real prompt")
                self.assertEqual(info["model"], "old-model")

    def test_pi_header_title_model_and_state_survive_long_append(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pi.jsonl"
            path.write_text(self.encode(self.rows("pi")))
            agents._transcript_info(str(path), "pi")
            rows = [{"type": "message", "message": {"role": "assistant", "content": "answer", "stopReason": "stop"}}] * 100
            rows += [{"type": "title_change", "id": "not-the-session", "title": "renamed"},
                     {"type": "message", "message": {"role": "user", "content": "<system-reminder> noise"}}]
            with path.open("a") as output:
                output.write(self.encode(rows))
            info = self.assert_cold_equal(path, "pi")
            self.assertEqual(info["sessionId"], "sid")
            self.assertEqual(info["cwd"], "/old")
            self.assertEqual(info["windowTitle"], "renamed")
            self.assertEqual(info["model"], "old-model")
            self.assertEqual(info["lastPrompt"], "real prompt")
            self.assertEqual(info["state"], "working")
            self.assertEqual(len(info["recent"]), 8)

    def test_malformed_claude_state_files_leave_healthy_sibling(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            sessions = home / ".claude" / "sessions"
            sessions.mkdir(parents=True)
            for pid, record in enumerate([[], 3, {"sessionId": []}, {"sessionId": "healthy", "cwd": "/work", "status": "idle"}], 1):
                (sessions / f"{pid}.json").write_text(json.dumps(record))
            with mock.patch.object(agents.Path, "home", return_value=home), mock.patch.object(agents, "_INFO_FN", {"claude": agents._claude_info}), mock.patch.object(agents, "_pgrep", return_value=[1, 2, 3, 4]), mock.patch.object(agents, "_parent_walk_for_host", return_value=("kitty", [])), mock.patch.object(agents, "_cwd_of", return_value="/work"):
                rows = agents._build_records()
            self.assertEqual(len(rows), 4)
            self.assertEqual(rows[-1]["sessionId"], "healthy")
            self.assertEqual([row["state"] for row in rows], ["untracked"] * 3 + ["idle"])

    def test_unknown_identity_and_failed_session_have_untracked_counts(self):
        def info(pid):
            if pid == 1:
                raise ValueError("bad session")
            return {"sessionId": [] if pid == 2 else "healthy", "state": "idle", "identityExact": True}
        with mock.patch.object(agents, "_INFO_FN", {"claude": info}), mock.patch.object(agents, "_pgrep", return_value=[1, 2, 3]), mock.patch.object(agents, "_parent_walk_for_host", return_value=("kitty", [])), mock.patch.object(agents, "_cwd_of", return_value="/work"):
            rows = agents._build_records()
        snapshot = agents._merge_snapshot(rows, {}, "boot", 1)
        self.assertEqual(snapshot["counts"], {"working": 0, "blocked": 0, "idle": 1, "untracked": 2, "total": 3})
        for row in rows[:2]:
            self.assertTrue(row["sessionId"].startswith("untracked-"))
            self.assertFalse(row["identityExact"])
            self.assertEqual(row["resumeCommand"], "")

    def test_fresh_process_once_reuses_checkpoint_and_prunes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "transcript.jsonl"
            aggregate = Path(directory) / "agents.json"
            path.write_text(self.encode(self.rows("claude")))
            script = r"""
import importlib.util, json, sys
from pathlib import Path
spec = importlib.util.spec_from_file_location("agents", sys.argv[1])
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)
count = [0]
parse = a._parse_record
def counted(*args):
    count[0] += 1
    return parse(*args)
a._parse_record = counted
a._build_records = lambda: (a._transcript_info(sys.argv[2], "claude") and []) if sys.argv[4] != "prune" else []
sweep = a._locked_sweep
a._locked_sweep = lambda *args, **kwargs: sweep(*args, **kwargs, aggregate_path=Path(sys.argv[3]))
a.main(["--once"])
print(count[0])
"""
            def run(mode="scan"):
                return int(subprocess.check_output([sys.executable, "-c", script, str(SCRIPT_PATH), str(path), str(aggregate), mode], text=True))
            self.assertEqual(run(), 3)
            self.assertEqual(run(), 0)
            with path.open("a") as output:
                output.write(self.encode([{"type": "ai-title", "aiTitle": "new title"}]))
            self.assertEqual(run(), 1)
            cache_path = aggregate.with_suffix(".parsers.json")
            self.assertEqual(stat.S_IMODE(cache_path.stat().st_mode), 0o600)
            self.assertEqual(run("prune"), 0)
            self.assertEqual(json.loads(cache_path.read_text())["entries"], {})

    def test_stdout_does_not_write_and_scan_timestamp_is_completion_time(self):
        with mock.patch.object(agents, "_build_records", return_value=[]), mock.patch.object(agents, "_write_aggregate") as write, mock.patch.object(agents.sys, "stdout"):
            agents.main([])
            write.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agents.json"
            with mock.patch.object(agents, "_build_records", return_value=[]), mock.patch.object(agents, "_read_boot_id", return_value="boot"), mock.patch.object(agents.time, "time", side_effect=[1.0, 4.0]):
                payload, _ = agents._locked_sweep(aggregate_path=path)
            self.assertEqual(payload["updatedAt"], 4000)

if __name__ == "__main__":
    unittest.main()
