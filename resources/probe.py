"""Bounded Codex reader and notifier-owned delivery claims; no installs.

The same source can execute locally or through SSH without creating a remote
script. Never return prompt text, assistant text, instructions or raw records.
"""

import argparse
import base64
import collections
import hashlib
from contextlib import closing
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import sqlite3
import sys
import time

MAX_TAIL = 262144
MAX_HEADER = 131072
MAX_OUTPUT = 524288
MAX_SCAN_BYTES = 1048576
MAX_CATCHUP_BYTES = 8388608
# App Server is the common backend for IDE and Desktop clients. Terminal CLI
# and exec sessions, unknown sources and structured subagent sources stay out.
CLIENT_SOURCES = ("vscode", "appServer")
CLIENT_SOURCE_SQL = "source IN (" + ",".join("?" for _ in CLIENT_SOURCES) + ")"


def comparison_path(path):
    value = str(path.resolve())
    if os.name == "nt":
        if value.startswith("\\\\?\\UNC\\"):
            value = "\\\\" + value[8:]
        elif value.startswith("\\\\?\\"):
            value = value[4:]
    return Path(value)


def completion_time(payload, item):
    timestamp = payload.get("completed_at")
    # Observed Codex 0.160.0 writes completed_at as Unix seconds.
    if isinstance(timestamp, (int, float)) and not isinstance(timestamp, bool):
        try:
            return datetime.fromtimestamp(timestamp, timezone.utc).isoformat().replace("+00:00", "Z")
        except (ValueError, OverflowError, OSError):
            return None
    if not isinstance(timestamp, str):
        timestamp = item.get("timestamp")
    if isinstance(timestamp, str):
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if parsed.tzinfo is not None:
                return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            pass
    return None


def valid_session_header(raw, session_id, source=None):
    """Identify the record format by its data, never by a CLI version string."""
    if len(raw) > MAX_HEADER or not raw.endswith(b"\n"):
        return False
    try:
        item = json.loads(raw)
        payload = item.get("payload") if isinstance(item, dict) else None
        return (item.get("type") == "session_meta" and isinstance(payload, dict)
                and payload.get("id") == session_id and payload.get("source") in CLIENT_SOURCES
                and (source is None or payload.get("source") == source))
    except (ValueError, TypeError, AttributeError):
        return False


def header_metadata(path):
    with path.open("rb") as stream:
        raw = stream.readline(MAX_HEADER + 1)
    if len(raw) > MAX_HEADER or not raw.endswith(b"\n"):
        return None
    item = json.loads(raw)
    if not isinstance(item, dict):
        return None
    payload = item.get("payload")
    if item.get("type") != "session_meta" or not isinstance(payload, dict):
        return None
    return {key: payload.get(key) for key in
            ("id", "source", "originator", "cli_version", "history_mode")}


def completion_summary(path):
    """Read a limited tail. Ignore partial final lines and unparsed records."""
    size = path.stat().st_size
    offset = max(0, size - MAX_TAIL)
    with path.open("rb") as stream:
        stream.seek(offset)
        raw = stream.read(MAX_TAIL)
    if offset:
        boundary = raw.find(b"\n")
        raw = raw[boundary + 1:] if boundary >= 0 else b""
    boundary = raw.rfind(b"\n")
    raw = raw[:boundary + 1] if boundary >= 0 else b""
    parsed = parse_records(raw)
    return {"bytesRead": min(MAX_TAIL, size), "explicitEvents": parsed["explicitEvents"],
            "completions": parsed["completions"][-10:]}


def parse_records(raw, state=None, limit=100):
    state = state or {}
    kinds = collections.Counter()
    warnings = set()
    complete = {}
    blocked = list(state.get("blocked", []))[-8:]
    active_turn = state.get("activeTurn")
    active_started = state.get("activeStartedAt")
    activity_known = bool(state.get("activityKnown"))
    unbound_abort = bool(state.get("unboundAbort"))
    consumed = 0
    records = 0
    for line in raw.splitlines(keepends=True):
        if not line.endswith(b"\n") or records >= limit:
            break
        records += 1
        consumed += len(line)
        try:
            item = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        if not isinstance(item, dict):
            continue
        payload = item.get("payload")
        if item.get("type") != "event_msg" or not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind in ("task_started", "task_complete", "turn_aborted"):
            kinds[kind] += 1
        turn = payload.get("turn_id")
        valid_turn = isinstance(turn, str) and 0 < len(turn) <= 128
        if kind in ("task_started", "task_complete") and not valid_turn:
            warnings.add("lifecycle_identity_unavailable")
            active_turn, active_started, activity_known = None, None, False
            continue
        if kind == "task_started":
            active_turn = turn if valid_turn else None
            active_started = completion_time({"completed_at": payload.get("started_at")}, item)
            activity_known = valid_turn
            unbound_abort = False
        elif kind == "turn_aborted":
            aborted = turn if valid_turn else active_turn
            if aborted:
                if aborted not in blocked:
                    blocked.append(aborted)
                complete.pop(aborted, None)
            else:
                unbound_abort = True
            if not valid_turn or turn == active_turn:
                active_turn = None
                active_started = None
            activity_known = True
        elif kind == "task_complete" and valid_turn:
            if active_turn is None or turn == active_turn:
                active_turn = None
                active_started = None
            activity_known = True
            # Codex 0.160.0 TurnCompleteEvent also represents terminal errors.
            # Never treat an errored or contradicted terminal event as success.
            if payload.get("error") is not None or payload.get("status", "completed") != "completed":
                if payload.get("status", "completed") not in ("completed", "failed", "interrupted", "cancelled"):
                    warnings.add("completion_status_unrecognized")
                if turn not in blocked:
                    blocked.append(turn)
                complete.pop(turn, None)
                continue
            if turn in blocked or unbound_abort:
                continue
            # Strict projection: last_agent_message must never escape the probe.
            complete[turn] = {"turnId": turn,
                              "completedAt": completion_time(payload, item)}
    return {"consumed": consumed, "records": records, "explicitEvents": dict(kinds),
            "warnings": sorted(warnings),
            "completions": list(complete.values()),
            "state": {"activeTurn": active_turn, "activeStartedAt": active_started, "activityKnown": activity_known,
                      "blocked": blocked[-8:], "unboundAbort": unbound_abort}}


