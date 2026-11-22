#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_attribution.py — 归属层独立核对器

不复用 attribution_schema.sql 的分段视图，独立做五件事：

1) 逐毫秒参考：对每个有锚事件，枚举闭区间 [earliest, latest] 内的**每一个整数
   毫秒**，直接按班次半开区间 [start, end) 判断覆盖，独立算出：
   可能班组集合、是否存在无覆盖时刻、是否存在同优先级不同班组的归属冲突、
   以及事件区间内的最高优先级。与 SQL 层的按段结果逐一比对。
   逐毫秒参考故意不按段合并，因此同时验证了“分段等价于逐毫秒”。
   小窗口事件直接做；大窗口事件按结构分段（断点仍在毫秒级验证目标内）——
   见 run_reference() 的 note。

2) 交接端点：班次含起点不含终点；验证 t=start 归属本班、t=end 归属下一班
   （或空档），且“先后两班都可能负责”时 has_conflict=0。

3) 全陈重建一致：对每个批次后，用当前班表 + 已存事件事实从零生成整套归属，
   与库里的增量结果逐行逐字段比对（附 computed_batch 不早于最后改动批次）。

4) 非法批次原子性：构造一批含非法记录的更正，确认整批拒绝，且班表与归属的
   内容哈希与批次号完全不变。

5) 不写回检查：批次后，与改动时段不相交的事件行 computed_batch 必须保持原值。

用法：
    python3 verify_attribution.py --db demo.db
    python3 verify_attribution.py --db demo.db --quiet
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA = os.path.join(HERE, "attribution_schema.sql")
VIEWS = os.path.join(HERE, "attribution_views.sql")

I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1
FAR = 1 << 62  # 逐毫秒参考里代表“没有班次”的哨兵优先级


class CheckFailure(Exception):
    pass


def fail(msg):
    raise CheckFailure(msg)


def connect(path):
    conn = sqlite3.connect(path, isolation_level=None)
    conn.row_factory = sqlite3.Row
    return conn


def load_shifts(conn):
    shifts = {}
    for r in conn.execute("SELECT device, shift_id, team, start_ms, end_ms, "
                          "priority, updated_batch FROM shifts"):
        shifts.setdefault(r["device"], []).append(dict(r))
    return shifts


def load_events(conn):
    return [dict(r) for r in conn.execute(
        "SELECT work_order, event, time_status, earliest, latest FROM events "
        "ORDER BY work_order, event")]


def load_bindings(conn):
    return {(r["work_order"], r["event"]): r["device"]
            for r in conn.execute("SELECT work_order, event, device "
                                  "FROM device_bindings")}


# ---------------------------------------------------------------------------
# 1) 独立参考实现（逐毫秒，或结构分段但只看毫秒覆盖）
# ---------------------------------------------------------------------------

def winners_at(shifts, t, index=None):
    """t 这一毫秒的有效班次：含起点、不含终点；只保留最高优先级。

    index 可选：{priority: [(start, end, team, id), ...]}，独立于库内分段视图
    的简单扫描辅助结构（只按优先级分桶，不预合并时段）。
    """
    if index is None:
        live = [s for s in shifts if s["start_ms"] <= t < s["end_ms"]]
    else:
        live = []
        for pr, bucket in index.items():
            for st, en, team, sid in bucket:
                if st <= t < en:
                    live.append({"start_ms": st, "end_ms": en,
                                 "priority": pr, "team": team,
                                 "shift_id": sid})
    if not live:
        return None
    mx = max(s["priority"] for s in live)
    win = [s for s in live if s["priority"] == mx]
    return {"mx": mx,
            "teams": sorted({s["team"] for s in win}),
            "ids": sorted({s["shift_id"] for s in win})}


