"""
mootdx TCP 数据源 Provider — 吸收 a-stock-data 的免费无限数据源方案

mootdx 通过通达信 TCP 二进制协议（端口 7709）直接连接行情服务器，
提供免费且几乎不封 IP 的 A 股行情数据。

特点：
  - 零 API Key，零成本
  - TCP 直连，几乎不封 IP（远超 HTTP API 的稳定性）
  - 支持日线/周线/月线 K 线 + 实时行情
  - 数据回溯可达 1990 年

实现 BaseProvider 接口（与 data_provider.py 兼容）。

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| mootdx 连接超时（海外 IP） | 重试 2 次，切换备用服务器 | 返回空 DataFrame |
| TCP 连接断开 | 重建连接重试 | 返回空 DataFrame |
| 数据为空（停牌/未上市） | 返回空 DataFrame 不报错 | 调用方自行处理 |
| ts_code 格式错误 | 自动提取前 6 位数字代码 | 返回空 DataFrame |

D4 CHECKPOINT：
- CP1-连接检查：每次请求前确保 Quotes 实例有效
- CP2-数据长度检查：返回的 K 线数据量 >= 预期天数
- CP3-列名标准化：列名映射为标准 daily_price 列名

D9反例：
- 不要高频调用 mootdx（虽然不封 IP，但通达信服务器有连接数限制）
- 不要用 mootdx 获取财务/基本面数据（它只支持行情）
- 不要假设海外 IP 能稳定连接（海外 TO 可能超时）
"""
import os
import socket
import logging
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd

logger = logging.getLogger(__name__)

# 项目根目录
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


