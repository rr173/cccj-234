#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_recall.py — 独立核对 recall_tool 的召回清单

不复用 recall_tool 的分段数据结构：本脚本把小规模流水里的每一件产品当成
一个整数 token（(出生箱, 出生位置) -> 当前箱/仓/出货单），逐件重放，然后
与 recall_tool 的输出逐行比对。大件数只用于验证“不为每件产品建行”。

核对内容
--------
1. 固定小流水：混装后的再次拆箱命中、原箱退回后的库存、移入新箱后再出货、
   缺口分隔、同箱正常产品不入选、同一产品不重复计入；
2. 错误流水：顺序错误、重复 seq、未知箱、取缺口、取重叠、空箱出货、
   已出货再取、退回不存在的出货、重复退回、退错箱 —— 每类都要求非零退出
   且带行号/seq、不输出任何清单；
3. 随机小流水：随机 inbound/repack/ship/return，逐件重放与输出比对，
   并校验出口守恒（每件选中产品恰好出现一次）；
4. 大件数：一条覆盖 10^12 个位置的 inbound 与一次换箱，确认进程时间与
   输出片段数不随件数增长。

用法：
    python3 verify_recall.py
    python3 verify_recall.py --fuzz 100 --seed 3
"""

import argparse
import json
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
TOOL = os.path.join(HERE, "recall_tool.py")
ATTR_TOOL = os.path.join(HERE, "attribution_tool.py")


class CheckFailure(Exception):
    pass


def fail(msg):
    raise CheckFailure(msg)


def expect(cond, msg):
    if not cond:
        fail(msg)


# ---------------------------------------------------------------------------
# 建库（只借用既有 attribution_tool build 产生 events 表）
# ---------------------------------------------------------------------------

def build_db(path):
    subprocess.run(
        [sys.executable, ATTR_TOOL, "build", "--db", path,
         "--timeline", os.path.join(HERE, "examples", "attribution",
                                    "timeline.json"),
         "--manifest", os.path.join(HERE, "examples", "attribution",
                                    "manifest.json"),
         "--shifts", os.path.join(HERE, "examples", "attribution",
                                  "shifts.json"),
         "--force"],
        check=True, stdout=subprocess.DEVNULL)


# ---------------------------------------------------------------------------
# 独立逐件参考实现
# ---------------------------------------------------------------------------

class RefBox(object):
    def __init__(self, box_id, tokens, warehouse):
        self.id = box_id
        self.tokens = tokens          # 位置 -> token 列表，长度不变
        self.orig = list(tokens)      # 建箱时的原始位置 -> token（首次 inbound）
        self.warehouse = warehouse
        self.out = None               # 当前出货对象


def ref_replay(records, source_box):
    boxes = {}
    shipments = {}
    for rec in records:
        op = rec["op"]
        seq = rec["seq"]
        if op == "inbound":
            n = rec["count"]
            boxes[rec["box"]] = RefBox(
                rec["box"], [(rec["box"], i) for i in range(n)],
                rec["warehouse"])
        elif op == "repack":
            for take in rec["takes"]:
                src = boxes[take["box"]]
                expect(src.warehouse == rec["warehouse"],
                       "reference: warehouse mismatch")
            tokens = []
            for take in rec["takes"]:
                src = boxes[take["box"]]
                for i in range(take["lo"], take["hi"]):
                    tok = src.tokens[i]
                    if tok is None:
                        raise CheckFailure("reference: take gap")
                    tokens.append(tok)
            for take in rec["takes"]:
                src = boxes[take["box"]]
                for i in range(take["lo"], take["hi"]):
                    src.tokens[i] = None
            new_box = RefBox(rec["box"], tokens, rec["warehouse"])
            new_box.orig = list(tokens)   # 新箱的“原始位置”即建箱时内容
            boxes[rec["box"]] = new_box
        elif op == "ship":
            box = boxes[rec["box"]]
            expect(box.warehouse is not None, "reference: ship not in wh")
            expect(any(t is not None for t in box.tokens),
                   "reference: ship empty")
            ship = {"seq": seq, "box": box.id, "shipment": rec["shipment"],
                    "returned": False}
            shipments[seq] = ship
            box.out = ship
            box.warehouse = None
        elif op == "return":
            box = boxes[rec["box"]]
            ship = shipments[rec["ship_seq"]]
            expect(not ship["returned"], "reference: double return")
            expect(ship["box"] == box.id, "reference: wrong box")
            expect(box.out is ship, "reference: box not out")
            ship["returned"] = True
            box.out = None
            box.warehouse = rec["warehouse"]
        else:
            raise CheckFailure("reference: unknown op")
    return boxes, shipments


def ref_rows(boxes, shipments, source_box, start, end):
    src = boxes[source_box]
    chosen = set()
    for i in range(start, end):
        tok = src.orig[i]
        chosen.add(tok)
    expect(len(chosen) == end - start, "reference: duplicate tokens in source")
    rows = {}
    for box in boxes.values():
        if box.warehouse is None and box.out is None:
            continue
        if box.warehouse is not None:
            loc = ("warehouse", box.warehouse, None, None)
        else:
            ship = box.out
            if ship["returned"]:
                loc = ("warehouse", None, None, None)  # 不应发生
            loc = ("shipment", None, ship["shipment"], ship["seq"])
        for pos, tok in enumerate(box.tokens):
            if tok is None or tok not in chosen:
                continue
            key = (box.id, loc)
            rows.setdefault(key, []).append(pos)
    out = []
    for (box_id, loc), positions in rows.items():
        positions.sort()
        run = [positions[0], positions[0] + 1]
        for p in positions[1:]:
            if p == run[1]:
                run[1] = p + 1
            else:
                out.append((box_id, loc, run[0], run[1]))
                run = [p, p + 1]
        out.append((box_id, loc, run[0], run[1]))
    out.sort(key=lambda r: (str(r[0]), r[2]))
    return out, chosen


# ---------------------------------------------------------------------------
# 调用被测工具
# ---------------------------------------------------------------------------

def write_log(path, records):
    with open(path, "w", encoding="utf-8") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def run_tool(db, log, source_box, start, end, work_order="WO-A",
             event="e_cross"):
    proc = subprocess.run(
        [sys.executable, TOOL, "--db", db, "--work-order", work_order,
         "--event", event, "--log", log, "--source-box", source_box,
         "--source-start", str(start), "--source-end", str(end)],
        capture_output=True, text=True)
    try:
        out = json.loads(proc.stdout)
    except ValueError:
        fail("tool did not emit JSON (rc=%d): %r / %r"
             % (proc.returncode, proc.stdout, proc.stderr))
    return proc.returncode, out


def key_list(rows):
    return [{"box": box_id, "start": a} for box_id, _loc, a, _b in rows]


def normalize_rows(rows):
    out = []
    for r in rows:
        expect("count" in r, "row missing count")
        expect(r["count"] == r["end"] - r["start"],
               "row count does not equal end-start")
        expect(r["start"] < r["end"], "row is empty/reversed")
        expect(r["location_type"] in ("warehouse", "shipment"),
               "bad location_type")
        if r["location_type"] == "warehouse":
            expect(r["warehouse"] is not None, "warehouse row without warehouse")
            expect(r["shipment"] is None and r["ship_seq"] is None,
                   "warehouse row carries shipment info")
            loc = ("warehouse", r["warehouse"], None, None)
        else:
            expect(r["warehouse"] is None, "shipment row carries warehouse")
            expect(r["shipment"] is not None, "shipment row without shipment")
            expect(isinstance(r["ship_seq"], int), "shipment row without seq")
            loc = ("shipment", None, r["shipment"], r["ship_seq"])
        out.append((r["box"], loc, r["start"], r["end"]))
    return out


def check_against_reference(records, source_box, start, end, result):
    boxes, shipments = ref_replay(records, source_box)
    want, chosen = ref_rows(boxes, shipments, source_box, start, end)
    got = normalize_rows(result["rows"])
    expect(got == want,
           "rows differ from per-piece reference\n  got  %r\n  want %r"
           % (got, want))
    if all(isinstance(r["box"], str) for r in result["rows"]):
        key = lambda r: (r["box"], r["start"])
        expect(key_list(got) == sorted(key_list(got), key=key),
               "rows not sorted by current box ID and start")
    seen = {}
    for box_id, loc, a, b in got:
        span = seen.setdefault(box_id, [])
        expect(all(b <= x or a >= y for x, y in span),
               "same box reports the same position twice")
        span.append((a, b))
    expect(result["totals"]["products"] == len(chosen),
           "totals.products %d != %d"
           % (result["totals"]["products"], len(chosen)))
    for r in result["rows"]:
        expect(r["count"] >= 1, "empty row in output")
    return want


# ---------------------------------------------------------------------------
# 固定小流水
# ---------------------------------------------------------------------------

EXAMPLE_LOG = os.path.join(HERE, "examples", "recall", "warehouse.jsonl")


def load_example():
    with open(EXAMPLE_LOG, "r", encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


BASE = load_example()


def scenario_fixed(db, tmp):
    log = os.path.join(tmp, "base.jsonl")
    write_log(log, BASE)

    # 源箱 B-IN 的原始位置 1..10：包含已拆到 B1/B2/B3 的、退回原箱的、
    # 以及未被选中的正常产品。
    rc, out = run_tool(db, log, "B-IN", 1, 10)
    expect(rc == 0 and out["status"] == "ok", "base scenario failed: %r" % out)
    rows = check_against_reference(BASE, "B-IN", 1, 10, out)
    by_box = {}
    for box_id, loc, a, b in rows:
        by_box.setdefault(box_id, []).append((loc, a, b))

    # 混装后再拆箱：B1 位置 4..6 是 B-IN 0 位置的 4,5 -> 在 S-A 上。
    expect(any(loc == ("shipment", None, "S-A", 60) and (a, b) == (4, 6)
               for loc, a, b in by_box.get("B1", [])),
           "mixed repack shipment not found: %r" % by_box.get("B1"))
    # 原箱退回后的库存：B-IN 6..7、8..9 在 W3，且与 7..8 的缺口分开。
    expect(by_box.get("B-IN") == [
        (("warehouse", "W3", None, None), 7, 8),
        (("warehouse", "W3", None, None), 9, 10)],
        "returned source box rows wrong: %r" % by_box.get("B-IN"))
    # 移入新箱后再出货：B2 的 0..3 已退回 W2，B1 的 4..6 仍在出货单 S-A。
    expect(by_box.get("B2") == [
        (("warehouse", "W2", None, None), 0, 3)],
        "repacked-then-returned rows wrong: %r" % by_box.get("B2"))
    # 同箱正常产品（B-MIX 的 0..2 中的 0..1 进 B1，但不在源区间）不入选。
    expect("B-MIX" not in by_box, "normal same-box product leaked into recall")
    # B3 有 0..1 与 2..3 两段，缺口（位置 1）隔开。
    expect([(a, b) for loc, a, b in by_box.get("B3", [])] == [(0, 1), (2, 3)],
           "gap-separated rows not split: %r" % by_box.get("B3"))
    # 选中 B-IN 0..12 = 整箱：正常产品也在召回里，但每件只出现一次。
    rc, out2 = run_tool(db, log, "B-IN", 0, 12)
    expect(rc == 0, "whole-box scenario failed")
    rows2 = check_against_reference(BASE, "B-IN", 0, 12, out2)
    expect(len(rows2) == 7, "whole-box row count changed: %r" % rows2)
    flat = []
    for box_id, loc, a, b in rows2:
        flat.extend(range(a, b))
    expect(len(flat) == 12, "whole-box products != 12")

    # 源箱原始位置已被全部拆走：查询仍合法，按出生位置追到 B2（3..5）。
    rc, out3 = run_tool(db, log, "B-MIX", 0, 2)
    expect(rc == 0, "emptied source box query rejected: %r" % out3)
    check_against_reference(BASE, "B-MIX", 0, 2, out3)
    got3 = normalize_rows(out3["rows"])
    expect(got3 == [
        ("B2", ("warehouse", "W2", None, None), 3, 5)],
        "emptied source rows: %r" % got3)

    # 源箱自身也是一个换箱箱（B1）：原始位置 0..8 的选中区间同样可追。
    rc, out6 = run_tool(db, log, "B1", 0, 8)
    expect(rc == 0, "repacked source box query rejected: %r" % out6)
    check_against_reference(BASE, "B1", 0, 8, out6)
    got6 = normalize_rows(out6["rows"])
    expect(got6 == [
        ("B1", ("shipment", None, "S-A", 60), 0, 1),
        ("B1", ("shipment", None, "S-A", 60), 4, 6),
        ("B2", ("warehouse", "W2", None, None), 0, 5)],
        "repacked source rows: %r" % got6)

    # 退回后再次出货：同一箱的第二次出货使用新的 seq，旧出货记录已结束。
    reship = [dict(r) for r in BASE]
    reship.append({"seq": 110, "op": "ship", "box": "B2",
                   "shipment": "S-D"})
    reship_path = os.path.join(tmp, "reship.jsonl")
    write_log(reship_path, reship)
    rc, out7 = run_tool(db, reship_path, "B-IN", 1, 10)
    expect(rc == 0, "reship scenario failed: %r" % out7)
    check_against_reference(reship, "B-IN", 1, 10, out7)
    got7 = normalize_rows(out7["rows"])
    expect(("B2", ("shipment", None, "S-D", 110), 0, 3) in got7,
           "second shipment not reported with its own seq: %r" % got7)
    expect(not any(r[1][3] == 70 for r in got7),
           "finished shipment 70 must not appear: %r" % got7)

    # 别名 + 整数箱 ID：命令行 --source-box 7 匹配流水中的 "box": 7。
    alias = [
        {"seq": 1, "type": "inbound", "box_id": 7, "wh": "W",
         "quantity": 6},
        {"seq": 2, "type": "inbound", "box": "B", "warehouse": "W",
         "count": 3},
        {"seq": 3, "action": "repack", "box": "N", "warehouse": "W",
         "sources": [{"box": 7, "start": 1, "end": 4},
                     {"box": "B", "start": 0, "end": 2}]},
        {"seq": 4, "action": "ship", "box": "N", "order": "O-1"},
        {"seq": 5, "type": "return", "box": "N", "shipment_seq": 4,
         "warehouse": "W9"},
    ]
    alias_path = os.path.join(tmp, "alias.jsonl")
    write_log(alias_path, alias)
    rc, out8 = run_tool(db, alias_path, "7", 2, 4)
    expect(rc == 0, "alias/integer-ID scenario failed: %r" % out8)
    got8 = normalize_rows(out8["rows"])
    expect(got8 == [("N", ("warehouse", "W9", None, None), 1, 3)],
           "alias/integer-ID rows wrong: %r" % got8)

    # 无锚事件同样可用：只看 events 表存在性，与时间锚/设备绑定无关。
    rc, out4 = run_tool(db, log, "B-IN", 1, 10, event="e_noanchor")
    expect(rc == 0 and out4["status"] == "ok",
           "unanchored event should still be recalled")
    # 没有任何设备归属的事件（events 表里有，但无 bindings）也可查。
    probe = sqlite3.connect(db)
    probe.execute("INSERT INTO events (work_order, event, time_status, "
                  "earliest, latest, bounds_json) "
                  "VALUES ('WO-A', 'e_nobind', 'anchored', 0, 0, '{}')")
    probe.commit()
    probe.close()
    rc, out5 = run_tool(db, log, "B-IN", 1, 10, work_order="WO-A",
                       event="e_nobind")
    expect(rc == 0 and out5["status"] == "ok",
           "event without device binding should still be recalled")
    return rows


# ---------------------------------------------------------------------------
# 错误流水：非零退出、带行号/seq、无部分清单
# ---------------------------------------------------------------------------

def expect_reject(db, tmp, records, code, label, source=("B-IN", 1, 10)):
    path = os.path.join(tmp, "bad_%s.jsonl" % label)
    write_log(path, records)
    rc, out = run_tool(db, path, source[0], source[1], source[2])
    expect(rc != 0, "%s: expected non-zero exit" % label)
    expect(out.get("status") == "rejected", "%s: not rejected: %r"
           % (label, out))
    expect("rows" not in out, "%s: partial rows emitted" % label)
    err = out["errors"][0]
    expect(err["code"] == code, "%s: code %r != %r (%r)"
           % (label, err["code"], code, err))
    expect("line" in err or "seq" in err, "%s: no line/seq in error: %r"
           % (label, err))
    return err


def scenario_errors(db, tmp):
    base = [dict(r) for r in BASE]

    def mutate(recs, index, **changes):
        recs = [dict(r) for r in recs]
        recs[index] = dict(recs[index])
        recs[index].update(changes)
        return recs

    # 未知操作
    bad = mutate(base, 2, op="explode")
    expect_reject(db, tmp, bad, "unknown_op", "unknown_op")

    # 空 takes
    bad = mutate(base, 2, takes=[])
    expect_reject(db, tmp, bad, "invalid_field", "empty_takes")

    # 字段类型错误（count 为布尔）
    bad = mutate(base, 0, count=True)
    expect_reject(db, tmp, bad, "invalid_field", "bool_count")

    # seq 非正整数
    bad = mutate(base, 0, seq=0)
    expect_reject(db, tmp, bad, "invalid_field", "bad_seq")

    # 顺序错误（文件顺序递减）
    bad = [dict(r) for r in base]
    bad[3]["seq"] = 5
    expect_reject(db, tmp, bad, "seq_order", "seq_order")

    # 重复 seq
    bad = mutate(base, 3, seq=30)
    expect_reject(db, tmp, bad, "duplicate_seq", "duplicate_seq")

    # 引用未知箱
    bad = mutate(base, 2, takes=[{"box": "GHOST", "lo": 0, "hi": 1}])
    err = expect_reject(db, tmp, bad, "unknown_box", "unknown_box")
    expect(err["seq"] == 30, "unknown_box error should carry seq 30")

    # 取已拆走的位置（B1 的 0..1 在 seq 40 被取走）
    bad = [dict(r) for r in base]
    bad.insert(4, {"seq": 45, "op": "repack", "box": "B4", "warehouse": "W1",
                   "takes": [{"box": "B1", "lo": 0, "hi": 2}]})
    expect_reject(db, tmp, bad, "take_gap", "take_gap")

    # 从同一箱取重叠两段
    bad = mutate(base, 2, takes=[{"box": "B-IN", "lo": 0, "hi": 4},
                                 {"box": "B-IN", "lo": 3, "hi": 5}])
    expect_reject(db, tmp, bad, "take_overlap", "take_overlap")

    # 已出货的箱再取
    bad = [dict(r) for r in base]
    bad.append({"seq": 105, "op": "repack", "box": "B4", "warehouse": "W1",
                "takes": [{"box": "B1", "lo": 0, "hi": 1}]})
    expect_reject(db, tmp, bad, "box_not_in_warehouse", "take_shipped")

    # 空箱出货：B-EMPTY 的唯一一件被取走后再出货
    bad = [{"seq": 5, "op": "inbound", "box": "B-EMPTY", "warehouse": "W1",
            "count": 1}]
    bad.extend(base)
    bad.append({"seq": 105, "op": "repack", "box": "B-TAKE",
                "warehouse": "W1",
                "takes": [{"box": "B-EMPTY", "lo": 0, "hi": 1}]})
    bad.append({"seq": 106, "op": "ship", "box": "B-EMPTY",
                "shipment": "S-X"})
    expect_reject(db, tmp, bad, "empty_box_ship", "empty_box_ship")

    # 退回不存在的出货 seq
    bad = mutate(base, 9, ship_seq=999)
    expect_reject(db, tmp, bad, "unknown_ship_seq", "unknown_ship_seq")

    # 重复使用已退回的出货
    bad = [dict(r) for r in base]
    bad.append({"seq": 110, "op": "return", "box": "B2", "ship_seq": 70,
                "warehouse": "W2"})
    err = expect_reject(db, tmp, bad, "shipment_already_returned",
                        "double_return")
    expect(err["seq"] == 110, "double return should carry seq 110")

    # 退回时箱号与出货记录不符
    bad = mutate(base, 9, box="B2")
    expect_reject(db, tmp, bad, "return_box_mismatch", "return_wrong_box")

    # 源区间越界 / 空 / 逆向
    for label, (a, b), code in [
            ("src_oob", (0, 13), "source_range_out_of_bounds"),
            ("src_empty", (4, 4), "empty_source_range"),
            ("src_rev", (5, 2), "invalid_source_range")]:
        path = os.path.join(tmp, "base_%s.jsonl" % label)
        write_log(path, base)
        rc, out = run_tool(db, path, "B-IN", a, b)
        expect(rc != 0 and out.get("status") == "rejected",
               "%s: not rejected" % label)
        expect(out["errors"][0]["code"] == code,
               "%s: %r" % (label, out["errors"][0]))
        expect("rows" not in out, "%s: partial rows" % label)

    # 未知源箱
    path = os.path.join(tmp, "base_unknown_src.jsonl")
    write_log(path, base)
    rc, out = run_tool(db, path, "GHOST", 0, 1)
    expect(rc != 0 and out["errors"][0]["code"] == "unknown_source_box",
           "unknown source box not rejected")

    # 数据库事件不存在
    path = os.path.join(tmp, "base_ev.jsonl")
    write_log(path, base)
    rc, out = run_tool(db, path, "B-IN", 0, 1, work_order="WO-A",
                       event="ghost")
    expect(rc != 0 and out["errors"][0]["code"] == "event_not_found",
           "missing event not rejected")

    # 数据库只读：查询前后文件内容哈希不变；只读文件 + 只读目录（WAL 库没有
    # -shm 且目录不可写）时仍应可查，不能因此写库或失败。
    import hashlib
    write_log(path, base)
    before = hashlib.sha256(open(db, "rb").read()).hexdigest()
    run_tool(db, path, "B-IN", 1, 10)
    after = hashlib.sha256(open(db, "rb").read()).hexdigest()
    expect(before == after, "database file changed by recall query")

    ro_dir = os.path.join(tmp, "ro")
    os.makedirs(ro_dir, exist_ok=True)
    ro_db = os.path.join(ro_dir, "readonly.db")
    shutil.copyfile(db, ro_db)
    for suffix in ("-wal", "-shm"):
        try:
            os.unlink(ro_db + suffix)
        except FileNotFoundError:
            pass
    os.chmod(ro_db, 0o444)
    os.chmod(ro_dir, 0o555)
    try:
        rc, out = run_tool(ro_db, path, "B-IN", 1, 10)
        expect(rc == 0 and out["status"] == "ok",
               "read-only database/dir query failed: %r" % out)
        check_against_reference(base, "B-IN", 1, 10, out)
    finally:
        os.chmod(ro_dir, 0o755)
        os.chmod(ro_db, 0o644)


# ---------------------------------------------------------------------------
# 随机小流水
# ---------------------------------------------------------------------------

def random_log(rng, n_ops=25, max_count=5):
    records = []
    boxes = {}
    shipments = {}
    counter = [0]
    seq = rng.randint(1, 5)

    def next_seq():
        nonlocal seq
        seq += rng.randint(1, 4)
        return seq

    # 先建若干箱
    for i in range(rng.randint(1, 3)):
        counter[0] += 1
        box = "R%d" % counter[0]
        n = rng.randint(1, max_count)
        wh = "W%d" % rng.randint(1, 2)
        records.append({"seq": next_seq(), "op": "inbound", "box": box,
                        "warehouse": wh, "count": n})
        boxes[box] = [wh, [(box, k) for k in range(n)], None]
    for _ in range(n_ops):
        candidates = [b for b, st in boxes.items()
                      if st[2] is None and st[1]
                      and any(t is not None for t in st[1])]
        if not candidates:
            counter[0] += 1
            box = "R%d" % counter[0]
            n = rng.randint(1, max_count)
            wh = "W%d" % rng.randint(1, 2)
            records.append({"seq": next_seq(), "op": "inbound", "box": box,
                            "warehouse": wh, "count": n})
            boxes[box] = [wh, [(box, k) for k in range(n)], None]
            continue
        choice = rng.random()
        src = rng.choice(candidates)
        wh = boxes[src][0]
        if choice < 0.55:
            takes = []
            used = {}
            wh_sources = [b for b in candidates if boxes[b][0] == wh]
            for _ in range(rng.randint(1, 2)):
                s = rng.choice(wh_sources)
                positions = [k for k, t in enumerate(boxes[s][1])
                             if t is not None]
                if not positions:
                    continue
                a = rng.choice(positions)
                b = a + 1
                while (b < len(boxes[s][1]) and boxes[s][1][b] is not None
                       and rng.random() < 0.6):
                    b += 1
                intervals = used.setdefault(s, [])
                if any(not (b <= x or a >= y) for x, y in intervals):
                    continue
                intervals.append((a, b))
                takes.append({"box": s, "lo": a, "hi": b})
            if not takes:
                continue
            counter[0] += 1
            box = "R%d" % counter[0]
            tokens = []
            for take in takes:
                tokens.extend(boxes[take["box"]][1][take["lo"]:take["hi"]])
            for take in takes:
                for k in range(take["lo"], take["hi"]):
                    boxes[take["box"]][1][k] = None
            records.append({"seq": next_seq(), "op": "repack", "box": box,
                            "warehouse": wh, "takes": takes})
            boxes[box] = [wh, tokens, None]
        elif choice < 0.8:
            box = src
            sid = "OUT-%d" % rng.randint(1, 999)
            records.append({"seq": next_seq(), "op": "ship", "box": box,
                            "shipment": sid})
            shipments[box] = records[-1]["seq"]
            boxes[box][0] = None
            boxes[box][2] = {"seq": records[-1]["seq"], "shipment": sid,
                             "returned": False}
        else:
            out_boxes = [b for b, st in boxes.items() if st[2] is not None
                         and not st[2]["returned"]]
            if not out_boxes:
                continue
            box = rng.choice(out_boxes)
            ship = boxes[box][2]
            wh = "W%d" % rng.randint(1, 3)
            records.append({"seq": next_seq(), "op": "return", "box": box,
                            "ship_seq": ship["seq"], "warehouse": wh})
            ship["returned"] = True
            boxes[box][0] = wh
            boxes[box][2] = None
    return records


def scenario_fuzz(db, tmp, count, seed):
    rng = random.Random(seed)
    checked = 0
    checks = 0
    for case in range(count):
        records = random_log(rng)
        inbound = [r for r in records if r["op"] == "inbound"]
        src_rec = rng.choice(inbound)
        source = src_rec["box"]
        n = src_rec["count"]
        a = rng.randint(0, n - 1)
        b = rng.randint(a + 1, n)
        path = os.path.join(tmp, "fuzz.jsonl")
        write_log(path, records)
        rc, out = run_tool(db, path, source, a, b)
        if rc != 0:
            fail("fuzz case %d rejected: %r" % (case, out))
        check_against_reference(records, source, a, b, out)
        checks += 1

        # 每个已完成退回的出货记录都必须被拒，并指出对应的 return seq。
        last_seq = records[-1]["seq"] if records else 0
        for rec in records:
            if rec["op"] != "return":
                continue
            bad = list(records) + [{"seq": last_seq + 1,
                                    "op": "return", "box": rec["box"],
                                    "ship_seq": rec["ship_seq"],
                                    "warehouse": rec["warehouse"]}]
            bad_path = os.path.join(tmp, "fuzz_bad.jsonl")
            write_log(bad_path, bad)
            rc2, out2 = run_tool(db, bad_path, source, a, b)
            expect(rc2 != 0 and out2.get("status") == "rejected"
                   and out2["errors"][0]["code"]
                   == "shipment_already_returned",
                   "fuzz case %d: reusing finished shipment not rejected: %r"
                   % (case, out2))
            expect(out2["errors"][0].get("seq") == bad[-1]["seq"],
                   "fuzz case %d: double-return error missing seq" % case)
            expect(not out2.get("rows"),
                   "fuzz case %d: rejected log emitted rows" % case)
            checks += 1
    return checks


# ---------------------------------------------------------------------------
# 大件数
# ---------------------------------------------------------------------------

def scenario_large(db, tmp):
    huge = 10 ** 12
    records = [
        {"seq": 1, "op": "inbound", "box": "H-IN", "warehouse": "W1",
         "count": huge},
        {"seq": 2, "op": "repack", "box": "H-A", "warehouse": "W1",
         "takes": [{"box": "H-IN", "lo": 10, "hi": huge - 10}]},
        {"seq": 3, "op": "ship", "box": "H-A", "shipment": "OUT-HUGE"},
        {"seq": 4, "op": "repack", "box": "H-B", "warehouse": "W1",
         "takes": [{"box": "H-IN", "lo": 0, "hi": 5},
                   {"box": "H-IN", "lo": huge - 5, "hi": huge}]},
    ]
    path = os.path.join(tmp, "huge.jsonl")
    write_log(path, records)
    start = time.monotonic()
    rc, out = run_tool(db, path, "H-IN", 0, huge)
    elapsed = time.monotonic() - start
    expect(rc == 0, "huge scenario failed: %r" % out)
    expect(elapsed < 10.0, "huge scenario took %.1fs (per-piece work?)" % elapsed)
    expect(out["totals"]["products"] == huge, "huge totals wrong")
    # H-IN 剩 [5, huge-10)，H-B 两段，H-A 整段出货。
    expect(len(out["rows"]) == 4, "huge rows: %r" % out["rows"])
    for row in out["rows"]:
        expect(row["count"] == row["end"] - row["start"],
               "huge row count mismatch")
    total = sum(r["count"] for r in out["rows"])
    expect(total == huge, "huge rows do not conserve products")
    # 再跑一次小源区间：只应产生两段（H-B 的 0..5）。
    rc, out2 = run_tool(db, path, "H-IN", 0, 5)
    expect(rc == 0 and len(out2["rows"]) == 1,
           "huge small-window rows wrong: %r" % out2["rows"])
    expect(out2["rows"][0]["box"] == "H-B"
           and out2["rows"][0]["start"] == 0
           and out2["rows"][0]["end"] == 5,
           "huge small-window row mismatch: %r" % out2["rows"])
    return elapsed


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--fuzz", type=int, default=80)
    ap.add_argument("--seed", type=int, default=11)
    args = ap.parse_args(argv)
    tmp = tempfile.mkdtemp(prefix="recall_verify_")
    db = os.path.join(tmp, "work.db")
    try:
        build_db(db)
        fixed = scenario_fixed(db, tmp)
        scenario_errors(db, tmp)
        fuzz = scenario_fuzz(db, tmp, args.fuzz, args.seed)
        elapsed = scenario_large(db, tmp)
        print("stats: " + json.dumps({
            "fixed_rows": len(fixed),
            "fuzz_cases": fuzz,
            "huge_seconds": round(elapsed, 3),
        }, sort_keys=True))
        print("ALL RECALL CHECKS PASSED")
        return 0
    except CheckFailure as ex:
        print("FAILED: %s" % ex, file=sys.stderr)
        return 1
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