def session_label(row):
    """Prefer an explicit name, then the native display title. Never read prompts."""
    for key in ("name", "title"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), key
    return None, "unset"


def rollout_root(home, row):
    # Only the two native stores are allowed, chosen from the indexed state.
    # Resolving both sides also supports a symlinked CODEX_HOME transparently.
    return comparison_path(home / ("archived_sessions" if row.get("archived") else "sessions"))


def probe(request):
    requested_home = request.get("home")
    if requested_home is not None and (not isinstance(requested_home, str) or len(requested_home) > 4096):
        raise ValueError("invalid_home")
    home = Path(requested_home or os.environ.get("CODEX_HOME") or str(Path.home() / ".codex")).expanduser().resolve()
    limit = request.get("limit", 3)
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 5:
        raise ValueError("invalid_limit")
    session_name = request.get("sessionName")
    if session_name is not None and (not isinstance(session_name, str) or not 1 <= len(session_name) <= 512):
        raise ValueError("invalid_name_filter")
    database = home / "state_5.sqlite"
    result = {"schemaVersion": 1, "platform": platform.system(),
              "hostname": platform.node(), "pythonVersion": platform.python_version(),
              "codexHome": str(home), "databaseExists": database.is_file(),
              "samples": [], "warnings": []}
    if not database.is_file():
        result["warnings"].append("database_missing")
        return result
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.execute("PRAGMA query_only=ON")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
        required = {"id", "source", "rollout_path", "updated_at"}
        if not required.issubset(columns):
            result["warnings"].append("unsupported_database_schema")
            return result
        selected = ["id", "source", "rollout_path"]
        selected += [key for key in ("name", "title", "cli_version", "archived") if key in columns]
        label_columns = [key for key in ("name", "title") if key in columns]
        if session_name is not None and not label_columns:
            result["warnings"].append("name_filter_unsupported")
            return result
        query = "SELECT " + ",".join(selected) + " FROM threads WHERE " + CLIENT_SOURCE_SQL
        params = list(CLIENT_SOURCES)
        if session_name is not None:
            labels = ["NULLIF(TRIM(" + key + "),'')" for key in label_columns]
            label_sql = "COALESCE(" + ",".join(labels) + ")" if len(labels) > 1 else labels[0]
            # SQL NOCASE folds ASCII only; keep Unicode otherwise exact. The
            # label itself keeps its original case. This is not substring search.
            query += " AND " + label_sql + " = ? COLLATE NOCASE"
            params.append(session_name.strip())
        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        rows = [dict(zip(selected, row)) for row in conn.execute(query, params)]
    for row in rows:
        session_id = row["id"]
        if not isinstance(session_id, str) or len(session_id) > 128:
            result["warnings"].append("invalid_session_identity")
            continue
        path = Path(row["rollout_path"]).resolve()
        # A synchronized or malicious database row cannot direct reads elsewhere.
        try:
            comparison_path(path).relative_to(rollout_root(home, row))
        except ValueError:
            result["warnings"].append("rollout_outside_sessions")
            continue
        label, label_source = session_label(row)
        sample = {"sessionId": session_id, "source": row["source"],
                  "archived": bool(row.get("archived")),
                  "hasName": label is not None, "nameSource": label_source,
                  "cliVersion": row.get("cli_version"), "fileExists": path.is_file()}
        if path.is_file():
            try:
                meta = header_metadata(path)
                if not meta or meta.get("id") != session_id or meta.get("source") != row["source"]:
                    result["warnings"].append("session_metadata_mismatch")
                    continue
                sample["metadata"] = meta
                sample["fileBytes"] = path.stat().st_size
                sample.update(completion_summary(path))
            except (OSError, ValueError, TypeError):
                result["warnings"].append("unreadable_session_metadata")
                continue
        result["samples"].append(sample)
    return result


def source_identity(home):
    """Hash an OS machine identity and canonical home, never expose the raw ID."""
    native = None
    if os.name == "nt":
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            native = winreg.QueryValueEx(key, "MachineGuid")[0]
    elif platform.system() == "Linux":
        with Path("/etc/machine-id").open("r", encoding="ascii") as stream:
            native = stream.read(128).strip()
    if not isinstance(native, str) or not native:
        raise ValueError("machine_identity_unavailable")
    canonical = str(comparison_path(home))
    if os.name == "nt":
        canonical = canonical.casefold()
    return hashlib.sha256((platform.system() + "\0" + native + "\0" + canonical).encode()).hexdigest()


