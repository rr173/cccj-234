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

---

# 召回清单层（recall_tool.py / `timeline_tool.py recall`）

质检员在仓库里定位到一条异常事件，并从现场确认原箱中需要召回的**连续区间**。
原箱可能已被拆装：部分产品还在库（可能换过箱、退回过），部分已随别的箱出货。
本层给出**现在**应扣住的库存位置和应联系的出货单，且**不能把同箱的正常产品算进去**。

- 只读打开现有班组归属 SQLite 库，仅确认 `events` 表里 `(工单, 事件)` 存在；
  **是否有时间锚、是否有设备归属都不影响召回**（`unlocatable`、无 binding 的事件同样可查）。
- 仓库操作流水是 JSON Lines；结果带有所选事件、源区间、按当前箱 ID 与起点排序的明细。
- 明细每一行是某个当前箱内**一段最大连续**的召回位置；`location_type` 为
  `warehouse`（附 `warehouse`）或 `shipment`（附 `shipment` 与 `ship_seq`）。
- 同一件产品只在其当前去向出现一次：换箱、多次退回不会重复计入。
- 缺口或未命中位置隔开的两段**不合并**；箱在库但已被抽走的位置不是库存。

## 流水格式（字段可自行命名，本实现支持的名称如下）

每行一个 JSON 对象，`seq` 唯一正整数、按文件顺序严格递增（可跳号）。
位置从 0 开始，区间一律半开 `[start, end)`。

| 操作 | 必填字段 | 语义 |
|---|---|---|
| `inbound` | `box`, `warehouse`, `count` | 在新仓建箱，位置 `[0, count)`，`count` 正整数 |
| `repack` | `box`, `warehouse`, `takes:[{box,lo,hi}]` | 从**同一仓**在库旧箱按 `takes` 顺序取区间，装入新箱自 0 连续编号；段内顺序不变；旧箱只失去被取区间，留下的位置不变；同一箱可多段但不可重叠、不可取已拆走位置 |
| `ship` | `box`, `shipment` | 把一个非空在库箱的全部剩余产品交给出货单，不重编号；同一箱同时只能有一次未退回出货 |
| `return` | `box`, `ship_seq`, `warehouse` | 按出货记录 `seq` 退回；必须正是该箱当前尚未退回的那次出货；整箱进指定仓，保留出货时剩余位置，之后可再换箱/出货 |

本实现接受的别名：`op`/`type`/`action`，`box`/`box_id`，`warehouse`/`warehouse_id`/`wh`，
`count`/`quantity`/`pieces`，`takes`/`sources`/`from`，take 内 `lo`/`start`、`hi`/`end`，
`shipment`/`shipment_id`/`order`，`ship_seq`/`shipment_seq`。同一字段的多个别名值不同即拒绝。
箱/仓/出货单 ID 可写字符串或整数（`7` 与 `"7"` 视为不同 ID）；`--source-box`
是文本参数，纯整数的写法会再去匹配整数 ID。

## 命令与可复跑示例

```bash
cd timeline_tool
# 1) 建一个演示库（也可用已有任意班组归属库；这里只借 events 表）
python3 attribution_tool.py build --db /tmp/recall_demo.db --force \
    --timeline examples/attribution/timeline.json \
    --manifest examples/attribution/manifest.json \
    --shifts   examples/attribution/shifts.json

# 2) 选中事件 WO-A/e_cross，源箱 B-IN 的原始位置 [1, 10)
python3 timeline_tool.py recall --db /tmp/recall_demo.db \
    --work-order WO-A --event e_cross \
    --log examples/recall/warehouse.jsonl \
    --source-box B-IN --source-start 1 --source-end 10
# 等价入口：python3 recall_tool.py 后接同样参数

# 3) 结果写入文件（已提交 out/recall_example.out.json）
python3 timeline_tool.py recall --db /tmp/recall_demo.db \
    --work-order WO-A --event e_cross \
    --log examples/recall/warehouse.jsonl \
    --source-box B-IN --source-start 1 --source-end 10 \
    -o out/recall_example.out.json --compact

# 4) 反例：同一条流水末尾重复使用已退回的出货 seq 70 → 非零退出、列出 line/seq
python3 timeline_tool.py recall --db /tmp/recall_demo.db \
    --work-order WO-A --event e_cross \
    --log examples/recall/warehouse_bad_double_return.jsonl \
    --source-box B-IN --source-start 1 --source-end 10
# {"status":"rejected","errors":[{"code":"shipment_already_returned",
#   "message":"ship seq 70 was already returned by seq 90 ...","line":11,"seq":110}]}
```