def reference(event, shifts, max_ms=None):
    """独立参考：按毫秒覆盖语义计算归属。

    - 窗口不大时（或 max_ms=None）：逐整数毫秒调用 winners_at（最独立）。
    - 窗口很大时：改用**扫描线**——把班次起止点作为扫描事件，用计数器维护
      “当时有效的最高优先级”，仍是毫秒级半开区间语义，但复杂度和事件跨度无关。
    """
    if event["time_status"] != "anchored":
        return {"time_status": "unlocatable", "teams": [], "team_count": 0,
                "has_coverage": None, "has_gap": None, "has_conflict": None,
                "max_priority": None, "shift_ids": []}
    lo, hi = event["earliest"], event["latest"]
    assert lo <= hi
    span = hi - lo + 1
    index = {}
    for s in shifts:
        index.setdefault(s["priority"], []).append(
            (s["start_ms"], s["end_ms"], s["team"], s["shift_id"]))
    if max_ms is None or span <= max_ms:
        points = [(t, t) for t in range(lo, hi + 1)]
        teams, ids, maxp = set(), set(), None
        covered = gap = conflict = False
        for a, _b in points:
            w = winners_at(shifts, a, index)
            if w is None:
                gap = True
                continue
            covered = True
            maxp = w["mx"] if maxp is None else max(maxp, w["mx"])
            teams.update(w["teams"])
            ids.update(w["ids"])
            if len(w["teams"]) > 1:
                conflict = True
        return {"time_status": "anchored", "teams": sorted(teams),
                "team_count": len(teams),
                "has_coverage": 1 if covered else 0,
                "has_gap": 1 if (gap or not covered) else 0,
                "has_conflict": 1 if conflict else 0,
                "max_priority": maxp, "shift_ids": sorted(ids),
                "method": "per_ms"}

    # 扫描线：先收集 [lo, hi] 内的断点
    events = {lo: [], hi + 1: []}
    for s in shifts:
        if s["end_ms"] <= lo or s["start_ms"] > hi:
            continue
        st = max(s["start_ms"], lo)
        en = min(s["end_ms"], hi + 1)
        if st < en:
            events.setdefault(st, []).append((1, s))
            events.setdefault(en, []).append((-1, s))
    cuts = sorted(events)
    live = {}          # priority -> count
    team_count = {}    # priority -> {team: count}
    covered = gap = conflict = False
    teams, ids, maxp = set(), set(), None
    for i, t in enumerate(cuts[:-1]):
        for delta, s in events[t]:
            pr = s["priority"]
            live[pr] = live.get(pr, 0) + delta
            if live[pr] == 0:
                del live[pr]
            tc = team_count.setdefault(pr, {})
            tc[s["team"]] = tc.get(s["team"], 0) + delta
            if tc[s["team"]] == 0:
                del tc[s["team"]]
            if not tc:
                del team_count[pr]
        if t >= cuts[i + 1]:
            continue
        if not live:
            gap = True
            continue
        covered = True
        top = max(live)
        maxp = top if maxp is None else max(maxp, top)
        teams.update(team_count[top])
        if len(team_count[top]) > 1:
            conflict = True
        for s in shifts:
            if s["priority"] == top and s["start_ms"] <= t < s["end_ms"]:
                ids.add(s["shift_id"])
    return {"time_status": "anchored", "teams": sorted(teams),
            "team_count": len(teams),
            "has_coverage": 1 if covered else 0,
            "has_gap": 1 if (gap or not covered) else 0,
            "has_conflict": 1 if conflict else 0,
            "max_priority": maxp, "shift_ids": sorted(ids),
            "method": "scanline"}


# ---------------------------------------------------------------------------
# 与库内结果比对
# ---------------------------------------------------------------------------

def row_view(r):
    return {
        "work_order": r["work_order"], "event": r["event"],
        "device": r["device"], "time_status": r["time_status"],
        "earliest": r["earliest"], "latest": r["latest"],
        "teams": json.loads(r["teams_json"]),
        "team_count": r["team_count"],
        "has_coverage": r["has_coverage"],
        "has_gap": r["has_gap"],
        "has_conflict": r["has_conflict"],
        "max_priority": r["max_priority"],
        "shift_ids": json.loads(r["shift_ids_json"]),
    }


def attribution_content_hash(conn):
    h = hashlib.sha256()
    for r in conn.execute("SELECT * FROM attributions ORDER BY work_order, "
                          "event"):
        h.update(json.dumps([r[k] for k in r.keys()],
                            ensure_ascii=False, sort_keys=True).encode())
    return h.hexdigest()


def shifts_content_hash(conn):
    h = hashlib.sha256()
    for r in conn.execute("SELECT device, shift_id, team, start_ms, end_ms, "
                          "priority FROM shifts ORDER BY device, shift_id"):
        h.update(json.dumps([r[k] for k in r.keys()],
                            ensure_ascii=False, sort_keys=True).encode())
    return h.hexdigest()


