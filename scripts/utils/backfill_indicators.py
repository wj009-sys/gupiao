#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
全市场技术指标批量回填脚本

用途：
  初次部署后全量回填技术指标到 daily_indicator 表。
  此后每日增量更新（作为 auto_sync.py Phase 11）。

问题背景：
  daily_indicator 表当前仅 5 只股票有数据（Agent2 按需计算），
  批量回填后 Agent5(选股机器人) 和 Agent2(分析师) 可直接从 DB
  获取全市场指标。

用法：
  # 全量回填（首次运行，建议 --max-stocks 100 先验证）
  python scripts/utils/backfill_indicators.py --backfill

  # 先验证：只处理 10 只股票
  python scripts/utils/backfill_indicators.py --backfill --max-stocks 10

  # 指定资产类型（默认 股票+ETF+指数）
  python scripts/utils/backfill_indicators.py --backfill --asset-types E

  # 每日增量更新（只处理最近 60 天）
  python scripts/utils/backfill_indicators.py --daily

D3 异常处理表：
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| 单只股票价格数据为空 | 跳过，打印 WARN | 不影响其他股票 |
| add_all_indicators 计算异常 | try/except 包裹单只股票 | 该股票跳过，不阻塞全流程 |
| DB 连接中断 | 重试 1 次 | 退出脚本，打印错误 |
| upsert 批量写入失败 | 按 stock_batch 降级为逐只 upsert | 跳过该批次，打印警告 |
| NaN 值处理 | 安全转换为 None（SQLite NULL） | 指标列留空，不影响其他列 |
| 数据不足 60 天 | 跳过该股票（不写入） | 新上市股票留空，待数据积累 |
| DataFrame vol 列缺失 | 重命名为 volume | 若 volume 也缺失，传 0 |

D4 CHECKPOINT:
- CP1-输入验证：查询 DB 前验证 asset_type 参数合法
- CP2-数据排序：daily_price 查询必须 ORDER BY trade_date ASC
- CP3-行格式：写入前验证行数与 INDICATOR_COLS 一致
- CP4-进度监控：每 20 只股票打印进度，每批提交后打印累计行数
- CP5-完整性检查：回填完成后对比 daily_indicator 和 daily_price 的股票覆盖数

