"""
补拉剩余全量数据 — 北向资金+融资融券+资金流向+估值补漏

用法:
    python scripts/utils/fetch_remaining.py

D3 异常处理:
    触发条件                    一线修复                        仍失败兜底
    ──────────────────────────  ──────────────────────────────  ────────────────────────────
    Tushare API超时/限频        等待30秒后重试1次                跳过该数据类别，继续下一类
    北向资金API返回空           检查trade_date范围是否正确       标注"北向资金数据暂缺"
    融资融券API返回空           尝试缩小日期范围分批拉取          跳过，标注"数据源不可用"
    资金流向接口异常             检查ts_code格式                  跳过个股流向，仅保留市场流向
    估值数据获取失败             检查股票是否已退市               跳过退市股，仅拉取当前上市

D4 CHECKPOINT:
    [ ] CP1-数据非空: 每个类别拉取后检查行数 > 0
    [ ] CP2-日期范围正确: 拉取日期start<=end，不请求未来日期
    [ ] CP3-写入DB前验证: 关键字段(north_money/trade_date等)非NULL

D9 工作反例:
    #  反模式                    为什么不要做                    应该怎么做
    ──  ────────────────────────  ─────────────────────────────  ──────────────────────────
    1   bare except:pass吞掉错误  不知道哪里失败了                用具体异常类型+打印错误信息
    2   一次性拉取10年数据         容易超时/限频/超内存            按年分批拉取
    3   不检查写入结果             数据静默丢失                     每次upsert后验证行数变化
"""

import os
import sys
import time
from datetime import datetime

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from scripts.utils.tushare_client import pro
from scripts.utils.db_manager import DatabaseManager


def ensure_table(db, name, sql):
    """确保表存在"""
    try:
        db.conn.execute(sql)
        db.conn.commit()
    except Exception as e:
        print(f"  [WARN] 建表失败: {e}", flush=True)