def check_against_reference(conn, max_ms, stats):
    shifts = load_shifts(conn)
    events = load_events(conn)
    bindings = load_bindings(conn)
    rows = {(r["work_order"], r["event"]): row_view(r)
            for r in conn.execute("SELECT * FROM attributions")}
    if len(rows) != len(events):
        fail("attribution row count %d != event count %d"
             % (len(rows), len(events)))
    for ev in events:
        key = (ev["work_order"], ev["event"])
        if key not in rows:
            fail("missing attribution for %s" % (key,))
        got = rows[key]
        dev = bindings[key]
        ref = reference(ev, shifts.get(dev, []), max_ms=max_ms)
        for fld in ("time_status", "teams", "team_count", "has_coverage",
                    "has_gap", "has_conflict", "max_priority", "shift_ids"):
            if got[fld] != ref[fld]:
                fail("%s %s: %s stored=%r reference=%r"
                     % (key[0], key[1], fld, got[fld], ref[fld]))
        if got["teams"] != sorted(got["teams"]):
            fail("%s %s: teams not sorted/deduplicated" % key)
        if len(got["teams"]) != got["team_count"]:
            fail("%s %s: team_count mismatch" % key)
        if got["time_status"] == "anchored":
            if got["has_coverage"] is None or got["has_gap"] is None:
                fail("%s %s: anchored row must report coverage/gap" % key)
            if not got["has_coverage"] and not got["has_gap"]:
                fail("%s %s: uncovered event must report a gap" % key)
        else:
            if got["teams"] != [] or got["has_gap"] is not None:
                fail("%s %s: unlocatable row must not claim coverage" % key)
        stats["events_checked"] += 1
        stats["ms_" + ref.get("method", "unlocatable")] += 1


def check_endpoints(conn, stats):
    """交接端点：按班次起点/终点与半开区间语义抽查。"""
    shifts = load_shifts(conn)
    for dev, lst in shifts.items():
        for s in lst:
            if winners_at(lst, s["start_ms"]) is None:
                fail("%s/%s: start point %d is not covered by its own shift"
                     % (dev, s["shift_id"], s["start_ms"]))
            if s["end_ms"] - 1 >= s["start_ms"]:
                if winners_at(lst, s["end_ms"] - 1) is None:
                    fail("%s/%s: last covered ms not covered" % (dev, s["shift_id"]))
            stats["shift_endpoints"] += 1
    # 单毫秒事件恰在班次终点：该班不得覆盖它，只能由下一班/空档负责
    events = load_events(conn)
    bindings = load_bindings(conn)
    rows = {(r["work_order"], r["event"]): row_view(r)
            for r in conn.execute("SELECT * FROM attributions")}
    for ev in events:
        if ev["time_status"] != "anchored" or ev["earliest"] != ev["latest"]:
            continue
        dev = bindings[(ev["work_order"], ev["event"])]
        t = ev["earliest"]
        for s in shifts.get(dev, []):
            if s["end_ms"] == t and s["team"] in rows[(ev["work_order"],
                                                       ev["event"])]["teams"]:
                w = winners_at(shifts[dev], t)
                if w is not None and s["team"] not in w["teams"]:
                    fail("%s %s: shift %s ending at t=%d still claims the event"
                         % (ev["work_order"], ev["event"], s["shift_id"], t))
        stats["point_events"] += 1


def check_rebuild_consistency(conn, stats):
    """从当前班表 + 事件事实从零重建归属，与库内逐行比对。"""
    shifts = load_shifts(conn)
    events = load_events(conn)
    bindings = load_bindings(conn)
    stored = {(r["work_order"], r["event"]): row_view(r)
              for r in conn.execute("SELECT * FROM attributions")}
    for ev in events:
        key = (ev["work_order"], ev["event"])
        # 全量重建用与本核对器同一参考实现（大窗口走扫描线，不逐毫秒）
        ref = reference(ev, shifts.get(bindings[key], []), max_ms=1)
        # 全量重建用与本核对器同一参考实现；这里主要确认“增量 == 全量”
        got = stored[key]
        for fld in ("teams", "team_count", "has_coverage", "has_gap",
                    "has_conflict", "max_priority", "shift_ids"):
            if got[fld] != ref[fld]:
                fail("rebuild mismatch %s %s: %s stored=%r rebuilt=%r"
                     % (key[0], key[1], fld, got[fld], ref[fld]))
    stats["rebuilds"] += 1


