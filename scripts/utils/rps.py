"""
RPS (Relative Price Strength) 相对价格强度指标

基于威廉·欧奈尔(William O'Neil) CAN SLIM 投资体系的核心指标

定义：
  RPS 衡量个股相对于全市场其他股票的价格表现。
  RPS = 90 意味着该股票在过去N日涨幅超过市场中90%的股票。

核心周期：
  - RPS_20:  20日（约1个月）→ 短期动量
  - RPS_60:  60日（约3个月）→ 中期动量
  - RPS_120: 120日（约6个月）→ 中长期趋势
  - RPS_250: 250日（约1年）→ 长期趋势

欧奈尔标准：
  - RPS ≥ 90: 强势股（牛股大涨前RPS通常≥87）
  - RPS 80-90: 关注
  - RPS 50-80: 一般
  - RPS < 30: 弱势，回避

计算方法：
  1. 对每只股票计算 N 日涨幅（使用后复权价 = close × adj_factor）
  2. 按涨幅从高到低排序
  3. 计算百分位排名：RPS = (1 - 排名/总有效股票数) × 100

数据源：
  - daily_price 表：close（日收盘价）
  - adj_factor 表：复权因子（后复权因子，用于计算真实收益率）
  - stock_basic 表：股票名称、上市状态

D3异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 无行情数据（非交易日） | 检查数据库是否存在该日期数据 | 返回空DataFrame，标注"数据不可用" |
| 部分股票无N日前数据（次新股/长期停牌） | 用最早可用价格代替,标注数据天数 | RPS标记为NaN，排名时跳过 |
| 复权因子缺失（次新股） | 使用原始close代替（close × 1.0） | NaN不计入排名 |
| 总有效股票数<50 | 标注"样本不足" | 返回空DataFrame |
| 除零错误(b.close=0) | COALESCE + CASE WHEN防御 | 该条数据跳过 |

D4 CHECKPOINT:
  - CP1-日期检查：trade_date在daily_price表中必须有数据
  - CP2-次新股过滤：上市不足N日的股票ret_n应为NaN（数据不足）
  - CP3-排名验证：RPS值必须在0-100范围内，否则标记异常
  - CP4-结果完整性：AGENT调用时检查返回DataFrame是否为空

D9反例：
  - 不要用后复权和不复权混用比（复权方式必须一致，否则涨幅失真）
  - 不要对次新股（上市<N日）标记高RPS——样本偏差，应标为NaN
  - 不要忽略停牌股——停牌期间涨幅为0，应计入排名（每日涨跌幅0%）
  - 不要把RPS当预测指标——RPS是滞后动量指标，用于发现趋势非预测拐点
  - 不要只看单周期RPS——不同周期结合分析（如RPS_120≥90+RPS_20≥80同时满足才推荐）
"""

import os
import sys
import pandas as pd
import numpy as np
from typing import List, Optional, Union, Dict, Tuple

# 确保项目根在 sys.path 中（支持直接 python rps.py 运行）
_project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# ============================================================
#  内部工具
# ============================================================

_db_instance = None

def _get_db():
    """懒加载数据库连接（带实例缓存）"""
    global _db_instance
    if _db_instance is None:
        from scripts.utils.db_manager import DatabaseManager
        _db_instance = DatabaseManager()
    return _db_instance


def _reuse_db(db):
    """复用已有db连接，减少重复创建"""
    global _db_instance
    if db is not None:
        _db_instance = db
    return _get_db()


