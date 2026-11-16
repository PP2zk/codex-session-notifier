import importlib.util
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import os
import subprocess
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("reader_scan", Path(__file__).resolve().parents[1] / "resources/probe.py")
reader = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reader)
NOW = 1792392000


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "sessions").mkdir()
        self.path = self.home / "sessions" / "sample.jsonl"
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE threads(id TEXT, source TEXT, rollout_path TEXT, updated_at INTEGER, name TEXT, title TEXT, cli_version TEXT)")
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)", ("s1", "vscode", str(self.path), NOW, None, "Test task", "0.160.0"))
            db.commit()
        self.append({"type": "session_meta", "payload": {"id": "s1", "source": "vscode", "originator": "codex_vscode", "cli_version": "0.160.0", "history_mode": "legacy"}})
        for target, value in [("time.time", lambda: NOW), ("source_identity", lambda p: hashlib.sha256(str(p).encode()).hexdigest()), ("execution_binding", lambda p, diagnostic=None: True)]:
            mocker = patch.object(reader.time, "time", value) if target == "time.time" else patch.object(reader, target, value)
            mocker.start()
            self.addCleanup(mocker.stop)
        self.state = {"home": str(self.home), "mode": "scan", "initialized": False, "startedAt": NOW - 10, "cursors": {}, "position": 0}

    def append(self, row):
        with self.path.open("ab") as stream:
            stream.write((json.dumps(row) + "\n").encode())

    def event(self, kind, turn="t1", **extra):
        self.append({"type": "event_msg", "payload": {"type": kind, "turn_id": turn, **extra}})

    def client_source(self, source):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET source=?", (source,))
            db.commit()
        self.path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "s1", "source": source}}) + "\n", encoding="utf-8")

    def test_desktop_appserver_running_completion_and_next_turn_use_existing_lifecycle(self):
        self.client_source("appServer")
        self.event("task_started", started_at=NOW)
        self.assertEqual([s["turnId"] for s in self.scan()["activeSessions"]], ["t1"])
        self.event("task_complete", completed_at=NOW)
        self.assertEqual([e["turnId"] for e in self.scan()["events"]], ["t1"])
        self.assertEqual(self.scan()["events"], [])
        self.event("task_started", "t2", started_at=NOW)
        self.assertEqual([s["turnId"] for s in self.scan()["activeSessions"]], ["t2"])
        self.event("task_complete", "t2", completed_at=NOW)
        self.assertEqual([e["turnId"] for e in self.scan()["events"]], ["t2"])

    def test_terminal_and_subagent_sources_never_become_candidates_even_with_tracked_cursor(self):
        self.scan()
        for source in ("cli", "exec", "unknown", '{"subagent":{"other":"guardian"}}'):
            with self.subTest(source=source):
                self.client_source(source)
                self.event("task_started", started_at=NOW)
                self.event("task_complete", completed_at=NOW)
                result = reader.scan(self.state)
                self.assertEqual(result["activeSessions"], [])
                self.assertEqual(result["events"], [])
                self.assertEqual(result["cursors"], {})

    def test_desktop_completion_still_requires_live_file_binding(self):
        self.client_source("appServer")
        self.scan()
        self.event("task_complete", completed_at=NOW)
        with patch.object(reader, "execution_binding", return_value=False):
            result = self.scan()
        self.assertEqual(result["events"], [])
        self.assertIn("execution_source_unverified", result["warnings"])
        self.assertEqual([e["turnId"] for e in self.scan()["events"]], ["t1"])

    def test_supported_client_source_mismatch_between_database_and_header_is_rejected(self):
        self.event("task_started", started_at=NOW)
        self.scan()
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET source='appServer'")
            db.commit()
        self.event("task_complete", completed_at=NOW)
        result = self.scan()
        self.assertEqual(result["events"], [])
        self.assertEqual(result["activeSessions"], [])
        self.assertIn("session_metadata_mismatch", result["warnings"])

    def scan(self):
        result = reader.scan(self.state)
        self.state.update({k: result[k] for k in ("initialized", "cursors", "position", "activity", "notificationBacklog")})
        return result

    def archive(self):
        directory = self.home / "archived_sessions"
        directory.mkdir(exist_ok=True)
        destination = directory / self.path.name
        self.path.replace(destination)
        self.path = destination
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("ALTER TABLE threads ADD COLUMN archived INTEGER DEFAULT 0")
            db.execute("UPDATE threads SET archived=1,rollout_path=? WHERE id='s1'", (str(self.path),))
            db.commit()

    def test_new_archived_history_does_not_become_running_or_unknown(self):
        self.event("task_started", started_at=NOW)
        self.event("task_complete", completed_at=NOW)
        self.archive()
        result = self.scan()
        self.assertEqual(result["events"], [])
        self.assertEqual(result["activeSessions"], [])
        self.assertEqual(result["activityUnknown"], 0)
        self.assertEqual(result["cursors"], {})
        self.assertEqual(result["warnings"], [])

    def test_tracked_archive_reads_final_completion_then_retires(self):
        self.event("task_started", started_at=NOW)
        self.assertEqual(len(self.scan()["activeSessions"]), 1)
        self.event("task_complete", completed_at=NOW)
        self.archive()
        result = self.scan()
        self.assertEqual([event["turnId"] for event in result["events"]], ["t1"])
        self.assertEqual(result["activeSessions"], [])
        self.assertEqual(result["activityUnknown"], 0)
        self.assertEqual(result["cursors"], {})
        self.assertEqual(result["warnings"], [])
        self.assertEqual(self.scan()["events"], [])

    def test_archiving_an_already_queued_turn_does_not_reverify_or_warn(self):
        self.scan()
        self.event("task_complete", completed_at=NOW)
        queued = self.scan()["events"][0]
        self.state["knownCompletions"] = [{k: queued[k] for k in ("sessionId", "turnId")}]
        self.archive()
        with patch.object(reader, "execution_binding", return_value=False) as binding:
            result = self.scan()
        self.assertEqual(result["events"], [])
        self.assertEqual(result["warnings"], [])
        self.assertEqual(result["verificationFailures"], [])
        self.assertEqual(result["cursors"], {})
        binding.assert_not_called()

    def test_known_completion_does_not_authorize_a_new_archived_turn(self):
        self.scan()
        self.event("task_complete", "known", completed_at=NOW)
        self.scan()
        self.state["knownCompletions"] = [{"sessionId": "s1", "turnId": "known"}]
        self.event("task_complete", "new", completed_at=NOW)
        self.archive()
        with patch.object(reader, "execution_binding", return_value=False):
            blocked = self.scan()
        self.assertEqual(blocked["events"], [])
        self.assertIn("execution_source_unverified", blocked["warnings"])
        self.assertEqual(blocked["verificationFailures"][0]["turnIds"], ["new"])
        self.assertEqual([e["turnId"] for e in self.scan()["events"]], ["new"])

    def test_known_turn_in_another_session_does_not_suppress_a_completion(self):
        self.scan()
        self.state["knownCompletions"] = [{"sessionId": "other-session", "turnId": "t1"}]
        self.event("task_complete", completed_at=NOW)
        with patch.object(reader, "execution_binding", return_value=False):
            result = self.scan()
        self.assertIn("execution_source_unverified", result["warnings"])

    def test_known_completion_input_is_bounded_and_requires_both_ids(self):
        for value in (None, {}, [{"sessionId": "s1"}], [{"sessionId": "s1", "turnId": "t1"}] * 201):
            with self.subTest(value_type=type(value).__name__):
                with self.assertRaisesRegex(ValueError, "invalid_known_completions"):
                    reader.scan({**self.state, "knownCompletions": value})

    def test_archive_flag_does_not_allow_reading_outside_the_native_store(self):
        self.scan()
        self.event("task_complete", completed_at=NOW)
        self.archive()
        external = self.home / "private.jsonl"
        self.path.replace(external)
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET rollout_path=?", (str(external),))
            db.commit()
        result = self.scan()
        self.assertEqual(result["events"], [])
        self.assertEqual(result["activeSessions"], [])
        self.assertEqual(result["bytesRead"], 0)
        self.assertIn("session_file_unavailable", result["warnings"])

    def test_linked_home_resolves_to_the_same_source_and_reads_the_current_turn(self):
        linked = tempfile.TemporaryDirectory()
        self.addCleanup(linked.cleanup)
        alias = Path(linked.name) / ".codex"
        try:
            alias.symlink_to(self.home, target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
            # Directory junctions do not need the Windows symlink privilege.
            subprocess.run(["cmd", "/c", "mklink", "/J", str(alias), str(self.home)],
                           check=True, capture_output=True)
        self.addCleanup(alias.unlink if alias.is_symlink() else alias.rmdir)
        self.event("task_started", started_at=NOW)
        direct = self.scan()
        linked_result = reader.scan({**self.state, "home": str(alias)})
        self.assertEqual(linked_result["sourceId"], direct["sourceId"])
        self.assertEqual(len(linked_result["activeSessions"]), 1)
        self.event("task_complete", completed_at=NOW)
        result = reader.scan({**self.state, "home": str(alias)})
        self.assertEqual([event["turnId"] for event in result["events"]], ["t1"])
        self.assertEqual(result["warnings"], [])

    def test_baseline_then_two_new_turns_and_rename(self):
        self.event("task_complete", "old", completed_at=NOW - 20)
        self.assertEqual(self.scan()["events"], [])
        self.event("task_complete", completed_at=NOW)
        event = self.scan()["events"][0]
        self.assertEqual((event["turnId"], event["sessionName"], event["nameSource"]), ("t1", "Test task", "title"))
        self.assertEqual(self.scan()["events"], [])
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET name='Renamed'")
            db.commit()
        self.event("task_complete", "t2", completed_at=NOW)
        self.assertEqual(self.scan()["events"][0]["sessionName"], "Renamed")

    def test_partial_final_record_waits_for_newline(self):
        self.scan()
        with self.path.open("ab") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "partial", "completed_at": NOW}}).encode())
        self.assertEqual(self.scan()["events"], [])
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        self.assertEqual(self.scan()["events"][0]["turnId"], "partial")

    def test_startup_cutoff_skips_old_completion_but_allows_later_completion_of_old_start(self):
        self.scan()
        self.event("task_complete", "before-startup", completed_at=NOW - 120)
        self.event("task_started", "running-at-startup", started_at=NOW - 90)
        self.event("task_complete", "running-at-startup", completed_at=NOW - 10)
        self.state["startedAt"] = NOW - 60
        result = self.scan()
        self.assertEqual([event["turnId"] for event in result["events"]], ["running-at-startup"])
        decisions = {item["turnId"]: item["reason"] for item in result["diagnostics"] if item["kind"] == "completion"}
        self.assertEqual(decisions["before-startup"], "before_monitor_start")

    def test_new_startup_cutoff_preserves_in_progress_activity(self):
        self.event("task_started", started_at=NOW - 100)
        self.assertEqual(len(self.scan()["activeSessions"]), 1)
        self.state["startedAt"] = NOW - 50
        self.assertEqual(len(self.scan()["activeSessions"]), 1)
        self.event("task_complete", completed_at=NOW)
        self.assertEqual([event["turnId"] for event in self.scan()["events"]], ["t1"])

    def test_observed_alpha_terminal_shapes_preserve_strict_completion_rules(self):
        for version in ("0.164.0-alpha.11.1", "0.162.0-alpha.17.2"):
            with self.subTest(version=version):
                with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
                    db.execute("UPDATE threads SET cli_version=?", (version,))
                    db.commit()
                self.state.update(initialized=False, cursors={}, activity={}, position=0)
                self.path.write_text("", encoding="utf-8")
                self.append({"type": "session_meta", "payload": {"id": "s1", "source": "vscode", "originator": "codex_vscode", "cli_version": version}})
                self.event("task_started", root_turn_id="t1", started_at=NOW - 5)
                baseline = self.scan()
                self.assertEqual(baseline["events"], [])
                self.assertEqual(baseline["activeSessions"][0]["turnId"], "t1")
                self.event("task_complete", root_turn_id="t1", started_at=NOW - 5, completed_at=NOW,
                           duration_ms=5000, time_to_first_token_ms=100, last_agent_message="PRIVATE REPLY")
                self.event("task_complete", "failed", completed_at=NOW, error={"message": "PRIVATE ERROR"})
                self.event("turn_aborted", "aborted")
                self.event("task_complete", "aborted", completed_at=NOW)
                result = self.scan()
                self.assertEqual([event["turnId"] for event in result["events"]], ["t1"])
                self.assertEqual(result["activeSessions"], [])
                self.assertNotIn("unsupported_codex_version", result["warnings"])
                self.assertNotIn("PRIVATE", json.dumps(result))

    def test_running_hover_backfills_start_without_replaying_completion(self):
        self.event("task_started", started_at=NOW - 20)
        self.event("agent_message", message="PRIVATE BODY" * reader.MAX_TAIL)
        found = None
        for _ in range(16):
            result = self.scan()
            self.assertEqual(result["events"], [])
            self.assertLessEqual(result["bytesRead"], 8388608)
            self.assertNotIn("PRIVATE", json.dumps(result))
            if result["activeSessions"]:
                found = result["activeSessions"][0]
                break
        self.assertIsNotNone(found)
        self.assertEqual((found["sessionName"], found["turnId"]), ("Test task", "t1"))
        self.assertIsNotNone(found["startedAt"])

    def test_running_hover_removes_success_error_and_aborted_turns(self):
        self.event("task_started", started_at=NOW)
        self.assertEqual(len(self.scan()["activeSessions"]), 1)
        self.event("task_complete", completed_at=NOW)
        self.assertEqual(self.scan()["activeSessions"], [])
        self.event("task_started", "failed", started_at=NOW)
        self.assertEqual(len(self.scan()["activeSessions"]), 1)
        self.event("task_complete", "failed", completed_at=NOW, error={"message": "PRIVATE"})
        self.assertEqual(self.scan()["activeSessions"], [])
        self.event("task_started", "aborted", started_at=NOW)
        self.assertEqual(len(self.scan()["activeSessions"]), 1)
        self.event("turn_aborted", "aborted")
        self.assertEqual(self.scan()["activeSessions"], [])

    def test_running_hover_requires_execution_binding_and_uses_current_name(self):
        self.event("task_started", started_at=NOW)
        with patch.object(reader, "execution_binding", lambda _, diagnostic=None: False):
            self.assertEqual(self.scan()["activeSessions"], [])
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET name='Renamed active session'")
            db.commit()
        with patch.object(reader.time, "time", lambda: NOW + 11):
            self.assertEqual(self.scan()["activeSessions"][0]["sessionName"], "Renamed active session")

    def test_current_activity_does_not_wait_for_forward_completion_backlog(self):
        self.scan()
        for _ in range(500):
            self.event("agent_message", message="PRIVATE BODY")
        self.event("task_started", started_at=NOW)
        for _ in range(200):
            self.event("agent_message", message="PRIVATE BODY")
        result = self.scan()
        self.assertLess(result["cursors"]["s1"]["offset"], self.path.stat().st_size)
        self.assertEqual(result["activeSessions"][0]["turnId"], "t1")
        self.assertEqual(result["events"], [])
        # Simulate a window using the older notification parser.
        self.state["cursors"]["s1"]["parser"] = {}
        self.assertEqual(self.scan()["activeSessions"][0]["turnId"], "t1")
        self.event("task_complete", completed_at=NOW)
        result = self.scan()
        self.assertEqual(result["activeSessions"], [])
        self.assertEqual(result["events"], [])

    def test_initialization_only_session_is_idle_without_backend_or_completion(self):
        self.event("thread_settings_applied")
        with patch.object(reader, "execution_binding", return_value=False) as binding:
            for _ in range(3):
                result = self.scan()
                self.assertEqual(result["activeSessions"], [])
                self.assertEqual(result["activityUnknown"], 0)
                self.assertEqual(result["events"], [])
                self.assertFalse(result["activityBacklog"])
            binding.assert_not_called()
        self.assertTrue(result["activity"]["s1"]["activityInitializationOnly"])

    def test_legacy_exhausted_unknown_is_rechecked_with_full_read_budget(self):
        self.event("thread_settings_applied")
        self.scan()
        cursor = self.state["activity"]["s1"]
        cursor.update(activityKnown=False, activityLookback=0)
        cursor.pop("activityInitChecked", None)
        cursor.pop("activityInitializationOnly", None)
        rows = [{"id": "s1", "source": "vscode", "rollout_path": str(self.path), "updated_at": NOW}]
        limited = {"bytesRead": 0, "warnings": []}
        reader.collect_activity(rows, self.home, limited, self.state["activity"], 10, 100)
        self.assertEqual(limited["activityUnknown"], 1)
        self.assertFalse(limited["activity"]["s1"].get("activityInitChecked", False))
        self.state["activity"] = limited["activity"]
        result = self.scan()
        self.assertEqual(result["activityUnknown"], 0)
        self.assertTrue(result["activity"]["s1"]["activityKnown"])

    def test_initialization_idle_rechecks_appends_and_tracks_real_turns(self):
        self.scan()
        self.event("thread_settings_applied")
        self.assertEqual(self.scan()["activityUnknown"], 0)
        self.event("task_started", started_at=NOW)
        result = self.scan()
        self.assertEqual([s["turnId"] for s in result["activeSessions"]], ["t1"])
        self.assertFalse(result["activity"]["s1"]["activityInitializationOnly"])
        self.event("task_complete", completed_at=NOW)
        self.assertEqual([e["turnId"] for e in self.scan()["events"]], ["t1"])
        self.assertEqual(self.scan()["events"], [])

    def test_initialization_classification_retries_after_record_budget_exhaustion(self):
        self.event("thread_settings_applied")
        rows = [{"id": "s1", "source": "vscode", "rollout_path": str(self.path), "updated_at": NOW}]
        limited = {"bytesRead": 0, "warnings": []}
        reader.collect_activity(rows, self.home, limited, {}, reader.MAX_TAIL, 1)
        self.assertEqual(limited["activityUnknown"], 1)
        self.assertFalse(limited["activity"]["s1"].get("activityInitChecked", False))
        self.state["activity"] = limited["activity"]
        self.assertEqual(self.scan()["activityUnknown"], 0)

    def test_initialization_idle_does_not_survive_unknown_malformed_or_partial_append(self):
        header = self.path.read_bytes()
        for appended in (b'{"type":"future_execution"}\n', b'not json\n',
                         b'{"type":"session_meta","payload":null}\n',
                         b'{"type":"session_meta","payload":{"id":"other","source":"vscode"}}\n',
                         b'{"type":"event_msg","payload":{"type":"task_started"'):
            with self.subTest(appended=appended):
                self.path.write_bytes(header)
                self.state.update(initialized=False, cursors={}, activity={}, position=0)
                self.assertEqual(self.scan()["activityUnknown"], 0)
                with self.path.open("ab") as stream:
                    stream.write(appended)
                result = self.scan()
                self.assertEqual(result["activityUnknown"], 1)
                self.assertEqual(result["activeSessions"], [])
                self.assertEqual(result["events"], [])

    def test_initialization_prefix_does_not_hide_later_unknown_records(self):
        self.event("thread_settings_applied")
        self.append({"type": "future_execution", "data": "x" * (reader.MAX_TAIL * 2)})
        result = self.scan()
        self.assertEqual(result["activityUnknown"], 1)
        self.assertEqual(result["activeSessions"], [])
        self.assertFalse(result["activity"]["s1"]["activityKnown"])
        self.assertFalse(result["activityBacklog"])
        self.assertLessEqual(result["bytesRead"], reader.MAX_CATCHUP_BYTES)

    def test_three_growing_running_sessions_fit_steady_activity_budget(self):
        paths = []
        for index in range(3):
            sid = "s" + str(index + 1)
            path = self.home / "sessions" / (sid + ".jsonl")
            path.write_text("".join(json.dumps(item) + "\n" for item in [
                {"type": "session_meta", "payload": {"id": sid, "source": "vscode", "instructions": "x" * 23000}},
                {"type": "event_msg", "payload": {"type": "task_started", "turn_id": sid, "started_at": NOW}},
                {"type": "event_msg", "payload": {"type": "agent_message", "message": "x" * (reader.MAX_TAIL * 3)}}]), encoding="utf-8")
            with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
                db.execute("DELETE FROM threads WHERE id=?", (sid,))
                db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)", (sid, "vscode", str(path), NOW, None, sid, "0.160.0"))
                db.commit()
            paths.append(path)
        self.assertEqual(len(self.scan()["activeSessions"]), 3)
        for _ in range(3):
            for path in paths:
                with path.open("ab") as stream:
                    stream.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "small append"}}).encode() + b"\n")
            for cursor in self.state["activity"].values():
                # The production state projection revalidates headers each poll.
                cursor.pop("activityHeaderSource", None)
            result = self.scan()
            self.assertEqual(len(result["activeSessions"]), 3)
            self.assertEqual(result["activityUnknown"], 0)
            self.assertLessEqual(result["bytesRead"], reader.MAX_SCAN_BYTES)

    def test_incremental_activity_keeps_partial_record_for_its_final_newline(self):
        self.event("task_started", started_at=NOW)
        self.scan()
        offset = self.state["activity"]["s1"]["activityOffset"]
        with self.path.open("ab") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "t1", "completed_at": NOW}}).encode())
        partial = self.scan()
        self.assertEqual(partial["activity"]["s1"]["activityOffset"], offset)
        self.assertEqual(len(partial["activeSessions"]), 1)
        with self.path.open("ab") as stream:
            stream.write(b"\n")
        complete = self.scan()
        self.assertEqual(complete["activeSessions"], [])
        self.assertEqual([event["turnId"] for event in complete["events"]], ["t1"])

    def test_activity_anchor_detects_equal_size_rewrite_without_reusing_running(self):
        self.event("task_started", started_at=NOW)
        self.scan()
        original = self.path.read_bytes()
        replacement = original.replace(b'task_started', b'task_complete').replace(b'started_at', b'completed_at')
        # Keep the original inode and exact byte count; whitespace is JSON-safe.
        if len(replacement) > len(original):
            original = original.rstrip(b"\n") + b" " * (len(replacement) - len(original)) + b"\n"
            self.path.write_bytes(original)
            self.scan()
        replacement = replacement.rstrip(b"\n").ljust(len(original) - 1, b" ") + b"\n"
        self.assertEqual(len(replacement), len(original))
        self.path.write_bytes(replacement)
        result = self.scan()
        self.assertEqual(result["activeSessions"], [])
        self.assertEqual(result["diagnostics"][0]["state"], "idle")
        cursor = result["activity"]["s1"]
        self.assertEqual(cursor["activityAnchor"], hashlib.sha256(reader.checkpoint_bytes(self.path, cursor["activityOffset"])).hexdigest())
        self.assertLess(self.scan()["bytesRead"], 2048)

    def test_diagnostics_distinguish_expired_completion_and_running_binding_failure(self):
        self.event("task_started", started_at=NOW)
        with patch.object(reader, "execution_binding", return_value=False):
            result = self.scan()
        self.assertEqual(result["diagnostics"][0]["reason"], "binding_unverified")
        self.event("task_complete", completed_at=NOW - 2000)
        result = self.scan()
        completions = [d for d in result["diagnostics"] if d["kind"] == "completion"]
        self.assertEqual(completions[0]["reason"], "before_monitor_start")
        self.assertEqual(result["events"], [])

    def test_a_large_unknown_session_cannot_starve_other_running_sessions(self):
        self.event("task_started", started_at=NOW)
        self.event("agent_message", message="x" * (reader.MAX_CATCHUP_BYTES + reader.MAX_TAIL))
        second = self.home / "sessions" / "second.jsonl"
        second.write_text(json.dumps({"type": "session_meta", "payload": {"id": "s2", "source": "vscode", "originator": "codex_vscode", "cli_version": "0.160.0"}}) + "\n" +
                          json.dumps({"type": "event_msg", "payload": {"type": "task_started", "turn_id": "second-turn", "started_at": NOW}}) + "\n", encoding="utf-8")
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)", ("s2", "vscode", str(second), NOW - 1, None, "Second task", "0.160.0"))
            db.commit()
        self.scan()
        result = self.scan()
        self.assertIn("s2", [session["sessionId"] for session in result["activeSessions"]])
        self.assertLessEqual(result["bytesRead"], 8388608)

    def test_cold_backfill_uses_larger_batches_then_returns_to_steady_state(self):
        self.event("task_started", started_at=NOW)
        self.event("agent_message", message="PRIVATE" * (reader.MAX_CATCHUP_BYTES // 7 + reader.MAX_TAIL))
        first = self.scan()
        self.assertEqual(first["activeSessions"], [])
        self.assertTrue(first["activityBacklog"])
        self.assertGreater(first["bytesRead"], 1048576)
        self.assertLessEqual(first["bytesRead"], 8388608)
        second = self.scan()
        self.assertEqual(second["activeSessions"][0]["turnId"], "t1")
        self.assertFalse(second["activityBacklog"])
        self.assertEqual(first["events"] + second["events"], [])
        steady = self.scan()
        self.assertFalse(steady["activityBacklog"])
        self.assertLessEqual(steady["bytesRead"], 1048576)
        self.event("task_complete", completed_at=NOW)
        self.assertEqual(self.scan()["activeSessions"], [])

    def test_unknown_binding_and_invalid_metadata_do_not_request_fast_backfill(self):
        self.event("task_started", started_at=NOW)
        with patch.object(reader, "execution_binding", lambda _, diagnostic=None: False):
            result = self.scan()
            self.assertEqual(result["activeSessions"], [])
            self.assertFalse(result["activityBacklog"])
        self.path.write_text(json.dumps({"type": "session_meta", "payload": {"id": "wrong"}}) + "\n", encoding="utf-8")
        result = self.scan()
        self.assertEqual(result["activeSessions"], [])
        self.assertFalse(result["activityBacklog"])

    def test_baseline_inside_a_partial_record_never_replays_it(self):
        with self.path.open("ab") as stream:
            stream.write(b'{"type":"event_msg","payload":{"type":"task_complete",')
        self.scan()
        with self.path.open("ab") as stream:
            stream.write(('"turn_id":"historical","completed_at":'+str(NOW)+'}}\n').encode())
        self.assertEqual(self.scan()["events"], [])
        self.event("task_complete", "new", completed_at=NOW)
        self.assertEqual(self.scan()["events"][0]["turnId"], "new")

    def test_abort_is_remembered_between_reads_and_errors_never_escape(self):
        self.scan()
        self.event("task_started")
        self.event("turn_aborted", reason="PRIVATE")
        self.assertEqual(self.scan()["events"], [])
        self.event("task_complete", completed_at=NOW)
        self.event("task_complete", "failed", completed_at=NOW, error={"message": "PRIVATE ERROR"})
        result = self.scan()
        self.assertEqual(result["events"], [])
        self.assertNotIn("PRIVATE", json.dumps(result))

    def test_big_records_are_skipped_with_a_budget_and_can_recover(self):
        self.scan()
        self.event("agent_message", message="PRIVATE" * reader.MAX_TAIL)
        self.event("task_complete", "after-big", completed_at=NOW)
        found = []
        warned = False
        for _ in range(12):
            catchup = self.state.get("notificationBacklog", False)
            result = self.scan()
            self.assertLessEqual(result["bytesRead"], reader.MAX_CATCHUP_BYTES if catchup else reader.MAX_SCAN_BYTES)
            self.assertNotIn("PRIVATE", json.dumps(result))
            warned |= "oversized_record_skipped" in result["warnings"]
            found.extend(result["events"])
        self.assertTrue(warned)
        self.assertEqual([r["turnId"] for r in found], ["after-big"])

    def test_notification_backlog_uses_bounded_catchup_then_returns_to_normal(self):
        self.scan()
        self.event("agent_message", message="x" * (reader.MAX_CATCHUP_BYTES * 2))
        self.event("task_complete", "after-images", completed_at=NOW)
        seen = []
        batches = []
        for _ in range(8):
            result = self.scan()
            seen.extend(result["events"])
            batches.append(result)
            self.assertLessEqual(result["bytesRead"], reader.MAX_CATCHUP_BYTES)
            if result["events"]:
                break
        self.assertTrue(batches[0]["notificationBacklog"])
        self.assertTrue(any(batch["bytesRead"] > reader.MAX_SCAN_BYTES for batch in batches))
        self.assertEqual([event["turnId"] for event in seen], ["after-images"])
        self.assertLessEqual(len(batches), 5)
        steady = self.scan()
        self.assertFalse(steady["notificationBacklog"])
        self.assertLessEqual(steady["bytesRead"], reader.MAX_SCAN_BYTES)
        self.assertEqual(steady["events"], [])

    def test_record_limited_notification_backlog_also_requests_catchup(self):
        self.scan()
        for _ in range(310):
            self.event("agent_message", message="small record")
        self.event("task_complete", completed_at=NOW)
        first = self.scan()
        self.assertTrue(first["notificationBacklog"])
        seen = first["events"]
        for _ in range(5):
            result = self.scan()
            seen += result["events"]
        self.assertEqual([event["turnId"] for event in seen], ["t1"])
        self.assertFalse(result["notificationBacklog"])

    def test_partial_eof_and_unverified_completion_do_not_fast_poll(self):
        self.scan()
        self.state["notificationBacklog"] = True
        with self.path.open("ab") as stream:
            stream.write(b'{"type":"unfinished"')
        partial = self.scan()
        self.assertFalse(partial["notificationBacklog"])
        with self.path.open("ab") as stream:
            stream.write(b'}\n')
        self.event("task_complete", completed_at=NOW)
        self.state["notificationBacklog"] = True
        with patch.object(reader, "execution_binding", return_value=False):
            withheld = self.scan()
        self.assertEqual(withheld["events"], [])
        self.assertFalse(withheld["notificationBacklog"])
        self.assertIn("execution_source_unverified", withheld["warnings"])
        self.assertEqual([event["turnId"] for event in self.scan()["events"]], ["t1"])

    def test_exact_size_incomplete_eof_is_not_skipped_as_oversized(self):
        self.scan()
        before = self.state["cursors"]["s1"]["offset"]
        with self.path.open("ab") as stream:
            stream.write(b'{"type":"partial","data":"' + b'x' * (reader.MAX_TAIL - 26))
        self.assertEqual(self.path.stat().st_size - before, reader.MAX_TAIL)
        result = self.scan()
        self.assertEqual(result["cursors"]["s1"]["offset"], before)
        self.assertFalse(result["notificationBacklog"])

    def test_catchup_rotation_serves_all_peers_even_when_one_file_fills_batch(self):
        paths = []
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("DELETE FROM threads")
            for i in range(8):
                sid = "s" + str(i)
                path = self.home / "sessions" / (sid + ".jsonl")
                path.write_text(json.dumps({"type": "session_meta", "payload": {"id": sid, "source": "vscode"}}) + "\n", encoding="utf-8")
                db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)", (sid, "vscode", str(path), NOW, None, sid, "0.160.0"))
                paths.append(path)
            db.commit()
        self.scan()
        offsets = {sid: cursor["offset"] for sid, cursor in self.state["cursors"].items()}
        for path in paths:
            with path.open("ab") as stream:
                stream.write(json.dumps({"type": "event_msg", "payload": {"type": "agent_message", "message": "x" * reader.MAX_SCAN_BYTES}}).encode() + b"\n")
        self.state["notificationBacklog"] = True
        with patch.object(reader, "MAX_CATCHUP_BYTES", reader.MAX_SCAN_BYTES):
            for _ in range(8):
                self.scan()
        self.assertTrue(all(self.state["cursors"][sid]["offset"] > offset for sid, offset in offsets.items()))

    def test_missing_time_and_old_events_are_never_recovered_as_new(self):
        self.scan()
        self.event("task_complete", "old", completed_at=NOW - 2000)
        self.event("task_complete", "unknown")
        self.assertEqual(self.scan()["events"], [])

    def test_unverified_machine_retries_without_advancing_the_cursor(self):
        self.scan()
        self.event("task_complete", completed_at=NOW)
        with patch.object(reader, "execution_binding", return_value=False):
            result = self.scan()
            self.assertEqual(result["events"], [])
            self.assertIn("execution_source_unverified", result["warnings"])
        self.assertEqual(self.scan()["events"][0]["turnId"], "t1")

    def test_failed_verification_identifies_the_session_turn_and_os_stage_without_paths(self):
        self.scan()
        previous = dict(self.state["cursors"]["s1"])
        self.event("task_complete", completed_at=NOW)

        def failed(path, diagnostic=None):
            if diagnostic is not None:
                diagnostic.update(reason="windows_list_failed", osError=5)
            return False

        with patch.object(reader, "execution_binding", failed):
            result = self.scan()
        self.assertEqual(result["events"], [])
        self.assertEqual(result["cursors"]["s1"], previous)
        self.assertEqual(result["verificationFailures"], [{"sessionId": "s1", "sessionName": "Test task",
            "turnIds": ["t1"], "reason": "windows_list_failed", "osError": 5}])
        self.assertNotIn(str(self.path), json.dumps(result["verificationFailures"]))
        recovered = self.scan()
        self.assertEqual(recovered["verificationFailures"], [])
        self.assertEqual(recovered["events"][0]["turnId"], "t1")

    def test_subagent_is_excluded_and_version_changes_do_not_block_valid_events(self):
        self.scan()
        self.event("task_complete", completed_at=NOW)
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET source=?", ('{"subagent":{}}',))
            db.commit()
        self.assertEqual(self.scan()["events"], [])
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET source='vscode',cli_version='9.9.9'")
            db.commit()
        result = self.scan()
        self.assertEqual([event["turnId"] for event in result["events"]], ["t1"])
        self.assertNotIn("unsupported_codex_version", result["warnings"])

    def test_future_header_versions_and_additional_fields_keep_working(self):
        self.path.write_text("", encoding="utf-8")
        self.append({"type": "session_meta", "payload": {"id": "s1", "source": "vscode",
                     "cli_version": "9.99.0-preview.future", "future_metadata": {"extra": True}}})
        self.scan()
        self.event("task_started", started_at=NOW, future_statistic=10)
        self.assertEqual(self.scan()["activeSessions"][0]["turnId"], "t1")
        self.event("task_complete", completed_at=NOW, future_statistic=20)
        result = self.scan()
        self.assertEqual([event["turnId"] for event in result["events"]], ["t1"])
        self.assertEqual(result["warnings"], [])

    def test_version_and_originator_are_optional_diagnostics(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE replacement AS SELECT id,source,rollout_path,updated_at,title FROM threads")
            db.execute("DROP TABLE threads")
            db.execute("ALTER TABLE replacement RENAME TO threads")
            db.commit()
        self.path.write_text("", encoding="utf-8")
        self.append({"type": "session_meta", "payload": {"id": "s1", "source": "vscode"}})
        self.scan()
        self.event("task_started", started_at=NOW)
        self.assertEqual(self.scan()["activeSessions"][0]["turnId"], "t1")
        self.event("task_complete", completed_at=NOW)
        self.assertEqual(self.scan()["events"][0]["sessionName"], "Test task")

    def test_missing_lifecycle_identity_and_unknown_status_do_not_guess_success(self):
        self.event("task_started", started_at=NOW)
        self.assertEqual(len(self.scan()["activeSessions"]), 1)
        self.event("task_complete", turn=None, completed_at=NOW)
        result = self.scan()
        self.assertEqual(result["events"], [])
        self.assertEqual(result["activeSessions"], [])
        self.assertIn("lifecycle_identity_unavailable", result["warnings"])
        self.event("task_started", "future-status", started_at=NOW)
        self.event("task_complete", "future-status", completed_at=NOW, status="future_pending")
        result = self.scan()
        self.assertEqual(result["events"], [])
        self.assertIn("completion_status_unrecognized", result["warnings"])

    def test_missing_critical_database_columns_report_schema_incompatibility(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("DROP TABLE threads")
            db.execute("CREATE TABLE threads(id TEXT, future_data TEXT)")
            db.commit()
        with self.assertRaisesRegex(ValueError, "unsupported_database_schema"):
            self.scan()

    def test_changed_header_structure_is_rejected_independently_of_version(self):
        self.scan()
        self.path.write_text("", encoding="utf-8")
        self.append({"type": "future_session_meta", "payload": {"id": "s1", "source": "vscode", "cli_version": "0.160.0"}})
        self.event("task_complete", completed_at=NOW)
        result = self.scan()
        self.assertEqual(result["events"], [])
        self.assertEqual(result["activeSessions"], [])
        self.assertIn("session_metadata_mismatch", result["warnings"])

    def test_new_session_and_paginated_file_change(self):
        self.scan()
        previous = self.path
        self.path = self.path.with_name("new-page.jsonl")
        self.append({"type": "session_meta", "payload": {"id": "s1", "source": "vscode", "originator": "codex_vscode", "cli_version": "0.160.0", "history_mode": "paginated"}})
        self.event("task_complete", "new-page", completed_at=NOW)
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET rollout_path=?", (str(self.path),))
            db.commit()
        result = self.scan()
        self.assertIn("session_file_rebased", result["warnings"])
        self.assertEqual(result["events"][0]["turnId"], "new-page")
        self.assertTrue(previous.is_file())

    def test_record_budget_continues_next_read(self):
        self.scan()
        for _ in range(150):
            self.event("agent_message", message="private")
        self.event("task_complete", completed_at=NOW)
        self.assertEqual(self.scan()["events"], [])
        self.assertEqual(self.scan()["events"][0]["turnId"], "t1")

    def test_truncation_and_regrowth_are_detected_by_cursor_checkpoint(self):
        self.event("agent_message", message="original" * 10)
        self.scan()
        # Reuse the same inode and grow past the old offset between polls.
        self.path.write_bytes(b"")
        self.append({"type": "session_meta", "payload": {"id": "s1", "source": "vscode", "originator": "codex_vscode", "cli_version": "0.160.0", "history_mode": "legacy"}})
        self.event("agent_message", message="x" * 200)
        self.event("task_complete", "after-rewrite", completed_at=NOW)
        result = self.scan()
        self.assertIn("session_file_rebased", result["warnings"])
        self.assertEqual(result["events"][0]["turnId"], "after-rewrite")

    def test_new_parallel_session_and_restored_cursor_state(self):
        self.scan()
        self.state = json.loads(json.dumps(self.state))
        second = self.home / "sessions" / "parallel.jsonl"
        second.write_text(json.dumps({"type": "session_meta", "payload": {"id": "s2", "source": "vscode", "originator": "codex_vscode", "cli_version": "0.160.0"}}) + "\n" + json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "parallel", "completed_at": NOW}}) + "\n", encoding="utf8")
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?,?)", ("s2", "vscode", str(second), NOW, "Parallel", None, "0.160.0"))
            db.commit()
        self.event("task_complete", completed_at=NOW)
        self.assertEqual({e["sessionId"] for e in self.scan()["events"]}, {"s1", "s2"})

    def test_malformed_cursor_and_excessive_scope_are_rejected(self):
        for extra in [{"cursors": {"s1": {"offset": -1}}}, {"position": -1}, {"initialized": "yes"}, {"notificationBacklog": "yes"}, {"cursors": {str(i): {} for i in range(129)}}]:
            with self.assertRaises(ValueError):
                reader.scan({**self.state, **extra})


if __name__ == "__main__":
    unittest.main()
