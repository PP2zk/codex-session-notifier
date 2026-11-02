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

    def scan(self):
        result = reader.scan(self.state)
        self.state.update({k: result[k] for k in ("initialized", "cursors", "position", "activity")})
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
        with patch.object(reader, "execution_binding", lambda _: False):
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
        with patch.object(reader, "execution_binding", lambda _: False):
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
            result = self.scan()
            self.assertLessEqual(result["bytesRead"], 1048576)
            self.assertNotIn("PRIVATE", json.dumps(result))
            warned |= "oversized_record_skipped" in result["warnings"]
            found.extend(result["events"])
        self.assertTrue(warned)
        self.assertEqual([r["turnId"] for r in found], ["after-big"])

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
        for extra in [{"cursors": {"s1": {"offset": -1}}}, {"position": -1}, {"initialized": "yes"}, {"cursors": {str(i): {} for i in range(129)}}]:
            with self.assertRaises(ValueError):
                reader.scan({**self.state, **extra})


if __name__ == "__main__":
    unittest.main()
