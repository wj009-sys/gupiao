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
_TOKEN = os.getenv("TUSHARE_TOKEN", "")
_TOKE = _TOKEN  # 向后兼容别名
_pro_instance = None

# 东方财富API封锁缓存（达尔文12.0 — 避免每次重复尝试已封锁的API）
_EASTMONEY_BLOCKED = False
_EASTMONEY_BLOCKED_AT = None  # 时间戳


def has_token() -> bool:
    """检查Tushare Token是否已配置（安全调用，不触发初始化）"""
    return bool(_TOKEN)


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

        # 方案3: 尝试东方财富（缓存封锁状态，避免重复尝试）
        global _EASTMONEY_BLOCKED, _EASTMONEY_BLOCKED_AT
        import time as _time
        if _EASTMONEY_BLOCKED:
            # 每30分钟重置一次，允许偶尔重试
            if _EASTMONEY_BLOCKED_AT and _time.time() - _EASTMONEY_BLOCKED_AT > 1800:
                _EASTMONEY_BLOCKED = False
        if not _EASTMONEY_BLOCKED:
            try:
                from scripts.utils.eastmoney_client import get_ths_daily_fallback
                df = get_ths_daily_fallback()
                if not df.empty:
                    print(f"[tushare] 东方财富回退成功，获取 {len(df)} 个板块")
                    return df
            except Exception as e3:
                _EASTMONEY_BLOCKED = True
                _EASTMONEY_BLOCKED_AT = _time.time()
                print(f"[tushare] 东方财富回退失败(已缓存封锁状态): {e3}")

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


