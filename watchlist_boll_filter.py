"""
从自选列表中筛选接近BOLL上下轨的股票
筛选条件：
- 下轨：价格低于下轨 或 价格差10%就到下轨
- 上轨：价格高于上轨 或 价格差10%就到上轨
"""

from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any
from dataclasses import dataclass, field

try:
    from longbridge.openapi import QuoteContext, Config, Period, AdjustType  # type: ignore
    LONGBRIDGE_AVAILABLE = True
except ImportError:
    LONGBRIDGE_AVAILABLE = False
    print("警告: longbridge SDK未安装，请运行: pip install longbridge")

from boll_calculator import BOLLCalculator


# 市场货币映射表
MARKET_CURRENCY = {
    '.US': ('$', 'USD'),      # 美元
    '.HK': ('H', 'HKD'),    # 港币
    '.SH': ('¥', 'CNY'),      # 人民币（上海）
    '.SZ': ('¥', 'CNY'),      # 人民币（深圳）
    '.T': ('¥', 'JPY'),       # 日元
    '.SI': ('S$', 'SGD'),     # 新加坡元
}


def get_currency_info(symbol: str) -> Tuple[str, str]:
    """获取股票对应的货币符号和货币代码
    
    Args:
        symbol: 股票代码，例如 "AAPL.US", "700.HK", "600000.SH"
    
    Returns:
        (货币符号, 货币代码)，例如 ("$", "USD")
    """
    for suffix, (currency_symbol, currency_code) in MARKET_CURRENCY.items():
        if symbol.endswith(suffix):
            return currency_symbol, currency_code
    
    # 默认返回美元
    return '$', 'USD'


@dataclass
class StockInfo:
    """单个股票的BOLL信息"""
    symbol: str
    display_name: str
    current_price: float
    lower_band: float
    mid_band: float
    upper_band: float
    distance_from_lower_pct: float
    distance_from_upper_pct: float
    position_pct: float
    currency_symbol: str = '$'
    currency_code: str = 'USD'


