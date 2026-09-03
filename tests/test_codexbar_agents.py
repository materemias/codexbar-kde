from __future__ import annotations

import importlib.util
import json
import shlex
import sqlite3
import stat
import tempfile
import unittest
from pathlib import Path
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


RECOVERY_FIELDS = {
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
        "hostPid": 100,
        "ancestorPids": [123, 100],
        "tty": "pts/1",
        "recent": [{"role": "user", "text": "private live data"}],
    }
    record.update(changes)
    return record


def recovery(provider: str, session_id: str, **changes: object) -> dict:
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

            *_, model = agents._tail_claude_transcript(str(path))

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
        previous = {"bootId": "boot-a", "agents": [old], "recovery": []}

        snapshot = agents._merge_snapshot([], previous, "boot-b", 200)

        self.assertEqual(
            set(snapshot), {"bootId", "updatedAt", "counts", "agents", "recovery"}
        )
        self.assertEqual(snapshot["bootId"], "boot-b")
        self.assertEqual(snapshot["agents"], [])
        self.assertEqual(len(snapshot["recovery"]), 1)
        restored = snapshot["recovery"][0]
        self.assertEqual(set(restored), RECOVERY_FIELDS)
        self.assertEqual(restored["cwd"], "/work/O'Brien project")
        self.assertEqual(restored["desktop"], "4")
        self.assertEqual(restored["lastSeenAt"], 100)
        self.assertEqual(
            restored["resumeCommand"],
            f"{shlex.join(['cd', '--', old['cwd']])} && "
            f"{shlex.join(['omp', '--resume', old['sessionId']])}",
        )
        self.assertTrue(
            {"pid", "hostPid", "ancestorPids", "tty", "recent", "identityExact"}
            .isdisjoint(restored)
        )

        live = active("omp", old["sessionId"], updatedAt=250)
        resumed = agents._merge_snapshot([live], snapshot, "boot-b", 250)
        self.assertEqual(resumed["recovery"], [])
        self.assertEqual(len(resumed["agents"]), 1)

    def test_unresolved_recovery_survives_reboots_and_unions_new_active(self) -> None:
        first = agents._merge_snapshot(
            [],
            {"bootId": "boot-a", "agents": [active("omp", "old")], "recovery": []},
            "boot-b",
            200,
        )
        second = agents._merge_snapshot([], first, "boot-c", 300)
        self.assertEqual(
            [(row["provider"], row["sessionId"]) for row in second["recovery"]],
            [("omp", "old")],
        )

        with_new_live = agents._merge_snapshot(
            [active("claude", "new", desktop="2")], second, "boot-c", 350
        )
        later = agents._merge_snapshot([], with_new_live, "boot-d", 400)
        self.assertEqual(
            {(row["provider"], row["sessionId"]) for row in later["recovery"]},
            {("omp", "old"), ("claude", "new")},
        )

    def test_just_ended_active_record_wins_recovery_duplicate(self) -> None:
        previous = {
            "bootId": "boot-a",
            "agents": [
                active(
                    "omp", "same", cwd="/new cwd", desktop="5", updatedAt=200
                )
            ],
            "recovery": [
                recovery(
                    "omp", "same", cwd="/old cwd", desktop="2", lastSeenAt=100
                )
            ],
        }
        snapshot = agents._merge_snapshot([], previous, "boot-b", 300)
        self.assertEqual(len(snapshot["recovery"]), 1)
        self.assertEqual(snapshot["recovery"][0]["cwd"], "/new cwd")
        self.assertEqual(snapshot["recovery"][0]["desktop"], "5")
        self.assertEqual(snapshot["recovery"][0]["lastSeenAt"], 200)

    def test_same_boot_disappearance_does_not_create_recovery(self) -> None:
        previous = {
            "bootId": "boot-a",
            "agents": [active("omp", "same-boot")],
            "recovery": [],
        }
        snapshot = agents._merge_snapshot([], previous, "boot-a", 200)
        self.assertEqual(snapshot["recovery"], [])

    def test_cross_provider_session_ids_remain_distinct(self) -> None:
        previous = {
            "bootId": "boot-a",
            "agents": [active("claude", "shared"), active("omp", "shared")],
            "recovery": [],
        }
        snapshot = agents._merge_snapshot([], previous, "boot-b", 200)
        self.assertEqual(
            {(row["provider"], row["sessionId"]) for row in snapshot["recovery"]},
            {("claude", "shared"), ("omp", "shared")},
        )

    def test_legacy_payload_records_boot_without_inventing_recovery(self) -> None:
        previous = {"agents": [active("omp", "legacy")], "recovery": []}
        snapshot = agents._merge_snapshot([], previous, "boot-a", 200)
        self.assertEqual(snapshot["bootId"], "boot-a")
        self.assertEqual(snapshot["recovery"], [])

    def test_unreadable_boot_id_keeps_saved_boot_and_only_existing_recovery(self) -> None:
        existing = recovery("omp", "unresolved")
        previous = {
            "bootId": "known-boot",
            "agents": [active("claude", "active-before-unknown")],
            "recovery": [existing],
        }
        snapshot = agents._merge_snapshot([], previous, "", 200)
        self.assertEqual(snapshot["bootId"], "known-boot")
        self.assertEqual(
            [(row["provider"], row["sessionId"]) for row in snapshot["recovery"]],
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
            "recovery": [None, {"provider": "omp", "sessionId": []}],
        }
        snapshot = agents._merge_snapshot([], previous, "boot-b", 200)
        self.assertEqual(
            [(row["provider"], row["sessionId"]) for row in snapshot["recovery"]],
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
        previous = {"bootId": "boot-a", "agents": [old], "recovery": []}

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
                agents._load_payload(path), {"agents": [], "recovery": []}
            )

            path.write_text(json.dumps({
                "bootId": "boot-a",
                "agents": [None, {"provider": "omp", "sessionId": "ok"}, "bad"],
                "recovery": "not-a-list",
            }))
            payload = agents._load_payload(path)
            self.assertEqual(payload["agents"], [{"provider": "omp", "sessionId": "ok"}])
            self.assertEqual(payload["recovery"], [])

            path.write_text("{not json")
            self.assertEqual(
                agents._load_payload(path), {"agents": [], "recovery": []}
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
            {"bootId": "boot-a", "agents": [], "recovery": []},
            "boot-a",
            100,
        )
        self.assertEqual(
            [row["resumeCommand"] for row in snapshot["agents"]], ["", ""]
        )

    def test_saved_recovery_command_must_match_canonical_command(self) -> None:
        valid = recovery(
            "omp",
            "safe-session",
            cwd="/work/project",
            resumeCommand="cd -- /work/project && omp --resume safe-session",
        )
        tampered = recovery(
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
                "recovery": [valid, tampered],
            },
            "boot-a",
            100,
        )
        commands = {
            row["sessionId"]: row["resumeCommand"]
            for row in snapshot["recovery"]
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

    def test_live_dedup_identity_remains_session_id_only(self) -> None:
        info = lambda _pid: {
            "sessionId": "shared",
            "cwd": "/work",
            "state": "idle",
            "identityExact": True,
        }
        with (
            mock.patch.object(agents, "_INFO_FN", {"claude": info, "omp": info}),
            mock.patch.object(agents, "_pids_for", return_value=[42]),
            mock.patch.object(
                agents, "_parent_walk_for_host", return_value=("kitty", 10, [42, 10])
            ),
        ):
            records = agents._build_records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["provider"], "claude")
        self.assertEqual(records[0]["sessionId"], "shared")
        self.assertTrue(records[0]["identityExact"])
        self.assertEqual(records[0]["resumeCommand"], "cd -- /work && claude --resume shared")


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
                    {"bootId": "boot-old", "agents": [], "recovery": []},
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
                    "recovery": [],
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


if __name__ == "__main__":
    unittest.main()
