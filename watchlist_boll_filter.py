"""
从自选列表中筛选接近BOLL上下轨的股票
筛选条件：
- 下轨：价格低于下轨 或 价格差10%就到下轨
- 上轨：价格高于上轨 或 价格差10%就到上轨
"""

from datetime import datetime
from typing import List, Dict, Optional, Tuple

try:
    from longbridge.openapi import QuoteContext, Config, Period, AdjustType  # type: ignore
    LONGBRIDGE_AVAILABLE = True
except ImportError:
    LONGBRIDGE_AVAILABLE = False
    print("警告: longbridge SDK未安装，请运行: pip install longbridge")

from boll_calculator import BOLLCalculator


def get_watchlist_symbols(quote_ctx: QuoteContext, exclude_options: bool = True) -> Tuple[List[str], Dict[str, str]]:
    """
    获取自选列表中的所有股票代码和名称映射
    
    Args:
        quote_ctx: QuoteContext实例
        exclude_options: 是否排除期权（默认True，只保留股票）
        
    Returns:
        (股票代码列表, 股票代码到名称的映射字典)
    """
    watchlist_groups = quote_ctx.watchlist()
    symbols = []
    symbol_to_name = {}
    
    for group in watchlist_groups:
        for security in group.securities:
            symbol = security.symbol
            # 排除期权（通常包含日期和C/P标识）
            if exclude_options and ('C' in symbol or 'P' in symbol) and any(char.isdigit() for char in symbol):
                continue
            if symbol not in symbols:
                symbols.append(symbol)
                symbol_to_name[symbol] = security.name
    
    return symbols, symbol_to_name


def get_stock_boll_data(quote_ctx: QuoteContext, symbol: str, period: int = 22, k: float = 2.0) -> Optional[Dict]:
    """
    获取单只股票的BOLL指标数据
    
    Args:
        quote_ctx: QuoteContext实例
        symbol: 股票代码
        period: BOLL计算周期
        k: 标准差倍数
        
    Returns:
        包含BOLL指标和当前价格的字典，如果失败返回None
    """
    try:
        # 1. 获取历史K线数据
        candlesticks = quote_ctx.candlesticks(
            symbol=symbol,
            period=Period.Day,
            count=period + 5,
            adjust_type=AdjustType.NoAdjust
        )
        
        if len(candlesticks) < period:
            return None
        
        # 2. 提取收盘价列表
        closes = [float(c.close) for c in candlesticks]
        
        # 3. 计算BOLL指标
        calculator = BOLLCalculator(period=period, k=k)
        boll_result = calculator.calculate_boll(closes)
        
        if not boll_result:
            return None
        
        # 4. 获取当前价格
        quotes = quote_ctx.quote([symbol])
        current_price = float(quotes[0].last_done) if quotes else None
        
        if current_price is None:
            return None
        
        return {
            "symbol": symbol,
            "current_price": current_price,
            **boll_result
        }
        
    except Exception as e:
        print(f"  获取 {symbol} 数据失败: {e}")
        return None


def analyze_all_stocks(
    quote_ctx: QuoteContext,
    symbols: List[str],
    symbol_to_name: Dict[str, str],
    period: int = 22,
    k: float = 2.0,
    threshold: float = 0.10,
    exclude_options: bool = True
) -> Dict[str, List[Dict]]:
    """
    分析所有股票，按位置分类
    
    Args:
        quote_ctx: QuoteContext实例
        symbols: 股票代码列表
        period: BOLL计算周期
        k: 标准差倍数
        threshold: 接近上下轨的阈值（10% = 0.10）
        exclude_options: 是否排除期权
        
    Returns:
        按位置分类的股票字典：
        {
            "below_lower": 低于下轨的股票列表,
            "near_lower": 接近下轨的股票列表,
            "near_upper": 接近上轨的股票列表,
            "above_upper": 超出上轨的股票列表
        }
    """
    results = {
        "below_lower": [],
        "near_lower": [],
        "near_upper": [],
        "above_upper": []
    }
    total = len(symbols)
    
    print(f"开始分析 {total} 只股票...")
    
    for idx, symbol in enumerate(symbols, 1):
        print(f"[{idx}/{total}] 正在分析 {symbol}...", end=" ")
        
        # 获取BOLL数据
        boll_data = get_stock_boll_data(quote_ctx, symbol, period, k)
        
        if not boll_data:
            print("数据不足，跳过")
            continue
        
        current_price = boll_data["current_price"]
        lower_band = boll_data["lower"]
        upper_band = boll_data["upper"]
        mid_band = boll_data["mid"]
        
        # 计算价格与下轨的距离百分比
        distance_from_lower_pct = (current_price - lower_band) / lower_band * 100
        
        # 计算价格与上轨的距离百分比
        distance_from_upper_pct = (current_price - upper_band) / upper_band * 100
        
        # 判断是否为美股，美股显示代码，其他市场显示名称
        is_us_stock = symbol.endswith('.US')
        display_name = symbol if is_us_stock else symbol_to_name.get(symbol, symbol)
        
        result = {
            "symbol": symbol,
            "display_name": display_name,  # 显示用的名称
            "current_price": current_price,
            "lower_band": lower_band,
            "mid_band": mid_band,
            "upper_band": upper_band,
            "distance_from_lower_pct": distance_from_lower_pct,
            "distance_from_upper_pct": distance_from_upper_pct,
            "position_pct": ((current_price - lower_band) / (upper_band - lower_band) * 100) if (upper_band - lower_band) > 0 else 50
        }
        
        # 分类判断
        if current_price < lower_band:
            # 低于下轨
            results["below_lower"].append(result)
            print(f"✓ 低于下轨 ({distance_from_lower_pct:.2f}%)")
        elif current_price > upper_band:
            # 超出上轨
            results["above_upper"].append(result)
            print(f"✓ 超出上轨 ({distance_from_upper_pct:.2f}%)")
        elif distance_from_lower_pct <= threshold * 100:
            # 接近下轨（在threshold范围内）
            results["near_lower"].append(result)
            print(f"✓ 接近下轨 ({distance_from_lower_pct:.2f}%)")
        elif abs(distance_from_upper_pct) <= threshold * 100:
            # 接近上轨（在threshold范围内）
            results["near_upper"].append(result)
            print(f"✓ 接近上轨 ({abs(distance_from_upper_pct):.2f}%)")
        else:
            print(f"✗ 正常区间")
    
    return results