def get_trading_date(trade_date: str, offset: int = 0) -> Optional[str]:
    """
    获取相对于某交易日偏移offset个交易日后的日期

    Args:
        trade_date: 基准日期 YYYYMMDD
        offset: 偏移量，负数=向前（历史），正数=向后（未来）

    Returns:
        交易日字符串 YYYYMMDD，或 None（找不到时）
    """
    db = _get_db()
    try:
        if offset < 0:
            # 向前N个交易日：从trade_date往前数|offset|个交易日
            sql = """
                SELECT DISTINCT trade_date FROM daily_price
                WHERE trade_date <= ? AND asset_type = 'I'
                ORDER BY trade_date DESC
                LIMIT 1 OFFSET ?
            """
            cur = db.conn.execute(sql, [trade_date, abs(offset)])
        else:
            # 向后N个交易日
            sql = """
                SELECT DISTINCT trade_date FROM daily_price
                WHERE trade_date >= ? AND asset_type = 'I'
                ORDER BY trade_date ASC
                LIMIT 1 OFFSET ?
            """
            cur = db.conn.execute(sql, [trade_date, offset])
        row = cur.fetchone()
        return str(row[0]) if row else None
    except Exception as e:
        print(f"[RPS] get_trading_date 失败: {e}")
        return None


# ============================================================
#  核心RPS计算
# ============================================================

def calc_n_day_returns(trade_date: str, n: int, min_trading_days: int = None) -> pd.DataFrame:
    """
    计算全市场所有股票N日涨幅

    使用后复权价格（close × adj_factor）确保分红送转不影响排名。
    对于复权因子缺失的股票，降级使用原始close（涨幅精度略降但不影响排名）。

    Args:
        trade_date: 计算日期 YYYYMMDD
        n: 回看周期（交易日数量）
        min_trading_days: 期间最少有效交易天数，None=不限制

    Returns:
        DataFrame: [ts_code, name, ret_n]
            ret_n 为百分比涨幅，无效值为 NaN
    """
    # D4-CP1: 查找N个交易日前的日期
    target_date = get_trading_date(trade_date, -n)
    if not target_date:
        print(f"[RPS] ⚠️ 无法找到 {trade_date} 向前 {n} 个交易日的日期")
        return pd.DataFrame()

    db = _get_db()

    # D4-CP2: 基础数据检查
    try:
        check = db.conn.execute(
            "SELECT COUNT(*) FROM daily_price WHERE trade_date=? AND asset_type='E'",
            [trade_date]
        ).fetchone()
        if not check or check[0] < 50:
            print(f"[RPS] ⚠️ {trade_date} 日行情数据不足({check[0] if check else 0}条)，无法计算RPS")
            return pd.DataFrame()
    except Exception as e:
        print(f"[RPS] 数据检查失败: {e}")
        return pd.DataFrame()

    # 核心SQL：用后复权价计算N日涨幅
    # 后复权价 = close × adj_factor -> 适用于收益率计算（latest因子抵消）
    # 如果复权因子缺失，降级使用原始close（在SQL中用COALESCE处理）
    sql = """
        SELECT
            a.ts_code,
            s.name,
            CASE
                WHEN b.close IS NULL OR b.close = 0 THEN NULL
                WHEN af1.adj_factor IS NULL OR af2.adj_factor IS NULL
                    THEN (a.close / b.close - 1.0) * 100.0
                ELSE (a.close * af1.adj_factor / (b.close * af2.adj_factor) - 1.0) * 100.0
            END AS ret_n
        FROM daily_price a
        JOIN stock_basic s ON a.ts_code = s.ts_code AND s.list_status = 'L'
        LEFT JOIN daily_price b
            ON a.ts_code = b.ts_code AND b.trade_date = ?
        LEFT JOIN adj_factor af1
            ON a.ts_code = af1.ts_code AND af1.trade_date = ?
        LEFT JOIN adj_factor af2
            ON a.ts_code = af2.ts_code AND af2.trade_date = ?
        WHERE a.trade_date = ?
          AND a.asset_type = 'E'
    """

    try:
        df = pd.read_sql_query(sql, db.conn, params=[target_date, trade_date, target_date, trade_date])
    except Exception as e:
        print(f"[RPS] 查询 {n} 日涨幅数据失败: {e}")
        return pd.DataFrame()

    if df.empty:
        return df

    # 转数值，过滤异常值
    df['ret_n'] = pd.to_numeric(df['ret_n'], errors='coerce')

    # 过滤极端值（正常A股单周期涨幅通常在-100%~+500%之间）
    df = df[df['ret_n'].between(-100, 500)].copy()

    # D3: 次新股过滤 — 上市不足N日的股票应标记为数据不足
    if min_trading_days is not None:
        # 可通过stock_basic.list_date判断
        try:
            list_dates = pd.read_sql_query(
                "SELECT ts_code, list_date FROM stock_basic WHERE list_status='L'",
                db.conn
            )
            if not list_dates.empty:
                cutoff = trade_date[:4] + '-' + trade_date[4:6] + '-' + trade_date[6:8]
                list_dates['list_date_dt'] = pd.to_datetime(list_dates['list_date'], errors='coerce')
                list_dates['list_date_dt'] = list_dates['list_date_dt'].fillna(pd.Timestamp(cutoff))
                cutoff_dt = pd.Timestamp(cutoff) - pd.Timedelta(days=n * 2)
                short_history = list_dates[list_dates['list_date_dt'] > cutoff_dt]['ts_code'].tolist()
                df.loc[df['ts_code'].isin(short_history), 'ret_n'] = np.nan
        except Exception:
            pass  # 次新股过滤失败不影响主流程

    return df


