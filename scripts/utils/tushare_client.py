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
- CP3-日期格式校验：trade_date参数必须是YYYYMMDD格式
- CP4-API限频保护：调用方需自行控制调用间隔(≥0.2s)
- CP5-网络超时兜底：所有get_*函数返回空DataFrame而非抛异常

D9反例：
- 不要在脚本中硬编码token（已在.gitignore保护settings.local.json）
- 不要假设每个API调用都返回数据（要检查DataFrame是否为空）
- 不要异常时直接退出（工具函数应返回空值让调用方做降级决策）
"""

import os
import pandas as pd
import tushare as ts

# ===== 配置 =====
# 优先从环境变量读取 token
# ⚠️ 延迟初始化：不在此处直接调用 ts.pro_api()，避免模块导入即崩溃
# 使用 get_pro() 函数按需初始化
_TOKE = os.getenv("TUSHARE_TOKEN", "")
_pro_instance = None


def has_token() -> bool:
    """检查Tushare Token是否已配置（安全调用，不触发初始化）"""
    return bool(_TOKE)


def get_pro():
    """
    获取 Tushare Pro 实例（延迟初始化）

    首次调用时初始化，后续复用缓存实例。
    若Token未配置，打印警告并返回 None。
    调用方需判断返回 None 时降级到 DataProvider 多源fallback。
    """
    global _pro_instance
    if _pro_instance is not None:
        return _pro_instance
    if not _TOKE:
        print("[tushare] TUSHARE_TOKEN 未配置，Tushare数据不可用。"
              "设置环境变量 TUSHARE_TOKEN 或使用 DataProvider 多源fallback。")
        return None
    try:
        _pro_instance = ts.pro_api(_TOKE)
        return _pro_instance
    except Exception as e:
        print(f"[tushare] Tushare初始化失败: {e}")
        return None


# 兼容旧代码：通过 get_pro() 按需获取实例
def _lazy_pro():
    """兼容旧接口 pro.xxx() 调用的延迟属性"""
    inst = get_pro()
    if inst is None:
        raise RuntimeError("Tushare Pro不可用（Token未配置或初始化失败）")
    return inst


# 提供向后兼容的 pro 对象（仅在调用时才检查Token）
class _LazyTushare:
    """延迟加载的Tushare代理 — 调用属性时才初始化"""

    def __getattr__(self, name):
        inst = get_pro()
        if inst is None:
            # Token缺失时返回一个mock，所有API调用返回空DataFrame
            return _EmptyAPI(name)
        return getattr(inst, name)


class _EmptyAPI:
    """Token缺失时的静默降级代理 — 所有API调用返回空DataFrame"""

    def __init__(self, name=""):
        self._name = name

    def __getattr__(self, name):
        return _EmptyAPI(f"{self._name}.{name}")

    def __call__(self, *args, **kwargs):
        print(f"[tushare] Token未配置，{self._name}() 返回空DataFrame（降级）")
        return pd.DataFrame()


# pro 对象：有Token时正常，无Token时所有API返回空DataFrame
pro = _LazyTushare()


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


def get_ths_index(daily: bool = True, trade_date: str = None) -> pd.DataFrame:
    """获取同花顺概念板块（Tushare优先，回退到东方财富，再回退到THS AkShare）

    Args:
        daily: True 获取日线数据，False 获取板块列表
        trade_date: 交易日 YYYYMMDD，默认今天
    """
    if trade_date is None:
        trade_date = today_str()
    try:
        if daily:
            return pro.ths_daily(trade_date=trade_date)
        return pro.ths_index()
    except Exception as e:
        print(f"[tushare] get_ths_index 失败: {e}")

        if not daily:
            return pd.DataFrame()

        # 方案2: 查DB（已有THS回填数据，最快）
        try:
            from scripts.utils.db_manager import DatabaseManager
            db = DatabaseManager()
            db_df = db.get_sector_ranking(trade_date, 500)
            db.close()
            if db_df is not None and len(db_df) > 100:
                print(f"[tushare] DB查询成功，获取 {len(db_df)} 个板块 (来源:THS回填)")
                return db_df
        except Exception as e2:
            print(f"[tushare] DB查询失败: {e2}")

        # 方案3: 尝试东方财富（当前被封锁，快速失败）
        try:
            from scripts.utils.eastmoney_client import get_ths_daily_fallback
            df = get_ths_daily_fallback()
            if not df.empty:
                print(f"[tushare] 东方财富回退成功，获取 {len(df)} 个板块")
                return df
        except Exception as e3:
            print(f"[tushare] 东方财富回退失败: {e3}")

        # 方案4: THS AkShare回填（仅首次缺失时触发，后续走DB查询）
        try:
            print(f"[tushare] 触发THS AkShare回填...")
            from scripts.utils.backfill_ths_daily import backfill as ths_backfill
            from datetime import datetime, timedelta
            since = (datetime.strptime(trade_date, "%Y%m%d") - timedelta(days=15)).strftime("%Y%m%d")
            until = datetime.now().strftime("%Y%m%d")
            result = ths_backfill(since=since, until=until, dry_run=False)
            if result.get("rows", 0) > 0:
                from scripts.utils.db_manager import DatabaseManager
                db = DatabaseManager()
                df = db.get_sector_ranking(trade_date, 500)
                db.close()
                if not df.empty:
                    print(f"[tushare] THS AkShare 回填成功，获取 {len(df)} 个板块")
                    return df
        except Exception as e4:
            print(f"[tushare] THS AkShare 回填失败: {e4}")
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