def windows_file_users(path, diagnostic=None):
    """Query resource users for source attribution; never shutdown processes."""
    import ctypes as c
    from ctypes import wintypes as w

    class Unique(c.Structure):
        _fields_ = [("pid", w.DWORD), ("started", w.FILETIME)]

    class Info(c.Structure):
        _fields_ = [("process", Unique), ("app", w.WCHAR * 256), ("service", w.WCHAR * 64),
                    ("kind", w.DWORD), ("status", w.DWORD), ("session", w.DWORD), ("restartable", w.BOOL)]

    rm = c.WinDLL("Rstrtmgr.dll")
    kernel = c.WinDLL("kernel32", use_last_error=True)
    rm.RmStartSession.argtypes = [c.POINTER(w.DWORD), w.DWORD, w.LPWSTR]
    rm.RmRegisterResources.argtypes = [w.DWORD, w.UINT, c.POINTER(w.LPCWSTR), w.UINT, c.POINTER(Unique), w.UINT, c.POINTER(w.LPCWSTR)]
    rm.RmGetList.argtypes = [w.DWORD, c.POINTER(w.UINT), c.POINTER(w.UINT), c.POINTER(Info), c.POINTER(w.DWORD)]
    rm.RmEndSession.argtypes = [w.DWORD]
    for name in ("RmStartSession", "RmRegisterResources", "RmGetList", "RmEndSession"):
        getattr(rm, name).restype = w.DWORD
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, c.POINTER(w.DWORD)]
    kernel.QueryFullProcessImageNameW.restype = w.BOOL
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle, key = w.DWORD(), c.create_unicode_buffer(33)
    def failed(reason, code):
        if diagnostic is not None:
            diagnostic.update(reason=reason, osError=code)
        return False

    code = rm.RmStartSession(c.byref(handle), 0, key)
    if code:
        return failed("windows_session_failed", code)
    try:
        files = (w.LPCWSTR * 1)(str(comparison_path(path)))
        code = rm.RmRegisterResources(handle, 1, files, 0, None, 0, None)
        if code:
            return failed("windows_register_failed", code)
        needed, count, reasons, entries = w.UINT(), w.UINT(64), w.DWORD(), (Info * 64)()
        code = rm.RmGetList(handle, c.byref(needed), c.byref(count), entries, c.byref(reasons))
        if code:
            return failed("windows_list_failed", code)
        for info in entries[:min(count.value, 64)]:
            process = kernel.OpenProcess(0x1000, False, info.process.pid)
            if not process:
                if diagnostic is not None:
                    diagnostic.update(reason="process_image_failed", osError=c.get_last_error(),
                                      operation="OpenProcess", processId=info.process.pid)
                continue
            try:
                image, length = c.create_unicode_buffer(4096), w.DWORD(4096)
                if kernel.QueryFullProcessImageNameW(process, 0, image, c.byref(length)):
                    value = image.value.replace("\\", "/").lower()
                    # Desktop and configured IDE backends need not live inside
                    # a VS Code extension. RM still ties this image to this file.
                    if value.endswith("/codex.exe"):
                        return True
                elif diagnostic is not None:
                    diagnostic.update(reason="process_image_failed", osError=c.get_last_error(),
                                      operation="QueryFullProcessImageNameW", processId=info.process.pid)
            finally:
                kernel.CloseHandle(process)
        return False
    finally:
        rm.RmEndSession(handle)


def execution_binding(path, diagnostic=None):
    """Fail closed if a synchronized file cannot be bound to an existing backend."""
    if diagnostic is not None:
        diagnostic["reason"] = "backend_not_observed"
    try:
        if os.name == "nt":
            return windows_file_users(path, diagnostic)
        if platform.system() != "Linux":
            if diagnostic is not None:
                diagnostic["reason"] = "unsupported_platform"
            return False
        wanted = str(path.resolve())
        candidates = 0
        for index, proc in enumerate(Path("/proc").iterdir()):
            if index >= 8192:
                break
            if not proc.name.isdigit():
                continue
            try:
                # Never inspect another shared-host user's process contents.
                if proc.stat().st_uid != os.getuid() or (proc / "comm").read_text().strip() != "codex":
                    continue
                with (proc / "cmdline").open("rb") as stream:
                    command = stream.read(4096).split(b"\0")
                image = os.readlink(proc / "exe")
                if b"app-server" not in command or Path(image).name != "codex":
                    continue
                candidates += 1
                if candidates > 8:
                    break
                for index, fd in enumerate((proc / "fd").iterdir()):
                    if index >= 256:
                        break
                    try:
                        if os.readlink(fd) != wanted:
                            continue
                        with (proc / "fdinfo" / fd.name).open() as stream:
                            info = stream.read(4096)
                        flags = next((line.split()[1] for line in info.splitlines() if line.startswith("flags:")), None)
                        if flags and int(flags, 8) & os.O_ACCMODE in (os.O_WRONLY, os.O_RDWR):
                            return True
                    except (OSError, ValueError, IndexError):
                        continue
            except (OSError, ValueError):
                continue
    except OSError as error:
        if diagnostic is not None:
            diagnostic["reason"] = "process_query_failed"
            code = getattr(error, "winerror", None) or error.errno
            if code is not None:
                diagnostic["osError"] = code
    return False


def checkpoint_bytes(path, offset):
    with path.open("rb") as stream:
        stream.seek(max(0, offset - 64))
        return stream.read(min(64, offset))


