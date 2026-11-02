import importlib.util
import ast
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

spec = importlib.util.spec_from_file_location("probe", Path(__file__).resolve().parents[1] / "resources/probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


class ProbeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        (self.home / "sessions").mkdir()
        self.path = self.home / "sessions" / "sample.jsonl"
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("CREATE TABLE threads(id TEXT, source TEXT, rollout_path TEXT, updated_at INTEGER, name TEXT, cli_version TEXT)")
            db.execute("INSERT INTO threads VALUES(?,?,?,?,?,?)", ("s1", "vscode", str(self.path), 1, "private session name", "0.160.0"))
            db.commit()

    def write(self, records):
        self.path.write_text("".join(json.dumps(row) + "\n" for row in records), encoding="utf-8")

    def metadata(self):
        return {"type": "session_meta", "payload": {"id": "s1", "source": "vscode", "cli_version": "0.160.0", "base_instructions": "PRIVATE INSTRUCTIONS"}}

    def test_returns_explicit_completion_without_body(self):
        self.write([self.metadata(), {"type": "event_msg", "timestamp": "2026-10-19T00:00:00Z", "payload": {"type": "task_complete", "turn_id": "turn1", "last_agent_message": "PRIVATE REPLY"}}])
        result = probe.probe({"home": str(self.home)})
        encoded = json.dumps(result)
        self.assertEqual(result["samples"][0]["completions"][0]["turnId"], "turn1")
        for private in ["PRIVATE", "last_agent_message", "base_instructions", "private session name"]:
            self.assertNotIn(private, encoded)

    def test_does_not_infer_completion_from_text_or_partial_records(self):
        self.write([self.metadata(), {"type": "event_msg", "payload": {"type": "agent_message", "message": "done"}}])
        with self.path.open("ab") as stream:
            stream.write(json.dumps({"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "partial"}}).encode())
        self.assertEqual(probe.probe({"home": str(self.home)})["samples"][0]["completions"], [])

    def test_does_not_read_a_database_supplied_external_file(self):
        external = self.home / "private.jsonl"
        external.write_text("secret", encoding="utf-8")
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET rollout_path=?", (str(external),))
            db.commit()
        report = probe.probe({"home": str(self.home)})
        self.assertEqual(report["samples"], [])
        self.assertIn("rollout_outside_sessions", report["warnings"])

    def test_archived_metadata_uses_its_own_native_store(self):
        self.write([self.metadata()])
        archive = self.home / "archived_sessions"
        archive.mkdir()
        destination = archive / self.path.name
        self.path.replace(destination)
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("ALTER TABLE threads ADD COLUMN archived INTEGER DEFAULT 0")
            db.execute("UPDATE threads SET archived=1,rollout_path=?", (str(destination),))
            db.commit()
        result = probe.probe({"home": str(self.home)})
        self.assertEqual(result["warnings"], [])
        self.assertTrue(result["samples"][0]["archived"])
        self.assertTrue(result["samples"][0]["fileExists"])

    def test_large_record_tail_stays_bounded(self):
        self.write([self.metadata(), {"type": "event_msg", "payload": {"type": "agent_message", "message": "x" * (probe.MAX_TAIL * 2)}}, {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "bounded"}}])
        report = probe.probe({"home": str(self.home)})
        self.assertEqual(report["samples"][0]["bytesRead"], probe.MAX_TAIL)
        self.assertEqual(report["samples"][0]["completions"][0]["turnId"], "bounded")

    def test_subagent_cannot_masquerade_as_a_vscode_session(self):
        meta = self.metadata()
        meta["payload"]["source"] = {"subagent": {"thread_spawn": {"parent_thread_id": "parent"}}}
        self.write([meta])
        self.assertEqual(probe.probe({"home": str(self.home)})["samples"], [])

    def test_numeric_completed_at_is_interpreted_as_seconds(self):
        self.assertEqual(probe.completion_time({"completed_at": 1792390394}, {}), "2026-10-19T06:13:14Z")
        self.assertIsNone(probe.completion_time({"completed_at": "not a date"}, {}))

    def test_name_filter_is_exact_and_does_not_disclose_the_name(self):
        self.write([self.metadata()])
        report = probe.probe({"home": str(self.home), "sessionName": "private session name"})
        self.assertEqual(len(report["samples"]), 1)
        self.assertNotIn("private session name", json.dumps(report))
        self.assertEqual(probe.probe({"home": str(self.home), "sessionName": "' OR 1=1 --"})["samples"], [])

    def test_native_title_is_used_when_explicit_name_is_missing(self):
        self.write([self.metadata()])
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("ALTER TABLE threads ADD COLUMN title TEXT")
            db.execute("ALTER TABLE threads ADD COLUMN first_user_message TEXT")
            db.execute("UPDATE threads SET name=NULL,title=?,first_user_message=?", ("Test task", "PRIVATE PROMPT"))
            db.commit()
        report = probe.probe({"home": str(self.home), "sessionName": "test task"})
        self.assertEqual(report["samples"][0]["nameSource"], "title")
        self.assertTrue(report["samples"][0]["hasName"])
        self.assertNotIn("Test task", json.dumps(report))
        self.assertNotIn("PRIVATE", json.dumps(report))
        self.assertEqual(probe.probe({"home": str(self.home), "sessionName": "test"})["samples"], [])
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET name=?", ("Renamed",))
            db.commit()
        self.assertEqual(probe.probe({"home": str(self.home), "sessionName": "test task"})["samples"], [])
        self.assertEqual(probe.probe({"home": str(self.home), "sessionName": "renamed"})["samples"][0]["nameSource"], "name")

    def test_never_uses_first_prompt_as_a_missing_name(self):
        self.assertEqual(probe.session_label({"name": " ", "title": "", "first_user_message": "PRIVATE"}), (None, "unset"))

    def test_queued_name_lookup_is_scoped_and_does_not_read_rollout_or_prompt(self):
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("ALTER TABLE threads ADD COLUMN title TEXT")
            db.execute("ALTER TABLE threads ADD COLUMN first_user_message TEXT")
            db.execute("UPDATE threads SET name=NULL,title=?,first_user_message=?", ("Test task", "PRIVATE PROMPT"))
            db.commit()
        # The rollout does not exist: this operation reads only named DB columns.
        report = probe.labels({"home": str(self.home), "sessionIds": ["s1", "' OR 1=1 --"]})
        self.assertEqual(report["sourceId"], probe.source_identity(self.home))
        self.assertEqual(report["labels"][0]["sessionName"], "Test task")
        self.assertEqual(report["labels"][0]["nameSource"], "title")
        self.assertEqual(report["labels"][1]["nameState"], "unavailable")
        self.assertNotIn("PRIVATE", json.dumps(report))
        with self.assertRaises(ValueError):
            probe.labels({"home": str(self.home), "sessionIds": ["s1"] * 11})

    def test_error_completion_is_not_success_and_does_not_disclose_error(self):
        self.write([self.metadata(), {"type": "event_msg", "payload": {
            "type": "task_complete", "turn_id": "failed", "error": {"message": "PRIVATE ERROR", "codex_error_info": "server_overloaded"}}}])
        report = probe.probe({"home": str(self.home)})
        self.assertEqual(report["samples"][0]["completions"], [])
        self.assertNotIn("PRIVATE", json.dumps(report))
        for invalid in [False, "error", {}]:
            self.write([self.metadata(), {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "failed", "error": invalid}}])
            self.assertEqual(probe.probe({"home": str(self.home)})["samples"][0]["completions"], [])

    def test_aborted_turn_cannot_be_reported_as_complete(self):
        for aborted in [{"type": "turn_aborted", "turn_id": "aborted"}, {"type": "turn_aborted"}]:
            self.write([self.metadata(), {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "aborted"}},
                        {"type": "event_msg", "payload": aborted},
                        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "aborted"}},
                        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "next"}},
                        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "next", "error": None}}])
            self.assertEqual([r["turnId"] for r in probe.probe({"home": str(self.home)})["samples"][0]["completions"]], ["next"])

    def test_waiting_and_failed_statuses_do_not_mean_complete(self):
        for kind in ["exec_approval_request", "request_user_input", "turn_aborted", "error"]:
            self.write([self.metadata(), {"type": "event_msg", "payload": {"type": kind, "turn_id": "waiting"}}])
            self.assertEqual(probe.probe({"home": str(self.home)})["samples"][0]["completions"], [])
        self.write([self.metadata(), {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "failed", "status": "failed"}}])
        self.assertEqual(probe.probe({"home": str(self.home)})["samples"][0]["completions"], [])

    def test_request_cannot_expand_read_scope(self):
        for extra in [{"limit": 6}, {"limit": True}, {"home": []}, {"sessionName": []}]:
            with self.assertRaises(ValueError):
                probe.probe({"home": str(self.home), **extra})

    def test_non_object_header_fails_closed(self):
        self.path.write_text("[]\n", encoding="utf-8")
        result = probe.probe({"home": str(self.home)})
        self.assertEqual(result["samples"], [])
        self.assertIn("session_metadata_mismatch", result["warnings"])

    def test_linux_binding_works_without_path_readlink_and_checks_process_owner(self):
        proc_root = self.home / "proc"
        proc = proc_root / "123"
        (proc / "fd").mkdir(parents=True)
        (proc / "fdinfo").mkdir()
        (proc / "comm").write_text("codex\n", encoding="utf-8")
        (proc / "cmdline").write_bytes(b"codex\0app-server\0")
        (proc / "fd" / "4").touch()
        (proc / "fdinfo" / "4").write_text("flags:\t0100001\n", encoding="utf-8")
        owner = proc.stat().st_uid
        def readlink(path):
            if path == proc / "exe":
                return "/home/test/.vscode-server/extensions/openai.chatgpt-1/bin/codex"
            if path == proc / "fd" / "4":
                return str(self.path.resolve())
            raise OSError("unexpected link")
        system = SimpleNamespace(name="posix", getuid=lambda: owner, readlink=readlink,
                                 O_ACCMODE=3, O_WRONLY=1, O_RDWR=2)
        with patch.object(probe, "os", system), patch.object(probe.platform, "system", lambda: "Linux"), \
                patch.object(probe, "Path", lambda value: proc_root if value == "/proc" else Path(value)), \
                patch.object(Path, "readlink", side_effect=AttributeError("unavailable on Python 3.8")):
            self.assertTrue(probe.execution_binding(self.path))
            system.getuid = lambda: owner + 1
            self.assertFalse(probe.execution_binding(self.path))

    def test_windows_resource_query_reports_the_failed_stage_and_preserves_cleanup(self):
        for function, reason in [("RmStartSession", "windows_session_failed"),
                                 ("RmRegisterResources", "windows_register_failed"),
                                 ("RmGetList", "windows_list_failed")]:
            with self.subTest(function=function):
                rm, kernel = MagicMock(), MagicMock()
                for name in ("RmStartSession", "RmRegisterResources", "RmGetList", "RmEndSession"):
                    getattr(rm, name).return_value = 0
                getattr(rm, function).return_value = 5
                detail = {}
                with patch("ctypes.WinDLL", side_effect=[rm, kernel], create=True):
                    self.assertFalse(probe.windows_file_users(self.path, detail))
                self.assertEqual(detail, {"reason": reason, "osError": 5})
                self.assertEqual(rm.RmEndSession.call_count, 0 if function == "RmStartSession" else 1)
                kernel.OpenProcess.assert_not_called()

    def test_runtime_readers_keep_python_37_compatible_syntax(self):
        for name in ("probe.py", "state.py"):
            source = (Path(__file__).resolve().parents[1] / "resources" / name).read_text(encoding="utf-8")
            ast.parse(source, feature_version=(3, 7))

    @unittest.skipUnless(os.name == "nt", "Windows extended paths only")
    def test_windows_extended_path_remains_inside_sessions(self):
        self.write([self.metadata()])
        with closing(sqlite3.connect(self.home / "state_5.sqlite")) as db:
            db.execute("UPDATE threads SET rollout_path=?", ("\\\\?\\" + str(self.path.resolve()),))
            db.commit()
        result = probe.probe({"home": str(self.home)})
        self.assertEqual(len(result["samples"]), 1)
        self.assertNotIn("rollout_outside_sessions", result["warnings"])


if __name__ == "__main__":
    unittest.main()
