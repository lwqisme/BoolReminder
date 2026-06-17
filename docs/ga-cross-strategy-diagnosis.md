# 股票 GA 跨策略竞争诊断记录

日期：2026-06-17

范围：参数实验室 → 股票【遗传算法 (GA)】→ 跨策略竞争

相关提交：

- `f237fa3 fix(ga): make stock cross-strategy evolution reproducible`
- `54cbb56 fix(ga): round-robin cross-strategy robust seeds`
- `ca27992 chore(ga): add copyable cross-strategy diagnostics log`

## 1. 用户报告的现象

用户在参数实验室股票 GA 中开启“跨策略竞争”，并设置策略突变率（例如 `0.2`）后，观察到：

1. 多次运行结果差异很大。
2. 最佳策略不稳定，一会儿是金字塔，一会儿是加权细切。
3. 同一场景下，固定单策略 `等距细切 + 成本区间去杠杆` 能跑出约 `122.5` 分；但跨策略全池、seed `123456789`、以 `2026-06-10` 为终点的 TSLA 1/3/5 年任务，25 代后只能收敛到约 `80~91` 分。
4. 典型坏结果：
   - 第 22~25 代均卡在约 `80.2` 分。
   - 最佳为 `线性递增加权细切 / 成本区间去杠杆`。
   - 用户质疑：如果固定策略能到 122.5，跨策略为什么这么快收敛到明显更差的结果？

用户要求：必须自己实际跑过，不接受只凭代码猜测。

## 2. 第一轮假设：策略突变没有发生

最初从代码阅读看，跨策略突变逻辑位于前端模板 `web/templates/strategy_parameter_lab.html` 的 `mutateGa`：

```js
if (crossEnabled && gaConfig.cross_strategy && Math.random() < (gaConfig.strategy_mutation_rate || 0.05)) {
    const buyStrats = (paramRanges.buy_fields ? Object.keys(paramRanges.buy_fields) : [child.buy_strategy]) || [child.buy_strategy];
    const sellStrats = (paramRanges.sell_fields ? Object.keys(paramRanges.sell_fields) : [child.sell_strategy]) || [child.sell_strategy];
    child.buy_strategy = buyStrats[Math.floor(Math.random() * buyStrats.length)];
    child.sell_strategy = sellStrats[Math.floor(Math.random() * sellStrats.length)];
}
```

我先写了一个 Node.js harness，复刻 `mutateGa`，使用多买入/卖出策略池和 `strategy_mutation_rate=0.2`。结果显示策略确实会变化，跨策略突变本身并非完全失效。

结论：

- “策略突变完全不发生”不是根因。
- 继续追查随机性、策略池、初始化和选择机制。

## 3. 第二轮发现：股票 GA 没有 seed，导致不可复现

前端股票 GA 大量使用 `Math.random()`：

- 锦标赛选择
- 交叉
- 参数突变
- 策略突变
- 生成 child key

同时服务端 `ga-packet` 初始化种群也没有读取股票 GA seed。对比 LEAPS GA，其已有 `leapsGaSeed`；股票 GA 没有对应输入。

这解释了用户说的：

> 多次运行跨策略，结果不同、策略不同、分数也不同。

修复：

1. GA 面板新增“随机种子”输入 `gaSeed`。
2. 前端加入 `mulberry32(seed)` PRNG。
3. 前端 GA 随机行为统一走 `gaRandom()`，替代 `Math.random()`。
4. `ga_seed` 传给后端，服务端 `EvolutionConfig(seed=...)` 控制初始种群。
5. `ga_config` 返回 `seed`。
6. JS 单测增加：
   - 同 seed 变异序列完全一致。
   - 不同 seed 变异序列不同。

相关提交：`f237fa3`。

## 4. 第三轮发现：策略突变池用错了，跳到未勾选策略

继续检查发现，前端策略突变使用：

```js
Object.keys(paramRanges.buy_fields)
Object.keys(paramRanges.sell_fields)
```

而 `paramRanges.buy_fields` / `sell_fields` 来自全局 registry，不是用户当前勾选的策略池。

这意味着：