def collect_activity(rows, home, result, previous, budget, record_budget):
    """Activity advances before completion replay, with its own persisted offsets."""
    running, unknown, binding_checks = [], 0, 0
    diagnostics = []
    backlog = False
    progress = {}
    candidate_budget = 16
    session_root = comparison_path(home / "sessions")

    def save_activity_anchor(path, cursor, offset):
        nonlocal budget
        if offset is not None and budget >= 64:
            anchor = checkpoint_bytes(path, offset)
            budget -= len(anchor)
            result["bytesRead"] += len(anchor)
            cursor.update(activityOffset=offset, activityAnchor=hashlib.sha256(anchor).hexdigest())

    def window(path, end, file_size, row, since=None):
        nonlocal budget, record_budget, candidate_budget
        cap = min(MAX_TAIL, max(0, budget - (64 if end == file_size else 0)), end)
        if cap <= 0 or record_budget <= 0 or candidate_budget <= 0:
            return None, end, False, None
        start = end - cap
        if since is not None:
            start = max(start, since)
            cap = end - start
        with path.open("rb") as stream:
            stream.seek(start)
            raw = stream.read(cap)
        budget -= len(raw)
        result["bytesRead"] += len(raw)
        read_offset = start + raw.rfind(b"\n") + 1 if b"\n" in raw else None
        if start and start != since:
            boundary = raw.find(b"\n")
            if 0 <= boundary < len(raw) - 1:
                start += boundary + 1
                raw = raw[boundary + 1:]
            else:
                raw = b""
        for line in reversed(raw.splitlines(keepends=True)):
            if not line.endswith(b"\n") or not any(marker in line for marker in (b'"task_started"', b'"task_complete"', b'"turn_aborted"')):
                continue
            record_budget -= 1
            candidate_budget -= 1
            try:
                item = json.loads(line)
                payload = item.get("payload", {})
                if item.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") in ("task_started", "task_complete", "turn_aborted"):
                    parsed = parse_records(line)
                    result["warnings"].extend(parsed["warnings"])
                    # A malformed latest lifecycle record cannot expose an
                    # older task_started as if it still described this turn.
                    return {**parsed["state"], "activityInitializationOnly": False}, start if parsed["state"]["activityKnown"] else 0, False, read_offset
            except (ValueError, TypeError, AttributeError):
                pass
            if record_budget <= 0 or candidate_budget <= 0:
                # Keep the unexamined older portion for a later scan.
                return None, end - (len(raw) - raw.find(line)), False, read_offset
        complete_file = start == 0 and end == file_size and len(raw) == file_size
        init_checked = complete_file and not raw.endswith(b"\n")
        if complete_file and raw.endswith(b"\n"):
            # A complete small file containing only initialization has never
            # begun a turn. Unknown or malformed execution records stay unknown.
            initial_lines = raw.splitlines(keepends=True)
            if 0 < len(initial_lines) <= min(record_budget, candidate_budget):
                init_checked = True
                initialization_only = True
                for line in initial_lines:
                    record_budget -= 1
                    candidate_budget -= 1
                    try:
                        item = json.loads(line)
                        payload = item.get("payload", {})
                        if not (item.get("type") == "session_meta" and valid_session_header(line, row["id"], row["source"]) or
                                item.get("type") == "event_msg" and isinstance(payload, dict) and
                                payload.get("type") == "thread_settings_applied"):
                            initialization_only = False
                            break
                    except (ValueError, TypeError, AttributeError):
                        initialization_only = False
                        break
                if initialization_only:
                    return {"activeTurn": None, "activeStartedAt": None, "activityKnown": True,
                            "activityInitializationOnly": True}, 0, True, read_offset
        return None, start, init_checked, read_offset

    sequence = max((p.get("activityOrder", 0) for p in previous.values()), default=0)
    order = sorted(rows, key=lambda r: (previous.get(r["id"], {}).get("activityOrder", 0), -r.get("updated_at", 0)))
    for row in order:
        sid = row["id"]
        cursor = None
        stat = None
        state, reason = "unknown", "file_unavailable"
        binding_diagnostic = {}
        before_budget, before_bindings = budget, binding_checks
        try:
            path = Path(row["rollout_path"]).resolve(strict=True)
            comparison_path(path).relative_to(session_root)
            stat = path.stat()
            key = hashlib.sha256((str(path) + ":" + str(stat.st_dev) + ":" + str(stat.st_ino)).encode()).hexdigest()
            cursor = dict(previous.get(sid, {}))
            new_file = cursor.get("fileKey") != key or cursor.get("observedSize", 0) > stat.st_size
            if new_file:
                cursor = {"fileKey": key, "observedSize": 0, "activityLookback": stat.st_size, "activityKnown": False}
            progress[sid] = cursor
            if not cursor.get("activityHeaderVerified") or cursor.get("activityHeaderSource") != row["source"]:
                if budget < MAX_HEADER + 1:
                    reason = "read_budget_exhausted"
                    backlog = True
                    unknown += 1
                    continue
                with path.open("rb") as stream:
                    header = stream.readline(MAX_HEADER + 1)
                budget -= len(header)
                result["bytesRead"] += len(header)
                if not valid_session_header(header, sid, row["source"]):
                    reason = "metadata_mismatch"
                    result["warnings"].append("session_metadata_mismatch")
                    unknown += 1
                    continue
                cursor["activityHeaderVerified"] = True
                cursor["activityHeaderSource"] = row["source"]
            delta = stat.st_size - cursor.get("observedSize", 0)
            since = None
            offset = cursor.get("activityOffset")
            if cursor.get("activityKnown") and isinstance(offset, int) and 0 <= offset <= stat.st_size and isinstance(cursor.get("activityAnchor"), str):
                if budget < 128:
                    reason = "read_budget_exhausted"
                    unknown += 1
                    continue
                anchor = checkpoint_bytes(path, offset)
                budget -= len(anchor)
                result["bytesRead"] += len(anchor)
                if hashlib.sha256(anchor).hexdigest() == cursor["activityAnchor"]:
                    since = offset
                else:
                    cursor.update(activityKnown=False, activityInitializationOnly=False,
                                  activityLookback=stat.st_size)
            if delta > 0:
                cursor["activityInitChecked"] = False
                if cursor.get("activityInitializationOnly"):
                    # Initialization establishes idle only for those exact
                    # bytes. Any append must supply fresh lifecycle evidence.
                    cursor.update(activityKnown=False, activityInitializationOnly=False,
                                  activityLookback=stat.st_size)
            check_initialization = (not cursor.get("activityKnown") and not cursor.get("activityLookback", 0)
                                    and not cursor.get("activityInitChecked") and stat.st_size <= MAX_TAIL)
            if (delta > 0 or check_initialization) and budget > 0 and record_budget > 0 and candidate_budget > 0:
                parsed, next_end, init_checked, read_offset = window(path, stat.st_size, stat.st_size, row, since)
                cursor["observedSize"] = stat.st_size
                save_activity_anchor(path, cursor, read_offset)
                if init_checked:
                    cursor["activityInitChecked"] = True
                if parsed:
                    cursor.update({k: parsed[k] for k in ("activeTurn", "activeStartedAt", "activityKnown", "activityInitializationOnly")})
                    cursor["activityLookback"] = next_end
                elif new_file or delta > MAX_TAIL or since is not None and next_end > since:
                    cursor["activityKnown"] = False
                    cursor["activityLookback"] = next_end
            while not cursor.get("activityKnown") and cursor.get("activityLookback", 0) > 0 and budget > 0 and candidate_budget > 0:
                end = cursor["activityLookback"]
                parsed, next_end, init_checked, read_offset = window(path, end, stat.st_size, row)
                if end == stat.st_size:
                    save_activity_anchor(path, cursor, read_offset)
                if init_checked:
                    cursor["activityInitChecked"] = True
                if next_end >= end:
                    break
                cursor["activityLookback"] = next_end
                if parsed:
                    cursor.update({k: parsed[k] for k in ("activeTurn", "activeStartedAt", "activityKnown", "activityInitializationOnly")})
            if not cursor.get("activityKnown"):
                backlog |= cursor.get("activityLookback", 0) > 0
                reason = "activity_backfill" if cursor.get("activityLookback", 0) > 0 else "lifecycle_unknown"
                unknown += 1
                continue
            if cursor.get("observedSize", 0) != stat.st_size:
                reason = "read_budget_exhausted" if budget < MAX_HEADER + 1 else "record_budget_exhausted" if record_budget <= 0 or candidate_budget <= 0 else "unread_changes"
                unknown += 1
                continue
            if not cursor.get("activeTurn"):
                state, reason = "idle", "initialization_only" if cursor.get("activityInitializationOnly") else "task_ended"
                continue
            if time.time() - cursor.get("activityCheckedAt", 0) >= 10 and binding_checks < 4:
                cursor["activityBound"] = execution_binding(path, binding_diagnostic)
                if cursor["activityBound"]:
                    binding_diagnostic.clear()
                cursor["activityCheckedAt"] = time.time()
                binding_checks += 1
            if not cursor.get("activityBound") or time.time() - cursor.get("activityCheckedAt", 0) > 30:
                reason = "binding_unverified" if not cursor.get("activityBound") else "binding_stale"
                unknown += 1
                continue
            name, _ = session_label(row)
            running.append({"sessionId": sid, "turnId": cursor["activeTurn"], "sessionName": name,
                            "nameState": "resolved" if name else "unset" if any(k in row for k in ("name", "title")) else "unavailable",
                            "startedAt": cursor.get("activeStartedAt")})
            state, reason = "running", "task_started"
        except (OSError, ValueError, TypeError, AttributeError):
            unknown += 1
        finally:
            if isinstance(sid, str) and 0 < len(sid) <= 128:
                name, _ = session_label(row)
                diagnostics.append({"kind": "activity", "sessionId": sid, "sessionName": name[:96] if name else None,
                    "turnId": cursor.get("activeTurn") if cursor else None, "state": state, "reason": reason,
                    "fileBytes": stat.st_size if stat else 0, "observedBytes": cursor.get("observedSize", 0) if cursor else 0,
                    "lookbackBytes": cursor.get("activityLookback", 0) if cursor else 0,
                    **({"bindingReason": binding_diagnostic["reason"]} if "reason" in binding_diagnostic else {}),
                    **{k: binding_diagnostic[k] for k in ("osError", "processId", "operation") if k in binding_diagnostic}})
            if cursor is not None and (budget < before_budget or binding_checks > before_bindings):
                sequence += 1
                cursor["activityOrder"] = sequence
    result.update({"activeSessions": running, "activityUnknown": unknown, "activity": progress,
                   "activityBacklog": backlog, "diagnostics": diagnostics})
    return record_budget


