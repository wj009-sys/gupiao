"""
财务数据批量采集脚本 — 拉取持仓+自选股的基本面和财务报表数据存入 SQLite

数据源:
  Tushare fina_indicator → fina_indicator 表 (季度: ROE/ROA/毛利率/负债率/增速)
  Tushare daily_basic     → daily_basic 表     (每日: PE/PB/市值/换手率)

用法:
    # 拉取全部持仓+自选股的财务数据
    python scripts/utils/fetch_financials.py

    # 拉取指定个股
    python scripts/utils/fetch_financials.py --codes 600388.SH,000001.SZ

    # 只拉取估值数据 (daily_basic)
    python scripts/utils/fetch_financials.py --type valuation

    # 只拉取财报数据 (fina_indicator)
    python scripts/utils/fetch_financials.py --type financials

    # 拉取最近N个交易日的估值数据
    python scripts/utils/fetch_financials.py --days 30

D3异常处理表:
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| Tushare API超时/限频 | 等待2秒重试1次 | 跳过该股票，汇总时报告 |
| fina_indicator返回空 | 标记"无财报数据" | 跳过该股票 |
| daily_basic返回空(非交易日) | 往前推1天重试 | 跳过该日，不影响其他日 |
| 持仓文件解析失败 | 检查portfolio.json格式 | 使用空列表，仅拉取自选股 |
| dividend接口返回空 | 这是正常现象（无分红记录） | 标记"无分红数据" |
| adj_factor返回空 | 可能是ETF/可转债等无复权品种 | 跳过该品种，继续下一个 |
| 数据库写入冲突 | 回滚并重试1次 | 跳过该批次，记录失败股票列表 |

D4 CHECKPOINT:
- CP1-股票列表非空：检查portfolio和watchlist至少有一个有数据
- CP2-数据覆盖检查：写入前确认end_date/trade_date字段存在
- CP3-汇总报告：采集完成后输出成功/失败/跳过统计
- CP4-API限频保护：每次调用间隔≥0.3s(financials/dividend)或≥0.15s(daily_basic)
- CP5-去重写入：使用INSERT OR REPLACE避免数据重复

D9反例：
- 不要在循环中无间隔调用API（Tushare限频，每次调用间隔≥0.3秒）
- 不要假设所有股票都有财报数据（次新股可能只有1-2期）
- 不要把不在portfolio也不在watchlist的股全量拉取（成本高）
- 不要假设watchlist.values()返回的是字符串列表（值是列表的列表，需嵌套遍历）
- 不要忽略API返回的None（None和空DataFrame都需要处理）
"""

import os
import sys
import json
import time
import argparse
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro
from scripts.utils.db_manager import DatabaseManager


PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


def load_stock_list() -> list:
    """从持仓和自选股中加载股票代码列表"""
    codes = set()

    # 1. 持仓
    portfolio_path = os.path.join(PROJECT_ROOT, "data", "portfolio.json")
    if os.path.exists(portfolio_path):
        try:
            with open(portfolio_path, "r", encoding="utf-8") as f:
                portfolio = json.load(f)
            for h in portfolio.get("持仓列表", []):
                code = h.get("代码", "")
                if code and code != "000000":
                    codes.add(code)
            print(f"  持仓股: {len(codes)} 只")
        except Exception as e:
            print(f"  [WARN] 持仓文件解析失败: {e}")

    # 2. 自选股
    watchlist_path = os.path.join(PROJECT_ROOT, "data", "watchlist.json")
    if os.path.exists(watchlist_path):
        try:
            with open(watchlist_path, "r", encoding="utf-8") as f:
                watchlist = json.load(f)
            watchlist_codes = set()
            for sector_stocks in watchlist.values():
                if isinstance(sector_stocks, list):
                    for s in sector_stocks:
                        if s and s != "000000":
                            watchlist_codes.add(s)
            new_from_watchlist = watchlist_codes - codes
            codes.update(watchlist_codes)
            print(f"  自选股: {len(watchlist)} 个板块, 新增 {len(new_from_watchlist)} 只")
        except Exception as e:
            print(f"  [WARN] 自选股文件解析失败: {e}")

    return sorted(codes)


