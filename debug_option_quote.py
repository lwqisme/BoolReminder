"""
调试期权查询 - 查看详细的API响应
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from longbridge.openapi import QuoteContext, Config
from config.config_manager import ConfigManager


def debug_option_quote(symbol: str):
    """调试期权查询"""
    print(f"=" * 60)
    print(f"调试期权查询: {symbol}")
    print(f"=" * 60)

    # 加载配置
    config_manager = ConfigManager()
    lb_config = config_manager.get_longbridge_config()

    # 初始化LongBridge配置
    config = Config(
        app_key=lb_config.get("app_key"),
        app_secret=lb_config.get("app_secret"),
        access_token=lb_config.get("access_token")
    )

    # 创建QuoteContext
    quote_ctx = QuoteContext(config)

    try:
        print(f"\n1. 尝试获取期权报价（使用option_quote方法）...")
        quotes = quote_ctx.option_quote([symbol])

        print(f"   返回的quotes数量: {len(quotes) if quotes else 0}")

        if quotes and len(quotes) > 0:
            quote = quotes[0]
            print(f"\n2. 报价详细信息:")
            print(f"   symbol: {quote.symbol}")
            print(f"   last_done: {quote.last_done}")
            print(f"   open: {quote.open}")
            print(f"   high: {quote.high}")
            print(f"   low: {quote.low}")
            print(f"   volume: {quote.volume}")
            print(f"   turnover: {quote.turnover}")
            print(f"   timestamp: {quote.timestamp}")

            # 打印所有属性
            print(f"\n3. 所有属性:")
            for attr in dir(quote):
                if not attr.startswith('_'):
                    try:
                        value = getattr(quote, attr)
                        if not callable(value):
                            print(f"   {attr}: {value}")
                    except:
                        pass
        else:
            print(f"   ❌ 未返回任何报价数据")

    except Exception as e:
        print(f"\n❌ 错误: {str(e)}")
        import traceback
        traceback.print_exc()


if __name__ == '__main__':
    # 测试多个期权
    symbols = [
        "MSFT270115C480000.US",
        "NVDA260618C220000.US",
        "AAPL260918C260000.US",
    ]

    for symbol in symbols:
        debug_option_quote(symbol)
        print("\n")
