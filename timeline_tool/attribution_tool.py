#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
attribution_tool.py — 班组归属层（SQLite）离线建仓 / 更正 / 查询

在 timeline_tool.py 之上再加一层“当时可能由哪个班组负责这台设备”：

  * 事件时间事实直接取自 timeline_tool.py `run` 的 JSON 输出（不重新推导、
    不取区间中点）；有锚事件保留闭区间 [earliest, latest]，无锚事件单独记为
    `unlocatable`，不参与班表匹配。
  * 设备绑定来自 JSON 清单：按 (工单, 事件) 绑定设备。正常工单的每个事件必须
    恰好绑定一台设备；隔离工单不生成归属行。
  * 班次是半开区间 [start_ms, end_ms)，即覆盖整数毫秒 start_ms .. end_ms-1；
    优先级为整数，越大越优先。
  * 归属按“时间分段”计算：每台设备的断点 = 所有班次起止点之并；相邻断点之间
    有效班次集合不变。事件闭区间与这些段做区间连接——不逐毫秒执行，跨长时间段
    同样可处理。每段只看该设备当时有效的最高优先级班次；同优先级同班组不算
    冲突，不同班组全部保留并记冲突。先后两班不构成冲突。
  * 时间/优先级全部落 SQLite INTEGER（有符号 64 位），入口拒绝超界值、浮点、
    布尔与字符串，不做浮点近似。

子命令：
    build    首次建仓（读取 run 输出 + 清单；可选初始班表）
    correct  一次更正某台/若干台设备的若干班次（整批全有或全无）
    show     读取某工单/设备/事件的归属
    summary  数据库摘要（批次数、事件数、冲突/空档计数）
    shifts   列出班表
    gaps     列出设备无班次覆盖的区间
    drift    检查物化归属与当前班表是否一致
    touch    按设备 + 时间闭区间在 SQL 侧查找候选事件
    sql      对数据库执行只读 SQL 查询（方便文档示例）

只依赖 Python 3 标准库（sqlite3 / json）。
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys

import timeline_tool

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "attribution_schema.sql")
VIEWS_PATH = os.path.join(HERE, "attribution_views.sql")

I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1

ATTRIBUTION_COLUMNS = (
    "work_order, event, device, time_status, earliest, latest, teams_json, "
    "team_count, has_coverage, has_gap, has_conflict, max_priority, "
    "shift_ids_json"
)
ATTRIBUTION_FIELDS = [c.strip() for c in ATTRIBUTION_COLUMNS.split(",")]


# ---------------------------------------------------------------------------
# 错误与基本校验
# ---------------------------------------------------------------------------

class Reject(Exception):
    """整批拒绝：带机器可读的 code 与说明。"""

    def __init__(self, code, message, **details):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.details = details

    def as_dict(self):
        d = {"code": self.code, "message": self.message}
        d.update(self.details)
        return d


class _DupDict(dict):
    """记录 JSON 对象里的重复键（重复键会让“同一事实”变得有歧义）。"""


def _json_pairs_hook(pairs):
    d = _DupDict()
    dups = []
    for k, v in pairs:
        if k in d:
            dups.append(k)
        d[k] = v
    if dups:
        raise Reject("duplicate_json_key",
                     "duplicate object key(s): %s" % ", ".join(map(repr, dups)),
                     keys=dups)
    return d


def load_json_file(path, what):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as ex:
        raise Reject("io_error", "cannot read %s: %s" % (what, ex))
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_pairs_hook)
    except Reject:
        raise
    except (UnicodeDecodeError, ValueError) as ex:
        raise Reject("invalid_json", "%s is not valid JSON: %s" % (what, ex))
    return obj, raw


def require_int(v, field, where):
    if isinstance(v, bool) or not isinstance(v, int):
        raise Reject("invalid_integer",
                     "%s: %s must be an exact integer (not bool/float/string)"
                     % (where, field), field=field)
    if v < I64_MIN or v > I64_MAX:
        raise Reject("integer_out_of_range",
                     "%s: %s=%r outside signed 64-bit range [%d, %d]"
                     % (where, field, v, I64_MIN, I64_MAX), field=field)
    return v


def require_str(v, field, where, nonempty=True):
    if not isinstance(v, str) or (nonempty and not v.strip()):
        raise Reject("invalid_string",
                     "%s: %s must be a non-empty string" % (where, field),
                     field=field)
    return v