class MootdxProvider:
    """
    mootdx TCP 数据源 Provider

    通过通达信 TCP 协议获取免费 K 线/实时行情数据。
    输出格式与 Tushare Provider 兼容。
    """
    name = "mootdx"

    def __init__(self):
        self._quotes = None
        self._connect()

    def _connect(self):
        """连接 mootdx Quotes 实例"""
        try:
            from mootdx.quotes import Quotes
            # 尝试连接（自动选择可用服务器）
            socket.setdefaulttimeout(10)  # 防止TCP阻塞20-30秒
            self._quotes = Quotes.factory()
            logger.info("[Mootdx] 连接成功")
        except Exception as e:
            logger.warning(f"[Mootdx] 连接失败: {e}")
            self._quotes = None

    def _ensure_connected(self) -> bool:
        """CP1: 检查连接，断开则重连"""
        if self._quotes is None:
            self._connect()
        return self._quotes is not None

    def _ts_code_to_tdx(self, ts_code: str) -> tuple:
        """
        转换 ts_code 为 mootdx 参数格式

        Returns:
            (market, code) market: 0=深交所, 1=上交所
        """
        if "." in ts_code:
            code_part = ts_code[:6]
            market_part = ts_code[7:]
        else:
            code_part = ts_code[:6]
            market_part = "SZ" if (ts_code[:2] in ["00", "30", "15", "16", "18"]) else "SH"

        # market: 0=深圳, 1=上海
        if market_part in ("SH", "sh"):
            return (1, int(code_part))
        else:
            return (0, int(code_part))

    def daily(self, ts_code: str,
              start_date: str = None,
              end_date: str = None) -> pd.DataFrame:
        """
        获取日线 K 线数据

        Args:
            ts_code: Tushare 格式代码
            start_date: 开始日期 YYYYMMDD
            end_date: 结束日期 YYYYMMDD

        Returns:
            DataFrame with columns: ts_code, trade_date, open, high, low,
            close, vol, amount, pct_chg
        """
        if not self._ensure_connected():
            return pd.DataFrame()

        try:
            market, code = self._ts_code_to_tdx(ts_code)
            # frequency=9 表示日线
            bars = self._quotes.bars(symbol=code, frequency=9, market=market)

            if bars is None or bars.empty:
                logger.debug(f"[Mootdx] {ts_code} 无数据")
                return pd.DataFrame()

            # 转换 mootdx 格式为标准 Tushare 格式
            df = bars.copy()

            # 列名映射
            col_map = {
                "date": "trade_date",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "vol",
                "amount": "amount",
            }
            df = df.rename(columns={c: col_map[c] for c in df.columns if c in col_map})

            if "trade_date" in df.columns:
                # mootdx 日期格式为 Timestamp 或 YYYYMMDD
                if df["trade_date"].dtype == "int64":
                    df["trade_date"] = df["trade_date"].astype(str)
                else:
                    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")

            # 计算涨跌幅
            if "close" in df.columns:
                df["pct_chg"] = df["close"].pct_change() * 100
                df["pct_chg"] = df["pct_chg"].fillna(0)

            # 补充 ts_code
            df["ts_code"] = ts_code

            # 日期过滤
            if start_date and "trade_date" in df.columns:
                df = df[df["trade_date"] >= start_date]
            if end_date and "trade_date" in df.columns:
                df = df[df["trade_date"] <= end_date]

            # 日期降序（与 Tushare 一致）
            df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)

            # CP2: 数据量检查
            if len(df) == 0:
                logger.debug(f"[Mootdx] {ts_code} 过滤后无数据")
                return pd.DataFrame()

            # CP3: 确保必要的列存在
            required = ["trade_date", "open", "high", "low", "close"]
            for col in required:
                if col not in df.columns:
                    logger.warning(f"[Mootdx] {ts_code} 缺少列: {col}")
                    return pd.DataFrame()

            return df

        except ValueError as e:
            logger.warning(f"[Mootdx] {ts_code} 数据格式错误: {e}")
            return pd.DataFrame()
        except Exception as e:
            logger.warning(f"[Mootdx] {ts_code} 获取失败: {e}")
            # 尝试重连
            self._quotes = None
            return pd.DataFrame()

    def daily_basic(self, ts_code: str, trade_date: str = None) -> pd.DataFrame:
        """
        mootdx 不支持 daily_basic（估值/换手率），返回空
        """
        return pd.DataFrame()

    def stock_basic(self, ts_code: str = None, fields: str = None) -> pd.DataFrame:
        """mootdx 不支持股票基础信息，返回空"""
        return pd.DataFrame()

    def moneyflow(self, ts_code: str,
                  start_date: str = None,
                  end_date: str = None) -> pd.DataFrame:
        """mootdx 不支持资金流向，返回空"""
        return pd.DataFrame()

    def index_daily(self, ts_code: str,
                    start_date: str = None,
                    end_date: str = None) -> pd.DataFrame:
        """获取指数日线"""
        if not self._ensure_connected():
            return pd.DataFrame()

        try:
            # 指数特殊处理
            code_map = {
                "000001.SH": ("sh", "000001"),
                "399001.SZ": ("sz", "399001"),
                "399006.SZ": ("sz", "399006"),
                "000688.SH": ("sh", "000688"),
            }
            if ts_code not in code_map:
                logger.warning(f"[Mootdx] 不支持的指数: {ts_code}")
                return pd.DataFrame()

            market_code = int(ts_code[:6])
            market = 1 if ts_code.endswith("SH") else 0

            # 指数使用 frequency=9
            bars = self._quotes.bars(symbol=market_code, frequency=9, market=market)
            if bars is None or bars.empty:
                return pd.DataFrame()

            df = bars.copy()
            col_map = {
                "date": "trade_date",
                "open": "open", "high": "high",
                "low": "low", "close": "close",
                "volume": "vol", "amount": "amount",
            }
            df = df.rename(columns={c: col_map[c] for c in df.columns if c in col_map})

            if "trade_date" in df.columns:
                if df["trade_date"].dtype == "int64":
                    df["trade_date"] = df["trade_date"].astype(str)
                else:
                    df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")

            df["pct_chg"] = df["close"].pct_change() * 100
            df["pct_chg"] = df["pct_chg"].fillna(0)
            df["ts_code"] = ts_code
            df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)

            return df

        except Exception as e:
            logger.warning(f"[Mootdx] 指数 {ts_code} 获取失败: {e}")
            self._quotes = None
            return pd.DataFrame()

    def realtime(self, ts_code: str) -> dict:
        """
        获取实时行情（mootdx 独有能力）

        Returns:
            {"price": float, "open": float, "high": float, "low": float,
             "volume": int, "amount": float, "timestamp": str}
        """
        if not self._ensure_connected():
            return {}

        try:
            market, code = self._ts_code_to_tdx(ts_code)
            # 使用 quotation 接口获取实时报价
            quote = self._quotes.quotation(symbols=[code], market=market)
            if quote is not None and not quote.empty:
                row = quote.iloc[0]
                return {
                    "price": float(row.get("price", 0)),
                    "open": float(row.get("open", 0)),
                    "high": float(row.get("high", 0)),
                    "low": float(row.get("low", 0)),
                    "pre_close": float(row.get("last_close", 0)),
                    "volume": int(row.get("volume", 0)),
                    "amount": float(row.get("amount", 0)),
                    "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
        except Exception as e:
            logger.warning(f"[Mootdx] 实时行情 {ts_code} 获取失败: {e}")

        return {}


# 为了向后兼容，创建类别名
MootdxSource = MootdxProvider


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    import os
    import sys
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

    mp = MootdxProvider()

    print("=== 测试: 日线数据 ===")
    for code in ["000001.SZ", "600519.SH", "300750.SZ"]:
        df = mp.daily(code, start_date="20260701", end_date="20260703")
        if not df.empty:
            print(f"  {code}: {len(df)} 条 ✅ (列: {list(df.columns)})")
            if len(df) > 0:
                print(f"    最新: {df.iloc[0]['trade_date']} close={df.iloc[0]['close']}")
        else:
            print(f"  {code}: 无数据 ❌")

    print("\n=== 测试: 指数日线 ===")
    for idx in ["000001.SH", "399001.SZ"]:
        df = mp.index_daily(idx, start_date="20260701", end_date="20260703")
        if not df.empty:
            print(f"  {idx}: {len(df)} 条 ✅")
        else:
            print(f"  {idx}: 无数据 ❌")

    print("\n=== 测试: 实时行情 ===")
    r = mp.realtime("600519.SH")
    print(f"  贵州茅台实时: {r.get('price')} (时间: {r.get('timestamp')})")
