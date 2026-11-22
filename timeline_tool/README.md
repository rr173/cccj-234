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
