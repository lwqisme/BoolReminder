# TODO — 已知待处理问题

> 2026-06-11 在 ADR-0004（周期锚点）修复过程中发现的存量问题。均与该修复无关（在干净 HEAD 上同样复现）。

## 1. test_position_strategy.py 整个文件无法收集（ImportError）

- **现象**：`ImportError: cannot import name '_grid_rebound_stages' from 'drawdown.position_strategy'`（`test_position_strategy.py:15`）。pytest 收集阶段即失败，**该文件全部测试长期处于未执行状态**——position_strategy 引擎的核心测试覆盖实际上是空缺的。
- **原因**：引擎重构时 `_grid_rebound_stages` 被改名/移除（现存 `grid_rebound_stages`，无下划线，可能已迁到 `strategy_rules.py`），测试 import 未同步。
- **修法**：更新 import；逐个核对同文件里其它私有符号（`_rearm_position_sell_cycle_after_dca_buy`、`_score_question_strategies` 等）是否仍存在；跑通全文件。注意修通后可能暴露更多因引擎演进而过期的断言，包括 ADR-0004 引入的 cost_deleverage 新语义。

## 2. test_strategy_lab_score_payload.py 两个失败 — 测试期望落后于 schema 演进

- `test_parameter_lab_packet_endpoint_returns_v3_manifest_and_cache_metadata`
  （`test_strategy_lab_score_payload.py:211`）：
  断言 `candidate_schema == ['candidate_id', 'buy_variant_id', 'sell_variant_id']`，
  实际后端已加第 4 列 `candidate_key`。
- `test_parameter_lab_estimate_and_packet_use_selected_values`
  （`test_strategy_lab_score_payload.py:328`）：
  固定全部参数后期望 `candidate_count == 1`，实际为 2——大概率是后来新增的某个参数维度
  （疑似 `sell_allow_same_day_sell` 或 `buy_rearm_mode`）未被该测试的"固定"payload 覆盖，
  仍产生两个变体。
- **修法**：先确认 `candidate_key` 列和多出的变体维度是有意为之（看 git log 对应提交），
  然后更新测试期望；不要反过来削 schema。

## 3. test_strategy_lab_robust.py 一个失败 — 权重字典新增 sell_quality

- `test_robust_ranking_ignores_runtime_weight_overrides`（`test_strategy_lab_robust.py:276`）：
  期望权重 `{'return': 0.9, 'drawdown': 0.1}`，实际多了 `'sell_quality': 0.0`。
  评分引擎加入卖出质量维度后默认权重 0.0 会出现在权重字典里，测试断言未更新。
- **修法**：更新期望加入 `'sell_quality': 0.0`，或断言时忽略值为 0 的键。

## 4. 运维备注

- 双核服务器上 `test_ga_e2e_restrict.py` + `test_strategy_lab_robust.py` 全量跑约 20 分钟
  （GA 进化端到端）。日常只跑与改动相关的测试文件；GA e2e 仅在明确要求时跑。
