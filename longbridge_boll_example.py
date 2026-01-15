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
            app_key="c4c4c413297059590cec25e0610439d1",
            app_secret="dec4555478e52c467ed8d0edc5832922579d17870ea34826ed06d338e7ee2b9d",
            access_token="m_eyJhbGciOiJSUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJsb25nYnJpZGdlIiwic3ViIjoiYWNjZXNzX3Rva2VuIiwiZXhwIjoxNzc2MjQyNTU1LCJpYXQiOjE3Njg0NjY1NTUsImFrIjoiYzRjNGM0MTMyOTcwNTk1OTBjZWMyNWUwNjEwNDM5ZDEiLCJhYWlkIjoyMDQ1MjAwOSwiYWMiOiJsYiIsIm1pZCI6MTUwODM1NTEsInNpZCI6Inc0MzJ4VjV6eVN0aWo0dndNUEg3YUE9PSIsImJsIjozLCJ1bCI6MCwiaWsiOiJsYl8yMDQ1MjAwOSJ9.u6wCZE6H9aK6OV-tVbeiUuG1l5mq0vbZNGjAJqBzZMuaGTZUuN154IcFCLY7Cgk1y21O6hHKq9ltwTcru7MxcCKE-qZEZ8W0PlVDsoTvI3oaA8v07JpKFkkwV8KS_yTQSggoCz6Tsn0GZqO5SviQU_PHxfoz5CiLpXu-1EBiUj9kS2gaqx2Ibyy7JSAvQnjn-vFPCRwHt50tE8VfwxMwxFI2thl9ydQ-xCwJtWRCKhw25vA8UFOBjYu2A3BnfDo--2nYp-Nxw9HCFqa4Pgacl4J_7IGyDLFiOqvKJvy7M1E2mpl7NFDZLFpXKZLdal59Lz08ELZiLjDMK1Irct32GhkFdaw4H9aSEGuCOCd8jaqbM2FWiIhu-EeWkg2EXo7h6Xv6NV0gYVxRzL1FwedX9zm7cn_fHiRdSUe6DGqZxJwpV6F9ob09V9MXkuqTKuUdV9sMwq64f4NPaK1lDZWzh2iPxvU4czTJUwxUwk_3X7xA4EPfxRIbbNTIDLNccwEa9oGW2dsdwUbYcu8C10gG_8IFjxSTgCDe4_Q_HOrfLX0xExDA5NnaZHLi-vy3py7BaPDKzXkzz3iPxHZgtPGrMGZ_2ROmz49kxlEFVeDpMVEO4k7TQTh3RXTdf7cZApDAhtHR-BNLRAGgAZNyFmCexd5dmlnrwXXEehBUNHtb3-I"
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
