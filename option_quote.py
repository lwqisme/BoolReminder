"""
期权价格查询模块
使用Polygon.io (Massive) API查询期权实时价格
"""

from typing import Optional, Dict, Any
import logging
import requests
from datetime import datetime

logger = logging.getLogger(__name__)


class OptionQuoteService:
    """期权报价服务 - 使用Polygon.io API"""

    def __init__(self, api_key: str):
        """
        初始化期权报价服务

        Args:
            api_key: Polygon.io API Key
        """
        self.api_key = api_key
        self.base_url = "https://api.polygon.io"

    def _convert_symbol_to_polygon_format(self, symbol: str) -> str:
        """
        将LongBridge格式的期权代码转换为Polygon.io格式

        LongBridge格式: MSFT270115C480000.US
        Polygon格式: O:MSFT270115C00480000

        Args:
            symbol: LongBridge格式的期权代码

        Returns:
            Polygon.io格式的期权代码
        """
        # 移除 .US 后缀
        symbol = symbol.replace('.US', '')

        # 提取各部分
        # 格式: TICKER + YYMMDD + C/P + STRIKE
        # 例如: MSFT270115C480000

        # 找到C或P的位置
        call_put_index = -1
        for i, char in enumerate(symbol):
            if char in ['C', 'P'] and i > 4:  # 确保不是股票代码中的C或P
                call_put_index = i
                break

        if call_put_index == -1:
            raise ValueError(f"无法解析期权代码: {symbol}")

        ticker = symbol[:call_put_index-6]  # 股票代码
        date = symbol[call_put_index-6:call_put_index]  # YYMMDD
        call_put = symbol[call_put_index]  # C或P
        strike = symbol[call_put_index+1:]  # 行权价

        # Polygon格式需要8位行权价（前面补0）
        strike_formatted = strike.zfill(8)

        # 组合成Polygon格式: O:TICKER + YYMMDD + C/P + 8位行权价
        polygon_symbol = f"O:{ticker}{date}{call_put}{strike_formatted}"

        logger.info(f"转换期权代码: {symbol} -> {polygon_symbol}")
        return polygon_symbol

    def get_option_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取期权报价（使用日终数据）

        Args:
            symbol: 期权代码，例如 "MSFT270115C480000.US"

        Returns:
            包含期权报价信息的字典
        """
        try:
            # 转换为Polygon格式
            polygon_symbol = self._convert_symbol_to_polygon_format(symbol)

            # 使用日终数据API（免费计划支持）
            # 端点: /v2/aggs/ticker/{ticker}/prev
            url = f"{self.base_url}/v2/aggs/ticker/{polygon_symbol}/prev"
            params = {
                "apiKey": self.api_key,
                "adjusted": "true"
            }

            logger.info(f"请求Polygon.io日终数据API: {url}")

            # 发送请求
            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()

            data = response.json()

            # 检查是否有数据
            if data.get("status") != "OK":
                logger.warning(f"API返回状态异常: {data.get('status')}")
                return None

            results = data.get("results", [])
            if not results or len(results) == 0:
                logger.warning(f"未找到期权 {symbol} 的报价")
                return None

            quote = results[0]

            # 提取报价信息（日终数据）
            result = {
                "symbol": symbol,
                "close": quote.get("c"),  # 收盘价
                "open": quote.get("o"),   # 开盘价
                "high": quote.get("h"),   # 最高价
                "low": quote.get("l"),    # 最低价
                "volume": quote.get("v"), # 成交量
                "timestamp": quote.get("t"),
                "data_type": "daily"  # 标记这是日终数据
            }

            logger.info(f"成功获取期权 {symbol} 的日终数据")
            return result

        except requests.exceptions.RequestException as e:
            logger.error(f"API请求失败: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"获取期权报价失败: {str(e)}")
            return None

    def get_option_history(self, symbol: str, days: int = 30) -> Optional[list]:
        """
        获取期权历史K线数据

        Args:
            symbol: 期权代码，例如 "MSFT270115C480000.US"
            days: 获取天数

        Returns:
            历史K线数据列表，每个元素包含 open, high, low, close
        """
        try:
            # 转换为Polygon格式
            polygon_symbol = self._convert_symbol_to_polygon_format(symbol)

            # 计算日期范围
            from datetime import datetime, timedelta
            end_date = datetime.now()
            start_date = end_date - timedelta(days=days)

            # 使用聚合数据API获取历史数据
            url = f"{self.base_url}/v2/aggs/ticker/{polygon_symbol}/range/1/day/{start_date.strftime('%Y-%m-%d')}/{end_date.strftime('%Y-%m-%d')}"
            params = {
                "apiKey": self.api_key,
                "adjusted": "true"
            }

            logger.info(f"请求期权历史数据: {url}")

            response = requests.get(url, params=params, timeout=10)
            response.raise_for_status()

            data = response.json()

            if data.get("status") not in ["OK", "DELAYED"]:
                logger.warning(f"API返回状态异常: {data.get('status')}")
                return None

            results = data.get("results", [])
            if not results:
                logger.warning(f"未找到期权 {symbol} 的历史数据")
                return None

            # 转换为标准格式
            history = []
            for bar in results:
                history.append({
                    "open": bar.get("o"),
                    "high": bar.get("h"),
                    "low": bar.get("l"),
                    "close": bar.get("c"),
                    "volume": bar.get("v"),
                    "timestamp": bar.get("t")
                })

            logger.info(f"成功获取期权 {symbol} 的 {len(history)} 天历史数据")
            return history

        except requests.exceptions.RequestException as e:
            logger.error(f"API请求失败: {str(e)}")
            return None
        except Exception as e:
            logger.error(f"获取期权历史数据失败: {str(e)}")
            return None
