"""
Tushare 数据客户端 - 供各 Agent 调用

使用方法：
1. 设置环境变量 TUSHARE_TOKEN 或在下方直接填入 token
2. 激活虚拟环境后：python scripts/utils/tushare_client.py

安装：
pip install tushare pandas numpy ta matplotlib mplfinance
"""

import os
import pandas as pd
import tushare as ts

# ===== 配置 =====
# 优先从环境变量读取 token，否则在这里填入
TOKEN = os.getenv("TUSHARE_TOKEN")
if not TOKEN:
    raise ValueError("请设置环境变量 TUSHARE_TOKEN")
# ================

pro = ts.pro_api(TOKEN)


def get_daily(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取日线行情"""
    return pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)


def get_index_daily(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取大盘指数日线"""
    return pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)


def get_moneyflow(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取个股资金流向"""
    return pro.moneyflow(ts_code=ts_code, start_date=start_date, end_date=end_date)


def get_moneyflow_hsgt(start_date: str, end_date: str) -> pd.DataFrame:
    """获取沪深港通资金流向（北向资金）"""
    return pro.moneyflow_hsgt(start_date=start_date, end_date=end_date)


def get_top10_holders(ts_code: str, period: str = "") -> pd.DataFrame:
    """获取十大股东"""
    return pro.top10_holders(ts_code=ts_code, period=period)


def get_daily_basic(ts_code: str, trade_date: str) -> pd.DataFrame:
    """获取每日指标（PE、PB、换手率等）"""
    return pro.daily_basic(ts_code=ts_code, trade_date=trade_date)


def get_limit_list(trade_date: str) -> pd.DataFrame:
    """获取龙虎榜数据"""
    return pro.limit_list(trade_date=trade_date)


def get_ths_index(daily: bool = True) -> pd.DataFrame:
    """获取同花顺概念板块"""
    if daily:
        return pro.ths_daily(trade_date=today_str())
    return pro.ths_index()


def today_str() -> str:
    """返回今天日期字符串 YYYYMMDD"""
    import datetime
    return datetime.date.today().strftime("%Y%m%d")


# ===== 演示 =====
if __name__ == "__main__":
    print("Tushare 客户端已加载")
    print(f"Token 状态: {'已配置' if TOKEN != '你的Token在这里' else '未配置'}")
    print()
    print("可用函数:")
    funcs = [n for n in dir() if n.startswith("get_")]
    for f in sorted(funcs):
        print(f"  {f}()")
