"""
测试Polygon.io期权查询
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from option_quote import OptionQuoteService
from config.config_manager import ConfigManager


def test_polygon_option_quote():
    """测试Polygon.io期权查询"""
    print("=" * 60)
    print("测试Polygon.io期权查询")
    print("=" * 60)

    # 加载配置
    config_manager = ConfigManager()
    polygon_config = config_manager.get_polygon_config()
    api_key = polygon_config.get("api_key")

    if not api_key:
        print("❌ 错误: Polygon.io API Key未配置")
        return

    print(f"✅ API Key已配置: {api_key[:10]}...")

    # 创建服务
    service = OptionQuoteService(api_key)

    # 测试期权列表
    test_symbols = [
        "MSFT270115C480000.US",
        "AAPL260918C260000.US",
        "NVDA260618C220000.US",
    ]

    for symbol in test_symbols:
        print(f"\n{'='*60}")
        print(f"测试期权: {symbol}")
        print(f"{'='*60}")

        try:
            result = service.get_option_quote(symbol)

            if result:
                print("✅ 成功获取报价!")
                print(f"  买价 (Bid): ${result.get('bid_price')}")
                print(f"  卖价 (Ask): ${result.get('ask_price')}")
                print(f"  中间价 (Mid): ${result.get('mid_price')}")
                print(f"  买量: {result.get('bid_size')}")
                print(f"  卖量: {result.get('ask_size')}")
                print(f"  时间戳: {result.get('timestamp')}")
            else:
                print("❌ 未获取到报价")

        except Exception as e:
            print(f"❌ 错误: {e}")


if __name__ == '__main__':
    test_polygon_option_quote()
