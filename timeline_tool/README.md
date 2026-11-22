# timeline_tool — 质检事件时间线离线入仓工具

车间多个工位的质检事件归到同一条时间线，但历史导出只给**时间区间**和**事件间延迟范围**。
本工具不把区间中点当事实，而是把证据建模为差分约束系统，离线交付：

- 同一工单证据**能同时成立**时：每个事件的**最早 / 最晚可行时刻**（精确整数毫秒），
  每个有限端点附一条**可从原始证据逐步复核的推导链**（路径证据）；
- 证据**互相矛盾**时：**隔离该工单**，给出能**单独证明不可满足**的负环证书（不要求最短）；
- **坏引用 / 逆置区间 / 合法但不可满足**三类问题分开报告（另含兜底 `malformed_record`）；
- 按需回答**指定事件对** `A<B` / `A=B` / `A>B` 是否各自存在，每个存在的关系附一份
  **满足该工单全部证据的时刻赋值**。**不预建、不交付全事件对比较矩阵**：
  每次查询只做 ≤3 次 Bellman-Ford（O(事件数 × 证据数)），与未查询的事件对无关。

只依赖 Python 3 标准库，不访问任何线上服务。

## 模型

每条证据翻译成两个半约束 `t_v - t_u ≤ w`（`@zero` 为恒 0 的人工参考变量）：

| 证据 | 含义 | 半约束 |
|---|---|---|
| 绝对 `[lo,hi]` on x | `lo ≤ t_x ≤ hi` | `t_x - t_@zero ≤ hi`，`t_@zero - t_x ≤ -lo` |
| 相对 before=b after=a `[lo,hi]` | `lo ≤ t_a − t_b ≤ hi` | `t_a − t_b ≤ hi`，`t_b − t_a ≤ −lo` |

- 同一对事件上的多条证据**不互相覆盖**，全部进入系统、必须同时成立；
- 自引用按同一逻辑处理：`m→m [lo,hi]` 等价于要求 `0 ∈ [lo,hi]`；
- 约束系统存在负环 ⟺ 不可满足；无负环时，
  最晚时刻 = `@zero` 到事件的最短路，最早时刻 = 事件到 `@zero` 最短路的相反数；
- **无绝对锚的连通片**可相对排序，但绝对上下界均为 `null`（未定义，绝不写成 0）；
- 各工单相互独立处理。

## 输入格式

```json
{
  "work_orders": [
    {"id": "WO-1",
     "events": ["s", "x", "y"],
     "absolute": [{"id": "a1", "event": "s", "lo": 1000, "hi": 1000, "source": "mes"}],
     "relative": [{"id": "r1", "before": "s", "after": "x", "lo": 50, "hi": 200, "source": "plc"}]}
  ],
  "queries": [{"work_order": "WO-1", "a": "x", "b": "y"}]
}
```

`id`/`source` 可缺省（证据 id 缺省自动编为 `abs1..`/`rel1..`，source 缺省为 `unknown`）。
时间必须是精确整数毫秒；负数合法。

## 命令

```bash
cd timeline_tool
python3 timeline_tool.py run examples/case1.json            # 结果打印到 stdout
python3 timeline_tool.py run examples/case1.json -o out.json --compact
python3 verify_enum.py examples/case5_small.json            # 独立枚举核对
python3 verify_enum.py examples/*.json --fuzz 150 --seed 7  # 加随机小工单模糊核对
```

## 可复跑小例：间接延迟决定先后，撤一条证据关系候选即改变

`examples/case1.json`：x、y 各自的绝对区间都很宽 `[0,10000]`，但共享锚
`s=1000` 与延迟 `s→x∈[50,200]`、`s→y∈[800,950]` 已决定先后。

```bash
python3 timeline_tool.py run examples/case1.json
```

输出（节选）——时间界表 + 路径证据：

```json
{"id": "WO-1", "status": "ok",
 "bounds": [
   {"event": "s", "earliest": 1000, "latest": 1000, ...},
   {"event": "x", "earliest": 1050, "latest": 1200,
    "latest_proof": {"derived": "t_x <= 1200", "total_weight": 1200,
      "steps": [
        {"from": "@zero", "to": "s", "weight": 1000, "evidence": "a1", "source": "mes", "kind": "abs_hi", ...},
        {"from": "s", "to": "x", "weight": 200, "evidence": "r1", "source": "plc", "kind": "rel_hi", ...}]},
    ...},
   {"event": "y", "earliest": 1800, "latest": 1950, ...}]
```

