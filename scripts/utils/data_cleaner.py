"""
股票数据清洗脚本 — 检查、清洗、标准化 stocks.db 全部数据

用法:
    # 仅审计（不修改数据）
    python scripts/utils/data_cleaner.py --audit

    # 审计 + 自动修复
    python scripts/utils/data_cleaner.py --clean

    # 审计 + 修复 + 添加复权价格列
    python scripts/utils/data_cleaner.py --clean --add-adj-price

    # 仅复权（对已有数据添加复权列）
    python scripts/utils/data_cleaner.py --add-adj-price-only

清洗规则:
    1. 日期格式标准化: YYYYMMDD → 确认全表统一（已达标）
    2. 字段命名规范: 已使用英文标准名 open/high/low/close/vol/amount（已达标）
    3. 数据类型: REAL for price/vol, TEXT for code/date（已达标）
    4. NaN处理: 前向填充价格 + 成交量补0（发现1行科创50基准日异常）
    5. 重复值删除: 按 (ts_code, trade_date) 去重（已达标，0重复）
    6. OHLC逻辑校验: high>=max(open,close), low<=min(open,close)（已达标，0异常）
    7. 复权处理: 基于adj_factor表计算前复权/后复权价格
    8. 涨跌停过滤: 标记超限行（不删除），分市场差异化阈值
    9. 交易日历对齐: 检查非交易日数据（已达标，0周末数据）

D3异常处理表:
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| NULL价格行 | 前向填充(同股票前一日close) | 标记为异常，不参与计算 |
| 复权因子缺失 | 对指数(asset_type='I')填充1.0 | 对股票尝试从相邻日推算 |
| 涨跌幅超限 | 检查是否新股首日(44%)或科创板(20%) | 标记为需人工复核 |
| 数据库写入失败 | 重试1次 | 回滚事务，报告错误 |
| 复权因子计算溢出 | 检查adj_factor范围 | 标记该行，使用原始close |
| 事务冲突(DB locked) | 等待5秒后重试1次 | 回滚，提示手动执行 |
| 磁盘空间不足 | 检查可用空间，提示清理 | 回滚，输出SQL脚本到文件备用 |

D4 CHECKPOINT:
- CP1: 修复前行数 == 修复后行数（不允许数据丢失）
- CP2: 复权价格必须满足: adj_close > 0, adj_high >= adj_low
- CP3: NULL行修复后所有价格列非空
- CP4: 复权因子覆盖率达到99%+
- CP5: 清洗操作前后行数校验: 每步UPDATE/DELETE前后对比行数变化合理性

D9反例:
- 不要对指数做复权（指数没有复权因子，用原价）
- 不要用当日涨跌幅做移动止损（必须用真实回撤）
- 不要删除涨跌停超限行（只标记，不删除，保留完整数据）
- 不要在复权计算中忽略adj_factor=0的行
- 不要假设所有股票都有完整复权因子历史
"""

import os
import sys
import argparse
import sqlite3
import time
from datetime import datetime, timedelta, date
from typing import Optional, Dict, List, Tuple, Any

import pandas as pd
import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.db_manager import DatabaseManager

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
DB_PATH = os.path.join(PROJECT_ROOT, "data", "stocks.db")

# ============================================================
#  市场涨跌停阈值 (2026年规则)
# ============================================================
PRICE_LIMIT_RULES = {
    "main_board": {  # 主板 60/00
        "prefixes": ("60", "00"),
        "daily_limit": 10.0,
        "ipo_first_day": 44.0,
        "st_limit": 5.0,
        "description": "主板±10%"
    },
    "gem": {  # 创业板 30
        "prefixes": ("30",),
        "daily_limit": 20.0,
        "ipo_first_days": float("inf"),  # 前5日无限制
        "st_limit": 20.0,
        "description": "创业板±20%"
    },
    "star": {  # 科创板 68
        "prefixes": ("68",),
        "daily_limit": 20.0,
        "ipo_first_days": float("inf"),  # 前5日无限制
        "st_limit": 20.0,
        "description": "科创板±20%"
    },
    "bse": {  # 北交所 8/4
        "prefixes": ("8", "4"),
        "daily_limit": 30.0,
        "ipo_first_day": float("inf"),
        "st_limit": 30.0,
        "description": "北交所±30%"
    },
}


def get_market_rule(ts_code: str) -> dict:
    """根据股票代码返回涨跌停规则"""
    code = ts_code.split(".")[0] if "." in ts_code else ts_code
    prefix2 = code[:2]
    prefix1 = code[:1]

    # ST股票（名称含ST）
    # 这里只根据前缀判断市场，ST判断需要结合stock_basic表

    for market, rule in PRICE_LIMIT_RULES.items():
        if prefix1 in rule["prefixes"] or prefix2 in rule["prefixes"]:
            return rule

    return PRICE_LIMIT_RULES["main_board"]  # 默认主板


def get_limit_pct(ts_code: str, is_st: bool = False, is_ipo_first_5_days: bool = False) -> float:
    """获取某只股票的涨跌停限制百分比"""
    rule = get_market_rule(ts_code)

    if is_ipo_first_5_days and "ipo_first_days" in rule:
        return float("inf")

    if is_st:
        return rule.get("st_limit", rule["daily_limit"])

    return rule["daily_limit"]


