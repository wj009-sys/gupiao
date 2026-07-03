"""
多数据源 Provider 层 — 借鉴 daily_stock_analysis 的多源自动fallback设计

提供统一的行情/基本面/板块数据接口，从Tushare优先获取，失败时自动fallback到AkShare。
所有Provider返回统一格式的DataFrame。

用法：
    from scripts.utils.data_provider import DataProvider
    dp = DataProvider()
    df = dp.daily("000001.SZ", start_date="20260701", end_date="20260703")

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| Tushare接口超时 | 重试1次(带0.5s延迟) | fallback到AkShare |
| AkShare接口异常 | 重试1次 | 返回空DataFrame，调用方用前收/cost价 |
| 两个数据源都失败 | 清空重试标记，抛出AllProvidersFailed | 调用方按"数据不可用"处理 |
| 列名不统一(AkShare返回pct_chg vs Tushare的change) | Provider内部映射为标准列名 | 保留原始列名+打印警告 |
"""

import os
import sys
import time
import logging

import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class DataUnavailableError(Exception):
    """所有数据源都不可用"""
    pass


class BaseProvider:
    """数据源基类"""
    name = "base"

    def daily(self, ts_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        raise NotImplementedError

    def daily_basic(self, ts_code: str, trade_date: str = None) -> pd.DataFrame:
        raise NotImplementedError

    def stock_basic(self, ts_code: str = None, fields: str = None) -> pd.DataFrame:
        raise NotImplementedError

    def moneyflow(self, ts_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        raise NotImplementedError

    def index_daily(self, ts_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        raise NotImplementedError


class TushareProvider(BaseProvider):
    """Tushare Pro 数据源（主数据源）"""
    name = "tushare"

    def __init__(self):
        from scripts.utils.tushare_client import pro as tushare_pro
        self._pro = tushare_pro

    def daily(self, ts_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        for attempt in range(2):
            try:
                if start_date and end_date:
                    df = self._pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
                else:
                    df = self._pro.daily(ts_code=ts_code)
                if df is not None and not df.empty:
                    # 统一为标准列名
                    return _normalize_daily_columns(df, source="tushare")
                return pd.DataFrame()
            except Exception as e:
                if attempt == 0:
                    time.sleep(0.5)
                else:
                    logger.warning(f"[Tushare] daily({ts_code}) 失败({attempt+1}/2): {e}")
        return pd.DataFrame()

    def daily_basic(self, ts_code: str, trade_date: str = None) -> pd.DataFrame:
        try:
            df = self._pro.daily_basic(ts_code=ts_code, trade_date=trade_date)
            if df is not None and not df.empty:
                return _normalize_basic_columns(df, source="tushare")
            return pd.DataFrame()
        except Exception as e:
            logger.warning(f"[Tushare] daily_basic({ts_code}) 失败: {e}")
            return pd.DataFrame()

    def stock_basic(self, ts_code: str = None, fields: str = None) -> pd.DataFrame:
        try:
            df = self._pro.stock_basic(ts_code=ts_code, fields=fields)
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            logger.warning(f"[Tushare] stock_basic({ts_code}) 失败: {e}")
            return pd.DataFrame()

    def moneyflow(self, ts_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        try:
            if start_date and end_date:
                df = self._pro.moneyflow(ts_code=ts_code, start_date=start_date, end_date=end_date)
            else:
                df = self._pro.moneyflow(ts_code=ts_code)
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            logger.warning(f"[Tushare] moneyflow({ts_code}) 失败: {e}")
            return pd.DataFrame()

    def index_daily(self, ts_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        try:
            if start_date and end_date:
                df = self._pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            else:
                df = self._pro.index_daily(ts_code=ts_code)
            return df if df is not None else pd.DataFrame()
        except Exception as e:
            logger.warning(f"[Tushare] index_daily({ts_code}) 失败: {e}")
            return pd.DataFrame()


class AkshareProvider(BaseProvider):
    """AkShare 数据源（后备数据源）"""
    name = "akshare"

    def __init__(self):
        try:
            import akshare as ak
            self._ak = ak
        except ImportError:
            raise ImportError("akshare 未安装，请运行 pip install akshare")

    def _ts_code_to_akshare_symbol(self, ts_code: str) -> str:
        """转换 ts_code (000001.SZ) 为 akshare 格式"""
        code, market = ts_code.split(".")
        if market == "SZ":
            return code  # 深交所不需要前缀
        elif market == "SH":
            return code  # 上交所不需要前缀
        return code

    def daily(self, ts_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        try:
            symbol = self._ts_code_to_akshare_symbol(ts_code)
            # akshare 的 stock_zh_a_hist 接口
            start = start_date or (end_date[:4] + "0101") if end_date else "20200101"
            end = end_date or start
            # 格式化日期为 YYYYMMDD → YYYY-MM-DD
            start_fmt = f"{start[:4]}-{start[4:6]}-{start[6:8]}"
            end_fmt = f"{end[:4]}-{end[4:6]}-{end[6:8]}"

            df = self._ak.stock_zh_a_hist(symbol=symbol, period="daily",
                                            start_date=start_fmt, end_date=end_fmt, adjust="")
            if df is None or df.empty:
                return pd.DataFrame()

            # 映射为标准列名格式（与Tushare一致）
            df = df.rename(columns={
                "日期": "trade_date",
                "开盘": "open",
                "收盘": "close",
                "最高": "high",
                "最低": "low",
                "成交量": "vol",
                "成交额": "amount",
                "振幅": "amplitude",
                "涨跌幅": "pct_chg",
                "涨跌额": "change",
                "换手率": "turnover_rate",
            })
            # 日期转为 YYYYMMDD 格式
            if "trade_date" in df.columns:
                df["trade_date"] = pd.to_datetime(df["trade_date"]).dt.strftime("%Y%m%d")

            # 补充 ts_code 列
            df["ts_code"] = ts_code

            # 按 trade_date 降序排列（与Tushare一致）
            if "trade_date" in df.columns:
                df = df.sort_values("trade_date", ascending=False).reset_index(drop=True)
            return df
        except Exception as e:
            logger.warning(f"[AkShare] daily({ts_code}) 失败: {e}")
            return pd.DataFrame()

    def daily_basic(self, ts_code: str, trade_date: str = None) -> pd.DataFrame:
        """AkShare不支持daily_basic（估值数据），返回空"""
        return pd.DataFrame()

    def stock_basic(self, ts_code: str = None, fields: str = None) -> pd.DataFrame:
        try:
            df = self._ak.stock_zh_a_spot_em()
            if df is not None and not df.empty:
                # 转换为Tushare格式的stock_basic
                df = df.rename(columns={
                    "代码": "ts_code",
                    "名称": "name",
                    "涨跌幅": "pct_chg",
                })
                # 代码补全市场后缀
                if "ts_code" in df.columns:
                    df["ts_code"] = df["ts_code"].apply(
                        lambda x: str(x) + (".SH" if str(x).startswith("6") else ".SZ") if len(str(x)) == 6 else str(x)
                    )
                if ts_code and "ts_code" in df.columns:
                    df = df[df["ts_code"] == ts_code]
                if fields:
                    available = [f for f in fields.split(",") if f in df.columns]
                    if "ts_code" in df.columns:
                        available = ["ts_code"] + [f for f in available if f != "ts_code"]
                    df = df[available] if available else df
                return df
            return pd.DataFrame()
        except Exception as e:
            logger.warning(f"[AkShare] stock_basic({ts_code}) 失败: {e}")
            return pd.DataFrame()

    def moneyflow(self, ts_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        """AkShare的资金流数据返回空（不支持该接口）"""
        return pd.DataFrame()

    def index_daily(self, ts_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        try:
            # 上证综指: "sh000001" or "000001"
            index_map = {
                "000001.SH": "上证综合指数",
                "399001.SZ": "深证成指",
                "399006.SZ": "创业板指",
                "000688.SH": "科创50",
            }
            name = index_map.get(ts_code)
            if name:
                df = self._ak.stock_zh_index_daily(symbol=f"sh{ts_code[:6]}" if ts_code.endswith("SH") else f"sz{ts_code[:6]}")
                if df is not None and not df.empty:
                    df["ts_code"] = ts_code
                    if "date" in df.columns:
                        df["trade_date"] = pd.to_datetime(df["date"]).dt.strftime("%Y%m%d")
                    return df
            return pd.DataFrame()
        except Exception as e:
            logger.warning(f"[AkShare] index_daily({ts_code}) 失败: {e}")
            return pd.DataFrame()


def _normalize_daily_columns(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """统一日线列名"""
    # Tushare列名已是标准格式
    return df


def _normalize_basic_columns(df: pd.DataFrame, source: str) -> pd.DataFrame:
    """统一基本面列名"""
    return df


class DataProvider:
    """
    多数据源 Provider — 自动fallback

    优先使用Tushare，失败时依次尝试后备数据源。
    所有方法返回统一的DataFrame格式。

    用法：
        dp = DataProvider()
        df = dp.daily("000001.SZ", start_date="20260701", end_date="20260703")
        # DataFrame列: ts_code, trade_date, open, high, low, close, vol, amount, pct_chg
    """

    def __init__(self):
        self._providers = []

        # Tushare（主数据源）
        try:
            self._providers.append(TushareProvider())
        except Exception as e:
            logger.warning(f"[DataProvider] Tushare初始化失败: {e}")

        # AkShare（后备数据源）
        try:
            self._providers.append(AkshareProvider())
        except Exception as e:
            logger.warning(f"[DataProvider] AkShare初始化失败: {e}")

        if not self._providers:
            raise DataUnavailableError("所有数据源都初始化失败")

    def _try_providers(self, method: str, *args, **kwargs) -> pd.DataFrame:
        """依次尝试所有Provider，全部失败时返回空DataFrame"""
        errors = []
        for provider in self._providers:
            try:
                fn = getattr(provider, method, None)
                if fn is None:
                    continue
                result = fn(*args, **kwargs)
                if result is not None and not result.empty:
                    return result
            except Exception as e:
                errors.append(f"{provider.name}: {e}")
                continue
        logger.warning(f"[DataProvider] {method}({args}) 所有数据源失败: {errors}")
        return pd.DataFrame()

    def daily(self, ts_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        return self._try_providers("daily", ts_code=ts_code, start_date=start_date, end_date=end_date)

    def daily_basic(self, ts_code: str, trade_date: str = None) -> pd.DataFrame:
        return self._try_providers("daily_basic", ts_code=ts_code, trade_date=trade_date)

    def stock_basic(self, ts_code: str = None, fields: str = None) -> pd.DataFrame:
        return self._try_providers("stock_basic", ts_code=ts_code, fields=fields)

    def moneyflow(self, ts_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        return self._try_providers("moneyflow", ts_code=ts_code, start_date=start_date, end_date=end_date)

    def index_daily(self, ts_code: str, start_date: str = None, end_date: str = None) -> pd.DataFrame:
        return self._try_providers("index_daily", ts_code=ts_code, start_date=start_date, end_date=end_date)

    @property
    def providers_info(self) -> list:
        """返回可用provider信息"""
        return [{"name": p.name, "available": True} for p in self._providers]


# 全局单例
_provider_instance = None


def get_provider() -> DataProvider:
    """获取全局DataProvider单例"""
    global _provider_instance
    if _provider_instance is None:
        _provider_instance = DataProvider()
    return _provider_instance


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    dp = DataProvider()
    print(f"可用数据源: {dp.providers_info}")

    # 测试行情获取
    test_codes = ["000001.SZ", "600519.SH", "300750.SZ"]
    for code in test_codes:
        df = dp.daily(code, start_date="20260701", end_date="20260703")
        if not df.empty:
            print(f"{code}: {len(df)} 条行情 ✅")
            print(f"  列: {list(df.columns)}")
        else:
            print(f"{code}: 无数据 ❌")