查询结果——只有 `A<B` 存在，并附满足全部证据的赋值：

```json
{"work_order": "WO-1", "a": "x", "b": "y",
 "relations": {
   "A<B": {"possible": true,  "assignment": {"s": 1000, "x": 1200, "y": 1800}},
   "A=B": {"possible": false},
   "A>B": {"possible": false}}}
```

撤掉证据 `r2` 再跑（`examples/case1b.json`），关系候选从 `{A<B}` 变为三种都存在：

```bash
python3 timeline_tool.py run examples/case1b.json
# "A<B": possible true  (witness y=1051)
# "A=B": possible true  (witness y=1050)
# "A>B": possible true  (witness y=1049)
```

## 其余核对样例

```bash
python3 timeline_tool.py run examples/case2.json   # 无锚分量 + 跨分量比较
python3 timeline_tool.py run examples/case3.json   # 仅经环路才能发现的矛盾
python3 timeline_tool.py run examples/case4.json   # 坏引用/逆置区间/自引用不可满足 的区分
python3 timeline_tool.py run examples/case5_small.json  # 小整数窗口（供全枚举核对）
```

- **case2**：`p,q` 仅有相对延迟 → 上下界为 `null` 并带说明；`z` 无任何证据同样为 `null`。
  `(p,q)` 只有 `A<B`；跨分量对 `(a,p)`、`(a,z)`、`(p,z)` 三种关系都存在
  （无锚片可整体平移）。
- **case3**：`e1→e2→e3→e1` 各要求间隔 ≥1ms，两两各自合法，合起来要求 `0≥3`。
  工单被隔离，输出负环证书：

```json
{"status": "quarantined", "error_kind": "infeasible",
 "contradiction": {"type": "negative_cycle", "cycle": ["e1","e3","e2","e1"],
   "steps": [{"from":"e1","to":"e3","weight":-1,"evidence":"c3","source":"mes",...},
             {"from":"e3","to":"e2","weight":-1,"evidence":"c2","source":"andon",...},
             {"from":"e2","to":"e1","weight":-1,"evidence":"c1","source":"plc",...}],
   "total_weight": -3,
   "derived": "summing the cycle constraints gives 0 <= -3, which is false; ..."}}
```

- **case4**：`WO-4A`→`bad_reference`（证据指向目录外事件 `ghost`）；
  `WO-4B`→`inverted_interval`（`[900,100]`）；`WO-4C`→`infeasible`
  （自引用 `[3,5]` 要求 `0∈[3,5]`，负环长 1）；`WO-4D` 正常
  （自引用 `[-2,4]` 合法且冗余，按同一逻辑通过）。
- **case5_small**：小整数窗口版本，含同对事件多条证据（`WO-8` 的 `m→n`
  同时受 `[0,9]` 与 `[2,5]` 约束，取交集后 `n∈[3,9]`）。

## 独立核对器 verify_enum.py

不复用求解器的约束传播逻辑，独立做四件事：

1. **小整数窗口全枚举**：窗口取安全外扩 `R = Σ|相对证据 lo,hi|`；
   有锚片事件 ∈ `[min(abs.lo)−R, max(abs.hi)+R]`，无锚片平移不变故固定代表元为 0、
   其余 ∈ `[−R,R]`。枚举全部可行赋值，独立算出每个事件两端与每个查询对的三种关系，
   与求解器逐一比对（窗口乘积超限时跳过枚举，但仍做下面 2、3 项）；
   跨分量且不全有锚的事件对由平移论证直接判定三种关系都存在；
2. **证书重放**：每条路径证据/负环证书逐步对照原始证据重新推导，
   确认端点/矛盾确实由这些证据推出；
3. **见证复核**：每个 `possible: true` 交付的赋值逐条对照原始证据验证，
   并确认确实实现了所声称的关系；
4. **分类核对**：三类隔离原因独立重判。

实测（本仓库 examples + 每次 150~300 个随机小工单，多种子）全部通过，例如：

```
stats: {"certs": 55, "enum_checked": 88, "enum_skipped": 2, "proofs": 238,
        "quarantined": 72, "queries": 179, "witnesses": 323, "work_orders": 162}
ALL CHECKS PASSED
```

