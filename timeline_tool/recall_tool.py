#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
recall_tool.py — 质检召回范围追踪（离线、只读）

给定一条已经写入班组归属 SQLite 库 `events` 表的 (工单, 事件)，再给定仓库
操作流水、源箱 ID 和源箱原始位置区间 [start, end)，输出：

  * 现在应扣住的库存箱及其位置；
  * 现在应联系的出货单及其出货记录 seq；
  * 同一当前箱内按“最大连续召回位置”分段；缺口或未命中位置会切断分段。

位置与坐标
----------
* 箱内位置从 0 开始；所有位置区间都是半开区间 [start, end)，含起点、不含终点。
* 箱 ID 一经分配永不复用；箱的原始编号和长度在抽走产品后不变。
* 每件产品有一个“出生位置”（第一次 inbound 时的箱 ID + 位置）。换箱、出货、
  退回都不改变出生位置；换箱只改变产品当前所在箱和当前箱内位置。
* 每个箱保存一份“当前位置区间 -> 出生位置区间”的分段映射。因此实现不为每
  件产品建行、建对象或逐件遍历：一条覆盖一万亿个连续位置的 inbound 只是一个
  分段，开销随流水记录数和实际产生的区间片段数增长。

流水（JSON Lines）
------------------
每行一个 JSON 对象，`seq` 是唯一正整数，按文件顺序严格递增（可跳号）。

  {"seq": 1, "op": "inbound", "box": "B1", "warehouse": "W1", "count": 10}
      在仓 W1 新建箱 B1，位置 [0, count)，count 为正整数。

  {"seq": 2, "op": "repack", "box": "B2", "warehouse": "W1",
   "takes": [{"box": "B1", "lo": 2, "hi": 5},
             {"box": "B3", "lo": 0, "hi": 1}]}
      从 W1 中仍在库的旧箱按 takes 的先后顺序取半开区间，装入新箱 B2，
      B2 从位置 0 连续编号；每段内部顺序不变。旧箱只失去实际取出的位置，
      留下的产品位置不变。同一旧箱可被取多段，但不得重叠，也不得取已经
      拆走/不存在的位置。新箱必须在同一仓；空箱不能再出货。

  {"seq": 3, "op": "ship", "box": "B2", "shipment": "OUT-7"}
      把在库非空箱 B2 的全部剩余产品交给出货单 OUT-7，不重编号。
      一个箱同时只能有一次未退回的出货。

  {"seq": 4, "op": "return", "box": "B2", "ship_seq": 3, "warehouse": "W2"}
      按出货记录的 seq=3 退回 B2；必须正好是该箱当前尚未退回的那次出货。
      整箱回到仓 W2，保留出货时的剩余位置，之后可以再换箱或再出货。

字段别名（为便于接入不同导出格式）：`op` 也可写 `type`/`action`；`box` 也可写
`box_id`；`warehouse` 也可写 `warehouse_id`/`wh`；`count` 也可写
`quantity`/`pieces`；`takes` 也可写 `sources`/`from`；take 中的 `lo`/`hi`
也可写 `start`/`end`；`shipment` 也可写 `shipment_id`/`order`；`ship_seq`
也可写 `shipment_seq`。同一个字段的多个别名同时出现且值不同则拒绝。