def calc_rps_from_returns(returns_df: pd.DataFrame, label: str = 'rps') -> pd.DataFrame:
    """
    根据N日涨幅计算RPS百分位排名

    RPS = (1 - 排名/总有效股票数) × 100

    Args:
        returns_df: calc_n_day_returns 返回的DataFrame（含 ts_code, name, ret_n）
        label: RPS列名前缀

    Returns:
        DataFrame: [ts_code, name, ret_n, rank, {label}]
    """
    # D4-CP2: 检查数据
    if returns_df is None or returns_df.empty:
        return pd.DataFrame()

    df = returns_df.copy()

    valid = df['ret_n'].notna()
    total_valid = valid.sum()

    # D4-CP2: 样本太少不排名
    if total_valid < 50:
        print(f"[RPS] ⚠️ 有效样本({total_valid})不足50，无法计算有意义的RPS")
        return pd.DataFrame()

    # 涨幅降序排名：涨幅最大 → rank=1
    df['rank'] = np.nan
    df.loc[valid, 'rank'] = df.loc[valid, 'ret_n'].rank(ascending=False, method='min')

    # RPS = (1 - rank / total) × 100
    df[label] = ((1 - df['rank'] / total_valid) * 100).round(1)

    # D4-CP3: RPS值范围检查
    rps_valid = df[label].notna()
    out_of_range = (df.loc[rps_valid, label] < 0) | (df.loc[rps_valid, label] > 100)
    if out_of_range.any():
        print(f"[RPS] ⚠️ {out_of_range.sum()} 条RPS值超出0-100范围，已修复")
        df.loc[rps_valid, label] = df.loc[rps_valid, label].clip(0, 100)

    return df[['ts_code', 'name', 'ret_n', 'rank', label]]