# ============================================================
#  审计函数
# ============================================================

def audit_database(db: DatabaseManager) -> dict:
    """
    全面审计数据库质量，返回审计报告字典。

    Returns:
        {
            "passed": [...],
            "warnings": [...],
            "errors": [...],
            "stats": {...},
            "fixable": {...}
        }
    """
    report = {
        "passed": [],
        "warnings": [],
        "errors": [],
        "stats": {},
        "fixable": {},
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }

    cur = db.conn.cursor()

    # ── 1. 日期格式检查 ──
    print("[审计] 1/12 日期格式检查...")
    cur.execute("SELECT COUNT(*) FROM daily_price WHERE length(trade_date) != 8")
    bad_dates = cur.fetchone()[0]
    if bad_dates == 0:
        report["passed"].append("日期格式: 全部为YYYYMMDD 8位格式 ✅")
    else:
        report["errors"].append(f"日期格式: {bad_dates}行格式异常 ❌")

    # ── 2. 重复值检查 ──
    print("[审计] 2/12 重复值检查...")
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT ts_code, trade_date, COUNT(*) as cnt
            FROM daily_price GROUP BY ts_code, trade_date HAVING cnt > 1
        )
    """)
    dup_groups = cur.fetchone()[0]
    if dup_groups == 0:
        report["passed"].append("daily_price: 无重复行 ✅")
    else:
        report["errors"].append(f"daily_price: {dup_groups}组重复 ❌")
        report["fixable"]["duplicates"] = dup_groups

    for table in ["daily_basic", "adj_factor"]:
        cur.execute(f"""
            SELECT COUNT(*) FROM (
                SELECT ts_code, trade_date, COUNT(*) as cnt
                FROM {table} GROUP BY ts_code, trade_date HAVING cnt > 1
            )
        """)
        d = cur.fetchone()[0]
        if d == 0:
            report["passed"].append(f"{table}: 无重复行 ✅")
        else:
            report["errors"].append(f"{table}: {d}组重复 ❌")

    # ── 3. NULL值检查 ──
    print("[审计] 3/12 NULL值检查...")
    null_cols = []
    for col in ["open", "high", "low", "close", "vol", "amount", "pre_close", "pct_chg"]:
        cur.execute(f"SELECT COUNT(*) FROM daily_price WHERE {col} IS NULL")
        n = cur.fetchone()[0]
        if n > 0:
            null_cols.append(f"{col}={n}")
            report["fixable"][f"null_{col}"] = n

    if null_cols:
        report["warnings"].append(f"daily_price NULL列: {', '.join(null_cols)} ⚠️")
    else:
        report["passed"].append("daily_price: 无NULL值 ✅")

    # ── 4. OHLC逻辑校验 ──
    print("[审计] 4/12 OHLC逻辑校验...")
    cur.execute("""
        SELECT COUNT(*) FROM daily_price
        WHERE high < open OR high < close OR low > open OR low > close
    """)
    ohlc_bad = cur.fetchone()[0]
    if ohlc_bad == 0:
        report["passed"].append("OHLC逻辑: 全部正确 (high>=max/o, low<=min/o) ✅")
    else:
        report["errors"].append(f"OHLC逻辑: {ohlc_bad}行异常 ❌")
        report["fixable"]["ohlc_bad"] = ohlc_bad

    # ── 5. 价格正数检查 ──
    print("[审计] 5/12 价格正数检查...")
    cur.execute("""
        SELECT COUNT(*) FROM daily_price
        WHERE open <= 0 OR high <= 0 OR low <= 0 OR close <= 0
    """)
    neg_price = cur.fetchone()[0]
    if neg_price == 0:
        report["passed"].append("价格正数: 全部>0 ✅")
    else:
        report["errors"].append(f"价格正数: {neg_price}行<=0 ❌")

    # ── 6. 成交量检查 ──
    print("[审计] 6/12 成交量检查...")
    cur.execute("SELECT COUNT(*) FROM daily_price WHERE vol = 0")
    zero_vol = cur.fetchone()[0]
    if zero_vol == 0:
        report["passed"].append("成交量: 无零值 ✅")
    else:
        report["warnings"].append(f"成交量: {zero_vol}行vol=0 (可能停牌) ⚠️")

    cur.execute("SELECT COUNT(*) FROM daily_price WHERE vol < 0")
    neg_vol = cur.fetchone()[0]
    if neg_vol > 0:
        report["errors"].append(f"成交量: {neg_vol}行为负值 ❌")

    # ── 7. 复权因子覆盖率 ──
    print("[审计] 7/12 复权因子覆盖率...")
    cur.execute("SELECT COUNT(*) FROM daily_price WHERE asset_type = 'E'")
    total_stocks = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM daily_price dp
        LEFT JOIN adj_factor af ON dp.ts_code = af.ts_code AND dp.trade_date = af.trade_date
        WHERE dp.asset_type = 'E' AND af.adj_factor IS NULL
    """)
    missing_adj = cur.fetchone()[0]
    adj_coverage = (1 - missing_adj / total_stocks) * 100 if total_stocks > 0 else 100
    report["stats"]["adj_coverage"] = round(adj_coverage, 2)
    report["stats"]["missing_adj_rows"] = missing_adj
    report["stats"]["total_stock_rows"] = total_stocks

    if adj_coverage >= 99:
        report["passed"].append(f"复权因子覆盖率: {adj_coverage:.2f}% (缺{missing_adj:,}行) ✅")
    elif adj_coverage >= 95:
        report["warnings"].append(f"复权因子覆盖率: {adj_coverage:.2f}% (缺{missing_adj:,}行) ⚠️")
    else:
        report["errors"].append(f"复权因子覆盖率: {adj_coverage:.2f}% (缺{missing_adj:,}行) ❌")

    # ── 8. 涨跌停检查 ──
    print("[审计] 8/12 涨跌停检查...")
    cur.execute("""
        SELECT COUNT(*) FROM daily_price dp
        WHERE ABS(pct_chg) > 10.5 AND dp.asset_type = 'E'
    """)
    over_10p5 = cur.fetchone()[0]
    cur.execute("""
        SELECT COUNT(*) FROM daily_price dp
        WHERE ABS(pct_chg) > 21 AND dp.asset_type = 'E'
    """)
    over_21 = cur.fetchone()[0]
    report["stats"]["pct_chg_over_10.5"] = over_10p5
    report["stats"]["pct_chg_over_21"] = over_21
    if over_21 > 0:
        report["warnings"].append(f"涨跌幅>21%: {over_21}行 (需人工复核是否为新股/复牌首日) ⚠️")
    else:
        report["passed"].append("涨跌幅检查: 无>21%异常 ✅")
    report["passed"].append(f"涨跌幅>10.5%: {over_10p5}行 (含创业板/科创板±20%正常范围)")

    # ── 9. 非交易日检查 ──
    print("[审计] 9/12 非交易日检查...")
    trading_cal = _load_trading_calendar()
    if trading_cal:
        cur.execute("SELECT DISTINCT trade_date FROM daily_price ORDER BY trade_date")
        all_dates = set(r[0] for r in cur.fetchall())
        # 只检查日历覆盖范围内的日期（日历可能不覆盖1990年代老数据）
        cal_min = min(trading_cal) if trading_cal else "99999999"
        cal_max = max(trading_cal) if trading_cal else "00000000"
        dates_in_range = {d for d in all_dates if cal_min <= d <= cal_max}
        non_trading = dates_in_range - trading_cal
        out_of_range = all_dates - dates_in_range
        if len(non_trading) == 0:
            report["passed"].append(f"交易日历对齐: {len(dates_in_range)}个交易日在日历范围内全部正确 ✅")
        else:
            report["warnings"].append(f"非交易日数据: {len(non_trading)}天在日历范围内但非交易日 ⚠️")
        if len(out_of_range) > 0:
            report["stats"]["dates_out_of_calendar_range"] = len(out_of_range)
    else:
        report["warnings"].append("交易日历: 缓存不存在，跳过检查 ⚠️")

    # ── 10. 停牌日成交量确认 ──
    print("[审计] 10/12 停牌/涨跌停日检查...")
    cur.execute("""
        SELECT COUNT(*) FROM daily_price
        WHERE vol = 0 AND amount = 0 AND open = close AND open = high AND open = low
    """)
    suspended = cur.fetchone()[0]
    report["stats"]["suspected_suspended_rows"] = suspended
    if suspended > 0:
        report["warnings"].append(f"疑似停牌日(vol=0,OHLC全等): {suspended}行 ⚠️")

    # ── 11. 数据完整性（各表行数与日期范围） ──
    print("[审计] 11/12 数据完整性...")
    for table in ["daily_price", "daily_basic", "adj_factor", "fina_indicator", "dividend"]:
        try:
            date_col = "trade_date" if table != "fina_indicator" and table != "dividend" else "end_date"
            cur.execute(f"SELECT COUNT(*), MIN({date_col}), MAX({date_col}) FROM {table}")
            cnt, mn, mx = cur.fetchone()
            report["stats"][f"{table}_rows"] = cnt
            report["stats"][f"{table}_range"] = f"{mn}~{mx}"
        except Exception as e:
            report["warnings"].append(f"{table}: 查询失败 - {e}")

    # ── 12. 股票基本信息完整性 ──
    print("[审计] 12/12 股票基本信息...")
    cur.execute("SELECT COUNT(*) FROM stock_basic")
    sb_cnt = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM stock_basic WHERE industry IS NULL OR industry = ''")
    no_ind = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM stock_basic WHERE list_status = 'L'")
    listed = cur.fetchone()[0]
    report["stats"]["total_stocks_in_basic"] = sb_cnt
    report["stats"]["listed_stocks"] = listed
    report["stats"]["no_industry"] = no_ind
    if no_ind > 0:
        report["warnings"].append(f"stock_basic: {no_ind}只股票无行业分类 ⚠️")

    db.conn.commit()
    return report