- 用户可能只勾选了部分策略。
- 但突变会跳到所有注册策略，包括未勾选的 `weekly_dca`、`salary_flow_dca`、`core_dip_dca` 等。
- 这些未选择策略会向种群注入噪声，降低跨策略竞争可信度。

修复：

- `runGeneticEvolution()` 把用户实际选择的 `ga_buy_strategies` / `ga_sell_strategies` 挂到 `gaConfig`。
- `mutateGa()` 跨策略突变时优先使用 `gaConfig.ga_buy_strategies` / `gaConfig.ga_sell_strategies`。
- 加 JS 回归测试：跨策略突变只能跳到用户选择的策略池。

相关提交：`f237fa3`。

## 5. 第四轮发现：跨策略初始化有顺序偏置，强组合没有足够 robust seed

用户进一步指出，固定 `equal_slice + cost_deleverage` 可跑出高分，而跨策略仍然只有约 91 分。

我用真实 `ga-packet` 检查 seed `123456789`、多买入/卖出策略池、`population_size=50` 时的初始种群分布。

修复前，30 个策略组合中：

- `equal_slice + cost_deleverage` 只有 **1 个**。
- 该 1 个只是默认组合，并非充分的成本去杠杆 robust seed。
- 同时前序组合占用大量 robust seed：
  - `equal_slice + none` 约 13 个
  - `equal_slice + repair_step` 约 9 个
  - 后面的组合基本只剩 1 个

根因在 `_initialize_population()`：

1. 先为每个策略组合塞默认参数。
2. 然后按 `buy_strategies × sell_strategies` 顺序塞 robust seeds。
3. 一旦 `population_size` 填满，后序组合拿不到 robust seed。

这对跨策略很致命：

- 固定单策略时，50 个个体全用于 `equal_slice + cost_deleverage` 搜索。
- 跨策略时，30 个组合先各占默认位，剩余 seed 被前序组合吃掉。
- 强组合没有获得足够初始搜索预算。

修复：

- robust seed 初始化改成 round-robin across strategy pairs。
- 每个策略组合轮流获得 robust seed，防止前序组合吃光。
- 加 Python 回归测试：多策略全池、`population_size=50` 时，`equal_slice + cost_deleverage` 必须至少有 default + robust seed。

修复后验证：

- `equal_slice + cost_deleverage` 从 1 个变为 2 个。
- 初始分布更公平。

相关提交：`54cbb56`。

## 6. 真实 worker harness 复现

用户要求“自己测过”。我写了 `/tmp/stock_ga_probe.js`，使用真实前端 worker：

- 加载 `web/static/strategy_parameter_lab_worker.js`
- 使用真实 `ga-packet`
- 只取 TSLA 1/3/5 三个任务：
  - `tsla_100__1y`
  - `tsla_100__3y`
  - `tsla_100__5y`
- 模拟前端 GA loop：
  - initRun
  - processBatch
  - fitness 计算
  - selection / crossover / mutation

### 6.1 固定策略结果

固定：

- `buy_strategy=equal_slice`
- `sell_strategy=cost_deleverage`
- `population_size=50`
- `generations=25`
- `seed=123456789`
- `continuous_mutation=true`
- TSLA 1/3/5

harness 结果：

```text
fixed gen 1 best 76.9 avg 30.8
...
fixed gen 16 best 113.8 avg 100.7
...
fixed gen 25 best 115.8 avg 101.7
FINAL_BEST fixed 115.761 equal_slice cost_deleverage
```

我的本地 harness 没复现到用户的 122.5，但同量级，且确认固定策略能持续进化到 110+。

### 6.2 跨策略全池结果

跨策略：

- 买入：`equal_slice`, `linear_weighted_slice`, `pyramid_3`, `core_dip_dca`, `weekly_dca`, `salary_flow_dca`
- 卖出：`none`, `repair_step`, `grid_rebound`, `price_rise_grid`, `cost_deleverage`
- `population_size=50`
- `generations=25`
- `strategy_mutation_rate=0.2`
- `seed=123456789`
- `continuous_mutation=true`
- TSLA 1/3/5

harness 结果：