只依赖 Python 3 标准库。
"""

import argparse
import bisect
import json
import os
import sqlite3
import sys
from urllib.parse import quote


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------

class RecallError(Exception):
    """命令失败：带机器可读 code，以及可选的流水行号 / seq。"""

    def __init__(self, code, message, line=None, seq=None, **details):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.line = line
        self.seq = seq
        self.details = details

    def as_dict(self):
        d = {"code": self.code, "message": self.message}
        if self.line is not None:
            d["line"] = self.line
        if self.seq is not None:
            d["seq"] = self.seq
        for k in sorted(self.details):
            d[k] = self.details[k]
        return d


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _id_ok(v):
    return ((isinstance(v, str) and v != "")
            or (isinstance(v, int) and not isinstance(v, bool)))


def _id_key(v):
    """内部键：整型 ID 与字符串 ID 不混淆。"""
    return (0, v) if isinstance(v, int) else (1, v)


def _id_sort_key(v):
    return _id_key(v)


# ---------------------------------------------------------------------------
# 区间片段
# ---------------------------------------------------------------------------

class Piece(object):
    """当前位置 [pos_start, pos_end) 对应出生位置 [origin_start, +长度)。"""

    __slots__ = ("pos_start", "pos_end", "origin_box", "origin_start")

    def __init__(self, pos_start, pos_end, origin_box, origin_start):
        self.pos_start = pos_start
        self.pos_end = pos_end
        self.origin_box = origin_box
        self.origin_start = origin_start

    @property
    def length(self):
        return self.pos_end - self.pos_start

    def __repr__(self):
        return "Piece(%d,%d,%r,%d)" % (
            self.pos_start, self.pos_end, self.origin_box, self.origin_start)


class Box(object):
    __slots__ = ("box_id", "key", "length", "initial", "current",
                 "warehouse", "ship_seq", "created_seq")

    def __init__(self, box_id, length, initial, current, warehouse,
                 created_seq):
        self.box_id = box_id
        self.key = _id_key(box_id)
        self.length = length         # 建箱时的原始长度，永不改变
        self.initial = initial       # 建箱时的位置 -> 出生位置，永不改变
        self.current = current       # 当前位置 -> 出生位置，只减不增
        self.warehouse = warehouse   # 在库仓号；已出货时为 None
        self.ship_seq = None         # 当前未退回出货的 seq；在库时为 None
        self.created_seq = created_seq

    @property
    def in_warehouse(self):
        return self.warehouse is not None

    @property
    def empty(self):
        return not self.current


class Shipment(object):
    __slots__ = ("seq", "box_id", "box_key", "shipment", "returned",
                 "return_seq")

    def __init__(self, seq, box_id, box_key, shipment):
        self.seq = seq
        self.box_id = box_id
        self.box_key = box_key
        self.shipment = shipment
        self.returned = False
        self.return_seq = None


class WarehouseState(object):
    def __init__(self):
        self.boxes = {}       # _id_key(box_id) -> Box
        self.shipments = {}   # seq -> Shipment


# ---------------------------------------------------------------------------
# 区间工具
# ---------------------------------------------------------------------------

def _first_overlapping(pieces, lo):
    """第一个 pos_end > lo 的片段下标（片段按位置有序且互不重叠）。"""
    left, right = 0, len(pieces)
    while left < right:
        mid = (left + right) // 2
        if pieces[mid].pos_end <= lo:
            left = mid + 1
        else:
            right = mid
    return left


def _slice_fragments(pieces, lo, hi):
    """返回 ([ (a,b,piece) ... ], covered_length)。

    a/b 是旧箱中的位置边界；调用者用 covered_length 判断区间是否落在缺口上。
    """
    out = []
    covered = 0
    i = _first_overlapping(pieces, lo)
    while i < len(pieces) and pieces[i].pos_start < hi:
        p = pieces[i]
        a = max(p.pos_start, lo)
        b = min(p.pos_end, hi)
        if a < b:
            out.append((a, b, p))
            covered += b - a
        i += 1
    return out, covered


def _remove_interval(pieces, lo, hi):
    """从当前片段表中删除 [lo, hi)。调用前已确认该区间被内容完整覆盖。

    只改动实际相交的片段：前面的片段原样保留，后面的片段整体挪到新位置。
    """
    i = _first_overlapping(pieces, lo)
    j = i
    replacement = []
    while j < len(pieces) and pieces[j].pos_start < hi:
        p = pieces[j]
        if p.pos_start < lo:
            replacement.append(Piece(p.pos_start, lo, p.origin_box,
                                     p.origin_start))
        if p.pos_end > hi:
            offset = hi - p.pos_start
            replacement.append(Piece(hi, p.pos_end, p.origin_box,
                                     p.origin_start + offset))
        j += 1
    pieces[i:j] = replacement


def _merge_intervals(intervals, what, detect_overlap=True):
    """排序并合并相邻区间；detect_overlap 时发现重叠即报内部不一致。"""
    if not intervals:
        return []
    ordered = sorted(intervals)
    out = []
    for a, b in ordered:
        if a > b:
            raise RecallError("internal_inconsistency",
                              "%s: reversed interval [%d, %d)" % (what, a, b))
        if out and a < out[-1][1]:
            if detect_overlap:
                raise RecallError(
                    "internal_inconsistency",
                    "%s: overlapping intervals [%d, %d) and [%d, %d): "
                    "a product is present twice" %
                    (what, out[-1][0], out[-1][1], a, b))
            if b > out[-1][1]:
                out[-1] = (out[-1][0], b)
        elif out and a == out[-1][1]:
            out[-1] = (out[-1][0], b)
        else:
            out.append((a, b))
    return out


# ---------------------------------------------------------------------------
# 流水读取与字段校验
# ---------------------------------------------------------------------------

def _pairs_hook(pairs):
    d = {}
    for k, v in pairs:
        if k in d:
            raise RecallError("duplicate_json_key",
                              "duplicate JSON object key %r" % (k,))
        d[k] = v
    return d


def _pick(rec, names, field, line, seq):
    found = [(n, rec[n]) for n in names if n in rec]
    if not found:
        raise RecallError("missing_field",
                          "missing field %s" % field, line=line, seq=seq)
    value = found[0][1]
    for name, other in found[1:]:
        if other != value:
            raise RecallError(
                "conflicting_field",
                "fields %s and %s both present with different values for %s"
                % (found[0][0], name, field), line=line, seq=seq)
    return value


def _need_id(value, field, line, seq):
    if not _id_ok(value):
        raise RecallError(
            "invalid_field",
            "%s must be a non-empty string or an integer" % field,
            line=line, seq=seq)
    return value


def _need_int(value, field, line, seq, minimum=None):
    if not _is_int(value):
        raise RecallError("invalid_field",
                          "%s must be an exact integer" % field,
                          line=line, seq=seq)
    if minimum is not None and value < minimum:
        raise RecallError("invalid_field",
                          "%s must be >= %d" % (field, minimum),
                          line=line, seq=seq)
    return value


def load_log(path):
    """读取 JSON Lines，校验 seq 唯一且按文件顺序严格递增。"""
    try:
        f = open(path, "r", encoding="utf-8")
    except OSError as ex:
        raise RecallError("log_io_error",
                          "cannot read warehouse log %r: %s" % (path, ex))
    records = []
    seen = set()
    previous = None
    try:
        with f:
            for lineno, raw in enumerate(f, 1):
                text = raw.strip()
                if not text:
                    continue
                try:
                    obj = json.loads(text, object_pairs_hook=_pairs_hook)
                except RecallError as ex:
                    if ex.line is None:
                        ex.line = lineno
                    raise
                except (UnicodeDecodeError, ValueError) as ex:
                    raise RecallError("invalid_json",
                                      "line %d is not valid JSON: %s"
                                      % (lineno, ex), line=lineno)
                if not isinstance(obj, dict):
                    raise RecallError("invalid_record",
                                      "line %d must be a JSON object" % lineno,
                                      line=lineno)
                if "seq" not in obj:
                    raise RecallError("missing_field",
                                      "line %d: missing field seq" % lineno,
                                      line=lineno)
                seq = _need_int(obj["seq"], "seq", lineno, None, minimum=1)
                if seq in seen:
                    raise RecallError("duplicate_seq",
                                      "duplicate seq %d" % seq,
                                      line=lineno, seq=seq)
                if previous is not None and seq <= previous:
                    raise RecallError(
                        "seq_order",
                        "seq %d is not strictly greater than previous seq %d "
                        "(file order must increase)" % (seq, previous),
                        line=lineno, seq=seq)
                seen.add(seq)
                previous = seq
                records.append((lineno, seq, obj))
    except OSError as ex:
        raise RecallError("log_io_error",
                          "cannot read warehouse log %r: %s" % (path, ex))
    except UnicodeDecodeError as ex:
        raise RecallError("invalid_encoding",
                          "warehouse log %r is not valid UTF-8: %s"
                          % (path, ex), line=ex.start)
    finally:
        f.close()
    return records


def _parse_takes(raw, line, seq):
    if not isinstance(raw, list) or not raw:
        raise RecallError("invalid_field",
                          "takes must be a non-empty list", line=line, seq=seq)
    takes = []
    for i, item in enumerate(raw):
        where = "takes[%d]" % i
        if not isinstance(item, dict):
            raise RecallError("invalid_field",
                              "%s must be an object" % where,
                              line=line, seq=seq)
        src = _need_id(_pick(item, ("box", "box_id"), "box", line, seq),
                       "box", line, seq)
        lo = _need_int(_pick(item, ("lo", "start"), "lo", line, seq),
                       "lo", line, seq)
        hi = _need_int(_pick(item, ("hi", "end"), "hi", line, seq),
                       "hi", line, seq)
        if lo >= hi:
            raise RecallError("empty_take",
                              "%s: interval [%d, %d) is empty" %
                              (where, lo, hi), line=line, seq=seq)
        takes.append({"box": src, "lo": lo, "hi": hi, "where": where})
    return takes


# ---------------------------------------------------------------------------
# 流水重放
# ---------------------------------------------------------------------------

def _lookup_box(state, box_id, line, seq, field="box"):
    box = state.boxes.get(_id_key(box_id))
    if box is None:
        raise RecallError("unknown_box",
                          "%s references unknown box %r" % (field, box_id),
                          line=line, seq=seq)
    return box


def _do_inbound(state, rec, line, seq):
    box_id = _need_id(_pick(rec, ("box", "box_id"), "box", line, seq),
                      "box", line, seq)
    warehouse = _need_id(
        _pick(rec, ("warehouse", "warehouse_id", "wh"), "warehouse",
              line, seq), "warehouse", line, seq)
    count = _need_int(_pick(rec, ("count", "quantity", "pieces"), "count",
                            line, seq), "count", line, seq, minimum=1)
    key = _id_key(box_id)
    if key in state.boxes:
        raise RecallError("box_id_reused",
                          "inbound reuses an existing box ID %r" % (box_id,),
                          line=line, seq=seq)
    piece = Piece(0, count, box_id, 0)
    state.boxes[key] = Box(box_id, count, [piece], [piece], warehouse, seq)


def _do_repack(state, rec, line, seq):
    box_id = _need_id(_pick(rec, ("box", "box_id"), "box", line, seq),
                      "box", line, seq)
    warehouse = _need_id(
        _pick(rec, ("warehouse", "warehouse_id", "wh"), "warehouse",
              line, seq), "warehouse", line, seq)
    takes = _parse_takes(
        _pick(rec, ("takes", "sources", "from"), "takes", line, seq),
        line, seq)
    new_key = _id_key(box_id)
    if new_key in state.boxes:
        raise RecallError("box_id_reused",
                          "repack reuses an existing box ID %r" % (box_id,),
                          line=line, seq=seq)

    # 同一旧箱在一个换箱记录中的多段不得重叠。
    by_source = {}
    for take in takes:
        by_source.setdefault(_id_key(take["box"]), []).append(
            (take["lo"], take["hi"], take["where"]))
    for key, ranges in by_source.items():
        ranges.sort()
        for (a, b, where_a), (c, d, where_c) in zip(ranges, ranges[1:]):
            if c < b:
                raise RecallError(
                    "take_overlap",
                    "%s and %s take overlapping intervals [%d, %d) and "
                    "[%d, %d) from the same box" %
                    (where_a, where_c, a, b, c, d), line=line, seq=seq)

    sources = {}
    for key in by_source:
        box = state.boxes.get(key)
        if box is None:
            # 找到原始 ID 仅用于错误信息
            src_id = next(t["box"] for t in takes
                          if _id_key(t["box"]) == key)
            raise RecallError("unknown_box",
                              "repack references unknown box %r" % (src_id,),
                              line=line, seq=seq)
        if not box.in_warehouse:
            raise RecallError("box_not_in_warehouse",
                              "repack takes from box %r, which is shipped "
                              "and not in a warehouse" % (box.box_id,),
                              line=line, seq=seq)
        if _id_key(box.warehouse) != _id_key(warehouse):
            raise RecallError(
                "warehouse_mismatch",
                "repack into %r but source box %r is in %r" %
                (warehouse, box.box_id, box.warehouse), line=line, seq=seq)
        sources[key] = box

    cursor = 0
    initial = []
    for take in takes:
        box = sources[_id_key(take["box"])]
        lo, hi = take["lo"], take["hi"]
        if lo < 0 or hi > box.length:
            raise RecallError(
                "take_out_of_range",
                "%s: [%d, %d) outside box %r original length %d" %
                (take["where"], lo, hi, box.box_id, box.length),
                line=line, seq=seq)
        frags, covered = _slice_fragments(box.current, lo, hi)
        if covered != hi - lo:
            missing = (hi - lo) - covered
            raise RecallError(
                "take_gap",
                "%s: [%d, %d) includes %d position(s) already taken or "
                "absent from box %r" %
                (take["where"], lo, hi, missing, box.box_id),
                line=line, seq=seq)
        for a, b, piece in frags:
            length = b - a
            origin_start = piece.origin_start + (a - piece.pos_start)
            initial.append(Piece(cursor, cursor + length,
                                 piece.origin_box, origin_start))
            cursor += length
        _remove_interval(box.current, lo, hi)

    state.boxes[new_key] = Box(box_id, cursor, initial, list(initial),
                               warehouse, seq)


def _do_ship(state, rec, line, seq):
    box_id = _need_id(_pick(rec, ("box", "box_id"), "box", line, seq),
                      "box", line, seq)
    shipment_id = _need_id(
        _pick(rec, ("shipment", "shipment_id", "order"), "shipment",
              line, seq), "shipment", line, seq)
    box = _lookup_box(state, box_id, line, seq)
    if not box.in_warehouse:
        raise RecallError("box_not_in_warehouse",
                          "cannot ship box %r: it is already out on shipment "
                          "seq %s" % (box.box_id, box.ship_seq),
                          line=line, seq=seq)
    if box.empty:
        raise RecallError("empty_box_ship",
                          "cannot ship box %r: it has no remaining products"
                          % (box.box_id,), line=line, seq=seq)
    shipment = Shipment(seq, box.box_id, box.key, shipment_id)
    state.shipments[seq] = shipment
    box.ship_seq = seq
    box.warehouse = None


def _do_return(state, rec, line, seq):
    box_id = _need_id(_pick(rec, ("box", "box_id"), "box", line, seq),
                      "box", line, seq)
    ship_seq = _need_int(
        _pick(rec, ("ship_seq", "shipment_seq"), "ship_seq", line, seq),
        "ship_seq", line, seq, minimum=1)
    warehouse = _need_id(
        _pick(rec, ("warehouse", "warehouse_id", "wh"), "warehouse",
              line, seq), "warehouse", line, seq)
    box = _lookup_box(state, box_id, line, seq)
    shipment = state.shipments.get(ship_seq)
    if shipment is None:
        raise RecallError("unknown_ship_seq",
                          "return references seq %d, which is not a ship "
                          "record" % ship_seq, line=line, seq=seq)
    if shipment.box_key != box.key:
        raise RecallError(
            "return_box_mismatch",
            "ship seq %d belongs to box %r, not %r" %
            (ship_seq, shipment.box_id, box.box_id), line=line, seq=seq)
    if shipment.returned:
        raise RecallError(
            "shipment_already_returned",
            "ship seq %d was already returned by seq %d and cannot be used "
            "again" % (ship_seq, shipment.return_seq), line=line, seq=seq)
    if box.ship_seq != ship_seq or box.in_warehouse:
        raise RecallError(
            "box_not_shipped",
            "box %r is not currently out on ship seq %d" %
            (box.box_id, ship_seq), line=line, seq=seq)
    box.warehouse = warehouse
    box.ship_seq = None
    shipment.returned = True
    shipment.return_seq = seq


OPS = {
    "inbound": _do_inbound,
    "repack": _do_repack,
    "ship": _do_ship,
    "return": _do_return,
}


def replay(records):
    state = WarehouseState()
    for line, seq, rec in records:
        op = _pick(rec, ("op", "type", "action"), "op", line, seq)
        if not isinstance(op, str) or op not in OPS:
            raise RecallError(
                "unknown_op",
                "unknown op %r (expected inbound/repack/ship/return)" % (op,),
                line=line, seq=seq)
        OPS[op](state, rec, line, seq)
    return state


# ---------------------------------------------------------------------------
# 只读数据库校验
# ---------------------------------------------------------------------------

def check_event_exists(db_path, work_order, event):
    """只读确认 `events` 表存在且 (工单, 事件) 存在。

    不检查 time_status、earliest/latest 或 device_bindings：是否有时间锚或
    班组归属不影响召回。
    """
    if not os.path.isfile(db_path):
        raise RecallError("database_not_found",
                          "database %r does not exist" % db_path)
    quoted = quote(os.path.abspath(db_path), safe="/")
    # 1) 普通只读打开（WAL 库若目录可写会顺带建 -shm，但不会写库文件）。
    # 2) 目录不可写 / 无 -shm 时退到 immutable=1：按当前文件内容读取，绝不写。
    # 3) 最后再退到普通连接 + PRAGMA query_only=ON。
    conn = None
    errors = []
    for target, kwargs in (
            ("file:%s?mode=ro" % quoted, {"uri": True}),
            ("file:%s?mode=ro&immutable=1" % quoted, {"uri": True}),
            (db_path, {})):
        try:
            conn = sqlite3.connect(target, timeout=30.0, **kwargs)
            conn.execute("PRAGMA query_only = ON")
            # SQLite 是惰性打开：真正读一次表才能确认本次打开可用
            # （WAL 库没有 -shm 且目录不可写时会在这一步才失败）。
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            break
        except sqlite3.Error as ex:
            errors.append(str(ex))
            if conn is not None:
                try:
                    conn.close()
                except sqlite3.Error:
                    pass
                conn = None
    if conn is None:
        raise RecallError("invalid_database",
                          "cannot open database %r: %s"
                          % (db_path, "; ".join(errors)))
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name = 'events'").fetchone()
        if row is None:
            raise RecallError("invalid_database",
                              "database %r has no events table" % db_path)
        row = conn.execute(
            "SELECT 1 FROM events WHERE work_order = ? AND event = ? LIMIT 1",
            (work_order, event)).fetchone()
        if row is None:
            raise RecallError(
                "event_not_found",
                "event (%r, %r) does not exist in the events table" %
                (work_order, event), work_order=work_order, event=event)
    except sqlite3.Error as ex:
        raise RecallError("invalid_database",
                          "cannot read events table from %r: %s"
                          % (db_path, ex))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 源区间展开与当前去向
# ---------------------------------------------------------------------------

def select_origins(state, source_box_id, start, end):
    """把源箱建箱时的原始位置区间展开成“出生位置”区间集合。

    流水里的箱 ID 可能是整数（如 `"box": 7`）；命令行参数总是文本。先按原样
    查，查不到再按整数解释一次，避免把 `7` 和 `"7"` 混为一谈。
    """
    box = state.boxes.get(_id_key(source_box_id))
    if box is None and isinstance(source_box_id, str) and source_box_id:
        try:
            numeric = int(source_box_id)
        except ValueError:
            numeric = None
        if numeric is not None and str(numeric) == source_box_id:
            box = state.boxes.get(_id_key(numeric))
            if box is not None:
                source_box_id = numeric
    if box is None:
        raise RecallError("unknown_source_box",
                          "source box %r does not occur in the log"
                          % (source_box_id,))
    if not _is_int(start) or not _is_int(end):
        raise RecallError("invalid_source_range",
                          "source start/end must be exact integers")
    if start == end:
        raise RecallError("empty_source_range",
                          "source interval [%d, %d) is empty" % (start, end))
    if start > end:
        raise RecallError("invalid_source_range",
                          "source interval [%d, %d) is reversed" %
                          (start, end))
    if start < 0 or end > box.length:
        raise RecallError(
            "source_range_out_of_bounds",
            "source interval [%d, %d) is outside box %r original length %d"
            % (start, end, source_box_id, box.length),
            box=source_box_id, length=box.length)

    selected = {}
    covered = 0
    for piece in box.initial:
        a = max(piece.pos_start, start)
        b = min(piece.pos_end, end)
        if a >= b:
            continue
        covered += b - a
        origin_a = piece.origin_start + (a - piece.pos_start)
        origin_b = origin_a + (b - a)
        selected.setdefault(_id_key(piece.origin_box), []).append(
            (origin_a, origin_b))
    if covered != end - start:
        raise RecallError(
            "internal_inconsistency",
            "source box %r initial content does not cover [%d, %d)"
            % (source_box_id, start, end))
    return box, {k: _merge_intervals(v, "selected source origins")
                 for k, v in selected.items()}


def collect_rows(state, selected):
    """扫描所有当前箱，返回按箱 ID、起点排序的召回行。"""
    # 为每个出生箱预先建好有序的选中区间，二分查找后只触碰实际交到的片段。
    arrays = {}
    for key, intervals in selected.items():
        starts = [a for a, _b in intervals]
        ends = [b for _a, b in intervals]
        arrays[key] = (starts, ends)

    contributions = {}
    raw = []
    for box in state.boxes.values():
        if not box.current:
            continue
        if box.in_warehouse:
            location_type = "warehouse"
            warehouse = box.warehouse
            shipment_id = None
            ship_seq = None
        else:
            shipment = state.shipments.get(box.ship_seq)
            if shipment is None:
                raise RecallError("internal_inconsistency",
                                  "box %r is neither in a warehouse nor on a "
                                  "known shipment" % (box.box_id,))
            location_type = "shipment"
            warehouse = None
            shipment_id = shipment.shipment
            ship_seq = shipment.seq
        for piece in box.current:
            origin_key = _id_key(piece.origin_box)
            pair = arrays.get(origin_key)
            if pair is None:
                continue
            starts, ends = pair
            origin_lo = piece.origin_start
            origin_hi = origin_lo + (piece.pos_end - piece.pos_start)
            i = bisect.bisect_right(ends, origin_lo)
            while i < len(starts) and starts[i] < origin_hi:
                a = max(starts[i], origin_lo)
                b = min(ends[i], origin_hi)
                if a < b:
                    pos_a = piece.pos_start + (a - origin_lo)
                    pos_b = piece.pos_start + (b - origin_lo)
                    raw.append({
                        "box": box.box_id,
                        "start": pos_a,
                        "end": pos_b,
                        "count": pos_b - pos_a,
                        "location_type": location_type,
                        "warehouse": warehouse,
                        "shipment": shipment_id,
                        "ship_seq": ship_seq,
                    })
                    contributions.setdefault(origin_key, []).append((a, b))
                i += 1

    # 守恒检查：选中的每件产品恰好出现一次，未丢失、未重复。
    for key, wanted in selected.items():
        got = _merge_intervals(contributions.get(key, []),
                               "current locations of source products")
        if got != wanted:
            raise RecallError(
                "internal_inconsistency",
                "selected source products for origin box %r are not covered "
                "exactly once by current boxes" % (key[1],))

    # 同一当前箱内合并相邻召回位置；缺口/未命中位置不合并。
    raw.sort(key=lambda r: (_id_sort_key(r["box"]), r["start"], r["end"]))
    rows = []
    for row in raw:
        if (rows and rows[-1]["box"] == row["box"]
                and rows[-1]["location_type"] == row["location_type"]
                and rows[-1]["warehouse"] == row["warehouse"]
                and rows[-1]["shipment"] == row["shipment"]
                and rows[-1]["ship_seq"] == row["ship_seq"]):
            if row["start"] < rows[-1]["end"]:
                raise RecallError(
                    "internal_inconsistency",
                    "overlapping current positions in box %r" %
                    (row["box"],))
            if row["start"] == rows[-1]["end"]:
                rows[-1]["end"] = row["end"]
                rows[-1]["count"] += row["count"]
                continue
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def recall(db_path, work_order, event, log_path, source_box, source_start,
           source_end):
    check_event_exists(db_path, work_order, event)
    records = load_log(log_path)
    state = replay(records)
    _source_box, selected = select_origins(
        state, source_box, source_start, source_end)
    rows = collect_rows(state, selected)

    warehouse_products = sum(r["count"] for r in rows
                             if r["location_type"] == "warehouse")
    shipped_products = sum(r["count"] for r in rows
                           if r["location_type"] == "shipment")
    total = warehouse_products + shipped_products
    if total != source_end - source_start:
        raise RecallError("internal_inconsistency",
                          "recall rows cover %d product(s), expected %d"
                          % (total, source_end - source_start))
    return {
        "status": "ok",
        "work_order": work_order,
        "event": event,
        "source": {
            "box": source_box,
            "start": source_start,
            "end": source_end,
            "count": source_end - source_start,
            "interval": "half-open [start, end)",
        },
        "rows": rows,
        "totals": {
            "rows": len(rows),
            "products": total,
            "warehouse_products": warehouse_products,
            "shipped_products": shipped_products,
            "warehouse_boxes": len({_id_key(r["box"]) for r in rows
                                    if r["location_type"] == "warehouse"}),
            "shipped_boxes": len({_id_key(r["box"]) for r in rows
                                  if r["location_type"] == "shipment"}),
        },
    }


def _emit(obj, stream=None):
    stream = stream or sys.stdout
    stream.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def add_arguments(ap):
    """把 recall 命令的参数挂到给定解析器（供 timeline_tool.py 复用）。"""
    ap.add_argument("--db", required=True,
                    help="现有班组归属 SQLite 库（只读打开）")
    ap.add_argument("--work-order", required=True, help="质检事件的工单 ID")
    ap.add_argument("--event", required=True, help="质检事件 ID")
    ap.add_argument("--log", "--warehouse-log", dest="log", required=True,
                    help="仓库操作流水（JSON Lines）")
    ap.add_argument("--source-box", required=True, help="源箱 ID")
    ap.add_argument("--source-start", "--source-lo", dest="source_start",
                    required=True, type=int,
                    help="源箱原始位置区间起点（含）")
    ap.add_argument("--source-end", "--source-hi", dest="source_end",
                    required=True, type=int,
                    help="源箱原始位置区间终点（不含）")
    ap.add_argument("-o", "--output", help="输出文件（缺省写到 stdout）")
    ap.add_argument("--compact", action="store_true", help="不缩进输出")
    return ap


def build_parser():
    ap = argparse.ArgumentParser(
        description="质检召回范围追踪（只读 SQLite events 表 + 仓库 JSON Lines 流水）")
    return add_arguments(ap)


def cmd(args):
    try:
        result = recall(args.db, args.work_order, args.event, args.log,
                        args.source_box, args.source_start, args.source_end)
    except RecallError as ex:
        _emit({"status": "rejected", "errors": [ex.as_dict()]})
        return 2
    text = json.dumps(result, ensure_ascii=False,
                      indent=None if args.compact else 2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


def main(argv=None):
    return cmd(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