@dataclass
class WatchlistBollFilterResult:
    """BOLL筛选结果结构化对象"""
    # 基本配置信息
    period: int = 22
    k: float = 2.0
    threshold: float = 0.10
    total_analyzed: int = 0
    total_found: int = 0
    update_time: str = ""
    
    # 筛选出的股票列表
    below_lower: List[StockInfo] = field(default_factory=list)
    near_lower: List[StockInfo] = field(default_factory=list)
    near_upper: List[StockInfo] = field(default_factory=list)
    above_upper: List[StockInfo] = field(default_factory=list)
    
    # 所有股票代码和名称映射
    all_symbols: List[str] = field(default_factory=list)
    symbol_to_name: Dict[str, str] = field(default_factory=dict)
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式"""
        return {
            "config": {
                "period": self.period,
                "k": self.k,
                "threshold": self.threshold
            },
            "summary": {
                "total_analyzed": self.total_analyzed,
                "total_found": self.total_found,
                "below_lower_count": len(self.below_lower),
                "near_lower_count": len(self.near_lower),
                "near_upper_count": len(self.near_upper),
                "above_upper_count": len(self.above_upper),
                "update_time": self.update_time
            },
            "results": {
                "below_lower": [self._stock_info_to_dict(s) for s in self.below_lower],
                "near_lower": [self._stock_info_to_dict(s) for s in self.near_lower],
                "near_upper": [self._stock_info_to_dict(s) for s in self.near_upper],
                "above_upper": [self._stock_info_to_dict(s) for s in self.above_upper]
            },
            "all_symbols": self.all_symbols,
            "symbol_to_name": self.symbol_to_name
        }
    
    def _stock_info_to_dict(self, stock: StockInfo) -> Dict[str, Any]:
        """将StockInfo转换为字典"""
        return {
            "symbol": stock.symbol,
            "display_name": stock.display_name,
            "current_price": stock.current_price,
            "lower_band": stock.lower_band,
            "mid_band": stock.mid_band,
            "upper_band": stock.upper_band,
            "distance_from_lower_pct": stock.distance_from_lower_pct,
            "distance_from_upper_pct": stock.distance_from_upper_pct,
            "position_pct": stock.position_pct
        }
    
    def print_summary(self) -> None:
        """打印汇总信息"""
        print("=" * 80)
        print(f"筛选结果：找到 {self.total_found} 只需要关注的股票")
        print("=" * 80)
        print(f"\n📊 统计汇总:")
        print(f"  分析总数: {self.total_analyzed}")
        print(f"  🔴 低于下轨（超卖）: {len(self.below_lower)} 只")
        print(f"  🟡 接近下轨: {len(self.near_lower)} 只")
        print(f"  🟠 接近上轨: {len(self.near_upper)} 只")
        print(f"  🔵 超出上轨（超买）: {len(self.above_upper)} 只")
        print(f"\n配置参数: 周期={self.period}, k={self.k}, 阈值={self.threshold * 100}%")
        print(f"更新时间: {self.update_time}")
    
    def print_detailed_results(self) -> None:
        """打印详细结果表格"""
        self._print_below_lower()
        self._print_near_lower()
        self._print_near_upper()
        self._print_above_upper()
    
    def _print_below_lower(self) -> None:
        """打印低于下轨的股票"""
        if not self.below_lower:
            return
        
        print(f"\n🔴 【低于下轨 - 超卖区域】({len(self.below_lower)} 只)")
        print("=" * 90)
        print(f"{'股票名称':<25} {'当前价格':<12} {'下轨':<12} {'中轨':<12} {'上轨':<12} {'距离下轨':<12}")
        print("-" * 90)
        
        for stock in self.below_lower:
            display_name = stock.display_name[:24] if len(stock.display_name) > 24 else stock.display_name
            print(f"{display_name:<25} "
                  f"{stock.currency_symbol}{stock.current_price:<11.2f} "
                  f"{stock.currency_symbol}{stock.lower_band:<11.4f} "
                  f"{stock.currency_symbol}{stock.mid_band:<11.4f} "
                  f"{stock.currency_symbol}{stock.upper_band:<11.4f} "
                  f"{stock.distance_from_lower_pct:>10.2f}% ⚠️")
    
    def _print_near_lower(self) -> None:
        """打印接近下轨的股票"""
        if not self.near_lower:
            return
        
        print(f"\n🟡 【接近下轨】(2%以内) ({len(self.near_lower)} 只)")
        print("=" * 90)
        print(f"{'股票名称':<25} {'当前价格':<12} {'下轨':<12} {'中轨':<12} {'上轨':<12} {'距离下轨':<12}")
        print("-" * 90)
        
        for stock in self.near_lower:
            display_name = stock.display_name[:24] if len(stock.display_name) > 24 else stock.display_name
            print(f"{display_name:<25} "
                  f"{stock.currency_symbol}{stock.current_price:<11.2f} "
                  f"{stock.currency_symbol}{stock.lower_band:<11.4f} "
                  f"{stock.currency_symbol}{stock.mid_band:<11.4f} "
                  f"{stock.currency_symbol}{stock.upper_band:<11.4f} "
                  f"{stock.distance_from_lower_pct:>10.2f}%")
    
    def _print_near_upper(self) -> None:
        """打印接近上轨的股票"""
        if not self.near_upper:
            return
        
        print(f"\n🟠 【接近上轨】(2%以内) ({len(self.near_upper)} 只)")
        print("=" * 90)
        print(f"{'股票名称':<25} {'当前价格':<12} {'下轨':<12} {'中轨':<12} {'上轨':<12} {'距离上轨':<12}")
        print("-" * 90)
        
        for stock in self.near_upper:
            distance_str = f"{abs(stock.distance_from_upper_pct):.2f}%"
            display_name = stock.display_name[:24] if len(stock.display_name) > 24 else stock.display_name
            print(f"{display_name:<25} "
                  f"{stock.currency_symbol}{stock.current_price:<11.2f} "
                  f"{stock.currency_symbol}{stock.lower_band:<11.4f} "
                  f"{stock.currency_symbol}{stock.mid_band:<11.4f} "
                  f"{stock.currency_symbol}{stock.upper_band:<11.4f} "
                  f"{distance_str:>10}")
    
    def _print_above_upper(self) -> None:
        """打印超出上轨的股票"""
        if not self.above_upper:
            return
        
        print(f"\n🔵 【超出上轨 - 超买区域】({len(self.above_upper)} 只)")
        print("=" * 90)
        print(f"{'股票名称':<25} {'当前价格':<12} {'下轨':<12} {'中轨':<12} {'上轨':<12} {'距离上轨':<12}")
        print("-" * 90)
        
        for stock in self.above_upper:
            display_name = stock.display_name[:24] if len(stock.display_name) > 24 else stock.display_name
            print(f"{display_name:<25} "
                  f"{stock.currency_symbol}{stock.current_price:<11.2f} "
                  f"{stock.currency_symbol}{stock.lower_band:<11.4f} "
                  f"{stock.currency_symbol}{stock.mid_band:<11.4f} "
                  f"{stock.currency_symbol}{stock.upper_band:<11.4f} "
                  f"{stock.distance_from_upper_pct:>10.2f}% ⚠️")
    
    def __str__(self) -> str:
        """字符串表示，用于直接打印对象"""
        lines = [
            "=" * 80,
            f"筛选结果：找到 {self.total_found} 只需要关注的股票",
            "=" * 80,
            f"\n📊 统计汇总:",
            f"  分析总数: {self.total_analyzed}",
            f"  🔴 低于下轨（超卖）: {len(self.below_lower)} 只",
            f"  🟡 接近下轨: {len(self.near_lower)} 只",
            f"  🟠 接近上轨: {len(self.near_upper)} 只",
            f"  🔵 超出上轨（超买）: {len(self.above_upper)} 只",
            f"\n配置参数: 周期={self.period}, k={self.k}, 阈值={self.threshold * 100}%",
            f"更新时间: {self.update_time}"
        ]
        
        return "\n".join(lines)


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
    exclude_options: bool = True,
    verbose: bool = False
) -> WatchlistBollFilterResult:
    """
    分析所有股票，按位置分类
    
    Args:
        quote_ctx: QuoteContext实例
        symbols: 股票代码列表
        period: BOLL计算周期
        k: 标准差倍数
        threshold: 接近上下轨的阈值（10% = 0.10）
        exclude_options: 是否排除期权
        verbose: 是否打印详细进度信息
        
    Returns:
        WatchlistBollFilterResult 结构化结果对象
    """
    result = WatchlistBollFilterResult(
        period=period,
        k=k,
        threshold=threshold,
        all_symbols=symbols.copy(),
        symbol_to_name=symbol_to_name.copy(),
        update_time=datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    )
    
    total = len(symbols)
    
    if verbose:
        print(f"开始分析 {total} 只股票...")
    
    for idx, symbol in enumerate(symbols, 1):
        if verbose:
            print(f"[{idx}/{total}] 正在分析 {symbol}...", end=" ")
        
        # 获取BOLL数据
        boll_data = get_stock_boll_data(quote_ctx, symbol, period, k)
        
        if not boll_data:
            if verbose:
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
        
        # 获取货币信息
        currency_symbol, currency_code = get_currency_info(symbol)
        
        stock_info = StockInfo(
            symbol=symbol,
            display_name=display_name,
            current_price=current_price,
            lower_band=lower_band,
            mid_band=mid_band,
            upper_band=upper_band,
            distance_from_lower_pct=distance_from_lower_pct,
            distance_from_upper_pct=distance_from_upper_pct,
            position_pct=((current_price - lower_band) / (upper_band - lower_band) * 100) if (upper_band - lower_band) > 0 else 50,
            currency_symbol=currency_symbol,
            currency_code=currency_code
        )
        
        # 分类判断
        if current_price < lower_band:
            result.below_lower.append(stock_info)
            if verbose:
                print(f"✓ 低于下轨 ({distance_from_lower_pct:.2f}%)")
        elif current_price > upper_band:
            result.above_upper.append(stock_info)
            if verbose:
                print(f"✓ 超出上轨 ({distance_from_upper_pct:.2f}%)")
        elif distance_from_lower_pct <= threshold * 100:
            result.near_lower.append(stock_info)
            if verbose:
                print(f"✓ 接近下轨 ({distance_from_lower_pct:.2f}%)")
        elif abs(distance_from_upper_pct) <= threshold * 100:
            result.near_upper.append(stock_info)
            if verbose:
                print(f"✓ 接近上轨 ({abs(distance_from_upper_pct):.2f}%)")
        else:
            if verbose:
                print(f"✗ 正常区间")
    
    # 更新统计信息
    result.total_analyzed = total
    result.total_found = len(result.below_lower) + len(result.near_lower) + \
                        len(result.near_upper) + len(result.above_upper)
    
    # 排序
    result.below_lower.sort(key=lambda x: x.distance_from_lower_pct)
    result.near_lower.sort(key=lambda x: x.distance_from_lower_pct)
    result.near_upper.sort(key=lambda x: x.distance_from_upper_pct, reverse=True)
    result.above_upper.sort(key=lambda x: x.distance_from_upper_pct, reverse=True)
    
    return result


def main(verbose: bool = False):
    """主函数
    
    Args:
        verbose: 是否显示详细进度信息
    
    Returns:
        WatchlistBollFilterResult 结构化结果对象
    """
    if not LONGBRIDGE_AVAILABLE:
        print("请先安装longbridge SDK: pip install longbridge")
        return None
    
    try:
        # 初始化配置
        config = Config(
            app_key="c4c4c413297059590cec25e0610439d1",
            app_secret="dec4555478e52c467ed8d0edc5832922579d17870ea34826ed06d338e7ee2b9d",
            access_token="m_eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJsb25nYnJpZGdlIiwic3ViIjoiYWNjZXNzX3Rva2VuIiwiZXhwIjoxNzc2MjQyNTU1LCJpYXQiOjE3Njg0NjY1NTUsImFrIjoiYzRjNGM0MTMyOTcwNTk1OTBjZWMyNWUwNjEwNDM5ZDEiLCJhYWlkIjoyMDQ1MjAwOSwiYWMiOiJsYiIsIm1pZCI6MTUwODM1NTEsInNpZCI6Inc0MzJ4VjV6eVN0aWo0dndNUEg3YUE9PSIsImJsIjozLCJ1bCI6MCwiaWsiOiJsYl8yMDQ1MjAwOSJ9.u6wCZE6H9aK6OV-tVbeiUuG1l5mq0vbZNGjAJqBzZMuaGTZUuN154IcFCLY7Cgk1y21O6hHKq9ltwTcru7MxcCKE-qZEZ8W0PlVDsoTvI3oaA8v07JpKFkkwV8KS_yTQSggoCz6Tsn0GZqO5SviQU_PHxfoz5CiLpXu-1EBiUj9kS2gaqx2Ibyy7JSAvQnjn-vFPCRwHt50tE8VfwxMwxFI2thl9ydQ-xCwJtWRCKhw25vA8UFOBjYu2A3BnfDo--2nYp-Nxw9HCFqa4Pgacl4J_7IGyDLFiOqvKJvy7M1E2mpl7NFDZLFpXKZLdal59Lz08ELZiLjDMK1Irct32GhkFdaw4H9aSEGuCOCd8jaqbM2FWiIhu-EeWkg2EXo7h6Xv6NV0gYVxRzL1FwedX9zm7cn_fHiRdSUe6DGqZxJwpV6F9ob09V9MXkuqTKuUdV9sMwq64f4NPaK1lDZWzh2iPxvU4czTJUwxUwk_3X7xA4EPfxRIbbNTIDLNccwEa9oGW2dsdwUbYcu8C10gG_8IFjxSTgCDe4_Q_HOrfLX0xExDA5NnaZHLi-vy3py7BaPDKzXkzz3iPxHZgtPGrMGZ_2ROmz49kxlEFVeDpMVEO4k7TQTh3RXTdf7cZApDAhtHR-BNLRAGgAZNyFmCexd5dmlnrwXXEehBUNHtb3-I"
        )
        
        quote_ctx = QuoteContext(config)
        
        # 1. 获取自选列表
        if verbose:
            print("=" * 60)
            print("获取自选列表...")
        symbols, symbol_to_name = get_watchlist_symbols(quote_ctx, exclude_options=True)
        if verbose:
            print(f"找到 {len(symbols)} 只股票（已排除期权）")
            print("=" * 60)
            print()
        
        # 2. 分析所有股票
        result = analyze_all_stocks(
            quote_ctx,
            symbols,
            symbol_to_name,
            period=22,
            k=2.0,
            threshold=0.02,  # 2%
            exclude_options=True,
            verbose=verbose
        )
        
        return result
        
    except Exception as e:
        print(f"执行失败: {e}")
        import traceback
        traceback.print_exc()
        return None


if __name__ == "__main__":
    # 执行分析并获取结构化结果对象
    result = main(verbose=True)
    
    if result is None:
        print("分析失败")
    else:
        # 打印详细结果表格
        result.print_detailed_results()