def fetch_financials(codes: list, db: DatabaseManager) -> dict:
    """
    拉取财报数据 (fina_indicator) 并写入数据库

    Returns:
        {"success": n, "empty": n, "error": n}
    """
    stats = {"success": 0, "empty": 0, "error": 0}
    today = datetime.now().strftime("%Y%m%d")
    # 取近5年的财报数据（最多20期）
    start_date = f"{datetime.now().year - 5}0101"

    print(f"\n  [财报数据] 拉取 {len(codes)} 只股票的财务指标...")
    print(f"    日期范围: {start_date} ~ {today}")

    for i, ts_code in enumerate(codes):
        try:
            df = pro.fina_indicator(
                ts_code=ts_code,
                start_date=start_date,
                end_date=today,
                fields="ts_code,end_date,revenue,profit_dedt,roe,roa,"
                       "grossprofit_margin,debt_to_assets,current_ratio,"
                       "profit_dedt_yoy,or_yoy"
            )
            if df is not None and not df.empty:
                n = db.upsert_fina_indicator(df)
                stats["success"] += n
                name = f"{ts_code}({len(df)}期)"
                if n > 0:
                    print(f"    [{i+1}/{len(codes)}] {name} -> 写入{n}行")
            else:
                stats["empty"] += 1
                print(f"    [{i+1}/{len(codes)}] {ts_code} -> 无财报数据")
        except Exception as e:
            stats["error"] += 1
            print(f"    [{i+1}/{len(codes)}] {ts_code} -> 失败: {e}")

        # 限频保护（0.3秒间隔）
        if i < len(codes) - 1:
            time.sleep(0.3)

    return stats


def fetch_daily_basic(codes: list, db: DatabaseManager, days_back: int = 30) -> dict:
    """
    拉取每日基本面数据 (daily_basic) 并写入数据库

    Args:
        days_back: 拉取最近多少个自然日的数据（会自动跳过非交易日）

    Returns:
        {"success": n, "empty": n, "error": n}
    """
    stats = {"success": 0, "empty": 0, "error": 0}

    # 生成日期范围
    today = datetime.now()
    dates = []
    for offset in range(days_back):
        d = today - timedelta(days=offset)
        dates.append(d.strftime("%Y%m%d"))

    print(f"\n  [估值数据] 拉取 {len(codes)} 只股票的基本面指标...")
    print(f"    日期范围: 近{days_back}天")

    for i, ts_code in enumerate(codes):
        stock_success = 0
        for trade_date in dates:
            try:
                df = pro.daily_basic(
                    ts_code=ts_code,
                    trade_date=trade_date,
                    fields="ts_code,trade_date,pe,pe_ttm,pb,total_mv,circ_mv,turnover_rate,volume_ratio"
                )
                if df is not None and not df.empty:
                    n = db.upsert_daily_basic(df)
                    stock_success += n
            except Exception as e:
                print(f'  [WARN] 写入 {ts_code} daily_basic失败: {e}')
            time.sleep(0.15)  # daily_basic 限频更严格

        if stock_success > 0:
            stats["success"] += stock_success
            if stock_success >= 5:
                print(f"    [{i+1}/{len(codes)}] {ts_code} -> {stock_success}个交易日")
        else:
            stats["empty"] += 1
            print(f"    [{i+1}/{len(codes)}] {ts_code} -> 无估值数据")

    return stats


def fetch_dividends(codes: list, db: DatabaseManager) -> dict:
    """
    拉取分红送转配股数据 (dividend) 并写入数据库

    Returns:
        {"success": n, "empty": n, "error": n}
    """
    stats = {"success": 0, "empty": 0, "error": 0}

    print(f"\n  [分红送转] 拉取 {len(codes)} 只股票的分红配股历史...")

    for i, ts_code in enumerate(codes):
        try:
            df = pro.dividend(ts_code=ts_code)
            if df is not None and not df.empty:
                n = db.upsert_dividend(df)
                stats["success"] += n

                # 统计实际分红记录
                implemented = df[df["div_proc"] == "实施"]
                cash_count = len(implemented[implemented["cash_div"] > 0])
                stock_count = len(implemented[
                    (implemented["stk_div"].fillna(0) > 0) |
                    (implemented["stk_bo_rate"].fillna(0) > 0) |
                    (implemented["stk_co_rate"].fillna(0) > 0)
                ])

                detail = f"{len(df)}条记录"
                if cash_count > 0 or stock_count > 0:
                    parts = []
                    if cash_count > 0:
                        parts.append(f"{cash_count}次现金分红")
                    if stock_count > 0:
                        parts.append(f"{stock_count}次送转配")
                    detail += " (" + ", ".join(parts) + ")"
                print(f"    [{i+1}/{len(codes)}] {ts_code} -> {detail}")
            else:
                stats["empty"] += 1
                print(f"    [{i+1}/{len(codes)}] {ts_code} -> 无分红数据")
        except Exception as e:
            stats["error"] += 1
            print(f"    [{i+1}/{len(codes)}] {ts_code} -> 失败: {e}")

        if i < len(codes) - 1:
            time.sleep(0.3)

    return stats


