# BoolReminder

量化策略回测与实时信号系统。支持股票策略和 LEAPS 期权策略的 GA 参数优化、回测评分、以及基于真实持仓的每日买卖信号生成。

## Language

**LEAPS**:
Long-term Equity Anticipation Securities — 长期深度实值看涨期权。
_Avoid_: 期权、option（在 LEAPS 语境下专指 LEAPS call）

**信号 (Signal)**:
引擎基于策略参数和当前市场数据生成的买入/卖出建议。包含 action、shares、price、reason。
_Avoid_: 交易指令、下单

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
