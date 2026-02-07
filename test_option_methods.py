"""
详细调试期权查询 - 测试不同的查询方法
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from longbridge.openapi import QuoteContext, Config
from config.config_manager import ConfigManager


def test_all_methods(symbol: str):
    """测试所有可能的查询方法"""
    print(f"=" * 60)
    print(f"测试期权: {symbol}")
    print(f"=" * 60)

    config_manager = ConfigManager()
    lb_config = config_manager.get_longbridge_config()

    config = Config(
        app_key=lb_config.get("app_key"),
        app_secret=lb_config.get("app_secret"),
        access_token=lb_config.get("access_token")
    )

    quote_ctx = QuoteContext(config)

    # 测试1: 普通quote方法
    print(f"\n【测试1】使用 quote() 方法:")
    try:
        quotes = quote_ctx.quote([symbol])
        print(f"  返回数量: {len(quotes) if quotes else 0}")
        if quotes and len(quotes) > 0:
            print(f"  ✅ 成功! last_done: {quotes[0].last_done}")
        else:
            print(f"  ❌ 返回空数据")
    except Exception as e:
        print(f"  ❌ 错误: {e}")

    # 测试2: option_quote方法
    print(f"\n【测试2】使用 option_quote() 方法:")
    try:
        quotes = quote_ctx.option_quote([symbol])
        print(f"  返回数量: {len(quotes) if quotes else 0}")
        if quotes and len(quotes) > 0:
            print(f"  ✅ 成功! last_done: {quotes[0].last_done}")
        else:
            print(f"  ❌ 返回空数据")
    except Exception as e:
        print(f"  ❌ 错误: {e}")

    # 测试3: 查询标的股票
    underlying = symbol.split('.')[0][:4]  # 提取股票代码
    print(f"\n【测试3】查询标的股票 {underlying}.US:")
    try:
        quotes = quote_ctx.quote([f"{underlying}.US"])
        print(f"  返回数量: {len(quotes) if quotes else 0}")
        if quotes and len(quotes) > 0:
            print(f"  ✅ 成功! last_done: {quotes[0].last_done}")
        else:
            print(f"  ❌ 返回空数据")
    except Exception as e:
        print(f"  ❌ 错误: {e}")


if __name__ == '__main__':
    # 测试几个期权
    symbols = [
        "MSFT270115C480000.US",
        "AAPL260918C260000.US",
        "NVDA260618C220000.US",
    ]

    for symbol in symbols:
        test_all_methods(symbol)
        print("\n")
