"""
全量历史数据拉取脚本 — 从 Tushare 拉取所有持仓+自选股的全部历史数据

拉取范围:
  1. stock_basic     — 股票基础信息
  2. daily_price     — 全部历史日线（从上市日起）
  3. adj_factor      — 全部历史复权因子
  4. daily_basic     — 全部历史每日估值（PE/PB/市值）
  5. fina_indicator  — 全部历史财报（季度）
  6. dividend        — 全部历史分红送转配股
  7. 大盘指数          — 上证/深证/创业板/科创50全部历史日线
  8. portfolio_snapshot — 当前持仓快照
  9. watchlist        — 自选股列表

用法:
    # 拉取全部历史数据
    python scripts/utils/fetch_all_history.py

    # 只拉取日线行情
    python scripts/utils/fetch_all_history.py --type daily_price

    # 只拉取指定股票
    python scripts/utils/fetch_all_history.py --codes 600388.SH,000001.SZ

    # 增量模式（只拉取数据库中缺失的日期）
    python scripts/utils/fetch_all_history.py --incremental

D3异常处理表:
| 触发条件 | 一线修复 | 仍失败兜底 |
|---------|---------|-----------|
| Tushare API超时/限频 | 等待3秒重试1次 | 跳过该股票，汇总时报告 |
| API返回空数据（股票不存在/退市）| 标记"无数据"，继续下一个 | 汇总时列出跳过清单 |
| API返回空数据（非交易日）| 正常，不报错 | 继续处理 |
| 数据库写入失败 | 打印警告 | 继续处理下一个 |
| 持仓文件解析失败 | 打印警告 | 使用空列表 |

D4 CHECKPOINT:
- CP1-股票列表非空：至少从portfolio或watchlist加载到股票
- CP2-数据覆盖检查：每批写入前确认trade_date字段存在
- CP3-汇总报告：采集完成输出各类型成功/失败/跳过统计
- CP4-数据库对比：拉取完成后对比前后行数变化

D9反例：
- 不要在循环中无间隔调用API（Tushare限频，每日线间隔≥0.2s，财报≥0.3s）
- 不要假设所有股票都有全部类型数据（ETF无财报、债券无估值）
- 不要拉取不在portfolio也不在watchlist的全市场股票（成本高）
- 不要在一次调用中请求超过5000条（Tushare单次上限）
"""

import os
import sys
import json
import time
import argparse
import pandas as pd
import numpy as np
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro
from scripts.utils.db_manager import DatabaseManager

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))

# === 主要大盘指数 ===
MAJOR_INDICES = [
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
    ("000016.SH", "上证50"),
    ("000300.SH", "沪深300"),
    ("399905.SZ", "中证500"),
    ("000852.SH", "中证1000"),
]

# === 全局统计 ===
stats_global = {
    "daily_price": {"success": 0, "empty": 0, "error": 0, "rows": 0},
    "adj_factor": {"success": 0, "empty": 0, "error": 0, "rows": 0},
    "daily_basic": {"success": 0, "empty": 0, "error": 0, "rows": 0},
    "fina_indicator": {"success": 0, "empty": 0, "error": 0, "rows": 0},
    "dividend": {"success": 0, "empty": 0, "error": 0, "rows": 0},
    "stock_basic": {"success": 0, "error": 0},
    "index_daily": {"success": 0, "error": 0, "rows": 0},
}


def load_all_stock_codes() -> list:
    """从持仓+自选股加载所有股票代码（去重）"""
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
            print(f"  持仓股: {len([h for h in portfolio.get('持仓列表', []) if h.get('代码', '') != '000000'])} 只")
        except Exception as e:
            print(f"  [WARN] 持仓文件解析失败: {e}")

    # 2. 自选股
    watchlist_path = os.path.join(PROJECT_ROOT, "data", "watchlist.json")
    if os.path.exists(watchlist_path):
        try:
            with open(watchlist_path, "r", encoding="utf-8") as f:
                watchlist = json.load(f)
            for sector, stocks in watchlist.items():
                if sector in ("说明", "更新日期", "数据来源"):
                    continue
                if isinstance(stocks, list):
                    for s in stocks:
                        if s and s != "000000":
                            codes.add(s)
            print(f"  自选股板块: {len([k for k in watchlist if k not in ('说明','更新日期','数据来源')])} 个")
        except Exception as e:
            print(f"  [WARN] 自选股文件解析失败: {e}")

    return sorted(codes)


CHECKPOINT_DIR = os.path.join(PROJECT_ROOT, "data", "checkpoints")


