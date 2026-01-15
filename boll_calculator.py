"""
BOLL指标计算器
通过LongBridge OpenAPI获取K线数据，计算布林带指标
"""

import statistics
from typing import List, Dict, Optional
from datetime import datetime


class BOLLCalculator:
    """布林带指标计算器"""
    
    def __init__(self, period: int = 22, k: float = 2.0):
        """
        初始化BOLL计算器
        
        Args:
            period: 计算周期，默认22（22日布林带）
            k: 标准差倍数，默认2.0
        """
        self.period = period
        self.k = k
    
    def calculate_boll(self, closes: List[float]) -> Optional[Dict[str, float]]:
        """
        计算布林带指标
        
        Args:
            closes: 收盘价列表，需要至少period个数据点
            
        Returns:
            包含上轨、中轨、下轨的字典，如果数据不足则返回None
        """
        if len(closes) < self.period:
            return None
        
        # 取最近period个收盘价
        recent_closes = closes[-self.period:]
        
        # 计算中轨（简单移动平均）
        mid = sum(recent_closes) / self.period
        
        # 计算标准差
        sd = statistics.stdev(recent_closes)
        
        # 计算上轨和下轨
        upper = mid + self.k * sd
        lower = mid - self.k * sd
        
        return {
            "upper": round(upper, 4),  # 上轨
            "mid": round(mid, 4),      # 中轨
            "lower": round(lower, 4),   # 下轨
            "period": self.period,
            "k": self.k
        }


def get_boll_from_longbridge(symbol: str, period: int = 22, k: float = 2.0):
    """
    从LongBridge API获取股票数据并计算BOLL指标
    
    注意：这个函数需要先安装longbridge SDK并配置token
    
    Args:
        symbol: 股票代码，例如 "700.HK", "AAPL.US"
        period: BOLL计算周期，默认20
        k: 标准差倍数，默认2.0
        
    Returns:
        BOLL指标数据字典
    """
    try:
        try:
            from longbridge.openapi import QuoteContext, Config, Period, AdjustType  # type: ignore
        except ImportError:
            print("请先安装longbridge SDK: pip install longbridge")
            return None
        
        # 这里需要配置你的token和app_key
        # config = Config.from_file("path/to/config.json")
        # quote_ctx = QuoteContext(config)
        
        # 获取历史K线数据（需要至少period+几根备用）
        # candlesticks = quote_ctx.candlesticks(
        #     symbol=symbol,
        #     period=Period.Day,  # 日线
        #     count=period + 5,  # 多获取几根备用
        #     adjust_type=AdjustType.NoAdjust
        # )
        
        # 提取收盘价
        # closes = [float(c.close) for c in candlesticks.candlesticks]
        
        # 计算BOLL
        # calculator = BOLLCalculator(period=period, k=k)
        # return calculator.calculate_boll(closes)
        
        print("请先配置LongBridge SDK并取消注释上面的代码")
        return None
        
    except ImportError:
        print("请先安装longbridge SDK: pip install longbridge")
        return None


# 示例使用
if __name__ == "__main__":
    # 示例：使用模拟数据计算BOLL
    calculator = BOLLCalculator(period=22, k=2.0)
    
    # 模拟22个收盘价数据
    sample_closes = [
        100.5, 101.2, 102.0, 101.8, 102.5,
        103.0, 102.8, 103.5, 104.0, 103.8,
        104.5, 105.0, 104.8, 105.5, 106.0,
        105.8, 106.5, 107.0, 106.8, 107.5,
        108.0, 107.8
    ]
    
    result = calculator.calculate_boll(sample_closes)
    if result:
        print(f"股票BOLL指标（周期={result['period']}, k={result['k']}）:")
        print(f"  上轨: {result['upper']}")
        print(f"  中轨: {result['mid']}")
        print(f"  下轨: {result['lower']}")
    else:
        print("数据不足，无法计算BOLL指标")