```text
cross gen 1 best 72.0 linear_weighted_slice/cost_deleverage avg 43.0
...
cross gen 21 best 80.2 linear_weighted_slice/cost_deleverage avg 69.5
...
cross gen 25 best 80.2 linear_weighted_slice/cost_deleverage avg 71.3
FINAL_BEST cross 80.172 linear_weighted_slice cost_deleverage
```

这基本复现了用户贴的前端结果。

## 7. 关键证据：equal_slice + cost_deleverage 在跨策略中很快死亡

我进一步在 harness 中记录每代：

- `equal_slice + cost_deleverage` 存活数量与 best fitness
- `linear_weighted_slice + cost_deleverage` best fitness

结果：

```text
cross gen 1 best 72.0 linear_weighted_slice/cost_deleverage avg 43.0 eqcostN 3 eqcostBest 63.5 linCostBest 72.0
cross gen 2 best 72.0 linear_weighted_slice/cost_deleverage avg 65.6 eqcostN 0 eqcostBest - linCostBest 72.0
cross gen 3 best 72.5 linear_weighted_slice/cost_deleverage avg 65.4 eqcostN 1 eqcostBest 58.0 linCostBest 72.5
cross gen 4 best 73.0 linear_weighted_slice/cost_deleverage avg 63.7 eqcostN 0 eqcostBest - linCostBest 73.0
...
cross gen 25 best 80.2 ... eqcostN 0 eqcostBest - linCostBest 80.2
```

第 1 代各策略对排行：

```text
PAIR 1 72.0 linear_weighted_slice/cost_deleverage
PAIR 2 68.8 pyramid_3/price_rise_grid
PAIR 3 68.6 linear_weighted_slice/price_rise_grid
PAIR 4 64.7 pyramid_3/cost_deleverage
PAIR 5 63.5 equal_slice/cost_deleverage
```

解释：

- `equal_slice + cost_deleverage` 并不是没进池，第 1 代第 5 名。
- 但第 2 代就经常归零。
- 固定单策略之所以能到 115+，是因为它有 50 个个体 × 25 代都在同一个策略对内连续调参。
- 跨策略全局锦标赛会把第 1 代稍低的策略对过早淘汰。
- `equal_slice + cost_deleverage` 还没来得及在自己的参数空间进化，就失去繁殖预算。

## 8. 已尝试但效果不够的替代机制

### 8.1 所有策略对都保种

我在 harness 试过每代保留每个策略对 best，并给每个策略对一个局部后代。

结果更差：最终约 `77`。

原因：

- 30 个策略对全保种，`population_size=50` 被切得太碎。
- 每个策略对仍没有足够局部搜索预算。

### 8.2 top-K 策略对局部预算

我试过保留 top 5 策略对，并把 80% budget 给 top 5 局部进化。

结果约 `90`，仍不如固定单策略。

原因：

- top-K 方案仍可能偏向早期领先策略对。
- `equal_slice + cost_deleverage` 第 1 代只有 63.5，虽第 5 名，但局部预算仍不足以复制固定单策略的 50 个体连续进化。

### 8.3 top-K 固定 quota

我试过 top 5 每个策略对固定 quota。

结果约 `85`。

说明简单“保种/配额”不够，需要更明确的两阶段机制。

## 9. 当前结论

截至目前，根因分为两层：

### 已修复层

1. 无 seed → 结果不可复现。
2. 策略突变跳到未勾选策略 → 注入噪声。
3. 初始 robust seed 顺序偏置 → 强组合缺少初始代表。

### 未完全解决层

跨策略 GA 的算法结构仍不适合“多策略对 + 每个策略对需要深度调参”的场景。

当前是一锅全局 GA：

- 所有策略对的个体一起比 fitness。
- 第 1 代稍高的策略对马上获得大量繁殖资源。
- 潜力策略对如果初始分数不是最高，会过早消失。
- 这会错过“初始不最高，但调参后很强”的策略对，例如 `equal_slice + cost_deleverage`。

## 10. 下一步更合理的方案

### 方案 A：两阶段 Cross-Strategy GA（推荐）

1. **策略对筛选阶段**
   - 对每个 buy/sell strategy pair 做少量种子评估。
   - 不做全局个体混战。
   - 得到 top K 策略对。

