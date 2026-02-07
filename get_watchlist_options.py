"""
获取自选股中的期权代码
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from longbridge.openapi import QuoteContext, Config
from config.config_manager import ConfigManager


def get_watchlist_options():
    """获取自选股中的期权"""
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

    # 获取自选列表
    watchlist_groups = quote_ctx.watchlist()

    print("=" * 60)
    print("自选股中的期权列表：")
    print("=" * 60)

    options = []
    stocks = []

    for group in watchlist_groups:
        print(f"\n分组: {group.name}")
        print("-" * 60)
        for security in group.securities:
            symbol = security.symbol
            name = security.name if hasattr(security, 'name') else symbol

            # 判断是否为期权（包含日期和C/P标识）
            is_option = False
            if ('C' in symbol or 'P' in symbol) and any(char.isdigit() for char in symbol):
                # 进一步检查是否符合期权格式
                if '.US' in symbol or '.HK' in symbol:
                    is_option = True

            if is_option:
                options.append(symbol)
                print(f"  [期权] {symbol} - {name}")
            else:
                stocks.append(symbol)
                print(f"  [股票] {symbol} - {name}")

    print("\n" + "=" * 60)
    print(f"统计: 共 {len(stocks)} 只股票, {len(options)} 个期权")
    print("=" * 60)

    if options:
        print("\n期权代码列表（可用于API测试）：")
        for opt in options:
            print(f"  {opt}")
    else:
        print("\n未找到期权")

    return options


if __name__ == '__main__':
    get_watchlist_options()
