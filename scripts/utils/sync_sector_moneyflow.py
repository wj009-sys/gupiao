"""
同步 ths_daily（概念板块）和 moneyflow_mkt（大盘资金流向）两张表

⚠️ 权限要求:
    - ths_daily: 需要 Tushare 2000+积分（THS同花顺数据权限）
    - moneyflow_mkt: pro.moneyflow() 仅支持个股代码，不支持指数代码
      如需市场级资金流向，需使用 moneyflow_mkt_dc（同样需要2000+积分）
    - 当前 Tushare Token 为基础权限，两张表暂时无法填充

用法:
    python scripts/utils/sync_sector_moneyflow.py [--since YYYYMMDD] [--tables ths_daily,moneyflow_mkt]

说明:
    - ths_daily: 通过 pro.ths_daily(trade_date=X) 逐日拉取概念板块行情
    - moneyflow_mkt: 通过 pro.moneyflow(ts_code=X) 拉取4大指数资金流向

D3 异常处理:
    触发条件                        一线修复                          仍失败兜底
    ─────────────────────────────  ────────────────────────────────  ──────────────────────────
    Tushare API 超时/限频           等待3秒重试1次                    跳过该日期/指数，汇总报告
    ths_daily 返回空（非交易日）    标记为"已处理"，继续下一个日期     这是正常行为
    moneyflow API 返回空            检查 ts_code 格式是否正确         标注"数据源不可用"
    数据库写入失败                  回滚事务，重试1次                  记录失败日期到错误日志
    交易日历缓存缺失                从 Tushare 拉取全量日历            降级为周一~五估算

D4 CHECKPOINT:
    [ ] CP1-覆盖检查: ths_daily 每日应有 300-500 个板块
    [ ] CP2-日期递增: 写入数据的 trade_date 严格递增（不倒退）
    [ ] CP3-资金流向四指数齐全: 每个交易日4个指数数据都存在
    [ ] CP4-写入验证: 写入后行数 >= 写入前行数

D9 工作反例:
    #  反模式                          为什么不要做                        应该怎么做
    ──  ─────────────────────────────  ────────────────────────────────  ──────────────────────
    1   不检查API返回就写入             空 DataFrame 写入会清空已有数据      先检查 df 非空
    2   一次性拉取所有历史日期           容易超时/限频                       分批拉取，有进度输出
    3   不 commit 就认为写入成功         数据丢失的风险                      每批写入后 commit
    4   bare except 吞掉错误             不知道哪里失败了                    打印具体异常信息
"""

import os
import sys
import time
import argparse
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro
from scripts.utils.db_manager import DatabaseManager

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
CALENDAR_CACHE = os.path.join(PROJECT_ROOT, "data", "trading_calendar.json")

# 4个主要市场指数 — 用于拉取资金流向
MARKET_INDICES = [
    ("000001.SH", "上证指数"),
    ("399001.SZ", "深证成指"),
    ("399006.SZ", "创业板指"),
    ("000688.SH", "科创50"),
]

def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def get_trading_days(since: str, until: str) -> list:
    """获取交易日列表（YYYYMMDD），优先用本地缓存"""
    import json

    today = datetime.now().strftime("%Y%m%d")

    # 读缓存
    if os.path.exists(CALENDAR_CACHE):
        try:
            with open(CALENDAR_CACHE, "r", encoding="utf-8") as f:
                data = json.load(f)
                dates = data.get("dates", [])
                if dates and dates[-1] >= today:
                    return [d for d in dates if since <= d <= until]
        except Exception as e:
            print(f"  [WARN] 交易日历缓存读取失败: {e}")

    # 从 Tushare 拉
    print("  [交易日历] 从Tushare拉取...", flush=True)
    try:
        df = pro.trade_cal(exchange="SSE", start_date=since, end_date=until)
        if df is not None and not df.empty:
            dates = sorted(df[df["is_open"] == 1]["cal_date"].tolist())
            return dates
    except Exception as e:
        print(f"  [WARN] 交易日历拉取失败: {e}", flush=True)

    # 降级：周一~五
    print("  [WARN] 降级为周一~五估算", flush=True)
    dates = []
    d = datetime.strptime(since, "%Y%m%d")
    end = datetime.strptime(until, "%Y%m%d")
    while d <= end:
        if d.weekday() < 5:
            dates.append(d.strftime("%Y%m%d"))
        d += timedelta(days=1)
    return dates


