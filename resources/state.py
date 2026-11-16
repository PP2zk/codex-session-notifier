"""Local-only SQLite cursors and atomic outbox. Never invoked over SSH."""

import argparse
import calendar
import ctypes
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
from datetime import datetime


def process_started_at(pid):
    """Read a local process birth time without installing packages."""
    if not isinstance(pid, int) or isinstance(pid, bool) or not 0 < pid < 2 ** 31:
        return None
    try:
        if os.name == "nt":
            from ctypes import wintypes as w
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
            kernel.OpenProcess.restype = w.HANDLE
            kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
            kernel.GetProcessTimes.restype = w.BOOL
            kernel.CloseHandle.argtypes = [w.HANDLE]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if not handle:
                return None
            try:
                times = [w.FILETIME() for _ in range(4)]
                if not kernel.GetProcessTimes(handle, *[ctypes.byref(value) for value in times]):
                    return None
                ticks = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
                started = ticks / 10000000 - 11644473600
            finally:
                kernel.CloseHandle(handle)
        elif Path("/proc/self/stat").is_file():
            with open("/proc/%d/stat" % pid) as stream:
                stat = stream.read(4096)
            ticks = int(stat[stat.rfind(")") + 2:].split()[19])
            with open("/proc/stat") as stream:
                boot = next(int(line.split()[1]) for line in stream if line.startswith("btime "))
            started = boot + ticks / os.sysconf("SC_CLK_TCK")
        else:
            result = subprocess.run(["ps", "-p", str(pid), "-o", "lstart="], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, universal_newlines=True,
                                    timeout=2, check=True, env={**os.environ, "LC_ALL": "C", "TZ": "UTC"})
            started = calendar.timegm(time.strptime(result.stdout.strip(), "%a %b %d %H:%M:%S %Y"))
        # Native completed_at values can have only second precision.
        return int(started) if 0 < started <= time.time() + 1 else None
    except (OSError, ValueError, IndexError, StopIteration, subprocess.SubprocessError):
        return None


def source_cutoff(conn, source):
    row = conn.execute("SELECT state FROM sources WHERE id=?", (source,)).fetchone()
    value = json.loads(row[0]).get("startedAt", 0) if row else 0
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and value > 0 else 0


def completion_timestamp(completed):
    try:
        value = datetime.fromisoformat(completed.replace("Z", "+00:00"))
        return value.timestamp() if value.tzinfo else 0
    except (ValueError, OverflowError, AttributeError):
        return 0


def project_diagnostic(item):
    if not isinstance(item, dict) or item.get("kind") not in ("activity", "completion", "scan"):
        raise ValueError("invalid_diagnostics")
    clean = {"kind": item["kind"]}
    for key in ("sessionId", "sessionName", "turnId", "completedAt", "state", "reason", "bindingReason", "operation"):
        value = item.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > 128):
            raise ValueError("invalid_diagnostics")
        if key in item:
            clean[key] = value
    if not isinstance(clean.get("sessionId"), str) or not all(isinstance(clean.get(k), str) and re.fullmatch("[a-z_]+", clean[k]) for k in ("state", "reason")):
        raise ValueError("invalid_diagnostics")
    for key in ("fileBytes", "observedBytes", "lookbackBytes", "offset", "bytesRead", "runningCount", "unknownCount", "position", "osError", "processId", "cutoff"):
        if key in item:
            value = item[key]
            if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 2 ** 53:
                raise ValueError("invalid_diagnostics")
            clean[key] = value
    if len(json.dumps(clean, ensure_ascii=True)) > 1536 and isinstance(clean.get("sessionName"), str):
        clean["sessionName"] = clean["sessionName"][:32]
    if len(json.dumps(clean, ensure_ascii=True)) > 1536:
        raise ValueError("invalid_diagnostics")
    return clean


def project_diagnostics(items):
    projected = []
    for item in items[:256] if isinstance(items, list) else []:
        try:
            projected.append(project_diagnostic(item))
        except (ValueError, TypeError):
            # Optional telemetry cannot reject an authoritative cursor commit.
            continue
    return projected


