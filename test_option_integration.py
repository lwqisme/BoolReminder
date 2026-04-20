"""
测试期权集成功能
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from config.config_manager import ConfigManager
from option_quote import OptionQuoteService
from watchlist_boll_filter import is_option_symbol, get_option_boll_data

def test_option_detection():
    """测试期权识别"""
    print("=" * 60)
    print("测试 1: 期权识别功能")
    print("=" * 60)

    test_cases = [
        ("AAPL.US", False),
        ("MSFT270115C480000.US", True),
        ("TSLA260321P200000.US", True),
        ("700.HK", False),
        ("SPY270115C600000.US", True),
    ]

    for symbol, expected in test_cases:
        result = is_option_symbol(symbol)
        status = "✓" if result == expected else "✗"
        print(f"{status} {symbol}: {result} (期望: {expected})")

    print()

def test_option_quote():
    """测试期权报价获取"""
    print("=" * 60)
    print("测试 2: 期权报价获取")
    print("=" * 60)

    # 加载配置
    config_manager = ConfigManager()
    polygon_config = config_manager.get_polygon_config()

    if not polygon_config.get("api_key"):
        print("✗ Polygon API Key 未配置")
        return

    print(f"✓ Polygon API Key: {polygon_config['api_key'][:10]}...")

    # 创建期权服务
    option_service = OptionQuoteService(polygon_config["api_key"])

    # 测试期权代码（使用一个真实的期权代码）
    test_symbol = "MSFT270115C480000.US"

    print(f"\n正在获取期权 {test_symbol} 的报价...")
    quote = option_service.get_option_quote(test_symbol)

    if quote:
        print("✓ 成功获取期权报价:")
        print(f"  收盘价: ${quote.get('close', 'N/A')}")
        print(f"  开盘价: ${quote.get('open', 'N/A')}")
        print(f"  最高价: ${quote.get('high', 'N/A')}")
        print(f"  最低价: ${quote.get('low', 'N/A')}")
        print(f"  成交量: {quote.get('volume', 'N/A')}")
    else:
        print("✗ 获取期权报价失败")

    print()

def test_option_boll():
    """测试期权 BOLL 计算"""
    print("=" * 60)
    print("测试 3: 期权 BOLL 计算")
    print("=" * 60)

    # 加载配置
    config_manager = ConfigManager()
    polygon_config = config_manager.get_polygon_config()

    if not polygon_config.get("api_key"):
        print("✗ Polygon API Key 未配置")
        return

    # 创建期权服务
    option_service = OptionQuoteService(polygon_config["api_key"])

    # 测试期权代码
    test_symbol = "MSFT270115C480000.US"

    print(f"正在计算期权 {test_symbol} 的 BOLL 指标...")
    boll_data = get_option_boll_data(option_service, test_symbol, period=22, k=2.0)

    if boll_data:
        print("✓ 成功计算 BOLL 指标:")
        print(f"  当前价格: ${boll_data['current_price']:.2f}")
        print(f"  下轨: ${boll_data['lower']:.2f}")
        print(f"  中轨: ${boll_data['mid']:.2f}")
        print(f"  上轨: ${boll_data['upper']:.2f}")
    else:
        print("✗ BOLL 计算失败")

    print()

if __name__ == '__main__':
    print("\n开始测试期权集成功能...\n")

    test_option_detection()
    test_option_quote()
    test_option_boll()

    print("=" * 60)
    print("测试完成")
    print("=" * 60)
