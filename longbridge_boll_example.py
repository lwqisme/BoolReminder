"""
完整的LongBridge API获取BOLL指标示例
需要先安装: pip install longbridge
需要配置token和app_key
"""

from datetime import datetime

try:
    from longbridge.openapi import QuoteContext, Config, Period, AdjustType  # type: ignore
    LONGBRIDGE_AVAILABLE = True
except ImportError:
    LONGBRIDGE_AVAILABLE = False
    print("警告: longbridge SDK未安装，请运行: pip install longbridge")

from boll_calculator import BOLLCalculator


def get_stock_boll_daily(symbol: str, period: int = 22, k: float = 2.0):
    """
    获取某只股票的BOLL指标当日数据
    
    Args:
        symbol: 股票代码，例如 "700.HK", "AAPL.US", "000001.SH"
        period: BOLL计算周期，默认22（22日布林带）
        k: 标准差倍数，默认2.0
        
    Returns:
        包含BOLL指标的字典，格式：
        {
            "symbol": "股票代码",
            "upper": 上轨,
            "mid": 中轨,
            "lower": 下轨,
            "current_price": 当前价格,
            "period": 周期,
            "k": 倍数
        }
    """
    if not LONGBRIDGE_AVAILABLE:
        print("请先安装longbridge SDK: pip install longbridge")
        return None
    
    try:
        # 初始化配置（需要从配置文件或环境变量读取）
        # 方式1: 从配置文件读取
        # config = Config.from_file("path/to/config.json")
        
        # 方式2: 从环境变量读取
        config = Config(
            app_key="",
            app_secret="",
            access_token=""
        )
        
        # 创建QuoteContext
        quote_ctx = QuoteContext(config)
        
        # 1. 获取历史K线数据（需要至少period根，建议多获取几根备用）
        candlesticks = quote_ctx.candlesticks(
            symbol=symbol,
            period=Period.Day,  # 日线
            count=period + 5,   # 多获取几根备用
            adjust_type=AdjustType.NoAdjust  # 不复权，也可以选择ForwardAdjust前复权
        )
        
        # 2. 提取收盘价列表（candlesticks返回的是列表）
        closes = [float(c.close) for c in candlesticks]
        
        # 3. 获取当前价格（可选）
        quotes = quote_ctx.quote([symbol])  # quote方法需要传入列表
        current_price = float(quotes[0].last_done) if quotes else None
        
        # 4. 计算BOLL指标
        calculator = BOLLCalculator(period=period, k=k)
        boll_result = calculator.calculate_boll(closes)
        
        # 5. 返回结果
        if boll_result:
            return {
                "symbol": symbol,
                **boll_result,
                "current_price": current_price,
                "timestamp": datetime.now().isoformat()
            }
        else:
            return None
        
    except Exception as e:
        print(f"获取BOLL指标失败: {e}")
        return None


def get_boll_with_realtime(symbol: str, period: int = 22, k: float = 2.0):
    """
    获取BOLL指标（包含实时价格判断）
    
    这个方法会：
    1. 获取历史K线计算BOLL
    2. 获取实时价格
    3. 判断当前价格相对于布林带的位置
    """
    try:
        # 获取BOLL数据
        boll_data = get_stock_boll_daily(symbol, period, k)
        
        if not boll_data:
            return None
        
        current_price = boll_data.get("current_price")
        upper = boll_data["upper"]
        mid = boll_data["mid"]
        lower = boll_data["lower"]
        
        # 判断价格位置
        if current_price:
            if current_price > upper:
                position = "上轨上方（超买区域）"
            elif current_price < lower:
                position = "下轨下方（超卖区域）"
            elif current_price > mid:
                position = "中轨上方"
            else:
                position = "中轨下方"
            
            boll_data["position"] = position
        
        return boll_data
        
    except Exception as e:
        print(f"获取BOLL指标失败: {e}")
        return None


# 使用示例
if __name__ == "__main__":
    # 示例：获取NVDA（英伟达）的BOLL指标
    symbol = "NVDA.US"
    
    print(f"正在获取 {symbol} 的BOLL指标...")
    result = get_boll_with_realtime(symbol, period=22, k=2.0)
    
    if result:
        print(f"\n股票代码: {result['symbol']}")
        print(f"当前价格: ${result.get('current_price', 'N/A'):.2f}")
        print(f"\nBOLL指标（{result['period']}日，k={result['k']}）:")
        print(f"  上轨: ${result['upper']:.4f}")
        print(f"  中轨: ${result['mid']:.4f}")
        print(f"  下轨: ${result['lower']:.4f}")
        if 'position' in result:
            print(f"\n价格位置: {result['position']}")
    else:
        print("获取失败，请检查配置")
