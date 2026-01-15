# BoolReminder - BOLL指标查询工具

这个项目用于查询股票的BOLL（布林带）指标数据。

## 📋 关于LongBridge API和BOLL指标

### 结论

**LongBridge OpenAPI 目前没有直接提供 BOLL 指标的接口**，但可以通过以下方式组合获取：

1. 使用 `candlesticks` 接口获取历史K线数据
2. 提取收盘价数据
3. 本地计算BOLL指标（中轨、上轨、下轨）

## 🚀 使用方法

### 1. 安装依赖

**重要：** `longbridge` 包需要 Rust 编译器来构建。如果遇到编译错误，请按以下步骤操作：

#### 步骤1：安装 Rust（如果还没有安装）

```bash
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y
source "$HOME/.cargo/env"
```

#### 步骤2：安装 longbridge

由于 Python 3.13 的兼容性问题，需要使用以下命令安装：

```bash
# 激活虚拟环境
source .venv/bin/activate  # Linux/Mac
# 或
.venv\Scripts\activate  # Windows

# 设置RUSTFLAGS环境变量并安装
RUSTFLAGS="-A dependency_on_unit_never_type_fallback" pip install longbridge
```

或者使用 requirements.txt：

```bash
RUSTFLAGS="-A dependency_on_unit_never_type_fallback" pip install -r requirements.txt
```

### 2. 配置LongBridge API

你需要：
- 在 [LongBridge开放平台](https://open.longbridge.com) 注册账号
- 创建应用获取 `app_key` 和 `app_secret`
- 获取 `access_token`

配置方式（二选一）：

**方式1：使用配置文件**
创建 `config.json`:
```json
{
  "app_key": "your_app_key",
  "app_secret": "your_app_secret",
  "access_token": "your_access_token"
}
```

**方式2：使用环境变量**
```bash
export LONGBRIDGE_APP_KEY="your_app_key"
export LONGBRIDGE_APP_SECRET="your_app_secret"
export LONGBRIDGE_ACCESS_TOKEN="your_access_token"
```

### 3. 使用示例

```python
from longbridge_boll_example import get_stock_boll_daily

# 获取某只股票的BOLL指标
result = get_stock_boll_daily("700.HK", period=20, k=2.0)

if result:
    print(f"上轨: {result['upper']}")
    print(f"中轨: {result['mid']}")
    print(f"下轨: {result['lower']}")
```

## 📊 BOLL指标说明

布林带（Bollinger Bands）由三条线组成：

- **上轨（Upper Band）** = 中轨 + k × 标准差
- **中轨（Middle Band）** = N日简单移动平均线（SMA）
- **下轨（Lower Band）** = 中轨 - k × 标准差

**参数说明：**
- `period`（周期N）：通常为20，表示20日移动平均
- `k`（倍数）：通常为2.0，表示2倍标准差

**应用：**
- 价格接近上轨：可能超买，考虑卖出
- 价格接近下轨：可能超卖，考虑买入
- 价格在中轨附近：正常波动

## 📁 文件说明

- `boll_calculator.py`: BOLL指标计算器核心类
- `longbridge_boll_example.py`: 完整的LongBridge API集成示例
- `main.py`: 项目入口文件

## 🔗 相关链接

- [LongBridge OpenAPI 文档](https://open.longbridge.com)
- [K线数据接口文档](https://open.longbridge.com/docs/quote/pull/candlestick)
- [计算指标接口文档](https://open.longbridge.com/docs/quote/pull/calc-index)
