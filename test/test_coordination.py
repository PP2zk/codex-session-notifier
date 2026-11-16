import concurrent.futures
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
import uuid


spec = importlib.util.spec_from_file_location("coordination_probe", Path(__file__).resolve().parents[1] / "resources/probe.py")
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def client_claim(directory, request):
    with patch.object(probe.Path, "home", return_value=Path(directory)):
        return probe.notification_claim(request)


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.codex = self.home / "codex"
        self.codex.mkdir()
        self.request = dict(mode="notification_claim", home=str(self.codex),
                            sourceId=probe.source_identity(self.codex), scope="b" * 64,
                            ticket=str(uuid.uuid4()), sessionId="session", turnId="turn",
                            completedAt=probe.datetime.now(probe.timezone.utc).isoformat())
        self.db = self.home / ".codex-session-notifier" / "delivery-v1.sqlite"

    def claim(self, **changes):
        return client_claim(str(self.home), dict(self.request, **changes))

    def test_independent_processes_and_restart_allow_one_attempt(self):
        requests = [dict(self.request, ticket=str(uuid.uuid4())) for _ in range(12)]
        with concurrent.futures.ProcessPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(client_claim, [str(self.home)] * len(requests), requests))
        self.assertEqual(sum(item["acquired"] for item in results), 1)
        self.assertEqual(self.claim()["reason"], "duplicate")
        self.assertEqual(list(self.codex.iterdir()), [])

    def test_keys_distinguish_turn_session_recipient_and_actual_source(self):
        self.assertTrue(self.claim()["acquired"])
        self.assertTrue(self.claim(turnId="next")["acquired"])
        self.assertTrue(self.claim(sessionId="other")["acquired"])
        self.assertTrue(self.claim(scope="c" * 64)["acquired"])
        other = self.home / "other-codex"
        other.mkdir()
        self.assertTrue(self.claim(home=str(other), sourceId=probe.source_identity(other))["acquired"])
        with self.assertRaisesRegex(ValueError, "source_changed"):
            self.claim(sourceId="a" * 64)

    def test_owner_can_release_only_its_unsent_claim(self):
        self.claim()
        self.claim(mode="notification_release", ticket=str(uuid.uuid4()))
        self.assertFalse(self.claim()["acquired"])
        self.claim(mode="notification_release")
        self.assertTrue(self.claim(ticket=str(uuid.uuid4()))["acquired"])

    def test_cleanup_cannot_make_expired_event_sendable(self):
        self.claim()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE claims SET claimed_at=?", (time.time() - 86401,))
        self.assertTrue(self.claim(turnId="next")["acquired"])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 1)
        old = probe.datetime.fromtimestamp(time.time() - 86401, probe.timezone.utc).isoformat()
        self.assertEqual(self.claim(completedAt=old)["reason"], "expired")
        future = probe.datetime.fromtimestamp(time.time() + 120, probe.timezone.utc).isoformat()
        self.assertEqual(self.claim(completedAt=future)["reason"], "expired")

    def test_capacity_preserves_live_claims_and_bounds_database(self):
        self.claim()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.executemany("INSERT INTO claims VALUES(?,?,?)", [(str(i), "ticket", time.time()) for i in range(9999)])
        with self.assertRaisesRegex(ValueError, "capacity_reached"):
            self.claim(turnId="next")
        self.assertFalse(self.claim()["acquired"])
        self.assertLess(self.db.stat().st_size, 16777216)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            conn.execute("UPDATE claims SET claimed_at=?", (time.time() - 86401,))
        self.assertTrue(self.claim(turnId="next")["acquired"])
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0], 1)

    def test_invalid_input_does_not_create_state(self):
        for changes in [dict(scope="secret"), dict(ticket="../escape"), dict(sessionId=""), dict(completedAt="not-a-date")]:
            with self.assertRaises(ValueError):
                self.claim(**changes)
        self.assertFalse(self.db.exists())

    def test_state_contains_only_hashed_event_ticket_and_time(self):
        self.claim()
        with closing(sqlite3.connect(self.db)) as conn, conn:
            row = conn.execute("SELECT * FROM claims").fetchone()
        self.assertEqual(len(row[0]), 64)
        self.assertEqual(row[1], self.request["ticket"])
        self.assertIsInstance(row[2], float)
        self.assertNotIn("session", json.dumps(row))