示例流水 `examples/recall/warehouse.jsonl`（`seq` 故意跳号）覆盖了：
混装后再次拆箱（B1→B2）、原箱的剩余与退回（B-IN 退回 W3）、移入新箱后再出货
（B1 在 S-A 上）、以及同箱但不在源区间的正常产品（B-MIX 的部分产品）。

输出（节选）：

```json
{"status": "ok", "work_order": "WO-A", "event": "e_cross",
 "source": {"box": "B-IN", "start": 1, "end": 10, "count": 9},
 "rows": [
   {"box": "B-IN", "start": 7, "end": 8, "count": 1,
    "location_type": "warehouse", "warehouse": "W3",
    "shipment": null, "ship_seq": null},
   {"box": "B-IN", "start": 9, "end": 10, "count": 1, ...},
   {"box": "B1", "start": 4, "end": 6, "count": 2,
    "location_type": "shipment", "shipment": "S-A", "ship_seq": 60},
   {"box": "B2", "start": 0, "end": 3, "count": 3, ...},
   {"box": "B3", "start": 0, "end": 1, "count": 1, ...},
   {"box": "B3", "start": 2, "end": 3, "count": 1, ...}],
 "totals": {"rows": 6, "products": 9, "warehouse_products": 7,
            "shipped_products": 2, "warehouse_boxes": 3, "shipped_boxes": 1}}
```

要点：

- B-IN 的原始位置 0..5 已装进 B1，其中 1..4 又进了 B2；B1 位置 4..6 是源箱
  0 位置的 4,5，现在在出货单 S-A 上 → 只报这一段，且带出货 `seq` 60。
- B-IN 剩 6..7、8..9 被拆走 7..8 的缺口隔开 → 两行，均标 W3 库存。
- B-MIX 的产品虽混入 B1，但不在源区间 → 不出现在清单里。

## 校验与错误

非法输入**整批拒绝**：非零退出、不输出部分清单，错误带 `line`（行号）和/或 `seq`：

| code | 情形 |
|---|---|
| `seq_order` / `duplicate_seq` / `invalid_field` / `invalid_json` | 流水顺序、seq、字段类型问题 |
| `unknown_box` / `unknown_source_box` / `box_id_reused` | 引用未知箱、源箱不在流水、箱 ID 复用 |
| `take_overlap` / `take_gap` / `take_out_of_range` / `warehouse_mismatch` | 换箱取段重叠、取缺口/已拆走、越界、跨仓 |
| `box_not_in_warehouse` / `empty_box_ship` | 从已出货箱取、空箱出货 |
| `unknown_ship_seq` / `return_box_mismatch` / `shipment_already_returned` / `box_not_shipped` | 退回引用了不存在/别的箱/已结束的出货，或箱并不在出货中 |
| `event_not_found` / `invalid_database` | `events` 表不存在或 `(工单, 事件)` 不存在 |
| `empty_source_range` / `invalid_source_range` / `source_range_out_of_bounds` | 源区间为空、逆向、越界 |

## 复杂度

每个箱只保存“当前位置区间 → 出生位置区间”的分段映射；一条覆盖一万亿个连续位置
的 `inbound` 只是一段。换箱只为实际取到的片段增加分段数，召回也只触碰与选中
出生区间相交的片段。内存与时间随流水记录数和区间片段数增长，**不随件数增长**。

```bash
python3 verify_recall.py                      # 固定小流水 + 错误流水 + 随机模糊 + 大件数
python3 verify_recall.py --fuzz 300 --seed 13
# stats: {"fixed_rows": 6, "fuzz_cases": 1231, "huge_seconds": 0.048}
# ALL RECALL CHECKS PASSED
```

