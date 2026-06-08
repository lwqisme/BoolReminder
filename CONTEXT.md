# BoolReminder

量化策略回测与实时信号系统。支持股票策略和 LEAPS 期权策略的 GA 参数优化、回测评分、以及基于真实持仓的每日买卖信号生成。

## Language

**LEAPS**:
Long-term Equity Anticipation Securities — 长期深度实值看涨期权。
_Avoid_: 期权、option（在 LEAPS 语境下专指 LEAPS call）

**信号 (Signal)**:
引擎基于策略参数和当前市场数据生成的买入/卖出建议。包含 action、shares、price、reason。
盘中信号在美东时间 11:00 和 12:00 自动生成并邮件通知，盘中休市跳过。
_Avoid_: 交易指令、下单

**实时价 (Realtime Price)**:
信号生成时，通过 Longbridge 实时报价 API 获取的最新成交价。用于追加/替换日线序列最后一天的价格点。
仅交易时段使用，休市跳过。
_Avoid_: 当前价、最新价

**档位 (Stage / S1/S2/S3)**:
LEAPS 卖出阶梯的每一级。每档定义最低持有天数、盈利阈值、卖出比例。
_Avoid_: 阶梯、层级

**入口信号 (Entry Signal)**:
检测到的 LEAPS call 买入机会，基于 120 日最高价回撤 + 布林带下轨。
_Avoid_: 买点、入场点

**GA 进化 (GA Evolution)**:
用遗传算法在参数空间内搜索最优策略参数组合。
_Avoid_: 优化、调参

**策略参数实验室 (Parameter Lab)**:
Web 界面，用于对比不同策略参数组合的回测结果。
_Avoid_: 参数搜索、回测界面

**Golden File**:
固化到仓库的测试数据，包含价格序列和预期买卖点，用于验证引擎输出一致性。
_Avoid_: fixture、snapshot

**代理期权 ROI (Proxy Option ROI)**:
用 Black-Scholes 模型估算的 LEAPS 期权回报率，不依赖真实期权价格。
_Avoid_: 理论ROI、BS定价

**开放交易 (Open Trade)**:
模拟中已触发入口但尚未完成全部卖出阶梯的 LEAPS 交易。`open_pct` 表示未卖出比例，`unrealized_roi_pct` 为按最新价格估算的未实现代理 ROI。
_Avoid_: 未平仓交易、进行中交易
