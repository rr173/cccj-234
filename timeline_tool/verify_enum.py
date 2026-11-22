#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_enum.py — timeline_tool.py 的独立核对器

不复用求解器的约束传播逻辑，独立做四件事：

1) 小整数窗口全枚举：对每个 ok 工单，在可证明足够大的整数窗口内枚举全部
   赋值，独立算出每个事件的最早/最晚可行时刻、每个被查询事件对的三种关系
   是否存在，与求解器输出逐一比对。
   窗口取法（安全外扩）：R = 全部相对证据 |lo|+|hi| 之和；
   有锚片事件落在 [min(abs.lo)-R, max(abs.hi)+R]；无锚片平移不变，
   固定片代表元为 0，其余落在 [-R, R]。跨分量事件对若不全有锚，
   平移自由 => 三种关系必然都存在（独立论证，不经枚举）。

2) 证书重放：把求解器给出的每条路径证据（最早/最晚推导链）和矛盾证书
   （负环）逐步对照原始证据重新推导，确认端点/矛盾确实由这些证据推出。

3) 见证复核：求解器为每个"存在"的关系交付的赋值，逐条对照原始证据验证，
   并确认该赋值确实实现了所声称的关系。

4) 分类核对：坏引用 / 逆置区间 / 合法但不可满足 三类隔离原因独立重判。