def main():
    today_str = datetime.now().strftime("%Y%m%d")
    db = DatabaseManager()
    stats_before = db.get_table_stats()

    # ============================================================
    # 1. 北向资金全量 (当前只有3条，应有多年)
    # ============================================================
    print("=" * 60)
    print("  [1/4] 北向资金全量历史 (2014至今)")
    print("=" * 60)
    try:
        df = pro.moneyflow_hsgt(start_date="20140101", end_date=today_str)
        if df is not None and not df.empty:
            df = df.sort_values("trade_date")
            n = 0
            for _, row in df.iterrows():
                data = {
                    "north_money": row.get("north_money"),
                    "south_money": row.get("south_money"),
                    "hgt": row.get("hgt"),
                    "sgt": row.get("sgt"),
                    "north_net": row.get("north_net", row.get("net_north")),
                }
                trade_date = str(row.get("trade_date", ""))
                db.upsert_moneyflow_hsgt(trade_date, data)
                n += 1
            print(f"  北向资金: {n} 条 ({df.trade_date.min()} ~ {df.trade_date.max()})")
        else:
            print("  无数据")
    except Exception as e:
        print(f"  失败: {e}")

    # ============================================================
    # 2. 补拉之前失败的 daily_basic
    # ============================================================
    print(f"\n{'='*60}")
    print(f"  [2/4] 补拉失败估值数据")
    print(f"{'='*60}")

    # 可通过环境变量 RETRY_CODES 覆盖（逗号分隔），默认补拉曾失败的历史数据
    _retry_default = "300274.SZ,300308.SZ,600388.SH,600930.SH"
    RETRY_CODES = os.environ.get("RETRY_CODES", _retry_default).split(",")
    current_year = datetime.now().year

    for code in RETRY_CODES:
        total = 0
        print(f"  {code}...", end=" ", flush=True)
        for year in range(2010, current_year + 1):
            try:
                start = f"{year}0101"
                end = f"{year}1231" if year < current_year else datetime.now().strftime("%Y%m%d")
                df = pro.daily_basic(
                    ts_code=code, start_date=start, end_date=end,
                    fields="ts_code,trade_date,pe,pe_ttm,pb,total_mv,circ_mv,turnover_rate,volume_ratio"
                )
                if df is not None and not df.empty:
                    n = db.upsert_daily_basic(df)
                    total += n
                time.sleep(0.15)
            except Exception as e:
                print(f"    [WARN] {code} {year}年估值失败: {e}", flush=True)
        if total > 0:
            print(f"{total}行")
        else:
            print(f"仍无数据")

    # ============================================================
    # 3. 融资融券 (margin)
    # ============================================================
    print(f"\n{'='*60}")
    print(f"  [3/4] 融资融券全量")
    print(f"{'='*60}")

    ensure_table(db, "margin", """
        CREATE TABLE IF NOT EXISTS margin (
            trade_date  TEXT NOT NULL,
            exchange    TEXT NOT NULL,
            rzye        REAL,
            rqye        REAL,
            rzrqye      REAL,
            rzmre       REAL,
            rqyl        REAL,
            rzche       REAL,
            rqchl       REAL,
            rzjmre      REAL,
            rqjmyl      REAL,
            PRIMARY KEY (trade_date, exchange)
        )
    """)

    # 拉取所有交易日
    try:
        df_cal = pro.trade_cal(exchange="SSE", start_date="20100101", end_date=today_str)
        trade_dates = df_cal[df_cal["is_open"] == 1]["cal_date"].tolist() if df_cal is not None else []
        print(f"  交易日: {len(trade_dates)} 天")

        margin_total = 0
        for i, td in enumerate(trade_dates):
            try:
                df_m = pro.margin(trade_date=td)
                if df_m is not None and not df_m.empty:
                    cur = db.conn.cursor()
                    for _, row in df_m.iterrows():
                        cur.execute(
                            """INSERT OR REPLACE INTO margin
                            (trade_date, exchange, rzye, rqye, rzrqye, rzmre, rqyl, rzche, rqchl, rzjmre, rqjmyl)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            (
                                td,
                                str(row.get("exchange", "")),
                                float(row["rzye"]) if row.get("rzye") is not None and str(row["rzye"]) != "nan" else None,
                                float(row["rqye"]) if row.get("rqye") is not None and str(row["rqye"]) != "nan" else None,
                                float(row["rzrqye"]) if row.get("rzrqye") is not None and str(row["rzrqye"]) != "nan" else None,
                                float(row["rzmre"]) if row.get("rzmre") is not None and str(row["rzmre"]) != "nan" else None,
                                float(row["rqyl"]) if row.get("rqyl") is not None and str(row["rqyl"]) != "nan" else None,
                                float(row["rzche"]) if row.get("rzche") is not None and str(row["rzche"]) != "nan" else None,
                                float(row["rqchl"]) if row.get("rqchl") is not None and str(row["rqchl"]) != "nan" else None,
                                float(row["rzjmre"]) if row.get("rzjmre") is not None and str(row["rzjmre"]) != "nan" else None,
                                float(row["rqjmyl"]) if row.get("rqjmyl") is not None and str(row["rqjmyl"]) != "nan" else None,
                            )
                        )
                    margin_total += len(df_m)
                if i % 100 == 0 and i > 0:
                    print(f"    进度: {i}/{len(trade_dates)} ({margin_total} 行)")
                    db.conn.commit()
            except Exception as e:
                print(f"    [WARN] {td} 融资融券拉取失败: {e}", flush=True)
            time.sleep(0.08)
        db.conn.commit()
        print(f"  融资融券: {margin_total} 行")
    except Exception as e:
        print(f"  失败: {e}")

    # ============================================================
    # 4. 大盘资金流向 (大单/中单/小单)
    # ============================================================
    print(f"\n{'='*60}")
    print(f"  [4/4] 大盘资金流向 (超级/大/中/小单)")
    print(f"{'='*60}")

    ensure_table(db, "moneyflow_mkt", """
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

    MARKET_INDICES = ["000001.SH", "399001.SZ", "399006.SZ", "000688.SH"]
    MARKET_NAMES = ["上证指数", "深证成指", "创业板指", "科创50"]

    mf_total = 0
    for idx_code, idx_name in zip(MARKET_INDICES, MARKET_NAMES):
        try:
            df_mf = pro.moneyflow(ts_code=idx_code, start_date="20070101", end_date=today_str)
            if df_mf is not None and not df_mf.empty:
                cur = db.conn.cursor()
                for _, row in df_mf.iterrows():
                    cur.execute(
                        """INSERT OR REPLACE INTO moneyflow_mkt
                        (trade_date, ts_code, net_amount, buy_elg_amount, sell_elg_amount,
                         buy_lg_amount, sell_lg_amount, buy_md_amount, sell_md_amount,
                         buy_sm_amount, sell_sm_amount)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            str(row.get("trade_date", "")),
                            idx_code,
                            float(row["net_amount"]) if row.get("net_amount") is not None and str(row["net_amount"]) != "nan" else None,
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
                mf_total += len(df_mf)
                print(f"  {idx_name} ({idx_code}): {len(df_mf)} 条")
            time.sleep(0.3)
        except Exception as e:
            print(f"  {idx_name} ({idx_code}): 失败 - {e}")
    db.conn.commit()
    print(f"  资金流向: {mf_total} 行")

    # ============================================================
    # 汇总
    # ============================================================
    print(f"\n{'='*60}")
    print(f"  补拉完成 - 最终数据库状态")
    print(f"{'='*60}")
    stats_after = db.get_table_stats()

    for table in sorted(stats_after.keys()):
        before = stats_before.get(table, 0)
        after = stats_after.get(table, 0)
        delta = after - before
        if delta > 0:
            print(f"  {table}: {before:,} -> {after:,} (+{delta:,})")
        else:
            print(f"  {table}: {after:,}")

    import os as _os
    size_mb = _os.path.getsize(db.db_path) / 1024 / 1024
    print(f"\n  数据库大小: {size_mb:.1f} MB")
    db.close()


if __name__ == "__main__":
    main()
