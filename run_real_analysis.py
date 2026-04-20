"""
运行真实分析，测试期权支持
"""

import sys
from pathlib import Path

# 添加项目根目录到路径
sys.path.insert(0, str(Path(__file__).parent))

from watchlist_boll_filter import main

if __name__ == '__main__':
    print("\n" + "=" * 80)
    print("开始运行真实分析（包含期权）")
    print("=" * 80 + "\n")

    # 运行分析，显示详细信息
    result = main(verbose=True)

    if result:
        print("\n" + "=" * 80)
        print("分析完成！")
        print("=" * 80)
        result.print_summary()
    else:
        print("\n分析失败")