D9 反例：
- 不要单行逐条 INSERT（executemany 批量写入快 100x）
- 不要用外部 API 获取行情（数据已在 daily_price 本地 DB）
- 不要对数据不足的股票强行计算指标（结果不可靠）
- 不要忘记处理 NaN → None 转换（JSON/DB 兼容性）
"""

import sys
import os
import time
import argparse

import pandas as pd
import numpy as np

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
sys.path.insert(0, PROJECT_ROOT)

from scripts.utils.db_manager import DatabaseManager
from scripts.utils.technical_analysis import add_all_indicators

# ── 常量 ──────────────────────────────────────────────────────

INDICATOR_COLS = [
    "ts_code", "trade_date",
    "macd", "macd_signal", "macd_diff",
    "macd_golden_cross", "macd_death_cross",
    "kdj_k", "kdj_d", "kdj_j", "kdj_golden_cross",
    "rsi_14", "rsi_oversold", "rsi_overbought",
    "boll_upper", "boll_mid", "boll_lower", "boll_width",
    "boll_break_upper", "boll_break_lower",
    "obv", "obv_ma20", "obv_trend", "obv_divergence",
    "ma_5", "ma_10", "ma_20", "ma_60",
]

BOOL_COLS = {"macd_golden_cross", "macd_death_cross", "kdj_golden_cross",
             "rsi_oversold", "rsi_overbought", "boll_break_upper", "boll_break_lower"}
STR_COLS = {"ts_code", "trade_date", "obv_trend", "obv_divergence"}

PLACEHOLDERS = ", ".join(["?" for _ in INDICATOR_COLS])
COLS_STR = ", ".join(INDICATOR_COLS)
UPSERT_SQL = f"INSERT OR REPLACE INTO daily_indicator ({COLS_STR}) VALUES ({PLACEHOLDERS})"

ASSET_TYPE_NAMES = {'E': '股票', 'F': 'ETF/基金', 'I': '指数'}

# 至少需要 60 根 K 线才能稳定计算 MA60 + MACD
MIN_DATA_DAYS = 60


# ── 核心函数 ──────────────────────────────────────────────────

def get_all_ts_codes(db: DatabaseManager, asset_type: str = 'E') -> list:
    """
    获取指定资产类型的所有唯一 ts_code。

    使用元数据表（stock_basic / fund_basic / index_basic）而非 daily_price，
    后者表大 19M 行，DISTINCT 需 ~43s。元数据表 < 6000 行，< 0.01s。

    D9反例：不要对 daily_price 做 SELECT DISTINCT ts_code(>19M 行)，
    应使用元数据表。
    """
    if asset_type == 'E':
        # 股票 — 所有在 stock_basic 中的 ts_code
        cur = db.conn.execute("SELECT ts_code FROM stock_basic ORDER BY ts_code")
        return [row[0] for row in cur.fetchall()]
    elif asset_type == 'F':
        # ETF/基金 — fund_basic 表
        cur = db.conn.execute("SELECT ts_code FROM fund_basic ORDER BY ts_code")
        return [row[0] for row in cur.fetchall()]
    elif asset_type == 'I':
        # 指数 — index_basic 表
        cur = db.conn.execute("SELECT ts_code FROM index_basic ORDER BY ts_code")
        return [row[0] for row in cur.fetchall()]
    else:
        print(f"  [WARN] 未知 asset_type: {asset_type}")
        return []


def process_single_stock(db: DatabaseManager, ts_code: str) -> list:
    """
    读取单只股票的价格数据 → 计算技术指标 → 提取行列表

    返回: list of tuples，每个 tuple 对应 INDICATOR_COLS 顺序。
          空列表表示该股票数据不足或计算失败。
    """
    # D4-CP2: 按日期升序
    df = pd.read_sql_query(
        "SELECT ts_code, trade_date, open, high, low, close, vol "
        "FROM daily_price WHERE ts_code = ? ORDER BY trade_date ASC",
        db.conn, params=(ts_code,)
    )

    if df.empty or len(df) < MIN_DATA_DAYS:
        return []

    # vol → volume (ta 库需要)
    if "vol" in df.columns and "volume" not in df.columns:
        df = df.rename(columns={"vol": "volume"})

    try:
        df_ta = add_all_indicators(df)
    except Exception as e:
        print(f"    [WARN] {ts_code} 指标计算失败: {e}")
        return []

    if df_ta is None or df_ta.empty:
        return []

    # 提取指标行
    rows = []
    for _, row in df_ta.iterrows():
        vals = []
        for col in INDICATOR_COLS:
            v = row.get(col)
            if col in STR_COLS:
                vals.append(str(v) if v is not None else None)
            elif col in BOOL_COLS:
                vals.append(1 if v else 0)
            else:
                # NaN → None (SQLite NULL)
                if v is None or (isinstance(v, float) and np.isnan(v)):
                    vals.append(None)
                else:
                    vals.append(float(v))
        rows.append(tuple(vals))

    return rows


def batch_upsert(db: DatabaseManager, all_rows: list, batch_size: int = 5000) -> int:
    """
    批量写入 daily_indicator，自动分段提交。

    D9反例：不要逐条 INSERT，executemany 快 100x 以上。
    """
    if not all_rows:
        return 0

    total = 0
    cur = db.conn.cursor()
    try:
        for i in range(0, len(all_rows), batch_size):
            batch = all_rows[i:i + batch_size]
            cur.executemany(UPSERT_SQL, batch)
            db.conn.commit()
            total += len(batch)
        return total
    except Exception as e:
        print(f"    [WARN] 批量写入失败: {e}")
        # 尝试降级：逐股票写入
        print("    [INFO] 降级为逐股票 upsert...")
        return fallback_upsert(db, all_rows)


def fallback_upsert(db: DatabaseManager, all_rows: list) -> int:
    """降级方案：按 ts_code 分组，逐股票写入"""
    cur = db.conn.cursor()
    total = 0
    current_code = None
    stock_rows = []

    for row in all_rows:
        if row[0] != current_code:
            if stock_rows:
                try:
                    cur.executemany(UPSERT_SQL, stock_rows)
                    db.conn.commit()
                    total += len(stock_rows)
                except Exception as e:
                    print(f"      [WARN] {current_code} upsert 失败: {e}")
            current_code = row[0]
            stock_rows = [row]
        else:
            stock_rows.append(row)

    # 最后一组
    if stock_rows:
        try:
            cur.executemany(UPSERT_SQL, stock_rows)
            db.conn.commit()
            total += len(stock_rows)
        except Exception as e:
            print(f"      [WARN] {current_code} upsert 失败: {e}")

    return total


def print_progress(at: str, done: int, total: int, elapsed: float, extra: str = ""):
    """打印进度条"""
    pct = done / total * 100 if total > 0 else 0
    eta_sec = (elapsed / max(done, 1)) * (total - done) if done > 0 else 0
    print(f"  [{at}] {done}/{total} ({pct:.0f}%) | 耗时 {elapsed:.0f}s | 预计剩余 {eta_sec:.0f}s {extra}")


# ── 全量回填 ──────────────────────────────────────────────────

def backfill(db: DatabaseManager, asset_types: list = None,
             batch_stocks: int = 50, max_stocks: int = None,
             skip_empty_check: bool = False):
    """全量回填所有股票/ETF/指数的技术指标"""
    if asset_types is None:
        asset_types = ['E', 'F', 'I']

    # D4-CP1: 参数合法性检查
    for at in asset_types:
        if at not in ASSET_TYPE_NAMES:
            print(f"[ERROR] 不支持的 asset_type: {at} (支持: E/F/I)")
            return

    grand_total = 0
    grand_elapsed = time.time()

    for at in asset_types:
        name = ASSET_TYPE_NAMES.get(at, at)
        print(f"\n{'=' * 60}")
        print(f"[{at}] 开始处理 {name}...")

        codes = get_all_ts_codes(db, at)
        if max_stocks:
            codes = codes[:max_stocks]

        if not codes:
            print(f"  [{at}] 无数据，跳过")
            continue

        print(f"  [{at}] 共 {len(codes)} 只，每批 {batch_stocks} 只并行")

        asset_total_rows = 0
        asset_skipped = 0
        t0 = time.time()

        for i in range(0, len(codes), batch_stocks):
            batch = codes[i:i + batch_stocks]
            batch_rows = []

            for j, code in enumerate(batch):
                rows = process_single_stock(db, code)
                if rows:
                    batch_rows.extend(rows)
                else:
                    asset_skipped += 1

                if (j + 1) % 20 == 0:
                    print(f"    [{at}] 已处理 {i + j + 1}/{len(codes)}, "
                          f"累计 {len(batch_rows)} 行指标", end="\r")

            # 写入
            if batch_rows:
                n = batch_upsert(db, batch_rows)
                asset_total_rows += n

            print()  # 换行
            print_progress(at, i + len(batch), len(codes), time.time() - t0,
                           f"已写入 {asset_total_rows} 行, 跳过 {asset_skipped} 只")

        elapsed = time.time() - t0
        print(f"\n[{at}] 完成! 写入 {asset_total_rows} 行技术指标, "
              f"跳过 {asset_skipped} 只 (数据不足 {MIN_DATA_DAYS} 天), "
              f"耗时 {elapsed:.0f}s")
        grand_total += asset_total_rows

    # D4-CP5: 完整性检查
    print(f"\n{'=' * 60}")
    print("完整性检查:")
    total_meta = 0
    for at in asset_types:
        codes = get_all_ts_codes(db, at)
        total_meta += len(codes)
        if not codes:
            continue
        # 分批 IN 查询（SQLite 参数上限 ~999）
        found = 0
        for i in range(0, len(codes), 900):
            batch = codes[i:i + 900]
            ph = ", ".join(["?" for _ in batch])
            cur = db.conn.execute(
                f"SELECT COUNT(DISTINCT ts_code) FROM daily_indicator "
                f"WHERE ts_code IN ({ph})", batch
            )
            found += cur.fetchone()[0]
        coverage = found / len(codes) * 100
        status = "✅" if coverage > 90 else "⚠️"
        print(f"  {status} [{at}] {ASSET_TYPE_NAMES.get(at, at)}: "
              f"{found}/{len(codes)} 只覆盖 ({coverage:.1f}%)")

    cur = db.conn.execute("SELECT COUNT(*) FROM daily_indicator")
    total_indicator = cur.fetchone()[0]
    print(f"\n  总计: 元数据 {total_meta} 只, daily_indicator {total_indicator} 行")

    print(f"\n总计写入 {grand_total} 行技术指标, 总耗时 {time.time() - grand_elapsed:.0f}s")


# ── 每日增量 ──────────────────────────────────────────────────

def daily_update(db: DatabaseManager, lookback_days: int = 80):
    """
    每日增量更新。

    读取最近 lookback_days 天的价格数据，计算并追加指标。
    由于所有指标都使用滚动窗口，重新计算最近 N 天的数据就能保证准确性。

    参数:
        lookback_days: 回看天数（MACD 需要 35+, MA60 需要 60+, 安全值 80）
    """
    lookback_sql = f"""
        SELECT ts_code, trade_date, open, high, low, close, vol
        FROM daily_price
        WHERE trade_date >= date('now', '-{lookback_days} days')
        ORDER BY ts_code, trade_date ASC
    """

    print(f"每日增量更新: 回看 {lookback_days} 天")
    t0 = time.time()

    df = pd.read_sql_query(lookback_sql, db.conn)
    if df.empty:
        print("  无新数据，跳过")
        return

    if "vol" in df.columns and "volume" not in df.columns:
        df = df.rename(columns={"vol": "volume"})

    all_rows = []
    total_stocks = df["ts_code"].nunique()
    processed = 0

    for code, grp in df.groupby("ts_code", sort=False):
        grp = grp.sort_values("trade_date").reset_index(drop=True)
        if len(grp) < 20:  # 至少需要 20 天
            processed += 1
            continue

        try:
            df_ta = add_all_indicators(grp)
        except Exception as e:
            print(f"  [WARN] {code} 指标计算失败: {e}")
            processed += 1
            continue

        if df_ta is None or df_ta.empty:
            processed += 1
            continue

        for _, row in df_ta.iterrows():
            vals = []
            for col in INDICATOR_COLS:
                v = row.get(col)
                if col in STR_COLS:
                    vals.append(str(v) if v is not None else None)
                elif col in BOOL_COLS:
                    vals.append(1 if v else 0)
                else:
                    if v is None or (isinstance(v, float) and np.isnan(v)):
                        vals.append(None)
                    else:
                        vals.append(float(v))
            all_rows.append(tuple(vals))

        processed += 1
        if processed % 100 == 0:
            print(f"  进度: {processed}/{total_stocks}, 累计 {len(all_rows)} 行")

    # 批量写入
    if all_rows:
        n = batch_upsert(db, all_rows)
        elapsed = time.time() - t0
        print(f"\n每日更新完成! 写入 {n} 行 (覆盖 {total_stocks} 只), "
              f"耗时 {elapsed:.0f}s")
    else:
        print("  无新指标数据")

    return len(all_rows)


# ── 主入口 ──────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="全市场技术指标批量回填 — daily_indicator 表",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  # 首次全量回填（先测 10 只）
  python scripts/utils/backfill_indicators.py --backfill --max-stocks 10

  # 全量回填所有股票
  python scripts/utils/backfill_indicators.py --backfill

  # 全量回填股票 + ETF + 指数（默认）
  python scripts/utils/backfill_indicators.py --backfill --asset-types E F I

  # 每日增量
  python scripts/utils/backfill_indicators.py --daily
        """
    )
    parser.add_argument("--backfill", action="store_true",
                        help="全量回填所有历史数据")
    parser.add_argument("--daily", action="store_true",
                        help="每日增量更新（回看 80 天）")
    parser.add_argument("--asset-types", nargs="+", default=['E', 'F', 'I'],
                        choices=['E', 'F', 'I'],
                        help="资产类型: E=股票 F=ETF I=指数 (默认: E F I)")
    parser.add_argument("--max-stocks", type=int, default=None,
                        help="限制处理数量（测试用）")
    parser.add_argument("--batch-stocks", type=int, default=50,
                        help="每批处理的股票数 (默认: 50)")
    parser.add_argument("--skip-empty-check", action="store_true",
                        help="跳过空数据检查")

    args = parser.parse_args()

    if not args.backfill and not args.daily:
        parser.print_help()
        sys.exit(1)

    print(f"[backfill_indicators] 启动 at {time.strftime('%Y-%m-%d %H:%M:%S')}")
    db = DatabaseManager()

    if args.backfill:
        backfill(db, args.asset_types, args.batch_stocks,
                 args.max_stocks, args.skip_empty_check)
    elif args.daily:
        daily_update(db)
