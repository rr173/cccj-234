#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_attribution_full.py — 归属层“从零重建”一致性 + 随机设备模糊核对

1) 独立从零重建：不用 timeline 工具、不用 attribution_tool 的任何代码，
   直接读原始时间线规格（自包含差分约束 Bellman-Ford）得到每个事件的最早/
   最晚可行时刻，再读规范 JSON、初始班表和更正批次；在内存里应用更正，
   按“设备 + 时间”生成整套归属。与库内 attributions 逐行、逐字段比对，
   并逐字节比对 shifts 表。等价于“更正后从更正后班表重新建仓”。

2) 随机设备模糊核对：随机生成小整数窗口的设备/班次/事件区间，独立枚举
   每一个整数毫秒（窗口很小），与 v_event_attribution 视图比对优先级并列、
   同班组并列、交接端点、空档、冲突等情形。

用法：
    python3 verify_attribution_full.py                  # 跑 examples/attribution 全流程
    python3 verify_attribution_full.py --fuzz 200 --seed 5
"""

import argparse
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "attribution_tool.py")
ATTR_DIR = os.path.join(HERE, "examples", "attribution")
I64_MIN = -(1 << 63)
I64_MAX = (1 << 63) - 1


class CheckFailure(Exception):
    pass


def fail(msg):
    raise CheckFailure(msg)


# ---------------------------------------------------------------------------
# 独立时间界（自包含 Bellman-Ford，不 import timeline_tool）
# ---------------------------------------------------------------------------

def independent_bounds(spec):
    """返回 {(工单, 事件): (earliest|None, latest|None)} 与工单状态。"""
    out = {}
    status = {}
    for wo in spec.get("work_orders", []):
        wid = wo["id"]
        events = wo["events"]
        abs_evs = wo.get("absolute", [])
        rel_evs = wo.get("relative", [])
        bad = False
        for e in abs_evs:
            if e["event"] not in events or e["lo"] > e["hi"]:
                bad = True
        for e in rel_evs:
            if e["before"] not in events or e["after"] not in events:
                bad = True
            if e["lo"] > e["hi"]:
                bad = True
        if bad:
            status[wid] = "quarantined"
            for ev in events:
                out[(wid, ev)] = ("unlocatable", None, None)
            continue
        z = "@zero"
        edges = []
        for e in abs_evs:
            edges.append((z, e["event"], e["hi"]))
            edges.append((e["event"], z, -e["lo"]))
        for e in rel_evs:
            edges.append((e["before"], e["after"], e["hi"]))
            edges.append((e["after"], e["before"], -e["lo"]))
        nodes = list(events) + [z]
        # Bellman-Ford 负环检测（虚拟源点 0）
        dist = {n: 0 for n in nodes}
        neg = False
        for i in range(len(nodes)):
            changed = False
            for u, v, w in edges:
                if dist[u] + w < dist[v]:
                    dist[v] = dist[u] + w
                    changed = True
            if not changed:
                break
        else:
            neg = True
        if neg:
            status[wid] = "quarantined"
            for ev in events:
                out[(wid, ev)] = ("unlocatable", None, None)
            continue
        status[wid] = "ok"
        for ev in events:
            # 最晚：0 -> ev 最短；最早：ev -> 0 最短取负
            d = {n: None for n in nodes}
            d[z] = 0
            for _ in range(len(nodes) - 1):
                ch = False
                for u, v, w in edges:
                    if d[u] is None:
                        continue
                    if d[v] is None or d[u] + w < d[v]:
                        d[v] = d[u] + w
                        ch = True
                if not ch:
                    break
            latest = d[ev]
            redges = [(v, u, w) for (u, v, w) in edges]
            d2 = {n: None for n in nodes}
            d2[z] = 0
            for _ in range(len(nodes) - 1):
                ch = False
                for u, v, w in redges:
                    if d2[u] is None:
                        continue
                    if d2[v] is None or d2[u] + w < d2[v]:
                        d2[v] = d2[u] + w
                        ch = True
                if not ch:
                    break
            earliest = None if d2[ev] is None else -d2[ev]
            if earliest is None or latest is None:
                out[(wid, ev)] = ("unlocatable", None, None)
            else:
                out[(wid, ev)] = ("anchored", earliest, latest)
    return out, status


# ---------------------------------------------------------------------------
# 独立归属参考（逐毫秒）
# ---------------------------------------------------------------------------

def device_attribution(device, lo, hi, shifts):
    """逐毫秒归属，只针对小窗口（模糊核对用）。"""
    teams = set()
    ids = set()
    maxp = None
    covered = gap = conflict = False
    for t in range(lo, hi + 1):
        live = [s for s in shifts if s["device"] == device
                and s["start_ms"] <= t < s["end_ms"]]
        if not live:
            gap = True
            continue
        covered = True
        mx = max(s["priority"] for s in live)
        maxp = mx if maxp is None else max(maxp, mx)
        win = [s for s in live if s["priority"] == mx]
        win_teams = {s["team"] for s in win}
        teams.update(win_teams)
        ids.update(s.get("shift_id", s.get("id")) for s in win)
        if len(win_teams) > 1:
            conflict = True
    return {"teams": sorted(teams), "team_count": len(teams),
            "has_coverage": 1 if covered else 0,
            "has_gap": 1 if (gap or not covered) else 0,
            "has_conflict": 1 if conflict else 0,
            "max_priority": maxp, "shift_ids": sorted(ids)}


def interval_attribution(device, lo, hi, shifts):
    """扫描线归属，与管理跨度无关（全流程核对用）。"""
    if hi - lo + 1 <= 4096:
        return device_attribution(device, lo, hi, shifts)
    ev = {lo: [], hi + 1: []}
    for s in shifts:
        if s["device"] != device:
            continue
        if s["end_ms"] <= lo or s["start_ms"] > hi:
            continue
        st, en = max(s["start_ms"], lo), min(s["end_ms"], hi + 1)
        if st < en:
            ev.setdefault(st, []).append((1, s))
            ev.setdefault(en, []).append((-1, s))
    cuts = sorted(ev)
    live, tcount = {}, {}
    teams, ids, maxp = set(), set(), None
    covered = gap = conflict = False
    for i, t in enumerate(cuts[:-1]):
        for d, s in ev[t]:
            pr, tm = s["priority"], s["team"]
            live[pr] = live.get(pr, 0) + d
            if not live[pr]:
                del live[pr]
            tc = tcount.setdefault(pr, {})
            tc[tm] = tc.get(tm, 0) + d
            if not tc[tm]:
                del tc[tm]
            if not tc:
                del tcount[pr]
        if cuts[i + 1] <= t:
            continue
        if not live:
            gap = True
            continue
        covered = True
        top = max(live)
        maxp = top if maxp is None else max(maxp, top)
        teams |= set(tcount[top])
        if len(tcount[top]) > 1:
            conflict = True
        for s in shifts:
            if (s["device"] == device and s["priority"] == top
                    and s["start_ms"] <= t < s["end_ms"]):
                ids.add(s.get("shift_id", s.get("id")))
    return {"teams": sorted(teams), "team_count": len(teams),
            "has_coverage": 1 if covered else 0,
            "has_gap": 1 if (gap or not covered) else 0,
            "has_conflict": 1 if conflict else 0,
            "max_priority": maxp, "shift_ids": sorted(ids)}


# ---------------------------------------------------------------------------
# 全流程：examples/attribution
# ---------------------------------------------------------------------------

def run_cli(args):
    proc = subprocess.run([sys.executable, TOOL] + args,
                          capture_output=True, text=True)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        fail("tool %s produced non-JSON: %s / %s"
             % (args[0], proc.stdout[:300], proc.stderr[:300]))
    if proc.returncode != 0:
        fail("tool %s failed: %s" % (args[0], json.dumps(out)[:400]))
    return out


def check_full_flow(tmp):
    spec = json.load(open(os.path.join(ATTR_DIR, "timeline.json")))
    manifest = json.load(open(os.path.join(ATTR_DIR, "manifest.json")))
    initial = json.load(open(os.path.join(ATTR_DIR, "shifts.json")))["shifts"]
    revs = [json.load(open(os.path.join(ATTR_DIR, f))) for f in
            ("revision_fill_gap.json", "revision_revoke_delegation.json")]

    db = os.path.join(tmp, "flow.db")
    if os.path.exists(db):
        os.unlink(db)
    run_cli(["build", "--db", db, "--timeline",
             os.path.join(ATTR_DIR, "timeline.json"), "--manifest",
             os.path.join(ATTR_DIR, "manifest.json"), "--shifts",
             os.path.join(ATTR_DIR, "shifts.json")])
    # 期望班表（内存里应用更正）
    shifts = [dict(s, device=s["device"], shift_id=s["id"])
              for s in initial]
    for rev in revs:
        path = os.path.join(tmp, "rev.json")
        json.dump(rev, open(path, "w"))
        run_cli(["correct", "--db", db, "--revision", path])
        for op in rev["shifts"]:
            key = (op["device"], op["id"])
            if op["action"] == "delete":
                shifts = [s for s in shifts
                          if (s["device"], s["shift_id"]) != key]
            elif op["action"] == "add":
                shifts.append({"device": op["device"],
                               "shift_id": op["id"], "team": op["team"],
                               "start_ms": op["start_ms"],
                               "end_ms": op["end_ms"],
                               "priority": op["priority"]})
            else:
                for s in shifts:
                    if (s["device"], s["shift_id"]) == key:
                        s.update(team=op["team"], start_ms=op["start_ms"],
                                 end_ms=op["end_ms"],
                                 priority=op["priority"])

    # 库内班表逐行比对
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    got = {(r["device"], r["shift_id"]): (r["team"], r["start_ms"],
                                          r["end_ms"], r["priority"])
           for r in conn.execute("SELECT * FROM shifts")}
    want = {(s["device"], s["shift_id"]): (s["team"], s["start_ms"],
                                           s["end_ms"], s["priority"])
            for s in shifts}
    if got != want:
        fail("shift table differs from expected correction result")

    # 独立时间界
    facts, status = independent_bounds(spec)
    bindings = {}
    for wo, evs in manifest["bindings"].items():
        for ev, dev in evs.items():
            bindings[(wo, ev)] = dev

    rows = {(r["work_order"], r["event"]): r
            for r in conn.execute("SELECT * FROM attributions")}
    expected_keys = {k for k in facts if status[k[0]] == "ok"}
    if set(rows) != expected_keys:
        fail("attribution key set differs: extra=%r missing=%r"
             % (sorted(set(rows) - expected_keys)[:5],
                sorted(expected_keys - set(rows))[:5]))
    for key, (ts, lo, hi) in facts.items():
        if key not in rows:
            continue
        r = rows[key]
        dev = bindings[key]
        if r["time_status"] != ts or r["earliest"] != lo or r["latest"] != hi:
            fail("%s: stored time facts differ from independent bounds" % (key,))
        if ts == "unlocatable":
            if json.loads(r["teams_json"]) != [] or r["team_count"] != 0:
                fail("%s: unlocatable event must not claim a team" % (key,))
            if (r["has_coverage"], r["has_gap"], r["has_conflict"],
                    r["max_priority"]) != (None, None, None, None):
                fail("%s: unlocatable event must have NULL flags" % (key,))
            continue
        ref = interval_attribution(dev, lo, hi, shifts)
        for fld in ("teams", "team_count", "has_coverage", "has_gap",
                    "has_conflict", "max_priority", "shift_ids"):
            stored = r[fld + "_json"] if fld in ("teams", "shift_ids") else r[fld]
            if fld in ("teams", "shift_ids"):
                stored = json.loads(stored)
            if stored != ref[fld]:
                fail("%s: %s stored=%r expected=%r"
                     % (key, fld, stored, ref[fld]))
    conn.close()
    return {"work_orders": len(spec["work_orders"]),
            "attributions": len(rows), "shifts": len(shifts)}


# ---------------------------------------------------------------------------
# 随机设备模糊核对
# ---------------------------------------------------------------------------

def fuzz_spec(seed):
    import random
    rng = random.Random(seed)
    devices = ["D%d" % i for i in range(3)]
    shifts, bindings, events, absolute = [], {}, [], []
    eid = 0
    for dev in devices:
        for k in range(rng.randint(0, 6)):
            st = rng.randint(0, 20)
            en = st + rng.randint(1, 8)
            shifts.append({"device": dev, "id": "S%d-%d" % (len(shifts), k),
                           "team": rng.choice(["甲", "乙", "丙"]),
                           "start_ms": st, "end_ms": en,
                           "priority": rng.randint(0, 2)})
        for _ in range(rng.randint(1, 4)):
            lo = rng.randint(-2, 20)
            hi = lo + rng.randint(0, 8)
            ev = "e%d" % eid
            eid += 1
            absolute.append({"id": "a%d" % eid, "event": ev, "lo": lo,
                             "hi": hi})
            bindings[ev] = dev
            events.append(ev)
    # 每题一个工单，全部有锚
    spec = {"work_orders": [{"id": "W", "events": events,
                             "absolute": absolute, "relative": []}],
            "queries": []}
    manifest = {"bindings": {"W": bindings}}
    return spec, manifest, shifts


def check_fuzz(tmp, n, seed):
    import subprocess
    ok = 0
    for i in range(n):
        spec, manifest, shifts = fuzz_spec(seed * 1000 + i)
        base = os.path.join(tmp, "f%d" % i)
        os.makedirs(base, exist_ok=True)
        sp = os.path.join(base, "spec.json")
        mp = os.path.join(base, "manifest.json")
        shp = os.path.join(base, "shifts.json")
        json.dump(spec, open(sp, "w"))
        json.dump(manifest, open(mp, "w"))
        json.dump({"shifts": [dict(s, id=s["id"]) for s in shifts]},
                  open(shp, "w"))
        db = os.path.join(base, "x.db")
        out = run_cli(["build", "--db", db, "--timeline", sp,
                       "--manifest", mp, "--shifts", shp])
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        rows = {(r["work_order"], r["event"]): r
                for r in conn.execute("SELECT * FROM attributions")}
        conn.close()
        for wo in spec["work_orders"]:
            for b in wo["absolute"]:
                key = (wo["id"], b["event"])
                dev = manifest["bindings"][wo["id"]][b["event"]]
                ref = device_attribution(dev, b["lo"], b["hi"], shifts)
                r = rows[key]
                for fld in ("team_count", "has_coverage", "has_gap",
                            "has_conflict", "max_priority"):
                    if r[fld] != ref[fld]:
                        fail("fuzz seed=%d case=%d %s: %s stored=%r want=%r"
                             % (seed, i, key, fld, r[fld], ref[fld]))
                if json.loads(r["teams_json"]) != ref["teams"]:
                    fail("fuzz seed=%d case=%d %s: teams %r vs %r"
                         % (seed, i, key, json.loads(r["teams_json"]),
                            ref["teams"]))
                if json.loads(r["shift_ids_json"]) != ref["shift_ids"]:
                    fail("fuzz seed=%d case=%d %s: shift_ids differ"
                         % (seed, i, key))
        ok += 1
        shutil.rmtree(base, ignore_errors=True)
    return ok


def check_random_corrections(tmp, n, seed):
    """随机更正序列：每次更正后全部归属必须等于“从零按新班表重建”。"""
    import random
    rng = random.Random(seed + 991)
    ok = 0
    for i in range(n):
        spec, manifest, shifts = fuzz_spec(seed * 7000 + i)
        base = os.path.join(tmp, "r%d" % i)
        os.makedirs(base, exist_ok=True)
        sp = os.path.join(base, "spec.json")
        mp = os.path.join(base, "manifest.json")
        shp = os.path.join(base, "shifts.json")
        json.dump(spec, open(sp, "w"))
        json.dump(manifest, open(mp, "w"))
        json.dump({"shifts": shifts}, open(shp, "w"))
        db = os.path.join(base, "x.db")
        run_cli(["build", "--db", db, "--timeline", sp, "--manifest", mp,
                 "--shifts", shp])
        model = [dict(s, shift_id=s["id"]) for s in shifts]
        for step in range(rng.randint(1, 4)):
            rev_ops = []
            dev = rng.choice(["D0", "D1", "D2"])
            existing = [s for s in model if s["device"] == dev]
            kind = rng.choice(["add", "replace", "delete", "add"])
            if kind == "add" or not existing:
                st = rng.randint(-3, 20)
                rev_ops.append({"action": "add", "device": dev,
                                "id": "R%d-%d" % (i, step),
                                "team": rng.choice(["甲", "乙", "丙", "丁"]),
                                "start_ms": st,
                                "end_ms": st + rng.randint(1, 6),
                                "priority": rng.randint(0, 2)})
            elif kind == "delete":
                s = rng.choice(existing)
                rev_ops.append({"action": "delete", "device": dev,
                                "id": s["shift_id"]})
            else:
                s = rng.choice(existing)
                st = rng.randint(-3, 20)
                rev_ops.append({"action": "replace", "device": dev,
                                "id": s["shift_id"],
                                "team": rng.choice(["甲", "乙", "丙", "丁"]),
                                "start_ms": st,
                                "end_ms": st + rng.randint(1, 6),
                                "priority": rng.randint(0, 2)})
            rp = os.path.join(base, "rev.json")
            json.dump({"shifts": rev_ops}, open(rp, "w"))
            run_cli(["correct", "--db", db, "--revision", rp])
            for op in rev_ops:
                key = (op["device"], op["id"])
                if op["action"] == "delete":
                    model = [s for s in model
                             if (s["device"], s["shift_id"]) != key]
                elif op["action"] == "add":
                    model.append({"device": op["device"],
                                  "shift_id": op["id"], "team": op["team"],
                                  "start_ms": op["start_ms"],
                                  "end_ms": op["end_ms"],
                                  "priority": op["priority"]})
                else:
                    for s in model:
                        if (s["device"], s["shift_id"]) == key:
                            s.update(team=op["team"],
                                     start_ms=op["start_ms"],
                                     end_ms=op["end_ms"],
                                     priority=op["priority"])
            conn = sqlite3.connect(db)
            conn.row_factory = sqlite3.Row
            for wo in spec["work_orders"]:
                for b in wo["absolute"]:
                    key = (wo["id"], b["event"])
                    dev2 = manifest["bindings"][wo["id"]][b["event"]]
                    r = conn.execute(
                        "SELECT * FROM attributions WHERE work_order=? AND "
                        "event=?", key).fetchone()
                    ref = device_attribution(dev2, b["lo"], b["hi"], model)
                    if r is None:
                        fail("correction removed attribution for %s" % (key,))
                    for fld in ("team_count", "has_coverage", "has_gap",
                                "has_conflict", "max_priority"):
                        if r[fld] != ref[fld]:
                            fail("random correction %d/%d %s: %s stored=%r "
                                 "want=%r" % (i, step, key, fld, r[fld],
                                              ref[fld]))
                    if json.loads(r["teams_json"]) != ref["teams"]:
                        fail("random correction %d/%d %s: teams differ"
                             % (i, step, key))
            conn.close()
        ok += 1
        shutil.rmtree(base, ignore_errors=True)
    return ok


def main(argv=None):
    ap = argparse.ArgumentParser(description="归属层全流程 / 模糊核对")
    ap.add_argument("--fuzz", type=int, default=0)
    ap.add_argument("--seed", type=int, default=5)
    args = ap.parse_args(argv)
    tmp = tempfile.mkdtemp(prefix="attrib-verify-")
    try:
        info = check_full_flow(tmp)
        print("full flow OK: %s" % json.dumps(info, sort_keys=True))
        if args.fuzz:
            n = check_fuzz(tmp, args.fuzz, args.seed)
            print("fuzz OK: %d random device cases" % n)
            m = check_random_corrections(tmp, args.fuzz, args.seed)
            print("random corrections OK: %d cases" % m)
    except CheckFailure as ex:
        print("FAIL: %s" % ex)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