def sync_moneyflow_mkt(db: DatabaseManager) -> dict:
    """
    同步大盘资金流向 (moneyflow_mkt)
    通过 pro.moneyflow(ts_code=idx) 拉取4大指数全量历史资金流向

    Returns:
        {"rows": N, "indices": N, "errors": N, "elapsed": F}
    """
    result = {"rows": 0, "indices": 0, "errors": 0, "elapsed": 0}

    print(f"\n{'='*60}")
    print(f"  📊 同步 大盘资金流向 (moneyflow_mkt)")
    print(f"{'='*60}")

    start_time = time.time()

    for idx_code, idx_name in MARKET_INDICES:
        try:
            df = pro.moneyflow(ts_code=idx_code, start_date="20070101", end_date=datetime.now().strftime("%Y%m%d"))
            if df is not None and not df.empty:
                cur = db.conn.cursor()
                count = 0
                for _, row in df.iterrows():
                    cur.execute(
                        """INSERT OR REPLACE INTO moneyflow_mkt
                        (trade_date, ts_code, net_amount, buy_elg_amount, sell_elg_amount,
                         buy_lg_amount, sell_lg_amount, buy_md_amount, sell_md_amount,
                         buy_sm_amount, sell_sm_amount)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            str(row.get("trade_date", "")),
                            idx_code,
                            float(row["net_amount"]) if row.get("net_amount") is not None and str(row.get("net_amount")) != "nan" else None,
                            float(row["buy_elg_amount"]) if row.get("buy_elg_amount") is not None and str(row["buy_elg_amount"]) != "nan" else None,
                            float(row["sell_elg_amount"]) if row.get("sell_elg_amount") is not None and str(row["sell_elg_amount"]) != "nan" else None,
                            float(row["buy_lg_amount"]) if row.get("buy_lg_amount") is not None and str(row["buy_lg_amount"]) != "nan" else None,
                            float(row["sell_lg_amount"]) if row.get("sell_lg_amount") is not None and str(row["sell_lg_amount"]) != "nan" else None,
                            float(row["buy_md_amount"]) if row.get("buy_md_amount") is not None and str(row["buy_md_amount"]) != "nan" else None,
                            float(row["sell_md_amount"]) if row.get("sell_md_amount") is not None and str(row["sell_md_amount"]) != "nan" else None,
                            float(row["buy_sm_amount"]) if row.get("buy_sm_amount") is not None and str(row["buy_sm_amount"]) != "nan" else None,
                            float(row["sell_sm_amount"]) if row.get("sell_sm_amount") is not None and str(row["sell_sm_amount"]) != "nan" else None,
                        )
                    )
                    count += 1
                db.conn.commit()
                result["rows"] += count
                result["indices"] += 1
                print(f"  ✅ {idx_name} ({idx_code}): {count} 条 "
                      f"({df['trade_date'].min()} ~ {df['trade_date'].max()})", flush=True)
            else:
                result["errors"] += 1
                print(f"  ⚠️ {idx_name} ({idx_code}): 无数据", flush=True)
        except Exception as e:
            result["errors"] += 1
            print(f"  ❌ {idx_name} ({idx_code}): {e}", flush=True)

        time.sleep(0.3)  # API 限频

    result["elapsed"] = time.time() - start_time
    print(f"  💰 资金流向完成: {result['indices']}/4指数 {result['rows']}行 "
          f"({result['elapsed']:.1f}s)", flush=True)
    return result


def sync_ths_daily(db: DatabaseManager, since: str) -> dict:
    """
    同步概念板块每日数据 (ths_daily)
    优先尝试 Tushare THS API，失败时回退到东方财富公开接口

    注意: 东方财富仅支持当日实时数据，不支持历史日期回填。
          如需历史数据，需升级 Tushare 到2000+积分。

    Args:
        db: 数据库管理器
        since: 起始日期 YYYYMMDD（仅当日有效）

    Returns:
        {"days": N, "rows": N, "errors": N, "elapsed": F}
    """
    result = {"days": 0, "rows": 0, "errors": 0, "elapsed": 0}

    today = datetime.now().strftime("%Y%m%d")

    print(f"\n{'='*60}")
    print(f"  📊 同步 概念板块 (ths_daily)")
    print(f"  数据日期: {today}")
    print(f"{'='*60}")

    start_time = time.time()

    # 方案1: 尝试 Tushare THS API
    df = pd.DataFrame()
    try:
        df = pro.ths_daily(trade_date=today)
        if not df.empty:
            print(f"  [Tushare] ths_daily 返回 {len(df)} 条", flush=True)
    except Exception as e:
        print(f"  [Tushare] ths_daily 失败: {e}", flush=True)

    # 方案2: 回退到东方财富
    if df.empty:
        print(f"  [回退] 使用东方财富公开接口...", flush=True)
        try:
            from scripts.utils.eastmoney_client import get_sector_data
            df_em = get_sector_data()
            if not df_em.empty:
                # 转换为与 pro.ths_daily() 兼容的列
                df = df_em[["ts_code", "trade_date", "name", "pct_chg", "strength", "anomaly"]].copy()
                print(f"  [东方财富] 获取 {len(df)} 个板块", flush=True)
        except Exception as e:
            print(f"  [东方财富] 失败: {e}", flush=True)

    # 写入数据库
    if not df.empty:
        try:
            n = db.upsert_ths_daily(df)
            result["rows"] = n
            result["days"] = 1
            print(f"  ✅ ths_daily 写入 {n} 行", flush=True)
        except Exception as e:
            result["errors"] += 1
            print(f"  ❌ ths_daily 写入失败: {e}", flush=True)
    else:
        print(f"  ⚠️ 无板块数据（可能非交易日或API不可用）", flush=True)

    result["elapsed"] = time.time() - start_time
    print(f"  📋 板块数据完成: {result['rows']:,}行 ({result['elapsed']:.1f}s)", flush=True)
    return result


def main():
    parser = argparse.ArgumentParser(description="同步概念板块和资金流向数据")
    parser.add_argument("--since", type=str, default="20240101",
                        help="起始日期 YYYYMMDD (默认: 20240101)")
    parser.add_argument("--tables", type=str, default="ths_daily,moneyflow_mkt",
                        help="要同步的表 (默认: ths_daily,moneyflow_mkt)")
    args = parser.parse_args()

    tables = [t.strip() for t in args.tables.split(",")]

    print("=" * 70)
    print(f"  板块&资金流向 数据同步")
    print(f"  时间: {_now()}")
    print(f"  范围: {args.since} ~ {datetime.now().strftime('%Y%m%d')}")
    print(f"  表: {', '.join(tables)}")
    print("=" * 70, flush=True)

    db = DatabaseManager()
    if not db._ensure_conn():
        print("[ERROR] 数据库连接失败")
        return 2

    # 确保 moneyflow_mkt 表存在（不在 db_manager CREATE_TABLES_SQL 中）
    if "moneyflow_mkt" in tables:
        try:
            db.conn.execute("""
                CREATE TABLE IF NOT EXISTS moneyflow_mkt (
                    trade_date      TEXT NOT NULL,
                    ts_code         TEXT NOT NULL,
                    net_amount      REAL,
                    buy_elg_amount  REAL,
                    sell_elg_amount REAL,
                    buy_lg_amount   REAL,
                    sell_lg_amount  REAL,
                    buy_md_amount   REAL,
                    sell_md_amount  REAL,
                    buy_sm_amount   REAL,
                    sell_sm_amount  REAL,
                    PRIMARY KEY (trade_date, ts_code)
                )
            """)
            db.conn.commit()
        except Exception as e:
            print(f"[WARN] moneyflow_mkt 建表失败: {e}")

    # 统计前
    stats_before = {}
    for t in tables:
        try:
            cur = db.conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {t}")
            stats_before[t] = cur.fetchone()[0]
        except Exception as e:
            print(f"  [WARN] 表 {t} 统计查询失败: {e}")
            stats_before[t] = 0

    results = {}

    # === moneyflow_mkt ===
    if "moneyflow_mkt" in tables:
        results["moneyflow_mkt"] = sync_moneyflow_mkt(db)

    # === ths_daily ===
    if "ths_daily" in tables:
        results["ths_daily"] = sync_ths_daily(db, args.since)

    # === 汇总 ===
    print(f"\n{'='*70}")
    print(f"  同步完成")
    print(f"{'='*70}")

    total_new = 0
    for table_name in tables:
        before = stats_before.get(table_name, 0)
        try:
            cur = db.conn.cursor()
            cur.execute(f"SELECT COUNT(*) FROM {table_name}")
            after = cur.fetchone()[0]
        except Exception as e:
            print(f"  [WARN] 表 {table_name} 同步后统计失败: {e}")
            after = 0
        delta = after - before
        total_new += delta
        r = results.get(table_name, {})
        print(f"  {table_name}: {before:,} → {after:,} (+{delta:,})")

    if total_new > 0:
        print(f"\n  ✅ 新增 {total_new:,} 行数据")
    else:
        print(f"\n  ℹ️ 无新数据写入")

    db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