## 输出字段说明

- `work_orders[].status`: `ok` | `quarantined`；
- `error_kind`: `malformed_record` | `bad_reference` | `inverted_interval` | `infeasible`；
- `bounds[].earliest/latest`: 整数毫秒或 `null`（无锚片，未定义，不是 0）；
- `*_proof.steps[]`: 每步 `from/to/weight/meaning/evidence/source/kind`，
  链上权重之和即被推导的端点；
- `contradiction.steps[]`: 沿环权重之和 `< 0`，单凭列出的证据即可证不可满足；
- `queries[].relations["A<B"|"A=B"|"A>B"]`: `possible` + 存在时的
  `assignment`（满足该工单全部证据的一份赋值；无锚事件的绝对取值是某一合法平移，
  只有差值受约束，输出中附 `note` 说明）。


---

# 班组归属层（attribution_tool.py）

`timeline_tool.py` 回答“事件最早/最晚什么时候发生”；归属层在这个结果之上回答
“**当时可能由哪个班组负责这台设备**”。归属层独立落一个本地 SQLite 文件，
可用任意 SQL 客户端直接读取。

## 事实来源与口径

- 事件时间事实**只取自 `run` 的 JSON 输出**（`--run-output` 直接读已有文件，
  或 `--timeline` 在本层调用现有 `run`）；不重新取证、不取区间中点。
- 有锚事件保留**闭区间** `[earliest, latest]`（整数毫秒）；无锚事件记
  `time_status='unlocatable'`，**不参与班表匹配**，`teams=[]`，覆盖/空档/冲突
  三列为 `NULL`——这与“有锚但没有任何班次覆盖”（`has_gap=1`）严格区分。
- 设备绑定清单按 `(工单, 事件)` 绑定设备。正常工单的**每个事件恰好绑定一台设备**
  （缺绑定或同一事件绑到两台设备都整批拒绝）；**隔离工单不生成归属行**。
- 班次是**半开区间** `[start_ms, end_ms)`，覆盖整数毫秒 `start_ms .. end_ms-1`：
  含起点、不含终点。`priority` 为整数，越大越优先。
- 所有时间/优先级都是 SQLite `INTEGER`（有符号 64 位）；入口拒绝 `bool`、
  浮点、字符串与 `[−2^63, 2^63−1]` 之外的值，不做浮点近似。

## 归属规则

对每个有锚事件，检查其闭区间内**每一个**整数毫秒：

1. 只看该设备当时有效的**最高优先级**班次；
2. 同优先级的多个班次若同属一个班组，不算冲突；不同班组则**全部保留**并
   记 `has_conflict=1`；
3. 先后两班（不同时刻各自唯一）不算冲突；
4. 交付一行：可能班组去重集合、`has_coverage`（是否至少有一毫秒有班次）、
   `has_gap`（是否存在没有任何班次覆盖的时刻）、`has_conflict`。

实现上**不逐毫秒执行连接**：每台设备的断点集合 = 所有班次起止点之并，相邻断点
之间有效班次集合不变；把事件区间与这些“段”做区间连接即可（段数 ≤ 2×班次数）。
因此跨长时间段的事件同样可处理，见 `v_segments` / `v_event_attribution` 视图。

## 建表

```bash
cd timeline_tool
python3 attribution_tool.py build \
    --db work.db \
    --timeline examples/attribution/timeline.json \
    --manifest examples/attribution/manifest.json \
    --shifts   examples/attribution/shifts.json
# 也可以直接吃已有 run 输出：
python3 timeline_tool.py run examples/attribution/timeline.json -o /tmp/run.json
python3 attribution_tool.py build --db work.db --run-output /tmp/run.json \
    --manifest examples/attribution/manifest.json
```

`build` 先在临时文件里建库，成功后才原子替换目标文件；失败不会留下半个库。

清单形状（`bindings` 与 `work_orders` 两种写法等价，可同时出现）：

```json
{"bindings": {"WO-A": {"e_cross": "PRS-01"}},
 "work_orders": {"WO-B": {"events": {"e_conflict": "CLN-02"}}}}
```

## SQL 读取

