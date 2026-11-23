#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
release_ledger.py — 质检放行账册（本地 SQLite）

回答两个复盘问题：
  1. 任一已入账批次结束时，工单能不能放行？依据是哪些凭证？
  2. 换掉一份凭证后，哪些签核不能再用？根因卡在哪个前置上？

模型
----
一张工单只有一份固定检查计划（首次入账时提供，之后不可改）。每个检查项绑定
该工单下的一个质检事件，事件以只读方式在外部班组归属库的 `events(work_order,
event)` 中核对；事件有没有时间锚不影响放行账。

计划为每个检查项定义可接受的前置组合：
  * 独立项没有任何前置组，登记凭证时不得带引用；
  * 有前置的项配置一组或多组替代方案，每组至少一项；登记新凭证时必须完整
    采用其中某一组（组内每个前置项各引用一份凭证），组内全部通过该组才成立；
  * 前置关系合起来不能成环，且只能引用本计划内的检查项。
计划至少声明一个最终放行必需项；所有必需项在批次结束时“有效且通过”才可放行。

批次
----
每个 JSON 入账批次只涉及一张工单，批次号是该工单内严格递增的正整数（可跳号）。
一批可对多个检查项登记新凭证（pass/fail）或撤销当前凭证；一项一批至多一个操作。
凭证 ID 在工单内永不复用。

  * 新凭证无论结论，都必须列出实际采用的一整组前置凭证 ID；被引用的必须是
    相应检查项在**本批结束时**仍登记（active）的通过凭证——同批登记的前置
    也可以引用，因此输入行排列不改变结果。
  * 撤销必须指向批前该项“最后登记且尚未撤销”的凭证（即使它自身已失效）；
    撤销不会让更早的凭证复活。
  * 新凭证取代同项旧凭证：旧凭证即使是通过也变为 superseded，依赖旧 ID 的
    下游签核随之失效；即使另一组前置已经通过，也不会改写旧签核当时引用的
    凭证，恢复有效性必须重新登记签核。
  * 历史凭证失效不是入账错误：合法的更换 / 撤销照常入账。
  * 任何入账错误都整批拒绝（不写本批任何内容，批次号不消耗），错误指出
    工单、批次和出错项；已入账历史照常可查。

存储
----
凭证全历史只追加、不删除（撤销 / 被取代只改 state），每个已入账批次另外写
一份“批次结束快照”。查账按 (工单, 批次号) 主键读一行快照，不重放历史、
不扫描其他工单；旧批次快照永不更新，之后的入账改变不了旧结论。

