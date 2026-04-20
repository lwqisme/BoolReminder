#!/usr/bin/env python3
"""测试期权 BOLL 计算"""

from option_quote import OptionQuoteService
from watchlist_boll_filter import get_option_boll_data
from config.config_manager import ConfigManager

config = ConfigManager()
polygon_config = config.get_polygon_config()
service = OptionQuoteService(polygon_config['api_key'])

# 测试 AMZN 期权 BOLL 计算
print('测试 AMZN260618C240000.US 期权...')
boll = get_option_boll_data(service, 'AMZN260618C240000.US', period=22, k=2.0)

if boll:
    print(f'当前价格: ${boll["current_price"]:.2f}')
    print(f'下轨: ${boll["lower"]:.2f}')
    print(f'中轨: ${boll["mid"]:.2f}')
    print(f'上轨: ${boll["upper"]:.2f}')

    distance = (boll['current_price'] - boll['lower']) / boll['lower'] * 100
    print(f'距离下轨: {distance:.2f}%')

    if boll['current_price'] < boll['lower']:
        print('✓ 低于下轨！')
    elif distance <= 2:
        print('✓ 接近下轨！')
    else:
        print('✗ 正常区间')
else:
    print('获取数据失败')