```bash
python3 attribution_tool.py show --db work.db --device PRS-01
python3 attribution_tool.py sql  --db work.db \
  --sql "SELECT event, teams_json, has_gap, has_conflict, max_priority
         FROM attributions WHERE device = 'PRS-01' ORDER BY event"
python3 attribution_tool.py sql --db work.db \
  --sql "SELECT * FROM v_segments WHERE team_count > 1"
python3 attribution_tool.py gaps --db work.db          # 设备无班次覆盖区间
python3 attribution_tool.py drift --db work.db         # 物化归属 vs 当前班表（应为 0）
python3 attribution_tool.py touch --db work.db --device PRS-01 --lo 16000 --hi 17999
```

`sql` 子命令以 `mode=ro` 打开数据库并设 `PRAGMA query_only=ON`，只读。

核心表：

| 表 / 视图 | 内容 |
|---|---|
| `work_orders` / `events` | `run` 输出与抽出的事件时间事实 |
| `device_bindings` | `(工单, 事件) → 设备` |
| `shifts` | 班表，主键 `(设备, 班次ID)`，半开区间 |
| `attributions` | 每个事件一行归属结果（物化） |
| `batches` | 建仓 / 每次更正的批次（同事务提交） |
| `v_segments` | 按断点切出的时段及其最高优先级班组集合 |
| `v_gap_intervals` | 设备跨度内无班次覆盖的闭区间 |
| `v_event_attribution` | 归属的单一事实来源（分段计算） |
| `v_attribution_drift` | 物化归属与按当前班表重算结果的差异（正常为空） |

数据库默认 WAL 模式：读连接在更正提交前看到旧快照，提交后看到新状态。

## 更正：一次改多班、整批原子

```bash
python3 attribution_tool.py correct --db work.db --revision rev.json
```

```json
{"note": "补上 [16000,18000) 的历史空档",
 "shifts": [
   {"action": "add", "device": "PRS-01", "id": "NIGHT", "team": "丁班",
    "start_ms": 16000, "end_ms": 18000, "priority": 0}]}
```

- `replace` 用同一 `(设备, 班次ID)` 整行替换时段、优先级或班组；`add` 新增；
  `delete` 按 ID 删除。
- 更正**复用已存事件事实**，不重跑时序取证。
- 候选事件在 **SQL 侧按“设备 + 时间相交”** 查出（`device_bindings` ⋈ `events`，
  用有锚区间与改动时段相交），不把整台设备历史载入程序。
- 只有归属结果真正变化的事件才写回；与改动时段**不相交**的事实既不重算也不写回
  （其 `computed_batch` 保持不变）。班表本身有实际改动（哪怕归属没变）也会提交批次，
  这样“更正后从零重建”的结果与库内状态一致。
- 以前没匹配到班次的事件（例如落在空档里）也在候选条件内，会被重新归属。
- 班表、归属与批次行在**同一次 `BEGIN IMMEDIATE` 事务**提交；读取者只会看到
  旧状态或新状态。批中任一非法记录（空班次/逆置、重复操作同一 ID、删除不存在
  的班次、超 64 位、非整数等）→ 整批回滚，**班表、归属、批次号都不变**。
- 可选 `--expect-seq N`：要求当前最大批次号等于 N，用于防止并发更正相互覆盖。

## 跨交接班实例：先补空档、再撤代班

演示数据在 `examples/attribution/`（先 `build` 时带 `--shifts`，或如“建表”一节
先用空班表建库再更正）。`e_cross` 的时间界是 `[15000, 21000]`，
设备 `PRS-01` 的班表覆盖了交接班、历史空档与两段外协代班：

| 时刻 | 有效班次（最高优先级） |
|---|---|
| 0–8000 | DAY / 甲班，优先级 0 |
| 8000–16000 | EVE / 乙班，优先级 0 |
| 14000–16500 | DELEG-1 / 外协W，优先级 10（覆盖 EVE） |
| 16000–18000 | **无班次（历史空档）** |
| 18000–20000 | LATE / 丙班，优先级 0 |
| 20000–22000 | DELEG-2 / 外协W，优先级 10（覆盖 LATE） |

初始归属：`teams=[丙班, 外协W]`，`has_gap=1`，`has_conflict=0`
（先后两班都可能负责 ≠ 冲突）。

1. **补上历史空档**（`revision_fill_gap.json`，加 NIGHT 16000–18000）：
   `teams=[丁班, 丙班, 外协W]`，`has_gap=0`。