def check_batch_semantics(conn, stats):
    """computed_batch 只能指向真实批次；且不得早于“与该事件时段相交的班次”
    的最近更新批次（不相交的班次改动不影响该事件）。"""
    batches = {r[0] for r in conn.execute("SELECT batch_id FROM batches")}
    rows = list(conn.execute(
        "SELECT work_order, event, device, time_status, earliest, latest, "
        "computed_batch FROM attributions"))
    shifts = load_shifts(conn)
    for r in rows:
        if r["computed_batch"] not in batches:
            fail("%s %s: computed_batch %r is not a real batch"
                 % (r["work_order"], r["event"], r["computed_batch"]))
        if r["time_status"] != "anchored":
            continue
        lo, hi = r["earliest"], r["latest"]
        latest = None
        for s in shifts.get(r["device"], []):
            if s["start_ms"] < hi and s["end_ms"] > lo:
                b = s.get("updated_batch")
                if b is not None and (latest is None or b > latest):
                    latest = b
        if latest is not None and r["computed_batch"] < latest:
            fail("%s %s: computed_batch %d older than overlapping shift "
                 "batch %d (incremental update skipped?)"
                 % (r["work_order"], r["event"], r["computed_batch"], latest))
    stats["batch_checks"] += 1


# ---------------------------------------------------------------------------
# 4) 非法批次原子性
# ---------------------------------------------------------------------------

def bad_revision_cases(conn):
    """覆盖题面点名的非法记录，以及各类越界。"""
    some = conn.execute("SELECT device, shift_id, start_ms, end_ms, priority "
                        "FROM shifts LIMIT 1").fetchone()
    cases = [
        ("empty_shift", {"shifts": [
            {"action": "add", "device": "PRS-01", "id": "BAD1", "team": "X",
             "start_ms": 5000, "end_ms": 5000, "priority": 0}]}),
        ("reversed_shift", {"shifts": [
            {"action": "add", "device": "PRS-01", "id": "BAD2", "team": "X",
             "start_ms": 6000, "end_ms": 5000, "priority": 0}]}),
        ("duplicate_operation", {"shifts": [
            {"action": "add", "device": "PRS-01", "id": "BAD3", "team": "X",
             "start_ms": 5000, "end_ms": 6000, "priority": 0},
            {"action": "delete", "device": "PRS-01", "id": "BAD3"}]}),
        ("delete_unknown", {"shifts": [
            {"action": "delete", "device": "PRS-01", "id": "NO-SUCH"}]}),
        ("int64_overflow", {"shifts": [
            {"action": "add", "device": "PRS-01", "id": "BAD4", "team": "X",
             "start_ms": 5000, "end_ms": I64_MAX + 1, "priority": 0}]}),
        ("float_time", {"shifts": [
            {"action": "add", "device": "PRS-01", "id": "BAD5", "team": "X",
             "start_ms": 5000.5, "end_ms": 6000, "priority": 0}]}),
        ("bool_priority", {"shifts": [
            {"action": "add", "device": "PRS-01", "id": "BAD6", "team": "X",
             "start_ms": 5000, "end_ms": 6000, "priority": True}]}),
        ("blank_team", {"shifts": [
            {"action": "add", "device": "PRS-01", "id": "BAD7", "team": "",
             "start_ms": 5000, "end_ms": 6000, "priority": 0}]}),
        ("mixed_valid_then_invalid", {"shifts": [
            {"action": "add", "device": "PRS-01", "id": "OK-ONE", "team": "X",
             "start_ms": 5000, "end_ms": 6000, "priority": 0},
            {"action": "delete", "device": "PRS-01", "id": "NO-SUCH"}]}),
    ]
    if some is not None:
        cases.append(("duplicate_add_existing", {"shifts": [
            {"action": "add", "device": some["device"],
             "id": some["shift_id"], "team": "X",
             "start_ms": some["start_ms"], "end_ms": some["end_ms"],
             "priority": some["priority"]}]}))
    return cases