def calc_all_rps(trade_date: Optional[str] = None,
                 periods: Optional[List[int]] = None) -> pd.DataFrame:
    """
    计算全市场多周期RPS（主入口函数）

    支持4个标准周期，返回每个股票的RPS评分。

    Args:
        trade_date: 计算日期 YYYYMMDD，None=使用最新交易日
        periods: 周期列表，默认 [20, 60, 120, 250]

    Returns:
        DataFrame: [ts_code, name,
                    ret_20, ret_60, ret_120, ret_250,
                    rps_20, rps_60, rps_120, rps_250,
                    avg_rps]
    """
    if periods is None:
        periods = [20, 60, 120, 250]

    # 自动获取最新交易日
    if trade_date is None:
        db = _get_db()
        try:
            cur = db.conn.execute(
                "SELECT trade_date FROM daily_price WHERE asset_type='I' ORDER BY trade_date DESC LIMIT 1"
            )
            row = cur.fetchone()
            trade_date = str(row[0]) if row else None
        except Exception as e:
            print(f"[RPS] 获取最新交易日失败: {e}")
            return pd.DataFrame()

    if not trade_date:
        return pd.DataFrame()

    print(f"[RPS] 计算 {trade_date} 的全市场RPS...")

    result = None
    period_results = {}

    for n in periods:
        df_returns = calc_n_day_returns(trade_date, n)
        if df_returns.empty:
            print(f"[RPS] ⚠️ {n}日涨幅数据为空，跳过")
            continue

        rps_df = calc_rps_from_returns(df_returns, label=f'rps_{n}')
        if rps_df.empty:
            print(f"[RPS] ⚠️ {n}日RPS计算失败，跳过")
            continue

        # 提取需要的列
        merge_cols = ['ts_code', 'ret_n', f'rps_{n}']
        period_result = rps_df[merge_cols].copy()
        period_result.rename(columns={'ret_n': f'ret_{n}'}, inplace=True)
        period_results[n] = period_result

    if not period_results:
        return pd.DataFrame()

    # 合并各周期结果
    periods_completed = sorted(period_results.keys())
    result = period_results[periods_completed[0]][['ts_code']].copy()
    for n in periods_completed:
        result = result.merge(period_results[n], on='ts_code', how='outer')

    # 补充股票名称
    try:
        db = _get_db()
        names = pd.read_sql_query(
            "SELECT ts_code, name FROM stock_basic WHERE list_status='L'",
            db.conn
        )
        result = result.merge(names, on='ts_code', how='left')
    except Exception:
        pass

    # 计算平均RPS（仅对有效周期求平均）
    rps_cols = [c for c in result.columns if c.startswith('rps_')]
    if rps_cols:
        result['avg_rps'] = result[rps_cols].mean(axis=1).round(1)

    print(f"[RPS] ✅ 完成: {len(result)}只股票, {len(periods_completed)}个周期")
    return result


# ============================================================
#  便捷查询函数
# ============================================================

def get_stock_rps(ts_code: str, trade_date: Optional[str] = None,
                  periods: Optional[List[int]] = None) -> Dict[str, float]:
    """
    获取单只股票的RPS值

    Args:
        ts_code: 股票代码
        trade_date: 计算日期，None=最新交易日
        periods: 周期列表

    Returns:
        dict: {rps_20: xx, rps_60: xx, rps_120: xx, rps_250: xx, avg_rps: xx}
    """
    df = calc_all_rps(trade_date, periods)
    if df.empty:
        return {}

    row = df[df['ts_code'] == ts_code]
    if row.empty:
        return {}

    result = {}
    for col in df.columns:
        if col.startswith('rps_') or col == 'avg_rps' or col.startswith('ret_'):
            val = row.iloc[0][col]
            result[col] = round(float(val), 1) if pd.notna(val) else None

    return result


def get_top_rps(top_n: int = 50, period: int = 120,
                trade_date: Optional[str] = None,
                min_rps: float = 80.0) -> pd.DataFrame:
    """
    获取RPS排名靠前的股票（用于选股候选池）

    Args:
        top_n: 返回前N只
        period: 排序周期（默认120日，中长期趋势最稳定）
        trade_date: 计算日期
        min_rps: 最低RPS阈值

    Returns:
        DataFrame，按RPS降序排列
    """
    df = calc_all_rps(trade_date, periods=[period])
    if df.empty:
        return pd.DataFrame()

    rps_col = f'rps_{period}'
    if rps_col not in df.columns:
        return pd.DataFrame()

    # 过滤低RPS
    df_filtered = df[df[rps_col] >= min_rps].copy()
    if df_filtered.empty:
        # 如果阈值过高则降低
        df_filtered = df.copy()

    df_filtered = df_filtered.sort_values(rps_col, ascending=False).head(top_n)
    return df_filtered[['ts_code', 'name', rps_col, f'ret_{period}']]


