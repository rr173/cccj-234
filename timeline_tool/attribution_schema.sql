-- attribution_schema.sql — 班组归属层（SQLite，精确 64 位整数毫秒）
--
-- 设计要点：
--   * 所有时间/优先级都是 SQLite INTEGER（有符号 64 位）；CHECK typeof(...)='integer'
--     拒绝浮点、文本等非整数存储。Python 入口另外做 i64 范围校验。
--   * 班次是半开区间 [start_ms, end_ms)：覆盖整数毫秒 start_ms .. end_ms-1。
--     事件时间界是闭区间 [earliest, latest]。
--   * 归属按“时间分段”计算，而不是逐毫秒连接：每个设备的断点集合是所有班次的
--     起点与终点之并；相邻断点之间覆盖集合不变。事件区间与这些段做区间连接。
--   * 每段只看该设备当时有效的最高优先级班次；同优先级不同班组 => 冲突；
--     同优先级同班组 => 不冲突。先后两班（不同时刻各自唯一）不构成冲突。
--   * 无锚事件 time_status='unlocatable'：teams=[]，覆盖/空档/冲突三列为 NULL，
--     与“有锚但没有任何班次覆盖”（has_gap=1）明确区分。

PRAGMA foreign_keys = ON;

CREATE TABLE meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE batches (
    batch_id        INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_key       TEXT UNIQUE,
    kind            TEXT NOT NULL CHECK (kind IN ('build', 'revision')),
    created_at      TEXT NOT NULL CHECK (length(created_at) > 0),
    note            TEXT,
    timeline_sha256 TEXT,
    manifest_sha256 TEXT
);

-- 首次建仓时保存的 timeline_tool.py `run` 输出（每个工单一行）
CREATE TABLE work_orders (
    work_order   TEXT PRIMARY KEY,
    status       TEXT NOT NULL CHECK (status IN ('ok', 'quarantined')),
    error_kind   TEXT,
    result_json  TEXT NOT NULL CHECK (json_valid(result_json) AND json_type(result_json) = 'object')
);

-- 从 run 输出抽取的事件时间事实（有锚 = 闭区间；无锚 = 两列 NULL）
CREATE TABLE events (
    work_order  TEXT NOT NULL,
    event       TEXT NOT NULL,
    time_status TEXT NOT NULL CHECK (time_status IN ('anchored', 'unlocatable')),
    earliest    INTEGER,
    latest      INTEGER,
    bounds_json TEXT NOT NULL CHECK (json_valid(bounds_json) AND json_type(bounds_json) = 'object'),
    PRIMARY KEY (work_order, event),
    FOREIGN KEY (work_order) REFERENCES work_orders (work_order),
    CHECK (
        (time_status = 'unlocatable' AND earliest IS NULL AND latest IS NULL)
        OR
        (time_status = 'anchored'
         AND typeof(earliest) = 'integer' AND typeof(latest) = 'integer'
         AND earliest <= latest)
    )
);

-- (工单, 事件) -> 设备；只允许绑定正常工单的事件
CREATE TABLE device_bindings (
    work_order TEXT NOT NULL,
    event      TEXT NOT NULL,
    device     TEXT NOT NULL CHECK (device <> ''),
    PRIMARY KEY (work_order, event),
    FOREIGN KEY (work_order, event) REFERENCES events (work_order, event)
);

CREATE INDEX ix_bindings_device ON device_bindings (device, work_order, event);

-- 班表：主键 (设备, 班次ID)；同一 ID 的一次更正 = 整行替换
CREATE TABLE shifts (
    device       TEXT NOT NULL CHECK (device <> ''),
    shift_id     TEXT NOT NULL CHECK (shift_id <> ''),
    team         TEXT NOT NULL CHECK (team <> ''),
    start_ms     INTEGER NOT NULL CHECK (typeof(start_ms) = 'integer'),
    end_ms       INTEGER NOT NULL CHECK (typeof(end_ms) = 'integer'),
    priority     INTEGER NOT NULL CHECK (typeof(priority) = 'integer'),
    updated_batch INTEGER NOT NULL REFERENCES batches (batch_id),
    PRIMARY KEY (device, shift_id),
    CHECK (start_ms < end_ms)
);

