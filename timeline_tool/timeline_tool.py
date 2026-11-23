#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
timeline_tool.py — 质检事件时间线离线入仓工具

把同一工单下的绝对时间证据（事件落在闭区间）与相对延迟证据
（后者时刻 - 前者时刻 落在闭区间，负数合法）合并为一组差分约束：

    绝对证据  lo <= t_x <= hi        ->  t_x - t_@zero <= hi  且  t_@zero - t_x <= -lo
    相对证据  lo <= t_a - t_b <= hi  ->  t_a - t_b <= hi      且  t_b - t_a <= -lo

其中 @zero 是恒为 0 的人工参考变量。同一对事件上的多条证据不互相覆盖，
全部进入约束系统、必须同时成立；自引用按同一逻辑处理（等价于要求 0 落在区间内）。

交付：
  * 每个事件的最早/最晚可行时刻（精确整数毫秒），每个有限端点附一条
    可从原始证据逐步复核的推导链（路径证据）；
  * 无绝对锚的连通片：可相对排序，但绝对上下界均为 null（未定义，不是 0）；
  * 证据互相矛盾时隔离该工单，给出能单独证明不可满足的负环证书（不要求最短）；
  * 坏引用 / 逆置区间 / 合法但不可满足 三类问题分开报告；
  * 按需回答指定事件对 A<B / A=B / A>B 是否各自存在，每个存在的关系附一份
    满足该工单全部证据的时刻赋值。不预建、不交付全事件对比较矩阵。