用法：
    python3 verify_enum.py examples/case5_small.json [更多规格.json ...]
    python3 verify_enum.py --fuzz 120 --seed 7     # 随机小工单模糊核对
    python3 verify_enum.py examples/*.json --fuzz 120
"""

import argparse
import itertools
import json
import os
import random
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "timeline_tool.py")
ZERO = "@zero"


class CheckFailure(Exception):
    pass


def fail(msg):
    raise CheckFailure(msg)


# ---------------------------------------------------------------------------
# 独立重读原始证据
# ---------------------------------------------------------------------------

def index_evidence(wo):
    evs = {}
    for i, e in enumerate(wo.get("absolute", [])):
        v = e.get("id")
        evs["abs%d" % (i + 1) if v is None else str(v)] = ("abs", e)
    for i, e in enumerate(wo.get("relative", [])):
        v = e.get("id")
        evs["rel%d" % (i + 1) if v is None else str(v)] = ("rel", e)
    return evs


def derive_step(ev_kind, ev, step_kind):
    """某条证据在某个方向上贡献的半约束 (u, v, w)：t_v - t_u <= w。"""
    if step_kind == "abs_hi" and ev_kind == "abs":
        return (ZERO, ev["event"], ev["hi"])
    if step_kind == "abs_lo" and ev_kind == "abs":
        return (ev["event"], ZERO, -ev["lo"])
    if step_kind == "rel_hi" and ev_kind == "rel":
        return (ev["before"], ev["after"], ev["hi"])
    if step_kind == "rel_lo" and ev_kind == "rel":
        return (ev["after"], ev["before"], -ev["lo"])
    return None


def wo_components(wo):
    events = wo["events"]
    parent = {e: e for e in events}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for e in wo.get("relative", []):
        ra, rb = find(e["before"]), find(e["after"])
        if ra != rb:
            parent[ra] = rb
    anchored = set()
    for e in wo.get("absolute", []):
        anchored.add(find(e["event"]))
    return {e: find(e) for e in events}, anchored


def satisfies(wo, assign):
    for e in wo.get("absolute", []):
        if not (e["lo"] <= assign[e["event"]] <= e["hi"]):
            return False
    for e in wo.get("relative", []):
        if not (e["lo"] <= assign[e["after"]] - assign[e["before"]] <= e["hi"]):
            return False
    return True


def rel_holds(rel, assign, a, b):
    d = assign[b] - assign[a]
    return {"A<B": d >= 1, "A=B": d == 0, "A>B": d <= -1}[rel]


# ---------------------------------------------------------------------------
# 小窗口全枚举
# ---------------------------------------------------------------------------

def enumerate_feasible(wo, limit):
    """返回 (feasible, comp_of, anchored)；窗口乘积超过 limit 时返回 None。"""
    events = wo["events"]
    abs_evs = wo.get("absolute", [])
    rel_evs = wo.get("relative", [])
    R = sum(abs(e["lo"]) + abs(e["hi"]) for e in rel_evs)
    comp_of, anchored = wo_components(wo)

    reps = {}
    for e in events:
        r = comp_of[e]
        if r not in anchored and r not in reps:
            reps[r] = e  # 无锚片平移不变：固定代表元为 0

    if abs_evs:
        lo0 = min(e["lo"] for e in abs_evs) - R
        hi0 = max(e["hi"] for e in abs_evs) + R
    domains, total = [], 1
    for e in events:
        r = comp_of[e]
        if r in anchored:
            d = range(lo0, hi0 + 1)
        elif reps[r] == e:
            d = range(0, 1)
        else:
            d = range(-R, R + 1)
        domains.append(d)
        total *= len(d)
    if total > limit:
        return None

    feasible = []
    for combo in itertools.product(*domains):
        assign = dict(zip(events, combo))
        if satisfies(wo, assign):
            feasible.append(assign)
    return feasible, comp_of, anchored


# ---------------------------------------------------------------------------
# 证书重放
# ---------------------------------------------------------------------------

def check_chain(evs, steps, start, end, what):
    """逐步对照原始证据重放一条推导链，返回总权重。"""
    if not steps:
        fail("%s: empty proof chain" % what)
    cur, total = start, 0
    for s in steps:
        if s["from"] != cur:
            fail("%s: chain broken at %r" % (what, s))
        if s["evidence"] not in evs:
            fail("%s: unknown evidence %r" % (what, s["evidence"]))
        ek, ev = evs[s["evidence"]]
        want = derive_step(ek, ev, s["kind"])
        if want is None or want != (s["from"], s["to"], s["weight"]):
            fail("%s: step %r not derivable from evidence %r"
                 % (what, s, s["evidence"]))
        total += s["weight"]
        cur = s["to"]
    if cur != end:
        fail("%s: chain ends at %r, expected %r" % (what, cur, end))
    return total


def check_cycle(evs, cert):
    steps = cert.get("steps") or []
    for i, s in enumerate(steps):
        if s["to"] != steps[(i + 1) % len(steps)]["from"]:
            fail("contradiction: steps do not form a cycle")
    total = check_chain(evs, steps, steps[0]["from"], steps[0]["from"],
                        "contradiction")
    if total >= 0:
        fail("contradiction: cycle weight %d is not negative" % total)
    if cert.get("total_weight") != total:
        fail("contradiction: total_weight mismatch")
    return total


# ---------------------------------------------------------------------------
# 分类独立重判
# ---------------------------------------------------------------------------

def expected_defect(wo):
    evset = set(wo["events"])
    for coll, keys in (("absolute", ("event",)), ("relative", ("before", "after"))):
        for e in wo.get(coll, []):
            for k in keys:
                if e.get(k) not in evset:
                    return "bad_reference"
    for coll in ("absolute", "relative"):
        for e in wo.get(coll, []):
            if e["lo"] > e["hi"]:
                return "inverted_interval"
    return None


# ---------------------------------------------------------------------------
# 工单级核对
# ---------------------------------------------------------------------------

def check_work_order(wo, wo_out, enum_limit, stats):
    wid = wo.get("id")
    what = "WO %s" % wid
    evs = index_evidence(wo)
    defect = expected_defect(wo)

    if defect is not None:
        if wo_out.get("status") != "quarantined" or wo_out.get("error_kind") != defect:
            fail("%s: expected quarantine/%s, got %s"
                 % (what, defect, wo_out.get("error_kind")))
        stats["quarantined"] += 1
        return

    if wo_out.get("status") == "quarantined":
        if wo_out.get("error_kind") != "infeasible":
            fail("%s: unexpected error_kind %r" % (what, wo_out.get("error_kind")))
        check_cycle(evs, wo_out["contradiction"])  # 证书单独即可证不可满足
        enum = enumerate_feasible(wo, enum_limit)
        if enum is not None and enum[0]:
            fail("%s: solver says infeasible but enumeration found assignments"
                 % what)
        stats["quarantined"] += 1
        stats["certs"] += 1
        return

    if wo_out.get("status") != "ok":
        fail("%s: unexpected status %r" % (what, wo_out.get("status")))

    comp_of, anchored = wo_components(wo)

    # 分量划分独立比对
    solver_comps = {}
    for c in wo_out.get("components", []):
        for e in c["events"]:
            solver_comps[e] = c["anchored"]
    for e in wo["events"]:
        if solver_comps.get(e) != (comp_of[e] in anchored):
            fail("%s: component/anchored mismatch for %s" % (what, e))

    # 路径证据重放（无论是否枚举都做）
    for b in wo_out["bounds"]:
        e = b["event"]
        if b["latest"] is not None:
            t = check_chain(evs, b["latest_proof"]["steps"], ZERO, e,
                            "%s latest_proof(%s)" % (what, e))
            if t != b["latest"]:
                fail("%s: latest proof total != bound for %s" % (what, e))
            stats["proofs"] += 1
        if b["earliest"] is not None:
            t = check_chain(evs, b["earliest_proof"]["steps"], e, ZERO,
                            "%s earliest_proof(%s)" % (what, e))
            if -t != b["earliest"]:
                fail("%s: earliest proof total != bound for %s" % (what, e))
            stats["proofs"] += 1

    # 小窗口全枚举：独立核对两端
    enum = enumerate_feasible(wo, enum_limit)
    if enum is None:
        stats["enum_skipped"] += 1
        return None
    feasible, _, _ = enum
    if not feasible:
        fail("%s: solver ok but enumeration found no assignment" % what)
    for b in wo_out["bounds"]:
        e = b["event"]
        if comp_of[e] in anchored:
            mn = min(a[e] for a in feasible)
            mx = max(a[e] for a in feasible)
            if b["earliest"] != mn or b["latest"] != mx:
                fail("%s: bounds for %s: solver [%s,%s] vs enum [%s,%s]"
                     % (what, e, b["earliest"], b["latest"], mn, mx))
        else:
            if b["earliest"] is not None or b["latest"] is not None:
                fail("%s: anchorless event %s must have null bounds" % (what, e))
    stats["enum_checked"] += 1
    return enum


def expected_relation(wo, enum, a, b, rel):
    """独立判定关系是否应存在；无法判定时返回 None。"""
    if a == b:
        return rel == "A=B"
    comp_of, anchored = wo_components(wo)
    if comp_of[a] != comp_of[b] and not (comp_of[a] in anchored
                                         and comp_of[b] in anchored):
        return True  # 跨分量且不全有锚：平移自由，三种关系都可达
    if enum is None:
        return None
    feasible = enum[0]
    return any(rel_holds(rel, assign, a, b) for assign in feasible)


def check_queries(spec, out, enums, stats):
    wos = {w.get("id"): w for w in spec.get("work_orders", [])}
    wos_out = {w["id"]: w for w in out["work_orders"]}
    for q_in, q_out in zip(spec.get("queries", []), out.get("queries", [])):
        what = "query %s(%s,%s)" % (q_in.get("work_order"), q_in.get("a"),
                                    q_in.get("b"))
        wo = wos.get(q_in.get("work_order"))
        wo_out = wos_out.get(q_in.get("work_order"))
        if wo is None or wo_out is None:
            if "error" not in q_out:
                fail("%s: expected an error result" % what)
            continue
        if wo_out["status"] != "ok":
            if q_out.get("error") != "work_order_quarantined":
                fail("%s: expected work_order_quarantined" % what)
            continue
        if "error" in q_out:
            fail("%s: unexpected error %r" % (what, q_out["error"]))
        a, b = q_in["a"], q_in["b"]
        enum = enums.get(q_in.get("work_order"))
        for rel in ("A<B", "A=B", "A>B"):
            r = q_out["relations"][rel]
            exp = expected_relation(wo, enum, a, b, rel)
            if exp is not None and r["possible"] != exp:
                fail("%s: relation %s: solver=%s expected=%s"
                     % (what, rel, r["possible"], exp))
            if r["possible"]:
                assign = r["assignment"]
                if not satisfies(wo, assign):
                    fail("%s: witness for %s violates raw evidence" % (what, rel))
                if not rel_holds(rel, assign, a, b):
                    fail("%s: witness does not realize %s" % (what, rel))
                stats["witnesses"] += 1
        stats["queries"] += 1


# ---------------------------------------------------------------------------
# 驱动
# ---------------------------------------------------------------------------

def run_tool(spec_path):
    with tempfile.NamedTemporaryFile("r", suffix=".json", delete=False) as tf:
        out_path = tf.name
    try:
        subprocess.run([sys.executable, TOOL, "run", spec_path,
                        "-o", out_path, "--compact"], check=True)
        with open(out_path, "r", encoding="utf-8") as f:
            return json.load(f)
    finally:
        os.unlink(out_path)


def check_spec(path, enum_limit, stats):
    with open(path, "r", encoding="utf-8") as f:
        spec = json.load(f)
    out = run_tool(path)
    wos_out = {w["id"]: w for w in out["work_orders"]}
    enums = {}
    for wo in spec.get("work_orders", []):
        wid = wo.get("id")
        if wid not in wos_out:
            fail("WO %s missing from solver output" % wid)
        enum = check_work_order(wo, wos_out[wid], enum_limit, stats)
        if enum is not None:
            enums[wid] = enum
        stats["work_orders"] += 1
    check_queries(spec, out, enums, stats)
    print("  checked %s: %d work orders, %d queries"
          % (os.path.basename(path), len(spec.get("work_orders", [])),
             len(spec.get("queries", []))))


# ---------------------------------------------------------------------------
# 随机小工单模糊核对
# ---------------------------------------------------------------------------

def gen_fuzz_spec(n, seed):
    rng = random.Random(seed)
    wos, queries = [], []
    for i in range(n):
        ne = rng.choice([2, 3, 3, 4])
        events = ["e%d" % k for k in range(ne)]
        abs_evs, rel_evs = [], []
        for j in range(rng.randint(0, 2)):
            lo = rng.randint(-4, 4)
            hi = lo + rng.randint(0, 4)
            if rng.random() < 0.05:
                lo, hi = hi + 1, lo  # 逆置区间
            tgt = rng.choice(events + (["ghost"] if rng.random() < 0.05 else []))
            abs_evs.append({"id": "A%d" % (j + 1), "event": tgt, "lo": lo,
                            "hi": hi, "source": "fuzz"})
        for j in range(rng.randint(0, 3)):
            lo = rng.randint(-3, 3)
            hi = lo + rng.randint(0, 3)
            if rng.random() < 0.05:
                lo, hi = hi + 1, lo
            rel_evs.append({"id": "R%d" % (j + 1),
                            "before": rng.choice(events),
                            "after": rng.choice(events),  # 可能自引用
                            "lo": lo, "hi": hi, "source": "fuzz"})
        wos.append({"id": "F%03d" % i, "events": events,
                    "absolute": abs_evs, "relative": rel_evs})
        for _ in range(2):
            queries.append({"work_order": "F%03d" % i,
                            "a": rng.choice(events), "b": rng.choice(events)})
    return {"work_orders": wos, "queries": queries}


def main(argv=None):
    ap = argparse.ArgumentParser(description="timeline_tool 独立枚举核对器")
    ap.add_argument("specs", nargs="*", help="输入规格 JSON 文件")
    ap.add_argument("--fuzz", type=int, default=0, help="随机工单数量")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--enum-limit", type=int, default=2000000,
                    help="单工单枚举赋值数上限")
    args = ap.parse_args(argv)

    stats = {"work_orders": 0, "queries": 0, "quarantined": 0, "certs": 0,
             "proofs": 0, "witnesses": 0, "enum_checked": 0, "enum_skipped": 0}
    failures = []

    specs = list(args.specs)
    tmp = None
    if args.fuzz:
        tmp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
        json.dump(gen_fuzz_spec(args.fuzz, args.seed), tmp)
        tmp.close()
        specs.append(tmp.name)
    try:
        for path in specs:
            try:
                check_spec(path, args.enum_limit, stats)
            except CheckFailure as ex:
                failures.append("%s: %s" % (path, ex))
    finally:
        if tmp is not None:
            os.unlink(tmp.name)

    print("stats: %s" % json.dumps(stats, sort_keys=True))
    if failures:
        for f in failures:
            print("FAIL: %s" % f)
        return 1
    print("ALL CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