def get_rps_signal(rps_values: Dict[str, Optional[float]]) -> Dict[str, str]:
    """
    根据RPS值生成交易信号

    Args:
        rps_values: {rps_20: xx, rps_60: xx, rps_120: xx, rps_250: xx}

    Returns:
        dict: {周期: 信号级别+说明}
    """
    signals = {}
    for period_name, val in rps_values.items():
        if val is None or not period_name.startswith('rps_'):
            continue
        period_label = period_name.replace('rps_', 'RPS_')

        if val >= 95:
            signals[period_name] = f"🔥 {period_label}={val:.0f} 极强"
        elif val >= 90:
            signals[period_name] = f"✅ {period_label}={val:.0f} 强势(欧奈尔买入区)"
        elif val >= 80:
            signals[period_name] = f"🟢 {period_label}={val:.0f} 偏强(关注)"
        elif val >= 50:
            signals[period_name] = f"➖ {period_label}={val:.0f} 一般"
        elif val >= 30:
            signals[period_name] = f"🟡 {period_label}={val:.0f} 偏弱"
        else:
            signals[period_name] = f"🔴 {period_label}={val:.0f} 弱势(回避)"

    # 综合判断
    valid_values = [v for v in rps_values.values() if v is not None and v > 0]
    if valid_values:
        avg_rps = sum(valid_values) / len(valid_values)
        if avg_rps >= 85:
            signals['overall'] = f"🏆 综合RPS={avg_rps:.0f} — 强势股特征"
        elif avg_rps >= 70:
            signals['overall'] = f"📈 综合RPS={avg_rps:.0f} — 中等偏强"
        elif avg_rps >= 50:
            signals['overall'] = f"➡️ 综合RPS={avg_rps:.0f} — 市场中位"
        else:
            signals['overall'] = f"📉 综合RPS={avg_rps:.0f} — 弱势"
    else:
        signals['overall'] = "❓ RPS数据不足"

    return signals


# ============================================================
#  板块RPS
# ============================================================

def get_sector_avg_rps(trade_date: Optional[str] = None,
                       periods: Optional[List[int]] = None) -> pd.DataFrame:
    """
    计算板块（行业）的平均RPS，判断板块强弱

    Args:
        trade_date: 计算日期
        periods: 周期列表

    Returns:
        DataFrame: [industry, count, avg_rps_120, avg_rps_250, ...]
    """
    df = calc_all_rps(trade_date, periods)
    if df.empty:
        return pd.DataFrame()

    # 获取行业信息
    db = _get_db()
    try:
        industry = pd.read_sql_query(
            "SELECT ts_code, industry FROM stock_basic WHERE list_status='L' AND industry != ''",
            db.conn
        )
        df = df.merge(industry, on='ts_code', how='inner')
    except Exception:
        return pd.DataFrame()

    # 按行业分组
    rps_cols = [c for c in df.columns if c.startswith('rps_')]
    agg = {c: 'mean' for c in rps_cols}
    agg['ts_code'] = 'count'

    sector_rps = df.groupby('industry').agg(agg).round(1)
    # 重命名列：rps_20→avg_rps_20, ts_code→stock_count
    rename_map = {}
    for col in sector_rps.columns:
        if col == 'ts_code':
            rename_map[col] = 'stock_count'
        elif str(col).startswith('rps_'):
            rename_map[col] = f'avg_{col}'
    if rename_map:
        sector_rps = sector_rps.rename(columns=rename_map)
    sector_rps = sector_rps.reset_index()
    sort_col = 'avg_rps_120' if 'avg_rps_120' in sector_rps.columns else \
               ([c for c in sector_rps.columns if c.startswith('avg_rps_')] or [None])[0]
    if sort_col and sort_col in sector_rps.columns:
        sector_rps = sector_rps.sort_values(sort_col, ascending=False)

    return sector_rps