`verify_recall.py` **不复用** `recall_tool` 的分段结构：对小流水逐件把每件产品当
token 重放，与工具输出逐行比对；错误流水要求被拒且带行号/seq；随机小流水校验
守恒（每件选中产品恰好出现一次）；大件数用 `10^12` 件验证耗时与片段数不随件数增长。

`timeline_tool.py run`、`verify_enum.py`、`attribution_tool.py` 及原有 SQLite
库均保持可用。`recall` 只读访问数据库：优先 `mode=ro`，目录不可写或无 `-shm`
时退到 `mode=ro&immutable=1`，最后才用普通连接 + `PRAGMA query_only=ON`；三种
方式都不写库（核对脚本在只读文件 + 只读目录下复跑并比对内容哈希）。

---

# 放行账册层（release_ledger.py / `timeline_tool.py release`）

质检主管复盘工单时要回答两件事：**当时凭哪些检查记录可以放行？后来换掉其中
一份凭证，哪些签核已不能再用？** 时间线、班组归属和召回清单都回答不了这个
问题，因为它们不保存“签核当时引用了哪份凭证”。本层在本地另落一个 SQLite
放行账册，按批次保存凭证全历史与**每批结束快照**：

- 一张工单只有一份**固定检查计划**（首次入账提供），之后不可改；
- 每个检查项绑定该工单下的一个质检事件，以只读方式在现有归属库
  `events(work_order, event)` 核对（`unlocatable`、无设备绑定的事件同样可用，
  不要求时间锚）；
- 计划至少声明一个**最终放行必需项**；每项定义零组（独立项）或多组替代前置，
  每组至少一项，组内全部通过该组才成立；前置只能引用本计划项且**不能成环**；
- 凭证按工单内**严格递增批次号**（正整数、可跳号）登记/撤销，一项一批至多
  一个操作，凭证 ID 在工单内永不复用；
- 每个已入账批次写一份不可变的**批次结束快照**。查账按主键读一行，不重放
  历史、不扫描其他工单；之后的入账改变不了旧批次结论；
- 任何入账错误**整批拒绝**（不写本批任何内容，批次号不消耗），错误带工单、
  批次和出错项。

## 凭证有效性与“不能替旧签核背书”

- 新凭证无论 pass/fail，都必须完整采用计划中的某一组前置；引用的必须是相应
  检查项在**本批结束时**仍登记（active）且通过的凭证，且其自身引用链有效
  （同批登记的前置也可引用，输入行排列不改变结果）。
- 新凭证**取代**同项旧凭证：旧凭证变 `superseded`，即使两份都是 pass，依赖
  旧 ID 的下游签核也立即失效。
- 下游凭证若仍是该项最后登记版本、但自身已失效，不能再当通过依据；新凭证
  引用它会被拒绝（`reference_not_valid`）。
- **另一组前置已经通过，也不会改写旧签核当时引用的凭证**；恢复有效性必须
  重新登记一份新签核。
- 撤销只针对批前该项“最后登记且尚未撤销”的凭证（即使它已失效），撤销不复活
  更早凭证。历史凭证失效不是入账错误，合法的更换/撤销照常入账。

## 存储结构（release_ledger_schema.sql）

| 表 | 内容 |
|---|---|
| `rl_plans` / `rl_plan_items` / `rl_prereq_groups` / `rl_prereq_members` | 固定计划、项→事件绑定、替代前置组 |
| `rl_batches` | 已入账批次（被拒批次不留行、不耗号） |
| `rl_credentials` | 凭证全历史：`active`/`superseded`/`revoked`，只追加不删除 |
| `rl_credential_refs` | 每份凭证实际采用的组与逐份前置凭证 ID |
| `rl_snapshots` | 每批结束快照（查账唯一读取点，永不更新） |

## 输入格式

首批在批次 JSON 里带 `plan`，之后的批次不得再带计划：

