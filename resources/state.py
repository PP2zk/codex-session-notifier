"""Local-only SQLite cursors and atomic outbox. Never invoked over SSH."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import re
import sqlite3
import sys
import time
import unicodedata
from datetime import datetime


def qq_operation(conn, request):
    """One shared local channel; a started HTTP send is never retried blindly."""
    conn.execute("CREATE TABLE IF NOT EXISTS qq_channel(id INTEGER PRIMARY KEY, enabled_at REAL, paused TEXT, next_at REAL NOT NULL DEFAULT 0, ticket TEXT, expires REAL)")
    conn.execute("INSERT OR IGNORE INTO qq_channel(id) VALUES(1)")
    conn.execute("CREATE TABLE IF NOT EXISTS notification_route(id INTEGER PRIMARY KEY, channel TEXT NOT NULL, option TEXT NOT NULL, revision INTEGER NOT NULL)")
    conn.execute("INSERT OR IGNORE INTO notification_route VALUES(1,'qq','',0)")
    route = conn.execute("SELECT channel,option,revision FROM notification_route WHERE id=1").fetchone()
    conn.execute("CREATE TABLE IF NOT EXISTS qq_delivery(source_id TEXT, session_id TEXT, turn_id TEXT, status TEXT NOT NULL, ticket TEXT NOT NULL, receipt TEXT, code TEXT, PRIMARY KEY(source_id,session_id,turn_id))")
    now = time.time()
    channel = conn.execute("SELECT enabled_at,paused,next_at,ticket,expires FROM qq_channel WHERE id=1").fetchone()
    if channel[3] and channel[4] <= now:
        # A crash before qq_begin is safe to retry. After it, delivery is unknown.
        conn.execute("DELETE FROM qq_delivery WHERE ticket=? AND status='preparing'", (channel[3],))
        conn.execute("UPDATE qq_delivery SET status='unknown',code='qq_delivery_unknown' WHERE ticket=? AND status='sending'", (channel[3],))
        conn.execute("UPDATE qq_channel SET ticket=NULL,expires=NULL WHERE id=1")
        channel = (*channel[:3], None, None)
    conn.execute("DELETE FROM qq_delivery WHERE NOT EXISTS(SELECT 1 FROM outbox o WHERE o.source_id=qq_delivery.source_id AND o.session_id=qq_delivery.session_id AND o.turn_id=qq_delivery.turn_id)")
    operation = request["operation"]
    if operation == "qq_configure":
        selected, option = request.get("channel"), request.get("option")
        if selected not in ("qq", "wechat", "clawbot", "mail", "app", "extension", "webhook", "cp", "cmcc", "sms", "voice") or not isinstance(option, str) or len(option) > 256 or option != option.strip() or any(unicodedata.category(c) in ("Cc", "Cf") for c in option) or option.lower().startswith(("http:", "https:")) or selected in ("webhook", "cp") and not option:
            raise ValueError("invalid_route")
        if request.get("routeRevision") != route[2]:
            return {"updated": False}
        if (selected, option) == route[:2]:
            return {"updated": True}
        if channel[3] and conn.execute("DELETE FROM qq_delivery WHERE ticket=? AND status='preparing'", (channel[3],)).rowcount:
            conn.execute("UPDATE qq_channel SET ticket=NULL,expires=NULL WHERE id=1")
        conn.execute("UPDATE qq_channel SET enabled_at=NULL,paused=NULL WHERE id=1")
        conn.execute("UPDATE notification_route SET channel=?,option=?,revision=revision+1 WHERE id=1", (selected, option))
        return {"updated": True}
    if operation in ("qq_enable", "qq_claim", "qq_begin") and request.get("routeRevision", 0) != route[2]:
        return {"item": None} if operation == "qq_claim" else {"updated": False}
    if operation in ("qq_enable", "qq_disable"):
        if channel[3] and conn.execute("DELETE FROM qq_delivery WHERE ticket=? AND status='preparing'", (channel[3],)).rowcount:
            conn.execute("UPDATE qq_channel SET ticket=NULL,expires=NULL WHERE id=1")
        conn.execute("UPDATE qq_channel SET enabled_at=?,paused=NULL WHERE id=1", (now if operation == "qq_enable" else None,))
        return {"enabled": operation == "qq_enable"}
    if operation == "qq_status":
        counts = dict(conn.execute("SELECT status,COUNT(*) FROM qq_delivery GROUP BY status"))
        return {"enabled": channel[0] is not None, "paused": channel[1], "counts": counts,
                "route": {"channel": route[0], "option": route[1]}, "routeRevision": route[2]}
    ticket = request.get("ticket")
    if not isinstance(ticket, str) or not re.fullmatch("[0-9a-f-]{36}", ticket):
        raise ValueError("invalid_ticket")
    if operation == "qq_claim":
        if channel[0] is None or channel[1] or channel[3] or channel[2] > now:
            return {"item": None}
        rows = conn.execute("SELECT o.session_id,o.turn_id,o.completed_at FROM outbox o LEFT JOIN qq_delivery d ON o.source_id=d.source_id AND o.session_id=d.session_id AND o.turn_id=d.turn_id WHERE o.source_id=? AND d.status IS NULL AND o.queued_at>=? ORDER BY o.queued_at LIMIT 200", (request["sourceId"], now - 1800))
        for sid, turn, completed in rows:
            try:
                completed_at = datetime.fromisoformat(completed.replace("Z", "+00:00"))
                timestamp = completed_at.timestamp() if completed_at.tzinfo else 0
            except (ValueError, OverflowError):
                timestamp = 0
            if not max(channel[0], now - 1800) <= timestamp <= now + 60:
                continue
            conn.execute("INSERT INTO qq_delivery VALUES(?,?,?,'preparing',?,NULL,NULL)", (request["sourceId"], sid, turn, ticket))
            conn.execute("UPDATE qq_channel SET ticket=?,expires=? WHERE id=1", (ticket, now + 45))
            return {"item": {"sessionId": sid, "turnId": turn, "completedAt": completed}}
        return {"item": None}
    if channel[3] != ticket:
        return {"updated": False}
    if operation == "qq_begin":
        if channel[0] is None or channel[1]:
            return {"updated": False}
        changed = conn.execute("UPDATE qq_delivery SET status='sending' WHERE ticket=? AND status='preparing'", (ticket,)).rowcount
        if changed:
            # Respect the provider's five requests/minute cap across windows.
            conn.execute("UPDATE qq_channel SET next_at=?,expires=? WHERE id=1", (now + 13, now + 45))
        return {"updated": bool(changed)}
    if operation != "qq_finish":
        raise ValueError("invalid_operation")
    outcome = request.get("outcome")
    if outcome not in ("retry", "accepted", "unknown", "rejected"):
        raise ValueError("invalid_outcome")
    row = conn.execute("SELECT status FROM qq_delivery WHERE ticket=?", (ticket,)).fetchone()
    if not row or row[0] != ("preparing" if outcome == "retry" else "sending"):
        return {"updated": False}
    receipt, code = request.get("receipt"), request.get("code")
    if receipt is not None and (not isinstance(receipt, str) or not re.fullmatch("[A-Za-z0-9_-]{1,128}", receipt)):
        raise ValueError("invalid_receipt")
    if code is not None and (not isinstance(code, str) or not re.fullmatch("qq_[a-z_0-9]{1,80}", code)):
        raise ValueError("invalid_code")
    if outcome == "retry":
        conn.execute("DELETE FROM qq_delivery WHERE ticket=? AND status='preparing'", (ticket,))
        conn.execute("UPDATE qq_channel SET next_at=MAX(next_at,?) WHERE id=1", (now + 10,))
    else:
        conn.execute("UPDATE qq_delivery SET status=?,receipt=?,code=? WHERE ticket=? AND status='sending'", (outcome, receipt, code, ticket))
    if outcome == "rejected" and request.get("routeRevision", 0) == route[2]:
        conn.execute("UPDATE qq_channel SET paused=? WHERE id=1", (code or "qq_rejected",))
    conn.execute("UPDATE qq_channel SET ticket=NULL,expires=NULL WHERE id=1")
    return {"updated": True}


def operate(database, request):
    source = request.get("sourceId")
    if request.get("operation") not in ("qq_enable", "qq_disable", "qq_status", "qq_begin", "qq_finish", "qq_configure", "activity_overview") and (not isinstance(source, str) or not re.fullmatch("[0-9a-f]{64}", source)):
        raise ValueError("invalid_source")
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, timeout=2)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL, updated_at REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS outbox(source_id TEXT, session_id TEXT, turn_id TEXT, completed_at TEXT NOT NULL, queued_at REAL NOT NULL, PRIMARY KEY(source_id,session_id,turn_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS leases(source_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS activity_progress(source_id TEXT PRIMARY KEY,state TEXT NOT NULL)")
        if request.get("operation") in ("activity_publish", "activity_overview"):
            conn.execute("CREATE TABLE IF NOT EXISTS activity_current(source_id TEXT PRIMARY KEY,machine TEXT NOT NULL,sessions TEXT NOT NULL,unknown_count INTEGER NOT NULL,updated_at REAL NOT NULL)")
            conn.execute("BEGIN IMMEDIATE")
            now = time.time()
            conn.execute("DELETE FROM activity_current WHERE updated_at<?", (now - 90,))
            if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='activity'").fetchone():
                conn.execute("DELETE FROM activity WHERE updated_at<?", (now - 90,))
            if request["operation"] == "activity_publish":
                machine, sessions, unknown = request.get("machine"), request.get("sessions"), request.get("unknown")
                if not isinstance(machine, str) or not 1 <= len(machine) <= 256 or not isinstance(sessions, list) or len(sessions) > 128 or not isinstance(unknown, int) or not 0 <= unknown <= 128:
                    raise ValueError("invalid_activity")
                projected = []
                for item in sessions:
                    if not isinstance(item, dict) or any(not isinstance(item.get(k), str) or not 1 <= len(item[k]) <= 128 for k in ("sessionId", "turnId")) or item.get("nameState") not in ("resolved", "unset", "unavailable") or not (item.get("sessionName") is None or isinstance(item.get("sessionName"), str)) or not (item.get("startedAt") is None or isinstance(item.get("startedAt"), str) and len(item["startedAt"]) <= 64):
                        raise ValueError("invalid_activity")
                    projected.append({"sessionId": item["sessionId"], "turnId": item["turnId"], "nameState": item["nameState"],
                                      "sessionName": item["sessionName"][:256] if item["sessionName"] else None, "startedAt": item.get("startedAt")})
                conn.execute("INSERT INTO activity_current VALUES(?,?,?,?,?) ON CONFLICT(source_id) DO UPDATE SET machine=excluded.machine,sessions=excluded.sessions,unknown_count=excluded.unknown_count,updated_at=excluded.updated_at", (source, machine[:128], json.dumps(projected, ensure_ascii=True), unknown, now))
                conn.commit()
                return {"published": True}
            sources, available = [], 64
            for sid, machine, encoded, unknown, updated in conn.execute("SELECT * FROM activity_current ORDER BY updated_at DESC LIMIT 16"):
                sessions = json.loads(encoded)
                shown = sessions[:available]
                available -= len(shown)
                sources.append({"sourceId": sid, "machine": machine, "sessions": shown, "runningCount": len(sessions), "unknown": unknown, "updatedAt": updated})
            qq = qq_operation(conn, {"operation": "qq_status"})
            conn.commit()
            return {"sources": sources, "qq": qq}
        if str(request.get("operation", "")).startswith("qq_"):
            conn.execute("BEGIN IMMEDIATE")
            result = qq_operation(conn, request)
            conn.commit()
            return result
        if request.get("operation") == "load":
            row = conn.execute("SELECT revision,state FROM sources WHERE id=?", (source,)).fetchone()
            state = json.loads(row[1]) if row else None
            activity = conn.execute("SELECT state FROM activity_progress WHERE source_id=?", (source,)).fetchone()
            if state is not None and activity:
                state["activity"] = json.loads(activity[0])
            known = [{"sessionId": sid, "turnId": turn} for sid, turn in conn.execute(
                "SELECT session_id,turn_id FROM outbox WHERE source_id=? AND queued_at>=? LIMIT 200",
                (source, time.time() - 1800))]
            return {"revision": row[0] if row else 0, "state": state, "knownCompletions": known}
        if request.get("operation") in ("lease", "release"):
            owner = request.get("owner")
            if not isinstance(owner, str) or not re.fullmatch("[0-9a-f-]{36}", owner):
                raise ValueError("invalid_owner")
            conn.execute("BEGIN IMMEDIATE")
            if request["operation"] == "release":
                conn.execute("DELETE FROM leases WHERE source_id=? AND owner=?", (source, owner))
                conn.commit()
                return {"released": True}
            now = time.time()
            conn.execute("DELETE FROM leases WHERE expires<=?", (now,))
            row = conn.execute("SELECT owner FROM leases WHERE source_id=?", (source,)).fetchone()
            if row and row[0] != owner:
                conn.rollback()
                return {"acquired": False}
            conn.execute("INSERT INTO leases VALUES(?,?,?) ON CONFLICT(source_id) DO UPDATE SET owner=excluded.owner,expires=excluded.expires", (source, owner, now + 45))
            conn.commit()
            return {"acquired": True}
        if request.get("operation") != "commit":
            raise ValueError("invalid_operation")
        state, events = request.get("state"), request.get("events")
        if not isinstance(state, dict) or not isinstance(events, list) or len(events) > 100:
            raise ValueError("invalid_state")
        state = dict(state)
        activity = state.pop("activity", None)
        serialized_activity = None
        if activity is not None:
            if not isinstance(activity, dict) or len(activity) > 128:
                raise ValueError("invalid_activity_state")
            projected = {}
            for sid, progress in activity.items():
                if not isinstance(sid, str) or not 1 <= len(sid) <= 128 or not isinstance(progress, dict):
                    raise ValueError("invalid_activity_state")
                clean = {}
                for key in ("fileKey", "activeTurn", "activeStartedAt"):
                    value = progress.get(key)
                    if value is not None and (not isinstance(value, str) or len(value) > 128):
                        raise ValueError("invalid_activity_state")
                    if key in progress:
                        clean[key] = value
                for key in ("observedSize", "activityLookback", "activityCheckedAt", "activityOrder"):
                    value = progress.get(key)
                    if key in progress:
                        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 2 ** 53:
                            raise ValueError("invalid_activity_state")
                        clean[key] = value
                for key in ("activityKnown", "activityHeaderVerified", "activityBound"):
                    if key in progress:
                        if not isinstance(progress[key], bool):
                            raise ValueError("invalid_activity_state")
                        clean[key] = progress[key]
                projected[sid] = clean
            serialized_activity = json.dumps(projected, ensure_ascii=True)
            if len(serialized_activity) > 196608:
                raise ValueError("state_budget_exceeded")
        serialized = json.dumps(state, ensure_ascii=True)
        if len(serialized) > 196608:
            raise ValueError("state_budget_exceeded")
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT revision FROM sources WHERE id=?", (source,)).fetchone()
        revision = row[0] if row else 0
        if request.get("revision") != revision:
            conn.rollback()
            return {"committed": False, "added": []}
        now = time.time()
        conn.execute("DELETE FROM outbox WHERE queued_at<?", (now - 1800,))
        conn.execute("DELETE FROM sources WHERE updated_at<?", (now - 14 * 86400,))
        if not row and conn.execute("SELECT COUNT(*) FROM sources").fetchone()[0] >= 64:
            raise ValueError("source_capacity_exceeded")
        added = []
        for event in events:
            if not isinstance(event, dict) or any(not isinstance(event.get(k), str) or not 1 <= len(event[k]) <= 128 for k in ("sessionId", "turnId", "completedAt")):
                raise ValueError("invalid_event")
            # Deliberately project IDs/time only. Labels, prompts and replies
            # cannot enter the durable outbox through this interface.
            cursor = conn.execute("INSERT OR IGNORE INTO outbox VALUES(?,?,?,?,?)", (source, event["sessionId"], event["turnId"], event["completedAt"], now))
            if cursor.rowcount:
                added.append({"sessionId": event["sessionId"], "turnId": event["turnId"]})
        if conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0] > 200:
            raise ValueError("outbox_capacity_exceeded")
        conn.execute("INSERT INTO sources VALUES(?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,state=excluded.state,updated_at=excluded.updated_at", (source, revision + 1, serialized, now))
        if serialized_activity is not None:
            conn.execute("INSERT INTO activity_progress VALUES(?,?) ON CONFLICT(source_id) DO UPDATE SET state=excluded.state", (source, serialized_activity))
        conn.execute("DELETE FROM activity_progress WHERE NOT EXISTS(SELECT 1 FROM sources s WHERE s.id=activity_progress.source_id)")
        conn.commit()
        return {"committed": True, "added": added}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True)
    args = parser.parse_args()
    try:
        data = sys.stdin.buffer.read(262145)
        if len(data) > 262144:
            raise ValueError("request_budget_exceeded")
        request = json.loads(data)
        if not isinstance(request, dict):
            raise ValueError("invalid_request")
        print(json.dumps(operate(args.db, request)))
    except (OSError, ValueError, TypeError, sqlite3.Error):
        print('{"error":"state_operation_failed"}')
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