# ============================================================
#  主入口（命令行调用）
# ============================================================

def main():
    """命令行调用：python scripts/utils/rps.py [trade_date]"""
    import sys
    trade_date = sys.argv[1] if len(sys.argv) > 1 else None

    print("=" * 60)
    print("  RPS 相对价格强度计算引擎")
    print("=" * 60)

    df = calc_all_rps(trade_date)
    if df.empty:
        print("[RPS] ❌ 计算失败")
        return

    print(f"\n📊 共计算 {len(df)} 只股票")

    # 输出RPS前十
    if 'rps_120' in df.columns:
        print("\n🏆 RPS_120 TOP 10:")
        top10 = df.sort_values('rps_120', ascending=False).head(10)
        for _, row in top10.iterrows():
            name = row.get('name', '') or ''
            print(f"  {row['ts_code']:12s} {name:10s}"
                  f"  RPS_120={row.get('rps_120', 'N/A'):>5}  "
                  f"RPS_20={row.get('rps_20', 'N/A'):>5}  "
                  f"AVG={row.get('avg_rps', 'N/A'):.0f}")

    # 输出行业RPS（复用已计算的RPS数据）
    sector_df = calc_sector_avg_rps_from_df(df)
    if not sector_df.empty:
        print("\n🏭 行业RPS排名 TOP 10:")
        top_sectors = sector_df.head(10)
        for _, row in top_sectors.iterrows():
            print(f"  {row['industry']:12s} 股票{int(row['count'])}只"
                  f"  RPS_120={row.get('avg_rps_120', 'N/A'):>5}"
                  f"  RPS_20={row.get('avg_rps_20', 'N/A'):>5}")

    print(f"\n[RPS] ✅ 完成")


def calc_sector_avg_rps_from_df(df: pd.DataFrame) -> pd.DataFrame:
    """
    从已计算的RPS DataFrame计算行业平均RPS（不重复调用calc_all_rps）

    Args:
        df: calc_all_rps 返回的DataFrame

    Returns:
        DataFrame: [industry, count, avg_rps_20, avg_rps_60, avg_rps_120, avg_rps_250]
    """
    if df is None or df.empty:
        return pd.DataFrame()

    db = _get_db()
    try:
        industry = pd.read_sql_query(
            "SELECT ts_code, industry FROM stock_basic WHERE list_status='L' AND industry != ''",
            db.conn
        )
        df2 = df.merge(industry, on='ts_code', how='inner')
    except Exception:
        return pd.DataFrame()

    rps_cols = [c for c in df2.columns if c.startswith('rps_')]
    if not rps_cols:
        return pd.DataFrame()

    # 按行业分组聚合
    agg_dict = {c: 'mean' for c in rps_cols}
    agg_dict['ts_code'] = 'count'
    sector_rps = df2.groupby('industry').agg(agg_dict).round(1)

    # 重命名列：rps_20→avg_rps_20, ts_code→stock_count
    rename_map = {}
    for col in sector_rps.columns:
        if col == 'ts_code':
            rename_map[col] = 'stock_count'
        elif str(col).startswith('rps_'):
            rename_map[col] = f'avg_{col}'
    if rename_map:
        sector_rps = sector_rps.rename(columns=rename_map)

    sector_rps = sector_rps.reset_index()
    # 排序
    sort_col = 'avg_rps_120' if 'avg_rps_120' in sector_rps.columns else \
               ([c for c in sector_rps.columns if c.startswith('avg_rps_')] or [None])[0]
    if sort_col and sort_col in sector_rps.columns:
        sector_rps = sector_rps.sort_values(sort_col, ascending=False)

    return sector_rps


if __name__ == "__main__":
    main()
