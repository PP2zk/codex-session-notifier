from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import importlib.util
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
                            "activeTurn": "t1", "activeStartedAt": "2026-11-01T00:00:00Z", "sessionName": "PRIVATE"}}
        self.op("commit", revision=0, state={"position": 1, "activity": activity}, events=[])
        self.op("commit", revision=1, state={"position": 2}, events=[])
        saved = self.op("load")["state"]
        self.assertEqual(saved["position"], 2)
        self.assertEqual(saved["activity"]["s1"]["activeTurn"], "t1")
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