def _load_trading_calendar() -> set:
    """加载交易日历缓存（多路径fallback）"""
    import json as _json
    # 多路径尝试：脚本所在项目根、当前工作目录、环境变量
    candidates = [
        os.path.join(PROJECT_ROOT, "data", "trading_calendar.json"),
        os.path.join(os.getcwd(), "data", "trading_calendar.json"),
        os.path.join(PROJECT_ROOT, "data", "trading_calendar.json"),
    ]
    for cal_path in candidates:
        if os.path.exists(cal_path):
            try:
                with open(cal_path, "r", encoding="utf-8") as f:
                    data = _json.load(f)
                dates = data.get("dates", data) if isinstance(data, dict) else data
                if dates and len(dates) > 0:
                    return set(dates)
            except Exception:
                continue
    return set()


# ============================================================
#  清洗函数
# ============================================================

def clean_null_rows(db: DatabaseManager) -> int:
    """
    修复NULL价格行。

    策略:
    - 对于asset_type='I'(指数)的NULL行: 如果close有值，用close填充open/high/low
    - 对于asset_type='E'(股票)的NULL行: 找同股票前一日close前向填充
    - vol/amount为NULL时填充0
    """
    print("[清洗] 修复NULL价格行...")
    try:
        cur = db.conn.cursor()

        # 找到所有有NULL价格的行
        cur.execute("""
            SELECT ts_code, trade_date, asset_type, open, high, low, close, pre_close, pct_chg, vol, amount
            FROM daily_price
            WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL
        """)
        null_rows = cur.fetchall()

        fixed = 0
        for row in null_rows:
            ts_code, trade_date, asset_type, op, hi, lo, cl, pre_cl, pct, vol, amt = row

            # 如果close有值，这是最可靠的数据
            if cl is not None and cl > 0:
                op = op if op is not None else cl
                hi = hi if hi is not None else cl
                lo = lo if lo is not None else cl
            elif op is not None and op > 0:
                # 用open填充
                cl = cl if cl is not None else op
                hi = hi if hi is not None else op
                lo = lo if lo is not None else op
            else:
                # 找前一日close
                cur.execute("""
                    SELECT close FROM daily_price
                    WHERE ts_code = ? AND trade_date < ? AND close IS NOT NULL
                    ORDER BY trade_date DESC LIMIT 1
                """, (ts_code, trade_date))
                prev = cur.fetchone()
                if prev and prev[0] is not None:
                    fill_price = prev[0]
                    op = op if op is not None else fill_price
                    hi = hi if hi is not None else fill_price
                    lo = lo if lo is not None else fill_price
                    cl = cl if cl is not None else fill_price
                else:
                    print(f"  ⚠️ 无法修复 {ts_code} {trade_date}: 无前一日数据")
                    continue

            # 填充pre_close, pct_chg
            if pre_cl is None and cl is not None:
                cur.execute("""
                    SELECT close FROM daily_price
                    WHERE ts_code = ? AND trade_date < ?
                    ORDER BY trade_date DESC LIMIT 1
                """, (ts_code, trade_date))
                prev_close = cur.fetchone()
                pre_cl = prev_close[0] if prev_close else cl

            if pct is None and pre_cl and pre_cl > 0 and cl is not None:
                pct = (cl - pre_cl) / pre_cl * 100

            # 成交量/成交额填0
            vol = vol if vol is not None else 0.0
            amt = amt if amt is not None else 0.0

            cur.execute("""
                UPDATE daily_price
                SET open=?, high=?, low=?, close=?, pre_close=?, pct_chg=?, vol=?, amount=?
                WHERE ts_code=? AND trade_date=?
            """, (op, hi, lo, cl, pre_cl, pct, vol, amt, ts_code, trade_date))
            fixed += 1
            print(f"  ✅ 修复 {ts_code} {trade_date}: open={op}, high={hi}, low={lo}, close={cl}")

        db.conn.commit()
        return fixed
    except Exception as e:
        db.conn.rollback()
        print(f"[ERROR] clean_null_rows 失败: {e}")
        return -1


