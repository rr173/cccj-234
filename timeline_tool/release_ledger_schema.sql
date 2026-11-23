-- release_ledger_schema.sql — 放行账册层（本地 SQLite）
--
-- 一张工单一份固定检查计划（首次入账时提供），之后按工单内严格递增的批次号
-- 登记 / 撤销凭证。每个已入账批次在同一事务里写两类数据：
--
--   1. 凭证全历史（rl_credentials / rl_credential_refs）：撤销不删行，
--      新凭证取代旧凭证只把旧行置为 superseded，因此“当时引用了什么”永远可查；
--   2. 批次结束快照（rl_snapshots）：查账按主键读一行即可，不重放历史、
--      不扫描其他工单。旧批次快照写入后永不改写。
--
-- 被拒绝的入账不写任何表（命令以非零退出 + rejected JSON 报告工单/批次/出错项）。
--
-- 事件只做只读核对：检查项绑定的 (工单, 事件) 必须存在于外部班组归属库的
-- events 表；本库不复制事件、不关心时间锚。

PRAGMA foreign_keys = ON;

CREATE TABLE rl_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- 每工单一份计划，首次入账后固定
CREATE TABLE rl_plans (
    work_order        TEXT PRIMARY KEY,
    plan_json         TEXT NOT NULL
        CHECK (json_valid(plan_json) AND json_type(plan_json) = 'object'),
    required_json     TEXT NOT NULL
        CHECK (json_valid(required_json) AND json_type(required_json) = 'array'),
    created_batch_seq INTEGER NOT NULL CHECK (created_batch_seq > 0),
    created_at        TEXT NOT NULL CHECK (length(created_at) > 0)
);

-- 检查项 -> 该工单下的质检事件（只读核对自外部 events 表）
CREATE TABLE rl_plan_items (
    work_order TEXT NOT NULL,
    item       TEXT NOT NULL CHECK (item <> ''),
    event      TEXT NOT NULL CHECK (event <> ''),
    ord        INTEGER NOT NULL CHECK (ord >= 0),
    PRIMARY KEY (work_order, item),
    FOREIGN KEY (work_order) REFERENCES rl_plans (work_order)
);

-- 前置替代组：一个 item 可有 0..N 组；独立项没有任何组行
CREATE TABLE rl_prereq_groups (
    work_order TEXT NOT NULL,
    item       TEXT NOT NULL,
    group_idx  INTEGER NOT NULL CHECK (group_idx >= 0),
    PRIMARY KEY (work_order, item, group_idx),
    FOREIGN KEY (work_order, item) REFERENCES rl_plan_items (work_order, item)
);

-- 组成员：组内所有项都必须有效通过，该组才算满足
CREATE TABLE rl_prereq_members (
    work_order  TEXT NOT NULL,
    item        TEXT NOT NULL,
    group_idx   INTEGER NOT NULL,
    prereq_item TEXT NOT NULL,
    ord         INTEGER NOT NULL CHECK (ord >= 0),
    PRIMARY KEY (work_order, item, group_idx, ord),
    FOREIGN KEY (work_order, item, group_idx)
        REFERENCES rl_prereq_groups (work_order, item, group_idx),
    FOREIGN KEY (work_order, prereq_item)
        REFERENCES rl_plan_items (work_order, item)
);

-- 已入账批次（只有成功入账的批次才有行；被拒绝的批次不消耗批次号、不留行）
CREATE TABLE rl_batches (
    work_order TEXT NOT NULL,
    batch_seq  INTEGER NOT NULL CHECK (batch_seq > 0),
    posted_at  TEXT NOT NULL CHECK (length(posted_at) > 0),
    note       TEXT,
    input_sha256 TEXT,
    n_register INTEGER NOT NULL CHECK (n_register >= 0),
    n_revoke   INTEGER NOT NULL CHECK (n_revoke >= 0),
    releasable INTEGER NOT NULL CHECK (releasable IN (0, 1)),
    PRIMARY KEY (work_order, batch_seq),
    FOREIGN KEY (work_order) REFERENCES rl_plans (work_order)
);

-- 凭证全历史。state 只在后续批次前进：active -> superseded / revoked
CREATE TABLE rl_credentials (
    work_order      TEXT NOT NULL,
    credential_id   TEXT NOT NULL CHECK (credential_id <> ''),
    item            TEXT NOT NULL,
    result          TEXT NOT NULL CHECK (result IN ('pass', 'fail')),
    registered_batch INTEGER NOT NULL CHECK (registered_batch > 0),
    state           TEXT NOT NULL CHECK (state IN ('active', 'superseded', 'revoked')),
    -- 仍登记（active）时两列均为 NULL；结束批次 = 被取代或撤销的批次
    ended_batch     INTEGER CHECK (ended_batch IS NULL OR ended_batch >= registered_batch),
    revoke_batch    INTEGER,
    PRIMARY KEY (work_order, credential_id),
    FOREIGN KEY (work_order, item) REFERENCES rl_plan_items (work_order, item),
    FOREIGN KEY (work_order, registered_batch)
        REFERENCES rl_batches (work_order, batch_seq),
    CHECK (
        (state = 'active' AND ended_batch IS NULL AND revoke_batch IS NULL)
        OR
        (state = 'superseded' AND ended_batch IS NOT NULL AND revoke_batch IS NULL)
        OR
        (state = 'revoked' AND ended_batch IS NOT NULL
         AND revoke_batch = ended_batch)
    )
);

CREATE INDEX ix_rl_cred_item ON rl_credentials (work_order, item, registered_batch);

-- 一份凭证实际采用的某一组完整前置：每行一个引用，ord 为组内顺序
CREATE TABLE rl_credential_refs (
    work_order        TEXT NOT NULL,
    credential_id     TEXT NOT NULL,
    group_idx         INTEGER NOT NULL CHECK (group_idx >= 0),
    prereq_item       TEXT NOT NULL,
    ref_credential_id TEXT NOT NULL,
    ord               INTEGER NOT NULL CHECK (ord >= 0),
    PRIMARY KEY (work_order, credential_id, ord),
    FOREIGN KEY (work_order, credential_id)
        REFERENCES rl_credentials (work_order, credential_id),
    FOREIGN KEY (work_order, prereq_item)
        REFERENCES rl_plan_items (work_order, item)
);

-- 批次结束快照：查账只读这一行（按主键），旧快照永不更新
CREATE TABLE rl_snapshots (
    work_order    TEXT NOT NULL,
    batch_seq     INTEGER NOT NULL,
    releasable    INTEGER NOT NULL CHECK (releasable IN (0, 1)),
    snapshot_json TEXT NOT NULL
        CHECK (json_valid(snapshot_json) AND json_type(snapshot_json) = 'object'),
    PRIMARY KEY (work_order, batch_seq),
    FOREIGN KEY (work_order, batch_seq)
        REFERENCES rl_batches (work_order, batch_seq)
);