CREATE INDEX ix_shifts_time ON shifts (device, start_ms, end_ms);

-- 设备断点（班次起止之并）与相邻断点之间的分段
CREATE VIEW v_shift_boundaries AS
    SELECT device, start_ms AS t FROM shifts
    UNION
    SELECT device, end_ms   AS t FROM shifts;

-- 分段结果只依赖班表；attributions 为物化结果，读取示例见 README。
CREATE VIEW v_segments AS
WITH bnd AS (
    SELECT device, t, LEAD(t) OVER (PARTITION BY device ORDER BY t) AS next_t
    FROM v_shift_boundaries
),
seg AS (
    SELECT device, t AS seg_start, next_t AS seg_end
    FROM bnd WHERE next_t IS NOT NULL
),
cov AS (
    SELECT sg.device, sg.seg_start, sg.seg_end,
           sh.shift_id, sh.team, sh.priority,
           MAX(sh.priority) OVER (PARTITION BY sg.device, sg.seg_start) AS mx
    FROM seg sg
    JOIN shifts sh
      ON sh.device = sg.device
     AND sh.start_ms <= sg.seg_start
     AND sh.end_ms   >= sg.seg_end
),
win AS (
    SELECT DISTINCT device, seg_start, seg_end, shift_id, team, mx
    FROM cov WHERE priority = mx
)
SELECT sg.device, sg.seg_start, sg.seg_end,
       (SELECT MAX(mx) FROM win w
         WHERE w.device = sg.device AND w.seg_start = sg.seg_start) AS max_priority,
       (SELECT COUNT(*) FROM win w
         WHERE w.device = sg.device AND w.seg_start = sg.seg_start) AS cover_count,
       (SELECT COUNT(DISTINCT team) FROM win w
         WHERE w.device = sg.device AND w.seg_start = sg.seg_start) AS team_count,
       (SELECT COALESCE(json_group_array(x.team), '[]') FROM
            (SELECT DISTINCT team FROM win w
              WHERE w.device = sg.device AND w.seg_start = sg.seg_start
              ORDER BY team) x) AS teams_json,
       (SELECT COALESCE(json_group_array(x.shift_id), '[]') FROM
            (SELECT DISTINCT shift_id FROM win w
              WHERE w.device = sg.device AND w.seg_start = sg.seg_start
              ORDER BY shift_id) x) AS shift_ids_json
FROM seg sg;

CREATE VIEW v_device_span AS
    SELECT device, MIN(start_ms) AS dmin, MAX(end_ms) AS dmax
    FROM shifts GROUP BY device;