只依赖 Python 3 标准库。
"""

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from urllib.parse import quote

HERE = os.path.dirname(os.path.abspath(__file__))
SCHEMA_PATH = os.path.join(HERE, "release_ledger_schema.sql")

# 凭证/检查项状态（对外 JSON 用词）
ST_NEVER = "never_registered"   # 从未登记
ST_REVOKED = "revoked"           # 已撤销
ST_INVALID = "invalid"           # 凭证失效
ST_FAIL = "fail"                 # 有效但不通过
ST_PASS = "pass"                 # 有效且通过


class LedgerError(Exception):
    """入账 / 查询失败：带机器可读 code，尽量带工单/批次/检查项定位。"""

    def __init__(self, code, message, work_order=None, batch_seq=None,
                 item=None, **details):
        Exception.__init__(self, message)
        self.code = code
        self.message = message
        self.work_order = work_order
        self.batch_seq = batch_seq
        self.item = item
        self.details = details

    def as_dict(self):
        d = {"code": self.code, "message": self.message}
        if self.work_order is not None:
            d["work_order"] = self.work_order
        if self.batch_seq is not None:
            d["batch_seq"] = self.batch_seq
        if self.item is not None:
            d["item"] = self.item
        for k in sorted(self.details):
            d[k] = self.details[k]
        return d


class _DupDict(dict):
    """记录 JSON 重复键（重复键会让同一事实有歧义，直接拒绝）。"""


def _json_pairs_hook(pairs):
    d = _DupDict()
    dups = []
    for k, v in pairs:
        if k in d:
            dups.append(k)
        d[k] = v
    if dups:
        raise LedgerError(
            "duplicate_json_key",
            "duplicate object key(s): %s" % ", ".join(map(repr, dups)),
            keys=dups)
    return d


def load_json_file(path, what):
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except OSError as ex:
        raise LedgerError("io_error", "cannot read %s: %s" % (what, ex))
    try:
        obj = json.loads(raw.decode("utf-8"), object_pairs_hook=_json_pairs_hook)
    except LedgerError:
        raise
    except (UnicodeDecodeError, ValueError) as ex:
        raise LedgerError("invalid_json", "%s is not valid JSON: %s" % (what, ex))
    return obj, raw


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _nonempty_str(v):
    return isinstance(v, str) and v != ""


def _utcnow():
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.%f+00:00")


# ---------------------------------------------------------------------------
# 外部班组归属库：只读核对 events(work_order, event)
# ---------------------------------------------------------------------------

def _open_readonly(db_path):
    """三档只读打开，与 recall_tool 同口径：绝不写外部库。"""
    if not os.path.isfile(db_path):
        raise LedgerError("database_not_found",
                          "attribution database %r does not exist" % db_path)
    quoted = quote(os.path.abspath(db_path), safe="/")
    conn, errors = None, []
    for target, kwargs in (
            ("file:%s?mode=ro" % quoted, {"uri": True}),
            ("file:%s?mode=ro&immutable=1" % quoted, {"uri": True}),
            (db_path, {})):
        try:
            c = sqlite3.connect(target, timeout=30.0, **kwargs)
            c.execute("PRAGMA query_only = ON")
            c.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
            conn = c
            break
        except sqlite3.Error as ex:
            errors.append(str(ex))
            try:
                c.close()
            except Exception:
                pass
    if conn is None:
        raise LedgerError("invalid_database",
                          "cannot open attribution database %r: %s"
                          % (db_path, "; ".join(errors)))
    return conn


def check_events_exist(db_path, work_order, item_events):
    """只读确认每个 (工单, 事件) 都在外部 events 表中。不关心时间锚/归属。"""
    conn = _open_readonly(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='events'"
        ).fetchone()
        if row is None:
            raise LedgerError("invalid_database",
                              "attribution database %r has no events table"
                              % db_path)
        missing = []
        for item, event in item_events:
            hit = conn.execute(
                "SELECT 1 FROM events WHERE work_order=? AND event=? LIMIT 1",
                (work_order, event)).fetchone()
            if hit is None:
                missing.append({"item": item, "event": event})
        if missing:
            raise LedgerError(
                "event_not_found",
                "events not found in attribution database for work_order %r: %s"
                % (work_order, ", ".join(
                    "%s->%s" % (m["item"], m["event"]) for m in missing)),
                work_order=work_order, missing=missing)
    except sqlite3.Error as ex:
        raise LedgerError("invalid_database",
                          "cannot read events table from %r: %s"
                          % (db_path, ex))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 计划校验（纯函数，不落库）
# ---------------------------------------------------------------------------

def validate_plan(plan):
    """返回 (plan_info, problems)。plan_info 规范化计划，problems 为错误列表。

    前置组成员按检查项 ID 的集合去重比对；替代组之间不允许重复。
    环检测用三色 DFS（白/灰/黑），灰色节点再次出现即成环。
    """
    problems = []
    info = {"items": {}, "order": [], "required": [], "groups": {}}

    def err(code, message, item=None, **details):
        d = {"code": code, "message": message}
        if item is not None:
            d["item"] = item
        d.update(details)
        problems.append(d)

    if not isinstance(plan, dict):
        err("invalid_plan", "plan must be a JSON object")
        return info, problems

    raw_items = plan.get("items")
    if not isinstance(raw_items, list) or not raw_items:
        err("invalid_plan", "plan.items must be a non-empty list")
        return info, problems

    seen_items = set()
    for idx, raw in enumerate(raw_items):
        where = "plan.items[%d]" % idx
        if not isinstance(raw, dict):
            err("invalid_plan", "%s must be an object" % where)
            continue
        item = raw.get("item")
        if not _nonempty_str(item):
            err("invalid_item", "%s.item must be a non-empty string" % where)
            continue
        if item in seen_items:
            err("duplicate_item", "duplicate check item %r" % item, item=item)
            continue
        seen_items.add(item)
        event = raw.get("event")
        if not _nonempty_str(event):
            err("invalid_event",
                "item %r must bind a non-empty event id" % item, item=item)
        groups_raw = raw.get("prereq_groups", [])
        if groups_raw is None:
            groups_raw = []
        if not isinstance(groups_raw, list):
            err("invalid_prereq_groups",
                "item %r prereq_groups must be a list" % item, item=item)
            groups_raw = []
        norm_groups = []
        seen_group_sets = set()
        for g_idx, group in enumerate(groups_raw):
            gwhere = "item %r prereq_groups[%d]" % (item, g_idx)
            if not isinstance(group, list) or not group:
                err("empty_group",
                    "%s must be a non-empty list of item ids" % gwhere,
                    item=item)
                continue
            if any(not _nonempty_str(m) for m in group):
                err("invalid_prereq_item",
                    "%s contains a non-string/empty member" % gwhere,
                    item=item)
                continue
            if len(set(group)) != len(group):
                err("duplicate_group_member",
                    "%s lists the same prereq item twice" % gwhere,
                    item=item)
                continue
            gset = frozenset(group)
            if gset in seen_group_sets:
                err("duplicate_prereq_group",
                    "item %r defines the same alternative group twice: %s"
                    % (item, sorted(gset)), item=item)
                continue
            seen_group_sets.add(gset)
            norm_groups.append(tuple(group))
        info["items"][item] = {"event": event, "groups": norm_groups}
        info["order"].append(item)
        info["groups"][item] = norm_groups

    # 前置只能引用本计划内的检查项
    for item in info["order"]:
        for g_idx, group in enumerate(info["groups"][item]):
            for member in group:
                if member not in info["items"]:
                    err("unknown_prereq_item",
                        "item %r group %d references unknown item %r"
                        % (item, g_idx, member), item=item)

    # 必需项：至少一个，且都在计划内
    required = plan.get("required")
    if not isinstance(required, list) or not required:
        err("invalid_required",
            "plan.required must be a non-empty list of item ids")
    else:
        if any(not _nonempty_str(r) for r in required):
            err("invalid_required", "plan.required contains an invalid id")
        seen_required = set()
        for r in required:
            if not _nonempty_str(r):
                continue
            if r not in info["items"]:
                err("unknown_required_item",
                    "required item %r is not in plan.items" % r, item=r)
            elif r in seen_required:
                err("duplicate_required_item",
                    "required item %r is listed twice" % r, item=r)
            else:
                seen_required.add(r)
                info["required"].append(r)

    # 成环检测：沿“item -> 其前置项”的边做三色 DFS
    WHITE, GRAY, BLACK = 0, 1, 2
    color = {item: WHITE for item in info["order"]}
    cycle = []

    def dfs(node, stack):
        color[node] = GRAY
        for group in info["groups"].get(node, ()):
            for nxt in group:
                if nxt not in info["items"]:
                    continue  # 未知引用已在上面报过
                if color[nxt] == GRAY:
                    i0 = stack.index(nxt)
                    cycle.extend(stack[i0:] + [nxt])
                    return True
                if color[nxt] == WHITE and dfs(nxt, stack + [nxt]):
                    return True
        color[node] = BLACK
        return False

    for item in info["order"]:
        if color[item] == WHITE and dfs(item, [item]):
            break
    if cycle:
        err("prereq_cycle",
            "prerequisite relations form a cycle: %s"
            % " -> ".join(cycle), cycle=cycle)

    return info, problems


# ---------------------------------------------------------------------------
# 入账前的内存态
# ---------------------------------------------------------------------------

class Credential(object):
    __slots__ = ("cred_id", "item", "result", "reg_batch", "state",
                 "ended_batch", "revoke_batch", "group_idx", "refs")

    def __init__(self, cred_id, item, result, reg_batch, group_idx, refs):
        self.cred_id = cred_id
        self.item = item
        self.result = result                 # pass / fail
        self.reg_batch = reg_batch
        self.state = "active"                # active / superseded / revoked
        self.ended_batch = None
        self.revoke_batch = None
        self.group_idx = group_idx           # 采用的计划替代组下标；独立项为 None
        self.refs = refs                     # [(prereq_item, ref_cred_id, ord)]

    def as_history(self):
        return {
            "credential_id": self.cred_id,
            "item": self.item,
            "result": self.result,
            "state": self.state,
            "registered_batch": self.reg_batch,
            "ended_batch": self.ended_batch,
            "revoke_batch": self.revoke_batch,
        }


def load_current_state(conn, work_order):
    """把该工单的凭证全历史装进内存（只触碰本工单的行）。"""
    creds = {}
    rows = conn.execute(
        "SELECT credential_id, item, result, registered_batch, state, "
        "ended_batch, revoke_batch FROM rl_credentials "
        "WHERE work_order=? ORDER BY registered_batch, credential_id",
        (work_order,)).fetchall()
    for r in rows:
        c = Credential(r[0], r[1], r[2], r[3], None, [])
        c.state = r[4]
        c.ended_batch = r[5]
        c.revoke_batch = r[6]
        creds[c.cred_id] = c
    ref_rows = conn.execute(
        "SELECT credential_id, group_idx, prereq_item, ref_credential_id, ord "
        "FROM rl_credential_refs WHERE work_order=? "
        "ORDER BY credential_id, ord", (work_order,)).fetchall()
    ref_by_cred = {}
    group_by_cred = {}
    for cred_id, gidx, pitem, ref_id, ord_ in ref_rows:
        ref_by_cred.setdefault(cred_id, []).append((pitem, ref_id, ord_))
        group_by_cred[cred_id] = gidx
    for cred_id, refs in ref_by_cred.items():
        creds[cred_id].refs = refs
        creds[cred_id].group_idx = group_by_cred[cred_id]
    return creds


def _item_history(creds, item):
    """该项按入账顺序排列的全部凭证。"""
    return sorted((c for c in creds.values() if c.item == item),
                  key=lambda c: (c.reg_batch, c.cred_id))


def _last_non_revoked(history):
    """最后登记且尚未撤销的凭证（撤销操作的合法目标）。"""
    for c in reversed(history):
        if c.state != "revoked":
            return c
    return None


# ---------------------------------------------------------------------------
# 批次校验与应用
# ---------------------------------------------------------------------------

def _parse_refs(raw, where, problems, item):
    refs = []
    if raw is None:
        return refs
    if not isinstance(raw, list):
        problems.append({"code": "invalid_refs",
                         "message": "%s refs must be a list" % where,
                         "item": item})
        return None
    seen_items = set()
    for i, ref in enumerate(raw):
        rwhere = "%s refs[%d]" % (where, i)
        if not isinstance(ref, dict):
            problems.append({"code": "invalid_refs",
                             "message": "%s must be an object" % rwhere,
                             "item": item})
            continue
        pitem = ref.get("item", ref.get("prereq_item"))
        rid = ref.get("credential_id", ref.get("credential"))
        if not _nonempty_str(pitem) or not _nonempty_str(rid):
            problems.append({"code": "invalid_refs",
                             "message": "%s needs item and credential_id"
                                        % rwhere, "item": item})
            continue
        if pitem in seen_items:
            problems.append({"code": "duplicate_ref",
                             "message": "%s references item %r twice"
                                        % (rwhere, pitem), "item": item})
            continue
        seen_items.add(pitem)
        refs.append((pitem, rid))
    return refs


def validate_and_apply(payload, plan_info, creds, max_seq):
    """校验并在内存中应用一个批次。

    返回 (new_creds_list, revoked_list, n_register, n_revoke)；
    校验失败抛 LedgerError（带全部已收集的 item 级问题）。
    """
    problems = []
    wo = payload.get("work_order")
    seq = payload.get("batch_seq")
    if not _nonempty_str(wo):
        raise LedgerError("invalid_work_order",
                          "payload work_order must be a non-empty string")
    if not _is_int(seq) or seq <= 0:
        raise LedgerError("invalid_batch_seq",
                          "batch_seq must be a positive integer",
                          work_order=wo)
    if max_seq is not None and seq <= max_seq:
        raise LedgerError(
            "batch_seq_order",
            "batch_seq %d must be strictly greater than the latest posted "
            "batch %d for work_order %r" % (seq, max_seq, wo),
            work_order=wo, batch_seq=seq)

    ops = payload.get("operations")
    if not isinstance(ops, list) or not ops:
        raise LedgerError("empty_operations",
                          "a batch must contain at least one operation",
                          work_order=wo, batch_seq=seq)

    def err(code, message, item=None, **details):
        d = {"code": code, "message": message, "work_order": wo,
             "batch_seq": seq}
        if item is not None:
            d["item"] = item
        d.update(details)
        problems.append(d)

    # 一项一批至多一个操作
    op_by_item = {}
    parsed = []
    for idx, op in enumerate(ops):
        where = "operations[%d]" % idx
        if not isinstance(op, dict):
            err("invalid_operation", "%s must be an object" % where)
            continue
        action = op.get("action")
        item = op.get("item")
        if action not in ("register", "revoke"):
            err("unknown_action",
                "%s action must be register or revoke" % where, item=item)
            continue
        if not _nonempty_str(item):
            err("invalid_item", "%s.item must be a non-empty string" % where)
            continue
        if item not in plan_info["items"]:
            err("unknown_item", "item %r is not in the fixed plan" % item,
                item=item)
            continue
        if item in op_by_item:
            err("duplicate_item_op",
                "item %r has more than one operation in this batch" % item,
                item=item)
            continue
        op_by_item[item] = op
        parsed.append((where, action, item, op))

    # 先解析登记，再统一按本批结束态校验引用（与输入行排列无关）
    new_creds = []
    by_item_new = {}
    batch_cred_ids = set()
    for where, action, item, op in parsed:
        if action != "register":
            continue
        cred_id = op.get("credential_id")
        if not _nonempty_str(cred_id):
            err("invalid_credential_id",
                "%s: credential_id must be a non-empty string" % where,
                item=item)
            continue
        result = op.get("result")
        if result not in ("pass", "fail"):
            err("invalid_result",
                "credential %r result must be pass or fail" % cred_id,
                item=item)
            continue
        if cred_id in creds or cred_id in batch_cred_ids:
            err("credential_id_reused",
                "credential id %r is already used in work_order %r"
                % (cred_id, wo), item=item)
            continue
        batch_cred_ids.add(cred_id)
        refs = _parse_refs(op.get("refs", []), where, problems, item)
        if refs is None:
            continue
        plan_groups = plan_info["groups"][item]
        if not plan_groups:
            if refs:
                err("refs_not_allowed",
                    "item %r is independent (no prereq groups) but credential "
                    "%r lists refs" % (item, cred_id), item=item)
                continue
            new_creds.append(
                Credential(cred_id, item, result, seq, None, []))
            by_item_new[item] = cred_id
            continue
        if not refs:
            err("missing_prereq_group",
                "item %r credential %r must adopt one complete prereq group"
                % (item, cred_id), item=item)
            continue
        ref_item_set = frozenset(p for p, _r in refs)
        match_idx = next((i for i, g in enumerate(plan_groups)
                          if frozenset(g) == ref_item_set), None)
        if match_idx is None:
            err("unknown_prereq_group",
                "credential %r refs %s do not match any prereq group of item "
                "%r: %s" % (cred_id, sorted(ref_item_set), item,
                            [list(g) for g in plan_groups]), item=item)
            continue
        # 组内成员齐全（集合相等已保证），按计划组顺序固化引用
        order_index = {p: i for i, (p, _r) in enumerate(refs)}
        ordered_refs = [(p, dict(refs)[p], i)
                        for i, p in enumerate(plan_groups[match_idx])]
        c = Credential(cred_id, item, result, seq, match_idx, ordered_refs)
        new_creds.append(c)
        by_item_new[item] = cred_id

    # 撤销目标校验：必须是批前该项最后登记且尚未撤销的凭证
    revoked = []
    for where, action, item, op in parsed:
        if action != "revoke":
            continue
        target_id = op.get("credential_id")
        if not _nonempty_str(target_id):
            err("invalid_credential_id",
                "%s: revoke needs a credential_id" % where, item=item)
            continue
        history = _item_history(creds, item)
        target = next((c for c in history if c.cred_id == target_id), None)
        if target is None:
            err("revoke_unknown_credential",
                "credential %r was never registered for item %r"
                % (target_id, item), item=item)
            continue
        current = _last_non_revoked(history)
        if current is None:
            err("no_revokable_credential",
                "item %r has no registered, non-revoked credential to revoke"
                % item, item=item)
            continue
        if current.cred_id != target_id:
            err("stale_revoke",
                "revoke of %r is stale: the last registered non-revoked "
                "credential of item %r is %r"
                % (target_id, item, current.cred_id), item=item)
            continue
        revoked.append(target)

    if problems:
        raise LedgerError("rejected_batch",
                          "batch rejected with %d problem(s)" % len(problems),
                          work_order=wo, batch_seq=seq, errors=problems)

    # ---- 全部结构性检查通过，开始应用 ----
    # 1) 新凭证先入账（同批前置可被同批后账引用），旧 current 置 superseded
    for c in new_creds:
        history = _item_history(creds, c.item)
        current = _last_non_revoked(history)
        if current is not None:
            current.state = "superseded"
            current.ended_batch = seq
        creds[c.cred_id] = c

    # 2) 撤销
    for c in revoked:
        c.state = "revoked"
        c.ended_batch = seq
        c.revoke_batch = seq

    # 3) 按本批结束态校验每条引用：必须仍是该项 active 的通过凭证
    end_creds = list(new_creds)
    for c in end_creds:
        for pitem, rid, _ord in c.refs:
            target = creds.get(rid)
            if target is None or target.item != pitem:
                err("unknown_credential_ref",
                    "credential %r references unknown credential %r for item "
                    "%r" % (c.cred_id, rid, pitem), item=c.item)
                continue
            if target.state != "active":
                err("reference_not_active",
                    "credential %r references %r, which is %s at batch end "
                    "(not the registered credential of item %r)"
                    % (c.cred_id, rid, target.state, pitem), item=c.item)
                continue
            if target.result != "pass":
                err("reference_not_pass",
                    "credential %r references %r for item %r, whose result is "
                    "%s" % (c.cred_id, rid, pitem, target.result),
                    item=c.item)
    if problems:
        # 内存态改动随事务回滚一起丢弃（本函数外不会写库）；直接抛错
        raise LedgerError("rejected_batch",
                          "batch rejected with %d problem(s)" % len(problems),
                          work_order=wo, batch_seq=seq, errors=problems)

    return new_creds, revoked, len(new_creds), len(revoked)


# ---------------------------------------------------------------------------
# 批次结束态评估：有效性能沿引用链追到根因
# ---------------------------------------------------------------------------

def evaluate(creds, plan_info):
    """评估所有 active 凭证的有效性。

    引用图只沿 active 凭证展开（入账校验已保证），而计划无环 => 引用 DAG，
    递归必然终止。返回 eval_map: cred_id -> {"valid": bool, "problems": [...]}
    """
    eval_map = {}

    def active(cid):
        c = creds.get(cid)
        return c if c is not None and c.state == "active" else None

    def root_cause(c):
        """一份已不能当通过依据的凭证，根因是什么。"""
        if c.state == "revoked":
            return {"kind": "revoked", "item": c.item,
                    "credential_id": c.cred_id,
                    "revoke_batch": c.revoke_batch}
        if c.state == "superseded":
            newer = next((n for n in _item_history(creds, c.item)
                          if n.state == "active"), None)
            return {"kind": "superseded", "item": c.item,
                    "credential_id": c.cred_id,
                    "ended_batch": c.ended_batch,
                    "replaced_by": newer.cred_id if newer else None}
        if c.result == "fail":
            return {"kind": "fail", "item": c.item,
                    "credential_id": c.cred_id}
        return {"kind": "invalid", "item": c.item,
                "credential_id": c.cred_id}

    def eval_cred(c, path):
        if c.cred_id in eval_map:
            return eval_map[c.cred_id]
        problems = []
        for pitem, rid, _ord in c.refs:
            target = active(rid)
            edge_path = path + [{"item": c.item, "credential_id": c.cred_id},
                                {"item": pitem, "credential_id": rid}]
            if target is None:
                old = creds.get(rid)
                problems.append({
                    "prereq_item": pitem,
                    "ref_credential_id": rid,
                    "path": edge_path,
                    "root_cause": (root_cause(old) if old else
                                   {"kind": "missing", "item": pitem,
                                    "credential_id": rid}),
                })
                continue
            sub = eval_cred(target, path + [
                {"item": c.item, "credential_id": c.cred_id}])
            if not sub["valid"]:
                # 沿同一份结果继续向下追，直到根因：
                # eval 结果 -> problems[0]（前置问题）-> root_cause
                root = sub["problems"][0]["root_cause"]
                problems.append({
                    "prereq_item": pitem,
                    "ref_credential_id": rid,
                    "path": edge_path,
                    "root_cause": root,
                })
        result = {"valid": not problems, "problems": problems}
        eval_map[c.cred_id] = result
        return result

    for c in list(creds.values()):
        if c.state == "active":
            eval_cred(c, [])
    return eval_map


def build_snapshot(work_order, seq, plan_info, creds, eval_map):
    """构造批次结束快照（即查账返回主体）。"""
    items_out = []
    releasable = True
    required_set = set(plan_info["required"])

    for item in plan_info["order"]:
        history = _item_history(creds, item)
        active_cred = next((c for c in reversed(history)
                            if c.state == "active"), None)
        revoked_history = [c for c in history if c.state == "revoked"]
        last_rev = (max(revoked_history,
                        key=lambda c: (c.revoke_batch, c.cred_id))
                    if revoked_history else None)
        entry = {
            "item": item,
            "event": plan_info["items"][item]["event"],
            "required": item in required_set,
            "prereq_groups": [list(g) for g in plan_info["groups"][item]],
            "status": ST_NEVER,
            "active_credential": None,
            "last_revocation": (
                None if last_rev is None else
                {"credential_id": last_rev.cred_id,
                 "revoke_batch": last_rev.revoke_batch}),
            "revoked_credentials": [
                {"credential_id": c.cred_id, "revoke_batch": c.revoke_batch}
                for c in revoked_history],
            "registered_credentials": [c.as_history() for c in history],
        }

        if active_cred is None:
            entry["status"] = ST_REVOKED if history else ST_NEVER
        else:
            ev = eval_map[active_cred.cred_id]
            ref_view = [{
                "prereq_item": p,
                "credential_id": rid,
                "valid": (rid in eval_map and eval_map[rid]["valid"]),
            } for p, rid, _o in active_cred.refs]
            entry["active_credential"] = {
                "credential_id": active_cred.cred_id,
                "result": active_cred.result,
                "registered_batch": active_cred.reg_batch,
                "adopted_group_idx": active_cred.group_idx,
                "refs": ref_view,
                "problems": ev["problems"],
                "valid": ev["valid"],
            }
            if not ev["valid"]:
                entry["status"] = ST_INVALID
            elif active_cred.result == "fail":
                entry["status"] = ST_FAIL
            else:
                entry["status"] = ST_PASS

        if item in required_set and entry["status"] != ST_PASS:
            releasable = False
        items_out.append(entry)

    registered = []
    for c in sorted((c for c in creds.values() if c.state == "active"),
                    key=lambda c: (c.reg_batch, c.item, c.cred_id)):
        registered.append({
            "credential_id": c.cred_id,
            "item": c.item,
            "result": c.result,
            "registered_batch": c.reg_batch,
            "adopted_group_idx": c.group_idx,
            "refs": [{"prereq_item": p, "credential_id": rid}
                     for p, rid, _o in c.refs],
            "valid": eval_map[c.cred_id]["valid"],
        })

    basis = []
    for item in plan_info["required"]:
        entry = next(e for e in items_out if e["item"] == item)
        if entry["status"] == ST_PASS:
            ac = entry["active_credential"]
            basis.append({
                "item": item,
                "credential_id": ac["credential_id"],
                "refs": ac["refs"],
            })

    return {
        "work_order": work_order,
        "batch_seq": seq,
        "releasable": releasable,
        "plan": {
            "required": list(plan_info["required"]),
            "items": {
                item: {
                    "event": plan_info["items"][item]["event"],
                    "prereq_groups": [list(g)
                                      for g in plan_info["groups"][item]],
                } for item in plan_info["order"]},
        },
        "release_basis": basis,
        "items": items_out,
        "registered_credentials": registered,
        "status_legend": {
            ST_NEVER: "从未登记",
            ST_REVOKED: "已撤销（最后登记的凭证已撤销，旧凭证不复活）",
            ST_INVALID: "凭证失效（仍登记但引用链上有不符，见 problems 根因）",
            ST_FAIL: "有效但不通过",
            ST_PASS: "有效且通过",
        },
    }


# ---------------------------------------------------------------------------
# SQLite 连接与建库
# ---------------------------------------------------------------------------

def open_ledger(path, create=False):
    existed = os.path.exists(path)
    if not existed:
        if not create:
            raise LedgerError("missing_database",
                              "release ledger %r does not exist" % path)
        d = os.path.dirname(os.path.abspath(path)) or "."
        os.makedirs(d, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode = WAL")
    has_tables = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='rl_plans'"
    ).fetchone()
    if has_tables is None:
        if not create:
            raise LedgerError("invalid_database",
                              "%r is not a release ledger database" % path)
        conn.executescript(
            open(SCHEMA_PATH, "r", encoding="utf-8").read())
        conn.execute("INSERT OR IGNORE INTO rl_meta(key,value) VALUES(?,?)",
                     ("format_version", "1"))
        conn.execute("INSERT OR IGNORE INTO rl_meta(key,value) VALUES(?,?)",
                     ("generator", "release_ledger.py"))
    return conn


def _store_plan(conn, wo, plan_info, raw_plan, seq, when):
    plan_json = json.dumps(raw_plan, ensure_ascii=False, sort_keys=True)
    required_json = json.dumps(plan_info["required"], ensure_ascii=False)
    conn.execute(
        "INSERT INTO rl_plans (work_order, plan_json, required_json, "
        "created_batch_seq, created_at) VALUES (?,?,?,?,?)",
        (wo, plan_json, required_json, seq, when))
    conn.executemany(
        "INSERT INTO rl_plan_items (work_order, item, event, ord) "
        "VALUES (?,?,?,?)",
        [(wo, item, plan_info["items"][item]["event"], i)
         for i, item in enumerate(plan_info["order"])])
    for item in plan_info["order"]:
        for gidx, group in enumerate(plan_info["groups"][item]):
            conn.execute(
                "INSERT INTO rl_prereq_groups (work_order, item, group_idx) "
                "VALUES (?,?,?)", (wo, item, gidx))
            conn.executemany(
                "INSERT INTO rl_prereq_members (work_order, item, group_idx, "
                "prereq_item, ord) VALUES (?,?,?,?,?)",
                [(wo, item, gidx, member, i)
                 for i, member in enumerate(group)])


def _store_batch(conn, wo, seq, note, sha, new_creds, revoked, snapshot, when):
    n_reg = len(new_creds)
    n_rev = len(revoked)
    conn.execute(
        "INSERT INTO rl_batches (work_order, batch_seq, posted_at, note, "
        "input_sha256, n_register, n_revoke, releasable) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (wo, seq, when, note, sha, n_reg, n_rev,
         1 if snapshot["releasable"] else 0))
    for c in new_creds:
        conn.execute(
            "INSERT INTO rl_credentials (work_order, credential_id, item, "
            "result, registered_batch, state, ended_batch, revoke_batch) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (wo, c.cred_id, c.item, c.result, seq, "active", None, None))
        for pitem, rid, ord_ in c.refs:
            conn.execute(
                "INSERT INTO rl_credential_refs (work_order, credential_id, "
                "group_idx, prereq_item, ref_credential_id, ord) "
                "VALUES (?,?,?,?,?,?)",
                (wo, c.cred_id, c.group_idx, pitem, rid, ord_))
    for c in revoked:
        conn.execute(
            "UPDATE rl_credentials SET state='revoked', ended_batch=?, "
            "revoke_batch=? WHERE work_order=? AND credential_id=?",
            (seq, seq, wo, c.cred_id))
    # 被同批新凭证取代的旧 active 凭证（revoked 行已不是 active，不会被波及）
    conn.executemany(
        "UPDATE rl_credentials SET state='superseded', ended_batch=? "
        "WHERE work_order=? AND item=? AND state='active' AND credential_id<>?",
        [(seq, wo, c.item, c.cred_id) for c in new_creds])
    conn.execute(
        "INSERT INTO rl_snapshots (work_order, batch_seq, releasable, "
        "snapshot_json) VALUES (?,?,?,?)",
        (wo, seq, 1 if snapshot["releasable"] else 0,
         json.dumps(snapshot, ensure_ascii=False, sort_keys=True)))


# ---------------------------------------------------------------------------
# 入账命令
# ---------------------------------------------------------------------------

def post_batch(ledger_path, payload_path, attribution_db=None):
    payload, raw = load_json_file(payload_path, "batch payload")
    if not isinstance(payload, dict):
        raise LedgerError("invalid_payload", "batch payload must be an object")
    wo = payload.get("work_order")
    if not _nonempty_str(wo):
        raise LedgerError("invalid_work_order",
                          "payload work_order must be a non-empty string")
    seq = payload.get("batch_seq")
    if not _is_int(seq) or seq <= 0:
        raise LedgerError("invalid_batch_seq",
                          "batch_seq must be a positive integer",
                          work_order=wo)

    conn = open_ledger(ledger_path, create=True)
    try:
        known = conn.execute(
            "SELECT 1 FROM rl_plans WHERE work_order=?", (wo,)).fetchone()
        raw_plan = payload.get("plan")
        if known is not None:
            if raw_plan is not None:
                raise LedgerError(
                    "plan_conflict",
                    "work_order %r already has a fixed plan; a plan may only "
                    "be supplied with the first batch" % wo,
                    work_order=wo, batch_seq=seq)
            row = conn.execute(
                "SELECT plan_json FROM rl_plans WHERE work_order=?",
                (wo,)).fetchone()
            plan_info, problems = validate_plan(json.loads(row["plan_json"]))
            if problems:
                raise LedgerError("internal_inconsistency",
                                  "stored plan for %r is invalid" % wo,
                                  work_order=wo)
        else:
            if raw_plan is None:
                raise LedgerError(
                    "plan_required",
                    "first batch of work_order %r must carry the fixed plan"
                    % wo, work_order=wo, batch_seq=seq)
            plan_info, problems = validate_plan(raw_plan)
            if problems:
                raise LedgerError(
                    "invalid_plan",
                    "plan rejected with %d problem(s)" % len(problems),
                    work_order=wo, batch_seq=seq, errors=problems)
            if attribution_db is None:
                raise LedgerError(
                    "missing_attribution_db",
                    "provide --attribution-db so plan events can be checked "
                    "against the read-only events table",
                    work_order=wo, batch_seq=seq)
            check_events_exist(
                attribution_db, wo,
                [(item, plan_info["items"][item]["event"])
                 for item in plan_info["order"]])

        max_row = conn.execute(
            "SELECT MAX(batch_seq) AS m FROM rl_batches WHERE work_order=?",
            (wo,)).fetchone()
        max_seq = max_row["m"]
        if max_seq is not None and seq <= max_seq:
            raise LedgerError(
                "batch_seq_order",
                "batch_seq %d must be strictly greater than the latest posted "
                "batch %d for work_order %r" % (seq, max_seq, wo),
                work_order=wo, batch_seq=seq)

        # 全历史装入内存（仅本工单）
        creds = load_current_state(conn, wo)
        new_creds, revoked, n_reg, n_rev = validate_and_apply(
            payload, plan_info, creds, max_seq)
        eval_map = evaluate(creds, plan_info)

        # 入账校验的最后一道：引用在本批结束时不仅要 active+pass，还必须自身
        # 有效（其前置链没有失效）。否则这份新凭证从入账起就是“凭证失效”，
        # 属于无效的新引用，整批拒绝。
        ref_problems = []
        for c in new_creds:
            for pitem, rid, _ord in c.refs:
                target = creds[rid]
                if not eval_map[rid]["valid"]:
                    root_cause = eval_map[rid]["problems"][0]["root_cause"]
                    ref_problems.append({
                        "code": "reference_not_valid",
                        "message": ("credential %r references %r for item %r, "
                                    "but that credential is itself invalid at "
                                    "batch end" % (c.cred_id, rid, pitem)),
                        "work_order": wo, "batch_seq": seq, "item": c.item,
                        "root_cause": root_cause,
                    })
        if ref_problems:
            raise LedgerError(
                "rejected_batch",
                "batch rejected with %d problem(s)" % len(ref_problems),
                work_order=wo, batch_seq=seq, errors=ref_problems)

        snapshot = build_snapshot(wo, seq, plan_info, creds, eval_map)
        when = _utcnow()
        sha = hashlib.sha256(raw).hexdigest()

        # 整批一个事务：任何失败都回滚，库里不留半笔
        conn.execute("BEGIN IMMEDIATE")
        try:
            if known is None:
                _store_plan(conn, wo, plan_info, raw_plan, seq, when)
                conn.execute(
                    "INSERT OR IGNORE INTO rl_meta(key,value) VALUES(?,?)",
                    ("attribution_db:%s" % wo, attribution_db))
            _store_batch(conn, wo, seq, payload.get("note"), sha,
                         new_creds, revoked, snapshot, when)
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                pass
            raise
    finally:
        conn.close()

    return {
        "status": "posted",
        "work_order": wo,
        "batch_seq": seq,
        "registered": n_reg,
        "revoked": n_rev,
        "releasable": snapshot["releasable"],
        "ledger": os.path.abspath(ledger_path),
    }


# ---------------------------------------------------------------------------
# 查账命令
# ---------------------------------------------------------------------------

def query_batch(ledger_path, work_order, batch_seq):
    if not _is_int(batch_seq) or batch_seq <= 0:
        raise LedgerError("invalid_batch_seq",
                          "batch_seq must be a positive integer",
                          work_order=work_order)
    conn = open_ledger(ledger_path, create=False)
    try:
        wo_row = conn.execute(
            "SELECT 1 FROM rl_plans WHERE work_order=?",
            (work_order,)).fetchone()
        if wo_row is None:
            raise LedgerError("unknown_work_order",
                              "work_order %r has never posted a batch; an "
                              "unknown work order is not an empty ledger"
                              % work_order, work_order=work_order)
        row = conn.execute(
            "SELECT s.snapshot_json, b.posted_at, b.note, b.input_sha256, "
            "b.n_register, b.n_revoke FROM rl_snapshots s JOIN rl_batches b "
            "ON b.work_order=s.work_order AND b.batch_seq=s.batch_seq "
            "WHERE s.work_order=? AND s.batch_seq=?",
            (work_order, batch_seq)).fetchone()
        if row is None:
            raise LedgerError(
                "unknown_batch",
                "batch %d is not posted for work_order %r (it is not an "
                "empty ledger)" % (batch_seq, work_order),
                work_order=work_order, batch_seq=batch_seq)
        snapshot = json.loads(row["snapshot_json"])
        snapshot["batch_meta"] = {
            "posted_at": row["posted_at"],
            "note": row["note"],
            "input_sha256": row["input_sha256"],
            "n_register": row["n_register"],
            "n_revoke": row["n_revoke"],
        }
        return snapshot
    finally:
        conn.close()


def list_ledger(ledger_path):
    conn = open_ledger(ledger_path, create=False)
    try:
        plans = conn.execute(
            "SELECT work_order, created_batch_seq, created_at FROM rl_plans "
            "ORDER BY work_order").fetchall()
        out = {"status": "ok", "work_orders": []}
        for p in plans:
            batches = conn.execute(
                "SELECT batch_seq, posted_at, releasable, n_register, "
                "n_revoke, note FROM rl_batches WHERE work_order=? "
                "ORDER BY batch_seq", (p["work_order"],)).fetchall()
            out["work_orders"].append({
                "work_order": p["work_order"],
                "plan_created_batch": p["created_batch_seq"],
                "plan_created_at": p["created_at"],
                "batches": [dict(b) for b in batches],
            })
        return out
    finally:
        conn.close()


def run_readonly_sql(ledger_path, sql_text):
    conn = sqlite3.connect(
        "file:%s?mode=ro" % quote(os.path.abspath(ledger_path), safe="/"),
        uri=True)
    try:
        conn.execute("PRAGMA query_only = ON")
        cur = conn.execute(sql_text)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [dict(zip(cols, r)) for r in cur.fetchall()] if cols else []
        return {"status": "ok", "columns": cols, "rows": rows}
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def _emit(obj, compact=False, stream=None):
    stream = stream or sys.stdout
    stream.write(json.dumps(obj, ensure_ascii=False,
                            indent=None if compact else 2) + "\n")


def add_arguments(ap):
    """挂到 timeline_tool.py 的 release 子命令上。"""
    sub = ap.add_subparsers(dest="release_command", required=True)

    p_init = sub.add_parser("init", help="创建空的放行账册 SQLite 文件")
    p_init.add_argument("--db", required=True)
    p_init.set_defaults(_func=cmd_init)

    p_post = sub.add_parser("post", help="入账一个检查计划/凭证批次（整批原子）")
    p_post.add_argument("--db", required=True)
    p_post.add_argument("--batch", required=True, help="批次 JSON 文件")
    p_post.add_argument("--attribution-db",
                        help="班组归属 SQLite 库（首批带计划时必填，只读）")
    p_post.add_argument("-o", "--output")
    p_post.add_argument("--compact", action="store_true")
    p_post.set_defaults(_func=cmd_post)

    p_query = sub.add_parser("query", help="按工单 + 已入账批次号查账")
    p_query.add_argument("--db", required=True)
    p_query.add_argument("--work-order", required=True)
    p_query.add_argument("--batch", required=True, type=int)
    p_query.add_argument("-o", "--output")
    p_query.add_argument("--compact", action="store_true")
    p_query.set_defaults(_func=cmd_query)

    p_list = sub.add_parser("list", help="列出账册内工单与已入账批次")
    p_list.add_argument("--db", required=True)
    p_list.add_argument("--compact", action="store_true")
    p_list.set_defaults(_func=cmd_list)

    p_sql = sub.add_parser("sql", help="对账册执行只读 SQL")
    p_sql.add_argument("--db", required=True)
    p_sql.add_argument("--sql", required=True)
    p_sql.add_argument("--compact", action="store_true")
    p_sql.set_defaults(_func=cmd_sql)
    return ap


def cmd_init(args):
    open_ledger(args.db, create=True).close()
    _emit({"status": "initialized", "ledger": os.path.abspath(args.db)})
    return 0


def cmd_post(args):
    try:
        result = post_batch(args.db, args.batch, args.attribution_db)
    except LedgerError as ex:
        _emit({"status": "rejected", "errors": [ex.as_dict()]},
              compact=args.compact)
        return 2
    except sqlite3.Error as ex:
        _emit({"status": "rejected",
               "errors": [{"code": "database_error", "message": str(ex)}]},
              compact=args.compact)
        return 2
    _emit(result, compact=args.compact)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(json.dumps(result, ensure_ascii=False,
                               indent=None if args.compact else 2) + "\n")
    return 0


def cmd_query(args):
    try:
        result = query_batch(args.db, args.work_order, args.batch)
    except LedgerError as ex:
        _emit({"status": "rejected", "errors": [ex.as_dict()]},
              compact=args.compact)
        return 2
    text = json.dumps(result, ensure_ascii=False,
                      indent=None if args.compact else 2)
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    else:
        print(text)
    return 0


def cmd_list(args):
    _emit(list_ledger(args.db), compact=args.compact)
    return 0


def cmd_sql(args):
    try:
        result = run_readonly_sql(args.db, args.sql)
    except sqlite3.Error as ex:
        _emit({"status": "rejected",
               "errors": [{"code": "sql_error", "message": str(ex)}]},
              compact=args.compact)
        return 2
    _emit(result, compact=args.compact)
    return 0


def build_parser():
    ap = argparse.ArgumentParser(
        description="质检放行账册（本地 SQLite，标准库）")
    return add_arguments(ap)


def main(argv=None):
    args = build_parser().parse_args(argv)
    return args._func(args)


if __name__ == "__main__":
    sys.exit(main())