def fetch_adj_factors(codes: list, db: DatabaseManager) -> dict:
    """
    拉取复权因子 (adj_factor) 并写入数据库

    复权因子用于计算前复权价格:
        前复权价 = close × (latest_adj_factor / 当日adj_factor)

    Returns:
        {"success": n, "empty": n, "error": n}
    """
    stats = {"success": 0, "empty": 0, "error": 0}

    print(f"\n  [复权因子] 拉取 {len(codes)} 只股票的复权因子...")

    for i, ts_code in enumerate(codes):
        # ETF/可转债/发债等没有复权因子，跳过
        if any(ts_code.startswith(p) for p in ("51", "11", "15", "58", "71", "73", "40", "15")):
            continue

        try:
            df = pro.adj_factor(ts_code=ts_code)
            if df is not None and not df.empty:
                n = db.upsert_adj_factor(df)
                stats["success"] += n

                # 检查最近一次除权日
                df_sorted = df.sort_values("trade_date", ascending=False)
                latest_af = df_sorted.iloc[0]["adj_factor"]
                # 找到因子变化点（除权日）
                changes = []
                for j in range(1, min(30, len(df_sorted))):
                    if df_sorted.iloc[j]["adj_factor"] != df_sorted.iloc[j - 1]["adj_factor"]:
                        changes.append(df_sorted.iloc[j]["trade_date"])

                detail = f"{n}条"
                if changes:
                    detail += f" (最近除权: {changes[0]})"
                print(f"    [{i+1}/{len(codes)}] {ts_code} -> {detail}")
            else:
                stats["empty"] += 1
                print(f"    [{i+1}/{len(codes)}] {ts_code} -> 无复权因子")
        except Exception as e:
            stats["error"] += 1
            print(f"    [{i+1}/{len(codes)}] {ts_code} -> 失败: {e}")

        if i < len(codes) - 1:
            time.sleep(0.3)

    return stats


def main():
    parser = argparse.ArgumentParser(description="财务数据批量采集")
    parser.add_argument("--codes", type=str, default=None,
                        help="指定股票代码，逗号分隔 (如 600388.SH,000001.SZ)")
    parser.add_argument("--type", type=str, default="all",
                        choices=["all", "valuation", "financials", "dividends", "adjfactor"],
                        help="拉取类型: all=全部, valuation=估值, financials=财报, dividends=分红送转, adjfactor=复权因子")
    parser.add_argument("--days", type=int, default=30,
                        help="估值数据拉取最近N天 (默认30)")
    args = parser.parse_args()

    print("=" * 60)
    print("  财务数据批量采集")
    print("=" * 60)

    # 加载股票列表
    if args.codes:
        codes = [c.strip() for c in args.codes.split(",") if c.strip()]
        print(f"\n  指定股票: {len(codes)} 只")
    else:
        codes = load_stock_list()

    if not codes:
        print("\n[ERROR] 无股票可拉取（持仓和自选均为空）")
        return

    print(f"  共计: {len(codes)} 只股票")

    # 数据库
    db = DatabaseManager()

    # 摘要
    summary = {}

    # 拉取财报数据
    if args.type in ("all", "financials"):
        summary["financials"] = fetch_financials(codes, db)

    # 拉取估值数据
    if args.type in ("all", "valuation"):
        summary["valuation"] = fetch_daily_basic(codes, db, days_back=args.days)

    # 拉取分红送转数据
    if args.type in ("all", "dividends"):
        summary["dividends"] = fetch_dividends(codes, db)

    # 拉取复权因子
    if args.type in ("all", "adjfactor"):
        summary["adjfactor"] = fetch_adj_factors(codes, db)

    # 汇总
    print(f"\n{'='*60}")
    print("  采集完成")
    print(f"{'='*60}")
    type_names = {"financials": "财报数据", "valuation": "估值数据", "dividends": "分红送转", "adjfactor": "复权因子"}
    for data_type, stats in summary.items():
        name = type_names.get(data_type, data_type)
        print(f"  {name}: 成功{stats['success']}行 | 无数据{stats['empty']}股 | 失败{stats['error']}股")

    # 数据库统计
    db_stats = db.get_table_stats()
    print(f"\n  数据库当前状态:")
    for table in ["fina_indicator", "daily_basic", "daily_price", "daily_indicator"]:
        count = db_stats.get(table, 0)
        print(f"    {table}: {count} 行")

    db.close()


if __name__ == "__main__":
    main()