```json
{
  "work_order": "WO-A",
  "batch_seq": 10,
  "plan": {
    "items": [
      {"item": "machine_measure", "event": "e_cross", "prereq_groups": []},
      {"item": "calibration",     "event": "e_before", "prereq_groups": []},
      {"item": "lab_retest",      "event": "e_after",  "prereq_groups": []},
      {"item": "release_signoff", "event": "e_noshift",
       "prereq_groups": [["machine_measure", "calibration"], ["lab_retest"]]},
      {"item": "pack_check", "event": "e_noanchor", "prereq_groups": []}
    ],
    "required": ["release_signoff", "pack_check"]
  },
  "operations": [
    {"action": "register", "item": "machine_measure",
     "credential_id": "M-1", "result": "pass", "refs": []},
    {"action": "register", "item": "release_signoff",
     "credential_id": "S-MAIN", "result": "pass",
     "refs": [{"item": "machine_measure", "credential_id": "M-1"},
              {"item": "calibration", "credential_id": "C-1"}]}
  ]
}
```

操作两种：`register`（带 `result: pass|fail` 与完整一组 `refs`；独立项
`refs` 必须为空）与 `revoke`（带目标 `credential_id`）。

## 命令

```bash
cd timeline_tool
# 0) 需要一个现有班组归属库（只借 events 表做只读核对；也可用任何含 events 表的库）
python3 attribution_tool.py build --db /tmp/attr_demo.db --force \
    --timeline examples/attribution/timeline.json \
    --manifest examples/attribution/manifest.json \
    --shifts   examples/attribution/shifts.json

# 1) 建空账册
python3 release_ledger.py init --db out/release_demo.db
#    等价入口：python3 timeline_tool.py release init --db ...

# 2) 入账（首批带 --attribution-db 做事件核对；后续批次不需要）
for f in woa_batch10_plan_and_pass woa_batch20_replace_fail \
         woa_batch30_alternative_group woa_batch40_resign woa_batch50_revoke; do
  python3 release_ledger.py post --db out/release_demo.db \
      --attribution-db /tmp/attr_demo.db \
      --batch examples/release/$f.json
done

# 3) 按工单 + 已入账批次号查账（快照直读）
python3 release_ledger.py query --db out/release_demo.db \
    --work-order WO-A --batch 20
python3 release_ledger.py list --db out/release_demo.db          # 工单与批次目录
python3 release_ledger.py sql  --db out/release_demo.db --compact \
    --sql "SELECT work_order,batch_seq,releasable FROM rl_batches ORDER BY batch_seq"
```

## 可复跑小例：一次凭证更换，放行 → 不放行 → 重新签核恢复

示例在 `examples/release/`，已提交对应输出到 `out/release_woa_batch*.out.json`。
计划中 `release_signoff` 有两组替代前置：`[机台测量, 校核]` 或 `[实验室复检]`；
必需项是 `release_signoff` 与 `pack_check`。

| 批次 | 动作 | 结束结论 |
|---|---|---|
| 10 | M-1、C-1、P-1 通过；签核 S-MAIN 引用第 0 组 (M-1,C-1) | **可放行** |
| 20 | 机台测量换发不通过凭证 M-2，M-1 被取代 | **不可放行**：S-MAIN `invalid` |
| 30 | 实验室复检 L-1 通过（第 1 组备选前置齐了） | **仍不可放行**：S-MAIN 引用没被改写 |
| 40 | 重新签核 S-RESIGN，显式引用第 1 组 (L-1) | **恢复可放行** |
| 50 | 撤销包装核对 P-1 | **不可放行**：`pack_check` 为 `revoked` |

批次 20 的查账结果（节选）——五项状态互斥区分，失效项带可追根因的路径：

```json
{"work_order": "WO-A", "batch_seq": 20, "releasable": false,
 "items": [
   {"item": "machine_measure", "status": "fail",
    "active_credential": {"credential_id": "M-2", "result": "fail", "valid": true}},
   {"item": "lab_retest", "status": "never_registered", "active_credential": null},
   {"item": "release_signoff", "status": "invalid",
    "active_credential": {
      "credential_id": "S-MAIN", "result": "pass", "valid": false,
      "problems": [
        {"prereq_item": "machine_measure", "ref_credential_id": "M-1",
         "root_cause": {"kind": "superseded", "item": "machine_measure",
                        "credential_id": "M-1", "ended_batch": 20,
                        "replaced_by": "M-2"}}]}}],
 "release_basis": [{"item": "pack_check", "credential_id": "P-1", "refs": []}]}
```

要点核对：