2. **策略对内进化阶段**
   - 对 top K 每个策略对分别运行小型局部 GA。
   - 每个策略对有独立 population / generations budget。
   - 类似固定单策略模式，只是对多个候选策略对并行跑。

3. **汇总阶段**
   - 合并 top K 的 best candidates。
   - 输出全局 best 和每个策略对 best。

优点：

- 能复现固定单策略的搜索能力。
- 不会因为第 1 代略低就淘汰潜力策略对。
- 结果更可信，也更容易解释。

缺点：

- 总模拟量增加。
- 前端实现复杂度更高。

### 方案 B：跨策略分岛模型

每个策略对维护一个 island：

- 岛内交叉/突变参数。
- 少量迁移/策略突变。
- 每代汇总全局排名。

优点：理论更 GA。
缺点：前端改动较大，UI 要解释 island budget。

### 方案 C：保留当前全局 GA，但增加“策略对最低配额”

例如：

- top 5 策略对每代至少保留 N 个。
- tracked 强策略对额外保留。

已在 harness 简单试过，提升有限，不推荐作为最终方案。

## 11. 页面诊断日志

为了让用户用真实浏览器跑，并把证据复制回来，我在页面新增：

- `GA 诊断日志` 折叠面板
- `复制诊断日志` 按钮

日志内容包括：

- seed / payload / 策略池
- packet tasks
- 初始种群 pair counts
- 每代 best / avg
- 每代 top strategy pairs
- 每代 `equal_slice + cost_deleverage` 存活数量与 best
- 每代 `linear_weighted_slice + cost_deleverage` 存活数量与 best

相关提交：`ca27992`。

## 12. 诊断方法复盘

这轮踩过的坑：

1. 一开始误以为“策略突变没发生”。
   - harness 证明突变本身会发生。
2. 后来只修 seed 和策略池，但没有验证最终业务目标。
   - 用户指出分数仍然差，是正确的。
3. 初始种群公平性修复后，仍没解决“固定策略强、跨策略弱”。
   - 真实 worker harness 证明核心是全局选择过早淘汰潜力策略对。
4. 简单保种/配额不是充分解。
   - harness 试验显示仍低于固定单策略。

关键经验：

- 跨策略 GA 不是“把策略类型加入基因”这么简单。
- 当每个策略对内部都有高维参数需要调时，全局混战会把搜索预算给早期领先者，错过后发强组合。
- 要让跨策略结果可信，必须给策略对级别足够局部进化预算。

## 13. 当前状态

已部署：

- seed 可复现
- 策略突变限制在用户选择池
- round-robin robust seed
- 可复制诊断日志
- ✅ 两阶段 Cross-Strategy GA（§14–§15，已闭环）

未完成：

- （无）跨策略 GA 两阶段重构已完成，见 §15。

## 14. 浏览器日志确认 + 两阶段实现（2026-06-17）

用户提供真实浏览器诊断日志（`galogs.log`，`population_size=200`），死亡路径与 harness 一致且更极端：

- `pyramid_3/price_rise_grid` 第 2 代吞掉 200 中的 169 个，之后 24 代基本 monoculture，天花板 ~79.05。
- `equal_slice/cost_deleverage` 全程 best 钉死 66.218（默认参数），靠策略突变零星回播，从未连续进化。
- 坐实 §9 根因：全局锦标赛过早淘汰潜力策略对。并发现文档 §10 方案 A 的「默认种子筛选」是错的（eqcost 默认 66 < pyramid_3 默认 76.5，纯默认筛选同样误杀 eqcost）。

实现修正后的两阶段 Cross-Strategy GA（提交 `bb33e7a`，纯 JS，后端不改）：

- **Stage1**：每个选中策略对独立岛内进化（策略锁死、无跨对交配），每代一次 worker 派发覆盖全部对；让 eqcost 这类后发策略爬升后公平排名。
- **Stage2**：按 Stage1 local-best 取 top-K finalists，全局深挖，开启低率跨策略突变 + 每对最低配额 elitism 防 monoculture。
- 预算由 `population_size`/`generations` 推导（~1.6–1.9x），全部走 seeded `gaRandom()` 保可复现；非跨策略模式不动。
- 诊断日志新增 `stage_config`、`stage1_pair_local_bests`、每代 `stage` 字段。
- JS 回归测试：岛内隔离、可复现、Stage2 配额。