def main():
    """主函数"""
    if not LONGBRIDGE_AVAILABLE:
        print("请先安装longbridge SDK: pip install longbridge")
        return
    
    try:
        # 初始化配置
        config = Config(
            app_key="c4c4c413297059590cec25e0610439d1",
            app_secret="dec4555478e52c467ed8d0edc5832922579d17870ea34826ed06d338e7ee2b9d",
            access_token="m_eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJsb25nYnJpZGdlIiwic3ViIjoiYWNjZXNzX3Rva2VuIiwiZXhwIjoxNzc2MjQyNTU1LCJpYXQiOjE3Njg0NjY1NTUsImFrIjoiYzRjNGM0MTMyOTcwNTk1OTBjZWMyNWUwNjEwNDM5ZDEiLCJhYWlkIjoyMDQ1MjAwOSwiYWMiOiJsYiIsIm1pZCI6MTUwODM1NTEsInNpZCI6Inc0MzJ4VjV6eVN0aWo0dndNUEg3YUE9PSIsImJsIjozLCJ1bCI6MCwiaWsiOiJsYl8yMDQ1MjAwOSJ9.u6wCZE6H9aK6OV-tVbeiUuG1l5mq0vbZNGjAJqBzZMuaGTZUuN154IcFCLY7Cgk1y21O6hHKq9ltwTcru7MxcCKE-qZEZ8W0PlVDsoTvI3oaA8v07JpKFkkwV8KS_yTQSggoCz6Tsn0GZqO5SviQU_PHxfoz5CiLpXu-1EBiUj9kS2gaqx2Ibyy7JSAvQnjn-vFPCRwHt50tE8VfwxMwxFI2thl9ydQ-xCwJtWRCKhw25vA8UFOBjYu2A3BnfDo--2nYp-Nxw9HCFqa4Pgacl4J_7IGyDLFiOqvKJvy7M1E2mpl7NFDZLFpXKZLdal59Lz08ELZiLjDMK1Irct32GhkFdaw4H9aSEGuCOCd8jaqbM2FWiIhu-EeWkg2EXo7h6Xv6NV0gYVxRzL1FwedX9zm7cn_fHiRdSUe6DGqZxJwpV6F9ob09V9MXkuqTKuUdV9sMwq64f4NPaK1lDZWzh2iPxvU4czTJUwxUwk_3X7xA4EPfxRIbbNTIDLNccwEa9oGW2dsdwUbYcu8C10gG_8IFjxSTgCDe4_Q_HOrfLX0xExDA5NnaZHLi-vy3py7BaPDKzXkzz3iPxHZgtPGrMGZ_2ROmz49kxlEFVeDpMVEO4k7TQTh3RXTdf7cZApDAhtHR-BNLRAGgAZNyFmCexd5dmlnrwXXEehBUNHtb3-I"
        )
        
        quote_ctx = QuoteContext(config)
        
        # 1. 获取自选列表
        print("=" * 60)
        print("获取自选列表...")
        symbols, symbol_to_name = get_watchlist_symbols(quote_ctx, exclude_options=True)
        print(f"找到 {len(symbols)} 只股票（已排除期权）")
        print("=" * 60)
        print()
        
        # 2. 分析所有股票
        results = analyze_all_stocks(
            quote_ctx,
            symbols,
            symbol_to_name,
            period=22,
            k=2.0,
            threshold=0.10,  # 10%
            exclude_options=True
        )
        
        # 3. 输出结果（分组显示）
        print()
        print("=" * 80)
        total_found = len(results["below_lower"]) + len(results["near_lower"]) + \
                     len(results["near_upper"]) + len(results["above_upper"])
        print(f"筛选结果：找到 {total_found} 只需要关注的股票")
        print("=" * 80)
        
        # 3.1 低于下轨的股票（超卖区域）
        if results["below_lower"]:
            results["below_lower"].sort(key=lambda x: x["distance_from_lower_pct"])
            print(f"\n🔴 【低于下轨 - 超卖区域】({len(results['below_lower'])} 只)")
            print("=" * 90)
            print(f"{'股票名称':<25} {'当前价格':<12} {'下轨':<12} {'中轨':<12} {'上轨':<12} {'距离下轨':<12}")
            print("-" * 90)
            for result in results["below_lower"]:
                display_name = result['display_name'][:24] if len(result['display_name']) > 24 else result['display_name']
                print(f"{display_name:<25} "
                      f"${result['current_price']:<11.2f} "
                      f"${result['lower_band']:<11.4f} "
                      f"${result['mid_band']:<11.4f} "
                      f"${result['upper_band']:<11.4f} "
                      f"{result['distance_from_lower_pct']:>10.2f}% ⚠️")
        
        # 3.2 接近下轨的股票
        if results["near_lower"]:
            results["near_lower"].sort(key=lambda x: x["distance_from_lower_pct"])
            print(f"\n🟡 【接近下轨】(10%以内) ({len(results['near_lower'])} 只)")
            print("=" * 90)
            print(f"{'股票名称':<25} {'当前价格':<12} {'下轨':<12} {'中轨':<12} {'上轨':<12} {'距离下轨':<12}")
            print("-" * 90)
            for result in results["near_lower"]:
                display_name = result['display_name'][:24] if len(result['display_name']) > 24 else result['display_name']
                print(f"{display_name:<25} "
                      f"${result['current_price']:<11.2f} "
                      f"${result['lower_band']:<11.4f} "
                      f"${result['mid_band']:<11.4f} "
                      f"${result['upper_band']:<11.4f} "
                      f"{result['distance_from_lower_pct']:>10.2f}%")
        
        # 3.3 接近上轨的股票
        if results["near_upper"]:
            results["near_upper"].sort(key=lambda x: x["distance_from_upper_pct"], reverse=True)
            print(f"\n🟠 【接近上轨】(10%以内) ({len(results['near_upper'])} 只)")
            print("=" * 90)
            print(f"{'股票名称':<25} {'当前价格':<12} {'下轨':<12} {'中轨':<12} {'上轨':<12} {'距离上轨':<12}")
            print("-" * 90)
            for result in results["near_upper"]:
                distance_str = f"{abs(result['distance_from_upper_pct']):.2f}%"
                display_name = result['display_name'][:24] if len(result['display_name']) > 24 else result['display_name']
                print(f"{display_name:<25} "
                      f"${result['current_price']:<11.2f} "
                      f"${result['lower_band']:<11.4f} "
                      f"${result['mid_band']:<11.4f} "
                      f"${result['upper_band']:<11.4f} "
                      f"{distance_str:>10}")
        
        # 3.4 超出上轨的股票（超买区域）
        if results["above_upper"]:
            results["above_upper"].sort(key=lambda x: x["distance_from_upper_pct"], reverse=True)
            print(f"\n🔵 【超出上轨 - 超买区域】({len(results['above_upper'])} 只)")
            print("=" * 90)
            print(f"{'股票名称':<25} {'当前价格':<12} {'下轨':<12} {'中轨':<12} {'上轨':<12} {'距离上轨':<12}")
            print("-" * 90)
            for result in results["above_upper"]:
                display_name = result['display_name'][:24] if len(result['display_name']) > 24 else result['display_name']
                print(f"{display_name:<25} "
                      f"${result['current_price']:<11.2f} "
                      f"${result['lower_band']:<11.4f} "
                      f"${result['mid_band']:<11.4f} "
                      f"${result['upper_band']:<11.4f} "
                      f"{result['distance_from_upper_pct']:>10.2f}% ⚠️")
        
        if total_found == 0:
            print("\n未找到符合条件的股票")
        
        print(f"\n更新时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        
    except Exception as e:
        print(f"执行失败: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
