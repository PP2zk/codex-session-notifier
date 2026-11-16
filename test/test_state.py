from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib.util
import io
import os
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from datetime import datetime, timezone
import time

spec = importlib.util.spec_from_file_location("local_state", Path(__file__).resolve().parents[1] / "resources/state.py")
local_state = importlib.util.module_from_spec(spec)
spec.loader.exec_module(local_state)


class StateTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = str(Path(self.temp.name) / "monitor.sqlite")
        self.source = "a" * 64
        self.event = {"sessionId": "s1", "turnId": "t1", "completedAt": "2026-10-19T06:37:38Z", "sessionName": "PRIVATE", "last_agent_message": "PRIVATE"}

    def op(self, operation, **extra):
        return local_state.operate(self.db, {"operation": operation, "sourceId": self.source, **extra})

    def test_atomic_compare_and_swap_across_windows(self):
        self.op("load")
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.op("commit", revision=0, state={"position": 1}, events=[self.event]), range(8)))
        self.assertEqual(sum(r["committed"] for r in results), 1)
        self.assertEqual(sum(len(r["added"]) for r in results), 1)
        self.assertEqual(self.op("load")["revision"], 1)

    def test_monitor_start_replaces_stale_boundary_and_invalidates_old_scan(self):
        now = int(time.time())
        state = {"startedAt": now - 7200, "initialized": True, "cursors": {"s1": {"offset": 10}}}
        self.op("commit", revision=0, state=state, events=[{**self.event, "completedAt": datetime.fromtimestamp(now - 100, timezone.utc).isoformat()}])
        joined = self.op("monitor_join", owner="a" * 36, pid=os.getpid(), startedAt=now - 5)
        saved = self.op("load")
        self.assertEqual(joined["startedAt"], now - 5)
        self.assertEqual(joined["diagnostics"][0]["reason"], "monitor_started")
        self.assertEqual(saved["state"], {**state, "startedAt": now - 5})
        self.assertEqual(saved["revision"], 2)
        self.assertFalse(self.op("commit", revision=1, state=state, events=[])["committed"])
        self.assertEqual(saved["knownCompletions"], [{"sessionId": "s1", "turnId": "t1"}])

    def test_live_same_source_peer_preserves_boundary_even_after_network_outage(self):
        now = int(time.time())
        self.op("commit", revision=0, state={"startedAt": now - 7200}, events=[])
        first = self.op("monitor_join", owner="a" * 36, pid=os.getpid(), startedAt=now - 10)
        with patch.object(local_state.time, "time", return_value=now + 3600):
            peer = self.op("monitor_join", owner="b" * 36, pid=os.getpid(), startedAt=now + 3600)
        self.assertEqual(peer["startedAt"], first["startedAt"])
        self.assertEqual(peer["diagnostics"][0]["reason"], "monitor_joined")
        self.op("monitor_leave", owner="a" * 36)
        third = self.op("monitor_join", owner="c" * 36, pid=os.getpid(), startedAt=now)
        self.assertEqual(third["startedAt"], first["startedAt"])
        self.op("monitor_leave", owner="b" * 36)
        self.op("monitor_leave", owner="c" * 36)
        restarted = self.op("monitor_join", owner="d" * 36, pid=os.getpid(), startedAt=now)
        self.assertEqual(restarted["startedAt"], now)

    def test_dead_or_reused_process_and_other_source_are_not_live_peers(self):
        now = int(time.time())
        for sid, token in [(self.source, "a" * 36), ("b" * 64, "b" * 36)]:
            self.op("commit", sourceId=sid, revision=0, state={"startedAt": now - 100}, events=[])
            with patch.object(local_state, "process_started_at", return_value=now - 100):
                self.op("monitor_join", sourceId=sid, owner=token, pid=1234, startedAt=now - 100)
        # Reusing the PID with a later birth invalidates both stale owners.
        with patch.object(local_state, "process_started_at", return_value=now - 50):
            joined = self.op("monitor_join", owner="c" * 36, pid=1234, startedAt=now - 10)
        self.assertEqual(joined["startedAt"], now - 10)
        with patch.object(local_state, "process_started_at", side_effect=lambda pid: now - 50 if pid == 1234 else now):
            other = self.op("monitor_join", sourceId="b" * 64, owner="d" * 36, pid=5678, startedAt=now)
        self.assertEqual(other["startedAt"], now, "a live peer for another machine cannot keep an old cutoff")
        with patch.object(local_state, "process_started_at", side_effect=lambda pid: now if pid == 9999 else None):
            dead = self.op("monitor_join", owner="e" * 36, pid=9999, startedAt=now)
        self.assertEqual(dead["startedAt"], now)

    def test_monitor_start_filter_applies_to_claim_and_prepared_send(self):
        now = int(time.time())
        self.op("qq_enable")
        state = {"startedAt": now - 60}
        event = {**self.event, "completedAt": datetime.fromtimestamp(now + 1, timezone.utc).isoformat()}
        self.op("commit", revision=0, state=state, events=[event])
        claimed = self.op("qq_claim", ticket="a" * 36)
        self.assertIsNotNone(claimed["item"])
        with patch.object(local_state.time, "time", return_value=now + 5):
            self.op("monitor_join", owner="b" * 36, pid=os.getpid(), startedAt=now + 5)
        self.assertFalse(self.op("qq_begin", ticket="a" * 36)["updated"])
        self.op("qq_finish", ticket="a" * 36, outcome="retry")
        self.assertIsNone(self.op("qq_claim", ticket="c" * 36)["item"])

    def test_diagnostics_survive_reload_and_record_state_changes_without_poll_spam(self):
        now = time.time()
        item = {"kind": "activity", "sessionId": "s1", "sessionName": "Test task", "turnId": "t1",
                "state": "running", "reason": "task_started", "offset": 100,
                "last_agent_message": "PRIVATE BODY", "path": "PRIVATE PATH"}
        with patch.object(local_state.time, "time", return_value=now):
            first = self.op("commit", revision=0, state={}, events=[], diagnostics=[item])
            unchanged = self.op("commit", revision=1, state={}, events=[], diagnostics=[{**item, "offset": 110}])
            failed = self.op("commit", revision=1, state={}, events=[], diagnostics=[{**item, "state": "unknown", "reason": "unread_changes"}])
            changed = self.op("commit", revision=2, state={}, events=[], diagnostics=[{**item, "state": "unknown", "reason": "read_budget_exhausted"}])
        self.assertEqual(len(first["diagnostics"]), 1)
        self.assertEqual(unchanged["diagnostics"], [])
        self.assertFalse(failed["committed"])
        self.assertEqual(changed["diagnostics"][0]["reason"], "read_budget_exhausted")
        entries = self.op("diagnostics")["entries"]
        self.assertEqual(len(entries), 2)
        self.assertNotIn("PRIVATE", json.dumps(entries))
        self.assertEqual(entries[0]["sourceId"], self.source)
        with patch.object(local_state.time, "time", return_value=now + 301):
            heartbeat = self.op("commit", revision=3, state={}, events=[], diagnostics=[{**item, "state": "unknown", "reason": "read_budget_exhausted"}])
        self.assertEqual(len(heartbeat["diagnostics"]), 1)

    def test_diagnostic_history_has_age_and_capacity_limits(self):
        self.op("load")
        now = time.time()
        detail = json.dumps({"kind": "activity", "sessionId": "s1", "state": "idle", "reason": "task_ended"})
        with closing(sqlite3.connect(self.db)) as db:
            db.executemany("INSERT INTO diagnostics(observed_at,source_id,kind,session_id,details) VALUES(?,?,?,?,?)",
                           [(now, self.source, "activity", "s1", detail)] * 4100)
            db.execute("INSERT INTO diagnostics(observed_at,source_id,kind,session_id,details) VALUES(?,?,?,?,?)",
                       (now - 8 * 86400, self.source, "activity", "old", detail))
            db.commit()
        self.op("commit", revision=0, state={}, events=[], diagnostics=[])
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM diagnostics").fetchone()[0], 4096)
            self.assertIsNone(db.execute("SELECT 1 FROM diagnostics WHERE session_id='old'").fetchone())
        self.assertEqual(len(self.op("diagnostics")["entries"]), 100)

    def test_oversized_unicode_and_bad_diagnostics_cannot_block_completion_commit(self):
        item = {"kind": "activity", "sessionId": "s1", "sessionName": "\U0001f600" * 96,
                "turnId": "t1", "state": "running", "reason": "task_started"}
        result = self.op("commit", revision=0, state={}, events=[self.event],
                         diagnostics=[item, {**item, "fileBytes": -1}, {"kind": "invalid"}])
        self.assertTrue(result["committed"])
        self.assertEqual(len(result["added"]), 1)
        self.assertEqual(len(result["diagnostics"]), 1)
        self.assertTrue(result["diagnostics"][0]["sessionName"].startswith("\U0001f600"))

    def test_diagnostic_commit_response_stays_within_transport_budget(self):
        items = [{"kind": "activity", "sessionId": str(i), "sessionName": "\U0001f600" * 64,
                  "turnId": "t1", "state": "running", "reason": "task_started"} for i in range(256)]
        result = self.op("commit", revision=0, state={}, events=[self.event], diagnostics=items)
        self.assertTrue(result["committed"])
        self.assertLess(len(json.dumps(result).encode("utf-8")), 262144)
        with closing(sqlite3.connect(self.db)) as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM diagnostics").fetchone()[0], 256)

    def test_turns_and_sources_are_distinct_and_names_are_not_persisted(self):
        first = self.op("commit", revision=0, state={}, events=[self.event])
        duplicate = self.op("commit", revision=1, state={}, events=[self.event])
        second = self.op("commit", revision=2, state={}, events=[{**self.event, "turnId": "t2"}])
        other = self.op("commit", sourceId="b" * 64, revision=0, state={}, events=[self.event])
        self.assertEqual([len(r["added"]) for r in (first, duplicate, second, other)], [1, 0, 1, 1])
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertNotIn("PRIVATE", json.dumps(conn.execute("SELECT * FROM outbox").fetchall()))

    def test_load_projects_recent_known_ids_for_only_the_requested_source(self):
        now = time.time()
        with patch.object(local_state.time, "time", return_value=now):
            self.op("commit", revision=0, state={"position": 1}, events=[self.event])
            saved = self.op("load")
            self.assertEqual(saved["knownCompletions"], [{"sessionId": "s1", "turnId": "t1"}])
            self.assertEqual(saved["state"], {"position": 1})
            self.assertNotIn("PRIVATE", json.dumps(saved))
            self.assertEqual(self.op("load", sourceId="b" * 64)["knownCompletions"], [])
        with patch.object(local_state.time, "time", return_value=now + 1801):
            self.assertEqual(self.op("load")["knownCompletions"], [])

    def test_full_queue_does_not_advance_cursors_or_partially_commit(self):
        self.op("commit", revision=0, state={"position": 1}, events=[{**self.event, "turnId": str(i)} for i in range(100)])
        self.op("commit", revision=1, state={"position": 2}, events=[{**self.event, "turnId": str(i)} for i in range(100, 200)])
        with self.assertRaises(ValueError):
            self.op("commit", revision=2, state={"position": 3}, events=[{**self.event, "turnId": "overflow"}])
        self.assertEqual(self.op("load")["state"]["position"], 2)
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0], 200)

    def test_source_lease_excludes_other_windows_and_is_owner_scoped(self):
        owner = "a" * 36
        other = "b" * 36
        self.assertTrue(self.op("lease", owner=owner)["acquired"])
        self.assertFalse(self.op("lease", owner=other)["acquired"])
        self.op("release", owner=other)
        self.assertFalse(self.op("lease", owner=other)["acquired"])
        self.op("release", owner=owner)
        self.assertTrue(self.op("lease", owner=other)["acquired"])

    def seed_qq(self):
        self.op("qq_enable")
        event = {**self.event, "completedAt": datetime.fromtimestamp(time.time() + 1, timezone.utc).isoformat()}
        self.op("commit", revision=0, state={}, events=[event])

    def test_process_birth_time_is_read_only_and_uses_real_process_lifetime(self):
        started = self.op("process_start", pid=os.getpid())["startedAt"]
        self.assertIsInstance(started, int)
        self.assertTrue(0 < started <= time.time())
        self.assertFalse(Path(self.db).exists())
        for pid in (None, True, -1, "123", 2 ** 31):
            self.assertIsNone(self.op("process_start", pid=pid)["startedAt"])

    def test_linux_process_start_handles_parentheses_and_uses_clock_tick_units(self):
        fields = ["S"] + ["0"] * 18 + ["12345"]
        files = {"/proc/123/stat": "123 (Code Helper (Plugin)) " + " ".join(fields),
                 "/proc/stat": "cpu 0 0 0\nbtime 1794656000\n"}
        with patch.object(local_state.os, "name", "posix"), patch.object(local_state, "Path") as paths, \
                patch("builtins.open", side_effect=lambda path: io.StringIO(files[path])), \
                patch.object(local_state.os, "sysconf", return_value=100, create=True), \
                patch.object(local_state.time, "time", return_value=1794658000):
            paths.return_value.is_file.return_value = True
            self.assertEqual(local_state.process_started_at(123), 1794656123)

    def test_unix_process_start_uses_utc_ps_without_a_shell(self):
        result = local_state.subprocess.CompletedProcess([], 0, stdout="Sat Nov 14 11:48:32 2026\n")
        with patch.object(local_state.os, "name", "posix"), patch.object(local_state, "Path") as paths, \
                patch.object(local_state.subprocess, "run", return_value=result) as run, \
                patch.object(local_state.time, "time", return_value=1794658000):
            paths.return_value.is_file.return_value = False
            self.assertEqual(local_state.process_started_at(123), 1794656912)
            self.assertEqual(run.call_args.args[0], ["ps", "-p", "123", "-o", "lstart="])
            self.assertEqual(run.call_args.kwargs["env"]["TZ"], "UTC")
            self.assertEqual(run.call_args.kwargs["timeout"], 2)

    def test_qq_claim_skips_pre_startup_pending_but_keeps_post_startup_pending(self):
        now = time.time()
        with patch.object(local_state.time, "time", return_value=now - 600):
            self.op("qq_enable")
        old = {**self.event, "turnId": "old", "completedAt": datetime.fromtimestamp(now - 120, timezone.utc).isoformat()}
        new = {**self.event, "turnId": "new", "completedAt": datetime.fromtimestamp(now - 30, timezone.utc).isoformat()}
        self.op("commit", revision=0, state={"startedAt": now - 60}, events=[old, new])
        item = self.op("qq_claim", ticket="a" * 36)["item"]
        self.assertEqual(item["turnId"], "new")
        self.assertEqual(self.op("qq_status")["counts"], {"preparing": 1})

    def test_qq_begin_rechecks_startup_after_a_pending_claim(self):
        now = time.time()
        with patch.object(local_state.time, "time", return_value=now - 600):
            self.op("qq_enable")
        event = {**self.event, "completedAt": datetime.fromtimestamp(now - 30, timezone.utc).isoformat()}
        self.op("commit", revision=0, state={"startedAt": now - 60}, events=[event])
        self.assertIsNotNone(self.op("qq_claim", ticket="a" * 36)["item"])
        self.op("commit", revision=1, state={"startedAt": now - 10}, events=[])
        self.assertFalse(self.op("qq_begin", ticket="a" * 36)["updated"])
        self.op("qq_finish", ticket="a" * 36, outcome="retry")
        self.assertIsNone(self.op("qq_claim", ticket="b" * 36)["item"])

    def test_qq_claim_and_finish_exclude_windows_and_never_resend_accepted(self):
        self.seed_qq()
        tickets = [str(i).zfill(36) for i in range(8)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda ticket: self.op("qq_claim", ticket=ticket), tickets))
        winners = [ticket for ticket, result in zip(tickets, results) if result["item"]]
        self.assertEqual(len(winners), 1)
        ticket = winners[0]
        self.assertTrue(self.op("qq_begin", ticket=ticket)["updated"])
        self.assertFalse(self.op("qq_finish", ticket=ticket, outcome="retry")["updated"])
        self.assertTrue(self.op("qq_finish", ticket=ticket, outcome="accepted", receipt="receipt")["updated"])
        with patch.object(local_state.time, "time", return_value=time.time() + 14):
            self.assertIsNone(self.op("qq_claim", ticket="b" * 36)["item"])
        self.assertEqual(self.op("qq_status")["counts"], {"accepted": 1})

    def test_qq_default_off_enable_skips_history_and_disable_blocks_prepared_send(self):
        self.assertFalse(self.op("qq_status")["enabled"])
        self.op("commit", revision=0, state={}, events=[self.event])
        self.op("qq_enable")
        self.assertIsNone(self.op("qq_claim", ticket="a" * 36)["item"])
        event = {**self.event, "turnId": "new", "completedAt": datetime.fromtimestamp(time.time() + 1, timezone.utc).isoformat()}
        self.op("commit", revision=1, state={}, events=[event])
        self.assertIsNotNone(self.op("qq_claim", ticket="a" * 36)["item"])
        self.op("qq_disable")
        self.assertFalse(self.op("qq_begin", ticket="a" * 36)["updated"])
        self.op("qq_finish", ticket="a" * 36, outcome="retry")
        self.assertFalse(self.op("qq_status")["enabled"])
        self.op("qq_enable")
        self.assertFalse(self.op("qq_begin", ticket="a" * 36)["updated"])

    def test_qq_crash_before_http_can_retry_but_crash_after_begin_is_unknown(self):
        self.seed_qq()
        self.op("qq_claim", ticket="a" * 36)
        with patch.object(local_state.time, "time", return_value=time.time() + 46):
            self.assertIsNotNone(self.op("qq_claim", ticket="b" * 36)["item"])
            self.op("qq_begin", ticket="b" * 36)
        with patch.object(local_state.time, "time", return_value=time.time() + 92):
            self.assertIsNone(self.op("qq_claim", ticket="c" * 36)["item"])
            self.assertEqual(self.op("qq_status")["counts"], {"unknown": 1})

    def test_qq_rate_gate_and_provider_rejection_are_shared_across_sources(self):
        self.seed_qq()
        other = "b" * 64
        event = {**self.event, "completedAt": datetime.fromtimestamp(time.time() + 1, timezone.utc).isoformat()}
        self.op("commit", sourceId=other, revision=0, state={}, events=[event])
        self.op("qq_claim", ticket="a" * 36)
        self.op("qq_begin", ticket="a" * 36)
        self.op("qq_finish", ticket="a" * 36, outcome="accepted", receipt="receipt")
        self.assertIsNone(self.op("qq_claim", sourceId=other, ticket="b" * 36)["item"])
        with patch.object(local_state.time, "time", return_value=time.time() + 14):
            self.assertIsNotNone(self.op("qq_claim", sourceId=other, ticket="b" * 36)["item"])
            self.op("qq_begin", ticket="b" * 36)
            self.op("qq_finish", ticket="b" * 36, outcome="rejected", code="qq_rejected_900")
        self.assertEqual(self.op("qq_status")["paused"], "qq_rejected_900")

    def test_activity_snapshots_are_shared_projected_and_expire_without_changing_outbox(self):
        self.op("commit", revision=0, state={}, events=[self.event])
        self.op("qq_enable")
        session = {"sessionId": "s1", "turnId": "t1", "sessionName": "Test task", "nameState": "resolved", "startedAt": None,
                   "prompt": "PRIVATE PROMPT", "reply": "PRIVATE REPLY"}
        self.op("activity_publish", machine="Host A", sessions=[session], unknown=0)
        self.op("activity_publish", sourceId="b" * 64, machine="Host B", sessions=[session], unknown=1)
        overview = self.op("activity_overview")
        self.assertEqual({row["machine"] for row in overview["sources"]}, {"Host A", "Host B"})
        self.assertTrue(overview["qq"]["enabled"])
        self.assertNotIn("PRIVATE", json.dumps(overview))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertNotIn("PRIVATE", json.dumps(conn.execute("SELECT * FROM activity_current").fetchall()))
            self.assertNotIn("Test task", json.dumps(conn.execute("SELECT * FROM outbox").fetchall()))
        with patch.object(local_state.time, "time", return_value=time.time() + 91):
            self.assertEqual(self.op("activity_overview")["sources"], [])
            self.assertTrue(self.op("qq_status")["enabled"])

    def test_activity_progress_survives_legacy_cursor_writes_without_persisting_labels(self):
        activity = {"s1": {"fileKey": "a" * 64, "observedSize": 100, "activityLookback": 50, "activityKnown": True,
                            "activityInitChecked": True, "activityInitializationOnly": False,
                            "activityOffset": 90, "activityAnchor": "b" * 64,
                            "activeTurn": "t1", "activeStartedAt": "2026-11-01T00:00:00Z", "sessionName": "PRIVATE"}}
        self.op("commit", revision=0, state={"position": 1, "activity": activity}, events=[])
        self.op("commit", revision=1, state={"position": 2}, events=[])
        saved = self.op("load")["state"]
        self.assertEqual(saved["position"], 2)
        self.assertEqual(saved["activity"]["s1"]["activeTurn"], "t1")
        self.assertTrue(saved["activity"]["s1"]["activityInitChecked"])
        self.assertFalse(saved["activity"]["s1"]["activityInitializationOnly"])
        self.assertEqual(saved["activity"]["s1"]["activityOffset"], 90)
        self.assertEqual(saved["activity"]["s1"]["activityAnchor"], "b" * 64)
        self.assertNotIn("PRIVATE", json.dumps(saved))
        with closing(sqlite3.connect(self.db)) as conn:
            self.assertNotIn("activity", json.loads(conn.execute("SELECT state FROM sources").fetchone()[0]))

    def test_legacy_activity_snapshot_cannot_overwrite_current_version(self):
        session = {"sessionId": "s1", "turnId": "t1", "sessionName": "Test task", "nameState": "resolved", "startedAt": None}
        self.op("activity_publish", machine="Host", sessions=[session], unknown=0)
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE activity(source_id TEXT PRIMARY KEY,machine TEXT,sessions TEXT,unknown_count INTEGER,updated_at REAL)")
            conn.execute("INSERT INTO activity VALUES(?,?,?,?,?)", (self.source, "Host", "[]", 1, time.time()))
            conn.commit()
        overview = self.op("activity_overview")
        self.assertEqual(overview["sources"][0]["runningCount"], 1)


    def test_route_upgrade_preserves_legacy_qq_enabled_state(self):
        # Construct the pre-feature schema independently of the new operations.
        with closing(sqlite3.connect(self.db)) as conn:
            conn.execute("CREATE TABLE qq_channel(id INTEGER PRIMARY KEY, enabled_at REAL, paused TEXT, next_at REAL NOT NULL DEFAULT 0, ticket TEXT, expires REAL)")
            conn.execute("INSERT INTO qq_channel VALUES(1,123,NULL,0,NULL,NULL)")
            conn.commit()
        state = self.op("qq_status")
        self.assertTrue(state["enabled"])
        self.assertEqual(state["route"], {"channel": "qq", "option": ""})
        self.assertEqual(state["routeRevision"], 0)

    def test_route_switch_is_atomic_disables_and_blocks_prepared_or_stale_sends(self):
        self.seed_qq()
        self.assertIsNotNone(self.op("qq_claim", ticket="a" * 36, routeRevision=0)["item"])
        changed = self.op("qq_configure", channel="wechat", option="", routeRevision=0)
        self.assertTrue(changed["updated"])
        state = self.op("qq_status")
        self.assertFalse(state["enabled"])
        self.assertEqual(state["route"], {"channel": "wechat", "option": ""})
        self.assertEqual(state["routeRevision"], 1)
        self.assertFalse(self.op("qq_enable", routeRevision=0)["updated"])
        self.assertFalse(self.op("qq_configure", channel="sms", option="", routeRevision=0)["updated"])
        self.op("qq_enable", routeRevision=1)
        self.assertFalse(self.op("qq_begin", ticket="a" * 36, routeRevision=0)["updated"])
        self.assertIsNone(self.op("qq_claim", ticket="b" * 36, routeRevision=0)["item"])
        # A fresh completion can use the current route, and retains deduplication.
        event = {**self.event, "turnId": "new", "completedAt": datetime.fromtimestamp(time.time() + 1, timezone.utc).isoformat()}
        self.op("commit", revision=1, state={}, events=[event])
        item = self.op("qq_claim", ticket="c" * 36, routeRevision=1)["item"]
        self.assertIsNotNone(item)
        self.assertTrue(self.op("qq_begin", ticket="c" * 36, routeRevision=1)["updated"])
        self.op("qq_finish", ticket="c" * 36, outcome="accepted", receipt="r", routeRevision=1)
        self.assertEqual(self.op("qq_status")["counts"], {"accepted": 1})

    def test_old_inflight_rejection_does_not_pause_new_route(self):
        self.seed_qq()
        self.op("qq_claim", ticket="a" * 36, routeRevision=0)
        self.op("qq_begin", ticket="a" * 36, routeRevision=0)
        self.op("qq_configure", channel="mail", option="", routeRevision=0)
        self.op("qq_enable", routeRevision=1)
        self.op("qq_finish", ticket="a" * 36, outcome="rejected", code="qq_rejected_900", routeRevision=0)
        state = self.op("qq_status")
        self.assertTrue(state["enabled"])
        self.assertIsNone(state["paused"])
        self.assertEqual(state["counts"], {"rejected": 1})

    def test_route_validation_and_same_selection_preserve_state(self):
        self.op("qq_enable")
        self.assertTrue(self.op("qq_configure", channel="qq", option="", routeRevision=0)["updated"])
        self.assertTrue(self.op("qq_status")["enabled"])
        for channel, option in [("qq,wechat", ""), ("webhook", ""), ("cp", ""), ("webhook", "https://private/hook"), ("mail", "bad\ncode")]:
            with self.assertRaises(ValueError):
                self.op("qq_configure", channel=channel, option=option, routeRevision=0)
        self.assertEqual(self.op("qq_status")["routeRevision"], 0)


if __name__ == "__main__":
    unittest.main()