def scan(request):
    requested_home = request.get("home")
    if requested_home is not None and (not isinstance(requested_home, str) or len(requested_home) > 4096):
        raise ValueError("invalid_home")
    home = Path(requested_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    cursors = request.get("cursors", {})
    if not isinstance(cursors, dict) or len(cursors) > 128 or any(not isinstance(k, str) or not 1 <= len(k) <= 128 for k in cursors):
        raise ValueError("invalid_cursors")
    known = request.get("knownCompletions", [])
    if not isinstance(known, list) or len(known) > 200 or any(
            not isinstance(event, dict) or any(not isinstance(event.get(k), str) or not 1 <= len(event[k]) <= 128
                                             for k in ("sessionId", "turnId")) for event in known):
        raise ValueError("invalid_known_completions")
    known = {(event["sessionId"], event["turnId"]) for event in known}
    started = request.get("startedAt", time.time())
    position = request.get("position", 0)
    initialized = request.get("initialized", False)
    notification_backlog = request.get("notificationBacklog", False)
    if not isinstance(started, (int, float)) or isinstance(started, bool) or not 0 < started <= time.time() + 300:
        raise ValueError("invalid_started_time")
    if not isinstance(position, int) or isinstance(position, bool) or not 0 <= position <= 128 or not isinstance(initialized, bool) or not isinstance(notification_backlog, bool):
        raise ValueError("invalid_scan_state")
    result = {"schemaVersion": 2, "sourceId": source_identity(home), "hostname": platform.node(),
              "observedAt": datetime.now(timezone.utc).isoformat(), "initialized": True,
              "cursors": {}, "position": 0, "events": [], "warnings": [], "bytesRead": 0,
              "verificationFailures": [], "notificationBacklog": False}
    database = home / "state_5.sqlite"
    if not database.is_file():
        raise ValueError("database_missing")
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.execute("PRAGMA query_only=ON")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
        if not {"id", "source", "rollout_path", "updated_at"}.issubset(columns):
            raise ValueError("unsupported_database_schema")
        selected = ["id", "source", "rollout_path", "updated_at"] + [k for k in ("name", "title", "archived") if k in columns]
        active = "(archived=0 OR archived IS NULL)" if "archived" in columns else "1=1"
        query = "SELECT " + ",".join(selected) + " FROM threads WHERE " + CLIENT_SOURCE_SQL + " AND ((" + active + " AND updated_at>=?)"
        params = [*CLIENT_SOURCES, int(time.time()) - 2100]
        if cursors:
            query += " OR id IN (" + ",".join("?" for _ in cursors) + ")"
            params.extend(cursors)
        query += ") ORDER BY id LIMIT 129"
        rows = [dict(zip(selected, row)) for row in conn.execute(query, params)]
    if len(rows) > 128:
        raise ValueError("session_capacity_exceeded")
    previous_activity = request.get("activity", {})
    if not isinstance(previous_activity, dict) or len(previous_activity) > 128 or any(not isinstance(v, dict) for v in previous_activity.values()):
        raise ValueError("invalid_activity_state")
    # Initial discovery can need tens of MiB before the last lifecycle record.
    # Read larger bounded batches for unfinished reverse discovery or advancing
    # notification replay; steady polls retain their budget. Reserve 512 KiB
    # for notification replay, and retain the per-session fair ordering above.
    catchup = notification_backlog or any(row["id"] not in previous_activity or
                  (not previous_activity[row["id"]].get("activityKnown") and
                   previous_activity[row["id"]].get("activityLookback", 0) > 0)
                  for row in rows)
    scan_budget = MAX_CATCHUP_BYTES if catchup else MAX_SCAN_BYTES
    record_budget = collect_activity([row for row in rows if not row.get("archived")], home, result,
                                    previous_activity, scan_budget - 524288, 100)
    budget = scan_budget - result["bytesRead"]
    forward_progress, forward_pending = False, False
    if rows:
        position %= len(rows)
    next_position = position

    def save_anchor(path, cursor):
        nonlocal budget
        anchor = checkpoint_bytes(path, cursor["offset"])
        cursor["anchor"] = hashlib.sha256(anchor).hexdigest()
        budget -= len(anchor)
        result["bytesRead"] += len(anchor)
        return anchor

    rows = rows[position:] + rows[:position]
    for index, row in enumerate(rows):
        sid = row["id"]
        if not isinstance(sid, str) or not 1 <= len(sid) <= 128:
            result["warnings"].append("invalid_session_identity")
            continue
        previous = cursors.get(sid)
        if previous is not None and (not isinstance(previous, dict) or not isinstance(previous.get("offset"), int)
                                     or isinstance(previous["offset"], bool) or not 0 <= previous["offset"] <= 2 ** 53):
            raise ValueError("invalid_cursor")
        path = Path(row["rollout_path"])
        try:
            path = path.resolve(strict=True)
            comparison_path(path).relative_to(rollout_root(home, row))
            stat = path.stat()
            key = hashlib.sha256((str(path) + ":" + str(stat.st_dev) + ":" + str(stat.st_ino)).encode()).hexdigest()
        except (OSError, ValueError):
            result["warnings"].append("session_file_unavailable")
            continue
        same = previous and previous.get("fileKey") == key and previous["offset"] <= stat.st_size
        if same and isinstance(previous.get("anchor"), str):
            if budget < 64:
                result["cursors"][sid] = previous
                result["warnings"].append("read_backlog")
                forward_pending |= previous["offset"] < stat.st_size
                continue
            checkpoint = dict(previous)
            save_anchor(path, checkpoint)
            same = checkpoint["anchor"] == previous["anchor"]
        cursor = dict(previous) if same else {"fileKey": key, "offset": stat.st_size, "skipping": False, "parser": previous.get("parser", {}) if previous else {}}
        result["cursors"][sid] = cursor
        if not initialized:
            # Baseline every known recent session, including incomplete final lines.
            cursor["offset"] = stat.st_size
            cursor["skipping"] = stat.st_size > 0
            anchor = save_anchor(path, cursor)
            cursor["skipping"] = bool(anchor) and not anchor.endswith(b"\n")
            if row.get("archived"):
                result["cursors"].pop(sid, None)
            continue
        if same and cursor["offset"] == stat.st_size:
            if row.get("archived"):
                result["cursors"].pop(sid, None)
            continue
        if budget < MAX_HEADER + 129 or record_budget <= 0:
            if previous:
                result["cursors"][sid] = previous
            else:
                result["cursors"].pop(sid, None)
            result["warnings"].append("read_backlog")
            forward_pending = True
            continue
        with path.open("rb") as stream:
            header = stream.readline(MAX_HEADER + 1)
        budget -= len(header)
        result["bytesRead"] += len(header)
        if not valid_session_header(header, sid, row["source"]):
            result["warnings"].append("session_metadata_mismatch")
            result["cursors"].pop(sid, None)
            continue
        if not same:
            cursor["offset"] = max(len(header), stat.st_size - MAX_TAIL)
            cursor["skipping"] = cursor["offset"] > len(header)
            if previous and not row.get("archived"):
                result["warnings"].append("session_file_rebased")
        cap = min(MAX_CATCHUP_BYTES if notification_backlog else MAX_TAIL, budget - 64)
        before_offset = cursor["offset"]
        with path.open("rb") as stream:
            stream.seek(cursor["offset"])
            raw = stream.read(cap)
        budget -= len(raw)
        result["bytesRead"] += len(raw)
        if raw:
            # Resume after the last file actually served. A fixed stride can
            # starve peers when one large file consumes a whole catchup batch.
            next_position = (position + index + 1) % len(rows)
        if cursor.get("skipping"):
            newline = raw.find(b"\n")
            if newline < 0:
                cursor["offset"] += len(raw)
                save_anchor(path, cursor)
                forward_progress |= cursor["offset"] > before_offset
                forward_pending |= cursor["offset"] < stat.st_size
                continue
            cursor["offset"] += newline + 1
            raw = raw[newline + 1:]
            cursor["skipping"] = False
        parsed = parse_records(raw, cursor.get("parser"), record_budget)
        result["warnings"].extend(parsed["warnings"])
        cursor["offset"] += parsed["consumed"]
        cursor["parser"] = parsed["state"]
        record_budget -= parsed["records"]
        if not parsed["consumed"] and len(raw) == cap and cap >= MAX_TAIL and before_offset + cap < stat.st_size:
            cursor["offset"] += len(raw)
            cursor["skipping"] = True
            result["warnings"].append("oversized_record_skipped")
        candidates = []
        for event in parsed["completions"]:
            completed = event["completedAt"]
            decision = "eligible"
            if completed is None:
                result["warnings"].append("completion_time_unavailable")
                decision = "time_unavailable"
            else:
                timestamp = datetime.fromisoformat(completed.replace("Z", "+00:00")).timestamp()
                if (sid, event["turnId"]) in known:
                    decision = "already_queued"
                elif timestamp < max(started, time.time() - 1800):
                    decision = "before_monitor_start" if timestamp < started else "expired"
                elif timestamp > time.time() + 60:
                    decision = "future_time"
            name, _ = session_label(row)
            result["diagnostics"].append({"kind": "completion", "sessionId": sid, "sessionName": name[:96] if name else None,
                "turnId": event["turnId"], "completedAt": completed, "state": "observed", "reason": decision,
                "offset": cursor["offset"], "fileBytes": stat.st_size})
            # Local outbox identities have already passed source verification.
            # A native archive move can replay those bytes after the writer has
            # closed the file. Skip only exact known session/turn pairs; this
            # never supplies provenance for a new completion in the same file.
            if decision == "eligible":
                candidates.append(event)
        if candidates:
            diagnostic = {"reason": "backend_not_observed"}
            if not execution_binding(path, diagnostic):
                result["warnings"].append("execution_source_unverified")
                name, _ = session_label(row)
                result["verificationFailures"].append({"sessionId": sid, "sessionName": name[:256] if name else None,
                    "turnIds": [event["turnId"] for event in candidates], **diagnostic})
                for item in result["diagnostics"]:
                    if item["kind"] == "completion" and item["sessionId"] == sid and item["reason"] == "eligible":
                        item.update(state="withheld", reason="source_unverified", bindingReason=diagnostic["reason"])
                # Retry the same bytes; do not silently lose a fresh completion.
                if previous:
                    result["cursors"][sid] = previous
                else:
                    result["cursors"].pop(sid, None)
                continue
            name, name_source = session_label(row)
            for item in result["diagnostics"]:
                if item["kind"] == "completion" and item["sessionId"] == sid and item["reason"] == "eligible":
                    item.update(state="verified", reason="new_completion")
            name_state = "resolved" if name else "unset" if any(k in row for k in ("name", "title")) else "unavailable"
            for event in candidates:
                result["events"].append({"sessionId": sid, "sessionName": name,
                                         "nameState": name_state, "nameSource": name_source,
                                         "evidence": "persisted-completion", **event})
        save_anchor(path, cursor)
        forward_progress |= cursor["offset"] > before_offset
        forward_pending |= cursor["offset"] < stat.st_size
        if row.get("archived") and cursor["offset"] == stat.st_size:
            result["cursors"].pop(sid, None)
    if rows:
        result["position"] = next_position
    result["notificationBacklog"] = forward_progress and forward_pending
    if result["notificationBacklog"]:
        result["warnings"].append("read_backlog")
    for diagnostic in result["diagnostics"]:
        cursor = result["cursors"].get(diagnostic["sessionId"], {})
        diagnostic.setdefault("offset", cursor.get("offset", 0))
    result["diagnostics"].append({"kind": "scan", "sessionId": "", "turnId": None,
        "state": "ok", "reason": "read_backlog" if "read_backlog" in result["warnings"] else "scan_complete",
        "bytesRead": result["bytesRead"], "runningCount": len(result["activeSessions"]),
        "unknownCount": result["activityUnknown"], "position": result["position"]})
    result["warnings"] = sorted(set(result["warnings"]))
    return result


def labels(request):
    """Rehydrate only names for already verified, queued completion IDs."""
    requested_home = request.get("home")
    if requested_home is not None and (not isinstance(requested_home, str) or len(requested_home) > 4096):
        raise ValueError("invalid_home")
    home = Path(requested_home or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    ids = request.get("sessionIds")
    if not isinstance(ids, list) or not 1 <= len(ids) <= 10 or any(not isinstance(s, str) or not 1 <= len(s) <= 128 for s in ids):
        raise ValueError("invalid_session_ids")
    result = {"schemaVersion": 3, "sourceId": source_identity(home), "hostname": platform.node(), "labels": []}
    database = home / "state_5.sqlite"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=2)) as conn:
        conn.execute("PRAGMA query_only=ON")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(threads)")}
        if not {"id", "source"}.issubset(columns):
            raise ValueError("unsupported_database_schema")
        selected = ["id"] + [k for k in ("name", "title") if k in columns]
        query = "SELECT " + ",".join(selected) + " FROM threads WHERE " + CLIENT_SOURCE_SQL + " AND id IN (" + ",".join("?" for _ in ids) + ") LIMIT 10"
        found = {row[0]: dict(zip(selected, row)) for row in conn.execute(query, [*CLIENT_SOURCES, *ids])}
    for sid in ids:
        row = found.get(sid, {})
        name, name_source = session_label(row)
        result["labels"].append({"sessionId": sid, "sessionName": name, "nameSource": name_source,
                                 "nameState": "resolved" if name else "unset" if any(k in row for k in ("name", "title")) else "unavailable"})
    return result