def json_text(v):
    return json.dumps(v, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256_bytes(raw):
    return hashlib.sha256(raw).hexdigest()


# ---------------------------------------------------------------------------
# timeline 事实载入
# ---------------------------------------------------------------------------

def load_timeline(spec_path=None, run_output_path=None):
    """取得 timeline 事实。

    优先读取已有的 `run` JSON 输出（--run-output）；否则读取规格并调用现有
    run()。返回 (out, spec_or_None, sha256)。
    """
    if run_output_path:
        out, raw = load_json_file(run_output_path, "run output")
        if not isinstance(out, dict) or not isinstance(out.get("work_orders"), list):
            raise Reject("invalid_timeline",
                         "run output must be an object with a work_orders list")
        return out, None, sha256_bytes(raw)
    spec, raw = load_json_file(spec_path, "timeline spec")
    if not isinstance(spec, dict) or not isinstance(spec.get("work_orders"), list):
        raise Reject("invalid_timeline",
                     "timeline spec must be an object with a work_orders list")
    try:
        out = timeline_tool.run(spec)
    except Reject:
        raise
    except Exception as ex:  # 现有 run 对坏输入可能抛异常
        raise Reject("invalid_timeline", "timeline run failed: %s" % ex)
    return out, spec, sha256_bytes(raw)


def fact_rows(out, spec=None):
    """把 run 输出拆成 work_orders / events 两类可入库事实，并做 i64/结构校验。"""
    wo_rows = []
    ev_rows = []
    known_events = {}
    seen_wo = set()
    for wo in out.get("work_orders", []):
        if not isinstance(wo, dict):
            raise Reject("invalid_timeline", "run output contains a non-object work order")
        wid = require_str(wo.get("id"), "id", "work order")
        if wid in seen_wo:
            raise Reject("invalid_timeline", "duplicate work_order id %r" % wid)
        seen_wo.add(wid)
        status = wo.get("status")
        if status not in ("ok", "quarantined"):
            raise Reject("invalid_timeline",
                         "work order %r has unexpected status %r" % (wid, status))
        wo_rows.append((wid, status, wo.get("error_kind"), json_text(wo)))
        if status != "ok":
            # 隔离工单：不生成 time facts，也不生成归属行
            spec_wo = None
            if isinstance(spec, dict):
                spec_wo = next((w for w in spec.get("work_orders", [])
                                if isinstance(w, dict) and w.get("id") == wid),
                               None)
            evs = spec_wo.get("events") if isinstance(spec_wo, dict) else None
            if isinstance(evs, list) and all(isinstance(e, str) for e in evs):
                known_events[wid] = set(evs)
            continue
        bounds = wo.get("bounds")
        if not isinstance(bounds, list):
            raise Reject("invalid_timeline",
                         "work order %r status ok but bounds is not a list" % wid)
        names = set()
        for b in bounds:
            if not isinstance(b, dict):
                raise Reject("invalid_timeline", "bad bounds entry in %r" % wid)
            ev = require_str(b.get("event"), "event", "bounds of %r" % wid)
            if ev in names:
                raise Reject("invalid_timeline",
                             "duplicate bound for event %r in %r" % (ev, wid))
            names.add(ev)
            lo, hi = b.get("earliest"), b.get("latest")
            if lo is None and hi is None:
                ev_rows.append((wid, ev, "unlocatable", None, None,
                                json_text(b)))
            elif lo is None or hi is None:
                raise Reject("invalid_timeline",
                             "event %r in %r has only one-sided bound" % (ev, wid))
            else:
                lo = require_int(lo, "earliest", "event %r of %r" % (ev, wid))
                hi = require_int(hi, "latest", "event %r of %r" % (ev, wid))
                if lo > hi:
                    raise Reject("invalid_timeline",
                                 "event %r in %r has earliest > latest" % (ev, wid))
                ev_rows.append((wid, ev, "anchored", lo, hi, json_text(b)))
        known_events[wid] = names
    return wo_rows, ev_rows, known_events


def manifest_bindings(manifest, wo_status, known_events, ev_rows):
    """校验 JSON 清单，返回 (bindings, ignored, notes)。

    支持的清单形状（可同时出现，含义相同）：
      {"bindings": {"WO-1": {"e1": "DEV-1"}},
       "work_orders": {"WO-1": {"events": {"e1": "DEV-1"}}
                              | {"events": ["e1"], "device": "DEV-1"},
                       ...}}
    重复绑定同一 (工单, 事件) 到不同设备 => 整批拒绝。
    隔离工单上的绑定被接受但忽略（不生成归属行）。
    """
    if not isinstance(manifest, dict):
        raise Reject("invalid_manifest", "manifest must be a JSON object")
    raw_pairs = []
    bindings = manifest.get("bindings", {})
    if bindings is None:
        bindings = {}
    if not isinstance(bindings, dict):
        raise Reject("invalid_manifest", "manifest.bindings must be an object")
    for wo, evmap in bindings.items():
        require_str(wo, "bindings work order", "manifest")
        if not isinstance(evmap, dict):
            raise Reject("invalid_manifest",
                         "bindings[%r] must be an object of event -> device" % wo)
        for ev, dev in evmap.items():
            require_str(ev, "bindings event", "manifest[%r]" % wo)
            require_str(dev, "bindings device", "manifest[%r][%r]" % (wo, ev))
            raw_pairs.append((wo, ev, dev))

    wos = manifest.get("work_orders", {})
    if wos is None:
        wos = {}
    if not isinstance(wos, dict):
        raise Reject("invalid_manifest", "manifest.work_orders must be an object")
    for wo, meta in wos.items():
        require_str(wo, "work_orders key", "manifest")
        if meta is None:
            continue
        if not isinstance(meta, dict):
            raise Reject("invalid_manifest",
                         "manifest.work_orders[%r] must be an object" % wo)
        if meta.get("isolated"):
            continue
        evs = meta.get("events")
        default_dev = meta.get("device")
        if evs is None:
            continue
        if isinstance(evs, dict):
            for ev, dev in evs.items():
                require_str(ev, "work_orders event", "manifest[%r]" % wo)
                require_str(dev, "work_orders device", "manifest[%r][%r]" % (wo, ev))
                raw_pairs.append((wo, ev, dev))
        elif isinstance(evs, list):
            if default_dev is None:
                raise Reject("invalid_manifest",
                             "manifest.work_orders[%r].events is a list but "
                             "no device given" % wo)
            require_str(default_dev, "device", "manifest[%r]" % wo)
            for ev in evs:
                require_str(ev, "event", "manifest[%r].events" % wo)
                raw_pairs.append((wo, ev, default_dev))
        else:
            raise Reject("invalid_manifest",
                         "manifest.work_orders[%r].events must be object or list" % wo)

    resolved = {}
    ignored = []
    for wo, ev, dev in raw_pairs:
        if wo not in wo_status:
            raise Reject("unknown_work_order",
                         "binding references work order %r absent from timeline" % wo,
                         work_order=wo)
        if wo_status[wo] != "ok":
            ignored.append({"work_order": wo, "event": ev,
                            "reason": "work_order_quarantined"})
            continue
        known = known_events.get(wo)
        if known is not None and ev not in known:
            raise Reject("unknown_event",
                         "binding references event %r absent from work order %r"
                         % (ev, wo), work_order=wo, event=ev)
        prev = resolved.get((wo, ev))
        if prev is not None and prev != dev:
            raise Reject("conflicting_binding",
                         "event %r of work order %r bound to both %r and %r"
                         % (ev, wo, prev, dev), work_order=wo, event=ev)
        resolved[(wo, ev)] = dev

    have = {(wo, ev) for (wo, ev, _s, _a, _b, _c) in ev_rows}
    missing = sorted(have - set(resolved))
    if missing:
        raise Reject("unbound_event",
                     "%d event(s) of normal work orders have no device binding"
                     % len(missing),
                     missing=[{"work_order": w, "event": e} for w, e in missing[:50]])
    return resolved, ignored


# ---------------------------------------------------------------------------
# 班次记录校验
# ---------------------------------------------------------------------------

SHIFT_KEYS = ("device", "id", "team", "start_ms", "end_ms", "priority")


def _pick(rec, names, field, where, required=True):
    for n in names:
        if n in rec:
            return rec[n]
    if required:
        raise Reject("missing_field", "%s: missing %s" % (where, field),
                     field=field)
    return None


def parse_shift(rec, where, action=None):
    """校验一条班次；返回 dict。空班次 / 逆置 / 非整数 / 超界一律拒绝。"""
    if not isinstance(rec, dict):
        raise Reject("invalid_shift", "%s must be an object" % where)
    device = require_str(_pick(rec, ("device",), "device", where),
                         "device", where)
    sid = require_str(_pick(rec, ("id", "shift_id"), "id", where), "id", where)
    if action == "delete":
        return {"device": device, "id": sid}
    team = require_str(_pick(rec, ("team",), "team", where), "team", where)
    start = require_int(_pick(rec, ("start_ms", "start"), "start_ms", where),
                        "start_ms", where)
    end = require_int(_pick(rec, ("end_ms", "end"), "end_ms", where),
                      "end_ms", where)
    prio = require_int(_pick(rec, ("priority",), "priority", where),
                       "priority", where)
    if start >= end:
        raise Reject("empty_shift",
                     "%s: shift %s/%s is empty or reversed ([%d, %d) covers no ms)"
                     % (where, device, sid, start, end),
                     device=device, shift_id=sid)
    return {"device": device, "id": sid, "team": team,
            "start_ms": start, "end_ms": end, "priority": prio}


def load_initial_shifts(paths):
    out = []
    seen = set()
    for path in paths:
        obj, _raw = load_json_file(path, "shifts file")
        recs = obj.get("shifts") if isinstance(obj, dict) else None
        if not isinstance(recs, list):
            raise Reject("invalid_shifts_file",
                         "%s: expected {\"shifts\": [...]}" % path)
        for i, rec in enumerate(recs):
            sh = parse_shift(rec, "shifts[%d]" % i)
            key = (sh["device"], sh["id"])
            if key in seen:
                raise Reject("duplicate_shift",
                             "duplicate shift %s/%s in initial shifts" % key)
            seen.add(key)
            out.append(sh)
    return out


# ---------------------------------------------------------------------------
# 数据库连接 / 事务
# ---------------------------------------------------------------------------

def open_db(path, must_exist=True):
    if must_exist and not os.path.exists(path):
        raise Reject("missing_database", "database %r does not exist" % path)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")   # 读者与写者互不阻塞
    return conn


def begin_immediate(conn):
    conn.execute("BEGIN IMMEDIATE")


def rollback_quiet(conn):
    try:
        conn.execute("ROLLBACK")
    except sqlite3.Error:
        pass


def ensure_schema(conn):
    conn.executescript(open(SCHEMA_PATH, "r", encoding="utf-8").read())
    conn.executescript(open(VIEWS_PATH, "r", encoding="utf-8").read())


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

def cmd_build(args):
    if not args.timeline and not args.run_output:
        raise Reject("missing_input",
                     "provide --timeline SPEC or --run-output RUN_JSON")
    out, spec, timeline_sha = load_timeline(args.timeline, args.run_output)
    wo_rows, ev_rows, known_events = fact_rows(out, spec)
    wo_status = {wid: st for wid, st, _ek, _j in wo_rows}

    manifest, manifest_raw = load_json_file(args.manifest, "manifest")
    bindings, ignored = manifest_bindings(manifest, wo_status, known_events,
                                          ev_rows)

    shift_paths = list(args.shifts or [])
    manifest_shifts = manifest.get("shifts") if isinstance(manifest, dict) else None
    initial = []
    if shift_paths:
        initial.extend(load_initial_shifts(shift_paths))
    if manifest_shifts is not None:
        if not isinstance(manifest_shifts, list):
            raise Reject("invalid_manifest", "manifest.shifts must be a list")
        seen = {(s["device"], s["id"]) for s in initial}
        for i, rec in enumerate(manifest_shifts):
            sh = parse_shift(rec, "manifest.shifts[%d]" % i)
            key = (sh["device"], sh["id"])
            if key in seen:
                raise Reject("duplicate_shift",
                             "duplicate shift %s/%s" % key)
            seen.add(key)
            initial.append(sh)

    db_path = args.db
    if os.path.exists(db_path) and not args.force:
        raise Reject("database_exists",
                     "database %r already exists (use --force to rebuild)" % db_path)
    db_dir = os.path.dirname(os.path.abspath(db_path)) or "."
    os.makedirs(db_dir, exist_ok=True)
    tmp_path = "%s.building-%d" % (db_path, os.getpid())
    for p in (tmp_path, tmp_path + "-wal", tmp_path + "-shm"):
        if os.path.exists(p):
            os.unlink(p)

    conn = sqlite3.connect(tmp_path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    summary = None
    try:
        ensure_schema(conn)
        begin_immediate(conn)
        try:
            cur = conn.execute(
                "INSERT INTO batches (batch_key, kind, created_at, note, "
                "timeline_sha256, manifest_sha256) VALUES (?,?,?,?,?,?)",
                (None, "build", _utcnow(), args.note, timeline_sha,
                 sha256_bytes(manifest_raw)))
            batch_id = cur.lastrowid

            conn.executemany(
                "INSERT INTO work_orders (work_order, status, error_kind, result_json) "
                "VALUES (?,?,?,?)", wo_rows)
            conn.executemany(
                "INSERT INTO events (work_order, event, time_status, earliest, "
                "latest, bounds_json) VALUES (?,?,?,?,?,?)", ev_rows)
            conn.executemany(
                "INSERT INTO device_bindings (work_order, event, device) "
                "VALUES (?,?,?)",
                [(wo, ev, dev) for (wo, ev), dev in bindings.items()])
            conn.executemany(
                "INSERT INTO shifts (device, shift_id, team, start_ms, end_ms, "
                "priority, updated_batch) VALUES (?,?,?,?,?,?,?)",
                [(s["device"], s["id"], s["team"], s["start_ms"], s["end_ms"],
                  s["priority"], batch_id) for s in initial])
            conn.execute(
                "INSERT INTO attributions (" + ATTRIBUTION_COLUMNS +
                ", computed_batch) SELECT " + ATTRIBUTION_COLUMNS +
                ", ? FROM v_event_attribution", (batch_id,))

            meta = {
                "format_version": "1",
                "generator": "attribution_tool.py",
                "built_at": _utcnow(),
                "timeline_sha256": timeline_sha,
                "manifest_sha256": sha256_bytes(manifest_raw),
            }
            conn.executemany("INSERT INTO meta (key, value) VALUES (?,?)",
                             sorted(meta.items()))
            n_attr = conn.execute(
                "SELECT COUNT(*) FROM attributions").fetchone()[0]
            n_ev = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
            if n_attr != n_ev:
                raise Reject("internal_inconsistency",
                             "attribution rows (%d) != events (%d)"
                             % (n_attr, n_ev))
            null_team = conn.execute(
                "SELECT COUNT(*) FROM events e JOIN attributions a "
                "ON a.work_order=e.work_order AND a.event=e.event "
                "WHERE e.time_status='anchored' AND a.earliest IS NULL"
            ).fetchone()[0]
            if null_team:
                raise Reject("internal_inconsistency",
                             "anchored events ended up unlocatable")
            conn.execute("COMMIT")
        except BaseException:
            rollback_quiet(conn)
            raise

        conn.execute("PRAGMA journal_mode = WAL")
        summary = {
            "status": "built",
            "database": db_path,
            "batch_id": batch_id,
            "work_orders": {"ok": sum(1 for r in wo_rows if r[1] == "ok"),
                            "quarantined": sum(1 for r in wo_rows
                                               if r[1] == "quarantined")},
            "events": {"anchored": sum(1 for r in ev_rows
                                       if r[2] == "anchored"),
                       "unlocatable": sum(1 for r in ev_rows
                                          if r[2] == "unlocatable")},
            "bindings": len(bindings),
            "shifts": len(initial),
            "attributions": n_attr,
            "ignored_bindings": ignored,
            "timeline_sha256": timeline_sha,
            "manifest_sha256": sha256_bytes(manifest_raw),
        }
    except BaseException:
        conn.close()
        for p in (tmp_path, tmp_path + "-wal", tmp_path + "-shm"):
            if os.path.exists(p):
                os.unlink(p)
        raise
    conn.close()

    for p in (db_path + "-wal", db_path + "-shm"):
        if os.path.exists(p):
            os.unlink(p)
    os.replace(tmp_path, db_path)
    return summary


# ---------------------------------------------------------------------------
# correct
# ---------------------------------------------------------------------------

def _parse_revision(rev):
    if not isinstance(rev, dict):
        raise Reject("invalid_revision", "revision must be a JSON object")
    recs = rev.get("shifts")
    if not isinstance(recs, list) or not recs:
        raise Reject("invalid_revision",
                     "revision.shifts must be a non-empty list")
    ops = []
    for i, rec in enumerate(recs):
        where = "revision.shifts[%d]" % i
        if not isinstance(rec, dict):
            raise Reject("invalid_revision", "%s must be an object" % where)
        action = rec.get("action", rec.get("op", "replace"))
        if not isinstance(action, str) or action not in ("add", "replace", "delete"):
            raise Reject("invalid_action",
                         "%s: action must be add/replace/delete" % where)
        ops.append(parse_shift(rec, where, action=action) |
                   {"action": action, "where": where})
    # 同一 (设备, 班次ID) 在一批里只能出现一次
    seen = {}
    for op in ops:
        key = (op["device"], op["id"])
        if key in seen:
            raise Reject("duplicate_operation",
                         "shift %s/%s is touched twice in one batch (%s, %s)"
                         % (key[0], key[1], seen[key], op["where"]),
                         device=key[0], shift_id=key[1])
        seen[key] = op["where"]
    return ops


def _validate_ops_against_db(conn, ops):
    current = {}
    for op in ops:
        row = conn.execute(
            "SELECT team, start_ms, end_ms, priority FROM shifts "
            "WHERE device=? AND shift_id=?", (op["device"], op["id"])).fetchone()
        exists = row is not None
        if op["action"] == "add" and exists:
            raise Reject("shift_exists",
                         "%s: shift %s/%s already exists"
                         % (op["where"], op["device"], op["id"]),
                         device=op["device"], shift_id=op["id"])
        if op["action"] in ("replace", "delete") and not exists:
            raise Reject("unknown_shift",
                         "%s: shift %s/%s does not exist"
                         % (op["where"], op["device"], op["id"]),
                         device=op["device"], shift_id=op["id"])
        if exists:
            current[(op["device"], op["id"])] = {
                "team": row["team"], "start_ms": row["start_ms"],
                "end_ms": row["end_ms"], "priority": row["priority"]}
    return current


def _apply_ops(conn, ops, batch_id):
    for op in ops:
        key = (op["device"], op["id"])
        if op["action"] == "delete":
            conn.execute("DELETE FROM shifts WHERE device=? AND shift_id=?",
                         (op["device"], op["id"]))
        elif op["action"] == "add":
            conn.execute(
                "INSERT INTO shifts (device, shift_id, team, start_ms, end_ms, "
                "priority, updated_batch) VALUES (?,?,?,?,?,?,?)",
                (op["device"], op["id"], op["team"], op["start_ms"],
                 op["end_ms"], op["priority"], batch_id))
        else:  # replace：整行替换（时段/优先级/班组都可改）
            conn.execute(
                "UPDATE shifts SET team=?, start_ms=?, end_ms=?, priority=?, "
                "updated_batch=? WHERE device=? AND shift_id=?",
                (op["team"], op["start_ms"], op["end_ms"], op["priority"],
                 batch_id, op["device"], op["id"]))


def _affected_pairs(conn, changed_intervals):
    """按“设备 + 时间”条件在 SQL 里找出可能受影响的事件，不载入整台设备历史。"""
    if not changed_intervals:
        return []
    values = ",".join("(?,?,?)" for _ in changed_intervals)
    sql = (
        "WITH changed(device, lo, hi) AS (VALUES " + values + ") "
        "SELECT DISTINCT b.work_order, b.event "
        "FROM device_bindings b "
        "JOIN events e ON e.work_order = b.work_order AND e.event = b.event "
        "JOIN changed c ON c.device = b.device "
        "WHERE e.time_status = 'anchored' "
        "  AND e.earliest <= c.hi AND e.latest >= c.lo "
        "ORDER BY b.work_order, b.event")
    params = []
    for dev, lo, hi in changed_intervals:
        params.extend([dev, lo, hi])
    return [(r[0], r[1]) for r in conn.execute(sql, params)]


def _snapshot_attributions(conn, pairs):
    snap = {}
    if not pairs:
        return snap
    values = ",".join("(?,?)" for _ in pairs)
    sql = ("WITH want(work_order, event) AS (VALUES " + values + ") "
           "SELECT a." + ", a.".join(ATTRIBUTION_FIELDS) + " "
           "FROM attributions a JOIN want w "
           "ON w.work_order = a.work_order AND w.event = a.event")
    params = [p for pair in pairs for p in pair]
    for row in conn.execute(sql, params):
        snap[(row["work_order"], row["event"])] = {
            k: row[k] for k in ATTRIBUTION_FIELDS}
    return snap


def _recompute(conn, pairs, batch_id, before):
    """只写回真正变化的事件；未变化的行保持原 computed_batch 不变。

    候选集 pairs 已由 SQL 按“设备 + 时间相交”条件选出；这里对候选行计算新值，
    逐行与旧值比较，只有不同的才 DELETE+INSERT。这保证“不相交的事实不写回”，
    也让 computed_batch 始终表示最后真正改变该行归属的批次。
    """
    if not pairs:
        return {}
    values = ",".join("(?,?)" for _ in pairs)
    params = [p for pair in pairs for p in pair]
    after = {}
    sql = ("WITH want(work_order, event) AS (VALUES " + values + ") "
           "SELECT " + ", ".join("v." + f for f in ATTRIBUTION_FIELDS) + " "
           "FROM v_event_attribution v JOIN want w "
           "ON w.work_order = v.work_order AND w.event = v.event")
    for row in conn.execute(sql, params):
        after[(row["work_order"], row["event"])] = {
            k: row[k] for k in ATTRIBUTION_FIELDS}
    changed = [p for p in pairs if before.get(p) != after.get(p)]
    for pair in changed:
        row = after[pair]
        conn.execute("DELETE FROM attributions WHERE work_order=? AND event=?",
                     pair)
        conn.execute(
            "INSERT INTO attributions (" + ATTRIBUTION_COLUMNS
            + ", computed_batch) VALUES ("
            + ",".join("?" for _ in ATTRIBUTION_FIELDS) + ",?)",
            [row[f] for f in ATTRIBUTION_FIELDS] + [batch_id])
    return after


def _utcnow():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%fZ")


def cmd_correct(args):
    rev, rev_raw = load_json_file(args.revision, "revision")
    ops = _parse_revision(rev)
    note = rev.get("note") if isinstance(rev, dict) else None

    db_path = args.db
    conn = open_db(db_path)
    result = None
    committed = False
    try:
        begin_immediate(conn)
        if args.hold_ms:
            import time as _time, os as _os
            _time.sleep(args.hold_ms / 1000.0)  # 事务已开、尚未写
            with open(_os.environ.get("ATTRIB_WRITE_MARKER", "/dev/null"), "w") as _m:
                _m.write("go")
        try:
            current = _validate_ops_against_db(conn, ops)
            if args.expect_seq is not None:
                got = conn.execute(
                    "SELECT COALESCE(MAX(batch_id), 0) FROM batches").fetchone()[0]
                if got != args.expect_seq:
                    raise Reject("stale_precondition",
                                 "expected latest batch %d, found %d"
                                 % (args.expect_seq, got),
                                 expected=args.expect_seq, actual=got)

            # 旧/新时段都算“被改到的区间”；与它们无交集的事件不会被重算/写回。
            changed_intervals = []
            for op in ops:
                key = (op["device"], op["id"])
                if op["action"] in ("replace", "delete"):
                    old = current[key]
                    changed_intervals.append(
                        (op["device"], old["start_ms"], old["end_ms"] - 1))
                if op["action"] in ("add", "replace"):
                    changed_intervals.append(
                        (op["device"], op["start_ms"], op["end_ms"] - 1))
            # 去重（区间可能因 replace 相同而重复）
            changed_intervals = sorted(set(changed_intervals))

            # 班表本身是否有实际改动（replace 可能写成完全相同的行）
            shift_changed = False
            for op in ops:
                if op["action"] in ("add", "delete"):
                    shift_changed = True
                    break
                old = current[(op["device"], op["id"])]
                if (old["team"], old["start_ms"], old["end_ms"],
                        old["priority"]) != (op["team"], op["start_ms"],
                                             op["end_ms"], op["priority"]):
                    shift_changed = True
                    break

            pairs = _affected_pairs(conn, changed_intervals)
            before = _snapshot_attributions(conn, pairs)

            cur = conn.execute(
                "INSERT INTO batches (batch_key, kind, created_at, note, "
                "timeline_sha256, manifest_sha256) VALUES (?,?,?,?,?,?)",
                (None, "revision", _utcnow(), note, None, None))
            batch_id = cur.lastrowid

            _apply_ops(conn, ops, batch_id)

            after = _recompute(conn, pairs, batch_id, before)

            changed = []
            for pair in pairs:
                b = before.get(pair)
                a = after.get(pair)
                if b != a:
                    changed.append({
                        "work_order": pair[0], "event": pair[1],
                        "before": b, "after": a})
            if not changed and not shift_changed:
                rollback_quiet(conn)
                result = {"status": "noop",
                          "reason": "correction changes neither shifts nor "
                                    "stored attributions",
                          "operations": _ops_public(ops),
                          "checked_events": len(pairs)}
            else:
                conn.execute("COMMIT")
                committed = True
                result = {"status": "corrected",
                          "database": db_path,
                          "batch_id": batch_id,
                          "operations": _ops_public(ops),
                          "shift_changed": shift_changed,
                          "affected_events": len(pairs),
                          "changed_attributions": changed,
                          "unchanged_but_overlapping": len(pairs) - len(changed),
                          "revision_sha256": sha256_bytes(rev_raw)}
        except BaseException:
            rollback_quiet(conn)
            raise
    finally:
        if not committed:
            rollback_quiet(conn)
        conn.close()
    return result


def _ops_public(ops):
    out = []
    for op in ops:
        d = {"action": op["action"], "device": op["device"],
             "shift_id": op["id"]}
        if op["action"] != "delete":
            d.update(team=op["team"], start_ms=op["start_ms"],
                     end_ms=op["end_ms"], priority=op["priority"])
        out.append(d)
    return out


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------

SHOW_SELECT = (
    "SELECT a.work_order, a.event, a.device, a.time_status, a.earliest, "
    "a.latest, a.teams_json, a.team_count, a.has_coverage, a.has_gap, "
    "a.has_conflict, a.max_priority, a.shift_ids_json, a.computed_batch "
    "FROM attributions a")


def _decode_attr(row):
    d = {k: row[k] for k in row.keys()}
    for key in ("teams_json", "shift_ids_json"):
        d[key[:-5]] = json.loads(d.pop(key))
    for key in ("has_coverage", "has_gap", "has_conflict"):
        if d.get(key) is not None:
            d[key] = bool(d[key])
    return d


def cmd_show(args):
    conn = open_db(args.db)
    try:
        sql = SHOW_SELECT + " WHERE 1=1"
        params = []
        if args.work_order:
            sql += " AND a.work_order = ?"
            params.append(args.work_order)
        if args.event:
            sql += " AND a.event = ?"
            params.append(args.event)
        if args.device:
            sql += " AND a.device = ?"
            params.append(args.device)
        sql += " ORDER BY a.device, a.work_order, a.event"
        rows = [_decode_attr(r) for r in conn.execute(sql, params)]
        return {"status": "ok", "rows": rows}
    finally:
        conn.close()


READ_ONLY_PREFIXES = ("select", "with", "pragma", "explain")


def cmd_sql(args):
    sql = args.sql.strip()
    head = sql.lower()
    if not any(head.startswith(p) for p in READ_ONLY_PREFIXES):
        raise Reject("not_read_only", "only read-only SQL is allowed here")
    uri = "file:%s?mode=ro" % os.path.abspath(args.db).replace("?", "%3f")
    conn = sqlite3.connect(uri, uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA query_only = ON")
        cur = conn.execute(sql, args.param or [])
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [list(r) for r in cur.fetchall()]
        return {"status": "ok", "columns": cols, "row_count": len(rows),
                "rows": rows}
    finally:
        conn.close()


def cmd_summary(args):
    conn = open_db(args.db)
    try:
        q = lambda sql, p=(): conn.execute(sql, p).fetchone()[0]
        data = {
            "status": "ok",
            "database": args.db,
            "batches": q("SELECT COUNT(*) FROM batches"),
            "latest_batch": q("SELECT MAX(batch_id) FROM batches"),
            "work_orders": {
                "ok": q("SELECT COUNT(*) FROM work_orders WHERE status='ok'"),
                "quarantined": q("SELECT COUNT(*) FROM work_orders "
                                 "WHERE status='quarantined'")},
            "events": {
                "anchored": q("SELECT COUNT(*) FROM events "
                              "WHERE time_status='anchored'"),
                "unlocatable": q("SELECT COUNT(*) FROM events "
                                 "WHERE time_status='unlocatable'")},
            "bindings": q("SELECT COUNT(*) FROM device_bindings"),
            "shifts": q("SELECT COUNT(*) FROM shifts"),
            "attributions": q("SELECT COUNT(*) FROM attributions"),
            "conflicted_events": q("SELECT COUNT(*) FROM attributions "
                                   "WHERE has_conflict=1"),
            "gap_events": q("SELECT COUNT(*) FROM attributions "
                            "WHERE has_gap=1"),
            "uncovered_events": q("SELECT COUNT(*) FROM attributions "
                                  "WHERE time_status='anchored' "
                                  "AND has_coverage=0"),
        }
        data["meta"] = {r[0]: r[1] for r in conn.execute("SELECT key, value FROM meta")}
        return data
    finally:
        conn.close()


def cmd_touch(args):
    """用 SQL 按“设备 + 时间”条件查找候选事件，不把整台设备历史载入程序。"""
    conn = open_db(args.db)
    try:
        sql = ("SELECT e.work_order, e.event, b.device, e.time_status, "
               "e.earliest, e.latest "
               "FROM events e JOIN device_bindings b "
               "  ON b.work_order = e.work_order AND b.event = e.event "
               "WHERE e.time_status='anchored' "
               "AND e.earliest <= ? AND e.latest >= ? AND b.device = ? "
               "ORDER BY e.work_order, e.event")
        rows = [dict(r) for r in conn.execute(sql, (args.hi, args.lo, args.device))]
        return {"status": "ok", "device": args.device,
                "window": [args.lo, args.hi], "events": rows}
    finally:
        conn.close()


def cmd_drift(args):
    """物化归属与“按当前班表重算”的差异（正常应为空）。"""
    conn = open_db(args.db)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT kind, work_order, event FROM v_attribution_drift "
            "ORDER BY kind, work_order, event")]
        return {"status": "ok" if not rows else "drift",
                "drift_count": len(rows), "drift": rows}
    finally:
        conn.close()


def cmd_shifts(args):
    conn = open_db(args.db)
    try:
        sql = ("SELECT device, shift_id, team, start_ms, end_ms, priority, "
               "updated_batch FROM shifts")
        params = []
        if args.device:
            sql += " WHERE device = ?"
            params.append(args.device)
        sql += " ORDER BY device, start_ms, shift_id"
        return {"status": "ok", "shifts": [dict(r) for r in conn.execute(sql, params)]}
    finally:
        conn.close()


def cmd_gaps(args):
    conn = open_db(args.db)
    try:
        sql = "SELECT device, gap_start, gap_end FROM v_gap_intervals"
        params = []
        if args.device:
            sql += " WHERE device = ?"
            params.append(args.device)
        sql += " ORDER BY device, gap_start"
        return {"status": "ok", "gaps": [dict(r) for r in conn.execute(sql, params)]}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _emit(obj, code=0):
    print(json.dumps(obj, ensure_ascii=False, indent=2))
    return code


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="质检事件班组归属层（SQLite，纯标准库）")
    sub = ap.add_subparsers(dest="command", required=True)

    b = sub.add_parser("build", help="首次建仓：读取 timeline run 输出 + 设备绑定清单")
    b.add_argument("--db", required=True, help="要创建的 SQLite 文件")
    b.add_argument("--timeline",
                   help="timeline_tool.py run 的输入规格 JSON（在本工具内调用 run）")
    b.add_argument("--run-output",
                   help="已有的 timeline_tool.py run 输出 JSON（与 --timeline 二选一）")
    b.add_argument("--manifest", required=True, help="设备绑定清单 JSON")
    b.add_argument("--shifts", action="append", default=[],
                   help="初始班表 JSON（可重复；也可写在 manifest.shifts 里）")
    b.add_argument("--note", help="批次备注")
    b.add_argument("--force", action="store_true", help="覆盖已存在的数据库")
    b.set_defaults(func=cmd_build)

    c = sub.add_parser("correct", help="一次更正若干班次（整批全有或全无）")
    c.add_argument("--db", required=True)
    c.add_argument("--revision", required=True, help="更正批次 JSON")
    c.add_argument("--expect-seq", type=int,
                   help="乐观并发：要求当前最大 batch_id 等于该值，否则整批拒绝")
    c.add_argument("--hold-ms", type=int, default=0,
                   help="测试用：在提交前保持事务打开若干毫秒，供读者验证隔离性")
    c.set_defaults(func=cmd_correct)

    s = sub.add_parser("show", help="读取归属结果")
    s.add_argument("--db", required=True)
    s.add_argument("--work-order")
    s.add_argument("--event")
    s.add_argument("--device")
    s.set_defaults(func=cmd_show)

    q = sub.add_parser("sql", help="执行只读 SQL")
    q.add_argument("--db", required=True)
    q.add_argument("--sql", required=True)
    q.add_argument("--param", action="append", type=_json_scalar,
                   help="位置参数（JSON 标量；可重复）")
    q.set_defaults(func=cmd_sql)

    m = sub.add_parser("summary", help="数据库摘要")
    m.add_argument("--db", required=True)
    m.set_defaults(func=cmd_summary)

    sh = sub.add_parser("shifts", help="列出班表")
    sh.add_argument("--db", required=True)
    sh.add_argument("--device")
    sh.set_defaults(func=cmd_shifts)

    d = sub.add_parser("drift", help="检查物化归属与当前班表是否一致")
    d.add_argument("--db", required=True)
    d.set_defaults(func=cmd_drift)

    t = sub.add_parser("touch", help="按设备 + 时间闭区间查找候选事件（SQL 侧）")
    t.add_argument("--db", required=True)
    t.add_argument("--device", required=True)
    t.add_argument("--lo", required=True, type=int)
    t.add_argument("--hi", required=True, type=int)
    t.set_defaults(func=cmd_touch)

    g = sub.add_parser("gaps", help="列出设备无班次覆盖的区间")
    g.add_argument("--db", required=True)
    g.add_argument("--device")
    g.set_defaults(func=cmd_gaps)

    args = ap.parse_args(argv)
    try:
        return _emit(args.func(args))
    except Reject as ex:
        return _emit({"status": "rejected", "errors": [ex.as_dict()]}, 2)
    except sqlite3.IntegrityError as ex:
        return _emit({"status": "rejected",
                      "errors": [{"code": "integrity_error",
                                  "message": str(ex)}]}, 2)
    except sqlite3.OperationalError as ex:
        return _emit({"status": "rejected",
                      "errors": [{"code": "sqlite_error",
                                  "message": str(ex)}]}, 2)


def _json_scalar(text):
    try:
        v = json.loads(text)
    except ValueError:
        return text
    return v


if __name__ == "__main__":
    sys.exit(main())
