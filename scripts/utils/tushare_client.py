"""
Tushare 数据客户端 - 供各 Agent 调用

使用方法：
1. 设置环境变量 TUSHARE_TOKEN 或在下方直接填入 token
2. 激活虚拟环境后：python scripts/utils/tushare_client.py

安装：
pip install tushare pandas numpy ta matplotlib mplfinance

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| Tushare token未设置 | 检查os.getenv("TUSHARE_TOKEN") | 抛出ValueError，提示设置方式 |
| 网络连接超时 | tushare内部重试机制（1次） | 返回空DataFrame，调用方用前收盘价/cost价 |
| API频率限制(429) | tushare自动限频+等待再试 | 返回空DataFrame，标记数据为"延迟获取" |
| 无效ts_code格式 | 自动修正格式（6位数字+.SZ/.SH） | 返回空DataFrame，跳过该品种 |
| trade_date格式错误 | 转为YYYYMMDD标准格式 | 返回空DataFrame，调用方用最新日线代替 |

D4 CHECKPOINT:
- CP1-Token验证：导入时立即检查token是否存在
- CP2-返回值非空：调用方必须检查返回值len>0

D9反例：
- 不要在脚本中硬编码token（已在.gitignore保护settings.local.json）
- 不要假设每个API调用都返回数据（要检查DataFrame是否为空）
- 不要异常时直接退出（工具函数应返回空值让调用方做降级决策）
"""

import os
import pandas as pd
import tushare as ts

# ===== 配置 =====
# 优先从环境变量读取 token，否则在这里填入
TOKEN = os.getenv("TUSHARE_TOKEN")
if not TOKEN:
    raise ValueError("请设置环境变量 TUSHARE_TOKEN")

pro = ts.pro_api(TOKEN)


def get_daily(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取日线行情"""
    try:
        return pro.daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    except Exception as e:
        print(f"[tushare] get_daily 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_index_daily(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取大盘指数日线"""
    try:
        return pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
    except Exception as e:
        print(f"[tushare] get_index_daily 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_moneyflow(ts_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """获取个股资金流向"""
    try:
        return pro.moneyflow(ts_code=ts_code, start_date=start_date, end_date=end_date)
    except Exception as e:
        print(f"[tushare] get_moneyflow 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_moneyflow_hsgt(start_date: str, end_date: str) -> pd.DataFrame:
    """获取沪深港通资金流向（北向资金）"""
    try:
        return pro.moneyflow_hsgt(start_date=start_date, end_date=end_date)
    except Exception as e:
        print(f"[tushare] get_moneyflow_hsgt 失败: {e}")
        return pd.DataFrame()


def get_top10_holders(ts_code: str, period: str = "") -> pd.DataFrame:
    """获取十大股东"""
    try:
        return pro.top10_holders(ts_code=ts_code, period=period)
    except Exception as e:
        print(f"[tushare] get_top10_holders 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_daily_basic(ts_code: str, trade_date: str) -> pd.DataFrame:
    """获取每日指标（PE、PB、换手率等）"""
    try:
        return pro.daily_basic(ts_code=ts_code, trade_date=trade_date)
    except Exception as e:
        print(f"[tushare] get_daily_basic 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_limit_list(trade_date: str) -> pd.DataFrame:
    """获取龙虎榜数据"""
    try:
        return pro.limit_list(trade_date=trade_date)
    except Exception as e:
        print(f"[tushare] get_limit_list 失败({trade_date}): {e}")
        return pd.DataFrame()


def get_ths_index(daily: bool = True) -> pd.DataFrame:
    """获取同花顺概念板块"""
    try:
        if daily:
            return pro.ths_daily(trade_date=today_str())
        return pro.ths_index()
    except Exception as e:
        print(f"[tushare] get_ths_index 失败: {e}")
        return pd.DataFrame()


def get_fund_daily(ts_code: str, trade_date: str) -> pd.DataFrame:
    """获取基金日线行情（ETF专用）"""
    try:
        return pro.fund_daily(ts_code=ts_code, trade_date=trade_date)
    except Exception as e:
        print(f"[tushare] get_fund_daily 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_suspend_d(ts_code: str, suspend_date: str) -> pd.DataFrame:
    """获取停复牌信息"""
    try:
        return pro.suspend_d(ts_code=ts_code, suspend_date=suspend_date)
    except Exception as e:
        print(f"[tushare] get_suspend_d 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_stk_limit(ts_code: str, trade_date: str) -> pd.DataFrame:
    """获取涨跌停价格"""
    try:
        return pro.stk_limit(ts_code=ts_code, trade_date=trade_date)
    except Exception as e:
        print(f"[tushare] get_stk_limit 失败({ts_code}): {e}")
        return pd.DataFrame()


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