只依赖 Python 标准库，不访问任何线上服务。
"""

import argparse
import json
import sys

ZERO = "@zero"  # 人工参考变量，恒为 0；绝对证据都相对它表达


class Edge(object):
    """一条半约束 t_v - t_u <= w，对应图中的一条边 u -> v（权重 w）。"""

    __slots__ = ("u", "v", "w", "evidence", "source", "kind", "orig")

    def __init__(self, u, v, w, evidence=None, source=None, kind=None, orig=None):
        self.u, self.v, self.w = u, v, w
        self.evidence = evidence  # 证据 id（查询用的临时约束为 None）
        self.source = source      # 证据来源身份
        self.kind = kind          # abs_hi / abs_lo / rel_hi / rel_lo / query
        self.orig = orig          # 反向边指向其原始边（用于最早时刻的推导链）

    def step(self):
        return {
            "from": self.u,
            "to": self.v,
            "weight": self.w,
            "meaning": "t_%s - t_%s <= %d" % (self.v, self.u, self.w),
            "evidence": self.evidence,
            "source": self.source,
            "kind": self.kind,
        }


def _is_int(x):
    return isinstance(x, int) and not isinstance(x, bool)


# ---------------------------------------------------------------------------
# 证据规范化与校验
# ---------------------------------------------------------------------------

def _normalize(wo_raw):
    """解析工单原始记录。返回 (events, abs_evs, rel_evs, problems)。"""
    problems = []
    events = wo_raw.get("events")
    if not isinstance(events, list) or any(not isinstance(e, str) for e in events):
        return None, None, None, [{"reason": "events must be a list of event-name strings"}]
    if len(set(events)) != len(events):
        problems.append({"reason": "duplicate event names in catalog"})
    if ZERO in events:
        problems.append({"reason": "%r is a reserved name" % ZERO})

    def _id(e, default):
        v = e.get("id")
        return default if v is None else str(v)

    abs_evs, rel_evs = [], []
    for i, e in enumerate(wo_raw.get("absolute", [])):
        abs_evs.append({"id": _id(e, "abs%d" % (i + 1)), "event": e.get("event"),
                        "lo": e.get("lo"), "hi": e.get("hi"),
                        "source": e.get("source", "unknown")})
    for i, e in enumerate(wo_raw.get("relative", [])):
        rel_evs.append({"id": _id(e, "rel%d" % (i + 1)), "before": e.get("before"),
                        "after": e.get("after"), "lo": e.get("lo"), "hi": e.get("hi"),
                        "source": e.get("source", "unknown")})

    seen = set()
    for ev in abs_evs + rel_evs:
        if ev["id"] in seen:
            problems.append({"reason": "duplicate evidence id", "evidence": ev["id"]})
        seen.add(ev["id"])
        if not _is_int(ev["lo"]) or not _is_int(ev["hi"]):
            problems.append({"reason": "lo/hi must be exact integers (ms)",
                             "evidence": ev["id"]})
    for ev in abs_evs:
        if not isinstance(ev["event"], str):
            problems.append({"reason": "event must be a string", "evidence": ev["id"]})
    for ev in rel_evs:
        for k in ("before", "after"):
            if not isinstance(ev[k], str):
                problems.append({"reason": "%s must be a string" % k,
                                 "evidence": ev["id"]})
    return events, abs_evs, rel_evs, problems


def _build_edges(abs_evs, rel_evs):
    """把证据翻译成半约束边。同一对事件上的多条证据全部保留。"""
    edges = []
    for ev in abs_evs:
        x = ev["event"]
        edges.append(Edge(ZERO, x, ev["hi"], ev["id"], ev["source"], "abs_hi"))
        edges.append(Edge(x, ZERO, -ev["lo"], ev["id"], ev["source"], "abs_lo"))
    for ev in rel_evs:
        b, a = ev["before"], ev["after"]
        edges.append(Edge(b, a, ev["hi"], ev["id"], ev["source"], "rel_hi"))
        edges.append(Edge(a, b, -ev["lo"], ev["id"], ev["source"], "rel_lo"))
    return edges


def _components(events, abs_evs, rel_evs):
    """按证据连通性划片；含绝对证据的片为 anchored。"""
    parent = {e: e for e in events}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for ev in rel_evs:
        ra, rb = find(ev["before"]), find(ev["after"])
        if ra != rb:
            parent[ra] = rb

    anchored = set()
    for ev in abs_evs:
        anchored.add(find(ev["event"]))

    groups = {}
    for e in events:
        groups.setdefault(find(e), []).append(e)
    return [{"events": members, "anchored": root in anchored}
            for root, members in groups.items()]


# ---------------------------------------------------------------------------
# Bellman-Ford：可满足性、负环证书、最短路（端点与推导链）、可行赋值
# ---------------------------------------------------------------------------

def find_negative_cycle(nodes, edges):
    """存在负环 <=> 约束不可满足。返回证书 dict 或 None。"""
    dist = {n: 0 for n in nodes}  # 相当于接了 0 权重边的虚拟源点
    pred = {}
    last = None
    for _ in range(len(nodes)):
        last = None
        for e in edges:
            if dist[e.u] + e.w < dist[e.v]:
                dist[e.v] = dist[e.u] + e.w
                pred[e.v] = e
                last = e.v
        if last is None:
            return None  # 已收敛：无负环
    # 第 n 轮仍在松弛 => 存在负环；沿 pred 走 n 步必落在环上
    x = last
    for _ in range(len(nodes)):
        x = pred[x].u
    back = [x]
    while pred[back[-1]].u != x:
        back.append(pred[back[-1]].u)
    # back = [b0, b1, ..., bk]，pred(b_i).u = b_{i+1}，pred(b_k).u = b_0
    # 前向环：b0 -> bk -> ... -> b1 -> b0
    fwd_nodes = [back[0]] + [back[i] for i in range(len(back) - 1, 0, -1)] + [back[0]]
    fwd_edges = [pred[back[i]] for i in range(len(back) - 1, -1, -1)]
    total = sum(e.w for e in fwd_edges)
    return {
        "type": "negative_cycle",
        "cycle": fwd_nodes,
        "steps": [e.step() for e in fwd_edges],
        "total_weight": total,
        "derived": ("summing the cycle constraints gives 0 <= %d, which is false; "
                    "the listed evidence alone proves infeasibility" % total),
    }


def _shortest_paths(source, nodes, edges):
    """无负环前提下的单源最短路；不可达节点距离为 None。"""
    dist = {n: None for n in nodes}
    dist[source] = 0
    pred = {}
    for _ in range(len(nodes) - 1):
        changed = False
        for e in edges:
            if dist[e.u] is None:
                continue
            d = dist[e.u] + e.w
            if dist[e.v] is None or d < dist[e.v]:
                dist[e.v] = d
                pred[e.v] = e
                changed = True
        if not changed:
            break
    return dist, pred


def _feasible_assignment(nodes, edges):
    """无负环前提下求一份可行势函数；随后整体平移使 t_@zero = 0，
    平移不改变任何差分约束，且使绝对证据按原义成立。"""
    dist = {n: 0 for n in nodes}
    for _ in range(len(nodes) - 1):
        changed = False
        for e in edges:
            if dist[e.u] + e.w < dist[e.v]:
                dist[e.v] = dist[e.u] + e.w
                changed = True
        if not changed:
            break
    base = dist[ZERO]
    return {n: d - base for n, d in dist.items()}


def _proof(chain, derived):
    return {
        "derived": derived,
        "total_weight": sum(e.w for e in chain),
        "steps": [e.step() for e in chain],
    }


def _chain_to(pred, start):
    """沿 pred 从 start 走回 @zero，再反转成 @zero -> ... -> start 的正向链。"""
    chain = []
    cur = start
    while cur != ZERO:
        e = pred[cur]
        chain.append(e)
        cur = e.u
    chain.reverse()
    return chain


def _chain_from(pred_lo, start):
    """在反向图上沿 pred 从 start 走回 @zero；逐步取原始边，
    正好得到原图中 start -> ... -> @zero 的正向链。"""
    chain = []
    cur = start
    while cur != ZERO:
        r = pred_lo[cur]
        chain.append(r.orig)
        cur = r.u
    return chain


def _bounds(events, nodes, edges):
    """最早/最晚可行时刻：最晚 = @zero 到事件的最短路；
    最早 = 事件到 @zero 最短路的相反数（在反向图上求）。无锚片均为 None。"""
    dist_hi, pred_hi = _shortest_paths(ZERO, nodes, edges)
    redges = [Edge(e.v, e.u, e.w, orig=e) for e in edges]
    dist_lo, pred_lo = _shortest_paths(ZERO, nodes, redges)
    out = []
    for e in events:
        entry = {"event": e,
                 "earliest": None if dist_lo[e] is None else -dist_lo[e],
                 "latest": dist_hi[e]}
        if dist_hi[e] is not None:
            entry["latest_proof"] = _proof(_chain_to(pred_hi, e),
                                           "t_%s <= %d" % (e, dist_hi[e]))
        if dist_lo[e] is not None:
            entry["earliest_proof"] = _proof(_chain_from(pred_lo, e),
                                             "t_%s >= %d" % (e, -dist_lo[e]))
        if dist_hi[e] is None:
            entry["note"] = ("anchorless component: absolute bounds undefined "
                             "(null, not 0); only relative order is constrained")
        out.append(entry)
    return out


# ---------------------------------------------------------------------------
# 工单处理
# ---------------------------------------------------------------------------

def process_work_order(wo_raw):
    wid = wo_raw.get("id", "?")
    result = {"id": wid}
    events, abs_evs, rel_evs, problems = _normalize(wo_raw)
    if problems:
        result.update(status="quarantined", error_kind="malformed_record",
                      defects=problems)
        return result, None
    evset = set(events)

    # 1) 坏引用：证据指向目录外事件
    bad = []
    for ev in abs_evs:
        if ev["event"] not in evset:
            bad.append({"evidence": ev["id"], "event": ev["event"],
                        "source": ev["source"]})
    for ev in rel_evs:
        for k in ("before", "after"):
            if ev[k] not in evset:
                bad.append({"evidence": ev["id"], "event": ev[k], "role": k,
                            "source": ev["source"]})
    if bad:
        result.update(status="quarantined", error_kind="bad_reference", defects=bad)
        return result, None

    # 2) 逆置区间：lo > hi
    inverted = [{"evidence": ev["id"], "lo": ev["lo"], "hi": ev["hi"],
                 "source": ev["source"]}
                for ev in abs_evs + rel_evs if ev["lo"] > ev["hi"]]
    if inverted:
        result.update(status="quarantined", error_kind="inverted_interval",
                      defects=inverted)
        return result, None

    # 3) 合法证据的可满足性（含自引用：等价于要求 0 落在区间内）
    edges = _build_edges(abs_evs, rel_evs)
    nodes = list(events) + [ZERO]
    cert = find_negative_cycle(nodes, edges)
    if cert is not None:
        result.update(status="quarantined", error_kind="infeasible",
                      contradiction=cert)
        return result, None

    comps = _components(events, abs_evs, rel_evs)
    result.update(status="ok", components=comps,
                  bounds=_bounds(events, nodes, edges))
    ctx = {"events": events, "evset": evset, "nodes": nodes, "edges": edges,
           "components": comps}
    return result, ctx


# ---------------------------------------------------------------------------
# 事件对关系查询（按需，不建全对矩阵）
# ---------------------------------------------------------------------------

def _query_extra_edges(rel, a, b):
    """关系对应的临时约束（整数毫秒：严格先后至少差 1ms）。"""
    if rel == "A<B":   # t_b - t_a >= 1
        return [Edge(b, a, -1, kind="query")]
    if rel == "A>B":   # t_a - t_b >= 1
        return [Edge(a, b, -1, kind="query")]
    return [Edge(a, b, 0, kind="query"), Edge(b, a, 0, kind="query")]  # A=B


def run_query(q, contexts):
    wid, a, b = q.get("work_order"), q.get("a"), q.get("b")
    out = {"work_order": wid, "a": a, "b": b}
    if wid not in contexts:
        out["error"] = "unknown_work_order"
        return out
    res, ctx = contexts[wid]
    if res["status"] != "ok":
        out["error"] = "work_order_quarantined"
        out["error_kind"] = res["error_kind"]
        return out
    if a not in ctx["evset"] or b not in ctx["evset"]:
        out["error"] = "unknown_event"
        return out
    relations = {}
    for rel in ("A<B", "A=B", "A>B"):
        edges2 = ctx["edges"] + _query_extra_edges(rel, a, b)
        if find_negative_cycle(ctx["nodes"], edges2) is not None:
            relations[rel] = {"possible": False}
        else:
            assign = _feasible_assignment(ctx["nodes"], edges2)
            relations[rel] = {
                "possible": True,
                "assignment": {e: assign[e] for e in ctx["events"]},
            }
    out["relations"] = relations
    anchorless = [e for c in ctx["components"] if not c["anchored"]
                  for e in c["events"]]
    if anchorless:
        out["note"] = ("events %s have no absolute anchor: their assigned "
                       "absolute values are one valid shift; only differences "
                       "are constrained" % anchorless)
    return out


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def run(spec):
    out = {
        "notes": [
            "times are exact integer milliseconds",
            "'%s' is an artificial reference pinned to 0; absolute evidence is "
            "expressed against it" % ZERO,
            "earliest/latest are null for events in components without an "
            "absolute anchor (undefined, never 0)",
            "quarantine kinds: malformed_record / bad_reference / "
            "inverted_interval / infeasible",
        ],
        "work_orders": [],
        "queries": [],
    }
    contexts = {}
    for wo_raw in spec.get("work_orders", []):
        res, ctx = process_work_order(wo_raw)
        if res["id"] in contexts:
            out.setdefault("warnings", []).append(
                "duplicate work_order id %r: queries use the first one" % res["id"])
        else:
            contexts[res["id"]] = (res, ctx)
        out["work_orders"].append(res)
    for q in spec.get("queries", []):
        out["queries"].append(run_query(q, contexts))
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description="质检事件时间线离线入仓工具（纯标准库）")
    sub = ap.add_subparsers(dest="command", required=True)
    pr = sub.add_parser("run", help="读取证据规格 JSON，输出时间界表、证书与查询结果")
    pr.add_argument("input", help="输入规格 JSON（work_orders + queries）")
    pr.add_argument("-o", "--output", help="输出文件（缺省写到 stdout）")
    pr.add_argument("--compact", action="store_true", help="不缩进输出")

    # 召回清单：只读现有班组归属 SQLite 库 + 仓库操作流水，见 recall_tool.py。
    import recall_tool
    r = sub.add_parser(
        "recall", help="按质检事件 + 源箱原始区间生成召回清单（只读）")
    recall_tool.add_arguments(r)
    r.set_defaults(_func=recall_tool.cmd)

    args = ap.parse_args(argv)
    if getattr(args, "_func", None) is not None:
        return args._func(args)

    with open(args.input, "r", encoding="utf-8") as f:
        spec = json.load(f)
    out = run(spec)
    text = json.dumps(out, ensure_ascii=False,
                      indent=None if args.compact else 2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