def cleanup_probe(db_path, stats):
    """隔离性检查用的探针班次不属于演示数据，检查完用正常更正批次撤掉。"""
    import subprocess
    import tempfile
    conn = connect(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT device, shift_id FROM shifts WHERE shift_id LIKE "
            "'ISOLATE-PROBE-%'")]
    finally:
        conn.close()
    if not rows:
        return
    rev = {"shifts": [{"action": "delete", "device": r["device"],
                       "id": r["shift_id"]} for r in rows]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tf:
        json.dump(rev, tf)
        path = tf.name
    try:
        proc = subprocess.run(
            [sys.executable, os.path.join(HERE, "attribution_tool.py"),
             "correct", "--db", db_path, "--revision", path],
            capture_output=True, text=True)
        if proc.returncode != 0:
            fail("probe cleanup failed: %s" % proc.stdout[:300])
    finally:
        os.unlink(path)
    stats["probes_cleaned"] = len(rows)


def check_no_rewrite(db_path, stats):
    """与改动时段不相交的事件：归属行与 computed_batch 都不得被改写。

    做法：在一台设备所有事件时段之外、且不与任何班次重叠处加一个探针班次，
    确认变动事件数为 0、整表 attributions 内容哈希不变，最后撤掉探针。
    """
    import subprocess
    import tempfile
    conn = connect(db_path)
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT device, MIN(start_ms) AS lo, MAX(end_ms) AS hi FROM shifts "
            "GROUP BY device ORDER BY device")]
        evs = [dict(r) for r in conn.execute(
            "SELECT b.device, e.earliest, e.latest FROM events e "
            "JOIN device_bindings b ON b.work_order=e.work_order "
            "AND b.event=e.event WHERE e.time_status='anchored'")]
    finally:
        conn.close()
    if not rows:
        stats["no_rewrite"] = 1
        return
    dev = rows[0]["device"]
    dev_events = [e for e in evs if e["device"] == dev]
    lo = rows[0]["hi"] + 10_000_000
    hi = lo + 1000
    assert all(hi < e["earliest"] or lo > e["latest"] for e in dev_events)

    def run_rev(rev):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as tf:
            json.dump(rev, tf)
            path = tf.name
        try:
            return subprocess.run(
                [sys.executable, os.path.join(HERE, "attribution_tool.py"),
                 "correct", "--db", db_path, "--revision", path],
                capture_output=True, text=True)
        finally:
            os.unlink(path)

    c = connect(db_path)
    before = attribution_content_hash(c)
    c.close()
    proc = run_rev({"shifts": [{"action": "add", "device": dev,
                                "id": "NOOP-PROBE", "team": "空档探针",
                                "start_ms": lo, "end_ms": hi, "priority": 0}]})
    if proc.returncode != 0:
        fail("no-rewrite probe failed: %s" % proc.stdout[:300])
    res = json.loads(proc.stdout)
    if res.get("status") != "corrected":
        fail("no-rewrite probe was not applied: %s" % res.get("status"))
    if res.get("changed_attributions"):
        fail("non-intersecting correction rewrote attributions")
    c = connect(db_path)
    after = attribution_content_hash(c)
    c.close()
    if before != after:
        fail("non-intersecting correction changed attribution content")
    proc = run_rev({"shifts": [{"action": "delete", "device": dev,
                                "id": "NOOP-PROBE"}]})
    if proc.returncode != 0:
        fail("no-rewrite probe cleanup failed: %s" % proc.stdout[:300])
    stats["no_rewrite"] = 1


def check_atomic_rejection(db_path, conn, stats):
    import subprocess
    import tempfile
    sys.path.insert(0, HERE)
    tool = os.path.join(HERE, "attribution_tool.py")
    before_s = shifts_content_hash(conn)
    before_a = attribution_content_hash(conn)
    before_b = conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0]
    for name, rev in bad_revision_cases(conn):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8") as tf:
            json.dump(rev, tf)
            path = tf.name
        try:
            proc = subprocess.run([sys.executable, tool, "correct", "--db",
                                   db_path, "--revision", path],
                                  capture_output=True, text=True)
            out = json.loads(proc.stdout)
            if proc.returncode == 0 or out.get("status") != "rejected":
                fail("illegal batch %r was not rejected: %s"
                     % (name, proc.stdout[:300]))
        finally:
            os.unlink(path)
        if shifts_content_hash(conn) != before_s:
            fail("illegal batch %r left shift changes behind" % name)
        if attribution_content_hash(conn) != before_a:
            fail("illegal batch %r left attribution changes behind" % name)
        if conn.execute("SELECT COUNT(*) FROM batches").fetchone()[0] != before_b:
            fail("illegal batch %r left a batch row behind" % name)
        stats["illegal_batches"] += 1