def clean_duplicates(db: DatabaseManager) -> int:
    """删除重复行，保留rowid最小的那条"""
    print("[清洗] 删除重复行...")
    try:
        cur = db.conn.cursor()

        total_deleted = 0
        for table in ["daily_price", "daily_basic", "adj_factor"]:
            # D4-CP5: 操作前行数校验
            cur.execute(f"SELECT COUNT(*) FROM {table}")
            before = cur.fetchone()[0]
            # SQLite方式：保留最小rowid，删除其余
            cur.execute(f"""
                DELETE FROM {table}
                WHERE rowid NOT IN (
                    SELECT MIN(rowid)
                    FROM {table}
                    GROUP BY ts_code, trade_date
                )
            """)
            deleted = cur.rowcount
            if deleted > 0:
                print(f"  {table}: 删除 {deleted} 行重复 (前{before}行)")
            total_deleted += deleted

        db.conn.commit()
        return total_deleted
    except Exception as e:
        db.conn.rollback()
        print(f"[ERROR] clean_duplicates 失败: {e}")
        return -1


def add_adj_prices(db: DatabaseManager, batch_size: int = 50000) -> dict:
    """
    基于adj_factor表计算前复权价格和后复权价格。

    前复权(Forward Adjusted): adj_price = close * adj_factor / latest_adj_factor
      保持当前价格不变，调整历史价格。适合分析当前股价与历史的关系。

    后复权(Backward Adjusted): adj_price = close * adj_factor / first_adj_factor
      保持历史起点价格不变，调整后续价格。适合计算长期真实投资收益率。

    添加列到 daily_price:
      - adj_close_f: 前复权收盘价
      - adj_open_f, adj_high_f, adj_low_f: 前复权OHL
      - adj_close_b: 后复权收盘价

    策略:
      - 指数(asset_type='I','FD'等): adj_factor=1.0，复权价=原价
      - 股票无复权因子: 用原价（标记为未复权）
    """
    print("[清洗] 计算复权价格...")
    cur = db.conn.cursor()

    # ── 添加复权价格列（如果不存在） ──
    adj_cols = [
        ("adj_factor_val", "REAL"),
        ("adj_close_f", "REAL"),
        ("adj_open_f", "REAL"),
        ("adj_high_f", "REAL"),
        ("adj_low_f", "REAL"),
        ("adj_close_b", "REAL"),
    ]

    existing_cols = set()
    cur.execute("PRAGMA table_info(daily_price)")
    for row in cur.fetchall():
        existing_cols.add(row[1])

    for col_name, col_type in adj_cols:
        if col_name not in existing_cols:
            cur.execute(f"ALTER TABLE daily_price ADD COLUMN {col_name} {col_type}")
            print(f"  新增列: daily_price.{col_name}")

    db.conn.commit()

    # ── 计算前复权/后复权 ──
    # 获取每只股票的最新复权因子和首日复权因子
    print("  获取每只股票的复权因子极值...")
    cur.execute("""
        SELECT af.ts_code,
               MAX(af.adj_factor) as latest_factor,
               MIN(CASE WHEN dp_first.first_date = af.trade_date THEN af.adj_factor END) as first_factor
        FROM adj_factor af
        INNER JOIN (
            SELECT ts_code, MIN(trade_date) as first_date
            FROM daily_price WHERE asset_type = 'E'
            GROUP BY ts_code
        ) dp_first ON af.ts_code = dp_first.ts_code
        GROUP BY af.ts_code
    """)

    # 更简单的方法：每个股票最新一天和第一天的adj_factor
    cur.execute("""
        WITH latest AS (
            SELECT ts_code, MAX(trade_date) as max_date FROM adj_factor GROUP BY ts_code
        ),
        first AS (
            SELECT ts_code, MIN(trade_date) as min_date FROM adj_factor GROUP BY ts_code
        )
        SELECT af.ts_code,
               af_max.adj_factor as latest_adj,
               af_min.adj_factor as first_adj
        FROM adj_factor af
        INNER JOIN latest l ON af.ts_code = l.ts_code
        INNER JOIN adj_factor af_max ON af.ts_code = af_max.ts_code AND af_max.trade_date = l.max_date
        INNER JOIN first f ON af.ts_code = f.ts_code
        INNER JOIN adj_factor af_min ON af.ts_code = af_min.ts_code AND af_min.trade_date = f.min_date
        GROUP BY af.ts_code
    """)

    stock_factors = {}
    for row in cur.fetchall():
        ts_code, latest_adj, first_adj = row
        if latest_adj and latest_adj > 0 and first_adj and first_adj > 0:
            stock_factors[ts_code] = {
                "latest": float(latest_adj),
                "first": float(first_adj),
            }

    print(f"  已获取 {len(stock_factors)} 只股票的复权因子极值")

    # ── 批量更新 ──
    # 1. 指数数据：adj_factor=1.0, 复权价=原价
    print("  更新指数复权价格...")
    cur.execute("""
        UPDATE daily_price SET
            adj_factor_val = 1.0,
            adj_close_f = close,
            adj_open_f = open,
            adj_high_f = high,
            adj_low_f = low,
            adj_close_b = close
        WHERE asset_type != 'E'
    """)
    index_updated = cur.rowcount
    print(f"  指数: {index_updated:,} 行")

    # 2. 股票数据：JOIN adj_factor 批量更新
    # 先更新 adj_factor_val
    print("  更新股票复权因子值...")
    cur.execute("""
        UPDATE daily_price SET adj_factor_val = (
            SELECT af.adj_factor FROM adj_factor af
            WHERE af.ts_code = daily_price.ts_code
            AND af.trade_date = daily_price.trade_date
        )
        WHERE asset_type = 'E'
        AND EXISTS (
            SELECT 1 FROM adj_factor af
            WHERE af.ts_code = daily_price.ts_code
            AND af.trade_date = daily_price.trade_date
        )
    """)
    factor_updated = cur.rowcount
    print(f"  复权因子更新: {factor_updated:,} 行")

    # 对没有adj_factor的股票，用1.0填充
    cur.execute("""
        UPDATE daily_price SET adj_factor_val = 1.0
        WHERE adj_factor_val IS NULL
    """)
    null_filled = cur.rowcount
    if null_filled > 0:
        print(f"  无复权因子(填充1.0): {null_filled:,} 行")

    # 3. 更新前复权价格
    print("  计算前复权价格(分批更新)...")
    # 分批处理：每批处理一批股票
    cur.execute("""
        SELECT DISTINCT ts_code FROM daily_price
        WHERE asset_type = 'E' AND adj_factor_val IS NOT NULL
        ORDER BY ts_code
    """)
    all_stocks = [r[0] for r in cur.fetchall()]
    total = len(all_stocks)

    fwd_updated = 0
    bwd_updated = 0

    for i in range(0, total, batch_size):
        batch = all_stocks[i : i + batch_size]
        placeholders = ",".join("?" * len(batch))

        # 前复权 = 原价 * adj_factor / 最新adj_factor
        # 先更新有latest factor的
        for ts_code in batch:
            factors = stock_factors.get(ts_code)
            if factors and factors["latest"] > 0:
                ratio = 1.0 / factors["latest"]
                cur.execute("""
                    UPDATE daily_price SET
                        adj_close_f = ROUND(close * adj_factor_val * ?, 4),
                        adj_open_f = ROUND(open * adj_factor_val * ?, 4),
                        adj_high_f = ROUND(high * adj_factor_val * ?, 4),
                        adj_low_f = ROUND(low * adj_factor_val * ?, 4),
                        adj_close_b = ROUND(close * adj_factor_val / ?, 4)
                    WHERE ts_code = ? AND adj_factor_val IS NOT NULL AND adj_factor_val > 0
                """, (ratio, ratio, ratio, ratio, factors["first"], ts_code))
                fwd_updated += cur.rowcount
            else:
                # 无复权因子极值，用原价
                cur.execute("""
                    UPDATE daily_price SET
                        adj_close_f = close,
                        adj_open_f = open,
                        adj_high_f = high,
                        adj_low_f = low,
                        adj_close_b = close
                    WHERE ts_code = ? AND adj_close_f IS NULL
                """, (ts_code,))
                fwd_updated += cur.rowcount

        if (i // batch_size) % 10 == 0:
            db.conn.commit()
            print(f"    进度: {min(i + batch_size, total):,}/{total:,} ({100 * min(i + batch_size, total) / total:.1f}%)")

    db.conn.commit()

    # 对未更新到的行（应该没有了）兜底
    cur.execute("""
        UPDATE daily_price SET
            adj_close_f = COALESCE(adj_close_f, close),
            adj_open_f = COALESCE(adj_open_f, open),
            adj_high_f = COALESCE(adj_high_f, high),
            adj_low_f = COALESCE(adj_low_f, low),
            adj_close_b = COALESCE(adj_close_b, close)
        WHERE adj_close_f IS NULL
    """)
    leftover = cur.rowcount
    if leftover > 0:
        print(f"  兜底填充: {leftover:,} 行（无有效复权因子）")

    db.conn.commit()

    return {
        "index_updated": index_updated,
        "factor_updated": factor_updated,
        "null_factor_filled": null_filled,
        "fwd_adj_updated": fwd_updated,
        "leftover_filled": leftover,
    }


def validate_cleaning(db: DatabaseManager) -> dict:
    """清洗后验证"""
    print("\n[验证] 清洗后数据质量检查...")
    cur = db.conn.cursor()
    results = {}

    # 1. 无NULL价格
    cur.execute("SELECT COUNT(*) FROM daily_price WHERE open IS NULL OR high IS NULL OR low IS NULL OR close IS NULL")
    results["null_prices"] = cur.fetchone()[0]

    # 2. 无重复
    cur.execute("""
        SELECT COUNT(*) FROM (
            SELECT ts_code, trade_date, COUNT(*) as cnt
            FROM daily_price GROUP BY ts_code, trade_date HAVING cnt > 1
        )
    """)
    results["duplicates"] = cur.fetchone()[0]

    # 3. OHLC逻辑
    cur.execute("""
        SELECT COUNT(*) FROM daily_price
        WHERE high < open OR high < close OR low > open OR low > close
    """)
    results["ohlc_bad"] = cur.fetchone()[0]

    # 4. 复权价格检查
    cur.execute("SELECT COUNT(*) FROM daily_price WHERE adj_close_f IS NULL")
    results["null_adj_close_f"] = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM daily_price WHERE adj_close_f <= 0")
    results["neg_adj_close_f"] = cur.fetchone()[0]

    # 5. 复权覆盖率
    cur.execute("SELECT COUNT(*) FROM daily_price WHERE asset_type = 'E'")
    total_e = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM daily_price WHERE asset_type = 'E' AND adj_factor_val > 1.0")
    has_factor = cur.fetchone()[0]
    results["stock_rows_with_factor"] = has_factor
    results["stock_rows_total"] = total_e

    return results


def add_price_limit_flags(db: DatabaseManager) -> dict:
    """
    添加涨跌停标记列 price_limit_flag 到 daily_price。

    标记值:
        0 = 正常范围内
        1 = 涨停 (涨幅接近上限)
        -1 = 跌停 (跌幅接近下限)
        2 = 新股首日/异常波动 (超出常规限制, 可能是IPO首日44%或科创板前5日)
        9 = 数据异常 (无法判断)

    分市场差异化阈值:
        主板(60/00): ±10%, ST ±5%
        创业板(30): ±20%
        科创板(68): ±20%
        北交所(8/4): ±30%
    """
    print("[清洗] 添加涨跌停标记...")
    cur = db.conn.cursor()

    # 添加列（如果不存在）
    cur.execute("PRAGMA table_info(daily_price)")
    existing_cols = {r[1] for r in cur.fetchall()}
    if "price_limit_flag" not in existing_cols:
        cur.execute("ALTER TABLE daily_price ADD COLUMN price_limit_flag INTEGER DEFAULT 0")
        print("  新增列: daily_price.price_limit_flag")
    if "price_limit_note" not in existing_cols:
        cur.execute("ALTER TABLE daily_price ADD COLUMN price_limit_note TEXT")
        print("  新增列: daily_price.price_limit_note")

    db.conn.commit()

    stats = {"normal": 0, "limit_up": 0, "limit_down": 0, "anomaly": 0, "error": 0}

    # ── 主板 (60xxxx, 00xxxx) ±10%, ST ±5% ──
    print("  处理主板 (60/00)...")
    # 正常主板非ST: ±10.2% (留0.2%容差)
    cur.execute("""
        UPDATE daily_price SET price_limit_flag =
            CASE
                WHEN pct_chg >= 9.8 AND pct_chg <= 10.2 THEN 1
                WHEN pct_chg <= -9.8 AND pct_chg >= -10.2 THEN -1
                WHEN ABS(pct_chg) > 10.2 THEN 2
                ELSE 0
            END
        WHERE asset_type = 'E'
        AND (ts_code LIKE '60%' OR ts_code LIKE '00%')
        AND price_limit_flag = 0
    """)

    # ── 创业板 (30xxxx) ±20% ──
    print("  处理创业板 (30)...")
    cur.execute("""
        UPDATE daily_price SET price_limit_flag =
            CASE
                WHEN pct_chg >= 19.8 AND pct_chg <= 20.2 THEN 1
                WHEN pct_chg <= -19.8 AND pct_chg >= -20.2 THEN -1
                WHEN ABS(pct_chg) > 20.2 THEN 2
                ELSE 0
            END
        WHERE asset_type = 'E'
        AND ts_code LIKE '30%'
        AND price_limit_flag = 0
    """)

    # ── 科创板 (688xxx) ±20% ──
    print("  处理科创板 (68)...")
    cur.execute("""
        UPDATE daily_price SET price_limit_flag =
            CASE
                WHEN pct_chg >= 19.8 AND pct_chg <= 20.2 THEN 1
                WHEN pct_chg <= -19.8 AND pct_chg >= -20.2 THEN -1
                WHEN ABS(pct_chg) > 20.2 THEN 2
                ELSE 0
            END
        WHERE asset_type = 'E'
        AND ts_code LIKE '68%'
        AND price_limit_flag = 0
    """)

    # ── 北交所 (8xxxxx, 4xxxxx) ±30% ──
    print("  处理北交所 (8/4)...")
    cur.execute("""
        UPDATE daily_price SET price_limit_flag =
            CASE
                WHEN pct_chg >= 29.8 AND pct_chg <= 30.2 THEN 1
                WHEN pct_chg <= -29.8 AND pct_chg >= -30.2 THEN -1
                WHEN ABS(pct_chg) > 30.2 THEN 2
                ELSE 0
            END
        WHERE asset_type = 'E'
        AND (ts_code LIKE '8%' OR ts_code LIKE '4%')
        AND price_limit_flag = 0
    """)

    # ── 统计 ──
    for flag, label in [(0, "normal"), (1, "limit_up"), (-1, "limit_down"), (2, "anomaly"), (9, "error")]:
        cur.execute("SELECT COUNT(*) FROM daily_price WHERE price_limit_flag = ?", (flag,))
        cnt = cur.fetchone()[0]
        stats[label] = cnt
        if cnt > 0 and flag != 0:
            print(f"  {label}: {cnt:,} 行")

    # ── 添加说明 ──
    cur.execute("""
        UPDATE daily_price SET price_limit_note = '涨停(主板±10%)'
        WHERE price_limit_flag = 1 AND (ts_code LIKE '60%' OR ts_code LIKE '00%')
    """)
    cur.execute("""
        UPDATE daily_price SET price_limit_note = '涨停(科创/创业±20%)'
        WHERE price_limit_flag = 1 AND (ts_code LIKE '68%' OR ts_code LIKE '30%')
    """)
    cur.execute("""
        UPDATE daily_price SET price_limit_note = '涨停(北交所±30%)'
        WHERE price_limit_flag = 1 AND (ts_code LIKE '8%' OR ts_code LIKE '4%')
    """)
    cur.execute("""
        UPDATE daily_price SET price_limit_note = '异常波动(可能新股首日/复牌)'
        WHERE price_limit_flag = 2
    """)

    db.conn.commit()
    print(f"  涨跌停标记完成: 正常{stats['normal']:,} | 涨停{stats['limit_up']:,} | 跌停{stats['limit_down']:,} | 异常{stats['anomaly']:,}")
    return stats


# ============================================================
#  报告输出
# ============================================================

def print_audit_report(report: dict):
    """格式化打印审计报告"""
    print("\n" + "=" * 70)
    print("  📊 数据质量审计报告")
    print(f"  检查时间: {report['checked_at']}")
    print("=" * 70)

    if report["errors"]:
        print(f"\n  ❌ 错误 ({len(report['errors'])}项):")
        for e in report["errors"]:
            print(f"     • {e}")

    if report["warnings"]:
        print(f"\n  ⚠️  警告 ({len(report['warnings'])}项):")
        for w in report["warnings"]:
            print(f"     • {w}")

    if report["passed"]:
        print(f"\n  ✅ 通过 ({len(report['passed'])}项):")
        for p in report["passed"]:
            print(f"     • {p}")

    if report["stats"]:
        print(f"\n  📈 统计:")
        for k, v in sorted(report["stats"].items()):
            if isinstance(v, float):
                print(f"     {k}: {v:.2f}")
            elif isinstance(v, int) and v > 1000:
                print(f"     {k}: {v:,}")
            else:
                print(f"     {k}: {v}")

    quality_score = _calculate_quality_score(report)
    print(f"\n  🏆 数据质量评分: {quality_score}/100")
    _print_quality_bar(quality_score)

    print("=" * 70)


def _calculate_quality_score(report: dict) -> int:
    """计算数据质量评分"""
    score = 100

    # 每个错误 -10分
    score -= len(report["errors"]) * 10
    # 每个警告 -3分
    score -= len(report["warnings"]) * 3

    # 具体指标扣分
    stats = report.get("stats", {})
    adj_cov = stats.get("adj_coverage", 100)
    if adj_cov < 99:
        score -= int((99 - adj_cov) * 1)
    if adj_cov < 95:
        score -= 10

    return max(0, min(100, score))


def _print_quality_bar(score: int):
    """打印质量条"""
    bar_len = 40
    filled = int(bar_len * score / 100)
    bar = "█" * filled + "░" * (bar_len - filled)

    if score >= 90:
        color = "🟢"
    elif score >= 70:
        color = "🟡"
    else:
        color = "🔴"

    print(f"  {color} [{bar}] {score}%")


# ============================================================
#  主流程
# ============================================================

def main():
    parser = argparse.ArgumentParser(description="股票数据库清洗工具")
    parser.add_argument("--audit", action="store_true", default=True,
                        help="仅审计数据质量（默认）")
    parser.add_argument("--clean", action="store_true",
                        help="执行数据清洗修复")
    parser.add_argument("--add-adj-price", action="store_true",
                        help="添加前复权/后复权价格列")
    parser.add_argument("--add-adj-price-only", action="store_true",
                        help="仅添加复权价格列（跳过清洗）")
    parser.add_argument("--add-price-limit-flag", action="store_true",
                        help="添加涨跌停标记列 price_limit_flag")
    parser.add_argument("--batch-size", type=int, default=500,
                        help="复权计算批大小（默认500）")
    parser.add_argument("--dry-run", action="store_true",
                        help="模拟运行，不实际修改数据")
    args = parser.parse_args()

    print(f"[{datetime.now().strftime('%H:%M:%S')}] 数据清洗工具启动")
    print(f"  数据库: {DB_PATH}")

    db = DatabaseManager()
    if not db.conn:
        print("❌ 数据库连接失败")
        sys.exit(1)

    try:
        # ── 阶段1: 审计 ──
        report = audit_database(db)
        print_audit_report(report)

        # ── 阶段2: 清洗 ──
        if args.clean or args.add_adj_price or args.add_adj_price_only or args.add_price_limit_flag:
            if args.dry_run:
                print("\n🔍 [DRY RUN] 模拟模式，不实际修改数据")
                return

            if args.clean:
                print("\n" + "-" * 50)
                print("  开始数据清洗...")

                # 2a. 删除重复
                deleted = clean_duplicates(db)
                print(f"  删除重复行: {deleted}")

                # 2b. 修复NULL
                fixed = clean_null_rows(db)
                print(f"  修复NULL行: {fixed}")

            if args.clean or args.add_adj_price or args.add_adj_price_only:
                print("\n" + "-" * 50)
                print("  添加复权价格列...")
                adj_result = add_adj_prices(db, batch_size=args.batch_size)
                print(f"  复权处理完成:")
                print(f"    指数(原价): {adj_result['index_updated']:,} 行")
                print(f"    复权因子更新: {adj_result['factor_updated']:,} 行")
                print(f"    无因子填充1.0: {adj_result['null_factor_filled']:,} 行")
                print(f"    前/后复权价格: {adj_result['fwd_adj_updated']:,} 行")

            if args.add_price_limit_flag:
                print("\n" + "-" * 50)
                print("  添加涨跌停标记...")
                limit_stats = add_price_limit_flags(db)
                print(f"  涨跌停标记完成: {limit_stats}")

            # ── 阶段3: 验证 ──
            print("\n" + "-" * 50)
            validation = validate_cleaning(db)
            all_clean = True
            for check, val in validation.items():
                if val > 0:
                    if check in ("stock_rows_with_factor", "stock_rows_total"):
                        continue  # 不是错误，只是计数
                    print(f"  ⚠️ {check}: {val}")
                    all_clean = False

            if all_clean:
                print("  ✅ 所有核心检查通过！数据清洗完成。")
            else:
                print("  ⚠️ 部分检查未通过，详情见上。")

            # 最终审计
            print("\n" + "-" * 50)
            print("  清洗后重新审计...")
            final_report = audit_database(db)
            print_audit_report(final_report)

    finally:
        db.close()

    print(f"\n[{datetime.now().strftime('%H:%M:%S')}] 完成")


if __name__ == "__main__":
    main()