def load_all_market_codes(db: DatabaseManager = None) -> list:
    """
    从 Tushare 加载全市场上市股票代码（不含债券、老三板等）

    Returns:
        排序后的股票代码列表
    """
    print("\n  [全市场模式] 从 Tushare 拉取全部上市股票列表...")
    try:
        df_all = pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,name,market,industry,list_status,list_date"
        )
        if df_all is None or df_all.empty:
            print("  [ERROR] stock_basic 返回空数据")
            return []

        # 只保留沪深两市（排除北交所 8/4 开头）
        df_filtered = df_all[df_all["ts_code"].str.match(r'^(0[01236]|3|6)\d{5}\.[A-Z]{2}$')].copy()

        codes = sorted(df_filtered["ts_code"].unique())
        print(f"  全市场上市股票: {len(codes)} 只 (已排除北交所/债券)")
        print(f"  其中沪市: {len([c for c in codes if c.endswith('.SH')])} 只")
        print(f"  其中深市: {len([c for c in codes if c.endswith('.SZ')])} 只")

        # 写入 stock_basic 表
        if db and db._ensure_conn():
            n = db.update_stock_basic(df_filtered)
            print(f"  stock_basic 表已更新: {n} 行")

        return codes
    except Exception as e:
        print(f"  [ERROR] 加载全市场股票列表失败: {e}")
        return []