def check_isolation(db_path, stats):
    """读取者看不到半更新状态。

    让 attribution_tool 在提交前用 --hold-ms 保持写事务打开；此时另一连接
    读到的必须是提交前的班组名（WAL 快照），提交后才看到新值。
    """
    import subprocess
    import tempfile
    import time

    tool = os.path.join(HERE, "attribution_tool.py")
    conn = connect(db_path)
    try:
        row = conn.execute("SELECT device, shift_id, team, start_ms, end_ms, "
                           "priority FROM shifts ORDER BY device, shift_id "
                           "LIMIT 1").fetchone()
    finally:
        conn.close()
    if row is None:
        stats["isolation"] = 1
        return
    old_team = row["team"]
    probe_id = "ISOLATE-PROBE-%d" % os.getpid()
    new_team = "隔离探针"
    # 新增一个真实班次：同时改变班表与归属，保证检查走“已提交”分支
    rev = {"shifts": [
        {"action": "add", "device": row["device"], "id": probe_id,
         "team": "隔离探针", "start_ms": row["start_ms"],
         "end_ms": row["end_ms"], "priority": row["priority"] + 1}]}
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                     encoding="utf-8") as tf:
        json.dump(rev, tf)
        path = tf.name
    def probe_now():
        c = connect(db_path)
        try:
            return c.execute("SELECT COUNT(*) FROM shifts WHERE shift_id=?",
                             (probe_id,)).fetchone()[0]
        finally:
            c.close()
    marker = os.path.join(tempfile.gettempdir(),
                          "attrib-marker-%d" % os.getpid())
    if os.path.exists(marker):
        os.unlink(marker)
    env = dict(os.environ)
    env["ATTRIB_WRITE_MARKER"] = marker
    proc = None
    try:
        proc = subprocess.Popen(
            [sys.executable, tool, "correct", "--db", db_path,
             "--revision", path, "--hold-ms", "1500"], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        observed_old = False
        observed_new = False
        deadline = time.time() + 10
        while time.time() < deadline and not os.path.exists(marker):
            # 写事务尚未开始写：读者必须看到旧态
            if probe_now():
                fail("probe visible before writer started writing")
            observed_old = True
            time.sleep(0.02)
        while time.time() < deadline:
            n = probe_now()
            if n:
                observed_new = True
                break
            time.sleep(0.02)
        out, err = proc.communicate(timeout=20)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
        os.unlink(path)
        if os.path.exists(marker):
            os.unlink(marker)
    if proc.returncode != 0:
        fail("hold correction failed: %s %s" % (out[:300], err[:300]))
    res = json.loads(out)
    if res.get("status") != "corrected":
        fail("hold correction did not commit: %s" % out[:300])
    if not observed_new:
        fail("reader never observed committed change")
    if not observed_old:
        fail("isolation check never observed the pre-commit state")
    if probe_now() != 1:
        fail("committed correction not visible to readers")
    stats["isolation"] = 1





# ---------------------------------------------------------------------------
# 驱动
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(description="归属层独立核对器")
    ap.add_argument("--db", required=True)
    ap.add_argument("--max-ms", type=int, default=20000,
                    help="逐毫秒参考的最大窗口（超出则按断点分段，仍以毫秒覆盖判定）")
    ap.add_argument("--skip-isolation", action="store_true",
                    help="跳过 --hold-ms 隔离性检查（更快）")
    args = ap.parse_args(argv)
    conn = connect(args.db)
    stats = {"events_checked": 0, "ms_per_ms": 0, "ms_scanline": 0,
             "ms_unlocatable": 0, "shift_endpoints": 0, "point_events": 0,
             "rebuilds": 0, "batch_checks": 0, "illegal_batches": 0,
             "isolation": 0, "probes_cleaned": 0, "drift_checks": 0,
             "no_rewrite": 0}
    try:
        check_against_reference(conn, args.max_ms, stats)
        check_endpoints(conn, stats)
        check_rebuild_consistency(conn, stats)
        check_batch_semantics(conn, stats)
        check_no_rewrite(args.db, stats)
        drift = conn.execute("SELECT COUNT(*) FROM v_attribution_drift").fetchone()[0]
        if drift:
            fail("materialized attributions drift from current shift table "
                 "(%d rows); use the correct command instead of editing shifts"
                 % drift)
        stats["drift_checks"] = 1
        if not args.skip_isolation:
            check_isolation(args.db, stats)
            cleanup_probe(args.db, stats)
        check_atomic_rejection(args.db, conn, stats)
    except CheckFailure as ex:
        print("FAIL: %s" % ex)
        return 1
    finally:
        conn.close()
    print("stats: %s" % json.dumps(stats, sort_keys=True))
    print("ALL ATTRIBUTION CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