- **失效根因沿同一份结果可追**：`problems[].root_cause` 区分 `superseded` /
  `revoked` / `fail` / `missing`，多级前置链会一路追到最底层凭证（见
  `verify_release.py` 的三级链用例 a→b→c）。
- **备选前置不替旧签核背书**：批 30 后 `lab_retest` 已是 pass，但 S-MAIN 的
  `status` 仍是 `invalid`；它的历史行保留为 `superseded`，只有批 40 的新签核
  S-RESIGN 才让必需项恢复 `pass`。
- **撤销可见且不复活**：批 50 的 `pack_check.last_revocation` 指向
  `{credential_id: "P-1", revoke_batch: 50}`，状态为 `revoked`；对同一凭证
  再次撤销报 `stale_revoke`。
- **旧批次结论不变**：批 50 之后回查批 10，仍是 `releasable: true` 且
  机台测量的 active 凭证为 M-1（快照不可变，没有从首批重放）。
- **同批前置排列无关**：`wob_batch3_orderA.json` 与 `wob_batch3_orderB.json`
  是同一批操作仅行序对调，两个独立库的批 3 快照逐字节一致
  （`verify_release.py` 自动核对）。

## 非法批次：定位到工单/批次/出错项，不留半笔数据

```bash
# 过期撤销：批 20 后机台测量的最后未撤销凭证已是 M-2，撤 M-1 被拒
python3 release_ledger.py post --db /tmp/rl_doc.db \
    --batch examples/release/woa_bad_stale_revoke.json
# {"status": "rejected", "errors": [{"code": "rejected_batch",
#   "work_order": "WO-A", "batch_seq": 25,
#   "errors": [{"code": "stale_revoke", "item": "machine_measure", ...}]}]}
```

`examples/release/woa_bad_*.json` 与 `wob_bad_plan_cycle.json` 覆盖：
过期撤销、同项一批多操作（`duplicate_item_op`）、凭证 ID 复用
（`credential_id_reused`）、前置组合不匹配计划（`unknown_prereq_group`）、
引用未登记/非 active/不通过/自身失效凭证（`unknown_credential_ref` /
`reference_not_active` / `reference_not_pass` / `reference_not_valid`）、
独立项带引用（`refs_not_allowed`）、计划成环（`prereq_cycle`）、必需项或
前置引用计划外检查项、事件不在归属库（`event_not_found`）、批次号未严格
递增（`batch_seq_order`）、首批缺计划 / 后续批次带计划
（`plan_required` / `plan_conflict`）。

被拒时进程退出码为 2，库内全部业务表内容哈希在尝试前后不变；未知工单或
未知批次的查账同样退出码 2（`unknown_work_order` / `unknown_batch`），
不会被当成一张空账。

## 独立核对 verify_release.py

```bash
python3 verify_release.py
python3 verify_release.py --fuzz 200 --seed 7
# stats: {"demo_batches": 5, "fuzz_batches": 791, "fuzz_rejections": 341,
#         "illegal_rejections": 6, "order_checks": 1, "snapshot_mismatches": 0}
# ALL RELEASE LEDGER CHECKS PASSED
```

它**完全不复用** `release_ledger.py` 的代码：

1. 用 subprocess 驱动真实 CLI 走完整演示流程，另写一份参考重放器
   （凭证历史 + 独立递归有效性判定）逐批比对快照的五项状态、凭证历史、
   撤销记录、仍登记凭证集合与放行依据；
2. 每类非法批次要求非零退出，并用业务表逻辑哈希证明不留半笔数据；
3. 同批前置两种排列的快照逐字节一致；
4. 随机生成无环计划与 register/revoke 序列（含同批取代、同批撤销、跳号、
   pass/fail 混排），参考实现逐批重放比对，并随机插入重复 ID 等非法批次
   验证原子拒绝。

`timeline_tool.py run`、`verify_enum.py`、`attribution_tool.py`、
`recall_tool.py` / `verify_recall.py` 与原有 SQLite 库均未改动、继续可用；
放行账册是独立的新库文件，对归属库只做只读打开（`mode=ro` 三档降级，
与 recall 同口径），入账不扫描其他工单，查账不扫描其他工单也不重放历史。