-- 由事件事实 + 设备绑定 + 班表，按“段”聚合出的归属（单一事实来源）
CREATE VIEW v_event_attribution AS
WITH b AS (
    SELECT e.work_order, e.event, db.device,
           e.time_status, e.earliest, e.latest
    FROM events e
    JOIN work_orders w
      ON w.work_order = e.work_order AND w.status = 'ok'
    JOIN device_bindings db
      ON db.work_order = e.work_order AND db.event = e.event
),
h AS (
    SELECT b.work_order, b.event, b.device,
           s.seg_start, s.seg_end, s.max_priority,
           s.cover_count, s.team_count, s.teams_json, s.shift_ids_json
    FROM b
    JOIN v_segments s
      ON s.device = b.device
     AND s.seg_start <= b.latest
     AND s.seg_end   >  b.earliest
    WHERE b.time_status = 'anchored'
),
g AS (
    SELECT work_order, event,
           MAX(max_priority)                    AS max_priority,
           SUM(CASE WHEN cover_count > 0 THEN 1 ELSE 0 END) AS n_covered,
           SUM(CASE WHEN cover_count = 0 THEN 1 ELSE 0 END) AS n_gap,
           SUM(CASE WHEN team_count  > 1 THEN 1 ELSE 0 END) AS n_conflict
    FROM h
    GROUP BY work_order, event
)
SELECT b.work_order, b.event, b.device, b.time_status, b.earliest, b.latest,
       CASE WHEN b.time_status = 'unlocatable' THEN '[]' ELSE
            (SELECT COALESCE(json_group_array(x.team), '[]') FROM
                 (SELECT DISTINCT j.value AS team
                    FROM h h2, json_each(h2.teams_json) j
                   WHERE h2.work_order = b.work_order AND h2.event = b.event
                   ORDER BY team) x)
       END AS teams_json,
       CASE WHEN b.time_status = 'unlocatable' THEN 0 ELSE
            (SELECT COUNT(DISTINCT j.value)
               FROM h h3, json_each(h3.teams_json) j
              WHERE h3.work_order = b.work_order AND h3.event = b.event)
       END AS team_count,
       CASE WHEN b.time_status = 'unlocatable' THEN NULL
            WHEN COALESCE(g.n_covered, 0) > 0 THEN 1 ELSE 0 END AS has_coverage,
       CASE WHEN b.time_status = 'unlocatable' THEN NULL
            WHEN sp.device IS NULL THEN 1
            WHEN b.earliest < sp.dmin OR b.latest >= sp.dmax THEN 1
            WHEN COALESCE(g.n_gap, 0) > 0 THEN 1 ELSE 0 END AS has_gap,
       CASE WHEN b.time_status = 'unlocatable' THEN NULL
            WHEN COALESCE(g.n_conflict, 0) > 0 THEN 1 ELSE 0 END AS has_conflict,
       g.max_priority,
       CASE WHEN b.time_status = 'unlocatable' THEN '[]' ELSE
            (SELECT COALESCE(json_group_array(x.sid), '[]') FROM
                 (SELECT DISTINCT j.value AS sid
                    FROM h h4, json_each(h4.shift_ids_json) j
                   WHERE h4.work_order = b.work_order AND h4.event = b.event
                   ORDER BY sid) x)
       END AS shift_ids_json
FROM b
LEFT JOIN g  ON g.work_order = b.work_order AND g.event = b.event
LEFT JOIN v_device_span sp ON sp.device = b.device;

-- 归属表：物化结果；写入总是与班表/批次在同一事务提交
CREATE TABLE attributions (
    work_order     TEXT NOT NULL,
    event          TEXT NOT NULL,
    device         TEXT NOT NULL,
    time_status    TEXT NOT NULL CHECK (time_status IN ('anchored', 'unlocatable')),
    earliest       INTEGER,
    latest         INTEGER,
    teams_json     TEXT NOT NULL CHECK (json_valid(teams_json) AND json_type(teams_json) = 'array'),
    team_count     INTEGER NOT NULL CHECK (typeof(team_count) = 'integer' AND team_count >= 0),
    has_coverage   INTEGER CHECK (has_coverage IN (0, 1)),
    has_gap        INTEGER CHECK (has_gap IN (0, 1)),
    has_conflict   INTEGER CHECK (has_conflict IN (0, 1)),
    max_priority   INTEGER CHECK (max_priority IS NULL OR typeof(max_priority) = 'integer'),
    shift_ids_json TEXT NOT NULL CHECK (json_valid(shift_ids_json) AND json_type(shift_ids_json) = 'array'),
    computed_batch INTEGER NOT NULL REFERENCES batches (batch_id),
    PRIMARY KEY (work_order, event),
    FOREIGN KEY (work_order, event) REFERENCES events (work_order, event),
    CHECK (
        (time_status = 'unlocatable'
         AND earliest IS NULL AND latest IS NULL
         AND has_coverage IS NULL AND has_gap IS NULL AND has_conflict IS NULL
         AND max_priority IS NULL AND team_count = 0
         AND teams_json = '[]' AND shift_ids_json = '[]')
        OR
        (time_status = 'anchored'
         AND typeof(earliest) = 'integer' AND typeof(latest) = 'integer'
         AND earliest <= latest
         AND has_coverage IN (0, 1) AND has_gap IN (0, 1) AND has_conflict IN (0, 1)
         AND (has_coverage = 1 OR has_gap = 1))
    )
);

CREATE INDEX ix_attributions_device ON attributions (device, work_order, event);
CREATE INDEX ix_attributions_teams  ON attributions (team_count);