def record_diagnostics(conn, source, items, now):
    added = []
    conn.execute("DELETE FROM diagnostics WHERE observed_at<?", (now - 7 * 86400,))
    for item in items:
        last = conn.execute("SELECT observed_at,details FROM diagnostics WHERE source_id=? AND kind=? AND session_id=? ORDER BY id DESC LIMIT 1",
                            (source, item["kind"], item["sessionId"])).fetchone()
        fingerprint = lambda value: tuple(value.get(k) for k in ("state", "reason", "turnId", "bindingReason", "osError", "cutoff"))
        heartbeat = 60 if item["kind"] == "scan" else 300
        if last and fingerprint(json.loads(last[1])) == fingerprint(item) and (item["kind"] == "completion" or now - last[0] < heartbeat):
            continue
        conn.execute("INSERT INTO diagnostics(observed_at,source_id,kind,session_id,details) VALUES(?,?,?,?,?)",
                     (now, source, item["kind"], item["sessionId"], json.dumps(item, ensure_ascii=True)))
        added.append({"observedAt": now, "sourceId": source, **item})
    conn.execute("DELETE FROM diagnostics WHERE id NOT IN (SELECT id FROM diagnostics ORDER BY id DESC LIMIT 4096)")
    return added


def monitor_presence(conn, source, request):
    owner = request.get("owner")
    if not isinstance(owner, str) or not re.fullmatch("[0-9a-f-]{36}", owner):
        raise ValueError("invalid_owner")
    conn.execute("CREATE TABLE IF NOT EXISTS monitor_presence(source_id TEXT,owner TEXT,pid INTEGER NOT NULL,process_start INTEGER NOT NULL,cutoff REAL NOT NULL,PRIMARY KEY(source_id,owner))")
    conn.execute("BEGIN IMMEDIATE")
    if request["operation"] == "monitor_leave":
        conn.execute("DELETE FROM monitor_presence WHERE source_id=? AND owner=?", (source, owner))
        conn.commit()
        return {"released": True}
    now, start, pid = time.time(), request.get("startedAt"), request.get("pid")
    if not isinstance(start, (int, float)) or isinstance(start, bool) or not 0 < start <= now + 1:
        raise ValueError("invalid_startup_boundary")
    birth = process_started_at(pid)
    if birth is None:
        raise ValueError("monitor_process_unverified")
    births = {pid: birth}
    for sid, token, other_pid, other_birth in conn.execute("SELECT source_id,owner,pid,process_start FROM monitor_presence").fetchall():
        if other_pid not in births:
            births[other_pid] = process_started_at(other_pid)
        if births[other_pid] != other_birth:
            conn.execute("DELETE FROM monitor_presence WHERE source_id=? AND owner=?", (sid, token))
    peers = conn.execute("SELECT cutoff FROM monitor_presence WHERE source_id=?", (source,)).fetchall()
    saved = conn.execute("SELECT state FROM sources WHERE id=?", (source,)).fetchone()
    state = json.loads(saved[0]) if saved else None
    previous = source_cutoff(conn, source)
    cutoff = previous if peers and previous else min(row[0] for row in peers) if peers else max(previous, start)
    if not conn.execute("SELECT 1 FROM monitor_presence WHERE source_id=? AND owner=?", (source, owner)).fetchone() and conn.execute("SELECT COUNT(*) FROM monitor_presence").fetchone()[0] >= 64:
        raise ValueError("monitor_presence_capacity_reached")
    conn.execute("INSERT INTO monitor_presence VALUES(?,?,?,?,?) ON CONFLICT(source_id,owner) DO UPDATE SET pid=excluded.pid,process_start=excluded.process_start,cutoff=excluded.cutoff",
                 (source, owner, pid, birth, cutoff))
    if state is not None and state.get("startedAt") != cutoff:
        state["startedAt"] = cutoff
        # Invalidate any scan loaded before the new monitoring interval.
        conn.execute("UPDATE sources SET state=?,revision=revision+1 WHERE id=?", (json.dumps(state, ensure_ascii=True), source))
    diagnostics = record_diagnostics(conn, source, [{"kind": "scan", "sessionId": "", "state": "monitoring",
        "reason": "monitor_joined" if peers else "monitor_started", "processId": pid, "cutoff": cutoff}], now)
    conn.commit()
    return {"startedAt": cutoff, "diagnostics": diagnostics}


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
        cutoff = max(channel[0], now - 1800, source_cutoff(conn, request["sourceId"]))
        for sid, turn, completed in rows:
            if not cutoff <= completion_timestamp(completed) <= now + 60:
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
        pending = conn.execute("SELECT o.source_id,o.completed_at FROM qq_delivery d JOIN outbox o USING(source_id,session_id,turn_id) WHERE d.ticket=? AND d.status='preparing'", (ticket,)).fetchone()
        if not pending or completion_timestamp(pending[1]) < source_cutoff(conn, pending[0]):
            return {"updated": False}
        changed = conn.execute("UPDATE qq_delivery SET status='sending' WHERE ticket=? AND status='preparing'", (ticket,)).rowcount
        if changed:
            # Respect the provider's five requests/minute cap across windows.
            conn.execute("UPDATE qq_channel SET next_at=?,expires=? WHERE id=1", (now + 13, now + 45))
        return {"updated": bool(changed)}
    if operation != "qq_finish":
        raise ValueError("invalid_operation")
    outcome = request.get("outcome")
    if outcome not in ("retry", "accepted", "unknown", "rejected", "suppressed"):
        raise ValueError("invalid_outcome")
    row = conn.execute("SELECT status FROM qq_delivery WHERE ticket=?", (ticket,)).fetchone()
    if not row or row[0] != ("preparing" if outcome in ("retry", "suppressed") else "sending"):
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
        conn.execute("UPDATE qq_delivery SET status=?,receipt=?,code=? WHERE ticket=?", (outcome, receipt, code, ticket))
    if outcome == "rejected" and request.get("routeRevision", 0) == route[2]:
        conn.execute("UPDATE qq_channel SET paused=? WHERE id=1", (code or "qq_rejected",))
    conn.execute("UPDATE qq_channel SET ticket=NULL,expires=NULL WHERE id=1")
    return {"updated": True}