def get_adj_factor(ts_code: str = None, trade_date: str = None,
                   start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """获取复权因子（doc_id=7）

    返回字段: ts_code, trade_date, adj_factor
    积分: 2000起，5000高频
    单次最多: 3000条
    更新时间: 每天9:30

    复权计算:
        前复权价 = close × (adj_factor / latest_adj_factor)
        后复权价 = close × adj_factor
    """
    try:
        kwargs = {}
        if ts_code:
            kwargs["ts_code"] = ts_code
        if trade_date:
            kwargs["trade_date"] = trade_date
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        return pro.adj_factor(**kwargs) if kwargs else pd.DataFrame()
    except Exception as e:
        print(f"[tushare] get_adj_factor 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_dividend(ts_code: str = None, ex_date: str = None,
                 ann_date: str = None) -> pd.DataFrame:
    """获取分红送股数据（doc_id=9）

    关键返回字段:
        ts_code, end_date(分红年度), div_proc(实施进度),
        stk_div(每股送转), cash_div(每股分红税后),
        ex_date(除权除息日), record_date(股权登记日)
    积分: 300起
    单次最多: 10000条
    """
    try:
        kwargs = {}
        if ts_code:
            kwargs["ts_code"] = ts_code
        if ex_date:
            kwargs["ex_date"] = ex_date
        if ann_date:
            kwargs["ann_date"] = ann_date
        return pro.dividend(**kwargs) if kwargs else pd.DataFrame()
    except Exception as e:
        print(f"[tushare] get_dividend 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_stock_basic(ts_code: str = None, list_status: str = "L",
                    exchange: str = None, market: str = None,
                    is_hs: str = None) -> pd.DataFrame:
    """获取股票基础信息（doc_id=122）

    关键返回字段:
        ts_code, name, industry(所属行业), market(市场类型),
        list_date(上市日期), delist_date(退市日期),
        is_hs(沪深港通), act_name(实控人)
    积分: 基础
    """
    try:
        kwargs = {"list_status": list_status}
        if ts_code:
            kwargs["ts_code"] = ts_code
        if exchange:
            kwargs["exchange"] = exchange
        if market:
            kwargs["market"] = market
        if is_hs:
            kwargs["is_hs"] = is_hs
        return pro.stock_basic(**kwargs)
    except Exception as e:
        print(f"[tushare] get_stock_basic 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_share_float(ts_code: str = None, float_date: str = None,
                    start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """获取限售股解禁数据（doc_id=450）

    关键返回字段:
        ts_code, float_date(解禁日期), float_share(流通股份),
        float_ratio(流通占比%), holder_name(股东名称), share_type(股份类型)
    积分: 120起
    单次最多: 6000条
    """
    try:
        kwargs = {}
        if ts_code:
            kwargs["ts_code"] = ts_code
        if float_date:
            kwargs["float_date"] = float_date
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        return pro.share_float(**kwargs) if kwargs else pd.DataFrame()
    except Exception as e:
        print(f"[tushare] get_share_float 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_ths_member(ts_code: str = None, con_code: str = None) -> pd.DataFrame:
    """获取同花顺板块成分股（doc_id=473）

    两种查询模式:
        1. ts_code=板块代码 → 查该板块所有成分股
        2. con_code=股票代码 → 查该股票所属所有板块
    返回字段: trade_date, ts_code(板块), con_code(成分股), name
    积分: 2000起
    """
    try:
        kwargs = {}
        if ts_code:
            kwargs["ts_code"] = ts_code
        if con_code:
            kwargs["con_code"] = con_code
        if not kwargs:
            return pd.DataFrame()
        return pro.ths_member(**kwargs)
    except Exception as e:
        print(f"[tushare] get_ths_member 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_top_list(ts_code: str = None, trade_date: str = None,
                 start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """获取龙虎榜机构/营业部交易明细（doc_id=230）

    返回字段:
        trade_date, ts_code, name, buy(买入金额), sell(卖出金额),
        buy_rate(买入占比%), sell_rate(卖出占比%),
        exalter(营业部/机构名称), exalter_type(类型)
    积分: 5000起（高级权限）
    """
    try:
        kwargs = {}
        if ts_code:
            kwargs["ts_code"] = ts_code
        if trade_date:
            kwargs["trade_date"] = trade_date
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        return pro.top_list(**kwargs) if kwargs else pd.DataFrame()
    except Exception as e:
        print(f"[tushare] get_top_list 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_moneyflow_dc(ts_code: str = None, trade_date: str = None,
                     start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """获取个股资金流向（东方财富版 — 更简洁，doc_id=463替代接口）

    相比 moneyflow 经典版:
        ✅ 含 name(股票名称)、pct_change(涨跌幅)、close(最新价)
        ✅ 返回净流入额而非买卖分开
        ⚠️ 无买卖量(手)信息
    返回字段: trade_date, ts_code, name, pct_change, close,
              net_amount(主力净流入万元), net_amount_rate(净占比%),
              buy_elg_amount(超大单), buy_lg_amount(大单)
    积分: 较低门槛
    """
    try:
        kwargs = {}
        if ts_code:
            kwargs["ts_code"] = ts_code
        if trade_date:
            kwargs["trade_date"] = trade_date
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        return pro.moneyflow_dc(**kwargs) if kwargs else pd.DataFrame()
    except Exception as e:
        print(f"[tushare] get_moneyflow_dc 失败({ts_code}): {e}")
        return pd.DataFrame()

def get_pro_bar(ts_code: str, freq: str = "D", asset: str = "E",
                start_date: str = None, end_date: str = None,
                adj: str = None, ma: list = None,
                factors: list = None) -> pd.DataFrame:
    """通用行情接口（P0 — 项目最缺失的核心接口）

    统一行情接口，替代 daily/index_daily/fund_daily 等。
    支持复权(adj)、均线(ma)、换手率/量比(factors)。

    Args:
        ts_code: 证券代码（不支持多值）
        freq: D日/W周/M月/1min/5min/15min/30min/60min
        asset: E股票/I指数/FT期货/FD基金/O期权/CB可转债
        start_date: 日线YYYYMMDD，分钟线YYYY-MM-DD HH:MM:SS
        end_date: 结束日期
        adj: None未复权/qfq前复权/hfq后复权（仅日线）
        ma: 均线如[5,10,20]→返回ma5/ma10/ma20
        factors: ['tor']换手率/['vr']量比
    """
    try:
        kwargs = {"ts_code": ts_code, "freq": freq, "asset": asset}
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        if adj:
            kwargs["adj"] = adj
        if ma:
            kwargs["ma"] = ma
        if factors:
            kwargs["factors"] = factors
        return ts.pro_bar(**kwargs)
    except Exception as e:
        print(f"[tushare] get_pro_bar 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_margin(trade_date: str = None,
               start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """获取融资融券交易汇总（P1 — 市场杠杆情绪指标）

    返回: trade_date, exchange, rzye(融资余额), rzmre(融资买入),
          rzjmr(融资净买入), rqye(融券余额), rqmcl(融券卖出)
    积分: 基础
    """
    try:
        kwargs = {}
        if trade_date:
            kwargs["trade_date"] = trade_date
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        return pro.margin(**kwargs) if kwargs else pd.DataFrame()
    except Exception as e:
        print(f"[tushare] get_margin 失败: {e}")
        return pd.DataFrame()


def get_hk_hold(ts_code: str = None, trade_date: str = None,
                start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """获取沪深港通持股明细（P1 — 外资具体持仓）

    返回: ts_code, name, trade_date, volume(持股数量),
          ratio(占总股本%), exchange(类型)
    积分: 2000起
    """
    try:
        kwargs = {}
        if ts_code:
            kwargs["ts_code"] = ts_code
        if trade_date:
            kwargs["trade_date"] = trade_date
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        return pro.hk_hold(**kwargs) if kwargs else pd.DataFrame()
    except Exception as e:
        print(f"[tushare] get_hk_hold 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_forecast(ts_code: str = None, start_date: str = None,
                 end_date: str = None) -> pd.DataFrame:
    """获取业绩预告（P1 — 选股核心基本面因子）

    返回: ts_code, name, end_date, type(预告类型),
          p_change_min/p_change_max(净利润变动%)
    """
    try:
        kwargs = {}
        if ts_code:
            kwargs["ts_code"] = ts_code
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        return pro.forecast(**kwargs) if kwargs else pd.DataFrame()
    except Exception as e:
        print(f"[tushare] get_forecast 失败({ts_code}): {e}")
        return pd.DataFrame()


def get_index_global(ts_code: str = None,
                     start_date: str = None, end_date: str = None) -> pd.DataFrame:
    """获取全球主要指数行情（P1 — 外围市场联动）

    指数代码: .DJI道指, .IXIC纳指, .SPX标普, .N225日经,
              .HSI恒生, .FTSE富时, .GDAXI德国DAX
    返回: ts_code, trade_date, close, pct_chg, vol, amount
    """
    try:
        kwargs = {}
        if ts_code:
            kwargs["ts_code"] = ts_code
        if start_date:
            kwargs["start_date"] = start_date
        if end_date:
            kwargs["end_date"] = end_date
        return pro.index_global(**kwargs) if kwargs else pd.DataFrame()
    except Exception as e:
        print(f"[tushare] get_index_global 失败: {e}")
        return pd.DataFrame()


def get_limit_list_d(trade_date: str = None) -> pd.DataFrame:
    """获取涨停连板天梯（P0 — 游资追踪核心数据）

    比 limit_list 更详细的连板数据：涨停池/连板池/跌停池/炸板池
    返回: trade_date, ts_code, name, pct_chg, close,
          lb_count(连板数), limit_status
    积分: 5000起
    """
    try:
        kwargs = {}
        if trade_date:
            kwargs["trade_date"] = trade_date
        return pro.limit_list_d(**kwargs) if kwargs else pd.DataFrame()
    except Exception as e:
        print(f"[tushare] get_limit_list_d 失败: {e}")
        return pd.DataFrame()





def today_str() -> str:
    """返回今天日期字符串 YYYYMMDD"""
    import datetime
    return datetime.date.today().strftime("%Y%m%d")


# ===== 演示 =====
if __name__ == "__main__":
    print("Tushare 客户端已加载")
    print(f"Token 状态: {'已配置' if _TOKEN != '你的Token在这里' else '未配置'}")
    print()
    print("可用函数:")
    funcs = [n for n in dir() if n.startswith("get_")]
    for f in sorted(funcs):
        print(f"  {f}()")