def load_checkpoint(data_type: str) -> set:
    """加载已处理股票列表（断点续传）"""
    ckpt_path = os.path.join(CHECKPOINT_DIR, f"{data_type}_done.json")
    if os.path.exists(ckpt_path):
        try:
            with open(ckpt_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return set(data.get("done", []))
        except Exception:
            pass
    return set()


def save_checkpoint(data_type: str, done_codes: set):
    """保存已处理股票列表"""
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    ckpt_path = os.path.join(CHECKPOINT_DIR, f"{data_type}_done.json")
    try:
        with open(ckpt_path, "w", encoding="utf-8") as f:
            json.dump({
                "data_type": data_type,
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "count": len(done_codes),
                "done": sorted(done_codes),
            }, f, ensure_ascii=False)
    except Exception as e:
        print(f"    [WARN] 保存断点失败: {e}")


def get_db_existing_codes(db: DatabaseManager, table: str = "daily_price") -> set:
    """获取数据库中已有数据的股票代码集合"""
    if not db or not db._ensure_conn():
        return set()
    try:
        cur = db.conn.cursor()
        cur.execute(f"SELECT DISTINCT ts_code FROM {table}")
        return set(row[0] for row in cur.fetchall())
    except Exception:
        return set()


def classify_stocks(codes: list) -> dict:
    """
    将股票按类型分类，因为不同市场有不同的API行为

    Returns:
        {"stocks": [...], "etfs": [...], "bonds": [...], "other": [...]}
    """
    result = {"stocks": [], "etfs": [], "bonds": [], "other": []}

    for code in codes:
        prefix = code[:3]
        exchange = code[-2:] if len(code) > 2 else ""

        # ETF: 51xxxx.SH, 159xxx.SZ, 588xxx.SH
        if code.startswith("51") or code.startswith("159") or code.startswith("588"):
            result["etfs"].append(code)
        # 可转债: 11xxxx, 12xxxx (部分)
        elif code.startswith("11") and len(code) > 6:
            result["bonds"].append(code)
        # 发债(待上市): 71xxxx, 73xxxx
        elif code.startswith("71") or code.startswith("73"):
            result["bonds"].append(code)
        # 老三板: 400xxx
        elif code.startswith("400"):
            result["other"].append(code)
        else:
            result["stocks"].append(code)

    return result


def fetch_stock_basic(codes: list, db: DatabaseManager):
    """拉取股票基础信息"""
    global stats_global
    print(f"\n{'='*60}")
    print(f"  [1/7] 股票基础信息 (stock_basic)")
    print(f"{'='*60}")

    # 先尝试从Tushare拉取全量股票列表
    try:
        df_all = pro.stock_basic(
            exchange="",
            list_status="L",
            fields="ts_code,name,market,industry,list_status,list_date"
        )
        if df_all is not None and not df_all.empty:
            # 只保留我们关注的那些
            df_our = df_all[df_all["ts_code"].isin(codes)].copy()
            if not df_our.empty:
                n = db.update_stock_basic(df_our)
                stats_global["stock_basic"]["success"] = n
                # 打印名称映射
                name_map = dict(zip(df_our["ts_code"], df_our["name"]))
                for code in codes:
                    name = name_map.get(code, "未知")
                    print(f"    {code} → {name}")
            else:
                print(f"    [WARN] 在Tushare全量列表中未找到匹配股票")

        # 对于在Tushare找不到的代码（ETF、债券等），手动插入基础信息
        missing = []
        for code in codes:
            if df_all is None or code not in df_all["ts_code"].values:
                # 从portfolio.json获取名称
                missing.append(code)

        if missing:
            print(f"\n    手动补充 {len(missing)} 只未在Tushare找到的代码...")
            # 尝试为每个缺失代码构造基础信息
            manual_rows = []
            for code in missing:
                manual_rows.append({
                    "ts_code": code,
                    "name": code,
                    "market": code[-2:] if len(code) > 2 else "",
                    "industry": "其他",
                    "list_status": "L",
                    "list_date": "",
                })
            if manual_rows:
                df_manual = pd.DataFrame(manual_rows)
                db.update_stock_basic(df_manual)
                print(f"    已补充 {len(manual_rows)} 条")

    except Exception as e:
        print(f"    [ERROR] stock_basic 拉取失败: {e}")
        stats_global["stock_basic"]["error"] += 1


def fetch_daily_price_history(codes: list, db: DatabaseManager, incremental: bool = False,
                                all_market: bool = False, resume: bool = False):
    """
    拉取全部历史日线行情

    对每只股票调用 pro.daily() 拉取从上市至今的全部日线数据。
    一次性调用返回全部历史（Tushare Pro 无分页限制）。

    Args:
        codes: 股票代码列表
        db: 数据库管理器
        incremental: 增量模式
        all_market: 全市场模式（启用断点续传、自动跳过已有数据）
        resume: 从断点恢复
    """
    global stats_global
    print(f"\n{'='*60}")
    print(f"  [2/7] 日线行情 (daily_price) — {len(codes)} 只证券")
    if all_market:
        print(f"  模式: 全市场 (断点续传已启用)")
    print(f"{'='*60}")

    # === 断点续传 ===
    ckpt_done = set()
    if all_market:
        if resume:
            ckpt_done = load_checkpoint("daily_price")
            print(f"  断点恢复: 已完成 {len(ckpt_done)} 只")
        elif not resume:
            # 首次全量运行，清空旧断点
            ckpt_path = os.path.join(CHECKPOINT_DIR, "daily_price_done.json")
            if os.path.exists(ckpt_path):
                os.remove(ckpt_path)

        # 检查DB中已有数据的股票
        db_existing = get_db_existing_codes(db, "daily_price")
        print(f"  数据库中已有: {len(db_existing)} 只股票")
        ckpt_done = ckpt_done | db_existing

    # 如果增量模式，获取每只股票在DB中已有数据的日期范围
    existing_dates = {}
    if incremental and not all_market and db and db._ensure_conn():
        try:
            cur = db.conn.cursor()
            for code in codes:
                cur.execute(
                    "SELECT MIN(trade_date), MAX(trade_date) FROM daily_price WHERE ts_code = ?",
                    (code,)
                )
                row = cur.fetchone()
                if row and row[0]:
                    existing_dates[code] = (row[0], row[1])
        except Exception:
            pass

    start_date = "19900101"  # 足够早的日期
    end_date = datetime.now().strftime("%Y%m%d")

    skipped_count = 0
    last_ckpt_save = 0
    CKPT_INTERVAL = 100  # 每100只保存一次断点

    for i, code in enumerate(codes):
        # 全市场模式：跳过已有数据的股票
        if all_market and code in ckpt_done:
            skipped_count += 1
            if skipped_count % 500 == 0:
                print(f"    已跳过 {skipped_count} 只 (已有数据)...")
            continue

        try:
            # 增量模式：如果已有较完整数据，只拉缺失部分
            if incremental and not all_market and code in existing_dates:
                db_start, db_end = existing_dates[code]
                db_end_dt = datetime.strptime(db_end, "%Y%m%d")
                if (datetime.now() - db_end_dt).days <= 7:
                    print(f"    [{i+1}/{len(codes)}] {code} → 已完整 (覆盖 {db_start}~{db_end})，跳过")
                    continue
                actual_start = (datetime.strptime(db_end, "%Y%m%d") + timedelta(days=1)).strftime("%Y%m%d")
                print(f"    [{i+1}/{len(codes)}] {code} → 增量 (已有 {db_start}~{db_end})，补拉 {actual_start}~{end_date}")
            else:
                actual_start = start_date

            df = pro.daily(ts_code=code, start_date=actual_start, end_date=end_date)

            if df is not None and not df.empty:
                n = db.upsert_daily_price(df, asset_type='E')
                stats_global["daily_price"]["rows"] += n
                stats_global["daily_price"]["success"] += 1
                date_min = df["trade_date"].min()
                date_max = df["trade_date"].max()
                if all_market:
                    # 全市场模式：精简输出
                    print(f"    [{i+1-skipped_count}/{len(codes)-skipped_count}] {code} → {n}行 ({date_min[:4]}~{date_max[:4]})")
                else:
                    print(f"    [{i+1}/{len(codes)}] {code} → {n}行 ({date_min}~{date_max})")
            else:
                stats_global["daily_price"]["empty"] += 1
                print(f"    [{i+1}/{len(codes)}] {code} → 无日线数据")

            # 断点：每处理成功一只就加入
            if all_market:
                ckpt_done.add(code)

        except Exception as e:
            stats_global["daily_price"]["error"] += 1
            print(f"    [{i+1}/{len(codes)}] {code} → 失败: {e}")
            # 失败的也加入断点（不再重试，最后汇总报告）
            if all_market:
                ckpt_done.add(code)

        # 断点保存
        if all_market and (len(ckpt_done) - last_ckpt_save >= CKPT_INTERVAL):
            save_checkpoint("daily_price", ckpt_done)
            last_ckpt_save = len(ckpt_done)
            elapsed_pct = len(ckpt_done) / len(codes) * 100 if codes else 0
            print(f"    💾 断点已保存 ({len(ckpt_done)}/{len(codes)}, {elapsed_pct:.1f}%)")

        # 限频保护（全市场模式用更保守的间隔）
        delay = 0.35 if all_market else 0.2
        if i < len(codes) - 1:
            time.sleep(delay)

    # 最终保存断点
    if all_market:
        save_checkpoint("daily_price", ckpt_done)
        print(f"\n  日线行情断点已保存: {len(ckpt_done)} 只")

    if skipped_count:
        print(f"  共跳过 {skipped_count} 只（已有数据）")


def fetch_adj_factor_history(codes: list, db: DatabaseManager, all_market: bool = False):
    """拉取全部历史复权因子"""
    global stats_global
    print(f"\n{'='*60}")
    print(f"  [3/7] 复权因子 (adj_factor) — {len(codes)} 只证券")
    print(f"{'='*60}")

    # 只对普通股票拉取复权因子（ETF/债券可能没有）
    stock_codes = [c for c in codes if not any(
        c.startswith(p) for p in ("51", "11", "15", "58", "71", "73", "40", "159", "588")
    )]

    # === 断点续传 ===
    ckpt_done = set()
    if all_market:
        ckpt_done = load_checkpoint("adj_factor")
        db_existing = get_db_existing_codes(db, "adj_factor")
        ckpt_done = ckpt_done | db_existing
        print(f"  数据库中已有: {len(db_existing)} 只")

    skipped_count = 0
    last_ckpt_save = len(ckpt_done)
    CKPT_INTERVAL = 200

    for i, code in enumerate(stock_codes):
        if all_market and code in ckpt_done:
            skipped_count += 1
            continue

        try:
            df = pro.adj_factor(ts_code=code)
            if df is not None and not df.empty:
                n = db.upsert_adj_factor(df)
                stats_global["adj_factor"]["rows"] += n
                stats_global["adj_factor"]["success"] += 1

                detail = f"{n}行"
                if all_market and (i - skipped_count) % 50 == 0:
                    print(f"    [{i+1-skipped_count}/{len(stock_codes)-skipped_count}] {code} → {detail}")
            else:
                stats_global["adj_factor"]["empty"] += 1

            if all_market:
                ckpt_done.add(code)

        except Exception as e:
            stats_global["adj_factor"]["error"] += 1
            if all_market:
                ckpt_done.add(code)

        # 断点保存
        if all_market and (len(ckpt_done) - last_ckpt_save >= CKPT_INTERVAL):
            save_checkpoint("adj_factor", ckpt_done)
            last_ckpt_save = len(ckpt_done)

        delay = 0.3 if all_market else 0.25
        if i < len(stock_codes) - 1:
            time.sleep(delay)

    if all_market:
        save_checkpoint("adj_factor", ckpt_done)
        print(f"\n  复权因子断点已保存: {len(ckpt_done)} 只")
    if skipped_count:
        print(f"  共跳过 {skipped_count} 只（已有数据）")


def fetch_daily_basic_history(codes: list, db: DatabaseManager, all_market: bool = False):
    """
    拉取全部历史每日估值数据

    非全市场模式：逐股×逐年拉取
    全市场模式：按交易日批量拉取（速度提升 ~20倍）
    """
    if all_market:
        return _fetch_daily_basic_by_date(db)
    return _fetch_daily_basic_by_stock(codes, db)


def _fetch_daily_basic_by_date(db: DatabaseManager):
    """
    全市场优化：按交易日批量拉取 daily_basic

    原理：Tushare daily_basic API 支持 trade_date 参数，一次调用返回全市场
          所有股票的当日估值数据。比逐股拉取快 ~20 倍。

    数据源：从 daily_price 表获取所有交易日，逐日拉取
    """
    global stats_global
    print(f"\n{'='*60}")
    print(f"  [4/7] 每日估值 (daily_basic) — 全市场(按日期批量)")
    print(f"{'='*60}")

    # 1. 获取 daily_price 中所有交易日期
    if not db._ensure_conn():
        return

    cur = db.conn.cursor()
    cur.execute("SELECT DISTINCT trade_date FROM daily_price WHERE trade_date >= '20100101' ORDER BY trade_date")
    all_dates = [row[0] for row in cur.fetchall()]
    print(f"  待拉取交易日: {len(all_dates)} 天")

    # 2. 加载已有日期（断点续传）
    ckpt_done_dates = set()
    ckpt_path = os.path.join(CHECKPOINT_DIR, "daily_basic_dates_done.json")
    if os.path.exists(ckpt_path):
        ckpt_done_dates = load_checkpoint("daily_basic_dates")
        print(f"  断点恢复: 已完成 {len(ckpt_done_dates)} 天")

    # 同时检查DB中已有数据的日期（覆盖率>2000只才算已完成）
    try:
        cur.execute("""SELECT trade_date, COUNT(*) as cnt FROM daily_basic
                       GROUP BY trade_date HAVING cnt >= 2000""")
        db_dates_full = set(row[0] for row in cur.fetchall())
        ckpt_done_dates = ckpt_done_dates | db_dates_full
        cur.execute("SELECT COUNT(DISTINCT trade_date) FROM daily_basic")
        total_db_dates = cur.fetchone()[0]
        print(f"  数据库中: {total_db_dates} 天 (覆盖率≥2000只: {len(db_dates_full)} 天)")
    except Exception:
        pass

    # 过滤
    dates_to_fetch = [d for d in all_dates if d not in ckpt_done_dates]
    print(f"  实际需拉取: {len(dates_to_fetch)} 天")

    if not dates_to_fetch:
        print("  所有日期已完成，跳过")
        return

    # 3. 逐日拉取
    total_rows = 0
    success_days = 0
    error_days = 0
    empty_days = 0
    last_ckpt_save = len(ckpt_done_dates)
    CKPT_INTERVAL = 250  # 每250天保存一次断点
    start_time = time.time()

    for i, trade_date in enumerate(dates_to_fetch):
        try:
            df = pro.daily_basic(
                trade_date=trade_date,
                fields="ts_code,trade_date,pe,pe_ttm,pb,total_mv,circ_mv,turnover_rate,volume_ratio"
            )

            if df is not None and not df.empty:
                n = db.upsert_daily_basic(df)
                total_rows += n
                success_days += 1

                if (i + 1) % 200 == 0:
                    elapsed = time.time() - start_time
                    eta = elapsed / (i + 1) * len(dates_to_fetch) - elapsed
                    print(f"    [{i+1}/{len(dates_to_fetch)}] {trade_date} → {n}只 "
                          f"| 累计{total_rows:,}行 | ⏱{elapsed/60:.0f}m ETA{eta/60:.0f}m")
            else:
                empty_days += 1

            ckpt_done_dates.add(trade_date)

        except Exception as e:
            error_days += 1
            if error_days <= 5:
                print(f"    [{i+1}/{len(dates_to_fetch)}] {trade_date} → 失败: {e}")
            ckpt_done_dates.add(trade_date)  # 跳过失败的日期

        # 断点保存
        if len(ckpt_done_dates) - last_ckpt_save >= CKPT_INTERVAL:
            _save_date_checkpoint("daily_basic_dates", ckpt_done_dates)
            last_ckpt_save = len(ckpt_done_dates)

        time.sleep(0.12)

    # 最终保存
    _save_date_checkpoint("daily_basic_dates", ckpt_done_dates)

    stats_global["daily_basic"]["rows"] = total_rows
    stats_global["daily_basic"]["success"] = success_days
    stats_global["daily_basic"]["error"] = error_days

    elapsed_total = time.time() - start_time
    print(f"\n  每日估值完成: {success_days}天成功 {error_days}天失败 {empty_days}天空数据")
    print(f"  总行数: {total_rows:,} | 总耗时: {elapsed_total/60:.1f}分钟")


def _save_date_checkpoint(data_type: str, done_dates: set):
    """保存按日期维度的断点"""
    os.makedirs(CHECKPOINT_DIR, exist_ok=True)
    ckpt_path = os.path.join(CHECKPOINT_DIR, f"{data_type}_done.json")
    try:
        with open(ckpt_path, "w", encoding="utf-8") as f:
            json.dump({
                "data_type": data_type,
                "updated": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "count": len(done_dates),
                "done": sorted(done_dates),
            }, f, ensure_ascii=False)
    except Exception:
        pass


def _fetch_daily_basic_by_stock(codes: list, db: DatabaseManager):
    """
    逐股拉取每日估值（原有逻辑，用于非全市场模式）
    """
    global stats_global
    print(f"\n{'='*60}")
    print(f"  [4/7] 每日估值 (daily_basic) — {len(codes)} 只证券")
    print(f"{'='*60}")

    current_year = datetime.now().year
    years = list(range(2010, current_year + 1))

    for i, code in enumerate(codes):
        total_rows = 0
        had_error = False

        for year in years:
            try:
                start = f"{year}0101"
                end = f"{year}1231" if year < current_year else datetime.now().strftime("%Y%m%d")

                df = pro.daily_basic(
                    ts_code=code,
                    start_date=start,
                    end_date=end,
                    fields="ts_code,trade_date,pe,pe_ttm,pb,total_mv,circ_mv,turnover_rate,volume_ratio"
                )

                if df is not None and not df.empty:
                    n = db.upsert_daily_basic(df)
                    total_rows += n
                time.sleep(0.12)
            except Exception:
                had_error = True
                continue

        if total_rows > 0:
            stats_global["daily_basic"]["rows"] += total_rows
            stats_global["daily_basic"]["success"] += 1
            if i % 50 == 0:
                print(f"    [{i+1}/{len(codes)}] {code} → {total_rows}行")
        elif had_error:
            stats_global["daily_basic"]["error"] += 1
        else:
            stats_global["daily_basic"]["empty"] += 1


def fetch_financials_history(codes: list, db: DatabaseManager, all_market: bool = False):
    """拉取全部历史财报数据"""
    global stats_global
    print(f"\n{'='*60}")
    print(f"  [5/7] 财报数据 (fina_indicator) — {len(codes)} 只证券")
    print(f"{'='*60}")

    # === 断点续传 ===
    ckpt_done = set()
    if all_market:
        ckpt_done = load_checkpoint("fina_indicator")
        db_existing = get_db_existing_codes(db, "fina_indicator")
        ckpt_done = ckpt_done | db_existing
        print(f"  数据库中已有: {len(db_existing)} 只")

    today = datetime.now().strftime("%Y%m%d")
    start_date = "20000101"  # 覆盖所有历史

    skipped_count = 0
    last_ckpt_save = len(ckpt_done)
    CKPT_INTERVAL = 200

    for i, code in enumerate(codes):
        # ETF、债券等无财报数据，自动跳过
        if code.startswith("51") or code.startswith("159") or code.startswith("588"):
            continue
        if code.startswith("11") or code.startswith("71") or code.startswith("73"):
            continue
        if code.startswith("40"):
            continue

        if all_market and code in ckpt_done:
            skipped_count += 1
            continue

        try:
            df = pro.fina_indicator(
                ts_code=code,
                start_date=start_date,
                end_date=today,
                fields="ts_code,end_date,revenue,profit_dedt,roe,roa,"
                       "grossprofit_margin,debt_to_assets,current_ratio,"
                       "revenue_yoy,profit_dedt_yoy,or_yoy"
            )
            if df is not None and not df.empty:
                n = db.upsert_fina_indicator(df)
                stats_global["fina_indicator"]["rows"] += n
                stats_global["fina_indicator"]["success"] += 1
                if (not all_market) or (i - skipped_count) % 100 == 0:
                    print(f"    [{i+1-skipped_count}/{len(codes)-skipped_count}] {code} → {n}行 ({len(df)}期)")
            else:
                stats_global["fina_indicator"]["empty"] += 1

            if all_market:
                ckpt_done.add(code)

        except Exception as e:
            stats_global["fina_indicator"]["error"] += 1
            if all_market:
                ckpt_done.add(code)

        # 断点保存
        if all_market and (len(ckpt_done) - last_ckpt_save >= CKPT_INTERVAL):
            save_checkpoint("fina_indicator", ckpt_done)
            last_ckpt_save = len(ckpt_done)

        delay = 0.35 if all_market else 0.3
        if i < len(codes) - 1:
            time.sleep(delay)

    if all_market:
        save_checkpoint("fina_indicator", ckpt_done)
        print(f"\n  财报数据断点已保存: {len(ckpt_done)} 只")
    if skipped_count:
        print(f"  共跳过 {skipped_count} 只（已有数据）")


def fetch_dividends_history(codes: list, db: DatabaseManager, all_market: bool = False):
    """拉取全部历史分红送转数据"""
    global stats_global
    print(f"\n{'='*60}")
    print(f"  [6/7] 分红送转 (dividend) — {len(codes)} 只证券")
    print(f"{'='*60}")

    # === 断点续传 ===
    ckpt_done = set()
    if all_market:
        ckpt_done = load_checkpoint("dividend")
        db_existing = get_db_existing_codes(db, "dividend")
        ckpt_done = ckpt_done | db_existing
        print(f"  数据库中已有: {len(db_existing)} 只")

    skipped_count = 0
    last_ckpt_save = len(ckpt_done)
    CKPT_INTERVAL = 200

    for i, code in enumerate(codes):
        # 只对股票拉取分红数据
        if any(code.startswith(p) for p in ("51", "159", "588", "11", "71", "73", "40")):
            continue

        if all_market and code in ckpt_done:
            skipped_count += 1
            continue

        try:
            df = pro.dividend(ts_code=code)
            if df is not None and not df.empty:
                n = db.upsert_dividend(df)
                stats_global["dividend"]["rows"] += n
                stats_global["dividend"]["success"] += 1

                if (not all_market) or (i - skipped_count) % 200 == 0:
                    # 统计实施记录
                    implemented = df[df["div_proc"] == "实施"] if "div_proc" in df.columns else pd.DataFrame()
                    cash_count = len(implemented[implemented["cash_div"].fillna(0) > 0]) if not implemented.empty else 0
                    detail = f"{n}行"
                    if cash_count > 0:
                        detail += f" ({cash_count}次现金分红)"
                    print(f"    [{i+1-skipped_count}/{len(codes)-skipped_count}] {code} → {detail}")
            else:
                stats_global["dividend"]["empty"] += 1

            if all_market:
                ckpt_done.add(code)

        except Exception as e:
            stats_global["dividend"]["error"] += 1
            if all_market:
                ckpt_done.add(code)

        # 断点保存
        if all_market and (len(ckpt_done) - last_ckpt_save >= CKPT_INTERVAL):
            save_checkpoint("dividend", ckpt_done)
            last_ckpt_save = len(ckpt_done)

        delay = 0.35 if all_market else 0.3
        if i < len(codes) - 1:
            time.sleep(delay)

    if all_market:
        save_checkpoint("dividend", ckpt_done)
        print(f"\n  分红送转折点已保存: {len(ckpt_done)} 只")
    if skipped_count:
        print(f"  共跳过 {skipped_count} 只（已有数据）")


def fetch_index_daily_history(db: DatabaseManager):
    """
    拉取大盘指数全部历史日线

    对8个主要指数调用 pro.index_daily()，拉取全部历史数据
    """
    global stats_global
    print(f"\n{'='*60}")
    print(f"  [7/7] 大盘指数日线 — {len(MAJOR_INDICES)} 个指数")
    print(f"{'='*60}")

    start_date = "19900101"
    end_date = datetime.now().strftime("%Y%m%d")

    for i, (ts_code, name) in enumerate(MAJOR_INDICES):
        try:
            df = pro.index_daily(ts_code=ts_code, start_date=start_date, end_date=end_date)
            if df is not None and not df.empty:
                df = df.sort_values("trade_date").reset_index(drop=True)
                # 重命名列以确保匹配
                if "vol" in df.columns:
                    df = df.rename(columns={"vol": "volume"})
                n = db.upsert_daily_price(df, asset_type='I')
                stats_global["index_daily"]["rows"] += n
                stats_global["index_daily"]["success"] += 1
                date_min = df["trade_date"].min()
                date_max = df["trade_date"].max()
                print(f"    [{i+1}/{len(MAJOR_INDICES)}] {name} ({ts_code}) → {n}行 ({date_min}~{date_max})")
            else:
                stats_global["index_daily"]["error"] += 1
                print(f"    [{i+1}/{len(MAJOR_INDICES)}] {name} ({ts_code}) → 无数据")
        except Exception as e:
            stats_global["index_daily"]["error"] += 1
            print(f"    [{i+1}/{len(MAJOR_INDICES)}] {name} ({ts_code}) → 失败: {e}")

        if i < len(MAJOR_INDICES) - 1:
            time.sleep(0.2)


def save_portfolio_and_watchlist(db: DatabaseManager):
    """将当前持仓和自选股写入数据库"""
    print(f"\n{'='*60}")
    print(f"  [附] 持仓快照 + 自选股")
    print(f"{'='*60}")

    # 持仓快照
    portfolio_path = os.path.join(PROJECT_ROOT, "data", "portfolio.json")
    if os.path.exists(portfolio_path):
        try:
            with open(portfolio_path, "r", encoding="utf-8") as f:
                portfolio = json.load(f)
            holdings = portfolio.get("持仓列表", [])
            snap_date = portfolio.get("更新日期", datetime.now().strftime("%Y-%m-%d"))
            n = db.save_portfolio_snapshot(snap_date, holdings)
            print(f"    portfolio_snapshot: {n} 条 ({snap_date})")
        except Exception as e:
            print(f"    [WARN] 持仓快照写入失败: {e}")

    # 自选股
    watchlist_path = os.path.join(PROJECT_ROOT, "data", "watchlist.json")
    if os.path.exists(watchlist_path):
        try:
            with open(watchlist_path, "r", encoding="utf-8") as f:
                watchlist = json.load(f)
            today = datetime.now().strftime("%Y-%m-%d")
            rows = []
            for sector, stocks in watchlist.items():
                if sector in ("说明", "更新日期", "数据来源"):
                    continue
                if isinstance(stocks, list):
                    for s in stocks:
                        if s and s != "000000":
                            rows.append((s, sector, today))
            if rows:
                cur = db.conn.cursor()
                cur.executemany(
                    "INSERT OR REPLACE INTO watchlist (ts_code, sector, added_date) VALUES (?, ?, ?)",
                    rows
                )
                db.conn.commit()
                print(f"    watchlist: {len(rows)} 条")
        except Exception as e:
            print(f"    [WARN] 自选股写入失败: {e}")


def print_summary(stats_before: dict, stats_after: dict):
    """打印汇总报告"""
    print(f"\n{'='*70}")
    print(f"  全量历史数据拉取 — 完成")
    print(f"{'='*70}")

    type_names = {
        "daily_price": "📊 日线行情",
        "adj_factor": "🔄 复权因子",
        "daily_basic": "📈 每日估值",
        "fina_indicator": "💰 财报数据",
        "dividend": "💵 分红送转",
        "stock_basic": "📋 股票信息",
        "index_daily": "🏛️ 大盘指数",
    }

    print(f"\n  {'类型':<16} {'成功':>6} {'无数据':>6} {'失败':>6} {'写入行数':>10}")
    print(f"  {'-'*48}")

    for key, name in type_names.items():
        s = stats_global.get(key, {})
        succ = s.get("success", 0)
        empty = s.get("empty", 0)
        err = s.get("error", 0)
        rows = s.get("rows", 0)
        if succ > 0 or empty > 0 or err > 0:
            print(f"  {name:<16} {succ:>6} {empty:>6} {err:>6} {rows:>10,}")

    # 各表行数变化
    if stats_before:
        print(f"\n  {'表名':<24} {'拉取前':>10} {'拉取后':>10} {'增量':>10}")
        print(f"  {'-'*58}")
        for table in sorted(stats_after.keys()):
            before = stats_before.get(table, 0)
            after = stats_after.get(table, 0)
            delta = after - before
            if delta != 0:
                marker = " ✅ NEW" if before == 0 else ""
                print(f"  {table:<24} {before:>10,} {after:>10,} {delta:>+10,}{marker}")
            elif after > 0:
                print(f"  {table:<24} {before:>10,} {after:>10,}       (无变化)")

    # 数据日期范围
    try:
        db = DatabaseManager()
        dr_price = db.get_date_range("daily_price")
        dr_adj = db.get_date_range("adj_factor")
        dr_basic = db.get_date_range("daily_basic")
        db.close()
        print(f"\n  数据覆盖范围:")
        print(f"    日线行情: {dr_price[0]} ~ {dr_price[1]}")
        print(f"    复权因子: {dr_adj[0]} ~ {dr_adj[1]}")
        print(f"    每日估值: {dr_basic[0]} ~ {dr_basic[1]}")
    except Exception:
        pass

    print(f"\n  数据库文件: data/stocks.db")
    db_size_mb = os.path.getsize(os.path.join(PROJECT_ROOT, "data", "stocks.db")) / 1024 / 1024
    print(f"  数据库大小: {db_size_mb:.1f} MB")


def main():
    parser = argparse.ArgumentParser(description="全量历史数据拉取")
    parser.add_argument("--type", type=str, default="all",
                        choices=["all", "daily_price", "adj_factor", "daily_basic",
                                 "fina_indicator", "dividend", "stock_basic", "index_daily"],
                        help="拉取类型 (默认 all)")
    parser.add_argument("--codes", type=str, default=None,
                        help="指定股票代码，逗号分隔")
    parser.add_argument("--incremental", action="store_true",
                        help="增量模式：只拉取数据库缺失的数据")
    parser.add_argument("--skip-if-exists", action="store_true",
                        help="如果某类型已有数据则跳过")
    parser.add_argument("--all-market", action="store_true",
                        help="全市场模式：拉取全部A股上市股票（含断点续传）")
    parser.add_argument("--resume", action="store_true",
                        help="从断点恢复（配合 --all-market 使用）")
    args = parser.parse_args()

    print("=" * 70)
    print("  全量历史数据拉取")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    if args.all_market:
        mode = "全市场" if not args.resume else "全市场(断点恢复)"
    elif args.incremental:
        mode = "增量"
    else:
        mode = "全量"
    print(f"  模式: {mode}")
    print(f"  类型: {args.type}")
    print("=" * 70)

    # 数据库
    db = DatabaseManager()
    stats_before = db.get_table_stats()

    # 加载股票列表
    if args.codes:
        codes = sorted([c.strip() for c in args.codes.split(",") if c.strip()])
        all_market_mode = False
        print(f"\n  指定股票: {len(codes)} 只")
    elif args.all_market:
        codes = load_all_market_codes(db)
        all_market_mode = True
        if not codes:
            print("\n[ERROR] 无法加载全市场股票列表")
            db.close()
            return
    else:
        codes = load_all_stock_codes()
        all_market_mode = False

    if not codes:
        print("\n[ERROR] 无股票可拉取（持仓和自选均为空）")
        db.close()
        return

    # 在 all_market 模式下，不区分股票类型（全市场都是股票）
    if all_market_mode:
        actual_stocks = codes  # 全市场模式下全部处理
        classified = {"stocks": codes, "etfs": [], "bonds": [], "other": []}
    else:
        classified = classify_stocks(codes)
        actual_stocks = classified["stocks"] + classified["etfs"]  # ETF也有日线数据

    if not all_market_mode:
        print(f"\n  股票: {len(classified['stocks'])} 只")
        print(f"  ETF:  {len(classified['etfs'])} 只")
        print(f"  债券: {len(classified['bonds'])} 只 (跳过)")
        print(f"  其他: {len(classified['other'])} 只 (跳过)")
        print(f"  合计: {len(codes)} 只代码")
    else:
        print(f"\n  全市场股票: {len(codes)} 只")

    # 检查是否需要跳过
    if args.skip_if_exists and args.type in ("all", "daily_price"):
        count_before = stats_before.get("daily_price", 0)
        if count_before > 5000:
            print(f"\n  [SKIP] daily_price 已有 {count_before} 行，跳过。使用 --type daily_price 强制拉取")
            args.type = args.type.replace("daily_price", "").strip() if args.type != "all" else "all_except_price"

    # === 1. 股票基础信息 ===
    if args.type in ("all", "stock_basic"):
        if not all_market_mode:  # all_market 已经在 load_all_market_codes 中写入
            fetch_stock_basic(actual_stocks, db)

    # === 2. 日线行情（全部历史）===
    if args.type in ("all", "daily_price"):
        fetch_daily_price_history(actual_stocks, db,
                                  incremental=args.incremental,
                                  all_market=all_market_mode,
                                  resume=args.resume)

    # === 3. 复权因子 ===
    if args.type in ("all", "adj_factor"):
        fetch_adj_factor_history(actual_stocks, db, all_market=all_market_mode)

    # === 4. 每日估值 ===
    if args.type in ("all", "daily_basic"):
        fetch_daily_basic_history(codes, db, all_market=all_market_mode)

    # === 5. 财报数据 ===
    if args.type in ("all", "fina_indicator"):
        fetch_financials_history(codes, db, all_market=all_market_mode)

    # === 6. 分红送转 ===
    if args.type in ("all", "dividend"):
        fetch_dividends_history(codes, db, all_market=all_market_mode)

    # === 7. 大盘指数 ===
    if args.type in ("all", "index_daily"):
        fetch_index_daily_history(db)

    # === 附：持仓+自选股入库 ===
    if args.type in ("all",) and not all_market_mode:
        save_portfolio_and_watchlist(db)

    # 汇总
    stats_after = db.get_table_stats()
    print_summary(stats_before, stats_after)

    db.close()


if __name__ == "__main__":
    main()