def operate(database, request):
    if request.get("operation") == "process_start":
        return {"startedAt": process_started_at(request.get("pid"))}
    source = request.get("sourceId")
    if request.get("operation") not in ("qq_enable", "qq_disable", "qq_status", "qq_begin", "qq_finish", "qq_configure", "activity_overview", "diagnostics") and (not isinstance(source, str) or not re.fullmatch("[0-9a-f]{64}", source)):
        raise ValueError("invalid_source")
    Path(database).parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(database, timeout=2)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, state TEXT NOT NULL, updated_at REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS outbox(source_id TEXT, session_id TEXT, turn_id TEXT, completed_at TEXT NOT NULL, queued_at REAL NOT NULL, PRIMARY KEY(source_id,session_id,turn_id))")
        conn.execute("CREATE TABLE IF NOT EXISTS leases(source_id TEXT PRIMARY KEY, owner TEXT NOT NULL, expires REAL NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS activity_progress(source_id TEXT PRIMARY KEY,state TEXT NOT NULL)")
        conn.execute("CREATE TABLE IF NOT EXISTS diagnostics(id INTEGER PRIMARY KEY,observed_at REAL NOT NULL,source_id TEXT NOT NULL,kind TEXT NOT NULL,session_id TEXT NOT NULL,details TEXT NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS diagnostics_identity ON diagnostics(source_id,kind,session_id,id)")
        if request.get("operation") in ("monitor_join", "monitor_leave"):
            return monitor_presence(conn, source, request)
        if request.get("operation") == "diagnostics":
            rows = conn.execute("SELECT observed_at,source_id,details FROM diagnostics WHERE observed_at>=? ORDER BY id DESC LIMIT 100", (time.time() - 7 * 86400,)).fetchall()
            return {"entries": [{"observedAt": at, "sourceId": source, **json.loads(details)} for at, source, details in reversed(rows)]}
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
        diagnostics = project_diagnostics(request.get("diagnostics", []))
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
                for key in ("fileKey", "activeTurn", "activeStartedAt", "activityAnchor"):
                    value = progress.get(key)
                    if value is not None and (not isinstance(value, str) or len(value) > 128):
                        raise ValueError("invalid_activity_state")
                    if key in progress:
                        clean[key] = value
                for key in ("observedSize", "activityLookback", "activityCheckedAt", "activityOrder", "activityOffset"):
                    value = progress.get(key)
                    if key in progress:
                        if not isinstance(value, (int, float)) or isinstance(value, bool) or not 0 <= value <= 2 ** 53:
                            raise ValueError("invalid_activity_state")
                        clean[key] = value
                for key in ("activityKnown", "activityHeaderVerified", "activityBound", "activityInitChecked", "activityInitializationOnly"):
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
        logged = record_diagnostics(conn, source, diagnostics, now)
        conn.commit()
        response = {"committed": True, "added": added, "diagnostics": []}
        remaining = 240000 - len(json.dumps(response, ensure_ascii=True))
        for item in logged:
            size = len(json.dumps(item, ensure_ascii=True)) + 1
            if size > remaining:
                continue
            response["diagnostics"].append(item)
            remaining -= size
        return response


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