入口：`web/templates/strategy_parameter_lab.html` `runTwoStageCrossStrategyGa`（cross 分支切入 `runGeneticEvolution`）。

待用户浏览器复现验证：seed `123456789`、跨策略、TSLA 1/3/5y，核对 eqcost 爬升进 top-K、Stage2 不再 monoculture、最终 best 显著高于 79.05。

## 15. 三轮迭代收敛到 115.95（2026-06-17）

两阶段上线后经历两轮修复，最终 seed `123456789` / pop 200 / 25 代跨策略得分：

| 版本 | 最终 best | 赢家策略对 | 问题 |
|------|----------|-----------|------|
| 单池（旧） | 79.05 | pyramid_3/price_rise_grid（错） | monoculture，eqcost 钉死 66.2 |
| 两阶段 v1（`bb33e7a`） | 98.86 | equal_slice/cost_deleverage（对） | Stage2 配额冻结 + Stage1 预算过薄 |
| 两阶段 v2（`c8bb944`） | **115.95** | equal_slice/cost_deleverage（对） | 健康持续爬升，无平台 |

### v1 暴露的两个缺陷

1. **Stage2 冻结**：配额公式 `(pop-top_k)/top_k` = 24/对 → 8 对×24 = 192/200 槽位每代冻结，仅 ~8 个新后代。gen 12–18 卡 95.819、gen 23–36 卡 98.723。配额本应是小「生存下限」，被错写成吃掉整个种群。
2. **Stage1 预算又薄又浪费**：每对仅 264 evals（eqcost 最后一代还在爬就被截断 90.5→93.4），却把 7920 evals 摊到含 ~45 分垃圾的 30 对。

### v2 修复（提交 `c8bb944`）

1. Stage2 配额改小 floor `clamp(round(P*0.02),2,6)` = 4，~92% 自由繁殖 → 曲线从 100.6 持续爬到 115.95，无长平台。
2. Stage1 改**两级**：cheap SCREEN 全 30 对（`screen_pop×screen_gens`）筛选 → DEEPEN 把真实预算（`deep_pop×deep_gens`）集中到 top-K finalists。

### 关键证据：DEEPEN 反超坐实「默认筛选误杀 eqcost」诊断

- screen 阶段 eqcost 只排第 6（73.3），被 `linear_weighted_slice/price_rise_grid` 的 82.8 压住。
- DEEPEN 集中预算后 eqcost 爬到 **100.6，反超所有人 4–20 分**，登顶 finalists。
- 这证明 §14 的核心结论：纯默认/轻筛选会误杀 eqcost，必须给每个策略对真实进化预算才能显出后发优势。

### 收敛与剩余差距

- Stage2 在 gen ~30 后趋收敛（gen 36→43 仅 +0.2 分），剩余预算产出低。
- 最终 gen pair_counts：eqcost 141/200，其余 finalists 各 4–8（均 ≥ 配额下限）。这次是 eqcost 凭真实优势自然扩张，非 bug 式 monoculture。
- 115.95 vs 固定单策略 122 的 ~6 分差距是**结构性代价**：跨策略必须为「发现哪个对最强」付税（Stage1 screen ~1800 evals 摊在 30 对），eqcost 实际只拿到 ~570 evals（固定单策略用满 5000）。用户评估后接受此差距，不再追调。

### 最终状态

已部署（`boll-reminder`），可复现（seed `123456789`）。诊断日志字段：`stage_config`（screen/deep/stage2 预算 + `stage2_min_quota`）、`stage1_screen_bests`、`stage1_deep_bests`、`stage2_finalists`、每代 `stage`。JS 回归测试 32 passed，含防 Stage2 冻结复发的 `test_stage2_quota_floor_stays_small`。

非跨策略模式不受影响。`docs/ga-cross-strategy-diagnosis.md` §13「未完成」项至此关闭。