2. **撤掉 [14000,16500) 的外协代班**（`revision_revoke_delegation.json`，
   删除 DELEG-1）：`teams=[丁班, 丙班, 乙班, 外协W]`，`has_gap=0`。
   —— 外协W 因另一段时间仍有 DELEG-2 而**继续保留**在该事件的候选里；
   撤掉的只是前半段代班。

```bash
python3 attribution_tool.py correct --db work.db \
  --revision examples/attribution/revision_fill_gap.json
python3 attribution_tool.py show --db work.db --event e_cross
python3 attribution_tool.py correct --db work.db \
  --revision examples/attribution/revision_revoke_delegation.json
python3 attribution_tool.py show --db work.db --event e_cross
```

同时可以看到：

- `e_before`@7999 → 只有 甲班；`e_after`@8000 → 只有 乙班
  （班次含起点、不含终点）。
- `e_tie`@1500 → 甲组两班并列，`has_conflict=0`；
  `e_conflict`@500 → 甲组与乙组同优先级并列，`has_conflict=1`，两组都保留。
- `e_seq`@[1500,2500] → `[丙组, 甲组]`，先后两班，`has_conflict=0`。
- `e_noanchor` → `unlocatable`，三个标志列 `NULL`；
  `e_noshift`（设备无任何班次）→ 有锚、`has_coverage=0`、`has_gap=1`。

## 独立核对 `verify_attribution.py` / `verify_attribution_full.py`

不复用分段视图，独立做五件事：

```bash
python3 verify_attribution.py --db work.db
# stats: {"batch_checks": 1, "drift_checks": 1, "events_checked": 8,
#         "illegal_batches": 10, "isolation": 1, "ms_per_ms": 7,
#         "no_rewrite": 1, "rebuilds": 1, ...}
# ALL ATTRIBUTION CHECKS PASSED
```

1. **小窗口逐毫秒参考**：对每个事件在 `[earliest, latest]` 内**逐整数毫秒**
   直接判定覆盖，与库内按段结果逐字段比对；窗口超过 `--max-ms`（默认 20000）
   时改用独立写的扫描线（按毫秒断点增删有效班次，不合并时段），跨度再大也不
   退化，结果语义仍是逐毫秒覆盖。
2. **交接端点**：每个班次起点必须被自己覆盖、终点那一毫秒必须已不属于自己；
   恰在终点的单毫秒事件不得由旧班负责。
3. **全量重建一致**：用当前班表从零生成整套归属，与增量结果逐行比对；并检查
   `computed_batch` 不早于与该事件时段相交的班次的最近更新批次。
4. **非法批次原子性**：现场构造 10 类非法更正（空班次、逆置、重复操作同一 ID、
   删除不存在班次、i64 溢出、浮点时间、布尔优先级、空班组、先合法后非法等），
   逐批确认被拒且班表/归属内容哈希与批次号完全不变。
5. **隔离性**：用一个 `--hold-ms` 打开写事务，另一连接在写入前后读取，确认
   看不见未提交改动、提交后立刻可见。
6. **不写回**：在设备所有事件时段之外加一个探针班次再撤掉，确认变动事件数为
   0、整表 `attributions` 内容哈希不变（不相交的事实不重算、不写回）。
7. **漂移**：`v_attribution_drift` 必须为空——物化结果与按当前班表重算一致。

## 原有 run 保持可用

`timeline_tool.py`、`verify_enum.py` 与 `examples/*.json` 未改动；
`python3 timeline_tool.py run examples/case1.json`、`python3 verify_enum.py
examples/*.json --fuzz 150 --seed 7` 仍然可用。

## 全流程核对 `verify_attribution_full.py`

```bash
python3 verify_attribution_full.py                      # examples/attribution 全流程
python3 verify_attribution_full.py --fuzz 60 --seed 5    # 随机设备 + 随机更正序列
```

它**完全不复用** `timeline_tool` 或归属层代码：

- 自包含 Bellman-Ford 独立重算每个事件的最早/最晚可行时刻；
- 在内存里重放示例的初始班表与两次更正，与库内 `shifts` 逐行比对；
- 用独立扫描线逐毫秒语义重建整套归属，与库内逐事件、逐字段比对
  （即“更正后 = 从更正后班表重新建仓”）；
- 随机生成小整数窗口的设备/班次/事件，逐毫秒枚举优先级并列、同班组并列、
  交接端点、空档与冲突；
- 随机生成连续更正序列（add/replace/delete），每步后全表重算比对。
