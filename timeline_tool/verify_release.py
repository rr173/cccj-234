#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_release.py — 放行账册独立核对器

完全不复用 release_ledger.py 的数据结构与判定逻辑，独立做四件事：

1. 固定演示全流程（examples/release/woa_*.json）：用 subprocess 驱动真实 CLI
   建库入账，逐批比对独立参考实现重放出的状态与 CLI 查账快照；
2. 非法批次：过期撤销、同项多操作、ID 复用、组不匹配、坏计划、坏事件、
   引用 fail/失效凭证、独立项带 refs —— 每类都要求非零退出，且库内内容
   哈希在尝试前后不变（不留半笔数据）；
3. 同批前置排列：同一批操作仅行序对调，两个独立库的批次结束快照逐字节一致；
4. 随机模糊（--fuzz N）：随机生成无环计划与 register/revoke 序列，参考实现
   逐批重放，与 CLI 快照逐项比对；并随机插入非法操作确认原子拒绝。

参考实现把每张工单建模成：
  item -> 凭证历史 [(cred_id, result, state, refs)]，state 只前进；
有效性由独立的递归函数沿引用链重算（计划无环 => 必然终止）。

用法：
    python3 verify_release.py
    python3 verify_release.py --fuzz 200 --seed 11
"""

import argparse
import hashlib
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "release_ledger.py")
ATTR_TOOL = os.path.join(HERE, "attribution_tool.py")
EXDIR = os.path.join(HERE, "examples", "release")

STATS = {"demo_batches": 0, "illegal_rejections": 0, "fuzz_batches": 0,
         "fuzz_rejections": 0, "order_checks": 0, "snapshot_mismatches": 0}


class CheckFailure(Exception):
    pass


def expect(cond, msg):
    if not cond:
        raise CheckFailure(msg)


def run_cli(*args):
    p = subprocess.run([sys.executable, TOOL] + list(args),
                       capture_output=True, text=True)
    return p.returncode, p.stdout, p.stderr


def build_attribution_db(path):
    subprocess.run(
        [sys.executable, ATTR_TOOL, "build", "--db", path, "--force",
         "--timeline", os.path.join(HERE, "examples", "attribution",
                                    "timeline.json"),
         "--manifest", os.path.join(HERE, "examples", "attribution",
                                    "manifest.json"),
         "--shifts", os.path.join(HERE, "examples", "attribution",
                                  "shifts.json")],
        check=True, stdout=subprocess.DEVNULL)


def db_content_hash(path):
    """逻辑哈希：把所有业务表行排序后哈希（不受 WAL 文件影响）。"""
    conn = sqlite3.connect(path)
    parts = []
    for t in ("rl_plans", "rl_plan_items", "rl_prereq_groups",
              "rl_prereq_members", "rl_batches", "rl_credentials",
              "rl_credential_refs", "rl_snapshots"):
        rows = conn.execute("SELECT * FROM %s" % t).fetchall()
        parts.append(json.dumps(
            sorted(rows, key=lambda r: json.dumps(r, default=str, sort_keys=True)),
            default=str, sort_keys=True))
    conn.close()
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


# ---------------------------------------------------------------------------
# 独立参考实现
# ---------------------------------------------------------------------------

class RefCred(object):
    def __init__(self, cid, item, result, batch, refs):
        self.cid = cid
        self.item = item
        self.result = result
        self.batch = batch
        self.refs = refs          # {prereq_item: cred_id}
        self.state = "active"     # active / superseded / revoked
        self.ended = None

    def to_hist(self):
        return {"credential_id": self.cid, "item": self.item,
                "result": self.result, "state": self.state,
                "registered_batch": self.batch, "ended_batch": self.ended,
                "revoke_batch": self.ended if self.state == "revoked" else None}


class RefLedger(object):
    """完全独立的参考语义；合法序列才会被喂进来。"""

    def __init__(self, plan):
        self.plan = plan                       # 已校验的 plan dict
        self.items = [it["item"] for it in plan["items"]]
        self.events = {it["item"]: it["event"] for it in plan["items"]}
        self.groups = {it["item"]: [tuple(g) for g in it.get("prereq_groups", [])]
                       for it in plan["items"]}
        self.required = list(plan["required"])
        self.hist = {it: [] for it in self.items}   # item -> [RefCred]
        self.ids = {}                                # cid -> RefCred
        self.posted = []

    def _last_live(self, item):
        for c in reversed(self.hist[item]):
            if c.state != "revoked":
                return c
        return None

    def apply(self, payload):
        seq = payload["batch_seq"]
        regs, revs = [], []
        touched = set()
        for op in payload["operations"]:
            expect(op["item"] not in touched, "ref: duplicate item op")
            touched.add(op["item"])
            if op["action"] == "register":
                expect(op["credential_id"] not in self.ids, "ref: id reuse")
                regs.append(op)
            else:
                revs.append(op)

        # 应用登记：旧 live 凭证 superseded
        for op in regs:
            old = self._last_live(op["item"])
            if old is not None:
                old.state, old.ended = "superseded", seq
            refs = {r["item"]: r["credential_id"] for r in op.get("refs", [])}
            c = RefCred(op["credential_id"], op["item"], op["result"], seq, refs)
            self.hist[op["item"]].append(c)
            self.ids[c.cid] = c
        # 应用撤销
        for op in revs:
            target = self._last_live(op["item"])
            expect(target is not None and target.cid == op["credential_id"],
                   "ref: stale revoke")
            target.state, target.ended = "revoked", seq
        self.posted.append(seq)

    def _active_of(self, item):
        for c in reversed(self.hist[item]):
            if c.state == "active":
                return c
        return None

    def validity(self, cid, stack=()):
        """递归重算一份 active 凭证是否有效；返回 (valid, root_cause)。"""
        c = self.ids[cid]
        expect(c.state == "active", "ref: validity only for active")
        if cid in stack:
            return False, {"kind": "cycle", "credential_id": cid}
        for pitem, rid in c.refs.items():
            t = self.ids.get(rid)
            if t is None or t.item != pitem or t.state != "active":
                old = self.ids.get(rid)
                if old is None:
                    return False, {"kind": "missing", "item": pitem,
                                   "credential_id": rid}
                if old.state == "revoked":
                    return False, {"kind": "revoked", "item": pitem,
                                   "credential_id": rid,
                                   "revoke_batch": old.ended}
                return False, {"kind": "superseded", "item": pitem,
                               "credential_id": rid, "ended_batch": old.ended}
            if t.result == "fail":
                return False, {"kind": "fail", "item": pitem,
                               "credential_id": rid}
            ok, root = self.validity(rid, stack + (cid,))
            if not ok:
                return False, root
        return True, None

    def item_status(self, item):
        active = self._active_of(item)
        if active is None:
            return ("revoked" if self.hist[item] else "never_registered"), None
        ok, _root = self.validity(active.cid)
        if not ok:
            return "invalid", active
        return ("pass" if active.result == "pass" else "fail"), active

    def releasable(self):
        for r in self.required:
            st, _ = self.item_status(r)
            if st != "pass":
                return False
        return True


# ---------------------------------------------------------------------------
# 快照比对
# ---------------------------------------------------------------------------

def compare_snapshot(ref, snapshot, seq):
    expect(snapshot["batch_seq"] == seq, "batch_seq mismatch")
    expect(snapshot["releasable"] == ref.releasable(),
            "batch %d releasable mismatch" % seq)
    by_item = {it["item"]: it for it in snapshot["items"]}
    expect(set(by_item) == set(ref.items), "item set mismatch")
    for item in ref.items:
        entry = by_item[item]
        st, active = ref.item_status(item)
        expect(entry["status"] == st,
               "batch %d item %s status: cli=%s ref=%s"
               % (seq, item, entry["status"], st))
        if active is None:
            expect(entry["active_credential"] is None, "unexpected active cred")
        else:
            expect(entry["active_credential"]["credential_id"] == active.cid,
                   "active credential id mismatch")
            ok, _ = ref.validity(active.cid)
            expect(entry["active_credential"]["valid"] == ok,
                   "valid flag mismatch")
            expect(entry["active_credential"]["result"] == active.result,
                   "result mismatch")
        # 历史逐行一致
        cli_hist = [(h["credential_id"], h["state"])
                    for h in entry["registered_credentials"]]
        ref_hist = [(c.cid, c.state) for c in ref.hist[item]]
        expect(cli_hist == ref_hist,
                "batch %d item %s history mismatch %s != %s"
                % (seq, item, cli_hist, ref_hist))
        # 撤销可见性
        rev = [c for c in ref.hist[item] if c.state == "revoked"]
        if rev:
            last = max(rev, key=lambda c: c.ended)
            expect(entry["last_revocation"] ==
                   {"credential_id": last.cid, "revoke_batch": last.ended},
                   "last_revocation mismatch")
        else:
            expect(entry["last_revocation"] is None,
                   "unexpected last_revocation")
    # 仍登记凭证集合一致
    cli_reg = {c["credential_id"]: c["valid"]
               for c in snapshot["registered_credentials"]}
    ref_reg = {}
    for item in ref.items:
        c = ref._active_of(item)
        if c is not None:
            ok, _ = ref.validity(c.cid)
            ref_reg[c.cid] = ok
    expect(cli_reg == ref_reg, "registered_credentials mismatch")
    # 放行依据：恰好是 pass 的必需项
    basis_items = sorted(b["item"] for b in snapshot["release_basis"])
    ref_basis = sorted(r for r in ref.required
                       if ref.item_status(r)[0] == "pass")
    expect(basis_items == ref_basis, "release_basis mismatch")


def cli_query(db, wo, seq):
    code, out, err = run_cli("query", "--db", db, "--work-order", wo,
                             "--batch", str(seq), "--compact")
    expect(code == 0, "query failed: %s %s" % (out, err))
    return json.loads(out)


def cli_post(db, payload_path, attr_db=None):
    args = ["post", "--db", db]
    if attr_db:
        args += ["--attribution-db", attr_db]
    args += ["--batch", payload_path, "--compact"]
    code, out, err = run_cli(*args)
    return code, out, err


# ---------------------------------------------------------------------------
# 1) 演示全流程
# ---------------------------------------------------------------------------

def demo_flow(attr_db, tmpdir):
    db = os.path.join(tmpdir, "demo.db")
    code, _, err = run_cli("init", "--db", db)
    expect(code == 0, err)
    files = [
        "woa_batch10_plan_and_pass.json",
        "woa_batch20_replace_fail.json",
        "woa_batch30_alternative_group.json",
        "woa_batch40_resign.json",
        "woa_batch50_revoke.json",
    ]
    ref = None
    for fname in files:
        payload = json.load(open(os.path.join(EXDIR, fname), encoding="utf-8"))
        if ref is None:
            ref = RefLedger(payload["plan"])
        before = db_content_hash(db)
        code, out, err = cli_post(db, os.path.join(EXDIR, fname), attr_db)
        expect(code == 0, "legal batch rejected: %s %s" % (out, err))
        expect(db_content_hash(db) != before, "legal batch wrote nothing")
        ref.apply(payload)
        snap = cli_query(db, "WO-A", payload["batch_seq"])
        compare_snapshot(ref, snap, payload["batch_seq"])
        STATS["demo_batches"] += 1

    expected_release = {10: True, 20: False, 30: False, 40: True, 50: False}
    for seq, want in expected_release.items():
        expect(cli_query(db, "WO-A", seq)["releasable"] == want,
               "batch %d release expectation" % seq)

    # 旧批次快照不随后续入账改变：再查批 10 必须仍是可放行、M-1 active
    s10 = cli_query(db, "WO-A", 10)
    expect(s10["releasable"] is True, "old batch 10 changed")
    mm = next(i for i in s10["items"] if i["item"] == "machine_measure")
    expect(mm["active_credential"]["credential_id"] == "M-1",
            "old snapshot was rewritten")

    # 未知工单/批次不是空账
    for wo, seq in (("WO-UNKNOWN", 1), ("WO-A", 999)):
        code, out, _ = run_cli("query", "--db", db, "--work-order", wo,
                               "--batch", str(seq), "--compact")
        expect(code == 2 and json.loads(out)["status"] == "rejected",
               "unknown %s/%s must be rejected" % (wo, seq))
    return db, ref


# ---------------------------------------------------------------------------
# 2) 非法批次原子性
# ---------------------------------------------------------------------------

def illegal_batches(attr_db, tmpdir, demo_db):
    # 在演示库当前状态（已到批 50）上，各类非法文件都不得改库
    for fname, expect_code in (
            ("woa_bad_stale_revoke.json", "rejected_batch"),
            ("woa_bad_dup_and_ref.json", "rejected_batch"),
            ("woa_bad_reused_id.json", "rejected_batch"),
            ("woa_bad_group.json", "rejected_batch")):
        before = db_content_hash(demo_db)
        code, out, err = cli_post(demo_db, os.path.join(EXDIR, fname))
        expect(code == 2, "%s should be rejected" % fname)
        body = json.loads(out)
        expect(body["status"] == "rejected", "status field")
        expect(db_content_hash(demo_db) == before,
               "%s left partial data" % fname)
        STATS["illegal_rejections"] += 1

    # 坏计划：新库、首批即拒，库内不得出现该工单任何行
    fresh = os.path.join(tmpdir, "badplan.db")
    run_cli("init", "--db", fresh)
    code, out, _ = cli_post(fresh, os.path.join(EXDIR, "wob_bad_plan_cycle.json"),
                            attr_db)
    expect(code == 2, "bad plan must be rejected")
    body = json.loads(out)["errors"][0]
    codes = [e["code"] for e in body.get("errors", [])]
    expect("prereq_cycle" in codes and "unknown_required_item" in codes,
           "bad plan error kinds: %s" % codes)
    conn = sqlite3.connect(fresh)
    expect(conn.execute("SELECT COUNT(*) FROM rl_plans").fetchone()[0] == 0,
           "rejected plan left a plan row")
    expect(conn.execute("SELECT COUNT(*) FROM rl_batches").fetchone()[0] == 0,
           "rejected plan left a batch row")
    conn.close()
    STATS["illegal_rejections"] += 1

    # 事件不在归属库
    ghost_db = os.path.join(tmpdir, "ghost.db")
    run_cli("init", "--db", ghost_db)
    gp = os.path.join(tmpdir, "ghost.json")
    json.dump({"work_order": "WO-B", "batch_seq": 1,
               "plan": {"items": [{"item": "x", "event": "e_ghost",
                                   "prereq_groups": []}],
                        "required": ["x"]},
               "operations": [{"action": "register", "item": "x",
                               "credential_id": "G1", "result": "pass",
                               "refs": []}]}, open(gp, "w"))
    code, out, _ = cli_post(ghost_db, gp, attr_db)
    expect(code == 2 and
           json.loads(out)["errors"][0]["code"] == "event_not_found",
           "ghost event must be rejected with event_not_found")
    STATS["illegal_rejections"] += 1


# ---------------------------------------------------------------------------
# 3) 同批前置排列
# ---------------------------------------------------------------------------

def order_equivalence(attr_db, tmpdir):
    snaps = {}
    for tag, fname in (("A", "wob_batch3_orderA.json"),
                       ("B", "wob_batch3_orderB.json")):
        db = os.path.join(tmpdir, "order%s.db" % tag)
        run_cli("init", "--db", db)
        code, out, err = cli_post(db, os.path.join(EXDIR, fname), attr_db)
        expect(code == 0, "order case rejected: %s%s" % (out, err))
        s = cli_query(db, "WO-B", 3)
        s.pop("batch_meta", None)
        snaps[tag] = json.dumps(s, sort_keys=True, ensure_ascii=False)
    expect(snaps["A"] == snaps["B"],
           "row permutation changed the batch-end snapshot")
    STATS["order_checks"] += 1


# ---------------------------------------------------------------------------
# 4) 随机模糊
# ---------------------------------------------------------------------------

PLAN_EVENTS = {
    "WO-A": ["e_cross", "e_before", "e_after", "e_noshift", "e_noanchor"],
    "WO-B": ["e_conflict", "e_tie", "e_seq"],
}


def random_plan(rng, wo):
    events = PLAN_EVENTS[wo]
    n = rng.randint(2, min(4, len(events)))
    names = ["it%d" % i for i in range(n)]
    items = []
    for i, name in enumerate(names):
        groups = []
        # 前置只能指向更早的项 => 天然无环
        if i > 0 and rng.random() < 0.7:
            seen_sets = set()
            for _ in range(rng.randint(1, 2)):
                k = rng.randint(1, i)
                g = rng.sample(names[:i], k)
                if frozenset(g) not in seen_sets:
                    seen_sets.add(frozenset(g))
                    groups.append(g)
        items.append({"item": name, "event": rng.choice(events),
                      "prereq_groups": groups})
    required = rng.sample(names, rng.randint(1, len(names)))
    return {"items": items, "required": required}


def fuzz(attr_db, tmpdir, rounds, seed):
    rng = random.Random(seed)
    for case in range(rounds):
        wo = rng.choice(list(PLAN_EVENTS))
        plan = random_plan(rng, wo)
        ref = RefLedger(plan)
        db = os.path.join(tmpdir, "fuzz%d.db" % case)
        run_cli("init", "--db", db)
        next_seq = 0
        uid = 0
        posted_any = False
        for step in range(rng.randint(1, 12)):
            # 先决定每项的“动作类型”，再按本批结束态构造引用，
            # 这样同批先登记的前置也能被引用（排列无关）。
            plan_ops = []   # (item, kind)
            used_items = set()
            for _ in range(rng.randint(1, len(ref.items))):
                item = rng.choice(ref.items)
                if item in used_items:
                    continue
                used_items.add(item)
                live = ref._last_live(item)
                if live is not None and rng.random() < 0.25:
                    plan_ops.append((item, "revoke"))
                else:
                    plan_ops.append((item, "register"))
            if not plan_ops:
                continue

            # 模拟本批结束态：仅用于判断引用是否合法，不修改 ref。
            # end_valid 模拟“本批结束时”的有效性（同批取代/撤销会让历史
            # active 凭证级联失效），与工具的 reference_not_valid 口径一致。
            end_active = {it: ref._active_of(it) for it in ref.items}
            staged = {}        # item -> (cid, result)
            for item, kind in plan_ops:
                if kind == "register":
                    uid += 1
                    cid = "F-%d-%d" % (case, uid)
                    result = rng.choice(["pass", "fail"])
                    staged[item] = (cid, result)
                    end_active[item] = type(
                        "S", (), {"cid": cid, "result": result})()
                else:
                    end_active[item] = None   # 撤销后该项本批结束无 active

            # 记录本批新凭证实际将采用的组，供 sim_valid 递归同批链
            chosen_group = {}
            for item, kind in plan_ops:
                if kind == "register" and ref.groups[item]:
                    chosen_group[item] = rng.choice(ref.groups[item])

            def sim_valid(item, stack=()):
                """item 在本批结束态的 active 凭证是否有效。"""
                c = end_active.get(item)
                if c is None or c.result != "pass" or item in stack:
                    return False
                if item in staged:
                    # 同批新登记：沿它将采用的组内前置继续递归
                    for p in chosen_group.get(item, ()):
                        if not sim_valid(p, stack + (item,)):
                            return False
                    return True
                # 历史 active 凭证：沿它实际引用的凭证 ID，按结束态重算
                old = ref.ids[c.cid]
                for pitem, rid in old.refs.items():
                    target = ref.ids.get(rid)
                    if target is None or target.state != "active":
                        return False
                    cur = end_active.get(pitem)
                    if cur is None or cur.cid != rid:
                        return False   # 引用的已不是该项结束态凭证
                    if not sim_valid(pitem, stack + (item,)):
                        return False
                return True

            # 结束态确定后再构造每个操作（引用按结束态取）
            ops = []
            blocked = False
            for item, kind in plan_ops:
                if kind == "revoke":
                    ops.append({"action": "revoke", "item": item,
                                "credential_id": ref._last_live(item).cid})
                    continue
                cid, result = staged[item]
                refs = []
                g = chosen_group.get(item)
                if g:
                    for p in g:
                        pc = end_active.get(p)
                        if pc is None or pc.result != "pass":
                            blocked = True
                            break
                        if not sim_valid(p, (item,)):
                            blocked = True
                            break
                        refs.append({"item": p, "credential_id": pc.cid})
                    if blocked:
                        break
                ops.append({"action": "register", "item": item,
                            "credential_id": cid, "result": result,
                            "refs": refs})
            if blocked or not ops:
                # 回收本步分配但不会入账的 uid 不影响正确性（ID 只要不重复）
                continue
            next_seq += rng.randint(1, 3)   # 严格递增、允许跳号
            seq = next_seq
            rng.shuffle(ops)   # 同批排列随机化
            payload = {"work_order": wo, "batch_seq": seq, "operations": ops}
            if not posted_any:
                payload["plan"] = plan
            path = os.path.join(tmpdir, "fuzz_payload.json")
            json.dump(payload, open(path, "w"), ensure_ascii=False)
            code, out, err = cli_post(db, path,
                                      attr_db if not posted_any else None)
            if code != 0:
                raise CheckFailure(
                    "fuzz legal batch rejected: %s\npayload=%s"
                    % (out, json.dumps(payload, ensure_ascii=False)))
            posted_any = True
            ref.apply(payload)
            snap = cli_query(db, wo, seq)
            compare_snapshot(ref, snap, seq)
            STATS["fuzz_batches"] += 1

            # 随机插一个非法批次：重复 ID，必须原子拒绝
            if rng.random() < 0.4 and ref.ids:
                old_id = rng.choice(list(ref.ids))
                bad = {"work_order": wo, "batch_seq": seq + 1,
                       "operations": [
                           {"action": "register",
                            "item": rng.choice(ref.items),
                            "credential_id": old_id, "result": "pass",
                            "refs": []}]}
                before = db_content_hash(db)
                bp = os.path.join(tmpdir, "fuzz_bad.json")
                json.dump(bad, open(bp, "w"))
                code, out, _ = cli_post(db, bp)
                expect(code == 2, "fuzz illegal batch was accepted")
                expect(db_content_hash(db) == before,
                       "fuzz illegal batch left data")
                STATS["fuzz_rejections"] += 1


def main(argv=None):
    ap = argparse.ArgumentParser(description="放行账册独立核对器")
    ap.add_argument("--fuzz", type=int, default=120)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--keep-tmp", action="store_true")
    args = ap.parse_args(argv)

    tmpdir = tempfile.mkdtemp(prefix="verify_release_")
    attr_db = os.path.join(tmpdir, "attr.db")
    build_attribution_db(attr_db)
    try:
        demo_db, _ref = demo_flow(attr_db, tmpdir)
        illegal_batches(attr_db, tmpdir, demo_db)
        order_equivalence(attr_db, tmpdir)
        fuzz(attr_db, tmpdir, args.fuzz, args.seed)
    finally:
        if args.keep_tmp:
            print("tmp kept at", tmpdir)
        else:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)
    print("stats:", json.dumps(STATS, sort_keys=True))
    print("ALL RELEASE LEDGER CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