def notification_claim(request):
    """Serialize attempts on the source machine, never modify Codex data.

    A successful claim is durable before HTTP. It does not expire into a new
    sender: retention exceeds event eligibility, including future tolerance.
    Only an owner that knows HTTP never started may explicitly release it.
    """
    home = Path(request.get("home") or os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser().resolve()
    source = source_identity(home)
    if request.get("sourceId") != source:
        raise ValueError("coordination_source_changed")
    scope, ticket = request.get("scope"), request.get("ticket")
    if not isinstance(scope, str) or not re.fullmatch(r"[0-9a-f]{64}", scope) or not isinstance(ticket, str) or not re.fullmatch(r"[0-9a-f-]{36}", ticket):
        raise ValueError("invalid_coordination_request")
    ids = [request.get("sessionId"), request.get("turnId")]
    if any(not isinstance(value, str) or not 1 <= len(value) <= 128 for value in ids):
        raise ValueError("invalid_coordination_request")
    completed = request.get("completedAt")
    if not isinstance(completed, str) or len(completed) > 128:
        raise ValueError("invalid_coordination_request")
    parsed = datetime.fromisoformat(completed.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("invalid_coordination_request")
    now = time.time()
    result = {"schemaVersion": 4, "sourceId": source, "acquired": False}
    if not now - 1800 <= parsed.timestamp() <= now + 60:
        return dict(result, reason="expired")
    key = hashlib.sha256(json.dumps([source, scope] + ids, separators=(",", ":")).encode()).hexdigest()
    # Fixed user-owned path, shared by local and SSH execution under this user.
    # Do not put this in CODEX_HOME, a workspace, or a client-supplied path.
    directory = Path.home() / ".codex-session-notifier"
    directory.mkdir(mode=0o700, exist_ok=True)
    database = directory / "delivery-v1.sqlite"
    descriptor = os.open(str(database), os.O_CREAT | os.O_RDWR, 0o600)
    os.close(descriptor)
    with closing(sqlite3.connect(str(database), timeout=2)) as conn:
        page_size = conn.execute("PRAGMA page_size").fetchone()[0]
        conn.execute("PRAGMA max_page_count=%d" % (16777216 // page_size))
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("CREATE TABLE IF NOT EXISTS claims(event_key TEXT PRIMARY KEY, ticket TEXT NOT NULL, claimed_at REAL NOT NULL)")
        conn.execute("CREATE INDEX IF NOT EXISTS claims_age ON claims(claimed_at)")
        conn.execute("DELETE FROM claims WHERE claimed_at<?", (now - 86400,))
        if request["mode"] == "notification_release":
            conn.execute("DELETE FROM claims WHERE event_key=? AND ticket=?", (key, ticket))
        elif conn.execute("SELECT 1 FROM claims WHERE event_key=?", (key,)).fetchone():
            result["reason"] = "duplicate"
        else:
            if conn.execute("SELECT COUNT(*) FROM claims").fetchone()[0] >= 10000:
                raise ValueError("coordination_capacity_reached")
            conn.execute("INSERT INTO claims VALUES(?,?,?)", (key, ticket, now))
            result["acquired"] = True
        conn.commit()
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", default="e30=", help="base64 UTF-8 JSON request")
    args = parser.parse_args()
    try:
        request = json.loads(base64.b64decode(args.request, validate=True))
        if not isinstance(request, dict):
            raise ValueError("invalid_request")
        mode = request.get("mode")
        response = notification_claim(request) if mode in ("notification_claim", "notification_release") else scan(request) if mode == "scan" else labels(request) if mode == "labels" else probe(request)
        encoded = json.dumps(response, ensure_ascii=True)
        if len(encoded.encode("utf-8")) > MAX_OUTPUT:
            raise ValueError("output_budget_exceeded")
        print(encoded)
    except (OSError, sqlite3.Error, ValueError, TypeError) as error:
        # Error text could contain paths or raw data, so return a code only.
        code = str(error) if isinstance(error, ValueError) and str(error) in (
            "database_missing", "unsupported_database_schema", "machine_identity_unavailable",
            "coordination_capacity_reached", "coordination_source_changed") else "probe_failed"
        print(json.dumps({"error": code}))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
