-- attribution_views.sql — 便于 SQL 直接读取的视图（不含递归/写操作）
--
--   v_device_queries     带秒/毫秒换算的便利字段，便于人读
--   v_gap_intervals      每台设备“无任何班次覆盖”的闭区间（设备跨度内）
--   v_event_times        每个事件的时间事实（与 run 输出对应）
--   v_attribution_touch  更正影响扇出所需的旧交点（见 attribution_tool.py 注释）

CREATE VIEW v_device_queries AS
SELECT a.work_order, a.event, a.device, a.time_status,
       a.earliest AS earliest_ms, a.latest AS latest_ms,
       a.earliest / 1000 AS earliest_s, a.earliest % 1000 AS earliest_ms_part,
       a.latest  / 1000 AS latest_s,  a.latest  % 1000 AS latest_ms_part,
       a.teams_json, a.team_count,
       a.has_coverage, a.has_gap, a.has_conflict, a.max_priority,
       a.shift_ids_json
FROM attributions a
ORDER BY a.device, a.work_order, a.event;

-- 设备跨度内没有班次覆盖的闭区间 [gap_start, gap_end]（整数毫秒，闭区间）
CREATE VIEW v_gap_intervals AS
WITH span AS (SELECT * FROM v_device_span),
bnd AS (
    SELECT device, t FROM v_shift_boundaries
    UNION ALL
    SELECT device, dmin FROM span
    UNION ALL
    SELECT device, dmax FROM span
),
b2 AS (
    SELECT device, t, LEAD(t) OVER (PARTITION BY device ORDER BY t) AS next_t
    FROM bnd
),
gap AS (
    SELECT b2.device, b2.t AS seg_start, b2.next_t AS seg_end
    FROM b2
    WHERE b2.next_t IS NOT NULL AND b2.t < b2.next_t
      AND NOT EXISTS (
          SELECT 1 FROM shifts sh
          WHERE sh.device = b2.device
            AND sh.start_ms <= b2.t AND sh.end_ms >= b2.next_t)
)
SELECT device, seg_start AS gap_start, seg_end - 1 AS gap_end
FROM gap
WHERE seg_end - 1 >= seg_start
ORDER BY device, gap_start;

-- 更正时用于判断“哪些事件可能受影响”的旧交点：
--   设备 D 的每个事件（有锚）在旧班表下的每个覆盖段都列一行；
--   查询这些行即可得到新班次时段可能触达的 (work_order, event) 种子。
CREATE VIEW v_attribution_touch AS
SELECT b.work_order, b.event, b.device,
       s.seg_start, s.seg_end
FROM events e
JOIN work_orders w ON w.work_order = e.work_order AND w.status = 'ok'
JOIN device_bindings b ON b.work_order = e.work_order AND b.event = e.event
JOIN v_segments s
  ON s.device = b.device
 AND s.seg_start <= e.latest
 AND s.seg_end   >  e.earliest
WHERE e.time_status = 'anchored';

-- 与 run 输出一一对应的事件时间事实（便于人工核对）
CREATE VIEW v_event_times AS
SELECT work_order, event, time_status, earliest, latest,
       CASE time_status WHEN 'anchored'
            THEN earliest || '..' || latest ELSE 'unlocatable' END AS span_text
FROM events
ORDER BY work_order, event;


-- 漂移检查：物化表与“当前班表 + 事件事实”重新计算的结果不一致的行。
-- 正常路径（build / correct）下应为空；直接改 shifts 表而不走 correct 才会出现。
CREATE VIEW v_attribution_drift AS
SELECT 'missing_or_stale' AS kind, v.work_order, v.event
FROM v_event_attribution v
LEFT JOIN attributions a
  ON a.work_order = v.work_order AND a.event = v.event
WHERE a.work_order IS NULL
   OR a.device IS NOT v.device
   OR a.time_status IS NOT v.time_status
   OR a.earliest IS NOT v.earliest
   OR a.latest IS NOT v.latest
   OR a.teams_json IS NOT v.teams_json
   OR a.team_count IS NOT v.team_count
   OR a.has_coverage IS NOT v.has_coverage
   OR a.has_gap IS NOT v.has_gap
   OR a.has_conflict IS NOT v.has_conflict
   OR a.max_priority IS NOT v.max_priority
   OR a.shift_ids_json IS NOT v.shift_ids_json
UNION ALL
SELECT 'orphan', a.work_order, a.event
FROM attributions a
LEFT JOIN v_event_attribution v
  ON v.work_order = a.work_order AND v.event = a.event
WHERE v.work_order IS NULL;
